# Yuki 主 Agent 历史信封与可信引用系统改造任务书

## 1. 项目名称

**Yuki 主 Agent 历史信封压缩与可信短引用改造**

---

## 2. 项目背景

Yuki 当前向主 Agent 投影聊天历史时，每条消息都会重复携带发送者、完整 QQ 号和真实平台消息 ID。在线样本中，44 条聊天事件只有 611 个正文字符，但渲染后达到 3,622 个字符，其中约 3,011 个字符来自身份与关系信封。

这会产生三类问题：

1. 聊天正文在上下文中的占比过低；
2. 相同字符预算下，可见历史时间跨度明显缩短；
3. 完整 QQ 号和长消息 ID 反复进入模型上下文，增加输入 token、缓存压力和模型误用真实标识符的风险。

本任务在现有讨论稿基础上采用更激进的方案：

- 主 Agent 不再看到真实 QQ 号、群号和平台消息 ID；
- 主 Agent 只使用短用户引用、短群引用和短消息引用；
- 所有真实标识符仅保留在后端可信映射中；
- 所有涉及 QQ、群、消息定位的模型可见工具统一改为引用参数；
- 后端在执行前完成引用解析、来源校验、权限校验和真实参数转换。

---

## 3. 改造目标

### 3.1 成本目标

- 显著减少历史信封字符数；
- 在不扩大上下文窗口的情况下保留更多真实聊天事件；
- 提高主 Agent 连续轮次的缓存前缀稳定性；
- 减少真实 ID、重复发送者信息和长消息号带来的 token 消耗。

### 3.2 能力目标

主 Agent 必须继续正确判断：

- 每条消息由谁发送；
- 同一用户连续发送了几条消息；
- 某条消息回复了哪条历史消息；
- 某条消息提及了哪些用户；
- 工具操作的目标用户、目标群和目标消息；
- 当前消息与历史消息的边界。

### 3.3 安全目标

- 模型不能自由构造真实 QQ 号、群号或消息 ID；
- 模型不能使用未在当前轮注册的引用；
- 历史中出现过的用户不能自动成为禁言、踢出等修改操作的合法目标；
- 修改类操作必须同时满足引用来源要求和现有后端权限要求；
- 引用系统不能扩大 Planner、OneBot、插件或用户原有权限。

---

## 4. 实施范围

### 4.1 本次修改范围

本次修改包括：

- 主 Agent 专用历史投影；
- 短消息引用；
- 短用户引用；
- 短群引用；
- 当前轮可信引用注册表；
- 模型可见工具参数；
- 工具执行前的引用解析；
- 引用来源与权限验证；
- 输出清理；
- 上下文与缓存指标；
- 单元测试、集成测试和迁移验证。

### 4.2 保持不变的模块

以下内容保持原始真实 ID 和现有协议：

- `chat_events` 数据结构与历史数据；
- Planner 的最近 10 条原始事件；
- Planner 内部真实 `reply_to_message_id`；
- 普通 Memory Worker；
- Self Reflection；
- Memory Audit；
- 历史重建；
- OneBot 底层 API；
- 数据库中的 QQ、群和消息标识；
- 插件与自动化的后端权限模型。

主 Agent 的显示层和工具参数层使用短引用，但底层存储和执行层仍使用真实标识符。

---

## 5. 总体架构

```text
chat_events / 当前 OneBot 事件
              ↓
可信引用注册表构建
              ↓
用户引用：u1、u2……
群引用：g1……
消息引用：m1、m2……
              ↓
主 Agent 专用历史块渲染
              ↓
主 Agent 只看到短引用
              ↓
工具调用提交 user_ref / group_ref / message_ref
              ↓
后端解析引用
              ↓
验证来源、可见性、群成员关系和权限
              ↓
转换为真实 QQ / 群号 / platform_message_id
              ↓
现有 Tool Kernel / OneBot / Memory 服务
```

---

## 6. 主 Agent 历史格式

### 6.1 推荐格式

