# R3：建立 Capability Namespace Runtime

> 目标：删除 Planner Scope 对 Tool 曝光的所有权，建立本地 Semantic Namespace、统一搜索索引、Schema Budget 和单调 Exposure Ledger。  
> 本轮结束后，普通插件新增 Tool 不需要修改 Core Planner，因为 Core Planner 已不再参与 Tool Routing。
> 已按代码核对：`main@2695484`。

> **代码审阅批注**：这里的 “Semantic Namespace” 指可搜索的语义元数据，不是 embedding/LLM 语义检索。原稿未处理当前 Catalog 的 per-turn provider 生成、MCP 空缓存 bootstrap、中文分词、Responses 已声明 Schema 与当前授权的冲突，直接实现会导致找不到 MCP Tool 或撤权失效。

R3 的切换提交同样是单 owner：Capability Runtime 生效时同步从 Planner prompt/Schema/`TurnPlan` 删除 tool mode、scope 与 selected-tool 字段，并删除热路径 Flash Tool Reranker 和 `ModelTask.TOOL_SELECTION`。Planner 在 R4 前只保留尚未迁移的 conversation/effect 决策，不能继续修改 Tool exposure。

## 1. 开工前 Codex 审阅

重点阅读：

```text
src/qq_ai_bot/capabilities/models.py
src/qq_ai_bot/capabilities/catalog.py
src/qq_ai_bot/capabilities/policy.py
src/qq_ai_bot/capabilities/provider.py
src/qq_ai_bot/capabilities/selection.py
src/qq_ai_bot/capabilities/request.py
src/qq_ai_bot/capabilities/binding.py
src/qq_ai_bot/capabilities/results.py
src/qq_ai_bot/capabilities/coordinator.py
src/qq_ai_bot/services/chat.py
src/qq_ai_bot/services/agent_runner.py
src/qq_ai_bot/services/agent_tools.py
src/qq_ai_bot/mcp/provider.py
src/qq_ai_bot/mcp/descriptors.py
src/qq_ai_bot/mcp/binding.py
src/qq_ai_bot/plugin_host/*
src/yuki_plugin_sdk/registrar.py
src/yuki_plugin_sdk/models.py
```

重点搜索：

```bash
rg -n "ToolGroup|ToolMode|ToolSelection|planner_scope|planner_tool_groups" src tests
rg -n "FlashToolReranker|TOOL_SELECTION|selected_tool_names" src tests
rg -n "request_tools|first_round_tool_hit|schema_token_budget" src tests
rg -n "planner_scope_descriptions|register_planner_signal" src tests
```

Codex 必须确认：

- 哪些 Scope 当前用于权限，哪些仅用于优先级。
- MCP lazy metadata 的发现时机。
- Plugin Tool 的真实 model name 生成规则。
- Responses continuation 对 Tool Schema 删除的限制。
- 当前 Provider 是否支持稳定 Tool Prefix Cache。
- request_tools 的状态修改方式。

输出 `docs/architecture/yuki-3.6.0-refactor-plan/reviews/r3-code-review-findings.md`。

---

## 2. 新 Capability Runtime

```text
src/qq_ai_bot/capabilities/
  runtime.py
  namespace.py
  search_document.py
  search_index.py
  exposure.py
  validation.py
  observability.py
```

### 2.1 CapabilityDescriptor 扩展

新增最终字段：

```python
namespace: str
aliases: tuple[str, ...] = ()
use_when: tuple[str, ...] = ()
```

保留：

```text
canonical_name
model_name
provider_id
trust_source
effect
risk
allowed_origins
required_permissions
idempotency
binding
parallel_safe
schema_version
input/output schema
provider metadata revision
```

删除 Planner 语义：

- `group` 不再代表 Planner group。
- `scope_ids` 不再由 Planner 选择。
- 可以保留兼容数据库字段只到当前开发分支中间提交，R3 结束时必须用 namespace 完整替代。

### 2.2 Namespace Model

```python
class CapabilityNamespace(BaseModel):
    id: str
    parent: str | None
    display_name: str
    description: str
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
```

规则：

- Namespace 是语义分类，不是 Provider。
- Namespace 不是权限。
- Namespace 不是硬路由门。
- 叶 Namespace 理想 Tool 数量小于 10。
- 一个 Tool 可以只有一个主 Namespace，附加 aliases/tags 处理交叉语义。
- Namespace metadata 只影响发现；所有 exact/alias/FTS 结果仍必须与本轮 authority-filtered requestable capability id 取交集。
- Core Tool 必须显式声明 effect/risk/origin/permission；未知或缺元数据的 Core Tool fail closed/quarantine，禁止当前“默认 memory read”式 fail-open。

