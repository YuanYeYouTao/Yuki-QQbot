# Yuki 3.6.1 会话历史压缩与分层 Rollup 开发任务书

> 文档状态：实施基准稿（2026-08-19 对照 3.6.0 代码修订）  
> 目标版本：3.6.1  
> 基线版本：3.6.0  
> 建议开发分支：`feature/3.6.1-conversation-history-rollup`  
> 审阅日期：2026-08-19  
> 实施方式：12 个可独立审阅、持续通过质量检查的 commit。不得合并成一次大提交。

---

## 0. 执行摘要

Yuki 3.6.0 已经完成 Planner 删除、Conversation Runtime、Memory Runtime 与 Capability Runtime 重构。当前会话历史仍采用高低水位原文窗口：达到上限后保留较新的原始消息，较早消息不再进入 Prompt，但没有持久的摘要视图。闲聊账单的大头也在这里：人格前缀约 2560 tok 可缓存，**uncached 历史约 5.7k–6.7k tok**（约 80–140 条带 `#event_id` 信封的原文）。

长会话的缺口：

1. 被历史窗口移出的内容不能以压缩形式继续参与当前会话。
2. 进程重启后，内存中的 `_history_window_anchors` 消失，窗口会重新从最新往回填。
3. 长期记忆保存事实和情景，不能替代「这一段会话发生了什么」的连续叙事。
4. 模型没有按 `event_id` 回读账本的 around 工具，窗口一切就只能编或靠关键词碰运气。

本项目要新增的不是第四套 Memory，也不是把 Summary 写入 `memory_facts`。**省 token 的动作是：有覆盖摘要之后主动缩短原文近窗，用一小段派生摘要换回被切掉的连续性；不是在 12k 字符池上再叠一层摘要。**

对照 3.6.0 代码后，相对初稿的硬修正：

- **双轨，禁止挖空。** 水位即将丢掉前缀时，同步写入 extractive 摘要（零 LLM），立刻可进 Prompt；后台 Flash 再把同一 Source Fingerprint 升级为结构化摘要。没有 extractive / ready 覆盖时，禁止把原文切掉。
- **摘要从历史字符池里出钱，不另开账单。** `max_context_characters=12000`、metadata 约 55%，history 余额约 5400。Rollup 占用其中 15–25%，近窗降到约 50–60%。禁止 `2400 摘要 + 原 12k 近窗`。
- **Prompt 顺序必须服从现有编译器，但编译器今天是二元的。** `PromptCompiler.compile` 把 `STATIC` 合成第一条 system，把 **所有非 STATIC（含从未启用的 `SESSION`）** 打进 `_with_dynamic_prefix`，挂在当前消息前缀上。注释禁止的是 mid-history system，不是「第二条 system」。只把 contribution 标成 `SESSION` 而不改编译器，摘要会每轮作为 TURN 重发，历史科目不降反升。Commit 9 必须把编译器改成三路：`STATIC` system → `SESSION` system → history → current+`TURN`。
- **SESSION 先占预算，再选近窗。** `max_context_characters` 只管 assembler 的 metadata+history。第二条 system 若事后叠上去，账单上升。必须 `history_budget = remainder - rollup_characters`。
- **source_fingerprint 只哈希来源，不含 summarizer_version。** 否则 extractive 与 Flash 无法在同一键上替换，会出现两行 active。
- **闲聊贵的是账本文字近窗，不是 MCP 工具 JSON。** 工具结果活在当轮 `agent_runner` 消息里，多数不进 `chat_events`。本项目不承诺降低点餐/artifact 账单。
- **缩短近窗必须配 around 回读。** 现有 `search_chat_history` 只有关键词，`get_recent_chat_history` 打 NapCat 近 20 条。没有 `get_chat_history_around(event_id)` 就是砍功能。

最终设计是：

```text
chat_events，永久原始事件
        |
        +--> Memory V2 派生事实
        |
        +--> Conversation History Rollup，派生会话摘要
                         |
                         +--> L0：原始事件摘要
                         +--> L1/L2/...：摘要的摘要
                         |
                         +--> Context Compiler
                                = 历史摘要 + 精确保留的近期原文
```

不可改变的架构结论：

- `chat_events` 是唯一原始依据，任何 Rollup 都不能删除或修改原始事件。
- Rollup 属于 Conversation Runtime，不属于 Memory V2。
- Rollup 不创建 `MemoryFact`，不增加 Evidence，不参与 Activation，不生成 Recall Receipt，不进入 Memory Mutation。
- Summary 是可重建、可替换、可审计的派生视图，不是当前事实的权威来源。
- 结构化压缩必须在后台运行，普通聊天主路径不得增加一次模型请求。
- **水位切窗当下允许零 LLM 的 extractive 写入；这不是「前台摘要模型」，是为了不挖空。**
- Prompt 永远保留一段精确的近期原文，摘要只替代较早的历史。
- 摘要覆盖范围与近期原文必须严格不重叠，也不能出现事件范围空洞。
- Context Reset 必须开启新的摘要 epoch，任何摘要不得跨越 Reset。
- R4 预留的 `conversation_summary` 只有在 source / trust / version / invalidation 齐了才能非空；合同见现有 `docs/architecture/conversation-rollup.md`，本任务书是它的实施计划，不得再写第三套互相打架的合同。

目标上下文结构（必须能映射到 `PromptCompiler.compile`）：

```text
[static system：Persona / CORE_CONTRACT]          ← 可缓存，禁止塞 rollup
[SESSION system：Conversation Summary Frontier]   ← 覆盖区间未变则字节稳定
[history：id > coverage_end 的原文气泡]           ← user/assistant，带 #event_id
[current + TURN dynamic：人物/场景/记忆/时间]     ← 仍打在当前消息前缀
```

禁止：

- 把 rollup 写进第一条 static system（会打穿约 2560 的人格 cache）。
- 把 rollup 放进 history 里冒充 assistant。
- 在 history 消息中间插入 system（`_with_dynamic_prefix` 的禁令）。
- 把 rollup 只挂在当前消息前缀里（旧叙事会出现在近窗原文之后，且每轮都变）。

`Summary Frontier` 是当前 epoch 内所有有效摘要构成的、互不重叠的历史表示。父摘要创建并验证后，子摘要才转为 `rolled_up`。extractive 与 model_summary 覆盖同一 fingerprint 时，model_summary 替换 extractive，不并排注入。

---

## 1. 3.6.0 当前实现判断

### 1.1 Conversation Runtime 已经具备承载位置

当前 `src/qq_ai_bot/conversation/runtime.py` 已经定义：

- `PreparedTurn`
- `ConversationTurnSession`
- `ConversationRuntime`
- `TurnRuntimeCore`

`PreparedTurn` 明确不携带 Planner 结果，Context、Memory Prefetch 与 Capability Exposure 都是 Host 决策。因此 Rollup 应进入 Conversation Context 准备阶段，不需要增加新的前置 Agent。

重点审阅：

- `src/qq_ai_bot/conversation/runtime.py`
- `src/qq_ai_bot/conversation/session.py`
- `src/qq_ai_bot/conversation/host.py`
- `src/qq_ai_bot/services/chat.py`
- `src/qq_ai_bot/services/processor.py`

### 1.2 Memory Runtime 已经具有清楚边界

当前 `src/qq_ai_bot/memory/runtime/contract.py` 把一次 Turn 的长期记忆访问拆为：

- Context Policy
- Read Policy
- Write Policy
- Write Transition
- Finalization Policy
- Availability

并且保持自动召回、主动读取、独占写入、Receipt Finalization 的不变量。Rollup 不能复用 `MemoryTurnContract`，因为会话摘要既不是 Memory Read，也不是 Memory Write。

重点审阅：

- `src/qq_ai_bot/memory/runtime/contract.py`
- `src/qq_ai_bot/memory/runtime/turn_session.py`
- `src/qq_ai_bot/memory/runtime/query_plane.py`
- `src/qq_ai_bot/memory/runtime/command_plane.py`
- `src/qq_ai_bot/memory/runtime/finalizer.py`
- `src/qq_ai_bot/memory/context.py`
- `src/qq_ai_bot/memory/receipt.py`
- `src/qq_ai_bot/memory/attribution.py`

禁止事项：

- 不给 `MemoryKind` 增加 `summary`。
- 不把 Conversation Summary 放进 Memory FTS 或 Memory Embedding。
- 不让摘要进入自动记忆条数预算。
- 不让摘要触发 Memory Activation 强化。
- 不让 Memory Dream 重写会话摘要。

### 1.3 当前 History Window 只做裁剪

`ContextAssembler` 目前维护 `_history_window_anchors`，并用 `_select_history_window()` 实现高低水位选择：

- 未达到上限时保持相同起点，利于 Prompt Prefix Cache。
- 达到高水位后，选择较新的低水位窗口。
- 被移出的较早消息没有持久摘要。

现有机制仍然有价值。3.6.1 不应完全删除它，而应把它改为「近期原文窗口」的优化，并让 **落库的 coverage_end 成为重启后的窗口锚点**：

```text
Summary Frontier 保持较早历史
+
High/Low Watermark 只在 coverage_end 之后的原文上滑动
+
有 ready/extractive 覆盖之后，高水位按 history 余额的 50–60% 计，而不是继续填满 12k
```

需要改名：

- `history_window_rolled` 容易与真正的 Rollup 混淆。
- 建议改为 `raw_history_window_shifted`。
- 摘要生成事件使用 `summary_created`、`summary_promoted` 或 `summary_frontier_changed`。
- 同步 extractive 写入使用 `extractive_summary_ready`。

默认阈值必须按 Yuki 实测，而不是按编码助手的「16 条 / 4000 字」：

- `local_context_event_limit` 默认 1000，真正卡住的是 12k 字符池。
- `context_metadata_budget_ratio` 默认 0.55，history 余额大约 5400 字符。
- 远野长私聊水位之后仍有约 80–140 条 provider 消息进 Prompt。
- 因此 Hot Tail 默认 **48 事件或 3600 渲染字符，取更大**；有 ready 覆盖后近窗目标为 history 余额的 50–60%。
- 16 事件 / 4000 字符对 QQ 闲聊过狠，会把「我们刚才在说什么」切没，再靠摘要补，得不偿失。

