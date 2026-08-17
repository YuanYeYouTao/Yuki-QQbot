"""Destructive 3.5.3 -> 3.6.0 upgrade from a constructed deployment fixture."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.util.exc import CommandError
from tests.unit.test_migration_0035 import _downgrade, _migrate
from tests.unit.test_model_profile_migrate_3_6 import _load, _v2_document
from tests.unit.test_versioned_docker_release import ROOT

from qq_ai_bot.deployment_setup.migrate_3_6 import migrate_deployment_3_6
from qq_ai_bot.deployment_setup.service import SetupPaths
from qq_ai_bot.model_runtime.profiles import (
    MIGRATE_3_6_COMMAND,
    PROFILE_SCHEMA_VERSION,
    ModelRuntimeConfigurationError,
)

_SECRET_FACT = "person-1001-likes-green-tea"
_PLUGIN_ID = "com.example.legacy-signal"


def _write_legacy_env(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "PLANNER_GROUP_ENABLED=true",
                "PLANNER_GROUP_DEBOUNCE_SECONDS=3",
                "PLANNER_REPLY_NECESSITY_THRESHOLD=0",
                "PLANNER_MAX_PENDING_MESSAGES=8",
                "PLANNER_TEMPERATURE=0.1",
                "PLANNER_DIRECT_ENABLED=true",
                "REPLY_PLAN_HARD_MAX_MESSAGES=10",
                "SPEECH_PLANNER_ENABLED=true",
                "MCP_TOOL_SELECTION_MODE=hybrid",
                "LLM_MODEL=pro-model",
                "",
            )
        ),
        encoding="utf-8",
    )


def _seed_0036_shaped_db(path: Path) -> None:
    _migrate(path, "0036")
    now = datetime.now(UTC).isoformat(sep=" ")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO memory_facts(
                scope_type,subject_user_id,group_id,visibility_type,visibility_user_id,
                visibility_group_id,kind,memory_key,category,content,normalized_content,
                importance,confidence,source_type,authority,status,conflict_state,
                supersedes_id,valid_from,valid_until,created_at,updated_at,last_confirmed_at,
                invalidated_reason,last_injected_at,validation_version,last_audited_at,review_state
            ) VALUES(
                'person','1001',NULL,NULL,NULL,NULL,'fact','tea','quality',?,?,3,0.9,
                'explicit','self_report','active','clear',
                NULL,NULL,NULL,?,?,?,NULL,?,'memory-v2-quality-v1',NULL,'verified'
            )
            """,
            (_SECRET_FACT, _SECRET_FACT, now, now, now, now),
        )
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
        connection.execute(
            """
            INSERT INTO model_invocations (
                task, profile_id, provider, model, success,
                prompt_tokens, completion_tokens, total_tokens,
                cached_prompt_tokens, latency_seconds, error_category, created_at
            ) VALUES (
                'planner', 'flash', 'fake', 'old-planner', 1,
                20, 8, 28, NULL, 0.12, NULL, ?
            )
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO runtime_config_overrides(
                config_key,scope_type,scope_id,value_json,value_type,apply_mode,
                version,created_at,updated_at,updated_by
            ) VALUES(
                'planner.max_pending_messages','global','','12','integer','hot',
                1,?,?, 'test'
            )
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO plugin_installations(
                plugin_id, name, version, plugin_api, yuki_requires, manifest_hash,
                entrypoint, status, enabled, approved_permissions_json,
                requested_permissions_json, failure_count, discovered_at, approved_at,
                updated_at
            ) VALUES (
                ?, ?, '1.0.0', '1.1', '>=3.5.0', 'hash', 'fixture:plugin',
                'approved', 1, ?, ?, 0, ?, ?, ?
            )
            """,
            (
                _PLUGIN_ID,
                _PLUGIN_ID,
                json.dumps(["planner.signal.register", "memory.read"]),
                json.dumps(["planner.signal.register", "memory.read"]),
                now,
                now,
                now,
            ),
        )
        connection.commit()


def test_constructed_3_5_3_deployment_upgrades_to_0040_without_losing_memory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deploy"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    db = root / "data/qq_ai_bot.db"
    _write_legacy_env(root / ".env")
    (root / "config/model_profiles.toml").write_text(_v2_document(), encoding="utf-8")
    (root / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    _seed_0036_shaped_db(db)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0036",)
        seeded_tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "planner_runs" in seeded_tables
        assert "runtime_turn_observations" not in seeded_tables
        assert "reply_effect_events" not in seeded_tables

    with pytest.raises(ModelRuntimeConfigurationError, match=MIGRATE_3_6_COMMAND):
        _load(root / "config/model_profiles.toml")

    result = migrate_deployment_3_6(
        SetupPaths(root),
        baseline_output=tmp_path / "baseline-v1.json",
        repo_root=tmp_path / "not-a-git-repo",
    )
    assert result.baseline.skipped is not None
    assert result.baseline.skipped.startswith("pre_0037_correlation:")
    env = (root / ".env").read_text(encoding="utf-8")
    assert "PLANNER_GROUP_ENABLED" not in env
    assert "CONVERSATION_AUTONOMOUS_ENABLED=true" in env
    assert "CONVERSATION_AUTONOMOUS_BATCH_LIMIT=8" in env
    assert "REPLY_HARD_MAX_MESSAGES=10" in env
    assert "SPEECH_AGENT_EFFECTS_ENABLED=true" in env
    assert "PLANNER_TEMPERATURE" not in env
    assert "MCP_TOOL_SELECTION_MODE" not in env
    catalog = _load(root / "config/model_profiles.toml")
    assert catalog.routes
    document = (root / "config/model_profiles.toml").read_text(encoding="utf-8")
    assert f"schema_version = {PROFILE_SCHEMA_VERSION}" in document
    assert "planner =" not in document
    assert "tool_selection =" not in document

    _migrate(db, "head")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0040",)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "planner_runs" not in tables
        assert "runtime_turn_observations" in tables
        assert "reply_effect_events" in tables
        assert connection.execute("SELECT content FROM memory_facts").fetchone() == (_SECRET_FACT,)
        assert connection.execute("SELECT COUNT(*) FROM memory_facts").fetchone() == (1,)
        assert connection.execute("SELECT task FROM model_invocations").fetchone() == ("planner",)
        overrides = list(
            connection.execute("SELECT config_key, value_json FROM runtime_config_overrides")
        )
        assert overrides == [("conversation.autonomous_batch_limit", "12")]
        plugin = connection.execute(
            """
            SELECT status, enabled, approved_permissions_json, requested_permissions_json
            FROM plugin_installations WHERE plugin_id = ?
            """,
            (_PLUGIN_ID,),
        ).fetchone()
        assert plugin[0] == "pending_approval"
        assert plugin[1] == 0
        assert "planner.signal.register" not in plugin[2]
        assert "memory.read" in plugin[2]
        cadence = connection.execute("SELECT source FROM reply_effect_events").fetchone()
        assert cadence == ("migrated_planner",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    with pytest.raises((RuntimeError, CommandError), match="cannot restore deleted planner_runs"):
        _downgrade(db, "0039")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT content FROM memory_facts").fetchone() == (_SECRET_FACT,)


def test_installers_abort_before_migrate_when_snapshot_fails() -> None:
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    for installer in (shell, powershell):
        lowered = installer.casefold()
        assert "unable to snapshot" in lowered
        assert "verification failed" in lowered
        assert lowered.index("unable to snapshot") < installer.index("migrate-3-6")
        assert lowered.index("verification failed") < installer.index("migrate-3-6")
        assert installer.index("integrity_check") < installer.index("migrate-3-6")