---

## 3. Namespace 迁移建议

| 当前能力 | 新 Namespace |
|---|---|
| web_search | `web.search` |
| read_webpage | `web.read` |
| get_recent_chat_history | `memory.history.recent` |
| search_chat_history | `memory.history.search` |
| get_person_memories | `memory.person.read` |
| get_self_memories | `memory.self.read` |
| get_group_memories | `memory.group.read` |
| memory_change | `memory.state.write` |
| get_relationship | `relationship.read` |
| automation_create | `automation.write` |
| automation_list/get | `automation.read` |
| call_onebot_api | `qq.platform.mutate` |
| send_voice | `reply.voice` |
| set_voice_preference（R4 新增；3.5.3 无此工具，偏好由 Planner `voice.preference_change` 字段驱动） | `reply.voice.preference.write` |
| send_emoji（R4 新增） | `reply.emoji` |
| set_reply_layout（R4 新增） | `reply.layout` |
| set_reply_target | `reply.target` |
| decline_reply（R4，仅 autonomous） | `reply.admission.decline` |
| read_tool_artifact | `kernel.artifact.read` |
| get_my_capabilities | `kernel.authority.read` |

MCP Tool Namespace 优先使用配置中的语义 namespace，而不是强制 `mcp.<server>`。Provider 信息仍保存为 `mcp.<server>`。`.mcp.json` 扩展 per-tool `namespace/aliases/useWhen/tags` 与 operator risk/origin/permission override；远端 annotations 仅作描述性下限，不能降低 Host 风险分类、扩大 origin/permission 或自行决定 finalize-after-commit。

---

## 4. CapabilitySearchIndex

### 4.1 Search Document

```python
class CapabilitySearchDocument(BaseModel):
    model_name: str
    canonical_name: str
    namespace_id: str
    namespace_path: tuple[str, ...]
    namespace_description: str
    description: str
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    parameter_names: tuple[str, ...]
    parameter_descriptions: tuple[str, ...]
    provider_id: str
    trust_source: CapabilityTrustSource
    effect: CapabilityEffect
    risk: CapabilityRisk
    estimated_schema_tokens: int
```

### 4.2 索引实现

采用单一、可复现的词法实现：

```text
exact map
alias map
in-memory SQLite FTS5 BM25
CJK 2/3/4-gram materialization + ASCII normalized terms
```

Catalog/Index 分两层：

```text
DescriptorRegistrySnapshot（context-free discovery metadata）
  -> per-turn Authority/Availability projection
  -> AuthorizedCatalogSnapshot（固定一个 registry revision）
  -> search
```

索引按 `DescriptorRegistrySnapshot.revision` 缓存：

- Catalog revision 未变化：复用。
- Plugin install/update、MCP refresh、Schema/metadata 内容变化：copy-on-write 重建并原子发布；在途 turn 继续固定旧 revision。
- 不在每次模型请求前重建 Catalog 和 Index。
- 不对每轮 query 调用远程 embedding。
- 不调用 LLM rerank。

Revision 对规范化 descriptor 全内容取 hash：canonical/model name、description、namespace、aliases/use_when/tags、input/output schema、effect/risk/origin/permissions 与 provider generation。不能只使用 `provider:model_name:schema_version`，也不能缓存任何已经按具体用户/群权限投影的 Catalog。

修复当前 Plugin adapter 丢字段问题：`ToolMetadata.schema_version`、manifest/content hash 与 Plugin manager generation 必须经 `PluginCapabilityAdapter -> ChatTool -> DescriptorRegistry` 完整传播；MCP metadata refresh 和 Core 配置变化也各自递增 provider generation。否则 Schema/描述改变时索引不会失效。

FTS 规定 tokenizer version、MATCH 转义、query/document 长度上限、column weights 和 `(score, canonical_name, capability_id)` 稳定 tie-break。沿用当前 `capabilities/request.py` 的 CJK n-gram 思路；默认 Unicode tokenizer 不作为中文可用性的假设。

### 4.3 排序

示意：

```text
exact name        最高优先级
exact alias       高优先级
BM25 tool score
BM25 namespace score
parameter match
recent namespace affinity 小幅加分
schema cost penalty 小幅扣分
```

Namespace score 只加分，不做 hard filter。

