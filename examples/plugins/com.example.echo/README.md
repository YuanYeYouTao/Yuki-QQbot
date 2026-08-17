# com.example.echo

这是 Yuki Plugin API 2.0 的最小完整示例。它演示：

- 普通用户可用工具 `echo_text`；
- 确定性命令 `echo`；
- `reply.sent` 通知 Hook；
- `plugin_context` Prompt Fragment；
- 普通用户可委托的自动化 Action `echo_later`；
- 插件私有 KV 计数；
- `global`、`user`、`group` 三种配置作用域；
- 使用独立 Fake Facade 的无网络测试。

插件代码只导入公开的 `yuki_plugin_sdk`（以及 handler 类型边界使用的 Pydantic `BaseModel`），不导入 Yuki Host 内部模块，不访问网络、文件、数据库、NoneBot 或 NapCat。

在仓库根目录运行：

```bash
uv run pytest -q examples/plugins/com.example.echo/tests
```

复制到运行目录并启用插件系统：

```bash
mkdir -p plugins
cp -R examples/plugins/com.example.echo plugins/com.example.echo
```

然后在 `.env` 设置 `PLUGIN_SYSTEM_ENABLED=true`，通过插件 CLI 发现、检查并批准 Manifest 中的权限。插件目录名必须与 `id` 完全一致；Manifest 或权限发生变化后必须重新批准。

完整说明见 [插件开发手册](../../../docs/plugin-development/index.md)。
