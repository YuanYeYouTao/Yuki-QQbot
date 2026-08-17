# Yuki 3.6.0 毁灭性 Runtime 重构总纲

> 文档状态：实施总纲  
> 目标版本：`3.6.0`  
> 目标分支：`codex/refactor-3.6-runtime`  
> 已按代码核对：Yuki-QQbot `main@2695484`（`v3.5.3`），审阅日期 2026-08-17。  
> 核心决定：删除强制 Planner，建立 Conversation Runtime、Memory Runtime、Capability Runtime，由 Main Agent 直接进入受控 Agent Loop。

> **代码审阅批注（总纲）**：核心方向成立，但原稿把“删除前置 Planner”扩大成了“模型前所有工作都必须是纯本地毫秒级”，与当前 Vision 和 hybrid Memory 的真实调用不符；还把自然语言关键词表当成了确定性 Memory/Voice/Emoji 路由。修订后严格禁止的是**前置生成式 Router/Planner LLM**，而不是隐瞒 Vision 或 Embedding 网络调用；自然语言语义决定权交给 Main Agent，Runtime 只执行可信事件规则、能力曝光、权限和事务门。

## 1. 结论先行：新架构会更快吗

**架构上有明确的减延迟机会，但必须以 3.5.3 实测基线证明；本文不预先把“更快”当成事实。**

当前典型路径是：

```text
本地准入判断
  -> Planner 模型请求
  -> Main Agent 模型请求
  -> 可选 Tool
  -> Main Agent 继续
```

3.6.0 的目标路径是：

```text
本地准入判断
  -> 本地 Memory Prefetch
  -> 本地 Capability Search
  -> Main Agent 模型请求
  -> 可选 Tool
  -> Main Agent 继续
```

旧路径中，Planner 是一次真实网络模型请求，而且当前 Planner 已承担回复决策、工具范围、记忆访问、语音、表情、发送方式、目标消息等多项工作。它必然增加一次网络往返和一份结构化 Prompt，但现有观测不足以证明其成本“常常高于 Main Agent”。新路径删除这次生成式路由请求；Vision 分析和 hybrid Memory Embedding 仍须单独计数，不能混入“本地 Runtime”耗时。

### 1.1 分场景判断

| 场景 | 旧架构 | 3.6.0 | 预期 |
|---|---|---|---|
| 普通私聊，无 Tool | Planner + Agent | Agent | 明显更快 |
| 普通私聊，自动记忆 | Planner + Memory + Agent | Memory + Agent | 明显更快 |
| 群聊不响应 | 本地判断，部分情况进入 Planner | 本地 Admission | 更快或相同 |
| 单 Tool 请求，首轮命中 | Planner + Agent + Tool + Agent | Agent + Tool + Agent | 更快 |
| Tool 首轮漏检 | Planner + Agent 或 Agent 补 Tool | Agent + `request_tools` + Agent + Tool + Agent | 可能更慢 |
| 显式记忆读取 | Planner + Agent + Memory Tool + Agent | Agent + Memory Tool + Agent | 更快 |
| 明确记忆修改 | Planner + Mutation Agent | 本地进入 Memory Mutation Session + Agent/Tool | 更快或相同，安全规则不变 |
| 复杂多步任务 | Planner + Agent Loop | Main Agent Loop | 通常更快；少数任务可能需要更多 Main Agent 迭代 |

所以本项目不宣称“每一轮都必然更快”。**整体性能收益依赖两项硬指标：**

1. 普通轮次彻底取消前置 Planner/Router 生成式模型调用。
2. Capability Runtime 的首轮工具命中率足够高，避免频繁使用 `request_tools`。

### 1.2 3.6.0 性能发布门槛

以下指标必须用脱敏后的真实流量回放验证：

- 无图片、无显式远程语义检索的普通文本，进入 Main Agent 前的**生成式/路由模型**调用数必须为 `0`。
- Vision、Embedding 和后台 Attribution 分列计数；不得用“0 前置 LLM”掩盖它们的请求、Token 或延迟。
- 普通无 Tool 私聊的 Main Agent 请求数必须为 `1`。
- 普通无 Tool 私聊 P50 总延迟相对固定的 3.5.3 基线至少下降 `35%`。
- 普通无 Tool 私聊 P95 总延迟相对固定的 3.5.3 基线至少下降 `20%`。
- 本地 Capability Search P95 小于 `25 ms`。
- 常见能力首轮 Tool Recall@K 至少 `95%`；K、Schema Budget 和语料版本固定后计算。
- 全量 Tool 场景首轮 Tool Recall@K 至少 `90%`。
- 常见请求的 `request_tools` 使用率低于 `5%`。
- 全量 Tool 请求的 `request_tools` 使用率低于 `10%`。
- 记忆跨人物污染、跨群污染继续保持 `0`。
- Runtime 已观察到的记忆修改尝试/提交，其成功声明必须 `100%` 由真实 mutation receipt 支持；未进入写状态的自然语言轮次不得声称已产生持久化效果。

