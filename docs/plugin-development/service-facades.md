# 服务 Facade

`PluginContext` 是 Host 在 `start()` 阶段绑定的能力集合。它不暴露 Settings、Container、Repository、数据库 Session、NoneBot Bot 或原始 `MessageEvent`。

| 属性 | 主要用途 |
|---|---|
| `current` | 当前脱敏 `CurrentMessage` 投影 |
| `messages` | 当前/回复/近期/搜索，以及受限发送 |
| `people`, `groups` | 人物、别名、群和成员投影 |
| `memory`, `relationship` | 结构记忆与关系服务 |
| `llm`, `agent` | 一次生成或受控 Agent 运行 |
| `agent_sessions` | 插件拥有的独立连续 AI 会话 |
| `web`, `http` | Yuki 联网与白名单 HTTP |
| `vision`, `media` | 当前真实媒体的受控分析 |
| `automation` | 当前所有者的持久化任务 |
| `mcp` | MCP Server 状态、目录检索与工具调用 |
| `config`, `secrets`, `storage` | 插件配置、Secret 和私有 KV |
| `scheduler` | Host 托管的短生命周期后台任务 |
| `onebot` | 按读/发送/修改分类的 OneBot 接口 |
| `events` | 发布类型化通知事件 |

完整签名见 [Facade API Reference](api-reference/facades.md)。每次调用仍会检查当前批准权限；持有一个 Python 属性不等于拥有调用权限。

Memory V2 的写入仍统一经过 Host `MemoryFactService`。插件 update 创建修正版本，delete 只做显式
失效；插件不能直接访问 Repository、指定事实状态/authority、物理删除审计记录或绕过当前真实
调用作用域。冲突审计与管理员 merge/resolve 不属于 Plugin API 2.0。

## 独立 AI 会话：跑团示例

插件可为骰子跑团建立与 Yuki 主聊天完全分离的连续会话：

```python
from yuki_plugin_sdk.sessions import (
    CreateAgentSessionRequest,
    RunAgentSessionRequest,
    SessionContextProfile,
    SessionPersistence,
)

campaign = await ctx.agent_sessions.create(
    CreateAgentSessionRequest(
        name="周末克苏鲁跑团",
        instructions=(
            "你是本次跑团的守秘人。连续维护角色、场景、线索和骰点后果；"
            "不离开跑团任务，不声称拥有 Yuki 管理权限。"
        ),
        persistence=SessionPersistence.DURABLE,
        context_profile=SessionContextProfile.CURRENT_GROUP,
        allowed_capabilities=(),
    )
)

turn = await ctx.agent_sessions.run(
    RunAgentSessionRequest(
        session_id=campaign.session_id,
        user_input="调查书房里的旧书桌。",
        max_model_requests=4,
    )
)
await ctx.messages.send_text(turn.text)
```

需要 Manifest 权限 `agent.session`。`CURRENT_USER` 还需 `person.current.read`；`CURRENT_GROUP` 还需 `group.current.read` 且当前必须是真实群聊。

### 会话保证

- `DURABLE` 可跨 Host 重启；`EPHEMERAL` 仅当前 Host 生命周期有效。
- 默认 `context_profile=none`，不注入 QQ、群、主聊天、人物记忆或关系。
- 只持久化用户/助手可见正文；不返回也不存储隐藏推理。
- `allowed_capabilities` 仍取批准交集；当前 v1 初始运行层可以把工具集收窄为 `none`。
- `reset()` 只清空该会话历史；`close()` 关闭后不能继续运行。
- 会话 UUID 是不透明标识，不能用来跨插件或跨真实场景访问。

## 一次性 LLM 与 Agent

`ctx.llm.generate()` 适合无需工具的短生成；`generate_with_context()` 需要对应上下文权限。`ctx.agent.run()` 适合宿主受控工具循环，但插件只能请求自身已获批准的 capability，不能传入超级管理员标志。

当前 Host 可向一次性插件 Agent 提供以下只读能力：

| Manifest 批准权限 | 可请求 capability |
|---|---|
| `message.history.read` | `get_recent_chat_history`、`search_chat_history` |
| `memory.person.read` | `get_person_memories` |
| `memory.group.read` | `get_group_memories` |
| `web.search` | `web_search` |
| `web.read` | `read_webpage` |

实际能力是“调用参数 ∩ 上表批准权限 ∩ 当前真实调用上下文 ∩ 本轮安全策略”。普通用户只能读取本人记忆、当前私聊或当前群范围；图片轮次或本轮已经使用网页时，Host 会继续收窄工具集。`call_onebot_api`、管理员修改和自动化修改不会通过这个只读后端开放。

## MCP Facade

`ctx.mcp.status/list_servers/search_tools` 需要 `mcp.read`；
`ctx.mcp.call(server_id, tool_name, arguments)` 需要 `mcp.call`。Facade 复用宿主唯一
`MCPManager`，不会创建插件私有连接池，也不会向插件暴露 Session、Header 或环境 Secret。

这两项是 Plugin Host 的能力批准，不是针对每个 MCP Tool 的审批；Server 是否可用仍只取决于
Yuki 配置和启停状态。

## 当前会话音乐卡片

`ctx.onebot.send_music_card(provider=..., resource_id=...)` 使用 `onebot.send` 权限，将
OneBot `music` 消息段发送到触发插件的当前真实私聊或群聊。插件不能为这个方法传入 QQ 号或
群号，因此它不能跨会话改变目标；Host 会再次验证当前事件、provider、资源 ID、图片轮次隔离和
发送权限，成功后再写事件账本与脱敏审计。

当前 provider 支持 `qq`、`netease`（发送时规范化为 `163`）、`kugou`、`kuwo` 和 `migu`。
如果资源来自 MCP、网页或其他外部数据，插件应先做结构校验和重名消歧，不得把自定义 URL 当成
资源 ID。任意 OneBot action 仍必须走权限更高的 `call_mutating_action`，不能借音乐卡片 Facade
绕过。

独立长期故事、跑团或游戏状态使用 `agent_sessions`；不要把大量连续历史塞进一次 `llm.generate()`。
