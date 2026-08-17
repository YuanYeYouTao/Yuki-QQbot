"""Strict, provider-neutral domain models for Planner decisions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.domain.relationships import RelationshipStage
from qq_ai_bot.emoji.models import (
    EmojiIntent,
    EmojiPlacement,
    EmojiReplyMode,
    EmojiReplyPlan,
)
from qq_ai_bot.speech.models import (
    SpeechLanguageHint,
    VoiceAgentToolPolicy,
    VoiceIntent,
    VoiceMode,
    VoicePreferenceChange,
    VoicePreferenceMode,
    VoiceReplyPlan,
)


class _StrictPlannerModel(BaseModel):
    """Reject unknown fields and prevent mutation after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannerDecision(StrEnum):
    """Whether the main Agent should answer this turn."""

    REPLY = "reply"
    SILENT = "silent"
    WAIT = "wait"


class DeliveryMode(StrEnum):
    """How a later reply sequence should present the final answer."""

    SINGLE = "single"
    NATURAL_MULTI = "natural_multi"
    STRUCTURED = "structured"
    CONCISE = "concise"
    DETAILED = "detailed"


_LEFTOVER_LLM_FIELDS = (
    "memory_context",
    "tool_selection",
    "tool_mode",
    "scopes",
    "groups",
)


def _discard_leftover_llm_fields(value: Any) -> Any:
    """Drop retired Planner-owned fields that older models may still emit."""

    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    for field in _LEFTOVER_LLM_FIELDS:
        normalized.pop(field, None)
    return normalized


class PlannerReasonCode(StrEnum):
    """Stable, low-cardinality reasons suitable for metrics and audit records."""

    DIRECT_REQUEST = "direct_request"
    DIRECT_MENTION = "direct_mention"
    CONTINUATION = "continuation"
    USEFUL_CONTRIBUTION = "useful_contribution"
    EMOTIONAL_SUPPORT = "emotional_support"
    CASUAL_REACTION = "casual_reaction"
    LOW_RELEVANCE = "low_relevance"
    BOT_OVERACTIVE = "bot_overactive"
    CONVERSATION_TOO_FAST = "conversation_too_fast"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    WAIT_FOR_MORE_CONTEXT = "wait_for_more_context"
    PLANNER_FALLBACK = "planner_fallback"
    DETERMINISTIC_EFFECT_REQUEST = "deterministic_effect_request"
    PLANNER_TIMEOUT_FALLBACK = "planner_timeout_fallback"
    PLANNER_INVALID_RESPONSE_FALLBACK = "planner_invalid_response_fallback"
    PLANNER_PROVIDER_ERROR_FALLBACK = "planner_provider_error_fallback"


class PlannerMemoryContext(_StrictPlannerModel):
    """Trusted availability flags, without memory contents or identities."""

    retrieval_enabled: bool = True
    semantic_enabled: bool = False
    self_enabled: bool = False


class PlannerSignal(_StrictPlannerModel):
    """A bounded, non-authoritative relevance hint contributed by one plugin."""

    source_plugin_id: str = Field(min_length=1, max_length=128)
    score_delta: float = Field(ge=-10, le=10, strict=True)
    reason_code: str = Field(min_length=1, max_length=64)
    summary: str = Field(default="", max_length=300)
    confidence: float = Field(default=1.0, ge=0, le=1, strict=True)
    expires_at: datetime | None = None


class PlannerSpeechContext(_StrictPlannerModel):
    """Trusted speech availability without filesystem or model internals."""

    enabled: bool = Field(default=False, description="后端是否启用语音功能。")
    available: bool = Field(
        default=False,
        description="后端是否确认本轮能够合成并发送语音；这是可信运行时状态。",
    )
    default_profile: str = ""
    available_styles: tuple[str, ...] = ()
    available_languages: tuple[str, ...] = ()
    preference_mode: VoicePreferenceMode = VoicePreferenceMode.AUTO
    spontaneous_frequency: float = Field(default=0.15, ge=0, le=1, strict=True)
    recent_spontaneous_turns: int = Field(default=0, ge=0, strict=True)
    recent_spontaneous_voice_turns: int = Field(default=0, ge=0, strict=True)
    recent_spontaneous_voice_ratio: float = Field(default=0, ge=0, le=1, strict=True)
    spontaneous_allowed: bool = True


class PlannerEmojiContext(_StrictPlannerModel):
    """Trusted availability of the Planner-owned emoji reply effect."""

    enabled: bool = Field(default=False, description="后端是否启用表情回复效果。")
    available: bool = Field(
        default=False,
        description="后端是否确认本轮能够从已采用表情池选择并发送图片。",
    )
    explicit_request: bool = False
    standalone_request: bool = False
    goal: str = Field(default="", max_length=300)
    spontaneous_frequency: float = Field(default=0.15, ge=0, le=1, strict=True)
    recent_spontaneous_turns: int = Field(default=0, ge=0, strict=True)
    recent_spontaneous_emoji_turns: int = Field(default=0, ge=0, strict=True)
    recent_spontaneous_emoji_ratio: float = Field(default=0, ge=0, le=1, strict=True)
    spontaneous_allowed: bool = True


