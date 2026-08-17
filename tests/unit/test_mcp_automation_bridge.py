"""MCP tools delegated to persistent automations through one generic bridge."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from tests.conftest import make_settings

from qq_ai_bot.automation.authority import (
    DelegatedAuthority,
    PermissionLevel,
    effective_delegated_capabilities,
)
from qq_ai_bot.automation.models import RetryPolicy, RiskClass, TurnOrigin
from qq_ai_bot.automation.registry import AutomationCapabilityRegistry
from qq_ai_bot.capabilities.results import ToolResultBudgeter
from qq_ai_bot.mcp.automation import MCPAutomationBridge
from qq_ai_bot.mcp.fake import FakeMCPConnection
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.models import MCPServerMetadata
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.persistence.database import Database


def _tool(
    name: str,
    schema: dict[str, object],
    *,
    read_only: bool,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=f"remote {name}",
        inputSchema=schema,
        outputSchema={"type": "object"},
        annotations=SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "readOnlyHint": read_only,
                "idempotentHint": read_only,
            }
        ),
    )


def _result(data: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(content=(), structuredContent=data, isError=False)


def _write_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcd": {
                        "url": "https://mcp.example.test",
                        "lifecycle": "lazy",
                        "yuki": {
                            "automation": {
                                "enabled": True,
                                "permission": "superuser",
                                "includeTools": ["campaign-calendar", "auto-bind-coupons"],
                            },
                            "toolAnnotations": {
                                "campaign-calendar": {"readOnlyHint": True},
                            },
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_automation_opt_in_requires_an_explicit_tool_list() -> None:
    with pytest.raises(ValidationError):
        MCPServerMetadata.model_validate({"automation": {"enabled": True}})


@pytest.mark.asyncio
async def test_bridge_registers_only_selected_mcp_tools_and_executes_through_manager(
    database: Database,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp.json"
    _write_config(config_path)
    query_schema: dict[str, object] = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    }
    connection = FakeMCPConnection(
        tools=(
            _tool("campaign-calendar", query_schema, read_only=True),
            _tool("auto-bind-coupons", {"type": "object"}, read_only=False),
            _tool("create-order", {"type": "object"}, read_only=False),
        ),
        results={"campaign-calendar": _result({"campaigns": ["周一会员日"]})},
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
    registry = AutomationCapabilityRegistry()
    bridge = MCPAutomationBridge(
        manager=manager,
        registry=registry,
        result_budgeter=ToolResultBudgeter(max_characters=8000),
    )
    await manager.start()
    await bridge.start()
    try:
        assert {item.name for item in registry.list()} == {
            "mcp.mcd.auto-bind-coupons",
            "mcp.mcd.campaign-calendar",
        }
        assert registry.names_for(PermissionLevel.USER) == ()
        query = registry.require("mcp.mcd.campaign-calendar")
        mutate = registry.require("mcp.mcd.auto-bind-coupons")
        assert query.risk_class is RiskClass.READ
        assert query.retry_policy is RetryPolicy.TRANSIENT_ONCE
        assert mutate.risk_class is RiskClass.MUTATE
        assert mutate.retry_policy is RetryPolicy.NONE
        assert query.input_schema == query_schema
        assert query.validate_arguments({"city": "上海"}) == {"city": "上海"}
        assert query.validate_arguments({"city": "${previous.city}"}, allow_templates=True) == {
            "city": "${previous.city}"
        }
        with pytest.raises(ValueError, match="JSON Schema"):
            query.validate_arguments(
                {"city": "${previous.city}", "unexpected": True},
                allow_templates=True,
            )
        with pytest.raises(ValueError, match="JSON Schema"):
            query.validate_arguments({"city": 1})
        assert query.handler is not None
        outcome = await query.handler(
            {"city": "上海"},
            cast(
                Any,
                SimpleNamespace(
                    conversation_key="automation:9",
                    authority=SimpleNamespace(
                        origin=TurnOrigin.SCHEDULED_AUTOMATION,
                        actor_user_id="10001",
                        actor_is_superuser=True,
                    ),
                    web_was_used=False,
                ),
            ),
        )
        assert outcome.data["data"] == {"campaigns": ["周一会员日"]}
        assert connection.calls == [("campaign-calendar", {"city": "上海"})]
        assert (await bridge.health())["registered_tools"] == 2
    finally:
        await bridge.close()
        await manager.close()


@pytest.mark.asyncio
async def test_remote_schema_change_revokes_an_old_delegation_snapshot(
    database: Database,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp.json"
    _write_config(config_path)
    connection = FakeMCPConnection(
        tools=(
            _tool(
                "campaign-calendar",
                {"type": "object", "properties": {}},
                read_only=True,
            ),
        )
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
    registry = AutomationCapabilityRegistry()
    bridge = MCPAutomationBridge(
        manager=manager,
        registry=registry,
        result_budgeter=ToolResultBudgeter(max_characters=8000),
    )
    await manager.start()
    await bridge.start()
    try:
        name = "mcp.mcd.campaign-calendar"
        old_version = registry.require(name).schema_version
        authority = DelegatedAuthority(
            creator_user_id="9000",
            bot_user_id="10000",
            created_from_message_id="event-1",
            created_at="2026-07-30T10:00:00+00:00",
            permission_level=PermissionLevel.SUPERUSER,
            granted_capabilities=(name,),
            capability_schema_versions={name: old_version},
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
        )
        assert name in effective_delegated_capabilities(
            authority,
            settings=make_settings("sqlite+aiosqlite:///:memory:"),
            registry=registry,
        )
        connection.tools = (
            _tool(
                "campaign-calendar",
                {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                    "required": ["limit"],
                },
                read_only=True,
            ),
        )
        await manager.refresh("mcd", force=True)
        assert registry.require(name).schema_version != old_version
        assert name not in effective_delegated_capabilities(
            authority,
            settings=make_settings("sqlite+aiosqlite:///:memory:"),
            registry=registry,
        )
    finally:
        await bridge.close()
        await manager.close()
