"""Versioned, cooperative cancellation for conversation turns."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import InboundMessage

TurnStage = Literal["admission", "generation", "reply"]


class TurnInterruptedError(RuntimeError):
    """A newer message superseded an interruptible admission or generation stage."""


class TurnSupersededError(RuntimeError):
    """A turn token no longer represents the current conversation input."""


class ReplySequenceCancelled(RuntimeError):
    """Unsent reply chunks were cancelled after the conversation advanced."""


@dataclass(frozen=True, slots=True)
class TurnToken:
    conversation_key: str
    version: int
    origin: TurnOrigin
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class _TurnState:
    version: int = 0
    origin: TurnOrigin = TurnOrigin.USER_MESSAGE
    mutation_started: bool = False
    protected_version: int | None = None
    tasks: dict[TurnStage, asyncio.Task[object]] = field(default_factory=dict)


class ConversationTurnCoordinator:
    """Track top-level turn stages without holding a lock across a whole reply."""

    def __init__(
        self,
        *,
        cancel_replies_on_new_message: bool = True,
        interrupt_autonomous_on_new_message: bool = True,
    ) -> None:
        self._cancel_replies = cancel_replies_on_new_message
        self._interrupt_autonomous = interrupt_autonomous_on_new_message
        self._states: dict[str, _TurnState] = {}
        self._guard = asyncio.Lock()

    def configure_policy(
        self,
        *,
        cancel_replies_on_new_message: bool,
        interrupt_autonomous_on_new_message: bool,
    ) -> None:
        """Apply HOT cancellation policy before admitting a new real message."""

        self._cancel_replies = cancel_replies_on_new_message
        self._interrupt_autonomous = interrupt_autonomous_on_new_message

    @staticmethod
    def key_for(message: InboundMessage) -> str:
        """Use one cancellation domain per private peer or whole QQ group."""

        if message.group_id is not None:
            return f"group:{message.group_id}"
        return f"private:{message.sender.user_id}"

    async def notify_message(
        self,
        conversation_key: str,
        origin: TurnOrigin = TurnOrigin.USER_MESSAGE,
        *,
        observation: bool = False,
        protect_from_observations: bool = False,
    ) -> TurnToken:
        """Advance input version while keeping direct group turns above observations."""

        to_cancel: set[asyncio.Task[object]] = set()
        async with self._guard:
            state = self._states.setdefault(conversation_key, _TurnState())
            if observation and state.protected_version == state.version:
                return TurnToken(conversation_key, state.version, state.origin)
            previous_origin = state.origin
            if self._cancel_replies:
                reply = state.tasks.get("reply")
                if reply is not None and not reply.done():
                    to_cancel.add(reply)
            if (
                self._interrupt_autonomous
                and previous_origin in {TurnOrigin.AUTONOMOUS_GROUP, TurnOrigin.PLUGIN_BACKGROUND}
                and not state.mutation_started
            ):
                for stage in ("admission", "generation"):
                    task = state.tasks.get(stage)
                    if task is not None and not task.done():
                        to_cancel.add(task)
            state.version += 1
            state.origin = origin
            state.mutation_started = False
            state.protected_version = state.version if protect_from_observations else None
            token = TurnToken(conversation_key, state.version, origin)
        current = asyncio.current_task()
        for task in to_cancel:
            if task is not current:
                task.cancel()
        return token

    @asynccontextmanager
    async def track(self, token: TurnToken, stage: TurnStage) -> AsyncIterator[None]:
        """Register the current coroutine as one observable turn stage."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("turn stage requires an asyncio task")
        typed_task = task  # asyncio tasks are invariant; storage never inspects results.
        async with self._guard:
            state = self._states.get(token.conversation_key)
            if state is None or state.version != token.version:
                raise TurnSupersededError("turn was superseded before stage registration")
            state.tasks[stage] = typed_task
        try:
            yield
        except asyncio.CancelledError as exc:
            if stage == "admission":
                raise TurnInterruptedError("turn interrupted by a newer message") from exc
            if stage == "reply":
                raise ReplySequenceCancelled("reply sequence cancelled") from exc
            raise TurnSupersededError("generation superseded by a newer message") from exc
        finally:
            async with self._guard:
                state = self._states.get(token.conversation_key)
                if state is not None and state.tasks.get(stage) is task:
                    state.tasks.pop(stage, None)

    async def mark_mutation_started(self, token: TurnToken) -> None:
        """Protect an already-started side effect from automatic cancellation."""

        async with self._guard:
            state = self._states.get(token.conversation_key)
            if state is not None and state.version == token.version:
                state.mutation_started = True

    async def begin_autonomous(self, token: TurnToken) -> TurnToken | None:
        """Promote an idle observed-message token to an interruptible autonomous turn.

        The promotion keeps the same input version: it does not invent a message.
        It only changes the trusted origin after the debounce window, so the next
        real group message can cancel admission or generation work immediately.  A
        still-running explicit turn always wins and blocks autonomous work.
        """

        async with self._guard:
            state = self._states.get(token.conversation_key)
            if state is None or state.version != token.version:
                return None
            if any(not task.done() for task in state.tasks.values()):
                return None
            state.origin = TurnOrigin.AUTONOMOUS_GROUP
            state.mutation_started = False
            state.protected_version = None
            return TurnToken(
                token.conversation_key,
                token.version,
                TurnOrigin.AUTONOMOUS_GROUP,
                token.created_at,
            )

    async def begin_background(self, conversation_key: str) -> TurnToken | None:
        """Admit plugin background work only while the conversation is idle.

        Unlike :meth:`notify_message`, this never cancels user work.  Once admitted,
        the next real message advances the version and cooperatively interrupts the
        background admission or generation stage.
        """

        async with self._guard:
            state = self._states.setdefault(conversation_key, _TurnState())
            if any(not task.done() for task in state.tasks.values()):
                return None
            state.version += 1
            state.origin = TurnOrigin.PLUGIN_BACKGROUND
            state.mutation_started = False
            state.protected_version = None
            return TurnToken(
                conversation_key,
                state.version,
                TurnOrigin.PLUGIN_BACKGROUND,
            )

    async def cancel_interruptible(self, conversation_key: str) -> bool:
        """Explicitly cancel registered admission/generation/reply work for `/ai stop`."""

        async with self._guard:
            state = self._states.get(conversation_key)
            if state is None:
                return False
            tasks = tuple(task for task in state.tasks.values() if not task.done())
            state.version += 1
        current = asyncio.current_task()
        cancelled = False
        for task in tasks:
            if task is not current:
                task.cancel()
                cancelled = True
        return cancelled

    def is_current(self, token: TurnToken) -> bool:
        state = self._states.get(token.conversation_key)
        return state is not None and state.version == token.version
