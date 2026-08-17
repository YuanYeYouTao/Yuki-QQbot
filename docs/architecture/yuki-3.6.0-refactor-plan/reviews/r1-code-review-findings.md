# R1 开工前代码审阅结论（r1-code-review-findings）

> 基线：`main@2695484`（Yuki `3.5.3`，Alembic head `0036`）。
> 审阅范围：R1 任务书第 2 节列出的 11 个文件，外加 `model_runtime/repository.py`、
> `mcp/repository.py`、`memory/receipt.py`、`planner/repository.py`、
> `services/autonomous_groups.py`、`plugin_host/background_turns.py`、
> `application/modules/persistence.py` 等观测/装配落点。

---

## 1. 任务书七问的核对结论

### 1.1 `ApplicationContainer` 资源所有权与启动/关闭顺序

- Container 构造期按固定顺序 build 模块 bundle：Persistence → MCP → ModelRuntime →
  Web → Media → Emoji → Speech → Conversation → Admin → Automation → Plugin，
  随后组装 `AutonomousGroupService`、`CommandService`、`MessageProcessor`
  （`container.py:88-491`）。
- 生命周期由 `LifecycleRegistry` 独占：注册顺序启动、精确逆序关闭；启动失败时对
  已启动条目做回滚关闭，失败聚合为 `ExceptionGroup`（`application/lifecycle.py:44-69`）。
  R1 §9 要求的 partial-start rollback 与逆序关闭**已经存在**，Bundle 只需接入，不需重建。
- 周期清理集中在 `container._cleanup_loop()`（processed events / web sources /
  media / emoji / automation runs / plugin state / artifacts / speech cache）。
  `runtime_turn_observations` 的 30 天 retention 清理挂在该循环是最小侵入点。

### 1.2 `ConversationTurnCoordinator` 语义

- `TurnToken = (conversation_key, version, origin, created_at)`；分区键为
  `group:{g}` / `private:{u}`（`turn_coordinator.py:72-78`），与历史 identity
  `group:{g}:user:{u}`（`domain/conversations.py:51-54`）**不是同一键**——证实任务书
  “三种分区不得混用”的批注。
- `notify_message`：版本自增；reply 阶段按策略取消；planner/generation 仅当前一
  origin ∈ {AUTONOMOUS_GROUP, PLUGIN_BACKGROUND} 且 `mutation_started=False` 才被打断。
  `observation=True` 且直连轮受 `protected_version` 保护时不推进版本。
- `begin_autonomous` 保持同版本仅改可信 origin；`begin_background` 仅空闲时以新版本
  进场且从不取消用户工作；`mark_mutation_started` 即 mutation shield；
  `cancel_interruptible` 服务 `/ai stop`。
- 结论：Coordinator 已经是 token/version/cancellation/mutation shield 唯一权威。
  R1 的 `TurnState` 不复制这些字段，只在 `TurnContext.turn_token` 引用。

### 1.3 四类入口的共享点与差异

| 维度 | User Message | Autonomous Group | Scheduled Automation | Plugin Background |
|---|---|---|---|---|
| 准入 | `evaluate_message` + 策略/限流/去重（processor） | 观察队列 + debounce（autonomous_groups） | worker 领取到期任务 | outbox job claim + `begin_background` |
| Planner | 是（可 WAIT 一次重规划） | 是（可 WAIT 一次） | 否（脚本步骤） | 是（tool-free） |
| 交付 | `chat.respond` + sender | `chat.respond(autonomous=True)` | 自动化步骤消息 | `chat.respond` 变体 |
| Memory 入队 | 是（非命令轮） | 否（消息在观察时已入队） | 否 | 否 |
| 共同点 | runtime snapshot、coordinator、planner service、chat service、ledger | 同左 | coordinator + delegated authority | 同左 |

差异证实 TurnTrigger 判别联合（Message/External/Scheduled/PluginSession）而不是
synthetic inbound 是正确形状；`InboundMessage` 只在真实消息轮存在。

### 1.4 `ToolRuntime` / `AgentRuntime` / `PlannedTurn` 的真实使用者

- `AgentRuntime`（`services/agent_runner.py:52-67`）：AgentRunner 主循环、各
  ToolBackend（chat/_ChatAgentBackend、plugin、automation）消费；字段混合了
  可信事实（origin/actor/delegated_authority/runtime_config/current_time）与
  预算（max_tool_calls/max_model_requests）以及 web 路由状态。
