# R4 未改正偏差清单

> 日期：2026-08-18  
> 对照：`04-R4-CONVERSATION-RUNTIME.md`、总纲 §9、`06-CODE-REVIEW-DECISIONS.md`、`07-PLAN-REVIEW-REPORT.md`、`reviews/r4-code-review-findings.md`  
> 代码基线：分支 `codex/refactor-3.6-runtime`（R4 主路径已切换；产品版本仍为 `3.5.3`；Alembic head `0039`）  
> 范围：相对任务书**尚未按原文改掉**、且本轮明确不返工的偏差。已纠正项见 findings §8，不重复。

本文件只记账，不改合同。R5 开工时按「归属」列处理；标成「锁定」的项不得为了贴近任务书原稿而改回去。

---

## 1. 锁定合同偏差（R4 刻意不按任务书原文落地）

这些是 findings / 06 已经拍板的实现合同。行为面已经切换，但形状与 `04-R4` 示例代码或命名表不一致。

| ID | 任务书原文 | 现状 | 为何不改 | 归属 |
|---|---|---|---|---|
| L1 | §3 用 `begin_turn` → `prepare` / `run` / `deliver` 接管主路径 | 生产入口是 `HostConversationRuntime.handle_turn` → `ChatService.respond()`。`begin_turn` 抛 `NotImplementedError`。R1 Protocol 方法仍是 `run_agent`，没有改名为 `run` | 禁止把 `conversation/host.py` 写成第二份巨型 ChatService；不得改 Protocol 成员名 | 锁定到 3.6.0；R5 不补第二套 Runtime |
| L2 | §2 重命名表把 necessity 快照改名为 `AdmissionDecision` | R1 已占用 `AdmissionDecision`（inbound）。必要性快照是 `AdmissionScoreSnapshot` / `AutonomousAdmissionScore` | 不得与 inbound 抢名 | 锁定 |
| L3 | §2 新增独立类 `InboundMessagePolicy` | 生产 inbound 仍是 `services/policies.py` 的 `evaluate_message()`，已含 reply-to-bot 分支 | 行为已对齐 AdmissionMode 表；不另造一层包装类 | 锁定 |
| L4 | §3 `MessageProcessor` 删除 PlannerContextBuilder / PlannerService / Planner 异常类型 | Processor 构造函数仍接收它们；`command_service` 仍读 planner 统计；异常名仍为 `PlannerInterruptedError`，结果 reason 仍为 `planner_interrupted` | 主路径已不调用 `plan()`。物理删除归 R5 | R5 |
| L5 | §7 `ReplySequenceSpec` 至少含 layout、hard max、pacing、reply target、effect placement | `ReplySequenceSpec` 只有 `max_messages` / `split_hint` / `suppress_text`。quote 在 `ReplyTargetControl`，effect 在 `ReplyControlState` | 所有权已离开 Planner；不把 ChatService 的发送账本再塞回一个巨型 spec | 锁定 |
| L6 | §10 按所有权拆分 `chat.py` | `ChatService` 仍是生产编排中心；`host.py` 只是薄转发 | 同 L1 | 锁定 |
| L7 | §6.2 Runtime 检查自发表情「最近发送比例」 | `send_emoji` 任务书 schema 无 `request_basis`。主路径只做 feature/可用性门；频率门只给有 basis 的 `send_voice` | 不发明短语分类器，也不用 `mode` 去猜显式/自发（会误伤 `emoji_only`） | 锁定；若产品要频率门，须先补 schema 或独立产品决策 |
| L8 | §2.2 需要抽象时命名为 `ConversationBatcher` | 仍叫 `AutonomousGroupService`，方法名仍有 `_plan_latest` | debounce/coalescing 行为已对齐；改名无行为收益 | R5 可改名 |
| L9 | cadence hash 函数随 Conversation Runtime 改名 | `conversation/cadence.py` 内联同一 payload（`yuki-planner-v1\0{kind}\0{value}`），不 import `planner` | 算法不得改，否则 0039 回填与新行断开 | R5 可改函数名，不得改算法 |

