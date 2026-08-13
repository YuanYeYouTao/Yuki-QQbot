"""Add one-to-many Episode Dream recomposition results.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("memory_dream_operations", recreate="always") as batch:
        batch.drop_constraint("ck_memory_dream_operations_type", type_="check")
        batch.create_check_constraint(
            "ck_memory_dream_operations_type",
            "operation_type IN ('keep','merge','synthesize','recompose','contest','resolve')",
        )
    op.create_table(
        "memory_dream_operation_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.Integer(), nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("result_signature", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["memory_dream_operations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("operation_id", "position", name="uq_memory_dream_results_position"),
        sa.UniqueConstraint("operation_id", "fact_id", name="uq_memory_dream_results_fact"),
    )
    op.create_index(
        "ix_memory_dream_results_fact",
        "memory_dream_operation_results",
        ["fact_id", "operation_id"],
    )
    op.execute(
        "INSERT INTO memory_dream_operation_results "
        "(operation_id, fact_id, position, result_signature) "
        "SELECT id, output_fact_id, 0, result_signature FROM memory_dream_operations "
        "WHERE output_fact_id IS NOT NULL AND result_signature IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_memory_dream_results_fact", table_name="memory_dream_operation_results")
    op.drop_table("memory_dream_operation_results")
    op.execute(
        "DELETE FROM memory_mutation_receipts WHERE dream_operation_id IN "
        "(SELECT id FROM memory_dream_operations WHERE operation_type = 'recompose')"
    )
    op.execute(
        "DELETE FROM memory_dream_operation_sources WHERE operation_id IN "
        "(SELECT id FROM memory_dream_operations WHERE operation_type = 'recompose')"
    )
    op.execute(
        "UPDATE memory_dream_fact_checkpoints SET last_operation_id = NULL WHERE "
        "last_operation_id IN "
        "(SELECT id FROM memory_dream_operations WHERE operation_type = 'recompose')"
    )
    op.execute("DELETE FROM memory_dream_operations WHERE operation_type = 'recompose'")
    with op.batch_alter_table("memory_dream_operations", recreate="always") as batch:
        batch.drop_constraint("ck_memory_dream_operations_type", type_="check")
        batch.create_check_constraint(
            "ck_memory_dream_operations_type",
            "operation_type IN ('keep','merge','synthesize','contest','resolve')",
        )
