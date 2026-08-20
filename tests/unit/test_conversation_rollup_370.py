"""Adversarial coverage for the 3.7 single-checkpoint rollup contract."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.conftest import make_settings

from qq_ai_bot.conversation.rollup.db_models import ConversationRollupJobModel
from qq_ai_bot.conversation.rollup.errors import (
    RollupLeaseLostError,
    RollupSourceChangedError,
)
from qq_ai_bot.conversation.rollup.models import RollupKind, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.prompt_accounting import prompt_accounting_characters
from qq_ai_bot.conversation.rollup.renderer import (
    projection_characters,
    rollup_source_projection,
)
from qq_ai_bot.conversation.rollup.repository import (
    ConversationRollupRepository,
    ConversationScopeRepository,
    eligible_prefix,
    protected_tail_start,
    recount_scope_uncovered,
)
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.conversation.rollup.worker import ConversationRollupWorker
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.services.context_assembler import ContextAssembler, _HistoryPromptWindow


def test_rollup_source_projection_renders_stored_utc_in_default_timezone() -> None:
    event = EventRecord(
        id=32865,
        bot_user_id="380726517",
        platform_message_id="nailong-can",
        scope_type=ScopeType.GROUP,
        sender_user_id="3135003586",
        sender_group_card="查无此人",
        direction="inbound",
        content="这是什么",
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 8, 20, 11, 21, 42, tzinfo=UTC),
        group_id="1049765710",
    )

    assert rollup_source_projection(event) == ("[2026-08-20T19:21:42+08:00] 查无此人: 这是什么")


def _reply_mention_events() -> tuple[EventRecord, ...]:
    occurred = datetime(2026, 8, 20, 11, 21, 42, tzinfo=UTC)
    parent = EventRecord(
        id=1,
        bot_user_id="380726517",
        platform_message_id="msg-parent",
        scope_type=ScopeType.GROUP,
        sender_user_id="10001",
        sender_group_card="Alice",
        direction="inbound",
        content="hello there",
        visual_summary="",
        segments=(),
        occurred_at=occurred,
        group_id="1049765710",
    )
    reply = EventRecord(
        id=2,
        bot_user_id="380726517",
        platform_message_id="msg-reply",
        scope_type=ScopeType.GROUP,
        sender_user_id="10002",
        sender_group_card="Bob",
        direction="inbound",
        content="got it",
        visual_summary="",
        segments=(),
        occurred_at=occurred + timedelta(seconds=1),
        group_id="1049765710",
        reply_to_message_id="msg-parent",
        mentioned_user_ids=("380726517",),
        reply_sender_user_id="10001",
    )
    return (parent, reply)


def test_prompt_accounting_matches_assembler_and_outweighs_projection() -> None:
    events = _reply_mention_events()
    prompt_chars = prompt_accounting_characters(events)
    projection_chars = sum(projection_characters(event) for event in events)
    assert projection_chars < prompt_chars

    settings = make_settings("sqlite+aiosqlite:///:memory:")
    assembler = ContextAssembler(
        settings=settings,
        ledger=MagicMock(),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=MagicMock(),
        rollup_service=MagicMock(),
    )
    dummy_current = EventRecord(
        id=99,
        bot_user_id="380726517",
        platform_message_id="msg-current",
        scope_type=ScopeType.GROUP,
        sender_user_id="10003",
        direction="inbound",
        content="now",
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 8, 20, 11, 22, tzinfo=UTC),
        group_id="1049765710",
    )
    inbound = InboundMessage(
        message_id="msg-current",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="10003", group_card="Carol"),
        text="now",
        bot_user_id="380726517",
        group_id="1049765710",
    )
    view = assembler._uncovered_prompt_view(
        events,
        inbound=inbound,
        content="now",
        current_event=dummy_current,
    )
    assert view is not None
    assert view.rendered_characters == prompt_chars


async def test_recount_writes_grouped_prompt_characters(database: Database) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.group("bot-prompt", "group-prompt")
    first = await uow.append(
        scope=scope,
        platform_message_id="msg-parent",
        sender_user_id="10001",
        sender_group_card="Alice",
        direction="inbound",
        content="hello there",
        occurred_at=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
    )
    await uow.append(
        scope=scope,
        platform_message_id="msg-reply",
        sender_user_id="10002",
        sender_group_card="Bob",
        direction="inbound",
        content="got it",
        occurred_at=datetime(2026, 8, 20, 0, 1, tzinfo=UTC),
        reply_to_message_id="msg-parent",
        segments=(
            {
                "type": "yuki_context",
                "data": {
                    "mentioned_user_ids": [scope.bot_user_id],
                    "reply_sender_user_id": "10001",
                },
            },
        ),
    )
    snapshot = await repository.load_prompt_snapshot(scope)
    expected = prompt_accounting_characters(
        snapshot.raw_events,
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    async with database.immediate_session() as session:
        from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel

        row = await session.get(ConversationScopeModel, first.scope.id)
        assert row is not None
        row.uncovered_character_count = sum(
            projection_characters(event) for event in snapshot.raw_events
        )
        recounted = await recount_scope_uncovered(session, row, policy)
    assert recounted == (2, expected)
    state, _rollup, _job = await repository.status(scope)
    assert state is not None
    assert state.uncovered_character_count == expected


def _policy(*, batch_max_events: int = 100) -> RollupPolicyConfig:
    return RollupPolicyConfig(
        raw_tail_events=2,
        raw_tail_characters=100_000,
        trigger_events=2,
        trigger_characters=100_000,
        stop_events=0,
        stop_characters=0,
        batch_max_events=batch_max_events,
        batch_max_characters=100_000,
        summary_max_characters=2_000,
    )


async def _append(
    uow: ScopedEventLedgerUnitOfWork,
    scope: ConversationScope,
    count: int,
    *,
    start: int = 1,
    actor_prefix: str = "member",
) -> None:
    for index in range(start, start + count):
        await uow.append(
            scope=scope,
            platform_message_id=f"message-{scope.bot_user_id}-{index}",
            sender_user_id=f"{actor_prefix}-{index % 2}",
            direction="inbound",
            content=f"event-{index}",
            occurred_at=datetime(2026, 8, 20, 0, index % 60, tzinfo=UTC),
        )


async def test_scoped_append_signals_one_group_job_and_isolates_other_bot(
    database: Database,
) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.group("bot-a", "group-1")
    other = ConversationScope.group("bot-b", "group-1")

    await _append(uow, scope, 4)
    await _append(uow, other, 1)

    state, rollup, job = await repository.status(scope)
    other_snapshot = await repository.load_prompt_snapshot(other)
    assert state is not None
    assert state.uncovered_event_count == 4
    assert rollup is None
    assert job is not None and job["signal_revision"] == 1
    assert [event.content for event in other_snapshot.raw_events] == ["event-1"]


async def test_rollup_health_is_content_free_and_reports_global_lag(database: Database) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy, metrics=uow.metrics)
    await _append(uow, ConversationScope.group("bot-a", "group-1"), 4)
    service = ConversationRollupService(
        models=None,
        config=policy,
        timeout_seconds=1,
        metrics=uow.metrics,
    )
    worker = ConversationRollupWorker(
        repository=repository,
        service=service,
        enabled=True,
        concurrency=1,
        poll_seconds=1,
        lease_seconds=30,
        heartbeat_seconds=5,
        retry_max_seconds=60,
        max_batches_per_claim=1,
        metrics=uow.metrics,
    )

    health = await worker.health()

    assert health["enabled"] is True
    assert health["running"] is False
    assert health["scope_count"] == 1
    assert health["max_lag_events"] == 4
    assert health["max_lag_characters"] > 0
    assert "group-1" not in repr(health)
    assert "bot-a" not in repr(health)


async def test_extractive_commit_forms_one_continuous_checkpoint_and_raw_tail(
    database: Database,
) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    scope = ConversationScope.private("bot-a", "peer-1")
    await _append(uow, scope, 4)
    before = await repository.load_prompt_snapshot(scope)

    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    summary, kind = await service.summarize_candidate(candidate)
    assert kind is RollupKind.EXTRACTIVE
    committed = await repository.commit_candidate(
        claim,
        candidate,
        summary_text=summary,
        summary_kind=kind,
    )
    snapshot = await repository.load_prompt_snapshot(scope)

    assert committed.rollup.covered_through_event_id == candidate.events[-1].id
    assert snapshot.effective_coverage == committed.rollup.covered_through_event_id
    assert [event.id for event in snapshot.raw_events] == [
        event.id for event in before.raw_events[len(candidate.events) :]
    ]
    assert all(event.id > snapshot.effective_coverage for event in snapshot.raw_events)
    assert len(snapshot.raw_events) == 2
    state, _rollup, job = await repository.status(scope)
    assert state is not None and state.uncovered_event_count == 2
    assert job is None


async def test_visual_projection_change_rejects_locked_candidate(database: Database) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.private("bot-a", "peer-visual")
    await _append(uow, scope, 4)
    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None

    await uow.set_visual_summary(candidate.events[0].id, "a newly available visual description")

    with pytest.raises(RollupSourceChangedError):
        await repository.commit_candidate(
            claim,
            candidate,
            summary_text="stale summary",
            summary_kind=RollupKind.MODEL,
        )


async def test_append_after_candidate_keeps_candidate_valid_and_preserves_signal(
    database: Database,
) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.private("bot-a", "peer-append")
    await _append(uow, scope, 4)
    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None

    await _append(uow, scope, 1, start=5)
    await repository.commit_candidate(
        claim,
        candidate,
        summary_text="valid locked prefix",
        summary_kind=RollupKind.EXTRACTIVE,
    )

    _state, _rollup, job = await repository.status(scope)
    assert job is not None
    assert job["status"] == "pending"
    assert job["signal_revision"] == claim.claimed_signal_revision + 1


async def test_two_database_instances_only_claim_one_job(database: Database) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    scope = ConversationScope.group("bot-a", "group-concurrent")
    await _append(uow, scope, 4)
    second_database = Database(database.url)
    try:
        first = ConversationRollupRepository(database, policy)
        second = ConversationRollupRepository(second_database, policy)
        claims = await asyncio.gather(
            first.claim_next_job(lease_owner="first", lease_seconds=30),
            second.claim_next_job(lease_owner="second", lease_seconds=30),
        )
    finally:
        await second_database.close()

    assert sum(claim is not None for claim in claims) == 1


async def test_expired_lease_same_owner_gets_new_token_and_old_token_is_rejected(
    database: Database,
) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    await _append(uow, ConversationScope.private("bot-a", "peer-lease"), 4)
    old = await repository.claim_next_job(lease_owner="same-owner", lease_seconds=30)
    assert old is not None
    async with database.immediate_session() as session:
        job = await session.get(ConversationRollupJobModel, old.scope_id)
        assert job is not None
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)

    new = await repository.claim_next_job(lease_owner="same-owner", lease_seconds=30)
    assert new is not None and new.lease_token != old.lease_token
    with pytest.raises(RollupLeaseLostError):
        await repository.heartbeat(old, lease_seconds=30)


async def test_commit_repairs_drifted_uncovered_counters_once(database: Database) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scopes = ConversationScopeRepository(database)
    scope = ConversationScope.private("bot-a", "peer-recount")
    await _append(uow, scope, 4)
    state = await scopes.get(scope)
    assert state is not None
    async with database.immediate_session() as session:
        from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel

        row = await session.get(ConversationScopeModel, state.id)
        assert row is not None
        row.uncovered_event_count += 7
        row.uncovered_character_count += 77

    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    await repository.commit_candidate(
        claim,
        candidate,
        summary_text="recounted",
        summary_kind=RollupKind.EXTRACTIVE,
    )

    repaired = await scopes.get(scope)
    assert repaired is not None
    assert repaired.uncovered_event_count == 2


async def test_successful_batch_resets_persisted_failure_before_next_retry(
    database: Database,
) -> None:
    policy = _policy(batch_max_events=1)
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.private("bot-a", "peer-retry")
    await _append(uow, scope, 6)
    async with database.immediate_session() as session:
        job = await session.scalar(ConversationRollupJobModel.__table__.select().limit(1))
        assert job is not None
    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None
    async with database.immediate_session() as session:
        stored = await session.get(ConversationRollupJobModel, claim.scope_id)
        assert stored is not None
        stored.failure_count = 4
    # Reclaim so the claim reflects the old failure count, then prove a successful
    # retained batch resets the persisted value used by the next infrastructure retry.
    async with database.immediate_session() as session:
        stored = await session.get(ConversationRollupJobModel, claim.scope_id)
        assert stored is not None
        stored.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None and claim.failure_count == 4
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    committed = await repository.commit_candidate(
        claim,
        candidate,
        summary_text="first successful batch",
        summary_kind=RollupKind.EXTRACTIVE,
        retain_lease=True,
    )
    assert committed.claim_retained is True
    await repository.retry_infrastructure(
        claim,
        error_category="database_unavailable",
        retry_max_seconds=960,
    )
    _state, _rollup, job = await repository.status(scope)
    assert job is not None and job["failure_count"] == 1


async def test_foreground_claim_prevents_background_result_overwrite(database: Database) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.private("bot-a", "peer-preempt")
    await _append(uow, scope, 4)
    background = await repository.claim_next_job(lease_owner="background", lease_seconds=30)
    assert background is not None
    stale_candidate = await repository.candidate_for_claim(background)
    assert stale_candidate is not None
    foreground = await repository.claim_scope_for_foreground(
        scope,
        lease_owner="foreground",
        lease_seconds=30,
    )
    assert foreground is not None
    current_candidate = await repository.candidate_for_claim(foreground)
    assert current_candidate is not None
    await repository.commit_candidate(
        foreground,
        current_candidate,
        summary_text="foreground wins",
        summary_kind=RollupKind.EXTRACTIVE,
    )

    with pytest.raises(RollupLeaseLostError):
        await repository.commit_candidate(
            background,
            stale_candidate,
            summary_text="stale background",
            summary_kind=RollupKind.MODEL,
        )


async def test_single_protected_event_never_creates_permanent_job(database: Database) -> None:
    policy = RollupPolicyConfig(
        raw_tail_events=1,
        raw_tail_characters=1,
        trigger_events=2,
        trigger_characters=2,
        stop_events=0,
        stop_characters=0,
        batch_max_events=10,
        batch_max_characters=10,
        summary_max_characters=100,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.private("bot-a", "peer-long")
    await uow.append(
        scope=scope,
        platform_message_id="one-long-message",
        sender_user_id="peer-long",
        direction="inbound",
        content="x" * 10_000,
    )

    _state, _rollup, job = await repository.status(scope)
    assert job is None


def test_prompt_event_caps_use_trigger_and_stop_not_tail_plus_one() -> None:
    assembler = ContextAssembler(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        ledger=MagicMock(),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=MagicMock(),
        rollup_service=MagicMock(),
    )

    assert assembler._prompt_event_admit(event_limit=2048, coverage_end=0) == 2047
    assert assembler._prompt_event_admit(event_limit=2048, coverage_end=100) == 1280
    assert assembler._prompt_event_target(event_limit=2048, coverage_end=100) == 256
    assert assembler._prompt_event_admit(event_limit=1024, coverage_end=100) == 1023


async def test_foreground_does_not_nibble_between_protected_tail_and_trigger(
    database: Database,
) -> None:
    settings = make_settings(
        database.url,
        local_context_event_limit=16,
        conversation_rollup_raw_tail_events=8,
        conversation_rollup_trigger_events=4,
        conversation_rollup_stop_events=2,
        conversation_rollup_raw_tail_characters=100_000,
        conversation_rollup_trigger_characters=100_000,
        conversation_rollup_stop_characters=10_000,
        conversation_rollup_batch_max_events=8,
        conversation_rollup_batch_max_characters=100_000,
        conversation_rollup_summary_max_characters=2_000,
        conversation_rollup_foreground_max_batches=4,
    )
    policy = RollupPolicyConfig(
        raw_tail_events=settings.conversation_rollup_raw_tail_events,
        raw_tail_characters=settings.conversation_rollup_raw_tail_characters,
        trigger_events=settings.conversation_rollup_trigger_events,
        trigger_characters=settings.conversation_rollup_trigger_characters,
        stop_events=settings.conversation_rollup_stop_events,
        stop_characters=settings.conversation_rollup_stop_characters,
        batch_max_events=settings.conversation_rollup_batch_max_events,
        batch_max_characters=settings.conversation_rollup_batch_max_characters,
        summary_max_characters=settings.conversation_rollup_summary_max_characters,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    assembler = ContextAssembler(
        settings=settings,
        ledger=MagicMock(),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=repository,
        rollup_service=service,
    )
    scope = ConversationScope.group("bot-hysteresis", "group-hysteresis")
    await _append(uow, scope, 12)
    claim = await repository.claim_next_job(lease_owner="seed", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    summary, kind = service.extractive(candidate)
    await repository.commit_candidate(claim, candidate, summary_text=summary, summary_kind=kind)
    seeded, seeded_rollup, _job = await repository.status(scope)
    assert seeded is not None and seeded_rollup is not None
    assert seeded.uncovered_event_count == 8
    seeded_revision = seeded_rollup.revision
    seeded_coverage = seeded_rollup.covered_through_event_id

    await _append(uow, scope, 3, start=13)
    dead_zone, dead_rollup, _job = await repository.status(scope)
    assert dead_zone is not None and dead_rollup is not None
    assert dead_zone.uncovered_event_count == 11
    await assembler._ensure_lightweight_backlog(
        scope,
        ConversationTurnSnapshot(
            scope_id=dead_zone.id,
            scope_key=dead_zone.scope.key,
            generation=dead_zone.generation,
            trigger_event_id=dead_zone.last_event_id,
            coordinator_version=1,
        ),
        event_limit=settings.local_context_event_limit,
    )
    after_dead, after_dead_rollup, _job = await repository.status(scope)
    assert after_dead is not None and after_dead_rollup is not None
    assert after_dead.uncovered_event_count == 11
    assert after_dead_rollup.revision == seeded_revision
    assert after_dead_rollup.covered_through_event_id == seeded_coverage

    await _append(uow, scope, 2, start=16)
    over_trigger, _rollup, _job = await repository.status(scope)
    assert over_trigger is not None
    assert over_trigger.uncovered_event_count == 13
    await assembler._ensure_lightweight_backlog(
        scope,
        ConversationTurnSnapshot(
            scope_id=over_trigger.id,
            scope_key=over_trigger.scope.key,
            generation=over_trigger.generation,
            trigger_event_id=over_trigger.last_event_id,
            coordinator_version=1,
        ),
        event_limit=settings.local_context_event_limit,
    )
    compacted, compacted_rollup, _job = await repository.status(scope)
    assert compacted is not None and compacted_rollup is not None
    assert compacted_rollup.revision > seeded_revision
    assert compacted.uncovered_event_count <= (
        settings.conversation_rollup_raw_tail_events + settings.conversation_rollup_stop_events
    )


async def test_lightweight_backlog_triggers_on_prompt_ruler_not_projection(
    database: Database,
) -> None:
    policy = RollupPolicyConfig(
        raw_tail_events=2,
        raw_tail_characters=100_000,
        trigger_events=32,
        trigger_characters=100_000,
        stop_events=0,
        stop_characters=0,
        batch_max_events=8,
        batch_max_characters=100_000,
        summary_max_characters=2_000,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    scope = ConversationScope.group("bot-ruler", "group-ruler")
    first = None
    for index in range(1, 7):
        result = await uow.append(
            scope=scope,
            platform_message_id=f"msg-{index}",
            sender_user_id=f"1000{index % 2}",
            sender_group_card="Alice" if index % 2 else "Bob",
            direction="inbound",
            content="short",
            occurred_at=datetime(2026, 8, 20, 0, index, tzinfo=UTC),
            reply_to_message_id=None if index == 1 else f"msg-{index - 1}",
            segments=(
                (
                    {
                        "type": "yuki_context",
                        "data": {
                            "mentioned_user_ids": [scope.bot_user_id],
                            "reply_sender_user_id": f"1000{(index - 1) % 2}",
                        },
                    },
                )
                if index > 1
                else ()
            ),
        )
        first = first or result
    snapshot = await repository.load_prompt_snapshot(scope)
    projection = sum(projection_characters(event) for event in snapshot.raw_events)
    prompt = prompt_accounting_characters(
        snapshot.raw_events,
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    remaining_prompt = prompt_accounting_characters(
        snapshot.raw_events[-2:],
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    admit = (projection + prompt) // 2
    assert projection < admit <= prompt
    assert remaining_prompt < admit
    raw_tail_characters = 1
    trigger_characters = max(2, admit - raw_tail_characters)
    settings = make_settings(
        database.url,
        local_context_event_limit=64,
        conversation_rollup_raw_tail_events=2,
        conversation_rollup_trigger_events=32,
        conversation_rollup_stop_events=0,
        conversation_rollup_raw_tail_characters=raw_tail_characters,
        conversation_rollup_trigger_characters=trigger_characters,
        conversation_rollup_stop_characters=trigger_characters - 1,
        conversation_rollup_batch_max_events=8,
        conversation_rollup_batch_max_characters=100_000,
        conversation_rollup_summary_max_characters=2_000,
        conversation_rollup_foreground_max_batches=4,
    )
    async with database.immediate_session() as session:
        from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel

        row = await session.get(ConversationScopeModel, first.scope.id)
        assert row is not None
        row.uncovered_character_count = projection
        recounted = await recount_scope_uncovered(session, row, policy)
    assert recounted[1] == prompt
    assembler = ContextAssembler(
        settings=settings,
        ledger=MagicMock(),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=repository,
        rollup_service=service,
    )
    seeded, seeded_rollup, _job = await repository.status(scope)
    assert seeded is not None
    assert seeded.uncovered_character_count == prompt
    assert seeded_rollup is None
    await assembler._ensure_lightweight_backlog(
        scope,
        ConversationTurnSnapshot(
            scope_id=seeded.id,
            scope_key=seeded.scope.key,
            generation=seeded.generation,
            trigger_event_id=seeded.last_event_id,
            coordinator_version=1,
        ),
        event_limit=settings.local_context_event_limit,
    )
    compacted, compacted_rollup, _job = await repository.status(scope)
    assert compacted is not None and compacted_rollup is not None
    assert compacted_rollup.revision >= 1
    assert compacted.uncovered_event_count < 6


def _counted_events(count: int, *, body: str) -> tuple[EventRecord, ...]:
    occurred = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    return tuple(
        EventRecord(
            id=index,
            bot_user_id="bot-floor",
            platform_message_id=f"msg-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id=f"1000{index % 2}",
            sender_group_card="Alice" if index % 2 else "Bob",
            direction="inbound",
            content=body,
            visual_summary="",
            segments=(),
            occurred_at=occurred + timedelta(seconds=index),
            group_id="group-floor",
        )
        for index in range(1, count + 1)
    )


def test_long_messages_raise_character_index_and_keep_eligible_prefix() -> None:
    policy = RollupPolicyConfig(
        raw_tail_events=4,
        raw_tail_characters=200,
        trigger_events=8,
        trigger_characters=100_000,
        stop_events=0,
        stop_characters=0,
        batch_max_events=8,
        batch_max_characters=100_000,
        summary_max_characters=2_000,
    )
    events = _counted_events(6, body="z" * 500)
    count_index = max(0, len(events) - policy.raw_tail_events)
    start = protected_tail_start(events, policy)
    assert start > count_index
    eligible = eligible_prefix(events, policy)
    assert eligible
    assert eligible[-1].id < events[start].id


async def test_event_floor_between_character_target_and_admit_skips_extractive() -> None:
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        local_context_event_limit=2048,
        conversation_rollup_raw_tail_events=256,
        conversation_rollup_trigger_events=1024,
        conversation_rollup_stop_events=0,
        conversation_rollup_raw_tail_characters=20_480,
        conversation_rollup_trigger_characters=81_920,
        conversation_rollup_stop_characters=0,
    )
    events = _counted_events(256, body="y" * 80)
    current = events[-1]
    history = events
    prompt = prompt_accounting_characters(history)
    target = (
        settings.conversation_rollup_raw_tail_characters
        + settings.conversation_rollup_stop_characters
    )
    admit = (
        settings.conversation_rollup_raw_tail_characters
        + settings.conversation_rollup_trigger_characters
    )
    assert target < prompt <= admit
    rollup_service = MagicMock()
    rollup_service.ensure_extractive_coverage = AsyncMock(
        side_effect=AssertionError("admit-window turns must not extractive")
    )
    assembler = ContextAssembler(
        settings=settings,
        ledger=MagicMock(),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=MagicMock(),
        rollup_service=rollup_service,
    )
    inbound = InboundMessage(
        message_id="msg-current",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="10009", group_card="Carol"),
        text="now",
        bot_user_id="bot-floor",
        group_id="group-floor",
    )
    dummy_current = EventRecord(
        id=10_000,
        bot_user_id="bot-floor",
        platform_message_id="msg-current",
        scope_type=ScopeType.GROUP,
        sender_user_id="10009",
        sender_group_card="Carol",
        direction="inbound",
        content="now",
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 8, 20, 1, 0, tzinfo=UTC),
        group_id="group-floor",
    )
    snapshot = _HistoryPromptWindow(
        recent=history,
        rollup_text="seed",
        coverage_end=1,
        revision=1,
        rollup=None,
        rollup_mode="extractive",
    )
    view = assembler._uncovered_prompt_view(
        history,
        inbound=inbound,
        content="now",
        current_event=dummy_current,
    )
    assert view is not None
    assert view.rendered_characters == prompt
    character_target = assembler._prompt_character_target(
        remainder=1_000_000,
        rollup_text="seed",
        coverage_end=1,
    )
    character_admit = assembler._prompt_character_admit(
        remainder=1_000_000,
        rollup_text="seed",
        coverage_end=1,
    )
    assert character_target < view.rendered_characters <= character_admit
    await assembler._ensure_uncovered_fits_budget(
        snapshot=snapshot,
        recent=history,
        inbound=inbound,
        content="now",
        remainder=1_000_000,
        event_limit=settings.local_context_event_limit,
        identity=ConversationScope.group("bot-floor", "group-floor"),
        current_event=dummy_current,
        turn=ConversationTurnSnapshot(
            scope_id=1,
            scope_key="bot:bot-floor:group:group-floor",
            generation=1,
            trigger_event_id=current.id,
            coordinator_version=1,
        ),
    )
    rollup_service.ensure_extractive_coverage.assert_not_called()
