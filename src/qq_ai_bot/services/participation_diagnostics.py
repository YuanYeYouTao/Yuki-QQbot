"""Bounded aggregate health facts; never expose source text, IDs or provider errors."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from yuki_participation.controller import State, StoredObservation

from qq_ai_bot.conversation.autonomy_db_models import (
    AutonomyBindingModel,
    InitiativeFeedbackModel,
    InitiativeRunModel,
)
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.persistence.database import Database

_WINDOW = 128
_FEEDBACK_WINDOW = 1024
_FALLBACKS = {"provider_unavailable", "missing_configuration", "semantic_not_ready"}
_OBSERVER_ERRORS = {
    "authentication",
    "request_validation",
    "rate_limit",
    "provider_http",
    "response_invalid",
    "transport",
    "partial_required_dimensions_invalid",
}


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _iso(value: datetime) -> str:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).isoformat()


def _controller_facts(states: Sequence[State], *, now: float) -> dict[str, object]:
    # These snapshots are bounded by the controller's own retention. They are not
    # lifetime request telemetry; expiry/eviction must not become fabricated zeros.
    support: Counter[str] = Counter()
    candidates: Counter[str] = Counter()
    observations: list[StoredObservation] = []
    pending = inflight = consecutive_failures = degraded = local_rejections = 0
    errors: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for state in states[:32]:
        for candidate in state.candidates.values():
            candidates[candidate.kind.value] += 1
            kind = candidate.support.kind
            support[kind] += 1
            if candidate.support.strength(now) > 0:
                support[f"{kind}_valid"] += 1
        observations.extend(state.observations.values())
        checkpoint = state.observer_checkpoint
        queue = checkpoint.get("queue", {})
        health = checkpoint.get("health", {})
        if isinstance(queue, dict):
            pending += len(queue.get("pending", ()))
            inflight += bool(queue.get("in_flight"))
        if isinstance(health, dict):
            consecutive_failures += int(health.get("failures", 0))
            degraded += bool(health.get("degraded", False))
        failure = checkpoint.get("last_failure")
        if isinstance(failure, dict):
            category = failure.get("category")
            errors[
                category if isinstance(category, str) and category in _OBSERVER_ERRORS else "other"
            ] += 1
            status = failure.get("status")
            if isinstance(status, int) and 400 <= status < 600:
                statuses[str(status)] += 1
        rejected = checkpoint.get("local_rejections", ())
        if isinstance(rejected, (tuple, list)):
            local_rejections += len(rejected)
    input_values = [item.input_tokens for item in observations if item.input_tokens is not None]
    output_values = [item.output_tokens for item in observations if item.output_tokens is not None]
    unknown = sum(
        any(
            answer.p("unknown")
            >= max(
                (value for option, value in answer.probabilities.items() if option != "unknown"),
                default=0.0,
            )
            for answer in item.answers.values()
        )
        for item in observations
    )
    return {
        "window": "retained_snapshots_of_loaded_scopes",
        "scope_limit": 32,
        "scope_count": min(len(states), 32),
        "pending_observations": pending,
        "in_flight_observations": inflight,
        "pending_proposals": sum(state.pending is not None for state in states[:32]),
        "candidates": {key: candidates[key] for key in ("conversation", "recall", "contact")},
        "support": {
            key: support[key]
            for key in ("observed", "predicted", "observed_valid", "predicted_valid")
        },
        "provider": {
            "observations_retained": len(observations),
            "input_tokens_known_sum": sum(input_values) if input_values else None,
            "input_tokens_known_samples": len(input_values),
            "output_tokens_known_sum": sum(output_values) if output_values else None,
            "output_tokens_known_samples": len(output_values),
            "unknown_winning_observations": unknown,
            "unknown_winning_ratio": _ratio(unknown, len(observations)),
            "consecutive_failures_sum": consecutive_failures,
            "degraded_scopes": degraded,
            "last_failure_categories": dict(sorted(errors.items())),
            "last_failure_http_statuses": dict(sorted(statuses.items())),
            "local_rejections_retained": local_rejections,
            "lifetime_requests": None,
            "lifetime_failures": None,
            "latency_seconds": None,
            "accuracy": None,
            "unknown_reason": "no_persisted_lifetime_latency_or_independent_labels",
        },
    }


async def participation_diagnostics(
    database: Database, states: Sequence[State], *, now: float
) -> dict[str, object]:
    """Read-only bounded windows. Ratios describe retained rows, never all-time rates."""
    controller = _controller_facts(states, now=now)
    async with database.sessions() as session:
        bindings = list(
            await session.execute(
                select(AutonomyBindingModel.effective_owner, AutonomyBindingModel.fallback_reason)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == AutonomyBindingModel.conversation_id,
                )
                .where(CanonicalConversationModel.generation == AutonomyBindingModel.generation)
                .order_by(AutonomyBindingModel.updated_at.desc())
                .limit(_WINDOW + 1)
            )
        )
        runs = list(
            await session.execute(
                select(
                    InitiativeRunModel.id, InitiativeRunModel.state, InitiativeRunModel.created_at
                )
                .order_by(InitiativeRunModel.created_at.desc(), InitiativeRunModel.id.desc())
                .limit(_WINDOW + 1)
            )
        )
        selected = runs[:_WINDOW]
        feedback = (
            list(
                await session.execute(
                    select(InitiativeFeedbackModel.run_id, InitiativeFeedbackModel.payload_json)
                    .where(InitiativeFeedbackModel.run_id.in_(row.id for row in selected))
                    .order_by(InitiativeFeedbackModel.created_at.desc())
                    .limit(_FEEDBACK_WINDOW + 1)
                )
            )
            if selected
            else []
        )
    owners = Counter(row.effective_owner for row in bindings[:_WINDOW])
    fallbacks = Counter(
        row.fallback_reason if row.fallback_reason in _FALLBACKS else "other"
        for row in bindings[:_WINDOW]
        if row.fallback_reason is not None
    )
    outcomes = Counter(row.state for row in selected)
    effects: set[tuple[str, str]] = set()
    invalid_pages = 0
    for row in feedback[:_FEEDBACK_WINDOW]:
        try:
            payload = json.loads(row.payload_json)
            refs = payload.get("effects", ())
            if not isinstance(refs, (list, tuple)):
                raise ValueError("invalid_effect_refs")
            effects.update((row.run_id, ref) for ref in refs if isinstance(ref, str))
        except (ValueError, TypeError, AttributeError):
            invalid_pages += 1
    actual = {(run, ref) for run, ref in effects if ref.startswith(("social:", "tool:"))}
    with_effects = len({run for run, _ in actual})
    feedback_complete = len(feedback) <= _FEEDBACK_WINDOW and invalid_pages == 0
    return {
        "selector": {
            "window": "latest_current_generation_bindings",
            "limit": _WINDOW,
            "count": min(len(bindings), _WINDOW),
            "truncated": len(bindings) > _WINDOW,
            "owners": {name: owners[name] for name in ("off", "legacy", "semantic")},
            "fallbacks": {name: fallbacks[name] for name in sorted(_FALLBACKS | {"other"})},
        },
        "controller": controller,
        "runs": {
            "window": "latest_accepted_runs_by_creation_all_generations",
            "limit": _WINDOW,
            "accepted_count": len(selected),
            "truncated": len(runs) > _WINDOW,
            "oldest_created_at": _iso(selected[-1].created_at) if selected else None,
            "newest_created_at": _iso(selected[0].created_at) if selected else None,
            "states": {
                name: outcomes[name]
                for name in (
                    "accepted",
                    "running",
                    "completed",
                    "no_reply",
                    "interrupted",
                    "failed",
                )
            },
            "no_reply_per_accepted": _ratio(outcomes["no_reply"], len(selected)),
            "effect_runs_per_accepted": _ratio(with_effects, len(selected))
            if feedback_complete
            else None,
            "effect_runs_known": with_effects,
            "message_effects_known": sum(ref.startswith("social:") for _, ref in actual),
            "tool_receipts_known": sum(ref.startswith("tool:") for _, ref in actual),
            "charged_model_requests_known": sum(
                ref.startswith("work-model:") for _, ref in effects
            ),
            "feedback_page_limit": _FEEDBACK_WINDOW,
            "feedback_pages_read": min(len(feedback), _FEEDBACK_WINDOW),
            "feedback_complete": feedback_complete,
            "invalid_feedback_pages": invalid_pages,
        },
    }
