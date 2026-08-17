"""Capability runtime protocol (R1 shape, R3 implementation).

The capability runtime owns the authorized catalog revision for one turn,
initial exposure, retrieval-based exposure growth and the callable set.
Authority is always recomputed at binding time — exposure is discovery, not
permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from qq_ai_bot.capabilities.search_index import CapabilitySearchHit
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.contracts import CapabilityExposureSnapshot, MemoryCapabilityView
from qq_ai_bot.runtime.origin import TurnOrigin


@dataclass(frozen=True, slots=True)
class CapabilityQuery:
    """One retrieval request (from ``request_tools`` or host heuristics)."""

    text: str
    origin: TurnOrigin
    limit: int = 5


class CapabilityRuntime(Protocol):
    """Per-turn capability surface management."""

    def pin_catalog_revision(self) -> int:
        """Pin one authorized catalog revision for the whole turn."""
        ...

    def initial_exposure(
        self,
        *,
        revision: int,
        authority: TurnAuthority,
        scene: TurnSceneFacts,
        memory_view: MemoryCapabilityView,
    ) -> CapabilityExposureSnapshot:
        """Kernel tools plus metadata/lexical retrieval for the first request."""
        ...

    async def search(
        self, query: CapabilityQuery, *, revision: int
    ) -> tuple[CapabilitySearchHit, ...]:
        """Search the catalog; results must already be authority-intersected."""
        ...

    def callable_capability_ids(
        self,
        *,
        revision: int,
        authority: TurnAuthority,
        scene: TurnSceneFacts,
        memory_view: MemoryCapabilityView,
    ) -> frozenset[str]:
        """Capabilities that may execute right now (recomputed per binding)."""
        ...
