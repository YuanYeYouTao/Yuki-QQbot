"""0039 creates reply_effect_events and backfills cadence from planner_runs."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_migration_0035 import _downgrade, _migrate

from qq_ai_bot.conversation.cadence import source_event_hash


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_0039_creates_table_backfills_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "0039.db"
    _migrate(path, "0038")
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO planner_runs(
                conversation_key_hash, trigger_message_id, scope_type, origin,
                sender_user_id_hash, necessity_score, gate_decision,
                planner_decision, voice_intent, voice_mode, created_at
            ) VALUES (
                'conv-hash', 'm1', 'group', 'user_message',
                'sender-hash', 90, 'reply',
                'reply', 'neutral', 'text_and_voice', ?
            )
            """,
            (now,),
        )
        connection.commit()

    _migrate(path, "0039")
    with sqlite3.connect(path) as connection:
        columns = _columns(connection, "reply_effect_events")
        assert {
            "conversation_key_hash",
            "source_event_hash",
            "text_sent",
            "voice_sent",
            "emoji_sent",
            "voice_cadence_eligible",
            "voice_request_basis",
            "source",
        }.issubset(columns)
        row = connection.execute(
            "SELECT text_sent, voice_sent, emoji_sent, voice_cadence_eligible, "
            "voice_request_basis, source, source_event_hash FROM reply_effect_events"
        ).fetchone()
        run_id = connection.execute("SELECT id FROM planner_runs").fetchone()[0]
        assert row[:6] == (1, 1, 0, 1, "agent_initiated", "migrated_planner")
        assert row[6] == source_event_hash(source="migrated_planner", raw=str(run_id))
        indexes = {
            item[1]
            for item in connection.execute(
                "PRAGMA index_list(reply_effect_events)"
            )
        }
        unique_indexes = {
            item[1]
            for item in connection.execute("PRAGMA index_list(reply_effect_events)")
            if item[2]
        }
        assert unique_indexes
        assert "ix_reply_effect_events_conversation_occurred" in indexes

    _migrate(path, "0039")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reply_effect_events").fetchone() == (1,)
        existing_hash = connection.execute(
            "SELECT source_event_hash FROM reply_effect_events"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO reply_effect_events(
                    conversation_key_hash, source_event_hash, text_sent, voice_sent,
                    emoji_sent, voice_cadence_eligible, voice_request_basis, source,
                    occurred_at, recorded_at
                ) VALUES (
                    'other', ?, 0, 0, 0, 1, 'none', 'migrated_planner', ?, ?
                )
                """,
                (existing_hash, now, now),
            )

    _downgrade(path, "0038")
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "reply_effect_events" not in tables
