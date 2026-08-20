"""Transactional persistence for controlled historical memory rebuilds."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from qq_ai_bot.memory.enums import (
    MemoryProcessingSource,
    MemoryRebuildCommitStatus,
    MemoryRebuildItemStatus,
    MemoryRebuildJobOutcome,
    MemoryRebuildReviewStatus,
    MemoryRebuildRunStatus,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.rebuild.models import (
    MemoryRebuildHealth,
    MemoryRebuildPlanStatistics,
    MemoryRebuildRun,
    MemoryRebuildSelection,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEmbeddingJobModel,
    MemoryJobModel,
    MemoryRebuildItemModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
)
from qq_ai_bot.persistence.repository_helpers import _ensure_person, _event_record
from qq_ai_bot.persistence.repository_records import EventRecord

TERMINAL_STATUSES = {
    MemoryRebuildRunStatus.COMPLETED.value,
    MemoryRebuildRunStatus.CANCELLED.value,
    MemoryRebuildRunStatus.FAILED.value,
}
EXECUTING_STATUSES = {
    MemoryRebuildRunStatus.EXTRACTING.value,
    MemoryRebuildRunStatus.COMMITTING.value,
}


class MemoryRebuildRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_run(
        self,
        *,
        selection: MemoryRebuildSelection,
        selection_json: str,
        selection_hash: str,
        snapshot_max_event_id: int,
        fingerprint: str,
        statistics: MemoryRebuildPlanStatistics,
        actor_user_id: str,
    ) -> MemoryRebuildRun:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            await _ensure_person(session, actor_user_id, now=now)
            row = MemoryRebuildRunModel(
                public_id=str(uuid.uuid4()),
                status=MemoryRebuildRunStatus.PLANNED.value,
                selection_json=selection_json,
                selection_hash=selection_hash,
                snapshot_max_event_id=snapshot_max_event_id,
                snapshot_created_at=now,
                scan_checkpoint_occurred_at=None,
                scan_checkpoint_event_id=None,
                commit_checkpoint_event_id=None,
                commit_checkpoint_claim_index=None,
                created_by_user_id=actor_user_id,
                extraction_fingerprint=fingerprint,
                plan_statistics_json=statistics.model_dump_json(),
                extraction_requests=0,
                consolidation_requests=0,
                input_tokens=0,
                output_tokens=0,
                latency_milliseconds=0,
                error_category=None,
                created_at=now,
                updated_at=now,
                started_at=None,
                review_ready_at=None,
                commit_started_at=None,
                completed_at=None,
                cancelled_at=None,
            )
            session.add(row)
            await session.flush()
            return self._run(row)

    async def get_run(self, public_id: str) -> MemoryRebuildRun | None:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == public_id)
            )
        return self._run(row) if row is not None else None

    async def list_runs(self, *, limit: int = 20) -> tuple[MemoryRebuildRun, ...]:
        async with self.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(MemoryRebuildRunModel)
                    .order_by(MemoryRebuildRunModel.created_at.desc())
                    .limit(limit)
                )
            ).all()
        return tuple(self._run(row) for row in rows)

    async def transition(
        self,
        public_id: str,
        *,
        expected: set[MemoryRebuildRunStatus],
        status: MemoryRebuildRunStatus,
        error_category: str | None = None,
    ) -> bool:
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "status": status.value,
            "updated_at": now,
            "error_category": error_category,
        }
        if status is MemoryRebuildRunStatus.EXTRACTING:
            values["started_at"] = now
        elif status is MemoryRebuildRunStatus.REVIEW:
            values["review_ready_at"] = now
        elif status is MemoryRebuildRunStatus.COMMITTING:
            values["commit_started_at"] = now
        elif status is MemoryRebuildRunStatus.COMPLETED:
            values["completed_at"] = now
        elif status is MemoryRebuildRunStatus.CANCELLED:
            values["cancelled_at"] = now
        conditions: list[Any] = [
            MemoryRebuildRunModel.public_id == public_id,
            MemoryRebuildRunModel.status.in_(tuple(item.value for item in expected)),
        ]
        if status.value in EXECUTING_STATUSES:
            other = aliased(MemoryRebuildRunModel)
            conditions.append(
                ~exists(
                    select(other.id).where(
                        other.public_id != public_id,
                        other.status.in_(EXECUTING_STATUSES),
                    )
                )
            )
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(MemoryRebuildRunModel).where(*conditions).values(**values)
            )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def pause_after_restart(self) -> int:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            extraction = await session.execute(
                update(MemoryRebuildRunModel)
                .where(MemoryRebuildRunModel.status == MemoryRebuildRunStatus.EXTRACTING.value)
                .values(
                    status=MemoryRebuildRunStatus.EXTRACTION_PAUSED.value,
                    error_category="process_restart",
                    updated_at=now,
                )
            )
            commit = await session.execute(
                update(MemoryRebuildRunModel)
                .where(MemoryRebuildRunModel.status == MemoryRebuildRunStatus.COMMITTING.value)
                .values(
                    status=MemoryRebuildRunStatus.COMMIT_PAUSED.value,
                    error_category="process_restart",
                    updated_at=now,
                )
            )
            await session.execute(
                update(MemoryRebuildItemModel)
                .where(MemoryRebuildItemModel.status == MemoryRebuildItemStatus.EXTRACTING.value)
                .values(
                    status=MemoryRebuildItemStatus.PENDING.value,
                    error_category="process_restart",
                    next_attempt_at=now,
                    updated_at=now,
                )
            )
        return int(cast(CursorResult[Any], extraction).rowcount or 0) + int(
            cast(CursorResult[Any], commit).rowcount or 0
        )

    async def get_executing_run(self) -> MemoryRebuildRun | None:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(MemoryRebuildRunModel)
                .where(MemoryRebuildRunModel.status.in_(EXECUTING_STATUSES))
                .order_by(MemoryRebuildRunModel.created_at)
                .limit(1)
            )
        return self._run(row) if row is not None else None

    async def executing_count(self) -> int:
        async with self.database.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRebuildRunModel)
                    .where(MemoryRebuildRunModel.status.in_(EXECUTING_STATUSES))
                )
                or 0
            )

    async def get_event(self, event_id: int) -> EventRecord | None:
        async with self.database.sessions() as session:
            row = await session.get(ChatEventModel, event_id)
        return _event_record(row) if row is not None else None

    async def ensure_item(
        self,
        public_id: str,
        *,
        event_id: int,
        source_event_hash: str,
    ) -> tuple[int, bool, str]:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            run_row = await session.scalar(
                select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == public_id)
            )
            if run_row is None:
                raise ValueError("memory rebuild run not found")
            run_id = run_row.id
            statement = insert(MemoryRebuildItemModel).values(
                run_id=run_id,
                event_id=event_id,
                status=MemoryRebuildItemStatus.PENDING.value,
                source_event_hash=source_event_hash,
                attempts=0,
                claim_count=0,
                next_attempt_at=now,
                error_category=None,
                created_at=now,
                updated_at=now,
            )
            await session.execute(
                statement.on_conflict_do_nothing(
                    index_elements=[MemoryRebuildItemModel.run_id, MemoryRebuildItemModel.event_id]
                )
            )
            row = await session.scalar(
                select(MemoryRebuildItemModel).where(
                    MemoryRebuildItemModel.run_id == run_id,
                    MemoryRebuildItemModel.event_id == event_id,
                )
            )
            assert row is not None
            if row.status in {
                MemoryRebuildItemStatus.STAGED.value,
                MemoryRebuildItemStatus.NO_CLAIMS.value,
                MemoryRebuildItemStatus.SKIPPED.value,
                MemoryRebuildItemStatus.COMMITTED.value,
                MemoryRebuildItemStatus.FAILED.value,
            }:
                return row.id, False, row.status
            retry_at = row.next_attempt_at
            if retry_at is not None:
                retry_at = retry_at.replace(tzinfo=UTC) if retry_at.tzinfo is None else retry_at
                if retry_at > now:
                    return row.id, False, row.status
            row.status = MemoryRebuildItemStatus.EXTRACTING.value
            row.attempts += 1
            row.next_attempt_at = None
            row.error_category = None
            row.updated_at = now
            await session.flush()
            return row.id, True, row.status

    async def stage_claims(
        self,
        public_id: str,
        *,
        item_id: int,
        event_id: int,
        claims: tuple[tuple[MemoryClaim, Any, str], ...],
    ) -> int:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            run_row = await session.scalar(
                select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == public_id)
            )
            if run_row is None:
                raise ValueError("memory rebuild run not found")
            run_id = run_row.id
            staged = 0
            for index, (claim, validated, claim_hash) in enumerate(claims):
                statement = insert(MemoryRebuildProposalModel).values(
                    run_id=run_id,
                    item_id=item_id,
                    event_id=event_id,
                    claim_index=index,
                    claim_json=claim.model_dump_json(),
                    claim_hash=claim_hash,
                    scope_type=validated.fact.scope_type.value,
                    subject_user_id=validated.fact.subject_user_id,
                    group_id=validated.fact.group_id,
                    operation=validated.operation.value,
                    kind=validated.fact.kind.value,
                    authority=validated.fact.authority.value,
                    confidence=validated.fact.confidence,
                    review_status=MemoryRebuildReviewStatus.PENDING.value,
                    commit_status=MemoryRebuildCommitStatus.PENDING.value,
                    actual_fact_id=None,
                    actual_action=None,
                    actual_reason_code=None,
                    attempts=0,
                    next_attempt_at=now,
                    error_category=None,
                    created_at=now,
                    updated_at=now,
                    reviewed_at=None,
                    reviewed_by_user_id=None,
                    committed_at=None,
                )
                result = await session.execute(
                    statement.on_conflict_do_nothing(
                        index_elements=[
                            MemoryRebuildProposalModel.item_id,
                            MemoryRebuildProposalModel.claim_index,
                        ]
                    )
                )
                staged += int(bool(cast(CursorResult[Any], result).rowcount))
            await session.execute(
                update(MemoryRebuildItemModel)
                .where(MemoryRebuildItemModel.id == item_id)
                .values(
                    status=(
                        MemoryRebuildItemStatus.STAGED.value
                        if claims
                        else MemoryRebuildItemStatus.NO_CLAIMS.value
                    ),
                    claim_count=len(claims),
                    next_attempt_at=None,
                    updated_at=now,
                    error_category=None,
                )
            )
            return staged

    async def fail_item(
        self,
        item_id: int,
        category: str,
        *,
        max_attempts: int,
        retry_initial_seconds: float,
    ) -> bool:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            row = await session.get(MemoryRebuildItemModel, item_id)
            if row is None:
                return False
            exhausted = row.attempts >= max_attempts
            row.error_category = category[:64]
            row.status = (
                MemoryRebuildItemStatus.FAILED.value
                if exhausted
                else MemoryRebuildItemStatus.PENDING.value
            )
            row.next_attempt_at = (
                None
                if exhausted
                else now
                + timedelta(seconds=retry_initial_seconds * (2 ** max(0, row.attempts - 1)))
            )
            row.updated_at = now
            return exhausted

    async def defer_item(self, item_id: int, *, category: str | None = None) -> None:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryRebuildItemModel)
                .where(
                    MemoryRebuildItemModel.id == item_id,
                    MemoryRebuildItemModel.status == MemoryRebuildItemStatus.EXTRACTING.value,
                )
                .values(
                    status=MemoryRebuildItemStatus.PENDING.value,
                    next_attempt_at=now,
                    error_category=category[:64] if category else None,
                    updated_at=now,
                )
            )

    async def scan_checkpoint(self, public_id: str) -> tuple[datetime | None, int | None]:
        async with self.database.sessions() as session:
            row = await session.execute(
                select(
                    MemoryRebuildRunModel.scan_checkpoint_occurred_at,
                    MemoryRebuildRunModel.scan_checkpoint_event_id,
                ).where(MemoryRebuildRunModel.public_id == public_id)
            )
            result = row.one_or_none()
        return (result[0], result[1]) if result is not None else (None, None)

    async def update_scan_checkpoint(self, public_id: str, event: EventRecord) -> None:
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryRebuildRunModel)
                .where(MemoryRebuildRunModel.public_id == public_id)
                .values(
                    scan_checkpoint_occurred_at=event.occurred_at,
                    scan_checkpoint_event_id=event.id,
                    updated_at=datetime.now(UTC),
                )
            )

    async def item_count(self, public_id: str) -> int:
        async with self.database.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRebuildItemModel)
                    .join(
                        MemoryRebuildRunModel,
                        MemoryRebuildRunModel.id == MemoryRebuildItemModel.run_id,
                    )
                    .where(MemoryRebuildRunModel.public_id == public_id)
                )
                or 0
            )

    async def proposal_counts(self, public_id: str) -> dict[str, int]:
        async with self.database.sessions() as session:
            rows = (
                await session.execute(
                    select(MemoryRebuildProposalModel.review_status, func.count())
                    .join(
                        MemoryRebuildRunModel,
                        MemoryRebuildRunModel.id == MemoryRebuildProposalModel.run_id,
                    )
                    .where(MemoryRebuildRunModel.public_id == public_id)
                    .group_by(MemoryRebuildProposalModel.review_status)
                )
            ).all()
        return {str(key): int(value) for key, value in rows}

    async def statistics(self, public_id: str) -> dict[str, Any]:
        """Return bounded, content-free execution counters derived from indexed staging rows."""

        async with self.database.sessions() as session:
            run_row = await session.scalar(
                select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == public_id)
            )
            if run_row is None:
                raise ValueError("memory rebuild run not found")
            run_id = run_row.id
            item_rows = (
                await session.execute(
                    select(MemoryRebuildItemModel.status, func.count())
                    .where(MemoryRebuildItemModel.run_id == run_id)
                    .group_by(MemoryRebuildItemModel.status)
                )
            ).all()
            review_rows = (
                await session.execute(
                    select(MemoryRebuildProposalModel.review_status, func.count())
                    .where(MemoryRebuildProposalModel.run_id == run_id)
                    .group_by(MemoryRebuildProposalModel.review_status)
                )
            ).all()
            commit_rows = (
                await session.execute(
                    select(MemoryRebuildProposalModel.commit_status, func.count())
                    .where(MemoryRebuildProposalModel.run_id == run_id)
                    .group_by(MemoryRebuildProposalModel.commit_status)
                )
            ).all()
            action_rows = (
                await session.execute(
                    select(MemoryRebuildProposalModel.actual_action, func.count())
                    .where(
                        MemoryRebuildProposalModel.run_id == run_id,
                        MemoryRebuildProposalModel.actual_action.is_not(None),
                    )
                    .group_by(MemoryRebuildProposalModel.actual_action)
                )
            ).all()
            attempts = int(
                await session.scalar(
                    select(func.sum(MemoryRebuildItemModel.attempts)).where(
                        MemoryRebuildItemModel.run_id == run_id
                    )
                )
                or 0
            )
            receipts = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryJobModel)
                    .where(
                        MemoryJobModel.rebuild_run_id == run_id,
                        MemoryJobModel.status == "done",
                    )
                )
                or 0
            )
            embedding_jobs_created = int(
                await session.scalar(
                    select(func.count(func.distinct(MemoryEmbeddingJobModel.id)))
                    .select_from(MemoryEmbeddingJobModel)
                    .join(
                        MemoryRebuildProposalModel,
                        MemoryRebuildProposalModel.actual_fact_id
                        == MemoryEmbeddingJobModel.fact_id,
                    )
                    .join(
                        MemoryRebuildRunModel,
                        MemoryRebuildRunModel.id == MemoryRebuildProposalModel.run_id,
                    )
                    .where(
                        MemoryRebuildProposalModel.run_id == run_id,
                        MemoryRebuildRunModel.commit_started_at.is_not(None),
                        MemoryEmbeddingJobModel.created_at
                        >= MemoryRebuildRunModel.commit_started_at,
                    )
                )
                or 0
            )
        return {
            "items": {str(key): int(value) for key, value in item_rows},
            "review": {str(key): int(value) for key, value in review_rows},
            "commit": {str(key): int(value) for key, value in commit_rows},
            "actions": {str(key): int(value) for key, value in action_rows},
            "extraction_attempts": attempts,
            "extraction_requests": run_row.extraction_requests,
            "consolidation_requests": run_row.consolidation_requests,
            "input_tokens": run_row.input_tokens,
            "output_tokens": run_row.output_tokens,
            "latency_milliseconds": run_row.latency_milliseconds,
            "receipts_completed": receipts,
            "embedding_jobs_created": embedding_jobs_created,
        }

    async def record_model_usage(
        self,
        public_id: str,
        *,
        extraction_requests: int = 0,
        consolidation_requests: int = 0,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_seconds: float = 0.0,
    ) -> None:
        """Persist additive, content-free provider usage for restart-safe status."""

        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryRebuildRunModel)
                .where(MemoryRebuildRunModel.public_id == public_id)
                .values(
                    extraction_requests=(
                        MemoryRebuildRunModel.extraction_requests + extraction_requests
                    ),
                    consolidation_requests=(
                        MemoryRebuildRunModel.consolidation_requests + consolidation_requests
                    ),
                    input_tokens=MemoryRebuildRunModel.input_tokens + (input_tokens or 0),
                    output_tokens=MemoryRebuildRunModel.output_tokens + (output_tokens or 0),
                    latency_milliseconds=(
                        MemoryRebuildRunModel.latency_milliseconds
                        + max(0, round(latency_seconds * 1000))
                    ),
                    updated_at=datetime.now(UTC),
                )
            )

    async def review_rows(self, public_id: str, *, offset: int, limit: int) -> tuple[Any, ...]:
        async with self.database.sessions() as session:
            return tuple(
                (
                    await session.execute(
                        select(MemoryRebuildProposalModel, ChatEventModel)
                        .join(
                            ChatEventModel, ChatEventModel.id == MemoryRebuildProposalModel.event_id
                        )
                        .join(
                            MemoryRebuildRunModel,
                            MemoryRebuildRunModel.id == MemoryRebuildProposalModel.run_id,
                        )
                        .where(MemoryRebuildRunModel.public_id == public_id)
                        .order_by(
                            ChatEventModel.occurred_at,
                            ChatEventModel.id,
                            MemoryRebuildProposalModel.claim_index,
                        )
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )

    async def set_review(
        self,
        public_id: str,
        *,
        proposal_ids: tuple[int, ...] | None,
        status: MemoryRebuildReviewStatus,
        actor_user_id: str,
    ) -> int:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            run_id = await session.scalar(
                select(MemoryRebuildRunModel.id).where(MemoryRebuildRunModel.public_id == public_id)
            )
            if run_id is None:
                raise ValueError("memory rebuild run not found")
            await _ensure_person(session, actor_user_id, now=now)
            statement = update(MemoryRebuildProposalModel).where(
                MemoryRebuildProposalModel.run_id == run_id,
                MemoryRebuildProposalModel.review_status == MemoryRebuildReviewStatus.PENDING.value,
            )
            if proposal_ids is not None:
                statement = statement.where(MemoryRebuildProposalModel.id.in_(proposal_ids))
            result = await session.execute(
                statement.values(
                    review_status=status.value,
                    reviewed_at=now,
                    reviewed_by_user_id=actor_user_id,
                    updated_at=now,
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def proposal_ids_for_filter(
        self,
        public_id: str,
        filters: dict[str, Any],
    ) -> tuple[int, ...]:
        conditions: list[Any] = [MemoryRebuildRunModel.public_id == public_id]
        mapping = {
            "scope": MemoryRebuildProposalModel.scope_type,
            "scope_type": MemoryRebuildProposalModel.scope_type,
            "operation": MemoryRebuildProposalModel.operation,
            "kind": MemoryRebuildProposalModel.kind,
            "authority": MemoryRebuildProposalModel.authority,
            "group": MemoryRebuildProposalModel.group_id,
            "group_id": MemoryRebuildProposalModel.group_id,
            "subject": MemoryRebuildProposalModel.subject_user_id,
            "subject_user_id": MemoryRebuildProposalModel.subject_user_id,
        }
        for key, value in filters.items():
            if key in mapping:
                conditions.append(mapping[key] == str(value))
            elif key == "confidence_min":
                conditions.append(MemoryRebuildProposalModel.confidence >= float(value))
            elif key == "confidence_max":
                conditions.append(MemoryRebuildProposalModel.confidence <= float(value))
            else:
                raise ValueError(f"unknown review filter: {key}")
        async with self.database.sessions() as session:
            rows = await session.scalars(
                select(MemoryRebuildProposalModel.id)
                .join(
                    MemoryRebuildRunModel,
                    MemoryRebuildRunModel.id == MemoryRebuildProposalModel.run_id,
                )
                .where(*conditions)
                .order_by(MemoryRebuildProposalModel.id)
            )
            return tuple(int(item) for item in rows.all())

    async def pending_review_count(self, public_id: str) -> int:
        async with self.database.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRebuildProposalModel)
                    .join(
                        MemoryRebuildRunModel,
                        MemoryRebuildRunModel.id == MemoryRebuildProposalModel.run_id,
                    )
                    .where(
                        MemoryRebuildRunModel.public_id == public_id,
                        MemoryRebuildProposalModel.review_status
                        == MemoryRebuildReviewStatus.PENDING.value,
                    )
                )
                or 0
            )

    async def next_commit_rows(self, public_id: str, *, limit: int) -> tuple[Any, ...]:
        now = datetime.now(UTC)
        async with self.database.sessions() as session:
            return tuple(
                (
                    await session.execute(
                        select(MemoryRebuildProposalModel, MemoryRebuildItemModel, ChatEventModel)
                        .join(
                            MemoryRebuildItemModel,
                            MemoryRebuildItemModel.id == MemoryRebuildProposalModel.item_id,
                        )
                        .join(
                            ChatEventModel, ChatEventModel.id == MemoryRebuildProposalModel.event_id
                        )
                        .join(
                            MemoryRebuildRunModel,
                            MemoryRebuildRunModel.id == MemoryRebuildProposalModel.run_id,
                        )
                        .where(
                            MemoryRebuildRunModel.public_id == public_id,
                            MemoryRebuildProposalModel.review_status
                            == MemoryRebuildReviewStatus.APPROVED.value,
                            MemoryRebuildProposalModel.commit_status
                            == MemoryRebuildCommitStatus.PENDING.value,
                            (
                                MemoryRebuildProposalModel.next_attempt_at.is_(None)
                                | (MemoryRebuildProposalModel.next_attempt_at <= now)
                            ),
                        )
                        .order_by(
                            ChatEventModel.occurred_at,
                            ChatEventModel.id,
                            MemoryRebuildProposalModel.claim_index,
                        )
                        .limit(limit)
                    )
                ).all()
            )

    async def finish_proposal(
        self,
        proposal_id: int,
        *,
        status: MemoryRebuildCommitStatus,
        fact_id: int | None,
        action: str,
        reason_code: str,
        error_category: str | None = None,
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            proposal = await session.get(MemoryRebuildProposalModel, proposal_id)
            if proposal is None:
                return
            await session.execute(
                update(MemoryRebuildProposalModel)
                .where(MemoryRebuildProposalModel.id == proposal_id)
                .values(
                    commit_status=status.value,
                    actual_fact_id=fact_id,
                    actual_action=action[:32],
                    actual_reason_code=reason_code[:64],
                    attempts=MemoryRebuildProposalModel.attempts + 1,
                    error_category=error_category[:64] if error_category else None,
                    next_attempt_at=None,
                    committed_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            await session.execute(
                update(MemoryRebuildRunModel)
                .where(MemoryRebuildRunModel.id == proposal.run_id)
                .values(
                    commit_checkpoint_event_id=proposal.event_id,
                    commit_checkpoint_claim_index=proposal.claim_index,
                    updated_at=datetime.now(UTC),
                )
            )

    async def fail_proposal(
        self,
        proposal_id: int,
        category: str,
        *,
        max_attempts: int,
        retry_initial_seconds: float,
    ) -> bool:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            row = await session.get(MemoryRebuildProposalModel, proposal_id)
            if row is None:
                return False
            row.attempts += 1
            exhausted = row.attempts >= max_attempts
            row.commit_status = (
                MemoryRebuildCommitStatus.FAILED.value
                if exhausted
                else MemoryRebuildCommitStatus.PENDING.value
            )
            row.error_category = category[:64]
            row.next_attempt_at = (
                None
                if exhausted
                else now
                + timedelta(seconds=retry_initial_seconds * (2 ** max(0, row.attempts - 1)))
            )
            row.updated_at = now
            return exhausted

    async def receipt_status(self, event_id: int) -> str | None:
        async with self.database.sessions() as session:
            return cast(
                str | None,
                await session.scalar(
                    select(MemoryJobModel.status).where(MemoryJobModel.event_id == event_id)
                ),
            )

    async def complete_item_receipts(
        self,
        public_id: str,
        *,
        include_failed_live_jobs: bool,
    ) -> int:
        now = datetime.now(UTC)
        completed = 0
        async with self.database.sessions() as session, session.begin():
            run_id = await session.scalar(
                select(MemoryRebuildRunModel.id).where(MemoryRebuildRunModel.public_id == public_id)
            )
            if run_id is None:
                return 0
            items = (
                await session.scalars(
                    select(MemoryRebuildItemModel).where(MemoryRebuildItemModel.run_id == run_id)
                )
            ).all()
            for item in items:
                pending = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(MemoryRebuildProposalModel)
                        .where(
                            MemoryRebuildProposalModel.item_id == item.id,
                            MemoryRebuildProposalModel.review_status
                            == MemoryRebuildReviewStatus.APPROVED.value,
                            MemoryRebuildProposalModel.commit_status.in_(
                                (
                                    MemoryRebuildCommitStatus.PENDING.value,
                                    MemoryRebuildCommitStatus.FAILED.value,
                                )
                            ),
                        )
                    )
                    or 0
                )
                if pending:
                    continue
                proposals = (
                    await session.scalars(
                        select(MemoryRebuildProposalModel).where(
                            MemoryRebuildProposalModel.item_id == item.id
                        )
                    )
                ).all()
                outcome = (
                    MemoryRebuildJobOutcome.NO_CLAIMS
                    if not proposals
                    else (
                        MemoryRebuildJobOutcome.ALL_REJECTED
                        if all(
                            row.review_status == MemoryRebuildReviewStatus.REJECTED.value
                            for row in proposals
                        )
                        else MemoryRebuildJobOutcome.CLAIMS_APPLIED
                    )
                )
                receipt = await session.scalar(
                    select(MemoryJobModel).where(MemoryJobModel.event_id == item.event_id)
                )
                if receipt is not None and receipt.status in {"done", "pending", "processing"}:
                    item.status = MemoryRebuildItemStatus.SKIPPED.value
                    item.error_category = (
                        "already_processed" if receipt.status == "done" else "live_job_active"
                    )
                    item.updated_at = now
                    continue
                if (
                    receipt is not None
                    and receipt.status == "failed"
                    and not include_failed_live_jobs
                ):
                    item.status = MemoryRebuildItemStatus.SKIPPED.value
                    item.error_category = "failed_live_job_not_selected"
                    item.updated_at = now
                    continue
                statement = insert(MemoryJobModel).values(
                    event_id=item.event_id,
                    conversation_key=f"rebuild:{public_id}",
                    status="done",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                    error_category=None,
                    processing_source=MemoryProcessingSource.REBUILD.value,
                    rebuild_run_id=run_id,
                    outcome=outcome.value,
                    completed_at=now,
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[MemoryJobModel.event_id],
                        where=(MemoryJobModel.status == "failed"),
                        set_={
                            "status": "done",
                            "updated_at": now,
                            "error_category": None,
                            "processing_source": MemoryProcessingSource.REBUILD.value,
                            "rebuild_run_id": run_id,
                            "outcome": outcome.value,
                            "completed_at": now,
                        },
                    )
                )
                item.status = MemoryRebuildItemStatus.COMMITTED.value
                item.updated_at = now
                completed += 1
        return completed

    async def failed_commit_count(self, public_id: str) -> int:
        async with self.database.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRebuildProposalModel)
                    .join(
                        MemoryRebuildRunModel,
                        MemoryRebuildRunModel.id == MemoryRebuildProposalModel.run_id,
                    )
                    .where(
                        MemoryRebuildRunModel.public_id == public_id,
                        MemoryRebuildProposalModel.commit_status
                        == MemoryRebuildCommitStatus.FAILED.value,
                    )
                )
                or 0
            )

    async def reset_failed(self, public_id: str) -> MemoryRebuildRunStatus:
        now = datetime.now(UTC)
        async with self.database.sessions() as session, session.begin():
            run = await session.scalar(
                select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == public_id)
            )
            if run is None:
                raise ValueError("memory rebuild run not found")
            failed_proposals = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRebuildProposalModel)
                    .where(
                        MemoryRebuildProposalModel.run_id == run.id,
                        MemoryRebuildProposalModel.commit_status
                        == MemoryRebuildCommitStatus.FAILED.value,
                    )
                )
                or 0
            )
            if failed_proposals:
                await session.execute(
                    update(MemoryRebuildProposalModel)
                    .where(
                        MemoryRebuildProposalModel.run_id == run.id,
                        MemoryRebuildProposalModel.commit_status
                        == MemoryRebuildCommitStatus.FAILED.value,
                    )
                    .values(
                        commit_status=MemoryRebuildCommitStatus.PENDING.value,
                        attempts=0,
                        next_attempt_at=now,
                        error_category=None,
                        updated_at=now,
                    )
                )
                return MemoryRebuildRunStatus.COMMIT_PAUSED
            await session.execute(
                update(MemoryRebuildItemModel)
                .where(
                    MemoryRebuildItemModel.run_id == run.id,
                    MemoryRebuildItemModel.status == MemoryRebuildItemStatus.FAILED.value,
                )
                .values(
                    status=MemoryRebuildItemStatus.PENDING.value,
                    attempts=0,
                    next_attempt_at=now,
                    error_category=None,
                    updated_at=now,
                )
            )
            return MemoryRebuildRunStatus.EXTRACTION_PAUSED

    async def remaining_commit_count(self, public_id: str) -> int:
        async with self.database.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryRebuildProposalModel)
                    .join(
                        MemoryRebuildRunModel,
                        MemoryRebuildRunModel.id == MemoryRebuildProposalModel.run_id,
                    )
                    .where(
                        MemoryRebuildRunModel.public_id == public_id,
                        MemoryRebuildProposalModel.review_status
                        == MemoryRebuildReviewStatus.APPROVED.value,
                        MemoryRebuildProposalModel.commit_status
                        == MemoryRebuildCommitStatus.PENDING.value,
                    )
                )
                or 0
            )

    async def purge(self, public_id: str) -> bool:
        async with self.database.sessions() as session, session.begin():
            run = await session.scalar(
                select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == public_id)
            )
            if run is None:
                return False
            if run.status not in TERMINAL_STATUSES:
                raise ValueError("only terminal rebuild runs can be purged")
            await session.delete(run)
            return True

    async def forget_person(
        self,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        """Cancel person-only runs and remove exact QQ values from stored selections."""

        if session is None:
            async with self.database.sessions() as owned_session, owned_session.begin():
                return await self.forget_person(user_id, session=owned_session)
        now = datetime.now(UTC)
        changed = 0
        deleted_proposals = await session.execute(
            delete(MemoryRebuildProposalModel).where(
                MemoryRebuildProposalModel.subject_user_id == user_id
            )
        )
        changed += int(cast(CursorResult[Any], deleted_proposals).rowcount or 0)
        rows = (await session.scalars(select(MemoryRebuildRunModel))).all()
        for row in rows:
            selection = MemoryRebuildSelection.model_validate_json(row.selection_json)
            if user_id not in selection.sender_user_ids and user_id not in selection.bot_user_ids:
                continue
            remaining = tuple(item for item in selection.sender_user_ids if item != user_id)
            remaining_bots = tuple(item for item in selection.bot_user_ids if item != user_id)
            other_bounds = bool(
                selection.all_events
                or remaining_bots
                or selection.scope_types
                or selection.group_ids
                or selection.after
                or selection.before
                or selection.minimum_event_id
                or selection.maximum_event_id
                or remaining
            )
            if not other_bounds:
                remaining = ("[deleted-user]",)
            sanitized = selection.model_copy(
                update={
                    "sender_user_ids": remaining,
                    "bot_user_ids": remaining_bots,
                }
            )
            encoded = json.dumps(
                sanitized.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            row.selection_json = encoded
            row.selection_hash = hashlib.sha256(encoded.encode()).hexdigest()
            if not other_bounds and row.status not in TERMINAL_STATUSES:
                row.status = MemoryRebuildRunStatus.CANCELLED.value
                row.cancelled_at = now
                row.error_category = "privacy_deletion"
            row.updated_at = now
            changed += 1
        return changed

    async def health(
        self,
        *,
        enabled: bool,
        active_in_flight_calls: int = 0,
    ) -> MemoryRebuildHealth:
        async with self.database.sessions() as session:
            status_result = (
                await session.execute(
                    select(MemoryRebuildRunModel.status, func.count()).group_by(
                        MemoryRebuildRunModel.status
                    )
                )
            ).all()
            statuses: dict[str, int] = {str(status): int(count) for status, count in status_result}
            oldest = await session.scalar(
                select(func.min(MemoryRebuildRunModel.created_at)).where(
                    MemoryRebuildRunModel.status.not_in(TERMINAL_STATUSES)
                )
            )
            item_result = (
                await session.execute(
                    select(MemoryRebuildItemModel.status, func.count()).group_by(
                        MemoryRebuildItemModel.status
                    )
                )
            ).all()
            item_statuses: dict[str, int] = {
                str(status): int(count) for status, count in item_result
            }
            proposal_result = (
                await session.execute(
                    select(MemoryRebuildProposalModel.commit_status, func.count()).group_by(
                        MemoryRebuildProposalModel.commit_status
                    )
                )
            ).all()
            proposal_statuses: dict[str, int] = {
                str(status): int(count) for status, count in proposal_result
            }
            last_error = await session.scalar(
                select(MemoryRebuildRunModel.error_category)
                .where(MemoryRebuildRunModel.error_category.is_not(None))
                .order_by(MemoryRebuildRunModel.updated_at.desc())
                .limit(1)
            )
            last_extraction = await session.scalar(
                select(func.max(MemoryRebuildItemModel.updated_at)).where(
                    MemoryRebuildItemModel.status.in_(
                        (
                            MemoryRebuildItemStatus.STAGED.value,
                            MemoryRebuildItemStatus.NO_CLAIMS.value,
                        )
                    )
                )
            )
            last_commit = await session.scalar(
                select(func.max(MemoryRebuildProposalModel.committed_at)).where(
                    MemoryRebuildProposalModel.commit_status
                    == MemoryRebuildCommitStatus.COMMITTED.value
                )
            )
        return MemoryRebuildHealth(
            enabled=enabled,
            planned_runs=int(statuses.get("planned", 0)),
            extracting_runs=int(statuses.get("extracting", 0)),
            paused_runs=int(statuses.get("extraction_paused", 0))
            + int(statuses.get("commit_paused", 0)),
            review_runs=int(statuses.get("review", 0)),
            committing_runs=int(statuses.get("committing", 0)),
            failed_runs=int(statuses.get("failed", 0)),
            oldest_active_run=oldest,
            active_in_flight_calls=active_in_flight_calls,
            pending_items=int(item_statuses.get("pending", 0))
            + int(item_statuses.get("extracting", 0)),
            pending_proposals=int(proposal_statuses.get("pending", 0)),
            failed_items=int(item_statuses.get("failed", 0)),
            failed_proposals=int(proposal_statuses.get("failed", 0)),
            last_successful_extraction=last_extraction,
            last_successful_commit=last_commit,
            last_error_category=last_error,
        )

    @staticmethod
    def _run(row: MemoryRebuildRunModel) -> MemoryRebuildRun:
        return MemoryRebuildRun(
            public_id=row.public_id,
            status=row.status,
            selection=MemoryRebuildSelection.model_validate_json(row.selection_json),
            selection_hash=row.selection_hash,
            snapshot_max_event_id=row.snapshot_max_event_id,
            snapshot_created_at=row.snapshot_created_at,
            scan_checkpoint_occurred_at=row.scan_checkpoint_occurred_at,
            scan_checkpoint_event_id=row.scan_checkpoint_event_id,
            commit_checkpoint_event_id=row.commit_checkpoint_event_id,
            commit_checkpoint_claim_index=row.commit_checkpoint_claim_index,
            extraction_fingerprint=row.extraction_fingerprint,
            plan_statistics=MemoryRebuildPlanStatistics.model_validate_json(
                row.plan_statistics_json
            ),
            error_category=row.error_category,
            created_at=row.created_at,
            updated_at=row.updated_at,
            started_at=row.started_at,
            review_ready_at=row.review_ready_at,
            commit_started_at=row.commit_started_at,
            completed_at=row.completed_at,
            cancelled_at=row.cancelled_at,
        )
