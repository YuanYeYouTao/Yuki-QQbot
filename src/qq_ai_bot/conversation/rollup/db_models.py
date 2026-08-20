"""SQLAlchemy models for the single-checkpoint rollup schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class ConversationScopeModel(Base):
    __tablename__ = "conversation_scopes"
    __table_args__ = (
        CheckConstraint(
            "(scope_type = 'private' AND private_peer_user_id IS NOT NULL AND group_id IS NULL) "
            "OR (scope_type = 'group' AND group_id IS NOT NULL "
            "AND private_peer_user_id IS NULL)",
            name="ck_conversation_scopes_identity",
        ),
        CheckConstraint(
            "generation >= 1 AND starts_after_event_id >= 0 AND last_event_id >= 0 "
            "AND last_generation_change_event_id >= 0 "
            "AND starts_after_event_id <= last_event_id "
            "AND last_generation_change_event_id <= last_event_id "
            "AND uncovered_event_count >= 0 AND uncovered_character_count >= 0",
            name="ck_conversation_scopes_state",
        ),
        Index(
            "uq_conversation_scopes_private",
            "bot_user_id",
            "private_peer_user_id",
            unique=True,
            sqlite_where=text("scope_type = 'private'"),
        ),
        Index(
            "uq_conversation_scopes_group",
            "bot_user_id",
            "group_id",
            unique=True,
            sqlite_where=text("scope_type = 'group'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    bot_user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    private_peer_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=True
    )
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    starts_after_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_generation_change_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uncovered_event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uncovered_character_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationRollupModel(Base):
    __tablename__ = "conversation_rollups"
    __table_args__ = (
        CheckConstraint(
            "summary_kind IN ('model', 'extractive')", name="ck_conversation_rollups_kind"
        ),
        CheckConstraint(
            "generation >= 1 AND covered_through_event_id >= 0 "
            "AND revision >= 1 AND length(summary_text) > 0",
            name="ck_conversation_rollups_state",
        ),
    )

    scope_id: Mapped[int] = mapped_column(
        ForeignKey("conversation_scopes.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    covered_through_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    summary_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationRollupJobModel(Base):
    __tablename__ = "conversation_rollup_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing')", name="ck_conversation_rollup_jobs_status"
        ),
        CheckConstraint(
            "generation >= 1 AND signal_revision >= 1 AND failure_count >= 0",
            name="ck_conversation_rollup_jobs_state",
        ),
        CheckConstraint(
            "(status = 'pending' AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_until IS NULL) OR (status = 'processing' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_conversation_rollup_jobs_lease",
        ),
        Index("ix_conversation_rollup_jobs_claim", "status", "next_attempt_at", "lease_until"),
    )

    scope_id: Mapped[int] = mapped_column(
        ForeignKey("conversation_scopes.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    signal_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
