# Permission Catalog

来源：`yuki_plugin_sdk.permissions.PluginPermission`。Manifest 只能使用下表精确字符串。

## 消息与发送

| 权限 | 含义 |
|---|---|
| `message.current.read` | 读取当前脱敏消息投影 |
| `message.reply.read` | 读取当前回复目标投影 |
| `message.history.read` | 读取/搜索当前授权范围历史 |
| `message.private.send` | 主动发送私聊文本 |
| `message.group.send` | 主动发送群文本 |
| `message.media.send` | 发送 Host 接受的媒体引用 |

## 人物与群

| 权限 | 含义 |
|---|---|
| `person.current.read` | 当前真实人物投影 |
| `person.read` | 指定授权人物投影 |
| `person.alias.read` | 读取人物别名 |
| `person.alias.write` | 添加人物别名 |
| `group.current.read` | 当前真实群投影 |
| `group.read` | 指定授权群投影 |
| `group.members.read` | 列出授权群成员 |
| `group.settings.write` | 修改授权群设置 |

## 记忆与关系

| 权限 | 含义 |
|---|---|
| `memory.person.read` | 读取人物记忆 |
| `memory.group.read` | 读取群记忆 |
| `memory.search` | 在授权作用域搜索记忆 |
| `memory.write` | 新增/更新记忆 |
| `memory.delete` | 删除记忆；高风险 |
| `relationship.current.read` | 当前人物关系投影 |
| `relationship.read` | 指定授权人物关系/事件 |
| `relationship.write` | 调整关系；高风险 |

## 模型、联网与视觉

| 权限 | 含义 |
|---|---|
| `llm.generate` | 无工具的一次生成 |
| `llm.generate_with_context` | 使用受控上下文生成 |
| `agent.run` | 运行 Host 受控 Agent；高风险 |
| `agent.session` | 创建独立插件 AI 会话；高风险 |
| `web.search` | 使用 Yuki 搜索服务 |
| `web.read` | 使用 Yuki 网页读取服务 |
| `network.http.allowlisted` | 请求 Manifest 精确白名单公共主机 |
| `network.http.unrestricted` | 更宽网络请求；高风险且仍受 Host 底线保护 |
| `vision.current.read` | 读取当前已有视觉观察 |
| `vision.analyze` | 请求分析当前真实媒体 |
| `media.current.read` | 读取当前媒体段投影 |
| `media.artifact.create` | 创建 Host 托管、带配额和 TTL 的媒体产物 |

## 自动化、配置与存储

| 权限 | 含义 |
|---|---|
| `automation.read` | 读取当前所有者自动化 |
| `automation.manage_self` | 创建/暂停/恢复/取消当前所有者任务 |
| `automation.action.register` | 注册插件 Automation Action |
| `mcp.read` | 读取 MCP Server 状态与紧凑工具目录 |
| `mcp.call` | 调用已配置并启用的 MCP Tool |
| `plugin.config.read` | 读取本插件配置 |
| `plugin.config.write` | 写入本插件配置 |
| `runtime.config.read` | 读取允许公开的 Yuki 运行时配置 |
| `runtime.config.write` | 修改允许公开的 Yuki 运行时配置；高风险 |
| `storage.private` | 使用本插件隔离 KV |

## OneBot 与注册能力

| 权限 | 含义 |
|---|---|
| `onebot.read` | 调用 Host 归类为只读的 OneBot action |
| `onebot.send` | 通过 OneBot Facade 发送消息 |
| `onebot.mutate` | 调用修改型 OneBot action；高风险 |
| `prompt.context.register` | 注册 `plugin_context` Fragment |
| `prompt.guidance.register` | 注册 `tool_guidance` Fragment |
| `tool.register` | 注册 Agent 工具 |
| `command.register` | 注册确定性命令 |
| `event.subscribe` | 注册通知 Hook |
| `background.worker` | 注册 Host 托管后台服务 |
| `notification.publish` | 向已授权目标发布外部事件和持久通知 |
| `notification.agent` | 请求已授权外部事件进入主会话 Agent；高风险 |
| `admission.signal.register` | 注册有界 AdmissionSignal |

`HIGH_RISK_PERMISSIONS` 当前包含：`relationship.write`、`memory.delete`、`runtime.config.write`、`network.http.unrestricted`、`onebot.mutate`、`agent.run`、`agent.session`、`notification.agent` 等管理型能力。

