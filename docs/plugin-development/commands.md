# 确定性插件命令

命令不经过 LLM，适合状态查询、开关和参数明确的操作。需要 `command.register`。

```python
from yuki_plugin_sdk.models import PermissionLevel, StrictModel
from yuki_plugin_sdk.registrar import CommandMetadata, CommandRegistration
from yuki_plugin_sdk.results import CommandResult


class StatusArguments(StrictModel):
    verbose: bool = False


async def status(arguments: StatusArguments) -> CommandResult:
    text = "插件运行正常"
    if arguments.verbose:
        text += "；无待处理任务"
    return CommandResult(text=text)


registrar.register_command(
    CommandRegistration(
        metadata=CommandMetadata(
            name="status",
            description="查看插件状态",
            short_alias="demo-status",
            permission=PermissionLevel.USER,
        ),
        argument_model=StatusArguments,
        handler=status,
    )
)
```

本地名匹配 `[a-z][a-z0-9_-]{0,63}`，短别名最多 32 字符。核心 `/ai` 名称和别名（如 `help`、`status`、`memory`、`plugin`、`stop`）保留，冲突会使注册失败。

命令参数同样要求 `extra='forbid'` 并由 Host 做类型校验。`CommandResult.text` 最多 12000 字符；失败时使用 `ok=False` 和稳定 `error_code`，不要在错误详情放 Secret 或原始外部响应。

命令只在当前真实消息上下文执行，不能从参数伪造超级管理员或跨群目标。需要发送、配置写入等操作时仍须相应 Facade 权限。

## Host 管理的静态直达绑定

部署者可在启动时把消息前缀直接绑定到一个已注册命令，无需修改插件元数据：

```dotenv
PLUGIN_DIRECT_COMMAND_BINDINGS={"*":"io.github.example.game:play"}
```

直达绑定只接受已批准、已启用、正在运行的命令。前缀可以是 `/github` 这类独立命令；Host 会拒绝空白、控制字符、与 `AI_PREFIX` 重叠，以及相同或互为前缀的配置。命令声明的 `USER` / `TRUSTED` / `MODERATOR` / `SUPERUSER` 权限仍在执行时根据真实发送者校验。匹配只是明确触发信号；准入、持久去重、入站账本、命令限流、参数校验、真实 `ToolRuntime`、权限、调用作用域和超时都与普通确定性命令相同。

已配置但暂时不可用的绑定会被消费并返回稳定错误，不会落入自主群评分或 Main Agent。SUPERUSER 直达命令只有真实超级用户能执行，普通用户会收到权限不足。
