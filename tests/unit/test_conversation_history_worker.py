"""Durable conversation history rollup worker."""

from __future__ import annotations

import asyncio

import pytest
from tests.conftest import make_settings

from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    HistoryJobKind,
    HistoryJobOutcome,
    HistoryJobStatus,
)
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.conversation.history.worker import (
    ConversationHistoryJobResult,
    ConversationHistoryWorker,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelExecutionPriority,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.persistence.database import Database


async def _until(predicate, *, seconds: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


def _settings(database_url: str, **overrides: object):
    values = {
        "conversation_history_rollup_poll_seconds": 0.05,
        "conversation_history_rollup_lease_seconds": 5,
        "conversation_history_rollup_timeout_seconds": 1.0,
        "conversation_history_rollup_worker_concurrency": 1,
        "conversation_history_rollup_retry_seconds": "1,2,4",
        "conversation_history_rollup_max_attempts": 7,
    }
    values.update(overrides)
    return make_settings(database_url, **values)


def _identity(peer: str = "1001") -> ConversationHistoryIdentity:
    return ConversationHistoryIdentity(
        bot_user_id="bot-1",
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id=peer,
    )


async def _enqueue(
    repository: ConversationHistoryRepository,
    *,
    peer: str = "1001",
    fingerprint: str = "fp-1",
) -> tuple[int, int]:
    state = await repository.get_or_create_state(_identity(peer))
    job = await repository.enqueue_job(
        state_id=state.id,
        job_kind=HistoryJobKind.RAW_RANGE,
        source_level=0,
        source_start_id=1,
        source_end_id=8,
        source_fingerprint=fingerprint,
        summarizer_version="flash-v1",
    )
    return state.id, job.id


class _CompleteProcessor:
    def __init__(self) -> None:
        self.jobs: list[int] = []

    async def process(self, job) -> ConversationHistoryJobResult:
        self.jobs.append(job.id)
        return ConversationHistoryJobResult(outcome=HistoryJobOutcome.NO_CHANGE)


class _FailProcessor:
    async def process(self, job) -> ConversationHistoryJobResult:
        del job
        raise RuntimeError("synthetic compaction failure")


class _HoldProcessor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self.states: list[int] = []

    async def process(self, job) -> ConversationHistoryJobResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.states.append(job.state_id)
        self.started.set()
        await self.release.wait()
        self.active -= 1
        return ConversationHistoryJobResult(outcome=HistoryJobOutcome.NO_CHANGE)


class _TimeoutProcessor:
    async def process(self, job) -> ConversationHistoryJobResult:
        del job
        await asyncio.sleep(1)
        return ConversationHistoryJobResult(outcome=HistoryJobOutcome.NO_CHANGE)


def _executor(provider: LLMProvider) -> TaskModelExecutor:
    profile = ModelProfile(
        id="test",
        provider="fake",
        protocol=ModelProtocol.CHAT_COMPLETIONS,
        model="fake",
        timeout_seconds=5,
        max_retries=0,
        default_temperature=0,
        default_max_output_tokens=128,
        thinking_enabled=False,
        structured_output_mode=StructuredOutputMode.FUNCTION_TOOL,
        capabilities=frozenset(ModelCapability),
    )
    routes = {
        task: ModelRoute(task=task, profile_id="test", required_capabilities=frozenset())
        for task in ModelTask
    }
    catalog = ModelProfileCatalog(profiles={"test": profile}, routes=routes)
    return TaskModelExecutor(
        router=ModelRouter(catalog),
        pool=ModelClientPool(injected_profiles={"test": provider}),
        max_concurrency=1,
    )


class _PreemptibleProvider(LLMProvider):
    def __init__(self) -> None:
        self.background_started = asyncio.Event()

    async def complete(self, request: ChatRequest) -> ChatResponse:
        if request.messages[0].content == "background":
            self.background_started.set()
            await asyncio.Event().wait()
        return ChatResponse(content="foreground", latency_seconds=0)


@pytest.mark.asyncio
async def test_worker_claims_and_completes_job(database: Database) -> None:
    repository = ConversationHistoryRepository(database)
    processor = _CompleteProcessor()
    worker = ConversationHistoryWorker(
        settings=_settings(database.url),
        repository=repository,
        processor=processor,
    )
    _state_id, job_id = await _enqueue(repository)
    await worker.start()
    worker.notify()
    try:
        await _until(lambda: worker.metrics.completed == 1)
        job = await repository.claim_next_job(lease_owner="probe", lease_seconds=1)
        assert job is None
        assert processor.jobs == [job_id]
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_worker_retries_with_delay_and_does_not_move_frontier(
    database: Database,
) -> None:
    repository = ConversationHistoryRepository(database)
    worker = ConversationHistoryWorker(
        settings=_settings(database.url, conversation_history_rollup_max_attempts=3),
        repository=repository,
        processor=_FailProcessor(),
    )
    state_id, _job_id = await _enqueue(repository)
    await worker.start()
    worker.notify()
    try:
        await _until(lambda: worker.metrics.retried == 1)
        state = await repository.get_or_create_state(_identity())
        assert state.id == state_id
        assert state.active_frontier_end_event_id == 0
        stolen = await repository.claim_next_job(lease_owner="probe", lease_seconds=1)
        assert stolen is None
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_worker_releases_stale_leases_on_start(database: Database) -> None:
    repository = ConversationHistoryRepository(database)
    _state_id, job_id = await _enqueue(repository)
    claimed = await repository.claim_next_job(lease_owner="dead-worker", lease_seconds=1)
    assert claimed is not None
    await asyncio.sleep(1.05)
    worker = ConversationHistoryWorker(
        settings=_settings(database.url),
        repository=repository,
        processor=_CompleteProcessor(),
    )
    await worker.start()
    worker.notify()
    try:
        await _until(lambda: worker.metrics.completed == 1)
        assert worker.metrics.stale_leases_released >= 1
        assert claimed.id == job_id
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_shutdown_releases_inflight_lease(database: Database) -> None:
    repository = ConversationHistoryRepository(database)
    processor = _HoldProcessor()
    worker = ConversationHistoryWorker(
        settings=_settings(database.url),
        repository=repository,
        processor=processor,
    )
    await _enqueue(repository)
    await worker.start()
    worker.notify()
    try:
        await asyncio.wait_for(processor.started.wait(), timeout=2)
    except Exception:
        await worker.close()
        raise
    await worker.close()
    reclaimed = await repository.claim_next_job(lease_owner="after-close", lease_seconds=5)
    assert reclaimed is not None
    assert reclaimed.status is HistoryJobStatus.PROCESSING
    await repository.complete_job(
        reclaimed.id,
        lease_owner="after-close",
        outcome=HistoryJobOutcome.NO_CHANGE,
        result_summary_id=None,
    )


@pytest.mark.asyncio
async def test_worker_retries_cancelled_background_model_call(database: Database) -> None:
    repository = ConversationHistoryRepository(database)
    provider = _PreemptibleProvider()
    executor = _executor(provider)

    class _BackgroundProcessor:
        async def process(self, job) -> ConversationHistoryJobResult:
            del job
            await executor.execute(
                ModelTask.CONVERSATION_COMPACTION,
                ChatRequest(messages=(ChatMessage(role="user", content="background"),)),
                priority=ModelExecutionPriority.BEST_EFFORT_BACKGROUND,
            )
            return ConversationHistoryJobResult(outcome=HistoryJobOutcome.NO_CHANGE)

    worker = ConversationHistoryWorker(
        settings=_settings(database.url),
        repository=repository,
        processor=_BackgroundProcessor(),
    )
    await _enqueue(repository)
    await worker.start()
    worker.notify()
    try:
        await asyncio.wait_for(provider.background_started.wait(), timeout=2)
        foreground = await executor.execute(
            ModelTask.CHAT_AGENT,
            ChatRequest(messages=(ChatMessage(role="user", content="foreground"),)),
        )
        assert foreground.content == "foreground"
        await _until(lambda: worker.metrics.retried == 1)
        assert worker.metrics.completed == 0
    finally:
        await worker.close()
        await executor.close()


@pytest.mark.asyncio
async def test_queue_wake_processes_before_poll_timeout(database: Database) -> None:
    repository = ConversationHistoryRepository(database)
    processor = _CompleteProcessor()
    worker = ConversationHistoryWorker(
        settings=_settings(database.url, conversation_history_rollup_poll_seconds=2.0),
        repository=repository,
        processor=processor,
    )
    await worker.start()
    try:
        await asyncio.sleep(0.05)
        await _enqueue(repository)
        started = asyncio.get_running_loop().time()
        worker.notify()
        await _until(lambda: worker.metrics.completed == 1, seconds=1.0)
        assert asyncio.get_running_loop().time() - started < 1.0
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_same_conversation_is_serial_and_other_conversations_can_run_together(
    database: Database,
) -> None:
    repository = ConversationHistoryRepository(database)
    processor = _HoldProcessor()
    worker = ConversationHistoryWorker(
        settings=_settings(database.url, conversation_history_rollup_worker_concurrency=2),
        repository=repository,
        processor=processor,
    )
    state_a, _first = await _enqueue(repository, peer="1001", fingerprint="a-1")
    await _enqueue(repository, peer="1001", fingerprint="a-2")
    state_b, _other = await _enqueue(repository, peer="2002", fingerprint="b-1")
    await worker.start()
    worker.notify()
    try:
        await _until(lambda: processor.max_active == 2, seconds=2.0)
        assert processor.max_active == 2
        assert {state_a, state_b} <= set(processor.states)
        assert processor.states.count(state_a) == 1
        processor.release.set()
        await _until(lambda: worker.metrics.completed == 3)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_job_timeout_retries_without_frontier_change(database: Database) -> None:
    repository = ConversationHistoryRepository(database)
    worker = ConversationHistoryWorker(
        settings=_settings(database.url, conversation_history_rollup_timeout_seconds=0.05),
        repository=repository,
        processor=_TimeoutProcessor(),
    )
    state_id, _job_id = await _enqueue(repository)
    await worker.start()
    worker.notify()
    try:
        await _until(lambda: worker.metrics.retried == 1)
        state = await repository.get_or_create_state(_identity())
        assert state.id == state_id
        assert state.active_frontier_end_event_id == 0
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_worker_can_stay_unstarted_and_chat_path_does_not_need_it(
    database: Database,
) -> None:
    repository = ConversationHistoryRepository(database)
    worker = ConversationHistoryWorker(
        settings=_settings(database.url, conversation_history_rollup_enabled=False),
        repository=repository,
        processor=_CompleteProcessor(),
    )
    await _enqueue(repository)
    await worker.start()
    try:
        health = await worker.health()
        assert health.enabled is False
        assert health.running is False
        assert health.ok is True
        leftover = await repository.claim_next_job(lease_owner="chat-path", lease_seconds=5)
        assert leftover is not None
        await repository.complete_job(
            leftover.id,
            lease_owner="chat-path",
            outcome=HistoryJobOutcome.NO_CHANGE,
            result_summary_id=None,
        )
    finally:
        await worker.close()
