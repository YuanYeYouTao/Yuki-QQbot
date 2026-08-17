"""Session lifecycle driver shared by all conversation-session implementations.

``ConversationSessionLifecycle`` owns exactly one ``TurnPhaseMachine`` plus
one ``TurnState`` and translates session events (model started, tool batch
finished, delivery finished, coordinator cancellation…) into phase machine
calls.  It deliberately does *not* copy coordinator facts: token, version and
cross-turn cancellation stay in ``ConversationTurnCoordinator``, and callers
translate coordinator decisions into ``supersede()`` / ``cancel()`` here.

R4's production ``ConversationTurnSession`` composes this driver; R1 ships it
with full unit coverage so the lifecycle semantics are frozen early.
"""

from __future__ import annotations

from qq_ai_bot.runtime.contracts import ToolBatchExecutionResult
from qq_ai_bot.runtime.delivery import DeliveryOutcome
from qq_ai_bot.runtime.errors import UntrustedFinalizationError
from qq_ai_bot.runtime.invariants import TurnPhase, TurnPhaseMachine, phase_for_delivery_status
from qq_ai_bot.runtime.result import DurableEffectState, TurnOutcome, TurnResult
from qq_ai_bot.runtime.turn import TurnContext, TurnState


class ConversationSessionLifecycle:
    """Drives one turn through admitted → prepared → agent loop → delivery."""

    __slots__ = ("_context", "_machine", "_state")

    def __init__(self, context: TurnContext) -> None:
        self._context = context
        self._machine = TurnPhaseMachine(context.turn_id)
        self._state = TurnState()

    @property
    def context(self) -> TurnContext:
        return self._context

    @property
    def state(self) -> TurnState:
        return self._state

    @property
    def phase(self) -> TurnPhase:
        return self._machine.phase

    @property
    def outcome(self) -> TurnOutcome | None:
        return self._machine.outcome

    @property
    def durable_effects(self) -> DurableEffectState:
        return self._machine.durable_effects

    @property
    def closed(self) -> bool:
        return self._machine.closed

    # -- forward progress -------------------------------------------------

    def admit(self) -> None:
        self._machine.advance(TurnPhase.ADMITTED)

    def mark_prepared(self) -> None:
        self._machine.advance(TurnPhase.PREPARED)

    def model_started(self) -> None:
        """First model request or a retry (empty reply / incomplete / fallback)."""

        self._machine.advance(TurnPhase.MODEL_ACTIVE)

    def tools_started(self) -> None:
        self._machine.advance(TurnPhase.TOOL_ACTIVE)

    def tools_finished(self, result: ToolBatchExecutionResult) -> None:
        """Return to the model loop, or finalize on a trusted terminal batch."""

        if result.terminal_finalization is not None:
            self._machine.to_finalizing(terminal=result.terminal_finalization)
            return
        self._machine.advance(TurnPhase.MODEL_ACTIVE)

    def finalize_from_model(self) -> None:
        """The model produced its final text without a terminal tool batch."""

        if self._machine.phase is TurnPhase.TOOL_ACTIVE:
            raise UntrustedFinalizationError(
                "tool batches must finalize through tools_finished(result)"
            )
        self._machine.to_finalizing()

    def delivery_started(self) -> None:
        self._machine.advance(TurnPhase.DELIVERING)

    def delivery_finished(self, delivery: DeliveryOutcome) -> None:
        self._machine.advance(phase_for_delivery_status(delivery.status.value))

    # -- durable effects ---------------------------------------------------

    def mutation_started(self) -> None:
        self._machine.mark_mutation_started()

    def mutation_committed(self) -> None:
        self._machine.mark_mutation_committed()
        self._state.taint.mark_mutation_committed()

    # -- terminal ----------------------------------------------------------

    def supersede(self) -> None:
        """A newer coordinator version replaced this turn."""

        self._machine.close(TurnOutcome.SUPERSEDED)

    def cancel(self) -> None:
        """Explicit cancellation (``/ai stop`` or coordinator decision)."""

        self._machine.close(TurnOutcome.CANCELLED)

    def close(self, outcome: TurnOutcome) -> None:
        """Idempotent terminal close; durable-effect rules are enforced."""

        self._machine.close(outcome)

    def build_result(
        self,
        *,
        generated_text: str,
        model_requests: int,
        tool_calls: int,
        delivery: DeliveryOutcome,
        outcome: TurnOutcome,
    ) -> TurnResult:
        return TurnResult(
            generated_text=generated_text,
            model_requests=model_requests,
            tool_calls=tool_calls,
            delivery=delivery,
            outcome=outcome,
            durable_effect_state=self._machine.durable_effects,
        )
