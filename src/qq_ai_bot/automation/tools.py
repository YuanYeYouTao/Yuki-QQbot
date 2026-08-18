"""Natural-language Agent tools for ordinary users and superusers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from qq_ai_bot.automation.compiler import ExecutionPlan
from qq_ai_bot.automation.models import AutomationRecord
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.time.formatting import local_iso

_CREATE_DESCRIPTION = (
    "根据高层 TaskSpec 创建真实持久化任务；不要手写 AutomationScript、步骤、"
    "底层 capability 名或预算。简单提醒使用 static 且 capabilities 留空；需要"
    "模型生成内容用 generated；需要运行时查询或操作外部系统用 agentic，并只"
    "选择必要 capability ID。不要把外部工具说明或 capability 目录写进本工具；"
    "capabilities 只填后端已委托的 ID，非法 ID 由后端拒绝。创建任务时不得提前"
    "执行这些外部工具。只有返回 confirmation='persisted' 和 automation_id 后"
    "才能告诉用户已经创建成功。"
)


def _object_schema(
    properties: Mapping[str, object], *, required: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _task_intent_schema() -> dict[str, object]:
    """Compact TaskSpec envelope for the model; backend still validates the real type."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "goal", "trigger"],
        "description": "高层任务意图。后端选择策略、计算预算并编译为 DSL。",
        "properties": {
            "version": {"type": "integer", "const": 1},
            "name": {"type": "string", "minLength": 1, "maxLength": 128},
            "goal": {"type": "string", "minLength": 1, "maxLength": 2500},
            "trigger": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "seconds"],
                        "properties": {
                            "type": {"const": "after"},
                            "seconds": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 31_536_000,
                            },
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "local_datetime"],
                        "properties": {
                            "type": {"const": "once"},
                            "local_datetime": {"type": "string"},
                            "timezone": {"type": "string", "maxLength": 64},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "hour", "minute"],
                        "properties": {
                            "type": {"const": "daily"},
                            "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                            "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                            "timezone": {"type": "string", "maxLength": 64},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "weekdays", "hour", "minute"],
                        "properties": {
                            "type": {"const": "weekly"},
                            "weekdays": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 7,
                                "items": {"type": "integer", "minimum": 1, "maximum": 7},
                            },
                            "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                            "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                            "timezone": {"type": "string", "maxLength": 64},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "seconds"],
                        "properties": {
                            "type": {"const": "interval"},
                            "seconds": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 31_536_000,
                            },
                        },
                    },
                ]
            },
            "timezone": {"type": "string", "minLength": 1, "maxLength": 64},
            "strategy": {
                "type": "string",
                "enum": ["auto", "static", "generated", "agentic"],
                "description": "纯提醒用 static；运行时需要模型或工具时用 agentic。",
            },
            "capabilities": {
                "type": "array",
                "maxItems": 128,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": "仅填写本任务运行时确实需要的 capability ID；简单提醒留空。",
            },
            "constraints": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "string"},
            },
            "context": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "scene": {
                        "type": "string",
                        "enum": ["none", "creator_private", "current_group"],
                    },
                    "include_relationship": {"type": "boolean"},
                    "include_memories": {"type": "boolean"},
                    "history_limit": {"type": "integer", "minimum": 0, "maximum": 30},
                },
            },
            "delivery": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["auto", "self_private", "current_group", "none"],
                    },
                    "text": {"type": "string", "minLength": 1, "maxLength": 12000},
                },
            },
        },
        "examples": [
            {
                "version": 1,
                "name": "五分钟后喝水提醒",
                "goal": "提醒我喝水",
                "trigger": {"type": "after", "seconds": 300},
                "strategy": "static",
                "capabilities": [],
                "constraints": [],
                "context": {"scene": "none"},
                "delivery": {"target": "auto", "text": "该喝水啦～"},
            }
        ],
    }


