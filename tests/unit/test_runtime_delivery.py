"""Unit tests for delivery outcome accounting (R1 commit 1)."""

from __future__ import annotations

from qq_ai_bot.runtime.delivery import (
    EMPTY_DELIVERY,
    DeliveryItemKind,
    DeliveryItemOutcome,
    DeliveryItemSource,
    DeliveryOutcome,
    DeliveryStatus,
)


def _item(
    *,
    kind: DeliveryItemKind = DeliveryItemKind.TEXT,
    source: DeliveryItemSource = DeliveryItemSource.AGENT_REPLY,
    accepted: bool = True,
    ledger: bool = True,
    error: str | None = None,
    receipt: str | None = "msg-1",
) -> DeliveryItemOutcome:
    return DeliveryItemOutcome(
        kind=kind,
        source=source,
        transport_accepted=accepted,
        receipt=receipt if accepted else None,
        ledger_recorded=ledger,
        error_category=error,
    )


class TestClassification:
    def test_all_accepted_is_complete(self) -> None:
        outcome = DeliveryOutcome(items=(_item(), _item(kind=DeliveryItemKind.EMOJI)))
        assert outcome.status is DeliveryStatus.COMPLETE
        assert outcome.sent_messages == 2

    def test_partial_delivery(self) -> None:
        outcome = DeliveryOutcome(items=(_item(), _item(accepted=False, error="transport_timeout")))
        assert outcome.status is DeliveryStatus.PARTIAL
        assert outcome.sent_messages == 1
        assert outcome.error_categories == ("transport_timeout",)

    def test_total_failure(self) -> None:
        outcome = DeliveryOutcome(items=(_item(accepted=False, error="send_failed"),))
        assert outcome.status is DeliveryStatus.FAILED
        assert outcome.sent_messages == 0

    def test_cancelled_keeps_accepted_items(self) -> None:
        outcome = DeliveryOutcome(items=(_item(),), cancelled=True)
        assert outcome.status is DeliveryStatus.CANCELLED
        assert outcome.sent_messages == 1

    def test_empty_delivery_is_complete_with_no_body(self) -> None:
        assert EMPTY_DELIVERY.status is DeliveryStatus.COMPLETE
        assert EMPTY_DELIVERY.sent_messages == 0
        assert not EMPTY_DELIVERY.agent_body_delivered


class TestRequiredRepresentations:
    def test_transport_accepted_but_ledger_failed(self) -> None:
        item = _item(ledger=False, error="ledger_write_failed")
        outcome = DeliveryOutcome(items=(item,))
        assert outcome.status is DeliveryStatus.COMPLETE
        assert item.transport_accepted and not item.ledger_recorded
        assert outcome.error_categories == ("ledger_write_failed",)

    def test_voice_only_delivery_counts_as_agent_body(self) -> None:
        outcome = DeliveryOutcome(items=(_item(kind=DeliveryItemKind.VOICE),))
        assert outcome.agent_body_delivered

    def test_emoji_only_delivery_is_not_agent_body(self) -> None:
        outcome = DeliveryOutcome(items=(_item(kind=DeliveryItemKind.EMOJI),))
        assert outcome.status is DeliveryStatus.COMPLETE
        assert not outcome.agent_body_delivered

    def test_recovery_notice_is_not_agent_body(self) -> None:
        outcome = DeliveryOutcome(items=(_item(source=DeliveryItemSource.RECOVERY_NOTICE),))
        assert not outcome.agent_body_delivered

    def test_sent_messages_is_derived_not_stored(self) -> None:
        assert isinstance(DeliveryOutcome.sent_messages, property)
        assert "sent_messages" not in DeliveryOutcome.__slots__
