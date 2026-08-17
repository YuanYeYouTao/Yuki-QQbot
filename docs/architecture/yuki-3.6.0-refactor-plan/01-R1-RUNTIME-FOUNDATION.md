# R1：建立最终 Runtime 骨架

> 目标：先把最终架构的地基建完整，再迁移业务逻辑。  
> 本轮不允许产生临时 Runtime、兼容 Wrapper 或双路径生产开关。
> 已按代码核对：`main@2695484`（Yuki `3.5.3`）。

> **代码审阅批注**：原稿的 Shared Turn 类型会把历史 key、群级取消 key 和 Memory scope 混成一个字符串，也无法表示 Scheduled/Plugin trigger；生命周期还错误地假设 MODEL 只执行一次。本轮必须先修正这些合同，否则 R2-R4 一定再次破坏冻结类型。

## 1. 本轮完成状态

R1 完成后，代码库中应存在最终版本的 Runtime 类型、依赖方向、生命周期和观测接口。旧 Planner 仍可在 3.5.3 feature-branch 主路径运行，但所有新 Runtime 代码禁止 import Planner。

本轮的关键不是“让新链路先能回复一句话”，而是把未来四轮要依赖的基础对象一次定义好。

---

## 2. 开工前 Codex 审阅

重点阅读：

```text
src/qq_ai_bot/container.py
src/qq_ai_bot/application/lifecycle.py
src/qq_ai_bot/application/module.py
src/qq_ai_bot/application/modules/conversation.py
src/qq_ai_bot/services/processor.py
src/qq_ai_bot/services/chat.py
src/qq_ai_bot/services/agent_runner.py
src/qq_ai_bot/services/turn_coordinator.py
src/qq_ai_bot/domain/messages.py
src/qq_ai_bot/automation/models.py
src/qq_ai_bot/admin/models.py
```

Codex 必须确认：

1. `ApplicationContainer` 当前资源所有权和启动/关闭顺序。
2. `ConversationTurnCoordinator` 的 token、取消、supersede、mutation started 语义。
3. User Message、Autonomous Group、Scheduled Automation、Plugin Background 的共享点和差异。
4. `ToolRuntime`、`AgentRuntime`、`PlannedTurn` 当前字段的真实使用者。
5. 哪些字段属于可信 Runtime 状态，哪些是 Planner 输出。
6. 哪些生命周期 Hook 依赖 Planner event 名称。
7. 当前模型调用和 Tool 调用的观测表能否记录基线。

输出 `docs/architecture/yuki-3.6.0-refactor-plan/reviews/r1-code-review-findings.md`。

---

## 3. 新包结构

一次建立：

```text
src/qq_ai_bot/runtime/
  __init__.py
  origin.py
  trigger.py
  keys.py
  authority.py
  turn.py
  result.py
  delivery.py
  errors.py
  invariants.py
  observability.py

src/qq_ai_bot/conversation/          # 扩展现有 package，不是新建同名包
  runtime.py
  session.py
  admission.py
  state.py

src/qq_ai_bot/memory/runtime/
  __init__.py
  contract.py
  session.py
  resolver.py
  capability_view.py

src/qq_ai_bot/capabilities/
  namespace.py
  runtime.py
  exposure.py
  search_index.py
  validation.py
```

R1 中 Memory/Capability 只建立最终 Protocol 和测试实现，业务迁移在 R2/R3 完成；禁止把空实现注册进生产 Container。跨域纯数据视图放在 `runtime/contracts.py`，避免 Memory 与 Capability 互相 import。

---

## 4. Shared Turn 类型

### 4.1 TurnTrigger 与身份键

不得强迫所有 origin 伪造 `InboundMessage`：

```python
TurnTrigger = (
    MessageTurnTrigger
    | ExternalEventTurnTrigger
    | ScheduledTurnTrigger
    | PluginSessionTurnTrigger
)
```

`TurnOrigin` 从 `automation.models` 提取到中立 `runtime/origin.py`（SDK 保留明确版本化投影），避免通用 Runtime/Capability 反向依赖 Automation。`DelegatedAuthoritySnapshot` 也作为中立纯数据合同保存 schema/provenance；Automation 领域负责构造和重验它。

只有 `MessageTurnTrigger` 必须包含真实 inbound、ledger event id 和用户 profile。`/ai forgetme`、native/direct command、拒绝/限流和只观察不回复的群消息继续保留各自现有入口；本章流程只描述 admitted Agent turn。

身份键必须强类型分离：

```text
ConversationIdentity   -> 历史与上下文隔离（群聊当前为 group:{g}:user:{u}）
TurnCoordinationKey    -> cancel/supersede/reply sequence（群聊当前为 group:{g}）
ResolvedMemoryScope    -> Memory Runtime 根据可信 actor/group/evidence 解析
```

