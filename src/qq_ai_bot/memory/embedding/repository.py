"""Persistence for immutable profiles and target-scoped derived vectors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.memory.embedding.models import (
    EmbeddingProviderProfile,
    MemoryEmbeddingProfileRecord,
)
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
from qq_ai_bot.memory.models import MemoryEntityTarget
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryEmbeddingJobModel,
    MemoryEmbeddingModel,
    MemoryEmbeddingProfileModel,
    MemoryFactModel,
)


@dataclass(frozen=True, slots=True)
class StoredTargetVector:
    fact_id: int
    content_hash: str
    vector_blob: bytes
    kind: str
    category: str
    memory_key: str
    content: str


class MemoryEmbeddingRepository:
    """Never loads vectors before SQL has enforced the exact identity target."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    async def ensure_profile(
        self, profile: EmbeddingProviderProfile
    ) -> MemoryEmbeddingProfileRecord:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                insert(MemoryEmbeddingProfileModel)
                .values(
                    fingerprint=profile.fingerprint,
                    provider_id=profile.provider_id,
                    model_id=profile.model_id,
                    dimensions=profile.dimensions,
                    output_type=profile.output_type,
                    document_template_version=profile.document_template_version,
                    endpoint_identity=profile.endpoint_identity,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["fingerprint"])
            )
            row = await session.scalar(
                select(MemoryEmbeddingProfileModel).where(
                    MemoryEmbeddingProfileModel.fingerprint == profile.fingerprint
                )
            )
            assert row is not None
        return MemoryEmbeddingProfileRecord(id=row.id, profile=profile, created_at=row.created_at)

    async def load_target_vectors(
        self,
        *,
        target: MemoryEntityTarget,
        profile_id: int,
        kinds: tuple[str, ...],
    ) -> tuple[StoredTargetVector, ...]:
        conditions: list[Any] = [
            MemoryEmbeddingModel.profile_id == profile_id,
            MemoryFactModel.status == "active",
            MemoryFactModel.review_state != "quarantined",
            MemoryFactModel.scope_type == target.scope_type.value,
            or_(
                MemoryFactModel.valid_until.is_(None),
                MemoryFactModel.valid_until > datetime.now(UTC),
            ),
        ]
        conditions.append(
            MemoryFactModel.subject_user_id.is_(None)
            if target.subject_user_id is None
            else MemoryFactModel.subject_user_id == target.subject_user_id
        )
        conditions.append(
            MemoryFactModel.group_id.is_(None)
            if target.group_id is None
            else MemoryFactModel.group_id == target.group_id
        )
        if target.scope_type.value == "self":
            current_visibility = and_(
                MemoryFactModel.visibility_type
                == (target.visibility_type.value if target.visibility_type else ""),
                (
                    MemoryFactModel.visibility_user_id.is_(None)
                    if target.visibility_user_id is None
                    else MemoryFactModel.visibility_user_id == target.visibility_user_id
                ),
                (
                    MemoryFactModel.visibility_group_id.is_(None)
                    if target.visibility_group_id is None
                    else MemoryFactModel.visibility_group_id == target.visibility_group_id
                ),
            )
            conditions.append(or_(MemoryFactModel.visibility_type == "global", current_visibility))
        if kinds:
            conditions.append(MemoryFactModel.kind.in_(kinds))
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        MemoryFactModel.id,
                        MemoryEmbeddingModel.content_hash,
                        MemoryEmbeddingModel.vector_blob,
                        MemoryFactModel.kind,
                        MemoryFactModel.category,
                        MemoryFactModel.memory_key,
                        MemoryFactModel.content,
                    )
                    .join(MemoryEmbeddingModel, MemoryEmbeddingModel.fact_id == MemoryFactModel.id)
                    .where(*conditions)
                    .order_by(MemoryFactModel.id.asc())
                )
            ).all()
        return tuple(
            StoredTargetVector(
                fact_id=int(row.id),
                content_hash=str(row.content_hash),
                vector_blob=bytes(row.vector_blob),
                kind=str(row.kind),
                category=str(row.category),
                memory_key=str(row.memory_key),
                content=str(row.content),
            )
            for row in rows
        )

    async def purge_old_profiles(self, *, current_profile_id: int) -> int:
        async with self._database.sessions() as session, session.begin():
            old_ids = select(MemoryEmbeddingProfileModel.id).where(
                MemoryEmbeddingProfileModel.id != current_profile_id
            )
            count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingProfileModel)
                    .where(MemoryEmbeddingProfileModel.id != current_profile_id)
                )
                or 0
            )
            await session.execute(
                delete(MemoryEmbeddingProfileModel).where(
                    MemoryEmbeddingProfileModel.id.in_(old_ids)
                )
            )
        return count

    async def load_vectors_for_fact_ids(
        self,
        *,
        fact_ids: tuple[int, ...],
        profile_id: int,
    ) -> dict[int, bytes]:
        if not fact_ids:
            return {}
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(MemoryEmbeddingModel.fact_id, MemoryEmbeddingModel.vector_blob).where(
                        MemoryEmbeddingModel.profile_id == profile_id,
                        MemoryEmbeddingModel.fact_id.in_(fact_ids),
                    )
                )
            ).all()
        return {int(row.fact_id): bytes(row.vector_blob) for row in rows}

    async def active_fact_count(self) -> int:
        async with self._database.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryFactModel)
                    .where(
                        MemoryFactModel.status == "active",
                        MemoryFactModel.review_state != "quarantined",
                        or_(
                            MemoryFactModel.valid_until.is_(None),
                            MemoryFactModel.valid_until > datetime.now(UTC),
                        ),
                    )
                )
                or 0
            )

    async def local_health(
        self,
        *,
        current_profile_id: int,
        documents: EmbeddingDocumentBuilder,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        active = [
            MemoryFactModel.status == "active",
            MemoryFactModel.review_state != "quarantined",
            or_(MemoryFactModel.valid_until.is_(None), MemoryFactModel.valid_until > now),
        ]
        async with self._database.sessions() as session:
            active_count = int(
                await session.scalar(
                    select(func.count()).select_from(MemoryFactModel).where(*active)
                )
                or 0
            )
            active_embedding_rows = (
                await session.execute(
                    select(
                        MemoryEmbeddingModel.content_hash,
                        MemoryFactModel.kind,
                        MemoryFactModel.category,
                        MemoryFactModel.memory_key,
                        MemoryFactModel.content,
                    )
                    .join(MemoryFactModel, MemoryFactModel.id == MemoryEmbeddingModel.fact_id)
                    .where(MemoryEmbeddingModel.profile_id == current_profile_id, *active)
                )
            ).all()
            matching_count = sum(
                1
                for row in active_embedding_rows
                if row.content_hash
                == documents.content_hash_fields(
                    kind=row.kind,
                    category=row.category,
                    memory_key=row.memory_key,
                    content=row.content,
                )
            )
            inactive_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingModel)
                    .join(MemoryFactModel, MemoryFactModel.id == MemoryEmbeddingModel.fact_id)
                    .where(
                        MemoryEmbeddingModel.profile_id == current_profile_id,
                        MemoryFactModel.status.not_in(("active", "contested")),
                    )
                )
                or 0
            )
            orphan_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingModel)
                    .outerjoin(MemoryFactModel, MemoryFactModel.id == MemoryEmbeddingModel.fact_id)
                    .where(MemoryFactModel.id.is_(None))
                )
                or 0
            )
            job_count_rows = (
                await session.execute(
                    select(MemoryEmbeddingJobModel.status, func.count())
                    .where(MemoryEmbeddingJobModel.profile_id == current_profile_id)
                    .group_by(MemoryEmbeddingJobModel.status)
                )
            ).all()
            job_counts: dict[str, int] = {
                str(status): int(count) for status, count in job_count_rows
            }
            old_profile_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryEmbeddingProfileModel)
                    .where(MemoryEmbeddingProfileModel.id != current_profile_id)
                )
                or 0
            )
            last_success = await session.scalar(
                select(func.max(MemoryEmbeddingModel.updated_at)).where(
                    MemoryEmbeddingModel.profile_id == current_profile_id
                )
            )
            last_error = await session.scalar(
                select(MemoryEmbeddingJobModel.error_category)
                .where(
                    MemoryEmbeddingJobModel.profile_id == current_profile_id,
                    MemoryEmbeddingJobModel.error_category.is_not(None),
                )
                .order_by(MemoryEmbeddingJobModel.updated_at.desc())
                .limit(1)
            )
        return {
            "active_fact_count": active_count,
            "ready_embedding_count": matching_count,
            "pending_job_count": int(job_counts.get("pending", 0)),
            "processing_job_count": int(job_counts.get("processing", 0)),
            "failed_job_count": int(job_counts.get("failed", 0)),
            "stale_embedding_count": inactive_count + (len(active_embedding_rows) - matching_count),
            "orphan_embedding_count": orphan_count,
            "old_profile_count": old_profile_count,
            "last_success_at": last_success,
            "last_error_category": last_error,
        }
