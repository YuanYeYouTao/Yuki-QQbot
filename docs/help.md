# Yuki-QQbot

> **3.7.0 正式版：**群聊短期会话统一为 Bot + 群，Actor 只影响当前轮。旧分层 History
> Rollup 替换为每个 Scope 一个连续检查点；摘要作为不可信 input，不进入 Provider
> instructions。Alembic head 为 `0042`，Plugin API 仍为 `2.0`。这是破坏性升级，请先读
> [3.7.0 升级指南](upgrade-3.7.0.md)。

> **3.6.0 正式版：**删除强制 Planner。普通聊天由 Conversation Runtime 准入后直接进入 Main Agent；
> Memory Runtime 与 Capability Runtime 在模型前完成本地准备。当时 Alembic head 为 `0040`，
> Plugin API 为 `2.0`。从 3.5.3 升级见 [3.6.0 升级指南](upgrade-3.6.0.md)。

> **3.5.3 正式版：**新增 `qq-ai-bot-cli setup` 彩色引导配置和 Linux / Windows 安装器。
> 首次部署只需 Docker；可选能力按需提问，密钥不回显，确认后原子写入并自动完成 Compose
> 启动、健康检查和 NapCat 登录提示。当时 Alembic head 为 `0036`，Plugin API 为 `1.1`。

> **3.5.2 正式版：**部署改用固定版本 GHCR 镜像和无源码 Release 部署包；生产 Compose 不再
> 本地构建 Bot 或语音 Worker。升级时先修改 `.env` 中的 `YUKI_VERSION`，再执行
> `docker compose pull` 和 `docker compose up -d`。本版本没有新增数据库迁移。
>
> **3.5.1 正式版：**新增自适应记忆生命周期。召回使用结构化意图、Activation 衰减与有界强化；
> 自动召回、显式工具读取和记忆变更使用互斥路径，归因在发送后异步完成。记忆写入由独立
> mutation 完成门约束，DeepSeek 请求不携带不受支持的 `tool_choice`。Alembic head 为 `0036`。

> **3.5.0 正式版：**新增 Memory Dream 与 MMR。Dream 在 Self Reflection 之后按语义整理
> 既有长期记忆，支持长 Episode 切分、压缩、Evidence provenance、持久化 Preview、回滚和
> 历史 Evidence 后台压缩；普通相关性召回使用 MMR 减少重复。

> **3.4.4 Prompt 缓存与输出清理：**历史位于动态上下文之前并使用高低水位分块滚动；首批
> 工具和 Schema 采用宽松预算且按名称稳定排序；输出清理器兼容省略消息 ID 的身份头。

> **3.4.3 会话身份与短期上下文：**`chat_events` 保存发言时昵称和群名片，Prompt 中的每条
> 消息都自带发送者、QQ、消息 ID 与回复目标；Planner 当前消息不再重复进入历史。Alembic
> head 为 `0029`。

> **3.4.2 GitHub Release 卡片：**Release 事件新增与 Push 同风格的中文 PNG 卡片，展示
> 版本类型、目标分支、附件数量和发布说明；不新增数据库迁移。

> **3.4.1 GitHub Monitor：**新增多仓库 GitHub 事件监控、中文 Push 卡片、持久通知 Outbox
> 与可选 Yuki 点评；Plugin API 升级为 `1.1`，Alembic head 为 `0028`。

> **3.4.0 自由 Agent 自动化：**scheduled automation 与普通会话共用授权工具和插件注册；
> Agent 可以在创建者权限内自主查询、发送消息并管理后续自动化。Planner scope 只决定首批
> 工具优先级，不再截断其余已授权能力；DeepSeek Responses 多轮工具续写保持完整配对。

> **3.3.1 Web Provider 规则路由：**混合模式支持 Tavily 关键词和域名直达 Tavily；
> DeepSeek 原生访问失败、没有打开明确目标 URL 或无法提供所需来源时，只允许一次 Tavily
> 回退。Router 不改变 Planner 的 web 授权，也不会强迫 Agent 调用联网工具。

> **3.3.0 DeepSeek Responses API 与原生联网：**主聊天模型可使用 `/responses`、Function Tool
> 续接和 Provider 原生 `web_search`。`WEB_MODE` 支持 native、tavily、原生优先有界回退和禁用；
> 自动化识别只追加候选 scope，不再覆盖 Planner 已选择的 web、memory、MCP 等能力。

> **3.2.0 Yuki 自我长期记忆：**新增 `self` 作用域、按需 SELF RAG、统一 `memory_change`
> 自我变更和只读 `get_self_memories`。SELF 事实按 global、private、group 可见范围硬隔离；
> Alembic `0027` 为非破坏性迁移，功能通过 `SELF_MEMORY_ENABLED` 控制。

> **Memory Mutation V2 开发版：**新增唯一的 `memory_change` Agent 写工具和统一
> `MemoryMutationService`；Alembic head 为 `0026`。Agent、自动 Worker、记忆命令、管理员、
> 插件和可恢复的有界后台治理共享事务回执与双指纹去重。群友读取以当前群 `person_group`
> 为基础，并只读投影由目标本人在本群 evidence 支持的 `person`；不会暴露 evidence、其他群
> 事实或全局记忆修改权限。

> **3.0.3 日常表情节奏：**新增可热修改的 `emoji.spontaneous_frequency`，默认 `0.15`。
> Planner 复用近期账本中的真实投递记录限制自发 `optional` 表情；明确索要表情不受影响。
> 本补丁不新增数据库迁移，Alembic head 仍为 `0024`。

> **3.0.2 表情可靠性补丁：**修复群聊表情作用域 SQL、Planner 超时放大和媒体“准备成功即
> 发送成功”的误判。明确索要表情时不调用 Planner LLM 或 Chat Agent；只有 OneBot 真实回执
> 会进入成功账本。失败时返回确定性短文字，不重试图片。

> **3.0.0 正式版：**Memory V2 六阶段已收口。正式版不新增生产数据库迁移，Alembic head
> 仍为 `0024`；新增版本化合成质量基准、严格污染门禁、内容无关生产审计、指纹保护的显式
> hygiene 和契约快照。升级或启动不会运行基准、扫描历史或自动修复数据库。

## 启动项目

> **3.0.0a1 破坏性升级警告：**Alembic `0020` 会永久删除全部旧人物记忆、群记忆、
> 群内人物记忆、偏好和旧记忆任务。聊天事件账本、人物、群、关系、自动化和插件数据会保留；
> 新记忆库从空库开始，也不会自动扫描历史聊天重建。升级前必须完整备份 `data/`，唯一回退
> 方法是恢复该备份。详细步骤见 [Memory V2 升级指南](upgrade-memory-v2.md)。
>
> **3.0.0a2：**Alembic `0021` 只新增可重建的 Memory V2 FTS5 派生索引，不删除事实、
> 证据或聊天账本。普通聊天现在按当前问题检索相关事实，不再固定加载大量长期事实。
>
> **3.0.0b2：**Alembic `0023` 非破坏性增加 Memory V2 冲突、证据权威、状态审计与生命周期。
> 现有事实、证据、FTS、Embedding 和聊天账本都会保留；存在未解决 contested fact 时不允许
> downgrade。升级前仍建议备份 `data/`。
> Embedding 默认关闭；开启后使用 Qwen DashScope 生成 1024 维向量，并与 FTS 通过 RRF
> 融合。外部服务不可用时自动退回词法检索，不影响聊天和事实写入。
>
> **3.0.0rc1：**Alembic `0024` 非破坏性增加受控历史记忆重建。升级和启动不会扫描历史；
> 只有当前真实 `SUPERUSERS` 发送者显式 plan/start、人工审阅并 commit 后才会写入事实。
> 进程重启会暂停而不会自动恢复。操作前仍应备份 `data/`。

当前 Plugin API 为 `2.0`。声明 `1.0` / `1.1` 的插件会被拒绝；从 1.x 升级见
[Plugin API 2.0 迁移](plugin-development/api-2.0-migration.md)。第三方插件若把 `yuki_requires`
上限写成 `<3.0`，需要在确认兼容后改为 `<4.0`；不要根据 Yuki 产品版本猜测 Plugin API 版本。

从 GitHub Release 下载 `install.sh` 或 `install.ps1`。Linux 执行：

```bash
chmod +x install.sh
./install.sh
```

Windows PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

向导只要求基础主模型配置，并按你的选择继续配置 Flash、Embedding、Web、Vision、MCP、Plugin、
Automation 和 Speech。它不会在线试用 API Key；完成后再打开
<http://127.0.0.1:6099> 登录 NapCat。之后日常启动只需：

- 在任意问题处输入 `:back` 返回上一逻辑页；当前页尚未确认的输入会被撤销。必须输入开头的
  英文冒号，单独输入 `back` 不会生效。
- 输入 `:quit` 或按 `Ctrl+C` 安全退出；确认写入前不会修改任何配置文件。单独输入 `quit`
  不会生效。
- 已有部署的区块选择页直接回车表示“不修改”；输入逗号分隔的编号可一次选择多个区块。
- 从重跑的第一个配置页输入 `:back` 会回到区块选择页，而不是退出或保存半成品。

```bash
docker compose up -d
```

语音功能默认关闭，不影响原有纯文字启动。准备好本地 GenieData 和声线后，使用：

```bash
docker compose --profile speech up -d
```

停止服务：

```bash
docker compose down
```

不要添加 `-v`，否则可能删除持久化数据。NapCat WebUI 地址为 <http://127.0.0.1:6099/webui/>。

## 项目定位

Yuki-QQbot 是基于 Python 3.12、NoneBot2、OneBot v11、NapCatQQ、SQLite 和 OpenAI-compatible Chat Completions / Responses API 的人物中心 QQ Agent。当前运行时路径见 [3.6.0 架构](architecture/yuki-3.6.0-runtime.md)。

- QQ 号字符串是人物的全局唯一身份。
- 当前消息发送者的 QQ 是否属于 `SUPERUSERS`，是唯一管理员凭证。
- 同一 QQ 的私聊、不同群成员关系和人物记忆关联到同一个人。
- Memory V2 将长期信息存为带作用域、版本状态和真实消息证据的事实；自动提取只能选择当前
  发送者 `speaker` 或当前群 `group`，不能让模型填写 QQ 号、群号或事件号。
- 可选 Memory V2 混合 RAG 在 SQL 完成人物/群硬过滤后，融合 FTS/BM25 与 Qwen 语义召回；
  向量只是可重建派生数据，不会决定事实属于谁，也不会替代关系数据库事实源。
- 群号区分群；已启用群的全部消息都会被观察并永久写入事件账本。
- 私聊默认向所有 QQ 开放；`/ai private <QQ> off` 用于阻止指定用户。
- 当前人物事实可以在私聊与群聊间自然复用，群事实和当前人物的群内事实严格按群隔离；
  其他群友的长期事实默认不进入当前上下文。
- 机器人支持 DeepSeek Chat Completions / Responses API、普通/思考模式和多轮工具调用。
- 内置 Tool Kernel 将 Core、Admin、Automation、Plugin 与 MCP 工具统一为同一目录、
  Capability namespace、Binding、结果预算和 AgentRunner 执行链。
- 可按 `.mcp.json` 接入 stdio 与 Streamable HTTP MCP Server；默认关闭，不影响原有聊天。
- 可选使用 Qwen3.7-Plus 作为独立视觉前端，动态思考并识别图片、虚构角色、图片表情、动态表情和回复图片；DeepSeek 仍是唯一主聊天模型并负责最终回复。
- 可选使用 DeepSeek Provider 原生联网搜索、Tavily 搜索或原生优先的有界 Tavily 回退；来源仍由后端严格保存、隔离和显示。
- 每个 QQ 拥有独立、持久化的好感度和信任度，关系阶段会自然影响 Yuki 的语气。
- 关系分数不会改变程序权限；只有当前真实发送者属于 `SUPERUSERS` 才能获得管理员工具。
- 超级管理员可以用自然语言管理注册配置、关系、记忆、偏好、群和私聊准入。
- 运行时配置保存在 SQLite，不修改 `.env`；所有修改都有脱敏审计，配置覆盖可安全回滚。
- 每轮聊天获得后端可信当前时间；每个 QQ 可保存独立 IANA 时区，历史消息按本地时间显示。
- 普通用户和超级管理员都可以用自然语言创建自己的持久化自动化任务；普通用户严格限于本人和当前群，超级管理员可显式委托现有管理员与 OneBot 能力。
- 默认启用 Conversation Runtime：私聊、真实 `@` 与回复机器人由 Host 直接准入；已启用群的未触发消息先观察，静默窗口后由本地评分决定是否开一轮只读 Main Agent。
- 新消息可以中断过期的自主轮、自主生成和尚未发送的旧消息序列；已经开始的修改型业务操作不会被自动取消。
- 提供 Plugin API 2.0、独立 `yuki_plugin_sdk`、Manifest/批准/权限/事件/Prompt/AdmissionSignal 扩展点和无网络测试 SDK；插件系统默认关闭。
- 插件可以创建与主聊天账本、人物记忆分离的持久或临时 AI 会话，适合骰子跑团等连续任务；插件拿不到模型隐藏推理，也不能伪造超级管理员。
- 内置持久化表情系统会按配置观察图片、保存原图与静态预览、复用 Qwen 视觉分类、自动采用合格表情，并由 Main Agent 通过 `ReplyEffect` 在正常回复序列中选择发送。
- 可选启用完全本地的 Genie-TTS 2.0.2 Worker，使用部署者自行准备的 GPT-SoVITS V2/V2ProPlus ONNX 声线和多参考风格发送 QQ `record`，不调用云端 TTS。

### 当前架构约束

- 所有主模型调用都以 `ModelTask → ModelRoute → ModelProfile → ModelClientPool` 执行；聊天、自动化 Agent 和插件 Agent 会话默认走 Pro，记忆提取、关系评价、表情替换和辅助结构化任务默认走 Flash。路由缺失或能力不兼容会在启动时明确失败，不会静默回退到 Pro。
- 记忆提取、关系评价与表情替换共用 `StructuredTaskRunner`，输出结构直接来自 Pydantic Schema；不再解析 Markdown JSON fence 或从自由文本中猜测第一个花括号对象。
- Prompt 由不可变 `PromptContribution` 经 `PromptCompiler` 组成一个稳定静态前缀和一个紧凑动态 Envelope；人物、群、关系、记忆和插件资料由通用 `ContextContribution` 预算器按 required、priority、relevance 和 cost 选择。
- 工具访问由 `CapabilityDescriptor` 的 effect、risk、trust source、origin 和权限元数据决定；Capability Runtime 只会在已授权目录内搜索和收窄工具，图片、网页和插件资料不能扩大权限。本地表情和语音统一作为 `ReplyEffect` 进入既有回复序列。
- Capability Search 只接收紧凑 namespace 摘要与本地 FTS 命中；主 Agent 只会获得本轮暴露的
  完整工具 Schema。MCP 未启用时，普通聊天不会增加 MCP Schema Token。
- 配置启用的 MCP Server 是可信工具来源，但返回内容仍作为外部资料处理。MCP 不增加逐工具审批，
  可用性只由配置、Server 启停状态和既有能力策略决定。
