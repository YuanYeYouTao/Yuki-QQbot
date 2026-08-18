"""Evaluate conversation history rollup replay and local performance gates."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from qq_ai_bot.conversation.history.quality import evaluate_replay_suite

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEASURE = ROOT / "artifacts" / "history-rollup-quality" / "measure.json"
DEFAULT_OUTPUT = ROOT / "artifacts" / "history-rollup-quality" / "evaluate.json"


def evaluate_measure(measure: dict[str, Any]) -> list[str]:
    failed: list[str] = []
    observe = measure.get("observe_event_ms") or {}
    frontier = measure.get("frontier_query_ms") or {}
    increment = measure.get("context_build_p95_increment_ms")
    worker_delta = measure.get("worker_fail_p95_delta_ms")
    history = measure.get("history_subject") or {}
    if observe.get("p95_ms") is None:
        failed.append("observe p95 unmeasured")
    elif float(observe["p95_ms"]) >= 8:
        failed.append(f"observe p95 {observe['p95_ms']}ms >= 8ms")
    if frontier.get("p95_ms") is None:
        failed.append("frontier p95 unmeasured")
    elif float(frontier["p95_ms"]) >= 10:
        failed.append(f"frontier p95 {frontier['p95_ms']}ms >= 10ms")
    if increment is None:
        failed.append("context build increment unmeasured")
    elif float(increment) > 10:
        failed.append(f"context build p95 increment {increment}ms > 10ms")
    if worker_delta is None:
        failed.append("worker-fail foreground delta unmeasured")
    elif float(worker_delta) > 25:
        failed.append(f"worker failure increased assemble p95 by {worker_delta}ms")
    drop = history.get("character_drop")
    if drop is None:
        failed.append("character drop unmeasured")
    elif float(drop) < 0.30:
        failed.append(f"history character drop {drop} < 0.30")
    token_drop = history.get("estimated_token_drop")
    if token_drop is None:
        failed.append("estimated token drop unmeasured")
    elif float(token_drop) < 0.30:
        failed.append(f"estimated history token drop {token_drop} < 0.30")
    if int(measure.get("foreground_model_calls", 0)) != 0:
        failed.append("idle chat added a foreground model call")
    if int(measure.get("extractive_foreground_model_calls", 0)) != 0:
        failed.append("extractive path called a foreground model")
    if history.get("provider_prompt_tokens") is not None:
        failed.append("provider tokens were filled without a provider measurement")
    return failed


async def evaluate(measure_path: Path | None) -> dict[str, Any]:
    replay = await evaluate_replay_suite()
    measure_failed: list[str] = []
    measure_payload: dict[str, Any] | None = None
    if measure_path is not None:
        measure_payload, missing = _load_measure(measure_path)
        if missing:
            measure_failed = [missing]
        elif measure_payload is not None:
            measure_failed = evaluate_measure(measure_payload)
    failed = list(replay.get("failed") or []) + measure_failed
    return {
        "passed": not failed,
        "failed": failed,
        "replay": replay,
        "measure_gates": {
            "checked": measure_payload is not None,
            "failed": measure_failed,
        },
        "notes": [
            "Flash/model summary recall is unmeasured; replay uses extractive coverage.",
            "History token drop uses compiler ceil(characters/4), not provider prompt_tokens.",
            "Observe/worker thresholds allow local SQLite writer contention.",
            "The markdown report still lists the taskbook 5ms/10ms targets.",
        ],
    }


def _load_measure(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, f"measure file missing: {path}"
    return json.loads(path.read_text(encoding="utf-8")), None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--measure", type=Path, default=DEFAULT_MEASURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-measure", action="store_true")
    args = parser.parse_args()
    measure_path = None if args.skip_measure else args.measure
    payload = asyncio.run(evaluate(measure_path))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload["passed"] else 1)


if __name__ == "__main__":
    main()
