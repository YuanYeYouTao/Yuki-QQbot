"""Shared bounded HTTP transport; adapters own only serialization and parsing."""

from __future__ import annotations

import time
from abc import abstractmethod
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.llm.base import (
    LLMConfigurationError,
    LLMProvider,
    LLMTimeoutError,
    LLMUnavailableError,
    RetryableProviderError,
)
from qq_ai_bot.llm.http_errors import check_provider_response
from qq_ai_bot.llm.wire_diagnostics import WireRequestObserver


class JSONHTTPProvider(LLMProvider):
    provider_name = "openai_compatible"
    protocol = "chat_completions"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        max_retries: int,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._max_retries = max_retries
        self._headers = dict(headers or {})
        self._owns_client = client is None
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/") + "/")
        self._wire_observer = WireRequestObserver()

    @abstractmethod
    def _build_payload(self, request: ChatRequest) -> dict[str, Any]: ...

    @abstractmethod
    def _parse(self, response: httpx.Response, request: ChatRequest) -> ChatResponse: ...

    def _path(self, request: ChatRequest) -> str:
        return "chat/completions"

    def _request_headers(self) -> dict[str, str]:
        return {**self._headers, "Authorization": f"Bearer {self._api_key}"}

    async def _post(self, request: ChatRequest) -> httpx.Response:
        from qq_ai_bot.model_runtime.dispatch_guard import check_model_dispatch

        payload = self._build_payload(request)
        self._wire_observer.observe(
            payload,
            self.protocol,
            chain_id=request.request_chain_id,
            provider=self.provider_name,
        )
        await check_model_dispatch()
        response = await self._client.post(
            self._path(request),
            headers=self._request_headers(),
            json=payload,
            timeout=self._timeout,
        )
        check_provider_response(response)
        return response

    async def complete(self, request: ChatRequest) -> ChatResponse:
        from dataclasses import replace

        from qq_ai_bot.runtime.work_activation import current_work_control

        if not self._api_key or not request.model:
            raise LLMConfigurationError("LLM is not configured")
        started = time.perf_counter()
        # Provider-executed tools have no local receipt for uncertain transport outcomes.
        attempts = 1 if request.native_tools else self._max_retries + 1
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_random_exponential(multiplier=0.25, max=2),
                retry=retry_if_exception_type((httpx.TransportError, RetryableProviderError)),
                reraise=True,
            ):
                with attempt:
                    work = current_work_control.get()
                    if work is not None and attempt.retry_state.attempt_number > 1:
                        await work.reserve_request(auxiliary=True)
                    response = await self._post(request)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("LLM request timed out") from exc
        except (httpx.TransportError, RetryableProviderError) as exc:
            raise LLMUnavailableError(
                "LLM is temporarily unavailable",
                diagnostics=getattr(exc, "diagnostics", {}),
            ) from exc
        return replace(
            self._parse(response, request), latency_seconds=time.perf_counter() - started
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
