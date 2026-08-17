"""Frozen 3.5.3 -> 3.6.0 config mapping (R1 §11)."""

from __future__ import annotations

import pytest

from qq_ai_bot.admin.config_migration_3_6 import (
    CONFLICT_POLICY,
    NEW_RUNTIME_CONFIG_KEYS,
    PLANNER_CONFIG_DELETED_KEYS,
    PLANNER_CONFIG_MIGRATION_MAP,
    ConfigMigrationAction,
    classify_planner_config_key,
)
from qq_ai_bot.admin.config_registry import ConfigRegistry


def test_mapping_matches_the_r1_r5_table() -> None:
    assert dict(PLANNER_CONFIG_MIGRATION_MAP) == {
        "planner.group_enabled": "conversation.autonomous_enabled",
        "planner.group_debounce_seconds": "conversation.autonomous_debounce_seconds",
        "planner.reply_necessity_threshold": "conversation.autonomous_admission_threshold",
        "planner.max_pending_messages": "conversation.autonomous_batch_limit",
        "planner.recent_presence_window_seconds": (
            "conversation.autonomous_presence_window_seconds"
        ),
        "planner.interrupt_autonomous_on_new_message": (
            "conversation.interrupt_autonomous_on_new_message"
        ),
        "reply.plan_hard_max_messages": "reply.hard_max_messages",
        "speech.planner_enabled": "speech.agent_effects_enabled",
    }
    assert CONFLICT_POLICY == "keep_new_key_value_and_delete_old_key"


def test_mapping_is_immutable() -> None:
    with pytest.raises(TypeError):
        PLANNER_CONFIG_MIGRATION_MAP["planner.group_enabled"] = "nope"  # type: ignore[index]


def test_migrated_and_deleted_keys_do_not_overlap() -> None:
    overlap = set(PLANNER_CONFIG_MIGRATION_MAP) & set(PLANNER_CONFIG_DELETED_KEYS)
    assert overlap == set()
    assert len(set(PLANNER_CONFIG_MIGRATION_MAP.values())) == len(PLANNER_CONFIG_MIGRATION_MAP)
    assert tuple(PLANNER_CONFIG_MIGRATION_MAP.values()) == NEW_RUNTIME_CONFIG_KEYS


def test_current_registry_owns_new_keys_and_drops_old_planner_keys() -> None:
    registry = ConfigRegistry()
    for key in (*PLANNER_CONFIG_MIGRATION_MAP, *PLANNER_CONFIG_DELETED_KEYS):
        assert registry.maybe_get(key) is None, key
    for key in NEW_RUNTIME_CONFIG_KEYS:
        assert registry.maybe_get(key) is not None, key
    assert registry.maybe_get("mcp.tool_selection_mode") is None


def test_classify_planner_config_key() -> None:
    assert classify_planner_config_key("planner.group_enabled") is ConfigMigrationAction.MIGRATE
    assert classify_planner_config_key("planner.temperature") is ConfigMigrationAction.DELETE
    assert classify_planner_config_key("reply.cancel_on_new_message") is ConfigMigrationAction.KEEP
