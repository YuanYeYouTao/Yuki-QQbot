# 更新日志

## Unreleased

## 3.7.0 - 2026-08-20

### Conversation Scope / Rollup

- 群聊短期会话统一为 Bot + 群；单检查点 Rollup 与 Alembic `0042`。详见
  [3.7.0 发布说明](docs/releases/v3.7.0.md)。
- Rollup 源投影按默认时区（`Asia/Shanghai`）渲染库存 UTC，避免模型把 11:21Z 读成上午。
- 模型出口剥掉行首 `>` 引用前缀，避免 QQ 打出引用块标记；Prompt 历史不再剥这层前缀，以免打断 DeepSeek 公共前缀。
- 插件后台轮次声明与群聊相同的首轮 `tools[]`，真实调用仍关闭，避免 0/3 工具把缓存前缀切断。

## 3.6.2

### Conversation History

- 近窗左沿只认落库 `coverage_end`，超预算时同步 extractive，不再按高低水位从尾巴滑动重切。
  合同见 [conversation-rollup.md](docs/architecture/conversation-rollup.md)；实施见
  [3.6.2 任务书](docs/architecture/Yuki-3.6.2-Frozen-History-Tail-Taskbook.md)。
- 热尾改为条数帽与渲染字符帽的交集；短消息不再把整段未覆盖原文护住。
- 有覆盖后仍可继续切 extractive。`raw_history_window_shifted` 仅在本 turn `coverage_end` 前进时为真。
- 默认热尾 `32` 条 / `1600` 渲染字符，近窗预算比 `0.40`；同一 assemble 最多同步 extractive `3` 刀。
  这些键可运营，不写死在运行时分类器里。
- 无需新 Alembic。升级见 [3.6.2 升级指南](docs/upgrade-3.6.2.md)。不在本版本改 `__init__.py` 版本号。

## 3.6.1 - 2026-08-19

### Conversation History Rollup

- 长会话在有 extractive / active 覆盖后缩短原文近窗，把较早历史编译成第二条 SESSION system。
  `chat_events` 仍是唯一原文；摘要不进入 Memory V2。
- 切窗当下只写零 LLM 的 extractive；后台 `ModelTask.CONVERSATION_COMPACTION`（默认 Flash）把同一
  `source_fingerprint` 升级为结构化摘要。没有覆盖时禁止切窗。
- `PromptCompiler.compile` 为三路：STATIC → SESSION → history → current+TURN。SESSION 先占
  历史字符余额，再选近窗。static 前缀不含 rollup。
- 新增 `get_chat_history_around`；缩短近窗后可按 `#event_id` 回读被覆盖原文。
- Alembic `0041` 增加四张 `conversation_history_*` 表，不改写原始事件与 Memory V2。
- 运维命令：`qq-ai-bot-cli history-rollup status|inspect|rebuild|invalidate|reconcile`。
  默认脱敏，不含正文。
- L0 成员按 `event_id` 排序。时间乱序事件不再让聊天轮因 `FrontierInvariantError` 失败；
  覆盖失败时退回原文近窗。

### 升级与观测

- 从 3.6.0 升级见 [3.6.1 升级指南](docs/upgrade-3.6.1.md) 与
  [发布说明](docs/releases/v3.6.1.md)。
- 本地测量见 [3.6.1 History Rollup 性能报告](docs/performance/3.6.1-history-rollup-report.md)。
  真实 Provider 的 `prompt_tokens - cached_tokens` 与 Flash 事实召回尚未测量，不外推。

## 3.6.0 - 2026-08-18

### Conversation / Memory / Capability Runtime

- 删除强制 Planner 与 Flash Tool Selection。普通文本进入 Main Agent 前不再有生成式路由请求。
- 私聊、@、回复机器人直接准入，不再计 necessity 分；群聊自主参与只做本地评分，默认只读。
- Plugin API 2.0 生效。`planner.signal.register` 批准在 `0038` 被撤销，须按
  `admission.signal.register` 重新批准。
- `model_profiles` schema v3 与 `qq-ai-bot-cli setup migrate-3-6` 成为 3.5.3 升级的前置步骤。
- Alembic `0040` 删除 `planner_runs` 并改写剩余 Planner 覆盖；downgrade 显式拒绝。
- 安装器在迁移前快照 SQLite 与 WAL/SHM；备份或校验失败不得启动 migrate-3-6。

### 升级与观测

- 从 3.5.3 升级见 [3.6.0 升级指南](docs/upgrade-3.6.0.md) 与
  [发布说明](docs/releases/v3.6.0.md)。
- 本地 Runtime 测量见 [3.6.0 性能报告](docs/performance/3.6.0-runtime-report.md)。
  端到端成对回放门槛尚未认证。

## 3.5.3 - 2026-08-16

### Guided Deployment v1

- 新增彩色交互式 `qq-ai-bot-cli setup`，在构造完整 Settings 前即可从空部署目录配置管理员、
  主模型及 Flash、Embedding、Web、Vision、MCP、Plugin、Automation、Speech 八类可选能力。
- 向导按选择条件提问，隐藏所有密钥输入，以脱敏摘要确认后原子写入；重跑时支持区块级修改、
  未知 `.env` 项保留、配置备份轮换和取消零写入。
- 模型档案由向导显式生成：Flash 关闭时全部任务使用主模型，开启时结构化后台任务使用 Flash；
  DeepSeek Responses 保留原生 Web 能力且请求继续完全省略 `tool_choice`。
- MCP 引导仅接受 Streamable HTTP，并把敏感 Header 移入 `.env`；插件只扫描 Manifest，逐项批准
  后在数据库迁移完成后应用，不导入插件代码，也不自动启用任何插件。
- 新增 Linux 与 Windows 安装器，校验精确版本部署包后用一次性无 Docker Socket 容器运行向导，
  再固定执行 Compose 校验、拉取、启动、插件应用和健康检查。
- Release 资产新增 `install.sh`、`install.ps1` 和 `SHA256SUMS`，生产 Compose 增加只读
  `.mcp.json` 挂载；正式平台仍为 `linux/amd64`，Alembic head 保持 `0036`，Plugin API 保持 `1.1`。

## 3.5.2 - 2026-08-15

### Versioned Docker Release

- 正式发布 `linux/amd64` Bot 与 Genie-TTS Worker GHCR 镜像；两个镜像共享 Yuki `3.5.2`
  标签和 OCI release metadata，Worker 内部组件版本保持 `1.9.0`。
- 根 Compose 改用固定版本镜像并移除生产构建；新增开发 Compose 覆盖，源码开发仍可构建本地
  `dev` 镜像。
- Release workflow 校验 Tag、包版本、运行时版本、锁文件与 Memory Release Check，复用完整质量
  门禁，并在推送不可变镜像前执行无源码 Smoke 与持久化检查。
- GitHub Release 附带白名单生成的部署压缩包、生产 Compose、环境模板和升级说明；普通用户无需
  安装 Python、uv 或克隆源码即可部署。

## 3.5.1 - 2026-08-15

### 自适应记忆生命周期

- Planner 在单次规划中输出结构化记忆访问方式、目的、主体、实体、时间和类型提示；后端保持
  人物、群和 SELF 可见性硬边界，并在 FTS/Semantic、RRF 之后按意图和 Activation 重排。
- 新增按记忆类型指数衰减的 Activation、Recall Receipt、发送后使用归因和 CAS 强化；归因由
  可抢占的后台 Flash Worker 完成，不再要求主 Agent 自报，也不阻塞正文发送。
- Alembic `0035` 增加 Activation 与 Recall Receipt，`0036` 将旧归因配置键迁移到异步归因开关；
  Plugin Memory Facade 和管理搜索继续保持纯读取与既有合同。

### 召回与写入隔离

- 自动召回、显式记忆工具读取和记忆修改形成互斥路径；自动召回按 purpose 使用全局 3/4/6/8
  条上限，并保留每目标 4 条上限，减少上下文和重复工具调用。
- 记忆创建、纠正、撤回和恢复只开放 `memory/write_state` 能力；最终回复由真实 mutation receipt
  完成门约束，未提交、歧义或未找到时不能声称成功。
- DeepSeek Chat Completions 与 Responses 请求均省略不受支持的 `tool_choice`；mutation 写能力
  不受通用候选工具裁剪，用户可见标签只能返回有界候选，不能触发模糊写入。

### 验证

- Ruff、mypy、1111 项 pytest 与 Memory Quality 18/18 cases、38/38 gates 通过；唯一跳过项为
  需要显式凭据的真实 Qwen Embedding 在线测试。
- 生产小批测试完成创建、自动读取、显式工具读取、纠正、撤回和重复撤回；事实版本链、mutation
  receipt、数据库完整性、Bot 健康状态和 OneBot 连接均通过核验。

## 3.4.5 - 2026-08-08

### Memory V2 写入质量与自主反思

- 自动记忆增加确定性的主体归属与长期价值策略；不确定事实先进入 7 天隔离候选区，普通人物
  候选需要两条独立可信证据才能晋升，Yuki 自我候选只交给独立 Self Reflection 处理。
- 新增每天 `04:00、12:00、20:00` 的 Yuki 自省 Worker，通过真实聊天事件、已投递回复和可信
  工具回执自主新增、纠正、合并、争议或失效动态 SELF 记忆，并保护身份、核心、安全和权限键。
- 事实增加验证版本、最近审计时间与 `legacy_unreviewed / verified / quarantined` 状态；提供分离的
  用户记忆与 SELF 记忆内部 dry-run 审计服务，不执行全库扫描。

### Planner 工具路由与成本

- Planner scope 描述改为具体能力摘要；明确工具任务选择最小 scope，并用规范化 intent 帮助本地
  工具匹配。Core 工具补充中文同义搜索标签和首轮命中指标。
- 首批 inherited 工具仍限制为 6 个，`request_tools` 保留为真实后端权限内的漏召回保险。
- 主 Agent 默认历史预算收紧为 12000 字符，每轮最多 8 次工具调用、6 次模型请求，单个工具结果
  最多 8000 字符；重复无进展工具批次会提前停止并进入最终回答。

### 主 Agent 会话身份

- 主 Agent 历史使用稳定的 `EventRecord.id` 作为消息引用，同一群名片与 QQ 的连续发言合并为
  一个身份信封；正文保持原样，Planner、自省和审计继续使用各自既有投影。
- 输出清理器同步移除模型误复述的新身份信封、事件号以及 reply/mention 元数据，避免内部上下文
  标记泄漏到 QQ 消息，同时保留普通句子中出现的相似文本。

### 兼容性

- Alembic `0030` 非破坏性增加记忆质量、候选、自省状态与有界工具回执结构。
- Plugin API 保持 `1.1`。本版本已完成正式发布验证，升级前仍建议备份 `data/`。

## 3.4.4 - 2026-08-06

### Prompt 缓存稳定性

- 主 Prompt 将历史消息放在动态运行时上下文之前，并把当前触发消息作为独立末尾输入，减少
  动态字段变化造成的前缀缓存失效。
- 历史窗口采用可配置的高低水位分块滚动，在达到上限前保持相同起点，避免每轮删除最旧消息
  导致历史前缀持续变化。
- 首批 Core/MCP 工具和 Schema 使用宽松默认预算，遗漏能力仍可由 Agent 按需加载；最终工具
  定义、Flash 候选目录与请求结果按规范名称稳定排序。
- 默认推理深度由 `max` 调整为 `high`，保留显式切换到 `max` 的能力。

### 输出清理

- 输出清理器兼容省略 `消息` 或 `时间` 字段的发送者身份头，避免模型复述的
  `[发送者:...|QQ:...]` 内部元数据泄漏到 QQ 回复。
- 新增缩短身份头和正文同行场景的回归测试。

## 3.4.3 - 2026-08-06

### 会话事件身份快照

- `chat_events` 直接保存消息到达时的发送者昵称与当前群名片，不再在 Prompt 装配阶段从人物
  目录反查历史发言人。
- 每条历史消息向 Agent 自包含投影发送者显示名、QQ、消息 ID 和回复目标，普通聊天、
  插件后台会话、兼容历史视图与历史搜索共用事件内的身份事实。
- 删除独立的相关人物上下文及其数量配置；明确提及和回复对象的记忆检索改用独立的
  `memory.max_referenced_targets` 限制。
- Alembic `0029` 以空快照兼容已有事件，新写入事件开始保留 OneBot 提供的原始身份字段。

### Prompt 与 Planner 可靠性

- Prompt 不再携带逐条时间字段，降低短期上下文 Token 开销；统一输出清理器会剥离模型复述的
  新旧内部身份头，已落库的污染文本也不会继续进入后续历史。
- Planner 的 `messages` 只包含当前消息之前的历史，当前触发消息只保留在
  `current_message`，修复将单次发言误判为重复发送的问题。
- 生产数据库发布门禁同步要求 Alembic `0029`，与当前迁移头保持一致。
- README 架构概览改为端到端 Mermaid 流程图，覆盖 OneBot 入站、Planner、Memory V2、统一
  工具内核、自动化、插件后台、联网、多模态、输出效果与持久回执。

## 3.4.2 - 2026-08-05

### GitHub Release 卡片

- GitHub Monitor 为 `ReleaseEvent` 增加与 Push 相同视觉体系的 1200×630 暗色 PNG 卡片。
- 卡片展示仓库、发布者、版本标签、正式版/预发布/草稿状态、目标分支、附件数量与有界发布说明。
- 事件归一化补充 Release 卡片所需的安全字段，并通过统一卡片分发入口复用现有媒体 Outbox。
- 新增长中文、长说明、事件归一化与通知媒体投递测试；插件版本提升至 `1.1.0`。
- 本版本不新增数据库迁移，Alembic head 仍为 `0028`。

