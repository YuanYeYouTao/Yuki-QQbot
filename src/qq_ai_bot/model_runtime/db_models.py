"""SQLAlchemy model for content-free model invocation telemetry."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class ModelInvocationModel(Base):
    """One model request without prompt, user, tool payload, or reasoning text."""

    __tablename__ = "model_invocations"
    __table_args__ = (
        CheckConstraint("latency_seconds >= 0", name="ck_model_invocations_latency"),
        Index("ix_model_invocations_task_created", "task", "created_at"),
        Index("ix_model_invocations_profile_created", "profile_id", "created_at"),
        Index("ix_model_invocations_runtime_turn", "runtime_turn_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Opaque whole-turn correlation id (3.6.0-R1); NULL outside a bound turn.
    runtime_turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
