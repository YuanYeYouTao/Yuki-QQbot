from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    InboundMessage,
    OutboundMessage,
    OutboundSendReceipt,
    SenderIdentity,
)
from qq_ai_bot.emoji.db_models import EmojiUsageEventModel
from qq_ai_bot.emoji.effects import EmojiReplyEffectService
from qq_ai_bot.emoji.grid import EmojiGridBuilder
from qq_ai_bot.emoji.models import (
    EmojiIntent,
    EmojiPlacement,
    EmojiReplyMode,
    EmojiReplyPlan,
)
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.retriever import EmojiRetriever
from qq_ai_bot.emoji.selector import EmojiSelector
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.planner import (
    DeliveryMode,
    FakePlannerProvider,
    PlannerDecision,
    PlannerObservability,
    PlannerReasonCode,
    TurnPlan,
)
from qq_ai_bot.planner.service import PlannerService
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from yuki_plugin_sdk.events import EventEnvelope, EventName


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    async def publish(self, event: EventEnvelope) -> object:
        self.events.append(event)
        return None


class _MediaFailingSender(MemorySender):
    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        self.calls += 1
        if message.media:
            raise RuntimeError("image transport failed")
        self.messages.append(message)
        return OutboundSendReceipt(
            platform_message_id=str(910000 + self.calls),
            transport="test",
        )


async def _install_real_emoji_effect(
    database: Database,
    tmp_path: Path,
    harness: object,
    *,
    with_asset: bool,
) -> tuple[EmojiRepository, _EventCollector]:
    storage = EmojiStorage(tmp_path / "emoji")
    repository = EmojiRepository(database)
    if with_asset:
        output = io.BytesIO()
        Image.new("RGB", (24, 20), "pink").save(output, format="PNG")
        content = output.getvalue()
        media = storage.inspect(content, near_duplicate_enabled=False)
        storage.persist(content, media)
        asset, _ = await repository.record_candidate(
            media,
            source_event_id=None,
            user_id=None,
            group_id=None,
            source_sub_type="emoji",
            source_emoji_id="",
            source_package_id="",
        )
        await repository.adopt_scope(asset.id, scope_type="global")
    selector = EmojiSelector(
        retriever=EmojiRetriever(repository, storage),
        grid_builder=EmojiGridBuilder(storage),
        preprocessor=ImagePreprocessor(),
        provider=None,
    )
    events = _EventCollector()
    effect = EmojiReplyEffectService(
        selector=selector,
        repository=repository,
        storage=storage,
        event_publisher=events,
    )
    harness.processor._chat._emoji_effects = effect  # type: ignore[attr-defined]
    return repository, events


def _message(message_id: str) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="测试用户"),
        text="发个表情",
        group_id="2001",
        mentions_bot=True,
        bot_user_id="9000",
    )


async def _usage_count(database: Database) -> int:
    async with database.sessions() as session:
        return int(
            await session.scalar(select(func.count()).select_from(EmojiUsageEventModel)) or 0
        )


@pytest.mark.asyncio
async def test_group_emoji_fast_path_sends_records_and_marks_usage(
    database: Database,
    tmp_path: Path,
) -> None:
    harness = build_harness(
        database,
        make_settings(database.url, emoji_enabled=True),
    )
    _repository, events = await _install_real_emoji_effect(
        database,
        tmp_path,
        harness,
        with_asset=True,
    )
    sender = MemorySender()

    result = await harness.processor.handle(_message("emoji-success"), sender)

    assert result.sent_messages == 1
    assert len(sender.messages) == 1 and sender.messages[0].media
    assert harness.provider.requests == []  # type: ignore[attr-defined]
    recent = await EventLedgerRepository(database).list_recent(
        scope_type=ScopeType.GROUP,
        user_id="1001",
        group_id="2001",
        limit=10,
    )
    outbound = [row for row in recent if row.direction == "outbound"]
    assert len(outbound) == 1
    assert any(segment.get("type") == "image" for segment in outbound[0].segments)
    assert await _usage_count(database) == 1
    assert sum(event.name is EventName.EMOJI_SENT for event in events.events) == 1


