"""Provider, vector, worker, identity, hybrid, and degradation coverage."""

from __future__ import annotations

import asyncio
import json
import math
import struct
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update

from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.fake import FakeEmbeddingProvider
from qq_ai_bot.memory.embedding.jobs import EmbeddingWrite, MemoryEmbeddingJobRepository
from qq_ai_bot.memory.embedding.metrics import MemoryEmbeddingMetrics
from qq_ai_bot.memory.embedding.models import (
    EmbeddingBatchResult,
    EmbeddingProviderProfile,
    EmbeddingVector,
)
from qq_ai_bot.memory.embedding.provider import EmbeddingProviderError
from qq_ai_bot.memory.embedding.query_cache import QueryEmbeddingCache
from qq_ai_bot.memory.embedding.qwen import QwenDashScopeEmbeddingProvider
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.semantic import MemorySemanticIndex
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder, EmbeddingQueryBuilder
from qq_ai_bot.memory.embedding.worker import MemoryEmbeddingWorker
from qq_ai_bot.memory.enums import (
    MemoryKind,
    MemoryRetrievalMode,
    MemoryScopeType,
    MemorySourceType,
    MemoryTargetRole,
)
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.models import MemoryEntityTarget, MemoryFact, MemoryFactCreate, MemoryQuery
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryEmbeddingJobModel,
    MemoryEmbeddingModel,
    MemoryFactModel,
)


def _qwen_vector(value: float = 1.0) -> list[float]:
    return [value, *([0.0] * 1023)]


def _response(
    count: int,
    *,
    indexes: list[int] | None = None,
    vector: list[float] | None = None,
) -> dict[str, object]:
    actual_indexes = indexes if indexes is not None else list(range(count))
    return {
        "output": {
            "embeddings": [
                {"text_index": index, "embedding": vector or _qwen_vector(float(index + 1))}
                for index in actual_indexes
            ]
        },
        "usage": {"input_tokens": count * 2},
        "request_id": "request-test",
    }


def _provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> QwenDashScopeEmbeddingProvider:
    return QwenDashScopeEmbeddingProvider(
        base_url="https://workspace.example/api/v1",
        api_key="test-secret-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_qwen_document_and_query_payloads_and_ordering() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        count = len(payload["input"]["texts"])
        return httpx.Response(200, json=_response(count, indexes=list(reversed(range(count)))))

    provider = _provider(handler)
    documents = await provider.embed_documents(("first", "second"))
    query = await provider.embed_query("question")
    await provider.close()

    assert requests[0]["parameters"] == {
        "text_type": "document",
        "dimension": 1024,
        "output_type": "dense",
    }
    assert requests[1]["parameters"]["text_type"] == "query"
    assert requests[1]["parameters"]["instruct"].startswith("Retrieve personal")
    assert documents.vectors[0].values[0] == 1
    assert documents.vectors[1].values[0] == 2
    assert query.usage.input_tokens == 2


@pytest.mark.asyncio
async def test_query_embedding_cache_is_bounded_by_ttl() -> None:
    current = [0.0]
    provider = FakeEmbeddingProvider(dimensions=4)
    cache = QueryEmbeddingCache(
        ttl_seconds=10,
        max_entries=2,
        clock=lambda: current[0],
    )

    async def create() -> EmbeddingBatchResult:
        return await provider.embed_query("same query")

    first, first_hit = await cache.get_or_create(
        profile_fingerprint=provider.profile.fingerprint,
        query_text="same query",
        factory=create,
    )
    second, second_hit = await cache.get_or_create(
        profile_fingerprint=provider.profile.fingerprint,
        query_text="same query",
        factory=create,
    )
    current[0] = 11
    third, third_hit = await cache.get_or_create(
        profile_fingerprint=provider.profile.fingerprint,
        query_text="same query",
        factory=create,
    )

    assert first == second == third
    assert (first_hit, second_hit, third_hit) == (False, True, False)
    assert provider.query_requests == 2


@pytest.mark.asyncio
async def test_query_embedding_cache_coalesces_concurrent_requests() -> None:
    provider = FakeEmbeddingProvider(dimensions=4)
    cache = QueryEmbeddingCache(ttl_seconds=10, max_entries=2)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def create() -> EmbeddingBatchResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return await provider.embed_query("same query")

    async def get() -> tuple[EmbeddingBatchResult, bool]:
        return await cache.get_or_create(
            profile_fingerprint=provider.profile.fingerprint,
            query_text="same query",
            factory=create,
        )

    first_task = asyncio.create_task(get())
    await started.wait()
    second_task = asyncio.create_task(get())
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first[0] == second[0]
    assert (first[1], second[1]) == (False, True)
    assert calls == 1
    assert provider.query_requests == 1


@pytest.mark.asyncio
async def test_qwen_provider_splits_at_declared_capability() -> None:
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        count = len(payload["input"]["texts"])
        sizes.append(count)
        return httpx.Response(200, json=_response(count))

    provider = _provider(handler)
    result = await provider.embed_documents(tuple(f"document-{index}" for index in range(45)))
    await provider.close()
    assert sizes == [20, 20, 5]
    assert len(result.vectors) == 45
    assert result.usage.input_count == 45


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "embedding_authentication_failed", False),
        (403, "embedding_authentication_failed", False),
        (429, "embedding_rate_limited", True),
        (500, "embedding_provider_unavailable", True),
        (400, "embedding_invalid_request", False),
    ],
)
async def test_qwen_provider_classifies_http_errors(
    status: int, code: str, retryable: bool
) -> None:
    provider = _provider(lambda _: httpx.Response(status, text="secret remote response"))
    with pytest.raises(EmbeddingProviderError) as caught:
        await provider.embed_query("fixed query")
    await provider.close()
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "secret remote response" not in str(caught.value)