> **代码审阅批注（指标）**：`model_invocations` 当前没有 turn/conversation 关联，`tool_invocations` 也没有 turn id，端到端延迟主要存在于日志。R1 必须先补齐无正文的关联观测，再冻结基线；固定百分比门槛保留为发布目标，但以同一回放集、同一 Provider/Profile 和足够样本量进行成对比较。

未达到这些门槛，不发布 3.6.0。

---

## 2. 重构性质

这是一次**破坏性架构替换**，不是渐进式兼容改造。

### 2.1 明确禁止

- 不保留 `legacy_planner_enabled`。
- 不保留旧 Planner 与新 Runtime 双路径。
- 不建立 `from_legacy_plan()` 运行时适配器。
- 不把旧 `TurnPlan` 包装后继续使用。
- 不新增一个更小的 LLM Router 来替代 Planner。
- 不让 Namespace Search 调用 LLM。
- 不让 Tool Search 默认调用远程 embedding。
- 不保留 `Planner Scope` 作为权限概念。
- 不允许普通插件继续依赖 `PlannerSignal`。
- 不允许 Memory Runtime 继续依赖 `planner.models`。

旧 Planner 可以仅用于离线回放比较，不能进入 3.6.0 运行路径。

### 2.2 分支策略

```text
main
  -> 继续维护 3.5.x

refactor/3.6-runtime
  -> R1 Runtime Foundation
  -> R2 Memory Runtime
  -> R3 Capability Namespace Runtime
  -> R4 Conversation Runtime
  -> R5 Planner Purge + 3.6.0
```

五轮开发可以分 PR 审阅，但合并后的 3.6.0 只允许新架构存在。

---

## 3. 当前架构问题

### 3.1 Planner 已成为中央智能节点

当前 `planner/models.py` 的 `TurnPlan` 同时承载：

- `reply / silent / wait`
- intent
- target user
- delivery mode
- desired messages
- reply target
- tool mode 与 scopes
- memory context
- emoji plan
- voice plan

`services/processor.py` 在普通聊天前调用 `_plan_turn()`，Planner 甚至可能先 `WAIT`，再发起第二次 Planner 请求。之后 `ChatService.respond()` 再依据 `PlannedTurn` 构造 Memory、Tool、Voice、Emoji、Web 和 Delivery 状态。

这造成三类问题：

1. 成本叠加：Planner 为每个进入规划的轮次固定增加网络 RTT、输入/输出 Token 和失败面；相对 Main Agent 的实际成本由 R1 基线测量，不预设结论。
2. 扩展困难：新增能力会扩大 Planner Prompt 和输出 Schema。
3. 所有权混乱：Memory、Tool、Delivery 的规则被 Planner 输出间接驱动。

### 3.2 Memory 与 Planner 强耦合

3.5.3 已建立四种互斥路径：

```text
none / automatic / tool / mutation
```

当前行为很有价值：

- automatic 首轮不暴露 Memory Scope。
- tool 不自动注入。
- mutation 只开放写能力。
- mutation 最终文本由真实 receipt 决定。
- 自动召回有 background、continuation、focused、overview 的独立预算。
- Recall Receipt、Activation、异步 Attribution 已经成熟。

问题不在四种行为，而在于它们目前由 Planner 决定，并由 `chat.py` 中大量分支执行。`MemoryAccessMode` 同时混合了上下文注入、读工具曝光、写事务、最终回答规则四个维度。

### 3.3 Capability 已有 Runtime 雏形，但仍受 Planner Scope 控制

Yuki 已经具备：

- `CapabilityDescriptor`
- `CapabilityPolicyEngine`
- `UnifiedToolCatalog`
- `ToolProviderRegistry`
- Schema Budget
- `request_tools`
- Core、Plugin、Automation、Admin、MCP 的统一 Binding
- 读操作并行、重复只读结果复用
- mutation commit 判定
- Tool Artifact
- Responses continuation

执行层已经成熟。真正需要替换的是 Exposure/Search 层：

```text
Planner Scope
  -> local selector
  -> 可选 Flash Tool Reranker
  -> selected_tool_names
```

3.6.0 将其替换为：

```text
真实权限
  -> Semantic Namespace
  -> Exact/Alias/FTS5 BM25
  -> Schema Budget
  -> Main Agent
```

---

## 4. 外部成熟设计确认

本计划只吸收成熟思想，不引入新的重型框架依赖。以下官方页面已于 2026-08-17 复核；外部建议是设计参考，不覆盖 Yuki 的 Provider/权限/Token 实测合同。

### 4.1 OpenAI Agents SDK

官方 Agents SDK 的核心是少量基础对象：Agent、Runner、Tools、Handoffs/Agents-as-tools、Guardrails。Runner 负责模型与工具循环，并不要求所有请求先经过独立 Planner。Function Tool 使用 Pydantic 生成和校验输入结构。

3.6.0 采用：

