# `memory_change`：Yuki 自主记忆更改接口

> 状态：Memory Mutation V2 已在 `codex/memory-mutation-service` 分支实现；本文前半保留设计
> 推导，实际运行边界以“实施说明”一节和代码测试为准。
> 目标读者：项目维护者、架构评审者和参与方案讨论的语言模型。

## 0. 实施说明

- 模型侧只增加 `memory_change`，仅在真实 `user_message` 轮开放；参数不能携带 QQ 号、群号
  或事件 ID，后端只接受当前发送者、当前群、真实 mention 和 reply author 别名。
- `MemoryMutationService` 统一执行主体解析、权限、证据、版本、冲突、事务、回执和 Embedding
  调度；Agent、生产 Memory Worker、确定性命令、管理员 Action、Plugin Memory Facade 和有界
  lifecycle reflection 与可恢复后台反思 Worker 均接入该边界。
- Alembic `0025` 新增 `memory_mutation_receipts`，分别保存请求幂等指纹和不含 operation 的
  claim 指纹；Agent 与 Worker 对同一事件、目标、key、内容的判断只提交一次。
- Alembic `0026` 新增 `memory_reflection_jobs`；后台有界扫描重复、争议和归属异常，持久领取、
  退避重试并恢复超时任务，实际更改仍只能经 `MemoryMutationService` 提交。
- 普通成员可影响本人 `person/person_group`、当前 `group` 和当前群他人的 `person_group`；
  第三方来源始终记录为 `third_party`，高权威冲突可以实际落为 `contest`，不会冒充本人。
- 读取本轮提及群友时只开放当前群 `person_group`，不再投影对方跨群 `person` 事实。
- 普通变更是版本化/状态化操作，不做物理删除；`forgetme` 仍沿用独立隐私删除路径。
- 普通对话中的创建、纠正、撤回和恢复由 Main Agent 调用 `memory_change`；Memory Runtime 只在
  真实 `user_message` 轮开放写事务。该路径跳过自动召回，首轮依据 capability metadata 只开放
  已授权的 `memory/write_state` 能力；不依赖自然语言关键词硬编码。
- 修改轮次的最终正文由聊天后端根据真实工具回执渲染。未调用、歧义、未找到、noop 或 contest
  不得声称原请求已完成；`invalidate` 必须表述为撤回/失效且保留审计，不得表述为物理删除。
- mutation 轮次只向主 Agent 暴露唯一写能力并追加有界执行契约；DeepSeek 的 wire payload
  始终省略不受支持的 `tool_choice`，因此正确性必须来自能力隔离和后端完成门。
- 通用工具候选裁剪不得移除 mutation 写能力。Agent 不知道内部 `memory_key` 时应使用
  `old_content`；若误将用户可见标签填作 key，后端只可将其用于返回正文词法候选，再由 Agent
  使用真实 `fact_id` 重试，不能直接模糊变更。

## 1. 摘要

Yuki 当前拥有成熟的自动记忆抽取、冲突治理、版本化事实、证据、生命周期和检索系统，但普通
聊天 Agent 只有记忆读取工具。用户可以通过确定性命令修改部分本人记忆，超级管理员可以通过
管理员工具修改人物记忆；Yuki 自己在回复或反思时不能主动提交记忆变更。

本文建议新增一个核心 Agent 工具：

```text
memory_change
```

它同时服务于三类来源：

1. 自动抽取器根据新消息更新记忆；
2. 普通用户通过自然语言委托 Yuki 修改记忆；
3. Yuki 根据证据主动纠错、合并、质疑或调整记忆。

三条路径必须进入同一个领域服务和同一套幂等、版本、证据、权限及审计规则，避免旧版本中
“自动抽取写一次、显式接口再写一次”的双写问题。

本文推荐的基本立场是：

> 普通用户拥有自然语言修改权，Yuki 拥有自主判断权；后端不替 Yuki 决定记忆内容，但负责
> 身份真实性、证据来源、原子事务、版本历史、幂等和可恢复性。

## 2. 当前状态

### 2.1 已有能力

Memory V2 已经具备：