重点审阅：

- `src/qq_ai_bot/services/context_assembler.py`
- `src/qq_ai_bot/event_prompt.py`
- `src/qq_ai_bot/prompting.py`
- `src/qq_ai_bot/services/prompt_composer.py`
- 所有断言 `history_window_rolled` 的测试与性能脚本

### 1.4 原始 Event Ledger 已经是正确依据

`EventLedgerRepository` 已经持久保存 `chat_events`，并支持：

- append
- exact event lookup
- recent event list
- earlier event list
- maximum event id
- context reset

Rollup 必须基于精确会话身份和确定的事件快照，不能根据模型可见字符串恢复身份。

会话身份至少包含：

- `bot_user_id`
- `scope_type`
- 私聊：`private_peer_user_id`
- 群聊：`group_id`
- 当前 `context_reset` epoch

重点审阅：

- `src/qq_ai_bot/persistence/event_repository.py`
- `src/qq_ai_bot/persistence/models.py`
- `src/qq_ai_bot/persistence/repository_records.py`
- `ContextResetModel` 相关 repository
- 所有 `EventLedgerRepository.append*` 调用点

### 1.5 当前后台 Worker 基础可以复用

3.6.0 已有多种持久 Worker：

- Memory Worker
- Memory Rebuild Worker
- Memory Maintenance Worker
- Memory Governance Worker
- Memory Dream Worker
- Evidence Compaction Worker
- Relationship Worker

Rollup 应建立独立 Worker，不塞进任一 Memory Worker。Worker 生命周期应由 `ConversationModule.register_workers()` 注册。

重点审阅：

- `src/qq_ai_bot/application/modules/conversation.py`
- `src/qq_ai_bot/application/modules/persistence.py`
- `src/qq_ai_bot/application/lifecycle.py`
- `src/qq_ai_bot/memory/rebuild/worker.py`
- `src/qq_ai_bot/memory/worker.py`
- `src/qq_ai_bot/memory/evidence_compaction.py`

### 1.6 对照代码后必须改掉的初稿错误

审阅日期 2026-08-19，对照 `codex/refactor-3.6-runtime` 与 `docs/architecture/conversation-rollup.md`。

| 初稿 | 代码事实 | 3.6.1 合同 |
|---|---|---|
| 摘要放在 Persona 与 Raw History 之间，但没说怎么编译 | `PromptCompiler` 二元：`STATIC` 一条 system，**非 STATIC 全进当前消息前缀**。`SESSION` 枚举已有、零调用方 | Commit 9 改成三路编译。只改 stability 标签等于把摘要每轮打进 TURN，账单上升 |
| 只做后台 LLM，普通 Turn 禁止任何摘要工作 | 若 Worker 没跑完就把水位切开，Prompt 会出现「无原文也无摘要」的空洞。Diana 的精髓是同步 extractive | 切窗前必须有 extractive 或 ready 覆盖同一区间 |
| Hot Tail 16 事件 / 4000 字符 | 现网闲聊近窗 80–140 条；history 余额约 5400 字符 | 默认 48 / 3600，全部进配置 |
| Summary 30% + Raw 70% 沿用总预算 | `max_context_characters` 只管 assembler 的 metadata+history；SESSION 是编译器里的新消息。不先从 history 余额扣摘要，12k 池外再叠一层 | 先渲染 SESSION，再 `history_budget = remainder - rollup_chars`；有覆盖后近窗高水位为扣完后余额的 50–60% |
| fingerprint 带上 summarizer_version | 双轨要求 extractive 与 Flash **同一来源键** 上替换 | `source_fingerprint` 只哈希来源；`summarizer_version` 是列，不是指纹输入 |
| 工具密集历史要压 JSON | 工具 JSON 在当轮 `agent_runner`，多数不进 `chat_events` | 账本 rollup 不解决 MCP schema/artifact；明确不做 |
| 缩短近窗即可 | `search_chat_history` 无 around；`get_recent_chat_history` 走 NapCat | Commit 9 必须交付 `get_chat_history_around` |
| 进程内 anchor 继续用 | `_history_window_anchors` 是 `OrderedDict`，重启即丢；`list_recent(limit=1000)` 再丢掉前缀 | coverage_end 落库后作为锚点；assemble 只拉 `id > coverage_end` |
| 新写 `conversation-history-rollup.md` | 已有 `conversation-rollup.md`，R4 要求 source/trust/version/invalidation；其中「dynamic contribution」与现行编译器矛盾 | Commit 1 升级现有文档为合同，改成 SESSION system，不平行再写一套 |
| 默认 3.7.0 大版本 | 不改 Plugin API、不改 Alembic 以外的破坏面 | 目标版本 **3.6.1**，迁移仍为 `0041` |

省 token 的验收口径（相对同一会话、同一模型、无工具闲聊）：

- 科目是 `prompt_tokens - cached_tokens` 里的 **历史部分**，不是整单。
- 目标：稳态历史科目下降 30–50%（近窗从约 6k tok 降到约 3–4k，外加 400–800 tok 摘要）。
- 人格 cache hit 不因 rollup 引入而塌（static 字节级稳定）。
- 摘要 LLM 次数远小于闲聊轮次；失败时账单不升（留在 extractive）。

---

## 2. 应借用的成熟设计

### 2.1 OpenAI Agents SDK

借用：

1. Compaction 作为 Session Store 的包装能力，而不是 Main Agent 前的 Planner。
2. 本地 Session Items 可以作为压缩依据，不必依赖 Provider 的 response chain。
3. 触发策略由本地 Hook 决定。
4. 低延迟应用不应在 Turn 完成前等待自动压缩，应在 Turn 之间或空闲期执行。
5. 压缩结果替换旧的活动历史表示，但底层存储机制仍需保证顺序与并发安全。

应用到 Yuki：

- 使用 `chat_events` 做 input-based compaction 的唯一依据。
- 普通 Turn 只做廉价调度，不等待摘要生成。
- Worker 完成后，下一轮 Context 才使用新 Frontier。
- 同一 Source Snapshot 只能产生一个有效结果。

不直接照搬：

- 不调用 OpenAI `responses.compact`，Yuki 需要 Provider-neutral 实现。
- 不清空 `chat_events`。
- 不把 Provider response id 作为历史权威。

官方资料：

- https://openai.github.io/openai-agents-python/sessions/
- https://openai.github.io/openai-agents-python/ref/memory/openai_responses_compaction_session/

### 2.2 Anthropic Compaction 与 Context Editing

借用：

1. 触发应主要看 Context 大小，而不是只看消息数量。
2. 摘要指令应明确要求保留技术决定、状态变化、约束和未完成事项。
3. Compaction 后仍保留一段精确近期消息。
4. 工具密集历史应优先移除大体积旧 Tool Payload，但保留 Tool 的最终结果、错误和持久化效果。
5. 压缩保持 active context 较小，目的不仅是避免超限，也包括降低噪声。

应用到 Yuki：

- L0 触发与现有高低水位绑定：`must_roll` 且被丢掉的前缀达到配置门槛。允许在窗口仍未滚时 **预抽** 最早未覆盖区间，让 LLM 赶在切窗前完成。
- 摘要 Schema 专门保留 `decisions`、`open_loops`、`constraints`、`state_changes`、`terminal_tool_outcomes`。
- Hot Tail 默认 48 个事件和至少 3600 个渲染字符，二者取保护范围更大的结果；全部进 env/admin。
- 账本里的 Tool 终局（若某类事件确实写入了）只保留工具名、成功状态、终局和持久化效果。当轮 MCP JSON 不在本项目范围。

不直接照搬：

- 不使用 Provider 侧透明 Compaction，因为 Yuki 需要跨 Provider、可审计和可重建。
- 不在同一次 Main Agent 请求中触发摘要模型。

官方资料：

- https://platform.claude.com/docs/en/build-with-claude/compaction
- https://platform.claude.com/docs/en/build-with-claude/context-editing

### 2.3 Diana

借用：

1. 持久 Job、Lease、失败重试和启动时 stale lease 恢复。
2. 单次原始事件摘要最多处理 100 个事件。
3. 层级摘要先创建父记录并确认成功，再退役子记录。
4. 原始事件永久保留。
5. 摘要保留人物、时间、决定、变化、矛盾与未解决事项。

需要修正后再使用：

- Diana 使用 12 条普通摘要生成“month”，12 条 month 生成“year”。这只是数量分层，不是真正自然月/年。Yuki 不使用 month/year 命名。
- Yuki 使用 `L0/L1/L2/...`，同时记录真实开始时间和结束时间。
- Diana 把 Summary 放入 Structured Memory。Yuki 使用独立 Conversation Summary Store。
- Diana 主要依赖 Key 与 Memory Action 管理摘要。Yuki 需要精确成员表和 Source Fingerprint。

参考代码：

- https://github.com/SuInk/Diana/blob/main/model/assistant/memory_runtime.go

---

## 3. 最终架构

### 3.1 包结构

新增：

```text
src/qq_ai_bot/conversation/history/
├── __init__.py
├── models.py
├── db_models.py
├── repository.py
├── policy.py
├── source.py
├── summarizer.py
├── renderer.py
├── service.py
├── worker.py
├── metrics.py
└── errors.py
```

职责：

| 文件 | 职责 |
|---|---|
| `models.py` | Provider-neutral Summary、Job、Frontier、Source Snapshot 模型 |
| `db_models.py` | SQLAlchemy 表模型 |
| `repository.py` | Job claim、Summary 写入、Frontier 快照与事务替换 |
| `policy.py` | 触发阈值、Hot Tail 保护、层级 Fan-in、预算选择 |
| `source.py` | 将 Event 与可信 Tool 终局投影成模型输入 |
| `summarizer.py` | 调用 `ModelTask.CONVERSATION_COMPACTION`，解析严格结构化输出 |
| `renderer.py` | 将结构化摘要确定性渲染成 Prompt Block |
| `service.py` | 调度 L0 与更高层 Rollup，验证连续性和范围 |
| `worker.py` | Lease、Retry、Stale Recovery、Background Priority |
| `metrics.py` | 不含正文的质量、大小、延迟与失败指标 |
| `errors.py` | 稳定错误分类 |

### 3.2 数据流

