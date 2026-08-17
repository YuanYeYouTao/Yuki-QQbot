# Yuki 3.6.0 重构任务书合理性评估报告

> 状态：独立核对完成，偏差已回写任务书  
> 核对基线：`main@2695484`（`26954843c26f3849044c61ee945704242c4bde25`，tag `v3.5.3`，Alembic Head `0036`，Plugin API `1.1`）  
> 核对日期：2026-08-17  
> 核对方式：对总纲与 R1-R5、06 决议共 7 份文档提取 65+ 项可验证的代码事实声称，分主路径、Memory、Capability/Plugin、观测/DB/配置/发布四个领域逐项与源码比对；另抽查外部引用（Anthropic Tool Search 官方指引）真实性。

---

## 1. 总体结论

**这套任务书可以作为实施合同使用。** 核心判断：

1. **对现状的描述准确率高。** 65+ 项声称中约 85% 完全属实且文件、行为、甚至报错文本级别吻合；其余为部分属实，多数是措辞精度问题而非方向错误。06 决议中列出的 18 项"原稿假设 vs 真实代码"偏差全部被独立证实，说明文档自称的"已按代码核对"不是套话。
2. **架构方向与代码现状兼容。** 删除前置 Planner、三 Runtime 分权、本地词法工具搜索、Memory 六维正交合同，均在现有代码中找到了对应的钩子与迁移点，不需要重写已稳定的数据面（Mutation Service、Recall Receipt、AgentRunner bounded loop、Responses continuation 等"保留清单"与代码一致）。
3. **本次核查新发现 6 类偏差，已直接修订进任务书**（见第 4 节）。其中 2 项若不修正会造成实施期返工（`set_voice_preference` 误当既有工具、`memory_attribution` 隐式路由回退），其余为口径与定性问题。
4. **剩余风险主要是流程性的**（见第 6 节）：性能门槛不可达时缺少处理程序、feature branch 生命周期未定义、R3 搜索质量门缺中期检查点。这些不影响任务书作为技术合同的成立。

---

## 2. 核对通过的关键声称（按领域摘要）

### 2.1 主路径与 Planner

| 声称 | 结论 | 关键证据 |
|---|---|---|
| `TurnPlan` 同时承载 decision/intent/target/delivery/desired_messages/reply_target/tool_selection/wait/memory_context/emoji/voice | 属实 | `planner/models.py:354-379` |
| `_plan_turn()` 存在 WAIT→sleep→第二次 Planner 请求 | 属实 | `services/processor.py:818-852` |
| Planner 是真实 LLM 请求，失败 fail-closed | 属实 | `planner/provider.py:363-421`；`services/chat.py:1994-2007` |
| `ChatService.respond(planned_turn=...)` 派生 Memory/Tool/Voice/Emoji/Web/Delivery；`_initial_scopes_for_memory_access`/`_automatic_memory_mode`/`_with_memory_mutation_contract`/locator/mutation 状态均在 | 属实 | `chat.py:182-210, 426-428, 1988-2191`（全文件 3206 行） |
| 协调 key 整群 `group:{g}` vs 历史 identity `group:{g}:user:{u}` 三键分裂 | 属实 | `turn_coordinator.py:72-78` vs `domain/conversations.py:48-54` |
| AgentRunner 有界 MODEL↔TOOL 循环 + empty retry/incomplete recovery/web fallback/post-commit recovery/ProviderContinuation | 属实 | `services/agent_runner.py:139-162+` |
| Vision 在 Planner 之前运行（图片轮多一次前置模型） | 属实 | `processor.py:660-668 → 695` |
| `AutonomousGroupService` 仅 debounce/coalescing/revision，Planner 拥有参与决策 | 属实 | `autonomous_groups.py:46-47, 90-156` |
| WAIT/debounce/necessity/speech 等 Planner 配置键存在，R1 §11 迁移映射的旧键全部真实 | 属实 | `config.py:325-335, 461`；`.env.example:270-285, 438` |

### 2.2 Memory

