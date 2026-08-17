"""Frozen Memory V2 and Plugin API 1.0 contract catalog."""

from __future__ import annotations

import hashlib
import inspect
import json
import tomllib
from pathlib import Path
from typing import Any

from qq_ai_bot.memory.embedding.models import EmbeddingProviderProfile
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryKind,
    MemoryResolutionAction,
    MemoryScopeType,
    MemorySourceType,
    MemoryStateAction,
    MemoryStatus,
)
from qq_ai_bot.memory.models import (
    MemoryContextBlock,
    MemoryEvidenceCreate,
    MemoryFactCreate,
    MemoryQuery,
)
from qq_ai_bot.memory.quality.models import (
    MemoryQualityCase,
    MemoryQualityReport,
    QualityPerformanceReport,
)
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from yuki_plugin_sdk.context import MemoryFacade


def _schema_digest(model: Any) -> str:
    payload = json.dumps(
        model.model_json_schema(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_contract_versions(path: Path) -> dict[str, int | str]:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    return {str(key): value for key, value in raw.items()}


def contract_catalog(version_path: Path) -> dict[str, object]:
    facade_methods = sorted(
        name
        for name, value in inspect.getmembers(MemoryFacade)
        if inspect.isfunction(value) and not name.startswith("_")
    )
    enums = {
        enum.__name__: [item.value for item in enum]
        for enum in (
            MemoryScopeType,
            MemoryKind,
            MemorySourceType,
            MemoryStatus,
            MemoryAuthority,
            MemoryConflictState,
            MemoryFactRelationType,
            MemoryEvidenceRelation,
            MemoryStateAction,
            MemoryInvalidationReason,
            MemoryResolutionAction,
        )
    }
    schemas = {
        name: _schema_digest(model)
        for name, model in {
            "MemoryFactCreate": MemoryFactCreate,
            "MemoryEvidenceCreate": MemoryEvidenceCreate,
            "MemoryQuery": MemoryQuery,
            "MemoryContextBlock": MemoryContextBlock,
            "EmbeddingProviderProfile": EmbeddingProviderProfile,
            "MemoryRebuildSelection": MemoryRebuildSelection,
            "MemoryQualityCase": MemoryQualityCase,
            "MemoryQualityReport": MemoryQualityReport,
            "QualityPerformanceReport": QualityPerformanceReport,
        }.items()
    }
    return {
        "schema_version": "1",
        "versions": load_contract_versions(version_path),
        "schemas": schemas,
        "enums": enums,
        "plugin_api": {
            "version": "2.0",
            "memory_facade_methods": facade_methods,
            "forbidden_capabilities": [
                "raw_vectors",
                "memory_rebuild",
                "quality_dataset",
                "global_production_audit",
                "other_user_evidence",
                "provider_keys",
            ],
        },
    }


def write_contract_snapshot(version_path: Path, output: Path) -> dict[str, object]:
    catalog = contract_catalog(version_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(catalog, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return catalog


def validate_contract_snapshot(version_path: Path, snapshot: Path) -> tuple[bool, str]:
    expected = json.loads(snapshot.read_text(encoding="utf-8"))
    observed = contract_catalog(version_path)
    if expected == observed:
        return True, "memory contracts match the frozen snapshot"
    return False, "memory contract snapshot changed; review and update it explicitly"
