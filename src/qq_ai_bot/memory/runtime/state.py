"""Orthogonal memory-session machines (R2 §4).

These machines do not copy the R1 turn lifecycle.  Access, mutation and
attribution are independent ledgers: a turn can prefetch, later become
exclusive-write, then freeze exposures for a worker that never holds the
live session.

Business code must inspect these states or the contract fields — never a
profile name.
"""

from __future__ import annotations

from enum import StrEnum

from qq_ai_bot.memory.enums import MemoryRecallPurpose
from qq_ai_bot.memory.runtime.contract import (
    MemoryAvailability,
    MemoryContextPolicy,
    MemoryReadPolicy,
    MemoryTurnContract,
    MemoryWritePolicy,
    MemoryWriteTransition,
    to_exclusive_write,
    to_locator_read,
    to_mutation_retry,
)
from qq_ai_bot.memory.runtime.errors import (
    IllegalMemoryTransitionError,
    MemoryLocatorRetryExhaustedError,
    MemorySessionClosedError,
)
from qq_ai_bot.runtime.keys import ResolvedMemoryScope


class AccessPhase(StrEnum):
    """How memory tools and prefetch currently participate in the turn."""

    DORMANT = "dormant"
    PREFETCHING = "prefetching"
    PREFETCHED = "prefetched"
    READ_ENABLED = "read_enabled"
    MUTATION_EXCLUSIVE = "mutation_exclusive"
    LOCATOR_READ_ENABLED = "locator_read_enabled"
    LOCATOR_READ_DONE = "locator_read_done"


class MutationState(StrEnum):
    """Durable-write attempt ledger for one turn.

    ``AMBIGUOUS`` / ``NOT_FOUND`` are not turn-terminal: they may enter the
    locator-read escalation once.  The other post-attempt values are terminal
    for the mutation ledger.
    """

    NOT_ATTEMPTED = "not_attempted"
    ATTEMPTED = "attempted"
    COMMITTED = "committed"
    COMMITTED_AS_CONTESTED = "committed_as_contested"
    DEDUPLICATED = "deduplicated"
    NO_CHANGE = "no_change"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class AttributionHandoff(StrEnum):
    """Delivery-side attribution job lifecycle.  The worker never holds a session."""

    NONE = "none"
    EXPOSURE_FROZEN = "exposure_frozen"
    QUEUED = "queued"
    SKIPPED = "skipped"


class LocatorStatus(StrEnum):
    """Bounded locator-read escalation.  At most one read-then-retry cycle."""

    UNUSED = "unused"
    OPEN = "open"
    CONSUMED = "consumed"
    EXHAUSTED = "exhausted"


_ACCESS_TRANSITIONS: dict[AccessPhase, frozenset[AccessPhase]] = {
    AccessPhase.DORMANT: frozenset(
        {
            AccessPhase.PREFETCHING,
            AccessPhase.READ_ENABLED,
            AccessPhase.MUTATION_EXCLUSIVE,
        }
    ),
    AccessPhase.PREFETCHING: frozenset({AccessPhase.PREFETCHED}),
    AccessPhase.PREFETCHED: frozenset({AccessPhase.READ_ENABLED, AccessPhase.MUTATION_EXCLUSIVE}),
    AccessPhase.READ_ENABLED: frozenset({AccessPhase.MUTATION_EXCLUSIVE}),
    AccessPhase.MUTATION_EXCLUSIVE: frozenset({AccessPhase.LOCATOR_READ_ENABLED}),
    AccessPhase.LOCATOR_READ_ENABLED: frozenset({AccessPhase.LOCATOR_READ_DONE}),
    AccessPhase.LOCATOR_READ_DONE: frozenset({AccessPhase.MUTATION_EXCLUSIVE}),
}

_TERMINAL_MUTATIONS = frozenset(
    {
        MutationState.COMMITTED,
        MutationState.COMMITTED_AS_CONTESTED,
        MutationState.DEDUPLICATED,
        MutationState.NO_CHANGE,
        MutationState.REJECTED,
    }
)
_LOCATOR_MUTATIONS = frozenset({MutationState.AMBIGUOUS, MutationState.NOT_FOUND})
_RESOLVED_MUTATIONS = _TERMINAL_MUTATIONS | _LOCATOR_MUTATIONS


