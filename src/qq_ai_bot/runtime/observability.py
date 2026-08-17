"""Ambient turn correlation and the content-free observation contract.

R1 introduces an opaque ``runtime_turn_id`` propagated to the persistence
write points of model invocations, tool invocations and memory recall
receipts.  Instead of changing every executor/repository signature,
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

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from qq_ai_bot.runtime.origin import TurnOrigin

logger = logging.getLogger(__name__)

DEFAULT_OBSERVATION_RETENTION_DAYS = 30


def new_runtime_turn_id() -> str:
    """Random opaque turn id; carries no timing or identity information."""

    return uuid.uuid4().hex


def hash_conversation_key(key: str) -> str:
    """Stable content-free projection of a conversation partition key."""

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def identifier_hash(value: str | None) -> str | None:
    """Return a stable short hash without exposing a QQ, group, or conversation key."""

    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


_STABLE_IDENTIFIER_SALT = "yuki-planner-v1"


def stable_identifier_hash(value: str, *, kind: str) -> str:
    """Domain-separated SHA-256 used by cadence and 0039 backfill.

    The salt string is historical and load-bearing; do not change the payload.
    """

    payload = f"{_STABLE_IDENTIFIER_SALT}\0{kind}\0{value}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


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


def build_turn_observation(
    correlation: RuntimeTurnCorrelation,
    *,
    scope_type: str,
    conversation_key: str | None,
    admission_outcome: str | None,
    handled: bool,
    sent_messages: int,
    error_category: str | None,
    total_latency_ms: int,
    retention_days: int = DEFAULT_OBSERVATION_RETENTION_DAYS,
    now: datetime | None = None,
) -> RuntimeTurnObservation:
    """Project one finished turn onto the content-free observation row.

    The raw ``conversation_key`` never reaches the row; only its hash does.
    """

    created = now or datetime.now(UTC)
    return RuntimeTurnObservation(
        runtime_turn_id=correlation.turn_id,
        origin=correlation.origin,
        scope_type=scope_type[:16],
        conversation_key_hash=(
            hash_conversation_key(conversation_key) if conversation_key else None
        ),
        admission_outcome=admission_outcome[:64] if admission_outcome else None,
        handled=handled,
        sent_messages=max(0, sent_messages),
        error_category=error_category[:128] if error_category else None,
        total_latency_ms=max(0, total_latency_ms),
        created_at=created,
        expires_at=created + timedelta(days=max(1, retention_days)),
    )


async def record_observation_safely(
    recorder: TurnObservationRecorder | None,
    observation: RuntimeTurnObservation,
) -> None:
    """Persist one observation row without ever breaking the turn itself.

    ``asyncio.shield`` lets an in-flight row survive task cancellation (the
    cancellation still propagates to the caller); any storage failure is
    reduced to a content-free warning.
    """

    if recorder is None:
        return
    try:
        await asyncio.shield(recorder.record_turn(observation))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "runtime_turn_observation_failed origin=%s exception_category=%s",
            observation.origin.value,
            type(exc).__name__,
        )
