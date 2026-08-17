# R5：物理删除 Planner，并发布 Yuki 3.6.0

> 目标：删除旧 Planner 的代码、数据表、配置、模型路由、观测、插件 API 和测试。  
> 本轮不是清理可做可不做的遗留物，而是 3.6.0 架构成立的必要条件。
> 已按代码核对：`main@2695484`（审阅基线 Alembic Head `0036`、Plugin API `1.1`；计划 Head：R1=`0037`、R3=`0038`、R4=`0039`、R5=`0040`）。

> **代码审阅批注**：R5 只能清除 R1-R4 已完成所有权转移的遗留物，不能重复承担 R3 的 `TOOL_SELECTION`/Plugin API 迁移。旧 `model_profiles.toml`、`planner_runs` 语音 cadence、历史 `model_invocations.task` 和安装器自动 Alembic 升级都是发布阻断，必须先迁移再删除。

## 1. 开工前 Codex 全库审阅

执行：

```bash
rg -n "Planner|planner" src tests migrations docs README.md .env.example pyproject.toml
rg -n "PLANNER|planner_" src tests migrations docs README.md .env.example
rg -n "ToolGroup|ToolSelection|ToolMode" src tests
rg -n "ModelTask\.PLANNER|ModelTask\.TOOL_SELECTION" src tests
rg -n "planner_runs|PlannerRunModel" src tests migrations
rg -n "register_planner_signal|PlannerSignal" src tests docs
```

分类每个结果：

```text
must delete
must rename and move
historical documentation only
false positive
```

输出 `docs/architecture/yuki-3.6.0-refactor-plan/reviews/r5-purge-inventory.md`。

历史 migration、3.6 release note 和迁移测试允许包含 `planner` 文本，不能修改历史 Alembic 文件。运行时零引用用 AST/import gate 与显式 allowlist 验证，宽泛 `rg migrations` 只用于分类清单。

---

## 2. 删除代码目录

删除整个：

```text
src/qq_ai_bot/planner/
  __init__.py
  context.py
  db_models.py
  fake.py
  models.py
  necessity.py
  observability.py
  prompt.py
  provider.py
  repository.py
  service.py
```

有价值逻辑必须在 R1-R4 已重新定义到正确位置：

| 旧对象 | 新位置 |
|---|---|
| ReplyNecessityScorer | `AutonomousParticipationPolicy` |
| ToolScopeSummary | capabilities/namespace.py |
| MemoryContextPlan 的语义部分 | memory/models.py 或 memory/runtime contract |
| identifier_hash 等通用观测 | runtime/observability.py |
| delivery fields | conversation/delivery.py |
| voice/emoji context | 各自 Runtime |

不得用 re-export 保留旧 import path。

---

## 3. 删除模型任务与路由

从 `ModelTask` 删除：

```text
PLANNER
TOOL_SELECTION
```

`TOOL_SELECTION`、Flash reranker、Planner tool scope 在 R3 所有权切换提交中已经删除；R5 只验证零残留，不得再次实现同一删除。`PLANNER` 在 R4 主路径切换稳定后由 R5 删除。

删除：

- Planner profile route。
- Tool selection profile route。
- Planner temperature/max token/timeout 配置。
- Planner structured output 配置。
- Planner provider client tests。
- Flash Tool Reranker tests。

检查：

```text
model routes
runtime config registry
.env.example
admin config schema
CLI config output
deployment guided setup
Docker release assets
```

在删除枚举前交付 `model_profiles` schema v3 与一次性 `qq-ai-bot-cli setup migrate-3-6`：

1. 安装器先拉取精确的 3.6.0 Bot 镜像但不启动服务，再在 `compose up` 和 Bot 自动 `init-db` 之前，以可写 deployment root 运行迁移容器。
2. 备份 `.env`、`config/model_profiles.toml`、`.mcp.json`，用临时文件 + 原子替换。
3. 仅删除已知 `[routes] planner/tool_selection`，保留 profiles 和其他 routes；删除前必须先物化隐式回退：当前 `ModelProfileCatalog` 在缺少 `memory_attribution` route 时按 `utility_structured -> planner` 顺序回退（`tool_selection` 缺失时也回退 `planner`），迁移器要在删除 planner route 前，把仍依赖该回退的 `memory_attribution` 显式写为原 planner profile，否则升级后 attribution 静默失去路由。Guided Setup 按新 `ModelTask` 重建显式路由。
4. 未迁移的旧 schema 启动时 fail-fast，并输出可执行命令，不只报 `invalid model profile configuration`。

