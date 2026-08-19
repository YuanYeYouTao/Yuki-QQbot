"""Settings tests for external system prompt files."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import ReasoningEffort


def test_deepseek_reasoning_effort_accepts_max() -> None:
    settings = Settings(_env_file=None, llm_reasoning_effort="max")

    assert settings.llm_reasoning_effort is ReasoningEffort.MAX
    assert settings.model_runtime.llm_reasoning_effort is ReasoningEffort.MAX


def test_memory_dream_hard_compression_ratio_cannot_be_below_target() -> None:
    with pytest.raises(
        ValidationError,
        match="hard compression ratio cannot be below its target ratio",
    ):
        Settings(
            _env_file=None,
            memory_dream_episode_compression_ratio=0.70,
            memory_dream_episode_hard_compression_ratio=0.45,
        )


def test_memory_dream_compression_defaults_keep_quality_headroom() -> None:
    settings = Settings(_env_file=None)

    assert settings.memory_dream_episode_compression_ratio == 0.45
    assert settings.memory_dream_episode_hard_compression_ratio == 0.70


def test_system_prompt_file_overrides_inline_prompt(tmp_path: Path) -> None:
    prompt_file = tmp_path / "system_prompt.md"
    prompt_file.write_text("# Role\n\nExternal prompt\n", encoding="utf-8")

    settings = Settings.model_validate(
        {
            "system_prompt": "inline prompt",
            "system_prompt_file": prompt_file,
        }
    )

    assert settings.system_prompt == "# Role\n\nExternal prompt"


def test_system_prompt_file_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot read SYSTEM_PROMPT_FILE"):
        Settings.model_validate(
            {
                "system_prompt_file": tmp_path / "missing.md",
            }
        )


def test_system_prompt_file_must_not_be_empty(tmp_path: Path) -> None:
    prompt_file = tmp_path / "empty.md"
    prompt_file.write_text(" \n", encoding="utf-8")

    with pytest.raises(ValidationError, match="SYSTEM_PROMPT_FILE must not be empty"):
        Settings.model_validate({"system_prompt_file": prompt_file})


def test_yuki_persona_file_is_required_and_expands_fixed_placeholder(
    tmp_path: Path,
) -> None:
    persona_file = tmp_path / "persona.md"
    persona_file.write_text("Yuki 的共享人格", encoding="utf-8")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(
        "before\n{{YUKI_PERSONA_CORE}}\nafter",
        encoding="utf-8",
    )

    settings = Settings.model_validate(
        {
            "BOT_PERSONA_FILE": None,
            "yuki_persona_file": persona_file,
            "system_prompt_file": prompt_file,
        }
    )

    assert settings.yuki_persona == "Yuki 的共享人格"
    assert settings.system_prompt == "before\nYuki 的共享人格\nafter"


def test_yuki_persona_file_must_exist_and_not_be_empty(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot read YUKI_PERSONA_FILE"):
        Settings.model_validate(
            {
                "BOT_PERSONA_FILE": None,
                "yuki_persona_file": tmp_path / "missing.md",
            }
        )

    empty = tmp_path / "empty-persona.md"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="YUKI_PERSONA_FILE must not be empty"):
        Settings.model_validate(
            {
                "BOT_PERSONA_FILE": None,
                "yuki_persona_file": empty,
            }
        )


def test_legacy_prompt_without_placeholder_is_not_duplicated(tmp_path: Path) -> None:
    persona_file = tmp_path / "persona.md"
    persona_file.write_text("shared persona", encoding="utf-8")
    prompt_file = tmp_path / "legacy.md"
    prompt_file.write_text("legacy prompt already contains its persona", encoding="utf-8")

    settings = Settings.model_validate(
        {
            "yuki_persona_file": persona_file,
            "system_prompt_file": prompt_file,
        }
    )

    assert settings.system_prompt == "legacy prompt already contains its persona"


def test_bot_identity_is_configurable_and_aliases_are_stably_deduplicated() -> None:
    settings = Settings.model_validate(
        {
            "BOT_DISPLAY_NAME": "Mika",
            "BOT_ALIASES": "Mika,mika,米卡, MIKA ",
            "BOT_VOICE_NAME": "みか",
        }
    )

    assert settings.bot_display_name == "Mika"
    assert settings.bot_aliases == ("Mika", "米卡")
    assert settings.bot_voice_name == "みか"
    assert settings.bot_identity.display_name == "Mika"


def test_bot_persona_does_not_modify_prompt_without_legacy_placeholder(
    tmp_path: Path,
) -> None:
    persona_file = tmp_path / "persona.md"
    persona_file.write_text("Mika 的独立共享人格", encoding="utf-8")
    prompt_file = tmp_path / "system_prompt.md"
    original_prompt = "# 私有系统提示词\n\n这里不包含任何人格占位符。"
    prompt_file.write_text(original_prompt, encoding="utf-8")

    settings = Settings.model_validate(
        {
            "BOT_PERSONA_FILE": persona_file,
            "system_prompt_file": prompt_file,
        }
    )

    assert settings.bot_persona == "Mika 的独立共享人格"
    assert settings.system_prompt == original_prompt


def test_example_system_prompt_preserves_yuki_persona_and_short_style() -> None:
    prompt_path = Path(__file__).parents[2] / "config" / "system_prompt.example.md"
    persona_path = Path(__file__).parents[2] / "config" / "persona.md"
    template = prompt_path.read_text(encoding="utf-8")
    persona = persona_path.read_text(encoding="utf-8")
    assert template.count("{{YUKI_PERSONA_CORE}}") == 1
    prompt = template.replace("{{YUKI_PERSONA_CORE}}", persona)

    required_fragments = (
        "生日是 7 月 23 日",
        "银白色长发",
        "蓝色兔耳形发带",
        "雪花发饰",
        "白色水手服",
        "默认只说一句",
        "通常控制在 50 个中文字符以内",
        "日常聊天使用短句和常用词",
        "普通短回复不使用中文句号“。”收尾",
        "日常聊天不使用括号动作、场景描写、心理旁白",
        "必须由用户明确提出这种表达方式",
        "不使用 Unicode Emoji",
        "不使用颜文字、ASCII 表情",
        "这些只是反应方向，不是固定台词",
        "作为自己名字或自称出现的英文 Yuki 写成平假名“ゆき”",
    )
    assert all(fragment in prompt for fragment in required_fragments)


def test_daily_chat_delay_range_must_be_ordered() -> None:
    with pytest.raises(
        ValidationError,
        match="daily chat minimum delay must not exceed",
    ):
        Settings.model_validate(
            {
                "daily_chat_message_delay_min_seconds": 3,
                "daily_chat_message_delay_max_seconds": 1,
            }
        )


def test_memory_embedding_disabled_needs_no_secret_but_enabled_does() -> None:
    disabled = Settings.model_validate(
        {
            "memory_embedding_enabled": False,
            "memory_embedding_base_url": "",
            "memory_embedding_api_key": "",
        }
    )
    assert disabled.memory_embedding_configured is False

    with pytest.raises(ValidationError, match="MEMORY_EMBEDDING_BASE_URL"):
        Settings.model_validate(
            {
                "memory_embedding_enabled": True,
                "memory_embedding_base_url": "",
                "memory_embedding_api_key": "",
            }
        )

    enabled = Settings.model_validate(
        {
            "memory_embedding_enabled": True,
            "memory_embedding_base_url": "https://workspace.example/api/v1",
            "memory_embedding_api_key": "test-only-key",
        }
    )
    assert enabled.memory_embedding_configured is True
    assert "test-only-key" not in repr(enabled)


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"memory_embedding_provider": "other"}, "must be qwen_dashscope"),
        ({"memory_embedding_dimensions": 768}, "supports 1024 dimensions"),
        ({"memory_embedding_output_type": "sparse"}, "must be dense"),
        ({"memory_embedding_document_template_version": 2}, "unsupported"),
    ],
)
def test_memory_embedding_rejects_unsupported_profiles(
    override: dict[str, object], error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        Settings.model_validate(override)


def test_planner_and_plugin_defaults_are_domain_validated_without_arbitrary_caps() -> None:
    settings = Settings(_env_file=None)
    assert settings.daily_chat_message_delay_min_seconds == 1
    assert settings.daily_chat_message_delay_max_seconds == 2
    assert settings.conversation_autonomous_debounce_seconds == 3
    assert settings.conversation_autonomous_admission_threshold == 0
    assert settings.conversation_autonomous_batch_limit == 8
    assert settings.reply_hard_max_messages == 10
    assert settings.max_context_characters == 12_000
    assert settings.local_context_event_limit == 1_000
    assert settings.history_window_low_watermark_ratio == 0.67
    assert settings.context_metadata_budget_ratio == 0.35
    assert settings.memory_context_limit_per_entity == 4
    assert settings.memory_automatic_recall_continuation_limit == 2
    assert settings.conversation_history_raw_tail_events == 32
    assert settings.conversation_history_raw_tail_characters == 1600
    assert settings.conversation_history_raw_tail_budget_ratio == 0.40
    assert settings.conversation_history_sync_extractive_max_slices == 3
    assert settings.tooling_selected_tool_limit == 32
    assert settings.tooling_schema_token_budget == 12000
    assert settings.mcp_selected_tool_limit == 16
    assert settings.mcp_schema_token_budget == 8000
    assert settings.agent_max_tool_calls == 12
    assert settings.agent_max_model_requests == 13
    assert settings.agent_tool_result_max_characters == 8000
    assert settings.memory_self_reflection_event_threshold == 50
    assert settings.memory_self_reflection_character_threshold == 8000
    assert settings.memory_self_reflection_low_event_threshold == 30
    assert settings.memory_self_reflection_low_character_threshold == 4800
    assert settings.memory_self_reflection_natural_gap_seconds == 300
    assert settings.memory_self_reflection_max_batches_per_run == 12
    assert settings.memory_self_reflection_max_batches_per_conversation_per_run == 7
    assert settings.memory_self_reflection_max_daily_calls == 36
    assert settings.memory_self_reflection_max_events == 100
    assert settings.emoji_selector_candidate_count == 3
    assert settings.emoji_selector_score_gap == 0.75
    assert settings.emoji_selector_timeout_seconds == 2
    assert not settings.plugin_system_enabled
    assert settings.plugin_api_version == "2.0"
    assert settings.plugin_ai_session_max_history_messages == 200

    assert Settings.model_validate({"conversation_autonomous_admission_threshold": 101})
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Settings.model_validate({"conversation_autonomous_admission_threshold": -1})
    assert Settings.model_validate({"conversation_autonomous_debounce_seconds": 0})
    assert Settings.model_validate({"conversation_autonomous_debounce_seconds": 61})
    assert Settings.model_validate({"reply_hard_max_messages": 21})
    with pytest.raises(ValidationError, match="PLUGIN_API_VERSION"):
        Settings.model_validate({"plugin_api_version": "v1"})
    with pytest.raises(ValidationError, match="total plugin prompt budget"):
        Settings.model_validate(
            {
                "plugin_max_prompt_fragment_characters": 2000,
                "plugin_max_total_prompt_characters": 2000,
            }
        )


def test_memory_limits_are_configurable_positive_values() -> None:
    assert Settings.model_validate({"group_memory_max_entries": 100})
    assert Settings.model_validate({"person_group_memory_max_entries": 500})
    with pytest.raises(ValidationError, match="greater than 0"):
        Settings.model_validate({"person_group_memory_max_entries": 0})


def test_legacy_self_reflection_session_limit_env_alias_is_supported() -> None:
    settings = Settings(
        _env_file=None,
        MEMORY_SELF_REFLECTION_MAX_SESSIONS_PER_RUN=5,
        memory_self_reflection_max_batches_per_conversation_per_run=4,
    )

    assert settings.memory_self_reflection_max_batches_per_run == 5


@pytest.mark.parametrize(
    ("override", "error"),
    [
        (
            {
                "memory_self_reflection_low_event_threshold": 31,
                "memory_self_reflection_event_threshold": 30,
            },
            "low event watermark cannot exceed high watermark",
        ),
        (
            {
                "memory_self_reflection_event_threshold": 51,
                "memory_self_reflection_max_events": 50,
            },
            "high event watermark cannot exceed batch event limit",
        ),
        (
            {
                "memory_self_reflection_low_character_threshold": 6001,
                "memory_self_reflection_character_threshold": 6000,
            },
            "low character watermark cannot exceed high watermark",
        ),
        (
            {
                "memory_self_reflection_character_threshold": 8001,
                "memory_self_reflection_max_characters": 8000,
            },
            "high character watermark cannot exceed batch character limit",
        ),
    ],
)
def test_self_reflection_watermarks_must_be_ordered(
    override: dict[str, object], error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        Settings.model_validate(override)


def test_web_enabled_requires_tavily_key_and_hides_it_from_repr() -> None:
    with pytest.raises(ValidationError, match="TAVILY_API_KEY"):
        Settings.model_validate({"web_enabled": True, "tavily_api_key": ""})

    settings = Settings.model_validate(
        {
            "web_enabled": True,
            "tavily_api_key": "tvly-sensitive-test-value",
        }
    )
    assert settings.web_configured
    assert "tvly-sensitive-test-value" not in repr(settings)


def test_web_limits_are_configurable_and_search_depth_is_validated() -> None:
    assert Settings.model_validate({"web_extract_max_results": 4})
    assert Settings.model_validate({"web_max_calls_per_turn": 4})
    with pytest.raises(ValidationError, match="WEB_SEARCH_DEPTH"):
        Settings.model_validate({"web_search_depth": "unbounded"})


def test_relationship_defaults_have_no_daily_caps_and_keep_single_turn_bounds() -> None:
    settings = Settings()
    assert settings.relationship_initial_affection == 50
    assert settings.relationship_initial_trust == 50
    assert settings.affection_max_auto_delta == 2
    assert settings.trust_max_auto_delta == 2
    assert not hasattr(settings, "affection_daily_positive_cap")
    assert not hasattr(settings, "affection_daily_negative_cap")
    assert not hasattr(settings, "trust_daily_positive_cap")
    assert not hasattr(settings, "trust_daily_negative_cap")


def test_relationship_configuration_is_validated() -> None:
    assert Settings.model_validate({"affection_max_auto_delta": 3})
    assert Settings.model_validate({"relationship_batch_max_turns": 11})
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        Settings.model_validate({"relationship_confidence_threshold": 1.1})


def test_vision_defaults_are_safe_and_api_key_is_hidden() -> None:
    settings = Settings.model_validate(
        {
            "vision_enabled": False,
            "vision_api_key": "vision-sensitive-test-value",
        }
    )

    assert not settings.vision_enabled
    assert not settings.vision_configured
    assert settings.vision_provider == "qwen"
    assert settings.vision_model == "qwen3.7-plus"
    assert settings.vision_timeout_seconds == 120
    assert settings.vision_global_concurrency == 4
    assert settings.vision_queue_max_pending == 32
    assert settings.vision_queue_timeout_seconds == 120
    assert settings.vision_media_download_timeout_seconds == 120
    assert settings.vision_max_output_tokens == 8192
    assert not settings.vision_thinking_enabled
    assert settings.vision_thinking_budget == 6144
    assert settings.vision_low_confidence_retry_threshold == 0.65
    assert settings.vision_max_images_per_turn == 5
    assert settings.vision_max_frames_per_turn == 16
    assert settings.vision_gif_max_frames == 8
    assert settings.vision_max_download_bytes == 20_971_520
    assert settings.vision_max_prepared_bytes == 16_777_216
    assert settings.vision_max_dimension == 4096
    assert settings.vision_max_pixels == 16_777_216
    assert settings.vision_per_user_requests_per_minute == 20
    assert settings.vision_per_group_requests_per_minute == 60
    assert "vision-sensitive-test-value" not in repr(settings)


def test_vision_enabled_requires_complete_provider_configuration() -> None:
    with pytest.raises(ValidationError, match=r"VISION_BASE_URL.*VISION_API_KEY"):
        Settings.model_validate(
            {
                "vision_enabled": True,
                "vision_base_url": "",
                "vision_api_key": "",
                "vision_model": "qwen3.7-plus",
            }
        )

    settings = Settings.model_validate(
        {
            "vision_enabled": True,
            "vision_base_url": "https://dashscope.example/v1",
            "vision_api_key": "secret",
            "vision_model": "qwen3.7-plus",
        }
    )
    assert settings.vision_configured


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vision_max_prepared_bytes", 0),
        ("vision_timeout_seconds", 0),
        ("vision_queue_max_pending", 0),
        ("vision_queue_timeout_seconds", 0),
        ("vision_media_download_timeout_seconds", 0),
        ("vision_max_retries", 0),
        ("vision_low_confidence_retry_threshold", 1.1),
    ],
)
def test_vision_numeric_domain_constraints_are_validated(field: str, value: int | float) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


def test_vision_operational_limits_have_no_hidden_upper_clamp() -> None:
    settings = Settings.model_validate(
        {
            "vision_max_images_per_turn": 25,
            "vision_gif_max_frames": 40,
            "vision_max_frames_per_turn": 50,
            "vision_max_download_bytes": 128 * 1024 * 1024,
            "vision_max_retries": 4,
            "vision_thinking_budget": 65536,
        }
    )
    assert settings.vision_max_images_per_turn == 25
    assert settings.vision_thinking_budget == 65536
