"""SQLAlchemy persistence model for redacted Planner observability."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class PlannerRunModel(Base):
    """One Planner gate/decision run containing metadata but no chat text."""

    __tablename__ = "planner_runs"
    __table_args__ = (
        CheckConstraint(
            "necessity_score >= 0 AND necessity_score <= 100",
            name="ck_planner_runs_necessity_score",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_planner_runs_confidence",
        ),
        CheckConstraint(
            "desired_messages IS NULL OR desired_messages >= 0",
            name="ck_planner_runs_desired_messages",
        ),
        CheckConstraint(
            "spontaneous_frequency IS NULL OR "
            "(spontaneous_frequency >= 0 AND spontaneous_frequency <= 1)",
            name="ck_planner_runs_spontaneous_frequency",
        ),
        CheckConstraint(
            "recent_voice_ratio IS NULL OR (recent_voice_ratio >= 0 AND recent_voice_ratio <= 1)",
            name="ck_planner_runs_recent_voice_ratio",
        ),
        CheckConstraint("messages_planned >= 0", name="ck_planner_runs_messages_planned"),
        CheckConstraint("messages_sent >= 0", name="ck_planner_runs_messages_sent"),
        CheckConstraint("latency_seconds >= 0", name="ck_planner_runs_latency"),
        Index("ix_planner_runs_created", "created_at"),
        Index(
            "ix_planner_runs_conversation_created",
            "conversation_key_hash",
            "created_at",
        ),
        Index("ix_planner_runs_finished", "finished_at"),
        Index("ix_planner_runs_runtime_turn", "runtime_turn_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Opaque whole-turn correlation id (3.6.0-R1); NULL for rows written
    # outside a bound turn and for all pre-0037 history.
    runtime_turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_message_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    sender_user_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    group_id_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    necessity_score: Mapped[float] = mapped_column(Float, nullable=False)
    necessity_reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    gate_decision: Mapped[str] = mapped_column(String(32), nullable=False)
    planner_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    planner_model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    planner_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    desired_messages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    voice_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    voice_intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    voice_tool_policy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    voice_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    voice_preference_change: Mapped[str | None] = mapped_column(String(32), nullable=True)
    spontaneous_frequency: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent_voice_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    interrupted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    messages_planned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