@pytest.mark.asyncio
async def test_qwen_provider_classifies_timeout_and_preserves_cancellation() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("contains provider internals", request=request)

    timeout_provider = _provider(timeout_handler)
    with pytest.raises(EmbeddingProviderError) as caught:
        await timeout_provider.embed_query("fixed query")
    await timeout_provider.close()
    assert caught.value.code == "embedding_timeout"

    def cancelled_handler(_: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    cancelled_provider = _provider(cancelled_handler)
    with pytest.raises(asyncio.CancelledError):
        await cancelled_provider.embed_query("fixed query")
    await cancelled_provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"not_output": {}},
        _response(2, indexes=[0]),
        _response(2, indexes=[0, 0]),
        _response(1, indexes=[2]),
        _response(1, vector=[1.0, 2.0]),
        _response(1, vector=[math.nan, *([0.0] * 1023)]),
        _response(1, vector=[math.inf, *([0.0] * 1023)]),
        _response(1, vector=[0.0] * 1024),
    ],
)
async def test_qwen_provider_rejects_invalid_responses(payload: dict[str, object]) -> None:
    provider = _provider(
        lambda _: httpx.Response(
            200,
            content=json.dumps(payload, allow_nan=True).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
    )
    usage = payload.get("usage")
    input_count = 2 if isinstance(usage, dict) and usage.get("input_tokens") == 4 else 1
    with pytest.raises(EmbeddingProviderError) as caught:
        await provider.embed_documents(tuple("one" for _ in range(input_count)))
    await provider.close()
    assert caught.value.code == "embedding_invalid_response"


def test_profile_fingerprint_is_stable_and_contains_no_secret() -> None:
    values = {
        "provider_id": "qwen_dashscope",
        "model_id": "qwen3.7-text-embedding",
        "dimensions": 1024,
        "output_type": "dense",
        "document_template_version": 1,
        "endpoint_identity": "https://workspace.example/api/v1",
    }
    first = EmbeddingProviderProfile(**values)
    second = EmbeddingProviderProfile(**values)
    assert first.fingerprint == second.fingerprint
    assert "secret" not in repr(first).casefold()
    with pytest.raises(ValidationError):
        EmbeddingProviderProfile(**values, fingerprint="0" * 64)


def test_document_and_query_builders_are_bounded_and_identity_free() -> None:
    fact = MemoryFact(
        id=99,
        scope_type=MemoryScopeType.PERSON_GROUP,
        subject_user_id="123456789",
        group_id="987654321",
        kind=MemoryKind.FACT,
        memory_key="drink",
        category="preference",
        content="喜欢\n冰咖啡，联系人 123456789，群 987654321",
        normalized_content="喜欢 冰咖啡",
        importance=3,
        confidence=1,
        source_type=MemorySourceType.AUTOMATIC,
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=200)
    text = documents.build(fact)
    assert "123456789" not in text
    assert "987654321" not in text
    assert "fact_id" not in text
    assert "Fact: 喜欢 冰咖啡,联系人 [id],群 [id]" in text
    assert len(documents.content_hash(fact)) == 64

    query = MemoryQuery(
        text="当前问题 123456789\n回复摘录",
        normalized_text="当前问题 123456789 回复摘录",
        mode=MemoryRetrievalMode.RELEVANT,
        targets=(),
        candidate_limit=5,
        limit_per_target=5,
        always_on_explicit_preference_limit=0,
        query_term_limit=5,
    )
    query_text = EmbeddingQueryBuilder(max_characters=80).build(query)
    assert query_text == "当前问题 [id] 回复摘录"
    assert "123456789" not in query_text


def test_float32_codec_normalizes_and_uses_little_endian() -> None:
    codec = Float32VectorCodec()
    payload = codec.encode(EmbeddingVector(values=(3.0, 4.0), dimensions=2))
    assert payload == struct.pack("<2f", 0.6, 0.8)
    decoded = codec.decode(payload, dimensions=2)
    assert math.isclose(
        math.sqrt(sum(value * value for value in decoded.values)),
        1,
        rel_tol=1e-6,
    )
    assert math.isclose(
        codec.dot(
            EmbeddingVector(values=(3.0, 4.0), dimensions=2),
            EmbeddingVector(values=(6.0, 8.0), dimensions=2),
        ),
        1,
    )
    with pytest.raises(ValueError):
        codec.decode(payload[:-1], dimensions=2)
    with pytest.raises(ValidationError):
        EmbeddingVector(values=(0.0, 0.0), dimensions=2)


def _target(user_id: str) -> MemoryEntityTarget:
    return MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_PERSON,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id=user_id,
        block_id=f"person:{user_id}",
    )


