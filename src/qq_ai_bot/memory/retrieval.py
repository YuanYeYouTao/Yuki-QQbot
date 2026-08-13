"""Single query-driven read entrypoint for Memory V2 facts."""

from __future__ import annotations

import hashlib
import logging
import time

from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.metrics import MemoryEmbeddingMetrics
from qq_ai_bot.memory.embedding.models import (
    EmbeddingBatchResult,
    EmbeddingVector,
    MemoryEmbeddingProfileRecord,
    MemorySemanticCandidate,
)
from qq_ai_bot.memory.embedding.provider import EmbeddingProvider, EmbeddingProviderError
from qq_ai_bot.memory.embedding.query_cache import QueryEmbeddingCache
from qq_ai_bot.memory.embedding.semantic import MemorySemanticIndex
from qq_ai_bot.memory.embedding.text import EmbeddingQueryBuilder
from qq_ai_bot.memory.enums import (
    MemoryRetrievalMode,
    MemoryScopeType,
    MemoryTargetRole,
)
from qq_ai_bot.memory.fts import MemoryLexicalIndex, build_safe_lexical_query
from qq_ai_bot.memory.metrics import MemoryRetrievalMetric, MemoryRetrievalMetrics
from qq_ai_bot.memory.models import (
    MemoryQuery,
    MemoryRetrievalBlock,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.ranking import MemoryRanker
from qq_ai_bot.memory.repository import MemoryFactRepository

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """Retrieve each identity target independently and never widen its SQL scope."""

    def __init__(
        self,
        *,
        repository: MemoryFactRepository,
        lexical_index: MemoryLexicalIndex,
        ranker: MemoryRanker | None = None,
        metrics: MemoryRetrievalMetrics | None = None,
        semantic_index: MemorySemanticIndex | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        embedding_profile: MemoryEmbeddingProfileRecord | None = None,
        embedding_queries: EmbeddingQueryBuilder | None = None,
        embedding_metrics: MemoryEmbeddingMetrics | None = None,
        query_embedding_cache: QueryEmbeddingCache | None = None,
        mmr_enabled: bool = True,
        mmr_lambda: float = 0.75,
        mmr_candidate_pool_size: int = 20,
    ) -> None:
        self._repository = repository
        self._index = lexical_index
        self._ranker = ranker or MemoryRanker()
        self._metrics = metrics or MemoryRetrievalMetrics()
        self._semantic_index = semantic_index
        self._embedding_provider = embedding_provider
        self._embedding_profile = embedding_profile
        self._embedding_queries = embedding_queries
        self._embedding_metrics = embedding_metrics
        self._query_embedding_cache = query_embedding_cache
        self._mmr_enabled = mmr_enabled
        self._mmr_lambda = mmr_lambda
        self._mmr_candidate_pool_size = mmr_candidate_pool_size
        self._vector_codec = Float32VectorCodec()

    @property
    def metrics(self) -> MemoryRetrievalMetrics:
        return self._metrics

    def configure_semantic(
        self,
        *,
        semantic_index: MemorySemanticIndex,
        provider: EmbeddingProvider,
        profile: MemoryEmbeddingProfileRecord,
        queries: EmbeddingQueryBuilder,
        metrics: MemoryEmbeddingMetrics | None = None,
        query_cache: QueryEmbeddingCache | None = None,
    ) -> None:
        self._semantic_index = semantic_index
        self._embedding_provider = provider
        self._embedding_profile = profile
        self._embedding_queries = queries
        self._embedding_metrics = metrics
        self._query_embedding_cache = query_cache

    async def retrieve(
        self,
        query: MemoryQuery,
        *,
        lexical_enabled: bool = True,
        diversify: bool = False,
    ) -> MemoryRetrievalResult:
        started = time.perf_counter()
        fts_latency = 0.0
        semantic_latency = 0.0
        hybrid_latency = 0.0
        candidate_count = 0
        semantic_candidate_count = 0
        blocks: list[MemoryRetrievalBlock] = []
        all_hits: list[MemoryRetrievalHit] = []
        short_fallback_used = False
        query_vector = None
        semantic_degraded = False
        semantic_status = "disabled"
        semantic_requested = (
            lexical_enabled
            and query.mode is MemoryRetrievalMode.RELEVANT
            and query.semantic_enabled
        )
        if semantic_requested and not query.normalized_text:
            semantic_status = "empty_query"
        elif semantic_requested and (
            self._semantic_index is None
            or self._embedding_provider is None
            or self._embedding_profile is None
            or self._embedding_queries is None
        ):
            semantic_status = "not_configured"
        elif semantic_requested:
            provider = self._embedding_provider
            profile = self._embedding_profile
            queries = self._embedding_queries
            assert provider is not None
            assert profile is not None
            assert queries is not None
            embedding_started = time.perf_counter()
            try:
                semantic_status = "ready"
                query_text = queries.build(query)
                if query_text:

                    async def embed_once() -> EmbeddingBatchResult:
                        result = await provider.embed_query(query_text)
                        if len(result.vectors) != 1:
                            raise EmbeddingProviderError(
                                "embedding_invalid_response",
                                "Embedding provider returned an invalid response.",
                                retryable=False,
                            )
                        return result

                    cache_hit = False
                    if self._query_embedding_cache is not None:
                        embedded, cache_hit = await self._query_embedding_cache.get_or_create(
                            profile_fingerprint=profile.profile.fingerprint,
                            query_text=query_text,
                            factory=embed_once,
                        )
                    else:
                        embedded = await embed_once()
                    embedding_latency = time.perf_counter() - embedding_started
                    semantic_latency += embedding_latency
                    if self._embedding_metrics is not None:
                        if cache_hit:
                            self._embedding_metrics.record_query_cache_hit()
                        else:
                            self._embedding_metrics.record_query(
                                input_tokens=embedded.usage.input_tokens,
                                latency=embedding_latency,
                            )
                    query_vector = embedded.vectors[0]
                else:
                    semantic_status = "empty_query"
            except EmbeddingProviderError as exc:
                if self._embedding_metrics is not None:
                    self._embedding_metrics.record_query(
                        input_tokens=None,
                        latency=time.perf_counter() - embedding_started,
                        failed=True,
                    )
                semantic_status = exc.code
                semantic_degraded = True
                logger.warning("memory_semantic_degraded error_category=%s", exc.code)
        elif query.mode is MemoryRetrievalMode.OVERVIEW:
            semantic_status = "overview"
        for target in query.targets:
            hits: tuple[MemoryRetrievalHit, ...]
            if query.mode is MemoryRetrievalMode.OVERVIEW or not lexical_enabled:
                facts = await self._repository.list_overview(
                    target,
                    limit=query.limit_per_target,
                )
                candidate_count += len(facts)
                hits = self._ranker.rank_overview(
                    facts,
                    target=target,
                    limit=query.limit_per_target,
                    reason=("overview" if lexical_enabled else "retrieval_disabled_fallback"),
                )
            else:
                preferences = (
                    await self._repository.list_explicit_preferences(
                        target,
                        limit=query.always_on_explicit_preference_limit,
                    )
                    if target.scope_type is MemoryScopeType.PERSON
                    and target.role is MemoryTargetRole.CURRENT_PERSON
                    and query.always_on_explicit_preference_limit > 0
                    else ()
                )
                safe = build_safe_lexical_query(
                    query.normalized_text,
                    term_limit=query.query_term_limit,
                )
                search_started = time.perf_counter()
                candidates = await self._index.search(
                    target,
                    safe,
                    candidate_limit=query.candidate_limit,
                    kinds=query.kinds,
                    short_query_fallback_enabled=query.short_query_fallback_enabled,
                )
                fts_latency += time.perf_counter() - search_started
                short_fallback_used = short_fallback_used or bool(
                    safe.short_term and query.short_query_fallback_enabled
                )
                semantic_candidates: tuple[MemorySemanticCandidate, ...] = ()
                if query_vector is not None:
                    assert self._semantic_index is not None
                    assert self._embedding_profile is not None
                    semantic_started = time.perf_counter()
                    try:
                        semantic_candidates = await self._semantic_index.search(
                            target=target,
                            query_vector=query_vector,
                            profile=self._embedding_profile.profile,
                            profile_id=self._embedding_profile.id,
                            candidate_limit=query.semantic_candidate_limit,
                            kinds=query.kinds,
                            min_similarity=query.semantic_min_similarity,
                        )
                    except ValueError:
                        semantic_degraded = True
                        semantic_status = "embedding_index_invalid"
                        logger.warning(
                            "memory_semantic_degraded error_category=%s",
                            semantic_status,
                        )
                    semantic_latency += time.perf_counter() - semantic_started
                candidate_ids = tuple(
                    dict.fromkeys(
                        [item.fact_id for item in candidates]
                        + [item.fact_id for item in semantic_candidates]
                    )
                )
                candidate_facts = await self._repository.get_active_for_target(
                    target, candidate_ids
                )
                candidate_count += len(preferences) + len(candidates) + len(semantic_candidates)
                semantic_candidate_count += len(semantic_candidates)
                preference_hits = self._ranker.rank_overview(
                    preferences,
                    target=target,
                    limit=query.always_on_explicit_preference_limit,
                    reason="always_on_explicit_preference",
                )
                remaining = max(0, query.limit_per_target - len(preference_hits))
                hybrid_started = time.perf_counter()
                lexical_hits = self._ranker.rank_hybrid(
                    facts=candidate_facts,
                    lexical_candidates=candidates,
                    semantic_candidates=semantic_candidates,
                    target=target,
                    normalized_query=query.normalized_text,
                    lexical_weight=query.hybrid_lexical_weight,
                    semantic_weight=query.hybrid_semantic_weight,
                    rrf_k=query.hybrid_rrf_k,
                    limit=(
                        max(remaining, self._mmr_candidate_pool_size)
                        if self._mmr_enabled and diversify and query_vector is not None
                        else remaining
                    ),
                )
                hybrid_latency += time.perf_counter() - hybrid_started
                preference_ids = {hit.fact.id for hit in preference_hits}
                deduplicated = tuple(
                    hit for hit in lexical_hits if hit.fact.id not in preference_ids
                )
                if (
                    self._mmr_enabled
                    and diversify
                    and query_vector is not None
                    and self._semantic_index is not None
                    and self._embedding_profile is not None
                    and remaining > 0
                ):
                    deduplicated = await self._diversify_mmr(
                        deduplicated,
                        query_vector=query_vector,
                        valid_fact_ids=frozenset(
                            item.fact_id for item in semantic_candidates
                        ),
                        limit=remaining,
                    )
                combined = (*preference_hits, *deduplicated)[: query.limit_per_target]
                hits = tuple(
                    hit.model_copy(update={"rank": rank})
                    for rank, hit in enumerate(combined, start=1)
                )
            blocks.append(MemoryRetrievalBlock(target=target, hits=hits))
            all_hits.extend(hits)

        query_hash = hashlib.sha256(query.normalized_text.encode("utf-8")).hexdigest()
        result = MemoryRetrievalResult(
            blocks=tuple(blocks),
            hits=tuple(all_hits),
            candidate_count=candidate_count,
            selected_count=len(all_hits),
            query_hash=query_hash,
            mode=query.mode,
            semantic_status=semantic_status,
            semantic_degraded=semantic_degraded,
            embedding_profile=(
                self._embedding_profile.profile.fingerprint
                if query_vector is not None and self._embedding_profile is not None
                else None
            ),
        )
        referenced = {
            target.subject_user_id
            for target in query.targets
            if target.role
            in {
                MemoryTargetRole.REFERENCED_PERSON,
                MemoryTargetRole.REFERENCED_PERSON_GROUP,
            }
        }
        self._metrics.record(
            MemoryRetrievalMetric(
                mode=query.mode,
                query_hash=query_hash,
                target_count=len(query.targets),
                candidate_count=candidate_count,
                selected_count=len(all_hits),
                context_selected_count=0,
                fts_latency=fts_latency,
                total_latency=time.perf_counter() - started,
                overview_used=query.mode is MemoryRetrievalMode.OVERVIEW,
                short_query_fallback_used=short_fallback_used,
                referenced_person_count=len(referenced - {None}),
                semantic_candidate_count=semantic_candidate_count,
                semantic_selected_count=sum(1 for hit in all_hits if "semantic" in hit.sources),
                hybrid_selected_count=sum(
                    1 for hit in all_hits if "semantic" in hit.sources and "lexical" in hit.sources
                ),
                semantic_degraded=semantic_degraded,
                semantic_search_latency=semantic_latency,
                hybrid_rank_latency=hybrid_latency,
            )
        )
        return result

    async def _diversify_mmr(
        self,
        hits: tuple[MemoryRetrievalHit, ...],
        *,
        query_vector: EmbeddingVector,
        valid_fact_ids: frozenset[int],
        limit: int,
    ) -> tuple[MemoryRetrievalHit, ...]:
        """Diversify the RRF tail while pinning exact matches and using local vectors only."""

        semantic_index = self._semantic_index
        if (
            limit <= 0
            or len(hits) <= 1
            or self._embedding_profile is None
            or semantic_index is None
        ):
            return hits[:limit]
        profile = self._embedding_profile.profile
        vectors = await semantic_index.repository.load_vectors_for_fact_ids(
            fact_ids=tuple(
                hit.fact.id
                for hit in hits[: self._mmr_candidate_pool_size]
                if hit.fact.id in valid_fact_ids
            ),
            profile_id=self._embedding_profile.id,
        )
        decoded = {
            fact_id: self._vector_codec.decode(payload, dimensions=profile.dimensions)
            for fact_id, payload in vectors.items()
        }
        pinned = [hit for hit in hits if hit.selection_reason.endswith("_exact")][:limit]
        selected = list(pinned)
        selected_ids = {hit.fact.id for hit in selected}
        candidates = [
            hit
            for hit in hits[: self._mmr_candidate_pool_size]
            if hit.fact.id not in selected_ids and hit.fact.id in decoded
        ]
        if not candidates:
            return hits[:limit]
        while candidates and len(selected) < limit:
            best = max(
                candidates,
                key=lambda hit: (
                    self._mmr_score(
                        hit,
                        query_vector=query_vector,
                        selected=selected,
                        vectors=decoded,
                    ),
                    -(hit.rank),
                    -hit.fact.id,
                ),
            )
            selected.append(best)
            candidates.remove(best)
        for hit in hits:
            if len(selected) >= limit:
                break
            if hit.fact.id not in {item.fact.id for item in selected}:
                selected.append(hit)
        return tuple(selected)

    def _mmr_score(
        self,
        hit: MemoryRetrievalHit,
        *,
        query_vector: EmbeddingVector,
        selected: list[MemoryRetrievalHit],
        vectors: dict[int, EmbeddingVector],
    ) -> float:
        vector = vectors[hit.fact.id]
        relevance = self._vector_codec.dot(query_vector, vector)
        redundancy = max(
            (
                self._vector_codec.dot(vector, vectors[item.fact.id])
                for item in selected
                if item.fact.id in vectors
                and self._mmr_partition(item) == self._mmr_partition(hit)
            ),
            default=0.0,
        )
        return self._mmr_lambda * relevance - (1.0 - self._mmr_lambda) * redundancy

    @staticmethod
    def _mmr_partition(hit: MemoryRetrievalHit) -> tuple[object, ...]:
        fact = hit.fact
        return (
            fact.scope_type,
            fact.subject_user_id,
            fact.group_id,
            fact.visibility_type,
            fact.visibility_user_id,
            fact.visibility_group_id,
        )
