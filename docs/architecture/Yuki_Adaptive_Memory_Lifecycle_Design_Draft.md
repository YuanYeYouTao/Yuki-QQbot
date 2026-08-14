# Yuki Adaptive Memory Lifecycle 设计稿（已按代码核对）

> 目标：强化 Yuki 现有自动记忆系统，使其从“自动形成与整理记忆”升级为完整的自适应记忆生命周期。

> 状态：2026-08-14 已对照 `main@23945cd` 的 Planner、MemoryQueryBuilder、RRF/MMR、
> ContextAssembler、AgentRunner、Plugin API、SQLite 模型与迁移链完成核对。本文中的“语义意图”均来自
> Planner 的当前消息、最近十条有界历史以及可信回复/提及元数据；它不表示 Planner 可以看到完整历史，
> 也不表示 Planner 可以授予人物、QQ 或群作用域权限。

## 代码核对后的约束修正

- `MemoryQueryIntent.subjects` 只表达 `current_person/current_group/referenced_person/current_self`
  等语义角色，并且只参与软排序。真实目标仍由后端根据发送者、当前群、@、回复关系、群成员资格和
  SELF 可见性解析；Planner 输出不能扩大目标集合。
- 现有 `TurnPlan.intent` 是通用工具/Agent 意图，不再拼入记忆 Query。记忆检索只消费专用的
  `MemoryQueryIntent`。
- 删除通过固定短语把请求改判为 `overview` 的实现。Regex 仍可用于 NFKC/空白规范化、FTS 转义等
  字符串处理，但不能参与 Recall Intent 分类。
- 当前 `last_used_at` 的真实含义是最终预算后的 Context 注入时间；迁移后统一命名为
  `last_injected_at`，历史值不能作为真正 Recall 或强化证据。
- Agent 不承担记忆使用自报。最终正文或对应语音获得发送回执后，由可抢占的后台 Flash 任务
  基于本轮问题、实际发送正文和实际呈现的 Exposure 判断真正使用；只有白名单内的 refs 才能进入强化。
- 没有 Planner 轮次的 Plugin/Admin 显式搜索保持中性、只读和 Plugin API 1.0 兼容。

---

## 1. 背景

当前 Yuki 的自动记忆系统已经能够完成：

```text
发生对话
   ↓
自动提取
   ↓
Fact / Preference / Episode
   ↓
冲突治理 / Evidence / Dream
```

这一套机制已经较完整地解决了：

- 什么内容值得形成长期记忆；
- 记忆属于谁、属于哪个作用域；
- 记忆的来源和 Evidence 是什么；
- 新旧事实冲突时如何处理；
- 长期记忆如何通过 Dream 进行整理、合并、重组和修正。

但这仍然主要是“形成记忆”和“维护记忆内容”。

下一阶段需要补齐的是：

> 记忆形成之后，如何随着时间自然淡化；如何根据当前语境被重新唤起；如何区分“被检索到”“被注入上下文”和“真正被 Agent 使用”；以及真正被使用后如何重新强化。

因此下一阶段不应把“对话意图”“自然遗忘”“记忆强化”拆成互不相关的功能，而应一次完成一个完整的 **Adaptive Memory Lifecycle**。

---

## 2. 核心目标

下一阶段的自动记忆生命周期应形成：

```text
形成记忆
   ↓
自然衰减
   ↓
根据当前语境被唤起
   ↓
进入 Agent 上下文
   ↓
判断是否真正使用
   ↓
重新强化
   ↓
长期未再次使用则继续淡化
   ↓
必要时 Dream 重组 / 修正
   ↓
再次参与未来 Recall
```

可以概括为：

\[
\text{Formation}
\rightarrow
\text{Decay}
\rightarrow
\text{Contextual Recall}
\rightarrow
\text{Actual Use}
\rightarrow
\text{Reinforcement}
\rightarrow
\text{Decay}
\rightarrow
\text{Consolidation}
\]

本阶段主要由四个部分组成：

