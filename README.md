<!-- release-baseline: version=3.8.4 schema=0072 -->

中文（默认） · [English](README.en.md)

<div align="center">

<p><img src="img/Yuki_2.png" alt="Yuki" width="280"></p>

<h1>Yuki</h1>

<p>一个在真实 QQ 对话中持续存在的社会化 AI Agent</p>

<p>
  <a href="https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4"><img src="https://img.shields.io/badge/Release-3.8.4-blue" alt="Yuki 3.8.4"></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <a href="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml"><img src="https://github.com/YuanYeYouTao/Yuki/actions/workflows/quality.yml/badge.svg" alt="Quality"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow" alt="MIT License"></a>
</p>

[下载正式版 3.8.4](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4) · [3.8.4 发布说明](docs/releases/v3.8.4.md) · [3.8.4 升级指南](docs/upgrade-3.8.4.md) · [使用帮助](docs/help.md)

</div>

Yuki 是一个开源、自托管的社会化 AI Agent，探索数字生命如何在真实社交场景中持续存在。她当前运行在 QQ 私聊和群聊中，记住人与共同经历，维护长期关系，也能使用工具和持久工作环境完成跨消息的任务。身份、记忆和关系由自己的数据库保存，更换模型、QQ 账号或网关时可以继续沿用。

**当前正式版为 3.8.4。** 本版将可见发言统一为主 Agent 显式 `send_message`，并加入可选的群聊语义观察、SELF 自主参与和自主自动化。Yuki 可决定发言、分条发送或沉默；自主参与默认关闭，真实 QQ 群聊中的长期效果仍在验证。工作环境需要单独部署；WebUI 尚未提供。

## Yuki 能做什么

| 能力 | 使用方式 |
| --- | --- |
| 长期聊天与记忆 | 在群聊、私聊中持续交流，查询旧事，明确要求记住、纠正或删除事实 |
| 图片、语音和附件 | 发送图片、语音、视频或文档；同一会话内可不引用原附件继续追问 |
| QQ 社交操作 | 查询成员、结构化 @、发送群消息或私聊、撤回自己的消息；目标与权限由后端校验 |
| 搜索与扩展 | 使用配置好的联网工具、MCP 服务和插件处理外部信息 |
| 持久工作环境 | 保存项目与文件，运行 Python、Node.js 或 Shell，安装依赖并交付结果 |
| 后台任务与自动化 | 启动作业后继续聊天，随后查询进度；按已授予的权限执行定时任务和续跑 |
| 语音与表情 | 可选 Genie-TTS 语音发送，以及表情包检索、分类和发送 |

这些能力取决于部署配置、模型能力和授权范围。任务被接纳、执行完成和消息发送分别有状态记录；调用工具不等于结果已经交付。

## 持久工作环境

启用后，Yuki 拥有全会话共用的 Linux 工作目录，可以保存下载文件、Git 项目、脚本与依赖。文件工具和终端操作同一份文件，普通工作文件不再按 24 小时过期。

- 预装 Bash、Python、Node.js、Git 和基础编译工具，支持 pip、npm，以及由 Manager 管理的 apt 安装和环境检查点。
- 终端支持交互输入、增量输出、取消和后台执行。可以先启动一个任务，继续处理消息，再回来看结果。
- Bot 或 Manager 重启时，环境进程可以继续运行；环境本身重启后，普通任务标记中断，已登记服务按策略恢复。
- 选定文件发布为不可变快照后，通过 QQ 发送链路交付；旧 `artifact_id` 保持兼容。

默认家目录容量 2 GiB，容器内存上限 512 MiB，最多一个主要执行任务、四个终端会话和两个内部服务。环境没有浏览器或桌面，也不挂载 Bot 数据库、QQ 凭据或宿主 Docker 控制接口。

这是一项**单独部署的可选能力**，需要 Linux 宿主、gVisor 和 Yuki Manager；普通 Bot 部署包不会自动安装。配置、资源限制与恢复方式见[持久环境说明](docs/operations/persistent-environment.zh-CN.md)（[English](docs/operations/persistent-environment.md)）。

## 记忆与跨会话连续性

