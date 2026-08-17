"""Conversation-side per-turn value objects (R1).

Only small pure data lives here; the mutable per-turn working set is
``qq_ai_bot.runtime.turn.TurnState`` and the phase machine lives in
``qq_ai_bot.runtime.invariants``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TurnEffectContext:
    """Which reply effects (emoji, speech, …) this turn may enqueue.

    Derived from trusted runtime config plus scene facts when the turn is
    prepared; the model can request effects but never widen this set.
    """

    allowed_effect_kinds: frozenset[str] = frozenset()
    max_effects_per_reply: int = 1

    def allows(self, kind: str) -> bool:
        return kind in self.allowed_effect_kinds