| 声称 | 结论 | 关键证据 |
|---|---|---|
| `MemoryAccessMode(none/automatic/tool/mutation)` 同时耦合注入/读曝光/写事务/终态规则四维 | 属实 | `memory/enums.py:242-248`；`chat.py:182-210, 526-550, 1082-1195` |
| 预算 background 3 / continuation 4 / focused 6 / overview 8，per-target 4 | 属实 | `config.py:183-187`；`memory/context.py:223-293` |
| Receipt `turn_id` 是每 Receipt 唯一 id 而非整轮 id；`candidate_count` 为 source 计数、`selected_count` 为 unique 数 | 属实 | `memory/receipt.py:44-60`；`persistence/models.py:502-538`；`retrieval.py:284-368` |
| Hybrid 检索在 lexical 质量评估前先调 Embedding，无 lexical-first 级联 | 属实 | `memory/retrieval.py:127-183`（R2.1 延期决策的前提成立） |
| Attribution job 单 intent；item reinforced 与 header 重算跨两个事务，存在 crash 不一致窗口 | 属实 | `attribution.py:121-127`；`activation.py:141-217` + `receipt.py:249-287`（R2 §9 `AttributionCommitter` 正确针对此缺口） |
| Mutation 8 操作、Service 独占事务/receipt/幂等锁/commit 后 embedding、Dream 外部 session | 属实 | `mutation/models.py:15-23`；`mutation/service.py:210-223, 1007-1107, 2412-2419` |
| Worker 按群分区（`group:{id}`）、隐式 SELF episode ≤1 条、reply excerpt ≤500 字、`retrieval_enabled=false` overview fallback | 属实 | `processor.py:558-564`；`context.py:234-270, 349-378`；`query.py:94-99` |
| Memory Quality 18 cases / 38 gates、`memory_query_schema=5`、`plugin_memory_facade=1.0` | 属实 | `tests/fixtures/memory_quality/v1/manifest.toml`；`config/memory_quality_gates.toml`；`tests/contracts/memory_v2/contracts.json:119-124` |

### 2.3 Capability / MCP / Plugin

| 声称 | 结论 | 关键证据 |
|---|---|---|
| Planner Scope 驱动曝光：`_prepare_tool_candidates()` explicit/inherited 分支、`ToolMode/ToolGroup/selected_tool_names` | 属实 | `chat.py:2918-3019`；`agent_tools.py:116-120` |
| `FlashToolReranker` 在 hybrid 路径使用 `ModelTask.TOOL_SELECTION` | 属实 | `capabilities/selection.py:106-142`；`chat.py:3000-3009` |
| `request_tools` 现状是 CJK 2/3/4-gram 线性打分，无 FTS5/持久索引 | 属实 | `capabilities/request.py:13, 56-108` |
| Catalog 逐 context 现算、无跨 turn revision 缓存；revision 仅 `provider:model_name:schema_version` | 属实 | `catalog.py:122-183`（R3 两层拆分的动机成立） |
| 未知 Core Tool fail-open 为 `("memory", READ_STATE, READ)` | 属实 | `capabilities/provider.py:169-173`（R3 要求 fail closed 正确） |
| MCP `prepare_scopes()` 依赖 Planner scope 才连接；远端 annotations 当前权威影响 effect/risk | 属实 | `mcp/provider.py:58-88`；`mcp/descriptors.py:25-57`（R3 synthetic bootstrap 与 Host policy 优先均为必要新工作） |
| Plugin `schema_version`/generation 在 Adapter→ChatTool→Descriptor 链上丢失 | 属实 | `capability_adapter.py:71-79`（R3 §4.2 修复项成立） |
| Responses declared schema 单调、DSML、DeepSeek 双协议均不发送 `tool_choice` | 属实 | `agent_runner.py:201-216`；`deepseek_responses.py:178`；`openai_compatible.py:141-146` |
| Plugin SDK 的 PlannerSignal/PromptStage.PLANNER_PLAN/PLANNER_SIGNAL_REGISTER/API 1.1 全套存在 | 属实 | `yuki_plugin_sdk/models.py:66-121`、`registrar.py:112-150`、`api.py:7` |
| 统一 JSON Schema 中央验证层不存在，`jsonschema` 仅用于 MCP automation | 属实 | `mcp/automation.py:10-11, 243-266`（R3 §9 是新建工作，依赖已在 `pyproject.toml:11`） |

### 2.4 观测 / DB / 配置 / 发布

