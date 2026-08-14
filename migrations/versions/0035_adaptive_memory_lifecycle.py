"""Add adaptive recall activation, attribution, and content-free receipts.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Native SQLite column rename preserves the external-content FTS table and triggers.
    op.execute("ALTER TABLE memory_facts RENAME COLUMN last_used_at TO last_injected_at")
    op.create_table(
        "memory_activation_states",
        sa.Column("fact_id", sa.Integer(), primary_key=True),
        sa.Column("activation", sa.Float(), nullable=False),
        sa.Column("activation_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_recalled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recall_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.CheckConstraint("activation BETWEEN 0 AND 1", name="ck_memory_activation_value"),
        sa.CheckConstraint("recall_count >= 0", name="ck_memory_activation_recall_count"),
        sa.CheckConstraint("revision >= 0", name="ck_memory_activation_revision"),
    )
    op.create_index(
        "ix_memory_activation_last_recalled",
        "memory_activation_states",
        ["last_recalled_at"],
    )
    op.execute(
        """
        INSERT INTO memory_activation_states (
            fact_id, activation, activation_updated_at, last_recalled_at, recall_count, revision
        )
        SELECT id,
            CASE
                WHEN source_type = 'explicit' OR authority = 'explicit' THEN 0.95
                WHEN kind = 'preference' THEN 0.80
                WHEN kind = 'episode' AND importance >= 4 THEN 0.75
                WHEN kind = 'episode' THEN 0.65
                ELSE 0.70
            END,
            created_at, NULL, 0, 0
        FROM memory_facts
        """
    )

    op.create_table(
        "memory_recall_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("conversation_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("trigger_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("injected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reinforced_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("turn_id", name="uq_memory_recall_receipts_turn_id"),
        sa.CheckConstraint(
            "mode IN ('none','lexical','hybrid','overview')",
            name="ck_memory_recall_receipt_mode",
        ),
        sa.CheckConstraint(
            "purpose IN ('background','recall','continuation','verify','correct')",
            name="ck_memory_recall_receipt_purpose",
        ),
    )
    op.create_index(
        "ix_memory_recall_receipts_expires",
        "memory_recall_receipts",
        ["expires_at", "id"],
    )
    op.create_table(
        "memory_recall_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.Column("target_role", sa.String(32), nullable=False),
        sa.Column("candidate", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("injected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reinforced", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("base_rank_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("subject_score", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("entity_score", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("temporal_score", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("kind_score", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("activation_score", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("rerank_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("selection_reason", sa.String(64), nullable=False, server_default=""),
        sa.Column("injected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reinforced_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["receipt_id"], ["memory_recall_receipts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("receipt_id", "fact_id", name="uq_memory_recall_item_fact"),
    )
    op.create_index(
        "ix_memory_recall_items_fact",
        "memory_recall_items",
        ["fact_id", "receipt_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_recall_items_fact", table_name="memory_recall_items")
    op.drop_table("memory_recall_items")
    op.drop_index("ix_memory_recall_receipts_expires", table_name="memory_recall_receipts")
    op.drop_table("memory_recall_receipts")
    op.drop_index("ix_memory_activation_last_recalled", table_name="memory_activation_states")
    op.drop_table("memory_activation_states")
    op.execute("ALTER TABLE memory_facts RENAME COLUMN last_injected_at TO last_used_at")
