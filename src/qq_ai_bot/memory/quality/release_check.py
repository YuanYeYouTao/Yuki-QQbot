"""Read-only release gate composition for Memory V2."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from sqlalchemy import text

from qq_ai_bot import __version__
from qq_ai_bot.memory.quality.audit import MemoryProductionQualityAudit
from qq_ai_bot.memory.quality.baseline import load_baseline
from qq_ai_bot.memory.quality.contracts import validate_contract_snapshot
from qq_ai_bot.memory.quality.gates import load_gate_configuration
from qq_ai_bot.memory.quality.loader import load_quality_suite
from qq_ai_bot.memory.quality.models import (
    QualitySuiteMode,
    ReleaseCheckItem,
    ReleaseCheckReport,
)
from qq_ai_bot.memory.quality.report import write_reports
from qq_ai_bot.memory.quality.runner import MemoryQualityRunner
from qq_ai_bot.persistence.database import Database

_ALEMBIC_HEAD = "0037"
_EXPECTED_RELEASE_VERSION = "3.5.3"


class MemoryReleaseCheck:
    def __init__(self, repository_root: Path, *, artifact_directory: Path | None = None) -> None:
        self._root = repository_root
        self._artifact_directory = artifact_directory or (
            repository_root / "artifacts/memory-quality"
        )

    async def run(self, *, database_url: str | None = None) -> ReleaseCheckReport:
        items: list[ReleaseCheckItem] = []
        items.append(
            self._item(
                "version",
                __version__ == _EXPECTED_RELEASE_VERSION,
                f"project version is {__version__}",
            )
        )
        head = self._alembic_head()
        items.append(self._item("alembic_head", head == _ALEMBIC_HEAD, f"Alembic head is {head}"))
        try:
            suite = load_quality_suite(self._root / "tests/fixtures/memory_quality/v1")
            items.append(
                ReleaseCheckItem(
                    code="dataset",
                    status="pass",
                    detail=f"{len(suite.cases)} synthetic cases; hash {suite.computed_hash}",
                )
            )
        except (OSError, ValueError) as exc:
            items.append(ReleaseCheckItem(code="dataset", status="fail", detail=str(exc)))
            suite = None
        gates = load_gate_configuration(self._root / "config/memory_quality_gates.toml")
        baseline_path = self._root / "tests/benchmarks/memory_v2/v1/baseline.json"
        if baseline_path.exists():
            baseline = load_baseline(baseline_path)
            baseline_ok = suite is not None and baseline.dataset_hash == suite.computed_hash
            baseline_ok = baseline_ok and baseline.gate_config_hash == gates.file_hash
            detail = (
                "baseline hashes are current"
                if baseline_ok
                else "baseline dataset or gate hash is stale"
            )
            items.append(self._item("baseline", baseline_ok, detail))
            performance = baseline.performance
            performance_ok = (
                performance is not None
                and performance.scenario.users == 100
                and performance.scenario.facts_per_user == 100
                and performance.scenario.chat_events == 100_000
                and performance.populated_fact_count == 10_000
                and performance.model_request_count == 0
            )
            items.append(
                self._item(
                    "performance_baseline",
                    performance_ok,
                    "100 users / 10,000 facts / 100,000 events synthetic baseline is present"
                    if performance_ok
                    else "required synthetic performance baseline is missing or stale",
                )
            )
        else:
            baseline = None
            items.append(
                ReleaseCheckItem(code="baseline", status="fail", detail="baseline is missing")
            )
            items.append(
                ReleaseCheckItem(
                    code="performance_baseline",
                    status="fail",
                    detail="performance baseline is missing",
                )
            )
        if suite is not None:
            quality = await MemoryQualityRunner(
                suite=suite,
                gates=gates,
                repository_root=self._root,
            ).run(mode=QualitySuiteMode.FULL, baseline=baseline)
            write_reports(self._artifact_directory, quality)
            items.append(
                self._item(
                    "quality_report",
                    quality.passed,
                    f"{quality.passed_count}/{quality.case_count} cases passed; "
                    f"{sum(item.passed for item in quality.gates)}/"
                    f"{len(quality.gates)} gates passed",
                )
            )
        contracts_ok, detail = validate_contract_snapshot(
            self._root / "config/memory_contracts.toml",
            self._root / "tests/contracts/memory_v2/contracts.json",
        )
        items.append(self._item("contracts", contracts_ok, detail))
        items.append(self._migration_contract_item())
        items.append(self._plugin_contract_item())
        status = await asyncio.to_thread(
            subprocess.check_output,
            ["git", "status", "--porcelain"],
            cwd=self._root,
            text=True,
            encoding="utf-8",
        )
        clean = not status.strip()
        items.append(
            ReleaseCheckItem(
                code="worktree",
                status="pass" if clean else "warn",
                detail="worktree is clean" if clean else "worktree contains release changes",
            )
        )
        if database_url is None:
            items.append(
                ReleaseCheckItem(
                    code="production_database",
                    status="warn",
                    detail="production audit skipped; pass --database-url explicitly",
                )
            )
        else:
            database = Database(database_url)
            try:
                integrity = await self._database_integrity(database)
                items.append(self._item("database_integrity", integrity, "PRAGMA checks complete"))
                audit = await MemoryProductionQualityAudit(database).run()
                items.append(
                    self._item(
                        "production_audit",
                        audit.error_count == 0,
                        f"content-free audit found {audit.error_count} blocking issues",
                    )
                )
            finally:
                await database.close()
        items.append(
            ReleaseCheckItem(
                code="real_model_benchmark",
                status="warn",
                detail="optional real-model benchmark was not run; deterministic gates use fakes",
            )
        )
        return ReleaseCheckReport(
            version=__version__,
            alembic_head=head,
            generated_at=datetime.now(UTC),
            items=tuple(items),
        )

    def _alembic_head(self) -> str:
        config = Config(str(self._root / "alembic.ini"))
        config.set_main_option("script_location", str(self._root / "migrations"))
        heads = ScriptDirectory.from_config(config).get_heads()
        return heads[0] if len(heads) == 1 else ",".join(sorted(heads))

    def _migration_contract_item(self) -> ReleaseCheckItem:
        versions = {path.name for path in (self._root / "migrations/versions").glob("00*.py")}
        required = {
            "0020_memory_v2_cutover.py",
            "0021_memory_facts_fts.py",
            "0022_memory_embeddings.py",
            "0023_memory_conflicts_lifecycle.py",
            "0024_memory_rebuild.py",
            "0025_memory_mutation_receipts.py",
            "0026_memory_reflection_jobs.py",
            "0027_yuki_self_memory.py",
            "0028_plugin_external_notifications.py",
            "0029_chat_event_sender_identity.py",
            "0030_memory_quality_candidates.py",
            "0031_episode_self_reflection_baseline.py",
            "0032_memory_dream.py",
            "0033_memory_dream_recompose.py",
            "0034_memory_dream_quality_and_evidence_provenance.py",
            "0035_adaptive_memory_lifecycle.py",
            "0036_async_memory_attribution.py",
            "0037_runtime_turn_correlation.py",
        }
        missing = sorted(required - versions)
        return self._item(
            "migration_contract",
            not missing and self._alembic_head() == _ALEMBIC_HEAD,
            f"fresh/upgrade matrix is current through {_ALEMBIC_HEAD}"
            if not missing
            else f"missing migration files: {','.join(missing)}",
        )

    def _plugin_contract_item(self) -> ReleaseCheckItem:
        manifests = tuple((self._root / "plugins").glob("*/plugin.toml")) + tuple(
            (self._root / "examples").glob("**/plugin.toml")
        )
        incompatible: list[str] = []
        for path in manifests:
            with path.open("rb") as stream:
                raw = tomllib.load(stream)
            requires = str(raw.get("yuki_requires", ""))
            try:
                compatible = bool(requires) and SpecifierSet(requires).contains(__version__)
            except InvalidSpecifier:
                compatible = False
            if raw.get("plugin_api") not in {"1.0", "1.1"} or not compatible:
                incompatible.append(path.parent.name)
        return self._item(
            "plugin_api_compatibility",
            not incompatible,
            f"{len(manifests)} manifests declare Plugin API 1.0/1.1 compatibility"
            if not incompatible
            else f"incompatible manifests: {','.join(sorted(incompatible))}",
        )

    @staticmethod
    async def _database_integrity(database: Database) -> bool:
        async with database.sessions() as session:
            integrity = str(await session.scalar(text("PRAGMA integrity_check")))
            foreign_keys = tuple((await session.execute(text("PRAGMA foreign_key_check"))).all())
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        return integrity == "ok" and not foreign_keys and str(revision) == _ALEMBIC_HEAD

    @staticmethod
    def _item(code: str, passed: bool, detail: str) -> ReleaseCheckItem:
        return ReleaseCheckItem(code=code, status="pass" if passed else "fail", detail=detail)


def runtime_versions() -> dict[str, str]:
    return {"python": __import__("sys").version.split()[0], "sqlite": sqlite3.sqlite_version}
