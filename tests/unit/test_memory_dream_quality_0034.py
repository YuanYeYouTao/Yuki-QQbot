"""Regression contracts for Dream preview persistence and evidence governance."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamClusterModel,
    MemoryDreamClusterPreviewModel,
    MemoryDreamRunModel,
)
from qq_ai_bot.memory.dream.models import (
    DreamAction,
    DreamOperationType,
    DreamOutput,
    DreamPlanStatistics,
    DreamRecomposeOutput,
    DreamRunMode,
)
from qq_ai_bot.memory.dream.repository import DreamRepository, fact_signature
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.evidence_compaction import EvidenceCompactionService
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryEvidenceModel,
    MemoryMutationReceiptModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository, PeopleRepository


def _proposal(content: str = "一段被压缩后的独立经历") -> DreamOutput:
    return DreamOutput(
        actions=(
            DreamAction(
                operation=DreamOperationType.RECOMPOSE,
                source_refs=("memory_1",),
                outputs=(
                    DreamRecomposeOutput(
                        focus="独立经历",
                        source_refs=("memory_1",),
                        content=content,
                        importance=4,
                    ),
                ),
            ),
        )
    )


@pytest.mark.asyncio
async def test_preview_is_immutable_superseded_and_stale_checked(database: Database) -> None:
    repository = DreamRepository(database)
    await PeopleRepository(database).observe(user_id="1001", nickname="owner")
    run = await repository.create_run(
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
        clusters=(("quality", "partition", "8000", "episode", (1,), "fingerprint"),),
        snapshot_max_fact_id=1,
        actor_user_id="1001",
        scheduled_slot=None,
    )
    cluster = (await repository.run_page(run.public_id)).clusters[0]
    first = await repository.save_preview(
        cluster_id=cluster.id,
        source_fingerprint="fingerprint",
        proposal=_proposal("第一版"),
        model_calls=1,
        source_characters=100,
        output_characters=3,
    )
    second = await repository.save_preview(
        cluster_id=cluster.id,
        source_fingerprint="fingerprint",
        proposal=_proposal("第二版"),
        model_calls=1,
        source_characters=100,
        output_characters=3,
    )
    assert first != second
    ready = await repository.ready_preview(cluster_id=cluster.id, source_fingerprint="fingerprint")
    assert ready is not None
    _id, public_id, proposal = ready
    assert public_id == second
    assert proposal.actions[0].outputs[0].content == "第二版"
    assert (
        await repository.ready_preview(cluster_id=cluster.id, source_fingerprint="changed") is None
    )
    async with database.sessions() as session:
        statuses = dict(
            (
                await session.execute(
                    select(
                        MemoryDreamClusterPreviewModel.public_id,
                        MemoryDreamClusterPreviewModel.status,
                    )
                )
            ).all()
        )
    assert statuses[first] == "superseded"
    assert statuses[second] == "stale"


@pytest.mark.asyncio
async def test_reflection_compactor_preserves_window_lineage_and_bounds_direct_evidence(
    database: Database,
) -> None:
    settings = make_settings(database.url)
    ledger = EventLedgerRepository(database)
    facts = MemoryFactService(MemoryFactRepository(database))
    await PeopleRepository(database).observe(user_id="8000", nickname="Yuki", is_bot=True)
    events = []
    for index in range(10):
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"quality-reflection-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000",
            direction="outbound",
            content=f"原始窗口消息 {index}",
            group_id="3001",
            sender_is_bot=True,
        )
        events.append(event)
    created = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.SELF,
            visibility_type=SelfMemoryVisibility.GROUP,
            visibility_group_id="3001",
            kind=MemoryKind.EPISODE,
            memory_key="self_episode:quality",
            category="self_episode",
            content="我记得这段完整经历。",
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.AGENT_REFLECTION,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=events[-1].id,
            source_speaker_user_id="8000",
            relation=MemoryEvidenceRelation.AGENT_REFLECTION,
            confidence=0.9,
            authority=MemoryAuthority.AGENT_REFLECTION,
            excerpt=events[-1].content,
        ),
    )
    async with facts.repository.transaction() as session:
        await facts.append_evidence_bundle(
            created.id,
            tuple(
                MemoryEvidenceCreate(
                    event_id=event.id,
                    source_speaker_user_id="8000",
                    relation=MemoryEvidenceRelation.AGENT_REFLECTION,
                    confidence=0.9,
                    authority=MemoryAuthority.AGENT_REFLECTION,
                    excerpt=event.content,
                )
                for event in events[:-1]
            ),
            confirmed_at=events[-1].occurred_at,
            session=session,
        )
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        run = MemorySelfReflectionRunModel(
            conversation_key_hash="quality-reflection",
            bot_user_id="8000",
            scheduled_slot="2026-08-13:04:quality",
            trigger_reason="manual",
            first_event_id=events[0].id,
            last_event_id=events[-1].id,
            status="completed",
            proposal_count=1,
            committed_count=1,
            error_category=None,
            started_at=now,
            completed_at=now,
        )
        session.add(run)
        await session.flush()
        session.add(
            MemorySelfReflectionResultModel(
                run_id=run.id,
                fact_id=created.id,
                result_kind="episode",
                result_index=1,
                created_at=now,
            )
        )
        session.add(
            MemoryMutationReceiptModel(
                mutation_id="00000000-0000-0000-0000-000000000034",
                idempotency_key="a" * 64,
                claim_fingerprint="b" * 64,
                target_fingerprint="c" * 64,
                trigger_source_type="chat_event",
                trigger_event_id=events[-1].id,
                dream_operation_id=None,
                conversation_key="group:3001:self-reflection",
                current_group_id="3001",
                turn_origin="memory_self_reflection",
                delegation_mode=f"self_episode:{events[0].id}:{events[-1].id}",
                trigger_actor_user_id="8000",
                decision_actor_type="reflection",
                decision_actor_id="yuki_self_reflection",
                executed_by_bot_user_id="8000",
                requested_operation="create",
                applied_operation="create",
                old_fact_id=None,
                new_fact_id=created.id,
                outcome="committed",
                reason_code="self_reflection_episode",
                created_at=now,
            )
        )
    service = EvidenceCompactionService(
        settings=settings,
        database=database,
        facts=facts,
    )
    assert await service.run_batch() == 1
    async with database.sessions() as session:
        count = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryEvidenceModel)
                .where(MemoryEvidenceModel.fact_id == created.id)
            )
            or 0
        )
    assert 1 <= count <= 8
    lineage = await facts.list_evidence_lineage(created.id, limit=50)
    assert len(cast(SimpleNamespace, lineage).items) >= 10


@pytest.mark.asyncio
async def test_compactor_prefers_latest_dream_provenance_over_reflection_mapping(
    database: Database,
) -> None:
    settings = make_settings(database.url)
    ledger = EventLedgerRepository(database)
    facts = MemoryFactService(MemoryFactRepository(database))
    repository = DreamRepository(database)
    await PeopleRepository(database).observe(user_id="8100", nickname="Yuki", is_bot=True)
    events = []
    for index in range(13):
        event, _ = await ledger.append(
            bot_user_id="8100",
            platform_message_id=f"quality-provenance-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8100",
            direction="outbound",
            content=f"来源消息 {index}",
            group_id="3100",
            sender_is_bot=True,
        )
        events.append(event)
    created = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.SELF,
            visibility_type=SelfMemoryVisibility.GROUP,
            visibility_group_id="3100",
            kind=MemoryKind.EPISODE,
            memory_key="self_episode:dream-provenance",
            category="self_episode",
            content="一段后来又被 Dream 处理过的经历。",
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.AGENT_REFLECTION,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=events[0].id,
            source_speaker_user_id="8100",
            relation=MemoryEvidenceRelation.AGENT_REFLECTION,
            confidence=0.9,
            authority=MemoryAuthority.AGENT_REFLECTION,
            excerpt=events[0].content,
        ),
    )
    async with facts.repository.transaction() as session:
        await facts.append_evidence_bundle(
            created.id,
            tuple(
                MemoryEvidenceCreate(
                    event_id=event.id,
                    source_speaker_user_id="8100",
                    relation=MemoryEvidenceRelation.AGENT_REFLECTION,
                    confidence=0.9,
                    authority=MemoryAuthority.AGENT_REFLECTION,
                    excerpt=event.content,
                )
                for event in events[1:]
            ),
            confirmed_at=events[-1].occurred_at,
            session=session,
        )
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        reflection_run = MemorySelfReflectionRunModel(
            conversation_key_hash="quality-provenance",
            bot_user_id="8100",
            scheduled_slot="2026-08-13:04:provenance",
            trigger_reason="manual",
            first_event_id=events[0].id,
            last_event_id=events[-1].id,
            status="completed",
            proposal_count=1,
            committed_count=1,
            error_category=None,
            started_at=now,
            completed_at=now,
        )
        session.add(reflection_run)
        await session.flush()
        session.add(
            MemorySelfReflectionResultModel(
                run_id=reflection_run.id,
                fact_id=created.id,
                result_kind="episode",
                result_index=1,
                created_at=now,
            )
        )
    dream_run = await repository.create_run(
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
        clusters=(("quality", "self", "8100", "episode", (created.id,), "f" * 64),),
        snapshot_max_fact_id=created.id,
        actor_user_id=None,
        scheduled_slot=None,
    )
    async with facts.repository.transaction() as session:
        cluster_id_value = await session.scalar(
            select(MemoryDreamClusterModel.id)
            .join(
                MemoryDreamRunModel,
                MemoryDreamRunModel.id == MemoryDreamClusterModel.run_id,
            )
            .where(MemoryDreamRunModel.public_id == dream_run.public_id)
        )
        assert cluster_id_value is not None
        cluster_id = int(cluster_id_value)
        current = await facts.repository.get_fact(created.id, session=session)
        assert current is not None
        operation = await repository.create_operation(
            cluster_id=cluster_id,
            action_index=0,
            operation_type=DreamOperationType.MERGE,
            source_facts=(current,),
            anchor_fact_id=current.id,
            session=session,
        )
        signature = fact_signature(current)
        await repository.commit_operation(
            operation.id,
            output_fact_id=current.id,
            output_results=((current.id, signature),),
            added_evidence_ids=(),
            added_relation_ids=(),
            result_signature=signature,
            source_signatures={current.id: signature},
            session=session,
        )
    service = EvidenceCompactionService(settings=settings, database=database, facts=facts)
    candidates = await service._candidate_facts(limit=20)
    assert candidates == ((created.id, "dream", operation.id, 13),)