async def _fact(
    service: MemoryFactService,
    *,
    user_id: str,
    content: str,
    key: str,
) -> MemoryFact:
    return await service.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id=user_id,
            kind=MemoryKind.FACT,
            memory_key=key,
            category="profile",
            content=content,
            source_type=MemorySourceType.AUTOMATIC,
        )
    )


async def _scoped_fact(
    service: MemoryFactService,
    *,
    scope_type: MemoryScopeType,
    user_id: str | None,
    group_id: str | None,
    content: str,
    key: str,
) -> MemoryFact:
    return await service.remember(
        MemoryFactCreate(
            scope_type=scope_type,
            subject_user_id=user_id,
            group_id=group_id,
            kind=MemoryKind.FACT,
            memory_key=key,
            category="profile",
            content=content,
            source_type=MemorySourceType.AUTOMATIC,
        )
    )


@pytest.mark.asyncio
async def test_worker_reconciles_batches_and_semantic_search_hard_filters_identity(
    database: Database,
) -> None:
    facts = MemoryFactService(MemoryFactRepository(database))
    wanted = await _fact(facts, user_id="1001", content="喜欢程序设计", key="hobby:code")
    other = await _fact(facts, user_id="1002", content="喜欢程序设计", key="hobby:code")
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4000)
    vectors = {
        documents.build(wanted): (1.0, 0.0, 0.0, 0.0),
        documents.build(other): (1.0, 0.0, 0.0, 0.0),
        "我爱算法": (1.0, 0.0, 0.0, 0.0),
    }
    provider = FakeEmbeddingProvider(dimensions=4, vectors=vectors)
    embeddings = MemoryEmbeddingRepository(database)
    profile = await embeddings.ensure_profile(provider.profile)
    jobs = MemoryEmbeddingJobRepository(database, profile=profile, documents=documents)
    assert await jobs.reconcile() == 2
    worker = MemoryEmbeddingWorker(
        provider=provider,
        jobs=jobs,
        interval_seconds=1,
        claim_limit=20,
        max_attempts=3,
        retry_initial_seconds=1,
    )
    assert await worker.process_once() == 2
    assert provider.document_requests == 1

    semantic = MemorySemanticIndex(embeddings, documents=documents)
    retriever = MemoryRetriever(
        repository=facts.repository,
        lexical_index=SQLiteMemoryFTSIndex(database),
        semantic_index=semantic,
        embedding_provider=provider,
        embedding_profile=profile,
        embedding_queries=EmbeddingQueryBuilder(max_characters=4000),
    )
    query = MemoryQuery(
        text="我爱算法",
        normalized_text="我爱算法",
        mode=MemoryRetrievalMode.RELEVANT,
        targets=(_target("1001"),),
        candidate_limit=20,
        limit_per_target=5,
        always_on_explicit_preference_limit=0,
        query_term_limit=5,
        semantic_min_similarity=0.99,
    )
    result = await retriever.retrieve(query)
    assert [hit.fact.id for hit in result.hits] == [wanted.id]
    assert result.hits[0].selection_reason == "semantic_match"
    assert other.id not in {hit.fact.id for hit in result.hits}
    assert provider.query_requests == 1
    assert result.embedding_profile == profile.profile.fingerprint


