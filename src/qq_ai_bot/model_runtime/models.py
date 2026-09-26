"""Provider-neutral model task, profile, route, and usage models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qq_ai_bot.domain.messages import ReasoningEffort, minimum_reasoning_effort
from qq_ai_bot.llm.vendor_policy import (
    CHAT_VENDORS,
    RESPONSES_VENDORS,
    ChatWireOptions,
)


class ModelTask(StrEnum):
    """Stable business purpose attached to every main-model invocation."""

    CHAT_AGENT = "chat_agent"
    MEMORY_EXTRACTION = "memory_extraction"
    MEMORY_SELF_REFLECTION = "memory_self_reflection"
    MEMORY_CONSOLIDATION = "memory_consolidation"
    MEMORY_DREAM = "memory_dream"
    MEMORY_ATTRIBUTION = "memory_attribution"
    RELATIONSHIP_EVALUATION = "relationship_evaluation"
    EMOJI_REPLACEMENT = "emoji_replacement"
    AUTOMATION_TEXT_GENERATION = "automation_text_generation"
    AUTOMATION_AGENT = "automation_agent"
    PLUGIN_AGENT_SESSION = "plugin_agent_session"
    UTILITY_STRUCTURED = "utility_structured"
    CONVERSATION_COMPACTION = "conversation_compaction"


class ModelExecutionPriority(StrEnum):
    """Scheduling priority for provider calls inside the shared executor."""

    FOREGROUND = "foreground"
    EXCLUSIVE = "exclusive"
    BEST_EFFORT_BACKGROUND = "best_effort_background"
    MAINTENANCE = "maintenance"
    REQUIRED = "required"


class ModelCapability(StrEnum):
    """Features a profile must explicitly advertise."""

    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured_output"
    REASONING = "reasoning"
    LONG_CONTEXT = "long_context"
    NATIVE_WEB_SEARCH = "native_web_search"
    IMAGE_INPUT = "image_input"


class ModelProtocol(StrEnum):
    """Wire protocol used by one model profile."""

    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GEMINI = "gemini"


class StructuredOutputMode(StrEnum):
    """Supported provider strategies for one validated object."""

    FUNCTION_TOOL = "function_tool"
    JSON_SCHEMA = "json_schema"
    TEXT_JSON = "text_json"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelProfile(_FrozenModel):
    """One named provider endpoint and its defaults."""

    id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_.-]+$")
    provider: str = Field(min_length=1)
    protocol: ModelProtocol = ModelProtocol.CHAT_COMPLETIONS
    base_url: str = ""
    api_key_env: str = ""
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    default_temperature: float = Field(ge=0, le=2)
    default_max_output_tokens: int = Field(gt=0)
    max_output_tokens_limit: int | None = Field(default=None, gt=0)
    thinking_enabled: bool | None = True
    reasoning_effort: ReasoningEffort | None = ReasoningEffort.LOW
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.FUNCTION_TOOL
    capabilities: frozenset[ModelCapability] = frozenset()
    wire_options: ChatWireOptions | None = None
    headers: dict[str, str] = Field(default_factory=dict, repr=False)

    @field_validator("thinking_enabled")
    @classmethod
    def _enable_reasoning(cls, value: bool | None) -> bool:
        # Legacy disable/unspecified settings cannot bypass the application floor.
        return True

    @field_validator("reasoning_effort")
    @classmethod
    def _reasoning_floor(cls, value: ReasoningEffort | None) -> ReasoningEffort:
        return minimum_reasoning_effort(value)

    @model_validator(mode="after")
    def _validate_endpoint(self) -> ModelProfile:
        vendor = self.provider.casefold()
        allowed = (
            CHAT_VENDORS
            if self.protocol is ModelProtocol.CHAT_COMPLETIONS
            else RESPONSES_VENDORS
            if self.protocol is ModelProtocol.RESPONSES
            else {"anthropic"}
            if self.protocol is ModelProtocol.ANTHROPIC_MESSAGES
            else {"gemini"}
        )
        if vendor != "fake" and vendor not in allowed:
            raise ValueError(f"provider {self.provider} does not support {self.protocol.value}")
        reserved = {
            "authorization",
            "api-key",
            "x-api-key",
            "x-goog-api-key",
            "host",
            "content-length",
        }
        if any(
            name.casefold() in reserved
            or "\n" in name
            or "\r" in name
            or "\n" in value
            or "\r" in value
            for name, value in self.headers.items()
        ):
            raise ValueError("headers cannot override authentication or contain line breaks")
        if self.provider.casefold() != "fake" and not self.base_url:
            raise ValueError("base_url is required for non-fake model profiles")
        if self.provider.casefold() != "fake" and not self.api_key_env:
            raise ValueError("api_key_env is required for non-fake model profiles")
        if ModelCapability.REASONING not in self.capabilities:
            raise ValueError(
                "all generation profiles require the reasoning capability (minimum low)"
            )
        return self


class ModelRoute(_FrozenModel):
    """Bind one business task to one profile and required capabilities."""

    task: ModelTask
    profile_id: str = Field(min_length=1)
    required_capabilities: frozenset[ModelCapability] = frozenset()


class ModelInvocationRecord(_FrozenModel):
    """Content-free usage and latency record returned by the repository."""

    id: int
    task: str
    profile_id: str
    provider: str
    model: str
    success: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    latency_seconds: float
    error_category: str | None = None
    created_at: datetime


class ModelStats(_FrozenModel):
    """Aggregated model usage without prompts, users, or tool payloads."""

    invocations: int = 0
    successes: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_prompt_tokens: int = 0
    unknown_usage: int = 0
    average_latency_seconds: float = 0
