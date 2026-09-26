"""Explicit vendor wire settings, independent of business tasks and message content."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from qq_ai_bot.domain.messages import ReasoningEffort


class ChatWireOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reasoning: Literal[
        "effort", "thinking", "enable_thinking", "openrouter", "builtin", "gemini", "budget"
    ] = "effort"
    token_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    send_temperature: bool = False
    send_tool_choice: bool = True
    replay_reasoning: bool = True
    reasoning_split: bool = False
    send_reasoning_effort: bool = False
    native_web_search: bool = False
    thinking_budget_tokens: int = Field(default=4096, ge=1024)
    effort_levels: tuple[ReasoningEffort, ...] | None = None
    include_reasoning: bool | None = None
    reasoning_format: Literal["parsed", "hidden"] | None = None

    @field_validator("effort_levels")
    @classmethod
    def _effort_levels(
        cls, value: tuple[ReasoningEffort, ...] | None
    ) -> tuple[ReasoningEffort, ...] | None:
        if value is not None:
            order = tuple(ReasoningEffort)
            if (
                not value
                or any(level in {ReasoningEffort.NONE, ReasoningEffort.MINIMAL} for level in value)
                or tuple(sorted(set(value), key=order.index)) != value
            ):
                raise ValueError("effort_levels must be distinct ascending levels at least low")
        return value


# Vendor presets specify wire dialect, not claims about any particular model.
CHAT_VENDORS = frozenset(
    {
        "openai",
        "openai_compatible",
        "deepseek",
        "qwen",
        "moonshot",
        "zhipu",
        "doubao",
        "minimax",
        "openrouter",
        "siliconflow",
        "together",
        "groq",
        "mistral",
        "xai",
        "azure_openai",
    }
)
RESPONSES_VENDORS = frozenset({"openai", "openai_compatible", "deepseek"})


def wire_options(vendor: str, overrides: ChatWireOptions | None = None) -> ChatWireOptions:
    defaults: dict[str, object] = {}
    if vendor in {"openai", "azure_openai"}:
        defaults = {"token_field": "max_completion_tokens", "replay_reasoning": False}
    elif vendor == "deepseek":
        defaults = {
            "reasoning": "thinking",
            "send_tool_choice": False,
            "send_reasoning_effort": True,
            "effort_levels": ("low", "high", "max"),
        }
    elif vendor == "qwen":
        defaults = {"reasoning": "enable_thinking"}
    elif vendor in {"moonshot", "zhipu", "doubao"}:
        defaults = {"reasoning": "thinking"}
        if vendor == "doubao":
            defaults["send_reasoning_effort"] = True
    elif vendor == "minimax":
        defaults = {"reasoning": "builtin", "reasoning_split": True}
    elif vendor == "openrouter":
        defaults = {"reasoning": "openrouter"}
    elif vendor == "groq":
        defaults = {
            "token_field": "max_completion_tokens",
            "include_reasoning": True,
            "effort_levels": ("low", "medium", "high"),
        }
    elif vendor == "mistral":
        defaults = {"effort_levels": ("high",)}
    elif vendor == "anthropic":
        defaults = {"reasoning": "effort", "effort_levels": ("low", "medium", "high", "max")}
    elif vendor == "gemini":
        defaults = {"reasoning": "gemini", "effort_levels": ("low", "medium", "high")}
    if overrides is not None:
        defaults.update(overrides.model_dump(exclude_unset=True))
    return ChatWireOptions.model_validate(defaults)


def effort_value(options: ChatWireOptions, effort: ReasoningEffort | None) -> str:
    if options.effort_levels is None:
        return effort.value if effort else "low"
    order = tuple(ReasoningEffort)
    for level in options.effort_levels:
        if order.index(level) >= order.index(effort or ReasoningEffort.LOW):
            return level.value
    from qq_ai_bot.llm.base import LLMUnsupportedFeatureError

    raise LLMUnsupportedFeatureError("requested effort exceeds configured model levels")


def thinking_budget(options: ChatWireOptions, effort: ReasoningEffort | None) -> int:
    """An explicit monotone policy for budget APIs, not a claim of effort equivalence."""
    levels = (
        ReasoningEffort.LOW,
        ReasoningEffort.MEDIUM,
        ReasoningEffort.HIGH,
        ReasoningEffort.XHIGH,
        ReasoningEffort.MAX,
    )
    level = levels.index(effort) if effort in levels else 0
    return options.thinking_budget_tokens * (1 << level)


def supports_native_search(
    vendor: str, protocol: str, options: ChatWireOptions | None, *, has_functions: bool
) -> bool:
    if vendor == "deepseek":
        return False
    if protocol == "responses":
        return vendor in RESPONSES_VENDORS
    if protocol == "chat_completions":
        return wire_options(vendor, options).native_web_search and not has_functions
    return False
