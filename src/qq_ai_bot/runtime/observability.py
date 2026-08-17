"""Ambient turn correlation and the content-free observation contract.

R1 introduces an opaque ``runtime_turn_id`` propagated to the persistence
write points of planner runs, model invocations, tool invocations and memory
recall receipts.  Instead of changing every executor/repository signature,
the id travels as ambient context (a ``ContextVar``), following the same
convention as OpenTelemetry context propagation.

Assumption (declared, load-bearing): one turn == one asyncio task tree.
Tasks spawned within a turn (speech synthesis, attribution enqueue) inherit
the correlation, which is the desired attribution.  Entry points that start
work *not* belonging to the current turn (autonomous scheduler loops, plugin
background workers) must bind a fresh correlation for each unit of work; if
a future change introduces a shared cross-turn task pool, that boundary must
re-bind explicitly.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from qq_ai_bot.runtime.origin import TurnOrigin


def new_runtime_turn_id() -> str:
    """Random opaque turn id; carries no timing or identity information."""

    return uuid.uuid4().hex


def hash_conversation_key(key: str) -> str:
    """Stable content-free projection of a conversation partition key."""

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class RuntimeTurnCorrelation:
    """Mutable per-turn correlation handle bound to the task tree.

    ``touched`` flips to ``True`` the first time any persistence write point
    claims the id; entry points use it to decide whether the turn deserves an
    observation row at all (pure command / observe-only turns stay silent).
    """

    turn_id: str
    origin: TurnOrigin
    touched: bool = field(default=False)


_CURRENT_CORRELATION: ContextVar[RuntimeTurnCorrelation | None] = ContextVar(
    "qq_ai_bot_runtime_turn_correlation", default=None
)


def current_runtime_turn_correlation() -> RuntimeTurnCorrelation | None:
    return _CURRENT_CORRELATION.get()


def claim_runtime_turn_id() -> str | None:
    """Read the ambient turn id from a persistence write point.

    Marks the correlation as touched.  Returns ``None`` outside any bound
    turn (startup jobs, maintenance loops), in which case callers persist
    NULL — exactly the pre-R1 behaviour.
    """

    correlation = _CURRENT_CORRELATION.get()
    if correlation is None:
        return None
    correlation.touched = True
    return correlation.turn_id


@contextmanager
def bind_runtime_turn(correlation: RuntimeTurnCorrelation) -> Iterator[RuntimeTurnCorrelation]:
    """Bind ``correlation`` to the current task tree for the duration."""

    token = _CURRENT_CORRELATION.set(correlation)
    try:
        yield correlation
    finally:
        _CURRENT_CORRELATION.reset(token)


@dataclass(frozen=True, slots=True)
class RuntimeTurnObservation:
    """One content-free observation row (see migration 0037).

    Only enums, counts, times, hashes and error categories — never prompts,
    message bodies, tool arguments, memory content or ref lists.
    """

    runtime_turn_id: str
    origin: TurnOrigin
    scope_type: str
    conversation_key_hash: str | None
    admission_outcome: str | None
    handled: bool
    sent_messages: int
    error_category: str | None
    total_latency_ms: int
    created_at: datetime
    expires_at: datetime


class TurnObservationRecorder(Protocol):
    """Persistence-side sink for observation rows."""

    async def record_turn(self, observation: RuntimeTurnObservation) -> None: ...
