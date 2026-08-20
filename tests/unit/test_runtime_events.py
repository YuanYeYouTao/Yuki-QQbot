"""Content-free runtime events that replace Planner notifications."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Message
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_autonomous_group_tasks import _conversation_config, _group_message
from tests.unit.test_normalizer import group_event, private_event

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.automation.models import TurnOrigin as AutomationTurnOrigin
from qq_ai_bot.capabilities.catalog import (
    DescriptorRegistrySnapshot,
    UnifiedToolCatalog,
    UnifiedToolCatalogEntry,
)
from qq_ai_bot.capabilities.models import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext
from qq_ai_bot.capabilities.runtime import (
    CapabilityIndexCache,
    CapabilityQuery,
    CapabilitySearchReport,
    TurnCapabilityRuntime,
)
from qq_ai_bot.conversation.participation import AdmissionFeatures
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.observability import stable_identifier_hash
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService, _GroupState
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from yuki_plugin_sdk.events import EventEnvelope, EventName


async def _capture(bus: PluginEventBus, *names: EventName) -> list[EventEnvelope]:
    events: list[EventEnvelope] = []

    async def capture(event: EventEnvelope) -> None:
        events.append(event)

    for name in names:
        bus.subscribe(
            plugin_id="com.example.runtime-events",
            hook_id=name.value,
            event=name,
            handler=capture,
        )
    return events


@pytest.mark.asyncio
async def test_policy_reject_emits_turn_rejected_without_body(database: Database) -> None:
    settings = make_settings(database.url)
    harness = build_harness(database, settings)
    bus = PluginEventBus(default_timeout_seconds=0.2)
    events = await _capture(bus, EventName.TURN_REJECTED, EventName.TURN_ADMITTED)
    harness.processor.set_event_publisher(bus)
    secret = "reject-body-MUST-NOT-LEAK"

    result = await harness.processor.handle(
        normalize_event(private_event(Message(secret), user_id=9999, message_id=801)),
        MemorySender(),
    )

    assert result.reason == "bot_message"
    assert [event.name for event in events] == [EventName.TURN_REJECTED]
    payload = events[0].payload
    assert payload["reason"] == "bot_message"
    assert payload["origin"] == "user_message"
    assert secret not in str(payload)
    assert "private:9999" not in str(payload)


@pytest.mark.asyncio
async def test_group_observed_does_not_emit_turn_rejected(database: Database) -> None:
    settings = make_settings(database.url, conversation_autonomous_enabled=False)
    harness = build_harness(database, settings)
    bus = PluginEventBus(default_timeout_seconds=0.2)
    events = await _capture(
        bus,
        EventName.TURN_REJECTED,
        EventName.TURN_ADMITTED,
        EventName.AUTONOMOUS_DECLINED,
    )
    harness.processor.set_event_publisher(bus)

    result = await harness.processor.handle(
        normalize_event(group_event(Message("闲聊一句"), message_id=802)),
        MemorySender(),
    )

    assert result.reason == "group_observed"
    assert EventName.TURN_REJECTED not in {event.name for event in events}
    assert EventName.TURN_ADMITTED not in {event.name for event in events}


@pytest.mark.asyncio
async def test_autonomous_decline_emits_score_and_reason_codes() -> None:
    publisher = _RecordingPublisher()
    coordinator = ConversationTurnCoordinator()
    chat = SimpleNamespace(
        _runtime_config=_SnapshotRuntime(),
        _turn_coordinator=coordinator,
        _event_publisher=publisher,
        respond=AsyncMock(),
    )
    service = AutonomousGroupService(
        chat=cast(Any, chat),
        admission_features=cast(Any, _LowValueFeatures()),
        runtime_config=cast(Any, _SnapshotRuntime()),
        turn_coordinator=coordinator,
    )
    message = _group_message("1", "哈哈")
    state = _GroupState()
    state.messages.append(message)
    state.profiles.append(
        UserProfileSnapshot(
            user_id="1001",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            group_card="远野",
        )
    )
    state.senders.append(cast(Any, object()))
    state.revision = 1
    scope_key = message.scope().key
    state.latest_token = await coordinator.notify_message(scope_key, observation=True)
    service._states[scope_key] = state

    await service._admit_latest(scope_key, 1, await _SnapshotRuntime().snapshot())

    assert chat.respond.await_count == 0
    assert [event.name for event in publisher.events] == [EventName.AUTONOMOUS_DECLINED]
    payload = publisher.events[0].payload
    assert payload["origin"] == "autonomous_group"
    assert payload["conversation_key_hash"] == stable_identifier_hash(
        scope_key,
        kind="conversation",
    )
    assert payload["score"] < payload["threshold"]
    assert "low_value_reaction" in payload["reasons"]
    assert "哈哈" not in str(payload)
    await service.close()


def test_capability_search_report_exposes_ids_not_query_text() -> None:
    reports: list[CapabilitySearchReport] = []
    descriptor = CapabilityDescriptor(
        canonical_name="web_search",
        model_name="web_search",
        group="web.search",
        namespace="web.search",
        description="search the public web for recent news",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        effect=CapabilityEffect.EXTERNAL_READ,
        risk=CapabilityRisk.READ,
        trust_source=CapabilityTrustSource.CORE,
        allowed_origins=frozenset(AutomationTurnOrigin),
        required_permissions=frozenset(),
        uses_external_data=True,
        cancellable=True,
        idempotency=CapabilityIdempotency.IDEMPOTENT,
        use_when=("search the web",),
        aliases=("web",),
    )
    entry = UnifiedToolCatalogEntry(
        descriptor=descriptor,
        provider_id="core",
        scope_ids=descriptor.scope_ids,
        compact_description=descriptor.description,
        tags=(),
        searchable_text="web_search search the public web",
        estimated_schema_tokens=12,
        available=True,
        revision="1",
    )
    catalog = UnifiedToolCatalog(entries=(entry,), scopes=(), revision="abcd1234")
    snapshot = DescriptorRegistrySnapshot(catalog)
    runtime = TurnCapabilityRuntime(
        registry=snapshot,
        index=CapabilityIndexCache().index_for(snapshot),
        authority=TurnAuthority(
            actor_user_id="1001",
            bot_user_id="9999",
            origin=TurnOrigin.USER_MESSAGE,
            permission_ceiling=frozenset(),
            delegated_authority=None,
            authority_revision=1,
        ),
        scene=TurnSceneFacts(scope_type=ScopeType.PRIVATE, group_id=None),
        memory_view=None,
        policy_context=CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="1001", is_superuser=False),
            origin=AutomationTurnOrigin.USER_MESSAGE,
        ),
        append_only=False,
        on_searched=reports.append,
    )

    runtime.initial_exposure(
        CapabilityQuery(
            text="search the web for news",
            origin=TurnOrigin.USER_MESSAGE,
            reply_excerpt="SECRET-REPLY-EXCERPT",
        )
    )
    assert reports == []
    assert "web_search" not in runtime.callable_capability_ids()

    asyncio.run(
        runtime.search(
            CapabilityQuery(
                text="search the web for news",
                origin=TurnOrigin.USER_MESSAGE,
                reply_excerpt="SECRET-REPLY-EXCERPT",
            )
        )
    )

    assert len(reports) == 1
    report = reports[0]
    assert report.origin == "user_message"
    assert report.hit_count == len(report.capability_ids)
    assert "web_search" in report.capability_ids
    assert "SECRET-REPLY-EXCERPT" not in report.capability_ids
    assert "search the web for news" not in report.capability_ids
    names = [tool.name for tool in runtime.definitions()]
    assert names.count("request_tools") == 1
    assert len(names) == len(set(names))


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    async def publish(self, event: EventEnvelope) -> object:
        self.events.append(event)
        return None


class _SnapshotRuntime:
    async def snapshot(self, **_kwargs: object) -> object:
        policy = _conversation_config()
        return SimpleNamespace(conversation=policy, conversation_policy=lambda: policy)


class _LowValueFeatures:
    async def admission_features(self, **_kwargs: object) -> AdmissionFeatures:
        return AdmissionFeatures(scope_type=ScopeType.GROUP, text="哈哈")
