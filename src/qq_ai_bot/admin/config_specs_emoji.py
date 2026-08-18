"""Runtime-config declarations for the persistent emoji system."""

from __future__ import annotations

from qq_ai_bot.admin.config_spec_helpers import _G, _GG, _GGU, _field, _spec
from qq_ai_bot.admin.models import ConfigApplyMode, ConfigSpec


def emoji_config_specs() -> tuple[ConfigSpec, ...]:
    """Return every supported emoji setting; no review-system setting exists."""

    boolean_specs = (
        ("emoji.enabled", "表情系统", "emoji_enabled", _GGU),
        ("emoji.collection_enabled", "表情自动收集", "emoji_collection_enabled", _GG),
        ("emoji.collect_private", "私聊表情收集", "emoji_collect_private", _G),
        ("emoji.collect_group", "群聊表情收集", "emoji_collect_group", _GG),
        ("emoji.auto_adopt_enabled", "表情自动采用", "emoji_auto_adopt_enabled", _GG),
        ("emoji.selector_enabled", "表情视觉精选", "emoji_selector_enabled", _GGU),
        ("emoji.near_duplicate_enabled", "表情近似哈希", "emoji_near_duplicate_enabled", _G),
    )
    specs: list[ConfigSpec] = [
        _spec(
            key,
            label,
            f"控制{label}是否启用。",
            value_type="boolean",
            scopes=scopes,
            env_alias=field.upper(),
            getter=_field(field),
            settings_fields=(field,),
            category="emoji",
        )
        for key, label, field, scopes in boolean_specs
    ]
    specs.extend(
        (
            _spec(
                "emoji.collection_mode",
                "表情收集模式",
                "metadata_only 只收明确表情元数据，likely 收集高可能候选，all_images 收集图片。",
                value_type="enum",
                choices=("metadata_only", "likely", "all_images"),
                scopes=_GG,
                env_alias="EMOJI_COLLECTION_MODE",
                getter=_field("emoji_collection_mode"),
                settings_fields=("emoji_collection_mode",),
                category="emoji",
            ),
            _spec(
                "emoji.auto_adopt_min_confidence",
                "自动采用置信度",
                "视觉分类结果直接进入表情池所需的最低置信度。",
                value_type="number",
                minimum=0,
                maximum=1,
                scopes=_GG,
                env_alias="EMOJI_AUTO_ADOPT_MIN_CONFIDENCE",
                getter=_field("emoji_auto_adopt_min_confidence"),
                settings_fields=("emoji_auto_adopt_min_confidence",),
                category="emoji",
            ),
            _spec(
                "emoji.pool_capacity",
                "表情池容量",
                "采用表情的作用域容量；环境变量未配置时为无限。",
                value_type="integer",
                minimum=1,
                maximum=100000,
                scopes=_GG,
                env_alias="EMOJI_POOL_CAPACITY",
                getter=_field("emoji_pool_capacity"),
                settings_fields=("emoji_pool_capacity",),
                category="emoji",
            ),
            _spec(
                "emoji.replacement_mode",
                "表情池替换模式",
                "池满时使用 off、score、llm 或 hybrid；模型失败或返回非法 ID 时回退 score。",
                value_type="enum",
                choices=("off", "score", "llm", "hybrid"),
                scopes=_GG,
                env_alias="EMOJI_REPLACEMENT_MODE",
                getter=_field("emoji_replacement_mode"),
                settings_fields=("emoji_replacement_mode",),
                category="emoji",
            ),
        )
    )
    integer_specs = (
        (
            "emoji.selector_candidate_count",
            "候选拼图数量",
            "emoji_selector_candidate_count",
            1,
            30,
            _GGU,
        ),
        (
            "emoji.max_effects_per_reply",
            "单轮表情效果数量",
            "emoji_max_effects_per_reply",
            1,
            10,
            _GGU,
        ),
        (
            "emoji.near_duplicate_distance",
            "近似哈希距离",
            "emoji_near_duplicate_distance",
            0,
            64,
            _G,
        ),
        (
            "emoji.same_emoji_cooldown_seconds",
            "同表情冷却",
            "emoji_same_emoji_cooldown_seconds",
            0,
            86400,
            _GGU,
        ),
        (
            "emoji.scope_repeat_cooldown_seconds",
            "作用域表情冷却",
            "emoji_scope_repeat_cooldown_seconds",
            0,
            86400,
            _GG,
        ),
        (
            "emoji.cache_retention_days",
            "候选保留天数",
            "emoji_cache_retention_days",
            1,
            3650,
            _G,
        ),
        ("emoji.worker_batch_size", "表情任务批量", "emoji_worker_batch_size", 1, 100, _G),
        ("emoji.worker_lease_seconds", "表情任务租约", "emoji_worker_lease_seconds", 1, 3600, _G),
        ("emoji.worker_max_attempts", "表情任务重试次数", "emoji_worker_max_attempts", 1, 20, _G),
    )
    specs.extend(
        _spec(
            key,
            label,
            f"配置{label}。",
            value_type="integer",
            minimum=minimum,
            maximum=maximum,
            scopes=scopes,
            env_alias=field.upper(),
            getter=_field(field),
            settings_fields=(field,),
            category="emoji",
        )
        for key, label, field, minimum, maximum, scopes in integer_specs
    )
    number_specs = (
        (
            "emoji.spontaneous_frequency",
            "日常表情频率",
            "emoji_spontaneous_frequency",
            0,
            1,
            _GGU,
        ),
        (
            "emoji.selector_score_gap",
            "视觉精选分差阈值",
            "emoji_selector_score_gap",
            0,
            20,
            _GGU,
        ),
        (
            "emoji.selector_timeout_seconds",
            "表情视觉精选超时",
            "emoji_selector_timeout_seconds",
            0.1,
            30,
            _GGU,
        ),
        (
            "emoji.worker_poll_seconds",
            "表情任务轮询秒数",
            "emoji_worker_poll_seconds",
            0.1,
            3600,
            _G,
        ),
        (
            "emoji.worker_retry_delay_seconds",
            "表情任务重试间隔",
            "emoji_worker_retry_delay_seconds",
            0,
            86400,
            _G,
        ),
    )
    specs.extend(
        _spec(
            key,
            label,
            (
                "用户未明确索要表情时，日常主动表情的目标频率，范围 0..1；0.15 表示 15%。"
                if key == "emoji.spontaneous_frequency"
                else f"配置{label}。"
            ),
            aliases=(
                ("日常表情频率", "主动表情频率", "自发表情频率")
                if key == "emoji.spontaneous_frequency"
                else ()
            ),
            value_type="number",
            minimum=minimum,
            maximum=maximum,
            scopes=scopes,
            env_alias=field.upper(),
            getter=_field(field),
            settings_fields=(field,),
            category="emoji",
        )
        for key, label, field, minimum, maximum, scopes in number_specs
    )
    specs.extend(
        (
            _spec(
                "emoji.analysis_version",
                "表情分析版本",
                "改变后新任务会按新版本重新生成结构化描述。",
                value_type="string",
                scopes=_G,
                env_alias="EMOJI_ANALYSIS_VERSION",
                getter=_field("emoji_analysis_version"),
                settings_fields=("emoji_analysis_version",),
                category="emoji",
            ),
            _spec(
                "emoji.storage_root",
                "表情存储目录",
                "重启后使用的表情原图和预览根目录。",
                value_type="string",
                scopes=_G,
                mode=ConfigApplyMode.RESTART_REQUIRED,
                env_alias="EMOJI_STORAGE_ROOT",
                getter=lambda settings: str(settings.emoji_storage_root),
                settings_fields=("emoji_storage_root",),
                category="emoji",
            ),
        )
    )
    return tuple(specs)
