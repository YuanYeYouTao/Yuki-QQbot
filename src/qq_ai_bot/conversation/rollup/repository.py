"""Short SQLite transactions and CAS operations for conversation rollup."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.conversation.rollup.db_models import (
    ConversationRollupJobModel,
    ConversationRollupModel,
    ConversationScopeModel,
)
from qq_ai_bot.conversation.rollup.errors import (
    ConversationCoverageError,
    RollupLeaseLostError,
    RollupSourceChangedError,
)
from qq_ai_bot.conversation.rollup.metrics import ConversationRollupMetrics
from qq_ai_bot.conversation.rollup.models import (
    ConversationPromptSnapshot,
    ConversationRollupState,
    ConversationScopeState,
    RollupCandidate,
    RollupCommitResult,
    RollupJobClaim,
    RollupKind,
    RollupPolicyConfig,
)
from qq_ai_bot.conversation.rollup.renderer import projection_characters, source_fingerprint
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import _event_record
from qq_ai_bot.persistence.repository_records import EventRecord


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_utc_iso(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized is not None else None


def _age_seconds(value: datetime | None, now: datetime) -> int:
    normalized = _as_utc(value)
    return max(0, int((now - normalized).total_seconds())) if normalized is not None else 0


def _scope_conditions(scope: ConversationScope) -> tuple[ColumnElement[bool], ...]:
    if scope.scope_type is ScopeType.GROUP:
        return (
            ChatEventModel.bot_user_id == scope.bot_user_id,
            ChatEventModel.scope_type == ScopeType.GROUP.value,
            ChatEventModel.group_id == scope.group_id,
        )
    return (
        ChatEventModel.bot_user_id == scope.bot_user_id,
        ChatEventModel.scope_type == ScopeType.PRIVATE.value,
        ChatEventModel.private_peer_user_id == scope.private_peer_user_id,
    )


def _scope_from_row(row: ConversationScopeModel) -> ConversationScope:
    if row.scope_type == ScopeType.GROUP.value:
        scope = ConversationScope.group(row.bot_user_id, row.group_id or "")
    else:
        scope = ConversationScope.private(row.bot_user_id, row.private_peer_user_id or "")
    if row.scope_key != scope.key:
        raise ConversationCoverageError("stored scope key does not match its identity")
    return scope


def _scope_state(row: ConversationScopeModel) -> ConversationScopeState:
    return ConversationScopeState(
        id=row.id,
        scope=_scope_from_row(row),
        generation=row.generation,
        starts_after_event_id=row.starts_after_event_id,
        last_event_id=row.last_event_id,
        last_generation_change_event_id=row.last_generation_change_event_id,
        uncovered_event_count=row.uncovered_event_count,
        uncovered_character_count=row.uncovered_character_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _rollup_state(row: ConversationRollupModel) -> ConversationRollupState:
    return ConversationRollupState(
        scope_id=row.scope_id,
        generation=row.generation,
        covered_through_event_id=row.covered_through_event_id,
        summary_text=row.summary_text,
        summary_kind=RollupKind(row.summary_kind),
        source_fingerprint=row.source_fingerprint,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def protected_tail_start(events: tuple[EventRecord, ...], config: RollupPolicyConfig) -> int:
    """Return the first protected event index using the later of both boundaries."""

    if not events:
        return 0
    count_index = max(0, len(events) - config.raw_tail_events)
    characters = 0
    character_index = len(events) - 1
    for index in range(len(events) - 1, -1, -1):
        characters += projection_characters(events[index])
        character_index = index
        if characters >= config.raw_tail_characters:
            break
    return max(count_index, character_index)


def eligible_prefix(
    events: tuple[EventRecord, ...], config: RollupPolicyConfig
) -> tuple[EventRecord, ...]:
    return events[: protected_tail_start(events, config)]


def exceeds_high_watermark(events: tuple[EventRecord, ...], config: RollupPolicyConfig) -> bool:
    characters = sum(projection_characters(event) for event in events)
    return len(events) >= config.trigger_events or characters >= config.trigger_characters


def exceeds_low_watermark(events: tuple[EventRecord, ...], config: RollupPolicyConfig) -> bool:
    if not events:
        return False
    characters = sum(projection_characters(event) for event in events)
    return len(events) > config.stop_events or characters > config.stop_characters


def take_batch(
    events: tuple[EventRecord, ...], config: RollupPolicyConfig
) -> tuple[EventRecord, ...]:
    selected: list[EventRecord] = []
    characters = 0
    for event in events:
        size = projection_characters(event)
        if selected and (
            len(selected) >= config.batch_max_events
            or characters + size > config.batch_max_characters
        ):
            break
        selected.append(event)
        characters += size
    return tuple(selected)


class ConversationScopeRepository:
    """Read scopes and enforce generation fences."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, scope: ConversationScope) -> ConversationScopeState | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(ConversationScopeModel).where(ConversationScopeModel.scope_key == scope.key)
            )
        if row is None:
            return None
        state = _scope_state(row)
        if state.scope != scope:
            raise ConversationCoverageError("scope key collision")
        return state

    async def get_by_id(self, scope_id: int) -> ConversationScopeState | None:
        async with self._database.sessions() as session:
            row = await session.get(ConversationScopeModel, scope_id)
        return _scope_state(row) if row is not None else None

    async def generation_matches(self, scope_id: int, generation: int) -> bool:
        async with self._database.sessions() as session:
            current = await session.scalar(
                select(ConversationScopeModel.generation).where(
                    ConversationScopeModel.id == scope_id
                )
            )
        return current == generation


