"""Turn correlation propagation and content-free observation rows (R1 commit 3).

Verifies the §10.1 contract: one opaque ``runtime_turn_id`` joins planner
runs, model invocations, tool invocations and memory recall receipts; the
receipt's pre-existing ``turn_id`` keeps its own receipt semantics; nothing
content-bearing reaches any of the new columns or the observation table.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.enums import MemoryRecallPurpose, MemoryRetrievalMode
from qq_ai_bot.memory.models import MemoryQueryIntent, MemoryRetrievalResult
from qq_ai_bot.memory.receipt import MemoryRecallRepository
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.persistence.models import (
    MemoryRecallReceiptModel,
    RuntimeTurnObservationModel,
    ToolInvocationModel,
)
from qq_ai_bot.persistence.turn_observations import RuntimeTurnObservationRepository
from qq_ai_bot.planner.db_models import PlannerRunModel
from qq_ai_bot.planner.repository import PlannerRepository
from qq_ai_bot.runtime.observability import (
    RuntimeTurnCorrelation,
    bind_runtime_turn,
    build_turn_observation,
    claim_runtime_turn_id,
    current_runtime_turn_correlation,
    hash_conversation_key,
    new_runtime_turn_id,
    record_observation_safely,
)
from qq_ai_bot.runtime.origin import TurnOrigin

RAW_CONVERSATION_KEY = "group:882000111"
RAW_TRIGGER_MESSAGE_ID = "platform-message-42"


def _correlation(origin: TurnOrigin = TurnOrigin.USER_MESSAGE) -> RuntimeTurnCorrelation:
    return RuntimeTurnCorrelation(turn_id=new_runtime_turn_id(), origin=origin)


async def _write_all_four(database) -> None:
    await PlannerRepository(database).begin(
        conversation_key=RAW_CONVERSATION_KEY,
        trigger_message_id=RAW_TRIGGER_MESSAGE_ID,
        scope_type="group",
        origin="user_message",
        sender_user_id="10001",
        group_id="882000111",
        necessity_score=50.0,
        necessity_reasons={},
        gate_decision="invoke",
        planner_used=True,
    )
    await ModelInvocationRepository(database).record(
        task=ModelTask.CHAT_AGENT,
        profile_id="main",
        provider="fake",
        model="fake-model",
        success=True,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cached_prompt_tokens=None,
        latency_seconds=0.2,
        error_category=None,
    )
    await MCPRepository(database).record_invocation(
        conversation_key=RAW_CONVERSATION_KEY,
        provider_id="builtin",
        tool_name="memory_search",
        success=True,
        latency_seconds=0.1,
        result_size=128,
        artifact_created=False,
        error_category=None,
    )
    await MemoryRecallRepository(database).record_initial(
        conversation_key=RAW_CONVERSATION_KEY,
        trigger_message_id=RAW_TRIGGER_MESSAGE_ID,
        origin="user_message",
        intent=MemoryQueryIntent(purpose=MemoryRecallPurpose.RECALL),
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


async def _fetch_correlated_rows(database):
    async with database.sessions() as session:
        planner_row = (await session.scalars(select(PlannerRunModel))).one()
        model_row = (await session.scalars(select(ModelInvocationModel))).one()
        tool_row = (await session.scalars(select(ToolInvocationModel))).one()
        receipt_row = (await session.scalars(select(MemoryRecallReceiptModel))).one()
    return planner_row, model_row, tool_row, receipt_row


class TestAmbientCorrelation:
    def test_claim_outside_any_turn_returns_none(self) -> None:
        assert current_runtime_turn_correlation() is None
        assert claim_runtime_turn_id() is None

    def test_claim_marks_correlation_touched(self) -> None:
        correlation = _correlation()
        with bind_runtime_turn(correlation):
            assert correlation.touched is False
            assert claim_runtime_turn_id() == correlation.turn_id
        assert correlation.touched is True
        assert current_runtime_turn_correlation() is None

    @pytest.mark.asyncio
    async def test_child_task_inherits_the_binding(self) -> None:
        correlation = _correlation()

        async def child() -> str | None:
            return claim_runtime_turn_id()

        with bind_runtime_turn(correlation):
            inherited = await asyncio.create_task(child())
        assert inherited == correlation.turn_id

    @pytest.mark.asyncio
    async def test_worker_rebinding_shadows_the_outer_turn(self) -> None:
        outer = _correlation()
        inner = _correlation(TurnOrigin.PLUGIN_BACKGROUND)
        with bind_runtime_turn(outer):
            with bind_runtime_turn(inner):
                assert claim_runtime_turn_id() == inner.turn_id
            assert claim_runtime_turn_id() == outer.turn_id
        assert outer.turn_id != inner.turn_id


class TestWritePointPropagation:
    @pytest.mark.asyncio
    async def test_one_bound_turn_joins_all_four_write_points(self, database) -> None:
        correlation = _correlation()
        with bind_runtime_turn(correlation):
            await _write_all_four(database)

        planner_row, model_row, tool_row, receipt_row = await _fetch_correlated_rows(database)
        assert planner_row.runtime_turn_id == correlation.turn_id
        assert model_row.runtime_turn_id == correlation.turn_id
        assert tool_row.runtime_turn_id == correlation.turn_id
        assert receipt_row.runtime_turn_id == correlation.turn_id
        assert correlation.touched is True

    @pytest.mark.asyncio
    async def test_receipt_turn_id_keeps_receipt_semantics(self, database) -> None:
        correlation = _correlation()
        with bind_runtime_turn(correlation):
            await _write_all_four(database)

        _, _, _, receipt_row = await _fetch_correlated_rows(database)
        # The pre-existing unique receipt id must not be repurposed as the
        # whole-turn id: both exist side by side with different values.
        assert receipt_row.turn_id
        assert receipt_row.turn_id != receipt_row.runtime_turn_id

    @pytest.mark.asyncio
    async def test_unbound_writes_persist_null_like_pre_r1(self, database) -> None:
        await _write_all_four(database)

        for row in await _fetch_correlated_rows(database):
            assert row.runtime_turn_id is None

    @pytest.mark.asyncio
    async def test_no_raw_identifiers_leak_into_correlated_columns(self, database) -> None:
        correlation = _correlation()
        with bind_runtime_turn(correlation):
            await _write_all_four(database)

        planner_row, _, tool_row, receipt_row = await _fetch_correlated_rows(database)
        for value in (
            correlation.turn_id,
            planner_row.conversation_key_hash,
            tool_row.conversation_key_hash,
            receipt_row.conversation_hash,
            receipt_row.trigger_hash,
        ):
            assert RAW_CONVERSATION_KEY not in value
            assert "882000111" not in value
            assert RAW_TRIGGER_MESSAGE_ID not in value


class TestObservationRows:
    @pytest.mark.asyncio
    async def test_record_turn_persists_the_content_free_row(self, database) -> None:
        repository = RuntimeTurnObservationRepository(database)
        correlation = _correlation()
        observation = build_turn_observation(
            correlation,
            scope_type="group",
            conversation_key=RAW_CONVERSATION_KEY,
            admission_outcome="chat",
            handled=True,
            sent_messages=2,
            error_category=None,
            total_latency_ms=1234,
        )
        await repository.record_turn(observation)

        async with database.sessions() as session:
            row = (await session.scalars(select(RuntimeTurnObservationModel))).one()
        assert row.runtime_turn_id == correlation.turn_id
        assert row.origin == "user_message"
        assert row.scope_type == "group"
        assert row.conversation_key_hash == hash_conversation_key(RAW_CONVERSATION_KEY)
        assert row.conversation_key_hash != RAW_CONVERSATION_KEY
        assert row.admission_outcome == "chat"
        assert row.handled is True
        assert row.sent_messages == 2
        assert row.total_latency_ms == 1234
        assert row.expires_at - row.created_at == timedelta(days=30)

    @pytest.mark.asyncio
    async def test_cleanup_expired_drains_in_bounded_batches(self, database) -> None:
        repository = RuntimeTurnObservationRepository(database)
        past = datetime.now(UTC) - timedelta(days=40)
        for _ in range(3):
            observation = build_turn_observation(
                _correlation(),
                scope_type="private",
                conversation_key=None,
                admission_outcome="chat",
                handled=True,
                sent_messages=1,
                error_category=None,
                total_latency_ms=10,
                now=past,
            )
            await repository.record_turn(observation)

        assert await repository.cleanup_expired(limit=2) == 2
        assert await repository.cleanup_expired(limit=2) == 1
        assert await repository.cleanup_expired(limit=2) == 0

    @pytest.mark.asyncio
    async def test_recording_failure_never_breaks_the_turn(self) -> None:
        class ExplodingRecorder:
            async def record_turn(self, observation) -> None:
                raise RuntimeError("storage down")

        observation = build_turn_observation(
            _correlation(),
            scope_type="private",
            conversation_key=None,
            admission_outcome="chat",
            handled=True,
            sent_messages=0,
            error_category=None,
            total_latency_ms=1,
        )
        await record_observation_safely(ExplodingRecorder(), observation)
        await record_observation_safely(None, observation)

    def test_build_turn_observation_clamps_and_bounds(self) -> None:
        observation = build_turn_observation(
            _correlation(),
            scope_type="x" * 40,
            conversation_key=None,
            admission_outcome="y" * 100,
            handled=False,
            sent_messages=-3,
            error_category="z" * 200,
            total_latency_ms=-1,
            retention_days=0,
        )
        assert len(observation.scope_type) == 16
        assert observation.conversation_key_hash is None
        assert len(observation.admission_outcome or "") == 64
        assert observation.sent_messages == 0
        assert len(observation.error_category or "") == 128
        assert observation.total_latency_ms == 0
        assert observation.expires_at - observation.created_at == timedelta(days=1)
