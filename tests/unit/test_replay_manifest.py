"""Replay manifest build/verify (R1 §8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from qq_ai_bot.observability.replay_manifest import (
    CacheCondition,
    CorpusKind,
    ReplayManifestError,
    ReplayManifestMeta,
    build_replay_manifest,
    dump_manifest,
    load_manifest,
    verify_replay_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = ROOT / "tests" / "fixtures" / "runtime_replay"


def _meta(kind: CorpusKind = CorpusKind.SYNTHETIC) -> ReplayManifestMeta:
    return ReplayManifestMeta(
        annotation_version="synthetic/2026-08-17",
        profile="main",
        hardware="test",
        cache_condition=CacheCondition.COLD,
        kind=kind,
        config={"llm.model": "fake"},
    )


def test_build_and_verify_in_repo_synthetic_fixture() -> None:
    document = build_replay_manifest(SYNTHETIC, meta=_meta(), repo_root=ROOT)
    assert document["schema"] == "yuki-replay-manifest/v1"
    assert document["file_count"] == 1
    assert document["cases"][0]["relative_path"] == "cases/private_greeting.md"
    verify_replay_manifest(document, SYNTHETIC)


def test_production_corpus_inside_git_is_rejected() -> None:
    with pytest.raises(ReplayManifestError, match="outside the git working tree"):
        build_replay_manifest(
            SYNTHETIC,
            meta=_meta(CorpusKind.PRODUCTION),
            repo_root=ROOT,
        )


def test_tampered_file_fails_verify(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "case.txt").write_text("hello", encoding="utf-8")
    document = build_replay_manifest(corpus, meta=_meta())
    (corpus / "case.txt").write_text("mutated", encoding="utf-8")
    with pytest.raises(ReplayManifestError, match="hash mismatch"):
        verify_replay_manifest(document, corpus)


def test_round_trip_dump_preserves_sha(tmp_path: Path) -> None:
    document = build_replay_manifest(SYNTHETIC, meta=_meta(), repo_root=ROOT)
    path = tmp_path / "manifest.json"
    dump_manifest(document, path)
    loaded = load_manifest(path)
    verify_replay_manifest(loaded, SYNTHETIC)
    assert loaded["manifest_sha256"] == document["manifest_sha256"]
