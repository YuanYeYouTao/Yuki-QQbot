"""Unified, auditable, and idempotent orchestration for Memory V2 writes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.claim_processor import (
    MemoryClaimProcessor,
    MemoryClaimResolution,
    MemoryProcessingContext,
)
from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamClusterModel,
    MemoryDreamOperationModel,
    MemoryDreamOperationResultModel,
    MemoryDreamOperationSourceModel,
)
from qq_ai_bot.memory.dream.models import (
    DreamMutationResult,
    DreamOperationStatus,
    DreamOperationType,
)
from qq_ai_bot.memory.dream.quality import episode_compression_limit
from qq_ai_bot.memory.dream.repository import fact_signature
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryClaimOperation,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryKind,
    MemoryProcessingSource,
    MemoryResolutionAction,
    MemoryReviewState,
    MemoryScopeType,
    MemorySourceType,
    MemoryStateAction,
    MemoryStatus,
    MemorySubjectBasis,
    MemoryTemporalMode,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.models import (
    MemoryCandidate,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactCreate,
    MemoryFactQuery,
    MemoryResolutionPlan,
)
from qq_ai_bot.memory.mutation.models import (
    SELF_MEMORY_CATEGORIES,
    MemoryDecisionActorType,
    MemoryMutationAppliedOperation,
    MemoryMutationCandidate,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationOutcome,
    MemoryMutationRequest,
    MemoryMutationRequestBasis,
    MemoryMutationResult,
    MemoryMutationSelector,
    MemoryMutationTarget,
    SelfMemoryVisibilityMode,
)
from qq_ai_bot.memory.mutation.repository import MemoryMutationReceiptRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import ResolvedSubject, SubjectResolver
from qq_ai_bot.memory.temporal import MemoryTemporalResolver
from qq_ai_bot.memory.validation import (
    ValidatedMemoryClaim,
    normalize_memory_text,
)
from qq_ai_bot.persistence.models import (
    MemoryEvidenceModel,
    MemoryFactRelationModel,
    MemorySelfReflectionResultModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DreamRecomposePlan:
    source_facts: tuple[MemoryFact, ...]
    content: str
    importance: int


class MemoryMutationRejected(ValueError):
    """A stable policy or request rejection safe to return to the main Agent."""

    def __init__(
        self,
        reason_code: str,
        *,
        candidates: tuple[MemoryMutationCandidate, ...] = (),
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.candidates = candidates


@dataclass(frozen=True, slots=True)
class _PreparedMutation:
    request: MemoryMutationRequest
    context: MemoryMutationContext
    subject_ref: str
    target: ResolvedSubject
    fact: MemoryFact | None
    merge_fact: MemoryFact | None
    evidence: MemoryEvidenceCreate
    claim: ValidatedMemoryClaim | None
    idempotency_key: str
    claim_fingerprint: str
    target_fingerprint: str


@dataclass(frozen=True, slots=True)
class _AppliedMutation:
    operation: MemoryMutationAppliedOperation
    outcome: MemoryMutationOutcome
    old_fact_id: int | None
    new_fact_id: int | None
    reason_code: str


class MemoryMutationService:
    """The only supported orchestration boundary for durable Memory V2 changes."""

    def __init__(
        self,
        *,
        settings: Settings,
        facts: MemoryFactService,
        processor: MemoryClaimProcessor,
        ledger: EventLedgerRepository,
        receipts: MemoryMutationReceiptRepository | None = None,
        subject_resolver: SubjectResolver | None = None,
        temporal_resolver: MemoryTemporalResolver | None = None,
    ) -> None:
        self._settings = settings
        self._facts = facts
        self._processor = processor
        self._ledger = ledger
        self._receipts = receipts or MemoryMutationReceiptRepository(facts.repository.database)
        self._subjects = subject_resolver or SubjectResolver()
        self._temporal = temporal_resolver or MemoryTemporalResolver()
        self._lock = asyncio.Lock()

    async def mutate(
        self,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
    ) -> MemoryMutationResult:
        """Validate, resolve, commit, and receipt one requested mutation."""

        try:
            prepared = await self._prepare(request, context)
        except MemoryMutationRejected as exc:
            return self._rejected(
                request.operation,
                exc.reason_code,
                candidates=exc.candidates,
            )
        return await self._commit_prepared(prepared)

    @staticmethod
    def select_dream_anchor(facts: tuple[MemoryFact, ...]) -> MemoryFact:
        """Choose one stable metadata anchor without asking the model for identity fields."""

        if not facts:
            raise ValueError("dream anchor selection requires at least one fact")
        authority_rank = {
            MemoryAuthority.THIRD_PARTY: 0,
            MemoryAuthority.GROUP_REPORT: 1,
            MemoryAuthority.SELF_REPORT: 2,
            MemoryAuthority.AGENT_REFLECTION: 3,
            MemoryAuthority.EXPLICIT: 4,
        }
        return max(
            facts,
            key=lambda fact: (
                fact.source_type is MemorySourceType.EXPLICIT
                or fact.authority is MemoryAuthority.EXPLICIT,
                fact.status is MemoryStatus.ACTIVE,
                authority_rank[fact.authority],
                fact.evidence_count,
                fact.updated_at.timestamp(),
                -fact.id,
            ),
        )

    async def mutate_dream(
        self,
        *,
        dream_operation_id: int,
        operation_type: DreamOperationType,
        source_facts: tuple[MemoryFact, ...],
        anchor_fact_id: int | None,
        content: str | None,
        importance: int | None,
        recompose_outputs: tuple[DreamRecomposePlan, ...] = (),
        bot_user_id: str,
        run_public_id: str,
        session: AsyncSession,
    ) -> DreamMutationResult:
        """Apply one already-validated Dream action inside its cluster transaction."""

        if not source_facts:
            raise ValueError("dream mutation requires source facts")
        current: list[MemoryFact] = []
        for snapshot in source_facts:
            fact = await self._facts.repository.get_fact(snapshot.id, session=session)
            if fact is None or fact.status not in {MemoryStatus.ACTIVE, MemoryStatus.CONTESTED}:
                raise ValueError("dream source changed before commit")
            current.append(fact)
        sources = tuple(current)
        partition = self._dream_partition(sources[0])
        if any(self._dream_partition(item) != partition for item in sources[1:]):
            raise ValueError("dream mutation cannot cross memory partitions")
        if operation_type is DreamOperationType.RECOMPOSE:
            if sources[0].kind is not MemoryKind.EPISODE:
                raise ValueError("dream recompose is only available for episodes")
            if content is not None or importance is not None:
                raise ValueError("dream recompose uses its bounded output list")
            if not 1 <= len(recompose_outputs) <= 4:
                raise ValueError("dream recompose requires one to four outputs")
            if any(not 1 <= output.importance <= 5 for output in recompose_outputs):
                raise ValueError("dream recompose importance is out of range")
        elif recompose_outputs:
            raise ValueError("only dream recompose accepts multiple outputs")
        explicit = tuple(item for item in sources if self._dream_explicit(item))
        if (
            operation_type
            in {
                DreamOperationType.CONTEST,
                DreamOperationType.SYNTHESIZE,
                DreamOperationType.RECOMPOSE,
            }
            and explicit
        ):
            raise ValueError("dream cannot modify an explicit memory anchor")
        if operation_type is DreamOperationType.MERGE and len(explicit) > 1:
            raise ValueError("dream cannot merge two explicit memory anchors")

        anchor = next((item for item in sources if item.id == anchor_fact_id), None)
        if operation_type in {
            DreamOperationType.MERGE,
            DreamOperationType.SYNTHESIZE,
            DreamOperationType.RECOMPOSE,
            DreamOperationType.RESOLVE,
        }:
            if anchor is None:
                raise ValueError("dream mutation anchor is missing")
            if explicit and anchor.id != explicit[0].id:
                raise ValueError("an explicit memory must remain the dream anchor")

        requested, applied = self._dream_receipt_operations(operation_type)
        fingerprint_payload = {
            "operation_id": dream_operation_id,
            "operation": operation_type.value,
            "sources": [item.id for item in sources],
            "anchor": anchor.id if anchor else None,
            "content": content,
            "recompose_outputs": [
                {
                    "sources": [item.id for item in output.source_facts],
                    "content": output.content,
                    "importance": output.importance,
                }
                for output in recompose_outputs
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        existing = await self._receipts.find(
            idempotency_key=fingerprint,
            claim_fingerprint=fingerprint,
            session=session,
        )
        if existing is not None:
            return DreamMutationResult(
                operation_id=dream_operation_id,
                output_fact_ids=(
                    (existing.new_fact_id,) if existing.new_fact_id is not None else ()
                ),
                added_evidence_ids=(),
                added_relation_ids=(),
                changed=False,
            )
        target_fingerprint = hashlib.sha256(repr(partition).encode()).hexdigest()
        receipt = await self._receipts.reserve_dream(
            mutation_id=str(uuid.uuid4()),
            idempotency_key=fingerprint,
            claim_fingerprint=fingerprint,
            target_fingerprint=target_fingerprint,
            dream_operation_id=dream_operation_id,
            conversation_key=f"memory-dream:{bot_user_id}:{run_public_id}",
            current_group_id=sources[0].group_id or sources[0].visibility_group_id,
            bot_user_id=bot_user_id,
            requested_operation=requested,
            created_at=datetime.now(UTC),
            session=session,
        )

        source_ids = tuple(item.id for item in sources)
        before_evidence = set(
            await session.scalars(
                select(MemoryEvidenceModel.id).where(MemoryEvidenceModel.fact_id.in_(source_ids))
            )
        )
        before_relations = set(
            await session.scalars(
                select(MemoryFactRelationModel.id).where(
                    (MemoryFactRelationModel.source_fact_id.in_(source_ids))
                    | (MemoryFactRelationModel.target_fact_id.in_(source_ids))
                )
            )
        )
        changed = False
        output_fact_ids: tuple[int, ...] = (anchor.id,) if anchor is not None else (sources[0].id,)
        outcome = MemoryMutationOutcome.NO_CHANGE
        reason_code = f"memory_dream_{operation_type.value}"

        if operation_type is DreamOperationType.KEEP:
            output_fact_ids = (sources[0].id,)
        elif operation_type is DreamOperationType.MERGE:
            assert anchor is not None
            for source in sources:
                if source.id == anchor.id:
                    continue
                merged = await self._facts.merge_facts(
                    source.id,
                    anchor.id,
                    actor_user_id=bot_user_id,
                    source_evidence=await self._dream_evidence_bundle(
                        (source,), session=session, maximum_total=2
                    ),
                    confirmed_at=max(item.last_confirmed_at for item in sources),
                    session=session,
                )
                changed = changed or merged is not None
            output_fact_ids = (anchor.id,)
            outcome = MemoryMutationOutcome.COMMITTED if changed else outcome
        elif operation_type is DreamOperationType.SYNTHESIZE:
            assert anchor is not None
            normalized = normalize_memory_text(content or "", maximum=4000)
            if not normalized:
                raise ValueError("dream synthesis content cannot be empty")
            for source in sources:
                await self._facts.repository.transition(
                    source.id,
                    status=MemoryStatus.SUPERSEDED,
                    conflict_state=MemoryConflictState.CLEAR,
                    invalidated_reason=None,
                    action=MemoryStateAction.SUPERSEDED,
                    reason_code=reason_code,
                    source_event_id=None,
                    actor_user_id=bot_user_id,
                    session=session,
                )
            replacement = MemoryFactCreate(
                scope_type=anchor.scope_type,
                subject_user_id=anchor.subject_user_id,
                group_id=anchor.group_id,
                visibility_type=anchor.visibility_type,
                visibility_user_id=anchor.visibility_user_id,
                visibility_group_id=anchor.visibility_group_id,
                kind=anchor.kind,
                memory_key=anchor.memory_key,
                category=anchor.category,
                content=normalized,
                importance=importance or anchor.importance,
                confidence=anchor.confidence,
                source_type=anchor.source_type,
                authority=anchor.authority,
                valid_from=min(
                    (item.valid_from for item in sources if item.valid_from is not None),
                    default=anchor.valid_from,
                ),
                valid_until=(
                    None
                    if any(item.valid_until is None for item in sources)
                    else max(item.valid_until for item in sources if item.valid_until is not None)
                ),
                validation_version=anchor.validation_version,
                review_state=anchor.review_state,
            )
            collision = await self._facts.repository.find_active(replacement, session=session)
            if collision is not None:
                raise ValueError("dream synthesis collided with an unrelated active key")
            created = await self._facts.repository.create_fact(
                replacement,
                normalized_content=normalized.casefold(),
                supersedes_id=anchor.id,
                recorded_at=datetime.now(UTC),
                session=session,
            )
            evidence = await self._dream_evidence_bundle(sources, session=session)
            await self._facts.append_evidence_bundle(
                created.id,
                evidence,
                confirmed_at=max(item.last_confirmed_at for item in sources),
                session=session,
            )
            await self._facts.repository.record_created(
                created.id,
                status=MemoryStatus.ACTIVE,
                conflict_state=MemoryConflictState.CLEAR,
                reason_code=reason_code,
                source_event_id=None,
                actor_user_id=bot_user_id,
                session=session,
            )
            for source in sources:
                await self._facts.repository.add_relation(
                    source_fact_id=source.id,
                    target_fact_id=created.id,
                    relation_type=MemoryFactRelationType.REFINES,
                    confidence=1.0,
                    source_event_id=None,
                    session=session,
                )
            output_fact_ids = (created.id,)
            changed = True
            outcome = MemoryMutationOutcome.COMMITTED
        elif operation_type is DreamOperationType.RECOMPOSE:
            assert anchor is not None
            source_by_id = {item.id: item for item in sources}
            if not recompose_outputs:
                raise ValueError("dream recompose requires outputs")
            referenced = {
                source.id for output in recompose_outputs for source in output.source_facts
            }
            if referenced != set(source_by_id):
                raise ValueError("dream recompose outputs must cover all operation sources")
            if any(
                not output.source_facts
                or any(source.id not in source_by_id for source in output.source_facts)
                for output in recompose_outputs
            ):
                raise ValueError("dream recompose output has invalid sources")
            normalized_outputs = tuple(
                normalize_memory_text(output.content, maximum=4000) for output in recompose_outputs
            )
            if any(not item for item in normalized_outputs):
                raise ValueError("dream recompose content cannot be empty")
            if len({item.casefold() for item in normalized_outputs}) != len(normalized_outputs):
                raise ValueError("dream recompose content must be unique")
            if any(
                len(item) > self._settings.memory_dream_episode_max_characters
                for item in normalized_outputs
            ):
                raise ValueError("dream recompose content exceeds the character limit")
            source_characters = sum(len(item.content) for item in sources)
            output_characters = sum(len(item) for item in normalized_outputs)
            if output_characters > episode_compression_limit(
                source_characters,
                ratio=self._settings.memory_dream_episode_hard_compression_ratio,
                maximum=self._settings.memory_dream_episode_max_characters,
            ):
                raise ValueError("dream recompose did not compress its source episodes")
            for source in sources:
                await self._facts.repository.transition(
                    source.id,
                    status=MemoryStatus.SUPERSEDED,
                    conflict_state=MemoryConflictState.CLEAR,
                    invalidated_reason=None,
                    action=MemoryStateAction.SUPERSEDED,
                    reason_code=reason_code,
                    source_event_id=None,
                    actor_user_id=bot_user_id,
                    session=session,
                )
            created_ids: list[int] = []
            used_keys: set[str] = set()
            for output_index, (output, normalized) in enumerate(
                zip(recompose_outputs, normalized_outputs, strict=True), start=1
            ):
                output_sources = tuple(source_by_id[item.id] for item in output.source_facts)
                output_anchor = self.select_dream_anchor(output_sources)
                memory_key = self._dream_recompose_key(
                    output_anchor.memory_key,
                    dream_operation_id=dream_operation_id,
                    output_index=output_index,
                    content=normalized,
                    used_keys=used_keys,
                )
                used_keys.add(memory_key)
                replacement = MemoryFactCreate(
                    scope_type=output_anchor.scope_type,
                    subject_user_id=output_anchor.subject_user_id,
                    group_id=output_anchor.group_id,
                    visibility_type=output_anchor.visibility_type,
                    visibility_user_id=output_anchor.visibility_user_id,
                    visibility_group_id=output_anchor.visibility_group_id,
                    kind=output_anchor.kind,
                    memory_key=memory_key,
                    category=output_anchor.category,
                    content=normalized,
                    importance=output.importance,
                    confidence=output_anchor.confidence,
                    source_type=output_anchor.source_type,
                    authority=output_anchor.authority,
                    valid_from=min(
                        (item.valid_from for item in output_sources if item.valid_from is not None),
                        default=output_anchor.valid_from,
                    ),
                    valid_until=(
                        None
                        if any(item.valid_until is None for item in output_sources)
                        else max(
                            item.valid_until
                            for item in output_sources
                            if item.valid_until is not None
                        )
                    ),
                    validation_version=output_anchor.validation_version,
                    review_state=output_anchor.review_state,
                )
                collision = await self._facts.repository.find_active(replacement, session=session)
                if collision is not None:
                    raise ValueError("dream recompose collided with an active key")
                created = await self._facts.repository.create_fact(
                    replacement,
                    normalized_content=normalized.casefold(),
                    supersedes_id=output_anchor.id,
                    recorded_at=datetime.now(UTC),
                    session=session,
                )
                evidence = await self._dream_evidence_bundle(output_sources, session=session)
                await self._facts.append_evidence_bundle(
                    created.id,
                    evidence,
                    confirmed_at=max(item.last_confirmed_at for item in output_sources),
                    session=session,
                )
                await self._facts.repository.record_created(
                    created.id,
                    status=MemoryStatus.ACTIVE,
                    conflict_state=MemoryConflictState.CLEAR,
                    reason_code=reason_code,
                    source_event_id=None,
                    actor_user_id=bot_user_id,
                    session=session,
                )
                for source in output_sources:
                    await self._facts.repository.add_relation(
                        source_fact_id=source.id,
                        target_fact_id=created.id,
                        relation_type=MemoryFactRelationType.REFINES,
                        confidence=1.0,
                        source_event_id=None,
                        session=session,
                    )
                created_ids.append(created.id)
            output_fact_ids = tuple(created_ids)
            changed = True
            outcome = MemoryMutationOutcome.COMMITTED
        elif operation_type is DreamOperationType.CONTEST:
            for source in sources:
                changed = (
                    await self._facts.contest_fact(
                        source.id,
                        reason_code=reason_code,
                        actor_user_id=bot_user_id,
                        session=session,
                    )
                    or changed
                )
            for index, source in enumerate(sources):
                for target in sources[index + 1 :]:
                    await self._facts.repository.add_relation(
                        source_fact_id=source.id,
                        target_fact_id=target.id,
                        relation_type=MemoryFactRelationType.CONTRADICTS,
                        confidence=1.0,
                        source_event_id=None,
                        session=session,
                    )
            output_fact_ids = (sources[0].id,)
            outcome = MemoryMutationOutcome.COMMITTED_AS_CONTESTED if changed else outcome
        elif operation_type is DreamOperationType.RESOLVE:
            assert anchor is not None
            for source in sources:
                if source.id == anchor.id:
                    continue
                if self._dream_explicit(source):
                    raise ValueError("dream cannot invalidate an explicit memory")
                await self._facts.repository.add_relation(
                    source_fact_id=source.id,
                    target_fact_id=anchor.id,
                    relation_type=MemoryFactRelationType.CONTRADICTS,
                    confidence=1.0,
                    source_event_id=None,
                    session=session,
                )
                changed = (
                    await self._facts.invalidate_fact(
                        source.id,
                        reason=MemoryInvalidationReason.CONFLICT_RESOLUTION,
                        actor_user_id=bot_user_id,
                        session=session,
                    )
                    or changed
                )
            if not self._dream_explicit(anchor) and (
                anchor.status is MemoryStatus.CONTESTED
                or anchor.conflict_state is MemoryConflictState.CONTESTED
            ):
                await self._facts.repository.transition(
                    anchor.id,
                    status=MemoryStatus.ACTIVE,
                    conflict_state=MemoryConflictState.CLEAR,
                    invalidated_reason=None,
                    action=MemoryStateAction.CONFLICT_CLEARED,
                    reason_code=reason_code,
                    source_event_id=None,
                    actor_user_id=bot_user_id,
                    session=session,
                )
                changed = True
            output_fact_ids = (anchor.id,)
            outcome = MemoryMutationOutcome.COMMITTED if changed else outcome

        affected_ids = tuple(dict.fromkeys((*source_ids, *output_fact_ids)))
        after_evidence = set(
            await session.scalars(
                select(MemoryEvidenceModel.id).where(MemoryEvidenceModel.fact_id.in_(affected_ids))
            )
        )
        after_relations = set(
            await session.scalars(
                select(MemoryFactRelationModel.id).where(
                    (MemoryFactRelationModel.source_fact_id.in_(affected_ids))
                    | (MemoryFactRelationModel.target_fact_id.in_(affected_ids))
                )
            )
        )
        finalized = await self._receipts.finalize(
            receipt.id,
            applied_operation=applied if changed else MemoryMutationAppliedOperation.NOOP,
            old_fact_id=sources[0].id,
            new_fact_id=output_fact_ids[0] if output_fact_ids else None,
            outcome=outcome,
            reason_code=reason_code,
            session=session,
        )
        _ = finalized
        return DreamMutationResult(
            operation_id=dream_operation_id,
            output_fact_ids=output_fact_ids,
            added_evidence_ids=tuple(sorted(after_evidence - before_evidence)),
            added_relation_ids=tuple(sorted(after_relations - before_relations)),
            changed=changed,
        )

    async def rollback_dream_operation(
        self,
        *,
        public_id: str,
        session: AsyncSession,
    ) -> tuple[int, ...]:
        """Rollback one unchanged Dream result through the unified mutation boundary."""

        operation_row = (
            await session.execute(
                select(MemoryDreamOperationModel, MemoryDreamClusterModel)
                .join(
                    MemoryDreamClusterModel,
                    MemoryDreamClusterModel.id == MemoryDreamOperationModel.cluster_id,
                )
                .where(
                    MemoryDreamOperationModel.public_id == public_id,
                    MemoryDreamOperationModel.status == DreamOperationStatus.COMMITTED.value,
                )
            )
        ).one_or_none()
        if operation_row is None:
            return ()
        operation, cluster = operation_row
        source_rows = tuple(
            (
                await session.scalars(
                    select(MemoryDreamOperationSourceModel)
                    .where(MemoryDreamOperationSourceModel.operation_id == operation.id)
                    .order_by(MemoryDreamOperationSourceModel.position)
                )
            ).all()
        )
        if not source_rows or any(row.after_signature is None for row in source_rows):
            raise RuntimeError("Dream operation is missing its committed source signatures")
        current_sources: dict[int, MemoryFact] = {}
        for source in source_rows:
            current = await self._facts.repository.get_fact(source.fact_id, session=session)
            if current is None or fact_signature(current) != source.after_signature:
                raise RuntimeError("Dream source changed after this operation")
            current_sources[source.fact_id] = current
        result_rows = tuple(
            (
                await session.scalars(
                    select(MemoryDreamOperationResultModel)
                    .where(MemoryDreamOperationResultModel.operation_id == operation.id)
                    .order_by(MemoryDreamOperationResultModel.position)
                )
            ).all()
        )
        output_ids: tuple[int, ...]
        expected_signatures: tuple[str | None, ...]
        if not result_rows and operation.output_fact_id is not None:
            output_ids = (operation.output_fact_id,)
            expected_signatures = (operation.result_signature,)
        else:
            output_ids = tuple(row.fact_id for row in result_rows)
            expected_signatures = tuple(row.result_signature for row in result_rows)
        for fact_id, signature in zip(output_ids, expected_signatures, strict=True):
            output = await self._facts.repository.get_fact(fact_id, session=session)
            if output is None or signature is None or fact_signature(output) != signature:
                raise RuntimeError("Dream result changed after this operation")

        affected_ids = tuple(
            dict.fromkeys(
                (
                    *(source.fact_id for source in source_rows),
                    *output_ids,
                )
            )
        )
        dependencies = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryDreamOperationSourceModel)
                .join(
                    MemoryDreamOperationModel,
                    MemoryDreamOperationModel.id == MemoryDreamOperationSourceModel.operation_id,
                )
                .where(
                    MemoryDreamOperationSourceModel.fact_id.in_(affected_ids),
                    MemoryDreamOperationModel.id > operation.id,
                    MemoryDreamOperationModel.status == DreamOperationStatus.COMMITTED.value,
                )
            )
            or 0
        )
        if dependencies:
            raise RuntimeError("Dream operation has later dependent operations")

        fingerprint = hashlib.sha256(f"dream-rollback:{operation.id}".encode()).hexdigest()
        receipt = await self._receipts.reserve_dream(
            mutation_id=str(uuid.uuid4()),
            idempotency_key=fingerprint,
            claim_fingerprint=fingerprint,
            target_fingerprint=hashlib.sha256(cluster.partition_key.encode()).hexdigest(),
            dream_operation_id=operation.id,
            conversation_key=f"memory-dream-rollback:{cluster.bot_user_id}:{public_id}",
            current_group_id=(
                current_sources[source_rows[0].fact_id].group_id
                or current_sources[source_rows[0].fact_id].visibility_group_id
            ),
            bot_user_id=cluster.bot_user_id,
            requested_operation=MemoryMutationOperation.RESTORE,
            created_at=datetime.now(UTC),
            session=session,
        )
        added_evidence = tuple(int(item) for item in json.loads(operation.added_evidence_ids_json))
        added_relations = tuple(int(item) for item in json.loads(operation.added_relation_ids_json))
        if added_evidence:
            await session.execute(
                delete(MemoryEvidenceModel).where(MemoryEvidenceModel.id.in_(added_evidence))
            )
        if added_relations:
            await session.execute(
                delete(MemoryFactRelationModel).where(
                    MemoryFactRelationModel.id.in_(added_relations)
                )
            )
        source_ids = {row.fact_id for row in source_rows}
        for output_fact_id in output_ids:
            if output_fact_id in source_ids:
                continue
            await self._facts.repository.transition(
                output_fact_id,
                status=MemoryStatus.INVALIDATED,
                conflict_state=MemoryConflictState.CLEAR,
                invalidated_reason=MemoryInvalidationReason.DREAM_ROLLBACK,
                action=MemoryStateAction.INVALIDATED,
                reason_code=MemoryInvalidationReason.DREAM_ROLLBACK.value,
                source_event_id=None,
                actor_user_id=cluster.bot_user_id,
                session=session,
            )
        for source in source_rows:
            await self._facts.repository.transition(
                source.fact_id,
                status=MemoryStatus(source.before_status),
                conflict_state=MemoryConflictState(source.before_conflict_state),
                invalidated_reason=(
                    MemoryInvalidationReason(source.before_invalidated_reason)
                    if source.before_invalidated_reason is not None
                    else None
                ),
                action=(
                    MemoryStateAction.RESTORED
                    if source.before_status == MemoryStatus.ACTIVE.value
                    else MemoryStateAction.CONTESTED
                ),
                reason_code="memory_dream_rollback",
                source_event_id=None,
                actor_user_id=cluster.bot_user_id,
                session=session,
            )
            await self._facts.repository.restore_confirmation_metadata(
                source.fact_id,
                authority=source.before_authority,
                confidence=source.before_confidence,
                last_confirmed_at=source.before_last_confirmed_at,
                session=session,
            )
        operation.status = DreamOperationStatus.ROLLED_BACK.value
        operation.rolled_back_at = datetime.now(UTC)
        cluster.status = "rolled_back"
        cluster.updated_at = datetime.now(UTC)
        await self._receipts.finalize(
            receipt.id,
            applied_operation=MemoryMutationAppliedOperation.RESTORE,
            old_fact_id=output_ids[0] if output_ids else None,
            new_fact_id=source_rows[0].fact_id,
            outcome=MemoryMutationOutcome.COMMITTED,
            reason_code="memory_dream_rollback",
            session=session,
        )
        return affected_ids

    async def _dream_evidence_bundle(
        self,
        facts: tuple[MemoryFact, ...],
        *,
        session: AsyncSession,
        maximum_total: int = 12,
    ) -> tuple[MemoryEvidenceCreate, ...]:
        rows: dict[tuple[str, int], MemoryEvidenceCreate] = {}
        for fact in facts:
            evidence_rows = await self._facts.repository.list_evidence(
                fact.id, limit=100_000, session=session
            )
            ordered = tuple(sorted(evidence_rows, key=lambda item: (item.created_at, item.id)))
            limit = self._settings.memory_dream_evidence_per_fact
            if len(ordered) <= limit:
                selected = ordered
            elif limit == 1:
                selected = (ordered[-1],)
            else:
                selected = (ordered[0], *ordered[-(limit - 1) :])
            for evidence in selected:
                source = (
                    ("event", evidence.event_id)
                    if evidence.event_id is not None
                    else ("tool", evidence.tool_receipt_id)
                )
                assert source[1] is not None
                rows[(source[0], int(source[1]))] = MemoryEvidenceCreate(
                    event_id=evidence.event_id,
                    tool_receipt_id=evidence.tool_receipt_id,
                    source_speaker_user_id=evidence.source_speaker_user_id,
                    relation=evidence.relation,
                    confidence=evidence.confidence,
                    authority=evidence.authority,
                    excerpt=evidence.excerpt,
                )
                if len(rows) >= maximum_total:
                    return tuple(rows.values())
        return tuple(rows.values())

    @staticmethod
    def _dream_recompose_key(
        base_key: str,
        *,
        dream_operation_id: int,
        output_index: int,
        content: str,
        used_keys: set[str],
    ) -> str:
        if base_key not in used_keys:
            return base_key
        suffix = hashlib.sha256(
            f"{dream_operation_id}:{output_index}:{content.casefold()}".encode()
        ).hexdigest()[:16]
        prefix = base_key[: max(1, 128 - len(suffix) - 7)]
        return f"{prefix}:dream:{suffix}"

    @staticmethod
    def _dream_partition(fact: MemoryFact) -> tuple[object, ...]:
        return (
            fact.scope_type,
            fact.subject_user_id,
            fact.group_id,
            fact.visibility_type,
            fact.visibility_user_id,
            fact.visibility_group_id,
            fact.kind,
        )

    @staticmethod
    def _dream_explicit(fact: MemoryFact) -> bool:
        return bool(
            fact.source_type is MemorySourceType.EXPLICIT
            or fact.authority is MemoryAuthority.EXPLICIT
        )

    @staticmethod
    def _dream_receipt_operations(
        operation: DreamOperationType,
    ) -> tuple[MemoryMutationOperation, MemoryMutationAppliedOperation]:
        return {
            DreamOperationType.KEEP: (
                MemoryMutationOperation.UPDATE_METADATA,
                MemoryMutationAppliedOperation.NOOP,
            ),
            DreamOperationType.MERGE: (
                MemoryMutationOperation.MERGE,
                MemoryMutationAppliedOperation.MERGE,
            ),
            DreamOperationType.SYNTHESIZE: (
                MemoryMutationOperation.CORRECT,
                MemoryMutationAppliedOperation.CORRECT,
            ),
            DreamOperationType.RECOMPOSE: (
                MemoryMutationOperation.CORRECT,
                MemoryMutationAppliedOperation.CORRECT,
            ),
            DreamOperationType.CONTEST: (
                MemoryMutationOperation.CONTEST,
                MemoryMutationAppliedOperation.CONTEST,
            ),
            DreamOperationType.RESOLVE: (
                MemoryMutationOperation.RESTORE,
                MemoryMutationAppliedOperation.RESTORE,
            ),
        }[operation]

    async def mutate_resolved(
        self,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
        *,
        target: ResolvedSubject,
        additional_evidence: tuple[MemoryEvidenceCreate, ...] = (),
        self_reflection_result: tuple[int, str, int] | None = None,
    ) -> MemoryMutationResult:
        """Apply a trusted command/admin/plugin target through the same boundary."""

        if additional_evidence and not self._trusted_self_reflection(context):
            return self._rejected(request.operation, "evidence_bundle_requires_self_reflection")
        if additional_evidence and not await self._evidence_matches_conversation(
            additional_evidence,
            context.event,
        ):
            return self._rejected(request.operation, "cross_conversation_evidence")
        try:
            prepared = await self._prepare(request, context, target_override=target)
        except MemoryMutationRejected as exc:
            return self._rejected(
                request.operation,
                exc.reason_code,
                candidates=exc.candidates,
            )
        if self_reflection_result is not None and not self._trusted_self_reflection(context):
            return self._rejected(request.operation, "result_mapping_requires_self_reflection")
        return await self._commit_prepared(
            prepared,
            additional_evidence=additional_evidence,
            self_reflection_result=self_reflection_result,
        )

    async def _commit_prepared(
        self,
        prepared: _PreparedMutation,
        *,
        additional_evidence: tuple[MemoryEvidenceCreate, ...] = (),
        self_reflection_result: tuple[int, str, int] | None = None,
    ) -> MemoryMutationResult:
        request = prepared.request
        context = prepared.context
        async with self._lock:
            existing = await self._receipts.find(
                idempotency_key=prepared.idempotency_key,
                claim_fingerprint=prepared.claim_fingerprint,
            )
            if existing is not None:
                return MemoryMutationResult.from_receipt(
                    existing,
                    deduplicated=True,
                    requested_operation=request.operation,
                )
            claim_resolution: MemoryClaimResolution | None = None
            if request.operation in {
                MemoryMutationOperation.CREATE,
                MemoryMutationOperation.CORRECT,
            }:
                if prepared.claim is None:
                    raise MemoryMutationRejected("validated_claim_required")
                claim_resolution = await self._processor.resolve(
                    prepared.claim,
                    MemoryProcessingContext(
                        source=MemoryProcessingSource.LIVE,
                        event=context.event,
                    ),
                )
            try:
                async with self._facts.repository.transaction() as session:
                    duplicate = await self._receipts.find(
                        idempotency_key=prepared.idempotency_key,
                        claim_fingerprint=prepared.claim_fingerprint,
                        session=session,
                    )
                    if duplicate is not None:
                        return MemoryMutationResult.from_receipt(
                            duplicate,
                            deduplicated=True,
                            requested_operation=request.operation,
                        )
                    reserved = await self._receipts.reserve(
                        mutation_id=str(uuid.uuid4()),
                        idempotency_key=prepared.idempotency_key,
                        claim_fingerprint=prepared.claim_fingerprint,
                        target_fingerprint=prepared.target_fingerprint,
                        trigger_event_id=context.event.id,
                        conversation_key=context.conversation_key,
                        current_group_id=context.event.group_id,
                        turn_origin=context.turn_origin,
                        delegation_mode=context.delegation_mode,
                        trigger_actor_user_id=context.trigger_actor_user_id,
                        decision_actor_type=context.decision_actor_type,
                        decision_actor_id=context.decision_actor_id,
                        executed_by_bot_user_id=context.executed_by_bot_user_id,
                        requested_operation=request.operation,
                        created_at=datetime.now(UTC),
                        session=session,
                    )
                    applied = await self._apply(
                        prepared,
                        session=session,
                        claim_resolution=claim_resolution,
                    )
                    if applied.new_fact_id is not None and additional_evidence:
                        await self._facts.append_evidence_bundle(
                            applied.new_fact_id,
                            additional_evidence,
                            confirmed_at=context.event.occurred_at,
                            session=session,
                        )
                    if applied.new_fact_id is not None and self_reflection_result is not None:
                        run_id, result_kind, result_index = self_reflection_result
                        session.add(
                            MemorySelfReflectionResultModel(
                                run_id=run_id,
                                fact_id=applied.new_fact_id,
                                result_kind=result_kind,
                                result_index=result_index,
                                created_at=datetime.now(UTC),
                            )
                        )
                    receipt = await self._receipts.finalize(
                        reserved.id,
                        applied_operation=applied.operation,
                        old_fact_id=applied.old_fact_id,
                        new_fact_id=applied.new_fact_id,
                        outcome=applied.outcome,
                        reason_code=applied.reason_code,
                        session=session,
                    )
            except IntegrityError:
                duplicate = await self._receipts.find(
                    idempotency_key=prepared.idempotency_key,
                    claim_fingerprint=prepared.claim_fingerprint,
                )
                if duplicate is None:
                    raise
                return MemoryMutationResult.from_receipt(
                    duplicate,
                    deduplicated=True,
                    requested_operation=request.operation,
                )
        await self._schedule_embedding_after_commit(receipt.new_fact_id)
        return MemoryMutationResult.from_receipt(receipt, deduplicated=False)

    @staticmethod
    def _trusted_self_reflection(context: MemoryMutationContext) -> bool:
        return bool(
            context.turn_origin == "memory_self_reflection"
            and context.decision_actor_type is MemoryDecisionActorType.REFLECTION
            and context.decision_actor_id == "yuki_self_reflection"
        )

    async def _evidence_matches_conversation(
        self,
        evidence: tuple[MemoryEvidenceCreate, ...],
        anchor: EventRecord,
    ) -> bool:
        for item in evidence:
            if (
                item.authority is not MemoryAuthority.AGENT_REFLECTION
                or item.relation is not MemoryEvidenceRelation.AGENT_REFLECTION
            ):
                return False
            if item.event_id is None:
                continue
            event = await self._ledger.get_event(item.event_id)
            if event is None or event.bot_user_id != anchor.bot_user_id:
                return False
            if event.scope_type is not anchor.scope_type:
                return False
            if anchor.scope_type is ScopeType.GROUP:
                if event.group_id != anchor.group_id:
                    return False
            elif event.private_peer_user_id != anchor.private_peer_user_id:
                return False
        return True

    async def mutate_validated_claim(
        self,
        claim: ValidatedMemoryClaim,
        processing_context: MemoryProcessingContext,
        *,
        conversation_key: str,
    ) -> MemoryMutationResult:
        """Commit one Worker claim through the same receipt and transaction boundary."""

        event = processing_context.event
        operation = self._claim_requested_operation(claim.operation)
        if (
            event.direction != "inbound"
            or event.sender_user_id == event.bot_user_id
            or await self._ledger.sender_is_bot(event.sender_user_id)
            or not self._validated_claim_matches_event(claim, event)
        ):
            return self._rejected(operation, "untrusted_trigger_event")
        target_payload = {
            "scope_type": claim.fact.scope_type.value,
            "subject_user_id": claim.fact.subject_user_id,
            "group_id": claim.fact.group_id,
            "visibility_type": (
                claim.fact.visibility_type.value if claim.fact.visibility_type is not None else None
            ),
            "visibility_user_id": claim.fact.visibility_user_id,
            "visibility_group_id": claim.fact.visibility_group_id,
        }
        common = {
            "event_id": event.id,
            "target": target_payload,
            "memory_key": normalize_memory_text(claim.fact.memory_key, maximum=128),
            "content": normalize_memory_text(
                claim.fact.content,
                maximum=4000,
            ).casefold(),
        }
        claim_fingerprint = _fingerprint(common)
        idempotency_key = _fingerprint({**common, "operation": operation.value})
        target_fingerprint = _fingerprint(target_payload)
        async with self._lock:
            existing = await self._receipts.find(
                idempotency_key=idempotency_key,
                claim_fingerprint=claim_fingerprint,
            )
            if existing is not None:
                return MemoryMutationResult.from_receipt(
                    existing,
                    deduplicated=True,
                    requested_operation=operation,
                )
            claim_resolution = await self._processor.resolve(claim, processing_context)
            try:
                async with self._facts.repository.transaction() as session:
                    duplicate = await self._receipts.find(
                        idempotency_key=idempotency_key,
                        claim_fingerprint=claim_fingerprint,
                        session=session,
                    )
                    if duplicate is not None:
                        return MemoryMutationResult.from_receipt(
                            duplicate,
                            deduplicated=True,
                            requested_operation=operation,
                        )
                    reserved = await self._receipts.reserve(
                        mutation_id=str(uuid.uuid4()),
                        idempotency_key=idempotency_key,
                        claim_fingerprint=claim_fingerprint,
                        target_fingerprint=target_fingerprint,
                        trigger_event_id=event.id,
                        conversation_key=conversation_key,
                        current_group_id=event.group_id,
                        turn_origin=event.origin,
                        delegation_mode="automatic_extraction",
                        trigger_actor_user_id=event.sender_user_id,
                        decision_actor_type=MemoryDecisionActorType.WORKER,
                        decision_actor_id="memory_worker",
                        executed_by_bot_user_id=event.bot_user_id,
                        requested_operation=operation,
                        created_at=datetime.now(UTC),
                        session=session,
                    )
                    processed = await self._processor.apply_resolution(
                        claim_resolution,
                        session=session,
                    )
                    applied = self._claim_applied(
                        action=processed.action,
                        fact_id=processed.fact_id,
                        reason_code=processed.reason_code,
                    )
                    receipt = await self._receipts.finalize(
                        reserved.id,
                        applied_operation=applied.operation,
                        old_fact_id=applied.old_fact_id,
                        new_fact_id=applied.new_fact_id,
                        outcome=applied.outcome,
                        reason_code=applied.reason_code,
                        session=session,
                    )
            except IntegrityError:
                duplicate = await self._receipts.find(
                    idempotency_key=idempotency_key,
                    claim_fingerprint=claim_fingerprint,
                )
                if duplicate is None:
                    raise
                return MemoryMutationResult.from_receipt(
                    duplicate,
                    deduplicated=True,
                    requested_operation=operation,
                )
        await self._schedule_embedding_after_commit(receipt.new_fact_id)
        return MemoryMutationResult.from_receipt(receipt, deduplicated=False)

    async def mutate_reflection(
        self,
        fact: MemoryFact,
        *,
        operation: MemoryMutationOperation,
        reason: MemoryInvalidationReason | str,
        merge_fact_id: int | None = None,
    ) -> MemoryMutationResult:
        """Apply one bounded background-governance decision using existing evidence."""

        evidence_rows = await self._facts.list_evidence(fact.id, limit=20)
        evidence = next((row for row in evidence_rows if row.event_id is not None), None)
        if evidence is None or evidence.event_id is None:
            return self._rejected(operation, "reflection_evidence_not_found")
        event = await self._ledger.get_event(evidence.event_id)
        if event is None:
            return self._rejected(operation, "reflection_trigger_event_not_found")
        quote = (
            evidence.excerpt
            if evidence.excerpt and evidence.excerpt in event.content
            else event.content[:500]
        )
        reason_code = reason.value if isinstance(reason, MemoryInvalidationReason) else reason
        return await self.mutate_resolved(
            MemoryMutationRequest(
                operation=operation,
                fact_id=fact.id,
                merge_fact_id=merge_fact_id,
                target=MemoryMutationTarget(
                    subject_ref="current_speaker",
                    scope_type=fact.scope_type,
                ),
                reason=reason_code,
                evidence_quote=quote,
            ),
            MemoryMutationContext(
                event=event,
                conversation_key=(
                    f"group:{fact.group_id}:reflection"
                    if fact.group_id is not None
                    else f"private:{fact.subject_user_id}:reflection"
                ),
                turn_origin="memory_reflection",
                delegation_mode=f"reflection:{reason_code}"[:32],
                trigger_actor_user_id=event.sender_user_id,
                decision_actor_type=MemoryDecisionActorType.REFLECTION,
                decision_actor_id="memory_maintenance",
                executed_by_bot_user_id=event.bot_user_id,
            ),
            target=ResolvedSubject(
                fact.scope_type,
                fact.subject_user_id,
                fact.group_id,
            ),
        )

    async def _prepare(
        self,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
        *,
        target_override: ResolvedSubject | None = None,
    ) -> _PreparedMutation:
        event = context.event
        trusted_self_reflection = (
            context.turn_origin == "memory_self_reflection"
            and context.decision_actor_type is MemoryDecisionActorType.REFLECTION
            and context.decision_actor_id == "yuki_self_reflection"
            and event.direction in {"inbound", "outbound"}
            and (
                (event.direction == "inbound" and event.sender_user_id != event.bot_user_id)
                or (event.direction == "outbound" and event.sender_user_id == event.bot_user_id)
            )
        )
        if (
            (not trusted_self_reflection and event.direction != "inbound")
            or event.sender_user_id != context.trigger_actor_user_id
            or event.bot_user_id != context.executed_by_bot_user_id
            or (
                not trusted_self_reflection
                and (
                    event.sender_user_id == event.bot_user_id
                    or await self._ledger.sender_is_bot(event.sender_user_id)
                )
            )
        ):
            raise MemoryMutationRejected("untrusted_trigger_event")
        if tuple(dict.fromkeys(request.evidence_refs)) != ("current_event",):
            raise MemoryMutationRejected("unsupported_evidence_reference")
        fact = await self._load_fact(request.fact_id)
        merge_fact = await self._load_fact(request.merge_fact_id)
        if request.target is None:
            if fact is None:
                raise MemoryMutationRejected("target_required")
            if request.operation is MemoryMutationOperation.REASSIGN:
                raise MemoryMutationRejected("reassign_target_required")
            subject_ref = "existing_fact"
            target = target_override or self._target_from_fact(fact)
        else:
            subject_ref = self._normalize_subject_ref(request.target.subject_ref, event)
            resolved_target = target_override or self._subjects.resolve(
                event,
                subject_ref=subject_ref,
                scope_type=request.target.scope_type,
            )
            if resolved_target is None:
                raise MemoryMutationRejected("target_not_available_in_current_event")
            target = resolved_target
            if request.target.scope_type is not target.scope_type:
                raise MemoryMutationRejected("target_scope_mismatch")
        target = self._resolve_visibility(request, target, event, fact=fact)
        self._authorize(request.operation, target, context)
        if request.operation is MemoryMutationOperation.REASSIGN and request.selector is not None:
            raise MemoryMutationRejected("selector_not_supported_for_reassign")
        if request.operation is not MemoryMutationOperation.CREATE and fact is None:
            if request.selector is None:
                raise MemoryMutationRejected("memory_selector_required")
            fact = await self._locate_fact(
                target,
                request.selector,
                statuses=self._locator_statuses(request.operation, merge_target=False),
            )
            request = request.model_copy(update={"fact_id": fact.id, "selector": None})
        if request.operation is MemoryMutationOperation.MERGE and merge_fact is None:
            if request.merge_selector is None:
                raise MemoryMutationRejected("merge_selector_required")
            merge_fact = await self._locate_fact(
                target,
                request.merge_selector,
                statuses=self._locator_statuses(request.operation, merge_target=True),
            )
            request = request.model_copy(
                update={"merge_fact_id": merge_fact.id, "merge_selector": None}
            )
        self._validate_self_request(request, target, event, fact=fact, merge_fact=merge_fact)
        self._validate_fact_requirements(request, target, fact, merge_fact, context)
        quote = (
            normalize_memory_text(request.evidence_quote or "", maximum=500)
            if trusted_self_reflection and context.evidence_tool_receipt_id is not None
            else self._evidence_quote(request, event.content)
        )
        if not quote:
            raise MemoryMutationRejected("memory_evidence_quote_required")
        authority, source_type = self._provenance(target, context, request)
        evidence = MemoryEvidenceCreate(
            event_id=(None if context.evidence_tool_receipt_id is not None else event.id),
            tool_receipt_id=context.evidence_tool_receipt_id,
            source_speaker_user_id=(
                event.bot_user_id
                if context.evidence_tool_receipt_id is not None
                else event.sender_user_id
            ),
            relation=self._evidence_relation(request.operation, authority, source_type),
            confidence=request.confidence,
            authority=authority,
            excerpt=quote,
        )
        claim = self._validated_claim(
            request,
            context,
            subject_ref=subject_ref,
            fact=fact,
            source_type=source_type,
            evidence=evidence,
            target_override=target if target_override is not None else None,
            resolved_target=target,
        )
        content = normalize_memory_text(
            request.new_content or (fact.content if fact is not None else ""),
            maximum=4000,
        )
        key = normalize_memory_text(
            request.memory_key or (fact.memory_key if fact is not None else ""),
            maximum=128,
        )
        target_payload = {
            "scope_type": target.scope_type.value,
            "subject_user_id": target.subject_user_id,
            "group_id": target.group_id,
            "visibility_type": (
                target.visibility_type.value if target.visibility_type is not None else None
            ),
            "visibility_user_id": target.visibility_user_id,
            "visibility_group_id": target.visibility_group_id,
        }
        target_fingerprint = _fingerprint(target_payload)
        common = {
            "event_id": event.id,
            "target": target_payload,
            "memory_key": key,
            "content": content.casefold(),
        }
        if context.decision_actor_type in {
            MemoryDecisionActorType.REFLECTION,
            MemoryDecisionActorType.SYSTEM,
        }:
            common["decision_namespace"] = context.delegation_mode
        claim_fingerprint = _fingerprint(common)
        idempotency_key = _fingerprint(
            {
                **common,
                "operation": request.operation.value,
                "fact_id": request.fact_id,
                "merge_fact_id": request.merge_fact_id,
            }
        )
        return _PreparedMutation(
            request=request,
            context=context,
            subject_ref=subject_ref,
            target=target,
            fact=fact,
            merge_fact=merge_fact,
            evidence=evidence,
            claim=claim,
            idempotency_key=idempotency_key,
            claim_fingerprint=claim_fingerprint,
            target_fingerprint=target_fingerprint,
        )

    async def _apply(
        self,
        prepared: _PreparedMutation,
        *,
        session: AsyncSession,
        claim_resolution: MemoryClaimResolution | None = None,
    ) -> _AppliedMutation:
        operation = prepared.request.operation
        if operation in {MemoryMutationOperation.CREATE, MemoryMutationOperation.CORRECT}:
            claim = prepared.claim
            if claim is None:
                raise MemoryMutationRejected("validated_claim_required")
            if claim_resolution is None:
                raise MemoryMutationRejected("claim_resolution_required")
            processed = await self._processor.apply_resolution(
                claim_resolution,
                session=session,
            )
            return self._claim_result(
                prepared, processed.action, processed.fact_id, processed.reason_code
            )
        if operation is MemoryMutationOperation.CONTEST and prepared.request.new_content:
            claim = prepared.claim
            fact = prepared.fact
            if claim is None or fact is None:
                raise MemoryMutationRejected("contest_fact_required")
            result = await self._facts.apply_claim(
                claim,
                candidates=(
                    MemoryCandidate(
                        candidate_ref="candidate_1",
                        fact=fact,
                        exact_key=True,
                    ),
                ),
                plan=MemoryResolutionPlan(
                    action=MemoryResolutionAction.CONTEST,
                    existing_fact_id=fact.id,
                    new_fact_status=MemoryStatus.CONTESTED,
                    new_conflict_state=MemoryConflictState.CONTESTED,
                    existing_status=MemoryStatus.ACTIVE,
                    existing_conflict_state=MemoryConflictState.CONTESTED,
                    relation_types=(MemoryFactRelationType.CONTRADICTS,),
                    reason_code="agent_requested_contest",
                    append_evidence=True,
                    create_new_fact=True,
                ),
                limit=self._scope_limit(claim.fact.scope_type),
                session=session,
            )
            return _AppliedMutation(
                MemoryMutationAppliedOperation.CONTEST,
                MemoryMutationOutcome.COMMITTED_AS_CONTESTED,
                fact.id,
                result.id if result is not None else None,
                "agent_requested_contest",
            )
        if operation is MemoryMutationOperation.CONTEST:
            fact = self._required_fact(prepared)
            changed = await self._facts.contest_fact(
                fact.id,
                reason_code="agent_requested_contest",
                actor_user_id=prepared.context.trigger_actor_user_id,
                evidence=prepared.evidence,
                session=session,
            )
            return self._direct_result(
                operation=MemoryMutationAppliedOperation.CONTEST,
                changed=changed,
                old_fact_id=fact.id,
                new_fact_id=fact.id,
                reason_code="agent_requested_contest",
                contested=True,
            )
        if operation is MemoryMutationOperation.INVALIDATE:
            fact = self._required_fact(prepared)
            invalidation_reason = self._invalidation_reason(prepared)
            changed = await self._facts.invalidate_fact(
                fact.id,
                reason=invalidation_reason,
                actor_user_id=prepared.context.trigger_actor_user_id,
                evidence=prepared.evidence,
                session=session,
            )
            return self._direct_result(
                operation=MemoryMutationAppliedOperation.INVALIDATE,
                changed=changed,
                old_fact_id=fact.id,
                new_fact_id=None,
                reason_code=invalidation_reason.value,
            )
        if operation is MemoryMutationOperation.RESTORE:
            fact = self._required_fact(prepared)
            restored = await self._facts.restore_fact(
                fact.id,
                actor_user_id=prepared.context.trigger_actor_user_id,
                evidence=prepared.evidence,
                confirmed_at=prepared.context.event.occurred_at,
                session=session,
            )
            return self._direct_result(
                operation=MemoryMutationAppliedOperation.RESTORE,
                changed=restored is not None,
                old_fact_id=fact.id,
                new_fact_id=restored.id if restored is not None else None,
                reason_code="explicit_restore",
            )
        if operation is MemoryMutationOperation.MERGE:
            fact = self._required_fact(prepared)
            merge_fact = prepared.merge_fact
            if merge_fact is None:
                raise MemoryMutationRejected("merge_fact_required")
            merged = await self._facts.merge_facts(
                fact.id,
                merge_fact.id,
                actor_user_id=prepared.context.trigger_actor_user_id,
                evidence=prepared.evidence,
                confirmed_at=prepared.context.event.occurred_at,
                session=session,
            )
            return self._direct_result(
                operation=MemoryMutationAppliedOperation.MERGE,
                changed=merged is not None,
                old_fact_id=fact.id,
                new_fact_id=merged.id if merged is not None else None,
                reason_code="merged",
            )
        if operation in {
            MemoryMutationOperation.REASSIGN,
            MemoryMutationOperation.UPDATE_METADATA,
        }:
            return await self._version(prepared, session=session)
        raise MemoryMutationRejected("unsupported_operation")

    async def _version(
        self,
        prepared: _PreparedMutation,
        *,
        session: AsyncSession,
    ) -> _AppliedMutation:
        fact = self._required_fact(prepared)
        request = prepared.request
        reassign = request.operation is MemoryMutationOperation.REASSIGN
        target = (
            prepared.target
            if reassign
            else ResolvedSubject(
                fact.scope_type,
                fact.subject_user_id,
                fact.group_id,
                fact.visibility_type,
                fact.visibility_user_id,
                fact.visibility_group_id,
            )
        )
        authority, source_type = self._provenance(target, prepared.context, request)
        temporal = None
        if request.valid_from is not None or request.valid_until is not None:
            temporal = self._temporal.resolve(
                mode=(
                    MemoryTemporalMode.TEMPORARY
                    if request.valid_until is not None
                    else MemoryTemporalMode.PERSISTENT
                ),
                valid_from=request.valid_from,
                valid_until=request.valid_until,
                occurred_at=prepared.context.event.occurred_at,
                timezone_name=self._settings.default_timezone,
            )
        replacement = MemoryFactCreate(
            scope_type=target.scope_type,
            subject_user_id=target.subject_user_id,
            group_id=target.group_id,
            visibility_type=target.visibility_type,
            visibility_user_id=target.visibility_user_id,
            visibility_group_id=target.visibility_group_id,
            kind=request.kind or fact.kind,
            memory_key=normalize_memory_text(
                request.memory_key or fact.memory_key,
                maximum=128,
            ),
            category=normalize_memory_text(request.category or fact.category, maximum=64),
            content=normalize_memory_text(
                request.new_content or fact.content,
                maximum=4000,
            ),
            importance=request.importance or fact.importance,
            confidence=request.confidence,
            source_type=source_type,
            authority=authority,
            valid_from=temporal.valid_from if temporal is not None else fact.valid_from,
            valid_until=temporal.valid_until if temporal is not None else fact.valid_until,
            validation_version=fact.validation_version,
            last_audited_at=(
                datetime.now(UTC) if request.review_state is not None else fact.last_audited_at
            ),
            review_state=request.review_state or fact.review_state,
        )
        versioned = await self._facts.version_fact(
            fact.id,
            replacement=replacement,
            evidence=prepared.evidence,
            actor_user_id=prepared.context.trigger_actor_user_id,
            reason_code="memory_reassigned" if reassign else "metadata_updated",
            limit=self._scope_limit(replacement.scope_type),
            copy_existing_evidence=True,
            copied_evidence_authority=authority if reassign else None,
            confirmed_at=prepared.context.event.occurred_at,
            session=session,
        )
        operation = (
            MemoryMutationAppliedOperation.REASSIGN
            if reassign
            else MemoryMutationAppliedOperation.UPDATE_METADATA
        )
        return self._direct_result(
            operation=operation,
            changed=versioned is not None,
            old_fact_id=fact.id,
            new_fact_id=versioned.id if versioned is not None else None,
            reason_code="memory_reassigned" if reassign else "metadata_updated",
        )

    def _validated_claim(
        self,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
        *,
        subject_ref: str,
        fact: MemoryFact | None,
        source_type: MemorySourceType,
        evidence: MemoryEvidenceCreate,
        target_override: ResolvedSubject | None,
        resolved_target: ResolvedSubject,
    ) -> ValidatedMemoryClaim | None:
        if request.operation not in {
            MemoryMutationOperation.CREATE,
            MemoryMutationOperation.CORRECT,
            MemoryMutationOperation.CONTEST,
        }:
            return None
        if request.operation is MemoryMutationOperation.CONTEST and request.new_content is None:
            return None
        content = normalize_memory_text(request.new_content or "", maximum=4000)
        key = normalize_memory_text(
            request.memory_key or (fact.memory_key if fact is not None else ""),
            maximum=128,
        )
        category = normalize_memory_text(
            request.category or (fact.category if fact is not None else ""),
            maximum=64,
        )
        if not content or not key or not category:
            raise MemoryMutationRejected("memory_content_key_and_category_required")
        quote = evidence.excerpt
        claim = MemoryClaim(
            operation=(
                MemoryClaimOperation.ASSERT
                if request.operation is MemoryMutationOperation.CREATE
                else MemoryClaimOperation.CORRECT
            ),
            subject_ref=subject_ref,
            scope_type=resolved_target.scope_type,
            kind=request.kind or (fact.kind if fact is not None else MemoryKind.FACT),
            memory_key=key,
            category=category,
            content=content,
            evidence_quote=quote,
            importance=request.importance or (fact.importance if fact is not None else 3),
            confidence=request.confidence,
            source_type=source_type,
            subject_basis=(
                MemorySubjectBasis.GROUP
                if resolved_target.scope_type is MemoryScopeType.GROUP
                else MemorySubjectBasis.REPLY_SUBJECT
                if subject_ref == "reply_author"
                else MemorySubjectBasis.MENTIONED_SUBJECT
                if subject_ref.startswith("mentioned_")
                else MemorySubjectBasis.OMITTED_SELF
            ),
            temporal_mode=(
                MemoryTemporalMode.TEMPORARY
                if request.valid_until is not None
                else MemoryTemporalMode.PERSISTENT
            ),
            valid_from=request.valid_from,
            valid_until=request.valid_until,
        )
        direct_target = target_override or (
            resolved_target
            if request.target is None or resolved_target.scope_type is MemoryScopeType.SELF
            else None
        )
        if direct_target is not None:
            try:
                temporal = self._temporal.resolve(
                    mode=claim.temporal_mode,
                    valid_from=claim.valid_from,
                    valid_until=claim.valid_until,
                    occurred_at=context.event.occurred_at,
                    timezone_name=self._settings.default_timezone,
                )
            except ValueError as exc:
                raise MemoryMutationRejected("invalid_memory_temporal_range") from exc
            authority, _source_type = self._provenance(direct_target, context, request)
            return ValidatedMemoryClaim(
                operation=claim.operation,
                fact=MemoryFactCreate(
                    scope_type=direct_target.scope_type,
                    subject_user_id=direct_target.subject_user_id,
                    group_id=direct_target.group_id,
                    visibility_type=direct_target.visibility_type,
                    visibility_user_id=direct_target.visibility_user_id,
                    visibility_group_id=direct_target.visibility_group_id,
                    kind=claim.kind,
                    memory_key=key,
                    category=category,
                    content=content,
                    importance=claim.importance,
                    confidence=claim.confidence,
                    source_type=source_type,
                    authority=authority,
                    valid_from=temporal.valid_from,
                    valid_until=temporal.valid_until,
                    last_audited_at=(
                        datetime.now(UTC) if request.review_state is not None else None
                    ),
                    review_state=request.review_state or MemoryReviewState.VERIFIED,
                ),
                evidence=evidence,
                subject_is_speaker=(direct_target.subject_user_id == context.event.sender_user_id),
                occurred_at=context.event.occurred_at,
            )
        validated = self._processor.validate(claim, context.event)
        if validated is None:
            raise MemoryMutationRejected("claim_not_supported_by_current_event")
        return validated

    async def _load_fact(self, fact_id: int | None) -> MemoryFact | None:
        if fact_id is None:
            return None
        fact = await self._facts.get_fact(fact_id)
        if fact is None:
            raise MemoryMutationRejected("memory_fact_not_found")
        return fact

    async def _locate_fact(
        self,
        target: ResolvedSubject,
        selector: MemoryMutationSelector,
        *,
        statuses: tuple[MemoryStatus, ...],
    ) -> MemoryFact:
        memory_key = (
            normalize_memory_text(selector.memory_key, maximum=128)
            if selector.memory_key is not None
            else None
        )
        normalized_content = (
            normalize_memory_text(selector.old_content, maximum=4000)
            if selector.old_content is not None
            else None
        )
        category = (
            normalize_memory_text(selector.category, maximum=64)
            if selector.category is not None
            else None
        )
        if memory_key == "":
            memory_key = None
        if normalized_content == "":
            normalized_content = None
        if memory_key is None and normalized_content is None:
            raise MemoryMutationRejected("invalid_memory_selector")
        rows = await self._facts.repository.list_mutation_locator_candidates(
            MemoryFactQuery(
                scope_type=target.scope_type,
                subject_user_id=target.subject_user_id,
                group_id=target.group_id,
                visibility_type=target.visibility_type,
                visibility_user_id=target.visibility_user_id,
                visibility_group_id=target.visibility_group_id,
            ),
            memory_key=memory_key,
            normalized_content=normalized_content,
            category=category,
            statuses=statuses,
            limit=4,
        )
        exact = tuple(
            fact
            for fact in rows
            if (memory_key is None or fact.memory_key == memory_key)
            and (normalized_content is None or fact.normalized_content == normalized_content)
            and (category is None or fact.category == category)
        )
        if len(exact) == 1:
            self._facts.metrics.increment("memory_mutation_locator_unique_count")
            return exact[0]
        candidates = tuple(
            MemoryMutationCandidate(
                fact_id=fact.id,
                memory_ref=f"M{fact.id}",
                memory_key=fact.memory_key,
                category=fact.category,
                kind=fact.kind,
                content=fact.content,
                status=fact.status,
            )
            for fact in rows[:3]
        )
        if candidates:
            self._facts.metrics.increment("memory_mutation_locator_ambiguous_count")
            raise MemoryMutationRejected(
                "memory_candidate_ambiguous",
                candidates=candidates,
            )
        self._facts.metrics.increment("memory_mutation_locator_not_found_count")
        raise MemoryMutationRejected("memory_candidate_not_found")

    @staticmethod
    def _locator_statuses(
        operation: MemoryMutationOperation,
        *,
        merge_target: bool,
    ) -> tuple[MemoryStatus, ...]:
        if operation is MemoryMutationOperation.RESTORE:
            return (MemoryStatus.INVALIDATED,)
        if operation is MemoryMutationOperation.MERGE and merge_target:
            return (MemoryStatus.ACTIVE,)
        return (MemoryStatus.ACTIVE, MemoryStatus.CONTESTED)

    def _resolve_visibility(
        self,
        request: MemoryMutationRequest,
        target: ResolvedSubject,
        event: EventRecord,
        *,
        fact: MemoryFact | None,
    ) -> ResolvedSubject:
        if target.scope_type is not MemoryScopeType.SELF:
            if request.visibility is not None:
                raise MemoryMutationRejected("visibility_only_valid_for_self_memory")
            return target
        if not self._settings.self_memory_enabled:
            raise MemoryMutationRejected("self_memory_disabled")
        if request.target is None:
            if fact is None or fact.scope_type is not MemoryScopeType.SELF:
                raise MemoryMutationRejected("self_memory_requires_self_subject_ref")
        elif request.target.subject_ref.strip().casefold() != "self":
            raise MemoryMutationRejected("self_memory_requires_self_subject_ref")
        if (
            request.visibility is None
            and fact is not None
            and fact.scope_type is MemoryScopeType.SELF
        ):
            return ResolvedSubject(
                MemoryScopeType.SELF,
                None,
                None,
                fact.visibility_type,
                fact.visibility_user_id,
                fact.visibility_group_id,
            )
        mode = request.visibility or SelfMemoryVisibilityMode.CURRENT_SCOPE
        if mode is SelfMemoryVisibilityMode.GLOBAL:
            return ResolvedSubject(
                MemoryScopeType.SELF,
                None,
                None,
                SelfMemoryVisibility.GLOBAL,
            )
        if target.visibility_type is not None:
            return target
        if event.scope_type is ScopeType.PRIVATE or event.group_id is None:
            return ResolvedSubject(
                MemoryScopeType.SELF,
                None,
                None,
                SelfMemoryVisibility.PRIVATE,
                event.sender_user_id,
                None,
            )
        return ResolvedSubject(
            MemoryScopeType.SELF,
            None,
            None,
            SelfMemoryVisibility.GROUP,
            None,
            event.group_id,
        )

    @staticmethod
    def _validate_self_request(
        request: MemoryMutationRequest,
        target: ResolvedSubject,
        event: EventRecord,
        *,
        fact: MemoryFact | None,
        merge_fact: MemoryFact | None,
    ) -> None:
        if target.scope_type is not MemoryScopeType.SELF:
            return
        category = normalize_memory_text(
            request.category or (fact.category if fact is not None else ""),
            maximum=64,
        ).casefold()
        if category and category not in SELF_MEMORY_CATEGORIES:
            raise MemoryMutationRejected("invalid_self_memory_category")
        keys = (
            request.memory_key,
            fact.memory_key if fact is not None else None,
            merge_fact.memory_key if merge_fact is not None else None,
        )
        if any(
            MemoryMutationService._is_protected_self_key(key) for key in keys if key is not None
        ):
            raise MemoryMutationRejected("protected_self_memory_key")
        kind = request.kind or (fact.kind if fact is not None else MemoryKind.FACT)
        if request.operation in {
            MemoryMutationOperation.CREATE,
            MemoryMutationOperation.CORRECT,
        } and ((kind is MemoryKind.EPISODE) != (category == "self_episode")):
            raise MemoryMutationRejected("self_episode_kind_category_mismatch")
        if target.visibility_type is SelfMemoryVisibility.GLOBAL:
            if kind is MemoryKind.EPISODE or category == "self_episode":
                raise MemoryMutationRejected("self_episode_cannot_be_global")
            if event.scope_type is ScopeType.PRIVATE and category not in {
                "self_preference",
                "self_reflection",
                "self_principle",
            }:
                raise MemoryMutationRejected("private_self_fact_cannot_be_global")

    @staticmethod
    def _is_protected_self_key(value: str) -> bool:
        key = normalize_memory_text(value, maximum=128).casefold()
        if key in {"identity:name", "identity:age", "identity:birthday"}:
            return True
        return key.startswith(
            (
                "identity:appearance:",
                "core:",
                "safety:",
                "system:",
                "permission:",
                "runtime:",
            )
        )

    @staticmethod
    def _validate_fact_requirements(
        request: MemoryMutationRequest,
        target: ResolvedSubject,
        fact: MemoryFact | None,
        merge_fact: MemoryFact | None,
        context: MemoryMutationContext,
    ) -> None:
        if request.review_state is not None and context.decision_actor_type not in {
            MemoryDecisionActorType.SYSTEM,
            MemoryDecisionActorType.REFLECTION,
        }:
            raise MemoryMutationRejected("review_state_requires_internal_auditor")
        if request.review_state is not None and request.operation not in {
            MemoryMutationOperation.UPDATE_METADATA,
            MemoryMutationOperation.CORRECT,
        }:
            raise MemoryMutationRejected("review_state_requires_metadata_update")
        if request.operation is MemoryMutationOperation.CREATE:
            if fact is not None or merge_fact is not None:
                raise MemoryMutationRejected("create_does_not_accept_fact_id")
            return
        if fact is None:
            raise MemoryMutationRejected("fact_id_required")
        if (
            request.expected_fact_state is not None
            and fact.status is not request.expected_fact_state
        ):
            raise MemoryMutationRejected("expected_fact_state_mismatch")
        fact_target = (
            fact.scope_type,
            fact.subject_user_id,
            fact.group_id,
            fact.visibility_type,
            fact.visibility_user_id,
            fact.visibility_group_id,
        )
        requested_target = (
            target.scope_type,
            target.subject_user_id,
            target.group_id,
            target.visibility_type,
            target.visibility_user_id,
            target.visibility_group_id,
        )
        if (
            request.operation is not MemoryMutationOperation.REASSIGN
            and fact_target != requested_target
        ):
            raise MemoryMutationRejected("fact_target_mismatch")
        if request.operation is MemoryMutationOperation.REASSIGN:
            if (
                context.event.group_id is None
                or fact.scope_type is not MemoryScopeType.PERSON_GROUP
                or fact.group_id != context.event.group_id
                or target.scope_type is not MemoryScopeType.PERSON_GROUP
                or target.group_id != context.event.group_id
            ):
                raise MemoryMutationRejected("reassign_must_remain_in_current_group")
            if (
                not context.actor_is_superuser
                and fact.subject_user_id != context.trigger_actor_user_id
                and fact.authority in {MemoryAuthority.EXPLICIT, MemoryAuthority.SELF_REPORT}
            ):
                raise MemoryMutationRejected("third_party_cannot_reassign_subject_owned_fact")
        if request.operation is MemoryMutationOperation.MERGE:
            if merge_fact is None:
                raise MemoryMutationRejected("merge_fact_required")
            merge_target = (
                merge_fact.scope_type,
                merge_fact.subject_user_id,
                merge_fact.group_id,
                merge_fact.visibility_type,
                merge_fact.visibility_user_id,
                merge_fact.visibility_group_id,
            )
            if merge_target != requested_target:
                raise MemoryMutationRejected("merge_target_mismatch")
        elif merge_fact is not None:
            raise MemoryMutationRejected("merge_fact_id_only_valid_for_merge")

    @staticmethod
    def _authorize(
        operation: MemoryMutationOperation,
        target: ResolvedSubject,
        context: MemoryMutationContext,
    ) -> None:
        if context.actor_is_superuser or context.decision_actor_type in {
            MemoryDecisionActorType.REFLECTION,
            MemoryDecisionActorType.SYSTEM,
        }:
            return
        if target.scope_type is MemoryScopeType.SELF:
            if context.decision_actor_type is not MemoryDecisionActorType.AGENT:
                raise MemoryMutationRejected("self_memory_requires_agent_judgment")
            allowed = {
                MemoryMutationOperation.CREATE,
                MemoryMutationOperation.CORRECT,
                MemoryMutationOperation.INVALIDATE,
                MemoryMutationOperation.RESTORE,
                MemoryMutationOperation.CONTEST,
                MemoryMutationOperation.MERGE,
            }
            if operation not in allowed:
                raise MemoryMutationRejected("operation_not_allowed_for_self_memory")
            return
        if target.subject_user_id == context.trigger_actor_user_id:
            allowed = {
                MemoryMutationOperation.CREATE,
                MemoryMutationOperation.CORRECT,
                MemoryMutationOperation.INVALIDATE,
                MemoryMutationOperation.RESTORE,
                MemoryMutationOperation.CONTEST,
                MemoryMutationOperation.MERGE,
                MemoryMutationOperation.UPDATE_METADATA,
            }
        elif target.scope_type is MemoryScopeType.GROUP:
            allowed = {
                MemoryMutationOperation.CREATE,
                MemoryMutationOperation.CORRECT,
                MemoryMutationOperation.INVALIDATE,
                MemoryMutationOperation.CONTEST,
                MemoryMutationOperation.MERGE,
                MemoryMutationOperation.UPDATE_METADATA,
            }
        else:
            allowed = {
                MemoryMutationOperation.CREATE,
                MemoryMutationOperation.CORRECT,
                MemoryMutationOperation.CONTEST,
                MemoryMutationOperation.MERGE,
                MemoryMutationOperation.REASSIGN,
            }
        if operation not in allowed:
            raise MemoryMutationRejected("operation_not_allowed_for_target")

    @staticmethod
    def _normalize_subject_ref(subject_ref: str, event: EventRecord) -> str:
        normalized = subject_ref.strip().casefold()
        aliases = {
            "current_speaker": "speaker",
            "current_group": "group",
            "replied_message_author": "reply_author",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized == "mentioned_user":
            available = tuple(
                item
                for item in SubjectResolver.available(event)
                if item.subject_ref.startswith("mentioned_")
            )
            if len(available) != 1:
                raise MemoryMutationRejected("mentioned_user_is_ambiguous")
            return available[0].subject_ref
        if normalized.startswith("mentioned_user_"):
            return "mentioned_" + normalized.removeprefix("mentioned_user_")
        return normalized

    @staticmethod
    def _evidence_quote(request: MemoryMutationRequest, event_content: str) -> str:
        source = normalize_memory_text(event_content, maximum=4000)
        if not source:
            raise MemoryMutationRejected("empty_trigger_event")
        if request.evidence_quote is None:
            if len(source) > 500:
                raise MemoryMutationRejected("evidence_quote_required_for_long_event")
            return source
        quote = normalize_memory_text(request.evidence_quote, maximum=500)
        if not quote or quote not in source:
            raise MemoryMutationRejected("evidence_quote_not_in_current_event")
        return quote

    @staticmethod
    def _provenance(
        target: ResolvedSubject,
        context: MemoryMutationContext,
        request: MemoryMutationRequest,
    ) -> tuple[MemoryAuthority, MemorySourceType]:
        if target.scope_type is MemoryScopeType.SELF:
            return MemoryAuthority.AGENT_REFLECTION, MemorySourceType.AUTOMATIC
        if target.subject_user_id and target.subject_user_id != context.trigger_actor_user_id:
            return MemoryAuthority.THIRD_PARTY, MemorySourceType.AUTOMATIC
        if target.scope_type is MemoryScopeType.GROUP:
            return MemoryAuthority.GROUP_REPORT, MemorySourceType.AUTOMATIC
        if (
            context.decision_actor_type
            not in {MemoryDecisionActorType.REFLECTION, MemoryDecisionActorType.SYSTEM}
            and request.request_basis is MemoryMutationRequestBasis.USER_REQUESTED
        ):
            return MemoryAuthority.EXPLICIT, MemorySourceType.EXPLICIT
        return MemoryAuthority.SELF_REPORT, MemorySourceType.AUTOMATIC

    @staticmethod
    def _evidence_relation(
        operation: MemoryMutationOperation,
        authority: MemoryAuthority,
        source_type: MemorySourceType,
    ) -> MemoryEvidenceRelation:
        if authority is MemoryAuthority.AGENT_REFLECTION:
            return MemoryEvidenceRelation.AGENT_REFLECTION
        if operation is MemoryMutationOperation.INVALIDATE:
            return MemoryEvidenceRelation.RETRACTION
        if operation in {
            MemoryMutationOperation.CORRECT,
            MemoryMutationOperation.CONTEST,
            MemoryMutationOperation.REASSIGN,
            MemoryMutationOperation.UPDATE_METADATA,
        }:
            return MemoryEvidenceRelation.CORRECTION
        if operation in {MemoryMutationOperation.RESTORE, MemoryMutationOperation.MERGE}:
            return MemoryEvidenceRelation.CONFIRMATION
        if source_type is MemorySourceType.EXPLICIT:
            return MemoryEvidenceRelation.EXPLICIT_COMMAND
        if authority is MemoryAuthority.THIRD_PARTY:
            return MemoryEvidenceRelation.THIRD_PARTY_STATEMENT
        if authority is MemoryAuthority.GROUP_REPORT:
            return MemoryEvidenceRelation.GROUP_STATEMENT
        return MemoryEvidenceRelation.SELF_STATEMENT

    def _scope_limit(self, scope_type: MemoryScopeType) -> int:
        if scope_type is MemoryScopeType.PERSON:
            return self._settings.person_memory_max_entries
        if scope_type is MemoryScopeType.GROUP:
            return self._settings.group_memory_max_entries
        if scope_type is MemoryScopeType.SELF:
            return self._settings.person_memory_max_entries
        return self._settings.person_group_memory_max_entries

    @staticmethod
    def _invalidation_reason(prepared: _PreparedMutation) -> MemoryInvalidationReason:
        context = prepared.context
        requested = prepared.request.reason
        if context.decision_actor_type is MemoryDecisionActorType.PLUGIN:
            return MemoryInvalidationReason.PLUGIN_EXPLICIT_INVALIDATION
        if (
            context.decision_actor_type
            in {MemoryDecisionActorType.AGENT, MemoryDecisionActorType.COMMAND}
            and prepared.request.request_basis is MemoryMutationRequestBasis.USER_REQUESTED
            and prepared.target.subject_user_id == context.trigger_actor_user_id
        ):
            return MemoryInvalidationReason.USER_RETRACTED
        if context.actor_is_superuser:
            try:
                return MemoryInvalidationReason(requested)
            except ValueError:
                return MemoryInvalidationReason.ADMINISTRATOR_INVALIDATED
        if context.decision_actor_type in {
            MemoryDecisionActorType.REFLECTION,
            MemoryDecisionActorType.SYSTEM,
        }:
            try:
                return MemoryInvalidationReason(requested)
            except ValueError:
                return MemoryInvalidationReason.STALE
        return MemoryInvalidationReason.USER_RETRACTED

    @staticmethod
    def _required_fact(prepared: _PreparedMutation) -> MemoryFact:
        if prepared.fact is None:
            raise MemoryMutationRejected("fact_id_required")
        return prepared.fact

    @staticmethod
    def _target_from_fact(fact: MemoryFact) -> ResolvedSubject:
        return ResolvedSubject(
            fact.scope_type,
            fact.subject_user_id,
            fact.group_id,
            fact.visibility_type,
            fact.visibility_user_id,
            fact.visibility_group_id,
        )

    @staticmethod
    def _claim_result(
        prepared: _PreparedMutation,
        action: MemoryResolutionAction,
        fact_id: int | None,
        reason_code: str,
    ) -> _AppliedMutation:
        return MemoryMutationService._claim_applied(
            action=action,
            fact_id=fact_id,
            reason_code=reason_code,
            old_fact_id=prepared.fact.id if prepared.fact is not None else None,
        )

    @staticmethod
    def _claim_applied(
        *,
        action: MemoryResolutionAction,
        fact_id: int | None,
        reason_code: str,
        old_fact_id: int | None = None,
    ) -> _AppliedMutation:
        if action is MemoryResolutionAction.CREATE:
            applied = MemoryMutationAppliedOperation.CREATE
            outcome = MemoryMutationOutcome.COMMITTED
            new_fact_id = fact_id
        elif action is MemoryResolutionAction.MERGE_EVIDENCE:
            applied = MemoryMutationAppliedOperation.MERGE_EVIDENCE
            outcome = MemoryMutationOutcome.COMMITTED
            old_fact_id = fact_id
            new_fact_id = fact_id
        elif action is MemoryResolutionAction.SUPERSEDE:
            applied = MemoryMutationAppliedOperation.CORRECT
            outcome = MemoryMutationOutcome.COMMITTED
            new_fact_id = fact_id
        elif action is MemoryResolutionAction.CONTEST:
            applied = MemoryMutationAppliedOperation.CONTEST
            outcome = MemoryMutationOutcome.COMMITTED_AS_CONTESTED
            new_fact_id = fact_id
        elif action is MemoryResolutionAction.INVALIDATE:
            applied = MemoryMutationAppliedOperation.INVALIDATE
            outcome = MemoryMutationOutcome.COMMITTED
            old_fact_id = fact_id or old_fact_id
            new_fact_id = None
        else:
            applied = MemoryMutationAppliedOperation.NOOP
            outcome = MemoryMutationOutcome.NO_CHANGE
            new_fact_id = None
        return _AppliedMutation(applied, outcome, old_fact_id, new_fact_id, reason_code)

    @staticmethod
    def _claim_requested_operation(
        operation: MemoryClaimOperation,
    ) -> MemoryMutationOperation:
        if operation is MemoryClaimOperation.CORRECT:
            return MemoryMutationOperation.CORRECT
        if operation is MemoryClaimOperation.RETRACT:
            return MemoryMutationOperation.INVALIDATE
        return MemoryMutationOperation.CREATE

    @staticmethod
    def _validated_claim_matches_event(
        claim: ValidatedMemoryClaim,
        event: EventRecord,
    ) -> bool:
        if (
            claim.evidence.event_id != event.id
            or claim.evidence.source_speaker_user_id != event.sender_user_id
        ):
            return False
        fact = claim.fact
        if fact.scope_type is MemoryScopeType.PERSON:
            return fact.subject_user_id == event.sender_user_id and fact.group_id is None
        if fact.scope_type is MemoryScopeType.GROUP:
            return fact.subject_user_id is None and fact.group_id == event.group_id
        referenced = {*event.mentioned_user_ids}
        if event.reply_sender_user_id:
            referenced.add(event.reply_sender_user_id)
        return bool(
            fact.scope_type is MemoryScopeType.PERSON_GROUP
            and fact.group_id is not None
            and fact.group_id == event.group_id
            and fact.subject_user_id != event.bot_user_id
            and fact.subject_user_id in {event.sender_user_id, *referenced}
        )

    @staticmethod
    def _direct_result(
        *,
        operation: MemoryMutationAppliedOperation,
        changed: bool,
        old_fact_id: int | None,
        new_fact_id: int | None,
        reason_code: str,
        contested: bool = False,
    ) -> _AppliedMutation:
        return _AppliedMutation(
            operation if changed else MemoryMutationAppliedOperation.NOOP,
            (
                MemoryMutationOutcome.COMMITTED_AS_CONTESTED
                if changed and contested
                else MemoryMutationOutcome.COMMITTED
                if changed
                else MemoryMutationOutcome.NO_CHANGE
            ),
            old_fact_id,
            new_fact_id if changed else None,
            reason_code if changed else "no_state_change",
        )

    async def _schedule_embedding_after_commit(self, fact_id: int | None) -> None:
        if fact_id is None:
            return
        fact = await self._facts.get_fact(fact_id)
        if fact is None or fact.status is not MemoryStatus.ACTIVE:
            return
        try:
            await self._facts.schedule_embedding(fact_id)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning(
                "memory_mutation_embedding_schedule_failed fact_id=%d category=%s",
                fact_id,
                type(exc).__name__,
            )

    @staticmethod
    def _rejected(
        operation: MemoryMutationOperation,
        reason_code: str,
        *,
        candidates: tuple[MemoryMutationCandidate, ...] = (),
    ) -> MemoryMutationResult:
        return MemoryMutationResult(
            ok=False,
            mutation_id=None,
            requested_operation=operation,
            applied_operation=MemoryMutationAppliedOperation.NOOP,
            outcome=MemoryMutationOutcome.REJECTED,
            reason_code=reason_code,
            candidates=candidates,
        )


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
