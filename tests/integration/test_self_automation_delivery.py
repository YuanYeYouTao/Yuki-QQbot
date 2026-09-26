"""SELF scheduled work must use the same main Agent and social receipts as a person task."""

import json

import pytest
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.handlers import AutomationCapabilityHandlers
from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.conversation.autonomy_repository import AutonomyRepository
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.operations.reset_conversations import reset_all
from qq_ai_bot.persistence.models import AutomationRunModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.social.automation import SocialAutomationAdapter
from qq_ai_bot.workspace.short_state import ShortState


async def self_actor(database, env):
    admissions = AutonomyRepository(database)
    binding = await admissions.ensure_binding(env.context.conversation_id, 1)
    binding = await admissions.transition(binding, master_enabled=True, external_enabled=True)
    accepted = await admissions.accept_host_proposal(
        proposal_id="self-daily-agent",
        binding=binding,
        owner=binding.effective_owner,
        space_id=env.space,
        presence_id=env.presence,
        sources=(),
        support_refs=(),
        trigger_kind="intrinsic",
    )
    assert accepted.run is not None
    return ToolActor(
        user_id="",
        bot_user_id="80001",
        group_id="20001",
        origin=TurnOrigin.SELF_INITIATIVE,
        instruction="每天整理自己的工作",
        execution_id="self-work",
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
        principal_kind="self",
        initiative_run_id=accepted.run.run_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["none", "current_group"])
async def test_self_daily_agent_runs_without_borrowing_a_person(database, tmp_path, delivery):
    env = await social_env(database, tmp_path)
    actor = await self_actor(database, env)
    settings = make_settings(
        database.url, automation_enabled=True, enabled_groups_csv="20001", runtime_work_enabled=True
    )
    turns = 0

    def respond(request):
        nonlocal turns
        turns += 1
        if delivery == "current_group" and turns == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="self-daily-send",
                        function=ToolFunction(
                            name="send_message", arguments=json.dumps({"text": "现在喝水"})
                        ),
                    ),
                ),
            )
        return "NO_REPLY" if delivery == "none" else "done"

    provider = FakeLLMProvider(responder=respond)
    harness = build_harness(database, settings, provider)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat._agent_runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    handlers = object.__new__(AutomationCapabilityHandlers)
    handlers._settings = settings
    handlers._runtime_config = chat._runtime_config
    handlers._ledger = harness.ledger
    handlers._memories = chat._memories
    handlers._relationships = harness.relationships
    handlers._time = chat._time
    handlers._agent_runner = chat._agent_runner
    handlers._gateway_factory = lambda context: None
    registry = build_capability_registry(handlers.mapping())
    repository = AutomationRepository(database)
    service = AutomationService(
        settings=settings, repository=repository, registry=registry, time_service=chat._time
    )
    row, _plan = await service.create_task(
        {
            "name": "SELF 每日工作",
            "goal": "每天在当前群做自己的工作",
            "trigger": {"type": "daily", "hour": 15, "minute": 0},
            "strategy": "agentic",
            "context": {"scene": "current_group", "history_limit": 5},
            "delivery": {"target": delivery},
        },
        actor=actor,
        conversation_key="bot:80001:group:20001",
    )
    run = await repository.create_run(
        row.id, scheduled_for=row.next_run_at, actual_started_at=chat._time.clock.now()
    )
    result = await AutomationExecutor(
        settings=settings,
        registry=registry,
        repository=repository,
        time_service=chat._time,
        router=env.router,
        gateway_factory=lambda context: None,
    ).execute(row, run)
    assert result.status is RunStatus.SUCCEEDED, result
    assert provider.requests
    assert len([call for call in env.bot.calls if call[0] == "send_group_msg"]) == int(
        delivery == "current_group"
    )


@pytest.mark.asyncio
async def test_self_static_reminder_uses_social_receipt_and_replay(database, tmp_path):
    env = await social_env(database, tmp_path)
    actor = await self_actor(database, env)
    settings = make_settings(database.url, automation_enabled=True, enabled_groups_csv="20001")
    adapter = SocialAutomationAdapter(env.service, None, None)
    registry = build_capability_registry(adapter.mapping())
    repository = AutomationRepository(database)
    time_service = build_harness(database, settings, FakeLLMProvider()).processor._chat._time
    service = AutomationService(
        settings=settings, repository=repository, registry=registry, time_service=time_service
    )
    row, _plan = await service.create_task(
        {
            "name": "SELF 静态提醒",
            "goal": "喝水",
            "trigger": {"type": "daily", "hour": 15, "minute": 0},
            "strategy": "static",
            "context": {"scene": "current_group"},
            "delivery": {"target": "current_group", "text": "现在喝水"},
        },
        actor=actor,
        conversation_key="bot:80001:group:20001",
    )
    assert row.required_capabilities == ("social.send_message",)
    run = await repository.create_run(
        row.id, scheduled_for=row.next_run_at, actual_started_at=time_service.clock.now()
    )
    executor = AutomationExecutor(
        settings=settings,
        registry=registry,
        repository=repository,
        time_service=time_service,
        router=env.router,
    )
    result = await executor.execute(row, run)
    assert result.status is RunStatus.SUCCEEDED, result
    assert result.messages_sent == 1
    assert len([call for call in env.bot.calls if call[0] == "send_group_msg"]) == 1
    replay = await executor.execute(row, run)
    assert replay.status is RunStatus.SUCCEEDED
    assert len([call for call in env.bot.calls if call[0] == "send_group_msg"]) == 1


@pytest.mark.asyncio
async def test_offline_generation_reset_rebinds_future_self_automation(database, tmp_path):
    env = await social_env(database, tmp_path)
    actor = await self_actor(database, env)
    settings = make_settings(database.url, automation_enabled=True, enabled_groups_csv="20001")
    service = AutomationService(
        settings=settings,
        repository=AutomationRepository(database),
        registry=build_capability_registry(),
        time_service=build_harness(database, settings, FakeLLMProvider()).processor._chat._time,
    )
    row, _ = await service.create_task(
        {
            "name": "切换后的 SELF 提醒",
            "goal": "喝水",
            "trigger": {"type": "daily", "hour": 15, "minute": 0},
            "strategy": "static",
            "context": {"scene": "current_group"},
            "delivery": {"target": "current_group", "text": "现在喝水"},
        },
        actor=actor,
        conversation_key="bot:80001:group:20001",
    )
    before = row.authority_snapshot["conversation_generation"]
    repository = AutomationRepository(database)
    run = await repository.create_run(
        row.id,
        scheduled_for=row.next_run_at,
        actual_started_at=service._time.clock.now(),
    )
    assert run is not None
    preview = await reset_all(database, "self-media-cutover", apply=False)
    assert preview["running_automation"] == 1
    with pytest.raises(RuntimeError, match="active_execution_must_drain"):
        await reset_all(database, "self-media-cutover", apply=True)
    async with database.sessions() as session, session.begin():
        live_run = await session.get(AutomationRunModel, run.id)
        assert live_run is not None
        live_run.status = RunStatus.SUCCEEDED.value
    result = await reset_all(database, "self-media-cutover", apply=True)
    assert result["self_automations_rebound"] == 1
    current = await AutomationRepository(database).get(row.id)
    assert (
        current is not None and current.authority_snapshot["conversation_generation"] == before + 1
    )
    async with database.sessions() as session:
        conversation = await session.get(CanonicalConversationModel, env.context.conversation_id)
    assert conversation is not None and conversation.generation == before + 1
    assert not [call for call in env.bot.calls if call[0] == "send_group_msg"]
