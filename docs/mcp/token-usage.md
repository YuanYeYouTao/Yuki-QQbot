# Token 使用

MCP Token 消耗主要来自实际注入主 Agent 的完整工具 Schema，而不是已配置 Server 数量。普通聊天
没有命中 MCP 工具时增加的 MCP Schema Token 为 **0**。Capability Search 只使用本地短描述做
检索，不会把完整 Schema 交给检索器。

可用 `TOOLING_SELECTED_TOOL_LIMIT`、`TOOLING_SCHEMA_TOKEN_BUDGET` 约束统一目录，使用
`MCP_SELECTED_TOOL_LIMIT`、`MCP_SCHEMA_TOKEN_BUDGET` 单独约束 MCP 部分。留空表示不增加该项
限制。预算器只选择能完整容纳的 Schema，不截断或修改参数结构。

需要更多连续操作时提高 `AGENT_MAX_TOOL_CALLS` 和 `AGENT_MAX_MODEL_REQUESTS`，并同步评估模型
费用与延迟。
