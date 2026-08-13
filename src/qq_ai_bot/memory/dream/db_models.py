"""Persistent run, cluster, operation, and checkpoint state for Memory Dream."""

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
)
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class MemoryDreamRuntimeModel(Base):
    __tablename__ = "memory_dream_runtime"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    initialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryDreamRunModel(Base):
    __tablename__ = "memory_dream_runs"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_memory_dream_runs_public_id"),
        UniqueConstraint("scheduled_slot", name="uq_memory_dream_runs_scheduled_slot"),
        CheckConstraint("mode IN ('full','incremental')", name="ck_memory_dream_runs_mode"),
        CheckConstraint(
            "status IN ('planned','running','partial_failed','completed','cancelled',"
            "'rolling_back','rolled_back')",
            name="ck_memory_dream_runs_status",
        ),
        Index("ix_memory_dream_runs_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    scheduled_slot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_max_fact_id: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="SET NULL"), nullable=True
    )
    statistics_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_clusters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_clusters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryDreamClusterModel(Base):
    __tablename__ = "memory_dream_clusters"
    __table_args__ = (
        UniqueConstraint("run_id", "cluster_key", name="uq_memory_dream_clusters_run_key"),
        CheckConstraint(
            "status IN ('pending','processing','completed','failed','stale',"
            "'skipped','rolled_back')",
            name="ck_memory_dream_clusters_status",
        ),
        CheckConstraint("kind IN ('fact','preference','episode')", name="ck_dream_cluster_kind"),
        Index("ix_memory_dream_clusters_run_status", "run_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("memory_dream_runs.id", ondelete="CASCADE"), nullable=False
    )
    cluster_key: Mapped[str] = mapped_column(String(64), nullable=False)
    partition_key: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    fact_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    operation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryDreamOperationModel(Base):
    __tablename__ = "memory_dream_operations"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_memory_dream_operations_public_id"),
        UniqueConstraint(
            "cluster_id", "action_index", name="uq_memory_dream_operations_cluster_action"
        ),
        CheckConstraint(
            "operation_type IN ('keep','merge','synthesize','contest','resolve')",
            name="ck_memory_dream_operations_type",
        ),
        CheckConstraint(
            "status IN ('processing','committed','rolled_back')",
            name="ck_memory_dream_operations_status",
        ),
        Index("ix_memory_dream_operations_cluster", "cluster_id", "action_index"),
        Index("ix_memory_dream_operations_output", "output_fact_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("memory_dream_clusters.id", ondelete="CASCADE"), nullable=False
    )
    action_index: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    anchor_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="SET NULL"), nullable=True
    )
    output_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="SET NULL"), nullable=True
    )
    source_fact_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    added_evidence_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    added_relation_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    result_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryDreamOperationSourceModel(Base):
    __tablename__ = "memory_dream_operation_sources"
    __table_args__ = (
        UniqueConstraint("operation_id", "fact_id", name="uq_memory_dream_operation_sources_fact"),
        Index("ix_memory_dream_operation_sources_fact", "fact_id", "operation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(
        ForeignKey("memory_dream_operations.id", ondelete="CASCADE"), nullable=False
    )
    fact_id: Mapped[int] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    before_status: Mapped[str] = mapped_column(String(16), nullable=False)
    before_conflict_state: Mapped[str] = mapped_column(String(16), nullable=False)
    before_invalidated_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    before_authority: Mapped[str] = mapped_column(String(16), nullable=False)
    before_confidence: Mapped[float] = mapped_column(nullable=False)
    before_last_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    before_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    after_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MemoryDreamFactCheckpointModel(Base):
    __tablename__ = "memory_dream_fact_checkpoints"

    fact_id: Mapped[int] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="CASCADE"), primary_key=True
    )
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    last_operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_dream_operations.id", ondelete="SET NULL"), nullable=True
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