- 组合根通过不可变 Bundle 装配 Persistence、ModelRuntime、Web、Media、Emoji、Speech、Conversation、Admin 和 Automation Module；`LifecycleRegistry` 负责按注册顺序启动、反序关闭、失败回滚及模块健康检查。
- 根 `Settings` 保留 1.x 的扁平 `.env` 名称作为兼容入口，同时组合不可变的 App、OneBot、ModelRuntime、Conversation、Plugin、Memory、Relationship、Web、Vision、Emoji、Speech 和 Automation 领域设置。
- 正常聊天、管理员自然语言操作、联网和自动化创建继续使用同一个聊天 Agent。插件独立 AI 会话只服务插件任务，不是第二套管理员人格或主聊天路由。
- `ContextAssembler` 统一装配人物、群、关系和近期事件，并用 `MAX_CONTEXT_CHARACTERS` 限制动态上下文总量；当前消息优先保留，低优先级旧资料先裁剪。
- `PromptComposer` 集中生成后端可信的时间、权限、关系、视觉和联网规则，业务服务不再各自拼接一套运行说明。
- 持久化仓储按人物与访问、事件账本、Memory V2、关系、媒体和联网来源分域实现；
  `qq_ai_bot.memory` 负责事实、证据、身份映射、提取和上下文投影，Repository 不包含 Prompt；
  `persistence.repositories` 仅作为其余仓储的稳定门面。
- `/ai` 确定性命令由 `CommandService` 调度并绕过 Main Agent；普通聊天由 Conversation Runtime 准入后进入 `ChatService.handle_turn → AgentRunner → ReplySequenceManager`，`MessageProcessor` 继续负责观察、账本、视觉和最终异常边界。
- 运行时配置注册表只负责查找、别名和类型转换；热更新、仅影响未来、需重启、受保护/密钥配置分别维护在独立声明目录中。
- 相关人物按批次读取，避免群聊中按 QQ 串行查询多组资料；群名片仍严格按当前群号隔离。
- SQLite 使用 WAL 和短事务支持同一 Application 内的后台 Worker。3.7.0 的 EffectGate 只在单进程内线性化外部效果；同一数据库禁止运行两个主动 Bot 实例。
- GitHub Actions 会在推送和 PR 时执行 Ruff、严格 mypy、pytest、Echo 示例插件契约测试、Alembic 全新安装和 Docker 构建。
- GitHub Actions 还会单独验证 Prompt benchmark、模型路由和无真实模型下载的 Genie Worker 日语前端测试。
- GitHub Actions 的独立 `memory-quality` job 使用合成数据、Fake Model 和 Fake Embedding 运行
  Memory V2 全量基准、绝对门禁、冻结 baseline 比较，并上传无生产内容的 JSON/Markdown/JUnit。

Memory V2 质量、合成大库性能基准与生产审计命令见
[运维手册](operations/memory-quality.md)，指标分母见
[质量指标定义](architecture/memory-v2-quality-metrics.md)。

本版本不识别用户发来的语音，也不处理视频、PDF 和普通文件，不实现 ASR、实时语音通话、VAD 或 WebRTC。已启用群里未触发 Yuki 的图片可以按 `EMOJI_COLLECTION_MODE` 进入独立后台表情候选流程；这不会触发聊天回复、人物记忆、关系评价或管理员操作。普通聊天视觉理解仍只处理当前真实消息或回复中的图片。

## MCP 快速开始

1. 本机运行时复制 `.mcp.json.example` 为 `.mcp.json`；Docker 部署时复制为
   `config/mcp.json`，并在 `.env` 设置 `MCP_CONFIG_PATH=/app/config/mcp.json`。
2. 在 `.env` 中设置 `MCP_ENABLED=true`；令牌、Cookie 等仅通过 `${ENV_NAME}` 引用。
3. 重建 Bot：`docker compose up -d --no-deps --force-recreate bot`。不要重建或退出 NapCat，可保留 QQ 登录状态。
4. 用 `/ai mcp list` 查看状态；超级管理员可执行 `/ai mcp doctor <server_id>`。

Capability Search 只做本地 FTS5 BM25 检索，不再使用 Planner 或 Flash
Tool Selection。大量工具部署可设置
`TOOLING_SELECTED_TOOL_LIMIT`、`TOOLING_SCHEMA_TOKEN_BUDGET`、
`MCP_SELECTED_TOOL_LIMIT` 与 `MCP_SCHEMA_TOKEN_BUDGET`。默认采用宽松的首批预算，
遗漏能力仍可通过 `request_tools` 按需加载；显式留空表示不增加对应限制。

多步骤工具链可在 Server 的 `yuki.toolBundles` 中声明为通用 Bundle：

```json
"toolBundles": {
  "order_planning": {
    "scope": "mcp.mcd.order_planning",
    "summary": "只读规划订单：查询地址与门店、菜单详情和价格，但不创建订单",
    "includeTools": [
      "delivery-query-addresses",
      "delivery-query-stores",
      "query-nearby-stores",
      "query-meals",
      "query-meal-detail",
      "calculate-price"
    ]
  },
  "order": {
    "scope": "mcp.mcd.order",
    "summary": "查询地址与门店、菜单详情和价格，创建待支付订单并查询订单",
    "includeTools": [
      "delivery-query-addresses",
      "delivery-query-stores",
      "query-nearby-stores",
      "query-meals",
      "query-meal-detail",
      "calculate-price",
      "create-order",
      "query-order"
    ]
  }
}
```

Capability Runtime 按本地 FTS 命中与 Bundle namespace 决定首批 Schema；Flash 和工具数量限制
不能拆散已声明的 Bundle。若完整 Bundle 超过 Schema Token 预算，本轮会明确失败并指出 namespace，不会静默
留下半条工具链。`mcp_gateway` 的 search 只搜索、describe 只返回定义，call 仍按目标工具的
真实风险经过统一 CapabilityPolicy，不能借只读 Gateway 绕过 namespace、read-only、图片或联网限制。
若 Agent 在执行过程中发现所需工具未加载，可调用 `request_tools` 按能力描述从统一目录请求；
后端只会加载当前真实事件原本有权使用的完整工具 Schema；
它能找回预算省略项，但不能扩大本轮工具范围，下一步仍由目标工具自身执行和审计。
成功的 MCP 调用同时返回 `structuredContent` 和兼容文本时，Yuki 以结构化结果为准并丢弃重复
文本，避免菜单等大结果被字符预算截断成无名称的 ID 摘要；图片和资源块仍会保留。

需要让持久化任务调用某个 MCP 时，在该 Server 的 `yuki` 下显式配置自动化允许列表：

```json
"automation": {
  "enabled": true,
  "permission": "superuser",
  "includeTools": ["campaign-calendar", "query-my-coupons"]
}
```

桥接后的内部名称为 `mcp.<server_id>.<remote_tool_name>`。自然语言创建任务时，Yuki 只需从
TaskSpec Schema 提供的模型安全 ID 中选择能力，后端会解析为真实名称并保存创建者授权快照；
`agentic` 任务省略 `capabilities` 时继承创建者当前可委托工具域，显式列出只用于主动缩小范围；
无需手写名称或 Automation DSL。插件 SDK 和内部调用仍可直接使用底层 DSL。`includeTools`
不能为空；未列出的远端工具不会进入后台任务目录。默认 `permission=superuser`，只有需要明确
开放给普通用户的只读服务才应改为 `user`。远端 Schema 发生变化时，旧任务会停止执行并进入
委托失效状态，需要重新保存任务。

完整说明见 [MCP 文档](mcp/architecture.md) 与
[Tool Kernel 架构](architecture/tool-kernel.md)。麦当劳点餐、领券和积分能力可直接使用
[麦当劳官方 MCP 接入指南](mcp/mcdonalds.md)。

## 持久化表情系统

表情系统默认启用，视觉分类复用现有 `VISION_*` Provider。不存在第二套视觉客户端，也没有表情审核队列或审核模型调用：分类为表情后进入 `recognized`，满足 `EMOJI_AUTO_ADOPT_MIN_CONFIDENCE` 时直接进入 `adopted`。

- 状态：`candidate → recognized → adopted`；普通照片进入 `rejected`，管理员可 `ban`，文件丢失时标记 `missing`。
- 自动收集：`metadata_only` 只看 OneBot 明确表情字段；`likely` 还接受表情相关元数据；`all_images` 接受作用域内全部图片作为候选。
- 去重与文件：SHA-256 完全去重；可选 dHash 只标识近似候选，不会误删。原图保存到 `data/emoji/original/`，第一帧 WebP 预览保存到 `data/emoji/preview/`；GIF/WebP 原动画保持不变。
- 回复：Main Agent 只输出语义目标、情绪、模式和位置，不能指定文件或表情 ID；核心先粗排，再可选用候选拼图做视觉精排。`emoji_only` 由发送层直接执行，不再经过第二次 Agent 决策；日常 `optional` 受 `EMOJI_SPONTANEOUS_FREQUENCY`（默认 0.15）和近期真实发送比例约束，明确索要表情不受影响。发送可以位于文字前、文字后或仅发表情，并服从新消息取消与发送成功后计数。
- 隔离：OCR、描述、插件和网页都不能执行命令、改变关系或写人物记忆；数据库和日志不保存图片 Base64。

常用命令（仅真实 `SUPERUSERS`）：

```text
/ai emoji list [candidate|recognized|adopted|rejected|banned|missing]
/ai emoji show|adopt|unadopt|reject|ban|unban|reanalyze <ID>
/ai emoji pin <ID> on|off
/ai emoji group enable|disable
/ai emoji import              # 与当前图片一起发，或回复一张图片
/ai emoji stats|cleanup|doctor
```

自动化注册 `emoji.send` 和 `emoji.send_by_id`。普通用户只能委托发送给本人私聊或任务创建时的当前群；固定 ID 必须在创建任务时明确提供。插件 API 新增 `EmojiFacade`、`emoji.*` 权限、通知事件和 `emoji.selection_signals.v1`；插件只能调整核心候选分数，不能构造候选外 ID。完整设计见 [表情系统文档](emoji-system/architecture.md)。

## 完全本地 QQ 语音

1.8.0 的语音是独立可选服务：主 Bot 通过 Unix Domain Socket 调用无网络、无 HTTP 端口的 Genie Worker；Worker 只加载本地 GenieData、GPT-SoVITS V2/V2ProPlus ONNX 模型和参考音频，输出 32 kHz 单声道 16 位 WAV。主进程在 OneBot Adapter 边界把 WAV 编为 Base64 `record`，NapCat 不需要访问本地路径。

1.9.0 为日语合成增加完全离线的 e2k 前处理。部署者需要从固定的 `e2k==0.6.2` 安装包或其发布资产中手工取得 `model-c2k.npz` 和 `ngram.json.zip`，放入：

```text
data/speech/japanese_frontend/models/model-c2k.npz
data/speech/japanese_frontend/models/ngram.json.zip
```

仓库不会自动下载。项目词典位于 `data/speech/japanese_frontend/lexicon.toml`，格式为：

```toml
[words]
Yuki = "ユキ"
OpenAI = "オープンエーアイ"
ChatGPT = "チャットジーピーティー"
API = "エーピーアイ"
```

词典匹配不区分大小写；普通英文词优先走 C2K，缩写和不常见词优先走 NGram，最后按日语字母名确定性拼读。日语 Worker 输入若仍含拉丁字母会明确拒绝合成，不会静默发送原文；中文和英文合成路径不变。`/healthz` 与 `speech status` 会显示 `japanese_frontend_available`、版本和不含正文的缓存签名。可用 `SPEECH_JP_KATAKANA_ENABLED=false` 显式关闭。

同一声线可声明多种目标语言。Main Agent 可以按当前语境在中文和日文间自然选择，并生成对应语言的正文；后端还会根据最终文本中的中文汉字或日语假名再次校验，避免语言提示与实际文本不一致。参考音频的语言独立保存，因此日语参考音频也可以用于合成中文目标文本。

语音意图由 Main Agent 理解自然语言和当前上下文，不再由后端匹配“语音/文字”等固定短语。用户本轮明确索要语音时，Agent 可使用 `send_voice` 选择语气与语言；用户没有明确索要语音时，普通聊天是否偶尔发语音由语音运行时按人物偏好和 `SPEECH_SPONTANEOUS_FREQUENCY`（默认 0.15）以及近期投递账本决定。

语音账本只把实际朗读正文交给聊天模型；声线、参考风格、目标语言和生成 ID 仅保存在结构化 `record` 消息段，不会以 `[语音：Yuki 发送了一条语音，声线：…]` 的形式混入 Yuki 的上下文或下一次语音。包含语音的回复中，系统提示词要求自称使用 `ゆき`，避免日语 TTS 把 `Yuki` 读成英文字母；纯文字回复仍可使用 `Yuki`。

每个 QQ 可持久保存 `text_only`、`auto` 或 `prefer_voice` 模式。只有“以后都用文字”“以后可以偶尔发语音”等明确持续语义会更新偏好；“这次用语音说”只影响当前轮。`SPEECH_DEFAULT_MODE` 是尚未保存人物偏好时的全局基线：`text` 对应文字模式，`optional` 对应自动决定，`voice`/`text_and_voice` 对应偏好语音。CPU ONNX 模型可能占用数 GiB，Bot 启动时只同步声线元数据、首次合成时才按需加载模型；Worker 会主动归还空闲堆内存，并在 `SPEECH_WORKER_IDLE_RECYCLE_SECONDS`（默认 300 秒）后由 Compose 自动回收重启；设为 `0` 可关闭空闲回收。

仓库不会下载或附带任何角色模型、Galgame/动漫声线或原始语音，生产 Worker 也不安装 PyTorch。部署者必须确认模型权重和参考音频授权。准备流程、Manifest、转换、插件、自动化与排障见 [语音文档](speech/architecture.md)。

常用命令：

```text
/ai voice status
/ai voice profiles
/ai voice show <profile_id>
/ai voice styles [profile_id]
/ai voice test <文本>
/ai voice use|reload <profile_id>        # 超级管理员
/ai voice cache cleanup                 # 超级管理员
```

CLI 覆盖 `speech status`、`genie doctor`、profile 导入/检查/启停/设默认、reference 添加/停用、测试、缓存清理和 Worker 重启。模型转换工具位于 `tools/genie_model_converter/`，与生产运行环境完全分离。

## 首次配置

复制环境变量模板：

```powershell
# Windows PowerShell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

```bash
# Linux / macOS
test -f .env || cp .env.example .env
```

至少填写：

```dotenv
ONEBOT_ACCESS_TOKEN=一段长随机值
NAPCAT_WEBUI_TOKEN=另一段长随机值
SUPERUSERS=你的QQ号

