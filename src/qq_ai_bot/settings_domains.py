"""Immutable domain projections of the backward-compatible flat environment."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qq_ai_bot.domain.messages import ReasoningEffort
from qq_ai_bot.web.models import WebMode

_PLUGIN_ID = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?$")
_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def validate_direct_command_bindings(value: dict[str, str]) -> dict[str, str]:
    """Normalize fail-closed Host-owned direct command bindings."""

    normalized: dict[str, str] = {}
    for prefix, raw_target in value.items():
        if not prefix or len(prefix) > 16:
            raise ValueError("PLUGIN_DIRECT_COMMAND_BINDINGS prefixes must be 1..16 characters")
        if prefix != prefix.strip() or any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in prefix
        ):
            raise ValueError(
                "PLUGIN_DIRECT_COMMAND_BINDINGS prefixes must not contain whitespace "
                "or control characters"
            )
        target = raw_target.strip()
        if target != raw_target or ":" not in target:
            raise ValueError("PLUGIN_DIRECT_COMMAND_BINDINGS targets must use plugin_id:command")
        plugin_id, command_name = target.rsplit(":", 1)
        if _PLUGIN_ID.fullmatch(plugin_id) is None or _COMMAND_NAME.fullmatch(command_name) is None:
            raise ValueError("PLUGIN_DIRECT_COMMAND_BINDINGS targets must use plugin_id:command")
        normalized[prefix] = target

    prefixes = tuple(normalized)
    for index, left in enumerate(prefixes):
        for right in prefixes[index + 1 :]:
            if left.startswith(right) or right.startswith(left):
                raise ValueError(
                    f"PLUGIN_DIRECT_COMMAND_BINDINGS prefixes must not overlap: {left!r}, {right!r}"
                )
    return normalized


class DomainSettings(BaseModel):
    """Base for explicit, constructor-friendly configuration slices."""

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)


class AppSettings(DomainSettings):
    app_host: str
    app_port: int = Field(gt=0)
    log_level: str
    log_message_content: bool
    database_url: str


class OneBotSettings(DomainSettings):
    onebot_access_token: str
    superusers_csv: str
    enabled_groups_csv: str
    ignored_bot_users_csv: str
    ai_prefix: str


class ModelRuntimeSettings(DomainSettings):
    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: float = Field(gt=0)
    llm_max_retries: int = Field(ge=0)
    llm_temperature: float = Field(ge=0, le=2)
    llm_max_output_tokens: int = Field(gt=0)
    llm_thinking_enabled: bool | None
    llm_reasoning_effort: ReasoningEffort | None
    llm_flash_base_url: str
    llm_flash_api_key: str
    llm_flash_model: str
    model_profiles_file: Path
    global_llm_concurrency: int = Field(gt=0)
    model_stats_recent_error_limit: int = Field(gt=0)


class ConversationSettings(DomainSettings):
    processed_event_ttl_seconds: int = Field(gt=0)
    processed_event_cleanup_seconds: int = Field(gt=0)
    max_context_characters: int = Field(gt=0)
    context_metadata_budget_ratio: float = Field(gt=0, lt=1)
    history_window_low_watermark_ratio: float = Field(gt=0, lt=1)
    per_user_requests_per_minute: int = Field(gt=0)
    per_group_requests_per_minute: int = Field(gt=0)
    max_input_characters: int = Field(gt=0)
    max_output_characters: int = Field(gt=0)
    max_qq_message_chars: int = Field(gt=0)
    daily_chat_message_delay_min_seconds: float = Field(ge=0)
    daily_chat_message_delay_max_seconds: float = Field(ge=0)
    observe_enabled_groups: bool
    recent_history_tool_limit: int = Field(gt=0)
    local_context_event_limit: int = Field(gt=0, le=20_000)
    agent_max_tool_calls: int = Field(gt=0)
    agent_max_model_requests: int = Field(gt=0)
    agent_tool_result_max_characters: int = Field(gt=0)
    reply_sequence_cancel_on_new_message: bool
    reply_plan_hard_max_messages: int = Field(gt=0)

    @model_validator(mode="after")
    def _delay_order(self) -> ConversationSettings:
        if self.daily_chat_message_delay_min_seconds > self.daily_chat_message_delay_max_seconds:
            raise ValueError("daily chat minimum delay must not exceed maximum delay")
        return self


class PlannerSettings(DomainSettings):
    planner_direct_enabled: bool
    planner_group_enabled: bool
    planner_group_debounce_seconds: float = Field(ge=0)
    planner_preferred_messages: int = Field(gt=0)
    planner_temperature: float = Field(ge=0, le=2)
    planner_max_output_tokens: int = Field(gt=0)
    planner_timeout_seconds: float = Field(gt=0)
    planner_confidence_threshold: float = Field(ge=0, le=1)
    planner_reply_necessity_threshold: int = Field(ge=0)
    planner_max_pending_messages: int = Field(gt=0)
    planner_recent_presence_window_seconds: int = Field(gt=0)
    planner_max_wait_seconds: int = Field(ge=0)
    planner_interrupt_autonomous_on_new_message: bool
    planner_record_runs: bool


class PluginSettings(DomainSettings):
    plugin_system_enabled: bool
    plugin_directory: Path
    plugin_api_version: str
    plugin_direct_command_bindings: dict[str, str] = Field(default_factory=dict)
    plugin_hook_timeout_seconds: float = Field(gt=0)
    plugin_start_timeout_seconds: float = Field(gt=0)
    plugin_stop_timeout_seconds: float = Field(gt=0)
    plugin_max_prompt_fragment_characters: int = Field(gt=0)
    plugin_max_prompt_characters_per_plugin: int = Field(gt=0)
    plugin_max_total_prompt_characters: int = Field(gt=0)
    plugin_background_task_limit: int = Field(gt=0)
    plugin_failure_disable_threshold: int = Field(gt=0)
    plugin_http_max_response_bytes: int = Field(gt=0)
    plugin_http_timeout_seconds: float = Field(gt=0)
    plugin_ai_session_max_history_messages: int = Field(gt=0)
    plugin_external_event_context_limit: int = Field(gt=0, le=100)
    plugin_external_event_context_characters: int = Field(gt=0, le=32_000)

    @field_validator("plugin_direct_command_bindings")
    @classmethod
    def _direct_command_bindings(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_direct_command_bindings(value)

    @model_validator(mode="after")
    def _prompt_budgets(self) -> PluginSettings:
        if self.plugin_max_total_prompt_characters <= self.plugin_max_prompt_fragment_characters:
            raise ValueError("total plugin prompt budget must exceed one fragment budget")
        if (
            self.plugin_max_prompt_characters_per_plugin
            < self.plugin_max_prompt_fragment_characters
        ):
            raise ValueError("per-plugin prompt budget must cover one fragment")
        return self


class MemorySettings(DomainSettings):
    group_memory_max_entries: int = Field(gt=0)
    person_memory_max_entries: int = Field(gt=0)
    person_group_memory_max_entries: int = Field(gt=0)
    preference_max_entries: int = Field(gt=0)
    memory_batch_seconds: float = Field(ge=0)
    memory_batch_trigger_count: int = Field(gt=0)
    memory_batch_max_events: int = Field(gt=0)
    memory_batch_max_characters: int = Field(gt=0)
    memory_batch_max_wait_seconds: float = Field(ge=0)
    memory_batch_max_output_tokens: int = Field(gt=0)
    memory_retrieval_enabled: bool
    self_memory_enabled: bool
    memory_self_reflection_enabled: bool
    memory_self_reflection_schedule_hours: str
    memory_self_reflection_timezone: str
    memory_self_reflection_poll_seconds: float = Field(gt=0)
    memory_self_reflection_max_batches_per_run: int = Field(gt=0, le=100)
    memory_self_reflection_max_batches_per_conversation_per_run: int = Field(gt=0, le=25)
    memory_self_reflection_max_daily_calls: int = Field(gt=0, le=365)
    memory_self_reflection_event_threshold: int = Field(gt=0)
    memory_self_reflection_character_threshold: int = Field(gt=0)
    memory_self_reflection_low_event_threshold: int = Field(gt=0)
    memory_self_reflection_low_character_threshold: int = Field(gt=0)
    memory_self_reflection_natural_gap_seconds: float = Field(gt=0)
    memory_self_reflection_max_wait_seconds: float = Field(gt=0)
    memory_self_reflection_max_events: int = Field(gt=0, le=100)
    memory_self_reflection_max_characters: int = Field(gt=0, le=8000)
    memory_self_reflection_max_output_tokens: int = Field(gt=0)
    memory_self_reflection_tool_receipt_characters: int = Field(gt=0, le=8000)
    memory_self_reflection_tool_receipt_retention_days: int = Field(gt=0, le=30)
    memory_max_referenced_targets: int = Field(gt=0)
    memory_lexical_candidate_limit: int = Field(gt=0)
    memory_context_limit_per_entity: int = Field(gt=0)
    memory_overview_limit_per_entity: int = Field(gt=0)
    memory_always_on_explicit_preference_limit: int = Field(ge=0)
    memory_query_term_limit: int = Field(gt=0)
    memory_short_query_fallback_enabled: bool
    memory_semantic_enabled: bool
    memory_semantic_candidate_limit: int = Field(gt=0)
    memory_semantic_min_similarity: float = Field(ge=-1, le=1)
    memory_hybrid_lexical_weight: float = Field(ge=0)
    memory_hybrid_semantic_weight: float = Field(ge=0)
    memory_hybrid_rrf_k: int = Field(gt=0)
    memory_consolidation_enabled: bool
    memory_consolidation_candidate_limit: int = Field(gt=0)
    memory_consolidation_min_relevance: float = Field(ge=0, le=1)
    memory_consolidation_model_task: str
    memory_consolidation_max_output_tokens: int = Field(gt=0)
    memory_dream_enabled: bool
    memory_dream_schedule_hour: int = Field(ge=0, le=23)
    memory_dream_timezone: str
    memory_dream_poll_seconds: float = Field(gt=0)
    memory_dream_max_clusters_per_run: int = Field(gt=0, le=100)
    memory_dream_max_model_calls_per_run: int = Field(gt=0, le=200)
    memory_dream_similarity_threshold: float = Field(ge=-1, le=1)
    memory_dream_max_cluster_size: int = Field(ge=2, le=20)
    memory_dream_max_input_characters: int = Field(gt=0, le=100_000)
    memory_dream_max_output_tokens: int = Field(gt=0)
    memory_dream_evidence_per_fact: int = Field(ge=0, le=10)
    memory_dream_evidence_excerpt_characters: int = Field(gt=0, le=2000)
    memory_mmr_enabled: bool
    memory_mmr_lambda: float = Field(ge=0, le=1)
    memory_mmr_candidate_pool_size: int = Field(gt=0, le=100)
    memory_evidence_weight_explicit: float = Field(ge=0, le=1)
    memory_evidence_weight_self: float = Field(ge=0, le=1)
    memory_evidence_weight_group: float = Field(ge=0, le=1)
    memory_evidence_weight_third_party: float = Field(ge=0, le=1)
    memory_evidence_weight_rebuild: float = Field(ge=0, le=1)
    memory_authority_cap_explicit: float = Field(ge=0, le=1)
    memory_authority_cap_self: float = Field(ge=0, le=1)
    memory_authority_cap_group: float = Field(ge=0, le=1)
    memory_authority_cap_third_party: float = Field(ge=0, le=1)
    memory_maintenance_enabled: bool
    memory_maintenance_interval_seconds: float = Field(gt=0)
    memory_maintenance_batch_limit: int = Field(gt=0)
    memory_automatic_stale_days: int = Field(gt=0)
    memory_third_party_stale_days: int = Field(gt=0)
    memory_contested_stale_days: int = Field(gt=0)
    memory_stale_max_importance: int = Field(ge=1, le=5)
    memory_stale_max_confidence: float = Field(ge=0, le=1)
    memory_embedding_enabled: bool
    memory_embedding_provider: str
    memory_embedding_base_url: str
    memory_embedding_api_key: str
    memory_embedding_model: str
    memory_embedding_dimensions: int = Field(gt=0)
    memory_embedding_output_type: str
    memory_embedding_document_template_version: int = Field(gt=0)
    memory_embedding_query_instruct: str
    memory_embedding_request_timeout_seconds: float = Field(gt=0)
    memory_embedding_max_text_characters: int = Field(gt=0)
    memory_embedding_worker_enabled: bool
    memory_embedding_worker_interval_seconds: float = Field(gt=0)
    memory_embedding_worker_claim_limit: int = Field(gt=0)
    memory_embedding_retry_attempts: int = Field(gt=0)
    memory_embedding_retry_initial_seconds: float = Field(gt=0)
    memory_embedding_http_concurrency: int = Field(gt=0)
    memory_embedding_query_cache_ttl_seconds: float = Field(gt=0)
    memory_embedding_query_cache_max_entries: int = Field(gt=0)
    memory_rebuild_enabled: bool
    memory_rebuild_worker_interval_seconds: float = Field(gt=0)
    memory_rebuild_scan_batch_size: int = Field(gt=0)
    memory_rebuild_extraction_concurrency: int = Field(gt=0)
    memory_rebuild_commit_batch_size: int = Field(gt=0)
    memory_rebuild_context_event_limit: int = Field(gt=0)
    memory_rebuild_retry_attempts: int = Field(gt=0)
    memory_rebuild_retry_initial_seconds: float = Field(gt=0)
    memory_rebuild_review_page_size: int = Field(gt=0)
    memory_rebuild_source_excerpt_characters: int = Field(gt=0)
    memory_rebuild_max_events_per_run: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _memory_batch_shape(self) -> MemorySettings:
        if self.memory_batch_trigger_count > self.memory_batch_max_events:
            raise ValueError("memory batch trigger count cannot exceed batch event limit")
        hours = [item.strip() for item in self.memory_self_reflection_schedule_hours.split(",")]
        if len(hours) != 3 or any(not item.isdigit() or not 0 <= int(item) <= 23 for item in hours):
            raise ValueError("memory self-reflection schedule must contain three hours")
        if len(set(hours)) != 3:
            raise ValueError("memory self-reflection schedule hours must be unique")
        if (
            self.memory_self_reflection_max_batches_per_conversation_per_run
            > self.memory_self_reflection_max_batches_per_run
        ):
            raise ValueError(
                "memory self-reflection per-conversation batch limit cannot exceed run limit"
            )
        if (
            self.memory_self_reflection_low_event_threshold
            > self.memory_self_reflection_event_threshold
        ):
            raise ValueError(
                "memory self-reflection low event watermark cannot exceed high watermark"
            )
        if self.memory_self_reflection_event_threshold > self.memory_self_reflection_max_events:
            raise ValueError(
                "memory self-reflection high event watermark cannot exceed batch event limit"
            )
        if (
            self.memory_self_reflection_low_character_threshold
            > self.memory_self_reflection_character_threshold
        ):
            raise ValueError(
                "memory self-reflection low character watermark cannot exceed high watermark"
            )
        if (
            self.memory_self_reflection_character_threshold
            > self.memory_self_reflection_max_characters
        ):
            raise ValueError(
                "memory self-reflection high character watermark cannot exceed "
                "batch character limit"
            )
        return self


class RelationshipSettings(DomainSettings):
    relationship_enabled: bool
    relationship_initial_affection: int = Field(ge=0, le=100)
    relationship_initial_trust: int = Field(ge=0, le=100)
    relationship_batch_seconds: float = Field(ge=0)
    relationship_batch_trigger_count: int = Field(gt=0)
    relationship_batch_max_turns: int = Field(gt=0)
    relationship_max_attempts: int = Field(gt=0)
    relationship_confidence_threshold: float = Field(ge=0, le=1)
    affection_max_auto_delta: int = Field(gt=0)
    trust_max_auto_delta: int = Field(gt=0)
    relationship_daily_positive_cap: int = Field(ge=0)
    relationship_daily_negative_cap: int = Field(ge=0)
    trust_affection_cap_offset: int = Field(ge=0, le=100)
    conflict_preference_min_gap: int = Field(ge=0, le=100)


class WebSettings(DomainSettings):
    web_enabled: bool
    web_mode: WebMode | None = None
    tavily_api_key: str
    web_search_depth: str
    web_search_max_results: int = Field(gt=0)
    web_extract_max_results: int = Field(gt=0)
    web_timeout_seconds: float = Field(gt=0)
    web_max_retries: int = Field(ge=0)
    web_global_concurrency: int = Field(gt=0)
    web_max_calls_per_turn: int = Field(gt=0)
    web_tool_result_max_characters: int = Field(gt=0)
    web_source_retention_days: int = Field(gt=0)
    web_source_max_runs_per_conversation: int = Field(gt=0)
    web_tavily_domains_csv: str = ""
    web_allow_provider_override: bool = True
    web_fallback_on_access_denied: bool = True
    web_fallback_on_target_miss: bool = True

    @model_validator(mode="after")
    def _credentials(self) -> WebSettings:
        if self.mode in {WebMode.TAVILY, WebMode.NATIVE_WITH_TAVILY_FALLBACK} and not (
            self.tavily_api_key
        ):
            raise ValueError("TAVILY_API_KEY is required for tavily and fallback web modes")
        return self

    @property
    def mode(self) -> WebMode:
        """Resolve WEB_MODE while preserving the legacy WEB_ENABLED behavior."""

        if self.web_mode is not None:
            return self.web_mode
        return WebMode.TAVILY if self.web_enabled else WebMode.DISABLED

    @property
    def tavily_domains(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.web_tavily_domains_csv.split(",") if item.strip()
        )


class VisionSettings(DomainSettings):
    vision_enabled: bool
    vision_provider: str
    vision_base_url: str
    vision_api_key: str
    vision_model: str
    vision_timeout_seconds: float = Field(gt=0)
    vision_max_retries: int = Field(gt=0)
    vision_global_concurrency: int = Field(gt=0)
    vision_queue_max_pending: int = Field(gt=0)
    vision_queue_timeout_seconds: float = Field(gt=0)
    vision_media_download_timeout_seconds: float = Field(gt=0)
    vision_allow_private_urls: bool
    vision_max_output_tokens: int = Field(gt=0)
    vision_thinking_enabled: bool
    vision_thinking_budget: int = Field(gt=0)
    vision_low_confidence_retry_threshold: float = Field(ge=0, le=1)
    vision_max_images_per_turn: int = Field(gt=0)
    vision_max_frames_per_turn: int = Field(gt=0)
    vision_gif_max_frames: int = Field(gt=0)
    vision_max_download_bytes: int = Field(gt=0)
    vision_max_prepared_bytes: int = Field(gt=0)
    vision_max_dimension: int = Field(gt=0)
    vision_max_pixels: int = Field(gt=0)
    vision_per_user_requests_per_minute: int = Field(gt=0)
    vision_per_group_requests_per_minute: int = Field(gt=0)
    vision_analysis_retention_days: int = Field(gt=0)

    @model_validator(mode="after")
    def _credentials(self) -> VisionSettings:
        if self.vision_enabled and not all(
            (self.vision_base_url.strip(), self.vision_api_key.strip(), self.vision_model.strip())
        ):
            raise ValueError("VISION_BASE_URL, VISION_API_KEY and VISION_MODEL are required")
        return self


class EmojiSettings(DomainSettings):
    emoji_enabled: bool
    emoji_collection_enabled: bool
    emoji_collection_mode: str
    emoji_collect_private: bool
    emoji_collect_group: bool
    emoji_auto_adopt_enabled: bool
    emoji_auto_adopt_min_confidence: float = Field(ge=0, le=1)
    emoji_pool_capacity: int | None = Field(default=None, gt=0)
    emoji_replacement_mode: str
    emoji_selector_enabled: bool
    emoji_selector_candidate_count: int = Field(gt=0)
    emoji_selector_score_gap: float = Field(ge=0)
    emoji_selector_timeout_seconds: float = Field(gt=0)
    emoji_max_effects_per_reply: int = Field(gt=0)
    emoji_spontaneous_frequency: float = Field(ge=0, le=1)
    emoji_near_duplicate_enabled: bool
    emoji_near_duplicate_distance: int = Field(ge=0, le=64)
    emoji_same_emoji_cooldown_seconds: int = Field(ge=0)
    emoji_scope_repeat_cooldown_seconds: int = Field(ge=0)
    emoji_cache_retention_days: int = Field(gt=0)
    emoji_worker_batch_size: int = Field(gt=0)
    emoji_worker_poll_seconds: float = Field(gt=0)
    emoji_worker_lease_seconds: int = Field(gt=0)
    emoji_worker_max_attempts: int = Field(gt=0)
    emoji_worker_retry_delay_seconds: float = Field(ge=0)
    emoji_analysis_version: str
    emoji_storage_root: Path
    emoji_preview_max_dimension: int = Field(gt=0)


class SpeechSettings(DomainSettings):
    speech_enabled: bool
    speech_provider: str
    speech_socket_path: Path
    speech_root: Path
    genie_data_dir: Path
    speech_default_profile: str
    speech_worker_start_timeout_seconds: float = Field(gt=0)
    speech_worker_request_timeout_seconds: float = Field(gt=0)
    speech_planner_enabled: bool
    speech_default_mode: str
    speech_split_sentence: bool
    speech_max_synthesis_characters: int | None = Field(default=None, gt=0)
    speech_queue_max_pending: int | None = Field(default=None, gt=0)
    speech_cache_retention_hours: int | None = Field(default=None, gt=0)
    speech_private_enabled: bool
    speech_group_enabled: bool
    speech_automation_enabled: bool
    speech_plugin_enabled: bool
    speech_text_fallback_enabled: bool
    speech_spontaneous_frequency: float = Field(ge=0, le=1)
    speech_jp_katakana_enabled: bool


class AutomationSettings(DomainSettings):
    automation_enabled: bool
    default_timezone: str
    automation_poll_seconds: float = Field(gt=0)
    automation_lease_seconds: int = Field(gt=0)
    automation_max_active_per_superuser: int = Field(gt=0)
    automation_max_active_per_user: int = Field(gt=0)
    automation_max_steps: int = Field(gt=0)
    automation_max_llm_calls_per_run: int = Field(gt=0)
    automation_max_tool_calls_per_run: int = Field(gt=0)
    automation_max_messages_per_run: int = Field(gt=0)
    automation_max_runtime_seconds: int = Field(gt=0)
    automation_min_interval_seconds: int = Field(gt=0)
    automation_default_misfire_grace_seconds: int = Field(gt=0)
    automation_max_consecutive_failures: int = Field(gt=0)
    automation_run_retention_days: int = Field(gt=0)


class ToolingSettings(DomainSettings):
    tooling_max_parallel_calls: int = Field(gt=0)
    tooling_selected_tool_limit: int | None = Field(default=None, gt=0)
    tooling_schema_token_budget: int | None = Field(default=None, gt=0)
    tooling_result_token_budget: int | None = Field(default=None, gt=0)
    tooling_result_item_limit: int | None = Field(default=None, gt=0)
    tooling_result_artifact_enabled: bool
    tooling_result_artifact_retention_seconds: int = Field(gt=0)


class MCPSettings(DomainSettings):
    mcp_enabled: bool
    mcp_config_path: Path
    mcp_cache_enabled: bool
    mcp_gateway_enabled: bool
    mcp_tool_selection_mode: str
    mcp_metadata_cache_ttl_seconds: int = Field(gt=0)
    mcp_connect_timeout_seconds: float = Field(gt=0)
    mcp_request_timeout_seconds: float = Field(gt=0)
    mcp_selected_tool_limit: int | None = Field(default=None, gt=0)
    mcp_schema_token_budget: int | None = Field(default=None, gt=0)
    mcp_result_token_budget: int | None = Field(default=None, gt=0)
    mcp_result_item_limit: int | None = Field(default=None, gt=0)
    mcp_max_parallel_calls: int = Field(gt=0)
    mcp_artifact_retention_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def _selection_mode(self) -> MCPSettings:
        if self.mcp_tool_selection_mode not in {"all", "catalog", "hybrid", "gateway"}:
            raise ValueError("MCP_TOOL_SELECTION_MODE must be all/catalog/hybrid/gateway")
        return self
