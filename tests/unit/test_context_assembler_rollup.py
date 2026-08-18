"""ContextAssembler compiles SESSION frontier and uncovered raw without overlap."""

from __future__ import annotations

import asyncio
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
from qq_ai_bot.conversation.history.service import ConversationHistoryService
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
from qq_ai_bot.persistence.repositories import PeopleRepository, RelationshipRepository
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.time.service import TimeContextService

_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
_BOT = "bot-1"
_PEER = "1001"
_IDENTITY = ConversationHistoryIdentity(
    bot_user_id=_BOT,
    scope_type=ScopeType.PRIVATE,
    private_peer_user_id=_PEER,
)


class SpyLedger(EventLedgerRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.list_recent_calls = 0

    async def list_recent(self, **kwargs):  # type: ignore[no-untyped-def]
        self.list_recent_calls += 1
        return await super().list_recent(**kwargs)


async def _seed_events(
    ledger: EventLedgerRepository,
    count: int,
    *,
    start: int = 1,
    occurred_at: datetime = _NOW,
    text: str = "line",
) -> tuple[int, ...]:
    ids: list[int] = []
    for index in range(start, start + count):
        inbound = InboundMessage(
            message_id=f"m-{_PEER}-{index}",
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id=_PEER),
            text=f"{text}-{index} " + ("内容" * 20),
            bot_user_id=_BOT,
            received_at=occurred_at + timedelta(seconds=index),
        )
        record, _created = await ledger.append_inbound(inbound, bot_user_id=_BOT)
        ids.append(record.id)
    return tuple(ids)


async def _commit_cover(
    repository: ConversationHistoryRepository,
    event_ids: tuple[int, ...],
    fingerprint: str,
) -> None:
    state = await repository.get_or_create_state(_IDENTITY)
    await repository.commit_l0_summary(
        state_id=state.id,
        event_ids=event_ids,
        fingerprint=fingerprint,
        mode=HistorySummaryMode.EXTRACTIVE,
        summarizer_version="extractive-v1",
        rendered_text="earlier turns compressed",
        structured_payload_json="{}",
        start_occurred_at=_NOW,
        end_occurred_at=_NOW,
        source_character_count=80,
    )


def _assembler(
    database: Database,
    settings,
    *,
    ledger: EventLedgerRepository | None = None,
    coverage: object | None = None,
    repository: ConversationHistoryRepository | None = None,
) -> ContextAssembler:
    people = PeopleRepository(database)
    memories = MemoryFactService(MemoryFactRepository(database))
    return ContextAssembler(
        settings=settings,
        ledger=ledger or EventLedgerRepository(database),
        people=people,
        memory_context=MemoryContextService(
            query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
            retriever=MemoryRetriever(
                repository=MemoryFactRepository(database),
                lexical_index=SQLiteMemoryFTSIndex(database),
            ),
            facts=memories,
        ),
        relationships=RelationshipRepository(database),
        time_service=TimeContextService(database),
        history_repository=repository or ConversationHistoryRepository(database),
        history_coverage=coverage,  # type: ignore[arg-type]
    )


async def _assemble(assembler: ContextAssembler, database: Database, settings, message_id: str):
    runtime = await RuntimeConfigService(settings=settings, database=database).snapshot()
    inbound = InboundMessage(
        message_id=message_id,
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=_PEER),
        text="当前消息",
        bot_user_id=_BOT,
    )
    return await assembler.assemble(
        inbound=inbound,
        identity=ConversationIdentity.private(_PEER),
        profile=UserProfileSnapshot(user_id=_PEER, scope_type=ScopeType.PRIVATE, nickname="用户"),
        content="当前消息",
        runtime=runtime,
        memory_retrieval=empty_retrieval(),
        persist_memory_exposure=False,
    )


@pytest.mark.asyncio
async def test_covered_raw_starts_after_frontier_and_skips_list_recent(
    database: Database,
) -> None:
    settings = make_settings(database.url, conversation_history_rollup_enabled=True)
    ledger = SpyLedger(database)
    ids = await _seed_events(ledger, 8)
    covered, uncovered = ids[:5], ids[5:]
    await _commit_cover(ConversationHistoryRepository(database), covered, "fp-cover")
    assembler = _assembler(database, settings, ledger=ledger)
    context = await _assemble(assembler, database, settings, f"m-{_PEER}-8")

    assert context.session_text
    assert "get_chat_history_around" in context.session_text
    assert context.metrics.covered_to == covered[-1]
    assert context.metrics.rollup_characters == len(context.session_text)
    assert min(context.visible_event_ids) > covered[-1]
    assert set(covered).isdisjoint(context.visible_event_ids)
    assert uncovered[-1] in context.visible_event_ids
    assert "conversation_summary" not in json.dumps(context.metadata_payload, ensure_ascii=False)
    remainder = settings.max_context_characters - context.metrics.metadata_characters
    assert context.metrics.history_characters + context.metrics.rollup_characters <= remainder
    assert ledger.list_recent_calls == 0


@pytest.mark.asyncio
async def test_no_coverage_does_not_shorten_near_window(database: Database) -> None:
    settings = make_settings(
        database.url,
        conversation_history_rollup_enabled=True,
        max_context_characters=700,
    )
    ledger = EventLedgerRepository(database)
    ids = await _seed_events(ledger, 12, text="很长的历史")
    assembler = _assembler(database, settings, ledger=ledger)
    context = await _assemble(assembler, database, settings, f"m-{_PEER}-12")
    assert context.metrics.covered_to is None
    assert context.session_text == ""
    assert not context.metrics.raw_history_window_shifted
    assert ids[0] in context.visible_event_ids


