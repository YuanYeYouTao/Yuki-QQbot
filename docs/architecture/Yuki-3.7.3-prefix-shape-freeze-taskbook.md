# Yuki 3.7.3 自主与 @ 权威合并

本文是实施合同。本轮范围收成一刀：**自主录取之后，和同一个人的 @ 轮同一套可写权威，第一跳请求形状也对齐。**

不做：插件点评可写、`generate_external_reply` 收口、删除 `align_conversation_prefix_tools`、rollup 冻结前缀、发版。

- **触发分开**：自主仍走 debounce / admission / 可打断；@ 仍走点名。
- **做成之后合并**：不再 `read_only`。可写记忆、可建定时任务、可表情/语音布局；超管触发则仍可 `request_tools` 拉特权。
- **第一跳形状对齐**：用户与自主发出去的 `tools[]` + `native_tools` 相同。插件本轮只享受「不要因为 `tools_closed` 被 runner 误钉 `read_webpage`」，执行仍关。

DeepSeek 从 token 0 匹配 `tools[]` + `native_tools` + 人设 + 历史。不要扩到点餐 bundle、管理员特权钉定、rollup 水位、冻结已发送历史、或发版。不硬编码群名、插件 id 或中文路由。

## 工作方式

1. 先有本任务书，再改代码。
2. 机制通用。pin / 硬顶 / `WEB_MODE` 继续走配置。
3. 禁止动 NapCat；生产禁止现场 `docker build`。
4. 本轮不涨版本号、不写 Release。正式版本仍是 3.7.1。

## 不要做

- 不钉 `admin_*`、`decline_reply`、MCP / 点餐 / 订阅。`decline_reply` 仍只给自主，走 `request_tools`，不进首轮表。
- 不把私聊、定时任务会话、插件私聊会话并进这本群账。
- 不改 rollup 水位，也不做「换摘要仍续写」的冻结前缀。
- 不把规划 origin 一律改成 `USER_MESSAGE`（`decline_reply` 会从 requestable 消失）。账本 / admission / 观察仍记 `autonomous_group`。
- 不为 github-monitor 开特例。
- 不把「不可信」策略写进静态 instructions。
- 不把插件点评也改成可写。

## 现网事实（2026-08-21 生产群账）

- 用户连聊 `request_shape_hash` 稳定为 `a51c7d36…`，命中约 90%。
- 自主同一跳先暴露 11 个工具，立刻变成 12 个（多出 `read_webpage`），hash 变成 `3d6cd442…`。
- 直接原因：`_run_agent` 在 `read_only` 时把 `allowed_capabilities` 收成空集 → 原生 web 绑不上 → runner 误判并 `enable_native_web_fallback()`。
- 产品原因：自主被做成只读，和 @ 不是同一条 Agent 路。去掉 `read_only` 能消掉这条误判，但 runner 仍可能因插件 `tools_closed` 或句子改 web 表再分叉，Commit 1 还是要做。
- 后台 extractive rollup 在自主前 1 秒换了左沿。换表后第一跳约 50% 是摘要代价；下一跳 20% 是表不一致。
- 3.7.2 单测只比了 `definitions()` 名字，没比最终 `ChatRequest`。

---

## 合同 A：自主 = @ 的权威，不是只读旁路

录取之后，`respond(..., autonomous=True)` 与同触发事件的 @ 轮对齐：

- `read_only=False`（有图仍走现有 `contains_images` 禁写，和 @ 一样）
- `allow_automation=True`（无视觉输入时，和 @ 一样）
- `allow_admin_actions` / `allow_generic_onebot` 看 **触发消息发送者** 是不是超管，不看 origin
- `scheduled_automation_intent` 不再排除 autonomous
- 记忆：`_WRITE_ORIGINS` 纳入 `AUTONOMOUS_GROUP`；`memory_change` 做成，不再只声明
- `set_voice_preference` 对自主开放（actor 仍是触发消息发送者）
- 能力策略里 `DESTRUCTIVE` 对自主与 @ 相同（特权仍不进首轮，超管走 `request_tools`）

仍不同的只有触发和「可以闭嘴」：

- 触发：debounce / admission / 新消息打断
- `decline_reply`：仅 `AUTONOMOUS_GROUP`，不钉首轮，`request_tools` 加载
- 观察 / 账本 origin 仍是 `autonomous_group`，方便统计，不另开工具表

Actor 就是触发那条群消息的发送者。自主改记忆、改语音偏好，等于这个人 @ 了一句同样的话。不要用 Bot 或授权用户顶替。

## 合同 B：什么叫「首轮形状冻结」

同一部署、同一本群账，每一轮 **第一跳** 与 origin / 当前句子 / 插件关闭态无关：

- 函数 `tools[]` 的 name + description + parameters
- `native_tools` 的类型列表
- `request_shape_hash`

允许变的：

