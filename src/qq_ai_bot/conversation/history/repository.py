"""Transactional persistence for conversation history rollup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.history.db_models import (
    ConversationHistoryRollupJobModel,
    ConversationHistoryStateModel,
    ConversationHistorySummaryMemberModel,
    ConversationHistorySummaryModel,
)
from qq_ai_bot.conversation.history.errors import (
    FrontierInvariantError,
    HistoryIdentityError,
    HistoryJobConflictError,
)
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    ConversationHistoryJob,
    ConversationHistoryMember,
    ConversationHistoryState,
    ConversationHistorySummary,
    HistoryContextSnapshot,
    HistoryJobKind,
    HistoryJobOutcome,
    HistoryJobStatus,
    HistoryMemberType,
    HistorySummaryMode,
    HistorySummaryStatus,
    HistorySummaryTrust,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import _event_record
from qq_ai_bot.persistence.repository_records import EventRecord


class ConversationHistoryRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_or_create_state(
        self, identity: ConversationHistoryIdentity
    ) -> ConversationHistoryState:
        self._validate_identity(identity)
        now = datetime.now(UTC)
        async with self._database.sessions() as session:
            row = await session.scalar(self._state_query(identity))
            if row is not None:
                return self._state(row)
        try:
            async with self._database.sessions() as session, session.begin():
                row = ConversationHistoryStateModel(
                    bot_user_id=identity.bot_user_id,
                    scope_type=identity.scope_type.value,
                    private_peer_user_id=identity.private_peer_user_id,
                    group_id=identity.group_id,
                    reset_at=identity.reset_at,
                    last_seen_event_id=0,
                    active_frontier_end_event_id=0,
                    pending_event_count=0,
                    pending_character_count=0,
                    revision=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                await session.flush()
                return self._state(row)
        except IntegrityError:
            async with self._database.sessions() as session:
                existing = await session.scalar(self._state_query(identity))
                if existing is None:
                    raise
                return self._state(existing)

    async def observe_event(
        self,
        identity: ConversationHistoryIdentity,
        *,
        event_id: int,
        character_count: int,
    ) -> ConversationHistoryState:
        if event_id <= 0:
            raise HistoryIdentityError("event id must be positive")
        if character_count < 0:
            raise HistoryIdentityError("character count must not be negative")
        state = await self.get_or_create_state(identity)
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(ConversationHistoryStateModel, state.id)
            if row is None:
                raise HistoryIdentityError("conversation history state disappeared")
            if event_id <= row.last_seen_event_id:
                return self._state(row)
            row.last_seen_event_id = event_id
            row.pending_event_count += 1
            row.pending_character_count += character_count
            row.revision += 1
            row.updated_at = now
            await session.flush()
            return self._state(row)

    async def enqueue_job(
        self,
        *,
        state_id: int,
        job_kind: HistoryJobKind,
        source_level: int,
        source_start_id: int,
        source_end_id: int,
        source_fingerprint: str,
        summarizer_version: str,
    ) -> ConversationHistoryJob:
        now = datetime.now(UTC)
        async with self._database.sessions() as session:
            existing = await session.scalar(
                select(ConversationHistoryRollupJobModel).where(
                    ConversationHistoryRollupJobModel.state_id == state_id,
                    ConversationHistoryRollupJobModel.job_kind == job_kind.value,
                    ConversationHistoryRollupJobModel.source_fingerprint == source_fingerprint,
                    ConversationHistoryRollupJobModel.summarizer_version == summarizer_version,
                )
            )
            if existing is not None:
                return self._job(existing)
        try:
            async with self._database.sessions() as session, session.begin():
                row = ConversationHistoryRollupJobModel(
                    state_id=state_id,
                    job_kind=job_kind.value,
                    source_level=source_level,
                    source_start_id=source_start_id,
                    source_end_id=source_end_id,
                    source_fingerprint=source_fingerprint,
                    summarizer_version=summarizer_version,
                    status=HistoryJobStatus.PENDING.value,
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                await session.flush()
                return self._job(row)
        except IntegrityError:
            async with self._database.sessions() as session:
                recovered = await session.scalar(
                    select(ConversationHistoryRollupJobModel).where(
                        ConversationHistoryRollupJobModel.state_id == state_id,
                        ConversationHistoryRollupJobModel.job_kind == job_kind.value,
                        ConversationHistoryRollupJobModel.source_fingerprint == source_fingerprint,
                        ConversationHistoryRollupJobModel.summarizer_version == summarizer_version,
                    )
                )
                if recovered is None:
                    raise
                return self._job(recovered)

    async def claim_next_job(
        self,
        *,
        lease_owner: str,
        lease_seconds: int,
    ) -> ConversationHistoryJob | None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.scalar(
                select(ConversationHistoryRollupJobModel)
                .where(
                    ConversationHistoryRollupJobModel.next_attempt_at <= now,
                    or_(
                        ConversationHistoryRollupJobModel.status == HistoryJobStatus.PENDING.value,
                        and_(
                            ConversationHistoryRollupJobModel.status
                            == HistoryJobStatus.PROCESSING.value,
                            ConversationHistoryRollupJobModel.lease_until.is_not(None),
                            ConversationHistoryRollupJobModel.lease_until < now,
                        ),
                    ),
                    ~ConversationHistoryRollupJobModel.state_id.in_(
                        select(ConversationHistoryRollupJobModel.state_id).where(
                            ConversationHistoryRollupJobModel.status
                            == HistoryJobStatus.PROCESSING.value,
                            ConversationHistoryRollupJobModel.lease_until.is_not(None),
                            ConversationHistoryRollupJobModel.lease_until >= now,
                        )
                    ),
                )
                .order_by(
                    ConversationHistoryRollupJobModel.next_attempt_at,
                    ConversationHistoryRollupJobModel.id,
                )
                .limit(1)
                .with_for_update()
            )
            if row is None:
                return None
            result = await session.execute(
                update(ConversationHistoryRollupJobModel)
                .where(
                    ConversationHistoryRollupJobModel.id == row.id,
                    or_(
                        ConversationHistoryRollupJobModel.status == HistoryJobStatus.PENDING.value,
                        and_(
                            ConversationHistoryRollupJobModel.status
                            == HistoryJobStatus.PROCESSING.value,
                            ConversationHistoryRollupJobModel.lease_until < now,
                        ),
                    ),
                )
                .values(
                    status=HistoryJobStatus.PROCESSING.value,
                    lease_owner=lease_owner,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    attempts=ConversationHistoryRollupJobModel.attempts + 1,
                    updated_at=now,
                )
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                return None
            await session.refresh(row)
            return self._job(row)

    async def retry_job(
        self,
        job_id: int,
        *,
        lease_owner: str,
        delay_seconds: int,
        error_category: str,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(ConversationHistoryRollupJobModel, job_id)
            if row is None:
                raise HistoryJobConflictError("job disappeared")
            if row.lease_owner != lease_owner:
                raise HistoryJobConflictError("lease owner mismatch")
            row.status = HistoryJobStatus.PENDING.value
            row.lease_owner = None
            row.lease_until = None
            row.error_category = error_category
            row.next_attempt_at = now + timedelta(seconds=delay_seconds)
            row.updated_at = now

    async def fail_job(
        self,
        job_id: int,
        *,
        lease_owner: str,
        error_category: str,
    ) -> ConversationHistoryJob:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(ConversationHistoryRollupJobModel, job_id)
            if row is None:
                raise HistoryJobConflictError("job disappeared")
            if row.lease_owner != lease_owner:
                raise HistoryJobConflictError("lease owner mismatch")
            row.status = HistoryJobStatus.FAILED.value
            row.error_category = error_category
            row.lease_owner = None
            row.lease_until = None
            row.completed_at = now
            row.updated_at = now
            await session.flush()
            return self._job(row)

    async def complete_job(
        self,
        job_id: int,
        *,
        lease_owner: str,
        outcome: HistoryJobOutcome,
        result_summary_id: int | None,
    ) -> ConversationHistoryJob:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(ConversationHistoryRollupJobModel, job_id)
            if row is None:
                raise HistoryJobConflictError("job disappeared")
            if row.lease_owner != lease_owner:
                raise HistoryJobConflictError("lease owner mismatch")
            if outcome is HistoryJobOutcome.SUMMARY and result_summary_id is None:
                raise HistoryJobConflictError("summary outcome requires result_summary_id")
            if outcome is HistoryJobOutcome.NO_CHANGE and result_summary_id is not None:
                raise HistoryJobConflictError("no_change outcome cannot attach a summary")
            row.status = HistoryJobStatus.DONE.value
            row.outcome = outcome.value
            row.result_summary_id = result_summary_id
            row.lease_owner = None
            row.lease_until = None
            row.completed_at = now
            row.updated_at = now
            await session.flush()
            return self._job(row)

    async def release_leases_for_owner(self, lease_owner: str) -> int:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(ConversationHistoryRollupJobModel)
                .where(
                    ConversationHistoryRollupJobModel.status == HistoryJobStatus.PROCESSING.value,
                    ConversationHistoryRollupJobModel.lease_owner == lease_owner,
                )
                .values(
                    status=HistoryJobStatus.PENDING.value,
                    lease_owner=None,
                    lease_until=None,
                    next_attempt_at=now,
                    updated_at=now,
                )
            )
            return int(cast(CursorResult[Any], result).rowcount)

    async def release_stale_leases(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(ConversationHistoryRollupJobModel)
                .where(
                    ConversationHistoryRollupJobModel.status == HistoryJobStatus.PROCESSING.value,
                    ConversationHistoryRollupJobModel.lease_until.is_not(None),
                    ConversationHistoryRollupJobModel.lease_until < current,
                )
                .values(
                    status=HistoryJobStatus.PENDING.value,
                    lease_owner=None,
                    lease_until=None,
                    next_attempt_at=current,
                    updated_at=current,
                )
            )
        return int(cast(CursorResult[Any], result).rowcount)

    async def list_active_frontier(self, state_id: int) -> tuple[ConversationHistorySummary, ...]:
        async with self._database.sessions() as session:
            return await self._load_frontier(session, state_id)

    async def load_source_events(
        self,
        identity: ConversationHistoryIdentity,
        *,
        start_event_id: int,
        end_event_id: int,
    ) -> tuple[EventRecord, ...]:
        self._validate_identity(identity)
        async with self._database.sessions() as session:
            query = select(ChatEventModel).where(
                ChatEventModel.bot_user_id == identity.bot_user_id,
                ChatEventModel.scope_type == identity.scope_type.value,
                ChatEventModel.id >= start_event_id,
                ChatEventModel.id <= end_event_id,
            )
            if identity.scope_type is ScopeType.PRIVATE:
                query = query.where(
                    ChatEventModel.private_peer_user_id == identity.private_peer_user_id
                )
            else:
                query = query.where(ChatEventModel.group_id == identity.group_id)
            if identity.reset_at is not None:
                query = query.where(ChatEventModel.occurred_at >= identity.reset_at)
            rows = (await session.scalars(query.order_by(ChatEventModel.id))).all()
        return tuple(_event_record(row) for row in rows)

    async def load_source_summaries(
        self, summary_ids: tuple[int, ...]
    ) -> tuple[ConversationHistorySummary, ...]:
        if not summary_ids:
            return ()
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ConversationHistorySummaryModel).where(
                        ConversationHistorySummaryModel.id.in_(summary_ids)
                    )
                )
            ).all()
            members = await self._members_by_summary(session, tuple(row.id for row in rows))
        by_id = {row.id: row for row in rows}
        ordered = tuple(by_id[item] for item in summary_ids if item in by_id)
        return tuple(self._summary(row, members.get(row.id, ())) for row in ordered)

    async def commit_l0_summary(
        self,
        *,
        state_id: int,
        event_ids: tuple[int, ...],
        fingerprint: str,
        mode: HistorySummaryMode,
        summarizer_version: str,
        rendered_text: str,
        structured_payload_json: str,
        start_occurred_at: datetime,
        end_occurred_at: datetime,
        source_character_count: int,
    ) -> ConversationHistorySummary:
        if not event_ids:
            raise FrontierInvariantError("L0 summary requires event members")
        if mode is HistorySummaryMode.EXTRACTIVE and summarizer_version.strip() == "":
            raise FrontierInvariantError("extractive summarizer_version is required")
        ordered = tuple(sorted(event_ids))
        if ordered != event_ids:
            raise FrontierInvariantError("L0 event members must be in id order")
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            existing = await session.scalar(
                select(ConversationHistorySummaryModel).where(
                    ConversationHistorySummaryModel.state_id == state_id,
                    ConversationHistorySummaryModel.source_fingerprint == fingerprint,
                    ConversationHistorySummaryModel.status == HistorySummaryStatus.ACTIVE.value,
                )
            )
            if existing is not None:
                if existing.mode == mode.value:
                    members = await self._members_by_summary(session, (existing.id,))
                    return self._summary(existing, members.get(existing.id, ()))
                if (
                    mode is not HistorySummaryMode.MODEL_SUMMARY
                    or existing.mode != HistorySummaryMode.EXTRACTIVE.value
                ):
                    raise FrontierInvariantError("active fingerprint already occupied")
            extractive = existing
            if extractive is not None:
                extractive.status = HistorySummaryStatus.INVALIDATED.value
                extractive.updated_at = now
                await session.flush()
            row = ConversationHistorySummaryModel(
                state_id=state_id,
                level=0,
                status=HistorySummaryStatus.ACTIVE.value,
                start_event_id=ordered[0],
                end_event_id=ordered[-1],
                start_occurred_at=start_occurred_at,
                end_occurred_at=end_occurred_at,
                source_event_count=len(ordered),
                source_character_count=source_character_count,
                output_character_count=len(rendered_text),
                structured_payload_json=structured_payload_json,
                rendered_text=rendered_text,
                mode=mode.value,
                trust=(
                    HistorySummaryTrust.EXTRACTIVE_COMPACT.value
                    if mode is HistorySummaryMode.EXTRACTIVE
                    else HistorySummaryTrust.MODEL_SUMMARY.value
                ),
                summarizer_version=summarizer_version,
                source_fingerprint=fingerprint,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            for ordinal, event_id in enumerate(event_ids):
                session.add(
                    ConversationHistorySummaryMemberModel(
                        summary_id=row.id,
                        member_type=HistoryMemberType.EVENT.value,
                        source_event_id=event_id,
                        source_summary_id=None,
                        ordinal=ordinal,
                        created_at=now,
                    )
                )
            if extractive is not None:
                extractive.status = HistorySummaryStatus.ROLLED_UP.value
                extractive.replaced_by_summary_id = row.id
                extractive.updated_at = now
            await session.flush()
            await self._sync_frontier_end(session, state_id, now)
            await self._assert_frontier(session, state_id)
            members = await self._members_by_summary(session, (row.id,))
            return self._summary(row, members.get(row.id, ()))

    async def commit_parent_summary_and_retire_children(
        self,
        *,
        state_id: int,
        child_ids: tuple[int, ...],
        fingerprint: str,
        summarizer_version: str,
        rendered_text: str,
        structured_payload_json: str,
        start_occurred_at: datetime,
        end_occurred_at: datetime,
        source_character_count: int,
    ) -> ConversationHistorySummary:
        if len(child_ids) < 2:
            raise FrontierInvariantError("parent summary requires contiguous children")
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            children = (
                await session.scalars(
                    select(ConversationHistorySummaryModel)
                    .where(ConversationHistorySummaryModel.id.in_(child_ids))
                    .order_by(ConversationHistorySummaryModel.start_event_id)
                )
            ).all()
            if {row.id for row in children} != set(child_ids):
                raise FrontierInvariantError("parent children are missing")
            ordered_children = tuple(children)
            self._assert_child_group(ordered_children, state_id=state_id, requested=child_ids)
            parent = ConversationHistorySummaryModel(
                state_id=state_id,
                level=ordered_children[0].level + 1,
                status=HistorySummaryStatus.ACTIVE.value,
                start_event_id=ordered_children[0].start_event_id,
                end_event_id=ordered_children[-1].end_event_id,
                start_occurred_at=start_occurred_at,
                end_occurred_at=end_occurred_at,
                source_event_count=sum(item.source_event_count for item in ordered_children),
                source_character_count=source_character_count,
                output_character_count=len(rendered_text),
                structured_payload_json=structured_payload_json,
                rendered_text=rendered_text,
                mode=HistorySummaryMode.MODEL_SUMMARY.value,
                trust=HistorySummaryTrust.MODEL_SUMMARY.value,
                summarizer_version=summarizer_version,
                source_fingerprint=fingerprint,
                created_at=now,
                updated_at=now,
            )
            session.add(parent)
            await session.flush()
            for ordinal, child in enumerate(ordered_children):
                session.add(
                    ConversationHistorySummaryMemberModel(
                        summary_id=parent.id,
                        member_type=HistoryMemberType.SUMMARY.value,
                        source_event_id=None,
                        source_summary_id=child.id,
                        ordinal=ordinal,
                        created_at=now,
                    )
                )
            for child in ordered_children:
                child.status = HistorySummaryStatus.ROLLED_UP.value
                child.replaced_by_summary_id = parent.id
                child.updated_at = now
            await session.flush()
            await self._sync_frontier_end(session, state_id, now)
            await self._assert_frontier(session, state_id)
            members = await self._members_by_summary(session, (parent.id,))
            return self._summary(parent, members.get(parent.id, ()))

    async def invalidate_summary_tree(self, summary_id: int) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            root = await session.get(ConversationHistorySummaryModel, summary_id)
            if root is None:
                raise FrontierInvariantError("summary disappeared")
            pending = [root]
            seen: set[int] = set()
            while pending:
                current = pending.pop()
                if current.id in seen:
                    continue
                seen.add(current.id)
                current.status = HistorySummaryStatus.INVALIDATED.value
                current.updated_at = now
                children = (
                    await session.scalars(
                        select(ConversationHistorySummaryModel).where(
                            ConversationHistorySummaryModel.replaced_by_summary_id == current.id
                        )
                    )
                ).all()
                pending.extend(children)
            await session.flush()
            await self._sync_frontier_end(session, root.state_id, now)
            await self._assert_frontier(session, root.state_id)

    async def validate_frontier(self, state_id: int) -> None:
        async with self._database.sessions() as session:
            await self._assert_frontier(session, state_id)

    async def load_context_snapshot(self, state_id: int) -> HistoryContextSnapshot:
        async with self._database.sessions() as session:
            state = await session.get(ConversationHistoryStateModel, state_id)
            if state is None:
                raise HistoryIdentityError("conversation history state disappeared")
            frontier = await self._load_frontier(session, state_id)
            return HistoryContextSnapshot(
                state=self._state(state),
                frontier=frontier,
                coverage_end_event_id=state.active_frontier_end_event_id,
                revision=state.revision,
            )

    @staticmethod
    def _validate_identity(identity: ConversationHistoryIdentity) -> None:
        if identity.scope_type is ScopeType.PRIVATE:
            if not identity.private_peer_user_id or identity.group_id is not None:
                raise HistoryIdentityError("private identity requires peer and no group")
            return
        if identity.scope_type is ScopeType.GROUP:
            if not identity.group_id or identity.private_peer_user_id is not None:
                raise HistoryIdentityError("group identity requires group and no peer")
            return
        raise HistoryIdentityError("unsupported conversation scope")

    @staticmethod
    def _state_query(identity: ConversationHistoryIdentity) -> Select[Any]:
        filters = [
            ConversationHistoryStateModel.bot_user_id == identity.bot_user_id,
            ConversationHistoryStateModel.scope_type == identity.scope_type.value,
        ]
        if identity.reset_at is None:
            filters.append(ConversationHistoryStateModel.reset_at.is_(None))
        else:
            filters.append(ConversationHistoryStateModel.reset_at == identity.reset_at)
        if identity.scope_type is ScopeType.PRIVATE:
            filters.append(
                ConversationHistoryStateModel.private_peer_user_id == identity.private_peer_user_id
            )
            filters.append(ConversationHistoryStateModel.group_id.is_(None))
        else:
            filters.append(ConversationHistoryStateModel.group_id == identity.group_id)
            filters.append(ConversationHistoryStateModel.private_peer_user_id.is_(None))
        return select(ConversationHistoryStateModel).where(*filters)

    async def _load_frontier(
        self, session: AsyncSession, state_id: int
    ) -> tuple[ConversationHistorySummary, ...]:
        rows = (
            await session.scalars(
                select(ConversationHistorySummaryModel)
                .where(
                    ConversationHistorySummaryModel.state_id == state_id,
                    ConversationHistorySummaryModel.status == HistorySummaryStatus.ACTIVE.value,
                )
                .order_by(ConversationHistorySummaryModel.start_event_id)
            )
        ).all()
        members = await self._members_by_summary(session, tuple(row.id for row in rows))
        return tuple(self._summary(row, members.get(row.id, ())) for row in rows)

    async def _members_by_summary(
        self, session: AsyncSession, summary_ids: tuple[int, ...]
    ) -> dict[int, tuple[ConversationHistoryMember, ...]]:
        if not summary_ids:
            return {}
        rows = (
            await session.scalars(
                select(ConversationHistorySummaryMemberModel)
                .where(ConversationHistorySummaryMemberModel.summary_id.in_(summary_ids))
                .order_by(
                    ConversationHistorySummaryMemberModel.summary_id,
                    ConversationHistorySummaryMemberModel.ordinal,
                )
            )
        ).all()
        grouped: dict[int, list[ConversationHistoryMember]] = {item: [] for item in summary_ids}
        for row in rows:
            grouped.setdefault(row.summary_id, []).append(self._member(row))
        return {key: tuple(value) for key, value in grouped.items()}

    async def _sync_frontier_end(self, session: AsyncSession, state_id: int, now: datetime) -> None:
        maximum = await session.scalar(
            select(func.max(ConversationHistorySummaryModel.end_event_id)).where(
                ConversationHistorySummaryModel.state_id == state_id,
                ConversationHistorySummaryModel.status == HistorySummaryStatus.ACTIVE.value,
            )
        )
        state = await session.get(ConversationHistoryStateModel, state_id)
        if state is None:
            raise HistoryIdentityError("conversation history state disappeared")
        state.active_frontier_end_event_id = int(maximum or 0)
        state.updated_at = now

    async def _assert_frontier(self, session: AsyncSession, state_id: int) -> None:
        rows = (
            await session.scalars(
                select(ConversationHistorySummaryModel)
                .where(
                    ConversationHistorySummaryModel.state_id == state_id,
                    ConversationHistorySummaryModel.status == HistorySummaryStatus.ACTIVE.value,
                )
                .order_by(ConversationHistorySummaryModel.start_event_id)
            )
        ).all()
        previous: ConversationHistorySummaryModel | None = None
        active_ids = {row.id for row in rows}
        for row in rows:
            if previous is not None:
                if row.start_event_id <= previous.end_event_id:
                    raise FrontierInvariantError("active frontier overlaps")
                if row.start_event_id != previous.end_event_id + 1:
                    raise FrontierInvariantError("active frontier has a coverage gap")
            previous = row
        member_rows = (
            await session.scalars(
                select(ConversationHistorySummaryMemberModel).where(
                    ConversationHistorySummaryMemberModel.summary_id.in_(tuple(active_ids) or (0,)),
                    ConversationHistorySummaryMemberModel.member_type
                    == HistoryMemberType.SUMMARY.value,
                )
            )
        ).all()
        for member in member_rows:
            if member.source_summary_id in active_ids:
                raise FrontierInvariantError("active parent and child cannot coexist")
        state = await session.get(ConversationHistoryStateModel, state_id)
        if state is None:
            raise HistoryIdentityError("conversation history state disappeared")
        expected = rows[-1].end_event_id if rows else 0
        if state.active_frontier_end_event_id != expected:
            raise FrontierInvariantError("frontier end does not match active coverage")

    @staticmethod
    def _assert_child_group(
        children: tuple[ConversationHistorySummaryModel, ...],
        *,
        state_id: int,
        requested: tuple[int, ...],
    ) -> None:
        if any(row.state_id != state_id for row in children):
            raise FrontierInvariantError("parent children must belong to one state")
        levels = {row.level for row in children}
        if len(levels) != 1:
            raise FrontierInvariantError("parent children must share one level")
        if any(row.status != HistorySummaryStatus.ACTIVE.value for row in children):
            raise FrontierInvariantError("parent children must still be active")
        if set(requested) != {row.id for row in children}:
            raise FrontierInvariantError("parent children are missing")
        previous: ConversationHistorySummaryModel | None = None
        for child in children:
            if previous is not None and child.start_event_id != previous.end_event_id + 1:
                raise FrontierInvariantError("parent children are not contiguous")
            previous = child

    @staticmethod
    def _state(row: ConversationHistoryStateModel) -> ConversationHistoryState:
        return ConversationHistoryState(
            id=row.id,
            bot_user_id=row.bot_user_id,
            scope_type=row.scope_type,
            private_peer_user_id=row.private_peer_user_id,
            group_id=row.group_id,
            reset_at=row.reset_at,
            last_seen_event_id=row.last_seen_event_id,
            active_frontier_end_event_id=row.active_frontier_end_event_id,
            pending_event_count=row.pending_event_count,
            pending_character_count=row.pending_character_count,
            revision=row.revision,
        )

    @staticmethod
    def _summary(
        row: ConversationHistorySummaryModel,
        members: tuple[ConversationHistoryMember, ...],
    ) -> ConversationHistorySummary:
        return ConversationHistorySummary(
            id=row.id,
            state_id=row.state_id,
            level=row.level,
            status=HistorySummaryStatus(row.status),
            start_event_id=row.start_event_id,
            end_event_id=row.end_event_id,
            mode=HistorySummaryMode(row.mode),
            trust=HistorySummaryTrust(row.trust),
            summarizer_version=row.summarizer_version,
            source_fingerprint=row.source_fingerprint,
            replaced_by_summary_id=row.replaced_by_summary_id,
            rendered_text=row.rendered_text,
            members=members,
        )

    @staticmethod
    def _member(row: ConversationHistorySummaryMemberModel) -> ConversationHistoryMember:
        return ConversationHistoryMember(
            member_type=HistoryMemberType(row.member_type),
            ordinal=row.ordinal,
            source_event_id=row.source_event_id,
            source_summary_id=row.source_summary_id,
        )

    @staticmethod
    def _job(row: ConversationHistoryRollupJobModel) -> ConversationHistoryJob:
        return ConversationHistoryJob(
            id=row.id,
            state_id=row.state_id,
            job_kind=HistoryJobKind(row.job_kind),
            source_level=row.source_level,
            source_start_id=row.source_start_id,
            source_end_id=row.source_end_id,
            source_fingerprint=row.source_fingerprint,
            summarizer_version=row.summarizer_version,
            status=HistoryJobStatus(row.status),
            attempts=row.attempts,
            outcome=HistoryJobOutcome(row.outcome) if row.outcome else None,
            result_summary_id=row.result_summary_id,
            lease_owner=row.lease_owner,
        )