def initial_access_phase(contract: MemoryTurnContract) -> AccessPhase:
    """Derive the starting access phase from a frozen contract.

    Passive turns start ``DORMANT`` so ``prefetch()`` can move them through
    ``PREFETCHING``; eager-read and exclusive-write start already enabled.
    """

    if contract.availability is MemoryAvailability.FORBIDDEN:
        return AccessPhase.DORMANT
    if contract.write_policy is MemoryWritePolicy.EXCLUSIVE:
        if contract.read_policy is MemoryReadPolicy.LOCATOR_ONLY:
            return AccessPhase.LOCATOR_READ_ENABLED
        return AccessPhase.MUTATION_EXCLUSIVE
    if contract.read_policy is MemoryReadPolicy.EAGER:
        return AccessPhase.READ_ENABLED
    return AccessPhase.DORMANT


class AccessPhaseMachine:
    """Legal access-phase table for one memory session."""

    __slots__ = ("_phase",)

    def __init__(self, initial: AccessPhase = AccessPhase.DORMANT) -> None:
        self._phase = initial

    @property
    def phase(self) -> AccessPhase:
        return self._phase

    def advance(self, target: AccessPhase) -> None:
        if target is self._phase:
            return
        allowed = _ACCESS_TRANSITIONS[self._phase]
        if target not in allowed:
            raise IllegalMemoryTransitionError(self._phase.value, target.value)
        self._phase = target


class MutationStateMachine:
    """Attempt → outcome ledger.  Locator outcomes may retry once."""

    __slots__ = ("_retries", "_state")

    def __init__(self) -> None:
        self._state = MutationState.NOT_ATTEMPTED
        self._retries = 0

    @property
    def state(self) -> MutationState:
        return self._state

    @property
    def locator_retries(self) -> int:
        return self._retries

    @property
    def terminal(self) -> bool:
        return self._state in _TERMINAL_MUTATIONS

    def mark_attempted(self) -> None:
        if self._state is MutationState.NOT_ATTEMPTED:
            self._state = MutationState.ATTEMPTED
            return
        if self._state in _LOCATOR_MUTATIONS:
            if self._retries >= 1:
                raise MemoryLocatorRetryExhaustedError(
                    "mutation already used its one locator-read retry"
                )
            self._retries += 1
            self._state = MutationState.ATTEMPTED
            return
        raise IllegalMemoryTransitionError(
            self._state.value,
            MutationState.ATTEMPTED.value,
            "write already resolved",
        )

    def resolve(self, outcome: MutationState) -> None:
        if outcome not in _RESOLVED_MUTATIONS:
            raise IllegalMemoryTransitionError(
                self._state.value,
                outcome.value,
                "not a mutation outcome",
            )
        if self._state is not MutationState.ATTEMPTED:
            raise IllegalMemoryTransitionError(
                self._state.value,
                outcome.value,
                "resolve requires ATTEMPTED",
            )
        if outcome in _LOCATOR_MUTATIONS and self._retries >= 1:
            raise MemoryLocatorRetryExhaustedError(
                "locator outcome after the allowed retry must close deterministically"
            )
        self._state = outcome


class AttributionHandoffMachine:
    """Freeze exposures, then queue or skip.  Never re-opens after a terminal."""

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state = AttributionHandoff.NONE

    @property
    def state(self) -> AttributionHandoff:
        return self._state

    def freeze_exposures(self) -> None:
        if self._state is AttributionHandoff.NONE:
            self._state = AttributionHandoff.EXPOSURE_FROZEN
            return
        if self._state is AttributionHandoff.EXPOSURE_FROZEN:
            return
        raise IllegalMemoryTransitionError(
            self._state.value,
            AttributionHandoff.EXPOSURE_FROZEN.value,
            "attribution already handed off",
        )

    def queue(self) -> None:
        if self._state is AttributionHandoff.QUEUED:
            return
        if self._state is not AttributionHandoff.EXPOSURE_FROZEN:
            raise IllegalMemoryTransitionError(
                self._state.value,
                AttributionHandoff.QUEUED.value,
                "queue requires frozen exposures",
            )
        self._state = AttributionHandoff.QUEUED

    def skip(self) -> None:
        if self._state in {AttributionHandoff.QUEUED, AttributionHandoff.SKIPPED}:
            if self._state is AttributionHandoff.QUEUED:
                raise IllegalMemoryTransitionError(
                    self._state.value,
                    AttributionHandoff.SKIPPED.value,
                    "already queued",
                )
            return
        if self._state is AttributionHandoff.NONE:
            self._state = AttributionHandoff.SKIPPED
            return
        if self._state is AttributionHandoff.EXPOSURE_FROZEN:
            self._state = AttributionHandoff.SKIPPED
            return
        raise IllegalMemoryTransitionError(self._state.value, AttributionHandoff.SKIPPED.value)