| 声称 | 结论 | 关键证据 |
|---|---|---|
| `model_invocations` 无 turn/conversation 关联；`tool_invocations` 仅 conversation hash；全库无 `runtime_turn_id` | 属实 | `model_runtime/db_models.py:23-35`；`persistence/models.py:1810-1819` |
| `voice_cadence()` 口径：decision=reply ∧ voice_intent=neutral，近 20 条，voice∈{voice,text_and_voice,optional} | 属实 | `planner/repository.py:256-284`（R4 0039 回填映射与此逐字段吻合） |
| `ModelTask(task_name)` 对未知 route 抛错并包装为 `invalid model profile configuration`，先于 Alembic 使启动失败 | 属实 | `profiles.py:204-214`（R5 配置迁移器先行的理由成立） |
| 历史 `model_invocations.task` 会被转回枚举，删枚举后历史查询崩溃 | 属实 | `model_runtime/repository.py:70, 140`（R5 tolerant retired string 必要） |
| `start.sh` 自动 `init-db`（Alembic upgrade）、compose `./config:ro` | 属实 | `scripts/start.sh:11`；`docker-compose.yml:27`（R5 备份门先于自动迁移的要求成立） |
| Planner 六事件 + AGENT_*/REPLY_* 事件名、`/healthz` planner 字段、发布魔数 3.5.3/0036/1.1 | 属实 | `yuki_plugin_sdk/events.py:23-42`；`health.py:25-96`；`release_check.py:32-33`、`release_validate.py:90-96`、`release_smoke.py:209` |
| PromptCompiler `static→history→dynamic→current` 稳定前缀合同 | 属实 | `prompting/compiler.py:50-57`（R4 §4.1 锁定保留该合同正确） |
| 迁移编号 0037-0040 与 Head 0036 衔接正确 | 属实 | `migrations/versions/0036_async_memory_attribution.py:14` |

### 2.5 外部引用抽查

总纲 §4.2 引用的 Anthropic Tool Search 官方建议（"保留 3-5 个高频工具常驻、其余 `defer_loading`"）经官方文档验证属实，且文档正确将其标注为"经验值、非 Yuki 必须照搬的下限"，对"只常驻 `request_tools`"的偏离给出了理由（provider-neutral 本地搜索 + Schema 预算）。引用使用方式规范。

---

## 3. 部分属实项（未构成修订，实施时注意）

1. **`desired_messages` 的实际消费者**：`reply_sequence` 主要消费 `delivery_mode` + `plan_hard_max_messages`，`desired_messages` 由 `planner/service.py` 约束后写入 plan（模型侧该字段在 `materialize()` 时被丢弃）。R4 删除两字段的结论不受影响。
2. **`is_scheduled_automation_request()` 的使用位置**：在 `chat.respond()` 中用于 automation 工具授权，不是 processor 的独立路由；R4 §8 保留本地 hint 的方案不受影响。
3. **"Planner 已决定"提示词的真实位置**：不在 Prompt 散文中，而在工具 description（`set_reply_target`「Planner 已给出默认目标」`chat.py:169`；`send_voice`「Planner 已确认…」`agent_tools.py:665`）。R4 §11 清理时应以工具 description 为主要目标。
4. **Plugin approval 无独立 `permissions_hash` 列**：现状为 `manifest_hash` + `approved_permissions` JSON；R3/R5 的"变化即失效重批"可用 manifest hash 实现，语义不变。
5. **配置 scope 枚举为 global/group/user**（非 "private"）；R1 §11 迁移映射按 scope 原值迁移的规则不受影响。
6. **架构边界测试现状为零**：现有 `test_rising_sea_architecture.py` 是行为测试，无 AST import-graph 硬边界；R1 §7 属全新建设，工作量按新建评估。
7. **`TurnOrigin` 双定义**：`automation/models.py:126` 与 SDK `models.py:46` 已是"领域定义 + SDK 投影"结构，R1 提取到 `runtime/origin.py` 时注意三处一致性。

---

## 4. 本次已修订的任务书偏差（共 11 处编辑）

