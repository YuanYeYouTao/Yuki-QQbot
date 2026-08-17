"""Debounced group observation delegated to the same Planner-first chat path."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ConversationIdentity, ConversationMode
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.planner.context import PlannerContextBuilder
from qq_ai_bot.planner.models import PlannerDecision
from qq_ai_bot.planner.provider import PlannerInterruptedError as ProviderPlannerInterruptedError
from qq_ai_bot.planner.service import PlannerService
from qq_ai_bot.plugin_host.planner_adapter import PluginPlannerSignalAdapter
from qq_ai_bot.runtime.observability import (
    RuntimeTurnCorrelation,
    TurnObservationRecorder,
    bind_runtime_turn,
    build_turn_observation,
    new_runtime_turn_id,
    record_observation_safely,
)
from qq_ai_bot.services.chat import ChatService, OutboundSender
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    PlannerInterruptedError,
    TurnSupersededError,
    TurnToken,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _GroupState:
    messages: deque[InboundMessage] = field(default_factory=lambda: deque(maxlen=100))
    profiles: deque[UserProfileSnapshot] = field(default_factory=lambda: deque(maxlen=100))
    senders: deque[OutboundSender] = field(default_factory=lambda: deque(maxlen=100))
    latest_token: TurnToken | None = None
    revision: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class AutonomousGroupService:
    """Only debounce batches; Planner owns all participation decisions."""

    def __init__(
        self,
        *,
        chat: ChatService,
        planner_context: PlannerContextBuilder,
        planner: PlannerService,
        runtime_config: RuntimeConfigService | None = None,
        turn_coordinator: ConversationTurnCoordinator | None = None,
        planner_signals: PluginPlannerSignalAdapter | None = None,
        turn_observations: TurnObservationRecorder | None = None,
    ) -> None:
        self._chat = chat
        self._runtime_config = runtime_config or chat._runtime_config
        self._planner_context = planner_context
        self._planner = planner
        self._coordinator = turn_coordinator or chat._turn_coordinator
        self._planner_signals = planner_signals
        self._turn_observations = turn_observations
        self._states: dict[str, _GroupState] = {}
        self._task_failures = 0
        self._closed = False

    @property
    def task_failures(self) -> int:
        """Return the process-local count of observed background task failures."""

        return self._task_failures

    def observe(
        self,
        message: InboundMessage,
        profile: UserProfileSnapshot,
        sender: OutboundSender,
        turn_token: TurnToken | None = None,
    ) -> None:
        group_id = message.group_id
        if group_id is None:
            return
        state = self._states.setdefault(group_id, _GroupState())
        state.messages.append(message)
        state.profiles.append(profile)
        state.senders.append(sender)
        state.latest_token = turn_token
        state.revision += 1
        state.changed.set()
        self._ensure_task(group_id, state)

    def _ensure_task(self, group_id: str, state: _GroupState) -> None:
        """Keep exactly one coalescing worker alive for one group."""

        if self._closed:
            return
        if state.task is not None and not state.task.done():
            return
        task = asyncio.create_task(
            self._after_silence(group_id),
            name=f"planner-group-{group_id}",
        )
        state.task = task

        def task_done(completed: asyncio.Task[None]) -> None:
            self._task_done(group_id, completed)

        task.add_done_callback(task_done)

    def _task_done(self, group_id: str, completed: asyncio.Task[None]) -> None:
        """Own a detached task, consume its outcome, and release its state reference."""

        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._task_failures += 1
            logger.exception(
                "autonomous_group_task_failed exception_category=%s",
                type(exc).__name__,
            )
        state = self._states.get(group_id)
        if state is None or state.task is not completed:
            return
        state.task = None
        # A message can arrive between the worker's final revision check and this
        # callback. Its event is the hand-off that prevents that update being lost.
        if state.changed.is_set():
            self._ensure_task(group_id, state)

    async def _after_silence(self, group_id: str) -> None:
        while True:
            revision = -1
            try:
                runtime = await self._runtime_config.snapshot(group_id=group_id)
                if not runtime.planner.group_enabled:
                    return
                state = self._states.get(group_id)
                if state is None or not state.messages:
                    return
                # One worker absorbs every update until the group has remained
                # quiet for a full debounce interval. No cancelled Planner calls.
                state.changed.clear()
                revision = state.revision
                try:
                    await asyncio.wait_for(
                        state.changed.wait(),
                        timeout=runtime.planner.group_debounce_seconds,
                    )
                except TimeoutError:
                    pass
                if not self._is_latest(group_id, revision):
                    continue
                await self._plan_latest(group_id, revision, runtime)
            except asyncio.CancelledError:
                raise
            except (
                PlannerInterruptedError,
                ProviderPlannerInterruptedError,
                TurnSupersededError,
            ):
                pass
            except SQLAlchemyError as exc:
                self._task_failures += 1
                logger.warning(
                    "autonomous_group_task_failed exception_category=%s",
                    type(exc).__name__,
                )
            except (LLMError, OSError, RuntimeError, ValueError, TypeError) as exc:
                self._task_failures += 1
                logger.warning(
                    "autonomous_group_task_failed exception_category=%s",
                    type(exc).__name__,
                )
            if revision >= 0 and not self._is_latest(group_id, revision):
                continue
            return

    def _is_latest(self, group_id: str, revision: int) -> bool:
        state = self._states.get(group_id)
        return state is not None and state.revision == revision

    async def _plan_latest(
        self,
        group_id: str,
        revision: int,
        runtime: RuntimeConfigSnapshot,
    ) -> None:
        """Bind one fresh runtime turn correlation per autonomous attempt.

        Delivery counts stay joinable through ``planner_runs.messages_sent``
        on the same ``runtime_turn_id``; the observation row itself only
        carries latency and outcome category.
        """

        started = time.perf_counter()
        correlation = RuntimeTurnCorrelation(
            turn_id=new_runtime_turn_id(),
            origin=TurnOrigin.AUTONOMOUS_GROUP,
        )
        error_category: str | None = None
        with bind_runtime_turn(correlation):
            try:
                await self._plan_latest_admitted(group_id, revision, runtime)
            except BaseException as exc:
                error_category = type(exc).__name__
                raise
            finally:
                if correlation.touched or error_category is not None:
                    observation = build_turn_observation(
                        correlation,
                        scope_type="group",
                        conversation_key=f"group:{group_id}",
                        admission_outcome="autonomous_group",
                        handled=error_category is None,
                        sent_messages=0,
                        error_category=error_category,
                        total_latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                    await record_observation_safely(self._turn_observations, observation)

    async def _plan_latest_admitted(
        self,
        group_id: str,
        revision: int,
        runtime: RuntimeConfigSnapshot,
    ) -> None:
        state = self._states.get(group_id)
        if state is None or not state.messages:
            return
        last = state.messages[-1]
        profile = state.profiles[-1]
        sender = state.senders[-1]
        token = state.latest_token
        if token is None:
            token = await self._coordinator.notify_message(
                f"group:{group_id}",
                TurnOrigin.AUTONOMOUS_GROUP,
            )
        else:
            token = await self._coordinator.begin_autonomous(token)
            if token is None:
                return
        plugin_signals = (
            await self._planner_signals.collect(
                message=last,
                origin=TurnOrigin.AUTONOMOUS_GROUP,
                runtime=runtime,
            )
            if self._planner_signals is not None
            else ()
        )
        planner_input = await self._planner_context.build(
            inbound=last,
            conversation_key=token.conversation_key,
            content=last.text,
            origin=TurnOrigin.AUTONOMOUS_GROUP,
            runtime=runtime,
            visual_input_present=False,
            available_tool_categories=("history", "memory", "web"),
            plugin_signals=plugin_signals,
        )
        if not self._is_latest(group_id, revision) or not self._coordinator.is_current(token):
            return
        async with self._coordinator.track(token, "planner"):
            outcome = await self._planner.plan(
                planner_input,
                runtime=runtime,
                turn_version=token.version,
            )
        if not self._is_latest(group_id, revision) or not self._coordinator.is_current(token):
            await self._planner.record_delivery(
                outcome.run_id,
                messages_sent=0,
                interrupted=True,
            )
            return
        plan = outcome.planned_turn.plan
        if plan.decision is PlannerDecision.WAIT:
            if plan.wait_seconds > 0:
                await asyncio.sleep(plan.wait_seconds)
            if not self._is_latest(group_id, revision) or not self._coordinator.is_current(token):
                await self._planner.record_delivery(
                    outcome.run_id,
                    messages_sent=0,
                    interrupted=True,
                )
                return
            await self._planner.record_delivery(
                outcome.run_id,
                messages_sent=0,
                interrupted=False,
            )
            # Re-plan exactly once after a bounded wait. A second wait becomes
            # silence, so one group message cannot create an endless loop.
            refreshed = await self._planner_context.build(
                inbound=last,
                conversation_key=token.conversation_key,
                content=last.text,
                origin=TurnOrigin.AUTONOMOUS_GROUP,
                runtime=runtime,
                visual_input_present=False,
                available_tool_categories=("history", "memory", "web"),
                plugin_signals=plugin_signals,
            )
            if not self._is_latest(group_id, revision):
                return
            async with self._coordinator.track(token, "planner"):
                outcome = await self._planner.plan(
                    refreshed,
                    runtime=runtime,
                    turn_version=token.version,
                )
            if not self._is_latest(group_id, revision) or not self._coordinator.is_current(token):
                await self._planner.record_delivery(
                    outcome.run_id,
                    messages_sent=0,
                    interrupted=True,
                )
                return
            plan = outcome.planned_turn.plan
            if plan.decision is PlannerDecision.WAIT:
                await self._planner.record_delivery(
                    outcome.run_id,
                    messages_sent=0,
                    interrupted=False,
                )
                return
        if plan.decision is not PlannerDecision.REPLY:
            return
        if not self._is_latest(group_id, revision) or not self._coordinator.is_current(token):
            return
        identity = ConversationIdentity.group(
            group_id,
            last.sender.user_id,
            ConversationMode.SHARED,
        )
        sent = await self._chat.respond(
            last,
            identity,
            profile,
            last.text,
            sender,
            autonomous=True,
            runtime_snapshot=runtime,
            planned_turn=outcome.planned_turn,
            turn_token=token,
        )
        await self._planner.record_delivery(
            outcome.run_id,
            messages_sent=sent,
            interrupted=not self._coordinator.is_current(token),
        )

    async def wait_until_idle(self, group_id: str) -> None:
        while True:
            state = self._states.get(group_id)
            if state is None or state.task is None:
                return
            task = state.task
            await asyncio.shield(task)
            # Let the ownership callback restart a task if an update raced with
            # the worker's last revision check.
            await asyncio.sleep(0)

    async def close(self) -> None:
        self._closed = True
        tasks = [
            state.task
            for state in self._states.values()
            if state.task is not None and not state.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