## 3.4.1 - 2026-08-05

### GitHub Monitor 与 Plugin API 1.1

- 新增多仓库 GitHub Monitor，可向多个 QQ 群或私聊发送中文事件、Push PNG 卡片和可选
  Yuki 自然点评；支持首次基线、事件过滤、稳定去重、条件请求与 Rate Limit 退避。
- Plugin API 扩展为 `1.1`，新增后台服务、媒体制品、持久通知、目标授权、受控 Agent 点评和
  Host HTTP Secret credential 注入。
- Alembic `0028` 非破坏性增加外部事件字段、通知 Outbox、媒体制品和后台点评任务；原有
  聊天、记忆、关系、自动化和插件数据保持不变。
- 插件配置序列化支持 Pydantic Model、`set` 与 `frozenset`，修复添加仓库时的 `TypeError`；
  插件启动失败也不再错误回复“已启用”。
- 重新设计 GitHub Push 卡片，并在 Bot 镜像安装轻量中文字体，修复 Linux 容器中文方框乱码。
- 版本提升至 `3.4.1`，完整说明见 `plugins/github-monitor/README.md` 和
  `docs/releases/v3.4.1.md`。

## 3.2.0 - 2026-08-03

### Yuki 自我长期记忆

- Memory V2 新增 `self` 作用域和 `current_self` 检索目标，用于保存 Yuki 自己形成的事实、
  偏好、经历、反思和原则；静态核心人格与系统规则仍保持更高优先级。
- Planner 新增默认关闭的 `self_recall`，只有问题明确涉及 Yuki 自己的过去、偏好、观点变化或
  自我认识时，才构造 SELF 目标并复用现有 FTS、Embedding、混合 RRF 和上下文预算。
- SELF 事实按 `global`、`private`、`group` 可见范围在候选查询前硬过滤，避免跨私聊或跨群泄露；
  Alembic `0027` 非破坏性增加可见范围字段、约束和唯一 active slot 索引。
- 统一 `memory_change` 支持 Yuki 自主创建、纠正、撤销、恢复、争议和合并自我记忆，要求当前
  真实入站消息证据，并拒绝 identity、core、safety、system、permission 和 runtime 保护键。
- 新增只读 `get_self_memories`，支持相关检索和总览，只返回当前会话可见的 SELF 事实投影，
  不暴露可见用户/群 ID、原始证据身份或审计内部信息。

### 工具链可靠性

- 允许绑定当前真实群消息的自主回应调用 `memory_change`，定时任务、系统任务和无真实事件的
  后台流程仍不可调用。
- 自动化意图检测改为按句判断，避免把一处“今天”和另一句“告诉我”拼成不存在的定时任务，
  从而错误覆盖 Planner 的 memory 工具域。
- SELF category 白名单成为统一领域契约，并同时暴露在工具 Schema 和拒绝回执中，使 Yuki 能
  使用 `self_fact`、`self_preference`、`self_episode`、`self_reflection`、`self_principle`
  正确提交或自行纠错。

### 发布

- 版本提升至 `3.2.0`；Plugin API 仍为 `1.0`，Alembic head 为 `0027`。

## 3.1.0 - 2026-08-03

### Memory Mutation V2

- 新增主 Agent 唯一记忆写工具 `memory_change`，支持 create、correct、invalidate、restore、
  contest、merge、reassign 和 update_metadata，并以实际 applied operation/outcome 驱动回复。
- 新增统一 `MemoryMutationService` 与 Alembic `0025` mutation receipt；Agent 与 Worker 通过
  request/claim 双指纹去重，事实、证据、状态和回执原子提交。
- 新增 Alembic `0026` 可恢复反思任务；有界扫描重复、争议和 `person_group` 归属异常，支持
  持久领取、指数退避、进程中断恢复，并只经统一变更服务执行合并或争议标记。
- 生产 Memory Worker、确定性记忆/偏好命令、管理员 Action、Plugin Memory Facade 和有界
  lifecycle reflection 接入统一变更边界；普通成员可影响本人、当前群及群内人物记忆，同时
  保留 `third_party` 来源。
- 群友读取以当前群 `person_group` 为基础，并只读投影由目标本人在当前群 evidence 支持的
  `person`；不暴露 evidence、其他群事实或变更权限。

### Host 管理的插件直达绑定

- 新增启动期静态 `PLUGIN_DIRECT_COMMAND_BINDINGS`，把受审阅前缀绑定到已批准、已启用、运行中的 `USER` 插件命令；拒绝空白、控制字符、斜杠、AI 前缀和任意互相重叠的前缀。
- 直达匹配只新增明确触发信号；群/私聊准入、持久消息去重、入站账本、命令限流、图片写隔离、插件权限、真实调用上下文和超时保持不变。
- configured-but-inactive 绑定失败关闭且不进入 Planner，插件 doctor 输出 active/inactive 原因。
- 确定性插件直达命令不再进入自动 Memory Worker，避免把 `*签到` 等游戏语法误抽取为长期记忆。

### Plugin API 与养鲲游戏

- `CurrentMessage` 新增默认空元组的可信 `mentioned_user_ids`，按真实消息顺序去重并剔除机器人自身；Feature Registry 新增 `message.current.mentions.v1`。
- 内置 `io.github.yuanyeyoutao.kun-game`，完整覆盖养成、PVP、BOSS、拍卖、小游戏和 SUPERUSER 管理动作；`*` 只绑定普通 `play`，管理动作仅保留长入口。
- 游戏规则为无 I/O 的确定性引擎，使用上海消息时间与局部 RNG；状态按群/私聊隔离，以每作用域锁和完整状态 CAS 一次提交。
- 修复负数和非有限数、重复处罚、属性重复显示、数星星答案、BOSS 排行、拍卖买方覆盖和文本 QQ 目标问题；不提供旧 AstrBot JSON 运行时迁移。
- 版本提升至 `3.1.0`；Plugin API 仍为 `1.0`，养鲲功能本身不新增迁移；当前开发分支的
  Alembic head 为 Memory Mutation V2 的 `0026`。

## 3.0.3 - 2026-08-02

### 日常表情节奏

- 新增可热修改的 `emoji.spontaneous_frequency`，默认 `0.15`；Yuki 可通过现有管理员配置工具按自然语言修改全局、群或用户作用域的日常表情频率。
- Planner 复用本轮已有的近期账本读取统计真实表情投递比例，连续分段消息按一次回复计算；超过目标频率时后端禁止日常 `optional`，明确索要表情的确定性发送路径保持不变。
- 不新增模型调用、数据库查询或迁移；现有同表情冷却、作用域冷却和候选选择规则继续生效。

### 设计文档与项目资源

- 新增 `memory_change` 自主记忆更改接口讨论稿，覆盖普通用户委托、Yuki 自主纠错、版本化变更、权限范围和跨入口幂等设计；本版本只提供设计文档，不改变运行时记忆行为。
- 新增 `img/Yuki_avatar.png` 项目头像资源。
- 版本提升至 `3.0.3`；不新增 Alembic 迁移，不清理或改写既有数据，head 仍为 `0024`。

## 3.0.2 - 2026-08-02

### Planner 响应瘦身

- 新增模型专用的稀疏 Planner 输出边界：发送方式、记忆深度、表情和语音保留明确决策，
  `intent`、目标人物、引用消息、等待时间、效果细节等次要字段省略时由后端补充默认值。
- 稀疏输出会转换回严格的 `TurnPlan`，不会将可空字段扩散到 Agent、工具内核或回复发送层；
  工具选择省略时保持原有 `inherit` 行为，避免静默关闭联网、记忆和 MCP 能力。
- 使用当前 DeepSeek Flash 配置进行真实调用验证，代表性 Planner 输出由旧版平均约 403 tokens
  降至约 222–227 tokens；联网意图仍能保留对应工具 scope。

### README 首页

- 更新 README 首页排版、功能概览、技术栈、快速开始和文档导航，并补充 MIT License。
- 在项目大标题上方居中展示 Yuki 形象图。

### 群聊表情可靠性

- 修复群聊表情作用域查询复用同一 SQLAlchemy 实体导致的 auto-correlation 异常；外层启用
  作用域与群禁用覆盖现在使用独立 alias，群 A 的禁用不会影响群 B 或私聊。
- 表情准备改为 `ready / no_candidate / repository_unavailable / asset_missing /
  storage_missing / unexpected_failure` 强类型结果；可选表情故障不再终止正常文字回复。
- 明确索要表情的独立消息由 PlannerService 生成确定性 `emoji_only` 计划，不调用 Planner LLM、
  Chat Agent 或工具；Planner 超时降级不再继承工具范围，最多只做一次无工具正文请求。

### 真实发送回执与故障恢复

- `OutboundSender` 统一返回带真实平台消息 ID 的 `OutboundSendReceipt`；OneBot 缺失或无法识别
  消息 ID 时视为发送失败，删除已确认发送链路中的本地假 UUID。
- 回复序列可只恢复失败的媒体效果：optional 静默跳过，preferred 与 emoji-only 使用确定性短文字，
  不重试原图、不换图，也不重新调用 Planner、Agent 或视觉模型。
- 平台已接收后，即使账本或表情使用记录写入失败也不会重发；发送、账本与使用记录分别记录状态。
- 新增可信 `recent_delivery`，只向下一轮投影当前精确会话最近三条已确认消息的 ID、时间、文本
  存在性和媒体种类，不包含表情 ID、图片描述或媒体字节。

### 后台任务与验证

- 自主群聊 detached task 增加 owner/done callback，统一消费异常、清理 task 引用并统计失败，
  消除 `Task exception was never retrieved`。
- 新增真实 SQLite 群作用域、表情准备、OneBot 回执、回复恢复、Planner 快路径、近期投递与
  自主任务回归测试。
- 版本提升至 `3.0.2`；不新增 Alembic 迁移，不清理或改写既有数据，head 仍为 `0024`。

## 3.0.1 - 2026-08-01

### DeepSeek V4 Flash Max

- 主聊天模型改用 DeepSeek 官方滚动别名 `deepseek-v4-flash`，保持 Planner 等结构化辅助任务的
  独立 Flash 非思考路由不变。
- 新增 `LLM_REASONING_EFFORT=high|max`，在思考模式开启时向兼容接口发送
  `reasoning_effort`；当前本地部署使用 `max`。

### Planner 按需记忆检索

- 新增 `none / lexical / hybrid / overview` 四级记忆上下文计划：纯效果回复不装配聊天上下文，
  简短日常交流优先使用本地词法检索，只有人物事实、偏好、模糊指代和历史语义问题才调用
  Embedding，显式记忆概览继续走不生成查询向量的概览模式。
- Planner 只选择检索深度，人物、QQ群和会话范围仍由后端根据真实事件确定；语义检索未配置或
  临时失败时自动降级为词法检索，不影响正常回复。
- 增加有界的进程内 query embedding 缓存，默认保留 10 分钟、最多 512 项；缓存键使用查询与
  profile 的哈希，不持久化原始查询，并合并并发的相同请求。
- Planner 日志与事件增加记忆模式和原因码，补充纯表情零上下文、分级检索、语义降级和缓存命中
  回归测试。

### 回复节奏调整

- 日常分段消息之间采用 1–2 秒随机间隔，避免连续多条消息瞬间刷出，同时不恢复旧版 3–5 秒的
  较长停顿；该等待仅发生在同一回复序列的第二条及后续消息之前。
- 保留新消息取消旧回复、网络超时、失败重试和限流；这些属于正确性与稳定性机制，不是拟人化发送延迟。

### Planner 执行边界修复

- 表情回复改为由 `TurnPlan.emoji` 独占规划和发送；`emoji_only` 会关闭本轮工具范围并直接进入
  回复效果发送层，不再额外调用 Chat Agent，也不再把 `send_emoji` 暴露成一套重复决策工具。
- 回复完成条件由“正文不能为空”改为“至少存在一个可见输出”；Planner 已安排的表情可以独立
  完成本轮，普通文字轮与需要正文合成的语音轮仍保留空响应重试和安全降级。
- `request_tools` 继续支持 Agent 按需加载被 Schema 预算省略的工具，但候选目录严格限制在
  Planner 本轮已批准的 scope 内，不能从用户的完整理论权限目录跨 scope 扩张。
- 增加 Planner 表情可用性、独占效果规范化和动态工具越界回归测试；自动化与插件的独立表情
  发送接口保持不变。

### 表情选择延迟优化

- 日常可选表情直接采用本地描述、标签、OCR、使用场景和冷却规则的评分第一名，不再同步调用
  Qwen 视觉模型。
- 用户明确索要表情时，只有前两名本地分差不超过 `0.75` 才进行视觉精选；候选数量从 6 降为
  3，视觉精选最长等待 2 秒，超时、失败或返回非法编号立即回退本地第一名。
- 新表情入库仍由后台 Qwen 视觉分类生成描述、情绪标签、适用场景和置信度，不影响当前聊天
  回复速度。

### 文档与发布

- 使用精简版项目介绍作为根目录 README，将原完整部署、配置、命令和架构说明迁移到
  `docs/help.md`，并修复迁移后的全部相对链接。
- 版本提升至 `3.0.1`；本补丁不新增 Alembic 迁移，不清理或改写现有聊天、记忆和表情数据。

## 3.0.0 - 2026-08-01

### Memory V2 正式质量门禁

- 新增 `memory/quality/` 独立质量包和 18 个版本化合成案例，使用真实 Memory V2 写入、FTS、
  Fake Embedding 语义检索与上下文服务，输出严格结构化指标及 JSON/Markdown/JUnit 报告。