- `person`、`person_group`、`group` 三种作用域；
- `active`、`contested`、`superseded`、`invalidated` 状态；
- 事实版本链和 `supersedes_id`；
- 绑定真实 `chat_events` 的证据；
- `explicit`、`self_report`、`group_report`、`third_party` 权威级别；
- 自动抽取、语义冲突判断和确定性 Resolution Policy；
- FTS、Embedding、生命周期维护和审计历史；
- 本人显式 `correct`、`invalidate`、`restore` 命令；
- 超级管理员 `memory.add/update/delete/prune` Action。

### 2.2 当前缺口

普通聊天 Agent 的核心记忆工具均为只读：

```text
get_person_memories
get_group_memories
get_memory_fact
get_memory_evidence
```

这意味着：

- Yuki 可以在回答中发现记忆可能有错，但不能直接修正；
- 普通用户用自然语言纠错时，主要依赖后台抽取器稍后处理；
- Yuki 无法在一次对话轮内明确告诉后端“我采用了哪个新版本”；
- 争议、重复、归属错误和低置信度事实缺少 Agent 主动治理入口；
- 管理员写接口不能直接作为 Yuki 自主记忆接口使用。

## 3. 设计目标

### 3.1 功能目标

- 普通用户可以直接用自然语言修改有权影响的记忆，不要求记忆 ID 或固定命令格式；
- Yuki 可以在回复中发现错误后主动调用工具修正；
- Yuki 可以在后台反思任务中治理长期争议、重复、归属错误和过时事实；
- `person_group` 和 `group` 保持开放，普通群成员的陈述能够真正影响群内记忆；
- 保留现有自动更新能力；
- 所有变更可解释、可审计、可恢复；
- 同一事件被多条入口处理时，不产生重复 active 事实。

### 3.2 非目标

- 不向模型暴露裸 SQL 或 ORM Session；
- 不允许模型伪造用户身份、群身份、事件 ID 或证据发送者；
- 不把 Yuki 自己生成的回复自动视为外部事实证据；
- 不让一次普通记忆修改顺带修改权限、自动化、插件或其他业务表；
- 不以不可恢复的物理删除作为普通记忆治理手段。

## 4. 核心原则

### 4.1 放权发生在认知决策层

Yuki 决定：

- 哪条记忆需要改变；
- 新判断是什么；
- 是纠正、失效、争议、合并还是归属修正；
- 自己有多确信；
- 使用哪些可见证据支持判断。

后端负责：

- 将模型选择的主体引用解析为真实身份；
- 校验证据确实存在且属于允许的会话范围；
- 维护状态机、版本链、索引和事务一致性；
- 记录真正的触发者、决策者和执行者；
- 防止重复提交和并发覆盖。

### 4.2 记忆是有来源的信念，不是无来源的真相

普通用户有权影响记忆，但“谁说的”不能被改写：

```text
本人自述                         → self_report
群成员描述当前群共同情况         → group_report
群成员描述当前群内另一名成员     → third_party
用户明确要求系统记住             → explicit（仅限其有权明确声明的目标）
Yuki 依据多条真实证据进行归纳     → 由证据决定 authority
Yuki 没有外部证据的推测           → bot_inference 或 contested proposal
```

“普通用户可以修改”不等于“普通用户可以冒充别人本人确认”。

### 4.3 更改默认版本化，而不是原地覆盖

- `correct` 创建新事实，旧事实进入 `superseded`；
- `invalidate` 只改变状态，不物理删除；
- `restore` 创建可审计的恢复状态事件；
- `merge` 保留来源证据和等价关系；
- `reassign` 在新主体下创建正确版本，并使错误归属版本失效；
- 只有隐私删除等明确流程执行物理清理。

## 5. 工具接口

### 5.1 为什么使用一个统一入口

建议向模型暴露一个统一工具，而不是继续扩展管理员 `memory.add/update/delete`：

- 所有操作共享同一身份和证据上下文；
- 模型容易发现和使用；
- 后端可以对请求动作进行降级，例如把证据不足的 `correct` 降为 `contest`；
- 自动抽取、确定性命令和 Agent 调用可以复用同一提交服务；
- 幂等键可以覆盖所有入口。

工具名：

```text
memory_change
```

