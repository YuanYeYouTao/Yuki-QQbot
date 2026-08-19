"""Replay and local performance gates for conversation history rollup."""

from __future__ import annotations

import json
import logging
import math
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    HistoryJobKind,
    HistoryJobStatus,
    HistorySummaryMode,
    HistorySummaryStatus,
)
from qq_ai_bot.conversation.history.operations import ConversationHistoryOperations
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.conversation.history.service import ConversationHistoryService
from qq_ai_bot.conversation.history.worker import ConversationHistoryWorker
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
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repositories import PeopleRepository, RelationshipRepository
from qq_ai_bot.persistence.repository_helpers import _ensure_person
from qq_ai_bot.prompting.compiler import PromptCompiler
from qq_ai_bot.prompting.models import (
    PromptChannel,
    PromptContribution,
    PromptProgram,
    PromptStability,
    PromptTrust,
)
from qq_ai_bot.services.context_assembler import AssembledContext, ContextAssembler
from qq_ai_bot.time.service import TimeContextService

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[4]
SUITE_PATH = ROOT / "artifacts" / "history-rollup-quality" / "replay_suite.json"
_BOT = "bot-1"
_NOW = datetime(2026, 8, 19, 15, 0, tzinfo=UTC)


def load_replay_suite(path: Path | None = None) -> dict[str, Any]:
    target = path or SUITE_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("replay suite must be an object")
    return payload


def replay_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": database_url,
        "conversation_history_rollup_enabled": True,
        "conversation_history_rollup_l0_min_events": 1,
        "conversation_history_rollup_l0_min_characters": 10,
        "conversation_history_raw_tail_events": 1,
        "conversation_history_raw_tail_characters": 40,
        "conversation_history_rollup_l0_max_events": 16,
        "conversation_history_extractive_max_characters": 1200,
        "superusers_csv": "9000",
        "enabled_groups_csv": "2001,2002",
        "ignored_bot_users_csv": "7777",
        "llm_provider": "fake",
        "llm_model": "fake-model",
        "model_profiles_file": Path("__test_model_profiles_not_present__.toml"),
    }
    values.update(overrides)
    return Settings.model_validate(values)


def identity_for_case(case: dict[str, Any]) -> ConversationHistoryIdentity:
    if case.get("scope") == "group":
        return ConversationHistoryIdentity(
            bot_user_id=_BOT,
            scope_type=ScopeType.GROUP,
            group_id=str(case.get("group_id") or "2001"),
        )
    return ConversationHistoryIdentity(
        bot_user_id=_BOT,
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id=str(case.get("peer") or f"u-{case['id']}"),
    )