- 新增外部 `memory_quality_gates.toml`、冻结 baseline 和独立 GitHub Actions job；无分母指标保持
  `null`，CI 不调用真实模型/Qwen，不读取真实聊天数据，也不会自动降低阈值或重写 expected。
- 新增显式合成大库性能场景：100 用户、10,000 facts、10 个群、100,000 条事件，测量 rebuild
  plan、keyset、混合检索、上下文投影、峰值内存和请求计数；结果按机器类别写入 baseline，
  不读取生产数据库，也不把跨硬件绝对延迟作为 CI 门禁。
- 新增 `memory quality validate-dataset|run|compare|update-baseline`、契约目录与 Plugin API 1.0
  快照；Memory Fact/Evidence v2、Query/Context/Embedding/Rebuild/Quality/Performance 公共契约正式冻结。
- 当前合成数据集 hash 为
  `6d2711f8d3882fefcbca82a39a073fd5f8e3c22cd26ffb36f89353170b0a3f15`，门禁配置 hash 为
  `281e6f2013b93569fc059a285097b4bff4ce305f40109af89db7be7764fe6120`；18/18 case、
  38/38 绝对门禁通过，最终回归为 722 passed、1 skipped。

### 生产治理与发布收口

- 新增必须显式指定数据库的内容无关 `memory audit`、fingerprint 保护的
  `memory hygiene scan/apply` 和只读 `memory release-check`。Hygiene 只处理来源明确无效的自动
  事实与可重建派生数据，不自动改 explicit/contested 事实，不物理删除事实或证据。
- 新增 fresh、2.1.2、a1、a2、b1、b2、rc1 到 `0024` 的迁移矩阵；正式版没有新增生产迁移，
  不自动 rebuild，不恢复 Memory V1，Plugin API 仍为 `1.0`。
- 版本提升为 `3.0.0`，补齐指标分母、运维、升级、隐私、故障排查与正式发布报告。

## 3.0.0rc1 - 2026-08-01

### 从事件账本受控重建

- 新增显式 `plan → start → review → approve/reject → commit` 历史重建流程，只读取固定快照内的
  `chat_events`。plan 不调用模型，提取阶段只暂存 proposal；升级、启动和 Worker 启动均不会
  自动创建、开始或恢复任务，进程重启会把执行中任务持久暂停。
- 实时记忆与历史重建共用 `MemoryEventExtractor`、`MemoryClaimProcessor`、`SubjectResolver`、
  Claim Validator、冲突候选和 `MemoryFactService`。空消息不提取，证据必须逐字来自当前事件，
  上下文只按 `current_speaker / other_member / bot` 辅助消歧，交互偏好不再混入人物事实。
- 历史提交会重新加载和校验源事件、身份与 live receipt；旧事实不能覆盖较新的 active 事实，
  相同历史证据不会把 `last_confirmed_at` 改早，过期事实只能跳过或保存为 invalidated，容量已满
  时不会淘汰当前事实。

### 持久状态、管理与运维

- 非破坏性 Alembic `0024` 新增 `memory_rebuild_runs/items/proposals`，并扩展
  `memory_jobs` 为 live/rebuild 共用的逐事件 receipt。扫描采用 `occurred_at + event_id` keyset，
  支持配置化提取并发、持久退避重试、暂停、恢复、取消、审阅分页和暂存清理；run 同时持久
  累计真实模型请求、供应商 usage token 与延迟统计。
- 新增 `/ai memory rebuild ...` 超级管理员命令和十个同服务 Tool Kernel 工具；`/ai forgetme`
  会删除人物暂存 proposal、取消仅针对该人物的非终态任务并脱敏 selection。Plugin API 仍为
  `1.0`，没有暴露历史重建接口。
- 新增无正文健康状态与指标、完整配置示例、架构/升级/隐私/故障排查文档，并将版本提升为
  `3.0.0rc1`。Run 完成只表示事实提交完成，不等待可重建的 Embedding 派生任务。

## 3.0.0b2 - 2026-08-01

### Memory V2 冲突治理与可信修正

- 新增 `assert / confirm / correct / retract` 记忆操作、`explicit / self_report /
  group_report / third_party` 来源权威、`clear / contested` 冲突状态和确定性
  `MemoryResolutionPolicy`。LLM 只对有界候选分类语义关系，不能决定数据库状态、身份、权限或
  authority；分类失败会保守降级，不覆盖已有事实。
- 修正、撤回和合并不再原地改写或物理删除正文：修正建立 supersedes 版本链，撤回转为
  invalidated，冲突保留双向可查关系和完整状态事件。相同陈述复用事实并聚合真实事件证据，
  confidence 使用后端固定权重和 authority 上限计算，好感度与信任度不参与事实真伪判断。
- 真实群 `@` 和回复作者可以成为第三方记忆主体，但只允许写入当前群的 `person_group` 作用域；
  普通名字文本、私聊、Bot 和其他群都不能产生第三方主体。本人后续确认可提升 authority，
  第三方陈述不能覆盖本人或显式事实。

### 生命周期、审计与接口

- 新增非破坏性 Alembic `0023`，扩展 `memory_facts` / `memory_evidence`，并建立
  `memory_fact_relations` 与 `memory_fact_state_events`；现有事实、证据、FTS 和 Embedding
  派生数据均保留。存在 contested fact 时 downgrade 会明确拒绝。
- 新增本地 `MemoryMaintenanceWorker`，按 `valid_until` 和可热更新的低价值陈旧规则有界处理
  自动事实，只改变状态、不扫描聊天历史、不调用 LLM/Embedding，也不修改 explicit 事实。
- 新增 `/ai memory show|explain|history|conflicts|correct|invalidate|restore|merge|resolve|doctor|
  maintenance`，以及 Core 只读工具 `get_memory_fact` / `get_memory_evidence`。普通用户只能审计和
  修正本人事实；跨人物、跨群合并、冲突裁决和完整诊断仅允许真实 `SUPERUSERS` 发送者。
- Plugin API 保持 `1.0`：`MemoryFacade.update()` 改为建立修正版，`delete()` 改为显式失效，
  插件不能自行设置 status、authority 或 conflict_state。健康检查增加冲突、一致性、维护和分类
  错误指标，文档与 `.env.example` 同步到 `3.0.0b2`。

## 3.0.0b1 - 2026-08-01

### Qwen Embedding 与混合 RAG

- 新增 DashScope `qwen3.7-text-embedding` Provider、1024 维 little-endian float32 BLOB 编解码、
  profile 指纹和持久化后台任务；事实事务先提交，Embedding 失败不会回滚或阻断聊天。
- 相关检索每轮最多生成一次 query embedding，在全部合法目标间复用；人物、人物群内和群作用域
  先由 SQL 硬过滤，再计算余弦相似度，并用确定性 RRF 融合 FTS 与语义候选。
- Provider 超时、限流、认证、响应格式或向量异常时自动降级为词法检索；overview 不调用外部
  Embedding。数据库、日志、指标和诊断均不保存 API Key、查询正文或事实正文。

### 数据库、运维与兼容

- 新增非破坏性 Alembic `0022`，建立 `memory_embedding_profiles`、`memory_embeddings` 和
  `memory_embedding_jobs`；事实删除级联清理派生向量，不扫描或重建历史聊天。
- 新增 `/ai memory embedding status|doctor|retry|rebuild|purge-old`、健康指标、热更新混合检索
  参数和完整 `.env.example`。Embedding 默认关闭，故障时保持 3.0.0a2 的词法行为。
- 补充迁移、Provider、批处理、重试、内容哈希、身份隔离、混合排序与降级测试；Plugin API
  继续保持 `1.0`，版本提升为 `3.0.0b1`。

## 3.0.0a2 - 2026-08-01

### 查询驱动的 Memory V2

- 新增 `MemoryQueryBuilder`、可信 `MemoryTargetResolver`、`MemoryRetriever`、确定性
  `MemoryRanker`、`MemoryContextService` 和不记录正文的检索指标；普通聊天按当前消息、
  有界回复文本和 Planner intent 选择相关事实，不增加 LLM 调用。
- 人物、人物群内和群作用域在词法候选 SQL 内硬过滤。只有当前事件真实 `@` 或回复且已验证为
  本群成员的人物才有独立 referenced block；最近发言者不再自动获得长期记忆检索资格。
- relevant 无匹配时不加载全部事实，只允许当前人物有界的显式交互偏好；overview 按实体独立
  限量。事实只有通过最终上下文预算后才批量更新 `last_used_at`。

### FTS5、接口与运维

- 新增 Alembic `0021`：建立外部内容 FTS5 `trigram` 表 `memory_facts_fts`、三类同步触发器并
  回填现有 Memory V2 facts；downgrade 只删除派生索引，不删除事实和证据。
- 查询统一执行 NFKC、casefold、空白压缩、运算符隔离与有界词项生成；两字查询只在已有实体
  硬过滤的范围内使用 `LIKE`。排序稳定考虑 key/内容/类别精确匹配、BM25、重要度、置信度、
  更新时间和事实 ID。
- Core `get_person_memories` / `get_group_memories` 增加 `query`、`mode` 和 `limit`；管理员新增
  memory search 与 index status/rebuild；Plugin API v1 `MemoryFacade.search()` 复用相同检索器。
- 新增 7 项热配置、跨 QQ/跨群/短查询/排序/迁移/一万条事实规模回归，版本提升到
  `3.0.0a2`。本版本没有 Embedding、向量依赖、模型重排或历史聊天重建。

## 3.0.0a1 - 2026-07-31

### Memory V2 不可逆切换

- 新增 Alembic `0020`，永久删除旧人物记忆、群记忆、群内人物记忆、偏好与旧记忆任务，
  建立空的 `memory_facts`、`memory_evidence` 和新版 `memory_jobs`；聊天事件账本、人物、群、
  关系、自动化和插件数据不迁移也不删除。该迁移不可 downgrade，回退必须恢复升级前数据库。
- 新增长期事实的 person、person_group、group 三种严格作用域，以及 fact、preference、episode
  类型、active/superseded/invalidated 状态、来源、置信度、有效期与真实消息证据链。
- 同一主体、类型和 key 的相同内容复用 active fact 并追加证据；内容变化建立新版本并替代旧
  版本；自动提取不能覆盖显式事实；事实与证据在同一事务中提交。

### 身份安全提取与上下文隔离

- 每个真实入站非 Bot 事件对应一个持久任务，逐事件调用 `ModelTask.MEMORY_EXTRACTION` 的
  Flash 路由并逐事件提交；不同人物、群或会话不共享结构化输出，取消会原样传播。
- 模型只看到 `primary_event`、同会话少量前文和后端生成的 `speaker/group` 引用，不能提交
  QQ 号、群号、事件号、证据发送者、状态或版本字段；未知主体与私聊 group claim 直接拒绝。
- 聊天上下文只注入当前人物、当前人物在本群和当前群的 active facts；相关群友只保留当前群
  身份元数据，不再默认加载其关系或长期记忆。Prompt 明确禁止跨 entity block 归因和猜测。

### 接口与工程化

- Core 记忆工具、管理员记忆/偏好命令、自动化与 Plugin API v1 的 MemoryFacade 全部切换到
  Memory V2；列表输出增加 `fact_id`、置信度、状态和证据数，并新增管理员证据查看命令。
- 删除旧记忆模型、Repository、Worker、记录类型和旧表测试，不保留双写、兼容读取、导入器
  或启动时历史 backfill；第一阶段没有加入 FTS、Embedding 或向量数据库。
- 新增 Memory V2 架构、路线与不可逆升级文档，并将项目版本提升到 `3.0.0a1`。

## 2.1.2 - 2026-07-31

### MCP 与 Tool Kernel 一致性

- 修复 `mcp_gateway` 以只读外壳直接执行任意远端工具的策略绕过：Gateway 的 `call` 现在必须先
  经 `MCPManager.resolve_tool` 解析当前已启用、已发现且通过 include/exclude 过滤的元数据，
  再按目标工具真实的 scope、effect、risk、当前轮 ToolMode、图片和联网状态通过同一个
  `CapabilityPolicyEngine`，最后才进入 `MCPToolBinding`。未查看/未选择、未发现、被排除或
  当前策略拒绝的工具均不能执行。
- `ToolExecutionResult.mutation_committed` 改为 `True / False / None` 三态，新增统一
  `resolve_mutation_commit()`：失败固定为 false，Provider 显式结果优先，只读成功推断 false，
  写入或平台修改成功推断 true。MCP 不再把所有成功结果硬编码为 false，成功下单能进入本轮
  重复修改保护；重复调用不会覆盖此前成功结果的最终摘要。
- MCP annotation 新增 `destructiveHint → CapabilityRisk.DESTRUCTIVE`，并在 Descriptor 元数据中
  保留 `openWorldHint`；远端声明只描述风险，最终执行权仍由 Yuki 的统一策略决定。
- 新增通用 `yuki.toolBundles`。一个工具可属于多个 scope；Planner 选中 Bundle scope 后，
  本地候选、Flash 精排、全局/MCP 工具数量限制都不能删掉 Bundle 必需成员，完整 Schema
  超出预算时返回明确错误，不实现 Workflow DSL，也没有麦当劳品牌分支。
- `get_my_capabilities` 移到独立 `capability` scope，并以 `DIRECT_ALWAYS` 在真实用户聊天且
  ToolMode 非 NONE 时保留；普通用户和超级管理员统一调用该工具。模型目录不再暴露
  `admin_list_capabilities`，真实权限仍按当前 OneBot 事件在后端解析。
