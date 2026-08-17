"""Strict, provider-neutral domain models for Planner decisions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

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
from qq_ai_bot.memory.enums import (
    MemoryAccessMode,
    MemoryContextMode,
    MemoryKind,
    MemoryRecallPurpose,
    MemorySubjectRole,
)
from qq_ai_bot.memory.models import MemoryQueryIntent, MemoryTemporalIntent
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


class ToolMode(StrEnum):
    """A monotonic restriction over capabilities granted by the backend."""

    INHERIT = "inherit"
    NONE = "none"
    READ_ONLY = "read_only"


class ToolGroup(StrEnum):
    """Coarse capability groups that Planner may retain for this turn."""

    WEB = "web"
    MEMORY = "memory"
    RELATIONSHIP = "relationship"
    ADMIN = "admin"
    CONFIG = "config"
    AUTOMATION = "automation"
    ONEBOT = "onebot"
    EMOJI = "emoji"
    SPEECH = "speech"
    PLUGIN = "plugin"
    CAPABILITY = "capability"


class ToolSelection(_StrictPlannerModel):
    """A monotonic tool mode plus dynamic backend-advertised scopes.

    ``groups`` remains an accepted legacy spelling for 2.0 Planner responses.
    New providers should emit ``scopes``.
    """

    mode: ToolMode = ToolMode.INHERIT
    scopes: tuple[str, ...] = ()
    groups: tuple[ToolGroup, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _merge_legacy_groups(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not value.get("groups"):
            return value
        normalized = dict(value)
        scopes = [str(item) for item in normalized.get("scopes", ())]
        for group in normalized.get("groups", ()):
            group_id = group.value if isinstance(group, ToolGroup) else str(group)
            if group_id not in scopes:
                scopes.append(group_id)
        normalized["scopes"] = scopes
        normalized["groups"] = []
        return normalized

    @property
    def scope_ids(self) -> tuple[str, ...]:
        return self.scopes


class ToolScopeSummary(_StrictPlannerModel):
    """Compact Planner-visible scope metadata; never contains JSON Schemas."""

    scope_id: str = Field(min_length=1, max_length=128)
    parent: str | None = Field(default=None, max_length=128)
    display_name: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=300)
    tool_count: int = Field(ge=0, strict=True)
    provider_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


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


class MemoryContextReasonCode(StrEnum):
    """Why Planner selected a memory retrieval depth for this turn."""

    DEFAULT = "default"
    EFFECT_ONLY = "effect_only"
    CASUAL_REPLY = "casual_reply"
    ROUTINE_CONTEXT = "routine_context"
    MEMORY_RECALL = "memory_recall"
    PERSON_REFERENCE = "person_reference"
    GROUP_REFERENCE = "group_reference"
    EXPLICIT_OVERVIEW = "explicit_overview"
    SELF_MEMORY_RECALL = "self_memory_recall"
    SELF_REFERENCE = "self_reference"
    SELF_OVERVIEW = "self_overview"


class MemoryContextPlan(_StrictPlannerModel):
    """Semantic intent only; identity targets remain backend-owned."""

    access: MemoryAccessMode = MemoryAccessMode.AUTOMATIC
    mode: MemoryContextMode = MemoryContextMode.LEXICAL
    purpose: MemoryRecallPurpose = MemoryRecallPurpose.BACKGROUND
    subjects: tuple[MemorySubjectRole, ...] = Field(default=(), max_length=4)
    entities: tuple[str, ...] = Field(default=(), max_length=5)
    temporal: MemoryTemporalIntent = Field(
        default_factory=MemoryTemporalIntent,
        description=(
            "可信时间意图；明确要求范围外不要使用时输出绝对 range 并设置 constraint=strict。"
        ),
    )
    preferred_kinds: tuple[MemoryKind, ...] = Field(default=(), max_length=3)
    requested_count: int | None = Field(default=None, ge=1, le=20)
    reason_code: MemoryContextReasonCode = MemoryContextReasonCode.DEFAULT
    self_recall: bool = False

    @model_validator(mode="after")
    def _validate_access_mode(self) -> MemoryContextPlan:
        if self.access is MemoryAccessMode.AUTOMATIC:
            if self.mode is MemoryContextMode.NONE:
                raise ValueError("automatic memory access requires a retrieval mode")
        elif self.mode is not MemoryContextMode.NONE:
            raise ValueError("none/tool/mutation memory access requires mode=none")
        if MemorySubjectRole.CURRENT_SELF in self.subjects and not self.self_recall:
            raise ValueError("current_self requires self_recall=true")
        return self

    def to_query_intent(self) -> MemoryQueryIntent:
        subjects = self.subjects
        if self.self_recall and MemorySubjectRole.CURRENT_SELF not in subjects:
            subjects = (*subjects, MemorySubjectRole.CURRENT_SELF)
        return MemoryQueryIntent(
            mode=self.mode,
            purpose=self.purpose,
            subjects=subjects,
            entities=self.entities,
            temporal=self.temporal,
            preferred_kinds=self.preferred_kinds,
        )


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
    available_tool_categories: tuple[str, ...] = ()
    available_tool_scopes: tuple[ToolScopeSummary, ...] = ()
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
    tool_selection: ToolSelection = ToolSelection()
    wait_seconds: float = Field(default=0, ge=0, le=300, strict=True)
    confidence: float = Field(ge=0, le=1, strict=True)
    reason_code: PlannerReasonCode
    planner_note: str = ""
    memory_context: MemoryContextPlan = MemoryContextPlan()
    emoji: EmojiReplyPlan = EmojiReplyPlan()
    voice: VoiceReplyPlan = VoiceReplyPlan()

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_tool_mode(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "tool_mode" not in value:
            return value
        normalized = dict(value)
        legacy = normalized.pop("tool_mode")
        if "tool_selection" not in normalized:
            normalized["tool_selection"] = {
                "mode": legacy,
                # An empty scope list means all backend-approved scopes.  This
                # preserves the 2.0 ``tool_mode`` contract without fabricating
                # names that may not exist in a dynamic provider catalog.
                "scopes": [],
            }
        return normalized

    @property
    def tool_mode(self) -> ToolMode:
        """Source-compatible projection for 1.8 integrations."""

        return self.tool_selection.mode

    @property
    def tool_selection_explicit(self) -> bool:
        """Whether the provider explicitly narrowed tool scopes for this turn."""

        return "tool_selection" in self.model_fields_set


class PlannerToolOutput(_StrictPlannerModel):
    """Sparse model-facing tool choice that must be stated explicitly."""

    mode: ToolMode = Field(
        description=(
            "当前请求需要调用任何工具时使用 inherit，明确只允许读取时使用 read_only；"
            "只有完全不需要工具时才使用 none。"
        )
    )
    scopes: tuple[str, ...] = Field(
        description=(
            "从 capabilities.tool_scopes 中选择完成当前请求所需的最小 scope 集合；"
            "明确需要工具时不得遗漏所需 scope。"
        )
    )

    def materialize(self) -> ToolSelection:
        """Convert the compact provider response into the domain type."""

        return ToolSelection(mode=self.mode, scopes=self.scopes)


type PlannerSubjectRole = Literal[
    MemorySubjectRole.CURRENT_PERSON,
    MemorySubjectRole.CURRENT_GROUP,
    MemorySubjectRole.REFERENCED_PERSON,
]


class _PlannerMemoryOutputBase(_StrictPlannerModel):
    """Fields shared by every schema-valid long-term memory access route."""

    access: MemoryAccessMode
    mode: MemoryContextMode
    purpose: MemoryRecallPurpose = Field(
        description=(
            "本轮记忆用途：开放式询问记忆内容或概括用 recall，顺接用 continuation；闭合式"
            "核验必须用 verify，例如‘你记得我更偏好深烘还是浅烘？’、‘是不是 X？’或"
            "‘有无依据？’；纠正/撤回/恢复用 correct；否则用 background。"
        )
    )
    subjects: tuple[PlannerSubjectRole, ...] = Field(default=(), max_length=3)
    entities: tuple[str, ...] = Field(default=(), max_length=5)
    temporal: MemoryTemporalIntent = Field(
        default_factory=MemoryTemporalIntent,
        description=(
            "相对时间须按 current_time 转换为绝对范围；只有用户明确排除范围外内容时使用 strict。"
        ),
    )
    preferred_kinds: tuple[MemoryKind, ...] = Field(default=(), max_length=3)
    requested_count: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="用户明确限制要返回多少条记忆时填写。",
    )
    self_recall: bool = Field(
        default=False,
        description="是否检索过去形成的动态 SELF 记忆。",
    )

    @model_validator(mode="before")
    @classmethod
    def _discard_legacy_reason_code(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "reason_code" not in value:
            return value
        normalized = dict(value)
        normalized.pop("reason_code", None)
        return normalized

    def materialize(self) -> MemoryContextPlan:
        if self.self_recall:
            reason_code = (
                MemoryContextReasonCode.SELF_OVERVIEW
                if self.mode is MemoryContextMode.OVERVIEW
                else MemoryContextReasonCode.SELF_MEMORY_RECALL
            )
        else:
            reason_code = {
                MemoryContextMode.NONE: MemoryContextReasonCode.DEFAULT,
                MemoryContextMode.LEXICAL: MemoryContextReasonCode.ROUTINE_CONTEXT,
                MemoryContextMode.HYBRID: MemoryContextReasonCode.MEMORY_RECALL,
                MemoryContextMode.OVERVIEW: MemoryContextReasonCode.EXPLICIT_OVERVIEW,
            }[self.mode]
        return MemoryContextPlan(
            access=self.access,
            mode=self.mode,
            purpose=self.purpose,
            subjects=tuple(MemorySubjectRole(subject) for subject in self.subjects),
            entities=self.entities,
            temporal=self.temporal,
            preferred_kinds=self.preferred_kinds,
            requested_count=self.requested_count,
            reason_code=reason_code,
            self_recall=self.self_recall,
        )


class PlannerAutomaticMemoryOutput(_PlannerMemoryOutputBase):
    """Automatic retrieval without first-round Memory Scope tools."""

    access: Literal[MemoryAccessMode.AUTOMATIC]
    mode: Literal[
        MemoryContextMode.LEXICAL,
        MemoryContextMode.HYBRID,
        MemoryContextMode.OVERVIEW,
    ]


class PlannerToolMemoryOutput(_PlannerMemoryOutputBase):
    """Explicit model-facing memory read tools without automatic retrieval."""

    access: Literal[MemoryAccessMode.TOOL]
    mode: Literal[MemoryContextMode.NONE]


class PlannerMutationMemoryOutput(_PlannerMemoryOutputBase):
    """One terminal long-term memory mutation path without automatic retrieval."""

    access: Literal[MemoryAccessMode.MUTATION]
    mode: Literal[MemoryContextMode.NONE]


class PlannerNoMemoryOutput(_PlannerMemoryOutputBase):
    """No long-term memory access for this turn."""

    access: Literal[MemoryAccessMode.NONE]
    mode: Literal[MemoryContextMode.NONE]


type PlannerMemoryOutput = Annotated[
    PlannerAutomaticMemoryOutput
    | PlannerToolMemoryOutput
    | PlannerMutationMemoryOutput
    | PlannerNoMemoryOutput,
    Field(discriminator="access"),
]


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
    the backend: delivery, memory depth, emoji, and voice. Tool selection is
    optional only because omission preserves the existing capability-kernel
    ``inherit`` behavior. Secondary details use backend defaults, keeping
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
    tool_selection: PlannerToolOutput | None = None
    wait_seconds: float = Field(default=0, ge=0, le=300, strict=True)
    memory_context: PlannerMemoryOutput
    emoji: PlannerEmojiOutput
    voice: PlannerVoiceOutput

    @model_validator(mode="before")
    @classmethod
    def _discard_legacy_derived_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized.pop("target_user_ids", None)
        normalized.pop("desired_messages", None)
        return normalized

    def materialize(self) -> TurnPlan:
        """Fill omitted provider fields from trusted backend defaults."""

        plan = TurnPlan(
            decision=self.decision,
            intent=self.intent,
            delivery_mode=self.delivery_mode,
            reply_to_event_id=self.reply_to_event_id,
            wait_seconds=self.wait_seconds,
            confidence=self.confidence,
            reason_code=self.reason_code,
            memory_context=self.memory_context.materialize(),
            emoji=self.emoji.materialize(),
            voice=self.voice.materialize(),
        )
        if self.tool_selection is None:
            return plan
        return plan.model_copy(
            update={"tool_selection": self.tool_selection.materialize()},
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
