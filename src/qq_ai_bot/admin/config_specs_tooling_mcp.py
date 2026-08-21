"""Unified Tool Kernel and MCP runtime configuration declarations."""

from __future__ import annotations

from qq_ai_bot.admin.config_spec_helpers import _G, _field, _spec
from qq_ai_bot.admin.models import ConfigSpec


def tooling_mcp_config_specs() -> tuple[ConfigSpec, ...]:
    return (
        _spec(
            "tooling.max_parallel_calls",
            "工具并发数",
            "同一 Agent 工具批次允许并发执行的 parallel_safe 工具数。",
            env_alias="TOOLING_MAX_PARALLEL_CALLS",
            getter=_field("tooling_max_parallel_calls"),
            settings_fields=("tooling_max_parallel_calls",),
            category="tooling",
            value_type="integer",
            minimum=1,
            scopes=_G,
        ),
        _spec(
            "tooling.first_round_hard_cap",
            "首轮工具硬顶",
            "主会话首轮 tools[] 个数硬顶，含 request_tools。改大只留余量。",
            env_alias="TOOLING_FIRST_ROUND_HARD_CAP",
            getter=_field("tooling_first_round_hard_cap"),
            settings_fields=("tooling_first_round_hard_cap",),
            category="tooling",
            value_type="integer",
            minimum=1,
            scopes=_G,
        ),
        _spec(
            "tooling.first_round_pin_ids",
            "首轮钉定工具",
            "部署级首轮 capability id，逗号分隔。不跟当前句子或 origin 变化。",
            env_alias="TOOLING_FIRST_ROUND_PIN_IDS",
            getter=_field("tooling_first_round_pin_ids_csv"),
            settings_fields=("tooling_first_round_pin_ids_csv",),
            category="tooling",
            value_type="string",
            scopes=_G,
        ),
        *(
            _spec(
                f"tooling.{name}",
                display,
                description,
                env_alias=f"TOOLING_{name.upper()}",
                getter=_field(f"tooling_{name}"),
                settings_fields=(f"tooling_{name}",),
                category="tooling",
                value_type="integer",
                minimum=1,
                scopes=_G,
            )
            for name, display, description in (
                (
                    "selected_tool_limit",
                    "工具选择数量预算",
                    "首批工具的宽松数量预算；为空时不额外限制，遗漏工具仍可按需加载。",
                ),
                (
                    "schema_token_budget",
                    "工具 Schema 预算",
                    "首批完整 JSON Schema 的宽松 Token 预算；为空时不额外限制。",
                ),
                ("result_token_budget", "工具结果 Token 预算", "为空时不额外限制统一工具结果。"),
                ("result_item_limit", "工具结果条目预算", "为空时不额外限制结构化结果条目。"),
            )
        ),
        _spec(
            "tooling.result_artifact_enabled",
            "超长结果 Artifact",
            "是否把超出模型预算的完整工具结果写入短期 Artifact。",
            value_type="boolean",
            scopes=_G,
            env_alias="TOOLING_RESULT_ARTIFACT_ENABLED",
            getter=_field("tooling_result_artifact_enabled"),
            settings_fields=("tooling_result_artifact_enabled",),
            category="tooling",
        ),
        _spec(
            "tooling.result_artifact_retention_seconds",
            "Artifact 保留时间",
            "完整工具结果 Artifact 的保留秒数。",
            env_alias="TOOLING_RESULT_ARTIFACT_RETENTION_SECONDS",
            getter=_field("tooling_result_artifact_retention_seconds"),
            settings_fields=("tooling_result_artifact_retention_seconds",),
            category="tooling",
            value_type="integer",
            minimum=1,
            scopes=_G,
        ),
        _spec(
            "mcp.enabled",
            "MCP 总开关",
            "是否向后续新会话开放已配置 MCP Server。",
            value_type="boolean",
            scopes=_G,
            env_alias="MCP_ENABLED",
            getter=_field("mcp_enabled"),
            settings_fields=("mcp_enabled",),
            category="mcp",
        ),
        _spec(
            "mcp.gateway_enabled",
            "MCP Gateway",
            "是否提供 MCP 目录搜索、描述和调用网关。",
            value_type="boolean",
            scopes=_G,
            env_alias="MCP_GATEWAY_ENABLED",
            getter=_field("mcp_gateway_enabled"),
            settings_fields=("mcp_gateway_enabled",),
            category="mcp",
        ),
        *(
            _spec(
                f"mcp.{name}",
                display,
                description,
                value_type=value_type,
                minimum=minimum,
                scopes=_G,
                env_alias=f"MCP_{name.upper()}",
                getter=_field(f"mcp_{name}"),
                settings_fields=(f"mcp_{name}",),
                category="mcp",
            )
            for name, display, description, value_type, minimum in (
                (
                    "metadata_cache_ttl_seconds",
                    "MCP 元数据缓存",
                    "tools/list 元数据缓存秒数。",
                    "integer",
                    1,
                ),
                (
                    "connect_timeout_seconds",
                    "MCP 连接超时",
                    "初始化 MCP 会话的超时秒数。",
                    "number",
                    0.1,
                ),
                (
                    "request_timeout_seconds",
                    "MCP 请求超时",
                    "单次 MCP 请求的超时秒数。",
                    "number",
                    0.1,
                ),
                (
                    "selected_tool_limit",
                    "MCP 工具数量预算",
                    "首批 MCP 工具的宽松数量预算；为空时不额外限制。",
                    "integer",
                    1,
                ),
                (
                    "schema_token_budget",
                    "MCP Schema 预算",
                    "首批 MCP Schema 的宽松 Token 预算；为空时不额外限制。",
                    "integer",
                    1,
                ),
                (
                    "result_token_budget",
                    "MCP 结果预算",
                    "为空时不额外限制 MCP 结果。",
                    "integer",
                    1,
                ),
                (
                    "result_item_limit",
                    "MCP 结果条目预算",
                    "为空时不额外限制结果条目。",
                    "integer",
                    1,
                ),
                (
                    "max_parallel_calls",
                    "MCP 并发数",
                    "MCP 工具的全局并发调用数。",
                    "integer",
                    1,
                ),
                (
                    "artifact_retention_seconds",
                    "MCP Artifact 保留",
                    "MCP 超长结果 Artifact 保留秒数。",
                    "integer",
                    1,
                ),
            )
        ),
    )
