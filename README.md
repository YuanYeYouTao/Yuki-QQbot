<div align="center">

<p>
  <img src="img/Yuki_2.png" alt="Yuki" width="280">
</p>

<h1>Yuki-QQbot</h1>

<p>面向个人部署、以长期关系和长期记忆为核心的 QQ AI Agent</p>

<p>
  <a href="https://github.com/YuanYeYouTao/Yuki-QQbot/releases/tag/v3.5.2"><img src="https://img.shields.io/badge/Version-3.5.2-orange" alt="Version 3.5.2"></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/NoneBot2-OneBot%20v11-green" alt="NoneBot2 and OneBot v11">
  <img src="https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <a href="https://github.com/YuanYeYouTao/Yuki-QQbot/actions/workflows/quality.yml"><img src="https://github.com/YuanYeYouTao/Yuki-QQbot/actions/workflows/quality.yml/badge.svg" alt="Quality"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow" alt="MIT License"></a>
</p>

<p>
  <a href="#项目概览">项目概览</a>
  ·
  <a href="#核心能力">核心能力</a>
  ·
  <a href="#memory-v2">Memory V2</a>
  ·
  <a href="#快速开始">快速开始</a>
  ·
  <a href="#文档">文档</a>
</p>

</div>

---

Yuki 不是把大模型简单接到 QQ 上的问答机器人。它以 NapCatQQ 和 NoneBot2 为通信入口，使用
一次 Planner 决策、受控 Agent 工具循环、身份隔离的 Memory V2、持久化自动化和插件系统，
让一个可自托管的 QQ 角色能够长期对话、记住人与共同经历，并安全地执行外部操作。

项目主要通过 Codex 协作开发，当前稳定版本为 **3.5.2**。它适合愿意自行维护模型配置、QQ
登录态和本地数据的个人用户；不是面向多租户的托管机器人平台。

## 项目概览

一次普通消息的主路径如下。所有图都使用纯文本表示，方便在终端、移动端和任意 Markdown
阅读器中查看。

```text
+---------+    OneBot     +---------+    event     +----------+
| QQ User | ------------> | NapCat  | -----------> | NoneBot  |
+---------+               +---------+              +-----+----+
                                                       |
                                                       v
                    +------------+    +----------------+----------------+
                    | Event DB   | <- | Normalize / Access / Dedupe     |
                    +------------+    +----------------+----------------+
                                                       |
                                                       v
                    +------------+    +----------------+----------------+
                    | Memory V2  | <- | Planner + Context Assembler     |
                    +------------+    +----------------+----------------+
                                                       |
                                                       v
                    +------------+    +----------------+----------------+
                    | Tool Kernel| <->| Main Agent + Model Runtime      |
                    +------------+    +----------------+----------------+
                                                       |
                                                       v
                    +------------+    +----------------+----------------+
                    | Audit / DB | <- | Reply Sequence + OneBot Sender  |
                    +------------+    +----------------+----------------+
                                                       |
                                                       v
                                                  [ QQ Reply ]
```

关键边界：

- QQ 事件先标准化、准入、去重并写入事件账本，再进入对话编排。
- 管理命令和静态插件绑定可走确定性入口；普通聊天只进行一次 Planner 调用。
- Planner 只表达语义意图和能力范围，不能直接发送消息、修改数据或授予身份权限。
- 主 Agent 只能看到本轮被授权的工具；数据库、OneBot、插件和外部服务都由后端执行。
- 只有 NapCat 返回真实发送回执后，系统才把回复视为已投递，并启动相应后台工作。

## 核心能力

| 模块 | 当前能力 |
| --- | --- |
| 对话编排 | 私聊、群聊、回复与 @ 元数据、多轮历史、回复必要性判断、Planner-first 路由 |
| Main Agent | OpenAI-compatible Chat Completions / Responses、思考模型、有界工具循环、输出清理与分段发送 |
| Memory V2 | 身份隔离、自动提炼、混合召回、结构化意图重排、自然衰减、使用强化、冲突与版本链 |
| Tool Kernel | Core、Admin、Automation、Plugin、MCP、Web 能力统一注册、筛选、授权、预算和审计 |
| 自动化 | 通过自然语言创建提醒与周期任务，保存真实创建者、作用域、权限和投递结果 |
| 插件系统 | Plugin API 1.1、独立 SDK、命令、工具、事件、Prompt、Planner Signal、后台服务和持久通知 |
| MCP Client | stdio 与 Streamable HTTP、动态发现、Schema 预算、并发控制、结果 Artifact |
| 联网搜索 | DeepSeek 原生搜索、Tavily 或受控降级链路，最终回答可携带来源 |
| 多模态 | 可选 Qwen 图片理解、持久化表情包系统、本地 Genie-TTS 语音回复 |
| 关系系统 | 按 QQ 身份保存独立好感度、信任度和关系阶段，并由后台任务更新 |
| 运行治理 | SQLite、Alembic、热配置、权限审计、健康检查、无正文指标和质量门禁 |