> **批注原因**：当前 `ModelProfileCatalog` 对每个 route 执行 `ModelTask(task_name)`；先删枚举会让真实 3.5.3 配置在 Alembic 之前直接启动失败。生产 Compose 的 `./config` 还是只读挂载，不能指望 Bot 容器启动后自修复。

历史 `model_invocations.task=planner/tool_selection` 不删除。Repository 的读取投影改为容忍 retired task string，写入端仍只接受现役 `ModelTask`；否则删除 enum 后历史 stats/近期错误查询会崩溃。

---

## 4. 数据库迁移

若开工时基线 Head 仍为 `0036`：R1 `0037` 建立 turn correlation，R3 `0038` 迁移 Plugin approvals，R4 `0039` 创建/回填 `reply_effect_events`，R5 `0040` 删除 Planner persistence 并迁移 overrides。若 Head 已变化则整体顺延并在 findings 记录，不修改历史 migration。

### 4.1 删除

```text
planner_runs table
planner_runs indexes
planner-specific config rows
```

数据库中不存在 planner-specific model route rows；模型路由由上节 TOML migrator 处理。`model_invocations` 历史成本记录保留。

### 4.2 新增或替换

R4 已先新增并验证：

```text
reply_effect_events
```

`0039` 已为每个 conversation 回填旧 `voice_cadence()` 口径中最近 20 条 `planner_decision=reply AND voice_intent=neutral` 行，每个旧 Planner run 生成一条 `source=migrated_planner` 事件；`source_event_hash` 由固定 salt domain + planner run id 产生，支持 upgrade 重试幂等。所有回填行 `voice_cadence_eligible=true`，有语音的行记为 `voice_request_basis=agent_initiated`；`voice_sent` 按旧 cadence 的 `{voice,text_and_voice,optional}` 口径映射，`text_sent` 仅对 `{text,text_and_voice}` 为 true。保留原 `created_at` 为 occurred_at，以迁移时间为 recorded_at，使它们至少保留 90 天并逐步被真实 confirmed-delivery 事件替代。R4 已比较新旧 cadence 结果且 Runtime 只读新表后，`0040` 才删除 `planner_runs`。

`runtime_turns/capability_search_events` 默认不新增，优先复用 content-free metrics/model/tool observations；确需持久化时先写 retention、批量 cleanup、索引和 cardinality 上限。

任何新增观测只保存：

- hash
- origin/scope
- admission result
- local search latency
- selected Tool count
- schema tokens
- request_tools count
- model/tool call count
- completion reason

不得保存聊天正文、Tool arguments、Memory content。

### 4.3 升级规则

- 升级前必须备份 SQLite。
- 迁移后不支持 3.5.3 继续写同一数据库。
- 不保留 planner_runs 归档表；迁移前必须成功运行 R1 的 `scripts/refactor_3_6/export_runtime_baseline.py --output <git外路径>/baseline-v1.json`，通过 schema/commit/sample-window 校验后才允许 purge。导出失败或目标位于 Release bundle/仓库跟踪区时拒绝继续。
- 回退只允许：停止 3.6 -> 恢复数据库、WAL/SHM 与配置的同一套快照 -> 启动 3.5.3。Alembic downgrade 不得伪装能恢复已删除 Planner 数据，可显式拒绝或只重建空 schema。

安装/升级脚本必须在新镜像第一次自动 `init-db` 之前创建 SQLite 一致性备份（处理 WAL/SHM）；备份失败立即停止升级。现有 `start.sh` 会先自动升级数据库，单写“请用户备份”不足以满足门禁。

正式升级顺序锁定为：

```text
verify 3.6.0 release assets/checksums
-> pull exact 3.6.0 Bot image without starting it
-> stop old Bot container and verify the DB writer has exited
-> snapshot .env/config/.mcp.json + data database/WAL/SHM to one timestamped directory
-> fsync/close archive, write checksum manifest, verify it can be opened
-> run setup migrate-3-6 with deployment root mounted read-write
-> docker compose config
-> docker compose up -d (new image now runs 0037-0040)
-> health/source-free upgrade assertions
```

