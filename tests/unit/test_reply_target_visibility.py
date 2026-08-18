"""Reply targets stay on this-turn visible raw events, never summary coverage."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    HistorySummaryMode,
)
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.runtime.turn_session import empty_retrieval
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    PeopleRepository,
    RelationshipRepository,
)
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.services.reply_target import ReplyTargetControl
from qq_ai_bot.time.service import TimeContextService

_NOW = datetime(2026, 8, 19, 13, 0, tzinfo=UTC)
_BOT = "bot-1"
_PEER = "1001"


async def _seed(ledger: EventLedgerRepository, count: int) -> tuple[int, ...]:
    ids: list[int] = []
    for index in range(1, count + 1):
        inbound = InboundMessage(
            message_id=f"m-{_PEER}-{index}",
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id=_PEER),
            text=f"原文-{index}",
            bot_user_id=_BOT,
            received_at=_NOW + timedelta(seconds=index),
        )
        record, _created = await ledger.append_inbound(inbound, bot_user_id=_BOT)
        ids.append(record.id)
    return tuple(ids)


@pytest.mark.asyncio
async def test_covered_events_cannot_be_reply_targets(database: Database) -> None:
    settings = make_settings(database.url, conversation_history_rollup_enabled=True)
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed(ledger, 6)
    covered = ids[:3]
    state = await repository.get_or_create_state(
        ConversationHistoryIdentity(
            bot_user_id=_BOT,
            scope_type=ScopeType.PRIVATE,
            private_peer_user_id=_PEER,
        )
    )
    await repository.commit_l0_summary(
        state_id=state.id,
        event_ids=covered,
        fingerprint="fp-reply",
        mode=HistorySummaryMode.EXTRACTIVE,
        summarizer_version="extractive-v1",
        rendered_text="covered",
        structured_payload_json="{}",
        start_occurred_at=_NOW,
        end_occurred_at=_NOW,
        source_character_count=30,
    )
    people = PeopleRepository(database)
    assembler = ContextAssembler(
        settings=settings,
        ledger=ledger,
        people=people,
        memory_context=MemoryContextService(
            query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
            retriever=MemoryRetriever(
                repository=MemoryFactRepository(database),
                lexical_index=SQLiteMemoryFTSIndex(database),
            ),
            facts=MemoryFactService(MemoryFactRepository(database)),
        ),
        relationships=RelationshipRepository(database),
        time_service=TimeContextService(database),
        history_repository=repository,
    )
    runtime = await RuntimeConfigService(settings=settings, database=database).snapshot()
    context = await assembler.assemble(
        inbound=InboundMessage(
            message_id=f"m-{_PEER}-6",
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id=_PEER),
            text="当前消息",
            bot_user_id=_BOT,
        ),
        identity=ConversationIdentity.private(_PEER),
        profile=UserProfileSnapshot(user_id=_PEER, scope_type=ScopeType.PRIVATE),
        content="当前消息",
        runtime=runtime,
        memory_retrieval=empty_retrieval(),
        persist_memory_exposure=False,
    )
    control = ReplyTargetControl(visible_event_ids=context.visible_event_ids)
    ok, reason = control.apply(covered[0])
    assert ok is False
    assert reason == "event_not_visible"
    ok, reason = control.apply(ids[-1])
    assert ok is True
    assert reason == "selected"


@pytest.mark.asyncio
async def test_around_reads_covered_originals_but_ids_stay_unquotable(
    database: Database,
) -> None:
    settings = make_settings(database.url, conversation_history_rollup_enabled=True)
    ledger = EventLedgerRepository(database)
    ids = await _seed(ledger, 8)
    covered = ids[:4]
    repository = ConversationHistoryRepository(database)
    state = await repository.get_or_create_state(
        ConversationHistoryIdentity(
            bot_user_id=_BOT,
            scope_type=ScopeType.PRIVATE,
            private_peer_user_id=_PEER,
        )
    )
    await repository.commit_l0_summary(
        state_id=state.id,
        event_ids=covered,
        fingerprint="fp-around",
        mode=HistorySummaryMode.EXTRACTIVE,
        summarizer_version="extractive-v1",
        rendered_text="covered",
        structured_payload_json="{}",
        start_occurred_at=_NOW,
        end_occurred_at=_NOW,
        source_character_count=40,
    )
    tools = AgentToolService(
        settings=settings,
        ledger=ledger,
        memories=MemoryFactService(MemoryFactRepository(database)),
        actions=AgentActionRepository(database),
    )
    inbound = InboundMessage(
        message_id=f"m-{_PEER}-8",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=_PEER),
        text="对齐原话",
        bot_user_id=_BOT,
    )
    payload = json.loads(
        await tools.execute(
            "get_chat_history_around",
            json.dumps({"event_id": covered[1], "before": 1, "after": 1}),
            ToolRuntime(inbound, None, False),
        )
    )
    assert payload["ok"] is True
    events = payload["data"]["events"]
    assert any(item["id"] == covered[1] and "原文-2" in item["content"] for item in events)
    around_ids = {item["id"] for item in events}
    people = PeopleRepository(database)
    assembler = ContextAssembler(
        settings=settings,
        ledger=ledger,
        people=people,
        memory_context=MemoryContextService(
            query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
            retriever=MemoryRetriever(
                repository=MemoryFactRepository(database),
                lexical_index=SQLiteMemoryFTSIndex(database),
            ),
            facts=MemoryFactService(MemoryFactRepository(database)),
        ),
        relationships=RelationshipRepository(database),
        time_service=TimeContextService(database),
        history_repository=repository,
    )
    runtime = await RuntimeConfigService(settings=settings, database=database).snapshot()
    context = await assembler.assemble(
        inbound=inbound,
        identity=ConversationIdentity.private(_PEER),
        profile=UserProfileSnapshot(user_id=_PEER, scope_type=ScopeType.PRIVATE),
        content="对齐原话",
        runtime=runtime,
        memory_retrieval=empty_retrieval(),
        persist_memory_exposure=False,
    )
    control = ReplyTargetControl(visible_event_ids=context.visible_event_ids)
    for event_id in around_ids:
        if event_id not in context.visible_event_ids:
            ok, reason = control.apply(event_id)
            assert ok is False
            assert reason == "event_not_visible"
            break
    else:
        raise AssertionError("around should retrieve at least one covered id")
