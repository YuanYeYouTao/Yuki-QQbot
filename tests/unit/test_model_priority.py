"""Foreground model work preempts best-effort memory attribution."""

from __future__ import annotations

import asyncio

import pytest

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.model_runtime.executor import BackgroundModelPreempted, TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelExecutionPriority,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter


class PreemptibleProvider(LLMProvider):
    def __init__(self) -> None:
        self.background_started = asyncio.Event()
        self.background_cancelled = asyncio.Event()

    async def complete(self, request: ChatRequest) -> ChatResponse:
        content = request.messages[0].content
        if content == "background":
            self.background_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.background_cancelled.set()
        return ChatResponse(content="foreground", latency_seconds=0)


def _executor(provider: LLMProvider, *, max_concurrency: int = 1) -> TaskModelExecutor:
    profile = ModelProfile(
        id="test",
        provider="fake",
        protocol=ModelProtocol.CHAT_COMPLETIONS,
        model="fake",
        timeout_seconds=5,
        max_retries=0,
        default_temperature=0,
        default_max_output_tokens=128,
        thinking_enabled=False,
        structured_output_mode=StructuredOutputMode.FUNCTION_TOOL,
        capabilities=frozenset(ModelCapability),
    )
    routes = {
        task: ModelRoute(
            task=task,
            profile_id="test",
            required_capabilities=frozenset(),
        )
        for task in ModelTask
    }
    catalog = ModelProfileCatalog(profiles={"test": profile}, routes=routes)
    pool = ModelClientPool(injected_profiles={"test": provider})
    return TaskModelExecutor(
        router=ModelRouter(catalog),
        pool=pool,
        max_concurrency=max_concurrency,
    )


@pytest.mark.asyncio
async def test_foreground_preempts_running_background_provider() -> None:
    provider = PreemptibleProvider()
    executor = _executor(provider)
    background = asyncio.create_task(
        executor.execute(
            ModelTask.MEMORY_ATTRIBUTION,
            ChatRequest(messages=(ChatMessage(role="user", content="background"),)),
            priority=ModelExecutionPriority.BEST_EFFORT_BACKGROUND,
        )
    )
    await asyncio.wait_for(provider.background_started.wait(), timeout=1)

    foreground = await asyncio.wait_for(
        executor.execute(
            ModelTask.CHAT_AGENT,
            ChatRequest(messages=(ChatMessage(role="user", content="foreground"),)),
        ),
        timeout=1,
    )

    assert foreground.content == "foreground"
    with pytest.raises(BackgroundModelPreempted):
        await background
    assert provider.background_cancelled.is_set()
    await executor.close()


class SlowCancellationProvider(LLMProvider):
    def __init__(self) -> None:
        self.background_started = asyncio.Event()
        self.cancellation_started = asyncio.Event()
        self.release_cancellation = asyncio.Event()

    async def complete(self, request: ChatRequest) -> ChatResponse:
        if request.messages[0].content == "background":
            self.background_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancellation_started.set()
                await self.release_cancellation.wait()
        return ChatResponse(content="foreground", latency_seconds=0)


@pytest.mark.asyncio
async def test_foreground_does_not_wait_for_background_cleanup_when_capacity_exists() -> None:
    provider = SlowCancellationProvider()
    executor = _executor(provider, max_concurrency=2)
    background = asyncio.create_task(
        executor.execute(
            ModelTask.MEMORY_ATTRIBUTION,
            ChatRequest(messages=(ChatMessage(role="user", content="background"),)),
            priority=ModelExecutionPriority.BEST_EFFORT_BACKGROUND,
        )
    )
    await asyncio.wait_for(provider.background_started.wait(), timeout=1)

    foreground = await asyncio.wait_for(
        executor.execute(
            ModelTask.CHAT_AGENT,
            ChatRequest(messages=(ChatMessage(role="user", content="foreground"),)),
        ),
        timeout=0.2,
    )

    assert foreground.content == "foreground"
    await asyncio.wait_for(provider.cancellation_started.wait(), timeout=1)
    assert not background.done()
    provider.release_cancellation.set()
    with pytest.raises(BackgroundModelPreempted):
        await background
    await executor.close()


