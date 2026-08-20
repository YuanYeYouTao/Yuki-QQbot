# Yuki 3.7.1 烧钱扫描剩余缺陷任务书

本文是 3.7.1 hotfix 的实施合同。扫描板剩余 6 条缺陷（P0 与首轮 `tools[]` 已在 #41–#43 修完）。观察路径已拍板：**Bot 不回复则不入队 Memory V2**。

实施时若忘记细节，回到本文件对齐。每个 bug 一个 commit；commit message 必须使用本文件给出的标题。不要凭记忆扩 scope。

## 工作方式

1. 先有本任务书，再改代码。
2. 每修完一个 bug 一个 commit；commit 前重读该条。
3. 机制通用，阈值 / origins 进 env；不硬编码群名或例句。
4. 禁止动 NapCat；生产禁止现场 `docker build`。
5. 分支：`hotfix/3.7.1-money-burn`。无新 Alembic。

## 已排除（不要当新 bug 修）

- 前台 `raw_tail+1` 每轮啃：admit = 热尾 + trigger，target = 热尾 + stop。
- 首轮工具跟当前句子检索走：`plan_initial` 丢掉 query/hits；`decline_reply` / `automation_create` 已走 `request_tools`。
- 时间、人物卡进公共前缀：TURN，挂在当前消息。
- 带 URL 就把 Tavily 钉进首轮：只在 `WEB_MODE=tavily`。
- 每张历史图重跑视觉：当前消息一次，结果写入 `visual_summary`。
- 表情选择每轮打视觉：仅显式要表情且分数接近才走 `vision_grid`。
- 记忆抽取失败死循环：3 次封顶 + 退避。
- Dream incremental / 自我反思日预算：已有 cap。Dream FULL 无 cap 与 self-reflection fail cursor 不在本扫描板剩余项里，本轮不修。

---

## Commit 0 — 任务书

本文件。不写实现代码。

**Commit：** `docs: add 3.7.1 money-burn hotfix taskbook`

---

## Bug A（P1）— 投影字符与 Prompt 渲染不是同一把尺子

### 根因

水位、未覆盖计数、`protected_tail_start`、job 触发、批大小切分全部走 `rollup_source_projection` / `projection_characters`（`src/qq_ai_bot/conversation/rollup/renderer.py`）：`[ISO时间] 发送者: 正文`，可选 `[Visual summary: …]`，不含 `#id`、回复链、提及、QQ 行。

前台 `_uncovered_fits_window` 与 `_bounded_history` 走 `ChatEventPromptRenderer.main_agent_history`（`src/qq_ai_bot/event_prompt.py`）：`[昵称|QQ:…]`、`#id`、`回复:#target/身份`、`提及:…`。`rendered_characters` 是分组后各 `ChatMessage.content` 长度之和。

结果：后台计数器认为未过 admit → `_ensure_lightweight_backlog` 不压；加载正文后 `_ensure_uncovered_fits_budget` 按 Prompt 渲染发现已超 → 同步 extractive → 改写 rollup checkpoint → `input[0]` 变了，DeepSeek 前缀缓存被打回。

### 修法

1. 新增与 `_uncovered_prompt_view` 同源的计量 API，建议放 `src/qq_ai_bot/conversation/rollup/prompt_accounting.py`（或 `event_prompt.py`）：
   - `prompt_accounting_characters(events, *, bot_display_name, timezone) -> int`：构造 `ChatEventPromptRenderer`，对 suffix 调用 `main_agent_history`，对分组后的 content 求和。必须与 assembler 的 `_uncovered_prompt_view` 同一把尺子。
   - `prompt_accounting_event_characters(event, *, events, bot_display_name, timezone) -> int`：ungrouped `len(render_reference_event(row))`，供 append 增量上界。
2. **只替换水位 / 计数 / `protected_tail_start` / `exceeds_*` / `take_batch` / `recount_scope_uncovered` / `scoped_event_uow` 增量。**
3. **保留** `rollup_source_projection` 作为压缩模型 / extractive 的输入，不要改摘要输入格式。
4. append 用 ungrouped 增量（上界，宁可略高）；`recount_scope_uncovered` 用 grouped 精确值校正。换尺后必须能走 recount，避免库存 `uncovered_character_count` 仍是旧投影。
5. 不要只调大 `trigger_characters` / `trigger_events`。不要改 `render_reference_event` 语义。
6. `protected_tail_start` 的 `max(count_index, character_index)` 语义保持：更晚下标 = 更短热尾。

必改文件（按需增减，不要扩到无关模块）：

- `src/qq_ai_bot/conversation/rollup/prompt_accounting.py`（新）
- `src/qq_ai_bot/conversation/rollup/renderer.py`（计数委托或保留投影给压缩输入）
- `src/qq_ai_bot/conversation/rollup/repository.py`
- `src/qq_ai_bot/persistence/scoped_event_uow.py`
- `src/qq_ai_bot/event_prompt.py`（若计量 API 放这里）
- `tests/unit/test_conversation_rollup_370.py`

