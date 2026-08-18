"""Conversation compaction model task and structured summarizer."""

from __future__ import annotations

import inspect
import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qq_ai_bot.conversation.history.errors import ConversationSummaryQualityError
from qq_ai_bot.conversation.history.source import build_source_snapshot
from qq_ai_bot.conversation.history.summarizer import (
    CONVERSATION_ROLLUP_PROMPT_VERSION,
    CompactionChildView,
    ConversationHistorySummarizer,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.model_runtime.models import (
    ModelExecutionPriority,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.model_runtime.structured import StructuredTaskError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repository_records import EventRecord

_NOW = datetime(2026, 8, 19, 5, 0, tzinfo=UTC)
_QUESTION = "明天去北京吗？"


def _event(event_id: int, content: str) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="bot-1",
        platform_message_id=f"m-{event_id}",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content=content,
        visual_summary="",
        segments=(),
        occurred_at=_NOW + timedelta(seconds=event_id),
        sender_nickname="远野",
        private_peer_user_id="1001",
    )


def _snapshot(*contents: str):
    events = tuple(_event(index, content) for index, content in enumerate(contents, start=1))
    return build_source_snapshot(
        state_id=4,
        reset_at=None,
        scope_type=ScopeType.PRIVATE,
        events=events,
    )


def _valid_l0() -> dict[str, object]:
    return {
        "narrative": "用户询问行程，尚未形成决定。",
        "decisions": [],
        "open_loops": [{"item": "是否前往北京", "owner": "用户", "state": "pending"}],
        "constraints": [],
        "entities": [{"name": "北京", "role": "行程候选目的地"}],
        "state_changes": [],
        "uncertainties": [{"claim": "是否成行", "reason": "来源是问句而不是陈述"}],
        "terminal_tool_outcomes": [],
    }


def _valid_parent() -> dict[str, object]:
    body = _valid_l0()
    body["narrative"] = "两段会话都在讨论未决行程，仍没有接受出行。"
    return body


class _CompactionExecutor:
    def __init__(
        self,
        payload: object,
        *,
        mode: StructuredOutputMode = StructuredOutputMode.TEXT_JSON,
        as_tool: bool = False,
        tool_name: str = "emit_result",
    ) -> None:
        self.payload = payload
        self.mode = mode
        self.as_tool = as_tool
        self.tool_name = tool_name
        self.requests: list[ChatRequest] = []
        self.priorities: list[ModelExecutionPriority] = []

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
    ) -> ChatResponse:
        assert task is ModelTask.CONVERSATION_COMPACTION
        self.requests.append(request)
        self.priorities.append(priority)
        body = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        if self.as_tool:
            return ChatResponse(
                content="",
                latency_seconds=0.01,
                tool_calls=(
                    ToolCall(
                        id="compaction-1",
                        function=ToolFunction(name=self.tool_name, arguments=body),
                    ),
                ),
            )
        return ChatResponse(content=body, latency_seconds=0.01)

    def model_name(self, task: ModelTask) -> str:
        assert task is ModelTask.CONVERSATION_COMPACTION
        return "flash"

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        assert task is ModelTask.CONVERSATION_COMPACTION
        return self.mode


def test_conversation_compaction_is_an_independent_model_task() -> None:
    assert ModelTask.CONVERSATION_COMPACTION.value == "conversation_compaction"
    assert ModelTask.CONVERSATION_COMPACTION.value != ModelTask.MEMORY_CONSOLIDATION.value
    assert CONVERSATION_ROLLUP_PROMPT_VERSION == "conversation-rollup-v1"


def test_example_profiles_route_compaction_to_flash_without_changing_chat_agent() -> None:
    document = tomllib.loads(Path("config/model_profiles.example.toml").read_text(encoding="utf-8"))
    assert document["routes"]["conversation_compaction"] == "flash"
    assert document["routes"]["chat_agent"] == "pro"
    assert document["routes"]["memory_consolidation"] == "flash"


def test_summarizer_does_not_import_memory_mutation() -> None:
    source = Path("src/qq_ai_bot/conversation/history/summarizer.py").read_text(encoding="utf-8")
    assert "memory.mutation" not in source
    assert "MemoryFactService" not in source
    assert "MEMORY_CONSOLIDATION" not in source


@pytest.mark.asyncio
async def test_summarizer_accepts_normal_l0_output(caplog: pytest.LogCaptureFixture) -> None:
    executor = _CompactionExecutor(_valid_l0())
    summarizer = ConversationHistorySummarizer(executor)
    snapshot = _snapshot("你好", _QUESTION)
    with caplog.at_level("INFO", logger="qq_ai_bot.conversation.history.summarizer"):
        output = await summarizer.summarize_events(snapshot, level=0)
    assert output.narrative.startswith("用户询问行程")
    assert output.open_loops[0].item == "是否前往北京"
    assert executor.priorities == [ModelExecutionPriority.BEST_EFFORT_BACKGROUND]
    assert executor.requests[0].thinking_enabled is False
    assert executor.requests[0].tools == ()
    assert _QUESTION not in caplog.text
    assert "你好" not in caplog.text


