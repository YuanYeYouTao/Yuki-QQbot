"""0040 drops planner_runs and migrates leftover Planner overrides."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.util.exc import CommandError
from tests.unit.test_migration_0035 import _downgrade, _migrate


def _override_row(
    key: str,
    scope_type: str,
    scope_id: str,
    value: str,
    now: str,
) -> tuple[str, str, str, str, str, str, str, str]:
    return (key, scope_type, scope_id, value, "integer", now, now, "test")


def test_0040_drops_planner_runs_migrates_overrides_and_refuses_downgrade(
    tmp_path: Path,
) -> None:
    path = tmp_path / "0040.db"
    _migrate(path, "0039")
    now = datetime.now(UTC).isoformat(sep=" ")
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
                'reply', 'neutral', 'text', ?
            )
            """,
            (now,),
        )
        connection.executemany(
            """
            INSERT INTO runtime_config_overrides(
                config_key,scope_type,scope_id,value_json,value_type,apply_mode,
                version,created_at,updated_at,updated_by
            ) VALUES(?,?,?,?,?,'hot',1,?,?,?)
            """,
            [
                _override_row("planner.max_pending_messages", "global", "", "12", now),
                _override_row("planner.max_pending_messages", "group", "2001", "4", now),
                _override_row("conversation.autonomous_batch_limit", "group", "2001", "9", now),
                _override_row("planner.temperature", "global", "", "0", now),
                _override_row("mcp.tool_selection_mode", "global", "", "1", now),
                _override_row("reply.plan_hard_max_messages", "global", "", "6", now),
                _override_row("speech.planner_enabled", "global", "", "1", now),
            ],
        )
        connection.commit()

    _migrate(path, "0040")
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "planner_runs" not in tables
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "ix_planner_runs_finished" not in indexes
        assert "ix_planner_runs_conversation_created" not in indexes
        assert "ix_planner_runs_created" not in indexes
        migrated = connection.execute(
            """
            SELECT config_key, scope_type, scope_id, value_json
            FROM runtime_config_overrides
            ORDER BY config_key, scope_type, scope_id
            """
        ).fetchall()

    assert migrated == [
        ("conversation.autonomous_batch_limit", "global", "", "12"),
        ("conversation.autonomous_batch_limit", "group", "2001", "9"),
        ("reply.hard_max_messages", "global", "", "6"),
        ("speech.agent_effects_enabled", "global", "", "1"),
    ]

    with pytest.raises((RuntimeError, CommandError), match="cannot restore deleted planner_runs"):
        _downgrade(path, "0039")
