# R2：整体抽出 Memory Runtime

> 目标：把 3.5.3 的四种互斥记忆行为从 Planner 决策和 Chat 条件分支中移出，建立独立 MemoryTurnSession。  
> 这是五轮中工作量最大、风险最高的一轮。
> 已按代码核对：`main@2695484`。

> **代码审阅批注**：原稿把当前可写状态、可申请写入、查询副作用、单次 recall intent 和异步归因放进一个平面 Session；这无法表示 passive -> request write、多次 read、locator 失败后读取重试，也会让 Plugin/Admin 纯读错误地产生 Receipt。本版把它们拆成正交合同与多个 ledger。

R2 的切换提交必须是单 owner：Memory Runtime 接管生产编排的同时，从 Planner prompt、结构化输出 Schema 和 `TurnPlan` 消费端删除 memory access/intent 字段。Planner 在 R4 前可以继续负责其余 reply/effect 决策，但不得再影响 Memory；不保留“Planner 建议 + Runtime 再解释”的双轨。

## 1. 不可损失的现有能力

R2 不是重新设计 Memory V2 数据面。以下语义必须完整保留：

- Person、PersonGroup、Group、SELF 的作用域和可见性。
- Fact、Preference、Episode。
- Evidence、Authority、Source Type。
- Active、Contested、Superseded、Invalidated。
- Supersedes、Conflict、Relation、State Event。
- Quarantine、Validation Version、Audit。
- FTS、Semantic、RRF、Intent、Activation、MMR、Context Budget。
- 3.5.3 的 background 3、continuation 4、focused 6、overview 8 检索预算数值；R2 仅把 focused/overview 的 owner 转到 Active Read。
- automatic 每目标最多 4 条；Active Read、Plugin、Admin 继续使用各自 consumer limit。
- automatic 未显式 self recall 时的隐式 current-self episode 合并（最多 1 条）与最多 500 字可信 reply excerpt 拼接。
- `memory.retrieval_enabled=false` 时对合法 current targets 的确定性 overview fallback。
- Recall Receipt 的 candidate/selected/injected/used/reinforced 阶段。
- 正文或语音成功发送后再执行 Attribution。
- mutation 的 create/correct/invalidate/restore/contest/merge/reassign/update_metadata。
- mutation 成功声明必须依据真实 receipt。
- locator 歧义不能跨人物、跨群或突破 SELF 可见性。
- Dream、Maintenance、Governance、Self Reflection、Evidence Compaction、Rebuild。

任何一项语义变化都必须在 `r2-code-review-findings.md` 明确记录，并新增回归案例。已锁定的一项变化是：删除 Planner 后 passive automatic 只产生中性 background/可信 continuation；focused/overview 由 Main Agent 的显式 read Tool 选择相同 6/8 预算，不再靠本地关键词猜测并自动注入。

术语对照必须固定，避免实现期混淆：3.5.3 代码中 `MemoryContextMode` 是检索模式（`none/lexical/hybrid/overview`），`background/continuation/recall/verify/correct` 属于 `MemoryRecallPurpose`，“focused”只是预算桶（非 background/continuation 的 purpose 映射 focused limit，见 `memory/context.py`）。本文的新 `MemoryContextPolicy(none/background/continuation)` 是注入策略新枚举，实现时不得与检索 mode 或 purpose 互相顶替。

---

## 2. 开工前 Codex 审阅

重点阅读：

```text
src/qq_ai_bot/memory/enums.py
src/qq_ai_bot/memory/models.py
src/qq_ai_bot/memory/context.py
src/qq_ai_bot/memory/query.py
src/qq_ai_bot/memory/retrieval.py
src/qq_ai_bot/memory/targets.py
src/qq_ai_bot/memory/activation.py
src/qq_ai_bot/memory/receipt.py
src/qq_ai_bot/memory/attribution.py
src/qq_ai_bot/memory/mutation/*
src/qq_ai_bot/memory/worker.py
src/qq_ai_bot/services/chat.py
src/qq_ai_bot/services/agent_tools.py
src/qq_ai_bot/services/context_assembler.py
src/qq_ai_bot/services/prompt_composer.py
src/qq_ai_bot/application/modules/conversation.py
```

