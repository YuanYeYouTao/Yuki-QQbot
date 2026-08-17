"""Deterministic `/ai plugin` management and registered command dispatch."""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import cast

from pydantic import ValidationError

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ConversationIdentity
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.plugin_host.direct_command_router import (
    DirectCommandMatch,
    DirectCommandRouter,
)
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.services.agent_tools import ToolRuntime
from yuki_plugin_sdk.models import PermissionLevel
from yuki_plugin_sdk.registrar import CommandRegistration


class PluginCommandAdapter:
    def __init__(
        self,
        *,
        manager: PluginManager,
        registry: ExtensionRegistry,
        superusers: frozenset[str],
        invocation_scope: Callable[..., object] | None = None,
        direct_commands: DirectCommandRouter | None = None,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._superusers = superusers
        self._invocation_scope = invocation_scope
        self._direct_commands = direct_commands

    async def execute(
        self,
        *,
        message: InboundMessage,
        identity: ConversationIdentity,
        argument: str,
        runtime: RuntimeConfigSnapshot,
    ) -> str:
        parts = argument.strip().split(maxsplit=1)
        operation = parts[0].casefold() if parts else "list"
        remainder = parts[1].strip() if len(parts) == 2 else ""
        if operation == "run":
            return await self._run_command(message, identity, remainder, runtime)
        if message.sender.user_id not in self._superusers:
            return "权限不足：插件管理命令仅限超级管理员。"
        if operation == "list":
            rows = await self._manager.list()
            if not rows:
                return "尚未发现任何插件。"
            return "插件列表：\n" + "\n".join(_installation_line(row) for row in rows)
        target = remainder.split(maxsplit=1)[0] if remainder else ""
        if not target:
            return f"格式错误：/ai plugin {operation} <plugin_id>"
        if operation in {"show", "permissions"}:
            row = await self._manager.show(target)
            return _installation_detail(row) if row is not None else "没有找到该插件。"
        if operation == "approve":
            row = await self._manager.approve(
                target,
                actor_user_id=message.sender.user_id,
            )
            return f"已批准插件：{getattr(row, 'plugin_id', target)}。"
        if operation == "enable":
            row = await self._manager.enable(
                target,
                actor_user_id=message.sender.user_id,
            )
            if target not in self._manager.running_plugin_ids:
                error_category = getattr(row, "last_error_category", None) or "unknown"
                return (
                    "插件启用开关已写入，但启动失败："
                    f"{error_category}。请使用 /ai plugin doctor {target} 查看诊断。"
                )
            return f"已启用插件：{getattr(row, 'plugin_id', target)}。"
        if operation == "disable":
            row = await self._manager.disable(
                target,
                actor_user_id=message.sender.user_id,
            )
            return f"已停用插件：{getattr(row, 'plugin_id', target)}。"
        if operation == "doctor":
            report = (await self._manager.doctor(target)).model_dump(mode="json")
            text = "插件诊断：\n" + "\n".join(
                f"- {key}: {value}" for key, value in sorted(report.items())
            )
            bindings = (
                self._direct_commands.diagnostics(plugin_id=target)
                if self._direct_commands is not None
                else ()
            )
            if bindings:
                text += "\n直达绑定：\n" + "\n".join(
                    f"- {row.prefix} -> {row.command_name}: {row.reason}" for row in bindings
                )
            return text
        return "未知插件命令，请使用 /ai plugin list。"

    async def execute_direct(
        self,
        *,
        message: InboundMessage,
        identity: ConversationIdentity,
        match: DirectCommandMatch,
        runtime: RuntimeConfigSnapshot,
    ) -> str:
        """Execute one Host-owned direct binding through the normal command scope."""

        if not match.active:
            return f"插件直达命令暂不可用：{match.reason}。"
        return await self._execute_registered_command(
            message=message,
            identity=identity,
            plugin_id=match.plugin_id,
            command_name=match.command_name,
            raw_arguments=match.arguments,
            runtime=runtime,
            allow_alias=False,
        )

    async def _run_command(
        self,
        message: InboundMessage,
        identity: ConversationIdentity,
        remainder: str,
        runtime: RuntimeConfigSnapshot,
    ) -> str:
        parts = remainder.split(maxsplit=2)
        if len(parts) < 2:
            return "格式错误：/ai plugin run <plugin_id> <command> [参数]"
        plugin_id, command_name = parts[:2]
        raw_arguments = parts[2] if len(parts) == 3 else ""
        return await self._execute_registered_command(
            message=message,
            identity=identity,
            plugin_id=plugin_id,
            command_name=command_name,
            raw_arguments=raw_arguments,
            runtime=runtime,
            allow_alias=True,
        )

    async def _execute_registered_command(
        self,
        *,
        message: InboundMessage,
        identity: ConversationIdentity,
        plugin_id: str,
        command_name: str,
        raw_arguments: str,
        runtime: RuntimeConfigSnapshot,
        allow_alias: bool,
    ) -> str:
        if plugin_id not in self._manager.running_plugin_ids:
            return "插件当前未运行。"
        item = self._registry.get(f"{plugin_id}:{command_name}")
        if allow_alias and (item is None or item.kind is not ExtensionKind.COMMAND):
            item = self._registry.resolve_command_alias(command_name)
        if item is None or item.kind is not ExtensionKind.COMMAND or item.plugin_id != plugin_id:
            return "没有找到该插件命令。"
        registration = cast(CommandRegistration, item.registration)
        if not _level_allowed(
            registration.metadata.permission,
            message.sender.user_id in self._superusers,
        ):
            return "权限不足：当前 QQ 不能执行该插件命令。"
        try:
            payload = _parse_arguments(registration, raw_arguments)
            arguments = registration.argument_model.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            return f"插件命令参数错误：{type(exc).__name__}"
        tool_runtime = ToolRuntime(
            inbound=message,
            gateway=None,
            allow_generic_onebot=False,
            allow_admin_actions=False,
            allow_automation=False,
            conversation_key=identity.key,
            trigger_message_id=message.message_id,
            actor_user_id=message.sender.user_id,
            actor_is_superuser=message.sender.user_id in self._superusers,
            current_group_id=message.group_id,
            mentioned_user_ids=message.mentioned_user_ids,
            runtime_config=runtime,
            origin=TurnOrigin.USER_MESSAGE,
            tools_closed=True,
        )
        try:
            async with asyncio.timeout(registration.metadata.timeout_seconds):
                async with self._scope(item.plugin_id, tool_runtime):
                    result = await registration.handler(arguments)
        except TimeoutError:
            return "插件命令执行超时。"
        except Exception as exc:
            return f"插件命令执行失败：{type(exc).__name__}"
        return (
            result.text
            or result.detail
            or (json.dumps(result.data, ensure_ascii=False) if result.data else "插件命令已完成。")
        )

    @asynccontextmanager
    async def _scope(self, plugin_id: str, runtime: ToolRuntime):  # type: ignore[no-untyped-def]
        if self._invocation_scope is None:
            yield
            return
        scope = self._invocation_scope(plugin_id, runtime, web_was_used=False)
        enter = getattr(scope, "__aenter__", None)
        leave = getattr(scope, "__aexit__", None)
        if not callable(enter) or not callable(leave):
            raise RuntimeError("plugin invocation scope must be an async context manager")
        await enter()
        try:
            yield
        finally:
            await leave(None, None, None)


def _parse_arguments(registration: CommandRegistration, raw: str) -> object:
    if not raw:
        return {}
    stripped = raw.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    fields = registration.argument_model.model_fields
    if len(fields) == 1:
        return {next(iter(fields)): stripped}
    result: dict[str, object] = {}
    for token in shlex.split(stripped):
        if "=" not in token:
            raise ValueError("multi-field command arguments must use key=value")
        key, value = token.split("=", 1)
        result[key] = value
    return result


def _level_allowed(level: PermissionLevel, superuser: bool) -> bool:
    return level is PermissionLevel.USER or (level is PermissionLevel.SUPERUSER and superuser)


def _installation_line(row: object) -> str:
    return (
        f"- {getattr(row, 'plugin_id', '?')} "
        f"v{getattr(row, 'version', '?')} "
        f"[{getattr(row, 'status', '?')}] "
        f"enabled={getattr(row, 'enabled', False)}"
    )


def _installation_detail(row: object) -> str:
    requested = ", ".join(getattr(row, "requested_permissions", ())) or "无"
    approved = ", ".join(getattr(row, "approved_permissions", ())) or "无"
    return (
        f"插件：{getattr(row, 'plugin_id', '?')}\n"
        f"版本：{getattr(row, 'version', '?')}\n"
        f"状态：{getattr(row, 'status', '?')}\n"
        f"已启用：{getattr(row, 'enabled', False)}\n"
        f"申请权限：{requested}\n"
        f"已批准权限：{approved}"
    )


__all__ = ["PluginCommandAdapter"]
