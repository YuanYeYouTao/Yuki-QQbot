"""Environment-driven application settings with safe defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from qq_ai_bot.domain.messages import ReasoningEffort
from qq_ai_bot.settings_domains import (
    AppSettings,
    AutomationSettings,
    ConversationSettings,
    EmojiSettings,
    MCPSettings,
    MemorySettings,
    ModelRuntimeSettings,
    OneBotSettings,
    PluginSettings,
    RelationshipSettings,
    SpeechSettings,
    ToolingSettings,
    VisionSettings,
    WebSettings,
    validate_direct_command_bindings,
)
from qq_ai_bot.web.models import WebMode


def _csv_set(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _csv_tuple(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class BotIdentity:
    """Human-facing identity shared by deterministic bot subsystems."""

    display_name: str
    aliases: tuple[str, ...]
    voice_name: str


class Settings(BaseSettings):
    """Configuration loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_host: str = "0.0.0.0"
    app_port: int = 8080
    log_level: str = "INFO"
    log_message_content: bool = False

    onebot_access_token: str = ""
    superusers_csv: str = Field(default="", validation_alias="SUPERUSERS")
    enabled_groups_csv: str = Field(default="", validation_alias="ENABLED_GROUPS")
    ignored_bot_users_csv: str = Field(default="", validation_alias="IGNORED_BOT_USERS")
    ai_prefix: str = "!ai"

    bot_display_name: str = Field(
        default="Yuki",
        min_length=1,
        max_length=128,
        validation_alias="BOT_DISPLAY_NAME",
    )
    bot_aliases_csv: str = Field(
        default="Yuki,yuki,由纪",
        validation_alias="BOT_ALIASES",
    )
    bot_voice_name: str = Field(
        default="ゆき",
        min_length=1,
        max_length=128,
        validation_alias="BOT_VOICE_NAME",
    )
    bot_persona_file: Path | None = Field(default=None, validation_alias="BOT_PERSONA_FILE")

    llm_provider: str = "openai"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.7
    llm_max_output_tokens: int = 8192
    llm_thinking_enabled: bool | None = None
    llm_reasoning_effort: ReasoningEffort | None = None
    llm_flash_base_url: str = ""
    llm_flash_api_key: str = Field(default="", repr=False)
    llm_flash_model: str = ""
    model_profiles_file: Path = Path("config/model_profiles.toml")
    model_stats_recent_error_limit: int = 5
    system_prompt: str = (
        "你是一个运行在 QQ 中的 AI 助手。请只输出给用户的最终回答，不要输出隐藏的推理过程。"
        "不要声称执行了未实际成功的工具、代码、命令或文件访问。"
        "只有联网工具实际成功时，才能说明已经搜索或读取网页。"
    )
    system_prompt_file: Path | None = None
    # Compatibility for deployments created before BOT_PERSONA_FILE existed.
    yuki_persona_file: Path | None = None
    _bot_persona: str = PrivateAttr(default="")

    database_url: str = "sqlite+aiosqlite:///./data/qq_ai_bot.db"
    processed_event_ttl_seconds: int = 86400
    processed_event_cleanup_seconds: int = 3600
    # PromptCompiler uses the repository-wide characters / 4 token estimate.
    # History must stay append-only long enough that DeepSeek can persist the
    # prefix; a 12k pool forced extractive rewrites of input[0] every few dozen
    # group messages. Keep actor metadata near 5k characters so expanding the
    # pool does not inflate the unavoidable per-turn miss.
    max_context_characters: int = 81_920
    context_metadata_budget_ratio: float = Field(default=0.06, gt=0, lt=1)
    history_window_low_watermark_ratio: float = Field(default=0.67, gt=0, lt=1)

    global_llm_concurrency: int = 4
    per_user_requests_per_minute: int = 10
    per_group_requests_per_minute: int = 30
    max_input_characters: int = 4000
    max_output_characters: int = 12000
    max_qq_message_chars: int = 1800
    daily_chat_message_delay_min_seconds: float = 1.0
    daily_chat_message_delay_max_seconds: float = 2.0
    group_memory_max_entries: int = 100

    observe_enabled_groups: bool = True
    recent_history_tool_limit: int = 20
    # This is a safety ceiling, not the normal window size. The character budget
    # remains the primary bound and rolls history in stable low/high-watermark blocks.
    local_context_event_limit: int = 1_024
    person_memory_max_entries: int = 100
    person_group_memory_max_entries: int = 50
    preference_max_entries: int = 30
    memory_batch_seconds: float = 30.0
    memory_batch_trigger_count: int = Field(default=12, gt=0)
    memory_batch_max_events: int = Field(default=12, gt=0)
    memory_batch_max_characters: int = Field(default=8000, gt=0)
    memory_batch_max_wait_seconds: float = Field(default=300.0, ge=0)
    memory_batch_max_output_tokens: int = Field(default=4096, gt=0)
    memory_retrieval_enabled: bool = True
    self_memory_enabled: bool = True
    memory_self_reflection_enabled: bool = True
    memory_self_reflection_schedule_hours: str = "4,12,20"
    memory_self_reflection_timezone: str = "Asia/Shanghai"
    memory_self_reflection_poll_seconds: float = 60.0
    memory_self_reflection_max_batches_per_run: int = Field(
        default=12,
        validation_alias=AliasChoices(
            "MEMORY_SELF_REFLECTION_MAX_BATCHES_PER_RUN",
            "MEMORY_SELF_REFLECTION_MAX_SESSIONS_PER_RUN",
        ),
    )
    memory_self_reflection_max_batches_per_conversation_per_run: int = 7
    memory_self_reflection_max_daily_calls: int = 36
    memory_self_reflection_event_threshold: int = 50
    memory_self_reflection_character_threshold: int = 8000
    memory_self_reflection_low_event_threshold: int = 30
    memory_self_reflection_low_character_threshold: int = 4800
    memory_self_reflection_natural_gap_seconds: float = 300.0
    memory_self_reflection_max_wait_seconds: float = 28800.0
    memory_self_reflection_max_events: int = 100
    memory_self_reflection_max_characters: int = 8000
    memory_self_reflection_max_output_tokens: int = 2400
    memory_self_reflection_tool_receipt_characters: int = 2000
    memory_self_reflection_tool_receipt_retention_days: int = 7
    memory_max_referenced_targets: int = 5
    memory_lexical_candidate_limit: int = 50
    memory_context_limit_per_entity: int = 4
    memory_overview_limit_per_entity: int = 10
    memory_automatic_recall_per_target_limit: int = Field(default=2, gt=0, le=20)
    memory_automatic_recall_background_limit: int = Field(default=2, gt=0, le=20)
    memory_automatic_recall_continuation_limit: int = Field(default=2, gt=0, le=20)
    memory_automatic_recall_focused_limit: int = Field(default=3, gt=0, le=20)
    memory_automatic_recall_overview_limit: int = Field(default=4, gt=0, le=20)
    memory_always_on_explicit_preference_limit: int = 3
    memory_query_term_limit: int = 12
    memory_short_query_fallback_enabled: bool = True
    memory_semantic_enabled: bool = True
    memory_semantic_candidate_limit: int = 50
    memory_semantic_min_similarity: float = 0.35
    memory_hybrid_lexical_weight: float = 1.0
    memory_hybrid_semantic_weight: float = 1.0
    memory_hybrid_rrf_k: int = 60
    memory_intent_rerank_enabled: bool = True
    memory_activation_ranking_enabled: bool = True
    memory_usage_attribution_enabled: bool = True
    memory_usage_attribution_timeout_seconds: float = Field(default=12.0, gt=0, le=120)
    memory_usage_attribution_job_ttl_seconds: float = Field(default=120.0, gt=0, le=3600)
    memory_usage_attribution_queue_limit: int = Field(default=128, gt=0, le=4096)
    memory_reinforcement_enabled: bool = True
    memory_recall_receipts_enabled: bool = True
    memory_activation_half_life_episode_days: float = Field(default=14.0, gt=0)
    memory_activation_half_life_fact_days: float = Field(default=60.0, gt=0)
    memory_activation_half_life_preference_days: float = Field(default=120.0, gt=0)
    memory_activation_half_life_explicit_days: float = Field(default=365.0, gt=0)
    memory_reinforcement_alpha_background: float = Field(default=0.05, ge=0, le=1)
    memory_reinforcement_alpha_continuation: float = Field(default=0.12, ge=0, le=1)
    memory_reinforcement_alpha_recall: float = Field(default=0.25, ge=0, le=1)
    memory_reinforcement_alpha_verify: float = Field(default=0.08, ge=0, le=1)
    memory_intent_recent_window_days: int = Field(default=90, gt=0)
    memory_recall_receipt_retention_days: int = Field(default=30, gt=0)
    memory_recall_trace_candidate_limit: int = Field(default=20, gt=0, le=100)
    memory_consolidation_enabled: bool = True
    memory_consolidation_candidate_limit: int = 12
    memory_consolidation_min_relevance: float = 0.25
    memory_consolidation_model_task: str = "memory_consolidation"
    memory_consolidation_max_output_tokens: int = 1200
    memory_dream_enabled: bool = True
    memory_dream_schedule_hour: int = Field(default=5, ge=0, le=23)
    memory_dream_timezone: str = "Asia/Shanghai"
    memory_dream_poll_seconds: float = Field(default=60.0, gt=0)
    memory_dream_max_clusters_per_run: int = Field(default=12, gt=0, le=100)
    memory_dream_max_model_calls_per_run: int = Field(default=12, gt=0, le=200)
    memory_dream_similarity_threshold: float = Field(default=0.70, ge=-1, le=1)
    memory_dream_max_cluster_size: int = Field(default=6, ge=2, le=20)
    memory_dream_max_input_characters: int = Field(default=24_000, gt=0, le=100_000)
    memory_dream_max_output_tokens: int = Field(default=2400, gt=0)
    memory_dream_episode_max_characters: int = Field(default=800, ge=200, le=4000)
    memory_dream_episode_compression_ratio: float = Field(default=0.45, gt=0, le=1)
    memory_dream_episode_hard_compression_ratio: float = Field(default=0.70, gt=0, le=1)
    memory_dream_evidence_per_fact: int = Field(default=2, ge=1, le=10)
    memory_dream_evidence_excerpt_characters: int = Field(default=300, gt=0, le=2000)
    memory_evidence_compaction_enabled: bool = True
    memory_evidence_compaction_batch_size: int = Field(default=20, ge=1, le=100)
    memory_mmr_enabled: bool = True
    memory_mmr_lambda: float = Field(default=0.75, ge=0, le=1)
    memory_mmr_candidate_pool_size: int = Field(default=20, gt=0, le=100)
    memory_evidence_weight_explicit: float = 1.0
    memory_evidence_weight_self: float = 0.9
    memory_evidence_weight_group: float = 0.7
    memory_evidence_weight_third_party: float = 0.55
    memory_evidence_weight_rebuild: float = 0.75
    memory_authority_cap_explicit: float = 1.0
    memory_authority_cap_self: float = 0.98
    memory_authority_cap_group: float = 0.9
    memory_authority_cap_third_party: float = 0.75
    memory_maintenance_enabled: bool = True
    memory_maintenance_interval_seconds: float = 300.0
    memory_maintenance_batch_limit: int = 100
    memory_automatic_stale_days: int = 180
    memory_third_party_stale_days: int = 30
    memory_contested_stale_days: int = 14
    memory_stale_max_importance: int = 2
    memory_stale_max_confidence: float = 0.7

    memory_embedding_enabled: bool = False
    memory_embedding_provider: str = "qwen_dashscope"
    memory_embedding_base_url: str = ""
    memory_embedding_api_key: str = Field(default="", repr=False)
    memory_embedding_model: str = "qwen3.7-text-embedding"
    memory_embedding_dimensions: int = 1024
    memory_embedding_output_type: str = "dense"
    memory_embedding_document_template_version: int = 1
    memory_embedding_query_instruct: str = (
        "Retrieve personal memory facts relevant to the conversational query."
    )
    memory_embedding_request_timeout_seconds: float = 20.0
    memory_embedding_max_text_characters: int = 4000
    memory_embedding_worker_enabled: bool = True
    memory_embedding_worker_interval_seconds: float = 5.0
    memory_embedding_worker_claim_limit: int = 100
    memory_embedding_retry_attempts: int = 5
    memory_embedding_retry_initial_seconds: float = 30.0
    memory_embedding_http_concurrency: int = 2
    memory_embedding_query_cache_ttl_seconds: float = 600.0
    memory_embedding_query_cache_max_entries: int = 512
    memory_rebuild_enabled: bool = False
    memory_rebuild_worker_interval_seconds: float = 5.0
    memory_rebuild_scan_batch_size: int = 100
    memory_rebuild_extraction_concurrency: int = 2
    memory_rebuild_commit_batch_size: int = 20
    memory_rebuild_context_event_limit: int = 8
    memory_rebuild_retry_attempts: int = 5
    memory_rebuild_retry_initial_seconds: float = 30.0
    memory_rebuild_review_page_size: int = 20
    memory_rebuild_source_excerpt_characters: int = 500
    memory_rebuild_max_events_per_run: int | None = None
    agent_max_tool_calls: int = 12
    agent_max_model_requests: int = 13
    agent_tool_result_max_characters: int = 8000

    # Generous initial Tool Kernel budgets keep schemas bounded without reducing authority;
    # request_tools can still load additional actor-authorized capabilities on demand.
    tooling_max_parallel_calls: int = 8
    tooling_selected_tool_limit: int | None = 32
    tooling_schema_token_budget: int | None = 12000
    tooling_result_token_budget: int | None = None
    tooling_result_item_limit: int | None = None
    tooling_result_artifact_enabled: bool = True
    tooling_result_artifact_retention_seconds: int = 86400

    # Generic MCP client. Only MCP_CONFIG_PATH is inspected; no other client config is imported.
    mcp_enabled: bool = False
    mcp_config_path: Path = Path(".mcp.json")
    mcp_cache_enabled: bool = True
    mcp_gateway_enabled: bool = True
    mcp_metadata_cache_ttl_seconds: int = 3600
    mcp_connect_timeout_seconds: float = 15.0
    mcp_request_timeout_seconds: float = 60.0
    mcp_selected_tool_limit: int | None = 16
    mcp_schema_token_budget: int | None = 8000
    mcp_result_token_budget: int | None = None
    mcp_result_item_limit: int | None = None
    mcp_max_parallel_calls: int = 8
    mcp_artifact_retention_seconds: int = 86400

    conversation_autonomous_enabled: bool = True
    conversation_autonomous_debounce_seconds: float = 3.0
    conversation_autonomous_admission_threshold: int = 0
    conversation_autonomous_batch_limit: int = 8
    conversation_autonomous_presence_window_seconds: int = 300
    conversation_interrupt_autonomous_on_new_message: bool = True
    conversation_rollup_enabled: bool = True
    conversation_rollup_worker_concurrency: int = Field(default=1, ge=1, le=2)
    conversation_rollup_poll_seconds: float = Field(default=1.0, gt=0)
    conversation_rollup_lease_seconds: int = Field(default=180, gt=0)
    conversation_rollup_model_timeout_seconds: float = Field(default=60.0, gt=0)
    conversation_rollup_raw_tail_events: int = Field(default=768, ge=1)
    conversation_rollup_raw_tail_characters: int = Field(default=65_536, ge=1)
    conversation_rollup_trigger_events: int = Field(default=512, ge=2)
    conversation_rollup_trigger_characters: int = Field(default=49_152, ge=1)
    conversation_rollup_stop_events: int = Field(default=192, ge=0)
    conversation_rollup_stop_characters: int = Field(default=16_384, ge=0)
    conversation_rollup_batch_max_events: int = Field(default=256, ge=1)
    conversation_rollup_batch_max_characters: int = Field(default=32_768, ge=1)
    conversation_rollup_worker_max_batches_per_claim: int = Field(default=4, ge=1)
    conversation_rollup_summary_max_characters: int = Field(default=1200, ge=1)
    conversation_rollup_retry_max_seconds: int = Field(default=960, ge=1)
    conversation_rollup_lease_heartbeat_seconds: float = Field(default=60.0, gt=0)
    conversation_rollup_llm_origins: str = "user_message"
    conversation_rollup_prompt_version: str = "conversation-rollup-v2"
    conversation_rollup_foreground_max_batches: int = Field(default=3, ge=1)
    conversation_effect_gate_timeout_seconds: float = Field(default=30.0, gt=0)
    conversation_history_around_before: int = Field(default=6, ge=0)
    conversation_history_around_after: int = Field(default=6, ge=0)
    conversation_history_around_limit: int = Field(default=24, ge=1)
    reply_sequence_cancel_on_new_message: bool = True
    reply_hard_max_messages: int = 10

    # Local in-process plugins.  Approval is API governance, not a Python sandbox.
    plugin_system_enabled: bool = False
    plugin_directory: Path = Path("plugins")
    plugin_api_version: str = "2.0"
    plugin_direct_command_bindings: dict[str, str] = Field(default_factory=dict)
    plugin_hook_timeout_seconds: float = 3.0
    plugin_start_timeout_seconds: float = 10.0
    plugin_stop_timeout_seconds: float = 10.0
    plugin_max_prompt_fragment_characters: int = 2000
    plugin_max_prompt_characters_per_plugin: int = 4000
    plugin_max_total_prompt_characters: int = 8000
    plugin_background_task_limit: int = 4
    plugin_failure_disable_threshold: int = 3
    plugin_http_max_response_bytes: int = 2_097_152
    plugin_http_timeout_seconds: float = 15.0
    plugin_ai_session_max_history_messages: int = 200
    plugin_external_event_context_limit: int = 10
    plugin_external_event_context_characters: int = 6000

    relationship_enabled: bool = True
    relationship_initial_affection: int = 50
    relationship_initial_trust: int = 50
    relationship_batch_seconds: float = 60.0
    relationship_batch_trigger_count: int = 5
    relationship_batch_max_turns: int = 10
    relationship_max_attempts: int = 3
    relationship_confidence_threshold: float = 0.75
    affection_max_auto_delta: int = 2
    trust_max_auto_delta: int = 2
    # Zero deliberately means unlimited, preserving the 1.2 relationship behavior.
    relationship_daily_positive_cap: int = 0
    relationship_daily_negative_cap: int = 0
    trust_affection_cap_offset: int = 10
    conflict_preference_min_gap: int = 15

    web_enabled: bool = False
    web_mode: WebMode | None = None
    tavily_api_key: str = Field(default="", repr=False)
    web_search_depth: str = "advanced"
    web_search_max_results: int = 5
    web_extract_max_results: int = 3
    web_timeout_seconds: float = 20.0
    web_max_retries: int = 1
    web_global_concurrency: int = 4
    web_max_calls_per_turn: int = 3
    web_tool_result_max_characters: int = 16000
    web_source_retention_days: int = 7
    web_source_max_runs_per_conversation: int = 10
    web_tavily_domains_csv: str = Field(default="", validation_alias="WEB_TAVILY_DOMAINS")
    web_allow_provider_override: bool = True
    web_fallback_on_access_denied: bool = True
    web_fallback_on_target_miss: bool = True

    vision_enabled: bool = False
    vision_provider: str = "qwen"
    vision_base_url: str = ""
    vision_api_key: str = Field(default="", repr=False)
    vision_model: str = "qwen3.7-plus"
    vision_timeout_seconds: float = 120.0
    vision_max_retries: int = 1
    vision_global_concurrency: int = 4
    vision_queue_max_pending: int = 32
    vision_queue_timeout_seconds: float = 120.0
    vision_media_download_timeout_seconds: float = 120.0
    vision_allow_private_urls: bool = False
    vision_max_output_tokens: int = 8192
    vision_thinking_enabled: bool = False
    vision_thinking_budget: int = 6144
    vision_low_confidence_retry_threshold: float = 0.65
    vision_max_images_per_turn: int = 5
    vision_max_frames_per_turn: int = 16
    vision_gif_max_frames: int = 8
    vision_max_download_bytes: int = 20_971_520
    vision_max_prepared_bytes: int = 16_777_216
    vision_max_dimension: int = 4096
    vision_max_pixels: int = 16_777_216
    vision_per_user_requests_per_minute: int = 20
    vision_per_group_requests_per_minute: int = 60
    vision_analysis_retention_days: int = 7

    # Persistent emoji collection and reply effects. Recognition reuses the
    # configured VisionProvider; no second visual client or review pipeline exists.
    emoji_enabled: bool = True
    emoji_collection_enabled: bool = True
    emoji_collection_mode: str = "likely"
    emoji_collect_private: bool = True
    emoji_collect_group: bool = True
    emoji_auto_adopt_enabled: bool = True
    emoji_auto_adopt_min_confidence: float = 0.78
    emoji_pool_capacity: int | None = None
    emoji_replacement_mode: str = "score"
    emoji_selector_enabled: bool = True
    emoji_selector_candidate_count: int = 3
    emoji_selector_score_gap: float = 0.75
    emoji_selector_timeout_seconds: float = 2.0
    emoji_max_effects_per_reply: int = 1
    emoji_spontaneous_frequency: float = Field(default=0.15, ge=0, le=1)
    emoji_near_duplicate_enabled: bool = True
    emoji_near_duplicate_distance: int = 6
    emoji_same_emoji_cooldown_seconds: int = 300
    emoji_scope_repeat_cooldown_seconds: int = 60
    emoji_cache_retention_days: int = 30
    emoji_worker_batch_size: int = 10
    emoji_worker_poll_seconds: float = 2.0
    emoji_worker_lease_seconds: int = 120
    emoji_worker_max_attempts: int = 3
    emoji_worker_retry_delay_seconds: float = 30.0
    emoji_analysis_version: str = "emoji-v1"
    emoji_storage_root: Path = Path("data/emoji")
    emoji_preview_max_dimension: int = 512

    # Local speech uses a separate, network-isolated Genie-TTS worker.  Optional
    # limits deliberately use None to mean "no speech-specific limit".
    speech_enabled: bool = False
    speech_provider: str = "genie"
    speech_socket_path: Path = Path("/run/yuki-speech/genie.sock")
    speech_root: Path = Path("/data/speech")
    genie_data_dir: Path = Path("/data/speech/genie_data")
    speech_default_profile: str = ""
    speech_worker_start_timeout_seconds: float = 30.0
    speech_worker_request_timeout_seconds: float = 120.0
    speech_agent_effects_enabled: bool = True
    speech_default_mode: str = "optional"
    speech_split_sentence: bool = True
    speech_max_synthesis_characters: int | None = None
    speech_queue_max_pending: int | None = None
    speech_cache_retention_hours: int | None = None
    speech_private_enabled: bool = True
    speech_group_enabled: bool = True
    speech_automation_enabled: bool = True
    speech_plugin_enabled: bool = True
    speech_text_fallback_enabled: bool = True
    speech_spontaneous_frequency: float = Field(default=0.15, ge=0, le=1)
    speech_jp_katakana_enabled: bool = True

    automation_enabled: bool = False
    default_timezone: str = "Asia/Shanghai"
    automation_poll_seconds: float = 2.0
    automation_lease_seconds: int = 120
    automation_max_active_per_superuser: int = 50
    automation_max_active_per_user: int = 10
    automation_max_steps: int = 16
    automation_max_llm_calls_per_run: int = 10
    automation_max_tool_calls_per_run: int = 16
    automation_max_messages_per_run: int = 10
    automation_max_runtime_seconds: int = 600
    automation_min_interval_seconds: int = 60
    automation_default_misfire_grace_seconds: int = 1800
    automation_max_consecutive_failures: int = 3
    automation_run_retention_days: int = 30

    @field_validator("web_search_depth")
    @classmethod
    def _web_search_depth(cls, value: str) -> str:
        normalized = value.casefold()
        if normalized not in {"basic", "advanced"}:
            raise ValueError("WEB_SEARCH_DEPTH must be basic or advanced")
        return normalized

    @field_validator("emoji_collection_mode")
    @classmethod
    def _emoji_collection_mode(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"metadata_only", "likely", "all_images"}:
            raise ValueError("EMOJI_COLLECTION_MODE must be metadata_only, likely, or all_images")
        return normalized

    @field_validator("emoji_replacement_mode")
    @classmethod
    def _emoji_replacement_mode(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"off", "score", "llm", "hybrid"}:
            raise ValueError("EMOJI_REPLACEMENT_MODE must be off, score, llm, or hybrid")
        return normalized

    @field_validator(
        "speech_max_synthesis_characters",
        "speech_queue_max_pending",
        "speech_cache_retention_hours",
        mode="before",
    )
    @classmethod
    def _optional_positive_speech_limit(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, bool):
            raise ValueError("speech limit must be a positive integer or empty")
        converted = int(value) if isinstance(value, str) else value
        if not isinstance(converted, int) or converted <= 0:
            raise ValueError("speech limit must be a positive integer or empty")
        return converted

    @field_validator("memory_rebuild_max_events_per_run", mode="before")
    @classmethod
    def _optional_memory_rebuild_limit(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, bool):
            raise ValueError("MEMORY_REBUILD_MAX_EVENTS_PER_RUN must be positive or empty")
        converted = int(value) if isinstance(value, str) else value
        if not isinstance(converted, int) or converted <= 0:
            raise ValueError("MEMORY_REBUILD_MAX_EVENTS_PER_RUN must be positive or empty")
        return converted

    @field_validator("speech_provider")
    @classmethod
    def _speech_provider(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized != "genie":
            raise ValueError("SPEECH_PROVIDER must be genie")
        return normalized

    @field_validator("speech_default_mode")
    @classmethod
    def _speech_default_mode(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"text", "voice", "text_and_voice", "optional"}:
            raise ValueError("SPEECH_DEFAULT_MODE must be text, voice, text_and_voice, or optional")
        return normalized

    @field_validator("bot_display_name", "bot_voice_name")
    @classmethod
    def _single_line_bot_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character in normalized for character in "\r\n"):
            raise ValueError("bot names must be non-empty single-line values")
        return normalized

    @field_validator("default_timezone")
    @classmethod
    def _valid_default_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("DEFAULT_TIMEZONE must be a valid IANA timezone") from exc
        return normalized

    @field_validator("memory_dream_timezone")
    @classmethod
    def _valid_memory_dream_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("MEMORY_DREAM_TIMEZONE must be a valid IANA timezone") from exc
        return normalized

    @field_validator("plugin_api_version")
    @classmethod
    def _valid_plugin_api_version(cls, value: str) -> str:
        normalized = value.strip()
        if re.fullmatch(r"[1-9][0-9]*\.[0-9]+", normalized) is None:
            raise ValueError("PLUGIN_API_VERSION must use major.minor format")
        return normalized

    @field_validator("plugin_directory")
    @classmethod
    def _safe_plugin_directory(cls, value: Path) -> Path:
        path = Path(value)
        resolved = path.resolve(strict=False)
        sensitive = {Path(resolved.anchor), Path.home().resolve(strict=False)}
        if resolved in sensitive:
            raise ValueError("PLUGIN_DIRECTORY must not point to a filesystem root or home")
        lowered = {part.casefold() for part in resolved.parts}
        if {"windows", "system32"}.issubset(lowered):
            raise ValueError("PLUGIN_DIRECTORY must not point to a system directory")
        return path

    @field_validator("plugin_direct_command_bindings")
    @classmethod
    def _valid_direct_command_bindings(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_direct_command_bindings(value)

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_conversation_history_settings(cls, value: object) -> object:
        removed = {
            "conversation_history_rollup_enabled",
            "conversation_history_rollup_worker_concurrency",
            "conversation_history_rollup_poll_seconds",
            "conversation_history_rollup_lease_seconds",
            "conversation_history_rollup_timeout_seconds",
            "conversation_history_rollup_retry_seconds",
            "conversation_history_raw_tail_events",
            "conversation_history_raw_tail_characters",
            "conversation_history_extractive_max_characters",
            "conversation_history_llm_origins",
            "conversation_history_rollup_prompt_version",
            "conversation_history_raw_tail_budget_ratio",
            "conversation_history_sync_extractive_max_slices",
        }
        supplied = {str(key).casefold() for key in value} if isinstance(value, dict) else set()
        supplied.update(key.casefold() for key in os.environ)
        removed_family = re.compile(
            r"^conversation_history_rollup_(?:"
            r"max_(?:attempts|level)|"
            r"l0_(?:min|max)_(?:events|characters)|"
            r"fan_(?:in|in_characters)"
            r")$"
        )
        found = sorted(
            (removed & supplied) | {key for key in supplied if removed_family.fullmatch(key)}
        )
        if found:
            names = ", ".join(name.upper() for name in found)
            raise ValueError(f"removed 3.6 conversation history settings are not accepted: {names}")
        return value

    @model_validator(mode="after")
    def _validate_conversation_rollup_settings(self) -> Self:
        if self.conversation_rollup_trigger_events <= self.conversation_rollup_stop_events:
            raise ValueError(
                "CONVERSATION_ROLLUP_TRIGGER_EVENTS must exceed CONVERSATION_ROLLUP_STOP_EVENTS"
            )
        if self.conversation_rollup_trigger_characters <= self.conversation_rollup_stop_characters:
            raise ValueError(
                "CONVERSATION_ROLLUP_TRIGGER_CHARACTERS must exceed "
                "CONVERSATION_ROLLUP_STOP_CHARACTERS"
            )
        if (
            self.conversation_rollup_lease_heartbeat_seconds
            > self.conversation_rollup_lease_seconds / 3
        ):
            raise ValueError(
                "CONVERSATION_ROLLUP_LEASE_HEARTBEAT_SECONDS must not exceed one third "
                "of CONVERSATION_ROLLUP_LEASE_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _direct_bindings_do_not_shadow_ai_prefix(self) -> Self:
        ai_prefix = self.ai_prefix.strip()
        if not ai_prefix:
            return self
        for prefix in self.plugin_direct_command_bindings:
            if prefix.startswith(ai_prefix) or ai_prefix.startswith(prefix):
                raise ValueError(
                    f"PLUGIN_DIRECT_COMMAND_BINDINGS must not overlap AI_PREFIX: {prefix!r}"
                )
        return self

    @model_validator(mode="after")
    def _load_system_prompt_file(self) -> Self:
        """Load the shared UTF-8 persona without changing prompt assembly semantics."""

        persona_file = self.bot_persona_file or self.yuki_persona_file or Path("config/persona.md")
        if (
            self.bot_persona_file is None
            and persona_file == Path("config/yuki_persona_core.md")
            and not persona_file.exists()
        ):
            persona_file = Path("config/persona.md")
        persona_setting = (
            "BOT_PERSONA_FILE"
            if self.bot_persona_file is not None or self.yuki_persona_file is None
            else "YUKI_PERSONA_FILE"
        )
        try:
            persona = persona_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read {persona_setting}: {persona_file}") from exc
        if not persona:
            raise ValueError(f"{persona_setting} must not be empty")
        self._bot_persona = persona

        if self.system_prompt_file is not None:
            try:
                prompt = self.system_prompt_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(
                    f"cannot read SYSTEM_PROMPT_FILE: {self.system_prompt_file}"
                ) from exc
            if not prompt:
                raise ValueError("SYSTEM_PROMPT_FILE must not be empty")
            self.system_prompt = prompt
        self.system_prompt = self.system_prompt.replace(
            "{{YUKI_PERSONA_CORE}}",
            self._bot_persona,
        )
        return self

    @property
    def bot_persona(self) -> str:
        return self._bot_persona

    @property
    def yuki_persona(self) -> str:
        """Compatibility alias for older callers."""

        return self._bot_persona

    @model_validator(mode="after")
    def _compose_domain_settings(self) -> Self:
        """Validate every environment value at its owning domain boundary."""

        _ = (
            self.app,
            self.onebot,
            self.model_runtime,
            self.conversation,
            self.plugins,
            self.memory,
            self.relationship,
            self.web,
            self.vision,
            self.emoji,
            self.speech,
            self.automation,
            self.tooling,
            self.mcp,
        )
        return self

    @model_validator(mode="after")
    def _validate_embedding_settings(self) -> Self:
        if self.memory_embedding_provider != "qwen_dashscope":
            raise ValueError("MEMORY_EMBEDDING_PROVIDER must be qwen_dashscope")
        if self.memory_embedding_output_type != "dense":
            raise ValueError("MEMORY_EMBEDDING_OUTPUT_TYPE must be dense")
        if self.memory_embedding_dimensions != 1024:
            raise ValueError("qwen_dashscope currently supports 1024 dimensions")
        if self.memory_embedding_document_template_version != 1:
            raise ValueError("unsupported MEMORY_EMBEDDING_DOCUMENT_TEMPLATE_VERSION")
        if self.memory_embedding_enabled and not (
            self.memory_embedding_base_url.strip() and self.memory_embedding_api_key
        ):
            raise ValueError(
                "MEMORY_EMBEDDING_BASE_URL and MEMORY_EMBEDDING_API_KEY are required "
                "when MEMORY_EMBEDDING_ENABLED=true"
            )
        return self

    @model_validator(mode="after")
    def _validate_memory_consolidation_settings(self) -> Self:
        if self.memory_consolidation_model_task != "memory_consolidation":
            raise ValueError("MEMORY_CONSOLIDATION_MODEL_TASK must be memory_consolidation")
        if self.memory_third_party_stale_days > self.memory_automatic_stale_days:
            raise ValueError(
                "MEMORY_THIRD_PARTY_STALE_DAYS must not exceed MEMORY_AUTOMATIC_STALE_DAYS"
            )
        return self

    @cached_property
    def app(self) -> AppSettings:
        return AppSettings.model_validate(self)

    @cached_property
    def onebot(self) -> OneBotSettings:
        return OneBotSettings.model_validate(self)

    @cached_property
    def model_runtime(self) -> ModelRuntimeSettings:
        return ModelRuntimeSettings.model_validate(self)

    @cached_property
    def conversation(self) -> ConversationSettings:
        return ConversationSettings.model_validate(self)

    @cached_property
    def plugins(self) -> PluginSettings:
        return PluginSettings.model_validate(self)

    @cached_property
    def memory(self) -> MemorySettings:
        return MemorySettings.model_validate(self)

    @cached_property
    def relationship(self) -> RelationshipSettings:
        return RelationshipSettings.model_validate(self)

    @cached_property
    def web(self) -> WebSettings:
        return WebSettings.model_validate(self)

    @cached_property
    def vision(self) -> VisionSettings:
        return VisionSettings.model_validate(self)

    @cached_property
    def emoji(self) -> EmojiSettings:
        return EmojiSettings.model_validate(self)

    @cached_property
    def speech(self) -> SpeechSettings:
        return SpeechSettings.model_validate(self)

    @cached_property
    def automation(self) -> AutomationSettings:
        return AutomationSettings.model_validate(self)

    @cached_property
    def tooling(self) -> ToolingSettings:
        return ToolingSettings.model_validate(self)

    @cached_property
    def mcp(self) -> MCPSettings:
        return MCPSettings.model_validate(self)

    @cached_property
    def superusers(self) -> frozenset[str]:
        return _csv_set(self.superusers_csv)

    @cached_property
    def enabled_groups(self) -> frozenset[str]:
        return _csv_set(self.enabled_groups_csv)

    @cached_property
    def ignored_bot_users(self) -> frozenset[str]:
        return _csv_set(self.ignored_bot_users_csv)

    @cached_property
    def bot_aliases(self) -> tuple[str, ...]:
        aliases = (self.bot_display_name, *_csv_tuple(self.bot_aliases_csv))
        seen: set[str] = set()
        normalized: list[str] = []
        for alias in aliases:
            key = alias.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(alias)
        return tuple(normalized)

    @cached_property
    def bot_identity(self) -> BotIdentity:
        return BotIdentity(
            display_name=self.bot_display_name,
            aliases=self.bot_aliases,
            voice_name=self.bot_voice_name,
        )

    @property
    def web_tavily_domains(self) -> frozenset[str]:
        """Domains routed directly to Tavily in hybrid web mode."""

        return _csv_set(self.web_tavily_domains_csv)

    @property
    def sqlite_path(self) -> Path | None:
        """Return the local SQLite path without exposing it in user-facing output."""

        prefix = "sqlite+aiosqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        return Path(self.database_url.removeprefix(prefix))

    @property
    def llm_configured(self) -> bool:
        """Whether enough configuration exists to make LLM requests."""

        if self.llm_provider.casefold() == "fake":
            return True
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def web_configured(self) -> bool:
        """Whether controlled web search is enabled with provider credentials."""

        mode = self.web.mode
        return bool(
            mode is not WebMode.DISABLED and (mode is WebMode.NATIVE or bool(self.tavily_api_key))
        )

    @property
    def vision_configured(self) -> bool:
        """Whether vision is enabled with all required provider configuration."""

        return bool(
            self.vision_enabled
            and self.vision_base_url.strip()
            and self.vision_api_key.strip()
            and self.vision_model.strip()
        )

    @property
    def memory_embedding_configured(self) -> bool:
        return bool(
            self.memory_embedding_enabled
            and self.memory_embedding_base_url.strip()
            and self.memory_embedding_api_key
        )
