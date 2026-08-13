"""Memory Dream partition, mutation, provenance, and rollback contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import cos, radians, sin
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor
from qq_ai_bot.memory.classifier import MemoryRelationClassifier
from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamOperationModel,
    MemoryDreamOperationResultModel,
)
from qq_ai_bot.memory.dream.models import (
    DreamAction,
    DreamInput,
    DreamMemoryInput,
    DreamOperationStatus,
    DreamOperationType,
    DreamOutput,
    DreamPlanStatistics,
    DreamRecomposeOutput,
    DreamRunMode,
    DreamRunStatus,
)
from qq_ai_bot.memory.dream.repository import (
    DreamCandidate,
    DreamCandidateLoad,
    DreamRepository,
    fact_signature,
)
from qq_ai_bot.memory.dream.service import DreamService
from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.models import EmbeddingVector
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryFactRelationType,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.memory.models import (
    MemoryEvidence,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactCreate,
)
from qq_ai_bot.memory.mutation.service import DreamRecomposePlan, MemoryMutationService
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


def test_dream_selects_only_first_and_last_evidence_when_limit_is_two() -> None:
    service = object.__new__(DreamService)
    service._settings = cast(
        object,
        SimpleNamespace(memory_dream_evidence_per_fact=2),
    )
    now = datetime.now(UTC)
    rows = tuple(
        SimpleNamespace(id=index, created_at=now + timedelta(seconds=index)) for index in range(5)
    )

    selected = service._select_evidence(cast(tuple[MemoryEvidence, ...], rows))

    assert [item.id for item in selected] == [0, 4]  # type: ignore[attr-defined]


def test_episode_recompose_can_split_one_source_into_multiple_outputs() -> None:
    action = DreamAction(
        operation=DreamOperationType.RECOMPOSE,
        source_refs=("memory_1",),
        outputs=(
            DreamRecomposeOutput(
                focus="第一次独立经历",
                source_refs=("memory_1",),
                content="第一次独立经历的压缩回忆。",
                importance=3,
            ),
            DreamRecomposeOutput(
                focus="第二次独立经历",
                source_refs=("memory_1",),
                content="第二次独立经历的压缩回忆。",
                importance=4,
            ),
        ),
    )

    assert len(action.outputs) == 2


def test_dream_keep_can_explicitly_preserve_several_independent_sources() -> None:
    service = object.__new__(DreamService)
    service._settings = cast(
        object,
        SimpleNamespace(
            memory_dream_episode_max_characters=800,
            memory_dream_episode_compression_ratio=0.45,
            memory_dream_episode_hard_compression_ratio=0.70,
        ),
    )
    memories = tuple(
        DreamMemoryInput(
            ref=f"memory_{index}",
            kind="episode",
            category="self_episode",
            memory_key=f"episode:keep:{index}",
            content=f"第 {index} 件已经清楚而独立的经历",
            importance=3,
            confidence=1.0,
            source_type="automatic",
            authority="agent_reflection",
            status="active",
            conflict_state="clear",
        )
        for index in range(1, 6)
    )
    output = DreamOutput(
        actions=(
            DreamAction(
                operation=DreamOperationType.KEEP,
                source_refs=tuple(memory.ref for memory in memories),
            ),
        )
    )

    service._validate_output(
        DreamInput(scope_type="self", kind="episode", memories=memories),
        output,
    )


def test_episode_recompose_rejects_an_uncompressed_long_output() -> None:
    service = object.__new__(DreamService)
    service._settings = cast(
        object,
        SimpleNamespace(
            memory_dream_episode_max_characters=800,
            memory_dream_episode_compression_ratio=0.45,
            memory_dream_episode_hard_compression_ratio=0.70,
        ),
    )
    payload = DreamInput(
        scope_type="self",
        kind="episode",
        memories=(
            DreamMemoryInput(
                ref="memory_1",
                kind="episode",
                category="self_episode",
                memory_key="episode:long",
                content="甲" * 1000,
                importance=3,
                confidence=1.0,
                source_type="automatic",
                authority="agent_reflection",
                status="active",
                conflict_state="clear",
            ),
        ),
    )
    output = DreamOutput(
        actions=(
            DreamAction(
                operation=DreamOperationType.RECOMPOSE,
                source_refs=("memory_1",),
                outputs=(
                    DreamRecomposeOutput(
                        focus="未充分压缩的经历",
                        source_refs=("memory_1",),
                        content="乙" * 750,
                        importance=3,
                    ),
                ),
            ),
        )
    )

    with pytest.raises(ValueError, match="did not compress"):
        service._validate_output(payload, output)


def test_episode_recompose_enforces_cluster_wide_output_and_compression_limits() -> None:
    with pytest.raises(ValueError, match="at most four"):
        DreamOutput(
            actions=tuple(
                DreamAction(
                    operation=DreamOperationType.RECOMPOSE,
                    source_refs=(f"memory_{index}",),
                    outputs=(
                        DreamRecomposeOutput(
                            focus=f"经历 {index}",
                            source_refs=(f"memory_{index}",),
                            content="简短经历",
                            importance=3,
                        ),
                    ),
                )
                for index in range(1, 6)
            )
        )

    with pytest.raises(ValueError, match="at most one recompose"):
        DreamOutput(
            actions=(
                DreamAction(
                    operation=DreamOperationType.RECOMPOSE,
                    source_refs=("memory_1",),
                    outputs=(
                        DreamRecomposeOutput(
                            focus="第一件事",
                            source_refs=("memory_1",),
                            content="第一件独立经历",
                            importance=3,
                        ),
                    ),
                ),
                DreamAction(
                    operation=DreamOperationType.RECOMPOSE,
                    source_refs=("memory_2",),
                    outputs=(
                        DreamRecomposeOutput(
                            focus="第二件事",
                            source_refs=("memory_2",),
                            content="第二件独立经历",
                            importance=3,
                        ),
                    ),
                ),
            )
        )

    service = object.__new__(DreamService)
    service._settings = cast(
        object,
        SimpleNamespace(
            memory_dream_episode_max_characters=800,
            memory_dream_episode_compression_ratio=0.45,
            memory_dream_episode_hard_compression_ratio=0.70,
        ),
    )
    memories = tuple(
        DreamMemoryInput(
            ref=f"memory_{index}",
            kind="episode",
            category="self_episode",
            memory_key=f"episode:{index}",
            content="甲" * 500,
            importance=3,
            confidence=1.0,
            source_type="automatic",
            authority="agent_reflection",
            status="active",
            conflict_state="clear",
        )
        for index in range(1, 3)
    )
    payload = DreamInput(scope_type="self", kind="episode", memories=memories)
    output = DreamOutput(
        actions=(
            DreamAction(
                operation=DreamOperationType.RECOMPOSE,
                source_refs=tuple(memory.ref for memory in memories),
                outputs=tuple(
                    DreamRecomposeOutput(
                        focus=f"独立经历 {index}",
                        source_refs=(memory.ref,),
                        content=f"{index}" + "乙" * 299,
                        importance=3,
                    )
                    for index, memory in enumerate(memories, start=1)
                ),
            ),
        )
    )
    service._validate_output(payload, output)
    with pytest.raises(ValueError, match="did not compress"):
        service._validate_compression_target(payload, output)


@pytest.mark.asyncio
async def test_episode_decision_keeps_first_hard_valid_proposal_when_repair_fails() -> None:
    service = object.__new__(DreamService)
    service._settings = cast(
        object,
        SimpleNamespace(
            memory_dream_episode_max_characters=800,
            memory_dream_episode_compression_ratio=0.45,
            memory_dream_episode_hard_compression_ratio=0.70,
            memory_dream_max_output_tokens=2400,
            bot_display_name="Yuki",
            bot_persona="测试人格",
        ),
    )
    payload = DreamInput(
        scope_type="self",
        kind="episode",
        memories=(
            DreamMemoryInput(
                ref="memory_1",
                kind="episode",
                category="self_episode",
                memory_key="episode:fallback",
                content="甲" * 1000,
                importance=3,
                confidence=1.0,
                source_type="automatic",
                authority="agent_reflection",
                status="active",
                conflict_state="clear",
            ),
        ),
    )
    first = DreamOutput(
        actions=(
            DreamAction(
                operation=DreamOperationType.RECOMPOSE,
                source_refs=("memory_1",),
                outputs=(
                    DreamRecomposeOutput(
                        focus="第一次合法但未达到目标的整理",
                        source_refs=("memory_1",),
                        content="乙" * 600,
                        importance=3,
                    ),
                ),
            ),
        )
    )
    repaired = first.model_copy(
        update={
            "actions": (
                first.actions[0].model_copy(
                    update={
                        "outputs": (
                            first.actions[0].outputs[0].model_copy(
                                update={"content": "丙" * 750}
                            ),
                        )
                    }
                ),
            )
        }
    )
    service._run_model = AsyncMock(side_effect=(first, repaired))  # type: ignore[method-assign]
    service._reserve_model_call = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result, calls = await service._decide(
        payload,
        self_memory=False,
        run=cast(object, SimpleNamespace(public_id="run-1")),  # type: ignore[arg-type]
        cluster=cast(object, SimpleNamespace(id=1)),  # type: ignore[arg-type]
    )

    assert result == first
    assert calls == 2


@pytest.mark.asyncio
async def test_episode_decision_prefers_shorter_hard_valid_repair() -> None:
    service = object.__new__(DreamService)
    service._settings = cast(
        object,
        SimpleNamespace(
            memory_dream_episode_max_characters=800,
            memory_dream_episode_compression_ratio=0.45,
            memory_dream_episode_hard_compression_ratio=0.70,
            memory_dream_max_output_tokens=2400,
            bot_display_name="Yuki",
            bot_persona="测试人格",
        ),
    )
    payload = DreamInput(
        scope_type="self",
        kind="episode",
        memories=(
            DreamMemoryInput(
                ref="memory_1",
                kind="episode",
                category="self_episode",
                memory_key="episode:shorter",
                content="甲" * 1000,
                importance=3,
                confidence=1.0,
                source_type="automatic",
                authority="agent_reflection",
                status="active",
                conflict_state="clear",
            ),
        ),
    )

    def proposal(character: str, length: int) -> DreamOutput:
        return DreamOutput(
            actions=(
                DreamAction(
                    operation=DreamOperationType.RECOMPOSE,
                    source_refs=("memory_1",),
                    outputs=(
                        DreamRecomposeOutput(
                            focus="压缩后的单一经历",
                            source_refs=("memory_1",),
                            content=character * length,
                            importance=3,
                        ),
                    ),
                ),
            )
        )

    first = proposal("乙", 600)
    repaired = proposal("丙", 500)
    service._run_model = AsyncMock(side_effect=(first, repaired))  # type: ignore[method-assign]
    service._reserve_model_call = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result, calls = await service._decide(
        payload,
        self_memory=False,
        run=cast(object, SimpleNamespace(public_id="run-2")),  # type: ignore[arg-type]
        cluster=cast(object, SimpleNamespace(id=2)),  # type: ignore[arg-type]
    )

    assert result == repaired
    assert calls == 2


async def _fact_with_evidence(
    facts: MemoryFactService,
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    memory_key: str,
    content: str,
    source_type: MemorySourceType = MemorySourceType.AUTOMATIC,
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT,
    kind: MemoryKind = MemoryKind.FACT,
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
            kind=kind,
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
async def test_dream_clustering_does_not_bridge_unrelated_endpoints(
    database: Database,
) -> None:
    _mutations, facts, ledger, dreams = _services(database)
    rows = []
    for index in range(3):
        rows.append(
            await _fact_with_evidence(
                facts,
                ledger,
                message_id=f"dream-chain-{index}",
                memory_key=f"episode:chain:{index}",
                content=f"独立事件 {index}",
                kind=MemoryKind.EPISODE,
            )
        )
    angles = (0, 40, 80)
    candidates = tuple(
        DreamCandidate(
            fact=fact,
            bot_user_id="8000",
            vector=EmbeddingVector(
                values=(cos(radians(angle)), sin(radians(angle))),
                dimensions=2,
            ),
            signature=fact_signature(fact),
        )
        for fact, angle in zip(rows, angles, strict=True)
    )
    service = object.__new__(DreamService)
    service._settings = cast(
        object,
        SimpleNamespace(
            memory_dream_similarity_threshold=0.70,
            memory_dream_max_cluster_size=6,
            memory_dream_episode_max_characters=800,
        ),
    )
    service._repository = dreams
    service._codec = Float32VectorCodec()

    clusters, _isolated = await service._clusters(
        DreamCandidateLoad(
            candidates=candidates,
            fact_signatures=tuple((item.fact.id, item.signature) for item in candidates),
            eligible_facts=3,
            missing_embeddings=0,
            ambiguous_bot_facts=0,
        ),
        incremental=False,
    )

    assert len(clusters) == 1
    assert len(clusters[0]) == 2
    assert {item.fact.id for item in clusters[0]} != {item.id for item in rows}


@pytest.mark.asyncio
async def test_cancelled_dream_run_can_enter_rollback(database: Database) -> None:
    dreams = DreamRepository(database)
    await PeopleRepository(database).observe(user_id="1001", nickname="owner")
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=DreamPlanStatistics(
            eligible_facts=0,
            ready_facts=0,
            missing_embeddings=0,
            ambiguous_bot_facts=0,
            partitions=0,
            candidate_clusters=0,
            isolated_facts=0,
            estimated_model_calls=0,
        ),
        clusters=(),
        snapshot_max_fact_id=0,
        actor_user_id="1001",
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    assert await dreams.cancel(run.public_id)
    assert await dreams.mark_run_rolling_back(run.public_id)
    current = await dreams.get_run(run.public_id)
    assert current is not None
    assert current.status is DreamRunStatus.ROLLING_BACK


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
            output_results=((output.id, fact_signature(output)),),
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
async def test_episode_recompose_is_atomic_one_to_many_and_reversible(
    database: Database,
) -> None:
    mutations, facts, ledger, dreams = _services(database)
    await PeopleRepository(database).observe(user_id="8000", nickname="Yuki")
    source = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-recompose-source",
        memory_key="episode:mixed-day",
        content="上午处理点单失败；晚上和群友讨论音乐。" * 30,
        kind=MemoryKind.EPISODE,
    )
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=DreamPlanStatistics(
            eligible_facts=1,
            ready_facts=1,
            missing_embeddings=0,
            ambiguous_bot_facts=0,
            partitions=1,
            candidate_clusters=1,
            isolated_facts=0,
            estimated_model_calls=1,
        ),
        clusters=(("recompose", "partition", "8000", "episode", (source.id,), "fp"),),
        snapshot_max_fact_id=source.id,
        actor_user_id="1001",
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    assert cluster is not None

    async with facts.repository.transaction() as session:
        current_source = await facts.repository.get_fact(source.id, session=session)
        assert current_source is not None
        operation = await dreams.create_operation(
            cluster_id=cluster.id,
            action_index=1,
            operation_type=DreamOperationType.RECOMPOSE,
            source_facts=(current_source,),
            anchor_fact_id=current_source.id,
            session=session,
        )
        result = await mutations.mutate_dream(
            dream_operation_id=operation.id,
            operation_type=DreamOperationType.RECOMPOSE,
            source_facts=(current_source,),
            anchor_fact_id=current_source.id,
            content=None,
            importance=None,
            recompose_outputs=(
                DreamRecomposePlan(
                    source_facts=(current_source,),
                    content="一次点单工具链失败后，我确认需要减少无意义的中间步骤。",
                    importance=3,
                ),
                DreamRecomposePlan(
                    source_facts=(current_source,),
                    content="晚上和群友集中聊了音乐，留下了几次有趣的推荐和争论。",
                    importance=3,
                ),
            ),
            bot_user_id="8000",
            run_public_id=run.public_id,
            session=session,
        )
        assert len(result.output_fact_ids) == 2
        loaded_outputs: list[MemoryFact] = []
        for fact_id in result.output_fact_ids:
            item = await facts.repository.get_fact(fact_id, session=session)
            if item is not None:
                loaded_outputs.append(item)
        outputs = tuple(loaded_outputs)
        assert len(outputs) == 2
        latest_source = await facts.repository.get_fact(source.id, session=session)
        assert latest_source is not None
        await dreams.commit_operation(
            operation.id,
            output_fact_id=result.output_fact_id,
            output_results=tuple((item.id, fact_signature(item)) for item in outputs),
            added_evidence_ids=result.added_evidence_ids,
            added_relation_ids=result.added_relation_ids,
            result_signature=fact_signature(outputs[0]),
            source_signatures={source.id: fact_signature(latest_source)},
            session=session,
        )

    superseded = await facts.get_fact(source.id)
    assert superseded is not None and superseded.status is MemoryStatus.SUPERSEDED
    for fact_id in result.output_fact_ids:
        fact = await facts.get_fact(fact_id)
        assert fact is not None and fact.status is MemoryStatus.ACTIVE
    async with database.sessions() as session:
        persisted_results = tuple(
            (
                await session.scalars(
                    select(MemoryDreamOperationResultModel).where(
                        MemoryDreamOperationResultModel.operation_id == operation.id
                    )
                )
            ).all()
        )
    assert {item.fact_id for item in persisted_results} == set(result.output_fact_ids)

    async with facts.repository.transaction() as session:
        affected = await mutations.rollback_dream_operation(
            public_id=operation.public_id,
            session=session,
        )
    assert set(affected) == {source.id, *result.output_fact_ids}
    restored = await facts.get_fact(source.id)
    assert restored is not None and restored.status is MemoryStatus.ACTIVE
    for fact_id in result.output_fact_ids:
        invalidated = await facts.get_fact(fact_id)
        assert invalidated is not None and invalidated.status is MemoryStatus.INVALIDATED


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
            output_results=((current_preferred.id, fact_signature(current_preferred)),),
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