重点搜索：

```bash
rg -n "MemoryAccessMode|MemoryContextMode|MemoryRecallPurpose" src tests
rg -n "memory_access|memory_intent|memory_turn_id|memory_exposures" src tests
rg -n "memory_locator_failed|memory_mutation_attempted|mutation_final" src tests
rg -n "automatic_recall|RecallReceipt|mark_injected|attribution" src tests
```

Codex 必须画出当前四条路径的完整时序图，再开始迁移。

输出 `docs/architecture/yuki-3.6.0-refactor-plan/reviews/r2-code-review-findings.md`。

---

## 3. 新 Memory Runtime 结构

```text
src/qq_ai_bot/memory/runtime/
  __init__.py
  contract.py
  state.py
  session.py
  resolver.py
  query_plane.py
  command_plane.py
  capability_view.py
  finalizer.py
  observability.py
  errors.py
```

### 3.1 MemoryTurnContract

```python
class MemoryContextPolicy(StrEnum):
    NONE = "none"
    BACKGROUND = "background"
    CONTINUATION = "continuation"

class MemoryReadPolicy(StrEnum):
    DENIED = "denied"
    DEFERRED = "deferred"
    EAGER = "eager"
    LOCATOR_ONLY = "locator_only"

class MemoryWritePolicy(StrEnum):
    DISABLED = "disabled"
    EXCLUSIVE = "exclusive"

class MemoryWriteTransition(StrEnum):
    DENIED = "denied"
    REQUESTABLE = "requestable"
    ALREADY_EXCLUSIVE = "already_exclusive"

class MemoryFinalizationPolicy(StrEnum):
    NORMAL = "normal"
    RECEIPT_GATED = "receipt_gated"

class MemoryAvailability(StrEnum):
    ENABLED = "enabled"
    FORBIDDEN = "forbidden"
```

```python
class MemoryTurnContract(BaseModel):
    context_policy: MemoryContextPolicy
    read_policy: MemoryReadPolicy
    write_policy: MemoryWritePolicy
    write_transition: MemoryWriteTransition
    finalization_policy: MemoryFinalizationPolicy
    availability: MemoryAvailability
    default_purpose: MemoryRecallPurpose
```

组合 validator 必须拒绝：

- `availability=FORBIDDEN` 但不是 `context=NONE/read=DENIED/write=DISABLED/transition=DENIED/finalization=NORMAL`；
- `write=EXCLUSIVE` 但不是 `context=NONE/transition=ALREADY_EXCLUSIVE/finalization=RECEIPT_GATED`；反向也必须成立；
- 非 NONE context 未配 DEFERRED read，或 EAGER/LOCATOR_ONLY read 配非 NONE context；
- 非 mutation locator phase 的 `LOCATOR_ONLY`；
- `REQUESTABLE` 配非 DISABLED 当前 write，或 authority 不允许持久写时的 `REQUESTABLE/ALREADY_EXCLUSIVE`。

### 3.2 Profile Factory

Profile 是便利构造，不是新基础枚举：

```text
dormant
passive
active_read
exclusive_write
forbidden
```

业务代码只能检查 Contract 字段或 Session 状态，不得写：

```python
if profile == "passive":
```

### 3.3 MemoryTurnSession

持有：

```text
contract
trusted resolved scope
recall ledger（0..N RecallHandle，每个含 runtime_turn_id、自己的 receipt_turn_id/intent/exposure）
exposure registry
mutation transition state
last mutation receipt
locator status
attribution handoff state
```

不持有整个 ChatService。异步 Attribution Worker 不持有 live Session；发送确认时冻结 immutable job 后 Session 即可关闭。