- 单 Main Agent + 受控 Agent Runner。
- 专家 Agent 只作为未来的可选 Tool，不放在主路径前面。
- Tool Namespace 作为高层语义目录。
- 大量 Tool 延迟曝光。
- Host 负责严格参数校验与权限判断。

OpenAI 官方 Tool Search 还建议尽量采用 Namespace，并建议单个 Namespace 保持较小，理想情况下少于 10 个函数。Yuki 将采用同一思想，但搜索在本地 Runtime 完成，以保持模型供应商中立。

参考：

- https://openai.github.io/openai-agents-python/
- https://openai.github.io/openai-agents-python/agents/
- https://openai.github.io/openai-agents-python/tools/
- https://openai.github.io/openai-agents-python/handoffs/

### 4.2 Anthropic Tool Search 与 Tool Context

Anthropic Tool Search 适合大量 Tool 定义，官方建议通常保留 3 到 5 个高频 Tool，其余延迟加载；一致的服务或资源前缀有助于搜索。该数值是 Anthropic server-side deferred loading 的经验值，不是 Yuki 必须照搬的下限。

3.6.0 采用：

- 只常驻 `request_tools`；`get_my_capabilities` 经搜索加载，`read_tool_artifact` 在产生 handle 后加载。这是基于 Yuki provider-neutral 本地搜索和 Schema 预算作出的明确偏离。
- 其余 Tool 由本地搜索预取。
- `request_tools` 只用于漏检恢复。
- 稳定 Tool Prefix 保持固定排序，利于 Provider Prompt Cache。
- 长 Agent Loop 保留 Tool Artifact，并在后续版本增加 Tool Result Context Editing。

3.6.0 不采用 Programmatic Tool Calling。Anthropic 官方指出，它更适合大规模 fan-out 和大结果聚合；其公开评估中，每轮一到两个顺序 Tool 的场景成本约高 8%。Yuki 当前 QQ 场景以少量 Tool 为主，现有并行 Batch 已经足够；发布报告仍以自身回放验证，而不是外推该数字。

参考：

- https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling

### 4.3 Google ADK

Google ADK 将 LLM Agent 与确定性 Workflow 分开。Workflow 的顺序、并行、循环由预定义逻辑管理，不需要模型参与编排。

3.6.0 采用：

- Admission、权限、状态转换、发送、重试、事务独占由代码控制。
- 只有自然语言理解、回答和具体 Tool 选择交给 Main Agent。

参考：

- https://adk.dev/agents/workflow-agents/
- https://adk.dev/agents/llm-agents/

---

## 5. 3.6.0 最终架构

```text
+--------------------------------------+
| Inbound Event / Authorized Trigger   |
+-------------------+------------------+
                    |
                    v
+--------------------------------------+
| Origin-specific Coordinator         |
| direct / autonomous / scheduled /   |
| plugin session / plugin background  |
+-------------------+------------------+
                    |
                    v
+--------------------------------------+
| Turn Runtime Core                    |
| authority / token / lifecycle/taint |
+------+----------------+--------------+
       |                |
       v                v
+--------------+  +-------------------+
| Memory       |  | Capability        |
| Runtime      |  | Runtime           |
+------+-------+  +---------+---------+
       |                    |
       +----------+---------+
                  v
+--------------------------------------+
| AgentRunner <-> Main Agent           |
| bounded MODEL <-> TOOL loop         |
+-------------------+------------------+
                    |
          +---------+---------+
          v                   v
+------------------+  +----------------+
| Tool Kernel /    |  | Conversation   |
| Domain Services  |  | Effects/Delivery|
+------------------+  +----------------+
```

`ConversationRuntime` 只负责需要形成对话回复的路径。Scheduled Automation、Plugin Session、Plugin Background 保留各自领域协调器，通过共享 `TurnRuntimeCore` 复用 Authority、Capability Policy、Agent Loop 和 Effect Ledger；不得为了“统一”把它们伪装成 `InboundMessage`，也不得重建一个万能 Router。

### 5.1 依赖方向

```text
application/container
  -> origin-specific coordinator
      -> turn runtime core
          -> conversation runtime（仅回复型 turn）
          -> memory runtime
          -> capability runtime
          -> agent runner
          -> delivery services

memory runtime
  -> memory domain/services/repositories
  X  planner
  X  capability implementation

capability runtime
  -> capability descriptors/providers/policy
  -> shared TurnAuthority
  X  planner
  X  memory internals

agent runner
  -> provider-neutral model runtime
  -> AgentToolBackend protocol
  X  planner
```

Memory Runtime 通过纯数据对象提供 `MemoryCapabilityView`，Capability Runtime 只消费该视图，不读取 Memory 内部状态。

`ConversationTurnCoordinator` 继续是 token、version、supersede、task cancellation 和 mutation cancellation shield 的唯一权威；`TurnSession.phase` 仅表示本地生命周期，不能复制这些字段形成第二份“当前状态”。History identity、coordination/cancellation key 和 Memory scope key 是三个不同概念，禁止继续用一个 `conversation_key` 混用。