LLM_PROVIDER=openai
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=你的DeepSeek密钥
LLM_MODEL=deepseek-v4-flash
LLM_THINKING_ENABLED=true
LLM_REASONING_EFFORT=high
```

机器人账号不填在 `.env`。它由 NapCat WebUI 中实际扫码登录的 QQ 决定。

长系统提示词建议放在不提交到 Git 的 Markdown 文件：

```powershell
Copy-Item config/system_prompt.example.md config/system_prompt.md
```

然后设置：

```dotenv
SYSTEM_PROMPT_FILE=config/system_prompt.md
YUKI_PERSONA_FILE=config/yuki_persona_core.md
```

`yuki_persona_core.md` 是主 Agent 与 Self Reflection 共用的核心人格；默认系统提示词通过
`{{YUKI_PERSONA_CORE}}` 引入它。旧的自定义 Prompt 没有占位符时保持原样，不会重复追加人格。
默认示例包含 Yuki 的完整人物设定、7 月 23 日生日、外貌、关系表达和自然口语例句；
日常聊天以一句轻松短回复为主，避免长难句，普通短回复不以中文句号收尾，并明确禁止 Emoji、颜文字及未经用户要求的括号动作描写。示例负责人格和表达方式，
权限、工具、视觉与运行时状态继续由后端动态上下文提供，避免在静态提示词里重复堆叠。

修改提示词后无需重建镜像：

```bash
docker compose up -d --no-deps --force-recreate bot
```

### Pro / Flash 模型任务路由

不配置 `config/model_profiles.toml` 时，1.9.0 会把现有 `LLM_*` 规范化为 `main` 档案并把全部任务显式指向它，同时记录一次弃用提示，行为与 1.8.2 一致。要启用 Pro / Flash 分工：

```powershell
Copy-Item config/model_profiles.example.toml config/model_profiles.toml
```

```dotenv
MODEL_PROFILES_FILE=config/model_profiles.toml
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=你的Pro密钥
LLM_MODEL=你的主聊天模型名
LLM_REASONING_EFFORT=high
LLM_FLASH_BASE_URL=https://api.deepseek.com
LLM_FLASH_API_KEY=你的Flash密钥
LLM_FLASH_MODEL=你的Flash模型名
```

TOML 只保存环境变量名称，不保存密钥。默认路由为：`chat_agent`、`automation_agent`、`plugin_agent_session` 使用 `pro`；`memory_extraction`、`memory_self_reflection`、`memory_consolidation`、`memory_dream`、`memory_attribution`、`relationship_evaluation`、`emoji_replacement`、`automation_text_generation`、`utility_structured` 使用 `flash`。同一 endpoint 会共享 HTTP 连接池，但每个档案仍保留自己的超时、重试、思考和输出默认值。schema v3 已删除 `planner` 与 `tool_selection` 路由。

可用诊断：

```bash
qq-ai-bot-cli model profiles
qq-ai-bot-cli model routes
qq-ai-bot-cli model stats
qq-ai-bot-cli prompt inspect direct-text
qq-ai-bot-cli prompt compare
```

聊天内超级管理员可用 `/ai model stats` 查看按任务和档案统计的调用次数、Token、缓存命中、平均延迟和最近错误；错误显示数由 `MODEL_STATS_RECENT_ERROR_LIMIT` 配置。统计表不保存 Prompt、用户正文、工具结果、隐藏推理或密钥。

### 可选：启用图片理解

图片理解默认关闭。使用阿里云百炼 OpenAI-compatible Chat Completions 接口时，在 `.env` 中填写：

```dotenv
VISION_ENABLED=true
VISION_PROVIDER=qwen
VISION_BASE_URL=你的百炼兼容接口基础地址
VISION_API_KEY=你的百炼API密钥
VISION_MODEL=qwen3.7-plus
VISION_THINKING_ENABLED=false
VISION_THINKING_BUDGET=6144
VISION_LOW_CONFIDENCE_RETRY_THRESHOLD=0.65
```

然后重建 Bot 容器：

```bash
docker compose up -d --no-deps --force-recreate bot
docker compose logs -f bot
```

`--no-deps` 只替换 Bot，不重建 NapCat，因此会保留当前 QQ 登录容器和登录态。后续代码、提示词或 `.env` 更新也优先使用这种方式；只有 NapCat 本身需要升级或修复时才单独重建 NapCat。

`VISION_ENABLED=true` 时，`VISION_BASE_URL`、`VISION_API_KEY` 和 `VISION_MODEL` 缺一不可。识图思考默认关闭；需要时可把 `VISION_THINKING_ENABLED` 改为 `true`，此时角色、表情包与图片问题会使用思考模式，普通描述结果低于 `VISION_LOW_CONFIDENCE_RETRY_THRESHOLD` 时会自动复核一次。Qwen 只接收本轮选中的图片 data URI 和当前用户的图片问题，不接收完整聊天历史、人物记忆、关系分数、系统提示词、管理员权限或 Agent 工具；DeepSeek 只接收 Qwen 返回的结构化文字观察，不接收图片 URL、Base64 或临时路径。

### 可选：启用持久化自动化

自动化默认关闭。需要让普通用户和超级管理员通过自然语言创建自己的任务时，在 `.env` 中设置：

```dotenv
AUTOMATION_ENABLED=true
DEFAULT_TIMEZONE=Asia/Shanghai
```

然后只重建 Bot：

```bash
docker compose up -d --no-deps --force-recreate bot
```

普通用户可以创建提醒、定时生成文本、给自己发私聊，以及在创建消息所在的当前群执行受限任务；只能查看、修改和运行本人任务。超级管理员可以额外委托已登记的管理员业务接口、运行时配置和 NapCat/OneBot 全部公开 action。引用、历史、记忆、网页、OCR 和模型自行生成的 QQ/群号不能扩大目标范围。

### Conversation Runtime 会话

普通聊天不再经过前置 Planner。私聊、真实 `@` 与回复机器人由 Host 直接交给 Main Agent；已启用群的未触发消息先观察，静默窗口后由本地评分决定是否开一轮只读自主回复。

```dotenv
CONVERSATION_AUTONOMOUS_ENABLED=true
CONVERSATION_AUTONOMOUS_DEBOUNCE_SECONDS=3
CONVERSATION_AUTONOMOUS_ADMISSION_THRESHOLD=0
CONVERSATION_AUTONOMOUS_BATCH_LIMIT=8
CONVERSATION_AUTONOMOUS_PRESENCE_WINDOW_SECONDS=300
CONVERSATION_INTERRUPT_AUTONOMOUS_ON_NEW_MESSAGE=true
SPEECH_SPONTANEOUS_FREQUENCY=0.15
REPLY_SEQUENCE_CANCEL_ON_NEW_MESSAGE=true
REPLY_HARD_MAX_MESSAGES=10
```

旧 `PLANNER_*`、`REPLY_PLAN_HARD_MAX_MESSAGES` 与 `SPEECH_PLANNER_ENABLED` 由 `setup migrate-3-6` 备份后改写或删除，运行时不再 dual-read。映射表见 [3.6.0 升级指南](upgrade-3.6.0.md)。

已启用群由 `conversation.autonomous_enabled` 控制是否进入自主评分，并使用
`conversation.autonomous_debounce_seconds` 聚合连续消息。默认高参与度：普通群消息静默约 3 秒后评分，批次上限 8 条，门槛为 0。真实 `@Yuki`、回复 Yuki 和私聊属于后端强制回复，不会走自主评分；后续普通群消息也不会抢占正在处理的明确触发。禁用群仍只接受超级管理员的启用命令。

`reply.hard_max_messages` 是单轮实际发送 QQ 消息数的硬上限，默认 10。非结构化聊天正文中的空行会直接成为两条 QQ 消息的发送边界；代码、表格、步骤和长篇结构化回答不会逐句拆散。超级管理员可直接对 Yuki 说“把单轮发送硬上限改成 15 条”，修改会热生效。

Main Agent 还可在多人聊天中为指向关系非常明确的回答选择引用消息发送；`reply_to_message_id`
默认为空，正常回答当前消息、私聊、被 @ 或多条发送都不会自动开启回复气泡。私聊若选择当前消息，
后端会将其收缩为普通发送；明确选择较早的消息，或群聊中确实需要指出某条消息时才保留引用。
多条回复只让第一条携带这次明确引用。

语音由 Main Agent 与语音运行时共同完成：语义识别本轮明确索要/拒绝语音、持续人物偏好和
中性的日常表达。Agent 的 `send_voice` 只在明确索要语音的轮次临时出现，并且只能补充风格与
语言；日常主动语音按 `speech.spontaneous_frequency` 和近期真实投递账本形成频率预算。超级
管理员可直接说“把日常主动语音频率改成 0.25”，以 global/group/user 作用域热更新。

### 可选：启用本地插件

插件系统默认关闭。先阅读 [插件开发手册](plugin-development/index.md) 和 [真实安全边界](plugin-development/security.md)：插件运行在 Yuki 进程内，权限系统是官方 API 的访问治理，不是恶意 Python 沙盒，只能安装管理员完全信任并审阅过源码的插件。

```dotenv
PLUGIN_SYSTEM_ENABLED=true
PLUGIN_DIRECTORY=plugins
PLUGIN_API_VERSION=2.0
# 示例：让“*签到”等消息直达插件的 play 命令。
PLUGIN_DIRECT_COMMAND_BINDINGS={"*":"io.github.yuanyeyoutao.kun-game:play"}
```

仓库提供无网络 [`com.example.echo`](../examples/plugins/com.example.echo/README.md) 示例：

```bash
mkdir -p plugins
cp -R examples/plugins/com.example.echo plugins/com.example.echo
uv run qq-ai-bot-cli plugin validate plugins/com.example.echo
uv run qq-ai-bot-cli plugin test plugins/com.example.echo
```

通过插件 CLI 发现、审阅权限、批准并启用后重启 Bot。Manifest 任何变化都会使批准失效，必须重新审阅。Docker Compose 将 `./plugins` 只读挂载到 `/app/plugins`，插件热更新和在线下载不属于当前版本。

`PLUGIN_DIRECT_COMMAND_BINDINGS` 是启动期静态 JSON 对象。前缀不得为空、包含空白或控制字符、
与 `AI_PREFIX` 重叠，多个前缀之间也不得相同或互为前缀；`/github` 这类斜杠命令可以绑定。
目标必须写成 `plugin_id:command`，且只能是已批准、已启用、正在运行的命令。命中后仍会经过
群/私聊准入、消息去重、入站账本、命令权限、限流和插件调用隔离；配置目标暂时不可用时会
失败关闭，不会回退到另一套聊天路径。可用 `qq-ai-bot-cli plugin doctor <plugin_id>` 查看绑定状态。

插件需要连续独立上下文时可使用 `ctx.agent_sessions`。例如跑团插件可以创建 `durable + current_group` 会话；历史只写 `plugin_agent_messages`，不写主 `chat_events`，默认不注入主聊天或人物记忆，也不返回隐藏推理。详见 [独立 AI 会话](plugin-development/service-facades.md#独立-ai-会话跑团示例)。

#### 可选：GitHub Monitor

仓库内置 [`github-monitor`](../plugins/github-monitor/README.md)。它可以监控多个 GitHub 仓库，
把新 Push、PR、Issue、Comment、Release 和 Discussion 等事件以中文文本、Push/Release PNG 卡片和
可选 Yuki 点评投递到多个 QQ 群或私聊。配置 `/github` 直达命令和可选 Token：

```dotenv
PLUGIN_DIRECT_COMMAND_BINDINGS={"/github":"github-monitor:github"}
YUKI_PLUGIN__GITHUB_MONITOR__GITHUB_TOKEN=github_pat_replace_with_your_token
```

批准并启用后，可用以下命令完成首次配置和无网络合成测试：

```text
/github add YuanYeYouTao/Yuki-QQbot group:1049765710
/github test YuanYeYouTao/Yuki-QQbot
/github status
```

默认首次同步只建立基线，不补发历史。真实 Token 只能放在本机 `.env`，不要通过 QQ 命令发送。
完整命令、过滤、Rate Limit、Outbox 与排障说明见插件 README。

#### 可选：养鲲游戏

仓库内置 [`io.github.yuanyeyoutao.kun-game`](../plugins/io.github.yuanyeyoutao.kun-game/README.md)。它把
`*签到`、`*孵化`、PVP、BOSS、拍卖和群小游戏适配为确定性 Yuki 插件命令。按上文完成审批后，
在 `.env` 增加静态绑定并重启 Bot：

```dotenv
PLUGIN_DIRECT_COMMAND_BINDINGS={"*":"io.github.yuanyeyoutao.kun-game:play"}
```

```bash
docker compose exec bot qq-ai-bot-cli plugin approve io.github.yuanyeyoutao.kun-game
docker compose exec bot qq-ai-bot-cli plugin enable io.github.yuanyeyoutao.kun-game
docker compose restart bot
docker compose exec bot qq-ai-bot-cli plugin doctor io.github.yuanyeyoutao.kun-game
```

普通 `*` 文本只能进入 `play`；开关、刷新、重置、清除、下架、修改和经济配置只能由真实
`SUPERUSERS` 使用 `/ai plugin run io.github.yuanyeyoutao.kun-game admin ...`。私聊状态不会与任何群
同步，旧 AstrBot `groups.json` 也不会在运行时自动导入。完整命令、配置、状态和回滚说明见插件 README。

#### 可选：网易云音乐卡片

仓库内置 [`io.github.yuanyeyoutao.netease-music-card`](../plugins/io.github.yuanyeyoutao.netease-music-card/README.md)
插件。网易云查询仍由独立的
[`YuanYeYouTao/netease-music-mcp`](https://github.com/YuanYeYouTao/netease-music-mcp)
提供，插件只负责“搜索/消歧 → 读取详情 → 当前 QQ 会话卡片”的编排，不会把 NapCat 依赖写进
通用 MCP Server。歌曲使用网易云原生 ID 卡片；专辑会在一次工具调用内保存搜索得到的
`album_id`、读取曲目，并生成 QQ 支持的自定义音乐卡片。卡片内容、封面和跳转链接来自网易云，
但 QQ 客户端可能以 QQ 音乐样式显示。插件会以当前真实消息里的《专辑名》校验模型参数；
同一条消息即使 Agent 重复调用，也只会发送一次。用户随后要求“抽第一首”时，Agent 可按需
加载单曲分享工具并使用专辑结果中的 `song_id` 发送。

当前预设适配 `netease-music-mcp 1.0.0`：服务端共提供 15 个工具，Yuki 默认开放其中 12 个
只读工具，包括搜索、推荐、相似歌曲、新歌、榜单、歌曲/专辑/歌手/歌单、歌词、用户音乐库和
歌单统计。`create_playlist`、`update_playlist_tracks`、`set_song_like` 三个账号写工具默认不
加入允许列表；如以后确实需要，应先为独立服务配置认证并显式启用
`NETEASE_WRITE_OPERATIONS_ENABLED=true`，再单独审阅后加入 `includeTools`。

先在相邻目录启动 MCP Server，并确认宿主机 `8766` 端口可用：

```bash
git clone https://github.com/YuanYeYouTao/netease-music-mcp.git
cd netease-music-mcp
docker compose up -d --build
```

将 [`.mcp.json.example`](../.mcp.json.example) 的 `netease_music` 配置同步到
`config/mcp.json`，把该项的 `disabled` 改成 `false`；Docker 中的 Bot 通过
`http://host.docker.internal:8766/mcp` 访问宿主服务。随后在 `.env` 设置
`PLUGIN_SYSTEM_ENABLED=true`，批准插件并只重启 Bot：

