# Yuki 养鲲游戏插件

这是 [`UBC2008/astrbot_plugin_kun_game`](https://github.com/UBC2008/astrbot_plugin_kun_game)
的 Yuki Plugin API 2.0 适配版，保留养成、PVP、BOSS、拍卖和群小游戏，并修复原实现中已确认的
负数资源、保存顺序、拍卖覆盖、文本 QQ 目标、数星星答案和重复处罚等问题。

插件只注册两个确定性命令：普通用户 `play` 与超级用户 `admin`。`*签到` 等短入口由 Yuki Host
静态绑定到 `play`，仍完整经过群/私聊准入、消息去重、入站账本、限流、权限、调用作用域和超时。

## 安装与启用

本仓库已包含插件目录。先在 `.env` 启用插件系统和静态直达绑定：

```dotenv
PLUGIN_SYSTEM_ENABLED=true
PLUGIN_DIRECT_COMMAND_BINDINGS={"*":"io.github.yuanyeyoutao.kun-game:play"}
```

校验、发现、审阅权限、批准并启用：

```bash
uv run qq-ai-bot-cli plugin validate plugins/io.github.yuanyeyoutao.kun-game
uv run qq-ai-bot-cli plugin test plugins/io.github.yuanyeyoutao.kun-game
uv run qq-ai-bot-cli plugin discover
uv run qq-ai-bot-cli plugin inspect io.github.yuanyeyoutao.kun-game
uv run qq-ai-bot-cli plugin approve io.github.yuanyeyoutao.kun-game
uv run qq-ai-bot-cli plugin enable io.github.yuanyeyoutao.kun-game
```

静态前缀和插件生命周期都在启动时装配，完成配置后重启 Bot：

```bash
docker compose restart bot
docker compose exec bot qq-ai-bot-cli plugin doctor io.github.yuanyeyoutao.kun-game
```

若不使用 Docker，把最后两条替换为重启本地 `qq-ai-bot` 并运行：

```bash
uv run qq-ai-bot-cli plugin doctor io.github.yuanyeyoutao.kun-game
```

插件停用、未批准或启动失败时，已配置的 `*` 绑定会失败关闭，不会回退 Planner。删除
`PLUGIN_DIRECT_COMMAND_BINDINGS` 中的绑定并重启，即可恢复原有星号消息处理路径。

## 使用

短入口示例：

```text
*签到
*孵化
*攻击 @真实玩家
*出售 20
*=42
```

调试或未配置直达绑定时，可使用长入口：

```text
/ai plugin run io.github.yuanyeyoutao.kun-game play 签到
```

普通命令包括：

- 基础：`签到`、`当前游戏`、`命令菜单`、`查阅属性`、`今日运势`、`阵亡名单`、`拍卖行`。
- 养成：`孵化`、`买蛋`、`砸蛋`、`喂食`、`磨炼`、`幻化`、`喝鸡汤`、`渡劫`、`放生`、`复活`。
- PVP：`吞噬`、`攻击`、`强袭`、`扔蛋`；必须恰好提及一个当前群中的真实玩家，纯文本 QQ 号无效。
- BOSS：`查询BOSS`、`攻击BOSS`、`吞噬BOSS`、`强袭BOSS`。
- 拍卖：`出售`、`出价`、`成交`。
- 其他：`免疫强袭`、`免疫吞噬`、`免疫攻击`、`查骰子`、`奥数比赛`、`数星星`、`抄作业`、`抽群主一个大嘴巴`、`单挑群主`、`=答案`。

未知星号命令只返回养鲲帮助，不进入 Planner。“绑定群”首版暂不支持；私聊与群聊是两个明确独立的
状态空间。私聊允许 `签到`、`孵化`、`砸蛋`、`磨炼`、`幻化`、`查阅属性`、`今日运势`、
`命令菜单`、`喝鸡汤` 和 `当前游戏`，PVP、BOSS、拍卖和群小游戏仍只允许群聊。

## SUPERUSER 管理

管理动作不能通过 `*` 或 `play` 执行，只接受 Yuki 的真实 `SUPERUSERS` 长入口：

```text
/ai plugin run io.github.yuanyeyoutao.kun-game admin 鲲开
/ai plugin run io.github.yuanyeyoutao.kun-game admin 鲲关
/ai plugin run io.github.yuanyeyoutao.kun-game admin 小游戏开
/ai plugin run io.github.yuanyeyoutao.kun-game admin 小游戏关
/ai plugin run io.github.yuanyeyoutao.kun-game admin 刷新BOSS
/ai plugin run io.github.yuanyeyoutao.kun-game admin 重置局状态
/ai plugin run io.github.yuanyeyoutao.kun-game admin 清除全群数据 <当前群号>
/ai plugin run io.github.yuanyeyoutao.kun-game admin 强制下架
/ai plugin run io.github.yuanyeyoutao.kun-game admin 修改 <QQ号> <项目> <数值>
```

如果当前群已被 Yuki Host 禁用，先用 Yuki 原生管理命令启用该群；插件不申请或绕过 Host 群准入权限。

`重置局状态` 保留玩家养成资源，只清理 BOSS、拍卖、阵亡记录和当前小游戏；拍卖托管中的鲲会先安全
返还卖家。`清除全群数据` 会删除本群全部玩家与局状态，必须把当前群号作为确认参数。

## 经济配置

保留原插件的经济参数，并支持全局配置与当前群覆盖：

| 配置项 | 默认值 |
|---|---:|
| `default_jie_cao`（初始节操） | 50 |
| `default_luck`（初始运势） | 50 |
| `egg_price`（蛋价） | 5 |
| `tribulation_cost`（渡劫消耗） | 10 |
| `train_daily_max`（每日磨炼上限） | 30 |
| `hatch_misfortune_rate`（悲属性孵化率） | 0.007 |

SUPERUSER 可查看或修改当前群覆盖值：

```text
/ai plugin run io.github.yuanyeyoutao.kun-game admin 配置
/ai plugin run io.github.yuanyeyoutao.kun-game admin 配置 蛋价 7
```

## 状态与故障语义

- 私有 KV 使用 `state/group:<gid>` 或 `state/private:<uid>`，不会把相同数字的群号与 QQ 号混在一起。
- 每个作用域由单进程锁串行化，并以整状态 CAS 提交；冲突时用相同消息时间和随机 seed 最多重算两次。
- 每日边界固定为 `Asia/Shanghai`，不受容器本地时区影响。
- 损坏状态会失败关闭，不会用默认值覆盖；首版不包含旧 `groups.json` 运行时迁移。
- 状态变更采用 at-most-once 执行。极少数情况下，状态可能已提交，但 QQ 回复发送失败，插件不会自动重放。

## 回滚

先执行 `qq-ai-bot-cli plugin disable io.github.yuanyeyoutao.kun-game`，再从 `.env` 删除对应
`PLUGIN_DIRECT_COMMAND_BINDINGS` 并重启 Bot。插件私有状态保留且不会注入 Planner；本版未新增
数据库迁移，无需删除或逆向修改业务表。

## 权限与许可

Manifest 只申请命令注册、当前可信消息、当前人物显示名、插件私有 KV 和插件自身配置读写；不申请网络、
LLM、Agent、OneBot、主动发送、Host 运行时配置、Secret 或后台任务权限。

本适配版按 MIT License 发布。原始玩法和 AstrBot 移植的著作权与归属见 [NOTICE](NOTICE) 和
[LICENSE](LICENSE)。