1. **Structured Recall Intent**
2. **Natural Decay / Activation**
3. **Usage Attribution**
4. **Reinforcement**

---

# 3. 最重要的架构原则：意图判断全部交给 Planner

## 3.1 禁止使用正则表达式、关键词表或启发式规则判断对话意图

本阶段必须明确：

> **所有对话意图、回忆目的、实体线索、时间线索和记忆类型倾向，都由 Planner 基于当前消息、最近十条有界历史及可信回复/提及元数据进行语义理解。**

禁止在 Memory Retriever、ContextAssembler 或其他下游模块中通过以下方式重新判断意图：

```text
regex
关键词匹配
if "还记得" in message
if "之前" in message
固定短语表
硬编码 pattern
```

例如不能写：

```python
if re.search(r"还记得|以前|上次|之前", message):
    purpose = "recall"
```

因为真实对话中的回忆意图并不一定包含这些词。

例如：

```text
“还是老样子吧”
“那个人后来怎么样了”
“你当时不是这么说的”
“那个事我现在想起来还是很好笑”
“麦当劳那次你还挺离谱的”
```

这些都可能需要历史 Memory，但无法可靠依赖关键词或正则判断。

反过来：

```text
“我之前看到有人说……”
```

出现“之前”也不意味着需要检索个人长期记忆。

因此：

> **意图识别是语义理解问题，不是字符串分类问题。**

## 3.2 Planner 是唯一的 Recall Intent Provider

当前 Planner 已经可以输出：

```text
none
lexical
hybrid
overview
```

并同时输出若干 Memory reason codes。

说明 Planner 已经具备一定程度的 Memory 需求判断。

本阶段不推翻这一设计，而是在现有结果上扩展结构化 Recall Intent。

建议形成：

```text
MemoryQueryIntent
│
├─ mode
│   └─ none / lexical / hybrid / overview
│
├─ purpose
│   ├─ background
│   ├─ recall
│   ├─ continuation
│   ├─ verify
│   └─ correct
│
├─ subjects
│   └─ 当前涉及的 person / group / self
│
├─ entities
│   └─ 人、地点、事件、事物、主题等
│
├─ temporal_hint
│   ├─ unspecified
│   ├─ recent
│   ├─ historical
│   └─ explicit / relative range
│
├─ preferred_kinds
│   └─ fact / preference / episode
│
└─ reason_codes
```

这里的字段均由 Planner 根据当前消息、最近 10 条有界历史和可信回复/提及元数据进行语义判断，
Planner 不读取完整历史。

## 3.3 Memory 系统只消费结构化意图，不负责重新猜意图

未来边界应该是：

```text
Conversation
     ↓
Planner
     ↓
MemoryQueryIntent
     ↓
Memory Engine
```

Memory Engine 不应该再次分析：

```text
“这句话是不是回忆？”
“是不是在问以前？”
“是不是要找 Episode？”
```

它只应该根据 Planner 已经给出的结构化结果执行：

```text
检索范围
召回模式
实体匹配
时间匹配
Memory Kind 偏好
Activation 调整
```

因此应保持原则：

> **Planner 负责理解；Memory Engine 负责执行。**

这样未来 Memory 系统抽离为独立仓库后，也不需要依赖 QQ、NoneBot 或具体 Planner 实现，只需要接受统一的 `MemoryQueryIntent`。

---

# 4. 当前必须修正的语义问题：`mark_used()` 并不等于真正使用

当前 ContextAssembler 在 Memory 被加入上下文后，会调用：

```text
mark_used()
```

并更新：

```text
last_used_at
```

但这个事件实际只意味着：

> Memory 已被注入 Agent Context。

它并不能证明：

> Agent 的最终回答真正使用了这条 Memory。

例如：

```text
Retriever 找到：
A B C D E

最终选择：
A B C

进入 Context：
A B C

真正影响回答：
A
```

当前语义容易变成：

```text
A used
B used
C used
```

但正确状态应该是：

