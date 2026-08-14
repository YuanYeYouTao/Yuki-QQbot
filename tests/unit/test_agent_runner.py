"""Agent final-answer recovery and reply-effect visibility tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    InboundMessage,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.emoji.models import EmojiPlacement, EmojiReplyMode, PendingReplyEffect
from qq_ai_bot.llm.base import LLMProvider, LLMUnavailableError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.receipt import FINALIZE_MEMORY_RESPONSE_TOOL, MemoryUsageControl
from qq_ai_bot.services.agent_runner import AgentRunner, AgentRuntime
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.chat import ChatService, _ChatAgentBackend
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.speech.reply_effect import PendingVoiceReplyEffect
from qq_ai_bot.time.models import TimeContext


def test_merge_function_tools_is_stable_by_tool_name() -> None:
    previous = (
        ChatTool(name="zeta", description="z", parameters={"type": "object"}),
        ChatTool(name="beta", description="old", parameters={"type": "object"}),
    )
    current = (
        ChatTool(name="alpha", description="a", parameters={"type": "object"}),
        ChatTool(name="beta", description="new", parameters={"type": "object"}),
    )

    merged = AgentRunner._merge_function_tools(previous, current)

    assert [item.name for item in merged] == ["alpha", "beta", "zeta"]
    assert merged[1].description == "new"


def test_only_explicit_successful_json_results_are_reusable() -> None:
    assert AgentRunner._tool_result_reusable('{"ok":true}')
    assert AgentRunner._tool_result_reusable('{"ok":true,"retryable":false}')
    assert not AgentRunner._tool_result_reusable('{"ok":false,"error":"tool_limit_exceeded"}')
    assert not AgentRunner._tool_result_reusable('{"ok":true,"retryable":true}')
    assert not AgentRunner._tool_result_reusable("plain text")


class EmptyAfterToolProvider(LLMProvider):
    """Return a tool call, an empty final answer, then a usable retry."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="voice-1",
                        function=ToolFunction(name="send_voice", arguments="{}"),
                    ),
                ),
            )
        if len(self.requests) == 2:
            return ChatResponse(content="", latency_seconds=0)
        assert "最终回复正文为空" in (request.messages[-1].content or "")
        return ChatResponse(content="好呀，我用语音和你说。", latency_seconds=0)


@dataclass(slots=True)
class VoiceEffectBackend:
    effects: list[str] = field(default_factory=list)

    def definitions(
        self,
        runtime: AgentRuntime,
        *,
        web_was_used: bool,
    ) -> tuple[ChatTool, ...]:
        del runtime, web_was_used
        return (
            ChatTool(
                name="send_voice",
                description="queue voice",
                parameters={"type": "object", "properties": {}},
            ),
        )

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None:
        del calls, runtime

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        del arguments_json, runtime
        self.effects.append(name)
        return json.dumps({"ok": True, "queued": True})

    def finalize(self, content: str, runtime: AgentRuntime) -> str:
        del runtime
        return content

    def exhausted(self, runtime: AgentRuntime) -> str:
        del runtime
        return "exhausted"

    def has_visible_effects(self) -> bool:
        # A queued voice needs model text and cannot complete a turn by itself.
        return False


class VisibleEffectBackend(VoiceEffectBackend):
    """Represent a Planner-owned media effect that can stand without text."""

    def definitions(
        self,
        runtime: AgentRuntime,
        *,
        web_was_used: bool,
    ) -> tuple[ChatTool, ...]:
        del runtime, web_was_used
        return ()

    def has_visible_effects(self) -> bool:
        return True


