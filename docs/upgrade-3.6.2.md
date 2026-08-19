# 从 Yuki 3.6.1 升级到 3.6.2

3.6.2 是非破坏性运行时升级。不新增 Alembic 版本，不改 `chat_events` 与 Memory V2。
近窗左沿改为只认落库 `coverage_end`；超预算时同步 extractive，不再从尾巴滑动切窗。

架构合同见 [会话历史 Rollup](architecture/conversation-rollup.md)。实施见
[3.6.2 任务书](architecture/Yuki-3.6.2-Frozen-History-Tail-Taskbook.md)。

## 升级会改变什么

- 有覆盖后 Prompt 加载 `id > coverage_end` 且按 id 升序。未覆盖条数超过 `local_event_limit`
  时不再用 DESC 在覆盖点与近窗之间挖洞。
- 热尾是最近 `CONVERSATION_HISTORY_RAW_TAIL_EVENTS` 条与最近
  `CONVERSATION_HISTORY_RAW_TAIL_CHARACTERS` 渲染字符的交集。
- `CONVERSATION_HISTORY_RAW_TAIL_BUDGET_RATIO` 只触发同步压缩，不从最新往回挑选 Prompt 起点。
- 同一 assemble 最多同步 extractive `CONVERSATION_HISTORY_SYNC_EXTRACTIVE_MAX_SLICES` 次（默认 3）。
- `HISTORY_WINDOW_LOW_WATERMARK_RATIO` 仍可读，但 Prompt 选窗不再使用。
- 默认热尾从 48/3600 收紧到 32/1600，预算比从 0.55 收到 0.40。现有 `.env` 若已写旧值，升级后
  仍用文件里的值；未写则吃新默认。

不在本版本关闭模型思考、不改 `LLM_MAX_OUTPUT_TOKENS`、不修 Flash `structured_output`。

## 现有 3.6.1 部署

无迁表。快照 `.env` 与数据库后，换成含 3.6.2 的镜像/源码并重启即可。任一步失败都不得删除快照。

## 回退

1. 停止 Bot。
2. 恢复升级前的代码或镜像，以及如需保留的 `.env`。
3. 数据库可继续用 3.6.1 的 `0041` 头。不要为了回退 drop rollup 表。

## 升级后检查

- `GET /healthz` 的 `database=ok`；`alembic_version` 仍为 `0041`。
- 有覆盖的闲聊会话：Prompt 第一条未覆盖原文的 id 等于 `coverage_end + 1`。
- 只追加一条消息时 `raw_history_window_shifted` 不得为真；覆盖前进时可以为真。
- 短消息超预算后 coverage 连续前进，不跳号。
