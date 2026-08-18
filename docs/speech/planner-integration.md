# 语音回复效果

主聊天只保留一条执行链：

```text
MessageProcessor → Conversation Runtime → Memory Runtime → Capability Runtime
  → Main Agent → ReplySequenceManager
```

没有语音关键词路由，也没有独立语音会话。用户明确索要语音时，Main Agent 调用 `send_voice`；
日常主动语音受 Conversation Runtime 的 cadence 与 `speech.spontaneous_frequency` 约束。

## 明确请求与 Agent 工具

Main Agent 根据自然语言和上下文判断是否调用 `send_voice`，不依赖固定词表。该工具只能选择
公开风格与 `auto/zh/jp`，不能传入模式、profile、模型、参考音频、文件或路径。未授权时直接
伪造调用会得到 `voice_not_authorized`。最终是否发送语音以后端回执和 ReplySequence 为准。

## 日常主动语音

用户未明确索要时，是否允许主动语音由 Conversation Runtime 结合下列可信上下文决定：

- 人物偏好：`text_only`、`auto` 或 `prefer_voice`；
- `speech.spontaneous_frequency`（环境变量 `SPEECH_SPONTANEOUS_FREQUENCY`）；
- 当前会话最近中性回复轮次与其中的语音轮次；
- 声线、Worker、私聊/群聊开关及可用语言/风格。

后端根据脱敏 `reply_effect_events` 计算确定性的频率预算；明确索要语音与明确拒绝语音不进入
该统计，聊天正文也不用于计数。`text_only` 或预算不足时，中性轮次收紧为文字。频率是上限
预算而不是强制配额，允许语音时仍可只发文字。

`0039` 曾把旧 `planner_runs` 的中性语音轮次回填为 `source=migrated_planner`；`0040` 删除
`planner_runs` 后，cadence 只读新表。哈希盐 `yuki-planner-v1` 不得改写。

## 持久人物偏好

`person_speech_preferences` 以 QQ 为主键，只保存一个当前模式、来源消息 ID 和时间。只有用户
本人在真实消息轮中明确表达“以后、默认、切换模式”等持续语义时，Main Agent 才能调用
`set_voice_preference` 写入 `persistent` 修改；只约束当前轮的要求不会落库，自主群聊也不能
修改人物偏好。删除人物时该行通过外键级联删除。

未保存人物偏好时，`SPEECH_DEFAULT_MODE` 作为全局基线：

- `text` → `text_only`；
- `optional` → `auto`；
- `voice` / `text_and_voice` → `prefer_voice`。

## 语言、失败与可观测性

默认声线只公开目标语言，Main Agent 可以按语境选择中文或日文。合成前仍按最终正文脚本校验
语言，避免把中文正文交给日语 G2P。新消息通过既有 TurnCoordinator 取消未发送的旧语音；TTS
不可用时按原有文字回退策略处理。
