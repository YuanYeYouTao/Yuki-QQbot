"""Per-turn memory contract (frozen by R2 §3.1).

The contract is derived from trusted origin/config/authority before the
first model call and only changes through explicit, validated transitions.
Business code must check contract fields or session state — never a profile
name.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from qq_ai_bot.memory.enums import MemoryRecallPurpose
from qq_ai_bot.memory.runtime.errors import MemoryContractError

__all__ = [
    "MemoryAvailability",
    "MemoryContextPolicy",
    "MemoryContractError",
    "MemoryFinalizationPolicy",
    "MemoryReadPolicy",
    "MemoryTurnContract",
    "MemoryWritePolicy",
    "MemoryWriteTransition",
    "active_read_contract",
    "dormant_contract",
    "exclusive_write_contract",
    "forbidden_contract",
    "passive_contract",
    "to_exclusive_write",
    "to_locator_read",
    "to_mutation_retry",
]


class MemoryContextPolicy(StrEnum):
    """Automatic context injection allowed for this turn."""

    NONE = "none"
    BACKGROUND = "background"
    CONTINUATION = "continuation"


class MemoryReadPolicy(StrEnum):
    """How memory read tools may be exposed this turn."""

    DENIED = "denied"
    DEFERRED = "deferred"
    EAGER = "eager"
    LOCATOR_ONLY = "locator_only"


class MemoryWritePolicy(StrEnum):
    """Whether this turn currently holds the exclusive write lane."""

    DISABLED = "disabled"
    EXCLUSIVE = "exclusive"


class MemoryWriteTransition(StrEnum):
    """Whether this turn may transition into the exclusive write lane."""

    DENIED = "denied"
    REQUESTABLE = "requestable"
    ALREADY_EXCLUSIVE = "already_exclusive"


class MemoryFinalizationPolicy(StrEnum):
    """How the turn's reply is finalized with respect to memory receipts."""

    NORMAL = "normal"
    RECEIPT_GATED = "receipt_gated"


class MemoryAvailability(StrEnum):
    """Whether memory participates in this turn at all."""

    ENABLED = "enabled"
    FORBIDDEN = "forbidden"


