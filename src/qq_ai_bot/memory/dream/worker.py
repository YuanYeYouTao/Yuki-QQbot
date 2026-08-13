"""Exclusive background scheduler and resumable runner for Memory Dream."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.dream.models import (
    DreamClusterPreview,
    DreamClusterStatus,
    DreamHealth,
    DreamRun,
    DreamRunMode,
    DreamRunPage,
)
from qq_ai_bot.memory.dream.repository import DreamRepository
from qq_ai_bot.memory.dream.service import DreamService
from qq_ai_bot.model_runtime.structured import StructuredTaskError

logger = logging.getLogger(__name__)


class DreamWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: DreamRepository,
        service: DreamService,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._service = service
        self._timezone = ZoneInfo(settings.memory_dream_timezone)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._process_lock = asyncio.Lock()
        self._baseline_ready = False

    async def start(self) -> None:
        if not self._settings.memory_dream_enabled or self._task is not None:
            return
        self._stop.clear()
        await self._repository.reset_processing_after_restart()
        try:
            initialized = await self._service.initialize_baseline()
            self._baseline_ready = initialized or await self._repository.baseline_exists()
        except RuntimeError as exc:
            initialized = False
            self._baseline_ready = False
            logger.warning("memory_dream_baseline_deferred error_category=%s", type(exc).__name__)
        self._task = asyncio.create_task(self._run(), name="memory-dream-worker")
        logger.info(
            "memory_dream_started hour=%d timezone=%s baseline_initialized=%s",
            self._settings.memory_dream_schedule_hour,
            self._settings.memory_dream_timezone,
            self._baseline_ready,
        )

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def plan_full(self, *, actor_user_id: str) -> DreamRun:
        if not self._settings.memory_dream_enabled:
            raise RuntimeError("Memory Dream 当前未启用")
        return await self._service.plan_full(actor_user_id=actor_user_id)

    async def start_run(self, public_id: str) -> DreamRun:
        if not await self._repository.start_run(public_id):
            raise RuntimeError("Dream 任务不存在、状态不可启动，或已有独占任务运行")
        self._wake.set()
        run = await self._repository.get_run(public_id)
        assert run is not None
        return run

    async def list_runs(self) -> tuple[DreamRun, ...]:
        return await self._repository.list_runs()

    async def status(self, public_id: str) -> DreamRun | None:
        return await self._repository.get_run(public_id)

    async def show(self, public_id: str, *, page: int = 1) -> DreamRunPage:
        if page <= 0:
            raise ValueError("Dream 页码必须大于零")
        return await self._repository.run_page(public_id, page=page)

    async def preview(self, public_id: str, *, cluster_id: int) -> DreamClusterPreview:
        if self._process_lock.locked():
            raise RuntimeError("Dream 正在运行，暂不能生成预览")
        async with self._process_lock:
            return await self._service.preview_cluster(public_id, cluster_id)

    async def cancel(self, public_id: str) -> bool:
        return await self._repository.cancel(public_id)

    async def resume(self, public_id: str) -> DreamRun:
        return await self.start_run(public_id)

    async def retry(self, public_id: str) -> DreamRun:
        if not await self._repository.retry_failed(public_id):
            raise RuntimeError("Dream 任务没有可重试的失败簇")
        return await self.start_run(public_id)

    async def rollback_operation(self, public_id: str) -> bool:
        if self._process_lock.locked():
            raise RuntimeError("Dream 正在运行，暂不能回滚")
        async with self._process_lock:
            return await self._service.rollback_operation(public_id)

    async def rollback_run(self, public_id: str) -> int:
        if self._process_lock.locked():
            raise RuntimeError("Dream 正在运行，暂不能回滚")
        async with self._process_lock:
            return await self._service.rollback_run(public_id)

    async def health(self) -> DreamHealth:
        return await self._repository.health(enabled=self._settings.memory_dream_enabled)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with self._process_lock:
                    await self._schedule_if_due()
                    await self._drain_active()
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError, IntegrityError) as exc:
                logger.warning("memory_dream_loop_failed error_category=%s", type(exc).__name__)
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=min(60.0, self._settings.memory_dream_poll_seconds),
                )
            except TimeoutError:
                pass

    async def _schedule_if_due(self) -> None:
        if not self._baseline_ready:
            initialized = await self._service.initialize_baseline()
            self._baseline_ready = initialized or await self._repository.baseline_exists()
            if initialized:
                return
        local = datetime.now(UTC).astimezone(self._timezone)
        if local.hour != self._settings.memory_dream_schedule_hour:
            return
        if await self._repository.active_run() is not None:
            return
        slot = f"{local.date().isoformat()}:{local.hour:02d}"
        try:
            await self._service.plan_incremental(scheduled_slot=slot)
        except IntegrityError:
            return

    async def _drain_active(self) -> None:
        await self._repository.reset_processing_after_restart()
        run = await self._repository.active_run()
        if run is None:
            return
        while not self._stop.is_set():
            refreshed = await self._repository.get_run(run.public_id)
            if refreshed is None or refreshed.status.value != "running":
                return
            run = refreshed
            if run.mode is DreamRunMode.INCREMENTAL:
                if run.model_calls >= self._settings.memory_dream_max_model_calls_per_run:
                    await self._repository.fail_pending(
                        run.public_id,
                        error_category="model_call_budget_exhausted",
                    )
                    await self._repository.finalize_run(run.public_id)
                    return
            cluster = await self._repository.claim_next_cluster(run.public_id)
            if cluster is None:
                await self._repository.finalize_run(run.public_id)
                return
            try:
                _calls, operations, valid = await self._service.process_cluster(
                    run,
                    cluster,
                )
                await self._repository.finish_cluster(
                    cluster.id,
                    status=(DreamClusterStatus.COMPLETED if valid else DreamClusterStatus.STALE),
                    operation_count=operations,
                    error_category=(None if valid else "snapshot_changed"),
                )
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError, StructuredTaskError) as exc:
                await self._repository.finish_cluster(
                    cluster.id,
                    status=DreamClusterStatus.FAILED,
                    operation_count=0,
                    error_category=(
                        f"StructuredTaskError:{exc.reason_code}"
                        if isinstance(exc, StructuredTaskError)
                        else type(exc).__name__
                    ),
                )
                logger.warning(
                    "memory_dream_cluster_failed run_id=%s cluster_id=%d error_category=%s",
                    run.public_id,
                    cluster.id,
                    type(exc).__name__,
                )
