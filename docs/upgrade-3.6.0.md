# 从 Yuki 3.5.3 升级到 3.6.0

3.6.0 是破坏性升级。升级后不能继续用 3.5.3 写同一份 SQLite。Alembic `0040` 删除 `planner_runs`，downgrade 会显式拒绝；唯一回退是恢复升级前同一套配置与数据库快照（含 WAL/SHM），再启动 3.5.3。

架构说明见 [3.6.0 运行时](architecture/yuki-3.6.0-runtime.md)。Plugin 1.x 必须先改到 API 2.0，见 [Plugin API 2.0 迁移](plugin-development/api-2.0-migration.md)。

## 升级会改变什么

- 强制 Planner 与 Flash Tool Selection 删除。普通聊天不再有一次前置生成式规划请求。
- `model_profiles.toml` 必须是 schema v3；未迁移的 v1/v2 启动 fail-fast。
- `.env` 中的 `PLANNER_*`、`REPLY_PLAN_HARD_MAX_MESSAGES`、`SPEECH_PLANNER_ENABLED`、`MCP_TOOL_SELECTION_MODE` 由 `setup migrate-3-6` 备份后改写或删除。
- 运行时覆盖按冻结映射迁到 `conversation.*` / `reply.hard_max_messages` / `speech.agent_effects_enabled`；同 scope 已有新键时保留新键并删除旧键。
- 数据库执行 `0037`–`0040`：turn 关联、插件批准清理、语音 cadence 回填，然后删除 `planner_runs`。
- 声明 Plugin API `1.0` / `1.1` 的插件会被拒绝。Manifest、权限或入口变化后必须重新批准。

Memory 事实、事件账本、关系和自动化数据保留。升级不会扫描历史聊天重建记忆。

## 现有部署：锁定顺序

安装器在识别到已有 3.5.3 部署后按此顺序执行。任一步失败都不得删除快照；数据库迁移开始前失败可以恢复配置并重启旧容器。

```text
校验 3.6.0 Release 资产与 checksum
-> docker pull 精确 3.6.0 Bot 镜像（此时不启动服务）
-> 停止旧 Bot，确认数据库写进程已退出
-> 快照 .env、config/、.mcp.json 以及 data 下数据库/WAL/SHM
   到 .yuki/backups/upgrade-3.6/<timestamp>/
-> checksum + sqlite integrity_check（通过已拉取镜像）
-> qq-ai-bot-cli setup migrate-3-6
   --deployment-root <deployment-root>
   --baseline-output .yuki/backups/upgrade-3.6/<timestamp>/baseline-v1.json
-> Guided Setup（可选改配置；未选区块保持迁移后的值）
-> docker compose config
-> docker compose up
```

快照目录不进入 Git 或 Release bundle。`migrate-3-6` 的 `--baseline-output` 必须在 git 工作树之外；安装器写到部署根 `.yuki/` 即满足该约束。

全新安装跳过停旧容器、快照和 `migrate-3-6`。

## migrate-3-6 做什么

1. 备份 `.env`、`config/model_profiles.toml`、`.mcp.json`。
2. 将 `model_profiles` 升到 schema v3：删除 `planner` / `tool_selection` 路由前，若 `memory_attribution` 仍依赖旧的 `utility_structured → planner` 隐式回退，先写成显式 route。
3. 按冻结表改写环境变量；删除不再映射的 `PLANNER_*` 与 tool-selection 同义键。
4. 若 `data/qq_ai_bot.db` 存在、含 `planner_runs`、且已具备 `0037` 关联表，则导出 content-free runtime baseline。没有数据库、没有该表、或仍早于 `0037` 时跳过导出并记录原因。

未迁移的旧 schema 启动失败信息会指向同一条命令：

```text
qq-ai-bot-cli setup migrate-3-6 --deployment-root <deployment-root>
```

## 配置映射

