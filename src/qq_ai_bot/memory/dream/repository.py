"""Transactional persistence and exact-partition snapshot loading for Memory Dream."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamClusterModel,
    MemoryDreamClusterPreviewModel,
    MemoryDreamFactCheckpointModel,
    MemoryDreamOperationModel,
    MemoryDreamOperationResultModel,
    MemoryDreamOperationSourceModel,
    MemoryDreamRunModel,
    MemoryDreamRuntimeModel,
    MemoryEvidenceCompactionItemModel,
    MemoryEvidenceCompactionRunModel,
)
from qq_ai_bot.memory.dream.models import (
    DreamCluster,
    DreamClusterStatus,
    DreamHealth,
    DreamOperationStatus,
    DreamOperationSummary,
    DreamOperationType,
    DreamOutput,
    DreamPlanStatistics,
    DreamRun,
    DreamRunMode,
    DreamRunPage,
    DreamRunStatus,
)
from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.models import EmbeddingVector
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
from qq_ai_bot.memory.enums import MemoryStatus
from qq_ai_bot.memory.models import MemoryFact
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEmbeddingModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryToolReceiptModel,
)

_DREAM_PREVIEW_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class DreamCandidate:
    fact: MemoryFact
    bot_user_id: str
    vector: EmbeddingVector
    signature: str

    @property
    def partition_identity(self) -> tuple[object, ...]:
        fact = self.fact
        return (
            self.bot_user_id,
            fact.scope_type.value,
            fact.subject_user_id,
            fact.group_id,
            fact.visibility_type.value if fact.visibility_type is not None else None,
            fact.visibility_user_id,
            fact.visibility_group_id,
            fact.kind.value,
        )


@dataclass(frozen=True, slots=True)
class DreamCandidateLoad:
    candidates: tuple[DreamCandidate, ...]
    fact_signatures: tuple[tuple[int, str], ...]
    eligible_facts: int
    missing_embeddings: int
    ambiguous_bot_facts: int


def fact_signature(fact: MemoryFact) -> str:
    payload = {
        "id": fact.id,
        "content": fact.content,
        "memory_key": fact.memory_key,
        "category": fact.category,
        "importance": fact.importance,
        "confidence": fact.confidence,
        "source_type": fact.source_type.value,
        "authority": fact.authority.value,
        "status": fact.status.value,
        "conflict_state": fact.conflict_state.value,
        "valid_from": fact.valid_from.isoformat() if fact.valid_from else None,
        "valid_until": fact.valid_until.isoformat() if fact.valid_until else None,
        "evidence_count": fact.evidence_count,
        "updated_at": fact.updated_at.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class DreamRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self._facts = MemoryFactRepository(database)
        self._codec = Float32VectorCodec()

    async def load_candidates(
        self,
        *,
        profile_id: int,
        dimensions: int,
        documents: EmbeddingDocumentBuilder,
        maximum_fact_id: int | None = None,
    ) -> DreamCandidateLoad:
        now = datetime.now(UTC)
        conditions: list[Any] = [
            MemoryFactModel.status.in_((MemoryStatus.ACTIVE.value, MemoryStatus.CONTESTED.value)),
            MemoryFactModel.review_state != "quarantined",
            or_(MemoryFactModel.valid_until.is_(None), MemoryFactModel.valid_until > now),
        ]
        if maximum_fact_id is not None:
            conditions.append(MemoryFactModel.id <= maximum_fact_id)
        async with self.database.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        MemoryFactModel.id,
                        MemoryEmbeddingModel.content_hash,
                        MemoryEmbeddingModel.vector_blob,
                    )
                    .outerjoin(
                        MemoryEmbeddingModel,
                        (MemoryEmbeddingModel.fact_id == MemoryFactModel.id)
                        & (MemoryEmbeddingModel.profile_id == profile_id),
                    )
                    .where(*conditions)
                    .order_by(MemoryFactModel.id)
                )
            ).all()
            candidates: list[DreamCandidate] = []
            signatures: list[tuple[int, str]] = []
            missing = 0
            ambiguous = 0
            for row in rows:
                fact = await self._facts.get_fact(int(row.id), session=session)
                if fact is None:
                    continue
                signature = fact_signature(fact)
                signatures.append((fact.id, signature))
                if row.vector_blob is None or row.content_hash is None:
                    missing += 1
                    continue
                expected_hash = documents.content_hash_fields(
                    kind=fact.kind.value,
                    category=fact.category,
                    memory_key=fact.memory_key,
                    content=fact.content,
                )
                if str(row.content_hash) != expected_hash:
                    missing += 1
                    continue
                bot_ids = await self._fact_bot_ids(fact.id, session=session)
                if len(bot_ids) != 1:
                    ambiguous += 1
                    continue
                candidates.append(
                    DreamCandidate(
                        fact=fact,
                        bot_user_id=next(iter(bot_ids)),
                        vector=self._codec.decode(bytes(row.vector_blob), dimensions=dimensions),
                        signature=signature,
                    )
                )
        return DreamCandidateLoad(
            candidates=tuple(candidates),
            fact_signatures=tuple(signatures),
            eligible_facts=len(rows),
            missing_embeddings=missing,
            ambiguous_bot_facts=ambiguous,
        )

    @staticmethod
    async def _fact_bot_ids(fact_id: int, *, session: AsyncSession) -> set[str]:
        event_ids = set(
            await session.scalars(
                select(ChatEventModel.bot_user_id)
                .join(MemoryEvidenceModel, MemoryEvidenceModel.event_id == ChatEventModel.id)
                .where(MemoryEvidenceModel.fact_id == fact_id)
            )
        )
        tool_ids = set(
            await session.scalars(
                select(MemoryToolReceiptModel.bot_user_id)
                .join(
                    MemoryEvidenceModel,
                    MemoryEvidenceModel.tool_receipt_id == MemoryToolReceiptModel.id,
                )
                .where(MemoryEvidenceModel.fact_id == fact_id)
            )
        )
        return {str(item) for item in (*event_ids, *tool_ids) if item}

    async def checkpoint_map(self) -> dict[int, str]:
        async with self.database.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        MemoryDreamFactCheckpointModel.fact_id,
                        MemoryDreamFactCheckpointModel.signature,
                    )
                )
            ).all()
        return {int(row.fact_id): str(row.signature) for row in rows}

    async def initialize_baseline(self, fact_signatures: tuple[tuple[int, str], ...]) -> bool:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            existing = await session.get(MemoryDreamRuntimeModel, 1)
            if existing is not None:
                return False
            for fact_id, signature in fact_signatures:
                await self._upsert_checkpoint(
                    fact_id,
                    signature,
                    operation_id=None,
                    checked_at=now,
                    session=session,
                )
            session.add(MemoryDreamRuntimeModel(id=1, initialized_at=now))
        return True

    async def baseline_exists(self) -> bool:
        async with self.database.sessions() as session:
            return await session.get(MemoryDreamRuntimeModel, 1) is not None

    async def create_run(
        self,
        *,
        mode: DreamRunMode,
        statistics: DreamPlanStatistics,
        clusters: tuple[tuple[str, str, str, str, tuple[int, ...], str], ...],
        snapshot_max_fact_id: int,
        actor_user_id: str | None,
        scheduled_slot: str | None,
    ) -> DreamRun:
        now = datetime.now(UTC)
        public_id = str(uuid.uuid4())
        async with self.database.sessions() as session, session.begin():
            row = MemoryDreamRunModel(
                public_id=public_id,
                mode=mode.value,
                status=(
                    DreamRunStatus.PLANNED.value
                    if mode is DreamRunMode.FULL
                    else DreamRunStatus.RUNNING.value
                ),
                scheduled_slot=scheduled_slot,
                snapshot_max_fact_id=snapshot_max_fact_id,
                snapshot_created_at=now,
                created_by_user_id=actor_user_id,
                statistics_json=statistics.model_dump_json(),
                model_calls=0,
                completed_clusters=0,
                failed_clusters=0,
                error_category=None,
                created_at=now,
                updated_at=now,
                started_at=(now if mode is DreamRunMode.INCREMENTAL else None),
                completed_at=None,
                cancelled_at=None,
                rolled_back_at=None,
            )
            session.add(row)
            await session.flush()
            for cluster_key, partition_key, bot_user_id, kind, fact_ids, fingerprint in clusters:
                session.add(
                    MemoryDreamClusterModel(
                        run_id=row.id,
                        cluster_key=cluster_key,
                        partition_key=partition_key,
                        bot_user_id=bot_user_id,
                        kind=kind,
                        status=DreamClusterStatus.PENDING.value,
                        fact_ids_json=json.dumps(fact_ids),
                        fingerprint=fingerprint,
                        attempts=0,
                        model_calls=0,
                        operation_count=0,
                        error_category=None,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                )
            await session.flush()
            return self._run(row)

    async def checkpoint_candidates(
        self,
        candidates: tuple[DreamCandidate, ...],
        *,
        operation_id: int | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        if session is None:
            async with self.database.sessions() as owned, owned.begin():
                await self.checkpoint_candidates(
                    candidates, operation_id=operation_id, session=owned
                )
                return
        now = datetime.now(UTC)
        for candidate in candidates:
            await self._upsert_checkpoint(
                candidate.fact.id,
                candidate.signature,
                operation_id=operation_id,
                checked_at=now,
                session=session,
            )

    async def checkpoint_fact(
        self,
        fact: MemoryFact,
        *,
        operation_id: int | None,
        session: AsyncSession,
    ) -> None:
        await self._upsert_checkpoint(
            fact.id,
            fact_signature(fact),
            operation_id=operation_id,
            checked_at=datetime.now(UTC),
            session=session,
        )

    @staticmethod
    async def _upsert_checkpoint(
        fact_id: int,
        signature: str,
        *,
        operation_id: int | None,
        checked_at: datetime,
        session: AsyncSession,
    ) -> None:
        statement = insert(MemoryDreamFactCheckpointModel).values(
            fact_id=fact_id,
            signature=signature,
            last_operation_id=operation_id,
            checked_at=checked_at,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[MemoryDreamFactCheckpointModel.fact_id],
                set_={
                    "signature": signature,
                    "last_operation_id": operation_id,
                    "checked_at": checked_at,
                },
            )
        )

    async def get_run(self, public_id: str) -> DreamRun | None:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(MemoryDreamRunModel).where(MemoryDreamRunModel.public_id == public_id)
            )
        return self._run(row) if row is not None else None

    async def list_runs(self, *, limit: int = 20) -> tuple[DreamRun, ...]:
        async with self.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(MemoryDreamRunModel)
                    .order_by(MemoryDreamRunModel.created_at.desc())
                    .limit(limit)
                )
            ).all()
        return tuple(self._run(row) for row in rows)

    async def run_page(
        self,
        run_public_id: str,
        *,
        page: int = 1,
        page_size: int = 10,
    ) -> DreamRunPage:
        offset = max(0, page - 1) * page_size
        async with self.database.sessions() as session:
            cluster_rows = (
                await session.scalars(
                    select(MemoryDreamClusterModel)
                    .join(
                        MemoryDreamRunModel,
                        MemoryDreamRunModel.id == MemoryDreamClusterModel.run_id,
                    )
                    .where(MemoryDreamRunModel.public_id == run_public_id)
                    .order_by(MemoryDreamClusterModel.id)
                    .offset(offset)
                    .limit(page_size)
                )
            ).all()
            cluster_ids = tuple(row.id for row in cluster_rows)
            operation_rows = (
                (
                    await session.scalars(
                        select(MemoryDreamOperationModel)
                        .where(MemoryDreamOperationModel.cluster_id.in_(cluster_ids))
                        .order_by(
                            MemoryDreamOperationModel.cluster_id,
                            MemoryDreamOperationModel.action_index,
                        )
                    )
                ).all()
                if cluster_ids
                else ()
            )
            operation_ids = tuple(row.id for row in operation_rows)
            result_rows = (
                (
                    await session.scalars(
                        select(MemoryDreamOperationResultModel)
                        .where(MemoryDreamOperationResultModel.operation_id.in_(operation_ids))
                        .order_by(
                            MemoryDreamOperationResultModel.operation_id,
                            MemoryDreamOperationResultModel.position,
                        )
                    )
                ).all()
                if operation_ids
                else ()
            )
            results_by_operation: dict[int, list[int]] = {}
            for result in result_rows:
                results_by_operation.setdefault(result.operation_id, []).append(result.fact_id)
        return DreamRunPage(
            clusters=tuple(self._cluster(row) for row in cluster_rows),
            operations=tuple(
                DreamOperationSummary(
                    public_id=row.public_id,
                    cluster_id=row.cluster_id,
                    operation=DreamOperationType(row.operation_type),
                    status=DreamOperationStatus(row.status),
                    source_fact_ids=tuple(json.loads(row.source_fact_ids_json)),
                    anchor_fact_id=row.anchor_fact_id,
                    output_fact_id=row.output_fact_id,
                    output_fact_ids=tuple(results_by_operation.get(row.id, ())),
                )
                for row in operation_rows
            ),
        )

    async def cluster_for_run(self, run_public_id: str, cluster_id: int) -> DreamCluster | None:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(MemoryDreamClusterModel)
                .join(
                    MemoryDreamRunModel,
                    MemoryDreamRunModel.id == MemoryDreamClusterModel.run_id,
                )
                .where(
                    MemoryDreamRunModel.public_id == run_public_id,
                    MemoryDreamClusterModel.id == cluster_id,
                )
            )
        return self._cluster(row) if row is not None else None

    async def start_run(self, public_id: str) -> bool:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            other = await session.scalar(
                select(func.count())
                .select_from(MemoryDreamRunModel)
                .where(
                    MemoryDreamRunModel.public_id != public_id,
                    MemoryDreamRunModel.status.in_(
                        (DreamRunStatus.RUNNING.value, DreamRunStatus.ROLLING_BACK.value)
                    ),
                )
            )
            if other:
                return False
            result = await session.execute(
                update(MemoryDreamRunModel)
                .where(
                    MemoryDreamRunModel.public_id == public_id,
                    MemoryDreamRunModel.status.in_(
                        (
                            DreamRunStatus.PLANNED.value,
                            DreamRunStatus.PARTIAL_FAILED.value,
                            DreamRunStatus.CANCELLED.value,
                        )
                    ),
                )
                .values(
                    status=DreamRunStatus.RUNNING.value,
                    started_at=func.coalesce(MemoryDreamRunModel.started_at, now),
                    cancelled_at=None,
                    completed_at=None,
                    error_category=None,
                    updated_at=now,
                )
            )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def active_run(self) -> DreamRun | None:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(MemoryDreamRunModel)
                .where(MemoryDreamRunModel.status == DreamRunStatus.RUNNING.value)
                .order_by(MemoryDreamRunModel.created_at)
                .limit(1)
            )
        return self._run(row) if row is not None else None

    async def claim_next_cluster(self, run_public_id: str) -> DreamCluster | None:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            run = await session.scalar(
                select(MemoryDreamRunModel).where(
                    MemoryDreamRunModel.public_id == run_public_id,
                    MemoryDreamRunModel.status == DreamRunStatus.RUNNING.value,
                )
            )
            if run is None:
                return None
            row = await session.scalar(
                select(MemoryDreamClusterModel)
                .where(
                    MemoryDreamClusterModel.run_id == run.id,
                    MemoryDreamClusterModel.status == DreamClusterStatus.PENDING.value,
                )
                .order_by(MemoryDreamClusterModel.id)
                .limit(1)
            )
            if row is None:
                return None
            row.status = DreamClusterStatus.PROCESSING.value
            row.attempts += 1
            row.error_category = None
            row.updated_at = now
            await session.flush()
            return self._cluster(row)

    async def reserve_model_call(
        self,
        *,
        run_public_id: str,
        cluster_id: int,
        maximum: int | None,
    ) -> bool:
        """Persist one actual API attempt before issuing it, including failed attempts."""

        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            row = (
                await session.execute(
                    select(MemoryDreamRunModel, MemoryDreamClusterModel)
                    .join(
                        MemoryDreamClusterModel,
                        MemoryDreamClusterModel.run_id == MemoryDreamRunModel.id,
                    )
                    .where(
                        MemoryDreamRunModel.public_id == run_public_id,
                        MemoryDreamRunModel.status == DreamRunStatus.RUNNING.value,
                        MemoryDreamClusterModel.id == cluster_id,
                        MemoryDreamClusterModel.status == DreamClusterStatus.PROCESSING.value,
                    )
                )
            ).one_or_none()
            if row is None:
                return False
            run, cluster = row
            if maximum is not None and run.model_calls >= maximum:
                return False
            run.model_calls += 1
            run.updated_at = now
            cluster.model_calls += 1
            cluster.updated_at = now
        return True

    async def finish_cluster(
        self,
        cluster_id: int,
        *,
        status: DreamClusterStatus,
        operation_count: int,
        error_category: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            row = await session.get(MemoryDreamClusterModel, cluster_id)
            if row is None:
                return
            row.status = status.value
            row.operation_count = operation_count
            row.error_category = error_category[:64] if error_category else None
            row.updated_at = now
            row.completed_at = now if status is not DreamClusterStatus.FAILED else None
            run = await session.get(MemoryDreamRunModel, row.run_id)
            if run is not None:
                if status in {
                    DreamClusterStatus.COMPLETED,
                    DreamClusterStatus.SKIPPED,
                    DreamClusterStatus.STALE,
                }:
                    run.completed_clusters += 1
                if status is DreamClusterStatus.FAILED:
                    run.failed_clusters += 1
                    run.error_category = row.error_category
                run.updated_at = now

    async def finalize_run(self, public_id: str) -> DreamRun | None:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            row = await session.scalar(
                select(MemoryDreamRunModel).where(MemoryDreamRunModel.public_id == public_id)
            )
            if row is None:
                return None
            pending = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryDreamClusterModel)
                    .where(
                        MemoryDreamClusterModel.run_id == row.id,
                        MemoryDreamClusterModel.status.in_(
                            (
                                DreamClusterStatus.PENDING.value,
                                DreamClusterStatus.PROCESSING.value,
                            )
                        ),
                    )
                )
                or 0
            )
            if pending:
                return self._run(row)
            failed = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryDreamClusterModel)
                    .where(
                        MemoryDreamClusterModel.run_id == row.id,
                        MemoryDreamClusterModel.status.in_(
                            (DreamClusterStatus.FAILED.value, DreamClusterStatus.STALE.value)
                        ),
                    )
                )
                or 0
            )
            row.status = (
                DreamRunStatus.PARTIAL_FAILED.value if failed else DreamRunStatus.COMPLETED.value
            )
            row.completed_at = now
            row.updated_at = now
            await session.flush()
            return self._run(row)

    async def cancel(self, public_id: str) -> bool:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(MemoryDreamRunModel)
                .where(
                    MemoryDreamRunModel.public_id == public_id,
                    MemoryDreamRunModel.status.in_(
                        (DreamRunStatus.PLANNED.value, DreamRunStatus.RUNNING.value)
                    ),
                )
                .values(
                    status=DreamRunStatus.CANCELLED.value,
                    cancelled_at=now,
                    updated_at=now,
                )
            )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def retry_failed(self, public_id: str) -> int:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            run_id = await session.scalar(
                select(MemoryDreamRunModel.id).where(MemoryDreamRunModel.public_id == public_id)
            )
            if run_id is None:
                return 0
            result = await session.execute(
                update(MemoryDreamClusterModel)
                .where(
                    MemoryDreamClusterModel.run_id == run_id,
                    MemoryDreamClusterModel.status.in_(
                        (DreamClusterStatus.FAILED.value, DreamClusterStatus.STALE.value)
                    ),
                )
                .values(
                    status=DreamClusterStatus.PENDING.value,
                    error_category=None,
                    completed_at=None,
                    updated_at=now,
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def fail_pending(self, public_id: str, *, error_category: str) -> int:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            run_id = await session.scalar(
                select(MemoryDreamRunModel.id).where(MemoryDreamRunModel.public_id == public_id)
            )
            if run_id is None:
                return 0
            result = await session.execute(
                update(MemoryDreamClusterModel)
                .where(
                    MemoryDreamClusterModel.run_id == run_id,
                    MemoryDreamClusterModel.status == DreamClusterStatus.PENDING.value,
                )
                .values(
                    status=DreamClusterStatus.FAILED.value,
                    error_category=error_category[:64],
                    updated_at=now,
                )
            )
            count = int(cast(CursorResult[Any], result).rowcount or 0)
            run = await session.get(MemoryDreamRunModel, run_id)
            if run is not None and count:
                run.failed_clusters += count
                run.error_category = error_category[:64]
                run.updated_at = now
        return count

    async def reset_processing_after_restart(self) -> int:
        """Recover orphaned processing clusters without replaying committed mutations."""

        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            rows = tuple(
                (
                    await session.scalars(
                        select(MemoryDreamClusterModel).where(
                            MemoryDreamClusterModel.status == DreamClusterStatus.PROCESSING.value
                        )
                    )
                ).all()
            )
            for row in rows:
                committed = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(MemoryDreamOperationModel)
                        .where(
                            MemoryDreamOperationModel.cluster_id == row.id,
                            MemoryDreamOperationModel.status
                            == DreamOperationStatus.COMMITTED.value,
                        )
                    )
                    or 0
                )
                row.updated_at = now
                run = await session.get(MemoryDreamRunModel, row.run_id)
                if committed:
                    row.status = DreamClusterStatus.COMPLETED.value
                    row.operation_count = committed
                    row.error_category = "recovered_committed_operation"
                    row.completed_at = now
                    if run is not None:
                        run.completed_clusters += 1
                        run.updated_at = now
                    continue
                row.status = DreamClusterStatus.PENDING.value
                row.error_category = "process_restart"
        return len(rows)

    async def create_operation(
        self,
        *,
        cluster_id: int,
        action_index: int,
        operation_type: DreamOperationType,
        source_facts: tuple[MemoryFact, ...],
        anchor_fact_id: int | None,
        session: AsyncSession,
        decision_focuses: tuple[str, ...] = (),
    ) -> MemoryDreamOperationModel:
        now = datetime.now(UTC)
        row = MemoryDreamOperationModel(
            public_id=str(uuid.uuid4()),
            cluster_id=cluster_id,
            action_index=action_index,
            operation_type=operation_type.value,
            status=DreamOperationStatus.PROCESSING.value,
            anchor_fact_id=anchor_fact_id,
            output_fact_id=None,
            source_fact_ids_json=json.dumps([item.id for item in source_facts]),
            decision_focuses_json=json.dumps(decision_focuses, ensure_ascii=False),
            added_evidence_ids_json="[]",
            added_relation_ids_json="[]",
            result_signature=None,
            created_at=now,
            committed_at=None,
            rolled_back_at=None,
        )
        session.add(row)
        await session.flush()
        for position, fact in enumerate(source_facts):
            session.add(
                MemoryDreamOperationSourceModel(
                    operation_id=row.id,
                    fact_id=fact.id,
                    position=position,
                    before_status=fact.status.value,
                    before_conflict_state=fact.conflict_state.value,
                    before_invalidated_reason=(
                        fact.invalidated_reason.value if fact.invalidated_reason else None
                    ),
                    before_authority=fact.authority.value,
                    before_confidence=fact.confidence,
                    before_last_confirmed_at=fact.last_confirmed_at,
                    before_signature=fact_signature(fact),
                    after_signature=None,
                )
            )
        await session.flush()
        return row

    @staticmethod
    async def commit_operation(
        operation_id: int,
        *,
        output_fact_id: int | None,
        output_results: tuple[tuple[int, str], ...],
        added_evidence_ids: tuple[int, ...],
        added_relation_ids: tuple[int, ...],
        result_signature: str | None,
        source_signatures: dict[int, str],
        session: AsyncSession,
    ) -> None:
        await session.execute(
            update(MemoryDreamOperationModel)
            .where(MemoryDreamOperationModel.id == operation_id)
            .values(
                status=DreamOperationStatus.COMMITTED.value,
                output_fact_id=output_fact_id,
                added_evidence_ids_json=json.dumps(added_evidence_ids),
                added_relation_ids_json=json.dumps(added_relation_ids),
                result_signature=result_signature,
                committed_at=datetime.now(UTC),
            )
        )
        for fact_id, signature in source_signatures.items():
            await session.execute(
                update(MemoryDreamOperationSourceModel)
                .where(
                    MemoryDreamOperationSourceModel.operation_id == operation_id,
                    MemoryDreamOperationSourceModel.fact_id == fact_id,
                )
                .values(after_signature=signature)
            )
        for position, (fact_id, signature) in enumerate(output_results):
            session.add(
                MemoryDreamOperationResultModel(
                    operation_id=operation_id,
                    fact_id=fact_id,
                    position=position,
                    result_signature=signature,
                )
            )
        await session.flush()

    async def cluster_facts(self, cluster: DreamCluster) -> tuple[MemoryFact, ...]:
        rows: list[MemoryFact] = []
        async with self.database.sessions() as session:
            for fact_id in cluster.fact_ids:
                fact = await self._facts.get_fact(fact_id, session=session)
                if fact is None:
                    return ()
                rows.append(fact)
        return tuple(rows)

    async def save_preview(
        self,
        *,
        cluster_id: int,
        source_fingerprint: str,
        proposal: DreamOutput,
        model_calls: int,
        source_characters: int,
        output_characters: int,
    ) -> str:
        """Persist one immutable proposal and supersede older ready proposals."""

        now = datetime.now(UTC)
        public_id = str(uuid.uuid4())
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryDreamClusterPreviewModel)
                .where(
                    MemoryDreamClusterPreviewModel.cluster_id == cluster_id,
                    MemoryDreamClusterPreviewModel.status == "ready",
                )
                .values(status="superseded")
            )
            session.add(
                MemoryDreamClusterPreviewModel(
                    public_id=public_id,
                    cluster_id=cluster_id,
                    source_fingerprint=source_fingerprint,
                    proposal_json=proposal.model_dump_json(),
                    schema_version=_DREAM_PREVIEW_SCHEMA_VERSION,
                    model_calls=model_calls,
                    source_characters=source_characters,
                    output_characters=output_characters,
                    status="ready",
                    created_at=now,
                    applied_at=None,
                )
            )
        return public_id

    async def ready_preview(
        self,
        *,
        cluster_id: int,
        source_fingerprint: str,
    ) -> tuple[int, str, DreamOutput] | None:
        """Return the exact ready proposal, marking incompatible snapshots stale."""

        async with self.database.sessions() as session, session.begin():
            rows = tuple(
                (
                    await session.scalars(
                        select(MemoryDreamClusterPreviewModel)
                        .where(
                            MemoryDreamClusterPreviewModel.cluster_id == cluster_id,
                            MemoryDreamClusterPreviewModel.status == "ready",
                        )
                        .order_by(MemoryDreamClusterPreviewModel.id.desc())
                    )
                ).all()
            )
            for row in rows:
                if (
                    row.source_fingerprint != source_fingerprint
                    or row.schema_version != _DREAM_PREVIEW_SCHEMA_VERSION
                ):
                    row.status = "stale"
                    continue
                return row.id, row.public_id, DreamOutput.model_validate_json(row.proposal_json)
        return None

    async def stale_previews(self, cluster_id: int) -> None:
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryDreamClusterPreviewModel)
                .where(
                    MemoryDreamClusterPreviewModel.cluster_id == cluster_id,
                    MemoryDreamClusterPreviewModel.status == "ready",
                )
                .values(status="stale")
            )

    @staticmethod
    async def mark_preview_applied(preview_id: int, *, session: AsyncSession) -> None:
        await session.execute(
            update(MemoryDreamClusterPreviewModel)
            .where(
                MemoryDreamClusterPreviewModel.id == preview_id,
                MemoryDreamClusterPreviewModel.status == "ready",
            )
            .values(status="applied", applied_at=datetime.now(UTC))
        )

    async def health(self, *, enabled: bool) -> DreamHealth:
        async with self.database.sessions() as session:
            active = await session.scalar(
                select(MemoryDreamRunModel)
                .where(MemoryDreamRunModel.status == DreamRunStatus.RUNNING.value)
                .order_by(MemoryDreamRunModel.created_at)
                .limit(1)
            )
            pending = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryDreamClusterModel)
                    .where(MemoryDreamClusterModel.status == DreamClusterStatus.PENDING.value)
                )
                or 0
            )
            failed = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryDreamClusterModel)
                    .where(MemoryDreamClusterModel.status == DreamClusterStatus.FAILED.value)
                )
                or 0
            )
            latest = await session.scalar(
                select(MemoryDreamRunModel)
                .where(MemoryDreamRunModel.completed_at.is_not(None))
                .order_by(MemoryDreamRunModel.completed_at.desc())
                .limit(1)
            )
            preview_ready = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryDreamClusterPreviewModel)
                    .where(MemoryDreamClusterPreviewModel.status == "ready")
                )
                or 0
            )
            preview_stale = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryDreamClusterPreviewModel)
                    .where(MemoryDreamClusterPreviewModel.status == "stale")
                )
                or 0
            )
            compaction_rows = (
                await session.execute(
                    select(
                        MemoryEvidenceCompactionItemModel.status,
                        func.count(MemoryEvidenceCompactionItemModel.id),
                    ).group_by(MemoryEvidenceCompactionItemModel.status)
                )
            ).all()
            compaction_counts = {str(row[0]): int(row[1]) for row in compaction_rows}
            compaction_latest = await session.scalar(
                select(MemoryEvidenceCompactionRunModel)
                .order_by(MemoryEvidenceCompactionRunModel.id.desc())
                .limit(1)
            )
        return DreamHealth(
            enabled=enabled,
            running=active is not None,
            active_run_id=active.public_id if active is not None else None,
            pending_clusters=pending,
            failed_clusters=failed,
            last_completed_at=latest.completed_at if latest is not None else None,
            last_error_category=latest.error_category if latest is not None else None,
            preview_ready=preview_ready,
            preview_stale=preview_stale,
            compaction_pending=(
                compaction_counts.get("pending", 0) + compaction_counts.get("processing", 0)
            ),
            compaction_completed=compaction_counts.get("completed", 0),
            compaction_skipped=compaction_counts.get("skipped", 0),
            compaction_failed=compaction_counts.get("failed", 0),
            compaction_evidence_before=(
                compaction_latest.evidence_before if compaction_latest is not None else 0
            ),
            compaction_evidence_after=(
                compaction_latest.evidence_after if compaction_latest is not None else 0
            ),
            compaction_last_error_category=(
                compaction_latest.error_category if compaction_latest is not None else None
            ),
        )

    async def committed_operation_ids(self, run_public_id: str) -> tuple[str, ...]:
        async with self.database.sessions() as session:
            return tuple(
                await session.scalars(
                    select(MemoryDreamOperationModel.public_id)
                    .join(
                        MemoryDreamClusterModel,
                        MemoryDreamClusterModel.id == MemoryDreamOperationModel.cluster_id,
                    )
                    .join(
                        MemoryDreamRunModel,
                        MemoryDreamRunModel.id == MemoryDreamClusterModel.run_id,
                    )
                    .where(
                        MemoryDreamRunModel.public_id == run_public_id,
                        MemoryDreamOperationModel.status == DreamOperationStatus.COMMITTED.value,
                    )
                    .order_by(MemoryDreamOperationModel.id.desc())
                )
            )

    async def mark_run_rolling_back(self, public_id: str) -> bool:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(MemoryDreamRunModel)
                .where(
                    MemoryDreamRunModel.public_id == public_id,
                    MemoryDreamRunModel.status.in_(
                        (
                            DreamRunStatus.COMPLETED.value,
                            DreamRunStatus.PARTIAL_FAILED.value,
                            DreamRunStatus.CANCELLED.value,
                            DreamRunStatus.ROLLING_BACK.value,
                        )
                    ),
                )
                .values(
                    status=DreamRunStatus.ROLLING_BACK.value,
                    updated_at=now,
                )
            )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def mark_run_rolled_back(self, public_id: str) -> None:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryDreamRunModel)
                .where(MemoryDreamRunModel.public_id == public_id)
                .values(
                    status=DreamRunStatus.ROLLED_BACK.value,
                    rolled_back_at=now,
                    updated_at=now,
                )
            )

    @staticmethod
    def _run(row: MemoryDreamRunModel) -> DreamRun:
        return DreamRun(
            public_id=row.public_id,
            mode=DreamRunMode(row.mode),
            status=DreamRunStatus(row.status),
            scheduled_slot=row.scheduled_slot,
            snapshot_max_fact_id=row.snapshot_max_fact_id,
            snapshot_created_at=row.snapshot_created_at,
            statistics=DreamPlanStatistics.model_validate_json(row.statistics_json),
            model_calls=row.model_calls,
            completed_clusters=row.completed_clusters,
            failed_clusters=row.failed_clusters,
            error_category=row.error_category,
            created_at=row.created_at,
            updated_at=row.updated_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            cancelled_at=row.cancelled_at,
            rolled_back_at=row.rolled_back_at,
        )

    @staticmethod
    def _cluster(row: MemoryDreamClusterModel) -> DreamCluster:
        return DreamCluster(
            id=row.id,
            run_id=row.run_id,
            cluster_key=row.cluster_key,
            partition_key=row.partition_key,
            bot_user_id=row.bot_user_id,
            kind=row.kind,
            status=DreamClusterStatus(row.status),
            fact_ids=tuple(int(item) for item in json.loads(row.fact_ids_json)),
            fingerprint=row.fingerprint,
            attempts=row.attempts,
            model_calls=row.model_calls,
            operation_count=row.operation_count,
            error_category=row.error_category,
        )