```text
Inbound / Outbound Event
        |
        +--> EventLedgerRepository，原始事件
        |
        +--> ConversationHistoryScheduler.observe_event()
                    |
                    +--> 更新当前 epoch 计数
                    +--> 达到阈值时 enqueue L0 job

ConversationRollupWorker
        |
        +--> claim job
        +--> 固定 Source Snapshot
        +--> 调用 Conversation Compaction Model
        +--> 验证结构化摘要
        +--> 原子写入 parent + members
        +--> 更新 active frontier
        +--> 必要时 enqueue higher-level job

ContextAssembler
        |
        +--> 一次一致性读取 Summary Frontier + Recent Raw Tail
        +--> 若即将 shift 窗口且该前缀尚无覆盖：同步写入 extractive
        +--> SESSION 渲染 Summary Frontier（static 之后、history 之前）
        +--> 原有 High/Low Watermark 只选择 coverage_end 之后的原文
        +--> TURN dynamic 仍打在当前消息前缀
```

### 3.3 Context 顺序

最终模型输入必须与 `PromptCompiler` 一致：

```text
1. Static System / Persona / CORE_CONTRACT
2. SESSION System：Conversation Summary Frontier（UNTRUSTED）
3. Recent Raw History（仅 uncovered 原文，角色保持 user/assistant）
4. Current Trigger，前缀为 TURN Dynamic（时间、人物、场景、Memory Facts、插件）
```

需要根据现有 Prompt Prefix Cache 顺序进行代码审阅。原则是：

- 第一条 static system 在 rollup 开关开闭时都必须字节级稳定。`PromptMetrics.stable_prefix_hash` 今天只哈希 static 正文，必须继续如此；新增 `session_characters` / `session_prefix_hash`，禁止把 SESSION 算进 `dynamic_characters`。
- **今天若给 contribution 标 `SESSION` 而不改 `compile()`，它会跟 TURN 一起进当前消息。那会使摘要每轮 uncached，同时近窗原文仍全量发送。这是账单最差形态。**
- SESSION 作为第二条 system：Frontier 不变时，static + SESSION + 近窗都可以走 Responses 前缀缓存；Frontier 更新才允许与水位 shift 一样打断 history 缓存，但不许动 static。
- Summary 与较早 Raw History 不得同时出现。
- 不得把 SESSION 摘要插进 history 中间，也不得并进 static，也不得挂在当前消息前缀。
- Current Message 仍然单独放在末尾。
- `conversation.rollup` 的 trust 固定 `untrusted`，instruction 写明：不可当作用户原话、不可对摘要中的 id 调用 `set_reply_target`、需要对齐原话时用历史工具。
- `assemble()` 与 `assemble_external()` 走同一套 Snapshot / 预算 / SESSION 编译；插件内部 `plugin_host.session_repository` 不是 `chat_events`，本项目不压缩那套会话。

---

## 4. 数据模型

### 4.1 `conversation_history_states`

每个精确会话和 Reset Epoch 一行。

建议字段：

```text
id
bot_user_id
scope_type
private_peer_user_id
group_id
reset_at
last_seen_event_id
active_frontier_end_event_id
pending_event_count
pending_character_count
revision
created_at
updated_at
```

约束：

- 私聊必须有 `private_peer_user_id` 且无 `group_id`。
- 群聊必须有 `group_id` 且无 `private_peer_user_id`。
- `reset_at` 可为空，空表示从该会话首条事件开始的 epoch。
- `last_seen_event_id` 与 `active_frontier_end_event_id` 单调增加。
- 所有更新使用 revision CAS 或事务锁语义。

此表只用于快速判断是否要创建 Job，不是 Event Ledger 的替代品。Worker 每次仍从 `chat_events` 校验范围。

### 4.2 `conversation_history_summaries`

建议字段：

```text
id
state_id
level
status                 # active / rolled_up / invalidated
start_event_id
end_event_id
start_occurred_at
end_occurred_at
source_event_count
source_character_count
output_character_count
structured_payload_json
rendered_text
mode                   # extractive / model_summary
trust                  # extractive_compact / model_summary，注入时固定 UNTRUSTED
summarizer_version
source_fingerprint
replaced_by_summary_id
created_at
updated_at
```

硬约束：

- `level >= 0`。
- extractive 只允许 L0；L1 及以上必须是 `model_summary`。
- `mode=extractive` 时 `structured_payload_json` 可为从投影字段合成的确定性结构，不调用模型。
- **一个 fingerprint 同一时刻最多一行 `status=active`。** SQLite 使用部分唯一索引：`UNIQUE(state_id, source_fingerprint) WHERE status='active'`。
- Flash 升级必须在同一事务里：插入/更新 `model_summary` 为 active，把同 fingerprint 的 extractive 改为 `rolled_up` 并写 `replaced_by_summary_id`。禁止两行同时 active。
- 禁止用 `(state_id, source_fingerprint, summarizer_version)` 当 active 唯一键：extractive 与 Flash 的 version 不同，那会允许双轨并排注入。
- 历史行（`rolled_up` / `invalidated`）可以保留同一 fingerprint，便于审计。
- `start_event_id <= end_event_id`。
- `status=rolled_up` 时必须有 `replaced_by_summary_id`。
- `status=active` 时 `replaced_by_summary_id` 必须为空。
- 父摘要与子摘要必须属于同一个 state/epoch。
- 父摘要覆盖范围必须精确等于所有子摘要范围并集，且成员之间连续、无重叠。

### 4.3 `conversation_history_summary_members`

建议字段：

```text
id
summary_id
member_type             # event / summary
source_event_id
source_summary_id
ordinal
created_at
```

硬约束：

- `member_type=event` 时仅 `source_event_id` 非空。
- `member_type=summary` 时仅 `source_summary_id` 非空。
- `(summary_id, ordinal)` 唯一。
- 同一来源不能在同一父摘要中重复。
- L0 只能包含 event 成员。
- L1 及以上只能包含同 level 的 active summary 成员。

### 4.4 `conversation_history_rollup_jobs`

建议字段：

```text
id
state_id
job_kind                # raw_range / summary_rollup / rebuild
source_level
source_start_id
source_end_id
source_fingerprint
status                  # pending / processing / done / failed
attempts
lease_owner
lease_until
next_attempt_at
error_category
result_summary_id
created_at
updated_at
completed_at
```

硬约束：

- Job 幂等键是 `(state_id, job_kind, source_fingerprint, summarizer_version)`：同一来源上 extractive 不占 Flash job 槽，重建也不和 L0 抢同一行。
- `source_fingerprint` 本身不含 `summarizer_version`（见 §6.1）。
- Job 完成后必须关联 `result_summary_id` 或明确 `no_change` 结果。
- Lease Owner 不匹配时不能提交状态。
- 失败不会改变 active frontier。

---

## 5. 摘要结构化合同

模型不得直接生成最终 Prompt 文本。模型只返回严格结构化对象，Host 再确定性渲染。

建议 Schema：

```json
{
  "narrative": "这一段会话的自包含概述",
  "decisions": [
    {
      "decision": "已经确定的决定",
      "status": "accepted|rejected|tentative",
      "actors": ["人物或系统角色"]
    }
  ],
  "open_loops": [
    {
      "item": "尚未完成或待确认事项",
      "owner": "用户|Yuki|外部系统|未知",
      "state": "pending|blocked|waiting|unknown"
    }
  ],
  "constraints": [
    {
      "constraint": "当前任务或会话中仍有效的限制",
      "scope": "conversation|task",
      "source_type": "user|system|tool"
    }
  ],
  "entities": [
    {
      "name": "实体名称",
      "role": "与当前会话的关系"
    }
  ],
  "state_changes": [
    {
      "subject": "发生变化的对象",
      "before": "旧状态或 unknown",
      "after": "新状态",
      "certainty": "confirmed|reported|uncertain"
    }
  ],
  "uncertainties": [
    {
      "claim": "存在分歧、证据不足或互相矛盾的内容",
      "reason": "为什么不能当作确定事实"
    }
  ],
  "terminal_tool_outcomes": [
    {
      "tool": "工具规范名",
      "outcome": "成功、失败或无变化",
      "durable_effect": "none|committed|unknown",
      "public_result": "短结果"
    }
  ]
}
```

模型输出上限：

- L0 `narrative` 最大 900 字符。
- L1 及以上 `narrative` 最大 1200 字符。
- 各数组最多 8 项。
- 单项最大 240 字符。
- 总序列化输出默认不超过 6000 字符。

### 5.1 摘要必须保留

- 谁说了什么以及角色关系。
- 已接受、被否定、仍在讨论的决定。
- 用户明确提出且在后续仍有效的任务限制。
- 未完成事项、等待外部条件、失败原因。
- 事实或系统状态的变化过程。
- 互相矛盾的陈述，不能只保留一个版本。
- Tool 的终局和持久化效果。
- 关键时间范围，但不能把数据库更新时间当作事件发生时间。

### 5.2 摘要必须删除或压缩

- 寒暄、无后续价值的重复。
- 大段 Tool JSON、日志、栈跟踪和二进制描述。
- 已被后续结果完全替代的中间步骤。
- 模型内部推理。
- 临时 signed URL、密钥、Token、Cookie、Authorization Header。
- 过期 Artifact Handle。

### 5.3 事实安全规则

- 用户问题不是事实。
- Yuki 的推测不是用户自述。
- Tool 返回的当前状态不能被摘要永久提升为长期事实。
- Summary 只能说明“当时会话中出现或决定了什么”。
- 当前 Memory Fact、实时 Tool 状态与 Summary 冲突时，Summary 不具有优先权。
- `uncertainties` 不得在渲染时改写成肯定句。

---

## 6. 压缩与分层算法

### 6.0 双轨：先保空洞，再换质量

水位触发（`_select_history_window` 判定 `must_roll`，且将被丢掉的前缀 `[L, R]` 达到配置门槛）时：

```text
1. 同步、零 LLM：对 [L, R] 写一条 status=active、mode=extractive 的 L0
   正文 = 区间内气泡的紧凑投影（去重复寒暄、保留说话人、#event_id、决定性句子）
   独立字符上限进配置，超限从最旧句子丢（与 Diana truncate-from-start 同方向）
   立刻可进 SESSION 块 → 这之后才允许把原文近窗收到低水位
2. 异步：enqueue raw_range job，Flash 把同一 fingerprint 重写成 model_summary
   成功则同区间 upsert，extractive 转为 rolled_up 或直接替换
   失败则继续用 extractive，不挡下一轮
```

