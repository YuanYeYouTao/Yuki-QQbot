# R5 清除清单（r5-purge-inventory）

> 日期：2026-08-18  
> 对照：`05-R5-PLANNER-PURGE-AND-RELEASE.md` §1  
> 代码基线：分支 `codex/refactor-3.6-runtime`（R4 主路径已切换；产品版本开工时仍为 `3.5.3`；Alembic head `0039`）  
> 扫描命令见任务书 §1。历史 Alembic 文件不改。运行时零引用用 AST/import gate 与本 allowlist 验证。

分类每个 `Planner|planner` 命中。归属列对应任务书建议提交顺序。

---

## 1. must delete

| 位置 | 说明 | 归属提交 |
|---|---|---|
| `src/qq_ai_bot/planner/` 整包 | `__init__` / `context` / `db_models` / `fake` / `models` / `necessity` / `observability` / `prompt` / `provider` / `repository` / `service` | 1 |
| `application/modules/conversation.py` 构造 `LLMPlannerProvider` / `PlannerService` / `PlannerObservability` | 主路径已不调用 `plan()` | 1 |
| `container.py` `planner` / `planner_provider` / `planner_observability` / `planner_runs` 接线 | 同上 | 1 |
| `persistence` 的 `PlannerRepository` / metadata 注册 `PlannerRunModel` | 表留到 0040；ORM 与仓库随包删除 | 1 |
| `plugin_host/background_turns.py` 已 `del` 的 `planner` / `planner_context` 形参 | 接线兼容 | 1 |
| `speech/service.py` `planner_context()` / `PlannerSpeechContext` | 仅 leftover `PlannerContextBuilder.build()` | 1 |
| `services/chat.py` `planner_tool_scopes()` 空桩 | R3 已退役 | 1 |
| `services/agent_tools.py` `voice_tool_authorized` / `planner_fallback` 死字段 | R4 曝光只看 speech feature | 1 |
| `cli.py` `PLANNER_SYSTEM_PROMPT` / autonomous-group 走 Planner 诊断 | 自主群已是 Main Agent | 1 |
| `/healthz` `planner_configured` / `planner_active_requests` | §8 | 1 |
| `/ai` 状态里的 Planner 模型/延迟/决策时间 | §6 | 1 |
| `tests/unit/test_planner_core.py` leftover Planner LLM / schema / fake provider 用例 | 评分回归迁到 conversation 测试 | 1 |
| `conftest.build_harness` 空转 `FakePlannerProvider` | Processor 不再接收 Planner | 1 |
| `ModelTask.PLANNER` 与 profile route / `_DEFAULT_REQUIREMENTS` | R3 已删 `TOOL_SELECTION`；本轮删 `PLANNER` | 2 |
| Guided Setup / `deployment_setup` 的 planner 路由 | 同上 | 2 |
| `scripts/release_smoke.py` `_MODEL_TASKS` 中的 `planner` | 同上 | 2 |
| Planner 专属 Settings / admin specs / `.env.example` 键（temperature、timeout、record_runs 等） | 先备份再删，不映射 | 4 |
| `planner_runs` 表与索引 | Alembic `0040` | 4 |
| SDK `EventName.PLANNER_*` 与生产发射点 | 替换为 runtime 事件 | 5 |
| `PromptStage.PLANNER_PLAN` / `PromptTarget.PLANNER` | Host 已跳过插件 `planner_plan`/`planner`/`both` | 3 |
| 产品文档 `docs/plugin-development/planner-signals.md` 等 1.1 文案 | 独立 API 2.0 迁移文档 | 3 / 6 |

`TOOL_SELECTION`、Flash reranker、Planner tool scope 已在 R3 删除。本清单只验证零残留，不再实现同一删除。

---

## 2. must rename and move

