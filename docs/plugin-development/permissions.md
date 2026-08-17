# 权限与风险

插件要同时满足三层条件：Manifest 请求、管理员按 Manifest 哈希批准、当前真实调用上下文允许。模型文本、历史、OCR、网页、AdmissionSignal 和插件参数都不能增加权限。

```text
实际能力 = Manifest 声明 ∩ 当前批准 ∩ 当前用户/群/TurnOrigin ∩ 本轮安全策略
```

图片轮次、联网后的高风险撤销、自动化 `DelegatedAuthority` 和本轮只读策略只能继续缩小集合。

## 权限家族

| 家族 | 示例 | 风险提示 |
|---|---|---|
| 当前消息/历史 | `message.current.read`, `message.history.read` | 可能读取聊天数据，必须按真实场景隔离 |
| 主动发送 | `message.group.send`, `onebot.send` | 会对 QQ 外部状态产生可见影响 |
| 人物/群 | `person.read`, `group.members.read` | 不得跨未批准目标枚举 |
| 记忆/关系 | `memory.write`, `relationship.write` | 写操作影响长期人格上下文 |
| 模型 | `llm.generate`, `agent.run`, `agent.session` | 消耗额度；Agent 能力仍由 Host 裁剪 |
| 联网 | `network.http.allowlisted` | 仅 Manifest 精确公共域名 |
| 自动化 | `automation.manage_self`, `automation.action.register` | 委托权限必须可重验证 |
| 配置/Secret/KV | `plugin.config.*`, `storage.private` | Secret 不等于普通配置，不得记录 |
| OneBot | `onebot.read/send/mutate` | `mutate` 是高风险外部操作 |
| 扩展注册 | `tool.register`, `event.subscribe` | 只允许在 `register()` 声明 |

完整枚举见 [Permission Catalog](api-reference/permissions.md)。

## 组件自身权限级别

工具、命令和自动化 Action 还声明 `PermissionLevel`：`user`、`trusted`、`moderator`、`superuser`。当前只有普通用户与真实 `SUPERUSERS` 是可执行授权来源；预留等级不能由插件分配。

## 高风险权限

SDK 把以下权限标记为高风险：

- `relationship.write`
- `memory.delete`
- `runtime.config.write`
- `network.http.unrestricted`
- `onebot.mutate`
- `agent.run`
- `agent.session`

批准前应逐项审阅代码、用途、目标范围和失败行为。权限批准不是恶意 Python 的沙盒。

## 不能声明的能力

不存在“读取完整系统提示词”“读取所有 Secret”“取得数据库连接”“执行 Shell”“访问 Docker Socket”“成为超级管理员”等权限。插件也不能通过 `user_id` 参数伪造当前真实发送者。