@pytest.mark.asyncio
async def test_relevant_retrieval_mmr_prefers_a_diverse_valid_vector(database: Database) -> None:
    facts = MemoryFactService(MemoryFactRepository(database))
    first = await _fact(facts, user_id="1001", content="偏爱浓烈咖啡风味", key="drink:1")
    duplicate = await _fact(
        facts,
        user_id="1001",
        content="喜欢浓郁咖啡风味",
        key="drink:2",
    )
    diverse = await _fact(
        facts,
        user_id="1001",
        content="也喜欢清爽水果茶",
        key="drink:3",
    )
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4000)
    provider = FakeEmbeddingProvider(
        dimensions=4,
        vectors={
            documents.build(first): (0.9, 0.4358899, 0.0, 0.0),
            documents.build(duplicate): (0.9, 0.4358899, 0.0, 0.0),
            documents.build(diverse): (0.8, -0.6, 0.0, 0.0),
            "饮品偏好": (1.0, 0.0, 0.0, 0.0),
        },
    )
    embeddings = MemoryEmbeddingRepository(database)
    profile = await embeddings.ensure_profile(provider.profile)
    jobs = MemoryEmbeddingJobRepository(database, profile=profile, documents=documents)
    await jobs.reconcile()
    worker = MemoryEmbeddingWorker(
        provider=provider,
        jobs=jobs,
        interval_seconds=1,
        claim_limit=20,
        max_attempts=3,
        retry_initial_seconds=1,
    )
    assert await worker.process_once() == 3
    retriever = MemoryRetriever(
        repository=facts.repository,
        lexical_index=SQLiteMemoryFTSIndex(database),
        semantic_index=MemorySemanticIndex(embeddings, documents=documents),
        embedding_provider=provider,
        embedding_profile=profile,
        embedding_queries=EmbeddingQueryBuilder(max_characters=4000),
        mmr_enabled=True,
        mmr_lambda=0.75,
        mmr_candidate_pool_size=20,
    )
    result = await retriever.retrieve(
        MemoryQuery(
            text="饮品偏好",
            normalized_text="饮品偏好",
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(_target("1001"),),
            candidate_limit=20,
            limit_per_target=2,
            always_on_explicit_preference_limit=0,
            query_term_limit=5,
            semantic_min_similarity=-1,
        ),
        diversify=True,
    )

    assert [hit.fact.id for hit in result.hits] == [first.id, diverse.id]
    assert duplicate.id not in {hit.fact.id for hit in result.hits}


@pytest.mark.asyncio
async def test_embedding_queue_keeps_contested_vectors_for_offline_dream(
    database: Database,
) -> None:
    facts = MemoryFactService(MemoryFactRepository(database))
    contested = await _fact(
        facts,
        user_id="1001",
        content="这条长期事实仍处于争议中",
        key="contested:dream",
    )
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == contested.id)
            .values(status="contested", conflict_state="contested")
        )
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4000)
    provider = FakeEmbeddingProvider(dimensions=4)
    embeddings = MemoryEmbeddingRepository(database)
    profile = await embeddings.ensure_profile(provider.profile)
    jobs = MemoryEmbeddingJobRepository(database, profile=profile, documents=documents)
    assert await jobs.reconcile() == 1
    worker = MemoryEmbeddingWorker(
        provider=provider,
        jobs=jobs,
        interval_seconds=1,
        claim_limit=20,
        max_attempts=3,
        retry_initial_seconds=1,
    )
    assert await worker.process_once() == 1
    vectors = await embeddings.load_vectors_for_fact_ids(
        fact_ids=(contested.id,),
        profile_id=profile.id,
    )
    assert contested.id in vectors


