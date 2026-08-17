# GitHub Monitor 使用说明

GitHub Monitor 是 Yuki 的只读 GitHub 仓库管家。它定时读取一个或多个仓库的新事件，将中文
摘要、Push/Release 卡片和可选的 Yuki 自然点评投递到指定 QQ 群或私聊。

插件不会修改 GitHub 仓库，也不会把 GitHub 返回的标题、评论或提交信息当成可信指令。

## 功能

- 同时监控多个公开或私有仓库，每个仓库可以投递到多个群聊或私聊。
- 支持 Push、Pull Request、Issue、Comment、Review、Release、Discussion、Fork、Star 等事件。
- Push 会补充 Compare 数据并生成 1200×630 暗色 PNG 卡片，展示分支、提交、文件与增删行数。
- Release 使用同风格 PNG 卡片，展示版本、类型、目标分支、附件数量和发布说明。
- 中文事件描述、稳定事件去重、条件请求、Rate Limit 感知和失败退避。
- 首次启用默认只建立当前基线，不补发历史事件。
- 通知先进入持久 Outbox；只有全部目标成功入库后才推进仓库 cursor。
- 可以让 Yuki 根据当前主会话关系自然点评，也可以只发送确定性文本和卡片。

## 运行要求

- Yuki `>=3.4.1,<4.0`
- Plugin API `2.0`
- Bot 可以访问 `https://api.github.com`
- 私有仓库必须提供可读取目标仓库的 GitHub Token

公开仓库不配置 Token 也能使用，但匿名 GitHub API 的调用额度更低。推荐使用 Fine-grained
Personal Access Token，只授权需要监控的仓库，并仅授予读取 Metadata 与 Contents 所需权限。

## 快速启用

### 1. 配置环境变量

在仓库根目录的 `.env` 中加入：

```dotenv
PLUGIN_SYSTEM_ENABLED=true
PLUGIN_API_VERSION=2.0
PLUGIN_DIRECT_COMMAND_BINDINGS={"/github":"github-monitor:github"}
YUKI_PLUGIN__GITHUB_MONITOR__GITHUB_TOKEN=github_pat_replace_with_your_token
```

Token 可省略，但不要把真实 Token 写入 `plugin.toml`、提交到 Git、通过 QQ 命令发送或发到群聊。
插件只能通过 Host 的 Secret 通道使用它，Token 不会进入普通插件配置、Prompt、工具结果或日志。

如果已有其他直达命令，应合并到同一个 JSON 对象，例如：

```dotenv
PLUGIN_DIRECT_COMMAND_BINDINGS={"/github":"github-monitor:github","*":"io.github.yuanyeyoutao.kun-game:play"}
```

### 2. 重建 Bot

只重建 Bot 可以保留 NapCat 容器与 QQ 登录状态：

```bash
docker compose up -d --build --no-deps bot
```

### 3. 批准并启用插件

在 QQ 中由超级管理员执行：

```text
/ai plugin inspect github-monitor
/ai plugin approve github-monitor
/ai plugin enable github-monitor
```

也可以在仓库根目录执行：

```bash
docker compose exec -u 10001:10001 bot qq-ai-bot-cli plugin inspect github-monitor
docker compose exec -u 10001:10001 bot qq-ai-bot-cli plugin approve github-monitor
docker compose exec -u 10001:10001 bot qq-ai-bot-cli plugin enable github-monitor
```

正常状态应为 `running`，且 `failure_count` 为 `0`。插件源码或 Manifest 发生变化时，Host
可能要求重新批准。

### 4. 添加仓库

向群 `1049765710` 推送：

```text
/github add YuanYeYouTao/Yuki-QQbot group:1049765710
```

向 QQ `2186567848` 私聊推送：

```text
/github add YuanYeYouTao/Yuki-QQbot private:2186567848
```

同一仓库可以重复执行 `add` 来增加不同目标；相同目标不会重复添加。默认首次轮询只记录当前
最新事件作为基线，并发送“监控已启用”，以后只推送新事件。

如果没有配置 `/github` 直达绑定，把命令写成：

```text
/ai plugin run github-monitor github add YuanYeYouTao/Yuki-QQbot group:1049765710
```

## 命令手册

所有管理命令只允许真实 `SUPERUSERS` 使用。

| 命令 | 作用 |
|---|---|
| `/github status` | 查看仓库数量、轮询状态、连续失败和 Outbox 状态 |
| `/github repos` | 列出仓库、启停状态和全部 QQ 投递目标 |
| `/github add owner/repo group:<群号>` | 添加群聊目标 |
| `/github add owner/repo private:<QQ号>` | 添加私聊目标 |
| `/github remove owner/repo group:<群号>` | 移除群聊目标 |
| `/github remove owner/repo private:<QQ号>` | 移除私聊目标 |
| `/github pause owner/repo` | 暂停仓库轮询，保留配置和 cursor |
| `/github resume owner/repo` | 恢复仓库轮询 |
| `/github sync owner/repo baseline` | 清除旧 cursor，按当前最新事件重新建立基线 |
| `/github sync owner/repo replay_recent` | 清除旧 cursor，并回放最近若干条事件 |
| `/github test owner/repo` | 发送本地合成 Push 通知，不调用 GitHub、不推进真实 cursor |
| `/github events owner/repo` | 查看该仓库当前允许的事件类型 |
| `/github rate-limit` | 查看最近一次轮询记录的 GitHub Rate Limit 信息 |
| `/github outbox` | 查看通知 Outbox 与后台点评任务状态 |