禁止：rollup 还是 pending 且没有 extractive 时就把原文切掉。

允许预抽：未覆盖区间已超过 L0 单 Job 上限、但窗口尚未 must_roll 时，可先 enqueue，不缩短近窗。这样切窗时更常已经是 model_summary。

群聊 `TurnOrigin.AUTONOMOUS_GROUP`、`PLUGIN_SESSION`、`PLUGIN_BACKGROUND` 默认 **只走 extractive，不叫 Flash**，避免用闲聊插话烧压缩预算。Flash 默认仅 `USER_MESSAGE`。白名单是配置里的 `TurnOrigin` 列表（`conversation_history_llm_origins`），不在 Python 里写死群号或「direct」这种非枚举词。

### 6.1 L0 原始事件摘要

默认保护 Hot Tail（全部配置，禁止写死）：

- 最新 `conversation_history_raw_tail_events`（默认 48）个可渲染事件不压缩。
- 最新至少 `conversation_history_raw_tail_characters`（默认 3600）个渲染字符不压缩。
- 二者取保护范围更大的结果。
- 当前触发事件永远不压缩。
- 至少有一条 active 覆盖摘要之后，原文高水位改为 history 余额的 `conversation_history_raw_tail_budget_ratio`（默认 0.55）。

L0 触发条件，满足任一：

- 现有窗口算法即将 `must_roll`，且将被移出的前缀达到 `l0_min_events` 或 `l0_min_characters`。
- 最早未覆盖区间达到 `l0_min_events`（默认 32）或 `l0_min_characters`（默认 8000）时预抽。

单个 L0 Job 上限：

- 最多 100 个事件。
- 最多 16000 个渲染字符。
- 必须选最早的连续未覆盖范围。
- 不能跨 Context Reset。
- 不能跨会话身份。

Source Fingerprint（**来源键，不含怎么摘要**）：

```text
sha256(
  state_id
  + reset_epoch
  + ordered event ids
  + normalized event content hashes
)
```

`summarizer_version` 与 `conversation_history_rollup_prompt_version` 是摘要行上的列，用于审计和 Flash 升级，**不得**编进 `source_fingerprint`。否则 extractive 与 model_summary 变成两个键，双轨替换失败，Prompt 可能并排注入。

Job 层可以用 `(source_fingerprint, summarizer_version, job_kind)` 做幂等。

### 6.2 高层 Rollup

等级规则：

- L0 来源为事件。
- L1 来源为连续 L0。
- L2 来源为连续 L1。
- 以此类推。

同层父摘要触发条件，满足任一：

- 8 条连续 active child summaries。
- 连续 child 的 `rendered_text` 总量达到 4800 字符。

选择规则：

- 始终选择同层最早的一组连续 child。
- 不允许跨 level 混合生成一个父摘要。
- 不允许跨 epoch。
- 不允许跳过中间 child。
- 父摘要 Level = child Level + 1。

事务顺序：

```text
1. 固定 child snapshot 与 fingerprint
2. 调用模型
3. 验证输出
4. 开启数据库事务
5. 再次确认 children 仍为 active 且 fingerprint 未变化
6. 插入 parent summary 与 member rows
7. 把 parent 设为 active
8. 把 children 设为 rolled_up，并写 replaced_by_summary_id
9. 更新 state frontier/revision
10. 提交事务
```

任何一步失败：

- Parent 不可见。
- Children 仍保持 active。
- Context Frontier 不变化。

### 6.3 Active Frontier

一个 epoch 的 Active Frontier 是所有 `status=active` 的 Summary，按范围排序后必须满足：

- 互不重叠。
- 范围顺序严格递增。
- 任一父摘要出现时，它的 children 不再 active。
- Frontier 最后一个 Summary 的 `end_event_id` 等于 `active_frontier_end_event_id`。

Frontier 可以包含不同 Level：

```text
L3 | L2 | L2 | L1 | L0 | Recent Raw Tail
```

这是正常状态，类似分层压缩结构。

### 6.4 Prompt 投影

`HistorySummaryRenderer` 对完整 Frontier 做确定性预算选择：

优先级：

1. 未完成事项。
2. 仍有效限制。
3. 已确认状态变化。
4. 最近决定。
5. 矛盾与不确定项。
6. Tool 终局（仅账本里真实出现的）。
7. 一般叙事。

默认预算（全部从 **history 字符余额** 出钱，不是 12k 总额，也不是人格 system）：

- history 余额 = `max_context_characters - metadata_json`，与今天 `ContextAssembler` 一致（人格不在这个池里）。
- **先**按 Frontier 渲染 SESSION 摘要，得到 `rollup_characters`。
- **再** `history_budget = max(0, 余额 - rollup_characters)`，把这个数字传给 `_select_history_window`。不先扣就等于在 12k 外再叠一层摘要，闲聊账单上升。
- Summary 使用余额的 `conversation_history_summary_budget_ratio`（默认 0.20）。
- Summary 最低 600 字符，最高 1600 字符。初稿 2400 对 5400 余额过大，会把近窗吃光。
- 有 active 覆盖后，Raw Tail 高水位使用 **扣完摘要后** 的 `raw_tail_budget_ratio`（默认 0.55），低水位仍乘 `history_window_low_watermark_ratio`。
- 无任何覆盖时：维持今天行为，不缩短近窗，不插入 SESSION system。
- `list_recent(limit=1000)` 在有 coverage_end 之后改为 `id > coverage_end` 再套 event_limit；不要先拉一千条再丢掉。`assemble_external` 同样。

稳态量级（用于说明，不是写死阈值）：近窗从约 6k tok 降到 2.5–3.5k，摘要 400–800 tok，历史科目约省 30–50%。一次 Flash 摊到之后几十轮闲聊。extractive 永不升级时，只要近窗缩短，账单仍下降。

若 Frontier 超过 Summary Budget：

- 不再调用模型进行实时二次摘要。
- 确定性保留高优先级结构字段。
- 先缩短旧叙事，再减少低优先级已完成事项。
- 输出 `summary_truncated=true` 内部标记，不向用户展示。

---

## 7. 一致性与并发

### 7.1 Context Snapshot

Summary Frontier 与 Recent Raw Tail 必须从同一个 SQLite Read Snapshot 读取。

建议新增：

```python
ConversationHistoryRepository.load_context_snapshot(...)
```

一次返回：

```text
active_frontier
coverage_end_event_id
frontier_revision
recent_events_after_coverage
reset_epoch
```

禁止：

```text
先查 Summary
Worker 更新 Frontier
再由另一个 Session 查 Raw Events
```

这会造成重复或缺失。

### 7.2 Reset

- `context_reset` 创建新 epoch。
- 新 epoch 不读取旧摘要。
- 旧摘要保留供审计与重建检查。
- Reset 后 state 计数从零开始。
- 当前 `_history_window_key(identity, reset)` 语义继续保留，并扩展为 summary state key。

### 7.3 并发 Turn

- Event ID 单调递增，但 occurred_at 可能相同。
- 范围排序使用 `(occurred_at, id)`。
- 同一会话最多一个 processing Job。
- 不同会话可并行。
- Worker 默认全局并发 1，配置最大 2。
- Context Read 不等待 Worker。

### 7.4 Lease 与 Retry

采用 Diana 已验证的形态：

- Lease 3 分钟。
- Job 超时 60 秒。
- 启动时释放过期 Lease。
- Retry 延迟：15、30、60、120、240、480、960 秒。
- 结构化输出非法属于可重试错误，但达到最大尝试后进入 failed。
- 身份、范围、Reset 或成员不一致属于永久错误，不重试。

### 7.5 Foreground 性能

普通 Turn 允许的新增工作：

- 一次 state upsert 或一次轻量 enqueue check。
- 一次一致性读取 Summary Frontier 与 Raw Tail。
- 一次确定性 Summary Render。
- **仅当 must_roll 且该前缀无覆盖时**，一次同步 extractive 写入（纯字符串投影，无模型）。P95 仍应小于 5ms 量级；超限则放弃本次 shift，保持原窗口，下一轮再试。

普通 Turn 禁止：

- 调用 `CONVERSATION_COMPACTION` 模型。
- 等待 Worker。
- 扫描完整会话历史。
- 重新构造旧 Summary。
- 计算远程 Embedding。
- 无覆盖时把原文窗口 shift 掉。

---

## 8. Model Runtime

新增：

```python
ModelTask.CONVERSATION_COMPACTION = "conversation_compaction"
```

默认 Route：

```toml
[routes]
conversation_compaction = "flash"
```

调用要求：

- `ModelExecutionPriority.BEST_EFFORT_BACKGROUND`
- `STRUCTURED_OUTPUT`
- Thinking 关闭或最低。
- Temperature 0.1。
- max output tokens 建议 2048。
- 不提供任何 Tool Schema。
- 不提供 Memory Facts，除非未来有单独的冲突标注需求，本版本不做。

禁止复用：

- `MEMORY_CONSOLIDATION`
- `MEMORY_EXTRACTION`
- `MEMORY_DREAM`

原因：模型调用的业务目的不同，必须在 Usage、Latency 与失败统计中独立显示。

---

## 9. 配置建议

新增静态或 Hot Config，最终字段名由 Codex 根据现有 Settings/RuntimeConfig 结构确认：

```text
conversation_history_rollup_enabled = true
conversation_history_rollup_worker_concurrency = 1
conversation_history_rollup_poll_seconds = 1.0
conversation_history_rollup_lease_seconds = 180
conversation_history_rollup_timeout_seconds = 60
conversation_history_rollup_max_attempts = 7
conversation_history_raw_tail_events = 48
conversation_history_raw_tail_characters = 3600
conversation_history_raw_tail_budget_ratio = 0.55
conversation_history_rollup_l0_min_events = 32
conversation_history_rollup_l0_min_characters = 8000
conversation_history_rollup_l0_max_events = 100
conversation_history_rollup_l0_max_characters = 16000
conversation_history_extractive_max_characters = 1200
conversation_history_rollup_fan_in = 8
conversation_history_rollup_fan_in_characters = 4800
conversation_history_summary_budget_ratio = 0.20
conversation_history_summary_min_characters = 600
conversation_history_summary_max_characters = 1600
conversation_history_llm_origins = "user_message"   # TurnOrigin 逗号列表；默认只有 USER_MESSAGE 走 Flash
# 例：user_message 或 user_message,scheduled_automation
# AUTONOMOUS_GROUP / PLUGIN_SESSION / PLUGIN_BACKGROUND 默认只 extractive，不写 Python 群号白名单
conversation_history_rollup_retention_days = 0
conversation_history_rollup_prompt_version = "conversation-rollup-v1"
```

