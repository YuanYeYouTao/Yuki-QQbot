"""R1 integration test: fake runtime drives a full turn without the planner.

Flow under test (R1 §10.2)::

    inbound -> begin_turn -> prepare -> fake agent -> deliver -> close

No planner service, no production message path — only the new protocols,
the session lifecycle driver and the shared turn types.
"""

from __future__ import annotations

from qq_ai_bot.conversation.runtime import (
    ConversationRuntime,
    ConversationTurnSession,
    PreparedTurn,
)
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.conversation.session import ConversationSessionLifecycle
from qq_ai_bot.conversation.state import TurnEffectContext
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage, SenderIdentity
from qq_ai_bot.memory.enums import MemoryRecallPurpose
from qq_ai_bot.memory.runtime.capability_view import build_capability_view
from qq_ai_bot.memory.runtime.contract import (
    MemoryAvailability,
    MemoryContextPolicy,
    MemoryFinalizationPolicy,
    MemoryReadPolicy,
    MemoryTurnContract,
    MemoryWritePolicy,
    MemoryWriteTransition,
)
from qq_ai_bot.memory.runtime.resolver import resolve_scope_from_scene
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.contracts import CapabilityExposureSnapshot
from qq_ai_bot.runtime.delivery import (
    DeliveryItemKind,
    DeliveryItemOutcome,
    DeliveryItemSource,
    DeliveryOutcome,
)
from qq_ai_bot.runtime.invariants import TurnPhase
from qq_ai_bot.runtime.keys import TurnCoordinationKey
from qq_ai_bot.runtime.observability import (
    RuntimeTurnCorrelation,
    bind_runtime_turn,
    claim_runtime_turn_id,
    new_runtime_turn_id,
)
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.result import TurnOutcome, TurnResult
from qq_ai_bot.runtime.trigger import MessageTurnTrigger
from qq_ai_bot.runtime.turn import ReplyTargetControl, TurnContext, TurnState, UntrustedContent
from qq_ai_bot.services.agent_runner import AgentRunResult


class _StubConfig:
    pass


class _StubTime:
    pass


def _make_context(turn_id: str) -> TurnContext:
    inbound = InboundMessage(
        message_id="m-1",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="u-1", nickname="tester"),
        text="早上好",
        bot_user_id="bot-9",
        group_id="g-1",
    )
    return TurnContext(
        trigger=MessageTurnTrigger(
            origin=TurnOrigin.USER_MESSAGE, inbound=inbound, ledger_event_id=11
        ),
        authority=TurnAuthority(
            actor_user_id="u-1",
            bot_user_id="bot-9",
            origin=TurnOrigin.USER_MESSAGE,
            permission_ceiling=frozenset({"user"}),
            delegated_authority=None,
            authority_revision=1,
        ),
        scene=TurnSceneFacts(scope_type=ScopeType.GROUP, group_id="g-1", mentions_bot=True),
        runtime_config=_StubConfig(),  # type: ignore[arg-type]
        scope=ConversationScope.group("bot-9", "g-1"),
        actor=inbound.sender,
        turn_snapshot=ConversationTurnSnapshot(1, "bot:bot-9:group:g-1", 1, 11, 1),
        coordination_key=TurnCoordinationKey.for_group("bot-9", "g-1"),
        turn_id=turn_id,
        turn_token=None,
        current_time=_StubTime(),  # type: ignore[arg-type]
        normalized_content=UntrustedContent(text="早上好"),
        visual_observation=None,
    )


def _passive_contract() -> MemoryTurnContract:
    return MemoryTurnContract(
        context_policy=MemoryContextPolicy.NONE,
        read_policy=MemoryReadPolicy.DEFERRED,
        write_policy=MemoryWritePolicy.DISABLED,
        write_transition=MemoryWriteTransition.REQUESTABLE,
        finalization_policy=MemoryFinalizationPolicy.NORMAL,
        availability=MemoryAvailability.ENABLED,
        default_purpose=MemoryRecallPurpose.BACKGROUND,
    )


