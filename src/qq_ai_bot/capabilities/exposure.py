"""Capability exposure planning contract (R1 shape, R3 implementation).

Exposure is monotonic within one turn (schemas already shown to the model
cannot be swapped) and is never authority: every tool call re-intersects the
authority ceiling, current permissions, delegation and taint at binding time.
"""

from __future__ import annotations

from typing import Protocol

from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.contracts import CapabilityExposureSnapshot, MemoryCapabilityView


class CapabilityExposurePlanner(Protocol):
    """Plans which capability schemas enter the model request."""

    def plan_initial(
        self,
        *,
        revision: int,
        authority: TurnAuthority,
        scene: TurnSceneFacts,
        memory_view: MemoryCapabilityView,
        schema_token_budget: int,
    ) -> CapabilityExposureSnapshot: ...

    def plan_growth(
        self,
        *,
        current: CapabilityExposureSnapshot,
        requested_capability_ids: tuple[str, ...],
        schema_token_budget: int,
    ) -> CapabilityExposureSnapshot:
        """Add retrieved capabilities; must be a superset of ``current``."""
        ...
