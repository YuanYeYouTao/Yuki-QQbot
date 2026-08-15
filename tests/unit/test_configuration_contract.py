"""Configuration documentation must stay synchronized with typed settings."""

from __future__ import annotations

import re
from pathlib import Path

from qq_ai_bot.config import Settings

_EXTERNAL_ENVIRONMENT_KEYS = {
    "MCD_MCP_TOKEN",
    "MINIFLUX_MCP_TOKEN",
    # Explicit offline quality experiments are read only by the administrative CLI.
    "MEMORY_QUALITY_REAL_EMBEDDING_ENABLED",
    "MEMORY_QUALITY_REAL_MODEL_ENABLED",
    "NAPCAT_ACCOUNT",
    "NAPCAT_GID",
    "NAPCAT_IMAGE",
    "NAPCAT_UID",
    "NAPCAT_WEBUI_TOKEN",
    "SPEECH_WORKER_IDLE_RECYCLE_SECONDS",
    # Compose selects immutable Yuki image tags before application settings load.
    "YUKI_VERSION",
}
_COMPOSE_MANAGED_SETTINGS = {
    "APP_HOST",
    "APP_PORT",
    "DATABASE_URL",
}


def test_env_example_matches_typed_settings_and_reviewed_compose_keys() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")
    documented = {
        match.group(1)
        for line in text.splitlines()
        if (match := re.match(r"#?\s*([A-Z][A-Z0-9_]*)=", line)) is not None
    }
    settings_keys = {
        field.validation_alias if isinstance(field.validation_alias, str) else name.upper()
        for name, field in Settings.model_fields.items()
    }

    assert documented - settings_keys == _EXTERNAL_ENVIRONMENT_KEYS
    assert settings_keys - documented == _COMPOSE_MANAGED_SETTINGS