@pytest.mark.asyncio
async def test_semantic_search_hard_filters_group_and_person_group(database: Database) -> None:
    facts = MemoryFactService(MemoryFactRepository(database))
    group_a = await _scoped_fact(
        facts,
        scope_type=MemoryScopeType.GROUP,
        user_id=None,
        group_id="2001",
        content="大家周末打羽毛球",
        key="activity",
    )
    group_b = await _scoped_fact(
        facts,
        scope_type=MemoryScopeType.GROUP,
        user_id=None,
        group_id="2002",
        content="大家周末打羽毛球",
        key="activity",
    )
    member_a = await _scoped_fact(
        facts,
        scope_type=MemoryScopeType.PERSON_GROUP,
        user_id="1001",
        group_id="2001",
        content="在群里喜欢聊摄影",
        key="topic",
    )
    member_b = await _scoped_fact(
        facts,
        scope_type=MemoryScopeType.PERSON_GROUP,
        user_id="1001",
        group_id="2002",
        content="在群里喜欢聊摄影",
        key="topic",
    )
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4000)
    provider = FakeEmbeddingProvider(dimensions=4)
    repository = MemoryEmbeddingRepository(database)
    profile = await repository.ensure_profile(provider.profile)
    jobs = MemoryEmbeddingJobRepository(database, profile=profile, documents=documents)
    await jobs.reconcile()
    worker = MemoryEmbeddingWorker(
        provider=provider,
        jobs=jobs,
        interval_seconds=1,
        claim_limit=20,
        max_attempts=3,
        retry_initial_seconds=1,
    )
    assert await worker.process_once() == 4
    index = MemorySemanticIndex(repository, documents=documents)
    query_vector = EmbeddingVector(values=(1, 0, 0, 0), dimensions=4)

    group_results = await index.search(
        target=MemoryEntityTarget(
            role=MemoryTargetRole.CURRENT_GROUP,
            scope_type=MemoryScopeType.GROUP,
            group_id="2001",
            block_id="group:2001",
        ),
        query_vector=query_vector,
        profile=profile.profile,
        profile_id=profile.id,
        candidate_limit=10,
        kinds=(MemoryKind.FACT,),
        min_similarity=-1,
    )
    member_results = await index.search(
        target=MemoryEntityTarget(
            role=MemoryTargetRole.CURRENT_PERSON_GROUP,
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="1001",
            group_id="2001",
            block_id="person_group:1001:2001",
        ),
        query_vector=query_vector,
        profile=profile.profile,
        profile_id=profile.id,
        candidate_limit=10,
        kinds=(MemoryKind.FACT,),
        min_similarity=-1,
    )

    assert {candidate.fact_id for candidate in group_results} == {group_a.id}
    assert group_b.id not in {candidate.fact_id for candidate in group_results}
    assert {candidate.fact_id for candidate in member_results} == {member_a.id}
    assert member_b.id not in {candidate.fact_id for candidate in member_results}