- 新增 `MCPFailureDisposition`，将工具/业务失败与连接失效分开。普通 4xx、429、MCP
  `isError` 和业务错误不再断开连接；仅会话失效、网络断开、协议或初始化失败触发断开。
  `CancelledError` 原样传播，不写失败调用、不更新错误状态，也不触发重连。
- Schema Token 估算统一覆盖函数名、描述、参数与 function-calling 外层；超长工具结果优先
  保留 URL、ID、状态和错误。`payH5Url` 会留在模型上下文及最终 QQ 回复中，但不会被自动打开
  或支付。

### 离线验收

- 新增 Fake MCP、Planner、Model、Sender 的完整下单集成测试，覆盖查询门店、菜单、详情、
  校价和创建待支付订单；验证下单只执行一次、提交状态为 true、重复调用被拦截、支付链接
  可见、无麦当劳插件、无联网调用且不泄露 Token。
- 增加 Gateway 只读/排除/未发现/未选择回归，Bundle 整体选择与预算回归，能力自省普通用户/
  管理员一致性、MCP 业务错误不断线、取消不重连，以及关键 URL/ID 裁剪保留测试。

### 网易云音乐卡片插件

- 修复当前 Host 中插件仍在运行、但持久化展示状态被短生命周期诊断进程回写后出现“工具可见却无法执行”的状态分裂；工具发现与执行现在统一以当前 Host 的内存生命周期为准，避免错误返回 `plugin_tool_denied`。
- 修复网易云搜索返回候选、未命中等“查询成功但尚未发送”的中间结果被误记为已完成修改，导致同一轮补充歌手或歌曲 ID 时触发 `duplicate_mutation` 的问题；Plugin SDK 现可显式报告条件型工具是否已经产生外部效果。
- 搜索请求会把歌名和歌手共同交给 MCP；仅输入歌手且搜索结果明确属于该歌手时，可直接选择网易云排序首位的歌曲发送。
- 新增内置可选插件 `io.github.yuanyeyoutao.netease-music-card`：通过独立
  `netease_music` MCP Server 搜索、校验歌曲，重名时先返回最多 5 个候选，选定后向
  当前 QQ 私聊或群聊发送 OneBot `music(type=163)` 原生网易云分享卡片。
- Plugin API v1 的 `ctx.onebot` 新增 `send_music_card(provider, resource_id)` 安全 Facade；
  只允许发送到当前真实消息场景，普通插件无需也不会获得任意 OneBot action 或任意目标权限。
  成功发送会写入永久事件账本，并保留脱敏审计记录。
- `.mcp.json.example` 新增默认禁用的网易云只读服务模板，README 补充外部 Server、插件批准、
  Docker 启动和自然语言实测步骤；网易云数据访问与 QQ 平台发送继续保持职责分离。

## 2.1.1 - 2026-07-31

### 通用自动化编译器

- 自然语言创建入口由要求模型手写完整 `AutomationScript` 改为高层 `TaskSpec`；新增
  `AutomationCompiler`，由后端确定 `static/generated/agentic` 策略、投递步骤、上下文、工具与
  超时预算，再生成现有严格 DSL 作为稳定运行时 IR。Worker、租约、执行器和插件 SDK 不需要
  重写，也未新增聊天路由或第二人格。
- capability 在模型侧使用只含字母、数字和下划线的稳定 ID；后端兼容点号、连字符与下划线
  差异并解析为真实注册名。受委托 `yuki.agent` 只保存 TaskSpec 明确选择的最小能力集合，不再
  自动继承当时全部可用工具；模型函数名也统一规范化，修复 `create-order/create_order` 导致的
  工具不可用问题。
- Agentic 自动化的基础模型请求预算提升到 10 次，并由编译器按后端硬限制计算工具和消息预算；
  复杂点餐等任务在触发时再查询动态菜单、优惠券、价格和订单信息，创建时不会提前执行外部工具。
- 创建成功结果新增 `confirmation=persisted`，只有数据库返回真实 `automation_id` 才允许报告成功；
  失败的编译/提交尝试写入脱敏审计，并新增 `automation_diagnose` 供 Yuki 核实最近创建结果。
- 修复“几分钟后查询菜单”“明天几点下单”等请求被 MCP 关键词抢占、当场执行的问题。未来触发
  意图现在在同一 Planner/Agent 链路中强制收敛到 `automation_create`，并从本轮移除即时 MCP、
  联网、OneBot 和业务工具；没有持久化确认时，最终输出不能再声称“设好了”。

### MCP 自动化桥接

- 新增通用 `MCPAutomationBridge`：可将任意 MCP Server 明确选择的远端工具动态投影为
  `mcp.<server>.<tool>` 自动化 capability，直接步骤和受委托 `yuki.agent` 共用同一套
  JSON Schema、执行结果、Artifact、重试和审计链路，不在自动化核心中硬编码麦当劳逻辑。
- MCP 配置新增 `yuki.automation.enabled/permission/includeTools`；必须显式列出可供后台任务
  使用的工具。远端 Schema 的完整元数据哈希会写入委托快照，工具参数变化、移除、禁用或
  创建者权限下降都会使旧委托失效。
- 麦当劳预设默认只向真实超级管理员的自动化开放 14 个活动、优惠券、账户、菜单、校价及
  订单工具；加入 `query-meals → query-meal-detail → calculate-price → create-order` 到店点餐链路。
  查询失败可瞬时重试，`auto-bind-coupons` 与 `create-order` 等修改型调用不自动重试；创建订单
  只返回官方待支付订单和支付链接，不代替用户完成支付。新增动态刷新、权限隔离、JSON Schema
  校验和旧委托撤销回归测试。

## 2.1.0 - 2026-07-30

### 麦当劳 MCP

- 将 `.mcp.json.example` 的麦当劳配置升级为官方 `https://mcp.mcd.cn` Streamable HTTP
  一等预设，补充 Bearer Token 环境变量、点餐/门店/优惠券/积分能力标签和独立接入文档。
- MCP Server 元数据新增 `yuki.toolAnnotations`，可按远端工具名覆盖标准只读、破坏性、幂等和
  开放世界提示；麦当劳查询工具据此并行调度，地址新增、领券、下单和兑换仍按修改型串行执行。
- MCP HTTP 401/403、429、5xx、超时和网络故障改为稳定且不含 Secret 的诊断信息；新增麦当劳
  Bearer Header、`2025-06-18` 协议协商、查询/下单调度语义和错误分类的离线回归测试。

### Tool Kernel 与 MCP Client

- 新增统一 `ToolProviderRegistry`、`UnifiedToolCatalog`、`ToolBinding`、
  `ToolExecutionResult`、`ToolInvocationCoordinator` 和完整 Schema/结果预算；Core、Admin、
  Automation、Plugin、MCP 共用同一 Planner 与 AgentRunner，不再按来源维护执行分支。
- Planner 的固定 `groups` 迁移为动态 `scopes`，继续接受 2.0 旧字段；未知 scope 明确失败。
  Planner 只看紧凑摘要，普通聊天未选择 MCP 时不会注入 MCP Schema。
- 新增本地 `ToolCandidateSelector` 与 `ModelTask.TOOL_SELECTION` Flash 精排；Flash 输入不含
  JSON Schema，返回值必须由后端当前目录复核。
- 新增通用 MCP Client，支持 `.mcp.json`、集中环境变量插值、stdio、Streamable HTTP、
  lazy/eager/keep_alive/lazy_keep_alive、元数据缓存、同名工具隔离和可选 `mcp_gateway`。
- 新增统一超长结果 Artifact 和 `read_tool_artifact` 分页读取；数据库只保存不可猜测 handle
  与内容无关的调用指标，完整结果保存在有期限的 `data/tool_artifacts/` 文件中。
- 新增 `/ai mcp list|show|status|tools|search|refresh|reconnect|enable|disable|doctor|call`、
  `/healthz` MCP 指标和 `/ai status` 最近调用状态。管理输出不显示 Header、Cookie 或 Secret。
- Plugin API v1 新增 `ctx.mcp` 门面与 `mcp.read`、`mcp.call` 权限；MCP Tool 本身不申请
  PluginPermission，也没有新增 MCP 专属逐项审批流程。
- Alembic `0019` 非破坏性新增 `mcp_server_states`、`mcp_tool_cache`、`tool_artifacts`、
  `tool_invocations`；版本提升到 2.1.0，新增完整 MCP 与扩展内核路线文档。

### 人格与语气

- 系统提示词恢复旧版的人格层次与口语例句，补充银白长发、蓝色兔耳发带、雪花发饰、
  白色水手服和蓝眼睛的完整外貌设定，并将 Yuki 的生日固定为 7 月 23 日。
- 强化日常聊天一句为主、通常不超过 50 个中文字符的规则，减少客服腔、报告腔、
  模板化开场与无意义总结；复杂任务仍按需要完整回答。
- 文本回复继续禁止 Unicode Emoji、颜文字、ASCII 表情和装饰性符号；保留语音轮次
  使用“ゆき”自称、工具结果如实报告和媒体占位符隔离规则。
- Planner 的引用目标默认保持为空；私聊回答当前消息时后端会清除冗余引用，只有明确选择
  更早的消息，或多人群聊中确实需要指向某条消息时才使用回复气泡。
- 日常语气进一步限制长难句和连续复合连接词；括号动作、场景与心理描写只在用户明确提出
  角色扮演或场景表达时启用，不再从亲密话题或普通情绪自行推断。
- 日常聊天保持轻松口语，普通短回复不再用中文句号收尾；问句、感叹句和正式长回答仍按语义
  保留必要标点，相关口语示例同步调整。

## 2.0.0 - 2026-07-30

### 破坏性精简

- 删除已经脱离当前主链路的 `AdminIntentRouter`。管理员自然语言操作只保留统一的
  Yuki Agent 工具循环，不再维护第二套模型路由、澄清状态和工具执行循环。
- Planner 现在是普通聊天固定且唯一的决策边界；删除 1.5.x 自主群聊候选器、旧置信度/冷却/小时计数器，
  以及 `PLANNER_ENABLED`、`AUTONOMOUS_*` 和 `autonomous.*` 运行时配置。
- 删除未被调用的能力执行包装器、旧 AI 聊天命令包装器和生产包中的 Web 测试 Fake；Web Fake 移入
  `tests/`，测试依赖不再混入部署产物。
- Planner、上下文构建器改为消息处理器的必需依赖，移除生产构造器静默创建测试 Planner 的兼容分支；
  群自主发言必须同时满足全局 Planner 群聊开关和当前群开关。
- 修复 Planner 故障回退错误清空已授权工具组的问题。回退现在只按后端声明的工具组和只读规则收紧能力，
  不会让历史、记忆、联网或管理员工具无故消失。
- 超级管理员能力目录随删除后的配置注册表更新为 131 项可修改配置；
  `planner.max_pending_messages` 新增中英文检索别名。
- 删除已不参与私聊准入的 `ALLOWED_PRIVATE_USERS`，并删除被 `MODEL_PROFILES_FILE` 任务路由取代的
  `PLANNER_MODEL`；当前 Planner 实际模型名改从模型路由执行器读取并写入运行记录。
- Plugin API 仍为 `1.0`；示例插件与开发文档的宿主版本范围更新为 `>=1.6.0,<3.0`，
  使声明兼容 2.x 的现有 v1 插件可继续通过合同校验。

### 修复

- 日语前端部署现在使用与 Worker 内 `e2k==0.6.2` 一致的 `model-c2k.npz` 和
  `ngram.json.zip`；语音 CLI 与 `/healthz` 显示日语前端可用性，无需重新导入 Roxy 声线。
- 在语言路由确定后增加最小的 Genie-TTS 2.0.2 中文 G2P 兼容层，避免短语气词“嗯”使
  ToneSandhi 产生空韵母并越界；日语平假名保持原文，不经过中文 G2P。
- 修复用户明确索要语音或表情时，Planner 意图已命中但 ReplyEffect 静默降级为文字的问题；
  明确的表情请求不再被自主发言的整轮冷却阻断，仍保留单张表情防重复规则。
- Planner 现在会缩小无效的消息目标、引用 ID、消息数和工具组，不再因一个可恢复的附带字段
  丢弃整份合法的语音或表情计划；降级日志会记录脱敏错误原因。
- 未部署日语前端资产时，Planner 不再选择日语；若已生成中日双语回复才发现资产缺失，
  语音服务会自动提取其中文部分完成合成，避免整轮语音消失。
- 修复 `function_tool` 结构化输出被误判为普通 Agent 工具能力的问题；Flash 档案现在可以正常
  执行 Planner、记忆、关系和表情替换任务，显式语音请求也会重新获得 Planner 授权。
- 重整模型历史投影：时间只保留在结构化运行时上下文，不再写进 assistant/user 台词；纯表情
  媒体及旧版语音/表情占位描述不会进入文本历史，混合图文事件仍保留真正发送的正文。
- 表情描述只保存在结构化消息段，不再作为 `[表情：…]` 伪正文写入账本；核心回复契约明确禁止
  用文本占位符假装发送语音或表情，实际媒体统一经过 ReplyEffect 与 OneBot 发送边界。

## 1.9.0 - 2026-07-29

### Rising Sea 架构重构