> **批注原因**：真实代码中 `domain/conversations.py`、`services/turn_coordinator.py` 和 Memory Worker 使用三种不同分区。复用一个 `conversation_key` 会在“每人历史隔离”和“整群抢占”之间必然破坏一个。

### 4.2 TurnAuthority、Scene 与 Taint

```python
@dataclass(frozen=True, slots=True)
class TurnAuthority:
    actor_user_id: str
    bot_user_id: str
    origin: TurnOrigin
    permission_ceiling: frozenset[str]
    delegated_authority: DelegatedAuthority | None
    authority_revision: int
```

规则：

- Authority 只由 Host `AuthorityFactory` 从真实 trigger、配置和数据库生成。
- 模型不能提供或修改这些字段。
- Plugin/MCP Tool 只能消费，不得重建 Authority。
- 真实身份目标仍由各领域 Resolver 验证。
- Superuser 和高风险执行前按当前配置复核，不把旧快照当永久授权。
- Scheduled delegation 保留 creator 当前权限、capability schema version、plugin provenance 和 allowed origin 的现有重验。

场景与动态状态分离：

```text
TurnSceneFacts (immutable): group/private, image present, reply/mention facts
TurnTaintState (monotonic): external_data_consumed, mutation_committed, ...
```

每次 Tool 曝光和真正执行前都计算：`authority ceiling ∩ delegation ∩ current permission ∩ scene/taint policy`。禁止用互相独立的 `allow_*` 布尔值表达权限。

### 4.3 TurnContext

```python
@dataclass(frozen=True, slots=True)
class TurnContext:
    trigger: TurnTrigger
    authority: TurnAuthority
    scene: TurnSceneFacts
    runtime_config: RuntimeConfigSnapshot
    conversation: ConversationIdentity | None
    coordination_key: TurnCoordinationKey | None
    turn_id: str
    turn_token: TurnToken | None
    current_time: TimeContext
    normalized_content: UntrustedContent
    visual_observation: VisualObservation | None
```

`profile`、`conversation`、`coordination_key` 只在对应 trigger 上存在。`normalized_content` 明确为不可信数据，不能进入 Authority。

### 4.4 TurnState

`TurnState` 只保存本轮可变状态，不保存长期事实：

```text
reply_target
effect queue
declared schema ledger
callable capability revision
memory session/receipt handles
turn taint state
```

`ConversationTurnCoordinator` 独占 token/version/task cancellation/mutation shield；`AgentRunner` 独占 model/tool 计数和 provider continuation；Memory Runtime 独占 mutation receipt。TurnState 不复制这些事实，最终从各组件结果汇总。若需要实时观测，给 Runner 增加 typed event sink，不能靠两边手工同步。

### 4.5 TurnResult 与 DeliveryOutcome

```python
@dataclass(frozen=True, slots=True)
class TurnResult:
    generated_text: str
    model_requests: int
    tool_calls: int
    delivery: DeliveryOutcome
    outcome: TurnOutcome
    durable_effect_state: DurableEffectState
```

`DeliveryOutcome` 逐项记录 `kind/source/transport_accepted/receipt/ledger_recorded/error_category`，并包含 `complete/partial/cancelled/failed` 和 `agent_body_delivered`。`sent_messages` 由 receipt 派生，不能与 receipt 冗余；voice-only、emoji-only、发送成功但 ledger 失败和部分发送都必须可表示。

---

## 5. ConversationRuntime 接口

R1 定义最终协议：

```python
class ConversationRuntime:
    async def begin_turn(...) -> ConversationTurnSession: ...

class ConversationTurnSession:
    context: TurnContext
    state: TurnState

    async def prepare(self) -> PreparedTurn: ...
    async def run_agent(self, prepared: PreparedTurn) -> AgentRunResult: ...
    async def deliver(self, result: AgentRunResult) -> TurnResult: ...
    async def close(self) -> None: ...
```

该接口只服务需要形成用户可见回复的 turn。Scheduled Automation、Plugin Session、Plugin Background 使用各自 trigger/coordinator，并复用更低层的 `TurnRuntimeCore`、Authority/Capability/Agent 协议；不得为统一接口继续制造 synthetic user inbound。

`PreparedTurn` 包含：

```text
model messages
memory session reference
capability exposure snapshot
reply target control
effect context
```

不包含 Planner Plan。

`AgentToolBackend.execute_batch()` 返回 typed `ToolBatchExecutionResult(tool_results, terminal_finalization?)`。只有 Host 受信任的 Memory receipt finalizer 或本地 reply-control 能设置 terminal；Plugin/MCP 返回值和远端 annotations 不能自行结束 Agent Loop。AgentRunner 收到合法 terminal 后直接进入 FINALIZING，不再强制额外模型请求。

---

## 6. Runtime 生命周期

