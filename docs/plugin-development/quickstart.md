# 10 分钟创建第一个插件

以下命令均在 Yuki 仓库根目录执行，Python 必须为 3.12。

## 1. 建立目录

```bash
mkdir -p plugins/com.example.hello
```

目录名必须和 Manifest 的 `id` 完全相同。

## 2. 写 `plugin.toml`

```toml
id = "com.example.hello"
name = "Hello"
version = "0.1.0"
description = "最小 Hello 插件"
entrypoint = "hello_plugin:HelloPlugin"
plugin_api = "2.0"
yuki_requires = ">=3.5.3,<4.0"
permissions = ["command.register"]

[limits]
background_tasks = 0
http_concurrency = 1
storage_mb = 10
prompt_characters = 0
```

## 3. 写插件

```python
from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.models import PermissionLevel, StrictModel
from yuki_plugin_sdk.registrar import (
    CommandMetadata,
    CommandRegistration,
    PluginRegistrar,
)
from yuki_plugin_sdk.results import CommandResult


class HelloArguments(StrictModel):
    name: str = "世界"


class HelloPlugin:
    async def register(self, registrar: PluginRegistrar) -> None:
        async def hello(arguments: HelloArguments) -> CommandResult:
            return CommandResult(text=f"你好，{arguments.name}！")

        registrar.register_command(
            CommandRegistration(
                metadata=CommandMetadata(
                    name="hello",
                    description="确定性问候",
                    permission=PermissionLevel.USER,
                ),
                argument_model=HelloArguments,
                handler=hello,
            )
        )

    async def start(self, context: PluginContext) -> None:
        context.logger.info("hello plugin started")

    async def stop(self) -> None:
        pass
```

保存为 `plugins/com.example.hello/hello_plugin.py`。三个生命周期方法都必须是 `async def`。

## 4. 校验与测试

```bash
uv sync --frozen --extra dev
uv run qq-ai-bot-cli plugin validate plugins/com.example.hello
uv run qq-ai-bot-cli plugin test plugins/com.example.hello
```

也可以在 pytest 中使用测试 SDK：

```python
from pathlib import Path
from yuki_plugin_sdk.testing import run_plugin_contract_tests


async def test_contract() -> None:
    report = await run_plugin_contract_tests(Path("plugins/com.example.hello"))
    assert report.passed, report.error_category
```

## 5. 启用并批准

`.env`：

```dotenv
PLUGIN_SYSTEM_ENABLED=true
PLUGIN_DIRECTORY=plugins
```

重启 Host，再执行：

```bash
uv run qq-ai-bot-cli plugin discover
uv run qq-ai-bot-cli plugin inspect com.example.hello
uv run qq-ai-bot-cli plugin approve com.example.hello
uv run qq-ai-bot-cli plugin enable com.example.hello
```

Manifest 哈希绑定批准状态。只要权限、入口、版本或其他 Manifest 字段变化，就必须重新审阅和批准。

Docker 部署默认把宿主的 `./plugins` 只读挂载到 `/app/plugins`：

```bash
docker compose up -d --no-deps --force-recreate bot
docker compose logs -f bot
```

## 下一步

先阅读 [权限](permissions.md) 和 [安全模型](security.md)，然后从 [Echo 示例](../../examples/plugins/com.example.echo/README.md) 复制完整结构。
