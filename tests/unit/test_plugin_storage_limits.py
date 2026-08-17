"""Manifest storage quota enforcement for plugin-private state."""

from __future__ import annotations

import pytest

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.repository import (
    PluginInstallationRepository,
    PluginStateRepository,
)
from qq_ai_bot.plugin_host.storage import BoundStorageFacade
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission


async def _install(database: Database, plugin_id: str) -> None:
    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id=plugin_id,
        name="Quota test",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=1.6,<2",
        manifest_hash="0" * 64,
        entrypoint="quota_plugin:Plugin",
        requested_permissions=(PluginPermission.STORAGE_PRIVATE,),
    )


@pytest.mark.asyncio
async def test_storage_facade_enforces_utf8_payload_capacity(database: Database) -> None:
    await _install(database, "quota.plugin")
    repository = PluginStateRepository(database)
    storage = BoundStorageFacade(
        repository=repository,
        plugin_id="quota.plugin",
        approved_permissions=(PluginPermission.STORAGE_PRIVATE,),
        storage_mb=1,
    )
    exact_one_megabyte = "x" * (1024 * 1024 - 2)

    await storage.set("cache", "large", exact_one_megabyte)
    assert await repository.storage_usage_bytes(plugin_id="quota.plugin") == 1024 * 1024

    with pytest.raises(PluginPermissionError, match="capacity"):
        await storage.set("cache", "extra", "x")

    # Replacing an existing row subtracts its old payload before applying the limit.
    await storage.set("cache", "large", "smaller")
    await storage.set("cache", "extra", "x")
    assert await storage.get("cache", "extra") == "x"


@pytest.mark.asyncio
async def test_storage_compare_and_set_checks_capacity_before_write(database: Database) -> None:
    await _install(database, "cas-quota.plugin")
    storage = BoundStorageFacade(
        repository=PluginStateRepository(database),
        plugin_id="cas-quota.plugin",
        approved_permissions=(PluginPermission.STORAGE_PRIVATE,),
        storage_mb=1,
    )
    await storage.set("cache", "large", "x" * (1024 * 1024 - 2))

    with pytest.raises(PluginPermissionError, match="capacity"):
        await storage.compare_and_set("cache", "extra", None, "x")
    assert await storage.get("cache", "extra") is None


@pytest.mark.asyncio
async def test_storage_facade_rejects_invalid_capacity(database: Database) -> None:
    with pytest.raises(ValueError, match="storage limit"):
        BoundStorageFacade(
            repository=PluginStateRepository(database),
            plugin_id="quota.plugin",
            approved_permissions=(PluginPermission.STORAGE_PRIVATE,),
            storage_mb=0,
        )
