"""Content-free runtime baseline export (R1 §8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.enums import MemoryRecallPurpose, MemoryRetrievalMode
from qq_ai_bot.memory.models import MemoryQueryIntent, MemoryRetrievalResult
from qq_ai_bot.memory.receipt import MemoryRecallRepository
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.observability.runtime_baseline import (
    BASELINE_SCHEMA,
    BaselineExportError,
    BaselineIdentity,
    assert_output_outside_git,
    dump_baseline,
    export_runtime_baseline,
    percentile,
)
from qq_ai_bot.persistence.turn_observations import RuntimeTurnObservationRepository
from qq_ai_bot.planner.repository import PlannerRepository
from qq_ai_bot.runtime.observability import (
    RuntimeTurnCorrelation,
    bind_runtime_turn,
    build_turn_observation,
    new_runtime_turn_id,
)
from qq_ai_bot.runtime.origin import TurnOrigin

ROOT = Path(__file__).resolve().parents[2]
SECRET_CONVERSATION = "group:SECRET-LEAK-882000111"
SECRET_TRIGGER = "platform-SECRET-message"
SECRET_USER = "10001"
T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=45)


def _identity() -> BaselineIdentity:
    return BaselineIdentity(commit="deadbeef", version="3.5.3", alembic_head="0037")


async def _finish_planner(
    database,
    *,
    conversation_key: str,
    decision: str,
    created_at: datetime,
    turn_id: str,
) -> None:
    correlation = RuntimeTurnCorrelation(turn_id=turn_id, origin=TurnOrigin.USER_MESSAGE)
    with bind_runtime_turn(correlation):
        record = await PlannerRepository(database).begin(
            conversation_key=conversation_key,
            trigger_message_id=SECRET_TRIGGER,
            scope_type="group",
            origin="user_message",
            sender_user_id=SECRET_USER,
            group_id="SECRET-LEAK-882000111",
            necessity_score=40.0,
            necessity_reasons={},
            gate_decision="invoke",
            planner_used=True,
            created_at=created_at,
        )
        await PlannerRepository(database).finish(
            record.id,
            planner_decision=decision,
            reason_code=decision,
            delivery_mode="natural_multi" if decision == "reply" else None,
            desired_messages=1 if decision == "reply" else None,
            tool_mode="auto" if decision == "reply" else None,
            confidence=0.8,
            latency_seconds=0.25,
            finished_at=created_at + timedelta(milliseconds=250),
        )


async def _seed(database) -> str:
    turn_id = new_runtime_turn_id()
    await _finish_planner(
        database,
        conversation_key=SECRET_CONVERSATION,
        decision="wait",
        created_at=T0,
        turn_id=turn_id,
    )
    await _finish_planner(
        database,
        conversation_key=SECRET_CONVERSATION,
        decision="reply",
        created_at=T1,
        turn_id=turn_id,
    )
    correlation = RuntimeTurnCorrelation(turn_id=turn_id, origin=TurnOrigin.USER_MESSAGE)
    with bind_runtime_turn(correlation):
        await ModelInvocationRepository(database).record(
            task=ModelTask.PLANNER,
            profile_id="planner",
            provider="fake",
            model="fake-planner",
            success=True,
            prompt_tokens=20,
            completion_tokens=8,
            total_tokens=28,
            cached_prompt_tokens=None,
            latency_seconds=0.12,
            error_category=None,
        )
        await ModelInvocationRepository(database).record(
            task=ModelTask.CHAT_AGENT,
            profile_id="main",
            provider="fake",
            model="fake-chat",
            success=True,
            prompt_tokens=100,
            completion_tokens=40,
            total_tokens=140,
            cached_prompt_tokens=4,
            latency_seconds=0.4,
            error_category=None,
        )
        await MCPRepository(database).record_invocation(
            conversation_key=SECRET_CONVERSATION,
            provider_id="builtin",
            tool_name="memory_search",
            success=True,
            latency_seconds=0.08,
            result_size=64,
            artifact_created=False,
            error_category=None,
        )
        await MemoryRecallRepository(database).record_initial(
            conversation_key=SECRET_CONVERSATION,
            trigger_message_id=SECRET_TRIGGER,
            origin="user_message",
            intent=MemoryQueryIntent(purpose=MemoryRecallPurpose.BACKGROUND),
            result=MemoryRetrievalResult(
                blocks=(),
                hits=(),
                trace_hits=(),
                candidate_count=0,
                selected_count=0,
                query_hash="0" * 64,
                mode=MemoryRetrievalMode.RELEVANT,
            ),
            injected_fact_ids=(),
            retention_days=30,
        )
        await RuntimeTurnObservationRepository(database).record_turn(
            build_turn_observation(
                correlation,
                scope_type="private",
                conversation_key=SECRET_CONVERSATION,
                admission_outcome="chat",
                handled=True,
                sent_messages=2,
                error_category=None,
                total_latency_ms=900,
                now=T1,
            )
        )
    return turn_id


@pytest.mark.asyncio
async def test_export_joins_one_turn_and_stays_content_free(database, tmp_path: Path) -> None:
    await _seed(database)
    document = export_runtime_baseline(
        database.url,
        identity=_identity(),
        since=T0.isoformat(),
        until=datetime.now(UTC).isoformat(),
    )
    assert document["schema"] == BASELINE_SCHEMA
    assert document["baseline"] == {
        "commit": "deadbeef",
        "version": "3.5.3",
        "alembic_head": "0037",
    }
    assert document["sample_size"]["turns"] == 1
    assert document["sample_size"]["planner_runs"] == 2
    assert document["planner"]["decisions"]["wait"] == 1
    assert document["planner"]["decisions"]["reply"] == 1
    assert document["planner"]["wait_then_second_call_ratio"] == 1.0
    assert document["models"]["planner_invocations"] == 1
    assert document["models"]["chat_agent_invocations"] == 1
    assert "tool_selection_invocations" not in document["models"]
    assert "tool_selection_flash_ratio" not in document["models"]
    assert document["tools"]["invocations"] == 1
    assert document["memory"]["recall"]["automatic_like"] == 1
    assert document["turns"]["join_coverage"]["planner_runs"]["ratio"] == 1.0
    assert document["turns"]["private_latency_ms"]["n"] == 1
    assert document["turns"]["tool_scene_latency_ms"]["n"] == 1
    assert {gap["status"] for gap in document["gaps"]} == {"log_approximated"}

    rendered = json.dumps(document, ensure_ascii=False)
    for secret in (SECRET_CONVERSATION, SECRET_TRIGGER, SECRET_USER, "SECRET-LEAK"):
        assert secret not in rendered

    output = tmp_path / "baseline-v1.json"
    dump_baseline(document, output)
    assert output.read_text(encoding="utf-8").startswith("{")


def test_refuses_to_write_inside_the_working_tree(tmp_path: Path) -> None:
    with pytest.raises(BaselineExportError, match="inside the git working tree"):
        assert_output_outside_git(ROOT / "tmp" / "baseline-v1.json", ROOT)
    assert_output_outside_git(tmp_path / "baseline-v1.json", ROOT)


def test_percentile_and_empty_sample() -> None:
    assert percentile([], 50) is None
    assert percentile([10], 95) == 10
    assert percentile([0, 10], 50) == 5
