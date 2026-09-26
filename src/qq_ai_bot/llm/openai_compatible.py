"""Chat Completions wire adapter with explicit vendor dialects."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import httpx

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FunctionCallOutput,
    ModelResponseStatus,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import (
    LLMEmptyResponseError,
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMUnsupportedFeatureError,
)
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.json_http import JSONHTTPProvider
from qq_ai_bot.llm.protocol_state import checkpoint_items, integer, ordered_delta
from qq_ai_bot.llm.vendor_policy import ChatWireOptions, effort_value, thinking_budget, wire_options


class OpenAICompatibleProvider(JSONHTTPProvider):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        max_retries: int,
        client: httpx.AsyncClient | None = None,
        provider_name: str = "openai_compatible",
        options: ChatWireOptions | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            client=client,
            headers=headers,
        )
        self.provider_name = provider_name
        self.options = wire_options(provider_name, options)

    def _message(self, message: ChatMessage) -> dict[str, Any]:
        if message.response_item is not None:
            raise LLMInvalidRequestError("opaque history requires its original protocol")
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.role == "assistant" and message.content is None and not message.tool_calls:
            item["content"] = ""
        if message.images:
            if message.role != "user":
                raise LLMInvalidRequestError("images must be attached to a user message")
            item["content"] = [
                {"type": "text", "text": message.content or ""},
                *(
                    {"type": "image_url", "image_url": {"url": image.data_url}}
                    for image in message.images
                ),
            ]
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.reasoning_content is not None and self.options.replay_reasoning:
            item["reasoning_content"] = message.reasoning_content
        return item

    def _history(self, request: ChatRequest) -> list[dict[str, Any]]:
        messages = [self._message(message) for message in request.messages]
        messages.extend(checkpoint_items(request, self.provider_name, self.protocol))
        for item in ordered_delta(request):
            messages.append(
                {"role": "tool", "tool_call_id": item.call_id, "content": item.output}
                if isinstance(item, FunctionCallOutput)
                else self._message(item)
            )
        return messages

    def _build_payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": self._history(request),
            "stream": False,
        }
        if request.max_output_tokens is not None:
            payload[self.options.token_field] = request.max_output_tokens
        if self.options.send_temperature and request.temperature is not None:
            payload["temperature"] = request.temperature
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
            if self.options.send_tool_choice:
                choice = request.tool_choice or "auto"
                payload["tool_choice"] = (
                    choice
                    if choice in {"auto", "none", "required"}
                    else {"type": "function", "function": {"name": choice}}
                )
        if request.native_tools:
            if not self.options.native_web_search:
                raise LLMUnsupportedFeatureError("Chat native search must be explicitly configured")
            if request.tools or request.tool_choice == "none":
                raise LLMUnsupportedFeatureError(
                    "Chat search models cannot promise mixed functions or disabled native search"
                )
            payload["web_search_options"] = {}
        if request.thinking_enabled:
            effort = effort_value(self.options, request.reasoning_effort)
            match self.options.reasoning:
                case "effort":
                    payload["reasoning_effort"] = effort
                case "thinking":
                    payload["thinking"] = {"type": "enabled"}
                    if self.options.send_reasoning_effort:
                        payload["reasoning_effort"] = effort
                    elif effort not in {"none", "minimal", "low"}:
                        raise LLMUnsupportedFeatureError(
                            "this thinking dialect has no effort control"
                        )
                case "enable_thinking":
                    payload["enable_thinking"] = True
                    payload["thinking_budget"] = thinking_budget(
                        self.options, request.reasoning_effort
                    )
                case "openrouter":
                    payload["reasoning"] = {"effort": effort, "exclude": False}
                case "builtin":
                    if effort not in {"none", "minimal", "low"}:
                        raise LLMUnsupportedFeatureError(
                            "this thinking-only model has no effort control"
                        )
                case _:
                    raise LLMUnsupportedFeatureError("reasoning mode does not match Chat protocol")
        if self.options.reasoning_split:
            payload["reasoning_split"] = True
        if self.options.reasoning_format is not None:
            payload["reasoning_format"] = self.options.reasoning_format
        elif self.options.include_reasoning is not None:
            payload["include_reasoning"] = self.options.include_reasoning
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        return payload

    def _parse(self, response: httpx.Response, request: ChatRequest) -> ChatResponse:
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMInvalidResponseError("provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise LLMInvalidResponseError("provider returned an invalid response")
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise LLMInvalidResponseError("provider must return exactly one choice")
        first = choices[0]
        message = first.get("message")
        if not isinstance(message, dict):
            raise LLMInvalidResponseError("provider returned no message")
        finish = first.get("finish_reason")
        if finish not in {None, "stop", "length", "tool_calls", "function_call"}:
            raise LLMInvalidResponseError("provider rejected or failed the completion")
        raw_content = message.get("content")
        content = raw_content if isinstance(raw_content, str) else ""
        if isinstance(raw_content, list):
            content = "".join(
                part["text"]
                for part in raw_content
                if isinstance(part, dict)
                and part.get("type") in {"text", "output_text"}
                and isinstance(part.get("text"), str)
            )
        calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise LLMInvalidResponseError("invalid tool call list")
        for item in raw_calls:
            function = item.get("function") if isinstance(item, dict) else None
            if (
                not isinstance(function, dict)
                or not all(
                    isinstance(value, str) and value
                    for value in (
                        item.get("id"),
                        function.get("name"),
                        function.get("arguments"),
                    )
                )
                or item.get("type", "function") != "function"
            ):
                raise LLMInvalidResponseError("provider returned a malformed tool call")
            calls.append(
                ToolCall(
                    id=item["id"],
                    function=ToolFunction(
                        name=function["name"],
                        arguments=function["arguments"],
                    ),
                )
            )
        if len({call.id for call in calls}) != len(calls):
            raise LLMInvalidResponseError("provider returned duplicate tool call IDs")
        raw_reasoning = message.get("reasoning_content", message.get("reasoning"))
        reasoning = raw_reasoning if isinstance(raw_reasoning, str) else None
        if isinstance(raw_content, list):
            thinking = [
                part.get("thinking")
                for part in raw_content
                if isinstance(part, dict) and part.get("type") == "thinking"
            ]
            chunks = [
                chunk["text"]
                for block in thinking
                if isinstance(block, list)
                for chunk in block
                if isinstance(chunk, dict) and isinstance(chunk.get("text"), str)
            ]
            if chunks:
                reasoning = "\n".join(chunks)
        truncated = finish == "length"
        if (
            not truncated
            and self.provider_name == "deepseek"
            and DeepSeekResponsesProvider._contains_dsml(content)
        ):
            if calls:
                raise LLMInvalidResponseError("mixed DSML tool response")
            calls = list(
                DeepSeekResponsesProvider._parse_dsml_tool_calls(
                    content,
                    allowed_tool_names=frozenset(tool.name for tool in request.tools),
                    response_id=str(payload.get("id") or ""),
                )
            )
            content = ""
            message = {
                **message,
                "content": "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in calls
                ],
            }
        if not content.strip() and not calls and not truncated:
            raise LLMEmptyResponseError(
                "provider returned empty content",
                diagnostics={
                    "reasoning_only": bool(reasoning or message.get("reasoning_details")),
                },
            )
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("prompt_tokens_details") or {}
        output_details = usage.get("completion_tokens_details") or {}
        annotations = message.get("annotations")
        annotations = annotations if isinstance(annotations, list) else []
        citations = DeepSeekResponsesProvider._parse_annotations(annotations)
        continuation = None
        # Opaque reasoning_details (MiniMax/OpenRouter) must survive tool and recovery rounds.
        if (
            message.get("reasoning_details") is not None
            or message.get("encrypted_content") is not None
            or message.get("reasoning") is not None
            or isinstance(raw_content, list)
            or request.continuation is not None
        ):
            tail = self._history(request)[len(request.messages) :]
            assistant = {
                key: deepcopy(message[key])
                for key in (
                    "content",
                    "tool_calls",
                    "reasoning_content",
                    "reasoning_details",
                    "reasoning",
                    "encrypted_content",
                )
                if key in message
            }
            assistant["role"] = "assistant"
            continuation = ProviderContinuation(
                provider=self.provider_name,
                protocol=self.protocol,
                payload=tuple([*tail, assistant]),
            )
        request_id = payload.get("id")
        return ChatResponse(
            content=content.strip(),
            latency_seconds=0,
            provider_request_id=request_id if isinstance(request_id, str) else None,
            tool_calls=tuple(calls),
            reasoning_content=reasoning,
            prompt_tokens=integer(usage.get("prompt_tokens")),
            completion_tokens=integer(usage.get("completion_tokens")),
            total_tokens=integer(usage.get("total_tokens")),
            cached_prompt_tokens=integer(
                details.get("cached_tokens", usage.get("prompt_cache_hit_tokens"))
                if isinstance(details, dict)
                else usage.get("prompt_cache_hit_tokens")
            ),
            reasoning_tokens=integer(output_details.get("reasoning_tokens"))
            if isinstance(output_details, dict)
            else None,
            status=ModelResponseStatus.INCOMPLETE if truncated else ModelResponseStatus.COMPLETED,
            incomplete_reason="max_output_tokens" if truncated else None,
            citations=tuple(citations),
            continuation=continuation,
            native_tool_events=(
                NativeToolEvent(
                    tool_type=NativeToolType.WEB_SEARCH,
                    call_id=request_id if isinstance(request_id, str) and request_id else "search",
                    status=NativeToolStatus.COMPLETED,
                ),
            )
            if request.native_tools and citations and not truncated
            else (),
        )
