"""Confirmed-delivery cadence owner for voice (and later emoji) effects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil

from sqlalchemy import delete, select

from qq_ai_bot.conversation.db_models import ReplyEffectEventModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.observability import claim_runtime_turn_id, stable_identifier_hash

_CADENCE_WINDOW = 20
_MAX_ROWS_PER_CONVERSATION = 100
_RETENTION_DAYS = 90


def conversation_key_hash(conversation_key: str) -> str:
    """Hash a conversation key with the same algorithm as historical planner_runs."""

    return stable_identifier_hash(conversation_key, kind="conversation")


def source_event_hash(*, source: str, raw: str) -> str:
    """Stable unique hash for ``(source, source_event_hash)``."""

    return stable_identifier_hash(f"{source}:{raw}", kind="reply-effect")


@dataclass(frozen=True, slots=True)
class VoiceCadence:
    eligible_turns: int
    voice_turns: int

    @property
    def ratio(self) -> float:
        if self.eligible_turns <= 0:
            return 0.0
        return self.voice_turns / self.eligible_turns


class ReplyEffectRepository:
    """Persist one cadence row after confirmed delivery and prune old rows."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(
        self,
        *,
        conversation_key: str,
        source_event_id: str,
        text_sent: bool,
        voice_sent: bool,
        emoji_sent: bool,
        voice_request_basis: str,
        source: str = "runtime",
        occurred_at: datetime | None = None,
        runtime_turn_id: str | None = None,
        voice_cadence_eligible: bool | None = None,
    ) -> None:
        if voice_request_basis not in {"user_requested", "agent_initiated", "none"}:
            raise ValueError("invalid voice_request_basis")
        eligible = (
            voice_request_basis != "user_requested"
            if voice_cadence_eligible is None
            else voice_cadence_eligible
        )
        timestamp = _aware_utc(occurred_at or datetime.now(UTC))
        row = ReplyEffectEventModel(
            conversation_key_hash=conversation_key_hash(conversation_key),
            runtime_turn_id=runtime_turn_id or claim_runtime_turn_id(),
            source_event_hash=source_event_hash(source=source, raw=source_event_id),
            text_sent=text_sent,
            voice_sent=voice_sent,
            emoji_sent=emoji_sent,
            voice_cadence_eligible=eligible,
            voice_request_basis=voice_request_basis,
            source=source,
            occurred_at=timestamp,
            recorded_at=datetime.now(UTC),
        )
        async with self._database.sessions() as session:
            existing = await session.scalar(
                select(ReplyEffectEventModel.id).where(
                    ReplyEffectEventModel.source == source,
                    ReplyEffectEventModel.source_event_hash == row.source_event_hash,
                )
            )
            if existing is None:
                session.add(row)
            await session.commit()
        await self.maintain(conversation_key)

    async def voice_cadence(self, conversation_key: str) -> VoiceCadence:
        conversation_hash = conversation_key_hash(conversation_key)
        async with self._database.sessions() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(ReplyEffectEventModel)
                        .where(
                            ReplyEffectEventModel.conversation_key_hash == conversation_hash,
                            ReplyEffectEventModel.voice_cadence_eligible.is_(True),
                        )
                        .order_by(
                            ReplyEffectEventModel.occurred_at.desc(),
                            ReplyEffectEventModel.id.desc(),
                        )
                        .limit(_CADENCE_WINDOW)
                    )
                ).all()
            )
        return VoiceCadence(
            eligible_turns=len(rows),
            voice_turns=sum(1 for row in rows if row.voice_sent),
        )

    def spontaneous_allowed(self, cadence: VoiceCadence, *, frequency: float) -> bool:
        if frequency <= 0:
            return False
        budget = ceil((cadence.eligible_turns + 1) * min(1.0, frequency))
        return cadence.voice_turns < budget

    async def maintain(self, conversation_key: str) -> None:
        conversation_hash = conversation_key_hash(conversation_key)
        cutoff = datetime.now(UTC) - timedelta(days=_RETENTION_DAYS)
        async with self._database.sessions() as session:
            await session.execute(
                delete(ReplyEffectEventModel).where(
                    ReplyEffectEventModel.conversation_key_hash == conversation_hash,
                    ReplyEffectEventModel.recorded_at < cutoff,
                )
            )
            ids = tuple(
                (
                    await session.scalars(
                        select(ReplyEffectEventModel.id)
                        .where(ReplyEffectEventModel.conversation_key_hash == conversation_hash)
                        .order_by(
                            ReplyEffectEventModel.occurred_at.desc(),
                            ReplyEffectEventModel.id.desc(),
                        )
                    )
                ).all()
            )
            extra = ids[_MAX_ROWS_PER_CONVERSATION:]
            if extra:
                await session.execute(
                    delete(ReplyEffectEventModel).where(ReplyEffectEventModel.id.in_(extra))
                )
            await session.commit()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
