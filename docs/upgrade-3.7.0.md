# 从 Yuki 3.6.1 升级到 3.7.0

3.7.0 是破坏性升级。Alembic `0042` 删除旧 History Rollup 表并建立 Bot-aware ConversationScope 与单检查点 Rollup；`downgrade()` 会明确拒绝执行。

## 升级前

1. 确认当前 Bot 为 3.6.1、Alembic revision 为 `0041`，并完成一次健康检查。
2. 停止 Bot；不要让旧实例、定时重启器或第二套 Compose 继续连接数据库。
3. 对同一时间点备份：
   - `.env`、`config/`、`.mcp.json` 和 Compose/安装器文件；
   - `data/qq_ai_bot.db` 以及存在的 `-wal`、`-shm`；
   - 当前 Bot 与可选 TTS Worker 镜像 ID/标签。
4. 对备份计算校验和，并在副本上确认 SQLite 可打开、`PRAGMA foreign_key_check` 无结果、revision 仍为 `0041`。

不要只复制主数据库而遗漏未 checkpoint 的 WAL，也不要在运行中的数据库上制作普通文件副本。

## 配置切换

将 `YUKI_VERSION` 改为 `3.7.0`。删除全部旧 `CONVERSATION_HISTORY_*` 和 3.6 分层 Rollup 键，按 `.env.example` 配置新的 `CONVERSATION_ROLLUP_*` 与 `CONVERSATION_EFFECT_GATE_TIMEOUT_SECONDS`。旧键不会被静默忽略，残留时启动失败。

生产必须保持一个主动 Bot 实例：不要执行 `docker compose up --scale bot=2`，不要滚动启动两个同时连接同一 SQLite 的版本。3.7.0 会在应用生命周期内持有数据库旁的 OS advisory lock；第二个进程会在启动阶段明确失败。

## 升级

```bash
docker compose pull bot
docker compose up -d --no-deps --force-recreate bot
```

启动入口会先升级到 0041，再执行 0042。0042 会先检查外键和账本身份；任何检查或故障注入失败都必须完整回滚，应用不得启动。

## 验证

1. `/healthz` 返回 `status=ok`、`version=3.7.0`、`database=ok`。
2. 数据库 revision 为 `0042`，`PRAGMA foreign_key_check` 无结果。
3. 旧五张表（四张 `conversation_history_*` 表和 `context_resets`）不存在，新三张表存在。
4. `chat_events` 与 Memory V2 行数/关键哈希和升级前记录一致。
5. 在测试群用两个普通成员交替聊天，确认 `/ai status` 显示同一 Scope 与 generation。
6. 由超级管理员执行一次群 `/ai new`，确认全群进入新 generation；普通成员执行会被拒绝。
7. 验证联网来源、工具、语音/图片回复、插件通知和 `/ai forgetme` 的基本路径。
8. 强制一次 Rollup 模型失败，确认 extractive 推进 coverage，job 不进入 failed 状态。

升级会把每个 Scope 的 `starts_after_event_id` 设为迁移时最大事件 ID：旧短期摘要和旧 reset 不恢复，但永久账本与 Memory V2 不删除。

## 回退

不能对生产数据库执行 Alembic downgrade，也不能在 0042 上启动 3.6.1。

1. 停止 3.7.0，确认没有其他实例连接数据库。
2. 保存故障现场副本供排查。
3. 恢复升级前同一时间点的数据库/WAL/SHM、`.env`、配置、MCP 与部署文件。
4. 将镜像恢复为备份记录的 3.6.1 镜像，启动唯一 Bot。
5. 验证 revision 为 `0041`、健康检查正常，并抽查聊天与 Memory V2。

恢复快照之后，3.7.0 运行期间产生的消息和配置变更不会存在；这是不可逆 schema 切换的预期代价。