```text
A:
retrieved  yes
selected   yes
injected   yes
used       yes
reinforced yes

B:
retrieved  yes
selected   yes
injected   yes
used       no

C:
retrieved  yes
selected   yes
injected   yes
used       no
```

这一边界必须在实现自然强化之前修正。

否则会产生：

```text
某条 Memory 本来比较容易被召回
             ↓
经常被注入 Context
             ↓
每次都被 mark_used()
             ↓
被不断强化
             ↓
Activation 越来越高
             ↓
以后更加容易被召回
```

最终形成错误的正反馈循环。

---

# 5. 建立完整 Recall 生命周期

本阶段应该正式区分：

```text
retrieved
    ↓
selected
    ↓
injected
    ↓
used
    ↓
reinforced
```

## 5.1 retrieved

表示 Memory 进入检索候选集合，例如进入 FTS / Semantic / RRF candidate pool。

**不产生任何 Memory 状态变化。**

## 5.2 selected

表示 Memory 通过排序和 MMR 后，被选为本轮准备提供给 Agent 的 Memory。

**仍然不强化。**

## 5.3 injected

表示 Memory 实际进入 Agent Context。

这才是当前 `mark_used()` 真正对应的语义。

如果需要记录，应使用类似：

```text
last_injected_at
```

而不是 `last_used_at`。

## 5.4 used

表示本轮回复或推理真正依赖了这条 Memory。

这是一个比 `injected` 更严格的事件。

只有进入 `used` 状态，才有资格触发后续 Reinforcement。

## 5.5 reinforced

表示已经根据本次真实 Recall 对 Memory Activation 完成强化写入。

最终形成：

```text
retrieved ≠ selected
selected ≠ injected
injected ≠ used
used ≠ reinforced
```

各阶段必须保持语义独立。

---

# 6. Read Path 与 Reinforcement Path 必须分离

## 6.1 Read Path

Retriever 只负责：

```text
MemoryQueryIntent
      ↓
Retrieve
      ↓
计算当前 Effective Activation
      ↓
Context-aware Ranking
      ↓
返回 RetrievalResult
```

Retriever 不允许更新：

```text
activation
last_recalled_at
recall_count
```

即 `retrieve()` 必须保持 side-effect free。

## 6.2 Reinforcement Path

只有 Agent 完成本轮回复以后，才进入：

```text
Agent Response
      ↓
Post-delivery Attribution
      ↓
Reinforcement Service
      ↓
Activation Update
```

因此：

```text
READ PATH
≠
WRITE / REINFORCEMENT PATH
```

这样可以避免：

> 因为 Retriever 自己搜到了某条 Memory，所以这条 Memory 越来越强。

---

# 7. 自然遗忘：Activation 与事实有效性分离

下一阶段需要引入独立的 Memory Activation State。

它回答：

> 这条 Memory 当前有多容易被 Yuki 自然想起来？

而不是：

> 这条 Memory 是否真实、可信或者仍然有效？

必须保持：

\[
\text{Validity} \neq \text{Activation}
\]

## 7.1 Epistemic State

继续由现有系统负责：

```text
confidence
authority
status
conflict
valid_from
valid_until
Evidence
```

回答：

> “这件事情为什么可信？”
>
> “现在是否仍然成立？”

## 7.2 Activation State

新增：

```text
activation
activation_updated_at
last_recalled_at
recall_count
```

回答：

> “Yuki 现在有多容易想起它？”

例如：

```text
用户五年前去过北京
```

可以是：

```text
confidence = 1.0
valid = true
activation = 0.05
```

意味着事实完全成立，但平时不会无缘无故浮现。

如果用户问：

```text
“我以前去过哪些城市？”
```

高度匹配的语义和时间线索仍然能够把它重新唤醒。

---

# 8. Natural Decay 设计

第一版使用简单、可解释的指数衰减即可。

假设数据库保存的 Activation 为：

\[
A_0
\]

上次更新时间为 \(t_0\)，当前时间为 \(t\)，则：

\[
A(t)=A_0e^{-\lambda(t-t_0)}
\]