class ConversationRollupRepository:
    """Single-checkpoint reads, leases, candidate construction, and CAS commits."""

    def __init__(
        self,
        database: Database,
        config: RollupPolicyConfig,
        metrics: ConversationRollupMetrics | None = None,
    ) -> None:
        self._database = database
        self.config = config
        self.metrics = metrics or ConversationRollupMetrics()

    async def health_snapshot(self) -> dict[str, object]:
        """Return content-free, low-cardinality process health for all scopes."""

        now = _utcnow()
        async with self._database.sessions() as session:
            scope_count, max_events, max_characters = (
                await session.execute(
                    select(
                        func.count(ConversationScopeModel.id),
                        func.max(ConversationScopeModel.uncovered_event_count),
                        func.max(ConversationScopeModel.uncovered_character_count),
                    )
                )
            ).one()
            expired_processing = int(
                await session.scalar(
                    select(func.count(ConversationRollupJobModel.scope_id)).where(
                        ConversationRollupJobModel.status == "processing",
                        ConversationRollupJobModel.lease_until <= now,
                    )
                )
                or 0
            )
            oldest_pending = await session.scalar(
                select(func.min(ConversationRollupJobModel.created_at)).where(
                    ConversationRollupJobModel.status == "pending"
                )
            )
            recent_error = await session.scalar(
                select(ConversationRollupJobModel.last_error_category)
                .where(ConversationRollupJobModel.last_error_category.is_not(None))
                .order_by(ConversationRollupJobModel.updated_at.desc())
                .limit(1)
            )
            last_extractive = await session.scalar(
                select(func.max(ConversationRollupModel.updated_at)).where(
                    ConversationRollupModel.summary_kind == RollupKind.EXTRACTIVE.value
                )
            )
        return {
            "scope_count": int(scope_count or 0),
            "expired_processing_leases": expired_processing,
            "oldest_pending_job_age_seconds": _age_seconds(oldest_pending, now),
            "max_lag_events": int(max_events or 0),
            "max_lag_characters": int(max_characters or 0),
            "recent_infrastructure_error_category": recent_error,
            "last_extractive_at": _as_utc_iso(last_extractive),
        }

    async def status(
        self, scope: ConversationScope
    ) -> tuple[
        ConversationScopeState | None, ConversationRollupState | None, dict[str, object] | None
    ]:
        async with self._database.sessions() as session:
            scope_row = await session.scalar(
                select(ConversationScopeModel).where(ConversationScopeModel.scope_key == scope.key)
            )
            if scope_row is None:
                return None, None, None
            state = _scope_state(scope_row)
            if state.scope != scope:
                raise ConversationCoverageError("scope key collision")
            rollup_row = await session.get(ConversationRollupModel, scope_row.id)
            job = await session.get(ConversationRollupJobModel, scope_row.id)
            job_state = (
                {
                    "status": job.status,
                    "signal_revision": job.signal_revision,
                    "failure_count": job.failure_count,
                    "created_at": job.created_at,
                    "last_error_category": job.last_error_category,
                }
                if job is not None
                else None
            )
        return state, _rollup_state(rollup_row) if rollup_row is not None else None, job_state

    async def load_prompt_snapshot(
        self,
        scope: ConversationScope,
        *,
        before_event_id: int | None = None,
    ) -> ConversationPromptSnapshot:
        """Load scope, checkpoint, and the exact continuous raw suffix in one transaction."""

        async with self._database.sessions() as session, session.begin():
            scope_row = await session.scalar(
                select(ConversationScopeModel).where(ConversationScopeModel.scope_key == scope.key)
            )
            if scope_row is None:
                raise ConversationCoverageError("conversation scope does not exist")
            state = _scope_state(scope_row)
            if state.scope != scope:
                raise ConversationCoverageError("scope key collision")
            rollup_row = await session.get(ConversationRollupModel, scope_row.id)
            if rollup_row is not None and rollup_row.generation != scope_row.generation:
                raise ConversationCoverageError("rollup generation mismatch")
            coverage = (
                rollup_row.covered_through_event_id
                if rollup_row is not None
                else scope_row.starts_after_event_id
            )
            if not scope_row.starts_after_event_id <= coverage <= scope_row.last_event_id:
                raise ConversationCoverageError("prompt snapshot coverage is outside scope bounds")
            query = select(ChatEventModel).where(
                *_scope_conditions(scope),
                ChatEventModel.id > coverage,
                ChatEventModel.id <= scope_row.last_event_id,
            )
            if before_event_id is not None:
                query = query.where(ChatEventModel.id < before_event_id)
            rows = tuple((await session.scalars(query.order_by(ChatEventModel.id.asc()))).all())
            events = tuple(_event_record(row) for row in rows)
            tail_end = events[-1].id if events else coverage
        return ConversationPromptSnapshot(
            scope=state,
            rollup=_rollup_state(rollup_row) if rollup_row is not None else None,
            raw_events=events,
            effective_coverage=coverage,
            raw_tail_end_event_id=tail_end,
        )

    async def claim_next_job(
        self, *, lease_owner: str, lease_seconds: int
    ) -> RollupJobClaim | None:
        now = _utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        token = uuid.uuid4().hex
        async with self._database.sessions() as session, session.begin():
            candidate_id = await session.scalar(
                select(ConversationRollupJobModel.scope_id)
                .where(
                    or_(
                        and_(
                            ConversationRollupJobModel.status == "pending",
                            ConversationRollupJobModel.next_attempt_at <= now,
                        ),
                        and_(
                            ConversationRollupJobModel.status == "processing",
                            ConversationRollupJobModel.lease_until <= now,
                        ),
                    )
                )
                .order_by(ConversationRollupJobModel.next_attempt_at.asc())
                .limit(1)
            )
            if candidate_id is None:
                return None
            row = await session.scalar(
                update(ConversationRollupJobModel)
                .where(
                    ConversationRollupJobModel.scope_id == candidate_id,
                    or_(
                        and_(
                            ConversationRollupJobModel.status == "pending",
                            ConversationRollupJobModel.next_attempt_at <= now,
                        ),
                        and_(
                            ConversationRollupJobModel.status == "processing",
                            ConversationRollupJobModel.lease_until <= now,
                        ),
                    ),
                )
                .values(
                    status="processing",
                    lease_owner=lease_owner,
                    lease_token=token,
                    lease_until=lease_until,
                    updated_at=now,
                )
                .returning(ConversationRollupJobModel)
            )
            if row is None:
                return None
            return RollupJobClaim(
                scope_id=row.scope_id,
                generation=row.generation,
                claimed_signal_revision=row.signal_revision,
                failure_count=row.failure_count,
                lease_owner=lease_owner,
                lease_token=token,
                lease_until=lease_until,
            )

    async def claim_scope_for_foreground(
        self,
        scope: ConversationScope,
        *,
        lease_owner: str,
        lease_seconds: int,
    ) -> RollupJobClaim | None:
        """Preempt background ownership so foreground can restore a bounded prompt."""

        now = _utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        token = uuid.uuid4().hex
        async with self._database.immediate_session() as session:
            scope_row = await session.scalar(
                select(ConversationScopeModel).where(ConversationScopeModel.scope_key == scope.key)
            )
            if scope_row is None:
                return None
            if _scope_from_row(scope_row) != scope:
                raise ConversationCoverageError("scope key collision")
            job = await session.get(ConversationRollupJobModel, scope_row.id)
            if job is None:
                job = ConversationRollupJobModel(
                    scope_id=scope_row.id,
                    generation=scope_row.generation,
                    signal_revision=1,
                    status="pending",
                    failure_count=0,
                    lease_owner=None,
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=now,
                    last_error_category=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(job)
                await session.flush()
            elif job.generation != scope_row.generation:
                job.generation = scope_row.generation
                job.signal_revision += 1
                job.failure_count = 0
                job.last_error_category = None
            job.status = "processing"
            job.lease_owner = lease_owner
            job.lease_token = token
            job.lease_until = lease_until
            job.next_attempt_at = now
            job.updated_at = now
            return RollupJobClaim(
                scope_id=scope_row.id,
                generation=scope_row.generation,
                claimed_signal_revision=job.signal_revision,
                failure_count=job.failure_count,
                lease_owner=lease_owner,
                lease_token=token,
                lease_until=lease_until,
            )

    async def heartbeat(self, claim: RollupJobClaim, *, lease_seconds: int) -> RollupJobClaim:
        now = _utcnow()
        renewed = now + timedelta(seconds=lease_seconds)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(ConversationRollupJobModel)
                .where(*self._lease_conditions(claim, now=now))
                .values(lease_until=renewed, updated_at=now)
            )
            if not cast(CursorResult[object], result).rowcount:
                raise RollupLeaseLostError("rollup heartbeat lost its lease")
        return RollupJobClaim(
            scope_id=claim.scope_id,
            generation=claim.generation,
            claimed_signal_revision=claim.claimed_signal_revision,
            failure_count=claim.failure_count,
            lease_owner=claim.lease_owner,
            lease_token=claim.lease_token,
            lease_until=renewed,
        )

    async def candidate_for_claim(self, claim: RollupJobClaim) -> RollupCandidate | None:
        now = _utcnow()
        async with self._database.sessions() as session, session.begin():
            job = await session.get(ConversationRollupJobModel, claim.scope_id)
            if job is None or not self._lease_matches(job, claim, now=now):
                raise RollupLeaseLostError("rollup candidate read lost its lease")
            scope_row = await session.get(ConversationScopeModel, claim.scope_id)
            if scope_row is None or scope_row.generation != claim.generation:
                return None
            scope = _scope_from_row(scope_row)
            rollup = await session.get(ConversationRollupModel, claim.scope_id)
            if rollup is not None and rollup.generation != claim.generation:
                raise ConversationCoverageError("rollup generation mismatch")
            coverage = (
                rollup.covered_through_event_id if rollup else scope_row.starts_after_event_id
            )
            revision = rollup.revision if rollup else 0
            previous = rollup.summary_text if rollup else ""
            rows = tuple(
                (
                    await session.scalars(
                        select(ChatEventModel)
                        .where(
                            *_scope_conditions(scope),
                            ChatEventModel.id > coverage,
                            ChatEventModel.id <= scope_row.last_event_id,
                        )
                        .order_by(ChatEventModel.id.asc())
                    )
                ).all()
            )
            all_events = tuple(_event_record(row) for row in rows)
            batch = take_batch(eligible_prefix(all_events, self.config), self.config)
            if not batch:
                return None
            characters = sum(projection_characters(event) for event in batch)
            fingerprint = source_fingerprint(
                scope_id=claim.scope_id,
                generation=claim.generation,
                source_coverage=coverage,
                source_rollup_revision=revision,
                previous_summary=previous,
                events=batch,
            )
            return RollupCandidate(
                scope_id=claim.scope_id,
                generation=claim.generation,
                source_coverage=coverage,
                source_rollup_revision=revision,
                previous_summary=previous,
                events=batch,
                event_count=len(batch),
                projection_characters=characters,
                fingerprint=fingerprint,
            )

    async def finish_without_candidate(self, claim: RollupJobClaim) -> bool:
        """Delete only an unchanged job; otherwise restore it to pending."""

        now = _utcnow()
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(ConversationRollupJobModel).where(
                    *self._lease_conditions(claim, now=now),
                    ConversationRollupJobModel.signal_revision == claim.claimed_signal_revision,
                )
            )
            if cast(CursorResult[object], result).rowcount:
                return True
            result = await session.execute(
                update(ConversationRollupJobModel)
                .where(*self._lease_conditions(claim, now=now))
                .values(
                    status="pending",
                    lease_owner=None,
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=now,
                    updated_at=now,
                )
            )
            if not cast(CursorResult[object], result).rowcount:
                raise RollupLeaseLostError("rollup idle completion lost its lease")
        return False

    async def commit_candidate(
        self,
        claim: RollupJobClaim,
        candidate: RollupCandidate,
        *,
        summary_text: str,
        summary_kind: RollupKind,
        retain_lease: bool = False,
    ) -> RollupCommitResult:
        normalized = summary_text.strip()
        if not normalized or len(normalized) > self.config.summary_max_characters:
            raise ValueError("summary violates configured output bounds")
        now = _utcnow()
        async with self._database.sessions() as session, session.begin():
            job = await session.get(ConversationRollupJobModel, claim.scope_id)
            if job is None or not self._lease_matches(job, claim, now=now):
                raise RollupLeaseLostError("rollup commit lost its lease")
            scope_row = await session.get(ConversationScopeModel, claim.scope_id)
            if scope_row is None or scope_row.generation != candidate.generation:
                raise RollupSourceChangedError("scope generation changed")
            scope = _scope_from_row(scope_row)
            current_rollup = await session.get(ConversationRollupModel, claim.scope_id)
            coverage = (
                current_rollup.covered_through_event_id
                if current_rollup is not None
                else scope_row.starts_after_event_id
            )
            revision = current_rollup.revision if current_rollup is not None else 0
            previous = current_rollup.summary_text if current_rollup is not None else ""
            if (
                coverage != candidate.source_coverage
                or revision != candidate.source_rollup_revision
            ):
                raise RollupSourceChangedError("rollup checkpoint changed")
            rows = tuple(
                (
                    await session.scalars(
                        select(ChatEventModel)
                        .where(
                            *_scope_conditions(scope),
                            ChatEventModel.id > coverage,
                            ChatEventModel.id <= candidate.events[-1].id,
                        )
                        .order_by(ChatEventModel.id.asc())
                    )
                ).all()
            )
            events = tuple(_event_record(row) for row in rows)
            fingerprint = source_fingerprint(
                scope_id=candidate.scope_id,
                generation=candidate.generation,
                source_coverage=coverage,
                source_rollup_revision=revision,
                previous_summary=previous,
                events=events,
            )
            if (
                tuple(event.id for event in events) != tuple(event.id for event in candidate.events)
                or fingerprint != candidate.fingerprint
            ):
                raise RollupSourceChangedError("rollup source projection changed")
            covered_through = candidate.events[-1].id
            remaining_rows = tuple(
                (
                    await session.scalars(
                        select(ChatEventModel)
                        .where(
                            *_scope_conditions(scope),
                            ChatEventModel.id > covered_through,
                            ChatEventModel.id <= scope_row.last_event_id,
                        )
                        .order_by(ChatEventModel.id.asc())
                    )
                ).all()
            )
            remaining = tuple(_event_record(row) for row in remaining_rows)
            expected_events = candidate.event_count + len(remaining)
            expected_characters = candidate.projection_characters + sum(
                projection_characters(event) for event in remaining
            )
            if (
                scope_row.uncovered_event_count != expected_events
                or scope_row.uncovered_character_count != expected_characters
            ):
                recounted = await recount_scope_uncovered(session, scope_row)
                self.metrics.counter_repairs += 1
                if recounted != (expected_events, expected_characters):
                    self.metrics.counter_reconcile_failures += 1
                    raise ConversationCoverageError("uncovered counter recount did not converge")
            statement = insert(ConversationRollupModel).values(
                scope_id=claim.scope_id,
                generation=candidate.generation,
                covered_through_event_id=covered_through,
                summary_text=normalized,
                summary_kind=summary_kind.value,
                source_fingerprint=candidate.fingerprint,
                revision=revision + 1,
                created_at=current_rollup.created_at if current_rollup is not None else now,
                updated_at=now,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ConversationRollupModel.scope_id],
                    set_={
                        "generation": candidate.generation,
                        "covered_through_event_id": covered_through,
                        "summary_text": normalized,
                        "summary_kind": summary_kind.value,
                        "source_fingerprint": candidate.fingerprint,
                        "revision": revision + 1,
                        "updated_at": now,
                    },
                )
            )
            scope_row.uncovered_event_count -= candidate.event_count
            scope_row.uncovered_character_count -= candidate.projection_characters
            remaining_characters = sum(projection_characters(event) for event in remaining)
            if (
                scope_row.uncovered_event_count != len(remaining)
                or scope_row.uncovered_character_count != remaining_characters
            ):
                self.metrics.counter_reconcile_failures += 1
                raise ConversationCoverageError("uncovered counters diverged after rollup commit")
            scope_row.updated_at = now
            job.failure_count = 0
            job.last_error_category = None
            continue_work = exceeds_low_watermark(
                eligible_prefix(remaining, self.config), self.config
            )
            signal_changed = job.signal_revision != claim.claimed_signal_revision
            retained = bool(retain_lease and (continue_work or signal_changed))
            if retained:
                job.failure_count = 0
                job.last_error_category = None
                job.updated_at = now
            elif continue_work or signal_changed:
                job.status = "pending"
                job.lease_owner = None
                job.lease_token = None
                job.lease_until = None
                job.next_attempt_at = now
                job.updated_at = now
            else:
                await session.delete(job)
            await session.flush()
            stored = await session.get(ConversationRollupModel, claim.scope_id)
            if stored is None:
                raise ConversationCoverageError("rollup commit did not persist")
            result = RollupCommitResult(
                rollup=_rollup_state(stored),
                claim_retained=retained,
            )
        return result

    async def retry_infrastructure(
        self,
        claim: RollupJobClaim,
        *,
        error_category: str,
        retry_max_seconds: int,
    ) -> None:
        now = _utcnow()
        async with self._database.sessions() as session, session.begin():
            job = await session.scalar(
                select(ConversationRollupJobModel).where(*self._lease_conditions(claim, now=now))
            )
            if job is None:
                raise RollupLeaseLostError("rollup retry lost its lease")
            failure_count = job.failure_count + 1
            delay = min(retry_max_seconds, 15 * (2 ** min(failure_count - 1, 20)))
            job.status = "pending"
            job.failure_count = failure_count
            job.lease_owner = None
            job.lease_token = None
            job.lease_until = None
            job.next_attempt_at = now + timedelta(seconds=delay)
            job.last_error_category = error_category[:64]
            job.updated_at = now

    async def release_owner(self, lease_owner: str) -> int:
        now = _utcnow()
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(ConversationRollupJobModel)
                .where(
                    ConversationRollupJobModel.status == "processing",
                    ConversationRollupJobModel.lease_owner == lease_owner,
                )
                .values(
                    status="pending",
                    lease_owner=None,
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=now,
                    updated_at=now,
                )
            )
            return int(cast(CursorResult[object], result).rowcount or 0)

    @staticmethod
    def _lease_conditions(
        claim: RollupJobClaim,
        *,
        now: datetime,
    ) -> tuple[ColumnElement[bool], ...]:
        return (
            ConversationRollupJobModel.scope_id == claim.scope_id,
            ConversationRollupJobModel.status == "processing",
            ConversationRollupJobModel.lease_owner == claim.lease_owner,
            ConversationRollupJobModel.lease_token == claim.lease_token,
            ConversationRollupJobModel.lease_until > now,
        )

    @staticmethod
    def _lease_matches(
        row: ConversationRollupJobModel, claim: RollupJobClaim, *, now: datetime
    ) -> bool:
        lease_until = row.lease_until
        if lease_until is not None and lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=UTC)
        return (
            row.status == "processing"
            and row.lease_owner == claim.lease_owner
            and row.lease_token == claim.lease_token
            and lease_until is not None
            and lease_until > now
        )


