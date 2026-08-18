# 表情与回复序列

表情是 Main Agent 的回复效果，由 `send_emoji` 请求。工具参数只有语义意图：

- `intent`：当前消息是否明确索要表情
- `mode`：`none/optional/preferred/emoji_only`
- `placement`：`before_text/after_text/only`
- `goal`：期望的社交作用
- `emotion`：目标情绪

Schema 禁止 `emoji_id`、路径和 URL。最终资产始终由后端选择。`emoji_only` 也必须走 Main
Agent，不再跳过上下文装配或 Agent。回复完成条件是至少存在一个可见输出；纯文字和需要正文
合成的语音仍不会把空响应当作成功。ReplySequenceManager 按“前置表情 → 文本消息 → 后置表情”
发送；选择失败时提供文字降级。新消息可以取消尚未发送的效果，已经发送成功的内容保留在账本中。

日常 `optional` 表情受 `emoji.spontaneous_frequency` 与近期账本中的真实投递比例约束，不新增
模型调用。用户明确索要表情时不受该频率限制。复杂请求仍由 Main Agent 正常生成正文并调用
`send_emoji`。

表情准备或发送失败由后端确定性恢复：optional 只跳过媒体并继续正文，preferred 保留正文并补一
条短说明，emoji-only 只发送失败说明。恢复不会重试原图、自动换图或重新进入 Agent。
