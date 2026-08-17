# 能力检索与 Agent

MCP 工具进入统一 Capability 目录后，由 Capability Runtime 做本地 FTS5 BM25 检索，而不是前置 Planner 或 Flash Tool Selection。Agent 只能看到本轮已授权且已暴露的完整工具 Schema。

未知 namespace 或未授权工具不会进入候选。一次响应可调用多个 Provider 的工具，后续模型请求可使用整批结果继续调用。MCP 不具有额外固定调用上限，统一使用 Agent 和 Tooling 运行时配置。

图片、网页、真实管理员身份、DelegatedAuthority、重复修改和 TurnCoordinator 规则仍在统一能力边界执行，远程文本不能扩大权限。遗漏能力可通过 `request_tools` 在本轮已授权目录内补齐。