class AutomationToolService:
    """Expose owner-scoped task management bound to the current real event."""

    _NAMES = frozenset(
        {
            "automation_create",
            "automation_list",
            "automation_list_history",
            "automation_get",
            "automation_update",
            "automation_pause",
            "automation_resume",
            "automation_cancel",
            "automation_run_now",
            "automation_history",
            "automation_diagnose",
            "time_get_current",
            "time_get_timezone",
            "time_set_timezone",
        }
    )
    _ALLOWED_ARGUMENTS: ClassVar[dict[str, frozenset[str]]] = {
        "automation_create": frozenset({"task", "max_runs"}),
        "automation_list": frozenset(),
        "automation_list_history": frozenset({"limit"}),
        "automation_get": frozenset({"automation_id"}),
        "automation_update": frozenset({"automation_id", "task"}),
        "automation_pause": frozenset({"automation_id"}),
        "automation_resume": frozenset({"automation_id"}),
        "automation_cancel": frozenset({"automation_id"}),
        "automation_run_now": frozenset({"automation_id"}),
        "automation_history": frozenset({"automation_id"}),
        "automation_diagnose": frozenset(),
        "time_get_current": frozenset(),
        "time_get_timezone": frozenset(),
        "time_set_timezone": frozenset({"timezone"}),
    }

    def __init__(self, service: AutomationService) -> None:
        self._service = service

    def definitions(self) -> tuple[ChatTool, ...]:
        task_schema = _task_intent_schema()
        id_schema = {"automation_id": {"type": "integer", "minimum": 1}}
        definitions = (
            ChatTool(
                name="automation_create",
                description=_CREATE_DESCRIPTION,
                parameters=_object_schema(
                    {"task": task_schema, "max_runs": {"type": "integer", "minimum": 1}},
                    required=("task",),
                ),
            ),
            ChatTool(
                name="automation_list",
                description=(
                    "只列出当前真实发送者仍在运行或暂停的任务。每条任务返回并显示稳定的 "
                    "automation_id，后续查看、修改或取消必须使用该 ID；不要生成临时编号。"
                    "已结束任务请使用 automation_list_history。"
                ),
                parameters=_object_schema({}),
            ),
            ChatTool(
                name="automation_list_history",
                description=(
                    "单独列出当前真实发送者已完成、取消、失败或阻塞的任务历史，"
                    "每条任务显示稳定的 automation_id。"
                ),
                parameters=_object_schema(
                    {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}
                ),
            ),
            ChatTool(
                name="automation_get",
                description="查看当前真实发送者自己的一个自动化任务。",
                parameters=_object_schema(id_schema, required=("automation_id",)),
            ),
            ChatTool(
                name="automation_update",
                description=(
                    "用完整的新 TaskSpec 编译并替换任务版本；只能修改当前发送者自己的任务。"
                ),
                parameters=_object_schema(
                    {**id_schema, "task": task_schema},
                    required=("automation_id", "task"),
                ),
            ),
            ChatTool(
                name="automation_diagnose",
                description=(
                    "读取当前真实发送者最近的自动化创建结果，用于核实任务是否真的持久化或定位失败。"
                ),
                parameters=_object_schema({}),
            ),
            *(
                ChatTool(
                    name=f"automation_{operation}",
                    description=f"{description}当前真实发送者自己的任务。",
                    parameters=_object_schema(id_schema, required=("automation_id",)),
                )
                for operation, description in (
                    ("pause", "暂停"),
                    ("resume", "恢复"),
                    ("cancel", "取消"),
                    ("run_now", "立即调度执行一次"),
                    ("history", "查看执行历史"),
                )
            ),
            ChatTool(
                name="time_get_current",
                description="读取后端可信的当前 UTC、本地时间、日期、星期和时区。",
                parameters=_object_schema({}),
            ),
            ChatTool(
                name="time_get_timezone",
                description="读取当前真实发送者保存的 IANA 时区。",
                parameters=_object_schema({}),
            ),
            ChatTool(
                name="time_set_timezone",
                description="设置当前真实发送者自己的 IANA 时区。",
                parameters=_object_schema(
                    {"timezone": {"type": "string", "maxLength": 64}},
                    required=("timezone",),
                ),
            ),
        )
        if self._service.enabled:
            return definitions
        return tuple(tool for tool in definitions if tool.name.startswith("time_"))

    def owns(self, name: str) -> bool:
        return name in self._NAMES

    async def execute(self, name: str, arguments_json: str, runtime: ToolRuntime) -> str:
        if not self._valid_runtime(runtime):
            return _result(
                error="permission_context_mismatch", detail="自动化工具未绑定当前真实消息"
            )
        inbound = runtime.inbound
        try:
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            if name == "automation_create":
                await self._service.record_creation_failure(
                    inbound=inbound,
                    conversation_key=runtime.conversation_key,
                    error=exc,
                )
            return _result(error="invalid_arguments", detail=str(exc))
        allowed = self._ALLOWED_ARGUMENTS.get(name)
        if allowed is None:
            return _result(error="unknown_tool", detail=f"未知自动化工具：{name}")
        unexpected = set(arguments) - allowed
        if unexpected:
            error = ValueError(f"不接受参数：{', '.join(sorted(unexpected))}")
            if name == "automation_create":
                await self._service.record_creation_failure(
                    inbound=inbound,
                    conversation_key=runtime.conversation_key,
                    error=error,
                )
            return _result(
                error="invalid_arguments",
                detail=str(error),
            )
        try:
            if name == "automation_create":
                row, plan = await self._service.create_task(
                    arguments.get("task"),
                    inbound=inbound,
                    conversation_key=runtime.conversation_key,
                    max_runs=arguments.get("max_runs"),
                )
                return _result(
                    data=_record(row, plan=plan, persisted=True),
                    public_message=f"自动化任务已创建（ID {row.id}）。",
                    mutation_committed=True,
                )
            if name == "automation_list":
                automations = await self._service.list_current(inbound.sender.user_id)
                return _result(
                    data={
                        "timezone": await self._service.timezone(inbound.sender.user_id),
                        "current_tasks": [_record(row) for row in automations],
                    }
                )
            if name == "automation_list_history":
                maximum = arguments.get("limit", 50)
                if isinstance(maximum, bool) or not isinstance(maximum, int):
                    raise ValueError("limit 必须是整数")
                automations = await self._service.list_completed(inbound.sender.user_id)
                return _result(
                    data={
                        "timezone": await self._service.timezone(inbound.sender.user_id),
                        "completed_history": [_record(row) for row in automations[:maximum]],
                    }
                )
            if name == "time_get_current":
                return _result(data=await self._service.current_time(inbound.sender.user_id))
            if name == "time_get_timezone":
                return _result(
                    data={"timezone": await self._service.timezone(inbound.sender.user_id)}
                )
            if name == "time_set_timezone":
                timezone = await self._service.set_timezone(
                    inbound.sender.user_id, str(arguments.get("timezone") or "")
                )
                return _result(
                    data={"timezone": timezone},
                    public_message=f"时区已设置为 {timezone}。",
                    mutation_committed=True,
                )
            if name == "automation_diagnose":
                return _result(
                    data={
                        "recent_creation_outcomes": await self._service.diagnose_creation(
                            inbound.sender.user_id
                        )
                    }
                )
            automation_id = _automation_id(arguments)
            if name == "automation_get":
                return _result(
                    data=_record(
                        await self._service.require_owned(automation_id, inbound.sender.user_id)
                    )
                )
            if name == "automation_update":
                row, plan = await self._service.update_task(
                    automation_id,
                    arguments.get("task"),
                    inbound=inbound,
                    conversation_key=runtime.conversation_key,
                )
                return _result(
                    data=_record(row, plan=plan, persisted=True),
                    public_message=f"自动化任务已更新（ID {row.id}）。",
                    mutation_committed=True,
                )
            if name == "automation_pause":
                changed = await self._service.pause(
                    automation_id, inbound=inbound, conversation_key=runtime.conversation_key
                )
            elif name == "automation_resume":
                changed = await self._service.resume(
                    automation_id, inbound=inbound, conversation_key=runtime.conversation_key
                )
            elif name == "automation_cancel":
                changed = await self._service.cancel(
                    automation_id, inbound=inbound, conversation_key=runtime.conversation_key
                )
            elif name == "automation_run_now":
                changed = await self._service.run_now(
                    automation_id, inbound=inbound, conversation_key=runtime.conversation_key
                )
            elif name == "automation_history":
                task = await self._service.require_owned(automation_id, inbound.sender.user_id)
                history_rows = await self._service.history(
                    automation_id, creator_user_id=inbound.sender.user_id
                )
                return _result(
                    data={
                        "runs": [
                            {
                                "id": row.id,
                                "status": row.status.value,
                                "scheduled_for_local": local_iso(row.scheduled_for, task.timezone),
                                "finished_at_local": local_iso(row.finished_at, task.timezone),
                                "timezone": task.timezone,
                                "error_category": row.error_category,
                            }
                            for row in history_rows
                        ]
                    }
                )
            else:
                return _result(error="unknown_tool", detail=f"未知自动化工具：{name}")
            public_message = {
                "automation_pause": "任务已暂停。" if changed else "任务状态没有改变。",
                "automation_resume": "任务已恢复。" if changed else "该任务不能恢复。",
                "automation_cancel": "任务已取消。" if changed else "任务状态没有改变。",
                "automation_run_now": (
                    "任务已进入待执行队列。" if changed else "该任务不能立即执行。"
                ),
            }[name]
            return _result(
                data={"automation_id": automation_id, "changed": changed},
                public_message=public_message,
                mutation_committed=changed,
            )
        except (PermissionError, ValueError) as exc:
            if name == "automation_create":
                await self._service.record_creation_failure(
                    inbound=inbound,
                    conversation_key=runtime.conversation_key,
                    error=exc,
                )
            return _result(error=type(exc).__name__, detail=str(exc))

    @staticmethod
    def _valid_runtime(runtime: ToolRuntime) -> bool:
        inbound = runtime.inbound
        return bool(
            runtime.allow_automation
            and runtime.actor_user_id == inbound.sender.user_id
            and runtime.trigger_message_id == inbound.message_id
            and runtime.current_group_id == inbound.group_id
        )


