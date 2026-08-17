"""MessageProcessor turn observation wiring (R1 commit 3).

The handle() wrapper must bind one fresh correlation per admitted message,
record a content-free observation row only for turns that engaged a
persistence write point (or failed unexpectedly), and never let observation
storage problems change the turn outcome.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import RuntimeTurnObservationModel
from qq_ai_bot.runtime.observability import claim_runtime_turn_id
from qq_ai_bot.services.processor import ProcessResult


def _private(message_id: str, text: str = "你好") -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text=text,
        bot_user_id="8000",
    )


async def _observation_rows(database) -> list[RuntimeTurnObservationModel]:
    async with database.sessions() as session:
        return list(await session.scalars(select(RuntimeTurnObservationModel)))


@pytest.mark.asyncio
async def test_turn_touching_a_write_point_records_one_row(database) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, FakeLLMProvider(lambda _request: "回复"))
    processor = harness.processor

    async def touching_turn(message, sender, profile_resolver=None) -> ProcessResult:
        assert claim_runtime_turn_id() is not None
        return ProcessResult(True, 2, "chat")

    processor._handle_admitted = touching_turn

    result = await processor.handle(_private("obs-touch"), MemorySender())

    assert result == ProcessResult(True, 2, "chat")
    rows = await _observation_rows(database)
    assert len(rows) == 1
    row = rows[0]
    assert row.origin == "user_message"
    assert row.scope_type == "private"
    assert row.admission_outcome == "chat"
    assert row.handled is True
    assert row.sent_messages == 2
    assert row.error_category is None
    assert "1001" not in (row.conversation_key_hash or "")


@pytest.mark.asyncio
async def test_untouched_turn_stays_silent(database) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, FakeLLMProvider(lambda _request: "回复"))
    processor = harness.processor

    async def command_like_turn(message, sender, profile_resolver=None) -> ProcessResult:
        return ProcessResult(True, 1, "command")

    processor._handle_admitted = command_like_turn

    await processor.handle(_private("obs-silent"), MemorySender())

    assert await _observation_rows(database) == []


@pytest.mark.asyncio
async def test_unexpected_failure_records_error_category_and_reraises(database) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, FakeLLMProvider(lambda _request: "回复"))
    processor = harness.processor

    async def exploding_turn(message, sender, profile_resolver=None) -> ProcessResult:
        raise ValueError("boom")

    processor._handle_admitted = exploding_turn

    with pytest.raises(ValueError, match="boom"):
        await processor.handle(_private("obs-error"), MemorySender())

    rows = await _observation_rows(database)
    assert len(rows) == 1
    assert rows[0].handled is False
    assert rows[0].error_category == "ValueError"
    assert rows[0].admission_outcome is None


@pytest.mark.asyncio
async def test_two_turns_get_distinct_runtime_turn_ids(database) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, FakeLLMProvider(lambda _request: "回复"))
    processor = harness.processor

    async def touching_turn(message, sender, profile_resolver=None) -> ProcessResult:
        claim_runtime_turn_id()
        return ProcessResult(True, 1, "chat")

    processor._handle_admitted = touching_turn

    await processor.handle(_private("obs-a"), MemorySender())
    await processor.handle(_private("obs-b"), MemorySender())

    rows = await _observation_rows(database)
    assert len(rows) == 2
    assert rows[0].runtime_turn_id != rows[1].runtime_turn_id
