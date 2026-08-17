# R4：替换主路径为 Conversation Runtime

> 目标：删除 Planner 对 reply/silent/wait、voice、emoji、delivery、reply target、automation hint 的所有权。  
> 本轮结束时，普通消息从 MessageProcessor 直接进入本地 Runtime 和 Main Agent，前置生成式 Router/Planner 请求为 0。
> 已按代码核对：`main@2695484`。

> **代码审阅批注**：`ReplyNecessityScorer` 不是 direct message admission；它主要决定未 @ 的群消息是否值得进入 Planner。把它简单改名并让高分必回会增加误插话。Voice/Emoji/Delivery 的原稿还保留二选一，不能作为实施合同，本版锁定为受约束 reply-effect Tool + Runtime 执行门。

## 1. 开工前 Codex 审阅

重点阅读：

```text
src/qq_ai_bot/services/processor.py
src/qq_ai_bot/services/chat.py
src/qq_ai_bot/services/policies.py
src/qq_ai_bot/planner/necessity.py
src/qq_ai_bot/services/turn_coordinator.py
src/qq_ai_bot/services/reply_sequence.py
src/qq_ai_bot/services/reply_target.py
src/qq_ai_bot/emoji/*
src/qq_ai_bot/speech/*
src/qq_ai_bot/automation/intent.py
src/qq_ai_bot/automation/*
src/qq_ai_bot/services/context_assembler.py
src/qq_ai_bot/services/prompt_composer.py
src/qq_ai_bot/services/source_policy.py
src/qq_ai_bot/services/source_renderer.py
src/qq_ai_bot/services/autonomous_groups.py
src/qq_ai_bot/application/modules/conversation.py
```

Codex 必须列出 Planner 当前控制的每一个用户可见行为，特别是：

- SILENT/WAIT。
- reply_to_event_id。
- desired_messages/delivery_mode。
- emoji-only。
- voice preference change。
- voice tool authorization。
- automation special route。
- Planner fail-closed。
- autonomous group path。
- external reply path。

输出 `docs/architecture/yuki-3.6.0-refactor-plan/reviews/r4-code-review-findings.md`。

---

## 2. Inbound 与 Autonomous Admission

拆成两个所有者：

```text
InboundMessagePolicy                 # 保留 services/policies.py 的 private/mention/direct-command/disabled 判定，并新增“回复 Yuki”显式分支
AutonomousParticipationPolicy       # 迁移 planner/necessity.py 的群聊自主评分
```

重命名：

```text
ReplyNecessityScorer -> AutonomousParticipationPolicy
ReplyNecessityFeatures -> AdmissionFeatures
ReplyNecessitySnapshot -> AdmissionDecision
PlannerSignal -> AdmissionSignal
```

> **代码审阅批注（评分范围）**：“回复 Yuki”的直达准入现状不在 `services/policies.py`，而在 `planner/necessity.py` 的 forced 逻辑（private/reply_target_is_bot/mentions_bot）。且 3.5.3 的 `ReplyNecessityScorer.score()` 对所有进入 Planner 的轮次运行：direct 轮次 forced 通过阈值门但仍计算并把 necessity_score 记入 `planner_runs`。R4 改为 direct 轮次完全不评分，是行为与观测的双重变化——迁移 `AutonomousParticipationPolicy` 时不得把 forced 语义误当成“现状仅群聊评分”，回放对比 admission 指标时 direct 轮次此后无分数样本，需单独标注口径。

### 2.1 规则

```text
private message              -> direct Agent turn（不评分）
reply to Yuki                -> direct Agent turn（不评分）
mention Yuki                 -> direct Agent turn（不评分）
direct command               -> command path
disabled group/private       -> reject
low group score              -> silent
high group score             -> 调用一次 Main Agent；允许受控 decline_reply Tool
```