| 旧键 | 新键 |
|---|---|
| `PLANNER_GROUP_ENABLED` / `planner.group_enabled` | `CONVERSATION_AUTONOMOUS_ENABLED` / `conversation.autonomous_enabled` |
| `PLANNER_GROUP_DEBOUNCE_SECONDS` | `CONVERSATION_AUTONOMOUS_DEBOUNCE_SECONDS` |
| `PLANNER_REPLY_NECESSITY_THRESHOLD` | `CONVERSATION_AUTONOMOUS_ADMISSION_THRESHOLD` |
| `PLANNER_MAX_PENDING_MESSAGES` | `CONVERSATION_AUTONOMOUS_BATCH_LIMIT` |
| `PLANNER_RECENT_PRESENCE_WINDOW_SECONDS` | `CONVERSATION_AUTONOMOUS_PRESENCE_WINDOW_SECONDS` |
| `PLANNER_INTERRUPT_AUTONOMOUS_ON_NEW_MESSAGE` | `CONVERSATION_INTERRUPT_AUTONOMOUS_ON_NEW_MESSAGE` |
| `REPLY_PLAN_HARD_MAX_MESSAGES` | `REPLY_HARD_MAX_MESSAGES` |
| `SPEECH_PLANNER_ENABLED` | `SPEECH_AGENT_EFFECTS_ENABLED` |

下列键备份后删除，不映射：`PLANNER_DIRECT_ENABLED`、`PLANNER_TEMPERATURE`、`PLANNER_MAX_OUTPUT_TOKENS`、`PLANNER_TIMEOUT_SECONDS`、`PLANNER_CONFIDENCE_THRESHOLD`、`PLANNER_MAX_WAIT_SECONDS`、`PLANNER_PREFERRED_MESSAGES`、`PLANNER_RECORD_RUNS`，以及 `MCP_TOOL_SELECTION_MODE` / `PLANNER_TOOL_SELECTION_MODE` / `TOOL_SELECTION_MODE`。

运行时不再 dual-read 旧键。Settings `extra="ignore"`，残留 `PLANNER_*` 环境变量会被忽略。

`planner.preferred_messages`（日常回复条数软目标）没有后继项。发送上限使用 `reply.hard_max_messages`。

## 插件

1. 把 `plugin.toml` 的 `plugin_api` 改为 `"2.0"`。
2. `PlannerSignal` / `register_planner_signal` 改为 `AdmissionSignal` / `register_admission_signal`。
3. Prompt `target` 只保留 `agent` 或 `plugin_session`。
4. 为工具补 namespace / aliases / use_when / tags（可选，但名称冲突由 Host 拒绝）。
5. 部署后重新 `plugin discover`、审阅、`approve`、`enable`，或通过 Guided Setup 一次应用 pending。

Alembic `0038` 会撤销旧 `planner.signal.register` 批准。不能沿用 3.5.3 的批准记录。

## 回退

1. 停止 3.6.0 Bot。
2. 从 `.yuki/backups/upgrade-3.6/<timestamp>/` 恢复 `.env`、`config/`、`.mcp.json` 和数据库文件（含 WAL/SHM）。
3. 将 Compose / `YUKI_VERSION` 指回 3.5.3 镜像并启动。

不要对已执行 `0040` 的库运行 Alembic downgrade。不要用 3.5.3 打开已经迁到 `0040` 的数据库。

## 升级后检查

- `GET /healthz` 不再包含 `planner_configured` / `planner_active_requests`。
- `qq-ai-bot-cli runtime snapshot`、`runtime capability-search`、`runtime memory-session` 只输出配置与 id/分数，不含用户正文。
- 内置插件在 `PLUGIN_SYSTEM_ENABLED=true` 时能 discover 并进入 pending/运行。
- 私聊普通问答只打 Main Agent，不再打 Planner 路由。

真实流量回放与 P50/P95 见性能报告；本指南不填写未实测数字。
