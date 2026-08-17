# EmojiFacade

`ctx.emoji` 是 Plugin API 2.0 的受控表情入口。按需申请 `emoji.read/collect/select/send/manage`；注册 `emoji.selection_signals.v1` 需 `emoji.hook`。

- `list/get/search` 返回安全元数据。
- `collect_current` 只读取当前真实消息附件。
- `select` 返回核心选择结果，不返回路径或图片字节。
- `queue_reply_effect` 把表情意图加入当前正常回复序列，不立即发送。
- `adopt/reject/ban` 需要高风险 `emoji.manage` 批准。

选择信号接收核心候选的安全描述和基础分，返回候选 ID、分数增量、原因与置信度。Host 会忽略候选集外 ID，并隔离超时或异常。
