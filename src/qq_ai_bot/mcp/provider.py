"""Expose cached MCP metadata as Tool Kernel descriptors."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.catalog import ToolScopeSummary, safe_model_tool_name
from qq_ai_bot.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.mcp.descriptors import (
    descriptor_from_mcp_tool,
    host_annotation_policy,
    mcp_capability_namespace,
)
from qq_ai_bot.mcp.gateway import MCPGatewayBinding
from qq_ai_bot.mcp.manager import MCPManager

_SYNTHETIC_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


class MCPToolProvider:
    provider_id = "mcp"

    def __init__(
        self,
        manager: MCPManager,
        *,
        gateway_enabled: bool,
    ) -> None:
        self._manager = manager
        self._gateway_enabled = gateway_enabled
        self._gateway_binding = MCPGatewayBinding(manager)

    def descriptors(self, context: Any) -> tuple[CapabilityDescriptor, ...]:
        if not self._configure_runtime(context):
            return ()
        runtime = getattr(context, "runtime_config", None)
        mcp = getattr(runtime, "mcp", None)
        gateway_enabled = mcp.gateway_enabled if mcp is not None else self._gateway_enabled
        cached = self._manager.cached_tools
        cached_servers = {item.server_id for item in cached}
        descriptors: list[CapabilityDescriptor] = []
        descriptors.extend(self._descriptor(item) for item in cached)
        for server_id in self._manager.configured_server_ids:
            if not self._manager.server_enabled(server_id):
                continue
            if server_id in cached_servers:
                continue
            descriptors.append(self._synthetic_descriptor(server_id))
        if gateway_enabled and self._manager.configured_server_ids:
            descriptors.append(self._gateway_descriptor())
        return tuple(descriptors)

    async def prepare_scopes(self, scopes: tuple[str, ...], context: Any) -> None:
        """Discover selected servers. Empty scopes discover nothing."""

        if not scopes or not self._configure_runtime(context):
            return
        for server_id in self._manager.configured_server_ids:
            config = self._manager.server_config(server_id)
            assert config is not None
            scope = mcp_capability_namespace(server_id, config)
            bundle_scopes = {bundle.scope for bundle in config.yuki.tool_bundles.values()}
            if (
                scope not in scopes
                and "mcp" not in scopes
                and not bundle_scopes.intersection(scopes)
            ):
                continue
            try:
                await self._manager.ensure_metadata(server_id)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                continue

    async def ensure_server_metadata(self, server_id: str, context: Any) -> None:
        """Connect one configured server after FTS hits its synthetic document."""

        if not self._configure_runtime(context):
            return
        try:
            await self._manager.ensure_metadata(server_id)
        except (OSError, RuntimeError, TimeoutError, ValueError):
            return

    def scope_summaries(self, runtime: Any | None = None) -> tuple[ToolScopeSummary, ...]:
        """Expose compact config metadata without connecting lazy servers."""

        mcp = getattr(runtime, "mcp", None)
        enabled = mcp.enabled if mcp is not None else self._manager.enabled
        if not enabled:
            return ()
        gateway_enabled = mcp.gateway_enabled if mcp is not None else self._gateway_enabled
        summaries: list[ToolScopeSummary] = []
        for server_id in self._manager.configured_server_ids:
            config = self._manager.server_config(server_id)
            assert config is not None
            scope = mcp_capability_namespace(server_id, config)
            summaries.append(
                ToolScopeSummary(
                    scope_id=scope,
                    parent=scope.rpartition(".")[0] or None,
                    display_name=server_id,
                    description=config.yuki.summary or f"MCP Server {server_id}",
                    tool_count=sum(
                        item.server_id == server_id for item in self._manager.cached_tools
                    ),
                    provider_ids=(f"mcp.{server_id}",),
                    tags=config.yuki.tags,
                )
            )
            summaries.extend(
                ToolScopeSummary(
                    scope_id=bundle.scope,
                    parent=bundle.scope.rpartition(".")[0] or None,
                    display_name=name,
                    description=bundle.summary,
                    tool_count=len(bundle.include_tools),
                    provider_ids=(f"mcp.{server_id}",),
                    tags=(*config.yuki.tags, "tool-bundle"),
                )
                for name, bundle in config.yuki.tool_bundles.items()
            )
        if gateway_enabled and summaries:
            summaries.append(
                ToolScopeSummary(
                    scope_id="mcp",
                    parent=None,
                    display_name="MCP",
                    description="MCP 工具目录与按需调用网关",
                    tool_count=1,
                    provider_ids=("mcp.gateway",),
                    tags=("mcp",),
                )
            )
        return tuple(summaries)

    def _configure_runtime(self, context: Any) -> bool:
        runtime = getattr(context, "runtime_config", None)
        mcp = getattr(runtime, "mcp", None)
        enabled = mcp.enabled if mcp is not None else self._manager.enabled
        self._manager.configure_runtime(
            enabled=enabled,
            metadata_cache_ttl_seconds=(mcp.metadata_cache_ttl_seconds if mcp else None),
            connect_timeout_seconds=(mcp.connect_timeout_seconds if mcp else None),
            request_timeout_seconds=(mcp.request_timeout_seconds if mcp else None),
            max_parallel_calls=(mcp.max_parallel_calls if mcp else None),
        )
        return bool(enabled)

    def _descriptor(self, item: Any) -> CapabilityDescriptor:
        return descriptor_from_mcp_tool(self._manager, item)

    def _synthetic_descriptor(self, server_id: str) -> CapabilityDescriptor:
        config = self._manager.server_config(server_id)
        assert config is not None
        namespace = mcp_capability_namespace(server_id, config)
        bundle_summaries = tuple(
            bundle.summary.strip()
            for bundle in config.yuki.tool_bundles.values()
            if bundle.summary.strip()
        )
        summary = config.yuki.summary.strip() or f"MCP Server {server_id}"
        description = " ".join((summary, *bundle_summaries)).strip()
        policy = host_annotation_policy(None)
        return CapabilityDescriptor(
            canonical_name=f"mcp:{server_id}:discover",
            model_name=safe_model_tool_name("mcp", server_id, "discover"),
            group=namespace,
            namespace=namespace,
            aliases=tuple(
                dict.fromkeys(
                    (
                        *config.yuki.tags,
                        server_id,
                        *config.yuki.tool_bundles,
                    )
                )
            ),
            use_when=tuple(
                dict.fromkeys(
                    item for item in (config.yuki.summary, *bundle_summaries) if item.strip()
                )
            ),
            input_schema=dict(_SYNTHETIC_SCHEMA),
            output_schema={"type": "object"},
            effect=policy.effect,
            risk=policy.risk,
            trust_source=CapabilityTrustSource.MCP,
            allowed_origins=frozenset(TurnOrigin),
            required_permissions=frozenset(),
            uses_external_data=True,
            cancellable=True,
            idempotency=policy.idempotency,
            provider_id=f"mcp.{server_id}",
            provider_tool_name="discover",
            description=description,
            compact_description=description[:300],
            tags=tuple(dict.fromkeys((*config.yuki.tags, "mcp"))),
            parallel_safe=False,
            schema_version="synthetic",
            provider_metadata={"synthetic": True},
        )

    def _gateway_descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            canonical_name="mcp.gateway",
            model_name="mcp_gateway",
            group="mcp",
            namespace="mcp",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["search", "describe", "call"]},
                    "search": {"type": "string"},
                    "describe": {"type": "string"},
                    "tool": {"type": "string"},
                    "server": {"type": "string"},
                    "query": {"type": "string"},
                    "server_id": {"type": "string"},
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect=CapabilityEffect.EXTERNAL_READ,
            risk=CapabilityRisk.READ,
            trust_source=CapabilityTrustSource.MCP,
            allowed_origins=frozenset(TurnOrigin),
            required_permissions=frozenset(),
            uses_external_data=True,
            cancellable=True,
            idempotency=CapabilityIdempotency.CONDITIONAL,
            provider_id="mcp.gateway",
            provider_tool_name="mcp_gateway",
            description="搜索、描述或调用已配置 MCP Server 的工具",
            compact_description="MCP 工具目录与调用网关",
            tags=("mcp", "gateway"),
            binding=self._gateway_binding,
            parallel_safe=False,
        )

    async def refresh(self, *, force: bool = False) -> None:
        for server_id in self._manager.configured_server_ids:
            try:
                await self._manager.refresh(server_id, force=force)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                continue

    async def close(self) -> None:
        await self._manager.close()
