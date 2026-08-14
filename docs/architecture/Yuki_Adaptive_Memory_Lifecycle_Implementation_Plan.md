# Yuki 自适应记忆生命周期实施计划

> 状态：已按 `codex/adaptive-memory-lifecycle` 的真实代码更新。记忆使用归因采用发送后的异步
> Flash 判断，Agent 不参与自报。

## 核心合同

- `MemoryQueryIntent` 由 Planner 在现有调用中产生；后端继续独占身份、群和可见性解析。
- `MemoryActivationState` 与事实真实性字段分离，读取时执行 Lazy Decay。
- `MemoryRecallReceipt` / `MemoryRecallItem` 只保存候选、选择、注入、使用、强化阶段和数值分数。
- `MemoryExposure` 只在本轮进程内保存实际向最终 Agent 呈现的 ref、事实内容和来源。
- `MemoryAttributionOutput` 只返回 Exposure 白名单内的 `used_refs`，不返回自由文本推理。

## 检索与生命周期

- 每个合法目标内执行 `FTS / Semantic → RRF → Intent/Activation Rerank → MMR → Budget`。
- Retriever 保持纯读取；最终预算完成后才更新 `last_injected_at` 和 Receipt injected 阶段。
- Activation 按 Episode 14 天、Fact 60 天、Preference 120 天、Explicit 365 天半衰期惰性计算。
- 只有后台归因确认 used 的事实才按 Purpose α 执行 Activation CAS 强化。
- `correct` 可以记录 used，但 α 固定为 0；真实性修正仍由 Mutation/Conflict 处理。

## 异步使用归因

1. ContextAssembler 从最终 metadata 预算中提取自动 Exposure。
2. 记忆工具从实际返回给 Agent 的 payload 中追加工具 Exposure。
3. Native Web fallback 每次 Agent 运行使用独立 Exposure Registry，只保留最终运行。
4. Agent 直接生成正文，不增加归因工具调用或 continuation。
5. 完整正文或对应语音获得发送回执后，主链使用 `put_nowait()` 投递内存任务。
6. 后台 Flash 根据用户问题、最终正文和 Exposure 判断实质使用的 refs。
7. Receipt 再次校验 refs 必须属于本轮 injected 白名单，然后标记 used 并执行 Activation CAS。

后台归因队列默认上限 128、TTL 120 秒、Flash 超时 12 秒、候选最多 32 条、输入总预算
24,000 字符。任意新的前台模型请求都可以抢占尚在推理阶段的归因；抢占、超时、非法输出、队列满
和重启都不重试，也不强化。问题、回复和记忆正文不进入 Receipt、数据库或日志。

## 配置与迁移

- `memory.usage_attribution_enabled`：异步归因总开关，默认开启。
- `memory.usage_attribution_timeout_seconds`：默认 12。
- `memory.usage_attribution_job_ttl_seconds`：默认 120。
- `memory.usage_attribution_queue_limit`：默认 128。
- `ModelTask.MEMORY_ATTRIBUTION` 显式路由至 Flash；旧 profile 文件仅做模型路由兼容。
- `0036` 只迁移后台配置覆盖键，不创建归因任务表或正文存储。

## 验收

- 覆盖直接引用、改写、核验、推论、共同经历、多候选和零使用语句。
- 非法、重复、跨轮或未 injected refs 不得进入 used。
- 发送失败、取消、中断、纯表情、空回复和 Plugin Background 不投递归因。
- 新前台请求抢占归因后，Receipt 保持 injected，Activation 不变。
- 无 Exposure 的轮次不调用归因模型；符合条件的轮次最多一次后台逻辑调用。
- 主响应路径不等待 Flash、Receipt 或 Activation。
- 通过 ruff、mypy、全量 pytest、Memory Quality release check 和旧自报符号清零检查。

## 自动召回与 Memory Scope 互斥（2026-08-15）

- `MemoryContextPlan.access` 是首轮记忆访问的唯一编排依据：`none` 不读取也不开放工具，
  `automatic` 自动召回且移除首轮 Memory Scope，`tool` 跳过自动召回并开放 Memory Scope。
- `request_tools` 仍可从当前真实权限目录加载记忆工具，作为修改请求和窄定位失败后的显式降级；
  它不改变首轮互斥语义。
- 自动召回每目标最多 4 条；整轮 `background/continuation/focused/overview` 默认分别最多
  3/4/6/8 条。overview 有 `requested_count` 时采用 `min(requested_count + 2, 8)`。
- 全局限额在目标内 Intent/Activation rerank 与 MMR 后执行；精确命中和显式偏好保留位优先，
  Context Budget 使用内部重排分数，但最终 Agent 上下文不暴露内部评分。
- `memory_change` 没有 fact ID 时可使用 `selector`；后端只在已解析的精确 target、允许状态且
  非 quarantined 的事实中执行无 Embedding 定位。只有唯一精确命中才写入，否则返回至多 3 条
  词法候选或 `memory_candidate_not_found`。`merge` 同样支持 `merge_selector`。
