# Yuki Adaptive Memory Lifecycle 实施计划

> 基线：`main@23945cd`  
> 分支：`codex/adaptive-memory-lifecycle`  
> 状态：2026-08-14 已完成实现与代码核对；等待人工审阅、提交与合并。

## 1. 范围与边界

本阶段一次交付 Structured Recall Intent、Context-aware Rerank、Lazy Decay、Usage
Attribution、Reinforcement 和 Recall Receipt。Plugin Memory Facade 合同继续保持 1.0；无
Planner 的 Admin/Plugin 搜索保持中性、只读排序。

本阶段不包含 Snowluma 专用适配、独立仓库拆分、WebUI、后台全库 Decay、低 Activation
自动失效或删除、间隔效应、RL、情绪系统和认知图。Dream、Evidence、Conflict、Mutation
以及基于 `last_confirmed_at` 的真实性生命周期不作语义变更。

## 2. 实施阶段

### 阶段 A：合同与 Planner

- 增加 provider-neutral 的 `MemoryQueryIntent`、`MemoryActivationState`、
  `MemoryUsageReport`、`MemoryRecallReceipt` 和 `MemoryRecallItem`。
- 在既有 Planner `memory_context` 内输出 mode、purpose、subjects、entities、temporal 和
  preferred kinds，不增加第二次意图模型调用。
- `temporal.constraint` 默认 `soft`；用户明确要求“只在某段时间”或“范围外不要用”时，
  Planner 输出绝对 `range+strict`，Retriever 在 MMR 和预算前剔除范围外及无事件时间事实。
- `self_recall` 仅作旧输入兼容并映射为 `current_self`；QQ、群、可见性和成员资格仍由后端
  解析，subject 只能参与合法目标内的软排序。
- 删除固定短语 overview 分类，不把通用 `TurnPlan.intent` 拼入记忆查询；Regex 只保留在
  文本规范化与 FTS 安全处理层。

### 阶段 B：0035 存储迁移

- 使用 SQLite 原生 rename 将 `memory_facts.last_used_at` 改为
  `last_injected_at`，避免重建事实表和破坏 FTS。
- 新建一对一 `memory_activation_states`，以及内容无关的
  `memory_recall_receipts` / `memory_recall_items`。
- 按 Explicit 0.95、Preference 0.80、Fact 0.70、Episode 0.65、重要 Episode 0.75
  回填历史事实；新事实在事实创建事务内同步创建 Activation State。
- Receipt 通过外键级联清理，默认保留 30 天；每个目标默认只追踪重排池前 20 条。

### 阶段 C：检索与 Activation

- 固定流水线为 `FTS/Semantic → RRF → Intent/Activation Rerank → MMR → Context Budget`。
- RRF 只以名次归一化后的 `1/log2(rank+1)` 进入加权；subject、entity、temporal、kind
  和 Activation 均使用 0–1 特征。
- preferred kinds 是软加分，不是候选硬过滤；精确 key/content/category 和既有显式偏好
  保留位置继续置顶，Activation 永不作为硬过滤。
- Activation 在读取时指数衰减，不回写数据库。默认半衰期为 Episode 14、Fact 60、
  Preference 120、Explicit 365 天；importance 与低质量倍率按设计组合。
- MMR 使用 rerank score 作为相关度，冗余度仍只比较当前合法身份分区内的向量相似度。

### 阶段 D：注入、归因与强化

- `ContextAssembler` 只在最终预算完成后更新 `last_injected_at`；Agent 记忆工具只在事实
  实际出现在工具结果后标记 injected。Admin/Plugin Facade 搜索保持纯读取。
- Prompt 和记忆工具结果提供 `memory_ref=M<fact_id>`；本地响应控制工具
  `report_memory_usage` 只接受本轮实际呈现过的白名单 refs，不携带最终正文。
- 工具必须单独、至多成功调用一次，并作为最终正文前的最后一次工具调用；它不占业务工具次数，
  实际报告使用时增加一次正文生成请求。Planner 降级、Plugin Background 或没有 refs 时不暴露。
- Agent 可直接返回正文且后端不强制归因、不重试；没有合法报告的轮次不强化，以避免错误强化和
  普通记忆轮次的额外延迟。
- 仅最终 Agent 运行的合法报告可进入 used；Native Web fallback 会重建本地控制状态。
- 至少一段 Agent 正文或由正文生成的语音取得发送回执后才强化。失败、取消、中断、纯表情、
  空正文和非法报告均不强化。
- 强化使用 `A+ = A- + α(1-A-)`，通过 revision CAS 和 Receipt/fact 唯一约束保证并发与
  重试幂等；写前重新确认事实仍 active 且未 quarantined。

### 阶段 E：配置、审计与观测

- 增加 Intent Rerank、Activation Ranking、Usage Reporting、Reinforcement、Receipt 五个
  默认开启的全局热开关。
- 增加四类半衰期、四个非零 α、recent window、Receipt retention 和 trace candidate
  limit 配置。
- 增加固定低基数观测：mode/purpose、五阶段计数、归因合法性、强化跳过原因、Activation
  分桶、缺失 Activation State、Receipt 清理量、重排延迟和额外模型请求数。
- Memory Audit explain 返回当前有效 Activation、最近注入/召回时间、召回次数和最近
  Receipt 摘要；内部 Activation 不进入 Plugin API。

## 3. 迁移与回滚验证

- Fresh DB、0032→0035、0034→0035 以及 0035→0034→0035 往返均纳入自动测试。
- 真实数据库只在副本上验证，禁止直接升级当前运行库。
- 真实副本结果：103 条事实对应 103 条 Activation State；原 87 个
  `last_used_at` 值完整迁为 `last_injected_at`；FTS 仍为 103 行且 3 个触发器均保留。

## 4. 质量门与交付判定

- 全量 pytest：1070 passed、1 skipped；跳过项是需显式启用的线上 embedding 测试。
- `mypy src` 通过；本分支涉及的 40 个 Python 文件通过 `ruff format --check` 与
  `ruff check`。
- Memory Quality：18/18 合成用例、38/38 gates 通过；跨人/跨群污染率保持 0。
- 标准 100 users / 10,000 facts / 100,000 events 场景的 retrieval p95 为
  30.919 ms，未超过基线 31.211 ms，更未触及 1.25 倍门限；模型请求数为 0。
- Fresh 0035 DB 的 `memory release-check` 所有强制项通过；仅保留工作树非空和未运行可选
  real-model benchmark 两项 warning。

全仓字面执行 `ruff format --check .` / `ruff check .` 仍会报告本分支外既有文件和用户保留
的未跟踪 `tmp/`。本阶段不修改这些文件，也不把 `tmp/` 纳入提交。