class ExclusiveGateProvider(LLMProvider):
    def __init__(self) -> None:
        self.exclusive_started = asyncio.Event()
        self.exclusive_release = asyncio.Event()
        self.exclusive_cancelled = asyncio.Event()
        self.order: list[str] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        content = request.messages[0].content or ""
        if content == "exclusive":
            self.exclusive_started.set()
            try:
                await self.exclusive_release.wait()
            except asyncio.CancelledError:
                self.exclusive_cancelled.set()
                raise
            self.order.append("exclusive")
            return ChatResponse(content="exclusive", latency_seconds=0)
        self.order.append("foreground")
        return ChatResponse(content="foreground", latency_seconds=0)


class OccupiedForegroundProvider(LLMProvider):
    def __init__(self) -> None:
        self.foreground_started = asyncio.Event()
        self.foreground_release = asyncio.Event()

    async def complete(self, request: ChatRequest) -> ChatResponse:
        content = request.messages[0].content or ""
        if content == "foreground":
            self.foreground_started.set()
            await self.foreground_release.wait()
            return ChatResponse(content="foreground", latency_seconds=0)
        return ChatResponse(content="exclusive", latency_seconds=0)


@pytest.mark.asyncio
async def test_exclusive_blocks_later_foreground_without_preemption() -> None:
    provider = ExclusiveGateProvider()
    executor = _executor(provider)
    exclusive = asyncio.create_task(
        executor.execute(
            ModelTask.CONVERSATION_COMPACTION,
            ChatRequest(messages=(ChatMessage(role="user", content="exclusive"),)),
            priority=ModelExecutionPriority.EXCLUSIVE,
        )
    )
    await asyncio.wait_for(provider.exclusive_started.wait(), timeout=1)
    foreground = asyncio.create_task(
        executor.execute(
            ModelTask.CHAT_AGENT,
            ChatRequest(messages=(ChatMessage(role="user", content="foreground"),)),
        )
    )
    await asyncio.sleep(0.05)
    assert not foreground.done()
    assert not exclusive.done()
    provider.exclusive_release.set()
    exclusive_response = await asyncio.wait_for(exclusive, timeout=1)
    foreground_response = await asyncio.wait_for(foreground, timeout=1)
    assert exclusive_response.content == "exclusive"
    assert foreground_response.content == "foreground"
    assert not provider.exclusive_cancelled.is_set()
    assert provider.order == ["exclusive", "foreground"]
    await executor.close()


@pytest.mark.asyncio
async def test_exclusive_waits_for_in_flight_foreground() -> None:
    provider = OccupiedForegroundProvider()
    executor = _executor(provider)
    foreground = asyncio.create_task(
        executor.execute(
            ModelTask.CHAT_AGENT,
            ChatRequest(messages=(ChatMessage(role="user", content="foreground"),)),
        )
    )
    await asyncio.wait_for(provider.foreground_started.wait(), timeout=1)
    exclusive = asyncio.create_task(
        executor.execute(
            ModelTask.CONVERSATION_COMPACTION,
            ChatRequest(messages=(ChatMessage(role="user", content="exclusive"),)),
            priority=ModelExecutionPriority.EXCLUSIVE,
        )
    )
    await asyncio.sleep(0.05)
    assert not exclusive.done()
    provider.foreground_release.set()
    foreground_response = await asyncio.wait_for(foreground, timeout=1)
    exclusive_response = await asyncio.wait_for(exclusive, timeout=1)
    assert foreground_response.content == "foreground"
    assert exclusive_response.content == "exclusive"
    await executor.close()


@pytest.mark.asyncio
async def test_exclusive_preempts_best_effort_background() -> None:
    provider = PreemptibleProvider()
    executor = _executor(provider)
    background = asyncio.create_task(
        executor.execute(
            ModelTask.MEMORY_ATTRIBUTION,
            ChatRequest(messages=(ChatMessage(role="user", content="background"),)),
            priority=ModelExecutionPriority.BEST_EFFORT_BACKGROUND,
        )
    )
    await asyncio.wait_for(provider.background_started.wait(), timeout=1)
    exclusive = await asyncio.wait_for(
        executor.execute(
            ModelTask.CONVERSATION_COMPACTION,
            ChatRequest(messages=(ChatMessage(role="user", content="exclusive"),)),
            priority=ModelExecutionPriority.EXCLUSIVE,
        ),
        timeout=1,
    )
    assert exclusive.content == "foreground"
    with pytest.raises(BackgroundModelPreempted):
        await background
    assert provider.background_cancelled.is_set()
    await executor.close()