---

## 2. 推迟到 R5 的残留（代码里还在，主路径已停用）

任务书 R4 退出条件是「主路径不再调用 Planner」。物理清除是 R5。下列残留**不是**主聊天路径上的 Planner LLM。

| ID | 残留 | 位置 | 说明 |
|---|---|---|---|
| R1 | `src/qq_ai_bot/planner/` 整包仍在 | 包、DB 模型、`planner_runs`、假 Provider | R5 删除；`planner/necessity.py` 已是 re-export |
| R2 | 启动仍构造 `LLMPlannerProvider` / `PlannerService` | `application/modules/conversation.py`、`container.py` | 构造不等于 `plan()`；主路径与 autonomous / background 已停调 |
| R3 | `conftest.build_harness` 仍 new 一个空转 `FakePlannerProvider` | `tests/conftest.py` | 只为 Processor 构造函数接线。普通聊天测试不得再用它驱动行为 |
| R4 | `tests/unit/test_planner_core.py` 仍测 leftover Planner | 单测 | R5 随包删除或改挂 Conversation Runtime |
| R5 | Coordinator stage 字面量仍含 `"planner"` | `services/turn_coordinator.py` | 主路径已 `track(..., "generation")`，不再 `track(..., "planner")` |
| R6 | `chat.py` 日志 hash 仍 import `planner.observability.identifier_hash` | `services/chat.py` | 仅观测哈希；cadence 已用 conversation 内联算法 |
| R7 | 配置 / Settings 双读旧键 | `conversation.*`←`planner.*`，`reply.hard_max_messages`←`plan_hard_max_messages`，`speech.agent_effects_enabled`←`planner_enabled` | snapshot 必须双读到 R5 `migrate-3-6` 删旧 override |
| R8 | `speech.service.planner_context()` 仍读 `runtime.planner_enabled` | `speech/service.py` | 与 `agent_effects_enabled` 是同一 snapshot 字段；仅 leftover Planner 上下文用 |
| R9 | `VoicePreferenceService.apply(VoiceReplyPlan)` 仍在 | `speech/preference_service.py` | 生产写路径已是 `set_voice_preference` → `set_persistent()`。Processor 不再 `apply()` |
| R10 | `PlannerContextBuilder.build()` 仍调用 `EmojiRequestDetector`、仍读 `planner_runs.voice_cadence` | `planner/context.py` | 生产只走 `admission_features()`。`build()` 是 leftover Planner 入参 |
| R11 | 未使用的 `PluginPlannerSignalAdapter` | `plugin_host/planner_adapter.py` | 生产用的是 `PluginAdmissionSignalAdapter`（已投影 SDK `AdmissionSignal`） |
| R12 | `ToolRuntime.voice_tool_authorized` 死字段 | `services/agent_tools.py` | 曝光只看 speech feature；不再被 Planner `REQUIRED` 赋值 |
| R13 | Plugin background 构造函数仍接收 `planner` / `planner_context` 并 `del` | `plugin_host/background_turns.py` | 接线兼容，不调用 |
| R14 | `/ai` 状态仍展示 planner 统计 | `services/command_service.py` | R5 逐处清理 health/admin/status，见 `05-R5` §6 |
| R15 | 观测基线仍扫 `planner_runs` | `observability/runtime_baseline.py` | R5 改口径；生产 cadence 已不双读旧表 |
| R16 | `ProcessResult.reason=planner_interrupted` | `services/processor.py` | 语义是 turn 被取消/抢占，不是又跑了一次 Planner |
| R17 | 配置 catalog / `.env.example` 仍并列旧键 | `admin/config_specs_*.py`、`config.py` | 与 R7 同一双读合同 |
| R18 | `conversation/admission.py` 注释仍写「R4 才迁移生产 policy」 | 过期注释 | 生产已用 `evaluate_message`；R5 顺手改注释即可 |
| R19 | 用户帮助仍写「不再覆盖 Planner 已选择的…」 | `docs/help.md` | 产品版本仍 3.5.3；3.6.0 发版时改帮助与 changelog |

