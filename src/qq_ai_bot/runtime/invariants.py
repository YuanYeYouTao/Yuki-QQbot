"""Turn phase machine and its legal transition table.

The machine enforces R1 §6: phases move forward through the admitted →
prepared → model/tool loop → finalizing → delivering pipeline, delivery
results are phases of their own, and every path ends in ``CLOSED`` with
exactly one ``TurnOutcome``.  ``DurableEffectState`` is orthogonal and
monotonic: once a mutation committed, closing the turn can never undo it and
failure outcomes must be reported as ``COMMITTED_BUT_FINALIZATION_FAILED``.

This machine intentionally covers the real 3.5.3 retry shapes: empty-reply
retries, incomplete-response recovery and the native web fallback all loop
inside ``MODEL_ACTIVE`` (self-transition), while tool rounds alternate
``MODEL_ACTIVE`` ↔ ``TOOL_ACTIVE``.
"""

from __future__ import annotations

from enum import StrEnum

from qq_ai_bot.runtime.contracts import (
    TRUSTED_TERMINAL_SOURCES,
    TerminalFinalization,
)
from qq_ai_bot.runtime.errors import (
    DurableEffectViolationError,
    IllegalTurnTransitionError,
    UntrustedFinalizationError,
)
from qq_ai_bot.runtime.result import (
    FAILURE_OUTCOMES,
    DurableEffectState,
    TurnOutcome,
    durable_effect_rank,
)


class TurnPhase(StrEnum):
    CREATED = "created"
    ADMITTED = "admitted"
    PREPARED = "prepared"
    MODEL_ACTIVE = "model_active"
    TOOL_ACTIVE = "tool_active"
    FINALIZING = "finalizing"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    PARTIALLY_DELIVERED = "partially_delivered"
    DELIVERY_FAILED = "delivery_failed"
    CLOSED = "closed"


_LEGAL_TRANSITIONS: dict[TurnPhase, frozenset[TurnPhase]] = {
    TurnPhase.CREATED: frozenset({TurnPhase.ADMITTED}),
    TurnPhase.ADMITTED: frozenset({TurnPhase.PREPARED}),
    TurnPhase.PREPARED: frozenset({TurnPhase.MODEL_ACTIVE}),
    TurnPhase.MODEL_ACTIVE: frozenset(
        {TurnPhase.MODEL_ACTIVE, TurnPhase.TOOL_ACTIVE, TurnPhase.FINALIZING}
    ),
    TurnPhase.TOOL_ACTIVE: frozenset({TurnPhase.MODEL_ACTIVE, TurnPhase.FINALIZING}),
    TurnPhase.FINALIZING: frozenset({TurnPhase.DELIVERING}),
    TurnPhase.DELIVERING: frozenset(
        {TurnPhase.DELIVERED, TurnPhase.PARTIALLY_DELIVERED, TurnPhase.DELIVERY_FAILED}
    ),
    TurnPhase.DELIVERED: frozenset(),
    TurnPhase.PARTIALLY_DELIVERED: frozenset(),
    TurnPhase.DELIVERY_FAILED: frozenset(),
    TurnPhase.CLOSED: frozenset(),
}

_POST_COMMIT_OUTCOMES = frozenset(
    {TurnOutcome.COMPLETED, TurnOutcome.COMMITTED_BUT_FINALIZATION_FAILED}
)


