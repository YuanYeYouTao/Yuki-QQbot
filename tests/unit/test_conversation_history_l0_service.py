"""Level-zero conversation history observe, extractive, and Flash upgrade."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_settings

from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    HistoryJobKind,
    HistorySummaryMode,
    HistorySummaryStatus,
)
from qq_ai_bot.conversation.history.policy import HistoryCompactionPolicy
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.conversation.history.service import ConversationHistoryService
from qq_ai_bot.conversation.history.source import build_source_snapshot
from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.model_runtime.models import (
    ModelExecutionPriority,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.structured import StructuredTaskError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.runtime.origin import TurnOrigin

_NOW = datetime(2026, 8, 19, 7, 0, tzinfo=UTC)
_PRIVATE = ConversationHistoryIdentity(
    bot_user_id="bot-1",
    scope_type=ScopeType.PRIVATE,
    private_peer_user_id="1001",
)


def _settings(database_url: str, **overrides: object):
    values = {
        "conversation_history_rollup_poll_seconds": 0.05,
        "conversation_history_llm_origins": "user_message",
    }
    values.update(overrides)
    return make_settings(database_url, **values)


def _valid_output() -> dict[str, object]:
    return {
        "narrative": "用户连续发送了多条消息，尚未形成明确决定。",
        "decisions": [],
        "open_loops": [{"item": "会话仍在继续", "owner": "用户", "state": "pending"}],
        "constraints": [],
        "entities": [{"name": "用户", "role": "当前说话人"}],
        "state_changes": [],
        "uncertainties": [{"claim": "后续意图", "reason": "尚未确认"}],
        "terminal_tool_outcomes": [],
    }


class _SummarizerExecutor:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0
        self.priorities: list[ModelExecutionPriority] = []

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
    ) -> ChatResponse:
        assert task is ModelTask.CONVERSATION_COMPACTION
        self.calls += 1
        self.priorities.append(priority)
        del request
        return ChatResponse(content=json.dumps(self.payload), latency_seconds=0.01)

    def model_name(self, task: ModelTask) -> str:
        del task
        return "flash"

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        del task
        return StructuredOutputMode.TEXT_JSON


class _InvalidJsonExecutor:
    async def execute(self, task, request, *, priority=ModelExecutionPriority.FOREGROUND):
        del task, request, priority
        return ChatResponse(content="{bad", latency_seconds=0)

    def model_name(self, task: ModelTask) -> str:
        del task
        return "flash"

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        del task
        return StructuredOutputMode.TEXT_JSON


async def _append(
    ledger: EventLedgerRepository,
    index: int,
    *,
    body: str,
    origin: str = TurnOrigin.USER_MESSAGE.value,
) -> EventRecord:
    record, _created = await ledger.append(
        bot_user_id="bot-1",
        platform_message_id=f"m-{index}",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content=body,
        private_peer_user_id="1001",
        occurred_at=_NOW + timedelta(seconds=index),
        origin=origin,
    )
    return record


def _rendered(records: list[EventRecord]) -> tuple[tuple[int, tuple[int, ...], ChatMessage], ...]:
    return tuple(
        (record.id, (record.id,), ChatMessage(role="user", content=record.content))
        for record in records
    )


def _first_protected_event_id(records: list[EventRecord]) -> int:
    snapshot = build_source_snapshot(
        state_id=1,
        reset_at=None,
        scope_type=ScopeType.PRIVATE,
        events=tuple(records),
    )
    boundary = HistoryCompactionPolicy().hot_tail_boundary(snapshot)
    assert boundary.first_protected_event_id is not None
    return boundary.first_protected_event_id


def _service(
    database: Database,
    *,
    models=None,
    **setting_overrides: object,
) -> tuple[ConversationHistoryService, EventLedgerRepository, ConversationHistoryRepository]:
    settings = _settings(database.url, **setting_overrides)
    repository = ConversationHistoryRepository(database)
    ledger = EventLedgerRepository(database)
    service = ConversationHistoryService(
        settings=settings,
        repository=repository,
        ledger=ledger,
        models=models,
    )
    ledger.set_history_observer(service.observe_event)
    return service, ledger, repository


async def _ensure_extractive(
    service: ConversationHistoryService,
    records: list[EventRecord],
):
    return await service.ensure_extractive_coverage(
        records[-1],
        rendered=_rendered(records),
        anchor_event_id=records[0].id,
        high_event_limit=1000,
        high_character_limit=4000,
        fallback_anchor_event_id=None,
    )


@pytest.mark.asyncio
async def test_prefetch_enqueues_after_32_uncovered_events(database: Database) -> None:
    _service_obj, ledger, repository = _service(database)
    records = [await _append(ledger, index, body="e" * 100) for index in range(1, 81)]
    job = await repository.claim_next_job(lease_owner="t", lease_seconds=5)
    assert job is not None
    assert job.job_kind is HistoryJobKind.RAW_RANGE
    assert len(range(job.source_start_id, job.source_end_id + 1)) <= 100
    assert job.source_end_id < _first_protected_event_id(records)


@pytest.mark.asyncio
async def test_prefetch_enqueues_after_8000_uncovered_characters(database: Database) -> None:
    _service_obj, ledger, repository = _service(database)
    records = [await _append(ledger, index, body="c" * 500) for index in range(1, 69)]
    job = await repository.claim_next_job(lease_owner="t", lease_seconds=5)
    assert job is not None
    assert job.source_end_id < _first_protected_event_id(records)
    leftover = await repository.claim_next_job(lease_owner="t2", lease_seconds=5)
    assert leftover is None


@pytest.mark.asyncio
async def test_below_threshold_does_not_enqueue(database: Database) -> None:
    _service_obj, ledger, repository = _service(database)
    for index in range(1, 11):
        await _append(ledger, index, body="short")
    job = await repository.claim_next_job(lease_owner="t", lease_seconds=5)
    assert job is None


@pytest.mark.asyncio
async def test_must_roll_writes_extractive_without_model(database: Database) -> None:
    executor = _SummarizerExecutor(_valid_output())
    service, ledger, repository = _service(database, models=executor)
    records = [await _append(ledger, index, body="w" * 500) for index in range(1, 69)]
    summary = await _ensure_extractive(service, records)
    assert summary is not None
    assert summary.mode is HistorySummaryMode.EXTRACTIVE
    assert executor.calls == 0
    assert not service.allow_raw_window_shift(has_active_coverage=False)
    assert service.allow_raw_window_shift(has_active_coverage=True)
    snapshot = await repository.load_context_snapshot(summary.state_id)
    assert snapshot.coverage_end_event_id == summary.end_event_id
    assert tuple(member.source_event_id for member in summary.members) == tuple(
        range(summary.start_event_id, summary.end_event_id + 1)
    )
    protected = _first_protected_event_id(records)
    assert all(member.source_event_id < protected for member in summary.members)
    assert records[-1].id not in {member.source_event_id for member in summary.members}


@pytest.mark.asyncio
async def test_second_extractive_advances_coverage_without_a_hole(database: Database) -> None:
    executor = _SummarizerExecutor(_valid_output())
    service, ledger, repository = _service(database, models=executor)
    records = [await _append(ledger, index, body="w" * 500) for index in range(1, 69)]
    first = await _ensure_extractive(service, records)
    assert first is not None
    second = await _ensure_extractive(service, records)
    assert second is not None
    assert second.id != first.id
    assert second.start_event_id == first.end_event_id + 1
    snapshot = await repository.load_context_snapshot(first.state_id)
    assert snapshot.coverage_end_event_id == second.end_event_id
    uncovered = [record for record in records if record.id > first.end_event_id]
    protected = _first_protected_event_id(uncovered)
    assert second.end_event_id < protected
    assert all(member.source_event_id < protected for member in second.members)
    assert records[-1].id not in {member.source_event_id for member in second.members}


@pytest.mark.asyncio
async def test_flash_replaces_extractive_on_same_fingerprint(database: Database) -> None:
    executor = _SummarizerExecutor(_valid_output())
    service, ledger, repository = _service(database, models=executor)
    records = [await _append(ledger, index, body="w" * 500) for index in range(1, 69)]
    extractive = await _ensure_extractive(service, records)
    assert extractive is not None
    fingerprint = extractive.source_fingerprint
    job = await repository.claim_next_job(lease_owner="flash", lease_seconds=30)
    assert job is not None
    assert job.source_fingerprint == fingerprint
    result = await service.process(job)
    await repository.complete_job(
        job.id,
        lease_owner="flash",
        outcome=result.outcome,
        result_summary_id=result.result_summary_id,
    )
    snapshot = await repository.load_context_snapshot(extractive.state_id)
    assert len(snapshot.frontier) == 1
    upgraded = snapshot.frontier[0]
    assert upgraded.mode is HistorySummaryMode.MODEL_SUMMARY
    assert upgraded.status is HistorySummaryStatus.ACTIVE
    assert upgraded.source_fingerprint == fingerprint
    assert upgraded.id != extractive.id
    assert executor.calls == 1
    assert executor.priorities == [ModelExecutionPriority.EXCLUSIVE]
    identity = ConversationHistoryIdentity(
        bot_user_id="bot-1",
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id="1001",
    )
    raw = await repository.load_source_events(
        identity,
        start_event_id=records[0].id,
        end_event_id=records[-1].id,
    )
    assert [item.content for item in raw] == [record.content for record in records]


@pytest.mark.asyncio
async def test_summarizer_failure_keeps_extractive(database: Database) -> None:
    service, ledger, repository = _service(database, models=_InvalidJsonExecutor())
    records = [await _append(ledger, index, body="w" * 500) for index in range(1, 69)]
    extractive = await _ensure_extractive(service, records)
    assert extractive is not None
    job = await repository.claim_next_job(lease_owner="flash", lease_seconds=30)
    assert job is not None
    with pytest.raises(StructuredTaskError):
        await service.process(job)
    snapshot = await repository.load_context_snapshot(extractive.state_id)
    assert snapshot.frontier[0].id == extractive.id
    assert snapshot.frontier[0].status is HistorySummaryStatus.ACTIVE
    assert snapshot.frontier[0].mode is HistorySummaryMode.EXTRACTIVE


@pytest.mark.asyncio
async def test_duplicate_observe_does_not_double_count(database: Database) -> None:
    service, ledger, repository = _service(database)
    first, created = await ledger.append(
        bot_user_id="bot-1",
        platform_message_id="dup-1",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="hello",
        private_peer_user_id="1001",
        occurred_at=_NOW,
    )
    second, created_again = await ledger.append(
        bot_user_id="bot-1",
        platform_message_id="dup-1",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="hello",
        private_peer_user_id="1001",
        occurred_at=_NOW,
    )
    await service.observe_event(first)
    await service.observe_event(first)
    assert created is True
    assert created_again is False
    assert first.id == second.id
    state = await repository.get_or_create_state(_PRIVATE)
    assert state.pending_event_count == 1


@pytest.mark.asyncio
async def test_restart_can_process_durable_job(database: Database) -> None:
    _first, ledger, repository = _service(database)
    records = [await _append(ledger, index, body="e" * 100) for index in range(1, 81)]
    job = await repository.claim_next_job(lease_owner="restart", lease_seconds=30)
    assert job is not None
    restarted = ConversationHistoryService(
        settings=_settings(database.url),
        repository=repository,
        ledger=ledger,
        models=_SummarizerExecutor(_valid_output()),
    )
    result = await restarted.process(job)
    await repository.complete_job(
        job.id,
        lease_owner="restart",
        outcome=result.outcome,
        result_summary_id=result.result_summary_id,
    )
    snapshot = await repository.load_context_snapshot(job.state_id)
    assert snapshot.frontier[0].mode is HistorySummaryMode.MODEL_SUMMARY
    assert snapshot.coverage_end_event_id == job.source_end_id
    assert snapshot.coverage_end_event_id < _first_protected_event_id(records)


@pytest.mark.asyncio
async def test_context_reset_starts_a_new_state(database: Database) -> None:
    _service_obj, ledger, repository = _service(database)
    await _append(ledger, 1, body="before-reset")
    identity = ConversationIdentity.private("1001")
    await ledger.set_context_reset(identity)
    await _append(ledger, 2, body="after-reset")
    old_state = await repository.get_or_create_state(_PRIVATE)
    reset_at = await ledger.context_reset(identity)
    new_state = await repository.get_or_create_state(
        ConversationHistoryIdentity(
            bot_user_id="bot-1",
            scope_type=ScopeType.PRIVATE,
            private_peer_user_id="1001",
            reset_at=reset_at,
        )
    )
    assert old_state.id != new_state.id
    assert new_state.last_seen_event_id > old_state.last_seen_event_id


@pytest.mark.parametrize(
    "origin",
    [
        TurnOrigin.AUTONOMOUS_GROUP.value,
        TurnOrigin.PLUGIN_SESSION.value,
        TurnOrigin.PLUGIN_BACKGROUND.value,
    ],
)
@pytest.mark.asyncio
async def test_non_user_origin_does_not_enqueue_flash(database: Database, origin: str) -> None:
    _service_obj, ledger, repository = _service(database)
    for index in range(1, 81):
        await _append(ledger, index, body="e" * 100, origin=origin)
    job = await repository.claim_next_job(lease_owner="t", lease_seconds=5)
    assert job is None
    state = await repository.get_or_create_state(_PRIVATE)
    assert state.pending_event_count == 80


@pytest.mark.asyncio
async def test_quality_ledger_without_observer_does_not_enqueue(database: Database) -> None:
    repository = ConversationHistoryRepository(database)
    ledger = EventLedgerRepository(database)
    await ledger.append(
        bot_user_id="bot-1",
        platform_message_id="quality-1",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="fixture",
        private_peer_user_id="1001",
    )
    job = await repository.claim_next_job(lease_owner="t", lease_seconds=5)
    assert job is None
