# Yuki 3.6.2 冻结近窗（取消滑动）开发任务书

> 文档状态：实施基准稿（2026-08-19 对照 3.6.1 生产账单与代码）  
> 目标版本：3.6.2  
> 基线版本：3.6.1（`258823548ffa7e520e949da052a071217c531e60`）  
> 建议开发分支：`feature/3.6.2-frozen-history-tail`  
> 审阅日期：2026-08-19  
> 实施方式：**6 个**可独立审阅、持续通过质量检查的 commit。不得合并成一次大提交。  
> 冻结合同：[conversation-rollup.md](conversation-rollup.md) 的 3.6.2 近窗条款以本文为准并已在 Commit 1 回写。

---

## 0. 执行摘要

3.6.1 已经把 Prompt 做成 `STATIC → SESSION → uncovered 原文 → current+TURN`，并用 `coverage_end` 作为账本左沿。生产上闲聊 **SESSION 已进入 DeepSeek 前缀缓存**（cached 中位 2560→3456），但 **原文近窗几乎整段 miss**。

原因不是「原文不能缓存」，而是装配在覆盖之后仍然用高低水位 **从尾巴重切**。短消息群的热尾按「48 条或 3600 原文字，取更大保护」把覆盖点之后的全部短消息护住，第二刀 L0 切不下去；近窗每轮超预算，于是每轮滑动，`input[0]` 左沿移动，前缀缓存在 SESSION 处断开。

本项目要做的不是取消有界近窗，也不是把全部 `chat_events` 塞进 Prompt。**左沿只允许 `coverage_end` 前进；超预算时同步 extractive，不准滑。**

对照 3.6.1 代码后的硬事实（2026-08-19 生产 18.7h）：

- 5/6 会话已有 active 覆盖；已覆盖会话 pending 平均 **199 事件 / 3426 原文字**。
- `hot_tail_boundary` 用 `min(event_start, char_start)`，总字数 < 3600 时 `char_start` 走到区间起点，**可压缩集合为空**。
- `ContextAssembler._ensure_coverage_before_shift` 在 `coverage_end > 0` 时直接返回。
- `ConversationHistoryService.ensure_extractive_coverage` 在已有覆盖时因 `allow_raw_window_shift=True` **直接返回 None**。
- 于是第一刀 L0 之后，Prompt 体积只靠 `_select_history_window` 滑动下降。这与合同「Frontier 不变时近窗可走前缀缓存」相反。
- `load_prompt_snapshot` 对 `id > coverage_end` 使用 `ORDER BY id DESC LIMIT n`。未覆盖条数一旦超过 `local_event_limit`，会在 coverage 与 Prompt 之间挖洞。本项目一并修掉。

不在范围内：关思考、Flash schema 失败、MCP 点餐、metadata 占比、Memory 抽取调用量。那些是别的账单。

---

## 1. 冻结结论

### 1.1 Prompt 左沿

```text
有覆盖：history 的第一条原文必须是 min{id | id > coverage_end} 对应气泡
无覆盖：history 从当前 epoch 最早仍装得下的账本事件开始；
        一旦会丢掉更早前缀，必须先同步 extractive，成功后左沿改为新的 coverage_end+1
禁止：按「从最新往回填满预算」选择任意中间起点
```

`coverage_end` 是唯一持久左沿。进程内 `_history_window_anchors` 不再参与选窗。重启后不得重新从尾巴填。

### 1.2 超预算 = 压缩，不是滑动

未覆盖原文的渲染体积（与 assembler 相同的信封字节）或条数超过近窗预算时：

1. 同步、零 LLM：从 **未覆盖区间的左端**（紧挨 `coverage_end`、且严格早于热尾）切一块 L0 extractive。
2. 重载 Snapshot（`id > 新 coverage_end`）。
3. 同一 turn 最多 `conversation_history_sync_extractive_max_slices` 次（默认 3）。
4. 仍超预算：保留全部未覆盖原文进入 Prompt，**不得**从左或从右丢掉气泡造成空洞。打指标，下一轮继续压。

禁止用低水位从尾巴重选来「假装」装进预算。

### 1.3 热尾是上限，不是「取更大保护」

3.6.1 合同「48 事件或 3600 字符，取更大」在短消息下等于全留。3.6.2 改为：

```text
热尾 = 最近 raw_tail_events 条 与 最近 raw_tail_characters 渲染字符
       两者的交集（保留集合更小的那边）
first_protected_event_id = max(event_start, char_start)
```

