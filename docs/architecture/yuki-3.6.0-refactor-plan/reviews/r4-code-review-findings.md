# R4 开工前代码审阅结论（r4-code-review-findings）

> 基线：当前分支 `codex/refactor-3.6-runtime`（R1/R2/R3 已合入；产品版本仍为 Yuki `3.5.3`，Alembic head `0038`）。
> 对照任务书：`04-R4-CONVERSATION-RUNTIME.md`，已按 `main@2695484` 与本分支实码核对。
> 审阅范围：任务书第 1 节列出的 processor / chat / policies / necessity / turn_coordinator /
> reply_sequence / reply_target / emoji / speech / automation / context_assembler /
> prompt_composer / source_policy / autonomous_groups / application/modules/conversation，
> 外加 `planner/models.py`、`planner/service.py`、`planner/context.py`、`plugin_host/background_turns.py`、
> `conversation/runtime.py`、`conversation/admission.py`、`memory/runtime/resolver.py`。

R1 已冻结 `ConversationTurnSession`（`prepare` / `run_agent` / `deliver` / `close`）、
`ConversationRuntime.begin_turn`、`TurnRuntimeCore`、`InboundMessagePolicy`、
`AutonomousParticipationPolicy`。生产编排仍全部在 `MessageProcessor._plan_turn()` →
`PlannerService.plan()` → `ChatService.respond(..., planned_turn=...)`。
本轮是单 owner 切换——Conversation Runtime 生效时同步删除主路径上的 Planner LLM、
WAIT、Planner 拥有的 voice/emoji/delivery/reply-target，不得保留兼容双轨。

---

## 1. 术语对照（实现期不得混用）

| 名字 | 3.5.3 真实含义 | R4 新含义 | 禁止 |
|---|---|---|---|
| `AdmissionDecision` | **R1 已占用**：inbound 结果（`DIRECT` / `AUTONOMOUS_CANDIDATE` / `OBSERVE` / `REJECT`） | **保持 R1 形状**，由 `InboundMessagePolicy` 产出 | 不得把 necessity 快照改名为 `AdmissionDecision` |
| `ReplyNecessitySnapshot` | Planner 入场前的 0..100 评分快照；direct 轮次也会记 `necessity_score` | 详细分项改称 `AdmissionScoreSnapshot`；对外协议仍用已冻结的 `AutonomousAdmissionScore` | 不得与 inbound `AdmissionDecision` 抢名 |
| `ReplyNecessityScorer` | 对所有进入 Planner 的轮次打分；private/reply-to-bot/@ 走 forced 但仍计分 | `LocalAutonomousParticipationPolicy`：只给未触发的群观察打分，无 forced | 不得把 forced 语义当成“现状仅群聊评分” |
| `PlannerSignal` | Host 侧插件加分结构；SDK 已是 `AdmissionSignal` | Host 评分只消费 SDK/`AdmissionSignal`；adapter 不再经 `PlannerSignal` | 不得再让主路径依赖 `planner.models.PlannerSignal` |
| `evaluate_message` | 私聊 / @ / `/ai` / 直达命令；**不含**回复 Yuki | inbound 显式增加 reply-to-bot → `DIRECT`，不评分 | 不得继续把回复 Yuki 留在 scorer forced 里 |
| `PlannerDecision.SILENT/WAIT` | 主路径和第二轮 Planner 的静默/等待 | 删除。低分 = 0 LLM；等待只走 `AutonomousGroupService` debounce | 不得保留 `_plan_turn()` WAIT 重入 |
| `TurnPlan.delivery_mode/desired_messages` | Planner 预测条数与发送形态 | `ReplySequenceSpec`：默认不预测条数；`reply.hard_max_messages` 为硬上限 | 不得继续 import Planner 做分块 |
| `voice.agent_tool=REQUIRED` | 本轮是否曝光 `send_voice` | 曝光只看 speech feature；Agent 用 `send_voice` Tool Call 请求效果 | 不得再用 Planner 授权语音工具 |
| `voice.preference_change` | Planner 字段 → `processor` 调 `voice_preferences.apply()` | 新增 write tool `set_voice_preference`，必须有 DB 回执 | 不得把一次性 `send_voice` 写成偏好 |
| `planner_emoji_only` | Planner `emoji_only` 时跳过 Agent | 合法可见输出来自成功的 `send_emoji` receipt；失败回退正文 | 不得再跳过 Main Agent 发 Planner 表情 |
| `ConversationRuntime.handle_turn` | 任务书最终入口；R1 Protocol 只有 `begin_turn` | 生产类增加 `handle_turn`（不改 Protocol 成员）；内部 `prepare` / `run_agent` / `deliver` | 不得把 Protocol 的 `run_agent` 改名为 `run` |

