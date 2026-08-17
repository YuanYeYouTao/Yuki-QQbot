"""0038 revokes leftover planner.signal.register plugin approvals."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tests.unit.test_migration_0035 import _downgrade, _migrate

_REVOKED = "planner.signal.register"
_KEPT = "memory.read"


def _insert_installation(
    connection: sqlite3.Connection,
    *,
    plugin_id: str,
    status: str,
    enabled: int,
    approved: list[str],
    requested: list[str],
    approved_at: str | None,
    timestamp: str,
) -> None:
    connection.execute(
        """
        INSERT INTO plugin_installations(
            plugin_id, name, version, plugin_api, yuki_requires, manifest_hash,
            entrypoint, status, enabled, approved_permissions_json,
            requested_permissions_json, failure_count, discovered_at, approved_at,
            updated_at
        ) VALUES (?, ?, '1.0.0', '1.1', '>=3.5.0', 'hash', 'fixture:plugin', ?, ?,
                  ?, ?, 0, ?, ?, ?)
        """,
        (
            plugin_id,
            plugin_id,
            status,
            enabled,
            json.dumps(approved, ensure_ascii=False, separators=(",", ":")),
            json.dumps(requested, ensure_ascii=False, separators=(",", ":")),
            timestamp,
            approved_at,
            timestamp,
        ),
    )


def test_0038_revokes_planner_signal_register_and_leaves_others(tmp_path: Path) -> None:
    path = tmp_path / "0038.db"
    _migrate(path, "0037")
    timestamp = datetime.now(UTC).isoformat(sep=" ")
    with sqlite3.connect(path) as connection:
        _insert_installation(
            connection,
            plugin_id="signal-plugin",
            status="approved",
            enabled=1,
            approved=[_REVOKED, _KEPT],
            requested=[_REVOKED, _KEPT],
            approved_at=timestamp,
            timestamp=timestamp,
        )
        _insert_installation(
            connection,
            plugin_id="other-plugin",
            status="approved",
            enabled=1,
            approved=[_KEPT],
            requested=[_KEPT],
            approved_at=timestamp,
            timestamp=timestamp,
        )
        connection.execute(
            """
            INSERT INTO plugin_state(
                plugin_id, namespace, key, value_json, version, updated_at
            ) VALUES (
                'signal-plugin', 'signals', 'permissions', ?, 1, ?
            )
            """,
            (json.dumps([_REVOKED, _KEPT], separators=(",", ":")), timestamp),
        )
        connection.commit()

    _migrate(path, "0038")
    with sqlite3.connect(path) as connection:
        affected = connection.execute(
            """
            SELECT status, enabled, approved_at, approved_permissions_json,
                   requested_permissions_json
            FROM plugin_installations WHERE plugin_id = 'signal-plugin'
            """
        ).fetchone()
        assert affected is not None
        status, enabled, approved_at, approved_json, requested_json = affected
        assert status == "pending_approval"
        assert not enabled
        assert approved_at is None
        assert json.loads(approved_json) == [_KEPT]
        assert json.loads(requested_json) == [_KEPT]
        assert _REVOKED not in approved_json
        assert _REVOKED not in requested_json

        leftover = connection.execute(
            "SELECT value_json FROM plugin_state WHERE plugin_id = 'signal-plugin'"
        ).fetchone()
        assert leftover is not None
        assert json.loads(leftover[0]) == [_KEPT]

        untouched = connection.execute(
            """
            SELECT status, enabled, approved_at, approved_permissions_json
            FROM plugin_installations WHERE plugin_id = 'other-plugin'
            """
        ).fetchone()
        assert untouched is not None
        assert untouched[0] == "approved"
        assert untouched[1]
        assert untouched[2] == timestamp
        assert json.loads(untouched[3]) == [_KEPT]

    _downgrade(path, "0037")
    with sqlite3.connect(path) as connection:
        restored = connection.execute(
            """
            SELECT status, enabled, approved_at, approved_permissions_json
            FROM plugin_installations WHERE plugin_id = 'signal-plugin'
            """
        ).fetchone()
        assert restored is not None
        assert restored[0] == "pending_approval"
        assert not restored[1]
        assert restored[2] is None
        assert json.loads(restored[3]) == [_KEPT]
        assert _REVOKED not in restored[3]
