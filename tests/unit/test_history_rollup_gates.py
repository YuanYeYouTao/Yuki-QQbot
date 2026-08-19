"""Replay quality gates for conversation history rollup."""

from __future__ import annotations

import pytest

from qq_ai_bot.conversation.history.quality import (
    evaluate_replay_suite,
    load_replay_suite,
    static_prefix_hash,
)


def test_replay_suite_covers_required_scenarios() -> None:
    suite = load_replay_suite()
    ids = {case["id"] for case in suite["cases"]}
    assert {
        "long_tech_discussion",
        "multi_round_code_review",
        "user_corrects_prior_decision",
        "contradictory_statements",
        "multi_person_group",
        "image_visual_summary",
        "external_plugin_event",
        "tool_outcomes",
        "large_tool_json",
        "context_reset",
        "worker_restart",
        "injection_style",
        "secret_like_content",
    }.issubset(ids)
    assert len(suite["cases"]) >= 13


def test_static_prefix_hash_stable_with_session() -> None:
    left = static_prefix_hash(session_text="frontier-a")
    right = static_prefix_hash(session_text="frontier-b")
    assert left == right


@pytest.mark.asyncio
async def test_replay_suite_structural_gates() -> None:
    report = await evaluate_replay_suite()
    assert report["passed"], report["failed"]
    assert report["cross_session_pollution"] == 0
    assert report["summary_raw_overlap"] == 0
    assert report["replacement_errors"] == 0
    assert report["source_coverage"] == 1.0
    assert report["frozen_left_edge_skips"] == 0
