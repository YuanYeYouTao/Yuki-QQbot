"""DeepSeek non-streaming Responses API provider."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
from collections.abc import Iterable
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    CitationOrigin,
    FunctionCallOutput,
    ModelResponseStatus,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ProviderContinuation,
    ResponseCitation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMError,
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMNativeToolError,
    LLMProvider,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
    RetryableProviderError,
)

logger = logging.getLogger(__name__)

_CONTINUATION_TYPES = frozenset(
    {"reasoning", "message", "function_call", "function_call_output", "web_search_call"}
)


class DeepSeekResponsesProvider(LLMProvider):
    """Translate Yuki's compatibility models to DeepSeek Responses items."""

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
        payload = self._build_payload(request)
        started = time.perf_counter()
        # Native search can already have incurred provider-side work. Avoid replaying
        # it after an ambiguous response; local-function-only calls retain bounded retries.
        attempts = 1 if request.native_tools else self._max_retries + 1
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_random_exponential(multiplier=0.25, max=2),
                retry=retry_if_exception_type(
                    (httpx.ConnectError, httpx.TimeoutException, RetryableProviderError)
                ),
                reraise=True,
            ):
                with attempt:
                    response = await self._post(payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("LLM request timed out") from exc
        except (httpx.ConnectError, RetryableProviderError) as exc:
            raise LLMUnavailableError("LLM is temporarily unavailable") from exc

        latency = time.perf_counter() - started
        parsed = self._parse_response(
            response,
            request.continuation,
            function_outputs=request.function_outputs,
            allowed_tool_names=frozenset(tool.name for tool in request.tools),
            latency=latency,
        )
        completed = sum(
            event.status is NativeToolStatus.COMPLETED for event in parsed.native_tool_events
        )
        failed = sum(event.status is NativeToolStatus.FAILED for event in parsed.native_tool_events)
        logger.info(
            "responses_request_completed provider=deepseek protocol=responses success=true "
            "response_status=%s latency_seconds=%.3f input_tokens=%s output_tokens=%s "
            "reasoning_tokens=%s cached_tokens=%s function_call_count=%d "
            "native_web_used=%s native_action_count=%d native_completed_count=%d "
            "native_failed_count=%d citation_count=%d incomplete_reason=%s",
            parsed.status.value,
            latency,
            parsed.prompt_tokens,
            parsed.completion_tokens,
            parsed.reasoning_tokens,
            parsed.cached_prompt_tokens,
            len(parsed.tool_calls),
            bool(parsed.native_tool_events),
            len(parsed.native_tool_events),
            completed,
            failed,
            len(parsed.citations),
            parsed.incomplete_reason or "none",
        )
        return parsed

    def _build_payload(self, request: ChatRequest) -> dict[str, Any]:
        instructions, inputs = self._convert_messages(request.messages)
        continuation_items = self._continuation_items(request.continuation)
        function_outputs = [
            {
                "type": "function_call_output",
                "call_id": output.call_id,
                "output": output.output,
            }
            for output in request.function_outputs
        ]
        payload: dict[str, Any] = {
            "model": request.model,
            "input": [*inputs, *continuation_items, *function_outputs],
            "stream": False,
        }
        if instructions:
            payload["instructions"] = instructions
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in request.tools
        ]
        tools.extend({"type": tool.type.value} for tool in request.native_tools)
        if tools:
            payload["tools"] = tools
        # DeepSeek Responses does not accept the OpenAI tool_choice field.
        # Tool schemas stay available and the model selects them from the
        # trusted instructions; AgentRunner validates terminal effects locally.
        if request.thinking_enabled and request.reasoning_effort is not None:
            payload["reasoning"] = {"effort": request.reasoning_effort.value}
        if request.response_format is not None:
            payload["text"] = {"format": request.response_format}
        logger.info(
            "responses_request_started provider=deepseek protocol=responses model=%s "
            "native_tool_types=%s function_tool_count=%d continuation=%s",
            request.model,
            ",".join(tool.type.value for tool in request.native_tools) or "none",
            len(request.tools),
            request.continuation is not None,
        )
        return payload

    @staticmethod
    def _convert_messages(
        messages: tuple[ChatMessage, ...],
    ) -> tuple[str, list[dict[str, Any]]]:
        leading: list[str] = []
        index = 0
        while index < len(messages) and messages[index].role in {"system", "developer"}:
            content = messages[index].content
            if content:
                leading.append(content)
            index += 1
        inputs: list[dict[str, Any]] = []
        for message in messages[index:]:
            if message.tool_calls or message.tool_call_id:
                raise LLMInvalidRequestError(
                    "Responses requests must use continuation and function_call_output items"
                )
            if message.role not in {"user", "assistant", "system", "developer"}:
                raise LLMInvalidRequestError("unsupported Responses message role")
            if message.content is not None:
                inputs.append({"role": message.role, "content": message.content})
        return "\n\n".join(leading), inputs

    @staticmethod
    def _continuation_items(continuation: ProviderContinuation | None) -> list[dict[str, Any]]:
        if continuation is None:
            return []
        if continuation.provider != "deepseek" or continuation.protocol != "responses":
            raise LLMInvalidRequestError("continuation belongs to another provider or protocol")
        if not isinstance(continuation.payload, tuple) or not all(
            isinstance(item, dict) for item in continuation.payload
        ):
            raise LLMInvalidRequestError("invalid Responses continuation payload")
        return [dict(item) for item in continuation.payload]

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        response = await self._client.post(
            "/responses",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
            timeout=self._timeout,
        )
        if response.status_code >= 500:
            raise RetryableProviderError("provider returned a server error")
        if response.status_code in {401, 403}:
            raise LLMAuthenticationError("provider rejected credentials")
        if response.status_code == 429:
            raise LLMRateLimitError("provider rate limit exceeded")
        if response.status_code == 400:
            raise LLMInvalidRequestError("provider rejected the Responses request")
        if response.status_code >= 400:
            raise LLMError(f"provider rejected request with HTTP {response.status_code}")
        return response

    @classmethod
    def _parse_response(
        cls,
        response: httpx.Response,
        previous: ProviderContinuation | None,
        *,
        function_outputs: tuple[FunctionCallOutput, ...] = (),
        allowed_tool_names: frozenset[str] = frozenset(),
        latency: float,
    ) -> ChatResponse:
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMInvalidResponseError("provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise LLMInvalidResponseError("provider returned a non-object response")
        status = payload.get("status", "completed")
        if status == "failed":
            raise LLMUnavailableError("provider returned a failed response")
        if status not in {"completed", "incomplete"}:
            raise LLMInvalidResponseError("provider returned an unknown response status")
        output = payload.get("output")
        if not isinstance(output, list):
            raise LLMInvalidResponseError("provider returned no output items")

        messages: list[str] = []
        reasoning: list[str] = []
        calls: list[ToolCall] = []
        native_events: list[NativeToolEvent] = []
        citations: list[ResponseCitation] = []
        last_assistant_message: dict[str, Any] | None = None
        for raw_item in output:
            if not isinstance(raw_item, dict):
                raise LLMInvalidResponseError("provider returned an invalid output item")
            item_type = raw_item.get("type")
            if item_type == "message":
                text, item_citations = cls._parse_message(raw_item)
                if raw_item.get("role", "assistant") == "assistant" and text:
                    messages.append(text)
                    last_assistant_message = raw_item
                citations.extend(item_citations)
            elif item_type == "reasoning":
                reasoning.extend(cls._reasoning_text(raw_item))
            elif item_type == "function_call":
                calls.append(cls._parse_function_call(raw_item))
            elif item_type == "web_search_call":
                event = cls._parse_native_event(raw_item)
                native_events.append(event)
                logger.info(
                    "responses_native_tool_event tool_type=%s status=%s action_type=%s "
                    "provider_request_id=%s",
                    event.tool_type.value,
                    event.status.value,
                    event.action_type or "none",
                    payload.get("id") if isinstance(payload.get("id"), str) else "none",
                )
            elif item_type == "custom_tool_call":
                raise LLMNativeToolError("custom_tool_call is not supported")

        content = messages[-1].strip() if messages else ""
        continuation_output = output
        if content and cls._contains_dsml(content):
            raw_response_id = payload.get("id")
            textual_calls = cls._parse_dsml_tool_calls(
                content,
                allowed_tool_names=allowed_tool_names,
                response_id=raw_response_id if isinstance(raw_response_id, str) else "",
            )
            calls.extend(textual_calls)
            content = ""
            continuation_output = [item for item in output if item is not last_assistant_message]
            continuation_output.extend(
                {
                    "id": f"fc_{call.id}",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                }
                for call in textual_calls
            )
            logger.warning(
                "responses_textual_tool_call_recovered count=%d",
                len(textual_calls),
            )
        response_status = (
            ModelResponseStatus.INCOMPLETE
            if status == "incomplete"
            else ModelResponseStatus.COMPLETED
        )
        if not content and not calls and response_status is ModelResponseStatus.COMPLETED:
            raise LLMEmptyResponseError("provider returned no final message or function call")
        continuation = ProviderContinuation(
            provider="deepseek",
            protocol="responses",
            payload=cls._merge_continuation(previous, function_outputs, continuation_output),
        )
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        input_details = usage.get("input_tokens_details")
        input_details = input_details if isinstance(input_details, dict) else {}
        output_details = usage.get("output_tokens_details")
        output_details = output_details if isinstance(output_details, dict) else {}
        prompt_tokens = cls._integer(usage.get("input_tokens"))
        completion_tokens = cls._integer(usage.get("output_tokens"))
        total_tokens = cls._integer(usage.get("total_tokens"))
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        incomplete = payload.get("incomplete_details")
        incomplete_reason = cls._error_category(incomplete) if status == "incomplete" else None
        return ChatResponse(
            content=content,
            latency_seconds=latency,
            provider_request_id=(payload.get("id") if isinstance(payload.get("id"), str) else None),
            tool_calls=tuple(calls),
            reasoning_content="\n".join(reasoning) or None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_prompt_tokens=cls._integer(input_details.get("cached_tokens")),
            status=response_status,
            native_tool_events=tuple(native_events),
            citations=tuple(citations),
            continuation=continuation,
            reasoning_tokens=cls._integer(output_details.get("reasoning_tokens")),
            incomplete_reason=incomplete_reason,
        )

    @classmethod
    def _parse_message(cls, item: dict[str, Any]) -> tuple[str, tuple[ResponseCitation, ...]]:
        content = item.get("content")
        if isinstance(content, str):
            return content, ()
        if not isinstance(content, list):
            return "", ()
        texts: list[str] = []
        citations: list[ResponseCitation] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and part.get("type") in {"output_text", "text", None}:
                texts.append(text)
            annotations = part.get("annotations")
            if isinstance(annotations, list):
                citations.extend(cls._parse_annotations(annotations))
        return "".join(texts), tuple(citations)

    @staticmethod
    def _parse_annotations(annotations: list[Any]) -> list[ResponseCitation]:
        citations: list[ResponseCitation] = []
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            nested = annotation.get("url_citation")
            source = nested if isinstance(nested, dict) else annotation
            raw_url = source.get("url") or source.get("source_url")
            if not isinstance(raw_url, str) or not raw_url:
                continue
            raw_title = source.get("title")
            citations.append(
                ResponseCitation(
                    url=raw_url,
                    title=raw_title if isinstance(raw_title, str) else "",
                    origin=CitationOrigin.ANNOTATION,
                )
            )
        return citations

    @staticmethod
    def _reasoning_text(item: dict[str, Any]) -> Iterable[str]:
        for key in ("summary", "content"):
            raw = item.get(key)
            if isinstance(raw, str) and raw:
                yield raw
            elif isinstance(raw, list):
                for part in raw:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        yield part["text"]

    @staticmethod
    def _parse_function_call(item: dict[str, Any]) -> ToolCall:
        call_id = item.get("call_id") or item.get("id")
        name = item.get("name")
        arguments = item.get("arguments")
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
            or not isinstance(arguments, str)
        ):
            raise LLMInvalidResponseError("provider returned an invalid function_call")
        return ToolCall(id=call_id, function=ToolFunction(name=name, arguments=arguments))

    @staticmethod
    def _contains_dsml(content: str) -> bool:
        return "DSML" in content and ("<｜｜DSML｜｜" in content or "<||DSML||" in content)

    @classmethod
    def _parse_dsml_tool_calls(
        cls,
        content: str,
        *,
        allowed_tool_names: frozenset[str],
        response_id: str,
    ) -> tuple[ToolCall, ...]:
        normalized = content.replace("｜", "|").strip()
        wrapper = re.fullmatch(
            r"<\|\|DSML\|\|tool_calls>\s*(?P<body>.*?)\s*"
            r"</\|\|DSML\|\|tool_calls>",
            normalized,
            flags=re.DOTALL,
        )
        if wrapper is None:
            raise LLMInvalidResponseError("provider returned malformed textual tool markup")
        body = wrapper.group("body")
        invoke_pattern = re.compile(
            r"<\|\|DSML\|\|invoke(?P<attrs>[^>]*)>\s*(?P<body>.*?)\s*"
            r"</\|\|DSML\|\|invoke>",
            flags=re.DOTALL,
        )
        parameter_pattern = re.compile(
            r"<\|\|DSML\|\|parameter(?P<attrs>[^>]*)>"
            r"(?P<value>.*?)</\|\|DSML\|\|parameter>",
            flags=re.DOTALL,
        )
        calls: list[ToolCall] = []
        cursor = 0
        for index, invoke in enumerate(invoke_pattern.finditer(body)):
            if body[cursor : invoke.start()].strip():
                raise LLMInvalidResponseError("provider returned malformed textual tool markup")
            cursor = invoke.end()
            invoke_attrs = cls._parse_dsml_attributes(invoke.group("attrs"))
            name = invoke_attrs.get("name")
            if not name or name not in allowed_tool_names:
                raise LLMInvalidResponseError("provider returned an undeclared textual tool call")
            arguments: dict[str, Any] = {}
            parameter_body = invoke.group("body")
            parameter_cursor = 0
            for parameter in parameter_pattern.finditer(parameter_body):
                if parameter_body[parameter_cursor : parameter.start()].strip():
                    raise LLMInvalidResponseError(
                        "provider returned malformed textual tool arguments"
                    )
                parameter_cursor = parameter.end()
                attrs = cls._parse_dsml_attributes(parameter.group("attrs"))
                argument_name = attrs.get("name")
                if not argument_name or argument_name in arguments:
                    raise LLMInvalidResponseError(
                        "provider returned malformed textual tool arguments"
                    )
                raw_value = html.unescape(parameter.group("value").strip())
                if attrs.get("string", "false").lower() == "true":
                    arguments[argument_name] = raw_value
                else:
                    try:
                        arguments[argument_name] = json.loads(raw_value)
                    except json.JSONDecodeError as exc:
                        raise LLMInvalidResponseError(
                            "provider returned malformed textual tool arguments"
                        ) from exc
            if parameter_body[parameter_cursor:].strip():
                raise LLMInvalidResponseError("provider returned malformed textual tool arguments")
            arguments_json = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(
                f"{response_id}\0{index}\0{name}\0{arguments_json}".encode()
            ).hexdigest()[:24]
            calls.append(
                ToolCall(
                    id=f"call_dsml_{digest}",
                    function=ToolFunction(name=name, arguments=arguments_json),
                )
            )
        if body[cursor:].strip() or not calls:
            raise LLMInvalidResponseError("provider returned malformed textual tool markup")
        return tuple(calls)

    @staticmethod
    def _parse_dsml_attributes(raw: str) -> dict[str, str]:
        pattern = re.compile(r'([A-Za-z_][\w.-]*)\s*=\s*"([^"]*)"')
        attributes = {match.group(1): match.group(2) for match in pattern.finditer(raw)}
        if pattern.sub("", raw).strip():
            raise LLMInvalidResponseError("provider returned malformed textual tool attributes")
        return attributes

    @classmethod
    def _parse_native_event(cls, item: dict[str, Any]) -> NativeToolEvent:
        call_id = item.get("id") or item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise LLMInvalidResponseError("provider returned a native event without an id")
        raw_status = item.get("status", "in_progress")
        try:
            status = NativeToolStatus(str(raw_status))
        except ValueError:
            status = NativeToolStatus.IN_PROGRESS
        action = item.get("action")
        action = action if isinstance(action, dict) else {}
        action_type = action.get("type")
        query = action.get("query")
        url = action.get("url")
        error = item.get("error") or action.get("error")
        return NativeToolEvent(
            tool_type=NativeToolType.WEB_SEARCH,
            call_id=call_id,
            status=status,
            action_type=action_type if isinstance(action_type, str) else "",
            query=query if isinstance(query, str) else "",
            url=url if isinstance(url, str) else "",
            error_category=cls._error_category(error),
        )

    @classmethod
    def _merge_continuation(
        cls,
        previous: ProviderContinuation | None,
        function_outputs: tuple[FunctionCallOutput, ...],
        output: list[Any],
    ) -> tuple[dict[str, Any], ...]:
        merged = cls._continuation_items(previous)
        seen = {cls._item_identity(item) for item in merged}
        new_items: list[Any] = [
            {
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": item.output,
            }
            for item in function_outputs
        ]
        new_items.extend(output)
        for item in new_items:
            if not isinstance(item, dict) or item.get("type") not in _CONTINUATION_TYPES:
                continue
            identity = cls._item_identity(item)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(dict(item))
        return tuple(merged)

    @staticmethod
    def _item_identity(item: dict[str, Any]) -> tuple[str, str]:
        item_type = str(item.get("type", ""))
        identity = item.get("id") or item.get("call_id")
        return item_type, str(identity or id(item))

    @staticmethod
    def _integer(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _error_category(value: object) -> str | None:
        if isinstance(value, str):
            return value[:100]
        if isinstance(value, dict):
            for key in ("reason", "type", "code"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    return candidate[:100]
        return None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