`sync replay_recent` 会产生真实通知，排障时优先使用 `test`，不要用回放代替普通测试。

## 默认配置

| 配置 | 默认值 | 范围与含义 |
|---|---:|---|
| `poll_interval_seconds` | `60` | 30–3600 秒；所有已启用仓库的轮询周期 |
| `initial_sync_mode` | `baseline` | `baseline` 或 `replay_recent` |
| `replay_recent_limit` | `5` | 回放最近 1–20 条事件 |
| `events_per_repository` | `100` | GitHub 每页读取 1–100 条 |
| `max_events_per_poll` | `50` | 单仓库单轮最多发布 1–200 条 |
| `request_timeout_seconds` | `20` | 单次请求 3–60 秒 |

每个仓库订阅还支持以下过滤项：

- `event_types`：允许的 GitHub 事件类型。
- `branches`：只接收指定 Push 分支；空集合表示不限制。
- `ignored_actors`：忽略指定 GitHub 用户。
- `ignore_bots`：默认忽略 Bot 账号。
- `ignore_draft_pull_requests`：是否忽略 Draft PR。
- `default_branch_only`：是否只接收默认分支 Push。
- 每个目标可分别控制 `send_text`、`send_card` 和 `ask_agent`。

这些结构化配置由 `GitHubMonitorConfig` 校验并保存在插件隔离配置表中，不应直接修改 SQLite。

## 通知行为

Push 通知包含：

- 仓库、Actor、分支和北京时间。
- 提交数、变更文件数、新增行和删除行。
- 最多四条提交摘要；其余提交以数量提示。
- 使用容器内开源中文字体渲染的本地 PNG，不加载远程图片或字体。

Release 通知包含仓库、发布者、版本标签、正式版/预发布状态、目标分支、附件数量和最多三行发布
说明。其他事件发送中文文本摘要。启用 `ask_agent` 后，外部事件先作为有界、不可信上下文进入主会话，
Yuki 可以根据当前关系补充一句自然反应；外部文本不能给 Agent 授权，也不能要求它修改记忆、
自动化、配置或调用其他工具。

## 可靠性与安全边界

- GitHub 客户端只允许访问 `api.github.com`，只发起读取请求。
- Token 由 Host 在 HTTP 请求阶段注入，插件拿不到可枚举的 Secret 集合。
- `plugin + event_key + target` 是通知幂等键；同一事件不会因轮询重复而反复推送。
- 首次基线、事件 cursor、ETag、Last-Modified、失败次数和 Rate Limit 会持久保存。
- 401 会记为 Token 无效；403/429 会按权限或限流分类并退避；低额度时会主动暂停。
- Outbox 发送结果不确定时标记为 `uncertain`，不会冒险自动重复发送。
- 删除最后一个 QQ 目标时，会同步撤销该目标授权并取消尚未发送的关联任务。

## 端到端测试

```text
/github status
/github repos
/github test YuanYeYouTao/Yuki-QQbot
/github outbox
```

测试成功时，目标会收到中文 Push 摘要和 PNG 卡片；若 `ask_agent=true`，随后还会收到 Yuki 的
自然点评。`test` 不访问 GitHub，因此 Token 和真实轮询需要结合 `status` 中的“上次成功”与
`rate-limit` 再确认。

## 常见问题

### 插件当前未运行

依次执行：

```text
/ai plugin inspect github-monitor
/ai plugin doctor github-monitor
/ai plugin approve github-monitor
/ai plugin enable github-monitor
```

确认 `.env` 中 `PLUGIN_SYSTEM_ENABLED=true`，再只重建 Bot。

### 添加仓库返回 TypeError

Yuki 3.4.1 已修复包含 Pydantic Model、`set` 和 `frozenset` 的插件配置 JSON 序列化。升级并重建
Bot 后重试即可。

### 添加成功，但没有补发旧事件

这是 `baseline` 的预期行为。使用 `/github test owner/repo` 测试投递；只有明确需要回放时才用
`/github sync owner/repo replay_recent`。

### 401、403 或 404

- 401：Token 无效或已撤销，重新生成并替换 `.env` 中的 Secret。
- 403：Token 权限不足或 GitHub API 已限流，查看 `/github rate-limit`。
- 404：仓库名错误，或 Token 无权看到该私有仓库。

### 卡片中文显示为方框

3.4.1 的 Bot 镜像已安装文泉驿微米黑。执行
`docker compose up -d --build --no-deps bot`，不要只重启旧镜像。

### 通知失败或重复

使用 `/github outbox` 查看 `pending`、`failed` 和 `uncertain`。不要通过重置数据库解决；稳定
幂等键、cursor 和 Outbox 会负责正常重试与去重。

## 开发验证

```bash
uv run ruff check plugins/github-monitor
uv run pytest -q plugins/github-monitor/tests
uv run qq-ai-bot-cli plugin test plugins/github-monitor
```

插件契约测试使用 Fake Host，不需要真实 GitHub Token，也不会访问网络。
