"""Debounced group observation scored locally, then one Main Agent turn."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import ConversationRuntimeConfig, RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.conversation.features import AdmissionFeatureBuilder
from qq_ai_bot.conversation.participation import (
    AdmissionSignalHint,
    LocalAutonomousParticipationPolicy,
)
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.plugin_host.admission_adapter import PluginAdmissionSignalAdapter
from qq_ai_bot.runtime.observability import (
    RuntimeTurnCorrelation,
    TurnObservationRecorder,
    bind_runtime_turn,
    build_turn_observation,
    new_runtime_turn_id,
    record_observation_safely,
)
from qq_ai_bot.services.chat import ChatService, OutboundSender
from qq_ai_bot.services.plugin_events import content_free_turn_payload, publish_notification
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    TurnInterruptedError,
    TurnSupersededError,
    TurnToken,
)
from yuki_plugin_sdk.events import EventName

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
    """Only debounce batches; local scoring owns participation, not Planner."""

    def __init__(
        self,
        *,
        chat: ChatService,
        admission_features: AdmissionFeatureBuilder,
        runtime_config: RuntimeConfigService | None = None,
        turn_coordinator: ConversationTurnCoordinator | None = None,
        admission_signals: PluginAdmissionSignalAdapter | None = None,
        turn_observations: TurnObservationRecorder | None = None,
    ) -> None:
        self._chat = chat
        self._runtime_config = runtime_config or chat._runtime_config
        self._admission_features = admission_features
        self._coordinator = turn_coordinator or chat._turn_coordinator
        self._admission_signals = admission_signals
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
        scope_key = message.scope().key
        state = self._states.setdefault(scope_key, _GroupState())
        state.messages.append(message)
        state.profiles.append(profile)
        state.senders.append(sender)
        state.latest_token = turn_token
        state.revision += 1
        state.changed.set()
        self._ensure_task(scope_key, state)

    def _ensure_task(self, scope_key: str, state: _GroupState) -> None:
        """Keep exactly one coalescing worker alive for one group."""

        if self._closed:
            return
        if state.task is not None and not state.task.done():
            return
        task = asyncio.create_task(
            self._after_silence(scope_key),
            name="conversation-scope-autonomous",
        )
        state.task = task

        def task_done(completed: asyncio.Task[None]) -> None:
            self._task_done(scope_key, completed)

        task.add_done_callback(task_done)

    def _task_done(self, scope_key: str, completed: asyncio.Task[None]) -> None:
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
        state = self._states.get(scope_key)
        if state is None or state.task is not completed:
            return
        state.task = None
        # A message can arrive between the worker's final revision check and this
        # callback. Its event is the hand-off that prevents that update being lost.
        if state.changed.is_set():
            self._ensure_task(scope_key, state)

    async def _after_silence(self, scope_key: str) -> None:
        while True:
            revision = -1
            try:
                state = self._states.get(scope_key)
                if state is None or not state.messages:
                    return
                group_id = state.messages[-1].group_id
                if group_id is None:
                    return
                runtime = await self._runtime_config.snapshot(group_id=group_id)
                policy = _conversation_policy(runtime)
                if not policy.autonomous_enabled:
                    return
                # One worker absorbs every update until the group has remained
                # quiet for a full debounce interval.
                state.changed.clear()
                revision = state.revision
                try:
                    await asyncio.wait_for(
                        state.changed.wait(),
                        timeout=policy.autonomous_debounce_seconds,
                    )
                except TimeoutError:
                    pass
                if not self._is_latest(scope_key, revision):
                    continue
                await self._run_latest(scope_key, revision, runtime)
            except asyncio.CancelledError:
                raise
            except (
                TurnInterruptedError,
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
            if revision >= 0 and not self._is_latest(scope_key, revision):
                continue
            return

    def _is_latest(self, scope_key: str, revision: int) -> bool:
        state = self._states.get(scope_key)
        return state is not None and state.revision == revision

    async def _run_latest(
        self,
        scope_key: str,
        revision: int,
        runtime: RuntimeConfigSnapshot,
    ) -> None:
        """Bind one fresh runtime turn correlation per autonomous attempt.

        Delivery counts stay joinable through confirmed-delivery observations
        on the same ``runtime_turn_id``; the observation row itself only
        carries latency and outcome category.
        """

        started = time.perf_counter()
        correlation = RuntimeTurnCorrelation(
            turn_id=new_runtime_turn_id(),
            origin=TurnOrigin.AUTONOMOUS_GROUP,
        )
        error_category: str | None = None
        observation_key = scope_key
        with bind_runtime_turn(correlation):
            try:
                await self._admit_latest(scope_key, revision, runtime)
            except BaseException as exc:
                error_category = type(exc).__name__
                raise
            finally:
                if correlation.touched or error_category is not None:
                    observation = build_turn_observation(
                        correlation,
                        scope_type="group",
                        conversation_key=observation_key,
                        admission_outcome="autonomous_group",
                        handled=error_category is None,
                        sent_messages=0,
                        error_category=error_category,
                        total_latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                    await record_observation_safely(self._turn_observations, observation)

    async def _admit_latest(
        self,
        scope_key: str,
        revision: int,
        runtime: RuntimeConfigSnapshot,
    ) -> None:
        state = self._states.get(scope_key)
        if state is None or not state.messages:
            return
        last = state.messages[-1]
        profile = state.profiles[-1]
        sender = state.senders[-1]
        token = state.latest_token
        if token is None:
            token = await self._coordinator.notify_message(
                last.scope().key,
                TurnOrigin.AUTONOMOUS_GROUP,
            )
        else:
            token = await self._coordinator.begin_autonomous(token)
            if token is None:
                return
        plugin_signals = (
            await self._admission_signals.collect(
                message=last,
                origin=TurnOrigin.AUTONOMOUS_GROUP,
                runtime=runtime,
            )
            if self._admission_signals is not None
            else ()
        )
        policy = _conversation_policy(runtime)
        features = await self._admission_features.admission_features(
            inbound=last,
            content=last.text,
            runtime=runtime,
            plugin_signals=cast(tuple[AdmissionSignalHint, ...], plugin_signals),
        )
        snapshot = LocalAutonomousParticipationPolicy(
            threshold=policy.autonomous_admission_threshold,
        ).evaluate(features)
        conversation_key = last.scope().key
        publisher = getattr(self._chat, "_event_publisher", None)
        if not snapshot.should_participate:
            await publish_notification(
                publisher,
                EventName.AUTONOMOUS_DECLINED,
                content_free_turn_payload(
                    origin=TurnOrigin.AUTONOMOUS_GROUP.value,
                    scope_type="group",
                    conversation_key=conversation_key,
                    score=snapshot.score,
                    threshold=snapshot.threshold,
                    reasons=list(snapshot.reasons),
                ),
            )
            return
        if not self._is_latest(scope_key, revision) or not self._coordinator.is_current(token):
            return
        if last.group_id is None:
            return
        identity = ConversationScope.group(last.bot_user_id, last.group_id)
        scope_state = await self._chat._conversation_scopes.get(identity)
        trigger_event = await self._chat._ledger.find_by_platform_message(
            bot_user_id=identity.bot_user_id,
            platform_message_id=last.message_id,
        )
        if scope_state is None or trigger_event is None:
            return
        turn_snapshot = ConversationTurnSnapshot(
            scope_id=scope_state.id,
            scope_key=identity.key,
            generation=scope_state.generation,
            trigger_event_id=trigger_event.id,
            coordinator_version=token.version,
        )
        await publish_notification(
            publisher,
            EventName.TURN_ADMITTED,
            content_free_turn_payload(
                origin=TurnOrigin.AUTONOMOUS_GROUP.value,
                scope_type="group",
                conversation_key=conversation_key,
                reason="autonomous_group",
            ),
        )
        started = time.perf_counter()
        outcome = "autonomous_group"
        try:
            await self._chat.respond(
                last,
                identity,
                profile,
                last.text,
                sender,
                autonomous=True,
                runtime_snapshot=runtime,
                turn_token=token,
                turn_snapshot=turn_snapshot,
            )
        except (TurnInterruptedError, TurnSupersededError):
            outcome = "turn_interrupted"
            raise
        finally:
            await publish_notification(
                publisher,
                EventName.TURN_CLOSED,
                content_free_turn_payload(
                    origin=TurnOrigin.AUTONOMOUS_GROUP.value,
                    scope_type="group",
                    conversation_key=conversation_key,
                    outcome=outcome,
                    handled=True,
                    sent_messages=0,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                ),
            )

    async def wait_until_idle(self, scope_key: str) -> None:
        while True:
            state = self._states.get(scope_key)
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


def _conversation_policy(runtime: object) -> ConversationRuntimeConfig:
    getter = getattr(runtime, "conversation_policy", None)
    if callable(getter):
        policy = getter()
        if isinstance(policy, ConversationRuntimeConfig):
            return policy
    conversation = getattr(runtime, "conversation", None)
    if isinstance(conversation, ConversationRuntimeConfig):
        return conversation
    raise TypeError("runtime snapshot is missing conversation policy")
