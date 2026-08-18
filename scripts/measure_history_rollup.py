"""Measure local conversation history rollup performance. Do not invent provider tokens."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    ConversationHistoryJob,
)
from qq_ai_bot.conversation.history.operations import ConversationHistoryOperations
from qq_ai_bot.conversation.history.quality import (
    assemble_case,
    estimated_tokens,
    static_prefix_hash,
)
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.conversation.history.service import ConversationHistoryService
from qq_ai_bot.conversation.history.worker import (
    ConversationHistoryJobResult,
    ConversationHistoryWorker,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.observability.runtime_baseline import bootstrap_percentile_ci, percentile
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository

ROOT = Path(__file__).resolve().parents[1]
WARMUP = 10
OBSERVE_SAMPLES = 80
FRONTIER_SAMPLES = 80
ASSEMBLE_SAMPLES = 30
LONG_SESSION_EVENTS = 200
_BOT = "bot-1"
_PEER = "1001"
_IDENTITY = ConversationHistoryIdentity(
    bot_user_id=_BOT,
    scope_type=ScopeType.PRIVATE,
    private_peer_user_id=_PEER,
)
_NOW = datetime(2026, 8, 19, 16, 0, tzinfo=UTC)


def _settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": database_url,
        "superusers_csv": "9000",
        "enabled_groups_csv": "2001,2002",
        "ignored_bot_users_csv": "7777",
        "llm_provider": "fake",
        "llm_model": "fake-model",
        "model_profiles_file": Path("__test_model_profiles_not_present__.toml"),
        "conversation_history_rollup_enabled": True,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _summarize(values: list[float]) -> dict[str, object]:
    return {
        "n": len(values),
        "min_ms": min(values) if values else None,
        "max_ms": max(values) if values else None,
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "p50_ci": bootstrap_percentile_ci(values, 50),
        "p95_ci": bootstrap_percentile_ci(values, 95),
        "percentile_algorithm": "linear_interpolation",
        "ci_method": "percentile_bootstrap",
        "ci_samples": 1000,
        "ci_seed": 36,
    }


async def _seed(
    ledger: EventLedgerRepository,
    count: int,
    *,
    start: int = 1,
    text: str = "技术讨论条目",
) -> tuple[int, ...]:
    ids: list[int] = []
    for index in range(start, start + count):
        inbound = InboundMessage(
            message_id=f"measure-{_PEER}-{index}",
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id=_PEER),
            text=f"{text}-{index} " + ("内容" * 18),
            bot_user_id=_BOT,
            received_at=_NOW + timedelta(seconds=index),
        )
        record, _created = await ledger.append_inbound(inbound, bot_user_id=_BOT)
        ids.append(record.id)
    return tuple(ids)


async def measure() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="yuki-history-measure-") as temporary:
        path = Path(temporary) / "measure.db"
        database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
        await database.create_schema()
        try:
            return await _measure_with(database)
        finally:
            await database.close()


async def _measure_with(database: Database) -> dict[str, object]:
    on_settings = _settings(database.url, conversation_history_rollup_enabled=True)
    off_settings = _settings(database.url, conversation_history_rollup_enabled=False)
    ledger = EventLedgerRepository(database)
    repository = ConversationHistoryRepository(database)
    service = ConversationHistoryService(
        settings=on_settings,
        repository=repository,
        ledger=ledger,
    )
    ids = await _seed(ledger, LONG_SESSION_EVENTS)
    observe_ms: list[float] = []
    for event_id in ids[:WARMUP]:
        await repository.observe_event(_IDENTITY, event_id=event_id, character_count=80)
    for event_id in ids[WARMUP : WARMUP + OBSERVE_SAMPLES]:
        started = time.perf_counter()
        await repository.observe_event(_IDENTITY, event_id=event_id, character_count=80)
        observe_ms.append((time.perf_counter() - started) * 1000.0)

    operations = ConversationHistoryOperations(
        settings=on_settings,
        repository=repository,
        ledger=ledger,
    )
    rebuild = await operations.rebuild(_IDENTITY, commit=True)
    for _ in range(WARMUP):
        await repository.load_prompt_snapshot(_IDENTITY, recent_limit=24)
    frontier_ms: list[float] = []
    for _ in range(FRONTIER_SAMPLES):
        started = time.perf_counter()
        await repository.load_prompt_snapshot(_IDENTITY, recent_limit=24)
        frontier_ms.append((time.perf_counter() - started) * 1000.0)

    on_context = await assemble_case(
        database, on_settings, _IDENTITY, f"m-{_PEER}-on", text="当前消息"
    )
    off_context = await assemble_case(
        database, off_settings, _IDENTITY, f"m-{_PEER}-off", text="当前消息"
    )
    on_history_subject = (
        on_context.metrics.history_characters + on_context.metrics.rollup_characters
    )
    off_history_subject = off_context.metrics.history_characters
    character_drop = None
    token_drop = None
    if off_history_subject:
        character_drop = 1.0 - (on_history_subject / off_history_subject)
        token_drop = 1.0 - (
            estimated_tokens(on_history_subject) / estimated_tokens(off_history_subject)
        )

    on_ms: list[float] = []
    off_ms: list[float] = []
    for index in range(ASSEMBLE_SAMPLES):
        started = time.perf_counter()
        await assemble_case(database, on_settings, _IDENTITY, f"m-{_PEER}-on-{index}")
        on_ms.append((time.perf_counter() - started) * 1000.0)
        started = time.perf_counter()
        await assemble_case(database, off_settings, _IDENTITY, f"m-{_PEER}-off-{index}")
        off_ms.append((time.perf_counter() - started) * 1000.0)

    failing = _FailingProcessor()
    worker = ConversationHistoryWorker(
        settings=on_settings,
        repository=repository,
        processor=failing,
    )
    await worker.start()
    try:
        fail_ms: list[float] = []
        for index in range(ASSEMBLE_SAMPLES):
            started = time.perf_counter()
            await assemble_case(database, on_settings, _IDENTITY, f"m-{_PEER}-fail-{index}")
            fail_ms.append((time.perf_counter() - started) * 1000.0)
    finally:
        await worker.close()

    jobs = await repository.list_jobs(state_id=(await repository.get_or_create_state(_IDENTITY)).id)
    prefix = static_prefix_hash(session_text=on_context.session_text or "frontier")
    sqlite_version = sqlite3.sqlite_version
    return {
        "measured_at": datetime.now(UTC).isoformat(),
        "commit": _git_commit(),
        "environment": {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "sqlite": sqlite_version,
            "provider": "none",
            "notes": "Local SQLite. No Chat/Flash/Vision calls.",
        },
        "sample_sizes": {
            "warmup": WARMUP,
            "observe": OBSERVE_SAMPLES,
            "frontier": FRONTIER_SAMPLES,
            "assemble": ASSEMBLE_SAMPLES,
            "long_session_events": LONG_SESSION_EVENTS,
        },
        "observe_event_ms": _summarize(observe_ms),
        "frontier_query_ms": _summarize(frontier_ms),
        "context_build_on_ms": _summarize(on_ms),
        "context_build_off_ms": _summarize(off_ms),
        "context_build_worker_fail_ms": _summarize(fail_ms),
        "context_build_p95_increment_ms": _delta_p95(on_ms, off_ms),
        "worker_fail_p95_delta_ms": _delta_p95(fail_ms, on_ms),
        "history_subject": {
            "off_characters": off_history_subject,
            "on_characters": on_history_subject,
            "character_drop": character_drop,
            "off_estimated_tokens": estimated_tokens(off_history_subject),
            "on_estimated_tokens": estimated_tokens(on_history_subject),
            "estimated_token_drop": token_drop,
            "token_estimator": "ceil(characters/4) compiler estimate, not provider prompt_tokens",
            "provider_prompt_tokens": None,
            "provider_cached_tokens": None,
        },
        "foreground_model_calls": 0,
        "extractive_foreground_model_calls": 0,
        "stable_prefix_hash": prefix,
        "rebuild": {
            "created_l0_summaries": rebuild.get("created_l0_summaries"),
            "coverage_end_event_id": rebuild.get("coverage_end_event_id"),
            "event_count": rebuild.get("event_count"),
        },
        "jobs": len(jobs),
        "compaction_requests_per_1000_events": None
        if not ids
        else round(1000 * len(jobs) / len(ids), 3),
        "service_used": type(service).__name__,
    }


def _delta_p95(left: list[float], right: list[float]) -> float | None:
    left_p95 = percentile(left, 95)
    right_p95 = percentile(right, 95)
    if left_p95 is None or right_p95 is None:
        return None
    return float(left_p95 - right_p95)


class _FailingProcessor:
    async def process(self, job: ConversationHistoryJob) -> ConversationHistoryJobResult:
        del job
        raise RuntimeError("synthetic worker failure")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "history-rollup-quality" / "measure.json",
    )
    args = parser.parse_args()
    payload = asyncio.run(measure())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
