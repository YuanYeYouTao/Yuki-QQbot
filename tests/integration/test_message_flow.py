"""Full normalized-event to persisted-reply integration tests."""

from __future__ import annotations

import asyncio
import json

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from tests.conftest import MemorySender, build_harness, make_settings
from tests.fakes import FakeWebSearchProvider
from tests.unit.test_normalizer import group_event, private_event
from tests.unit.test_runtime_admin import _request_tools_response, admin_stack

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.models import RetryPolicy, RiskClass, TurnOrigin
from qq_ai_bot.automation.registry import (
    AutomationCapability,
    CapabilityArguments,
    build_capability_registry,
)
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.admin.config_admin import ConfigAdminService
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.web.models import WebMode, WebSearchResponse, WebSearchSource


def _attach_test_automation(database: Database, harness, settings):
    registry = build_capability_registry()
    registry.register(
        AutomationCapability(
            name="mcp.mcd.query-meals",
            description="查询指定门店菜单",
            argument_model=CapabilityArguments,
            output_schema={"type": "object"},
            required_permission=PermissionLevel.USER,
            risk_class=RiskClass.READ,
            retry_policy=RetryPolicy.TRANSIENT_ONCE,
            allowed_origins=frozenset({TurnOrigin.SCHEDULED_AUTOMATION}),
        )
    )
    repository = AutomationRepository(database)
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=TimeContextService(database),
    )
    harness.processor._chat.set_automation_tools(AutomationToolService(service))
    return repository, registry