@pytest.mark.asyncio
async def test_query_embedding_is_once_for_multiple_targets_and_overview_is_zero(
    database: Database,
) -> None:
    facts = MemoryFactService(MemoryFactRepository(database))
    first = await _fact(facts, user_id="1001", content="偏爱红茶", key="drink")
    second = await _fact(facts, user_id="1002", content="偏爱红茶", key="drink")
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4000)
    provider = FakeEmbeddingProvider(
        dimensions=4,
        vectors={
            documents.build(first): (1, 0, 0, 0),
            documents.build(second): (1, 0, 0, 0),
            "饮品": (1, 0, 0, 0),
        },
    )
    embeddings = MemoryEmbeddingRepository(database)
    profile = await embeddings.ensure_profile(provider.profile)
    jobs = MemoryEmbeddingJobRepository(database, profile=profile, documents=documents)
    await jobs.reconcile()
    worker = MemoryEmbeddingWorker(
        provider=provider,
        jobs=jobs,
        interval_seconds=1,
        claim_limit=20,
        max_attempts=3,
        retry_initial_seconds=1,
    )
    await worker.process_once()
    metrics = MemoryEmbeddingMetrics()
    cache = QueryEmbeddingCache(ttl_seconds=600, max_entries=32)
    retriever = MemoryRetriever(
        repository=facts.repository,
        lexical_index=SQLiteMemoryFTSIndex(database),
        semantic_index=MemorySemanticIndex(embeddings, documents=documents),
        embedding_provider=provider,
        embedding_profile=profile,
        embedding_queries=EmbeddingQueryBuilder(max_characters=4000),
        embedding_metrics=metrics,
        query_embedding_cache=cache,
    )
    relevant = MemoryQuery(
        text="饮品",
        normalized_text="饮品",
        mode=MemoryRetrievalMode.RELEVANT,
        targets=(_target("1001"), _target("1002")),
        candidate_limit=10,
        limit_per_target=2,
        always_on_explicit_preference_limit=0,
        query_term_limit=5,
        semantic_min_similarity=0.99,
    )
    result = await retriever.retrieve(relevant)
    assert provider.query_requests == 1
    assert [[hit.fact.subject_user_id for hit in block.hits] for block in result.blocks] == [
        ["1001"],
        ["1002"],
    ]

    repeated = await retriever.retrieve(relevant)
    assert repeated.hits == result.hits
    assert provider.query_requests == 1
    assert metrics.snapshot().query_embedding_cache_misses == 1
    assert metrics.snapshot().query_embedding_cache_hits == 1

    await retriever.retrieve(relevant.model_copy(update={"mode": MemoryRetrievalMode.OVERVIEW}))
    assert provider.query_requests == 1


@pytest.mark.asyncio
async def test_embedding_failure_degrades_to_lexical_without_loading_all_facts(
    database: Database,
) -> None:
    facts = MemoryFactService(MemoryFactRepository(database))
    wanted = await _fact(facts, user_id="1001", content="住在杭州西湖", key="city")
    await _fact(facts, user_id="1002", content="住在杭州西湖", key="city")
    provider = FakeEmbeddingProvider(
        dimensions=4,
        error=EmbeddingProviderError(
            "embedding_timeout", "Embedding provider timed out.", retryable=True
        ),
    )
    embeddings = MemoryEmbeddingRepository(database)
    profile = await embeddings.ensure_profile(provider.profile)
    retriever = MemoryRetriever(
        repository=facts.repository,
        lexical_index=SQLiteMemoryFTSIndex(database),
        semantic_index=MemorySemanticIndex(
            embeddings,
            documents=EmbeddingDocumentBuilder(template_version=1, max_characters=4000),
        ),
        embedding_provider=provider,
        embedding_profile=profile,
        embedding_queries=EmbeddingQueryBuilder(max_characters=4000),
    )
    result = await retriever.retrieve(
        MemoryQuery(
            text="杭州西湖",
            normalized_text="杭州西湖",
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(_target("1001"),),
            candidate_limit=10,
            limit_per_target=5,
            always_on_explicit_preference_limit=0,
            query_term_limit=5,
        )
    )
    assert [hit.fact.id for hit in result.hits] == [wanted.id]
    assert result.semantic_degraded is True
    assert result.semantic_status == "embedding_timeout"
    assert provider.query_requests == 1


