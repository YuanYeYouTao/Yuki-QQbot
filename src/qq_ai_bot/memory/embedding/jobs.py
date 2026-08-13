"""Persistent embedding job scheduling, reconciliation, and atomic completion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult

from qq_ai_bot.memory.embedding.models import (
    MemoryEmbeddingJob,
    MemoryEmbeddingJobStatus,
    MemoryEmbeddingProfileRecord,
)
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryEmbeddingJobModel,
    MemoryEmbeddingModel,
    MemoryFactModel,
)

_EMBEDDABLE_STATUSES = ("active", "contested")


@dataclass(frozen=True, slots=True)
class EmbeddingWrite:
    job_id: int
    fact_id: int
    content_hash: str
    vector_blob: bytes


class MemoryEmbeddingJobRepository:
    def __init__(
        self,
        database: Database,
        *,
        profile: MemoryEmbeddingProfileRecord,
        documents: EmbeddingDocumentBuilder,
    ) -> None:
        self._database = database
        self.profile = profile
        self.documents = documents

    def _hash_row(self, row: MemoryFactModel) -> str:
        return self.documents.content_hash_fields(
            kind=row.kind,
            category=row.category,
            memory_key=row.memory_key,
            content=row.content,
        )

    async def enqueue_fact(self, fact_id: int, *, force: bool = False) -> bool:
        async with self._database.sessions() as session, session.begin():
            fact = await session.get(MemoryFactModel, fact_id)
            if fact is None or fact.status not in _EMBEDDABLE_STATUSES:
                return False
            content_hash = self._hash_row(fact)
            existing = await session.scalar(
                select(MemoryEmbeddingModel.id).where(
                    MemoryEmbeddingModel.fact_id == fact_id,
                    MemoryEmbeddingModel.profile_id == self.profile.id,
                    MemoryEmbeddingModel.content_hash == content_hash,
                )
            )
            if existing is not None and not force:
                return False
            now = datetime.now(UTC)
            statement = insert(MemoryEmbeddingJobModel).values(
                fact_id=fact_id,
                profile_id=self.profile.id,
                content_hash=content_hash,
                status=MemoryEmbeddingJobStatus.PENDING.value,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
                error_category=None,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["fact_id", "profile_id"],
                    set_={
                        "content_hash": content_hash,
                        "status": MemoryEmbeddingJobStatus.PENDING.value,
                        "attempts": 0,
                        "next_attempt_at": now,
                        "updated_at": now,
                        "error_category": None,
                    },
                )
            )
        return True

    async def reconcile(self, *, force: bool = False) -> int:
        """Create only missing/stale jobs and recover interrupted processing jobs."""

        async with self._database.sessions() as session:
            facts = tuple(
                (
                    await session.scalars(
                        select(MemoryFactModel).where(
                            MemoryFactModel.status.in_(_EMBEDDABLE_STATUSES),
                            MemoryFactModel.review_state != "quarantined",
                            or_(
                                MemoryFactModel.valid_until.is_(None),
                                MemoryFactModel.valid_until > datetime.now(UTC),
                            ),
                        )
                    )
                ).all()
            )
        created = 0
        for fact in facts:
            created += int(await self.enqueue_fact(fact.id, force=force))
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryEmbeddingJobModel)
                .where(
                    MemoryEmbeddingJobModel.profile_id == self.profile.id,
                    MemoryEmbeddingJobModel.status == MemoryEmbeddingJobStatus.PROCESSING.value,
                )
                .values(
                    status=MemoryEmbeddingJobStatus.PENDING.value,
                    next_attempt_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    error_category="embedding_worker_interrupted",
                )
            )
        return created

    async def claim(self, *, limit: int) -> tuple[MemoryEmbeddingJob, ...]:
        if limit <= 0:
            return ()
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            rows = tuple(
                (
                    await session.scalars(
                        select(MemoryEmbeddingJobModel)
                        .where(
                            MemoryEmbeddingJobModel.profile_id == self.profile.id,
                            MemoryEmbeddingJobModel.status
                            == MemoryEmbeddingJobStatus.PENDING.value,
                            MemoryEmbeddingJobModel.next_attempt_at <= now,
                        )
                        .order_by(MemoryEmbeddingJobModel.id.asc())
                        .limit(limit)
                    )
                ).all()
            )
            for row in rows:
                row.status = MemoryEmbeddingJobStatus.PROCESSING.value
                row.attempts += 1
                row.updated_at = now
        return tuple(self._project(row) for row in rows)

    async def load_active_facts(
        self, jobs: tuple[MemoryEmbeddingJob, ...]
    ) -> dict[int, MemoryFactModel]:
        ids = tuple(job.fact_id for job in jobs)
        if not ids:
            return {}
        async with self._database.sessions() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(MemoryFactModel).where(
                            MemoryFactModel.id.in_(ids),
                            MemoryFactModel.status.in_(_EMBEDDABLE_STATUSES),
                            MemoryFactModel.review_state != "quarantined",
                            or_(
                                MemoryFactModel.valid_until.is_(None),
                                MemoryFactModel.valid_until > datetime.now(UTC),
                            ),
                        )
                    )
                ).all()
            )
        return {row.id: row for row in rows}

    async def complete(self, writes: tuple[EmbeddingWrite, ...]) -> None:
        if not writes:
            return
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            for item in writes:
                job = await session.get(MemoryEmbeddingJobModel, item.job_id)
                fact = await session.get(MemoryFactModel, item.fact_id)
                if (
                    job is None
                    or job.profile_id != self.profile.id
                    or job.content_hash != item.content_hash
                    or job.status != MemoryEmbeddingJobStatus.PROCESSING.value
                ):
                    continue
                valid_until = fact.valid_until if fact is not None else None
                if valid_until is not None and valid_until.tzinfo is None:
                    valid_until = valid_until.replace(tzinfo=UTC)
                if (
                    fact is None
                    or fact.status not in _EMBEDDABLE_STATUSES
                    or (valid_until is not None and valid_until <= now)
                ):
                    job.status = MemoryEmbeddingJobStatus.DONE.value
                    job.updated_at = now
                    job.error_category = None
                    continue
                current_hash = self._hash_row(fact)
                if current_hash != item.content_hash:
                    job.content_hash = current_hash
                    job.status = MemoryEmbeddingJobStatus.PENDING.value
                    job.attempts = 0
                    job.next_attempt_at = now
                    job.updated_at = now
                    job.error_category = None
                    continue
                statement = insert(MemoryEmbeddingModel).values(
                    fact_id=item.fact_id,
                    profile_id=self.profile.id,
                    content_hash=item.content_hash,
                    vector_blob=item.vector_blob,
                    created_at=now,
                    updated_at=now,
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["fact_id", "profile_id"],
                        set_={
                            "content_hash": item.content_hash,
                            "vector_blob": item.vector_blob,
                            "updated_at": now,
                        },
                    )
                )
                await session.execute(
                    update(MemoryEmbeddingJobModel)
                    .where(
                        MemoryEmbeddingJobModel.id == item.job_id,
                        MemoryEmbeddingJobModel.content_hash == item.content_hash,
                        MemoryEmbeddingJobModel.status == MemoryEmbeddingJobStatus.PROCESSING.value,
                    )
                    .values(
                        status=MemoryEmbeddingJobStatus.DONE.value,
                        updated_at=now,
                        error_category=None,
                    )
                )

    async def skip(self, job_id: int) -> None:
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryEmbeddingJobModel)
                .where(MemoryEmbeddingJobModel.id == job_id)
                .values(status="done", updated_at=datetime.now(UTC), error_category=None)
            )

    async def fail(
        self,
        job: MemoryEmbeddingJob,
        *,
        error_category: str,
        retryable: bool,
        max_attempts: int,
        initial_delay_seconds: float,
    ) -> None:
        retry = retryable and job.attempts < max_attempts
        delay = initial_delay_seconds * (2 ** max(0, job.attempts - 1))
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryEmbeddingJobModel)
                .where(MemoryEmbeddingJobModel.id == job.id)
                .values(
                    status="pending" if retry else "failed",
                    next_attempt_at=now + timedelta(seconds=delay) if retry else now,
                    updated_at=now,
                    error_category=error_category[:64],
                )
            )

    async def retry_failed(self) -> int:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(MemoryEmbeddingJobModel)
                .where(
                    MemoryEmbeddingJobModel.profile_id == self.profile.id,
                    MemoryEmbeddingJobModel.status == "failed",
                )
                .values(
                    status="pending",
                    attempts=0,
                    next_attempt_at=now,
                    updated_at=now,
                    error_category=None,
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def pending_count(self) -> int:
        async with self._database.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingJobModel)
                    .where(
                        MemoryEmbeddingJobModel.profile_id == self.profile.id,
                        MemoryEmbeddingJobModel.status == "pending",
                    )
                )
                or 0
            )

    async def delete_for_old_profiles(self) -> int:
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(MemoryEmbeddingJobModel).where(
                    MemoryEmbeddingJobModel.profile_id != self.profile.id
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    @staticmethod
    def _project(row: MemoryEmbeddingJobModel) -> MemoryEmbeddingJob:
        return MemoryEmbeddingJob(
            id=row.id,
            fact_id=row.fact_id,
            profile_id=row.profile_id,
            content_hash=row.content_hash,
            status=row.status,
            attempts=row.attempts,
            next_attempt_at=row.next_attempt_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            error_category=row.error_category,
        )
