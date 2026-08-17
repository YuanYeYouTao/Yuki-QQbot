"""0037 adds nullable runtime turn correlation and the observations table."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.unit.test_migration_0035 import _downgrade, _migrate

_CORRELATED_TABLES = (
    "planner_runs",
    "model_invocations",
    "tool_invocations",
    "memory_recall_receipts",
)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_0037_adds_nullable_correlation_and_observations_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "0037.db"
    _migrate(path, "0036")
    now = datetime.now(UTC).isoformat(sep=" ")
    with sqlite3.connect(path) as connection:
        # Pre-0037 rows must survive the upgrade with NULL correlation.
        connection.execute(
            """
            INSERT INTO memory_recall_receipts(
                turn_id,conversation_hash,trigger_hash,origin,mode,purpose,
                candidate_count,selected_count,injected_count,used_count,
                reinforced_count,created_at,updated_at,expires_at
            ) VALUES('receipt-1','h1','h2','user_message','lexical','recall',
                     0,0,0,0,0,?,?,?)
            """,
            (now, now, now),
        )

    _migrate(path, "0037")
    with sqlite3.connect(path) as connection:
        for table in _CORRELATED_TABLES:
            assert "runtime_turn_id" in _columns(connection, table), table
        legacy = connection.execute(
            "SELECT turn_id, runtime_turn_id FROM memory_recall_receipts"
        ).fetchone()
        assert legacy == ("receipt-1", None)
        expires = (datetime.now(UTC) + timedelta(days=30)).isoformat(sep=" ")
        connection.execute(
            """
            INSERT INTO runtime_turn_observations(
                runtime_turn_id,origin,scope_type,conversation_key_hash,
                admission_outcome,handled,sent_messages,error_category,
                total_latency_ms,created_at,expires_at
            ) VALUES('turn-abc','user_message','private',NULL,'chat',1,1,NULL,120,?,?)
            """,
            (now, expires),
        )
        stored = connection.execute(
            "SELECT runtime_turn_id, handled, sent_messages FROM runtime_turn_observations"
        ).fetchone()
        assert stored == ("turn-abc", 1, 1)

    _downgrade(path, "0036")
    with sqlite3.connect(path) as connection:
        for table in _CORRELATED_TABLES:
            assert "runtime_turn_id" not in _columns(connection, table), table
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "runtime_turn_observations" not in tables
        survivor = connection.execute("SELECT turn_id FROM memory_recall_receipts").fetchone()
        assert survivor == ("receipt-1",)
