# R3 开工前代码审阅结论（r3-code-review-findings）

> 基线（开工前）：分支 `codex/refactor-3.6-runtime`（R1/R2 已合入；当时产品版本仍为 Yuki `3.5.3`，Alembic head `0037`）。
> 对照任务书：`03-R3-CAPABILITY-NAMESPACE-RUNTIME.md`，已按 `main@2695484` 与本分支实码核对。
> 审阅范围：任务书第 1 节列出的 capabilities / chat / agent / mcp / plugin_host / SDK 文件，
> 外加 `planner/models.py`、`planner/prompt.py`、`planner/provider.py`、`model_runtime/models.py`、
> `memory/runtime/capability_view.py`、`tests/unit/test_runtime_dependency_boundaries.py`。
>
> 复检（2026-08-18）：产品已是 Yuki `3.6.0`，Alembic head `0040`。下文第 1–10 节保留开工前结论；
> 第 11 节记录按 R3 §14 十一刀全量复检后的缺口与收口。

R1 已冻结 `CapabilityNamespace`、`CapabilitySearchIndex`、`CapabilityRuntime`、
`CapabilityExposurePlanner`、`CapabilitySchemaValidator` 的 Protocol 形状，但生产编排
仍全部在 `ChatService._ChatAgentBackend`：Planner scopes 决定首轮优先级，
`FlashToolReranker` 可走 `ModelTask.TOOL_SELECTION`，`request_tools` 用另一套词法匹配。
本轮是单 owner 切换——Capability Runtime 生效时同步删除 Planner 的 tool mode / scope /
selected-tool 字段，不得保留兼容双轨。

---

## 1. 术语对照（实现期不得混用）

| 名字 | 3.5.3 真实含义 | R3 新含义 | 禁止 |
|---|---|---|---|
| `group` / `scope_ids` | Catalog 分组 + Planner 首轮优先级（**不是**权限） | 用 `namespace` 做发现分类；权限只走 origin/permission/memory view | 不得把 namespace 当权限或硬过滤 |
| `ToolMode` (`inherit/none/read_only`) | Planner 可收紧工具；`none` 关闭本轮工具 | **删除**。Host 用 origin / `tools_closed` / 只读 origin 表达 | 不得把 Planner `none` 当成 Runtime 门 |
| `ToolSelection.scopes` | 显式 scope 包；空列表 = 继承全部后端授权 | **删除**。首轮由 lexical search ∩ 本轮 requestable ids | 不得保留 `planner_scopes_explicit` |
| `planner_intent` | 拼进 tool search query | **不得**作为 Tool Search 输入 | 查询只含当前作者文本 + 可信 reply 摘要 |
| `CapabilityExposure.DIRECT_ALWAYS` | `get_my_capabilities` / `read_tool_artifact` 常驻 | 默认常驻只剩 `request_tools`；二者改条件开放 | 不得继续把权限目录当默认工具 |
| `MemoryAccessMode` | 已在 R2 退为 metrics bucket | 保持；Capability Runtime 只读 `MemoryCapabilityView` | 不得从 Planner 短语猜 memory write |
| Plugin API `1.1` | 当前 SDK / 内置插件 | **2.0**，无 1.1 运行时兼容层 | 不得半加载 1.x 插件 |

`query.py` 的记忆 FTS 与本轮 Tool Search 是两套索引；Tool Search 不得调用记忆 embedding。

---

## 2. 任务书核对：Scope 是权限还是优先级？

**结论：当前 Scope 几乎全是优先级，不是权限。**

- `_ChatAgentBackend.definitions()`（`chat.py:500-502`）写明：Planner scopes 只排首轮 Schema，
  `request_tools` 可加载「真实 actor / origin / tool mode」允许的任何能力。
- 真权限在 `CapabilityPolicyEngine.visible()`：`allowed_origins`、`required_permissions`、
  superuser、以及 `ToolMode.READ_ONLY` 对 effect 的收紧（`policy.py:35-58`）。
- `onebot` / admin 工具靠 `required_permissions={"superuser"}` 与收窄 origin，不靠 Planner 选 scope。
- R2 之后 Memory 首轮由 `MemoryCapabilityView` 经 `apply_memory_tool_groups()` 注入 `memory` group；
  Planner 不能添加 memory scope。R3 必须让 Runtime **直接消费 namespace view**，不再绕 group。