---

## 6. 新的 Turn 主流程

```text
1. normalize inbound event
2. admission / policy / rate limit / dedup
3. append raw event to ledger
4. create TurnContext and TurnAuthority
5. ConversationRuntime.begin_turn()
6. MemoryRuntime.begin_turn()
7. Memory policy derives a conservative initial contract from trusted origin/config
8. Memory passive prefetch runs when allowed; exposure is confirmed only when included in the real model request
9. CapabilityRuntime pins one authorized catalog revision for this turn
10. local metadata namespace + lexical Tool retrieval selects initial schemas
11. ContextAssembler builds Main Agent input
12. AgentRunner executes bounded MODEL <-> TOOL loop
13. Memory Runtime validates per-read handles and atomic write transitions
14. Conversation Runtime prepares effects and reply target
15. sender returns a typed delivery outcome/receipt
16. ledger records confirmed outbound
17. immutable Memory attribution jobs run asynchronously after confirmed delivery
18. turn closes; background attribution no longer owns the live Session
```

整个流程中，步骤 1 到 10 不允许调用前置生成式 Router/Planner LLM。图片轮次的 Vision 请求和 Memory Semantic Embedding 若发生，必须作为独立依赖记录；R2 为隔离架构风险沿用现有 hybrid Memory 检索并计入前置延迟。lexical-first/有界 semantic 级联是独立 R2.1 性能任务，必须用冻结 Memory Quality 回放单独验收，不能夹带进 Runtime 所有权迁移。

---

## 7. Memory Runtime 规范

### 7.1 四种旧模式不再作为基础枚举

旧行为映射为新的 Runtime Profile：

| 旧行为 | 新 Profile | Context | Read | 当前 Write | Write Transition | Finalization |
|---|---|---|---|---|---|---|
| none | dormant | none | deferred | disabled | requestable/denied（按 authority） | normal |
| automatic | passive | background/continuation | deferred | disabled | requestable/denied（按 authority） | normal |
| tool | active-read | none | eager | disabled | requestable/denied（按 authority） | normal |
| mutation | exclusive-write | none | locator-only | exclusive | already-exclusive | receipt-gated |

3.6.0 不保留 `MemoryAccessMode` 作为业务分支依据。

### 7.2 正交 Contract

```python
MemoryTurnContract(
    context_policy=MemoryContextPolicy,
    read_policy=MemoryReadPolicy,
    write_policy=MemoryWritePolicy,
    write_transition=MemoryWriteTransition,
    finalization_policy=MemoryFinalizationPolicy,
    availability=MemoryAvailability,
)
```

锁定枚举：

```text
MemoryContextPolicy:
  none / background / continuation

MemoryReadPolicy:
  denied / deferred / eager / locator_only

MemoryWritePolicy:
  disabled / exclusive

MemoryWriteTransition:
  denied / requestable / already_exclusive

MemoryFinalizationPolicy:
  normal / receipt_gated

MemoryAvailability:
  enabled / forbidden
```

`dormant` 只表示当前未预取、未预载，不等于禁止。真正禁止 Memory 必须由 `MemoryAvailability.FORBIDDEN` 表达。

> **代码审阅批注（合同）**：原合同用 `write_policy=disabled` 同时表示“此刻不能写”和“永远不能申请写”，导致 passive/dormant 的 `memory.state.write` 可请求性自相矛盾。新增 transition 维度后，当前执行权限与后续状态转换分离。合同构造器必须拒绝非法组合：`FORBIDDEN` 必须是 context none/read denied/write disabled/transition denied/finalization normal；`EXCLUSIVE` 必须 context none + already-exclusive + receipt-gated，且反向成立；`LOCATOR_ONLY` 只允许处于 mutation locator 阶段。

### 7.3 MemoryTurnSession 状态机

```text
Turn lifecycle: OPEN -> PREPARED -> AGENT_LOOP -> DELIVERY_DECIDED
                -> DELIVERED | DELIVERY_FAILED | CANCELLED | SUPERSEDED -> CLOSED

Memory access:  DORMANT | PREFETCHED
                -> READ_ACTIVE (可重复，每次产生独立 RecallHandle)
                -> MUTATION_EXCLUSIVE
                   -> LOCATOR_READ_ENABLED -> LOCATOR_READ_DONE -> MUTATION_EXCLUSIVE

Mutation outcome: NOT_ATTEMPTED -> ATTEMPTED
                  -> COMMITTED | COMMITTED_AS_CONTESTED | DEDUPLICATED
                  -> NO_CHANGE | REJECTED | AMBIGUOUS | NOT_FOUND

Attribution: NONE -> EXPOSURE_FROZEN -> QUEUED -> USED/NO_USED/FAILED
```

硬规则：

