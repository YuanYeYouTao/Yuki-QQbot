# Provider 输出与可见消息边界

本文说明主 Agent 当前的交付边界，以及 [Issue #55](https://github.com/YuanYeYouTao/Yuki-QQbot/issues/55)
仍需上游验证的部分。它不是 DeepSeek 已修复或生产验收通过的声明。

## 当前实现

- Responses 的 `reasoning` item 与 `message/output_text` 分别解析；推理保留在独立字段和
  Provider continuation 中，不拼成最终正文。Chat Completions 同样保留独立的
  `reasoning_content`；Chat 的 reasoning_details/encrypted_content、Claude 签名块与 Gemini
  thoughtSignature 同样保存在私有检查点，协议边界见[模型供应商合同](model-providers.md)。
- 主 Agent 普通聊天、主动轮、自动化和插件主入口只通过显式 `send_message` 调用交付正文。
  最终正文是内部结果，不能自动发送；附带工具调用的正文也不是发送指令。
- DeepSeek 现有 DSML 兼容路径可把整段合法工具标记转换为声明内的 function call，再经过
  同一工具授权与回执执行；混杂自然语言或未声明工具会被拒绝。因此这里的正文边界指
  协议解析后的正文，不声称原始 `output_text` 中合法的 DSML 工具调用会被忽略。
- 直接聊天如果首次只返回非空最终正文，且没有发送尝试和已接纳工作，Runner 在原链追加
  一次未送达反馈。再次遗漏即失败，不把未发送的文字标成已回复。该反馈不会删除或改写
  前一次响应、重排工具声明或替用户生成发送内容。
- Chat Completions 的正文纠正、新输入改向和完成回执拒绝会追加完整的 assistant 消息，
  保留独立 `reasoning_content`；不能因正文未发送就丢弃同响应的推理通道。
  Runner 已收到空 ChatResponse 时的内部重试遵守同一规则；适配器直接抛错、没有返回
  ChatResponse 时，不补造 assistant 消息。现行 HTTP 适配器会在解析时拒绝完整的空响应。
- 显式命令、模型不可用时的运维反馈及静态字面量自动化按各自合同交付。
  生成式自动化由主 Agent 显式发送，不再通过 DSL 发送内部最终正文。

## DeepSeek 的缓解与限制

Issue #55 记录了 `reasoning.effort=none` 时，Provider 把内部规划作为 `output_text` 返回的
情况，且当时 `reasoning_tokens=0`。适配器不能根据此类正文的措辞可靠恢复丢失的通道信息。

现有缓解保持不变：底层 DeepSeek 适配器在 `thinking_enabled=false` 时省略 `reasoning`；
这使用 Provider 默认行为，不等同于关闭思考。应用 ModelProfile/TaskModelExecutor 还会
把低于 `low` 的思考配置提升到 `low`。底层协议适配器仍能表达显式启用时的 `none`，
不能把底层映射测试误当成主 Agent 线上配置。

显式发送工具阻止的是“任意模型正文自动流入聊天”。如果模型把不合适的内容主动填入
`send_message.text`，这仍是模型生成质量问题，客户端不宣称能自动识别全部内部规划。
不新增基于关键词、正则或另一个判断模型的正文过滤器，也不借此改变固定工具合同。

## 回归与关闭条件

`tests/integration/test_visible_output_boundary.py` 使用合成响应和 MockTransport，贯穿
真实协议适配器、Runner、发送工具、假 QQ 网关及隔离 SQLite 账本。四种协议分别验证：

1. 最终正文包含规划文字时只在原链追加一次未送达反馈；重复遗漏不会把正文发送出去。
2. 显式发送与规划正文同时存在时，只发送工具中的目标文本；后续最终正文不重复发送。
3. 独立推理和被错误标成正文的内容均不进入对外消息或 outbound 账本。
4. 纠正和工具续跑保留真实历史前缀及完整工具声明，Responses 固定 instructions 不变。

这些测试证明本地交付边界，不证明 Provider 在真实长上下文下不再污染 `output_text`，
也不代表线上缓存命中率。因此本轮不关闭 #55，不恢复主调用的 `effort=none`。
未来需经授权在隔离环境对相同模型、长聊天上下文、混合工具调用做真实协议探测，核对
实际 output item、usage 和最终发送行为；不得只看一个短问题的回答或 reasoning token 数。
生产日志继续只记录无正文的协议诊断，脱敏的原始探测证据另行保存。
