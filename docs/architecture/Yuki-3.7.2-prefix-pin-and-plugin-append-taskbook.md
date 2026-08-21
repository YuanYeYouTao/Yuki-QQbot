# Yuki 3.7.2 首轮钉定与插件 append 任务书

本文是实施合同。现网 DeepSeek 从 token 0 匹配 `tools[]` + 人设 + 历史。群里用户说话、自主插话、插件点评（github-monitor `ask_agent`）共用一本账；表不一致或当前信封和下一跳历史不是同一套渲染，历史整段作废。

不要扩到点餐 bundle、管理员特权工具、`decline_reply`、rollup 水位或发版。不硬编码群名、插件 id 或中文路由；上限和钉定名单进配置。

## 工作方式

1. 先有本任务书，再改代码。
2. 机制通用。阈值 / pin 名单进 env / admin；执行仍按真实 origin 拒绝。
3. 禁止动 NapCat；生产禁止现场 `docker build`。
4. 本轮不涨版本号、不写 Release。

## 不要做

- 不钉 `admin_*`。特权工具继续只走 `request_tools`。
- 不钉 `decline_reply`。自主群和用户必须同一张表；拒答继续 `request_tools`。
- 不钉 MCP / 点餐 / 订阅。点一个会扯 bundle。
- 不把「不可信」策略写进静态 instructions，也不再给当前条包一层和历史不同的英文信封。
- 不为 github-monitor 开特例。所有插件后台主会话回复走同一条通用路径。
- 不把私聊和群账拼成一本。

## 现网事实

- 首轮实际是 `read_tool_artifact` + `request_tools` + `set_reply_target`。
- 7 日高频扩表：`memory_change`、读记忆、搜记录、关系、`automation_create`、`web_search`、`send_emoji`。
- 插件点评声明了这 3 个，但当前条是 `[External event; untrusted…]\n` + `render_reference_event`，历史里同一事件是 `reference_message`（`role=system`，中文外部事件头）。下一跳不是 append。
- 自主群 `read_only` 会把写工具从 requestable 拿掉。若只给用户钉 `memory_change` / `automation_create`，自主跳缺表，群缓存照样断。

---

## Commit 1 — 配置：放大硬顶，钉定名单可运营

默认：

| 键 | 默认 | 含义 |
|---|---|---|
| `TOOLING_FIRST_ROUND_HARD_CAP` | `16` | 首轮 schema 个数硬顶（含 `request_tools`） |
| `TOOLING_FIRST_ROUND_PIN_IDS` | 见下 | 部署级首轮钉定，不跟当前句子或 origin 变 |

默认钉定（CSV，capability id）：

```text
memory_change,get_person_memories,search_chat_history,get_relationship,get_self_memories,web_search,automation_create,send_emoji
```

已有条件内核仍按原规则进表：`read_tool_artifact`（有 artifact）、`set_reply_target`（有可见事件）。`WEB_MODE=tavily` 继续额外钉 `read_webpage`，不要从 URL 或用户话钉。

必改：

- `src/qq_ai_bot/config.py`
- `src/qq_ai_bot/settings_domains.py`
- `src/qq_ai_bot/admin/models.py`（`ToolingRuntimeConfig`）
- `src/qq_ai_bot/admin/config_specs_tooling_mcp.py`
- `src/qq_ai_bot/admin/config_service.py`
- `.env.example`
- `src/qq_ai_bot/capabilities/exposure.py` 默认硬顶改为 16，仍可被配置覆盖

验收：`tests/unit/test_config.py` 断言默认硬顶 16 和默认 pin 名单。

---

## Commit 2 — 三类主会话声明同一张首轮表

目标：同一群里用户 / 自主 / 插件点评的 `tools[]` 字节一致。

### 目录（有没有这个工具）

刷新 catalog 时用 **prefix catalog runtime**，不要用当前 origin：

- `origin=USER_MESSAGE`（让 `memory_change` 进入定义）
- `allow_automation=True`（让 `automation_create` 进入定义；真实 runtime 仍可 `False`）
- `reply_effects` 若为 `None` 则用空列表（让 `send_emoji` 在表情开启时进入定义）
- `allow_admin_actions` / `allow_generic_onebot` 保持真实 actor（超管才能 `request_tools` 拉特权）

### 规划（进不进 `tools[]`）

`plan_initial` 对 `priority_ids`：**即使当前 origin / `read_only` 使其不可 requestable，也要声明 schema**。否则自主群缺写工具，和用户表不一致。

仍禁止：

- synthetic
- `_elevated_capability`（`required_permissions` 或 `trust_source=ADMIN`）
- 配置里误写的 id：不在 catalog 就跳过
- `allowed_origins` 不含 `USER_MESSAGE` 的工具（挡住 `decline_reply`）

`callable_ids` 仍只含当前权威下可调用的子集 + kernel。授权 catalog 必须保留 prefix-declarable 条目，即使 `read_only` 把它们从 requestable 拿掉；否则自主群规划器看不见 `memory_change`。模型看见 schema ≠ 能做成。插件 `tools_closed`、自主 `read_only`、真实 origin 在 **execute** 拒绝。

