from __future__ import annotations

import pytest

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.config import BoundConfigFacade
from qq_ai_bot.plugin_host.repository import (
    PluginConfigRepository,
    PluginInstallationRepository,
)
from yuki_plugin_sdk.models import StrictModel
from yuki_plugin_sdk.permissions import PluginPermission

PLUGIN_ID = "test.config.nested"


class Subscription(StrictModel):
    repository: str
    event_types: frozenset[str]


class NestedPluginConfig(StrictModel):
    repositories: tuple[Subscription, ...] = ()


@pytest.mark.asyncio
async def test_config_accepts_nested_models_and_frozensets(database: Database) -> None:
    installations = PluginInstallationRepository(database)
    await installations.upsert_discovered(
        plugin_id=PLUGIN_ID,
        name="Nested config",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.4",
        manifest_hash="a" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("plugin.config.read", "plugin.config.write"),
    )
    facade = BoundConfigFacade(
        repository=PluginConfigRepository(database),
        plugin_id=PLUGIN_ID,
        approved_permissions=(
            PluginPermission.PLUGIN_CONFIG_READ,
            PluginPermission.PLUGIN_CONFIG_WRITE,
        ),
        schema=NestedPluginConfig,
    )

    await facade.set(
        "repositories",
        [
            {
                "repository": "owner/repo",
                "event_types": ["PushEvent", "IssuesEvent"],
            }
        ],
    )

    stored = await facade.get("repositories")
    assert isinstance(stored, list)
    assert stored[0]["repository"] == "owner/repo"
    assert set(stored[0]["event_types"]) == {"PushEvent", "IssuesEvent"}
