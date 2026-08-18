"""0041 durable conversation history rollup schema."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_migration_0035 import _downgrade, _migrate

_NOW = datetime(2026, 8, 19, tzinfo=UTC).isoformat(sep=" ")
_SUMMARY_COLUMNS = (
    "state_id, level, status, start_event_id, end_event_id, start_occurred_at, "
    "end_occurred_at, source_event_count, source_character_count, "
    "output_character_count, structured_payload_json, rendered_text, mode, "
    "trust, summarizer_version, source_fingerprint, replaced_by_summary_id, "
    "created_at, updated_at"
)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _create_state(connection: sqlite3.Connection) -> int:
    connection.execute(
        """
        INSERT INTO conversation_history_states(
            bot_user_id, scope_type, private_peer_user_id, group_id, reset_at,
            last_seen_event_id, active_frontier_end_event_id, pending_event_count,
            pending_character_count, revision, created_at, updated_at
        ) VALUES ('bot-1', 'private', '1001', NULL, NULL, 0, 0, 0, 0, 0, ?, ?)
        """,
        (_NOW, _NOW),
    )
    row = connection.execute("SELECT last_insert_rowid()").fetchone()
    assert row is not None
    return int(row[0])


def _insert_summary(
    connection: sqlite3.Connection,
    *,
    state_id: int,
    status: str,
    mode: str,
    level: int,
    fingerprint: str,
    version: str,
    replaced_by: int | None = None,
    start_id: int = 1,
    end_id: int = 8,
) -> int:
    trust = "extractive_compact" if mode == "extractive" else "model_summary"
    connection.execute(
        f"""
        INSERT INTO conversation_history_summaries({_SUMMARY_COLUMNS})
        VALUES (?, ?, ?, ?, ?, ?, ?, 8, 400, 120, '{{}}', 'body', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            state_id,
            level,
            status,
            start_id,
            end_id,
            _NOW,
            _NOW,
            mode,
            trust,
            version,
            fingerprint,
            replaced_by,
            _NOW,
            _NOW,
        ),
    )
    row = connection.execute("SELECT last_insert_rowid()").fetchone()
    assert row is not None
    return int(row[0])


def test_0041_creates_rollup_tables_indexes_and_constraints(tmp_path: Path) -> None:
    path = tmp_path / "0041.db"
    _migrate(path, "0040")
    _migrate(path, "0041")
    _migrate(path, "head")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        tables = _tables(connection)
        assert "conversation_history_states" in tables
        assert "conversation_history_summaries" in tables
        assert "conversation_history_summary_members" in tables
        assert "conversation_history_rollup_jobs" in tables
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "ix_chat_events_bot_scope_private_id" in indexes
        assert "ix_chat_events_bot_scope_group_id" in indexes
        assert "uq_conversation_history_summaries_active_fingerprint" in indexes
        jobs_sql = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type='table' AND name='conversation_history_rollup_jobs'
            """
        ).fetchone()
        assert jobs_sql is not None and jobs_sql[0] is not None
        assert "source_fingerprint" in jobs_sql[0]
        assert "summarizer_version" in jobs_sql[0]
        assert "uq_conversation_history_rollup_jobs_idempotent" in jobs_sql[0]
        ddl = " ".join(
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name LIKE "
                "'conversation_history_%'"
            )
            if row[0]
        )
        assert "memory_facts" not in ddl
        state_id = _create_state(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO conversation_history_states(
                    bot_user_id, scope_type, private_peer_user_id, group_id,
                    reset_at, last_seen_event_id, active_frontier_end_event_id,
                    pending_event_count, pending_character_count, revision,
                    created_at, updated_at
                ) VALUES ('bot-1', 'private', '1001', '2001', NULL, 0, 0, 0, 0, 0, ?, ?)
                """,
                (_NOW, _NOW),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO conversation_history_states(
                    bot_user_id, scope_type, private_peer_user_id, group_id,
                    reset_at, last_seen_event_id, active_frontier_end_event_id,
                    pending_event_count, pending_character_count, revision,
                    created_at, updated_at
                ) VALUES ('bot-1', 'group', NULL, NULL, NULL, 0, 0, 0, 0, 0, ?, ?)
                """,
                (_NOW, _NOW),
            )
        extractive_id = _insert_summary(
            connection,
            state_id=state_id,
            status="active",
            mode="extractive",
            level=0,
            fingerprint="abc",
            version="extractive-v1",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_summary(
                connection,
                state_id=state_id,
                status="active",
                mode="model_summary",
                level=0,
                fingerprint="abc",
                version="flash-v1",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_summary(
                connection,
                state_id=state_id,
                status="active",
                mode="extractive",
                level=1,
                fingerprint="def",
                version="extractive-v1",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_summary(
                connection,
                state_id=state_id,
                status="rolled_up",
                mode="extractive",
                level=0,
                fingerprint="ghi",
                version="extractive-v1",
            )
        replacement_id = _insert_summary(
            connection,
            state_id=state_id,
            status="invalidated",
            mode="model_summary",
            level=0,
            fingerprint="placeholder",
            version="flash-v1",
            start_id=90,
            end_id=91,
        )
        connection.execute(
            """
            UPDATE conversation_history_summaries
            SET status='rolled_up', replaced_by_summary_id=?
            WHERE id=?
            """,
            (replacement_id, extractive_id),
        )
        _insert_summary(
            connection,
            state_id=state_id,
            status="active",
            mode="model_summary",
            level=0,
            fingerprint="abc",
            version="flash-v1",
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO conversation_history_summary_members(
                    summary_id, member_type, source_event_id, source_summary_id,
                    ordinal, created_at
                ) VALUES (?, 'event', NULL, NULL, 0, ?)
                """,
                (extractive_id, _NOW),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO conversation_history_summary_members(
                    summary_id, member_type, source_event_id, source_summary_id,
                    ordinal, created_at
                ) VALUES (?, 'event', 1, 1, 0, ?)
                """,
                (extractive_id, _NOW),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0041_downgrade_drops_rollup_tables_only(tmp_path: Path) -> None:
    path = tmp_path / "0041-down.db"
    _migrate(path, "0041")
    _downgrade(path, "0040")
    with sqlite3.connect(path) as connection:
        tables = _tables(connection)
        assert "conversation_history_states" not in tables
        assert "conversation_history_summaries" not in tables
        assert "chat_events" in tables
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "ix_chat_events_bot_scope_private_id" not in indexes
