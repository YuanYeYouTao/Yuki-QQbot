"""Model runtime and Agent loop coverage for the Responses protocol."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    FunctionCallOutput,
    ModelResponseStatus,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import LLMIncompleteResponseError, LLMUnavailableError
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import (
    ModelProfileCatalog,
    ModelRuntimeConfigurationError,
    load_model_profile_catalog,
)
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.services.agent_runner import AgentRunner, AgentRuntime
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.time.models import TimeContext


def _profile_document(*, schema_version: int, protocol: str | None) -> str:
    protocol_line = f'protocol = "{protocol}"\n' if protocol is not None else ""
    routes = "\n".join(f'{task.value} = "pro"' for task in ModelTask)
    return (
        f"schema_version = {schema_version}\n"
        "[profiles.pro]\n"
        'provider = "deepseek"\n'
        f"{protocol_line}"
        'base_url = "https://api.deepseek.com"\n'
        'api_key_env = "TEST_KEY"\n'
        'model = "deepseek-v4-flash"\n'
        "timeout_seconds = 10\n"
        "max_retries = 0\n"
        "default_temperature = 0.1\n"
        "default_max_output_tokens = 512\n"
        'thinking_mode = "disabled"\n'
        'structured_output_mode = "function_tool"\n'
        'capabilities = ["tools", "structured_output", "native_web_search"]\n'
        "[routes]\n"
        f"{routes}\n"
    )


def _load(path: Path) -> ModelProfileCatalog:
    return load_model_profile_catalog(
        path,
        legacy_provider="fake",
        legacy_base_url="",
        legacy_model="fake",
        legacy_timeout_seconds=10,
        legacy_max_retries=0,
        legacy_temperature=0,
        legacy_max_output_tokens=100,
        legacy_thinking_enabled=False,
        environment={},
    )


def test_profile_schema_v3_accepts_responses_and_rejects_legacy(tmp_path: Path) -> None:
    v3 = tmp_path / "v3.toml"
    v3.write_text(_profile_document(schema_version=3, protocol="responses"), encoding="utf-8")
    assert _load(v3).profiles["pro"].protocol is ModelProtocol.RESPONSES

    omitted = tmp_path / "v3-default.toml"
    omitted.write_text(_profile_document(schema_version=3, protocol=None), encoding="utf-8")
    assert _load(omitted).profiles["pro"].protocol is ModelProtocol.CHAT_COMPLETIONS

    v2 = tmp_path / "v2.toml"
    v2.write_text(_profile_document(schema_version=2, protocol="responses"), encoding="utf-8")
    with pytest.raises(ModelRuntimeConfigurationError, match="migrate-3-6"):
        _load(v2)


@pytest.mark.asyncio
async def test_client_pool_selects_deepseek_responses_provider() -> None:
    profile = ModelProfile(
        id="responses",
        provider="deepseek",
        protocol=ModelProtocol.RESPONSES,
        base_url="https://api.deepseek.com",
        api_key_env="TEST_KEY",
        model="deepseek-v4-flash",
        timeout_seconds=10,
        max_retries=0,
        default_temperature=0,
        default_max_output_tokens=100,
        capabilities=frozenset({ModelCapability.TOOLS}),
    )
    pool = ModelClientPool(secret_overrides={"TEST_KEY": "secret"})
    assert isinstance(pool.get(profile), DeepSeekResponsesProvider)
    await pool.close()


class _CapturingProvider:
    async def complete(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse(content="ok", latency_seconds=0, continuation=request.continuation)


class _Pool:
    def get(self, profile: ModelProfile) -> _CapturingProvider:
        del profile
        return _CapturingProvider()

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_executor_rejects_continuation_from_another_profile() -> None:
    profile = ModelProfile(
        id="current",
        provider="fake",
        protocol=ModelProtocol.RESPONSES,
        model="fake",
        timeout_seconds=10,
        max_retries=0,
        default_temperature=0,
        default_max_output_tokens=100,
        capabilities=frozenset({ModelCapability.TOOLS}),
    )
    catalog = ModelProfileCatalog(
        profiles={"current": profile},
        routes={
            task: ModelRoute(task=task, profile_id="current", required_capabilities=frozenset())
            for task in ModelTask
        },
    )
    executor = TaskModelExecutor(
        router=ModelRouter(catalog),
        pool=cast(ModelClientPool, _Pool()),
    )
    with pytest.raises(ValueError, match="different model profile"):
        await executor.execute(
            ModelTask.CHAT_AGENT,
            ChatRequest(
                messages=(ChatMessage(role="user", content="hello"),),
                continuation=ProviderContinuation(
                    provider="fake",
                    protocol="responses",
                    profile_id="other",
                    payload=(),
                ),
            ),
        )


class _ResponsesExecutor:
    def __init__(self, responses: list[ChatResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[ChatRequest] = []

    async def execute(self, task: ModelTask, request: ChatRequest) -> ChatResponse:
        assert task is ModelTask.CHAT_AGENT
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def model_name(self, task: ModelTask) -> str:
        del task
        return "deepseek-v4-flash"

    def structured_output_mode(self, task: ModelTask) -> object:
        raise AssertionError(task)

    def protocol(self, task: ModelTask) -> ModelProtocol:
        del task
        return ModelProtocol.RESPONSES

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]:
        del task
        return frozenset({ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH})


class _Backend:
    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        del runtime, web_was_used
        return (ChatTool(name="demo", description="demo", parameters={}),)

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None:
        del calls, runtime

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        del name, arguments_json, runtime
        return json.dumps({"ok": True})

    def parallel_safe(self, name: str, runtime: AgentRuntime) -> bool:
        del name, runtime
        return False

    def finalize(self, content: str, runtime: AgentRuntime) -> str:
        del runtime
        return content

    def exhausted(self, runtime: AgentRuntime) -> str:
        del runtime
        return "exhausted"


class _ChangingBackend(_Backend):
    def __init__(self) -> None:
        self.definition_calls = 0

    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        del runtime, web_was_used
        self.definition_calls += 1
        name = "demo" if self.definition_calls == 1 else "new_tool"
        return (ChatTool(name=name, description=name, parameters={}),)


class _CountingBackend(_Backend):
    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        self.executions += 1
        return await super().execute(name, arguments_json, runtime)


class _MutationBackend(_Backend):
    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        del name, arguments_json, runtime
        return json.dumps(
            {
                "ok": True,
                "mutation_committed": True,
                "finalize_after_commit": True,
                "data": {"status": "pending_payment"},
            }
        )

    def post_commit_recovery_text(self) -> str | None:
        return "操作已经提交。"


def _runtime(
    max_requests: int = 4,
    *,
    web_mode: str = "disabled",
    allowed: frozenset[str] = frozenset(),
) -> AgentRuntime:
    now = datetime.now(UTC)
    config = cast(
        RuntimeConfigSnapshot,
        SimpleNamespace(
            llm=SimpleNamespace(
                model="deepseek-v4-flash",
                temperature=0.1,
                max_output_tokens=100,
                thinking_enabled=False,
            ),
            web=SimpleNamespace(mode=web_mode),
        ),
    )
    return AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="user",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="private:user",
        current_group_id=None,
        bot_user_id="bot",
        gateway=None,
        runtime_config=config,
        current_time=TimeContext(utc=now, local=now, timezone="Asia/Shanghai"),
        allowed_capabilities=allowed,
        max_tool_calls=3,
        max_model_requests=max_requests,
    )


@pytest.mark.asyncio
async def test_agent_runner_uses_function_outputs_without_duplicate_tool_messages() -> None:
    continuation = ProviderContinuation(provider="deepseek", protocol="responses", payload=())
    executor = _ResponsesExecutor(
        [
            ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(id="call-1", function=ToolFunction(name="demo", arguments="{}")),
                ),
                continuation=continuation,
            ),
            ChatResponse(content="done", latency_seconds=0, continuation=continuation),
        ]
    )
    result = await AgentRunner(
        cast(TaskModelExecutor, executor),
        ConcurrencyManager(1),
    ).run((ChatMessage(role="user", content="run"),), _runtime(), _Backend())

    assert result.text == "done"
    assert len(executor.requests) == 2
    assert executor.requests[1].messages == (ChatMessage(role="user", content="run"),)
    assert executor.requests[1].function_outputs == (
        FunctionCallOutput(call_id="call-1", output='{"ok": true}'),
    )


@pytest.mark.asyncio
async def test_responses_continuation_never_removes_declared_function_tools() -> None:
    continuation = ProviderContinuation(provider="deepseek", protocol="responses", payload=())
    executor = _ResponsesExecutor(
        [
            ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(id="call-1", function=ToolFunction(name="demo", arguments="{}")),
                ),
                continuation=continuation,
            ),
            ChatResponse(content="done", latency_seconds=0, continuation=continuation),
        ]
    )
    await AgentRunner(
        cast(TaskModelExecutor, executor),
        ConcurrencyManager(1),
    ).run((ChatMessage(role="user", content="run"),), _runtime(), _ChangingBackend())

    assert {tool.name for tool in executor.requests[1].tools} == {"demo", "new_tool"}


@pytest.mark.asyncio
async def test_responses_committed_mutation_keeps_schema_but_forces_tool_choice_none() -> None:
    continuation = ProviderContinuation(provider="deepseek", protocol="responses", payload=())
    executor = _ResponsesExecutor(
        [
            ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(id="mutation-1", function=ToolFunction(name="demo", arguments="{}")),
                ),
                continuation=continuation,
            ),
            ChatResponse(
                content="订单已经创建，等待支付。",
                latency_seconds=0,
                continuation=continuation,
            ),
        ]
    )

    result = await AgentRunner(
        cast(TaskModelExecutor, executor),
        ConcurrencyManager(1),
    ).run((ChatMessage(role="user", content="create"),), _runtime(), _MutationBackend())

    assert result.text == "订单已经创建，等待支付。"
    assert len(executor.requests) == 2
    assert executor.requests[1].tools
    assert executor.requests[1].tool_choice == "none"
    assert executor.requests[1].function_outputs == (
        FunctionCallOutput(
            call_id="mutation-1",
            output=(
                '{"ok": true, "mutation_committed": true, '
                '"finalize_after_commit": true, "data": {"status": "pending_payment"}}'
            ),
        ),
    )


@pytest.mark.asyncio
async def test_agent_runner_stops_identical_no_progress_tool_batches_early() -> None:
    continuation = ProviderContinuation(provider="deepseek", protocol="responses", payload=())
    repeated = [
        ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(
                ToolCall(
                    id=f"same-{index}",
                    function=ToolFunction(
                        name="demo",
                        arguments=(
                            '{"query":"same","limit":5}'
                            if index == 0
                            else '{"limit":5,"query":"same"}'
                        ),
                    ),
                ),
            ),
            continuation=continuation,
        )
        for index in range(3)
    ]
    executor = _ResponsesExecutor(
        [*repeated, ChatResponse(content="根据已有结果回答", latency_seconds=0)]
    )
    backend = _CountingBackend()

    result = await AgentRunner(
        cast(TaskModelExecutor, executor),
        ConcurrencyManager(1),
    ).run(
        (ChatMessage(role="user", content="run"),),
        _runtime(max_requests=10),
        backend,
    )

    assert result.text == "根据已有结果回答"
    assert result.model_requests == 4
    assert result.tool_calls_used == 1
    assert backend.executions == 1
    assert executor.requests[-1].tool_choice == "none"
    assert executor.requests[-1].tools


@pytest.mark.asyncio
async def test_agent_runner_allows_only_one_incomplete_recovery() -> None:
    continuation = ProviderContinuation(provider="deepseek", protocol="responses", payload=())
    executor = _ResponsesExecutor(
        [
            ChatResponse(
                content="partial",
                latency_seconds=0,
                status=ModelResponseStatus.INCOMPLETE,
                continuation=continuation,
            ),
            ChatResponse(
                content="still partial",
                latency_seconds=0,
                status=ModelResponseStatus.INCOMPLETE,
                continuation=continuation,
            ),
        ]
    )
    with pytest.raises(LLMIncompleteResponseError):
        await AgentRunner(
            cast(TaskModelExecutor, executor),
            ConcurrencyManager(1),
        ).run((ChatMessage(role="user", content="run"),), _runtime(), _Backend())
    assert len(executor.requests) == 2
    assert executor.requests[1].tools == executor.requests[0].tools


class _FallbackBackend(_Backend):
    def __init__(self) -> None:
        self.enabled = False

    def enable_native_web_fallback(self) -> None:
        self.enabled = True

    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        del runtime, web_was_used
        if not self.enabled:
            return ()
        return (ChatTool(name="web_search", description="fallback", parameters={}),)


@pytest.mark.asyncio
async def test_native_timeout_falls_back_once_without_exposing_both_web_tools() -> None:
    continuation = ProviderContinuation(provider="deepseek", protocol="responses", payload=())
    executor = _ResponsesExecutor(
        [
            LLMUnavailableError("synthetic native outage"),
            ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="fallback-1",
                        function=ToolFunction(name="web_search", arguments="{}"),
                    ),
                ),
                continuation=continuation,
            ),
            ChatResponse(content="fallback done", latency_seconds=0, continuation=continuation),
        ]
    )
    backend = _FallbackBackend()
    result = await AgentRunner(
        cast(TaskModelExecutor, executor),
        ConcurrencyManager(1),
    ).run(
        (ChatMessage(role="user", content="search"),),
        _runtime(
            web_mode="native_with_tavily_fallback",
            allowed=frozenset({"web"}),
        ),
        backend,
    )

    assert result.text == "fallback done"
    assert executor.requests[0].native_tools and not executor.requests[0].tools
    assert executor.requests[1].tools and not executor.requests[1].native_tools
    assert backend.enabled
