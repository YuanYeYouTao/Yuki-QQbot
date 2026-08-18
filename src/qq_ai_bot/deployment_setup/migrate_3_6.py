"""One-shot 3.6.0 deployment migration (R5 commits 2 and 4).

Commit 2 rewrites ``model_profiles.toml`` to schema v3.  Commit 4 also
rewrites ``.env`` with the frozen Conversation Runtime mapping and exports a
content-free runtime baseline when ``planner_runs`` is still present.
"""

from __future__ import annotations

import sqlite3
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qq_ai_bot import __version__
from qq_ai_bot.deployment_setup.service import (
    EnvironmentDocument,
    SetupPaths,
    SetupValidationError,
    _atomic_write,
    _create_backup,
)
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.profiles import (
    MIGRATE_3_6_COMMAND,
    PROFILE_SCHEMA_VERSION,
    RETIRED_MODEL_ROUTES,
)
from qq_ai_bot.observability.runtime_baseline import (
    BaselineExportError,
    BaselineIdentity,
    assert_output_outside_git,
    dump_baseline,
    export_runtime_baseline,
    load_baseline,
)

_ATTRIBUTION_FALLBACKS = ("utility_structured", "planner")
_COMPACTION_FALLBACKS = ("memory_dream", "utility_structured", "memory_extraction")
_MEMORY_FALLBACKS = (
    ("memory_self_reflection", "memory_extraction"),
    ("memory_consolidation", "memory_extraction"),
    ("memory_dream", "memory_consolidation"),
)
_ENV_RENAMES = {
    "PLANNER_GROUP_ENABLED": "CONVERSATION_AUTONOMOUS_ENABLED",
    "PLANNER_GROUP_DEBOUNCE_SECONDS": "CONVERSATION_AUTONOMOUS_DEBOUNCE_SECONDS",
    "PLANNER_REPLY_NECESSITY_THRESHOLD": "CONVERSATION_AUTONOMOUS_ADMISSION_THRESHOLD",
    "PLANNER_MAX_PENDING_MESSAGES": "CONVERSATION_AUTONOMOUS_BATCH_LIMIT",
    "PLANNER_RECENT_PRESENCE_WINDOW_SECONDS": ("CONVERSATION_AUTONOMOUS_PRESENCE_WINDOW_SECONDS"),
    "PLANNER_INTERRUPT_AUTONOMOUS_ON_NEW_MESSAGE": (
        "CONVERSATION_INTERRUPT_AUTONOMOUS_ON_NEW_MESSAGE"
    ),
    "REPLY_PLAN_HARD_MAX_MESSAGES": "REPLY_HARD_MAX_MESSAGES",
    "SPEECH_PLANNER_ENABLED": "SPEECH_AGENT_EFFECTS_ENABLED",
}
_ENV_DELETES = frozenset(
    {
        "PLANNER_DIRECT_ENABLED",
        "PLANNER_TEMPERATURE",
        "PLANNER_MAX_OUTPUT_TOKENS",
        "PLANNER_TIMEOUT_SECONDS",
        "PLANNER_CONFIDENCE_THRESHOLD",
        "PLANNER_MAX_WAIT_SECONDS",
        "PLANNER_PREFERRED_MESSAGES",
        "PLANNER_RECORD_RUNS",
        "MCP_TOOL_SELECTION_MODE",
        "PLANNER_TOOL_SELECTION_MODE",
        "TOOL_SELECTION_MODE",
    }
)
_EXPORT_REQUIRED_TABLES = (
    "runtime_turn_observations",
    "planner_runs",
    "model_invocations",
    "tool_invocations",
    "memory_recall_receipts",
)


@dataclass(frozen=True, slots=True)
class ModelProfileMigrationResult:
    changed: bool
    backup: Path | None
    materialized_attribution_from: str | None
    removed_routes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EnvMigrationResult:
    changed: bool
    renamed: tuple[tuple[str, str], ...]
    deleted: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BaselineExportResult:
    output: Path | None
    skipped: str | None