class TurnPhaseMachine:
    """Single authority for one turn's local lifecycle.

    The machine only represents the *local* session lifecycle.  Token,
    version, cross-turn cancellation and the mutation shield stay owned by
    ``ConversationTurnCoordinator``; callers translate coordinator decisions
    into ``close()`` calls here.
    """

    __slots__ = ("_durable", "_outcome", "_phase", "_turn_id")

    def __init__(self, turn_id: str) -> None:
        self._turn_id = turn_id
        self._phase = TurnPhase.CREATED
        self._outcome: TurnOutcome | None = None
        self._durable = DurableEffectState.NONE

    @property
    def turn_id(self) -> str:
        return self._turn_id

    @property
    def phase(self) -> TurnPhase:
        return self._phase

    @property
    def outcome(self) -> TurnOutcome | None:
        return self._outcome

    @property
    def durable_effects(self) -> DurableEffectState:
        return self._durable

    @property
    def closed(self) -> bool:
        return self._phase is TurnPhase.CLOSED

    def advance(self, target: TurnPhase) -> None:
        """Move to ``target`` if the transition table allows it.

        ``CLOSED`` is reached exclusively through :meth:`close` and
        TOOL_ACTIVE → FINALIZING exclusively through :meth:`to_finalizing`,
        which checks terminal-source trust.
        """

        if target is TurnPhase.CLOSED:
            raise IllegalTurnTransitionError(
                self._phase.value, target.value, "use close(outcome) to close a turn"
            )
        if self._phase is TurnPhase.TOOL_ACTIVE and target is TurnPhase.FINALIZING:
            raise IllegalTurnTransitionError(
                self._phase.value,
                target.value,
                "tool batches finalize via to_finalizing(terminal=...) only",
            )
        self._require_open(target)
        if target not in _LEGAL_TRANSITIONS[self._phase]:
            raise IllegalTurnTransitionError(self._phase.value, target.value)
        self._phase = target

    def to_finalizing(self, *, terminal: TerminalFinalization | None = None) -> None:
        """Enter FINALIZING; from TOOL_ACTIVE a trusted terminal is mandatory."""

        self._require_open(TurnPhase.FINALIZING)
        if self._phase is TurnPhase.TOOL_ACTIVE:
            if terminal is None:
                raise UntrustedFinalizationError(
                    "tool batch tried to finalize without host-authorized terminal metadata"
                )
            if terminal.source not in TRUSTED_TERMINAL_SOURCES:
                raise UntrustedFinalizationError(
                    f"terminal finalization from untrusted source: {terminal.source!r}"
                )
        elif TurnPhase.FINALIZING not in _LEGAL_TRANSITIONS[self._phase]:
            raise IllegalTurnTransitionError(self._phase.value, TurnPhase.FINALIZING.value)
        self._phase = TurnPhase.FINALIZING

    def mark_mutation_started(self) -> None:
        self._advance_durable(DurableEffectState.MUTATION_STARTED)

    def mark_mutation_committed(self) -> None:
        if self._durable is DurableEffectState.NONE:
            raise DurableEffectViolationError(
                "mutation cannot commit before being marked as started"
            )
        self._advance_durable(DurableEffectState.MUTATION_COMMITTED)

    def close(self, outcome: TurnOutcome) -> None:
        """Close the turn with a terminal outcome.

        Idempotent for the same outcome; a second close with a different
        outcome is an error.  Once a mutation committed, only
        ``COMPLETED`` / ``COMMITTED_BUT_FINALIZATION_FAILED`` are legal.
        """

        if self._phase is TurnPhase.CLOSED:
            if self._outcome is outcome:
                return
            current = self._outcome.value if self._outcome else "<none>"
            raise IllegalTurnTransitionError(
                TurnPhase.CLOSED.value,
                TurnPhase.CLOSED.value,
                f"already closed as {current}, refusing re-close as {outcome.value}",
            )
        if self._durable is DurableEffectState.MUTATION_COMMITTED:
            if outcome not in _POST_COMMIT_OUTCOMES:
                raise DurableEffectViolationError(
                    "mutation already committed; close with COMPLETED or "
                    f"COMMITTED_BUT_FINALIZATION_FAILED, not {outcome.value}"
                )
        elif outcome is TurnOutcome.COMMITTED_BUT_FINALIZATION_FAILED:
            raise DurableEffectViolationError(
                "COMMITTED_BUT_FINALIZATION_FAILED requires a committed mutation"
            )
        if outcome in FAILURE_OUTCOMES and self._durable is DurableEffectState.MUTATION_COMMITTED:
            raise DurableEffectViolationError(
                f"{outcome.value} cannot represent a turn with committed effects"
            )
        self._outcome = outcome
        self._phase = TurnPhase.CLOSED

    def _advance_durable(self, target: DurableEffectState) -> None:
        if self._phase in (TurnPhase.CREATED, TurnPhase.ADMITTED, TurnPhase.CLOSED):
            raise DurableEffectViolationError(
                f"durable effects cannot change in phase {self._phase.value}"
            )
        if durable_effect_rank(target) < durable_effect_rank(self._durable):
            raise DurableEffectViolationError(
                f"durable effect state cannot regress: {self._durable.value} -> {target.value}"
            )
        self._durable = target

    def _require_open(self, target: TurnPhase) -> None:
        if self._phase is TurnPhase.CLOSED:
            raise IllegalTurnTransitionError(
                TurnPhase.CLOSED.value, target.value, "turn already closed"
            )


def phase_for_delivery_status(status_value: str) -> TurnPhase:
    """Map a ``DeliveryStatus`` value onto the corresponding delivery phase."""

    mapping = {
        "complete": TurnPhase.DELIVERED,
        "partial": TurnPhase.PARTIALLY_DELIVERED,
        "cancelled": TurnPhase.PARTIALLY_DELIVERED,
        "failed": TurnPhase.DELIVERY_FAILED,
    }
    try:
        return mapping[status_value]
    except KeyError as exc:
        raise IllegalTurnTransitionError(
            TurnPhase.DELIVERING.value, f"<{status_value}>", "unknown delivery status"
        ) from exc
