"""Translate discovered MCP metadata into unified Tool Kernel descriptors."""

from __future__ import annotations

import re
from dataclasses import dataclass

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.capabilities.namespace import is_valid_namespace_id
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.models import MCPServerConfig, MCPToolAnnotationOverride, MCPToolMetadata

_NAMESPACE_SEGMENT = re.compile(r"[^a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class MCPHostAnnotationPolicy:
    """Host risk derived only from operator overrides, never remote hints."""

    effect: CapabilityEffect
    risk: CapabilityRisk
    idempotency: CapabilityIdempotency
    parallel_safe: bool
    finalize_after_commit: bool
    read_only: bool


def mcp_capability_namespace(server_id: str, config: MCPServerConfig) -> str:
    """Prefer operator scope when it is already a valid namespace id."""

    scope = (config.yuki.scope or "").strip()
    if is_valid_namespace_id(scope):
        return scope
    candidate = f"mcp.{server_id}"
    if is_valid_namespace_id(candidate):
        return candidate
    sanitized = _NAMESPACE_SEGMENT.sub("_", server_id.casefold()).strip("_") or "server"
    fallback = f"mcp.{sanitized}"
    return fallback if is_valid_namespace_id(fallback) else "mcp"


def mcp_tool_namespace(
    server_id: str,
    config: MCPServerConfig,
    remote_tool_name: str,
) -> str:
    """Resolve one tool's primary namespace; bundles stay additional scopes."""

    override = config.yuki.tool_annotations.get(remote_tool_name)
    if override is not None and is_valid_namespace_id(override.namespace.strip()):
        return override.namespace.strip()
    matching = [
        bundle
        for bundle in config.yuki.tool_bundles.values()
        if remote_tool_name in bundle.include_tools and is_valid_namespace_id(bundle.scope)
    ]
    if len(matching) == 1:
        return matching[0].scope
    return mcp_capability_namespace(server_id, config)


def host_annotation_policy(
    override: MCPToolAnnotationOverride | None,
) -> MCPHostAnnotationPolicy:
    """Unknown MCP tools default to WRITE_STATE/MUTATE until an operator override."""

    read_only = bool(override is not None and override.read_only_hint)
    destructive = bool(override is not None and override.destructive_hint)
    if override is not None and override.idempotent_hint is not None:
        idempotent = bool(override.idempotent_hint)
    else:
        idempotent = read_only
    finalize_after_commit = bool(override is not None and override.finalize_after_commit)
    return MCPHostAnnotationPolicy(
        effect=CapabilityEffect.EXTERNAL_READ if read_only else CapabilityEffect.WRITE_STATE,
        risk=(
            CapabilityRisk.DESTRUCTIVE
            if destructive
            else CapabilityRisk.READ
            if read_only
            else CapabilityRisk.MUTATE
        ),
        idempotency=(
            CapabilityIdempotency.IDEMPOTENT if idempotent else CapabilityIdempotency.CONDITIONAL
        ),
        parallel_safe=read_only,
        finalize_after_commit=finalize_after_commit,
        read_only=read_only,
    )


def descriptor_from_mcp_tool(
    manager: MCPManager,
    item: MCPToolMetadata,
) -> CapabilityDescriptor:
    """Build one descriptor; remote annotations stay descriptive, not authoritative."""

    from qq_ai_bot.mcp.binding import MCPToolBinding

    config = manager.server_config(item.server_id)
    if config is None:
        raise ValueError(f"unknown MCP server: {item.server_id}")
    override = config.yuki.tool_annotations.get(item.remote_tool_name)
    namespace = mcp_tool_namespace(item.server_id, config, item.remote_tool_name)
    policy = host_annotation_policy(override)
    bundles = tuple(
        bundle
        for bundle in config.yuki.tool_bundles.values()
        if item.remote_tool_name in bundle.include_tools
    )
    bundle_scopes = tuple(bundle.scope for bundle in bundles)
    aliases = tuple(
        dict.fromkeys(
            (
                *(override.aliases if override is not None else ()),
                *config.yuki.tags,
                item.remote_tool_name,
                item.remote_tool_name.replace("-", " "),
                *item.remote_tool_name.split("-"),
            )
        )
    )
    use_when = tuple(
        dict.fromkeys(
            value
            for value in (
                *(override.use_when if override is not None else ()),
                config.yuki.summary,
                *(bundle.summary for bundle in bundles),
            )
            if value.strip()
        )
    )
    return CapabilityDescriptor(
        canonical_name=f"mcp:{item.server_id}:{item.remote_tool_name}",
        model_name=item.model_name,
        group=namespace,
        namespace=namespace,
        aliases=aliases,
        use_when=use_when,
        additional_scopes=bundle_scopes,
        bundle_scopes=bundle_scopes,
        scope_summaries=tuple((bundle.scope, bundle.summary) for bundle in bundles),
        input_schema=item.input_schema,
        output_schema=item.output_schema or {"type": "object"},
        effect=policy.effect,
        risk=policy.risk,
        trust_source=CapabilityTrustSource.MCP,
        allowed_origins=frozenset(TurnOrigin),
        required_permissions=frozenset(),
        uses_external_data=True,
        cancellable=True,
        idempotency=policy.idempotency,
        provider_id=f"mcp.{item.server_id}",
        provider_tool_name=item.remote_tool_name,
        description=item.description,
        compact_description=item.compact_description,
        tags=tuple(
            dict.fromkeys(
                (
                    *(override.tags if override is not None else ()),
                    *config.yuki.tags,
                    *(
                        name
                        for name, bundle in config.yuki.tool_bundles.items()
                        if bundle in bundles
                    ),
                )
            )
        ),
        binding=MCPToolBinding(manager, item.server_id, item.remote_tool_name),
        parallel_safe=policy.parallel_safe,
        result_kind="mcp_content",
        schema_version=item.metadata_hash,
        provider_metadata={"mcp_annotations": dict(item.annotations)},
        finalize_after_commit=policy.finalize_after_commit,
    )
