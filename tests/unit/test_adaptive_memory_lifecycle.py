"""Deterministic coverage for adaptive recall ranking, decay, and attribution."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.memory.activation import (
    MemoryActivationRepository,
    MemoryIntentRanker,
    effective_activation,
)
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.enums import (
    MemoryContextMode,
    MemoryKind,
    MemoryRecallPurpose,
    MemoryRetrievalMode,
    MemoryScopeType,
    MemorySourceType,
    MemorySubjectRole,
    MemoryTargetRole,
    MemoryTemporalConstraint,
    MemoryTemporalIntentMode,
)
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import (
    MemoryActivationState,
    MemoryEntityTarget,
    MemoryFactCreate,
    MemoryQuery,
    MemoryQueryIntent,
    MemoryRetrievalBlock,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
    MemoryTemporalIntent,
)
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.receipt import MemoryRecallRepository, MemoryUsageControl
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.people_repository import PeopleRepository


def _query(intent: MemoryQueryIntent | None = None) -> MemoryQuery:
    return MemoryQuery(
        text="",
        normalized_text="",
        mode=MemoryRetrievalMode.RELEVANT,
        targets=(),
        candidate_limit=20,
        limit_per_target=8,
        always_on_explicit_preference_limit=0,
        query_term_limit=12,
        intent=intent,
    )


async def _remember(
    database: Database,
    *,
    key: str,
    kind: MemoryKind = MemoryKind.FACT,
    importance: int = 3,
    confidence: float = 0.9,
    valid_from: datetime | None = None,
):
    return await MemoryFactService(MemoryFactRepository(database)).remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=kind,
            memory_key=key,
            category="profile",
            content=f"content {key}",
            importance=importance,
            confidence=confidence,
            source_type=MemorySourceType.AUTOMATIC,
            valid_from=valid_from,
        )
    )


@pytest.mark.asyncio
async def test_new_fact_creates_activation_in_same_write(database: Database) -> None:
    fact = await _remember(database, key="activation-created", kind=MemoryKind.PREFERENCE)
    state = (await MemoryActivationRepository(database).load((fact.id,))).get(fact.id)

    assert state is not None
    assert state.activation == pytest.approx(0.80)
    assert state.activation_updated_at == fact.created_at
    assert state.last_recalled_at is None
    assert state.recall_count == 0


@pytest.mark.asyncio
async def test_lazy_decay_and_half_life_multipliers(database: Database) -> None:
    fact = await _remember(database, key="normal-decay")
    now = datetime.now(UTC)
    state = MemoryActivationState(
        fact_id=fact.id,
        activation=0.8,
        activation_updated_at=now - timedelta(days=60),
    )
    assert effective_activation(state, fact, _query(), now=now) == pytest.approx(0.4)

    important = fact.model_copy(update={"importance": 4})
    important_state = state.model_copy(update={"activation_updated_at": now - timedelta(days=120)})
    assert effective_activation(important_state, important, _query(), now=now) == pytest.approx(0.4)

    low_quality = fact.model_copy(update={"confidence": 0.6})
    low_state = state.model_copy(update={"activation_updated_at": now - timedelta(days=30)})
    assert effective_activation(low_state, low_quality, _query(), now=now) == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_intent_rerank_uses_explicit_features_inside_target(database: Database) -> None:
    now = datetime.now(UTC)
    old = await _remember(
        database,
        key="tea",
        kind=MemoryKind.PREFERENCE,
        valid_from=now - timedelta(days=200),
    )
    recent = await _remember(
        database,
        key="coffee",
        kind=MemoryKind.PREFERENCE,
        valid_from=now - timedelta(days=5),
    )
    target = MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_PERSON,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1001",
        block_id="person:1001",
    )
    hits = (
        MemoryRetrievalHit(
            fact=old,
            target=target,
            rank=1,
            selection_reason="semantic_match",
        ),
        MemoryRetrievalHit(
            fact=recent,
            target=target,
            rank=2,
            selection_reason="semantic_match",
        ),
    )
    intent = MemoryQueryIntent(
        mode=MemoryContextMode.HYBRID,
        purpose=MemoryRecallPurpose.CONTINUATION,
        subjects=(MemorySubjectRole.CURRENT_PERSON,),
        entities=("coffee",),
        temporal=MemoryTemporalIntent(mode=MemoryTemporalIntentMode.RECENT),
        preferred_kinds=(MemoryKind.PREFERENCE,),
    )
    states = await MemoryActivationRepository(database).load((old.id, recent.id))
    ranked = MemoryIntentRanker().rerank(hits, query=_query(intent), states=states, now=now)

    assert ranked[0].fact.id == recent.id
    assert ranked[0].subject_score == 1
    assert ranked[0].entity_score == 1
    assert ranked[0].temporal_score > ranked[1].temporal_score
    assert ranked[0].kind_score == 1

    exact_ranked = MemoryIntentRanker().rerank(
        (
            hits[1].model_copy(update={"rank": 1}),
            hits[0].model_copy(update={"rank": 2, "selection_reason": "memory_key_exact"}),
        ),
        query=_query(intent),
        states=states,
        now=now,
    )
    assert exact_ranked[0].fact.id == old.id


@pytest.mark.asyncio
async def test_preferred_kind_is_soft_and_overview_trace_is_bounded(database: Database) -> None:
    facts = [
        await _remember(
            database,
            key=f"overview-{index}",
            kind=MemoryKind.PREFERENCE if index == 24 else MemoryKind.FACT,
        )
        for index in range(25)
    ]
    target = MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_PERSON,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1001",
        block_id="person:1001",
    )
    intent = MemoryQueryIntent(
        mode=MemoryContextMode.OVERVIEW,
        purpose=MemoryRecallPurpose.RECALL,
        preferred_kinds=(MemoryKind.PREFERENCE,),
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url), database=database
    ).snapshot(user_id="1001")
    query = MemoryQueryBuilder.for_targets(
        text="overview",
        mode=MemoryRetrievalMode.OVERVIEW,
        targets=(target,),
        runtime=runtime,
        intent=intent,
    ).model_copy(update={"limit_per_target": 8, "recall_trace_candidate_limit": 20})

    assert query.kinds == ()
    result = await MemoryRetriever(
        repository=MemoryFactRepository(database),
        lexical_index=SQLiteMemoryFTSIndex(database),
        activation_repository=MemoryActivationRepository(database),
    ).retrieve(query)

    assert len(result.hits) == 8
    assert len(result.trace_hits) == 20
    assert result.hits[0].fact.kind is MemoryKind.PREFERENCE
    unchanged = await MemoryFactRepository(database).get_fact(facts[0].id)
    assert unchanged is not None and unchanged.last_injected_at is None


def test_usage_control_accepts_refs_and_rejects_invalid_batches() -> None:
    metrics = MemoryLifecycleMetrics()
    valid = MemoryUsageControl(
        turn_id="turn", injected_fact_ids=(1, 2), enabled=True, metrics=metrics
    )
    valid.begin_batch(("report_memory_usage",))
    result = valid.apply('{"memory_refs":["M2"]}')
    assert '"ok":true' in result
    valid.finalize("正文依赖 M2")
    assert valid.used_fact_ids == (2,)

    unavailable = MemoryUsageControl(turn_id="turn", injected_fact_ids=(1,), enabled=True)
    unavailable.begin_batch(("report_memory_usage",))
    assert '"ok":false' in unavailable.apply('{"memory_refs":["M9"]}')
    unavailable.finalize("正文")
    assert unavailable.used_fact_ids == ()

    parallel = MemoryUsageControl(turn_id="turn", injected_fact_ids=(1,), enabled=True)
    parallel.begin_batch(("report_memory_usage", "get_memory_fact"))
    parallel.apply('{"memory_refs":["M1"]}')
    parallel.finalize("正文")
    assert parallel.used_fact_ids == ()

    nonfinal = MemoryUsageControl(turn_id="turn", injected_fact_ids=(1,), enabled=True)
    nonfinal.begin_batch(("report_memory_usage",))
    nonfinal.apply('{"memory_refs":["M1"]}')
    nonfinal.note_call("web_search")
    nonfinal.finalize("正文")
    assert nonfinal.used_fact_ids == ()

    missing = MemoryUsageControl(
        turn_id="turn", injected_fact_ids=(1,), enabled=True, metrics=metrics
    )
    missing.finalize("正文")

    snapshot = metrics.adaptive_snapshot()
    assert snapshot["memory_usage_report_valid_count"] == 1
    assert snapshot["memory_usage_report_missing_count"] == 1
    assert snapshot["memory_usage_report_extra_model_request_count"] == 1


@pytest.mark.asyncio
async def test_strict_temporal_range_excludes_outside_and_undated_facts(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    inside = await _remember(
        database,
        key="strict-inside",
        valid_from=now - timedelta(days=3),
    )
    await _remember(
        database,
        key="strict-outside",
        valid_from=now - timedelta(days=30),
    )
    await _remember(database, key="strict-undated")
    target = MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_PERSON,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1001",
        block_id="person:1001",
    )
    intent = MemoryQueryIntent(
        mode=MemoryContextMode.OVERVIEW,
        purpose=MemoryRecallPurpose.RECALL,
        temporal=MemoryTemporalIntent(
            mode=MemoryTemporalIntentMode.RANGE,
            constraint=MemoryTemporalConstraint.STRICT,
            start_at=now - timedelta(days=7),
            end_at=now,
        ),
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url), database=database
    ).snapshot(user_id="1001")
    query = MemoryQueryBuilder.for_targets(
        text="strict range",
        mode=MemoryRetrievalMode.OVERVIEW,
        targets=(target,),
        runtime=runtime,
        intent=intent,
    )

    result = await MemoryRetriever(
        repository=MemoryFactRepository(database),
        lexical_index=SQLiteMemoryFTSIndex(database),
        activation_repository=MemoryActivationRepository(database),
    ).retrieve(query)

    assert tuple(hit.fact.id for hit in result.hits) == (inside.id,)
    assert tuple(hit.fact.id for hit in result.trace_hits) == (inside.id,)


def test_strict_temporal_constraint_requires_range() -> None:
    with pytest.raises(ValueError, match="strict memory temporal constraint requires range"):
        MemoryTemporalIntent(
            mode=MemoryTemporalIntentMode.RECENT,
            constraint=MemoryTemporalConstraint.STRICT,
        )


@pytest.mark.asyncio
async def test_receipt_usage_reinforcement_is_idempotent(database: Database) -> None:
    fact = await _remember(database, key="reinforce")
    target = MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_PERSON,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1001",
        block_id="person:1001",
    )
    hit = MemoryRetrievalHit(
        fact=fact,
        target=target,
        rank=1,
        selection_reason="lexical_match",
        base_rank_score=1,
        rerank_score=0.8,
    )
    result = MemoryRetrievalResult(
        blocks=(MemoryRetrievalBlock(target=target, hits=(hit,)),),
        hits=(hit,),
        trace_hits=(hit,),
        candidate_count=1,
        selected_count=1,
        query_hash="0" * 64,
        mode=MemoryRetrievalMode.RELEVANT,
    )
    intent = MemoryQueryIntent(
        mode=MemoryContextMode.LEXICAL,
        purpose=MemoryRecallPurpose.RECALL,
    )
    receipts = MemoryRecallRepository(database)
    turn = await receipts.record_initial(
        conversation_key="private:1001",
        trigger_message_id="message-1",
        origin="user_message",
        intent=intent,
        result=result,
        injected_fact_ids=(fact.id,),
        retention_days=30,
    )
    facts = MemoryFactService(MemoryFactRepository(database))
    activation = MemoryActivationRepository(database)
    service = MemoryContextService(
        query_builder=MemoryQueryBuilder(MemoryTargetResolver(PeopleRepository(database))),
        retriever=MemoryRetriever(
            repository=facts.repository,
            lexical_index=SQLiteMemoryFTSIndex(database),
            activation_repository=activation,
        ),
        facts=facts,
        activation=activation,
        receipts=receipts,
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot(user_id="1001")
    assert await service.record_usage(turn.turn_id, (fact.id,)) == (fact.id,)
    first = await service.reinforce_usage(
        turn_id=turn.turn_id,
        fact_ids=(fact.id,),
        intent=intent,
        runtime=runtime,
    )
    second = await service.reinforce_usage(
        turn_id=turn.turn_id,
        fact_ids=(fact.id,),
        intent=intent,
        runtime=runtime,
    )
    state = (await activation.load((fact.id,)))[fact.id]

    assert first == (fact.id,)
    assert second == ()
    assert state.activation == pytest.approx(0.775, abs=0.001)
    assert state.recall_count == 1
    assert state.revision == 1


@pytest.mark.asyncio
async def test_concurrent_same_receipt_reinforces_once(database: Database) -> None:
    fact = await _remember(database, key="concurrent-reinforce")
    target = MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_PERSON,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1001",
        block_id="person:1001",
    )
    hit = MemoryRetrievalHit(
        fact=fact,
        target=target,
        rank=1,
        selection_reason="lexical_match",
        base_rank_score=1,
        rerank_score=0.8,
    )
    result = MemoryRetrievalResult(
        blocks=(MemoryRetrievalBlock(target=target, hits=(hit,)),),
        hits=(hit,),
        trace_hits=(hit,),
        candidate_count=1,
        selected_count=1,
        query_hash="1" * 64,
        mode=MemoryRetrievalMode.RELEVANT,
    )
    intent = MemoryQueryIntent(purpose=MemoryRecallPurpose.RECALL)
    receipts = MemoryRecallRepository(database)
    turn = await receipts.record_initial(
        conversation_key="private:1001",
        trigger_message_id="message-concurrent",
        origin="user_message",
        intent=intent,
        result=result,
        injected_fact_ids=(fact.id,),
        retention_days=30,
    )
    await receipts.mark_used(turn.turn_id, (fact.id,))
    activation = MemoryActivationRepository(database)
    facts = MemoryFactService(MemoryFactRepository(database))
    service = MemoryContextService(
        query_builder=MemoryQueryBuilder(MemoryTargetResolver(PeopleRepository(database))),
        retriever=MemoryRetriever(
            repository=facts.repository,
            lexical_index=SQLiteMemoryFTSIndex(database),
        ),
        facts=facts,
        activation=activation,
        receipts=receipts,
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url), database=database
    ).snapshot(user_id="1001")
    results = await asyncio.gather(
        *(
            service.reinforce_usage(
                turn_id=turn.turn_id,
                fact_ids=(fact.id,),
                intent=intent,
                runtime=runtime,
            )
            for _ in range(2)
        )
    )
    state = (await activation.load((fact.id,)))[fact.id]

    assert sum(len(item) for item in results) == 1
    assert state.recall_count == 1
