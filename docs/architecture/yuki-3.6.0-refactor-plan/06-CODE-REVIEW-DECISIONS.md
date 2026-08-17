# Yuki 3.6.0 Planner 重构代码核对与审阅决议

> 状态：已按真实代码核对并回写总纲/R1-R5  
> 基线：`main@26954843c26f3849044c61ee945704242c4bde25`（tag `v3.5.3`）  
> 审阅日期：2026-08-17  
> 本文优先级：若旧段落与本文或各章中的“代码审阅批注”冲突，以修订后的合同为准。

## 1. 审阅结论

删除强制 Planner 的总方向成立，而且值得做。真正需要保留的不是 `TurnPlan` 这张大表，而是散落在 Planner、ChatService、AgentRunner、Memory、Capability、Delivery 和各 origin coordinator 中的安全不变量。

本次审阅没有采取“先包一层再慢慢替换”的保守路线，已直接锁定以下最终所有权：

```text
Host/Domain Coordinator  -> 可信 trigger、identity、origin、delegation
Turn Runtime Core        -> token/version/lifecycle/outcome/taint
Conversation Runtime     -> reply admission、effects、delivery
Memory Runtime           -> scope、read exposure、write transition、receipt
Capability Runtime       -> discovery、authority projection、schema/callability
AgentRunner              -> provider-neutral bounded MODEL <-> TOOL loop
Domain Services          -> durable transaction 与领域验证
```

Planner 不保留运行时兼容层，也不换成另一个前置 LLM Router。

## 2. 关键偏差与修订原因