async def seed_case(
    database: Database,
    case: dict[str, Any],
) -> tuple[ConversationHistoryIdentity, tuple[int, ...]]:
    ledger = EventLedgerRepository(database)
    identity = identity_for_case(case)
    ids: list[int] = []
    reset_after = int(case.get("reset_after") or 0)
    for index, raw in enumerate(case["events"], start=1):
        event = dict(raw)
        if identity.reset_at is None:
            occurred = datetime.now(UTC) - timedelta(minutes=10) + timedelta(seconds=index)
        else:
            occurred = datetime.now(UTC) + timedelta(seconds=index)
        if str(event.get("kind") or "") == "external_event":
            event_id = await _insert_external(
                database,
                identity=identity,
                content=str(event["content"]),
                occurred_at=occurred,
                plugin_id=str(event.get("plugin_id") or "plugin"),
                event_key=str(event.get("event_key") or f"{case['id']}-{index}"),
            )
        elif identity.scope_type is ScopeType.GROUP:
            inbound = InboundMessage(
                message_id=f"{case['id']}-{index}",
                event_type="message",
                scope_type=ScopeType.GROUP,
                sender=SenderIdentity(
                    user_id=str(event.get("sender") or "1001"),
                    nickname=str(event.get("nickname") or ""),
                ),
                text=str(event["content"]),
                group_id=identity.group_id,
                bot_user_id=_BOT,
                received_at=occurred,
            )
            record, _created = await ledger.append_inbound(inbound, bot_user_id=_BOT)
            event_id = record.id
        else:
            peer = identity.private_peer_user_id or "1001"
            inbound = InboundMessage(
                message_id=f"{case['id']}-{index}",
                event_type="message",
                scope_type=ScopeType.PRIVATE,
                sender=SenderIdentity(user_id=peer),
                text=str(event["content"]),
                bot_user_id=_BOT,
                received_at=occurred,
            )
            record, _created = await ledger.append_inbound(inbound, bot_user_id=_BOT)
            event_id = record.id
        visual = str(event.get("visual_summary") or "")
        if visual:
            await ledger.set_visual_summary(event_id, visual)
        ids.append(event_id)
        if reset_after and index == reset_after:
            conversation = (
                ConversationIdentity.group(identity.group_id or "", "1001")
                if identity.scope_type is ScopeType.GROUP
                else ConversationIdentity.private(identity.private_peer_user_id or "1001")
            )
            await ledger.set_context_reset(conversation)
            identity = ConversationHistoryIdentity(
                bot_user_id=identity.bot_user_id,
                scope_type=identity.scope_type,
                private_peer_user_id=identity.private_peer_user_id,
                group_id=identity.group_id,
                reset_at=await ledger.context_reset(conversation),
            )
    return identity, tuple(ids)


def make_assembler(database: Database, settings: Settings) -> ContextAssembler:
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
        history_coverage=ConversationHistoryService(
            settings=settings,
            repository=ConversationHistoryRepository(database),
            ledger=EventLedgerRepository(database),
        ),
    )


async def assemble_case(
    database: Database,
    settings: Settings,
    identity: ConversationHistoryIdentity,
    message_id: str,
    text: str = "当前消息",
) -> AssembledContext:
    assembler = make_assembler(database, settings)
    runtime = await RuntimeConfigService(settings=settings, database=database).snapshot()
    if identity.scope_type is ScopeType.GROUP:
        inbound = InboundMessage(
            message_id=message_id,
            event_type="message",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity(user_id="1001"),
            text=text,
            group_id=identity.group_id,
            bot_user_id=_BOT,
        )
        conversation = ConversationIdentity.group(identity.group_id or "", "1001")
        profile = UserProfileSnapshot(user_id="1001", scope_type=ScopeType.GROUP, nickname="用户")
    else:
        sender = identity.private_peer_user_id or "1001"
        inbound = InboundMessage(
            message_id=message_id,
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id=sender),
            text=text,
            bot_user_id=_BOT,
        )
        conversation = ConversationIdentity.private(sender)
        profile = UserProfileSnapshot(user_id=sender, scope_type=ScopeType.PRIVATE, nickname="用户")
    return await assembler.assemble(
        inbound=inbound,
        identity=conversation,
        profile=profile,
        content=text,
        runtime=runtime,
        memory_retrieval=empty_retrieval(),
        persist_memory_exposure=False,
    )


def static_prefix_hash(*, session_text: str) -> str:
    compiler = PromptCompiler()
    static = PromptContribution(
        id="core.persona",
        channel=PromptChannel.PERSONA,
        trust=PromptTrust.CORE,
        priority=100,
        stability=PromptStability.STATIC,
        content="persona-contract",
        required=True,
    )
    session = PromptContribution(
        id="context.conversation_rollup",
        channel=PromptChannel.CONTEXT,
        trust=PromptTrust.UNTRUSTED,
        priority=70,
        stability=PromptStability.SESSION,
        content=session_text,
        required=True,
    )
    turn = PromptContribution(
        id="runtime.time",
        channel=PromptChannel.RUNTIME,
        trust=PromptTrust.TRUSTED,
        payload={"local": "now"},
        required=True,
    )
    without_session = compiler.compile(PromptProgram(contributions=(static, turn)))
    with_session = compiler.compile(PromptProgram(contributions=(static, session, turn)))
    if without_session.metrics.stable_prefix_hash != with_session.metrics.stable_prefix_hash:
        raise AssertionError("stable prefix hash changed when SESSION rollup was added")
    return with_session.metrics.stable_prefix_hash