@pytest.mark.asyncio
async def test_future_mcd_query_is_persisted_instead_of_executed_immediately(
    database: Database,
) -> None:
    calls = 0
    settings = make_settings(
        database.url,
        automation_enabled=True,
        automation_max_llm_calls_per_run=10,
    )
    capability_ids: dict[str, str] = {}

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        assert {
            "automation_create",
            "request_tools",
        } <= {tool.name for tool in request.tools}
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="create-future-query",
                        function=ToolFunction(
                            name="automation_create",
                            arguments=json.dumps(
                                {
                                    "task": {
                                        "name": "两分钟后查询早餐套餐",
                                        "goal": (
                                            "查询 storeCode 1410135 当前可用早餐套餐和价格，不下单"
                                        ),
                                        "trigger": {"type": "after", "seconds": 120},
                                        "strategy": "agentic",
                                        "capabilities": [capability_ids["query_meals"]],
                                    }
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        tool_payload = json.loads(
            next(
                message.content or "{}"
                for message in reversed(request.messages)
                if message.role == "tool"
            )
        )
        assert tool_payload["data"]["confirmation"] == "persisted"
        return ChatResponse(content="设好了，两分钟后再查询", latency_seconds=0)

    harness = build_harness(database, settings, FakeLLMProvider(responder))
    repository, registry = _attach_test_automation(database, harness, settings)
    safe_id = registry.agent_tool_name("mcp.mcd.query-meals")
    capability_ids["query_meals"] = safe_id
    sender = MemorySender()

    result = await harness.processor.handle(
        normalize_event(
            private_event(
                Message(
                    "两分钟后查询 storeCode 1410135 当前可用的早餐套餐，"
                    "把套餐名称和价格简短发给我，不要下单"
                ),
                message_id=105,
            )
        ),
        sender,
    )

    assert result.reason == "chat"
    assert calls == 2
    tasks = await repository.list_for_creator("1001")
    assert len(tasks) == 1
    assert tasks[0].required_capabilities == (
        "yuki.agent",
        "mcp.mcd.query-meals",
        "onebot.send_private_message",
    )
    assert sender.messages[0].text == "设好了，两分钟后再查询"


@pytest.mark.asyncio
async def test_future_task_success_claim_is_blocked_without_create_tool_result(
    database: Database,
) -> None:
    settings = make_settings(database.url, automation_enabled=True)

    def responder(request: ChatRequest) -> ChatResponse:
        assert {
            "automation_create",
            "request_tools",
        } <= {tool.name for tool in request.tools}
        return ChatResponse(content="设好了，明天九点四十五分准时查", latency_seconds=0)

    harness = build_harness(database, settings, FakeLLMProvider(responder))
    repository, _registry = _attach_test_automation(database, harness, settings)
    sender = MemorySender()
    await harness.processor.handle(
        normalize_event(
            private_event(
                Message(
                    "明天早上九点四十五分，在 storeCode 1410135 查询双层原味板烧鸡腿麦满分套餐"
                ),
                message_id=106,
            )
        ),
        sender,
    )

    assert await repository.list_for_creator("1001") == ()
    assert sender.messages[0].text == "这个定时任务还没有写入任务列表，不能算创建成功。"


@pytest.mark.asyncio
async def test_automation_hint_keeps_web_search_available(database: Database) -> None:
    calls = 0

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            tool_names = {tool.name for tool in request.tools}
            assert "automation_create" in tool_names
            assert "web_search" in tool_names
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="current-web-query",
                        function=ToolFunction(
                            name="web_search",
                            arguments=json.dumps(
                                {"query": "DeepSeek Responses API 官方文档"},
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        payload = json.loads(request.messages[-1].content or "{}")
        assert payload["ok"] is True
        return ChatResponse(content="已根据当前网页总结三项功能。", latency_seconds=0)

    settings = make_settings(
        database.url,
        automation_enabled=True,
        web_enabled=True,
        web_mode=WebMode.TAVILY,
        tavily_api_key="test-placeholder",
    )
    web = FakeWebSearchProvider(
        response=WebSearchResponse(
            query="DeepSeek Responses API 官方文档",
            sources=(
                WebSearchSource(
                    source_id="deepseek-docs",
                    title="DeepSeek Responses API",
                    url="https://api-docs.deepseek.com/zh-cn/guides/responses_api/",
                    domain="api-docs.deepseek.com",
                    snippet="Responses API 官方说明",
                    relevant_content="Responses API 支持文本生成和工具调用。",
                ),
            ),
            provider_request_id="deepseek-docs-request",
            latency_seconds=0,
        )
    )
    harness = build_harness(
        database,
        settings,
        FakeLLMProvider(responder),
        web_provider=web,
    )
    repository, _registry = _attach_test_automation(database, harness, settings)
    sender = MemorySender()

    result = await harness.processor.handle(
        normalize_event(
            private_event(
                Message(
                    "请联网搜索 DeepSeek Responses API 官方文档当前支持的功能。"
                    "请只根据你这次实际打开的网页回答，列出 3 点，并附上实际来源链接。"
                ),
                message_id=108,
            )
        ),
        sender,
    )

    assert result.reason == "chat"
    assert calls == 2
    assert len(web.search_requests) == 1
    assert await repository.list_for_creator("1001") == ()
    assert len(sender.messages) == 1
    assert sender.messages[0].text.startswith("已根据当前网页总结三项功能。")
    assert "DeepSeek Responses API" in sender.messages[0].text


@pytest.mark.asyncio
async def test_private_and_group_mention_end_to_end(database: Database) -> None:
    provider = FakeLLMProvider()
    harness = build_harness(database, make_settings(database.url), provider)

    private_sender = MemorySender()
    private = normalize_event(private_event(Message("private question"), message_id=101))
    private_result = await harness.processor.handle(private, private_sender)

    group_sender = MemorySender()
    group = normalize_event(
        group_event(
            Message([MessageSegment.at(9999), MessageSegment.text("group question")]),
            message_id=102,
        )
    )
    group_result = await harness.processor.handle(group, group_sender)

    assert private_result.reason == "chat" and group_result.reason == "chat"
    assert private_sender.messages[0].text.endswith("private question")
    assert group_sender.messages[0].text.endswith("group question")
    assert await harness.conversations.count_messages(ConversationIdentity.private("1001")) == 2
    assert (
        await harness.conversations.count_messages(ConversationIdentity.group("2001", "1001")) == 2
    )


@pytest.mark.asyncio
async def test_ordinary_natural_language_capability_question_calls_current_user_tool(
    database: Database,
) -> None:
    calls = 0

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "get_my_capabilities" in {tool.name for tool in request.tools}
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="my-capabilities",
                        function=ToolFunction(
                            name="get_my_capabilities",
                            arguments=json.dumps({"mode": "summary"}),
                        ),
                    ),
                ),
            )
        assert "get_my_capabilities" in {tool.name for tool in request.tools}
        payload = json.loads(
            next(
                message.content or "{}"
                for message in reversed(request.messages)
                if message.role == "tool"
            )
        )
        assert payload["data"]["transient_internal_reference"] is True
        assert payload["data"]["do_not_copy_verbatim_to_user"] is True
        assert payload["data"]["counts"]["self_service_operations"] == 37
        return ChatResponse(
            content="你目前有 37 项本人自助能力，其中 17 项会修改本人数据；不能修改系统配置。",
            latency_seconds=0,
        )

    provider = FakeLLMProvider(responder)
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()
    result = await harness.processor.handle(
        normalize_event(
            private_event(
                Message("Yuki，我能修改什么？能改多少参数？"),
                message_id=104,
            )
        ),
        sender,
    )

    assert result.reason == "chat"
    assert calls == 2
    rendered = "\n".join(message.text for message in sender.messages)
    assert rendered == "你目前有 37 项本人自助能力，其中 17 项会修改本人数据；不能修改系统配置。"
    assert "transient_internal_reference" not in rendered
    events = await EventLedgerRepository(database).list_recent(
        scope_type=ScopeType.PRIVATE,
        user_id="1001",
        group_id=None,
        limit=10,
    )
    persisted = "\n".join(event.content for event in events)
    assert "transient_internal_reference" not in persisted
    assert "permission_levels" not in persisted


@pytest.mark.asyncio
async def test_capability_runtime_keeps_authority_tool_discovery(
    database: Database,
) -> None:
    def responder(request: ChatRequest) -> ChatResponse:
        names = {tool.name for tool in request.tools}
        assert "request_tools" in names
        assert "set_reply_target" in names
        return ChatResponse(content="可以，告诉我你想了解哪一类能力", latency_seconds=0)

    harness = build_harness(
        database,
        make_settings(database.url),
        FakeLLMProvider(responder),
    )

    result = await harness.processor.handle(
        normalize_event(private_event(Message("你能做什么"), message_id=106)),
        MemorySender(),
    )

    assert result.reason == "chat"


@pytest.mark.asyncio
async def test_user_query_can_expose_memory_write_without_planner_scopes(
    database: Database,
) -> None:
    def responder(request: ChatRequest) -> ChatResponse:
        tool_names = {tool.name for tool in request.tools}
        assert "memory_change" in tool_names or "request_tools" in tool_names
        return ChatResponse(content="可以，我先检查相关记忆", latency_seconds=0)

    harness = build_harness(
        database,
        make_settings(database.url),
        FakeLLMProvider(responder),
    )

    result = await harness.processor.handle(
        normalize_event(private_event(Message("忘掉我之前的一条记忆"), message_id=107)),
        MemorySender(),
    )

    assert result.reason == "chat"


@pytest.mark.asyncio
async def test_ordinary_capability_payload_echo_is_neither_sent_nor_persisted(
    database: Database,
) -> None:
    calls = 0

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="my-capabilities-echo",
                        function=ToolFunction(
                            name="get_my_capabilities",
                            arguments=json.dumps({"mode": "summary"}),
                        ),
                    ),
                ),
            )
        payload = next(
            message.content or "{}"
            for message in reversed(request.messages)
            if message.role == "tool"
        )
        return ChatResponse(content=payload, latency_seconds=0)

    harness = build_harness(
        database,
        make_settings(database.url),
        FakeLLMProvider(responder),
    )
    sender = MemorySender()
    result = await harness.processor.handle(
        normalize_event(private_event(Message("Yuki，我能修改什么？"), message_id=105)),
        sender,
    )

    assert result.reason == "chat"
    rendered = "\n".join(message.text for message in sender.messages)
    assert "内部读取" in rendered
    assert "transient_internal_reference" not in rendered
    assert "do_not_copy_verbatim_to_user" not in rendered
    events = await EventLedgerRepository(database).list_recent(
        scope_type=ScopeType.PRIVATE,
        user_id="1001",
        group_id=None,
        limit=10,
    )
    persisted = "\n".join(event.content for event in events)
    assert "transient_internal_reference" not in persisted
    assert "do_not_copy_verbatim_to_user" not in persisted


