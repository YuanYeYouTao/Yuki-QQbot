"""Transactional proof for the irreversible 0041 -> 0042 cutover."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from tests.unit.test_migration_0021 import _config

_OLD_ROLLUP_TABLES = {
    "conversation_history_summary_members",
    "conversation_history_rollup_jobs",
    "conversation_history_summaries",
    "conversation_history_states",
}
_NEW_TABLES = {
    "conversation_scopes",
    "conversation_rollups",
    "conversation_rollup_jobs",
}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _seed_valid_private_event(path: Path) -> tuple[object, ...]:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO people(user_id,nickname,enabled,is_bot,first_seen_at,last_seen_at) "
            "VALUES('bot-1','Yuki',1,1,'2026-08-20','2026-08-20')"
        )
        connection.execute(
            "INSERT INTO people(user_id,nickname,enabled,is_bot,first_seen_at,last_seen_at) "
            "VALUES('peer-1','Peer',1,0,'2026-08-20','2026-08-20')"
        )
        connection.execute(
            """
            INSERT INTO chat_events(
                bot_user_id,platform_message_id,scope_type,private_peer_user_id,
                sender_user_id,direction,content,visual_summary,segments_json,
                origin,occurred_at,observed_at
            ) VALUES(
                'bot-1','cutover-event','private','peer-1','peer-1','inbound',
                'permanent content','visual context','[]','user_message',
                '2026-08-20','2026-08-20'
            )
            """
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM chat_events WHERE platform_message_id='cutover-event'"
        ).fetchone()
        assert row is not None
        return tuple(row)


@pytest.mark.parametrize(
    "failpoint",
    ["new_tables", "scope_backfill", "first_old_table_drop"],
)
def test_0042_failpoints_restore_schema_data_and_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
) -> None:
    path = tmp_path / f"rollback-{failpoint}.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0041")
    expected_event = _seed_valid_private_event(path)
    with sqlite3.connect(path) as connection:
        before_tables = _tables(connection)

    monkeypatch.setenv("YUKI_MIGRATION_0042_FAILPOINT", failpoint)
    with pytest.raises(RuntimeError, match="0042 injected failure"):
        command.upgrade(config, "head")
    monkeypatch.delenv("YUKI_MIGRATION_0042_FAILPOINT")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0041",)
        assert _tables(connection) == before_tables
        assert _OLD_ROLLUP_TABLES <= before_tables
        assert not (_NEW_TABLES & before_tables)
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM chat_events WHERE platform_message_id='cutover-event'"
                ).fetchone()
            )
            == expected_event
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0042_backfills_cutover_boundary_without_rewriting_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "valid-cutover.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0041")
    expected_event = _seed_valid_private_event(path)

    command.upgrade(config, "head")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0042",)
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM chat_events WHERE platform_message_id='cutover-event'"
                ).fetchone()
            )
            == expected_event
        )
        event_id = int(expected_event[0])
        assert connection.execute(
            """
            SELECT scope_key,generation,starts_after_event_id,last_event_id,
                   uncovered_event_count,uncovered_character_count
            FROM conversation_scopes
            """
        ).fetchone() == (
            "bot:bot-1:private:peer-1",
            1,
            event_id,
            event_id,
            0,
            0,
        )
        tables = _tables(connection)
        assert _NEW_TABLES <= tables
        assert not (_OLD_ROLLUP_TABLES & tables)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0042_rejects_invalid_foreign_keys_without_partial_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "invalid-ledger.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0041")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO people(user_id,nickname,enabled,is_bot,first_seen_at,last_seen_at) "
            "VALUES('bot-1','Yuki',1,1,'2026-08-20','2026-08-20')"
        )
        connection.execute(
            "INSERT INTO people(user_id,nickname,enabled,is_bot,first_seen_at,last_seen_at) "
            "VALUES('member-1','Member',1,0,'2026-08-20','2026-08-20')"
        )
        connection.execute(
            """
            INSERT INTO chat_events(
                bot_user_id,platform_message_id,scope_type,group_id,sender_user_id,
                direction,content,visual_summary,segments_json,origin,occurred_at,observed_at
            ) VALUES(
                'bot-1','invalid-group-event','group','missing-group','member-1',
                'inbound','invalid','', '[]','user_message','2026-08-20','2026-08-20'
            )
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="foreign_key_check failed"):
        command.upgrade(config, "head")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0041",)
        assert not (_NEW_TABLES & _tables(connection))
        assert connection.execute(
            "SELECT content FROM chat_events WHERE platform_message_id='invalid-group-event'"
        ).fetchone() == ("invalid",)


def test_0042_downgrade_requires_snapshot_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "irreversible.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0041")
    command.upgrade(config, "head")

    with pytest.raises(RuntimeError, match=r"restore the pre-3\.7\.0 database snapshot"):
        command.downgrade(config, "0041")
