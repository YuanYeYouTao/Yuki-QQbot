"""Unified memory read entry (R2 §6).

The query kernel stays pure-read.  ``consumer`` is chosen by the backend
entry, never by the model.  Plugin/Admin reads are always side-effect free;
only ``AUTOMATIC_CONTEXT`` / ``AGENT_TOOL`` may later ``publish_exposure``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.memory.enums import MemoryContextMode, MemoryRecallPurpose, MemoryRetrievalMode
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryQueryIntent,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.receipt import MemoryRecallTurn
from qq_ai_bot.memory.runtime.errors import MemoryRuntimeError


class MemoryReadConsumer(StrEnum):
    """Who initiated a read.  Not a model-writable field."""

    AUTOMATIC_CONTEXT = "automatic_context"
    AGENT_TOOL = "agent_tool"
    PLUGIN = "plugin"
    ADMIN = "admin"


class ResolvedReadScope(BaseModel):
    """Host-resolved targets.  Models never submit raw QQ or group ids here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    targets: tuple[MemoryEntityTarget, ...]


class MemoryReadRequest(BaseModel):
    """One consumer-facing read.  Quantity lives here, not on the intent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    intent: MemoryQueryIntent | None = None
    requested_limit: int | None = Field(default=None, ge=1, le=100)
    resolved_scope: ResolvedReadScope


_EXPOSURE_CONSUMERS = frozenset(
    {MemoryReadConsumer.AUTOMATIC_CONTEXT, MemoryReadConsumer.AGENT_TOOL}
)


class MemoryQueryKernel(Protocol):
    """Read/exposure operations the query plane may call."""

    async def search(
        self,
        *,
        text: str,
        mode: MemoryRetrievalMode,
        targets: tuple[MemoryEntityTarget, ...],
        runtime: RuntimeConfigSnapshot,
        limit: int | None = None,
        intent: MemoryQueryIntent | None = None,
    ) -> MemoryRetrievalResult: ...

    async def mark_injected(
        self,
        result: MemoryRetrievalResult,
        fact_ids: tuple[int, ...],
    ) -> int: ...

    async def record_recall(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
        origin: str,
        intent: MemoryQueryIntent | None,
        result: MemoryRetrievalResult,
        injected_fact_ids: tuple[int, ...],
        runtime: RuntimeConfigSnapshot,
    ) -> MemoryRecallTurn | None: ...


def retrieval_mode_for_request(request: MemoryReadRequest) -> MemoryRetrievalMode:
    """Map structured intent mode onto the retriever's two retrieval modes."""

    if request.intent is not None and request.intent.mode is MemoryContextMode.OVERVIEW:
        return MemoryRetrievalMode.OVERVIEW
    return MemoryRetrievalMode.RELEVANT


def resolve_read_limit(
    consumer: MemoryReadConsumer,
    request: MemoryReadRequest,
    runtime: RuntimeConfigSnapshot,
) -> int:
    """Consumer budget.  Automatic never reads ``requested_limit``."""

    memory = runtime.memory
    if consumer is MemoryReadConsumer.AUTOMATIC_CONTEXT:
        purpose = (
            request.intent.purpose if request.intent is not None else MemoryRecallPurpose.BACKGROUND
        )
        if purpose is MemoryRecallPurpose.CONTINUATION:
            return memory.automatic_recall_continuation_limit
        return memory.automatic_recall_background_limit
    if request.requested_limit is not None:
        return request.requested_limit
    if consumer is MemoryReadConsumer.AGENT_TOOL:
        if request.intent is not None and request.intent.mode is MemoryContextMode.OVERVIEW:
            return memory.automatic_recall_overview_limit
        return memory.automatic_recall_focused_limit
    return memory.context_limit_per_entity


def apply_total_hit_limit(result: MemoryRetrievalResult, total_limit: int) -> MemoryRetrievalResult:
    """Cap unique facts while preserving retriever order."""

    selected: list[MemoryRetrievalHit] = []
    seen: set[int] = set()
    for hit in result.hits:
        if len(selected) >= total_limit:
            break
        if hit.fact.id in seen:
            continue
        selected.append(hit)
        seen.add(hit.fact.id)
    selected_ids = {hit.fact.id for hit in selected}
    blocks = tuple(
        block.model_copy(
            update={"hits": tuple(hit for hit in block.hits if hit.fact.id in selected_ids)}
        )
        for block in result.blocks
    )
    final_hits = tuple(hit for block in blocks for hit in block.hits)
    return result.model_copy(
        update={
            "blocks": blocks,
            "hits": final_hits,
            "selected_count": len(final_hits),
        }
    )


class MemoryQueryPlane:
    """Single turn-query entry.  Dream/Rebuild/Maintenance stay on domain ports."""

    def __init__(self, kernel: MemoryQueryKernel) -> None:
        self._kernel = kernel

    async def read(
        self,
        consumer: MemoryReadConsumer,
        request: MemoryReadRequest,
        *,
        runtime: RuntimeConfigSnapshot,
    ) -> MemoryRetrievalResult:
        """Pure retrieve.  Never writes receipts, activation, or injected flags."""

        intent = (
            None
            if consumer in {MemoryReadConsumer.PLUGIN, MemoryReadConsumer.ADMIN}
            else (request.intent)
        )
        result = await self._kernel.search(
            text=request.text,
            mode=retrieval_mode_for_request(request),
            targets=request.resolved_scope.targets,
            runtime=runtime,
            limit=resolve_read_limit(consumer, request, runtime),
            intent=intent,
        )
        if consumer is MemoryReadConsumer.PLUGIN or consumer is MemoryReadConsumer.ADMIN:
            return result
        return apply_total_hit_limit(result, resolve_read_limit(consumer, request, runtime))

    async def publish_exposure(
        self,
        consumer: MemoryReadConsumer,
        *,
        conversation_key: str,
        trigger_message_id: str,
        origin: str,
        intent: MemoryQueryIntent | None,
        result: MemoryRetrievalResult,
        injected_fact_ids: tuple[int, ...],
        runtime: RuntimeConfigSnapshot,
    ) -> MemoryRecallTurn | None:
        """Mark injected + write a receipt after payload entered a Main Agent request."""

        if consumer not in _EXPOSURE_CONSUMERS:
            raise MemoryRuntimeError(
                f"{consumer.value} reads are side-effect free and cannot publish exposure"
            )
        await self._kernel.mark_injected(result, injected_fact_ids)
        return await self._kernel.record_recall(
            conversation_key=conversation_key,
            trigger_message_id=trigger_message_id,
            origin=origin,
            intent=intent,
            result=result,
            injected_fact_ids=injected_fact_ids,
            runtime=runtime,
        )
