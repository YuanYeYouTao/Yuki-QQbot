"""Unified explicit person-memory administration."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.config import Settings
from qq_ai_bot.memory.audit import MemoryAuditService
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.dream.models import DreamClusterPreview, DreamRun, DreamRunPage
from qq_ai_bot.memory.dream.worker import DreamWorker
from qq_ai_bot.memory.embedding.models import MemoryEmbeddingHealth
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.enums import (
    MemoryInvalidationReason,
    MemoryKind,
    MemoryRetrievalMode,
    MemoryScopeType,
    MemoryTargetRole,
)
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.models import (
    MemoryConsistencyHealth,
    MemoryEntityTarget,
    MemoryEvidence,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactStateEvent,
    MemoryIndexHealth,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationResult,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.self_reflection.models import SelfReflectionManualRun
from qq_ai_bot.memory.self_reflection.worker import SelfReflectionWorker
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import ResolvedSubject
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.admin.common import require_self_or_superuser


class MemoryAdminService:
    """Manage explicit memories and bounded automatic-memory retention rules."""

    def __init__(
        self,
        *,
        settings: Settings,
        memories: MemoryFactService,
        audit: AdminAuditService,
        memory_context: MemoryContextService | None = None,
        memory_index: SQLiteMemoryFTSIndex | None = None,
        runtime_config: RuntimeConfigService | None = None,
        memory_embeddings: MemoryEmbeddingRuntime | None = None,
        fact_audit: MemoryAuditService | None = None,
        maintenance: MemoryMaintenanceWorker | None = None,
        mutations: MemoryMutationService | None = None,
        ledger: EventLedgerRepository | None = None,
        self_reflection: SelfReflectionWorker | None = None,
        dream: DreamWorker | None = None,
    ) -> None:
        self._settings = settings
        self._memories = memories
        self._audit = audit
        database = memories.repository.database
        self._memory_index = memory_index or SQLiteMemoryFTSIndex(database)
        self._memory_context = memory_context or MemoryContextService(
            query_builder=MemoryQueryBuilder(MemoryTargetResolver(PeopleRepository(database))),
            retriever=MemoryRetriever(
                repository=memories.repository,
                lexical_index=self._memory_index,
            ),
            facts=memories,
        )
        self._runtime_config = runtime_config or RuntimeConfigService(
            settings=settings,
            database=database,
        )
        self._memory_embeddings = memory_embeddings
        self._fact_audit = fact_audit or MemoryAuditService(memories.repository)
        self._maintenance = maintenance
        self._mutations = mutations
        self._ledger = ledger
        self._self_reflection = self_reflection
        self._dream = dream

    async def list_memories(
        self,
        actor: AdminActor,
        target: str,
    ) -> tuple[MemoryFact, ...]:
        require_self_or_superuser(actor, target, self._settings)
        return await self._memories.list_person(
            target,
            limit=self._settings.person_memory_max_entries,
        )

    async def set_explicit_preference(
        self,
        actor: AdminActor,
        target: str,
        key: str,
        value: str,
        *,
        existing: MemoryFact | None,
    ) -> MemoryFact:
        """Route deterministic preference writes through the mutation boundary."""

        mutation = await self._apply_mutation(
            actor,
            target=ResolvedSubject(MemoryScopeType.PERSON, target, None),
            operation=(
                MemoryMutationOperation.CORRECT
                if existing is not None
                else MemoryMutationOperation.CREATE
            ),
            fact_id=existing.id if existing is not None else None,
            new_content=value,
            memory_key=key,
            category="preference",
            kind=MemoryKind.PREFERENCE,
            reason="deterministic_preference_set",
            confidence=1.0,
            importance=4,
        )
        if mutation is None:
            raise RuntimeError("memory mutation service is unavailable")
        row = await self._mutation_fact(mutation)
        assert row is not None
        return row

    async def delete_explicit_preference(
        self,
        actor: AdminActor,
        target: str,
        existing: MemoryFact,
    ) -> bool:
        """Route deterministic preference deletion through the mutation boundary."""

        mutation = await self._apply_mutation(
            actor,
            target=ResolvedSubject(MemoryScopeType.PERSON, target, None),
            operation=MemoryMutationOperation.INVALIDATE,
            fact_id=existing.id,
            reason="deterministic_preference_delete",
        )
        if mutation is None:
            raise RuntimeError("memory mutation service is unavailable")
        return mutation.ok

    async def add_memory(
        self,
        actor: AdminActor,
        target: str,
        content: str,
        *,
        evidence: MemoryEvidenceCreate | None = None,
    ) -> MemoryFact:
        require_self_or_superuser(actor, target, self._settings)
        normalized = " ".join(content.split()).strip()
        if not normalized:
            raise ValueError("记忆内容不能为空")
        started = time.perf_counter()
        mutation = await self._apply_mutation(
            actor,
            target=ResolvedSubject(MemoryScopeType.PERSON, target, None),
            operation=MemoryMutationOperation.CREATE,
            new_content=normalized,
            memory_key=None,
            category="explicit",
            kind=MemoryKind.FACT,
            reason="deterministic_memory_add",
            confidence=1.0,
            importance=5,
        )
        if mutation is not None:
            row = await self._mutation_fact(mutation)
            assert row is not None
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="add",
                target_type="user",
                target_id=target,
                before=None,
                after={"memory_id": row.id, "content": normalized},
                success=True,
                duration_seconds=time.perf_counter() - started,
            )
            return row
        async with self._audit.transaction() as session:
            if (
                await self._memories.count_person(target, session=session)
                >= self._settings.person_memory_max_entries
            ):
                raise ValueError("人物记忆已达到上限，请先删除或合并旧记忆")
            row = await self._memories.add_explicit_person(
                target,
                normalized,
                limit=self._settings.person_memory_max_entries,
                evidence=evidence,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="add",
                target_type="user",
                target_id=target,
                before=None,
                after={"memory_id": row.id, "content": normalized},
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        await self._memories.schedule_embedding(row.id)
        return row

    async def update_memory(
        self,
        actor: AdminActor,
        target: str,
        memory_id: int,
        content: str,
    ) -> bool:
        require_self_or_superuser(actor, target, self._settings)
        normalized = " ".join(content.split()).strip()
        if not normalized:
            raise ValueError("记忆内容不能为空")
        started = time.perf_counter()
        current = await self._memories.get_fact(memory_id)
        mutation = await self._apply_mutation(
            actor,
            target=ResolvedSubject(MemoryScopeType.PERSON, target, None),
            operation=MemoryMutationOperation.CORRECT,
            fact_id=memory_id,
            new_content=normalized,
            memory_key=current.memory_key if current is not None else None,
            category=current.category if current is not None else None,
            kind=current.kind if current is not None else None,
            reason="deterministic_memory_update",
            confidence=1.0,
            importance=current.importance if current is not None else None,
        )
        if mutation is not None:
            updated = mutation.ok and mutation.new_fact_id is not None
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="update",
                target_type="user",
                target_id=target,
                before={
                    "memory_id": memory_id,
                    "content": current.content if current is not None else None,
                },
                after={"memory_id": mutation.new_fact_id, "content": normalized},
                success=updated,
                error_category=None if updated else mutation.reason_code,
                duration_seconds=time.perf_counter() - started,
            )
            return updated
        async with self._audit.transaction() as session:
            before = next(
                (
                    row
                    for row in await self._memories.list_person(
                        target,
                        limit=self._settings.person_memory_max_entries,
                        session=session,
                    )
                    if row.id == memory_id
                ),
                None,
            )
            updated_row = await self._memories.update_explicit_person(
                memory_id,
                user_id=target,
                content=normalized,
                session=session,
            )
            updated = updated_row is not None
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="update",
                target_type="user",
                target_id=target,
                before={"memory_id": memory_id, "content": before.content if before else None},
                after={"memory_id": memory_id, "content": normalized},
                success=updated,
                error_category=None if updated else "not_found",
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        if updated_row is not None:
            await self._memories.schedule_embedding(updated_row.id)
        return updated

    async def delete_memory(
        self,
        actor: AdminActor,
        target: str,
        memory_id: int,
    ) -> bool:
        require_self_or_superuser(actor, target, self._settings)
        started = time.perf_counter()
        current = await self._memories.get_fact(memory_id)
        mutation = await self._apply_mutation(
            actor,
            target=ResolvedSubject(MemoryScopeType.PERSON, target, None),
            operation=MemoryMutationOperation.INVALIDATE,
            fact_id=memory_id,
            reason="deterministic_memory_delete",
        )
        if mutation is not None:
            deleted = mutation.ok
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="delete",
                target_type="user",
                target_id=target,
                before={
                    "memory_id": memory_id,
                    "content": current.content if current is not None else None,
                },
                after=None,
                success=deleted,
                error_category=None if deleted else mutation.reason_code,
                duration_seconds=time.perf_counter() - started,
            )
            return deleted
        async with self._audit.transaction() as session:
            before = next(
                (
                    row
                    for row in await self._memories.list_person(
                        target,
                        limit=self._settings.person_memory_max_entries,
                        session=session,
                    )
                    if row.id == memory_id
                ),
                None,
            )
            deleted = await self._memories.invalidate_person(
                memory_id,
                user_id=target,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="delete",
                target_type="user",
                target_id=target,
                before={"memory_id": memory_id, "content": before.content if before else None},
                after=None,
                success=deleted,
                error_category=None if deleted else "not_found",
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return deleted

    async def list_evidence(
        self,
        actor: AdminActor,
        target: str,
        memory_id: int,
    ) -> tuple[MemoryEvidence, ...]:
        require_self_or_superuser(actor, target, self._settings)
        fact = next(
            (
                row
                for row in await self._memories.list_person(
                    target,
                    limit=self._settings.person_memory_max_entries,
                )
                if row.id == memory_id
            ),
            None,
        )
        if fact is None:
            return ()
        return await self._memories.list_evidence(memory_id)

    async def prune_memories(
        self,
        actor: AdminActor,
        target: str,
        *,
        max_importance: int,
        older_than_days: int,
    ) -> int:
        """Atomically prune stale low-importance automatic person memories."""

        require_self_or_superuser(actor, target, self._settings)
        if not 1 <= max_importance <= 5:
            raise ValueError("max_importance 必须在 1～5")
        if not 1 <= older_than_days <= 3650:
            raise ValueError("older_than_days 必须在 1～3650")
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        started = time.perf_counter()
        async with self._audit.transaction() as session:
            deleted = await self._memories.prune_person_memories(
                user_id=target,
                max_importance=max_importance,
                older_than=cutoff,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="prune",
                target_type="user",
                target_id=target,
                before=None,
                after={
                    "max_importance": max_importance,
                    "older_than_days": older_than_days,
                    "deleted_count": deleted,
                },
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return deleted

    async def search_person(
        self,
        actor: AdminActor,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> MemoryRetrievalResult:
        require_self_or_superuser(actor, user_id, self._settings)
        runtime = await self._runtime_config.snapshot(user_id=user_id)
        target = MemoryEntityTarget(
            role=MemoryTargetRole.CURRENT_PERSON,
            scope_type=MemoryScopeType.PERSON,
            subject_user_id=user_id,
            block_id="admin_person",
        )
        return await self._memory_context.search(
            text=query,
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(target,),
            runtime=runtime,
            limit=limit,
        )

    async def search_group(
        self,
        actor: AdminActor,
        group_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> MemoryRetrievalResult:
        if not actor.is_superuser or actor.user_id not in self._settings.superusers:
            raise PermissionError("只有超级管理员可以诊断群记忆")
        runtime = await self._runtime_config.snapshot(group_id=group_id)
        target = MemoryEntityTarget(
            role=MemoryTargetRole.CURRENT_GROUP,
            scope_type=MemoryScopeType.GROUP,
            group_id=group_id,
            block_id="admin_group",
        )
        return await self._memory_context.search(
            text=query,
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(target,),
            runtime=runtime,
            limit=limit,
        )

    async def index_status(self, actor: AdminActor) -> MemoryIndexHealth:
        self._require_superuser(actor)
        return await self._memory_index.health()

    async def rebuild_index(self, actor: AdminActor) -> MemoryIndexHealth:
        self._require_superuser(actor)
        started = time.perf_counter()
        health = await self._memory_index.rebuild()
        await self._audit.record(
            actor=actor,
            capability="memory",
            operation="index_rebuild",
            target_type="derived_index",
            target_id="memory_facts_fts",
            before=None,
            after=health.model_dump(),
            success=True,
            duration_seconds=time.perf_counter() - started,
        )
        return health

    async def embedding_status(self, actor: AdminActor) -> MemoryEmbeddingHealth:
        self._require_superuser(actor)
        if self._memory_embeddings is None:
            raise RuntimeError("memory embedding runtime is unavailable")
        return await self._memory_embeddings.health()

    async def embedding_doctor(self, actor: AdminActor) -> int:
        self._require_superuser(actor)
        if self._memory_embeddings is None:
            raise RuntimeError("memory embedding runtime is unavailable")
        return await self._memory_embeddings.doctor()

    async def embedding_retry(self, actor: AdminActor) -> int:
        self._require_superuser(actor)
        if self._memory_embeddings is None:
            raise RuntimeError("memory embedding runtime is unavailable")
        return await self._memory_embeddings.retry()

    async def embedding_rebuild(self, actor: AdminActor) -> int:
        self._require_superuser(actor)
        if self._memory_embeddings is None:
            raise RuntimeError("memory embedding runtime is unavailable")
        return await self._memory_embeddings.rebuild()

    async def embedding_purge_old(self, actor: AdminActor) -> int:
        self._require_superuser(actor)
        if self._memory_embeddings is None:
            raise RuntimeError("memory embedding runtime is unavailable")
        return await self._memory_embeddings.purge_old()

    async def show_fact(self, actor: AdminActor, fact_id: int) -> MemoryFact | None:
        fact = await self._fact_audit.get_fact(fact_id)
        self._require_fact_access(actor, fact)
        return fact

    async def explain_fact(self, actor: AdminActor, fact_id: int) -> dict[str, object] | None:
        fact = await self._fact_audit.get_fact(fact_id)
        self._require_fact_access(actor, fact)
        if fact is None:
            return None
        explanation = await self._fact_audit.explain(fact_id)
        if (
            explanation is not None
            and actor.is_superuser
            and actor.user_id in self._settings.superusers
        ):
            explanation["evidence_sources"] = [
                {
                    "event_id": row.event_id,
                    "source_speaker_user_id": row.source_speaker_user_id,
                    "relation": row.relation.value,
                    "excerpt": row.excerpt,
                }
                for row in await self._fact_audit.get_evidence(fact_id)
            ]
        return explanation

    async def fact_history(
        self,
        actor: AdminActor,
        fact_id: int,
    ) -> tuple[MemoryFactStateEvent, ...]:
        fact = await self._fact_audit.get_fact(fact_id)
        self._require_fact_access(actor, fact)
        return await self._fact_audit.get_state_history(fact_id) if fact is not None else ()

    async def list_conflicts(
        self,
        actor: AdminActor,
        *,
        target_user_id: str | None = None,
    ) -> tuple[MemoryFact, ...]:
        target = target_user_id or actor.user_id
        require_self_or_superuser(actor, target, self._settings)
        return await self._fact_audit.list_conflicts(subject_user_id=target)

    async def correct_fact(
        self,
        actor: AdminActor,
        fact_id: int,
        content: str,
    ) -> MemoryFact | None:
        fact = await self._fact_audit.get_fact(fact_id)
        self._require_fact_mutation(actor, fact)
        if fact is not None:
            mutation = await self._apply_mutation(
                actor,
                target=ResolvedSubject(
                    fact.scope_type,
                    fact.subject_user_id,
                    fact.group_id,
                ),
                operation=MemoryMutationOperation.CORRECT,
                fact_id=fact_id,
                new_content=content,
                memory_key=fact.memory_key,
                category=fact.category,
                kind=fact.kind,
                reason="memory_fact_correct_command",
                confidence=1.0,
                importance=fact.importance,
            )
            if mutation is not None:
                return await self._mutation_fact(mutation, required=False)
        return await self._memories.correct_fact(
            fact_id,
            content=content,
            actor_user_id=actor.user_id,
        )

    async def invalidate_fact(
        self,
        actor: AdminActor,
        fact_id: int,
        reason: str | None = None,
    ) -> bool:
        fact = await self._fact_audit.get_fact(fact_id)
        self._require_fact_mutation(actor, fact)
        selected = (
            MemoryInvalidationReason.ADMINISTRATOR_INVALIDATED
            if actor.is_superuser
            else MemoryInvalidationReason.USER_RETRACTED
        )
        if reason and actor.is_superuser:
            selected = MemoryInvalidationReason(reason)
        if fact is not None:
            mutation = await self._apply_mutation(
                actor,
                target=ResolvedSubject(
                    fact.scope_type,
                    fact.subject_user_id,
                    fact.group_id,
                ),
                operation=MemoryMutationOperation.INVALIDATE,
                fact_id=fact_id,
                reason=selected.value,
            )
            if mutation is not None:
                return mutation.ok
        return await self._memories.invalidate_fact(
            fact_id,
            reason=selected,
            actor_user_id=actor.user_id,
        )

    async def restore_fact(self, actor: AdminActor, fact_id: int) -> MemoryFact | None:
        fact = await self._fact_audit.get_fact(fact_id)
        self._require_fact_mutation(actor, fact)
        if (
            fact is not None
            and not actor.is_superuser
            and fact.invalidated_reason
            not in {
                MemoryInvalidationReason.USER_RETRACTED,
                MemoryInvalidationReason.PLUGIN_EXPLICIT_INVALIDATION,
            }
        ):
            raise PermissionError("只能恢复由本人撤回的记忆")
        if fact is not None:
            mutation = await self._apply_mutation(
                actor,
                target=ResolvedSubject(
                    fact.scope_type,
                    fact.subject_user_id,
                    fact.group_id,
                ),
                operation=MemoryMutationOperation.RESTORE,
                fact_id=fact_id,
                reason="memory_fact_restore_command",
            )
            if mutation is not None:
                return await self._mutation_fact(mutation, required=False)
        return await self._memories.restore_fact(fact_id, actor_user_id=actor.user_id)

    async def merge_facts(
        self,
        actor: AdminActor,
        source_fact_id: int,
        target_fact_id: int,
    ) -> MemoryFact | None:
        self._require_superuser(actor)
        source = await self._fact_audit.get_fact(source_fact_id)
        if source is not None:
            mutation = await self._apply_mutation(
                actor,
                target=ResolvedSubject(
                    source.scope_type,
                    source.subject_user_id,
                    source.group_id,
                ),
                operation=MemoryMutationOperation.MERGE,
                fact_id=source_fact_id,
                merge_fact_id=target_fact_id,
                reason="memory_fact_merge_command",
            )
            if mutation is not None:
                return await self._mutation_fact(mutation, required=False)
        return await self._memories.merge_facts(
            source_fact_id,
            target_fact_id,
            actor_user_id=actor.user_id,
        )

    async def resolve_conflicts(
        self,
        actor: AdminActor,
        preferred_fact_id: int,
        contested_fact_ids: tuple[int, ...],
    ) -> int:
        self._require_superuser(actor)
        return await self._memories.resolve_conflicts(
            preferred_fact_id,
            contested_fact_ids,
            actor_user_id=actor.user_id,
        )

    async def consistency_health(self, actor: AdminActor) -> MemoryConsistencyHealth:
        self._require_superuser(actor)
        return await self._fact_audit.health()

    async def maintenance_status(
        self,
        actor: AdminActor,
    ) -> tuple[bool, MemoryConsistencyHealth]:
        self._require_superuser(actor)
        health = await self._fact_audit.health()
        return bool(self._maintenance and self._maintenance.running), health

    async def maintenance_run(self, actor: AdminActor) -> int:
        self._require_superuser(actor)
        if self._maintenance is None:
            raise RuntimeError("memory maintenance worker is unavailable")
        return await self._maintenance.process_once()

    async def self_reflection_run(self, actor: AdminActor) -> SelfReflectionManualRun:
        """Run one bounded manual SELF reflection cycle for a real superuser."""

        self._require_superuser(actor)
        if self._self_reflection is None:
            raise RuntimeError("Self Reflection Worker 当前不可用")
        cycle = await self._self_reflection.run_now()
        return SelfReflectionManualRun(
            attempted_batches=cycle.attempted_batches,
            completed_batches=cycle.completed_batches,
            failed_batches=cycle.failed_batches,
            proposal_count=cycle.proposal_count,
            committed_count=cycle.committed_count,
            health=await self._self_reflection.health(),
            max_daily_calls=self._settings.memory_self_reflection_max_daily_calls,
        )

    async def dream_plan(self, actor: AdminActor) -> DreamRun:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.plan_full(actor_user_id=actor.user_id)

    async def dream_start(self, actor: AdminActor, public_id: str) -> DreamRun:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.start_run(public_id)

    async def dream_list(self, actor: AdminActor) -> tuple[DreamRun, ...]:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.list_runs()

    async def dream_status(self, actor: AdminActor, public_id: str) -> DreamRun | None:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.status(public_id)

    async def dream_show(
        self,
        actor: AdminActor,
        public_id: str,
        *,
        page: int = 1,
    ) -> DreamRunPage:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream 未初始化")
        return await self._dream.show(public_id, page=page)

    async def dream_preview(
        self,
        actor: AdminActor,
        public_id: str,
        *,
        cluster_id: int,
    ) -> DreamClusterPreview:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream 未初始化")
        return await self._dream.preview(public_id, cluster_id=cluster_id)

    async def dream_cancel(self, actor: AdminActor, public_id: str) -> bool:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.cancel(public_id)

    async def dream_resume(self, actor: AdminActor, public_id: str) -> DreamRun:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.resume(public_id)

    async def dream_retry(self, actor: AdminActor, public_id: str) -> DreamRun:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.retry(public_id)

    async def dream_rollback_operation(self, actor: AdminActor, public_id: str) -> bool:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.rollback_operation(public_id)

    async def dream_rollback_run(self, actor: AdminActor, public_id: str) -> int:
        self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.rollback_run(public_id)

    async def _apply_mutation(
        self,
        actor: AdminActor,
        *,
        target: ResolvedSubject,
        operation: MemoryMutationOperation,
        reason: str,
        fact_id: int | None = None,
        merge_fact_id: int | None = None,
        new_content: str | None = None,
        memory_key: str | None = None,
        category: str | None = None,
        kind: MemoryKind | None = None,
        confidence: float = 1.0,
        importance: int | None = None,
    ) -> MemoryMutationResult | None:
        if self._mutations is None and self._ledger is None:
            return None
        if self._mutations is None or self._ledger is None:
            raise RuntimeError("memory mutation dependencies are incomplete")
        if not actor.bot_user_id:
            raise RuntimeError("memory mutation is not bound to a real Bot event")
        event = await self._ledger.find_by_platform_message(
            bot_user_id=actor.bot_user_id,
            platform_message_id=actor.trigger_message_id,
        )
        if event is None or event.sender_user_id != actor.user_id:
            raise RuntimeError("memory mutation trigger event cannot be verified")
        try:
            decision_actor_type = MemoryDecisionActorType(actor.decision_actor_type)
        except ValueError:
            decision_actor_type = MemoryDecisionActorType.ADMIN
        quote_source = event.content
        preferred_quote = " ".join((new_content or "").split()).strip()
        evidence_quote = (
            preferred_quote
            if preferred_quote and len(preferred_quote) <= 500 and preferred_quote in quote_source
            else quote_source[:500]
        )
        effective_key = memory_key
        if operation is MemoryMutationOperation.CREATE and not effective_key:
            effective_key = f"explicit:{event.id}"
        request = MemoryMutationRequest(
            operation=operation,
            fact_id=fact_id,
            merge_fact_id=merge_fact_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=target.scope_type,
            ),
            new_content=new_content,
            memory_key=effective_key,
            category=category,
            kind=kind,
            reason=reason,
            confidence=confidence,
            importance=importance,
            evidence_quote=evidence_quote,
        )
        return await self._mutations.mutate_resolved(
            request,
            MemoryMutationContext(
                event=event,
                conversation_key=actor.conversation_key,
                turn_origin=event.origin,
                delegation_mode=decision_actor_type.value,
                trigger_actor_user_id=event.sender_user_id,
                decision_actor_type=decision_actor_type,
                decision_actor_id=actor.decision_actor_id or actor.user_id,
                executed_by_bot_user_id=event.bot_user_id,
                actor_is_superuser=(
                    actor.is_superuser and actor.user_id in self._settings.superusers
                ),
            ),
            target=target,
        )

    async def _mutation_fact(
        self,
        mutation: MemoryMutationResult,
        *,
        required: bool = True,
    ) -> MemoryFact | None:
        fact_id = mutation.new_fact_id or mutation.old_fact_id
        row = await self._memories.get_fact(fact_id) if fact_id is not None else None
        if row is None and required:
            raise ValueError(f"记忆变更未提交：{mutation.reason_code}")
        return row

    def _require_fact_access(self, actor: AdminActor, fact: MemoryFact | None) -> None:
        if fact is None:
            return
        if actor.is_superuser and actor.user_id in self._settings.superusers:
            return
        if fact.subject_user_id != actor.user_id:
            raise PermissionError("只能查看与本人有关的人物记忆")

    def _require_fact_mutation(self, actor: AdminActor, fact: MemoryFact | None) -> None:
        self._require_fact_access(actor, fact)
        if fact is not None and fact.scope_type is MemoryScopeType.GROUP and not actor.is_superuser:
            raise PermissionError("普通用户不能修改群共同事实")

    def _require_superuser(self, actor: AdminActor) -> None:
        if not actor.is_superuser or actor.user_id not in self._settings.superusers:
            raise PermissionError("只有超级管理员可以执行此记忆管理操作")
