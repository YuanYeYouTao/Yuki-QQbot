"""Load validated model profiles and task routes from TOML."""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from qq_ai_bot.domain.messages import ReasoningEffort
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelRoute,
    ModelTask,
    StructuredOutputMode,
)

logger = logging.getLogger(__name__)

PROFILE_SCHEMA_VERSION = 3
RETIRED_MODEL_ROUTES = frozenset({"planner", "tool_selection"})
MIGRATE_3_6_COMMAND = "qq-ai-bot-cli setup migrate-3-6 --deployment-root <deployment-root>"


class ModelRuntimeConfigurationError(ValueError):
    """The profile file or compatibility configuration is unusable."""


class _ProfileDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3]
    profiles: dict[str, dict[str, Any]]
    routes: dict[str, str]


class ModelProfileCatalog(BaseModel):
    """Immutable, fully validated model configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profiles: dict[str, ModelProfile]
    routes: dict[ModelTask, ModelRoute]
    compatibility_mode: bool = False

    @model_validator(mode="after")
    def _validate_routes(self) -> ModelProfileCatalog:
        missing = set(ModelTask).difference(self.routes)
        if missing:
            names = ", ".join(sorted(task.value for task in missing))
            raise ValueError(f"missing model routes: {names}")
        for task, route in self.routes.items():
            profile = self.profiles.get(route.profile_id)
            if profile is None:
                raise ValueError(
                    f"model route {task.value} references unknown profile {route.profile_id}"
                )
            unavailable = route.required_capabilities.difference(profile.capabilities)
            if unavailable:
                names = ", ".join(sorted(item.value for item in unavailable))
                raise ValueError(
                    f"model route {task.value} requires unsupported capabilities: {names}"
                )
        return self


_DEFAULT_REQUIREMENTS: dict[ModelTask, frozenset[ModelCapability]] = {
    ModelTask.CHAT_AGENT: frozenset({ModelCapability.TOOLS}),
    ModelTask.MEMORY_EXTRACTION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.MEMORY_SELF_REFLECTION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.MEMORY_CONSOLIDATION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.MEMORY_DREAM: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.MEMORY_ATTRIBUTION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.RELATIONSHIP_EVALUATION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.EMOJI_REPLACEMENT: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.AUTOMATION_TEXT_GENERATION: frozenset(),
    ModelTask.AUTOMATION_AGENT: frozenset({ModelCapability.TOOLS}),
    ModelTask.PLUGIN_AGENT_SESSION: frozenset({ModelCapability.TOOLS}),
    ModelTask.UTILITY_STRUCTURED: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.CONVERSATION_COMPACTION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
}


def load_model_profile_catalog(
    path: Path,
    *,
    legacy_provider: str,
    legacy_base_url: str,
    legacy_model: str,
    legacy_timeout_seconds: float,
    legacy_max_retries: int,
    legacy_temperature: float,
    legacy_max_output_tokens: int,
    legacy_thinking_enabled: bool | None,
    legacy_reasoning_effort: ReasoningEffort | None = None,
    environment: Mapping[str, str] | None = None,
) -> ModelProfileCatalog:
    """Load TOML or explicitly normalize the legacy LLM settings to ``main``."""

    if not path.is_file():
        logger.warning(
            "model_profiles_compatibility_mode file=%s profile=main",
            path,
        )
        capabilities = frozenset(ModelCapability)
        profile = ModelProfile(
            id="main",
            provider=legacy_provider,
            base_url=legacy_base_url,
            api_key_env="LLM_API_KEY" if legacy_provider.casefold() != "fake" else "",
            model=legacy_model or "fake",
            timeout_seconds=legacy_timeout_seconds,
            max_retries=legacy_max_retries,
            default_temperature=legacy_temperature,
            default_max_output_tokens=legacy_max_output_tokens,
            thinking_enabled=legacy_thinking_enabled,
            reasoning_effort=legacy_reasoning_effort,
            structured_output_mode=StructuredOutputMode.FUNCTION_TOOL,
            capabilities=capabilities,
        )
        routes = {
            task: ModelRoute(
                task=task,
                profile_id="main",
                required_capabilities=requirements,
            )
            for task, requirements in _DEFAULT_REQUIREMENTS.items()
        }
        return ModelProfileCatalog(
            profiles={"main": profile},
            routes=routes,
            compatibility_mode=True,
        )

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        version = raw.get("schema_version", 1)
        if version != PROFILE_SCHEMA_VERSION:
            raise ModelRuntimeConfigurationError(
                f"model profile schema v{version} is no longer accepted; run: {MIGRATE_3_6_COMMAND}"
            )
        document = _ProfileDocument.model_validate(raw)
        profiles = {
            profile_id: ModelProfile.model_validate(
                {
                    "id": profile_id,
                    **_resolve_profile_environment(payload, environment=environment),
                }
            )
            for profile_id, payload in document.profiles.items()
        }
        raw_routes = dict(document.routes)
        retired = RETIRED_MODEL_ROUTES.intersection(raw_routes)
        if retired:
            names = ", ".join(sorted(retired))
            raise ModelRuntimeConfigurationError(
                f"retired model routes remain ({names}); run: {MIGRATE_3_6_COMMAND}"
            )
        if (
            ModelTask.MEMORY_SELF_REFLECTION.value not in raw_routes
            and ModelTask.MEMORY_EXTRACTION.value in raw_routes
        ):
            logger.warning(
                "model_route_compatibility task=memory_self_reflection source=memory_extraction"
            )
            raw_routes[ModelTask.MEMORY_SELF_REFLECTION.value] = raw_routes[
                ModelTask.MEMORY_EXTRACTION.value
            ]
        if (
            ModelTask.MEMORY_CONSOLIDATION.value not in raw_routes
            and ModelTask.MEMORY_EXTRACTION.value in raw_routes
        ):
            logger.warning(
                "model_route_compatibility task=memory_consolidation source=memory_extraction"
            )
            raw_routes[ModelTask.MEMORY_CONSOLIDATION.value] = raw_routes[
                ModelTask.MEMORY_EXTRACTION.value
            ]
        if (
            ModelTask.MEMORY_DREAM.value not in raw_routes
            and ModelTask.MEMORY_CONSOLIDATION.value in raw_routes
        ):
            logger.warning(
                "model_route_compatibility task=memory_dream source=memory_consolidation"
            )
            raw_routes[ModelTask.MEMORY_DREAM.value] = raw_routes[
                ModelTask.MEMORY_CONSOLIDATION.value
            ]
        if ModelTask.MEMORY_ATTRIBUTION.value not in raw_routes:
            source_task = next(
                (task for task in (ModelTask.UTILITY_STRUCTURED,) if task.value in raw_routes),
                None,
            )
            if source_task is not None:
                logger.warning(
                    "model_route_compatibility task=memory_attribution source=%s",
                    source_task.value,
                )
                raw_routes[ModelTask.MEMORY_ATTRIBUTION.value] = raw_routes[source_task.value]
        if ModelTask.CONVERSATION_COMPACTION.value not in raw_routes:
            source_task = next(
                (
                    task
                    for task in (
                        ModelTask.MEMORY_DREAM,
                        ModelTask.UTILITY_STRUCTURED,
                        ModelTask.MEMORY_EXTRACTION,
                    )
                    if task.value in raw_routes
                ),
                None,
            )
            if source_task is not None:
                logger.warning(
                    "model_route_compatibility task=conversation_compaction source=%s",
                    source_task.value,
                )
                raw_routes[ModelTask.CONVERSATION_COMPACTION.value] = raw_routes[source_task.value]
        routes = {
            ModelTask(task_name): ModelRoute(
                task=ModelTask(task_name),
                profile_id=profile_id,
                required_capabilities=_DEFAULT_REQUIREMENTS[ModelTask(task_name)],
            )
            for task_name, profile_id in raw_routes.items()
        }
        return ModelProfileCatalog(profiles=profiles, routes=routes)
    except ModelRuntimeConfigurationError:
        raise
    except (OSError, tomllib.TOMLDecodeError, ValidationError, KeyError, ValueError) as exc:
        raise ModelRuntimeConfigurationError(f"invalid model profile configuration: {exc}") from exc


def _resolve_profile_environment(
    payload: dict[str, Any],
    *,
    environment: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Resolve public endpoint/model indirections while leaving API keys unread."""

    resolved = dict(payload)
    for value_name, env_name_key in (("base_url", "base_url_env"), ("model", "model_env")):
        env_name = resolved.pop(env_name_key, None)
        if env_name is None:
            continue
        if not isinstance(env_name, str) or not env_name:
            raise ValueError(f"{env_name_key} must name an environment variable")
        value = (environment or {}).get(env_name) or os.environ.get(env_name)
        if not value:
            raise ValueError(f"environment variable {env_name} is required")
        resolved[value_name] = value

    reasoning_effort_env = resolved.pop("reasoning_effort_env", None)
    if reasoning_effort_env is not None:
        if not isinstance(reasoning_effort_env, str) or not reasoning_effort_env:
            raise ValueError("reasoning_effort_env must name an environment variable")
        value = (environment or {}).get(reasoning_effort_env) or os.environ.get(
            reasoning_effort_env
        )
        if value:
            resolved["reasoning_effort"] = value

    thinking_mode = resolved.pop("thinking_mode", None)
    if thinking_mode is not None:
        modes = {"configurable": None, "disabled": False, "enabled": True}
        try:
            resolved["thinking_enabled"] = modes[thinking_mode]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"unknown thinking_mode: {thinking_mode}") from exc
    return resolved
