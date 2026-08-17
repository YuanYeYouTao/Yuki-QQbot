"""Stable, dependency-light value objects shared by Yuki plugins."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class StrictModel(BaseModel):
    """Base for public SDK payloads; unknown fields are never silently accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PermissionLevel(StrEnum):
    USER = "user"
    TRUSTED = "trusted"
    MODERATOR = "moderator"
    SUPERUSER = "superuser"


class RiskClass(StrEnum):
    READ = "read"
    GENERATE = "generate"
    SEND = "send"
    MUTATE = "mutate"
    DESTRUCTIVE = "destructive"


class RetryPolicy(StrEnum):
    NONE = "none"
    TRANSIENT_ONCE = "transient_once"


class RestartPolicy(StrEnum):
    NEVER = "never"
    ON_FAILURE = "on_failure"


class TurnOrigin(StrEnum):
    USER_MESSAGE = "user_message"
    AUTONOMOUS_GROUP = "autonomous_group"
    SCHEDULED_AUTOMATION = "scheduled_automation"
    SYSTEM_TASK = "system_task"
    PLUGIN_SESSION = "plugin_session"
    PLUGIN_BACKGROUND = "plugin_background"


class PromptStage(StrEnum):
    CORE_IDENTITY = "core_identity"
    CORE_SECURITY = "core_security"
    CORE_BEHAVIOR = "core_behavior"
    TRUSTED_TIME = "trusted_time"
    TRUSTED_AUTHORITY = "trusted_authority"
    RELATIONSHIP = "relationship"
    SCENE = "scene"
    MEMORY = "memory"
    VISUAL_CONTEXT = "visual_context"
    WEB_POLICY = "web_policy"
    PLUGIN_CONTEXT = "plugin_context"
    TOOL_GUIDANCE = "tool_guidance"
    FINAL_CONSTRAINTS = "final_constraints"


class PromptTarget(StrEnum):
    AGENT = "agent"
    PLUGIN_SESSION = "plugin_session"


class TrustedLevel(StrEnum):
    CORE = "core"
    HOST = "host"
    PLUGIN_UNTRUSTED = "plugin_untrusted"


class PromptFragment(StrictModel):
    """One bounded prompt contribution; third-party text remains untrusted."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    plugin_id: str | None = None
    stage: PromptStage
    priority: int = Field(default=0, ge=-10_000, le=10_000)
    content: str = Field(min_length=1, max_length=16_000)
    trusted_level: TrustedLevel = TrustedLevel.PLUGIN_UNTRUSTED
    max_characters: int = Field(default=2_000, ge=1, le=16_000)
    target: PromptTarget = PromptTarget.AGENT
    source: str = Field(default="plugin", min_length=1, max_length=128)
    cache_key: str | None = Field(default=None, max_length=256)


class AdmissionSignal(StrictModel):
    source_plugin_id: str
    score_delta: int = Field(ge=-10, le=10)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)
    expires_at: datetime | None = None


class CurrentMessage(StrictModel):
    """Sanitized current-event projection, never a raw NoneBot event."""

    message_id: str = Field(min_length=1, max_length=128)
    sender_user_id: str = Field(min_length=1, max_length=64)
    scope_type: str = Field(pattern=r"^(private|group)$")
    group_id: str | None = Field(default=None, max_length=64)
    text: str = Field(default="", max_length=12_000)
    mentioned_user_ids: tuple[str, ...] = Field(default=(), max_length=20)
    received_at: datetime


class AdmissionSignalContext(StrictModel):
    """Current trusted envelope plus untrusted message text for one signal callback."""

    conversation_key: str = Field(min_length=1, max_length=256)
    origin: TurnOrigin
    current: CurrentMessage
    text_is_untrusted: bool = True


class EmojiSelectionCandidate(StrictModel):
    emoji_id: str = Field(min_length=8, max_length=64)
    description: str = Field(default="", max_length=500)
    emotion_tags: tuple[str, ...] = Field(default=(), max_length=30)
    usage_scenarios: tuple[str, ...] = Field(default=(), max_length=30)
    base_score: float


class EmojiSelectionSignalContext(StrictModel):
    goal: str = Field(default="", max_length=300)
    emotion: str = Field(default="", max_length=100)
    group_id: str | None = Field(default=None, max_length=64)
    candidates: tuple[EmojiSelectionCandidate, ...] = Field(min_length=1, max_length=100)
    text_is_untrusted: bool = True


class EmojiSelectionSignal(StrictModel):
    candidate_id: str = Field(min_length=8, max_length=64)
    score_delta: float = Field(ge=-10, le=10)
    reason: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)


class PluginResourceLimits(StrictModel):
    background_tasks: int = Field(default=0, ge=0, le=64)
    http_concurrency: int = Field(default=1, ge=1, le=64)
    storage_mb: int = Field(default=10, ge=1, le=10_240)
    prompt_characters: int = Field(default=2_000, ge=0, le=16_000)


class GeneratedSpeechHandle(StrictModel):
    """Opaque Host-owned speech result; it never exposes a local path."""

    handle_id: str = Field(min_length=1, max_length=128)
    generation_id: int = Field(ge=1)
    profile_id: str = Field(min_length=1, max_length=128)
    duration_milliseconds: int = Field(ge=0)
    expires_at: datetime | None = None


class NotificationTarget(StrictModel):
    target_type: Literal["group", "private"]
    target_id: str = Field(min_length=1, max_length=64)


class PublishNotificationRequest(StrictModel):
    event_key: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=128)
    external_source: str = Field(min_length=1, max_length=64)
    target: NotificationTarget
    occurred_at: datetime
    summary: str = Field(min_length=1, max_length=4_000)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    text: str = Field(default="", max_length=12_000)
    media_handles: tuple[str, ...] = Field(default=(), max_length=4)
    ask_agent: bool = False
    agent_intent: str = Field(default="", max_length=1_000)


class NotificationPublishReceipt(StrictModel):
    notification_id: str = Field(min_length=1, max_length=64)
    source_event_id: int = Field(ge=1)
    event_created: bool
    delivery_enqueued: bool
    agent_turn_enqueued: bool
    deduplicated: bool


class BackgroundTargetGrantView(StrictModel):
    target_type: Literal["group", "private"]
    target_id: str = Field(min_length=1, max_length=64)
    bot_user_id: str = Field(min_length=1, max_length=64)
    enabled: bool
    created_by_user_id: str = Field(min_length=1, max_length=64)


class MediaArtifactHandle(StrictModel):
    handle_id: str = Field(min_length=1, max_length=128)
    content_type: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: datetime