@dataclass(frozen=True, slots=True)
class DeploymentMigrationResult:
    profiles: ModelProfileMigrationResult
    env: EnvMigrationResult
    baseline: BaselineExportResult


def migrate_deployment_3_6(
    paths: SetupPaths,
    *,
    baseline_output: Path | None = None,
    repo_root: Path | None = None,
) -> DeploymentMigrationResult:
    """Export a baseline when possible, then rewrite profiles and ``.env``."""

    baseline = _export_runtime_baseline_if_needed(
        paths,
        baseline_output=baseline_output,
        repo_root=repo_root,
    )
    profiles = migrate_deployment_model_profiles(paths)
    env = migrate_deployment_env(paths)
    return DeploymentMigrationResult(profiles=profiles, env=env, baseline=baseline)


def migrate_deployment_model_profiles(paths: SetupPaths) -> ModelProfileMigrationResult:
    """Backup deployment files and rewrite schema v2 profiles to v3."""

    existing = tuple(
        path for path in (paths.env, paths.model_profiles, paths.mcp) if path.is_file()
    )
    if not paths.model_profiles.is_file():
        return ModelProfileMigrationResult(
            changed=False,
            backup=_create_backup(paths, existing),
            materialized_attribution_from=None,
            removed_routes=(),
        )
    try:
        original = paths.model_profiles.read_text(encoding="utf-8")
        document = tomllib.loads(original)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SetupValidationError(f"无法读取 config/model_profiles.toml: {exc}") from exc
    migrated, attribution_source, removed = _migrate_profile_document(document)
    rendered = _emit_profile_document(migrated)
    changed = rendered != original
    backup = _create_backup(paths, existing)
    if changed:
        _atomic_write(paths.model_profiles, rendered.encode("utf-8"), private=False)
    return ModelProfileMigrationResult(
        changed=changed,
        backup=backup,
        materialized_attribution_from=attribution_source,
        removed_routes=removed,
    )


def migrate_deployment_env(paths: SetupPaths) -> EnvMigrationResult:
    """Rename mapped Planner env keys and delete exclusive Planner keys."""

    if not paths.env.is_file():
        return EnvMigrationResult(changed=False, renamed=(), deleted=())
    try:
        original = paths.env.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SetupValidationError(f"无法读取 .env: {exc}") from exc
    rewritten, renamed, deleted = EnvironmentDocument(original).rewrite_keys(
        renames=_ENV_RENAMES,
        deletes=_ENV_DELETES,
    )
    if rewritten == original:
        return EnvMigrationResult(changed=False, renamed=renamed, deleted=deleted)
    _atomic_write(paths.env, rewritten.encode("utf-8"), private=True)
    return EnvMigrationResult(changed=True, renamed=renamed, deleted=deleted)


def _export_runtime_baseline_if_needed(
    paths: SetupPaths,
    *,
    baseline_output: Path | None,
    repo_root: Path | None,
) -> BaselineExportResult:
    database = _deployment_sqlite(paths)
    if database is None:
        return BaselineExportResult(output=None, skipped="database_missing")
    try:
        skipped = _baseline_skip_reason(database)
    except sqlite3.Error as exc:
        raise SetupValidationError(f"无法读取 SQLite 表清单: {exc}") from exc
    if skipped is not None:
        return BaselineExportResult(output=None, skipped=skipped)
    output = baseline_output or (
        paths.root
        / ".yuki/backups/upgrade-3.6"
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        / "baseline-v1.json"
    )
    git_root = repo_root if repo_root is not None else _source_git_root()
    try:
        if git_root is not None:
            assert_output_outside_git(output, git_root)
        document = export_runtime_baseline(
            database,
            identity=BaselineIdentity(
                commit="unknown",
                version=__version__,
                alembic_head="",
            ),
        )
        dump_baseline(document, output)
        load_baseline(output)
    except BaselineExportError as exc:
        raise SetupValidationError(f"runtime baseline 导出失败: {exc}") from exc
    return BaselineExportResult(output=output, skipped=None)