---

## 4. 状态机

Memory 不复制 R1 的 Turn lifecycle，而保存三个正交状态：

```text
AccessPhase:
  DORMANT | PREFETCHING | PREFETCHED | READ_ENABLED
  -> MUTATION_EXCLUSIVE
     -> LOCATOR_READ_ENABLED -> LOCATOR_READ_DONE -> MUTATION_EXCLUSIVE

MutationState:
  NOT_ATTEMPTED -> ATTEMPTED
  -> COMMITTED | COMMITTED_AS_CONTESTED | DEDUPLICATED
  -> NO_CHANGE | REJECTED | AMBIGUOUS | NOT_FOUND

AttributionHandoff:
  NONE -> EXPOSURE_FROZEN -> QUEUED | SKIPPED
```

Turn 的 cancelled/superseded/model_failed/tool_failed/delivery_failed 由 Turn Runtime 保存，并通过 typed hook 通知 Memory Session；Memory 只记录其领域结果。

### 4.1 强制规则

- `FORBIDDEN` 不允许 prefetch、read、write。
- `DEFERRED` 不在首轮曝光 read Tool，但允许后续请求。
- `EAGER` 首轮可以曝光 read Tool，且不自动注入。
- `EXCLUSIVE` 禁止 automatic context。
- `EXCLUSIVE` 只曝光 `memory.state.write`，定位失败后才允许受限 locator read。
- write 成功后关闭所有业务 Tool。
- write 与其他 side effect 同 Batch 时：memory transition 先验证，冲突 Tool 返回 `memory_mutation_exclusive_violation`。
- write 结果即使 `ok=true`，也必须依据 `mutation_committed` 判定是否真正提交。
- COMMITTED/COMMITTED_AS_CONTESTED/DEDUPLICATED/NO_CHANGE/REJECTED 等终止结果直接由 MemoryFinalizer 使用真实 receipt/result 生成确定性正文并结束 Agent Loop，不再发起一轮必然被丢弃的 final model。
- Mutation turn 是终端操作；混合请求中的其他问答不在同一轮继续执行，避免 commit 后模型再次获得业务能力。
- AMBIGUOUS/NOT_FOUND 不是终态：可进入 `LOCATOR_READ_ENABLED`，最多一次有界读取后重试；仍失败才确定性收束。
- 同一 Tool batch 在执行任何 call 前 preflight；一旦包含 memory write，拒绝批内所有其他 side-effect，与模型给出的调用顺序无关。

---

## 5. MemoryAccessResolver

这是本地 Authority/Availability/initial-prefetch policy，不调用模型，也不承担开放域自然语言意图分类。

输入：

```text
TurnContext
可信 trigger 类型与当前真实作者消息
结构化 direct command（如有）
图片/外部事件状态
运行配置
```

输出：`MemoryTurnContract`。

### 5.1 可直接判定的证据

- 真实平台/direct command 的结构化 action。
- 当前 origin、actor、group、image isolation 与 Runtime feature availability。
- 已经发生的 `request_tools`/read/write Tool Call 及其 capability id。
- 前一个有界 locator/read transition 的 typed result。

“记住、忘掉、你还记得”等句子只能作为回放行为样例，禁止编译成 substring/regex phrase dictionary。引用、否定、条件句、多语言表达和“讨论这个功能”会让这种分类器产生假写入或假读取。

### 5.2 决策规则

