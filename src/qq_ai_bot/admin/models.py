"""Domain models for explicit administrator capabilities and runtime settings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from qq_ai_bot.config import Settings

ConfigValue = str | int | float | bool | None
ConfigValueType = Literal["string", "integer", "number", "boolean", "enum"]


class ConfigApplyMode(StrEnum):
    """How a registered configuration value becomes effective."""

    HOT = "hot"
    FUTURE_ONLY = "future_only"
    RESTART_REQUIRED = "restart_required"
    IMMUTABLE = "immutable"
    SECRET = "secret"


class ConfigScopeType(StrEnum):
    """Supported override scopes, ordered elsewhere by specificity."""

    GLOBAL = "global"
    GROUP = "group"
    USER = "user"


@dataclass(frozen=True, slots=True)
class ConfigSpec:
    """One explicitly exposed configuration capability."""

    key: str
    display_name: str
    description: str
    aliases: tuple[str, ...]
    value_type: ConfigValueType
    minimum: float | None
    maximum: float | None
    choices: tuple[str, ...]
    allowed_scopes: tuple[ConfigScopeType, ...]
    apply_mode: ConfigApplyMode
    permission: str
    sensitive: bool
    env_alias: str | None
    default_getter: Callable[[Settings], ConfigValue]
    settings_fields: tuple[str, ...] = ()
    category: str = ""

    @property
    def mutable(self) -> bool:
        """Return whether a validated database override may be written."""

        return self.apply_mode not in {
            ConfigApplyMode.IMMUTABLE,
            ConfigApplyMode.SECRET,
        }


@dataclass(frozen=True, slots=True)
class EffectiveConfigValue:
    """A resolved value plus provenance, without leaking secret material."""

    key: str
    value: ConfigValue
    source: str
    scope_type: ConfigScopeType | None
    scope_id: str
    apply_mode: ConfigApplyMode
    pending_restart: bool = False
    configured: bool | None = None


@dataclass(frozen=True, slots=True)
class ConfigChangeResult:
    """Truthful result returned by deterministic commands and model tools."""

    success: bool
    key: str
    scope_type: ConfigScopeType
    scope_id: str
    before: ConfigValue = None
    after: ConfigValue = None
    apply_mode: ConfigApplyMode | None = None
    pending_restart: bool = False
    change_id: int | None = None
    version: int | None = None
    error_category: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AdminOperationEvent:
    """Safe projection of one persisted administrator operation."""

    id: int
    actor_user_id: str
    trigger_message_id: str
    conversation_key: str
    capability: str
    operation: str
    target_type: str
    target_id: str
    before: object
    after: object
    success: bool
    error_category: str | None
    duration_seconds: float
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AdminActor:
    """Authority derived only from the current transport event."""

    user_id: str
    is_superuser: bool
    trigger_message_id: str
    conversation_key: str
    current_group_id: str | None = None
    mentioned_user_ids: tuple[str, ...] = ()
    current_message_text: str = ""
    bot_user_id: str = ""
    decision_actor_type: str = "command"
    decision_actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContextRuntimeConfig:
    local_event_limit: int


@dataclass(frozen=True, slots=True)
class MemoryRetrievalRuntimeConfig:
    retrieval_enabled: bool
    max_referenced_targets: int
    lexical_candidate_limit: int
    context_limit_per_entity: int
    overview_limit_per_entity: int
    always_on_explicit_preference_limit: int
    query_term_limit: int
    short_query_fallback_enabled: bool
    automatic_recall_per_target_limit: int = 2
    automatic_recall_background_limit: int = 2
    automatic_recall_continuation_limit: int = 2
    automatic_recall_focused_limit: int = 3
    automatic_recall_overview_limit: int = 4
    self_enabled: bool = False
    semantic_enabled: bool = True
    semantic_candidate_limit: int = 50
    semantic_min_similarity: float = 0.35
    hybrid_lexical_weight: float = 1.0
    hybrid_semantic_weight: float = 1.0
    hybrid_rrf_k: int = 60
    intent_rerank_enabled: bool = True
    activation_ranking_enabled: bool = True
    usage_attribution_enabled: bool = True
    usage_attribution_timeout_seconds: float = 12.0
    usage_attribution_job_ttl_seconds: float = 120.0
    usage_attribution_queue_limit: int = 128
    reinforcement_enabled: bool = True
    recall_receipts_enabled: bool = True
    activation_half_life_episode_days: float = 14.0
    activation_half_life_fact_days: float = 60.0
    activation_half_life_preference_days: float = 120.0
    activation_half_life_explicit_days: float = 365.0
    reinforcement_alpha_background: float = 0.05
    reinforcement_alpha_continuation: float = 0.12
    reinforcement_alpha_recall: float = 0.25
    reinforcement_alpha_verify: float = 0.08
    intent_recent_window_days: int = 90
    recall_receipt_retention_days: int = 30
    recall_trace_candidate_limit: int = 20
    consolidation_enabled: bool = True
    consolidation_candidate_limit: int = 12
    consolidation_min_relevance: float = 0.25
    consolidation_model_task: str = "memory_consolidation"
    consolidation_max_output_tokens: int = 1200
    evidence_weight_explicit: float = 1.0
    evidence_weight_self: float = 0.9
    evidence_weight_group: float = 0.7
    evidence_weight_third_party: float = 0.55
    evidence_weight_rebuild: float = 0.75
    authority_cap_explicit: float = 1.0
    authority_cap_self: float = 0.98
    authority_cap_group: float = 0.9
    authority_cap_third_party: float = 0.75
    maintenance_enabled: bool = True
    maintenance_interval_seconds: float = 300.0
    maintenance_batch_limit: int = 100
    automatic_stale_days: int = 180
    third_party_stale_days: int = 30
    contested_stale_days: int = 14
    stale_max_importance: int = 2
    stale_max_confidence: float = 0.7


@dataclass(frozen=True, slots=True)
class ConversationRuntimeConfig:
    """Effective autonomous-conversation policy for one turn."""

    autonomous_enabled: bool
    autonomous_debounce_seconds: float
    autonomous_admission_threshold: int
    autonomous_batch_limit: int
    autonomous_presence_window_seconds: int
    interrupt_autonomous_on_new_message: bool


@dataclass(frozen=True, slots=True)
class ReplyRuntimeConfig:
    delay_min_seconds: float
    delay_max_seconds: float
    max_qq_message_chars: int
    cancel_on_new_message: bool
    hard_max_messages: int


@dataclass(frozen=True, slots=True)
class PluginRuntimeConfig:
    """Hot plugin limits that never include credentials or installation paths."""

    hook_timeout_seconds: float
    max_prompt_fragment_characters: int
    max_prompt_characters_per_plugin: int
    max_total_prompt_characters: int


@dataclass(frozen=True, slots=True)
class LLMRuntimeConfig:
    model: str
    timeout_seconds: float
    max_retries: int
    temperature: float
    max_output_tokens: int
    thinking_enabled: bool | None


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfig:
    max_tool_calls: int
    max_model_requests: int
    tool_result_max_characters: int


@dataclass(frozen=True, slots=True)
class ToolingRuntimeConfig:
    max_parallel_calls: int
    selected_tool_limit: int | None
    first_round_hard_cap: int
    first_round_pin_ids: tuple[str, ...]
    schema_token_budget: int | None
    result_token_budget: int | None
    result_item_limit: int | None
    result_artifact_enabled: bool
    result_artifact_retention_seconds: int


@dataclass(frozen=True, slots=True)
class MCPRuntimeConfig:
    enabled: bool
    gateway_enabled: bool
    metadata_cache_ttl_seconds: int
    connect_timeout_seconds: float
    request_timeout_seconds: float
    selected_tool_limit: int | None
    schema_token_budget: int | None
    result_token_budget: int | None
    result_item_limit: int | None
    max_parallel_calls: int
    artifact_retention_seconds: int


@dataclass(frozen=True, slots=True)
class WebRuntimeConfig:
    search_max_results: int
    extract_max_results: int
    max_calls_per_turn: int
    tool_result_max_characters: int
    source_retention_days: int
    source_max_runs_per_conversation: int
    mode: str = "disabled"


@dataclass(frozen=True, slots=True)
class RelationshipRuntimeConfig:
    confidence_threshold: float
    max_auto_delta: int
    daily_positive_cap: int
    daily_negative_cap: int
    conflict_preference_min_gap: int
    initial_affection: int
    initial_trust: int


@dataclass(frozen=True, slots=True)
class VisionRuntimeConfig:
    max_images_per_turn: int
    max_frames_per_turn: int
    gif_max_frames: int
    thinking_enabled: bool
    thinking_budget: int
    low_confidence_retry_threshold: float
    per_user_requests_per_minute: int
    per_group_requests_per_minute: int
    analysis_retention_days: int


@dataclass(frozen=True, slots=True)
class EmojiRuntimeConfig:
    """Effective collection, pool, selection, and worker policy for one turn."""

    enabled: bool
    collection_enabled: bool
    collection_mode: str
    collect_private: bool
    collect_group: bool
    auto_adopt_enabled: bool
    auto_adopt_min_confidence: float
    pool_capacity: int | None
    replacement_mode: str
    selector_enabled: bool
    selector_candidate_count: int
    selector_score_gap: float
    selector_timeout_seconds: float
    max_effects_per_reply: int
    spontaneous_frequency: float
    near_duplicate_enabled: bool
    near_duplicate_distance: int
    same_emoji_cooldown_seconds: int
    scope_repeat_cooldown_seconds: int
    cache_retention_days: int
    worker_batch_size: int
    worker_poll_seconds: float
    worker_lease_seconds: int
    worker_max_attempts: int
    worker_retry_delay_seconds: float
    analysis_version: str


@dataclass(frozen=True, slots=True)
class SpeechRuntimeConfig:
    """Effective local speech policy for one turn."""

    enabled: bool
    provider: str
    socket_path: str
    root: str
    genie_data_dir: str
    default_profile: str
    agent_effects_enabled: bool
    default_mode: str
    split_sentence: bool
    max_synthesis_characters: int | None
    queue_max_pending: int | None
    cache_retention_hours: int | None
    private_enabled: bool
    group_enabled: bool
    automation_enabled: bool
    plugin_enabled: bool
    text_fallback_enabled: bool
    spontaneous_frequency: float = 0.15


@dataclass(frozen=True, slots=True)
class RuntimeConfigSnapshot:
    """One internally consistent runtime view for an incoming message."""

    plugins: PluginRuntimeConfig
    context: ContextRuntimeConfig
    memory: MemoryRetrievalRuntimeConfig
    reply: ReplyRuntimeConfig
    llm: LLMRuntimeConfig
    agent: AgentRuntimeConfig
    web: WebRuntimeConfig
    relationship: RelationshipRuntimeConfig
    vision: VisionRuntimeConfig
    emoji: EmojiRuntimeConfig
    speech: SpeechRuntimeConfig
    conversation: ConversationRuntimeConfig
    tooling: ToolingRuntimeConfig | None = None
    mcp: MCPRuntimeConfig | None = None

    def conversation_policy(self) -> ConversationRuntimeConfig:
        """Return the autonomous-conversation policy for this snapshot."""

        return self.conversation
