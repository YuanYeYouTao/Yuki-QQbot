"""Composition boundary for provider, derived indexes, reconciliation, and worker."""

from __future__ import annotations

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.embedding.health import MemoryEmbeddingHealthService
from qq_ai_bot.memory.embedding.jobs import MemoryEmbeddingJobRepository
from qq_ai_bot.memory.embedding.metrics import MemoryEmbeddingMetrics
from qq_ai_bot.memory.embedding.models import MemoryEmbeddingHealth
from qq_ai_bot.memory.embedding.provider import EmbeddingProvider
from qq_ai_bot.memory.embedding.query_cache import QueryEmbeddingCache
from qq_ai_bot.memory.embedding.qwen import QwenDashScopeEmbeddingProvider
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.semantic import MemorySemanticIndex
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder, EmbeddingQueryBuilder
from qq_ai_bot.memory.embedding.worker import MemoryEmbeddingWorker
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database


class MemoryEmbeddingRuntime:
    """Own the optional provider without leaking it into ContextAssembler or plugins."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        facts: MemoryFactService,
        retriever: MemoryRetriever,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        self._settings = settings
        self._facts = facts
        self._retriever = retriever
        self.repository = MemoryEmbeddingRepository(database)
        self.documents = EmbeddingDocumentBuilder(
            template_version=settings.memory_embedding_document_template_version,
            max_characters=settings.memory_embedding_max_text_characters,
        )
        self.queries = EmbeddingQueryBuilder(
            max_characters=settings.memory_embedding_max_text_characters
        )
        self.provider = provider or self._build_provider(settings)
        self.metrics = MemoryEmbeddingMetrics()
        self.query_cache = QueryEmbeddingCache(
            ttl_seconds=settings.memory_embedding_query_cache_ttl_seconds,
            max_entries=settings.memory_embedding_query_cache_max_entries,
        )
        self.jobs: MemoryEmbeddingJobRepository | None = None
        self.worker: MemoryEmbeddingWorker | None = None
        self._profile_id: int | None = None

    @staticmethod
    def _build_provider(settings: Settings) -> EmbeddingProvider | None:
        if not settings.memory_embedding_enabled:
            return None
        return QwenDashScopeEmbeddingProvider(
            base_url=settings.memory_embedding_base_url,
            api_key=settings.memory_embedding_api_key,
            model=settings.memory_embedding_model,
            dimensions=settings.memory_embedding_dimensions,
            output_type=settings.memory_embedding_output_type,
            document_template_version=settings.memory_embedding_document_template_version,
            query_instruct=settings.memory_embedding_query_instruct,
            timeout_seconds=settings.memory_embedding_request_timeout_seconds,
            http_concurrency=settings.memory_embedding_http_concurrency,
        )

    @property
    def enabled(self) -> bool:
        return self._settings.memory_embedding_enabled

    @property
    def profile_id(self) -> int | None:
        return self._profile_id

    @property
    def dimensions(self) -> int:
        return self._settings.memory_embedding_dimensions

    async def start(self) -> None:
        if self.provider is None:
            return
        profile = await self.repository.ensure_profile(self.provider.profile)
        self._profile_id = profile.id
        self.jobs = MemoryEmbeddingJobRepository(
            self.repository.database,
            profile=profile,
            documents=self.documents,
        )
        semantic = MemorySemanticIndex(self.repository, documents=self.documents)
        self._retriever.configure_semantic(
            semantic_index=semantic,
            provider=self.provider,
            profile=profile,
            queries=self.queries,
            metrics=self.metrics,
            query_cache=self.query_cache,
        )
        if self._settings.memory_embedding_worker_enabled:
            self.worker = MemoryEmbeddingWorker(
                provider=self.provider,
                jobs=self.jobs,
                interval_seconds=self._settings.memory_embedding_worker_interval_seconds,
                claim_limit=self._settings.memory_embedding_worker_claim_limit,
                max_attempts=self._settings.memory_embedding_retry_attempts,
                retry_initial_seconds=self._settings.memory_embedding_retry_initial_seconds,
                metrics=self.metrics,
            )
            self._facts.set_embedding_scheduler(self.worker)
            await self.worker.start()
        else:
            await self.jobs.reconcile()

    async def close(self) -> None:
        self._facts.set_embedding_scheduler(None)
        if self.worker is not None:
            await self.worker.close()
        if self.provider is not None:
            await self.provider.close()

    async def health(self) -> MemoryEmbeddingHealth:
        return await MemoryEmbeddingHealthService(
            enabled=self.enabled,
            provider=self.provider,
            repository=self.repository,
            profile_id=self._profile_id,
            documents=self.documents,
        ).health()

    async def doctor(self) -> int:
        return await MemoryEmbeddingHealthService(
            enabled=self.enabled,
            provider=self.provider,
            repository=self.repository,
            profile_id=self._profile_id,
            documents=self.documents,
        ).doctor()

    async def retry(self) -> int:
        if self.jobs is None:
            raise RuntimeError("memory embedding is not enabled")
        return await self.jobs.retry_failed()

    async def rebuild(self) -> int:
        if self.jobs is None:
            raise RuntimeError("memory embedding is not enabled")
        return await self.jobs.reconcile(force=True)

    async def purge_old(self) -> int:
        if self.jobs is None or self._profile_id is None:
            raise RuntimeError("memory embedding is not enabled")
        await self.jobs.delete_for_old_profiles()
        return await self.repository.purge_old_profiles(current_profile_id=self._profile_id)