| 旧对象 | 新位置 | 归属 |
|---|---|---|
| `PlannerContextBuilder.admission_features` + `_metrics` | `conversation/features.py` `AdmissionFeatureBuilder` | 1 |
| `planner.observability.identifier_hash`（sha256 hex[:16]） | `runtime/observability.py` `identifier_hash` | 1 |
| `hash_planner_identifier` / cadence `_stable_identifier_hash` | `runtime/observability.py` `stable_identifier_hash`；**payload 不得改**（`yuki-planner-v1\0{kind}\0{value}`） | 1 |
| Processor `planner_signals=` | `admission_signals=` | 1 |
| `AutonomousGroupService._plan_latest*` | `_run_latest` / `_admit_latest` | 1 |
| `ProcessResult.reason=planner_interrupted` | `turn_interrupted`（语义仍是抢占，不是又跑了 Planner） | 1 |
| Coordinator `TurnStage="planner"` / `PlannerInterruptedError` | `"admission"` / `TurnInterruptedError` | 1 |
| `conversation.*` ← `planner.*` 配置键 | `migrate-3-6` 物化后删除旧 override | 4 |
| `reply.plan_hard_max_messages` → `reply.hard_max_messages` | 同上 | 4 |
| `speech.planner_enabled` → `speech.agent_effects_enabled` | 同上 | 4 |
| `memory_attribution` 隐式 `utility_structured → planner` 回退 | 删除 planner route **前**写成显式 route | 2 |
| 历史 `model_invocations.task=planner/tool_selection` | Repository 读取容忍 retired string；写入只接受现役 `ModelTask` | 2 |
| Plugin docs `PlannerSignal` → `AdmissionSignal` | API 2.0 迁移文档 | 3 |
| `PLANNER_NECESSITY_EVALUATED` 等 | `TURN_ADMITTED` / `TURN_REJECTED` / `AUTONOMOUS_DECLINED` / `CAPABILITY_SEARCHED` / `TURN_CLOSED` | 5 |

锁定不改：R1 `AdmissionDecision` 名称、Protocol `run_agent`、`handle_turn` 薄转发、cadence 哈希算法、自主群默认禁写。

---

## 3. historical documentation only

允许继续含 `planner` 文本，不要求零匹配：

- `migrations/versions/0013_*` / `0017_*` / `0037_*` / `0038_*` / `0039_*`（不修改历史文件）
- `tests/unit/test_migration_0013.py` / `0017` / `0037` / `0038` / `0039` 对旧表的升级断言
- `docs/architecture/yuki-3.6.0-refactor-plan/**` 任务书与 findings
- `CHANGELOG.md` 历史条目
- 3.6.0 release note / 迁移文档中的「已删除 Planner」陈述
- `source=migrated_planner` 作为 0039 回填枚举值（数据域，不是运行时包）
- 哈希盐 `yuki-planner-v1`（load-bearing；改了会断开 0039 与新 cadence）

---

## 4. false positive / 保留到后续提交的运行时字符串

| 命中 | 分类 |
|---|---|
| `plugin_host/prompt_adapter.py` `_DELETED_PLUGIN_STAGES={"planner_plan"}` | 拒绝旧插件字段；提交 3 可改名但语义保留 |
| `emoji/models.py` `source: Literal["planner", ...]` | 历史效果来源枚举；提交 5 可冻结为 archived |
| `observability/runtime_baseline.py` 读 `planner_runs` | 3.5.3 基线导出器；0040 前必须能读旧表 |
| `admin/config_specs_planner_plugins.py` 文件名与 `planner.*` 键 | 提交 4 删除/改名 |
| `config.py` / `settings_domains.py` dual-read | 提交 4 `migrate-3-6` 后删除 |
| `ModelTask.PLANNER` | 提交 2 |
| SDK `EventName.PLANNER_*` | 提交 5 |
| `capabilities/selection.py` `planner_intent=""` 形参 | 死参数；提交 1 删除以免生产 `src` 继续出现该名 |
| `MCP_TOOL_SELECTION_MODE` | 提交 4 备份后删除，不映射 |

R3 已完成：`PluginPermission.ADMISSION_SIGNAL_REGISTER`、`register_admission_signal`、内置插件 API 2.0、0038 撤销旧 approval。提交 3 做最终验收与文档，不重做 API 迁移。

---

## 5. 运行时零引用 allowlist（§9.3）

生产 `src/qq_ai_bot` + `src/yuki_plugin_sdk` 在 R5 结束后：

- **禁止**：`import qq_ai_bot.planner`、`ModelTask.PLANNER` / `TOOL_SELECTION` 作为现役写入、`register_planner_signal`、`PlannerRunModel` ORM。
- **允许**（显式）：
  1. 哈希盐字符串 `yuki-planner-v1`
  2. cadence/回填 `source=migrated_planner`
  3. 基线导出器对 **可选** `planner_runs` 表的 sqlite 读取（表不存在则记 gap，不 fail-fast 于 3.6 DB）
  4. 配置迁移器识别旧键名以便删除
  5. Alembic `0040` 的 `DROP TABLE planner_runs`

`tests/unit/test_runtime_dependency_boundaries.py` 的 `ALLOWED_LEGACY_IMPORTS` 开工时已为空，R5 保持为空，并增加「`planner/` 目录不存在」断言。