### 验收

```text
pytest tests/unit/test_conversation_rollup_370.py -q
```

必须新增或扩展断言：

- 同一批带回复 / @ 的 `EventRecord`：`prompt_accounting_characters(events) ==` assembler 同源渲染长度。
- `sum(projection_characters) < prompt_accounting_characters`（回复链夹具）。
- `recount_scope_uncovered` 写入的 `uncovered_character_count` 等于 grouped Prompt 计量。
- `test_foreground_does_not_nibble_between_protected_tail_and_trigger` 改用 Prompt 计数后仍通过。

**Commit：** `Align rollup watermarks with the prompt renderer character ruler.`

---

## Bug B（P2）— 256 条热尾可能超过 20480 字目标

### 根因

`protected_tail_start` 取 `max(count_index, character_index)` = 更晚下标、更短热尾。`character target = raw_tail_characters + stop_characters(0) = 20480`。256 条 floor 绑定时 eligible 可为空。最终检查用 **admit 不是 target**，是滞回不是死循环。

Bug A 修好后：长消息会按 Prompt 尺缩短热尾。短消息 256 条超过 20480 是「条数 floor 优先于字符帽」的产品张力。

### 修法（单独 commit，不压热尾）

- 不破坏 256 条语义，不把 target 改成 fail-closed。
- 不把 `CONVERSATION_ROLLUP_RAW_TAIL_CHARACTERS` 再调一轮当主修复（256+1024 水位保持）。
- 在 `docs/architecture/conversation-rollup.md` 写清：计数 = Prompt `main_agent_history` 长度；压缩输入仍是 `rollup_source_projection`；256 条 floor 优先于 20480 字符 target；最终前台检查用 admit。
- 补 `protected_tail_start` 与「最终检查用 admit」回归。

### 验收

```text
pytest tests/unit/test_conversation_rollup_370.py tests/unit/test_config.py -q
```

- 长消息：`character_index > count_index`，`eligible_prefix` 非空。
- 256 条短消息且 Prompt 字符在 `(target, admit)`：不抛 `ConversationCoverageError`，不每轮 extractive。

**Commit：** `Document the 256-event tail floor versus the character target.`

---

## Bug F（P1）— 群 observe 仍抽取 Memory V2

### 根因

`src/qq_ai_bot/services/processor.py` 在 `group_observed` 返回之前，只要 `created && command is None && direct_match is None` 就 `memory_worker.enqueue`。Bot 沉默的启用群每 12 条一批 `extract_batch`，`memory_consolidation_enabled` 默认再打分类模型。

`observe_enabled_groups` 只控制是否写 ledger / 走 observe，不控制 Memory V2。

### 修法（已拍板 skip-observe）

```python
direct_turn = decision.should_respond or admin_candidate
if created and decision.command is None and direct_match is None and direct_turn:
    await self._memory_worker.enqueue(...)
```

仍写 ledger / profile / `autonomous.observe`。私聊恒 `should_respond=True`，不受影响。命令与 plugin direct 原守卫保留。不要写死群名。

### 验收

```text
pytest tests/unit/test_v1_person_agent.py tests/unit/test_memory_v2.py tests/unit/test_plugin_direct_commands.py -q
```

- 未 @ / 未 prefix / 未 reply Bot 的启用群：`reason=group_observed` 且该事件 **无** memory job。
- `mentions_bot=True` 的群聊：仍 enqueue。
- plugin direct / `/ai` 命令：仍不 enqueue（已有守卫）。

**Commit：** `Skip Memory V2 enqueue on observe-only group turns.`

---

## Bug C（P1）— 同一张图 Vision + Emoji 双通道

### 根因

带图且 `should_respond || admin_candidate` 时，processor 先 `EmojiCollector.submit`，再 `_analyze_visual_input`。`media_analyses` 缓存按 `analysis_mode` 隔离：Turn Vision 常用 `question` / `character`；Emoji 固定 `meme` + `analysis_version` 后缀。同 `content_hash` 在 turn 完成后 Emoji 仍 miss。

Qwen 低置信：Vision 路最多再 review 一次；Emoji 路 `low_confidence_retry_threshold=0`，不 review。observe 带图只走 Emoji，不走 Turn Vision。`vision_grid` 是显式选表情的第三条路。

### 修法

Turn Vision persist 成功后，对**单图**再写一条通用 `meme` 别名到 `media_analyses`：

- `content_hash` = 该图 hash
- `analysis_mode = "meme"`
- `question_hash = ""`
- `prompt_version` 必须能被 `EmojiClassifier.find_latest_for_content(..., prompt_version_suffix=analysis_version)` 命中

不合并两路 prompt。不把 `vision_grid` 算进「同一张用户图」验收。不要写死场景词。

### 验收

```text
pytest tests/unit/test_vision_emoji_bridge.py tests/unit/test_emoji_system.py -q
```