## Memory V2

Memory V2 是 Yuki 规模最大、边界最多的模块。它不是一张“聊天摘要表”，而是一套把原始事件、
证据、可验证事实、检索过程、变更记录和自适应生命周期分开的长期记忆系统。

### 设计目标

- **先隔离，再检索**：人物、群、群内人物和 Yuki 自身的记忆拥有不同作用域，语义相似不能
  绕过身份与可见性边界。
- **事实可追溯**：长期事实保留来源、Evidence、authority、confidence、有效期、状态、冲突和
  版本关系，不把模型输出直接当作无来源真相。
- **读取与写入分离**：自动召回、显式工具读取和记忆变更互斥，修改只能依据真实事务回执确认。
- **相关性会变化**：Activation 随时间自然衰减，真正支撑已发送回复的记忆才会获得强化。
- **后台工作不拖慢聊天**：提炼、Embedding、Dream、维护和使用归因由后台 Worker 完成。
- **可审计但少留正文**：Recall Receipt 保存阶段与分数，不保存用户问题、回复或记忆正文。

### 数据边界

```text
+------------------+
| Chat Event Ledger|
+--------+---------+
         |
         +--------------------> [Short History]
         |
         v
+------------------+     +------------------+
| Claims / Evidence| --->| Memory Fact      |
+------------------+     | fact / preference|
                         | episode          |
                         +--------+---------+
                                  |
                 +----------------+----------------+
                 |                |                |
                 v                v                v
          [Version Chain]  [Conflict State] [Activation State]
                 |                |                |
                 +----------------+----------------+
                                  |
                                  v
                         [Audit / Receipts]
```

事实作用域由后端根据可信 QQ 元数据建立：

| Scope | 含义 | 典型内容 |
| --- | --- | --- |
| `person` | 某个人跨会话可见的本人记忆 | 明确偏好、稳定个人事实 |
| `person_group` | 某个人在当前群语境中的记忆 | 群内称呼、局部共同经历 |
| `group` | 当前群共享的事实 | 群规则、共同事件 |
| `self` | Yuki 对自身与共同经历的记忆 | 自我经历、角色连续性 |

Planner 给出的 `subjects` 只是软排序提示。真实发送者、当前群、回复对象、被 @ 成员和 SELF
可见性始终由后端解析；模型不能凭名字创造新目标，也不能把一个人的记忆补给另一个人。

### 四条访问路径

Planner 在一次既有调用中输出 `MemoryQueryIntent`，其中包含访问方式、召回目的、主体提示、实体、
绝对时间范围、偏好类型和期望数量。`memory.access` 是首轮记忆编排的唯一入口：

```text
                         +------------------+
                         | Planner          |
                         | memory.access    |
                         +---------+--------+
                                   |
          +----------------+-------+-------+----------------+
          |                |               |                |
          v                v               v                v
      [ none ]        [ automatic ]      [ tool ]       [ mutation ]
          |                |               |                |
     no memory       auto recall       read tools       write tools
     no scope        no read tools      no recall        no recall
          |                |               |                |
          +----------------+-------+-------+----------------+
                                   |
                                   v
                              [Main Agent]
```

- `none`：本轮不需要长期记忆，也不开放 Memory Scope。
- `automatic`：普通回忆、概括、延续和核验使用自动召回；首轮不再同时暴露通用记忆工具。
- `tool`：用户明确要求调用记忆工具时跳过自动注入，只开放合法的只读记忆能力。
- `mutation`：创建、纠正、撤回和恢复跳过自动召回，只开放 `memory/write_state` 能力。

如果写入定位失败，Agent 可以再通过 `request_tools` 加载只读工具后重试。这是受控降级，不会
扩大人物、群、SELF、权限或事实状态范围。

### 自动召回

