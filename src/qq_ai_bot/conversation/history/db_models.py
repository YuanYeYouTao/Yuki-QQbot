"""SQLAlchemy tables for conversation history rollup. No Memory V2 FKs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class ConversationHistoryStateModel(Base):
    __tablename__ = "conversation_history_states"
    __table_args__ = (
        CheckConstraint(
            "("
            "scope_type = 'private' AND private_peer_user_id IS NOT NULL "
            "AND group_id IS NULL"
            ") OR ("
            "scope_type = 'group' AND group_id IS NOT NULL "
            "AND private_peer_user_id IS NULL"
            ")",
            name="ck_conversation_history_states_identity",
        ),
        CheckConstraint(
            "last_seen_event_id >= 0 AND active_frontier_end_event_id >= 0 "
            "AND pending_event_count >= 0 AND pending_character_count >= 0 "
            "AND revision >= 0",
            name="ck_conversation_history_states_counters",
        ),
        Index(
            "uq_conversation_history_states_identity",
            "bot_user_id",
            "scope_type",
            text("ifnull(private_peer_user_id, '')"),
            text("ifnull(group_id, '')"),
            text("ifnull(reset_at, '')"),
            unique=True,
        ),
        Index(
            "ix_conversation_history_states_frontier",
            "bot_user_id",
            "scope_type",
            "active_frontier_end_event_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    private_peer_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_frontier_end_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_character_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationHistorySummaryModel(Base):
    __tablename__ = "conversation_history_summaries"
    __table_args__ = (
        CheckConstraint("level >= 0", name="ck_conversation_history_summaries_level"),
        CheckConstraint(
            "start_event_id <= end_event_id",
            name="ck_conversation_history_summaries_range",
        ),
        CheckConstraint(
            "status IN ('active', 'rolled_up', 'invalidated')",
            name="ck_conversation_history_summaries_status",
        ),
        CheckConstraint(
            "mode IN ('extractive', 'model_summary')",
            name="ck_conversation_history_summaries_mode",
        ),
        CheckConstraint(
            "trust IN ('extractive_compact', 'model_summary')",
            name="ck_conversation_history_summaries_trust",
        ),
        CheckConstraint(
            "mode != 'extractive' OR level = 0",
            name="ck_conversation_history_summaries_extractive_l0",
        ),
        CheckConstraint(
            "(status = 'active' AND replaced_by_summary_id IS NULL) OR "
            "(status = 'rolled_up' AND replaced_by_summary_id IS NOT NULL) OR "
            "(status = 'invalidated')",
            name="ck_conversation_history_summaries_replaced_by",
        ),
        CheckConstraint(
            "source_event_count >= 0 AND source_character_count >= 0 "
            "AND output_character_count >= 0",
            name="ck_conversation_history_summaries_counts",
        ),
        Index(
            "uq_conversation_history_summaries_active_fingerprint",
            "state_id",
            "source_fingerprint",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "ix_conversation_history_summaries_state_range",
            "state_id",
            "status",
            "start_event_id",
            "end_event_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    state_id: Mapped[int] = mapped_column(
        ForeignKey("conversation_history_states.id", ondelete="CASCADE"),
        nullable=False,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    start_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    end_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    start_occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_character_count: Mapped[int] = mapped_column(Integer, nullable=False)
    output_character_count: Mapped[int] = mapped_column(Integer, nullable=False)
    structured_payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    rendered_text: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    trust: Mapped[str] = mapped_column(String(32), nullable=False)
    summarizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    replaced_by_summary_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation_history_summaries.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationHistorySummaryMemberModel(Base):
    __tablename__ = "conversation_history_summary_members"
    __table_args__ = (
        CheckConstraint(
            "member_type IN ('event', 'summary')",
            name="ck_conversation_history_summary_members_type",
        ),
        CheckConstraint(
            "("
            "member_type = 'event' AND source_event_id IS NOT NULL "
            "AND source_summary_id IS NULL"
            ") OR ("
            "member_type = 'summary' AND source_summary_id IS NOT NULL "
            "AND source_event_id IS NULL"
            ")",
            name="ck_conversation_history_summary_members_source",
        ),
        UniqueConstraint(
            "summary_id",
            "ordinal",
            name="uq_conversation_history_summary_members_ordinal",
        ),
        Index(
            "uq_conversation_history_summary_members_event",
            "summary_id",
            "source_event_id",
            unique=True,
            sqlite_where=text("member_type = 'event'"),
        ),
        Index(
            "uq_conversation_history_summary_members_summary",
            "summary_id",
            "source_summary_id",
            unique=True,
            sqlite_where=text("member_type = 'summary'"),
        ),
        Index("ix_conversation_history_summary_members_event", "source_event_id"),
        Index("ix_conversation_history_summary_members_child", "source_summary_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    summary_id: Mapped[int] = mapped_column(
        ForeignKey("conversation_history_summaries.id", ondelete="CASCADE"),
        nullable=False,
    )
    member_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_summary_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation_history_summaries.id", ondelete="RESTRICT"),
        nullable=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationHistoryRollupJobModel(Base):
    __tablename__ = "conversation_history_rollup_jobs"
    __table_args__ = (
        CheckConstraint(
            "job_kind IN ('raw_range', 'summary_rollup', 'rebuild')",
            name="ck_conversation_history_rollup_jobs_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')",
            name="ck_conversation_history_rollup_jobs_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('summary', 'no_change')",
            name="ck_conversation_history_rollup_jobs_outcome",
        ),
        CheckConstraint(
            "source_level >= 0 AND source_start_id <= source_end_id AND attempts >= 0",
            name="ck_conversation_history_rollup_jobs_range",
        ),
        CheckConstraint(
            "(status != 'done') OR "
            "(outcome = 'summary' AND result_summary_id IS NOT NULL) OR "
            "(outcome = 'no_change' AND result_summary_id IS NULL)",
            name="ck_conversation_history_rollup_jobs_done",
        ),
        UniqueConstraint(
            "state_id",
            "job_kind",
            "source_fingerprint",
            "summarizer_version",
            name="uq_conversation_history_rollup_jobs_idempotent",
        ),
        Index(
            "ix_conversation_history_rollup_jobs_pending",
            "status",
            "next_attempt_at",
        ),
        Index("ix_conversation_history_rollup_jobs_lease", "lease_until"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    state_id: Mapped[int] = mapped_column(
        ForeignKey("conversation_history_states.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_level: Mapped[int] = mapped_column(Integer, nullable=False)
    source_start_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_end_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    summarizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result_summary_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation_history_summaries.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