class NativeThenLocalProvider(LLMProvider):
    """Return a native event and a local call in the same model response."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ChatRequest) -> ChatResponse:
        del request
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                native_tool_events=(
                    NativeToolEvent(
                        tool_type=NativeToolType.WEB_SEARCH,
                        call_id="native-1",
                        status=NativeToolStatus.COMPLETED,
                        action_type="search",
                    ),
                ),
                tool_calls=(
                    ToolCall(
                        id="local-1",
                        function=ToolFunction(name="send_voice", arguments="{}"),
                    ),
                ),
            )
        return ChatResponse(content="isolated", latency_seconds=0)


class UnavailableAfterToolProvider(LLMProvider):
    """Commit one tool call, then fail while asking the model for final text."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ChatRequest) -> ChatResponse:
        del request
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="mutation-1",
                        function=ToolFunction(name="send_voice", arguments="{}"),
                    ),
                ),
            )
        raise LLMUnavailableError("synthetic post-commit failure")


class CommittedMutationBackend(VoiceEffectBackend):
    committed = False

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        result = await super().execute(name, arguments_json, runtime)
        self.committed = True
        return result

    def post_commit_recovery_text(self) -> str | None:
        if not self.committed:
            return None
        return "任务已取消。\n后续回复生成失败，但操作已经生效。"


class NativeIsolationBackend(VoiceEffectBackend):
    native_web_used = False

    def mark_native_web_used(self) -> None:
        self.native_web_used = True

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        assert self.native_web_used
        return await super().execute(name, arguments_json, runtime)


class FinalizationBudgetProvider(LLMProvider):
    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if request.tools:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="read-1",
                        function=ToolFunction(name="send_voice", arguments="{}"),
                    ),
                ),
            )
        return ChatResponse(content="根据已有结果回答。", latency_seconds=0)


class TerminalMutationBackend(VoiceEffectBackend):
    def __init__(self) -> None:
        super().__init__()
        self.committed = False

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        del arguments_json, runtime
        self.effects.append(name)
        self.committed = True
        return json.dumps(
            {
                "ok": True,
                "mutation_committed": True,
                "finalize_after_commit": True,
                "public_message": "任务已取消。",
            },
            ensure_ascii=False,
        )

    def post_commit_recovery_text(self) -> str | None:
        return "任务已取消。" if self.committed else None


class ChainedMutationProvider(LLMProvider):
    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if len(self.requests) <= 2:
            assert request.tools
            step = len(self.requests)
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"mutation-{step}",
                        function=ToolFunction(
                            name="send_voice",
                            arguments=json.dumps({"step": step}),
                        ),
                    ),
                ),
            )
        assert request.tools
        return ChatResponse(content="两步都完成了。", latency_seconds=0)


class NonTerminalMutationBackend(VoiceEffectBackend):
    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        del runtime
        self.effects.append(f"{name}:{arguments_json}")
        return json.dumps(
            {
                "ok": True,
                "mutation_committed": True,
                "public_message": "当前步骤已经提交。",
            },
            ensure_ascii=False,
        )


class TerminalMemoryProvider(LLMProvider):
    def __init__(self, *, refs: tuple[str, ...]) -> None:
        self.refs = refs
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        assert request.tool_choice == "required"
        assert {tool.name for tool in request.tools} == {FINALIZE_MEMORY_RESPONSE_TOOL}
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(
                ToolCall(
                    id="memory-final-1",
                    function=ToolFunction(
                        name=FINALIZE_MEMORY_RESPONSE_TOOL,
                        arguments=json.dumps(
                            {"content": "记得你偏好深烘咖啡。", "memory_refs": list(self.refs)},
                            ensure_ascii=False,
                        ),
                    ),
                ),
            ),
        )


