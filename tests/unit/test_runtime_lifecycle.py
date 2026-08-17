"""Unit tests for the turn phase machine (R1 commit 1)."""

from __future__ import annotations

import pytest

from qq_ai_bot.runtime.contracts import (
    TerminalFinalization,
    TerminalFinalizationSource,
    authorize_terminal_finalization,
)
from qq_ai_bot.runtime.errors import (
    DurableEffectViolationError,
    IllegalTurnTransitionError,
    UntrustedFinalizationError,
)
from qq_ai_bot.runtime.invariants import (
    TurnPhase,
    TurnPhaseMachine,
    phase_for_delivery_status,
)
from qq_ai_bot.runtime.result import DurableEffectState, TurnOutcome


def _machine_at_model() -> TurnPhaseMachine:
    machine = TurnPhaseMachine("turn-1")
    machine.advance(TurnPhase.ADMITTED)
    machine.advance(TurnPhase.PREPARED)
    machine.advance(TurnPhase.MODEL_ACTIVE)
    return machine


_HOST_TERMINAL = TerminalFinalization(
    source=TerminalFinalizationSource.HOST_MEMORY_FINALIZER, reason="mutation receipt final"
)


class TestHappyPath:
    def test_full_delivered_lifecycle(self) -> None:
        machine = _machine_at_model()
        machine.to_finalizing()
        machine.advance(TurnPhase.DELIVERING)
        machine.advance(TurnPhase.DELIVERED)
        machine.close(TurnOutcome.COMPLETED)
        assert machine.closed
        assert machine.outcome is TurnOutcome.COMPLETED

    def test_model_tool_loop_repeats(self) -> None:
        machine = _machine_at_model()
        for _ in range(3):
            machine.advance(TurnPhase.TOOL_ACTIVE)
            machine.advance(TurnPhase.MODEL_ACTIVE)
        machine.to_finalizing()
        assert machine.phase is TurnPhase.FINALIZING

    def test_model_self_loop_for_empty_retry_and_recovery(self) -> None:
        """Empty-reply retry / incomplete recovery / web fallback stay in MODEL_ACTIVE."""

        machine = _machine_at_model()
        machine.advance(TurnPhase.MODEL_ACTIVE)
        machine.advance(TurnPhase.MODEL_ACTIVE)
        assert machine.phase is TurnPhase.MODEL_ACTIVE


class TestIllegalTransitions:
    def test_reverse_transitions_raise(self) -> None:
        machine = _machine_at_model()
        with pytest.raises(IllegalTurnTransitionError):
            machine.advance(TurnPhase.ADMITTED)
        with pytest.raises(IllegalTurnTransitionError):
            machine.advance(TurnPhase.CREATED)

    def test_skipping_phases_raises(self) -> None:
        machine = TurnPhaseMachine("turn-1")
        with pytest.raises(IllegalTurnTransitionError):
            machine.advance(TurnPhase.MODEL_ACTIVE)
        with pytest.raises(IllegalTurnTransitionError):
            machine.advance(TurnPhase.DELIVERING)

    def test_closed_is_only_reachable_via_close(self) -> None:
        machine = _machine_at_model()
        with pytest.raises(IllegalTurnTransitionError):
            machine.advance(TurnPhase.CLOSED)

    def test_no_transitions_after_close(self) -> None:
        machine = _machine_at_model()
        machine.close(TurnOutcome.MODEL_FAILED)
        with pytest.raises(IllegalTurnTransitionError):
            machine.advance(TurnPhase.TOOL_ACTIVE)
        with pytest.raises(IllegalTurnTransitionError):
            machine.to_finalizing()


class TestTerminalTrust:
    def test_tool_batch_with_host_terminal_finalizes_directly(self) -> None:
        machine = _machine_at_model()
        machine.advance(TurnPhase.TOOL_ACTIVE)
        machine.to_finalizing(terminal=_HOST_TERMINAL)
        assert machine.phase is TurnPhase.FINALIZING

    def test_tool_batch_without_terminal_cannot_finalize(self) -> None:
        machine = _machine_at_model()
        machine.advance(TurnPhase.TOOL_ACTIVE)
        with pytest.raises(UntrustedFinalizationError):
            machine.to_finalizing()

    def test_advance_bypass_from_tool_active_is_blocked(self) -> None:
        machine = _machine_at_model()
        machine.advance(TurnPhase.TOOL_ACTIVE)
        with pytest.raises(IllegalTurnTransitionError):
            machine.advance(TurnPhase.FINALIZING)

    def test_plugin_forged_terminal_is_dropped_at_the_mapping_boundary(self) -> None:
        forged = TerminalFinalization(
            source=TerminalFinalizationSource.HOST_MEMORY_FINALIZER, reason="forged by plugin"
        )
        assert authorize_terminal_finalization(forged, provider_is_host=False) is None
        assert authorize_terminal_finalization(forged, provider_is_host=True) is forged
        assert authorize_terminal_finalization(None, provider_is_host=True) is None