class ReplyNecessitySnapshot(_StrictPlannerModel):
    """Deterministic gate result captured before a Planner request."""

    score: int = Field(ge=0, le=100, strict=True)
    should_enter_planner: bool
    relevance_score: int = Field(ge=0, le=100, strict=True)
    content_score: int = Field(ge=-100, le=100, strict=True)
    pressure_score: int = Field(ge=0, le=100, strict=True)
    presence_penalty: int = Field(ge=0, le=100, strict=True)
    activity_penalty: int = Field(ge=0, le=100, strict=True)
    relationship_adjustment: int = Field(ge=-5, le=5, strict=True)
    plugin_adjustment: int = Field(default=0, ge=-15, le=15, strict=True)
    reasons: tuple[str, ...] = ()
    pending_message_count: int = Field(ge=0, le=100, strict=True)
    recent_bot_messages: int = Field(ge=0, strict=True)
    recent_total_messages: int = Field(ge=0, strict=True)
    average_human_interval_seconds: float = Field(ge=0, strict=True)
    idle_seconds: float = Field(ge=0, strict=True)


class PlannerInput(_StrictPlannerModel):
    """Trusted envelope plus explicitly untrusted conversation material."""

    conversation_key: str
    scope_type: ScopeType
    origin: TurnOrigin
    trigger_message_id: str
    trigger_event_id: int | None = Field(default=None, exclude=True)
    bot_user_id: str
    current_sender_user_id: str
    current_group_id: str | None = None
    history_messages: tuple[ChatMessage, ...] = Field(default=(), max_length=10)
    current_message: ChatMessage
    current_message_text: str = Field(default="", exclude=True)
    trusted_history_sender_user_ids: tuple[str, ...] = Field(default=(), exclude=True)
    trusted_history_event_ids: tuple[int, ...] = Field(default=(), exclude=True)
    reply_target_is_bot: bool = False
    mentions_bot: bool = False
    mentioned_user_ids: tuple[str, ...] = ()
    visual_input_present: bool = False
    relationship_stage: RelationshipStage | None = None
    current_time: datetime
    necessity: ReplyNecessitySnapshot
    plugin_signals: tuple[PlannerSignal, ...] = ()
    emoji: PlannerEmojiContext = PlannerEmojiContext()
    speech: PlannerSpeechContext = PlannerSpeechContext()
    memory: PlannerMemoryContext = PlannerMemoryContext()
    external_event: dict[str, Any] | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def known_target_user_ids(self) -> tuple[str, ...]:
        """Return only QQ identities that occur in the trusted current envelope."""

        known: list[str] = []
        for user_id in (
            self.current_sender_user_id,
            *self.mentioned_user_ids,
            *self.trusted_history_sender_user_ids,
        ):
            if not user_id or user_id == self.bot_user_id or user_id in known:
                continue
            known.append(user_id)
        return tuple(known)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def known_event_ids(self) -> tuple[int, ...]:
        """Return local event IDs from only the bounded current conversation input."""

        known: list[int] = []
        for event_id in (*self.trusted_history_event_ids, self.trigger_event_id):
            if event_id is not None and event_id not in known:
                known.append(event_id)
        return tuple(known)

    @property
    def current_text(self) -> str:
        """Return clean current content for deterministic backend constraints."""

        return self.current_message_text or self.current_message.content or ""


class TurnPlan(_StrictPlannerModel):
    """Validated plan; it cannot grant tools or contain a final reply body."""

    schema_version: Literal[1] = 1
    decision: PlannerDecision
    intent: str = Field(default="", max_length=300)
    target_user_ids: tuple[str, ...] = Field(default=(), max_length=5)
    delivery_mode: DeliveryMode = DeliveryMode.SINGLE
    desired_messages: int = Field(default=1, ge=1, le=20, strict=True)
    reply_to_event_id: int | None = Field(
        default=None,
        gt=0,
        strict=True,
        description=(
            "默认必须为 null。只有需要在多人对话中明确指向某条真实事件，或特意回到较早事件时"
            "才填写该事件信封中的 #EventRecord.id；普通顺接无需使用。"
        ),
    )
    wait_seconds: float = Field(default=0, ge=0, le=300, strict=True)
    confidence: float = Field(ge=0, le=1, strict=True)
    reason_code: PlannerReasonCode
    planner_note: str = ""
    emoji: EmojiReplyPlan = EmojiReplyPlan()
    voice: VoiceReplyPlan = VoiceReplyPlan()

    @model_validator(mode="before")
    @classmethod
    def _discard_leftover_tool_fields(cls, value: Any) -> Any:
        return _discard_leftover_llm_fields(value)


