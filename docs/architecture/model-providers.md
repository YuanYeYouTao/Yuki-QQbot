# 模型供应商与协议合同

模型由 `ModelTask → ModelProfile → Provider` 显式绑定。更换供应商只影响协议转换；
主 Agent、插件、自动化、记忆、媒体与 Work 共用原执行层，不按消息内容自动选择模型。
部署向导可选择主模型供应商和协议；多个 Profile 可分别配置 endpoint、密钥环境变量和任务路由。
默认示例仍是 DeepSeek，不会自动覆盖部署中的 `.env`、模型路由或人格提示词。

## 协议与能力

| Yuki 能力 | Chat Completions | Responses | Claude Messages | Gemini GenerateContent |
| --- | --- | --- | --- | --- |
| 固定工具声明、并行调用与逐项回执 | 支持 | 支持 | 支持 | 支持 |
| 图片输入、按需媒体工具 | 支持，需声明模型能力 | 同左 | 同左 | 同左 |
| 结构化任务 | function tool / JSON Schema | 同左 | 同左 | 同左 |
| 截断判定、同 Work 续跑与预算 | 支持 | 支持 | 支持 | 支持 |
| 独立思考通道与协议状态 | reasoning_content / reasoning_details / encrypted_content | reasoning items | 签名 thinking blocks | thoughtSignature parts |
| 外部搜索、MCP、插件、终端等本地工具 | 支持 | 支持 | 支持 | 支持 |
| 上游原生搜索 | 显式配置的搜索专用 Profile | 依 Provider 能力；DeepSeek 主调用关闭 | 暂不声明 | 暂不声明 |

这里的支持指适配器与 Yuki 执行合同通过离线协议回放，不代表任意同名模型都支持这些功能，
也不代表各家服务已完成真实 API 或 QQ 验收。Profile 的能力声明必须符合实际模型。
原生搜索不能假装为客户端函数；Claude server tools 和 Gemini grounding 当前明确拒绝。
主 Agent 使用完整函数合同，Chat 搜索专用模型不能混用该合同；它们也不能通过 `tool_choice=none`
保证禁用服务端搜索。需要联网的主 Agent 可配置现有外部搜索工具。

## Chat 供应商预设

`provider` 用于选择参数方言，不通过模型名称猜测，不在上游 400 后自动删参数重试。

| provider | 思考参数与差异 |
| --- | --- |
| openai、azure_openai | reasoning_effort、max_completion_tokens；不回传非标准 reasoning_content |
| deepseek | thinking.type=enabled + reasoning_effort；不发送 tool_choice；合法整段 DSML 转为声明内工具调用 |
| qwen | enable_thinking + thinking_budget |
| moonshot、zhipu | thinking.type=enabled；不同型号的 effort 支持须显式配置 |
| doubao | thinking.type=enabled + reasoning_effort；保留 encrypted_content |
| minimax | 声明思考专用模型；reasoning_split=true，完整保留 reasoning_details |
| openrouter | reasoning.effort、reasoning.exclude=false，保留 reasoning_details |
| groq | reasoning_effort、max_completion_tokens、include_reasoning=true；默认针对 GPT-OSS 方言 |
| mistral | reasoning_effort 最低发 high，保留 thinking 内容块；须选支持该字段的模型 |
| siliconflow、together、xai、openai_compatible | 通用 reasoning_effort 方言，必须选支持该字段的模型或显式覆盖 |

Azure 仅接入 `/openai/v1/` API，model 填部署名；旧的 deployment URL 和 api-version 路径不在此预设内。
本适配器使用 Bearer key，Azure v1 支持该鉴权方式。
Chat 默认省略 temperature，避免思考模型不支持或仅支持固定温度；需要时显式启用。
上游支持多种接口不意味着可以跨协议续跑同一私有状态。

### 按模型覆盖参数

在原 Profile 内添加 `wire_options`，只覆盖声明的字段，其余仍用供应商预设。例如：

```toml
[profiles.main.wire_options]
reasoning = "effort"             # 例如使用 reasoning_effort 的 Kimi 型号
token_field = "max_completion_tokens"
send_temperature = false
effort_levels = ["low", "high", "max"] # 可选，按具体型号声明支持的档位
```

