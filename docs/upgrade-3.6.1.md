# 从 Yuki 3.6.0 升级到 3.6.1

3.6.1 是非破坏性功能升级。Alembic `0041` 只新增 `conversation_history_*` 四张表，以及
`chat_events` 上按 bot/scope 回放用的索引。不删除、不改写 `chat_events` 与 Memory V2。

架构合同见 [会话历史 Rollup](architecture/conversation-rollup.md)。发布说明见
[3.6.1](releases/v3.6.1.md)。本地测量见
[3.6.1 History Rollup 性能报告](performance/3.6.1-history-rollup-report.md)；本指南不填写
未实测的 Provider Token。

## 升级会改变什么

- 默认 `CONVERSATION_HISTORY_ROLLUP_ENABLED=true`。长会话在有覆盖后缩短原文近窗，并把较早历史
  放进第二条 SESSION system。
- `model_profiles.toml` 增加独立路由 `conversation_compaction`（默认 flash）。缺失时运行时从
  已有 Flash 任务补齐，不复用 MEMORY_CONSOLIDATION / EXTRACTION / DREAM。
- `.env.example` 增加一组 `CONVERSATION_HISTORY_*` 键。现有 `.env` 不写这些键时使用代码默认值。
- 新增 Main Agent 工具 `get_chat_history_around`。
- 新增 CLI：`qq-ai-bot-cli history-rollup status|inspect|rebuild|invalidate|reconcile`。

Memory 事实、事件账本、关系、自动化和插件数据保留。升级不会扫描历史聊天重写记忆，也不会把
摘要写成 `MemoryFact`。

## 现有 3.6.0 部署：锁定顺序

```text
快照 .env、config/、.mcp.json 以及 data 下数据库/WAL/SHM
-> checksum + sqlite integrity_check
-> 将 YUKI_VERSION 改为 3.6.1
-> docker compose pull
-> docker compose up -d
```

新镜像启动时自动 `alembic upgrade head` 到 `0041`。任一步失败都不得删除快照。

全新 3.6.1 安装跳过快照，走安装器。从 3.5.3 升级必须先走
[3.6.0 升级指南](upgrade-3.6.0.md) 的备份门与 `setup migrate-3-6`，再启动 3.6.1。

## 配置

默认启用。若要先观察、暂不压缩：

```text
CONVERSATION_HISTORY_ROLLUP_ENABLED=false
```

关闭后 Context 回到 3.6.0 的原文高低水位窗口；已写入的 Summary 仍留在库里，可用 CLI inspect。
Worker 故障或关闭时，聊天主路径不增加 Compaction 模型调用；没有覆盖则近窗不会被 shift。

Flash 压缩的来源由 `CONVERSATION_HISTORY_LLM_ORIGINS` 配置（TurnOrigin 逗号列表，默认
`user_message`）。不要在 Python 里为某个群号或产品词写白名单。

完整键见仓库 `.env.example` 中 Conversation History Rollup 一段。阈值、近窗条数和 extractive
上限放在配置里，不写死在运行时。

## 回退

1. 停止 3.6.1 Bot。
2. 从升级前快照恢复 `.env`、`config/`、`.mcp.json` 和数据库文件（含 WAL/SHM）。
3. 将 Compose / `YUKI_VERSION` 指回 3.6.0 并启动。

不要把已执行 `0041` 的库交给 3.6.0 镜像当生产库。`0041` downgrade 只 drop rollup 表；生产回退
以同一套快照为准。

## 升级后检查

- `GET /healthz` 返回 `version=3.6.1`、`database=ok`。
- `SELECT version_num FROM alembic_version` 为 `0041`。
- `chat_events` 与 `memory_facts` 行数相对升级前快照不变。
- 私聊普通问答仍只打 Main Agent，不增加前台 Compaction 调用。
- Worker 关闭时聊天可继续；有覆盖的会话重启后 `coverage_end` 仍约束近窗。
- `qq-ai-bot-cli history-rollup status --bot-user-id … --scope private --user-id …`
  需要完整身份，禁止模糊会话目标。

真实 Provider 账单与 Flash 事实召回见性能报告；本指南不外推未测数字。