3.6.0 不设置前置灰区 LLM Judge。低分仍为 0 调用；只有 autonomous origin 才曝光本地 `decline_reply(reason_code)` terminal control Tool，`reason_code` 固定为 `not_relevant / would_interrupt / insufficient_context / duplicate`。它不落自由文本理由，不产生业务副作用或持久化，调用后 Runtime 直接结束本轮而不再发 continuation；private/@/reply/direct 不曝光且后端拒绝调用。它只允许作为尚未执行任何 Tool/持久 effect 时的单独 Batch call；ToolCoordinator 在执行前发现夹带其他调用或已有 effect 时拒绝整批，绝不先执行副作用再静默。它不依赖 `tool_choice`，Main Agent 也可正常生成回复。阈值以 precision、false-intervention rate、recall 和 response-rate delta 的回放共同调整。

### 2.2 Group WAIT

Planner WAIT 删除：

- 快速群聊复用 `AutonomousGroupService` 的 per-group coalescing worker、revision、debounce、changed-event handoff；需要抽象时命名为 `ConversationBatcher`。
- `ConversationTurnCoordinator` 只做 version、取消、supersede、protected direct turn 和 mutation shield，不拥有 debounce/batching。
- 新消息 supersede 旧 turn。
- 一条消息不允许引发第二次“是否等待”的模型请求。
- private/direct @/reply 保持即时，不进入 autonomous debounce。

---

## 3. ConversationTurnSession

正式接管主路径：

```python
session = await conversation_runtime.begin_turn(message_trigger, authority_snapshot)

prepared = await session.prepare()
result = await session.run(prepared)
return await session.deliver(result)
```

`MessageProcessor` 保留：

- inbound normalization
- effective group/private policy
- direct commands
- rate limit
- dedup
- identity/profile
- ledger append
- vision preparation
- relationship job enqueue

其中 `/ai forgetme`、native/direct plugin command、限流/空输入/过长输入/Vision failure 等合法 early-return 不强迫进入 Agent Runtime；不响应的群 observation 仍先写 Ledger 并排队 Memory extraction。

`MessageProcessor` 删除：

- PlannerContextBuilder
- PlannerService
- `_plan_turn()`
- Planner WAIT
- Planner delivery record
- Planner exception types
- PlannedTurn 传递

---

## 4. Context Preparation

ContextAssembler 输入改为：

```text
TurnContext
MemoryPrefetchResult
ConversationState
VisualObservation
CapabilityExposureSummary
RecallHandle/Exposure tokens
Delivery/External event facts
```

不再接收 `planned_turn`。

### 4.1 固定顺序

```text
Static system: identity / hard rules / stable tool-use rules
Bounded recent history: preserved user/assistant roles
Dynamic system envelope:
  current authority / scene / source boundaries
  current conversation state / recent delivery / external event facts
  memory context blocks / exposure metadata
  visual observation
Current user message
```

锁定保留当前 `PromptCompiler` 的 `static system -> history -> dynamic system -> current` role/serialization 合同，而不是把每轮变化的 Authority 提到 history 前破坏稳定前缀。Dynamic envelope 内按上表排序并带显式 source/trust 标记。Chat Completions 与 Responses 的协议映射分别冻结快照；Adapter 不得为了“合并 system”改变历史角色或把 dynamic data 降成 user content。

### 4.2 Context Budget

R4 保留并明确现有预算所有权，不在同轮发明另一套不兼容数值：

