"""Adversarial coverage for the 3.7 single-checkpoint rollup contract."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from qq_ai_bot.conversation.rollup.db_models import ConversationRollupJobModel
from qq_ai_bot.conversation.rollup.errors import (
    RollupLeaseLostError,
    RollupSourceChangedError,
)
from qq_ai_bot.conversation.rollup.models import RollupKind, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.renderer import rollup_source_projection
from qq_ai_bot.conversation.rollup.repository import (
    ConversationRollupRepository,
    ConversationScopeRepository,
)
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.conversation.rollup.worker import ConversationRollupWorker
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


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
