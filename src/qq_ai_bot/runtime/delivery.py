"""Typed delivery accounting for one turn.

Every outbound item is recorded individually so that voice-only, emoji-only,
"transport accepted but ledger write failed" and partial deliveries are all
representable.  ``sent_messages`` style aggregates are derived from the items;
they are never stored redundantly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeliveryItemKind(StrEnum):
    """What kind of payload one outbound item carried."""

    TEXT = "text"
    VOICE = "voice"
    EMOJI = "emoji"
    MEDIA = "media"


class DeliveryItemSource(StrEnum):
    """Which trusted host component produced the outbound item."""

    AGENT_REPLY = "agent_reply"
    REPLY_EFFECT = "reply_effect"
    AUTOMATION_STEP = "automation_step"
    PLUGIN_EMIT = "plugin_emit"
    RECOVERY_NOTICE = "recovery_notice"


class DeliveryStatus(StrEnum):
    """Aggregate delivery classification for one turn."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeliveryItemOutcome:
    """Result of sending exactly one outbound item.

    ``transport_accepted`` means the messaging transport acknowledged the
    send; ``receipt`` carries the transport message id when one exists.
    ``ledger_recorded`` tracks whether the outbound ledger write succeeded —
    a send can be accepted while the ledger write fails, and both facts must
    survive.
    """

    kind: DeliveryItemKind
    source: DeliveryItemSource
    transport_accepted: bool
    receipt: str | None = None
    ledger_recorded: bool = False
    error_category: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """Per-item delivery record plus derived aggregates for one turn.

    ``cancelled=True`` means delivery was interrupted by supersede/cancel;
    items already accepted before the interruption stay recorded.
    """

    items: tuple[DeliveryItemOutcome, ...]
    cancelled: bool = False

    @property
    def status(self) -> DeliveryStatus:
        if self.cancelled:
            return DeliveryStatus.CANCELLED
        if not self.items:
            return DeliveryStatus.COMPLETE
        accepted = sum(1 for item in self.items if item.transport_accepted)
        if accepted == len(self.items):
            return DeliveryStatus.COMPLETE
        if accepted == 0:
            return DeliveryStatus.FAILED
        return DeliveryStatus.PARTIAL

    @property
    def sent_messages(self) -> int:
        """Derived from per-item transport acceptance, never stored twice."""

        return sum(1 for item in self.items if item.transport_accepted)

    @property
    def agent_body_delivered(self) -> bool:
        """True when the agent's actual reply body (text/voice) went out."""

        return any(
            item.transport_accepted
            and item.source is DeliveryItemSource.AGENT_REPLY
            and item.kind in (DeliveryItemKind.TEXT, DeliveryItemKind.VOICE)
            for item in self.items
        )

    @property
    def error_categories(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.error_category for item in self.items if item.error_category is not None
            )
        )


EMPTY_DELIVERY = DeliveryOutcome(items=())
"""Delivery outcome for turns that intentionally sent nothing."""
