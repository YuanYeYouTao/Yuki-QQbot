"""Native Claude Messages adapter with signed thinking and ordered tool receipts."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FunctionCallOutput,
    ModelResponseStatus,
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
from qq_ai_bot.llm.json_http import JSONHTTPProvider
from qq_ai_bot.llm.protocol_state import checkpoint_items, integer, ordered_delta
from qq_ai_bot.llm.vendor_policy import ChatWireOptions, effort_value, thinking_budget, wire_options


class AnthropicMessagesProvider(JSONHTTPProvider):
    provider_name = "anthropic"
    protocol = "anthropic_messages"

    def __init__(self, *, options: ChatWireOptions | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.options = wire_options(self.provider_name, options)

    def _path(self, request: ChatRequest) -> str:
        return "messages"

    def _request_headers(self) -> dict[str, str]:
        return {
            "anthropic-version": "2023-06-01",
            **self._headers,
            "x-api-key": self._api_key,
        }

    def _message(self, message: ChatMessage) -> dict[str, Any]:
        if message.response_item is not None:
            raise LLMInvalidRequestError("opaque history requires its original protocol")
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        if message.images:
            if message.role != "user":
                raise LLMInvalidRequestError("images must be attached to a user message")
            for image in message.images:
                prefix, data = image.data_url.split(",", 1)
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": prefix[5:].split(";")[0],
                            "data": data,
                        },
                    }
                )
        for call in message.tool_calls:
            try:
                arguments = json.loads(call.function.arguments)
            except ValueError as exc:
                raise LLMInvalidRequestError("invalid local tool arguments") from exc
            blocks.append(
                {"type": "tool_use", "id": call.id, "name": call.function.name, "input": arguments}
            )
        if message.role == "tool":
            if not message.tool_call_id:
                raise LLMInvalidRequestError("tool result requires call ID")
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content or "",
                }
            ]
        return {"role": "assistant" if message.role == "assistant" else "user", "content": blocks}

    @staticmethod
    def _coalesce(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            if result and result[-1]["role"] == message["role"]:
                result[-1]["content"].extend(deepcopy(message["content"]))
            else:
                result.append(deepcopy(message))
        return result

    def _history(self, request: ChatRequest) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        system: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role in {"system", "developer"} and not messages:
                system.append({"type": "text", "text": message.content or ""})
            else:
                messages.append(self._message(message))
        messages.extend(checkpoint_items(request, self.provider_name, self.protocol))
        for item in ordered_delta(request):
            messages.append(
                self._message(
                    ChatMessage(role="tool", content=item.output, tool_call_id=item.call_id)
                    if isinstance(item, FunctionCallOutput)
                    else item,
                )
            )
        return system, self._coalesce(messages)

    def _build_payload(self, request: ChatRequest) -> dict[str, Any]:
        if request.native_tools:
            raise LLMUnsupportedFeatureError("Claude server tools are not in this native contract")
        system, messages = self._history(request)
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens or 4096,
            "messages": messages,
            "stream": False,
        }
        if system:
            system[-1]["cache_control"] = {"type": "ephemeral"}
            payload["system"] = system
        if request.thinking_enabled:
            if self.options.reasoning == "budget":
                budget = thinking_budget(self.options, request.reasoning_effort)
                if budget >= payload["max_tokens"]:
                    raise LLMInvalidRequestError(
                        "Claude thinking budget must be below output limit"
                    )
                payload["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": budget,
                }
            elif self.options.reasoning == "effort":
                payload["thinking"] = {"type": "adaptive"}
                effort = effort_value(self.options, request.reasoning_effort)
                payload["output_config"] = {"effort": effort}
            else:
                raise LLMUnsupportedFeatureError("Claude requires adaptive effort or manual budget")
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
            payload["tools"][-1]["cache_control"] = {"type": "ephemeral"}
            choice = request.tool_choice or "auto"
            # Thinking cannot be forced into a tool-only response; validation remains local.
            if request.thinking_enabled and choice not in {"auto", "none"}:
                choice = "auto"
            payload["tool_choice"] = (
                {"type": "any" if choice == "required" else choice}
                if choice in {"auto", "none", "required"}
                else {"type": "tool", "name": choice}
            )
        if request.response_format is not None:
            nested = request.response_format.get("json_schema")
            if request.response_format.get("type") != "json_schema" or not isinstance(nested, dict):
                raise LLMUnsupportedFeatureError("Claude structured output requires JSON Schema")
            payload.setdefault("output_config", {})["format"] = {
                "type": "json_schema",
                "schema": nested["schema"],
            }
        return payload

    def _parse(self, response: httpx.Response, request: ChatRequest) -> ChatResponse:
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMInvalidResponseError("provider returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
            raise LLMInvalidResponseError("Claude returned invalid content blocks")
        stop = payload.get("stop_reason")
        if stop not in {"end_turn", "tool_use", "max_tokens", "stop_sequence"}:
            raise LLMInvalidResponseError("Claude response was rejected or not completed")
        blocks = payload["content"]
        texts: list[str] = []
        reasoning: list[str] = []
        calls: list[ToolCall] = []
        for block in blocks:
            if not isinstance(block, dict):
                raise LLMInvalidResponseError("invalid Claude content block")
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif kind == "thinking" and isinstance(block.get("thinking"), str):
                reasoning.append(block["thinking"])
            elif kind == "tool_use":
                if not isinstance(block.get("input"), dict) or not all(
                    isinstance(block.get(key), str) and block[key] for key in ("id", "name")
                ):
                    raise LLMInvalidResponseError("invalid Claude tool call")
                calls.append(
                    ToolCall(
                        id=block["id"],
                        function=ToolFunction(
                            name=block["name"],
                            arguments=json.dumps(block["input"], ensure_ascii=False),
                        ),
                    )
                )
            elif kind != "redacted_thinking":
                raise LLMInvalidResponseError("unsupported Claude content block")
        if len({call.id for call in calls}) != len(calls):
            raise LLMInvalidResponseError("duplicate Claude tool IDs")
        truncated = stop == "max_tokens"
        content = "".join(texts)
        if not content and not calls and not truncated:
            raise LLMEmptyResponseError("Claude returned no visible text or tool calls")
        # Save only the protocol tail. Initial history is owned by TurnTranscript.
        # Coalescing can join a new tool result to the final initial message; use uncoalesced
        # checkpoint data so the initial prefix is never duplicated on replay.
        tail = checkpoint_items(request, self.provider_name, self.protocol)
        for item in ordered_delta(request):
            tail.append(
                self._message(
                    ChatMessage(
                        role="tool",
                        content=item.output,
                        tool_call_id=item.call_id,
                    )
                    if isinstance(item, FunctionCallOutput)
                    else item
                )
            )
        if blocks:
            tail.append({"role": "assistant", "content": deepcopy(blocks)})
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        incoming = integer(usage.get("input_tokens"))
        cached = integer(usage.get("cache_read_input_tokens"))
        creation = integer(usage.get("cache_creation_input_tokens"))
        output = integer(usage.get("output_tokens"))
        total_input = incoming + (cached or 0) + (creation or 0) if incoming is not None else None
        return ChatResponse(
            content=content,
            latency_seconds=0,
            provider_request_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
            reasoning_content="\n".join(reasoning) or None,
            tool_calls=tuple(calls),
            prompt_tokens=total_input,
            completion_tokens=output,
            cached_prompt_tokens=cached,
            total_tokens=total_input + output
            if total_input is not None and output is not None
            else None,
            status=ModelResponseStatus.INCOMPLETE if truncated else ModelResponseStatus.COMPLETED,
            incomplete_reason="max_output_tokens" if truncated else None,
            continuation=ProviderContinuation(
                provider=self.provider_name,
                protocol=self.protocol,
                payload=tuple(tail),
            ),
        )
