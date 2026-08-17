"""Replay corpus manifest build/verify (R1 §8).

The manifest stores only hashes, annotation metadata, profile, hardware and
cache condition.  Real conversation bodies stay in Git-external storage;
the repository may keep synthetic or rewritten cases plus the manifest SHA.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "yuki-replay-manifest/v1"
_SKIP_NAMES = frozenset({".git", "__pycache__", ".DS_Store"})


class ReplayManifestError(ValueError):
    """Raised when a replay manifest cannot be built or verified."""


class CorpusKind(StrEnum):
    SYNTHETIC = "synthetic"
    REWRITTEN = "rewritten"
    PRODUCTION = "production"


class CacheCondition(StrEnum):
    COLD = "cold"
    HOT = "hot"


@dataclass(frozen=True, slots=True)
class ReplayManifestMeta:
    annotation_version: str
    profile: str
    hardware: str
    cache_condition: CacheCondition
    kind: CorpusKind
    config: Mapping[str, str]


def canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def assert_corpus_location(corpus_root: Path, repo_root: Path, *, kind: CorpusKind) -> None:
    """Production corpora must live outside git; synthetic/rewritten may not."""

    if kind is not CorpusKind.PRODUCTION:
        return
    try:
        corpus_root.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return
    raise ReplayManifestError("production replay corpora must live outside the git working tree")


def build_replay_manifest(
    corpus_root: Path,
    *,
    meta: ReplayManifestMeta,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Walk ``corpus_root`` and return a content-free manifest document."""

    root = corpus_root.resolve()
    if not root.is_dir():
        raise ReplayManifestError(f"corpus directory does not exist: {root}")
    if repo_root is not None:
        assert_corpus_location(root, repo_root, kind=meta.kind)

    cases: list[dict[str, Any]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name in _SKIP_NAMES or any(part in _SKIP_NAMES for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        cases.append(
            {
                "id": relative.replace("/", ":"),
                "kind": meta.kind.value,
                "relative_path": relative,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    document: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "annotation_version": meta.annotation_version,
        "profile": meta.profile,
        "hardware": meta.hardware,
        "cache_condition": meta.cache_condition.value,
        "kind": meta.kind.value,
        "config": dict(sorted(meta.config.items())),
        "file_count": len(cases),
        "total_bytes": sum(int(case["bytes"]) for case in cases),
        "cases": cases,
    }
    document["manifest_sha256"] = sha256_text(canonical_json(_without_sha(document)))
    return document


def _without_sha(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "manifest_sha256"}


def manifest_sha256(document: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(_without_sha(document)))


def verify_replay_manifest(
    document: Mapping[str, Any],
    corpus_root: Path,
) -> None:
    """Recompute file hashes and the manifest SHA; raise on any mismatch."""

    if document.get("schema") != MANIFEST_SCHEMA:
        raise ReplayManifestError(f"unsupported replay manifest schema: {document.get('schema')!r}")
    expected = document.get("manifest_sha256")
    actual = manifest_sha256(document)
    if expected != actual:
        raise ReplayManifestError("replay manifest SHA mismatch")
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ReplayManifestError("replay manifest cases must be a list")
    root = corpus_root.resolve()
    for case in cases:
        if not isinstance(case, Mapping):
            raise ReplayManifestError("replay manifest case must be an object")
        relative = str(case.get("relative_path") or "")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ReplayManifestError(f"case path escapes corpus root: {relative}") from exc
        if not path.is_file():
            raise ReplayManifestError(f"missing corpus file: {relative}")
        digest = sha256_file(path)
        if digest != case.get("sha256"):
            raise ReplayManifestError(f"corpus file hash mismatch: {relative}")
        if path.stat().st_size != int(case.get("bytes") or -1):
            raise ReplayManifestError(f"corpus file size mismatch: {relative}")


def dump_manifest(document: Mapping[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_manifest(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ReplayManifestError("replay manifest must be a JSON object")
    return document