- 新增 `ModelTask`、`ModelProfile`、`ModelRoute`、`ModelRouter`、共享连接池和 `TaskModelExecutor`。默认显式路由把主聊天、自动化 Agent、插件 Agent 会话交给 Pro，把 Planner、记忆、关系、表情替换、自动化文本和辅助结构化任务交给 Flash；缺失路由、能力不兼容或密钥缺失会在启动时失败，不会隐式改用 Pro。
- 新增 `config/model_profiles.example.toml` 与 `MODEL_PROFILES_FILE`；TOML 只引用环境变量名。未提供文件时把旧 `LLM_*` 规范化为 `main` 档案并记录弃用提示，保持 1.8.2 行为。
- `ChatResponse` 新增 prompt、completion、total 和 cached prompt Token；Alembic `0018` 新增不含正文的 `model_invocations`。新增 CLI `model profiles|routes|stats` 和超级管理员 `/ai model stats`，最近错误显示数通过 `MODEL_STATS_RECENT_ERROR_LIMIT` 配置。
- 新增泛用 `StructuredTaskRunner`，Planner、MemoryWorker、RelationshipEvaluator、EmojiReplacement 和兼容自主参与判断直接使用 Pydantic 输出 Schema；删除这些路径的 fenced JSON、平衡花括号和重复字段解析。
- Planner 固定提示词改为短决策契约，`TurnPlan` Schema 成为结构权威；`ToolSelection` 使用 `inherit|none|read_only` 与后端工具组子集，Planner 只能缩小能力。
- 新增 `PromptContribution`、`PromptProgram`、`PromptCompiler`、`ContextContribution` 和通用预算器。常规请求现在只有一个稳定静态前缀和一个紧凑动态 Envelope；插件上下文合并为一次不可信包装，当前消息始终保留。
- 系统提示词精简为五段人格与文本风格规则：Yuki 不主动输出 Unicode Emoji、颜文字或 ASCII 表情；引用、解释或转换用户明确指定的符号时仍可原样输出，不在最终回复上使用粗暴删除正则。
- 新增 `CapabilityDescriptor` 与元数据策略引擎，统一 effect、risk、trust source、origin、权限、外部数据和幂等性；图片/网页隔离、只读筛选与 ReplyEffect 通过元数据执行。表情图片和语音请求共同实现 `ReplyEffect` 契约。
- 根配置保留全部 1.x `.env` 名称，同时组合 13 个不可变领域设置模型；移除跨数百字段的统一 validator 和多项任意硬上限，不合法值在所属 Pydantic 领域边界明确失败。
- 新增 `ApplicationModule`、不可变 Bundle 与 `LifecycleRegistry`，拆出 Persistence、ModelRuntime、Web、Media、Emoji、Speech、Conversation、Admin、Automation 和 Plugin 模块；生命周期支持有序启动、反序关闭、启动失败回滚、关闭错误聚合和独立健康结果。`ApplicationContainer` 从基线 1,097 行降至 710 行。

### 离线日语语音前端

- Genie Worker 固定加入 `e2k==0.6.2`，新增 `JapaneseSpeechFrontend`。日语目标文本中的英文和拉丁字母片段按本地词典、C2K、NGram 和确定性字母拼读转换为片假名；进入 Genie 前保证不残留 ASCII 拉丁字母。
- 新增 `data/speech/japanese_frontend/lexicon.toml`，内置 Yuki、OpenAI、ChatGPT、API 等常用读音，匹配不区分大小写；词典可直接编辑并通过只读目录挂载给 Worker。
- `model-c2k.npz` 与 `ngram.json.zip` 必须由部署者手工放入 `data/speech/japanese_frontend/models/`，项目不会下载模型。资产缺失或损坏时日语请求明确失败，中文和英文合成保持不变。
- IPC 和健康状态新增可选的 frontend version、spoken text hash、transformed token count、availability 和 signature；语音缓存键包含前端签名，资产或词典变化会自然失效旧缓存，数据库和日志不保存转换后全文。

### 质量与诊断

- 新增 `qq-ai-bot-cli prompt inspect|compare`，输出静态/动态/历史/工具 Schema 字符、估算 Token、贡献 ID、模型路由、usage 可用性和稳定前缀 hash；基线与结果报告位于 `docs/refactor/`。
- GitHub Actions 增加 Prompt benchmark 和模型路由专项测试；主项目继续执行 Ruff、严格 mypy、pytest、Alembic 空库升级、Plugin 契约和 Docker 构建，Genie Worker 继续独立执行 Ruff、mypy、pytest 与 speech profile 构建。

## 1.8.2 - 2026-07-29

### Planner 统一语音治理

- 修复文字加语音模式把 `[语音：Yuki 发送了一条语音，声线：…]` 等内部 TTS 元数据写成普通
  对话正文的问题。实际说话文本与媒体元数据现在严格分离；声线、风格、语言仍保留在结构化
  消息段，旧格式记录会从模型上下文中隐藏，并在 `0017` 升级时清理其正文副本。
- 系统提示词新增语音朗读规范：包含语音的轮次中，自称 `Yuki` 写作 `ゆき`，避免日语 TTS
  按 `Y U K I` 逐字母朗读；纯文字回复不受影响。
- 文档补充 `PLANNER_MODEL=deepseek-v4-flash` 的独立低延迟配置方式；当前部署已切换到该模型，
  主聊天模型仍保持独立，不因 Planner 配置而改变。
- 修复 Agent 成功排队 `send_voice` 或普通可选表情后，模型最终正文为空时整轮静默的问题：
  语音与普通表情不再被误判为可独立发送的内容，Agent 会在同一轮补生成正文；只有明确的
  “纯表情回复”允许没有文字。媒体选择或合成最终未产出时，发送层仍会退回文字，保证 Planner
  已决定回复的轮次不会无声结束。
- 修复 Planner 的真实 JSON 响应被内部 Python 严格枚举模型误拒绝的问题；仅对
  `EmojiReplyPlan`、`VoiceReplyPlan` 和语音偏好变更这三个 LLM 边界 DTO 接受 JSON 字符串枚举，
  其他持久化与运行时模型继续严格校验。未知的模型自定义 `reason_code` 现在只会归一化为稳定
  观测类别，不再丢弃其余完全合法的回复、表情和语音计划。
- 删除聊天后端的语音请求/拒绝固定短语表，改由 Planner 根据自然语言和对话上下文输出
  `explicit_request`、`explicit_opt_out` 或 `neutral` 语义意图；明确触发、持续偏好和普通闲聊
  使用同一条 Planner → Agent → ReplySequence 链路，不新增语音路由。
- `send_voice` 只在 Planner 确认当前用户本轮明确索要语音时向 Agent 开放；Agent 仅能选择
  `style_hint` 和目标语言，不能覆盖 Planner 决定的 `text`、`voice` 或 `text_and_voice`。
- 日常主动语音完全归 Planner 决定，并新增可热更新的
  `speech.spontaneous_frequency` / `SPEECH_SPONTANEOUS_FREQUENCY`（默认 `0.15`）；后端按当前
  会话最近的中性 Planner 轮次计算频率预算，不读取或匹配聊天正文。
- 新增人物级 `text_only`、`auto`、`prefer_voice` 持久语音偏好。只有用户本人明确表达未来/默认
  模式时才写入，单轮要求不落库；人物删除时偏好级联删除。
- 新增非破坏性 Alembic `0017`：创建 `person_speech_preferences`，并为 `planner_runs` 增加脱敏的
  语音意图、工具授权、模式、原因和频率观测字段；聊天正文、人物、声线和生成历史全部保留。
- Planner/Agent/迁移/仓储测试覆盖语义意图、未授权工具隐藏、持久偏好、人物级联、频率预算和
  中性轮次统计。

## 1.8.1 - 2026-07-29

### Roxy 中日双语、语音调用与内存优化

- 声线 Manifest 新增 `supported_languages`，Planner 语音计划新增受后端约束的 `language`（`auto`、`zh`、`jp`）；Yuki 可以根据语境自行切换中文或日文，最终文本脚本会作为安全校验，防止错误语言前端。
- 区分参考音频语言与目标合成语言：日语参考音频可为中文或日文目标文本提供音色，Worker 在同一声线切换语言时会正确重载角色前端。
- 语音缓存键和生成记录新增目标语言，避免中日文缓存串用；新增非破坏性 Alembic `0016`，保留现有声线和生成历史。
- 模型转换工具新增 `--languages` 与 `--reference-language`，支持一次生成多语言档案；部署文档补充双语 Manifest、转换、Planner 与参考音频规则。
- Agent 的可信运行时说明明确标注本地 TTS 不提供 HTTP/TCP 端口，只通过配置的 Unix Socket 通信，避免把 Bot 8080 或 NapCat WebUI 6099 误报为 TTS 端口。
- 修复 `speech.default_mode` 未进入 Planner 后端约束的问题；明确“用语音说/念/读”会确定性强制语音，明确拒绝语音和技术型长内容保持文字，日常短聊天会实际采用配置的默认语音模式。
- Agent 新增路径隔离的 `send_voice` 回复效果工具，可在当前轮自主排队 `voice`、`text_and_voice` 或 `optional`，但不能指定模型、profile、参考音频或文件路径。
- Worker 在模型切换、合成、参考缓存清理和卸载后主动执行 GC，并在 Linux/glibc 上调用 `malloc_trim`，把 ONNX 转换与推理产生的空闲堆页归还给 Docker/Windows，降低长时间运行和中日切换后的常驻内存与峰值。
- Bot 启动时只校验并同步默认声线元数据，不再预热完整 ONNX 模型；首次真正合成时按需加载，避免仅保持 QQ 在线也占用数 GiB 内存。
- 新增 `SPEECH_WORKER_IDLE_RECYCLE_SECONDS`（默认 300 秒）：语音空闲后 Worker 正常退出并由 Compose 自动拉起为空载进程，释放 Genie 持有的全局 ONNX Session；本机可缩短该值，在内存与首次生成延迟间取舍。

## 1.8.0 - 2026-07-29

### 完全本地 Genie-TTS QQ 语音

- 新增可选的独立 `genie-tts-worker`：固定 Genie-TTS 2.0.2，通过 Unix Domain Socket 接收严格版本化 IPC，使用部署者自行准备的 GPT-SoVITS V2/V2ProPlus ONNX 模型离线生成 32 kHz 单声道 16 位 WAV；Worker 不开放 HTTP/TCP、禁用网络和自动下载，生产环境不安装 PyTorch。
- 新增 `SpeechService`、通用 `TTSProvider`、Genie Client、严格 `SpeechPathPolicy`、文本朗读规范化、模型/参考校验和缓存键、缓存清理、取消和队列生命周期；主 Bot、Planner、插件与自动化不直接依赖 Genie 客户端。
- 新增非破坏性 Alembic `0015`：`speech_voice_profiles`、`speech_voice_references`、`speech_generations` 保存声线、多风格参考与生成状态，只保存 `data/speech/` 内相对路径和文本哈希，保留全部现有数据。
- 新增严格 `profile.toml` 声线档案、原子目录导入、启停/默认/重载、参考音频 sidecar 导入、style/alias 确定性匹配；未知字段、路径逃逸、缺模型/参考和无效默认风格会明确失败。
- Planner `TurnPlan` 新增受后端约束的 `voice` 计划，支持 `text`、`voice`、`text_and_voice`、`optional` 与语义 `style_hint`；模型不能指定 profile/reference/path，Worker 或档案不可用时保持文字回复，新消息会取消尚未发送的过期语音。
- 复用现有 OutboundMedia、ReplySequence 和事件账本；OneBot Adapter 在最终发送边界把本地 WAV 编码为 Base64 `record`，NapCat 不需要看到本地路径，Base64 不进入普通日志或数据库。voice-only 保存实际朗读文本，text-and-voice 不重复正文。
- 新增 `/ai voice`、完整 `qq-ai-bot-cli speech`、自然语言管理员 `speech.*` action、`speech.send_private/group` 自动化；普通用户只能使用当前默认声线并发送到本人或任务创建时当前群，切换/重载/清理和任意声线测试仅超级管理员。
- Plugin API v1 新增 `SpeechFacade`、六项 `speech.*` 权限、Opaque `GeneratedSpeechHandle`、11 个生命周期事件和 `speech.tts_provider.v1` 预留扩展点；插件无法取得本地路径、模型、参考音频或伪造其他插件 Handle。
- 新增 `tools/genie_model_converter/`，把需要 PyTorch 的官方 GPT-SoVITS → Genie ONNX 转换与生产环境彻底分离；新增 12 篇 `docs/speech/` 文档、`.env.example`、README、健康状态、Docker speech profile 和 CI Worker 测试/镜像构建。
- 语音功能默认关闭，未准备模型时原有文字聊天行为不变。仓库不附带或下载任何 Galgame、动漫角色模型或原始语音，相关权利由部署者负责确认。

## 1.7.1 - 2026-07-29

### 群聊高参与度与明确触发可靠性

- 已启用群中的真实 `@Yuki`、回复 Yuki 和私聊改为后端强制 `reply` 且不等待；Planner 不能再因历史活跃度或自身偏好选择 `silent`。
- 普通未触发群消息不再抢占正在处理的明确触发 turn，修复 Planner 已决定回复但最终 `messages_sent=0` 的繁忙群竞态。
- Planner 默认改为活跃群友策略：群聊聚合等待从 8 秒降为 3 秒，必要性门槛从 80 降为 0，置信度门槛从 0.65 降为 0.2，决策历史从 20 条缩短为 8 条。
- 已通过自主发言门槛时，Planner 返回格式异常会可靠降级为正常回复，避免格式回退让 Yuki 持续沉默。
- 同步 `.env.example` 和 README；关闭 Planner 后的兼容自主模式也采用 3 秒静默、0.2 置信度、20 秒冷却和每小时 30 次。

## 1.7.0 - 2026-07-28

### 持久化表情系统

