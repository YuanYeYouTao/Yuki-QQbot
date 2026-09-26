"""Share provider clients only when provider endpoint and credential source match."""

from __future__ import annotations

import os
from collections.abc import Mapping

import httpx

from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.base import LLMConfigurationError, LLMProvider
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.openai_responses import (
    OpenAICompatibleResponsesProvider,
    OpenAIResponsesProvider,
)
from qq_ai_bot.llm.vendor_policy import CHAT_VENDORS
from qq_ai_bot.model_runtime.models import ModelProfile, ModelProtocol


class ModelClientPool:
    """Own provider clients independently from business services."""

    def __init__(
        self,
        *,
        secret_overrides: Mapping[str, str] | None = None,
        injected_profiles: Mapping[str, LLMProvider] | None = None,
    ) -> None:
        self._secret_overrides = dict(secret_overrides or {})
        self._injected_profiles = dict(injected_profiles or {})
        self._clients: dict[tuple[str, float], LLMProvider] = {}
        self._connection_pools: dict[tuple[str, str, str], httpx.AsyncClient] = {}

    def get(self, profile: ModelProfile, *, timeout_seconds: float | None = None) -> LLMProvider:
        injected = self._injected_profiles.get(profile.id)
        if injected is not None:
            return injected
        if timeout_seconds is not None:
            profile = profile.model_copy(update={"timeout_seconds": timeout_seconds})
        key = (profile.id, profile.timeout_seconds)
        existing = self._clients.get(key)
        if existing is not None:
            return existing
        if profile.provider.casefold() == "fake":
            provider: LLMProvider = FakeLLMProvider()
        elif profile.provider.casefold() in CHAT_VENDORS | {"anthropic", "gemini"}:
            api_key = self._secret_overrides.get(profile.api_key_env)
            if api_key is None:
                api_key = os.getenv(profile.api_key_env, "")
            if not api_key:
                raise LLMConfigurationError(
                    f"model profile {profile.id} is missing secret environment variable "
                    f"{profile.api_key_env}"
                )
            connection_key = (
                profile.provider.casefold(),
                profile.base_url.rstrip("/"),
                profile.api_key_env,
            )
            connection_pool = self._connection_pools.get(connection_key)
            if connection_pool is None:
                connection_pool = httpx.AsyncClient(
                    base_url=profile.base_url.rstrip("/"),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
                self._connection_pools[connection_key] = connection_pool
            if profile.protocol is ModelProtocol.CHAT_COMPLETIONS:
                provider = OpenAICompatibleProvider(
                    base_url=profile.base_url,
                    api_key=api_key,
                    timeout_seconds=profile.timeout_seconds,
                    max_retries=profile.max_retries,
                    client=connection_pool,
                    provider_name=profile.provider.casefold(),
                    options=profile.wire_options,
                    headers=profile.headers,
                )
            elif profile.protocol in {ModelProtocol.ANTHROPIC_MESSAGES, ModelProtocol.GEMINI}:
                native_provider = (
                    AnthropicMessagesProvider
                    if profile.protocol is ModelProtocol.ANTHROPIC_MESSAGES
                    else GeminiProvider
                )
                provider = native_provider(
                    base_url=profile.base_url,
                    api_key=api_key,
                    timeout_seconds=profile.timeout_seconds,
                    max_retries=profile.max_retries,
                    client=connection_pool,
                    options=profile.wire_options,
                    headers=profile.headers,
                )
            elif profile.provider.casefold() == "deepseek":
                provider = DeepSeekResponsesProvider(
                    base_url=profile.base_url,
                    api_key=api_key,
                    timeout_seconds=profile.timeout_seconds,
                    max_retries=profile.max_retries,
                    client=connection_pool,
                    headers=profile.headers,
                )
            else:
                response_provider = (
                    OpenAIResponsesProvider
                    if profile.provider.casefold() == "openai"
                    else OpenAICompatibleResponsesProvider
                )
                provider = response_provider(
                    base_url=profile.base_url,
                    api_key=api_key,
                    timeout_seconds=profile.timeout_seconds,
                    max_retries=profile.max_retries,
                    client=connection_pool,
                    headers=profile.headers,
                )
        else:
            raise LLMConfigurationError(
                f"model profile {profile.id} uses unsupported provider {profile.provider}"
            )
        self._clients[key] = provider
        return provider

    @property
    def connection_pool_count(self) -> int:
        """Expose a content-free diagnostic for reuse tests and health output."""

        return len(self._connection_pools)

    async def close(self) -> None:
        closed: set[int] = set()
        for provider in (*self._clients.values(), *self._injected_profiles.values()):
            identity = id(provider)
            if identity in closed:
                continue
            closed.add(identity)
            await provider.close()
        for connection_pool in self._connection_pools.values():
            await connection_pool.aclose()