`active namespace affinity`：

- 每个 ConversationTurnSession 可保存短期 affinity。
- TTL 短。
- 只影响排序。
- 新 query 的强证据可以覆盖 affinity。
- 不影响权限。

`CapabilityQuery` 只含当前作者文本、可信 reply 摘要、origin/command hints 和短期 namespace affinity；不把整段历史或 Planner intent 拼入查询。用户 query 不写入数据库/日志，质量语料仅使用合成或脱敏文本。

### 4.4 MCP Lazy Bootstrap

MCP 空缓存时仍须可发现：

1. 从 `.mcp.json` 为每个启用 server/namespace 建立不含远端 Tool Schema 的 synthetic discovery entry。
2. 本地搜索命中后执行有界 `ensure_metadata(server_id)`，限制连接超时、并发和每轮 server 数。
3. metadata 成功后发布新 provider revision，并在同一 authority 下重跑一次本地搜索。
4. 失败仅返回该 namespace 暂不可用，不 eager-connect 全部 server，不扩大权限。

远端未知 Tool 默认保守为不可曝光或 `WRITE_STATE/MUTATE`；只有 operator policy 可把它降为 read-only。

---

## 5. Authority First

搜索前先用真实 Authority 过滤：

```text
origin
actor permissions
superuser
image write isolation
external event restrictions
MemoryCapabilityView
runtime feature availability
```

搜索索引可以包含完整 Catalog，但结果必须只来自本轮真正可请求的集合。

曝光不是永久授权：每次 Tool Call 在 binding 前重新求交 `TurnAuthority` ceiling、当前数据库权限、DelegatedAuthority 的 schema/provenance/origin、最新 feature availability 与 monotonic taint。中途撤权返回稳定错误并从 `CallableCapabilitySet` 移除；不得因为本轮早先搜索命中过就沿用 stale view。

Planner 从来不能添加权限，3.6.0 进一步做到 Planner 完全不存在。

---

## 6. Initial Exposure

### 6.1 Stable Kernel Tools

默认唯一常驻：

```text
request_tools
```

`get_my_capabilities` 仅在权限查询语义命中或经 `request_tools` 请求后曝光；`read_tool_artifact` 仅当本轮已经产生 artifact handle 后曝光。`set_reply_target` 仅当当前上下文存在后端可验证的 event 时加入；`decline_reply` 仅在 autonomous-group admission 已进入 Main Agent 时加入，并由后端作为无副作用的 terminal control 处理。

### 6.2 Retrieved Tools

本地索引先取稳定 Top 10 候选，再由 authority 与 Schema Budget 选择最多 8 个非常驻能力；首轮所有 function/native Tool 合计硬上限为 12。三个值进入热配置但以上述值作为 3.6.0 默认和 release replay 合同。选择继续受以下限制：

- 全局 Tool 数量上限。
- Schema Token Budget。
- MCP 独立预算。
- 必须保留当前状态下合法的 Kernel Tool；常态只有 `request_tools`，条件式 Kernel Tool 不能借此绕过自己的开放门。
- MemoryTurnSession 的 exposure policy。
- Native Provider Tool（例如原生 Web）必须拥有与 function Tool 相同的 capability id、namespace 和 authority policy，纳入同一搜索与预算；`WebProviderRouter` 只在 `web.search` 已被选中后决定 Native/Tavily provider。

初始曝光总 Tool 数不得超过 12；超出时按 authority hard filter、required Kernel、稳定相关度、schema bytes、capability id 的顺序确定性裁剪并记录预算原因。

---

## 7. request_tools

接口保持简单：

```json
{
  "query": "搜索并发送网易云单曲",
  "max_results": 4
}
```

内部调用与首轮预取完全相同的 `CapabilitySearchIndex`。

返回：

```json
{
  "loaded_tools": [
    {
      "name": "plugin__netease__search_song",
      "namespace": "music.search",
      "description": "搜索歌曲"
    }
  ]
}
```

### 7.1 Memory Transition

当搜索结果包含 `memory.state.write`：

1. 调用 `MemoryTurnSession.request_exclusive_write()`。
2. Session 验证 transition。
3. Capability Runtime 从 `CallableCapabilitySet` 撤销其他业务写能力。
4. Chat Completions 下一次请求发送最小合法集合；Responses continuation 保留历史已声明 Schema，但只允许调用 mutation Tool。

`request_tools` 不直接执行 Tool。

---

## 8. DeclaredSchemaLedger 与 CallableCapabilitySet

