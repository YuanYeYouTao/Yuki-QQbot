"""Model runtime module and immutable bundle."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.model_runtime import (
    ModelClientPool,
    ModelInvocationRepository,
    ModelProfileCatalog,
    ModelRouter,
    ModelTask,
    TaskModelExecutor,
    load_model_profile_catalog,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.settings_domains import ModelRuntimeSettings


@dataclass(frozen=True, slots=True)
class ModelRuntimeBundle:
    profiles: ModelProfileCatalog
    clients: ModelClientPool
    invocations: ModelInvocationRepository
    router: ModelRouter
    executor: TaskModelExecutor
    chat_provider: LLMProvider


class ModelRuntimeModule:
    def __init__(
        self,
        settings: ModelRuntimeSettings,
        database: Database,
        *,
        lifecycle: LifecycleRegistry,
    ) -> None:
        self._settings = settings
        self._database = database
        self._lifecycle = lifecycle

    def build(self) -> ModelRuntimeBundle:
        settings = self._settings
        profiles = load_model_profile_catalog(
            settings.model_profiles_file,
            legacy_provider=settings.llm_provider,
            legacy_base_url=settings.llm_base_url,
            legacy_model=settings.llm_model,
            legacy_timeout_seconds=settings.llm_timeout_seconds,
            legacy_max_retries=settings.llm_max_retries,
            legacy_temperature=settings.llm_temperature,
            legacy_max_output_tokens=settings.llm_max_output_tokens,
            legacy_thinking_enabled=settings.llm_thinking_enabled,
            legacy_reasoning_effort=settings.llm_reasoning_effort,
            environment={
                "LLM_BASE_URL": settings.llm_base_url,
                "LLM_MODEL": settings.llm_model,
                "LLM_REASONING_EFFORT": (
                    settings.llm_reasoning_effort.value
                    if settings.llm_reasoning_effort is not None
                    else ""
                ),
                "LLM_FLASH_BASE_URL": settings.llm_flash_base_url,
                "LLM_FLASH_MODEL": settings.llm_flash_model,
            },
        )
        clients = ModelClientPool(
            secret_overrides={
                "LLM_API_KEY": settings.llm_api_key,
                "LLM_FLASH_API_KEY": settings.llm_flash_api_key,
            },
        )
        invocations = ModelInvocationRepository(self._database)
        router = ModelRouter(profiles)
        for profile in profiles.profiles.values():
            clients.get(profile)
        executor = TaskModelExecutor(
            router=router,
            pool=clients,
            invocations=invocations,
            max_concurrency=settings.global_llm_concurrency,
            compaction_timeout_seconds=settings.conversation_rollup_model_timeout_seconds,
            self_reflection_timeout_seconds=settings.memory_self_reflection_timeout_seconds,
        )
        self._lifecycle.register("model_runtime", close=executor.close)
        _route, chat_profile = router.route(ModelTask.CHAT_AGENT)
        return ModelRuntimeBundle(
            profiles,
            clients,
            invocations,
            router,
            executor,
            clients.get(chat_profile),
        )