- `PlannedTurn`（planner/models.py）：`chat.respond` 消费 delivery/voice/emoji/tool
  字段；`processor.handle` 消费 `plan.decision` 与 `plan.voice`（偏好写库）；
  `planner_tool_scopes` 把 Planner 的 scope 选择投影进工具曝光。
- 结论：R1 新类型不复制 PlannedTurn 字段；`TurnAuthority/TurnSceneFacts/TurnTaintState`
  吸收 AgentRuntime 中的可信部分，预算/web 路由留在 Runner 层（R4 迁移）。

### 1.5 可信 Runtime 状态 vs Planner 输出

- 可信：coordinator token/version、runtime config snapshot、ledger 记录、
  vision `VisualObservation`（后端产物）、`TimeContext`、superuser 判定
  （`settings.superusers`）、delegated authority 重验（`automation/authority.py:49-82`）。
- Planner 输出（模型产物，不可信为权威）：decision/wait/delivery_mode/desired_messages/
  tool_selection/voice/emoji/intent/confidence。3.5.3 中 tool_selection 实际影响
  工具曝光範圍，属于“模型影响权限面”的历史债，R3 的 authority-first 将其移除。
- R1 落点：`TurnAuthority` 冻结 dataclass 仅由 Host 工厂构造；`normalized_content`
  以 `UntrustedContent` 包装，类型上隔离出 Authority。

### 1.6 依赖 Planner 事件名称的生命周期 Hook

- `PlannerService.set_event_publisher`（processor 传入）发布 planner 相关插件事件；
  `PlannerSignalProvider.collect`（plugin_planner_signals）是 Plugin SDK 1.x 的
  Planner 信号面；`container.plugin_planner_signals` 注入 processor 与 autonomous。
- `yuki_plugin_sdk.events.EventName` 中存在 Planner 生命周期事件名；R5 随
  Plugin API 2.0 删除。R1 不动这些 hook，仅保证新 runtime 包不 import 它们。

### 1.7 现有观测表能否记录基线

| 表 | 现状 | 缺口 |
|---|---|---|
| `planner_runs` | 决策/延迟/necessity/voice 元数据齐全 | 无整轮 turn id |
| `model_invocations` | task/profile/token/延迟（`0018`） | 无 turn/conversation 关联 |
| `tool_invocations` | conversation hash + provider/tool/延迟 | 无 turn id |
| `memory_recall_receipts` | 有自己的 `turn_id`（每 Receipt 唯一 uuid） | 该列是 receipt 级 id，不是整轮 id |
| 端到端延迟 | 仅 `message_handled` 日志 | 无表 |
| 首轮命中/request_tools | `ToolKernelMetrics` 进程内 Counter | 重启即丢 |

结论与任务书一致：没有 0037 的 `runtime_turn_id` + `runtime_turn_observations`，
`export_runtime_baseline.py` 无法可靠 join 一次 turn。`memory_recall_receipts.turn_id`
保持 receipt 级语义不改义，新增列另名 `runtime_turn_id`。

---

## 2. R1 实现决策记录（含偏差说明）

每条按「发现 → 决策」记录；与任务书冲突处已按 §14.3 格式说明理由。

### D1 TurnOrigin 提取方式

`automation.models.TurnOrigin` 有约 30 个 import 位点（coordinator、processor、
chat、plugin host、SDK 适配等）。R1 将枚举定义移至 `runtime/origin.py`，
`automation/models.py` 改为 `from qq_ai_bot.runtime.origin import TurnOrigin` 并
保留原名导出。这不是 R5 禁止的“Planner 旧路径 re-export”——Automation 域继续
合法持有该名字，仅所有权移到中立层；SDK 侧的版本化投影维持现状。

### D2 `runtime/__init__.py` 保持空导出

`automation.models -> runtime.origin` 与未来 `conversation.runtime -> runtime.turn ->
services.turn_coordinator -> automation.models` 会经包 `__init__` 形成环。
决策：`runtime/__init__.py` 不做任何 eager import，消费方一律从子模块导入。

### D3 TurnToken / RuntimeConfigSnapshot / VisualObservation 的引用方向

`TurnContext` 按任务书字段引用 `TurnToken`（services.turn_coordinator）、
`RuntimeConfigSnapshot`（admin.models）、`VisualObservation`（vision.models）、
`TimeContext`（time.models）。四者均不 import planner，方向安全；
`turn_coordinator` 仅依赖 automation.models 与 domain.messages，不会成环。