每个 Agent Turn 保存：

```text
declared schema（provider continuation contract）
currently callable capability ids（runtime authority contract）
registry/authorized/exposure revision
schema token total / provider strategy
provider continuation state
```

规则：

- Responses 中 declared Schema 只追加；Chat Completions 每轮可按当前 callable set 缩小 Schema，避免无意义 Token 膨胀。
- Tool name 去重。
- 已声明同名 Tool Schema revision 冲突时中止当前 continuation，不能静默覆盖。
- 新 Tool 按 canonical name 稳定追加。
- 达到累计 Schema Budget 后拒绝新加载。
- 已声明但被撤权的 Tool Call 返回稳定 `capability_no_longer_authorized`，不执行。

Schema 冲突后的恢复策略必须显式：若尚无 side effect，可从 immutable initial transcript + sanitized prior tool results 启动新的 provider chain；若已有 side effect，只能在 idempotency/commit ledger 证明不会重放时恢复，否则 fail closed 并使用已有 receipt 确定性收束。

---

## 9. 统一 JSON Schema Validation

当前 Yuki 主要由各 Tool Handler 自行检查参数。R3 在 Binding 前新增中央验证：

```text
ToolCall
  -> JSON parse
  -> declared Tool check
  -> JSON Schema validation
  -> domain-specific validator
  -> binding.invoke
```

要求：

- 使用现有 `jsonschema` 依赖。
- Catalog admission 时按 schema fingerprint 编译并缓存 validator；非法 Schema quarantine，不等到用户调用才发现。
- 固定支持的 JSON Schema dialect；拒绝 remote/network `$ref`、未知 dialect、超限深度/大小和不安全 regex，校验器绝不访问网络。
- Validation error 返回稳定 `error_code=tool_input_validation_failed`。
- 不将异常 Schema 或内部路径发给用户。
- Plugin Tool 的 Pydantic Model 与 Capability JSON Schema 必须一致。
- MCP 仍保留 server-side validation，但 Host 先验证已缓存 Schema。
- Central input validation 后仍保留 Pydantic/domain validation；Plugin 输出按声明合同验证，MCP 任意输出先做有界 normalization，不能因不声明 output schema 而破坏兼容。

这吸收 OpenAI Function Tool 的 Pydantic validation 和 Anthropic strict tool use 的设计目标。

---

## 10. Plugin API 2.0

`ToolMetadata` 新增：

```python
namespace: str
aliases: tuple[str, ...] = ()
use_when: tuple[str, ...] = ()
tags: tuple[str, ...] = ()
```

删除：

```text
PlannerSignal
PlannerSignalContext
PlannerSignalRegistration
register_planner_signal
planner_scope_descriptions
```

新增：

```text
AdmissionSignal
AdmissionSignalContext
AdmissionSignalRegistration
register_admission_signal
```

AdmissionSignal 只能影响群聊参与评分，不能影响 Tool 权限、Namespace 或 Memory Contract。

Plugin API 2.0 不提供 1.1 运行时兼容层。3.6.0 发布文档提供迁移示例。

API 2.0 是全合同升级：同步删除/替换 `PluginPermission.PLANNER_SIGNAL_REGISTER`、`ExtensionKind.PLANNER_SIGNAL`、`PromptStage.PLANNER_PLAN`、`PromptTarget.PLANNER/BOTH`、registrar/adapter/export、内置 manifest、示例和 frozen snapshots。Plugin manifest/API version 在导入代码前校验；1.0/1.1 清晰拒绝，不允许半加载。

`AdmissionSignal` 仅用于 autonomous group，具有单插件/总分 cap、timeout、TTL 和最小隐私 context；不能 veto private/reply/@，不能改变 Tool 排序、Memory contract 或 Authority。Namespace/alias/tag/use_when 规定保留前缀、格式、长度、数量和归一化去重。metadata/permissions/API 变化使旧审批失效，升级器 sanitize 旧 `planner.signal.register` 并要求重新批准。

基线迁移号未变化时，R3 使用 `0038` 清理数据库中旧 `planner.signal.register` approval/extension 数据并把受影响插件置为 `pending_approval`；不得等到 R5 才让 Plugin API 2.0 的数据库状态生效。

---

## 11. 删除项

R3 结束时删除：

