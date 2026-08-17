# Event Catalog v1

来源：`yuki_plugin_sdk.events.EventName`。所有事件使用不可变 `EventEnvelope`，默认 `schema_version=1`。

## 应用

- `application.starting`
- `application.started`
- `application.stopping`

## 消息

- `message.normalized`
- `message.recorded`
- `message.observed`
- `message.triggered`

## 回合与能力

- `turn.admitted`
- `turn.rejected`
- `turn.autonomous_declined`
- `turn.closed`
- `capability.searched`

## 上下文与 Prompt

- `context.assembled`
- `prompt.collecting`
- `prompt.composed`

## Agent

- `agent.starting`
- `agent.tool_called`
- `agent.tool_completed`
- `agent.finished`
- `agent.interrupted`

## 回复

- `reply.planned`
- `reply.generated`
- `reply.sending`
- `reply.sent`
- `reply.cancelled`
- `reply.failed`

## 业务数据

- `memory.created`
- `memory.updated`
- `memory.deleted`
- `relationship.changed`

## 视觉与联网

- `vision.completed`
- `vision.failed`
- `web.search_completed`
- `web.read_completed`

## 自动化

- `automation.created`
- `automation.started`
- `automation.completed`
- `automation.failed`

```python
class EventEnvelope(StrictModel):
    event_id: UUID
    name: EventName
    schema_version: int = 1
    occurred_at: datetime
    payload: Mapping[str, JsonValue]


class HookExecution(StrictModel):
    plugin_id: str
    hook_id: str
    success: bool
    duration_seconds: float
    error_category: str | None
```

具体 `payload` 字段由事件 Schema 版本定义；插件必须忽略未知可选字段，并在执行前验证需要的值。

