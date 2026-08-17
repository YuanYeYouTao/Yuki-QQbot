# Plugin API 2.0 迁移

Plugin API `2.0` 是破坏性升级。Host 在导入插件代码前校验 `plugin_api` 主版本；声明 `1.0` 或 `1.1` 的插件会被拒绝，不会半加载。

当前 Host 产品版本仍以发布说明为准；不要用 Yuki 次版本号猜测 Plugin API。以 `plugin.toml` 的 `plugin_api`、`yuki_plugin_sdk.PLUGIN_API_VERSION` 和 `ctx.features` 为准。

## 必须修改

| 1.x | 2.0 |
|---|---|
| `plugin_api = "1.0"` / `"1.1"` | `plugin_api = "2.0"` |
| `PlannerSignal` / `PlannerSignalContext` / `PlannerSignalRegistration` | `AdmissionSignal` / `AdmissionSignalContext` / `AdmissionSignalRegistration` |
| `register_planner_signal(...)` | `register_admission_signal(...)` |
| `planner.signal.register` | `admission.signal.register` |
| `ctx.features.has("planner.signal.v1")` | `ctx.features.has("admission.signal.v1")` |
| `PromptTarget.PLANNER` / `BOTH` | `PromptTarget.AGENT` 或 `PLUGIN_SESSION` |
| `PromptStage.PLANNER_PLAN` | 删除；第三方只能注册 `plugin_context` / `tool_guidance` |
| 本地工具名作为全局身份 | `ToolMetadata.namespace`（可留空，Host 填 `plugin.{plugin_id}`）以及可选 `aliases` / `use_when` / `tags` |
| `planner.necessity_evaluated` / `planner.silent` | `turn.rejected` / `turn.autonomous_declined` |
| `planner.entered` / `planner.planned` | `turn.admitted` |
| `planner.interrupted` | 复用 `agent.interrupted` |
| `planner.fallback` | 删除；无同义事件 |
| （无） | `capability.searched`、`turn.closed` |

Alembic `0038` 会撤销旧 `planner.signal.register` 批准。Manifest、权限、入口或 API 版本变化后，插件进入 `pending_approval`，必须重新审阅，不能沿用旧批准。

## Manifest

```toml
plugin_api = "2.0"
yuki_requires = ">=3.5.3,<4.0"
```

`yuki_requires` 仍独立于 Plugin API。把上限写成 `<3.0` 的插件需要在确认兼容后改为 `<4.0`。

观测事件只携带 origin、scope、hash、分数、原因码、工具 id 和延迟；不含聊天正文、Tool arguments 或 Memory refs。`agent.starting` / `agent.finished` / `agent.interrupted` 与 `reply.sent` / `reply.failed` / `reply.cancelled` 继续复用，没有同义的 `agent.started` / `agent.completed` / `turn.delivered`。

## AdmissionSignal

群自主插话评分可以接收有界加分/减分；私聊、@、回复机器人仍由 Host 直接准入，插件不能 veto。信号不能改权限、不能改工具排序、不能驱动发送。完整规则见 [AdmissionSignal](admission-signals.md)。

## 工具元数据

```python
ToolMetadata(
    name="roll_dice",
    description="掷一个指定面数的骰子。",
    namespace="",  # 空值由 Host 填默认 plugin.{plugin_id}
    aliases=("dice",),
    use_when=("用户要求掷骰或随机整数时",),
    tags=("game",),
    ...
)
```

- `namespace` 必须是合法点分 id；不能使用 `kernel` / `memory` / `core` / `yuki` 等保留前缀。
- `aliases` / `tags` 最多 8 个、小写、不重复。
- `use_when` 最多 8 条、每条 1–200 字符。
- Provider / Trust 由 Host 决定，插件不能自行声明成核心能力。

## 升级步骤

1. 把 `plugin.toml` 的 `plugin_api` 改为 `"2.0"`，权限字符串改成 2.0 目录。
2. 删除 `register_planner_signal` 及相关 1.x 类型。
3. 需要自主群提示时改为 `register_admission_signal`。
4. Prompt `target` 只保留 `agent` 或 `plugin_session`。
5. 为工具补 namespace/aliases/use_when/tags（可选，但名称冲突由 Host 拒绝）。
6. `uv run qq-ai-bot-cli plugin validate <path>`，再 `plugin test`。
7. 部署后重新 `discover` / 审阅 / `approve` / `enable`，或通过 Guided Setup 的 pending 一次应用。