- 先跑 Turn Vision（question 模式）→ bridge 写入 → `EmojiClassifier` 0 次 provider。
- observe 带图：Turn Vision 0 次（现有行为保持）。
- 现有单路 emoji cache 回归仍过。

**Commit：** `Reuse turn vision observations for emoji classification cache.`

---

## Bug D（P1）— 群自主门槛生产是 0

### 根因

`Settings.conversation_autonomous_admission_threshold` 默认 `0`（`src/qq_ai_bot/config.py`）。`LocalAutonomousParticipationPolicy` 类默认 `80`。运行时用 Settings / env，不是类默认。群基础分 10，`score >= 0` 即开一整轮主 Agent。生产 `.env` 写死 0。Admin 该键是 HOT，但现网走 Settings。

### 修法

- 代码默认改为 `80`。
- `.env.example`、`docs/help.md` 同步为 `80`，注明 `0` 几乎总是插话。
- 部署脚本把生产 `.env` 的 `CONVERSATION_AUTONOMOUS_ADMISSION_THRESHOLD` 写成 `80`。只 recreate bot。不写死群 ID。

### 验收

```text
pytest tests/unit/test_config.py tests/unit/test_conversation_runtime_r4.py -q
```

- Settings 默认 == 80。
- `threshold=80` + 群 `"哈哈"` → 不开 Agent。
- `threshold=0` + score=10 → 仍开（行为锁，证明门槛真的在用）。

**Commit：** `Raise the default autonomous admission threshold to 80.`

---

## Bug E（P2）— `CONVERSATION_ROLLUP_LLM_ORIGINS` 是假配置

### 根因

键在 `src/qq_ai_bot/config.py`，`.env.example` 声称「默认只有 user_message 走 Flash」。`rollup/` 零引用。Worker 每批先 `CONVERSATION_COMPACTION`，失败再 extractive。入站 ledger 几乎写死 `origin="user_message"`；`plugin_background` 已有打标。自主轮次 origin 在 turn coordinator，出站常仍标 `user_message`。

### 修法（接线，不删键）

1. 解析 `settings.conversation_rollup_llm_origins` 为 `frozenset[TurnOrigin]`，注入 `RollupPolicyConfig` / `ConversationRollupService`。空 → 默认 `{USER_MESSAGE}`。
2. Worker / `summarize_candidate`：batch 内没有任何 `event.origin in llm_origins` → 直接 extractive，零模型。混合 batch **整批 extractive**（保守，不烧模型）。
3. 诚实打标：插件后台保持 `plugin_background`；自主轮次出站标 `autonomous_group`。人类入站（含 observe）仍是 `user_message`。不要发明 `group_observation`。
4. 不要硬编码群号；origins 留 env。

### 验收

```text
pytest tests/unit/test_conversation_rollup_370.py tests/unit/test_conversation_rollup_llm_origins.py -q
```

- `llm_origins={plugin_background}` 时，human `user_message` batch **不**调 ModelExecutor。
- 纯 `plugin_background` batch extractive。
- 混合 batch 整批 extractive。
- `RollupSourceChangedError` 回归仍过。

**Commit：** `Honor CONVERSATION_ROLLUP_LLM_ORIGINS in the rollup worker.`

---

## Commit 7 — 版本 3.7.1

四面必须一致（`scripts/release_validate.py`）：

- `pyproject.toml`
- `src/qq_ai_bot/__init__.py`
- `uv.lock`
- `src/qq_ai_bot/memory/quality/release_check.py`

文档 / 测试：

- 新建 `docs/releases/v3.7.1.md`
- `CHANGELOG.md` 增加 `## 3.7.1`
- `docs/help.md` 顶部正式版说明
- `.env.example` 的 `YUKI_VERSION`
- `tests/unit/test_versioned_docker_release.py` 的 `VERSION`
- `tests/unit/test_guided_setup.py` 的版本断言

无新 Alembic。Plugin API 仍 `2.0`。Worker 组件版本保持 `1.9.0`。

**Commit：** `Release Yuki 3.7.1.`

---

## 上线 / PR / Release / 关机

1. 本机：

```text
docker buildx build --platform linux/amd64 --load \
  --build-arg YUKI_VERSION=3.7.1 \
  --build-arg VCS_REF=<full-sha> \
  --tag qq-ai-bot-bot:3.7.1-<short-sha> .
```

2. `docker save` → scp → 生产 `docker load`。只执行：

```text
docker compose up -d --no-deps --no-build --force-recreate bot
```

3. 生产 `.env` 写入 `CONVERSATION_AUTONOMOUS_ADMISSION_THRESHOLD=80`。断言 healthz `version=3.7.1`、`onebot_connected`，NapCat 容器 ID 不变。
4. 推分支，开 PR，合入 `main`。
5. 在合入后的 `main` SHA 上 `git tag v3.7.1 && git push origin v3.7.1`。
6. 等 Release workflow：**Quality 全绿且 GitHub Release `v3.7.1` 已发布** 才算成功。失败不关机。
7. 成功后本机 `shutdown /s /t 600`（可用 `shutdown /a` 取消）。