- 可信结构化 memory write command：`exclusive_write`。
- 用户显式调用 read command/tool：`active_read`。
- 普通自然语言：`passive`；Main Agent 可在首轮调用已曝光能力或用 `request_tools` 请求 read/write。
- Authority/origin 明确禁止 Memory：`forbidden`。`memory.retrieval_enabled=false` 只表示检索引擎降级，不等同所有 Memory forbidden；锁定保留当前对合法 current targets 的 deterministic overview fallback，且该 fallback 仍受 consumer budget/纯读副作用策略约束。
- 图片轮次禁止写：context 可读，但 write disabled。此项相对 3.5.3 是收紧而非保持：现状仅“带图的显式写命令”走 `image_write_isolated` 拒绝，`memory_change` 工具与 Planner 的 MUTATION 选择并未按图片轮硬关。收紧必须进入回放对比与 release note，不得描述为“保持现状”。
- Plugin Background / External Event：依据 origin 和 authority 明确限制。
- Admin/Direct Command 继续走专有命令路径，不经过自然语言 resolver。

### 5.3 模糊情况

普通自然语言不调用专用 Router：

- 首轮以 passive 进入 Main Agent。
- `memory.state.write` 保持 requestable，但未曝光。
- Main Agent 调用 `request_tools` 搜索修改记忆能力时，MemoryTurnSession 原子切换为 `MUTATION_EXCLUSIVE`。
- 下一次模型请求只曝光合法 mutation 能力。

Provider 若为 Responses，历史声明的 Schema 不能物理删除，只将其从 callable set 撤权；Chat Completions 可发送最小 Schema 集。任何路径都不得依赖 `tool_choice=required`，DeepSeek 请求不得发送不支持的 `tool_choice` 字段。

---

## 6. Query Plane

统一入口：

```python
class MemoryReadConsumer(StrEnum):
    AUTOMATIC_CONTEXT = "automatic_context"
    AGENT_TOOL = "agent_tool"
    PLUGIN = "plugin"
    ADMIN = "admin"

class MemoryReadRequest(BaseModel):
    text: str
    intent: MemoryQueryIntent | None
    requested_limit: int | None = Field(default=None, ge=1, le=100)
    resolved_scope: ResolvedReadScope
```

`consumer` 由后端入口选择，不能作为调用者/模型可写字段；`resolved_scope` 由 Target Resolver 从 Authority、真实发送者、当前群、@/回复者与成员资格生成，模型不得直接提交 QQ/group id。Plugin/Admin 先经过自己的 permission adapter。REBUILD/Dream/Maintenance/Governance 继续使用专有 domain port，不伪装成 turn Query Plane consumer。

`requested_count` 从 `MemoryQueryIntent` 移除，数量是 consumer/request budget，不是语义意图。Memory Query 合同版本由 5 升至 6 并更新 frozen snapshot；Plugin Memory Facade 的 1.0 方法与返回结构不变，Plugin/Admin 缺少 intent 时继续 neutral ordering。

所有读取继续复用：

```text
Target Resolver
  -> Query Builder
  -> FTS / Semantic
  -> RRF
  -> optional Intent + Activation（neutral policy 时跳过）
  -> MMR
  -> Consumer Budget
  -> frozen result
```

查询内核必须纯读。仅 `AUTOMATIC_CONTEXT`/`AGENT_TOOL` 在“最终 payload 已经实际进入 Main Agent 请求”后调用 `publish_exposure()`，再写 injected/Receipt/last_injected_at。Plugin/Admin 永远不产生 Recall Receipt、Activation 或 injection 副作用。Retriever 前后数据库必须完全一致。

`publish_exposure()` 以既有唯一 `receipt_turn_id` 和 `(receipt_id, fact_id)` item constraint 幂等，在一个数据库事务内创建/更新该 handle 的 Receipt header/items、写入同一 `runtime_turn_id`、把真实 payload facts 标为 injected，并更新相同 facts 的 `last_injected_at`；提交前崩溃则这些变化都不可见，提交后重试不得重复计数。candidate/selected trace 来自 frozen query result，但纯查询或从未进入模型的 payload 不落 Receipt。

### 6.1 Automatic Context

