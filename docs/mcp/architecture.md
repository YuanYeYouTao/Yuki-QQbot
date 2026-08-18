# MCP 架构

MCP 是 Tool Kernel 的一个 Provider，不是第二套 Agent。配置、连接、缓存、目录、Binding、结果
归一化和运维命令分别由 `mcp/config.py`、`connection.py`、`manager.py`、`provider.py`、
`binding.py`、`result_normalizer.py` 和 `admin.py` 负责。

配置启用的 Server 被视为可信工具来源，不需要 MCP Tool 逐项审批；远程返回内容仍是外部资料，
不会授予权限。MCP Tool 与 Plugin Tool 使用同一个 Capability Runtime、能力策略、AgentRunner、调用协调器和
结果预算器。

持久化自动化通过 `mcp/automation.py` 的通用桥接层接入。只有 Server 配置中
`yuki.automation.includeTools` 明确列出的远端工具才会注册为
`mcp.<server_id>.<remote_tool_name>`；权限、风险、重试、JSON Schema 和输出 Artifact 随
注册定义进入原有 `AutomationCapabilityRegistry`。直接 DSL 步骤与 `yuki.agent` 使用同一个
Binding 路径，不存在按品牌编写的执行分支。

自然语言创建任务时不会把远端名称直接交给模型拼写。`AutomationCompiler` 在 TaskSpec Schema
中提供模型安全 ID，兼容连字符、下划线和点号差异，再解析为注册表中的真实名称；最终委托快照
只保存本任务明确选择的能力。底层 DSL 仍供插件 SDK 与内部调用使用。

自动化委托快照保存远端工具的完整元数据哈希。`tools/list_changed`、手工 refresh 或重连刷新
目录后，桥接层会原子替换该 Server 的动态定义；Schema 改变时旧快照不再匹配，禁用 Server 或
删除允许项时定义会消失。两种情况都会由既有执行器阻止旧任务，而不是把新能力自动补授给它。

普通聊天没有命中 MCP 工具时不注入 MCP Schema，也不会为了健康检查连接 lazy Server。

Gateway 不拥有目标工具的风险。`call` 必须经过 `resolve_tool` 取得当前已启用、已发现并通过
include/exclude 的元数据，再用目标 Descriptor 进入现有 `CapabilityPolicyEngine` 和
`MCPToolBinding`；只读模式、图片/联网限制、scope 与本轮选择都按目标工具检查。search 不执行，
describe 只返回定义。

`yuki.toolBundles` 可把一个 Server 的多项工具声明为不可拆分的 semantic namespace，一个工具也可
加入多个 Bundle。Bundle 只解决目录选择完整性，不定义步骤顺序或条件，因此没有额外 Workflow
DSL。远端 `destructiveHint` 映射为破坏性风险，`openWorldHint` 保留在 Provider 元数据中，但
两者都不能取代本地策略。

MCP 工具成功结果的提交状态默认未知，由 Tool Kernel 按目标 effect 解析；只读成功为 false，
写入成功为 true，MCP `isError` 固定为 false。普通工具/业务错误、4xx 和 429 不会销毁连接；
只有会话失效、网络断开、协议或初始化失败才断开。协程取消原样传播，不记为失败或触发重连。
