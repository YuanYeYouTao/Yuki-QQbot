"""Four-path regression for Memory Runtime ownership (R2 C6)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.memory.attribution import MemoryExposure, MemoryExposureSource
from qq_ai_bot.memory.enums import MemoryRecallPurpose
from qq_ai_bot.memory.models import MemoryQueryIntent
from qq_ai_bot.memory.receipt import MemoryRecallTurn
from qq_ai_bot.memory.runtime.capability_view import build_capability_view
from qq_ai_bot.memory.runtime.contract import (
    MemoryAvailability,
    MemoryContextPolicy,
    MemoryReadPolicy,
    MemoryWritePolicy,
    MemoryWriteTransition,
)
from qq_ai_bot.memory.runtime.errors import MemoryRuntimeError
from qq_ai_bot.memory.runtime.query_plane import MemoryQueryPlane, MemoryReadConsumer
from qq_ai_bot.memory.runtime.resolver import MemoryStructuredCommand, resolve_memory_access
from qq_ai_bot.memory.runtime.state import LocatorStatus, MutationState
from qq_ai_bot.memory.runtime.turn_session import (
    TurnMemorySession,
    apply_memory_tool_groups,
    empty_retrieval,
    scene_from_inbound,
)
from qq_ai_bot.runtime.authority import TurnAuthority
from qq_ai_bot.runtime.contracts import DeliverySummary
from qq_ai_bot.runtime.delivery import DeliveryStatus
from qq_ai_bot.runtime.origin import TurnOrigin


class _FakeMemoryContext:
    def __init__(self) -> None:
        self.retrieve_calls = 0
        self.mark_injected_calls: list[tuple[int, ...]] = []
        self.record_recall_calls: list[tuple[int, ...]] = []

    async def retrieve_for_turn(self, **_kwargs: object) -> object:
        self.retrieve_calls += 1
        return empty_retrieval()

    async def search(self, **_kwargs: object) -> object:
        return empty_retrieval()

    async def mark_injected(self, _result: object, fact_ids: tuple[int, ...]) -> int:
        self.mark_injected_calls.append(fact_ids)
        return len(fact_ids)

    async def record_recall(self, **kwargs: object) -> MemoryRecallTurn:
        injected = kwargs.get("injected_fact_ids")
        assert isinstance(injected, tuple)
        self.record_recall_calls.append(injected)
        return MemoryRecallTurn(
            turn_id=f"receipt-{len(self.record_recall_calls)}",
            injected_fact_ids=injected,
        )


def _inbound(*, image: bool = False, reply: bool = False) -> InboundMessage:
    from qq_ai_bot.domain.messages import AttachmentKind, MessageAttachment

    return InboundMessage(
        message_id="m-path",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("1001"),
        text="普通一句话",
        bot_user_id="bot-9",
        reply_text="刚才那句" if reply else "",
        attachments=((MessageAttachment(AttachmentKind.IMAGE, "image"),) if image else ()),
    )


def _authority(origin: TurnOrigin = TurnOrigin.USER_MESSAGE) -> TurnAuthority:
    return TurnAuthority(
        actor_user_id="1001",
        bot_user_id="bot-9",
        origin=origin,
        permission_ceiling=frozenset(),
        delegated_authority=None,
        authority_revision=1,
    )


def _runtime() -> Any:
    return SimpleNamespace(
        memory=SimpleNamespace(
            retrieval_enabled=True,
            usage_attribution_enabled=True,
        )
    )


def _open(
    context: _FakeMemoryContext,
    *,
    command: MemoryStructuredCommand = MemoryStructuredCommand.NONE,
    image: bool = False,
    reply: bool = False,
    origin: TurnOrigin = TurnOrigin.USER_MESSAGE,
    memory_available: bool = True,
) -> TurnMemorySession:
    inbound = _inbound(image=image, reply=reply)
    return TurnMemorySession.open(
        inbound=inbound,
        identity=ConversationIdentity.private("1001"),
        runtime=_runtime(),
        memory_context=context,  # type: ignore[arg-type]
        origin=origin,
        user_question=inbound.text,
        authority=_authority(origin),
        structured_command=command,
        image_present=image,
        memory_available=memory_available,
    )


class TestPathContracts:
    def test_ordinary_language_is_passive(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=scene_from_inbound(_inbound()),
        )
        assert decision.contract.context_policy is MemoryContextPolicy.BACKGROUND
        assert decision.contract.read_policy is MemoryReadPolicy.DEFERRED
        assert decision.contract.write_policy is MemoryWritePolicy.DISABLED
        assert decision.contract.write_transition is MemoryWriteTransition.REQUESTABLE
        view = build_capability_view(decision.contract, transition_revision=1)
        assert apply_memory_tool_groups(view, frozenset({"memory", "web"})) == frozenset({"web"})

    def test_structured_read_is_active_without_inject(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=scene_from_inbound(_inbound()),
            structured_command=MemoryStructuredCommand.READ,
        )
        assert decision.contract.read_policy is MemoryReadPolicy.EAGER
        assert decision.contract.context_policy is MemoryContextPolicy.NONE
        view = build_capability_view(decision.contract, transition_revision=1)
        assert "memory" in apply_memory_tool_groups(view, frozenset({"web"}))

    def test_structured_write_is_exclusive(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=scene_from_inbound(_inbound()),
            structured_command=MemoryStructuredCommand.WRITE,
        )
        assert decision.contract.write_policy is MemoryWritePolicy.EXCLUSIVE
        assert decision.contract.context_policy is MemoryContextPolicy.NONE
        view = build_capability_view(decision.contract, transition_revision=1)
        assert view.exclusive_namespace == "memory.state.write"

    def test_image_turn_keeps_context_and_denies_write(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=scene_from_inbound(_inbound(image=True), image_present=True),
        )
        assert decision.contract.context_policy is MemoryContextPolicy.BACKGROUND
        assert decision.contract.write_transition is MemoryWriteTransition.DENIED
        assert decision.contract.write_policy is MemoryWritePolicy.DISABLED

    def test_forbidden_hides_every_namespace(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=scene_from_inbound(_inbound()),
            memory_available=False,
        )
        assert decision.contract.availability is MemoryAvailability.FORBIDDEN
        view = build_capability_view(decision.contract, transition_revision=1)
        assert view.eager_namespaces == ()
        assert apply_memory_tool_groups(view, frozenset({"memory", "web"})) == frozenset({"web"})


class TestPrefetchReceiptDelay:
    @pytest.mark.asyncio
    async def test_prefetch_does_not_write_injected_or_receipt(self) -> None:
        context = _FakeMemoryContext()
        session = _open(context)
        result = await session.prefetch()
        assert result is not None
        assert context.retrieve_calls == 1
        assert context.mark_injected_calls == []
        assert context.record_recall_calls == []

    @pytest.mark.asyncio
    async def test_confirm_after_staging_writes_one_receipt(self) -> None:
        context = _FakeMemoryContext()
        session = _open(context)
        await session.prefetch()
        session.stage_prompt_selection(
            (11,),
            (
                MemoryExposure(
                    memory_ref="M11",
                    fact_id=11,
                    kind="fact",
                    category="pref",
                    content="喜欢美式",
                    occurred_at=None,
                    target_role="current_person",
                    source=MemoryExposureSource.AUTOMATIC,
                ),
            ),
        )
        handle = await session.confirm_prompt_exposure()
        assert handle is not None
        assert handle.receipt_turn_id == "receipt-1"
        assert context.mark_injected_calls == [(11,)]
        assert context.record_recall_calls == [(11,)]
        second = await session.confirm_prompt_exposure()
        assert second is None
        assert context.record_recall_calls == [(11,)]

    @pytest.mark.asyncio
    async def test_exclusive_write_skips_prefetch(self) -> None:
        context = _FakeMemoryContext()
        session = _open(context, command=MemoryStructuredCommand.WRITE)
        assert session.exclusive_write
        assert await session.prefetch() is None
        assert context.retrieve_calls == 0


class TestLocatorBound:
    @pytest.mark.asyncio
    async def test_one_read_and_one_retry_then_rejected(self) -> None:
        session = _open(_FakeMemoryContext(), command=MemoryStructuredCommand.WRITE)
        ambiguous = json.dumps(
            {
                "ok": False,
                "error": "memory_candidate_ambiguous",
                "data": {
                    "applied_operation": "noop",
                    "outcome": "rejected",
                    "reason_code": "memory_candidate_ambiguous",
                },
            },
            ensure_ascii=False,
        )
        await session.observe_tool_result("memory_change", ambiguous)
        assert session.locator_open
        assert session._state.mutation_state is MutationState.AMBIGUOUS

        await session.observe_tool_result(
            "get_person_memories",
            json.dumps(
                {
                    "ok": True,
                    "data": {
                        "memories": [
                            {"memory_ref": "M11", "content": "咖啡", "kind": "fact"},
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        )
        assert session._state.locator_status is LocatorStatus.CONSUMED
        assert not session.locator_open

        await session.observe_tool_result("memory_change", ambiguous)
        assert session._state.mutation_state is MutationState.REJECTED
        assert session.mutation_terminal
        assert "不能唯一定位" in (session.finalize_text() or "") or "未执行" in (
            session.finalize_text() or ""
        )


class TestExposureSafety:
    @pytest.mark.asyncio
    async def test_plugin_and_admin_cannot_publish_exposure(self) -> None:
        plane = MemoryQueryPlane(_FakeMemoryContext())
        kwargs = {
            "conversation_key": "private:1001",
            "trigger_message_id": "m1",
            "origin": TurnOrigin.USER_MESSAGE.value,
            "intent": MemoryQueryIntent(purpose=MemoryRecallPurpose.BACKGROUND),
            "result": empty_retrieval(),
            "injected_fact_ids": (1,),
            "runtime": _runtime(),
        }
        with pytest.raises(MemoryRuntimeError, match="side-effect free"):
            await plane.publish_exposure(MemoryReadConsumer.PLUGIN, **kwargs)
        with pytest.raises(MemoryRuntimeError, match="side-effect free"):
            await plane.publish_exposure(MemoryReadConsumer.ADMIN, **kwargs)

    @pytest.mark.asyncio
    async def test_cancelled_delivery_skips_attribution(self) -> None:
        jobs: list[object] = []
        session = _open(_FakeMemoryContext())
        session._attribution = SimpleNamespace(enqueue=jobs.append)  # type: ignore[assignment]
        await session.prefetch()
        session.stage_prompt_selection((11,), ())
        await session.confirm_prompt_exposure()
        await session.on_delivery_confirmed(
            DeliverySummary(
                final_agent_run_id="m-path",
                status=DeliveryStatus.CANCELLED,
                delivered_text="已送达但被取消",
            )
        )
        assert jobs == []


class TestRequestWriteTransition:
    def test_passive_can_enter_exclusive_write(self) -> None:
        session = _open(_FakeMemoryContext())
        assert not session.exclusive_write
        session.request_exclusive_write()
        assert session.exclusive_write
        assert session.receipt_gated
        assert session.finalize_text() is not None
