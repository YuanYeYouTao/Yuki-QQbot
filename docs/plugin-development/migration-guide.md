# 从内部扩展迁移到 Plugin API v1

1.6.0 不会把记忆、关系、权限、视觉、联网、自动化核心或 AgentRunner 改成插件。只迁移真正可选、边界清晰的本地扩展。

## 映射

| 旧做法 | Plugin API v1 |
|---|---|
| 直接注册 NoneBot matcher | 确定性 `CommandRegistration` 或 Agent `ToolRegistration` |
| 导入 `ApplicationContainer` | `PluginContext` 的最小 Facade |
| 直接读 Repository/SQLite | `storage`、`config` 或对应业务 Facade |
| 拼接系统提示词 | `plugin_context`/`tool_guidance` Fragment |
| 自行决定群里插话 | 有界 `PlannerSignal` |
| 裸 `asyncio.create_task` | Background Service / `ctx.scheduler` |
| 自建 cron 表 | Automation Action |
| 在主历史里保存游戏上下文 | `ctx.agent_sessions` 独立会话 |
| 直接发 OneBot action | `messages`/`onebot` Facade + 显式权限 |

## 迁移步骤

1. 建立 `plugin.toml`，只声明实际使用的权限。
2. 将输入/输出转换为继承 `StrictModel` 的 Pydantic Schema。
3. 把注册与运行拆开：`register()` 仅声明，`start()` 保存绑定 Context。
4. 用 Config/KV 替换全局变量和自建表；不要读取主数据库。
5. 把长任务改为托管后台服务，把持久任务改为 Automation。
6. 为每个真实场景补权限与越权测试。
7. 使用 `FakePluginContext` 和契约测试，不连接真实外部服务。
8. 由管理员发现、审阅、批准和启用。

## 数据迁移

Host 的 Alembic `0013` 非破坏性创建 Planner、插件安装/配置/KV/审计和独立 AI 会话表，保留 1.5.2 人物、聊天、记忆、关系、视觉、联网与自动化数据。插件自己的 KV 迁移应使用版本键和 CAS，保持可回滚；不要从插件运行代码执行任意 DDL。

升级前备份 `data/`：

```bash
docker compose down
cp -R data data.backup-1.6.0
docker compose pull
docker compose up -d
```

首次升级可设置 `PLUGIN_SYSTEM_ENABLED=false`，先检查数据库和普通聊天，再逐项启用插件。Planner 是普通聊天的固定调度边界，不再提供旧流程回退开关。
