"""SQLAlchemy model for reply-effect cadence events (R4 / 0039)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class ReplyEffectEventModel(Base):
    """One confirmed-delivery cadence row per turn.  No chat text or asset paths."""

    __tablename__ = "reply_effect_events"
    __table_args__ = (
        UniqueConstraint("source", "source_event_hash", name="uq_reply_effect_events_source"),
        Index(
            "ix_reply_effect_events_conversation_occurred",
            "conversation_key_hash",
            "occurred_at",
            "id",
        ),
        CheckConstraint(
            "voice_request_basis IN ('user_requested', 'agent_initiated', 'none')",
            name="ck_reply_effect_events_voice_request_basis",
        ),
        CheckConstraint(
            "source IN ('runtime', 'migrated_planner')",
            name="ck_reply_effect_events_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    text_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    voice_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    emoji_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    voice_cadence_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    voice_request_basis: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
