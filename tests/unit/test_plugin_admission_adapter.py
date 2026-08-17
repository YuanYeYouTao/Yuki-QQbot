"""Focused contracts for admission signals contributed by Plugin API 2.0."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.planner.models import PlannerSignal
from qq_ai_bot.plugin_host.admission_adapter import PluginAdmissionSignalAdapter
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from yuki_plugin_sdk.models import (
    AdmissionSignal as SdkAdmissionSignal,
)
from yuki_plugin_sdk.models import (
    AdmissionSignalContext,
)
from yuki_plugin_sdk.models import (
    TurnOrigin as SdkTurnOrigin,
)
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import AdmissionSignalProvider, AdmissionSignalRegistration


def _runtime() -> RuntimeConfigSnapshot:
    """The adapter treats the runtime snapshot as opaque and forwards its identity."""

    return cast(RuntimeConfigSnapshot, object())


def _group_message() -> InboundMessage:
    return InboundMessage(
        message_id="message-42",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(
            user_id="10001",
            nickname="Alice",
            group_card="Group Alice",
        ),
        text="hello from the real sender",
        bot_user_id="90001",
        raw_text="hello from the real sender",
        group_id="20002",
        mentions_bot=True,
        received_at=datetime(2026, 7, 28, 3, 4, 5, tzinfo=UTC),
    )


def _register(
    registry: ExtensionRegistry,
    plugin_id: str,
    name: str,
    provider: AdmissionSignalProvider,
) -> None:
    registry.registrar(
        plugin_id,
        (PluginPermission.ADMISSION_SIGNAL_REGISTER,),
    ).register_admission_signal(AdmissionSignalRegistration(name=name, provider=provider))


async def test_contextual_provider_receives_real_message_origin_and_scope() -> None:
    registry = ExtensionRegistry()
    message = _group_message()
    runtime = _runtime()
    received_contexts: list[AdmissionSignalContext] = []
    scope_calls: list[tuple[str, InboundMessage, TurnOrigin, RuntimeConfigSnapshot]] = []

    async def provider(context: AdmissionSignalContext) -> SdkAdmissionSignal:
        received_contexts.append(context)
        return SdkAdmissionSignal(
            source_plugin_id="spoofed-by-provider",
            score_delta=4,
            reason_code="group_context",
            summary="Relevant to this group turn",
            confidence=0.9,
        )

    @asynccontextmanager
    async def invocation_scope(
        plugin_id: str,
        inbound: InboundMessage,
        origin: TurnOrigin,
        snapshot: RuntimeConfigSnapshot,
    ) -> AsyncIterator[None]:
        scope_calls.append((plugin_id, inbound, origin, snapshot))
        yield

    _register(registry, "com.example.context", "context", provider)
    adapter = PluginAdmissionSignalAdapter(
        registry,
        invocation_scope=invocation_scope,
    )

    signals = await adapter.collect(
        message=message,
        origin=TurnOrigin.AUTONOMOUS_GROUP,
        runtime=runtime,
    )

    assert scope_calls == [("com.example.context", message, TurnOrigin.AUTONOMOUS_GROUP, runtime)]
    assert len(received_contexts) == 1
    context = received_contexts[0]
    assert context.conversation_key == "group:20002"
    assert context.origin is SdkTurnOrigin.AUTONOMOUS_GROUP
    assert context.text_is_untrusted is True
    assert context.current.message_id == "message-42"
    assert context.current.sender_user_id == "10001"
    assert context.current.scope_type == "group"
    assert context.current.group_id == "20002"
    assert context.current.text == "hello from the real sender"
    assert context.current.received_at == message.received_at
    assert len(signals) == 1
    assert isinstance(signals[0], SdkAdmissionSignal)
    assert not isinstance(signals[0], PlannerSignal)
    assert signals[0].source_plugin_id == "com.example.context"


async def test_parameterless_provider_still_collects() -> None:
    registry = ExtensionRegistry()
    calls = 0

    async def legacy_provider() -> SdkAdmissionSignal:
        nonlocal calls
        calls += 1
        return SdkAdmissionSignal(
            source_plugin_id="legacy-value-is-not-authority",
            score_delta=-2,
            reason_code="legacy_provider",
            summary="Parameterless callback still works",
            confidence=0.75,
        )

    _register(registry, "com.example.legacy", "legacy", legacy_provider)
    adapter = PluginAdmissionSignalAdapter(registry)

    signals = await adapter.collect(
        message=_group_message(),
        origin=TurnOrigin.USER_MESSAGE,
        runtime=_runtime(),
    )

    assert calls == 1
    assert len(signals) == 1
    assert signals[0].source_plugin_id == "com.example.legacy"
    assert signals[0].reason_code == "legacy_provider"


async def test_provider_and_invocation_scope_failures_are_isolated() -> None:
    registry = ExtensionRegistry()

    async def good_provider(_context: AdmissionSignalContext) -> SdkAdmissionSignal:
        return SdkAdmissionSignal(
            source_plugin_id="ignored",
            score_delta=3,
            reason_code="healthy",
            summary="The healthy plugin still contributes",
            confidence=1,
        )

    async def failing_provider(_context: AdmissionSignalContext) -> SdkAdmissionSignal:
        raise RuntimeError("one plugin must not break planning")

    async def provider_behind_broken_scope(
        _context: AdmissionSignalContext,
    ) -> SdkAdmissionSignal:
        raise AssertionError("a provider must not run when its scope cannot bind")

    @asynccontextmanager
    async def invocation_scope(
        plugin_id: str,
        _message: InboundMessage,
        _origin: TurnOrigin,
        _runtime_snapshot: RuntimeConfigSnapshot,
    ) -> AsyncIterator[None]:
        if plugin_id == "com.example.broken_scope":
            raise RuntimeError("synthetic binding failure")
        yield

    _register(registry, "com.example.good", "good", good_provider)
    _register(registry, "com.example.broken", "broken", failing_provider)
    _register(
        registry,
        "com.example.broken_scope",
        "broken_scope",
        provider_behind_broken_scope,
    )
    adapter = PluginAdmissionSignalAdapter(
        registry,
        invocation_scope=invocation_scope,
    )

    signals = await adapter.collect(
        message=_group_message(),
        origin=TurnOrigin.AUTONOMOUS_GROUP,
        runtime=_runtime(),
    )

    assert [(signal.source_plugin_id, signal.reason_code) for signal in signals] == [
        ("com.example.good", "healthy")
    ]