- `MUTATION_EXCLUSIVE` 内不得执行其他业务写操作。
- mutation 与其他 side-effect Tool 同 Batch 时拒绝整个冲突部分。
- mutation 提交/终止结果由 `MemoryFinalizer` 直接生成确定性正文并结束 Agent Loop；只有 locator `AMBIGUOUS/NOT_FOUND` 允许有界读取后重试。
- 未取得真实 receipt 时不得声称修改成功。
- locator 歧义最多返回合法作用域内的候选。
- 跨人物、跨群、SELF 可见性不由模型参数决定。
- automatic context、agent read、plugin read、admin read 复用纯查询内核，但只有真正进入模型 payload 的 automatic/agent exposure 产生 Receipt 与 `last_injected_at`；Plugin/Admin 保持纯读取。
- Main Agent 的 Memory Tool 参数表达“查什么”，Runtime Contract 表达“允许怎么查”。

### 7.4 访问判定

`MemoryAccessPolicy` 是纯本地的**权限与保守初始策略**，不是自然语言意图分类器：

- 可信 direct command/结构化事件可直接进入 `exclusive-write`；普通自然语言不得靠固定中文短语、substring 或 regex 判定 write/read/overview。
- 普通对话进入 `passive`，使用中性 `background` intent 和小额 prefetch；R2 保持现有 hybrid 检索并单列 Embedding，它不能凭本地规则发明 entity、absolute time range、overview 或 current-self intent。
- Main Agent 理解语义后，通过实际 memory read Tool 提交 `purpose/mode/subject_ref/entities/absolute range/preferred kinds/limit`；后端解析真实目标并限制范围。
- Main Agent 请求 `memory.state.write` 时，Session 先按 authority 原子切换到 `MUTATION_EXCLUSIVE`，再授权执行；已被 passive prefetch 看过的 fact 不能充当 mutation target authority。
- 不可信外部事件、图片写隔离等场景由 origin/authority 决定 forbidden 或 write denied。

不得引入新的 LLM Memory Router。

> **代码审阅批注（语义缺口）**：删除 Planner 后，Runtime 无法仅靠本地规则完整恢复 Planner 的 entity、temporal、overview、verify/correct 语义。正确补位点是 Main Agent 的受约束 read/write Tool 参数，而不是维护多语言关键词字典。DeepSeek 路径也不得依赖 `tool_choice=required`；没有写 Tool/receipt 的轮次只能给普通回答，不能由后端伪造成功。

---

## 8. Capability Namespace 规范

### 8.1 Namespace 与 Provider 分离

```text
namespace = github.issue
provider = mcp.github
```

```text
namespace = music.search
provider = plugin.netease
```

模型关注能力语义，Runtime 关注 Provider、Trust、权限和执行位置。

### 8.2 Namespace 结构示例

```text
github
  github.repo
  github.issue
  github.pull_request
  github.code

memory
  memory.person.read
  memory.self.read
  memory.group.read
  memory.history.read
  memory.state.write

automation
  automation.read
  automation.write

qq
  qq.group
  qq.friend
  qq.message
```

叶 Namespace 理想情况下不超过 10 个 Tool。父 Namespace 用于搜索和分类，不直接成为权限。

### 8.3 搜索索引

每个 Tool Document 至少包含：

```text
model_name
canonical_name
namespace_id
namespace_path
namespace_description
description
aliases
tags
parameter_names
parameter_descriptions
provider_id
trust_source
risk
effect
estimated_schema_tokens
```

3.6.0 使用“语义元数据 + 本地词法检索”，不把 FTS5 误称为语义模型：

```text
exact name / alias map
  +
SQLite FTS5 BM25 in-memory index
  + CJK 2-4 gram / ASCII term normalization
  +
namespace soft score
  +
tool soft score
  +
Schema Budget
```

不使用远程 embedding，不使用 Flash Tool Reranker，不使用 LLM Tool Router。

Catalog 必须拆成长期 `DescriptorRegistry` 与 per-turn `AuthorizedCatalogSnapshot`：Plugin/MCP refresh 以 copy-on-write 原子发布新 revision，在途 turn 固定使用一个 revision；任何按用户权限过滤后的结果都不得跨 turn 缓存。Revision 必须对 canonical JSON Schema、name/description/namespace/aliases/tags/use_when 和 provider metadata version 取指纹，不能只信任手写 `schema_version`。

Namespace 只提供排序加分，不能硬排除其他高相关 Tool。正确 Tool 必须能凭自身描述进入结果，避免 Namespace 误判成为新的单点故障。

### 8.4 初始 Tool 集合

稳定 Kernel Tool 锁定为：

- `request_tools`

`get_my_capabilities` 经语义命中/请求后曝光，`read_tool_artifact` 仅在本轮产生 handle 后曝光，`set_reply_target` 仅在存在可验证 event 时曝光。

业务 Tool：本地索引先取稳定 Top 10，authority 与 Schema Token Budget 最多选 8 个非常驻能力，首轮 function/native Tool 合计硬上限 12；默认值和确定性裁剪规则由 R3 冻结。

