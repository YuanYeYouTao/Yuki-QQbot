"""Migration and repository contracts for Planner and Plugin API v1 storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.repository import (
    PluginConfigRepository,
    PluginInstallationRepository,
    PluginStateRepository,
    PluginVersionConflictError,
)
from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRepository

_NEW_TABLES = {
    "planner_runs",
    "plugin_installations",
    "plugin_config_values",
    "plugin_state",
    "plugin_audit_events",
    "plugin_agent_sessions",
    "plugin_agent_messages",
}
_EMOJI_TABLES = {
    "emoji_assets",
    "emoji_scope_states",
    "emoji_jobs",
    "emoji_usage_events",
}
_TOOL_KERNEL_TABLES = {
    "mcp_server_states",
    "mcp_tool_cache",
    "tool_artifacts",
    "tool_invocations",
}


def _alembic_config(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", database_url)
    return Config("alembic.ini")


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }


def test_0005_does_not_create_0013_tables_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "at-0005.db"
    config = _alembic_config(f"sqlite+aiosqlite:///{path.as_posix()}", monkeypatch)

    command.upgrade(config, "0005")

    assert not ((_NEW_TABLES | _EMOJI_TABLES) & _tables(path))


def test_0013_non_destructively_upgrades_0012(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "upgrade-0013.db"
    config = _alembic_config(f"sqlite+aiosqlite:///{path.as_posix()}", monkeypatch)
    command.upgrade(config, "0012")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO people (
                user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("10001", "保留用户", 1, 0, "2026-07-28", "2026-07-28"),
        )
        connection.commit()

    command.upgrade(config, "0013")

    with sqlite3.connect(path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        person = connection.execute(
            "SELECT nickname FROM people WHERE user_id = '10001'"
        ).fetchone()
    assert revision == ("0013",)
    assert person == ("保留用户",)
    assert _NEW_TABLES <= _tables(path)

    command.downgrade(config, "0012")
    with sqlite3.connect(path) as connection:
        downgraded_revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        retained_person = connection.execute(
            "SELECT nickname FROM people WHERE user_id = '10001'"
        ).fetchone()
    assert downgraded_revision == ("0012",)
    assert retained_person == ("保留用户",)
    assert not (_NEW_TABLES & _tables(path))


async def _install(repository: PluginInstallationRepository, plugin_id: str) -> None:
    await repository.upsert_discovered(
        plugin_id=plugin_id,
        name="Test",
        version="0.1.0",
        plugin_api="1.0",
        yuki_requires=">=1.6.0,<2.0",
        manifest_hash=(plugin_id.encode().hex() + "0" * 64)[:64],
        entrypoint="plugin:TestPlugin",
        requested_permissions=("storage.private", "agent.run"),
    )


async def test_plugin_config_and_state_use_real_cas(database: Database) -> None:
    installations = PluginInstallationRepository(database)
    await _install(installations, "com.example.cas")
    configs = PluginConfigRepository(database)
    state = PluginStateRepository(database)

    created = await configs.compare_and_set(
        plugin_id="com.example.cas",
        scope_type="global",
        scope_id="",
        key="difficulty",
        expected_version=0,
        value={"level": 3},
    )
    assert created.version == 1
    assert created.value == {"level": 3}
    with pytest.raises(PluginVersionConflictError):
        await configs.compare_and_set(
            plugin_id="com.example.cas",
            scope_type="global",
            scope_id="",
            key="difficulty",
            expected_version=0,
            value={"level": 4},
        )
    updated = await configs.compare_and_set(
        plugin_id="com.example.cas",
        scope_type="global",
        scope_id="",
        key="difficulty",
        expected_version=1,
        value={"level": 4},
    )
    assert updated.version == 2

    state_created = await state.compare_and_set(
        plugin_id="com.example.cas",
        namespace="campaign",
        key="chapter",
        expected_version=0,
        value={"number": 1},
    )
    assert state_created.version == 1
    with pytest.raises(PluginVersionConflictError):
        await state.compare_and_set(
            plugin_id="com.example.cas",
            namespace="campaign",
            key="chapter",
            expected_version=9,
            value={"number": 2},
        )


async def test_manifest_change_revokes_plugin_approval(database: Database) -> None:
    repository = PluginInstallationRepository(database)
    await _install(repository, "com.example.approval")
    approved = await repository.approve("com.example.approval")
    assert approved is not None
    await repository.set_enabled("com.example.approval", enabled=True)

    changed = await repository.upsert_discovered(
        plugin_id="com.example.approval",
        name="Test",
        version="0.2.0",
        plugin_api="1.0",
        yuki_requires=">=1.6.0,<2.0",
        manifest_hash="f" * 64,
        entrypoint="plugin:TestPlugin",
        requested_permissions=("storage.private", "agent.run", "web.search"),
    )

    assert changed.status == "pending_approval"
    assert changed.enabled is False
    assert changed.approved_permissions == ()
    assert changed.approved_at is None


async def test_plugin_agent_sessions_are_isolated_from_other_plugins(
    database: Database,
) -> None:
    installations = PluginInstallationRepository(database)
    await _install(installations, "com.example.rpg")
    await _install(installations, "com.example.other")
    sessions = PluginAgentSessionRepository(database)
    campaign = await sessions.create(
        plugin_id="com.example.rpg",
        owner_user_id="123456789",
        scope_type="group",
        scope_id="987654321",
        name="周末跑团",
        model="fake-model",
    )
    first = await sessions.append_message(
        plugin_id="com.example.rpg",
        session_id=campaign.session_id,
        role="user",
        sender_user_id="123456789",
        content="我要调查房间。",
        metadata={"reasoning_content": "不得持久化", "dice": 72},
    )
    second = await sessions.append_message(
        plugin_id="com.example.rpg",
        session_id=campaign.session_id,
        role="assistant",
        content="你发现了一扇暗门。",
    )

    assert (first.sequence, second.sequence) == (1, 2)
    assert first.metadata == {"dice": 72, "reasoning_content": "[REDACTED]"}
    assert await sessions.get(plugin_id="com.example.other", session_id=campaign.session_id) is None
    assert (
        await sessions.list_messages(plugin_id="com.example.other", session_id=campaign.session_id)
        == ()
    )
    transcript = await sessions.list_messages(
        plugin_id="com.example.rpg", session_id=campaign.session_id
    )
    assert [message.content for message in transcript] == [
        "我要调查房间。",
        "你发现了一扇暗门。",
    ]
