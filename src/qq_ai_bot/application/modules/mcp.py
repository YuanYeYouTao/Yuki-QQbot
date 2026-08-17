"""Application composition for the generic MCP client and Tool Kernel artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.config import Settings
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.provider import MCPToolProvider
from qq_ai_bot.mcp.repository import MCPRepository, ToolArtifactRepository
from qq_ai_bot.persistence.database import Database


@dataclass(frozen=True, slots=True)
class MCPBundle:
    repository: MCPRepository
    artifacts: ToolArtifactRepository
    manager: MCPManager
    provider: MCPToolProvider


class MCPModule:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        lifecycle: LifecycleRegistry,
    ) -> None:
        self._settings = settings
        self._database = database
        self._lifecycle = lifecycle

    def build(self) -> MCPBundle:
        settings = self._settings
        repository = MCPRepository(
            self._database,
            reflection_excerpt_characters=settings.memory_self_reflection_tool_receipt_characters,
            reflection_retention_days=settings.memory_self_reflection_tool_receipt_retention_days,
        )
        artifacts = ToolArtifactRepository(
            self._database,
            Path("data/tool_artifacts"),
            retention_seconds=settings.tooling_result_artifact_retention_seconds,
        )
        manager = MCPManager(
            enabled=settings.mcp_enabled,
            config_path=settings.mcp_config_path,
            cache_enabled=settings.mcp_cache_enabled,
            metadata_cache_ttl_seconds=settings.mcp_metadata_cache_ttl_seconds,
            connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
            request_timeout_seconds=settings.mcp_request_timeout_seconds,
            max_parallel_calls=settings.mcp_max_parallel_calls,
            repository=repository,
        )
        provider = MCPToolProvider(
            manager,
            gateway_enabled=settings.mcp_gateway_enabled,
        )
        self._lifecycle.register("mcp", start=manager.start, close=manager.close)
        return MCPBundle(repository, artifacts, manager, provider)
