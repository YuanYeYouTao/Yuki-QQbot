"""Post-delivery memory attribution and exposure isolation tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import (
    ChatRequest,
    ChatResponse,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.memory.attribution import (
    MemoryAttributionJob,
    MemoryAttributionWorker,
    MemoryExposure,
    MemoryExposureRegistry,
    MemoryExposureSource,
)
from qq_ai_bot.memory.enums import MemoryAccessMode, MemoryContextMode, MemoryRecallPurpose
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import MemoryQueryIntent
from qq_ai_bot.memory.receipt import MemoryRecallTurn
from qq_ai_bot.memory.runtime.turn_session import TurnMemorySession, empty_retrieval
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelExecutionPriority,
    ModelProtocol,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.authority import TurnAuthority
from qq_ai_bot.runtime.contracts import DeliverySummary
from qq_ai_bot.runtime.delivery import DeliveryStatus
from qq_ai_bot.runtime.origin import TurnOrigin as RuntimeTurnOrigin


class AttributionExecutor:
    def __init__(self, refs: tuple[str, ...]) -> None:
        self.refs = refs
        self.priorities: list[ModelExecutionPriority] = []

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
    ) -> ChatResponse:
        assert task is ModelTask.MEMORY_ATTRIBUTION
        self.priorities.append(priority)
        assert "user_question" in json.loads(request.messages[-1].content)
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(
                ToolCall(
                    id="attribution-result",
                    function=ToolFunction(
                        name="emit_result",
                        arguments=json.dumps({"used_refs": self.refs}),
                    ),
                ),
            ),
        )

    def model_name(self, task: ModelTask) -> str:
        del task
        return "flash"

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        del task
        return StructuredOutputMode.FUNCTION_TOOL

    def protocol(self, task: ModelTask) -> ModelProtocol:
        del task
        return ModelProtocol.CHAT_COMPLETIONS

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]:
        del task
        return frozenset({ModelCapability.STRUCTURED_OUTPUT})


@dataclass
class AttributionContext:
    committed: asyncio.Event = field(default_factory=asyncio.Event)
    used: list[tuple[str, tuple[int, ...]]] = field(default_factory=list)
    reinforced: list[tuple[str, tuple[int, ...]]] = field(default_factory=list)

    async def mark_attributed_used(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
    ) -> tuple[int, ...]:
        self.used.append((turn_id, fact_ids))
        return fact_ids

    async def reinforce_usage(self, **kwargs: Any) -> tuple[int, ...]:
        turn_id = cast(str, kwargs["turn_id"])
        fact_ids = cast(tuple[int, ...], kwargs["fact_ids"])
        self.reinforced.append((turn_id, fact_ids))
        self.committed.set()
        return fact_ids


@dataclass
class AttributionQueue:
    jobs: list[MemoryAttributionJob] = field(default_factory=list)

    def enqueue(self, job: MemoryAttributionJob) -> bool:
        self.jobs.append(job)
        return True


def test_adaptive_metrics_include_mutation_access_and_terminal_outcomes() -> None:
    metrics = MemoryLifecycleMetrics()

    metrics.record_access(MemoryAccessMode.MUTATION)
    metrics.record_mutation_turn_outcome("attempted")
    metrics.record_mutation_turn_outcome("not_found")

    snapshot = metrics.adaptive_snapshot()
    assert snapshot["memory_access_mutation"] == 1
    assert snapshot["memory_mutation_turn_attempted_count"] == 1
    assert snapshot["memory_mutation_turn_not_found_count"] == 1
    assert snapshot["memory_mutation_turn_committed_count"] == 0


def _exposure(fact_id: int, *, source: MemoryExposureSource) -> MemoryExposure:
    return MemoryExposure(
        memory_ref=f"M{fact_id}",
        fact_id=fact_id,
        kind="preference",
        category="food",
        content="偏好深烘咖啡" if fact_id == 1 else "偏好浅烘咖啡",
        target_role="current_person",
        source=source,
    )


def test_exposure_registry_prefers_actual_tool_payload() -> None:
    registry = MemoryExposureRegistry((_exposure(1, source=MemoryExposureSource.AUTOMATIC),))
    captured = registry.register_tool_payload(
        {
            "facts": [
                {
                    "memory_ref": "M2",
                    "kind": "preference",
                    "category": "food",
                    "content": "偏好浅烘咖啡",
                }
            ]
        }
    )

    assert captured == (2,)
    assert [item.memory_ref for item in registry.snapshot()] == ["M2", "M1"]


@pytest.mark.asyncio
async def test_worker_marks_only_flash_attributed_refs(database: Database) -> None:
    runtime_config = RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    )
    runtime = await runtime_config.snapshot(user_id="1001")
    executor = AttributionExecutor(("M1",))
    context = AttributionContext()
    metrics = MemoryLifecycleMetrics()
    worker = MemoryAttributionWorker(
        models=executor,
        memory_context=cast(Any, context),
        runtime_config=runtime_config,
        metrics=metrics,
    )
    job = MemoryAttributionJob(
        turn_id="turn-1",
        user_id="1001",
        group_id=None,
        user_question="我偏好哪一种咖啡？",
        final_response="你偏好深烘咖啡。",
        intent=MemoryQueryIntent(
            mode=MemoryContextMode.HYBRID,
            purpose=MemoryRecallPurpose.RECALL,
        ),
        exposures=(
            _exposure(1, source=MemoryExposureSource.AUTOMATIC),
            _exposure(2, source=MemoryExposureSource.AUTOMATIC),
        ),
        runtime=runtime,
        enqueued_at=datetime.now(UTC),
    )

    await worker.start()
    try:
        assert worker.enqueue(job)
        assert not worker.enqueue(job)
        await asyncio.wait_for(context.committed.wait(), timeout=1)
    finally:
        await worker.close()

    assert context.used == [("turn-1", (1,))]
    assert context.reinforced == [("turn-1", (1,))]
    assert executor.priorities == [ModelExecutionPriority.BEST_EFFORT_BACKGROUND]
    snapshot = metrics.adaptive_snapshot()
    assert snapshot["memory_attribution_success_count"] == 1
    assert snapshot["memory_attribution_duplicate_count"] == 1
    assert snapshot["memory_attribution_used_count"] == 1


@pytest.mark.asyncio
async def test_worker_rejects_ref_outside_exposure_whitelist(database: Database) -> None:
    runtime_config = RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    )
    runtime = await runtime_config.snapshot(user_id="1001")
    context = AttributionContext()
    worker = MemoryAttributionWorker(
        models=AttributionExecutor(("M999",)),
        memory_context=cast(Any, context),
        runtime_config=runtime_config,
        metrics=MemoryLifecycleMetrics(),
    )
    job = MemoryAttributionJob(
        turn_id="turn-invalid",
        user_id="1001",
        group_id=None,
        user_question="问题",
        final_response="回答",
        intent=MemoryQueryIntent(
            mode=MemoryContextMode.LEXICAL,
            purpose=MemoryRecallPurpose.BACKGROUND,
        ),
        exposures=(_exposure(1, source=MemoryExposureSource.AUTOMATIC),),
        runtime=runtime,
        enqueued_at=datetime.now(UTC),
    )

    await worker._process(job)

    assert context.used == []
    assert context.reinforced == []


@pytest.mark.asyncio
async def test_session_attribution_requires_an_eligible_delivered_turn(database: Database) -> None:
    runtime_config = RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    )
    config = await runtime_config.snapshot(user_id="1001")
    queue = AttributionQueue()
    inbound = InboundMessage(
        message_id="message-1",
        event_type="private_message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="question",
        bot_user_id="bot-9",
    )
    exposure = _exposure(1, source=MemoryExposureSource.AUTOMATIC)

    class _Context:
        async def retrieve_for_turn(self, **_kwargs: object) -> object:
            return empty_retrieval()

        async def mark_injected(self, _result: object, fact_ids: tuple[int, ...]) -> int:
            return len(fact_ids)

        async def record_recall(self, **kwargs: object) -> MemoryRecallTurn:
            injected = kwargs.get("injected_fact_ids")
            assert isinstance(injected, tuple)
            return MemoryRecallTurn(turn_id="turn-gated", injected_fact_ids=injected)

    def open_session(
        *, origin: RuntimeTurnOrigin = RuntimeTurnOrigin.USER_MESSAGE
    ) -> TurnMemorySession:
        return TurnMemorySession.open(
            inbound=inbound,
            identity=ConversationScope.private("bot-9", "1001"),
            runtime=config,
            memory_context=_Context(),  # type: ignore[arg-type]
            origin=origin,
            user_question="question",
            authority=TurnAuthority(
                actor_user_id="1001",
                bot_user_id="bot-9",
                origin=origin,
                permission_ceiling=frozenset(),
                delegated_authority=None,
                authority_revision=1,
            ),
            attribution=queue,
        )

    async def confirm(session: TurnMemorySession) -> None:
        await session.prefetch()
        session.stage_prompt_selection((1,), (exposure,))
        await session.confirm_prompt_exposure()

    eligible = open_session()
    await confirm(eligible)
    await eligible.on_delivery_confirmed(
        DeliverySummary(
            final_agent_run_id="message-1",
            status=DeliveryStatus.COMPLETE,
            delivered_text="memory-backed answer",
        )
    )
    assert len(queue.jobs) == 1

    cancelled = open_session()
    await confirm(cancelled)
    await cancelled.on_delivery_confirmed(
        DeliverySummary(
            final_agent_run_id="message-1",
            status=DeliveryStatus.CANCELLED,
            delivered_text="memory-backed answer",
        )
    )
    plugin = open_session(origin=RuntimeTurnOrigin.PLUGIN_BACKGROUND)
    await confirm(plugin)
    await plugin.on_delivery_confirmed(
        DeliverySummary(
            final_agent_run_id="message-1",
            status=DeliveryStatus.COMPLETE,
            delivered_text="memory-backed answer",
        )
    )
    empty_text = open_session()
    await confirm(empty_text)
    await empty_text.on_delivery_confirmed(
        DeliverySummary(
            final_agent_run_id="message-1",
            status=DeliveryStatus.COMPLETE,
            delivered_text="",
        )
    )
    no_exposure = open_session()
    await no_exposure.prefetch()
    await no_exposure.on_delivery_confirmed(
        DeliverySummary(
            final_agent_run_id="message-1",
            status=DeliveryStatus.COMPLETE,
            delivered_text="memory-backed answer",
        )
    )
    assert len(queue.jobs) == 1