字符必须按 **Prompt 渲染后的气泡**（信封 + 正文 + 视觉摘要）累计，不得只用 `content` 裸长。否则 QQ 短句永远撞不到字符帽。

热尾之内禁止进 L0。热尾之外必须可压缩。

### 1.4 缓存

- STATIC 仍不可塞 rollup。
- SESSION 仅在 Frontier 更新时变；允许因此打断 history 缓存，不许动 STATIC。
- 未覆盖原文在左沿冻结时只允许在末尾追加。`splice_appended_input` 继续服务 Responses；`raw_history_window_shifted` 仅在 **本 turn `coverage_end` 前进** 时为真（composer 丢弃 input cache）。不得在「只是新来一条消息」时为真。
- 同轮工具 continuation 的高 cache 不作为本项目验收科目。

### 1.5 预算语义

`max_context_characters`、metadata 占比、SESSION 先占 history 余额，全部保留。

```text
history 余额 = max(0, max_context_characters - metadata_json - session_text)
近窗预算 = min(
    history 余额,
    有覆盖时 history 余额 * raw_tail_budget_ratio,
    可选的 raw 渲染字符硬顶（配置，默认等于 raw_tail_characters）
)
```

该数字只用于 **是否触发同步 extractive** 和运维观测，不用于从尾巴切 Prompt。

无覆盖且尚未能写出第一刀 extractive 时：仍加载当前 epoch 最近一段原文（现有 `list_recent` 行为仅作为 **bootstrap**）。一旦 `must_roll` 且 extractive 成功，立即改走 `id > coverage_end`，其后禁止再 bootstrap 滑动。

### 1.6 仍然有效的 3.6.1 不变量

- `chat_events` 唯一原文；不写 Memory V2。
- 无 active 覆盖时，Prompt 不得丢掉未覆盖前缀（今天 `allow_shift=coverage_end>0` 的精神保留，但「shift」不再指 assembler 滑动）。
- extractive 同步、Flash 异步、同一 `source_fingerprint`。
- 摘要与近窗不重叠、无覆盖空洞。
- Reset 开新 epoch。
- Flash 默认仅 `TurnOrigin.USER_MESSAGE`；白名单在配置。
- 机制通用；专有名词、别名、阈值进配置。

---

## 2. 代码入口（实施时必读）

| 点 | 文件 | 现状 |
|---|---|---|
| 滑动选窗 | `src/qq_ai_bot/services/context_assembler.py` `_select_history_window` | 超高水位从 `reversed(candidate)` 填到低水位 |
| 覆盖后仍滑 | 同文件 `assemble` / `assemble_external` | `allow_shift=snapshot.coverage_end > 0` |
| 有覆盖不再压 | 同文件 `_ensure_coverage_before_shift` | `coverage_end > 0` 直接返回 |
| 有覆盖拒绝 extractive | `src/qq_ai_bot/conversation/history/service.py` `ensure_extractive_coverage` | `allow_raw_window_shift` 为真则 `return None` |
| 热尾取更大 | `src/qq_ai_bot/conversation/history/policy.py` `hot_tail_boundary` | `first_protected = min(event_start, char_start)`，字符用裸 `content` |
| 预抽被热尾掏空 | 同文件 `select_l0_candidate` | `compressible` 要求 `id < first_protected` |
| 滑动时钟耦合 | 同文件 `must_roll_prefix` | 调用 `_select_history_window` 算「会被丢掉的前缀」 |
| DESC 截断 | `src/qq_ai_bot/conversation/history/repository.py` `load_prompt_snapshot` | `newest=True` |
| 拼接缓存 | `src/qq_ai_bot/prompting/input_cache.py`、`prompt_composer.py` | `shifted` 则 forget |
| 回归测试把滑动当正确行为 | `tests/unit/test_context_assembler.py` `test_history_window_rolls_in_blocks_*` | 必须改写，不得再断言从尾巴重切 |
| 热尾测试锁定「取更大」 | `tests/unit/test_conversation_history_policy.py` | 必须改成交集 / 更小保留集 |

---

## 3. 明确不做

- 不改人格 / CORE_CONTRACT 位置，不把 SESSION 并回第一条 system。
- 不恢复 Planner，不把摘要写入 `memory_facts`。
- 不在本项目关思考、改 `LLM_MAX_OUTPUT_TOKENS`、改 `model_profiles`。
- 不修 Flash `structured_output` / `HistoryJobConflictError`（已知后台升级问题；extractive 仍是权威覆盖）。
- 不为「点麦当劳」之类例句加分支。
- 不把限额写死在 Python：热尾条数/字符、同步 extractive 次数、预算比一律配置。
- 不删除 `get_chat_history_around`。左沿前进后仍靠它回读。

