"""Memory Dream partition, mutation, provenance, and rollback contracts."""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor
from qq_ai_bot.memory.classifier import MemoryRelationClassifier
from qq_ai_bot.memory.dream.db_models import MemoryDreamOperationModel
from qq_ai_bot.memory.dream.models import (
    DreamOperationStatus,
    DreamOperationType,
    DreamPlanStatistics,
    DreamRunMode,
)
from qq_ai_bot.memory.dream.repository import DreamRepository, fact_signature
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryFactRelationType,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFact, MemoryFactCreate
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.resolution import MemoryResolutionPolicy
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryEvidenceModel,
    MemoryFactRelationModel,
    MemoryMutationReceiptModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository, PeopleRepository


def _services(
    database: Database,
) -> tuple[MemoryMutationService, MemoryFactService, EventLedgerRepository, DreamRepository]:
    settings = make_settings(database.url)
    facts = MemoryFactService(MemoryFactRepository(database))
    ledger = EventLedgerRepository(database)
    processor = MemoryClaimProcessor(
        settings=settings,
        facts=facts,
        candidate_resolver=MemoryConflictCandidateResolver(facts.repository),
        relation_classifier=cast(MemoryRelationClassifier, object()),
        resolution_policy=MemoryResolutionPolicy(),
    )
    return (
        MemoryMutationService(
            settings=settings,
            facts=facts,
            processor=processor,
            ledger=ledger,
        ),
        facts,
        ledger,
        DreamRepository(database),
    )


async def _fact_with_evidence(
    facts: MemoryFactService,
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    memory_key: str,
    content: str,
    source_type: MemorySourceType = MemorySourceType.AUTOMATIC,
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT,
) -> MemoryFact:
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content=content,
        group_id="3001",
    )
    return await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="1001",
            group_id="3001",
            kind=MemoryKind.FACT,
            memory_key=memory_key,
            category="profile",
            content=content,
            source_type=source_type,
            authority=authority,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id="1001",
            relation=(
                MemoryEvidenceRelation.EXPLICIT_COMMAND
                if source_type is MemorySourceType.EXPLICIT
                else MemoryEvidenceRelation.SELF_STATEMENT
            ),
            confidence=1.0,
            authority=authority,
            excerpt=content,
        ),
    )