```text
CREATED -> ADMITTED -> PREPARED
PREPARED -> MODEL_ACTIVE
MODEL_ACTIVE -> TOOL_ACTIVE -> MODEL_ACTIVE      # 可重复
TOOL_ACTIVE -> FINALIZING                        # Host-authorized terminal receipt/control
MODEL_ACTIVE -> FINALIZING
FINALIZING -> DELIVERING
DELIVERING -> DELIVERED | PARTIALLY_DELIVERED | DELIVERY_FAILED
terminal outcome -> CLOSED
```

`TurnPhase`、`TurnOutcome` 和 `DurableEffectState` 分离。异常属于 Outcome：

- cancelled
- superseded
- model_failed
- tool_failed
- delivery_failed
- committed_but_finalization_failed

mutation committed 是不可逆 DurableEffectState，不是普通 phase。Coordinator 的 mutation shield 在 durable write 开始后生效；close 幂等，不能回滚已提交效果。状态机必须覆盖 empty retry、incomplete recovery、Native Web fallback 和 post-commit recovery。

---

## 7. 依赖边界测试

新增测试脚本扫描 AST import，并维护逐轮**只减不增**的旧依赖 exception allowlist：

```text
qq_ai_bot.runtime          不得 import qq_ai_bot.planner
qq_ai_bot.conversation     新 Runtime 文件不得 import qq_ai_bot.planner
qq_ai_bot.memory.runtime   不得 import qq_ai_bot.planner
qq_ai_bot.capabilities     R1 新 Runtime 文件不得 import qq_ai_bot.planner
```

还要禁止：

```text
memory.runtime -> capabilities.runtime
capabilities.runtime -> memory implementation
```

双方只通过共享纯数据协议交互。扫描覆盖普通/相对 import、`TYPE_CHECKING` 和动态 import 常量；R5 的 runtime `src` allowlist 必须归零，历史 migration/doc 不计入运行时零匹配。

---

## 8. 基线观测

在改动主路径前，先建立 content-free turn correlation，再记录 3.5.3 基线：

- Planner 调用次数、延迟、输入/输出 Token。
- Main Agent 调用次数、延迟、输入/输出 Token。
- 普通私聊 P50/P95 总延迟。
- Tool 场景 P50/P95。
- Memory automatic/tool/mutation 各自频率。
- Planner silent/wait/reply 比例。
- Planner WAIT 后第二次调用比例。
- Tool selection Flash 调用比例。
- 首轮 Tool 命中率。
- `request_tools` 使用率和零结果率。
- Tool Schema Token 分布。

新增离线脚本：

```text
scripts/refactor_3_6/export_runtime_baseline.py
scripts/refactor_3_6/replay_manifest.py
```

`export_runtime_baseline.py` 固定输出 schema-versioned、UTF-8、无正文 JSON：baseline commit/version、采样窗口、provider/profile、样本量、失败口径、turn/model/tool/delivery 聚合、Planner decision/effect 比例、bootstrap confidence interval 和 corpus manifest SHA。默认写入用户显式指定的 Git 外目录；Release 只保存脱敏聚合与 SHA，不保存对话、prompt、Tool arguments、Memory 正文或 ref。

Admission 生成随机 opaque `TurnContext.turn_id`，数据库投影统一命名为 `runtime_turn_id`，传播到 planner run、model invocation、tool invocation、memory recall receipt 和 delivery outcome。相关现有表增加 nullable indexed `runtime_turn_id`，同时新增 `runtime_turn_observations`（含 retention/清理索引）；只存枚举、计数、时间、hash、错误类别和 partial-delivery 状态，不保存 prompt、正文、Tool arguments、Memory 内容或 ref 列表。

基线 Head 仍为 0036 时，本轮使用 `0037`：为 `planner_runs/model_invocations/tool_invocations/memory_recall_receipts` 增加 nullable indexed `runtime_turn_id`，并新建有 30 天 retention 的 `runtime_turn_observations`；所有旧行保持 NULL。`memory_recall_receipts.turn_id` 这个既有列继续作为每个 Recall Receipt 自己的唯一 `receipt_turn_id`，不得误当整轮 ID 或原地改义。Maintenance 分批清理 turn observations，模型/工具/Receipt 历史表沿用其现有保留策略。

0037 的关联范围刻意最小化：`agent_actions`、`speech_generations`、`web_search_runs`、`memory_mutation_receipts`、`memory_tool_receipts` 等其余观测表本轮不加列。mutation/tool receipt 的 turn 关联由 R2 在 Memory Runtime 观测中决定，reply effect/speech 维度由 R4 的 `reply_effect_events` 承担；基线报告若需要这些维度，先以日志口径近似并显式注明，不得把缺失列的表当成已可 join。