`request_tools` 与首轮预取必须调用同一个 `CapabilitySearchIndex`。两者只允许 query 来源不同。

### 8.5 Responses continuation

建立两个不同的账本：

- `DeclaredSchemaLedger`：Responses continuation 内已声明 Schema 单调追加。
- `CallableCapabilitySet`：每次执行前重新按 Authority、Memory exclusive transition 和 taint 求交集，可收缩。

因此“已声明但后来撤权”的 Tool 仍可能出现在 Provider continuation 中，但调用必须稳定返回 `capability_no_longer_authorized`，绝不能执行。Chat Completions 每次请求允许发送当前最小 Schema 集，不强制为协议统一而单调膨胀。

`DeclaredSchemaLedger` 规则：

- 初始 Tool Snapshot 固定。
- 动态 Tool 只追加。
- 按 canonical name 稳定排序。
- 不重复声明。
- 每轮记录累计 Schema Token。
- 超过 turn-level 最大累计预算时拒绝继续加载，并要求 Main Agent使用已加载能力完成或说明限制。

---

## 9. Conversation Runtime 规范

### 9.1 Admission

Admission 分成两个所有者：

- `InboundMessagePolicy`：保留 `services/policies.py` 的 private/group/mention/direct-command/disabled 判定，并新增“回复 Yuki”显式分支。现状“回复 Yuki”直达准入不在 `policies.py`，而在 `planner/necessity.py` 的 forced 逻辑（private/reply_target_is_bot/mentions_bot），R4 必须把它迁为 `InboundMessagePolicy` 的显式判定。
- `AutonomousParticipationPolicy`：迁移 `ReplyNecessityScorer` 的群聊自主参与评分，并接收有界 `AdmissionSignal`。

规则：

- 私聊、回复 Yuki、明确 @ Yuki：由 `InboundMessagePolicy` 直接进入 Main Agent，不经过内容评分。注意这是双重变化：3.5.3 对此类轮次仍运行 `ReplyNecessityScorer.score()`（forced 通过阈值门但记录 necessity_score），3.6.0 起 direct 轮次不再产生分数样本，基线对比 admission 指标时必须注明口径差异。
- 群聊自主参与：纯本地评分。
- 低分：不响应，0 LLM。
- 高分：进入 Main Agent。
- 3.6.0 不设置灰区 LLM Judge。

### 9.2 WAIT

Planner 的 WAIT 删除。群聊自主参与继续由现有 `AutonomousGroupService` 的 per-group coalescing、revision、debounce 和 changed-event handoff 等待；direct/private turn 不增加等待。`ConversationTurnCoordinator` 只负责 version、supersede、task cancellation 和 mutation protection，不承担历史聚合。

### 9.3 Voice、Emoji、Delivery

- 明确的结构化命令可直接曝光 speech/emoji capability；普通自然语言不维护固定短语表，使用同一 Capability Search 的 metadata 作为 soft hint，由 Main Agent 调用真实 effect Tool。
- 自发表情/语音：只能由 Main Agent 的真实 `send_emoji`/`send_voice` Tool Call 请求，再由 Runtime 频率与可用性门决定，不能解析正文 hint，也不能增加前置 LLM。
- 普通文本发送数量由现有消息分块和 ReplySequence 决定，不再由 Planner 预测。
- 默认引用目标由 Conversation Runtime 判断；复杂多人场景保留 `set_reply_target` Tool。
- Automation 仅由确定性时间结构/显式命令或本地 metadata 检索提高 capability 搜索分数，是否调用由 Main Agent 决定。

> **代码审阅批注（自然语言路由）**：Memory、Voice、Emoji、Automation 若各自建立中文关键词 parser，会把被删除的 Planner 拆成多个脆弱 Router。Runtime 可以识别精确定义的命令语法和可信事件字段；其余自然语言只影响本地能力检索的软分数，最终动作必须通过 Main Agent 的实际 Tool Call 与后端回执。

---

## 10. 保留的成熟组件

以下组件不应因删除 Planner 被重写：

- `AgentRunner` 的 provider-neutral bounded loop。
- DeepSeek Responses `ProviderContinuation` 适配。
- Tool Batch 去重与只读结果复用。
- parallel-safe 调用协调。
- Tool Result Budget 与 Artifact。
- Capability effect/risk/trust/permission 元数据。
- Memory Fact、Evidence、Conflict、Mutation、Receipt、Activation、Attribution。
- Event Ledger。
- ConversationTurnCoordinator。
- confirmed delivery receipt 后写账本。

重构重点是重新划分所有权，不是重写已经稳定的数据面和执行面。

---

## 11. 五轮开发