实现锁定：Authority 求交只用 origin、actor permissions、superuser、image write isolation、
external-event 限制、`MemoryCapabilityView`、feature availability。Namespace 只影响排序加分。

---

## 3. MCP lazy metadata 发现时机

当前路径：

1. `MCPToolProvider.descriptors()` 只暴露 **已缓存** tools；空缓存时最多一个 `mcp_gateway`
   （`mcp/provider.py:52-56`）。
2. 真正连 server 发生在 `prepare_scopes()`（`chat.py:_prepare_tool_candidates` 调用），
   且只连接 Planner 选中的 scope / `mcp` / bundle。
3. 空缓存 + 无 Planner mcp scope → **MCP 工具对搜索与首轮都不可见**。这就是任务书批注的
   bootstrap 缺口。

R3 锁定：

- `.mcp.json` 为每个启用 server 建 **不含远端 Schema** 的 synthetic discovery document
  （namespace 优先用配置语义，而不是强制 `mcp.<server>`；provider 仍为 `mcp.<server>`）。
- 搜索命中后再有界 `ensure_metadata(server_id)`（超时 / 并发 / 每轮 server 数）。
- 失败只标记该 namespace 暂不可用，不 eager-connect 全部 server，不扩大权限。
- 远端 `readOnlyHint` **不得**降低 Host 风险：未知 MCP Tool 默认 `WRITE_STATE/MUTATE`；
  只有 operator `toolAnnotations` 可降为 read-only。当前 `descriptor_from_mcp_tool`
  会按 annotation 降风险（`mcp/descriptors.py:50-57`），必须改掉。

---

## 4. Plugin Tool 的 model name 与丢字段

- 生成规则：`plugin__{plugin_id}__{local_name}`，非法字符替换为 `_`，超 64 截断 + 8 位 hash
  （`extension_registry.py:_model_tool_name`）。
- `PluginCapabilityAdapter.definitions()` 只产出 `ChatTool(name, description, parameters)`，
  **丢弃** `ToolMetadata.schema_version`、namespace、aliases、use_when、tags。
- `ChatToolCapabilityProvider` 对未知 CORE 工具 **fail-open 成 memory read**
  （`provider.py:169-173`）。`get_memory_fact` / `get_memory_evidence` 已走这条默认路径。
- Catalog revision 只 hash `provider:model_name:schema_version`（`catalog.py:177-182`），
  描述 / namespace / aliases / 权限 / 完整 schema 变化不会失效索引。

R3 锁定：

- `CapabilityDescriptor` 增加 `namespace` / `aliases` / `use_when`；CORE 必须显式声明，
  未知 CORE 工具 quarantine，不进 Catalog。
- `get_memory_fact` → `memory.fact.read`，`get_memory_evidence` → `memory.evidence.read`，
  并加入 R2 的 `MEMORY_READ_NAMESPACES`（与 history/person/self/group 同受 read policy）。
- Plugin adapter 把 schema_version / namespace / aliases / use_when / tags 完整传入 descriptor。
- Revision 对规范化 descriptor **全内容**取 hash（含 generation）；索引按该 hash 缓存，
  在途 turn 钉住旧 revision。

---

## 5. Responses continuation 对 Schema 删除的限制

`agent_runner.py:201-209` 已实现：Responses continuation **只追加、不删除** 已声明 function/native
tools，否则部分 provider 会 HTTP 400。Chat Completions 每轮可缩小。

缺口：

- 没有「同名 Tool Schema revision 冲突则中止 continuation」的显式错误。
- 撤权后工具仍留在 declared set 里（正确），但 binding 前必须再求交并返回
  `capability_no_longer_authorized`，不得执行。当前主要靠 `_callable_tool_names`。
- 无副作用冲突可重建 chain；有副作用时不得重放。R3 ledger 必须把这条写成 API。

---

## 6. Provider 是否支持稳定 Tool Prefix Cache

当前 **不支持** 跨请求的稳定 prefix cache：

- `_ChatAgentBackend.definitions()` 每次模型请求都重建 registry + catalog + 预算选择，
  工具列表按 name 排序（`chat.py:728`），但成员随 Planner scope / request_tools / exclusive
  write 变化。
- Catalog 不按内容 hash 缓存；MCP `prepare_scopes` 可能在 `_prepare_tool_candidates` 时
  改变缓存。

R3 不要求模型商用 prefix cache 命中，但要求：

