from __future__ import annotations

import asyncio

import pytest
from tests.conftest import MemorySender, make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    OutboundMedia,
    OutboundMessage,
    OutboundSendReceipt,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.planner.models import (
    DeliveryMode,
    PlannerDecision,
    PlannerReasonCode,
    TurnPlan,
)
from qq_ai_bot.services.reply_sequence import (
    DeliveryFailureRecovery,
    ReplySequenceManager,
)
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    ReplySequenceCancelled,
)


def _plan(mode: DeliveryMode, messages: int = 1) -> TurnPlan:
    return TurnPlan(
        decision=PlannerDecision.REPLY,
        intent="test",
        delivery_mode=mode,
        desired_messages=messages,
        confidence=1.0,
        reason_code=PlannerReasonCode.DIRECT_REQUEST,
    )


def test_structured_code_blocks_are_reopened_when_qq_limit_requires_split() -> None:
    assert _plan(DeliveryMode.STRUCTURED).delivery_mode is DeliveryMode.STRUCTURED
    block = "```python\n" + "\n".join(f"print({index})" for index in range(30)) + "\n```"
    chunks = ReplySequenceManager._split_preserving_structure(block, limit=80)
    assert len(chunks) > 1
    assert all(chunk.startswith("```python\n") and chunk.endswith("\n```") for chunk in chunks)


@pytest.mark.asyncio
async def test_blank_line_splits_even_a_single_mode_chat_reply(database: Database) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    manager = ReplySequenceManager(ConversationTurnCoordinator())

    assert manager.render(
        "先说第一件事。\n\n然后说第二件事。",
        plan=_plan(DeliveryMode.SINGLE),
        runtime=runtime,
    ) == ("先说第一件事。", "然后说第二件事。")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    (DeliveryMode.SINGLE, DeliveryMode.CONCISE, DeliveryMode.NATURAL_MULTI),
)
async def test_each_plain_chat_line_becomes_one_qq_message(
    database: Database,
    mode: DeliveryMode,
) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    manager = ReplySequenceManager(ConversationTurnCoordinator())

    assert manager.render(
        "first line\nsecond line",
        plan=_plan(mode, messages=3),
        runtime=runtime,
    ) == ("first line", "second line")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", (DeliveryMode.STRUCTURED, DeliveryMode.DETAILED))
async def test_formatted_modes_preserve_internal_lines(
    database: Database,
    mode: DeliveryMode,
) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    manager = ReplySequenceManager(ConversationTurnCoordinator())
    text = "说明：\n\n```python\nprint('one')\n\nprint('two')\n```"

    assert manager.render(
        text,
        plan=_plan(mode),
        runtime=runtime,
    ) == (text,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "items:\n- first\n- second",
        "```python\nprint('one')\nprint('two')\n```",
        "| name | value |\n| --- | --- |\n| one | two |",
    ),
)
async def test_structured_content_guard_preserves_lines_after_chat_mode_misclassification(
    database: Database,
    text: str,
) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    manager = ReplySequenceManager(ConversationTurnCoordinator())

    assert manager.render(
        text,
        plan=_plan(DeliveryMode.SINGLE),
        runtime=runtime,
    ) == (text,)


@pytest.mark.asyncio
async def test_natural_multi_does_not_split_punctuation_without_model_line_break(
    database: Database,
) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    manager = ReplySequenceManager(ConversationTurnCoordinator())
    text = "first sentence! second sentence?"

    assert manager.render(
        text,
        plan=_plan(DeliveryMode.NATURAL_MULTI, messages=3),
        runtime=runtime,
    ) == (text,)