- 新增 `emoji_assets`、`emoji_scope_states`、`emoji_jobs` 和 `emoji_usage_events`，通过非破坏性 Alembic `0014` 保留全部 1.6 数据；旧 `emoji_descriptions` 继续作为 QQ 表情视觉描述缓存，不再承担新表情池状态。
- 新增格式感知、SHA-256 去重、可选 dHash、原子文件写入、动画原图保存和第一帧 WebP 预览；正式文件位于 `data/emoji/`，数据库、账本与普通日志不保存图片 Base64、绝对路径或签名 URL。
- 已启用群的未触发图片可按 `metadata_only/likely/all_images` 进入独立后台收集，但不会触发回复、关系评价、人物记忆或命令；私聊与群聊收集开关独立。
- 表情分类复用现有 `VisionProvider`、`MediaResolver` 和 `ImagePreprocessor`，写入结构化描述、情绪、场景、OCR、强度和置信度；按本次要求不实现审核系统、审核队列或第二次审核模型调用。
- 生命周期集中为 `candidate/recognized/adopted/rejected/banned/missing`；满足运行时阈值直接自动采用，容量为空时无限，容量满时按配置执行替换且保护 pinned 资产。
- Planner 新增不含资产标识的表情意图；Agent 新增只排队 `PendingReplyEffect` 的 `send_emoji` 工具。ReplySequence 支持文字前、文字后与仅表情发送，新消息可取消尚未发送部分，只有 OneBot 成功后才增加使用次数。
- 新增 `/ai emoji` 管理命令、自然语言 `emoji.*` 管理动作、`/ai status` 与 `/healthz` 状态；定时清理只删除过期且未采用、未固定的候选。
- Plugin API 新增 `EmojiFacade`、六项 `emoji.*` 权限、表情生命周期事件和 `emoji.selection_signals.v1`；插件信号失败不影响核心选择，也不能引入核心候选集之外的 ID。
- 自动化注册 `emoji.send` 与 `emoji.send_by_id`，复用既有调度器和 `DelegatedAuthority`；普通用户仅限本人私聊或任务创建时的当前群，发送写回永久账本及使用统计。
- 新增 Emoji 运行时配置组、`.env.example`、架构/生命周期/收集/视觉/选择/Planner/管理/配置/插件/自动化/存储文档，并将版本升级为 `1.7.0`。

## 1.6.0 - 2026-07-28

### Planner-first 会话重写

- 新增确定性的 `ReplyNecessityScorer`、严格 `TurnPlan`、`PlannerProvider/Service`、受限上下文构建、失败降级与脱敏可观测性；普通聊天先规划 `reply/wait/silent`、意图、发送模式、目标消息数和工具收窄方式，再进入现有单一 Yuki Agent。
- 私聊、明确 @/回复、管理员自然语言操作和群聊自主观察采用不同的后端门槛与降级规则；Planner 不提供工具、不产生最终正文，也不能修改身份、权限、记忆、关系、配置、自动化或插件批准。
- 新增 `ConversationTurnCoordinator` 和 `ReplySequenceManager`：群聊新消息可中断过期的自主 Planner/生成，私聊新消息可停止尚未发送的旧分句；已经开始的修改型工具不自动取消，代码块、表格、来源和结构化内容不会被机械逐句拆散。
- Planner 默认开启；`PLANNER_ENABLED=false` 保留 1.5.2 兼容路径。Planner-first 群聊不再读取任何旧 `AUTONOMOUS_*` 限制，群消息聚合改由新的热配置 `planner.group_debounce_seconds` 独立控制。
- `/ai` 确定性命令继续绕过 Planner；正常聊天、管理员工具、联网和自动化创建仍共用原有单一 Agent，不新增管理员人格或隐藏客服路由。
- 新增热配置 `planner.preferred_messages`（默认 3、范围 1～20）：Planner 在日常聊天和用户明确要求多条回复时选择 `natural_multi`，Agent 按句子或自然段形成语义边界，短内容不凑数，结构化内容保持完整。
- 超级管理员可用自然语言修改日常消息目标条数及单轮硬上限；`desired_messages` 作为配置别名可被能力目录检索。`reply.plan_hard_max_messages` 的可配置上限由 10 提高到 20，默认值仍为 10。
- 日常分句器不再因句子数超过目标就退回单条，而是按原顺序合并相邻语义单元，并支持以自然换行为分隔位置。
- 非结构化聊天输出中的空行现在是强发送边界，即使 Planner 选择 `single` 或 `concise` 也会拆成多条 QQ 消息；超过硬上限时只用单换行合并相邻段落，结构化模式仍保持完整。
- `TurnPlan` 新增受后端校验的 `reply_to_message_id`：Planner 可在多人聊天指向关系足够明确时自主选择引用回复，默认继续普通发送；目标只能取当前受限会话输入中的真实消息，多条回复仅第一条引用，并将引用关系写入永久事件账本。

### Plugin API v1

- 新增可独立安装的 `yuki_plugin_sdk` 和 Plugin API `1.0`，提供严格 Manifest、Feature Registry、Permission Catalog、生命周期 Protocol、声明型 Registrar、类型化结果与网络为空的测试 Fake。
- 新增插件发现、Manifest 哈希批准、名称冲突保护、通知 Event Bus、Prompt Fragment、PlannerSignal、Agent 工具、确定性命令、普通用户自动化 Action、配置 Schema、私有 KV 和托管后台服务扩展契约。
- 插件能力按“Manifest 声明 ∩ 管理员批准 ∩ 当前真实用户/群/来源 ∩ 本轮安全策略”计算；Planner、历史、网页、OCR、记忆、参数和用户自报不能扩大权限。图片轮次、联网后撤权和自动化委托继续收窄插件写能力。
- 新增绑定 `PluginContext` 的消息、人物、群、记忆、关系、LLM/Agent、联网、HTTP、视觉、媒体、自动化、配置、Secret、Storage、Scheduler 和 OneBot Facade Protocol；不向插件暴露 Container、Settings、主数据库、Repository、NoneBot Bot、原始事件、完整 Prompt 或隐藏推理。
- `ctx.agent.run()` 接入现有 `AgentRunner` 的只读工具后端；历史、人物记忆、群记忆与联网能力严格取 Manifest 批准交集，普通用户的历史和记忆查询自动锁定当前本人/群，不能借插件 Agent 扩大作用域。
- 插件 HTTP 对每次请求和重定向执行公网 DNS 复检与连接地址固定，跨源重定向丢弃调用方数据，并按 Manifest 限制并发；私有 KV 同样按 `storage_mb` 执行容量上限。
- 插件消息发送和 OneBot 读/写调用增加脱敏审计；仅成功发送写入 `chat_events`，媒体账本不保存文件 ID、签名 URL 或正文参数。持久化插件自动化 Action 在到期执行时恢复经过校验的创建者委托上下文。
- Plugin API v1 是本地可信代码的 API 治理，不是恶意 Python 沙盒；插件与 Yuki 同进程运行，默认 `PLUGIN_SYSTEM_ENABLED=false`，不提供在线下载、市场、热更新或第三方独立进程。

### 独立插件 AI 会话

- 新增 `ctx.agent_sessions.create/run/reset/close`，支持 `durable` 与 `ephemeral`、`none/current_user/current_group` 上下文策略、最长 8000 字符插件指令以及批准能力交集，适合骰子跑团、游戏主持和插件向导。
- 独立会话使用 `plugin-session:<plugin_id>:<uuid>` 并发键；历史只来自 `plugin_agent_messages`，不写 `chat_events`，默认不读取主聊天、人物记忆或关系，不向 SDK 返回或持久化 `reasoning_content`。
- Facade 固定真实插件、用户、群和批准权限，Agent 运行时始终不能伪造 `actor_is_superuser`；会话 UUID 不能跨插件或跨真实场景访问。

### 数据、配置与开发体验

- 新增非破坏性 Alembic `0013`：创建 `planner_runs`、插件安装/配置/KV/审计、独立 Agent 会话及消息表；保留 1.5.2 的人物、聊天、记忆、关系、联网、视觉、表情和自动化数据。
- 新增 `PLANNER_*`、`REPLY_SEQUENCE_CANCEL_ON_NEW_MESSAGE`、`REPLY_PLAN_HARD_MAX_MESSAGES` 与 `PLUGIN_*` 配置；Planner 热配置进入现有显式运行时注册表，插件目录/API 版本和系统开关按需重启。
- 新增 `docs/plugin-development/` 的 29 页完整手册与 API Reference，覆盖 10 分钟入门、Manifest、权限、生命周期、Facade、事件、Prompt、PlannerSignal、工具、命令、自动化、网络、视觉、测试、发布、兼容与真实安全边界。
- 新增无网络 `examples/plugins/com.example.echo`，演示普通工具、确定性命令、`reply.sent` Hook、`plugin_context`、普通用户自动化 Action、global/user/group 配置和私有 KV；GitHub Actions 增加示例插件契约测试。
- Docker Compose 新增只读 `./plugins:/app/plugins` 挂载；插件目录不进入运行镜像，也不会默认启用示例插件。
- 版本提升至 `1.6.0`，同步 README、`.env.example`、包版本、锁文件和升级说明。

## 1.5.2 - 2026-07-28

### 架构与上下文治理

- 将人物、群、关系和近期聊天上下文从 `ChatService` 抽取为独立 `ContextAssembler`，为动态人物资料与历史消息启用统一 `MAX_CONTEXT_CHARACTERS` 字符预算；当前消息始终保留，低优先级旧资料会先被裁剪。
- 相关人物的资料、个人记忆、成员群记忆和关系状态改为批量数据库查询，不再按人物逐项串行访问；读取相关人物关系时不再产生无意义的新关系记录。
- 将时间、权限、自动化、OneBot、联网、关系和视觉规则集中到 `PromptComposer`；正常聊天继续只使用同一个 Agent，没有增加管理员路由或隐藏会话。
- 删除已经脱离正式运行流程的旧群记忆提取服务、提及成员解析链路、兼容领域对象和 `GROUP_MEMORY_ENABLED`；现有三层记忆仍由持久化 `MemoryWorker` 维护。
- `ChatService` 改为显式接收账本、人物、记忆、关系、运行时配置和时间服务，不再通过其他服务的私有字段悄悄构造第二套依赖。
- SQLite 连接启用外键、WAL、5 秒 `busy_timeout` 和 `synchronous=NORMAL`，提高消息、记忆、关系、视觉与自动化 Worker 并发写入时的稳定性。
- 新增 GitHub Actions，自动执行 Ruff、严格 mypy、pytest、Alembic 全新安装和 Docker 构建；新增上下文预算、跨群名片批量查询、SQLite PRAGMA 和 `.env.example`/`Settings` 同步契约测试。

### 模块化与群聊触发

- 将原先超过 3,000 行的 `persistence.repositories` 按人物与访问、事件账本、记忆、关系、媒体和联网来源拆成独立仓储模块；原导入路径保留为显式兼容门面，数据库 schema 与现有数据不变。
- 将 `/ai` 确定性命令从 `MessageProcessor` 抽离，并继续按人物数据、运行时配置和自动化领域拆分处理器；没有新增第二 Agent、管理员人格或隐藏会话路由。
- 将运行时配置目录按热更新、仅影响未来、需重启和受保护/密钥四类拆分，`ConfigRegistry` 只保留键/别名查找和标量转换职责。
- 群聊中仅发送 `@Yuki`、不附带文字时现在会进入正常聊天 Agent 并自然回应；永久事件账本仍保留真实空文本，不会把后端占位描述伪造成用户原话。
- 修复 Bot 所在系统启动不足自主发言冷却时长时，第一次自主群聊被误判为仍在冷却的问题；只有实际自主发言过的群才会进入后续冷却判断。
- 增加仓储兼容导入、配置目录完整性、命令回归以及 OneBot 纯 `at` 消息归一化测试。

## 1.5.1 - 2026-07-27

- DeepSeek/兼容 Provider 在工具执行后返回空正文时，Agent 会保留已有工具结果并在同一轮最多重试 2 次，避免把成功的中间步骤误判成整轮失败。
- 取消“一轮只能执行一个修改或人物业务操作”的硬限制；同一轮现在可在工具总预算内顺序执行多个不同修改，仍会拦截参数完全相同的重复写入；自动化创建或修改失败时也由后端覆盖虚假的成功措辞。
- 新增原子管理员动作 `memory.prune`，可按人物、最大重要度和最短年龄批量删除非显式记忆，供每日清理自动化安全复用；显式记忆不受影响。
- 提高默认 LLM 超时、重试、输出 Token、聊天 Agent 与自动化任务的调用和运行预算，降低深度思考与复杂任务中的空回复和半途终止。
- 新增 `VISION_ALLOW_PRIVATE_URLS` 启动开关；TUN/Fake-IP 环境可显式解除图片 URL 的 SSRF 地址拦截，避免 QQ 图片域名被 `198.18.0.0/15` 代理地址误判；默认仍保持关闭。
- 系统提示词改为任何场景都尽量减少 Emoji，表达情绪时优先使用简短、自然的颜文字。

## 1.5.0 - 2026-07-27

### 可信时间与持久化自动化

- 新增 `TimeContextService`：每轮聊天注入后端可信的 UTC、本地时间、IANA 时区、日期和星期，历史事件使用当前用户时区显示简短时间戳；`person_time_settings` 持久保存每个 QQ 的时区。
- 新增严格的 Automation DSL v1，支持 `after`、`once`、`daily`、`weekly`、`interval` 五类触发器，以及顺序步骤、受限模板变量、结构化步骤输出和每次运行硬限制；不执行 Python、Shell、JavaScript、SQL、文件或任意 HTTP。
- 新增显式 `AutomationCapabilityRegistry`，首批登记 Yuki 生成/Agent、OneBot 主动发送及通用 action、管理员业务 action、运行时配置、联网、人物/群记忆和历史搜索能力。
- 按新增需求向普通用户开放自动化工具：普通用户只能创建和管理自己的任务，并只能委托本人私聊、当前真实群、生成及安全只读能力；超级管理员可额外委托管理员、配置和全部公开 OneBot action。
- 创建时保存最小 `DelegatedAuthority` 快照；执行能力取“创建时授予 ∩ 当前仍登记且版本一致 ∩ 当前权限仍允许”。超级管理员资格撤销、能力删除或 Schema 版本变化都会阻止旧任务执行，后端新增能力不会自动进入旧任务。