`conversation_summary` 在 3.6.0 固定为 `None`（任务书 §4.2）。

---

## 2. Planner 当前控制的每一个用户可见行为

### 2.1 SILENT / WAIT

- `MessageProcessor._plan_turn()` 必调 `PlannerService.plan()`（LLM）。
- `SILENT` → `ProcessResult(reason="planner_silent")`，0 发送。
- `WAIT` → `asyncio.sleep(wait_seconds)` 后 **再 plan 一次**；第二次仍 WAIT 则静默。
- `AutonomousGroupService._plan_latest_admitted()` 同样：debounce 之后仍调 Planner，WAIT 再 plan 一次。
- 私聊 / @ 也会进入这条 Planner 路径（`evaluate_message` 已 `should_respond=True`）。

R4 锁定：主路径 0 Planner LLM。WAIT 只复用现有 per-group coalescing/revision/debounce。Coordinator 不拥有 debounce。private/@/reply 不进 autonomous debounce。

### 2.2 reply_to_event_id

- Planner 输出 `TurnPlan.reply_to_event_id`；`ChatService._resolve_reply_target` 优先 Agent `set_reply_target` override，否则用 Planner 目标。
- 无 Planner override 时默认 **不引用**（私聊不引用；回复 Yuki / 明确 @ 也不自动引用）。
- Quote 发送失败已有 retry-without-quote。

R4 锁定：删除 Planner 默认目标。需要引用时只接受本轮 visible event ids 的 `set_reply_target`。

### 2.3 desired_messages / delivery_mode

- `ReplySequenceManager.render(..., plan: TurnPlan)` 按 `delivery_mode` 决定是否按行拆分，硬上限 `reply.plan_hard_max_messages`。
- Planner 可把 `desired_messages` 顶到硬上限。

R4 锁定：`ReplySequenceSpec` 取代 `TurnPlan`。默认不预测条数。`reply.plan_hard_max_messages` 迁移为 `reply.hard_max_messages`（snapshot 双读旧键直到 R5）。`set_reply_layout(max_messages, split_hint=auto|sentence|paragraph)` 是普通 NL 的唯一布局表达。

### 2.4 emoji-only

- Planner `emoji.is_exclusive` 时 `ChatService.respond` **跳过 Agent**（`tools_closed`、空 messages），只发送表情效果。
- Planner 把 `PendingReplyEffect(source="planner")` 塞进 `reply_effects`。

R4 锁定：Main Agent 必须实际调用 `send_emoji`。`emoji_only` 成功 receipt 是合法输出；失败且有正文则回退正文。不再有 Planner 预置效果，也不再跳过 Agent。

### 2.5 voice preference change

- 3.5.3 **没有** `set_voice_preference` 工具。
- Planner `voice.preference_change` → processor 在 chat **之前** 调 `VoicePreferenceService.apply()`。
- 一次性语音不应当写库；现状靠 Planner 字段区分。

R4 锁定：持久偏好只走新增 `set_voice_preference` write capability，必须有真实 DB 回执。`send_voice` 不改长期偏好。

### 2.6 voice tool authorization

- `send_voice` 已存在，schema 仅 `style_hint` / `language`。
- 曝光条件：`ToolRuntime.voice_tool_authorized` = Planner `voice.agent_tool is REQUIRED`。
- 拒绝文案仍写“Planner 未确认…”。
- 自发语音 cadence 读 `planner_runs`（`decision=reply` 且 `voice_intent=neutral`）。

R4 锁定：保留工具名 `send_voice`，升级 schema 为 `mode=voice_only|text_and_voice`、`request_basis=user_requested|agent_initiated`。曝光只看 speech feature。`voice_only` 仅在 transport receipt 成功后抑制正文。cadence 改读 `reply_effect_events`（0039），切换后不双读旧表。

### 2.7 automation special route

- `is_scheduled_automation_request(content)` 仍是本地 hint；命中后 host-pin `automation_create` 并追加 system 说明。
- 不强制创建；`confirmation=persisted` 才可声称成功。
- Memory exclusive write 时 automation write 已隐藏。

R4 锁定：保留该 hint，不新增 NL 短语分类器。其余自然语言只影响 Capability Search 软分数。

### 2.8 Planner fail-closed

- `reason_code in {timeout/invalid/provider_error fallback}` 且非 emoji-only 时，chat **不进 Agent**，发送固定失败文案。

R4 锁定：删除这条 Planner fail-closed。模型/配置失败走 processor 现有 `LLMError` 路径。

### 2.9 autonomous group path