- `FlashToolReranker` 热路径。
- `ModelTask.TOOL_SELECTION`。
- Tool Selection 模型路由和配置。
- `planner_scope_descriptions`。
- `planner_scopes_explicit`。
- `planner_tool_groups`。
- `planner_intent` 作为 Tool Search 输入。
- `ToolGroup` 的 Planner 所有权。
- `_prepare_tool_candidates()` 中 Planner inherited/explicit 分支。

`request_tools`、Schema Budget、Provider Registry、Binding、Tool Result Budget 保留。

---

## 12. 测试与指标

### 12.1 Search Quality

建立至少 300 条 Tool Query 测试：

- Core 100
- Plugin 80
- MCP 80
- 中英文同义表达 40

指标：

- common Recall@K >= 95%
- overall Recall@K >= 90%
- zero-result <= 2%
- wrong namespace 不阻止正确 Tool 进入 Top-K

固定 `eligible query`、required-tool set、K、Schema Budget、token estimator version 和 corpus version；hit 定义为 Top-K/预算后集合包含该案例全部 required tools。零工具案例单独评估，不混入分母。300 条语料拆分 tune/release holdout，覆盖中文 n-gram、英文、混合文本、aliases、hard negatives、MCP bootstrap 与 Native Tool。

### 12.2 Performance

- Index rebuild 只在 revision 变化时执行。
- Search P50 < 10 ms。
- Search P95 < 25 ms。
- 初始 Tool 数中位数 <= 10。
- 初始 Schema Token 中位数相对完整授权 Catalog 至少下降 60%。
- 常见请求 request_tools rate < 5%。

10/25 ms 仅针对固定 runner、SQLite/FTS 版本、warm index、固定查询批次与 percentile 算法；revision 变化后的冷重建单独报告，不混入热搜索分位数，也不得在普通请求同步等待全量 rebuild。

### 12.3 Security

- Planner 删除后权限结果不变。
- request_tools 不能加载未授权 Tool。
- Plugin/MCP 不能伪造 namespace 取得权限。
- memory exclusive transition 正确。
- destructive Tool 不进入 read-only origin。
- MCP remote annotations 不能降低 Host 风险或扩大 origin/permission。
- 未分类 Core Tool fail closed。
- turn 中途权限、delegation schema/provenance、feature availability 或 taint 变化后，stale exposed Tool 在 binding 前被拒绝。
- undeclared Tool Call 被拒绝。
- declared-but-revoked Tool Call 被拒绝且不执行。
- Schema validation 必须先于 Binding。

### 12.4 Responses

- continuation 中旧 Tool 保留。
- 动态 Tool 追加。
- 不重复。
- Schema revision 冲突中止。
- 无副作用冲突可安全重建 chain；有副作用时不重放。
- 累计预算生效。
- DSML 恢复只允许已声明 Tool。

---

## 13. 本轮禁止事项

- 不用 LLM 选择 Namespace。
- 不用远程 embedding 作为默认 Tool Search。
- 不保留 Planner Scope 兼容字段。
- 不按 Provider 组织模型可见 Namespace。
- 不让 Namespace 成为权限。
- 不让 Namespace 成为硬过滤条件。
- 不把 `request_tools` 变成代理执行 Tool 的 gateway。
- 不向 DeepSeek 发送任何 `tool_choice` 字段，不依赖 `tool_choice=required` 保证 Tool Call。

---

## 14. 建议提交顺序

1. `feat(capability): add semantic namespaces`
2. `feat(capability): add revision-cached FTS5 search index`
3. `feat(capability): add authority-first exposure runtime`
4. `feat(capability): unify prefetch and request_tools search`
5. `feat(capability): add monotonic exposure ledger`
6. `feat(capability): add central schema validation`
7. `feat(plugin-sdk): release API 2.0 namespace metadata`
8. `migration(plugin): revoke legacy planner signal approvals`
9. `refactor(capability): remove planner scopes and flash reranker`
10. `test(capability): add search quality and responses suites`
11. `docs(refactor): record R3 code review findings`

---

## 15. R3 退出条件

- Tool Exposure 不读取任何 Planner 字段。
- `ModelTask.TOOL_SELECTION` 已删除。
- 新 Tool Search 无 LLM 和远程 embedding。
- Descriptor Registry/Authorized Snapshot 分层，in-flight revision 固定。
- MCP 空缓存可通过 synthetic namespace entry 有界发现，远端 metadata 失败安全降级。
- Plugin API 2.0 已完成。
- 首轮命中率和搜索延迟达到门槛。
- Responses continuation 回归通过。
- 新增 Plugin Tool 不修改 Core 搜索逻辑。
- 全量测试通过。
