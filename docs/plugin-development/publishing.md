# 发布插件

Plugin API 2.0 没有在线市场或自动下载。发布物是管理员手动审阅并复制到本地 `plugins/<id>/` 的源码目录。

## 发布前清单

1. `plugin.toml` 的目录、ID、版本、入口和权限准确。
2. `yuki_requires` 使用支持范围，`plugin_api = "2.0"`。
3. 权限最小化，高风险权限写明必要性和目标范围。
4. 不包含 `.env`、Token、Cookie、数据库、日志或用户数据。
5. 插件代码只依赖公开 SDK，不导入 `qq_ai_bot` 或 `_` 内部对象。
6. 提供无网络单元测试和 `run_plugin_contract_tests`。
7. 记录配置键、Secret 名称、网络白名单、存储容量和升级步骤。
8. `stop()`、失败和回滚行为经过测试。

```bash
uv run ruff check path/to/plugin
uv run pytest -q path/to/plugin/tests
uv run qq-ai-bot-cli plugin validate path/to/plugin
uv run qq-ai-bot-cli plugin docs path/to/plugin
uv run qq-ai-bot-cli plugin test path/to/plugin
```

## 安装方流程

安装方必须审阅源码和 Manifest，复制目录，执行 `discover/inspect/permissions`，再明确 `approve` 和 `enable`。升级后 Manifest 哈希改变，旧批准自动失效。

不要宣传“可安全运行任意第三方插件”。当前版本是本地可信、同进程模型；强隔离需要未来独立进程运行时。