class TerminalMemoryBackend(VoiceEffectBackend):
    def __init__(self) -> None:
        super().__init__()
        self.usage = MemoryUsageControl(
            turn_id="turn-terminal",
            injected_fact_ids=(249,),
            enabled=True,
        )

    def definitions(
        self,
        runtime: AgentRuntime,
        *,
        web_was_used: bool,
    ) -> tuple[ChatTool, ...]:
        del runtime, web_was_used
        if not self.usage.report_available:
            return ()
        return (
            ChatTool(
                name=FINALIZE_MEMORY_RESPONSE_TOOL,
                description="submit final response",
                parameters={"type": "object"},
            ),
        )

    def terminal_tool_names(self, runtime: AgentRuntime) -> frozenset[str]:
        del runtime
        return frozenset({FINALIZE_MEMORY_RESPONSE_TOOL})

    def requires_terminal_response(self, runtime: AgentRuntime) -> bool:
        del runtime
        return self.usage.report_available

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None:
        del runtime
        self.usage.begin_batch(tuple(call.function.name for call in calls))

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        del runtime
        self.usage.note_call(name)
        assert name == FINALIZE_MEMORY_RESPONSE_TOOL
        return self.usage.apply(arguments_json)

    def counts_toward_limit(self, name: str, runtime: AgentRuntime) -> bool:
        del runtime
        return name != FINALIZE_MEMORY_RESPONSE_TOOL

    def finalize(self, content: str, runtime: AgentRuntime) -> str:
        del runtime
        self.usage.finalize(content)
        return content


def _agent_runtime() -> AgentRuntime:
    now = datetime.now(UTC)
    config = cast(
        RuntimeConfigSnapshot,
        SimpleNamespace(
            llm=SimpleNamespace(
                model="test-model",
                temperature=0.1,
                max_output_tokens=256,
                thinking_enabled=False,
            )
        ),
    )
    return AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="private:1001",
        current_group_id=None,
        bot_user_id="8000",
        gateway=None,
        runtime_config=config,
        current_time=TimeContext(utc=now, local=now, timezone="Asia/Shanghai"),
        allowed_capabilities=frozenset({"send_voice"}),
        max_tool_calls=5,
        max_model_requests=6,
    )


@pytest.mark.asyncio
async def test_empty_final_answer_after_voice_tool_is_retried() -> None:
    provider = EmptyAfterToolProvider()
    backend = VoiceEffectBackend()
    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="发条语音吧"),),
        _agent_runtime(),
        backend,
    )

    assert result.text == "好呀，我用语音和你说。"
    assert result.model_requests == 3
    assert backend.effects == ["send_voice"]


@pytest.mark.asyncio
async def test_empty_model_response_is_valid_when_planner_has_visible_media() -> None:
    provider = FakeLLMProvider(lambda _request: "   ")

    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="发个表情"),),
        _agent_runtime(),
        VisibleEffectBackend(),
    )

    assert result.text == ""
    assert result.model_requests == 1
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_native_web_state_is_marked_before_same_response_local_calls() -> None:
    backend = NativeIsolationBackend()
    result = await AgentRunner(
        NativeThenLocalProvider(),
        ConcurrencyManager(1),
    ).run((ChatMessage(role="user", content="search then act"),), _agent_runtime(), backend)

    assert result.text == "isolated"
    assert result.web_was_used
    assert backend.native_web_used


@pytest.mark.asyncio
async def test_committed_mutation_uses_receipt_when_final_model_request_fails() -> None:
    provider = UnavailableAfterToolProvider()
    backend = CommittedMutationBackend()

    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="取消任务"),),
        _agent_runtime(),
        backend,
    )

    assert result.text == "任务已取消。\n后续回复生成失败，但操作已经生效。"
    assert result.tool_calls_used == 1
    assert result.model_requests == 2
    assert backend.effects == ["send_voice"]


@pytest.mark.asyncio
async def test_agent_runner_reserves_last_request_for_final_text() -> None:
    provider = FinalizationBudgetProvider()
    runtime = replace(_agent_runtime(), max_model_requests=2)

    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="先查再回答"),),
        runtime,
        VoiceEffectBackend(),
    )

    assert result.text == "根据已有结果回答。"
    assert result.model_requests == 2
    assert provider.requests[0].tools
    assert provider.requests[0].tool_choice == "auto"
    assert provider.requests[1].tools == ()
    assert provider.requests[1].tool_choice is None
    assert "预留的最终回复请求" in (provider.requests[1].messages[-1].content or "")


