"""Async persistence for privacy-preserving Planner run metadata."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.planner.db_models import PlannerRunModel
from qq_ai_bot.runtime.observability import claim_runtime_turn_id

_IDENTIFIER_LIKE = re.compile(r"(?<!\d)\d{5,20}(?!\d)")
_SENSITIVE_REASON_KEYS = ("content", "message", "prompt", "history", "ocr", "image")


@dataclass(frozen=True, slots=True)
class PlannerRunRecord:
    """Immutable projection of one redacted Planner run."""

    id: int
    conversation_key_hash: str
    trigger_message_id: str
    scope_type: str
    origin: str
    sender_user_id_hash: str
    group_id_hash: str | None
    necessity_score: float
    necessity_reasons_json: str
    gate_decision: str
    planner_used: bool
    planner_model: str
    planner_decision: str | None
    reason_code: str | None
    delivery_mode: str | None
    desired_messages: int | None
    tool_mode: str | None
    voice_mode: str | None
    voice_intent: str | None
    voice_tool_policy: str | None
    voice_reason: str | None
    voice_preference_change: str | None
    spontaneous_frequency: float | None
    recent_voice_ratio: float | None
    confidence: float | None
    latency_seconds: float
    interrupted: bool
    fallback_used: bool
    messages_planned: int
    messages_sent: int
    created_at: datetime
    finished_at: datetime | None
    error_category: str | None


@dataclass(frozen=True, slots=True)
class PlannerVoiceCadence:
    spontaneous_turns: int
    spontaneous_voice_turns: int

    @property
    def ratio(self) -> float:
        if self.spontaneous_turns <= 0:
            return 0.0
        return self.spontaneous_voice_turns / self.spontaneous_turns


def hash_planner_identifier(value: str, *, kind: str) -> str:
    """Hash one identifier with a domain separator before persistence."""

    payload = f"yuki-planner-v1\0{kind}\0{value}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


class PlannerRepository:
    """Record Planner observability without storing messages or raw QQ IDs."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def begin(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
        scope_type: str,
        origin: str,
        sender_user_id: str,
        group_id: str | None,
        necessity_score: float,
        necessity_reasons: Mapping[str, object],
        gate_decision: str,
        planner_used: bool,
        planner_model: str = "",
        fallback_used: bool = False,
        created_at: datetime | None = None,
    ) -> PlannerRunRecord:
        """Start one run; raw identifiers are hashed before the ORM sees them."""

        timestamp = _aware_utc(created_at or datetime.now(UTC))
        row = PlannerRunModel(
            runtime_turn_id=claim_runtime_turn_id(),
            conversation_key_hash=hash_planner_identifier(conversation_key, kind="conversation"),
            trigger_message_id=trigger_message_id[:128],
            scope_type=scope_type[:16],
            origin=origin[:32],
            sender_user_id_hash=hash_planner_identifier(sender_user_id, kind="sender"),
            group_id_hash=(hash_planner_identifier(group_id, kind="group") if group_id else None),
            necessity_score=_bounded(necessity_score, minimum=0.0, maximum=100.0),
            necessity_reasons_json=_bounded_json(
                _safe_reason_metadata(necessity_reasons), limit=8_000
            ),
            gate_decision=gate_decision[:32],
            planner_used=planner_used,
            planner_model=planner_model[:128],
            planner_decision=None,
            reason_code=None,
            delivery_mode=None,
            desired_messages=None,
            tool_mode=None,
            voice_mode=None,
            voice_intent=None,
            voice_tool_policy=None,
            voice_reason=None,
            voice_preference_change=None,
            spontaneous_frequency=None,
            recent_voice_ratio=None,
            confidence=None,
            latency_seconds=0.0,
            interrupted=False,
            fallback_used=fallback_used,
            messages_planned=0,
            messages_sent=0,
            created_at=timestamp,
            finished_at=None,
            error_category=None,
        )
        async with self._database.sessions() as session, session.begin():
            session.add(row)
            await session.flush()
            return _record(row)

    async def finish(
        self,
        run_id: int,
        *,
        planner_decision: str | None,
        reason_code: str | None,
        delivery_mode: str | None,
        desired_messages: int | None,
        tool_mode: str | None,
        voice_mode: str | None = None,
        voice_intent: str | None = None,
        voice_tool_policy: str | None = None,
        voice_reason: str | None = None,
        voice_preference_change: str | None = None,
        spontaneous_frequency: float | None = None,
        recent_voice_ratio: float | None = None,
        confidence: float | None,
        latency_seconds: float,
        interrupted: bool = False,
        fallback_used: bool = False,
        messages_planned: int = 0,
        messages_sent: int = 0,
        error_category: str | None = None,
        finished_at: datetime | None = None,
    ) -> PlannerRunRecord | None:
        """Finalize a run with only bounded structured decision metadata."""

        values: dict[str, object] = {
            "planner_decision": _optional_text(planner_decision, 32),
            "reason_code": _optional_text(reason_code, 64),
            "delivery_mode": _optional_text(delivery_mode, 32),
            "desired_messages": max(0, desired_messages) if desired_messages is not None else None,
            "tool_mode": _optional_text(tool_mode, 32),
            "voice_mode": _optional_text(voice_mode, 32),
            "voice_intent": _optional_text(voice_intent, 32),
            "voice_tool_policy": _optional_text(voice_tool_policy, 32),
            "voice_reason": _safe_summary(voice_reason, 300),
            "voice_preference_change": _optional_text(voice_preference_change, 32),
            "spontaneous_frequency": (
                _bounded(spontaneous_frequency, minimum=0.0, maximum=1.0)
                if spontaneous_frequency is not None
                else None
            ),
            "recent_voice_ratio": (
                _bounded(recent_voice_ratio, minimum=0.0, maximum=1.0)
                if recent_voice_ratio is not None
                else None
            ),
            "confidence": (
                _bounded(confidence, minimum=0.0, maximum=1.0) if confidence is not None else None
            ),
            "latency_seconds": max(0.0, latency_seconds),
            "interrupted": interrupted,
            "fallback_used": fallback_used,
            "messages_planned": max(0, messages_planned),
            "messages_sent": max(0, messages_sent),
            "error_category": _optional_text(error_category, 64),
            "finished_at": _aware_utc(finished_at or datetime.now(UTC)),
        }
        async with self._database.sessions() as session, session.begin():
            row = await session.get(PlannerRunModel, run_id)
            if row is None:
                return None
            for name, value in values.items():
                setattr(row, name, value)
            await session.flush()
            return _record(row)

    async def update_delivery(
        self,
        run_id: int,
        *,
        messages_sent: int,
        interrupted: bool | None = None,
        error_category: str | None = None,
    ) -> bool:
        """Update delivery counters after the plan itself has completed."""

        values: dict[str, object] = {"messages_sent": max(0, messages_sent)}
        if interrupted is not None:
            values["interrupted"] = interrupted
        if error_category is not None:
            values["error_category"] = error_category[:64]
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(PlannerRunModel).where(PlannerRunModel.id == run_id).values(**values)
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def get(self, run_id: int) -> PlannerRunRecord | None:
        async with self._database.sessions() as session:
            row = await session.get(PlannerRunModel, run_id)
            return _record(row) if row is not None else None

    async def latest(self) -> PlannerRunRecord | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(PlannerRunModel).order_by(
                    PlannerRunModel.created_at.desc(), PlannerRunModel.id.desc()
                )
            )
            return _record(row) if row is not None else None

    async def active_count(self) -> int:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(func.count(PlannerRunModel.id)).where(PlannerRunModel.finished_at.is_(None))
            )
            return int(value or 0)

    async def voice_cadence(
        self,
        conversation_key: str,
        *,
        limit: int = 20,
    ) -> PlannerVoiceCadence:
        """Summarize recent neutral Planner turns without reading chat text."""

        conversation_hash = hash_planner_identifier(conversation_key, kind="conversation")
        async with self._database.sessions() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(PlannerRunModel)
                        .where(
                            PlannerRunModel.conversation_key_hash == conversation_hash,
                            PlannerRunModel.planner_decision == "reply",
                            PlannerRunModel.voice_intent == "neutral",
                        )
                        .order_by(PlannerRunModel.created_at.desc(), PlannerRunModel.id.desc())
                        .limit(max(1, min(limit, 100)))
                    )
                ).all()
            )
        voice_modes = {"voice", "text_and_voice", "optional"}
        return PlannerVoiceCadence(
            spontaneous_turns=len(rows),
            spontaneous_voice_turns=sum(1 for row in rows if row.voice_mode in voice_modes),
        )


