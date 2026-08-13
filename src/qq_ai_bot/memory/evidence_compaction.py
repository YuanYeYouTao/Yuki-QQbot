"""Restart-safe evidence compaction with conservative provenance checks."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamFactCheckpointModel,
    MemoryDreamOperationModel,
    MemoryDreamOperationResultModel,
    MemoryDreamOperationSourceModel,
    MemoryEvidenceCompactionItemModel,
    MemoryEvidenceCompactionRunModel,
)
from qq_ai_bot.memory.dream.repository import fact_signature
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactRelationModel,
    MemoryMutationReceiptModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
)

logger = logging.getLogger(__name__)


class EvidenceCompactionService:
    """Compact only evidence whose complete source lineage is independently recoverable."""

    def __init__(self, *, settings: Settings, database: Database, facts: MemoryFactService) -> None:
        self._settings = settings
        self._database = database
        self._facts = facts

    async def run_batch(self) -> int:
        await self._backfill_reflection_results()
        run_id = await self._ensure_run()
        candidates = await self._candidate_facts(
            limit=self._settings.memory_evidence_compaction_batch_size
        )
        if not candidates:
            await self._finish_run(run_id)
            return 0
        processed = 0
        for fact_id, provenance, operation_id, evidence_count in candidates:
            item_id = await self._claim_item(
                run_id=run_id,
                fact_id=fact_id,
                provenance=provenance,
                operation_id=operation_id,
                evidence_before=evidence_count,
            )
            if item_id is None:
                continue
            try:
                after = await self._compact_fact(
                    fact_id=fact_id,
                    provenance=provenance,
                    operation_id=operation_id,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                await self._finish_item(
                    item_id,
                    status="failed",
                    before=evidence_count,
                    after=evidence_count,
                    error_category=type(exc).__name__,
                )
                logger.warning(
                    "memory_evidence_compaction_failed fact_id=%d error_category=%s",
                    fact_id,
                    type(exc).__name__,
                )
            else:
                status = "completed" if after < evidence_count else "skipped"
                await self._finish_item(
                    item_id,
                    status=status,
                    before=evidence_count,
                    after=after,
                    error_category=None if status == "completed" else "no_safe_reduction",
                )
            processed += 1
        await self._refresh_run(run_id)
        return processed

    async def _backfill_reflection_results(self) -> None:
        async with self._database.sessions() as session, session.begin():
            receipts = tuple(
                (
                    await session.scalars(
                        select(MemoryMutationReceiptModel)
                        .where(
                            MemoryMutationReceiptModel.decision_actor_type == "reflection",
                            MemoryMutationReceiptModel.delegation_mode.like("self_episode:%"),
                            MemoryMutationReceiptModel.new_fact_id.is_not(None),
                            ~select(MemorySelfReflectionResultModel.id)
                            .where(
                                MemorySelfReflectionResultModel.fact_id
                                == MemoryMutationReceiptModel.new_fact_id
                            )
                            .exists(),
                        )
                        .order_by(MemoryMutationReceiptModel.id)
                        .limit(200)
                    )
                ).all()
            )
            for receipt in receipts:
                parts = receipt.delegation_mode.split(":")
                if len(parts) != 3:
                    continue
                try:
                    first_event_id, last_event_id = int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                runs = tuple(
                    (
                        await session.scalars(
                            select(MemorySelfReflectionRunModel).where(
                                MemorySelfReflectionRunModel.bot_user_id
                                == receipt.executed_by_bot_user_id,
                                MemorySelfReflectionRunModel.first_event_id == first_event_id,
                                MemorySelfReflectionRunModel.last_event_id == last_event_id,
                            )
                        )
                    ).all()
                )
                if len(runs) != 1 or receipt.new_fact_id is None:
                    continue
                await session.execute(
                    insert(MemorySelfReflectionResultModel)
                    .values(
                        run_id=runs[0].id,
                        fact_id=receipt.new_fact_id,
                        result_kind="episode",
                        result_index=1,
                        created_at=receipt.created_at,
                    )
                    .on_conflict_do_nothing()
                )

    async def _ensure_run(self) -> int:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            current = await session.scalar(
                select(MemoryEvidenceCompactionRunModel)
                .where(MemoryEvidenceCompactionRunModel.status == "running")
                .order_by(MemoryEvidenceCompactionRunModel.id)
                .limit(1)
            )
            if current is not None:
                await session.execute(
                    update(MemoryEvidenceCompactionItemModel)
                    .where(
                        MemoryEvidenceCompactionItemModel.run_id == current.id,
                        MemoryEvidenceCompactionItemModel.status == "processing",
                    )
                    .values(status="pending", updated_at=now)
                )
                return current.id
            row = MemoryEvidenceCompactionRunModel(
                public_id=str(uuid.uuid4()),
                status="running",
                scan_after_fact_id=0,
                scanned_facts=0,
                completed_items=0,
                skipped_items=0,
                failed_items=0,
                evidence_before=0,
                evidence_after=0,
                error_category=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            session.add(row)
            await session.flush()
            return row.id

    async def _candidate_facts(
        self, *, limit: int
    ) -> tuple[tuple[int, str, int | None, int], ...]:
        async with self._database.sessions() as session:
            counts = (
                select(
                    MemoryEvidenceModel.fact_id.label("fact_id"),
                    func.count(MemoryEvidenceModel.id).label("evidence_count"),
                )
                .group_by(MemoryEvidenceModel.fact_id)
                .subquery()
            )
            rows = (
                await session.execute(
                    select(
                        counts.c.fact_id,
                        counts.c.evidence_count,
                        MemorySelfReflectionResultModel.id.label("reflection_result_id"),
                        MemoryDreamOperationResultModel.operation_id,
                        MemoryDreamOperationModel.operation_type,
                    )
                    .outerjoin(
                        MemorySelfReflectionResultModel,
                        MemorySelfReflectionResultModel.fact_id == counts.c.fact_id,
                    )
                    .outerjoin(
                        MemoryDreamOperationResultModel,
                        MemoryDreamOperationResultModel.fact_id == counts.c.fact_id,
                    )
                    .outerjoin(
                        MemoryDreamOperationModel,
                        MemoryDreamOperationModel.id
                        == MemoryDreamOperationResultModel.operation_id,
                    )
                    .where(
                        (
                            (MemorySelfReflectionResultModel.id.is_not(None))
                            & (counts.c.evidence_count > 8)
                        )
                        | (
                            MemoryDreamOperationModel.operation_type.in_(
                                ("synthesize", "recompose")
                            )
                            & (MemoryDreamOperationModel.status == "committed")
                            & (counts.c.evidence_count > 12)
                        )
                    )
                    .order_by(counts.c.fact_id)
                    .limit(max(1, limit * 4))
                )
            ).all()
            result: list[tuple[int, str, int | None, int]] = []
            for row in rows:
                provenance = (
                    "self_reflection" if row.reflection_result_id is not None else "dream"
                )
                operation_id = int(row.operation_id) if row.operation_id is not None else None
                previous = await session.scalar(
                    select(MemoryEvidenceCompactionItemModel)
                    .where(
                        MemoryEvidenceCompactionItemModel.fact_id == int(row.fact_id),
                        MemoryEvidenceCompactionItemModel.evidence_before
                        == int(row.evidence_count),
                        MemoryEvidenceCompactionItemModel.status.in_(
                            ("completed", "skipped", "failed")
                        ),
                    )
                    .order_by(MemoryEvidenceCompactionItemModel.id.desc())
                    .limit(1)
                )
                if previous is not None:
                    continue
                result.append(
                    (int(row.fact_id), provenance, operation_id, int(row.evidence_count))
                )
                if len(result) >= limit:
                    break
        return tuple(result)

    async def _claim_item(
        self,
        *,
        run_id: int,
        fact_id: int,
        provenance: str,
        operation_id: int | None,
        evidence_before: int,
    ) -> int | None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            existing = await session.scalar(
                select(MemoryEvidenceCompactionItemModel).where(
                    MemoryEvidenceCompactionItemModel.run_id == run_id,
                    MemoryEvidenceCompactionItemModel.fact_id == fact_id,
                    MemoryEvidenceCompactionItemModel.status.in_(("pending", "processing")),
                )
            )
            if existing is not None:
                existing.updated_at = now
                existing.status = "processing"
                return existing.id
            item_id = await session.scalar(
                insert(MemoryEvidenceCompactionItemModel)
                .values(
                    run_id=run_id,
                    fact_id=fact_id,
                    provenance_type=provenance,
                    dream_operation_id=operation_id,
                    status="processing",
                    evidence_before=evidence_before,
                    evidence_after=evidence_before,
                    deleted_count=0,
                    error_category=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        MemoryEvidenceCompactionItemModel.run_id,
                        MemoryEvidenceCompactionItemModel.fact_id,
                    ]
                )
                .returning(MemoryEvidenceCompactionItemModel.id)
            )
            return int(item_id) if item_id is not None else None

    async def _compact_fact(
        self, *, fact_id: int, provenance: str, operation_id: int | None
    ) -> int:
        async with self._facts.repository.transaction() as session:
            fact = await self._facts.repository.get_fact(fact_id, session=session)
            if fact is None:
                raise ValueError("compaction fact disappeared")
            evidence = tuple(
                (
                    await session.scalars(
                        select(MemoryEvidenceModel)
                        .where(MemoryEvidenceModel.fact_id == fact_id)
                        .order_by(MemoryEvidenceModel.created_at, MemoryEvidenceModel.id)
                    )
                ).all()
            )
            if provenance == "self_reflection":
                keep_ids = await self._self_reflection_keep_ids(
                    fact_id=fact_id, evidence=evidence, session=session
                )
            else:
                if operation_id is None:
                    return len(evidence)
                keep_ids = await self._dream_keep_ids(
                    fact_id=fact_id,
                    operation_id=operation_id,
                    evidence=evidence,
                    current_signature=fact_signature(fact),
                    session=session,
                )
            delete_ids = tuple(row.id for row in evidence if row.id not in keep_ids)
            if not delete_ids:
                return len(evidence)
            await session.execute(
                delete(MemoryEvidenceModel).where(MemoryEvidenceModel.id.in_(delete_ids))
            )
            await self._facts.refresh_evidence_metadata(
                fact_id,
                confirmed_at=fact.last_confirmed_at,
                session=session,
            )
            if provenance == "dream" and operation_id is not None:
                await self._rebase_dream_operation(
                    fact_id=fact_id,
                    operation_id=operation_id,
                    deleted_ids=delete_ids,
                    session=session,
                )
            return len(evidence) - len(delete_ids)

    async def _self_reflection_keep_ids(
        self, *, fact_id: int, evidence: tuple[Any, ...], session: Any
    ) -> set[int]:
        mapping = await session.scalar(
            select(MemorySelfReflectionResultModel).where(
                MemorySelfReflectionResultModel.fact_id == fact_id,
                MemorySelfReflectionResultModel.result_kind == "episode",
            )
        )
        if mapping is None:
            return {row.id for row in evidence}
        run = await session.get(MemorySelfReflectionRunModel, mapping.run_id)
        if run is None:
            return {row.id for row in evidence}
        receipt = await session.scalar(
            select(MemoryMutationReceiptModel)
            .where(
                MemoryMutationReceiptModel.new_fact_id == fact_id,
                MemoryMutationReceiptModel.decision_actor_type == "reflection",
            )
            .order_by(MemoryMutationReceiptModel.id)
            .limit(1)
        )
        if receipt is None:
            return {row.id for row in evidence}
        by_event = {row.event_id: row for row in evidence if row.event_id is not None}
        selected: list[Any] = []
        if receipt.trigger_event_id in by_event:
            selected.append(by_event[receipt.trigger_event_id])
        event_rows = tuple(row for row in evidence if row.event_id is not None)
        nonempty_ids = set(
            await session.scalars(
                select(ChatEventModel.id).where(
                    ChatEventModel.id.in_(tuple(row.event_id for row in event_rows)),
                    func.length(func.trim(ChatEventModel.content)) > 0,
                )
            )
        )
        nonempty = [row for row in event_rows if row.event_id in nonempty_ids]
        if nonempty:
            selected.extend((nonempty[0], nonempty[-1]))
        selected.extend(row for row in evidence if row.tool_receipt_id is not None)
        authority_rank = {
            "explicit": 5,
            "agent_reflection": 4,
            "self_report": 3,
            "group_report": 2,
            "third_party": 1,
        }
        selected.extend(
            sorted(
                evidence,
                key=lambda row: (
                    authority_rank.get(row.authority, 0),
                    row.confidence,
                    row.created_at,
                    row.id,
                ),
                reverse=True,
            )
        )
        unique: list[Any] = []
        seen: set[int] = set()
        for row in selected:
            if row is None or row.id in seen:
                continue
            seen.add(row.id)
            unique.append(row)
            if len(unique) >= 8:
                break
        return {row.id for row in unique}

    async def _dream_keep_ids(
        self,
        *,
        fact_id: int,
        operation_id: int,
        evidence: tuple[Any, ...],
        current_signature: str,
        session: Any,
    ) -> set[int]:
        operation = await session.get(MemoryDreamOperationModel, operation_id)
        result = await session.scalar(
            select(MemoryDreamOperationResultModel).where(
                MemoryDreamOperationResultModel.operation_id == operation_id,
                MemoryDreamOperationResultModel.fact_id == fact_id,
            )
        )
        if (
            operation is None
            or result is None
            or operation.status != "committed"
            or operation.operation_type not in {"synthesize", "recompose"}
            or result.result_signature != current_signature
        ):
            return {row.id for row in evidence}
        dependency = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryDreamOperationSourceModel)
                .join(
                    MemoryDreamOperationModel,
                    MemoryDreamOperationModel.id
                    == MemoryDreamOperationSourceModel.operation_id,
                )
                .where(
                    MemoryDreamOperationSourceModel.fact_id == fact_id,
                    MemoryDreamOperationModel.id > operation_id,
                    MemoryDreamOperationModel.status == "committed",
                )
            )
            or 0
        )
        if dependency:
            return {row.id for row in evidence}
        added_ids = {int(item) for item in json.loads(operation.added_evidence_ids_json)}
        if any(row.id not in added_ids for row in evidence):
            return {row.id for row in evidence}
        source_ids = tuple(
            await session.scalars(
                select(MemoryDreamOperationSourceModel.fact_id)
                .join(
                    MemoryFactRelationModel,
                    MemoryFactRelationModel.source_fact_id
                    == MemoryDreamOperationSourceModel.fact_id,
                )
                .where(
                    MemoryDreamOperationSourceModel.operation_id == operation_id,
                    MemoryFactRelationModel.target_fact_id == fact_id,
                    MemoryFactRelationModel.relation_type.in_(("refines", "equivalent")),
                )
                .order_by(MemoryDreamOperationSourceModel.position)
            )
        )
        chosen: list[Any] = []
        for source_id in source_ids:
            source_keys = {
                (event_id, tool_receipt_id)
                for event_id, tool_receipt_id in (
                    await session.execute(
                    select(
                        MemoryEvidenceModel.event_id,
                        MemoryEvidenceModel.tool_receipt_id,
                    ).where(MemoryEvidenceModel.fact_id == source_id)
                    )
                ).all()
            }
            matches = [
                row
                for row in evidence
                if (row.event_id, row.tool_receipt_id) in source_keys
            ]
            if len(matches) <= 2:
                chosen.extend(matches)
            elif matches:
                chosen.extend((matches[0], matches[-1]))
        unique: list[Any] = []
        seen: set[int] = set()
        for row in chosen:
            if row.id in seen:
                continue
            seen.add(row.id)
            unique.append(row)
            if len(unique) >= 12:
                break
        return {row.id for row in unique}

    async def _rebase_dream_operation(
        self,
        *,
        fact_id: int,
        operation_id: int,
        deleted_ids: tuple[int, ...],
        session: Any,
    ) -> None:
        fact = await self._facts.repository.get_fact(fact_id, session=session)
        if fact is None:
            raise RuntimeError("compacted Dream result disappeared")
        signature = fact_signature(fact)
        operation = await session.get(MemoryDreamOperationModel, operation_id)
        if operation is None:
            raise RuntimeError("Dream provenance operation disappeared")
        deleted = set(deleted_ids)
        operation.added_evidence_ids_json = json.dumps(
            [
                int(item)
                for item in json.loads(operation.added_evidence_ids_json)
                if int(item) not in deleted
            ]
        )
        result = await session.scalar(
            select(MemoryDreamOperationResultModel).where(
                MemoryDreamOperationResultModel.operation_id == operation_id,
                MemoryDreamOperationResultModel.fact_id == fact_id,
            )
        )
        if result is not None:
            result.result_signature = signature
            if result.position == 0:
                operation.result_signature = signature
        source = await session.scalar(
            select(MemoryDreamOperationSourceModel).where(
                MemoryDreamOperationSourceModel.operation_id == operation_id,
                MemoryDreamOperationSourceModel.fact_id == fact_id,
            )
        )
        if source is not None:
            source.after_signature = signature
        checkpoint = await session.get(MemoryDreamFactCheckpointModel, fact_id)
        if checkpoint is not None and checkpoint.last_operation_id == operation_id:
            checkpoint.signature = signature
            checkpoint.checked_at = datetime.now(UTC)

    async def _finish_item(
        self,
        item_id: int,
        *,
        status: str,
        before: int,
        after: int,
        error_category: str | None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryEvidenceCompactionItemModel)
                .where(MemoryEvidenceCompactionItemModel.id == item_id)
                .values(
                    status=status,
                    evidence_after=after,
                    deleted_count=max(0, before - after),
                    error_category=error_category,
                    updated_at=now,
                    completed_at=now,
                )
            )

    async def _refresh_run(self, run_id: int) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            rows = (
                await session.execute(
                    select(
                        MemoryEvidenceCompactionItemModel.status,
                        func.count(MemoryEvidenceCompactionItemModel.id),
                        func.sum(MemoryEvidenceCompactionItemModel.evidence_before),
                        func.sum(MemoryEvidenceCompactionItemModel.evidence_after),
                    )
                    .where(MemoryEvidenceCompactionItemModel.run_id == run_id)
                    .group_by(MemoryEvidenceCompactionItemModel.status)
                )
            ).all()
            counts = {str(row[0]): int(row[1]) for row in rows}
            before = sum(int(row[2] or 0) for row in rows)
            after = sum(int(row[3] or 0) for row in rows)
            await session.execute(
                update(MemoryEvidenceCompactionRunModel)
                .where(MemoryEvidenceCompactionRunModel.id == run_id)
                .values(
                    scanned_facts=sum(counts.values()),
                    completed_items=counts.get("completed", 0),
                    skipped_items=counts.get("skipped", 0),
                    failed_items=counts.get("failed", 0),
                    evidence_before=before,
                    evidence_after=after,
                    error_category=("item_failed" if counts.get("failed", 0) else None),
                    updated_at=now,
                )
            )

    async def _finish_run(self, run_id: int) -> None:
        await self._refresh_run(run_id)
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(MemoryEvidenceCompactionRunModel, run_id)
            if row is None or row.status != "running":
                return
            row.status = "partial_failed" if row.failed_items else "completed"
            row.updated_at = now
            row.completed_at = now


class EvidenceCompactionWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        service: EvidenceCompactionService,
        process_lock: asyncio.Lock,
    ) -> None:
        self._settings = settings
        self._service = service
        self._process_lock = process_lock
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.waiting_for_lock = False
        self.holding_lock = False

    async def start(self) -> None:
        if not self._settings.memory_evidence_compaction_enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="memory-evidence-compaction")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.waiting_for_lock = self._process_lock.locked()
                async with self._process_lock:
                    self.waiting_for_lock = False
                    self.holding_lock = True
                    try:
                        processed = await self._service.run_batch()
                    finally:
                        self.holding_lock = False
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "memory_evidence_compaction_loop_failed error_category=%s",
                    type(exc).__name__,
                )
                processed = 0
            await asyncio.sleep(
                1.0
                if processed
                else min(60.0, self._settings.memory_dream_poll_seconds)
            )