```text
[u1=远野#7848]
m1> 今晚把记忆写入测试完吧
m2> 然后再看一下缓存命中率

[u2=查无此人#3586]
m3↳m1> 你昨天也是这么说的

[Yuki]
m4> 好，我先检查记忆写入
m5> 失败时列出具体原因
```

### 6.2 格式规则

1. 连续同发送者消息合并为一个消息块；
2. 合并依据只能是可信 `sender_user_id`；
3. 每条原始 QQ 消息保留独立一行；
4. 每条历史消息都分配短消息引用；
5. Reply 使用 `m3↳m1>`；
6. Mention 放在对应消息行，不提升为块级属性；
7. Yuki 固定显示为 `[Yuki]`；
8. 当前消息继续单独传入，不进入历史块；
9. 当前消息使用固定别名 `current_event`，不分配历史 `mN`；
10. 外部事件、工具结果和系统事件不得与普通用户消息合并。

### 6.3 用户标签

模型可见用户标签格式：

```text
u1=远野#7848
```

要求：

- 首选群名片；
- 无群名片时使用昵称；
- 标签附带 QQ 尾四位；
- 尾四位冲突时自动扩展到尾六位；
- 仍冲突时使用更长尾号；
- 模型不看到完整 QQ 号；
- 后端身份始终使用完整 `sender_user_id`。

---

## 7. 引用生命周期

### 7.1 消息引用

消息引用采用**历史窗口 epoch 内稳定编号**。

同一 epoch 中：

```text
m1 m2 m3
      ↓ 新消息
m1 m2 m3 m4
      ↓ 新消息
m1 m2 m3 m4 m5
```

禁止每轮重新从最近消息开始编号。

### 7.2 Epoch 重建条件

仅在以下情况建立新 epoch：

- 历史达到高水位并发生块状滚动；
- 会话执行明确的 context reset；
- 当前会话身份或作用域变化；
- 机器人进程重启后无法恢复原 epoch；
- 引用注册表发生不可恢复的不一致。

窗口滚动时缓存前缀本来就会失效，因此允许重新编号。

### 7.3 当前轮冻结

Agent turn 开始后，引用映射必须冻结：

- 多次模型请求共用同一份映射；
- `request_tools` 前后共用同一份映射；
- 并行工具调用共用同一份映射；
- 当前 turn 结束后释放；
- 下一轮可以在同一 epoch 上追加新引用。

---

## 8. 可信引用注册表

### 8.1 数据结构

建议新增：

```python
@dataclass(frozen=True, slots=True)
class UserReference:
    ref: str
    user_id: str
    display_label: str
    provenance: str
    group_id: str | None
    visible: bool
    mutable_target: bool


@dataclass(frozen=True, slots=True)
class MessageReference:
    ref: str
    event_id: int
    platform_message_id: str
    sender_user_ref: str
    provenance: str
    visible: bool


@dataclass(frozen=True, slots=True)
class GroupReference:
    ref: str
    group_id: str
    provenance: str
    visible: bool


@dataclass(frozen=True, slots=True)
class TurnReferenceRegistry:
    users: tuple[UserReference, ...]
    messages: tuple[MessageReference, ...]
    groups: tuple[GroupReference, ...]
    current_event_id: int
    epoch_id: str
```

### 8.2 用户引用来源

`provenance` 至少区分：

```text
current_sender
current_mention
current_reply
history
explicit_current_message
system
```

### 8.3 群引用

当前群固定使用：

```text
g1
```

非当前群不得自动加入主 Agent 引用注册表。

### 8.4 显式 QQ 输入

用户当前消息中逐字提供 QQ 号时，后端可以建立：

```text
q1 -> 123456789
```

模型只看到：

```text
explicit_user_1=q1
```

要求：

- QQ 号必须逐字存在于当前真实入站消息；
- 只能由后端确定性抽取；
- 模型不能自行提交原始 QQ；
- 后端仍需验证当前用户是否有权对该目标执行操作。

---

## 9. 工具参数改造

### 9.1 统一引用参数

模型可见工具统一使用：

```text
user_ref
group_ref
message_ref
```

