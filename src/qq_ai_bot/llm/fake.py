"""Deterministic LLM used by tests and offline development."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.llm.base import LLMEmptyResponseError, LLMProvider
from qq_ai_bot.prompting.serializer import strip_dynamic_prefix


class FakeLLMProvider(LLMProvider):
    """Return a deterministic response without any network access."""

    def __init__(
        self,
        responder: Callable[[ChatRequest], str | ChatResponse] | None = None,
        *,
        delay_seconds: float = 0,
    ) -> None:
        self._responder = responder or self._default_response
        self._delay_seconds = delay_seconds
        self.requests: list[ChatRequest] = []

    @staticmethod
    def _default_response(request: ChatRequest) -> str:
        user_messages = [
            message.content or "" for message in request.messages if message.role == "user"
        ]
        content = strip_dynamic_prefix(user_messages[-1] if user_messages else "")
        _, envelope_separator, event_line = content.partition("\n")
        if envelope_separator and event_line.startswith("#"):
            event_header, body_separator, body = event_line.partition(">")
            event_id = event_header[1:].split("|", maxsplit=1)[0]
            if body_separator and event_id.isdigit():
                content = body
        return f"FakeLLM: {content}"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        response = self._responder(request)
        if isinstance(response, ChatResponse):
            return response
        content = response.strip()
        if not content:
            raise LLMEmptyResponseError("model returned empty content")
        return ChatResponse(content=content, latency_seconds=self._delay_seconds)