@pytest.mark.asyncio
async def test_successful_mutation_uses_reserved_finalization_request() -> None:
    provider = FinalizationBudgetProvider()
    backend = TerminalMutationBackend()
    runtime = replace(_agent_runtime(), max_model_requests=2)

    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="取消任务"),),
        runtime,
        backend,
    )

    assert result.text == "根据已有结果回答。"
    assert result.model_requests == 2
    assert result.tool_calls_used == 1
    assert len(provider.requests) == 2
    assert provider.requests[0].tools
    assert provider.requests[1].tools == ()
    assert backend.effects == ["send_voice"]


@pytest.mark.asyncio
async def test_successful_mutation_forces_a_tool_free_final_reply_before_budget_end() -> None:
    provider = FinalizationBudgetProvider()
    backend = TerminalMutationBackend()
    runtime = replace(_agent_runtime(), max_model_requests=6)

    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="创建一次操作"),),
        runtime,
        backend,
    )

    assert result.text == "根据已有结果回答。"
    assert result.model_requests == 2
    assert result.tool_calls_used == 1
    assert len(provider.requests) == 2
    assert provider.requests[0].tools
    assert provider.requests[1].tools == ()
    assert backend.effects == ["send_voice"]


@pytest.mark.asyncio
async def test_non_terminal_mutation_can_continue_with_another_tool_round() -> None:
    provider = ChainedMutationProvider()
    backend = NonTerminalMutationBackend()

    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="先完成一步，再继续下一步"),),
        _agent_runtime(),
        backend,
    )

    assert result.text == "两步都完成了。"
    assert result.model_requests == 3
    assert result.tool_calls_used == 2
    assert len(backend.effects) == 2
    assert all(request.tools for request in provider.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(("refs", "used"), [(("M249",), (249,)), ((), ())])
async def test_terminal_memory_response_uses_one_model_request_without_business_budget(
    refs: tuple[str, ...],
    used: tuple[int, ...],
) -> None:
    provider = TerminalMemoryProvider(refs=refs)
    backend = TerminalMemoryBackend()

    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="我喜欢哪种咖啡？"),),
        _agent_runtime(),
        backend,
    )

    assert result.text == "记得你偏好深烘咖啡。"
    assert result.model_requests == 1
    assert result.tool_calls_used == 0
    assert backend.usage.used_fact_ids == used
    assert len(provider.requests) == 1


def test_voice_effect_cannot_complete_chat_without_text() -> None:
    inbound = InboundMessage(
        message_id="voice-visibility",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="发条语音吧",
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        reply_effects=[PendingVoiceReplyEffect()],
    )
    backend = _ChatAgentBackend(cast(ChatService, object()), runtime)

    assert not backend.has_visible_effects()

    emoji_runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        reply_effects=[
            PendingReplyEffect(
                mode=EmojiReplyMode.EMOJI_ONLY,
                placement=EmojiPlacement.ONLY,
                goal="回应用户",
                source="agent",
            )
        ],
    )
    emoji_backend = _ChatAgentBackend(cast(ChatService, object()), emoji_runtime)
    assert emoji_backend.has_visible_effects()

    preferred_emoji_runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        reply_effects=[
            PendingReplyEffect(
                mode=EmojiReplyMode.PREFERRED,
                placement=EmojiPlacement.AFTER_TEXT,
                goal="回应用户",
                source="planner",
            )
        ],
    )
    preferred_emoji_backend = _ChatAgentBackend(
        cast(ChatService, object()),
        preferred_emoji_runtime,
    )
    assert preferred_emoji_backend.has_visible_effects()

    optional_emoji_runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        reply_effects=[
            PendingReplyEffect(
                mode=EmojiReplyMode.OPTIONAL,
                placement=EmojiPlacement.AFTER_TEXT,
                goal="回应用户",
                source="agent",
            )
        ],
    )
    optional_emoji_backend = _ChatAgentBackend(
        cast(ChatService, object()),
        optional_emoji_runtime,
    )
    assert not optional_emoji_backend.has_visible_effects()