@pytest.mark.asyncio
async def test_summarizer_accepts_parent_level_output() -> None:
    executor = _CompactionExecutor(_valid_parent())
    output = await ConversationHistorySummarizer(executor).summarize_children(
        (
            CompactionChildView(
                summary_id=1,
                level=0,
                start_event_id=1,
                end_event_id=20,
                rendered_text="第一段在问行程。",
            ),
            CompactionChildView(
                summary_id=2,
                level=0,
                start_event_id=21,
                end_event_id=40,
                rendered_text="第二段仍未决定。",
            ),
        ),
        level=1,
        fingerprint="abc",
    )
    assert "未决行程" in output.narrative


@pytest.mark.asyncio
async def test_summarizer_rejects_invalid_json() -> None:
    executor = _CompactionExecutor("{not-json")
    with pytest.raises(StructuredTaskError, match="invalid JSON"):
        await ConversationHistorySummarizer(executor).summarize_events(_snapshot("hello"))


@pytest.mark.asyncio
async def test_summarizer_rejects_unknown_fields() -> None:
    payload = {**_valid_l0(), "markdown": "# 摘要"}
    executor = _CompactionExecutor(payload)
    with pytest.raises(StructuredTaskError) as captured:
        await ConversationHistorySummarizer(executor).summarize_events(_snapshot("hello"))
    assert captured.value.reason_code == "schema_validation"


@pytest.mark.asyncio
async def test_summarizer_rejects_oversized_fields() -> None:
    payload = _valid_l0()
    payload["narrative"] = "长" * 1201
    executor = _CompactionExecutor(payload)
    with pytest.raises(StructuredTaskError) as captured:
        await ConversationHistorySummarizer(executor).summarize_events(_snapshot("hello"))
    assert captured.value.reason_code == "schema_validation"


@pytest.mark.asyncio
async def test_summarizer_rejects_l0_narrative_above_level_budget() -> None:
    payload = _valid_l0()
    payload["narrative"] = "长" * 901
    executor = _CompactionExecutor(payload)
    with pytest.raises(ConversationSummaryQualityError, match="narrative_too_long"):
        await ConversationHistorySummarizer(executor).summarize_events(_snapshot("hello"), level=0)


@pytest.mark.asyncio
async def test_summarizer_rejects_tool_call_output() -> None:
    executor = _CompactionExecutor(
        _valid_l0(),
        mode=StructuredOutputMode.FUNCTION_TOOL,
        as_tool=True,
        tool_name="search",
    )
    with pytest.raises(StructuredTaskError) as captured:
        await ConversationHistorySummarizer(executor).summarize_events(_snapshot("hello"))
    assert captured.value.reason_code == "unknown_function"
    assert executor.requests[0].tools[0].name == "emit_result"
    assert executor.requests[0].tool_choice == "required"


@pytest.mark.asyncio
async def test_summarizer_redacts_secret_patterns() -> None:
    payload = _valid_l0()
    payload["narrative"] = "用户贴出 api_key=sk-secret-value 后询问行程。"
    output = await ConversationHistorySummarizer(_CompactionExecutor(payload)).summarize_events(
        _snapshot("hello")
    )
    assert "sk-secret-value" not in output.narrative
    assert "[redacted]" in output.narrative


@pytest.mark.asyncio
async def test_summarizer_rejects_question_promoted_to_accepted_fact() -> None:
    payload = _valid_l0()
    payload["decisions"] = [
        {"decision": "明天去北京", "status": "accepted", "actors": ["用户"]},
    ]
    payload["open_loops"] = []
    payload["uncertainties"] = []
    with pytest.raises(ConversationSummaryQualityError, match="question_as_fact"):
        await ConversationHistorySummarizer(_CompactionExecutor(payload)).summarize_events(
            _snapshot(_QUESTION)
        )


@pytest.mark.asyncio
async def test_conversation_compaction_stats_are_independent(database: Database) -> None:
    repository = ModelInvocationRepository(database)
    await repository.record(
        task=ModelTask.CONVERSATION_COMPACTION,
        profile_id="flash",
        provider="fake",
        model="flash-model",
        success=True,
        prompt_tokens=80,
        completion_tokens=40,
        total_tokens=120,
        cached_prompt_tokens=0,
        latency_seconds=0.2,
        error_category=None,
    )
    await repository.record(
        task=ModelTask.MEMORY_CONSOLIDATION,
        profile_id="flash",
        provider="fake",
        model="flash-model",
        success=True,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cached_prompt_tokens=0,
        latency_seconds=0.1,
        error_category=None,
    )
    compaction = await repository.stats(task=ModelTask.CONVERSATION_COMPACTION)
    consolidation = await repository.stats(task=ModelTask.MEMORY_CONSOLIDATION)
    by_task = await repository.stats_by_task()
    assert compaction.invocations == 1
    assert compaction.total_tokens == 120
    assert consolidation.total_tokens == 15
    assert "conversation_compaction" in by_task
    assert "memory_consolidation" in by_task


def test_summarizer_source_has_no_full_prompt_logging() -> None:
    source = inspect.getsource(ConversationHistorySummarizer.summarize)
    logged = source.split("logger.info", 1)[-1].split("output =", 1)[0]
    assert "instruction" not in logged
    assert "structured_input" not in logged