| 内容 | 预算/优先级 | 不足时行为 |
|---|---|---|
| static system、current user message | required，受整体 provider context window 校验 | 超限 fail closed，不截掉权限/当前消息 |
| authority、scene、source boundaries | required dynamic contribution | 超过 dynamic budget 时 `context_required_budget_exceeded`，不降级为 user text |
| current-image visual observation | 图片轮次 required；非图片不存在 | Vision 已失败时沿用 Processor early return，不造占位结论 |
| memory grounding rules | required；memory facts 按 rerank/consumer order optional | 依次删除最低优先 facts，并只对真实 payload facts confirm exposure |
| conversation state、recent delivery、external facts | typed optional，高于普通插件片段 | 按时间/priority 稳定裁剪 |
| plugin prompt fragments | 沿用 per-plugin/total prompt budget，optional | 低 priority 先移除，不影响 authority |
| history | 使用 metadata 后剩余额度和既有 anchored high/low watermark | 按现有窗口滚动，不随机截断单条消息 |
| tool results/artifacts | 继续由 AgentRunner 的 Tool Result Budget 所有 | 大结果转 artifact，不挤占 initial history budget |

整体继续使用 `max_context_characters`；dynamic metadata 沿用 `context_metadata_budget_ratio`，history 沿用 `context.local_event_limit` 与 `history_window_low_watermark_ratio`，Plugin 沿用已有 fragment/total budget。每项记录 required/optional、priority、实际字符、丢弃原因和 provider token estimator version。Memory metadata 与最终 payload fact ids 一起冻结，以供实际请求 hook 确认 injected；`available_memory_subjects`、recent delivery、external event、visual observations、source boundaries 和 attribution runtime/receipt handle 不得在迁移中丢失。

会话 Rollup 可以作为 3.6.x 后续工作；3.6.0 的 `conversation_summary` 固定为 `None`，直到定义 source/trust/version/invalidation 合同，禁止先塞入无来源摘要。

---

## 5. Voice Runtime

### 5.1 单次语音 Reply Effect

普通自然语言不建立固定短语 parser。`reply.voice` 由 Capability Search 的 namespace/aliases/use_when 软命中，Main Agent 调用扩展后的受约束 Tool：

```text
send_voice(mode=voice_only|text_and_voice, request_basis=user_requested|agent_initiated, style?, language?)
```

保留仓库现有的 canonical 工具名 `send_voice`，只提升 schema 和后端回执语义；无需为了 Runtime 重构制造一次无价值的工具重命名与模型快照迁移。

结果：

- `reply.voice` Namespace 获得搜索加分。
- speech feature unavailable 时不曝光。
- Main Agent 选择 mode/style/language；Runtime 校验 feature/profile/文件/参考音频并负责实际发送。
- `request_basis` 只决定 cadence/审计归类，不扩大权限；精确 direct command 可由后端覆盖，普通 Tool Call 使用枚举值。
- Voice Profile、文件、参考音频仍由 Runtime 掌握。
- `voice_only` 只有在对应语音 transport receipt 成功后才抑制正文；生成或发送失败时回退已生成正文，不能留下空回复或伪造 voice delivery。

### 5.2 偏好变化

Planner 不再写 VoicePreferenceChange。

`set_voice_preference` 是 R4 新增 write capability：3.5.3 没有该工具，偏好变化由 Planner 输出 `voice.preference_change` 字段、`processor` 调用 `voice_preferences.apply()` 落库。R4 需把这条数据库写路径迁为工具后端，保留原有回执与审计语义，不得因 R3 迁移表把它当成既有工具而漏建。

锁定规则：

- 持久偏好只走独立 `set_voice_preference` write capability。
- 必须有真实数据库写回执。
- 普通“一次用语音”不修改长期偏好。

### 5.3 自发语音