async def get_or_create_scope_row(
    session: AsyncSession,
    scope: ConversationScope,
    *,
    first_event_id: int,
    now: datetime,
) -> ConversationScopeModel:
    """Transactional helper shared by all scoped ledger mutations."""

    statement = insert(ConversationScopeModel).values(
        scope_key=scope.key,
        bot_user_id=scope.bot_user_id,
        scope_type=scope.scope_type.value,
        private_peer_user_id=scope.private_peer_user_id,
        group_id=scope.group_id,
        generation=1,
        starts_after_event_id=max(0, first_event_id - 1),
        last_event_id=max(0, first_event_id - 1),
        last_generation_change_event_id=0,
        uncovered_event_count=0,
        uncovered_character_count=0,
        created_at=now,
        updated_at=now,
    )
    await session.execute(statement.on_conflict_do_nothing(index_elements=["scope_key"]))
    row = await session.scalar(
        select(ConversationScopeModel).where(ConversationScopeModel.scope_key == scope.key)
    )
    if row is None or _scope_from_row(row) != scope:
        raise ConversationCoverageError("could not create a valid conversation scope")
    return row


async def recount_scope_uncovered(
    session: AsyncSession,
    scope_row: ConversationScopeModel,
) -> tuple[int, int]:
    """Repair exact current-generation counters from the canonical ledger once."""

    scope = _scope_from_row(scope_row)
    rollup = await session.get(ConversationRollupModel, scope_row.id)
    if rollup is not None and rollup.generation != scope_row.generation:
        raise ConversationCoverageError("cannot recount across rollup generations")
    coverage = rollup.covered_through_event_id if rollup else scope_row.starts_after_event_id
    if not scope_row.starts_after_event_id <= coverage <= scope_row.last_event_id:
        raise ConversationCoverageError("cannot recount outside scope bounds")
    rows = tuple(
        (
            await session.scalars(
                select(ChatEventModel)
                .where(
                    *_scope_conditions(scope),
                    ChatEventModel.id > coverage,
                    ChatEventModel.id <= scope_row.last_event_id,
                )
                .order_by(ChatEventModel.id.asc())
            )
        ).all()
    )
    events = tuple(_event_record(row) for row in rows)
    scope_row.uncovered_event_count = len(events)
    scope_row.uncovered_character_count = sum(projection_characters(event) for event in events)
    return scope_row.uncovered_event_count, scope_row.uncovered_character_count
