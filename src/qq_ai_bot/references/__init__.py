"""Trusted, turn-frozen model references for the main chat Agent."""

from qq_ai_bot.references.epoch import ReferenceEpochManager
from qq_ai_bot.references.errors import ReferenceResolutionError
from qq_ai_bot.references.models import (
    GroupReference,
    MessageReference,
    ReferenceProvenance,
    TurnReferenceRegistry,
    UserReference,
)
from qq_ai_bot.references.registry import MainAgentHistoryProjector, MainHistoryBlock
from qq_ai_bot.references.resolver import ReferenceToolAdapter

__all__ = [
    "GroupReference",
    "MainAgentHistoryProjector",
    "MainHistoryBlock",
    "MessageReference",
    "ReferenceEpochManager",
    "ReferenceProvenance",
    "ReferenceResolutionError",
    "ReferenceToolAdapter",
    "TurnReferenceRegistry",
    "UserReference",
]
