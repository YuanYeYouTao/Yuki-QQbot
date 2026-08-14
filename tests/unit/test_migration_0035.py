"""0035 preserves injection timestamps and FTS while backfilling Activation."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).parents[2]


def _migrate(path: Path, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path.as_posix()}"
    try:
        command.upgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _downgrade(path: Path, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path.as_posix()}"
    try:
        command.downgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_0035_backfill_rename_fts_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "0035.db"
    _migrate(path, "0034")
    timestamp = datetime(2026, 8, 1, tzinfo=UTC).isoformat(sep=" ")
    rows = (
        ("explicit", "fact", 3, "self_report", 0.95),
        ("automatic", "preference", 3, "self_report", 0.80),
        ("automatic", "fact", 3, "self_report", 0.70),
        ("automatic", "episode", 3, "self_report", 0.65),
        ("automatic", "episode", 4, "self_report", 0.75),
    )
    with sqlite3.connect(path) as connection:
        for index, (source, kind, importance, authority, _expected) in enumerate(rows, start=1):
            connection.execute(
                """
                INSERT INTO memory_facts(
                    scope_type,subject_user_id,group_id,visibility_type,visibility_user_id,
                    visibility_group_id,kind,memory_key,category,content,normalized_content,
                    importance,confidence,source_type,authority,status,conflict_state,
                    supersedes_id,valid_from,valid_until,created_at,updated_at,last_confirmed_at,
                    invalidated_reason,last_used_at,validation_version,last_audited_at,review_state
                ) VALUES(
                    'person','1001',NULL,NULL,NULL,NULL,?,?,?,?,?,?,?,?,?,'active','clear',
                    NULL,NULL,NULL,?,?,?,NULL,?,'memory-v2-quality-v1',NULL,'verified'
                )
                """,
                (
                    kind,
                    f"key-{index}",
                    "quality",
                    f"content-{index}",
                    f"content-{index}",
                    importance,
                    0.9,
                    source,
                    authority,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp if index != 5 else None,
                ),
            )
        connection.commit()

    _migrate(path, "0035")
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_facts)")}
        assert "last_injected_at" in columns
        assert "last_used_at" not in columns
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_facts WHERE last_injected_at IS NOT NULL"
        ).fetchone() == (4,)
        activations = connection.execute(
            "SELECT fact_id, activation, activation_updated_at, last_recalled_at, recall_count "
            "FROM memory_activation_states ORDER BY fact_id"
        ).fetchall()
        assert [row[1] for row in activations] == [row[4] for row in rows]
        assert all(row[2] == timestamp for row in activations)
        assert all(row[3] is None and row[4] == 0 for row in activations)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_facts_fts WHERE memory_facts_fts MATCH 'content'"
        ).fetchone() == (5,)

    _downgrade(path, "0034")
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_facts)")}
        assert "last_used_at" in columns
        assert "last_injected_at" not in columns
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_facts WHERE last_used_at IS NOT NULL"
        ).fetchone() == (4,)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "memory_activation_states" not in tables
        assert "memory_recall_receipts" not in tables
        assert "memory_recall_items" not in tables
