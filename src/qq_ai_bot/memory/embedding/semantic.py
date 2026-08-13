"""Exact-target semantic search over normalized SQLite float32 BLOBs."""

from __future__ import annotations

from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.models import (
    EmbeddingProviderProfile,
    EmbeddingVector,
    MemorySemanticCandidate,
)
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
from qq_ai_bot.memory.enums import MemoryKind
from qq_ai_bot.memory.models import MemoryEntityTarget


class MemorySemanticIndex:
    """Compute similarity only after repository SQL enforces identity and profile."""

    def __init__(
        self,
        repository: MemoryEmbeddingRepository,
        *,
        documents: EmbeddingDocumentBuilder,
        codec: Float32VectorCodec | None = None,
    ) -> None:
        self._repository = repository
        self._documents = documents
        self._codec = codec or Float32VectorCodec()

    @property
    def repository(self) -> MemoryEmbeddingRepository:
        return self._repository

    async def search(
        self,
        *,
        target: MemoryEntityTarget,
        query_vector: EmbeddingVector,
        profile: EmbeddingProviderProfile,
        profile_id: int,
        candidate_limit: int,
        kinds: tuple[MemoryKind, ...],
        min_similarity: float,
    ) -> tuple[MemorySemanticCandidate, ...]:
        rows = await self._repository.load_target_vectors(
            target=target,
            profile_id=profile_id,
            kinds=tuple(kind.value for kind in kinds),
        )
        scored: list[tuple[int, float]] = []
        for row in rows:
            current_hash = self._documents.content_hash_fields(
                kind=row.kind,
                category=row.category,
                memory_key=row.memory_key,
                content=row.content,
            )
            if row.content_hash != current_hash:
                continue
            vector = self._codec.decode(row.vector_blob, dimensions=profile.dimensions)
            similarity = self._codec.dot(query_vector, vector)
            if similarity >= min_similarity:
                scored.append((row.fact_id, similarity))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return tuple(
            MemorySemanticCandidate(
                fact_id=fact_id,
                target=target,
                cosine_similarity=similarity,
                semantic_rank=rank,
            )
            for rank, (fact_id, similarity) in enumerate(scored[:candidate_limit], start=1)
        )
