"""Stable Plugin API v2 permission names."""

from __future__ import annotations

from enum import StrEnum


class PluginPermission(StrEnum):
    MESSAGE_CURRENT_READ = "message.current.read"
    MESSAGE_REPLY_READ = "message.reply.read"
    MESSAGE_HISTORY_READ = "message.history.read"
    MESSAGE_PRIVATE_SEND = "message.private.send"
    MESSAGE_GROUP_SEND = "message.group.send"
    MESSAGE_MEDIA_SEND = "message.media.send"
    PERSON_CURRENT_READ = "person.current.read"
    PERSON_READ = "person.read"
    PERSON_ALIAS_READ = "person.alias.read"
    PERSON_ALIAS_WRITE = "person.alias.write"
    GROUP_CURRENT_READ = "group.current.read"
    GROUP_READ = "group.read"
    GROUP_MEMBERS_READ = "group.members.read"
    GROUP_SETTINGS_WRITE = "group.settings.write"
    MEMORY_PERSON_READ = "memory.person.read"
    MEMORY_GROUP_READ = "memory.group.read"
    MEMORY_SEARCH = "memory.search"
    MEMORY_WRITE = "memory.write"
    MEMORY_DELETE = "memory.delete"
    RELATIONSHIP_CURRENT_READ = "relationship.current.read"
    RELATIONSHIP_READ = "relationship.read"
    RELATIONSHIP_WRITE = "relationship.write"
    LLM_GENERATE = "llm.generate"
    LLM_GENERATE_WITH_CONTEXT = "llm.generate_with_context"
    AGENT_RUN = "agent.run"
    AGENT_SESSION = "agent.session"
    WEB_SEARCH = "web.search"
    WEB_READ = "web.read"
    NETWORK_HTTP_ALLOWLISTED = "network.http.allowlisted"
    NETWORK_HTTP_UNRESTRICTED = "network.http.unrestricted"
    VISION_CURRENT_READ = "vision.current.read"
    VISION_ANALYZE = "vision.analyze"
    MEDIA_CURRENT_READ = "media.current.read"
    MEDIA_ARTIFACT_CREATE = "media.artifact.create"
    EMOJI_READ = "emoji.read"
    EMOJI_COLLECT = "emoji.collect"
    EMOJI_SELECT = "emoji.select"
    EMOJI_SEND = "emoji.send"
    EMOJI_MANAGE = "emoji.manage"
    EMOJI_HOOK = "emoji.hook"
    SPEECH_PROFILE_READ = "speech.profile.read"
    SPEECH_GENERATE = "speech.generate"
    SPEECH_REPLY_EFFECT = "speech.reply_effect"
    SPEECH_SEND = "speech.send"
    SPEECH_MANAGE = "speech.manage"
    SPEECH_PROVIDER_REGISTER = "speech.provider.register"
    AUTOMATION_READ = "automation.read"
    AUTOMATION_MANAGE_SELF = "automation.manage_self"
    AUTOMATION_ACTION_REGISTER = "automation.action.register"
    PLUGIN_CONFIG_READ = "plugin.config.read"
    PLUGIN_CONFIG_WRITE = "plugin.config.write"
    RUNTIME_CONFIG_READ = "runtime.config.read"
    RUNTIME_CONFIG_WRITE = "runtime.config.write"
    ONEBOT_READ = "onebot.read"
    ONEBOT_SEND = "onebot.send"
    ONEBOT_MUTATE = "onebot.mutate"
    PROMPT_CONTEXT_REGISTER = "prompt.context.register"
    PROMPT_GUIDANCE_REGISTER = "prompt.guidance.register"
    TOOL_REGISTER = "tool.register"
    COMMAND_REGISTER = "command.register"
    EVENT_SUBSCRIBE = "event.subscribe"
    BACKGROUND_WORKER = "background.worker"
    NOTIFICATION_PUBLISH = "notification.publish"
    NOTIFICATION_AGENT = "notification.agent"
    STORAGE_PRIVATE = "storage.private"
    ADMISSION_SIGNAL_REGISTER = "admission.signal.register"
    MCP_READ = "mcp.read"
    MCP_CALL = "mcp.call"


HIGH_RISK_PERMISSIONS: frozenset[PluginPermission] = frozenset(
    {
        PluginPermission.RELATIONSHIP_WRITE,
        PluginPermission.MEMORY_DELETE,
        PluginPermission.RUNTIME_CONFIG_WRITE,
        PluginPermission.NETWORK_HTTP_UNRESTRICTED,
        PluginPermission.ONEBOT_MUTATE,
        PluginPermission.AGENT_RUN,
        PluginPermission.AGENT_SESSION,
        PluginPermission.NOTIFICATION_AGENT,
        PluginPermission.EMOJI_MANAGE,
        PluginPermission.SPEECH_MANAGE,
        PluginPermission.SPEECH_PROVIDER_REGISTER,
    }
)

RESERVED_PLUGIN_NAMESPACES: frozenset[str] = frozenset(
    {"qq_ai_bot", "qq-ai-bot", "yuki", "core", "system"}
)


def parse_permissions(values: list[str] | tuple[str, ...]) -> tuple[PluginPermission, ...]:
    """Validate, de-duplicate, and preserve manifest order."""

    result: list[PluginPermission] = []
    seen: set[PluginPermission] = set()
    for raw in values:
        try:
            permission = PluginPermission(raw)
        except ValueError as exc:
            raise ValueError(f"unknown plugin permission: {raw}") from exc
        if permission not in seen:
            seen.add(permission)
            result.append(permission)
    return tuple(result)
