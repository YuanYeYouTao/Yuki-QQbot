"""Secret-free persistence for MCP metadata, artifacts, and invocation telemetry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select

from qq_ai_bot.mcp.models import MCPServerConfig, MCPToolMetadata
from qq_ai_bot.mcp.redaction import redact_sensitive_data, redact_sensitive_text
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MCPServerStateModel,
    MCPToolCacheModel,
    MemoryToolReceiptModel,
    ToolArtifactModel,
    ToolInvocationModel,
)
from qq_ai_bot.runtime.observability import claim_runtime_turn_id

_MAX_STRUCTURED_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_JSON_PATH_PARTS = 32
_MAX_JSON_QUERY_CHARACTERS = 256
_MAX_JSON_SCAN_NODES = 50_000
_MAX_JSON_SCAN_DEPTH = 64
_MAX_JSON_PAGE_ITEMS = 100


def _redact_reflection_result(value: str) -> str:
    """Redact structured secrets before a bounded tool result can become evidence."""

    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return redact_sensitive_text(value)
    return json.dumps(
        redact_sensitive_data(decoded),
        ensure_ascii=False,
        separators=(",", ":"),
    )


class MCPRepository:
    def __init__(
        self,
        database: Database,
        *,
        reflection_excerpt_characters: int = 2000,
        reflection_retention_days: int = 7,
    ) -> None:
        self._database = database
        self._reflection_excerpt_characters = max(1, min(reflection_excerpt_characters, 8000))
        self._reflection_retention_days = max(1, min(reflection_retention_days, 30))

    async def state(self, server_id: str) -> MCPServerStateModel | None:
        async with self._database.sessions() as session:
            return await session.get(MCPServerStateModel, server_id)

    async def save_state(
        self,
        server_id: str,
        config: MCPServerConfig,
        config_hash: str,
        *,
        enabled: bool,
        status: str,
        server_info: dict[str, str] | None = None,
        connected: bool = False,
        refreshed: bool = False,
        error_category: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session:
            row = await session.get(MCPServerStateModel, server_id)
            if row is None:
                row = MCPServerStateModel(
                    server_id=server_id,
                    transport=config.transport.value,
                    config_hash=config_hash,
                    enabled=enabled,
                    lifecycle=config.lifecycle.value,
                    status=status,
                    protocol_version="",
                    server_name="",
                    server_version="",
                    server_instructions="",
                    updated_at=now,
                )
                session.add(row)
            row.transport = config.transport.value
            row.config_hash = config_hash
            row.enabled = enabled
            row.lifecycle = config.lifecycle.value
            row.status = status
            row.last_error_category = error_category
            row.updated_at = now
            if connected:
                row.last_connected_at = now
            if refreshed:
                row.last_refreshed_at = now
            if server_info:
                row.protocol_version = server_info.get("protocol_version", "")[:64]
                row.server_name = server_info.get("server_name", "")[:255]
                row.server_version = server_info.get("server_version", "")[:128]
                row.server_instructions = server_info.get("server_instructions", "")[:8000]
            await session.commit()

    async def set_enabled(self, server_id: str, enabled: bool) -> bool:
        async with self._database.sessions() as session:
            row = await session.get(MCPServerStateModel, server_id)
            if row is None:
                return False
            row.enabled = enabled
            row.status = "disconnected" if enabled else "disabled"
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return True

    async def cached_tools(self, server_id: str) -> tuple[MCPToolMetadata, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(MCPToolCacheModel)
                    .where(MCPToolCacheModel.server_id == server_id)
                    .order_by(MCPToolCacheModel.remote_tool_name)
                )
            ).scalars()
            return tuple(self._metadata(row) for row in rows)

    async def replace_cached_tools(
        self,
        server_id: str,
        tools: tuple[MCPToolMetadata, ...],
    ) -> None:
        async with self._database.sessions() as session:
            await session.execute(
                delete(MCPToolCacheModel).where(MCPToolCacheModel.server_id == server_id)
            )
            session.add_all(
                MCPToolCacheModel(
                    server_id=item.server_id,
                    remote_tool_name=item.remote_tool_name,
                    model_name=item.model_name,
                    description=item.description,
                    compact_description=item.compact_description,
                    input_schema_json=json.dumps(item.input_schema, ensure_ascii=False),
                    output_schema_json=json.dumps(item.output_schema, ensure_ascii=False),
                    annotations_json=json.dumps(item.annotations, ensure_ascii=False),
                    metadata_hash=item.metadata_hash,
                    refreshed_at=item.refreshed_at,
                )
                for item in tools
            )
            await session.commit()

    async def clear_cached_tools(self, server_id: str) -> None:
        async with self._database.sessions() as session:
            await session.execute(
                delete(MCPToolCacheModel).where(MCPToolCacheModel.server_id == server_id)
            )
            await session.commit()

    async def record_invocation(
        self,
        *,
        conversation_key: str,
        provider_id: str,
        tool_name: str,
        success: bool,
        latency_seconds: float,
        result_size: int,
        artifact_created: bool,
        error_category: str | None,
        trigger_message_id: str = "",
        bot_user_id: str = "",
        result_excerpt: str = "",
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            session.add(
                ToolInvocationModel(
                    runtime_turn_id=claim_runtime_turn_id(),
                    conversation_key_hash=hashlib.sha256(
                        conversation_key.encode("utf-8")
                    ).hexdigest(),
                    provider_id=provider_id[:128],
                    tool_name=tool_name[:255],
                    success=success,
                    latency_seconds=max(0.0, latency_seconds),
                    result_size=max(0, result_size),
                    artifact_created=artifact_created,
                    error_category=error_category[:128] if error_category else None,
                    created_at=now,
                )
            )
            event = None
            if trigger_message_id and bot_user_id:
                event = await session.scalar(
                    select(ChatEventModel).where(
                        ChatEventModel.bot_user_id == bot_user_id,
                        ChatEventModel.platform_message_id == trigger_message_id,
                    )
                )
            if event is not None:
                conversation_key = (
                    f"group:{event.group_id}"
                    if event.group_id
                    else f"private:{event.private_peer_user_id or event.sender_user_id}"
                )
                redacted = _redact_reflection_result(result_excerpt.strip())
                session.add(
                    MemoryToolReceiptModel(
                        conversation_key_hash=hashlib.sha256(
                            conversation_key.encode("utf-8")
                        ).hexdigest(),
                        trigger_event_id=event.id,
                        bot_user_id=event.bot_user_id,
                        provider_id=provider_id[:128],
                        tool_name=tool_name[:255],
                        success=success,
                        result_excerpt=redacted[: self._reflection_excerpt_characters],
                        result_characters=len(redacted),
                        error_category=error_category[:128] if error_category else None,
                        created_at=now,
                        expires_at=now + timedelta(days=self._reflection_retention_days),
                    )
                )

    @staticmethod
    def _metadata(row: MCPToolCacheModel) -> MCPToolMetadata:
        return MCPToolMetadata(
            server_id=row.server_id,
            remote_tool_name=row.remote_tool_name,
            model_name=row.model_name,
            description=row.description,
            compact_description=row.compact_description,
            input_schema=json.loads(row.input_schema_json),
            output_schema=json.loads(row.output_schema_json),
            annotations=json.loads(row.annotations_json),
            metadata_hash=row.metadata_hash,
            refreshed_at=_as_utc(row.refreshed_at),
        )


class ToolArtifactRepository:
    """Store complete oversized results in bounded files, not SQLite."""

    def __init__(self, database: Database, root: Path, *, retention_seconds: int) -> None:
        if retention_seconds <= 0:
            raise ValueError("artifact retention must be positive")
        self._database = database
        self._root = root
        self._retention = retention_seconds

    def configure_retention(self, retention_seconds: int) -> None:
        if retention_seconds <= 0:
            raise ValueError("artifact retention must be positive")
        self._retention = retention_seconds

    async def write_artifact(
        self,
        *,
        provider_id: str,
        tool_name: str,
        content: str,
        media_type: str,
        retention_seconds: int | None = None,
    ) -> str:
        handle = uuid.uuid4().hex
        relative = f"{handle}.json"
        path = self._root / relative
        encoded = content.encode("utf-8")
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, encoded)
        retention = retention_seconds if retention_seconds is not None else self._retention
        if retention <= 0:
            raise ValueError("artifact retention must be positive")
        now = datetime.now(UTC)
        async with self._database.sessions() as session:
            session.add(
                ToolArtifactModel(
                    handle_id=handle,
                    provider_id=provider_id[:128],
                    tool_name=tool_name[:255],
                    relative_path=relative,
                    media_type=media_type[:128],
                    byte_size=len(encoded),
                    created_at=now,
                    expires_at=now + timedelta(seconds=retention),
                )
            )
            await session.commit()
        return handle

    async def read(
        self,
        handle_id: str,
        *,
        operation: str = "text",
        path: tuple[str | int, ...] = (),
        offset: int = 0,
        limit: int = 8000,
        query: str = "",
        max_characters: int = 8000,
    ) -> dict[str, object] | None:
        if offset < 0 or limit <= 0 or max_characters <= 0:
            raise ValueError("artifact offset must be non-negative and limit must be positive")
        if operation != "text" and len(path) > _MAX_JSON_PATH_PARTS:
            return _artifact_error("artifact_path_too_deep", "Artifact 路径层级过深")
        if operation == "search" and len(query) > _MAX_JSON_QUERY_CHARACTERS:
            return _artifact_error("artifact_query_too_long", "Artifact 搜索词过长")
        if not handle_id.isalnum() or len(handle_id) > 64:
            return None
        async with self._database.sessions() as session:
            row = await session.get(ToolArtifactModel, handle_id)
            if row is None or _as_utc(row.expires_at) <= datetime.now(UTC):
                return None
            relative = row.relative_path
            provider_id = row.provider_id
            tool_name = row.tool_name
            byte_size = row.byte_size
        file_path = (self._root / relative).resolve()
        root = self._root.resolve()
        if root not in file_path.parents:
            return None
        if operation != "text" and byte_size > _MAX_STRUCTURED_ARTIFACT_BYTES:
            return _artifact_error(
                "artifact_too_large",
                "Artifact 超过结构化读取的安全大小上限",
                byte_size=byte_size,
            )
        try:
            content = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
        except OSError:
            return None
        if operation == "text":
            return _read_text_artifact(
                handle_id,
                content,
                offset=offset,
                limit=limit,
                query=query,
            )
        if operation not in {"inspect", "get", "search"}:
            return _artifact_error(
                "artifact_operation_invalid",
                "Artifact operation 必须是 inspect、get、search 或 text",
            )
        try:
            decoded = json.loads(content)
        except (json.JSONDecodeError, RecursionError):
            return _artifact_error(
                "artifact_not_json",
                "Artifact 不是合法 JSON，请使用 text 模式读取",
            )
        logical_root, logical_root_name = _logical_artifact_root(decoded)
        resolved_ok, resolved = _resolve_artifact_path(logical_root, path)
        if not resolved_ok:
            assert isinstance(resolved, dict)
            return resolved
        base: dict[str, object] = {
            "handle": handle_id,
            "mode": "json",
            "logical_root": logical_root_name,
            "provider_id": provider_id,
            "tool_name": tool_name,
        }
        if operation == "inspect":
            return _inspect_json(
                resolved,
                path=path,
                offset=offset,
                limit=min(limit, _MAX_JSON_PAGE_ITEMS),
                base=base,
                max_characters=max_characters,
            )
        if operation == "get":
            return _get_json(
                resolved,
                path=path,
                offset=offset,
                limit=min(limit, _MAX_JSON_PAGE_ITEMS),
                base=base,
                max_characters=max_characters,
            )
        if not query:
            return _artifact_error("artifact_query_required", "search 操作必须提供 query")
        return _search_json(
            resolved,
            path=path,
            query=query,
            offset=offset,
            limit=min(limit, _MAX_JSON_PAGE_ITEMS),
            base=base,
            max_characters=max_characters,
        )

    async def cleanup(self) -> int:
        now = datetime.now(UTC)
        async with self._database.sessions() as session:
            rows = tuple(
                (
                    await session.execute(
                        select(ToolArtifactModel).where(ToolArtifactModel.expires_at <= now)
                    )
                ).scalars()
            )
            for row in rows:
                path = (self._root / row.relative_path).resolve()
                if self._root.resolve() in path.parents:
                    try:
                        await asyncio.to_thread(path.unlink, missing_ok=True)
                    except OSError:
                        pass
                await session.delete(row)
            await session.commit()
            return len(rows)


def _read_text_artifact(
    handle_id: str,
    content: str,
    *,
    offset: int,
    limit: int,
    query: str,
) -> dict[str, object]:
    start = offset
    if query:
        found = content.casefold().find(query.casefold(), offset)
        if found < 0:
            return {
                "handle": handle_id,
                "mode": "text",
                "offset": offset,
                "next_offset": None,
                "total_characters": len(content),
                "content": "",
                "query_matched": False,
            }
        start = found
    end = min(len(content), start + limit)
    return {
        "handle": handle_id,
        "mode": "text",
        "offset": start,
        "next_offset": end if end < len(content) else None,
        "total_characters": len(content),
        "content": content[start:end],
        "query_matched": True if query else None,
    }


def _logical_artifact_root(value: object) -> tuple[object, str]:
    if (
        isinstance(value, dict)
        and "data" in value
        and ("provider_id" in value or "tool_name" in value or "ok" in value)
    ):
        return value["data"], "data"
    return value, "$"


def _resolve_artifact_path(
    root: object,
    path: tuple[str | int, ...],
) -> tuple[bool, object]:
    value = root
    traversed: list[str | int] = []
    for part in path:
        if isinstance(value, dict):
            if not isinstance(part, str) or part not in value:
                return (
                    False,
                    _artifact_error(
                        "artifact_path_not_found",
                        "Artifact 对象路径不存在",
                        path=traversed,
                        failed_part=part,
                    ),
                )
            value = value[part]
        elif isinstance(value, list):
            if isinstance(part, bool) or not isinstance(part, int) or not 0 <= part < len(value):
                return (
                    False,
                    _artifact_error(
                        "artifact_path_not_found",
                        "Artifact 数组下标不存在",
                        path=traversed,
                        failed_part=part,
                    ),
                )
            value = value[part]
        else:
            return (
                False,
                _artifact_error(
                    "artifact_path_not_found",
                    "Artifact 路径穿过了标量值",
                    path=traversed,
                    failed_part=part,
                ),
            )
        traversed.append(part)
    return True, value


def _inspect_json(
    value: object,
    *,
    path: tuple[str | int, ...],
    offset: int,
    limit: int,
    base: dict[str, object],
    max_characters: int,
) -> dict[str, object]:
    result: dict[str, object] = {**base, "path": list(path), "type": _json_type(value)}
    if isinstance(value, dict):
        keys = sorted(value, key=lambda item: str(item).casefold())
        selected = keys[offset : offset + limit]
        while selected:
            candidate = {
                **result,
                "total_children": len(keys),
                "children": [{"key": str(key), **_value_shape(value[key])} for key in selected],
                "next_offset": (
                    offset + len(selected) if offset + len(selected) < len(keys) else None
                ),
            }
            if _fits_json_budget(candidate, max_characters):
                return candidate
            selected.pop()
        result.update({"total_children": len(keys), "children": [], "next_offset": None})
    elif isinstance(value, list):
        selected = value[offset : offset + limit]
        while selected:
            candidate = {
                **result,
                "length": len(value),
                "children": [
                    {"index": offset + index, **_value_shape(item)}
                    for index, item in enumerate(selected)
                ],
                "next_offset": (
                    offset + len(selected) if offset + len(selected) < len(value) else None
                ),
            }
            if _fits_json_budget(candidate, max_characters):
                return candidate
            selected = selected[:-1]
        result.update({"length": len(value), "children": [], "next_offset": None})
    elif isinstance(value, str):
        result["characters"] = len(value)
    else:
        result["value"] = value
    return result


def _get_json(
    value: object,
    *,
    path: tuple[str | int, ...],
    offset: int,
    limit: int,
    base: dict[str, object],
    max_characters: int,
) -> dict[str, object]:
    direct = {**base, "path": list(path), "type": _json_type(value), "value": value}
    if offset == 0 and _fits_json_budget(direct, max_characters):
        return direct
    if isinstance(value, dict):
        keys = sorted(value, key=lambda item: str(item).casefold())
        selected = keys[offset : offset + limit]
        if not selected and offset >= len(keys):
            return {
                **base,
                "path": list(path),
                "type": "object",
                "total_items": len(keys),
                "offset": offset,
                "value": {},
                "next_offset": None,
            }
        while selected:
            page = {str(key): value[key] for key in selected}
            candidate = {
                **base,
                "path": list(path),
                "type": "object",
                "total_items": len(keys),
                "offset": offset,
                "value": page,
                "next_offset": (
                    offset + len(selected) if offset + len(selected) < len(keys) else None
                ),
            }
            if _fits_json_budget(candidate, max_characters):
                return candidate
            selected.pop()
        return _oversized_value(path, value, base=base, max_characters=max_characters)
    if isinstance(value, list):
        selected_values = value[offset : offset + limit]
        if not selected_values and offset >= len(value):
            return {
                **base,
                "path": list(path),
                "type": "array",
                "total_items": len(value),
                "offset": offset,
                "value": [],
                "next_offset": None,
            }
        while selected_values:
            candidate = {
                **base,
                "path": list(path),
                "type": "array",
                "total_items": len(value),
                "offset": offset,
                "value": selected_values,
                "next_offset": (
                    offset + len(selected_values)
                    if offset + len(selected_values) < len(value)
                    else None
                ),
            }
            if _fits_json_budget(candidate, max_characters):
                return candidate
            selected_values = selected_values[:-1]
        return _oversized_value(path, value, base=base, max_characters=max_characters)
    return _oversized_value(path, value, base=base, max_characters=max_characters)


def _search_json(
    value: object,
    *,
    path: tuple[str | int, ...],
    query: str,
    offset: int,
    limit: int,
    base: dict[str, object],
    max_characters: int,
) -> dict[str, object]:
    folded = query.casefold()
    matches: dict[tuple[str | int, ...], dict[str, object]] = {}
    scanned_nodes = 0
    scan_truncated = False

    def add_match(
        record_path: tuple[str | int, ...],
        matched_path: tuple[str | int, ...],
        record: object,
    ) -> None:
        matches.setdefault(
            record_path,
            {
                "path": list(record_path),
                "matched_path": list(matched_path),
                "value": record,
            },
        )

    def walk(item: object, item_path: tuple[str | int, ...], depth: int = 0) -> None:
        nonlocal scanned_nodes, scan_truncated
        if scan_truncated:
            return
        if depth > _MAX_JSON_SCAN_DEPTH:
            scan_truncated = True
            return
        scanned_nodes += 1
        if scanned_nodes > _MAX_JSON_SCAN_NODES:
            scan_truncated = True
            return
        if isinstance(item, dict):
            for key in sorted(item, key=lambda candidate: str(candidate).casefold()):
                child = item[key]
                child_path = (*item_path, str(key))
                if folded in str(key).casefold():
                    if isinstance(child, (dict, list)):
                        add_match(child_path, child_path, child)
                    else:
                        add_match(item_path, child_path, item)
                if _is_json_scalar(child):
                    if folded in _scalar_text(child).casefold():
                        add_match(item_path, child_path, item)
                else:
                    walk(child, child_path, depth + 1)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                child_path = (*item_path, index)
                if _is_json_scalar(child):
                    if folded in _scalar_text(child).casefold():
                        add_match(child_path, child_path, child)
                else:
                    walk(child, child_path, depth + 1)
        elif folded in _scalar_text(item).casefold():
            add_match(item_path, item_path, item)

    walk(value, path)
    ordered = [matches[key] for key in sorted(matches, key=_path_sort_key)]
    selected = ordered[offset : offset + limit]
    rendered: list[dict[str, object]] = []
    for match in selected:
        candidate = dict(match)
        if not _fits_json_budget(candidate, max_characters):
            record = candidate.pop("value")
            candidate.update(
                {
                    "value_omitted": True,
                    "value_shape": _value_shape(record),
                    "instruction": "匹配对象过大，请对 path 执行 inspect 或 get",
                }
            )
        aggregate = {
            **base,
            "query": query,
            "matches": [*rendered, candidate],
            "next_offset": None,
            "scan_truncated": scan_truncated,
            "scanned_nodes": min(scanned_nodes, _MAX_JSON_SCAN_NODES),
        }
        if not _fits_json_budget(aggregate, max_characters):
            break
        rendered.append(candidate)
    has_more = offset + len(rendered) < len(ordered) or len(rendered) < len(selected)
    return {
        **base,
        "query": query,
        "matches": rendered,
        "next_offset": offset + len(rendered) if has_more else None,
        "scan_truncated": scan_truncated,
        "scanned_nodes": min(scanned_nodes, _MAX_JSON_SCAN_NODES),
    }


def _oversized_value(
    path: tuple[str | int, ...],
    value: object,
    *,
    base: dict[str, object],
    max_characters: int,
) -> dict[str, object]:
    result = _artifact_error(
        "artifact_value_too_large",
        "目标值无法完整放入当前工具结果预算，请读取更深层路径",
        **base,
        path=list(path),
        value_shape=_value_shape(value),
        estimated_characters=_json_size(value),
    )
    if not _fits_json_budget(result, max_characters):
        result.pop("estimated_characters", None)
    return result


def _artifact_error(error_code: str, detail: str, **metadata: object) -> dict[str, object]:
    return {"error_code": error_code, "detail": detail, **metadata}


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _value_shape(value: object) -> dict[str, object]:
    shape: dict[str, object] = {"type": _json_type(value)}
    if isinstance(value, dict):
        shape["child_count"] = len(value)
    elif isinstance(value, list):
        shape["length"] = len(value)
    elif isinstance(value, str):
        shape["characters"] = len(value)
    return shape


def _is_json_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _scalar_text(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _path_sort_key(path: tuple[str | int, ...]) -> tuple[str, ...]:
    return tuple(f"{type(part).__name__}:{part}" for part in path)


def _fits_json_budget(value: object, max_characters: int) -> bool:
    size = _json_size(value, compact=True)
    return size is not None and size <= max(256, max_characters - 512)


def _json_size(value: object, *, compact: bool = False) -> int | None:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":") if compact else None,
        )
    except (RecursionError, ValueError):
        return None
    return len(rendered)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