| # | 文件 | 位置 | 偏差 | 修订 |
|---|---|---|---|---|
| 1 | 00 总纲 | §9.1 | 声称"回复 Yuki"判定在 `services/policies.py`；实际在 `planner/necessity.py` forced 逻辑（`necessity.py:234-239`），且 3.5.3 对 direct 轮次仍评分并记录 necessity_score | 改为"新增显式分支 + 迁移来源标注"，并注明 direct 不评分是行为+观测双重变化 |
| 2 | 00 总纲 | §12.2 | "图片写隔离保持"把收紧当保持 | 改为"不弱于 3.5.3，R2 收紧为图片轮 write=disabled，差异进回放与 release note" |
| 3 | 01 R1 | §8 | 0037 只覆盖 4 表，但 `agent_actions`/`speech_generations`/`web_search_runs`/`memory_mutation_receipts`/`memory_tool_receipts` 均无 turn 关联，端到端 join 口径未声明 | 增补"0037 范围刻意最小化"段：mutation/tool receipt 关联归 R2 决定，effect/speech 归 R4 `reply_effect_events`，基线以日志口径近似须注明 |
| 4 | 02 R2 | §1 | 不可损失清单漏隐式 current-self episode 合并、500 字 reply excerpt、`retrieval_enabled=false` overview fallback（文内其他节已提，但合同清单不完整） | 补入清单 |
| 5 | 02 R2 | §1 | `MemoryContextMode` 术语与代码不一致：代码为 `none/lexical/hybrid/overview` 检索模式，focused 只是预算桶 | 增补术语对照段，禁止新 `MemoryContextPolicy` 与检索 mode/purpose 互相顶替 |
| 6 | 02 R2 | §5.2 | "图片轮次禁止写"未标注为收紧（现状仅 `image_write_isolated` 拦带图写命令，`memory_change`/Planner MUTATION 未按图片轮硬关） | 明确定性为收紧，须进回放与 release note |
| 7 | 03 R3 | §3 表 | `set_voice_preference` 被列为现有工具 | 标注"R4 新增；3.5.3 无此工具，偏好由 Planner `voice.preference_change` 驱动" |
| 8 | 04 R4 | §2 | `InboundMessagePolicy` 注释与评分现状不符 | 修正注释 + 新增"评分范围"代码审阅批注 |
| 9 | 04 R4 | §5.2 | 未说明 `set_voice_preference` 是新建工具及原数据库写路径（`processor` 调 `voice_preferences.apply()`） | 增补新增说明与迁移要求 |
| 10 | 05 R5 | §3 | 配置迁移器只删 planner/tool_selection route，未处理 `memory_attribution` 的 `utility_structured→planner` 隐式回退（`profiles.py:186-203`），删除后依赖回退的部署静默丢失 attribution 路由 | 增补"删除前先物化隐式回退"步骤 |
| 11 | 05 R5 | §6 | "删除 Planner statistics/health/admin 命令"与现状不符（无独立 planner 子命令，状态分散在 `/ai`、`model stats`、`/healthz`） | 改为逐处清理并更新快照测试 |

另将上述第 1/2/6/7/10 及 0037 范围问题补录进 06 决议 §2 偏差表（6 行），保持 06 作为"唯一偏差账本"的权威性。

---

## 5. 设计合理性评估（分轮次）

### R1（Runtime 骨架 + 基线）— 合理，注意日历时间

三键分离（`ConversationIdentity`/`TurnCoordinationKey`/`ResolvedMemoryScope`）直接对应代码中已证实的三种分区，是本轮最有价值的合同修正。turn correlation 先行的顺序正确——没有 0037，性能门槛无法举证。风险：退出条件"积累足够基线样本"未定义样本量与采集时长，R1 的日历时间取决于线上流量，建议冻结一个最小样本数。

### R2（Memory Runtime）— 最大轮，方向正确

六维正交合同与现状四模式的映射经核对成立；`MemoryWriteTransition` 的引入确实解决了 passive 下 `write=disabled` 与 `memory.state.write` 可请求性的矛盾。`AttributionCommitter` 同事务重算精确针对已证实的双事务 crash 窗口。R2.1 级联延期的理由（Retriever 在 lexical 质量前先调 Embedding）经代码证实。切换提交"单 owner 原子化"意味着一个巨型 PR（chat.py 相关分支 + Planner prompt/Schema + 测试迁移同时动），建议 PR 内部用提交序列保持可审。

### R3（Capability Runtime）— 诊断准确，三处工作量易被低估