长期记忆用于保存稳定事实、偏好和有意义的经历；普通自动提取会聚合消息，明确要求记住、纠正和删除时则即时处理。长会话通过 Rollup 压缩历史，原始历史仍有独立查询入口。

`short_state` 是全局共用、有界且会过期的短期记录区，用于暂存跨会话信息；它与长期记忆、持久文件分开。群聊和私聊不会自动拼成同一份完整聊天历史。

记忆读取仍有具体边界：历史共同群关系可以开放人物结构化事实，**其中可能包含私聊来源的事实**，但不开放他人的原始私聊或私有证据。部署前请阅读 [Memory 的范围与权限](docs/architecture/memory-v2.md)。模型提取和回忆可能出错，重要信息应核对来源。

## 图片、语音、文件与联网

- **图片与视频**：支持图片输入的模型可直接查看当前或引用图片；后续在同一会话不引用地追问时可按内部事件索引按需查看。MP4/MOV 视频通过 FFmpeg 抽帧进入同一个主 Agent，不分析音轨，也不保证覆盖所有瞬间。
- **接收语音**：私聊、符合回复策略的群聊及引用语音经 Qwen ASR 转写，进入聊天历史、搜索和 Rollup。可复用千问连接，与 Genie-TTS 语音发送独立配置。见[语音识别说明](docs/speech/recognition.md)。
- **文件阅读**：支持文本、代码、CSV/JSON、PDF 文字、DOCX 和 XLSX 的有界提取。扫描 PDF 不做 OCR，表格公式不重算，宏和附件中的代码不会因阅读而执行。在同一会话中，后续提问可不引用原附件；Yuki 按需读取 24 小时临时缓存。
- **联网**：可配置 Provider 原生搜索或外部 `web_search`。外部搜索默认使用 Tavily，也可设置 `WEB_MODE=tavily`、`WEB_SEARCH_BACKEND=deepseek_anthropic` 使用 [DeepSeek 搜索桥](docs/deepseek-search-bridge.md)；此时 Tavily 密钥仅用于可选失败兜底。`both` 允许已配置的外部搜索和 Provider 支持的原生搜索，`disabled` 禁止联网。DeepSeek 主 Agent 的原生搜索能力当前由适配层关闭，搜索桥使用独立协议请求。

聊天、插件唤醒、自动化和任务续跑使用统一主 Agent 与完整工具声明。工具结构在部署内保持固定，执行时再检查权限与预算；这减少请求前缀变化，但不保证 Provider 的缓存命中率。

## 配置与启动

基础部署需要：

- Linux amd64，或运行 Linux 容器的 Windows Docker Desktop；
- Docker Engine 和 Docker Compose v2；
- 可用的模型服务配置，支持项目接入的 Chat Completions 或 Responses 协议；
- 至少一个 NapCat 或 SnowLuma QQ 网关及登录账号。