class MemoryTurnContract(BaseModel):
    """Composition-validated memory policy for exactly one turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context_policy: MemoryContextPolicy
    read_policy: MemoryReadPolicy
    write_policy: MemoryWritePolicy
    write_transition: MemoryWriteTransition
    finalization_policy: MemoryFinalizationPolicy
    availability: MemoryAvailability
    default_purpose: MemoryRecallPurpose

    @model_validator(mode="after")
    def _validate_composition(self) -> MemoryTurnContract:
        if self.availability is MemoryAvailability.FORBIDDEN:
            forbidden_shape = (
                self.context_policy is MemoryContextPolicy.NONE
                and self.read_policy is MemoryReadPolicy.DENIED
                and self.write_policy is MemoryWritePolicy.DISABLED
                and self.write_transition is MemoryWriteTransition.DENIED
                and self.finalization_policy is MemoryFinalizationPolicy.NORMAL
            )
            if not forbidden_shape:
                raise MemoryContractError(
                    "FORBIDDEN availability requires context=NONE, read=DENIED, "
                    "write=DISABLED, transition=DENIED, finalization=NORMAL"
                )
        if self.write_policy is MemoryWritePolicy.EXCLUSIVE:
            if self.context_policy is not MemoryContextPolicy.NONE:
                raise MemoryContractError("EXCLUSIVE write forbids automatic context")
            if self.write_transition is not MemoryWriteTransition.ALREADY_EXCLUSIVE:
                raise MemoryContractError("EXCLUSIVE write requires transition=ALREADY_EXCLUSIVE")
            if self.finalization_policy is not MemoryFinalizationPolicy.RECEIPT_GATED:
                raise MemoryContractError("EXCLUSIVE write requires finalization=RECEIPT_GATED")
        else:
            if self.write_transition is MemoryWriteTransition.ALREADY_EXCLUSIVE:
                raise MemoryContractError("transition=ALREADY_EXCLUSIVE requires write=EXCLUSIVE")
            if self.finalization_policy is MemoryFinalizationPolicy.RECEIPT_GATED:
                raise MemoryContractError("finalization=RECEIPT_GATED requires write=EXCLUSIVE")
        if (
            self.context_policy is not MemoryContextPolicy.NONE
            and self.read_policy is not MemoryReadPolicy.DEFERRED
        ):
            raise MemoryContractError(
                "automatic context (background/continuation) requires read=DEFERRED"
            )
        if (
            self.read_policy is MemoryReadPolicy.LOCATOR_ONLY
            and self.write_policy is not MemoryWritePolicy.EXCLUSIVE
        ):
            raise MemoryContractError(
                "LOCATOR_ONLY read is only legal in the mutation locator phase"
            )
        if (
            self.write_transition is MemoryWriteTransition.REQUESTABLE
            and self.write_policy is not MemoryWritePolicy.DISABLED
        ):
            raise MemoryContractError("transition=REQUESTABLE requires current write=DISABLED")
        return self

    def require_write_authority(self, *, persistent_write_allowed: bool) -> None:
        """Reject write-capable contracts when authority forbids persistent writes."""

        if persistent_write_allowed:
            return
        if self.write_transition in (
            MemoryWriteTransition.REQUESTABLE,
            MemoryWriteTransition.ALREADY_EXCLUSIVE,
        ):
            raise MemoryContractError(
                "authority does not allow persistent memory writes; "
                f"transition={self.write_transition.value} is illegal"
            )


def _write_transition(*, persistent_write_allowed: bool) -> MemoryWriteTransition:
    if persistent_write_allowed:
        return MemoryWriteTransition.REQUESTABLE
    return MemoryWriteTransition.DENIED


def dormant_contract(
    default_purpose: MemoryRecallPurpose = MemoryRecallPurpose.BACKGROUND,
    *,
    persistent_write_allowed: bool = True,
) -> MemoryTurnContract:
    """No automatic inject; reads stay requestable.  Not a checkable profile name."""

    contract = MemoryTurnContract(
        context_policy=MemoryContextPolicy.NONE,
        read_policy=MemoryReadPolicy.DEFERRED,
        write_policy=MemoryWritePolicy.DISABLED,
        write_transition=_write_transition(persistent_write_allowed=persistent_write_allowed),
        finalization_policy=MemoryFinalizationPolicy.NORMAL,
        availability=MemoryAvailability.ENABLED,
        default_purpose=default_purpose,
    )
    contract.require_write_authority(persistent_write_allowed=persistent_write_allowed)
    return contract


def passive_contract(
    default_purpose: MemoryRecallPurpose = MemoryRecallPurpose.BACKGROUND,
    *,
    persistent_write_allowed: bool = True,
) -> MemoryTurnContract:
    """Automatic background/continuation inject; first-round reads stay deferred."""

    if default_purpose is MemoryRecallPurpose.CONTINUATION:
        context = MemoryContextPolicy.CONTINUATION
    elif default_purpose is MemoryRecallPurpose.BACKGROUND:
        context = MemoryContextPolicy.BACKGROUND
    else:
        raise MemoryContractError(
            "passive automatic context only accepts background or continuation purpose"
        )
    contract = MemoryTurnContract(
        context_policy=context,
        read_policy=MemoryReadPolicy.DEFERRED,
        write_policy=MemoryWritePolicy.DISABLED,
        write_transition=_write_transition(persistent_write_allowed=persistent_write_allowed),
        finalization_policy=MemoryFinalizationPolicy.NORMAL,
        availability=MemoryAvailability.ENABLED,
        default_purpose=default_purpose,
    )
    contract.require_write_authority(persistent_write_allowed=persistent_write_allowed)
    return contract


def active_read_contract(
    default_purpose: MemoryRecallPurpose = MemoryRecallPurpose.RECALL,
    *,
    persistent_write_allowed: bool = True,
) -> MemoryTurnContract:
    """No automatic inject; read tools are eager on the first model request."""

    contract = MemoryTurnContract(
        context_policy=MemoryContextPolicy.NONE,
        read_policy=MemoryReadPolicy.EAGER,
        write_policy=MemoryWritePolicy.DISABLED,
        write_transition=_write_transition(persistent_write_allowed=persistent_write_allowed),
        finalization_policy=MemoryFinalizationPolicy.NORMAL,
        availability=MemoryAvailability.ENABLED,
        default_purpose=default_purpose,
    )
    contract.require_write_authority(persistent_write_allowed=persistent_write_allowed)
    return contract


def exclusive_write_contract(
    default_purpose: MemoryRecallPurpose = MemoryRecallPurpose.CORRECT,
    *,
    persistent_write_allowed: bool = True,
    locator_only: bool = False,
) -> MemoryTurnContract:
    """Exclusive mutation lane.  Locator reads are an explicit escalation."""

    contract = MemoryTurnContract(
        context_policy=MemoryContextPolicy.NONE,
        read_policy=(MemoryReadPolicy.LOCATOR_ONLY if locator_only else MemoryReadPolicy.DENIED),
        write_policy=MemoryWritePolicy.EXCLUSIVE,
        write_transition=MemoryWriteTransition.ALREADY_EXCLUSIVE,
        finalization_policy=MemoryFinalizationPolicy.RECEIPT_GATED,
        availability=MemoryAvailability.ENABLED,
        default_purpose=default_purpose,
    )
    contract.require_write_authority(persistent_write_allowed=persistent_write_allowed)
    return contract


def forbidden_contract(default_purpose: MemoryRecallPurpose) -> MemoryTurnContract:
    """The only valid FORBIDDEN-availability shape, as a convenience factory."""

    return MemoryTurnContract(
        context_policy=MemoryContextPolicy.NONE,
        read_policy=MemoryReadPolicy.DENIED,
        write_policy=MemoryWritePolicy.DISABLED,
        write_transition=MemoryWriteTransition.DENIED,
        finalization_policy=MemoryFinalizationPolicy.NORMAL,
        availability=MemoryAvailability.FORBIDDEN,
        default_purpose=default_purpose,
    )


def to_exclusive_write(contract: MemoryTurnContract) -> MemoryTurnContract:
    """Atomically enter the exclusive write lane from a requestable contract."""

    if contract.availability is MemoryAvailability.FORBIDDEN:
        raise MemoryContractError("FORBIDDEN contracts cannot enter exclusive write")
    if contract.write_transition is not MemoryWriteTransition.REQUESTABLE:
        raise MemoryContractError(
            "exclusive write requires transition=REQUESTABLE on the current contract"
        )
    return exclusive_write_contract(
        contract.default_purpose,
        persistent_write_allowed=True,
    )


def to_locator_read(contract: MemoryTurnContract) -> MemoryTurnContract:
    """Open the one-shot locator-read escalation on an exclusive-write contract."""

    if contract.write_policy is not MemoryWritePolicy.EXCLUSIVE:
        raise MemoryContractError("locator read requires write=EXCLUSIVE")
    if contract.read_policy is MemoryReadPolicy.LOCATOR_ONLY:
        return contract
    return exclusive_write_contract(
        contract.default_purpose,
        persistent_write_allowed=True,
        locator_only=True,
    )


def to_mutation_retry(contract: MemoryTurnContract) -> MemoryTurnContract:
    """Return to exclusive write after a locator read, hiding read tools again."""

    if contract.write_policy is not MemoryWritePolicy.EXCLUSIVE:
        raise MemoryContractError("mutation retry requires write=EXCLUSIVE")
    return exclusive_write_contract(
        contract.default_purpose,
        persistent_write_allowed=True,
        locator_only=False,
    )