其中：

- \(A(t)\)：当前有效 Activation；
- \(\lambda\)：衰减速度；
- \(t-t_0\)：从上次 Activation 更新开始经过的时间。

## 8.1 使用 Lazy Decay

不需要后台 Worker 定期执行：

```sql
UPDATE memories SET activation = ...
```

数据库只保存：

```text
activation
activation_updated_at
```

读取时临时计算当前有效值。

只有真正发生强化时才写数据库。

---

# 9. 不同 Memory 使用不同衰减速度

第一版可以根据 Memory 类型和性质设置不同 \(\lambda\)。

例如：

```text
explicit durable fact
→ 极慢

长期稳定 preference
→ 很慢

普通 preference
→ 慢

important episode
→ 中等

ordinary episode
→ 较快

automatic low-confidence memory
→ 快
```

但必须严格注意：

> Decay 只改变 Recall Accessibility。

不能：

```text
Activation 很低
→ 自动 invalidated
```

也不能：

```text
Activation 很低
→ 删除 Evidence
```

---

# 10. Activation 不能成为硬过滤条件

禁止：

```text
if activation < threshold:
    discard(memory)
```

也不建议简单使用：

\[
Score = R_{\text{base}} \times Activation
\]

因为当 Activation 接近 0 时，会让非常旧但高度相关的 Memory 几乎无法重新出现。

更合理的是把 Activation 作为一个温和的 Ranking Feature：

\[
R(m)
=
R_{\text{base}}
+w_pP
+w_eE
+w_tT
+w_kK
+w_aA
\]

其中：

- \(R_{\text{base}}\)：现有 Lexical / Semantic / RRF 分数；
- \(P\)：Recall Purpose 匹配；
- \(E\)：Entity 匹配；
- \(T\)：Temporal Hint 匹配；
- \(K\)：Memory Kind 匹配；
- \(A\)：当前 Effective Activation。

这样可以形成：

```text
鲜活 Memory
→ 普通对话里更容易自然出现

已经淡忘的 Memory
→ 普通场景不容易出现

用户给出非常精确的线索
→ 依然可以被重新唤醒
```

---

# 11. Context-aware Retrieval

现有流程：

```text
Scope Filter
   ↓
FTS + Embedding
   ↓
RRF
   ↓
MMR
   ↓
Context
```

下一阶段建议变为：

```text
Planner
   ↓
MemoryQueryIntent
   ↓
Scope Filter
   ↓
FTS + Embedding
   ↓
RRF
   ↓
Context-aware Rerank
├─ purpose match
├─ subject match
├─ entity match
├─ temporal match
├─ kind match
└─ activation
   ↓
MMR
   ↓
selected memories
   ↓
ContextAssembler
   ↓
injected memories
```

其中 Intent、Entity、Temporal Hint 等全部来自 Planner 的语义判断。

**Memory Retriever 不允许通过 Regex、关键词、固定短语表重新生成或修正这些信息。**

如果 Planner 输出不够准确，应改进 Planner 的 Prompt、Schema 或语义推理，而不是在下游增加字符串规则兜底。

---

# 12. 真正使用后的 Reinforcement

设当前 Memory 在衰减后的 Activation 为：

\[
A^-
\]

真正发生 Recall 后：

\[
A^+
=
A^-+\alpha(1-A^-)
\]

其中：

- \(A^-\)：强化前当前有效 Activation；
- \(A^+\)：强化后的 Activation；
- \(\alpha\)：本次 Recall 的强化程度。

这个公式天然有上限，不会无限增长。

## 12.1 强化后继续自然衰减

完成强化：

```text
activation = A+
activation_updated_at = now
last_recalled_at = now
recall_count += 1
```

此后如果长期不再使用：

\[
A(t)=A^+e^{-\lambda\Delta t}
\]

继续淡化。

最终形成：

```text
淡化
 ↓
被重新唤起
 ↓
强化
 ↓
再次淡化
 ↓
再次 Recall
 ↓
再次强化
```

即：

