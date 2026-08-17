"""Strict automation DSL and runtime projections."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# TurnOrigin moved to the neutral runtime layer in 3.6.0-R1; this re-export
# keeps the pre-existing import sites working until they migrate.
from qq_ai_bot.runtime.origin import TurnOrigin as TurnOrigin


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AfterSchedule(StrictModel):
    type: Literal["after"]
    seconds: int = Field(ge=1, le=31_536_000)


class OnceSchedule(StrictModel):
    type: Literal["once"]
    local_datetime: datetime
    timezone: str | None = Field(default=None, max_length=64)


class DailySchedule(StrictModel):
    type: Literal["daily"]
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    timezone: str | None = Field(default=None, max_length=64)


class WeeklySchedule(StrictModel):
    type: Literal["weekly"]
    weekdays: tuple[int, ...] = Field(min_length=1, max_length=7)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    timezone: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _valid_weekdays(self) -> WeeklySchedule:
        if any(day < 1 or day > 7 for day in self.weekdays):
            raise ValueError("weekdays 必须使用星期一=1 到星期日=7")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays 不能重复")
        return self


class IntervalSchedule(StrictModel):
    type: Literal["interval"]
    seconds: int = Field(ge=1, le=31_536_000)


Schedule = Annotated[
    AfterSchedule | OnceSchedule | DailySchedule | WeeklySchedule | IntervalSchedule,
    Field(discriminator="type"),
]


class AutomationContext(StrictModel):
    scene: Literal["none", "creator_private", "current_group"] = "none"
    include_relationship: bool = False
    include_memories: bool = False
    history_limit: int = Field(default=0, ge=0, le=30)


class AutomationStep(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    call: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]
    save_as: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")


class AutomationLimits(StrictModel):
    max_steps: int = Field(default=3, ge=1, le=16)
    max_llm_calls: int = Field(default=1, ge=0, le=10)
    max_tool_calls: int = Field(default=3, ge=1, le=16)
    max_messages: int = Field(default=1, ge=0, le=10)
    timeout_seconds: int = Field(default=60, ge=1, le=600)


class AutomationScript(StrictModel):
    """Versioned JSON-only automation declaration accepted from the Agent."""

    version: Literal[1]
    name: str = Field(min_length=1, max_length=128)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    schedule: Schedule
    context: AutomationContext = Field(default_factory=AutomationContext)
    steps: tuple[AutomationStep, ...] = Field(min_length=1, max_length=16)
    limits: AutomationLimits = Field(default_factory=AutomationLimits)

    @model_validator(mode="after")
    def _consistent_limits(self) -> AutomationScript:
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("步骤 id 不能重复")
        aliases = [step.save_as for step in self.steps if step.save_as]
        if len(set(aliases)) != len(aliases):
            raise ValueError("save_as 不能重复")
        if len(self.steps) > self.limits.max_steps:
            raise ValueError("步骤数量超过脚本 limits.max_steps")
        return self


class AutomationStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    BLOCKED = "blocked"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    MISSED = "missed"
    UNCERTAIN = "uncertain"
    BLOCKED = "blocked"


class RiskClass(StrEnum):
    READ = "read"
    GENERATE = "generate"
    SEND = "send"
    MUTATE = "mutate"
    DESTRUCTIVE = "destructive"


class RetryPolicy(StrEnum):
    NONE = "none"
    TRANSIENT_ONCE = "transient_once"


class AutomationRecord(StrictModel):
    id: int
    creator_user_id: str
    bot_user_id: str
    name: str
    status: AutomationStatus
    timezone: str
    script: AutomationScript
    script_hash: str
    required_capabilities: tuple[str, ...]
    authority_snapshot: dict[str, Any]
    created_from_message_id: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    run_count: int
    max_runs: int | None
    consecutive_failures: int
    misfire_grace_seconds: int
    created_at: datetime
    updated_at: datetime


class AutomationRunRecord(StrictModel):
    id: int
    automation_id: int
    scheduled_for: datetime
    actual_started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    steps_completed: int
    llm_calls: int
    tool_calls: int
    messages_sent: int
    error_category: str | None
    result_summary: dict[str, Any]


class ExecutionResult(StrictModel):
    status: RunStatus
    steps_completed: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    messages_sent: int = 0
    error_category: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