规则：

- `retention_days=0` 表示 Summary 随原始事件保存，不自动过期。
- 删除会话原始历史时，相关 Summary 通过 FK 或显式事务一并删除。
- 运行时关闭 Rollup 时，已有 Summary 仍可读取；单独提供 `use_existing_summaries` 开关没有必要。
- 若功能关闭，Context 回到原有 Raw Window 行为。

---

# 10. Commit 计划

以下 12 个 Commit 必须按顺序实施。每个 Commit 都必须通过 Ruff、mypy 与本 Commit 新增测试。不得把多个 Commit 合成一个无法审阅的大提交。

---

## Commit 1

### Commit Message

```text
docs(architecture): define conversation history rollup contract
```

### 目标

先冻结术语、职责、数据边界和不可违反的规则，避免后续把 Summary 接入 Memory V2 或同步主路径。

### 开工前重点审阅

- `src/qq_ai_bot/conversation/runtime.py`
- `src/qq_ai_bot/conversation/session.py`
- `src/qq_ai_bot/services/context_assembler.py`
- `src/qq_ai_bot/memory/runtime/contract.py`
- `src/qq_ai_bot/persistence/event_repository.py`
- `docs/releases/v3.6.0.md`
- `docs/performance/3.6.0-runtime-report.md`
- `docs/architecture/conversation-rollup.md`
- `src/qq_ai_bot/prompting/compiler.py`
- Diana `model/assistant/memory_runtime.go`

### 本 Commit 内容

新增或升级：

```text
docs/architecture/conversation-rollup.md
```

不得另起 `conversation-history-rollup.md` 与现有报告并行。必须把 R4 的 source / trust / version / invalidation 写进同一份合同，并加上本任务书冻结的：

- Raw Event、Memory Fact、Conversation Summary 三者区别。
- Extractive / model_summary 双轨与「无覆盖禁止切窗」。
- Summary Frontier 定义。
- L0/L1/L2 分层定义。
- Reset Epoch。
- SESSION 摘要插在 static 之后、history 之前（必须改 `PromptCompiler.compile` 三路分流，不是只改 contribution.stability）。
- 摘要字符从 history 余额扣除后再选近窗。
- Summary 与 Raw 不重叠规则。
- Parent-first Replacement 事务规则。
- Background LLM + 同步 extractive。
- Prompt 顺序与 Responses 前缀缓存。
- 结构化 Summary Schema。
- around 回读与 `set_reply_target` 可见集。
- 性能与质量门槛（历史科目 30–50%，不是整单）。

### 测试

无运行时代码变化，但运行：

```bash
ruff format --check .
ruff check .
mypy src
```

### 验收

- 架构文档中不得把 Summary 称为 Memory Fact。
- 不出现 month/year 等误导性 Level 名称。
- 明确原始事件永久保留。
- 明确 Foreground 无新增模型调用。

### 禁止捷径

- 不在此 Commit 提交半成品模型或数据库代码。
- 不把需求只写成一张 Mermaid 图。

---

## Commit 2

### Commit Message

```text
feat(persistence): add durable conversation history rollup schema
```

### 目标

建立 Summary、Member、Job 与 State 的最终数据库结构。

### 开工前重点审阅

- `src/qq_ai_bot/persistence/models.py`
- `src/qq_ai_bot/conversation/db_models.py`
- `migrations/versions/0037_runtime_turn_correlation.py`
- `migrations/versions/0040_drop_planner_persistence.py`
- SQLite Partial Index 与 Alembic Batch Migration 的现有用法

### 本 Commit 内容

新增迁移：

```text
migrations/versions/0041_conversation_history_rollup.py
```

新增模型：

```text
src/qq_ai_bot/conversation/history/db_models.py
src/qq_ai_bot/conversation/history/models.py
```

创建四张表：

- `conversation_history_states`
- `conversation_history_summaries`
- `conversation_history_summary_members`
- `conversation_history_rollup_jobs`

增加必要索引：

- 精确会话身份 + Reset Epoch。
- active summary 按 state/range。
- active summary：`UNIQUE(state_id, source_fingerprint) WHERE status='active'`。
- 全表不把 `summarizer_version` 编进该唯一键。
- jobs：`(state_id, job_kind, source_fingerprint, summarizer_version)` 幂等。
- pending job 按 next_attempt_at。
- lease_until。
- member source 查询。

增加 `chat_events` 精确会话 + id 的复合索引，确保按 coverage boundary 查询不会扫描全表。

### 测试

新增：

```text
tests/unit/test_conversation_history_schema.py
```

覆盖：

- 私聊/群聊身份 Check Constraint。
- Member exactly-one-source Check Constraint。
- Summary 状态与 replaced_by 约束。
- 同一 fingerprint 不能两行同时 active。
- extractive 与 model_summary 不同 `summarizer_version` 仍必须替换而非并存。
- Alembic 从 0040 升级到 0041。
- SQLite `PRAGMA foreign_key_check`。

### 验收

- `alembic upgrade head` 成功。
- 重复执行迁移路径不产生孤立表。
- Summary 表无 FK 指向 `memory_facts`。
- Downgrade 策略按项目现行破坏性迁移规范处理并写清楚。

### 禁止捷径

- 不用一个 JSON 大字段代替成员表。
- 不只保存 start/end 而省略 exact members。
- 不以 `conversation_key_hash` 作为唯一身份依据。

---

## Commit 3

### Commit Message

```text
feat(history): implement summary repository and frontier invariants
```

### 目标

实现所有数据库操作和 Active Frontier 一致性验证，不调用模型。

### 开工前重点审阅

- `src/qq_ai_bot/persistence/event_repository.py`
- `src/qq_ai_bot/memory/rebuild/repository.py`
- `src/qq_ai_bot/memory/receipt.py`
- `src/qq_ai_bot/persistence/database.py`
- SQLAlchemy async transaction 现有风格

### 本 Commit 内容

新增：

```text
src/qq_ai_bot/conversation/history/repository.py
src/qq_ai_bot/conversation/history/errors.py
```

实现：

- `get_or_create_state()`
- `observe_event()`
- `claim_next_job()`
- `retry_job()`
- `complete_job()`
- `release_stale_leases()`
- `list_active_frontier()`
- `load_source_events()`
- `load_source_summaries()`
- `commit_l0_summary()`
- `commit_parent_summary_and_retire_children()`
- `invalidate_summary_tree()`
- `validate_frontier()`
- `load_context_snapshot()` 协议先定义，Context 接入在 Commit 9

`commit_parent_summary_and_retire_children()` 必须在一个事务中完成 Parent 与 Children 状态替换。

### 测试

新增：

```text
tests/unit/test_conversation_history_repository.py
```

覆盖：

- L0 exact member 顺序。
- Parent 覆盖范围。
- Children 不连续时拒绝。
- Children Level 不同拒绝。
- 重复 fingerprint 幂等。
- 双 Worker 竞争只能一个成功。
- Parent Commit 失败时 Children 仍 active。
- Reset Epoch 隔离。
- 私聊/群聊/Bot 实例隔离。

### 验收

- 所有 Frontier 变更可在事务后通过 `validate_frontier()`。
- 不存在 active parent 与 active child 同时出现。
- Repository 不 import Memory Runtime。

### 禁止捷径

- 不在 Python 内先更新 Children、后插 Parent。
- 不用 delete 退役 Children。
- 不吞掉 IntegrityError 后假装成功。

---

## Commit 4

### Commit Message

```text
feat(history): add deterministic compaction policy and source snapshots
```

### 目标

确定何时压缩、压缩哪一段，以及如何保护 Recent Raw Tail。

### 开工前重点审阅

- `ContextAssembler._select_history_window()`
- `ChatEventPromptRenderer.main_agent_history()`
- `EventRecord` 字段
- `ContextResetModel`
- 所有 Event Origin 与 Event Kind
- Outbound 事件是否只在平台接受后写入

### 本 Commit 内容

新增：

```text
src/qq_ai_bot/conversation/history/policy.py
src/qq_ai_bot/conversation/history/source.py
```

实现：

- `HistoryCompactionPolicy`
- `HotTailBoundary`
- `RawRangeCandidate`
- `SummaryRollupCandidate`
- `ConversationSourceSnapshot`
- `source_fingerprint()`
- `extractive_compact()`
- `select_l0_candidate()`
- `select_parent_candidate()`
- `must_roll_prefix()` 与现有 `_select_history_window` 对齐，禁止另搞一套条数时钟

Source Projection 规则：

- 使用真实 Event Role/Direction/Sender Snapshot。
- 保留时间与引用关系。
- 外部事件标记为 untrusted external event。
- 图片只使用已持久化 visual summary，不重新处理图片。
- Tool 大结果不得直接复制。
- 对旧 Tool 终局的接入点先定义 Protocol；Codex 必须审阅 Agent Action/Tool Receipt 的可用字段再决定具体查询。

### 测试

新增：

```text
tests/unit/test_conversation_history_policy.py
tests/unit/test_conversation_history_source.py
```

覆盖：

- 48 Event Hot Tail（配置可变，测试夹具用默认）。
- 3600 Character Hot Tail。
- 两个保护条件取更大范围。
- 窗口 must_roll 触发 extractive。
- 未覆盖达到 32 Event/8000 Character 时预抽。
- 无覆盖时拒绝 shift。
- 单 Job 100 Event/16000 Character 上限。
- 不跨 Reset。
- 不跨会话。
- 时间相同使用 Event ID 稳定排序。
- Source Fingerprint 稳定。
- extractive 超限从最旧句子丢。

### 验收

