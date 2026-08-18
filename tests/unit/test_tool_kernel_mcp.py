"""Offline contracts for the 2.1 Tool Kernel and generic MCP client."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityExposure,
    CapabilityRisk,
    CapabilityTrustSource,
    ChatToolCapabilityProvider,
    InProcessToolProvider,
    ToolExecutionResult,
    ToolInvocationCoordinator,
    ToolProviderRegistry,
    ToolResultBudgeter,
    estimate_chat_tool_tokens,
    resolve_mutation_commit,
)
from qq_ai_bot.capabilities.invocation import ToolInvocationContext
from qq_ai_bot.capabilities.results import normalize_legacy_result
from qq_ai_bot.domain.messages import ChatTool, ToolCall, ToolFunction
from qq_ai_bot.mcp.binding import MCPPolicyRuntime, MCPToolBinding
from qq_ai_bot.mcp.config import MCPConfigurationError, load_mcp_config, redacted_server_config
from qq_ai_bot.mcp.connection import SDKMCPConnection
from qq_ai_bot.mcp.fake import FakeMCPConnection
from qq_ai_bot.mcp.gateway import MCPGatewayBinding
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.models import MCPServerConfig, MCPTransport
from qq_ai_bot.mcp.provider import MCPToolProvider
from qq_ai_bot.mcp.repository import MCPRepository, ToolArtifactRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.services.chat import _fit_artifact_page_result


def _tool(name: str, description: str = "") -> ChatTool:
    return ChatTool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        },
    )


async def _call_mcp(
    manager: MCPManager,
    server_id: str,
    tool_name: str,
    arguments: dict[str, object],
) -> ToolExecutionResult:
    runtime = MCPPolicyRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="test-user",
        actor_is_superuser=False,
    )
    return await MCPToolBinding(manager, server_id, tool_name).invoke(
        arguments,
        ToolInvocationContext(
            runtime=runtime,
            conversation_key="test:mcp",
            actor_user_id=runtime.actor_user_id,
        ),
    )


def test_schema_token_estimate_includes_function_envelope() -> None:
    short = _tool("x")
    described = _tool("x", "long description " * 20)
    assert estimate_chat_tool_tokens(described) > estimate_chat_tool_tokens(short)
    assert estimate_chat_tool_tokens(short) > len(json.dumps(short.parameters)) // 4


def test_conditional_mutation_result_preserves_explicit_commit_state() -> None:
    lookup_only = normalize_legacy_result(
        {"ok": True, "data": {"status": "selection_required"}, "mutation_committed": False},
        provider_id="plugin",
        tool_name="conditional_send",
    )
    legacy_success = normalize_legacy_result(
        {"ok": True, "data": {"status": "sent"}},
        provider_id="plugin",
        tool_name="legacy_send",
    )

    assert lookup_only.mutation_committed is False
    assert legacy_success.mutation_committed is None


def test_mutation_commit_resolution_uses_explicit_result_then_descriptor_effect() -> None:
    descriptors = ChatToolCapabilityProvider(
        (
            _tool("get_person_memories"),
            _tool("get_my_capabilities"),
            _tool("read_tool_artifact"),
        ),
        source=CapabilityTrustSource.CORE,
    ).descriptors()
    read, capability, artifact_reader = descriptors
    assert capability.namespace_id == "kernel.authority.read"
    assert capability.exposure is CapabilityExposure.PLANNED
    assert artifact_reader.namespace_id == "kernel.artifact.read"
    assert artifact_reader.exposure is CapabilityExposure.PLANNED
    write = replace(
        read,
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
    )

    assert not resolve_mutation_commit(ToolExecutionResult(ok=True), read)
    assert resolve_mutation_commit(ToolExecutionResult(ok=True), write)
    assert not resolve_mutation_commit(
        ToolExecutionResult(ok=True, mutation_committed=False),
        write,
    )
    assert resolve_mutation_commit(
        ToolExecutionResult(ok=True, mutation_committed=True),
        read,
    )
    assert not resolve_mutation_commit(
        ToolExecutionResult(ok=False, mutation_committed=True),
        write,
    )


@dataclass(slots=True)
class _StaticProvider:
    provider_id: str
    items: tuple[CapabilityDescriptor, ...]

    def descriptors(self, _context: object) -> tuple[CapabilityDescriptor, ...]:
        return self.items

    async def refresh(self, *, force: bool = False) -> None:
        del force

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_catalog_selection_schema_budget_and_binding_are_provider_neutral() -> None:
    calls: list[tuple[str, str]] = []

    async def execute(name: str, arguments: str, _context: object) -> object:
        calls.append((name, arguments))
        return {"ok": True, "data": name}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _context: (
                _tool("search_chat_history", "搜索聊天历史"),
                _tool("get_person_memories", "读取人物记忆"),
            ),
            execute=execute,
        )
    )
    catalog = registry.catalog(object())
    history = catalog.by_model_name("search_chat_history")
    assert history is not None
    binding = history.descriptor.binding
    assert binding is not None
    outcome = await binding.invoke(
        {"query": "昨天"},
        SimpleNamespace(runtime=object()),
    )
    assert outcome.ok
    assert calls

    original = catalog.entries[0].descriptor
    collision_registry = ToolProviderRegistry()
    collision_registry.register(_StaticProvider("first", (original,)))
    collision_registry.register(
        _StaticProvider(
            "second",
            (
                replace(
                    original,
                    model_name="different_model_name",
                    provider_id="second",
                ),
            ),
        )
    )
    with pytest.raises(ValueError, match="duplicate canonical capability"):
        collision_registry.catalog(object())


@dataclass(slots=True)
class _BatchBackend:
    active: int = 0
    maximum_active: int = 0
    completed: list[str] = field(default_factory=list)

    def parallel_safe(self, name: str, runtime: object) -> bool:
        del runtime
        return name.startswith("read")

    async def execute(self, name: str, arguments: str, runtime: object) -> str:
        del arguments, runtime
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0.01 if name.startswith("read_slow") else 0)
        self.completed.append(name)
        self.active -= 1
        return json.dumps({"ok": True, "name": name})


@dataclass(slots=True)
class _ControlAwareBatchBackend(_BatchBackend):
    def counts_toward_limit(self, name: str, runtime: object) -> bool:
        del runtime
        return name != "set_reply_target"


@pytest.mark.asyncio
async def test_coordinator_parallelizes_safe_stretches_but_returns_original_order() -> None:
    backend = _BatchBackend()
    calls = tuple(
        ToolCall(id=name, function=ToolFunction(name=name, arguments="{}"))
        for name in ("read_slow", "read_fast", "write", "read_after")
    )
    result = await ToolInvocationCoordinator().execute_batch(
        calls,
        backend,
        object(),
        remaining_calls=20,
        max_parallel_calls=8,
    )
    assert backend.maximum_active == 2
    assert [call.id for call, _payload, _ran in result.calls] == [call.id for call in calls]
    assert result.executed_count == 4

    many = tuple(
        ToolCall(
            id=f"read-{index}",
            function=ToolFunction(name=f"read_slow_{index}", arguments="{}"),
        )
        for index in range(64)
    )
    large_result = await ToolInvocationCoordinator().execute_batch(
        many,
        backend,
        object(),
        remaining_calls=1000,
        max_parallel_calls=64,
    )
    assert large_result.executed_count == 64
    assert [call.id for call, _payload, _ran in large_result.calls] == [call.id for call in many]


@pytest.mark.asyncio
async def test_coordinator_keeps_response_controls_outside_business_call_budget() -> None:
    backend = _ControlAwareBatchBackend()
    calls = tuple(
        ToolCall(id=name, function=ToolFunction(name=name, arguments="{}"))
        for name in ("write", "set_reply_target")
    )

    result = await ToolInvocationCoordinator().execute_batch(
        calls,
        backend,
        object(),
        remaining_calls=0,
        max_parallel_calls=1,
    )

    assert result.executed_count == 0
    assert backend.completed == ["set_reply_target"]
    assert [ran for _call, _payload, ran in result.calls] == [False, True]
    assert "tool_limit_exceeded" in result.calls[0][1]


def test_coordinator_missing_call_id_returns_error_payload() -> None:
    from qq_ai_bot.capabilities.coordinator import TOOL_RESULT_MISSING, _attach_batch_results

    calls = (
        ToolCall(id="call_01_kept", function=ToolFunction(name="a", arguments="{}")),
        ToolCall(id="call_01_missing", function=ToolFunction(name="b", arguments="{}")),
    )
    ordered = _attach_batch_results(
        calls,
        results={"call_01_kept": '{"ok":true}'},
        overflow_ids=set(),
        limited='{"ok":false,"error":"tool_limit_exceeded"}',
    )
    assert ordered[0][2] is True
    assert json.loads(ordered[1][1])["error"] == TOOL_RESULT_MISSING
    assert ordered[1][2] is False


@pytest.mark.asyncio
async def test_result_budget_keeps_valid_summary_and_pages_full_artifact(
    database: Database,
    tmp_path: Path,
) -> None:
    artifacts = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=60)
    result = ToolExecutionResult(
        ok=True,
        data=[{"value": index} for index in range(20)],
        provider_id="fake",
        tool_name="large",
    )
    rendered = await ToolResultBudgeter(
        max_characters=400,
        item_limit=3,
        artifacts=artifacts,
    ).render(result)
    payload = json.loads(rendered.text)
    assert payload["truncated"] is True
    assert payload["artifact_handle"] == rendered.artifact_id
    page = await artifacts.read(rendered.artifact_id or "", offset=0, limit=80)
    assert page is not None
    assert page["next_offset"] == 80
    assert "content" in page


@pytest.mark.asyncio
async def test_artifact_reader_result_never_creates_nested_artifact(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    artifacts = ToolArtifactRepository(database, root, retention_seconds=60)
    original_handle = await artifacts.write_artifact(
        provider_id="fake",
        tool_name="large",
        content="source",
        media_type="text/plain",
    )
    result = ToolExecutionResult(
        ok=True,
        data={
            "handle": original_handle,
            "offset": 0,
            "next_offset": 2000,
            "total_characters": 2000,
            "content": "x" * 2000,
            "query_matched": None,
        },
        provider_id="artifacts",
        tool_name="read_tool_artifact",
    )

    rendered = await ToolResultBudgeter(
        max_characters=400,
        artifacts=artifacts,
    ).render(result)

    assert rendered.truncated is True
    assert rendered.artifact_id is None
    assert len(tuple(root.glob("*.json"))) == 1


def test_artifact_pages_fit_budget_and_reconstruct_without_gaps() -> None:
    source = ('中文\\"menu"\n' * 600) + "end"
    reconstructed: list[str] = []
    offset = 0
    while offset < len(source):
        raw_end = min(len(source), offset + 32_000)
        page = {
            "handle": "stable-handle",
            "offset": offset,
            "next_offset": raw_end if raw_end < len(source) else None,
            "total_characters": len(source),
            "content": source[offset:raw_end],
            "query_matched": None,
        }
        fitted = _fit_artifact_page_result(page, max_characters=700)
        assert fitted.ok is True
        assert len(json.dumps(fitted.model_payload(), ensure_ascii=False, default=str)) <= 700
        assert isinstance(fitted.data, dict)
        assert fitted.data["handle"] == "stable-handle"
        assert fitted.data["offset"] == offset
        content = fitted.data["content"]
        assert isinstance(content, str) and content
        reconstructed.append(content)
        next_offset = fitted.data["next_offset"]
        offset = len(source) if next_offset is None else int(next_offset)

    assert "".join(reconstructed) == source


@pytest.mark.asyncio
async def test_result_budget_prioritizes_payment_url_and_order_identifier() -> None:
    result = ToolExecutionResult(
        ok=True,
        data={
            "catalog": [{"description": "x" * 2000} for _ in range(20)],
            "order": {
                "orderId": "ORDER-001",
                "status": "pending_payment",
                "payH5Url": "https://example.com/pay",
            },
        },
        provider_id="fake",
        tool_name="create-order",
    )
    rendered = await ToolResultBudgeter(max_characters=600, item_limit=1).render(result)
    assert "https://example.com/pay" in rendered.text
    assert "ORDER-001" in rendered.text
    assert "pending_payment" in rendered.text


def test_mcp_config_supports_both_transports_aliases_and_central_env_expansion(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "music": {
                        "command": "python",
                        "args": ["-m", "fake_music"],
                        "env": {"COOKIE": "${MUSIC_COOKIE}"},
                        "connectTimeoutSeconds": 3,
                        "requestTimeoutSeconds": 9,
                        "yuki": {"scope": "mcp.music", "tags": ["音乐"]},
                    },
                    "mcd": {
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer ${MCD_TOKEN}"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    loaded = load_mcp_config(
        path,
        environment={"MUSIC_COOKIE": "secret-cookie", "MCD_TOKEN": "secret-token"},
    )
    assert loaded.servers["music"].transport is MCPTransport.STDIO
    assert loaded.servers["mcd"].transport is MCPTransport.STREAMABLE_HTTP
    assert loaded.servers["music"].connect_timeout_seconds == 3
    display = json.dumps(redacted_server_config(loaded.servers["mcd"]), ensure_ascii=False)
    assert "secret-token" not in display
    with pytest.raises(MCPConfigurationError):
        load_mcp_config(path, environment={})


class _ConnectionFactory:
    def __init__(self, connections: dict[str, FakeMCPConnection]) -> None:
        self.connections = connections
        self.transports: list[MCPTransport] = []

    def __call__(self, config: Any, **_kwargs: object) -> FakeMCPConnection:
        self.transports.append(config.transport)
        key = "music" if config.command else "mcd"
        return self.connections[key]


@dataclass(slots=True)
class _SlowConnection(FakeMCPConnection):
    call_started: asyncio.Event = field(default_factory=asyncio.Event)
    call_cancelled: bool = False

    async def call_tool(self, name: str, arguments: dict[str, object]) -> Any:
        del name, arguments
        self.call_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.call_cancelled = True
            raise


@dataclass(slots=True)
class _BusinessFailureConnection(FakeMCPConnection):
    async def call_tool(self, name: str, arguments: dict[str, object]) -> Any:
        self.calls.append((name, arguments))
        request = httpx.Request("POST", "https://example.invalid/mcp")
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError(
            "invalid business input",
            request=request,
            response=response,
        )


@pytest.mark.asyncio
async def test_lazy_mcp_discovery_same_name_is_collision_free_and_calls_fake_transports(
    database: Database,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "music": {"command": "python", "lifecycle": "lazy"},
                    "mcd": {"url": "https://example.invalid/mcp", "lifecycle": "lazy"},
                }
            }
        ),
        encoding="utf-8",
    )
    sdk_tool = SimpleNamespace(
        name="search",
        description="search remote data",
        inputSchema={"type": "object", "properties": {}},
        outputSchema=None,
        annotations=SimpleNamespace(model_dump=lambda **_kwargs: {"readOnlyHint": True}),
    )
    sdk_result = SimpleNamespace(content=(), structuredContent={"found": True}, isError=False)
    connections = {
        "music": FakeMCPConnection(tools=(sdk_tool,), results={"search": sdk_result}),
        "mcd": FakeMCPConnection(tools=(sdk_tool,), results={"search": sdk_result}),
    }
    factory = _ConnectionFactory(connections)
    manager = MCPManager(
        enabled=True,
        config_path=config_path,
        cache_enabled=True,
        metadata_cache_ttl_seconds=3600,
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        max_parallel_calls=4,
        repository=MCPRepository(database),
        connection_factory=factory,
    )
    await manager.start()
    assert not any(connection.connected for connection in connections.values())
    provider = MCPToolProvider(manager, gateway_enabled=True)
    bootstrap = provider.descriptors(SimpleNamespace(runtime_config=None))
    synthetic = {item.model_name: item for item in bootstrap}
    assert synthetic["mcp__music__discover"].provider_metadata == {"synthetic": True}
    assert synthetic["mcp__mcd__discover"].provider_metadata == {"synthetic": True}
    assert synthetic["mcp__music__discover"].namespace_id == "mcp.music"
    await provider.prepare_scopes((), SimpleNamespace(runtime_config=None))
    assert not any(connection.connected for connection in connections.values())
    await provider.ensure_server_metadata("music", SimpleNamespace(runtime_config=None))
    await manager.ensure_metadata("mcd")
    descriptors = provider.descriptors(SimpleNamespace(runtime_config=None))
    names = [item.model_name for item in descriptors]
    assert "mcp__music__search" in names
    assert "mcp__mcd__search" in names
    assert "mcp__music__discover" not in names
    assert len(names) == len(set(names))
    search = next(item for item in descriptors if item.model_name == "mcp__music__search")
    assert search.effect is CapabilityEffect.WRITE_STATE
    assert search.risk is CapabilityRisk.MUTATE
    assert not (search.provider_metadata or {}).get("synthetic")
    outcome = await _call_mcp(manager, "music", "search", {"query": "Yuki"})
    assert outcome.ok and outcome.data == {"found": True}
    gateway = MCPGatewayBinding(manager)
    context = ToolInvocationContext(runtime=object(), conversation_key="test:gateway")
    search_result = await gateway.invoke(
        {"operation": "search", "query": "remote"},
        context,
    )
    describe_result = await gateway.invoke(
        {"operation": "describe", "server_id": "music", "tool_name": "search"},
        context,
    )
    call_result = await gateway.invoke(
        {
            "operation": "call",
            "server_id": "music",
            "tool_name": "search",
            "arguments": {"query": "gateway"},
        },
        context,
    )
    assert search_result.ok and len(search_result.data) == 2
    assert describe_result.ok
    assert call_result.ok and call_result.data == {"found": True}
    detail_tool = SimpleNamespace(
        name="detail",
        description="read remote detail",
        inputSchema={"type": "object", "properties": {}},
        outputSchema=None,
        annotations=SimpleNamespace(model_dump=lambda **_kwargs: {"readOnlyHint": True}),
    )
    connections["music"].tools = (sdk_tool, detail_tool)
    await connections["music"].notify_tools_changed()
    for _ in range(20):
        if manager.describe_tool("music", "detail") is not None:
            break
        await asyncio.sleep(0.01)
    assert manager.describe_tool("music", "detail") is not None
    await manager.refresh("music")
    assert MCPTransport.STDIO in factory.transports
    assert MCPTransport.STREAMABLE_HTTP in factory.transports
    await manager.close()

    cached_connection = FakeMCPConnection(fail_connect=True)
    cached_manager = MCPManager(
        enabled=True,
        config_path=config_path,
        cache_enabled=True,
        metadata_cache_ttl_seconds=3600,
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        max_parallel_calls=4,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: cached_connection,
    )
    await cached_manager.start()
    assert len(cached_manager.cached_tools) == 3
    assert not cached_connection.connected

    changed = json.loads(config_path.read_text(encoding="utf-8"))
    changed["mcpServers"]["music"]["yuki"] = {"summary": "changed configuration"}
    config_path.write_text(json.dumps(changed), encoding="utf-8")
    await cached_manager.reload_config()
    assert [item.server_id for item in cached_manager.cached_tools] == ["mcd"]
    await cached_manager.close()


@pytest.mark.asyncio
async def test_gateway_requires_describe_then_call_and_honors_read_only_policy(
    database: Database,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "gateway-policy.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shop": {
                        "command": "fake",
                        "excludeTools": ["hidden"],
                        "yuki": {
                            "scope": "mcp.shop",
                            "toolAnnotations": {
                                "read": {"readOnlyHint": True},
                                "create": {"readOnlyHint": False},
                            },
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    tools = tuple(
        SimpleNamespace(
            name=name,
            description=name,
            inputSchema={"type": "object", "properties": {}},
            outputSchema=None,
            annotations=None,
        )
        for name in ("read", "create", "hidden")
    )
    connection = FakeMCPConnection(
        tools=tools,
        results={
            "read": SimpleNamespace(
                content=(),
                structuredContent={"value": 1},
                isError=False,
            ),
            "create": SimpleNamespace(
                content=(),
                structuredContent={"created": True},
                isError=False,
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
    await manager.ensure_metadata("shop")
    gateway = MCPGatewayBinding(manager)

    def context(
        key: str,
        *,
        read_only: bool = False,
        trigger: str | None = None,
    ) -> ToolInvocationContext:
        runtime = SimpleNamespace(
            actor_user_id="1001",
            actor_is_superuser=False,
            origin=TurnOrigin.USER_MESSAGE,
            read_only=read_only,
            tools_closed=False,
        )
        return ToolInvocationContext(
            runtime=runtime,
            conversation_key=key,
            trigger_message_id=trigger or key,
        )

    unseen = await gateway.invoke(
        {
            "operation": "call",
            "server": "shop",
            "tool": "read",
            "arguments": {},
        },
        context("unseen"),
    )
    assert unseen.error_code == "mcp_tool_not_selected"

    excluded = await gateway.invoke(
        {"operation": "describe", "server": "shop", "tool": "hidden"},
        context("excluded"),
    )
    assert excluded.error_code == "unknown_mcp_tool"

    namespace_is_not_permission = await gateway.invoke(
        {"operation": "describe", "server": "shop", "tool": "read"},
        context("other-namespace"),
    )
    assert namespace_is_not_permission.ok

    described = await gateway.invoke(
        {"operation": "describe", "server": "shop", "tool": "create"},
        context("readonly"),
    )
    assert described.ok
    denied = await gateway.invoke(
        {
            "operation": "call",
            "server": "shop",
            "tool": "create",
            "arguments": {},
        },
        context("readonly", read_only=True),
    )
    assert denied.error_code == "mcp_tool_policy_denied"
    next_turn = await gateway.invoke(
        {
            "operation": "call",
            "server": "shop",
            "tool": "create",
            "arguments": {},
        },
        context("readonly", trigger="readonly-next-turn"),
    )
    assert next_turn.error_code == "mcp_tool_not_selected"
    assert connection.calls == []
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_disabled_keeps_catalog_empty_without_connecting(
    database: Database,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"local": {"command": "python"}}}),
        encoding="utf-8",
    )
    connection = FakeMCPConnection()
    manager = MCPManager(
        enabled=False,
        config_path=path,
        cache_enabled=True,
        metadata_cache_ttl_seconds=60,
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        max_parallel_calls=1,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    await manager.start()
    provider = MCPToolProvider(manager, gateway_enabled=True)
    assert provider.descriptors(SimpleNamespace(runtime_config=None)) == ()
    assert not connection.connected


@pytest.mark.asyncio
async def test_official_sdk_stdio_transport_initializes_lists_calls_and_closes() -> None:
    server = Path(__file__).parents[1] / "fixtures" / "fake_mcp_server.py"
    connection = SDKMCPConnection(
        MCPServerConfig(
            command=sys.executable,
            args=(str(server),),
            requestTimeoutSeconds=10,
            connectTimeoutSeconds=10,
        ),
        connect_timeout_seconds=10,
        request_timeout_seconds=10,
    )

    try:
        await asyncio.create_task(connection.connect())
        tools = await connection.list_tools()
        result = await connection.call_tool("echo", {"text": "Yuki"})
        assert connection.connected
        assert [tool.name for tool in tools] == ["echo"]
        assert result.isError is False
        assert result.structuredContent == {"echo": "Yuki", "transport": "stdio"}
    finally:
        await asyncio.create_task(connection.close())
    assert not connection.connected


@pytest.mark.asyncio
async def test_official_sdk_streamable_http_uses_mock_transport_without_network() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200)
        payload = json.loads(request.content)
        method = payload.get("method", "")
        requests.append(method)
        if "id" not in payload:
            return httpx.Response(202)
        if method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": payload["params"]["protocolVersion"],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "FakeHTTP", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "coupon",
                        "description": "return a fake coupon",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": "offline coupon"}],
                "structuredContent": {"coupon": "offline"},
                "isError": False,
            }
        else:
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "mcp-session-id": "offline-session",
            },
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    connection = SDKMCPConnection(
        MCPServerConfig(url="https://offline.invalid/mcp"),
        connect_timeout_seconds=5,
        request_timeout_seconds=5,
        http_transport=httpx.MockTransport(handler),
    )
    try:
        await connection.connect()
        tools = await connection.list_tools()
        result = await connection.call_tool("coupon", {})
        assert [tool.name for tool in tools] == ["coupon"]
        assert result.structuredContent == {"coupon": "offline"}
        assert requests == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_mcp_connection_failure_is_real_and_next_call_can_recover(
    database: Database,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"recoverable": {"command": "python"}}}),
        encoding="utf-8",
    )
    sdk_result = SimpleNamespace(content=(), structuredContent={"ready": True}, isError=False)
    connection = FakeMCPConnection(
        tools=(
            SimpleNamespace(
                name="health",
                description="health",
                inputSchema={"type": "object", "properties": {}},
                outputSchema=None,
                annotations=None,
            ),
        ),
        results={"health": sdk_result},
        fail_connect=True,
    )
    manager = MCPManager(
        enabled=True,
        config_path=path,
        cache_enabled=True,
        metadata_cache_ttl_seconds=60,
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        max_parallel_calls=2,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    await manager.start()

    failed = await _call_mcp(manager, "recoverable", "health", {})
    assert not failed.ok and failed.error_code == "mcp_transport_unavailable"
    connection.fail_connect = False
    recovered = await _call_mcp(manager, "recoverable", "health", {})
    assert recovered.ok and recovered.data == {"ready": True}
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_tool_and_business_errors_do_not_disconnect(
    database: Database,
    tmp_path: Path,
) -> None:
    path = tmp_path / "business-errors.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "business": {
                        "command": "python",
                        "lifecycle": "keep_alive",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    tool = SimpleNamespace(
        name="order",
        description="order",
        inputSchema={"type": "object", "properties": {}},
        outputSchema=None,
        annotations=None,
    )
    tool_error = FakeMCPConnection(
        tools=(tool,),
        results={
            "order": SimpleNamespace(
                content=(),
                structuredContent={"reason": "not found"},
                isError=True,
            )
        },
    )
    manager = MCPManager(
        enabled=True,
        config_path=path,
        cache_enabled=False,
        metadata_cache_ttl_seconds=60,
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        max_parallel_calls=1,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: tool_error,
    )
    await manager.start()
    await manager.ensure_metadata("business")
    result = await _call_mcp(manager, "business", "order", {})
    assert not result.ok and result.mutation_committed is False
    assert tool_error.connected
    await manager.close()

    business_error = _BusinessFailureConnection(tools=(tool,))
    manager = MCPManager(
        enabled=True,
        config_path=path,
        cache_enabled=False,
        metadata_cache_ttl_seconds=60,
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        max_parallel_calls=1,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: business_error,
    )
    await manager.start()
    await manager.ensure_metadata("business")
    result = await _call_mcp(manager, "business", "order", {})
    assert result.error_code == "mcp_http_400"
    assert business_error.connected
    await manager.close()


@pytest.mark.asyncio
async def test_keep_alive_reconnects_until_server_recovers(
    database: Database,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "persistent": {
                        "command": "python",
                        "lifecycle": "keep_alive",
                        "reconnectDelaySeconds": 0.01,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    connection = FakeMCPConnection(fail_connect=True)
    manager = MCPManager(
        enabled=True,
        config_path=path,
        cache_enabled=False,
        metadata_cache_ttl_seconds=60,
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        max_parallel_calls=1,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    await manager.start()
    assert not connection.connected
    connection.fail_connect = False
    for _ in range(50):
        if connection.connected:
            break
        await asyncio.sleep(0.01)
    assert connection.connected
    assert (await manager.status("persistent")).status == "connected"
    await manager.close()


@pytest.mark.asyncio
async def test_mcp_call_cancellation_propagates_to_transport(
    database: Database,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"slow": {"command": "python"}}}),
        encoding="utf-8",
    )
    connection = _SlowConnection(
        tools=(
            SimpleNamespace(
                name="wait",
                description="wait",
                inputSchema={"type": "object", "properties": {}},
                outputSchema=None,
                annotations=None,
            ),
        )
    )
    manager = MCPManager(
        enabled=True,
        config_path=path,
        cache_enabled=False,
        metadata_cache_ttl_seconds=60,
        connect_timeout_seconds=1,
        request_timeout_seconds=60,
        max_parallel_calls=1,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    await manager.start()
    task = asyncio.create_task(_call_mcp(manager, "slow", "wait", {}))
    await connection.call_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.call_cancelled
    assert connection.connected
    assert manager.health().last_error_category is None
    assert manager.health().last_call_at is None
    await manager.close()