- background：最多 3 条。
- continuation：最多 4 条。
- `focused/overview` 不再是 Context Policy；overview 仅作为 Active Read 的 Query mode，focused 语义由结构化 query/intent 表达。
- 每目标最多 4 条。
- 不曝光 Memory read Tool。
- Memory Intent 默认由 Runtime 生成：background 或确定性 continuation。
- 默认 background intent 不包含 entities、absolute range、overview、current_self 等推断字段；continuation 只使用可信 reply excerpt/上一轮 handle，不把整段最近历史拼入 FTS query。
- 保留 3.5.3 行为：automatic query 只拼当前消息与最多 500 字的可信 reply excerpt；未显式 self recall 时仍额外合并最多 1 条合法 current-self episode。最近历史不得整体拼入 FTS query，SELF 仍受原 visibility/target rules。

### 6.2 Active Read

Main Agent Tool 参数表达：

```text
query
purpose
mode: lexical/hybrid/overview
subject reference
entities（最多 5 个，每个规范化后最多 64 字符）
ISO-8601 绝对时间范围
preferred kinds: fact/preference/episode
数量（1..100；automatic 不读取此值，仍固定为 background 3 / continuation 4）
```

Active Read 未给数量时，结构化相关查询默认 6，overview 默认 8；显式数量经 consumer budget 截断到 1..100。这样保留 3.5.3 的默认预算，而不保留不可达的 focused/overview Context Policy。

模型只表达语义。真实目标由 Resolver 决定；subject 只能给合法目标软加分，绝不能扩大 scope。strict range 继续按 `valid_from` 语义执行，Runtime 校验绝对时间但不解析自然语言日期。

锁定保留现有 subject-specific Tool 边界：`get_person_memories`、`get_group_memories`、`get_self_memories` 继续拥有各自的 Target Resolver 与可见性规则，`get_memory_fact/get_memory_evidence` 继续作为已有 ref/fact 的有界直读；不新增一个允许模型自由提交身份字段的通用 gateway。前三个检索 Tool 仅增加可选的结构化 intent 字段并在后端汇入共同 Query Plane，缺省时采用 consumer 决定的 neutral ordering。identity、QQ、group id 与 `subject_ref` 不进入 intent；已有同群投影和 SELF visibility adapter 必须先解析出 `ResolvedReadScope`。

### 6.3 检索模式

R2 不新增 LLM Query Planner。

R2 的锁定策略：

- background / continuation：保持 3.5.3 的现有 hybrid 检索与 semantic degrade 语义，单独记录前置 Embedding 请求和延迟。
- agent tool relevant：hybrid。
- overview：结构化 overview。

当前 `MemoryRetriever` 在看 lexical 质量前就先调用 Embedding，不适合顺手实现 lexical->semantic 级联。级联固定延期到独立 R2.1 性能任务；R2 不同时重写 Runtime 所有权与排序算法。总纲的“0 前置模型”只指生成式 Router/Planner，不得把此 Embedding 隐去。

---

## 7. Command Plane

唯一的**自然语言对话编排入口**：

```python
MemoryRuntime.command(
    session,
    MemoryMutationRequest,
) -> MemoryMutationReceipt
```

继续使用现有 `MemoryMutationService`，包括：

- candidate locator
- evidence
- authority
- semantic relation
- resolution plan
- conflict
- transaction
- state event
- receipt

`MemoryMutationService` 继续是所有领域（Agent/Admin/Plugin/Worker/Reflection/Dream）的唯一 durable persistence boundary，并独占数据库 transaction、receipt reserve/apply/finalize、幂等锁和 commit 后 embedding job。`MemoryRuntime.command()` 只做 Session transition/authority adapter，不能在 Service 外重包事务；Dream/Worker 不伪装成 TurnSession。

`AgentToolService` 不再负责 mutation turn 的最终文字和状态管理，只负责将合法参数转为 Command Request。Mutation batch 在 ToolCoordinator 执行首个 call 之前完成 effect preflight。

---

## 8. MemoryCapabilityView

Memory Runtime 对 Capability Runtime 只暴露：