工具效果：

```text
CapabilityEffect.WRITE_STATE
CapabilityRisk.MUTATE
```

它应当是 Core Agent capability，而不是 Admin capability。

### 5.2 建议请求结构

```json
{
  "operation": "correct",
  "fact_id": 123,
  "target": {
    "subject_ref": "current_speaker",
    "scope_type": "person_group"
  },
  "new_content": "现在住在上海",
  "memory_key": "location:home",
  "category": "location",
  "reason": "当事人在本轮明确表示已经搬到上海",
  "confidence": 0.96,
  "evidence_refs": ["current_event"],
  "expected_fact_state": "active"
}
```

模型可提交字段：

| 字段 | 用途 |
|---|---|
| `operation` | 希望执行的领域动作 |
| `fact_id` | 修改现有事实时使用，只能来自本轮可见检索结果 |
| `selector` | 没有 `fact_id` 时按 key/旧内容在已解析 target 内有界定位 |
| `merge_fact_id` | merge 的目标事实 ID |
| `merge_selector` | 没有 `merge_fact_id` 时定位 merge 目标 |
| `target.subject_ref` | 后端提供的可信主体引用 |
| `target.scope_type` | `person`、`person_group` 或 `group` |
| `new_content` | 创建、纠正或归属修正后的内容 |
| `memory_key` | 创建时的稳定事实槽；修改时通常沿用旧 key |
| `category` | 有界分类 |
| `reason` | 简短、可审计的变更理由，不保存隐藏推理 |
| `confidence` | Yuki 对当前判断的自评，不直接等于事实最终 confidence |
| `evidence_refs` | 本轮后端提供的事件引用，不接受任意数据库 ID |
| `expected_fact_state` | 乐观并发检查 |

`selector` 至少包含 `memory_key` 或 `old_content`，可附加 `category`。没有 `fact_id` 时必须同时
提供合法 `target`。后端不调用 Embedding，不跨人物、群或 SELF 可见性；所有已提供字段唯一精确
命中时才执行。否则只返回至多 3 条包含 `fact_id/memory_ref/key/category/kind/content/status` 的
词法候选，或返回 `memory_candidate_not_found`，数据库保持不变。

后端注入且模型不能提交或覆盖的字段：

```text
trigger_event_id
trigger_actor_user_id
decision_actor_type
executed_by_bot_id
conversation_key
current_group_id
turn_origin
delegation_mode
idempotency_key
occurred_at
```

### 5.3 操作集合

| 操作 | 含义 | 典型结果 |
|---|---|---|
| `create` | 创建一个此前不存在的事实 | 新 `active` fact |
| `correct` | 旧内容错误或已经发生变化 | 新版本 active，旧版本 superseded |
| `invalidate` | 事实不应继续参与回忆 | 原 fact invalidated |
| `restore` | 恢复一条可恢复的失效事实 | fact active |
| `contest` | 有矛盾但无法可靠选择 | 旧事实/新 claim 进入争议结构 |
| `merge` | 多条事实本质相同 | 合并证据，重复项 superseded/invalidated |
| `reassign` | 内容正确但人物或作用域归属错误 | 新 target 创建版本，旧 target 失效 |
| `update_metadata` | 调整重要度、置信度建议或有效期 | 正文不变，记录状态事件 |

工具不提供普通 `purge`。物理删除继续由隐私删除、`forgetme` 或明确的高权限数据治理流程负责。

### 5.4 建议响应结构

```json
{
  "ok": true,
  "requested_operation": "correct",
  "applied_operation": "correct",
  "outcome": "committed",
  "old_fact_id": 123,
  "new_fact_id": 456,
  "old_status": "superseded",
  "new_status": "active",
  "conflict_state": "clear",
  "reason_code": "subject_self_correction",
  "deduplicated": false
}
```

后端可以返回不同于请求的实际操作：

```json
{
  "ok": true,
  "requested_operation": "correct",
  "applied_operation": "contest",
  "outcome": "committed_as_contested",
  "reason_code": "third_party_cannot_silently_replace_self_report"
}
```

这不是拒绝 Yuki 的判断，而是准确记录当前认知状态。