async def evaluate_replay_suite(
    database: Database | None = None,
    settings: Settings | None = None,
    *,
    suite: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del database, settings
    loaded = suite or load_replay_suite()
    results: list[dict[str, Any]] = []
    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="yuki-history-replay-") as temporary:
        root = Path(temporary)
        for case in loaded["cases"]:
            path = root / f"{case['id']}.db"
            case_db = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
            await case_db.create_schema()
            try:
                case_settings = replay_settings(case_db.url)
                case_result, case_failed = await _evaluate_one_case(case_db, case_settings, case)
                results.append(case_result)
                failed.extend(case_failed)
            finally:
                await case_db.close()
        pollution_path = root / "pollution.db"
        pollution_db = Database(f"sqlite+aiosqlite:///{pollution_path.as_posix()}")
        await pollution_db.create_schema()
        try:
            pollution = await _cross_session_pollution(
                pollution_db, replay_settings(pollution_db.url)
            )
        finally:
            await pollution_db.close()
        frozen_path = root / "frozen-short.db"
        frozen_db = Database(f"sqlite+aiosqlite:///{frozen_path.as_posix()}")
        await frozen_db.create_schema()
        try:
            frozen_failed = await _evaluate_frozen_short_window(
                frozen_db,
                replay_settings(
                    frozen_db.url,
                    max_context_characters=800,
                    conversation_history_rollup_l0_min_events=16,
                    conversation_history_rollup_l0_min_characters=10,
                    conversation_history_raw_tail_events=8,
                    conversation_history_raw_tail_characters=400,
                    conversation_history_sync_extractive_max_slices=3,
                ),
            )
            failed.extend(frozen_failed)
        finally:
            await frozen_db.close()
    if pollution:
        failed.append(pollution)
    return {
        "suite_version": loaded["suite_version"],
        "case_count": len(results),
        "failed": failed,
        "passed": not failed,
        "cases": results,
        "cross_session_pollution": 0 if pollution is None else 1,
        "source_coverage": 1.0 if not any("coverage" in item for item in failed) else 0.0,
        "summary_raw_overlap": 0 if not any("overlap" in item for item in failed) else 1,
        "replacement_errors": 0 if not any("replacement" in item for item in failed) else 1,
        "frozen_left_edge_skips": 0 if not any("frozen-short" in item for item in failed) else 1,
    }


