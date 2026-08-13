"""Persistence-only repositories for Memory V2 facts, evidence, and jobs."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.eligibility import MemoryEventEligibilityPolicy
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryJobStatus,
    MemoryProcessingSource,
    MemoryRebuildJobOutcome,
    MemoryScopeType,
    MemoryStateAction,
    MemoryStatus,
)
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryEvidence,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactCreate,
    MemoryFactQuery,
    MemoryFactRelation,
    MemoryFactStateEvent,
    MemoryJob,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MembershipModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryFactRelationModel,
    MemoryFactStateEventModel,
    MemoryJobModel,
    PersonModel,
)
from qq_ai_bot.persistence.repository_helpers import (
    _ensure_group,
    _ensure_person,
    _event_record,
)

logger = logging.getLogger(__name__)


class MemoryFactRepository:
    """Store and query facts without extraction or prompt logic."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._database.sessions() as session, session.begin():
            yield session

    async def count_active_for_create(self, fact: MemoryFactCreate) -> int:
        """Count current active facts in the exact target without mutating capacity."""

        return await self.count_active(
            MemoryFactQuery(
                scope_type=fact.scope_type,
                subject_user_id=fact.subject_user_id,
                group_id=fact.group_id,
                visibility_type=fact.visibility_type,
                visibility_user_id=fact.visibility_user_id,
                visibility_group_id=fact.visibility_group_id,
            )
        )

    async def list_facts(
        self,
        query: MemoryFactQuery,
        *,
        limit: int = 100,
        after_id: int | None = None,
        include_quarantined: bool = False,
        order_by_id: bool = False,
        order_by_id_desc: bool = False,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        if order_by_id and order_by_id_desc:
            raise ValueError("memory facts cannot use both ascending and descending id order")
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_facts(
                    query,
                    limit=limit,
                    after_id=after_id,
                    include_quarantined=include_quarantined,
                    order_by_id=order_by_id,
                    order_by_id_desc=order_by_id_desc,
                    session=owned,
                )
        conditions = [
            MemoryFactModel.scope_type == query.scope_type.value,
            MemoryFactModel.status == query.status.value,
        ]
        if not include_quarantined:
            conditions.append(MemoryFactModel.review_state != "quarantined")
        if after_id is not None:
            conditions.append(MemoryFactModel.id > after_id)
        if query.subject_user_id is None:
            conditions.append(MemoryFactModel.subject_user_id.is_(None))
        else:
            conditions.append(MemoryFactModel.subject_user_id == query.subject_user_id)
        if query.group_id is None:
            conditions.append(MemoryFactModel.group_id.is_(None))
        else:
            conditions.append(MemoryFactModel.group_id == query.group_id)
        conditions.extend(self._exact_visibility_conditions(query))
        if query.kind is not None:
            conditions.append(MemoryFactModel.kind == query.kind.value)
        if query.status is MemoryStatus.ACTIVE:
            conditions.append(
                or_(
                    MemoryFactModel.valid_until.is_(None),
                    MemoryFactModel.valid_until > datetime.now(UTC),
                )
            )
        order: tuple[Any, ...]
        if order_by_id:
            order = (MemoryFactModel.id.asc(),)
        elif order_by_id_desc:
            order = (MemoryFactModel.id.desc(),)
        else:
            order = (MemoryFactModel.importance.desc(), MemoryFactModel.updated_at.desc())
        statement = (
            select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
            .outerjoin(MemoryEvidenceModel, MemoryEvidenceModel.fact_id == MemoryFactModel.id)
            .where(*conditions)
            .group_by(MemoryFactModel.id)
            .order_by(*order)
            .limit(max(1, limit))
        )
        rows = (await session.execute(statement)).all()
        return tuple(self._project_fact(row, int(evidence_count)) for row, evidence_count in rows)

    async def list_person_facts_projected_to_group(
        self,
        user_id: str,
        group_id: str,
        *,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        """Project global person facts supported by that person's evidence in one group.

        This is a read-only visibility query.  It never changes the canonical fact scope,
        and deliberately requires an inbound event and self/explicit evidence from the
        target user in the current group.
        """

        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_person_facts_projected_to_group(
                    user_id,
                    group_id,
                    limit=limit,
                    session=owned,
                )
        qualifying_evidence = (
            select(MemoryEvidenceModel.id)
            .join(ChatEventModel, ChatEventModel.id == MemoryEvidenceModel.event_id)
            .where(
                MemoryEvidenceModel.fact_id == MemoryFactModel.id,
                MemoryEvidenceModel.source_speaker_user_id == user_id,
                MemoryEvidenceModel.authority.in_(
                    (MemoryAuthority.SELF_REPORT.value, MemoryAuthority.EXPLICIT.value)
                ),
                ChatEventModel.scope_type == "group",
                ChatEventModel.group_id == group_id,
                ChatEventModel.sender_user_id == user_id,
                ChatEventModel.direction == "inbound",
            )
            .correlate(MemoryFactModel)
            .exists()
        )
        statement = (
            select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
            .outerjoin(MemoryEvidenceModel, MemoryEvidenceModel.fact_id == MemoryFactModel.id)
            .where(
                MemoryFactModel.scope_type == MemoryScopeType.PERSON.value,
                MemoryFactModel.subject_user_id == user_id,
                MemoryFactModel.group_id.is_(None),
                MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                MemoryFactModel.review_state != "quarantined",
                or_(
                    MemoryFactModel.valid_until.is_(None),
                    MemoryFactModel.valid_until > datetime.now(UTC),
                ),
                qualifying_evidence,
            )
            .group_by(MemoryFactModel.id)
            .order_by(
                MemoryFactModel.importance.desc(),
                MemoryFactModel.confidence.desc(),
                MemoryFactModel.updated_at.desc(),
                MemoryFactModel.id.asc(),
            )
            .limit(max(1, limit))
        )
        rows = (await session.execute(statement)).all()
        return tuple(self._project_fact(row, int(count)) for row, count in rows)

    async def get_fact(
        self,
        fact_id: int,
        *,
        session: AsyncSession | None = None,
    ) -> MemoryFact | None:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.get_fact(fact_id, session=owned)
        result = (
            await session.execute(
                select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
                .outerjoin(
                    MemoryEvidenceModel,
                    MemoryEvidenceModel.fact_id == MemoryFactModel.id,
                )
                .where(MemoryFactModel.id == fact_id)
                .group_by(MemoryFactModel.id)
            )
        ).first()
        return self._project_fact(result[0], int(result[1])) if result else None

    async def get_active_for_target(
        self,
        target: MemoryEntityTarget,
        fact_ids: tuple[int, ...],
        *,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        """Load candidate facts only inside the already resolved identity boundary."""

        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not unique_ids:
            return ()
        if session is None:
            async with self._database.sessions() as owned:
                return await self.get_active_for_target(target, unique_ids, session=owned)
        statement = (
            select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
            .outerjoin(MemoryEvidenceModel, MemoryEvidenceModel.fact_id == MemoryFactModel.id)
            .where(
                MemoryFactModel.id.in_(unique_ids),
                *self._target_conditions(target),
                MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                MemoryFactModel.review_state != "quarantined",
                or_(
                    MemoryFactModel.valid_until.is_(None),
                    MemoryFactModel.valid_until > datetime.now(UTC),
                ),
            )
            .group_by(MemoryFactModel.id)
        )
        rows = (await session.execute(statement)).all()
        projected = {
            row.id: self._project_fact(row, int(evidence_count)) for row, evidence_count in rows
        }
        return tuple(projected[fact_id] for fact_id in unique_ids if fact_id in projected)

    async def list_conflict_candidates(
        self,
        fact: MemoryFactCreate,
        *,
        normalized_content: str,
        limit: int,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        """Return bounded same-target candidates; never widens an identity scope."""

        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_conflict_candidates(
                    fact,
                    normalized_content=normalized_content,
                    limit=limit,
                    session=owned,
                )
        rows = (
            await session.execute(
                select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
                .outerjoin(MemoryEvidenceModel, MemoryEvidenceModel.fact_id == MemoryFactModel.id)
                .where(
                    MemoryFactModel.scope_type == fact.scope_type.value,
                    MemoryFactModel.subject_user_id == fact.subject_user_id,
                    MemoryFactModel.group_id == fact.group_id,
                    *self._exact_visibility_conditions(fact),
                    MemoryFactModel.status.in_(
                        (
                            MemoryStatus.ACTIVE.value,
                            MemoryStatus.CONTESTED.value,
                        )
                    ),
                    MemoryFactModel.review_state != "quarantined",
                    or_(
                        MemoryFactModel.memory_key == fact.memory_key,
                        MemoryFactModel.normalized_content == normalized_content,
                        and_(
                            MemoryFactModel.category == fact.category,
                            MemoryFactModel.kind == fact.kind.value,
                        ),
                    ),
                )
                .group_by(MemoryFactModel.id)
                .order_by(
                    (MemoryFactModel.memory_key == fact.memory_key).desc(),
                    (MemoryFactModel.normalized_content == normalized_content).desc(),
                    MemoryFactModel.updated_at.desc(),
                    MemoryFactModel.id.asc(),
                )
                .limit(max(1, limit))
            )
        ).all()
        return tuple(self._project_fact(row, int(count)) for row, count in rows)

    async def list_overview(
        self,
        target: MemoryEntityTarget,
        *,
        limit: int,
    ) -> tuple[MemoryFact, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
                    .outerjoin(
                        MemoryEvidenceModel,
                        MemoryEvidenceModel.fact_id == MemoryFactModel.id,
                    )
                    .where(
                        *self._target_conditions(target),
                        MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                        MemoryFactModel.review_state != "quarantined",
                        or_(
                            MemoryFactModel.valid_until.is_(None),
                            MemoryFactModel.valid_until > datetime.now(UTC),
                        ),
                    )
                    .group_by(MemoryFactModel.id)
                    .order_by(
                        MemoryFactModel.importance.desc(),
                        MemoryFactModel.confidence.desc(),
                        MemoryFactModel.updated_at.desc(),
                        MemoryFactModel.id.asc(),
                    )
                    .limit(max(1, limit))
                )
            ).all()
        return tuple(self._project_fact(row, int(count)) for row, count in rows)

    async def list_explicit_preferences(
        self,
        target: MemoryEntityTarget,
        *,
        limit: int,
    ) -> tuple[MemoryFact, ...]:
        if limit <= 0:
            return ()
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
                    .outerjoin(
                        MemoryEvidenceModel,
                        MemoryEvidenceModel.fact_id == MemoryFactModel.id,
                    )
                    .where(
                        *self._target_conditions(target),
                        MemoryFactModel.kind == "preference",
                        MemoryFactModel.source_type == "explicit",
                        MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                        MemoryFactModel.review_state != "quarantined",
                        or_(
                            MemoryFactModel.valid_until.is_(None),
                            MemoryFactModel.valid_until > datetime.now(UTC),
                        ),
                    )
                    .group_by(MemoryFactModel.id)
                    .order_by(
                        MemoryFactModel.importance.desc(),
                        MemoryFactModel.confidence.desc(),
                        MemoryFactModel.updated_at.desc(),
                        MemoryFactModel.id.asc(),
                    )
                    .limit(limit)
                )
            ).all()
        return tuple(self._project_fact(row, int(count)) for row, count in rows)

    async def mark_used(self, fact_ids: tuple[int, ...]) -> int:
        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not unique_ids:
            return 0
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(MemoryFactModel)
                .where(
                    MemoryFactModel.id.in_(unique_ids),
                    MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                    MemoryFactModel.review_state != "quarantined",
                )
                .values(last_used_at=datetime.now(UTC))
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def find_active(
        self,
        fact: MemoryFactCreate,
        *,
        session: AsyncSession,
    ) -> MemoryFactModel | None:
        conditions = [
            MemoryFactModel.scope_type == fact.scope_type.value,
            MemoryFactModel.subject_user_id == fact.subject_user_id,
            MemoryFactModel.group_id == fact.group_id,
            MemoryFactModel.memory_key == fact.memory_key,
            MemoryFactModel.status == MemoryStatus.ACTIVE.value,
            *self._exact_visibility_conditions(fact),
        ]
        if fact.scope_type is not MemoryScopeType.SELF:
            conditions.append(MemoryFactModel.kind == fact.kind.value)
        return cast(
            MemoryFactModel | None,
            await session.scalar(select(MemoryFactModel).where(*conditions)),
        )

    async def create_fact(
        self,
        fact: MemoryFactCreate,
        *,
        normalized_content: str,
        supersedes_id: int | None,
        recorded_at: datetime | None = None,
        session: AsyncSession,
    ) -> MemoryFactModel:
        now = recorded_at or datetime.now(UTC)
        if fact.subject_user_id:
            await _ensure_person(session, fact.subject_user_id, now=now)
        if fact.group_id:
            await _ensure_group(session, fact.group_id, now=now)
        if fact.subject_user_id and fact.group_id:
            membership = await session.get(
                MembershipModel,
                {"user_id": fact.subject_user_id, "group_id": fact.group_id},
            )
            if membership is None:
                session.add(
                    MembershipModel(
                        user_id=fact.subject_user_id,
                        group_id=fact.group_id,
                        group_card="",
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
        row = MemoryFactModel(
            scope_type=fact.scope_type.value,
            subject_user_id=fact.subject_user_id,
            group_id=fact.group_id,
            visibility_type=(fact.visibility_type.value if fact.visibility_type else None),
            visibility_user_id=fact.visibility_user_id,
            visibility_group_id=fact.visibility_group_id,
            kind=fact.kind.value,
            memory_key=fact.memory_key,
            category=fact.category,
            content=fact.content,
            normalized_content=normalized_content,
            importance=fact.importance,
            confidence=fact.confidence,
            source_type=fact.source_type.value,
            authority=fact.authority.value,
            status=fact.status.value,
            conflict_state=fact.conflict_state.value,
            supersedes_id=supersedes_id,
            valid_from=fact.valid_from,
            valid_until=fact.valid_until,
            created_at=now,
            updated_at=now,
            last_confirmed_at=now,
            invalidated_reason=(
                fact.invalidated_reason.value if fact.invalidated_reason is not None else None
            ),
            last_used_at=None,
            validation_version=fact.validation_version,
            last_audited_at=fact.last_audited_at,
            review_state=fact.review_state.value,
        )
        session.add(row)
        await session.flush()
        return row

    async def transition(
        self,
        fact_id: int,
        *,
        status: MemoryStatus,
        conflict_state: MemoryConflictState,
        invalidated_reason: MemoryInvalidationReason | None,
        action: MemoryStateAction,
        reason_code: str,
        source_event_id: int | None,
        actor_user_id: str | None,
        session: AsyncSession,
    ) -> bool:
        row = await session.get(MemoryFactModel, fact_id)
        if row is None:
            return False
        now = datetime.now(UTC)
        if actor_user_id:
            await _ensure_person(session, actor_user_id, now=now)
        before_status = row.status
        before_conflict = row.conflict_state
        row.status = status.value
        row.conflict_state = conflict_state.value
        row.invalidated_reason = invalidated_reason.value if invalidated_reason else None
        row.updated_at = now
        session.add(
            MemoryFactStateEventModel(
                fact_id=fact_id,
                action=action.value,
                from_status=before_status,
                to_status=status.value,
                from_conflict_state=before_conflict,
                to_conflict_state=conflict_state.value,
                reason_code=reason_code[:64],
                source_event_id=source_event_id,
                actor_user_id=actor_user_id,
                created_at=now,
            )
        )
        await session.flush()
        return True

    async def record_created(
        self,
        fact_id: int,
        *,
        status: MemoryStatus,
        conflict_state: MemoryConflictState,
        reason_code: str,
        source_event_id: int | None,
        actor_user_id: str | None,
        session: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)
        if actor_user_id:
            await _ensure_person(session, actor_user_id, now=now)
        session.add(
            MemoryFactStateEventModel(
                fact_id=fact_id,
                action=MemoryStateAction.CREATED.value,
                from_status=None,
                to_status=status.value,
                from_conflict_state=None,
                to_conflict_state=conflict_state.value,
                reason_code=reason_code[:64],
                source_event_id=source_event_id,
                actor_user_id=actor_user_id,
                created_at=now,
            )
        )
        await session.flush()

    async def update_confirmation_metadata(
        self,
        fact_id: int,
        *,
        authority: str,
        confidence: float,
        confirmed_at: datetime,
        session: AsyncSession,
    ) -> None:
        current = await session.get(MemoryFactModel, fact_id)
        if current is None:
            return
        previous = current.last_confirmed_at
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        if confirmed_at.tzinfo is None:
            confirmed_at = confirmed_at.replace(tzinfo=UTC)
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == fact_id)
            .values(
                authority=authority,
                confidence=confidence,
                last_confirmed_at=max(previous, confirmed_at),
                updated_at=datetime.now(UTC),
            )
        )

    async def restore_confirmation_metadata(
        self,
        fact_id: int,
        *,
        authority: str,
        confidence: float,
        last_confirmed_at: datetime,
        session: AsyncSession,
    ) -> None:
        """Restore exact aggregate fields captured by a reversible internal operation."""

        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == fact_id)
            .values(
                authority=authority,
                confidence=confidence,
                last_confirmed_at=last_confirmed_at,
                updated_at=datetime.now(UTC),
            )
        )

    async def add_relation(
        self,
        *,
        source_fact_id: int,
        target_fact_id: int,
        relation_type: MemoryFactRelationType,
        confidence: float,
        source_event_id: int | None,
        session: AsyncSession,
    ) -> bool:
        statement = insert(MemoryFactRelationModel).values(
            source_fact_id=source_fact_id,
            target_fact_id=target_fact_id,
            relation_type=relation_type.value,
            confidence=confidence,
            source_event_id=source_event_id,
            created_at=datetime.now(UTC),
        )
        result = await session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[
                    MemoryFactRelationModel.source_fact_id,
                    MemoryFactRelationModel.target_fact_id,
                    MemoryFactRelationModel.relation_type,
                ]
            )
        )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def refresh_fact(
        self,
        fact_id: int,
        *,
        importance: int,
        confidence: float,
        session: AsyncSession,
    ) -> None:
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == fact_id)
            .values(
                importance=func.max(MemoryFactModel.importance, importance),
                confidence=func.max(MemoryFactModel.confidence, confidence),
                updated_at=datetime.now(UTC),
            )
        )

    async def add_evidence(
        self,
        fact_id: int,
        evidence: MemoryEvidenceCreate,
        *,
        session: AsyncSession,
    ) -> bool:
        reflection_authority = evidence.authority is MemoryAuthority.AGENT_REFLECTION
        reflection_relation = evidence.relation.value == "agent_reflection"
        if reflection_authority != reflection_relation:
            raise ValueError("agent reflection evidence relation and authority must match")
        if reflection_authority:
            scope_type = await session.scalar(
                select(MemoryFactModel.scope_type).where(MemoryFactModel.id == fact_id)
            )
            if scope_type != MemoryScopeType.SELF.value:
                raise ValueError("agent reflection evidence is only valid for self memory")
        statement = insert(MemoryEvidenceModel).values(
            fact_id=fact_id,
            event_id=evidence.event_id,
            tool_receipt_id=evidence.tool_receipt_id,
            source_speaker_user_id=evidence.source_speaker_user_id,
            relation=evidence.relation.value,
            confidence=evidence.confidence,
            authority=evidence.authority.value,
            excerpt=evidence.excerpt[:500],
            created_at=datetime.now(UTC),
        )
        index_elements = (
            [MemoryEvidenceModel.fact_id, MemoryEvidenceModel.event_id]
            if evidence.event_id is not None
            else [MemoryEvidenceModel.fact_id, MemoryEvidenceModel.tool_receipt_id]
        )
        result = await session.execute(
            statement.on_conflict_do_nothing(index_elements=index_elements)
        )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def list_evidence(
        self,
        fact_id: int,
        *,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryEvidence, ...]:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_evidence(fact_id, limit=limit, session=owned)
        rows = (
            await session.scalars(
                select(MemoryEvidenceModel)
                .where(MemoryEvidenceModel.fact_id == fact_id)
                .order_by(MemoryEvidenceModel.created_at.desc())
                .limit(max(1, limit))
            )
        ).all()
        return tuple(
            MemoryEvidence(
                id=row.id,
                fact_id=row.fact_id,
                event_id=row.event_id,
                tool_receipt_id=row.tool_receipt_id,
                source_speaker_user_id=row.source_speaker_user_id,
                relation=row.relation,
                confidence=row.confidence,
                authority=row.authority,
                excerpt=row.excerpt,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def list_relations(
        self,
        fact_id: int,
        *,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFactRelation, ...]:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_relations(fact_id, session=owned)
        rows = (
            await session.scalars(
                select(MemoryFactRelationModel)
                .where(
                    or_(
                        MemoryFactRelationModel.source_fact_id == fact_id,
                        MemoryFactRelationModel.target_fact_id == fact_id,
                    )
                )
                .order_by(MemoryFactRelationModel.created_at, MemoryFactRelationModel.id)
            )
        ).all()
        return tuple(
            MemoryFactRelation(
                id=row.id,
                source_fact_id=row.source_fact_id,
                target_fact_id=row.target_fact_id,
                relation_type=row.relation_type,
                confidence=row.confidence,
                source_event_id=row.source_event_id,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def list_state_events(self, fact_id: int) -> tuple[MemoryFactStateEvent, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(MemoryFactStateEventModel)
                    .where(MemoryFactStateEventModel.fact_id == fact_id)
                    .order_by(
                        MemoryFactStateEventModel.created_at,
                        MemoryFactStateEventModel.id,
                    )
                )
            ).all()
        return tuple(
            MemoryFactStateEvent(
                id=row.id,
                fact_id=row.fact_id,
                action=row.action,
                from_status=row.from_status,
                to_status=row.to_status,
                from_conflict_state=row.from_conflict_state,
                to_conflict_state=row.to_conflict_state,
                reason_code=row.reason_code,
                source_event_id=row.source_event_id,
                actor_user_id=row.actor_user_id,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def list_conflicts(
        self,
        *,
        scope_type: str | None = None,
        subject_user_id: str | None = None,
        group_id: str | None = None,
        limit: int = 100,
    ) -> tuple[MemoryFact, ...]:
        conditions = [
            or_(
                MemoryFactModel.status == MemoryStatus.CONTESTED.value,
                MemoryFactModel.conflict_state == MemoryConflictState.CONTESTED.value,
            )
        ]
        if scope_type is not None:
            conditions.append(MemoryFactModel.scope_type == scope_type)
        if subject_user_id is not None:
            conditions.append(MemoryFactModel.subject_user_id == subject_user_id)
        if group_id is not None:
            conditions.append(MemoryFactModel.group_id == group_id)
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
                    .outerjoin(
                        MemoryEvidenceModel,
                        MemoryEvidenceModel.fact_id == MemoryFactModel.id,
                    )
                    .where(*conditions)
                    .group_by(MemoryFactModel.id)
                    .order_by(MemoryFactModel.updated_at.desc(), MemoryFactModel.id)
                    .limit(max(1, limit))
                )
            ).all()
        return tuple(self._project_fact(row, int(count)) for row, count in rows)

    async def list_lifecycle_candidates(
        self,
        *,
        now: datetime,
        automatic_cutoff: datetime,
        third_party_cutoff: datetime,
        contested_cutoff: datetime,
        max_importance: int,
        max_confidence: float,
        limit: int,
    ) -> tuple[MemoryFact, ...]:
        stale_window = or_(
            (
                (MemoryFactModel.authority == "third_party")
                & (MemoryFactModel.last_confirmed_at <= third_party_cutoff)
            ),
            (
                (MemoryFactModel.status == MemoryStatus.CONTESTED.value)
                & (MemoryFactModel.last_confirmed_at <= contested_cutoff)
            ),
            (
                (MemoryFactModel.authority != "third_party")
                & (MemoryFactModel.status != MemoryStatus.CONTESTED.value)
                & (MemoryFactModel.last_confirmed_at <= automatic_cutoff)
            ),
        )
        conditions = [
            MemoryFactModel.status.in_((MemoryStatus.ACTIVE.value, MemoryStatus.CONTESTED.value)),
            MemoryFactModel.review_state != "quarantined",
            or_(
                MemoryFactModel.valid_until <= now,
                and_(
                    MemoryFactModel.source_type != "explicit",
                    MemoryFactModel.authority != "explicit",
                    MemoryFactModel.scope_type != MemoryScopeType.SELF.value,
                    MemoryFactModel.source_type == "automatic",
                    MemoryFactModel.importance <= max_importance,
                    MemoryFactModel.confidence <= max_confidence,
                    stale_window,
                ),
            ),
        ]
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
                    .outerjoin(
                        MemoryEvidenceModel,
                        MemoryEvidenceModel.fact_id == MemoryFactModel.id,
                    )
                    .where(*conditions)
                    .group_by(MemoryFactModel.id)
                    .order_by(MemoryFactModel.valid_until.asc(), MemoryFactModel.id)
                    .limit(max(1, limit))
                )
            ).all()
        return tuple(self._project_fact(row, int(count)) for row, count in rows)

    async def count_active(
        self,
        query: MemoryFactQuery,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        return len(await self.list_facts(query, limit=100_000, session=session))

    async def make_room(
        self,
        query: MemoryFactQuery,
        *,
        limit: int,
        session: AsyncSession,
    ) -> bool:
        """Invalidate the least useful automatic fact when a scope is full."""

        if await self.count_active(query, session=session) < max(1, limit):
            return True
        conditions = [
            MemoryFactModel.scope_type == query.scope_type.value,
            MemoryFactModel.status == MemoryStatus.ACTIVE.value,
            MemoryFactModel.source_type != "explicit",
        ]
        conditions.append(
            MemoryFactModel.subject_user_id.is_(None)
            if query.subject_user_id is None
            else MemoryFactModel.subject_user_id == query.subject_user_id
        )
        conditions.append(
            MemoryFactModel.group_id.is_(None)
            if query.group_id is None
            else MemoryFactModel.group_id == query.group_id
        )
        row = await session.scalar(
            select(MemoryFactModel)
            .where(*conditions)
            .order_by(MemoryFactModel.importance.asc(), MemoryFactModel.updated_at.asc())
            .limit(1)
        )
        if row is None:
            return False
        prior_conflict = row.conflict_state
        row.status = MemoryStatus.INVALIDATED.value
        row.conflict_state = MemoryConflictState.CLEAR.value
        row.invalidated_reason = MemoryInvalidationReason.STALE.value
        row.updated_at = datetime.now(UTC)
        session.add(
            MemoryFactStateEventModel(
                fact_id=row.id,
                action=MemoryStateAction.STALE_INVALIDATED.value,
                from_status=MemoryStatus.ACTIVE.value,
                to_status=MemoryStatus.INVALIDATED.value,
                from_conflict_state=prior_conflict,
                to_conflict_state=MemoryConflictState.CLEAR.value,
                reason_code="capacity_retention",
                source_event_id=None,
                actor_user_id=None,
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()
        return True

    async def delete_orphaned_automatic_facts(
        self,
        *,
        event_ids: tuple[int, ...],
        exact_text: str,
        session: AsyncSession,
    ) -> None:
        evidence_fact_ids = select(MemoryEvidenceModel.fact_id).where(
            MemoryEvidenceModel.event_id.in_(event_ids)
        )
        await session.execute(
            delete(MemoryFactModel).where(
                MemoryFactModel.source_type != "explicit",
                or_(
                    MemoryFactModel.content.contains(exact_text),
                    MemoryFactModel.id.in_(evidence_fact_ids),
                ),
            )
        )

    @staticmethod
    def _project_fact(row: MemoryFactModel, evidence_count: int = 0) -> MemoryFact:
        return MemoryFact(
            id=row.id,
            scope_type=row.scope_type,
            subject_user_id=row.subject_user_id,
            group_id=row.group_id,
            visibility_type=row.visibility_type,
            visibility_user_id=row.visibility_user_id,
            visibility_group_id=row.visibility_group_id,
            kind=row.kind,
            memory_key=row.memory_key,
            category=row.category,
            content=row.content,
            normalized_content=row.normalized_content,
            importance=row.importance,
            confidence=row.confidence,
            source_type=row.source_type,
            authority=row.authority,
            status=row.status,
            conflict_state=row.conflict_state,
            supersedes_id=row.supersedes_id,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_confirmed_at=row.last_confirmed_at,
            invalidated_reason=row.invalidated_reason,
            last_used_at=row.last_used_at,
            evidence_count=evidence_count,
            validation_version=row.validation_version,
            last_audited_at=row.last_audited_at,
            review_state=row.review_state,
        )

    @staticmethod
    def _target_conditions(target: MemoryEntityTarget) -> tuple[Any, ...]:
        conditions: list[Any] = [
            MemoryFactModel.scope_type == target.scope_type.value,
            (
                MemoryFactModel.subject_user_id.is_(None)
                if target.subject_user_id is None
                else MemoryFactModel.subject_user_id == target.subject_user_id
            ),
            (
                MemoryFactModel.group_id.is_(None)
                if target.group_id is None
                else MemoryFactModel.group_id == target.group_id
            ),
        ]
        if target.scope_type is MemoryScopeType.SELF:
            current_visibility = and_(
                MemoryFactModel.visibility_type
                == (target.visibility_type.value if target.visibility_type else ""),
                (
                    MemoryFactModel.visibility_user_id.is_(None)
                    if target.visibility_user_id is None
                    else MemoryFactModel.visibility_user_id == target.visibility_user_id
                ),
                (
                    MemoryFactModel.visibility_group_id.is_(None)
                    if target.visibility_group_id is None
                    else MemoryFactModel.visibility_group_id == target.visibility_group_id
                ),
            )
            conditions.append(or_(MemoryFactModel.visibility_type == "global", current_visibility))
        else:
            conditions.extend(
                (
                    MemoryFactModel.visibility_type.is_(None),
                    MemoryFactModel.visibility_user_id.is_(None),
                    MemoryFactModel.visibility_group_id.is_(None),
                )
            )
        return tuple(conditions)

    @staticmethod
    def _exact_visibility_conditions(
        target: MemoryFactCreate | MemoryFactQuery,
    ) -> tuple[Any, ...]:
        return (
            (
                MemoryFactModel.visibility_type.is_(None)
                if target.visibility_type is None
                else MemoryFactModel.visibility_type == target.visibility_type.value
            ),
            (
                MemoryFactModel.visibility_user_id.is_(None)
                if target.visibility_user_id is None
                else MemoryFactModel.visibility_user_id == target.visibility_user_id
            ),
            (
                MemoryFactModel.visibility_group_id.is_(None)
                if target.visibility_group_id is None
                else MemoryFactModel.visibility_group_id == target.visibility_group_id
            ),
        )


class MemoryJobRepository:
    """Durable one-event extraction queue with bounded retries."""

    def __init__(
        self,
        database: Database,
        *,
        eligibility: MemoryEventEligibilityPolicy | None = None,
    ) -> None:
        self._database = database
        self._eligibility = eligibility or MemoryEventEligibilityPolicy()

    async def enqueue(self, event_id: int, conversation_key: str) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            event = await session.get(ChatEventModel, event_id)
            if event is None:
                logger.warning(
                    "memory_job_enqueue_skipped event_id=%d reason=event_not_found",
                    event_id,
                )
                return False
            sender = await session.get(PersonModel, event.sender_user_id)
            if sender is None:
                logger.warning(
                    "memory_job_enqueue_skipped event_id=%d reason=sender_not_found",
                    event_id,
                )
                return False
            rejection_reason = self._eligibility.rejection_reason(
                _event_record(event),
                sender_is_bot=sender.is_bot,
            )
            if rejection_reason is not None:
                logger.info(
                    "memory_job_enqueue_skipped event_id=%d reason=%s",
                    event_id,
                    rejection_reason,
                )
                return False
            statement = insert(MemoryJobModel).values(
                event_id=event_id,
                conversation_key=conversation_key[:255],
                status=MemoryJobStatus.PENDING.value,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
                error_category=None,
                processing_source=MemoryProcessingSource.LIVE.value,
                outcome=None,
                completed_at=None,
            )
            result = await session.execute(
                statement.on_conflict_do_nothing(index_elements=[MemoryJobModel.event_id])
            )
            created = bool(cast(CursorResult[Any], result).rowcount)
            if not created:
                logger.debug(
                    "memory_job_enqueue_skipped event_id=%d reason=already_enqueued",
                    event_id,
                )
            return created

    async def pending_count(self) -> int:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(MemoryJobModel)
                .where(
                    MemoryJobModel.status == MemoryJobStatus.PENDING.value,
                    MemoryJobModel.next_attempt_at <= datetime.now(UTC),
                )
            )
        return int(value or 0)

    async def claim(self, *, limit: int = 20) -> tuple[MemoryJob, ...]:
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=5)
        async with self._database.sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(MemoryJobModel)
                    .where(
                        or_(
                            MemoryJobModel.status == MemoryJobStatus.PENDING.value,
                            (
                                (MemoryJobModel.status == MemoryJobStatus.PROCESSING.value)
                                & (MemoryJobModel.updated_at <= stale)
                            ),
                        ),
                        MemoryJobModel.next_attempt_at <= now,
                    )
                    .order_by(MemoryJobModel.id)
                    .limit(max(1, limit))
                )
            ).all()
            jobs: list[MemoryJob] = []
            for row in rows:
                event = await session.get(ChatEventModel, row.event_id)
                if event is None:
                    await session.delete(row)
                    continue
                row.status = MemoryJobStatus.PROCESSING.value
                row.updated_at = now
                jobs.append(
                    MemoryJob(
                        id=row.id,
                        event_id=row.event_id,
                        conversation_key=row.conversation_key,
                        status=row.status,
                        attempts=row.attempts,
                        next_attempt_at=row.next_attempt_at,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                        error_category=row.error_category,
                        processing_source=row.processing_source,
                        rebuild_run_id=row.rebuild_run_id,
                        outcome=row.outcome,
                        completed_at=row.completed_at,
                        event=_event_record(event),
                    )
                )
            return tuple(jobs)

    async def claim_ready_batch(
        self,
        *,
        limit: int,
        trigger_count: int,
        max_characters: int,
        max_wait_seconds: float,
        now: datetime | None = None,
    ) -> tuple[MemoryJob, ...]:
        """Claim one ready conversation batch without mixing conversation scopes."""

        claimed_at = now or datetime.now(UTC)
        stale = claimed_at - timedelta(minutes=5)
        oldest_ready = claimed_at - timedelta(seconds=max_wait_seconds)
        eligible = and_(
            or_(
                MemoryJobModel.status == MemoryJobStatus.PENDING.value,
                (
                    (MemoryJobModel.status == MemoryJobStatus.PROCESSING.value)
                    & (MemoryJobModel.updated_at <= stale)
                ),
            ),
            MemoryJobModel.next_attempt_at <= claimed_at,
        )
        job_count = func.count(MemoryJobModel.id)
        character_count = func.coalesce(func.sum(func.length(ChatEventModel.content)), 0)
        oldest_job = func.min(MemoryJobModel.created_at)
        first_job_id = func.min(MemoryJobModel.id)
        async with self._database.sessions() as session, session.begin():
            ready = (
                await session.execute(
                    select(
                        MemoryJobModel.conversation_key,
                        first_job_id.label("first_job_id"),
                    )
                    .join(ChatEventModel, ChatEventModel.id == MemoryJobModel.event_id)
                    .where(eligible)
                    .group_by(MemoryJobModel.conversation_key)
                    .having(
                        or_(
                            job_count >= max(1, trigger_count),
                            character_count >= max(1, max_characters),
                            oldest_job <= oldest_ready,
                        )
                    )
                    .order_by(first_job_id)
                    .limit(1)
                )
            ).first()
            if ready is None:
                return ()
            conversation_key = str(ready[0])
            rows = (
                await session.scalars(
                    select(MemoryJobModel)
                    .where(eligible, MemoryJobModel.conversation_key == conversation_key)
                    .order_by(MemoryJobModel.id)
                    .limit(max(1, limit))
                )
            ).all()
            jobs: list[MemoryJob] = []
            characters = 0
            for row in rows:
                event = await session.get(ChatEventModel, row.event_id)
                if event is None:
                    await session.delete(row)
                    continue
                event_characters = len(event.content)
                if jobs and characters + event_characters > max(1, max_characters):
                    break
                characters += event_characters
                row.status = MemoryJobStatus.PROCESSING.value
                row.updated_at = claimed_at
                jobs.append(
                    MemoryJob(
                        id=row.id,
                        event_id=row.event_id,
                        conversation_key=row.conversation_key,
                        status=row.status,
                        attempts=row.attempts,
                        next_attempt_at=row.next_attempt_at,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                        error_category=row.error_category,
                        processing_source=row.processing_source,
                        rebuild_run_id=row.rebuild_run_id,
                        outcome=row.outcome,
                        completed_at=row.completed_at,
                        event=_event_record(event),
                    )
                )
            return tuple(jobs)

    async def complete(
        self,
        job_id: int,
        *,
        outcome: MemoryRebuildJobOutcome = MemoryRebuildJobOutcome.CLAIMS_APPLIED,
        result_category: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryJobModel)
                .where(MemoryJobModel.id == job_id)
                .values(
                    status=MemoryJobStatus.DONE.value,
                    updated_at=now,
                    error_category=(result_category[:64] if result_category else None),
                    outcome=outcome.value,
                    completed_at=now,
                )
            )

    async def fail(self, job_id: int, error_category: str) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(MemoryJobModel, job_id)
            if row is None:
                return
            row.attempts += 1
            row.status = (
                MemoryJobStatus.FAILED.value if row.attempts >= 3 else MemoryJobStatus.PENDING.value
            )
            row.next_attempt_at = now + timedelta(seconds=30 * row.attempts)
            row.updated_at = now
            row.error_category = error_category[:64]