```python
class MemoryCapabilityView(BaseModel):
    eager_namespaces: tuple[str, ...]
    requestable_namespaces: tuple[str, ...]
    hidden_namespaces: tuple[str, ...]
    exclusive_namespace: str | None
    transition_revision: int
```

示例：

```text
passive:
  eager = []
  requestable = [memory.person.read, memory.self.read, memory.group.read,
                 memory.history.read, memory.state.write]

active_read:
  eager = [memory.*.read]
  requestable = [memory.state.write]  # 仅 authority 允许时；请求后先原子 transition
  hidden = []

exclusive_write:
  eager = [memory.state.write]
  hidden = [other memory reads and all other business writes]
```

Capability Runtime 不读取 `MemoryTurnSession` 内部枚举。

`transition_revision` 对应 callable policy revision，不等于 Responses 已声明 Schema revision。旧 schema 可继续存在于 continuation，但 transition 后未授权调用必须被 Backend 拒绝。

---

## 9. 从 ChatService 移出的逻辑

必须移出：

```text
_initial_scopes_for_memory_access
_automatic_memory_mode
_with_memory_mutation_contract
memory_access branches
memory_locator_failed state
memory_mutation_attempted state
last_memory_mutation_result
memory mutation final text
memory post-commit recovery text
memory access metrics dispatch
memory exposure registry construction
attribution queue condition
```

`ChatService` 最终只能调用：

```python
memory_session.prefetch()
memory_session.capability_view()
memory_session.observe_tool_result()
memory_session.finalize_text()
memory_session.on_delivery_confirmed(delivery_summary)
```

`DeliverySummary` 必须来自 Delivery Runtime，包含 final agent run id、实际送达正文/语音、complete/partial/cancelled/superseded、纯表情/空正文和 transport receipt。Native Web fallback 每次 Agent run 使用独立 Exposure Registry，只冻结最终 run。

Prompt 构造完成不等于 exposure。`prefetch()` 返回候选 token；只有 AgentRunner 即将发送的真实 model request hook 才调用 `confirm_prompt_exposure(token, payload_fact_ids)`。Prompt 构造失败、模型未启动或被 supersede 时不得标 injected。

每次 automatic/tool read 产生独立 `RecallHandle(intent, receipt_id, exposure_refs)`。归因 job 按 handle 分组 used/reinforcement，避免 background/recall/verify 使用错误 alpha。发送确认后冻结 job，Session 可关闭。

Receipt/Activation 的允许崩溃语义写死：used 但尚未 reinforced 可以作为 best-effort 中断状态；新增 `AttributionCommitter`，单 fact 的 receipt item claim、active/quarantine recheck、Activation CAS、item reinforced 和该 receipt header `reinforced_count` 重算必须在同一事务完成，删除后续独立 `mark_reinforced()` 事务。used items 与 header `used_count` 也在同一事务重算。不得出现 item 已 reinforced 而 header 永久陈旧。

---

## 10. 模型 Prompt 规则

Memory Runtime 提供固定规则片段：

- Entity block 归属规则。
- reported/third-party/contested 语义。
- occurred_at 与 updated_at 区别。
- current_self 不覆盖静态人格。
- mutation 只认 receipt。

Mutation contract 不再由 `chat.py` 临时追加，而由 Session 在 `exclusive_write` 中生成。

---

## 11. 测试矩阵

### 11.1 Contract

- dormant：无 prefetch、read 可请求；write transition 由 authority 决定 requestable/denied。
- passive：自动 prefetch、首轮无 Memory Tool。
- active-read：无自动注入、首轮有 read Tool、无 write Tool。
- exclusive-write：无自动注入、只有 write Tool。
- forbidden：所有 Memory 能力均不可见。
- 所有非法 Contract 组合在构造时失败。

### 11.2 状态转换