### 调度、执行与审计

- 新增非破坏性 Alembic `0012`：创建 `automations`、`automation_versions`、`automation_runs`、`automation_step_runs` 和 `person_time_settings`，并为 `chat_events` 增加自动化来源字段；保留现有人物、聊天、记忆、关系、联网、视觉和表情数据。
- 新增数据库租约驱动的 `AutomationWorker`，支持重启恢复、双 Worker 竞争防重、`automation_id + scheduled_for` 幂等、关闭等待、Bot 断线宽限、misfire 跳过和周期任务无补发风暴。
- 新增 `AutomationExecutor` 与 `OneBotProactiveGateway`：按真实 `bot_user_id` 选择连接，成功发送写回永久事件账本并标记 `scheduled_automation`；无法确认是否发送成功时标记 `uncertain` 且不自动重发。
- 可重试的生成、Agent、联网、记忆与历史读取只对明确瞬时错误最多重试一次；发送、通用 OneBot、配置修改和管理员操作默认不重试。连续失败达到阈值后任务进入 `failed`，可修改后恢复。
- 创建、修改、暂停、恢复、取消和手动运行共用 `AutomationService` 并写脱敏审计；运行与步骤记录只保存计数和摘要，不保存密钥、隐藏推理、网页正文、图片 Base64 或完整 OneBot 返回。

### Agent、命令、配置与质量

- 普通文本聊天 Agent 新增 `automation_create/list/get/update/pause/resume/cancel/run_now/history` 与 `time_get_current/get_timezone/set_timezone` 工具；图片、OCR、网页、引用、历史和模型生成文本都不能授予或扩大权限。
- 新增 `/ai automation list|show|pause|resume|cancel|run|history`，所有用户只能操作自己创建的任务；`/ai status` 和 `/healthz` 增加自动化开关、Worker、活跃数和最近/下一次运行状态。
- 新增 15 项 `AUTOMATION_*`/`DEFAULT_TIMEZONE` 启动配置及 14 项显式运行时配置；任务自身禁止修改 `automation.*` 硬限制。
- 抽取可复用的有界 `AgentRunner` 供计划任务中的 Yuki Agent 使用，并保留普通聊天现有工具次数、管理员结果校验和联网后撤销修改能力的规则。
- 自动化列表不再直接展示数据库 UTC：按用户时区（默认 `Asia/Shanghai`）输出本地时间；当前任务与已结束历史分队列展示，当前编号始终从 1 重新排列，底层稳定 ID 仅供后端操作。
- 增加模型历史时间标记的输出清理，并明确普通提醒应使用 `onebot.send_private_message`/`onebot.send_group_message`，避免把自动化主动发送与聊天轮 `call_onebot_api` 混淆。
- 版本提升至 `1.5.0`，同步 `.env.example`、README、系统提示词示例、能力目录、迁移与自动化回归测试。

## 1.4.2 - 2026-07-27

### 持久化 QQ 表情描述库

- 新增非破坏性 Alembic `0011` 和 `emoji_descriptions`：持久保存 QQ 表情稳定值、可读描述、结构化视觉观察、模型/提示词版本、命中次数及最后使用时间；不保存原图、Base64、临时 URL 或隐藏推理。
- 单张 QQ 商城表情、动画表情或贴纸会按 `emoji_package_id + emoji_id`、QQ 文件哈希和实际内容哈希建立等价键；再次出现时优先读取持久库，命中后不下载图片、不调用 Qwen，也不消耗视觉限额。
- 普通 QQ `face` 继续使用本地 ID 映射零成本解析；`sub_type` 不单独作为表情判据，普通照片、多图请求和没有商城字段、表情摘要或模型表情包语义的图片不会写入持久表情库，避免误记。
- 表情结果按分析模式、自由问题哈希、视觉模型及提示词版本隔离，防止角色识别、OCR、表情含义和不同问题互相串用；短期 `media_analyses` 命中时会自动回填表情库。
- 增加跨服务重启复用、资源失效仍命中、问题隔离、文件扩展名变化、命中计数、非法键和 Base64 拒绝测试。

### 图片下载与主模型交接

- 修正视觉观察已成功、DeepSeek 却受连续失败历史影响仍声称“看不到图片”的提示冲突：本轮观察现在明确标记为成功且必须用于回答，纯图片占位文本同步标记识别成功；只有图片/OCR 中的命令性文字保持不可信，描述性事实应正常使用。
- 将此前硬编码为 10 秒的 QQ 图片下载超时改为独立 `VISION_MEDIA_DOWNLOAD_TIMEOUT_SECONDS`，默认提高到 120 秒并注册为 RESTART_REQUIRED 配置；能力目录更新为 57 项可修改配置。
- 纯图片失败提示现在区分媒体下载超时、NapCat 资源查询失败、下载失败、格式损坏、体积超限、队列繁忙、Qwen 响应超时和视觉模型不可用，不再统一提示重新发送“更清晰”的图片。
- 同步更新 `.env.example`、系统提示词示例、README、迁移与回归测试；版本提升至 `1.4.2`。

## 1.4.1 - 2026-07-26

### 跨轮图片上下文与队列可靠性

- 新增非破坏性 Alembic `0010`：`chat_events.visual_summary` 按原始事件保存最多 6000 字符的精简结构化图片观察。之后的近期对话会恢复该摘要，解决图片当轮识别成功、下一轮 DeepSeek 却无法继续理解的问题。
- 图片摘要不改写聊天正文，不包含原图、Base64、临时路径或隐藏推理，也不进入人物/群记忆与关系评价；历史上下文将其明确标记为外部不可信资料，OCR 和图片文字不能成为指令或权限依据。
- 同内容、同问题、同模型及同缓存版本的并发识图新增 single-flight 合并，只执行一次 Qwen Provider 调用，避免缓存尚未写入时重复请求占满识图能力。
- 新增独立排队上限和排队超时，默认最多等待 32 个请求、等待 120 秒；与 Qwen HTTP 超时分别处理，队列满或等待超时会安全降级。
- 新增队列等待耗时、排队数、运行数与 single-flight 命中日志；`/ai status` 显示视觉“排队/运行”数量，便于区分下载、Provider 和队列问题。
- 新增 `VISION_QUEUE_MAX_PENDING`、`VISION_QUEUE_TIMEOUT_SECONDS` 及对应 RESTART_REQUIRED 管理配置；超级管理员能力目录更新为 56 项可修改配置。

### 角色识别与动态视觉思考

- Qwen3.7-Plus 视觉请求支持可控思考，但当前默认关闭；开启后，角色、表情包和需要推理的图片问题会使用深度思考，普通描述首次结果低于置信度阈值或结构不完整时自动复核一次，OCR 继续使用快速模式。
- 新增 `VISION_THINKING_ENABLED`、`VISION_THINKING_BUDGET` 和 `VISION_LOW_CONFIDENCE_RETRY_THRESHOLD`，当前默认分别为 `false`、`6144` 和 `0.65`；对应运行时 HOT 配置可由超级管理员自然语言调整。关闭思考时不会触发低置信度二次思考请求。
- 视觉结构新增高置信度角色名、作品来源以及最多三个候选角色、视觉依据和候选置信度。视觉提示词明确允许识别动漫、游戏、影视、吉祥物、虚拟人物与网络表情角色，同时继续禁止猜测现实人物身份。
- 新增 `character` 分析模式；“这是谁”“什么角色”“来自哪部作品”等问题会优先进入角色识别流程，开启思考时使用对应的深度推理，表情包仍同时分析角色、情绪、动作、梗意和使用语境。
- 视觉任务与 JSON 结构要求移至用户任务消息，system message 只保留不可信图片与权限边界；低置信度复核失败时保留首次可用结果，不影响正常聊天。
- 视觉提示词缓存版本升级至 `vision-observation-v3`，并把思考开关、预算和复核阈值加入缓存变体，避免继续复用 1.4.0 的旧识别结果；旧缓存无需手动删除。
- 为减少多图、动态表情和高分辨率图片被截断，采用高容量默认值：最多 5 张图、16 总帧、每个动图 8 帧、20 MB 下载、16 MB 预处理、4096 边长、16777216 像素、120 秒超时、8192 输出 Token、6144 思考预算、4 路视觉并发，并提高用户与群视觉频率上限。
- 新增动态思考、低置信度二次复核、角色候选解析、角色模式路由、缓存隔离和 DeepSeek 结构注入测试；版本提升至 `1.4.1`。

## 1.4.0 - 2026-07-26

### 双模型图片理解

- 新增独立视觉前端：Qwen3.7-Plus 通过阿里云百炼 OpenAI-compatible Chat Completions 接口分析图片，DeepSeek 继续负责 Yuki 人格、上下文、记忆、关系、Agent 工具和最终 QQ 回复；DeepSeek 不接收图片 URL、Base64 或临时路径。
- 新增图片、图片表情、动态 GIF/WEBP 和回复图片理解。当前消息图片优先于回复图片，多图保持消息段顺序，默认每轮最多 3 张并合并为一次视觉请求。
- 扩展 OneBot 标准化，保留图片的 `file`、`url`、`summary`、`sub_type`、`file_size`、`key`、`emoji_id`、`emoji_package_id` 和消息段位置；回复消息同步解析附件与原始消息段。
- 新增 `QQFaceResolver` 和 `config/qq_face_map.json`：QQ 内置 `face` 转为可读文本，未知 ID 保留原 ID；Unicode Emoji 继续作为普通文本，不调用视觉 API。
- 私聊纯图片可进入正常聊天；图片加文字会把真实用户文字作为视觉问题。群聊仅在原有触发条件成立时分析图片，未触发图片和自主群聊批次不会下载或分析。

### 媒体安全与视觉预处理

- 新增 `MediaResolver`，只接受当前真实 OneBot 事件、真实回复图片或对应 NapCat `get_image` 返回的资源；拒绝模型、OCR、记忆和网页提供的任意下载 URL。
- HTTP(S) 下载新增凭据 URL、localhost、回环、私有、链路本地和保留地址防护；DNS 解析及每次重定向后重新校验，限制 3 次重定向、总超时和流式读取字节数，并安全处理非法 Base64。
- 新增基于 Pillow 的 `ImagePreprocessor`，按实际文件内容识别 JPEG、PNG、WEBP、GIF 和动态 WEBP，执行 EXIF 方向修正、尺寸/像素限制、透明通道处理、等比缩放与 JPEG/PNG data URI 转换。
- 动态图片默认最多按时间顺序抽取 4 个关键帧，单轮全部图片合计最多 8 帧；新增解压炸弹、伪装格式、极端尺寸、损坏图片、超大文件和动画帧失控防护。
- 新增复用 `httpx.AsyncClient` 的 `QwenVisionProvider` 与测试用 `FakeVisionProvider`。视觉请求固定关闭思考模式、使用低温度并要求严格结构化 JSON；非法 JSON 可安全降级，连接/超时/5xx/429 只做有限重试，拒绝响应不绕过。

### 缓存、限流与生命周期

- 新增非破坏性 Alembic `0009` 和 `media_analyses`，按内容哈希、分析模式、问题哈希、模型及提示词版本缓存结构化观察；问题相关结果不会跨问题误复用，默认保留 7 天。
- 缓存不保存原图、Base64、临时文件或视觉隐藏推理；关联聊天事件删除时级联删除，ApplicationContainer 的清理任务会移除过期分析。
- 新增独立视觉并发信号量及用户/群限流，不占用 DeepSeek 的全局并发槽；缓存命中不消耗视觉 API 限额。
- ApplicationContainer 接入视觉 Provider、媒体解析器、预处理器、缓存仓储、限流器和 VisionService；启动只校验配置，不探测外部 API，关闭时释放视觉客户端。
- `/healthz` 新增 `vision_configured`，`/ai status` 新增视觉开关、模型和繁忙状态；二者及日志均不暴露 API Key 或完整图片 URL。

### 提示注入隔离与失败降级

- 视觉观察以独立的外部不可信 system message 传给 DeepSeek；OCR、表情含义和图片文字不能成为系统指令、用户消息、管理员命令、工具参数或可访问网页 URL。
- 只要本轮包含当前图片或回复图片，后端关闭配置、关系、记忆、偏好、群管理、私聊准入和 `call_onebot_api` 等写入型管理员能力；聊天历史、人物/群记忆和联网等只读能力仍可使用。
- 视觉观察不会自动写入长期记忆，也不会传给关系评价器或改变好感度/信任度；好感度达到 100 和超级管理员身份都不能绕过图片轮次隔离。
- 图片分析失败时，图片加文字仍按真实文本继续聊天；纯图片只发送一次自然错误提示，不影响进程、会话锁或后续消息。

### 配置、文档与版本