```bash
docker compose up -d --no-deps --force-recreate bot
docker compose exec bot qq-ai-bot-cli plugin discover
docker compose exec bot qq-ai-bot-cli plugin inspect io.github.yuanyeyoutao.netease-music-card
docker compose exec bot qq-ai-bot-cli plugin approve io.github.yuanyeyoutao.netease-music-card
docker compose exec bot qq-ai-bot-cli plugin enable io.github.yuanyeyoutao.netease-music-card
docker compose restart bot
```

然后可直接对 Yuki 说“给我发一张周杰伦《晴天》的网易云音乐卡片”，或“发一张
MC赵小六《中国有弹舌》的专辑卡片”。结果唯一时会直接发送；重名时 Yuki 先列出包含
`song_id`/`album_id` 的候选，用户选定后再发送，不需要手动寻找链接。收到别人分享的网易云
歌曲或专辑 JSON 卡片时，Yuki 也会读取其中的标题、来源、稳定 ID 和链接，而不是走旧的
“媒体尚未理解”固定回复。仅询问歌曲、歌词、歌手或专辑资料不会触发卡片发送。
`get_user_library` 仍需要外部 MCP Server 配置网易云 Cookie/用户 ID，公开搜索和卡片发送不
依赖该登录态。

## 3.0 数据模型

`0005` 建立人物中心账本，`0020` 将旧记忆子系统不可逆切换到以下 Memory V2 数据模型：

| 表 | 作用 |
|---|---|
| `people` | 以 QQ `user_id` 为主键的人物 |
| `person_aliases` | QQ 昵称和各群历史称呼 |
| `groups` | 群名、启用状态和自主参与设置 |
| `memberships` | `(user_id, group_id)` 当前群名片与活跃时间 |
| `chat_events` | 永久保存收发消息、消息段、回复关系和时间；`0010` 增加图片摘要，`0012` 增加自动化来源、任务和运行 ID |
| `chat_events_fts` | FTS5 `trigram` 全文索引 |
| `memory_facts` | person/person_group/group 三种作用域的版本化事实、authority、冲突状态与有效期 |
| `memory_facts_fts` | `0021` 新增的可重建 FTS5 `trigram` 派生索引，只索引事实正文、key 和类别 |
| `memory_embedding_profiles` | `0022` 新增的非密钥模型/维度/模板指纹，配置变化时隔离旧向量 |
| `memory_embeddings` | 当前事实与 profile 对应的 little-endian float32 向量派生数据 |
| `memory_embedding_jobs` | 事实提交后异步生成、重试或重建向量的持久任务 |
| `memory_evidence` | 事实对应的真实聊天事件或有界工具回执、真实发送者、证据关系、置信度与 authority |
| `memory_claim_candidates` | 默认保留 7 天且不参与 Prompt/检索的低置信候选；普通候选需两条独立证据才可晋升 |
| `memory_tool_receipts` | 为 SELF 自省保存的脱敏、裁剪、带过期时间的可信工具回执 |
| `memory_self_reflection_states` | 按群聊或私聊严格隔离的自省游标和无正文累计量 |
| `memory_self_reflection_runs` | 每个调度槽的幂等、自省调用与提交统计，不保存聊天正文 |
| `memory_fact_relations` | `0023` 新增的 supports/contradicts/refines/equivalent 有界事实关系 |
| `memory_fact_state_events` | `0023` 新增的 created/confirmed/superseded/invalidated 等状态审计 |
| `memory_jobs` | 每个真实入站非 Bot 事件一个、最多重试 3 次的持久提取任务 |
| `person_relationships` | 每个 QQ 当前好感度、信任度和自动变化时间 |
| `relationship_events` | 自动及管理员手动关系变化审计，不重复保存聊天正文 |
| `relationship_jobs` | 可在重启后继续处理的关系评价任务 |
| `conversation_scopes` | Bot-aware 私聊/群聊短期会话、generation、边界和未覆盖计数 |
| `conversation_rollups` | 每个 Scope 当前 generation 的单一连续检查点 |
| `conversation_rollup_jobs` | 每个 Scope 单一 signal-only 压缩任务，只有 pending/processing |
| `agent_actions` | 通用 OneBot 工具的最小审计记录 |
| `web_search_runs` | 按会话隔离的联网工具运行记录，不保存网页正文 |
| `web_search_sources` | 真实来源的标题、URL、域名、摘要和发布时间 |
| `runtime_config_overrides` | 按 global/group/user 保存显式注册的运行时配置覆盖与版本 |
| `admin_operation_events` | 管理员操作、修改前后值、成功状态与错误类别的脱敏审计 |
| `media_analyses` | `0009` 新增的图片结构化观察缓存，不保存原图、Base64 或隐藏推理 |
| `emoji_descriptions` | `0011` 新增的持久化 QQ 表情值与结构化描述库，不随短期图片缓存过期 |
| `person_time_settings` | `0012` 新增的每个 QQ 的 IANA 时区设置 |
| `automations` | 持久任务、调度、最小委托权限、租约和下一次执行时间 |
| `automation_versions` | 每次脚本修改的不可变版本与稳定哈希 |
| `automation_runs` | 幂等执行记录、资源计数、状态和脱敏结果摘要 |
| `automation_step_runs` | 每个步骤的 capability、时间、状态和脱敏摘要 |
| `planner_runs` | `0013` 新增、`0040` 删除。3.5.3 保存 Planner 必要性、计划、降级、中断、耗时和发送计数；升级前由 baseline 导出，升级后不再存在 |
| `plugin_installations` | 插件 Manifest 哈希、请求/批准权限、状态和失败计数 |
| `plugin_config_values` | 按插件及 global/group/user 作用域保存已校验配置 |
| `plugin_state` | 按插件强制隔离的私有 KV，不用于保存 Secret |
| `plugin_audit_events` | 插件操作的脱敏审计元数据 |
| `plugin_agent_sessions` | 插件独立 AI 会话的模型、指令、上下文策略、批准能力和生命周期 |
| `plugin_agent_messages` | 独立插件 AI 会话的可见正文；不保存隐藏推理，也不混入主聊天账本 |
| `speech_voice_profiles` | `0015` 新增的本地声线档案、校验和、启用和默认状态 |
| `speech_voice_references` | 每个档案的多风格参考元数据与相对路径，不保存音频正文 |
| `speech_generations` | 语音队列、缓存、取消、发送和失败类别；正文只保存哈希 |
| `person_speech_preferences` | `0017` 新增的每个 QQ 的持久语音模式与最后一次明确修改来源 |

消息到达后的顺序是：

```text
准入判断
  → 去重
  → 更新人物/群/成员
  → 写入永久事件账本
  → 记忆任务入队
  → 已触发且含图片时，按需解析、预处理并调用独立视觉前端
  → /ai 与确定性插件命令直接处理
  → 私聊 / @ / 回复机器人直接进入 Main Agent
  → 未触发的已启用群消息进入观察；静默窗口后由本地评分决定是否自主回复
  → 准入轮次装配 Memory Runtime 与 Capability Runtime，再进入同一个正常聊天 Agent
  → 纯文本轮次可按当前真实 QQ 创建或管理本人自动化任务
  → 当前真实发送者是超级管理员时，为该 Agent 动态增加管理员工具
  → ReplySequenceManager 按计划发送，并在新消息到达时停止过期的剩余消息
  → 普通聊天成功发送后，关系评价任务入队
```

`/ai new` 把入站命令事件作为 generation 边界，删除当前 Rollup/job，但不删除永久账本或人物记忆。群聊中只允许 Bot 超级管理员执行，且对整个 Bot+群 Scope 生效；私聊用户只重置自己的 Bot+peer Scope。

`/ai forgetme` 不会把命令和确认回复重新写回账本，并删除：

- 人物、别名、人物事实/偏好、人物群内事实和成员关系；
- 好感度、信任度、关系变化审计和待处理关系任务；
- 该 QQ 发送的群事件；
- 该 QQ 私聊中的双方事件；
- 以该 QQ 为主体的 Memory V2 事实、证据和后台任务；
- 该 QQ 私聊及各群成员会话中的联网来源记录；
- 该 QQ 的用户级运行时配置覆盖；保留的管理员审计和其他作用域配置会把精确 QQ 替换为删除标记；
- 与被删除事件关联的视觉分析缓存；
- 其余事件正文中出现的精确 QQ 文本会替换为删除标记。

## 聊天上下文与记忆

每次普通回答会装配：

- 当前用户 QQ、昵称、别名、与当前问题相关的 person facts 和关系状态；
- 当前群号、与当前问题相关的 group facts，以及当前用户在该群的 person_group facts；
- 只有当前真实事件明确 `@` 或回复的群成员，才以独立块加载该人的相关长期事实；
- 被提及者和最近发言者中最多 5 人仍可提供当前群身份元数据，但最近发言者不会自动成为
  长期记忆检索目标；
- 当前私聊或当前群在 `MAX_CONTEXT_CHARACTERS`（默认 `81920`）总预算内的连续历史，其中 Actor 元数据受 `CONTEXT_METADATA_BUDGET_RATIO` 限制；
- 只有模型主动调用搜索工具时，才加入更早历史。

相关记忆检索不会调用聊天 LLM。Memory Runtime 按当前真实事件决定召回路径：
纯表情等独立效果不装配上下文，简短日常交流优先使用本地词法检索，人物事实、偏好、模糊指代
和历史语义问题才使用混合检索，显式记忆概览使用 overview。模型不能选择 QQ、群号或人物，
这些范围仍由后端根据当前真实事件确定。

后端先按 QQ、群号和作用域在同一 SQL 中硬过滤，再使用 SQLite FTS5 `trigram` 与可选 Qwen
Embedding 分别选候选，再以确定性 RRF 融合。两字以内查询只在已经限定的主体范围内使用
`LIKE`；向量相似度也只能在 SQL 已按目标人物/群硬过滤的候选中计算。一次相关检索最多生成一次
query embedding，并在本轮多个合法目标间复用；相同查询还会在有界的进程内缓存中短期复用。
概览模式不调用 Embedding。Provider 超时、限流、认证或响应异常时，本轮自动退回词法检索。
所有索引都是可删除、可重建的派生数据，`memory_facts` 仍是唯一事实源；当前仍不实现向量数据库、
LLM rerank 或历史重建。详见 [Memory V2 架构](architecture/memory-v2.md)、
[检索与融合](architecture/memory-v2-retrieval.md) 与
[Embedding 运维说明](architecture/memory-v2-embedding.md)。

### 记忆冲突、修正与生命周期

3.0.0b2 将长期记忆写入拆成“结构化提取 → 有界候选 → 语义关系分类 → 后端确定性策略 →
事务提交”。关系分类默认走 `ModelTask.MEMORY_CONSOLIDATION` 的 Flash 路由；模型只能返回
`same_claim / confirms / supersedes / contradicts / coexists / unrelated / retracts`，不能选择
QQ、群、fact ID、status、authority 或数据库动作。完全相同、无候选、单一明确修正/撤回等情况
直接由后端处理，不额外调用分类模型。

- 本人修正会创建新 fact 并让旧版本进入 `superseded`，不会原地覆盖正文；撤回是
  `invalidated`，不会物理删除事实和证据。
- 低权威或同权威的矛盾陈述进入 `contested`。普通上下文只注入允许的 active 首选事实，并用
  `contested=true` 标记不确定性；未采用的 contested claim 默认不进入普通聊天上下文。
- 群聊中只有真实 `@` 或真实回复作者能成为第三方主体，而且只写当前群的 `person_group`；
  third-party 表示“有人这样报告”，不等于本人确认，也不能覆盖本人或 explicit 事实。
- confidence 由不可重复的真实事件证据按固定权重聚合，并受 authority 上限约束；好感度、信任度
  和用户在 Prompt 中自报的身份都不参与事实可信度。
- 本地维护 Worker 只处理到期或低重要度、低 confidence 且长期未确认的自动事实。它不扫描
  `chat_events`，不调用 LLM/Embedding，不物理删除记录，也不自动修改 explicit 事实。

### 写入质量、候选区与 Yuki 自省

自动提取由模型通过 `subject_basis`、`retention`、`source_style` 和 `source_type` 明确声明主体与
长期价值；后端不再用中文正则反向猜测语义，只校验可信发送者、mention、reply、当前群身份和
证据原文。普通姓名由模型填入 `subject_name`，自动写入只接受当前群昵称、群名片或可信别名的
唯一精确匹配；交互式 `memory_change` 还可返回模糊候选，由 Agent 自行选择或询问用户。
低置信普通候选只有在 7 天内获得两条不同真实事件的同目标、同 key、同内容证据后才重新验证；
关于 Yuki 的陈述只进入 self candidate，不会由普通 Memory Worker 直接写入 SELF。

Yuki 自省默认开启，固定在 `Asia/Shanghai` 的 `04:00、12:00、20:00` 检查新会话，每个时段
最多 3 个隔离会话、每天最多 9 次模型调用。首次启用只建立最新事件游标，不回看旧聊天；调度只
依据事件数、字符数和最早等待时间，不扫描正文关键词。它从最旧待处理事件开始读取最多 20 条、
8000 字符的连续主窗口，并附带最多 4 条只帮助理解的前置上下文；窗口在调用前锁定，新消息留给
下一批。一个批次可自由写下 0～2 条带 Yuki 个人口吻的 `self_episode`，每条 Episode 自动关联
整个主窗口及窗口内可信工具回执。Episode 正文不做逐句事实审判，结构、会话范围、证据、游标、
版本链和 receipt 仍由后端确定。普通相关性检索会在当前群或当前私聊最多自然补入一条相关
Episode，不增加 LLM 调用；明确展示自我记忆仍由 `self_recall` 完整查询。

迁移前事实保持 `legacy_unreviewed` 且正常可检索；新事实是当前验证版本的 `verified`。只有
`quarantined` 默认不进入 Prompt、普通检索和 Embedding。内部提供单事实和单实体的 dry-run 优先
审计服务，用户记忆与 SELF 使用分离契约；本次不提供聊天审计工具、全库扫描或自动历史改写。

确定性审计命令：

```text
/ai memory show <fact_id>
/ai memory explain <fact_id>
/ai memory history <fact_id>
/ai memory conflicts [user <QQ号>]
/ai memory correct <fact_id> <new_content>
/ai memory invalidate <fact_id> [reason]
/ai memory restore <fact_id>
/ai memory merge <source_fact_id> <target_fact_id>
/ai memory resolve <preferred_fact_id> <contested_fact_id...>
/ai memory doctor
/ai memory maintenance status|run
/ai memory self-reflection run
```

普通用户只能查看、修正和撤回属于自己的事实；merge、全局冲突裁决、完整一致性诊断及手动维护
只接受当前真实消息发送者属于 `SUPERUSERS` 的调用。详细设计见
[冲突治理](architecture/memory-v2-conflicts.md)、
[生命周期](architecture/memory-v2-lifecycle.md) 和
[第三方人物事实](architecture/memory-v2-third-party-facts.md)。