> **遗忘—唤起—再巩固—再次遗忘。**

---

# 13. Recall Intent 可以影响强化程度

由于本阶段已经由 Planner 提供结构化 Recall Purpose，因此强化程度可以根据语义意图调整。

例如：

```text
background
→ 弱强化

continuation
→ 中等强化

recall
→ 较强强化

用户显式确认某段共同经历
→ 强强化
```

因此：

\[
\alpha=f(\text{Recall Intent})
\]

后续还可以考虑间隔效应：

```text
刚刚才想起过
→ 再次强化较弱

长期未想起后成功 Recall
→ 强化更明显
```

第一版应保持简单，可以先设置有限等级。

---

# 14. 如何判断真正的 `used`

这是本阶段最谨慎的部分。

原则：

> **宁愿少强化，也不要错误强化。**

绝不能继续采用：

```text
injected == used
```

## 14.1 Asynchronous Usage Attribution

所有注入上下文的 Memory 都携带稳定内部 ID，例如：

```text
M312
M481
M602
```

最终 Agent 运行期间，系统只在内存中登记实际进入上下文或由记忆工具实际返回的 Exposure：

```text
MemoryExposure(memory_ref=M312, source=automatic)
MemoryExposure(memory_ref=M481, source=agent_tool)
```

Agent 直接生成正文，不调用任何归因工具。完整正文或由该正文生成的语音成功发送后，主链仅执行一次
非阻塞内存入队；后台 Flash 根据本轮用户问题、最终发送正文和 Exposure 判断哪些 refs 构成实质依赖。
后台归因可以被新的前台模型请求抢占；抢占、超时、非法输出和进程重启均按未归因处理，不做强化。

系统内部得到：

```text
MemoryAttributionOutput(used_refs=[M312])
```

然后：

```text
M312
→ reinforce

M481
→ no reinforcement

M602
→ no reinforcement
```

这作为当前 `used` 的唯一模型语义来源；后端不根据正文关键词猜测依赖关系。

## 14.2 Planner Recall Intent 可提供额外语义依据

对于强 Recall 场景，例如 Planner 已明确判断：

```text
purpose = recall
```

并且某条 Episode 与 Planner 提供的：

```text
subjects
entities
temporal_hint
```

高度吻合，这可以成为较强的 Recall Evidence。

但仍应避免简单：

```text
purpose == recall
→ 所有 injected Memory 全强化
```

仍需要确定真正对应的 Memory。

---

# 15. 修正当前 `last_used_at`

当前 `last_used_at` 实际更接近：

```text
last_injected_at
```

本阶段应该重新整理字段语义。

建议：

```text
last_injected_at
```

表示最近一次进入 Agent Context。

而：

```text
last_recalled_at
```

表示最近一次被确认真正使用并进入 Reinforcement。

旧数据无法可靠证明过去是否真正 Recall，因此历史 `last_used_at` 不能直接当作高质量 Recall Evidence，可以视为：

```text
historical injection signal
```

同时必须审计当前所有依赖 `last_used_at` 的：

```text
Lifecycle
Stale Detection
Ranking
Maintenance
Metrics
```

避免字段语义改变后影响现有生命周期逻辑。

---

# 16. Recall Trace / Receipt

建议增加统一的 Recall Trace，例如：

```text
MemoryRecallReceipt
```

可以包含：

```text
turn_id
intent

candidate memories
├─ memory_id
├─ lexical_score
├─ semantic_score
├─ rrf_score
├─ purpose_score
├─ entity_score
├─ temporal_score
├─ activation_score
├─ selected
├─ injected
├─ used
└─ reinforced
```

主要用于：

### Explainability

可以回答：

```text
为什么这条 Memory 被想起来？
为什么另一条没有被想起来？
为什么这次发生了强化？
```

### Debugging

未来调整：

```text
intent weight
entity weight
temporal weight
activation weight
```

时，可以离线对历史 Recall 进行 replay。

不一定需要永久保存所有完整 candidate pool，可以根据日志级别控制保存范围和保留周期。

