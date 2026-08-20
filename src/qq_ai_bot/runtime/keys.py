"""Strongly typed, deliberately separate runtime key families."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.runtime.errors import InvalidTurnContextError


@dataclass(frozen=True, slots=True)
class TurnCoordinationKey:
    """Partition key for the conversation turn coordinator.

    Its string value is exactly the bot-aware ``ConversationScope.key``.
    """

    bot_user_id: str
    scope_type: ScopeType
    scope_id: str

    def __post_init__(self) -> None:
        if not self.bot_user_id or not self.scope_id:
            raise InvalidTurnContextError("coordination key requires bot and scope ids")

    @classmethod
    def from_scope(cls, scope: ConversationScope) -> TurnCoordinationKey:
        return cls(
            bot_user_id=scope.bot_user_id,
            scope_type=scope.scope_type,
            scope_id=scope.group_id or scope.private_peer_user_id or "",
        )

    @classmethod
    def for_group(cls, bot_user_id: str, group_id: str) -> TurnCoordinationKey:
        return cls(bot_user_id=bot_user_id, scope_type=ScopeType.GROUP, scope_id=group_id)

    @classmethod
    def for_private(cls, bot_user_id: str, user_id: str) -> TurnCoordinationKey:
        return cls(bot_user_id=bot_user_id, scope_type=ScopeType.PRIVATE, scope_id=user_id)

    @classmethod
    def from_inbound(cls, message: InboundMessage) -> TurnCoordinationKey:
        return cls.from_scope(message.scope())

    @property
    def partition_key(self) -> str:
        if self.scope_type is ScopeType.GROUP:
            return f"bot:{self.bot_user_id}:group:{self.scope_id}"
        return f"bot:{self.bot_user_id}:private:{self.scope_id}"


@dataclass(frozen=True, slots=True)
class ResolvedMemoryScope:
    """Memory partition resolved by the host for one turn.

    Resolution happens in the memory runtime resolver. This type intentionally
    remains incompatible with bot-aware conversation/coordination keys.
    """

    scope_type: ScopeType
    scope_id: str

    def __post_init__(self) -> None:
        if not self.scope_id:
            raise InvalidTurnContextError("memory scope requires a scope id")

    @classmethod
    def for_group(cls, group_id: str) -> ResolvedMemoryScope:
        return cls(scope_type=ScopeType.GROUP, scope_id=group_id)

    @classmethod
    def for_private(cls, user_id: str) -> ResolvedMemoryScope:
        return cls(scope_type=ScopeType.PRIVATE, scope_id=user_id)

    @property
    def partition_key(self) -> str:
        if self.scope_type is ScopeType.GROUP:
            return f"group:{self.scope_id}"
        return f"private:{self.scope_id}"