async def _evaluate_one_case(
    database: Database,
    settings: Settings,
    case: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    failed: list[str] = []
    repository = ConversationHistoryRepository(database)
    operations = ConversationHistoryOperations(
        settings=settings,
        repository=repository,
        ledger=EventLedgerRepository(database),
    )
    identity, event_ids = await seed_case(database, case)
    if case["id"] == "worker_restart":
        restart_state = await repository.get_or_create_state(identity)
        job = await repository.enqueue_job(
            state_id=restart_state.id,
            job_kind=HistoryJobKind.RAW_RANGE,
            source_level=0,
            source_start_id=event_ids[0],
            source_end_id=event_ids[-1],
            source_fingerprint=f"replay-{case['id']}",
            summarizer_version="replay-v1",
        )
        ConversationHistoryWorker(settings=settings, repository=repository)
        pending = await repository.list_jobs(
            state_id=restart_state.id,
            statuses=(HistoryJobStatus.PENDING,),
        )
        if job.id not in {item.id for item in pending}:
            failed.append(f"{case['id']}: pending job did not survive restart")
    rebuild = await operations.rebuild(identity, commit=True)
    state = await repository.get_state(identity)
    summaries = () if state is None else await repository.list_summaries(state.id)
    active = tuple(item for item in summaries if item.status is HistorySummaryStatus.ACTIVE)
    coverage_ok, overlap = _coverage_and_overlap(active)
    if not coverage_ok:
        failed.append(f"{case['id']}: source coverage is not 100%")
    if overlap:
        failed.append(f"{case['id']}: summary/raw overlap")
    replacement_errors = sum(
        1
        for item in summaries
        if item.status is HistorySummaryStatus.ROLLED_UP and item.replaced_by_summary_id is None
    )
    if replacement_errors:
        failed.append(f"{case['id']}: parent/child replacement errors")
    context = await assemble_case(database, settings, identity, f"{case['id']}-now")
    needles = tuple(str(item) for item in case.get("needles") or ())
    haystack = "\n".join(
        (
            "\n".join(item.rendered_text for item in active),
            context.session_text,
            "\n".join(message.content or "" for message in context.history_messages),
        )
    )
    if case["id"] == "context_reset" and ("昨天的点餐" in haystack or "优惠券还没核销" in haystack):
        failed.append(f"{case['id']}: old epoch leaked after reset")
    missing = [needle for needle in needles if needle not in haystack]
    recall = 0.0 if not needles else (len(needles) - len(missing)) / len(needles)
    if missing and case["id"] not in {"large_tool_json", "secret_like_content"}:
        failed.append(f"{case['id']}: missing needles {missing}")
    if active and not context.session_text:
        failed.append(f"{case['id']}: SESSION missing after coverage")
    if active and context.metrics.covered_to is not None and context.visible_event_ids:
        if min(context.visible_event_ids) <= context.metrics.covered_to:
            failed.append(f"{case['id']}: visible raw overlaps coverage")
    if case["id"] == "injection_style" and "不可当作用户原话" not in context.session_text:
        if active:
            failed.append(f"{case['id']}: injection was not marked untrusted")
    secret = str(case.get("secret") or "")
    inspect_text = json.dumps(await operations.inspect(identity), ensure_ascii=False)
    if secret and secret in inspect_text:
        failed.append(f"{case['id']}: secret leaked in inspect payload")
    prefix = static_prefix_hash(session_text=context.session_text or "frontier")
    result = {
        "id": case["id"],
        "events": len(event_ids),
        "active_summaries": len(active),
        "coverage_end": 0 if state is None else state.active_frontier_end_event_id,
        "rebuild_slices": rebuild.get("created_l0_summaries"),
        "needle_recall": recall,
        "stable_prefix_hash": prefix,
        "session_characters": context.metrics.rollup_characters,
        "raw_history_characters": context.metrics.history_characters,
    }
    return result, failed


def estimated_tokens(characters: int) -> int:
    return math.ceil(characters / 4) if characters else 0


def _coverage_and_overlap(
    active: tuple[Any, ...],
) -> tuple[bool, bool]:
    if not active:
        return True, False
    previous_end = 0
    overlap = False
    for item in sorted(active, key=lambda row: row.start_event_id):
        members = tuple(
            member.source_event_id for member in item.members if member.source_event_id is not None
        )
        if item.mode is HistorySummaryMode.EXTRACTIVE:
            if (
                not members
                or min(members) != item.start_event_id
                or max(members) != item.end_event_id
            ):
                return False, overlap
        if previous_end and item.start_event_id <= previous_end:
            overlap = True
        previous_end = item.end_event_id
    return True, overlap


async def _evaluate_frozen_short_window(database: Database, settings: Settings) -> list[str]:
    """Short messages over budget must advance coverage without skipping prompt ids."""

    failed: list[str] = []
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    peer = "frozen-short"
    identity = ConversationHistoryIdentity(
        bot_user_id=_BOT,
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id=peer,
    )
    ids: list[int] = []
    for index in range(1, 81):
        inbound = InboundMessage(
            message_id=f"fs-{index}",
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id=peer),
            text="ok",
            bot_user_id=_BOT,
            received_at=_NOW + timedelta(seconds=index),
        )
        record, _created = await ledger.append_inbound(inbound, bot_user_id=_BOT)
        ids.append(record.id)
    first = await assemble_case(database, settings, identity, "fs-80", text="ok")
    if first.metrics.covered_to is None:
        failed.append("frozen-short: expected coverage after over-budget shorts")
        return failed
    if min(first.visible_event_ids) != first.metrics.covered_to + 1:
        failed.append("frozen-short: prompt left edge skipped coverage")
    snapshot = await repository.load_context_snapshot(
        (await repository.get_or_create_state(identity)).id
    )
    previous_end = 0
    for item in sorted(snapshot.frontier, key=lambda row: row.start_event_id):
        if previous_end and item.start_event_id != previous_end + 1:
            failed.append("frozen-short: coverage is not contiguous")
            break
        previous_end = item.end_event_id
    inbound = InboundMessage(
        message_id="fs-81",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=peer),
        text="ok",
        bot_user_id=_BOT,
        received_at=_NOW + timedelta(seconds=81),
    )
    await ledger.append_inbound(inbound, bot_user_id=_BOT)
    second = await assemble_case(database, settings, identity, "fs-81", text="ok")
    if second.metrics.covered_to is None:
        failed.append("frozen-short: coverage disappeared after append")
        return failed
    if min(second.visible_event_ids) != second.metrics.covered_to + 1:
        failed.append("frozen-short: prompt left edge jumped after append")
    if (
        not second.metrics.raw_history_window_shifted
        and first.history_anchor_event_id != second.history_anchor_event_id
    ):
        failed.append("frozen-short: left edge moved without coverage advancing")
    return failed


