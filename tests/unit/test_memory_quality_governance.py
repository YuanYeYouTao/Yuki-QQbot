"""Production audit, hygiene, and release-check safety contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import (
    MemoryConflictState,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemorySourceType,
    MemoryStateAction,
    MemoryStatus,
)
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.quality.audit import MemoryProductionQualityAudit
from qq_ai_bot.memory.quality.hygiene import MemoryProvenanceHygiene
from qq_ai_bot.memory.quality.release_check import MemoryReleaseCheck
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository

ROOT = Path(__file__).parents[2]


@pytest.mark.asyncio
async def test_clean_production_audit_is_content_free(database: Database) -> None:
    report = await MemoryProductionQualityAudit(database).run()
    assert report.error_count == 0
    payload = report.model_dump_json()
    assert "content" not in payload
    assert "synthetic secret" not in payload
    assert all(len(item.sample_ids) <= 20 for item in report.issues)


async def _invalid_provenance_fact(database: Database, suffix: str) -> int:
    ledger = EventLedgerRepository(database)
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=f"invalid-source-{suffix}",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="outbound",
        content="synthetic outbound source",
        private_peer_user_id="1001",
    )
    fact = await MemoryFactService(MemoryFactRepository(database)).remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="1001",
            memory_key=f"invalid:{suffix}",
            category="quality",
            content=f"synthetic fact {suffix}",
            source_type=MemorySourceType.AUTOMATIC,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id="1001",
            relation="self_statement",
            excerpt="synthetic outbound source",
        ),
    )
    return fact.id


@pytest.mark.asyncio
async def test_audit_detects_invalid_evidence_without_exposing_text(database: Database) -> None:
    await _invalid_provenance_fact(database, "audit")
    report = await MemoryProductionQualityAudit(database).run()
    issue = next(item for item in report.issues if item.issue_code == "evidence_source_invalid")
    assert issue.count == 1
    assert issue.sample_ids  # evidence row IDs are reported, never content
    assert report.error_count >= 1
    assert "synthetic outbound source" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_audit_accepts_superseded_fact_with_semantic_relation_chain(
    database: Database,
) -> None:
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    older = await service.remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="1001",
            memory_key="quality:older",
            category="quality",
            content="synthetic older fact",
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    newer = await service.remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="1001",
            memory_key="quality:newer",
            category="quality",
            content="synthetic newer fact",
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    async with repository.transaction() as session:
        await repository.transition(
            older.id,
            status=MemoryStatus.SUPERSEDED,
            conflict_state=MemoryConflictState.CLEAR,
            invalidated_reason=None,
            action=MemoryStateAction.SUPERSEDED,
            reason_code="quality_test_chain",
            source_event_id=None,
            actor_user_id=None,
            session=session,
        )
        await repository.add_relation(
            source_fact_id=newer.id,
            target_fact_id=older.id,
            relation_type=MemoryFactRelationType.CONTRADICTS,
            confidence=1.0,
            source_event_id=None,
            session=session,
        )

    report = await MemoryProductionQualityAudit(database).run()
    issue = next(item for item in report.issues if item.issue_code == "superseded_without_chain")
    assert issue.count == 0


@pytest.mark.asyncio
async def test_hygiene_requires_matching_fingerprint_and_versions_invalidation(
    database: Database,
) -> None:
    fact_id = await _invalid_provenance_fact(database, "apply")
    hygiene = MemoryProvenanceHygiene(database)
    plan = await hygiene.scan()
    assert fact_id in plan.invalid_fact_ids
    applied = await hygiene.apply(plan.fingerprint)
    assert applied.fingerprint == plan.fingerprint
    fact = await MemoryFactRepository(database).get_fact(fact_id)
    assert fact is not None
    assert fact.status is MemoryStatus.INVALIDATED
    assert fact.invalidated_reason is MemoryInvalidationReason.ADMINISTRATOR_INVALIDATED
    history = await MemoryFactRepository(database).list_state_events(fact_id)
    assert history[-1].reason_code == "invalid_provenance"
    assert fact_id not in (await MemoryProvenanceHygiene(database).scan()).invalid_fact_ids


@pytest.mark.asyncio
async def test_hygiene_rejects_stale_fingerprint(database: Database) -> None:
    await _invalid_provenance_fact(database, "first")
    hygiene = MemoryProvenanceHygiene(database)
    plan = await hygiene.scan()
    await _invalid_provenance_fact(database, "second")
    with pytest.raises(RuntimeError, match="fingerprint changed"):
        await hygiene.apply(plan.fingerprint)


@pytest.mark.asyncio
async def test_release_check_is_read_only_and_requires_explicit_database(tmp_path: Path) -> None:
    report = await MemoryReleaseCheck(ROOT, artifact_directory=tmp_path).run()
    assert report.alembic_head == "0040"
    database = next(item for item in report.items if item.code == "production_database")
    assert database.status == "warn"
    assert "--database-url" in database.detail