- 由 Runtime 频率限制。
- Main Agent 只能通过本轮实际的 `send_voice` Tool Call 请求语音效果；不解析隐藏字段或正文 hint。
- 不增加前置模型。
- 发送成功后再记录使用。
- 基线迁移号未变化时，R4 的 `0039` 新建 `reply_effect_events`，一轮一行，字段固定为 `conversation_key_hash / runtime_turn_id? / source_event_hash / text_sent / voice_sent / emoji_sent / voice_cadence_eligible / voice_request_basis(user_requested|agent_initiated|none) / source(runtime|migrated_planner) / occurred_at / recorded_at`；以 `(source, source_event_hash)` 唯一并索引 `(conversation_key_hash, occurred_at, id)`。这样 `text_and_voice` 不会因拆成两行扭曲 cadence 分母。表不存正文、平台 message id、模型理由或资产路径。新 Runtime 只在 confirmed delivery 后写入：没有显式语音请求的正常回复默认 cadence eligible；`request_basis=user_requested` 与精确 opt-out command 不进入分母，agent-initiated voice 进入。旧数据按每会话最近 20 条 `decision=reply/voice_intent=neutral` 一行一行幂等回填，全部 eligible，`voice_sent = voice_mode in {voice,text_and_voice,optional}`，`text_sent = voice_mode in {text,text_and_voice}`，无发送证据的旧 emoji 不回填为 true。新 cadence 固定为最近 20 条 eligible 事件中 `voice_sent=true` 的比例。普通自然语言 opt-out 若没有结构化 command/Tool receipt，只会作为一次未发送语音的 eligible 事件，保守降低而不会提高自发语音频率。切换后只从新表计算 cadence。Maintenance 删除 recorded_at 超过 90 天的行，并将每会话最多保留 100 条。R5 的 `0040` 才删除旧 Planner 表；不保留生产双读。

---

## 6. Emoji Runtime

### 6.1 Emoji Reply Effect

锁定为受约束 `send_emoji(mode=emoji_only|with_text, placement, goal, emotion?)` Tool。Capability Search 负责自然语言软命中；只有平台精确定义的 direct command 可绕过 Main Agent。Emoji Selector 仍可在 Tool 执行内部选择资产，但不成为前置 Router。

### 6.2 自发表情

- Runtime 检查最近发送比例、可用资产和场景。
- Main Agent 只能通过本轮实际的 `send_emoji` Tool Call 请求表情效果；不解析隐藏字段或正文 hint。
- Runtime 决定是否执行。
- optional emoji 失败不能让正常文字回复消失。
- `emoji_only` 成功 receipt 是合法可见输出；失败时若有 Agent 正文则回退正文，不伪造成功。

原有发送失败处理、asset 记录和 Ledger 记录保留。

---

## 7. Delivery Runtime

Planner 字段删除：

```text
delivery_mode
desired_messages
```

新规则：

- Main Agent 只生成完整正文。
- `clean_model_output` 负责清理。
- `split_qq_message` 与 ReplySequence 负责平台分块。
- 精确平台 command 可直接设置布局；普通自然语言通过 `set_reply_layout(max_messages, split_hint)` reply-control Tool 表达，其中 `max_messages` 为 `1..reply.hard_max_messages`，`split_hint` 固定为 `auto/sentence/paragraph`，后端再次 clamp；不维护关键词 parser。
- 默认不预测消息数量。
- 新建 `ReplySequenceSpec` 取代 `TurnPlan`，至少含 layout、hard max、pacing、reply target 和 effect placement；`ReplySequenceManager` 不再 import Planner。
- `reply.plan_hard_max_messages` 迁移为 `reply.hard_max_messages`，这是输出安全上限，不能随 Planner 删除。

### 7.1 Reply Target

锁定默认规则，保持 3.5.3 无 Planner override 时的行为：

- 私聊：不引用。
- 回复 Yuki 或群聊明确 @：默认不自动引用，避免无证据改变产品行为。
- 需要引用时由 `set_reply_target(event_id)` 请求；后端只接受本轮 visible event ids。
- quote 发送失败保留现有 retry-without-quote；DeliveryOutcome 分别记录 transport accepted 与 ledger recorded。

Planner 默认目标删除。若回放证明 3.5.3 某些场景依赖自动引用，必须作为单独产品决策调整此表，而不是在实现中留“依据回放再决定”。

---

## 8. Automation

保留 `is_scheduled_automation_request()` 仅用于精确结构化/时间信号的本地 hint；其他自然语言由 Capability Search metadata 命中：