class RecallHandle:
    """One automatic or tool read that actually entered a model request.

    ``receipt_turn_id`` keeps the pre-existing receipt-row identity.  It is
    never interchangeable with ``runtime_turn_id``.
    """

    __slots__ = (
        "injected_fact_ids",
        "purpose",
        "receipt_turn_id",
        "runtime_turn_id",
    )

    def __init__(
        self,
        *,
        runtime_turn_id: str,
        receipt_turn_id: str,
        purpose: MemoryRecallPurpose,
        injected_fact_ids: tuple[int, ...] = (),
    ) -> None:
        if not runtime_turn_id:
            raise IllegalMemoryTransitionError("recall", "handle", "runtime_turn_id is required")
        if not receipt_turn_id:
            raise IllegalMemoryTransitionError("recall", "handle", "receipt_turn_id is required")
        self.runtime_turn_id = runtime_turn_id
        self.receipt_turn_id = receipt_turn_id
        self.purpose = purpose
        self.injected_fact_ids = injected_fact_ids


class RecallLedger:
    """Append-only list of recall handles for one turn."""

    __slots__ = ("_handles",)

    def __init__(self) -> None:
        self._handles: list[RecallHandle] = []

    def append(self, handle: RecallHandle) -> None:
        if any(item.receipt_turn_id == handle.receipt_turn_id for item in self._handles):
            raise IllegalMemoryTransitionError(
                handle.receipt_turn_id,
                "append",
                "receipt_turn_id already recorded",
            )
        self._handles.append(handle)

    def snapshot(self) -> tuple[RecallHandle, ...]:
        return tuple(self._handles)

    def __len__(self) -> int:
        return len(self._handles)