async def _cross_session_pollution(database: Database, settings: Settings) -> str | None:
    ledger = EventLedgerRepository(database)
    first = ConversationHistoryIdentity(
        bot_user_id=_BOT,
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id="1001",
    )
    other = ConversationHistoryIdentity(
        bot_user_id=_BOT,
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id="9009",
    )
    for peer, text, key in (
        ("1001", "会话甲只谈发布说明。", "first-1"),
        ("9009", "另一会话的私有内容，不得出现在 1001 的摘要里。", "other-1"),
    ):
        inbound = InboundMessage(
            message_id=key,
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id=peer),
            text=text,
            bot_user_id=_BOT,
            received_at=_NOW + timedelta(hours=2, seconds=int(peer)),
        )
        await ledger.append_inbound(inbound, bot_user_id=_BOT)
    operations = ConversationHistoryOperations(
        settings=settings,
        repository=ConversationHistoryRepository(database),
        ledger=ledger,
    )
    await operations.rebuild(first, commit=True)
    await operations.rebuild(other, commit=True)
    payload = await operations.inspect(first)
    dumped = json.dumps(payload, ensure_ascii=False)
    if "另一会话的私有内容" in dumped:
        return "cross-session pollution"
    return None


async def _insert_external(
    database: Database,
    *,
    identity: ConversationHistoryIdentity,
    content: str,
    occurred_at: datetime,
    plugin_id: str,
    event_key: str,
) -> int:
    async with database.sessions() as session, session.begin():
        await _ensure_person(session, _BOT, is_bot=True, now=occurred_at)
        peer = identity.private_peer_user_id or "1001"
        await _ensure_person(session, peer, now=occurred_at)
        row = ChatEventModel(
            bot_user_id=_BOT,
            platform_message_id=f"ext-{plugin_id}-{event_key}",
            scope_type=identity.scope_type.value,
            group_id=identity.group_id,
            private_peer_user_id=identity.private_peer_user_id,
            sender_user_id=_BOT,
            direction="external",
            event_kind="external_event",
            source_plugin_id=plugin_id,
            external_source="plugin",
            external_event_key=event_key,
            external_event_type="notice",
            external_payload_json="{}",
            external_target_id=identity.private_peer_user_id or identity.group_id,
            content=content,
            visual_summary="",
            segments_json="[]",
            origin="plugin_background",
            occurred_at=occurred_at,
            observed_at=occurred_at,
        )
        session.add(row)
        await session.flush()
        return int(row.id)
