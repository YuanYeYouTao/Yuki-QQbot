"""Memory turn session protocol (R1 shape, R2 implementation).

One session exists per turn.  It owns the recall ledger, exposure registry
and mutation transition state; it never holds the chat service, and the
asynchronous attribution worker never holds a live session — delivery
confirmation freezes an immutable job and the session can close.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from qq_ai_bot.memory.runtime.contract import MemoryTurnContract
from qq_ai_bot.runtime.contracts import DeliverySummary, MemoryCapabilityView, MemoryReceiptHandle
from qq_ai_bot.runtime.keys import ResolvedMemoryScope


@dataclass(frozen=True, slots=True)
class MemoryPrefetchCandidate:
    """Result of a passive prefetch that has *not* been exposed yet.

    Exposure (and the receipt) is confirmed only when the candidate is
    actually included in a real model request, via
    :meth:`MemoryTurnSession.confirm_prompt_exposure`.
    """

    token: str
    fact_ids: tuple[int, ...]
    token_estimate: int = 0


class MemoryTurnSession(Protocol):
    """Per-turn memory session driven by the conversation runtime."""

    @property
    def contract(self) -> MemoryTurnContract: ...

    @property
    def scope(self) -> ResolvedMemoryScope: ...

    async def prefetch(self) -> MemoryPrefetchCandidate | None:
        """Run the passive prefetch when the contract allows it."""
        ...

    def capability_view(self) -> MemoryCapabilityView:
        """Current pure-data view consumed by the capability runtime."""
        ...

    async def confirm_prompt_exposure(self, token: str) -> MemoryReceiptHandle | None:
        """Confirm a prefetch candidate actually entered the model prompt."""
        ...

    async def observe_tool_result(self, capability_id: str, result_json: str) -> None:
        """Record a memory tool read/write result into the session ledger."""
        ...

    async def on_delivery_confirmed(self, summary: DeliverySummary) -> None:
        """Freeze the immutable attribution job after confirmed delivery."""
        ...

    async def close(self) -> None:
        """Release the session; idempotent."""
        ...
