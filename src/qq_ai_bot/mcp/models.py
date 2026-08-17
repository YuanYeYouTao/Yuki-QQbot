"""Strict MCP configuration and runtime records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class MCPTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class MCPLifecycle(StrEnum):
    LAZY = "lazy"
    EAGER = "eager"
    KEEP_ALIVE = "keep_alive"
    LAZY_KEEP_ALIVE = "lazy_keep_alive"


class MCPToolAnnotationOverride(_StrictModel):
    """Operator-supplied MCP annotation hints keyed by the remote tool name."""

    namespace: str = ""
    aliases: tuple[str, ...] = ()
    use_when: tuple[str, ...] = Field(default=(), alias="useWhen")
    tags: tuple[str, ...] = ()
    read_only_hint: bool | None = Field(default=None, alias="readOnlyHint")
    destructive_hint: bool | None = Field(default=None, alias="destructiveHint")
    idempotent_hint: bool | None = Field(default=None, alias="idempotentHint")
    open_world_hint: bool | None = Field(default=None, alias="openWorldHint")
    finalize_after_commit: bool | None = Field(default=None, alias="finalizeAfterCommit")


class MCPAutomationMetadata(_StrictModel):
    """Explicit opt-in for exposing selected server tools to scheduled tasks."""

    enabled: bool = False
    permission: Literal["user", "superuser"] = "superuser"
    include_tools: tuple[str, ...] = Field(default=(), alias="includeTools")

    @model_validator(mode="after")
    def _included_tools(self) -> MCPAutomationMetadata:
        if self.enabled and not self.include_tools:
            raise ValueError("automation.includeTools is required when automation is enabled")
        if any(not name.strip() or len(name) > 255 for name in self.include_tools):
            raise ValueError("automation.includeTools values must be remote tool names")
        if len(set(self.include_tools)) != len(self.include_tools):
            raise ValueError("automation.includeTools contains duplicate tool names")
        return self


class MCPToolBundle(_StrictModel):
    """A named, indivisible tool-selection unit within one MCP server."""

    scope: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=500)
    include_tools: tuple[str, ...] = Field(alias="includeTools", min_length=1)

    @model_validator(mode="after")
    def _valid_tools(self) -> MCPToolBundle:
        if any(not name.strip() or len(name) > 255 for name in self.include_tools):
            raise ValueError("toolBundles includeTools values must be remote tool names")
        if len(set(self.include_tools)) != len(self.include_tools):
            raise ValueError("toolBundles includeTools contains duplicate tool names")
        return self


class MCPServerMetadata(_StrictModel):
    scope: str = ""
    summary: str = Field(default="", max_length=500)
    tags: tuple[str, ...] = ()
    tool_annotations: dict[str, MCPToolAnnotationOverride] = Field(
        default_factory=dict,
        alias="toolAnnotations",
    )
    automation: MCPAutomationMetadata = MCPAutomationMetadata()
    tool_bundles: dict[str, MCPToolBundle] = Field(
        default_factory=dict,
        alias="toolBundles",
    )

    @model_validator(mode="after")
    def _tool_names(self) -> MCPServerMetadata:
        if any(not name.strip() or len(name) > 255 for name in self.tool_annotations):
            raise ValueError("toolAnnotations keys must be non-empty remote tool names")
        if any(not name.strip() or len(name) > 64 for name in self.tool_bundles):
            raise ValueError("toolBundles keys must be non-empty names")
        scopes = [bundle.scope for bundle in self.tool_bundles.values()]
        if len(scopes) != len(set(scopes)):
            raise ValueError("toolBundles scopes must be unique within a server")
        return self


class MCPServerConfig(_StrictModel):
    """One stdio or Streamable HTTP endpoint from `.mcp.json`."""

    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    url: str | None = Field(default=None, repr=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    lifecycle: MCPLifecycle = MCPLifecycle.LAZY
    disabled: bool = False
    connect_timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        alias="connectTimeoutSeconds",
    )
    request_timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        alias="requestTimeoutSeconds",
    )
    reconnect_delay_seconds: float = Field(default=5, gt=0, alias="reconnectDelaySeconds")
    include_tools: tuple[str, ...] = Field(default=(), alias="includeTools")
    exclude_tools: tuple[str, ...] = Field(default=(), alias="excludeTools")
    yuki: MCPServerMetadata = MCPServerMetadata()

    @model_validator(mode="after")
    def _transport(self) -> MCPServerConfig:
        if bool(self.command) == bool(self.url):
            raise ValueError("exactly one of command or url is required")
        if self.url is not None and not self.url.casefold().startswith(("http://", "https://")):
            raise ValueError("MCP url must use http or https")
        if self.command is None and (self.args or self.cwd or self.env):
            raise ValueError("args/cwd/env are only valid for stdio servers")
        if self.url is None and self.headers:
            raise ValueError("headers are only valid for Streamable HTTP servers")
        if set(self.include_tools) & set(self.exclude_tools):
            raise ValueError("includeTools and excludeTools overlap")
        return self

    @property
    def transport(self) -> MCPTransport:
        return MCPTransport.STDIO if self.command is not None else MCPTransport.STREAMABLE_HTTP


class MCPConfigFile(_StrictModel):
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict, alias="mcpServers")


class MCPToolMetadata(_StrictModel):
    server_id: str
    remote_tool_name: str
    model_name: str
    description: str = ""
    compact_description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    metadata_hash: str
    refreshed_at: datetime


class MCPServerStatus(_StrictModel):
    server_id: str
    transport: MCPTransport
    enabled: bool
    lifecycle: MCPLifecycle
    status: str
    configured_tools: int = 0
    connected: bool = False
    last_error_category: str | None = None
    protocol_version: str = ""
    server_name: str = ""
    server_version: str = ""


class MCPHealthSnapshot(_StrictModel):
    enabled: bool
    configured_servers: int
    connected_servers: int
    cached_tools: int
    active_calls: int
    last_call_at: datetime | None = None
    last_error_category: str | None = None


class MCPCallContent(_StrictModel):
    type: str
    text: str | None = None
    data: str | None = None
    mime_type: str | None = None


class MCPCallResult(_StrictModel):
    ok: bool
    structured_content: Any = None
    content: tuple[dict[str, Any], ...] = ()
    error_code: str | None = None
    public_message: str | None = None
    retryable: bool = False


TransportLiteral = Literal["stdio", "streamable_http"]
