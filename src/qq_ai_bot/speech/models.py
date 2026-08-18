"""Strict domain models for profiles, generations, and voice reply intent."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SpeechEngineModelVersion(StrEnum):
    V2 = "v2"
    V2_PRO_PLUS = "v2proplus"


class VoiceMode(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    TEXT_AND_VOICE = "text_and_voice"
    OPTIONAL = "optional"


class VoiceIntent(StrEnum):
    """Semantic voice intent for a reply-effect turn."""

    NEUTRAL = "neutral"
    EXPLICIT_REQUEST = "explicit_request"
    EXPLICIT_OPT_OUT = "explicit_opt_out"


class VoiceAgentToolPolicy(StrEnum):
    """Whether the Agent may customize an already-authorized voice reply."""

    FORBIDDEN = "forbidden"
    REQUIRED = "required"


class VoicePreferenceMode(StrEnum):
    """Persistent person-level preference, distinct from one-turn delivery."""

    TEXT_ONLY = "text_only"
    AUTO = "auto"
    PREFER_VOICE = "prefer_voice"


class VoicePreferenceDuration(StrEnum):
    """Whether a semantic preference applies only now or to future turns."""

    TURN = "turn"
    PERSISTENT = "persistent"


class SpeechLanguage(StrEnum):
    ZH = "zh"
    JP = "jp"
    EN = "en"


class SpeechLanguageHint(StrEnum):
    AUTO = "auto"
    ZH = "zh"
    JP = "jp"


class SpeechGenerationStatus(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SENT = "sent"
    EXPIRED = "expired"


class VoiceManifestModel(_FrozenModel):
    path: str = Field(min_length=1)


class VoiceManifestReference(_FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    style: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    audio: str = Field(min_length=1)
    text: str = Field(min_length=1)
    language: str = Field(min_length=1)
    enabled: bool = True
    priority: int = 0

    @model_validator(mode="after")
    def _validate_language(self) -> VoiceManifestReference:
        if self.language not in {item.value for item in SpeechLanguage}:
            raise ValueError("unsupported reference language")
        return self


class VoiceProfileManifest(_FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    display_name: str = Field(min_length=1)
    provider: Literal["genie"] = "genie"
    engine_model_version: SpeechEngineModelVersion
    language: str = Field(min_length=1)
    supported_languages: tuple[str, ...] = ()
    default_style: str = Field(min_length=1)
    enabled: bool = True
    source: str = Field(min_length=1)
    source_note: str = ""
    license_note: str = ""
    model: VoiceManifestModel
    references: tuple[VoiceManifestReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_references(self) -> VoiceProfileManifest:
        try:
            default_language = SpeechLanguage(self.language).value
            supported = tuple(
                dict.fromkeys(SpeechLanguage(item).value for item in self.supported_languages)
            )
        except ValueError as exc:
            raise ValueError("unsupported speech language") from exc
        if not supported:
            supported = (default_language,)
        if default_language not in supported:
            raise ValueError("default language must be supported")
        keys = [item.id for item in self.references]
        if len(keys) != len(set(keys)):
            raise ValueError("reference ids must be unique")
        defaults = [
            item for item in self.references if item.enabled and item.style == self.default_style
        ]
        if len(defaults) != 1:
            raise ValueError("default_style must identify exactly one enabled reference")
        return self.model_copy(update={"supported_languages": supported})


class VoiceReference(_FrozenModel):
    id: int
    profile_id: str
    reference_key: str
    style: str
    aliases: tuple[str, ...]
    audio_relative_path: str
    audio_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript: str
    language: str
    enabled: bool
    priority: int
    created_at: datetime
    updated_at: datetime


class VoiceProfile(_FrozenModel):
    profile_id: str
    display_name: str
    provider: Literal["genie"]
    engine_model_version: SpeechEngineModelVersion
    language: str
    supported_languages: tuple[str, ...]
    model_relative_path: str
    model_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    default_style: str
    enabled: bool
    is_default: bool
    source: str
    source_note: str
    license_note: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    references: tuple[VoiceReference, ...] = ()
    created_at: datetime
    updated_at: datetime


class SpeechGeneration(_FrozenModel):
    id: int
    request_id: str
    conversation_key_hash: str
    trigger_event_id: int | None
    profile_id: str
    reference_id: int | None
    engine_version: str
    target_language: str
    text_hash: str
    normalized_text_hash: str
    character_count: int = Field(gt=0)
    cache_key: str
    output_relative_path: str
    output_format: str
    sample_rate: int | None
    channels: int | None
    duration_milliseconds: int | None
    status: SpeechGenerationStatus
    error_category: str | None
    created_at: datetime
    expires_at: datetime | None


class VoicePreferenceChange(_FrozenModel):
    # This model is parsed from JSON emitted by the Agent tool, where enum
    # members are necessarily represented by strings. Keep extra/frozen
    # guarantees without inheriting the internal Python-only strict enum
    # requirement.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    mode: VoicePreferenceMode
    duration: VoicePreferenceDuration = VoicePreferenceDuration.TURN


class VoiceReplyPlan(_FrozenModel):
    # Same trust boundary as VoicePreferenceChange: JSON strings must validate
    # into enums before backend constraints are applied.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    mode: VoiceMode = Field(
        default=VoiceMode.TEXT,
        description="本轮最终发送载体；明确索要语音时使用 voice 或 text_and_voice。",
    )
    intent: VoiceIntent = Field(
        default=VoiceIntent.NEUTRAL,
        description=(
            "只分类用户对发送载体的意图：明确索要语音为 explicit_request，明确不要语音为 "
            "explicit_opt_out，未表达载体偏好才是 neutral。"
        ),
    )
    agent_tool: VoiceAgentToolPolicy = Field(
        default=VoiceAgentToolPolicy.FORBIDDEN,
        description="intent 为 explicit_request 时必须为 required，否则为 forbidden。",
    )
    style_hint: str = Field(default="", max_length=128)
    language: SpeechLanguageHint = SpeechLanguageHint.AUTO
    reason: str = Field(default="", max_length=300)
    preference_change: VoicePreferenceChange | None = None

    @model_validator(mode="after")
    def _reject_paths_and_profiles(self) -> VoiceReplyPlan:
        hint = self.style_hint
        if any(token in hint for token in ("/", "\\", "://")):
            raise ValueError("style_hint cannot contain a path")
        if (
            self.agent_tool is VoiceAgentToolPolicy.REQUIRED
            and self.intent is not VoiceIntent.EXPLICIT_REQUEST
        ):
            raise ValueError("send_voice can only be authorized for an explicit request")
        return self