class PlannerEmojiOutput(_StrictPlannerModel):
    """Compact visual-effect decision; secondary presentation fields are optional."""

    intent: EmojiIntent = Field(
        description="当前用户明确要求发送表情时必须为 explicit_request，否则为 neutral。"
    )
    mode: EmojiReplyMode = Field(
        description=(
            "明确索要表情时使用 preferred 或 emoji_only；自然聊天可用 optional；"
            "不适合发表情时使用 none。"
        )
    )
    placement: EmojiPlacement | None = None
    goal: str = Field(default="", max_length=300)
    emotion: str = Field(default="", max_length=100)

    def materialize(self) -> EmojiReplyPlan:
        placement = self.placement
        if placement is None:
            placement = (
                EmojiPlacement.ONLY
                if self.mode is EmojiReplyMode.EMOJI_ONLY
                else EmojiPlacement.AFTER_TEXT
            )
        return EmojiReplyPlan(
            intent=self.intent,
            mode=self.mode,
            placement=placement,
            goal=self.goal,
            emotion=self.emotion,
        )


class PlannerVoiceOutput(_StrictPlannerModel):
    """Compact voice decision with backend-owned tool authorization defaults."""

    mode: VoiceMode = Field(
        description=(
            "当前消息明确要求发送或朗读语音且 speech.available=true 时必须为 voice 或 "
            "text_and_voice；明确不要语音时为 text。"
        )
    )
    intent: VoiceIntent = Field(
        description=(
            "当前消息明确索要语音时必须为 explicit_request，明确拒绝语音时为 "
            "explicit_opt_out，只有没有表达语音偏好时才为 neutral。"
        )
    )
    style_hint: str = Field(default="", max_length=128)
    language: SpeechLanguageHint = SpeechLanguageHint.AUTO
    preference_change: VoicePreferenceChange | None = None

    def materialize(self) -> VoiceReplyPlan:
        return VoiceReplyPlan(
            mode=self.mode,
            intent=self.intent,
            agent_tool=(
                VoiceAgentToolPolicy.REQUIRED
                if self.intent is VoiceIntent.EXPLICIT_REQUEST
                else VoiceAgentToolPolicy.FORBIDDEN
            ),
            style_hint=self.style_hint,
            language=self.language,
            preference_change=self.preference_change,
        )


class PlannerModelOutput(_StrictPlannerModel):
    """Sparse provider response materialized into one strict :class:`TurnPlan`.

    The model must still classify behavior that cannot be inferred safely by
    the backend: delivery, emoji, and voice. Capability Runtime owns tool
    exposure, so leftover ``tool_selection`` / ``tool_mode`` fields are dropped
    before validation. Secondary details use backend defaults, keeping
    completions small without spreading nullable values through the domain.
    """

    decision: PlannerDecision
    confidence: float = Field(ge=0, le=1, strict=True)
    reason_code: PlannerReasonCode
    intent: str = Field(default="", max_length=300)
    delivery_mode: DeliveryMode = Field(
        description="选择正文发送形态；用户要求多条或自然聊天适合拆分时使用 natural_multi。"
    )
    reply_to_event_id: int | None = Field(default=None, gt=0, strict=True)
    wait_seconds: float = Field(default=0, ge=0, le=300, strict=True)
    emoji: PlannerEmojiOutput
    voice: PlannerVoiceOutput

    @model_validator(mode="before")
    @classmethod
    def _discard_legacy_derived_fields(cls, value: Any) -> Any:
        normalized = _discard_leftover_llm_fields(value)
        if not isinstance(normalized, dict):
            return normalized
        normalized.pop("target_user_ids", None)
        normalized.pop("desired_messages", None)
        return normalized

    def materialize(self) -> TurnPlan:
        """Fill omitted provider fields from trusted backend defaults."""

        return TurnPlan(
            decision=self.decision,
            intent=self.intent,
            delivery_mode=self.delivery_mode,
            reply_to_event_id=self.reply_to_event_id,
            wait_seconds=self.wait_seconds,
            confidence=self.confidence,
            reason_code=self.reason_code,
            emoji=self.emoji.materialize(),
            voice=self.voice.materialize(),
        )


class PlannedTurn(_StrictPlannerModel):
    """Planner result metadata passed to later orchestration without hidden reasoning."""

    plan: TurnPlan
    necessity: ReplyNecessitySnapshot
    planner_model: str
    planner_latency_seconds: float = Field(ge=0, strict=True)
    planner_used: bool
    fallback_used: bool
    turn_version: int = Field(ge=0, strict=True)
