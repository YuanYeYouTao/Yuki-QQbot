# Yuki 3.6.0 运行时架构

3.6.0 删除强制前置 Planner。普通聊天在本地 Conversation Runtime 准入后，由 Memory Runtime 与 Capability Runtime 完成本地准备，再进入同一个 Main Agent。旧 `qq_ai_bot.planner` 包、`ModelTask.PLANNER` / `TOOL_SELECTION`、`planner_runs` 表和 Plugin API 1.x 信号不再存在。

实施合同见 [重构总纲](yuki-3.6.0-refactor-plan/00-MASTER-YUKI-3.6.0-RUNTIME-REFACTOR.md)。从 3.5.3 升级见 [升级指南](../upgrade-3.6.0.md)。本文不填写未实测的回放延迟或 Token 数字。

## 主路径

```text
Event
  -> Conversation Runtime   准入、去重、账本、自主群本地评分
  -> Memory Runtime         会话、召回、mutation 门；不由模型选择人物/群
  -> Capability Runtime     本地 FTS5 BM25 搜索、权限收窄、Schema 预算
  -> Main Agent             唯一生成式聊天请求与有界工具循环
  -> Tool Kernel            执行、审计、Artifact
  -> Delivery               分段发送、回执、ReplyEffect（表情/语音）
```

普通无图纯文本进入 Main Agent 之前，没有生成式 Router / Planner 请求。Vision 分析、Memory Embedding 和发送后归因是独立网络调用，单独计数，不能算进「本地 Runtime 前置」。

私聊、真实 `@` 机器人、回复机器人由 Host 直接准入，不经过自主群评分。已启用群的未触发消息写入观察账本；静默窗口结束后，本地 `LocalAutonomousParticipationPolicy` 决定是否开一轮只读 Main Agent。

## 三个 Runtime

| Runtime | 负责 | 不负责 |
|---|---|---|
| Conversation | 消息准入、去重、自主群 debounce/评分、中断与 supersede、投递节奏 | 生成 `TurnPlan`、选择工具、选择记忆人物 |
| Memory | 身份硬过滤、召回、mutation 完成门、SELF / 群隔离 | 让模型填写 QQ/群号，或用自然语言关键词表当路由 |
| Capability | namespace 目录、本地搜索、origin/权限/effect 收窄、`request_tools` | Flash Tool Selection、插件自行声明 Trust |

Main Agent 仍然是唯一可以调用业务工具、写记忆和发送回复的执行者。需要复杂任务规划时，未来只能新增按需 Tool（例如 `agents/planning_specialist.py`），不能恢复前置 Planner 节点。

## 工具与搜索

工具身份是 semantic namespace，不是 Planner scope。Capability Search 使用进程内 SQLite FTS5 BM25；热路径不得重建索引。首批暴露受 Schema Token 与工具数量预算约束，遗漏项可通过 `request_tools` 在**本轮已授权目录**内补齐，不能扩大权限。

`MCP_TOOL_SELECTION_MODE` 及 Planner/Tool Selection 同义环境键已删除，不映射到新配置。

## 观测

插件与运行时通知不再使用 `planner.*` 事件。映射见 [Plugin API 2.0 迁移](../plugin-development/api-2.0-migration.md)。事件 payload 只含 origin、scope、hash、分数、原因码、工具 id 和延迟，不含聊天正文、Tool arguments 或 Memory refs。

`0037` 起用 `runtime_turn_id` 关联模型/工具/记忆回执。`0040` 删除 `planner_runs`。基线导出器在 3.6 数据库上把该表记为可选 historical gap，不会 fail-fast。

## 配置与数据面

| 3.5.3 | 3.6.0 |
|---|---|
| `planner.group_enabled` | `conversation.autonomous_enabled` |
| `planner.group_debounce_seconds` | `conversation.autonomous_debounce_seconds` |
| `planner.reply_necessity_threshold` | `conversation.autonomous_admission_threshold` |
| `planner.max_pending_messages` | `conversation.autonomous_batch_limit` |
| `planner.recent_presence_window_seconds` | `conversation.autonomous_presence_window_seconds` |
| `planner.interrupt_autonomous_on_new_message` | `conversation.interrupt_autonomous_on_new_message` |
| `reply.plan_hard_max_messages` | `reply.hard_max_messages` |
| `speech.planner_enabled` | `speech.agent_effects_enabled` |
| `model_profiles` schema v2 + `planner` / `tool_selection` 路由 | schema v3；删除这两条路由前物化 `memory_attribution` |
| Alembic `0036` | `0037`–`0040`（head `0040`） |
| Plugin API `1.1` | Plugin API `2.0` |

哈希盐 `yuki-planner-v1` 与 cadence 回填 `source=migrated_planner` 是历史数据域，算法不得改写。

## 相关文档

- [从 3.5.3 升级到 3.6.0](../upgrade-3.6.0.md)
- [Plugin API 2.0 迁移](../plugin-development/api-2.0-migration.md)
- [AdmissionSignal](../plugin-development/admission-signals.md)
- [Tool Kernel](tool-kernel.md)
- [Memory V2](memory-v2.md)