快照存入部署根 `.yuki/backups/upgrade-3.6/<timestamp>/`，不进入 Release bundle/Git；包含恢复所需的 Compose 版本、镜像 digest 和脱敏 manifest。任何一步失败均不删除快照。数据库迁移开始前失败可恢复配置并重启旧容器；迁移开始后只能停 3.6、恢复同一套配置+数据库快照、再启 3.5.3。

---

## 5. Plugin API 2.0 最终清理

删除：

```text
PlannerSignal
PlannerSignalContext
PlannerSignalRegistration
register_planner_signal
```

确认：

```text
ToolMetadata.namespace required
ToolMetadata.aliases
ToolMetadata.use_when
ToolMetadata.tags
register_admission_signal
```

Plugin Host 需要验证：

- Namespace 格式。
- Tool name 冲突。
- Alias 长度和数量。
- Schema Token 估算。
- Provider/Trust 不由插件任意声明。
- AdmissionSignal 不能修改权限或 Tool 排序。

R3 已完成 API 2.0 代码与生态迁移，R5 只做最终验收：

- `PluginPermission.PLANNER_SIGNAL_REGISTER` -> `ADMISSION_SIGNAL_REGISTER`。
- `ExtensionKind.PLANNER_SIGNAL`、registrar handler、Prompt `PLANNER_PLAN`/`PLANNER|BOTH` target、exports/adapters/events 全部迁移或删除。
- 三个内置插件、example、manifest、SDK docs/frozen snapshots 全部声明 2.0；API major 在 import 插件代码前校验。
- 旧 `planner.signal.register` approval 被 sanitize/revoke，metadata/API/permission/hash 变化后的插件进入 `pending_approval`，不得静默沿用。
- Source-free smoke 必须实际开启 Plugin、运行 discovery 和 apply-pending，不能用默认关闭掩盖内置插件不可用。

发布独立 Plugin API 2.0 迁移文档。

---

## 6. 配置与 CLI 清理

删除 Planner **模型专属**配置项：

```text
planner_enabled
planner_model/profile
planner_temperature
planner_max_output_tokens
planner_timeout_seconds
planner_max_wait_seconds
planner structured-output / tool-selection mode
```

按 scope 原值迁移真正属于 Runtime 的配置（同 scope 已有新键时保留新键并删除旧键）：

```text
planner.group_enabled                         -> conversation.autonomous_enabled
planner.group_debounce_seconds                -> conversation.autonomous_debounce_seconds
planner.reply_necessity_threshold             -> conversation.autonomous_admission_threshold
planner.max_pending_messages                  -> conversation.autonomous_batch_limit
planner.recent_presence_window_seconds        -> conversation.autonomous_presence_window_seconds
planner.interrupt_autonomous_on_new_message   -> conversation.interrupt_autonomous_on_new_message
reply.plan_hard_max_messages                  -> reply.hard_max_messages
speech.planner_enabled                        -> speech.agent_effects_enabled（合法布尔值原样复制）
```

`MCP_TOOL_SELECTION_MODE` 及其 Planner/Tool Selection 同义环境键全部备份后删除，不映射到新配置；Capability Runtime 只有唯一 local-search 模式。更新 `RuntimeConfigSnapshot`、Admin specs/overrides、CLI、health、`.env.example`、Guided Setup 和 Release bundle。配置迁移是部署前独立步骤，不保留 Planner ignored-warning 运行时兼容。

CLI：

- 清理命令面中的 Planner 输出：现状无独立 planner 子命令，Planner 状态分散在 `/ai` 状态文本、`model stats` 任务行与 `/healthz` 字段中，逐处删除并更新对应快照测试。
- 新增 Runtime、Capability Search、Memory Session 的只读诊断。
- 输出不得泄漏用户内容。

---

## 7. Event 与 Observability 清理

删除或迁移真实 Planner 专属 EventName：

```text
PLANNER_NECESSITY_EVALUATED
PLANNER_ENTERED
PLANNER_PLANNED
PLANNER_SILENT
PLANNER_INTERRUPTED
PLANNER_FALLBACK
```

