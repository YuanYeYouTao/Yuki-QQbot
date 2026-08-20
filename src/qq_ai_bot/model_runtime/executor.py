"""Execute model requests through explicit tasks and profiles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import replace
from typing import Protocol

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelExecutionPriority,
    ModelProtocol,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.model_runtime.routes import ModelRouter

logger = logging.getLogger(__name__)


def request_shape_hash(
    request: ChatRequest,
    *,
    provider: str,
    model: str,
    profile_id: str,
    protocol: str,
) -> str:
    """Hash the actual cache-relevant request shape without message content."""

    payload = {
        "provider": provider,
        "model": model,
        "profile_id": profile_id,
        "protocol": protocol,
        "static_prompt_revision": request.static_prompt_revision,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in request.tools
        ],
        "native_tools": [tool.type.value for tool in request.native_tools],
        "response_format": request.response_format,
        "structured_output": request.structured_output,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class BackgroundModelPreempted(RuntimeError):
    """A best-effort provider call yielded to newly arrived foreground work."""


class ModelCompleter(Protocol):
    """Small compatibility boundary for injected test providers."""

    async def complete(self, request: ChatRequest) -> ChatResponse: ...


class ModelExecutor(Protocol):
    """Business-facing task executor contract."""

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
    ) -> ChatResponse: ...

    def model_name(self, task: ModelTask) -> str: ...

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode: ...

    def protocol(self, task: ModelTask) -> ModelProtocol: ...

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]: ...


class LegacyTaskModelExecutor:
    """Adapt an injected test provider without leaking it into business services."""

    def __init__(self, provider: ModelCompleter, *, model: str = "fake") -> None:
        self._provider = provider
        self._model = model

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
    ) -> ChatResponse:
        del task, priority
        normalized = replace(
            request,
            request_shape_hash=request_shape_hash(
                request,
                provider="fake",
                model=self._model,
                profile_id="legacy",
                protocol=ModelProtocol.CHAT_COMPLETIONS.value,
            ),
        )
        return await self._provider.complete(normalized)

    def model_name(self, task: ModelTask) -> str:
        del task
        return self._model

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        del task
        return StructuredOutputMode.TEXT_JSON

    def protocol(self, task: ModelTask) -> ModelProtocol:
        del task
        return ModelProtocol.CHAT_COMPLETIONS

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]:
        del task
        return frozenset(ModelCapability)


def require_model_executor(
    model_executor: ModelExecutor | None,
    *,
    provider: ModelCompleter | None = None,
    model: str = "fake",
) -> ModelExecutor:
    """Normalize old test injection at one migration boundary."""

    if model_executor is not None:
        return model_executor
    if provider is None:
        raise TypeError("model_executor is required")
    return LegacyTaskModelExecutor(provider, model=model)


class TaskModelExecutor:
    """The only main-model entry point used by business services."""

    def __init__(
        self,
        *,
        router: ModelRouter,
        pool: ModelClientPool,
        invocations: ModelInvocationRepository | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        if max_concurrency is not None and max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive when configured")
        self._router = router
        self._pool = pool
        self._invocations = invocations
        self._semaphore = (
            asyncio.Semaphore(max_concurrency) if max_concurrency is not None else None
        )
        self._priority_condition = asyncio.Condition()
        self._foreground_active = 0
        self._foreground_waiting = 0
        self._exclusive_active = 0
        self._exclusive_waiting = 0
        self._exclusive_slot = asyncio.Lock()
        self._background_slot = asyncio.Lock()
        self._background_provider_task: asyncio.Task[ChatResponse] | None = None
        self._prompt_shapes: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._prefix_shape_match_total = 0
        self._prefix_shape_split_total = 0

    @property
    def router(self) -> ModelRouter:
        return self._router

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
    ) -> ChatResponse:
        required: set[ModelCapability] = set()
        if request.tools and not request.structured_output:
            required.add(ModelCapability.TOOLS)
        if request.structured_output or request.response_format is not None:
            required.add(ModelCapability.STRUCTURED_OUTPUT)
        if request.thinking_enabled or request.reasoning_effort is not None:
            required.add(ModelCapability.REASONING)
        if request.native_tools:
            required.add(ModelCapability.NATIVE_WEB_SEARCH)
        _route, profile = self._router.route(task, required_capabilities=frozenset(required))
        if request.continuation is not None:
            continuation = request.continuation
            if (
                continuation.profile_id != profile.id
                or continuation.provider != profile.provider.casefold()
                or continuation.protocol != profile.protocol.value
            ):
                raise ValueError("continuation cannot be routed to a different model profile")
        provider = self._pool.get(profile)
        thinking_enabled = (
            profile.thinking_enabled
            if request.thinking_enabled is None
            else request.thinking_enabled
        )
        normalized = ChatRequest(
            messages=request.messages,
            model=profile.model,
            temperature=(
                profile.default_temperature if request.temperature is None else request.temperature
            ),
            max_output_tokens=(
                profile.default_max_output_tokens
                if request.max_output_tokens is None
                else request.max_output_tokens
            ),
            thinking_enabled=thinking_enabled,
            reasoning_effort=(
                (request.reasoning_effort or profile.reasoning_effort) if thinking_enabled else None
            ),
            tools=request.tools,
            tool_choice=request.tool_choice,
            response_format=request.response_format,
            structured_output=request.structured_output,
            native_tools=request.native_tools,
            continuation=request.continuation,
            function_outputs=request.function_outputs,
            conversation_prefix_hash=request.conversation_prefix_hash,
            request_shape_hash=request_shape_hash(
                request,
                provider=profile.provider,
                model=profile.model,
                profile_id=profile.id,
                protocol=profile.protocol.value,
            ),
            prompt_snapshot_fingerprint=request.prompt_snapshot_fingerprint,
            static_prompt_revision=request.static_prompt_revision,
        )
        if normalized.conversation_prefix_hash:
            self._observe_prompt_shape(normalized)
            logger.info(
                "prompt_request_diagnostics task=%s conversation_prefix_hash=%s "
                "request_shape_hash=%s prompt_snapshot_fingerprint=%s",
                task.value,
                normalized.conversation_prefix_hash,
                normalized.request_shape_hash,
                normalized.prompt_snapshot_fingerprint,
            )
        if profile.protocol is ModelProtocol.RESPONSES:
            logger.info(
                "responses_request_routed task=%s profile_id=%s provider=%s protocol=%s "
                "model=%s native_tool_types=%s function_tool_count=%d web_scope_approved=%s",
                task.value,
                profile.id,
                profile.provider,
                profile.protocol.value,
                profile.model,
                ",".join(tool.type.value for tool in normalized.native_tools) or "none",
                len(normalized.tools),
                bool(normalized.native_tools),
            )

        started = time.perf_counter()
        try:
            response = await self._execute_provider(
                provider,
                normalized,
                priority=priority,
            )
        except Exception as exc:
            if self._invocations is not None:
                await self._invocations.record(
                    task=task,
                    profile_id=profile.id,
                    provider=profile.provider,
                    model=profile.model,
                    success=False,
                    prompt_tokens=None,
                    completion_tokens=None,
                    total_tokens=None,
                    cached_prompt_tokens=None,
                    latency_seconds=time.perf_counter() - started,
                    error_category=type(exc).__name__,
                )
            raise
        if response.continuation is not None:
            response = replace(
                response,
                continuation=replace(response.continuation, profile_id=profile.id),
            )
        if profile.protocol is ModelProtocol.RESPONSES:
            logger.info(
                "responses_request_recorded task=%s profile_id=%s provider=%s protocol=%s "
                "response_status=%s input_tokens=%s output_tokens=%s reasoning_tokens=%s "
                "cached_tokens=%s native_action_count=%d citation_count=%d",
                task.value,
                profile.id,
                profile.provider,
                profile.protocol.value,
                response.status.value,
                response.prompt_tokens,
                response.completion_tokens,
                response.reasoning_tokens,
                response.cached_prompt_tokens,
                len(response.native_tool_events),
                len(response.citations),
            )
        if self._invocations is not None:
            await self._invocations.record(
                task=task,
                profile_id=profile.id,
                provider=profile.provider,
                model=profile.model,
                success=True,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens,
                cached_prompt_tokens=response.cached_prompt_tokens,
                latency_seconds=response.latency_seconds,
                error_category=None,
            )
        return response

    def prompt_shape_metrics(self) -> dict[str, int]:
        return {
            "conversation_prefix_shape_match_total": self._prefix_shape_match_total,
            "conversation_prefix_shape_split_total": self._prefix_shape_split_total,
        }

    def _observe_prompt_shape(self, request: ChatRequest) -> None:
        fingerprint = request.prompt_snapshot_fingerprint
        if not fingerprint:
            return
        observed = (request.conversation_prefix_hash, request.request_shape_hash)
        previous = self._prompt_shapes.get(fingerprint)
        if previous is not None:
            if previous == observed:
                self._prefix_shape_match_total += 1
            elif previous[0] == observed[0]:
                self._prefix_shape_split_total += 1
            self._prompt_shapes.move_to_end(fingerprint)
            return
        self._prompt_shapes[fingerprint] = observed
        if len(self._prompt_shapes) > 1024:
            self._prompt_shapes.popitem(last=False)

    async def _execute_provider(
        self,
        provider: ModelCompleter,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority,
    ) -> ChatResponse:
        if priority is ModelExecutionPriority.BEST_EFFORT_BACKGROUND:
            return await self._execute_background_provider(provider, request)
        if priority is ModelExecutionPriority.EXCLUSIVE:
            return await self._execute_exclusive_provider(provider, request)
        return await self._execute_foreground_provider(provider, request)

    def _cancel_best_effort_background(self) -> None:
        background = self._background_provider_task
        if background is not None and not background.done():
            background.cancel()

    async def _execute_foreground_provider(
        self,
        provider: ModelCompleter,
        request: ChatRequest,
    ) -> ChatResponse:
        waiting = False
        active = False
        async with self._priority_condition:
            self._foreground_waiting += 1
            waiting = True
            self._cancel_best_effort_background()
            self._priority_condition.notify_all()
            await self._priority_condition.wait_for(
                lambda: self._exclusive_active == 0 and self._exclusive_waiting == 0
            )
            self._foreground_waiting -= 1
            waiting = False
            self._foreground_active += 1
            active = True
            self._priority_condition.notify_all()
        try:
            return await self._complete_provider(provider, request)
        finally:
            async with self._priority_condition:
                if active:
                    self._foreground_active -= 1
                elif waiting:
                    self._foreground_waiting -= 1
                self._priority_condition.notify_all()

    async def _execute_exclusive_provider(
        self,
        provider: ModelCompleter,
        request: ChatRequest,
    ) -> ChatResponse:
        async with self._exclusive_slot:
            waiting = False
            active = False
            async with self._priority_condition:
                self._exclusive_waiting += 1
                waiting = True
                self._cancel_best_effort_background()
                self._priority_condition.notify_all()
                await self._priority_condition.wait_for(lambda: self._foreground_active == 0)
                self._exclusive_waiting -= 1
                waiting = False
                self._exclusive_active += 1
                active = True
                self._priority_condition.notify_all()
            try:
                return await self._complete_provider(provider, request)
            finally:
                async with self._priority_condition:
                    if active:
                        self._exclusive_active -= 1
                    elif waiting:
                        self._exclusive_waiting -= 1
                    self._priority_condition.notify_all()

    async def _execute_background_provider(
        self,
        provider: ModelCompleter,
        request: ChatRequest,
    ) -> ChatResponse:
        async with self._background_slot:
            async with self._priority_condition:
                await self._priority_condition.wait_for(
                    lambda: (
                        self._foreground_active == 0
                        and self._foreground_waiting == 0
                        and self._exclusive_active == 0
                        and self._exclusive_waiting == 0
                    )
                )
                provider_task = asyncio.create_task(
                    self._complete_provider(provider, request),
                    name="best-effort-model-provider",
                )
                self._background_provider_task = provider_task
            try:
                return await provider_task
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                raise BackgroundModelPreempted("background model request preempted") from exc
            finally:
                async with self._priority_condition:
                    if self._background_provider_task is provider_task:
                        self._background_provider_task = None
                    self._priority_condition.notify_all()

    async def _complete_provider(
        self,
        provider: ModelCompleter,
        request: ChatRequest,
    ) -> ChatResponse:
        if self._semaphore is None:
            return await provider.complete(request)
        async with self._semaphore:
            return await provider.complete(request)

    def profile_id(self, task: ModelTask) -> str:
        route, _profile = self._router.route(task)
        return route.profile_id

    def model_name(self, task: ModelTask) -> str:
        _route, profile = self._router.route(task)
        return profile.model

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        _route, profile = self._router.route(task)
        return profile.structured_output_mode

    def protocol(self, task: ModelTask) -> ModelProtocol:
        _route, profile = self._router.route(task)
        return profile.protocol

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]:
        _route, profile = self._router.route(task)
        return profile.capabilities

    async def close(self) -> None:
        async with self._priority_condition:
            background = self._background_provider_task
            if background is not None and not background.done():
                background.cancel()
        if background is not None:
            await asyncio.gather(background, return_exceptions=True)
        await self._pool.close()
