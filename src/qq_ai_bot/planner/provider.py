"""Planner provider contract and constrained LLM-backed implementation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import Any, Protocol

from pydantic import ValidationError

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.emoji.models import EmojiIntent, EmojiPlacement, EmojiReplyMode, EmojiReplyPlan
from qq_ai_bot.llm.base import LLMError, LLMTimeoutError
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.structured import StructuredTaskError, StructuredTaskRunner
from qq_ai_bot.planner.models import (
    DeliveryMode,
    PlannerDecision,
    PlannerInput,
    PlannerModelOutput,
    PlannerReasonCode,
    TurnPlan,
)
from qq_ai_bot.planner.observability import PlannerObservability
from qq_ai_bot.planner.prompt import planner_payload, planner_system_prompt
from qq_ai_bot.services.prompt_registry import PromptRegistry, PromptTarget

logger = logging.getLogger(__name__)


class PlannerProviderError(RuntimeError):
    """Sanitized base exception for Planner-only failures."""


class PlannerInterruptedError(PlannerProviderError):
    """The current Planner request was superseded by a newer real message."""


class PlannerTimeoutError(PlannerProviderError):
    """The planning-only request exceeded its independent timeout."""


class PlannerResponseError(PlannerProviderError):
    """The model did not return one valid TurnPlan object."""


class PlannerProvider(Protocol):
    """Provider-neutral Planner interface used by later orchestration."""

    async def plan(
        self,
        planner_input: PlannerInput,
        *,
        runtime: RuntimeConfigSnapshot,
        cancellation: asyncio.Event | None = None,
    ) -> TurnPlan: ...


def constrain_turn_plan(
    payload: dict[str, Any],
    planner_input: PlannerInput,
    *,
    hard_max_messages: int = 10,
    max_wait_seconds: float = 60,
) -> TurnPlan:
    """Validate task-specific constraints without silently rewriting model output."""

    if not 1 <= hard_max_messages <= 20:
        raise ValueError("hard_max_messages must be between 1 and 20")
    if not 0 <= max_wait_seconds <= 300:
        raise ValueError("max_wait_seconds must be between 0 and 300")
    try:
        plan = TurnPlan.model_validate(payload)
    except ValidationError as exc:
        raise PlannerResponseError("planner returned an invalid TurnPlan") from exc
    return validate_turn_plan(
        plan,
        planner_input,
        hard_max_messages=hard_max_messages,
        max_wait_seconds=max_wait_seconds,
    )


def validate_turn_plan(
    plan: TurnPlan,
    planner_input: PlannerInput,
    *,
    hard_max_messages: int,
    max_wait_seconds: float,
) -> TurnPlan:
    """Narrow event-bound fields without discarding otherwise valid intent."""

    known_targets = set(planner_input.known_target_user_ids)
    updates: dict[str, object] = {
        "target_user_ids": tuple(
            dict.fromkeys(target for target in plan.target_user_ids if target in known_targets)
        ),
        "reply_to_event_id": normalize_reply_target(
            plan.reply_to_event_id,
            planner_input,
        ),
        "desired_messages": min(plan.desired_messages, hard_max_messages),
        "wait_seconds": (
            min(plan.wait_seconds, max_wait_seconds)
            if plan.decision is PlannerDecision.WAIT
            else 0.0
        ),
    }
    return plan.model_copy(update=updates)


def normalize_reply_target(
    requested_event_id: int | None,
    planner_input: PlannerInput,
) -> int | None:
    """Keep only an intentional, useful local event quote target.

    Replying to the current private message adds a redundant quote bubble to
    every turn, so it is always rendered as a normal message.  The Planner may
    still quote an older private message or any explicitly selected message in
    a group conversation where disambiguation is useful.
    """

    if requested_event_id is None or requested_event_id not in planner_input.known_event_ids:
        return None
    if (
        planner_input.scope_type is ScopeType.PRIVATE
        and requested_event_id == planner_input.trigger_event_id
    ):
        return None
    return requested_event_id


def deterministic_effect_plan(planner_input: PlannerInput) -> TurnPlan:
    """Build the trusted standalone emoji effect without a model request."""

    return TurnPlan(
        decision=PlannerDecision.REPLY,
        intent="发送当前用户明确索要的聊天表情",
        target_user_ids=(planner_input.current_sender_user_id,),
        delivery_mode=DeliveryMode.SINGLE,
        desired_messages=1,
        confidence=1.0,
        reason_code=PlannerReasonCode.DETERMINISTIC_EFFECT_REQUEST,
        emoji=EmojiReplyPlan(
            intent=EmojiIntent.EXPLICIT_REQUEST,
            mode=EmojiReplyMode.EMOJI_ONLY,
            placement=EmojiPlacement.ONLY,
            goal=planner_input.emoji.goal or "自然回应当前用户",
        ),
    )


def deterministic_fallback_plan(
    planner_input: PlannerInput,
    *,
    reason_code: PlannerReasonCode = PlannerReasonCode.PLANNER_PROVIDER_ERROR_FALLBACK,
) -> TurnPlan:
    """Return a deterministic fallback without consulting a model.

    Autonomous turns only reach the provider after the necessity gate admits
    them. Replying here keeps a temporary planner-format failure from turning
    an otherwise relevant group turn into permanent silence.
    """

    explicitly_triggered = (
        planner_input.scope_type is ScopeType.PRIVATE
        or planner_input.mentions_bot
        or planner_input.reply_target_is_bot
    )
    should_reply = explicitly_triggered or planner_input.necessity.should_enter_planner
    if (
        planner_input.emoji.available
        and planner_input.emoji.explicit_request
        and planner_input.emoji.standalone_request
    ):
        return deterministic_effect_plan(planner_input).model_copy(
            update={"reason_code": reason_code, "confidence": 0.0}
        )
    return TurnPlan(
        decision=PlannerDecision.REPLY if should_reply else PlannerDecision.SILENT,
        intent=(
            "回应当前真实发送者的消息"
            if should_reply
            else "Planner 失败且本轮未通过发言门槛，保持沉默"
        ),
        target_user_ids=(planner_input.current_sender_user_id,) if should_reply else (),
        delivery_mode=DeliveryMode.CONCISE,
        desired_messages=1,
        wait_seconds=0.0,
        confidence=0.0,
        reason_code=reason_code,
        planner_note="deterministic fallback after planner failure",
    )


class LLMPlannerProvider:
    """Request one tool-free TurnPlan from the current main LLM provider."""

    def __init__(
        self,
        provider: ModelCompleter | None = None,
        *,
        model_executor: ModelExecutor | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
        hard_max_messages: int | None = None,
        max_wait_seconds: float | None = None,
        fallback_on_error: bool = True,
        observability: PlannerObservability | None = None,
        prompt_registry: PromptRegistry | None = None,
        bot_display_name: str = "Yuki",
    ) -> None:
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if hard_max_messages is not None and not 1 <= hard_max_messages <= 20:
            raise ValueError("hard_max_messages must be between 1 and 20")
        if max_wait_seconds is not None and not 0 <= max_wait_seconds <= 300:
            raise ValueError("max_wait_seconds must be between 0 and 300")
        self._models = require_model_executor(
            model_executor,
            provider=provider,
            model=model or "fake",
        )
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._hard_max_messages = hard_max_messages
        self._max_wait_seconds = max_wait_seconds
        self._fallback_on_error = fallback_on_error
        self._observability = observability
        self._prompt_registry = prompt_registry
        self._system_prompt = planner_system_prompt(bot_display_name)
        self._structured = StructuredTaskRunner(self._models)

    @property
    def model_name(self) -> str:
        """Return the model selected by the explicit Planner task route."""

        return self._models.model_name(ModelTask.PLANNER)

    async def plan(
        self,
        planner_input: PlannerInput,
        *,
        runtime: RuntimeConfigSnapshot,
        cancellation: asyncio.Event | None = None,
    ) -> TurnPlan:
        """Plan once; invalid output is never retried and may safely fall back."""

        started = time.perf_counter()
        token = (
            self._observability.request_started(
                conversation_key=planner_input.conversation_key,
                sender_user_id=planner_input.current_sender_user_id,
                group_id=planner_input.current_group_id,
            )
            if self._observability is not None
            else None
        )
        try:
            _raise_if_cancelled(cancellation)
            planner_runtime = runtime.planner
            timeout_seconds = planner_runtime.timeout_seconds or self._timeout_seconds or 20.0
            hard_max_messages = (
                runtime.reply.plan_hard_max_messages or self._hard_max_messages or 10
            )
            max_wait_seconds = (
                planner_runtime.max_wait_seconds
                if planner_runtime.max_wait_seconds is not None
                else (self._max_wait_seconds or 60.0)
            )
            structured_input: dict[str, object] = {
                "delivery_preferences": {
                    "preferred_messages": planner_runtime.preferred_messages,
                    "maximum_messages": hard_max_messages,
                },
                **planner_payload(planner_input),
            }
            if self._prompt_registry is not None:
                plugin_messages = self._prompt_registry.render(target=PromptTarget.PLANNER)
                if plugin_messages:
                    structured_input["plugin_context"] = list(plugin_messages)
            model_output = await _await_with_cancellation(
                self._structured.run(
                    task=ModelTask.PLANNER,
                    instruction=self._system_prompt,
                    structured_input=structured_input,
                    output_model=PlannerModelOutput,
                    temperature=planner_runtime.temperature,
                    max_output_tokens=planner_runtime.max_output_tokens,
                    allow_text_json=True,
                    compact_schema=True,
                ),
                cancellation=cancellation,
                timeout_seconds=timeout_seconds,
            )
            _raise_if_cancelled(cancellation)
            plan = model_output.materialize()
            plan = validate_turn_plan(
                plan,
                planner_input,
                hard_max_messages=hard_max_messages,
                max_wait_seconds=max_wait_seconds,
            )
        except PlannerInterruptedError:
            if self._observability is not None and token is not None:
                self._observability.request_interrupted(
                    token,
                    latency_seconds=time.perf_counter() - started,
                )
            raise
        except asyncio.CancelledError:
            if self._observability is not None and token is not None:
                self._observability.request_interrupted(
                    token,
                    latency_seconds=time.perf_counter() - started,
                )
            raise
        except (
            PlannerProviderError,
            StructuredTaskError,
            LLMError,
            TimeoutError,
            OSError,
            ValueError,
        ) as exc:
            if not self._fallback_on_error:
                if self._observability is not None and token is not None:
                    self._observability.request_failed(
                        token,
                        latency_seconds=time.perf_counter() - started,
                        error_category=type(exc).__name__,
                    )
                raise
            logger.warning(
                "planner_fallback error_category=%s reason=%s",
                type(exc).__name__,
                str(exc),
            )
            fallback_reason = _fallback_reason(exc)
            plan = deterministic_fallback_plan(planner_input, reason_code=fallback_reason)
            if self._observability is not None and token is not None:
                self._observability.request_finished(
                    token,
                    plan=plan,
                    latency_seconds=time.perf_counter() - started,
                    fallback=True,
                )
            return plan
        if self._observability is not None and token is not None:
            self._observability.request_finished(
                token,
                plan=plan,
                latency_seconds=time.perf_counter() - started,
            )
        return plan


def _fallback_reason(exc: Exception) -> PlannerReasonCode:
    if isinstance(exc, (PlannerTimeoutError, LLMTimeoutError, TimeoutError)):
        return PlannerReasonCode.PLANNER_TIMEOUT_FALLBACK
    if isinstance(exc, (PlannerResponseError, StructuredTaskError, ValueError)):
        return PlannerReasonCode.PLANNER_INVALID_RESPONSE_FALLBACK
    return PlannerReasonCode.PLANNER_PROVIDER_ERROR_FALLBACK


def _raise_if_cancelled(cancellation: asyncio.Event | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise PlannerInterruptedError("planner request superseded")


async def _await_with_cancellation[T](
    awaitable: Awaitable[T],
    *,
    cancellation: asyncio.Event | None,
    timeout_seconds: float,
) -> T:
    operation = asyncio.ensure_future(awaitable)
    cancellation_waiter: asyncio.Task[bool] | None = None
    try:
        waiters: set[asyncio.Future[Any]] = {operation}
        if cancellation is not None:
            if cancellation.is_set():
                raise PlannerInterruptedError("planner request superseded")
            cancellation_waiter = asyncio.create_task(cancellation.wait())
            waiters.add(cancellation_waiter)
        done, _ = await asyncio.wait(
            waiters,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_waiter is not None and cancellation_waiter in done:
            raise PlannerInterruptedError("planner request superseded")
        if operation not in done:
            raise PlannerTimeoutError("planner request timed out")
        return await operation
    finally:
        if cancellation_waiter is not None and not cancellation_waiter.done():
            cancellation_waiter.cancel()
        if not operation.done():
            operation.cancel()
        pending = tuple(
            task
            for task in (operation, cancellation_waiter)
            if task is not None and not task.done()
        )
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
