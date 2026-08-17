"""Cross-domain pure data contracts.

Types that must be visible to more than one runtime (conversation, memory,
capability) live here so those packages never import each other directly.
Everything in this module is pure data plus pure policy functions — no I/O,
no service references.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from qq_ai_bot.runtime.delivery import DeliveryStatus
from qq_ai_bot.runtime.errors import UntrustedFinalizationError


class MemoryCapabilityView(BaseModel):
    """What the memory runtime exposes to the capability runtime (R2 §8).

    The capability runtime consumes this view verbatim and never reads
    memory-internal state.  ``transition_revision`` increments on every
    contract transition so stale views are detectable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    eager_namespaces: tuple[str, ...]
    requestable_namespaces: tuple[str, ...]
    hidden_namespaces: tuple[str, ...]
    exclusive_namespace: str | None
    transition_revision: int


@dataclass(frozen=True, slots=True)
class MemoryReceiptHandle:
    """Content-free handle to one recall receipt produced this turn.

    ``receipt_turn_id`` is the receipt's own unique id (the pre-existing
    ``memory_recall_receipts.turn_id`` semantics), *not* the runtime turn id.
    """

    receipt_turn_id: str
    injected_fact_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityExposureSnapshot:
    """Capability surface pinned for one turn at one catalog revision."""

    revision: int
    exposed_capability_ids: tuple[str, ...]
    requestable_capability_ids: tuple[str, ...] = ()
    schema_token_estimate: int = 0


@dataclass(frozen=True, slots=True)
class DeliverySummary:
    """What the delivery runtime hands to memory finalization/attribution.

    Carries the actually-delivered body because attribution must run on what
    the user saw, not on what the model drafted.  This object stays in
    process memory; it is never persisted.
    """

    final_agent_run_id: str
    status: DeliveryStatus
    delivered_text: str
    delivered_voice_text: str = ""
    emoji_only: bool = False
    transport_receipt_ids: tuple[str, ...] = ()


class TerminalFinalizationSource(StrEnum):
    """Host components trusted to end the agent loop from a tool batch."""

    HOST_MEMORY_FINALIZER = "host_memory_finalizer"
    HOST_REPLY_CONTROL = "host_reply_control"


TRUSTED_TERMINAL_SOURCES = frozenset(
    {
        TerminalFinalizationSource.HOST_MEMORY_FINALIZER,
        TerminalFinalizationSource.HOST_REPLY_CONTROL,
    }
)


@dataclass(frozen=True, slots=True)
class TerminalFinalization:
    """Host-authorized instruction to finish the agent loop after this batch."""

    source: TerminalFinalizationSource
    reason: str = ""


def authorize_terminal_finalization(
    candidate: TerminalFinalization | None,
    *,
    provider_is_host: bool,
) -> TerminalFinalization | None:
    """Gate terminal metadata at the provider→result mapping boundary.

    Only host-owned tool providers may propagate terminal finalization.
    Plugin/MCP providers returning terminal-looking metadata get it dropped
    here, so forged annotations can never end the agent loop.
    """

    if candidate is None:
        return None
    if not provider_is_host:
        return None
    if candidate.source not in TRUSTED_TERMINAL_SOURCES:
        raise UntrustedFinalizationError(
            f"unknown terminal finalization source: {candidate.source!r}"
        )
    return candidate


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    """Result of one executed (or skipped) tool call inside a batch."""

    call_id: str
    tool_name: str
    result_json: str
    executed: bool = True
    error_category: str | None = None


@dataclass(frozen=True, slots=True)
class ToolBatchExecutionResult:
    """Typed result of ``AgentToolBackend.execute_batch`` (R1 §5).

    ``terminal_finalization`` must have passed
    :func:`authorize_terminal_finalization`; the conversation session rejects
    TOOL_ACTIVE → FINALIZING transitions without a trusted source.
    """

    tool_results: tuple[ToolCallOutcome, ...]
    terminal_finalization: TerminalFinalization | None = None
