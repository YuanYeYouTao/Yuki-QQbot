"""Reusable OpenAI-compatible Chat Completions client."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.base import (
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMError,
    LLMProvider,
    LLMTimeoutError,
    LLMUnavailableError,
    RetryableProviderError,
)

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    """Non-streaming provider with bounded retries for transient failures only."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        max_retries: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._max_retries = max_retries
        self._owns_client = client is None
        self._timeout = httpx.Timeout(
            connect=timeout_seconds,
            read=timeout_seconds,
            write=timeout_seconds,
            pool=timeout_seconds,
        )
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=self._timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def complete(self, request: ChatRequest) -> ChatResponse:
        if not self._api_key or not request.model:
            raise LLMConfigurationError("LLM is not configured")

        started = time.perf_counter()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_retries + 1),
                wait=wait_random_exponential(multiplier=0.25, max=2),
                retry=retry_if_exception_type((httpx.TransportError, RetryableProviderError)),
                reraise=True,
            ):
                with attempt:
                    response = await self._post(request)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("LLM request timed out") from exc
        except (httpx.TransportError, RetryableProviderError) as exc:
            raise LLMUnavailableError("LLM is temporarily unavailable") from exc

        latency = time.perf_counter() - started
        logger.info("llm_request_complete latency_seconds=%.3f success=true", latency)
        (
            content,
            request_id,
            tool_calls,
            reasoning_content,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cached_prompt_tokens,
        ) = self._parse_response(response)
        return ChatResponse(
            content=content,
            latency_seconds=latency,
            provider_request_id=request_id,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
        )

    async def _post(self, request: ChatRequest) -> httpx.Response:
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            item: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": call.type,
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in message.tool_calls
                ]
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
            if message.reasoning_content is not None:
                item["reasoning_content"] = message.reasoning_content
            messages.append(item)
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
            # DeepSeek tool-capable endpoints reject the OpenAI tool_choice
            # field in both thinking modes. Omission preserves model-selected
            # tool use while other OpenAI-compatible providers keep the field.
            deepseek_model = request.model.casefold().startswith("deepseek-")
            if not deepseek_model:
                payload["tool_choice"] = request.tool_choice or "auto"
        if request.thinking_enabled is not None:
            payload["thinking"] = {"type": "enabled" if request.thinking_enabled else "disabled"}
        if request.reasoning_effort is not None:
            payload["reasoning_effort"] = request.reasoning_effort.value
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        response = await self._client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
            timeout=self._timeout,
        )
        if response.status_code >= 500:
            raise RetryableProviderError("provider returned a server error")
        if response.status_code >= 400:
            raise LLMError(f"provider rejected request with HTTP {response.status_code}")
        return response

    @staticmethod
    def _parse_response(
        response: httpx.Response,
    ) -> tuple[
        str,
        str | None,
        tuple[ToolCall, ...],
        str | None,
        int | None,
        int | None,
        int | None,
        int | None,
    ]:
        try:
            payload: dict[str, Any] = response.json()
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise LLMEmptyResponseError("provider returned no choices")
            first = choices[0]
            if not isinstance(first, dict):
                raise LLMEmptyResponseError("provider returned an invalid choice")
            message = first.get("message")
            if not isinstance(message, dict):
                raise LLMEmptyResponseError("provider returned no message")
            raw_content = message.get("content")
            raw_tool_calls = message.get("tool_calls", [])
            tool_calls: list[ToolCall] = []
            if isinstance(raw_tool_calls, list):
                for item in raw_tool_calls:
                    if not isinstance(item, dict):
                        continue
                    function = item.get("function")
                    if not isinstance(function, dict):
                        continue
                    call_id = item.get("id")
                    name = function.get("name")
                    arguments = function.get("arguments")
                    if (
                        not isinstance(call_id, str)
                        or not isinstance(name, str)
                        or not isinstance(arguments, str)
                    ):
                        continue
                    tool_calls.append(
                        ToolCall(
                            id=call_id,
                            type=str(item.get("type", "function")),
                            function=ToolFunction(name=name, arguments=arguments),
                        )
                    )
            content = raw_content.strip() if isinstance(raw_content, str) else ""
            if not content and not tool_calls:
                raise LLMEmptyResponseError("provider returned empty content")
            request_id = payload.get("id")
            raw_reasoning = message.get("reasoning_content")
            reasoning = raw_reasoning if isinstance(raw_reasoning, str) else None
            usage = payload.get("usage")
            prompt_tokens: int | None = None
            completion_tokens: int | None = None
            total_tokens: int | None = None
            cached_prompt_tokens: int | None = None
            if isinstance(usage, dict):
                raw_prompt = usage.get("prompt_tokens")
                raw_completion = usage.get("completion_tokens")
                raw_total = usage.get("total_tokens")
                prompt_tokens = raw_prompt if isinstance(raw_prompt, int) else None
                completion_tokens = raw_completion if isinstance(raw_completion, int) else None
                total_tokens = raw_total if isinstance(raw_total, int) else None
                raw_cached = usage.get("prompt_cache_hit_tokens")
                details = usage.get("prompt_tokens_details")
                if isinstance(details, dict):
                    raw_cached = details.get("cached_tokens", raw_cached)
                cached_prompt_tokens = raw_cached if isinstance(raw_cached, int) else None
            return (
                content,
                request_id if isinstance(request_id, str) else None,
                tuple(tool_calls),
                reasoning,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                cached_prompt_tokens,
            )
        except ValueError as exc:
            raise LLMError("provider returned invalid JSON") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
