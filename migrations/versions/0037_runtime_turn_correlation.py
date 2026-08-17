"""Add opaque runtime turn correlation and content-free turn observations.

3.6.0-R1: every admitted turn gets a random opaque ``runtime_turn_id`` that
joins planner runs, model invocations, tool invocations and memory recall
receipts.  ``memory_recall_receipts.turn_id`` keeps its pre-existing meaning
(the receipt's own unique id) and is not repurposed.  The new
``runtime_turn_observations`` table stores one bounded, content-free row per
turn with a 30-day retention purged in batches by maintenance.  All existing
rows keep ``runtime_turn_id`` NULL.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CORRELATED_TABLES = (
    ("planner_runs", "ix_planner_runs_runtime_turn"),
    ("model_invocations", "ix_model_invocations_runtime_turn"),
    ("tool_invocations", "ix_tool_invocations_runtime_turn"),
    ("memory_recall_receipts", "ix_memory_recall_receipts_runtime_turn"),
)


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    for table, index_name in _CORRELATED_TABLES:
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "runtime_turn_id" not in columns:
            op.add_column(table, sa.Column("runtime_turn_id", sa.String(64), nullable=True))
        indexes = {index["name"] for index in inspector.get_indexes(table)}
        if index_name not in indexes:
            op.create_index(index_name, table, ["runtime_turn_id"])

    if "runtime_turn_observations" not in inspector.get_table_names():
        op.create_table(
            "runtime_turn_observations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("runtime_turn_id", sa.String(64), nullable=False),
            sa.Column("origin", sa.String(32), nullable=False),
            sa.Column("scope_type", sa.String(16), nullable=False),
            sa.Column("conversation_key_hash", sa.String(64), nullable=True),
            sa.Column("admission_outcome", sa.String(64), nullable=True),
            sa.Column("handled", sa.Boolean(), nullable=False),
            sa.Column("sent_messages", sa.Integer(), nullable=False),
            sa.Column("error_category", sa.String(128), nullable=True),
            sa.Column("total_latency_ms", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("runtime_turn_id"),
            sa.CheckConstraint("sent_messages >= 0", name="ck_runtime_turn_obs_sent_messages"),
            sa.CheckConstraint("total_latency_ms >= 0", name="ck_runtime_turn_obs_latency"),
        )
        op.create_index(
            "ix_runtime_turn_observations_expires",
            "runtime_turn_observations",
            ["expires_at", "id"],
        )
        op.create_index(
            "ix_runtime_turn_observations_created",
            "runtime_turn_observations",
            ["created_at"],
        )
        op.create_index(
            "ix_runtime_turn_observations_origin_created",
            "runtime_turn_observations",
            ["origin", "created_at"],
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if "runtime_turn_observations" in inspector.get_table_names():
        op.drop_table("runtime_turn_observations")
    for table, index_name in _CORRELATED_TABLES:
        indexes = {index["name"] for index in inspector.get_indexes(table)}
        if index_name in indexes:
            op.drop_index(index_name, table_name=table)
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "runtime_turn_id" in columns:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("runtime_turn_id")
