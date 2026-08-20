"""Trusted invocation binding for plugin-backed automation actions."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from qq_ai_bot.automation.authority import (
    AuthorityContext,
    DelegatedAuthority,
    PermissionLevel,
)
from qq_ai_bot.automation.models import AutomationContext, TurnOrigin
from qq_ai_bot.automation.registry import (
    AutomationCapabilityRegistry,
    CapabilityExecutionContext,
)
from qq_ai_bot.persistence.repository_records import GroupSetting
from qq_ai_bot.plugin_host.automation_adapter import PluginAutomationAdapter
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.facades import (
    HostPluginContext,
    PluginFacadeServices,
    PluginInvocation,
)
from qq_ai_bot.plugin_host.manifest import PluginManifest
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import StrictModel
from yuki_plugin_sdk.models import TurnOrigin as SdkTurnOrigin
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import (
    AutomationActionMetadata,
    AutomationActionRegistration,
    ToolMetadata,
    ToolRegistration,
)

PLUGIN_ID = "com.example.scheduled"
ACTION_NAME = f"plugin.{PLUGIN_ID}.inspect_context"


class ActionInput(StrictModel):
    text: str


class ActionOutput(StrictModel):
    text: str
    group_id: str


class FakeGroups:
    async def get(self, group_id: str, **_kwargs: object) -> GroupSetting:
        return GroupSetting(
            group_id=group_id,
            name="Scheduled group",
            enabled=True,
            require_mention=False,
        )


def _manifest(*, version: str = "1.0.0") -> PluginManifest:
    return PluginManifest(
        id=PLUGIN_ID,
        name="Scheduled",
        version=version,
        description="Scheduled action test plugin",
        entrypoint="scheduled_plugin:Plugin",
        plugin_api="2.0",
        yuki_requires=">=1.6.0,<2",
        permissions=(PluginPermission.AUTOMATION_ACTION_REGISTER,),
    )


def _execution_context(
    *,
    actor_user_id: str = "10001",
    creator_user_id: str | None = None,
    bot_user_id: str = "99999",
    current_group_id: str | None = "20002",
    allowed_capabilities: frozenset[str] | None = None,
    web_was_used: bool = False,
) -> CapabilityExecutionContext:
    capabilities = allowed_capabilities or frozenset(
        {ACTION_NAME, PluginPermission.GROUP_CURRENT_READ.value}
    )
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    delegated = DelegatedAuthority(
        creator_user_id=actor_user_id,
        bot_user_id=bot_user_id,
        created_from_message_id="create-message",
        created_at=now.isoformat(),
        permission_level=PermissionLevel.USER,
        granted_capabilities=tuple(sorted(capabilities)),
        capability_schema_versions={name: 1 for name in capabilities},
        current_group_id=current_group_id,
    )
    return CapabilityExecutionContext(
        authority=AuthorityContext(
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
            actor_user_id=actor_user_id,
            actor_is_superuser=True,
            bot_user_id=bot_user_id,
            delegated_authority=delegated,
            allowed_capabilities=capabilities,
        ),
        automation_id=11,
        automation_run_id=12,
        step_id="inspect",
        creator_user_id=creator_user_id or actor_user_id,
        bot_user_id=bot_user_id,
        current_group_id=current_group_id,
        scheduled_for=now,
        actual_started_at=now,
        local_time=now,
        timezone="Asia/Shanghai",
        automation_context=AutomationContext(scene="current_group"),
        conversation_key="automation:11",
        web_was_used=web_was_used,
    )


def _register_action(
    extensions: ExtensionRegistry,
    handler: Any,
) -> None:
    registrar = extensions.registrar(
        PLUGIN_ID,
        (PluginPermission.AUTOMATION_ACTION_REGISTER,),
    )
    registrar.register_automation_action(
        AutomationActionRegistration(
            metadata=AutomationActionMetadata(
                name="inspect_context",
                description="Inspect the scheduled invocation",
            ),
            input_model=ActionInput,
            output_model=ActionOutput,
            handler=handler,
        )
    )


@pytest.mark.asyncio
async def test_single_tool_registration_is_projected_into_scheduled_automation() -> None:
    extensions = ExtensionRegistry()
    automation = AutomationCapabilityRegistry()
    registrar = extensions.registrar(PLUGIN_ID, (PluginPermission.TOOL_REGISTER,))

    async def tool(arguments: object) -> ActionOutput:
        assert isinstance(arguments, ActionInput)
        return ActionOutput(text=arguments.text, group_id="20002")

    registrar.register_tool(
        ToolRegistration(
            metadata=ToolMetadata(
                name="shared_tool",
                description="One implementation for user and scheduled turns",
                allowed_origins=frozenset(
                    {SdkTurnOrigin.USER_MESSAGE, SdkTurnOrigin.SCHEDULED_AUTOMATION}
                ),
            ),
            input_model=ActionInput,
            output_model=ActionOutput,
            handler=tool,
        )
    )
    manifest = PluginManifest(
        id=PLUGIN_ID,
        name="Scheduled",
        version="1.0.0",
        description="Scheduled action test plugin",
        entrypoint="scheduled_plugin:Plugin",
        plugin_api="2.0",
        yuki_requires=">=1.6.0,<2",
        permissions=(PluginPermission.TOOL_REGISTER,),
    )
    adapter = PluginAutomationAdapter(extensions=extensions, automation=automation)

    assert adapter.activate(manifest) == 1
    definition = automation.require(f"plugin.{PLUGIN_ID}.shared_tool")
    assert TurnOrigin.SCHEDULED_AUTOMATION in definition.allowed_origins
    assert definition.provider_plugin_id == PLUGIN_ID


@pytest.mark.asyncio
async def test_action_binds_trusted_scheduled_invocation_and_preserves_provenance() -> None:
    extensions = ExtensionRegistry()
    automation = AutomationCapabilityRegistry()
    captured: list[PluginInvocation] = []
    host = HostPluginContext(
        plugin_id=PLUGIN_ID,
        approved_permissions=(PluginPermission.GROUP_CURRENT_READ,),
        superuser_ids=(),
        services=PluginFacadeServices(groups=cast(Any, FakeGroups())),
    )

    async def action(arguments: object) -> ActionOutput:
        assert isinstance(arguments, ActionInput)
        group = await host.groups.get_current()
        assert group is not None
        return ActionOutput(text=arguments.text, group_id=str(group["group_id"]))

    def invocation_scope(
        plugin_id: str,
        invocation: PluginInvocation,
    ) -> AbstractAsyncContextManager[object]:
        assert plugin_id == PLUGIN_ID
        captured.append(invocation)
        return host.bind(invocation)

    _register_action(extensions, action)
    adapter = PluginAutomationAdapter(
        extensions=extensions,
        automation=automation,
        invocation_scope=invocation_scope,
    )
    manifest = _manifest()
    assert adapter.activate(manifest) == 1
    definition = automation.require(ACTION_NAME)
    assert definition.provider_plugin_id == PLUGIN_ID
    assert definition.provider_version == manifest.version
    assert definition.provider_manifest_hash == manifest.manifest_hash
    assert definition.schema_version == 1
    assert definition.handler is not None
    execution_context = _execution_context(web_was_used=True)

    result = await definition.handler({"text": "hello"}, execution_context)

    assert result.data == {"text": "hello", "group_id": "20002"}
    assert len(captured) == 1
    invocation = captured[0]
    assert invocation.plugin_id == PLUGIN_ID
    assert invocation.origin is TurnOrigin.SCHEDULED_AUTOMATION
    assert invocation.actor_user_id == execution_context.authority.actor_user_id
    assert invocation.bot_user_id == execution_context.authority.bot_user_id
    assert invocation.delegated_authority is execution_context.authority.delegated_authority
    assert invocation.current_group_id == execution_context.current_group_id
    assert invocation.allowed_capabilities == execution_context.authority.allowed_capabilities
    assert invocation.web_was_used is True
    assert not hasattr(invocation, "actor_is_superuser")
    with pytest.raises(PluginPermissionError, match="trusted invocation"):
        await host.groups.get_current()

    assert adapter.deactivate(PLUGIN_ID) == 1
    assert automation.get(ACTION_NAME) is None


@pytest.mark.asyncio
async def test_web_use_does_not_revoke_delegated_plugin_mutation() -> None:
    extensions = ExtensionRegistry()
    automation = AutomationCapabilityRegistry()
    host = HostPluginContext(
        plugin_id=PLUGIN_ID,
        approved_permissions=(PluginPermission.ONEBOT_MUTATE,),
        superuser_ids=("10001",),
    )

    async def action(_arguments: object) -> ActionOutput:
        result = await host.onebot.call_mutating_action("set_group_ban", {})
        assert result.ok is False
        assert result.error_code == "feature.unavailable"
        return ActionOutput(text="allowed", group_id="20002")

    def invocation_scope(
        _plugin_id: str,
        invocation: PluginInvocation,
    ) -> AbstractAsyncContextManager[object]:
        return host.bind(invocation)

    _register_action(extensions, action)
    adapter = PluginAutomationAdapter(
        extensions=extensions,
        automation=automation,
        invocation_scope=invocation_scope,
    )
    adapter.activate(_manifest())
    definition = automation.require(ACTION_NAME)
    assert definition.handler is not None
    context = _execution_context(
        allowed_capabilities=frozenset({ACTION_NAME, PluginPermission.ONEBOT_MUTATE.value}),
        web_was_used=True,
    )

    result = await definition.handler({"text": "hello"}, context)
    assert result.data == {"text": "allowed", "group_id": "20002"}


@pytest.mark.asyncio
async def test_authority_superuser_flag_cannot_elevate_plugin_invocation() -> None:
    extensions = ExtensionRegistry()
    automation = AutomationCapabilityRegistry()
    host = HostPluginContext(
        plugin_id=PLUGIN_ID,
        approved_permissions=(PluginPermission.GROUP_READ,),
        superuser_ids=(),
    )

    async def action(_arguments: object) -> ActionOutput:
        await host.groups.get("30003")
        raise AssertionError("non-superuser must not escape the delegated group")

    def invocation_scope(
        _plugin_id: str,
        invocation: PluginInvocation,
    ) -> AbstractAsyncContextManager[object]:
        return host.bind(invocation)

    _register_action(extensions, action)
    adapter = PluginAutomationAdapter(
        extensions=extensions,
        automation=automation,
        invocation_scope=invocation_scope,
    )
    adapter.activate(_manifest())
    definition = automation.require(ACTION_NAME)
    assert definition.handler is not None
    context = _execution_context(
        allowed_capabilities=frozenset({ACTION_NAME, PluginPermission.GROUP_READ.value})
    )
    assert context.authority.actor_is_superuser is True

    with pytest.raises(PluginPermissionError, match="outside the current real turn"):
        await definition.handler({"text": "hello"}, context)


@pytest.mark.asyncio
async def test_action_fails_closed_without_scope_or_matching_authority() -> None:
    extensions = ExtensionRegistry()
    automation = AutomationCapabilityRegistry()
    called = False

    async def action(_arguments: object) -> ActionOutput:
        nonlocal called
        called = True
        return ActionOutput(text="unexpected", group_id="unexpected")

    _register_action(extensions, action)
    adapter = PluginAutomationAdapter(extensions=extensions, automation=automation)
    adapter.activate(_manifest())
    definition = automation.require(ACTION_NAME)
    assert definition.handler is not None
    with pytest.raises(RuntimeError, match="scope is unavailable"):
        await definition.handler({"text": "hello"}, _execution_context())
    assert called is False

    entered = False

    def invocation_scope(
        _plugin_id: str,
        _invocation: PluginInvocation,
    ) -> AbstractAsyncContextManager[object]:
        nonlocal entered
        entered = True
        raise AssertionError("mismatched authority must be rejected before scope entry")

    guarded_registry = AutomationCapabilityRegistry()
    guarded = PluginAutomationAdapter(
        extensions=extensions,
        automation=guarded_registry,
        invocation_scope=invocation_scope,
    )
    guarded.activate(_manifest())
    guarded_definition = guarded_registry.require(ACTION_NAME)
    assert guarded_definition.handler is not None
    with pytest.raises(RuntimeError, match="creator does not match"):
        await guarded_definition.handler(
            {"text": "hello"},
            _execution_context(creator_user_id="different-user"),
        )
    assert entered is False
    assert called is False
