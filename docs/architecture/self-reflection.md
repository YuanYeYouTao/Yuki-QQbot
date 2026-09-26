# Self Reflection 执行与积压治理

Self Reflection 使用 canonical owner、内部事件范围与持久 run ID；不使用平台消息 ID
登记工作。schema 0061 增加 cycle、实际请求账本、源批次重试和恢复检查点。
SELF 自主证据扩展已有定向验证；T20 真实 QQ 社交效果仍须单独验收。
迁移顺序为 `0065`（关系历史索引）→ `0066`（autonomy 接纳）→
`0067`（SELF 工具来源与自省水位）→ `0068`（内部引用事件）→
`0069`（Social 回执内部事件关联）→ `0070`（无来源机会与讨论线程）。
实际线上版本以当前容器镜像与数据库迁移记录为准。

## 结构化生成

`memory_self_reflection` 使用独立模型任务与结构化输出合同；现有 DeepSeek Responses
配置方案为 `json_schema`、low reasoning、180 秒、32768 输出 tokens。实际生产 profile
须核对部署配置，本文不替代该检查；其他模型任务与主 Agent 工具声明不因此变化。
Responses 通用适配器将 Chat 风格的嵌套 schema 展开为 `text.format`。
任务可显式绑定支持 JSON Schema 的 Chat、Claude 或 Gemini Profile，仍使用同一范围、
mutation、请求预算和恢复合同；协议转换见[模型供应商合同](model-providers.md)。

返回值须完整、零工具调用、单个 JSON object，并通过 Pydantic、引用、范围、所有权与
原有 mutation 校验。截断或输出达到预算时报 `output_budget_exhausted`，不自动加预算。
稳定且不含具体人物隐私的 SELF fact、preference、reflection 和 principle 可使用 global；
私聊产生的 SELF fact 保持 current scope，Episode 永不进入 global。
配置显式允许时，只接受 Provider `unsupported_json_schema` / `json_schema_not_supported`
错误码触发一次严格 text JSON 降级；普通 400、格式错误和校验失败不触发降级。

## 调度与恢复

固定 04/12/20 点调度。默认每轮 32 批、每 owner 16 批、每批最多 200 事件/16000
字符。批次按实际渲染后的事件文本计数，实际事件数可能少于 200；不能在生成前丢弃尾部
事件后再把整批标为完成。前台可以抢占后台自省；抢占保留批次，不消耗失败隔离次数。

每日 96 次限额在实际 HTTP 调用前原子登记，含校验修复和传输重试；进程重启不重置。
迁移保留旧 model invocation 账本已有记录，旧版本没有记录的额外 HTTP 重试无法追溯。

排空默认关闭，用于分阶段上线。启用后 >=500 actionable events 每 10 分钟增加后台周期，
>=1000 告警，<100 退出。持久 cycle 防止同一时间槽重复执行，进程锁使 worker 串行。
同一 cycle 内失败批次不再次领取。按 owner 轮转分配份额；一个失败范围不阻挡其他 owner 或该 owner 后续独立范围。

失败范围按原 run ID、first/last 内部事件和指纹恢复，退避 5/15 分钟；第三次失败隔离，
记录 30 分钟边界但不自动重新接纳。变更后的源范围拒绝重放。连续检查点不会跨过失败
空洞，后续成功范围独立保存、不会重复生成。已验证输入和输出保存到有界检查点，
完成提交后释放输入输出快照，保留源范围和 mutation 回执；失败快照继续保留。
逐项 mutation 回执避免重放；只有完整执行检查点才允许把新批次恢复为 completed。
历史版本无检查点的已提交批次沿用其原回执恢复语义，不能伪称能恢复旧版未保存的输出。
mutation 的正常拒绝、重复、低价值跳过与 `no_change` 都是已处理终态，不把整批标为失败；
数据库、事务、检查点和未预期代码异常才重试。consolidation 只修改完全相同的持久化目标；
更宽范围的完全同键、同 kind、同正文 SELF fact 可覆盖窄范围写入，但不得追加局部 evidence。

无自身回复或可信工具证据的到期范围不调用模型，不写记忆；记录 `no_self_evidence`
并推进自省投影。原始事件账本不变。

