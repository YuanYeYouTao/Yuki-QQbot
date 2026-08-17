"""Persistent main-conversation turns triggered by plugin external events."""

from __future__ import annotations

import asyncio
import logging
import time

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.plugin_host.notification_repository import (
    BackgroundTurnJobRecord,
    PluginNotificationRepository,
)
from qq_ai_bot.runtime.observability import (
    RuntimeTurnCorrelation,
    TurnObservationRecorder,
    bind_runtime_turn,
    build_turn_observation,
    new_runtime_turn_id,
    record_observation_safely,
)
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    PlannerInterruptedError,
    TurnSupersededError,
)

logger = logging.getLogger(__name__)


class PluginBackgroundTurnWorker:
    """Generate tool-free Yuki replies in the target's normal conversation."""

    def __init__(
        self,
        *,
        repository: PluginNotificationRepository,
        ledger: EventLedgerRepository,
        runtime_config: RuntimeConfigService,
        chat: ChatService,
        turns: ConversationTurnCoordinator,
        turn_observations: TurnObservationRecorder | None = None,
        planner_context: object | None = None,
        planner: object | None = None,
    ) -> None:
        del planner_context, planner
        self._repository = repository
        self._ledger = ledger
        self._runtime_config = runtime_config
        self._chat = chat
        self._turns = turns
        self._turn_observations = turn_observations
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self._run(),
                name="plugin-background-turns",
            )

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            job = await self._repository.claim_turn()
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
            await self._execute(job)

    async def _execute(self, job: BackgroundTurnJobRecord) -> None:
        """Bind one fresh runtime turn correlation per background job attempt."""

        started = time.perf_counter()
        correlation = RuntimeTurnCorrelation(
            turn_id=new_runtime_turn_id(),
            origin=TurnOrigin.PLUGIN_BACKGROUND,
        )
        error_category: str | None = None
        conversation_key = (
            f"group:{job.target_id}" if job.target_type == "group" else f"private:{job.target_id}"
        )
        with bind_runtime_turn(correlation):
            try:
                await self._execute_admitted(job)
            except BaseException as exc:
                error_category = type(exc).__name__
                raise
            finally:
                if correlation.touched or error_category is not None:
                    observation = build_turn_observation(
                        correlation,
                        scope_type=job.target_type,
                        conversation_key=conversation_key,
                        admission_outcome="plugin_background",
                        handled=error_category is None,
                        sent_messages=0,
                        error_category=error_category,
                        total_latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                    await record_observation_safely(self._turn_observations, observation)

    async def _execute_admitted(self, job: BackgroundTurnJobRecord) -> None:
        creator = await self._repository.grant_creator(
            plugin_id=job.plugin_id,
            target_type=job.target_type,
            target_id=job.target_id,
        )
        if creator is None:
            await self._repository.fail_turn(
                job.id,
                error_category="target_grant_or_plugin_unavailable",
            )
            return
        event = await self._ledger.get_event(job.source_event_id)
        if (
            event is None
            or event.event_kind != "external_event"
            or event.source_plugin_id != job.plugin_id
        ):
            await self._repository.fail_turn(job.id, error_category="source_event_invalid")
            return

        conversation_key = (
            f"group:{job.target_id}" if job.target_type == "group" else f"private:{job.target_id}"
        )
        token = await self._turns.begin_background(conversation_key)
        if token is None:
            await self._repository.defer_turn(
                job.id,
                error_category="conversation_busy",
                delay_seconds=3,
                preserve_attempt=True,
            )
            return

        context_user_id = job.target_id if job.target_type == "private" else creator
        runtime = await self._runtime_config.snapshot(
            user_id=context_user_id,
            group_id=event.group_id,
        )
        self._chat.configure_runtime_controls(runtime)
        self._turns.configure_policy(
            cancel_replies_on_new_message=runtime.reply.cancel_on_new_message,
            interrupt_autonomous_on_new_message=(
                runtime.conversation_policy().interrupt_autonomous_on_new_message
            ),
        )
        try:
            async with self._turns.track(token, "generation"):
                result = await self._chat.generate_external_reply(
                    event=event,
                    authorization_user_id=context_user_id,
                    conversation_key=conversation_key,
                    runtime=runtime,
                    agent_intent=job.agent_intent,
                    turn_token=token,
                )
            await self._repository.finish_turn(
                job.id,
                text=result.text,
                tool_calls_used=result.tool_calls_used,
                model_requests=result.model_requests,
            )
            logger.info(
                "plugin_background_turn_completed plugin_id=%s event_id=%d "
                "reply=%s model_requests=%d",
                job.plugin_id,
                event.id,
                bool(result.text),
                result.model_requests,
            )
        except (
            PlannerInterruptedError,
            TurnSupersededError,
        ):
            if job.attempts >= 2:
                await self._repository.abandon_turn(
                    job.id,
                    error_category="interrupted_twice",
                )
            else:
                await self._repository.defer_turn(
                    job.id,
                    error_category="interrupted_by_user",
                    delay_seconds=5,
                )
        except asyncio.CancelledError:
            await self._repository.defer_turn(
                job.id,
                error_category="worker_stopped",
                delay_seconds=5,
                preserve_attempt=True,
            )
            raise
        except Exception as exc:
            logger.exception(
                "plugin_background_turn_failed plugin_id=%s event_id=%d error_category=%s",
                job.plugin_id,
                job.source_event_id,
                type(exc).__name__,
            )
            await self._repository.fail_turn(
                job.id,
                error_category=type(exc).__name__,
            )