```text
[Current Message]
       +
[Last 10 Events]
       +
[Trusted Reply / Mention Metadata]
       |
       v
[Planner: MemoryQueryIntent]
       |
       v
[Backend Target Resolution]
       |
       v
[FTS Search] + [Optional Semantic Search]
       |                  |
       +--------+---------+
                v
          [RRF Fusion]
                |
                v
   [Intent + Activation Rerank]
                |
                v
          [MMR Diversity]
                |
                v
        [Global Recall Limit]
                |
                v
         [Context Budget]
                |
                v
       [Injected Memories]
```

召回过程的含义：

1. **Target Resolution** 先产生合法身份目标；这一层是硬边界。
2. **FTS / Semantic** 使用 SQLite FTS 与可选 Qwen Embedding 生成候选；任一通道不可用时可局部降级。
3. **RRF** 融合词法和语义名次，避免把不同量纲的原始分数直接相加。
4. **Intent Rerank** 根据 `purpose`、主体、实体、时间、记忆类型与 Activation 调整相关度；它不
   解析新身份，也不使用额外 rerank 模型。
5. **MMR** 在同一身份分区内减少高度重复的候选。
6. **Global Limit** 先保留精确命中和显式偏好，再执行稳定的整轮与每目标上限。
7. **Context Budget** 只把预算内的最终事实放入主 Agent Prompt，并记录 injected 阶段。

默认自动注入上限：

| Purpose / Mode | 整轮上限 |
| --- | ---: |
| `background` | 3 |
| `continuation` | 4 |
| `recall` / `verify` / `correct` | 6 |
| `overview` | 8 |

每个合法目标最多 4 条。若用户明确要求 overview 返回 N 条，系统使用 `min(N + 2, 8)` 作为内部
候选余量；显式工具读取、管理搜索和 Plugin Memory Facade 不受这组自动注入上限影响。

### Activation、归因与强化

Activation 只参与排序，不会让事实自动变成无效，也不是硬过滤条件。当前值在读取时按指数函数
惰性计算，默认半衰期如下：

| 记忆类型 / 来源 | 默认半衰期 |
| --- | ---: |
| Episode | 14 天 |
| Fact | 60 天 |
| Preference | 120 天 |
| Explicit source / authority | 365 天 |

高重要性事实会获得更长半衰期；低置信度或低重要性的自动记忆会更快衰减。高度精确但 Activation
较低的旧事实仍可被召回。

```text
[candidate] -> [selected] -> [injected] -> [sent reply]
                                              |
                                              v
                                      [In-memory Queue]
                                              |
                                              v
                                      [Flash Attribution]
                                              |
                              +---------------+---------------+
                              |                               |
                              v                               v
                         [not used]                       [used refs]
                                                              |
                                                              v
                                                   [CAS Reinforcement]
                                                              |
                                                              v
                                                        [reinforced]
```

主 Agent 不负责自报“用了哪些记忆”。正文或由正文生成的语音成功发送后，单实例后台 Worker 才
使用 Flash 模型判断本轮白名单 Exposure 中哪些事实实质支撑了回答。新前台请求可以抢占仍在推理
的后台归因；队列满、超时、异常、重启或非法输出只会跳过本轮强化，不会阻塞回复或改变事实状态。

Recall Receipt 记录 `candidate -> selected -> injected -> used -> reinforced` 五个阶段及数值分数，
默认保留 30 天。问题、回复、记忆正文和 ref 列表不会写入日志；归因 Job 也不会落库。

### 记忆写入与纠正

```text
[Create / Correct / Invalidate / Restore]
                    |
                    v
          [Planner: mutation]
                    |
                    v
       [memory/write_state only]
                    |
                    v
            [memory_change]
                    |
          +---------+----------+
          |                    |
          v                    v
 [Unique Exact Target]   [Ambiguous / Not Found]
          |                    |
          v                    v
 [Transactional Write]   [0..3 Safe Candidates]
          |                    |
          v                    v
 [Mutation Receipt]      [Read Tool Fallback]
          |
          v
 [Backend Final Message]
```

- 没有 `fact_id` 时，Locator 可用稳定 key、旧内容和分类在当前合法目标内精确定位。
- 唯一精确命中才执行写入；歧义时最多返回 3 条合法候选，完全无结果则明确不执行。
- Locator 不调用 Embedding，不跨人、跨群或绕过 SELF，可疑的 quarantined 事实永不作为候选。
- 纠正创建新版本并让旧版本失效；撤回是 `invalidate`，不是物理删除，审计痕迹会保留。
- 后端根据真实 `Mutation Receipt` 生成最终结果。未调用工具、noop、contest、歧义或未找到时，
  模型不能声称操作成功。