---

## 4. 配置（全部可运营）

现有键保留，**语义在 Commit 2/6 变更**，默认值在 Commit 6 收紧（代码先吃新语义、后改默认，避免中间 commit 行为漂移说不清）。

| 键 | 3.6.1 默认 | 3.6.2 建议默认 | 作用 |
|---|---|---|---|
| `conversation_history_raw_tail_events` | 48 | 32 | 热尾最多保留条数 |
| `conversation_history_raw_tail_characters` | 3600 | 1600 | 热尾最多保留 **渲染** 字符 |
| `conversation_history_raw_tail_budget_ratio` | 0.55 | 0.40 | 近窗预算相对 history 余额的上限（触发压缩，不切窗） |
| `history_window_low_watermark_ratio` | 0.67 | 保留键但 **Prompt 选窗不再使用** | 避免无迁移删字段；文档标明废弃于装配 |
| **新增** `conversation_history_sync_extractive_max_slices` | — | 3 | 同一 assemble 最多同步 L0 次数 |
| `conversation_history_rollup_l0_min_events` | 32 | 不动 | 预抽 / 切片门槛（OR 字符） |
| `conversation_history_rollup_l0_min_characters` | 8000 | 不动 | 同上 |
| `max_context_characters` | 12000 | 不动 | |

`settings_domains.py`、`config.py`、`help.md` 同步。本项目不强制做 admin 热更新；若加，别名放 spec，不写 Python 词表。

---

## 5. Commit 计划

每个 commit：`ruff` + 相关单元测试必须绿。禁止「先堆代码最后再补测试」。禁止把 6 个 commit squash 进 main。

---

### Commit 1 — 回写合同

**Why：** 3.6.1 合同仍写「有覆盖后高水位 / 低水位切近窗」。不先改合同，后面删滑动会被当成违约。

**改：**

- `docs/architecture/conversation-rollup.md`
  - §5 Hot Tail：改为交集上限，字符按渲染气泡。
  - §6.1：Frontier 不变时近窗 **左沿冻结、只追加**；打断 history 缓存的唯一正当理由是 `coverage_end` / Frontier 更新。
  - §6.2：预算数字用于触发压缩，禁止「从最新往回填」选窗。
  - §6.3：删除「这之后才允许把原文收到低水位」；改为「这之后 Prompt 左沿 = 新 coverage_end」。
- 本文档保持为实施计划（已存在则本 commit 只动合同 + 必要时微修订本文）。
- `CHANGELOG.md` Unreleased 先记一条指向合同修订（实现细节等后续 commit 再补全也可以，但 Unreleased 必须出现 3.6.2 标题下的意图句）。

**不改 Python。**

**验收：** 合同中不再出现「有覆盖后用低水位从尾巴重切近窗」作为现行行为。

---

### Commit 2 — 热尾改为上限（策略纯函数）

**Why：** 不改热尾，后面再勤快的同步 extractive 也会得到空的 `compressible`。这是生产 199 条挂死的根因之一，可单独落地、单独回滚。

**改：**

- `src/qq_ai_bot/conversation/history/policy.py` `hot_tail_boundary`
  - `first_protected_event_id = max(event_start, char_start)`（保留集更小）。
  - 从后往前累计字符时，使用与 Prompt 一致的渲染长度。不得在 policy 里 new 一个依赖请求态的 Assembler；把「一条 SourceEvent 的渲染字符」收成纯函数（可放 `source.py` 或 `event_prompt` 的无请求辅助），测试可直接喂。
- 空 snapshot / 单条事件：行为保持「全部视为热尾」。
- `tests/unit/test_conversation_history_policy.py`
  - 删除或改写 `test_hot_tail_uses_the_wider_of_event_and_character_protection`。
  - 新增：短消息（裸正文远小于 3600）不得把整个未覆盖区间护住；可压缩前缀非空。
  - 新增：长消息时字符帽先于条数帽生效（保留集更小）。
  - `select_l0_candidate(must_roll=False)` 在「199 条 × 短正文、已有 coverage_end」夹具上必须给出非空切片。

**不改 Assembler / Service 控制流。** 生产在只合并本 commit 时：预抽 L0 开始能切，Prompt 可能仍滑动——允许短暂中间态。

**验收：** 短消息夹具 `len(protected) <= raw_tail_events`，且 `first_protected > coverage_end+1`（当未覆盖条数明显大于热尾时）。

---

