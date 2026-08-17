"""Content-free 3.5.3 runtime baseline aggregation (R1 §8).

Reads a SQLite file through the sqlite3 module and emits a schema-versioned
JSON document.  The document contains only enums, counts, hashes, latencies
and token totals — never prompts, message bodies, tool arguments, memory
text or ref lists.

Metrics that still live only in process counters
(``ToolKernelMetrics.first_round_tool_hits`` / ``request_tools_*``) or on
tables that 0037 deliberately did not correlate are listed under ``gaps``
with ``status=log_approximated`` instead of being invented.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASELINE_SCHEMA = "yuki-runtime-baseline/v1"
BOOTSTRAP_SAMPLES = 1000
BOOTSTRAP_SEED = 36
_REQUIRED_CORRELATED_TABLES = (
    "runtime_turn_observations",
    "model_invocations",
    "tool_invocations",
    "memory_recall_receipts",
)
_OPTIONAL_HISTORICAL_TABLES = ("planner_runs",)
_CONTENT_BEARING_KEYS = frozenset(
    {
        "prompt",
        "content",
        "text",
        "arguments",
        "ref",
        "refs",
        "message",
        "body",
        "excerpt",
        "conversation_key",
        "trigger_message_id",
    }
)


class BaselineExportError(ValueError):
    """Raised when a baseline cannot be produced without violating the contract."""


@dataclass(frozen=True, slots=True)
class BaselineIdentity:
    commit: str
    version: str
    alembic_head: str


@dataclass(frozen=True, slots=True)
class SampleWindow:
    since: str
    until: str
    source: str


def resolve_sqlite_path(database: str | Path) -> Path:
    """Accept a filesystem path or a SQLAlchemy sqlite URL."""

    raw = str(database).strip()
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if raw.startswith(prefix):
            raw = raw.removeprefix(prefix)
    if raw in {":memory:", ""}:
        raise BaselineExportError("baseline export requires a persistent SQLite file")
    path = Path(raw)
    if not path.is_file():
        raise BaselineExportError(f"database file does not exist: {path}")
    return path


def assert_output_outside_git(output: Path, repo_root: Path) -> None:
    """Refuse to write a baseline into the working tree or a release bundle."""

    resolved = output.resolve()
    root = repo_root.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return
    raise BaselineExportError(
        "refusing to write a runtime baseline inside the git working tree "
        f"({relative.as_posix()}); pass a Git-external --output path"
    )


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolated percentile; ``None`` when the sample is empty."""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return float(ordered[low] * (1.0 - fraction) + ordered[high] * fraction)