可用 reasoning 方言：`effort`、`thinking`、`enable_thinking`、`openrouter`、`builtin`；
Claude 使用 `effort`（adaptive）或 `budget`，Gemini 使用 `gemini`（thinkingLevel）或 `budget`。
`thinking` 可用 `send_reasoning_effort=true` 表明该型号同时支持 effort。
`builtin` 只适用于已经验证、始终思考且不接受思考控制参数的模型，不能用来接入无思考模型。
没有 effort 控制的 `thinking` / `builtin` 方言拒绝高于 low 的请求，不静默降低要求。
声明 `effort_levels` 后，取不低于请求的最小支持档位，没有更高档位则在请求前拒绝。
因此 Mistral 支持 none/high 的型号用 high 满足 low 下限，DeepSeek medium 映射 high。
Groq 的其他型号可显式设置 `reasoning_format="parsed"`；此时不发送 include_reasoning，
两种字段不混用，不按模型名猜测方言。

预算接口用 `thinking_budget_tokens` 表示 low 的预算（默认 4096，至少 1024）；
medium/high/xhigh/max 分别为该基数的 2/4/8/16 倍。这是一项明确的单调转换策略，
不声称与供应商 effort 精确等价。Claude 手动思考预算必须小于输出预算，否则在请求前拒绝；
其他供应商的型号上限也须自行核对，不能靠降低预算掩盖不支持的配置。

旧型号 Claude 可配置 `reasoning="budget"`；Gemini 2.5 可配置同一方言。
Gemini 3 各型号支持的 thinkingLevel 可能不同，不支持的档位由服务明确拒绝。
Claude 思考开启时不支持强制调用特定工具，适配为 auto；结构化任务仍由现有 Runner
严格检查恰好一个 emit_result 及 schema，不增加隐藏模型调用。

`headers` 支持 OpenRouter 的站点标识、Anthropic beta 等非鉴权 Header；不能覆盖认证字段，
不能放换行或把密钥写入 TOML。密钥只从 `api_key_env` 获取；客户端不跨供应商或密钥来源共享。
新增供应商示例见 [多供应商配置](../../config/model_profiles.providers.example.toml)。
无 TOML 的兼容配置也使用同一个客户端池；显式 `LLM_PROVIDER=anthropic/gemini`
分别采用对应原生协议，其他兼容供应商保持 Chat。额外命名的 endpoint/model/key 变量需要
存在于进程环境；Docker Compose 的 env_file 会加载 `.env`。本地 CLI 若只使用 Settings
读取 `.env`，其默认 LLM/LLM_FLASH 字段可用，额外变量须先导出，不会偷偷扫描其他密钥文件。

## 私有状态与恢复

- 每次响应的工具 ID、签名思考和原生块按原顺序保存在私有 ProviderContinuation；
  工具回执和用户改向按到达顺序追加，不重建已有调用，不重发已确认效果。
- Gemini 无原生 call ID 时，仅在接纳响应时生成确定性内部调用 ID；原始 thoughtSignature
  原样保存，内部映射不发给上游。它不是平台 message_id。
- WorkJournal 保存完整检查点；进程重启保持执行身份、请求链、预算、调用 ID 和实际 HTTP 请求。
  供应商、协议或 Profile 修订变化必须显式开新链，不能把旧签名状态拼入新模型。
- 已冻结的公共聊天投影当前仅支持 Responses 原生块；其他协议的私有检查点只属于当前 Work。
  提交公共投影时显式结束该缓存视图，下一任务从普通历史开始新链，不改写旧聊天事件。
  因此不能宣称这些协议在跨 Work 的缓存复用上与 Responses 完全一样。
- Chat `length`、Claude `max_tokens`、Gemini `MAX_TOKENS` 均转为 INCOMPLETE；
  Runner 的既有截断处理不会执行其中的工具。不自动增加预算。
- reasoning、签名、完整工具回执不进入对外消息或内容日志；只有显式 send_message 交付。
- 请求原生服务端工具时，传输结果不明不自动重试；普通有界传输重试仍计入 Work 请求预算。

## 验证边界与协议来源

定向测试覆盖参数、图片/schema、截断、私有签名、工具结果顺序、SQLite 重启后的 HTTP 字节一致性，
以及真实 Runner/隔离数据库/假网关中的可见输出边界。没有进行付费 API 或真实 QQ 消息测试。
模型效果、供应商实时可用性、长上下文缓存和计费需独立验收。

- [OpenAI Chat 参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)
- [Qwen 思考参数](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)
- [Kimi 思考模型](https://platform.kimi.ai/docs/guide/use-thinking-models)
- [MiniMax OpenAI 接口](https://platform.minimax.io/docs/api-reference/text-openai-api)
- [Groq 思考协议](https://console.groq.com/docs/reasoning)
- [Mistral 思考内容块](https://docs.mistral.ai/studio/conversations/reasoning)
- [GLM 思考模式](https://docs.z.ai/guides/capabilities/thinking-mode)
- [Claude adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)
- [Claude 结构化输出](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [Gemini thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)
- [Azure v1](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/api-version-lifecycle?tabs=key)