### Commit 3 — 有覆盖后仍可同步 extractive；must_roll 与滑动解耦

**Why：** 热尾放开后，Service 仍会在已有覆盖时拒绝 extractive，Assembler 也不会叫它。

**改：**

- `allow_raw_window_shift`：不再表示「Assembler 可以滑」。若仍保留方法，文档与测试改为「Prompt 左沿是否已由 coverage 钉住」。**禁止**再用它在 `ensure_extractive_coverage` 开头 `return None`。
- `must_roll_prefix`：禁止调用 `_select_history_window` 的滑动结果。改为：
  - 输入 = 未覆盖渲染序列（已排除 current）。
  - 若体积/条数 ≤ 高水位：返回空。
  - 否则返回 **从左起、不含热尾** 的连续 id，长度不超过一刀 L0（`l0_max_events` / `l0_max_characters`）。这就是下一刀 extractive 的来源，不是「滑动后会被丢掉的任意前缀」。
- `ensure_extractive_coverage`：无论 `coverage_end` 是否 >0，只要 `must_roll_prefix` 非空就切 extractive + 按现规则 enqueue Flash。
- `observe_event` 预抽路径保持；热尾修复后它应开始有候选。
- 测试：
  - `ensure_extractive_coverage` 在已有 L0 且未覆盖尾巴超预算时提交第二刀，且新 `coverage_end` 连续无洞。
  - 热尾内的最新消息不得出现在 `event_ids`。
  - 无覆盖 + 超预算：仍先写第一刀（回归 3.6.1 不挖空）。

**本 commit 仍不要删 `_select_history_window`**，以免和 Assembler 大改缠在一起。可以标内部注释：下一 commit 停止从 assemble 调用滑动分支。

**验收：** 单测即可；不要求此时 Prompt 已不滑。

---

### Commit 4 — Assembler：左沿只认 coverage_end

**Why：** 这是用户能感知的行为变化，也是缓存能否续上的关键。

**改：**

- `load_prompt_snapshot` / `_load_recent_events`：有 `coverage_end` 时 `id > coverage_end ORDER BY id ASC`。禁止 `newest=True` 造成 coverage 与窗口之间的空洞。`limit` 仅作安全阀；若命中安全阀仍超，走 Commit 3 的压缩循环，不得改成 DESC。
- `_ensure_coverage_before_shift` 更名为能表达「超预算则压」的名字（例如 `_ensure_uncovered_fits_budget`）。删除 `coverage_end > 0 → return`。循环最多 `sync_extractive_max_slices` 次：压 → 重载 → 再量渲染体积。
- `_bounded_history` / `_bounded_external_history`：对 `recent` **整段**渲染进 Prompt（排除 current）。删除对滑动分支的调用。`allow_shift` 参数删除。
- `_select_history_window`：若已无调用方则本 commit 删除；若 policy 仍引用则 policy 必须已在 Commit 3 脱钩。不得留下「assemble 还在滑」的死路径。
- `_history_window_anchors`：停止写入选窗。`prompt_cache_key` 继续包含 `coverage_end` 与 `revision`。
- `raw_history_window_shifted`：仅当本 turn 重载前后 `coverage_end` 增加。
- `assemble` 与 `assemble_external` 同一套规则。
- 日志：`context_assembled` 保持内容脱敏；可增加 `uncovered_events` / `over_budget` 整数，不得打正文。

**测试（必须）：**

- 改写 `test_history_window_rolls_in_blocks_between_high_and_low_watermarks`：追加消息时左沿不变；超预算且 extractive 成功后左沿 = 新 coverage_end+1，而不是低水位那条。
- 改写 `test_history_window_character_roll_keeps_a_contiguous_recent_block`：不再断言从尾巴留下最后两条。
- `test_context_assembler_rollup.py`：有覆盖后 80+ 条短消息，Prompt 第一条 id 等于 `coverage_end+1`；连续两轮 assemble（只追加）第一条气泡字节不变。
- 空洞：覆盖 `[1,100]` 后不得出现 Prompt 从 140 起跳。
- extractive 抛 `FrontierInvariantError`：不滑，聊天成功，现有 `conversation_history_coverage_skipped` 仍打。
- 插件 `assemble_external` 同行。

**验收：** 单元测试证明「超预算 → coverage 前进或整段保留」，证明「不从尾巴选起点」。

---

### Commit 5 — 追加缓存与指标

**Why：** 不滑之后，Responses 的 append-only 才有稳定 `sent_prefix`。要有测试钉住，否则下一轮又把 TURN 前缀渲染进历史气泡、拼不上。

