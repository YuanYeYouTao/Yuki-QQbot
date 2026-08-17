"""Offline compatibility checks for the official McDonald's China MCP preset."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from qq_ai_bot.capabilities.models import CapabilityIdempotency, CapabilityRisk
from qq_ai_bot.mcp.config import load_mcp_config, redacted_server_config
from qq_ai_bot.mcp.connection import SDKMCPConnection
from qq_ai_bot.mcp.errors import classify_mcp_exception
from qq_ai_bot.mcp.fake import FakeMCPConnection
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.models import MCPServerConfig
from qq_ai_bot.mcp.provider import MCPToolProvider
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.persistence.database import Database


def _remote_tool(name: str, description: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}},
        outputSchema=None,
        annotations=None,
    )


def test_checked_in_mcp_presets_are_standalone_and_secret_free() -> None:
    loaded = load_mcp_config(
        Path(".mcp.json.example"),
        environment={
            "MCD_MCP_TOKEN": "offline-token",
            "MINIFLUX_MCP_TOKEN": "miniflux-offline-token",
        },
    )
    assert set(loaded.servers) == {"mcd", "miniflux", "netease_music"}
    server = loaded.servers["mcd"]
    assert server.url == "https://mcp.mcd.cn"
    assert server.headers["Authorization"] == "Bearer offline-token"
    display = json.dumps(redacted_server_config(server), ensure_ascii=False)
    assert "offline-token" not in display
    assert "query-meals" in display
    assert "calculate-price" in server.yuki.automation.include_tools
    assert "create-order" in server.yuki.automation.include_tools
    assert server.yuki.automation.permission == "superuser"
    assert "mall-order-detail" in display
    planning_bundle = server.yuki.tool_bundles["order_planning"]
    assert planning_bundle.scope == "mcp.mcd.order_planning"
    assert set(planning_bundle.include_tools) == {
        "delivery-query-addresses",
        "delivery-query-stores",
        "query-nearby-stores",
        "query-my-coupons",
        "query-meals",
        "query-meal-detail",
        "calculate-price",
    }
    assert "create-order" not in planning_bundle.include_tools
    order_bundle = server.yuki.tool_bundles["order"]
    assert order_bundle.scope == "mcp.mcd.order"
    assert set(order_bundle.include_tools) == {
        "delivery-query-addresses",
        "delivery-query-stores",
        "query-nearby-stores",
        "query-my-coupons",
        "query-meals",
        "query-meal-detail",
        "calculate-price",
        "create-order",
        "query-order",
    }
    assert {
        "delivery-query-addresses",
        "delivery-query-stores",
        "query-nearby-stores",
    }.issubset(server.yuki.automation.include_tools)

    music = loaded.servers["netease_music"]
    assert music.disabled is True
    assert music.url == "http://host.docker.internal:8766/mcp"
    assert music.yuki.scope == "mcp.netease_music"
    assert set(music.include_tools) == {
        "music_search",
        "get_recommendations",
        "get_similar_songs",
        "get_new_songs",
        "get_rankings",
        "get_songs",
        "get_album",
        "get_artist",
        "get_playlist",
        "get_lyrics",
        "get_user_library",
        "get_playlist_statistics",
    }
    assert {
        "create_playlist",
        "update_playlist_tracks",
        "set_song_like",
    }.isdisjoint(music.include_tools)
    music_display = json.dumps(redacted_server_config(music), ensure_ascii=False)
    assert "Authorization" not in music_display

    miniflux = loaded.servers["miniflux"]
    assert miniflux.disabled is True
    assert miniflux.url == "http://miniflux-mcp:8080/mcp"
    assert miniflux.headers["Authorization"] == "Bearer miniflux-offline-token"
    assert miniflux.yuki.scope == "mcp.miniflux"
    assert len(miniflux.include_tools) == 33
    assert {
        "create_user",
        "delete_user",
        "create_api_key",
        "delete_api_key",
        "export",
        "flush_history",
    }.isdisjoint(miniflux.include_tools)
    assert miniflux.yuki.automation.permission == "user"
    assert "get_entries" in miniflux.yuki.automation.include_tools
    assert "delete_feed" not in miniflux.yuki.automation.include_tools
    assert set(miniflux.yuki.tool_bundles) == {"subscriptions", "articles", "categories"}
    miniflux_display = json.dumps(redacted_server_config(miniflux), ensure_ascii=False)
    assert "miniflux-offline-token" not in miniflux_display


@pytest.mark.asyncio
async def test_mcd_preset_overrides_query_semantics_without_hardcoding_provider(
    database: Database,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcd": {
                        "url": "https://mcp.mcd.cn",
                        "headers": {"Authorization": "Bearer offline-token"},
                        "yuki": {
                            "scope": "mcp.mcd",
                            "tags": ["麦当劳", "点餐"],
                            "toolAnnotations": {
                                "query-meals": {
                                    "readOnlyHint": True,
                                    "idempotentHint": True,
                                },
                                "create-order": {
                                    "destructiveHint": True,
                                    "openWorldHint": True,
                                },
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
        tools=(
            _remote_tool("query-meals", "查询当前门店菜单"),
            _remote_tool("create-order", "创建麦当劳订单"),
        )
    )
    manager = MCPManager(
        enabled=True,
        config_path=config_path,
        cache_enabled=True,
        metadata_cache_ttl_seconds=3600,
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        max_parallel_calls=4,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    await manager.start()
    await manager.ensure_metadata("mcd")
    provider = MCPToolProvider(manager, gateway_enabled=False)
    descriptors = {
        item.model_name: item for item in provider.descriptors(SimpleNamespace(runtime_config=None))
    }

    query = descriptors["mcp__mcd__query-meals"]
    create = descriptors["mcp__mcd__create-order"]
    assert query.risk is CapabilityRisk.READ
    assert query.idempotency is CapabilityIdempotency.IDEMPOTENT
    assert query.parallel_safe
    assert create.risk is CapabilityRisk.DESTRUCTIVE
    assert create.provider_metadata == {
        "mcp_annotations": {
            "destructiveHint": True,
            "openWorldHint": True,
        }
    }
    assert create.idempotency is CapabilityIdempotency.CONDITIONAL
    assert not create.parallel_safe
    await manager.close()


@pytest.mark.asyncio
async def test_mcd_streamable_http_auth_and_protocol_negotiation_are_supported() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer offline-token"
        if request.method == "DELETE":
            return httpx.Response(200)
        payload = json.loads(request.content)
        requests.append(payload.get("method", ""))
        if "id" not in payload:
            return httpx.Response(202)
        method = payload["method"]
        if method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "McDonaldsOffline", "version": "1.0.6"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "query-meals",
                        "description": "查询当前门店菜单",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        else:
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "mcp-session-id": "mcd-offline-session",
            },
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    connection = SDKMCPConnection(
        MCPServerConfig(
            url="https://mcp.mcd.cn",
            headers={"Authorization": "Bearer offline-token"},
        ),
        connect_timeout_seconds=5,
        request_timeout_seconds=5,
        http_transport=httpx.MockTransport(handler),
    )
    try:
        await connection.connect()
        tools = await connection.list_tools()
        assert connection.server_info["protocol_version"] == "2025-06-18"
        assert [item.name for item in tools] == ["query-meals"]
        assert requests == ["initialize", "notifications/initialized", "tools/list"]
    finally:
        await connection.close()


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "mcp_authentication_failed", False),
        (429, "mcp_rate_limited", True),
        (503, "mcp_server_unavailable", True),
    ],
)
def test_mcd_http_failures_have_actionable_secret_free_diagnostics(
    status: int,
    code: str,
    retryable: bool,
) -> None:
    request = httpx.Request("POST", "https://mcp.mcd.cn")
    response = httpx.Response(status, request=request)
    failure = httpx.HTTPStatusError("remote rejected request", request=request, response=response)
    details = classify_mcp_exception(ExceptionGroup("SDK transport", [failure]))
    assert details.code == code
    assert details.retryable is retryable
    assert details.disconnect is False
    assert "offline-token" not in details.public_message
