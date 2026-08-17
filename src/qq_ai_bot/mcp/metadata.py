"""MCP tool metadata hashing and filtering."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from qq_ai_bot.capabilities.catalog import safe_model_tool_name
from qq_ai_bot.mcp.models import MCPServerConfig, MCPToolMetadata


def metadata_from_sdk_tool(
    server_id: str,
    tool: Any,
    config: MCPServerConfig,
) -> MCPToolMetadata | None:
    name = str(getattr(tool, "name", "")).strip()
    if not name:
        return None
    if config.include_tools and name not in config.include_tools:
        return None
    if name in config.exclude_tools:
        return None
    description = str(getattr(tool, "description", "") or "")
    input_schema = getattr(tool, "inputSchema", {})
    output_schema = getattr(tool, "outputSchema", {}) or {}
    annotations_value = getattr(tool, "annotations", None)
    annotations: object = {}
    if annotations_value is not None:
        dump = getattr(annotations_value, "model_dump", None)
        if callable(dump):
            annotations = dump(mode="json", exclude_none=True)
    annotation_values = dict(annotations) if isinstance(annotations, dict) else {}
    override = config.yuki.tool_annotations.get(name)
    if override is not None:
        for key, value in override.model_dump(
            mode="json", by_alias=True, exclude_none=True
        ).items():
            if key in {
                "readOnlyHint",
                "destructiveHint",
                "idempotentHint",
                "openWorldHint",
                "finalizeAfterCommit",
            }:
                annotation_values[key] = value
    raw = {
        "name": name,
        "description": description,
        "input": input_schema,
        "output": output_schema,
        "annotations": annotation_values,
    }
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    return MCPToolMetadata(
        server_id=server_id,
        remote_tool_name=name,
        model_name=safe_model_tool_name("mcp", server_id, name),
        description=description,
        compact_description=" ".join(description.split())[:300],
        input_schema=dict(input_schema) if isinstance(input_schema, dict) else {"type": "object"},
        output_schema=dict(output_schema) if isinstance(output_schema, dict) else {},
        annotations=annotation_values,
        metadata_hash=digest,
        refreshed_at=datetime.now(UTC),
    )
