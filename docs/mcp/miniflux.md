# Miniflux RSS MCP

Yuki 可通过 `tssujt/miniflux-mcp v0.4.0` 管理 RSS 订阅源、文章和分类。正式部署使用同机
Docker 私有网络；公网 `8078` 是否保留由部署者决定，私网接入不会改变它的端口映射。

## Yuki 配置

将 [`.mcp.json.example`](../../.mcp.json.example) 中的 `miniflux` 配置同步到
`config/mcp.json`，把 `disabled` 改为 `false`，并在 `.env` 设置：

```dotenv
MCP_ENABLED=true
MCP_CONFIG_PATH=/app/config/mcp.json
MINIFLUX_MCP_TOKEN=与 Miniflux MCP_AUTH_TOKEN 相同的值
```

配置使用 `http://miniflux-mcp:8080/mcp`，不会绕到服务器公网 IP。Token 只能通过环境变量
引用，不能写入 MCP JSON、日志或 Git。

## 共享网络

只需创建一次外部网络：

```bash
docker network create yuki-miniflux-network
```

Yuki 通过附加 Compose 文件启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.miniflux.yml up -d --no-deps bot
```

Miniflux MCP 的 Compose 服务需要同时加入自身默认网络和该外部网络，并设置网络别名：

```yaml
services:
  mcp:
    networks:
      default: {}
      yuki-miniflux-network:
        aliases: [miniflux-mcp]

networks:
  yuki-miniflux-network:
    name: yuki-miniflux-network
    external: true
```

保留以下映射即可继续通过公网调试，不影响 Yuki 使用私网：

```yaml
ports:
  - "0.0.0.0:8078:8080"
```

## 工具权限

- 普通聊天允许管理订阅源、文章和分类，包括明确的删除操作。
- 自动化只允许查询订阅、文章、分类、计数和健康状态，不允许后台删除或修改。
- 用户管理、API Key 管理、`export` 和 `flush_history` 不在 `includeTools` 中。
- Capability Runtime 按 `mcp.miniflux.subscriptions`、`mcp.miniflux.articles` 和
  `mcp.miniflux.categories` 三个小型 namespace 检索，主 Agent 无需一次加载全部 Schema。

可用命令：

```text
/ai mcp status
/ai mcp show miniflux
/ai mcp tools miniflux
/ai mcp refresh miniflux
/ai mcp doctor miniflux
```
