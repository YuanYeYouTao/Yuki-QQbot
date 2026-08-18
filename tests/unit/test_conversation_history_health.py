"""Frontier integrity health checks for conversation history rollup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from tests.conftest import make_settings

from qq_ai_bot.conversation.history.db_models import (
    ConversationHistoryRollupJobModel,
    ConversationHistorySummaryMemberModel,
    ConversationHistorySummaryModel,
)
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    HistoryJobKind,
    HistoryJobStatus,
    HistorySummaryMode,
    HistorySummaryStatus,
)
from qq_ai_bot.conversation.history.operations import ConversationHistoryOperations
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository

_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
_BOT = "bot-1"
_PEER = "1001"
_OTHER = "2002"
_IDENTITY = ConversationHistoryIdentity(
    bot_user_id=_BOT,
    scope_type=ScopeType.PRIVATE,
    private_peer_user_id=_PEER,
)


def _ops(database: Database) -> ConversationHistoryOperations:
    settings = make_settings(database.url, conversation_history_rollup_enabled=True)
    return ConversationHistoryOperations(
        settings=settings,
        repository=ConversationHistoryRepository(database),
        ledger=EventLedgerRepository(database),
    )


async def _seed_events(
    ledger: EventLedgerRepository,
    count: int,
    *,
    peer: str = _PEER,
    start: int = 1,
) -> tuple[int, ...]:
    ids: list[int] = []
    for index in range(start, start + count):
        inbound = InboundMessage(
            message_id=f"m-{peer}-{index}",
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id=peer),
            text=f"line-{index} " + ("内容" * 8),
            bot_user_id=_BOT,
            received_at=_NOW + timedelta(seconds=index),
        )
        record, _created = await ledger.append_inbound(inbound, bot_user_id=_BOT)
        ids.append(record.id)
    return tuple(ids)


async def _cover(
    repository: ConversationHistoryRepository,
    event_ids: tuple[int, ...],
    fingerprint: str,
) -> int:
    state = await repository.get_or_create_state(_IDENTITY)
    summary = await repository.commit_l0_summary(
        state_id=state.id,
        event_ids=event_ids,
        fingerprint=fingerprint,
        mode=HistorySummaryMode.EXTRACTIVE,
        summarizer_version="extractive-v1",
        rendered_text="compressed",
        structured_payload_json="{}",
        start_occurred_at=_NOW,
        end_occurred_at=_NOW,
        source_character_count=40,
    )
    return summary.id


def _kinds(payload: dict[str, object]) -> set[str]:
    findings = payload["findings"]
    assert isinstance(findings, list)
    return {str(item["kind"]) for item in findings}


def _member_query(summary_id: int, event_id: int):
    return select(ConversationHistorySummaryMemberModel).where(
        ConversationHistorySummaryMemberModel.summary_id == summary_id,
        ConversationHistorySummaryMemberModel.source_event_id == event_id,
    )


@pytest.mark.asyncio
async def test_health_reports_frontier_gap(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 8)
    await _cover(repository, ids[:4], "fp-a")
    second = await _cover(repository, ids[4:], "fp-b")
    async with database.sessions() as session, session.begin():
        row = await session.get(ConversationHistorySummaryModel, second)
        assert row is not None
        row.start_event_id = ids[5]
        member = await session.scalar(_member_query(second, ids[4]))
        if member is not None:
            await session.delete(member)
    payload = await _ops(database).health()
    assert "frontier_gap" in _kinds(payload)
    assert payload["ok"] is False


@pytest.mark.asyncio
async def test_health_reports_overlap(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 8)
    await _cover(repository, ids[:4], "fp-a")
    second = await _cover(repository, ids[4:], "fp-b")
    async with database.sessions() as session, session.begin():
        row = await session.get(ConversationHistorySummaryModel, second)
        assert row is not None
        row.start_event_id = ids[3]
    payload = await _ops(database).health()
    assert "overlap" in _kinds(payload)


@pytest.mark.asyncio
async def test_health_reports_orphan_member(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 4)
    other = await _seed_events(ledger, 2, peer=_OTHER, start=20)
    summary_id = await _cover(repository, ids[:3], "fp-a")
    async with database.sessions() as session, session.begin():
        member = await session.scalar(_member_query(summary_id, ids[0]))
        assert member is not None
        member.source_event_id = other[0]
    payload = await _ops(database).health()
    assert "orphan_member" in _kinds(payload)
    for item in payload["findings"]:
        assert "line-" not in str(item)
        assert "内容" not in str(item)


@pytest.mark.asyncio
async def test_health_reports_bad_replacement(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 8)
    child_id = await _cover(repository, ids[:4], "fp-a")
    parent_id = await _cover(repository, ids[4:], "fp-b")
    async with database.sessions() as session, session.begin():
        child = await session.get(ConversationHistorySummaryModel, child_id)
        assert child is not None
        child.status = HistorySummaryStatus.ROLLED_UP.value
        child.replaced_by_summary_id = parent_id
    payload = await _ops(database).health()
    assert "bad_replacement" in _kinds(payload)


@pytest.mark.asyncio
async def test_health_reports_stale_lease(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    await _seed_events(ledger, 3)
    state = await repository.get_or_create_state(_IDENTITY)
    job = await repository.enqueue_job(
        state_id=state.id,
        job_kind=HistoryJobKind.RAW_RANGE,
        source_level=0,
        source_start_id=1,
        source_end_id=2,
        source_fingerprint="stale",
        summarizer_version="v1",
    )
    async with database.sessions() as session, session.begin():
        row = await session.get(ConversationHistoryRollupJobModel, job.id)
        assert row is not None
        row.status = HistoryJobStatus.PROCESSING.value
        row.lease_owner = "worker-1"
        row.lease_until = datetime.now(UTC) - timedelta(hours=1)
    payload = await _ops(database).health()
    assert "stale_lease" in _kinds(payload)


@pytest.mark.asyncio
async def test_worker_does_not_claim_rebuild_jobs(database: Database) -> None:
    repository = ConversationHistoryRepository(database)
    state = await repository.get_or_create_state(_IDENTITY)
    pending = await repository.enqueue_job(
        state_id=state.id,
        job_kind=HistoryJobKind.REBUILD,
        source_level=0,
        source_start_id=0,
        source_end_id=0,
        source_fingerprint="rebuild-pending",
        summarizer_version="ops-v1",
    )
    claimed = await repository.claim_next_job(lease_owner="worker-1", lease_seconds=180)
    assert claimed is None
    jobs = await repository.list_jobs(state_id=state.id)
    assert any(item.id == pending.id and item.status is HistoryJobStatus.PENDING for item in jobs)
