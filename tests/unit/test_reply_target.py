"""Stable EventRecord reply-target resolution tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.reply_target import ReplyTargetControl, ReplyTargetResolver


class _Ledger:
    def __init__(self, *events: EventRecord) -> None:
        self.events = {event.id: event for event in events}

    async def get_event(self, event_id: int) -> EventRecord | None:
        return self.events.get(event_id)


def _event(
    event_id: int,
    *,
    scope: ScopeType = ScopeType.GROUP,
    bot_user_id: str = "9000",
    group_id: str | None = "2001",
    private_peer_user_id: str | None = None,
    platform_message_id: str = "12345",
    event_kind: str = "message",
    direction: str = "inbound",
) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id=bot_user_id,
        platform_message_id=platform_message_id,
        scope_type=scope,
        sender_user_id="9000" if direction == "outbound" else "1001",
        direction=direction,
        content="消息",
        visual_summary="",
        segments=(),
        occurred_at=datetime.now(UTC),
        group_id=group_id,
        private_peer_user_id=private_peer_user_id,
        event_kind=event_kind,
    )


def _inbound(
    *,
    scope: ScopeType = ScopeType.GROUP,
    group_id: str | None = "2001",
) -> InboundMessage:
    return InboundMessage(
        message_id="67890",
        event_type="message",
        scope_type=scope,
        sender=SenderIdentity(user_id="1001"),
        text="当前消息",
        bot_user_id="9000",
        group_id=group_id,
    )


@pytest.mark.asyncio
async def test_resolver_accepts_current_group_human_and_bot_events() -> None:
    human = _event(1)
    bot = _event(2, platform_message_id="12346", direction="outbound")
    resolver = ReplyTargetResolver(_Ledger(human, bot))

    human_result = await resolver.resolve(1, inbound=_inbound())
    bot_result = await resolver.resolve(2, inbound=_inbound())

    assert human_result.platform_message_id == "12345"
    assert bot_result.platform_message_id == "12346"


@pytest.mark.asyncio
async def test_resolver_rejects_cross_conversation_and_cross_bot_events() -> None:
    other_group = _event(1, group_id="2002")
    other_bot = _event(2, bot_user_id="9001")
    resolver = ReplyTargetResolver(_Ledger(other_group, other_bot))

    group_result = await resolver.resolve(1, inbound=_inbound())
    bot_result = await resolver.resolve(2, inbound=_inbound())

    assert group_result.reason == "different_conversation"
    assert bot_result.reason == "different_bot"
    assert group_result.platform_message_id is None
    assert bot_result.platform_message_id is None


@pytest.mark.asyncio
async def test_resolver_isolates_private_peers() -> None:
    current_peer = _event(
        1,
        scope=ScopeType.PRIVATE,
        group_id=None,
        private_peer_user_id="1001",
    )
    other_peer = _event(
        2,
        scope=ScopeType.PRIVATE,
        group_id=None,
        private_peer_user_id="1002",
    )
    resolver = ReplyTargetResolver(_Ledger(current_peer, other_peer))
    inbound = _inbound(scope=ScopeType.PRIVATE, group_id=None)

    current_result = await resolver.resolve(1, inbound=inbound)
    other_result = await resolver.resolve(2, inbound=inbound)

    assert current_result.ok is True
    assert other_result.reason == "different_conversation"


@pytest.mark.asyncio
async def test_resolver_rejects_external_or_non_onebot_events() -> None:
    external = _event(1, event_kind="external_event")
    missing_transport = _event(2, platform_message_id="synthetic-id")
    resolver = ReplyTargetResolver(_Ledger(external, missing_transport))

    external_result = await resolver.resolve(1, inbound=_inbound())
    transport_result = await resolver.resolve(2, inbound=_inbound())

    assert external_result.reason == "unsupported_event_kind"
    assert transport_result.reason == "transport_id_unavailable"


@pytest.mark.asyncio
async def test_agent_override_and_clear_control_quote_target() -> None:
    planner_event = _event(1)
    agent_event = _event(2, platform_message_id="12346")
    service = object.__new__(ChatService)
    service._reply_target_resolver = ReplyTargetResolver(  # type: ignore[attr-defined]
        _Ledger(planner_event, agent_event)
    )
    override = ReplyTargetControl(frozenset({1, 2}))
    assert override.apply(2)[0] is True

    selected = await ChatService._resolve_reply_target(
        service,
        inbound=_inbound(),
        conversation_key="group:2001",
        control=override,
    )

    assert selected == "12346"

    cleared = ReplyTargetControl(frozenset({1, 2}))
    assert cleared.apply(None)[0] is True
    selected_after_clear = await ChatService._resolve_reply_target(
        service,
        inbound=_inbound(),
        conversation_key="group:2001",
        control=cleared,
    )
    assert selected_after_clear is None