> **批注原因**：当前 `model_invocations` 没有 turn/conversation 关联，`tool_invocations` 只有 conversation hash，端到端延迟主要在日志，进程内首轮命中 counter 重启即丢。没有这一步，`export_runtime_baseline.py` 无法可靠 join 一次 turn，也无法证明性能收益。

回放正文不能只保存 hash，否则 Memory/Capability 搜索无法执行。真实脱敏语料放在 Git 外受控存储；仓库只保存合成/人工改写案例、manifest SHA、标注版本、配置、Profile、硬件和冷/热缓存条件。

---

## 9. Application Module 调整

R1 中 `ConversationModule` 只构造新 Runtime protocol/factory，不把空实现装入或接管生产消息。

新增：

```text
RuntimeFoundationBundle
  turn_observability
  authority_factory
  provider_registry
  turn_runtime_core_factory
```

`ApplicationContainer` 继续是资源宿主，不把所有业务重新塞进 Container。各模块只向独立 `ProviderRegistry` 注册 provider；在生命周期完成注册后原子冻结 revision。明确注册顺序、健康检查、partial-start rollback 和逆序关闭，禁止 Runtime 回查 Container 形成 service locator。

---

## 10. 本轮测试

### 10.1 单元测试

- TurnAuthority 不能由模型字段覆盖。
- Message/External/Scheduled/PluginSession 四类 trigger，禁止 synthetic user inbound。
- History identity、coordination key、Memory scope 不串用。
- DelegatedAuthority 降权、Schema 变化、Plugin provenance 变化后重新求交集。
- TurnPhase/Outcome/DurableEffectState 状态转换合法性。
- 非法逆向转换抛出明确错误。
- Session close 幂等。
- supersede/cancel 状态正确。
- committed mutation 的恢复状态能表示。
- MODEL <-> TOOL 多轮、空回复重试、incomplete recovery、Web fallback。
- Host-authorized terminal Tool batch 可直接 FINALIZING；Plugin/MCP 伪造 terminal metadata 无效。
- partial delivery、transport accepted 但 ledger 失败、voice-only、emoji-only。
- opaque runtime_turn_id 跨 planner/model/tool/recall receipt/delivery 可关联；Receipt 既有 turn_id 仍保持 receipt_turn_id 语义，且全链零正文泄漏。
- import boundary 扫描。

### 10.2 集成测试

使用 Fake Runtime 验证：

```text
inbound -> begin_turn -> prepare -> fake agent -> deliver -> close
```

不调用旧 Planner，不访问生产主路径。

---

## 11. 本轮禁止事项

- 不新增 `NewTurnPlan`。
- 不复制 Planner Schema 到 Runtime。
- 不建立 Planner Adapter。
- 不把 Memory 具体查询逻辑搬进 ConversationRuntime。
- 不把 Tool Search 具体逻辑搬进 ConversationRuntime。
- 不创建一个巨大 `Runtime` 类持有所有服务。
- 不改变 AgentRunner 协议。

“不改变 AgentRunner 协议”仅指不重写 bounded loop；R1 允许增加 provider-neutral typed event sink、turn correlation 和只读结果字段。否则无法满足本轮观测与生命周期合同。

配置迁移也在 R1 冻结映射、由 R5 执行：`planner.group_enabled→conversation.autonomous_enabled`、`planner.group_debounce_seconds→conversation.autonomous_debounce_seconds`、`planner.reply_necessity_threshold→conversation.autonomous_admission_threshold`、`planner.max_pending_messages→conversation.autonomous_batch_limit`、`planner.recent_presence_window_seconds→conversation.autonomous_presence_window_seconds`、`planner.interrupt_autonomous_on_new_message→conversation.interrupt_autonomous_on_new_message`、`reply.plan_hard_max_messages→reply.hard_max_messages`、`speech.planner_enabled→speech.agent_effects_enabled`。模型 temperature/token/timeout/confidence/max_wait 等 Planner 专属键删除。同 scope 已存在新键时保留新值并删除旧键。

---

## 12. 建议提交顺序

1. `feat(runtime): add authoritative turn domain`
2. `feat(runtime): add conversation session lifecycle`
3. `migration(observability): add turn correlation`
4. `test(runtime): enforce dependency boundaries`
5. `feat(observability): capture 3.5.3 runtime baseline`
6. `docs(refactor): record R1 code review findings`

---

## 13. R1 退出条件

- 最终 Runtime 类型已经确定。
- 新 Runtime 包不引用 Planner。
- 状态机测试完整。
- Coordinator 仍是 token/version/cancellation 的唯一所有者。
- 基线回放数据已生成。
- 真实 turn 关联观测已上线并积累足够基线样本。
- 当前 3.5.3 全量测试仍通过。
- 后续 R2/R3 不需要再改 Shared Turn 类型的基本所有权。

未满足最后一条，不能进入 R2。
