"""Focused coverage for privacy-safe plugin chat lifecycle notifications."""

from __future__ import annotations

import json

from nonebot.adapters.onebot.v11 import Message
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_normalizer import private_event

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from yuki_plugin_sdk.events import EventEnvelope, EventName


async def test_chat_lifecycle_events_are_metadata_only_and_ordered(
    database: Database,
) -> None:
    settings = make_settings(database.url)
    harness = build_harness(database, settings)
    bus = PluginEventBus(default_timeout_seconds=0.2)
    events: list[EventEnvelope] = []

    async def capture(event: EventEnvelope) -> None:
        events.append(event)

    async def failing_hook(_event: EventEnvelope) -> None:
        raise RuntimeError("event-secret must never escape")

    for event_name in (
        EventName.MESSAGE_NORMALIZED,
        EventName.MESSAGE_TRIGGERED,
        EventName.REPLY_SENT,
    ):
        bus.subscribe(
            plugin_id="com.example.capture",
            hook_id=event_name.value,
            event=event_name,
            handler=capture,
        )
    bus.subscribe(
        plugin_id="com.example.failure",
        hook_id="reply-failure",
        event=EventName.REPLY_SENT,
        handler=failing_hook,
    )
    harness.processor.set_event_publisher(bus)

    secret_body = "private-body-MUST-NOT-BE-IN-EVENT"
    sender = MemorySender()
    result = await harness.processor.handle(
        normalize_event(private_event(Message(secret_body), message_id=901)),
        sender,
    )

    assert result.reason == "chat"
    assert sender.messages
    assert [event.name for event in events] == [
        EventName.MESSAGE_NORMALIZED,
        EventName.MESSAGE_TRIGGERED,
        EventName.REPLY_SENT,
    ]
    assert set(events[0].payload) == {
        "message_id",
        "scope_type",
        "has_text",
        "attachment_count",
        "reply_attachment_count",
        "mentions_bot",
        "is_self_message",
    }
    assert set(events[1].payload) == {
        "message_id",
        "scope_type",
        "trigger_reason",
        "command",
        "visual_input_present",
        "mentions_bot",
    }
    assert set(events[2].payload) == {
        "trigger_message_id",
        "platform_message_id",
        "scope_type",
        "character_count",
        "delivered",
        "recorded",
    }
    serialized = json.dumps(
        [event.model_dump(mode="json") for event in events],
        ensure_ascii=False,
    )
    assert secret_body not in serialized
    assert "FakeLLM" not in serialized
    assert "tester" not in serialized
    assert "1001" not in serialized


async def test_publisher_exception_does_not_affect_chat(database: Database) -> None:
    class ExplodingPublisher:
        async def publish(self, _event: EventEnvelope) -> object:
            raise RuntimeError("message body or secret from third party")

    harness = build_harness(database, make_settings(database.url))
    harness.processor.set_event_publisher(ExplodingPublisher())
    sender = MemorySender()

    result = await harness.processor.handle(
        normalize_event(private_event(Message("still works"), message_id=902)),
        sender,
    )

    assert result.reason == "chat"
    assert len(sender.messages) == 1