@pytest.mark.asyncio
async def test_dream_merge_is_atomic_audited_and_reversible(database: Database) -> None:
    mutations, facts, ledger, dreams = _services(database)
    await PeopleRepository(database).observe(user_id="8000", nickname="Yuki")
    first = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-first",
        memory_key="drink:coffee",
        content="我平时喜欢喝美式咖啡",
    )
    second = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-second",
        memory_key="preference:americano",
        content="我喜欢不加糖的美式",
    )
    anchor = mutations.select_dream_anchor((first, second))
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=DreamPlanStatistics(
            eligible_facts=2,
            ready_facts=2,
            missing_embeddings=0,
            ambiguous_bot_facts=0,
            partitions=1,
            candidate_clusters=1,
            isolated_facts=0,
            estimated_model_calls=1,
        ),
        clusters=(("cluster", "partition", "8000", "fact", (first.id, second.id), "fp"),),
        snapshot_max_fact_id=max(first.id, second.id),
        actor_user_id="1001",
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    assert cluster is not None

    async with facts.repository.transaction() as session:
        source_rows: list[MemoryFact] = []
        for fact_id in (first.id, second.id):
            fact = await facts.repository.get_fact(fact_id, session=session)
            assert fact is not None
            source_rows.append(fact)
        sources = tuple(source_rows)
        operation = await dreams.create_operation(
            cluster_id=cluster.id,
            action_index=1,
            operation_type=DreamOperationType.MERGE,
            source_facts=sources,
            anchor_fact_id=anchor.id,
            session=session,
        )
        result = await mutations.mutate_dream(
            dream_operation_id=operation.id,
            operation_type=DreamOperationType.MERGE,
            source_facts=sources,
            anchor_fact_id=anchor.id,
            content=None,
            importance=None,
            bot_user_id="8000",
            run_public_id=run.public_id,
            session=session,
        )
        current_sources: dict[int, MemoryFact] = {}
        for fact_id in (first.id, second.id):
            current = await facts.repository.get_fact(fact_id, session=session)
            assert current is not None
            current_sources[fact_id] = current
        output = current_sources[anchor.id]
        await dreams.commit_operation(
            operation.id,
            output_fact_id=result.output_fact_id,
            added_evidence_ids=result.added_evidence_ids,
            added_relation_ids=result.added_relation_ids,
            result_signature=fact_signature(output),
            source_signatures={
                fact_id: fact_signature(current) for fact_id, current in current_sources.items()
            },
            session=session,
        )

    assert await dreams.reset_processing_after_restart() == 1
    recovered_run = await dreams.get_run(run.public_id)
    recovered_page = await dreams.run_page(run.public_id)
    assert recovered_run is not None and recovered_run.completed_clusters == 1
    assert recovered_page.clusters[0].status.value == "completed"
    assert recovered_page.clusters[0].operation_count == 1

    merged_source_id = first.id if anchor.id == second.id else second.id
    merged_source = await facts.get_fact(merged_source_id)
    merged_anchor = await facts.get_fact(anchor.id)
    assert merged_source is not None and merged_source.status is MemoryStatus.SUPERSEDED
    assert merged_anchor is not None and merged_anchor.status is MemoryStatus.ACTIVE
    assert len(await facts.list_evidence(anchor.id, limit=10)) == 2
    async with database.sessions() as session:
        receipt = await session.scalar(
            select(MemoryMutationReceiptModel).where(
                MemoryMutationReceiptModel.dream_operation_id == operation.id,
                MemoryMutationReceiptModel.reason_code == "memory_dream_merge",
            )
        )
        assert receipt is not None
        assert receipt.trigger_event_id is None
        assert receipt.trigger_source_type == "dream_operation"

    async with facts.repository.transaction() as session:
        affected = await mutations.rollback_dream_operation(
            public_id=operation.public_id,
            session=session,
        )
    assert set(affected) == {first.id, second.id}
    restored_source = await facts.get_fact(merged_source_id)
    restored_anchor = await facts.get_fact(anchor.id)
    assert restored_source is not None and restored_source.status is MemoryStatus.ACTIVE
    assert restored_anchor is not None and restored_anchor.status is MemoryStatus.ACTIVE
    assert len(await facts.list_evidence(anchor.id, limit=10)) == 1
    async with database.sessions() as session:
        rolled_back = await session.get(MemoryDreamOperationModel, operation.id)
        rollback_receipts = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryMutationReceiptModel)
                .where(
                    MemoryMutationReceiptModel.dream_operation_id == operation.id,
                    MemoryMutationReceiptModel.reason_code == "memory_dream_rollback",
                )
            )
            or 0
        )
        evidence_count = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryEvidenceModel)
                .where(MemoryEvidenceModel.fact_id.in_((first.id, second.id)))
            )
            or 0
        )
    assert rolled_back is not None
    assert rolled_back.status == DreamOperationStatus.ROLLED_BACK.value
    assert rollback_receipts == 1
    assert evidence_count == 2


@pytest.mark.asyncio
async def test_dream_never_modifies_an_explicit_anchor(database: Database) -> None:
    mutations, facts, ledger, _dreams = _services(database)
    explicit = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-explicit",
        memory_key="identity:explicit",
        content="这是用户明确要求长期保留的事实",
        source_type=MemorySourceType.EXPLICIT,
        authority=MemoryAuthority.EXPLICIT,
    )
    automatic = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-automatic",
        memory_key="identity:auto",
        content="这是自动提取的近似事实",
    )
    async with facts.repository.transaction() as session:
        with pytest.raises(ValueError, match="explicit memory anchor"):
            await mutations.mutate_dream(
                dream_operation_id=1,
                operation_type=DreamOperationType.SYNTHESIZE,
                source_facts=(explicit, automatic),
                anchor_fact_id=explicit.id,
                content="模型不得改写这个显式事实",
                importance=5,
                bot_user_id="8000",
                run_public_id="protected",
                session=session,
            )


