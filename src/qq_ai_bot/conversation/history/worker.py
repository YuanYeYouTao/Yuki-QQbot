"""Durable conversation history rollup worker. Jobs store ranges, not event text."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.history.errors import (
    ConversationHistoryError,
    ConversationSummaryQualityError,
    FrontierInvariantError,
    HistoryIdentityError,
    HistoryJobConflictError,
)
from qq_ai_bot.conversation.history.metrics import (
    ConversationHistoryMetrics,
    ConversationHistoryWorkerHealth,
)
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryJob,
    HistoryJobOutcome,
)
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.model_runtime.executor import BackgroundModelPreempted
from qq_ai_bot.model_runtime.structured import StructuredTaskError

logger = logging.getLogger(__name__)

_PERMANENT_ERRORS = (
    HistoryIdentityError,
    FrontierInvariantError,
    HistoryJobConflictError,
)


@dataclass(frozen=True, slots=True)
class ConversationHistoryJobResult:
    outcome: HistoryJobOutcome
    result_summary_id: int | None = None


class ConversationHistoryJobProcessor(Protocol):
    async def process(self, job: ConversationHistoryJob) -> ConversationHistoryJobResult: ...


class ConversationHistoryWorker:
    """Claim durable jobs, run them in the background, and never block chat."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: ConversationHistoryRepository,
        processor: ConversationHistoryJobProcessor | None = None,
        metrics: ConversationHistoryMetrics | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._processor = processor
        self.metrics = metrics or ConversationHistoryMetrics()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._owners: tuple[str, ...] = ()
        self._retry_schedule = _parse_retry_schedule(
            settings.conversation_history_rollup_retry_seconds
        )

    def set_processor(self, processor: ConversationHistoryJobProcessor | None) -> None:
        self._processor = processor

    def notify(self) -> None:
        self.metrics.wakes += 1
        self._wake.set()

    async def start(self) -> None:
        if not self._settings.conversation_history_rollup_enabled or self._tasks:
            return
        self._stop.clear()
        released = await self._repository.release_stale_leases()
        self.metrics.stale_leases_released += released
        count = self._settings.conversation_history_rollup_worker_concurrency
        self._owners = tuple(
            f"history-rollup-{index}-{uuid.uuid4().hex[:8]}" for index in range(count)
        )
        self._tasks = [
            asyncio.create_task(self._run(owner), name=f"conversation-history-rollup-{index}")
            for index, owner in enumerate(self._owners)
        ]
        logger.info(
            "conversation_history_worker_started workers=%s poll=%s lease=%s",
            count,
            self._settings.conversation_history_rollup_poll_seconds,
            self._settings.conversation_history_rollup_lease_seconds,
        )

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        tasks = self._tasks
        self._tasks = []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for owner in self._owners:
            released = await self._repository.release_leases_for_owner(owner)
            self.metrics.stale_leases_released += released
        self._owners = ()

    async def health(self) -> ConversationHistoryWorkerHealth:
        return self.metrics.snapshot(
            enabled=self._settings.conversation_history_rollup_enabled,
            running=bool(self._tasks),
            worker_count=len(self._tasks),
        )

    async def _run(self, lease_owner: str) -> None:
        while not self._stop.is_set():
            if self._processor is None:
                await self._idle()
                continue
            try:
                job = await self._repository.claim_next_job(
                    lease_owner=lease_owner,
                    lease_seconds=self._settings.conversation_history_rollup_lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except ConversationHistoryError as exc:
                logger.warning(
                    "conversation_history_claim_failed error_category=%s",
                    type(exc).__name__,
                )
                await self._idle()
                continue
            if job is None:
                await self._idle()
                continue
            self.metrics.claimed += 1
            await self._execute(job, lease_owner)

    async def _execute(self, job: ConversationHistoryJob, lease_owner: str) -> None:
        processor = self._processor
        if processor is None:
            await self._retry_or_fail(job, lease_owner, "processor_missing")
            return
        try:
            result = await asyncio.wait_for(
                processor.process(job),
                timeout=self._settings.conversation_history_rollup_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._retry_or_fail(job, lease_owner, "timeout")
            return
        except BackgroundModelPreempted:
            await self._retry_or_fail(job, lease_owner, "preempted")
            return
        except (StructuredTaskError, ConversationSummaryQualityError):
            await self._retry_or_fail(job, lease_owner, "structured_output")
            return
        except _PERMANENT_ERRORS as exc:
            await self._fail(job, lease_owner, type(exc).__name__)
            return
        except (OSError, RuntimeError, ValueError) as exc:
            await self._retry_or_fail(job, lease_owner, type(exc).__name__)
            return
        try:
            await self._repository.complete_job(
                job.id,
                lease_owner=lease_owner,
                outcome=result.outcome,
                result_summary_id=result.result_summary_id,
            )
            self.metrics.completed += 1
        except HistoryJobConflictError as exc:
            logger.warning(
                "conversation_history_complete_conflict error_category=%s",
                type(exc).__name__,
            )

    async def _retry_or_fail(
        self,
        job: ConversationHistoryJob,
        lease_owner: str,
        error_category: str,
    ) -> None:
        if job.attempts >= self._settings.conversation_history_rollup_max_attempts:
            await self._fail(job, lease_owner, error_category)
            return
        delay = self._retry_schedule[min(job.attempts - 1, len(self._retry_schedule) - 1)]
        try:
            await self._repository.retry_job(
                job.id,
                lease_owner=lease_owner,
                delay_seconds=delay,
                error_category=error_category,
            )
            self.metrics.retried += 1
        except HistoryJobConflictError:
            return

    async def _fail(
        self,
        job: ConversationHistoryJob,
        lease_owner: str,
        error_category: str,
    ) -> None:
        try:
            await self._repository.fail_job(
                job.id,
                lease_owner=lease_owner,
                error_category=error_category,
            )
            self.metrics.failed += 1
        except HistoryJobConflictError:
            return

    async def _idle(self) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(
                self._wake.wait(),
                timeout=self._settings.conversation_history_rollup_poll_seconds,
            )
        except TimeoutError:
            return
        except asyncio.CancelledError:
            raise


def _parse_retry_schedule(raw: str) -> tuple[int, ...]:
    delays = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    if not delays:
        raise ValueError("conversation history retry schedule must not be empty")
    return delays
