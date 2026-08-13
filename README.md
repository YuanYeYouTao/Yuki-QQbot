<div align="center">

<p>
  <img src="img/Yuki_2.png" alt="Yuki" width="280">
</p>

<h1>Yuki-QQbot</h1>

<p>
  面向个人部署的 QQ AI Agent
</p>

<p>
  <img src="https://img.shields.io/badge/Version-3.5.0-orange" alt="Version">
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python Version">
  <img src="https://img.shields.io/badge/NoneBot2-OneBot%20v11-green" alt="NoneBot2">
  <img src="https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-MIT-yellow" alt="MIT License">
  </a>
</p>

<p>
  <a href="#-主要功能">主要功能</a>
  ·
  <a href="#-快速开始">快速开始</a>
  ·
  <a href="#-文档">文档</a>
  ·
  <a href="#-开发">开发</a>
</p>

</div>

---

Yuki 是一个纯用 Codex vibe coding 开发、面向个人部署的 QQ AI Agent。它通过 NapCatQQ 接入 QQ，使用 Planner、Agent、长期记忆、工具系统和插件系统完成聊天、检索、自动化与外部服务调用。

> **当前版本：3.5.0 正式版**
>
> Memory Dream 会在 Self Reflection 之后整理既有长期记忆，按语义重组长 Episode、治理
> Evidence 与来源链，并在召回阶段通过 MMR 减少重复；所有整理均可审计、预览和回滚。

## ✨ 主要功能

- **自然对话**：支持私聊、群聊、多轮上下文和思考模型。
- **Planner + Agent**：先规划是否回复、调用哪些能力，再由同一个 Agent 完成工具调用与回答。
- **Memory V2**：按人物、群、群内身份和 Yuki 自身保存长期事实；Yuki 可通过统一变更回执自主
  创建、纠正、争议、合并或恢复记忆，并记录证据、来源、有效期和版本链。
- **混合 RAG**：在人物与群硬隔离后，结合 SQLite FTS 与可选 Qwen Embedding 检索相关记忆。
- **关系系统**：为每个 QQ 保存独立的好感度、信任度和关系阶段。
- **自动化任务**：用户可以通过自然语言创建持久化提醒和周期任务。
- **统一工具内核**：Core、Admin、Automation、Plugin 与 MCP 工具统一交给 Planner 和 Agent 调用。
- **MCP Client**：支持 stdio 与 Streamable HTTP，可接入麦当劳、网易云音乐等 MCP Server。
- **插件系统**：提供 Plugin API 1.1、独立 SDK、权限、事件、Prompt、Planner Signal、静态直达绑定、后台服务与持久通知扩展点。
- **GitHub 仓库管家**：可选 GitHub Monitor 支持多仓库、多 QQ 目标、事件过滤、中文 Push/Release 卡片、去重推送和 Yuki 自然点评。
- **多模态扩展**：可选图片理解、表情系统、DeepSeek 原生或 Tavily 联网搜索和本地 Genie-TTS 语音回复。
- **运行时管理**：支持管理员自然语言配置、权限审计、健康检查和数据库迁移。

---

## 🧰 技术栈

- Python 3.12
- NoneBot2
- OneBot v11 / NapCatQQ
- SQLite / SQLAlchemy / Alembic
- Pydantic
- OpenAI-compatible Chat Completions / Responses API，建议使用 DeepSeek
- MCP Python SDK
- Docker Compose
- 可选 DeepSeek 原生联网、Tavily、Qwen Vision、Qwen Embedding 与 Genie-TTS

---

## 🏗️ 架构概览

主路径从 QQ 入站开始，经 Planner、上下文、主 Agent 和统一工具内核完成回复；SQLite 账本、
后台 Worker、自动化与插件形成持久回流。模型只能通过授权后的工具和服务访问外部能力，不能
直接读写数据库或调用 OneBot。

<p align="center">
  <img src="img/yuki-architecture-overview.png" alt="Yuki-QQbot 端到端架构总览" width="100%">
</p>

### 1. QQ 入站与消息路由

NapCatQQ 通过 OneBot v11 把事件交给 NoneBot2。标准化层保留正文、回复、@、媒体、QQ、昵称
与群名片，准入层负责群/私聊策略、去重、限流和可信权限，再写入事件账本并进入消息路由。

<p align="center">
  <img src="img/yuki-architecture-inbound-routing.png" alt="QQ 入站、标准化、准入与消息路由" width="100%">
</p>

### 2. Planner 与确定性入口