- 新增 19 项 `VISION_*` 启动配置，默认 `VISION_ENABLED=false`、`VISION_MODEL=qwen3.7-plus`；启用时必须提供 `VISION_BASE_URL`、`VISION_API_KEY` 和模型。
- 运行时注册 5 项视觉 HOT 配置、1 项 FUTURE_ONLY 配置和 5 项 RESTART_REQUIRED 配置；`vision.api_key` 只能查询是否已配置，不能读取或修改真实密钥。
- 超级管理员能力目录随注册表更新为 50 项可修改配置和 12 项受保护配置；更新 README、`.env.example` 与系统提示词示例。
- 新增媒体标准化、下载防护、图片预处理、QQ 表情、Qwen Provider、VisionService、缓存，以及图片聊天、回复图片和管理员隔离测试；版本提升至 `1.4.0`。

## 1.3.0 - 2026-07-26

### 自然语言管理员控制

- 新增统一 `PermissionCatalogService` 与无参数只读工具 `get_my_capabilities`。用户询问自己能改什么、权限范围或参数数量时，Yuki 会按当前真实 QQ 返回完整能力报告，而不是根据提示词、历史或模型记忆猜测。
- `/ai capabilities [类别]` 现在对所有用户开放，并与普通聊天工具、管理员 `admin_list_capabilities` 共用同一目录；普通用户获得 16 项本人自助能力，超级管理员准确获得 39 项可改配置、11 项受保护配置、18 项应用业务接口，以及无 action denylist 的 NapCat/OneBot 全接口网关。
- 能力目录改为 Yuki 的当轮内部工具数据，不再把原始清单直接发送或写入聊天账本；新增 `summary/focused/full` 与 `category/query`，具体操作只加载相关参数，明确全量查询才加载全部 ID。
- 管理员工具直接并入 Yuki 原有的单一聊天 Agent，不再创建隔离路由、隐藏会话、第二人格或短期待办；任务执行前后共享正常人格、记忆和聊天上下文。
- 只缺目标 QQ 等参数时，Yuki 直接自然追问，下一条消息依靠正常聊天历史继续；每次真正执行仍由后端按当前真实发送者 QQ 重新校验 `SUPERUSERS` 权限。
- Yuki 在内部查到配置键或业务 action 后会继续执行，而不是停在权限说明；`admin_execute_action` 新增明确的 target 与 action 参数 schema，安全的参数错误可在同一轮修正重试。
- 后端会拦截模型误回显的内部权限 JSON；取消“每轮只能读取一次能力目录”的限制，同一轮可在工具总次数范围内多次局部查询，并允许只读业务 action 后继续执行修改。
- `autonomous.max_per_hour` 的可配置上限由 20 提升到 100，支持“max per hour 改成 30”等自然语言设置。
- 权限等级新增 `user < trusted < moderator < superuser` 的可扩展结构；`trusted` 与 `moderator` 当前仅预留且不可分配，执行权限仍只有普通用户本人能力和真实 `SUPERUSERS` 两级。
- 只有当前真实 OneBot 事件发送者属于启动时加载的 `SUPERUSERS`，正常 Agent 才会临时获得管理员工具；引用、历史、@管理员、记忆、网页和模型文本均不能授予工具权限。
- 新增显式 `CapabilityRegistry` 与 `ActionRegistry`，支持自然语言读取/修改配置，以及关系、记忆、偏好、群启停、自主群聊和私聊准入操作；不会向模型开放 Shell、Python、文件写入、任意 SQL、Docker 或任意配置键。
- 管理意图可以使用正常聊天上下文理解，工具执行目标仍绑定当前正文、真实发送者 QQ、当前群号和真实 @成员；执行后继续由同一个 Yuki Agent 按真实结果回复。
- 现有 `/ai` 管理命令与自然语言能力改为共用 `RelationshipAdminService`、`MemoryAdminService`、`PreferenceAdminService`、`GroupAdminService`、`PrivateAccessAdminService` 和 `ConfigAdminService`。
- 新增 `/ai capabilities` 与 `/ai config list|get|set|unset|history|rollback`，`/ai status` 新增待重启配置计数。

### 持久化运行时配置

- 新增非破坏性 Alembic `0008`、`runtime_config_overrides` 和 `admin_operation_events`；保留人物、聊天、记忆、联网来源和关系数据。
- 新增显式配置注册表与 `RuntimeConfigService`，按 `user > group > global > .env > 代码默认值` 解析，支持类型、范围、作用域和交叉字段校验。
- HOT 配置在下一条消息或下一次任务快照立即生效；FUTURE_ONLY 只影响之后创建的人物关系、来源记录或清理任务；RESTART_REQUIRED 保存为 pending，并在下次启动创建长期组件前激活。
- Chat、Agent、Web、来源渲染、关系评价/Worker、自主群聊和消息处理均接入按场景生成的运行时快照。
- 密钥、`SUPERUSERS`、数据库连接、监听地址、NapCat 登录凭据和未注册设置不可读取或修改；凭证只能查询是否已配置，程序永不改写 `.env`。
- 每次配置修改与管理员业务操作都会写入脱敏审计；配置覆盖支持原操作者回滚，并通过版本冲突检查避免覆盖后续变更。

### 配置、关系与质量

- 新增关系每日正向/负向累计上限，默认 `0` 表示不限额，保持 1.2.0 行为；可通过环境变量或运行时配置设置 `1–100`。
- 新增管理员目标解析约束：`self`、当前群、真实 @成员，以及必须在当前正文明确出现的 QQ/群号；模型无法凭空构造目标。
- `/ai forgetme` 会同步删除该 QQ 的用户级运行时配置覆盖，并在保留的配置/管理员审计中脱敏精确 QQ。
- 版本提升至 `1.3.0`，同步 README、`.env.example`、系统提示词示例、命令帮助和完整回归测试。

## 1.2.0 - 2026-07-25

### 持久化关系系统

- 新增 `person_relationships`，以 QQ 为主键独立保存 `0–100` 的好感度与信任度；新人物默认均为 `50`。
- 新增 `relationship_events` 审计自动与管理员手动变化，不重复保存聊天正文；同一聊天事件最多自动评价一次。
- 新增可在重启后继续处理的 `relationship_jobs`，默认每 60 秒或累计 5 条唤醒，单批最多 10 个会话，失败最多重试 3 次。
- 新增非破坏性 Alembic `0007`，保留现有人物、聊天、记忆和联网来源，并为现有人物补建默认关系。
- 自动单次变化仍限制在 `-2～2`，总分仍限制在 `0～100`；按当前需求不设置每日累计增加或降低上限。

### 评价、上下文与关系风格

- 新增 `RelationshipStage`、有效信任度和关系权重纯函数，以及 `GUARDED`、`DISTANT`、`FRIENDLY`、`CLOSE`、`AFFECTIONATE`、`BONDED` 六个固定阶段。
- 新增独立关系评价器：`temperature=0.1`、关闭思考、不开放工具，每个任务最多读取当前人物最近 5 条相关事件。
- 置信度低于 `0.75`、越界输出、未知原因、伪造 JSON 和要求直接加分的文本均由后端中和。
- 当前人物及最多 5 位相关人物的关系状态进入模型上下文，并由独立可信 system message 注入当前场景风格和多人信息冲突规则。
- 客观证据始终优先；只有无证据且关系权重差至少 `15` 时才倾向较高者，群聊不得公开其他人物的具体分数。
- 好感度不参与 `SUPERUSERS` 或工具权限判断；达到 `100` 的普通用户仍不能获得通用 OneBot 管理工具。

### 命令、提示词与配置

- 新增 `/ai affection show|history`。
- 新增超级管理员命令 `/ai affection show|history|set|adjust|trust user <QQ号> ...`，所有修改均记录 actor QQ。
- `/ai forgetme` 通过外键级联删除关系状态、事件和任务。
- 更新 Yuki 系统提示词：固定为 18 岁成年少女，根据后端关系阶段调整语气，工作请求优先，亲密交流不改变工具权限。
- 新增关系 Worker、初始分数、置信度、单次变化、信任上限偏移和冲突权重差配置，并同步 `.env.example` 与本地 `.env`。
- 版本提升至 `1.2.0`，更新 README、命令帮助及关系系统回归测试。

## 1.1.0 - 2026-07-25

### 受控联网搜索

- 新增 Tavily REST `web_search` 和 `read_webpage` Agent 工具，不引入爬虫、浏览器自动化或额外框架。
- `web_search` 自动完成候选搜索和最多 3 个网页的批量正文提取；Tavily 不生成最终答案，仍由当前 LLM 总结。
- `read_webpage` 只允许读取用户明确发送或本轮搜索真实返回的公开 HTTP(S) URL。
- 新增查询长度、URL、公网地址、超时、429、5xx、非法 JSON、部分提取失败和工具结果长度限制。
- 每轮最多 3 次联网工具调用；网页内容进入工具上下文后，本轮撤销 OneBot 管理工具。
- 网页标题、摘要和正文被标记为外部不可信数据，不写入人物记忆、群记忆或普通聊天账本。

### 来源显示与持久化

- 新增后端 `SourceDisplayPolicy`，识别明确来源请求、否定表达和“来源呢”等短追问。
- 默认隐藏来源、URL、引用编号和模型生成的末尾来源段落；与真实来源无关的普通链接保留。
- 用户明确索要时，由后端把数据库中真实来源渲染为独立 QQ 消息，模型不能拼接或编造链接。
- 短追问直接返回当前隔离会话最近一次来源，不再次调用 LLM 或 Tavily。
- 新增 `web_search_runs`、`web_search_sources` 和 Alembic `0006`；不保存网页正文。
- 来源按 `ConversationIdentity.key` 隔离，每会话最多 10 次，默认清理 7 天前记录。

### 配置、文档与质量

- 新增 `WEB_ENABLED`、`TAVILY_API_KEY` 和搜索、并发、重试、保留期配置。
- `/healthz` 新增不发起外部请求的 `web_configured` 字段。
- 版本提升至 `1.1.0`，更新默认系统提示词、`.env.example` 和 README。
- 新增来源策略、渲染、Tavily MockTransport、Agent 工具、持久化及端到端测试。

## 1.0.0 - 2026-07-25

### 破坏性变更

- 新增不可逆 Alembic `0005`：删除 1.0 之前的会话、资料、权限和记忆数据，重建人物中心 schema。
- QQ 号字符串成为人物的全局唯一身份；`SUPERUSERS` 环境变量成为唯一管理员来源。
- 所有私聊默认准入，`/ai private <QQ> off|on` 改为阻止/恢复指定用户。
- 已启用群开始观察并永久保存未触发消息；禁用群只处理超级管理员启用命令。
- `/ai new` 改为写上下文切点，不再删除历史。
- `/ai forgetme` 改为彻底删除当前 QQ 的人物、记忆、成员关系和可归属聊天事件。

### 人物、事件与记忆

- 新增 `people`、`person_aliases`、`groups`、`memberships`。
- 新增永久 `chat_events` 事件账本，保存机器人账号、消息 ID、QQ、群号、方向、文本、消息段 JSON、回复关系和时间。
- 新增 FTS5 `trigram` 索引；三字及以上使用全文检索，短词使用带范围限制的 `LIKE`。
- 新增 `person_memories`、`group_memories`、`person_group_memories` 和 `person_preferences`。
- 新增持久化 `memory_jobs`，每 30 秒或累计 10 条批量提炼，单批最多 20 条，失败最多重试 3 次。
- 回答上下文支持当前人物跨私聊/群聊记忆、当前群记忆、成员群记忆和最多 5 位相关人物。
- 保留图片、表情、语音、视频、文件和转发消息段元数据；本版不下载媒体、不接视觉模型。

### QQ Agent

- 扩展 Chat Completions 类型：`tools`、`tool_choice`、`tool_calls`、`tool_call_id`、`reasoning_content`。
- 支持 DeepSeek 普通和思考模式多轮工具调用；思考模式中间轮原样回传 `reasoning_content`。
- 每轮最多 5 次工具、6 次模型请求；未知工具、无效 JSON 和超限循环返回工具错误。
- 新增 `get_recent_chat_history`：每次直接调用 NapCat 当前私聊/群历史接口，并把未见消息补入账本。
- 新增 `search_chat_history`、`get_person_memories`、`get_group_memories`。
- 当前直接消息发送者属于 `SUPERUSERS` 时开放 `call_onebot_api(action, params)`，可调用全部 OneBot action，无 denylist 和二次确认。
- 通用 OneBot 调用新增最小审计，不在普通日志保存完整工具结果。

### 群聊自主参与

- 新增 8 秒静默批次和最多 20 条候选上下文。
- 仅对回复机器人、提到机器人、群提问或记忆相关内容进入模型参与判断。
- 默认置信度阈值 `0.85`、冷却 300 秒、每小时最多 3 次。
- 两次自主发言之间必须有新的人类消息；自主判断轮不开放通用 OneBot 工具。
- 保留普通消息发送、日常分句和 3–5 秒随机间隔。

### 命令

- 新增 `/ai memory list|add|update|delete`。
- 新增 `/ai preference list|set|delete`。
- 超级管理员可在操作名后增加 `user <QQ号>` 管理任意人物。
- `/ai whoami` 新增已知别名、人物记忆数和群成员关系数。
- 命令处理与 AI 工具共用业务仓储，不通过模型生成 `/ai ...` 再回灌。

### 文档与质量

- README 开头提供启动命令和 1.0 数据清空警告。
- 更新 `.env.example`、架构、命令、Agent 工具、配置、部署与升级说明。
- 新增人物、账本、FTS、记忆、工具权限、删除和破坏性迁移测试。

## 0.1.0 - 2026-07-23

- 初始 Python 3.12、NoneBot2、OneBot v11、NapCatQQ、SQLite 和 OpenAI-compatible LLM 项目。
- 支持私聊、群聊触发、管理员开关、SQLite 会话、身份资料、群记忆、分句发送和 Docker Compose 部署。
