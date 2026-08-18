"""Content-free counters for conversation history rollup."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ConversationHistoryWorkerHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    enabled: bool
    running: bool
    worker_count: int = Field(ge=0)
    claimed: int = Field(ge=0)
    completed: int = Field(ge=0)
    retried: int = Field(ge=0)
    failed: int = Field(ge=0)
    wakes: int = Field(ge=0)
    stale_leases_released: int = Field(ge=0)


class ConversationHistoryMetrics:
    """In-memory counters. No prompts, events, or summaries."""

    def __init__(self) -> None:
        self.claimed = 0
        self.completed = 0
        self.retried = 0
        self.failed = 0
        self.wakes = 0
        self.stale_leases_released = 0

    def snapshot(
        self,
        *,
        enabled: bool,
        running: bool,
        worker_count: int,
    ) -> ConversationHistoryWorkerHealth:
        return ConversationHistoryWorkerHealth(
            ok=enabled is False or running,
            enabled=enabled,
            running=running,
            worker_count=worker_count,
            claimed=self.claimed,
            completed=self.completed,
            retried=self.retried,
            failed=self.failed,
            wakes=self.wakes,
            stale_leases_released=self.stale_leases_released,
        )
