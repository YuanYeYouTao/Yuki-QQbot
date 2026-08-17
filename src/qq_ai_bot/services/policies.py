"""Pure permission, trigger, and command parsing policies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationMode, ScopeType
from qq_ai_bot.domain.messages import InboundMessage


class CommandName(StrEnum):
    """Supported `/ai` commands."""

    HELP = "help"
    NEW = "new"
    STATUS = "status"
    STOP = "stop"
    ON = "on"
    OFF = "off"
    PING = "ping"
    WHOAMI = "whoami"
    FORGETME = "forgetme"
    PRIVATE = "private"
    GROUP = "group"
    MEMORY = "memory"
    PREFERENCE = "preference"
    AFFECTION = "affection"
    CAPABILITIES = "capabilities"
    CONFIG = "config"
    AUTOMATION = "automation"
    PLUGIN = "plugin"
    EMOJI = "emoji"
    VOICE = "voice"
    MODEL = "model"
    MCP = "mcp"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Result of deciding whether and how to handle a message."""

    should_respond: bool
    content: str = ""
    command: CommandName | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class EffectiveGroupPolicy:
    """Effective group state after database overrides."""

    enabled: bool
    require_mention: bool = True
    conversation_mode: ConversationMode = ConversationMode.PER_USER
    autonomous_enabled: bool = True


@dataclass(frozen=True, slots=True)
class EffectivePrivatePolicy:
    """Effective private-chat state after a database override."""

    enabled: bool


def _command_and_content(text: str, ai_prefix: str) -> tuple[CommandName | None, str, bool]:
    stripped = text.strip()
    lower = stripped.casefold()
    triggered = False
    remainder = stripped
    if lower == "/ai" or lower.startswith("/ai "):
        triggered = True
        remainder = stripped[3:].strip()
        if not remainder:
            return CommandName.HELP, "", True
        command_parts = remainder.split(maxsplit=1)
        first = command_parts[0].casefold()
        argument = command_parts[1].strip() if len(command_parts) > 1 else ""
        try:
            return CommandName(first), argument, True
        except ValueError:
            return None, remainder, True
    if ai_prefix and (stripped == ai_prefix or stripped.startswith(f"{ai_prefix} ")):
        triggered = True
        remainder = stripped[len(ai_prefix) :].strip()
    return None, remainder, triggered


def replies_to_bot(message: InboundMessage) -> bool:
    """Return whether this inbound message is a platform reply to Yuki."""

    return (
        bool(message.reply_sender_user_id) and message.reply_sender_user_id == message.bot_user_id
    )


def evaluate_message(
    message: InboundMessage,
    settings: Settings,
    *,
    group_policy: EffectiveGroupPolicy | None = None,
    private_policy: EffectivePrivatePolicy | None = None,
    direct_triggered: bool = False,
) -> PolicyDecision:
    """Apply self/bot, allowlist, group, mention, reply-to-bot, prefix, and command rules."""

    if message.is_self_message or message.sender.is_bot:
        return PolicyDecision(False, reason="bot_message")

    command, content, prefix_triggered = _command_and_content(message.text, settings.ai_prefix)
    is_superuser = message.sender.user_id in settings.superusers

    if message.scope_type is ScopeType.PRIVATE:
        private_policy_effective = private_policy or EffectivePrivatePolicy(True)
        if not is_superuser and not private_policy_effective.enabled:
            return PolicyDecision(False, reason="private_not_allowed")
        return PolicyDecision(True, content=content, command=command, reason="private_allowed")

    if message.group_id is None:
        return PolicyDecision(False, reason="missing_group_id")
    group_policy_effective = group_policy or EffectiveGroupPolicy(
        message.group_id in settings.enabled_groups
    )

    if not group_policy_effective.enabled:
        is_enable_command = command is CommandName.ON or (
            command is CommandName.GROUP and content.casefold().endswith(" on")
        )
        if is_superuser and is_enable_command:
            return PolicyDecision(
                True,
                content=content,
                command=command,
                reason="superuser_group_enable",
            )
        return PolicyDecision(False, reason="group_disabled")
    reply_to_bot = replies_to_bot(message)
    if message.mentions_bot or prefix_triggered or direct_triggered or reply_to_bot:
        reason = "group_triggered"
        if reply_to_bot and not (message.mentions_bot or prefix_triggered or direct_triggered):
            reason = "group_reply_to_bot"
        return PolicyDecision(
            True,
            content=content,
            command=command,
            reason=reason,
        )
    return PolicyDecision(False, reason="group_not_triggered")


def command_requires_superuser(command: CommandName) -> bool:
    """Return whether a command mutates group-wide state."""

    return command in {
        CommandName.ON,
        CommandName.OFF,
        CommandName.PRIVATE,
        CommandName.GROUP,
        CommandName.CONFIG,
        CommandName.EMOJI,
        CommandName.MODEL,
    }