def bootstrap_percentile_ci(
    values: Sequence[float],
    p: float,
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Percentile bootstrap CI for one percentile statistic."""

    if not values:
        return {"p": p, "low": None, "high": None, "samples": 0, "method": "percentile_bootstrap"}
    rng = random.Random(seed)
    size = len(values)
    estimates: list[float] = []
    for _ in range(samples):
        draw = [values[rng.randrange(size)] for _ in range(size)]
        estimate = percentile(draw, p)
        if estimate is not None:
            estimates.append(estimate)
    estimates.sort()
    if not estimates:
        return {
            "p": p,
            "low": None,
            "high": None,
            "samples": samples,
            "method": "percentile_bootstrap",
        }
    low_index = min(len(estimates) - 1, max(0, int(0.025 * (len(estimates) - 1))))
    high_index = min(len(estimates) - 1, max(0, int(0.975 * (len(estimates) - 1))))
    return {
        "p": p,
        "low": estimates[low_index],
        "high": estimates[high_index],
        "samples": samples,
        "method": "percentile_bootstrap",
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _counts(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


def _latency_block(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p50_ci": bootstrap_percentile_ci(values, 50),
        "p95_ci": bootstrap_percentile_ci(values, 95),
    }


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _require_tables(connection: sqlite3.Connection) -> None:
    names = _table_names(connection)
    missing = [table for table in _REQUIRED_CORRELATED_TABLES if table not in names]
    if missing:
        raise BaselineExportError(
            "database is missing 0037 correlation tables "
            f"({', '.join(missing)}); run alembic upgrade head first"
        )
    for table in _REQUIRED_CORRELATED_TABLES:
        if table == "runtime_turn_observations":
            continue
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "runtime_turn_id" not in columns:
            raise BaselineExportError(
                f"{table} is missing runtime_turn_id; run alembic upgrade head first"
            )


def _alembic_head(connection: sqlite3.Connection) -> str:
    names = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "alembic_version" not in names:
        return "unknown"
    row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return str(row[0]) if row else "unknown"


def _normalize_window_bound(value: str) -> str:
    """Compare ISO-8601 CLI bounds with SQLAlchemy's SQLite DateTime strings.

    SQLAlchemy typically persists timezone-aware values as
    ``YYYY-MM-DD HH:MM:SS.ffffff``.  A CLI bound that still contains ``T``
    would sort *after* those rows and silently empty the window.
    """

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _window_bounds(
    connection: sqlite3.Connection,
    *,
    since: str | None,
    until: str | None,
) -> SampleWindow:
    if since and until:
        return SampleWindow(
            since=_normalize_window_bound(since),
            until=_normalize_window_bound(until),
            source="explicit",
        )
    row = connection.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM runtime_turn_observations"
    ).fetchone()
    observed_min, observed_max = (row[0], row[1]) if row else (None, None)
    if observed_min is None or observed_max is None:
        if "planner_runs" in _table_names(connection):
            fallback = connection.execute(
                "SELECT MIN(created_at), MAX(created_at) FROM planner_runs"
            ).fetchone()
            observed_min = observed_min or (fallback[0] if fallback else None)
            observed_max = observed_max or (fallback[1] if fallback else None)
            source = "planner_runs.created_at"
        else:
            source = "runtime_turn_observations.created_at"
    else:
        source = "runtime_turn_observations.created_at"
    if observed_min is None or observed_max is None:
        now = _normalize_window_bound(datetime.now(UTC).isoformat())
        return SampleWindow(
            since=_normalize_window_bound(since) if since else now,
            until=_normalize_window_bound(until) if until else now,
            source="empty",
        )
    return SampleWindow(
        since=_normalize_window_bound(since) if since else str(observed_min),
        until=_normalize_window_bound(until) if until else str(observed_max),
        source=source,
    )


def _in_window(column: str = "created_at") -> str:
    return f"{column} >= ? AND {column} <= ?"


def _fetchall(
    connection: sqlite3.Connection,
    statement: str,
    window: SampleWindow,
) -> list[sqlite3.Row]:
    return list(connection.execute(statement, (window.since, window.until)))


def _join_coverage(
    connection: sqlite3.Connection,
    table: str,
    window: SampleWindow,
) -> dict[str, int | float | None]:
    row = connection.execute(
        f"SELECT COUNT(*), SUM(CASE WHEN runtime_turn_id IS NOT NULL THEN 1 ELSE 0 END) "
        f"FROM {table} WHERE {_in_window()}",
        (window.since, window.until),
    ).fetchone()
    total = int(row[0] or 0) if row else 0
    joined = int(row[1] or 0) if row else 0
    return {"total": total, "with_runtime_turn_id": joined, "ratio": _ratio(joined, total)}


def _wait_second_call_ratio(rows: Sequence[sqlite3.Row]) -> float | None:
    """Share of WAIT runs that are followed by another run in the same conversation."""

    by_conversation: dict[str, list[tuple[str, str | None]]] = {}
    for row in rows:
        by_conversation.setdefault(str(row["conversation_key_hash"]), []).append(
            (str(row["created_at"]), row["planner_decision"])
        )
    waits = 0
    followed = 0
    for items in by_conversation.values():
        items.sort(key=lambda item: item[0])
        for index, (_created, decision) in enumerate(items):
            if decision != "wait":
                continue
            waits += 1
            if index + 1 < len(items):
                followed += 1
    return _ratio(followed, waits)


def export_runtime_baseline(
    database: str | Path,
    *,
    identity: BaselineIdentity,
    since: str | None = None,
    until: str | None = None,
    corpus_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Aggregate one content-free baseline document from a SQLite file."""

    path = resolve_sqlite_path(database)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        _require_tables(connection)
        window = _window_bounds(connection, since=since, until=until)
        document = _aggregate(connection, identity=identity, window=window)
    finally:
        connection.close()
    if corpus_manifest_sha256:
        document["corpus_manifest_sha256"] = corpus_manifest_sha256
    assert_content_free(document)
    return document


def _aggregate(
    connection: sqlite3.Connection,
    *,
    identity: BaselineIdentity,
    window: SampleWindow,
) -> dict[str, Any]:
    turns = _fetchall(
        connection,
        "SELECT runtime_turn_id, origin, scope_type, admission_outcome, handled, "
        "sent_messages, error_category, total_latency_ms FROM runtime_turn_observations "
        f"WHERE {_in_window()}",
        window,
    )
    names = _table_names(connection)
    planner_rows = (
        _fetchall(
            connection,
            "SELECT runtime_turn_id, conversation_key_hash, planner_decision, origin, "
            "scope_type, latency_seconds, fallback_used, interrupted, planner_used, "
            "tool_mode, gate_decision, created_at FROM planner_runs "
            f"WHERE {_in_window()}",
            window,
        )
        if "planner_runs" in names
        else []
    )
    model_rows = _fetchall(
        connection,
        "SELECT runtime_turn_id, task, profile_id, provider, model, success, "
        "prompt_tokens, completion_tokens, total_tokens, cached_prompt_tokens, "
        "latency_seconds FROM model_invocations "
        f"WHERE {_in_window()}",
        window,
    )
    tool_rows = _fetchall(
        connection,
        "SELECT runtime_turn_id, provider_id, tool_name, success, latency_seconds "
        "FROM tool_invocations "
        f"WHERE {_in_window()}",
        window,
    )
    recall_rows = _fetchall(
        connection,
        f"SELECT runtime_turn_id, mode, purpose FROM memory_recall_receipts WHERE {_in_window()}",
        window,
    )
    mutation_count = _count_optional(connection, "memory_mutation_receipts", window)
    tool_receipt_count = _count_optional(connection, "memory_tool_receipts", window)

    turn_ids_with_tools = {
        str(row["runtime_turn_id"]) for row in tool_rows if row["runtime_turn_id"]
    }
    private_latencies = [
        int(row["total_latency_ms"]) for row in turns if row["scope_type"] == "private"
    ]
    tool_scene_latencies = [
        int(row["total_latency_ms"])
        for row in turns
        if row["runtime_turn_id"] in turn_ids_with_tools
    ]
    all_latencies = [int(row["total_latency_ms"]) for row in turns]

    models_by_task: dict[str, dict[str, Any]] = {}
    for task, group in _group(model_rows, "task"):
        models_by_task[task] = {
            "invocations": len(group),
            "successes": sum(1 for row in group if row["success"]),
            "prompt_tokens": sum(int(row["prompt_tokens"] or 0) for row in group),
            "completion_tokens": sum(int(row["completion_tokens"] or 0) for row in group),
            "total_tokens": sum(int(row["total_tokens"] or 0) for row in group),
            "cached_prompt_tokens": sum(int(row["cached_prompt_tokens"] or 0) for row in group),
            "latency_seconds": _latency_block([float(row["latency_seconds"]) for row in group]),
        }

    profiles = sorted(
        {(str(row["profile_id"]), str(row["provider"]), str(row["model"])) for row in model_rows}
    )
    planner_used = sum(1 for row in planner_rows if row["planner_used"])
    wait_rows = [row for row in planner_rows if row["planner_decision"] == "wait"]

    return {
        "schema": BASELINE_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "baseline": {
            "commit": identity.commit,
            "version": identity.version,
            "alembic_head": identity.alembic_head or _alembic_head(connection),
        },
        "sample_window": {
            "since": window.since,
            "until": window.until,
            "source": window.source,
        },
        "provider_profile": {
            "profiles": [
                {"profile_id": profile, "provider": provider, "model": model}
                for profile, provider, model in profiles
            ]
        },
        "sample_size": {
            "turns": len(turns),
            "planner_runs": len(planner_rows),
            "model_invocations": len(model_rows),
            "tool_invocations": len(tool_rows),
            "memory_recall_receipts": len(recall_rows),
        },
        "failure_definition": {
            "turn_error": "runtime_turn_observations.error_category IS NOT NULL",
            "model_failure": "model_invocations.success = 0",
            "tool_failure": "tool_invocations.success = 0",
        },
        "turns": {
            "count": len(turns),
            "by_origin": _counts(Counter(str(row["origin"]) for row in turns)),
            "by_admission_outcome": _counts(
                Counter(str(row["admission_outcome"] or "unknown") for row in turns)
            ),
            "handled_ratio": _ratio(sum(1 for row in turns if row["handled"]), len(turns)),
            "error_ratio": _ratio(sum(1 for row in turns if row["error_category"]), len(turns)),
            "sent_messages": {
                "total": sum(int(row["sent_messages"]) for row in turns),
                "p50": percentile([int(row["sent_messages"]) for row in turns], 50),
            },
            "latency_ms": _latency_block(all_latencies),
            "private_latency_ms": _latency_block(private_latencies),
            "tool_scene_latency_ms": _latency_block(tool_scene_latencies),
            "join_coverage": {
                "planner_runs": (
                    _join_coverage(connection, "planner_runs", window)
                    if "planner_runs" in names
                    else {"total": 0, "with_runtime_turn_id": 0, "ratio": None}
                ),
                "model_invocations": _join_coverage(connection, "model_invocations", window),
                "tool_invocations": _join_coverage(connection, "tool_invocations", window),
                "memory_recall_receipts": _join_coverage(
                    connection, "memory_recall_receipts", window
                ),
            },
        },
        "planner": {
            "runs": len(planner_rows),
            "planner_used": planner_used,
            "planner_used_ratio": _ratio(planner_used, len(planner_rows)),
            "decisions": _counts(
                Counter(str(row["planner_decision"] or "unknown") for row in planner_rows)
            ),
            "gate_decisions": _counts(Counter(str(row["gate_decision"]) for row in planner_rows)),
            "wait_ratio": _ratio(len(wait_rows), len(planner_rows)),
            "wait_then_second_call_ratio": _wait_second_call_ratio(planner_rows),
            "fallback_used_ratio": _ratio(
                sum(1 for row in planner_rows if row["fallback_used"]), len(planner_rows)
            ),
            "interrupted_ratio": _ratio(
                sum(1 for row in planner_rows if row["interrupted"]), len(planner_rows)
            ),
            "latency_seconds": _latency_block(
                [float(row["latency_seconds"]) for row in planner_rows]
            ),
        },
        "models": {
            "by_task": models_by_task,
            "planner_invocations": models_by_task.get("planner", {}).get("invocations", 0),
            "chat_agent_invocations": models_by_task.get("chat_agent", {}).get("invocations", 0),
        },
        "tools": {
            "invocations": len(tool_rows),
            "success_ratio": _ratio(sum(1 for row in tool_rows if row["success"]), len(tool_rows)),
            "latency_seconds": _latency_block([float(row["latency_seconds"]) for row in tool_rows]),
            "by_provider": _counts(Counter(str(row["provider_id"]) for row in tool_rows)),
        },
        "memory": {
            "recall": {
                "receipts": len(recall_rows),
                "by_mode": _counts(Counter(str(row["mode"]) for row in recall_rows)),
                "by_purpose": _counts(Counter(str(row["purpose"]) for row in recall_rows)),
                "automatic_like": sum(1 for row in recall_rows if row["purpose"] == "background"),
            },
            "tool_receipts": {
                "count": tool_receipt_count,
                "joinable": False,
            },
            "mutations": {
                "count": mutation_count,
                "joinable": False,
            },
        },
        "gaps": _baseline_gaps(
            planner_runs_present=all(table in names for table in _OPTIONAL_HISTORICAL_TABLES)
        ),
        "corpus_manifest_sha256": None,
    }


def _baseline_gaps(*, planner_runs_present: bool) -> list[dict[str, str]]:
    gaps = [
        {
            "metric": "first_round_tool_hit_rate",
            "status": "log_approximated",
            "reason": "ToolKernelMetrics.first_round_tool_hits is an in-process counter",
        },
        {
            "metric": "request_tools_usage_and_zero_result_rate",
            "status": "log_approximated",
            "reason": "request_tools counters are process-local and have no history table",
        },
        {
            "metric": "tool_schema_token_distribution",
            "status": "log_approximated",
            "reason": "schema token sizes are not persisted on tool_invocations",
        },
        {
            "metric": "memory_tool_and_mutation_turn_join",
            "status": "log_approximated",
            "reason": (
                "0037 does not add runtime_turn_id to memory_tool_receipts "
                "or memory_mutation_receipts; counts are window totals only"
            ),
        },
    ]
    if not planner_runs_present:
        gaps.append(
            {
                "metric": "planner_runs",
                "status": "optional_historical",
                "reason": "planner_runs was dropped by 0040; 3.6.0 exports record an empty gap",
            }
        )
    return gaps


def _count_optional(connection: sqlite3.Connection, table: str, window: SampleWindow) -> int | None:
    names = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if table not in names:
        return None
    row = connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {_in_window()}",
        (window.since, window.until),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _group(rows: Sequence[sqlite3.Row], column: str) -> list[tuple[str, list[sqlite3.Row]]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row[column]), []).append(row)
    return sorted(grouped.items())


def assert_content_free(document: Mapping[str, Any]) -> None:
    """Fail closed if a content-bearing key sneaks into the document."""

    stack: list[Any] = [document]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, value in current.items():
                if str(key) in _CONTENT_BEARING_KEYS:
                    raise BaselineExportError(
                        f"baseline document must not contain content-bearing key {key!r}"
                    )
                stack.append(value)
        elif isinstance(current, list | tuple):
            stack.extend(current)


def dump_baseline(document: Mapping[str, Any], output: Path) -> None:
    """Write UTF-8 JSON with a trailing newline; parents must be writable."""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_baseline(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise BaselineExportError("baseline document must be a JSON object")
    if document.get("schema") != BASELINE_SCHEMA:
        raise BaselineExportError(f"unsupported baseline schema: {document.get('schema')!r}")
    assert_content_free(document)
    return document
