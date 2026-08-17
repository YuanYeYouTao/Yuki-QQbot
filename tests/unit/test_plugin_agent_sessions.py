"""Host runtime tests for isolated plugin-owned AI conversations."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.plugin_host.session_facade import BoundAgentSessionFacade
from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.plugin_sessions import (
    PluginAgentSessionService,
    PluginSessionNotFoundError,
    PluginSessionPermissionError,
)
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.sessions import (
    CreateAgentSessionRequest,
    RunAgentSessionRequest,
    SessionContextProfile,
    SessionPersistence,
    SessionStatus,
)

T = TypeVar("T")


class RecordingConcurrency(ConcurrencyManager):
    def __init__(self) -> None:
        super().__init__(4)
        self.keys: list[str] = []

    async def run_llm(
        self,
        conversation_key: str,
        operation: Callable[[], Coroutine[Any, Any, T]],
    ) -> T:
        self.keys.append(conversation_key)
        return await super().run_llm(conversation_key, operation)


async def _install(database: Database, plugin_id: str) -> None:
    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id=plugin_id,
        name="Session Test",
        version="0.1.0",
        plugin_api="2.0",
        yuki_requires=">=1.6.0,<2.0",
        manifest_hash="a" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("agent.session", "web.search", "onebot.mutate"),
    )


async def _service(
    database: Database,
    provider: FakeLLMProvider,
    concurrency: ConcurrencyManager | None = None,
) -> tuple[PluginAgentSessionService, PluginAgentSessionRepository, ConcurrencyManager]:
    settings = make_settings(database.url)
    runtime_config = RuntimeConfigService(settings=settings, database=database)
    await runtime_config.initialize()
    manager = concurrency or RecordingConcurrency()
    repository = PluginAgentSessionRepository(database)
    return (
        PluginAgentSessionService(
            provider=provider,
            concurrency=manager,
            runtime_config=runtime_config,
            repository=repository,
        ),
        repository,
        manager,
    )


async def test_plugin_session_uses_only_its_transcript_and_never_persists_reasoning(
    database: Database,
) -> None:
    await _install(database, "com.example.rpg")
    await EventLedgerRepository(database).append(
        bot_user_id="9000",
        platform_message_id="main-ledger-1",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        private_peer_user_id="1001",
        direction="inbound",
        content="MAIN_CHAT_SECRET_MUST_NOT_ENTER_PLUGIN_SESSION",
    )

    def responder(request: ChatRequest) -> ChatResponse:
        user_messages = [
            message.content or "" for message in request.messages if message.role == "user"
        ]
        return ChatResponse(
            content=f"回合回应:{user_messages[-1]}",
            latency_seconds=0,
            reasoning_content="HIDDEN_REASONING_MUST_NOT_PERSIST",
        )

    provider = FakeLLMProvider(responder)
    service, repository, concurrency = await _service(database, provider)
    facade = BoundAgentSessionFacade(
        service=service,
        plugin_id="com.example.rpg",
        actor_user_id="1001",
        current_group_id=None,
        approved_permissions=(
            PluginPermission.AGENT_SESSION,
            PluginPermission.WEB_SEARCH,
        ),
    )
    created = await facade.create(
        CreateAgentSessionRequest(
            name="克苏鲁跑团",
            instructions="担任守秘人，连续维护本次跑团世界状态。",
            persistence=SessionPersistence.DURABLE,
            context_profile=SessionContextProfile.NONE,
            allowed_capabilities=("web.search", "onebot.mutate"),
        )
    )
    stored = await repository.get(plugin_id="com.example.rpg", session_id=str(created.session_id))
    assert stored is not None
    assert stored.allowed_capabilities == ("web.search",)
    assert stored.instructions.startswith("担任守秘人")

    first = await facade.run(
        RunAgentSessionRequest(
            session_id=created.session_id,
            user_input="调查书房",
            allowed_capabilities=("web.search", "onebot.mutate"),
            max_tool_calls=64,
            max_model_requests=64,
        )
    )
    second = await facade.run(
        RunAgentSessionRequest(session_id=created.session_id, user_input="继续调查")
    )

    assert first.text == "回合回应:调查书房"
    assert second.session.turn_count == 2
    assert second.text == "回合回应:继续调查"
    assert all(request.tools == () for request in provider.requests)
    second_prompt = "\n".join(message.content or "" for message in provider.requests[-1].messages)
    assert "调查书房" in second_prompt
    assert "回合回应:调查书房" in second_prompt
    assert "MAIN_CHAT_SECRET_MUST_NOT_ENTER_PLUGIN_SESSION" not in second_prompt
    assert "1001" not in second_prompt
    transcript = await repository.list_messages(
        plugin_id="com.example.rpg", session_id=str(created.session_id)
    )
    persisted_text = "\n".join(message.content for message in transcript)
    assert "HIDDEN_REASONING_MUST_NOT_PERSIST" not in persisted_text
    assert isinstance(concurrency, RecordingConcurrency)
    assert concurrency.keys == [
        f"plugin-session:com.example.rpg:{created.session_id}",
        f"plugin-session:com.example.rpg:{created.session_id}",
    ]
    async with database.sessions() as db_session:
        ledger_count = await db_session.scalar(select(func.count(ChatEventModel.id)))
    assert ledger_count == 1


async def test_reset_clears_only_session_history_and_close_prevents_more_runs(
    database: Database,
) -> None:
    await _install(database, "com.example.reset")
    provider = FakeLLMProvider()
    service, repository, _ = await _service(database, provider)
    facade = BoundAgentSessionFacade(
        service=service,
        plugin_id="com.example.reset",
        actor_user_id="1002",
        current_group_id=None,
        approved_permissions=(PluginPermission.AGENT_SESSION,),
    )
    created = await facade.create(
        CreateAgentSessionRequest(name="独立会话", instructions="维持独立状态。")
    )
    await facade.run(RunAgentSessionRequest(session_id=created.session_id, user_input="第一轮"))

    reset = await facade.reset(created.session_id)
    assert reset.turn_count == 0
    assert (
        await repository.list_messages(
            plugin_id="com.example.reset", session_id=str(created.session_id)
        )
        == ()
    )
    await facade.run(
        RunAgentSessionRequest(session_id=created.session_id, user_input="重置后的第一轮")
    )
    latest_prompt = "\n".join(message.content or "" for message in provider.requests[-1].messages)
    assert "第一轮" not in latest_prompt.replace("重置后的第一轮", "")

    closed = await facade.close(created.session_id)
    assert closed.status is SessionStatus.CLOSED
    with pytest.raises(PluginSessionNotFoundError):
        await facade.run(
            RunAgentSessionRequest(session_id=created.session_id, user_input="不应执行")
        )


async def test_bound_authority_blocks_missing_permission_and_cross_scope_access(
    database: Database,
) -> None:
    await _install(database, "com.example.scope")
    await _install(database, "com.example.other")
    service, _, _ = await _service(database, FakeLLMProvider())
    no_permission = BoundAgentSessionFacade(
        service=service,
        plugin_id="com.example.scope",
        actor_user_id="1003",
        current_group_id="2001",
        approved_permissions=(),
    )
    with pytest.raises(PluginSessionPermissionError):
        await no_permission.create(
            CreateAgentSessionRequest(name="拒绝", instructions="不应创建。")
        )

    group_facade = BoundAgentSessionFacade(
        service=service,
        plugin_id="com.example.scope",
        actor_user_id="1003",
        current_group_id="2001",
        approved_permissions=(
            PluginPermission.AGENT_SESSION,
            PluginPermission.GROUP_CURRENT_READ,
        ),
    )
    created = await group_facade.create(
        CreateAgentSessionRequest(
            name="群跑团",
            instructions="只处理本群跑团。",
            context_profile=SessionContextProfile.CURRENT_GROUP,
        )
    )
    other_group = BoundAgentSessionFacade(
        service=service,
        plugin_id="com.example.scope",
        actor_user_id="1004",
        current_group_id="2002",
        approved_permissions=(
            PluginPermission.AGENT_SESSION,
            PluginPermission.GROUP_CURRENT_READ,
        ),
    )
    with pytest.raises(PluginSessionNotFoundError):
        await other_group.run(
            RunAgentSessionRequest(session_id=created.session_id, user_input="越权")
        )
    other_plugin = BoundAgentSessionFacade(
        service=service,
        plugin_id="com.example.other",
        actor_user_id="1003",
        current_group_id="2001",
        approved_permissions=(PluginPermission.AGENT_SESSION,),
    )
    with pytest.raises(PluginSessionNotFoundError):
        await other_plugin.reset(created.session_id)


async def test_durable_sessions_survive_service_recreation_but_ephemeral_sessions_do_not(
    database: Database,
) -> None:
    await _install(database, "com.example.persistence")
    first_service, repository, _ = await _service(database, FakeLLMProvider())
    first_facade = BoundAgentSessionFacade(
        service=first_service,
        plugin_id="com.example.persistence",
        actor_user_id="1005",
        current_group_id=None,
        approved_permissions=(PluginPermission.AGENT_SESSION,),
    )
    durable = await first_facade.create(
        CreateAgentSessionRequest(
            name="持久跑团",
            instructions="保持长期世界状态。",
            persistence=SessionPersistence.DURABLE,
        )
    )
    ephemeral = await first_facade.create(
        CreateAgentSessionRequest(
            name="临时试跑",
            instructions="只在当前进程存在。",
            persistence=SessionPersistence.EPHEMERAL,
        )
    )
    await first_facade.run(
        RunAgentSessionRequest(session_id=durable.session_id, user_input="第一幕")
    )

    second_service, _, _ = await _service(database, FakeLLMProvider())
    second_facade = BoundAgentSessionFacade(
        service=second_service,
        plugin_id="com.example.persistence",
        actor_user_id="1005",
        current_group_id=None,
        approved_permissions=(PluginPermission.AGENT_SESSION,),
    )
    continued = await second_facade.run(
        RunAgentSessionRequest(session_id=durable.session_id, user_input="第二幕")
    )
    assert continued.session.turn_count == 2
    with pytest.raises(PluginSessionNotFoundError):
        await second_facade.run(
            RunAgentSessionRequest(session_id=ephemeral.session_id, user_input="不应恢复")
        )
    assert await repository.delete_ephemeral() == 1
    assert (
        await repository.get(
            plugin_id="com.example.persistence", session_id=str(ephemeral.session_id)
        )
        is None
    )