@pytest.mark.asyncio
async def test_short_plain_chat_without_line_break_stays_one_message(
    database: Database,
) -> None:
    provider = FakeLLMProvider(lambda _request: "第一句。第二句！")
    settings = make_settings(database.url)
    harness = build_harness(database, settings, provider)
    sender = MemorySender()
    event = normalize_event(private_event(Message("聊聊天"), message_id=103))

    result = await harness.processor.handle(event, sender)

    assert result.reason == "chat"
    assert result.sent_messages == 1
    assert [message.text for message in sender.messages] == ["第一句。第二句！"]
    history = await harness.conversations.list_context(
        ConversationIdentity.private("1001"),
        max_messages=10,
        max_characters=1000,
    )
    assert [item.content for item in history[-1:]] == ["第一句。第二句！"]


@pytest.mark.asyncio
async def test_ten_concurrent_conversations_do_not_cross_context(database: Database) -> None:
    provider = FakeLLMProvider()
    harness = build_harness(database, make_settings(database.url), provider)
    senders = [MemorySender() for _ in range(10)]
    messages = [
        normalize_event(
            private_event(
                Message(f"unique-{index}"),
                message_id=200 + index,
                user_id=1001 + index,
            )
        )
        for index in range(10)
    ]

    await asyncio.gather(
        *(
            harness.processor.handle(message, sender)
            for message, sender in zip(messages, senders, strict=True)
        )
    )

    assert len(provider.requests) == 10
    for index in range(10):
        identity = ConversationIdentity.private(str(1001 + index))
        history = await harness.conversations.list_context(
            identity, max_messages=10, max_characters=1000
        )
        contents = [item.content for item in history]
        assert contents[0] == f"unique-{index}"
        assert contents[1] == f"FakeLLM: unique-{index}"
        assert senders[index].messages[0].text == contents[1]


