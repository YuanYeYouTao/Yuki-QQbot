"""Project selected MCP tools into the persistent automation capability registry."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from jsonschema import ValidationError as JSONSchemaValidationError
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.executor import AutomationExecutionError
from qq_ai_bot.automation.models import RetryPolicy, RiskClass, TurnOrigin
from qq_ai_bot.automation.registry import (
    AutomationCapability,
    AutomationCapabilityRegistry,
    CapabilityExecutionContext,
    CapabilityResult,
)
from qq_ai_bot.capabilities.invocation import ToolInvocationContext
from qq_ai_bot.capabilities.models import CapabilityRisk
from qq_ai_bot.capabilities.results import ToolResultBudgeter
from qq_ai_bot.mcp.binding import MCPPolicyRuntime, MCPToolBinding
from qq_ai_bot.mcp.descriptors import host_annotation_policy
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.models import MCPToolMetadata

logger = logging.getLogger(__name__)


class _MCPArguments(BaseModel):
    """Fallback Pydantic type; remote JSON Schema remains authoritative."""

    model_config = ConfigDict(extra="allow")


class MCPAutomationBridge:
    """Expose explicitly selected MCP tools to scheduled DSL and delegated Agents."""

    def __init__(
        self,
        *,
        manager: MCPManager,
        registry: AutomationCapabilityRegistry,
        result_budgeter: ToolResultBudgeter,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._result_budgeter = result_budgeter
        self._registered_by_server: dict[str, set[str]] = {}
        self._missing_by_server: dict[str, tuple[str, ...]] = {}

    @property
    def registered_tool_count(self) -> int:
        return sum(len(names) for names in self._registered_by_server.values())

    @property
    def missing_tool_count(self) -> int:
        return sum(len(names) for names in self._missing_by_server.values())

    async def start(self) -> None:
        self._manager.add_tools_changed_listener(self._tools_changed)
        for server_id in self._manager.configured_server_ids:
            config = self._manager.server_config(server_id)
            if config is None or not config.yuki.automation.enabled:
                continue
            try:
                tools = await self._manager.ensure_metadata(server_id)
            except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                tools = tuple(
                    tool for tool in self._manager.cached_tools if tool.server_id == server_id
                )
                logger.warning(
                    "mcp_automation_discovery_failed server=%s category=%s cached=%s",
                    server_id,
                    type(exc).__name__,
                    len(tools),
                )
            await self._replace_server(server_id, tools)

    async def close(self) -> None:
        self._manager.remove_tools_changed_listener(self._tools_changed)
        for names in self._registered_by_server.values():
            for name in names:
                self._registry.unregister(name)
        self._registered_by_server.clear()
        self._missing_by_server.clear()

    async def health(self) -> dict[str, object]:
        return {
            "registered_tools": self.registered_tool_count,
            "servers": len(self._registered_by_server),
            "missing_tools": {
                server_id: list(names)
                for server_id, names in sorted(self._missing_by_server.items())
                if names
            },
        }

    async def _tools_changed(
        self,
        server_id: str,
        tools: tuple[MCPToolMetadata, ...],
    ) -> None:
        await self._replace_server(server_id, tools)

    async def _replace_server(
        self,
        server_id: str,
        tools: tuple[MCPToolMetadata, ...],
    ) -> None:
        previous = self._registered_by_server.pop(server_id, set())
        for name in previous:
            self._registry.unregister(name)

        config = self._manager.server_config(server_id)
        if (
            config is None
            or not self._manager.server_enabled(server_id)
            or not config.yuki.automation.enabled
        ):
            self._missing_by_server.pop(server_id, None)
            return

        included = set(config.yuki.automation.include_tools)
        selected = tuple(tool for tool in tools if tool.remote_tool_name in included)
        available = {tool.remote_tool_name for tool in selected}
        missing = tuple(sorted(included - available))
        self._missing_by_server[server_id] = missing
        if missing:
            logger.warning(
                "mcp_automation_tools_missing server=%s count=%s",
                server_id,
                len(missing),
            )

        registered: set[str] = set()
        for tool in selected:
            name = capability_name(server_id, tool.remote_tool_name)
            if len(name) > 128:
                logger.warning(
                    "mcp_automation_tool_name_too_long server=%s tool=%s",
                    server_id,
                    tool.remote_tool_name,
                )
                continue
            try:
                definition = self._definition(tool)
                self._registry.register(definition)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "mcp_automation_tool_registration_failed server=%s tool=%s category=%s",
                    server_id,
                    tool.remote_tool_name,
                    type(exc).__name__,
                )
                continue
            registered.add(name)
        if registered:
            self._registered_by_server[server_id] = registered

    def _definition(self, tool: MCPToolMetadata) -> AutomationCapability:
        config = self._manager.server_config(tool.server_id)
        if config is None:
            raise ValueError("MCP server disappeared during automation registration")
        policy = host_annotation_policy(config.yuki.tool_annotations.get(tool.remote_tool_name))
        read_only = policy.read_only
        risk = (
            RiskClass.DESTRUCTIVE
            if policy.risk is CapabilityRisk.DESTRUCTIVE
            else RiskClass.READ
            if read_only
            else RiskClass.MUTATE
        )
        permission = (
            PermissionLevel.SUPERUSER
            if config.yuki.automation.permission == "superuser"
            else PermissionLevel.USER
        )
        schema = dict(tool.input_schema or {"type": "object"})
        validate = _json_schema_validator(schema)

        async def execute(
            arguments: dict[str, Any],
            context: CapabilityExecutionContext,
        ) -> CapabilityResult:
            runtime = MCPPolicyRuntime(
                origin=context.authority.origin,
                actor_user_id=context.authority.actor_user_id,
                actor_is_superuser=context.authority.actor_is_superuser,
            )
            result = await MCPToolBinding(
                self._manager,
                tool.server_id,
                tool.remote_tool_name,
                record_invocation=True,
            ).invoke(
                arguments,
                ToolInvocationContext(
                    runtime=runtime,
                    conversation_key=context.conversation_key,
                    actor_user_id=runtime.actor_user_id,
                    provider_metadata={"web_was_used": context.web_was_used},
                ),
            )
            if not result.ok:
                raise AutomationExecutionError(
                    result.error_code or "mcp_tool_failed",
                    transient=result.retryable and read_only,
                    uncertain=bool(result.mutation_committed),
                )
            rendered = await self._result_budgeter.render(result)
            try:
                payload: object = json.loads(rendered.text)
            except json.JSONDecodeError:
                payload = rendered.text
            data = payload if isinstance(payload, dict) else {"result": payload}
            return CapabilityResult(data=data)

        description = tool.description or tool.compact_description or tool.remote_tool_name
        return AutomationCapability(
            name=capability_name(tool.server_id, tool.remote_tool_name),
            description=f"MCP {tool.server_id}：{description}",
            argument_model=_MCPArguments,
            argument_schema=schema,
            argument_validator=validate,
            output_schema=dict(tool.output_schema or {"type": "object"}),
            required_permission=permission,
            risk_class=risk,
            retry_policy=(RetryPolicy.TRANSIENT_ONCE if read_only else RetryPolicy.NONE),
            allowed_origins=frozenset({TurnOrigin.SCHEDULED_AUTOMATION, TurnOrigin.SYSTEM_TASK}),
            schema_version=tool.metadata_hash,
            handler=execute,
        )


def capability_name(server_id: str, remote_tool_name: str) -> str:
    return f"mcp.{server_id}.{remote_tool_name}"


def _json_schema_validator(
    schema: dict[str, object],
) -> Callable[[object, bool], dict[str, Any]]:
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator = validator_class(schema)
    relaxed_schema = _template_relaxed_schema(schema)
    relaxed_validator_class = validator_for(relaxed_schema)
    relaxed_validator_class.check_schema(relaxed_schema)
    relaxed_validator = relaxed_validator_class(relaxed_schema)

    def validate(value: object, allow_templates: bool) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("MCP 工具参数必须是 JSON 对象")
        try:
            selected_validator = (
                relaxed_validator if allow_templates and _contains_template(value) else validator
            )
            selected_validator.validate(value)
        except JSONSchemaValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path)
            location = f"（{path}）" if path else ""
            raise ValueError(f"MCP JSON Schema 校验失败{location}：{exc.message}") from exc
        return dict(value)

    return validate


def _template_relaxed_schema(schema: dict[str, object]) -> dict[str, object]:
    """Allow template strings at value positions while preserving object structure."""

    mapped_keywords = {
        "$defs",
        "definitions",
        "dependentSchemas",
        "patternProperties",
        "properties",
    }
    single_keywords = {
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedProperties",
    }
    sequence_keywords = {"allOf", "anyOf", "oneOf", "prefixItems"}
    transformed: dict[str, object] = {}
    for key, value in schema.items():
        if key in mapped_keywords and isinstance(value, dict):
            transformed[key] = {
                str(name): _template_relaxed_schema(child) if isinstance(child, dict) else child
                for name, child in value.items()
            }
        elif key in single_keywords and isinstance(value, dict):
            transformed[key] = _template_relaxed_schema(value)
        elif key in sequence_keywords and isinstance(value, list):
            transformed[key] = [
                _template_relaxed_schema(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            transformed[key] = value
    return {
        "anyOf": [
            transformed,
            {
                "type": "string",
                "pattern": r"^(?:\$[A-Za-z_][A-Za-z0-9_]*|\$\{[^{}]+\}|.*\$\{[^{}]+\}.*)$",
            },
        ]
    }


def _contains_template(value: object) -> bool:
    if isinstance(value, str):
        return value.startswith("$") or "${" in value
    if isinstance(value, dict):
        return any(_contains_template(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_template(item) for item in value)
    return False