替换为：

```text
TURN_ADMITTED / TURN_REJECTED
AUTONOMOUS_DECLINED
CAPABILITY_SEARCHED
TURN_CLOSED（含 outcome）
```

现有 `AGENT_STARTING/FINISHED/INTERRUPTED` 与 `REPLY_SENT/FAILED/CANCELLED` 继续复用，不新增同义 `AGENT_STARTED/COMPLETED/TURN_DELIVERED`。Plugin API 2.0 发布 event 映射表；观测事件不含正文、Tool arguments 或 Memory refs。

Memory 事件保持领域名称，不挂在 Planner 下。

---

## 8. 文档与发布说明

3.6.0 发布说明必须明确：

- 强制 Planner 已删除。
- 普通聊天删除一次前置 Planner 生成式请求（Vision/Embedding 口径另列）。
- Memory 变为独立 Runtime Session。
- Tool Scope 改为 Semantic Namespace。
- Tool Search 改成本地 FTS5 BM25。
- Flash Tool Selection 模型已删除。
- Plugin API 2.0 是破坏性升级。
- 数据库迁移单向。
- 3.5.3 Plugin 需要修改 metadata/signal API 并重新批准。
- 性能回放结果。
- Memory 和 Tool 安全回归结果。

版本更新：

```text
pyproject.toml -> 3.6.0
package/version constants -> 3.6.0
Docker tags -> 3.6.0
release assets -> 3.6.0
```

版本面清单还包括：`uv.lock`、runtime `__version__`、Alembic Head、Plugin API、`scripts/release_validate.py`、`scripts/release_smoke.py`、Memory Release Check（Memory Query=6、Plugin Memory Facade=1.0）、migration matrix、Guided Setup、内置 manifests、README/help/changelog/release note 和 OCI labels。尽量由单一 release contract 读取 version/head/API，减少散落魔数。

`/healthz` 删除 Planner 字段并迁移 response schema、帮助文档与 source-free smoke；不得在已删除 `RuntimeConfigSnapshot.planner` 上继续解引用。

---

## 9. 最终测试

### 9.1 静态检查

```bash
ruff format --check .
ruff check .
mypy src
```

### 9.2 全量测试

```bash
python -m pytest -q
python -m qq_ai_bot.memory.quality.release_check
```

同步更新并实际执行 Quality/Release workflow 中的 Ruff、Mypy、全量 Pytest、Memory Release Check、migration matrix、Plugin contract、Bot/Worker image 与 source-free upgrade smoke；不能只改脚本常量而漏掉 workflow 调用。

### 9.3 架构审计

```bash
rg -n "Planner|planner" src/qq_ai_bot src/yuki_plugin_sdk
rg -n "ModelTask\.PLANNER|ModelTask\.TOOL_SELECTION" src tests
rg -n "planner_runs|register_planner_signal" src tests migrations
```

生产 `src` 的 Planner import/active schema 结果必须为 0；历史 migrations、迁移测试和 release note 使用显式 allowlist，不要求文本零匹配。

### 9.4 关键回归

- Main Agent normal no-tool。
- Main Agent with Tool。
- request_tools dynamic load。
- Responses continuation。
- native web + Tavily recovery。
- Memory passive/read/write/forbidden。
- memory mutation receipt finalization。
- plugin/MCP/admin/automation。
- autonomous group。
- voice/emoji/reply target。
- delivery receipt + ledger。
- cancellation/supersede。

### 9.5 数据库升级测试

- 从真实结构的 3.5.3 DB 副本升级。
- 从真实 3.5.3 source-free deployment bundle 升级：旧 `.env`、schema v2 model profile、0036 DB、planner runs、历史 model task、runtime override 和 plugin approvals 全部在场。
- Integrity check。
- Planner 表删除。
- Memory 表数据不变。
- Tool/Plugin 配置迁移正确。
- 启动、运行、关闭、再次启动正常。
- Head 为 0040（若审阅基线 head 未变化），配置备份/原子重写、0037 turn correlation、0038 approvals、0039 cadence backfill、Plugin pending approval、健康输出和持久目录正确。
- 备份失败不得启动迁移；回退演练按整套快照恢复 3.5.3。