def _deployment_sqlite(paths: SetupPaths) -> Path | None:
    candidate = paths.root / "data/qq_ai_bot.db"
    return candidate if candidate.is_file() else None


def _baseline_skip_reason(path: Path) -> str | None:
    """Skip export unless 0037 correlation tables and columns are present."""

    with _connect_sqlite_readonly(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "planner_runs" not in tables:
            return "planner_runs_absent"
        missing = [name for name in _EXPORT_REQUIRED_TABLES if name not in tables]
        for table in (
            "planner_runs",
            "model_invocations",
            "tool_invocations",
            "memory_recall_receipts",
        ):
            if table not in tables:
                continue
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "runtime_turn_id" not in columns:
                missing.append(f"{table}.runtime_turn_id")
        if missing:
            return "pre_0037_correlation:" + ",".join(missing)
    return None


def _connect_sqlite_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri()
    separator = "&" if "?" in uri else "?"
    return sqlite3.connect(f"{uri}{separator}mode=ro", uri=True)


def _source_git_root() -> Path | None:
    current = Path(__file__).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _migrate_profile_document(
    document: dict[str, Any],
) -> tuple[dict[str, Any], str | None, tuple[str, ...]]:
    profiles = document.get("profiles")
    routes = document.get("routes")
    if not isinstance(profiles, dict) or not isinstance(routes, dict):
        raise SetupValidationError("model_profiles.toml 缺少 profiles 或 routes")
    next_routes = {str(key): str(value) for key, value in routes.items()}
    attribution_source: str | None = None
    if "memory_attribution" not in next_routes:
        for source in _ATTRIBUTION_FALLBACKS:
            if source in next_routes:
                next_routes["memory_attribution"] = next_routes[source]
                attribution_source = source
                break
    for target, source in _MEMORY_FALLBACKS:
        if target not in next_routes and source in next_routes:
            next_routes[target] = next_routes[source]
    if "conversation_compaction" not in next_routes:
        for source in _COMPACTION_FALLBACKS:
            if source in next_routes:
                next_routes["conversation_compaction"] = next_routes[source]
                break
    fill_from = next_routes.get("chat_agent") or next_routes.get("planner")
    if fill_from is not None:
        for task in ModelTask:
            next_routes.setdefault(task.value, fill_from)
    removed = tuple(sorted(name for name in RETIRED_MODEL_ROUTES if name in next_routes))
    for name in removed:
        del next_routes[name]
    missing = [task.value for task in ModelTask if task.value not in next_routes]
    if missing:
        raise SetupValidationError(
            "migrate-3-6 无法补齐模型路由："
            + ", ".join(missing)
            + f"；请先补齐 routes 或运行 Guided Setup。命令：{MIGRATE_3_6_COMMAND}"
        )
    return (
        {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profiles": profiles,
            "routes": next_routes,
        },
        attribution_source,
        removed,
    )


def _emit_profile_document(document: dict[str, Any]) -> str:
    lines = [
        f"schema_version = {int(document['schema_version'])}",
        "",
        "# Generated by qq-ai-bot-cli setup migrate-3-6. Secrets remain in .env.",
    ]
    profiles = document["profiles"]
    if not isinstance(profiles, dict):
        raise SetupValidationError("model_profiles.toml profiles 必须是表")
    for profile_id, payload in profiles.items():
        if not isinstance(payload, dict):
            raise SetupValidationError(f"profile {profile_id} 必须是表")
        lines.append("")
        lines.append(f"[profiles.{profile_id}]")
        for key, value in payload.items():
            lines.append(f"{key} = {_toml_value(value)}")
    lines.extend(("", "[routes]"))
    routes = document["routes"]
    if not isinstance(routes, dict):
        raise SetupValidationError("model_profiles.toml routes 必须是表")
    for task in ModelTask:
        lines.append(f'{task.value} = "{routes[task.value]}"')
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value) if value != int(value) else f"{value:.1f}"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise SetupValidationError(f"无法序列化 model profile 字段: {type(value).__name__}")
