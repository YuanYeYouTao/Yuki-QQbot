"""Unit tests for the conversation session lifecycle driver (R1 commit 2)."""

from __future__ import annotations

import pytest

from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.conversation.session import ConversationSessionLifecycle
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.contracts import (
    TerminalFinalization,
    TerminalFinalizationSource,
    ToolBatchExecutionResult,
    ToolCallOutcome,
)
from qq_ai_bot.runtime.delivery import (
    DeliveryItemKind,
    DeliveryItemOutcome,
    DeliveryItemSource,
    DeliveryOutcome,
)
from qq_ai_bot.runtime.errors import (
    DurableEffectViolationError,
    IllegalTurnTransitionError,
    UntrustedFinalizationError,
)
from qq_ai_bot.runtime.invariants import TurnPhase
from qq_ai_bot.runtime.keys import TurnCoordinationKey
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.result import DurableEffectState, TurnOutcome
from qq_ai_bot.runtime.trigger import MessageTurnTrigger
from qq_ai_bot.runtime.turn import TurnContext, UntrustedContent


class _StubConfig:
    pass


class _StubTime:
    pass


def _context() -> TurnContext:
    inbound = InboundMessage(
        message_id="m-1",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="u-1"),
        text="hi",
        bot_user_id="bot-9",
        group_id="g-1",
    )
    return TurnContext(
        trigger=MessageTurnTrigger(
            origin=TurnOrigin.USER_MESSAGE, inbound=inbound, ledger_event_id=1
        ),
        authority=TurnAuthority(
            actor_user_id="u-1",
            bot_user_id="bot-9",
            origin=TurnOrigin.USER_MESSAGE,
            permission_ceiling=frozenset({"user"}),
            delegated_authority=None,
            authority_revision=1,
        ),
        scene=TurnSceneFacts(scope_type=ScopeType.GROUP, group_id="g-1"),
        runtime_config=_StubConfig(),  # type: ignore[arg-type]
        scope=ConversationScope.group("bot-9", "g-1"),
        actor=inbound.sender,
        turn_snapshot=ConversationTurnSnapshot(1, "bot:bot-9:group:g-1", 1, 1, 1),
        coordination_key=TurnCoordinationKey.for_group("bot-9", "g-1"),
        turn_id="turn-1",
        turn_token=None,
        current_time=_StubTime(),  # type: ignore[arg-type]
        normalized_content=UntrustedContent(text="hi"),
        visual_observation=None,
    )


def _delivery(*, accepted: bool = True) -> DeliveryOutcome:
    return DeliveryOutcome(
        items=(
            DeliveryItemOutcome(
                kind=DeliveryItemKind.TEXT,
                source=DeliveryItemSource.AGENT_REPLY,
                transport_accepted=accepted,
                receipt="msg-1" if accepted else None,
                ledger_recorded=accepted,
                error_category=None if accepted else "send_failed",
            ),
        )
    )


def _terminal_batch() -> ToolBatchExecutionResult:
    return ToolBatchExecutionResult(
        tool_results=(ToolCallOutcome(call_id="c1", tool_name="memory_change", result_json="{}"),),
        terminal_finalization=TerminalFinalization(
            source=TerminalFinalizationSource.HOST_MEMORY_FINALIZER
        ),
    )


def _session_at_model() -> ConversationSessionLifecycle:
    session = ConversationSessionLifecycle(_context())
    session.admit()
    session.mark_prepared()
    session.model_started()
    return session


class TestLifecycleFlow:
    def test_plain_reply_flow(self) -> None:
        session = _session_at_model()
        session.finalize_from_model()
        session.delivery_started()
        delivery = _delivery()
        session.delivery_finished(delivery)
        assert session.phase is TurnPhase.DELIVERED
        session.close(TurnOutcome.COMPLETED)
        result = session.build_result(
            generated_text="ok",
            model_requests=1,
            tool_calls=0,
            delivery=delivery,
            outcome=TurnOutcome.COMPLETED,
        )
        assert result.outcome is TurnOutcome.COMPLETED
        assert result.durable_effect_state is DurableEffectState.NONE

    def test_tool_loop_then_model_finalize(self) -> None:
        session = _session_at_model()
        session.tools_started()
        session.tools_finished(
            ToolBatchExecutionResult(
                tool_results=(
                    ToolCallOutcome(call_id="c1", tool_name="web_search", result_json="{}"),
                )
            )
        )
        assert session.phase is TurnPhase.MODEL_ACTIVE
        session.finalize_from_model()
        assert session.phase is TurnPhase.FINALIZING

    def test_terminal_tool_batch_finalizes_directly(self) -> None:
        session = _session_at_model()
        session.tools_started()
        session.mutation_started()
        session.mutation_committed()
        session.tools_finished(_terminal_batch())
        assert session.phase is TurnPhase.FINALIZING
        assert session.state.taint.mutation_committed
        assert session.durable_effects is DurableEffectState.MUTATION_COMMITTED

    def test_model_finalize_is_illegal_during_tools(self) -> None:
        session = _session_at_model()
        session.tools_started()
        with pytest.raises(UntrustedFinalizationError):
            session.finalize_from_model()


class TestTerminalStates:
    def test_supersede_and_close_idempotency(self) -> None:
        session = _session_at_model()
        session.supersede()
        assert session.closed
        assert session.outcome is TurnOutcome.SUPERSEDED
        session.close(TurnOutcome.SUPERSEDED)
        with pytest.raises(IllegalTurnTransitionError):
            session.close(TurnOutcome.COMPLETED)

    def test_cancel(self) -> None:
        session = _session_at_model()
        session.cancel()
        assert session.outcome is TurnOutcome.CANCELLED

    def test_committed_mutation_blocks_plain_failure_close(self) -> None:
        session = _session_at_model()
        session.tools_started()
        session.mutation_started()
        session.mutation_committed()
        with pytest.raises(DurableEffectViolationError):
            session.close(TurnOutcome.TOOL_FAILED)
        session.close(TurnOutcome.COMMITTED_BUT_FINALIZATION_FAILED)
        assert session.outcome is TurnOutcome.COMMITTED_BUT_FINALIZATION_FAILED

    def test_failed_delivery_close(self) -> None:
        session = _session_at_model()
        session.finalize_from_model()
        session.delivery_started()
        session.delivery_finished(_delivery(accepted=False))
        assert session.phase is TurnPhase.DELIVERY_FAILED
        session.close(TurnOutcome.DELIVERY_FAILED)
        assert session.outcome is TurnOutcome.DELIVERY_FAILED
