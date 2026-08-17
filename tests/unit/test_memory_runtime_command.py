"""Unit tests for the mutation command plane (R2 commit 4)."""

from __future__ import annotations

from typing import cast

import pytest

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryKind, MemoryRecallPurpose, MemoryStatus
from qq_ai_bot.memory.mutation.models import (
    MemoryMutationAppliedOperation,
    MemoryMutationCandidate,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationOutcome,
    MemoryMutationRequest,
    MemoryMutationResult,
)
from qq_ai_bot.memory.runtime.command_plane import (
    MemoryCommandPlane,
    mutation_state_for_result,
)
from qq_ai_bot.memory.runtime.contract import (
    exclusive_write_contract,
    forbidden_contract,
    passive_contract,
)
from qq_ai_bot.memory.runtime.errors import MemoryRuntimeError
from qq_ai_bot.memory.runtime.state import (
    AccessPhase,
    LocatorStatus,
    MemorySessionState,
    MutationState,
)
from qq_ai_bot.runtime.keys import ResolvedMemoryScope


def _scope() -> ResolvedMemoryScope:
    return ResolvedMemoryScope(scope_type=ScopeType.PRIVATE, scope_id="u-1")


def _session(**kwargs: object) -> MemorySessionState:
    contract = kwargs.get("contract") or exclusive_write_contract()
    return MemorySessionState(contract, _scope())  # type: ignore[arg-type]


def _request() -> MemoryMutationRequest:
    return MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        new_content="喜欢拿铁",
        category="pref",
        kind=MemoryKind.PREFERENCE,
    )


def _context() -> MemoryMutationContext:
    return cast(MemoryMutationContext, object())


def _result(
    *,
    ok: bool = True,
    applied: MemoryMutationAppliedOperation = MemoryMutationAppliedOperation.CREATE,
    outcome: MemoryMutationOutcome = MemoryMutationOutcome.COMMITTED,
    reason_code: str = "",
    mutation_id: str | None = "m-1",
    candidates: tuple[MemoryMutationCandidate, ...] = (),
) -> MemoryMutationResult:
    return MemoryMutationResult(
        ok=ok,
        mutation_id=mutation_id,
        requested_operation=MemoryMutationOperation.CREATE,
        applied_operation=applied,
        outcome=outcome,
        reason_code=reason_code,
        candidates=candidates,
    )


def _ambiguous() -> MemoryMutationResult:
    return _result(
        ok=False,
        applied=MemoryMutationAppliedOperation.NOOP,
        outcome=MemoryMutationOutcome.REJECTED,
        reason_code="memory_candidate_ambiguous",
        mutation_id=None,
        candidates=(
            MemoryMutationCandidate(
                fact_id=3,
                memory_ref="M3",
                memory_key="drink",
                category="pref",
                kind=MemoryKind.PREFERENCE,
                content="拿铁",
                status=MemoryStatus.ACTIVE,
            ),
        ),
    )


class _FakeMutations:
    def __init__(self, results: list[MemoryMutationResult]) -> None:
        self._results = list(results)
        self.calls = 0

    async def mutate(self, request: object, context: object) -> MemoryMutationResult:
        del request, context
        self.calls += 1
        return self._results.pop(0)

    async def mutate_resolved(
        self,
        request: object,
        context: object,
        *,
        target: object,
    ) -> MemoryMutationResult:
        del target
        return await self.mutate(request, context)


class TestMutationStateMapping:
    def test_locator_reasons_win_over_rejected_noop(self) -> None:
        assert mutation_state_for_result(_ambiguous()) is MutationState.AMBIGUOUS
        assert (
            mutation_state_for_result(
                _result(
                    ok=False,
                    applied=MemoryMutationAppliedOperation.NOOP,
                    outcome=MemoryMutationOutcome.REJECTED,
                    reason_code="memory_candidate_not_found",
                    mutation_id=None,
                )
            )
            is MutationState.NOT_FOUND
        )

    def test_committed_and_contested(self) -> None:
        assert mutation_state_for_result(_result()) is MutationState.COMMITTED
        assert (
            mutation_state_for_result(
                _result(
                    applied=MemoryMutationAppliedOperation.CONTEST,
                    outcome=MemoryMutationOutcome.COMMITTED_AS_CONTESTED,
                )
            )
            is MutationState.COMMITTED_AS_CONTESTED
        )


class TestCommandPlane:
    @pytest.mark.asyncio
    async def test_committed_write_is_terminal(self) -> None:
        state = _session()
        plane = MemoryCommandPlane(_FakeMutations([_result()]))
        result = await plane.command(state, _request(), _context())
        assert result.ok
        assert state.mutation_state is MutationState.COMMITTED
        assert state.last_mutation_receipt_id == "m-1"
        assert plane.finalize_text(state) == "已将这条信息写入长期记忆。"

    @pytest.mark.asyncio
    async def test_requestable_passive_enters_exclusive_write(self) -> None:
        state = _session(contract=passive_contract())
        plane = MemoryCommandPlane(_FakeMutations([_result()]))
        await plane.command(state, _request(), _context())
        assert state.access_phase is AccessPhase.MUTATION_EXCLUSIVE
        assert state.mutation_state is MutationState.COMMITTED

    @pytest.mark.asyncio
    async def test_denied_write_is_rejected_before_service(self) -> None:
        state = _session(contract=forbidden_contract(MemoryRecallPurpose.BACKGROUND))
        plane = MemoryCommandPlane(_FakeMutations([_result()]))
        with pytest.raises(MemoryRuntimeError, match="denied"):
            await plane.command(state, _request(), _context())
        assert state.mutation_state is MutationState.NOT_ATTEMPTED

    @pytest.mark.asyncio
    async def test_ambiguous_opens_locator_then_retry_commits(self) -> None:
        state = _session()
        plane = MemoryCommandPlane(_FakeMutations([_ambiguous(), _result(mutation_id="m-2")]))
        first = await plane.command(state, _request(), _context())
        assert first.reason_code == "memory_candidate_ambiguous"
        assert state.access_phase is AccessPhase.LOCATOR_READ_ENABLED
        assert state.locator_status is LocatorStatus.OPEN
        assert "M3｜drink" in plane.finalize_text(state)
        with pytest.raises(MemoryRuntimeError, match="locator read must complete"):
            await plane.command(state, _request(), _context())
        plane.complete_locator_read(state)
        second = await plane.command(state, _request(), _context())
        assert second.mutation_id == "m-2"
        assert state.mutation_state is MutationState.COMMITTED
        assert state.access_phase is AccessPhase.MUTATION_EXCLUSIVE

    @pytest.mark.asyncio
    async def test_second_locator_outcome_closes_deterministically(self) -> None:
        state = _session()
        plane = MemoryCommandPlane(_FakeMutations([_ambiguous(), _ambiguous()]))
        await plane.command(state, _request(), _context())
        plane.complete_locator_read(state)
        await plane.command(state, _request(), _context())
        assert state.mutation_state is MutationState.REJECTED
        assert "不能唯一定位目标" in plane.finalize_text(state)
