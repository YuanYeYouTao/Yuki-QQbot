"""Export a content-free 3.5.3 runtime baseline JSON document.

Usage:

    uv run python scripts/refactor_3_6/export_runtime_baseline.py \\
        --database data/bot.db \\
        --output /git-external/baseline-v1.json

The output path must sit outside this repository.  Release / R5 purge
consumes the document after schema, commit and sample-window checks.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from qq_ai_bot import __version__
from qq_ai_bot.observability.runtime_baseline import (
    BaselineExportError,
    BaselineIdentity,
    assert_output_outside_git,
    dump_baseline,
    export_runtime_baseline,
    load_baseline,
    resolve_sqlite_path,
)

ROOT = Path(__file__).resolve().parents[2]


def _git(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="SQLite file or sqlite+aiosqlite URL")
    parser.add_argument(
        "--output",
        required=True,
        help="Git-external UTF-8 JSON path for the baseline document",
    )
    parser.add_argument("--since", default=None, help="Inclusive ISO-8601 lower bound")
    parser.add_argument("--until", default=None, help="Inclusive ISO-8601 upper bound")
    parser.add_argument("--commit", default=None, help="Override baseline commit SHA")
    parser.add_argument("--version", default=None, help="Override baseline product version")
    parser.add_argument(
        "--alembic-head",
        default=None,
        help="Override recorded Alembic head (default: read alembic_version)",
    )
    parser.add_argument(
        "--corpus-manifest-sha256",
        default=None,
        help="Optional SHA of the companion replay manifest",
    )
    parser.add_argument(
        "--repo-root",
        default=str(ROOT),
        help="Working tree used to reject in-repo --output paths",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        database = resolve_sqlite_path(args.database)
        output = Path(args.output)
        assert_output_outside_git(output, Path(args.repo_root))
        document = export_runtime_baseline(
            database,
            identity=BaselineIdentity(
                commit=args.commit or _git(["rev-parse", "HEAD"]),
                version=args.version or __version__,
                alembic_head=args.alembic_head or "",
            ),
            since=args.since,
            until=args.until,
            corpus_manifest_sha256=args.corpus_manifest_sha256,
        )
        dump_baseline(document, output)
        load_baseline(output)
    except BaselineExportError as exc:
        print(f"baseline export failed: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
