"""Transactional Memory V2 fact lifecycle rules."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryKind,
    MemoryResolutionAction,
    MemoryScopeType,
    MemorySourceType,
    MemoryStateAction,
    MemoryStatus,
)
from qq_ai_bot.memory.evidence import MemoryEvidencePolicy, MemoryEvidenceWeights
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import (
    MemoryCandidate,
    MemoryEvidence,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactCreate,
    MemoryFactQuery,
    MemoryResolutionPlan,
)
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.validation import ValidatedMemoryClaim, normalize_memory_text

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from qq_ai_bot.admin.config_service import RuntimeConfigService


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class MemoryEmbeddingScheduler(Protocol):
    async def schedule(self, fact_id: int) -> None: ...


class MemoryFactService:
    """Apply deduplication, versioning, explicit protection, and evidence atomically."""

    def __init__(
        self,
        repository: MemoryFactRepository,
        *,
        embedding_scheduler: MemoryEmbeddingScheduler | None = None,
        evidence_policy: MemoryEvidencePolicy | None = None,
        runtime_config: RuntimeConfigService | None = None,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._repository = repository
        self._embedding_scheduler = embedding_scheduler
        self._evidence_policy = evidence_policy or MemoryEvidencePolicy()
        self._runtime_config = runtime_config
        self.metrics = metrics or MemoryLifecycleMetrics()

    def set_embedding_scheduler(self, scheduler: MemoryEmbeddingScheduler | None) -> None:
        self._embedding_scheduler = scheduler

    async def schedule_embedding(self, fact_id: int) -> None:
        if self._embedding_scheduler is None:
            return
        try:
            await self._embedding_scheduler.schedule(fact_id)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "memory_embedding_schedule_failed fact_id=%d error_category=%s",
                fact_id,
                type(exc).__name__,
            )

    @property
    def repository(self) -> MemoryFactRepository:
        return self._repository

    async def remember(
        self,
        fact: MemoryFactCreate,
        *,
        evidence: MemoryEvidenceCreate | None = None,
        limit: int | None = None,
        session: AsyncSession | None = None,
    ) -> MemoryFact:
        if session is None:
            async with self._repository.transaction() as owned:
                owned_result = await self.remember(
                    fact,
                    evidence=evidence,
                    limit=limit,
                    session=owned,
                )
            await self.schedule_embedding(owned_result.id)
            return owned_result
        content = normalize_memory_text(fact.content, maximum=4000)
        key = normalize_memory_text(fact.memory_key, maximum=128)
        category = normalize_memory_text(fact.category, maximum=64)
        if not content or not key or not category:
            raise ValueError("memory fact content, key, and category cannot be empty")
        normalized = content.casefold()
        prepared = fact.model_copy(
            update={"content": content, "memory_key": key, "category": category}
        )
        existing = await self._repository.find_active(prepared, session=session)
        if existing is not None and existing.normalized_content == normalized:
            if evidence is not None:
                added = await self._repository.add_evidence(existing.id, evidence, session=session)
                if added:
                    self.metrics.increment("evidence_added")
                    self.metrics.increment("facts_confirmed")
                    await self._refresh_evidence(
                        existing.id, confirmed_at=datetime.now(UTC), session=session
                    )
            else:
                await self._repository.refresh_fact(
                    existing.id,
                    importance=prepared.importance,
                    confidence=prepared.confidence,
                    session=session,
                )
            repeated = await self._repository.get_fact(existing.id, session=session)
            assert repeated is not None
            return repeated
        if (
            existing is not None
            and existing.source_type == MemorySourceType.EXPLICIT.value
            and prepared.source_type is not MemorySourceType.EXPLICIT
        ):
            protected = await self._repository.get_fact(existing.id, session=session)
            assert protected is not None
            return protected
        if existing is None and limit is not None:
            query = MemoryFactQuery(
                scope_type=prepared.scope_type,
                subject_user_id=prepared.subject_user_id,
                group_id=prepared.group_id,
                visibility_type=prepared.visibility_type,
                visibility_user_id=prepared.visibility_user_id,
                visibility_group_id=prepared.visibility_group_id,
            )
            if not await self._repository.make_room(query, limit=limit, session=session):
                raise ValueError("memory capacity is occupied by explicit facts")
        supersedes_id = existing.id if existing is not None else None
        if existing is not None:
            await self._repository.transition(
                existing.id,
                status=MemoryStatus.SUPERSEDED,
                conflict_state=MemoryConflictState.CLEAR,
                invalidated_reason=None,
                action=MemoryStateAction.SUPERSEDED,
                reason_code="legacy_key_replacement",
                source_event_id=evidence.event_id if evidence else None,
                actor_user_id=evidence.source_speaker_user_id if evidence else None,
                session=session,
            )
        created = await self._repository.create_fact(
            prepared,
            normalized_content=normalized,
            supersedes_id=supersedes_id,
            session=session,
        )
        if evidence is not None:
            added = await self._repository.add_evidence(created.id, evidence, session=session)
            if added:
                self.metrics.increment("evidence_added")
            await self._refresh_evidence(
                created.id, confirmed_at=datetime.now(UTC), session=session
            )
        await self._repository.record_created(
            created.id,
            status=prepared.status,
            conflict_state=prepared.conflict_state,
            reason_code="legacy_remember",
            source_event_id=evidence.event_id if evidence else None,
            actor_user_id=evidence.source_speaker_user_id if evidence else None,
            session=session,
        )
        projected = await self._repository.get_fact(created.id, session=session)
        assert projected is not None
        self.metrics.increment("facts_created")
        return projected

    async def apply_claim(
        self,
        claim: ValidatedMemoryClaim,
        *,
        candidates: tuple[MemoryCandidate, ...],
        plan: MemoryResolutionPlan,
        limit: int | None = None,
        session: AsyncSession | None = None,
    ) -> MemoryFact | None:
        """Apply one already-validated backend plan in one transaction."""

        if session is None:
            async with self._repository.transaction() as owned:
                result = await self.apply_claim(
                    claim,
                    candidates=candidates,
                    plan=plan,
                    limit=limit,
                    session=owned,
                )
            if result is not None and result.status is MemoryStatus.ACTIVE and plan.create_new_fact:
                await self.schedule_embedding(result.id)
            return result
        self._validate_plan(claim, candidates, plan)
        evidence = claim.evidence
        existing_id = plan.existing_fact_id
        if plan.action is MemoryResolutionAction.NOOP:
            return None
        if plan.action is MemoryResolutionAction.MERGE_EVIDENCE:
            assert existing_id is not None
            added = await self._repository.add_evidence(existing_id, evidence, session=session)
            if added:
                self.metrics.increment("evidence_added")
                self.metrics.increment("facts_confirmed")
                await self._refresh_evidence(
                    existing_id,
                    confirmed_at=claim.occurred_at,
                    session=session,
                )
                current = await self._repository.get_fact(existing_id, session=session)
                assert current is not None
                await self._repository.transition(
                    existing_id,
                    status=current.status,
                    conflict_state=current.conflict_state,
                    invalidated_reason=current.invalidated_reason,
                    action=MemoryStateAction.CONFIRMED,
                    reason_code=plan.reason_code,
                    source_event_id=evidence.event_id,
                    actor_user_id=evidence.source_speaker_user_id,
                    session=session,
                )
                if (
                    claim.subject_is_speaker
                    and current.conflict_state is MemoryConflictState.CONTESTED
                ):
                    await self._resolve_by_subject_confirmation(
                        existing_id,
                        actor_user_id=evidence.source_speaker_user_id,
                        source_event_id=evidence.event_id,
                        session=session,
                    )
            return await self._repository.get_fact(existing_id, session=session)
        if plan.action is MemoryResolutionAction.INVALIDATE:
            assert existing_id is not None
            if await self._repository.add_evidence(existing_id, evidence, session=session):
                self.metrics.increment("evidence_added")
            await self._repository.transition(
                existing_id,
                status=MemoryStatus.INVALIDATED,
                conflict_state=MemoryConflictState.CLEAR,
                invalidated_reason=MemoryInvalidationReason.USER_RETRACTED,
                action=MemoryStateAction.INVALIDATED,
                reason_code=plan.reason_code,
                source_event_id=evidence.event_id,
                actor_user_id=evidence.source_speaker_user_id,
                session=session,
            )
            self.metrics.increment("facts_invalidated")
            await self._clear_resolved_related_conflicts(existing_id, session=session)
            return await self._repository.get_fact(existing_id, session=session)

        if plan.action is MemoryResolutionAction.SUPERSEDE:
            assert existing_id is not None
            await self._repository.transition(
                existing_id,
                status=MemoryStatus.SUPERSEDED,
                conflict_state=MemoryConflictState.CLEAR,
                invalidated_reason=None,
                action=MemoryStateAction.SUPERSEDED,
                reason_code=plan.reason_code,
                source_event_id=evidence.event_id,
                actor_user_id=evidence.source_speaker_user_id,
                session=session,
            )
            self.metrics.increment("facts_superseded")

        if plan.action is MemoryResolutionAction.CONTEST and existing_id is not None:
            current = await self._repository.get_fact(existing_id, session=session)
            if current is None:
                raise ValueError("memory resolution candidate no longer exists")
            await self._repository.transition(
                existing_id,
                status=current.status,
                conflict_state=MemoryConflictState.CONTESTED,
                invalidated_reason=current.invalidated_reason,
                action=MemoryStateAction.CONTESTED,
                reason_code=plan.reason_code,
                source_event_id=evidence.event_id,
                actor_user_id=evidence.source_speaker_user_id,
                session=session,
            )
            self.metrics.increment("conflicts_open")

        if not plan.create_new_fact:
            return (
                await self._repository.get_fact(existing_id, session=session)
                if existing_id
                else None
            )
        if limit is not None:
            query = MemoryFactQuery(
                scope_type=claim.fact.scope_type,
                subject_user_id=claim.fact.subject_user_id,
                group_id=claim.fact.group_id,
                visibility_type=claim.fact.visibility_type,
                visibility_user_id=claim.fact.visibility_user_id,
                visibility_group_id=claim.fact.visibility_group_id,
            )
            if not await self._repository.make_room(query, limit=limit, session=session):
                raise ValueError("memory capacity is occupied by explicit facts")
        status = plan.new_fact_status or MemoryStatus.ACTIVE
        conflict = plan.new_conflict_state or MemoryConflictState.CLEAR
        prepared = claim.fact.model_copy(update={"status": status, "conflict_state": conflict})
        row = await self._repository.create_fact(
            prepared,
            normalized_content=normalize_memory_text(prepared.content, maximum=4000).casefold(),
            supersedes_id=(
                existing_id if plan.action is MemoryResolutionAction.SUPERSEDE else None
            ),
            recorded_at=claim.occurred_at,
            session=session,
        )
        if await self._repository.add_evidence(row.id, evidence, session=session):
            self.metrics.increment("evidence_added")
        await self._refresh_evidence(row.id, confirmed_at=claim.occurred_at, session=session)
        await self._repository.record_created(
            row.id,
            status=status,
            conflict_state=conflict,
            reason_code=plan.reason_code,
            source_event_id=evidence.event_id,
            actor_user_id=evidence.source_speaker_user_id,
            session=session,
        )
        if existing_id is not None:
            for relation in plan.relation_types:
                await self._repository.add_relation(
                    source_fact_id=row.id,
                    target_fact_id=existing_id,
                    relation_type=relation,
                    confidence=evidence.confidence,
                    source_event_id=evidence.event_id,
                    session=session,
                )
        self.metrics.increment(
            "facts_contested" if status is MemoryStatus.CONTESTED else "facts_created"
        )
        return await self._repository.get_fact(row.id, session=session)

    async def _refresh_evidence(
        self,
        fact_id: int,
        *,
        confirmed_at: datetime,
        session: AsyncSession,
    ) -> None:
        current = await self._repository.get_fact(fact_id, session=session)
        if current is None:
            return
        evidence = await self._repository.list_evidence(fact_id, limit=100_000, session=session)
        policy = await self._effective_evidence_policy(current)
        authority = policy.strongest_authority(
            (current.authority, *(row.authority for row in evidence)),
            default=current.authority,
        )
        confidence = policy.aggregate(evidence, authority=authority)
        await self._repository.update_confirmation_metadata(
            fact_id,
            authority=authority.value,
            confidence=confidence,
            confirmed_at=confirmed_at,
            session=session,
        )

    async def _effective_evidence_policy(self, fact: MemoryFact) -> MemoryEvidencePolicy:
        if self._runtime_config is None:
            return self._evidence_policy
        runtime = (
            await self._runtime_config.snapshot(
                user_id=fact.subject_user_id,
                group_id=fact.group_id,
            )
        ).memory
        return MemoryEvidencePolicy(
            MemoryEvidenceWeights(
                explicit=runtime.evidence_weight_explicit,
                self_report=runtime.evidence_weight_self,
                group_report=runtime.evidence_weight_group,
                third_party=runtime.evidence_weight_third_party,
                rebuild=runtime.evidence_weight_rebuild,
                cap_explicit=runtime.authority_cap_explicit,
                cap_self=runtime.authority_cap_self,
                cap_group=runtime.authority_cap_group,
                cap_third_party=runtime.authority_cap_third_party,
            )
        )

    @staticmethod
    def _validate_plan(
        claim: ValidatedMemoryClaim,
        candidates: tuple[MemoryCandidate, ...],
        plan: MemoryResolutionPlan,
    ) -> None:
        by_id = {candidate.fact.id: candidate.fact for candidate in candidates}
        if plan.existing_fact_id is not None and plan.existing_fact_id not in by_id:
            raise ValueError("memory resolution plan references an unbounded candidate")
        target = (
            claim.fact.scope_type,
            claim.fact.subject_user_id,
            claim.fact.group_id,
            claim.fact.visibility_type,
            claim.fact.visibility_user_id,
            claim.fact.visibility_group_id,
        )
        if any(
            (
                row.scope_type,
                row.subject_user_id,
                row.group_id,
                row.visibility_type,
                row.visibility_user_id,
                row.visibility_group_id,
            )
            != target
            for row in by_id.values()
        ):
            raise ValueError("memory resolution plan crosses identity targets")
        if (
            claim.fact.authority is MemoryAuthority.THIRD_PARTY
            and plan.action is MemoryResolutionAction.SUPERSEDE
            and plan.existing_fact_id is not None
            and by_id[plan.existing_fact_id].authority
            in {MemoryAuthority.EXPLICIT, MemoryAuthority.SELF_REPORT}
        ):
            raise ValueError("third-party memory cannot supersede self or explicit memory")

    async def list_person(
        self,
        user_id: str,
        *,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        return await self._repository.list_facts(
            MemoryFactQuery(scope_type=MemoryScopeType.PERSON, subject_user_id=user_id),
            limit=limit,
            session=session,
        )

    async def list_group(
        self,
        group_id: str,
        *,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        return await self._repository.list_facts(
            MemoryFactQuery(scope_type=MemoryScopeType.GROUP, group_id=group_id),
            limit=limit,
            session=session,
        )

    async def list_person_group(
        self,
        user_id: str,
        group_id: str,
        *,
        limit: int = 50,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        return await self._repository.list_facts(
            MemoryFactQuery(
                scope_type=MemoryScopeType.PERSON_GROUP,
                subject_user_id=user_id,
                group_id=group_id,
            ),
            limit=limit,
            session=session,
        )

    async def list_preferences(
        self,
        user_id: str,
        *,
        limit: int = 30,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        return await self._repository.list_facts(
            MemoryFactQuery(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=user_id,
                kind=MemoryKind.PREFERENCE,
            ),
            limit=limit,
            session=session,
        )

    async def count_person(
        self,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        return await self._repository.count_active(
            MemoryFactQuery(scope_type=MemoryScopeType.PERSON, subject_user_id=user_id),
            session=session,
        )

    async def add_explicit_person(
        self,
        user_id: str,
        content: str,
        *,
        limit: int,
        memory_key: str | None = None,
        evidence: MemoryEvidenceCreate | None = None,
        session: AsyncSession | None = None,
    ) -> MemoryFact:
        return await self.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=user_id,
                kind=MemoryKind.FACT,
                memory_key=memory_key or f"explicit-{uuid.uuid4()}",
                category="explicit",
                content=content,
                importance=5,
                confidence=1,
                source_type=MemorySourceType.EXPLICIT,
                authority=MemoryAuthority.EXPLICIT,
            ),
            evidence=evidence,
            limit=limit,
            session=session,
        )

    async def update_explicit_person(
        self,
        fact_id: int,
        *,
        user_id: str,
        content: str,
        session: AsyncSession | None = None,
    ) -> MemoryFact | None:
        if session is None:
            async with self._repository.transaction() as owned:
                return await self.update_explicit_person(
                    fact_id,
                    user_id=user_id,
                    content=content,
                    session=owned,
                )
        current = await self._repository.get_fact(fact_id, session=session)
        if (
            current is None
            or current.status is not MemoryStatus.ACTIVE
            or current.scope_type is not MemoryScopeType.PERSON
            or current.subject_user_id != user_id
        ):
            return None
        return await self.remember(
            MemoryFactCreate(
                scope_type=current.scope_type,
                subject_user_id=user_id,
                kind=current.kind,
                memory_key=current.memory_key,
                category=current.category,
                content=content,
                importance=current.importance,
                confidence=1,
                source_type=MemorySourceType.EXPLICIT,
                authority=MemoryAuthority.EXPLICIT,
                valid_from=current.valid_from,
                valid_until=current.valid_until,
            ),
            session=session,
        )

    async def invalidate_person(
        self,
        fact_id: int,
        *,
        user_id: str,
        session: AsyncSession | None = None,
    ) -> bool:
        if session is None:
            async with self._repository.transaction() as owned:
                return await self.invalidate_person(fact_id, user_id=user_id, session=owned)
        fact = await self._repository.get_fact(fact_id, session=session)
        if (
            fact is None
            or fact.subject_user_id != user_id
            or fact.status is MemoryStatus.INVALIDATED
        ):
            return False
        changed = await self._repository.transition(
            fact_id,
            status=MemoryStatus.INVALIDATED,
            conflict_state=MemoryConflictState.CLEAR,
            invalidated_reason=MemoryInvalidationReason.USER_RETRACTED,
            action=MemoryStateAction.INVALIDATED,
            reason_code=MemoryInvalidationReason.USER_RETRACTED.value,
            source_event_id=None,
            actor_user_id=user_id,
            session=session,
        )
        if changed:
            self.metrics.increment("facts_invalidated")
            await self._clear_resolved_related_conflicts(fact_id, session=session)
        return changed

    async def get_fact(self, fact_id: int) -> MemoryFact | None:
        return await self._repository.get_fact(fact_id)

    async def confirm_fact(
        self,
        fact_id: int,
        evidence: MemoryEvidenceCreate,
        *,
        confirmed_at: datetime | None = None,
    ) -> MemoryFact | None:
        async with self._repository.transaction() as session:
            current = await self._repository.get_fact(fact_id, session=session)
            if current is None or current.status not in {
                MemoryStatus.ACTIVE,
                MemoryStatus.CONTESTED,
            }:
                return None
            added = await self._repository.add_evidence(fact_id, evidence, session=session)
            if added:
                self.metrics.increment("evidence_added")
                self.metrics.increment("facts_confirmed")
                await self._refresh_evidence(
                    fact_id,
                    confirmed_at=confirmed_at or datetime.now(UTC),
                    session=session,
                )
                await self._repository.transition(
                    fact_id,
                    status=current.status,
                    conflict_state=current.conflict_state,
                    invalidated_reason=current.invalidated_reason,
                    action=MemoryStateAction.CONFIRMED,
                    reason_code="supporting_evidence",
                    source_event_id=evidence.event_id,
                    actor_user_id=evidence.source_speaker_user_id,
                    session=session,
                )
                if (
                    current.subject_user_id == evidence.source_speaker_user_id
                    and current.conflict_state is MemoryConflictState.CONTESTED
                ):
                    await self._resolve_by_subject_confirmation(
                        fact_id,
                        actor_user_id=evidence.source_speaker_user_id,
                        source_event_id=evidence.event_id,
                        session=session,
                    )
            return await self._repository.get_fact(fact_id, session=session)

    async def append_evidence_bundle(
        self,
        fact_id: int,
        evidence: tuple[MemoryEvidenceCreate, ...],
        *,
        confirmed_at: datetime,
        session: AsyncSession,
    ) -> int:
        """Append one trusted evidence window and refresh aggregate metadata once."""

        added = 0
        for item in evidence:
            added += int(await self._repository.add_evidence(fact_id, item, session=session))
        if added:
            self.metrics.increment("evidence_added", added)
            await self._refresh_evidence(
                fact_id,
                confirmed_at=confirmed_at,
                session=session,
            )
        return added

    async def correct_fact(
        self,
        fact_id: int,
        *,
        content: str,
        actor_user_id: str,
    ) -> MemoryFact | None:
        normalized = normalize_memory_text(content, maximum=4000)
        if not normalized:
            raise ValueError("memory correction cannot be empty")
        async with self._repository.transaction() as session:
            current = await self._repository.get_fact(fact_id, session=session)
            if current is None or current.status is not MemoryStatus.ACTIVE:
                return None
            await self._repository.transition(
                fact_id,
                status=MemoryStatus.SUPERSEDED,
                conflict_state=MemoryConflictState.CLEAR,
                invalidated_reason=None,
                action=MemoryStateAction.SUPERSEDED,
                reason_code="explicit_correction",
                source_event_id=None,
                actor_user_id=actor_user_id,
                session=session,
            )
            created = await self._repository.create_fact(
                MemoryFactCreate(
                    scope_type=current.scope_type,
                    subject_user_id=current.subject_user_id,
                    group_id=current.group_id,
                    kind=current.kind,
                    memory_key=current.memory_key,
                    category=current.category,
                    content=normalized,
                    importance=current.importance,
                    confidence=1.0,
                    source_type=MemorySourceType.EXPLICIT,
                    authority=MemoryAuthority.EXPLICIT,
                    valid_from=current.valid_from,
                    valid_until=current.valid_until,
                ),
                normalized_content=normalized.casefold(),
                supersedes_id=current.id,
                session=session,
            )
            await self._repository.record_created(
                created.id,
                status=MemoryStatus.ACTIVE,
                conflict_state=MemoryConflictState.CLEAR,
                reason_code="explicit_correction",
                source_event_id=None,
                actor_user_id=actor_user_id,
                session=session,
            )
            result = await self._repository.get_fact(created.id, session=session)
            self.metrics.increment("facts_superseded")
            self.metrics.increment("facts_created")
        if result is not None:
            await self.schedule_embedding(result.id)
        return result

    async def version_fact(
        self,
        fact_id: int,
        *,
        replacement: MemoryFactCreate,
        evidence: MemoryEvidenceCreate,
        actor_user_id: str,
        reason_code: str,
        limit: int | None,
        copy_existing_evidence: bool,
        confirmed_at: datetime,
        copied_evidence_authority: MemoryAuthority | None = None,
        session: AsyncSession | None = None,
    ) -> MemoryFact | None:
        """Atomically supersede one fact with a validated replacement version."""

        if session is None:
            async with self._repository.transaction() as owned:
                result = await self.version_fact(
                    fact_id,
                    replacement=replacement,
                    evidence=evidence,
                    actor_user_id=actor_user_id,
                    reason_code=reason_code,
                    limit=limit,
                    copy_existing_evidence=copy_existing_evidence,
                    copied_evidence_authority=copied_evidence_authority,
                    confirmed_at=confirmed_at,
                    session=owned,
                )
            if result is not None and result.status is MemoryStatus.ACTIVE:
                await self.schedule_embedding(result.id)
            return result
        current = await self._repository.get_fact(fact_id, session=session)
        if current is None or current.status not in {
            MemoryStatus.ACTIVE,
            MemoryStatus.CONTESTED,
        }:
            return None
        collision = await self._repository.find_active(replacement, session=session)
        if collision is not None and collision.id != current.id:
            raise ValueError("memory replacement target already has an active fact for this key")
        old_target = (
            current.scope_type,
            current.subject_user_id,
            current.group_id,
            current.visibility_type,
            current.visibility_user_id,
            current.visibility_group_id,
        )
        new_target = (
            replacement.scope_type,
            replacement.subject_user_id,
            replacement.group_id,
            replacement.visibility_type,
            replacement.visibility_user_id,
            replacement.visibility_group_id,
        )
        if old_target != new_target and limit is not None:
            query = MemoryFactQuery(
                scope_type=replacement.scope_type,
                subject_user_id=replacement.subject_user_id,
                group_id=replacement.group_id,
                visibility_type=replacement.visibility_type,
                visibility_user_id=replacement.visibility_user_id,
                visibility_group_id=replacement.visibility_group_id,
            )
            if not await self._repository.make_room(query, limit=limit, session=session):
                raise ValueError("memory capacity is occupied by explicit facts")
        await self._repository.transition(
            fact_id,
            status=MemoryStatus.SUPERSEDED,
            conflict_state=MemoryConflictState.CLEAR,
            invalidated_reason=None,
            action=MemoryStateAction.SUPERSEDED,
            reason_code=reason_code,
            source_event_id=evidence.event_id,
            actor_user_id=actor_user_id,
            session=session,
        )
        created = await self._repository.create_fact(
            replacement.model_copy(
                update={
                    "status": MemoryStatus.ACTIVE,
                    "conflict_state": MemoryConflictState.CLEAR,
                    "invalidated_reason": None,
                }
            ),
            normalized_content=normalize_memory_text(replacement.content, maximum=4000).casefold(),
            supersedes_id=current.id,
            recorded_at=confirmed_at,
            session=session,
        )
        if copy_existing_evidence:
            for row in await self._repository.list_evidence(
                fact_id, limit=100_000, session=session
            ):
                await self._repository.add_evidence(
                    created.id,
                    MemoryEvidenceCreate(
                        event_id=row.event_id,
                        source_speaker_user_id=row.source_speaker_user_id,
                        relation=(
                            MemoryEvidenceRelation.THIRD_PARTY_STATEMENT
                            if copied_evidence_authority is MemoryAuthority.THIRD_PARTY
                            else row.relation
                        ),
                        confidence=row.confidence,
                        authority=copied_evidence_authority or row.authority,
                        excerpt=row.excerpt,
                    ),
                    session=session,
                )
        await self._repository.add_evidence(created.id, evidence, session=session)
        await self._refresh_evidence(
            created.id,
            confirmed_at=confirmed_at,
            session=session,
        )
        await self._repository.record_created(
            created.id,
            status=MemoryStatus.ACTIVE,
            conflict_state=MemoryConflictState.CLEAR,
            reason_code=reason_code,
            source_event_id=evidence.event_id,
            actor_user_id=actor_user_id,
            session=session,
        )
        self.metrics.increment("facts_superseded")
        self.metrics.increment("facts_created")
        return await self._repository.get_fact(created.id, session=session)

    async def invalidate_fact(
        self,
        fact_id: int,
        *,
        reason: MemoryInvalidationReason,
        actor_user_id: str | None,
        evidence: MemoryEvidenceCreate | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        if session is None:
            async with self._repository.transaction() as owned:
                return await self.invalidate_fact(
                    fact_id,
                    reason=reason,
                    actor_user_id=actor_user_id,
                    evidence=evidence,
                    session=owned,
                )
        current = await self._repository.get_fact(fact_id, session=session)
        if current is None or current.status is MemoryStatus.INVALIDATED:
            return False
        if evidence is not None:
            await self._repository.add_evidence(fact_id, evidence, session=session)
        changed = await self._repository.transition(
            fact_id,
            status=MemoryStatus.INVALIDATED,
            conflict_state=MemoryConflictState.CLEAR,
            invalidated_reason=reason,
            action=(
                MemoryStateAction.EXPIRED
                if reason is MemoryInvalidationReason.EXPIRED
                else MemoryStateAction.STALE_INVALIDATED
                if reason is MemoryInvalidationReason.STALE
                else MemoryStateAction.INVALIDATED
            ),
            reason_code=reason.value,
            source_event_id=evidence.event_id if evidence is not None else None,
            actor_user_id=actor_user_id,
            session=session,
        )
        if changed:
            self.metrics.increment("facts_invalidated")
            await self._clear_resolved_related_conflicts(fact_id, session=session)
        return changed

    async def retract_fact(self, fact_id: int, *, actor_user_id: str) -> bool:
        """Retract a fact without deleting its version, evidence, or audit history."""

        return await self.invalidate_fact(
            fact_id,
            reason=MemoryInvalidationReason.USER_RETRACTED,
            actor_user_id=actor_user_id,
        )

    async def contest_fact(
        self,
        fact_id: int,
        *,
        reason_code: str,
        actor_user_id: str | None = None,
        evidence: MemoryEvidenceCreate | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        if session is None:
            async with self._repository.transaction() as owned:
                return await self.contest_fact(
                    fact_id,
                    reason_code=reason_code,
                    actor_user_id=actor_user_id,
                    evidence=evidence,
                    session=owned,
                )
        current = await self._repository.get_fact(fact_id, session=session)
        if current is None or current.status is not MemoryStatus.ACTIVE:
            return False
        if evidence is not None:
            await self._repository.add_evidence(fact_id, evidence, session=session)
        changed = await self._repository.transition(
            fact_id,
            status=current.status,
            conflict_state=MemoryConflictState.CONTESTED,
            invalidated_reason=None,
            action=MemoryStateAction.CONTESTED,
            reason_code=reason_code,
            source_event_id=evidence.event_id if evidence is not None else None,
            actor_user_id=actor_user_id,
            session=session,
        )
        if changed:
            self.metrics.increment("facts_contested")
            self.metrics.increment("conflicts_open")
        return changed

    async def clear_conflict(
        self,
        fact_id: int,
        *,
        reason_code: str,
        actor_user_id: str | None = None,
    ) -> bool:
        async with self._repository.transaction() as session:
            current = await self._repository.get_fact(fact_id, session=session)
            if (
                current is None
                or current.status is MemoryStatus.CONTESTED
                or current.conflict_state is MemoryConflictState.CLEAR
            ):
                return False
            changed = await self._repository.transition(
                fact_id,
                status=current.status,
                conflict_state=MemoryConflictState.CLEAR,
                invalidated_reason=current.invalidated_reason,
                action=MemoryStateAction.CONFLICT_CLEARED,
                reason_code=reason_code,
                source_event_id=None,
                actor_user_id=actor_user_id,
                session=session,
            )
            if changed:
                self.metrics.increment("conflicts_cleared")
            return changed

    async def _clear_conflict_in_session(
        self,
        fact_id: int,
        *,
        reason_code: str,
        actor_user_id: str | None,
        session: AsyncSession,
    ) -> bool:
        current = await self._repository.get_fact(fact_id, session=session)
        if (
            current is None
            or current.status is MemoryStatus.CONTESTED
            or current.conflict_state is MemoryConflictState.CLEAR
        ):
            return False
        changed = await self._repository.transition(
            fact_id,
            status=current.status,
            conflict_state=MemoryConflictState.CLEAR,
            invalidated_reason=current.invalidated_reason,
            action=MemoryStateAction.CONFLICT_CLEARED,
            reason_code=reason_code,
            source_event_id=None,
            actor_user_id=actor_user_id,
            session=session,
        )
        if changed:
            self.metrics.increment("conflicts_cleared")
        return changed

    async def _resolve_by_subject_confirmation(
        self,
        fact_id: int,
        *,
        actor_user_id: str,
        source_event_id: int | None,
        session: AsyncSession,
    ) -> None:
        """Let the real subject settle a bounded third-party contradiction."""

        current = await self._repository.get_fact(fact_id, session=session)
        if current is None or current.conflict_state is MemoryConflictState.CLEAR:
            return
        counterpart_ids = {
            relation.target_fact_id
            if relation.source_fact_id == fact_id
            else relation.source_fact_id
            for relation in await self._repository.list_relations(fact_id, session=session)
            if relation.relation_type is MemoryFactRelationType.CONTRADICTS
        }
        invalidated = 0
        blocked = False
        for counterpart_id in counterpart_ids:
            counterpart = await self._repository.get_fact(counterpart_id, session=session)
            if counterpart is None or counterpart.status not in {
                MemoryStatus.ACTIVE,
                MemoryStatus.CONTESTED,
            }:
                continue
            if self._evidence_policy.authority_rank(
                current.authority
            ) < self._evidence_policy.authority_rank(counterpart.authority):
                blocked = True
                continue
            if await self._repository.transition(
                counterpart.id,
                status=MemoryStatus.INVALIDATED,
                conflict_state=MemoryConflictState.CLEAR,
                invalidated_reason=MemoryInvalidationReason.CONFLICT_RESOLUTION,
                action=MemoryStateAction.INVALIDATED,
                reason_code="subject_confirmation",
                source_event_id=source_event_id,
                actor_user_id=actor_user_id,
                session=session,
            ):
                invalidated += 1
        if blocked or (current.status is MemoryStatus.CONTESTED and not invalidated):
            return
        await self._repository.transition(
            fact_id,
            status=MemoryStatus.ACTIVE,
            conflict_state=MemoryConflictState.CLEAR,
            invalidated_reason=None,
            action=MemoryStateAction.CONFLICT_CLEARED,
            reason_code="subject_confirmation",
            source_event_id=source_event_id,
            actor_user_id=actor_user_id,
            session=session,
        )
        if invalidated:
            self.metrics.increment("facts_invalidated", invalidated)
        self.metrics.increment("conflicts_cleared")

    async def _clear_resolved_related_conflicts(
        self,
        fact_id: int,
        *,
        session: AsyncSession,
    ) -> None:
        relations = await self._repository.list_relations(fact_id, session=session)
        related_ids = {
            relation.target_fact_id
            if relation.source_fact_id == fact_id
            else relation.source_fact_id
            for relation in relations
            if relation.relation_type is MemoryFactRelationType.CONTRADICTS
        }
        for related_id in related_ids:
            related = await self._repository.get_fact(related_id, session=session)
            if (
                related is None
                or related.status is not MemoryStatus.ACTIVE
                or related.conflict_state is MemoryConflictState.CLEAR
            ):
                continue
            still_conflicted = False
            for relation in await self._repository.list_relations(related_id, session=session):
                if relation.relation_type is not MemoryFactRelationType.CONTRADICTS:
                    continue
                counterpart_id = (
                    relation.target_fact_id
                    if relation.source_fact_id == related_id
                    else relation.source_fact_id
                )
                counterpart = await self._repository.get_fact(counterpart_id, session=session)
                if counterpart is not None and counterpart.status in {
                    MemoryStatus.ACTIVE,
                    MemoryStatus.CONTESTED,
                }:
                    still_conflicted = True
                    break
            if not still_conflicted:
                await self._clear_conflict_in_session(
                    related_id,
                    reason_code="all_contradictions_resolved",
                    actor_user_id=None,
                    session=session,
                )

    async def restore_fact(
        self,
        fact_id: int,
        *,
        actor_user_id: str,
        evidence: MemoryEvidenceCreate | None = None,
        confirmed_at: datetime | None = None,
        session: AsyncSession | None = None,
    ) -> MemoryFact | None:
        if session is None:
            async with self._repository.transaction() as owned:
                result = await self.restore_fact(
                    fact_id,
                    actor_user_id=actor_user_id,
                    evidence=evidence,
                    confirmed_at=confirmed_at,
                    session=owned,
                )
            if result is not None:
                await self.schedule_embedding(result.id)
            return result
        current = await self._repository.get_fact(fact_id, session=session)
        if current is None or current.status is not MemoryStatus.INVALIDATED:
            return None
        if current.valid_until is not None and current.valid_until <= datetime.now(UTC):
            return None
        probe = MemoryFactCreate(
            scope_type=current.scope_type,
            subject_user_id=current.subject_user_id,
            group_id=current.group_id,
            kind=current.kind,
            memory_key=current.memory_key,
            category=current.category,
            content=current.content,
            source_type=current.source_type,
            authority=current.authority,
        )
        if await self._repository.find_active(probe, session=session) is not None:
            return None
        if evidence is not None:
            await self._repository.add_evidence(fact_id, evidence, session=session)
            await self._refresh_evidence(
                fact_id,
                confirmed_at=confirmed_at or datetime.now(UTC),
                session=session,
            )
        await self._repository.transition(
            fact_id,
            status=MemoryStatus.ACTIVE,
            conflict_state=MemoryConflictState.CLEAR,
            invalidated_reason=None,
            action=MemoryStateAction.RESTORED,
            reason_code="explicit_restore",
            source_event_id=evidence.event_id if evidence is not None else None,
            actor_user_id=actor_user_id,
            session=session,
        )
        self.metrics.increment("facts_restored")
        return await self._repository.get_fact(fact_id, session=session)

    async def merge_facts(
        self,
        source_fact_id: int,
        target_fact_id: int,
        *,
        actor_user_id: str,
        evidence: MemoryEvidenceCreate | None = None,
        confirmed_at: datetime | None = None,
        session: AsyncSession | None = None,
    ) -> MemoryFact | None:
        if source_fact_id == target_fact_id:
            raise ValueError("cannot merge a memory fact into itself")
        if session is None:
            async with self._repository.transaction() as owned:
                return await self.merge_facts(
                    source_fact_id,
                    target_fact_id,
                    actor_user_id=actor_user_id,
                    evidence=evidence,
                    confirmed_at=confirmed_at,
                    session=owned,
                )
        source = await self._repository.get_fact(source_fact_id, session=session)
        target = await self._repository.get_fact(target_fact_id, session=session)
        if source is None or target is None:
            return None
        if source.status not in {MemoryStatus.ACTIVE, MemoryStatus.CONTESTED}:
            raise ValueError("memory merge source must be active or contested")
        source_target = (
            source.scope_type,
            source.subject_user_id,
            source.group_id,
            source.visibility_type,
            source.visibility_user_id,
            source.visibility_group_id,
        )
        target_target = (
            target.scope_type,
            target.subject_user_id,
            target.group_id,
            target.visibility_type,
            target.visibility_user_id,
            target.visibility_group_id,
        )
        if source_target != target_target:
            raise ValueError("memory merge cannot cross identity targets")
        if target.status is not MemoryStatus.ACTIVE:
            raise ValueError("memory merge target must be active")
        for row in await self._repository.list_evidence(
            source_fact_id, limit=100_000, session=session
        ):
            await self._repository.add_evidence(
                target_fact_id,
                MemoryEvidenceCreate(
                    event_id=row.event_id,
                    tool_receipt_id=row.tool_receipt_id,
                    source_speaker_user_id=row.source_speaker_user_id,
                    relation=row.relation,
                    confidence=row.confidence,
                    authority=row.authority,
                    excerpt=row.excerpt,
                ),
                session=session,
            )
        if evidence is not None:
            await self._repository.add_evidence(target_fact_id, evidence, session=session)
        await self._refresh_evidence(
            target_fact_id,
            confirmed_at=max(
                _as_utc(source.last_confirmed_at),
                _as_utc(target.last_confirmed_at),
                _as_utc(confirmed_at or target.last_confirmed_at),
            ),
            session=session,
        )
        await self._repository.add_relation(
            source_fact_id=source_fact_id,
            target_fact_id=target_fact_id,
            relation_type=MemoryFactRelationType.EQUIVALENT,
            confidence=1.0,
            source_event_id=evidence.event_id if evidence is not None else None,
            session=session,
        )
        await self._repository.transition(
            source_fact_id,
            status=MemoryStatus.SUPERSEDED,
            conflict_state=MemoryConflictState.CLEAR,
            invalidated_reason=None,
            action=MemoryStateAction.MERGED,
            reason_code=MemoryInvalidationReason.MERGED.value,
            source_event_id=evidence.event_id if evidence is not None else None,
            actor_user_id=actor_user_id,
            session=session,
        )
        self.metrics.increment("facts_merged")
        return await self._repository.get_fact(target_fact_id, session=session)

    async def resolve_conflicts(
        self,
        preferred_fact_id: int,
        contested_fact_ids: tuple[int, ...],
        *,
        actor_user_id: str,
    ) -> int:
        """Atomically choose one fact and invalidate the explicitly supplied alternatives."""

        unique_ids = tuple(
            fact_id for fact_id in dict.fromkeys(contested_fact_ids) if fact_id != preferred_fact_id
        )
        promoted = False
        async with self._repository.transaction() as session:
            preferred = await self._repository.get_fact(preferred_fact_id, session=session)
            if preferred is None or preferred.status not in {
                MemoryStatus.ACTIVE,
                MemoryStatus.CONTESTED,
            }:
                raise ValueError("preferred memory fact is not resolvable")
            target = (
                preferred.scope_type,
                preferred.subject_user_id,
                preferred.group_id,
                preferred.visibility_type,
                preferred.visibility_user_id,
                preferred.visibility_group_id,
                preferred.kind,
                preferred.memory_key,
            )
            alternatives: list[MemoryFact] = []
            for fact_id in unique_ids:
                fact = await self._repository.get_fact(fact_id, session=session)
                if fact is None:
                    raise ValueError("contested memory fact does not exist")
                if fact.status not in {MemoryStatus.ACTIVE, MemoryStatus.CONTESTED}:
                    raise ValueError("conflict alternative is not active or contested")
                if (
                    fact.scope_type,
                    fact.subject_user_id,
                    fact.group_id,
                    fact.visibility_type,
                    fact.visibility_user_id,
                    fact.visibility_group_id,
                    fact.kind,
                    fact.memory_key,
                ) != target:
                    raise ValueError("memory conflict resolution cannot cross a fact slot")
                alternatives.append(fact)
            if preferred.status is MemoryStatus.CONTESTED:
                active = await self._repository.find_active(
                    MemoryFactCreate(
                        scope_type=preferred.scope_type,
                        subject_user_id=preferred.subject_user_id,
                        group_id=preferred.group_id,
                        visibility_type=preferred.visibility_type,
                        visibility_user_id=preferred.visibility_user_id,
                        visibility_group_id=preferred.visibility_group_id,
                        kind=preferred.kind,
                        memory_key=preferred.memory_key,
                        category=preferred.category,
                        content=preferred.content,
                        source_type=preferred.source_type,
                        authority=preferred.authority,
                    ),
                    session=session,
                )
                if active is not None and active.id not in {row.id for row in alternatives}:
                    raise ValueError("the active conflicting fact must be included in resolution")
            changed = 0
            for fact in alternatives:
                if fact.status is MemoryStatus.INVALIDATED:
                    continue
                if await self._repository.transition(
                    fact.id,
                    status=MemoryStatus.INVALIDATED,
                    conflict_state=MemoryConflictState.CLEAR,
                    invalidated_reason=MemoryInvalidationReason.CONFLICT_RESOLUTION,
                    action=MemoryStateAction.INVALIDATED,
                    reason_code=MemoryInvalidationReason.CONFLICT_RESOLUTION.value,
                    source_event_id=None,
                    actor_user_id=actor_user_id,
                    session=session,
                ):
                    changed += 1
            if preferred.status is MemoryStatus.CONTESTED or (
                preferred.conflict_state is MemoryConflictState.CONTESTED
            ):
                await self._repository.transition(
                    preferred.id,
                    status=MemoryStatus.ACTIVE,
                    conflict_state=MemoryConflictState.CLEAR,
                    invalidated_reason=None,
                    action=MemoryStateAction.CONFLICT_CLEARED,
                    reason_code="administrator_resolution",
                    source_event_id=None,
                    actor_user_id=actor_user_id,
                    session=session,
                )
                promoted = preferred.status is MemoryStatus.CONTESTED
            if changed:
                self.metrics.increment("facts_invalidated", changed)
            self.metrics.increment("conflicts_cleared")
        if promoted:
            await self.schedule_embedding(preferred_fact_id)
        return changed

    async def set_preference(
        self,
        user_id: str,
        key: str,
        value: str,
        *,
        limit: int = 30,
        source_type: str = "explicit",
        session: AsyncSession | None = None,
    ) -> MemoryFact:
        return await self.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=user_id,
                kind=MemoryKind.PREFERENCE,
                memory_key=key,
                category="preference",
                content=value,
                importance=4,
                confidence=1,
                source_type=MemorySourceType(source_type),
                authority=(
                    MemoryAuthority.EXPLICIT
                    if source_type == MemorySourceType.EXPLICIT.value
                    else MemoryAuthority.SELF_REPORT
                ),
            ),
            limit=limit,
            session=session,
        )

    async def delete_preference(
        self,
        user_id: str,
        key: str,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        if session is None:
            async with self._repository.transaction() as owned:
                return await self.delete_preference(user_id, key, session=owned)
        rows = await self._repository.list_facts(
            MemoryFactQuery(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=user_id,
                kind=MemoryKind.PREFERENCE,
            ),
            limit=100_000,
            session=session,
        )
        row = next((item for item in rows if item.memory_key == key), None)
        if row is None:
            return False
        changed = await self._repository.transition(
            row.id,
            status=MemoryStatus.INVALIDATED,
            conflict_state=MemoryConflictState.CLEAR,
            invalidated_reason=MemoryInvalidationReason.USER_RETRACTED,
            action=MemoryStateAction.INVALIDATED,
            reason_code="preference_deleted",
            source_event_id=None,
            actor_user_id=user_id,
            session=session,
        )
        if changed:
            self.metrics.increment("facts_invalidated")
        return changed

    async def prune_person_memories(
        self,
        *,
        user_id: str,
        max_importance: int,
        older_than: datetime,
        session: AsyncSession,
    ) -> int:
        rows = await self._repository.list_facts(
            MemoryFactQuery(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=user_id,
            ),
            limit=100_000,
            session=session,
        )
        candidates = tuple(
            row
            for row in rows
            if row.source_type is not MemorySourceType.EXPLICIT
            and row.importance <= max_importance
            and row.updated_at < older_than
        )
        changed = 0
        for row in candidates:
            if await self._repository.transition(
                row.id,
                status=MemoryStatus.INVALIDATED,
                conflict_state=MemoryConflictState.CLEAR,
                invalidated_reason=MemoryInvalidationReason.STALE,
                action=MemoryStateAction.STALE_INVALIDATED,
                reason_code="manual_prune",
                source_event_id=None,
                actor_user_id=user_id,
                session=session,
            ):
                changed += 1
        if changed:
            self.metrics.increment("facts_invalidated", changed)
        return changed

    async def list_evidence(self, fact_id: int, *, limit: int = 100) -> tuple[MemoryEvidence, ...]:
        return await self._repository.list_evidence(fact_id, limit=limit)

    async def mark_used(self, fact_ids: tuple[int, ...]) -> int:
        """Mark only facts that survived final context budgeting."""

        return await self._repository.mark_used(fact_ids)