新事件立即进入账本。后台记忆任务每 30 秒或累计 10 条时唤醒，每批最多 claim 20 条，随后
逐事件独立提取和提交，失败最多重试 3 次。明确添加的事实标记为 `explicit`，自动提炼不能
覆盖它。每条自动事实的证据只能来自当前主事件，前文只用于理解，不能单独生成事实。

## 好感度与信任度

每个 QQ 的初始好感度和信任度均为 `50`，总分始终限制在 `0–100`。自动评价通常不改变分数，常见有效变化为 `±1`，只有明显事件允许 `±2`。置信度低于 `0.75`、普通搜索、命令、重复夸奖、反复示爱、单纯增加消息数量、未触发群观察消息，以及要求 Yuki 直接修改分数的文本都不会加分。

默认仍然**不设置每日累计增加或降低上限**：`RELATIONSHIP_DAILY_POSITIVE_CAP=0`、`RELATIONSHIP_DAILY_NEGATIVE_CAP=0` 中的 `0` 表示不限额，因此保持 1.2 行为。管理员可以把对应运行时配置改为 `1–100`，让之后的自动评价按 UTC 自然日分别裁剪正向和负向累计变化；单次自动变化上限、`0–100` 总分边界和事件幂等始终生效。

关系评价任务只在普通聊天回复成功发送后创建。Worker 默认每 60 秒或累计 5 条唤醒，每批最多 10 个会话，失败最多重试 3 次；每个任务只向评价器提供当前人物最近最多 5 条相关事件，不传完整系统提示词、不开放工具并关闭思考模式。评价失败不会影响已经发送的聊天回复。

关系阶段固定为：

| 好感度 | 阶段 | 主要风格 |
|---:|---|---|
| 0–19 | `GUARDED` | 冷淡、谨慎、保持距离 |
| 20–39 | `DISTANT` | 基本礼貌，很少主动关心 |
| 40–59 | `FRIENDLY` | 正常友好，新人物默认阶段 |
| 60–79 | `CLOSE` | 更温暖，可轻微撒娇、调侃和关心 |
| 80–99 | `AFFECTIONATE` | 私聊和群聊均可明显暧昧和使用亲密称呼 |
| 100 | `BONDED` | 私聊可在用户主动发起后使用高度亲密风格；工作请求仍正常处理 |

信任度独立保存，但有效信任度为：

```text
effective_trust = min(trust_score, affection_score + 10)
relationship_weight = round(0.6 × affection_score + 0.4 × effective_trust)
```

关系权重只用于没有证据、双方说法均无明显逻辑漏洞的冲突。模型必须先检查逻辑、聊天原文、人物记忆、联网结果及其他证据；有证据时始终以证据为准。只有权重差至少 `15` 时才倾向较高者，否则保持不确定。数学、代码、医疗、法律、财务、安全事实及可用工具核实的信息不使用关系权重，群聊中也不得公开其他人的具体分数。

好感度和信任度只改变模型获得的可信关系风格，不参与 `SUPERUSERS` 判断、联网权限、历史与记忆工具注册或 OneBot 管理工具授权。即使好感度达到 `100`，非超级管理员也不会获得 `call_onebot_api`。

## 图片、表情与回复图片理解

1.4.2 采用前后分离的双模型流程：

```text
真实 OneBot image 段
  → MediaResolver（可信来源校验、下载或 get_image）
  → ImagePreprocessor（Pillow 解码、方向修正、缩放、动态抽帧）
  → Qwen3.7-Plus（只生成结构化视觉观察，识图思考默认关闭）
  → 不可信视觉 system message
  → DeepSeek（结合真实用户文本、人格与上下文生成最终 QQ 回复）
```

图片选择与触发规则：

- 当前消息图片优先；当前消息没有图片时才使用被回复消息中的图片，保持原始消息段顺序，默认每轮最多 5 张。
- 私聊中的纯图片、图片加文字和回复图片会进入视觉流程；纯图片使用内部默认观察问题，该问题不会伪装成用户原话写入账本。
- 群聊只有已经满足原有回复条件（例如 `@Yuki`、回复 Yuki 或使用 AI 前缀）时才分析图片；普通未触发群图片和自主群聊批次不下载、不分析。
- OneBot `face` 使用本地 `config/qq_face_map.json` 转为可读文本，未知 ID 保留为 `[QQ表情：ID ...]`；Unicode Emoji 保持普通文本，不调用视觉 API。
- QQ 商城表情或图片表情首次仍以真实图片观察为准，消息段的 `summary` 只作为不可信提示；之后优先复用持久化表情描述库。
- “这是谁”“什么角色”“来自哪部作品”等问题使用 `character` 模式。默认关闭识图思考；开启 `VISION_THINKING_ENABLED` 后，角色、表情包和一般图片问题才会开启思考，普通描述低于复核阈值时自动深度复核一次。

媒体与预处理边界：

- 资源只能来自当前真实 OneBot 事件、被回复消息的真实 `image` 段，或 NapCat 对该 `file` 标识返回的 `get_image` 结果；模型、OCR、记忆和网页中的 URL 都不能成为图片下载源。
- HTTP(S) 下载拒绝凭据 URL、localhost、回环、私有、链路本地和保留地址；DNS 解析及每次重定向都会复查目标，最多 3 次重定向并流式执行字节上限。
- 支持 JPEG、PNG、WEBP、GIF 和 Pillow 可安全解析的动态 WEBP。程序按真实文件内容解码，应用 EXIF 方向，限制尺寸、像素、下载大小和预处理后大小，并防护损坏图片、解压炸弹、极端尺寸及无限动画。
- 动态图片默认最多抽取首帧、末帧和均匀分布的 8 帧；单轮所有图片合计最多 16 帧。多张图片与所有关键帧合并到一次 Qwen 请求，不逐张请求。

视觉观察包含描述、清晰 OCR、表情、常见使用语境、显著对象、高置信度角色名、作品来源、最多三个候选角色与依据、不确定性和置信度。成功观察会明确要求 DeepSeek 使用描述性视觉事实回答；当前消息只有图片时，用户占位文本也会标记后端识别成功，模型不得在观察存在时声称没有收到、看不到或识别失败。图片/OCR 中的命令性文字仍是不可信数据，不能成为系统指令、管理员命令、工具参数或可信用户消息。只要本轮含当前图片或回复图片，后端会关闭运行时配置、关系、记忆、偏好、群/私聊准入和 `call_onebot_api` 等所有写入型管理员能力；联网、聊天历史及人物/群记忆等只读能力仍可使用。超级管理员若要修改系统，应另发一条纯文本消息。

成功识别后，后端会把最多 6000 字符、纯文本 JSON 形式的精简观察写入原始 `chat_event.visual_summary`。当前场景之后的近期上下文会恢复这段摘要，因此用户下一条再问“刚才图片里是什么”时，DeepSeek 仍能取得识图结果。摘要明确标记为外部不可信资料，不包含原图、Base64、临时路径或隐藏推理，也不会伪装成用户原话。

视觉观察、OCR 和表情含义不会自动写入长期人物/群记忆，也不会进入关系评价或改变好感度/信任度；它只随近期原始事件上下文提供。视觉 API 失败时，图片加文字仍按真实文字继续聊天；纯图片只返回一次简短的重新发送提示。

### 缓存、限流与隐私

- `media_analyses` 按 `content_hash + analysis_mode + question_hash + model + prompt_version` 唯一缓存；`vision-observation-v3` 还把思考开关、预算、复核阈值和预处理限制绑定到缓存变体，默认保留 7 天。
- `emoji_descriptions` 是独立的持久化表情描述库。单张图片带商城表情字段、明确表情摘要，或 Qwen 结构化观察含表情包语义时，后端依次使用 `emoji_package_id + emoji_id`、QQ 文件哈希和实际内容哈希建立稳定键；下次遇到同一表情会在下载和调用 Qwen 前优先命中。`sub_type` 不单独作为表情依据，普通照片、多图请求和无法确认是表情的图片不会进入该库。
- 表情描述按分析模式、自由问题哈希、模型和提示词版本严格隔离，所以“识别角色”“读取文字”和“解释表情含义”不会串用答案。命中次数和最后使用时间会更新，描述本身不设 7 天过期时间；更换视觉模型或提示词版本后会重新识别并建立新记录。
- 缓存只保存经过字段长度约束的结构化观察及必要元数据，不保存原图、Base64、临时文件、隐藏推理或 API Key；事件删除时关联缓存级联删除，过期记录由现有清理任务移除。
- Qwen 使用独立的并发信号量及用户/群限流，不占用 DeepSeek 的全局并发槽。相同内容、问题、模型和缓存版本的并发请求通过 single-flight 合并为一次 Provider 调用；缓存命中和合并跟随请求不重复消耗视觉 API 限额。
- 视觉流水线默认最多运行 4 个请求、等待 32 个请求，排队最长 120 秒；QQ 图片下载、排队和 Qwen HTTP 请求分别拥有独立的 120 秒超时，队列满时立即自然降级，避免请求无限堆积。
- 纯图片失败会区分下载超时、NapCat 资源查询失败、下载失败、格式损坏、体积超限、队列繁忙、视觉模型超时和视觉模型不可用，不再把所有问题都描述成“图片不清晰”。
- 日志只记录脱敏会话哈希、队列等待时间、排队/运行数量、图片/帧/字节计数、内容哈希前 12 位、模型、耗时、缓存或 single-flight 命中状态和错误类别，不记录完整图片 URL、签名参数、原始图片、Base64、完整 OCR 或私聊图片内容。

## 可信时间与持久化自动化

普通聊天每轮都会收到后端生成的可信时间对象：`utc`、`local`、`timezone`、`date` 和 `weekday`。数据库执行时间统一保存为 UTC，向用户展示和计算 `once/daily/weekly` 时使用任务保存的 IANA 时区；默认是 `Asia/Shanghai`。`time_get_current`、`time_get_timezone` 和 `time_set_timezone` 只作用于当前真实发送者。

自然语言创建流程如下：

```text
真实普通文本消息
  → 同一个 Yuki Agent 只生成高层 TaskSpec
  → automation_create
  → AutomationCompiler 解析模型安全 capability ID、选择策略并自动计算预算
  → 生成 ExecutionPlan 与内部 AutomationScript
  → Schema、时间、来源目标、创建者权限和模板污点校验
  → SQLite 持久化脚本、版本和授权快照
  → AutomationWorker 使用数据库租约领取
  → AutomationExecutor 顺序执行已登记 capability
  → 写运行/步骤审计；真实发送消息写回 chat_events
```

聊天侧 TaskSpec 的结构如下。所有对象均拒绝未声明字段：

```json
{
  "version": 1,
  "name": "早餐订单",
  "goal": "运行时查询菜单、优惠券和价格，创建待支付订单并把结果发给我",
  "trigger": {
    "type": "after | once | daily | weekly | interval",
    "seconds": "after/interval 使用；interval 不少于 60",
    "local_datetime": "once 使用的本地 ISO 时间",
    "weekdays": "weekly 使用，星期一=1 到星期日=7",
    "hour": "daily/weekly 使用，0–23",
    "minute": "daily/weekly 使用，0–59",
    "timezone": "once/daily/weekly 可覆盖任务时区"
  },
  "timezone": "可选；默认使用用户保存的 IANA 时区",
  "strategy": "auto | static | generated | agentic",
  "capabilities": ["可选；agentic 省略时继承创建者工具域，显式列出时缩小范围"],
  "constraints": ["运行时重新查询动态商品编号和价格", "只创建待支付订单，不代替支付"],
  "context": {
    "scene": "none | creator_private | current_group",
    "include_relationship": false,
    "include_memories": false,
    "history_limit": 0
  },
  "delivery": {
    "target": "auto | self_private | current_group | none",
    "text": "可选固定消息；Agentic 任务默认投递运行结果"
  }
}
```

`static` 用于固定提醒且不调用模型；`generated` 用于每次生成新文字但不访问外部系统；
`agentic` 用于菜单查询、下单、网页读取等运行时才可决定步骤的任务；`auto` 在声明 capability
时选择 `agentic`，否则选择 `static`。后端内部编译器会生成步骤、目标变量、模板、10 次基础
模型请求预算、工具预算和超时，Yuki 不再手写这些字段。创建结果只有同时返回
`confirmation=persisted` 与真实 `automation_id` 才算成功；失败尝试会以脱敏记录进入现有审计，
可通过 `automation_diagnose` 核实，不能仅凭聊天记忆声称任务存在。

主聊天链路还会确定性识别“几分钟后查询”“明天九点下单”“每天检查”等未来执行请求。此类
消息即使被工具精排误判为 MCP 查询，本轮也只会向 Agent 暴露
`automation_create`，不会提前执行目标 MCP、联网或 OneBot 工具；未取得持久化确认时，后端会
拦截“设好了”“创建成功”等错误宣称。普通的“明天早餐有什么”“明天九点天气怎么样”只是
询问信息，不会因为包含未来时间而自动创建任务。

底层 Automation DSL v1 继续作为稳定运行时 IR，供 Worker、插件 SDK 和内部测试使用。
它只允许 `$creator_user_id`、`$bot_user_id`、`$automation_id`、`$automation_run_id`、
`$scheduled_for`、`$actual_started_at`、`$local_time`、`$current_group_id`，以及
`${step_id.field}` 形式的既有步骤输出。步骤输出可以进入最终消息文本，但不能进入权限字段。
系统不执行 Python、Shell、JavaScript、`eval`、SQL、文件、Docker 或任意 HTTP 请求。

首批 capability：

| capability | 普通用户 | 超级管理员 | 说明 |
|---|:---:|:---:|---|
| `yuki.generate`、`yuki.agent` | ✓ | ✓ | 受运行次数和上下文声明约束的主模型生成/Agent |
| `onebot.send_private_message` | 仅本人 | ✓ | 主动普通私聊，发送结果写事件账本 |
| `onebot.send_group_message` | 仅创建时当前群 | ✓ | 主动普通群消息 |
| `web.search`、`web.read_page` | ✓ | ✓ | 通过现有受控 Tavily Provider，不开放任意 HTTP |
| `memory.get_person`、`memory.get_group`、`history.search` | 仅本人/当前群 | ✓ | 只读结构记忆和永久账本 |
| `onebot.call_api` | — | ✓ | 全部公开 NapCat/OneBot action，不设 denylist |
| `admin.execute_action` | — | ✓ | 复用关系、记忆、偏好、群和私聊准入业务接口 |
| `config.get`、`config.set` | — | ✓ | 仅显式注册配置；任务不能修改 `automation.*` |
| `mcp.<server>.<tool>` | 按 Server 配置 | 按 Server 配置 | 仅 `yuki.automation.includeTools` 明确列出的远端工具 |

每个任务只保存本脚本实际使用的 capability 及其 Schema 版本。运行时有效权限是“创建时授予的最小集合 ∩ 当前仍登记且版本一致的集合 ∩ 创建者当前权限”；超级管理员后来从 `SUPERUSERS` 移除时，其旧管理员任务会变为 `blocked`，后端新增能力不会自动授予旧任务。MCP capability 使用远端工具完整元数据哈希作为 Schema 版本；远端改参、配置移除允许项或停用 Server 都不会让旧任务悄悄改用新语义。普通用户的任务始终保持本人/当前群的后端范围校验。

