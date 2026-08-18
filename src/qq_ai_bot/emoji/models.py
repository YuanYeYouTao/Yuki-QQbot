"""Strict domain models for the persistent emoji system."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qq_ai_bot.domain.messages import OutboundMessage


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EmojiLifecycleStatus(StrEnum):
    CANDIDATE = "candidate"
    RECOGNIZED = "recognized"
    ADOPTED = "adopted"
    REJECTED = "rejected"
    BANNED = "banned"
    MISSING = "missing"


class EmojiCollectionMode(StrEnum):
    METADATA_ONLY = "metadata_only"
    LIKELY = "likely"
    ALL_IMAGES = "all_images"


class EmojiReplyMode(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    PREFERRED = "preferred"
    EMOJI_ONLY = "emoji_only"


class EmojiPlacement(StrEnum):
    BEFORE_TEXT = "before_text"
    AFTER_TEXT = "after_text"
    ONLY = "only"


class EmojiAnalysis(_FrozenModel):
    """Normalized output derived from the shared VisionProvider response."""

    is_emoji: bool
    description: str = Field(min_length=1, max_length=2000)
    emotion_tags: tuple[str, ...] = Field(default=(), max_length=20)
    usage_scenarios: tuple[str, ...] = Field(default=(), max_length=20)
    ocr_text: str = Field(default="", max_length=2000)
    intensity: float = Field(default=0.5, ge=0, le=1, strict=True)
    confidence: float = Field(ge=0, le=1, strict=True)
    animated: bool
    analysis_version: str = Field(min_length=1, max_length=64)


class EmojiAsset(_FrozenModel):
    """A persisted emoji asset without local absolute paths or image bytes."""

    id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    perceptual_hash: str | None = None
    relative_path: str
    preview_relative_path: str | None = None
    image_format: str
    mime_type: str
    byte_size: int = Field(ge=0, strict=True)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    frame_count: int = Field(gt=0, strict=True)
    animated: bool
    status: EmojiLifecycleStatus
    description: str = ""
    emotion_tags: tuple[str, ...] = ()
    usage_scenarios: tuple[str, ...] = ()
    ocr_text: str = ""
    intensity: float = Field(default=0.5, ge=0, le=1, strict=True)
    confidence: float = Field(default=0, ge=0, le=1, strict=True)
    analysis_version: str = ""
    pinned: bool = False
    seen_count: int = Field(default=1, ge=1, strict=True)
    use_count: int = Field(default=0, ge=0, strict=True)
    source_event_id: int | None = None
    first_seen_user_id: str | None = None
    first_seen_group_id: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    last_used_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EmojiScopeState(_FrozenModel):
    emoji_id: str
    scope_type: Literal["global", "group"]
    scope_id: str = ""
    enabled: bool = True
    weight: float = Field(default=1.0, ge=0, strict=True)
    adopted_at: datetime
    updated_at: datetime


class EmojiSelectionRequest(_FrozenModel):
    actor_user_id: str
    group_id: str | None = None
    reply_text: str = Field(default="", max_length=4000)
    goal: str = Field(default="", max_length=300)
    emotion: str = Field(default="", max_length=100)
    explicit_request: bool = False
    mode: EmojiReplyMode = EmojiReplyMode.OPTIONAL
    placement: EmojiPlacement = EmojiPlacement.AFTER_TEXT


class EmojiSelectionResult(_FrozenModel):
    emoji_id: str | None = None
    score: float = 0
    reason: str = ""
    selected_by: Literal["none", "coarse", "vision"] = "none"


class EmojiPreparationStatus(StrEnum):
    """Stable outcomes at the optional reply-effect infrastructure boundary."""

    READY = "ready"
    NO_CANDIDATE = "no_candidate"
    REPOSITORY_UNAVAILABLE = "repository_unavailable"
    ASSET_MISSING = "asset_missing"
    STORAGE_MISSING = "storage_missing"
    UNEXPECTED_FAILURE = "unexpected_failure"


class EmojiPreparationResult(_FrozenModel):
    """A prepared emoji message or one sanitized, recoverable failure."""

    status: EmojiPreparationStatus
    message: OutboundMessage | None = None
    emoji_id: str | None = None
    reason_code: str = Field(min_length=1, max_length=64)
    retryable: bool = False

    @model_validator(mode="after")
    def _validate_ready_payload(self) -> EmojiPreparationResult:
        if self.status is EmojiPreparationStatus.READY:
            if self.message is None or not self.emoji_id:
                raise ValueError("ready emoji preparation requires message and emoji_id")
        elif self.message is not None or self.emoji_id is not None:
            raise ValueError("failed emoji preparation cannot contain media")
        return self


class EmojiIntent(StrEnum):
    """Whether the current user explicitly requested an emoji reply effect."""

    NEUTRAL = "neutral"
    EXPLICIT_REQUEST = "explicit_request"


class EmojiReplyPlan(_FrozenModel):
    """Reply-effect behavioural intent; it never contains an asset identifier."""

    # The LLM boundary model accepts JSON enum strings. Other persisted/runtime
    # emoji models remain strict.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    intent: EmojiIntent = Field(
        default=EmojiIntent.NEUTRAL,
        description="当前消息明确索要表情时为 explicit_request，否则为 neutral。",
    )
    mode: EmojiReplyMode = Field(
        default=EmojiReplyMode.NONE,
        description=(
            "用户明确要求发送表情时必须使用 preferred 或 emoji_only；只有没有表情意图时才用 none。"
        ),
    )
    placement: EmojiPlacement = Field(
        default=EmojiPlacement.AFTER_TEXT,
        description="表情相对正文的位置；emoji_only 使用 only。",
    )
    goal: str = Field(default="", max_length=300, description="希望表情表达的聊天语义。")
    emotion: str = Field(default="", max_length=100, description="希望表情表达的情绪。")

    @property
    def is_exclusive(self) -> bool:
        """Return whether the emoji is the complete user-visible reply."""

        return self.mode is EmojiReplyMode.EMOJI_ONLY or self.placement is EmojiPlacement.ONLY


class PendingReplyEffect(_FrozenModel):
    """A queued user-visible effect created by the Agent tool, plugin, or automation."""

    kind: Literal["emoji"] = "emoji"
    mode: EmojiReplyMode
    placement: EmojiPlacement
    goal: str = Field(default="", max_length=300)
    emotion: str = Field(default="", max_length=100)
    explicit_request: bool = False
    source: Literal["planner", "agent", "plugin", "automation"]


class StoredEmojiMedia(_FrozenModel):
    """Result of atomically persisting one original and its static preview."""

    sha256: str
    relative_path: str
    preview_relative_path: str
    image_format: str
    mime_type: str
    byte_size: int = Field(gt=0, strict=True)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    frame_count: int = Field(gt=0, strict=True)
    animated: bool
    perceptual_hash: str | None = None
