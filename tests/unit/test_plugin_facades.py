"""Invocation authority and public-surface tests for Host Plugin Facades."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from tests.conftest import make_settings

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.enums import MemoryScopeType, MemorySourceType
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository, PeopleRepository
from qq_ai_bot.plugin_host.audit import PluginAuditService
from qq_ai_bot.plugin_host.facades import (
    HostPluginContext,
    PluginFacadeServices,
    PluginInvocation,
)
from qq_ai_bot.plugin_host.repository import PluginAuditRepository
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.web.base import WebSearchValidationError
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission


@dataclass(slots=True)
class Gateway:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def call_api(self, action: str, params: dict[str, Any]) -> object:
        self.calls.append((action, params))
        return {
            "message_id": 41 + len(self.calls),
            "access_token": "must-not-leak",
        }


@dataclass(slots=True)
class FailingGateway:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def call_api(self, action: str, params: dict[str, Any]) -> object:
        self.calls.append((action, params))
        raise RuntimeError("https://private.example/result?token=exception-secret-must-not-leak")


def inbound(
    *,
    user_id: str = "10001",
    group_id: str | None = None,
    attachments: tuple[MessageAttachment, ...] = (),
    mentioned_user_ids: tuple[str, ...] = (),
) -> InboundMessage:
    return InboundMessage(
        message_id=f"message-{user_id}",
        event_type="message",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname="Tester"),
        text="hello",
        bot_user_id="99999",
        group_id=group_id,
        attachments=attachments,
        mentioned_user_ids=mentioned_user_ids,
        received_at=datetime.now(UTC),
    )


def invocation(
    *,
    plugin_id: str = "example.plugin",
    user_id: str = "10001",
    group_id: str | None = None,
    gateway: Gateway | FailingGateway | None = None,
    attachments: tuple[MessageAttachment, ...] = (),
    mentioned_user_ids: tuple[str, ...] = (),
    web_was_used: bool = False,
) -> PluginInvocation:
    message = inbound(
        user_id=user_id,
        group_id=group_id,
        attachments=attachments,
        mentioned_user_ids=mentioned_user_ids,
    )
    return PluginInvocation(
        plugin_id=plugin_id,
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id=user_id,
        bot_user_id=message.bot_user_id,
        inbound=message,
        gateway=gateway,
        web_was_used=web_was_used,
    )


@pytest.mark.asyncio
async def test_contextvar_binding_is_required_scoped_and_task_local() -> None:
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.MESSAGE_CURRENT_READ,),
    )
    assert context.current is None


@pytest.mark.asyncio
async def test_current_message_projects_unique_trusted_mentions_without_bot() -> None:
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.MESSAGE_CURRENT_READ,),
    )
    trusted = invocation(
        group_id="20001",
        mentioned_user_ids=("10002", "99999", "10002"),
    )

    with context.bind(trusted):
        current = await context.messages.get_current()

    assert current is not None
    assert current.mentioned_user_ids == ("10002",)
    with pytest.raises(PluginPermissionError, match="trusted invocation"):
        await context.messages.get_current()

    async def bound_user(user_id: str) -> str:
        with context.bind(invocation(user_id=user_id)):
            await asyncio.sleep(0)
            current = await context.messages.get_current()
            assert current is not None
            return current.sender_user_id

    assert tuple(await asyncio.gather(bound_user("10001"), bound_user("10002"))) == (
        "10001",
        "10002",
    )
    assert context.current is None


@pytest.mark.asyncio
async def test_plugin_memory_facade_writes_v2_fact_with_current_event_evidence_only(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    message = inbound(user_id="10001")
    event, _ = await ledger.append_inbound(message, bot_user_id="99999")
    facts = MemoryFactService(MemoryFactRepository(database))
    audit = AdminAuditService(database)
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(
            PluginPermission.MEMORY_WRITE,
            PluginPermission.MEMORY_PERSON_READ,
        ),
        services=PluginFacadeServices(
            memories=facts,
            memory_admin=MemoryAdminService(
                settings=make_settings(database.url),
                memories=facts,
                audit=audit,
            ),
        ),
    )
    trusted = PluginInvocation(
        plugin_id="example.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        bot_user_id="99999",
        inbound=message,
        source_event_id=event.id,
    )
    with context.bind(trusted):
        created = await context.memory.add(
            scope_type="person",
            subject_id="10001",
            content="喜欢桌游",
            source_type="plugin",
            confidence=0.9,
            source_event_ids=(str(event.id),),
        )
        rows = await context.memory.list_person("10001")
        with pytest.raises(PluginPermissionError, match="source events"):
            await context.memory.add(
                scope_type="person",
                subject_id="10001",
                content="伪造证据",
                source_type="plugin",
                confidence=0.9,
                source_event_ids=("999999",),
            )

    assert created.ok
    assert rows[0]["fact_id"] == created.data["memory"]["fact_id"]
    assert rows[0]["status"] == "active"
    assert rows[0]["source_type"] == "explicit"
    assert rows[0]["evidence_count"] == 1
    evidence = await facts.list_evidence(int(rows[0]["fact_id"]))
    assert evidence[0].event_id == event.id
    assert evidence[0].source_speaker_user_id == "10001"


@pytest.mark.asyncio
async def test_plugin_memory_search_reuses_scoped_retriever(database: Database) -> None:
    settings = make_settings(database.url)
    repository = MemoryFactRepository(database)
    facts = MemoryFactService(repository)
    wanted = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="10001",
            kind="fact",
            memory_key="hobby:boardgame",
            category="hobby",
            content="喜欢合作桌游",
            importance=4,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="10002",
            kind="fact",
            memory_key="hobby:boardgame",
            category="hobby",
            content="喜欢合作桌游",
            importance=5,
            confidence=1,
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    memory_context = MemoryContextService(
        query_builder=MemoryQueryBuilder(MemoryTargetResolver(PeopleRepository(database))),
        retriever=MemoryRetriever(
            repository=repository,
            lexical_index=SQLiteMemoryFTSIndex(database),
        ),
        facts=facts,
    )
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.MEMORY_SEARCH,),
        services=PluginFacadeServices(
            memories=facts,
            memory_context=memory_context,
            runtime_config=RuntimeConfigService(settings=settings, database=database),
        ),
    )
    with context.bind(invocation(user_id="10001")):
        rows = await context.memory.search(
            "合作桌游",
            scope_type="person",
            subject_id="10001",
            limit=5,
        )
        with pytest.raises(PluginPermissionError):
            await context.memory.search(
                "合作桌游",
                scope_type="person",
                subject_id="10002",
                limit=5,
            )

    assert [row["fact_id"] for row in rows] == [wanted.id]
    assert rows[0]["retrieval_reason"] == "lexical_match"


def test_public_context_does_not_expose_core_objects_or_raw_media() -> None:
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.MESSAGE_CURRENT_READ,),
    )
    attachment = MessageAttachment(
        kind=AttachmentKind.IMAGE,
        label="image",
        file="secret-file-id",
        url="https://signed.example/image?token=secret",
    )
    with context.bind(invocation(attachments=(attachment,))):
        current = context.current
        assert current is not None
        assert current.sender_user_id == "10001"
        assert not hasattr(current, "file")
        assert not hasattr(current, "url")

    for forbidden in ("settings", "container", "database", "session", "bot", "event"):
        assert not hasattr(context, forbidden)


@pytest.mark.asyncio
async def test_message_facade_rechecks_permission_scope_and_redacts_gateway_result() -> None:
    gateway = Gateway()
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(
            PluginPermission.MESSAGE_PRIVATE_SEND,
            PluginPermission.MESSAGE_GROUP_SEND,
        ),
    )
    with context.bind(invocation(group_id="20001", gateway=gateway)):
        result = await context.messages.send_group("20001", "hello group")
        assert result.ok
        assert result.data["result"] == {
            "message_id": 42,
            "access_token": "[redacted]",
        }
        with pytest.raises(PluginPermissionError, match="target user"):
            await context.messages.send_private("10002", "cross-user")
        with pytest.raises(PluginPermissionError, match="target group"):
            await context.messages.send_group("20002", "cross-group")

    assert gateway.calls == [("send_group_msg", {"group_id": "20001", "message": "hello group"})]


@pytest.mark.asyncio
async def test_music_card_facade_targets_only_the_current_real_scene() -> None:
    gateway = Gateway()
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.ONEBOT_SEND,),
    )

    with context.bind(invocation(user_id="10001", gateway=gateway)):
        result = await context.onebot.send_music_card(
            provider="netease",
            resource_id="123456",
        )
        assert result.ok
        custom = await context.onebot.send_custom_music_card(
            url="https://y.music.163.com/m/album?id=242154493",
            image="https://example.com/album.jpg",
            title="中国有弹舌",
            singer="MC赵小六",
            content="网易云专辑 · 2 首",
        )
        assert custom.ok
        with pytest.raises(ValueError, match="provider"):
            await context.onebot.send_music_card(provider="custom", resource_id="123456")
        with pytest.raises(ValueError, match="resource id"):
            await context.onebot.send_music_card(provider="netease", resource_id="https://bad")
        with pytest.raises(WebSearchValidationError, match="本地或私有 IP"):
            await context.onebot.send_custom_music_card(
                url="http://127.0.0.1/private",
                image="https://example.com/album.jpg",
                title="private",
            )

    assert gateway.calls == [
        (
            "send_private_msg",
            {
                "user_id": "10001",
                "message": [
                    {
                        "type": "music",
                        "data": {"type": "163", "id": "123456"},
                    }
                ],
            },
        ),
        (
            "send_private_msg",
            {
                "user_id": "10001",
                "message": [
                    {
                        "type": "music",
                        "data": {
                            "type": "custom",
                            "url": "https://y.music.163.com/m/album?id=242154493",
                            "image": "https://example.com/album.jpg",
                            "title": "中国有弹舌",
                            "singer": "MC赵小六",
                            "content": "网易云专辑 · 2 首",
                        },
                    }
                ],
            },
        ),
    ]


@pytest.mark.asyncio
async def test_plugin_sends_are_audited_and_persist_confirmed_outbound_events(
    database: Database,
) -> None:
    gateway = Gateway()
    audit_repository = PluginAuditRepository(database)
    ledger = EventLedgerRepository(database)
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(
            PluginPermission.MESSAGE_PRIVATE_SEND,
            PluginPermission.MESSAGE_GROUP_SEND,
            PluginPermission.MESSAGE_MEDIA_SEND,
            PluginPermission.ONEBOT_SEND,
        ),
        services=PluginFacadeServices(
            ledger=ledger,
            audit=PluginAuditService(audit_repository),
        ),
    )
    with context.bind(invocation(group_id="20001", gateway=gateway)):
        result = await context.messages.send_text("send-text-body-secret")
        assert result.data["result"] == {
            "message_id": 42,
            "access_token": "[redacted]",
        }
        await context.messages.send_private("10001", "private-body-secret")
        await context.messages.send_group("20001", "group-body-secret")
        await context.messages.send_image(
            target_type="group",
            target_id="20001",
            media_reference="event-file-secret",
        )
        await context.onebot.send_music_card(provider="netease", resource_id="123456")
        await context.onebot.send_custom_music_card(
            url="https://y.music.163.com/m/album?id=242154493",
            image="https://example.com/album.jpg",
            title="中国有弹舌",
            singer="MC赵小六",
            content="网易云专辑 · 2 首",
        )
        await context.onebot.send_private("10001", "onebot-private-body-secret")
        await context.onebot.send_group("20001", "onebot-group-body-secret")

    group_events = await ledger.list_scope_recent(
        ConversationScope.group("99999", "20001"),
        limit=20,
    )
    assert [row.content for row in group_events] == [
        "send-text-body-secret",
        "group-body-secret",
        "",
        "",
        "",
        "onebot-group-body-secret",
    ]
    assert all(row.direction == "outbound" for row in group_events)
    assert all(row.sender_user_id == "99999" for row in group_events)
    assert all(row.group_id == "20001" for row in group_events)
    assert all(row.private_peer_user_id is None for row in group_events)
    assert group_events[2].segments == ({"type": "image", "data": {}},)
    assert group_events[3].segments == (
        {"type": "music", "data": {"provider": "163", "id": "123456"}},
    )
    assert group_events[4].segments == (
        {
            "type": "music",
            "data": {
                "type": "custom",
                "url": "https://y.music.163.com/m/album?id=242154493",
                "image": "https://example.com/album.jpg",
                "title": "中国有弹舌",
                "singer": "MC赵小六",
                "content": "网易云专辑 · 2 首",
            },
        },
    )

    private_events = await ledger.list_scope_recent(
        ConversationScope.private("99999", "10001"),
        limit=20,
    )
    assert [row.content for row in private_events] == [
        "private-body-secret",
        "onebot-private-body-secret",
    ]
    assert all(row.direction == "outbound" for row in private_events)
    assert all(row.sender_user_id == "99999" for row in private_events)
    assert all(row.private_peer_user_id == "10001" for row in private_events)
    assert all(row.group_id is None for row in private_events)

    audit_rows = await audit_repository.history(plugin_id="example.plugin")
    by_operation = {row.operation: row for row in audit_rows}
    assert set(by_operation) == {
        "message.send_text",
        "message.send_private",
        "message.send_group",
        "message.send_image",
        "onebot.send_music_card",
        "onebot.send_custom_music_card",
        "onebot.send_private",
        "onebot.send_group",
    }
    assert all(row.actor_user_id == "10001" for row in audit_rows)
    assert all(row.success and row.error_category is None for row in audit_rows)
    assert by_operation["message.send_text"].permission == "message.group.send"
    assert by_operation["message.send_image"].permission == "message.media.send"
    assert by_operation["onebot.send_music_card"].permission == "onebot.send"
    assert by_operation["onebot.send_custom_music_card"].permission == "onebot.send"
    assert by_operation["onebot.send_private"].permission == "onebot.send"
    assert all(row.detail == {} for row in audit_rows)
    serialized_audit = json.dumps(
        [
            {
                "operation": row.operation,
                "permission": row.permission,
                "error_category": row.error_category,
                "detail": row.detail,
            }
            for row in audit_rows
        ],
        ensure_ascii=False,
    )
    for sensitive in (
        "send-text-body-secret",
        "private-body-secret",
        "group-body-secret",
        "onebot-private-body-secret",
        "onebot-group-body-secret",
        "event-file-secret",
        "must-not-leak",
        "https://",
    ):
        assert sensitive not in serialized_audit


@pytest.mark.asyncio
async def test_onebot_read_and_mutation_audit_omits_action_params_and_results(
    database: Database,
) -> None:
    gateway = Gateway()
    audit_repository = PluginAuditRepository(database)
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(
            PluginPermission.ONEBOT_READ,
            PluginPermission.ONEBOT_MUTATE,
        ),
        superuser_ids=("90000",),
        services=PluginFacadeServices(
            audit=PluginAuditService(audit_repository),
        ),
    )
    with context.bind(invocation(user_id="90000", group_id="20001", gateway=gateway)):
        assert (
            await context.onebot.call_read_action(
                "get_group_info",
                {
                    "group_id": "20001",
                    "access_token": "read-param-secret",
                },
            )
        ).ok
        assert (
            await context.onebot.call_mutating_action(
                "set_group_name",
                {
                    "group_id": "20001",
                    "group_name": "https://private.example/?token=mutation-param-secret",
                },
            )
        ).ok

    audit_rows = await audit_repository.history(plugin_id="example.plugin")
    by_operation = {row.operation: row for row in audit_rows}
    assert set(by_operation) == {
        "onebot.call_read_action",
        "onebot.call_mutating_action",
    }
    assert by_operation["onebot.call_read_action"].permission == "onebot.read"
    assert by_operation["onebot.call_mutating_action"].permission == "onebot.mutate"
    assert all(row.actor_user_id == "90000" for row in audit_rows)
    assert all(row.success and row.error_category is None for row in audit_rows)
    assert all(row.detail == {} for row in audit_rows)
    serialized_audit = json.dumps(
        [
            {
                "operation": row.operation,
                "permission": row.permission,
                "detail": row.detail,
            }
            for row in audit_rows
        ]
    )
    for sensitive in (
        "get_group_info",
        "set_group_name",
        "read-param-secret",
        "mutation-param-secret",
        "must-not-leak",
        "https://",
    ):
        assert sensitive not in serialized_audit


@pytest.mark.asyncio
async def test_failed_plugin_send_is_audited_without_fabricating_ledger_event(
    database: Database,
) -> None:
    gateway = FailingGateway()
    audit_repository = PluginAuditRepository(database)
    ledger = EventLedgerRepository(database)
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.MESSAGE_PRIVATE_SEND,),
        services=PluginFacadeServices(
            ledger=ledger,
            audit=PluginAuditService(audit_repository),
        ),
    )
    with context.bind(invocation(gateway=gateway)):
        result = await context.messages.send_private(
            "10001",
            "failed-body-secret",
        )
    assert not result.ok
    assert result.error_code == "onebot.call_failed"
    assert result.detail == "RuntimeError"
    assert (
        await ledger.list_scope_recent(
            ConversationScope.private("99999", "10001"),
            limit=20,
        )
        == ()
    )
    audit_rows = await audit_repository.history(plugin_id="example.plugin")
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row.actor_user_id == "10001"
    assert row.operation == "message.send_private"
    assert row.permission == "message.private.send"
    assert not row.success
    assert row.error_category == "RuntimeError"
    assert row.detail == {}
    assert "failed-body-secret" not in repr(row)
    assert "exception-secret-must-not-leak" not in repr(row)


@pytest.mark.asyncio
async def test_image_and_web_context_do_not_revoke_authorized_side_effects() -> None:
    gateway = Gateway()
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(
            PluginPermission.MESSAGE_PRIVATE_SEND,
            PluginPermission.ONEBOT_MUTATE,
        ),
        superuser_ids=("90000",),
    )
    image = MessageAttachment(kind=AttachmentKind.IMAGE, label="image", file="file-id")
    with context.bind(invocation(user_id="90000", gateway=gateway, attachments=(image,))):
        sent = await context.messages.send_private("90000", "allowed")
        assert sent.ok

    with context.bind(invocation(user_id="90000", gateway=gateway, web_was_used=True)):
        changed = await context.onebot.call_mutating_action("delete_msg", {"message_id": 1})
        assert changed.ok
    assert [action for action, _params in gateway.calls] == ["send_private_msg", "delete_msg"]


@pytest.mark.asyncio
async def test_privileged_onebot_mutation_uses_real_superusers_and_direct_origin() -> None:
    gateway = Gateway()
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.ONEBOT_MUTATE,),
        superuser_ids=("90000",),
    )
    with context.bind(invocation(user_id="10001", gateway=gateway)):
        with pytest.raises(PluginPermissionError, match="SUPERUSERS"):
            await context.onebot.call_mutating_action("delete_msg", {"message_id": 1})

    with context.bind(invocation(user_id="90000", gateway=gateway)):
        result = await context.onebot.call_mutating_action(
            "delete_msg",
            {"message_id": 1},
        )
        assert result.ok
    assert gateway.calls == [("delete_msg", {"message_id": 1})]


def test_binding_rejects_other_plugin_and_spoofed_runtime_actor() -> None:
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.MESSAGE_CURRENT_READ,),
    )
    with pytest.raises(PluginPermissionError, match="another plugin"):
        with context.bind(invocation(plugin_id="other.plugin")):
            pass

    runtime = SimpleNamespace(
        inbound=inbound(user_id="10001"),
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="90000",
        gateway=None,
        runtime_config=None,
        delegated_authority=None,
        allowed_capabilities=(),
    )
    with pytest.raises(ValueError, match="actor must match"):
        context.invocation_scope(
            "example.plugin",
            runtime,
            web_was_used=False,
        )


def test_context_exposes_every_sdk_facade_but_not_dependency_bundle() -> None:
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(),
        services=PluginFacadeServices(),
    )
    names = (
        "messages",
        "people",
        "groups",
        "memory",
        "relationship",
        "llm",
        "agent",
        "agent_sessions",
        "web",
        "http",
        "vision",
        "media",
        "automation",
        "config",
        "secrets",
        "storage",
        "scheduler",
        "onebot",
        "events",
    )
    assert all(getattr(context, name) is not None for name in names)
    assert not hasattr(context, "services")