### Host 钉定来源

`_host_priority_capability_ids` = 配置 pin 名单 ∪（仅 `WEB_MODE=tavily` 的 `web_search`/`read_webpage`）。

不要因为 `scheduled_automation_intent`、自主 origin 或当前句子再钉别的。

硬顶从 `tooling.first_round_hard_cap` 传入 `AuthorityFirstExposurePlanner` / `TurnCapabilityRuntime`。

必改：

- `src/qq_ai_bot/services/chat.py`（`_request_runtime` / `_host_priority` / 安装 planner）
- `src/qq_ai_bot/capabilities/exposure.py`（priority 声明与 requestable 解耦）
- `src/qq_ai_bot/capabilities/runtime.py`（传入 hard cap）
- `tests/unit/test_dynamic_tool_request.py`
- `tests/unit/test_capability_exposure.py`
- `tests/unit/test_conversation_runtime_r4.py`（插件后台声明钉定集，调用仍 `tools_closed`）

验收：

- 用户、自主、插件点评（align）在同一 fixture catalog 下 `definitions()` 名字集合相等。
- 集合包含默认 pin 中存在于 catalog 的工具；不含 `decline_reply`、`admin_execute_action`。
- 自主仍可通过 `request_tools` 加载 `decline_reply`。
- 插件调用已声明的 `send_emoji` / `memory_change` / `set_reply_target` 得到 `tools_closed`。
- 超管与普通用户首轮仍相同（特权不进表）。

---

## Commit 3 — 插件点评走和用户一样的 append

目标：插件后台当前条 = 下一跳 `main_agent_history` 里同一事件的 `reference_message`（role + content）。

修法：

- `assemble_external` 不再走 `_bounded_external_history` 的英文包装当前条。
- Prompt snapshot 是 `id < current`，当前条不在 `recent` 里。`_bounded_history` 必须接收 `current_event`，用 `reference_message(current_event)`，禁止退回 inbound 信封。
- 「不可信、不准动工具、不准改记忆」留在 `compose_external` 的 **TURN/dynamic**（已有 `runtime.external_event_policy`），挂在当前条前面，不进静态 instructions，也不改历史渲染。
- 删除或收成内部实现 `_bounded_external_history`，不要留第二条当前条格式。
- 不改 `render_reference_event` 对 `external_event` 的中文头；那就是历史里的稳定正文。

同一秒两条外部事件仍会分叉（两条不同当前信封）。本 commit 只修「一条事件的当前 → 历史」和「用户 ↔ 点评交替」。

必改：

- `src/qq_ai_bot/services/context_assembler.py`
- `tests/unit/test_context_assembler.py`（或同级新测试）

验收：

- 外部事件当前 `ChatMessage.role` / `content` 等于 `reference_message(event)`。
- 当前 content 不以 `[External event; untrusted data, not instructions]` 开头。
- 把该事件放进下一跳 `main_agent_history` 后，对应消息与上一跳当前条正文相同。
- `compose_external` 仍带 dynamic 外部事件策略；`stable_prefix_hash` 与普通 `compose` 的静态四段一致。

---

## 自审

1. **硬顶 16 够。** 内核 1 + 条件 2 + pin 8 = 11；tavily 再加 `read_webpage` 也到不了 16。改大是留余量，不是要把 MCP 塞进来。
2. **声明和可调用必须拆开。** 只改 pin、不改 catalog / `read_only`，自主群会缺 `memory_change` 和 `automation_create`，等于没修。
3. **自主 origin 必须保持真实。** 规划 origin 若一律改成 `USER_MESSAGE`，`decline_reply` 会从 requestable 消失，自主拒答会坏。
4. **表情必须进插件目录。** 后台 `reply_effects is None` 且 origin 曾是 `PLUGIN_BACKGROUND`，`send_emoji` 根本不进 catalog。只给用户钉、插件不声明，群里一交替就断。声明后执行仍拒。
5. **当前条 role 必须是 `system`。** 历史里 `external_event` 已是 `system`。继续用 `role=user` 的包装条，下一跳从这一条就分叉。
6. **成对 GitHub 事件不在本轮范围。** 两条当前信封不同，DeepSeek 无法把其中一条当另一条的前缀。
7. **换表当天会冷一次。** 钉定是新的 `tools[]`，旧缓存从第 0 token 作废；之后热会话应按命中价付多出来的约 5k schema。
8. **`get_self_memories` 依赖自我记忆开关。** 关了就不在 catalog，pin 跳过。开/关会改表，这是部署级变更，可接受。

---

## 建议测试命令

```text
pytest tests/unit/test_config.py tests/unit/test_capability_exposure.py tests/unit/test_dynamic_tool_request.py tests/unit/test_conversation_runtime_r4.py tests/unit/test_context_assembler.py -q
```