- `evaluate_message` 对未 @ 群消息返回 `should_respond=False` / `group_not_triggered`。
- processor 在 `planner.group_enabled` 且群 `autonomous_enabled` 时 `observe()`。
- debounce 后 **仍然 Planner LLM** 决定 REPLY/SILENT/WAIT。
- `chat.respond(autonomous=True, planned_turn=...)`：关 automation/admin/onebot，但 **未** `read_only`，且 Memory resolver 仍把 `AUTONOMOUS_GROUP` 算进 `_WRITE_ORIGINS`。

R4 锁定：debounce 后只跑本地 `LocalAutonomousParticipationPolicy`。低分 0 LLM；高分一次 Main Agent，仅该 origin 曝光 `decline_reply`。默认禁止 persistent write（相对 3.5.3 的收紧，必须进 release note）：`read_only=True` 且 memory `_WRITE_ORIGINS` 去掉 `AUTONOMOUS_GROUP`。

### 2.10 external / plugin background path

- `PluginBackgroundTurnWorker` 为每个 job 调 Planner；非 `REPLY` 则不生成。
- `generate_external_reply(..., planned_turn=)` 仍把 plan 写入 prompt。
- 合成 `InboundMessage` 仅作 authority 信封，不入 ledger（已符合 R1 trigger 纪律）。

R4 锁定：删除该路径 Planner。background 不再用 LLM 决定是否开口；tool-free / `read_only` 的 Main Agent 直接生成。不伪造用户 inbound。这是行为变化：3.5.3 可由 Planner SILENT 吞掉 background 回复。

---

## 3. Inbound 与评分范围（双重变化）

现状：

- `services/policies.py` **没有** reply-to-bot 分支。判定用 `inbound.reply_sender_user_id == inbound.bot_user_id`（`planner/context.py:122-125`）。
- `ReplyNecessityScorer` 对 **所有** Planner 轮次打分。private / reply-to-bot / @ 用 forced 过阈值，但 `necessity_score` 仍写入 `planner_runs`。

R4 锁定：

- `replies_to_bot(message)` 成为 `InboundMessagePolicy` / `evaluate_message` 的显式分支 → `DIRECT`，**完全不评分**。
- 回放对比 admission 指标时，direct 轮次此后无分数样本，必须单独标注口径。
- 评分只用于 `AUTONOMOUS_CANDIDATE`。删除 scorer 内 private/reply/mention forced。
- 不设置灰区 LLM Judge。

`AdmissionMode` 映射：

| 现状 `PolicyDecision.reason` | R4 `AdmissionMode` |
|---|---|
| `private_allowed` / `group_triggered` / `group_reply_to_bot` / `superuser_group_enable` | `DIRECT` |
| `group_not_triggered`（群启用且 autonomous 开） | `AUTONOMOUS_CANDIDATE` → observe + 本地评分 |
| `group_not_triggered`（autonomous 关） | `OBSERVE`（写 ledger，不进 Agent） |
| `group_disabled` / `private_not_allowed` / `bot_message` | `REJECT` |

---

## 4. ConversationTurnSession 与 ChatService

- R1 Protocol 方法是 `run_agent`，不是任务书示例里的 `run`。实现跟 Protocol。
- 任务书最终入口 `ConversationRuntime.handle_turn(...)` 加在生产类上，**不修改** Protocol 成员。
- `ChatService.respond(... planned_turn=...)` 删除。测试与 processor 改走 `handle_turn` / 无 plan 的薄 `respond`。
- 禁止把 `conversation/host.py` 写成第二份巨型 `chat.py`。生产 session 编排 prepare/run/deliver；context 装配、AgentRunner、emoji/speech prepare 仍是现有 collaborator。
- `PreparedTurn.reply_target` 是 `runtime.turn.ReplyTargetControl`（`reply_to_message_id` / `pinned`）。生产覆盖控制仍用 `services.reply_target.ReplyTargetControl`（visible event ids）。prepare 只填默认（不引用，未 pinned）；deliver 再解析 Agent override。

ContextAssembler **已经不接收** `planned_turn`。仍要删的是 `PromptComposer._plan_contribution` 与 `compose(..., planned_turn=)`。`CORE_CONTRACT` 里的 “TurnPlan 中的媒体…” 改为 Runtime 事实（工具回执后才能声称发送成功）。

---

## 5. 新工具与 cadence

R4 新增/升级（不得当成 R3 已有工具漏建）：

| 工具 | 性质 | 锁定 |
|---|---|---|
| `send_voice` | 已有，升 schema | `mode` + `request_basis`；feature 不可用则不曝光 |
| `set_voice_preference` | **新增 write** | 唯一持久偏好写路径 |
| `send_emoji` | 新增 | `mode=emoji_only\|with_text`；Selector 只在 tool 内部跑 |
| `set_reply_layout` | 新增 reply-control | `max_messages`∈`1..hard_max`，`split_hint`∈`auto/sentence/paragraph` |
| `set_reply_target` | 已有 | 仅 visible event ids |
| `decline_reply` | 新增 terminal control | 只 AUTONOMOUS_GROUP；`reason_code` 四枚举；必须是尚无 effect 的单独 batch；拒绝则整批不执行；成功后 0 continuation、0 delivery。Direct 伪造调用后端拒绝。不依赖 `tool_choice`。 |