## 6. 调用身份与权威

### 6.1 三个角色必须分开

每次变更至少记录：

```text
trigger_actor   谁的消息或任务触发了这次思考
decision_actor  谁决定应当怎样改变记忆
executed_by     哪个 Bot/服务执行了数据库事务
```

示例：群友 A 告诉 Yuki“小明已经搬到上海”。

```text
trigger_actor   = A
decision_actor  = Yuki
executed_by     = Yuki bot instance
subject         = 小明在当前群的 person_group
authority       = third_party
```

### 6.2 两种 Agent 模式

#### 用户委托模式 `delegated`

用户明确表达创建、纠正、删除、恢复或合并意图，Yuki 负责理解并执行。

```text
decision_actor_type = user
delegation_mode = delegated
```

对于本人事实，用户明确纠正应当具有最高可用权威，并可直接创建新版本。

#### Yuki 自主模式 `autonomous`

用户没有直接要求修改，但 Yuki 在检索、回复或后台反思时发现记忆不一致。

```text
decision_actor_type = bot
delegation_mode = autonomous
```

自主模式允许真正提交变更，但必须携带可验证证据；无证据推断默认进入争议或低权威推断状态。

### 6.3 是否增加 `bot_inference`

建议讨论两个方案：

方案 A：增加 `MemoryAuthority.BOT_INFERENCE`，排序低于第三方陈述。

```text
explicit > self_report > group_report > third_party > bot_inference
```

方案 B：不扩展 authority。Yuki 只能综合已有 evidence；无证据推测只生成临时 proposal，不进入
`memory_facts`。

本文倾向方案 B。Yuki 根据真实证据作出决策时，authority 应来自证据而不是来自“Yuki”这个
身份；纯推测不应成为长期事实。

## 7. 普通用户权限建议

### 7.1 推荐矩阵

| 目标 | 普通用户能力 | 证据/权威处理 |
|---|---|---|
| 自己的 `person` | 自由创建、纠正、失效、恢复 | explicit/self_report |
| 自己的 `person_group` | 在当前群自由创建、纠正、失效、恢复 | explicit/self_report |
| 当前群 `group` | 当前群成员可创建、纠正、争议 | group_report；冲突按证据解决 |
| 当前群其他成员的 `person_group` | 可创建、纠正、争议、提出归属修正 | 强制 third_party |
| 其他人的跨群 `person` | 默认不能由普通群友直接改 | 需要当事人自述或更明确的可信上下文 |

### 7.2 放权不等于覆盖来源

普通群成员可以实质影响他人的 `person_group`，但后端必须保留：

- 实际说话者；
- 当前群；
- 原始事件；
- `third_party` authority；
- 与更高权威事实的冲突关系。

如果第三方新说法与当事人本人事实冲突，推荐结果是 `contest`，而不是静默覆盖。后续当事人确认、
多名群友独立支持或更强证据出现时，Yuki 可以自主解决争议。

### 7.3 不要求普通用户知道记忆 ID

用户可以直接说：

```text
你记错了，我现在住上海。
把你记得的“我喜欢可乐”删掉，我早就不喝了。
小明在这个群里大家都叫他老张，不是老王。
我们群已经不玩原神了。
```

Yuki 的执行流程是：检索候选 → 必要时消歧 → 调用 `memory_change` → 用自然语言报告真实结果。

确定性 `/ai memory ...` 命令继续作为模型不可用时的逃生入口，但最终也调用同一个变更服务。

## 8. Yuki 自主修改策略

### 8.1 回复时反思

以下情况可以触发工具调用：

- 当前消息明确否定已召回事实；
- 当前消息提供了明显更新的时间状态；
- 两条召回事实内容相同但作用域或主体可能错误；
- 回复所需事实处于 contested，且当前消息提供了解决证据；
- Yuki 在使用事实前发现它已经过有效期；
- 同一事实槽存在重复或语义等价版本。

### 8.2 后台反思

后台任务可以有界扫描：

- 长期 contested 的事实；
- 同 target、同 key 的版本链异常；
- 高相似度重复事实；
- 疑似错误人物归属；
- 低 confidence、长期未确认的自动事实；
- 已经过期但状态未同步的事实。