- 提高 `automation.write` Namespace 分数。
- 不强制创建任务。
- Main Agent 根据用户意图调用 `automation_create`。
- 只有 `confirmation=persisted` 和真实 `automation_id` 才能声称创建成功。

Memory mutation exclusive 时，Automation write 必须隐藏。

---

## 9. Web Route

Native Web 先作为 `web.search` capability 进入统一搜索/权限账本；被选中后 `WebProviderRouter` 再本地选择 Provider：

- Runtime 根据配置、Provider 能力和已授权 capability 选择 Native/Tavily。
- Capability Runtime 决定 Web Tool 是否曝光。
- Main Agent决定是否真正调用。
- Native failure 后的 Tavily 恢复路径保留。

Planner Scope 删除后，Web 权限只由真实配置、TurnAuthority 和动态 taint policy 决定。路由选择不能替代 Main Agent 的实际 Tool Call。

---

## 10. ChatService 拆分

当前 `chat.py` 过大。R4 建议按所有权拆分：

```text
conversation/runtime.py
conversation/context.py
conversation/effects.py
conversation/delivery.py
capabilities/chat_backend.py
capabilities/provider_registry.py
memory/runtime/*
services/agent_runner.py
```

`ChatService` 可以删除或缩小为兼容命名的薄入口，但 3.6.0 不保留旧 `respond(... planned_turn=...)` 签名。

最终入口固定为：

```python
ConversationRuntime.handle_turn(...)
```

---

## 11. Main Agent Prompt

删除所有“Planner 已决定”文本，例如：

```text
Planner 已确认语音
Planner 已给出默认目标
Planner 选择了 Tool Scope
```

替换为 Runtime 事实：

```text
本轮可用能力由后端真实权限与当前场景决定。
只有工具返回成功回执后才能声称操作完成。
需要未加载能力时调用 request_tools。
```

Main Agent 不需要知道 Planner 已被删除。

---

## 12. External / Autonomous / Scheduled Path

每一种 origin 使用同一 `TurnRuntimeCore` 协议与 Authority/Capability/Agent 不变量，但保留各自顶层 coordinator、context/history/delivery 边界：

| Origin | Admission | Memory | Write | Tool |
|---|---|---|---|---|
| USER_MESSAGE | normal | passive/read/write | authority based | full authorized |
| AUTONOMOUS_GROUP | local score | passive/read | no persistent write by default | read/reply effects |
| SCHEDULED_AUTOMATION | pre-authorized event | targeted context | action-specific | action allowlist |
| PLUGIN_SESSION | session-specific | isolated/no main memory by default | plugin contract | session allowlist |
| PLUGIN_BACKGROUND | no user reply admission | restricted | forbidden by default | plugin-declared allowlist |
| SYSTEM_TASK | deterministic | explicit | explicit only | task allowlist |

不得为不同 origin 重新建立独立 Agent Router，也不得把 scheduled/plugin session 伪造成用户 `InboundMessage`。AUTONOMOUS_GROUP 默认禁 persistent write 是相对 3.5.3 的安全收紧，必须在 release note 和回放中明确；Plugin Background 继续保持 tool-free 或显式最小 allowlist，不能自动获得完整搜索目录。

---

## 13. 测试矩阵

### 13.1 Admission

- 私聊始终 reply。
- reply-to-bot 始终 reply。
- mention 始终 reply。
- disabled group reject。
- low-value group silent。
- fast conversation presence penalty 正确。
- AdmissionSignal 每插件有限幅度，过期失效。
- autonomous high-score 的 reply/decline precision、false-intervention rate、recall 与 response-rate delta 达标。
- `decline_reply` 只在 autonomous origin 可见；direct/private/@/reply 即使模型伪造同名调用也被后端拒绝。
- `decline_reply` 与其他 Tool 同 Batch、或已有 Tool/effect 后调用时整批拒绝且不产生副作用。
- observation 不打断 protected direct turn，per-group coalescing/revision 不丢消息。

