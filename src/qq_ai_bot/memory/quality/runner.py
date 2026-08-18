"""Isolated deterministic runner using the production Memory V2 services."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from qq_ai_bot import __version__
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.context import MemoryContextService, retrieval_fact_context
from qq_ai_bot.memory.embedding.fake import FakeEmbeddingProvider
from qq_ai_bot.memory.embedding.jobs import MemoryEmbeddingJobRepository
from qq_ai_bot.memory.embedding.provider import EmbeddingProvider
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.semantic import MemorySemanticIndex
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder, EmbeddingQueryBuilder
from qq_ai_bot.memory.embedding.worker import MemoryEmbeddingWorker
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryKind,
    MemoryRetrievalMode,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
    MemoryTargetRole,
)
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.models import MemoryEntityTarget, MemoryFact, MemoryFactCreate
from qq_ai_bot.memory.quality.database import migrate_sqlite_database
from qq_ai_bot.memory.quality.evaluator import (
    MemoryQualityEvaluator,
    evidence_key,
    fact_key,
    relation_key,
)
from qq_ai_bot.memory.quality.fake import (
    CountingEmbeddingProvider,
    CountingModelExecutor,
    QualityFakeModel,
)
from qq_ai_bot.memory.quality.gates import (
    GateConfiguration,
    compare_baseline,
    evaluate_gates,
)
from qq_ai_bot.memory.quality.loader import LoadedQualitySuite
from qq_ai_bot.memory.quality.metrics import MemoryQualityMetrics
from qq_ai_bot.memory.quality.models import (
    MemoryQualityCase,
    MemoryQualityReport,
    QualityBaseline,
    QualityClaim,
    QualityEvidenceSpec,
    QualityFactSpec,
    QualityObservation,
    QualityQuerySpec,
    QualityRebuildExpectation,
    QualityRelationSpec,
    QualitySuiteMode,
)
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService
from qq_ai_bot.memory.rebuild.worker import MemoryRebuildWorker
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.memory.worker import MemoryWorker
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryFactModel
from qq_ai_bot.persistence.repositories import EventLedgerRepository, PeopleRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager

_MODE_CATEGORIES: dict[QualitySuiteMode, frozenset[str]] = {
    QualitySuiteMode.STRUCTURAL: frozenset({"identity", "privacy", "idempotency"}),
    QualitySuiteMode.PIPELINE: frozenset(
        {"identity", "extraction", "third_party", "correction", "conflict", "temporal"}
    ),
    QualitySuiteMode.RETRIEVAL: frozenset({"retrieval", "identity", "privacy", "temporal"}),
    QualitySuiteMode.CONTEXT: frozenset({"context", "identity", "privacy", "third_party"}),
    QualitySuiteMode.REBUILD: frozenset({"rebuild"}),
    QualitySuiteMode.FULL: frozenset(),
}


class MemoryQualityRunner:
    def __init__(
        self,
        *,
        suite: LoadedQualitySuite,
        gates: GateConfiguration,
        repository_root: Path,
        model_executor: ModelExecutor | None = None,
        model_provider_id: str | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._suite = suite
        self._gates = gates
        self._root = repository_root
        self._evaluator = MemoryQualityEvaluator()
        self._metrics = MemoryQualityMetrics()
        self._model_executor = (
            CountingModelExecutor(model_executor) if model_executor is not None else None
        )
        self._model_provider_id = model_provider_id
        self._embedding_provider = (
            CountingEmbeddingProvider(embedding_provider)
            if embedding_provider is not None
            else None
        )

    async def run(
        self,
        *,
        mode: QualitySuiteMode = QualitySuiteMode.FULL,
        baseline: QualityBaseline | None = None,
    ) -> MemoryQualityReport:
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        categories = _MODE_CATEGORIES[mode]
        cases = tuple(
            item
            for item in self._suite.cases
            if mode is QualitySuiteMode.FULL or item.category in categories
        )
        with tempfile.TemporaryDirectory(prefix="yuki-memory-quality-") as temporary:
            root = Path(temporary)
            template = root / "template.db"
            await asyncio.to_thread(migrate_sqlite_database, self._root, template)
            observations = []
            for index, case in enumerate(cases, start=1):
                database_path = root / f"case-{index:04d}.db"
                await asyncio.to_thread(shutil.copy2, template, database_path)
                observations.append(await self._run_case(case, database_path))
        results = tuple(
            self._evaluator.evaluate(case, observation)
            for case, observation in zip(cases, observations, strict=True)
        )
        metrics = self._metrics.calculate(cases, results)
        gate_results = evaluate_gates(
            metrics,
            self._gates,
            allow_not_applicable=mode is not QualitySuiteMode.FULL,
        )
        regressions = (
            compare_baseline(metrics, baseline, self._gates) if baseline is not None else ()
        )
        return MemoryQualityReport(
            suite_version=self._suite.manifest.suite_version,
            suite_mode=mode,
            commit=self._commit(),
            python_version=sys.version.split()[0],
            sqlite_version=sqlite3.sqlite_version,
            dataset_hash=self._suite.computed_hash,
            gate_config_hash=self._gates.file_hash,
            fake_model_id=self._suite.manifest.fake_model_id,
            fake_embedding_id=self._suite.manifest.fake_embedding_id,
            deterministic=self._model_executor is None and self._embedding_provider is None,
            model_provider_id=self._model_provider_id,
            embedding_provider_id=(
                self._embedding_provider.profile.fingerprint
                if self._embedding_provider is not None
                else None
            ),
            started_at=started_at,
            duration_seconds=time.perf_counter() - started,
            case_count=len(results),
            passed_count=sum(item.passed for item in results),
            failed_count=sum(not item.passed for item in results),
            cases=results,
            metrics=metrics,
            gates=gate_results,
            baseline_regressions=regressions,
        )

    async def _run_case(self, case: MemoryQualityCase, path: Path) -> QualityObservation:
        database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
        started = time.perf_counter()
        try:
            return await self._execute_case(case, database, started)
        except Exception as exc:
            return QualityObservation(
                case_id=case.case_id,
                error_code=type(exc).__name__,
                extraction_latency_ms=(time.perf_counter() - started) * 1000,
            )
        finally:
            await database.close()

    async def _execute_case(
        self,
        case: MemoryQualityCase,
        database: Database,
        started: float,
    ) -> QualityObservation:
        symbols = self._suite.manifest.symbolic_identities
        reverse_symbols = {value: key for key, value in symbols.items()}
        repository = MemoryFactRepository(database)
        facts = MemoryFactService(repository)
        ledger = EventLedgerRepository(database)
        for spec in case.initial_facts:
            await facts.remember(self._fact_create(spec, symbols))
        initial_rows = await self._all_facts(database, repository)
        initial_refs = self._map_fact_refs(initial_rows, case.initial_facts, symbols)
        initial_ids = {fact_ref: fact_id for fact_id, fact_ref in initial_refs.items()}
        async with repository.transaction() as session:
            for initial_relation in case.initial_relations:
                await repository.add_relation(
                    source_fact_id=initial_ids[initial_relation.source_fact_ref],
                    target_fact_id=initial_ids[initial_relation.target_fact_ref],
                    relation_type=MemoryFactRelationType(initial_relation.relation_type),
                    confidence=1.0,
                    source_event_id=None,
                    session=session,
                )
        initial_state = {
            item.id: (item.status, item.last_confirmed_at)
            for item in initial_rows
            if item.status is MemoryStatus.ACTIVE
        }

        event_ids: dict[str, int] = {}
        outputs: dict[str, tuple[dict[str, object], ...]] = {}
        for fixture in case.events:
            segments: tuple[dict[str, object], ...] = (
                {
                    "type": "yuki_context",
                    "data": {
                        "mentioned_user_ids": [symbols[item] for item in fixture.mentioned],
                        "reply_sender_user_id": (
                            symbols[fixture.reply_speaker] if fixture.reply_speaker else None
                        ),
                    },
                },
            )
            event, _ = await ledger.append(
                bot_user_id=symbols["bot"],
                platform_message_id=f"quality:{case.case_id}:{fixture.event_ref}",
                scope_type=(
                    ScopeType.GROUP if fixture.scope_type == "group" else ScopeType.PRIVATE
                ),
                sender_user_id=symbols[fixture.speaker],
                direction=fixture.direction,
                content=fixture.content,
                segments=segments,
                group_id=symbols[fixture.group] if fixture.group else None,
                private_peer_user_id=(
                    symbols[fixture.speaker] if fixture.scope_type == "private" else None
                ),
                occurred_at=fixture.occurred_at,
                sender_is_bot=fixture.speaker == "bot",
            )
            event_ids[fixture.event_ref] = event.id
            # Quality fixtures must not attach ConversationHistoryService; this ledger
            # is constructed without a history observer so Flash jobs stay empty.
            claims = tuple(
                self._claim_payload(item)
                for item in case.fake_model_outputs
                if item.event_ref == fixture.event_ref
            )
            outputs[fixture.content] = claims

        provider = QualityFakeModel(outputs)
        extraction_before = (
            self._model_executor.count(ModelTask.MEMORY_EXTRACTION)
            if self._model_executor is not None
            else 0
        )
        consolidation_before = (
            self._model_executor.count(ModelTask.MEMORY_CONSOLIDATION)
            if self._model_executor is not None
            else 0
        )
        jobs = MemoryJobRepository(database)
        if case.category != "rebuild":
            for fixture in case.events:
                await jobs.enqueue(
                    event_ids[fixture.event_ref],
                    (
                        f"group:{symbols[fixture.group]}"
                        if fixture.group
                        else f"private:{symbols[fixture.speaker]}"
                    ),
                )
        settings = self._settings(
            database.url,
            superuser=symbols["person_a"],
            consolidation_enabled=self._model_executor is not None,
        )
        worker = MemoryWorker(
            settings=settings,
            jobs=jobs,
            facts=facts,
            ledger=ledger,
            provider=provider if self._model_executor is None else None,
            model_executor=self._model_executor,
            concurrency=ConcurrencyManager(1),
        )
        pipeline_started = time.perf_counter()
        rebuild_observation = None
        if case.category == "rebuild":
            rebuild_observation = await self._execute_rebuild(
                case=case,
                settings=settings,
                database=database,
                ledger=ledger,
                live_worker=worker,
                facts=facts,
                actor_user_id=symbols["person_a"],
                initial_state=initial_state,
            )
        else:
            while await worker.process_once():
                pass
        pipeline_latency_ms = (time.perf_counter() - pipeline_started) * 1000

        rows = await self._all_facts(database, repository)
        specs = tuple(dict.fromkeys((*case.initial_facts, *case.expected_facts)))
        fact_refs = self._map_fact_refs(rows, specs, symbols)
        event_refs = {value: key for key, value in event_ids.items()}
        observed_fact_keys = tuple(
            self._observed_fact_key(row, fact_refs.get(row.id), reverse_symbols) for row in rows
        )
        observed_evidence: list[str] = []
        observed_claim_keys: set[str] = set()
        observed_relations: set[str] = set()
        for row in rows:
            ref = fact_refs.get(row.id, self._unmapped_ref(row, reverse_symbols))
            for evidence in await repository.list_evidence(row.id):
                event_ref = (
                    event_refs.get(evidence.event_id, "event_unknown")
                    if evidence.event_id is not None
                    else "tool_receipt"
                )
                observed_evidence.append(
                    evidence_key(
                        QualityEvidenceSpec(
                            fact_ref=ref,
                            event_ref=event_ref,
                            speaker=reverse_symbols.get(
                                evidence.source_speaker_user_id,
                                "unknown_speaker",
                            ),
                            excerpt=evidence.excerpt,
                            relation=evidence.relation.value,
                        )
                    )
                )
                symbolic_subject = (
                    reverse_symbols.get(row.group_id or "", "unknown_group")
                    if row.scope_type is MemoryScopeType.GROUP
                    else reverse_symbols.get(row.subject_user_id or "", "unknown_person")
                )
                observed_claim_keys.add(
                    "|".join(
                        (
                            event_ref,
                            row.scope_type.value,
                            symbolic_subject,
                            row.memory_key,
                            row.content,
                        )
                    )
                )
            for relation in await repository.list_relations(row.id):
                source = fact_refs.get(relation.source_fact_id)
                target = fact_refs.get(relation.target_fact_id)
                if source and target:
                    observed_relations.add(
                        relation_key(
                            QualityRelationSpec(
                                source_fact_ref=source,
                                target_fact_ref=target,
                                relation_type=relation.relation_type.value,
                            )
                        )
                    )

        embedding_queries_before = (
            self._embedding_provider.query_requests if self._embedding_provider is not None else 0
        )
        retriever, embedding_provider = await self._retriever(
            case,
            database,
            facts,
            rows,
            fact_refs,
        )
        runtime_config = RuntimeConfigService(settings=settings, database=database)
        await runtime_config.initialize()
        runtime = await runtime_config.snapshot()
        context_service = MemoryContextService(
            query_builder=MemoryQueryBuilder(MemoryTargetResolver(PeopleRepository(database))),
            retriever=retriever,
            facts=facts,
        )
        retrieval_keys: list[str] = []
        context_keys: list[str] = []
        retrieval_latencies: list[float] = []
        context_latencies: list[float] = []
        context_characters = 0
        for query in case.queries:
            entity_target = self._target(query, symbols)
            retrieval_started = time.perf_counter()
            result = await context_service.search(
                text=query.text,
                mode=MemoryRetrievalMode.RELEVANT,
                targets=(entity_target,),
                runtime=runtime,
                limit=query.limit,
            )
            retrieval_latencies.append((time.perf_counter() - retrieval_started) * 1000)
            refs = [
                fact_refs.get(hit.fact.id, self._unmapped_ref(hit.fact, reverse_symbols))
                for hit in result.hits
            ]
            retrieval_keys.extend(f"{query.query_ref}|{item}" for item in refs)
            if query.context:
                context_started = time.perf_counter()
                projected = [retrieval_fact_context(hit) for hit in result.hits]
                context_characters += len(
                    json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
                )
                context_keys.extend(f"{query.query_ref}|{item}" for item in refs)
                context_latencies.append((time.perf_counter() - context_started) * 1000)

        extraction_requests = (
            self._model_executor.count(ModelTask.MEMORY_EXTRACTION) - extraction_before
            if self._model_executor is not None
            else provider.extraction_requests
        )
        consolidation_requests = (
            self._model_executor.count(ModelTask.MEMORY_CONSOLIDATION) - consolidation_before
            if self._model_executor is not None
            else provider.classification_requests
        )
        query_embedding_requests = (
            self._embedding_provider.query_requests - embedding_queries_before
            if self._embedding_provider is not None
            else embedding_provider.query_requests
            if isinstance(embedding_provider, FakeEmbeddingProvider)
            else 0
        )
        return QualityObservation(
            case_id=case.case_id,
            claims=tuple(sorted(observed_claim_keys)),
            facts=tuple(sorted(observed_fact_keys)),
            evidence=tuple(sorted(set(observed_evidence))),
            relations=tuple(sorted(observed_relations)),
            retrieval=tuple(retrieval_keys),
            context=tuple(context_keys),
            rebuild=rebuild_observation,
            extraction_requests=extraction_requests,
            consolidation_requests=consolidation_requests,
            query_embedding_requests=query_embedding_requests,
            context_characters=context_characters,
            extraction_latency_ms=pipeline_latency_ms,
            retrieval_latency_ms=tuple(retrieval_latencies),
            context_latency_ms=tuple(context_latencies),
        )

    async def _retriever(
        self,
        case: MemoryQualityCase,
        database: Database,
        facts: MemoryFactService,
        rows: tuple[MemoryFact, ...],
        fact_refs: dict[int, str],
    ) -> tuple[MemoryRetriever, EmbeddingProvider | None]:
        semantic_queries = tuple(item for item in case.queries if item.semantic)
        if not semantic_queries:
            return (
                MemoryRetriever(
                    repository=facts.repository,
                    lexical_index=SQLiteMemoryFTSIndex(database),
                ),
                None,
            )
        documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4000)
        vectors: dict[str, tuple[float, ...]] = {}
        dimensions = max(4, len(semantic_queries) + 1)
        for index, query in enumerate(semantic_queries):
            vector = tuple(1.0 if slot == index else 0.0 for slot in range(dimensions))
            vectors[query.text] = vector
            for row in rows:
                if fact_refs.get(row.id) in query.expected_fact_refs:
                    vectors[documents.build(row)] = vector
        provider: EmbeddingProvider = self._embedding_provider or FakeEmbeddingProvider(
            dimensions=dimensions,
            vectors=vectors,
        )
        embeddings = MemoryEmbeddingRepository(database)
        profile = await embeddings.ensure_profile(provider.profile)
        jobs = MemoryEmbeddingJobRepository(database, profile=profile, documents=documents)
        await jobs.reconcile()
        embedding_worker = MemoryEmbeddingWorker(
            provider=provider,
            jobs=jobs,
            interval_seconds=1,
            claim_limit=100,
            max_attempts=1,
            retry_initial_seconds=1,
        )
        while await embedding_worker.process_once():
            pass
        return (
            MemoryRetriever(
                repository=facts.repository,
                lexical_index=SQLiteMemoryFTSIndex(database),
                semantic_index=MemorySemanticIndex(embeddings, documents=documents),
                embedding_provider=provider,
                embedding_profile=profile,
                embedding_queries=EmbeddingQueryBuilder(max_characters=4000),
            ),
            provider,
        )

    @staticmethod
    def _settings(
        database_url: str,
        *,
        superuser: str,
        consolidation_enabled: bool,
    ) -> Settings:
        return Settings.model_validate(
            {
                "database_url": database_url,
                "superusers_csv": superuser,
                "enabled_groups_csv": "88000001,88000002",
                "llm_provider": "fake",
                "llm_model": "memory-quality-fake-model",
                "model_profiles_file": Path("__memory_quality_no_profiles__.toml"),
                "memory_batch_max_events": 12,
                "memory_batch_max_wait_seconds": 0,
                "memory_consolidation_enabled": consolidation_enabled,
                "memory_rebuild_enabled": True,
                "global_llm_concurrency": 1,
            }
        )

    @staticmethod
    async def _execute_rebuild(
        *,
        case: MemoryQualityCase,
        settings: Settings,
        database: Database,
        ledger: EventLedgerRepository,
        live_worker: MemoryWorker,
        facts: MemoryFactService,
        actor_user_id: str,
        initial_state: Mapping[int, tuple[MemoryStatus, datetime]],
    ) -> QualityRebuildExpectation:
        """Exercise the real plan/extract/review/commit/receipt state machine."""

        repository = MemoryRebuildRepository(database)
        service = MemoryRebuildService(
            settings=settings,
            repository=repository,
            ledger=ledger,
            extractor=live_worker.extractor,
            processor=live_worker.processor,
        )
        run = await service.plan(
            MemoryRebuildSelection(all_events=True), actor_user_id=actor_user_id
        )
        await service.start(run.public_id, actor_user_id=actor_user_id)
        worker = MemoryRebuildWorker(
            service,
            interval_seconds=settings.memory_rebuild_worker_interval_seconds,
        )
        while await worker.process_once():
            pass
        review = await service.review(run.public_id, actor_user_id=actor_user_id)
        if review:
            await service.set_review(
                run.public_id,
                "all",
                approved=True,
                actor_user_id=actor_user_id,
            )
        await service.commit(run.public_id, actor_user_id=actor_user_id)
        while await worker.process_once():
            pass
        statistics = await repository.statistics(run.public_id)
        regressions = 0
        for fact_id, (status, confirmed_at) in initial_state.items():
            refreshed = await facts.get_fact(fact_id)
            if (
                refreshed is None
                or refreshed.status is not status
                or refreshed.last_confirmed_at < confirmed_at
            ):
                regressions += 1
        commit = statistics["commit"]
        return QualityRebuildExpectation(
            committed=int(commit.get("committed", 0)),
            skipped=int(commit.get("skipped", 0)),
            receipts=int(statistics["receipts_completed"]),
            historical_regressions=regressions,
        )

    @staticmethod
    def _claim_payload(claim: QualityClaim) -> dict[str, object]:
        data = claim.model_dump(mode="json", exclude={"event_ref"}, exclude_none=True)
        if claim.scope_type == MemoryScopeType.GROUP.value or claim.subject_ref == "group":
            data["subject_basis"] = "group"
        elif claim.subject_ref.startswith("mentioned_"):
            data["subject_basis"] = "mentioned_subject"
        elif claim.subject_ref == "reply_author":
            data["subject_basis"] = "reply_subject"
        return dict(data)

    @staticmethod
    def _fact_create(spec: QualityFactSpec, symbols: dict[str, str]) -> MemoryFactCreate:
        return MemoryFactCreate(
            scope_type=MemoryScopeType(spec.scope_type),
            subject_user_id=symbols[spec.subject] if spec.subject else None,
            group_id=symbols[spec.group] if spec.group else None,
            kind=MemoryKind(spec.kind),
            memory_key=spec.memory_key,
            category=spec.category,
            content=spec.content,
            importance=spec.importance,
            confidence=spec.confidence,
            source_type=MemorySourceType(spec.source_type),
            authority=MemoryAuthority(spec.authority),
            status=MemoryStatus(spec.status),
            conflict_state=MemoryConflictState(spec.conflict_state),
            invalidated_reason=(
                MemoryInvalidationReason.ADMINISTRATOR_INVALIDATED
                if spec.status == MemoryStatus.INVALIDATED.value
                else None
            ),
            valid_from=spec.valid_from,
            valid_until=spec.valid_until,
        )

    @staticmethod
    async def _all_facts(
        database: Database,
        repository: MemoryFactRepository,
    ) -> tuple[MemoryFact, ...]:
        async with database.sessions() as session:
            ids = tuple(
                await session.scalars(select(MemoryFactModel.id).order_by(MemoryFactModel.id))
            )
        result = []
        for fact_id in ids:
            fact = await repository.get_fact(int(fact_id))
            if fact is not None:
                result.append(fact)
        return tuple(result)

    @classmethod
    def _map_fact_refs(
        cls,
        facts: tuple[MemoryFact, ...],
        specs: tuple[QualityFactSpec, ...],
        symbols: dict[str, str],
    ) -> dict[int, str]:
        mapped: dict[int, str] = {}
        for fact in facts:
            matches = [spec for spec in specs if cls._matches(fact, spec, symbols)]
            if len(matches) == 1:
                mapped[fact.id] = matches[0].fact_ref
        return mapped

    @staticmethod
    def _matches(
        fact: MemoryFact,
        spec: QualityFactSpec,
        symbols: dict[str, str],
    ) -> bool:
        return (
            fact.scope_type.value == spec.scope_type
            and fact.subject_user_id == (symbols[spec.subject] if spec.subject else None)
            and fact.group_id == (symbols[spec.group] if spec.group else None)
            and fact.kind.value == spec.kind
            and fact.memory_key == spec.memory_key
            and fact.content == spec.content
            and fact.status.value == spec.status
            and fact.conflict_state.value == spec.conflict_state
            and fact.authority.value == spec.authority
        )

    @staticmethod
    def _observed_fact_key(
        fact: MemoryFact,
        reference: str | None,
        reverse_symbols: dict[str, str],
    ) -> str:
        return fact_key(
            QualityFactSpec(
                fact_ref=reference or MemoryQualityRunner._unmapped_ref(fact, reverse_symbols),
                scope_type=fact.scope_type.value,
                subject=(
                    reverse_symbols.get(fact.subject_user_id, "unknown_person")
                    if fact.subject_user_id
                    else None
                ),
                group=(
                    reverse_symbols.get(fact.group_id, "unknown_group") if fact.group_id else None
                ),
                kind=fact.kind.value,
                memory_key=fact.memory_key,
                category=fact.category,
                content=fact.content,
                status=fact.status.value,
                conflict_state=fact.conflict_state.value,
                source_type=fact.source_type.value,
                authority=fact.authority.value,
                importance=fact.importance,
                confidence=fact.confidence,
                valid_from=fact.valid_from,
                valid_until=fact.valid_until,
            )
        )

    @staticmethod
    def _unmapped_ref(fact: MemoryFact, reverse_symbols: dict[str, str]) -> str:
        subject = reverse_symbols.get(fact.subject_user_id or "", "none")
        group = reverse_symbols.get(fact.group_id or "", "none")
        payload = "|".join(
            (fact.scope_type.value, subject, group, fact.memory_key, fact.content)
        ).encode("utf-8")
        return f"fact_unmapped_{hashlib.sha256(payload).hexdigest()[:16]}"

    @staticmethod
    def _target(query: QualityQuerySpec, symbols: dict[str, str]) -> MemoryEntityTarget:
        scope = MemoryScopeType(query.scope_type)
        role = {
            MemoryScopeType.PERSON: MemoryTargetRole.CURRENT_PERSON,
            MemoryScopeType.PERSON_GROUP: MemoryTargetRole.CURRENT_PERSON_GROUP,
            MemoryScopeType.GROUP: MemoryTargetRole.CURRENT_GROUP,
        }[scope]
        subject = query.subject
        group = query.group
        return MemoryEntityTarget(
            role=role,
            scope_type=scope,
            subject_user_id=symbols[subject] if subject else None,
            group_id=symbols[group] if group else None,
            block_id=f"quality:{query.query_ref}",
        )

    @staticmethod
    def _claim_key(
        claim: QualityClaim,
        case: MemoryQualityCase,
        symbols: dict[str, str],
    ) -> str:
        event = next(item for item in case.events if item.event_ref == claim.event_ref)
        subject = event.speaker
        if claim.subject_ref.startswith("mentioned_"):
            position = int(claim.subject_ref.removeprefix("mentioned_")) - 1
            subject = event.mentioned[position] if position < len(event.mentioned) else "unknown"
        elif claim.subject_ref == "reply_author":
            subject = event.reply_speaker or "unknown"
        elif claim.subject_ref == "group":
            subject = event.group or "unknown"
        _ = symbols
        return "|".join(
            (
                claim.event_ref,
                claim.scope_type,
                subject,
                claim.memory_key,
                claim.content,
            )
        )

    def _commit(self) -> str:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self._root,
                text=True,
                encoding="utf-8",
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return f"unknown-{__version__}"
