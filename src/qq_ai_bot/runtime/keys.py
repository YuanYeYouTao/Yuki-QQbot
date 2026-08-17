"""Strongly typed identity keys used by the runtime layer.

Three distinct key families exist in 3.5.3 and must never be interchanged:

- conversation history identity: ``group:{g}:user:{u}`` / ``private:{u}``
  (owned by :class:`qq_ai_bot.domain.conversations.ConversationIdentity`);
- turn coordination partition: ``group:{g}`` / ``private:{u}``
  (owned by :class:`TurnCoordinationKey`, matching
  ``ConversationTurnCoordinator.key_for``);
- memory partition: ``group:{g}`` / ``private:{u}``
  (owned by :class:`ResolvedMemoryScope`, matching the memory worker).

Coordination and memory partitions share the same string shape today but are
separate types on purpose: they answer different questions (who may run right
now vs. where facts live) and may diverge later.
"""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.runtime.errors import InvalidTurnContextError


@dataclass(frozen=True, slots=True)
class TurnCoordinationKey:
    """Partition key for the conversation turn coordinator.

    One coordinator slot exists per group (all members share it) and one per
    private chat.  Use the factories; the constructor validates shape only.
    """

    scope_type: ScopeType
    scope_id: str

    def __post_init__(self) -> None:
        if not self.scope_id:
            raise InvalidTurnContextError("coordination key requires a scope id")

    @classmethod
    def for_group(cls, group_id: str) -> TurnCoordinationKey:
        return cls(scope_type=ScopeType.GROUP, scope_id=group_id)

    @classmethod
    def for_private(cls, user_id: str) -> TurnCoordinationKey:
        return cls(scope_type=ScopeType.PRIVATE, scope_id=user_id)

    @classmethod
    def from_inbound(cls, message: InboundMessage) -> TurnCoordinationKey:
        """Mirror ``ConversationTurnCoordinator.key_for`` semantics."""

        if message.group_id is not None:
            return cls.for_group(message.group_id)
        return cls.for_private(message.sender.user_id)

    @property
    def partition_key(self) -> str:
        if self.scope_type is ScopeType.GROUP:
            return f"group:{self.scope_id}"
        return f"private:{self.scope_id}"


@dataclass(frozen=True, slots=True)
class ResolvedMemoryScope:
    """Memory partition resolved by the host for one turn.

    ``partition_key`` matches the memory worker partitions (``group:{g}`` /
    ``private:{u}``).  Resolution happens in the memory runtime resolver; this
    type only carries the trusted result.
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