禁止继续向模型暴露自由填写的：

```text
user_id
group_id
message_id
platform_message_id
```

### 9.2 示例

旧参数：

```json
{
  "group_id": "917568554",
  "user_id": "3135003586",
  "duration": 600
}
```

新参数：

```json
{
  "group_ref": "g1",
  "user_ref": "u2",
  "duration_seconds": 600
}
```

后端执行：

```python
group_id = refs.resolve_group("g1")
user_id = refs.resolve_user("u2")
```

### 9.3 Schema 稳定性

工具 Schema 不得动态生成：

```json
"enum": ["u1", "u2", "u3"]
```

固定 Schema 使用：

```json
{
  "user_ref": {
    "type": "string",
    "pattern": "^(u|q)[1-9][0-9]*$",
    "description": "使用本轮运行资料中提供的可信用户引用"
  }
}
```

实际可用引用放在本轮动态上下文，不进入 Tool Schema，以保持缓存稳定。

---

## 10. OneBot 工具改造

### 10.1 模型侧禁止通用原始 ID 网关

不再直接向模型暴露可自由提交真实 ID 的通用：

```text
call_onebot_api
```

推荐拆成类型化工具：

```text
set_group_ban
kick_group_member
send_private_message
delete_message
get_group_member_info
```

### 10.2 内部执行链

```text
模型类型化工具
    ↓
引用解析
    ↓
权限与目标来源验证
    ↓
转换真实参数
    ↓
内部 OneBot Gateway
    ↓
NapCat
```

通用 OneBot Gateway 继续保留，但只作为内部接口。

---

## 11. 引用权限规则

### 11.1 读取类操作

允许来源：

- `current_sender`
- `current_mention`
- `current_reply`
- `history`
- `explicit_current_message`

适用：

- 读取聊天历史；
- 读取人物记忆；
- 查询群成员信息；
- 查看当前群中的公开资料。

### 11.2 修改类操作

禁言、踢人、删除消息、发送私聊等修改操作仅允许：

- `current_mention`
- `current_reply`
- `explicit_current_message`
- 后端明确授权的系统引用

历史中仅出现过的用户不得成为修改目标。

### 11.3 当前发送者本人

当前发送者可以通过固定引用：

```text
current_speaker
```

或对应用户引用进行：

- 修改本人设置；
- 查询本人权限；
- 管理本人自动化；
- 操作本人记忆。

### 11.4 昵称输入

用户仅输入昵称、群名片或普通姓名时：

- 后端不得猜测；
- 不得使用历史中最近同名用户；
- 不得由模型自行选择；
- 应要求用户 @ 对方、回复对方消息或提供 QQ 号。

---

## 12. 引用解析错误

统一错误代码：

```text
unknown_user_ref
unknown_group_ref
unknown_message_ref
stale_reference
reference_epoch_mismatch
target_not_visible
target_not_authorized
target_not_group_member
target_not_mutable
ambiguous_target
explicit_identifier_not_in_current_event
```

模型收到错误后只能：

- 更换已存在的合法引用；
- 要求用户明确目标；
- 停止操作。

禁止根据昵称或历史内容猜测真实目标。

---

## 13. 渲染与预算顺序

压缩必须发生在历史裁剪之前。

正确顺序：

```text
EventRecord
    ↓
建立 epoch 引用
    ↓
主 Agent 消息块渲染
    ↓
按紧凑格式计算字符数
    ↓
执行高低水位裁剪
    ↓
PromptComposer
    ↓
主 Agent
```

禁止在 PromptComposer 最后一步才做字符串压缩，否则不能恢复此前已经被预算淘汰的旧消息。

---

## 14. 推荐代码边界

```text
ChatEventPromptRenderer
├── render_event(event)
│   └── Planner、审计、自省、Memory 继续使用
│
└── render_main_blocks(events, registry)
    └── 仅主 Agent 使用
```

建议新增：

```text
src/qq_ai_bot/references/
├── models.py
├── registry.py
├── resolver.py
├── epoch.py
└── errors.py
```

