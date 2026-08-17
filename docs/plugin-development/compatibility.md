# 兼容性

Yuki 产品版本、Plugin API、Feature 和各 Schema 版本相互独立。

## Plugin API

- 当前 `2.0`；同一主版本内保持向后兼容。
- `1.0` / `1.1` 在导入插件代码前被拒绝，没有半加载兼容层。
- 新字段优先为可选并提供默认值。
- 删除字段前至少两个次版本标记 deprecated。
- 主版本不一致时拒绝加载。
- 插件不得依赖 `_` 开头属性、Host 类或数据库表结构。

从 1.x 升级见 [API 2.0 迁移](api-2.0-migration.md)。

Manifest 同时使用：

```toml
plugin_api = "2.0"
yuki_requires = ">=3.5.3,<4.0"
```

## Feature 探测

```python
if ctx.features.has("admission.signal.v1"):
    ...
ctx.features.require("plugin.agent_session.v1")
```

2.0 默认 Feature 包括：

- `message.normalized.v1`
- `message.current.mentions.v1`
- `prompt.fragment.v1`
- `admission.signal.v1`
- `automation.action.v1`
- `plugin.agent_session.v1`
- `emoji.facade.v1`
- `emoji.selection_signals.v1`
- `speech.facade.v1`
- `mcp.facade.v1`
- `notification.facade.v1`
- `media.artifact.v1`
- `http.credential.v1`

不要因为 Yuki 版本“看起来足够新”就假设部署者启用了某 Feature。

## Schema 版本

工具、自动化 Action 和 Event 各自声明 Schema 版本。改变必填字段、类型或语义属于不兼容变更，应新建组件名或提升 Schema 并迁移；旧 Automation 不会自动套用新 Schema。