| 原稿假设 | 真实代码/风险 | 最终决议 |
|---|---|---|
| 普通请求进入 Agent 前“没有任何模型调用” | Vision 在 Agent 前运行；hybrid Memory 可调用远程 Embedding；发送后还有 Attribution | 门定义为无前置 **generative/router LLM**；Vision/Embedding/后台模型单列 |
| 一个 `conversation_key` 足以表达 turn | History 是 `group:g:user:u`，Coordinator 是 `group:g`，Memory worker 又按群分区 | 分成 `ConversationIdentity`、`TurnCoordinationKey`、`ResolvedMemoryScope` |
| 所有 origin 都能用 `InboundMessage` | Scheduled、Plugin Session/Background 没有真实用户 inbound，当前甚至存在 synthetic inbound | 使用 discriminated `TurnTrigger`；各领域 coordinator 复用底层 core，不伪造消息 |
| MODEL -> TOOL -> FINALIZE 是线性状态 | AgentRunner 是 `MODEL -> (TOOL -> MODEL)*`，还有 retry/recovery/web fallback | Phase、Outcome、DurableEffectState 分离，并允许 MODEL/TOOL 循环 |
| 本地短语可替代 Memory/Voice/Emoji 意图 | 引用、否定、条件句、多语言会让 substring/regex 误判，且违背既有 Memory 规则 | 只识别精确定义 command/可信事件；普通自然语言交给 Main Agent + capability Tool |
| passive 的 `write=disabled` 同时能 request write | 当前执行权与未来 transition 被混成一个枚举，合同自相矛盾 | 新增 `MemoryWriteTransition`，与 `MemoryWritePolicy` 正交 |
| 一个 recall turn id/intent 足够 | Agent 可多次 read，automatic/tool 的 purpose 与强化 alpha 不同 | 使用 `RecallLedger`，每次读取有独立 `RecallHandle` |
| Receipt 现有 `turn_id` 可直接复用为整轮 ID | 该列实际上是每次 Recall Receipt 的唯一 ID；一轮可有多个 | 保留其 `receipt_turn_id` 语义，另加 nullable `runtime_turn_id` 做整轮关联 |
| 所有 Memory query 都走 Recall Receipt | Plugin/Admin 是纯读取；prefetch 也不等于真正进入模型 | Query 纯读；仅真实 model payload 中的 automatic/agent exposure 写 Receipt |
| `requested_count` 属于语义 intent | 数量实际由 automatic/tool/admin/plugin 各 consumer 的预算决定 | 从 intent 移到 `MemoryReadRequest.requested_limit`，Memory Query contract 5→6；Plugin Facade 1.0 不变 |
| 统一一个通用 Memory gateway 更整齐 | 现有 person/group/self Tool 各有独立 target/visibility adapter | 保留 subject-specific Tool，内部统一 Query Plane，不让模型提交自由身份 scope |
| 全局 UnifiedToolCatalog 可以直接缓存 | Provider 当前按 turn/context 生成 Catalog，缓存权限投影会串用户 | `DescriptorRegistrySnapshot` 与 per-turn `AuthorizedCatalogSnapshot` 分层 |
| FTS5 BM25 天然支持中文 | 当前实现专门生成 CJK 2/3/4-gram；默认 tokenizer 命中不足 | 冻结 CJK n-gram materialization、转义、权重与 tie-break |
| MCP 空缓存仍能被 Tool Search 找到 | `prepare_scopes()` 才会连接 Planner 选中的 server；删除 Planner 后无入口 | 从配置建立 synthetic namespace entry，命中后 bounded metadata discovery |
| Tool Schema 永远单调即可满足 Memory exclusive | Responses 不能删已声明 schema，但权限必须能收缩；Chat Completions 无需膨胀 | 拆 `DeclaredSchemaLedger` 与 `CallableCapabilitySet` |
| `planner_runs` 是纯统计表，可直接 drop | `voice_cadence()` 仍从该表计算最近自发语音比例 | R4/0039 先建立 reply-effect cadence owner，R5/0040 再 drop |
| Alembic 能处理全部升级 | model routes 在只读 TOML；删除 enum 后旧 route 会在 DB migration 前使启动失败 | 安装器先运行可写 config migrator，再启动 Bot/Alembic |
| 删除 Planner enum 不影响历史调用记录 | `model_invocations.task` 存有 planner/tool_selection，Repository 会再转 enum | 历史读取使用 tolerant retired string，写入仍只接收现役 enum |
| `set_voice_preference` 是既有工具，仅换 namespace | 3.5.3 无此工具；偏好由 Planner `voice.preference_change` 字段输出、`processor` 调 `voice_preferences.apply()` 落库 | R4 明确它为新增 write capability，迁移原数据库写路径与回执语义 |
| “回复 Yuki”准入判定在 `services/policies.py` | 实际在 `planner/necessity.py` forced 逻辑；且 3.5.3 对 direct 轮次仍评分并记录 necessity_score | R4 把该判定迁为 `InboundMessagePolicy` 显式分支；direct 不评分作为行为+观测双重变化标注口径 |
| 图片写隔离是“保持现状” | 现状仅拦截带图的显式写命令（`image_write_isolated`）；`memory_change`/Planner MUTATION 未按图片轮硬关 | R2 的图片轮 write=disabled 定性为收紧，进入回放对比与 release note |
| 删除 `[routes] planner` 只影响 Planner 自身 | `ModelProfileCatalog` 对缺失 `memory_attribution` 按 `utility_structured -> planner` 回退，隐式依赖会静默断路由 | R5 配置迁移器在删除 planner route 前先物化仍依赖回退的 `memory_attribution` 显式路由 |
| 0037 加列即可端到端 join 一次 turn | `agent_actions`、`speech_generations`、`web_search_runs`、`memory_mutation_receipts`、`memory_tool_receipts` 均无 turn 关联 | 0037 范围最小化并显式声明；mutation/tool receipt 关联由 R2 决定，effect/speech 由 R4 `reply_effect_events` 承担 |

## 3. 代码证据

### 3.1 Turn、Authority 与生命周期

- `src/qq_ai_bot/domain/conversations.py`：历史隔离 identity。
- `src/qq_ai_bot/services/turn_coordinator.py`：群级 version/cancel/supersede、protected version、mutation shield；不负责 debounce。
- `src/qq_ai_bot/services/autonomous_groups.py`：真实 per-group debounce/coalescing/revision。
- `src/qq_ai_bot/automation/authority.py`：DelegatedAuthority 的 current permission、schema version、plugin provenance、origin 重验。
- `src/qq_ai_bot/services/agent_runner.py`：bounded MODEL/TOOL loop、empty retry、incomplete recovery、native web fallback、post-commit recovery。
- `src/qq_ai_bot/services/reply_sequence.py`：Planner-dependent delivery policy、partial/cancel、quote retry 和 hard max。

### 3.2 Memory

