"""Immutable scope and turn-generation contracts."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.domain.conversations import ConversationScope


@dataclass(frozen=True, slots=True)
class ConversationTurnSnapshot:
    """Database and in-process versions captured when a turn is admitted."""

    scope_id: int
    scope_key: str
    generation: int
    trigger_event_id: int
    coordinator_version: int

    def __post_init__(self) -> None:
        if self.scope_id < 1:
            raise ValueError("scope_id must be positive")
        if not self.scope_key:
            raise ValueError("scope_key must not be empty")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.trigger_event_id < 1:
            raise ValueError("trigger_event_id must be positive")
        if self.coordinator_version < 1:
            raise ValueError("coordinator_version must be positive")


__all__ = ["ConversationScope", "ConversationTurnSnapshot"]
