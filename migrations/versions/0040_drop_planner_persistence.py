"""Drop planner_runs and migrate leftover Planner runtime overrides.

3.6.0-R5: Conversation Runtime owns admission policy.  This revision deletes
Planner persistence after 0039 cadence backfill and rewrites override keys
using the frozen R1 mapping.  Downgrade cannot restore deleted rows.

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect, text

from qq_ai_bot.admin.config_migration_3_6 import (
    PLANNER_CONFIG_DELETED_KEYS,
    PLANNER_CONFIG_MIGRATION_MAP,
)

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXTRA_DELETED_KEYS = ("mcp.tool_selection_mode",)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "runtime_config_overrides" in tables:
        _migrate_overrides(connection)
    if "planner_runs" not in tables:
        return
    for index_name in (
        "ix_planner_runs_finished",
        "ix_planner_runs_conversation_created",
        "ix_planner_runs_created",
    ):
        connection.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")
    connection.exec_driver_sql("DROP TABLE planner_runs")


def downgrade() -> None:
    raise RuntimeError(
        "0040 cannot restore deleted planner_runs; restore the pre-upgrade "
        "SQLite + WAL/SHM snapshot and start 3.5.3"
    )


def _migrate_overrides(connection: object) -> None:
    rows = list(
        connection.execute(
            text("SELECT id, config_key, scope_type, scope_id FROM runtime_config_overrides")
        )
    )
    existing = {(row.config_key, row.scope_type, row.scope_id) for row in rows}
    deleted = set(PLANNER_CONFIG_DELETED_KEYS) | set(_EXTRA_DELETED_KEYS)
    for row in rows:
        key = str(row.config_key)
        scope = (key, str(row.scope_type), str(row.scope_id))
        if key in deleted:
            connection.execute(
                text("DELETE FROM runtime_config_overrides WHERE id = :id"),
                {"id": row.id},
            )
            existing.discard(scope)
            continue
        new_key = PLANNER_CONFIG_MIGRATION_MAP.get(key)
        if new_key is None:
            continue
        target = (new_key, str(row.scope_type), str(row.scope_id))
        if target in existing:
            connection.execute(
                text("DELETE FROM runtime_config_overrides WHERE id = :id"),
                {"id": row.id},
            )
            existing.discard(scope)
            continue
        connection.execute(
            text("UPDATE runtime_config_overrides SET config_key = :key WHERE id = :id"),
            {"key": new_key, "id": row.id},
        )
        existing.discard(scope)
        existing.add(target)