反思任务不应无界扫描全部聊天，也不应把 Yuki 自己的回复当作外部证据。它可以调用与聊天
Agent 相同的 `memory_change` 服务，但 `turn_origin` 必须标记为后台反思。

### 8.3 Yuki 自己的回复如何使用

默认规则：

- Yuki 回复不是关于用户事实的正向证据；
- Yuki 可以记录“我曾经这样认为”的元审计，但不能因此提升事实 confidence；
- 用户随后确认 Yuki 的说法时，用户消息才成为新证据；
- 工具或网页结果不能直接成为人物事实，除非未来为外部来源建立独立 provenance 类型。

这可以防止：

```text
Yuki 猜测 → Yuki 说出来 → Yuki 把自己的回复当证据 → 猜测被自我强化
```

## 9. 与自动更新共存

自动抽取不应被 `memory_change` 取代。推荐保留三个入口：

```text
日常消息 → Memory Worker 自动抽取 ┐
用户要求 → Agent memory_change     ├→ MemoryMutationService → MemoryFactService
Yuki 反思 → Agent memory_change    ┘
```

三个入口共享：

- `SubjectResolver` / `MemoryTargetResolver`；
- claim validation；
- candidate resolution；
- `MemoryResolutionPolicy`；
- versioning、evidence 和 state events；
- embedding 调度；
- mutation receipt 与幂等规则。

任何入口都不能直接绕过服务修改 `memory_facts`。

## 10. 防止双写

### 10.1 双写来源

同一条消息可能被以下路径同时观察：

- 自动 Memory Worker；
- Yuki 即时调用 `memory_change`；
- `/ai memory ...` 确定性命令；
- 插件记忆 Facade；
- 管理员 Action；
- 后台反思任务。

仅靠随机 `memory_key` 或数据库行 ID 无法防止语义重复。

### 10.2 变更指纹

建议生成后端变更指纹：

```text
sha256(
  trigger_event_id
  + scope_type
  + resolved_subject_user_id
  + resolved_group_id
  + kind
  + normalized_memory_key
  + requested_operation
  + normalized_content
)
```

说明：

- 同一事件可以产生多条不同事实，因此不能只按 `event_id` 去重；
- 相同事件、相同目标、相同 key、相同操作和相同内容只能成功提交一次；
- 后续重复入口返回第一次提交的 receipt；
- 同义但文本不同的重复继续交给 candidate resolver 和语义关系分类处理。

### 10.3 Mutation Receipt

建议增加独立的提交回执概念，例如：

```text
memory_mutation_receipts
```

最小字段：

```text
mutation_id
idempotency_key UNIQUE
trigger_event_id NULLABLE
turn_origin
decision_actor_type
decision_actor_id NULLABLE
requested_operation
applied_operation
target_fingerprint
old_fact_id NULLABLE
new_fact_id NULLABLE
outcome
created_at
```

receipt 不是第二套事实库，只用于幂等、审计和故障恢复。

### 10.4 并发保护

- 修改请求携带 `expected_fact_state` 或事实版本号；
- 写事务中重新读取事实；
- 状态已经变化时重新执行 resolution，而不是盲目覆盖；
- receipt、事实变更、证据和 state event 尽量在同一事务提交；
- embedding 调度失败不回滚已经提交的事实，但必须可重试；
- 同一事实槽最多一个 active fact 的数据库约束继续保留。

## 11. 典型流程

### 11.1 用户明确纠正本人事实

```text
旧事实：用户住在福州
用户：不是，我现在已经搬到上海了
```

流程：

1. Yuki 检索到旧事实；
2. 当前事件证明主体是当前用户；
3. Yuki 调用 `memory_change(correct)`；
4. 后端采用 `self_report`，创建上海版本；
5. 福州版本进入 `superseded`；
6. 自动 Worker 稍后处理同一事件时命中 receipt/相同事实，不重复创建；
7. Yuki 回复“好，我更新了这条记忆”。

### 11.2 普通群友纠正另一名群友的 `person_group`

```text
旧事实：小明在本群昵称是老王（小明本人曾确认）
A：小明现在大家都叫他老张了
```

流程：

