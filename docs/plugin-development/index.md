# Yuki Plugin API 1.1 开发手册

Yuki 1.6.0 提供 Plugin API `1.0`：插件通过独立的 `yuki_plugin_sdk` 声明扩展，由 Host 负责发现、批准、生命周期、权限裁剪和运行时 Facade。插件不能直接取得 `ApplicationContainer`、数据库 Session、NoneBot Bot、原始事件、完整设置或任何密钥集合。

Yuki 2.0.0 不改变 Plugin API `1.0` 的公共含义。插件 Prompt Fragment 会先由现有 `PromptRegistry` 校验，再作为一个 `context.plugins` 不可信贡献进入统一 Runtime Envelope；不会为每个插件重复一层系统包装。插件 Agent 会话通过 `ModelTask.PLUGIN_AGENT_SESSION` 使用显式模型路由，其独立上下文、持久/临时会话和权限行为保持兼容。插件工具会被适配为 `CapabilityDescriptor`，最终可见性由批准权限、调用来源、effect/risk 和 Planner 工具组共同决定；改名不应被当作安全策略。

Yuki 3.4.1 将 Plugin API 扩展为 `1.1`，新增受 Host 管理的后台服务、媒体制品、持久外部通知、
受控 Agent 点评和 HTTP Secret credential 注入；`1.0` 的现有注册与 Facade 语义保持兼容。

> **真实安全边界：**1.6.0 插件是运行在 Yuki 进程内的本地可信 Python 代码。权限系统治理的是官方 API 的访问，不是操作系统沙盒；恶意插件理论上仍能绕过约束。只安装管理员完全信任、审阅过源码的插件。

## 从这里开始

- [10 分钟快速开始](quickstart.md)
- [架构与数据流](architecture.md)
- [Manifest v1](manifest.md)
- [生命周期](lifecycle.md)
- [权限与风险](permissions.md)
- [服务 Facade 与独立 AI 会话](service-facades.md)
- [事件](events.md)
- [Prompt Fragment](prompts.md)
- [PlannerSignal](planner-signals.md)
- [Agent 工具](tools.md)
- [确定性命令](commands.md)
- [自动化 Action](automation.md)
- [配置与 Secret](config-and-secrets.md)
- [私有 KV Storage](storage.md)
- [网络访问](networking.md)
- [媒体与视觉](media-and-vision.md)
- [后台服务](background-services.md)
- [测试](testing.md)
- [调试](debugging.md)
- [发布](publishing.md)
- [兼容性](compatibility.md)
- [安全模型](security.md)
- [从内部扩展迁移](migration-guide.md)

## API Reference

- [Permission Catalog](api-reference/permissions.md)
- [Event Catalog](api-reference/events.md)
- [Facade Protocol](api-reference/facades.md)
- [Registrar Schema](api-reference/registrar.md)
- [Result 与会话模型](api-reference/results.md)
- [EmojiFacade 与选择信号](api-reference/emoji.md)

仓库中的 [`com.example.echo`](../../examples/plugins/com.example.echo/README.md) 是可运行的无网络参考实现，覆盖工具、命令、事件、Prompt、普通用户自动化、配置和 KV。

## 版本标识

| 标识 | 当前值 | 用途 |
|---|---:|---|
| Yuki | `3.5.0` | Host 产品版本 |
| Plugin API | `1.1` | SDK 主兼容边界 |
| Event/Tool/Automation Schema | `1` | 单类载荷的结构版本 |
| Feature | 如 `planner.signal.v1` | 运行时能力探测 |

不要通过 Yuki 次版本号猜测功能。使用 `ctx.features.has(...)` 或 `require(...)`。