def _automation_id(arguments: dict[str, Any]) -> int:
    value = arguments.get("automation_id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("automation_id 必须是正整数")
    return value


def _record(
    row: AutomationRecord,
    *,
    plan: ExecutionPlan | None = None,
    persisted: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "automation_id": row.id,
        "name": row.name,
        "status": row.status.value,
        "timezone": row.timezone,
        "schedule": row.script.schedule.model_dump(mode="json"),
        "next_run_at_local": local_iso(row.next_run_at, row.timezone),
        "required_capabilities": row.required_capabilities,
        "run_count": row.run_count,
    }
    if plan is not None:
        payload["compiled_strategy"] = plan.strategy
        payload["selected_capabilities"] = plan.selected_capabilities
        payload["warnings"] = plan.warnings
    if persisted:
        payload["confirmation"] = "persisted"
    return payload


def _result(
    *,
    data: object = None,
    error: str | None = None,
    detail: str = "",
    public_message: str | None = None,
    mutation_committed: bool | None = None,
) -> str:
    payload: dict[str, object] = {"ok": error is None}
    if error is None:
        payload["data"] = data
        if public_message is not None:
            payload["public_message"] = public_message
        if mutation_committed is not None:
            payload["mutation_committed"] = mutation_committed
    else:
        payload.update({"error": error, "detail": detail})
    return json.dumps(payload, ensure_ascii=False, default=str)