- 给定相同 Event Snapshot，Candidate 与 Fingerprint 完全一致。
- Policy 全部纯函数，不查询模型。
- Source Snapshot 是不可变模型。

### 禁止捷径

- 不用“每 12 条消息压一次”的单一规则。
- 不用最近 N 条简单切片代替 Hot Tail 字符保护。
- 不把 `summarizer_version` 编进 `source_fingerprint`。
- 不把 Source Content Hash 省略出 fingerprint。

---

## Commit 5

### Commit Message

```text
feat(model-runtime): add conversation compaction model task
```

### 目标

给会话压缩建立独立模型业务任务、路由和结构化输出合同。

### 开工前重点审阅

- `src/qq_ai_bot/model_runtime/models.py`
- `src/qq_ai_bot/model_runtime/executor.py`
- `src/qq_ai_bot/model_runtime/router.py`
- Memory Extraction 与 Dream 的 structured output 调用方式
- `config/model_profiles.example.toml`
- Setup 生成模型 Route 的代码

### 本 Commit 内容

新增：

```python
ModelTask.CONVERSATION_COMPACTION
```

新增：

```text
src/qq_ai_bot/conversation/history/summarizer.py
```

实现：

- 严格 Pydantic Summary Output。
- Prompt Version 常量。
- Provider-neutral `ConversationHistorySummarizer`。
- `ModelExecutionPriority.BEST_EFFORT_BACKGROUND`。
- 不传 Tool。
- 强制模型仅返回结构化摘要。
- Sensitive Pattern 二次检查。
- 输出大小与数组数量验证。

更新：

- `config/model_profiles.example.toml`
- Setup/迁移模型配置代码
- Fake Model 测试支持

### 测试

新增：

```text
tests/unit/test_conversation_history_summarizer.py
```

覆盖：

- 正常 L0 输出。
- 高层输出。
- 非法 JSON。
- 未知字段。
- 超长字段。
- Tool Call 输出拒绝。
- Secret Pattern 拒绝或脱敏。
- 问句不应被总结为确定事实的固定测试样例。

### 验收

- Model Stats 中单独出现 `conversation_compaction`。
- 默认 Route 使用 Flash。
- Main Agent Route 不受影响。
- Summarizer 不 import Memory Mutation 或 Memory Fact Service。

### 禁止捷径

- 不复用 `MEMORY_CONSOLIDATION` 统计名。
- 不让模型输出 Markdown 摘要。
- 不把完整历史 Prompt 写入日志。

---

## Commit 6

### Commit Message

```text
feat(history): add durable rollup queue and background worker
```

### 目标

建立不阻塞前台的持久任务执行系统。

### 开工前重点审阅

- `MemoryWorker`
- `MemoryRebuildWorker`
- `EvidenceCompactionWorker`
- `Application LifecycleRegistry`
- `ModelExecutor` 前台抢占后台的行为
- `ConversationTurnCoordinator`

### 本 Commit 内容

新增：

```text
src/qq_ai_bot/conversation/history/worker.py
src/qq_ai_bot/conversation/history/metrics.py
```

实现：

- Worker Coordinator。
- 1 个默认 Worker，最大 2。
- 750ms 至 1s Poll。
- Wake Channel/Event。
- 3 分钟 Lease。
- 60 秒 Job Timeout。
- Stale Lease Recovery。
- 指数 Retry。
- 启动与关闭。
- Health Snapshot。
- 前台模型请求可抢占 Background Model Call。

在：

```text
src/qq_ai_bot/application/modules/conversation.py
```

注册 Worker 生命周期。

在 Persistence Bundle 中增加 Repository。

### 测试

新增：

```text
tests/unit/test_conversation_history_worker.py
```

覆盖：

- Claim/Complete。
- Retry 延迟。
- Stale Lease。
- Shutdown 释放 Lease。
- Cancelled Background Call。
- Queue Wake。
- 同会话串行，不同会话可并行。

### 验收

- 普通 Chat 测试中不存在新的 Foreground `ModelTask.CONVERSATION_COMPACTION`。
- Worker 未启动时 Chat 仍可正常工作。
- Worker 失败不改变 Active Frontier。

### 禁止捷径

- 不用 `asyncio.create_task()` 无持久状态地执行摘要。
- 不把整批历史放在 Job JSON 中，Job 只存 Source Range/Fingerprint。
- 不在 Event Append 内等待 Worker。

---

## Commit 7

### Commit Message

```text
feat(history): summarize raw event ranges into level zero
```

### 目标

打通 Raw Events -> L0 Summary -> Active Frontier 的完整执行路径。

### 开工前重点审阅

- 每个 inbound/outbound/external event 的 append 路径。
- 发送成功与发送失败的 Event Ledger 行为。
- Runtime Turn Correlation。
- Tool Receipt/Agent Action 中可用于终局摘要的字段。
- Context Reset 触发路径。

### 本 Commit 内容

新增：

```text
src/qq_ai_bot/conversation/history/service.py
src/qq_ai_bot/conversation/history/renderer.py
```

实现：

- `ConversationHistoryService.observe_event()`。
- State Counter 幂等更新。
- 达阈值后创建 L0 Job（预抽）或在 must_roll 时同步 `extractive_compact()`。
- **无 extractive/ready 覆盖时 ContextAssembler 不得 shift。**
- Worker 固定 Source Snapshot。
- Summarizer 调用。
- Summary Quality Gate。
- 成功后以同一 fingerprint 替换 extractive。
- L0 Parent + Event Members 原子提交。
- 成功后减少 pending counters。
- 更新 active frontier end。
- 失败保持 extractive。

所有 Event Append 入口接入 `observe_event()`。Codex 必须先列出所有 `EventLedgerRepository.append*` 调用点，再提交代码。当前代码至少包括：

- `processor.py`：用户入站 `append_inbound`。
- `chat.py`：确认投递后的 outbound `append`。
- `plugin_host/facades.py`：外部/插件写入账本。
- `plugin_host/notification_delivery.py`：通知投递入账。
- `automation/gateway.py`：自动化产生的会话事件（多处 `append`）。
- `agent_tools.py`：仍写入账本的路径（至少两处 `append`）。
- `memory/quality/runner.py`：质量/重建夹具，**生产 Worker 不走这里**；接入时 no-op 或显式跳过，禁止让测试灌数打满 Flash 队列。

不观察：

- `plugin_host/session_repository.append_message`：那是插件会话私有 transcript，不是 `chat_events`。

`SCHEDULED_AUTOMATION` 若写入了 `chat_events`，state 计数仍要更新；是否 enqueue Flash 只看 `conversation_history_llm_origins`。

### 测试

新增：

```text
tests/unit/test_conversation_history_l0_service.py
```

覆盖：

- 32 条事件预抽。
- 字符阈值预抽。
- must_roll 时同步 extractive，无模型调用。
- 低于阈值不触发。
- 无覆盖时窗口不 shift。
- 最新 Hot Tail 不入 Source。
- Summary 成功后 Frontier End 正确，extractive 不再 active。
- Summarizer 失败后 extractive 仍在。
- 重复 Event Observe 不重复计数。
- 重启后可继续处理。
- Context Reset 后新 State。
- autonomous / plugin origin 不 enqueue Flash job（配置默认）。
- `source_fingerprint` 在 extractive→Flash 替换前后不变。

### 验收

- L0 Summary 可从成员表还原全部 Event ID。
- Source Snapshot 与 Summary Range 一致。
- 无任何 Raw Event 被修改或删除。
- 观察 Event 的前台新增延迟 P95 小于 5ms，本地 SQLite 环境。

### 禁止捷径

- 不以“Context Window 已经 shift”作为唯一调度信号。
- 不直接在 `ContextAssembler` 中调用 Summarizer。
- 不在 Summary 或 extractive 成功前推进 Frontier End / 缩短近窗。

---

## Commit 8

### Commit Message

```text
feat(history): implement hierarchical summary rollup
```

### 目标

实现 L0 -> L1 -> L2 的递归压缩与 Parent-first Replacement。

### 开工前重点审阅

- Diana `selectMemorySummaryRollup()`。
- Diana `forgetRolledUpSummaries()`。
- Yuki Repository 事务。
- Summary Frontier 验证测试。

### 本 Commit 内容

在 Service/Policy/Repository 中实现：

- `select_parent_candidate()`。
- Fan-in 8。
- Combined Characters 4800 Trigger。
- Parent Summarizer Input。
- Parent Summary Structured Output。
- Parent Commit 与 Child Retirement。
- 完成后递归检查下一 Level。
- Parent Source Fingerprint。
- Rollup 最大 Level 防护，例如默认 16。

高层 Prompt 必须要求：

- 保留所有未完成事项。
- 保留相互矛盾内容。
- 保留状态变化顺序。
- 不简单拼接 Child Narrative。
- 不扩大事实确定性。

### 测试

新增：

```text
tests/unit/test_conversation_history_hierarchical_rollup.py
```

覆盖：

- 8 个 L0 -> 1 个 L1。
- 8 个 L1 -> 1 个 L2。
- 字符阈值提前触发。
- Child Gap 拒绝。
- Child Level 不同拒绝。
- Parent 写入失败 Children 保持 active。
- Parent 成功后 Children 全部 rolled_up。
- 并发两个 Parent Job 仅一个生效。
- 递归创建下一 Level Job。

### 验收

- Active Frontier 始终无父子并存。
- Level 使用数字，不出现 month/year。
- Parent 精确包含所有 Child Member。
- Frontier 大小随历史增长为对数级，而非线性增长。

### 禁止捷径

- 不直接删除 Child Summary。
- 不按自然月强制切分。
- 不允许非连续 Child 进入同一 Parent。

---

## Commit 9

### Commit Message

```text
feat(context): compile summary frontier with exact recent history
```

### 目标

把 Rollup 正式接入 Main Agent Context，补上 around 回读，并保证 Snapshot、预算、缓存稳定性与无重叠。缩短近窗而不给回读就是砍功能。

### 开工前重点审阅