- `callable_ids`：用户与自主应对齐（同一 actor、同一 pin）；插件仍 `tools_closed`
- 自主额外可 `request_tools` 出 `decline_reply`（只影响本轮后续跳）
- 真实原生失败后的 Tavily 兜底（只影响本轮后续跳；下一轮第一跳回到冻结首轮）
- TURN/dynamic 挂在当前条上的时间、记忆、外部事件策略

禁止变的：

- runner 在第一跳之前 `enable_native_web_fallback()` 补表
- 因 `tools_closed` / 残存 `read_only` 清空 `allowed_capabilities` 导致原生绑不上
- 因当前句子、URL、口头覆盖、域名规则改第一跳 web 形状
- exclusive write 缩小已声明的首轮 schema（只许缩小 callable）

`WEB_MODE` 决定部署级首轮 web 形状：

- `tavily`：函数 `web_search` + `read_webpage`，无 native
- `native` / `native_with_tavily_fallback`：Responses 下三类声明同一份 native；若已绑定 native，发出去的函数表剥掉 `web_search`/`read_webpage`，三类剥得一样
- `disabled`：三类都无 web

代价：Responses 下声明了 native_tools，模型就能发起原生检索。插件点评为了表一致也要带上；本轮接受插件可能走原生检索，仍禁止 function 写工具。

---

## Commit 1 — 冻结首轮形状，清 runner 改表

目标：用户 / 自主 / 插件点评发出去的第一跳 `ChatRequest` 形状相同。`read_webpage` 不得在自主第一跳被钉入。

### `_run_agent` 的 `allowed_capabilities`

主会话三类的 **前缀绑定** 只看部署 `WEB_MODE` 是否启用 web。这个集合只给 `NativeToolBinder.bind`，不是写工具授权。

### runner 禁止误判

「`web_search` 已选 + native 为空 + `NATIVE_WITH_TAVILY_FALLBACK` → fallback」只允许在协议/模型能力真的绑不上 native 时触发（Chat Completions、profile 无 `NATIVE_WEB_SEARCH`）。

禁止因为 `allowed_capabilities` 被收空而走这条路。

`enable_native_web_fallback()` 仍可用于本轮后续跳（原生已经真正失败）。不得在第一跳、不得因 origin 改 pin。

### 第一跳 web 形状只看 `WEB_MODE`

`WebProviderRouter.select(content, mode)` 仍可用于事后路由、失败兜底、日志。第一跳是否绑定 native / `force_tavily_fallback` 不得跟句子、URL、口头覆盖、域名走。

### `_host_priority` 与 exclusive write

保持 3.7.2 pin 规则。`_native_web_fallback` 不得把 `read_webpage` 钉进 **新一轮第一跳**。exclusive write 只改 `callable_ids`，不砍已声明 schema。

## Commit 2 — 自主与 @ 权威合并

目标：自主不再是只读旁路。录取之后和同 actor 的 @ 轮同一套可写权威。不收插件入口，不删 `align` 旗标。

- `respond`：`read_only=False`；`allow_automation` / 超管特权 / `scheduled_automation_intent` 不再排除 autonomous。
- `_WRITE_ORIGINS` 含 `AUTONOMOUS_GROUP`。
- `DESTRUCTIVE` 对自主与 @ 相同。
- `set_voice_preference` 对自主开放；`decline_reply` 仍仅自主。
- 记忆 session origin 仍记 `AUTONOMOUS_GROUP`（账本），合同按可写处理。

仍分流：admission / 投递 / `decline_reply` 仅自主 / 插件 `tools_closed`。

## 自审

1. **只去掉 `read_only` 不够。** 记忆 `_WRITE_ORIGINS`、语音偏好、`DESTRUCTIVE`、`allow_automation` 不一起改，自主仍是半只读。
2. **3.7.2 没测最终请求。** 验收必须经过 runner 绑定之后的 `ChatRequest`。
3. **插件仍会 `tools_closed`。** Commit 1 的 `allowed_capabilities` 不能指望自主合并就顺带修好。
4. **`decline_reply` 必须留在真实自主 origin。** 规划 origin 若改成 `USER_MESSAGE`，拒答会坏。不要把它钉进首轮。
5. **Actor 是触发消息的人。** 闲聊里最后说话的人会被写成记忆主语；这和「他 @ 了同样一句话」一致。
6. **句子改 web 表仍是洞。** 第一跳只看 `WEB_MODE`。
7. **rollup 换左沿仍冷一跳。** 修完后换表约 50%，下一跳应回约 90%。20% 那种崩法必须消失。
8. **换最终请求形状当天会再冷一次。**

## 建议测试命令

```text
pytest tests/unit/test_agent_runner.py tests/unit/test_dynamic_tool_request.py tests/unit/test_native_web.py tests/unit/test_capability_exposure.py tests/unit/test_conversation_runtime_r4.py tests/unit/test_context_assembler.py tests/integration/test_web_search_chat.py -q
```
