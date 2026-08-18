# 工具发现与选择

`tools/list` 结果会转为稳定的 MCP metadata 并缓存。配置哈希一致且 TTL 有效时，重启后可直接用于
目录检索；哈希变化后旧缓存不用于执行，下一次连接重新发现。

Capability Runtime 用进程内 SQLite FTS5 BM25 从统一目录做本地检索。没有
`all` / `catalog` / `hybrid` / `gateway` 选择模式，也没有 `ModelTask.TOOL_SELECTION` 或
Flash 精排。已缓存的 MCP 工具进入统一目录；`mcp_gateway` 描述符只在 Server 启用 gateway
时加入。

Agent 只能看到本轮已授权且已暴露的完整工具 Schema。首批暴露受 Schema Token 与工具数量预算
约束。遗漏项可通过 `request_tools` 在**本轮已授权目录**内补齐，不能扩大权限。两个 Server
的同名工具通过 Server ID 隔离。