@pytest.mark.asyncio
async def test_excess_blank_sections_merge_without_recreating_empty_line(
    database: Database,
) -> None:
    runtime_service = RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    )
    await runtime_service.set_override(
        "reply.plan_hard_max_messages",
        2,
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="blank-line-cap",
    )
    runtime = await runtime_service.snapshot()
    manager = ReplySequenceManager(ConversationTurnCoordinator())

    chunks = manager.render(
        "第一段\n\n第二段\n\n第三段",
        plan=_plan(DeliveryMode.SINGLE),
        runtime=runtime,
    )

    assert chunks == ("第一段\n第二段", "第三段")
    assert all("\n\n" not in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_only_first_message_in_sequence_quotes_planner_target(database: Database) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("group:2001", TurnOrigin.USER_MESSAGE)
    manager = ReplySequenceManager(coordinator)
    sender = MemorySender()
    recorded: list[OutboundMessage] = []

    async def record(message: OutboundMessage, _receipt: OutboundSendReceipt) -> None:
        recorded.append(message)

    result = await manager.send(
        text="第一条。\n\n第二条。",
        plan=_plan(DeliveryMode.NATURAL_MULTI, messages=2),
        runtime=runtime,
        token=token,
        sender=sender,
        record_outbound=record,
        reply_to_message_id="12345",
    )

    assert result.sent_messages == 2
    assert [message.reply_to_message_id for message in sender.messages] == ["12345", None]
    assert recorded == sender.messages


@pytest.mark.asyncio
async def test_first_actual_media_message_receives_the_quote(database: Database) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("group:2001", TurnOrigin.USER_MESSAGE)
    manager = ReplySequenceManager(coordinator)
    sender = MemorySender()

    result = await manager.send(
        text="随后发送的正文",
        plan=_plan(DeliveryMode.SINGLE),
        runtime=runtime,
        token=token,
        sender=sender,
        record_outbound=lambda *_args: asyncio.sleep(0),
        before_messages=(_emoji_message(),),
        reply_to_message_id="12345",
    )

    assert result.sent_messages == 2
    assert sender.messages[0].media
    assert [message.reply_to_message_id for message in sender.messages] == ["12345", None]


class _QuoteFailingSender:
    def __init__(self) -> None:
        self.attempts: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        self.attempts.append(message)
        if message.reply_to_message_id is not None:
            raise RuntimeError("quoted message is unavailable")
        return OutboundSendReceipt(platform_message_id="sent-without-quote")


@pytest.mark.asyncio
async def test_quote_failure_retries_once_without_quote(database: Database) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("group:2001", TurnOrigin.USER_MESSAGE)
    manager = ReplySequenceManager(coordinator)
    sender = _QuoteFailingSender()
    recorded: list[OutboundMessage] = []

    async def record(message: OutboundMessage, _receipt: OutboundSendReceipt) -> None:
        recorded.append(message)

    result = await manager.send(
        text="正文不能丢",
        plan=_plan(DeliveryMode.SINGLE),
        runtime=runtime,
        token=token,
        sender=sender,
        record_outbound=record,
        reply_to_message_id="12345",
    )

    assert result.sent_messages == 1
    assert len(sender.attempts) == 2
    assert sender.attempts[0].reply_to_message_id == "12345"
    assert sender.attempts[1].reply_to_message_id is None
    assert recorded == [sender.attempts[1]]


class _MediaFailingSender:
    def __init__(self, *, fail_text: bool = False) -> None:
        self.attempts: list[OutboundMessage] = []
        self.fail_text = fail_text

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        self.attempts.append(message)
        if message.media or (self.fail_text and message.text):
            raise RuntimeError("transport failed")
        return OutboundSendReceipt(platform_message_id=f"sent-{len(self.attempts)}")


def _emoji_message() -> OutboundMessage:
    return OutboundMessage(
        media=(
            OutboundMedia(
                kind=AttachmentKind.IMAGE,
                content=b"GIF89a",
                mime_type="image/gif",
                emoji_id="emoji-one",
            ),
        )
    )


@pytest.mark.asyncio
async def test_media_failure_recovery_sends_replacement_and_continues_text(
    database: Database,
) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("private:1001", TurnOrigin.USER_MESSAGE)
    manager = ReplySequenceManager(coordinator, random_uniform=lambda _low, _high: 0)
    sender = _MediaFailingSender()
    recorded: list[str] = []
    failures: list[OutboundMessage] = []

    async def record(message: OutboundMessage, _receipt: OutboundSendReceipt) -> None:
        recorded.append(message.text)

    async def record_failure(message: OutboundMessage, _error: Exception) -> None:
        failures.append(message)

    async def recover(
        _message: OutboundMessage,
        _error: Exception,
    ) -> DeliveryFailureRecovery:
        return DeliveryFailureRecovery(
            handled=True,
            replacement_messages=(OutboundMessage(text="表情没发出去，先用文字回你。"),),
        )

    result = await manager.send(
        text="正文仍然发送",
        plan=_plan(DeliveryMode.SINGLE),
        runtime=runtime,
        token=token,
        sender=sender,
        record_outbound=record,
        record_failure=record_failure,
        recover_failure=recover,
        before_messages=(_emoji_message(),),
    )

    assert result.sent_messages == 2
    assert len(failures) == 1
    assert sum(bool(item.media) for item in sender.attempts) == 1
    assert recorded == ["表情没发出去，先用文字回你。", "正文仍然发送"]


@pytest.mark.asyncio
async def test_optional_media_failure_can_be_handled_without_extra_text(
    database: Database,
) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("private:1001", TurnOrigin.USER_MESSAGE)
    manager = ReplySequenceManager(coordinator, random_uniform=lambda _low, _high: 0)
    sender = _MediaFailingSender()
    recorded: list[str] = []

    async def record(message: OutboundMessage, _receipt: OutboundSendReceipt) -> None:
        recorded.append(message.text)

    async def recover(
        _message: OutboundMessage,
        _error: Exception,
    ) -> DeliveryFailureRecovery:
        return DeliveryFailureRecovery(handled=True)

    result = await manager.send(
        text="正文",
        plan=_plan(DeliveryMode.SINGLE),
        runtime=runtime,
        token=token,
        sender=sender,
        record_outbound=record,
        recover_failure=recover,
        after_messages=(_emoji_message(),),
    )

    assert result.sent_messages == 1
    assert recorded == ["正文"]
    assert sum(bool(item.media) for item in sender.attempts) == 1


@pytest.mark.asyncio
async def test_post_send_record_failure_does_not_resend_transport(database: Database) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("private:1001", TurnOrigin.USER_MESSAGE)
    manager = ReplySequenceManager(coordinator, random_uniform=lambda _low, _high: 0)
    sender = MemorySender()

    async def record(
        _message: OutboundMessage,
        _receipt: OutboundSendReceipt,
    ) -> None:
        raise RuntimeError("ledger failed")

    result = await manager.send(
        text="只发送一次",
        plan=_plan(DeliveryMode.SINGLE),
        runtime=runtime,
        token=token,
        sender=sender,
        record_outbound=record,
    )

    assert result.sent_messages == 1
    assert len(sender.messages) == 1


@pytest.mark.asyncio
async def test_replacement_failure_propagates_without_retry(database: Database) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("private:1001", TurnOrigin.USER_MESSAGE)
    manager = ReplySequenceManager(coordinator, random_uniform=lambda _low, _high: 0)
    sender = _MediaFailingSender(fail_text=True)

    async def record(_message: OutboundMessage, _receipt: OutboundSendReceipt) -> None:
        return None

    async def recover(
        _message: OutboundMessage,
        _error: Exception,
    ) -> DeliveryFailureRecovery:
        return DeliveryFailureRecovery(
            handled=True,
            replacement_messages=(OutboundMessage(text="fallback"),),
        )

    with pytest.raises(RuntimeError, match="transport failed"):
        await manager.send(
            text="",
            plan=_plan(DeliveryMode.SINGLE),
            runtime=runtime,
            token=token,
            sender=sender,
            record_outbound=record,
            recover_failure=recover,
            before_messages=(_emoji_message(),),
            suppress_text=True,
        )
    assert len(sender.attempts) == 2


async def test_new_message_stops_unsent_reply_chunks() -> None:
    # Runtime snapshot construction is covered through RuntimeConfigService in
    # integration tests; this unit test focuses on coordinator cancellation.
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("group:1", TurnOrigin.USER_MESSAGE)
    entered = asyncio.Event()

    async def tracked() -> None:
        with pytest.raises(ReplySequenceCancelled):
            async with coordinator.track(token, "reply"):
                entered.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(tracked())
    await entered.wait()
    await coordinator.notify_message("group:1", TurnOrigin.USER_MESSAGE)
    await task
