"""Policy-preserving catalog gateway for large MCP tool sets."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from qq_ai_bot.capabilities.invocation import ToolInvocationContext
from qq_ai_bot.capabilities.models import CapabilityDescriptor
from qq_ai_bot.capabilities.policy import CapabilityPolicyEngine
from qq_ai_bot.capabilities.results import (
    ToolExecutionResult,
    resolve_mutation_commit,
)
from qq_ai_bot.mcp.binding import mcp_policy_context
from qq_ai_bot.mcp.descriptors import descriptor_from_mcp_tool
from qq_ai_bot.mcp.manager import MCPManager


@dataclass(slots=True)
class MCPGatewayBinding:
    """Search and describe freely, but execute only resolved policy-visible tools."""

    manager: MCPManager
    policy: CapabilityPolicyEngine = field(default_factory=CapabilityPolicyEngine)
    _described: set[tuple[str, str, str, str]] = field(default_factory=set)

    async def invoke(
        self,
        arguments: dict[str, object],
        context: ToolInvocationContext,
    ) -> ToolExecutionResult:
        operation = _operation(arguments)
        server_id = str(arguments.get("server", arguments.get("server_id", "")))
        if operation == "search":
            query = str(arguments.get("search", arguments.get("query", "")))
            items = self.manager.search_tools(query, server_id=server_id or None)
            return _result(
                ok=True,
                data=[
                    {
                        "server_id": item.server_id,
                        "tool_name": item.remote_tool_name,
                        "description": item.compact_description,
                    }
                    for item in items
                ],
            )

        tool_name = str(
            arguments.get("tool", arguments.get("tool_name", arguments.get("describe", "")))
        )
        if not server_id or not tool_name:
            return _result(
                ok=False,
                error_code="invalid_arguments",
                public_message="MCP server 和 tool 都不能为空",
            )
        try:
            metadata = await self.manager.resolve_tool(server_id, tool_name)
            descriptor = descriptor_from_mcp_tool(self.manager, metadata)
        except (OSError, RuntimeError, TimeoutError, ValueError):
            return _result(
                ok=False,
                error_code="unknown_mcp_tool",
                public_message="未找到可用的 MCP 工具",
            )

        if operation == "describe":
            if not self._policy_allows(context, descriptor):
                return _result(
                    ok=False,
                    error_code="mcp_tool_policy_denied",
                    public_message="当前轮次策略不允许查看该 MCP 工具",
                )
            self._remember(context, descriptor)
            return _result(ok=True, data=metadata.model_dump(mode="json"))

        if operation == "call":
            raw_arguments = arguments.get("arguments", {})
            if not isinstance(raw_arguments, dict):
                return _result(
                    ok=False,
                    error_code="invalid_arguments",
                    public_message="MCP arguments 必须是对象",
                )
            if not self._selected(context, descriptor):
                return _result(
                    ok=False,
                    error_code="mcp_tool_not_selected",
                    public_message="目标 MCP 工具尚未在本轮选择或查看",
                )
            if not self._policy_allows(context, descriptor):
                return _result(
                    ok=False,
                    error_code="mcp_tool_policy_denied",
                    public_message="当前轮次策略不允许调用该 MCP 工具",
                )
            binding = descriptor.binding
            if binding is None:
                return _result(
                    ok=False,
                    error_code="mcp_tool_unbound",
                    public_message="目标 MCP 工具当前不可执行",
                )
            outcome = await binding.invoke(
                {str(key): value for key, value in raw_arguments.items()},
                context,
            )
            return replace(
                outcome,
                mutation_committed=resolve_mutation_commit(outcome, descriptor),
            )

        return _result(
            ok=False,
            error_code="unknown_gateway_operation",
            public_message="未知 MCP gateway 操作",
        )

    def target_descriptor(self, arguments: dict[str, object]) -> CapabilityDescriptor | None:
        """Resolve cached target metadata for duplicate-mutation coordination."""

        if _operation(arguments) != "call":
            return None
        server_id = str(arguments.get("server", arguments.get("server_id", "")))
        tool_name = str(arguments.get("tool", arguments.get("tool_name", "")))
        metadata = self.manager.describe_tool(server_id, tool_name)
        if metadata is None or not self.manager.server_enabled(server_id):
            return None
        return descriptor_from_mcp_tool(self.manager, metadata)

    def _remember(
        self,
        context: ToolInvocationContext,
        descriptor: CapabilityDescriptor,
    ) -> None:
        if len(self._described) >= 2048:
            self._described.clear()
        self._described.add(
            (
                context.conversation_key,
                context.trigger_message_id,
                descriptor.provider_id,
                descriptor.provider_tool_name,
            )
        )

    def _selected(
        self,
        context: ToolInvocationContext,
        descriptor: CapabilityDescriptor,
    ) -> bool:
        return (
            context.conversation_key,
            context.trigger_message_id,
            descriptor.provider_id,
            descriptor.provider_tool_name,
        ) in self._described

    def _policy_allows(
        self,
        context: ToolInvocationContext,
        descriptor: CapabilityDescriptor,
    ) -> bool:
        return bool(self.policy.visible((descriptor,), mcp_policy_context(context)))


def _operation(arguments: dict[str, object]) -> str:
    operation = str(arguments.get("operation", ""))
    if operation:
        return operation
    if arguments.get("tool") or arguments.get("tool_name"):
        return "call"
    if arguments.get("describe"):
        return "describe"
    return "search"


def _result(
    *,
    ok: bool,
    data: object = None,
    error_code: str | None = None,
    public_message: str | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=ok,
        data=data,
        error_code=error_code,
        public_message=public_message,
        mutation_committed=False,
        provider_id="mcp.gateway",
        tool_name="mcp_gateway",
    )