---

# 17. Dream、Lifecycle、Evidence 与 Activation 的职责边界

下一阶段不能把这些机制混在一起。

```text
Formation
→ 我形成了什么 Memory

Activation Decay
→ Memory 当前有多鲜活

Contextual Recall
→ 当前为什么应该想起它

Reinforcement
→ 真正想起后，让它重新鲜活

Dream
→ 已有 Memory 应该如何整理、重组、修正

Lifecycle
→ 事实现在是否仍然有效

Evidence
→ 为什么相信这条 Memory
```

因此：

- Decay 不负责把旧 Fact 宣布失效；
- Dream 不负责日常 Activation 衰减；
- Retriever 不负责修改 Activation；
- ContextAssembler 不负责把 injected 自动认定为 used。

---

# 18. 完成后的完整自动记忆系统

最终 Yuki 的 Memory 不再只是：

```text
Conversation
   ↓
Extract
   ↓
Store
```

而形成完整闭环：

```text
                         Raw Event
                            ↓
                    Memory Formation
                            ↓
              Fact / Preference / Episode
                            ↓
                 Evidence / Governance
                            ↓
                      Long-term Memory
                            │
          ┌─────────────────┼──────────────────┐
          │                 │                  │
          ↓                 ↓                  ↓
        Decay             Dream            Lifecycle
     Activation↓        Reorganize        Validity
          │                 │                  │
          └─────────────────┼──────────────────┘
                            ↓
                      Memory Store
                            ↓
                          Planner
                            ↓
             Semantic Recall Intent
                            ↓
                   Contextual Retrieval
                            ↓
                  selected / injected
                            ↓
                           Agent
                            ↓
                     Actual Usage
                            ↓
                    Usage Attribution
                            ↓
                     Reinforcement
                            │
                            └──────────→ Memory
```

最终 Memory 将从：

> 静态数据库中的长期信息

变成：

> **随时间、语境和真实交互不断变化的长期认知状态。**

---

# 19. 本阶段开发任务

建议本阶段一次打通以下闭环：

1. **修复 `injected / used` 语义边界。**
2. 停止 ContextAssembler 在注入 Memory 后直接把它记为真正使用。
3. 审计当前 `last_used_at` 的全部使用点。
4. 在现有 Planner `none / lexical / hybrid / overview + reason_codes` 基础上增加结构化 `MemoryQueryIntent`。
5. **所有 Recall Intent、Entity、Temporal Hint、Kind Preference 均由 Planner 语义理解产生。**
6. **禁止 Regex、关键词列表、固定短语规则参与意图判断。**
7. Memory Retriever 只消费 Planner 的结构化意图。
8. 增加 Context-aware Reranking。
9. 增加独立 Activation State。
10. 实现 Lazy Exponential Decay。
11. Activation 作为温和 Ranking Feature，而非硬过滤器。
12. 建立 `retrieved → selected → injected → used → reinforced` 生命周期。
13. 发送成功后由后台 Flash 返回白名单约束的 `MemoryAttributionOutput`。
14. 只有真正 `used` 的 Memory 才进入 Reinforcement Path。
15. 实现 Activation Reinforcement。
16. Recall Intent 可以影响 Reinforcement 强度。
17. 增加 Recall Trace / Receipt 和必要指标。
18. 保持 Dream、Evidence、Conflict、Lifecycle 的现有职责边界。

---

# 20. 明确不做

本阶段不做：

```text
Procedural Memory
Learned Memory Policy / RL
情绪 valence / arousal 系统
复杂认知图
后台定时全库 Decay
低 Activation 自动删除 Memory
全面 Memory V3 重构
Regex / Keyword-based Intent Detection
```

特别强调：

> **不得为了“方便”在 Planner 之外重新加入 Regex、关键词匹配或固定短语表进行 Recall Intent 判断。**

如果 Planner 的结构化意图不足，应改进 Planner 的语义输出、Prompt 或 Schema，而不是在下游增加字符串规则兜底。