| 轮次 | 目标 | 主要删除时点 |
|---|---|---|
| R1 | 建立最终 Runtime 骨架、Turn 类型、依赖规则、基准测试 | 不删除生产 Planner，但新代码禁止依赖 Planner |
| R2 | 整体抽出 Memory Runtime，建立 MemoryTurnSession | 删除业务路径中的 `MemoryAccessMode` 与 Planner-owned memory semantics |
| R3 | 建立 Capability Namespace Runtime 与本地 Tool Search | 删除 Planner Scope、Flash Tool Reranker 热路径、旧 selector 所有权 |
| R4 | 建立 Conversation Runtime，替代 Planner 的回复、等待、效果与发送决策 | MessageProcessor 主路径不再调用 Planner |
| R5 | 物理删除 Planner、数据库表、配置、模型路由与测试，发布 3.6.0 | 删除整个 `src/qq_ai_bot/planner/` |

R1-R5 的 PR 只合并到 3.6 feature branch，任何半迁移轮次都不单独进入 `main`。这不是长期双轨：

- R2 合并时，Memory Runtime 原子成为唯一记忆编排者，并从 Planner prompt/TurnPlan 停止生成或消费 memory 字段。
- R3 合并时，Capability Runtime 原子成为唯一 Tool exposure 编排者，同时删除 Planner tool scope、Flash reranker 与 `ModelTask.TOOL_SELECTION`。
- R4 切换 Conversation 主路径，Planner 不再运行。
- R5 只做物理清除、配置/数据库迁移和发布核验，不重复实现 R2/R3 的所有权转移。

详细文档：

1. [R1 Runtime Foundation](./01-R1-RUNTIME-FOUNDATION.md)
2. [R2 Memory Runtime](./02-R2-MEMORY-RUNTIME.md)
3. [R3 Capability Namespace Runtime](./03-R3-CAPABILITY-NAMESPACE-RUNTIME.md)
4. [R4 Conversation Runtime](./04-R4-CONVERSATION-RUNTIME.md)
5. [R5 Planner Purge and 3.6.0 Release](./05-R5-PLANNER-PURGE-AND-RELEASE.md)
6. [代码核对审阅决议](./06-CODE-REVIEW-DECISIONS.md)

---

## 12. 测试与评估总矩阵

### 12.1 回放集

从合成或人工匿名化文本构造至少 600 个版本化案例；真实流量只在受控离线环境临时回放，发布物仅保存 corpus hash 与聚合指标：

- 普通私聊 100
- 普通私聊 + background memory 100
- 显式 Memory read 80
- Memory mutation 60
- 单 Tool 80
- 多 Tool 40
- 群聊不响应/响应 80
- Plugin/MCP 60

每个案例保存预期：

- 是否响应
- 允许的能力
- 首轮 Tool 集合
- Memory Contract
- Tool Calls
- mutation receipt
- 最终发送结果
- 模型请求数
- Token 和延迟

记录 corpus/version SHA、标注版本、Provider/Profile、并发、硬件、冷/热缓存和指标定义。R3 的 300 条 Capability Query 是独立 search holdout，可与 turn 回放共享语句但不得混用分母。Admission 必须同时标注 precision、recall、false-intervention 和 response-rate delta。

### 12.2 安全回归

- 跨人物记忆污染为 0。
- 跨群记忆污染为 0。
- SELF visibility 泄漏为 0。
- 模型伪造 user_id/group_id 无效。
- 未加载 Tool 不能直接执行。
- 不可信 external event 不能获得 write capability。
- 图片轮次 memory write 隔离不弱于 3.5.3；R2 将其收紧为图片轮 write=disabled（3.5.3 仅拦截带图的显式写命令，`memory_change` 与 Planner MUTATION 未按图片轮硬关），收紧差异必须在回放与 release note 标注。
- memory mutation 与其他 side effect 互斥。
- mutation terminal receipt 直接由 MemoryFinalizer 生成确定性文本，不发起一轮其正文必然被丢弃的最终模型请求。
- Responses continuation 内 `DeclaredSchemaLedger` 单调增加且不重复；`CallableCapabilitySet` 可按 Authority/taint/exclusive transition 收缩，撤权工具绝不执行。

### 12.3 架构测试

新增静态 import 检查：

```text
memory/runtime     X planner
capabilities       X planner
conversation       X planner
agent_runner       X planner
plugin SDK         X planner
```

R5 后：

```bash
rg -n "Planner|planner" src/qq_ai_bot src/yuki_plugin_sdk
```

运行时引用必须为 0。

---

## 13. 数据库与版本迁移

3.6.0 使用有备份门的破坏性 Alembic 迁移，但先迁移仍有业务价值的数据：

- 以 0036 为基线：R1/0037 建立 turn correlation，R3/0038 迁移 Plugin approvals，R4/0039 新建 `reply_effect_events` 并回填 cadence；Runtime 切换为只读新 owner 后，R5/0040 才删除 `planner_runs` 表及索引。
- Planner 模型专属配置删除；group debounce、reply necessity threshold、recent presence、interrupt autonomous、reply hard max 等仍有 Runtime 所有者的值按 scope 原子迁到 `conversation.*` / `reply.*`。
- 删除 Planner ModelTask/Route。
- 新增 Runtime/Capability observability 表时，只存元数据，不存聊天正文。
- Plugin API 升级为 `2.0`。
- Memory Query frozen contract 由 `5` 升到 `6`（数量从 semantic intent 移到 consumer request）；Plugin Memory Facade 保持 `1.0`。
- `ToolMetadata` 新增 namespace、aliases、search tags/use-when 等字段。
- 删除 `register_planner_signal`，新增 `register_admission_signal`。