@pytest.mark.asyncio
async def test_extractive_coverage_then_allows_shift(database: Database) -> None:
    settings = make_settings(
        database.url,
        conversation_history_rollup_enabled=True,
        max_context_characters=700,
        conversation_history_rollup_l0_min_events=2,
        conversation_history_rollup_l0_min_characters=20,
        conversation_history_raw_tail_events=4,
        conversation_history_raw_tail_characters=200,
    )
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 12, text="很长的历史")
    coverage = ConversationHistoryService(
        settings=settings,
        repository=repository,
        ledger=ledger,
    )
    assembler = _assembler(
        database, settings, ledger=ledger, coverage=coverage, repository=repository
    )
    context = await _assemble(assembler, database, settings, f"m-{_PEER}-12")
    assert context.metrics.covered_to is not None
    assert context.session_text
    assert min(context.visible_event_ids) > context.metrics.covered_to
    assert ids[0] not in context.visible_event_ids


@pytest.mark.asyncio
async def test_frontier_change_refreshes_cache_key(database: Database) -> None:
    settings = make_settings(database.url, conversation_history_rollup_enabled=True)
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 10)
    await _commit_cover(repository, ids[:4], "fp-a")
    assembler = _assembler(database, settings, ledger=ledger, repository=repository)
    first = await _assemble(assembler, database, settings, f"m-{_PEER}-10")
    await _commit_cover(repository, ids[4:7], "fp-b")
    second = await _assemble(assembler, database, settings, f"m-{_PEER}-10")
    assert first.prompt_cache_key != second.prompt_cache_key
    assert first.session_text != second.session_text
    third = await _assemble(assembler, database, settings, f"m-{_PEER}-10")
    assert third.prompt_cache_key == second.prompt_cache_key
    assert third.session_text == second.session_text


@pytest.mark.asyncio
async def test_reset_drops_old_summaries(database: Database) -> None:
    settings = make_settings(database.url, conversation_history_rollup_enabled=True)
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 6)
    await _commit_cover(repository, ids[:4], "fp-old")
    assembler = _assembler(database, settings, ledger=ledger, repository=repository)
    before = await _assemble(assembler, database, settings, f"m-{_PEER}-6")
    assert before.session_text
    await ledger.set_context_reset(ConversationIdentity.private(_PEER))
    later = datetime.now(UTC) + timedelta(seconds=30)
    await _seed_events(ledger, 2, start=20, occurred_at=later)
    after = await _assemble(assembler, database, settings, f"m-{_PEER}-21")
    assert after.session_text == ""
    assert after.metrics.covered_to is None


@pytest.mark.asyncio
async def test_disabled_rollup_keeps_raw_window_and_no_session(database: Database) -> None:
    settings = make_settings(database.url, conversation_history_rollup_enabled=False)
    ledger = SpyLedger(database)
    ids = await _seed_events(ledger, 6)
    await _commit_cover(ConversationHistoryRepository(database), ids[:3], "fp-off")
    assembler = _assembler(database, settings, ledger=ledger)
    context = await _assemble(assembler, database, settings, f"m-{_PEER}-6")
    assert context.session_text == ""
    assert context.conversation_summary is None
    assert ids[0] in context.visible_event_ids
    assert ledger.list_recent_calls >= 1


@pytest.mark.asyncio
async def test_snapshot_has_no_summary_raw_overlap_under_concurrent_commit(
    database: Database,
) -> None:
    repository = ConversationHistoryRepository(database)
    ledger = EventLedgerRepository(database)
    ids = await _seed_events(ledger, 8)

    async def commit() -> None:
        await _commit_cover(repository, ids[:5], "fp-race")

    async def read() -> None:
        await repository.load_prompt_snapshot(_IDENTITY, recent_limit=20)

    await asyncio.gather(commit(), read())
    snapshot = await repository.load_prompt_snapshot(_IDENTITY, recent_limit=20)
    recent_ids = {event.id for event in snapshot.recent_events if hasattr(event, "id")}
    assert snapshot.coverage_end_event_id == 0 or min(recent_ids, default=10**9) > (
        snapshot.coverage_end_event_id
    )


@pytest.mark.asyncio
async def test_assemble_external_uses_session_budget(database: Database) -> None:
    settings = make_settings(database.url, conversation_history_rollup_enabled=True)
    ledger = EventLedgerRepository(database)
    ids = await _seed_events(ledger, 6)
    await _commit_cover(ConversationHistoryRepository(database), ids[:3], "fp-ext")
    inbound = InboundMessage(
        message_id="ext-1",
        event_type="external_event",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=_PEER),
        text="插件事件",
        bot_user_id=_BOT,
        received_at=_NOW + timedelta(seconds=80),
    )
    event, _created = await ledger.append_inbound(inbound, bot_user_id=_BOT)
    assembler = _assembler(database, settings, ledger=ledger)
    runtime = await RuntimeConfigService(settings=settings, database=database).snapshot()
    context = await assembler.assemble_external(
        event=event,
        authorization_user_id=_PEER,
        runtime=runtime,
        agent_intent="notify",
    )
    assert context.session_text
    assert context.metrics.rollup_characters == len(context.session_text)
