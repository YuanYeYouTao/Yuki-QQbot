# Result、值对象与独立会话

## Result

```python
class PluginResult(StrictModel):
    ok: bool = True
    data: dict[str, JsonValue] = {}
    error_code: str | None = None
    detail: str = ""  # <=1000


class ToolResult(PluginResult):
    pass


class CommandResult(PluginResult):
    text: str = ""  # <=12000
```

成功结果不能带 `error_code`；失败结果必须带匹配 `[a-z][a-z0-9_.-]{0,63}` 的稳定错误码：

```python
PluginResult(ok=False, error_code="weather.rate_limited", detail="稍后再试")
```

`data` 必须是 JSON 值，工具结果被 Host 当作不可信模型上下文。

## CurrentMessage

```python
class CurrentMessage(StrictModel):
    message_id: str
    sender_user_id: str
    scope_type: Literal["private", "group"]
    group_id: str | None
    text: str  # <=12000
    received_at: datetime
    mentioned_user_ids: tuple[str, ...] = ()  # <=20，按消息中的可信提及顺序去重
```

它是脱敏投影，不是原始事件。`mentioned_user_ids` 来自 Host 归一化的真实提及，且不包含机器人自身；需要该字段的插件可调用 `ctx.features.require("message.current.mentions.v1")`。

## Agent Session

```python
class CreateAgentSessionRequest(StrictModel):
    name: str  # 1..128
    instructions: str  # 1..8000
    persistence: ephemeral | durable = durable
    context_profile: none | current_user | current_group = none
    allowed_capabilities: tuple[str, ...] = ()  # <=64, 不重复
    metadata: dict[str, JsonValue] = {}


class RunAgentSessionRequest(StrictModel):
    session_id: UUID
    user_input: str  # 1..12000
    allowed_capabilities: tuple[str, ...] | None
    max_tool_calls: int | None  # 0..64
    max_model_requests: int | None  # 1..64


class AgentSession(StrictModel):
    session_id: UUID
    name: str
    status: active | closed
    persistence: ephemeral | durable
    context_profile: none | current_user | current_group
    created_at: datetime
    updated_at: datetime
    turn_count: int


class AgentSessionRunResult(StrictModel):
    session: AgentSession
    text: str  # <=24000
    tool_calls_used: int = 0
    model_requests: int = 1
```

结果没有 `reasoning_content` 字段；隐藏推理不会跨 Plugin API 边界。

## AdmissionSignal / PromptFragment

`AdmissionSignal.score_delta` 为 `-10..10`、`confidence` 为 `0..1`；`PromptFragment.content` 最多 16000 字符，但最终还受更小 Host/Manifest 预算限制。详细规则见 [AdmissionSignal](../admission-signals.md) 与 [Prompt](../prompts.md)。
