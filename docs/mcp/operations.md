# MCP 运维

普通用户可查看：`/ai mcp list`、`show`、`status`、`tools` 和 `search`。SUPERUSERS 可执行
`refresh`、`reconnect`、`enable`、`disable`、`doctor` 和确定性 `call`。诊断调用绕过
Capability Search 的自然语言检索，但仍经过同一 Manager、结果归一化和结果预算。

`/healthz` 公开启用状态、配置/连接 Server 数、缓存工具数和活动调用数，不连接 lazy Server。
`/ai status` 还显示最近调用时间和最近错误类别。调用指标位于 `tool_invocations`，不保存参数、结果或
用户消息；`conversation_key` 只保存 SHA-256。

变更 `.mcp.json` 后执行 `/ai mcp refresh <server>` 或重启 Bot。只重建 Bot 可保留 NapCat 登录：

```bash
docker compose up -d --build bot
```