@pytest.mark.asyncio
async def test_natural_and_deterministic_config_entrypoints_share_runtime_instance(
    database: Database,
) -> None:
    calls = 0

    def responder(_request: object) -> ChatResponse:
        nonlocal calls
        request = _request
        assert isinstance(request, ChatRequest)
        if "admin_set_config" not in {tool.name for tool in request.tools}:
            return _request_tools_response("admin_set_config")
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="natural-set",
                        function=ToolFunction(
                            name="admin_set_config",
                            arguments=json.dumps(
                                {
                                    "key": "conversation.autonomous_batch_limit",
                                    "value": 10,
                                    "scope_type": "global",
                                    "scope_id": "",
                                }
                            ),
                        ),
                    ),
                ),
            )
        return ChatResponse(content="已立即改为 10。", latency_seconds=0)

    settings = make_settings(database.url)
    provider = FakeLLMProvider(responder)
    harness = build_harness(database, settings, provider)
    runtime, capabilities = admin_stack(database)
    harness.processor._runtime_config = runtime
    harness.processor._config_admin = ConfigAdminService(runtime)
    harness.processor._chat._runtime_config = runtime
    harness.processor._chat.set_admin_tools(capabilities)

    natural_sender = MemorySender()
    natural = normalize_event(
        private_event(
            Message("把每小时自动插话次数改成 10"),
            message_id=410,
            user_id=9000,
        )
    )
    natural_result = await harness.processor.handle(natural, natural_sender)
    assert natural_result.reason == "chat"

    command_sender = MemorySender()
    command = normalize_event(
        private_event(
            Message("/ai config get conversation.autonomous_batch_limit"),
            message_id=411,
            user_id=9000,
        )
    )
    command_result = await harness.processor.handle(command, command_sender)
    assert command_result.reason == "command_config"
    assert "10" in command_sender.messages[0].text
    assert harness.processor._config_admin._runtime_config is runtime