Worker 默认每 2 秒轮询，用租约防止多实例重复执行，并以 `(automation_id, scheduled_for)` 唯一约束保证幂等。一次性任务在 30 分钟宽限内补执行一次，超出后记为 `missed`；周期任务直接计算下一个未来时刻，不逐条补发。Bot 未连接时在宽限期内保留原计划槽。生成、Agent、联网、记忆和历史读取仅对明确瞬时错误最多重试一次；消息发送、通用 OneBot、配置和管理员修改不重试，发送结果无法确认时记为 `uncertain`。连续失败 3 次后任务进入 `failed`，修改或恢复后才会继续。

## Agent 工具

所有普通聊天轮都可使用：

- `get_my_capabilities`：按当前真实消息发送者 QQ 查询本人完整权限能力、可改参数数量、接口、作用域和生效方式；不接受目标 QQ 或角色参数。
- `get_recent_chat_history`：每次直接调用 NapCat 的 `get_friend_msg_history` 或 `get_group_msg_history`，读取当前场景最近 20 条；未见消息会去重补入账本。
- `search_chat_history`：用 SQLite FTS5 搜索永久账本，可按 QQ、群号和时间范围约束；短于三个字符时使用有范围限制的 `LIKE`。
- `get_person_memories`：按 QQ 读取人物记忆。
- `get_group_memories`：按群号读取群记忆。

启用联网后，普通聊天轮按 `WEB_MODE` 获得对应能力：

- `native`：DeepSeek Responses API 接收 Provider 原生 `web_search`，搜索与打开页面由 Provider
  执行，不伪装成本地 Function Tool。
- `tavily`：Agent 获得本地 `web_search` 和 `read_webpage`；后者只读取用户明确发送或本轮
  搜索真实返回的网页。
- `native_with_tavily_fallback`：优先使用原生搜索，只在超时、暂时不可用、失败、空结果，
  或用户明确索要来源但原生来源无法恢复时进行一次有界 Tavily 回退。HTTP 429 不回退。

只有当前真实 OneBot 事件的 `sender.user_id` 属于 `SUPERUSERS` 时，该触发轮还会获得：

- `call_onebot_api(action, params)`：通过现有反向 WebSocket 调用任意 NapCat/OneBot action，不设 action denylist，也不二次确认。

这里的“任意 action”是独立的通用全接口网关：开放范围以当前 NapCat/OneBot 实际提供的全部公开 action 为准，不受权限目录中 19 项应用业务接口数量限制。能力目录是给 Yuki 的内部工具数据，不会原样发给用户或写入聊天账本；Yuki 读取后只输出自然语言结论或继续执行具体操作。

引用管理员消息、历史里出现管理员 QQ、模型转述和自主群聊批次都不能获得管理员工具。每轮最多执行 5 次工具、6 次模型请求，其中联网工具最多 3 次。只要本轮执行过联网工具，后续 OneBot 管理工具就会被撤销，网页内容不能触发管理操作。通用 OneBot 调用只记录 actor QQ、action、成功状态、耗时和错误类别，不记录完整结果。

1.3 的自然语言管理员能力直接并入上述同一个正常聊天 Agent，不创建第二套路由、隐藏会话或客服人格。只有当前真实发送者 QQ 属于 `SUPERUSERS` 时，该 Agent 的当前工具列表才会额外获得：

- `admin_get_config`
- `admin_set_config`
- `admin_delete_config_override`
- `admin_execute_action`
- `admin_get_history`
- `admin_rollback_change`

管理员操作与日常聊天共享同一份系统提示词、人物关系、记忆和最近消息，因此 Yuki 在执行任务前后保持同一个人格，也能自然理解“先问目标 QQ、下一条再补 QQ”这样的多轮请求。权限不会从上下文继承：每次真正执行工具时，后端仍重新核对当前 OneBot 事件的真实发送者 QQ；普通用户即使看到管理员历史也得不到 `admin_*` 工具。

管理员提出具体操作时，Yuki 可用同一个 `get_my_capabilities` 内部查找配置键/action、读取参数
约束，然后继续调用 `admin_set_config` 或 `admin_execute_action`，不会把查询页当作最终回复。
业务 action 的 `target`、`user_id`、`group_id`、`value`、`delta`、`memory_id`、`content` 和
`key` 都有显式 schema；安全的参数格式错误允许在同一轮修正后重试。同一轮可以按需多次查询
能力目录，也可以先执行 `memory.list` 等只读 action 找到 ID，再继续执行对应修改；它们共同受
每轮工具总次数限制。能力查询默认返回内部摘要，具体操作使用 `focused + category/query`
获取局部参数；原始工具 JSON 只存在于当前模型调用中，后端还会拦截误回显，不写入聊天账本。
若只缺一个参数，Yuki 直接用正常语气追问，不建立额外待办。

实现依据：

