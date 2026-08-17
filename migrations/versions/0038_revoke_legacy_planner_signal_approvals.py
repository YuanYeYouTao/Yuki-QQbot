"""Revoke leftover planner.signal.register plugin approvals.

3.6.0-R3: Plugin API 2.0 replaces ``planner.signal.register`` with
``admission.signal.register``.  Existing approval and request arrays that
still name the retired permission are sanitized, and any affected plugin is
returned to ``pending_approval`` so administrators must re-approve the new
contract.  Plugins that never requested or approved the permission are left
unchanged.

Downgrade cannot restore deleted approvals; it is intentionally a no-op.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-17
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op
from sqlalchemy import inspect

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REVOKED_PERMISSION = "planner.signal.register"


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "plugin_installations" in tables:
        _revoke_installation_approvals(connection)
    for table, column in _plugin_json_columns(inspector, tables):
        _strip_json_column(connection, table, column)


def downgrade() -> None:
    """Approvals that named planner.signal.register cannot be reconstructed."""

    return


def _plugin_json_columns(
    inspector: object,
    tables: set[str],
) -> tuple[tuple[str, str], ...]:
    columns: list[tuple[str, str]] = []
    for table in sorted(tables):
        if not table.startswith("plugin_") or table == "plugin_installations":
            continue
        for column in inspector.get_columns(table):  # type: ignore[attr-defined]
            name = str(column["name"])
            if name.endswith("_json") or name.endswith("json"):
                columns.append((table, name))
    return tuple(columns)


def _revoke_installation_approvals(connection: object) -> None:
    rows = connection.exec_driver_sql(  # type: ignore[attr-defined]
        """
        SELECT plugin_id, approved_permissions_json, requested_permissions_json
        FROM plugin_installations
        """
    ).fetchall()
    now = datetime.now(UTC).isoformat(sep=" ")
    for plugin_id, approved_raw, requested_raw in rows:
        approved, approved_found = _strip_permission_document(approved_raw)
        requested, requested_found = _strip_permission_document(requested_raw)
        if not approved_found and not requested_found:
            continue
        connection.exec_driver_sql(  # type: ignore[attr-defined]
            """
            UPDATE plugin_installations
            SET approved_permissions_json = ?,
                requested_permissions_json = ?,
                status = 'pending_approval',
                enabled = 0,
                approved_at = NULL,
                updated_at = ?
            WHERE plugin_id = ?
            """,
            (
                _dump_json(approved),
                _dump_json(requested),
                now,
                plugin_id,
            ),
        )


def _strip_json_column(connection: object, table: str, column: str) -> None:
    rows = connection.exec_driver_sql(  # type: ignore[attr-defined]
        f'SELECT rowid, "{column}" FROM "{table}"'
    ).fetchall()
    for rowid, raw in rows:
        stripped, found = _strip_permission_document(raw)
        if not found:
            continue
        connection.exec_driver_sql(  # type: ignore[attr-defined]
            f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
            (_dump_json(stripped), rowid),
        )


def _strip_permission_document(raw: object) -> tuple[object, bool]:
    if raw is None:
        return [], False
    if not isinstance(raw, str):
        return raw, False
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return raw, False
    return _strip_permission_value(document)


def _strip_permission_value(value: object) -> tuple[object, bool]:
    found = False
    if isinstance(value, list):
        kept: list[object] = []
        for item in value:
            if item == _REVOKED_PERMISSION:
                found = True
                continue
            stripped, nested = _strip_permission_value(item)
            found = found or nested
            kept.append(stripped)
        return kept, found
    if isinstance(value, dict):
        stripped_items: dict[object, object] = {}
        for key, child in value.items():
            stripped, nested = _strip_permission_value(child)
            found = found or nested
            stripped_items[key] = stripped
        return stripped_items, found
    return value, False


def _dump_json(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
