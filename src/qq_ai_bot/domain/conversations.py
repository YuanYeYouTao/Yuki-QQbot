"""Conversation scope identity and isolation rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ScopeType(StrEnum):
    """Supported conversation scopes."""

    PRIVATE = "private"
    GROUP = "group"


@dataclass(frozen=True, slots=True)
class ConversationScope:
    """Bot-aware identity for one current short-term conversation.

    A group scope deliberately contains no actor identifier. The current
    speaker is turn data, while the conversation belongs to the bot and group.
    """

    bot_user_id: str
    scope_type: ScopeType
    key: str
    private_peer_user_id: str | None = None
    group_id: str | None = None

    def __post_init__(self) -> None:
        bot_user_id = self.bot_user_id.strip()
        private_peer_user_id = (
            self.private_peer_user_id.strip() if self.private_peer_user_id is not None else None
        )
        group_id = self.group_id.strip() if self.group_id is not None else None
        if not bot_user_id:
            raise ValueError("conversation scope requires bot_user_id")
        if self.scope_type is ScopeType.PRIVATE:
            if not private_peer_user_id or group_id is not None:
                raise ValueError("private scope requires only private_peer_user_id")
            expected = f"bot:{bot_user_id}:private:{private_peer_user_id}"
        else:
            if not group_id or private_peer_user_id is not None:
                raise ValueError("group scope requires only group_id")
            expected = f"bot:{bot_user_id}:group:{group_id}"
        if self.key != expected:
            raise ValueError("conversation scope key does not match its identity")
        object.__setattr__(self, "bot_user_id", bot_user_id)
        object.__setattr__(self, "private_peer_user_id", private_peer_user_id)
        object.__setattr__(self, "group_id", group_id)

    @classmethod
    def private(cls, bot_user_id: str, peer_user_id: str) -> ConversationScope:
        """Create a bot-aware private conversation scope."""

        bot = bot_user_id.strip()
        peer = peer_user_id.strip()
        return cls(
            bot_user_id=bot,
            scope_type=ScopeType.PRIVATE,
            key=f"bot:{bot}:private:{peer}",
            private_peer_user_id=peer,
        )

    @classmethod
    def group(
        cls,
        bot_user_id: str,
        group_id: str,
    ) -> ConversationScope:
        """Create the single conversation scope shared by all actors in a group."""

        bot = bot_user_id.strip()
        group = group_id.strip()
        return cls(
            bot_user_id=bot,
            scope_type=ScopeType.GROUP,
            key=f"bot:{bot}:group:{group}",
            group_id=group,
        )