1. A 必须真实 `@` 小明或回复小明的真实消息；
2. 新证据为 `third_party`；
3. Yuki 可以调用 `correct`；
4. 后端发现旧事实权威更高，将请求应用为 `contest`；
5. 后续小明确认“现在叫老张”时，Yuki 自主解决争议并创建新 active 版本。

### 11.3 更新群共同事实

```text
旧事实：本群每周五组织游戏
群成员：以后改到周六了
```

如果消息语义明确且属于当前群，普通群成员可以直接触发 group correction。若其他群成员随后反对，
系统保留多个证据并进入 contested；Yuki 不需要超级管理员许可才能参与判断。

### 11.4 Yuki 自主发现重复

```text
#10 喜欢爵士乐
#24 最喜欢的音乐类型是 Jazz
```

Yuki 在后台反思中检索到两条高相似事实，读取证据后调用 `merge`。后端合并证据、保留状态事件，
并使重复版本不再同时进入普通上下文。

### 11.5 归属修正

```text
错误事实：A 喜欢摄影
证据原文和回复关系实际指向 B
```

Yuki 调用 `reassign`。后端重新校验原事件中的可信主体引用；验证通过后在 B 的合法作用域创建事实，
并以 `misattributed` reason 使 A 的错误版本失效。验证失败则拒绝归属变化，不能仅凭昵称猜人。

## 12. 与当前代码的建议集成点

### 12.1 Tool Kernel

- 在核心 Agent capability provider 注册 `memory_change`；
- 普通用户轮次和允许的自主轮次均可见；
- 图片、Web、MCP 等不可信外部内容参与当前轮时，可要求证据只能来自真实入站聊天事件；
- Memory Runtime 只决定是否开放 memory write namespace，不决定身份或数据库目标；
- 工具结果必须报告实际 commit 状态，回复层不能把失败描述成成功。

### 12.2 Memory V2

优先复用：

- `MemoryClaimValidator`；
- `MemoryConflictCandidateResolver`；
- `MemoryRelationClassifier`；
- `MemoryResolutionPolicy`；
- `MemoryFactService.apply_claim/correct_fact/invalidate_fact/restore_fact/merge_facts`；
- `memory_fact_state_events`；
- embedding 调度和现有 unique indexes。

建议新增一个比 `MemoryFactService` 更上层的 `MemoryMutationService`，负责：

- 统一不同入口；
- 构造并领取 mutation receipt；
- 解析 delegation/autonomous actor；
- 应用权限和证据策略；
- 将请求转换为现有 claim/resolution 操作；
- 返回统一的 commit result。

### 12.3 旧入口收敛

以下入口最终都应转调 `MemoryMutationService`：

- Memory Worker；
- `/ai memory add/update/delete/correct/invalidate/restore`；
- `admin_execute_action` 中的 memory actions；
- Plugin Memory Facade；
- 新的 `memory_change` Agent 工具；
- 后台反思 Worker。

## 13. 失败和降级策略

| 情况 | 建议行为 |
|---|---|
| 事实不存在或本轮不可见 | 返回 `memory_not_found`，不泄露是否存在 |
| 主体引用无法由后端解析 | 拒绝，不按昵称猜测 |
| 证据不属于允许会话 | 拒绝 |
| 第三方试图冒充本人 explicit | 改为 third_party 或拒绝 |
| 证据不足却请求直接替换高权威事实 | 降级为 contest |
| 相同 mutation 已提交 | 返回原 receipt，`deduplicated=true` |
| 事实并发变化 | 重新 resolution 或返回 `stale_fact_state` |
| 分类模型失败 | 使用保守确定性 fallback |
| embedding 调度失败 | 保留事实提交，记录可重试任务 |
| 数据库事务失败 | 不向用户声称已修改 |

## 14. 可观察性与用户体验

每次变更应能够回答：

- 谁触发了修改；
- 谁作出了决定；
- 修改了哪条事实；
- 使用了什么来源类型的证据；
- 请求动作和实际动作是否不同；
- 为什么进入 superseded、contested 或 invalidated；
- 是否由重复调用命中已有 receipt；
- 是否可以恢复。