---

## 3. 任务书退出条件里尚未执行的发布门

这些不是实现漏做，是 R4/R5 发布核验。当前分支**没有**用脱敏回放集跑过。

| ID | 任务书 | 现状 | 归属 |
|---|---|---|---|
| G1 | §14 / 总纲：普通文本 P50 −35%、P95 −20% | 未跑成对回放 | R5 发布门。07-PLAN §6.1：若 Planner 只占端到端较小比例，门槛可能数学不可达，须先走门槛复核程序 |
| G2 | §14：平均 foreground model request/回复轮显著下降 | 未跑 | 同 G1 |
| G3 | §13.1 autonomous 高分 reply/decline 的 precision、false-intervention、recall、response-rate delta | 有单测形状，无 600 条回放达标证明 | 发布门 |
| G4 | §13.2 / 07-PLAN §5：回放集应单列 emoji-only（新路径 1 次更重的 Main Agent） | 600 条分配表未单列 | 发布语料 |
| G5 | 总纲 Capability Search Recall@K、`request_tools` 使用率 | R3 质量门，R4 保持 | 发布门 |
| G6 | §16 建议 11 笔提交切分 | 主路径一次落地（`884e9c3`），审计修正另提交 | 不重写已推送历史 |
| G7 | §12 / findings §9：行为变化进正式 release note | findings 已有草稿；`CHANGELOG` / 3.6.0 发布说明未写 | R5 发版时从 findings §9 抄出 |

---

## 4. 测试矩阵中本轮未单独立项、但不阻塞主路径切换的项

§13 核心回归（私聊/@/reply-to-bot、silent、decline、黄金 1 次 Agent、cadence 分母、emoji-only、quote retry、0039）已有单测。下列是任务书列出、但没有做成独立回放/精度门的部分：

| ID | 项 | 说明 |
|---|---|---|
| T1 | AdmissionSignal「每插件有限幅度、过期失效」的独立 R4 文件 | scorer 与 adapter 已实现并有 `test_planner_core` / `test_plugin_admission_adapter`；无单独 conversation 文件名 |
| T2 | observation 不打断 protected direct turn 的 R4 新文件 | Coordinator 既有保护逻辑；未在 `test_conversation_runtime_r4.py` 再写一条 |
| T3 | native web failure → Tavily 的 R4 新文件 | 既有 web 集成测试，不是本次切换新写 |
| T4 | Chat Completions / Responses stable-prefix frozen snapshot | R3/编译器合同，R4 未重跑冻结快照 |
| T5 | `tests/unit/test_planner_core.py` 文件名仍叫 planner | 内容已含 R4 scorer 回归；R5 再改名 |

---

## 5. 07-PLAN 保留意见（流程，不是漏实现）

`07-PLAN-REVIEW-REPORT.md` §6 明确「不修改任务书」。R4 也没有单独落地这些流程：

1. 性能门槛不可达时的批准程序（谁改门槛、要什么证据）。
2. feature branch 与 `main` 的定期 rebase 节奏。
3. R3 搜索质量中期检查点。
4. 把 cached tokens / 缓存命中率变化当成 R3/R4 回放一级指标。
5. 「运行时 : 测试 ≈ 1 : 1」的排期估算法。

---

## 6. 明确不是偏差、避免回头再改

- Direct 轮次不评分、自主群默认禁写、background 不再 SILENT：这是**已落地的产品变化**，见 findings §9，不是未改项。
- `evaluate_message` 没有独立类名、`handle_turn` 而不是 `begin_turn`：见 §1 锁定项。
- 主路径 0 Planner LLM：已满足。不要为了「删干净 import」在 R4 再拆 Processor 构造函数。
- 不要把 `EmojiRequestDetector` 接到 `ChatService.respond`。
- 不要恢复 `_plan_turn()` / `planned_turn=`。
- 不要删除 `src/qq_ai_bot/planner/`（R5）。
- 不要 force-push 把 `884e9c3` 拆成 11 笔提交。