- `ContextAssembler.assemble()` / `assemble_external()`。
- `_select_history_window()` 与 `_history_window_anchors`。
- `prompting/compiler.py`：今天 `static = STATIC`，`dynamic = not STATIC`；`_with_dynamic_prefix`。
- `PromptMetrics` 只有 `static_characters` / `dynamic_characters` / `stable_prefix_hash`。
- `list_recent(limit=local_context_event_limit)` 默认 1000。
- `PromptContribution` / `PromptStability`（SESSION 目前无调用方）。
- DeepSeek Responses append-only 与 Prompt Cache 测试（`tests/unit/test_prompt_input_cache.py`、`tests/unit/test_rising_sea_architecture.py`）。
- `visible_event_ids` → `ReplyTargetControl`。
- `search_chat_history` / `get_recent_chat_history` 现有契约。

### 本 Commit 内容

新增：

```text
HistoryContextSnapshot
HistorySummaryRenderer
get_chat_history_around   # 或 search_chat_history 的 around operation
```

`PromptCompiler`（必须改 `compile()`，不是只改标签）：

- 三路分流：`STATIC` → 第一条 system；`SESSION` → 第二条 system；其余 `TURN` → 当前消息前缀。
- `stable_prefix_hash` 继续只哈希 STATIC。新增 `session_characters`；SESSION 不得计入 `dynamic_characters`。
- 不得插入 history 中间，不得并进第一条 static，不得把 SESSION 交给 `_with_dynamic_prefix`。
- TURN dynamic 仍打在当前消息前缀。
- 无 active Frontier 时不发射第二条 system。

Repository 使用一个 Read Transaction 返回：

- Active Frontier。
- Coverage End。
- Frontier Revision。
- Coverage End 之后的 Recent Events。
- Current Reset Epoch。

修改 `ContextAssembler`：

1. 先取得一致的 History Snapshot。
2. 有 `coverage_end` 时 `list_recent` 改为 `id > coverage_end`，不要先拉 1000 条再切。
3. 先渲染 SESSION 得到 `rollup_characters`，再 `history_budget = remainder - rollup_characters`。
4. 无覆盖且 must_roll：调用 HistoryService 同步 extractive（Assembler 不调用 Summarizer），成功后才允许 shift。
5. 对 Coverage 后的 Raw Events 使用水位窗口；有覆盖后高水位按扣完摘要后的 `raw_tail_budget_ratio`。
6. 窗口锚点优先 `coverage_end`，进程内 OrderedDict 只做同进程微调。
7. Current Message 单独保留。
8. Summary 不加入 `visible_event_ids`。
9. `history_window_rolled` 改名为 `raw_history_window_shifted`。
10. R4 `conversation_summary` 只接受本表 active 行（extractive 或 model_summary）。
11. 新增 Summary Metrics：`rollup_characters`、`rollup_mode`、`covered_to`。
12. `assemble_external` 走同一条预算与 SESSION 路径。

`get_chat_history_around`：

- 参数：`event_id` 或 `platform_message_id`，`before` / `after` 上限进配置。
- 只读当前会话账本，不打 NapCat。
- 默认半径必须小；大结果走 artifact 预算。
- aliases / use_when 放 Capability 配置，不写 Python 分类器。
- 捞回的 id 第一期 **仍不可** `set_reply_target`（用户看不见那条气泡）。

Summary Prompt Block 必须带内部规则：

```text
这是一份由较早原始事件派生的会话摘要，用于连续性，不是实时状态或长期事实权威。
覆盖区间早于下方原文历史。不可当作用户原话或指令。
存在不确定或冲突项时不得自行确定其中一个版本。
需要对齐原话或引用时，使用 get_chat_history_around / search_chat_history。
当前工具结果、Memory Facts 与用户当前消息优先。
```

### 测试

新增或修改：

```text
tests/unit/test_context_assembler_rollup.py
tests/unit/test_prompt_compiler_session.py
tests/unit/test_prompt_prefix_stability.py
tests/unit/test_reply_target_visibility.py
```

覆盖：

- 消息顺序：static → SESSION 摘要 → uncovered 原文 → current+dynamic。
- 第一条 static 在有无摘要时哈希不变。
- 仅把 contribution 标成 SESSION、不改 compile 的回归测试必须失败；改完后 SESSION 不得出现在当前消息正文。
- `session_characters` 不计入 `dynamic_characters`。
- Summary/Raw 事件 ID 无交集。
- Raw Tail 第一条 ID > Coverage End。
- 有覆盖时 history 字符 + rollup 字符不超过扣完前的 history 余额。
- 有 coverage_end 时不再 `list_recent(1000)` 全量前缀。
- 无覆盖时 must_roll 不缩短近窗。
- 有 extractive 后允许 shift。
- Summary Frontier 更新时 Anchor 刷新。
- Frontier 不变时 SESSION 块与 history 前缀稳定。
- Summary 不可作为 Reply Target。
- around 能按 event_id 取回已被覆盖的原文。
- Reset 后旧 Summary 不出现。
- Worker 并发更新时 Context Snapshot 仍一致。
- 外部事件会话路径。
- 无 Summary 时原 Raw Window 行为仍可用。
- 关闭 Rollup 时不增加 SESSION system。

### 验收

- `ContextAssembler` 内不调用 Summarizer。
- 普通 Turn 不等待 Worker。
- 100% 无 Summary/Raw 重叠。
- 100% 无跨私聊、跨群、跨 Bot、跨 Reset 污染。
- Context 总字符预算仍严格生效。

### 禁止捷径

- 不先查 Summary、再用另一个 DB Session 查 Raw。
- 不把 Summary 内容放进 Memory Metadata Payload。
- 不移除 High/Low Raw Window 而改成每轮 last-N 滑动。
- 不把 SESSION 摘要并进人格 static。
- 不在 history 中间插 system。
- 不把 SESSION 交给 `_with_dynamic_prefix`。
- 不在扣摘要之前用满 history 余额。
- 不把缩短近窗而不做 around 当成完成。

---

## Commit 10

### Commit Message

```text
feat(operations): add rollup inspection rebuild invalidation and health
```

### 目标

提供生产审计、故障恢复和重建能力。

### 开工前重点审阅

- 当前 CLI 管理命令结构。
- Memory Rebuild 的 plan/review/commit 设计。
- Runtime Health 汇总。
- Admin Permission Catalog。
- 数据库备份与完整性检查工具。

### 本 Commit 内容

新增 CLI：

```text
qq-ai-bot-cli history-rollup status
qq-ai-bot-cli history-rollup inspect
qq-ai-bot-cli history-rollup rebuild
qq-ai-bot-cli history-rollup invalidate
qq-ai-bot-cli history-rollup reconcile
```

功能：

- 查看 State、Frontier、Pending/Failed Job。
- 按精确会话与 epoch 查看 Summary Coverage。
- 从 Raw Events 重建，但先 dry-run 输出统计。
- Invalidate 只失效派生 Summary，不删除 Raw Events。
- Reconcile 根据 Event Ledger 修复漏掉的 State Counters。
- Health 检查 Frontier Gap、Overlap、Orphan Member、Bad Replacement、Stale Lease。

权限：

- 普通 Agent 不暴露这些工具。
- CLI 或管理员命令必须使用真实本地身份权限。
- 不新增 Main Agent 的 History Mutation Tool。

### 测试

新增：

```text
tests/unit/test_conversation_history_cli.py
tests/unit/test_conversation_history_health.py
```

覆盖：

- Dry-run 无写入。
- Rebuild 幂等。
- Invalidate 后 Context 回到 Raw History。
- Reconcile 修复 Counter。
- Health 可发现每种异常。
- 日志不输出完整历史正文。

### 验收

- 任何 Summary 都可追溯到 exact members。
- 删除所有 Summary 后可从 Raw Events 重建。
- CLI 输出默认脱敏。
- 运维命令不经过 Main Agent。

### 禁止捷径

- 不提供“删除 Raw Events 以节省空间”的命令。
- 不允许模糊会话目标执行 Invalidate。
- 不允许 Rebuild 与正常 Worker 同时修改同一 State。

---

## Commit 11

### Commit Message

```text
test(history): add replay quality resilience and performance gates
```

### 目标

建立 3.6.1 发布所需的质量、性能和恢复测试。

### 开工前重点审阅

- `scripts/measure_3_6_runtime.py`
- `docs/performance/3.6.0-runtime-report.md`
- Memory Quality Harness。
- Model Invocation Metrics。
- CI workflow 的 artifact 输出方式。

### 本 Commit 内容

新增：

```text
scripts/measure_history_rollup.py
scripts/evaluate_history_rollup.py
docs/performance/3.6.1-history-rollup-report.md
artifacts/history-rollup-quality/
```

建立测试集，至少包含：

- 长期技术讨论。
- 多轮代码审阅。
- 用户反复纠正前述决定。
- 互相矛盾陈述。
- 多人群聊。
- 图片摘要。
- 外部插件事件。
- Tool 成功、失败、No-op、持久化成功。
- 大 Tool JSON。
- Context Reset。
- Worker 重启。
- Summary 中含恶意指令样式文本。
- 包含密钥样式内容。

质量 Gate：

- 问题不被写成确定事实。
- 矛盾项保留。
- 未完成事项召回率。
- 决定保留率。
- 当前任务限制保留率。
- Tool 终局保留率。
- 跨会话污染为 0。
- Source Coverage 为 100%。
- Summary/Raw Overlap 为 0。
- Parent/Child Replacement 错误为 0。

性能 Gate：

- 普通无 Tool Chat 不增加 Foreground 模型调用。
- Context Build P95 增量不超过 10ms。
- Event Observe P95 小于 5ms。
- 长会话 Dynamic History 字符平均降低至少 40%（有覆盖后的稳态，相对同一会话无 rollup 基线）。
- 历史科目 `prompt_tokens - cached_tokens` 中 history 部分下降 30–50%。
- static 前缀哈希在开关 rollup 前后不变。
- extractive 路径零 Foreground 模型调用。
- Worker 失败不增加前台 P95。
- Summary Frontier 查询 P95 小于 10ms，本地 SQLite 基线。

成本报告：

- 每 1000 个原始事件的 Compaction 请求数。
- L0/L1/L2 请求分布。
- 输入/输出 Token。
- Background 模型总成本。
- 平均 Context 字符节省。

### 测试

完整执行：

```bash
ruff format --check .
ruff check .
mypy src
pytest
alembic upgrade head
python scripts/measure_history_rollup.py
python scripts/evaluate_history_rollup.py
```

### 验收