---

## 10. 性能与成本最终验收

必须发布一份 `docs/performance/3.6.0-runtime-report.md`：

```text
old vs new
P50/P95 end-to-end latency
model calls per turn
planner calls removed
prompt/completion/cached tokens
initial tool count
schema tokens
capability search latency
first-round tool hit
request_tools rate
memory pure retrieval / end-to-end prepare latency
mutation latency
```

发布门槛使用总纲定义。

额外要求：

- 常见普通消息的本地 Runtime 前置 P95 增量不得超过 100 ms；该口径包含 authority/resolver、Capability Search、SQLite/FTS retrieval、budget、exposure/Receipt bookkeeping，不包含远程 Vision、Embedding 或生成式模型网络时间。
- Capability Search 不允许在请求热路径重建索引。
- Memory Prefetch 与其他独立本地准备工作可并发时，应使用结构化并发，但不能牺牲数据库一致性。

性能报告固定 provider/profile、地区、并发、硬件、SQLite/FTS 版本、语料 SHA、样本量、warmup、冷/热 cache、失败处理和 percentile/置信区间算法。远程模型百分位使用成对 canary/replay，不作为波动性单点 CI 断言。Capability Search 的 25 ms 只测 warm index，冷重建另报；Vision/Embedding 单列，DB 作为本地 Runtime 的组成部分同时给出子项分解，不能从 100 ms 总口径中扣除。

---

## 11. 真实环境验证

至少执行：

1. 私聊普通问答。
2. 私聊 automatic memory。
3. 显式人物记忆读取。
4. SELF memory 读取。
5. 记忆创建、纠正、撤回、恢复。
6. 不存在目标和歧义目标。
7. Web search/read。
8. Automation create/list/delete。
9. Plugin Tool。
10. MCP Tool 与 lazy metadata。
11. 群聊 mention、reply、silent、自主参与。
12. 图片轮次写隔离。
13. Voice、Emoji。
14. Agent 中断与新消息 supersede。
15. 已提交 mutation 后模型 finalization 失败的恢复文本。

数据库状态、receipt、Ledger 和用户可见结果必须一致。

---

## 12. 本轮禁止事项

- 不留下空 Planner package。
- 不留下旧 import alias。
- 不留下 planner_runs 只读兼容。
- 不留下 Planner config ignored warning。
- 不保留旧 Plugin API 运行时适配。
- 不把旧 Planner 改名成 Runtime Planner。
- 不在 3.6.0 默认加入 planning specialist。

未来确实需要复杂任务规划时，新建：

```text
agents/planning_specialist.py
```

它只能由 Main Agent 作为 Tool 按需调用，不能继承旧 Planner，也不能成为前置节点。

---

## 13. 建议提交顺序

1. `refactor(planner): remove planner package`
2. `refactor(model-runtime): remove planner and tool-selection tasks`
3. `refactor(plugin-sdk): finalize API 2.0`
4. `migration: drop planner persistence and config`
5. `refactor(observability): replace planner events with runtime events`
6. `docs: add 3.6.0 migration and architecture`
7. `test: complete destructive upgrade regression`
8. `perf: publish 3.6.0 runtime benchmark`
9. `release: bump version to 3.6.0`

---

## 14. 3.6.0 发布判定

只有全部满足才可发布：

- Planner 运行时代码为 0。
- 普通纯文本前置 foreground generative/router 模型调用为 0；Vision/Embedding/后台任务单列。
- Memory Runtime 状态机为唯一记忆访问管理者。
- Capability Namespace 为唯一 Tool 搜索组织结构。
- Tool Search 为本地执行。
- Plugin API 2.0 生效。
- 所有性能门槛通过。
- 所有 Memory 质量门通过。
- 所有权限和 mutation 安全测试通过。
- 真实环境验证通过。
- 0037-0040 数据库迁移、部署前配置迁移和整套快照回退演练通过。
- 全量静态检查和测试通过。

R5 结束后，Yuki 3.6.0 的核心路径应为：

```text
Event -> Conversation Runtime -> Memory Runtime -> Capability Runtime
      -> Main Agent -> Tool Kernel -> Delivery
```

旧 Planner 不再存在。