---

# 21. 面向未来独立仓库的接口边界

Memory 系统成熟后计划从 Yuki 中抽离为独立仓库，因此本阶段应该开始建立清晰边界。

未来核心接口应接近：

```text
Agent / Yuki
     │
     │ MemoryQueryIntent
     ▼
┌────────────────────────┐
│      Memory Engine     │
│                        │
│ retrieval              │
│ activation             │
│ reinforcement          │
│ consolidation          │
│ evidence               │
│ lifecycle              │
└───────────┬────────────┘
            │
            │ MemoryRetrievalResult
            ▼
          Agent
            │
            │ delivered reply + MemoryExposure
            ▼
      Async Attribution
            │ used refs
            ▼
      Reinforcement
```

Memory Engine 不应该依赖：

```text
QQ
NoneBot
具体 Planner 实现
具体 Agent 实现
Regex Intent Detector
```

只依赖明确的数据契约：

```text
MemoryQueryIntent
MemoryRetrievalResult
MemoryExposure
MemoryAttributionOutput
```

这样未来才能自然抽成独立记忆引擎。

---

# 22. 写入路径与读取路径的职责分离

`MemoryAccessMode` 使用四条互斥路径：

```text
none       不访问长期记忆
automatic  后端自动召回；首轮不暴露 Memory Scope
tool       不自动召回；显式开放记忆读取路径
mutation   不自动召回；首轮只开放获授权的 memory/write_state 能力
```

创建、纠正、撤回和恢复不是“先召回一些内容再猜是否需要修改”的读取问题，而是独立的终端写入
操作。Planner 只负责选择 `mutation + mode=none`，后端再按真实 origin、权限、风险和 capability
metadata 选择写能力；不得按工具名、正则或用户措辞建立另一套路由规则。locator 无法唯一命中时，
Agent 可以通过 `request_tools` 显式加载读取工具缩小目标，但任何降级都不能扩大身份和可见性范围。

修改轮次的最终陈述属于后端完成门。模型可以决定要提交的变更，却不能决定变更是否真的成功；
正文必须由最后一次真实工具回执中的 `applied_operation / outcome / reason_code / candidates` 生成。
尤其是 `invalidate` 只表示状态失效并保留审计记录，不能描述为物理删除。

主 Agent 的 mutation 路径通过末尾系统执行契约提示“先调用当前唯一写能力”，但不依赖模型供应商
不支持的强制工具选择字段。DeepSeek Chat Completions 与 Responses 的线上请求体均省略
`tool_choice`；即使模型没有调用工具，后端完成门也只能返回“未执行”，不能补写或伪造回执。

Planner 输出对 `access` 使用结构化联合类型，使非法的 access/mode 组合在模型边界直接失败。除可信
纯表情效果外，Planner 超时、供应商错误或最终输出无效时整轮失败关闭，不启动 Agent、召回或工具，
从而消除“Planner 降级后 Agent 无工具却口头声称写入成功”的假完成路径。

---

# 23. 一句话目标

> **将 Yuki 的自动记忆从“自动形成和整理记忆”升级为完整的自适应记忆生命周期：由 Planner 基于当前消息、有界历史与可信消息元数据承担全部 Recall Intent 的语义理解，Memory 系统不使用 Regex、关键词或固定规则猜测意图；后端继续独占身份与可见性解析；长期记忆随时间自然淡化，根据人物角色、实体、时间和当前语境被重新唤起，严格区分检索、选择、注入、真正使用和强化，仅对真正参与且已成功发送的 Memory 进行再强化，并继续由现有 Evidence、Lifecycle、Conflict 与 Dream 负责真实性治理和长期巩固。**

最终核心循环：

\[
\boxed{
\text{Formation}
\rightarrow
\text{Decay}
\rightarrow
\text{Semantic Contextual Recall}
\rightarrow
\text{Actual Use}
\rightarrow
\text{Reinforcement}
\rightarrow
\text{Decay}
\rightarrow
\text{Dream Consolidation}
}
\]