- 报告不填写未测数据。
- 测量环境、样本量、失败处理和百分位算法写清楚。
- 所有 Gate 产生机器可读 JSON 与人类可读 Markdown。

### 禁止捷径

- 不用人工挑选的 3 条成功样例代替回放集。
- 不根据字符缩短比例推断事实准确率。
- 不把 Background 模型延迟计入 Foreground 后又宣称前台无影响。

---

## Commit 12

### Commit Message

```text
release: ship Yuki 3.6.1 conversation history rollup
```

### 目标

完成版本、默认配置、安装升级、发布说明和最终清理。

### 开工前重点审阅

- 3.6.0 Release Workflow。
- Guided Setup。
- `.env.example`。
- `config/model_profiles.example.toml`。
- Docker Release Assets。
- Alembic Head 检查。
- Plugin API 2.0 兼容测试。

### 本 Commit 内容

- `pyproject.toml` 版本升到 3.6.1。
- Alembic Head 更新为 `0041`。
- 默认启用 Rollup。
- Setup 为 Flash Route 添加 `conversation_compaction`。
- `.env.example` 增加 Rollup 配置。
- 更新 README 架构图。
- 新增 `docs/releases/v3.6.1.md`。
- 新增 `docs/upgrade-3.6.1.md`。
- 更新 CHANGELOG。
- 发布性能与质量报告。
- 清理临时调试配置和实验代码。

最终代码清理：

- 不保留 `history_window_rolled` 旧名称。
- 不保留未使用的 Summary Adapter。
- 不保留将 Summary 写入 Memory Fact 的实验代码。
- 不保留同步 **模型** Compaction Path（extractive 同步写入必须保留）。
- 不保留 month/year Level。

### 测试

- 全量 CI。
- 干净数据库安装。
- 3.6.0 数据库升级到 3.6.1。
- SQLite Backup + Integrity Check。
- Docker Smoke。
- 真实长会话小规模 Canary。
- Worker 关闭状态 Smoke。
- Worker 故障恢复 Smoke。

### 发布验收

- 3.6.0 升级后 Raw Events 与 Memory V2 数据不变。
- 普通聊天可在 Rollup Worker 不可用时继续运行。
- 已创建 Summary 的会话在重启后仍保持历史连续性。
- Reset 后不出现旧 epoch Summary。
- 所有 Context Summary 都可通过 Member 表追溯。
- 正式版本不含任何 Foreground Summary Model Call。

---

## 11. Codex 每轮审阅清单

Codex 在每个 Commit 开工前必须先输出一份短审阅记录，不能直接写代码。

固定格式：

```text
1. 本 Commit 涉及的现有执行路径
2. 真实权威来源
3. 事务边界
4. 并发与取消语义
5. 当前测试保护了什么
6. 当前测试没有保护什么
7. 预计新增或删除的文件
8. 可能破坏的行为
```

### 特别需要静态追踪的调用链

1. 所有 `EventLedgerRepository.append*` 调用点。
2. Outbound 成功回执到 Chat Event 的写入路径。
3. Context Reset 的写入与读取路径。
4. `ContextAssembler.assemble()` 与 `assemble_external()` 的所有调用点。
5. `PromptCompiler.compile`：STATIC / SESSION / TURN 三路，以及 `_with_dynamic_prefix`。
6. `visible_event_ids` 到 Reply Target 的完整路径。
7. Prompt 消息排序与 Provider Cache Prefix（`stable_prefix_hash` 不含 SESSION）。
8. Background Model Priority 与前台抢占。
9. Application Container 的 Worker 生命周期。
10. CLI Migration Head 与 Release Workflow。
11. 数据删除、隐私删除与 Summary FK 的关系。

### 需要动态验证的场景

- Worker 在 Summary 模型请求中途被前台请求取消。
- Parent Commit 前另一个 Worker 已经替换 Children。
- Context Read 与 Frontier Commit 同时发生。
- 同一会话连续快速发送 20 条消息。
- 进程重启导致内存 Raw Window Anchor 消失。
- Reset 与 Pending Job 同时发生。
- Summary 已生成，但 Context Budget 极小。
- Summary 结构正确但包含提示注入式文本。

---

## 12. 最终质量门槛

### 架构

- [ ] Summary 不进入 Memory V2。
- [ ] Raw Events 不删除。
- [ ] Foreground 无 Compaction 模型调用（extractive 允许）。
- [ ] 无覆盖时禁止切窗。
- [ ] SESSION 摘要在 static 之后、history 之前。
- [ ] SESSION 不出现在当前消息正文，也不计入 `dynamic_characters`。
- [ ] static 前缀不含 rollup。
- [ ] 有覆盖时 `rollup_characters + history_characters` 不超过扣摘要前的 history 余额。
- [ ] 同一 `source_fingerprint` 不能两行同时 active。
- [ ] Context Summary 与 Raw Tail 单 Snapshot 读取。
- [ ] Summary 不能成为 Reply Target。
- [ ] 缩短近窗后 around 能取回被覆盖原文。
- [ ] Reset Epoch 隔离。

### 数据

- [ ] Summary Member Coverage 100%。
- [ ] Active Frontier 无重叠。
- [ ] Active Frontier 无非法 Gap。
- [ ] Parent/Child 同时 Active 为 0。
- [ ] Orphan Member 为 0。
- [ ] Cross-conversation Member 为 0。
- [ ] 重建 Fingerprint 可重复。

### 语义

- [ ] 问句误写成事实为 0。
- [ ] 相互矛盾内容保留。
- [ ] Open Loops 保留率达到测试门槛。
- [ ] 已确认决定保留率达到测试门槛。
- [ ] Tool Durable Effect 不被错误声明。
- [ ] Summary 不覆盖实时状态。
- [ ] Sensitive Pattern 不进入远程 Summary Input 或最终 Summary。

### 性能

- [ ] 普通聊天 Foreground 模型调用数不增加。
- [ ] Context Build P95 增量 <= 10ms。
- [ ] Event Observe P95 < 5ms。
- [ ] Frontier Query P95 < 10ms。
- [ ] 长会话 Dynamic History 字符平均降低 >= 40%。
- [ ] 历史科目（uncached history）下降 30–50%。
- [ ] Worker 故障不阻塞聊天。

### 工程

- [ ] Ruff 通过。
- [ ] mypy strict 通过。
- [ ] 全量 pytest 通过。
- [ ] Alembic 0041 通过。
- [ ] SQLite integrity check 通过。
- [ ] 3.6.0 -> 3.6.1 Upgrade Smoke 通过。
- [ ] Docker Smoke 通过。

---

## 13. 最终文件变更预览

```text
src/qq_ai_bot/conversation/history/
├── __init__.py
├── db_models.py
├── errors.py
├── metrics.py
├── models.py
├── policy.py
├── renderer.py
├── repository.py
├── service.py
├── source.py
├── summarizer.py
└── worker.py

migrations/versions/
└── 0041_conversation_history_rollup.py

tests/unit/
├── test_conversation_history_schema.py
├── test_conversation_history_repository.py
├── test_conversation_history_policy.py
├── test_conversation_history_source.py
├── test_conversation_history_summarizer.py
├── test_conversation_history_worker.py
├── test_conversation_history_l0_service.py
├── test_conversation_history_hierarchical_rollup.py
├── test_context_assembler_rollup.py
├── test_prompt_compiler_session.py
├── test_prompt_prefix_stability.py
├── test_reply_target_visibility.py
├── test_conversation_history_cli.py
└── test_conversation_history_health.py

scripts/
├── measure_history_rollup.py
└── evaluate_history_rollup.py

docs/
├── architecture/conversation-rollup.md
├── performance/3.6.1-history-rollup-report.md
├── releases/v3.6.1.md
└── upgrade-3.6.1.md
```

现有重点修改：

```text
src/qq_ai_bot/services/context_assembler.py
src/qq_ai_bot/services/prompt_composer.py
src/qq_ai_bot/prompting/compiler.py
src/qq_ai_bot/prompting/models.py
src/qq_ai_bot/services/agent_tools.py
src/qq_ai_bot/capabilities/provider.py
src/qq_ai_bot/persistence/models.py
src/qq_ai_bot/persistence/event_repository.py
src/qq_ai_bot/application/modules/persistence.py
src/qq_ai_bot/application/modules/conversation.py
src/qq_ai_bot/model_runtime/models.py
src/qq_ai_bot/config.py
config/model_profiles.example.toml
.env.example
pyproject.toml
CHANGELOG.md
README.md
docs/architecture/conversation-rollup.md
```

---

## 14. 最终设计判断

Yuki 的最佳实现不是简单复制 Diana 的“12 条摘要变 month，12 个 month 变 year”，也不是把 Anthropic 或 OpenAI 的 Provider Compaction 直接包在 Responses 上。

最适合 Yuki 3.6.1 的形态是：

```text
Event Ledger 是永久依据
Memory Runtime 维护长期事实
Conversation History Runtime 维护可重建的会话摘要层
  extractive 保证切窗不挖空
  Flash 异步升级同一 fingerprint
Context Assembler 编译 SESSION Frontier + uncovered Raw Tail
get_chat_history_around 把细节留在磁盘
Background Worker 执行结构化压缩
有覆盖后主动缩短近窗，从历史科目省 token
```

这保留了 Yuki 当前最强的事实治理和事务安全，同时补上长期会话连续性。实现后，Yuki 将同时具备：

- 事实型长期记忆。
- 会话型历史连续性。
- 精确近期上下文。
- 可追溯的层级摘要。
- 不增加普通聊天前置模型成本的 Context Compaction。
- 近窗缩短后仍能按 event_id 回读原文。

层级 L1/L2 仍然值得做，但 **第一期 ROI 在 extractive + 缩短近窗 + around**。不要为了分层把双轨和回读挤出 12 个 commit。Commit 8 可以在 L0 双轨与 Context 接入之后做，顺序不变。

不够彻底就会发生的三件事，本修订视为硬失败：

1. 只改 `PromptStability.SESSION` 标签、不改 `compile()`：摘要每轮挂在当前消息上，近窗原文一点没少。
2. 摘要叠在 12k 池外面：历史科目上升。
3. fingerprint 带上 `summarizer_version`：extractive 与 Flash 并排 active，Prompt 付两份钱。