建议修改：

```text
src/qq_ai_bot/services/context_assembler.py
src/qq_ai_bot/services/prompt_composer.py
src/qq_ai_bot/services/agent_tools.py
src/qq_ai_bot/services/chat.py
src/qq_ai_bot/capabilities/provider.py
src/qq_ai_bot/capabilities/policy.py
src/qq_ai_bot/event_prompt.py
```

---

## 15. 输出清理

主 Agent 最终回答不应泄漏：

```text
u1
u2
m1
m2
g1
current_event
```

输出清理器应：

- 只匹配明确的内部引用信封格式；
- 不使用可能误删正常文本的宽泛正则；
- 保留用户主动输入的普通字符串；
- 对引用泄漏记录低敏感度日志，不记录真实映射。

---

## 16. 缓存设计

### 16.1 稳定原则

- 同一 epoch 中旧引用不变；
- 新消息只在末尾追加；
- 工具 Schema 固定；
- 工具定义按名称排序；
- 动态可用引用放在靠后的本轮资料；
- 真实 ID 不进入主 Agent prompt；
- 只有窗口滚动时重建历史前缀。

### 16.2 指标

新增：

```text
history_event_count
history_block_count
history_envelope_characters
history_body_characters
reference_registry_user_count
reference_registry_message_count
reference_registry_group_count
reference_resolution_failures
reference_epoch_rolls
history_window_rolled
prompt_tokens
cached_prompt_tokens
cache_hit_rate
```

---

## 17. 实施任务

### T1：建立引用领域模型

- 新增用户、群、消息和 turn registry 模型；
- 增加引用来源枚举；
- 增加引用错误类型；
- 引用模型不得包含模型可修改字段。

### T2：建立 epoch 管理器

- 按会话维护历史 epoch；
- 新消息稳定追加；
- 高水位滚动后创建新 epoch；
- 支持 context reset；
- 限制进程内 epoch 状态数量；
- 处理并发 turn，禁止旧 turn 回退锚点。

### T3：建立可信引用注册表

- 从真实 `EventRecord` 和当前入站事件构建；
- 分配 `uN`、`mN`、`gN`、`qN`；
- 记录来源和可修改性；
- turn 内冻结。

### T4：实现主 Agent 消息块渲染

- 连续同发送者合并；
- 每条事件独立一行；
- Reply 使用短消息引用；
- Mention 使用短用户引用；
- 当前消息保持单独传入；
- 外部事件保持不可信标记。

### T5：在裁剪前使用紧凑投影

- ContextAssembler 使用块渲染结果计算预算；
- 历史高低水位逻辑继续绑定真实事件 ID；
- 不影响 Planner、Memory、自省和审计。

### T6：改造模型可见工具 Schema

- 删除模型可填写的真实 ID 参数；
- 改为 `user_ref/group_ref/message_ref`；
- Schema 使用固定 pattern；
- 不动态写入 enum。

### T7：实现工具引用解析层

- 工具执行前解析引用；
- 校验 epoch；
- 校验来源；
- 校验当前群成员；
- 校验读取/修改权限；
- 转换为真实底层参数。

### T8：类型化 OneBot 工具

- 新增常用读写工具；
- 通用 OneBot 调用降为内部接口；
- 修改类工具要求强来源引用；
- 统一返回引用错误代码。

### T9：输出清理与日志

- 清理内部引用信封；
- 日志仅记录引用类型和错误代码；
- 禁止日志输出完整映射；
- 保留必要的审计事件 ID。

### T10：指标与诊断

- 增加历史压缩率；
- 增加引用解析成功率；
- 增加 cache hit rate；
- 增加 epoch 滚动次数；
- 增加首轮工具命中和模型请求数关联分析。

---

## 18. 测试要求

### 18.1 历史渲染测试

1. 同一用户连续发送 2 至 4 条；
2. 两名用户交替发言；
3. 同名群友尾号消歧；
4. Reply 同一块内较早消息；
5. Reply 其他用户消息；
6. Reply 与 Mention 同时存在；
7. Yuki 连续发送多条；
8. 当前消息发送者与最后一条历史相同；
9. 外部事件与普通消息相邻；
10. 窗口滚动后引用重建。

