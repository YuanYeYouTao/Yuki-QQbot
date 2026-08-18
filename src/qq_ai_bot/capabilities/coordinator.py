"""Provider-neutral ordered execution for one model tool-call batch."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from qq_ai_bot.domain.messages import ToolCall

logger = logging.getLogger(__name__)
TOOL_RESULT_MISSING = "tool_result_missing"
MISSING_TOOL_RESULT = json.dumps(
    {
        "ok": False,
        "error": TOOL_RESULT_MISSING,
        "detail": "工具批次缺少这次调用的回执，已跳过而不是中断整轮。",
    },
    ensure_ascii=False,
)


class CoordinatedToolBackend(Protocol):
    async def execute(self, name: str, arguments_json: str, runtime: Any) -> str: ...

    def parallel_safe(self, name: str, runtime: Any) -> bool: ...


@dataclass(frozen=True, slots=True)
class CoordinatedToolResult:
    calls: tuple[tuple[ToolCall, str, bool], ...]
    executed_count: int
    reused_count: int = 0


class ToolInvocationCoordinator:
    """Run read-safe stretches concurrently while preserving model call order."""

    async def execute_batch(
        self,
        calls: tuple[ToolCall, ...],
        backend: CoordinatedToolBackend | None,
        runtime: Any,
        *,
        remaining_calls: int,
        max_parallel_calls: int,
    ) -> CoordinatedToolResult:
        if remaining_calls < 0 or max_parallel_calls <= 0:
            raise ValueError("tool call budgets must be non-negative and parallelism positive")
        if backend is None:
            unavailable = json.dumps(
                {"ok": False, "error": "tools_unavailable"},
                ensure_ascii=False,
            )
            return CoordinatedToolResult(
                tuple((call, unavailable, False) for call in calls),
                0,
            )

        def counts_toward_limit(call: ToolCall) -> bool:
            check = getattr(backend, "counts_toward_limit", None)
            return not callable(check) or bool(check(call.function.name, runtime))

        executable_list: list[ToolCall] = []
        overflow_ids: set[str] = set()
        counted_executions = 0
        for call in calls:
            counted = counts_toward_limit(call)
            if counted and counted_executions >= remaining_calls:
                overflow_ids.add(call.id)
                continue
            executable_list.append(call)
            if counted:
                counted_executions += 1
        executable = tuple(executable_list)
        results: dict[str, str] = {}
        semaphore = asyncio.Semaphore(max_parallel_calls)

        async def execute_one(call: ToolCall) -> None:
            async with semaphore:
                results[call.id] = await backend.execute(
                    call.function.name,
                    call.function.arguments,
                    runtime,
                )

        def is_parallel_safe(call: ToolCall) -> bool:
            check = getattr(backend, "parallel_safe", None)
            return bool(callable(check) and check(call.function.name, runtime))

        index = 0
        while index < len(executable):
            call = executable[index]
            if not is_parallel_safe(call):
                await execute_one(call)
                index += 1
                continue
            end = index + 1
            while end < len(executable) and is_parallel_safe(executable[end]):
                end += 1
            async with asyncio.TaskGroup() as group:
                for candidate in executable[index:end]:
                    group.create_task(execute_one(candidate))
            index = end

        limited = json.dumps(
            {"ok": False, "error": "tool_limit_exceeded"},
            ensure_ascii=False,
        )
        return CoordinatedToolResult(
            _attach_batch_results(
                calls, results=results, overflow_ids=overflow_ids, limited=limited
            ),
            counted_executions,
        )


def _attach_batch_results(
    calls: tuple[ToolCall, ...],
    *,
    results: dict[str, str],
    overflow_ids: set[str],
    limited: str,
) -> tuple[tuple[ToolCall, str, bool], ...]:
    """Map each model call id back to a payload without raising on a missing key."""

    ordered: list[tuple[ToolCall, str, bool]] = []
    for call in calls:
        if call.id in overflow_ids:
            ordered.append((call, limited, False))
            continue
        payload = results.get(call.id)
        if payload is None:
            logger.error("tool_result_missing call_id=%s", call.id)
            ordered.append((call, MISSING_TOOL_RESULT, False))
            continue
        ordered.append((call, payload, True))
    return tuple(ordered)