class FakeTurnSession:
    """Minimal but honest ConversationTurnSession implementation."""

    def __init__(self, context: TurnContext) -> None:
        self._lifecycle = ConversationSessionLifecycle(context)
        self._lifecycle.admit()
        self.phases_seen: list[TurnPhase] = [self._lifecycle.phase]

    @property
    def context(self) -> TurnContext:
        return self._lifecycle.context

    @property
    def state(self) -> TurnState:
        return self._lifecycle.state

    @property
    def lifecycle(self) -> ConversationSessionLifecycle:
        return self._lifecycle

    async def prepare(self) -> PreparedTurn:
        context = self._lifecycle.context
        memory_scope = resolve_scope_from_scene(authority=context.authority, scene=context.scene)
        assert memory_scope.partition_key == "group:g-1"
        memory_view = build_capability_view(_passive_contract(), transition_revision=1)
        assert memory_view.exclusive_namespace is None

        self._lifecycle.mark_prepared()
        self.phases_seen.append(self._lifecycle.phase)
        return PreparedTurn(
            model_messages=(
                ChatMessage(role="system", content="you are yuki"),
                ChatMessage(role="user", content=context.normalized_content.text),
            ),
            memory_session_id="mem-session-1",
            capability_exposure=CapabilityExposureSnapshot(
                revision=1, exposed_capability_ids=("request_tools",)
            ),
            reply_target=ReplyTargetControl(reply_to_message_id="m-1", pinned=False),
            effect_context=TurnEffectContext(allowed_effect_kinds=frozenset({"emoji"})),
        )

    async def run_agent(self, prepared: PreparedTurn) -> AgentRunResult:
        self._lifecycle.model_started()
        self.phases_seen.append(self._lifecycle.phase)
        assert claim_runtime_turn_id() == self._lifecycle.context.turn_id
        return AgentRunResult(
            text="早上好呀！",
            tool_calls_used=0,
            model_requests=1,
            web_was_used=False,
        )

    async def deliver(self, result: AgentRunResult) -> TurnResult:
        self._lifecycle.finalize_from_model()
        self._lifecycle.delivery_started()
        self.phases_seen.extend([TurnPhase.FINALIZING, TurnPhase.DELIVERING])
        delivery = DeliveryOutcome(
            items=(
                DeliveryItemOutcome(
                    kind=DeliveryItemKind.TEXT,
                    source=DeliveryItemSource.AGENT_REPLY,
                    transport_accepted=True,
                    receipt="platform-msg-77",
                    ledger_recorded=True,
                ),
            )
        )
        self._lifecycle.delivery_finished(delivery)
        self.phases_seen.append(self._lifecycle.phase)
        return self._lifecycle.build_result(
            generated_text=result.text,
            model_requests=result.model_requests,
            tool_calls=result.tool_calls_used,
            delivery=delivery,
            outcome=TurnOutcome.COMPLETED,
        )

    async def close(self) -> None:
        self._lifecycle.close(TurnOutcome.COMPLETED)


class FakeConversationRuntime:
    async def begin_turn(self, context: TurnContext) -> FakeTurnSession:
        return FakeTurnSession(context)


async def test_full_fake_turn_flow() -> None:
    runtime = FakeConversationRuntime()
    # Static protocol conformance (would fail type/attribute checks otherwise).
    _runtime_check: ConversationRuntime = runtime
    turn_id = new_runtime_turn_id()
    context = _make_context(turn_id)
    correlation = RuntimeTurnCorrelation(turn_id=turn_id, origin=context.trigger.origin)

    with bind_runtime_turn(correlation):
        session = await runtime.begin_turn(context)
        _session_check: ConversationTurnSession = session
        prepared = await session.prepare()
        assert prepared.capability_exposure.exposed_capability_ids == ("request_tools",)
        agent_result = await session.run_agent(prepared)
        turn_result = await session.deliver(agent_result)
        await session.close()

    assert correlation.touched, "agent phase must have claimed the runtime turn id"
    assert session.phases_seen == [
        TurnPhase.ADMITTED,
        TurnPhase.PREPARED,
        TurnPhase.MODEL_ACTIVE,
        TurnPhase.FINALIZING,
        TurnPhase.DELIVERING,
        TurnPhase.DELIVERED,
    ]
    assert turn_result.outcome is TurnOutcome.COMPLETED
    assert turn_result.generated_text == "早上好呀！"
    assert turn_result.delivery.sent_messages == 1
    assert turn_result.delivery.agent_body_delivered
    assert session.lifecycle.closed

    # close() is idempotent.
    await session.close()


async def test_correlation_does_not_leak_between_turns() -> None:
    runtime = FakeConversationRuntime()
    first_id = new_runtime_turn_id()
    second_id = new_runtime_turn_id()

    with bind_runtime_turn(
        RuntimeTurnCorrelation(turn_id=first_id, origin=TurnOrigin.USER_MESSAGE)
    ):
        session = await runtime.begin_turn(_make_context(first_id))
        prepared = await session.prepare()
        await session.run_agent(prepared)

    assert claim_runtime_turn_id() is None, "correlation must not survive its context"

    with bind_runtime_turn(
        RuntimeTurnCorrelation(turn_id=second_id, origin=TurnOrigin.USER_MESSAGE)
    ):
        assert claim_runtime_turn_id() == second_id
