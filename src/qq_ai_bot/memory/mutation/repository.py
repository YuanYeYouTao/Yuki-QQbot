"""Persistence for atomic and idempotent memory mutation receipts."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationAppliedOperation,
    MemoryMutationOperation,
    MemoryMutationOutcome,
    MemoryMutationReceipt,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryMutationReceiptModel


class MemoryMutationReceiptRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def find(
        self,
        *,
        idempotency_key: str,
        claim_fingerprint: str,
        session: AsyncSession | None = None,
    ) -> MemoryMutationReceipt | None:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.find(
                    idempotency_key=idempotency_key,
                    claim_fingerprint=claim_fingerprint,
                    session=owned,
                )
        row = await session.scalar(
            select(MemoryMutationReceiptModel).where(
                or_(
                    MemoryMutationReceiptModel.idempotency_key == idempotency_key,
                    MemoryMutationReceiptModel.claim_fingerprint == claim_fingerprint,
                )
            )
        )
        return _receipt(row) if row is not None else None

    async def reserve(
        self,
        *,
        mutation_id: str,
        idempotency_key: str,
        claim_fingerprint: str,
        target_fingerprint: str,
        trigger_event_id: int,
        conversation_key: str,
        current_group_id: str | None,
        turn_origin: str,
        delegation_mode: str,
        trigger_actor_user_id: str,
        decision_actor_type: MemoryDecisionActorType,
        decision_actor_id: str | None,
        executed_by_bot_user_id: str,
        requested_operation: MemoryMutationOperation,
        created_at: datetime,
        session: AsyncSession,
    ) -> MemoryMutationReceipt:
        row = MemoryMutationReceiptModel(
            mutation_id=mutation_id,
            idempotency_key=idempotency_key,
            claim_fingerprint=claim_fingerprint,
            target_fingerprint=target_fingerprint,
            trigger_source_type="chat_event",
            trigger_event_id=trigger_event_id,
            dream_operation_id=None,
            conversation_key=conversation_key,
            current_group_id=current_group_id,
            turn_origin=turn_origin,
            delegation_mode=delegation_mode,
            trigger_actor_user_id=trigger_actor_user_id,
            decision_actor_type=decision_actor_type.value,
            decision_actor_id=decision_actor_id,
            executed_by_bot_user_id=executed_by_bot_user_id,
            requested_operation=requested_operation.value,
            applied_operation=MemoryMutationAppliedOperation.NOOP.value,
            old_fact_id=None,
            new_fact_id=None,
            outcome=MemoryMutationOutcome.PROCESSING.value,
            reason_code="processing",
            created_at=created_at,
        )
        session.add(row)
        await session.flush()
        return _receipt(row)

    async def reserve_dream(
        self,
        *,
        mutation_id: str,
        idempotency_key: str,
        claim_fingerprint: str,
        target_fingerprint: str,
        dream_operation_id: int,
        conversation_key: str,
        current_group_id: str | None,
        bot_user_id: str,
        requested_operation: MemoryMutationOperation,
        created_at: datetime,
        session: AsyncSession,
    ) -> MemoryMutationReceipt:
        row = MemoryMutationReceiptModel(
            mutation_id=mutation_id,
            idempotency_key=idempotency_key,
            claim_fingerprint=claim_fingerprint,
            target_fingerprint=target_fingerprint,
            trigger_source_type="dream_operation",
            trigger_event_id=None,
            dream_operation_id=dream_operation_id,
            conversation_key=conversation_key,
            current_group_id=current_group_id,
            turn_origin="memory_dream",
            delegation_mode="dream",
            trigger_actor_user_id=bot_user_id,
            decision_actor_type=MemoryDecisionActorType.SYSTEM.value,
            decision_actor_id="memory_dream",
            executed_by_bot_user_id=bot_user_id,
            requested_operation=requested_operation.value,
            applied_operation=MemoryMutationAppliedOperation.NOOP.value,
            old_fact_id=None,
            new_fact_id=None,
            outcome=MemoryMutationOutcome.PROCESSING.value,
            reason_code="processing",
            created_at=created_at,
        )
        session.add(row)
        await session.flush()
        return _receipt(row)

    async def finalize(
        self,
        receipt_id: int,
        *,
        applied_operation: MemoryMutationAppliedOperation,
        old_fact_id: int | None,
        new_fact_id: int | None,
        outcome: MemoryMutationOutcome,
        reason_code: str,
        session: AsyncSession,
    ) -> MemoryMutationReceipt:
        await session.execute(
            update(MemoryMutationReceiptModel)
            .where(MemoryMutationReceiptModel.id == receipt_id)
            .values(
                applied_operation=applied_operation.value,
                old_fact_id=old_fact_id,
                new_fact_id=new_fact_id,
                outcome=outcome.value,
                reason_code=reason_code[:64],
            )
        )
        row = await session.get(MemoryMutationReceiptModel, receipt_id)
        if row is None:
            raise RuntimeError("memory mutation receipt disappeared during transaction")
        return _receipt(row)


def _receipt(row: MemoryMutationReceiptModel) -> MemoryMutationReceipt:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return MemoryMutationReceipt(
        id=row.id,
        mutation_id=row.mutation_id,
        idempotency_key=row.idempotency_key,
        claim_fingerprint=row.claim_fingerprint,
        target_fingerprint=row.target_fingerprint,
        trigger_source_type=row.trigger_source_type,
        trigger_event_id=row.trigger_event_id,
        dream_operation_id=row.dream_operation_id,
        conversation_key=row.conversation_key,
        current_group_id=row.current_group_id,
        turn_origin=row.turn_origin,
        delegation_mode=row.delegation_mode,
        trigger_actor_user_id=row.trigger_actor_user_id,
        decision_actor_type=MemoryDecisionActorType(row.decision_actor_type),
        decision_actor_id=row.decision_actor_id,
        executed_by_bot_user_id=row.executed_by_bot_user_id,
        requested_operation=MemoryMutationOperation(row.requested_operation),
        applied_operation=MemoryMutationAppliedOperation(row.applied_operation),
        old_fact_id=row.old_fact_id,
        new_fact_id=row.new_fact_id,
        outcome=MemoryMutationOutcome(row.outcome),
        reason_code=row.reason_code,
        created_at=created_at,
    )
