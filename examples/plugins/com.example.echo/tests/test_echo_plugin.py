"""Contract and behavior tests for the example plugin."""

from __future__ import annotations

import ast
import importlib.util
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import cast

from pydantic import BaseModel

from yuki_plugin_sdk.events import EventEnvelope, EventName
from yuki_plugin_sdk.models import CurrentMessage, PromptFragment, PromptStage
from yuki_plugin_sdk.plugin import Plugin
from yuki_plugin_sdk.registrar import (
    AdmissionSignalRegistration,
    AutomationActionRegistration,
    BackgroundServiceRegistration,
    CommandRegistration,
    EventHookRegistration,
    ToolRegistration,
)
from yuki_plugin_sdk.testing import FakePluginContext, run_plugin_contract_tests

PLUGIN_ROOT = Path(__file__).parents[1]


class RecordingRegistrar:
    """Small SDK-only registrar used to inspect the example's declarations."""

    def __init__(self) -> None:
        self.tools: list[ToolRegistration] = []
        self.commands: list[CommandRegistration] = []
        self.event_hooks: list[EventHookRegistration] = []
        self.prompt_fragments: list[PromptFragment] = []
        self.automation_actions: list[AutomationActionRegistration] = []
        self.admission_signals: list[AdmissionSignalRegistration] = []
        self.config_schemas: list[type[BaseModel]] = []
        self.background_services: list[BackgroundServiceRegistration] = []

    def register_tool(self, registration: ToolRegistration) -> None:
        self.tools.append(registration)

    def register_command(self, registration: CommandRegistration) -> None:
        self.commands.append(registration)

    def register_event_hook(self, registration: EventHookRegistration) -> None:
        self.event_hooks.append(registration)

    def register_prompt_fragment(self, fragment: PromptFragment) -> None:
        self.prompt_fragments.append(fragment)

    def register_automation_action(self, registration: AutomationActionRegistration) -> None:
        self.automation_actions.append(registration)

    def register_admission_signal(self, registration: AdmissionSignalRegistration) -> None:
        self.admission_signals.append(registration)

    def register_config_schema(self, schema: type[BaseModel]) -> None:
        self.config_schemas.append(schema)

    def register_background_service(self, registration: BackgroundServiceRegistration) -> None:
        self.background_services.append(registration)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "echo_plugin_under_test", PLUGIN_ROOT / "echo_plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_plugin() -> Plugin:
    return cast(Plugin, _load_module().EchoPlugin())


async def test_example_plugin_passes_host_contract() -> None:
    report = await run_plugin_contract_tests(PLUGIN_ROOT)
    assert report.passed is True
    assert report.checks == (
        "manifest",
        "permissions",
        "entrypoint",
        "register",
        "start",
        "stop",
    )


async def test_echo_extensions_use_fake_context_without_network() -> None:
    plugin = _load_plugin()
    registrar = RecordingRegistrar()
    await plugin.register(registrar)

    context = FakePluginContext("com.example.echo")
    context.messages.current = CurrentMessage(
        message_id="message-1",
        sender_user_id="10001",
        scope_type="group",
        group_id="20001",
        text="echo hello",
        received_at=datetime.now(UTC),
    )
    await context.config.set("prefix", "Global", scope_type="global")
    await context.config.set("prefix", "User", scope_type="user", scope_id="10001")
    await context.config.set("prefix", "Group", scope_type="group", scope_id="20001")
    await plugin.start(context)

    tool = registrar.tools[0]
    tool_result = await tool.handler(tool.input_model.model_validate({"text": "hello"}))
    assert tool_result.model_dump() == {"text": "Group: hello", "invocation_count": 1}
    assert await context.storage.get("stats", "tool_calls") == 1

    command = registrar.commands[0]
    command_result = await command.handler(
        command.argument_model.model_validate({"text": "deterministic"})
    )
    assert command_result.text == "deterministic"

    assert registrar.prompt_fragments[0].stage is PromptStage.PLUGIN_CONTEXT

    hook = registrar.event_hooks[0]
    await hook.handler(EventEnvelope(name=EventName.REPLY_SENT, payload={"sent": True}))
    assert await context.storage.get("stats", "reply_sent") == 1

    automation = registrar.automation_actions[0]
    automation_result = await automation.handler(
        automation.input_model.model_validate({"text": "later"})
    )
    assert automation_result.text == "Group: later"

    await plugin.stop()


def test_plugin_source_imports_only_public_sdk_and_standard_library() -> None:
    source = (PLUGIN_ROOT / "echo_plugin.py").read_text(encoding="utf-8")
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert "qq_ai_bot" not in imported_roots
    assert not ({"httpx", "requests", "socket", "urllib"} & imported_roots)
    assert imported_roots <= {"__future__", "pydantic", "typing", "yuki_plugin_sdk"}


def test_manifest_requests_no_network_or_host_mutation_permission() -> None:
    payload = tomllib.loads((PLUGIN_ROOT / "plugin.toml").read_text(encoding="utf-8"))
    permissions = set(payload["permissions"])
    assert permissions.isdisjoint(
        {
            "network.http.allowlisted",
            "network.http.unrestricted",
            "onebot.mutate",
            "runtime.config.write",
        }
    )