@pytest.mark.asyncio
async def test_worker_retry_and_nonretryable_failures(database: Database) -> None:
    facts = MemoryFactService(MemoryFactRepository(database))
    fact = await _fact(facts, user_id="1001", content="准备旅行", key="plan")
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4000)
    retry_provider = FakeEmbeddingProvider(
        dimensions=4,
        error=EmbeddingProviderError("embedding_rate_limited", "rate limited", retryable=True),
    )
    embeddings = MemoryEmbeddingRepository(database)
    profile = await embeddings.ensure_profile(retry_provider.profile)
    jobs = MemoryEmbeddingJobRepository(database, profile=profile, documents=documents)
    await jobs.reconcile()
    retry_worker = MemoryEmbeddingWorker(
        provider=retry_provider,
        jobs=jobs,
        interval_seconds=1,
        claim_limit=20,
        max_attempts=3,
        retry_initial_seconds=1,
    )
    assert await retry_worker.process_once() == 0
    async with database.sessions() as session:
        retry_job = await session.scalar(
            select(MemoryEmbeddingJobModel).where(MemoryEmbeddingJobModel.fact_id == fact.id)
        )
        assert retry_job is not None
        assert retry_job.status == "pending"
        assert retry_job.error_category == "embedding_rate_limited"

    async with database.sessions() as session, session.begin():
        stored = await session.get(MemoryEmbeddingJobModel, retry_job.id)
        assert stored is not None
        stored.next_attempt_at = datetime.now(UTC)
    nonretry_provider = FakeEmbeddingProvider(
        dimensions=4,
        error=EmbeddingProviderError(
            "embedding_authentication_failed", "auth failed", retryable=False
        ),
    )
    nonretry_worker = MemoryEmbeddingWorker(
        provider=nonretry_provider,
        jobs=jobs,
        interval_seconds=1,
        claim_limit=20,
        max_attempts=3,
        retry_initial_seconds=1,
    )
    assert await nonretry_worker.process_once() == 0
    async with database.sessions() as session:
        status = await session.scalar(
            select(MemoryEmbeddingJobModel.status).where(MemoryEmbeddingJobModel.fact_id == fact.id)
        )
    assert status == "failed"


@pytest.mark.asyncio
async def test_stale_content_hash_cannot_commit_an_old_vector(database: Database) -> None:
    facts = MemoryFactService(MemoryFactRepository(database))
    fact = await _fact(facts, user_id="1001", content="喜欢咖啡", key="drink")
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4000)
    provider = FakeEmbeddingProvider(dimensions=4)
    repository = MemoryEmbeddingRepository(database)
    profile = await repository.ensure_profile(provider.profile)
    jobs = MemoryEmbeddingJobRepository(database, profile=profile, documents=documents)
    await jobs.reconcile()
    claimed = await jobs.claim(limit=1)
    assert len(claimed) == 1
    old_hash = claimed[0].content_hash

    async with database.sessions() as session, session.begin():
        stored = await session.get(MemoryFactModel, fact.id)
        assert stored is not None
        stored.content = "喜欢红茶"
        stored.normalized_content = "喜欢红茶"

    await jobs.complete(
        (
            EmbeddingWrite(
                job_id=claimed[0].id,
                fact_id=fact.id,
                content_hash=old_hash,
                vector_blob=Float32VectorCodec().encode(
                    EmbeddingVector(values=(1, 0, 0, 0), dimensions=4)
                ),
            ),
        )
    )
    async with database.sessions() as session:
        embedding_count = int(
            await session.scalar(select(func.count()).select_from(MemoryEmbeddingModel)) or 0
        )
        job = await session.get(MemoryEmbeddingJobModel, claimed[0].id)
    assert embedding_count == 0
    assert job is not None
    assert job.status == "pending"
    assert job.content_hash != old_hash


@pytest.mark.asyncio
async def test_content_change_requeues_and_fact_delete_cascades(database: Database) -> None:
    facts = MemoryFactService(MemoryFactRepository(database))
    first = await _fact(facts, user_id="1001", content="喜欢咖啡", key="drink")
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=4000)
    provider = FakeEmbeddingProvider(dimensions=4)
    embeddings = MemoryEmbeddingRepository(database)
    profile = await embeddings.ensure_profile(provider.profile)
    jobs = MemoryEmbeddingJobRepository(database, profile=profile, documents=documents)
    await jobs.reconcile()
    worker = MemoryEmbeddingWorker(
        provider=provider,
        jobs=jobs,
        interval_seconds=1,
        claim_limit=20,
        max_attempts=3,
        retry_initial_seconds=1,
    )
    await worker.process_once()
    second = await _fact(facts, user_id="1001", content="喜欢红茶", key="drink")
    assert second.id != first.id
    await jobs.reconcile()
    await worker.process_once()
    async with database.sessions() as session:
        ready = int(
            await session.scalar(select(func.count()).select_from(MemoryEmbeddingModel)) or 0
        )
        assert ready == 2
    async with database.sessions() as session, session.begin():
        stored_fact = await session.get(MemoryFactModel, second.id)
        assert stored_fact is not None
        await session.delete(stored_fact)
    async with database.sessions() as session:
        remaining = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryEmbeddingModel)
                .where(MemoryEmbeddingModel.fact_id == second.id)
            )
            or 0
        )
    assert remaining == 0