- passive -> request_tools(memory write) -> exclusive-write。
- active-read 不能直接执行 write。
- exclusive-write 不能执行 Web/Admin/Automation mutation。
- locator ambiguous 后只开放合法 locator read。
- locator read 后最多一次重试并确定性收束。
- 多次 read handle 的 purpose/receipt/强化 alpha 不串线。
- commit 后 Tool closed。
- terminal mutation 不再发最终模型请求，MemoryFinalizer 只依据 receipt/result 生成确定性正文。

### 11.3 安全

- 跨人物污染 0。
- 跨群污染 0。
- SELF visibility 泄漏 0。
- 模型传入任意 user_id 不扩大范围。
- 同群被提及成员只读取 `list_person_facts_projected_to_group` 允许的本人/explicit evidence 投影，不泄漏其全局 PERSON facts，且投影 read-only。
- 图片轮次不写入。
- external event 不写入。
- contest/noop/not-found 不被描述为成功修改。

### 11.4 质量

- 现有 Memory Quality 18/18、38/38 全部通过。
- 自动召回预算保持。
- Memory Query v6 frozen snapshot 固定，Plugin Memory Facade 1.0 snapshot 不变。
- Retriever、Plugin/Admin search 前后数据库状态完全一致。
- Prompt/model start 失败不得产生 injected；只有真实 model payload exposure 才产生 Receipt。
- Activation/Receipt 的 crash consistency、CAS、item/header 同事务重算和幂等通过故障注入。
- 现有 Receipt header `candidate_count` 冻结为 source-candidate 次数（lexical/semantic 同一 fact 可重复），不改列义；新增进程指标 `candidate_unique_count`，而 `selected_count` 继续是 unique fact 数，trace item 数由实际截断且去重后的 items 派生。测试不得断言三者相等，因此 R2 不为此新增数据库列。
- Plugin/Admin read 不受 automatic budget 限制。
- `retrieval_enabled=false` fallback、semantic provider degrade、strict temporal missing `valid_from`、隐式 SELF episode 和 reply excerpt 回归通过。

---

## 12. 数据与配置

R2 可以新增 Memory Runtime 观测字段，但不得删除旧 Planner 表，删除工作留到 R5。

必须观测：

```text
contract profile
resolver reason
prefetch latency
candidate_source/unique、selected/injected counts
read transition
mutation transition
finalization source
```

不保存用户问题、回复正文或记忆正文。

---

## 13. 本轮禁止事项

- 不保留 `MemoryAccessMode` compatibility alias。
- 不在 ChatService 写新的 Memory 分支。
- 不让 Capability Runtime理解 Memory mutation 细节。
- 不让 Main Agent决定真实身份作用域。
- 不把 mutation 简化为普通 write Tool。
- 不取消 automatic/tool/mutation 的互斥行为。
- 不引入 Memory Router LLM。

---

## 14. 建议提交顺序

1. `feat(memory-runtime): add turn contract and state machine`
2. `feat(memory-runtime): add deterministic access resolver`
3. `feat(memory-runtime): unify query consumers`
4. `feat(memory-runtime): orchestrate mutation and deterministic finalization`
5. `refactor(chat): remove memory access ownership`
6. `test(memory-runtime): port four-path regression suite`
7. `refactor(memory): delete MemoryAccessMode from business path`
8. `docs(refactor): record R2 code review findings`

---

## 15. R2 退出条件

- `rg "MemoryAccessMode" src/qq_ai_bot/services src/qq_ai_bot/conversation` 无结果。
- ChatService 不生成 mutation 最终文本。
- Memory Runtime 独立控制 capability view。
- 四类行为、locator 重试、多 RecallHandle 和安全不变量全部由正交状态测试覆盖。
- Memory Quality 与污染门全部通过。
- R2 分别报告 pure retrieval P95（与旧基线可比）和 end-to-end memory prepare P95（含 resolver/target/budget/exposure/receipt）；automatic 与 active read 不混算。
- 全量测试通过。
