"""Ordered opaque checkpoints shared by native protocol adapters."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, FunctionCallOutput
from qq_ai_bot.llm.base import LLMInvalidRequestError


def checkpoint_items(request: ChatRequest, provider: str, protocol: str) -> list[dict[str, Any]]:
    continuation = request.continuation
    if continuation is None:
        return []
    if continuation.provider != provider or continuation.protocol != protocol:
        raise LLMInvalidRequestError("continuation belongs to another provider or protocol")
    if not isinstance(continuation.payload, tuple) or not all(
        isinstance(item, dict) for item in continuation.payload
    ):
        raise LLMInvalidRequestError("invalid provider checkpoint")
    return deepcopy(list(continuation.payload))


def ordered_delta(request: ChatRequest) -> tuple[ChatMessage | FunctionCallOutput, ...]:
    if request.continuation_items and (request.function_outputs or request.continuation_messages):
        raise LLMInvalidRequestError("mixed ordered and legacy continuation inputs")
    return request.continuation_items or (*request.function_outputs, *request.continuation_messages)


def integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
