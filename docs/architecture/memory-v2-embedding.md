# Memory V2 Embedding 与混合 RAG

## 定位

`3.0.0b1` 在既有 Memory V2 事实库和 FTS5 之上增加可选语义召回；`3.0.0b2` 的冲突治理继续
复用该派生索引。`memory_facts` 仍是唯一
事实源；Embedding profile、向量和任务都是可丢弃、可重建的派生数据。系统不引入向量数据库、
NumPy、LLM rerank 或历史聊天扫描。

## 检索边界

```text
当前问题
  -> Memory Runtime 决定本轮是否预取，以及 lexical / hybrid / overview
  -> 后端解析真实人物/群目标
  -> 每个目标分别执行带 scope/user/group/status/profile 条件的 SQL
  -> FTS/BM25 候选 + 目标内余弦相似候选
  -> 确定性 RRF 融合
  -> 实体分块与上下文预算
```

向量检索不会全库搜索后再猜身份。当前人物、人物在当前群、当前群以及真实 @/回复得到的引用
人物始终独立检索。不同 QQ、不同群或私聊资料不会因为语义相似而越界。

检索深度由 Memory Runtime 会话合同与 Main Agent 的显式 read Tool 决定，不能选择人物、QQ、群或会话范围：

- `none`：无需长期记忆的纯效果或即时短回应；不进入长期记忆检索。
- `lexical`：普通日常交流；只使用本地 FTS/LIKE，不访问 Embedding Provider。
- `hybrid`：人物事实、偏好、模糊指代、较早细节或群关系问题；使用词法与向量候选融合。
- `overview`：显式询问记忆概览；按后端概览规则读取，不生成 query embedding。

相关混合检索会为当前查询构造一次 `query` embedding，并在同一轮所有合法目标间复用。相同
profile 与查询还会通过哈希键在有界进程内缓存中短期复用；原始查询不会因为缓存而写入数据库。
概览查询不调用 Embedding。语义服务不可用时，检索状态标记为 degraded 并继续使用词法候选。

## 文档模板与隐私

文档模板 v1 只包含有界的 `kind`、`category`、`memory_key` 和 `content`。它不包含 QQ 号、
群号、昵称、证据正文、聊天历史、系统提示词、管理员权限或其他人物资料。查询输入同样有长度
上限。日志、健康检查和指标只记录数量、耗时、错误类别、profile 指纹等无正文元数据。

DashScope API Key 仅通过 `MEMORY_EMBEDDING_API_KEY` 读取，不进入 profile、数据库、日志、
命令输出或健康响应。profile 指纹由 provider、端点身份、模型、维度、输出类型、模板版本和
query instruct 等非密钥配置生成。

## 存储与任务

- `memory_embedding_profiles`：不可变配置指纹及非密钥能力信息。
- `memory_embeddings`：`fact_id + profile_id` 唯一，保存内容哈希、维度和 little-endian
  float32 BLOB。
- `memory_embedding_jobs`：持久化 pending/running/retry/failed/done 状态、租约、尝试次数与
  无正文错误类别。

事实写入事务提交后才排队。启动时只协调当前 active facts 与当前 profile，不读取聊天历史。
文档内容变化会产生新哈希并重新生成；事实删除通过外键级联删除向量和任务。模型或模板配置
变化会建立新 profile，旧 profile 数据保持隔离，直到管理员显式清理。

3.0.0b2 的 correction 会创建新 fact，因此生成新的 FTS row 和 Embedding job；旧版本进入
superseded 后不会参与普通语义检索。只改变 authority、confidence、conflict_state 或
last_confirmed_at 不改变文档正文，不会重复向量化。Embedding 故障也不会阻断修正、证据聚合或
状态事件事务。

## 配置

默认保持关闭：

```dotenv
MEMORY_EMBEDDING_ENABLED=false
MEMORY_EMBEDDING_PROVIDER=qwen_dashscope
MEMORY_EMBEDDING_BASE_URL=
MEMORY_EMBEDDING_API_KEY=
MEMORY_EMBEDDING_MODEL=qwen3.7-text-embedding
MEMORY_EMBEDDING_DIMENSIONS=1024
MEMORY_EMBEDDING_OUTPUT_TYPE=dense
MEMORY_EMBEDDING_DOCUMENT_TEMPLATE_VERSION=1
MEMORY_EMBEDDING_QUERY_INSTRUCT=Retrieve personal memory facts relevant to the conversational query.
MEMORY_EMBEDDING_REQUEST_TIMEOUT_SECONDS=20
MEMORY_EMBEDDING_MAX_TEXT_CHARACTERS=4000
MEMORY_EMBEDDING_WORKER_ENABLED=true
MEMORY_EMBEDDING_WORKER_INTERVAL_SECONDS=5
MEMORY_EMBEDDING_WORKER_CLAIM_LIMIT=100
MEMORY_EMBEDDING_RETRY_ATTEMPTS=5
MEMORY_EMBEDDING_RETRY_INITIAL_SECONDS=30
MEMORY_EMBEDDING_HTTP_CONCURRENCY=2
MEMORY_EMBEDDING_QUERY_CACHE_TTL_SECONDS=600
MEMORY_EMBEDDING_QUERY_CACHE_MAX_ENTRIES=512
```

混合检索可热更新：

```dotenv
MEMORY_SEMANTIC_ENABLED=true
MEMORY_SEMANTIC_CANDIDATE_LIMIT=50
MEMORY_SEMANTIC_MIN_SIMILARITY=0.35
MEMORY_HYBRID_LEXICAL_WEIGHT=1.0
MEMORY_HYBRID_SEMANTIC_WEIGHT=1.0
MEMORY_HYBRID_RRF_K=60
```

只有 `MEMORY_EMBEDDING_ENABLED=true` 时才要求 base URL 和 API Key。当前实现只接受
`qwen_dashscope`、dense 与 1024 维，避免 profile 声明和真实向量不一致。
查询缓存只存在于 Bot 进程内，重启即清空；TTL 和容量是启动配置，不影响数据库 schema。

## 运维命令

```text
/ai memory embedding status
/ai memory embedding doctor
/ai memory embedding retry
/ai memory embedding rebuild
/ai memory embedding purge-old
```

- `status`：查看开关、当前 profile、覆盖率和任务计数。
- `doctor`：用固定无隐私测试文本执行一次 Provider 远程连通性与维度检查。
- `retry`：把当前 profile 可重试的失败任务重新排队。
- `rebuild`：为当前 active facts 建立当前 profile 的任务，不修改事实或 FTS。
- `purge-old`：删除非当前 profile 的旧向量、任务和 profile。

部署时先备份 `data/`，执行 `uv run alembic upgrade head`，再只重建 Bot：

```bash
docker compose up -d --build --no-deps bot
```

NapCat 容器与 QQ 登录态无需重建。外部 API 故障不会令健康检查主动访问网络，也不会阻止 Bot
启动；可通过 status/doctor 和不含正文的计数判断积压，再在恢复后执行 retry。