### 18.2 引用测试

1. `u1` 正确解析；
2. 未注册 `u999` 被拒绝；
3. 旧 epoch 引用被拒绝；
4. 历史用户可读但不可禁言；
5. 当前 mention 用户可以作为修改目标；
6. reply author 可以作为修改目标；
7. 显式 QQ 必须存在于当前消息；
8. 重名昵称不得自动解析；
9. 当前群外用户不得用于群操作；
10. 并行工具调用共享同一注册表。

### 18.3 工具测试

1. 所有模型可见工具不再接收原始 ID；
2. Schema 顺序和内容稳定；
3. OneBot 类型化工具正确转换；
4. 引用错误不触发底层调用；
5. `request_tools` 加载后仍可使用原引用；
6. 修改操作不能使用 `history` 来源引用。

### 18.4 回归测试

确认以下内容完全不变：

- Planner 历史格式；
- Planner 原生回复决策；
- Memory Worker；
- Self Reflection；
- Memory Audit；
- 历史重建；
- 数据库存储；
- OneBot 底层协议；
- 插件和自动化权限。

---

## 19. 验收标准

### 19.1 功能验收

- 主 Agent 能正确区分所有历史发送者；
- 主 Agent 能正确判断消息数量和 Reply 关系；
- 工具可以使用短引用完成真实 QQ 操作；
- 模型无法提交任意真实 QQ、群号或消息号；
- 历史引用不能用于未授权修改操作；
- 当前消息保持独立。

### 19.2 成本验收

在同一批线上样本上比较：

```text
旧格式字符数
新格式字符数
身份信封字符数
可见事件数量
可见历史时间跨度
prompt_tokens
cached_prompt_tokens
```

建议目标：

- 历史身份信封字符数下降不少于 50%；
- 相同预算下可见事件数量明显增加；
- 主 Agent 首次请求未命中 token 不上升；
- 工具 Schema token 不因动态引用变化。

### 19.3 安全验收

- 伪造引用全部被拒绝；
- 过期引用全部被拒绝；
- 历史来源引用不能执行高风险修改；
- 昵称不能直接解析为真实目标；
- 后端权限判断继续生效；
- 模型输出不泄漏真实映射。

---

## 20. 发布步骤

1. 先完成领域模型、注册表和纯渲染测试；
2. 以 feature flag 接入主 Agent 历史投影；
3. 只记录新旧格式字符对比，不执行引用工具；
4. 验证历史理解和缓存表现；
5. 改造只读工具使用短引用；
6. 验证只读引用解析；
7. 改造修改类工具和类型化 OneBot 工具；
8. 开启来源权限限制；
9. 小范围真实群聊试运行；
10. 观察错误率、模型请求数和缓存命中；
11. 全量启用；
12. 删除主 Agent 原始 ID 参数兼容入口。

---

## 21. 回滚方案

保留 feature flag：

```text
MAIN_AGENT_REFERENCE_ENVELOPE_ENABLED
```

关闭后：

- 主 Agent 恢复现有原始事件投影；
- 工具内部仍可保留引用解析代码；
- 数据库不需要回滚；
- Planner、Memory 和 OneBot 底层不受影响。

在正式删除旧参数前，引用模式和旧模式不得同时向模型暴露，以免模型混用。

---

## 22. 最终原则

本次改造的核心不是简单缩短显示文本，而是建立完整的模型标识符隔离层：

> **模型理解短引用，后端掌握真实身份。**

主 Agent 只负责理解：

```text
u2 在 m3 回复了 m1
对 u2 执行某个允许的动作
```

后端负责确认：

```text
u2 究竟是谁
m1 对应哪条真实消息
该引用来自哪里
当前用户是否有权操作
最终应传给 OneBot 哪些真实参数
```

这样可以同时获得更低 token 消耗、更稳定的缓存前缀、更清晰的工具参数和更严格的身份安全边界。
