"""Frozen 3.5.3 -> 3.6.0 runtime-config migration mapping (R1 §11, executed by R5).

R1 freezes this table so every later round codes against one authoritative
mapping; the actual override migration runs as the pre-deployment step
``qq-ai-bot-cli setup migrate-3-6`` delivered in R5.  Do not edit entries
between rounds without a recorded plan deviation.

Conflict policy (R5 §6): overrides migrate per scope with their original
values; when the *new* key already has an override in the same scope, the new
value wins and the old key's override is deleted.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType

#: Old override key -> new override key, migrated per scope with original values.
PLANNER_CONFIG_MIGRATION_MAP: MappingProxyType[str, str] = MappingProxyType(
    {
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
)

#: Planner-exclusive override keys deleted (backed up, never mapped) by R5.
PLANNER_CONFIG_DELETED_KEYS: tuple[str, ...] = (
    "planner.direct_enabled",
    "planner.temperature",
    "planner.max_output_tokens",
    "planner.timeout_seconds",
    "planner.confidence_threshold",
    "planner.max_wait_seconds",
    "planner.preferred_messages",
    "planner.record_runs",
)

#: Target keys created by the mapping.  R4/R5 register them; R1 only freezes names.
NEW_RUNTIME_CONFIG_KEYS: tuple[str, ...] = tuple(PLANNER_CONFIG_MIGRATION_MAP.values())

#: What happens when the same scope already carries an override for the new key.
CONFLICT_POLICY = "keep_new_key_value_and_delete_old_key"


class ConfigMigrationAction(StrEnum):
    """How R5 treats one 3.5.3 override key."""

    MIGRATE = "migrate"
    DELETE = "delete"
    KEEP = "keep"


def classify_planner_config_key(key: str) -> ConfigMigrationAction:
    """Classify one override key against the frozen 3.6.0 mapping."""

    if key in PLANNER_CONFIG_MIGRATION_MAP:
        return ConfigMigrationAction.MIGRATE
    if key in PLANNER_CONFIG_DELETED_KEYS:
        return ConfigMigrationAction.DELETE
    return ConfigMigrationAction.KEEP
