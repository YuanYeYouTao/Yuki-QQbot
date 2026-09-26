"""SELF social effects use the accepted scene, never a group's latest speaker."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import Bot
from tests.unit.test_self_initiative_runtime import self_source

from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.gateway.providers import builtin_provider_catalog
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.canonical_repository import ensure_space
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.social.agent_adapter import invoke_social
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import SocialError
from qq_ai_bot.social.service import SocialContext, SocialService


async def social_scene(database):
    source, admissions, binding = await self_source(database)
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    bot = Bot("8000")
    registry.connect(bot, provider_id="snowluma", presence_id=source["presence_id"])
    writer = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
    router = PresenceRouter(database, registry)
    service = SocialService(database, router, writer)
    actor = ToolActor(
        user_id="",
        bot_user_id="8000",
        group_id="2001",
        origin=TurnOrigin.SELF_INITIATIVE,
        instruction=source["instruction"],
        conversation_id=source["conversation_id"],
        presence_id=source["presence_id"],
        principal_kind="self",
        initiative_run_id=source["initiative_run_id"],
        execution_id="work",
    )
    context = SocialContext(
        turn_id=f"{source['conversation_id']}:initiative:{source['initiative_run_id']}",
        call_id="call",
        conversation_id=source["conversation_id"],
        space_id=source["space_id"],
        origin="self_initiative",
        initiative_run_id=source["initiative_run_id"],
        presence_id=source["presence_id"],
        actor=actor,
    )
    return SimpleNamespace(
        source=source,
        admissions=admissions,
        binding=binding,
        bot=bot,
        service=service,
        context=context,
    )


async def test_self_send_receipt_is_run_bound_and_master_off_does_not_revoke(database):
    env = await social_scene(database)
    await env.admissions.transition(env.binding, master_enabled=False, external_enabled=True)
    result = await env.service.execute("send_message", {"text": "想到一个问题"}, env.context)
    repeated = await env.service.execute("send_message", {"text": "想到一个问题"}, env.context)
    assert result == repeated and result["status"] == "succeeded"
    sends = [call for call in env.bot.calls if call[0] == "send_group_msg"]
    assert len(sends) == 1 and sends[0][1]["group_id"] == 2001
    async with database.sessions() as db:
        receipt = await db.scalar(select(SocialOperationModel))
        assert receipt.source_turn_id == env.context.turn_id
        assert receipt.presence_id == env.source["presence_id"]
        event = await db.scalar(select(ChatEventModel))
        assert event.origin == "self_initiative" and event.caused_by_event_id is None
        assert event.author_person_id is None
        assert event.author_presence_id == env.source["presence_id"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("send_message", {"text": "hello", "mentions": [{"display_name": "someone"}]}),
        ("poke_person", {}),
        ("recall_own_message", {"event_id": 1}),
        ("find_contacts", {"kind": "person"}),
        ("read_conversation_history", {"kind": "person"}),
        ("read_conversation_history", {"operation_id": "foreign-op"}),
    ],
)
async def test_self_rejects_unrelated_social_authority(database, name, args):
    env = await social_scene(database)
    with pytest.raises(SocialError):
        await env.service.execute(name, args, env.context)
    assert env.bot.calls == []
    async with database.sessions() as db:
        assert not list(await db.scalars(select(SocialOperationModel.id)))


async def test_self_directory_members_and_history_are_current_space_only(database):
    env = await social_scene(database)
    async with database.immediate_session() as db:
        other = await ensure_space(db, "2002")
    contacts = await env.service.execute("find_contacts", {}, env.context)
    assert [item["target_id"] for item in contacts["items"]] == [env.source["space_id"]]
    members = await env.service.execute("get_group_members", {}, env.context)
    assert members["items"][0]["user_id"] == "10001"
    result = await env.service.execute("read_conversation_history", {}, env.context)
    assert result["content_trust"] == "untrusted_data"
    for name, args in (
        ("send_message", {"text": "no", "target": {"kind": "space", "target_id": other}}),
        ("get_group_members", {"target_id": other}),
        ("read_conversation_history", {"kind": "space", "target_id": other}),
    ):
        with pytest.raises(SocialError, match="self_target_outside_current_space"):
            await env.service.execute(name, args, replace(env.context, call_id=name))
    assert not any(call[0] == "send_group_msg" for call in env.bot.calls)


async def test_self_rechecks_generation_after_route_resolution_before_preparing_effect(
    database,
    monkeypatch,
):
    env = await social_scene(database)
    original = env.service.send_route

    async def reset_after_route(target, context):
        route = await original(target, context)
        async with database.immediate_session() as db:
            await db.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == env.source["conversation_id"])
                .values(generation=2)
            )
        return route

    monkeypatch.setattr(env.service, "send_route", reset_after_route)
    with pytest.raises(SocialError, match="self_initiative_scene_changed"):
        await env.service.execute("send_message", {"text": "must not send"}, env.context)
    assert env.bot.calls == []
    async with database.sessions() as db:
        assert not list(await db.scalars(select(SocialOperationModel.id)))


async def test_self_adapter_builds_empty_person_context(database, monkeypatch):
    env = await social_scene(database)
    runtime = ToolRuntime(
        inbound=None,
        gateway=None,
        allow_generic_onebot=False,
        actor_context=env.context.actor,
        actor_user_id="",
        origin=TurnOrigin.SELF_INITIATIVE,
        execution_id="work",
        initiative_run_id=env.source["initiative_run_id"],
        conversation_id=env.source["conversation_id"],
        presence_id=env.source["presence_id"],
        space_id=env.source["space_id"],
        current_group_id="2001",
    )
    captured = AsyncMock(return_value={"status": "succeeded"})
    monkeypatch.setattr(env.service, "execute", captured)
    token = current_invocation.set(ToolInvocationContext(runtime, call_id="call"))
    try:
        await invoke_social(env.service, "send_message", {"text": "hello"}, runtime)
    finally:
        current_invocation.reset(token)
    context = captured.call_args.args[2]
    assert context.initiative_run_id == env.source["initiative_run_id"]
    assert context.turn_id == env.context.turn_id
    assert not context.person_refs and not context.account_refs
    assert context.trigger_event_id is None and context.inbound is None
