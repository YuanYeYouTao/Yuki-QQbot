"""Add Dream previews, reflection provenance, and evidence compaction state.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_dream_operations",
        sa.Column(
            "decision_focuses_json", sa.Text(), nullable=False, server_default="[]"
        ),
    )
    op.create_table(
        "memory_self_reflection_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.Column("result_kind", sa.String(16), nullable=False),
        sa.Column("result_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["memory_self_reflection_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "run_id", "result_kind", "result_index", name="uq_self_reflection_result_position"
        ),
        sa.CheckConstraint(
            "result_kind IN ('episode','proposal')", name="ck_self_reflection_result_kind"
        ),
    )
    op.create_index(
        "ix_self_reflection_results_fact",
        "memory_self_reflection_results",
        ["fact_id", "run_id"],
    )

    op.create_table(
        "memory_dream_cluster_previews",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("proposal_json", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("source_characters", sa.Integer(), nullable=False),
        sa.Column("output_characters", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["cluster_id"], ["memory_dream_clusters.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("public_id", name="uq_memory_dream_previews_public_id"),
        sa.CheckConstraint(
            "status IN ('ready','applied','stale','superseded')",
            name="ck_memory_dream_previews_status",
        ),
    )
    op.create_index(
        "ix_memory_dream_previews_cluster_status",
        "memory_dream_cluster_previews",
        ["cluster_id", "status", "id"],
    )

    op.create_table(
        "memory_evidence_compaction_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("scan_after_fact_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scanned_facts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_before", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_after", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("public_id", name="uq_evidence_compaction_runs_public_id"),
        sa.CheckConstraint(
            "status IN ('running','completed','partial_failed','failed')",
            name="ck_evidence_compaction_runs_status",
        ),
    )
    op.create_index(
        "ix_evidence_compaction_runs_status_created",
        "memory_evidence_compaction_runs",
        ["status", "created_at"],
    )

    op.create_table(
        "memory_evidence_compaction_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.Column("provenance_type", sa.String(24), nullable=False),
        sa.Column("dream_operation_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("evidence_before", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_after", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["memory_evidence_compaction_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["dream_operation_id"], ["memory_dream_operations.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("run_id", "fact_id", name="uq_evidence_compaction_items_fact"),
        sa.CheckConstraint(
            "provenance_type IN ('self_reflection','dream')",
            name="ck_evidence_compaction_items_provenance",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','skipped','failed')",
            name="ck_evidence_compaction_items_status",
        ),
    )
    op.create_index(
        "ix_evidence_compaction_items_run_status",
        "memory_evidence_compaction_items",
        ["run_id", "status", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evidence_compaction_items_run_status",
        table_name="memory_evidence_compaction_items",
    )
    op.drop_table("memory_evidence_compaction_items")
    op.drop_index(
        "ix_evidence_compaction_runs_status_created",
        table_name="memory_evidence_compaction_runs",
    )
    op.drop_table("memory_evidence_compaction_runs")
    op.drop_index(
        "ix_memory_dream_previews_cluster_status",
        table_name="memory_dream_cluster_previews",
    )
    op.drop_table("memory_dream_cluster_previews")
    op.drop_index(
        "ix_self_reflection_results_fact", table_name="memory_self_reflection_results"
    )
    op.drop_table("memory_self_reflection_results")
    with op.batch_alter_table("memory_dream_operations", recreate="always") as batch:
        batch.drop_column("decision_focuses_json")