严格配置加载前必须运行一次 3.5.3 -> 3.6.0 配置升级器：备份并原子改写 `config/model_profiles.toml`，移除 `planner`/`tool_selection` route；否则当前 `ModelTask(task_name)` 会因未知 route 令应用在 Alembic 之前启动失败。`.env.example`、Guided Setup、Admin overrides、健康检查和 Release bundle 同步迁移。历史 Alembic 文件中的 `planner` 文本属于迁移史，不受运行时代码“零匹配”门限制。

升级文档必须要求先备份 `data/qq_ai_bot.db`、WAL/SHM 和受影响配置。迁移完成后不支持回退到 3.5.3 继续写同一数据库；回退必须恢复同一套升级前快照。

---

## 14. Codex 审阅协议

每一轮开始前，Codex 必须先做代码审阅，不得只按本文机械改名。

### 14.1 全局重点文件

```text
src/qq_ai_bot/services/processor.py
src/qq_ai_bot/services/chat.py
src/qq_ai_bot/services/agent_runner.py
src/qq_ai_bot/services/agent_tools.py
src/qq_ai_bot/services/context_assembler.py
src/qq_ai_bot/services/prompt_composer.py
src/qq_ai_bot/services/reply_sequence.py
src/qq_ai_bot/services/turn_coordinator.py
src/qq_ai_bot/application/modules/conversation.py
src/qq_ai_bot/container.py
src/qq_ai_bot/planner/*
src/qq_ai_bot/memory/*
src/qq_ai_bot/capabilities/*
src/yuki_plugin_sdk/*
```

### 14.2 Codex 每轮必须回答

1. 旧 Planner 的哪些行为没有被本文列出？
2. 哪些行为实际属于安全规则，不能简单删除？
3. 哪些状态由真实 Event、DB、Receipt 提供，哪些只是模型提示？
4. 哪些逻辑存在于测试而非注释中？
5. 哪些分支会影响 Autonomous Group、Plugin Background、Scheduled Automation？
6. 哪些 Provider 对 Tool Schema 变化和 continuation 有特殊要求？
7. 哪些配置、CLI、管理接口、数据库列仍引用 Planner？
8. 哪些插件 API 会发生破坏性变化？
9. 当前线上指标能否为性能门槛提供基线？
10. 本轮结束后，是否出现新的双重所有权？

### 14.3 审阅输出格式

每轮实现前提交一份：

```text
code-review-findings.md
  - discovered behavior
  - hidden invariant
  - affected files
  - test evidence
  - deviation from plan
  - final implementation decision
```

发现本文遗漏时，可以修订实现细节，但不得改变总纲中的核心规则：无强制 Planner、无前置 Router LLM、Memory Runtime 独立、Capability Namespace 本地搜索、Runtime 掌握权限和事务。

---

## 15. 最终完成定义

3.6.0 只有同时满足以下条件才算完成：

- 普通、非视觉、成功首尝试的文本轮次进入 Main Agent 前没有生成式 Router/Planner 请求；Vision/Embedding/后台 Attribution 独立计量。
- `src/qq_ai_bot/planner/` 已删除。
- `ModelTask.PLANNER` 和 `ModelTask.TOOL_SELECTION` 已删除。
- Planner 配置、环境变量、CLI、管理命令、数据库表已删除。
- Memory Runtime 不依赖 Planner 或 Capability 实现。
- Capability Runtime 不依赖 Planner。
- Namespace 与 Provider 已分离。
- Tool Search 全程本地执行。
- Plugin 新增 Tool 不需要修改 Core Runtime。
- Memory read/write 互斥规则由 Runtime 状态机和 batch preflight 强制执行；Responses 已声明 Schema 与当前可调用集合明确分离。
- Mutation 最终声明只来自真实 receipt。
- AgentRunner 与 Responses continuation 回归通过。
- Memory Quality 全部通过，污染门为 0。
- 性能发布门槛全部通过。
- 全量 `ruff`、`mypy`、`pytest` 通过。
- 发布版本为 `3.6.0`，Plugin API 为 `2.0`。
- 真实 3.5.3 部署包（数据库、model profile、runtime overrides、插件批准状态）完成 source-free Docker 升级演练。

这次重构的最终原则是：

```text
Runtime 决定能不能做
Memory Runtime 决定记忆如何进入和改变
Capability Runtime 决定给模型看哪些能力
Main Agent 决定当前要做什么
```

Planner 不再站在每一轮请求之前。
