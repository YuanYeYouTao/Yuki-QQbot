# 插件生命周期

插件入口必须实现：

```python
class Plugin:
    async def register(self, registrar: PluginRegistrar) -> None: ...
    async def start(self, context: PluginContext) -> None: ...
    async def stop(self) -> None: ...
```

## 状态

`discovered → pending_approval → approved → registered → starting → running → stopping → disabled`

校验失败可进入 `invalid`/`incompatible`，运行失败进入 `failed`。一个插件失败不会阻塞其他插件或 Yuki 主聊天。

## `register`

只声明工具、命令、通知 Hook、Prompt Fragment、自动化 Action、AdmissionSignal、配置 Schema 和后台服务。不得在此阶段：

- 使用 LLM、网络、QQ、用户资料或 KV；
- 启动 `asyncio.Task`；
- 读取 Secret；
- 假设 `PluginContext` 已存在。

注册项会验证权限、Pydantic `extra='forbid'`、名称冲突和 Prompt 阶段。核心命令别名不可覆盖。

## `start`

批准后 Host 才提供 `PluginContext`。可以读取配置、初始化私有 KV，并让 Host 调度已声明后台服务。应保持快速；超过 `PLUGIN_START_TIMEOUT_SECONDS` 会导致该插件启动失败。

不要使用裸 `asyncio.create_task()` 创建永久任务。通过 `ctx.scheduler` 或注册 `BackgroundServiceRegistration`，这样 Host 才能跟踪和取消。

## `stop`

停止新工作、释放插件自己创建的内存资源。Host 会取消托管任务并等待不超过 `PLUGIN_STOP_TIMEOUT_SECONDS`。`stop()` 应幂等：初始化未完成或调用两次都不应破坏进程。

## 超时与失败

- Hook 超时/异常：记录该 Hook 失败，聊天继续。
- 后台服务失败：按声明的 `RestartPolicy` 和 Host 失败阈值处理。
- 插件启动失败：仅禁用该插件。
- 持久用户计划：必须使用 Automation，而不是后台 Task。

插件系统默认关闭；`PLUGIN_SYSTEM_ENABLED=false` 时 Host 不扫描外部目录。

