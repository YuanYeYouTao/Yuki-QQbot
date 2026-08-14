"""Task-routed model runtime public API, resolved lazily for SQLAlchemy startup."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "BackgroundModelPreempted",
    "LegacyTaskModelExecutor",
    "ModelCapability",
    "ModelClientPool",
    "ModelCompleter",
    "ModelExecutionPriority",
    "ModelExecutor",
    "ModelInvocationRecord",
    "ModelInvocationRepository",
    "ModelProfile",
    "ModelProfileCatalog",
    "ModelProtocol",
    "ModelRoute",
    "ModelRouter",
    "ModelRuntimeConfigurationError",
    "ModelStats",
    "ModelTask",
    "StructuredOutputMode",
    "StructuredTaskError",
    "StructuredTaskRunner",
    "TaskModelExecutor",
    "load_model_profile_catalog",
    "require_model_executor",
]

_EXPORT_MODULES = {
    "LegacyTaskModelExecutor": "executor",
    "BackgroundModelPreempted": "executor",
    "ModelCompleter": "executor",
    "ModelExecutor": "executor",
    "TaskModelExecutor": "executor",
    "require_model_executor": "executor",
    "ModelCapability": "models",
    "ModelExecutionPriority": "models",
    "ModelInvocationRecord": "models",
    "ModelProfile": "models",
    "ModelProtocol": "models",
    "ModelRoute": "models",
    "ModelStats": "models",
    "ModelTask": "models",
    "StructuredOutputMode": "models",
    "ModelClientPool": "pool",
    "ModelProfileCatalog": "profiles",
    "ModelRuntimeConfigurationError": "profiles",
    "load_model_profile_catalog": "profiles",
    "ModelInvocationRepository": "repository",
    "ModelRouter": "routes",
    "StructuredTaskError": "structured",
    "StructuredTaskRunner": "structured",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"qq_ai_bot.model_runtime.{module_name}"), name)
    globals()[name] = value
    return value
