"""Local-only Memory V2 expiration and stale-fact maintenance worker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.memory.lifecycle import MemoryLifecycleConfig, MemoryLifecyclePolicy
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.mutation.models import (
    MemoryMutationAppliedOperation,
    MemoryMutationOperation,
)
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.receipt import MemoryRecallRepository
from qq_ai_bot.memory.service import MemoryFactService


@dataclass(frozen=True, slots=True)
class _MaintenanceRuntime:
    enabled: bool
    interval_seconds: float
    batch_limit: int
    automatic_stale_days: int
    third_party_stale_days: int
    contested_stale_days: int
    stale_max_importance: int
    stale_max_confidence: float


class MemoryMaintenanceWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        facts: MemoryFactService,
        runtime_config: RuntimeConfigService | None = None,
        policy: MemoryLifecyclePolicy | None = None,
        metrics: MemoryLifecycleMetrics | None = None,
        mutations: MemoryMutationService | None = None,
        receipts: MemoryRecallRepository | None = None,
    ) -> None:
        self._settings = settings
        self._facts = facts
        self._runtime_config = runtime_config
        self._policy = policy or MemoryLifecyclePolicy()
        self.metrics = metrics or MemoryLifecycleMetrics()
        self._mutations = mutations
        self._receipts = receipts
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="memory-maintenance-worker")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            runtime = await self._snapshot()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=runtime.interval_seconds,
                )
            except TimeoutError:
                pass
            self._wake.clear()
            if not self._stop.is_set() and runtime.enabled:
                await self.process_once()

    async def process_once(self) -> int:
        runtime = await self._snapshot()
        if not runtime.enabled:
            return 0
        now = datetime.now(UTC)
        if self._receipts is not None:
            cleaned = await self._receipts.cleanup_expired(
                now=now,
                limit=runtime.batch_limit,
            )
            if cleaned:
                self.metrics.increment("memory_recall_receipts_cleaned_count", cleaned)
        config = MemoryLifecycleConfig(
            automatic_stale_days=runtime.automatic_stale_days,
            third_party_stale_days=runtime.third_party_stale_days,
            contested_stale_days=runtime.contested_stale_days,
            stale_max_importance=runtime.stale_max_importance,
            stale_max_confidence=runtime.stale_max_confidence,
        )
        rows = await self._facts.repository.list_lifecycle_candidates(
            now=now,
            automatic_cutoff=now - timedelta(days=config.automatic_stale_days),
            third_party_cutoff=now - timedelta(days=config.third_party_stale_days),
            contested_cutoff=now - timedelta(days=config.contested_stale_days),
            max_importance=config.stale_max_importance,
            max_confidence=config.stale_max_confidence,
            limit=runtime.batch_limit,
        )
        changed = 0
        if self._mutations is not None:
            for candidate in rows:
                fact = await self._facts.repository.get_fact(candidate.id)
                if fact is None:
                    continue
                reason = self._policy.reason(fact, now=now, config=config)
                if reason is None:
                    continue
                result = await self._mutations.mutate_reflection(
                    fact,
                    operation=MemoryMutationOperation.INVALIDATE,
                    reason=reason,
                )
                if result.ok and result.applied_operation is (
                    MemoryMutationAppliedOperation.INVALIDATE
                ):
                    changed += 1
                    self.metrics.increment(
                        "maintenance_expired"
                        if reason.value == "expired"
                        else "maintenance_stale_invalidated"
                    )
            self.metrics.record_maintenance_success(now)
            return changed
        async with self._facts.repository.transaction() as session:
            for candidate in rows:
                # Candidate loading is intentionally bounded and read-only. Re-read each
                # fact inside the write transaction so a concurrent confirmation or
                # correction cannot be invalidated from a stale snapshot.
                fact = await self._facts.repository.get_fact(candidate.id, session=session)
                if fact is None:
                    continue
                reason = self._policy.reason(fact, now=now, config=config)
                if reason is None:
                    continue
                if await self._facts.invalidate_fact(
                    fact.id,
                    reason=reason,
                    actor_user_id=None,
                    session=session,
                ):
                    changed += 1
                    self.metrics.increment(
                        "maintenance_expired"
                        if reason.value == "expired"
                        else "maintenance_stale_invalidated"
                    )
        self.metrics.record_maintenance_success(now)
        return changed

    async def _snapshot(self) -> _MaintenanceRuntime:
        if self._runtime_config is not None:
            runtime = (await self._runtime_config.snapshot()).memory
            return _MaintenanceRuntime(
                enabled=runtime.maintenance_enabled,
                interval_seconds=runtime.maintenance_interval_seconds,
                batch_limit=runtime.maintenance_batch_limit,
                automatic_stale_days=runtime.automatic_stale_days,
                third_party_stale_days=runtime.third_party_stale_days,
                contested_stale_days=runtime.contested_stale_days,
                stale_max_importance=runtime.stale_max_importance,
                stale_max_confidence=runtime.stale_max_confidence,
            )
        return _MaintenanceRuntime(
            enabled=self._settings.memory_maintenance_enabled,
            interval_seconds=self._settings.memory_maintenance_interval_seconds,
            batch_limit=self._settings.memory_maintenance_batch_limit,
            automatic_stale_days=self._settings.memory_automatic_stale_days,
            third_party_stale_days=self._settings.memory_third_party_stale_days,
            contested_stale_days=self._settings.memory_contested_stale_days,
            stale_max_importance=self._settings.memory_stale_max_importance,
            stale_max_confidence=self._settings.memory_stale_max_confidence,
        )