### D4 DelegatedAuthoritySnapshot 为中立纯数据 + 纯函数重验

不让 runtime import `automation.authority`（该模块引 Settings 与注册表）。
`runtime/authority.py` 定义中立 `DelegatedAuthoritySnapshot` 与
`CapabilityRevalidationFacts`，并以纯函数 `revalidate_delegated_capabilities()`
复刻 `automation/authority.py:effective_delegated_capabilities` 的语义
（superuser 降权归零、schema 版本相等、provenance 三元组相等、当前权限、
allowed origin）。Automation 域在 R4/R5 切换到该合同；3.5.3 行为不变。

### D5 身份键强类型

`ConversationIdentity` 沿用 `domain/conversations.py`（已是强类型且被广泛使用），
`runtime/keys.py` 新增 `TurnCoordinationKey`（工厂 `for_group/for_private/
from_inbound`，语义与 `ConversationTurnCoordinator.key_for` 一致）与
`ResolvedMemoryScope`（`partition_key` 对齐 Memory Worker 的 `group:{g}` /
`private:{u}` 分区）。三者互不可替换（不同类型、无隐式转换）。

### D6 生命周期状态机形状

`runtime/invariants.py` 的 `TurnPhaseMachine`：
- 合法转换按任务书 §6；`MODEL_ACTIVE -> MODEL_ACTIVE` 自环合法（空回复重试、
  incomplete recovery、Web fallback 的真实代码路径都在同一 MODEL 阶段内重试，
  见 `agent_runner.py:351-372/404-436/446-460`）。
- `DurableEffectState` 单调；mutation committed 后的失败必须以
  `COMMITTED_BUT_FINALIZATION_FAILED` 收轮，普通失败 outcome 会被拒绝——
  对应 coordinator mutation shield 与 `post_commit_recovery_text` 的现实语义。
- `close()` 幂等；重复 close 不同 outcome 抛 `IllegalTurnTransitionError`。

### D7 turn correlation 用 ContextVar 而不是改协议

`model_invocations` 的写点在 `TaskModelExecutor.execute`（`executor.py:209-259`），
被全部业务共享；显式传参需要动 `ModelExecutor` 协议与全部调用方，违反
“不改变 AgentRunner/Executor 协议”的禁令。决策：`runtime/observability.py`
提供 `RuntimeTurnCorrelation` ContextVar（provider-neutral ambient correlation，
等价 OTel context 惯例）；四个 DB 写点（planner begin、model record、
tool record_invocation、recall record_initial）读取 `claim_runtime_turn_id()`
填 `runtime_turn_id` 列，签名零变化。异步任务继承 context 的语义正确
（本轮派生的 speech/attribution 任务归属本轮）；autonomous/plugin background
在各自入口重新 bind 新 correlation，不继承观察消息的 turn id。

### D8 观测行的写入条件

`MessageProcessor.handle` 外提为薄包装：生成 correlation → 执行原逻辑 →
仅当 correlation 被任一写点消费过（`touched`）才落一行
`runtime_turn_observations`。这样纯命令轮、观察轮、去重轮不产生噪音行；
所有真正进入 Planner/Agent 的轮都可 join。autonomous（`_plan_latest`）与
plugin background（`_execute`）同样各自包装。

### D9 观测行字段（content-free 审计口径）

`runtime_turn_id`(unique) / origin / scope_type / conversation_key_hash(sha256) /
admission_outcome（复用 `ProcessResult.reason` 的低基数枚举字串）/ handled /
sent_messages / error_category / total_latency_ms / created_at / expires_at
（默认 30 天，`ix_runtime_turn_observations_expires` 服务清理）。无任何正文、
prompt、参数、Memory 内容或 ref。

### D10 Memory/Capability 只落协议与纯数据

- `memory/runtime/contract.py` 按 R2 §3.1 冻结 6 枚举 + `MemoryTurnContract`
  组合 validator（含 FORBIDDEN/EXCLUSIVE/context-read 配对/LOCATOR_ONLY/
  REQUESTABLE 规则）；`default_purpose` 复用现有 `MemoryRecallPurpose`。
- `MemoryCapabilityView` 定义在 `runtime/contracts.py`（跨域纯数据），
  `memory/runtime/capability_view.py` 提供从合同推导视图的构造器；
  Capability 侧只消费该视图。