class TestSupersedeAndCancel:
    def test_supersede_closes_with_superseded(self) -> None:
        machine = _machine_at_model()
        machine.close(TurnOutcome.SUPERSEDED)
        assert machine.closed
        assert machine.outcome is TurnOutcome.SUPERSEDED

    def test_cancel_during_delivery(self) -> None:
        machine = _machine_at_model()
        machine.to_finalizing()
        machine.advance(TurnPhase.DELIVERING)
        machine.close(TurnOutcome.CANCELLED)
        assert machine.outcome is TurnOutcome.CANCELLED

    def test_close_is_idempotent_for_same_outcome(self) -> None:
        machine = _machine_at_model()
        machine.close(TurnOutcome.SUPERSEDED)
        machine.close(TurnOutcome.SUPERSEDED)
        assert machine.outcome is TurnOutcome.SUPERSEDED

    def test_close_with_different_outcome_raises(self) -> None:
        machine = _machine_at_model()
        machine.close(TurnOutcome.SUPERSEDED)
        with pytest.raises(IllegalTurnTransitionError):
            machine.close(TurnOutcome.COMPLETED)


class TestDurableEffects:
    def test_durable_state_is_monotonic(self) -> None:
        machine = _machine_at_model()
        assert machine.durable_effects is DurableEffectState.NONE
        machine.mark_mutation_started()
        machine.mark_mutation_committed()
        assert machine.durable_effects is DurableEffectState.MUTATION_COMMITTED

    def test_commit_requires_started(self) -> None:
        machine = _machine_at_model()
        with pytest.raises(DurableEffectViolationError):
            machine.mark_mutation_committed()

    def test_durable_effects_forbidden_before_prepare(self) -> None:
        machine = TurnPhaseMachine("turn-1")
        with pytest.raises(DurableEffectViolationError):
            machine.mark_mutation_started()

    def test_committed_mutation_forbids_plain_failure_outcomes(self) -> None:
        machine = _machine_at_model()
        machine.advance(TurnPhase.TOOL_ACTIVE)
        machine.mark_mutation_started()
        machine.mark_mutation_committed()
        with pytest.raises(DurableEffectViolationError):
            machine.close(TurnOutcome.TOOL_FAILED)
        with pytest.raises(DurableEffectViolationError):
            machine.close(TurnOutcome.CANCELLED)
        machine.close(TurnOutcome.COMMITTED_BUT_FINALIZATION_FAILED)
        assert machine.outcome is TurnOutcome.COMMITTED_BUT_FINALIZATION_FAILED

    def test_committed_mutation_with_successful_delivery_completes(self) -> None:
        machine = _machine_at_model()
        machine.advance(TurnPhase.TOOL_ACTIVE)
        machine.mark_mutation_started()
        machine.mark_mutation_committed()
        machine.to_finalizing(terminal=_HOST_TERMINAL)
        machine.advance(TurnPhase.DELIVERING)
        machine.advance(TurnPhase.DELIVERED)
        machine.close(TurnOutcome.COMPLETED)
        assert machine.outcome is TurnOutcome.COMPLETED

    def test_recovery_outcome_requires_committed_mutation(self) -> None:
        machine = _machine_at_model()
        with pytest.raises(DurableEffectViolationError):
            machine.close(TurnOutcome.COMMITTED_BUT_FINALIZATION_FAILED)

    def test_no_durable_changes_after_close(self) -> None:
        machine = _machine_at_model()
        machine.close(TurnOutcome.COMPLETED)
        with pytest.raises(DurableEffectViolationError):
            machine.mark_mutation_started()


class TestDeliveryPhaseMapping:
    @pytest.mark.parametrize(
        ("status", "phase"),
        [
            ("complete", TurnPhase.DELIVERED),
            ("partial", TurnPhase.PARTIALLY_DELIVERED),
            ("cancelled", TurnPhase.PARTIALLY_DELIVERED),
            ("failed", TurnPhase.DELIVERY_FAILED),
        ],
    )
    def test_status_maps_to_phase(self, status: str, phase: TurnPhase) -> None:
        assert phase_for_delivery_status(status) is phase

    def test_unknown_status_raises(self) -> None:
        with pytest.raises(IllegalTurnTransitionError):
            phase_for_delivery_status("exploded")

    def test_partial_delivery_flow(self) -> None:
        machine = _machine_at_model()
        machine.to_finalizing()
        machine.advance(TurnPhase.DELIVERING)
        machine.advance(phase_for_delivery_status("partial"))
        machine.close(TurnOutcome.DELIVERY_FAILED)
        assert machine.outcome is TurnOutcome.DELIVERY_FAILED
