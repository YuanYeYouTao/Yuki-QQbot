"""Content-free recall receipts and local response usage control."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update

from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import MemoryQueryIntent, MemoryRetrievalHit, MemoryRetrievalResult
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryRecallItemModel, MemoryRecallReceiptModel

FINALIZE_MEMORY_RESPONSE_TOOL = "finalize_memory_response"


def hashed_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


@dataclass(frozen=True, slots=True)
class MemoryRecallTurn:
    turn_id: str
    injected_fact_ids: tuple[int, ...]


class MemoryUsageControl:
    """One-run whitelist; never trusts identity or fact ids supplied by the model."""

    def __init__(
        self,
        *,
        turn_id: str,
        injected_fact_ids: tuple[int, ...],
        enabled: bool,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self.turn_id = turn_id
        self.enabled = enabled
        self._available = set(injected_fact_ids)
        self._attempted = False
        self._reported = False
        self._valid = True
        self._finalized = False
        self._used: tuple[int, ...] = ()
        self._report_batch_valid = True
        self._metrics = metrics
        self._invalid_recorded = False
        self._submitted_content = ""

    @property
    def available_refs(self) -> tuple[str, ...]:
        return tuple(f"M{fact_id}" for fact_id in sorted(self._available))

    @property
    def report_available(self) -> bool:
        return self.enabled and bool(self._available) and not self._attempted

    @property
    def used_fact_ids(self) -> tuple[int, ...]:
        if not (self._reported and self._valid and self._finalized):
            return ()
        return self._used

    @property
    def reported(self) -> bool:
        return bool(self.used_fact_ids)

    def register_presented(self, fact_ids: tuple[int, ...]) -> None:
        if self._reported:
            self._valid = False
            return
        self._available.update(fact_ids)

    def begin_batch(self, names: tuple[str, ...]) -> None:
        self._report_batch_valid = not (FINALIZE_MEMORY_RESPONSE_TOOL in names and len(names) != 1)

    def note_call(self, name: str) -> None:
        if self._reported and name != FINALIZE_MEMORY_RESPONSE_TOOL:
            self._valid = False
            self._record_invalid()

    def apply(self, arguments_json: str) -> str:
        self._attempted = True
        if not self.enabled or not self._report_batch_valid:
            self._valid = False
            self._record_invalid()
            return _result(False, "memory_usage_report_unavailable")
        if self._reported:
            self._valid = False
            self._record_invalid()
            return _result(False, "memory_usage_report_duplicate")
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError:
            arguments = None
        refs = arguments.get("memory_refs") if isinstance(arguments, dict) else None
        content = arguments.get("content") if isinstance(arguments, dict) else None
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"content", "memory_refs"}
            or not isinstance(content, str)
            or not content.strip()
            or not isinstance(refs, list)
            or len(refs) > 100
            or any(not isinstance(ref, str) for ref in refs)
        ):
            self._valid = False
            self._record_invalid()
            return _result(False, "memory_usage_report_invalid")
        unique_refs = tuple(dict.fromkeys(refs))
        if any(not ref.startswith("M") or not ref[1:].isdigit() for ref in unique_refs):
            self._valid = False
            self._record_invalid()
            return _result(False, "memory_usage_ref_invalid")
        fact_ids = tuple(int(ref[1:]) for ref in unique_refs)
        if any(fact_id not in self._available for fact_id in fact_ids):
            self._valid = False
            self._record_invalid()
            return _result(False, "memory_usage_ref_not_presented")
        self._reported = True
        self._used = fact_ids
        self._submitted_content = content.strip()
        if self._metrics is not None:
            self._metrics.record_usage_report("valid" if fact_ids else "empty")
        return json.dumps(
            {
                "ok": True,
                "accepted": len(fact_ids),
                "terminal_response": content,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def finalize(self, content: str) -> None:
        normalized = content.strip()
        self._finalized = bool(normalized)
        if self._reported and normalized != self._submitted_content:
            self._valid = False
            self._record_invalid()
        if self.enabled and self._available and not self._attempted and self._metrics is not None:
            self._metrics.record_usage_report("missing")

    def _record_invalid(self) -> None:
        if not self._invalid_recorded and self._metrics is not None:
            self._metrics.record_usage_report("invalid")
            self._invalid_recorded = True


class MemoryRecallRepository:
    """Persist bounded recall stages without query, message, or memory text."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record_initial(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
        origin: str,
        intent: MemoryQueryIntent,
        result: MemoryRetrievalResult,
        injected_fact_ids: tuple[int, ...],
        retention_days: int,
    ) -> MemoryRecallTurn:
        turn_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        selected_ids = {hit.fact.id for hit in result.hits}
        injected_ids = set(injected_fact_ids)
        by_fact: dict[int, MemoryRetrievalHit] = {}
        for hit in result.trace_hits:
            by_fact.setdefault(hit.fact.id, hit)
        async with self._database.sessions() as session, session.begin():
            receipt = MemoryRecallReceiptModel(
                turn_id=turn_id,
                conversation_hash=hashed_identifier(conversation_key),
                trigger_hash=hashed_identifier(trigger_message_id),
                origin=origin[:32],
                mode=intent.mode.value,
                purpose=intent.purpose.value,
                candidate_count=result.candidate_count,
                selected_count=len(selected_ids),
                injected_count=len(injected_ids),
                used_count=0,
                reinforced_count=0,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(days=retention_days),
            )
            session.add(receipt)
            await session.flush()
            for hit in by_fact.values():
                session.add(
                    MemoryRecallItemModel(
                        receipt_id=receipt.id,
                        fact_id=hit.fact.id,
                        target_role=hit.target.role.value,
                        candidate=True,
                        selected=hit.fact.id in selected_ids,
                        injected=hit.fact.id in injected_ids,
                        used=False,
                        reinforced=False,
                        base_rank_score=hit.base_rank_score,
                        subject_score=hit.subject_score,
                        entity_score=hit.entity_score,
                        temporal_score=hit.temporal_score,
                        kind_score=hit.kind_score,
                        activation_score=hit.activation_score,
                        rerank_score=hit.rerank_score,
                        selection_reason=hit.selection_reason[:64],
                        injected_at=now if hit.fact.id in injected_ids else None,
                        used_at=None,
                        reinforced_at=None,
                    )
                )
        return MemoryRecallTurn(turn_id=turn_id, injected_fact_ids=injected_fact_ids)

    async def mark_used(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
    ) -> tuple[int, ...] | None:
        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not turn_id or not unique_ids:
            return ()
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            receipt_id = await session.scalar(
                select(MemoryRecallReceiptModel.id).where(
                    MemoryRecallReceiptModel.turn_id == turn_id
                )
            )
            if receipt_id is None:
                return None
            allowed = tuple(
                await session.scalars(
                    select(MemoryRecallItemModel.fact_id).where(
                        MemoryRecallItemModel.receipt_id == receipt_id,
                        MemoryRecallItemModel.fact_id.in_(unique_ids),
                        MemoryRecallItemModel.injected.is_(True),
                    )
                )
            )
            if not allowed:
                return ()
            await session.execute(
                update(MemoryRecallItemModel)
                .where(
                    MemoryRecallItemModel.receipt_id == receipt_id,
                    MemoryRecallItemModel.fact_id.in_(allowed),
                )
                .values(used=True, used_at=now)
            )
            used_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRecallItemModel)
                    .where(
                        MemoryRecallItemModel.receipt_id == receipt_id,
                        MemoryRecallItemModel.used.is_(True),
                    )
                )
                or 0
            )
            await session.execute(
                update(MemoryRecallReceiptModel)
                .where(MemoryRecallReceiptModel.id == receipt_id)
                .values(used_count=used_count, updated_at=now)
            )
        return allowed

    async def record_tool_injected(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
    ) -> None:
        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not turn_id or not unique_ids:
            return
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            receipt_id = await session.scalar(
                select(MemoryRecallReceiptModel.id).where(
                    MemoryRecallReceiptModel.turn_id == turn_id
                )
            )
            if receipt_id is None:
                return
            existing = {
                row.fact_id: row
                for row in (
                    await session.scalars(
                        select(MemoryRecallItemModel).where(
                            MemoryRecallItemModel.receipt_id == receipt_id,
                            MemoryRecallItemModel.fact_id.in_(unique_ids),
                        )
                    )
                ).all()
            }
            for fact_id in unique_ids:
                row = existing.get(fact_id)
                if row is not None:
                    row.selected = True
                    row.injected = True
                    row.injected_at = now
                    continue
                session.add(
                    MemoryRecallItemModel(
                        receipt_id=receipt_id,
                        fact_id=fact_id,
                        target_role="agent_tool",
                        candidate=True,
                        selected=True,
                        injected=True,
                        used=False,
                        reinforced=False,
                        base_rank_score=0,
                        subject_score=0.5,
                        entity_score=0.5,
                        temporal_score=0.5,
                        kind_score=0.5,
                        activation_score=0.5,
                        rerank_score=0,
                        selection_reason="agent_tool_result",
                        injected_at=now,
                    )
                )
            await session.flush()
            injected_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRecallItemModel)
                    .where(
                        MemoryRecallItemModel.receipt_id == receipt_id,
                        MemoryRecallItemModel.injected.is_(True),
                    )
                )
                or 0
            )
            await session.execute(
                update(MemoryRecallReceiptModel)
                .where(MemoryRecallReceiptModel.id == receipt_id)
                .values(injected_count=injected_count, updated_at=now)
            )

    async def pending_reinforcement(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
    ) -> tuple[int, ...] | None:
        if not turn_id or not fact_ids:
            return ()
        async with self._database.sessions() as session:
            receipt_id = await session.scalar(
                select(MemoryRecallReceiptModel.id).where(
                    MemoryRecallReceiptModel.turn_id == turn_id
                )
            )
            if receipt_id is None:
                return None
            rows = await session.scalars(
                select(MemoryRecallItemModel.fact_id).where(
                    MemoryRecallItemModel.receipt_id == receipt_id,
                    MemoryRecallItemModel.fact_id.in_(tuple(dict.fromkeys(fact_ids))),
                    MemoryRecallItemModel.used.is_(True),
                    MemoryRecallItemModel.reinforced.is_(False),
                )
            )
            return tuple(rows)

    async def mark_reinforced(self, turn_id: str, fact_ids: tuple[int, ...]) -> int:
        if not turn_id or not fact_ids:
            return 0
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            receipt_id = await session.scalar(
                select(MemoryRecallReceiptModel.id).where(
                    MemoryRecallReceiptModel.turn_id == turn_id
                )
            )
            if receipt_id is None:
                return 0
            await session.execute(
                update(MemoryRecallItemModel)
                .where(
                    MemoryRecallItemModel.receipt_id == receipt_id,
                    MemoryRecallItemModel.fact_id.in_(tuple(dict.fromkeys(fact_ids))),
                    MemoryRecallItemModel.used.is_(True),
                    MemoryRecallItemModel.reinforced.is_(False),
                )
                .values(reinforced=True, reinforced_at=now)
            )
            reinforced_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRecallItemModel)
                    .where(
                        MemoryRecallItemModel.receipt_id == receipt_id,
                        MemoryRecallItemModel.reinforced.is_(True),
                    )
                )
                or 0
            )
            await session.execute(
                update(MemoryRecallReceiptModel)
                .where(MemoryRecallReceiptModel.id == receipt_id)
                .values(reinforced_count=reinforced_count, updated_at=now)
            )
        return reinforced_count

    async def cleanup_expired(self, *, now: datetime, limit: int) -> int:
        async with self._database.sessions() as session, session.begin():
            ids = tuple(
                await session.scalars(
                    select(MemoryRecallReceiptModel.id)
                    .where(MemoryRecallReceiptModel.expires_at < now)
                    .order_by(MemoryRecallReceiptModel.id)
                    .limit(limit)
                )
            )
            if not ids:
                return 0
            await session.execute(
                delete(MemoryRecallReceiptModel).where(MemoryRecallReceiptModel.id.in_(ids))
            )
            return len(ids)

    async def recent_for_fact(
        self,
        fact_id: int,
        *,
        limit: int = 5,
    ) -> tuple[dict[str, object], ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(MemoryRecallReceiptModel, MemoryRecallItemModel)
                    .join(
                        MemoryRecallItemModel,
                        MemoryRecallItemModel.receipt_id == MemoryRecallReceiptModel.id,
                    )
                    .where(MemoryRecallItemModel.fact_id == fact_id)
                    .order_by(MemoryRecallReceiptModel.created_at.desc())
                    .limit(max(1, limit))
                )
            ).all()
        return tuple(
            {
                "turn_id": receipt.turn_id,
                "mode": receipt.mode,
                "purpose": receipt.purpose,
                "selected": item.selected,
                "injected": item.injected,
                "used": item.used,
                "reinforced": item.reinforced,
                "rerank_score": item.rerank_score,
                "created_at": receipt.created_at.isoformat(),
            }
            for receipt, item in rows
        )


def _result(ok: bool, error: str) -> str:
    return json.dumps({"ok": ok, "error": error}, ensure_ascii=False, separators=(",", ":"))