**改：**

- `prompt_composer`：`shifted` 只跟 coverage 前进走（Commit 4 已给指标）。coverage 不变时必须走 `splice_appended_input`。
- `tests/unit/test_prompt_input_cache.py`：新增「左沿冻结、TURN 只在 current」的两轮拼接；失败信息不得要求看正文也可以断言 fingerprint。
- 若拼接失败（群名片导致信封变化等）：允许 miss，**仍不得滑动重切**。可打 `prompt_input_rebuilt` debug（已有）。
- 指标：`raw_history_window_shifted` 的公开含义写进 `conversation-rollup.md` 一句。质量夹具 `quality.py` 若断言旧 `shifted` 语义，一并改。

**验收：** 两轮私聊追加，assembler 历史前缀指纹相同；composer 第二次走 splice 而不是 rebuild（可用 spy/计数，不要快照用户正文到仓库）。

---

### Commit 6 — 默认值、文档、质量门

**Why：** 语义先落地，默认值后收紧，回滚默认值不必回滚算法。

**改：**

- `config.py` / `settings_domains.py`：表 §4 的新默认 + 新字段校验（`ge=1` 等）。
- `docs/help.md`：热尾、预算比、同步切片次数。写清「预算触发压缩，不滑动切窗」。
- `CHANGELOG.md` Unreleased / 3.6.2 条目写完。
- `src/qq_ai_bot/__init__.py` 版本号 **不要**在未发布时提前改；等发布流程。本 commit 只动 changelog 与 help。
- `conversation/history/quality.py` 与 `artifacts/history-rollup-quality`：增加「短消息超预算后 coverage 连续前进、Prompt 左沿不跳号」；删除对滑动低水位的依赖。
- `docs/upgrade-3.6.1.md` 不改历史；若需要升级说明，新增 `docs/upgrade-3.6.2.md` 草稿（无 Alembic 则写明无需迁表）。

**预计无新迁移。** 若必须加列（不应发生），另开讨论，不得夹带进本 commit。

**验收：** `pytest tests/unit/test_conversation_history_policy.py tests/unit/test_context_assembler.py tests/unit/test_context_assembler_rollup.py tests/unit/test_prompt_input_cache.py tests/unit/test_config.py` 以及现有 history quality 测试。全量 `ruff` / 项目惯用单测命令。

---

## 6. 验收科目（相对 3.6.1 生产）

对账仍用 `model_invocations` 的 `prompt_tokens - cached_prompt_tokens` 与 `cached_prompt_tokens`。禁止用用户原文。

闲聊单轮（每 turn 一次 `chat_agent`）：

| 科目 | 3.6.1 生产（18.7h） | 3.6.2 目标 |
|---|---|---|
| cached p50 | 3456（停在 SESSION） | 明显高于 SESSION：连续追加后 cached 随近窗增长，或至少 p90 进入 history 段 |
| 满窗 uncached p90 | 4975 | 不劣于 3.6.1；左沿冻结后 **同会话后续轮次** uncached 应明显低于首轮 miss |
| Prompt 左沿 | 滑动 | `id == coverage_end+1` |
| 短消息第二刀 L0 | 基本没有 | pending 条数下降或 coverage 前进 |

不把整单 USD、思考输出、Memory 抽取列入本项目门禁。

---

## 7. 风险与回滚

| 风险 | 处理 |
|---|---|
| 同步 extractive 循环拖慢 assemble | 次数硬顶；超时/不变式错误跳过，不滑 |
| 第一轮未覆盖尾巴很大，cache miss 一次偏高 | 接受；后续 hit 摊还。禁止为了第一轮好看再滑 |
| 热尾改交集后压缩过狠、引用变差 | 只改配置 `raw_tail_events` / `raw_tail_characters`；around 工具仍在 |
| 群名片变化导致信封字节变、拼接失败 | 整段 history miss 一轮，左沿仍冻；不要因此恢复滑动 |
| 只合并 Commit 2 未合并 4 | 预抽开始工作，Prompt 仍可能滑；发布必须以 1–6 齐 |

回滚：按 commit 逆序。Commit 2 与 4 不要单独长期留在 main。

---

## 8. 给实现者的顺序约束

1. 先合同，再热尾，再 Service，再 Assembler。调换 2/3 可以，**4 不得早于 2 和 3**。
2. 先改测试断言再改生产路径时，同一个 commit 内必须两者一起，避免 main 上红测。
3. 不得为了过测试给固定例句加分支。
4. 不提交 `.cursor/`、`tmp/`、生产库、`.env`。
