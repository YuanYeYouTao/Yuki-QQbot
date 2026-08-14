"""0036 renames runtime attribution overrides without duplicating scopes."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tests.unit.test_migration_0035 import _downgrade, _migrate


def test_0036_renames_override_and_preserves_existing_target(tmp_path: Path) -> None:
    path = tmp_path / "0036.db"
    _migrate(path, "0035")
    now = datetime.now(UTC).isoformat(sep=" ")
    rows = (
        ("memory.usage_reporting_enabled", "global", "", "false"),
        ("memory.usage_reporting_enabled", "user", "1001", "true"),
        ("memory.usage_reporting_enabled", "group", "2001", "false"),
        ("memory.usage_attribution_enabled", "group", "2001", "true"),
    )
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO runtime_config_overrides(
                config_key,scope_type,scope_id,value_json,value_type,apply_mode,
                version,created_at,updated_at,updated_by
            ) VALUES(?,?,?,?,?,'hot',1,?,?,?)
            """,
            [(*row, "boolean", now, now, "test") for row in rows],
        )

    _migrate(path, "0036")
    with sqlite3.connect(path) as connection:
        migrated = connection.execute(
            """
            SELECT config_key,scope_type,scope_id,value_json
            FROM runtime_config_overrides
            ORDER BY scope_type,scope_id
            """
        ).fetchall()
    assert migrated == [
        ("memory.usage_attribution_enabled", "global", "", "false"),
        ("memory.usage_attribution_enabled", "group", "2001", "true"),
        ("memory.usage_attribution_enabled", "user", "1001", "true"),
    ]

    _downgrade(path, "0035")
    with sqlite3.connect(path) as connection:
        downgraded = connection.execute(
            "SELECT DISTINCT config_key FROM runtime_config_overrides"
        ).fetchall()
    assert downgraded == [("memory.usage_reporting_enabled",)]
