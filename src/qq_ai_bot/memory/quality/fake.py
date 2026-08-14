"""Deterministic quality-only model provider."""

from __future__ import annotations

import json
from collections import Counter

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.memory.embedding.models import (
    EmbeddingBatchResult,
    EmbeddingProviderCapabilities,
    EmbeddingProviderProfile,
)
from qq_ai_bot.memory.embedding.provider import EmbeddingProvider
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelExecutionPriority,
    ModelProtocol,
    ModelTask,
    StructuredOutputMode,
)


class QualityFakeModel(LLMProvider):
    """Return fixture claims keyed by the immutable primary-event content."""

    provider_id = "memory-quality-fake-model-v1"

    def __init__(self, outputs: dict[str, tuple[dict[str, object], ...]]) -> None:
        self._outputs = outputs
        self.extraction_requests = 0
        self.classification_requests = 0

    async def complete(self, request: ChatRequest) -> ChatResponse:
        payload = json.loads(request.messages[-1].content or "{}")
        if "primary_event" in payload:
            self.extraction_requests += 1
            content = str(payload["primary_event"]["content"])
            body: dict[str, object] = {"claims": self._outputs.get(content, ())}
        elif "events" in payload:
            self.extraction_requests += 1
            claims: list[dict[str, object]] = []
            for event in payload["events"]:
                content = str(event["content"])
                claims.extend(
                    {
                        "source_event_id": event["source_event_id"],
                        "claim": claim,
                    }
                    for claim in self._outputs.get(content, ())
                )
            body = {"claims": claims}
        else:
            self.classification_requests += 1
            body = {"relations": []}
        return ChatResponse(
            content=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            latency_seconds=0,
        )


class CountingModelExecutor:
    """Content-free request counter around an explicitly supplied real-model executor."""

    def __init__(self, delegate: ModelExecutor) -> None:
        self._delegate = delegate
        self._counts: Counter[ModelTask] = Counter()

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: ModelExecutionPriority = ModelExecutionPriority.FOREGROUND,
    ) -> ChatResponse:
        self._counts[task] += 1
        return await self._delegate.execute(task, request, priority=priority)

    def model_name(self, task: ModelTask) -> str:
        return self._delegate.model_name(task)

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        return self._delegate.structured_output_mode(task)

    def protocol(self, task: ModelTask) -> ModelProtocol:
        return self._delegate.protocol(task)

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]:
        return self._delegate.capabilities(task)

    def count(self, task: ModelTask) -> int:
        return int(self._counts[task])


class CountingEmbeddingProvider:
    """Count only request cardinality; never retain embedding inputs or vectors."""

    def __init__(self, delegate: EmbeddingProvider) -> None:
        self._delegate = delegate
        self.document_requests = 0
        self.query_requests = 0

    @property
    def profile(self) -> EmbeddingProviderProfile:
        return self._delegate.profile

    @property
    def capabilities(self) -> EmbeddingProviderCapabilities:
        return self._delegate.capabilities

    async def embed_documents(self, texts: tuple[str, ...]) -> EmbeddingBatchResult:
        self.document_requests += 1
        return await self._delegate.embed_documents(texts)

    async def embed_query(self, text: str) -> EmbeddingBatchResult:
        self.query_requests += 1
        return await self._delegate.embed_query(text)

    async def close(self) -> None:
        await self._delegate.close()
