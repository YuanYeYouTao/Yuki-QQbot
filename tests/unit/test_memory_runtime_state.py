"""Unit tests for memory-session machines and ledgers (R2 commit 1)."""

from __future__ import annotations

import pytest

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryRecallPurpose
from qq_ai_bot.memory.runtime.contract import (
    exclusive_write_contract,
    forbidden_contract,
    passive_contract,
)
from qq_ai_bot.memory.runtime.errors import (
    IllegalMemoryTransitionError,
    MemoryLocatorRetryExhaustedError,
    MemorySessionClosedError,
)
from qq_ai_bot.memory.runtime.state import (
    AccessPhase,
    AttributionHandoff,
    LocatorStatus,
    MemorySessionState,
    MutationState,
    RecallHandle,
    initial_access_phase,
)
from qq_ai_bot.runtime.keys import ResolvedMemoryScope


def _scope() -> ResolvedMemoryScope:
    return ResolvedMemoryScope(scope_type=ScopeType.PRIVATE, scope_id="u-1")


def _session(**kwargs: object) -> MemorySessionState:
    contract = kwargs.pop("contract", None)
    if contract is None:
        contract = passive_contract()
    return MemorySessionState(contract, _scope())  # type: ignore[arg-type]


def _handle(*, receipt: str = "r-1") -> RecallHandle:
    return RecallHandle(
        runtime_turn_id="turn-1",
        receipt_turn_id=receipt,
        purpose=MemoryRecallPurpose.BACKGROUND,
        injected_fact_ids=(11,),
    )


class TestInitialPhase:
    def test_passive_starts_dormant(self) -> None:
        assert initial_access_phase(passive_contract()) is AccessPhase.DORMANT

    def test_exclusive_starts_mutation_lane(self) -> None:
        assert initial_access_phase(exclusive_write_contract()) is AccessPhase.MUTATION_EXCLUSIVE

    def test_forbidden_starts_dormant_and_blocks_work(self) -> None:
        session = _session(contract=forbidden_contract(MemoryRecallPurpose.BACKGROUND))
        assert session.access_phase is AccessPhase.DORMANT
        with pytest.raises(IllegalMemoryTransitionError, match="FORBIDDEN"):
            session.start_prefetch()
        with pytest.raises(IllegalMemoryTransitionError, match="FORBIDDEN"):
            session.enter_exclusive_write()


class TestPrefetchAndRead:
    def test_passive_prefetch_then_request_read(self) -> None:
        session = _session()
        session.start_prefetch()
        assert session.access_phase is AccessPhase.PREFETCHING
        session.complete_prefetch()
        session.enable_read()
        assert session.access_phase is AccessPhase.READ_ENABLED

    def test_none_context_cannot_prefetch(self) -> None:
        session = _session(contract=exclusive_write_contract())
        with pytest.raises(IllegalMemoryTransitionError, match="forbids prefetch"):
            session.start_prefetch()


class TestExclusiveWriteAndLocator:
    def test_request_write_from_passive(self) -> None:
        session = _session()
        session.start_prefetch()
        session.complete_prefetch()
        session.enter_exclusive_write()
        assert session.access_phase is AccessPhase.MUTATION_EXCLUSIVE
        assert session.contract.write_policy.value == "exclusive"
        assert session.transition_revision == 2

    def test_active_read_cannot_write_without_transition(self) -> None:
        session = _session()
        session.enable_read()
        with pytest.raises(IllegalMemoryTransitionError, match="MUTATION_EXCLUSIVE"):
            session.mark_mutation_attempted()

    def test_locator_cycle_allows_one_retry(self) -> None:
        session = _session(contract=exclusive_write_contract())
        session.mark_mutation_attempted(receipt_id="m-1")
        session.resolve_mutation(MutationState.AMBIGUOUS, receipt_id="m-1")
        session.open_locator_read()
        assert session.access_phase is AccessPhase.LOCATOR_READ_ENABLED
        assert session.locator_status is LocatorStatus.OPEN
        assert session.contract.read_policy.value == "locator_only"
        session.complete_locator_read()
        session.return_to_exclusive_write()
        assert session.access_phase is AccessPhase.MUTATION_EXCLUSIVE
        assert session.contract.read_policy.value == "denied"
        session.mark_mutation_attempted(receipt_id="m-2")
        session.resolve_mutation(MutationState.COMMITTED, receipt_id="m-2")
        assert session.mutation_state is MutationState.COMMITTED
        assert session.last_mutation_receipt_id == "m-2"

    def test_second_locator_outcome_is_exhausted(self) -> None:
        session = _session(contract=exclusive_write_contract())
        session.mark_mutation_attempted()
        session.resolve_mutation(MutationState.NOT_FOUND)
        session.open_locator_read()
        session.complete_locator_read()
        session.return_to_exclusive_write()
        session.mark_mutation_attempted()
        with pytest.raises(MemoryLocatorRetryExhaustedError):
            session.resolve_mutation(MutationState.NOT_FOUND)

    def test_locator_without_ambiguous_is_illegal(self) -> None:
        session = _session(contract=exclusive_write_contract())
        with pytest.raises(IllegalMemoryTransitionError, match="AMBIGUOUS"):
            session.open_locator_read()

    def test_committed_cannot_be_reopened(self) -> None:
        session = _session(contract=exclusive_write_contract())
        session.mark_mutation_attempted()
        session.resolve_mutation(MutationState.COMMITTED)
        with pytest.raises(IllegalMemoryTransitionError, match="already resolved"):
            session.mark_mutation_attempted()


class TestRecallLedgerAndAttribution:
    def test_handles_keep_purpose_and_receipt_identity(self) -> None:
        session = _session()
        session.record_recall(_handle(receipt="r-bg"))
        session.record_recall(
            RecallHandle(
                runtime_turn_id="turn-1",
                receipt_turn_id="r-tool",
                purpose=MemoryRecallPurpose.VERIFY,
                injected_fact_ids=(22, 23),
            )
        )
        handles = session.recall_handles()
        assert [item.purpose for item in handles] == [
            MemoryRecallPurpose.BACKGROUND,
            MemoryRecallPurpose.VERIFY,
        ]
        assert handles[0].receipt_turn_id == "r-bg"
        assert handles[1].injected_fact_ids == (22, 23)

    def test_duplicate_receipt_turn_id_is_rejected(self) -> None:
        session = _session()
        session.record_recall(_handle(receipt="r-1"))
        with pytest.raises(IllegalMemoryTransitionError, match="already recorded"):
            session.record_recall(_handle(receipt="r-1"))

    def test_attribution_none_to_frozen_to_queued(self) -> None:
        session = _session()
        session.freeze_exposures()
        session.queue_attribution()
        assert session.attribution is AttributionHandoff.QUEUED

    def test_attribution_can_skip_without_exposures(self) -> None:
        session = _session()
        session.skip_attribution()
        assert session.attribution is AttributionHandoff.SKIPPED

    def test_queued_cannot_skip(self) -> None:
        session = _session()
        session.freeze_exposures()
        session.queue_attribution()
        with pytest.raises(IllegalMemoryTransitionError, match="already queued"):
            session.skip_attribution()


class TestClose:
    def test_close_is_idempotent_and_blocks_work(self) -> None:
        session = _session()
        session.close()
        session.close()
        with pytest.raises(MemorySessionClosedError):
            session.start_prefetch()
        session.skip_attribution()
