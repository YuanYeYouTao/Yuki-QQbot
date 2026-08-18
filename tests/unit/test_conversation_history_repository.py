"""Conversation history repository frontier and job invariants."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from qq_ai_bot.conversation.history.errors import FrontierInvariantError
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    ConversationHistorySummary,
    HistoryJobKind,
    HistoryJobOutcome,
    HistoryJobStatus,
    HistorySummaryMode,
)
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository

_NOW = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)


def _identity(
    *,
    bot: str = "bot-1",
    peer: str = "1001",
    group_id: str | None = None,
    reset_at: datetime | None = None,
) -> ConversationHistoryIdentity:
    if group_id is None:
        return ConversationHistoryIdentity(
            bot_user_id=bot,
            scope_type=ScopeType.PRIVATE,
            private_peer_user_id=peer,
            reset_at=reset_at,
        )
    return ConversationHistoryIdentity(
        bot_user_id=bot,
        scope_type=ScopeType.GROUP,
        group_id=group_id,
        reset_at=reset_at,
    )


async def _seed_events(
    database: Database,
    identity: ConversationHistoryIdentity,
    count: int,
    *,
    start: int = 1,
) -> tuple[int, ...]:
    ledger = EventLedgerRepository(database)
    ids: list[int] = []
    for index in range(start, start + count):
        inbound = InboundMessage(
            message_id=(
                f"m-{identity.bot_user_id}-"
                f"{identity.group_id or identity.private_peer_user_id}-{index}"
            ),
            event_type="message",
            scope_type=identity.scope_type,
            sender=SenderIdentity(user_id=identity.private_peer_user_id or "1001"),
            text=f"line-{index}",
            bot_user_id=identity.bot_user_id,
            group_id=identity.group_id,
            received_at=_NOW + timedelta(seconds=index),
        )
        record, _created = await ledger.append_inbound(inbound, bot_user_id=identity.bot_user_id)
        ids.append(record.id)
    return tuple(ids)


async def _commit_l0(
    repository: ConversationHistoryRepository,
    *,
    state_id: int,
    event_ids: tuple[int, ...],
    fingerprint: str,
    occurred_at: datetime = _NOW,
) -> ConversationHistorySummary:
    return await repository.commit_l0_summary(
        state_id=state_id,
        event_ids=event_ids,
        fingerprint=fingerprint,
        mode=HistorySummaryMode.EXTRACTIVE,
        summarizer_version="extractive-v1",
        rendered_text="extractive",
        structured_payload_json="{}",
        start_occurred_at=occurred_at,
        end_occurred_at=occurred_at,
        source_character_count=20,
    )


@pytest.mark.asyncio
async def test_l0_members_keep_event_order_and_fingerprint_is_idempotent(
    database: Database,
) -> None:
    repository = ConversationHistoryRepository(database)
    identity = _identity()
    event_ids = await _seed_events(database, identity, 4)
    state = await repository.get_or_create_state(identity)
    first = await _commit_l0(
        repository, state_id=state.id, event_ids=event_ids, fingerprint="fp-l0"
    )
    second = await _commit_l0(
        repository, state_id=state.id, event_ids=event_ids, fingerprint="fp-l0"
    )
    assert first.id == second.id
    assert tuple(member.source_event_id for member in first.members) == event_ids
    await repository.validate_frontier(state.id)


@pytest.mark.asyncio
async def test_parent_covers_contiguous_children_and_rejects_gaps_or_levels(
    database: Database,
) -> None:
    repository = ConversationHistoryRepository(database)
    identity = _identity()
    event_ids = await _seed_events(database, identity, 6)
    state = await repository.get_or_create_state(identity)
    first = await _commit_l0(
        repository,
        state_id=state.id,
        event_ids=event_ids[:2],
        fingerprint="a",
    )
    second = await _commit_l0(
        repository,
        state_id=state.id,
        event_ids=event_ids[2:4],
        fingerprint="b",
    )
    third = await _commit_l0(
        repository,
        state_id=state.id,
        event_ids=event_ids[4:],
        fingerprint="c",
    )
    with pytest.raises(FrontierInvariantError, match="contiguous"):
        await repository.commit_parent_summary_and_retire_children(
            state_id=state.id,
            child_ids=(first.id, third.id),
            fingerprint="skip",
            summarizer_version="flash-v1",
            rendered_text="parent",
            structured_payload_json="{}",
            start_occurred_at=_NOW,
            end_occurred_at=_NOW,
            source_character_count=40,
        )
    frontier = await repository.list_active_frontier(state.id)
    assert {item.id for item in frontier} == {first.id, second.id, third.id}
    parent = await repository.commit_parent_summary_and_retire_children(
        state_id=state.id,
        child_ids=(first.id, second.id, third.id),
        fingerprint="parent",
        summarizer_version="flash-v1",
        rendered_text="parent",
        structured_payload_json="{}",
        start_occurred_at=_NOW,
        end_occurred_at=_NOW,
        source_character_count=40,
    )
    assert parent.start_event_id == first.start_event_id
    assert parent.end_event_id == third.end_event_id
    assert tuple(member.source_summary_id for member in parent.members) == (
        first.id,
        second.id,
        third.id,
    )
    frontier = await repository.list_active_frontier(state.id)
    assert [item.id for item in frontier] == [parent.id]
    reloaded = await repository.load_source_summaries((first.id, second.id, third.id))
    assert all(item.status.value == "rolled_up" for item in reloaded)
    extra = await _seed_events(database, identity, 2, start=10)
    extra_l0 = await _commit_l0(
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
            summarizer_version="flash-v1",
            rendered_text="levels",
            structured_payload_json="{}",
            start_occurred_at=_NOW,
            end_occurred_at=_NOW,
            source_character_count=10,
        )
    other = _identity(peer="2002")
    other_events = await _seed_events(database, other, 2)
    other_state = await repository.get_or_create_state(other)
    outsider = await _commit_l0(
        repository,
        state_id=other_state.id,
        event_ids=other_events,
        fingerprint="other",
    )
    with pytest.raises(FrontierInvariantError, match="one state"):
        await repository.commit_parent_summary_and_retire_children(
            state_id=state.id,
            child_ids=(parent.id, outsider.id),
            fingerprint="mixed",
            summarizer_version="flash-v1",
            rendered_text="mixed",
            structured_payload_json="{}",
            start_occurred_at=_NOW,
            end_occurred_at=_NOW,
            source_character_count=10,
        )


@pytest.mark.asyncio
async def test_jobs_are_idempotent_and_only_one_worker_claims(
    database: Database,
) -> None:
    repository = ConversationHistoryRepository(database)
    identity = _identity()
    state = await repository.get_or_create_state(identity)
    first = await repository.enqueue_job(
        state_id=state.id,
        job_kind=HistoryJobKind.RAW_RANGE,
        source_level=0,
        source_start_id=1,
        source_end_id=8,
        source_fingerprint="job-fp",
        summarizer_version="flash-v1",
    )
    second = await repository.enqueue_job(
        state_id=state.id,
        job_kind=HistoryJobKind.RAW_RANGE,
        source_level=0,
        source_start_id=1,
        source_end_id=8,
        source_fingerprint="job-fp",
        summarizer_version="flash-v1",
    )
    assert first.id == second.id
    claimed = await asyncio.gather(
        repository.claim_next_job(lease_owner="w1", lease_seconds=180),
        repository.claim_next_job(lease_owner="w2", lease_seconds=180),
    )
    winners = [job for job in claimed if job is not None]
    assert len(winners) == 1
    assert winners[0].status is HistoryJobStatus.PROCESSING
    await repository.complete_job(
        winners[0].id,
        lease_owner=winners[0].lease_owner or "w1",
        outcome=HistoryJobOutcome.NO_CHANGE,
        result_summary_id=None,
    )


@pytest.mark.asyncio
async def test_reset_and_bot_scope_isolation(database: Database) -> None:
    repository = ConversationHistoryRepository(database)
    identity = _identity()
    reset_identity = _identity(reset_at=_NOW + timedelta(hours=1))
    other_bot = _identity(bot="bot-2")
    group = _identity(group_id="2001")
    private_events = await _seed_events(database, identity, 2)
    group_events = await _seed_events(database, group, 2)
    other_events = await _seed_events(database, other_bot, 2)
    private_state = await repository.get_or_create_state(identity)
    reset_state = await repository.get_or_create_state(reset_identity)
    group_state = await repository.get_or_create_state(group)
    bot_state = await repository.get_or_create_state(other_bot)
    assert len({private_state.id, reset_state.id, group_state.id, bot_state.id}) == 4
    await _commit_l0(
        repository,
        state_id=private_state.id,
        event_ids=private_events,
        fingerprint="private",
    )
    await _commit_l0(
        repository,
        state_id=group_state.id,
        event_ids=group_events,
        fingerprint="group",
    )
    await _commit_l0(
        repository,
        state_id=bot_state.id,
        event_ids=other_events,
        fingerprint="bot",
    )
    private_frontier = await repository.list_active_frontier(private_state.id)
    group_frontier = await repository.list_active_frontier(group_state.id)
    assert private_frontier[0].source_fingerprint == "private"
    assert group_frontier[0].source_fingerprint == "group"
    snapshot = await repository.load_context_snapshot(private_state.id)
    assert snapshot.coverage_end_event_id == private_events[-1]
    await repository.validate_frontier(private_state.id)
    await repository.validate_frontier(reset_state.id)
