"""Turn outcome and durable effect state.

``TurnOutcome`` classifies how a turn ended; ``DurableEffectState`` tracks
whether the turn produced irreversible external effects (memory mutations,
persisted preferences).  They are deliberately separate from ``TurnPhase``:
a committed mutation is not a phase and can never be rolled back by closing
the turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from qq_ai_bot.runtime.delivery import DeliveryOutcome


class TurnOutcome(StrEnum):
    """Terminal classification of one turn."""

    COMPLETED = "completed"
    NO_REPLY = "no_reply"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    MODEL_FAILED = "model_failed"
    TOOL_FAILED = "tool_failed"
    DELIVERY_FAILED = "delivery_failed"
    COMMITTED_BUT_FINALIZATION_FAILED = "committed_but_finalization_failed"


FAILURE_OUTCOMES = frozenset(
    {
        TurnOutcome.CANCELLED,
        TurnOutcome.SUPERSEDED,
        TurnOutcome.MODEL_FAILED,
        TurnOutcome.TOOL_FAILED,
        TurnOutcome.DELIVERY_FAILED,
    }
)
"""Outcomes that are illegal once a durable mutation has committed.

After commit the only legal terminals are ``COMPLETED`` (finalization worked)
and ``COMMITTED_BUT_FINALIZATION_FAILED`` (effect persisted, wrap-up failed).
"""


class DurableEffectState(StrEnum):
    """Monotonic record of irreversible external effects within one turn."""

    NONE = "none"
    MUTATION_STARTED = "mutation_started"
    MUTATION_COMMITTED = "mutation_committed"


_DURABLE_ORDER = {
    DurableEffectState.NONE: 0,
    DurableEffectState.MUTATION_STARTED: 1,
    DurableEffectState.MUTATION_COMMITTED: 2,
}


def durable_effect_rank(state: DurableEffectState) -> int:
    """Total order used to enforce forward-only durable effect transitions."""

    return _DURABLE_ORDER[state]


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Immutable summary of one finished turn (shape frozen by R1 §4.5)."""

    generated_text: str
    model_requests: int
    tool_calls: int
    delivery: DeliveryOutcome
    outcome: TurnOutcome
    durable_effect_state: DurableEffectState
