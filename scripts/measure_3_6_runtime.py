"""Measure local 3.6.0 runtime pieces. Do not invent paired-replay numbers."""

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
from datetime import UTC, datetime
from pathlib import Path

from qq_ai_bot.capabilities.models import CapabilityTrustSource
from qq_ai_bot.capabilities.provider import _CORE_METADATA, _CORE_SEARCH_TAGS, _CORE_USE_WHEN
from qq_ai_bot.capabilities.search_document import CapabilitySearchDocument
from qq_ai_bot.capabilities.search_index import FtsCapabilitySearchIndex
from qq_ai_bot.conversation.participation import (
    AdmissionFeatures,
    LocalAutonomousParticipationPolicy,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryScopeType, MemoryTargetRole
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex, build_safe_lexical_query
from qq_ai_bot.memory.models import MemoryEntityTarget
from qq_ai_bot.memory.quality.database import migrate_sqlite_database
from qq_ai_bot.memory.quality.models import QualityPerformanceScenario
from qq_ai_bot.memory.quality.performance import MemoryQualityPerformanceRunner
from qq_ai_bot.observability.runtime_baseline import bootstrap_percentile_ci, percentile
from qq_ai_bot.persistence.database import Database

ROOT = Path(__file__).resolve().parents[1]
WARMUP = 20
SAMPLES = 200
SEARCH_QUERIES = (
    "刚刚说了什么",
    "他以前提过吗",
    "请记住我不喝咖啡",
    "搜一下最新新闻",
    "看看这个网页",
    "发个语音",
    "改成引用回复",
    "我能改什么",
)
ADMISSION_TEXTS = (
    "今晚要不要一起看新番？",
    "666",
    "雪纪你觉得呢",
    "帮我查一下明天天气",
    "哦",
)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _core_documents() -> tuple[CapabilitySearchDocument, ...]:
    return tuple(
        CapabilitySearchDocument(
            capability_id=name,
            model_name=name,
            canonical_name=name,
            namespace_id=namespace,
            description=(_CORE_USE_WHEN.get(name) or (name,))[0],
            aliases=_CORE_SEARCH_TAGS.get(name, ()),
            use_when=_CORE_USE_WHEN.get(name, ()),
            trust_source=CapabilityTrustSource.CORE,
            effect=effect,
            risk=risk,
        )
        for name, (namespace, effect, risk) in _CORE_METADATA.items()
    )


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


def _time_ms(callback) -> float:
    started = time.perf_counter()
    callback()
    return (time.perf_counter() - started) * 1000.0


def _measure_capability() -> dict[str, object]:
    documents = _core_documents()
    cold: list[float] = []
    for index in range(10):
        index_obj = FtsCapabilitySearchIndex()
        cold.append(
            _time_ms(
                lambda current=index_obj, revision=f"cold-{index}": current.rebuild(
                    revision=revision,
                    documents=documents,
                )
            )
        )
    cache = FtsCapabilitySearchIndex()
    cache.rebuild(revision="warm-core", documents=documents)
    for index in range(WARMUP):
        cache.search(SEARCH_QUERIES[index % len(SEARCH_QUERIES)], limit=8)
    warm: list[float] = []
    for index in range(SAMPLES):
        query = SEARCH_QUERIES[index % len(SEARCH_QUERIES)]
        warm.append(_time_ms(lambda current=query: cache.search(current, limit=8)))
    return {
        "document_count": len(documents),
        "index_reuse": "CapabilityIndexCache rebuilds only when registry revision changes",
        "cold_rebuild": _summarize(cold),
        "warm_search": _summarize(warm),
        "queries": list(SEARCH_QUERIES),
    }


def _measure_admission() -> dict[str, object]:
    policy = LocalAutonomousParticipationPolicy(threshold=0, bot_aliases=("雪纪", "yuki"))
    features = [
        AdmissionFeatures(scope_type=ScopeType.GROUP, text=text, pending_message_count=3)
        for text in ADMISSION_TEXTS
    ]
    for item in features:
        policy.evaluate(item)
    samples: list[float] = []
    for index in range(SAMPLES):
        item = features[index % len(features)]
        samples.append(_time_ms(lambda current=item: policy.evaluate(current)))
    return {"warm_evaluate": _summarize(samples), "texts": list(ADMISSION_TEXTS)}


async def _measure_memory_and_combined(
    capability: FtsCapabilitySearchIndex,
    policy: LocalAutonomousParticipationPolicy,
) -> dict[str, object]:
    scenario = QualityPerformanceScenario(
        users=3,
        facts_per_user=4,
        groups=2,
        chat_events=20,
        query_count=8,
        keyset_batch_size=7,
    )
    with tempfile.TemporaryDirectory(prefix="yuki-36-runtime-") as temporary:
        path = Path(temporary) / "runtime.db"
        await asyncio.to_thread(migrate_sqlite_database, ROOT, path)
        await asyncio.to_thread(MemoryQualityPerformanceRunner._populate_core, path, scenario)
        database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
        try:
            lexical = SQLiteMemoryFTSIndex(database)
            target = MemoryEntityTarget(
                role=MemoryTargetRole.CURRENT_PERSON,
                scope_type=MemoryScopeType.PERSON,
                subject_user_id="99010000",
                block_id="current_person",
            )
            query = build_safe_lexical_query("synthetic person 0000 topic 0000", term_limit=16)
            for _ in range(WARMUP):
                await lexical.search(target, query, candidate_limit=10)
            lexical_ms: list[float] = []
            for _ in range(SAMPLES):
                started = time.perf_counter()
                await lexical.search(target, query, candidate_limit=10)
                lexical_ms.append((time.perf_counter() - started) * 1000.0)
            features = AdmissionFeatures(
                scope_type=ScopeType.GROUP,
                text="今晚要不要一起看新番？",
                pending_message_count=3,
            )
            combined_ms: list[float] = []
            for index in range(SAMPLES):
                search_query = SEARCH_QUERIES[index % len(SEARCH_QUERIES)]
                started = time.perf_counter()
                policy.evaluate(features)
                capability.search(search_query, limit=8)
                await lexical.search(target, query, candidate_limit=10)
                combined_ms.append((time.perf_counter() - started) * 1000.0)
        finally:
            await database.close()
    return {
        "shape": {
            "users": scenario.users,
            "facts_per_user": scenario.facts_per_user,
            "chat_events": scenario.chat_events,
            "mode": "lexical_fts_only",
            "embedding": "excluded",
            "vision": "excluded",
            "generative_model": "excluded",
        },
        "lexical_fts": _summarize(lexical_ms),
        "local_pre_model_combined": _summarize(combined_ms),
        "combined_includes": (
            "LocalAutonomousParticipationPolicy.evaluate",
            "FtsCapabilitySearchIndex.search (warm)",
            "SQLiteMemoryFTSIndex.search (lexical)",
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    documents = _core_documents()
    capability = FtsCapabilitySearchIndex()
    capability.rebuild(revision="report-core", documents=documents)
    policy = LocalAutonomousParticipationPolicy(threshold=0, bot_aliases=("雪纪", "yuki"))
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": _git_commit(),
        "hardware": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
        },
        "concurrency": 1,
        "warmup": WARMUP,
        "sample_size": SAMPLES,
        "region": "local-dev-machine",
        "provider_profile": "none-local-only",
        "capability": _measure_capability(),
        "admission": _measure_admission(),
        "memory_and_combined": asyncio.run(_measure_memory_and_combined(capability, policy)),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
