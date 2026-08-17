"""Unit tests for the unified memory query plane (R2 commit 3)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from qq_ai_bot.admin.models import MemoryRetrievalRuntimeConfig
from qq_ai_bot.memory.enums import (
    MemoryContextMode,
    MemoryRecallPurpose,
    MemoryScopeType,
    MemoryTargetRole,
)
from qq_ai_bot.memory.models import MemoryEntityTarget, MemoryQueryIntent
from qq_ai_bot.memory.runtime.errors import MemoryRuntimeError
from qq_ai_bot.memory.runtime.query_plane import (
    MemoryQueryPlane,
    MemoryReadConsumer,
    MemoryReadRequest,
    ResolvedReadScope,
    resolve_read_limit,
)


def _memory_config() -> MemoryRetrievalRuntimeConfig:
    return MemoryRetrievalRuntimeConfig(
        retrieval_enabled=True,
        max_referenced_targets=3,
        lexical_candidate_limit=40,
        context_limit_per_entity=20,
        overview_limit_per_entity=8,
        always_on_explicit_preference_limit=1,
        query_term_limit=8,
        short_query_fallback_enabled=True,
    )


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(memory=_memory_config())


def _request(**overrides: object) -> MemoryReadRequest:
    values: dict[str, object] = {
        "text": "coffee",
        "resolved_scope": ResolvedReadScope(
            targets=(
                MemoryEntityTarget(
                    role=MemoryTargetRole.CURRENT_PERSON,
                    scope_type=MemoryScopeType.PERSON,
                    subject_user_id="u-1",
                    block_id="current_person",
                ),
            )
        ),
    }
    values.update(overrides)
    return MemoryReadRequest(**values)  # type: ignore[arg-type]


class TestIntentNoLongerCarriesQuantity:
    def test_requested_count_is_rejected_on_intent(self) -> None:
        with pytest.raises(ValidationError, match="requested_count"):
            MemoryQueryIntent(requested_count=2)  # type: ignore[call-arg]


class TestConsumerBudgets:
    def test_automatic_ignores_requested_limit(self) -> None:
        runtime = _runtime()
        background = resolve_read_limit(
            MemoryReadConsumer.AUTOMATIC_CONTEXT,
            _request(requested_limit=20),
            runtime,  # type: ignore[arg-type]
        )
        continuation = resolve_read_limit(
            MemoryReadConsumer.AUTOMATIC_CONTEXT,
            _request(
                requested_limit=20,
                intent=MemoryQueryIntent(purpose=MemoryRecallPurpose.CONTINUATION),
            ),
            runtime,  # type: ignore[arg-type]
        )
        assert background == 3
        assert continuation == 4

    def test_agent_tool_defaults_to_focused_or_overview(self) -> None:
        runtime = _runtime()
        relevant = resolve_read_limit(
            MemoryReadConsumer.AGENT_TOOL,
            _request(),
            runtime,  # type: ignore[arg-type]
        )
        overview = resolve_read_limit(
            MemoryReadConsumer.AGENT_TOOL,
            _request(intent=MemoryQueryIntent(mode=MemoryContextMode.OVERVIEW)),
            runtime,  # type: ignore[arg-type]
        )
        explicit = resolve_read_limit(
            MemoryReadConsumer.AGENT_TOOL,
            _request(requested_limit=12),
            runtime,  # type: ignore[arg-type]
        )
        assert relevant == 6
        assert overview == 8
        assert explicit == 12

    def test_plugin_and_admin_use_requested_or_entity_limit(self) -> None:
        runtime = _runtime()
        plugin = resolve_read_limit(
            MemoryReadConsumer.PLUGIN,
            _request(),
            runtime,  # type: ignore[arg-type]
        )
        admin = resolve_read_limit(
            MemoryReadConsumer.ADMIN,
            _request(requested_limit=7),
            runtime,  # type: ignore[arg-type]
        )
        assert plugin == 20
        assert admin == 7


class TestNeutralPluginAdminReads:
    @pytest.mark.asyncio
    async def test_plugin_and_admin_drop_intent_before_search(self) -> None:
        kernel = _CapturingKernel()
        plane = MemoryQueryPlane(kernel)
        request = _request(
            intent=MemoryQueryIntent(
                mode=MemoryContextMode.OVERVIEW,
                purpose=MemoryRecallPurpose.RECALL,
                entities=("coffee",),
            ),
            requested_limit=5,
        )
        for consumer in (MemoryReadConsumer.PLUGIN, MemoryReadConsumer.ADMIN):
            kernel.last_intent = "unset"
            await plane.read(consumer, request, runtime=_runtime())  # type: ignore[arg-type]
            assert kernel.last_intent is None
            assert kernel.last_limit == 5

    @pytest.mark.asyncio
    async def test_agent_tool_keeps_intent(self) -> None:
        kernel = _CapturingKernel()
        plane = MemoryQueryPlane(kernel)
        intent = MemoryQueryIntent(mode=MemoryContextMode.OVERVIEW)
        await plane.read(
            MemoryReadConsumer.AGENT_TOOL,
            _request(intent=intent, requested_limit=8),
            runtime=_runtime(),  # type: ignore[arg-type]
        )
        assert kernel.last_intent is intent
        assert kernel.last_limit == 8


class TestPublishExposureGuard:
    @pytest.mark.asyncio
    async def test_plugin_and_admin_cannot_publish_exposure(self) -> None:
        plane = MemoryQueryPlane(_ForbiddenKernel())
        for consumer in (MemoryReadConsumer.PLUGIN, MemoryReadConsumer.ADMIN):
            with pytest.raises(MemoryRuntimeError, match="side-effect free"):
                await plane.publish_exposure(
                    consumer,
                    conversation_key="private:u-1",
                    trigger_message_id="m-1",
                    origin="user_message",
                    intent=None,
                    result=_empty_result(),
                    injected_fact_ids=(),
                    runtime=_runtime(),  # type: ignore[arg-type]
                )


class _CapturingKernel:
    def __init__(self) -> None:
        self.last_intent: object = "unset"
        self.last_limit: int | None = None

    async def search(self, **kwargs: object) -> object:
        self.last_intent = kwargs.get("intent")
        limit = kwargs.get("limit")
        self.last_limit = limit if isinstance(limit, int) else None
        return _empty_result()

    async def mark_injected(self, *args: object, **kwargs: object) -> int:
        raise AssertionError("read() must stay side-effect free")

    async def record_recall(self, **kwargs: object) -> object:
        raise AssertionError("read() must stay side-effect free")


class _ForbiddenKernel:
    async def search(self, **kwargs: object) -> object:
        raise AssertionError("search should not run")

    async def mark_injected(self, *args: object, **kwargs: object) -> int:
        raise AssertionError("plugin/admin must not mark injected")

    async def record_recall(self, **kwargs: object) -> object:
        raise AssertionError("plugin/admin must not write receipts")


def _empty_result() -> object:
    from qq_ai_bot.memory.enums import MemoryRetrievalMode
    from qq_ai_bot.memory.models import MemoryRetrievalResult

    return MemoryRetrievalResult(
        blocks=(),
        hits=(),
        candidate_count=0,
        selected_count=0,
        query_hash="0" * 64,
        mode=MemoryRetrievalMode.RELEVANT,
    )
