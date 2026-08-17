"""Low-cardinality Planner metrics and privacy-preserving structured logs."""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from qq_ai_bot.planner.models import (
    PlannerDecision,
    PlannerReasonCode,
    ReplyNecessitySnapshot,
    TurnPlan,
)

logger = logging.getLogger(__name__)


def identifier_hash(value: str | None) -> str | None:
    """Return a stable short hash without exposing a QQ, group, or conversation key."""

    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class PlannerRequestToken:
    """Opaque handle used to finish exactly one active Planner request."""

    id: str
    conversation_key_hash: str
    sender_user_id_hash: str
    group_id_hash: str | None


@dataclass(frozen=True, slots=True)
class PlannerMetricsSnapshot:
    """Process-local metrics suitable for status and health endpoints."""

    total_requests: int
    successful_plans: int
    fallback_plans: int
    interrupted_requests: int
    failed_requests: int
    deterministic_effects: int
    timeout_fallbacks: int
    invalid_response_fallbacks: int
    provider_error_fallbacks: int
    fallback_agent_requests: int
    fallback_tool_calls: int
    active_requests: int
    last_latency_seconds: float | None
    last_decision: PlannerDecision | None
    last_planned_at: datetime | None


class PlannerObservability:
    """Track Planner health without retaining prompts, message text, or raw identifiers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._total_requests = 0
        self._successful_plans = 0
        self._fallback_plans = 0
        self._interrupted_requests = 0
        self._failed_requests = 0
        self._deterministic_effects = 0
        self._timeout_fallbacks = 0
        self._invalid_response_fallbacks = 0
        self._provider_error_fallbacks = 0
        self._fallback_agent_requests = 0
        self._fallback_tool_calls = 0
        self._last_latency_seconds: float | None = None
        self._last_decision: PlannerDecision | None = None
        self._last_planned_at: datetime | None = None

    def record_necessity(
        self,
        snapshot: ReplyNecessitySnapshot,
        *,
        conversation_key: str,
    ) -> None:
        """Log only bounded scoring data and a hashed conversation identity."""

        logger.info(
            "planner_necessity_evaluated conversation_hash=%s score=%d enter=%s reasons=%s",
            identifier_hash(conversation_key),
            snapshot.score,
            snapshot.should_enter_planner,
            ",".join(snapshot.reasons),
        )

    def request_started(
        self,
        *,
        conversation_key: str,
        sender_user_id: str,
        group_id: str | None,
    ) -> PlannerRequestToken:
        """Increment active request gauges and return an opaque finish token."""

        token = PlannerRequestToken(
            id=uuid.uuid4().hex,
            conversation_key_hash=identifier_hash(conversation_key) or "missing",
            sender_user_id_hash=identifier_hash(sender_user_id) or "missing",
            group_id_hash=identifier_hash(group_id),
        )
        with self._lock:
            self._active.add(token.id)
            self._total_requests += 1
        logger.info(
            "planner_entered conversation_hash=%s sender_hash=%s group_hash=%s",
            token.conversation_key_hash,
            token.sender_user_id_hash,
            token.group_id_hash,
        )
        return token

    def request_finished(
        self,
        token: PlannerRequestToken,
        *,
        plan: TurnPlan,
        latency_seconds: float,
        fallback: bool = False,
    ) -> None:
        """Finish one request and record its validated, low-cardinality plan metadata."""

        if not self._remove_active(token):
            return
        now = datetime.now(UTC)
        with self._lock:
            self._successful_plans += 1
            self._fallback_plans += int(fallback)
            if plan.reason_code is PlannerReasonCode.PLANNER_TIMEOUT_FALLBACK:
                self._timeout_fallbacks += 1
            elif plan.reason_code is PlannerReasonCode.PLANNER_INVALID_RESPONSE_FALLBACK:
                self._invalid_response_fallbacks += 1
            elif plan.reason_code is PlannerReasonCode.PLANNER_PROVIDER_ERROR_FALLBACK:
                self._provider_error_fallbacks += 1
            if fallback and plan.decision is PlannerDecision.REPLY and not plan.emoji.is_exclusive:
                self._fallback_agent_requests += 1
            self._last_latency_seconds = max(0.0, latency_seconds)
            self._last_decision = plan.decision
            self._last_planned_at = now
        logger.info(
            "planner_planned conversation_hash=%s decision=%s reason=%s delivery=%s "
            "fallback=%s latency_seconds=%.4f",
            token.conversation_key_hash,
            plan.decision.value,
            plan.reason_code.value,
            plan.delivery_mode.value,
            fallback,
            max(0.0, latency_seconds),
        )

    def record_deterministic_effect(self, *, conversation_key: str) -> None:
        """Record a model-free Planner effect decision without raw identifiers."""

        with self._lock:
            self._deterministic_effects += 1
            self._last_decision = PlannerDecision.REPLY
            self._last_planned_at = datetime.now(UTC)
            self._last_latency_seconds = 0.0
        logger.info(
            "planner_deterministic_effect conversation_hash=%s",
            identifier_hash(conversation_key),
        )

    def request_interrupted(
        self,
        token: PlannerRequestToken,
        *,
        latency_seconds: float,
    ) -> None:
        """Record a normal supersession without classifying it as a system failure."""

        if not self._remove_active(token):
            return
        with self._lock:
            self._interrupted_requests += 1
            self._last_latency_seconds = max(0.0, latency_seconds)
        logger.info(
            "planner_interrupted conversation_hash=%s latency_seconds=%.4f",
            token.conversation_key_hash,
            max(0.0, latency_seconds),
        )

    def request_failed(
        self,
        token: PlannerRequestToken,
        *,
        latency_seconds: float,
        error_category: str,
    ) -> None:
        """Record a sanitized failure category without provider or prompt details."""

        if not self._remove_active(token):
            return
        with self._lock:
            self._failed_requests += 1
            self._last_latency_seconds = max(0.0, latency_seconds)
        logger.warning(
            "planner_failed conversation_hash=%s error_category=%s latency_seconds=%.4f",
            token.conversation_key_hash,
            error_category,
            max(0.0, latency_seconds),
        )

    def snapshot(self) -> PlannerMetricsSnapshot:
        """Return one internally consistent metrics view without requesting a model."""

        with self._lock:
            return PlannerMetricsSnapshot(
                total_requests=self._total_requests,
                successful_plans=self._successful_plans,
                fallback_plans=self._fallback_plans,
                interrupted_requests=self._interrupted_requests,
                failed_requests=self._failed_requests,
                deterministic_effects=self._deterministic_effects,
                timeout_fallbacks=self._timeout_fallbacks,
                invalid_response_fallbacks=self._invalid_response_fallbacks,
                provider_error_fallbacks=self._provider_error_fallbacks,
                fallback_agent_requests=self._fallback_agent_requests,
                fallback_tool_calls=self._fallback_tool_calls,
                active_requests=len(self._active),
                last_latency_seconds=self._last_latency_seconds,
                last_decision=self._last_decision,
                last_planned_at=self._last_planned_at,
            )

    def _remove_active(self, token: PlannerRequestToken) -> bool:
        with self._lock:
            if token.id not in self._active:
                return False
            self._active.remove(token.id)
            return True
