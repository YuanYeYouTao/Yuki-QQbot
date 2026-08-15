# 调试

先在仓库根目录运行：

```bash
uv run qq-ai-bot-cli plugin validate plugins/com.example.plugin
uv run qq-ai-bot-cli plugin inspect com.example.plugin
uv run qq-ai-bot-cli plugin permissions com.example.plugin
uv run qq-ai-bot-cli plugin doctor com.example.plugin
uv run qq-ai-bot-cli plugin test plugins/com.example.plugin
```

## 常见状态

| 状态/错误 | 处理 |
|---|---|
| `invalid` | 检查 TOML、ID/目录、版本、entrypoint、权限和网络域名 |
| `incompatible` | 检查 `plugin_api` 与 `yuki_requires` |
| `pending_approval` | Manifest 新增或变化，重新审阅后批准 |
| `failed` | 查看脱敏错误类别；单插件失败不会影响主聊天 |
| `RegistrationError` | 名称/别名重复、Prompt 阶段或 Schema 不合法 |
| `PluginPermissionError` | Manifest 未声明、未批准或当前轮被安全策略撤销 |
| Hook 超时 | 缩短 Hook，把工作交给托管任务 |
| Automation `blocked` | 插件禁用、批准撤销或 Action Schema 变化 |

## 日志纪律

使用 `ctx.logger`，记录插件 ID、组件名、耗时、计数和稳定错误类别。不要记录：Secret/API Key、完整聊天正文、完整 Prompt、隐藏推理、Base64、OCR 全文、签名 URL 或原始高风险 OneBot 返回。

## Docker

```bash
docker compose config
docker compose up -d --no-deps --force-recreate bot
docker compose logs -f bot
```

确认 `./plugins:/app/plugins:ro` 已挂载，`.env` 中 `PLUGIN_DIRECTORY=plugins`。插件代码变化目前需要重启 Bot；1.6.0 不提供热更新。
