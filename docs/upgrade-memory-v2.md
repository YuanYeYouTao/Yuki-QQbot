# 升级到 Memory V2（3.0.0）

## 必须理解的不可逆变化

升级会永久删除所有旧人物记忆、群记忆、群内人物记忆、偏好和旧记忆任务。
不会删除聊天事件账本。不会自动重建历史。唯一回退方式是恢复升级前完整数据库备份。

Alembic `0020` 不提供 downgrade，也不迁移、导入或双写旧记忆。升级完成后新记忆库为空，
只有之后收到的真实入站非 Bot 消息才会进入 Memory V2 提取队列。

## 升级步骤

1. 停止 bot 写入，但不必退出或重建 NapCat。
2. 完整复制仓库的 `data/` 到仓库外或带时间戳的备份目录，并确认数据库文件已复制。
3. 拉取代码后对照 `.env.example`；本次没有新增密钥。
4. 执行 `uv run alembic upgrade head`，确认版本为 `0026`。
5. 执行 `docker compose up -d --no-deps --force-recreate bot`，只重建 bot 可保留 NapCat 登录状态。
6. 检查 `/healthz`、bot 日志和一轮私聊/群聊；新记忆应从空库开始产生。

## 3.0.0b1 增量升级

从 `3.0.0a2` 升级时，Alembic `0022` 只增加 Embedding profile、向量和持久任务表，不删除或
改写事实、证据、FTS 与聊天账本。Embedding 默认关闭；启用方式、故障降级和维护命令见
[Embedding 与混合 RAG](architecture/memory-v2-embedding.md)。迁移不会扫描历史聊天。

如果必须回退，请停止服务并恢复升级前的完整数据库备份。不要尝试执行 Alembic downgrade，
也不要把旧表手工复制到 Memory V2。

## 3.0.0b2 增量升级

从 `3.0.0b1` 升级时，Alembic `0023` 会为事实和证据增加 authority、冲突状态、确认时间与
失效原因，并创建事实关系和状态事件表。迁移以现有来源类型确定性回填元数据，不调用 LLM、
Embedding，也不重新解释事实正文；现有 FTS 与 Embedding 派生数据会保留。

```bash
docker compose stop bot
# 先把 data/ 复制到带时间戳的备份目录
uv run alembic upgrade head
uv run alembic current
docker compose up -d --no-deps --force-recreate bot
```

升级后运行 `/ai memory doctor` 与 `/ai memory maintenance status`。没有 contested fact 时，
`0023 → 0022` downgrade 会删除本阶段新增关系/状态表和元数据列，但保留原 facts/evidence；
存在 contested fact 时 downgrade 会拒绝，以免无声丢失冲突语义。

## 3.0.0rc1 增量升级

从 `3.0.0b2` 升级时，Alembic `0024` 只增加历史重建 staging 和 `memory_jobs` receipt 元数据，
不会修改或删除已有 facts、evidence、relations、state events、FTS、Embedding 或 chat_events。

```powershell
docker compose stop bot
Copy-Item data data-backup-before-3.0.0rc1 -Recurse
uv run alembic upgrade head
docker compose up -d --no-deps --force-recreate bot
```

默认 `MEMORY_REBUILD_ENABLED=false`。即使设为 true，也只表示管理员可以使用该功能：迁移、启动、
重启和 Worker 都不会自行 plan/start/resume。执行中任务遇到进程重启会进入 paused，必须由当前
真实超级管理员显式恢复。降级前必须先让所有 run 进入 completed/cancelled/failed；downgrade
只删除 staging 与新增 receipt 列，不删除已经提交的事实和证据。

## 3.0.0 正式版

`3.0.0` 不新增生产迁移，Alembic head 继续保持 `0024`。正式版增加离线合成质量门禁、只读
生产审计和必须显式执行的 provenance hygiene；启动、健康检查和发布检查都不会扫描历史或
自动修改数据库。升级代码后可按以下顺序检查：

```powershell
uv run qq-ai-bot-cli memory quality validate-dataset
uv run qq-ai-bot-cli memory quality run --suite full
uv run qq-ai-bot-cli memory quality compare
uv run qq-ai-bot-cli memory release-check --database-url sqlite+aiosqlite:///./data/qq_ai_bot.db
```

如审计发现可明确治理的问题，先运行 `memory hygiene scan` 查看内容无关计划；只有人工确认
fingerprint 后才运行 `memory hygiene apply <fingerprint>`。不要把 hygiene 当作启动步骤。

## Memory Mutation V2 增量升级

Alembic `0025` 只新增 `memory_mutation_receipts` 及索引，不扫描聊天、不改写既有事实、证据、
版本链、FTS 或 Embedding。升级前仍应停止 Bot 写入并备份 `data/`；NapCat 无需停止或重建。

```powershell
docker compose stop bot
Copy-Item data data-backup-before-0025 -Recurse
uv run alembic upgrade head
uv run alembic current
docker compose up -d --no-deps --force-recreate bot
```

回执与事实写入处于同一事务。Embedding 调度发生在提交之后；调度失败不会回滚已提交事实，
后续由既有任务机制重试。`0025 → 0024` 只删除 mutation receipts，不删除事实及状态历史。

Alembic `0026` 只新增 `memory_reflection_jobs` 任务表与索引，不在迁移或启动时扫描聊天，
也不直接改写任何事实。后台反思复用既有维护开关和批量上限，有界发现重复、长期争议及
`person_group` 归属异常；每项任务先持久化、领取，再通过 `MemoryMutationService` 提交，
失败会退避重试，进程中断后的超时领取可恢复。

`0026 → 0025` 只删除反思任务状态。已经由统一变更服务提交的事实状态、证据、版本关系和
mutation receipt 不会被回滚；需要完整恢复数据时仍应使用升级前的 `data/` 备份。