普通回复不必暴露内部字段，可以自然表达：

```text
我已经把“住在福州”更新为“现在住在上海”，旧版本仍保留在历史里。
```

或：

```text
我记下了你的说法，但这和小明本人之前说的有冲突，所以暂时标记为有争议。
```

用户仍可通过显式命令查看事实、历史和冲突。

## 15. 分阶段落地建议

### Phase 1：统一写入入口

- 实现 `MemoryMutationService` 和 mutation receipt；
- 让现有自动 Worker、显式命令和管理员 Action 复用它；
- 先解决双写和统一审计，不开放新权限。

### Phase 2：普通用户自然语言修改

- 注册 `memory_change` Core Agent 工具；
- 开放本人 `person/person_group`；
- 开放当前群 `group` 和当前群第三方 `person_group`；
- 保留确定性命令作为备用路径。

### Phase 3：回复时自主纠错

- 允许 Yuki 在当前轮证据充分时自主调用；
- 支持 correct、contest、merge 和 reassign；
- 对无证据推测保持保守。

### Phase 4：后台反思

- 有界领取 contested、重复和疑似归属错误候选；
- 复用同一个工具策略和提交服务；
- 增加健康、指标、重试和停机恢复。

## 16. 测试要求

至少覆盖：

- 同一事件由 Agent 和 Worker 同时处理，只产生一个 active fact；
- `/ai memory add` 与自动抽取并发时幂等；
- 普通用户自然语言纠正本人 person；
- 普通用户纠正本人 person_group；
- 普通群友可以影响当前 group；
- 第三方纠正另一成员 person_group 时不会冒充 self_report；
- 低权威证据不会静默覆盖高权威事实；
- Yuki 自主纠错有证据时成功、无证据时进入 contested/noop；
- `correct` 创建版本而非原地覆盖；
- `reassign` 不允许跨越后端无法验证的身份边界；
- 重复工具调用返回相同 receipt；
- 并发状态变化不会制造两个 active fact；
- 工具成功但索引调度失败时仍能恢复；
- 事务失败后回复层不得声称修改成功；
- Yuki 的 outbound 回复不能成为用户事实证据；
- `forgetme` 和隐私物理删除不被 receipt 阻止。

## 17. 需要重点讨论的问题

请评审者重点回答：

1. 单一 `memory_change` 是否优于多个细粒度工具？
2. `operation` 应由模型直接选择，还是只提交“期望变化”再由后端决定动作？
3. 普通群成员是否应当能够直接修正 `group`，还是默认先进入 contested？
4. 普通群成员对他人 `person_group` 的 `correct` 应当直接产生版本，还是只能产生冲突 claim？
5. Yuki 无外部证据的推断是否应持久化为 `bot_inference`？
6. mutation receipt 是否需要独立表，还是复用 `memory_fact_state_events` 足够？
7. 变更指纹中是否应该包含 `operation`，以便同一事件既能创建又能撤回不同事实？
8. `reassign` 应当作为一等操作，还是拆成 create + invalidate 的原子复合命令？
9. 自主纠错需要多高的证据门槛，是否应按 scope 分别配置？
10. 后台反思应该直接提交，还是先生成 proposal 并在下一次对话中由 Yuki继续判断？
11. 如何在“放权给普通用户”和“保留事实来源真实性”之间保持最少的后端硬限制？
12. 当前 `authority` 是否足够表达决策者与证据来源的区别，还是需要新增独立字段？

## 18. 本文推荐默认答案

- 使用单一 `memory_change` 模型工具和单一 `MemoryMutationService`；
- 模型提交期望动作，后端允许降级为 contest/noop，但不能擅自改写正文；
- 普通群成员可以直接影响 group 和当前群他人的 person_group；
- 第三方来源不能伪装成本人来源，也不能静默覆盖更高权威事实；
- 不持久化无证据 `bot_inference`，只创建 proposal 或 contested 状态；
- 使用独立 mutation receipt，解决跨入口幂等和故障恢复；
- `reassign` 作为一等、原子的领域操作；
- Yuki 回复时可以自主提交，后台反思分阶段开放；
- 所有旧入口必须收敛到同一服务后，再开放新的自主权限。