### 13.2 模型调用数

- 普通文本黄金用例（首尝试成功、无 fallback/recovery）：恰好 1 个 foreground Agent 请求。
- silent：0。
- direct command：0 或命令自身定义的调用数。
- 单 Tool 首轮命中黄金用例：恰好 2 个 foreground Agent 请求（Agent、Tool、Agent）。
- autonomous terminal `decline_reply`：恰好 1 个 foreground Agent 请求、0 个 continuation、0 个用户可见 delivery。
- 一般多 Tool/recovery/native fallback 轮次：不锁死为 2，必须不超过 AgentRunner `max_model_requests` 并逐步骤观测。
- Planner 调用：0。
- Vision、Embedding、Memory Extraction、Emoji Selector、post-delivery Attribution 分列，不伪装成 foreground Agent 请求。
- DeepSeek Main Agent 请求不含任何 `tool_choice` 字段，所有流程都不依赖 `tool_choice=required`。

### 13.3 Effects

- voice explicit。
- voice unavailable。
- one-shot voice 不改偏好。
- user-requested voice 不进入自发 cadence 分母；agent-initiated voice 与普通未发语音回复进入，最近 20 条一轮一行计算。
- emoji-only。
- optional emoji failure keeps text。
- reply target override。
- quote send failure retry without quote。

### 13.4 Automation/Web

- scheduled hint 不强制写。
- persisted receipt 才宣称成功。
- memory exclusive 隐藏 automation write。
- native web failure 切换 Tavily。

### 13.5 Context

- Memory blocks 正确。
- authority 不进入用户可控内容。
- stable prefix 顺序固定。
- Tool definitions 稳定排序。
- Plugin prompt budget 保持。
- Chat Completions/Responses role serialization 和 stable prefix frozen snapshots 均通过。

---

## 14. 性能门槛

R4 必须跑总纲回放集：

- 普通纯文本进入 Main Agent 前 foreground generative/router LLM 调用为 0；Vision/Embedding 单独报告。
- 普通文本 P50 至少下降 35%。
- 普通文本 P95 至少下降 20%。
- 平均 foreground model request/回复轮显著下降。
- Tool 首轮命中门槛保持。
- Memory quality 不下降。

---

## 15. 本轮禁止事项

- 不保留 `_plan_turn()`。
- 不保留 Planner WAIT。
- 不新增 LLM participation judge。
- 不让 Voice/Emoji 重新创建独立 Router。
- 不让 Main Agent决定权限。
- 不把 ConversationRuntime 写成新的巨型 ChatService。
- 不删除 AgentRunner 的 bounded loop。
- 不引入 Voice/Emoji/Memory/Automation 自然语言固定短语分类器。

---

## 16. 建议提交顺序

1. `feat(conversation): add deterministic admission policy`
2. `feat(conversation): add turn preparation runtime`
3. `refactor(context): remove PlannedTurn input`
4. `feat(conversation): move reply target and delivery policy`
5. `migration(reply-effects): add cadence event owner and backfill`
6. `feat(reply-effects): remove planner ownership of voice and emoji`
7. `feat(automation): route capability through local hints`
8. `refactor(processor): remove mandatory planner call`
9. `refactor(chat): split runtime ownership`
10. `test(conversation): add no-pre-agent-llm regressions`
11. `docs(refactor): record R4 code review findings`

---

## 17. R4 退出条件

- `MessageProcessor.handle()` 普通聊天不调用 Planner。
- `ChatService/ConversationRuntime` 不接受 `PlannedTurn`。
- 普通纯文本聊天进入 Main Agent 前 0 foreground generative/router LLM；Vision/Embedding 另计。
- SILENT/WAIT/Voice/Emoji/Delivery/ReplyTarget/Automation 均有新所有者。
- External/Autonomous/Scheduled 回归通过。
- 性能门槛达到。
- 全量测试通过。
