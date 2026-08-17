# 事件与通知 Hook

事件是不可变 `EventEnvelope`：`event_id`、`name`、`schema_version`、`occurred_at`、`payload`。插件 Hook 是通知型观察者，不能修改原事件或主聊天流水线。

```python
from yuki_plugin_sdk.events import EventEnvelope, EventName
from yuki_plugin_sdk.registrar import (
    EventHookMetadata,
    EventHookRegistration,
)


async def on_reply_sent(event: EventEnvelope) -> None:
    sent = event.payload.get("sent", False)
    logger.debug("reply.sent: %s", sent)


registrar.register_event_hook(
    EventHookRegistration(
        metadata=EventHookMetadata(
            id="observe_reply_sent",
            event=EventName.REPLY_SENT,
            priority=0,
        ),
        handler=on_reply_sent,
    )
)
```

需要 `event.subscribe`。

3.6.0 删除 `planner.*` 事件。准入、自主拒绝、本地能力搜索和回合结束分别是 `turn.admitted`、`turn.rejected`、`turn.autonomous_declined`、`capability.searched`、`turn.closed`。`agent.*` 与 `reply.*` 继续复用。映射表见 [API 2.0 迁移](api-2.0-migration.md)。这些 payload 只有 origin、scope、hash、分数、原因码、工具 id 和延迟，不含聊天正文。

## 执行语义

- 同一事件按优先级从高到低，再按插件 ID/Hook ID 稳定排序。
- 每个 Hook 使用自身 `timeout_seconds` 或 Host 默认值。
- 超时、异常和慢 Hook 会生成 `HookExecution` 并记录脱敏日志，不阻塞其他 Hook 或聊天。
- 不保证 Hook 作为事务的一部分；必须容忍重复通知和进程重启。
- 不要在 Hook 中执行长耗时工作；将短任务交给 `ctx.scheduler`，持久任务交给 Automation。
- `payload` 是按事件定义的 JSON 值投影，不是原始 OneBot/NoneBot 对象。

完整事件名见 [Event Catalog](api-reference/events.md)。

