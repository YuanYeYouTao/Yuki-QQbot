"""Persistence for content-free runtime turn observations (3.6.0-R1)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import RuntimeTurnObservationModel
from qq_ai_bot.runtime.observability import RuntimeTurnObservation


class RuntimeTurnObservationRepository:
    """Store one bounded row per admitted turn; never any content."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record_turn(self, observation: RuntimeTurnObservation) -> None:
        row = RuntimeTurnObservationModel(
            runtime_turn_id=observation.runtime_turn_id[:64],
            origin=observation.origin.value[:32],
            scope_type=observation.scope_type[:16],
            conversation_key_hash=observation.conversation_key_hash,
            admission_outcome=observation.admission_outcome,
            handled=observation.handled,
            sent_messages=max(0, observation.sent_messages),
            error_category=observation.error_category,
            total_latency_ms=max(0, observation.total_latency_ms),
            created_at=observation.created_at,
            expires_at=observation.expires_at,
        )
        async with self._database.sessions() as session, session.begin():
            session.add(row)

    async def cleanup_expired(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """Delete one bounded batch of expired rows; call repeatedly to drain."""

        cutoff = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            ids = tuple(
                await session.scalars(
                    select(RuntimeTurnObservationModel.id)
                    .where(RuntimeTurnObservationModel.expires_at <= cutoff)
                    .order_by(
                        RuntimeTurnObservationModel.expires_at,
                        RuntimeTurnObservationModel.id,
                    )
                    .limit(max(1, limit))
                )
            )
            if not ids:
                return 0
            await session.execute(
                delete(RuntimeTurnObservationModel).where(RuntimeTurnObservationModel.id.in_(ids))
            )
            return len(ids)