- Descriptor registry / FTS 索引不在每次模型请求前全量重建。
- 同一 turn 钉住 registry revision；plugin/MCP refresh 只 copy-on-write 发布新 generation。
- 首轮 function+native 硬上限 12；非常驻最多 8；Top 10 lexical 再裁。

---

## 7. `request_tools` 如何改状态

`_request_tools`（`chat.py:1341-1454`）：

1. 在 **authority-visible** catalog 上跑 `match_requestable_tools`（与首轮 `ToolCandidateSelector`
   / Flash reranker **不是**同一套实现）。
2. 命中后写入 `_requested_tool_names`，并把 scope 并入 `tool_groups`，必要时把
   `tool_mode` 从 `NONE` 抬到 `INHERIT`。
3. 若命中 memory write：`session.request_exclusive_write()`。
4. **不执行**目标工具；下一步模型请求才会把它们标成 `DIRECT_ALWAYS` 挤进预算。

R3 锁定：prefetch 与 `request_tools` **共用** `CapabilitySearchIndex`；命中
`memory.state.write` 仍走 `request_exclusive_write()`，并从 callable set 撤销其他业务写。
Completions 下轮缩小 Schema；Responses 保留历史声明，只允许调用 mutation。

---

## 8. 当前热路径必须删除的 Planner / Flash 接线

| 落点 | 现状 | R3 动作 |
|---|---|---|
| `TurnPlan.tool_selection` / `PlannerToolOutput` | Planner 必选或省略继承 | 从 prompt / schema / `TurnPlan` 删除；LLM 残留字段 before-validator 剥离 |
| `ToolRuntime.tool_mode/groups/planner_*` | Chat 传给 backend | 删除；改为 origin + memory view + capability session |
| `_prepare_tool_candidates` | inherited 分支 + hybrid Flash rerank | 删除 Planner 分支；MCP bootstrap 改由 Runtime 有界触发 |
| `FlashToolReranker` / `ModelTask.TOOL_SELECTION` | hybrid MCP 热路径 | 删除任务、路由、配置默认、deployment 列表 |
| `planner_scope_descriptions` | 注入 Planner prompt | 删除 |
| `CapabilityPolicyEngine` + MCP gateway/binding | 读 `ToolSelection` | 改为 TurnAuthority / scene / memory view |
| `capabilities/policy.py` 对 planner 的 legacy import | R1 边界 allowlist | R3 结束时该项必须清空 |
| Plugin `PlannerSignal` / `planner.signal.register` | SDK 1.1 | 改为 `AdmissionSignal` / `admission.signal.register`；0038 清库 |

Planner 在 R4 前仍可做 conversation/effect 决策（emoji/voice/delivery），但 **不能再改 Tool exposure**。
`send_voice` 是否进入 catalog 由 speech feature availability 决定，不再读
`voice.agent_tool=required` 当曝光门（工具描述里的「Planner 已确认」在 R4 替换；R3 先去掉
Planner 对 tool 列表的授权）。

---

## 9. Namespace 迁移表（本轮冻结）

任务书第 3 节 + 本分支多出的 CORE 工具：

| 能力 | Namespace |
|---|---|
| `web_search` | `web.search` |
| `read_webpage` | `web.read` |
| `get_recent_chat_history` | `memory.history.recent` |
| `search_chat_history` | `memory.history.search` |
| `get_person_memories` | `memory.person.read` |
| `get_self_memories` | `memory.self.read` |
| `get_group_memories` | `memory.group.read` |
| `get_memory_fact` | `memory.fact.read` |
| `get_memory_evidence` | `memory.evidence.read` |
| `memory_change` | `memory.state.write` |
| `get_relationship` | `relationship.read` |
| `automation_*` 读 / `time_get_*` | `automation.read` |
| `automation_*` 写 / `time_set_timezone` | `automation.write` |
| `call_onebot_api` | `qq.platform.mutate` |
| `send_voice` | `reply.voice` |
| `set_reply_target` | `reply.target` |
| `read_tool_artifact` | `kernel.artifact.read` |
| `get_my_capabilities` | `kernel.authority.read` |
| admin 读配置/历史 | `admin.config.read` / `admin.history.read` |
| admin 写配置/动作/重建 | `admin.config.write` / `admin.action.write` / `admin.memory.rebuild` |
| `set_voice_preference` / `send_emoji` / `set_reply_layout` / `decline_reply` | **R4 新增，本轮不发明** |