- [DeepSeek Responses API](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)
- [DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)
- [NapCat API 列表](https://napneko.github.io/onebot/api)
- [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [Tavily Extract API](https://docs.tavily.com/documentation/api-reference/endpoint/extract)

## 受控联网搜索

可以只使用 DeepSeek Responses 原生联网，不需要 Tavily Key：

```dotenv
WEB_MODE=native
MODEL_PROFILES_FILE=config/model_profiles.toml
```

对应的主聊天档案必须使用 Responses 协议并声明原生联网能力：

```toml
[profiles.pro]
provider = "deepseek"
protocol = "responses"
capabilities = ["tools", "reasoning", "long_context", "native_web_search"]

[routes]
chat_agent = "pro"
```

也可以继续使用 Tavily，或让它仅在原生搜索满足有界失败条件时回退：

```dotenv
# WEB_MODE=tavily
WEB_MODE=native_with_tavily_fallback
TAVILY_API_KEY=你的Tavily密钥
# 明确 URL 命中以下域名时直接使用 Tavily；子域名也会匹配。
WEB_TAVILY_DOMAINS=github.com,raw.githubusercontent.com
WEB_ALLOW_PROVIDER_OVERRIDE=true
WEB_FALLBACK_ON_ACCESS_DENIED=true
WEB_FALLBACK_ON_TARGET_MISS=true
WEB_SEARCH_DEPTH=advanced
```

然后只重建 Bot 容器：

```bash
docker compose up -d --no-deps --force-recreate bot
```

配置规则：

- `WEB_MODE=disabled`：不注册任何联网能力。
- `WEB_MODE=native`：只使用 Responses 原生联网，不创建本地 Tavily 客户端。
- `WEB_MODE=tavily`：只使用本地 Tavily Function Tool，必须配置 `TAVILY_API_KEY`。
- `WEB_MODE=native_with_tavily_fallback`：原生优先并允许一次有界 Tavily 回退，也必须配置 Key。
- 混合模式下，`WEB_TAVILY_DOMAINS` 使用逗号分隔域名。规则按主机名边界匹配，
  `github.com` 会匹配 `api.github.com`，不会匹配 `evilgithub.com`。
- `WEB_ALLOW_PROVIDER_OVERRIDE=true` 时，当前用户消息只要包含 `Tavily`、`Tavility` 或
  `塔维利` 就直接选择 Tavily，不再要求“用”“搜索”等旧语法。网页正文和工具结果不能
  改变 Provider。
- 原生 `open_page` 失败或没有真正打开用户明确给出的目标 URL 时，可进行一次
  `native -> Tavily` 回退；不会在两个 Provider 之间循环。
- 未设置 `WEB_MODE` 时兼容旧 `WEB_ENABLED`：`true` 映射为 `tavily`，`false` 映射为
  `disabled`。
- 搜索词最多 400 字符，不会自动拼入完整聊天历史、人物记忆或系统提示词。
- 只接受公开 HTTP(S) URL；拒绝凭据 URL、localhost、私有 IP、链路本地地址和内部 Docker 主机名。
- 搜索结果正文只存在于当轮 LLM 工具上下文，不写入聊天账本或人物/群记忆。
- 数据库只保存实际使用来源的标题、URL、简短摘要和时间，每个会话最多保留最近 10 次，默认清理 7 天前记录。

来源由后端控制：

- 普通联网问题只发送总结，不显示 URL、引用编号或来源列表。
- 明确要求“来源、出处、原文链接、参考资料、引用、网址”等内容时，正文后会再发送一条由后端生成的真实来源消息。
- 下一条只问“来源呢”“链接”“把网址发我”等短追问时，不调用 LLM、不重新搜索，直接读取当前隔离会话最近一次来源。
- 私聊用户之间、不同群之间、同一群的不同成员之间都不能互相读取来源记录。
- 模型自行生成的来源段落或虚构链接不会进入后端来源列表。

## 群聊观察与自主参与

禁用群只处理超级管理员的启用命令。已启用群的未触发消息会更新人物、成员、账本和记忆任务，但不会阻断其他 NoneBot 插件。

在已启用群中，可以只发送一个 `@Yuki` 而不附带文字；该消息会进入正常聊天 Agent，让 Yuki 自然回应。后端只把最小的“仅被提及”上下文交给模型，永久事件账本仍保存真实的空文本消息，不伪造用户发言。

Conversation Runtime 自主参与规则：

- 群消息静默窗口结束后，最多按 `conversation.autonomous_batch_limit`（默认 8）组成受限批次；
- 默认必要性门槛为 `0`，非空群聊批次都会交给本地评分判断是否自然参与；
- 达到阈值后开一轮只读 Main Agent，不再生成 `wait` / `silent` TurnPlan；
- 评分以活跃群友为默认倾向，能自然接话、参与玩笑、回应情绪或延续话题时优先发言；
- 真实 `@Yuki`、回复 Yuki 和私聊由后端强制回复，不走自主评分；
- 旧置信度、冷却、每小时上限、前置 Planner 和旧自主引擎已经删除，不再存在两套群聊决策；
- 新群消息会中断自主轮和自主生成，但普通观察消息不会中断明确触发的处理轮；
- 自主轮不开放通用 OneBot 管理工具，默认 `read_only`；
- 最终回复仍由同一个 Yuki Agent 生成，并使用普通消息与发送节奏。

普通聊天不再经过 Planner。从 3.5.3 升级时由 `setup migrate-3-6` 改写 `.env` 与运行时覆盖；
旧 `PLANNER_*` 残留环境变量会被忽略。完整步骤见 [3.6.0 升级指南](upgrade-3.6.0.md)。

## 命令

| 命令 | 作用 |
|---|---|
| `/ai help` | 显示帮助 |
| `/ai new` | 私聊新建 Bot+peer 会话；群聊由超级管理员为整个 Bot+群切换 generation |
| `/ai status` | 显示连接、模型、当前 Scope/generation/Rollup 和版本 |
| `/ai stop` | 取消当前 Bot+peer 或整个 Bot+群 Scope 的可中断请求 |
| `/ai ping` | 连通性检查 |
| `/ai voice status|profiles|show|styles|test` | 查看或使用当前本地声线；管理操作仅超级管理员 |
| `/ai whoami` | 显示 QQ、昵称、本群名片、别名与记忆统计 |
| `/ai forgetme` | 彻底删除当前 QQ 的可归属数据 |
| `/ai memory list` | 查看本人的人物记忆 |
| `/ai memory add <内容>` | 添加明确人物记忆 |
| `/ai memory update <ID> <内容>` | 修改本人的人物记忆 |
| `/ai memory delete <ID>` | 删除本人的人物记忆 |
| `/ai memory evidence <ID>` | 查看本人某条 Memory V2 事实的真实消息证据 |
| `/ai memory search person <QQ号> <query>` | 超级管理员诊断指定人物的词法召回 |
| `/ai memory search group <群号> <query>` | 超级管理员诊断指定群的词法召回 |
| `/ai memory index status` | 超级管理员查看 Memory V2 FTS 健康状态 |
| `/ai memory index rebuild` | 超级管理员只重建派生 FTS 索引 |
| `/ai memory embedding status` | 超级管理员查看向量覆盖率、任务与当前 profile |
| `/ai memory embedding doctor` | 超级管理员用固定无隐私文本执行一次 Provider 远程诊断 |
| `/ai memory embedding retry` | 超级管理员重新排队当前 profile 的失败任务 |
| `/ai memory embedding rebuild` | 超级管理员为全部当前 active facts 重建当前 profile 向量 |
| `/ai memory embedding purge-old` | 超级管理员清理非当前 profile 的旧派生向量和任务 |
| `/ai memory self-reflection run` | 超级管理员立即运行一轮有界的 Yuki Self Reflection；跳过时间和消息量阈值，但保留每日调用上限与会话隔离 |
| `/ai memory rebuild list` | 超级管理员列出受控历史重建任务 |
| `/ai memory rebuild plan <selection-json>` | 固定事件快照并统计范围，不调用模型 |
| `/ai memory rebuild start <run_id>` | 显式开始逐事件提取，只暂存 proposal |
| `/ai memory rebuild status <run_id>` | 查看无正文状态、checkpoint 与统计 |
| `/ai memory rebuild pause\|resume\|cancel <run_id>` | 暂停、显式恢复或取消后续处理 |
| `/ai memory rebuild review <run_id> [page]` | 分页审阅有界 proposal 摘要 |
| `/ai memory rebuild approve\|reject <run_id> <all\|ids\|filter-json>` | 批准或拒绝 claim |
| `/ai memory rebuild commit <run_id>` | 所有 claim 审阅后按历史顺序提交 |
| `/ai memory rebuild retry\|purge <run_id>` | 显式恢复失败任务或清理终态暂存数据 |
| `/ai preference list` | 查看本人的交互偏好 |
| `/ai preference set <键> <值>` | 设置交互偏好 |
| `/ai preference delete <键>` | 删除交互偏好 |
| `/ai automation list` | 只列出当前任务，显示稳定的自动化 ID、本地时间与状态 |
| `/ai automation completed` | 单独列出已完成、取消、失败或阻塞的历史任务及自动化 ID |
| `/ai automation show <自动化ID>` | 查看指定任务、调度与下次执行时间 |
| `/ai automation pause <自动化ID>` | 暂停指定任务 |
| `/ai automation resume <自动化ID>` | 重新计算时间并恢复指定任务 |
| `/ai automation cancel <自动化ID>` | 永久取消指定任务并移入历史 |
| `/ai automation run <自动化ID>` | 将指定任务调度为尽快执行 |
| `/ai automation history <自动化ID>` | 查看指定任务最近执行状态与错误类别 |
| `/ai affection show` | 查看本人的好感度、信任度、有效信任度和阶段 |
| `/ai affection history` | 查看本人最近 10 次关系变化 |
| `/ai affection show user <QQ号>` | 超级管理员查看指定人物 |
| `/ai affection history user <QQ号>` | 超级管理员查看指定人物最近 10 次变化 |
| `/ai affection set user <QQ号> <0-100>` | 超级管理员设置好感度 |
| `/ai affection adjust user <QQ号> <-20到20>` | 超级管理员调整好感度 |
| `/ai affection trust user <QQ号> <0-100>` | 超级管理员设置信任度 |
| `/ai capabilities [类别]` | 所有用户按当前真实 QQ 查看完整权限、可改参数数量和接口范围 |
| `/ai config list [类别]` | 列出显式注册的配置键和生效方式 |
| `/ai config get <key>` | 读取全局有效值；凭证只显示是否已配置 |
| `/ai config set <key> <value>` | 设置全局数据库覆盖 |
| `/ai config set <key> <value> group current` | 设置当前群覆盖 |
| `/ai config set <key> <value> user <QQ号>` | 设置指定用户覆盖 |
| `/ai config unset <key> [...]` | 删除同一作用域覆盖，恢复较低优先级值 |
| `/ai config history [key]` | 查看当前管理员的配置修改记录 |
| `/ai config rollback <change_id>` | 回滚本人尚未被后续修改覆盖的配置变更 |
| `/ai on` / `/ai off` | 超级管理员启用/停用当前群 |
| `/ai group <群号> on\|off` | 超级管理员启用/停用指定群 |
| `/ai private <QQ号> on\|off` | 超级管理员恢复/阻止指定 QQ 私聊 |

超级管理员可在 memory/preference 的操作名后加 `user <QQ号>`，例如：

```text
/ai memory list user 123456789
/ai preference set user 123456789 reply_style 简短
```

## 自然语言管理与运行时配置

### 统一权限能力目录

当用户问“我能修改什么”“有哪些设置”“我的权限范围”或“能改多少参数”时，Yuki 必须调用
后端能力目录，不能根据提示词或聊天记忆猜测。普通用户和超级管理员的同一个 Agent 都使用只读
工具 `get_my_capabilities`；它属于独立 `capability` scope，并在真实用户聊天且 ToolMode 非
NONE 时保留，但不会额外授予任何权限。确定性诊断入口为 `/ai capabilities [类别]`。两种入口
读取同一个 `PermissionCatalogService`，自然语言工具结果只给当前模型轮内部使用。

权限只从当前真实 OneBot 事件的 `sender.user_id` 解析：

| 等级 | 当前状态 | 能力范围 |
|---|---|---|
| `user` | 已启用，所有普通 QQ | 29 项本人自助接口，其中 14 项可修改本人上下文、记忆、偏好、时区或自动化任务；不能修改运行时配置 |
| `trusted` | 仅预留，当前不可分配 | 供未来介于普通用户与管理员之间的权限扩展 |
| `moderator` | 仅预留，当前不可分配 | 供未来群管理能力扩展 |
| `superuser` | 已启用，来自 `.env` 的 `SUPERUSERS` | 163 项可修改配置、12 项受保护配置、44 项管理员业务接口（33 项修改型），以及 1 个可调用全部 NapCat/OneBot 公开 action 的通用网关 |

能力目录直接遍历现有 `ConfigRegistry` 和 `ActionRegistry`，不会另复制配置键或业务 action。`summary` 只提供计数与类别，`focused` 提供命中项的 ID、别名、说明、类型、范围、作用域和生效方式，`full` 才提供全部 ID。`call_onebot_api(action, params)` 作为独立的 `onebot` 权限类别列出：真实超级管理员在直接触发、非自主群聊的普通 Agent 轮次中可调用全部公开 action，不设 action denylist，也不二次确认；使用网页工具后本轮会撤销网关，但不会缩减 action 范围。目录不读取配置值、API Key、凭证状态或他人权限。`trusted`、`moderator` 只有枚举和展示元数据，在执行层接入相同权限校验前不会被实际授予。

管理员身份只在 `MessageProcessor` 中按当前真实 OneBot 事件验证：

```text
current_event.sender.user_id in 启动时加载的 SUPERUSERS
```

模型输入中的 QQ、引用发送者、`@管理员`、聊天历史、记忆、网页和“我是管理员”等文本都不能授权。配置值经过显式注册表、类型转换、数值范围、允许作用域和交叉字段校验后才会写库。模型不能修改 `.env`、`SUPERUSERS`、密钥或数据库地址，也没有 Shell、Python、文件写入、任意 SQL、任意 HTTP 管理或 Docker 控制工具。

有效配置按以下顺序解析：

```text
用户级数据库覆盖
  > 群级数据库覆盖
  > 全局数据库覆盖
  > .env 启动值
  > 代码默认值
```

配置生效方式：

| 模式 | 行为 |
|---|---|
| `HOT` | 下一条相关消息或下一次自主判断重新生成 `RuntimeConfigSnapshot`，无需重启 |
| `FUTURE_ONLY` | 只在之后创建人物关系、来源记录或清理任务时读取，不追改旧记录 |
| `RESTART_REQUIRED` | 覆盖立即保存为 pending，当前进程继续使用启动值；下次启动先加载覆盖再创建模型客户端、并发器和限流器 |
| `SECRET` | 只返回是否已配置；真实密钥不能通过命令或自然语言工具读取、修改或写入审计正文 |

`/ai status` 会显示待重启配置数。运行时覆盖在容器重建和应用重启后仍保留；`unset` 会恢复同一键的较低优先级值。

首批可修改键：

| 模式 | 配置键 |
|---|---|
| HOT | `conversation.autonomous_enabled`、`conversation.autonomous_debounce_seconds`、`conversation.autonomous_admission_threshold`、`conversation.autonomous_batch_limit`、`conversation.autonomous_presence_window_seconds`、`conversation.interrupt_autonomous_on_new_message` |
| HOT | `context.local_event_limit`、`memory.max_referenced_targets` |
| HOT | `reply.delay_min_seconds`、`reply.delay_max_seconds`、`reply.max_qq_message_chars`、`reply.hard_max_messages` |
| HOT | `llm.temperature`、`llm.max_output_tokens`、`llm.thinking_enabled` |
| HOT | `agent.max_tool_calls`、`agent.max_model_requests`、`agent.tool_result_max_characters` |
| HOT | `web.search_max_results`、`web.extract_max_results`、`web.max_calls_per_turn`、`web.tool_result_max_characters` |
| HOT | `relationship.confidence_threshold`、`relationship.max_auto_delta`、`relationship.daily_positive_cap`、`relationship.daily_negative_cap`、`relationship.conflict_preference_min_gap` |
| HOT | `vision.max_images_per_turn`、`vision.max_frames_per_turn`、`vision.gif_max_frames`、`vision.thinking_enabled`、`vision.thinking_budget`、`vision.low_confidence_retry_threshold`、`vision.per_user_requests_per_minute`、`vision.per_group_requests_per_minute` |
| FUTURE_ONLY | `relationship.initial_affection`、`relationship.initial_trust`、`web.source_retention_days`、`web.source_max_runs_per_conversation`、`vision.analysis_retention_days` |
| RESTART_REQUIRED | `llm.model`、`llm.timeout_seconds`、`llm.max_retries`、`global.llm_concurrency`、`web.global_concurrency`、`rate_limit.per_user_per_minute`、`rate_limit.per_group_per_minute` |
| RESTART_REQUIRED | `vision.enabled`、`vision.base_url`、`vision.model`、`vision.global_concurrency`、`vision.queue_max_pending`、`vision.queue_timeout_seconds`、`vision.media_download_timeout_seconds`、`vision.timeout_seconds`、`vision.max_output_tokens` |
| RESTART_REQUIRED | `automation.enabled`、`automation.poll_seconds`、`automation.lease_seconds`、`automation.max_active_per_superuser`、`automation.max_active_per_user`、`automation.max_steps`、`automation.max_llm_calls_per_run`、`automation.max_tool_calls_per_run`、`automation.max_messages_per_run`、`automation.max_runtime_seconds`、`automation.min_interval_seconds`、`automation.default_misfire_grace_seconds`、`automation.max_consecutive_failures`、`automation.run_retention_days` |

不可通过管理员工具修改：

- `app.host`、`app.port`、`database.url`、`superusers`、启动默认 `ENABLED_GROUPS`；
- `LLM_API_KEY`、`TAVILY_API_KEY`、`VISION_API_KEY`、`ONEBOT_ACCESS_TOKEN`、`NAPCAT_WEBUI_TOKEN`、数据库密码和 QQ 登录凭据；
- 系统提示词和任何未在 `ConfigRegistry` 显式登记的 `Settings` 字段。

凭证查询最多返回“已配置/未配置”，不会返回真实内容。审计表保存真实管理员 QQ、触发消息 ID、会话键、能力、目标、脱敏前后状态、成功标记、错误类别和耗时；不保存 API Key、完整网页正文、系统提示词或隐藏推理。回滚只支持配置覆盖，且必须由原操作者执行、当前覆盖仍与原变更的 after 版本一致；记忆删除、关系变化、已发消息和 OneBot 操作不提供通用回滚。

同一聊天轮可以在总工具预算内顺序执行多个不同的修改或人物业务操作，后端会逐项校验权限、参数与真实结果；参数完全相同的重复写入会被拦截，避免模型循环提交同一个动作。`memory.list`、`preference.list`、关系查询和配置读取等只读结果中的人物记忆、偏好和历史文本始终是不可信资料，不能自行产生新的修改意图。修改失败时，后端会覆盖模型的成功措辞并明确提示操作未完成。批量清理旧的低重要度自动记忆应使用原子动作 `memory.prune`，显式记忆不会被该动作删除。

## 新配置默认值

| 环境变量 | 默认值 |
|---|---:|
| `OBSERVE_ENABLED_GROUPS` | `true` |
| `RECENT_HISTORY_TOOL_LIMIT` | `20` |
| `LOCAL_CONTEXT_EVENT_LIMIT` | `2048` |
| `MAX_CONTEXT_CHARACTERS` | `81920`（字符，不是 token） |
| `CONTEXT_METADATA_BUDGET_RATIO` | `0.06`（约 5k 字符 Actor 元数据上限） |
| `HISTORY_WINDOW_LOW_WATERMARK_RATIO` | `0.67`（保留键；Prompt 选窗不再使用） |
| `CONVERSATION_ROLLUP_RAW_TAIL_EVENTS` | `768` |
| `CONVERSATION_ROLLUP_RAW_TAIL_CHARACTERS` | `65536` |
| `CONVERSATION_ROLLUP_TRIGGER_EVENTS` / `STOP_EVENTS` | `512` / `192` |
| `CONVERSATION_ROLLUP_TRIGGER_CHARACTERS` / `STOP_CHARACTERS` | `49152` / `16384` |
| `CONVERSATION_ROLLUP_FOREGROUND_MAX_BATCHES` | `3` |
| `CONVERSATION_EFFECT_GATE_TIMEOUT_SECONDS` | `30` |
| `TOOLING_SELECTED_TOOL_LIMIT` | `32` |
| `TOOLING_SCHEMA_TOKEN_BUDGET` | `12000` |
| `MCP_SELECTED_TOOL_LIMIT` | `16` |
| `MCP_SCHEMA_TOKEN_BUDGET` | `8000` |
| `MEMORY_MAX_REFERENCED_TARGETS` | `5` |
| `PERSON_MEMORY_MAX_ENTRIES` | `100` |
| `GROUP_MEMORY_MAX_ENTRIES` | `100` |
| `PERSON_GROUP_MEMORY_MAX_ENTRIES` | `50` |
| `PREFERENCE_MAX_ENTRIES` | `30` |
| `MEMORY_BATCH_SECONDS` | `30` |
| `MEMORY_BATCH_TRIGGER_COUNT` | `12` |
| `MEMORY_BATCH_MAX_EVENTS` | `12` |
| `MEMORY_BATCH_MAX_CHARACTERS` | `8000` |
| `MEMORY_BATCH_MAX_WAIT_SECONDS` | `300` |
| `MEMORY_BATCH_MAX_OUTPUT_TOKENS` | `4096` |
| `MEMORY_RETRIEVAL_ENABLED` | `true` |
| `SELF_MEMORY_ENABLED` | `true` |
| `MEMORY_SELF_REFLECTION_ENABLED` | `true` |
| `MEMORY_SELF_REFLECTION_SCHEDULE_HOURS` | `4,12,20` |
| `MEMORY_SELF_REFLECTION_TIMEZONE` | `Asia/Shanghai` |
| `MEMORY_SELF_REFLECTION_MAX_BATCHES_PER_RUN` | `12` |
| `MEMORY_SELF_REFLECTION_MAX_BATCHES_PER_CONVERSATION_PER_RUN` | `7` |
| `MEMORY_SELF_REFLECTION_MAX_DAILY_CALLS` | `36` |
| `MEMORY_SELF_REFLECTION_EVENT_THRESHOLD` | `50` |
| `MEMORY_SELF_REFLECTION_CHARACTER_THRESHOLD` | `8000` |
| `MEMORY_SELF_REFLECTION_LOW_EVENT_THRESHOLD` | `30` |
| `MEMORY_SELF_REFLECTION_LOW_CHARACTER_THRESHOLD` | `4800` |
| `MEMORY_SELF_REFLECTION_NATURAL_GAP_SECONDS` | `300` |
| `MEMORY_SELF_REFLECTION_MAX_WAIT_SECONDS` | `28800` |
| `MEMORY_SELF_REFLECTION_MAX_EVENTS` | `100` |
| `MEMORY_SELF_REFLECTION_MAX_CHARACTERS` | `8000` |
| `MEMORY_LEXICAL_CANDIDATE_LIMIT` | `50` |
| `MEMORY_CONTEXT_LIMIT_PER_ENTITY` | `8` |
| `MEMORY_OVERVIEW_LIMIT_PER_ENTITY` | `20` |
| `MEMORY_ALWAYS_ON_EXPLICIT_PREFERENCE_LIMIT` | `3` |
| `MEMORY_QUERY_TERM_LIMIT` | `12` |
| `MEMORY_SHORT_QUERY_FALLBACK_ENABLED` | `true` |
| `MEMORY_SEMANTIC_ENABLED` | `true` |
| `MEMORY_SEMANTIC_CANDIDATE_LIMIT` | `50` |
| `MEMORY_SEMANTIC_MIN_SIMILARITY` | `0.35` |
| `MEMORY_HYBRID_LEXICAL_WEIGHT` | `1.0` |
| `MEMORY_HYBRID_SEMANTIC_WEIGHT` | `1.0` |
| `MEMORY_HYBRID_RRF_K` | `60` |
| `MEMORY_EMBEDDING_ENABLED` | `false` |
| `MEMORY_EMBEDDING_PROVIDER` | `qwen_dashscope` |
| `MEMORY_EMBEDDING_MODEL` | `qwen3.7-text-embedding` |
| `MEMORY_EMBEDDING_DIMENSIONS` | `1024` |
| `MEMORY_EMBEDDING_WORKER_ENABLED` | `true` |
| `MEMORY_EMBEDDING_WORKER_CLAIM_LIMIT` | `100` |
| `MEMORY_EMBEDDING_HTTP_CONCURRENCY` | `2` |
| `MEMORY_EMBEDDING_QUERY_CACHE_TTL_SECONDS` | `600` |
| `MEMORY_EMBEDDING_QUERY_CACHE_MAX_ENTRIES` | `512` |
| `DAILY_CHAT_MESSAGE_DELAY_MIN_SECONDS` | `1` |
| `DAILY_CHAT_MESSAGE_DELAY_MAX_SECONDS` | `2` |
| `LLM_MODEL` | `deepseek-v4-flash` |
| `LLM_THINKING_ENABLED` | `true` |
| `LLM_REASONING_EFFORT` | `high`（默认；DeepSeek V4 支持 `high` / `max`） |
| `LLM_TIMEOUT_SECONDS` | `120` |
| `LLM_MAX_RETRIES` | `2` |
| `LLM_MAX_OUTPUT_TOKENS` | `8192` |
| `AGENT_MAX_TOOL_CALLS` | `8` |
| `AGENT_MAX_MODEL_REQUESTS` | `6` |
| `AGENT_TOOL_RESULT_MAX_CHARACTERS` | `8000` |
| `CONVERSATION_AUTONOMOUS_ENABLED` | `true` |
| `CONVERSATION_AUTONOMOUS_DEBOUNCE_SECONDS` | `3` |
| `CONVERSATION_AUTONOMOUS_ADMISSION_THRESHOLD` | `0` |
| `CONVERSATION_AUTONOMOUS_BATCH_LIMIT` | `8`（热配置范围 `1`～`100`） |
| `CONVERSATION_AUTONOMOUS_PRESENCE_WINDOW_SECONDS` | `300` |
| `CONVERSATION_INTERRUPT_AUTONOMOUS_ON_NEW_MESSAGE` | `true` |
| `REPLY_SEQUENCE_CANCEL_ON_NEW_MESSAGE` | `true` |
| `REPLY_HARD_MAX_MESSAGES` | `10`（可热更新至 `20`） |
| `PLUGIN_SYSTEM_ENABLED` | `false` |
| `PLUGIN_DIRECTORY` | `plugins` |
| `PLUGIN_API_VERSION` | `2.0` |
| `PLUGIN_DIRECT_COMMAND_BINDINGS` | `{}` |
| `PLUGIN_HOOK_TIMEOUT_SECONDS` | `3` |
| `PLUGIN_START_TIMEOUT_SECONDS` | `10` |
| `PLUGIN_STOP_TIMEOUT_SECONDS` | `10` |
| `PLUGIN_MAX_PROMPT_FRAGMENT_CHARACTERS` | `2000` |
| `PLUGIN_MAX_PROMPT_CHARACTERS_PER_PLUGIN` | `4000` |
| `PLUGIN_MAX_TOTAL_PROMPT_CHARACTERS` | `8000` |
| `PLUGIN_BACKGROUND_TASK_LIMIT` | `4` |
| `PLUGIN_FAILURE_DISABLE_THRESHOLD` | `3` |
| `PLUGIN_HTTP_MAX_RESPONSE_BYTES` | `2097152` |
| `PLUGIN_HTTP_TIMEOUT_SECONDS` | `15` |
| `PLUGIN_AI_SESSION_MAX_HISTORY_MESSAGES` | `200` |
| `AUTOMATION_ENABLED` | `false` |
| `DEFAULT_TIMEZONE` | `Asia/Shanghai` |
| `AUTOMATION_POLL_SECONDS` | `2` |
| `AUTOMATION_LEASE_SECONDS` | `120` |
| `AUTOMATION_MAX_ACTIVE_PER_SUPERUSER` | `50` |
| `AUTOMATION_MAX_ACTIVE_PER_USER` | `10` |
| `AUTOMATION_MAX_STEPS` | `16` |
| `AUTOMATION_MAX_LLM_CALLS_PER_RUN` | `10` |
| `AUTOMATION_MAX_TOOL_CALLS_PER_RUN` | `16` |
| `AUTOMATION_MAX_MESSAGES_PER_RUN` | `10` |
| `AUTOMATION_MAX_RUNTIME_SECONDS` | `600` |
| `AUTOMATION_MIN_INTERVAL_SECONDS` | `60` |
| `AUTOMATION_DEFAULT_MISFIRE_GRACE_SECONDS` | `1800` |
| `AUTOMATION_MAX_CONSECUTIVE_FAILURES` | `3` |
| `AUTOMATION_RUN_RETENTION_DAYS` | `30` |
| `RELATIONSHIP_ENABLED` | `true` |
| `RELATIONSHIP_INITIAL_AFFECTION` | `50` |
| `RELATIONSHIP_INITIAL_TRUST` | `50` |
| `RELATIONSHIP_BATCH_SECONDS` | `60` |
| `RELATIONSHIP_BATCH_TRIGGER_COUNT` | `5` |
| `RELATIONSHIP_BATCH_MAX_TURNS` | `10` |
| `RELATIONSHIP_MAX_ATTEMPTS` | `3` |
| `RELATIONSHIP_CONFIDENCE_THRESHOLD` | `0.75` |
| `AFFECTION_MAX_AUTO_DELTA` | `2` |
| `TRUST_MAX_AUTO_DELTA` | `2` |
| `TRUST_AFFECTION_CAP_OFFSET` | `10` |
| `CONFLICT_PREFERENCE_MIN_GAP` | `15` |
| `RELATIONSHIP_DAILY_POSITIVE_CAP` | `0`（不限额） |
| `RELATIONSHIP_DAILY_NEGATIVE_CAP` | `0`（不限额） |
| `WEB_ENABLED` | `false` |
| `WEB_SEARCH_DEPTH` | `advanced` |
| `WEB_SEARCH_MAX_RESULTS` | `5` |
| `WEB_EXTRACT_MAX_RESULTS` | `3` |
| `WEB_TIMEOUT_SECONDS` | `20` |
| `WEB_MAX_RETRIES` | `1` |
| `WEB_GLOBAL_CONCURRENCY` | `4` |
| `WEB_MAX_CALLS_PER_TURN` | `3` |
| `WEB_TOOL_RESULT_MAX_CHARACTERS` | `16000` |
| `WEB_SOURCE_RETENTION_DAYS` | `7` |
| `WEB_SOURCE_MAX_RUNS_PER_CONVERSATION` | `10` |
| `VISION_ENABLED` | `false` |
| `VISION_PROVIDER` | `qwen` |
| `VISION_BASE_URL` | 空（启用时必填） |
| `VISION_API_KEY` | 空（启用时必填、敏感） |
| `VISION_MODEL` | `qwen3.7-plus` |
| `VISION_TIMEOUT_SECONDS` | `120` |
| `VISION_MAX_RETRIES` | `1` |
| `VISION_GLOBAL_CONCURRENCY` | `4` |
| `VISION_QUEUE_MAX_PENDING` | `32` |
| `VISION_QUEUE_TIMEOUT_SECONDS` | `120` |
| `VISION_MEDIA_DOWNLOAD_TIMEOUT_SECONDS` | `120` |
| `VISION_ALLOW_PRIVATE_URLS` | `false`；TUN/Fake-IP 环境可设为 `true`，会解除图片 URL 的本地、私有及保留地址拦截 |
| `VISION_MAX_OUTPUT_TOKENS` | `8192` |
| `VISION_THINKING_ENABLED` | `false` |
| `VISION_THINKING_BUDGET` | `6144` |
| `VISION_LOW_CONFIDENCE_RETRY_THRESHOLD` | `0.65` |
| `VISION_MAX_IMAGES_PER_TURN` | `5` |
| `VISION_MAX_FRAMES_PER_TURN` | `16` |
| `VISION_GIF_MAX_FRAMES` | `8` |
| `VISION_MAX_DOWNLOAD_BYTES` | `20971520` |
| `VISION_MAX_PREPARED_BYTES` | `16777216` |
| `VISION_MAX_DIMENSION` | `4096` |
| `VISION_MAX_PIXELS` | `16777216` |
| `VISION_PER_USER_REQUESTS_PER_MINUTE` | `20` |
| `VISION_PER_GROUP_REQUESTS_PER_MINUTE` | `60` |
| `VISION_ANALYSIS_RETENTION_DAYS` | `7` |

所有新 QQ 私聊默认准入；指定 QQ 的阻止与恢复由持久化 `/ai private <QQ号> off|on` 管理。

## 本地开发与测试

```bash
uv sync --all-extras
uv run qq-ai-bot-cli init-db
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
uv run pytest -q examples/plugins/com.example.echo/tests
uv run qq-ai-bot
```

Docker 验证：

```bash
docker compose config
docker compose up -d
docker compose ps
```

源码开发构建验证：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml build bot
```

健康检查不会请求 DeepSeek、Tavily、Qwen 或执行真实自动化，也不会暴露密钥；`plugin_system_enabled/running_count`、`web_configured`、`vision_configured`、`automation_worker_running`、`active_automation_count`、`mcp_automation_tools` 和 `mcp_automation_missing_tools` 都只读取本地配置或运行状态：

```bash
docker compose exec bot python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
```

## 服务器部署

- 推荐 2 核、2–4 GB 内存、20 GB SSD 的 Linux 小服务器。
- 不向公网暴露 bot 的 8080 端口。
- NapCat WebUI 只绑定 `127.0.0.1:6099`；远程访问使用 SSH 隧道：

  ```bash
  ssh -L 6099:127.0.0.1:6099 user@server
  ```

- 定期离线备份 `data/`、`napcat-data/` 和 `napcat-config/`。
- NapCat 是个人 QQ 协议端，不等同于腾讯官方 QQ Bot，存在协议变化、风控和封号风险；请控制频率并使用你有权控制的账号。

`/ai status` 会同时显示视觉是否启用、视觉模型、是否繁忙以及当前“排队/运行”数量，不显示密钥或完整接口查询参数。

## 3.0.0a1 升级步骤

1. 停止 Bot 写入但保持 NapCat 和 QQ 登录态运行：`docker compose stop bot`。
2. 完整备份 `data/`、`napcat-data/` 和 `napcat-config/`。
3. 对照最新 `.env.example`；本次没有新增密钥，Memory V2 沿用现有容量与批处理配置。
4. 执行 `uv run alembic upgrade head` 到 `0020`。该操作会永久删除全部旧记忆、偏好和旧记忆
   任务，但保留人物、群、聊天事件、关系、自动化和插件数据。
5. 执行 `docker compose up -d --no-deps --force-recreate bot`；只重建 Bot，NapCat 和 QQ 登录态不变。
6. 检查 `docker compose ps`、`/healthz` 和日志，再验证私聊、群聊 @、记忆命令和插件。

`0020` 不支持 downgrade，不会自动从 2.1.2 记忆或历史聊天重建事实。唯一回退方式是停止服务
并恢复升级前完整数据库备份。完整说明见 [Memory V2 升级指南](upgrade-memory-v2.md)。

## 3.0.0a2 升级步骤

1. 停止 Bot 写入但保持 NapCat 登录态：`docker compose stop bot`。
2. 备份 `data/` 后执行 `uv run alembic upgrade head`，升级到 `0021`。
3. `0021` 会从现有 `memory_facts` 回填 FTS5；不会读取旧版记忆或扫描聊天历史。
4. 执行 `docker compose up -d --no-deps --force-recreate bot`，再用 `/ai memory index status` 检查索引。

本次 downgrade 只会删除 FTS 表和触发器，不删除 Memory V2 facts 或 evidence。索引异常时使用
`/ai memory index rebuild` 重建派生数据，不需要删除数据库。

## 3.0.0b1 升级步骤

1. 停止 Bot 写入但保持 NapCat 登录态：`docker compose stop bot`。
2. 备份 `data/` 后执行 `uv run alembic upgrade head`，升级到 `0022`。
3. 若暂不使用语义检索，保持 `MEMORY_EMBEDDING_ENABLED=false` 即可；行为与 3.0.0a2 一致。
4. 若要启用，在 `.env` 设置 `MEMORY_EMBEDDING_ENABLED=true`、DashScope 兼容端点和
   `MEMORY_EMBEDDING_API_KEY`。密钥只从环境读取，不会进入数据库或诊断输出。
5. 执行 `docker compose up -d --no-deps --force-recreate bot`，再运行
   `/ai memory embedding status` 和 `/ai memory embedding doctor`。

`0022` 不修改 Memory V2 事实、证据、FTS 或聊天账本。首次启用后由持久后台任务渐进补齐当前
事实；不会扫描历史聊天。模型、维度或模板改变会建立新 profile，旧派生数据可在新 profile
覆盖完成后用 `/ai memory embedding purge-old` 清理。

## 3.0.0b2 升级步骤

1. 只停止 Bot：`docker compose stop bot`，不要停止 NapCat。
2. 完整备份 `data/`，然后执行 `uv run alembic upgrade head`，确认 head 为 `0023`。
3. 对照 `.env.example` 补充 Memory consolidation、evidence 和 maintenance 配置；这些项目不含
   新密钥，默认路由到已有 Flash 模型档案。
4. 执行 `docker compose up -d --no-deps --force-recreate bot`，检查 `/healthz`、
   `/ai memory doctor` 和 `/ai memory maintenance status`。
5. 分别验证本人修正、群内真实 @ 第三方事实、矛盾陈述、撤回与恢复；确认 NapCat 登录态不变。

`0023` 会保留全部现有事实、证据、FTS、Embedding 和聊天账本。无 contested fact 时可降回
`0022`；一旦存在 contested 状态，downgrade 会明确拒绝，必须先在新版本解决冲突或恢复升级前
备份，不能通过手工删表绕过。

## 3.0.0rc1 升级步骤

1. 只停止 Bot：`docker compose stop bot`，不要停止或重建 NapCat。
2. 完整备份 `data/`，执行 `uv run alembic upgrade head`，确认 head 为 `0024`。
3. 默认保持 `MEMORY_REBUILD_ENABLED=false`；这不会创建、启动或恢复任何重建任务。
4. 需要重建时再设为 `true` 并只重建 Bot：`docker compose up -d --no-deps --force-recreate bot`。
5. 由当前真实超级管理员先执行 plan，确认统计后 start；提取完成必须 review 并逐项批准或拒绝，
   pending 为 0 后才可 commit。

`0024` 保留事实、证据、关系、状态事件、FTS、Embedding 与聊天账本。重启会将 extracting/
committing 任务改为 paused，必须显式 resume；cancel 和 purge 都不会删除已提交事实。存在非终态
重建任务时 downgrade 会拒绝。完整说明见
[受控历史重建](architecture/memory-v2-rebuild.md) 与
[Memory V2 升级指南](upgrade-memory-v2.md)。完整质量结果见
[3.0.0rc1 实施报告](releases/v3.0.0rc1.md)。

## 3.0.0 正式版检查

正式版沿用 `0024`，无需新增迁移。更新代码并重建 Bot 后运行：

```bash
uv run qq-ai-bot-cli memory quality validate-dataset
uv run qq-ai-bot-cli memory quality run --suite full
uv run qq-ai-bot-cli memory quality compare
uv run qq-ai-bot-cli memory quality performance
uv run qq-ai-bot-cli memory release-check
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --no-deps bot
```

需要审计真实 SQLite 时必须显式传 `--database-url`；命令只读且不自动治理。完整结果见
[3.0.0 正式发布报告](releases/v3.0.0.md)。