- `src/qq_ai_bot/memory/targets.py`：真实 sender/group/@/reply/member target resolution。
- `src/qq_ai_bot/memory/query.py`：Planner intent 的 entity/temporal/purpose 价值，以及“regex 不做 recall intent 分类”的既有边界。
- `src/qq_ai_bot/memory/retrieval.py`：hybrid 在 lexical quality 之前调用 Embedding；candidate source count 与 unique fact 不同。
- `src/qq_ai_bot/memory/repository.py`：同群人物 evidence projection、mark injected、副作用边界和 Activation 同事务创建。
- `src/qq_ai_bot/memory/context.py`、`receipt.py`、`activation.py`：candidate/selected/injected/used/reinforced 与 CAS/事务边界。
- `src/qq_ai_bot/memory/attribution.py`：当前 job 只有一个 intent，且 Worker 在发送后异步运行。
- `src/qq_ai_bot/memory/mutation/service.py`：durable mutation transaction、receipt reserve/apply/finalize、Dream 外部 session 与 commit 后 embedding。

### 3.3 Capability、MCP 与 Plugin

- `src/qq_ai_bot/capabilities/catalog.py`：当前 revision 摘要与 schema token estimator。
- `src/qq_ai_bot/capabilities/provider.py`：Catalog 逐 context 生成、Core metadata 默认分类风险。
- `src/qq_ai_bot/capabilities/request.py`：现有中文 2/3/4-gram query 处理。
- `src/qq_ai_bot/services/agent_runner.py`：Responses continuation 已声明 Tool 的协议约束。
- `src/qq_ai_bot/mcp/provider.py`：Planner scope 驱动的 lazy metadata 入口。
- `src/qq_ai_bot/mcp/descriptors.py`：远端 annotations 当前影响 effect/risk，必须改为 Host policy 优先。
- `src/yuki_plugin_sdk/*`、`src/qq_ai_bot/plugin_host/*`：PlannerSignal、Prompt Stage/Target、permissions、extension kind 和 approval hash 的完整 API 2.0 迁移面。

### 3.4 配置、数据库与发布

- `src/qq_ai_bot/model_runtime/profiles.py`：对旧 route 执行 `ModelTask(task_name)`，删 enum 后会 fail-fast。
- `src/qq_ai_bot/model_runtime/repository.py`：历史 task 字符串再次转 enum 的兼容风险。
- `src/qq_ai_bot/planner/repository.py`、`planner/context.py`：`planner_runs.voice_cadence()` 的业务用途。
- `scripts/start.sh`、`src/qq_ai_bot/cli.py`：容器启动前自动 Alembic upgrade，备份门必须更早。
- `docker-compose.yml`：config 挂载只读，Bot 不能原地迁移文件配置。
- `scripts/release_validate.py`、`scripts/release_smoke.py`、`src/qq_ai_bot/memory/quality/release_check.py`：3.5.3/0036/Plugin API 1.1 的发布魔数。

## 4. 锁定合同

### 4.1 不依赖新的前置 Router

- Main Agent 自己理解开放域自然语言。
- Runtime 只处理可信事件、精确 command、权限、可用性、事务和回执。
- Capability Search 使用语义元数据的本地词法检索，不使用 LLM/remote embedding。
- Memory hybrid Embedding 暂时保留，单独度量；lexical-first cascade 另立 R2.1，不能夹带进所有权迁移。
- DeepSeek 请求不发送任何 `tool_choice` 字段；任何 Provider 都不依赖 `tool_choice=required`。

### 4.2 Memory 写入成功声明

Runtime 能严格保证的是：所有已观察到的 memory write Tool attempt/commit 都由 receipt gate 收束。没有进入写状态、没有实际 Tool Call 的普通自然语言轮次，Main Agent Prompt 明确禁止声称持久化成功；后端不能凭关键词或模型正文伪造 receipt。

这比原稿宣称“识别所有自然语言修改请求并 100% gate”更诚实：在不增加语义 Router、也不强制 Tool Call 的前提下，Runtime 不可能证明模型漏掉的隐含意图。验收应测 mutation request recall 和 false success，而不是写一个数学上不可达的绝对承诺。

### 4.3 Memory 查询与归因

