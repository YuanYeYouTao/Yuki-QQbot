# 配置

配置按 `user > group > global > .env > 代码默认值` 解析。核心键：

| 组 | 键 |
|---|---|
| 开关/收集 | `emoji.enabled`、`collection_enabled`、`collection_mode`、`collect_private`、`collect_group` |
| 采用/容量 | `auto_adopt_enabled`、`auto_adopt_min_confidence`、`pool_capacity`、`replacement_mode` |
| 选择/冷却 | `spontaneous_frequency`、`selector_enabled`、`selector_candidate_count`、`selector_score_gap`、`selector_timeout_seconds`、`same_emoji_cooldown_seconds`、`scope_repeat_cooldown_seconds` |
| 去重/维护 | `near_duplicate_enabled`、`near_duplicate_distance`、`cache_retention_days`、`analysis_version` |
| Worker | `worker_batch_size`、`worker_poll_seconds`、`worker_lease_seconds`、`worker_max_attempts`、`worker_retry_delay_seconds` |

`selector_candidate_count` 默认是 3。日常 `optional` 表情直接采用本地描述和标签评分第一名；只有明确索要表情且前两名分差不超过 `selector_score_gap` 时才调用视觉精选，并受 `selector_timeout_seconds` 短超时约束。视觉调用超时或失败会立即回退本地第一名。

`spontaneous_frequency` 默认是 `0.15`。Conversation Runtime 使用现有近期账本中的真实表情投递比例控制日常 `optional`，不新增模型调用或数据库查询；用户明确索要表情时不受该频率限制。该配置支持全局、群和用户作用域，可通过 `admin_set_config` 热修改。

`pool_capacity` 未设置表示无限；两个 cooldown 都允许 `0` 表示关闭。`storage_root` 和预览尺寸是启动配置。不存在 `emoji.review_enabled`。

确定性表情意图识别、真实回执校验和失败文字属于固定正确性契约，不能通过放宽工具范围关闭。
本次频率配置不需要数据库迁移。