Native Web 与 function `web_search` 共用 capability id `web_search` 与 namespace `web.search`；
`WebProviderRouter` 只在该能力被选中后选择 Native vs Tavily。

---

## 10. 锁定偏差（实现期不得「为了兼容 3.5.3」改回去）

1. **单 owner**：Chat/AgentRunner 的工具曝光不得再读 `TurnPlan.tool_selection`。
2. **默认常驻只有 `request_tools`**；`get_my_capabilities` 仅权限查询语义或 request 后；
   `read_tool_artifact` 仅本轮已有 handle；`set_reply_target` 仅有可验证 event。
3. **未知 CORE fail closed**，禁止默认 memory read。
4. **Plugin API 2.0 无 1.1 运行时兼容层**；manifest 在 import 前校验。
5. **AdmissionSignal** 只能改 autonomous 群评分，不能改工具/记忆/权限。
6. **搜索无 LLM、无默认远程 embedding**；warm index P50/P95 不混入 rebuild。
7. **0038** 必须在本轮清理 `planner.signal.register` 并把受影响插件置 `pending_approval`。
8. R2 的 `MemoryCapabilityView` / exclusive write / 禁止 Planner 添加 memory 继续有效。
9. `capabilities` 不得 import `planner`（R1 边界 allowlist 本轮清零）。
10. 不向 DeepSeek 发送 `tool_choice`，不依赖 `tool_choice=required`。

---

## 11. 2026-08-18 按 §14 十一刀复检

对照任务书逐条核对生产热路径，而不是对照 3.5.3 基线假装尚未开工。
当时已经切到 Capability Runtime，但首轮曝光仍按旧 Planner `prepare_scopes()` 行事：
水合到的 MCP server 或任意一条 MCP FTS 命中，就会把该 `provider_id` 下的工具静默倒进
`mcp_tool_limit`。这不是 Bundle（选中后整包必开，超预算显式失败），所以「等待」一类弱查询
也能带上八个麦当劳工具。搜索文档曾写入最长 4000 字的远端菜单；`request_tools` 在
`definitions()` 里可能重复追加；超级管理员被 Host 常驻钉上 `call_onebot_api`；
`web_search` 未选中时 Native Web 仍随 `allowed_capabilities={"web"}` 发送。

| §14 提交 | 复检结论 |
|---|---|
| 1 `add semantic namespaces` | CORE 叶 namespace 与 MCP per-tool `namespace/aliases/useWhen/tags` 已补齐 |
| 2 `add revision-cached FTS5 search index` | 文档体改为 compact 400 字；短中文不再靠 2-gram 误中肥菜单；alias 按 term 命中 |
| 3 `add authority-first exposure runtime` | 删除 provider dump；水合只刷新索引再搜；Bundle 全开或 `bundle_exceeds_schema_budget`；Native 仅在 `web_search` 已选中后绑定；`call_onebot_api` 不再 Host 常驻 |
| 4 `unify prefetch and request_tools search` | 删除第二套 `match_requestable_tools`；两条路径只走 `CapabilitySearchIndex` |
| 5 `add monotonic exposure ledger` | `request_tools` 去重；无副作用 schema 冲突可重建 chain；有副作用 fail closed |
| 6 `add central schema validation` | 锁定 Draft 2020-12；拒绝远端 `$ref`、未知 dialect、超限与嵌套量词正则 |
| 7 `release API 2.0 namespace metadata` | 已在 SDK/`ToolMetadata`/adapter 落地，本轮无空提交 |
| 8 `revoke legacy planner signal approvals` | `0038` 与回归已在库中，本轮无空提交 |
| 9 `remove planner scopes and flash reranker` | 删除 `ToolCandidateSelector` / `ToolSelectionMode` / `mcp/selector.py` 与 `planner_scope_explicit` 指标。`FlashToolReranker` 与 `ModelTask.TOOL_SELECTION` 此前已不在 `src` |
| 10 `add search quality and responses suites` | 保留 ≥300 语料与 P50/P95；补首轮「等待」不曝光麦当劳、以及 Responses ledger 合同 |
| 11 本文 | 记录上述复检，不改写第 1–10 节开工前事实 |

仍属后续轮次、不得塞进 R3 的项：

- `src/qq_ai_bot/planner/` 整包删除是 R5，不是本轮。
- `automation_create` 目录不再塞进 description 的热修、以及文档里残留的 Planner 用语，不混入上述十一刀。
- Namespace 仍不是权限，也不是硬过滤。
