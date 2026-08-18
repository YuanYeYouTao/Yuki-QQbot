"""Hierarchical L0 -> L1 -> L2 conversation history rollup."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_settings

from qq_ai_bot.conversation.history.errors import FrontierInvariantError
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    HistoryJobKind,
    HistoryJobOutcome,
    HistorySummaryMode,
    HistorySummaryStatus,
)
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.conversation.history.service import ConversationHistoryService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, InboundMessage, SenderIdentity
from qq_ai_bot.model_runtime.models import (
    ModelExecutionPriority,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository

_NOW = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
_PRIVATE = ConversationHistoryIdentity(
    bot_user_id="bot-1",
    scope_type=ScopeType.PRIVATE,
    private_peer_user_id="1001",
)


def _valid_output(narrative: str = "多段会话已压缩，未完成事项仍保留。") -> dict[str, object]:
    return {
        "narrative": narrative,
        "decisions": [],
        "open_loops": [{"item": "会话仍在继续", "owner": "用户", "state": "pending"}],
        "constraints": [],
        "entities": [{"name": "用户", "role": "当前说话人"}],
        "state_changes": [],
        "uncertainties": [{"claim": "后续意图", "reason": "尚未确认"}],
        "terminal_tool_outcomes": [],
    }


class _SummarizerExecutor:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or _valid_output()
        self.calls = 0

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
    ) -> ChatResponse:
        assert task is ModelTask.CONVERSATION_COMPACTION
        self.calls += 1
        del request, priority
        return ChatResponse(content=json.dumps(self.payload), latency_seconds=0.01)

    def model_name(self, task: ModelTask) -> str:
        del task
        return "flash"

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        del task
        return StructuredOutputMode.TEXT_JSON


class _BoomExecutor:
    async def execute(self, task, request, *, priority=ModelExecutionPriority.FOREGROUND):
        del task, request, priority
        raise RuntimeError("parent write failed")

    def model_name(self, task: ModelTask) -> str:
        del task
        return "flash"

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        del task
        return StructuredOutputMode.TEXT_JSON


def _service(
    database: Database,
    *,
    models=None,
    **overrides: object,
) -> tuple[ConversationHistoryService, ConversationHistoryRepository]:
    settings = make_settings(database.url, **overrides)
    repository = ConversationHistoryRepository(database)
    ledger = EventLedgerRepository(database)
    service = ConversationHistoryService(
        settings=settings,
        repository=repository,
        ledger=ledger,
        models=models,
    )
    return service, repository


async def _seed_events(database: Database, count: int, *, start: int = 1) -> tuple[int, ...]:
    ledger = EventLedgerRepository(database)
    ids: list[int] = []
    for index in range(start, start + count):
        inbound = InboundMessage(
            message_id=f"m-{index}",
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id="1001"),
            text=f"line-{index}",
            bot_user_id="bot-1",
            received_at=_NOW + timedelta(seconds=index),
        )
        record, _created = await ledger.append_inbound(inbound, bot_user_id="bot-1")
        ids.append(record.id)
    return tuple(ids)


async def _commit_model_l0(
    repository: ConversationHistoryRepository,
    *,
    state_id: int,
    event_ids: tuple[int, ...],
    fingerprint: str,
    rendered_text: str = "child-summary",
):
    return await repository.commit_l0_summary(
        state_id=state_id,
        event_ids=event_ids,
        fingerprint=fingerprint,
        mode=HistorySummaryMode.MODEL_SUMMARY,
        summarizer_version="conversation-rollup-v1",
        rendered_text=rendered_text,
        structured_payload_json="{}",
        start_occurred_at=_NOW,
        end_occurred_at=_NOW,
        source_character_count=20,
    )


@pytest.mark.asyncio
async def test_eight_l0_summaries_roll_into_one_l1(database: Database) -> None:
    executor = _SummarizerExecutor()
    service, repository = _service(database, models=executor)
    event_ids = await _seed_events(database, 8)
    state = await repository.get_or_create_state(_PRIVATE)
    children = []
    for index, event_id in enumerate(event_ids, start=1):
        children.append(
            await _commit_model_l0(
                repository,
                state_id=state.id,
                event_ids=(event_id,),
                fingerprint=f"l0-{index}",
            )
        )
    await service.consider_parent_rollup(state.id)
    job = await repository.claim_next_job(lease_owner="p", lease_seconds=30)
    assert job is not None
    assert job.job_kind is HistoryJobKind.SUMMARY_ROLLUP
    assert job.source_level == 0
    result = await service.process(job)
    await repository.complete_job(
        job.id,
        lease_owner="p",
        outcome=result.outcome,
        result_summary_id=result.result_summary_id,
    )
    snapshot = await repository.load_context_snapshot(state.id)
    assert len(snapshot.frontier) == 1
    parent = snapshot.frontier[0]
    assert parent.level == 1
    assert parent.mode is HistorySummaryMode.MODEL_SUMMARY
    assert tuple(member.source_summary_id for member in parent.members) == tuple(
        item.id for item in children
    )
    reloaded = await repository.load_source_summaries(tuple(item.id for item in children))
    assert all(item.status is HistorySummaryStatus.ROLLED_UP for item in reloaded)
    assert "month" not in parent.rendered_text.lower()
    assert "year" not in parent.rendered_text.lower()


@pytest.mark.asyncio
async def test_eight_l1_summaries_roll_into_one_l2(database: Database) -> None:
    executor = _SummarizerExecutor()
    service, repository = _service(database, models=executor)
    event_ids = await _seed_events(database, 16)
    state = await repository.get_or_create_state(_PRIVATE)
    l1_ids: list[int] = []
    for index in range(8):
        pair = event_ids[index * 2 : index * 2 + 2]
        first = await _commit_model_l0(
            repository,
            state_id=state.id,
            event_ids=(pair[0],),
            fingerprint=f"l0-{index}-a",
        )
        second = await _commit_model_l0(
            repository,
            state_id=state.id,
            event_ids=(pair[1],),
            fingerprint=f"l0-{index}-b",
        )
        parent = await repository.commit_parent_summary_and_retire_children(
            state_id=state.id,
            child_ids=(first.id, second.id),
            fingerprint=f"l1-{index}",
            summarizer_version="conversation-rollup-v1",
            rendered_text="l1-child",
            structured_payload_json="{}",
            start_occurred_at=_NOW,
            end_occurred_at=_NOW,
            source_character_count=40,
        )
        l1_ids.append(parent.id)
    await service.consider_parent_rollup(state.id)
    job = await repository.claim_next_job(lease_owner="p", lease_seconds=30)
    assert job is not None
    assert job.source_level == 1
    result = await service.process(job)
    await repository.complete_job(
        job.id,
        lease_owner="p",
        outcome=result.outcome,
        result_summary_id=result.result_summary_id,
    )
    snapshot = await repository.load_context_snapshot(state.id)
    assert len(snapshot.frontier) == 1
    assert snapshot.frontier[0].level == 2
    assert tuple(member.source_summary_id for member in snapshot.frontier[0].members) == tuple(
        l1_ids
    )


@pytest.mark.asyncio
async def test_character_threshold_triggers_parent_before_fan_in(database: Database) -> None:
    executor = _SummarizerExecutor()
    service, repository = _service(database, models=executor)
    event_ids = await _seed_events(database, 2)
    state = await repository.get_or_create_state(_PRIVATE)
    await _commit_model_l0(
        repository,
        state_id=state.id,
        event_ids=(event_ids[0],),
        fingerprint="fat-a",
        rendered_text="a" * 2400,
    )
    await _commit_model_l0(
        repository,
        state_id=state.id,
        event_ids=(event_ids[1],),
        fingerprint="fat-b",
        rendered_text="b" * 2400,
    )
    await service.consider_parent_rollup(state.id)
    job = await repository.claim_next_job(lease_owner="p", lease_seconds=30)
    assert job is not None
    result = await service.process(job)
    assert result.result_summary_id is not None
    snapshot = await repository.load_context_snapshot(state.id)
    assert snapshot.frontier[0].level == 1


@pytest.mark.asyncio
async def test_child_gap_and_level_mismatch_leave_children_active(database: Database) -> None:
    _service_obj, repository = _service(database)
    event_ids = await _seed_events(database, 6)
    state = await repository.get_or_create_state(_PRIVATE)
    first = await _commit_model_l0(
        repository, state_id=state.id, event_ids=event_ids[:2], fingerprint="a"
    )
    second = await _commit_model_l0(
        repository, state_id=state.id, event_ids=event_ids[2:4], fingerprint="b"
    )
    third = await _commit_model_l0(
        repository, state_id=state.id, event_ids=event_ids[4:], fingerprint="c"
    )
    with pytest.raises(FrontierInvariantError, match="contiguous"):
        await repository.commit_parent_summary_and_retire_children(
            state_id=state.id,
            child_ids=(first.id, third.id),
            fingerprint="gap",
            summarizer_version="conversation-rollup-v1",
            rendered_text="gap",
            structured_payload_json="{}",
            start_occurred_at=_NOW,
            end_occurred_at=_NOW,
            source_character_count=40,
        )
    parent = await repository.commit_parent_summary_and_retire_children(
        state_id=state.id,
        child_ids=(first.id, second.id, third.id),
        fingerprint="ok",
        summarizer_version="conversation-rollup-v1",
        rendered_text="ok",
        structured_payload_json="{}",
        start_occurred_at=_NOW,
        end_occurred_at=_NOW,
        source_character_count=40,
    )
    extra = await _seed_events(database, 1, start=7)
    extra_l0 = await _commit_model_l0(
        repository,
        state_id=state.id,
        event_ids=extra,
        fingerprint="extra",
    )
    with pytest.raises(FrontierInvariantError, match="one level"):
        await repository.commit_parent_summary_and_retire_children(
            state_id=state.id,
            child_ids=(parent.id, extra_l0.id),
            fingerprint="levels",
            summarizer_version="conversation-rollup-v1",
            rendered_text="levels",
            structured_payload_json="{}",
            start_occurred_at=_NOW,
            end_occurred_at=_NOW,
            source_character_count=10,
        )
    frontier = await repository.list_active_frontier(state.id)
    assert {item.id for item in frontier} == {parent.id, extra_l0.id}


@pytest.mark.asyncio
async def test_parent_failure_keeps_children_active(database: Database) -> None:
    service, repository = _service(database, models=_BoomExecutor())
    event_ids = await _seed_events(database, 8)
    state = await repository.get_or_create_state(_PRIVATE)
    child_ids = []
    for index, event_id in enumerate(event_ids, start=1):
        child = await _commit_model_l0(
            repository,
            state_id=state.id,
            event_ids=(event_id,),
            fingerprint=f"l0-{index}",
        )
        child_ids.append(child.id)
    await service.consider_parent_rollup(state.id)
    job = await repository.claim_next_job(lease_owner="p", lease_seconds=30)
    assert job is not None
    with pytest.raises(RuntimeError, match="parent write failed"):
        await service.process(job)
    snapshot = await repository.load_context_snapshot(state.id)
    assert [item.id for item in snapshot.frontier] == child_ids
    assert all(item.status is HistorySummaryStatus.ACTIVE for item in snapshot.frontier)


@pytest.mark.asyncio
async def test_concurrent_parent_jobs_only_one_commits(database: Database) -> None:
    executor = _SummarizerExecutor()
    service, repository = _service(database, models=executor)
    event_ids = await _seed_events(database, 8)
    state = await repository.get_or_create_state(_PRIVATE)
    for index, event_id in enumerate(event_ids, start=1):
        await _commit_model_l0(
            repository,
            state_id=state.id,
            event_ids=(event_id,),
            fingerprint=f"l0-{index}",
        )
    await service.consider_parent_rollup(state.id)
    first = await repository.get_open_job(state.id, job_kind=HistoryJobKind.SUMMARY_ROLLUP)
    assert first is not None
    second = await repository.enqueue_job(
        state_id=state.id,
        job_kind=HistoryJobKind.SUMMARY_ROLLUP,
        source_level=first.source_level,
        source_start_id=first.source_start_id,
        source_end_id=first.source_end_id,
        source_fingerprint=first.source_fingerprint,
        summarizer_version="other-v1",
    )
    outcomes = await asyncio.gather(
        service.process(first),
        service.process(second),
        return_exceptions=True,
    )
    summaries = [
        item
        for item in outcomes
        if not isinstance(item, BaseException) and item.outcome is HistoryJobOutcome.SUMMARY
    ]
    assert len(summaries) == 1
    snapshot = await repository.load_context_snapshot(state.id)
    assert len(snapshot.frontier) == 1
    assert snapshot.frontier[0].level == 1


@pytest.mark.asyncio
async def test_parent_success_enqueues_next_level_job(database: Database) -> None:
    executor = _SummarizerExecutor()
    service, repository = _service(
        database,
        models=executor,
        conversation_history_rollup_fan_in=2,
    )
    event_ids = await _seed_events(database, 4)
    state = await repository.get_or_create_state(_PRIVATE)
    for index, event_id in enumerate(event_ids, start=1):
        await _commit_model_l0(
            repository,
            state_id=state.id,
            event_ids=(event_id,),
            fingerprint=f"l0-{index}",
        )
    await service.consider_parent_rollup(state.id)
    first = await repository.claim_next_job(lease_owner="p1", lease_seconds=30)
    assert first is not None
    first_result = await service.process(first)
    await repository.complete_job(
        first.id,
        lease_owner="p1",
        outcome=first_result.outcome,
        result_summary_id=first_result.result_summary_id,
    )
    second = await repository.claim_next_job(lease_owner="p2", lease_seconds=30)
    assert second is not None
    second_result = await service.process(second)
    await repository.complete_job(
        second.id,
        lease_owner="p2",
        outcome=second_result.outcome,
        result_summary_id=second_result.result_summary_id,
    )
    third = await repository.claim_next_job(lease_owner="p3", lease_seconds=30)
    assert third is not None
    assert third.job_kind is HistoryJobKind.SUMMARY_ROLLUP
    assert third.source_level == 1
    third_result = await service.process(third)
    snapshot = await repository.load_context_snapshot(state.id)
    assert len(snapshot.frontier) == 1
    assert snapshot.frontier[0].level == 2
    assert third_result.result_summary_id == snapshot.frontier[0].id
