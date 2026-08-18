# MCP 故障排查

- `当前没有配置`：确认 `MCP_CONFIG_PATH` 指向存在的 UTF-8 JSON，并检查 `mcpServers` 拼写
- `missing environment variable`：为 `${NAME}` 配置环境变量，不要把 Secret 直接写进文件
- `disabled`：检查全局 `MCP_ENABLED`、运行时 `mcp.enabled` 和 Server enable 状态
- `failed/timeout`：运行 `/ai mcp doctor <server>`，检查本地命令、cwd、网络和超时
- 工具列表为空：执行 `refresh`；确认 includeTools/excludeTools 没有过滤目标
- 普通聊天看不到工具：检查 Capability Search 是否命中对应 namespace，以及 Schema/数量预算
- 结果截断：根据返回的 `artifact_handle` 让 Agent 调用 `read_tool_artifact`
- 两个同名工具：模型名包含 Server ID，不应手工改成相同名称

状态和日志只显示脱敏错误类别。需要检查远程原始协议时，应在受控环境直接调试 MCP Server，不要让
Yuki 把 Header、Cookie 或完整返回值写入普通日志。