```text
pure query
  -> consumer budget
  -> frozen model payload
  -> AgentRunner confirms actual exposure
  -> Receipt injected
  -> DeliveryOutcome confirms agent body/derived voice
  -> immutable attribution job
  -> used / Activation CAS
```

Plugin/Admin 不进入 exposure/attribution。一个 turn 可包含多个 RecallHandle；worker 按 handle 的 purpose/alpha 分组强化。Session 在 handoff 后关闭，Worker 不持有 live Session。

### 4.4 Capability Schema 与权限

```text
Registry revision (metadata/schema)
  -> Authority/availability projection
  -> Local search + Schema Budget
  -> DeclaredSchemaLedger (provider contract)
  -> CallableCapabilitySet (runtime contract)
  -> central schema validation
  -> domain validation
  -> binding
```

已声明但撤权的调用返回 `capability_no_longer_authorized`。MCP annotations、Plugin namespace 和模型参数都不能扩大权限。

### 4.5 Conversation 与其他 Origin

- Direct/private/@/reply 由 InboundMessagePolicy 决定，不走内容评分。
- Autonomous observation 才使用本地 Participation Policy；高分进入一次 Main Agent，仅该 origin 曝光无副作用的 terminal `decline_reply` Tool，调用后不再发 continuation。
- Scheduled、Plugin Session、Plugin Background 保留领域 coordinator，复用 TurnRuntimeCore，不统一成假用户消息。
- Voice、Emoji、Reply Layout、Reply Target 使用受约束 reply-effect Tool；仅结构化 direct command 可直达。
- 语音保留 canonical `send_voice` 并扩展 `mode`；新表情能力使用 `send_emoji`。两者都只接受真实 Tool Call，不解析正文 hint。
- DeliveryOutcome 是发送与 Attribution 的事实来源。

## 5. 迁移顺序门

```text
R1: typed trigger/authority/key/lifecycle + turn correlation baseline
R2: Memory ownership atomic transfer; stop Planner memory ownership
R3: Capability ownership atomic transfer; delete TOOL_SELECTION and migrate API 2.0
R4: direct/autonomous conversation switch + reply-effect cadence owner
R5-preflight: writable config migration + consistent SQLite backup + verify R3 approval state
0037 (R1): turn correlation + runtime observations
0038 (R3): Plugin API 2.0 approval cleanup/revocation
0039 (R4): reply_effect_events + cadence backfill + owner switch
0040 (R5): planner_runs drop + override mapping
R5: Planner package/PLANNER task purge + source-free upgrade smoke + release
```

R1-R5 只进入 feature branch；任何中间态不单独发布到 `main`。

## 6. 发布门的可复现定义

- 600 turn replay 与 300 Capability Query 均版本化，拆 tune/release holdout。
- 仓库只提交合成/人工匿名化文本；真实流量不写入 Git、DB、日志或 CI artifact。
- 指标记录样本量、语料 SHA、Provider/Profile、硬件/地区、并发、warmup、cache 状态和置信区间。
- Capability hit 固定 K、Schema Budget、required-tool set、token estimator version；`request_tools` 分母仅为需要业务 Tool 的 turn。
- Admission 同时发布 precision、recall、false-intervention rate、response-rate delta。
- 无 Tool 黄金用例恰好 1 个 foreground Agent request；单 Tool 首轮命中黄金用例恰好 2 个；一般 loop 只受 max-model-requests 上限约束。
- Vision、Embedding、Memory Extraction、Emoji Selector、Attribution 分列调用数和延迟。
- 真实 3.5.3 source-free bundle + 0036 DB + 旧 config/plugin approvals 完成 0037-0040、3.6.0 启动、备份与整套快照回退演练。

## 7. 开工前仍需冻结的产物

这些不是架构未决，而是必须由 R1 baseline 生成的实测输入：

1. 3.5.3 turn-correlation 基线数据与回放语料 SHA。
2. 300 条 Capability Search 的 release holdout 与标注工具集合。
3. Autonomous Participation 的误插话/漏回复标注集。
4. 真实 3.5.3 deployment/config/DB 的脱敏升级 fixture。
5. Plugin API 2.0 内置插件迁移清单和重新批准 UX 文案。

缺少这些产物时可以实现 R1 的合同和观测，但不能宣称性能、命中率或发布门已通过。
