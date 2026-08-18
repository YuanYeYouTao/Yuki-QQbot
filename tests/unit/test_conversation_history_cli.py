"""CLI inspection, rebuild, invalidation, and reconcile for conversation history."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.capabilities.provider import _CORE_METADATA
from qq_ai_bot.cli import _add_history_rollup_parser, _history_rollup_command
from qq_ai_bot.conversation.history.errors import HistoryIdentityError, HistoryJobConflictError
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    HistoryJobKind,
    HistorySummaryMode,
)
from qq_ai_bot.conversation.history.operations import (
    ConversationHistoryOperations,
    parse_history_identity,
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
from qq_ai_bot.persistence.repositories import PeopleRepository, RelationshipRepository
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.time.service import TimeContextService

_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
_BOT = "bot-1"
_PEER = "1001"
_SECRET = "UNIQUE_HISTORY_BODY_TOKEN_9f3a"
_IDENTITY = ConversationHistoryIdentity(
    bot_user_id=_BOT,
    scope_type=ScopeType.PRIVATE,
    private_peer_user_id=_PEER,
)


def _settings(database: Database, **overrides: object):
    values: dict[str, object] = {
        "conversation_history_rollup_enabled": True,
        "conversation_history_rollup_l0_min_events": 2,
        "conversation_history_rollup_l0_min_characters": 20,
        "conversation_history_raw_tail_events": 4,
        "conversation_history_raw_tail_characters": 200,
        "conversation_history_rollup_l0_max_events": 8,
        "conversation_history_rollup_l0_max_characters": 4000,
    }
    values.update(overrides)
    return make_settings(database.url, **values)


def _ops(database: Database, settings) -> ConversationHistoryOperations:
    return ConversationHistoryOperations(
        settings=settings,
        repository=ConversationHistoryRepository(database),
        ledger=EventLedgerRepository(database),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qq-ai-bot-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_history_rollup_parser(subparsers)
    return parser


def _identity_args() -> list[str]:
    return ["--bot-user-id", _BOT, "--scope", "private", "--user-id", _PEER]


async def _seed_events(
    ledger: EventLedgerRepository,
    count: int,
    *,
    peer: str = _PEER,
    start: int = 1,
    text: str = "line",
) -> tuple[int, ...]:
    ids: list[int] = []
    for index in range(start, start + count):
        inbound = InboundMessage(
            message_id=f"m-{peer}-{index}",
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id=peer),
            text=f"{text}-{index} " + ("内容" * 20),
            bot_user_id=_BOT,
            received_at=_NOW + timedelta(seconds=index),
        )
        record, _created = await ledger.append_inbound(inbound, bot_user_id=_BOT)
        ids.append(record.id)
    return tuple(ids)


async def _commit_cover(
    repository: ConversationHistoryRepository,
    event_ids: tuple[int, ...],
    fingerprint: str,
    *,
    rendered_text: str = "earlier turns compressed",
) -> None:
    state = await repository.get_or_create_state(_IDENTITY)
    await repository.commit_l0_summary(
        state_id=state.id,
        event_ids=event_ids,
        fingerprint=fingerprint,
        mode=HistorySummaryMode.EXTRACTIVE,
        summarizer_version="extractive-v1",
        rendered_text=rendered_text,
        structured_payload_json="{}",
        start_occurred_at=_NOW,
        end_occurred_at=_NOW,
        source_character_count=80,
    )


def _assembler(database: Database, settings) -> ContextAssembler:
    people = PeopleRepository(database)
    memories = MemoryFactService(MemoryFactRepository(database))
    return ContextAssembler(
        settings=settings,
        ledger=EventLedgerRepository(database),
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
        history_repository=ConversationHistoryRepository(database),
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


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def test_parser_requires_exact_identity_for_invalidate() -> None:
    parser = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["history-rollup", "invalidate"])


def test_fuzzy_private_identity_is_rejected() -> None:
    with pytest.raises(HistoryIdentityError):
        parse_history_identity(
            bot_user_id=_BOT,
            scope="private",
            user_id=None,
            group_id=None,
            reset_at=None,
        )
    with pytest.raises(HistoryIdentityError):
        parse_history_identity(
            bot_user_id=_BOT,
            scope="private",
            user_id=_PEER,
            group_id="2001",
            reset_at=None,
        )


def test_agent_tools_do_not_include_history_rollup_mutation() -> None:
    names = frozenset(_CORE_METADATA)
    assert "get_chat_history_around" in names
    assert "invalidate_conversation_history" not in names
    assert "rebuild_conversation_history" not in names
    assert not any("history_rollup" in name or "history-rollup" in name for name in names)


@pytest.mark.asyncio
async def test_dry_run_rebuild_does_not_write(database: Database) -> None:
    settings = _settings(database)
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 12, text=_SECRET)
    await _commit_cover(repository, ids[:4], "fp-cover", rendered_text=_SECRET)
    before = await repository.list_summaries((await repository.get_or_create_state(_IDENTITY)).id)
    ops = _ops(database, settings)
    payload = await ops.rebuild(_IDENTITY, commit=False)
    state = await repository.get_state(_IDENTITY)
    assert state is not None
    after = await repository.list_summaries(state.id)
    assert payload["dry_run"] is True
    assert payload["writes"] is False
    assert payload["planned_l0_slices"]
    assert len(after) == len(before)
    assert {item.status.value for item in after} == {item.status.value for item in before}
    assert _SECRET not in _dump(payload)


@pytest.mark.asyncio
async def test_rebuild_is_idempotent(database: Database) -> None:
    settings = _settings(database)
    ledger = EventLedgerRepository(database)
    await _seed_events(ledger, 16, text="很长的历史")
    ops = _ops(database, settings)
    first = await ops.rebuild(_IDENTITY, commit=True)
    second = await ops.rebuild(_IDENTITY, commit=True)
    assert first["writes"] is True
    assert first["created_l0_summaries"] >= 1
    assert second["created_l0_summaries"] == first["created_l0_summaries"]
    assert second["source_fingerprints"] == first["source_fingerprints"]
    assert second["coverage_end_event_id"] == first["coverage_end_event_id"]
    assert second["coverage_end_event_id"] == first["planned_coverage_end"]


@pytest.mark.asyncio
async def test_invalidate_returns_context_to_raw_history(database: Database) -> None:
    settings = _settings(database)
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 8)
    await _commit_cover(repository, ids[:5], "fp-cover")
    assembler = _assembler(database, settings)
    before = await _assemble(assembler, database, settings, f"m-{_PEER}-8")
    assert before.session_text
    assert ids[0] not in before.visible_event_ids
    payload = await _ops(database, settings).invalidate(_IDENTITY)
    assert payload["invalidated_summaries"] >= 1
    assert payload["coverage_end_event_id"] == 0
    after = await _assemble(assembler, database, settings, f"m-{_PEER}-8")
    assert after.session_text == ""
    assert after.metrics.covered_to is None
    assert ids[0] in after.visible_event_ids


@pytest.mark.asyncio
async def test_reconcile_repairs_counters(database: Database) -> None:
    settings = _settings(database)
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 6)
    state = await repository.get_or_create_state(_IDENTITY)
    async with database.sessions() as session, session.begin():
        from qq_ai_bot.conversation.history.db_models import ConversationHistoryStateModel

        row = await session.get(ConversationHistoryStateModel, state.id)
        assert row is not None
        row.last_seen_event_id = 0
        row.pending_event_count = 0
        row.pending_character_count = 0
    payload = await _ops(database, settings).reconcile(_IDENTITY)
    repaired = payload["state"]
    assert repaired["last_seen_event_id"] == ids[-1]
    assert repaired["pending_event_count"] == len(ids)
    assert repaired["pending_character_count"] > 0


@pytest.mark.asyncio
async def test_rebuild_refuses_live_worker_lease(database: Database) -> None:
    settings = _settings(database)
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    await _seed_events(ledger, 8)
    state = await repository.get_or_create_state(_IDENTITY)
    await repository.enqueue_job(
        state_id=state.id,
        job_kind=HistoryJobKind.RAW_RANGE,
        source_level=0,
        source_start_id=1,
        source_end_id=2,
        source_fingerprint="abc",
        summarizer_version="v1",
    )
    claimed = await repository.claim_next_job(lease_owner="worker-1", lease_seconds=180)
    assert claimed is not None
    with pytest.raises(HistoryJobConflictError):
        await _ops(database, settings).rebuild(_IDENTITY, commit=True)


@pytest.mark.asyncio
async def test_cli_status_and_inspect_are_redacted(
    database: Database,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(database)
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    ids = await _seed_events(ledger, 6, text=_SECRET)
    await _commit_cover(repository, ids[:3], "fp-secret", rendered_text=_SECRET)
    args = _parser().parse_args(["history-rollup", "inspect", *_identity_args()])
    caplog.set_level(logging.INFO, logger="qq_ai_bot.conversation.history.operations")
    code = await _history_rollup_command(settings, args)
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    text = captured.out + captured.err + caplog.text
    assert _SECRET not in text
    assert "rendered_text" not in captured.out
    assert payload["frontier"][0]["member_event_ids"]
    assert payload["frontier"][0]["rendered_characters"] > 0
    status_args = _parser().parse_args(["history-rollup", "status"])
    status_code = await _history_rollup_command(settings, status_args)
    status_out = capsys.readouterr()
    assert status_code == 0
    assert _SECRET not in status_out.out
    assert json.loads(status_out.out)["health"]["ok"] is True