- DeepSeek 请求不发送任何 `tool_choice` 字段；正确性由能力隔离、事务和完成门保证。

### 后台记忆循环

```text
[Chat Events]
      |
      +--------> [Live Extraction] -------> [Facts / Evidence]
      |
      +--------> [Self Reflection] -------> [Self Memories]
      |
      +--------> [Embedding Worker] ------> [Vector Index]
      |
      +--------> [Dream Worker] ----------> [Merge / Synthesize / Resolve]
      |
      +--------> [Maintenance] -----------> [Expiry / Cleanup / Receipts]
      |
      +--------> [Controlled Rebuild] ----> [Review / Commit]
```

Live Extraction 从事件账本提炼候选与 Evidence；Self Reflection 默认在每天 04:00、12:00、20:00
处理 Yuki 自身经历；Dream 默认在 05:00 对记忆簇执行 keep、merge、synthesize、recompose、
contest 或 resolve，并保留预览、来源与回滚信息。Maintenance 处理真实性生命周期、过期 Receipt
和索引维护。受控 Rebuild 默认关闭，用于从历史事件重建记忆并经过 review 后提交。

Memory V2 还提供版本化质量数据集、确定性 benchmark、跨人/跨群污染门、生产库只读审计与正式
release check。更多细节见 [Memory V2 架构](docs/architecture/memory-v2.md)、
[自适应生命周期实施计划](docs/architecture/Yuki_Adaptive_Memory_Lifecycle_Implementation_Plan.md)
和 [3.5.1 发布说明](docs/releases/v3.5.1.md)。

## Agent 与工具系统

Planner 与 Main Agent 职责分离：Planner 决定是否回复、记忆访问方式、能力 scope 与媒体效果；
Main Agent 负责回答和实际工具调用。工具内核再按 origin、scope、effect、risk、创建者身份、当前
群权限和轮次预算做最终治理。

```text
[Planner Plan]
      |
      v
[Capability Catalog]
      |
      v
[Policy + Permission + Budget]
      |
      v
[Selected Tools]
      |
      v
[Main Agent] <----> [Bounded Tool Loop]
                         |
        +----------------+----------------+
        |        |        |       |       |
       Core    Admin  Automation Plugin  MCP / Web
```

`request_tools` 允许 Agent 在已有授权范围内按需加载更多能力，但不会授予新权限。Plugin Host、
MCP Gateway、自动化 Scheduler 和后台通知最终都回到同一能力目录、Agent 与投递链路。

## 扩展能力

### Plugin API 1.1

插件可以注册命令、工具、事件处理器、Prompt 片段、Planner Signal、后台服务和受控 Agent
Session，也可以使用存储、网络、媒体、Memory Facade、Automation Facade 与 Notification
Outbox。插件运行前需通过 Manifest、版本、权限和管理员批准检查。

- [插件快速开始](docs/plugin-development/quickstart.md)
- [Plugin API 文档](docs/plugin-development/index.md)
- [GitHub Monitor](plugins/github-monitor/README.md)

### MCP

MCP Client 支持 stdio 和 Streamable HTTP，包含动态发现、元数据缓存、Schema Token 预算、并发
限制和大型结果 Artifact。示例包括麦当劳、网易云音乐和 Miniflux。

- [MCP 配置](docs/mcp/configuration.md)
- [Planner 与 Agent 路由](docs/mcp/planner-and-agent.md)
- [故障排查](docs/mcp/troubleshooting.md)

### 图片、表情与语音

- 图片理解：可选 Qwen Vision，只向视觉模型发送本轮选中的图片和当前问题。
- 表情系统：支持收集、分析、选择、生命周期管理、自动回复与 Plugin API。
- 本地语音：Genie-TTS Worker 使用独立 Compose profile、只读模型目录和 Unix Socket，运行时可
  完全断网。

## 快速开始

### 环境要求

- Docker Engine / Docker Desktop 与 Docker Compose
- 一个可登录 NapCatQQ 的 QQ 账号
- 一个 OpenAI-compatible 模型接口；推荐为 Planner 配置低延迟 Flash 模型
- 只有本地开发才需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和完整源码

### 1. 获取部署包

