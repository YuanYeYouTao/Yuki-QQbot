"""MCP configuration, lazy connection, metadata refresh, and execution lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.mcp.config import LoadedMCPConfig, load_mcp_config, redacted_server_config
from qq_ai_bot.mcp.connection import MCPConnection, MCPConnectionFactory, SDKMCPConnection
from qq_ai_bot.mcp.errors import classify_mcp_exception
from qq_ai_bot.mcp.metadata import metadata_from_sdk_tool
from qq_ai_bot.mcp.models import (
    MCPHealthSnapshot,
    MCPLifecycle,
    MCPServerConfig,
    MCPServerStatus,
    MCPToolMetadata,
)
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.mcp.result_normalizer import normalize_mcp_result

logger = logging.getLogger(__name__)
MCPToolsChangedListener = Callable[[str, tuple[MCPToolMetadata, ...]], Awaitable[None]]


class MCPManager:
    """Own every MCP session; commands, gateway, and direct tools all use this path."""

    def __init__(
        self,
        *,
        enabled: bool,
        config_path: Path,
        cache_enabled: bool,
        metadata_cache_ttl_seconds: int,
        connect_timeout_seconds: float,
        request_timeout_seconds: float,
        max_parallel_calls: int,
        repository: MCPRepository,
        connection_factory: MCPConnectionFactory = SDKMCPConnection,
    ) -> None:
        if metadata_cache_ttl_seconds <= 0 or max_parallel_calls <= 0:
            raise ValueError("MCP cache TTL and parallel call count must be positive")
        self._enabled = enabled
        self._config_path = config_path
        self._cache_enabled = cache_enabled
        self._cache_ttl = metadata_cache_ttl_seconds
        self._connect_timeout = connect_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._repository = repository
        self._factory = connection_factory
        self._config = LoadedMCPConfig({}, {}, False)
        self._connections: dict[str, MCPConnection] = {}
        self._tools: dict[str, tuple[MCPToolMetadata, ...]] = {}
        self._enabled_servers: dict[str, bool] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._max_parallel_calls = max_parallel_calls
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        self._active_calls = 0
        self._closing = False
        self._last_call_at: datetime | None = None
        self._last_error_category: str | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._reconnect_tasks: dict[str, asyncio.Task[None]] = {}
        self._tools_changed_listeners: list[MCPToolsChangedListener] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure_runtime(
        self,
        *,
        enabled: bool,
        metadata_cache_ttl_seconds: int | None = None,
        connect_timeout_seconds: float | None = None,
        request_timeout_seconds: float | None = None,
        max_parallel_calls: int | None = None,
    ) -> None:
        """Apply the effective global hot switch without rebuilding sessions."""

        if metadata_cache_ttl_seconds is not None:
            if metadata_cache_ttl_seconds <= 0:
                raise ValueError("MCP metadata cache TTL must be positive")
            self._cache_ttl = metadata_cache_ttl_seconds
        if connect_timeout_seconds is not None:
            if connect_timeout_seconds <= 0:
                raise ValueError("MCP connect timeout must be positive")
            self._connect_timeout = connect_timeout_seconds
        if request_timeout_seconds is not None:
            if request_timeout_seconds <= 0:
                raise ValueError("MCP request timeout must be positive")
            self._request_timeout = request_timeout_seconds
        if max_parallel_calls is not None:
            if max_parallel_calls <= 0:
                raise ValueError("MCP parallel call count must be positive")
            self._max_parallel_calls = max_parallel_calls
        self._enabled = enabled

    @property
    def configured_server_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._config.servers))

    @property
    def cached_tools(self) -> tuple[MCPToolMetadata, ...]:
        return tuple(item for server in sorted(self._tools) for item in self._tools[server])

    def server_config(self, server_id: str) -> MCPServerConfig | None:
        return self._config.servers.get(server_id)

    def server_enabled(self, server_id: str) -> bool:
        config = self._config.servers.get(server_id)
        return bool(
            self._enabled
            and config is not None
            and self._enabled_servers.get(server_id, not config.disabled)
        )

    def add_tools_changed_listener(self, listener: MCPToolsChangedListener) -> None:
        if listener not in self._tools_changed_listeners:
            self._tools_changed_listeners.append(listener)

    def remove_tools_changed_listener(self, listener: MCPToolsChangedListener) -> None:
        if listener in self._tools_changed_listeners:
            self._tools_changed_listeners.remove(listener)

    async def start(self) -> None:
        self._closing = False
        self._config = load_mcp_config(self._config_path)
        for server_id, config in self._config.servers.items():
            previous = await self._repository.state(server_id)
            enabled = previous.enabled if previous is not None else not config.disabled
            self._enabled_servers[server_id] = enabled
            valid_hash = (
                previous is not None and previous.config_hash == self._config.hashes[server_id]
            )
            if self._cache_enabled and valid_hash:
                cached = await self._repository.cached_tools(server_id)
                if cached and datetime.now(UTC) - cached[0].refreshed_at <= timedelta(
                    seconds=self._cache_ttl
                ):
                    self._tools[server_id] = cached
            elif previous is not None:
                await self._repository.clear_cached_tools(server_id)
            await self._repository.save_state(
                server_id,
                config,
                self._config.hashes[server_id],
                enabled=enabled,
                status="disabled" if not enabled else "disconnected",
            )
        if not self._enabled:
            return
        for server_id, config in self._config.servers.items():
            if self._enabled_servers.get(server_id) and config.lifecycle in {
                MCPLifecycle.EAGER,
                MCPLifecycle.KEEP_ALIVE,
            }:
                try:
                    await self.refresh(server_id)
                except Exception:
                    if config.lifecycle is MCPLifecycle.KEEP_ALIVE:
                        self._schedule_reconnect(server_id)
                    continue

    async def reload_config(self) -> None:
        loaded = load_mcp_config(self._config_path)
        removed = set(self._config.servers) - set(loaded.servers)
        changed = {
            server_id
            for server_id in set(self._config.servers) & set(loaded.servers)
            if self._config.hashes.get(server_id) != loaded.hashes.get(server_id)
        }
        for server_id in removed | changed:
            self._cancel_reconnect(server_id)
            await self.disconnect(server_id)
            self._tools.pop(server_id, None)
        for server_id in removed:
            self._enabled_servers.pop(server_id, None)
        self._config = loaded
        await self.start()
        for server_id in removed:
            await self._notify_tools_changed(server_id, ())

    async def refresh(self, server_id: str, *, force: bool = True) -> tuple[MCPToolMetadata, ...]:
        config = self._require_enabled(server_id)
        cached = self._tools.get(server_id, ())
        if (
            not force
            and cached
            and datetime.now(UTC) - min(item.refreshed_at for item in cached)
            <= timedelta(seconds=self._cache_ttl)
        ):
            return cached
        async with self._lock(server_id):
            connection = await self._connect_unlocked(server_id, config)
            try:
                raw_tools = await connection.list_tools()
                tools = tuple(
                    item
                    for raw in raw_tools
                    if (item := metadata_from_sdk_tool(server_id, raw, config)) is not None
                )
                self._tools[server_id] = tools
                if self._cache_enabled:
                    await self._repository.replace_cached_tools(server_id, tools)
                await self._repository.save_state(
                    server_id,
                    config,
                    self._config.hashes[server_id],
                    enabled=True,
                    status="connected",
                    server_info=connection.server_info,
                    connected=True,
                    refreshed=True,
                )
                await self._notify_tools_changed(server_id, tools)
                return tools
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._save_error(server_id, config, exc)
                raise

    async def ensure_metadata(self, server_id: str) -> tuple[MCPToolMetadata, ...]:
        """Populate a lazy server catalog when Capability Runtime needs that server."""

        return await self.refresh(server_id, force=False)

    async def resolve_tool(
        self,
        server_id: str,
        tool_name: str,
        *,
        discover: bool = True,
    ) -> MCPToolMetadata:
        """Resolve only an enabled, discovered, config-filtered current tool."""

        self._require_enabled(server_id)
        if discover and not self._tools.get(server_id):
            await self.ensure_metadata(server_id)
        item = self.describe_tool(server_id, tool_name)
        if item is None:
            raise ValueError(f"unknown or unavailable MCP tool: {server_id}/{tool_name}")
        return item

    async def _call_resolved_tool(
        self,
        metadata: MCPToolMetadata,
        arguments: dict[str, object],
        *,
        conversation_key: str = "",
        record_invocation: bool = True,
    ) -> ToolExecutionResult:
        """Execute metadata that was resolved and policy-checked by a ToolBinding."""

        if self._closing:
            raise RuntimeError("MCP manager is shutting down")
        server_id = metadata.server_id
        tool_name = metadata.remote_tool_name
        config = self._require_enabled(server_id)
        started = time.perf_counter()
        result = ToolExecutionResult(
            ok=False,
            error_code="mcp_tool_failed",
            public_message="MCP 工具调用失败",
            mutation_committed=False,
            provider_id=f"mcp.{server_id}",
            tool_name=tool_name,
        )
        cancelled = False
        semaphore = self._semaphores.setdefault(
            self._max_parallel_calls,
            asyncio.Semaphore(self._max_parallel_calls),
        )
        async with semaphore:
            self._active_calls += 1
            try:
                current = await self.resolve_tool(server_id, tool_name)
                tool_name = current.remote_tool_name
                connection = await self._ensure_connection(server_id, config)
                raw = await connection.call_tool(tool_name, arguments)
                result = normalize_mcp_result(raw, server_id=server_id, tool_name=tool_name)
                return result
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                failure = classify_mcp_exception(exc)
                result = ToolExecutionResult(
                    ok=False,
                    error_code=failure.code,
                    public_message=failure.public_message,
                    retryable=failure.retryable,
                    mutation_committed=False,
                    provider_id=f"mcp.{server_id}",
                    tool_name=tool_name,
                )
                self._last_error_category = result.error_code
                if failure.disconnect:
                    await self.disconnect(server_id)
                    self._schedule_reconnect_if_persistent(server_id, config)
                return result
            finally:
                self._active_calls -= 1
                if not cancelled:
                    self._last_call_at = datetime.now(UTC)
                    if result.ok:
                        self._last_error_category = None
                if record_invocation and not cancelled:
                    serialized = json.dumps(result.model_payload(), ensure_ascii=False, default=str)
                    await self._repository.record_invocation(
                        conversation_key=conversation_key,
                        provider_id=f"mcp.{server_id}",
                        tool_name=tool_name,
                        success=result.ok,
                        latency_seconds=time.perf_counter() - started,
                        result_size=len(serialized.encode("utf-8")),
                        artifact_created=False,
                        error_category=result.error_code,
                    )
                if config.lifecycle is MCPLifecycle.LAZY and not cancelled:
                    await self.disconnect(server_id)

    def search_tools(
        self, query: str, *, server_id: str | None = None
    ) -> tuple[MCPToolMetadata, ...]:
        terms = tuple(part for part in query.casefold().split() if part)
        items = [
            item
            for item in self.cached_tools
            if self.server_enabled(item.server_id)
            and (server_id is None or item.server_id == server_id)
        ]
        items.sort(
            key=lambda item: (
                -sum(
                    term
                    in " ".join(
                        (
                            item.remote_tool_name,
                            item.description,
                            *self._config.servers[item.server_id].yuki.tags,
                        )
                    ).casefold()
                    for term in terms
                ),
                item.server_id,
                item.remote_tool_name,
            )
        )
        return tuple(items)

    def describe_tool(self, server_id: str, tool_name: str) -> MCPToolMetadata | None:
        return next(
            (
                item
                for item in self._tools.get(server_id, ())
                if item.remote_tool_name == tool_name or item.model_name == tool_name
            ),
            None,
        )

    async def set_enabled(self, server_id: str, enabled: bool) -> None:
        config = self._require_server(server_id)
        self._enabled_servers[server_id] = enabled
        await self._repository.save_state(
            server_id,
            config,
            self._config.hashes[server_id],
            enabled=enabled,
            status="disconnected" if enabled else "disabled",
        )
        if not enabled:
            self._cancel_reconnect(server_id)
            await self.disconnect(server_id)
            await self._notify_tools_changed(server_id, ())

    async def reconnect(self, server_id: str) -> tuple[MCPToolMetadata, ...]:
        self._cancel_reconnect(server_id)
        await self.disconnect(server_id)
        return await self.refresh(server_id)

    async def disconnect(self, server_id: str) -> None:
        connection = self._connections.pop(server_id, None)
        if connection is not None:
            await connection.close()

    async def close(self) -> None:
        self._closing = True
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._reconnect_tasks.clear()
        for server_id in tuple(self._connections):
            await self.disconnect(server_id)

    async def status(self, server_id: str) -> MCPServerStatus:
        config = self._require_server(server_id)
        connection = self._connections.get(server_id)
        state = await self._repository.state(server_id)
        return MCPServerStatus(
            server_id=server_id,
            transport=config.transport,
            enabled=self._enabled_servers.get(server_id, not config.disabled),
            lifecycle=config.lifecycle,
            status=state.status if state is not None else "uninitialized",
            configured_tools=len(self._tools.get(server_id, ())),
            connected=bool(connection and connection.connected),
            last_error_category=state.last_error_category if state is not None else None,
            protocol_version=state.protocol_version if state is not None else "",
            server_name=state.server_name if state is not None else "",
            server_version=state.server_version if state is not None else "",
        )

    async def statuses(self) -> tuple[MCPServerStatus, ...]:
        return tuple([await self.status(server_id) for server_id in self.configured_server_ids])

    def health(self) -> MCPHealthSnapshot:
        """Return in-memory state only; never connects a lazy server."""

        return MCPHealthSnapshot(
            enabled=self._enabled,
            configured_servers=len(self._config.servers),
            connected_servers=sum(
                connection.connected for connection in self._connections.values()
            ),
            cached_tools=len(self.cached_tools),
            active_calls=self._active_calls,
            last_call_at=self._last_call_at,
            last_error_category=self._last_error_category,
        )

    def display_config(self, server_id: str) -> dict[str, object]:
        return redacted_server_config(self._require_server(server_id))

    async def _ensure_connection(
        self,
        server_id: str,
        config: MCPServerConfig,
    ) -> MCPConnection:
        connection = self._connections.get(server_id)
        if connection is not None and connection.connected:
            return connection
        async with self._lock(server_id):
            return await self._connect_unlocked(server_id, config)

    async def _connect_unlocked(
        self,
        server_id: str,
        config: MCPServerConfig,
    ) -> MCPConnection:
        connection = self._connections.get(server_id)
        if connection is not None and connection.connected:
            return connection
        connection = self._factory(
            config,
            connect_timeout_seconds=(config.connect_timeout_seconds or self._connect_timeout),
            request_timeout_seconds=(config.request_timeout_seconds or self._request_timeout),
        )
        setter = getattr(connection, "set_tools_changed_callback", None)
        if callable(setter):
            setter(lambda: self._queue_tools_refresh(server_id))
        try:
            await connection.connect()
        except asyncio.CancelledError:
            await connection.close()
            raise
        except Exception as exc:
            await connection.close()
            await self._save_error(server_id, config, exc)
            raise
        self._connections[server_id] = connection
        await self._repository.save_state(
            server_id,
            config,
            self._config.hashes[server_id],
            enabled=True,
            status="connected",
            server_info=connection.server_info,
            connected=True,
        )
        return connection

    async def _queue_tools_refresh(self, server_id: str) -> None:
        if self._closing:
            return

        async def refresh_after_notification() -> None:
            try:
                await self.refresh(server_id, force=True)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                return

        task = asyncio.create_task(
            refresh_after_notification(),
            name=f"mcp-tools-refresh:{server_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _schedule_reconnect_if_persistent(
        self,
        server_id: str,
        config: MCPServerConfig,
    ) -> None:
        if config.lifecycle in {MCPLifecycle.KEEP_ALIVE, MCPLifecycle.LAZY_KEEP_ALIVE}:
            self._schedule_reconnect(server_id)

    def _schedule_reconnect(self, server_id: str) -> None:
        existing = self._reconnect_tasks.get(server_id)
        if self._closing or (existing is not None and not existing.done()):
            return
        config = self._config.servers.get(server_id)
        if config is None or not self._enabled_servers.get(server_id, not config.disabled):
            return

        async def restore() -> None:
            while not self._closing and self._enabled_servers.get(server_id, False):
                await asyncio.sleep(config.reconnect_delay_seconds)
                try:
                    await self.refresh(server_id, force=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                return

        task = asyncio.create_task(restore(), name=f"mcp-reconnect:{server_id}")
        self._reconnect_tasks[server_id] = task
        self._background_tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            self._background_tasks.discard(done)
            if self._reconnect_tasks.get(server_id) is done:
                self._reconnect_tasks.pop(server_id, None)

        task.add_done_callback(completed)

    def _cancel_reconnect(self, server_id: str) -> None:
        task = self._reconnect_tasks.pop(server_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _save_error(
        self,
        server_id: str,
        config: MCPServerConfig,
        exc: Exception,
    ) -> None:
        failure = classify_mcp_exception(exc)
        await self._repository.save_state(
            server_id,
            config,
            self._config.hashes[server_id],
            enabled=self._enabled_servers.get(server_id, True),
            status="failed",
            error_category=failure.code,
        )

    def _require_server(self, server_id: str) -> MCPServerConfig:
        config = self._config.servers.get(server_id)
        if config is None:
            raise ValueError(f"unknown MCP server: {server_id}")
        return config

    def _require_enabled(self, server_id: str) -> MCPServerConfig:
        if not self._enabled:
            raise RuntimeError("MCP is disabled")
        config = self._require_server(server_id)
        if not self._enabled_servers.get(server_id, not config.disabled):
            raise RuntimeError("MCP server is disabled")
        return config

    def _lock(self, server_id: str) -> asyncio.Lock:
        return self._locks.setdefault(server_id, asyncio.Lock())

    async def _notify_tools_changed(
        self,
        server_id: str,
        tools: tuple[MCPToolMetadata, ...],
    ) -> None:
        for listener in tuple(self._tools_changed_listeners):
            try:
                await listener(server_id, tools)
            except Exception as exc:
                logger.warning(
                    "mcp_tools_changed_listener_failed server=%s category=%s",
                    server_id,
                    type(exc).__name__,
                )
