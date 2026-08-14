"""One strict structured-output path for multiple model tasks."""

from __future__ import annotations

import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse, ChatTool
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelExecutionPriority,
    ModelTask,
    StructuredOutputMode,
)

OutputT = TypeVar("OutputT", bound=BaseModel)
logger = logging.getLogger(__name__)

_MAX_REPAIR_RESULT_CHARACTERS = 8000


class StructuredTaskError(RuntimeError):
    """A structured request did not produce exactly one valid result."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "invalid_result",
        detail: str = "",
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.detail = detail
        self.attempts = attempts


class StructuredTaskRunner:
    """Render a Pydantic schema and validate bounded provider results."""

    def __init__(self, models: ModelExecutor) -> None:
        self._models = models

    async def run(
        self,
        *,
        task: ModelTask,
        instruction: str,
        structured_input: BaseModel | dict[str, Any] | list[Any],
        output_model: type[OutputT],
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        mode: StructuredOutputMode | None = None,
        allow_text_json: bool = False,
        compact_schema: bool = False,
        validation_retries: int = 0,
        validation_repair_hint: str = "",
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
    ) -> OutputT:
        result, _response = await self.run_with_response(
            task=task,
            instruction=instruction,
            structured_input=structured_input,
            output_model=output_model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            mode=mode,
            allow_text_json=allow_text_json,
            compact_schema=compact_schema,
            validation_retries=validation_retries,
            validation_repair_hint=validation_repair_hint,
            priority=priority,
        )
        return result

    async def run_with_response(
        self,
        *,
        task: ModelTask,
        instruction: str,
        structured_input: BaseModel | dict[str, Any] | list[Any],
        output_model: type[OutputT],
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        mode: StructuredOutputMode | None = None,
        allow_text_json: bool = False,
        compact_schema: bool = False,
        validation_retries: int = 0,
        validation_repair_hint: str = "",
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
    ) -> tuple[OutputT, ChatResponse]:
        """Return validated data together with provider-safe usage metadata."""

        if not instruction.strip():
            raise ValueError("structured task instruction must not be empty")
        effective_mode = mode or self._models.structured_output_mode(task)
        if effective_mode is StructuredOutputMode.TEXT_JSON and not allow_text_json:
            raise ValueError("text_json mode must be explicitly enabled for this task")
        if not 0 <= validation_retries <= 1:
            raise ValueError("validation_retries must be zero or one")
        if len(validation_repair_hint) > 1000:
            raise ValueError("validation_repair_hint must not exceed 1000 characters")
        if isinstance(structured_input, BaseModel):
            payload: Any = structured_input.model_dump(
                mode="json",
                exclude_none=True,
                exclude_defaults=True,
                exclude_computed_fields=True,
            )
        else:
            payload = structured_input
        schema = output_model.model_json_schema()
        if compact_schema:
            schema = _compact_json_schema(schema)
        tools: tuple[ChatTool, ...] = ()
        tool_choice: str | None = None
        response_format: dict[str, object] | None = None
        if effective_mode is StructuredOutputMode.FUNCTION_TOOL:
            tools = (
                ChatTool(
                    name="emit_result",
                    description="Return the validated task result.",
                    parameters=schema,
                ),
            )
            tool_choice = "required"
        elif effective_mode is StructuredOutputMode.JSON_SCHEMA:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "emit_result",
                    "strict": True,
                    "schema": schema,
                },
            }
        base_messages: tuple[ChatMessage, ...] = (
            ChatMessage(role="system", content=instruction.strip()),
            ChatMessage(
                role="user",
                content=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        messages: tuple[ChatMessage, ...] = base_messages
        for attempt in range(validation_retries + 1):
            request = ChatRequest(
                messages=messages,
                model=self._models.model_name(task),
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                thinking_enabled=False,
                tools=tools,
                tool_choice=tool_choice,
                response_format=response_format,
                structured_output=True,
            )
            if priority is ModelExecutionPriority.FOREGROUND:
                response = await self._models.execute(task, request)
            else:
                response = await self._models.execute(task, request, priority=priority)
            try:
                decoded = _decode_response(
                    response,
                    mode=effective_mode,
                    output_model=output_model,
                )
                if attempt:
                    logger.info(
                        "structured_task_validation_recovered task=%s attempts=%d",
                        task.value,
                        attempt + 1,
                    )
                return decoded, response
            except StructuredTaskError as exc:
                attempts = attempt + 1
                if attempt >= validation_retries:
                    logger.warning(
                        "structured_task_validation_failed task=%s attempts=%d reason=%s detail=%s",
                        task.value,
                        attempts,
                        exc.reason_code,
                        exc.detail or "none",
                    )
                    raise StructuredTaskError(
                        str(exc),
                        reason_code=exc.reason_code,
                        detail=exc.detail,
                        attempts=attempts,
                    ) from exc
                logger.warning(
                    "structured_task_validation_retry task=%s attempt=%d reason=%s detail=%s",
                    task.value,
                    attempts,
                    exc.reason_code,
                    exc.detail or "none",
                )
                messages = (
                    *base_messages,
                    ChatMessage(
                        role="user",
                        content=_repair_message(
                            response,
                            effective_mode,
                            exc,
                            repair_hint=validation_repair_hint,
                        ),
                    ),
                )
        raise AssertionError("structured validation loop exited unexpectedly")


def _decode_response[DecodedT: BaseModel](
    response: ChatResponse,
    *,
    mode: StructuredOutputMode,
    output_model: type[DecodedT],
) -> DecodedT:
    if mode is StructuredOutputMode.FUNCTION_TOOL:
        if len(response.tool_calls) != 1:
            raise StructuredTaskError(
                "structured task must return exactly one emit_result call",
                reason_code="tool_call_count",
                detail=f"count={len(response.tool_calls)}",
            )
        call = response.tool_calls[0]
        if call.function.name != "emit_result":
            raise StructuredTaskError(
                "structured task returned an unknown function",
                reason_code="unknown_function",
                detail=f"name={call.function.name[:64]}",
            )
        raw_result = call.function.arguments
    else:
        raw_result = response.content.strip()
    try:
        decoded = json.loads(raw_result)
    except json.JSONDecodeError as exc:
        raise StructuredTaskError(
            "structured task returned invalid JSON",
            reason_code="json_decode",
            detail=f"line={exc.lineno} column={exc.colno} error={exc.msg[:120]}",
        ) from exc
    if not isinstance(decoded, dict):
        raise StructuredTaskError(
            "structured text result must be one object",
            reason_code="result_type",
            detail=f"type={type(decoded).__name__}",
        )
    try:
        return output_model.model_validate(decoded)
    except ValidationError as exc:
        raise StructuredTaskError(
            "structured task result failed schema validation",
            reason_code="schema_validation",
            detail=_validation_error_detail(exc),
        ) from exc


def _validation_error_detail(exc: ValidationError) -> str:
    details: list[str] = []
    for item in exc.errors(
        include_url=False,
        include_context=False,
        include_input=True,
    )[:8]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "$"
        error_type = str(item.get("type", "validation_error"))
        detail = f"{location}:{error_type}"
        invalid_value = item.get("input")
        if error_type == "literal_error" and isinstance(invalid_value, str):
            visible = " ".join(invalid_value.split())[:64]
            detail += f" value={visible!r}"
        if error_type == "value_error":
            message = " ".join(str(item.get("msg", "")).split())[:160]
            if message:
                detail += f" message={message!r}"
        details.append(detail)
    return ",".join(details)[:500] or "unknown_validation_error"


def _repair_message(
    response: ChatResponse,
    mode: StructuredOutputMode,
    error: StructuredTaskError,
    *,
    repair_hint: str = "",
) -> str:
    if mode is StructuredOutputMode.FUNCTION_TOOL:
        previous: object = [
            {
                "name": call.function.name,
                "arguments": call.function.arguments,
            }
            for call in response.tool_calls
        ]
        if not response.tool_calls and response.content:
            previous = response.content
    else:
        previous = response.content
    return_channel = (
        "through exactly one emit_result call"
        if mode is StructuredOutputMode.FUNCTION_TOOL
        else "as exactly one JSON object"
    )
    serialized = json.dumps(previous, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(
        {
            "repair_request": {
                "reason_code": error.reason_code,
                "detail": error.detail,
                "instruction": (
                    "The previous result was structurally invalid. Return the complete result "
                    f"again {return_channel}, matching the supplied schema. "
                    "Do not explain the correction and do not omit required fields."
                ),
                "task_specific_hint": repair_hint or None,
            },
            "previous_invalid_result": serialized[:_MAX_REPAIR_RESULT_CHARACTERS],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _compact_json_schema(value: Any) -> Any:
    """Remove non-validating prose while preserving one stable strict schema."""

    if isinstance(value, dict):
        return {
            key: _compact_json_schema(item)
            for key, item in value.items()
            if key not in {"title", "description", "default"}
        }
    if isinstance(value, list):
        return [_compact_json_schema(item) for item in value]
    return value