class MemorySessionState:
    """In-memory holder for one turn's contract, machines and ledgers.

    This is not the I/O session.  Chat must not reach into these fields
    except through the later ``MemoryTurnSession`` implementation.
    """

    __slots__ = (
        "_access",
        "_attribution",
        "_closed",
        "_contract",
        "_last_mutation_receipt_id",
        "_locator",
        "_mutation",
        "_recalls",
        "_scope",
        "_transition_revision",
    )

    def __init__(self, contract: MemoryTurnContract, scope: ResolvedMemoryScope) -> None:
        self._contract = contract
        self._scope = scope
        self._access = AccessPhaseMachine(initial_access_phase(contract))
        self._mutation = MutationStateMachine()
        self._attribution = AttributionHandoffMachine()
        self._locator = (
            LocatorStatus.OPEN
            if self._access.phase is AccessPhase.LOCATOR_READ_ENABLED
            else LocatorStatus.UNUSED
        )
        self._recalls = RecallLedger()
        self._last_mutation_receipt_id: str | None = None
        self._transition_revision = 1
        self._closed = False

    @property
    def contract(self) -> MemoryTurnContract:
        return self._contract

    @property
    def scope(self) -> ResolvedMemoryScope:
        return self._scope

    @property
    def access_phase(self) -> AccessPhase:
        return self._access.phase

    @property
    def mutation_state(self) -> MutationState:
        return self._mutation.state

    @property
    def attribution(self) -> AttributionHandoff:
        return self._attribution.state

    @property
    def locator_status(self) -> LocatorStatus:
        return self._locator

    @property
    def transition_revision(self) -> int:
        return self._transition_revision

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_mutation_receipt_id(self) -> str | None:
        return self._last_mutation_receipt_id

    def recall_handles(self) -> tuple[RecallHandle, ...]:
        return self._recalls.snapshot()

    def start_prefetch(self) -> None:
        self._require_open()
        self._require_available()
        if self._contract.context_policy is MemoryContextPolicy.NONE:
            raise IllegalMemoryTransitionError(
                self._access.phase.value,
                AccessPhase.PREFETCHING.value,
                "context_policy=NONE forbids prefetch",
            )
        self._access.advance(AccessPhase.PREFETCHING)

    def complete_prefetch(self) -> None:
        self._require_open()
        self._access.advance(AccessPhase.PREFETCHED)

    def enable_read(self) -> None:
        self._require_open()
        self._require_available()
        if self._contract.read_policy is MemoryReadPolicy.DENIED:
            raise IllegalMemoryTransitionError(
                self._access.phase.value,
                AccessPhase.READ_ENABLED.value,
                "read_policy=DENIED",
            )
        if self._contract.write_policy is MemoryWritePolicy.EXCLUSIVE:
            raise IllegalMemoryTransitionError(
                self._access.phase.value,
                AccessPhase.READ_ENABLED.value,
                "exclusive write cannot become a general read lane",
            )
        self._access.advance(AccessPhase.READ_ENABLED)

    def enter_exclusive_write(self) -> None:
        self._require_open()
        self._require_available()
        if self._contract.write_transition is MemoryWriteTransition.ALREADY_EXCLUSIVE:
            self._access.advance(AccessPhase.MUTATION_EXCLUSIVE)
            return
        self._replace_contract(to_exclusive_write(self._contract))
        self._access.advance(AccessPhase.MUTATION_EXCLUSIVE)

    def open_locator_read(self) -> None:
        self._require_open()
        if self._mutation.state not in _LOCATOR_MUTATIONS:
            raise IllegalMemoryTransitionError(
                self._access.phase.value,
                AccessPhase.LOCATOR_READ_ENABLED.value,
                "locator read requires AMBIGUOUS or NOT_FOUND",
            )
        if self._locator is LocatorStatus.EXHAUSTED:
            raise MemoryLocatorRetryExhaustedError("locator read already exhausted")
        if self._locator is LocatorStatus.CONSUMED:
            raise MemoryLocatorRetryExhaustedError("locator read already consumed")
        self._replace_contract(to_locator_read(self._contract))
        self._access.advance(AccessPhase.LOCATOR_READ_ENABLED)
        self._locator = LocatorStatus.OPEN

    def complete_locator_read(self) -> None:
        self._require_open()
        self._access.advance(AccessPhase.LOCATOR_READ_DONE)
        self._locator = LocatorStatus.CONSUMED

    def return_to_exclusive_write(self) -> None:
        self._require_open()
        self._replace_contract(to_mutation_retry(self._contract))
        self._access.advance(AccessPhase.MUTATION_EXCLUSIVE)

    def mark_mutation_attempted(self, *, receipt_id: str | None = None) -> None:
        self._require_open()
        if self._access.phase is not AccessPhase.MUTATION_EXCLUSIVE:
            raise IllegalMemoryTransitionError(
                self._access.phase.value,
                MutationState.ATTEMPTED.value,
                "writes require MUTATION_EXCLUSIVE",
            )
        self._mutation.mark_attempted()
        if receipt_id:
            self._last_mutation_receipt_id = receipt_id

    def resolve_mutation(self, outcome: MutationState, *, receipt_id: str | None = None) -> None:
        self._require_open()
        self._mutation.resolve(outcome)
        if receipt_id:
            self._last_mutation_receipt_id = receipt_id
        if outcome in _LOCATOR_MUTATIONS and self._locator is LocatorStatus.UNUSED:
            return
        if outcome in _LOCATOR_MUTATIONS and self._locator is LocatorStatus.CONSUMED:
            self._locator = LocatorStatus.EXHAUSTED

    def record_recall(self, handle: RecallHandle) -> None:
        self._require_open()
        self._recalls.append(handle)

    def freeze_exposures(self) -> None:
        self._require_open()
        self._attribution.freeze_exposures()

    def queue_attribution(self) -> None:
        self._require_open()
        self._attribution.queue()

    def skip_attribution(self) -> None:
        self._attribution.skip()

    def close(self) -> None:
        self._closed = True

    def _replace_contract(self, contract: MemoryTurnContract) -> None:
        self._contract = contract
        self._transition_revision += 1

    def _require_open(self) -> None:
        if self._closed:
            raise MemorySessionClosedError("memory session already closed")

    def _require_available(self) -> None:
        if self._contract.availability is MemoryAvailability.FORBIDDEN:
            raise IllegalMemoryTransitionError(
                self._access.phase.value,
                "available",
                "FORBIDDEN forbids prefetch, read and write",
            )
