# PluginContext Facade Reference

下列签名来自 `yuki_plugin_sdk.context`；所有方法都可能因权限、作用域、当前轮安全策略或功能关闭而拒绝。

## Message

```python
get_current() -> CurrentMessage | None
get_reply() -> CurrentMessage | None
get_recent(limit=20) -> tuple[CurrentMessage, ...]
search_history(query, limit=20) -> tuple[CurrentMessage, ...]
send_text(text) -> PluginResult
send_private(user_id, text) -> PluginResult
send_group(group_id, text) -> PluginResult
send_image(*, target_type, target_id, media_reference) -> PluginResult
```

## People / Group

```python
people.get_current() -> Mapping | None
people.get(user_id) -> Mapping | None
people.list_aliases(user_id) -> tuple[str, ...]
people.add_alias(user_id, alias) -> PluginResult

groups.get_current() -> Mapping | None
groups.get(group_id) -> Mapping | None
groups.list_members(group_id, limit=100) -> tuple[Mapping, ...]
groups.get_settings(group_id) -> Mapping
groups.set_setting(group_id, key, value) -> PluginResult
```

## Memory / Relationship

```python
memory.list_person(user_id, limit=20) -> tuple[Mapping, ...]
memory.list_group(group_id, limit=20) -> tuple[Mapping, ...]
memory.search(query, *, scope_type, subject_id, limit=20) -> tuple[Mapping, ...]
memory.add(*, scope_type, subject_id, content, source_type, confidence,
           source_event_ids=()) -> PluginResult
memory.update(memory_id, *, content, confidence=None) -> PluginResult
memory.delete(memory_id) -> PluginResult

relationship.get_current() -> Mapping | None
relationship.get(user_id) -> Mapping | None
relationship.list_events(user_id, limit=20) -> tuple[Mapping, ...]
relationship.adjust(user_id, *, affection_delta=0, trust_delta=0,
                    reason) -> PluginResult
```

`memory.search()` 在 Plugin API 2.0 内部复用主程序的 `MemoryRetriever`：插件作用域校验先确定
真实人物或群，随后 FTS 与可选 Embedding 都只在该 SQL 硬过滤范围内召回，并由确定性 RRF
融合。插件不能选择 Provider/profile、获取原始向量、提交 FTS 语法、通过关键词改变
`subject_id`，也不会在无匹配时回退加载全部事实。返回记录只包含有界的
`retrieval_reason`；`list_person()` / `list_group()` 仍用于确定性列表。Embedding 关闭或故障时
自动保持词法行为，Plugin API 版本仍为 `1.0`。

从 Yuki `3.0.0b2` 开始，`memory.update()` 会创建 explicit correction 新版本并让旧 fact 进入
superseded，不原地改写旧正文；`memory.delete()` 会以
`plugin_explicit_invalidation` 将事实标记为 invalidated，不物理删除事实、证据或状态历史。
插件不能设置 authority、status、conflict_state、supersedes_id，也不能跨人物/群合并或解决
冲突。Facade 签名与 Plugin API 主版本仍保持 `1.0`。

Yuki 3.0.0 对 MemoryFacade 做了正式 contract freeze：稳定方法为 `list_person`、`list_group`、
`search`、`add`、`update`、`delete`。插件不能访问原始向量、历史 rebuild、质量 fixture、全局
生产 audit、其他人物证据或 Provider API Key；上述能力不会因插件声明额外 permission 而开放。

## LLM / Agent / AgentSession

```python
llm.generate(instruction, *, max_characters=2000) -> str
llm.generate_with_context(instruction, *, context_profile,
                          max_characters=2000) -> str

agent.run(instruction, *, allowed_capabilities=(),
          max_tool_calls=None, max_model_requests=None) -> PluginResult

agent_sessions.create(CreateAgentSessionRequest) -> AgentSession
agent_sessions.run(RunAgentSessionRequest) -> AgentSessionRunResult
agent_sessions.reset(session_id: UUID) -> AgentSession
agent_sessions.close(session_id: UUID) -> AgentSession
```

## Web / HTTP / Vision / Media

```python
web.search(query) -> PluginResult
web.read(url, question="") -> PluginResult
http.request(method, url, *, headers=None, body=None, auth_secret=None) -> PluginResult
vision.get_current_observation() -> Mapping | None
vision.analyze_current_media(question="") -> PluginResult
media.get_current() -> tuple[Mapping, ...]
media.create_artifact(*, data, content_type, filename, ttl_seconds=86400) -> MediaArtifactHandle
```

`auth_secret` 只能引用 Manifest 已声明的 Secret。Host 仅在同源请求中注入 Bearer credential，跨源重定向会移除；响应只返回 ETag、Last-Modified、Retry-After、Link 和有限的 Rate Limit / request-id Header。

## Background Notification

```python
notifications.publish(PublishNotificationRequest) -> NotificationPublishReceipt
notifications.grant_target(NotificationTarget, *, bot_user_id) -> BackgroundTargetGrantView
notifications.revoke_target(NotificationTarget) -> bool
notifications.list_grants() -> tuple[BackgroundTargetGrantView, ...]
notifications.status() -> Mapping[str, int]
```

后台发布不需要伪造当前用户调用，但只能投向 Host 已授权目标。外部事件先进入目标主会话 EventLedger；文字、媒体和可选主 Agent 回复由持久 Outbox 独立发送。Grant 的增删仍要求真实 `SUPERUSERS` 调用上下文。

## Automation

```python
automation.list_current_owner() -> tuple[Mapping, ...]
automation.create_from_template(template, parameters) -> PluginResult
automation.pause(task_id) -> PluginResult
automation.resume(task_id) -> PluginResult
automation.cancel(task_id) -> PluginResult
```

## MCP

```python
mcp.status() -> Mapping[str, JsonValue]
mcp.list_servers() -> tuple[Mapping[str, JsonValue], ...]
mcp.search_tools(query) -> tuple[Mapping[str, JsonValue], ...]
mcp.call(server_id, tool_name, arguments) -> PluginResult
```

MCP Facade 使用 Host 的共享 Manager，不暴露连接、进程或 Secret。

## Config / Secret / Storage

```python
config.get(key, *, scope_type="global", scope_id="") -> JsonValue
config.set(key, value, *, scope_type="global", scope_id="") -> None
secrets.configured(name) -> bool
secrets.get(name) -> str
storage.get(namespace, key) -> JsonValue
storage.set(namespace, key, value) -> None
storage.delete(namespace, key) -> bool
storage.list(namespace) -> Mapping[str, JsonValue]
storage.compare_and_set(namespace, key, expected, value) -> bool
```

## Scheduler / OneBot / Events

```python
scheduler.create_task(name, runner) -> str
scheduler.cancel(task_id) -> bool
scheduler.sleep_until_stopped() -> None
onebot.send_music_card(*, provider, resource_id) -> PluginResult
onebot.send_private(user_id, text) -> PluginResult
onebot.send_group(group_id, text) -> PluginResult
onebot.call_read_action(action, params) -> PluginResult
onebot.call_mutating_action(action, params) -> PluginResult
events.publish(EventEnvelope) -> None
```

`PluginContext` 还提供 `plugin_id`、隔离 `logger`、脱敏 `current` 和 `FeatureRegistry features`。
