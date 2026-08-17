from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.conversation.participation import AdmissionFeatures
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService, _GroupState
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator


class _FailingRuntime:
    async def snapshot(self, **_kwargs: object) -> object:
        raise SQLAlchemyError("database unavailable")


class _WorkingRuntime:
    async def snapshot(self, **_kwargs: object) -> object:
        return SimpleNamespace(
            planner=SimpleNamespace(group_enabled=True, group_debounce_seconds=0.02)
        )


def _service() -> AutonomousGroupService:
    chat = SimpleNamespace(_runtime_config=_FailingRuntime(), _turn_coordinator=object())
    return AutonomousGroupService(
        chat=cast(Any, chat),
        admission_features=cast(Any, object()),
        runtime_config=cast(Any, _FailingRuntime()),
        turn_coordinator=cast(Any, object()),
    )


def _working_service() -> AutonomousGroupService:
    runtime = _WorkingRuntime()
    chat = SimpleNamespace(_runtime_config=runtime, _turn_coordinator=object())
    return AutonomousGroupService(
        chat=cast(Any, chat),
        admission_features=cast(Any, object()),
        runtime_config=cast(Any, runtime),
        turn_coordinator=cast(Any, object()),
    )


def _group_message(message_id: str, text: str) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", group_card="远野"),
        text=text,
        bot_user_id="9999",
        group_id="2001",
    )


@pytest.mark.asyncio
async def test_after_silence_observes_sqlalchemy_failure() -> None:
    service = _service()
    await service._after_silence("2001")
    assert service.task_failures == 1


@pytest.mark.asyncio
async def test_task_owner_consumes_unexpected_failure_and_clears_reference() -> None:
    service = _service()
    service._states["2001"] = _GroupState()

    async def fail() -> None:
        raise LookupError("unexpected")

    task = asyncio.create_task(fail())
    service._states["2001"].task = task
    task.add_done_callback(lambda completed: service._task_done("2001", completed))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert service._states["2001"].task is None
    assert service.task_failures == 1


@pytest.mark.asyncio
async def test_task_owner_treats_cancellation_as_normal() -> None:
    service = _service()
    service._states["2001"] = _GroupState()

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    service._states["2001"].task = task
    task.add_done_callback(lambda completed: service._task_done("2001", completed))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert service._states["2001"].task is None
    assert service.task_failures == 0


@pytest.mark.asyncio
async def test_group_updates_share_one_worker_and_plan_only_latest_quiet_revision() -> None:
    service = _working_service()
    profile = UserProfileSnapshot(
        user_id="1001",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        group_card="远野",
    )
    sender = cast(Any, object())

    with patch.object(service, "_run_latest", new_callable=AsyncMock) as run_latest:
        service.observe(_group_message("1", "第一条"), profile, sender)
        first_task = service._states["2001"].task
        await asyncio.sleep(0.005)
        service.observe(_group_message("2", "第二条"), profile, sender)

        assert service._states["2001"].task is first_task
        await service.wait_until_idle("2001")

    assert run_latest.await_count == 1
    assert run_latest.await_args is not None
    assert run_latest.await_args.args[:2] == ("2001", 2)
    await service.close()


@pytest.mark.asyncio
async def test_stale_admission_result_cannot_start_agent_or_tools() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingContext:
        async def admission_features(self, **_kwargs: object) -> AdmissionFeatures:
            started.set()
            await release.wait()
            return AdmissionFeatures(
                scope_type=ScopeType.GROUP,
                text="请帮我查一下这是什么？你觉得怎么样",
                pending_message_count=8,
                idle_seconds=90,
                recent_total_messages=8,
            )

    runtime_service = _WorkingRuntime()
    coordinator = ConversationTurnCoordinator()
    chat = SimpleNamespace(
        _runtime_config=runtime_service,
        _turn_coordinator=coordinator,
        respond=AsyncMock(),
    )
    service = AutonomousGroupService(
        chat=cast(Any, chat),
        admission_features=cast(Any, BlockingContext()),
        runtime_config=cast(Any, runtime_service),
        turn_coordinator=coordinator,
    )
    message = _group_message("1", "第一条")
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
    state.latest_token = await coordinator.notify_message(
        "group:2001",
        observation=True,
    )
    service._states["2001"] = state
    runtime = await runtime_service.snapshot(group_id="2001")

    task = asyncio.create_task(service._run_latest("2001", 1, cast(Any, runtime)))
    await started.wait()
    state.revision = 2
    state.changed.set()
    release.set()
    await task

    assert chat.respond.await_count == 0
    await service.close()
