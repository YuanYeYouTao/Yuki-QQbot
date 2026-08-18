"""Runtime configuration declarations for the local speech subsystem."""

from __future__ import annotations

from qq_ai_bot.admin.config_spec_helpers import _GGU, _field, _path_field, _spec
from qq_ai_bot.admin.models import ConfigApplyMode, ConfigSpec


def speech_config_specs() -> tuple[ConfigSpec, ...]:
    specs = (
        *(
            _spec(
                key,
                display,
                description,
                aliases=aliases,
                value_type=value_type,
                choices=choices,
                mode=ConfigApplyMode.RESTART_REQUIRED,
                env_alias=env_alias,
                getter=(
                    _path_field(field_name)
                    if key in {"speech.socket_path", "speech.root", "genie.data_dir"}
                    else _field(field_name)
                ),
                settings_fields=(field_name,),
                category="speech",
            )
            for key, display, description, aliases, value_type, choices, env_alias, field_name in (
                (
                    "speech.enabled",
                    "本地语音开关",
                    "重启后连接本地 Genie-TTS Worker。",
                    ("语音功能开关",),
                    "boolean",
                    (),
                    "SPEECH_ENABLED",
                    "speech_enabled",
                ),
                (
                    "speech.provider",
                    "语音 Provider",
                    "重启后使用的本地语音 Provider。",
                    ("语音提供器",),
                    "enum",
                    ("genie",),
                    "SPEECH_PROVIDER",
                    "speech_provider",
                ),
                (
                    "speech.socket_path",
                    "语音 Worker Socket",
                    "重启后连接的本地 Unix Domain Socket 路径。",
                    (),
                    "string",
                    (),
                    "SPEECH_SOCKET_PATH",
                    "speech_socket_path",
                ),
                (
                    "speech.root",
                    "语音数据根目录",
                    "重启后使用的共享语音数据根目录。",
                    (),
                    "string",
                    (),
                    "SPEECH_ROOT",
                    "speech_root",
                ),
                (
                    "genie.data_dir",
                    "GenieData 目录",
                    "重启后 Worker 读取的离线 GenieData 目录。",
                    (),
                    "string",
                    (),
                    "GENIE_DATA_DIR",
                    "genie_data_dir",
                ),
            )
        ),
        _spec(
            "speech.default_profile",
            "默认声线档案",
            "之后生成语音时使用的默认 profile；profile activate 可立即切换。",
            aliases=("默认声线",),
            value_type="string",
            mode=ConfigApplyMode.FUTURE_ONLY,
            env_alias="SPEECH_DEFAULT_PROFILE",
            getter=_field("speech_default_profile"),
            settings_fields=("speech_default_profile",),
            category="speech",
        ),
        _spec(
            "speech.spontaneous_frequency",
            "日常自主语音频率",
            "用户未主动询问语音时，Conversation Runtime 允许主动语音的目标频率，范围 0..1。",
            aliases=("主动语音频率", "语音频率"),
            value_type="number",
            minimum=0,
            maximum=1,
            scopes=_GGU,
            env_alias="SPEECH_SPONTANEOUS_FREQUENCY",
            getter=_field("speech_spontaneous_frequency"),
            settings_fields=("speech_spontaneous_frequency",),
            category="speech",
        ),
        *(
            _spec(
                key,
                display,
                description,
                aliases=aliases,
                value_type=value_type,
                minimum=minimum,
                choices=choices,
                scopes=_GGU,
                env_alias=env_alias,
                getter=_field(field_name),
                settings_fields=(field_name,),
                category="speech",
            )
            for (
                key,
                display,
                description,
                aliases,
                value_type,
                minimum,
                choices,
                env_alias,
                field_name,
            ) in (
                (
                    "speech.agent_effects_enabled",
                    "Agent 语音效果",
                    "允许 Main Agent 通过 send_voice 请求语音效果。",
                    (),
                    "boolean",
                    None,
                    (),
                    "SPEECH_AGENT_EFFECTS_ENABLED",
                    "speech_agent_effects_enabled",
                ),
                (
                    "speech.default_mode",
                    "全局语音偏好基线",
                    "未保存人物语音偏好时使用：text 禁止日常主动语音，optional 自动决定，"
                    "voice/text_and_voice 偏好语音；本轮最终模式仍由 Main Agent 决定。",
                    (),
                    "enum",
                    None,
                    ("text", "voice", "text_and_voice", "optional"),
                    "SPEECH_DEFAULT_MODE",
                    "speech_default_mode",
                ),
                (
                    "speech.split_sentence",
                    "语音按句切分",
                    "生成时是否让 Genie 按句切分。",
                    (),
                    "boolean",
                    None,
                    (),
                    "SPEECH_SPLIT_SENTENCE",
                    "speech_split_sentence",
                ),
                (
                    "speech.max_synthesis_characters",
                    "语音合成字符上限",
                    "单次语音合成字符上限；未配置则只受现有输出限制。",
                    (),
                    "integer",
                    1,
                    (),
                    "SPEECH_MAX_SYNTHESIS_CHARACTERS",
                    "speech_max_synthesis_characters",
                ),
                (
                    "speech.queue_max_pending",
                    "语音排队上限",
                    "等待生成的请求上限；未配置则不主动拒绝排队。",
                    (),
                    "integer",
                    1,
                    (),
                    "SPEECH_QUEUE_MAX_PENDING",
                    "speech_queue_max_pending",
                ),
                (
                    "speech.cache_retention_hours",
                    "语音缓存保留小时",
                    "成功缓存的保留小时；未配置则不自动清理。",
                    (),
                    "integer",
                    1,
                    (),
                    "SPEECH_CACHE_RETENTION_HOURS",
                    "speech_cache_retention_hours",
                ),
                *(
                    (
                        key,
                        display,
                        description,
                        (),
                        "boolean",
                        None,
                        (),
                        env_alias,
                        field_name,
                    )
                    for key, display, description, env_alias, field_name in (
                        (
                            "speech.private_enabled",
                            "私聊语音开关",
                            "允许私聊生成语音。",
                            "SPEECH_PRIVATE_ENABLED",
                            "speech_private_enabled",
                        ),
                        (
                            "speech.group_enabled",
                            "群聊语音开关",
                            "允许群聊生成语音。",
                            "SPEECH_GROUP_ENABLED",
                            "speech_group_enabled",
                        ),
                        (
                            "speech.automation_enabled",
                            "自动化语音开关",
                            "允许自动化能力生成和发送语音。",
                            "SPEECH_AUTOMATION_ENABLED",
                            "speech_automation_enabled",
                        ),
                        (
                            "speech.plugin_enabled",
                            "插件语音开关",
                            "允许获批插件使用语音能力。",
                            "SPEECH_PLUGIN_ENABLED",
                            "speech_plugin_enabled",
                        ),
                        (
                            "speech.text_fallback_enabled",
                            "语音失败文字回退",
                            "语音不可用或生成失败时保留文字回复。",
                            "SPEECH_TEXT_FALLBACK_ENABLED",
                            "speech_text_fallback_enabled",
                        ),
                    )
                ),
            )
        ),
    )
    return specs
