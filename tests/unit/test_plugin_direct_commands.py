"""Host-owned direct plugin bindings keep the normal command safety chain."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryJobModel
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.plugin_host.command_adapter import PluginCommandAdapter
from qq_ai_bot.plugin_host.direct_command_router import (
    DirectCommandMatch,
    DirectCommandRouter,
)
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.command_service import CommandExecution, CommandService
from yuki_plugin_sdk.models import PermissionLevel, StrictModel
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import (
    CommandMetadata,
    CommandRegistration,
)
from yuki_plugin_sdk.results import CommandResult

PLUGIN_ID = "io.github.yuanyeyoutao.kun-game"
TARGET = f"{PLUGIN_ID}:play"


class TextArguments(StrictModel):
    text: str = ""


@dataclass(slots=True)
class RunningManager:
    running_plugin_ids: tuple[str, ...]


def _register_command(
    registry: ExtensionRegistry,
    *,
    permission: PermissionLevel = PermissionLevel.USER,
    handler: Callable[[BaseModel], Awaitable[CommandResult]] | None = None,
    timeout_seconds: float = 30,
) -> None:
    async def default_handler(arguments: BaseModel) -> CommandResult:
        return CommandResult(text=str(arguments))

    registry.registrar(PLUGIN_ID, (PluginPermission.COMMAND_REGISTER,)).register_command(
        CommandRegistration(
            metadata=CommandMetadata(
                name="play",
                description="Play the Kun game",
                permission=permission,
                timeout_seconds=timeout_seconds,
            ),
            argument_model=TextArguments,
            handler=handler or default_handler,
        )
    )


def test_settings_parse_and_validate_static_direct_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLUGIN_DIRECT_COMMAND_BINDINGS", f'{{"*":"{TARGET}"}}')
    settings = Settings(_env_file=None)

    assert settings.plugins.plugin_direct_command_bindings == {"*": TARGET}


def test_settings_accepts_slash_direct_binding() -> None:
    settings = Settings(
        _env_file=None,
        plugin_direct_command_bindings={"/github": TARGET},
    )

    assert settings.plugins.plugin_direct_command_bindings == {"/github": TARGET}


@pytest.mark.parametrize(
    "bindings",
    [
        {"": TARGET},
        {" *": TARGET},
        {"*\n": TARGET},
        {"*": "not-a-target"},
        {"*": "bad-:play"},
        {"*": TARGET, "**": TARGET},
    ],
)
def test_settings_reject_unsafe_direct_bindings(bindings: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="PLUGIN_DIRECT_COMMAND_BINDINGS"):
        Settings(_env_file=None, plugin_direct_command_bindings=bindings)


def test_settings_reject_direct_binding_that_overlaps_ai_prefix() -> None:
    with pytest.raises(ValidationError, match="AI_PREFIX"):
        Settings(
            _env_file=None,
            ai_prefix="!ai",
            plugin_direct_command_bindings={"!": TARGET},
        )


def test_router_resolves_running_commands() -> None:
    registry = ExtensionRegistry()
    _register_command(registry)
    manager = RunningManager((PLUGIN_ID,))
    router = DirectCommandRouter(bindings={"*": TARGET}, registry=registry, manager=manager)

    match = router.match("  *攻击 @玩家  ")

    assert match == DirectCommandMatch(
        prefix="*",
        plugin_id=PLUGIN_ID,
        command_name="play",
        arguments="攻击 @玩家",
        active=True,
        reason="active",
    )
    assert router.match("ordinary chat") is None
    assert router.diagnostics()[0].active is True


def test_router_keeps_configured_but_inactive_bindings_fail_closed() -> None:
    registry = ExtensionRegistry()
    _register_command(registry)
    manager = RunningManager(())
    router = DirectCommandRouter(bindings={"*": TARGET}, registry=registry, manager=manager)

    stopped = router.match("*签到")
    assert stopped is not None and stopped.active is False
    assert stopped.reason == "plugin_not_running"

    manager.running_plugin_ids = (PLUGIN_ID,)
    registry.remove_plugin(PLUGIN_ID)
    missing = router.match("*签到")
    assert missing is not None and missing.reason == "command_not_registered"


@pytest.mark.parametrize(
    "permission",
    [PermissionLevel.TRUSTED, PermissionLevel.MODERATOR, PermissionLevel.SUPERUSER],
)
def test_router_keeps_permissioned_command_targets_active(permission: PermissionLevel) -> None:
    registry = ExtensionRegistry()
    _register_command(registry, permission=permission)
    router = DirectCommandRouter(
        bindings={"*": TARGET},
        registry=registry,
        manager=RunningManager((PLUGIN_ID,)),
    )

    match = router.match("*数据清除")
    assert match is not None and match.active is True
    assert match.reason == "active"


@pytest.mark.asyncio
async def test_adapter_rechecks_lifecycle_permission_and_trusted_context() -> None:
    captured: dict[str, object] = {}

    async def handler(arguments: BaseModel) -> CommandResult:
        captured["arguments"] = arguments.model_dump()
        return CommandResult(text="played")

    registry = ExtensionRegistry()
    _register_command(registry, handler=handler)
    manager = RunningManager((PLUGIN_ID,))

    @asynccontextmanager
    async def invocation_scope(
        plugin_id: str, runtime: object, *, web_was_used: bool
    ) -> AsyncIterator[None]:
        captured["plugin_id"] = plugin_id
        captured["runtime"] = runtime
        captured["web_was_used"] = web_was_used
        yield

    adapter = PluginCommandAdapter(
        manager=cast(PluginManager, manager),
        registry=registry,
        superusers=frozenset(),
        invocation_scope=invocation_scope,
    )
    message = InboundMessage(
        message_id="direct-1",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="*攻击 @玩家",
        bot_user_id="99999",
        group_id="20001",
        mentioned_user_ids=("10002",),
        received_at=datetime.now(UTC),
    )
    match = DirectCommandMatch("*", PLUGIN_ID, "play", "攻击 @玩家", True, "active")

    result = await adapter.execute_direct(
        message=message,
        identity=ConversationScope.group("99999", "20001"),
        match=match,
        runtime=cast(RuntimeConfigSnapshot, SimpleNamespace()),
    )

    assert result == "played"
    assert captured["arguments"] == {"text": "攻击 @玩家"}
    runtime = cast(ToolRuntime, captured["runtime"])
    assert runtime.inbound is message
    assert runtime.trigger_message_id == "direct-1"
    assert runtime.actor_user_id == "10001"
    assert runtime.current_group_id == "20001"
    assert runtime.mentioned_user_ids == ("10002",)

    manager.running_plugin_ids = ()
    assert "未运行" in await adapter.execute_direct(
        message=message,
        identity=ConversationScope.group("99999", "20001"),
        match=match,
        runtime=cast(RuntimeConfigSnapshot, SimpleNamespace()),
    )


@pytest.mark.asyncio
async def test_adapter_keeps_registered_command_timeout() -> None:
    async def handler(_arguments: BaseModel) -> CommandResult:
        await asyncio.sleep(1)
        return CommandResult(text="too late")

    registry = ExtensionRegistry()
    _register_command(registry, handler=handler, timeout_seconds=0.001)
    adapter = PluginCommandAdapter(
        manager=cast(PluginManager, RunningManager((PLUGIN_ID,))),
        registry=registry,
        superusers=frozenset(),
    )
    message = _inbound("*签到", message_id="direct-timeout")

    result = await adapter.execute_direct(
        message=message,
        identity=ConversationScope.group("99999", "2001"),
        match=DirectCommandMatch("*", PLUGIN_ID, "play", "签到", True, "active"),
        runtime=cast(RuntimeConfigSnapshot, SimpleNamespace()),
    )

    assert result == "插件命令执行超时。"


class StaticResolver:
    def __init__(self, *, active: bool = True) -> None:
        self.active = active

    def match(self, text: str) -> DirectCommandMatch | None:
        if not text.strip().startswith("*"):
            return None
        return DirectCommandMatch(
            "*",
            PLUGIN_ID,
            "play",
            text.strip()[1:].strip(),
            self.active,
            "active" if self.active else "plugin_not_running",
        )


class RecordingCommandService:
    def __init__(self, ledger: EventLedgerRepository) -> None:
        self.ledger = ledger
        self.calls: list[DirectCommandMatch] = []
        self.ledger_seen = False

    @staticmethod
    def may_write(_command: object, _argument: str) -> bool:
        return True

    async def execute_direct_plugin(
        self,
        message: InboundMessage,
        _identity: ConversationScope,
        match: DirectCommandMatch,
    ) -> CommandExecution:
        self.calls.append(match)
        record = await self.ledger.find_by_platform_message(
            bot_user_id=message.bot_user_id or "unknown-bot",
            platform_message_id=message.message_id,
        )
        self.ledger_seen = record is not None
        return CommandExecution("played" if match.active else "binding inactive")


def _inbound(
    text: str,
    *,
    message_id: str,
    group_id: str = "2001",
    image: bool = False,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text=text,
        bot_user_id="99999",
        group_id=group_id,
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "image"),) if image else (),
        received_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_management_enable_reports_start_failure_instead_of_success() -> None:
    class FailedManager:
        running_plugin_ids: tuple[str, ...] = ()

        @staticmethod
        async def enable(plugin_id: str, *, actor_user_id: str) -> SimpleNamespace:
            assert actor_user_id == "10001"
            return SimpleNamespace(
                plugin_id=plugin_id,
                last_error_category="PluginLifecycleError",
            )

    adapter = PluginCommandAdapter(
        manager=cast(PluginManager, FailedManager()),
        registry=ExtensionRegistry(),
        superusers=frozenset({"10001"}),
    )

    result = await adapter.execute(
        message=_inbound("/ai plugin enable github-monitor", message_id="plugin-enable-failed"),
        identity=ConversationScope.group("99999", "2001"),
        argument="enable github-monitor",
        runtime=cast(RuntimeConfigSnapshot, SimpleNamespace()),
    )

    assert "启动失败：PluginLifecycleError" in result
    assert "/ai plugin doctor github-monitor" in result


@pytest.mark.asyncio
async def test_processor_direct_command_obeys_admission_dedup_ledger_and_image_isolation(
    database: Database,
) -> None:
    service = RecordingCommandService(EventLedgerRepository(database))
    harness = build_harness(
        database,
        make_settings(database.url),
        command_service=cast(CommandService, service),
        direct_plugin_commands=StaticResolver(),
    )

    sender = MemorySender()
    message = _inbound("*签到", message_id="direct-processor")
    first = await harness.processor.handle(message, sender)
    duplicate = await harness.processor.handle(message, MemorySender())
    disabled = await harness.processor.handle(
        _inbound("*签到", message_id="direct-disabled", group_id="2999"),
        MemorySender(),
    )
    image_sender = MemorySender()
    image = await harness.processor.handle(
        _inbound("*签到", message_id="direct-image", image=True),
        image_sender,
    )

    assert first.reason == "command_plugin_direct"
    assert sender.messages[0].text == "played"
    assert service.ledger_seen is True
    assert duplicate.reason == "duplicate"
    assert disabled.reason == "group_disabled"
    assert image.reason == "image_write_isolated"
    assert len(service.calls) == 1
    async with database.sessions() as session:
        memory_job_count = int(
            await session.scalar(select(func.count()).select_from(MemoryJobModel)) or 0
        )
    assert memory_job_count == 0


@pytest.mark.asyncio
async def test_processor_consumes_inactive_binding_without_entering_planner(
    database: Database,
) -> None:
    service = RecordingCommandService(EventLedgerRepository(database))
    harness = build_harness(
        database,
        make_settings(database.url),
        command_service=cast(CommandService, service),
        direct_plugin_commands=StaticResolver(active=False),
    )
    sender = MemorySender()

    result = await harness.processor.handle(_inbound("*签到", message_id="direct-inactive"), sender)

    assert result.reason == "command_plugin_direct"
    assert sender.messages[0].text == "binding inactive"
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_processor_rate_limits_direct_commands(database: Database) -> None:
    service = RecordingCommandService(EventLedgerRepository(database))
    harness = build_harness(
        database,
        make_settings(
            database.url,
            per_user_requests_per_minute=1,
            per_group_requests_per_minute=10,
        ),
        command_service=cast(CommandService, service),
        direct_plugin_commands=StaticResolver(),
    )

    first = await harness.processor.handle(
        _inbound("*签到", message_id="direct-rate-1"), MemorySender()
    )
    second = await harness.processor.handle(
        _inbound("*签到", message_id="direct-rate-2"), MemorySender()
    )

    assert first.reason == "command_plugin_direct"
    assert second.reason == "user_rate_limited"
    assert len(service.calls) == 1