def _record(row: PlannerRunModel) -> PlannerRunRecord:
    return PlannerRunRecord(
        id=row.id,
        conversation_key_hash=row.conversation_key_hash,
        trigger_message_id=row.trigger_message_id,
        scope_type=row.scope_type,
        origin=row.origin,
        sender_user_id_hash=row.sender_user_id_hash,
        group_id_hash=row.group_id_hash,
        necessity_score=row.necessity_score,
        necessity_reasons_json=row.necessity_reasons_json,
        gate_decision=row.gate_decision,
        planner_used=row.planner_used,
        planner_model=row.planner_model,
        planner_decision=row.planner_decision,
        reason_code=row.reason_code,
        delivery_mode=row.delivery_mode,
        desired_messages=row.desired_messages,
        tool_mode=row.tool_mode,
        voice_mode=row.voice_mode,
        voice_intent=row.voice_intent,
        voice_tool_policy=row.voice_tool_policy,
        voice_reason=row.voice_reason,
        voice_preference_change=row.voice_preference_change,
        spontaneous_frequency=row.spontaneous_frequency,
        recent_voice_ratio=row.recent_voice_ratio,
        confidence=row.confidence,
        latency_seconds=row.latency_seconds,
        interrupted=row.interrupted,
        fallback_used=row.fallback_used,
        messages_planned=row.messages_planned,
        messages_sent=row.messages_sent,
        created_at=_aware_utc(row.created_at),
        finished_at=_aware_utc(row.finished_at) if row.finished_at else None,
        error_category=row.error_category,
    )


def _bounded_json(value: object, *, limit: int) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    if len(encoded) <= limit:
        return encoded
    return json.dumps({"truncated": True}, separators=(",", ":"))


def _safe_reason_metadata(value: object, *, key: str = "") -> object:
    """Allow scores/reason codes while rejecting accidental text and identifiers."""

    if any(token in key.casefold() for token in _SENSITIVE_REASON_KEYS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key)[:64]: _safe_reason_metadata(item_value, key=str(item_key))
            for item_key, item_value in list(value.items())[:50]
        }
    if isinstance(value, list | tuple):
        return [_safe_reason_metadata(item) for item in value[:50]]
    if isinstance(value, str):
        if _IDENTIFIER_LIKE.search(value):
            return "[REDACTED_IDENTIFIER]"
        return value[:64]
    if value is None or isinstance(value, bool | int | float):
        return value
    return type(value).__name__


def _optional_text(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else None


def _safe_summary(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split())
    redacted = _IDENTIFIER_LIKE.sub("[REDACTED_IDENTIFIER]", normalized)
    return redacted[:limit] or None


def _bounded(value: float, *, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