- `capabilities/namespace.py` 按 R3 §2.2 冻结 `CapabilityNamespace` 与 id 语法
  （小写点分层级）；runtime/exposure/search_index/validation 为 Protocol 与
  纯数据快照，无空实现进入生产 Container。

### D11 ToolBatchExecutionResult 与 terminal 信任

`runtime/contracts.py` 定义 `ToolBatchExecutionResult(tool_results,
terminal_finalization)`；`TerminalFinalizationSource` 仅
HOST_MEMORY_FINALIZER / HOST_REPLY_CONTROL 两个可信来源。
conversation session 仅在来源可信时接受 TOOL_ACTIVE→FINALIZING；
不可信来源（含插件/MCP 构造的任何值）直接拒绝。R3/R4 在 provider→result
映射层保证插件结果永远不可能携带 host 来源。

### D12 RuntimeFoundationBundle 装配

新 `application/modules/runtime_foundation.py`：
`turn_observability`（真实 DB recorder）、`authority_factory`（Host 工厂，
superuser ceiling 来自 settings）、`provider_registry`（注册→原子冻结 revision，
冻结后拒绝注册、冻结前拒绝读取）、`turn_runtime_core_factory`
（协议占位工厂，R4 提供生产实现）。Container 仅构造并持有 Bundle，
不把空实现接入消息路径；唯一生产变化是 turn correlation 与观测行。

### D13 配置迁移映射冻结位置

映射属于 runtime config 域，冻结在
`src/qq_ai_bot/admin/config_migration_3_6.py`（常量 + 单测校验旧键存在于
当前 Settings/hot 注册表），执行器由 R5 实现。

### D14 基线脚本口径

`scripts/refactor_3_6/export_runtime_baseline.py` 只读 SQLite 聚合，输出
schema-versioned JSON（baseline commit/version、窗口、profile、样本量、
planner decision/WAIT 二次比例、latency P50/P95 + bootstrap CI、
token 分布、memory 模式频率、tool 维度、turn join 覆盖率）。
“首轮 Tool 命中率 / request_tools 使用率”为进程内 counter，无历史表——
报告以 `log_approximated` 显式标注缺口，不伪造。回放语料仓库外存放，
`replay_manifest.py` 负责 manifest SHA 的 build/verify。

---

## 3. 风险与后续轮次交接

1. **观察轮不产生观测行**是刻意选择；若 R4 需要 admission 拒绝率基线，
   使用 `planner_runs.gate_decision` 与日志口径，不回填观测表。
2. `agent_actions/speech_generations/web_search_runs/memory_mutation_receipts/
   memory_tool_receipts` 本轮不加列（任务书 0037 最小范围）；R2/R4 决定其关联。
3. ContextVar correlation 依赖“每轮一个 task 树”的现实；如未来出现跨轮共享
   task 池，需要在该边界显式 re-bind（已在 docstring 声明）。
4. `TurnState.effect_queue` 使用结构化 `TurnEffect` 协议（kind/source），与现有
   `conversation.reply.ReplyEffect` 结构兼容；R4 迁移时无需改类型。

---

## 4. R1 落地状态（实现后回写）

本轮已按建议提交顺序落地，未接管 3.5.3 生产消息路径：

1. `feat(runtime): add authoritative turn domain`
2. `feat(runtime): add conversation session lifecycle`
3. `migration(observability): add turn correlation`（Alembic `0037`）
4. `test(runtime): enforce dependency boundaries`
5. `feat(observability): capture 3.5.3 runtime baseline`

补充实现与审阅决策的对应关系：

- 配置映射冻结在 `src/qq_ai_bot/admin/config_migration_3_6.py`（D13）。
- 基线聚合在 `src/qq_ai_bot/observability/runtime_baseline.py`，CLI 为
  `scripts/refactor_3_6/export_runtime_baseline.py`；默认拒绝写入仓库工作树。
- 回放 manifest 在 `src/qq_ai_bot/observability/replay_manifest.py`，CLI 为
  `scripts/refactor_3_6/replay_manifest.py`；`production` 语料不得位于 Git 内，
  仓库只保留 `tests/fixtures/runtime_replay` 合成案例。
- `RuntimeFoundationBundle` 由 `application/modules/runtime_foundation.py` 构造，
  Container 持有后在 `__init__` 末尾冻结空的 `ProviderRegistry`。R4 必须在该
  `freeze()` 之前注册 provider；`turn_runtime_core_factory` 本轮为 `None`。
