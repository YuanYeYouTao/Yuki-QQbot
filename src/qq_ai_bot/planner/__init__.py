"""Planner-first conversation planning primitives.

Exports are resolved lazily because SQLAlchemy imports ``planner.db_models``
while the persistence package itself is still initializing.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "DeliveryMode",
    "FakePlannerProvider",
    "LLMPlannerProvider",
    "PlannedTurn",
    "PlannerDecision",
    "PlannerInput",
    "PlannerInterruptedError",
    "PlannerMemoryContext",
    "PlannerMetricsSnapshot",
    "PlannerObservability",
    "PlannerProvider",
    "PlannerProviderError",
    "PlannerReasonCode",
    "PlannerRequestToken",
    "PlannerResponseError",
    "PlannerSignal",
    "PlannerTimeoutError",
    "ReplyNecessityFeatures",
    "ReplyNecessityScorer",
    "ReplyNecessitySnapshot",
    "ToolMode",
    "TurnPlan",
    "constrain_turn_plan",
    "deterministic_fallback_plan",
    "identifier_hash",
]

_EXPORT_MODULES = {
    "DeliveryMode": "models",
    "PlannedTurn": "models",
    "PlannerDecision": "models",
    "PlannerInput": "models",
    "PlannerMemoryContext": "models",
    "PlannerReasonCode": "models",
    "PlannerSignal": "models",
    "ReplyNecessitySnapshot": "models",
    "ToolMode": "models",
    "TurnPlan": "models",
    "ReplyNecessityFeatures": "necessity",
    "ReplyNecessityScorer": "necessity",
    "PlannerMetricsSnapshot": "observability",
    "PlannerObservability": "observability",
    "PlannerRequestToken": "observability",
    "identifier_hash": "observability",
    "FakePlannerProvider": "fake",
    "LLMPlannerProvider": "provider",
    "PlannerInterruptedError": "provider",
    "PlannerProvider": "provider",
    "PlannerProviderError": "provider",
    "PlannerResponseError": "provider",
    "PlannerTimeoutError": "provider",
    "constrain_turn_plan": "provider",
    "deterministic_fallback_plan": "provider",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"qq_ai_bot.planner.{module_name}"), name)
    globals()[name] = value
    return value
