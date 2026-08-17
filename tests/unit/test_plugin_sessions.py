from __future__ import annotations

from pathlib import Path

from yuki_plugin_sdk.permissions import HIGH_RISK_PERMISSIONS, PluginPermission
from yuki_plugin_sdk.sessions import (
    CreateAgentSessionRequest,
    RunAgentSessionRequest,
    SessionStatus,
)
from yuki_plugin_sdk.testing import (
    FakeAgentSessionFacade,
    FakePluginContext,
    run_plugin_contract_tests,
)


def test_plugin_ai_session_permission_is_explicitly_high_risk() -> None:
    assert PluginPermission.AGENT_SESSION in HIGH_RISK_PERMISSIONS


async def test_fake_agent_sessions_keep_transcripts_isolated_and_can_reset() -> None:
    sessions = FakeAgentSessionFacade(lambda text, history: f"history={len(history)} input={text}")
    first = await sessions.create(
        CreateAgentSessionRequest(name="first", instructions="Run one campaign")
    )
    second = await sessions.create(
        CreateAgentSessionRequest(name="second", instructions="Run another campaign")
    )

    first_run = await sessions.run(
        RunAgentSessionRequest(session_id=first.session_id, user_input="roll")
    )
    second_run = await sessions.run(
        RunAgentSessionRequest(session_id=second.session_id, user_input="roll")
    )
    assert first_run.text == "history=0 input=roll"
    assert second_run.text == "history=0 input=roll"

    again = await sessions.run(
        RunAgentSessionRequest(session_id=first.session_id, user_input="continue")
    )
    assert again.text == "history=2 input=continue"
    reset = await sessions.reset(first.session_id)
    assert reset.turn_count == 0
    closed = await sessions.close(second.session_id)
    assert closed.status is SessionStatus.CLOSED


async def test_fake_context_exposes_broad_facades_without_core_objects() -> None:
    context = FakePluginContext("com.example.contract")
    created = await context.automation.create_from_template("reminder", {"delay": 10})
    memory = await context.memory.add(
        scope_type="person",
        subject_id="10001",
        content="likes dice games",
        source_type="explicit",
        confidence=1.0,
    )
    await context.onebot.send_group("20001", "ready")

    assert created.data["task_id"]
    assert memory.data["memory_id"]
    assert context.onebot.sent == [("group", "20001", "ready")]
    assert not hasattr(context, "database")
    assert not hasattr(context, "container")


async def test_contract_runner_loads_async_lifecycle_plugin(tmp_path: Path) -> None:
    root = tmp_path / "com.example.contract"
    root.mkdir()
    (root / "plugin.toml").write_text(
        """id = "com.example.contract"
name = "Contract"
version = "0.1.0"
description = "Contract test plugin"
entrypoint = "contract_plugin:ContractPlugin"
plugin_api = "2.0"
yuki_requires = ">=1.6.0,<3"
permissions = []
""",
        encoding="utf-8",
    )
    (root / "contract_plugin.py").write_text(
        """class ContractPlugin:
    async def register(self, registrar):
        self.registered = True

    async def start(self, context):
        assert self.registered
        assert context.features.has("plugin.agent_session.v1")

    async def stop(self):
        self.stopped = True
""",
        encoding="utf-8",
    )

    report = await run_plugin_contract_tests(root)
    assert report.passed is True
    assert report.checks == ("manifest", "permissions", "entrypoint", "register", "start", "stop")
