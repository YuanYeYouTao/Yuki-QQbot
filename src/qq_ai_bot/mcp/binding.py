"""Tool Kernel binding for one remote MCP tool."""

from __future__ import annotations

from dataclasses import dataclass, replace

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.invocation import ToolInvocationContext
from qq_ai_bot.capabilities.models import AuthorityContext
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext, CapabilityPolicyEngine
from qq_ai_bot.capabilities.results import ToolExecutionResult, resolve_mutation_commit
from qq_ai_bot.mcp.descriptors import descriptor_from_mcp_tool
from qq_ai_bot.mcp.errors import classify_mcp_exception
from qq_ai_bot.mcp.manager import MCPManager


@dataclass(frozen=True, slots=True)
class MCPPolicyRuntime:
    """Minimal trusted runtime for non-chat MCP callers."""

    origin: TurnOrigin
    actor_user_id: str
    actor_is_superuser: bool
    tools_closed: bool = False
    read_only: bool = False


def mcp_policy_context(context: ToolInvocationContext) -> CapabilityPolicyContext:
    """Build the current Host policy context; Planner tool selection is not read."""

    runtime = context.runtime
    metadata = context.provider_metadata or {}
    return CapabilityPolicyContext(
        authority=AuthorityContext(
            actor_user_id=(getattr(runtime, "actor_user_id", "") or context.actor_user_id),
            is_superuser=bool(getattr(runtime, "actor_is_superuser", False)),
        ),
        origin=getattr(runtime, "origin", TurnOrigin.USER_MESSAGE),
        contains_images=bool(metadata.get("contains_images", False)),
        web_was_used=bool(metadata.get("web_was_used", False)),
        tools_closed=bool(getattr(runtime, "tools_closed", False)),
        read_only=bool(getattr(runtime, "read_only", False)),
        memory_view=getattr(runtime, "memory_view", None),
    )


@dataclass(frozen=True, slots=True)
class MCPToolBinding:
    manager: MCPManager
    server_id: str
    remote_tool_name: str
    record_invocation: bool = False

    async def invoke(
        self,
        arguments: dict[str, object],
        context: ToolInvocationContext,
    ) -> ToolExecutionResult:
        try:
            metadata = await self.manager.resolve_tool(self.server_id, self.remote_tool_name)
            descriptor = descriptor_from_mcp_tool(self.manager, metadata)
        except ValueError:
            return _denied_result(
                self.server_id,
                self.remote_tool_name,
                error_code="unknown_mcp_tool",
                public_message="未找到当前可用的 MCP 工具",
            )
        except Exception as exc:
            failure = classify_mcp_exception(exc)
            return ToolExecutionResult(
                ok=False,
                error_code=failure.code,
                public_message=failure.public_message,
                retryable=failure.retryable,
                mutation_committed=False,
                provider_id=f"mcp.{self.server_id}",
                tool_name=self.remote_tool_name,
            )
        visible = CapabilityPolicyEngine().visible((descriptor,), mcp_policy_context(context))
        if not visible:
            return _denied_result(
                self.server_id,
                self.remote_tool_name,
                error_code="mcp_tool_policy_denied",
                public_message="当前轮次策略不允许调用该 MCP 工具",
            )
        result = await self.manager._call_resolved_tool(
            metadata,
            arguments,
            conversation_key=context.conversation_key,
            record_invocation=self.record_invocation,
        )
        mutation_committed = resolve_mutation_commit(result, descriptor)
        return replace(
            result,
            mutation_committed=mutation_committed,
            finalize_after_commit=(
                True
                if mutation_committed and descriptor.finalize_after_commit
                else result.finalize_after_commit
            ),
        )


def _denied_result(
    server_id: str,
    tool_name: str,
    *,
    error_code: str,
    public_message: str,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=False,
        error_code=error_code,
        public_message=public_message,
        mutation_committed=False,
        provider_id=f"mcp.{server_id}",
        tool_name=tool_name,
    )