从 [Yuki 3.5.2 Release](https://github.com/YuanYeYouTao/Yuki-QQbot/releases/tag/v3.5.2)
下载 `yuki-3.5.2-deploy.zip` 或 `yuki-3.5.2-deploy.tar.gz` 并解压。正式部署不需要克隆源码，
也不会在用户机器上构建镜像。

### 2. 创建配置

Linux / macOS：

```bash
test -f .env || cp .env.example .env
```

Windows PowerShell：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

至少填写以下内容：

```dotenv
ONEBOT_ACCESS_TOKEN=一段长随机值
NAPCAT_WEBUI_TOKEN=另一段长随机值
SUPERUSERS=你的QQ号

LLM_PROVIDER=openai
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=你的模型密钥
LLM_MODEL=你的主模型名称
```

机器人 QQ 号由 NapCat WebUI 中实际登录的账号决定，不需要写入 `.env`。完整配置与默认值见
[`.env.example`](.env.example)。

### 3. 启动

```bash
docker compose up -d
docker compose ps
docker compose logs -f bot napcat
```

Bot 容器启动时会自动生成 NapCat OneBot 配置并执行 `alembic upgrade head`。打开
`http://127.0.0.1:6099` 登录 NapCat，确认反向 WebSocket 已连接后即可在 QQ 中测试。

升级前先备份 `data/`，再把 `.env` 中的 `YUKI_VERSION` 修改为目标版本：

```bash
docker compose pull
docker compose up -d
```

版本镜像不可变；`docker compose pull` 只拉取 `.env` 当前指定的版本。不要用新部署包直接覆盖
旧目录，保留现有 `config/`、`plugins/`、`data/` 和 `napcat-*`。完整步骤见
[3.5.2 升级说明](docs/releases/v3.5.2.md)。

停止全部服务：

```bash
docker compose down
```

本地源码开发使用独立覆盖文件：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

### 4. 可选 Pro / Flash 路由

不提供模型档案时，全部任务使用 `LLM_*` 主配置。若要把聊天交给 Pro、把 Planner 和后台结构化
任务交给 Flash：

```powershell
Copy-Item config/model_profiles.example.toml config/model_profiles.toml
```

然后在 `.env` 中设置：

```dotenv
MODEL_PROFILES_FILE=config/model_profiles.toml
LLM_FLASH_BASE_URL=https://api.deepseek.com
LLM_FLASH_API_KEY=你的Flash密钥
LLM_FLASH_MODEL=你的Flash模型名称
```

示例路由中，`chat_agent`、`automation_agent`、`plugin_agent_session` 使用 Pro；Planner、Memory
Extraction、Self Reflection、Consolidation、Attribution、Relationship、Emoji 和工具选择等结构化
任务使用 Flash。TOML 只引用环境变量名，不保存密钥。

## 配置与运行

常用配置组：

| 配置组 | 关键入口 |
| --- | --- |
| 身份与准入 | `SUPERUSERS`、`ENABLED_GROUPS`、`IGNORED_BOT_USERS` |
| 上下文 | `MAX_CONTEXT_CHARACTERS`、`CONTEXT_METADATA_BUDGET_RATIO`、历史水位 |
| Memory V2 | `MEMORY_*` 提炼、召回、Activation、归因、Dream、维护与质量设置 |
| Agent | `AGENT_MAX_TOOL_CALLS`、`AGENT_MAX_MODEL_REQUESTS`、工具结果预算 |
| MCP | `MCP_ENABLED`、`.mcp.json`、发现与结果 Artifact 设置 |
| Plugin | `PLUGIN_SYSTEM_ENABLED`、`PLUGIN_DIRECTORY`、审批与 Plugin API 设置 |
| Vision / Web | `VISION_*`、`WEB_MODE`、Tavily 与受控 fallback 设置 |
| Emoji / Speech | `EMOJI_*`、`SPEECH_*` 和 `speech` Compose profile |
| Automation | `AUTOMATION_ENABLED`、时区、调度和投递设置 |

常用诊断：

```bash
docker compose ps
docker compose logs --tail 200 bot
docker compose exec bot qq-ai-bot-cli model profiles
docker compose exec bot qq-ai-bot-cli model routes
docker compose exec bot qq-ai-bot-cli model stats
docker compose exec bot qq-ai-bot-cli memory audit \
  --database-url sqlite+aiosqlite:////app/data/qq_ai_bot.db
```

健康检查位于 Bot 容器内的 `http://127.0.0.1:8080/healthz`；Compose 通过该端点决定何时启动
NapCat。

## 数据、安全与升级

Yuki 使用 SQLite 保存事件、身份、关系、记忆、自动化、插件状态和运行配置。默认数据库为
`data/qq_ai_bot.db`，NapCat 登录数据位于 `napcat-data/`。

- 不要提交 `.env`、数据库、QQ 登录数据、语音模型或第三方密钥。
- `LOG_MESSAGE_CONTENT=false` 时常规日志不记录消息正文；质量报告和记忆指标采用无正文设计。
- `SUPERUSERS`、群准入、能力权限、插件审批和工具风险策略是不同层级，不应互相替代。
- 升级前先备份 `data/` 与当前镜像，再替换容器；Alembic 迁移由 Bot 启动脚本自动执行。
- 从 2.x 升级到 3.x 前必须阅读 [Memory V2 升级指南](docs/upgrade-memory-v2.md)。

## 项目结构

```text
Yuki-QQbot/
+-- src/qq_ai_bot/
|   +-- application/     # application wiring and runtime modules
|   +-- planner/         # turn planning and structured intent
|   +-- conversation/    # turn coordination and agent loop
|   +-- memory/          # Memory V2, retrieval, mutation and workers
|   +-- capabilities/    # unified capability catalog and policy
|   +-- plugins/         # built-in NoneBot entrypoints
|   +-- plugin_host/     # Plugin API host runtime
|   +-- automation/      # persistent schedules and execution
|   +-- mcp/             # MCP client and gateway
|   +-- web/             # controlled web access
|   +-- vision/          # optional image understanding
|   +-- emoji/           # persistent emoji system
|   +-- speech/          # speech orchestration
|   +-- persistence/     # SQLAlchemy models and repositories
|   +-- admin/           # runtime configuration and audit
|   +-- model_runtime/   # model profiles, routing and statistics
|   +-- prompting/       # prompt compilation and budgets
|   +-- services/        # shared application services
|   +-- llm/             # provider-specific model adapters
|   +-- domain/          # core domain contracts
|   +-- adapters/        # external transport adapters
|   +-- time/            # trusted time services
|   +-- references/      # controlled reference handling
|   +-- config.py        # environment configuration
|   +-- container.py     # dependency composition root
|   +-- main.py          # NoneBot / FastAPI entrypoint
|   +-- cli.py           # administration CLI
+-- src/yuki_plugin_sdk/ # standalone Plugin API 1.1 SDK
+-- migrations/          # Alembic migrations
+-- plugins/             # installed local plugins
+-- services/            # isolated auxiliary workers
+-- config/              # persona, model routes and contracts
+-- docs/                # architecture and operations guides
+-- tests/               # unit, integration and contract tests
+-- docker-compose.yml
+-- Dockerfile
```

## 开发与质量

```bash
uv sync --frozen --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

数据库与 Memory V2 发布门：

```bash
uv run alembic upgrade head
uv run qq-ai-bot-cli memory quality run --suite full
uv run qq-ai-bot-cli memory release-check
```

GitHub Actions 还会验证 Docker Compose、运行时镜像、隔离的 Genie-TTS Worker、Migration Matrix、
示例插件合同和冻结的 Memory Quality baseline。

## 文档

- [完整使用帮助](docs/help.md)
- [3.5.2 发布与升级说明](docs/releases/v3.5.2.md)
- [Memory V2 架构](docs/architecture/memory-v2.md)
- [记忆检索与混合 RAG](docs/architecture/memory-v2-retrieval.md)
- [记忆冲突](docs/architecture/memory-v2-conflicts.md)
- [记忆生命周期](docs/architecture/memory-v2-lifecycle.md)
- [记忆变更接口](docs/architecture/memory-change.md)
- [自适应记忆生命周期](docs/architecture/Yuki_Adaptive_Memory_Lifecycle_Implementation_Plan.md)
- [Memory 质量与运维](docs/operations/memory-quality.md)
- [版本化 Docker 发布](docs/operations/versioned-docker-release.md)
- [受控历史重建](docs/architecture/memory-v2-rebuild.md)
- [Tool Kernel](docs/architecture/tool-kernel.md)
- [Plugin 开发](docs/plugin-development/index.md)
- [MCP 文档](docs/mcp/architecture.md)
- [表情系统](docs/emoji-system/architecture.md)
- [语音系统](docs/speech/architecture.md)
- [版本记录](CHANGELOG.md)

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
