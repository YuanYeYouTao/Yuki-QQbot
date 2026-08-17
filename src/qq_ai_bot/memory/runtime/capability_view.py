"""Derive the capability-facing view from a memory contract.

The capability runtime never reads memory-internal state; it consumes the
pure ``MemoryCapabilityView`` built here.  Namespace ids follow the R3
migration table (R2 §8's ``memory.history.read`` is shorthand for the two
history namespaces below).
"""

from __future__ import annotations

from qq_ai_bot.memory.runtime.contract import (
    MemoryAvailability,
    MemoryReadPolicy,
    MemoryTurnContract,
    MemoryWritePolicy,
    MemoryWriteTransition,
)
from qq_ai_bot.runtime.contracts import MemoryCapabilityView

MEMORY_READ_NAMESPACES: tuple[str, ...] = (
    "memory.history.recent",
    "memory.history.search",
    "memory.person.read",
    "memory.self.read",
    "memory.group.read",
)
MEMORY_WRITE_NAMESPACE = "memory.state.write"


def build_capability_view(
    contract: MemoryTurnContract, *, transition_revision: int
) -> MemoryCapabilityView:
    """Project one contract onto eager/requestable/hidden namespace sets.

    Deterministic mapping:

    - ``FORBIDDEN`` hides everything.
    - ``EXCLUSIVE`` write exposes only ``memory.state.write`` eagerly and
      hides every read namespace (locator reads are a session-internal
      escalation, not an exposure default).
    - Otherwise reads follow the read policy (``EAGER`` → eager,
      ``DEFERRED`` → requestable, ``DENIED`` → hidden) and the write
      namespace follows the transition (``REQUESTABLE`` → requestable,
      ``DENIED`` → hidden).
    """

    if contract.availability is MemoryAvailability.FORBIDDEN:
        return MemoryCapabilityView(
            eager_namespaces=(),
            requestable_namespaces=(),
            hidden_namespaces=(*MEMORY_READ_NAMESPACES, MEMORY_WRITE_NAMESPACE),
            exclusive_namespace=None,
            transition_revision=transition_revision,
        )
    if contract.write_policy is MemoryWritePolicy.EXCLUSIVE:
        if contract.read_policy is MemoryReadPolicy.LOCATOR_ONLY:
            return MemoryCapabilityView(
                eager_namespaces=(*MEMORY_READ_NAMESPACES, MEMORY_WRITE_NAMESPACE),
                requestable_namespaces=(),
                hidden_namespaces=(),
                exclusive_namespace=MEMORY_WRITE_NAMESPACE,
                transition_revision=transition_revision,
            )
        return MemoryCapabilityView(
            eager_namespaces=(MEMORY_WRITE_NAMESPACE,),
            requestable_namespaces=(),
            hidden_namespaces=MEMORY_READ_NAMESPACES,
            exclusive_namespace=MEMORY_WRITE_NAMESPACE,
            transition_revision=transition_revision,
        )

    eager: tuple[str, ...] = ()
    requestable: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()
    if contract.read_policy is MemoryReadPolicy.EAGER:
        eager = MEMORY_READ_NAMESPACES
    elif contract.read_policy is MemoryReadPolicy.DEFERRED:
        requestable = MEMORY_READ_NAMESPACES
    else:
        hidden = MEMORY_READ_NAMESPACES

    if contract.write_transition is MemoryWriteTransition.REQUESTABLE:
        requestable = (*requestable, MEMORY_WRITE_NAMESPACE)
    else:
        hidden = (*hidden, MEMORY_WRITE_NAMESPACE)

    return MemoryCapabilityView(
        eager_namespaces=eager,
        requestable_namespaces=requestable,
        hidden_namespaces=hidden,
        exclusive_namespace=None,
        transition_revision=transition_revision,
    )