从 [3.8.4 Release](https://github.com/YuanYeYouTao/Yuki/releases/tag/v3.8.4) 下载部署包，解压后可以手动填写 `.env` 和模型配置，也可以使用配置向导。

开发主线增加 Claude Messages、Gemini GenerateContent 和常见 Chat 供应商方言。
多个供应商可按任务显式配置；能力与恢复边界见[模型协议说明](docs/architecture/model-providers.md)
及[多供应商示例](config/model_profiles.providers.example.toml)。这不是既有 3.8.4 部署包的能力声明。

Linux：

```bash
curl -fLO https://github.com/YuanYeYouTao/Yuki/releases/download/v3.8.4/install.sh
chmod +x install.sh
./install.sh
```

Windows PowerShell：

```powershell
Invoke-WebRequest -Uri https://github.com/YuanYeYouTao/Yuki/releases/download/v3.8.4/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**向导只负责配置。** 在空目录中下载并校验部署包，在已有部署中保留 Compose、插件和数据；确认后备份并写入配置。它不会停服、迁移数据库、启动服务或切换网关。

首次部署在配置完成后，进入部署目录执行：

```bash
docker compose config --quiet
docker compose pull
docker compose run --rm --no-deps --entrypoint qq-ai-bot-cli bot init-db
docker compose up -d
```

还需完成 QQ 登录，并按所选插件和语音组件的说明进行初始化。首次部署和从旧版升级见 [3.8.4 升级指南](docs/upgrade-3.8.4.md)。已有部署应保留原项目名、Compose 覆盖文件和挂载配置。

正式镜像为 `ghcr.io/yuanyeyoutao/yuki-qqbot:3.8.4`；可选 TTS Worker 镜像为 `ghcr.io/yuanyeyoutao/yuki-genie-tts-worker:3.8.4`。发布包提供 `SHA256SUMS`。单独下载的环境模板附件名为 `default.env.example`，压缩包内仍为 `.env.example`。

## 升级与日常维护

当前源码使用 Plugin API **3.0**，数据库目标由随包 Alembic 单一 head 决定；应用版本号不能替代数据库版本检查。3.8.2 发布包的历史目标为 0055。较旧的数据库必须先满足迁移前提，不能通过 `stamp` 跳过迁移。旧插件的 `llm.generate` / `agent.run` 已统一到主入口，依赖旧独立生成语义的插件需要适配。

升级前保存一致的数据库、配置、插件及文件备份；持久环境还需保存家目录与运行回执。暂停写入只涉及 Bot 和相关 Manager，不需要关闭整个 Docker 或 QQ 网关。回退时应先保全升级后的新消息、文件和回执，详见 [3.8.4 升级指南](docs/upgrade-3.8.4.md)。

```bash
docker compose ps
docker compose logs --tail 200 bot
docker compose exec bot qq-ai-bot-cli gateway doctor --provider snowluma
```

使用 NapCat 时将最后一个参数改为 `napcat`；所有命令沿用部署时的 Compose 参数。同一 QQ 只允许一条活动连接，切换网关前需先停止旧连接。见 [SnowLuma 部署与切换](docs/deployment/snowluma.md)。

## 相邻项目

[Alice](https://github.com/LlmKira/Alice) 探索 AI 如何持续参与真实聊天；[Letta](https://docs.letta.com/) 关注有记忆、能保持状态的 Agent；[AstrBot](https://docs.astrbot.app/) 提供面向 QQ 等聊天平台的 Agent 与插件框架。Yuki 关注这些能力如何在同一个持续存在的群聊主体中协同工作：认识人、积累关系与记忆，并自主判断何时参与。

## 架构与开发

开发前阅读 [共同架构约束](docs/architecture/development-contract.md) 与
[架构文档索引](docs/architecture/README.md)。历史任务书不替代现行合同。

一个数据库对应一个长期存在的 Yuki。人物、群空间、QQ 账号和网关连接分别建模，聊天历史与关系不绑定在某一次登录连接上。工具由后端执行权限、预算、幂等和审计检查。

目前提供 QQ 交互、CLI 和供管理界面复用的 Control Plane 业务层，**尚未提供 Yuki 管理 WebUI 或管理 HTTP API**。

```bash
uv sync --extra dev
uv run ruff format --check
uv run ruff check
uv run mypy src
uv run pytest
```

开发时按改动范围选择定向验证；发布流程还会验证迁移、镜像和无源码部署。

| 文档 | 内容 |
| --- | --- |
| [使用帮助](docs/help.md) | 聊天、命令与日常操作 |
| [架构说明](docs/architecture/canonical-runtime.md) | 人物、空间、账号和会话的关系 |
| [开发约束](docs/architecture/development-contract.md) | 事件 ID、解耦边界、固定工具、续跑和事务原则 |
| [Rollup](docs/architecture/conversation-rollup.md) | 长会话的历史压缩 |
| [Memory](docs/architecture/memory-v2.md) | 记忆提取、检索和权限 |
| [Plugin API 3.0](docs/plugin-development/index.md) | 插件开发与能力边界 |
| [MCP](docs/mcp/architecture.md) | 外部工具接入 |
| [语音发送](docs/speech/operations.md) | Genie-TTS 部署与运维 |
| [版本化发布](docs/operations/versioned-docker-release.md) | 镜像、下载包与发布流程 |
| [CHANGELOG](CHANGELOG.md) | 历史变更 |

## License

[MIT](LICENSE)
