"""Offline end-to-end coverage for a generic bundled MCP mutation flow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import Message
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_normalizer import private_event

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.mcp.fake import FakeMCPConnection
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.provider import MCPToolProvider
from qq_ai_bot.mcp.repository import MCPRepository, ToolArtifactRepository
from qq_ai_bot.persistence.database import Database

_REMOTE_SEQUENCE = (
    "query-store",
    "query-meals",
    "query-meal-detail",
    "calculate-price",
    "create-order",
)


def _remote_tool(name: str) -> SimpleNamespace:
    properties: dict[str, object] = {}
    if name == "query-meal-detail":
        properties = {"mealCode": {"type": "string"}}
    return SimpleNamespace(
        name=name,
        description=f"offline {name}",
        inputSchema={
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        },
        outputSchema={"type": "object"},
        annotations=None,
    )


def _result(data: object) -> SimpleNamespace:
    return SimpleNamespace(content=(), structuredContent=data, isError=False)


def _large_menu() -> dict[str, object]:
    return {
        "data": {
            "meals": {
                f"meal-{index:03d}": {
                    "name": "o麦金四件套随心选" if index == 73 else f"套餐 {index}",
                    "mealCode": "gold-four" if index == 73 else f"meal-{index:03d}",
                    "currentPrice": 31 if index == 73 else 20 + index / 10,
                    "description": "套餐详情" * 30,
                }
                for index in range(104)
            },
            "categories": [{"name": f"分类 {index}"} for index in range(15)],
        }
    }


def _large_meal_detail() -> dict[str, object]:
    desired = [
        {"name": "麦辣鸡腿堡", "code": "burger-spicy", "diffPrice": 0},
        {"name": "麦辣鸡翅2块", "code": "wing-spicy-2", "diffPrice": 2},
        {"name": "阳光柠檬红茶中杯", "code": "lemon-tea-medium", "diffPrice": 0},
    ]
    filler = [
        {
            "name": f"其他选项 {index}",
            "code": f"option-{index:03d}",
            "description": "选项说明" * 25,
        }
        for index in range(80)
    ]
    return {
        "data": {
            "mealCode": "gold-four",
            "options": [*desired, *filler],
        }
    }


@pytest.mark.asyncio
async def test_bundled_mcd_order_flow_commits_once_and_preserves_payment_url(
    database: Database,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp-order.json"
    token = "offline-secret-token"
    tools = tuple(_remote_tool(name) for name in dict.fromkeys(_REMOTE_SEQUENCE))
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcd": {
                        "command": "fake-mcd",
                        "env": {"TOKEN": token},
                        "yuki": {
                            "scope": "mcp.mcd",
                            "toolBundles": {
                                "order": {
                                    "scope": "mcp.mcd.order",
                                    "summary": "完整查询、校价和创建待支付订单",
                                    "includeTools": [
                                        "query-store",
                                        "query-meals",
                                        "query-meal-detail",
                                        "calculate-price",
                                        "create-order",
                                    ],
                                }
                            },
                            "toolAnnotations": {
                                "query-store": {"readOnlyHint": True},
                                "query-meals": {"readOnlyHint": True},
                                "query-meal-detail": {"readOnlyHint": True},
                                "calculate-price": {"readOnlyHint": True},
                                "create-order": {"finalizeAfterCommit": True},
                            },
                        },
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    connection = FakeMCPConnection(
        tools=tools,
        results={
            "query-store": _result({"storeCode": "1410135"}),
            "query-meals": _result(_large_menu()),
            "query-meal-detail": _result(_large_meal_detail()),
            "calculate-price": _result({"amount": 31.0, "status": "confirmed"}),
            "create-order": _result(
                {
                    "success": True,
                    "orderId": "001",
                    "status": "pending_payment",
                    "realTotalAmount": 31.0,
                    "payH5Url": "https://example.com/pay",
                    "expirePayTime": "2026-08-12 12:30:00",
                }
            ),
        },
    )
    manager = MCPManager(
        enabled=True,
        config_path=config_path,
        cache_enabled=False,
        metadata_cache_ttl_seconds=60,
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        max_parallel_calls=2,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    await manager.start()

    requests: list[ChatRequest] = []
    committed_seen = False

    def latest_tool_payload(request: ChatRequest) -> dict[str, object]:
        tool_messages = [message for message in request.messages if message.role == "tool"]
        assert tool_messages
        loaded = json.loads(tool_messages[-1].content or "{}")
        assert isinstance(loaded, dict)
        return loaded

    def model(request: ChatRequest) -> ChatResponse:
        nonlocal committed_seen
        requests.append(request)
        tool_messages = [message for message in request.messages if message.role == "tool"]
        if tool_messages:
            latest = latest_tool_payload(request)
            if latest.get("tool_name") == "create-order" and latest.get("ok"):
                committed_seen = latest.get("mutation_committed") is True
        index = len(requests) - 1
        visible_names = {tool.name for tool in request.tools}
        expected_bundle = {f"mcp__mcd__{name}" for name in dict.fromkeys(_REMOTE_SEQUENCE)}
        if visible_names:
            assert expected_bundle.issubset(visible_names)
            assert "read_tool_artifact" in visible_names
            assert "web_search" not in visible_names
        if index == 0:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"offline-{index}",
                        function=ToolFunction(
                            name="mcp__mcd__query-store",
                            arguments="{}",
                        ),
                    ),
                ),
            )
        if index == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"offline-{index}",
                        function=ToolFunction(name="mcp__mcd__query-meals", arguments="{}"),
                    ),
                ),
            )
        if index == 2:
            menu = latest_tool_payload(request)
            assert menu["truncated"] is True
            handle = str(menu["artifact_handle"])
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"offline-{index}",
                        function=ToolFunction(
                            name="read_tool_artifact",
                            arguments=json.dumps(
                                {
                                    "handle": handle,
                                    "operation": "search",
                                    "query": "四件套随心选",
                                    "limit": 5,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        if index == 3:
            search_result = latest_tool_payload(request)
            search_data = search_result["data"]
            assert isinstance(search_data, dict)
            matches = search_data["matches"]
            assert isinstance(matches, list)
            first_match = matches[0]
            assert isinstance(first_match, dict)
            first_value = first_match["value"]
            assert isinstance(first_value, dict)
            assert first_value["mealCode"] == "gold-four"
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"offline-{index}",
                        function=ToolFunction(
                            name="mcp__mcd__query-meal-detail",
                            arguments='{"mealCode":"gold-four"}',
                        ),
                    ),
                ),
            )
        if index == 4:
            detail = latest_tool_payload(request)
            assert detail["truncated"] is True
            handle = str(detail["artifact_handle"])
            queries = ("麦辣鸡腿堡", "麦辣鸡翅2块", "阳光柠檬红茶中杯")
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=tuple(
                    ToolCall(
                        id=f"detail-{query_index}",
                        function=ToolFunction(
                            name="read_tool_artifact",
                            arguments=json.dumps(
                                {
                                    "handle": handle,
                                    "operation": "search",
                                    "query": query,
                                    "limit": 3,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    for query_index, query in enumerate(queries)
                ),
            )
        if index == 5:
            tool_payloads = [json.loads(message.content or "{}") for message in tool_messages[-3:]]
            selected_codes = {
                payload["data"]["matches"][0]["value"]["code"] for payload in tool_payloads
            }
            assert selected_codes == {
                "burger-spicy",
                "wing-spicy-2",
                "lemon-tea-medium",
            }
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"offline-{index}",
                        function=ToolFunction(
                            name="mcp__mcd__calculate-price",
                            arguments="{}",
                        ),
                    ),
                ),
            )
        if index == 6:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"offline-{index}",
                        function=ToolFunction(name="mcp__mcd__create-order", arguments="{}"),
                    ),
                ),
            )
        assert index == 7
        assert request.tools == ()
        order = latest_tool_payload(request)
        assert order["mutation_committed"] is True
        order_data = order["data"]
        assert isinstance(order_data, dict)
        assert order_data["realTotalAmount"] == 31.0
        return ChatResponse(
            content=(
                "待支付订单已经创建，金额 31 元，订单号 001，"
                "请在 2026-08-12 12:30:00 前支付：https://example.com/pay"
            ),
            latency_seconds=0,
        )

    settings = make_settings(
        database.url,
        mcp_enabled=True,
        mcp_config_path=config_path,
        mcp_gateway_enabled=True,
        tooling_selected_tool_limit=32,
        agent_max_tool_calls=10,
        agent_max_model_requests=10,
        agent_tool_result_max_characters=8000,
    )
    harness = build_harness(database, settings, FakeLLMProvider(model))
    harness.processor._chat._tool_artifacts = ToolArtifactRepository(
        database,
        tmp_path / "artifacts",
        retention_seconds=60,
    )
    harness.processor._chat.register_tool_provider(
        MCPToolProvider(manager, gateway_enabled=True)
    )
    sender = MemorySender()
    try:
        outcome = await harness.processor.handle(
            normalize_event(
                private_event(
                    Message("帮我点麦辣鸡腿堡，到店取餐，创建待支付订单，把链接发给我。"),
                    message_id=212,
                )
            ),
            sender,
        )
    finally:
        await manager.close()

    assert outcome.reason == "chat"
    assert [name for name, _arguments in connection.calls].count("create-order") == 1
    assert committed_seen
    final_text = "\n".join(message.text for message in sender.messages)
    assert "https://example.com/pay" in final_text
    assert "31 元" in final_text
    assert "订单号 001" in final_text
    assert "pending_payment" not in final_text
    assert len(requests) == 8
    assert all("plugin" not in tool.name for request in requests for tool in request.tools)
    serialized_requests = json.dumps(
        [
            {
                "messages": [message.content for message in request.messages],
                "tools": [tool.name for tool in request.tools],
            }
            for request in requests
        ],
        ensure_ascii=False,
        default=str,
    )
    assert token not in serialized_requests
    assert all(request.messages[0].role == "system" for request in requests)
