"""Per-scope linearization gate for bounded external effects."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from qq_ai_bot.conversation.scope import ConversationTurnSnapshot


class EffectGateTimeoutError(TimeoutError):
    """The scope gate could not be obtained within its bounded wait."""


class EffectPermitRejectedError(RuntimeError):
    """The generation or coordinator fence rejected an effect."""


@dataclass(frozen=True, slots=True)
class EffectPermit:
    scope_id: int
    generation: int
    coordinator_version: int
    effect_id: str


FenceValidator = Callable[[ConversationTurnSnapshot], Awaitable[bool]]


class ConversationEffectGate:
    """Own one asyncio lock per bot-aware conversation scope."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self.superseded_rejections = 0

    async def _lock_for(self, scope_key: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(scope_key, asyncio.Lock())

    @asynccontextmanager
    async def hold(self, scope_key: str, *, timeout_seconds: float) -> AsyncIterator[None]:
        """Hold a scope gate for reset/privacy orchestration."""

        lock = await self._lock_for(scope_key)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise EffectGateTimeoutError(f"effect gate timed out for {scope_key}") from exc
        try:
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def permit(
        self,
        snapshot: ConversationTurnSnapshot,
        *,
        validate: FenceValidator,
        timeout_seconds: float,
    ) -> AsyncIterator[EffectPermit]:
        """Validate inside the gate and issue a one-use linearization permit."""

        async with self.hold(snapshot.scope_key, timeout_seconds=timeout_seconds):
            if not await validate(snapshot):
                self.superseded_rejections += 1
                raise EffectPermitRejectedError("turn generation was superseded")
            yield EffectPermit(
                scope_id=snapshot.scope_id,
                generation=snapshot.generation,
                coordinator_version=snapshot.coordinator_version,
                effect_id=uuid.uuid4().hex,
            )