对现状的四个关键诊断（Catalog 逐 context 现算、Plugin revision 字段丢失、MCP 空缓存无入口、未知 Core fail-open）全部证实。易低估项：
1. `DescriptorRegistrySnapshot`/`AuthorizedCatalogSnapshot` 两层拆分是对 provider 协议的重构，不是加一层缓存；
2. MCP synthetic bootstrap（配置态 discovery entry + 有界 `ensure_metadata` + revision 重跑）是全新子系统；
3. 首轮曝光从"DIRECT_ALWAYS + Planner explicit/inherited(≤6)"切到"仅 `request_tools` 常驻 + ≤12"是行为面变更，Responses 单调声明约束下的回归面大。300 条搜索语料与标注是纯新建工作。

### R4（Conversation Runtime）— 细节质量最高的一轮

`reply_effect_events` 的设计（幂等 salt、`(source, source_event_hash)` 唯一、90 天 retention、user_requested 不进分母、回填口径与 `voice_cadence()` 逐字段对齐）经与真实查询逻辑比对完全吻合，是全套文档中数据迁移设计最扎实的部分。注意：emoji-only 轮次现状可由 Planner 决定且 0 次 Main Agent 请求，新架构需 1 次 Main Agent 调 `send_emoji`——净请求数不变但单请求更重，回放集应包含 emoji-only 案例（当前 600 条分配表未单列）。

### R5（清除与发布）— 成熟

先配置迁移、后 Alembic、备份门先于 `start.sh` 自动升级、整套快照回退、tolerant retired task string——全部先决条件都对应已证实的真实故障模式。本次补上 `memory_attribution` 隐式回退物化后，配置迁移链闭环。

---

## 6. 保留意见（不修改任务书，供决策参考）

1. **性能门槛缺少不可达处理程序。** 总纲一面声明"不预先把更快当成事实"，一面把 P50 -35%/P95 -20% 定为硬发布门。若 R1 基线显示 Planner 请求只占端到端延迟的较小比例（例如 Planner 用轻量模型、Main Agent 用重模型时），-35% 可能数学上不可达。建议在 R1 基线冻结后增加一次"门槛复核"决策点，并规定调整门槛的程序（谁批准、以什么证据），避免项目在 R4 末期陷入"门槛不可达但无人有权改"的死锁。
2. **Feature branch 生命周期未定义。** 五轮全部只进 `refactor/3.6-runtime`，期间 `main` 继续 3.5.x。以本仓库体量（源码 9.5 万行、测试 3.9 万行、chat.py 单文件 3206 行），五轮周期内 main 的 hotfix（如 3.5.4）与 feature branch 的漂移不可忽略。建议规定定期 rebase/merge-from-main 节奏与冲突责任人。
3. **R3 搜索质量门建议加中期检查点。** Recall@K≥95%/90% 是全新索引在全新语料上的目标，当前没有任何先验数据支持可达性。建议在 R3 中期（索引原型 + 前 100 条语料）先跑一次预评估，不达标时优先调整文档字段/别名而不是推翻方案。
4. **动态加载与 Provider Prompt Cache 的张力。** 首轮仅 `request_tools` 常驻会把大量 Schema 声明后置到 continuation，工具追加会使缓存前缀失效。总纲 §10 的性能报告已含 cached tokens 项，实施时应把"缓存命中率变化"作为 R3/R4 回放的一级指标而非事后观察。
5. **测试迁移工作量。** 现有 ~123 个测试文件大量消费 `PlannedTurn`/`MemoryAccessMode`/Planner fake。R2/R4 的测试改造量可能超过运行时代码本身，排期时应按"运行时 : 测试 ≈ 1 : 1"预估。

---

## 7. 结论

任务书总体**合理且可执行**：对代码现状的描述经 65+ 项核对基本属实，架构方向有真实代码痛点支撑（Planner 中央化、四维耦合的 `MemoryAccessMode`、Planner Scope 工具曝光、观测无 turn 关联），保留/删除清单边界清晰，迁移与发布链考虑了真实故障模式。本次发现的 11 处偏差已直接修订进对应文档并补录 06 决议偏差表；剩余 5 项保留意见均为流程与排期风险，不构成架构层面的反对理由。

建议开工顺序维持 R1 先行，并在 R1 结束时执行第 6.1 条的门槛复核决策点。
