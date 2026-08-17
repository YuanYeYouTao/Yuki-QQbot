"""Compatibility re-export; R4 owns scoring in conversation.participation."""

from qq_ai_bot.conversation.participation import (
    AdmissionFeatures as ReplyNecessityFeatures,
    AdmissionScoreSnapshot as ReplyNecessitySnapshot,
    LocalAutonomousParticipationPolicy as ReplyNecessityScorer,
)

__all__ = [
    "ReplyNecessityFeatures",
    "ReplyNecessityScorer",
    "ReplyNecessitySnapshot",
]
