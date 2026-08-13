"""Add Memory Dream runs, lineage, checkpoints, and receipt provenance.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_dream_runtime",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "memory_dream_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("scheduled_slot", sa.String(64), nullable=True),
        sa.Column("snapshot_max_fact_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("statistics_json", sa.Text(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_clusters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_clusters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["people.user_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("public_id", name="uq_memory_dream_runs_public_id"),
        sa.UniqueConstraint("scheduled_slot", name="uq_memory_dream_runs_scheduled_slot"),
        sa.CheckConstraint("mode IN ('full','incremental')", name="ck_memory_dream_runs_mode"),
        sa.CheckConstraint(
            "status IN ('planned','running','partial_failed','completed','cancelled',"
            "'rolling_back','rolled_back')",
            name="ck_memory_dream_runs_status",
        ),
    )
    op.create_index(
        "ix_memory_dream_runs_status_created",
        "memory_dream_runs",
        ["status", "created_at"],
    )
    op.create_table(
        "memory_dream_clusters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("cluster_key", sa.String(64), nullable=False),
        sa.Column("partition_key", sa.String(64), nullable=False),
        sa.Column("bot_user_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("fact_ids_json", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("operation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["memory_dream_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "cluster_key", name="uq_memory_dream_clusters_run_key"),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed','stale','skipped','rolled_back')",
            name="ck_memory_dream_clusters_status",
        ),
        sa.CheckConstraint("kind IN ('fact','preference','episode')", name="ck_dream_cluster_kind"),
    )
    op.create_index(
        "ix_memory_dream_clusters_run_status",
        "memory_dream_clusters",
        ["run_id", "status", "id"],
    )
    op.create_table(
        "memory_dream_operations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("operation_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("anchor_fact_id", sa.Integer(), nullable=True),
        sa.Column("output_fact_id", sa.Integer(), nullable=True),
        sa.Column("source_fact_ids_json", sa.Text(), nullable=False),
        sa.Column("added_evidence_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("added_relation_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("result_signature", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["cluster_id"], ["memory_dream_clusters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["anchor_fact_id"], ["memory_facts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["output_fact_id"], ["memory_facts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("public_id", name="uq_memory_dream_operations_public_id"),
        sa.UniqueConstraint(
            "cluster_id", "action_index", name="uq_memory_dream_operations_cluster_action"
        ),
        sa.CheckConstraint(
            "operation_type IN ('keep','merge','synthesize','contest','resolve')",
            name="ck_memory_dream_operations_type",
        ),
        sa.CheckConstraint(
            "status IN ('processing','committed','rolled_back')",
            name="ck_memory_dream_operations_status",
        ),
    )
    op.create_index(
        "ix_memory_dream_operations_cluster",
        "memory_dream_operations",
        ["cluster_id", "action_index"],
    )
    op.create_index(
        "ix_memory_dream_operations_output", "memory_dream_operations", ["output_fact_id"]
    )
    op.create_table(
        "memory_dream_operation_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.Integer(), nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("before_status", sa.String(16), nullable=False),
        sa.Column("before_conflict_state", sa.String(16), nullable=False),
        sa.Column("before_invalidated_reason", sa.String(40), nullable=True),
        sa.Column("before_authority", sa.String(16), nullable=False),
        sa.Column("before_confidence", sa.Float(), nullable=False),
        sa.Column("before_last_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("before_signature", sa.String(64), nullable=False),
        sa.Column("after_signature", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["memory_dream_operations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "operation_id", "fact_id", name="uq_memory_dream_operation_sources_fact"
        ),
    )
    op.create_index(
        "ix_memory_dream_operation_sources_fact",
        "memory_dream_operation_sources",
        ["fact_id", "operation_id"],
    )
    op.create_table(
        "memory_dream_fact_checkpoints",
        sa.Column("fact_id", sa.Integer(), primary_key=True),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("last_operation_id", sa.Integer(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_operation_id"], ["memory_dream_operations.id"], ondelete="SET NULL"
        ),
    )

    with op.batch_alter_table("memory_mutation_receipts", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "trigger_source_type",
                sa.String(24),
                nullable=False,
                server_default="chat_event",
            )
        )
        batch.add_column(sa.Column("dream_operation_id", sa.Integer(), nullable=True))
        batch.alter_column("trigger_event_id", existing_type=sa.Integer(), nullable=True)
        batch.create_foreign_key(
            "fk_memory_mutation_receipts_dream_operation",
            "memory_dream_operations",
            ["dream_operation_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint(
            "ck_memory_mutation_trigger_source",
            "(trigger_source_type = 'chat_event' AND trigger_event_id IS NOT NULL "
            "AND dream_operation_id IS NULL) OR "
            "(trigger_source_type = 'dream_operation' AND trigger_event_id IS NULL "
            "AND dream_operation_id IS NOT NULL)",
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM memory_mutation_receipts "
        "WHERE trigger_source_type = 'dream_operation'"
    )
    with op.batch_alter_table("memory_mutation_receipts", recreate="always") as batch:
        batch.drop_constraint("ck_memory_mutation_trigger_source", type_="check")
        batch.drop_constraint("fk_memory_mutation_receipts_dream_operation", type_="foreignkey")
        batch.alter_column("trigger_event_id", existing_type=sa.Integer(), nullable=False)
        batch.drop_column("dream_operation_id")
        batch.drop_column("trigger_source_type")
    op.drop_table("memory_dream_fact_checkpoints")
    op.drop_index(
        "ix_memory_dream_operation_sources_fact", table_name="memory_dream_operation_sources"
    )
    op.drop_table("memory_dream_operation_sources")
    op.drop_index("ix_memory_dream_operations_output", table_name="memory_dream_operations")
    op.drop_index("ix_memory_dream_operations_cluster", table_name="memory_dream_operations")
    op.drop_table("memory_dream_operations")
    op.drop_index("ix_memory_dream_clusters_run_status", table_name="memory_dream_clusters")
    op.drop_table("memory_dream_clusters")
    op.drop_index("ix_memory_dream_runs_status_created", table_name="memory_dream_runs")
    op.drop_table("memory_dream_runs")
    op.drop_table("memory_dream_runtime")