`HOST_REPLY_CONTROL` 已是受信任的 `TerminalFinalizationSource`。`decline_reply` 成功后走该 source 结束 loop；`AgentRunner` 需识别 host backend 的 decline，避免再发一轮模型请求。

0039 `reply_effect_events`：

- 一轮一行；唯一 `(source, source_event_hash)`；索引 `(conversation_key_hash, occurred_at, id)`。
- 字段按任务书 §5.3；不存正文 / 平台 message id / 模型理由 / 资产路径。
- **conversation_key_hash 必须与 `planner_runs` 使用同一算法**（`hash_planner_identifier(..., kind="conversation")`），否则回填行与新行无法连续计算 cadence。R5 可改名函数，不得改算法。
- 回填：每会话最近 20 条 `planner_decision=reply AND voice_intent=neutral`；全部 eligible；`voice_sent = voice_mode in {voice,text_and_voice,optional}`；`text_sent = voice_mode in {text,text_and_voice}`；旧 emoji 不回填 true。
- 新 cadence：最近 20 条 eligible 中 `voice_sent=true` 的比例。`request_basis=user_requested` 与精确 opt-out 不进分母；agent-initiated 与普通未发语音回复进入。
- 切换后只读新表。Maintenance：`recorded_at` > 90 天删除；每会话最多 100 行。
- R5 `0040` 才 drop `planner_runs`；本轮不双读。

配置：R4 注册 `conversation.*`、`reply.hard_max_messages`、`speech.agent_effects_enabled`，snapshot **双读** 旧 `planner.*` / `reply.plan_hard_max_messages` / `speech.planner_enabled`。R5 的 `migrate-3-6` 才删旧 override 键。测试里未填 `conversation=` 的 `RuntimeConfigSnapshot` 必须仍能从 `planner` 派生。

---

## 6. 实现锁定（偏差）

1. **命名**：保留 R1 `AdmissionDecision`。必要性快照不叫 `AdmissionDecision`。
2. **Protocol**：`run_agent` 不改名。`handle_turn` 只存在于生产类。
3. **hash**：cadence 会话键沿用 planner identifier hash，保证回填连续。
4. **AUTONOMOUS_GROUP write**：默认 `read_only` + memory 禁止 persistent write。3.5.3 允许自主群 `memory_change`，这是安全收紧。
5. **Plugin background**：去 Planner 后不再 SILENT 过滤，直接 tool-free 生成。
6. **emoji-only 不再跳过 Agent**：明确索要表情也要 1–2 次 foreground Agent 请求（任务书 13.2 的单 Tool 黄金用例）。
7. **`planner/necessity.py`**：逻辑迁到 `conversation/participation.py`；旧模块只做 re-export，供 R5 删除。
8. **Coordinator stage 名** `planner` 可暂时保留（R5 再改），主路径不再 `track(..., "planner")`。
9. **不发明 NL 短语分类器**：不把 `EmojiRequestDetector` / voice 短语表接到主路径。Capability Search metadata 已有软 hint。
10. **不删除** `src/qq_ai_bot/planner/` 包（R5）。主路径与 autonomous / background 停止调用。

---

## 7. 测试与 Alembic

必须覆盖任务书 §13 的核心回归（本轮可测部分）：

- 私聊 / reply-to-bot / @ → DIRECT，无 necessity_score。
- disabled group reject；low-value group silent（0 LLM）；fast conversation presence penalty。
- `decline_reply` 仅 autonomous 可见；direct 伪造拒绝；与其他 tool 同 batch 整批拒绝且无副作用。
- 普通文本黄金路径：进入 Main Agent 前 0 foreground Planner/router LLM；成功时恰好 1 个 Agent 请求。
- silent：0；autonomous decline：1 Agent、0 continuation、0 delivery。
- DeepSeek 请求不含 `tool_choice`。
- voice explicit / unavailable / one-shot 不改偏好；cadence 分母规则。
- emoji-only 成功；optional emoji 失败保留正文。
- quote 失败 retry-without-quote。
- 0039 建表、幂等回填、唯一约束。

Alembic head 针：`release_check.py`、`release_validate.py`、`release_smoke.py`、`test_memory_migration_matrix.py`、`test_memory_quality_governance.py`、`test_user_profiles.py`、`test_versioned_docker_release.py` 从 `0038` 改为 `0039`。

`tests/conftest.py` 与集成测试不得再给普通聊天注入 `FakePlannerProvider` / `planned_turn`。
