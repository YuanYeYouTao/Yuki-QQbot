# AdmissionSignal

AdmissionSignal 允许插件向**群自主插话**评分提供一个很小、可观察的建议。它不能让 Yuki 发言、调用工具、获得权限，也不能改变工具排序。

私聊、@ 机器人、回复机器人由 Host 直接准入，不走该评分，插件不能 veto。

```python
from yuki_plugin_sdk.models import AdmissionSignal, AdmissionSignalContext
from yuki_plugin_sdk.registrar import AdmissionSignalRegistration


async def campaign_signal(context: AdmissionSignalContext) -> AdmissionSignal | None:
    if context.current.scope_type != "group":
        return None
    if not campaign_is_active:
        return None
    return AdmissionSignal(
        source_plugin_id="com.example.rpg",
        score_delta=4,
        reason_code="campaign.active",
        summary="当前群正在进行插件主持的跑团",
        confidence=0.9,
    )


registrar.register_admission_signal(
    AdmissionSignalRegistration(name="campaign_active", provider=campaign_signal)
)
```

需要 `admission.signal.register`，并应先检查 `ctx.features.has("admission.signal.v1")`。
`provider` 可以接收 `AdmissionSignalContext`，也可以是无参协程。

## 硬限制

- 单个信号 `score_delta` 只能在 `-10..10`。
- `confidence` 为 `0..1`；`summary` 最多 500 字符，Host 还可能再截断。
- `reason_code` 匹配 `[a-z][a-z0-9_.-]{0,63}`。
- Host 会覆盖 `source_plugin_id` 为真实插件 id，并丢弃过期信号。
- 同一插件多条信号先按加权分合并，再裁剪到 `±10`；全部插件合计再裁剪到 `±15`。
- 信号只影响“是否值得进入自主群回复”的建议。阈值、群聊速度、存在感惩罚和小时上限仍由 Host 执行。
- 信号不能绕过 Host 权限，不能修改 Memory contract，也不能指定工具顺序。

不要把 Signal 当事件总线或状态存储。需要连续状态时使用私有 KV；需要独立叙事历史时使用插件 AI 会话。