@pytest.mark.asyncio
async def test_dream_resolution_records_conflict_provenance(database: Database) -> None:
    mutations, facts, ledger, dreams = _services(database)
    await PeopleRepository(database).observe(user_id="8000", nickname="Yuki")
    preferred = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-resolve-preferred",
        memory_key="location:current",
        content="我现在住在连江",
    )
    rejected = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-resolve-rejected",
        memory_key="location:old",
        content="我现在住在福州",
    )
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=DreamPlanStatistics(
            eligible_facts=2,
            ready_facts=2,
            missing_embeddings=0,
            ambiguous_bot_facts=0,
            partitions=1,
            candidate_clusters=1,
            isolated_facts=0,
            estimated_model_calls=1,
        ),
        clusters=(("resolve", "partition", "8000", "fact", (preferred.id, rejected.id), "fp"),),
        snapshot_max_fact_id=rejected.id,
        actor_user_id="1001",
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    assert cluster is not None
    async with facts.repository.transaction() as session:
        operation = await dreams.create_operation(
            cluster_id=cluster.id,
            action_index=1,
            operation_type=DreamOperationType.RESOLVE,
            source_facts=(preferred, rejected),
            anchor_fact_id=preferred.id,
            session=session,
        )
        result = await mutations.mutate_dream(
            dream_operation_id=operation.id,
            operation_type=DreamOperationType.RESOLVE,
            source_facts=(preferred, rejected),
            anchor_fact_id=preferred.id,
            content=None,
            importance=None,
            bot_user_id="8000",
            run_public_id=run.public_id,
            session=session,
        )
        current_preferred = await facts.repository.get_fact(preferred.id, session=session)
        current_rejected = await facts.repository.get_fact(rejected.id, session=session)
        assert current_preferred is not None and current_rejected is not None
        await dreams.commit_operation(
            operation.id,
            output_fact_id=result.output_fact_id,
            added_evidence_ids=result.added_evidence_ids,
            added_relation_ids=result.added_relation_ids,
            result_signature=fact_signature(current_preferred),
            source_signatures={
                preferred.id: fact_signature(current_preferred),
                rejected.id: fact_signature(current_rejected),
            },
            session=session,
        )

    resolved = await facts.get_fact(rejected.id)
    assert resolved is not None and resolved.status is MemoryStatus.INVALIDATED
    async with database.sessions() as session:
        relation = await session.scalar(
            select(MemoryFactRelationModel).where(
                MemoryFactRelationModel.source_fact_id == rejected.id,
                MemoryFactRelationModel.target_fact_id == preferred.id,
                MemoryFactRelationModel.relation_type == MemoryFactRelationType.CONTRADICTS.value,
            )
        )
    assert relation is not None


@pytest.mark.asyncio
async def test_dream_reserves_actual_model_calls_before_execution(database: Database) -> None:
    _mutations, _facts, _ledger, dreams = _services(database)
    run = await dreams.create_run(
        mode=DreamRunMode.INCREMENTAL,
        statistics=DreamPlanStatistics(
            eligible_facts=2,
            ready_facts=2,
            missing_embeddings=0,
            ambiguous_bot_facts=0,
            partitions=1,
            candidate_clusters=1,
            isolated_facts=0,
            estimated_model_calls=1,
        ),
        clusters=(("cluster-budget", "partition", "8000", "fact", (1, 2), "fp"),),
        snapshot_max_fact_id=2,
        actor_user_id=None,
        scheduled_slot="2026-08-13:05",
    )
    cluster = await dreams.claim_next_cluster(run.public_id)
    assert cluster is not None
    assert await dreams.reserve_model_call(
        run_public_id=run.public_id,
        cluster_id=cluster.id,
        maximum=1,
    )
    assert not await dreams.reserve_model_call(
        run_public_id=run.public_id,
        cluster_id=cluster.id,
        maximum=1,
    )
    refreshed = await dreams.get_run(run.public_id)
    page = await dreams.run_page(run.public_id)
    assert refreshed is not None and refreshed.model_calls == 1
    assert page.clusters[0].model_calls == 1


@pytest.mark.asyncio
async def test_first_enable_baselines_facts_even_before_embeddings_exist(
    database: Database,
) -> None:
    _mutations, facts, ledger, dreams = _services(database)
    fact = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-baseline-missing-vector",
        memory_key="baseline:fact",
        content="首次启用时这条事实还没有 embedding",
    )
    assert await dreams.initialize_baseline(((fact.id, fact_signature(fact)),))
    assert not await dreams.initialize_baseline(((fact.id, "changed"),))
    assert await dreams.checkpoint_map() == {fact.id: fact_signature(fact)}
