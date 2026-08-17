"""Turn-session adapter over the durable mutation service (R2 §7).

``MemoryMutationService`` remains the only persistence boundary.  This plane
only advances session machines and never opens its own transaction.
Dream/Worker/Admin/Plugin keep calling the service directly; they are not
turn sessions.
"""

from __future__ import annotations

from typing import Protocol

from qq_ai_bot.memory.mutation.models import (
    MemoryMutationAppliedOperation,
    MemoryMutationContext,
    MemoryMutationOutcome,
    MemoryMutationRequest,
    MemoryMutationResult,
)
from qq_ai_bot.memory.runtime.contract import MemoryWritePolicy, MemoryWriteTransition
from qq_ai_bot.memory.runtime.errors import MemoryRuntimeError
from qq_ai_bot.memory.runtime.finalizer import (
    MutationFinalizationInput,
    finalize_mutation_text,
    mutation_view_from_result,
)
from qq_ai_bot.memory.runtime.state import (
    AccessPhase,
    LocatorStatus,
    MemorySessionState,
    MutationState,
)
from qq_ai_bot.memory.subjects import ResolvedSubject


class MemoryMutationGateway(Protocol):
    """The durable write port the command plane may call."""

    async def mutate(
        self,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
    ) -> MemoryMutationResult: ...

    async def mutate_resolved(
        self,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
        *,
        target: ResolvedSubject,
    ) -> MemoryMutationResult: ...


_LOCATOR_STATES = frozenset({MutationState.AMBIGUOUS, MutationState.NOT_FOUND})


def mutation_state_for_result(result: MemoryMutationResult) -> MutationState:
    """Map a durable result onto the session mutation ledger."""

    if result.reason_code == "memory_candidate_ambiguous":
        return MutationState.AMBIGUOUS
    if result.reason_code == "memory_candidate_not_found":
        return MutationState.NOT_FOUND
    if result.outcome is MemoryMutationOutcome.REJECTED or not result.ok:
        return MutationState.REJECTED
    if (
        result.applied_operation is MemoryMutationAppliedOperation.NOOP
        or result.outcome is MemoryMutationOutcome.NO_CHANGE
    ):
        return MutationState.NO_CHANGE
    if (
        result.outcome is MemoryMutationOutcome.DEDUPLICATED
        or result.applied_operation is MemoryMutationAppliedOperation.MERGE_EVIDENCE
    ):
        return MutationState.DEDUPLICATED
    if (
        result.outcome is MemoryMutationOutcome.COMMITTED_AS_CONTESTED
        or result.applied_operation is MemoryMutationAppliedOperation.CONTEST
    ):
        return MutationState.COMMITTED_AS_CONTESTED
    return MutationState.COMMITTED


class MemoryCommandPlane:
    """Session-facing mutation entry.  Does not re-wrap the service transaction."""

    def __init__(self, mutations: MemoryMutationGateway) -> None:
        self._mutations = mutations

    async def command(
        self,
        state: MemorySessionState,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
        *,
        resolved_target: ResolvedSubject | None = None,
    ) -> MemoryMutationResult:
        """Attempt one durable write and record the ledger outcome."""

        self._prepare_exclusive_write(state)
        state.mark_mutation_attempted()
        if resolved_target is None:
            result = await self._mutations.mutate(request, context)
        else:
            result = await self._mutations.mutate_resolved(request, context, target=resolved_target)
        view = mutation_view_from_result(result)
        state.remember_mutation_view(view)
        outcome = mutation_state_for_result(result)
        if outcome in _LOCATOR_STATES and state.locator_status is not LocatorStatus.UNUSED:
            state.resolve_mutation(MutationState.REJECTED, receipt_id=result.mutation_id)
            return result
        state.resolve_mutation(outcome, receipt_id=result.mutation_id)
        if outcome in _LOCATOR_STATES:
            state.open_locator_read()
        return result

    def complete_locator_read(self, state: MemorySessionState) -> None:
        """Consume the one allowed locator-read window."""

        state.complete_locator_read()

    def finalize_text(self, state: MemorySessionState) -> str:
        """Receipt-gated reply.  Never asks a model."""

        view = state.last_mutation_view
        if view is None:
            return finalize_mutation_text(MutationFinalizationInput(attempted=False))
        return finalize_mutation_text(view)

    def _prepare_exclusive_write(self, state: MemorySessionState) -> None:
        if state.access_phase is AccessPhase.LOCATOR_READ_ENABLED:
            raise MemoryRuntimeError("locator read must complete before a mutation retry")
        if state.access_phase is AccessPhase.LOCATOR_READ_DONE:
            state.return_to_exclusive_write()
            return
        if state.contract.write_policy is MemoryWritePolicy.EXCLUSIVE:
            return
        if state.contract.write_transition is MemoryWriteTransition.REQUESTABLE:
            state.enter_exclusive_write()
            return
        raise MemoryRuntimeError("persistent memory writes are denied for this turn")