@pytest.mark.asyncio
async def test_group_emoji_repository_failure_becomes_truthful_text(
    database: Database,
    tmp_path: Path,
) -> None:
    harness = build_harness(
        database,
        make_settings(database.url, emoji_enabled=True),
    )
    repository, events = await _install_real_emoji_effect(
        database,
        tmp_path,
        harness,
        with_asset=True,
    )
    repository.selectable = AsyncMock(side_effect=SQLAlchemyError("query failed"))  # type: ignore[method-assign]
    sender = MemorySender()

    result = await harness.processor.handle(_message("emoji-repository-failure"), sender)

    assert result.sent_messages == 1
    assert [message.text for message in sender.messages] == ["表情没发出去，表情库暂时不可用。"]
    assert all(not message.media for message in sender.messages)
    assert await _usage_count(database) == 0
    assert all(event.name is not EventName.EMOJI_SENT for event in events.events)


@pytest.mark.asyncio
async def test_group_emoji_no_candidate_is_not_silent(
    database: Database,
    tmp_path: Path,
) -> None:
    harness = build_harness(
        database,
        make_settings(database.url, emoji_enabled=True),
    )
    await _install_real_emoji_effect(database, tmp_path, harness, with_asset=False)
    sender = MemorySender()

    result = await harness.processor.handle(_message("emoji-no-candidate"), sender)

    assert result.sent_messages == 1
    assert [message.text for message in sender.messages] == ["我这边暂时没有可用的表情。"]


@pytest.mark.asyncio
async def test_group_emoji_send_failure_records_only_fallback_text(
    database: Database,
    tmp_path: Path,
) -> None:
    harness = build_harness(
        database,
        make_settings(database.url, emoji_enabled=True),
    )
    _repository, events = await _install_real_emoji_effect(
        database,
        tmp_path,
        harness,
        with_asset=True,
    )
    sender = _MediaFailingSender()

    result = await harness.processor.handle(_message("emoji-send-failure"), sender)

    assert result.sent_messages == 1
    assert sender.calls == 2
    assert [message.text for message in sender.messages] == ["表情没发出去，发送失败了。"]
    recent = await EventLedgerRepository(database).list_recent(
        scope_type=ScopeType.GROUP,
        user_id="1001",
        group_id="2001",
        limit=10,
    )
    outbound = [row for row in recent if row.direction == "outbound"]
    assert len(outbound) == 1
    assert all(segment.get("type") != "image" for segment in outbound[0].segments)
    assert await _usage_count(database) == 0
    assert all(event.name is not EventName.EMOJI_SENT for event in events.events)


@pytest.mark.asyncio
async def test_optional_emoji_transport_failure_keeps_normal_text_reply(
    database: Database,
    tmp_path: Path,
) -> None:
    harness = build_harness(
        database,
        make_settings(database.url, emoji_enabled=True),
    )
    plan = TurnPlan(
        decision=PlannerDecision.REPLY,
        intent="普通文字回复可选附带表情",
        delivery_mode=DeliveryMode.SINGLE,
        desired_messages=1,
        confidence=1,
        reason_code=PlannerReasonCode.DIRECT_REQUEST,
        emoji=EmojiReplyPlan(
            intent=EmojiIntent.NEUTRAL,
            mode=EmojiReplyMode.OPTIONAL,
            placement=EmojiPlacement.AFTER_TEXT,
            goal="自然回应",
        ),
    )
    harness.processor._planner = PlannerService(
        provider=FakePlannerProvider(plan),
        observability=PlannerObservability(),
    )
    _repository, events = await _install_real_emoji_effect(
        database,
        tmp_path,
        harness,
        with_asset=True,
    )
    sender = _MediaFailingSender()
    message = replace(_message("optional-emoji-send-failure"), text="你好")

    result = await harness.processor.handle(message, sender)

    assert result.sent_messages == 1
    assert len(sender.messages) == 1
    assert sender.messages[0].text
    assert not sender.messages[0].media
    assert sender.calls == 2
    assert await _usage_count(database) == 0
    assert all(event.name is not EventName.EMOJI_SENT for event in events.events)
