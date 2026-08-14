"""Rename the memory attribution runtime override.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KEY = "memory.usage_reporting_enabled"
_NEW_KEY = "memory.usage_attribution_enabled"


def upgrade() -> None:
    _rename_override(_OLD_KEY, _NEW_KEY)


def downgrade() -> None:
    _rename_override(_NEW_KEY, _OLD_KEY)


def _rename_override(source: str, target: str) -> None:
    connection = op.get_bind()
    connection.exec_driver_sql(
        """
        DELETE FROM runtime_config_overrides AS source
        WHERE source.config_key = ?
          AND EXISTS (
              SELECT 1
              FROM runtime_config_overrides AS target
              WHERE target.config_key = ?
                AND target.scope_type = source.scope_type
                AND target.scope_id = source.scope_id
          )
        """,
        (source, target),
    )
    connection.exec_driver_sql(
        "UPDATE runtime_config_overrides SET config_key = ? WHERE config_key = ?",
        (target, source),
    )
