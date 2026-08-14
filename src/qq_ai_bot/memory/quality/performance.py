"""Explicit, synthetic large-database Memory V2 performance scenario."""

from __future__ import annotations

import asyncio
import gc
import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import insert, select

from qq_ai_bot.memory.context import retrieval_fact_context
from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.fake import FakeEmbeddingProvider
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.semantic import MemorySemanticIndex
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder, EmbeddingQueryBuilder
from qq_ai_bot.memory.enums import MemoryRetrievalMode, MemoryScopeType, MemoryTargetRole
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.models import MemoryEntityTarget, MemoryQuery
from qq_ai_bot.memory.quality.database import migrate_sqlite_database
from qq_ai_bot.memory.quality.models import (
    QualityPerformanceReport,
    QualityPerformanceScenario,
)
from qq_ai_bot.memory.query import normalize_query_text
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryEmbeddingModel, MemoryFactModel
from qq_ai_bot.persistence.repositories import EventLedgerRepository


class MemoryQualityPerformanceRunner:
    """Measure one fixed synthetic shape without touching a configured database."""

    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root

    async def run(
        self,
        scenario: QualityPerformanceScenario,
        *,
        quality_report_path: Path | None = None,
    ) -> QualityPerformanceReport:
        with tempfile.TemporaryDirectory(prefix="yuki-memory-performance-") as temporary:
            path = Path(temporary) / "performance.db"
            await asyncio.to_thread(migrate_sqlite_database, self._root, path)
            await asyncio.to_thread(self._populate_core, path, scenario)
            database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
            provider = FakeEmbeddingProvider(dimensions=8)
            try:
                embedded_fact_count = await self._populate_embeddings(database, provider)
                measurements = await self._measure(database, provider, scenario)
            finally:
                await database.close()

        suite_ms = self._quality_suite_duration(quality_report_path)
        return QualityPerformanceReport(
            generated_at=datetime.now(UTC),
            machine_class=(
                f"{platform.system()}-{platform.machine()}-python{sys.version_info.major}."
                f"{sys.version_info.minor}"
            ),
            python_version=sys.version.split()[0],
            sqlite_version=sqlite3.sqlite_version,
            scenario=scenario,
            populated_fact_count=scenario.users * scenario.facts_per_user,
            active_embedded_fact_count=embedded_fact_count,
            populated_event_count=scenario.chat_events,
            quality_suite_total_ms=suite_ms,
            model_request_count=0,
            embedding_document_request_count=provider.document_requests,
            embedding_query_request_count=provider.query_requests,
            **measurements,
        )

    @staticmethod
    def _populate_core(path: Path, scenario: QualityPerformanceScenario) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        users = tuple(f"9901{index:04d}" for index in range(scenario.users))
        groups = tuple(f"8801{index:04d}" for index in range(scenario.groups))
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            timestamp = base.isoformat(sep=" ")
            connection.executemany(
                "INSERT INTO people(user_id,nickname,enabled,is_bot,first_seen_at,last_seen_at) "
                "VALUES(?,?,1,0,?,?)",
                (
                    (user, f"Synthetic {index}", timestamp, timestamp)
                    for index, user in enumerate(users)
                ),
            )
            connection.executemany(
                "INSERT INTO groups(group_id,name,enabled,require_mention,autonomous_enabled,"
                "first_seen_at,last_seen_at,updated_at) VALUES(?,?,1,1,1,?,?,?)",
                (
                    (group, f"Synthetic group {index}", timestamp, timestamp, timestamp)
                    for index, group in enumerate(groups)
                ),
            )
            connection.executemany(
                "INSERT INTO memberships(user_id,group_id,group_card,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?,?)",
                (
                    (user, groups[index % len(groups)], "Synthetic card", timestamp, timestamp)
                    for index, user in enumerate(users)
                ),
            )

            facts: list[tuple[object, ...]] = []
            person_fact_count = max(1, scenario.facts_per_user * 4 // 5)
            for user_index, user in enumerate(users):
                group = groups[user_index % len(groups)]
                for fact_index in range(scenario.facts_per_user):
                    person_scope = fact_index < person_fact_count
                    status = "contested" if fact_index >= scenario.facts_per_user - 2 else "active"
                    content = f"synthetic person {user_index:04d} topic {fact_index:04d}"
                    facts.append(
                        (
                            "person" if person_scope else "person_group",
                            user,
                            None if person_scope else group,
                            "fact",
                            f"quality:{fact_index:04d}",
                            "quality",
                            content,
                            content,
                            3,
                            0.9,
                            "automatic",
                            "self_report" if person_scope else "group_report",
                            status,
                            "contested" if status == "contested" else "clear",
                            None,
                            None,
                            timestamp,
                            timestamp,
                            timestamp,
                            None,
                            None,
                        )
                    )
            connection.executemany(
                "INSERT INTO memory_facts(scope_type,subject_user_id,group_id,kind,memory_key,"
                "category,content,normalized_content,importance,confidence,source_type,authority,"
                "status,conflict_state,supersedes_id,valid_from,created_at,updated_at,"
                "last_confirmed_at,invalidated_reason,last_injected_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                facts,
            )
            for user_index in range(scenario.users):
                first = user_index * scenario.facts_per_user + scenario.facts_per_user - 1
                second = first + 1
                connection.execute(
                    "INSERT INTO memory_fact_relations(source_fact_id,target_fact_id,relation_type,"
                    "confidence,source_event_id,created_at) VALUES(?,?,'contradicts',1.0,NULL,?)",
                    (first, second, timestamp),
                )

            event_rows: list[tuple[object, ...]] = []
            for index in range(scenario.chat_events):
                user = users[index % len(users)]
                group = groups[index % len(groups)]
                occurred = (base + timedelta(seconds=index)).isoformat(sep=" ")
                event_rows.append(
                    (
                        "99019999",
                        f"quality-performance-{index:06d}",
                        "group",
                        group,
                        None,
                        user,
                        "inbound",
                        f"synthetic event {index:06d}",
                        "",
                        "[]",
                        None,
                        "user_message",
                        None,
                        None,
                        occurred,
                        occurred,
                    )
                )
                if len(event_rows) == 5_000:
                    connection.executemany(
                        "INSERT INTO chat_events("
                        "bot_user_id,platform_message_id,scope_type,group_id,"
                        "private_peer_user_id,sender_user_id,direction,content,visual_summary,"
                        "segments_json,reply_to_message_id,origin,automation_id,automation_run_id,"
                        "occurred_at,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        event_rows,
                    )
                    event_rows.clear()
            if event_rows:
                connection.executemany(
                    "INSERT INTO chat_events(bot_user_id,platform_message_id,scope_type,group_id,"
                    "private_peer_user_id,sender_user_id,direction,content,visual_summary,"
                    "segments_json,reply_to_message_id,origin,automation_id,automation_run_id,"
                    "occurred_at,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    event_rows,
                )
            connection.commit()

    @staticmethod
    async def _populate_embeddings(
        database: Database,
        provider: FakeEmbeddingProvider,
    ) -> int:
        documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4_000)
        repository = MemoryEmbeddingRepository(database)
        profile = await repository.ensure_profile(provider.profile)
        codec = Float32VectorCodec()
        async with database.sessions() as session:
            facts = tuple(
                (
                    await session.execute(
                        select(
                            MemoryFactModel.id,
                            MemoryFactModel.kind,
                            MemoryFactModel.category,
                            MemoryFactModel.memory_key,
                            MemoryFactModel.content,
                        ).where(MemoryFactModel.status == "active")
                    )
                ).all()
            )
        for offset in range(0, len(facts), 500):
            batch = facts[offset : offset + 500]
            texts = tuple(
                documents.build_fields(
                    kind=row.kind,
                    category=row.category,
                    memory_key=row.memory_key,
                    content=row.content,
                )
                for row in batch
            )
            embedded = await provider.embed_documents(texts)
            now = datetime.now(UTC)
            values = [
                {
                    "fact_id": int(row.id),
                    "profile_id": profile.id,
                    "content_hash": documents.content_hash_fields(
                        kind=row.kind,
                        category=row.category,
                        memory_key=row.memory_key,
                        content=row.content,
                    ),
                    "vector_blob": codec.encode(vector),
                    "created_at": now,
                    "updated_at": now,
                }
                for row, vector in zip(batch, embedded.vectors, strict=True)
            ]
            async with database.sessions() as session, session.begin():
                await session.execute(insert(MemoryEmbeddingModel), values)
        return len(facts)

    @staticmethod
    async def _measure(
        database: Database,
        provider: FakeEmbeddingProvider,
        scenario: QualityPerformanceScenario,
    ) -> dict[str, float]:
        ledger = EventLedgerRepository(database)
        selection = MemoryRebuildSelection(all_events=True)
        snapshot = await ledger.maximum_event_id()
        started = time.perf_counter()
        plan = await ledger.count_rebuild_candidates(
            selection,
            snapshot_max_event_id=snapshot,
        )
        plan_latency_ms = (time.perf_counter() - started) * 1_000
        if plan.eligible_events != scenario.chat_events:
            raise RuntimeError("synthetic performance event population is incomplete")

        gc.collect()
        tracemalloc.start()
        scan_started = time.perf_counter()
        scanned = 0
        after_time = None
        after_id = None
        while True:
            rows = await ledger.list_rebuild_candidates(
                selection,
                snapshot_max_event_id=snapshot,
                after_occurred_at=after_time,
                after_event_id=after_id,
                limit=scenario.keyset_batch_size,
            )
            if not rows:
                break
            scanned += len(rows)
            after_time, after_id = rows[-1].occurred_at, rows[-1].id
        scan_seconds = time.perf_counter() - scan_started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if scanned != scenario.chat_events:
            raise RuntimeError("keyset scan did not visit every synthetic event")

        documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4_000)
        embeddings = MemoryEmbeddingRepository(database)
        profile = await embeddings.ensure_profile(provider.profile)
        retriever = MemoryRetriever(
            repository=MemoryFactRepository(database),
            lexical_index=SQLiteMemoryFTSIndex(database),
            semantic_index=MemorySemanticIndex(embeddings, documents=documents),
            embedding_provider=provider,
            embedding_profile=profile,
            embedding_queries=EmbeddingQueryBuilder(max_characters=4_000),
        )
        retrieval_latencies: list[float] = []
        context_latencies: list[float] = []
        person_fact_count = max(1, scenario.facts_per_user * 4 // 5)
        for index in range(scenario.query_count):
            user_index = index % scenario.users
            fact_index = index % person_fact_count
            text_value = f"synthetic person {user_index:04d} topic {fact_index:04d}"
            target = MemoryEntityTarget(
                role=MemoryTargetRole.CURRENT_PERSON,
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=f"9901{user_index:04d}",
                block_id="current_person",
            )
            query = MemoryQuery(
                text=text_value,
                normalized_text=normalize_query_text(text_value),
                mode=MemoryRetrievalMode.RELEVANT,
                targets=(target,),
                candidate_limit=50,
                limit_per_target=10,
                always_on_explicit_preference_limit=0,
                query_term_limit=16,
                semantic_enabled=True,
                semantic_candidate_limit=50,
                semantic_min_similarity=-1,
            )
            retrieval_started = time.perf_counter()
            result = await retriever.retrieve(query)
            retrieval_latencies.append((time.perf_counter() - retrieval_started) * 1_000)
            context_started = time.perf_counter()
            json.dumps(
                [retrieval_fact_context(hit) for hit in result.hits],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            context_latencies.append((time.perf_counter() - context_started) * 1_000)

        return {
            "plan_latency_ms": plan_latency_ms,
            "keyset_scan_seconds": scan_seconds,
            "keyset_events_per_second": scanned / max(scan_seconds, 1e-9),
            "retrieval_p50_ms": statistics.median(retrieval_latencies),
            "retrieval_p95_ms": _percentile(retrieval_latencies, 0.95),
            "context_p50_ms": statistics.median(context_latencies),
            "context_p95_ms": _percentile(context_latencies, 0.95),
            "peak_memory_mib": peak / 1024 / 1024,
        }

    @staticmethod
    def _quality_suite_duration(path: Path | None) -> float | None:
        if path is None or not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        duration = payload.get("duration_seconds")
        return float(duration) * 1_000 if isinstance(duration, int | float) else None


def _percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * ratio + 0.999999)))
    return ordered[index]
