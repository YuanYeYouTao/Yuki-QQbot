# Memory V2 检索

## 一条共享检索链

Core 记忆工具、聊天上下文、管理员 search 和 Plugin API v1 `MemoryFacade.search()` 共用同一个
`MemoryRetriever`。调用方只能提供当前问题和后端允许的目标，不能选择 Provider、profile、
原始向量或全库搜索。

`relevant` 模式的顺序固定为：

1. `MemoryQueryBuilder` 规范化当前文字、有界回复摘录和 Memory Runtime / Agent 读工具给出的结构化 intent。
2. `MemoryTargetResolver` 从真实当前事件生成相互独立的人物/群目标。
3. 查询非空且语义开关开启时，生成一次 query embedding。
4. 每个目标分别执行 FTS/短词 LIKE 和当前 profile 的目标内语义搜索。
5. `MemoryRanker` 使用 RRF 融合候选；精确 key/content/category 命中保持优先。
6. 每个目标独立截断，再交给 `ContextBudgeter`；只有最终注入的事实更新 `last_used_at`。

`overview` 模式继续使用有界的结构化事实列表，不调用 Embedding。空查询也不会发送请求。

## RRF 与稳定顺序

每个来源按自己的 rank 参与：

```text
score = lexical_weight / (rrf_k + lexical_rank)
      + semantic_weight / (rrf_k + semantic_rank)
```

词法 BM25 与余弦分数不直接相加。融合分相同时，依次使用 authority、conflict_state、
importance、confidence、updated_at 和 fact_id 作为稳定 tie-break。一个事实同时被两路命中时只返回一次，并保留来源与有限的
selection reason；向量、完整 profile 和内部语义分数不会注入主模型上下文。

普通检索只读取 `status=active`：active + contested conflict 可以作为带不确定标记的首选事实，
`status=contested` 的未采用 claim、superseded 与 invalidated 均不进入普通上下文。冲突关系本身
不会扩大人物/群目标，也不会让检索返回相反事实全文。

## 身份隔离

语义 Repository 的 SQL 必须先匹配 scope、subject QQ、group ID、active/有效期、kind 和当前
profile，之后才将这一个目标的 BLOB 加载到 Python。相似度、query vector 或昵称不能创建或
改变目标。因此即使不同 QQ 或不同群的事实文本和向量完全相同，也不会互相进入候选。

## 部分覆盖与降级

新 profile 渐进生成文档向量。已有向量参与混合召回，尚未完成的事实仍可由完整 FTS 命中。
query Provider 超时、429、5xx、认证或响应验证失败时，本轮只调用一次并退回词法结果；不会
加载全库事实、自动切换模型或重试聊天请求。结果和指标只记录稳定错误类别与 query hash，
不记录查询、事实、QQ、群号或向量正文。
