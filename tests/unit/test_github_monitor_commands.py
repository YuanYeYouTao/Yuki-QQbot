from __future__ import annotations

# ruff: noqa: E402,I001 -- the local plugin package is intentionally added before import.

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "github-monitor"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from github_monitor.commands import GitHubCommandArguments, GitHubCommands
from github_monitor.config import GitHubMonitorConfig, load_config
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.people_repository import GroupSettingsRepository, PeopleRepository
from qq_ai_bot.plugin_host.config import BoundConfigFacade
from qq_ai_bot.plugin_host.facades import (
    HostPluginContext,
    PluginFacadeServices,
    PluginInvocation,
)
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.repository import (
    PluginConfigRepository,
    PluginInstallationRepository,
)
from yuki_plugin_sdk.permissions import PluginPermission

PLUGIN_ID = "github-monitor"
ADMIN_ID = "2186567848"
BOT_ID = "380726517"


class ContextHolder:
    context: HostPluginContext


@pytest.mark.asyncio
async def test_add_persists_real_host_grant_and_nested_config(database: Database) -> None:
    installations = PluginInstallationRepository(database)
    permissions = (
        PluginPermission.NOTIFICATION_PUBLISH,
        PluginPermission.PLUGIN_CONFIG_READ,
        PluginPermission.PLUGIN_CONFIG_WRITE,
    )
    await installations.upsert_discovered(
        plugin_id=PLUGIN_ID,
        name="GitHub Monitor",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.4",
        manifest_hash="a" * 64,
        entrypoint="github_monitor:GitHubMonitorPlugin",
        requested_permissions=tuple(item.value for item in permissions),
    )
    await installations.approve(PLUGIN_ID)
    await installations.set_enabled(PLUGIN_ID, enabled=True)
    await installations.set_status(PLUGIN_ID, status="running")
    await PeopleRepository(database).observe(user_id=ADMIN_ID, nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("1049765710", True)
    configs = PluginConfigRepository(database)

    def config_factory(user_id: str | None, group_id: str | None) -> BoundConfigFacade:
        return BoundConfigFacade(
            repository=configs,
            plugin_id=PLUGIN_ID,
            approved_permissions=permissions,
            schema=GitHubMonitorConfig,
            current_user_id=user_id,
            current_group_id=group_id,
        )

    context = HostPluginContext(
        plugin_id=PLUGIN_ID,
        approved_permissions=permissions,
        superuser_ids=(ADMIN_ID,),
        services=PluginFacadeServices(
            notifications=PluginNotificationRepository(database),
            config_factory=config_factory,
        ),
    )
    ContextHolder.context = context
    message = InboundMessage(
        message_id="github-add",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(ADMIN_ID),
        text="/github add YuanYeYouTao/Yuki-QQbot group:1049765710",
        bot_user_id=BOT_ID,
        received_at=datetime.now(UTC),
    )
    trusted = PluginInvocation(
        plugin_id=PLUGIN_ID,
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id=ADMIN_ID,
        bot_user_id=BOT_ID,
        inbound=message,
    )
    commands = GitHubCommands(ContextHolder(), object())

    with context.bind(trusted):
        result = await commands.handle(
            GitHubCommandArguments(text="add YuanYeYouTao/Yuki-QQbot group:1049765710")
        )

    assert result.ok, result
    config = await load_config(context)
    assert config.repositories[0].repository == "YuanYeYouTao/Yuki-QQbot"
    assert config.repositories[0].targets[0].target_id == "1049765710"
    grants = await context.notifications.list_grants()
    assert len(grants) == 1
    assert grants[0].bot_user_id == BOT_ID
