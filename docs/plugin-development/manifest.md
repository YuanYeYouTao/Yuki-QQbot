# Plugin Manifest v1

每个插件根目录必须有 UTF-8 `plugin.toml`，目录名必须等于 `id`。

## 完整语法

```toml
id = "com.example.weather"
name = "Weather"
version = "0.1.0"
description = "天气查询和提醒插件"
entrypoint = "weather_plugin:WeatherPlugin"
plugin_api = "2.0"
yuki_requires = ">=3.5.3,<4.0"

permissions = [
  "message.current.read",
  "llm.generate",
  "network.http.allowlisted",
  "tool.register",
]

[network]
allowed_hosts = ["api.weather.example"]

[limits]
background_tasks = 2
http_concurrency = 4
storage_mb = 50
prompt_characters = 2000
```

## 顶层字段

| 字段 | 规则 |
|---|---|
| `id` | 1–128 字符，仅小写字母、数字、点和连字符；推荐反向域名 |
| `name` | 1–128 字符 |
| `version` | 有效 PEP 440 版本 |
| `description` | 1–1000 字符 |
| `entrypoint` | `module.path:Symbol`，模块必须位于插件根目录内 |
| `plugin_api` | `MAJOR.MINOR`；当前为 `2.0`，其它主版本拒绝加载 |
| `yuki_requires` | PEP 440 Specifier，例如 `>=3.5.3,<4.0` |
| `permissions` | 去重后的已知权限列表；未知值拒绝加载 |

保留命名空间包括 `qq_ai_bot`、`qq-ai-bot`、`yuki`、`core` 和 `system`，也不能使用这些前缀冒充核心插件。

## `network`

`allowed_hosts` 最多 128 个，只能填写精确公共主机名，不能含 scheme、端口、路径、通配符、用户信息或查询串。`localhost`、`.local`、`.internal`、`.lan`、非全局 IP 均拒绝。填写此表时必须声明 `network.http.allowlisted` 或 `network.http.unrestricted`。

## `limits`

| 字段 | 默认 | 范围 |
|---|---:|---:|
| `background_tasks` | `0` | 0–64 |
| `http_concurrency` | `1` | 1–64 |
| `storage_mb` | `10` | 1–10240 |
| `prompt_characters` | `2000` | 0–16000 |

Host 还会使用部署级全局上限取更小值；Manifest 不能扩大 Host 配置。

## 哈希与重新批准

Host 对规范化后的完整 Manifest 计算 SHA-256。批准记录绑定该哈希；任何字段变化都会使旧批准失效，插件回到 `pending_approval`。这能防止插件获批后静默增加权限。

```bash
uv run qq-ai-bot-cli plugin validate plugins/com.example.weather
uv run qq-ai-bot-cli plugin inspect com.example.weather
```
