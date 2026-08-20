"""Rollup model compaction honors the configured origin whitelist."""

from __future__ import annotations

from datetime import UTC, datetime

from qq_ai_bot.conversation.rollup.models import RollupCandidate, RollupKind, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.origins import parse_rollup_llm_origins
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatResponse,
    InboundMessage,
    OutboundMessage,
    OutboundSendReceipt,
    SenderIdentity,
)
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.chat import ChatService


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, task, request, *, priority=None):
        del task, request, priority
        self.calls += 1
        return ChatResponse(content="model summary", latency_seconds=0)


def _event(event_id: int, *, origin: str) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="bot-a",
        platform_message_id=f"msg-{event_id}",
        scope_type=ScopeType.GROUP,
        sender_user_id="10001",
        sender_group_card="Alice",
        direction="inbound",
        content=f"event-{event_id}",
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 8, 20, 0, event_id % 60, tzinfo=UTC),
        group_id="group-1",
        origin=origin,
    )


def _candidate(events: tuple[EventRecord, ...]) -> RollupCandidate:
    return RollupCandidate(
        scope_id=1,
        generation=1,
        source_coverage=0,
        source_rollup_revision=0,
        previous_summary="",
        events=events,
        event_count=len(events),
        projection_characters=10,
        fingerprint="test",
    )


def _policy(*origins: str) -> RollupPolicyConfig:
    return RollupPolicyConfig(
        raw_tail_events=2,
        raw_tail_characters=100_000,
        trigger_events=2,
        trigger_characters=100_000,
        stop_events=0,
        stop_characters=0,
        batch_max_events=10,
        batch_max_characters=100_000,
        summary_max_characters=2_000,
        llm_origins=frozenset(origins),
    )


async def _summarize(
    events: tuple[EventRecord, ...],
    *origins: str,
) -> tuple[RecordingExecutor, RollupKind]:
    executor = RecordingExecutor()
    service = ConversationRollupService(
        models=executor,
        config=_policy(*origins),
        timeout_seconds=1,
    )
    _summary, kind = await service.summarize_candidate(_candidate(events))
    return executor, kind


async def test_human_batch_skips_model_when_whitelist_is_plugin_background() -> None:
    executor, kind = await _summarize(
        (_event(1, origin="user_message"), _event(2, origin="user_message")),
        "plugin_background",
    )
    assert executor.calls == 0
    assert kind is RollupKind.EXTRACTIVE


async def test_plugin_background_batch_is_extractive_under_default_whitelist() -> None:
    executor, kind = await _summarize(
        (_event(1, origin="plugin_background"), _event(2, origin="plugin_background")),
        "user_message",
    )
    assert executor.calls == 0
    assert kind is RollupKind.EXTRACTIVE


async def test_mixed_batch_is_extractive() -> None:
    executor, kind = await _summarize(
        (_event(1, origin="user_message"), _event(2, origin="plugin_background")),
        "user_message",
    )
    assert executor.calls == 0
    assert kind is RollupKind.EXTRACTIVE


async def test_human_batch_uses_model_when_whitelisted() -> None:
    executor, kind = await _summarize(
        (_event(1, origin="user_message"),),
        "user_message",
    )
    assert executor.calls == 1
    assert kind is RollupKind.MODEL


def test_empty_llm_origins_config_defaults_to_user_message() -> None:
    assert parse_rollup_llm_origins("") == frozenset({"user_message"})
    assert parse_rollup_llm_origins("not_an_origin") == frozenset({"user_message"})
    assert RollupPolicyConfig(llm_origins=frozenset()).llm_origins == frozenset({"user_message"})


async def test_autonomous_outbound_records_autonomous_group_origin() -> None:
    captured: dict[str, object] = {}

    class Ledger:
        async def append(self, **kwargs: object) -> None:
            captured.update(kwargs)

    service = object.__new__(ChatService)
    service._ledger = Ledger()
    service._ledger_origin = TurnOrigin.AUTONOMOUS_GROUP.value
    service._event_publisher = None
    inbound = InboundMessage(
        message_id="in-1",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="10001"),
        text="hi",
        bot_user_id="bot-a",
        group_id="group-1",
    )
    await service._record_outbound_message(
        inbound,
        OutboundMessage(text="reply"),
        OutboundSendReceipt(platform_message_id="out-1"),
    )
    assert captured["origin"] == "autonomous_group"
    assert captured["direction"] == "outbound"
