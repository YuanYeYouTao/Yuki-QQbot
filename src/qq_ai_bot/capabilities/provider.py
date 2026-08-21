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
from qq_ai_bot.capabilities.search_aliases import merge_search_terms
from qq_ai_bot.domain.messages import ChatTool

_ALL_ORIGINS = frozenset(TurnOrigin)
_DIRECT_ORIGINS = frozenset({TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP})
_AUTONOMOUS_ORIGIN = frozenset({TurnOrigin.AUTONOMOUS_GROUP})
_REPLY_LAYOUT_ORIGINS = frozenset({TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP})
_ORIGIN_OVERRIDES: dict[str, frozenset[TurnOrigin]] = {
    "decline_reply": _AUTONOMOUS_ORIGIN,
    "set_voice_preference": _DIRECT_ORIGINS,
    "set_reply_layout": _REPLY_LAYOUT_ORIGINS,
    "send_voice": _REPLY_LAYOUT_ORIGINS,
    "send_emoji": _REPLY_LAYOUT_ORIGINS,
}

_CORE_METADATA: dict[str, tuple[str, CapabilityEffect, CapabilityRisk]] = {
    "get_my_capabilities": (
        "kernel.authority.read",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "read_tool_artifact": (
        "kernel.artifact.read",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "get_recent_chat_history": (
        "memory.history.recent",
        CapabilityEffect.EXTERNAL_READ,
        CapabilityRisk.READ,
    ),
    "search_chat_history": (
        "memory.history.search",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "get_chat_history_around": (
        "memory.history.around",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "get_person_memories": (
        "memory.person.read",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "get_relationship": (
        "relationship.read",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "get_self_memories": (
        "memory.self.read",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "get_group_memories": (
        "memory.group.read",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "get_memory_fact": (
        "memory.fact.read",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "get_memory_evidence": (
        "memory.evidence.read",
        CapabilityEffect.READ_STATE,
        CapabilityRisk.READ,
    ),
    "memory_change": (
        "memory.state.write",
        CapabilityEffect.WRITE_STATE,
        CapabilityRisk.MUTATE,
    ),
    "web_search": ("web.search", CapabilityEffect.EXTERNAL_READ, CapabilityRisk.READ),
    "read_webpage": ("web.read", CapabilityEffect.EXTERNAL_READ, CapabilityRisk.READ),
    "call_onebot_api": (
        "qq.platform.mutate",
        CapabilityEffect.PLATFORM_MUTATE,
        CapabilityRisk.MUTATE,
    ),
    "send_voice": ("reply.voice", CapabilityEffect.REPLY_EFFECT, CapabilityRisk.READ),
    "send_emoji": ("reply.emoji", CapabilityEffect.REPLY_EFFECT, CapabilityRisk.READ),
    "set_reply_layout": (
        "reply.layout",
        CapabilityEffect.REPLY_EFFECT,
        CapabilityRisk.READ,
    ),
    "set_reply_target": (
        "reply.target",
        CapabilityEffect.REPLY_EFFECT,
        CapabilityRisk.READ,
    ),
    "set_voice_preference": (
        "reply.voice.preference.write",
        CapabilityEffect.WRITE_STATE,
        CapabilityRisk.MUTATE,
    ),
    "decline_reply": (
        "reply.admission.decline",
        CapabilityEffect.REPLY_EFFECT,
        CapabilityRisk.READ,
    ),
}

_CORE_USE_WHEN: dict[str, tuple[str, ...]] = {
    "get_my_capabilities": ("我能改什么", "权限范围", "有哪些设置"),
    "get_recent_chat_history": ("刚才说了什么", "最近消息", "当前对话历史"),
    "search_chat_history": ("以前聊过", "查记录", "历史消息"),
    "get_chat_history_around": ("这条前后", "附近消息", "对齐原话"),
    "get_person_memories": ("记得他", "人物记忆", "关于她"),
    "get_self_memories": ("你记得", "你的经历", "自我记忆"),
    "get_group_memories": ("群记忆", "这个群", "群里的情况"),
    "memory_change": ("记住", "忘记", "纠正记忆", "保存记忆"),
    "get_relationship": ("好感度", "信任度", "关系阶段"),
    "web_search": ("搜索", "联网", "查资料", "最新新闻", "搜下", "上网"),
    "read_webpage": ("打开网页", "阅读链接", "看这个URL"),
    "call_onebot_api": ("禁言", "踢人", "QQ群操作"),
    "send_voice": ("语音", "朗读", "说出来"),
    "send_emoji": ("表情", "表情包", "发个表情"),
    "set_reply_layout": ("分条", "拆成几条", "分开发"),
    "set_reply_target": ("引用这条", "回复那条消息"),
    "set_voice_preference": ("以后用语音", "默认语音", "不要语音"),
    "decline_reply": ("不用回", "先不插话"),
    "read_tool_artifact": ("读取工具结果", "artifact"),
}

_ADMIN_NAMESPACES: dict[str, str] = {
    "admin_get_config": "admin.config.read",
    "admin_set_config": "admin.config.write",
    "admin_delete_config_override": "admin.config.write",
    "admin_execute_action": "admin.action.write",
    "admin_get_history": "admin.history.read",
    "admin_rollback_change": "admin.config.write",
    "admin_memory_rebuild_plan": "admin.memory.rebuild",
    "admin_memory_rebuild_start": "admin.memory.rebuild",
    "admin_memory_rebuild_status": "admin.memory.rebuild",
    "admin_memory_rebuild_pause": "admin.memory.rebuild",
    "admin_memory_rebuild_resume": "admin.memory.rebuild",
    "admin_memory_rebuild_cancel": "admin.memory.rebuild",
    "admin_memory_rebuild_review": "admin.memory.rebuild",
    "admin_memory_rebuild_approve": "admin.memory.rebuild",
    "admin_memory_rebuild_reject": "admin.memory.rebuild",
    "admin_memory_rebuild_commit": "admin.memory.rebuild",
}

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
    "get_chat_history_around": (
        "这条前后",
        "附近消息",
        "对齐原话",
        "原文",
        "前后几条",
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
    "get_relationship": (
        "好感度",
        "信任度",
        "关系阶段",
        "亲密度",
        "关系数据",
    ),
    "get_self_memories": (
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
    "web_search": (
        "搜索",
        "联网",
        "网上查",
        "最新",
        "新闻",
        "查资料",
        "查询资料",
        "搜下",
        "搜搜",
        "上网",
    ),
    "read_webpage": ("网页", "链接", "URL", "打开网页", "读取页面", "看这个链接"),
    "call_onebot_api": ("QQ群", "好友", "禁言", "踢人", "群设置", "QQ操作"),
    "send_voice": ("语音", "朗读", "说出来", "用语音"),
    "send_emoji": ("表情", "表情包", "发个表情", "来张图"),
    "set_reply_layout": ("分条", "拆成几条", "分开发", "一条一条"),
    "set_voice_preference": ("以后用语音", "默认语音", "不要语音", "语音偏好"),
    "decline_reply": ("不用回", "先不插话", "这条无关"),
}

_ADMIN_READ = frozenset({"admin_get_config", "admin_get_history"})


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
        result: list[CapabilityDescriptor] = []
        for tool in self._tools:
            descriptor = self._descriptor(tool)
            if descriptor is not None:
                result.append(descriptor)
        return tuple(result)

    def _descriptor(self, tool: ChatTool) -> CapabilityDescriptor | None:
        metadata = self._metadata(tool)
        if metadata is None:
            return None
        namespace, effect, risk = metadata
        permissions = (
            frozenset({"superuser"})
            if self._source is CapabilityTrustSource.ADMIN or namespace.startswith("qq.")
            else frozenset()
        )
        aliases = tool.aliases or _CORE_SEARCH_TAGS.get(tool.name, ())
        use_when = tool.use_when or _CORE_USE_WHEN.get(tool.name, ())
        aliases, use_when = merge_search_terms(
            aliases=tuple(aliases),
            use_when=tuple(use_when),
            tool_name=tool.name,
        )
        tags = tool.tags or (namespace.split(".")[0], self._source.value)
        return CapabilityDescriptor(
            canonical_name=f"{self._source.value}.{tool.name}",
            model_name=tool.name,
            group=namespace.split(".")[0],
            namespace=namespace,
            aliases=tuple(dict.fromkeys(aliases)),
            use_when=tuple(dict.fromkeys(use_when)),
            input_schema=tool.parameters,
            output_schema={"type": "object"},
            effect=effect,
            risk=risk,
            trust_source=self._source,
            allowed_origins=_ORIGIN_OVERRIDES.get(
                tool.name,
                (
                    _DIRECT_ORIGINS
                    if self._source is CapabilityTrustSource.ADMIN or namespace.startswith("qq.")
                    else _ALL_ORIGINS
                ),
            ),
            required_permissions=permissions,
            uses_external_data=effect is CapabilityEffect.EXTERNAL_READ,
            cancellable=effect in {CapabilityEffect.READ_STATE, CapabilityEffect.EXTERNAL_READ},
            idempotency=(
                CapabilityIdempotency.IDEMPOTENT
                if risk is CapabilityRisk.READ
                else CapabilityIdempotency.CONDITIONAL
            ),
            exposure=CapabilityExposure.PLANNED,
            schema_version=str(tool.schema_version),
            tags=tuple(dict.fromkeys(tags)),
        )

    def _metadata(self, tool: ChatTool) -> tuple[str, CapabilityEffect, CapabilityRisk] | None:
        if self._source is CapabilityTrustSource.CORE:
            mapped = _CORE_METADATA.get(tool.name)
            if mapped is None:
                return None
            return mapped
        if self._source is CapabilityTrustSource.ADMIN:
            namespace = _ADMIN_NAMESPACES.get(
                tool.name,
                "admin.config.write" if "config" in tool.name else "admin.action.write",
            )
            if tool.name in _ADMIN_READ:
                return namespace, CapabilityEffect.READ_STATE, CapabilityRisk.READ
            return namespace, CapabilityEffect.WRITE_STATE, CapabilityRisk.MUTATE
        if self._source is CapabilityTrustSource.AUTOMATION:
            if tool.name in _AUTOMATION_READ:
                return "automation.read", CapabilityEffect.READ_STATE, CapabilityRisk.READ
            return "automation.write", CapabilityEffect.WRITE_STATE, CapabilityRisk.MUTATE
        namespace = tool.namespace.strip() or f"plugin.{tool.name}"
        read_only = bool(self._plugin_read_only and self._plugin_read_only(tool.name))
        return (
            namespace,
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
        bot_aliases: tuple[str, ...] = ("Yuki", "yuki", "由纪"),
    ) -> None:
        self._provider_id = provider_id
        self._source = source
        self._definitions = definitions
        self._execute = execute
        self._plugin_read_only = plugin_read_only
        self._bot_aliases = bot_aliases

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def descriptors(self, context: Any) -> tuple[CapabilityDescriptor, ...]:
        tools = self._definitions(context)
        provider = ChatToolCapabilityProvider(
            tools,
            source=self._source,
            plugin_read_only=self._plugin_read_only,
        )
        by_name = {item.model_name: item for item in provider.descriptors()}
        return tuple(
            self._bound(by_name[tool.name], tool) for tool in tools if tool.name in by_name
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
            else tool.tags
        )
        if tool.name == "get_self_memories":
            search_tags = (
                *search_tags,
                *(f"{alias}记忆" for alias in self._bot_aliases if alias.strip()),
            )
        return replace(
            descriptor,
            provider_id=self._provider_id,
            provider_tool_name=tool.name,
            description=tool.description,
            compact_description=tool.description[:240],
            aliases=tuple(dict.fromkeys((*descriptor.aliases, *search_tags))),
            tags=tuple(
                dict.fromkeys(
                    (
                        descriptor.namespace_id,
                        self._source.value,
                        *descriptor.tags,
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
