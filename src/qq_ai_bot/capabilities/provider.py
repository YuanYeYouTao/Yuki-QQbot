"""Adapters that export existing ChatTool definitions as common descriptors."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.binding import InProcessToolBinding
from qq_ai_bot.capabilities.invocation import ToolInvocationContext
from qq_ai_bot.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityExposure,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.domain.messages import ChatTool

_ALL_ORIGINS = frozenset(TurnOrigin)
_DIRECT_ORIGINS = frozenset({TurnOrigin.USER_MESSAGE})

_CORE_METADATA: dict[str, tuple[str, CapabilityEffect, CapabilityRisk]] = {
    "get_my_capabilities": ("capability", CapabilityEffect.READ_STATE, CapabilityRisk.READ),
    "get_recent_chat_history": ("memory", CapabilityEffect.EXTERNAL_READ, CapabilityRisk.READ),
    "search_chat_history": ("memory", CapabilityEffect.READ_STATE, CapabilityRisk.READ),
    "get_person_memories": ("memory", CapabilityEffect.READ_STATE, CapabilityRisk.READ),
    "get_self_memories": ("memory", CapabilityEffect.READ_STATE, CapabilityRisk.READ),
    "get_group_memories": ("memory", CapabilityEffect.READ_STATE, CapabilityRisk.READ),
    "memory_change": ("memory", CapabilityEffect.WRITE_STATE, CapabilityRisk.MUTATE),
    "web_search": ("web", CapabilityEffect.EXTERNAL_READ, CapabilityRisk.READ),
    "read_webpage": ("web", CapabilityEffect.EXTERNAL_READ, CapabilityRisk.READ),
    "call_onebot_api": ("onebot", CapabilityEffect.PLATFORM_MUTATE, CapabilityRisk.MUTATE),
    "get_group_member_info": (
        "onebot",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "set_group_ban": ("onebot", CapabilityEffect.PLATFORM_MUTATE, CapabilityRisk.MUTATE),
    "kick_group_member": (
        "onebot",
        CapabilityEffect.PLATFORM_MUTATE,
        CapabilityRisk.DESTRUCTIVE,
    ),
    "send_private_message": (
        "onebot",
        CapabilityEffect.PLATFORM_SEND,
        CapabilityRisk.MUTATE,
    ),
    "delete_message": (
        "onebot",
        CapabilityEffect.PLATFORM_MUTATE,
        CapabilityRisk.DESTRUCTIVE,
    ),
    "send_voice": ("speech", CapabilityEffect.REPLY_EFFECT, CapabilityRisk.READ),
}

_CORE_SEARCH_TAGS: dict[str, tuple[str, ...]] = {
    "get_recent_chat_history": (
        "刚才",
        "刚刚",
        "最近消息",
        "聊天记录",
        "对话历史",
        "前面说了什么",
    ),
    "search_chat_history": (
        "之前",
        "以前",
        "历史消息",
        "聊天记录",
        "说过",
        "提过",
        "查记录",
        "以前聊过",
    ),
    "get_person_memories": (
        "人物记忆",
        "群友记忆",
        "某人",
        "关于他",
        "关于她",
        "偏好",
        "记得",
    ),
    "get_self_memories": (
        "Yuki记忆",
        "自我记忆",
        "你的经历",
        "你的偏好",
        "你记得",
    ),
    "get_group_memories": ("群记忆", "群整体", "这个群", "群信息", "群里的情况"),
    "memory_change": (
        "记住",
        "保存记忆",
        "纠正记忆",
        "修改记忆",
        "忘记",
        "撤销",
        "恢复记忆",
    ),
    "web_search": ("搜索", "联网", "网上查", "最新", "新闻", "查资料", "查询资料"),
    "read_webpage": ("网页", "链接", "URL", "打开网页", "读取页面", "看这个链接"),
    "call_onebot_api": ("QQ群", "好友", "禁言", "踢人", "群设置", "QQ操作"),
    "get_group_member_info": ("group member", "member info", "群成员"),
    "set_group_ban": ("ban", "mute", "禁言"),
    "kick_group_member": ("kick", "remove member", "踢人"),
    "send_private_message": ("private message", "send message", "私聊"),
    "delete_message": ("delete message", "recall", "撤回"),
    "send_voice": ("语音", "朗读", "说出来", "用语音"),
}

_ADMIN_READ = frozenset({"admin_get_config", "admin_get_history"})
_AUTOMATION_READ = frozenset(
    {
        "automation_list",
        "automation_list_history",
        "automation_get",
        "automation_history",
        "time_get_current",
        "time_get_timezone",
    }
)


class CapabilityProvider(Protocol):
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]: ...


class ChatToolCapabilityProvider:
    """Attach policy metadata to existing domain-owned tool definitions."""

    def __init__(
        self,
        tools: tuple[ChatTool, ...],
        *,
        source: CapabilityTrustSource,
        plugin_read_only: Callable[[str], bool] | None = None,
    ) -> None:
        self._tools = tools
        self._source = source
        self._plugin_read_only = plugin_read_only

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._descriptor(tool) for tool in self._tools)

    def _descriptor(self, tool: ChatTool) -> CapabilityDescriptor:
        group, effect, risk = self._metadata(tool.name)
        permissions = (
            frozenset({"superuser"})
            if self._source is CapabilityTrustSource.ADMIN or group == "onebot"
            else frozenset()
        )
        return CapabilityDescriptor(
            canonical_name=f"{self._source.value}.{tool.name}",
            model_name=tool.name,
            group=group,
            input_schema=tool.parameters,
            output_schema={"type": "object"},
            effect=effect,
            risk=risk,
            trust_source=self._source,
            allowed_origins=(
                _DIRECT_ORIGINS
                if self._source is CapabilityTrustSource.ADMIN or group == "onebot"
                else _ALL_ORIGINS
            ),
            required_permissions=permissions,
            uses_external_data=effect is CapabilityEffect.EXTERNAL_READ,
            cancellable=effect in {CapabilityEffect.READ_STATE, CapabilityEffect.EXTERNAL_READ},
            idempotency=(
                CapabilityIdempotency.IDEMPOTENT
                if risk is CapabilityRisk.READ
                else CapabilityIdempotency.CONDITIONAL
            ),
            exposure=(
                CapabilityExposure.DIRECT_ALWAYS
                if tool.name == "get_my_capabilities"
                else CapabilityExposure.PLANNED
            ),
        )

    def _metadata(self, name: str) -> tuple[str, CapabilityEffect, CapabilityRisk]:
        if self._source is CapabilityTrustSource.CORE:
            return _CORE_METADATA.get(
                name,
                ("memory", CapabilityEffect.READ_STATE, CapabilityRisk.READ),
            )
        if self._source is CapabilityTrustSource.ADMIN:
            if name in _ADMIN_READ:
                group = "config" if name == "admin_get_config" else "admin"
                return group, CapabilityEffect.READ_STATE, CapabilityRisk.READ
            group = "config" if "config" in name or "rollback" in name else "admin"
            return group, CapabilityEffect.WRITE_STATE, CapabilityRisk.MUTATE
        if self._source is CapabilityTrustSource.AUTOMATION:
            if name in _AUTOMATION_READ:
                return "automation", CapabilityEffect.READ_STATE, CapabilityRisk.READ
            return "automation", CapabilityEffect.WRITE_STATE, CapabilityRisk.MUTATE
        read_only = bool(self._plugin_read_only and self._plugin_read_only(name))
        return (
            "plugin",
            CapabilityEffect.READ_STATE if read_only else CapabilityEffect.WRITE_STATE,
            CapabilityRisk.READ if read_only else CapabilityRisk.MUTATE,
        )


LegacyExecutor = Callable[[str, str, Any], Awaitable[object]]
DefinitionFactory = Callable[[Any], tuple[ChatTool, ...]]


class InProcessToolProvider:
    """Expose an existing domain service through provider-neutral bindings."""

    def __init__(
        self,
        *,
        provider_id: str,
        source: CapabilityTrustSource,
        definitions: DefinitionFactory,
        execute: LegacyExecutor,
        plugin_read_only: Callable[[str], bool] | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._source = source
        self._definitions = definitions
        self._execute = execute
        self._plugin_read_only = plugin_read_only

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def descriptors(self, context: Any) -> tuple[CapabilityDescriptor, ...]:
        tools = self._definitions(context)
        descriptors = ChatToolCapabilityProvider(
            tools,
            source=self._source,
            plugin_read_only=self._plugin_read_only,
        ).descriptors()
        return tuple(
            self._bound(descriptor, tool)
            for descriptor, tool in zip(descriptors, tools, strict=True)
        )

    def _bound(
        self,
        descriptor: CapabilityDescriptor,
        tool: ChatTool,
    ) -> CapabilityDescriptor:
        async def invoke(
            arguments: dict[str, object],
            context: ToolInvocationContext,
        ) -> object:
            return await self._execute(
                tool.name,
                json.dumps(arguments, ensure_ascii=False),
                context.runtime,
            )

        search_tags = (
            _CORE_SEARCH_TAGS.get(tool.name, ())
            if self._source is CapabilityTrustSource.CORE
            else ()
        )
        return replace(
            descriptor,
            provider_id=self._provider_id,
            provider_tool_name=tool.name,
            description=tool.description,
            compact_description=tool.description[:240],
            tags=tuple(
                dict.fromkeys(
                    (
                        descriptor.group,
                        self._source.value,
                        *search_tags,
                    )
                )
            ),
            binding=InProcessToolBinding(
                provider_id=self._provider_id,
                tool_name=tool.name,
                handler=invoke,
            ),
            parallel_safe=descriptor.risk is CapabilityRisk.READ,
        )

    async def refresh(self, *, force: bool = False) -> None:
        del force

    async def close(self) -> None:
        return None
