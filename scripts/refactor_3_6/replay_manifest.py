"""Build or verify a content-free replay corpus manifest.

Usage:

    uv run python scripts/refactor_3_6/replay_manifest.py build \\
        --corpus /git-external/replay-corpus \\
        --output /git-external/replay-manifest.json \\
        --annotation-version 2026-08-17 \\
        --profile main \\
        --hardware "cpu=8,ram=16g" \\
        --cache-condition cold \\
        --kind production

    uv run python scripts/refactor_3_6/replay_manifest.py verify \\
        --corpus /git-external/replay-corpus \\
        --manifest /git-external/replay-manifest.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Walk a corpus directory and write a manifest")
    build.add_argument("--corpus", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--annotation-version", required=True)
    build.add_argument("--profile", required=True)
    build.add_argument("--hardware", required=True)
    build.add_argument(
        "--cache-condition",
        required=True,
        choices=[item.value for item in CacheCondition],
    )
    build.add_argument("--kind", choices=[item.value for item in CorpusKind], default="synthetic")
    build.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Repeatable content-free config fingerprint entry",
    )
    build.add_argument("--repo-root", default=str(ROOT))

    verify = sub.add_parser("verify", help="Recompute file hashes and the manifest SHA")
    verify.add_argument("--corpus", required=True)
    verify.add_argument("--manifest", required=True)
    return parser.parse_args(argv)


def _parse_config(items: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ReplayManifestError(f"config entry must be KEY=VALUE: {item!r}")
        key, value = item.split("=", 1)
        if not key.strip():
            raise ReplayManifestError("config key must not be empty")
        parsed[key.strip()] = value
    return parsed


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "build":
            document = build_replay_manifest(
                Path(args.corpus),
                meta=ReplayManifestMeta(
                    annotation_version=args.annotation_version,
                    profile=args.profile,
                    hardware=args.hardware,
                    cache_condition=CacheCondition(args.cache_condition),
                    kind=CorpusKind(args.kind),
                    config=_parse_config(args.config),
                ),
                repo_root=Path(args.repo_root),
            )
            output = Path(args.output)
            dump_manifest(document, output)
            print(document["manifest_sha256"])
            print(output)
            return 0
        document = load_manifest(Path(args.manifest))
        verify_replay_manifest(document, Path(args.corpus))
        print(document["manifest_sha256"])
        return 0
    except ReplayManifestError as exc:
        print(f"replay manifest failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
