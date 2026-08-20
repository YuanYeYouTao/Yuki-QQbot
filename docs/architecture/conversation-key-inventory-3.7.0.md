# Yuki 3.7.0 会话键与账本写入清单

本清单记录 3.7.0 切换时对携带 `conversation_key`、scope 身份或其哈希的持久化数据逐项作出的决定。它不是字符串替换清单；ConversationScope、Memory V2 分区、独立插件会话和历史审计关联具有不同语义。

## 键族决定

| 表或组件 | 3.7.0 策略 | 说明 |
|---|---|---|
| `conversation_scopes`、`conversation_rollups`、`conversation_rollup_jobs` | 当前运行时 Scope | 唯一使用 Bot-aware `ConversationScope.key`；群为 Bot+group，私聊为 Bot+peer。 |
| `processed_events.event_key` | 当前运行时幂等 | admission hash 由 Bot-aware Scope 构造；`chat_events` 唯一约束仍是最终事实，缺账本时允许修复。 |
| `relationship_jobs.conversation_key` | 当前运行时关联 | 新任务写入当前 `scope.key`；人物关系主体仍由独立 `user_id` 表达。 |
| `web_search_runs.conversation_key` | 当前运行时关联 | 新运行写入当前 `scope.key`，用于同一 Bot-aware 会话内的来源展示；隐私删除同时清理新键与历史 legacy 键。 |
| `admin_operation_events.conversation_key` | 历史审计 | 新记录可保存当前 `scope.key`，既有 legacy 字符串不迁移，也不得用于 Scope 历史查询。 |
| `memory_mutation_receipts.conversation_key` | 历史审计 / Memory provenance | 保留触发时关联字符串；不参与 ConversationScope 查询或 Rollup。 |
| `memory_jobs.conversation_key` | Memory V2 分区 | 保持 person、person-in-group、group、self 的现有调度语义；不得替换为 ConversationScopeKey。 |
| `memory_recall_receipts.conversation_hash` | Memory V2 诊断 | 仅是无正文、有限保留的 Memory recall 关联哈希，不是 Scope 身份。 |
| `memory_tool_receipts.conversation_key_hash` | Memory V2 evidence | 保持 Memory SELF reflection 的证据分组语义，不用于短期历史选择。 |
| `memory_self_reflection_* .conversation_key_hash` | Memory V2 分区 | 保留既有人物/群/私聊反思游标语义，并由 `bot_user_id` 单独隔离；不改成 Scope repository key。 |
| `tool_invocations.conversation_key_hash`、`runtime_turn_observations.conversation_key_hash` | 历史可观测性 | 只保存不可逆低敏关联哈希；不读取为当前 Scope，不作为公共指标 label。 |
| `plugin_agent_sessions.scope_type/scope_id` | 独立插件状态 | 插件自有 Agent session，不属于主会话账本，保持独立。 |
| `plugin_notification_outbox`、`plugin_background_turn_jobs` | 外部投递状态 | 目标由 `bot_user_id + target_type + target_id` 表达；进入主账本和主 Agent 前统一解析成 ConversationScope。 |
| `automations`、`automation_runs`、`automation_step_runs` | 独立业务状态 | 所有权、目标和执行审计保持独立；向主账本投递时统一解析 Scope，授权主体不充当群聊 Actor。 |
| plugin/config scope、人物/群组/membership、模型调用记录 | 无会话键迁移 | 分别是插件配置、身份资料或内容无关调用审计，不参与短期会话边界。 |

`ConversationScopeKey` 与 `TurnCoordinationKey` 在含义上相同但保持强类型边界；`MemoryPartitionKey` 继续由 Memory runtime 解析。Scope repository 只接受 `ConversationScope`，Memory repository 继续接受其原有分区类型。

## 主账本写入点迁移

运行时唯一写入口是 `ScopedEventLedgerUnitOfWork`。迁移结果如下：

| 来源 | 入口 |
|---|---|
| 普通入站、命令入站与 `/ai new` 边界 | `append_inbound()` / `append_new_generation_command()` |
| 已获得平台回执的文本、语音、图片和工具 outbound | `append_outbound()` |
| 插件通知、notification delivery、插件 background turn | scoped append |
| 自动化投递与自动化 Agent 输出 | scoped append |
| agent tool、facade 与外部事件 | scoped append |
| visual summary 派生补写 | `set_visual_summary()` 特许路径，同时维护精确字符差值和 job signal |
| migration、fixture、隐私删除/脱敏 | 明确特许路径，不属于普通运行时 append |

CI 的 AST 守卫扫描 `src/qq_ai_bot`：除 scoped UoW、ORM 模型声明、migration 和 fixture 外，出现 `ChatEventModel(...)` 或直接插入 `chat_events` 即失败。静态测试还验证旧会话符号在运行时代码和测试中为零。
