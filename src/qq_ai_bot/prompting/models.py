"""Immutable prompt program models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from qq_ai_bot.domain.messages import ChatMessage


class PromptChannel(StrEnum):
    PERSONA = "persona"
    INVARIANT = "invariant"
    RUNTIME = "runtime"
    CONTEXT = "context"
    PLUGIN = "plugin"
    PLAN = "plan"
    MODALITY = "modality"


class PromptTrust(StrEnum):
    CORE = "core"
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class PromptStability(StrEnum):
    STATIC = "static"
    SESSION = "session"
    TURN = "turn"


class PromptContribution(BaseModel):
    """One independently testable contribution to a prompt program."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    id: str
    channel: PromptChannel
    trust: PromptTrust
    priority: int = 0
    stability: PromptStability = PromptStability.TURN
    content: str | None = None
    payload: Any | None = None
    source: str = "core"
    required: bool = False

    @model_validator(mode="after")
    def _validate_body(self) -> PromptContribution:
        if not self.id.strip():
            raise ValueError("prompt contribution id must not be empty")
        if (self.content is None) == (self.payload is None):
            raise ValueError("prompt contribution requires exactly one of content or payload")
        if self.content is not None and not self.content.strip():
            raise ValueError("prompt contribution content must not be empty")
        return self


class PromptProgram(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contributions: tuple[PromptContribution, ...]


class PromptMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    static_characters: int
    dynamic_characters: int
    history_characters: int
    current_message_characters: int
    total_characters: int
    estimated_tokens: int
    contribution_count: int
    message_count: int
    stable_prefix_hash: str
    session_characters: int = 0


class CompiledPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    messages: tuple[ChatMessage, ...]
    selected: tuple[PromptContribution, ...]
    metrics: PromptMetrics
