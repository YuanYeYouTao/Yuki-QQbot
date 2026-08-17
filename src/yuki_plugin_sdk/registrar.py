"""Declarative extension contracts used during a plugin's register phase."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field, field_validator

from yuki_plugin_sdk.events import EventName, NotificationHandler
from yuki_plugin_sdk.models import (
    AdmissionSignal,
    AdmissionSignalContext,
    EmojiSelectionSignal,
    EmojiSelectionSignalContext,
    PermissionLevel,
    PromptFragment,
    RestartPolicy,
    RetryPolicy,
    RiskClass,
    StrictModel,
    TurnOrigin,
)
from yuki_plugin_sdk.namespace import (
    is_reserved_plugin_namespace,
    is_valid_namespace_id,
)
from yuki_plugin_sdk.results import CommandResult, ToolResult

ToolHandler = Callable[[BaseModel], Awaitable[ToolResult | BaseModel]]
CommandHandler = Callable[[BaseModel], Awaitable[CommandResult]]
AutomationHandler = Callable[[BaseModel], Awaitable[ToolResult | BaseModel]]
AdmissionSignalProvider = (
    Callable[[AdmissionSignalContext], Awaitable[AdmissionSignal | None]]
    | Callable[[], Awaitable[AdmissionSignal | None]]
)
EmojiSelectionSignalProvider = Callable[
    [EmojiSelectionSignalContext], Awaitable[EmojiSelectionSignal | None]
]
BackgroundRunner = Callable[[], Awaitable[None]]

_MAX_TOOL_LABELS = 8
_MAX_USE_WHEN_CHARS = 200


class ToolMetadata(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=1_000)
    permission: PermissionLevel = PermissionLevel.USER
    risk: RiskClass = RiskClass.READ
    schema_version: int = Field(default=1, ge=1)
    namespace: str = ""
    aliases: tuple[str, ...] = Field(default=(), max_length=_MAX_TOOL_LABELS)
    use_when: tuple[str, ...] = Field(default=(), max_length=_MAX_TOOL_LABELS)
    tags: tuple[str, ...] = Field(default=(), max_length=_MAX_TOOL_LABELS)
    allowed_origins: frozenset[TurnOrigin] = Field(
        default_factory=lambda: frozenset({TurnOrigin.USER_MESSAGE})
    )
    timeout_seconds: float = Field(default=10, gt=0, le=600)
    retry_policy: RetryPolicy = RetryPolicy.NONE

    @field_validator("namespace")
    @classmethod
    def _valid_namespace(cls, value: str) -> str:
        if not value:
            return value
        if not is_valid_namespace_id(value):
            raise ValueError(f"invalid tool namespace: {value!r}")
        if is_reserved_plugin_namespace(value):
            raise ValueError(f"plugin tools cannot use reserved namespace: {value!r}")
        return value

    @field_validator("aliases", "tags")
    @classmethod
    def _valid_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for label in value:
            if not label or label != label.lower():
                raise ValueError(f"tool aliases/tags must be lowercase, got {label!r}")
        if len(set(value)) != len(value):
            raise ValueError("tool aliases/tags must be unique")
        return value

    @field_validator("use_when")
    @classmethod
    def _valid_use_when(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item or len(item) > _MAX_USE_WHEN_CHARS:
                raise ValueError("use_when items must be 1-200 characters")
        return value


class CommandMetadata(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=1_000)
    permission: PermissionLevel = PermissionLevel.USER
    short_alias: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    timeout_seconds: float = Field(default=10, gt=0, le=600)


class AutomationActionMetadata(ToolMetadata):
    allowed_origins: frozenset[TurnOrigin] = Field(
        default_factory=lambda: frozenset({TurnOrigin.SCHEDULED_AUTOMATION, TurnOrigin.SYSTEM_TASK})
    )


class EventHookMetadata(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    event: EventName
    priority: int = Field(default=0, ge=-10_000, le=10_000)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)


class BackgroundServiceMetadata(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(default="", max_length=500)
    shutdown_timeout_seconds: float = Field(default=10, gt=0, le=600)
    max_concurrency: int = Field(default=1, ge=1, le=64)
    restart_policy: RestartPolicy = RestartPolicy.NEVER


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    metadata: ToolMetadata
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler


@dataclass(frozen=True, slots=True)
class CommandRegistration:
    metadata: CommandMetadata
    argument_model: type[BaseModel]
    handler: CommandHandler


@dataclass(frozen=True, slots=True)
class AutomationActionRegistration:
    metadata: AutomationActionMetadata
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: AutomationHandler


@dataclass(frozen=True, slots=True)
class EventHookRegistration:
    metadata: EventHookMetadata
    handler: NotificationHandler


@dataclass(frozen=True, slots=True)
class AdmissionSignalRegistration:
    name: str
    provider: AdmissionSignalProvider


@dataclass(frozen=True, slots=True)
class EmojiSelectionSignalRegistration:
    name: str
    provider: EmojiSelectionSignalProvider


@dataclass(frozen=True, slots=True)
class BackgroundServiceRegistration:
    metadata: BackgroundServiceMetadata
    runner: BackgroundRunner


@dataclass(frozen=True, slots=True)
class TTSProviderRegistration:
    """Reserved extension point for a Host-compatible local TTS provider."""

    name: str
    provider: object


class PluginRegistrar(Protocol):
    """Registration-only surface; it deliberately exposes no runtime service."""

    def register_tool(self, registration: ToolRegistration) -> None: ...

    def register_command(self, registration: CommandRegistration) -> None: ...

    def register_event_hook(self, registration: EventHookRegistration) -> None: ...

    def register_prompt_fragment(self, fragment: PromptFragment) -> None: ...

    def register_automation_action(self, registration: AutomationActionRegistration) -> None: ...

    def register_admission_signal(self, registration: AdmissionSignalRegistration) -> None: ...

    def register_emoji_selection_signal(
        self, registration: EmojiSelectionSignalRegistration
    ) -> None: ...

    def register_config_schema(self, schema: type[BaseModel]) -> None: ...

    def register_background_service(self, registration: BackgroundServiceRegistration) -> None: ...

    def register_tts_provider(self, registration: TTSProviderRegistration) -> None: ...