## SELF 自主执行证据

schema 0067 允许工具回执由内部 `trigger_event_id` 或可信 `initiative_run_id` 单独归属，
两者严格互斥。自主执行从 run 的 canonical Conversation、Space、Presence 解析来源，
`target_person_id` 不授予任何私人 Memory 权限。执行回执按 run、execution、Provider、
tool 和真实 tool call ID 去重；没有聊天发言也保留实际成功或失败结果，不制造聊天事件。

每个已结束 initiative 有独立 receipt 水位；窗口最多 8 条回执，保持原有输入字符边界。
迟到回执进入该 run 的后续窗口，不受其他 run 进度影响。窗口复用原 SelfReflectionRun、
cycle、每日请求预算、检查点、退避与隔离机制；管理输出以 receipt 范围显示，不将它们
统计成聊天事件。已领取未完成窗口保护其源回执，完成后仍被 Memory evidence 引用的
回执不会过期删除。未讲话的失败也可以参与判断，但仅能支持真实失败或尝试的经历。

主 SELF 入口使用 actorless TurnMemorySession，自动读取当前群与该群可见的 SELF 资料，
不借最近发言者或目标人的身份准备上下文。纯工具自省经同一 MutationService 验证、
冲突处理和原子回执写入；只能影响有相应证据的 SELF global/当前群范围，不能写私人事实。
历史 run 证据可保留，不能因此恢复已失效 generation 的执行权限。

主生成仍复用同一 MainAgentTurnService、Runner 和固定完整工具声明。资料与 SELF run
位于动态输入中，恢复读取原 journal；不把恢复时新检索到的记忆登记成旧投影已经曝光。
子 Agent 保留原 SELF principal 与 execution ID，回执由原 run 归属，不冒充新的真人事件。
聊天事件水位、自主回执水位和参与控制器反馈序列各有用途，不能相互替代。

可选 `<yuki-state>` 是主 SELF 的内部状态自报，在可见发送及语音正文前剥离；不会生成
聊天记录或充当其他人意图的证据。自省仍依实际事件、工具尝试和回执做判断，不能把
自报、proposal 或模型说“做过了”当作操作成功证明。

## 管理与健康

超级管理员 `/ai memory self-reflection run` 以当前内部事件登记后台 manual cycle，
立即返回 ID、积压和边界；重复事件复用原 cycle。`status [run_id] [页码]` 只读查询。
开始和结束报告只有数量、时间、失败类别、事件范围与预算，不包含源正文、reasoning。
最终报告复用 Social 的 prepared/succeeded/uncertain 回执。同一 cycle 的固定调用键不重发；
传输结果未知时标记 unknown，不能声称端到端 exactly-once，也不能猜测失败后重新发送。

健康区分 actionable、waiting_retry、isolated、policy_ineligible、recent_not_due、processing。
后者计数可以在同一 owner 不同范围出现；不能把各组会话数相加当作去重会话数。
已跳过且没有新消息的 policy-ineligible owner 显示 0 pending events。
失败详情按 5 项分页，其他原始内容不进入报告。`retry <batch_id>` 可由超级管理员
重新接纳隔离批次，保留原 ID、尝试次数和总额度。健康快照按最近 24 小时内周期提供
实际流入/排出速率，观察不足 60 秒时为未知；连续三轮积压未下降会告警。
配置见 `.env.example`。

管理员可用 `/ai config set memory.self_reflection_drain_enabled false` 关闭额外排空，
用 `true` 开启，`/ai config get memory.self_reflection_drain_enabled` 查询。
配置持久化且需要重建/重启 Bot 才生效，不终止当前周期，不关闭固定调度和失败重试。

## 部署

先备份并在副本验证当前迁移链，再只替换 Bot；首次保持 drain=false，完成真实 manual 验证后
开启。回滚先关闭 drain，保留新表、预算与 mutation/发送回执，不恢复旧数据库覆盖新记忆。
这是后续部署顺序，不是本轮已执行操作。本地迁移与回执测试不能替代真实 QQ 验收；
独立参与库的 Jev 合成 smoke 也不能证明自省记忆质量或独立人工语义准确率。
