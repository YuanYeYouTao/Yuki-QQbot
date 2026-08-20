"""Stable repository imports grouped behind domain-specific implementations."""

from qq_ai_bot.persistence.event_repository import (
    AgentActionRepository,
    EventLedgerRepository,
    ProcessedEventRepository,
)
from qq_ai_bot.persistence.media_repository import (
    EmojiDescriptionRepository,
    MediaAnalysisRepository,
)
from qq_ai_bot.persistence.people_repository import (
    GroupSettingsRepository,
    PeopleRepository,
    PrivateUserSettingsRepository,
    UserProfileRepository,
)
from qq_ai_bot.persistence.relationship_repository import (
    RelationshipJobRepository,
    RelationshipRepository,
)
from qq_ai_bot.persistence.repository_records import (
    EmojiDescriptionRecord,
    EventRecord,
    GroupSetting,
    MediaAnalysisRecord,
    PrivateUserSetting,
    RelationshipEventRecord,
    RelationshipJobRecord,
)
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository

__all__ = [
    "AgentActionRepository",
    "EmojiDescriptionRecord",
    "EmojiDescriptionRepository",
    "EventLedgerRepository",
    "EventRecord",
    "GroupSetting",
    "GroupSettingsRepository",
    "MediaAnalysisRecord",
    "MediaAnalysisRepository",
    "PeopleRepository",
    "PrivateUserSetting",
    "PrivateUserSettingsRepository",
    "ProcessedEventRepository",
    "RelationshipEventRecord",
    "RelationshipJobRecord",
    "RelationshipJobRepository",
    "RelationshipRepository",
    "UserProfileRepository",
    "WebSearchSourceRepository",
]
