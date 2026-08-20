"""Self-recovering worker for single-row scope signals."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.conversation.rollup.errors import (
    ConversationCoverageError,
    RollupLeaseLostError,
    RollupSourceChangedError,
)
from qq_ai_bot.conversation.rollup.metrics import ConversationRollupMetrics
from qq_ai_bot.conversation.rollup.models import RollupJobClaim
from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
from qq_ai_bot.conversation.rollup.service import ConversationRollupService

logger = logging.getLogger(__name__)


class ConversationRollupWorker:
    """Claim jobs with owner+token leases and never create terminal failures."""

    def __init__(
        self,
        *,
        repository: ConversationRollupRepository,
        service: ConversationRollupService,
        enabled: bool,
        concurrency: int,
        poll_seconds: float,
        lease_seconds: int,
        heartbeat_seconds: float,
        retry_max_seconds: int,
        max_batches_per_claim: int,
        metrics: ConversationRollupMetrics | None = None,
    ) -> None:
        self._repository = repository
        self._service = service
        self._enabled = enabled
        self._concurrency = concurrency
        self._poll_seconds = poll_seconds
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._retry_max_seconds = retry_max_seconds
        self._max_batches_per_claim = max_batches_per_claim
        self.metrics = metrics or service.metrics
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._owners: tuple[str, ...] = ()

    def notify(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return bool(self._tasks) and all(not task.done() for task in self._tasks)

    async def health(self) -> dict[str, object]:
        snapshot = await self._repository.health_snapshot()
        snapshot.update(
            {
                "enabled": self._enabled,
                "running": self.running,
                "model_success_total": self.metrics.model_summaries,
                "extractive_total": self.metrics.extractive_fallbacks,
                "infrastructure_retry_total": self.metrics.infrastructure_retries,
                "commit_conflict_total": (
                    self.metrics.lease_conflicts + self.metrics.source_conflicts
                ),
                "late_visual_total": self.metrics.late_visual_after_coverage,
                "scoped_append_repair_total": self.metrics.scoped_append_repairs,
                "counter_repair_total": self.metrics.counter_repairs,
                "counter_reconcile_failure_total": (self.metrics.counter_reconcile_failures),
            }
        )
        return snapshot

    async def start(self) -> None:
        if not self._enabled or self._tasks:
            return
        self._stop.clear()
        self._owners = tuple(
            f"conversation-rollup-{index}-{uuid.uuid4().hex[:8]}"
            for index in range(self._concurrency)
        )
        self._tasks = [
            asyncio.create_task(self._run(owner), name=f"conversation-rollup-{index}")
            for index, owner in enumerate(self._owners)
        ]

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for owner in self._owners:
            try:
                await self._repository.release_owner(owner)
            except (OSError, RuntimeError, SQLAlchemyError):
                logger.warning("conversation_rollup_shutdown_release_failed")
        self._owners = ()

    async def _run(self, owner: str) -> None:
        while not self._stop.is_set():
            try:
                claim = await self._repository.claim_next_job(
                    lease_owner=owner, lease_seconds=self._lease_seconds
                )
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, SQLAlchemyError):
                await self._idle()
                continue
            if claim is None:
                await self._idle()
                continue
            self.metrics.jobs_claimed += 1
            heartbeat = asyncio.create_task(self._heartbeat(claim))
            try:
                for batch_index in range(self._max_batches_per_claim):
                    candidate = await self._repository.candidate_for_claim(claim)
                    if candidate is None:
                        await self._repository.finish_without_candidate(claim)
                        break
                    summary_task = asyncio.create_task(
                        self._service.summarize_candidate(candidate),
                        name="conversation-rollup-model",
                    )
                    done, _pending = await asyncio.wait(
                        {summary_task, heartbeat},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if heartbeat in done:
                        summary_task.cancel()
                        await asyncio.gather(summary_task, return_exceptions=True)
                        error = heartbeat.exception()
                        if error is not None:
                            raise RollupLeaseLostError(
                                "heartbeat rejected rollup result"
                            ) from error
                        raise RollupLeaseLostError("heartbeat stopped before rollup result")
                    summary, kind = await summary_task
                    committed = await self._repository.commit_candidate(
                        claim,
                        candidate,
                        summary_text=summary,
                        summary_kind=kind,
                        retain_lease=(batch_index + 1 < self._max_batches_per_claim),
                    )
                    self.metrics.coverage_commits += 1
                    if not committed.claim_retained:
                        break
            except asyncio.CancelledError:
                raise
            except RollupLeaseLostError:
                self.metrics.lease_conflicts += 1
            except RollupSourceChangedError:
                self.metrics.source_conflicts += 1
                await self._safe_retry(claim, "source_changed")
            except ConversationCoverageError:
                await self._safe_retry(claim, "coverage_invariant")
            except (OSError, RuntimeError, SQLAlchemyError) as exc:
                await self._safe_retry(claim, type(exc).__name__)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, claim: RollupJobClaim) -> None:
        current = claim
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            current = await self._repository.heartbeat(
                current,
                lease_seconds=self._lease_seconds,
            )

    async def _safe_retry(self, claim: RollupJobClaim, category: str) -> None:
        try:
            await self._repository.retry_infrastructure(
                claim,
                error_category=category,
                retry_max_seconds=self._retry_max_seconds,
            )
            self.metrics.infrastructure_retries += 1
        except (OSError, RuntimeError, SQLAlchemyError):
            # The still-processing row is recovered by lease expiry.
            return

    async def _idle(self) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
        except TimeoutError:
            return