管理命令和绑定插件命令走确定性入口；普通聊天先处理可选视觉输入，再由 Planner Context、
回复必要性判断和 Planner 决定回复、等待、静默、工具范围、记忆深度以及媒体效果。

<p align="center">
  <img src="img/yuki-architecture-planner-flow.png" alt="Planner 决策与确定性命令流程" width="100%">
</p>

### 3. 上下文与 Memory V2

短期历史使用 `chat_events` 中的发送者身份快照；人物、群、关系和场景与长期记忆共同进入
Context Assembler。Memory V2 先按 `person`、`person_group`、`group`、`self` 硬过滤，再执行
FTS、可选 Embedding 与 RRF 融合，所有更改统一经过 Memory Mutation Service。

<p align="center">
  <img src="img/yuki-architecture-context-memory.png" alt="上下文装配、Memory V2 检索与统一变更" width="100%">
</p>

### 4. 主 Agent 与统一工具内核

Prompt Compiler 把稳定人格、历史、动态上下文和当前消息交给 Model Runtime。AgentRunner
执行有界多轮工具调用；Tool Kernel 依据 Origin、创建者、当前群、权限和预算统一治理 Core、
Admin、Automation、Plugin、MCP 与 Web 能力。

<p align="center">
  <img src="img/yuki-architecture-agent-tools.png" alt="主 Agent、Model Runtime 与统一 Tool Kernel" width="100%">
</p>

### 5. 输出、媒体效果与投递回执

最终文本先经过输出清理器和 Reply Sequence；Planner 的表情与 Genie-TTS 效果在同一回复序列
合并。只有 OneBot Sender 获得 NapCat 真实回执后，投递结果才会写入事件账本和审计链。

<p align="center">
  <img src="img/yuki-architecture-output-delivery.png" alt="输出清理、媒体效果、OneBot 投递与回执" width="100%">
</p>

### 6. 后台循环、自动化与插件

Memory Worker 和 Relationship Worker 消费事件账本；Scheduler 恢复带真实创建者与权限的
Scheduled Turn；Plugin Host 负责生命周期、后台 Worker、外部事件、Planner Signal 和持久
Notification Outbox，它们都回到同一 Planner、Agent 与回复链路。

<p align="center">
  <img src="img/yuki-architecture-background-extensions.png" alt="记忆关系 Worker、自动化调度与插件后台扩展" width="100%">
</p>

---

## 🚀 快速开始

### 1. 准备配置

```bash
cp .env.example .env
```

至少填写：

- `ONEBOT_ACCESS_TOKEN`
- `NAPCAT_WEBUI_TOKEN`
- `SUPERUSERS`
- 主模型的 API 地址、密钥和模型名称

### 2. 启动服务

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot napcat
```

完成 NapCat 登录并看到反向 WebSocket 已连接后，即可在 QQ 中测试。

日常启动：

```bash
docker compose up -d
```

停止服务：

```bash
docker compose down
```

---

## 🧩 可选能力

以下功能默认可以关闭，不影响基础聊天：

- MCP Server
- Qwen 图片理解
- Qwen Memory Embedding
- Tavily 联网搜索
- 插件系统
- 表情收集与自动回复
- Genie-TTS 本地语音

配置示例见 [`.env.example`](.env.example)。

---

## 🗃️ 数据与升级

Yuki 使用 SQLite 保存事件、人物、关系、记忆、自动化、插件和运行配置。

> [!IMPORTANT]
> 从 2.x 升级到 3.x 前必须完整备份 `data/`。Memory V2 的首次迁移会删除旧记忆表，但保留聊天事件账本和其他核心数据。

详细步骤见 [Memory V2 升级指南](docs/upgrade-memory-v2.md)。

---

## 📚 文档

- [Memory V2 架构](docs/architecture/memory-v2.md)
- [记忆检索与混合 RAG](docs/architecture/memory-v2-retrieval.md)
- [受控历史重建](docs/architecture/memory-v2-rebuild.md)
- [插件开发](docs/plugin-development/)
- [GitHub Monitor 使用说明](plugins/github-monitor/README.md)
- [Memory 质量与运维](docs/operations/memory-quality.md)
- [完整使用帮助](docs/help.md)
- [版本记录](CHANGELOG.md)
- [完整文档目录](docs/)

---

## 🛠️ 开发

安装依赖并运行检查：

```bash
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

执行数据库迁移：

```bash
uv run alembic upgrade head
```

---

## 📄 开源协议

本项目基于 [MIT License](LICENSE) 开源。
