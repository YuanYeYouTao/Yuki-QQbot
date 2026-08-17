"""Dynamic Tool Kernel request gateway tests."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities import (
    CapabilityTrustSource,
    InProcessToolProvider,
    ToolCandidateSelector,
    ToolKernelMetrics,
    ToolProviderRegistry,
)
from qq_ai_bot.capabilities.request import REQUEST_TOOLS_NAME, match_requestable_tools
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatTool,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.memory.runtime.contract import MemoryReadPolicy
from qq_ai_bot.memory.runtime.finalizer import (
    MutationFinalizationInput,
    finalize_mutation_text,
    mutation_view_from_tool_result,
)
from qq_ai_bot.planner.models import ToolMode
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.chat import ChatService, _ChatAgentBackend
from qq_ai_bot.services.reply_target import ReplyTargetControl


def _tool(name: str, description: str) -> ChatTool:
    return ChatTool(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )


def _registry(calls: list[str]) -> ToolProviderRegistry:
    async def execute(name: str, _arguments: str, _runtime: object) -> object:
        calls.append(name)
        return {"ok": True, "data": {"called": name}}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="plugin",
            source=CapabilityTrustSource.PLUGIN,
            definitions=lambda _runtime: (
                _tool("album_share", "搜索并发送网易云专辑卡片"),
                _tool("song_share", "搜索并发送网易云单曲；也可从刚才专辑抽一首"),
            ),
            execute=execute,
        )
    )
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _runtime: (_tool("web_search", "联网搜索公开网页"),),
            execute=execute,
        )
    )
    return registry


class _Service:
    def __init__(self, registry: ToolProviderRegistry) -> None:
        self.registry = registry
        self._tool_selector = ToolCandidateSelector()
        self._tool_metrics = ToolKernelMetrics()
        self._tool_invocations = None
        self._tool_artifacts = None
        self.mutation_turn_outcomes: list[str] = []

    def _build_tool_registry(
        self,
        _runtime: ToolRuntime,
        *,
        web_was_used: bool,
    ) -> ToolProviderRegistry:
        del web_was_used
        return self.registry

    @staticmethod
    def _decode_tool_result(value: str) -> dict[str, object]:
        decoded = json.loads(value)
        assert isinstance(decoded, dict)
        return decoded

    def _record_memory_mutation_turn_outcome(self, outcome: str) -> None:
        self.mutation_turn_outcomes.append(outcome)


class _CandidateChatService(ChatService):
    def __init__(self, registry: ToolProviderRegistry) -> None:
        self.registry = registry
        self._tool_artifacts = None
        self._tool_selector = ToolCandidateSelector()

    def _build_tool_registry(
        self,
        _runtime: ToolRuntime,
        *,
        web_was_used: bool,
    ) -> ToolProviderRegistry:
        del web_was_used
        return self.registry


class _FakeMemorySession:
    def __init__(
        self,
        *,
        exclusive_write: bool = False,
        locator_open: bool = False,
        eager_read: bool = False,
        receipt_gated: bool = False,
        mutation_terminal: bool = False,
        mutation_text: str | None = None,
    ) -> None:
        self.exclusive_write = exclusive_write
        self.locator_open = locator_open
        self.receipt_gated = receipt_gated
        self.mutation_terminal = mutation_terminal
        self._mutation_text = mutation_text
        if exclusive_write and locator_open:
            read_policy = MemoryReadPolicy.LOCATOR_ONLY
        elif eager_read:
            read_policy = MemoryReadPolicy.EAGER
        else:
            read_policy = MemoryReadPolicy.DEFERRED
        self.contract = SimpleNamespace(read_policy=read_policy)

    def finalize_text(self) -> str | None:
        return self._mutation_text

    def request_exclusive_write(self) -> None:
        self.exclusive_write = True

    async def observe_tool_result(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def confirm_prompt_exposure(self) -> None:
        return None


def _exclusive_session(
    *,
    locator_open: bool = False,
    result: dict[str, object] | None = None,
) -> _FakeMemorySession:
    if result is None:
        text = finalize_mutation_text(MutationFinalizationInput(attempted=False))
        terminal = False
    else:
        text = finalize_mutation_text(mutation_view_from_tool_result(result, attempted=True))
        terminal = True
    return _FakeMemorySession(
        exclusive_write=True,
        locator_open=locator_open,
        receipt_gated=True,
        mutation_terminal=terminal,
        mutation_text=text,
    )


def _runtime() -> ToolRuntime:
    inbound = InboundMessage(
        message_id="m1",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="10001"),
        text="抽第一首",
        bot_user_id="99999",
    )
    return ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="private:10001",
        trigger_message_id="m1",
        actor_user_id="10001",
        runtime_config=SimpleNamespace(
            tooling=None,
            mcp=None,
            agent=SimpleNamespace(tool_result_max_characters=32_000),
            web=SimpleNamespace(max_calls_per_turn=3),
        ),
        origin=TurnOrigin.USER_MESSAGE,
        tool_mode=ToolMode.INHERIT,
        tool_groups=frozenset({"plugin"}),
        planner_scopes_explicit=True,
        selected_tool_names=frozenset({"album_share"}),
    )


def test_request_matcher_prefers_song_capability_and_has_no_arbitrary_fallback() -> None:
    catalog = _registry([]).catalog(object())

    matches = match_requestable_tools(catalog, query="搜索并发送网易云单曲", limit=2)

    assert matches[0].entry.descriptor.model_name == "song_share"
    assert match_requestable_tools(catalog, query="完全无关的量子天气", limit=2) == ()


@pytest.mark.asyncio
async def test_reply_target_control_survives_tool_mode_none_and_is_bounded() -> None:
    service = _Service(_registry([]))
    control = ReplyTargetControl(visible_event_ids=frozenset({42}))
    runtime = replace(
        _runtime(),
        tool_mode=ToolMode.NONE,
        tool_groups=frozenset(),
        selected_tool_names=frozenset(),
        reply_target_control=control,
    )
    backend = _ChatAgentBackend(service, runtime)  # type: ignore[arg-type]
    agent_runtime = SimpleNamespace()

    definitions = backend.definitions(agent_runtime, web_was_used=False)

    assert [tool.name for tool in definitions] == ["request_tools", "set_reply_target"]
    assert backend.counts_toward_limit("set_reply_target", agent_runtime) is False
    assert backend.counts_toward_limit("read_tool_artifact", agent_runtime) is False
    assert backend.counts_toward_limit("business_tool", agent_runtime) is True
    arguments = json.dumps({"event_id": 42})
    call = ToolCall(
        id="reply-target",
        function=ToolFunction(name="set_reply_target", arguments=arguments),
    )
    backend.begin_batch((call,), agent_runtime)
    selected = json.loads(await backend.execute("set_reply_target", arguments, agent_runtime))
    assert selected == {"ok": True, "outcome": "selected", "reply_to_event_id": 42}
    assert control.override_applied is True
    assert control.event_id == 42

    second = ToolCall(
        id="reply-target-again",
        function=ToolFunction(name="set_reply_target", arguments="{}"),
    )
    backend.begin_batch((second,), agent_runtime)
    repeated = json.loads(await backend.execute("set_reply_target", "{}", agent_runtime))
    assert repeated["ok"] is False
    assert repeated["outcome"] == "reply_target_already_selected"


@pytest.mark.asyncio
async def test_user_message_can_request_authorized_tools_from_tool_mode_none() -> None:
    calls: list[str] = []
    service = _Service(_registry(calls))
    runtime = replace(
        _runtime(),
        tool_mode=ToolMode.NONE,
        tool_groups=frozenset(),
        selected_tool_names=frozenset(),
        planner_scopes_explicit=True,
    )
    backend = _ChatAgentBackend(service, runtime)  # type: ignore[arg-type]
    agent_runtime = SimpleNamespace()

    assert {tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)} == {
        REQUEST_TOOLS_NAME
    }
    arguments = json.dumps(
        {"query": "搜索并发送网易云单曲", "max_results": 1},
        ensure_ascii=False,
    )
    call = ToolCall(
        id="request-from-none",
        function=ToolFunction(name=REQUEST_TOOLS_NAME, arguments=arguments),
    )
    backend.begin_batch((call,), agent_runtime)
    requested = json.loads(await backend.execute(REQUEST_TOOLS_NAME, arguments, agent_runtime))

    assert requested["ok"] is True
    assert requested["data"]["loaded_tools"][0]["name"] == "song_share"
    assert "song_share" in {
        tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)
    }


@pytest.mark.asyncio
async def test_artifact_reads_do_not_add_a_separate_internal_budget() -> None:
    calls: list[str] = []

    async def execute(name: str, _arguments: str, _runtime: object) -> object:
        calls.append(name)
        return {"ok": True, "data": {"read": len(calls)}}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _runtime: (
                ChatTool(
                    name="read_tool_artifact",
                    description="read",
                    parameters={"type": "object", "properties": {}},
                ),
            ),
            execute=execute,
        )
    )
    backend = _ChatAgentBackend(_Service(registry), _runtime())  # type: ignore[arg-type]
    agent_runtime = SimpleNamespace(max_model_requests=10)
    exposed = {tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)}
    assert "read_tool_artifact" in exposed
    calls_in_batch = tuple(
        ToolCall(
            id=f"artifact-{index}",
            function=ToolFunction(
                name="read_tool_artifact",
                arguments=json.dumps({"handle": f"handle-{index}"}),
            ),
        )
        for index in range(5)
    )
    backend.begin_batch(calls_in_batch, agent_runtime)

    results = [
        json.loads(
            await backend.execute(
                call.function.name,
                call.function.arguments,
                agent_runtime,
            )
        )
        for call in calls_in_batch
    ]

    assert len(calls) == 5
    assert all(result["ok"] is True for result in results)


@pytest.mark.asyncio
async def test_reply_target_control_rejects_unseen_event_without_overriding() -> None:
    service = _Service(_registry([]))
    control = ReplyTargetControl(visible_event_ids=frozenset({42}))
    runtime = replace(_runtime(), reply_target_control=control)
    backend = _ChatAgentBackend(service, runtime)  # type: ignore[arg-type]
    agent_runtime = SimpleNamespace()
    backend.definitions(agent_runtime, web_was_used=False)
    arguments = json.dumps({"event_id": 99})
    call = ToolCall(
        id="unseen-reply-target",
        function=ToolFunction(name="set_reply_target", arguments=arguments),
    )

    backend.begin_batch((call,), agent_runtime)
    rejected = json.loads(await backend.execute("set_reply_target", arguments, agent_runtime))

    assert rejected["ok"] is False
    assert rejected["outcome"] == "event_not_visible"
    assert control.override_applied is False


def test_reply_target_control_is_not_exposed_to_scheduled_automation() -> None:
    service = _Service(_registry([]))
    runtime = replace(
        _runtime(),
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        tool_mode=ToolMode.NONE,
        tool_groups=frozenset(),
        selected_tool_names=frozenset(),
        reply_target_control=ReplyTargetControl(visible_event_ids=frozenset({42})),
    )
    backend = _ChatAgentBackend(service, runtime)  # type: ignore[arg-type]

    definitions = backend.definitions(SimpleNamespace(), web_was_used=False)

    assert definitions == ()


def test_mutation_access_exposes_only_memory_write_capability_initially() -> None:
    async def execute(name: str, _arguments: str, _runtime: object) -> object:
        return {"ok": True, "data": {"called": name}}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _runtime: (
                _tool("get_my_capabilities", "list capabilities"),
                _tool("get_person_memories", "read memories"),
                _tool("memory_change", "change durable memory"),
                _tool("web_search", "search the web"),
            ),
            execute=execute,
        )
    )
    registry.register(
        InProcessToolProvider(
            provider_id="admin",
            source=CapabilityTrustSource.ADMIN,
            definitions=lambda _runtime: (_tool("admin_execute_action", "run an admin action"),),
            execute=execute,
        )
    )
    runtime = replace(
        _runtime(),
        actor_is_superuser=True,
        allow_admin_actions=True,
        memory_session=_exclusive_session(),
        tool_groups=frozenset({"memory", "admin", "web"}),
        selected_tool_names=frozenset({"web_search"}),
    )
    backend = _ChatAgentBackend(_Service(registry), runtime)  # type: ignore[arg-type]

    names = {tool.name for tool in backend.definitions(SimpleNamespace(), web_was_used=False)}

    assert "memory_change" in names
    assert "request_tools" not in names
    assert "get_my_capabilities" not in names
    assert "get_person_memories" not in names
    assert "admin_execute_action" not in names
    assert "web_search" not in names

    backend._memory_session.locator_open = True  # type: ignore[union-attr]
    fallback_names = {
        tool.name for tool in backend.definitions(SimpleNamespace(), web_was_used=False)
    }
    assert "request_tools" in fallback_names
    assert "memory_change" in fallback_names
    assert "admin_execute_action" not in fallback_names
    assert "web_search" not in fallback_names

    read_backend = _ChatAgentBackend(  # type: ignore[arg-type]
        _Service(registry),
        replace(
            runtime,
            memory_session=_FakeMemorySession(eager_read=True),
            selected_tool_names=None,
        ),
    )
    read_names = {
        tool.name for tool in read_backend.definitions(SimpleNamespace(), web_was_used=False)
    }
    assert "get_person_memories" in read_names
    assert "memory_change" not in read_names
    assert "admin_execute_action" not in read_names
    assert "web_search" not in read_names


@pytest.mark.asyncio
async def test_mutation_access_rejects_early_request_tools_call() -> None:
    backend = _ChatAgentBackend(  # type: ignore[arg-type]
        _Service(_registry([])),
        replace(_runtime(), memory_session=_exclusive_session()),
    )
    agent_runtime = SimpleNamespace()
    call = ToolCall(
        id="early-request-tools",
        function=ToolFunction(
            name=REQUEST_TOOLS_NAME,
            arguments=json.dumps({"query": "read memory", "max_results": 1}),
        ),
    )
    backend.begin_batch((call,), agent_runtime)

    result = json.loads(
        await backend.execute(
            REQUEST_TOOLS_NAME,
            call.function.arguments,
            agent_runtime,
        )
    )

    assert result["ok"] is False
    assert result["error"] == "capability_not_loaded"


def test_mutation_locator_failure_can_load_memory_read_without_widening_initial_path() -> None:
    async def execute(name: str, _arguments: str, _runtime: object) -> object:
        return {"ok": True, "data": {"called": name}}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _runtime: (
                _tool("get_person_memories", "read durable person memories"),
                _tool("memory_change", "change durable memory"),
            ),
            execute=execute,
        )
    )
    backend = _ChatAgentBackend(  # type: ignore[arg-type]
        _Service(registry),
        replace(
            _runtime(),
            memory_session=_exclusive_session(),
            tool_groups=frozenset({"memory"}),
            selected_tool_names=None,
        ),
    )
    agent_runtime = SimpleNamespace()
    assert {tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)} == {
        "memory_change"
    }

    backend._memory_session.locator_open = True  # type: ignore[union-attr]
    locator_names = {
        tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)
    }
    assert locator_names == {"get_person_memories", "memory_change"}


def test_mutation_completion_gate_uses_last_real_receipt() -> None:
    service = _Service(_registry([]))
    backend = _ChatAgentBackend(  # type: ignore[arg-type]
        service,
        replace(_runtime(), memory_session=_exclusive_session()),
    )

    assert "未执行" in backend.finalize("已经改好了", SimpleNamespace())
    backend._memory_session = _exclusive_session(
        result={
            "ok": True,
            "mutation_committed": True,
            "data": {
                "applied_operation": "invalidate",
                "outcome": "committed",
            },
        }
    )

    rendered = backend.finalize("已经永久删除", SimpleNamespace())

    assert "撤回并失效" in rendered
    assert "审计记录仍保留" in rendered
    assert "永久删除" not in rendered
    assert (
        backend._memory_mutation_outcome(
            {
                "ok": True,
                "mutation_committed": True,
                "data": {"applied_operation": "noop", "outcome": "no_change"},
            }
        )
        == "noop"
    )
    assert (
        backend._memory_mutation_outcome(
            {
                "ok": False,
                "error_code": "memory_candidate_ambiguous",
                "data": {"candidates": []},
            }
        )
        == "ambiguous"
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {
                "ok": True,
                "mutation_committed": True,
                "data": {"applied_operation": "create", "outcome": "committed"},
            },
            "写入长期记忆",
        ),
        (
            {
                "ok": True,
                "mutation_committed": True,
                "data": {"applied_operation": "correct", "outcome": "committed"},
            },
            "旧版本不再作为当前有效答案",
        ),
        (
            {
                "ok": True,
                "mutation_committed": True,
                "data": {"applied_operation": "restore", "outcome": "committed"},
            },
            "恢复为有效状态",
        ),
        (
            {
                "ok": True,
                "mutation_committed": True,
                "data": {"applied_operation": "noop", "outcome": "no_change"},
            },
            "没有发生变化",
        ),
        (
            {
                "ok": True,
                "mutation_committed": True,
                "data": {"applied_operation": "contest", "outcome": "committed_as_contested"},
            },
            "没有按原请求直接覆盖或删除",
        ),
        (
            {
                "ok": False,
                "error_code": "memory_candidate_not_found",
                "data": {"candidates": []},
            },
            "没有找到可唯一定位的目标",
        ),
    ],
)
def test_mutation_completion_gate_renders_domain_outcomes(
    result: dict[str, object],
    expected: str,
) -> None:
    backend = _ChatAgentBackend(  # type: ignore[arg-type]
        _Service(_registry([])),
        replace(_runtime(), memory_session=_exclusive_session(result=result)),
    )

    assert expected in backend.finalize("模型声称全部成功", SimpleNamespace())


def test_mutation_completion_gate_bounds_ambiguous_candidates() -> None:
    backend = _ChatAgentBackend(  # type: ignore[arg-type]
        _Service(_registry([])),
        replace(
            _runtime(),
            memory_session=_exclusive_session(
                result={
                    "ok": False,
                    "error_code": "memory_candidate_ambiguous",
                    "data": {
                        "candidates": [
                            {
                                "fact_id": fact_id,
                                "memory_ref": f"M{fact_id}",
                                "key": f"test:{fact_id}",
                                "content": f"candidate {fact_id}",
                            }
                            for fact_id in range(1, 5)
                        ]
                    },
                }
            ),
        ),
    )

    rendered = backend.finalize("任选一个就当成功", SimpleNamespace())

    assert "尚未执行" in rendered
    assert "M1" in rendered and "M3" in rendered
    assert "M4" not in rendered


def test_core_search_tags_recall_tools_from_natural_chinese_phrases() -> None:
    async def execute(name: str, _arguments: str, _runtime: object) -> object:
        return {"ok": True, "data": name}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _runtime: tuple(
                _tool(name, "核心能力")
                for name in (
                    "get_recent_chat_history",
                    "search_chat_history",
                    "get_person_memories",
                    "memory_change",
                    "web_search",
                    "read_webpage",
                )
            ),
            execute=execute,
        )
    )
    catalog = registry.catalog(object())
    cases = {
        "刚刚说了什么": "get_recent_chat_history",
        "他以前提过吗": "search_chat_history",
        "你记得我的爱好吗": "get_person_memories",
        "请记住我不喝咖啡": "memory_change",
        "搜一下最新新闻": "web_search",
        "看看这个网页": "read_webpage",
    }

    for query, expected in cases.items():
        matches = match_requestable_tools(catalog, query=query, limit=1)
        assert matches
        assert matches[0].entry.descriptor.model_name == expected


def test_builtin_planner_scope_descriptions_explain_actual_capabilities() -> None:
    service = object.__new__(ChatService)
    service._plugin_tools = None
    service._external_tool_providers = []

    scopes = {
        item.scope_id: item.description
        for item in service.planner_tool_scopes(
            ("memory", "web", "automation", "onebot", "capability")
        )
    }

    assert "搜索近期或永久聊天历史" in scopes["memory"]
    assert "读取网页" in scopes["web"]
    assert "周期任务" in scopes["automation"]
    assert "QQ 平台" in scopes["onebot"]
    assert "真实用户" in scopes["capability"]


def test_tool_exposure_log_records_scopes_and_final_tool_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="qq_ai_bot.services.chat")
    backend = _ChatAgentBackend(_Service(_registry([])), _runtime())  # type: ignore[arg-type]

    backend.definitions(SimpleNamespace(), web_was_used=False)

    assert "agent_tools_exposed" in caplog.text
    assert "planner_scope_source=explicit" in caplog.text
    assert "planner_scopes=plugin" in caplog.text
    assert "effective_scopes=plugin" in caplog.text
    assert "tools=album_share,request_tools" in caplog.text
    assert "exposed_count=2" in caplog.text
    assert "private:10001" not in caplog.text


def test_tool_exposure_log_distinguishes_inherited_scopes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="qq_ai_bot.services.chat")
    runtime = replace(
        _runtime(),
        tool_groups=frozenset(),
        planner_scopes_explicit=False,
    )
    backend = _ChatAgentBackend(_Service(_registry([])), runtime)  # type: ignore[arg-type]

    backend.definitions(SimpleNamespace(), web_was_used=False)

    assert "planner_scope_source=inherited" in caplog.text
    assert "planner_scopes=backend_authorized" in caplog.text
    assert "effective_scopes=backend_authorized" in caplog.text
    assert "memory_scope_added=False" in caplog.text


def test_tool_exposure_log_identifies_deterministic_memory_scope_addition(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="qq_ai_bot.services.chat")
    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _runtime: (
                _tool("get_my_capabilities", "list available capabilities"),
                _tool("memory_change", "change durable memory"),
            ),
            execute=lambda *_args: None,  # type: ignore[arg-type]
        )
    )
    runtime = replace(
        _runtime(),
        tool_groups=frozenset({"memory"}),
        planner_scopes_explicit=False,
        planner_tool_groups=frozenset(),
        selected_tool_names=frozenset({"memory_change"}),
    )
    backend = _ChatAgentBackend(_Service(registry), runtime)  # type: ignore[arg-type]

    backend.definitions(SimpleNamespace(), web_was_used=False)

    assert "memory_scope_added=True" in caplog.text


@pytest.mark.asyncio
async def test_inherited_scope_preloads_only_positive_relevance_up_to_six_tools() -> None:
    async def execute(name: str, _arguments: str, _runtime: object) -> object:
        return {"ok": True, "data": name}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="plugin",
            source=CapabilityTrustSource.PLUGIN,
            definitions=lambda _runtime: (
                tuple(_tool(f"music_{index}", "music lookup and sharing") for index in range(8))
                + tuple(_tool(f"weather_{index}", "weather forecast") for index in range(4))
            ),
            execute=execute,
        )
    )
    runtime = replace(
        _runtime(),
        runtime_config=SimpleNamespace(
            tooling=SimpleNamespace(
                selected_tool_limit=32,
                schema_token_budget=12_000,
                result_artifact_retention_seconds=86_400,
            ),
            mcp=None,
            agent=SimpleNamespace(tool_result_max_characters=32_000),
            web=SimpleNamespace(max_calls_per_turn=3),
        ),
        tool_groups=frozenset(),
        planner_scopes_explicit=False,
        planner_tool_groups=frozenset(),
        selection_query="music",
        planner_intent="share music",
        selected_tool_names=None,
    )

    prepared = await _CandidateChatService(registry)._prepare_tool_candidates(runtime)

    assert prepared.selected_tool_names is not None
    assert len(prepared.selected_tool_names) == 6
    assert all(name.startswith("music_") for name in prepared.selected_tool_names)


def test_explicit_scope_exposes_complete_package_despite_inherited_count_limit() -> None:
    runtime = replace(
        _runtime(),
        runtime_config=SimpleNamespace(
            tooling=SimpleNamespace(selected_tool_limit=1, schema_token_budget=None),
            mcp=None,
            agent=SimpleNamespace(tool_result_max_characters=32_000),
            web=SimpleNamespace(max_calls_per_turn=3),
        ),
        selected_tool_names=None,
    )
    backend = _ChatAgentBackend(_Service(_registry([])), runtime)  # type: ignore[arg-type]

    exposed = {tool.name for tool in backend.definitions(SimpleNamespace(), web_was_used=False)}

    assert exposed == {"album_share", "song_share", REQUEST_TOOLS_NAME}


def test_memory_tools_are_not_forced_by_query_text() -> None:
    assert not hasattr(ChatService, "_retain_turn_required_tools")


@pytest.mark.asyncio
async def test_agent_can_request_and_then_call_an_omitted_authorized_tool() -> None:
    calls: list[str] = []
    service = _Service(_registry(calls))
    backend = _ChatAgentBackend(service, _runtime())  # type: ignore[arg-type]
    agent_runtime = SimpleNamespace()

    first = {tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)}
    assert first == {"album_share", REQUEST_TOOLS_NAME}
    assert service._tool_metrics.tool_enabled_turns == 1
    assert service._tool_metrics.planner_scope_turns[True] == 1

    hidden_call = ToolCall(
        id="hidden",
        function=ToolFunction(name="song_share", arguments="{}"),
    )
    backend.begin_batch((hidden_call,), agent_runtime)
    hidden = json.loads(
        await backend.execute("song_share", "{}", agent_runtime)  # type: ignore[arg-type]
    )
    assert hidden["error"] == "capability_not_loaded"
    assert calls == []

    request_arguments = json.dumps(
        {"query": "搜索并发送网易云单曲", "max_results": 1},
        ensure_ascii=False,
    )
    request_call = ToolCall(
        id="request",
        function=ToolFunction(name=REQUEST_TOOLS_NAME, arguments=request_arguments),
    )
    backend.begin_batch((request_call,), agent_runtime)
    requested = json.loads(
        await backend.execute(
            REQUEST_TOOLS_NAME,
            request_arguments,
            agent_runtime,  # type: ignore[arg-type]
        )
    )
    assert requested["ok"] is True
    assert requested["data"]["loaded_tools"][0]["name"] == "song_share"
    assert service._tool_metrics.request_tools_calls == 1
    assert service._tool_metrics.request_tools_zero_results == 0
    assert service._tool_metrics.automatic_memory_request_tools_calls == 1

    second = {tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)}
    assert second == {"album_share", "song_share", REQUEST_TOOLS_NAME}
    assert service._tool_metrics.tool_enabled_turns == 1

    song_call = ToolCall(id="song", function=ToolFunction(name="song_share", arguments="{}"))
    backend.begin_batch((song_call,), agent_runtime)
    outcome = json.loads(
        await backend.execute("song_share", "{}", agent_runtime)  # type: ignore[arg-type]
    )
    assert outcome["ok"] is True
    assert calls == ["song_share"]
    assert service._tool_metrics.first_round_tool_hits[False] == 1


@pytest.mark.asyncio
async def test_first_real_tool_call_records_initial_schema_hit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="qq_ai_bot.services.chat")
    calls: list[str] = []
    service = _Service(_registry(calls))
    backend = _ChatAgentBackend(service, _runtime())  # type: ignore[arg-type]
    agent_runtime = SimpleNamespace()
    backend.definitions(agent_runtime, web_was_used=False)
    call = ToolCall(id="album", function=ToolFunction(name="album_share", arguments="{}"))

    backend.begin_batch((call,), agent_runtime)
    outcome = json.loads(
        await backend.execute("album_share", "{}", agent_runtime)  # type: ignore[arg-type]
    )

    assert outcome["ok"] is True
    assert service._tool_metrics.first_round_tool_hits[True] == 1
    assert "agent_first_round_tool_hit" in caplog.text
    assert "hit=True" in caplog.text


@pytest.mark.asyncio
async def test_agent_can_request_authorized_tool_outside_planner_priority_scopes() -> None:
    calls: list[str] = []
    backend = _ChatAgentBackend(_Service(_registry(calls)), _runtime())  # type: ignore[arg-type]
    agent_runtime = SimpleNamespace()

    exposed = {tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)}
    assert exposed == {"album_share", REQUEST_TOOLS_NAME}

    arguments = json.dumps(
        {"query": "web_search", "max_results": 1},
        ensure_ascii=False,
    )
    request_call = ToolCall(
        id="request-web",
        function=ToolFunction(name=REQUEST_TOOLS_NAME, arguments=arguments),
    )
    backend.begin_batch((request_call,), agent_runtime)
    result = json.loads(
        await backend.execute(REQUEST_TOOLS_NAME, arguments, agent_runtime)  # type: ignore[arg-type]
    )

    assert result["ok"] is True
    assert result["data"]["loaded_tools"][0]["name"] == "web_search"
    assert calls == []
