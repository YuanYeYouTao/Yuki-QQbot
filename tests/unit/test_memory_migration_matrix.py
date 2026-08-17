"""Fresh and historical Memory V2 release-candidate schema matrix."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).parents[2]
MATRIX = {
    "2.1.2": "0019",
    "3.0.0a1": "0020",
    "3.0.0a2": "0021",
    "3.0.0b1": "0022",
    "3.0.0b2": "0023",
    "3.0.0rc1": "0024",
    "memory-mutation": "0025",
    "memory-reflection": "0026",
    "yuki-self-memory": "0027",
    "plugin-external-notifications": "0028",
    "chat-event-sender-identity": "0029",
    "memory-quality-self-reflection": "0030",
    "episode-self-reflection": "0031",
    "memory-dream": "0032",
    "memory-dream-recompose": "0033",
}


def _upgrade(path: Path, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path.as_posix()}"
    try:
        command.upgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _schema(path: Path) -> set[tuple[str, str]]:
    with sqlite3.connect(path) as connection:
        return {
            (str(item[0]), str(item[1] or ""))
            for item in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table','index','trigger') AND name NOT LIKE 'sqlite_%'"
            )
        }


def test_fresh_and_upgrade_matrix_have_equivalent_head_schema(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    _upgrade(fresh, "head")
    expected_names = {name for name, _ in _schema(fresh)}
    assert "memory_facts" in expected_names
    assert "memory_facts_fts" in expected_names
    assert "memory_embeddings" in expected_names
    assert "memory_rebuild_runs" in expected_names
    assert "memory_mutation_receipts" in expected_names
    assert "memory_reflection_jobs" in expected_names
    assert "memory_claim_candidates" in expected_names
    assert "memory_self_reflection_runs" in expected_names
    assert "memory_dream_runs" in expected_names
    assert "memory_dream_operations" in expected_names
    assert "memory_dream_operation_results" in expected_names
    assert "memory_self_reflection_results" in expected_names
    assert "memory_dream_cluster_previews" in expected_names
    assert "memory_evidence_compaction_runs" in expected_names
    assert "memory_evidence_compaction_items" in expected_names
    assert "memory_activation_states" in expected_names
    assert "memory_recall_receipts" in expected_names
    assert "memory_recall_items" in expected_names
    with sqlite3.connect(fresh) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0038",)

    for label, revision in MATRIX.items():
        database = tmp_path / f"{label}.db"
        _upgrade(database, revision)
        _upgrade(database, "head")
        names = {name for name, _ in _schema(database)}
        assert names == expected_names, label
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0038",
            )
            assert connection.execute("SELECT COUNT(*) FROM memory_rebuild_runs").fetchone() == (0,)


def test_async_memory_attribution_is_the_current_production_migration() -> None:
    versions = sorted((ROOT / "migrations/versions").glob("*.py"))
    assert versions[-1].name == "0038_revoke_legacy_planner_signal_approvals.py"
