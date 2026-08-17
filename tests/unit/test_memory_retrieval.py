"""Query-driven Memory V2 retrieval, targeting, and scale regressions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryContextMode,
    MemoryKind,
    MemoryRetrievalMode,
    MemoryScopeType,
    MemorySourceType,
    MemoryTargetRole,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.errors import MemoryRetrievalError
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex, build_safe_lexical_query
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryFact,
    MemoryFactCreate,
    MemoryLexicalCandidate,
    MemoryQuery,
    MemoryQueryIntent,
)
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.ranking import MemoryRanker
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.people_repository import PeopleRepository


def _target(
    user_id: str,
    *,
    group_id: str | None = None,
    role: MemoryTargetRole | None = None,
) -> MemoryEntityTarget:
    return MemoryEntityTarget(
        role=role
        or (MemoryTargetRole.CURRENT_PERSON_GROUP if group_id else MemoryTargetRole.CURRENT_PERSON),
        scope_type=(MemoryScopeType.PERSON_GROUP if group_id else MemoryScopeType.PERSON),
        subject_user_id=user_id,
        group_id=group_id,
        block_id=f"target:{user_id}:{group_id or 'global'}",
    )


def _group_target(group_id: str) -> MemoryEntityTarget:
    return MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_GROUP,
        scope_type=MemoryScopeType.GROUP,
        group_id=group_id,
        block_id=f"group:{group_id}",
    )


def _query(
    value: str,
    *targets: MemoryEntityTarget,
    mode: MemoryRetrievalMode = MemoryRetrievalMode.RELEVANT,
    limit: int = 8,
) -> MemoryQuery:
    return MemoryQuery(
        text=value,
        normalized_text=value.casefold(),
        mode=mode,
        targets=targets,
        candidate_limit=50,
        limit_per_target=limit,
        always_on_explicit_preference_limit=2,
        query_term_limit=12,
        short_query_fallback_enabled=True,
    )


async def _remember(
    service: MemoryFactService,
    *,
    content: str,
    memory_key: str,
    user_id: str | None = "1001",
    group_id: str | None = None,
    scope: MemoryScopeType = MemoryScopeType.PERSON,
    kind: MemoryKind = MemoryKind.FACT,
    source: MemorySourceType = MemorySourceType.AUTOMATIC,
    category: str = "profile",
    importance: int = 3,
    confidence: float = 0.8,
    visibility_type: SelfMemoryVisibility | None = None,
    visibility_user_id: str | None = None,
    visibility_group_id: str | None = None,
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT,
) -> MemoryFact:
    return await service.remember(
        MemoryFactCreate(
            scope_type=scope,
            subject_user_id=user_id,
            group_id=group_id,
            visibility_type=visibility_type,
            visibility_user_id=visibility_user_id,
            visibility_group_id=visibility_group_id,
            kind=kind,
            memory_key=memory_key,
            category=category,
            content=content,
            importance=importance,
            confidence=confidence,
            source_type=source,
            authority=authority,
        )
    )


def _self_target(
    visibility: SelfMemoryVisibility,
    *,
    user_id: str | None = None,
    group_id: str | None = None,
) -> MemoryEntityTarget:
    return MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_SELF,
        scope_type=MemoryScopeType.SELF,
        visibility_type=visibility,
        visibility_user_id=user_id,
        visibility_group_id=group_id,
        block_id="current_self",
    )


@pytest.mark.asyncio
async def test_self_retrieval_hard_filters_global_and_current_visibility(
    database: Database,
) -> None:
    memories, retriever = _retriever(database)
    common = dict(
        content="Yuki 喜欢认真讨论记忆架构",
        user_id=None,
        group_id=None,
        scope=MemoryScopeType.SELF,
        category="self_preference",
        authority=MemoryAuthority.AGENT_REFLECTION,
    )
    global_fact = await _remember(
        memories,
        memory_key="self:global",
        visibility_type=SelfMemoryVisibility.GLOBAL,
        **common,
    )
    private_fact = await _remember(
        memories,
        memory_key="self:private:1001",
        visibility_type=SelfMemoryVisibility.PRIVATE,
        visibility_user_id="1001",
        **common,
    )
    other_private = await _remember(
        memories,
        memory_key="self:private:1002",
        visibility_type=SelfMemoryVisibility.PRIVATE,
        visibility_user_id="1002",
        **common,
    )
    group_fact = await _remember(
        memories,
        memory_key="self:group:2001",
        visibility_type=SelfMemoryVisibility.GROUP,
        visibility_group_id="2001",
        **common,
    )
    other_group = await _remember(
        memories,
        memory_key="self:group:2002",
        visibility_type=SelfMemoryVisibility.GROUP,
        visibility_group_id="2002",
        **common,
    )

    private_result = await retriever.retrieve(
        _query(
            "认真讨论记忆架构",
            _self_target(SelfMemoryVisibility.PRIVATE, user_id="1001"),
        )
    )
    assert {hit.fact.id for hit in private_result.hits} == {global_fact.id, private_fact.id}
    assert other_private.id not in {hit.fact.id for hit in private_result.hits}

    group_result = await retriever.retrieve(
        _query(
            "认真讨论记忆架构",
            _self_target(SelfMemoryVisibility.GROUP, group_id="2001"),
        )
    )
    assert {hit.fact.id for hit in group_result.hits} == {global_fact.id, group_fact.id}
    assert other_group.id not in {hit.fact.id for hit in group_result.hits}


@pytest.mark.asyncio
async def test_query_builder_adds_self_target_only_for_enabled_explicit_recall(
    database: Database,
) -> None:
    settings = make_settings(database.url, self_memory_enabled=True)
    runtime = await RuntimeConfigService(settings=settings, database=database).snapshot(
        user_id="1001"
    )
    builder = MemoryQueryBuilder(MemoryTargetResolver(PeopleRepository(database)))
    inbound = InboundMessage(
        message_id="self-recall-target",
        event_type="message:private",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="你喜欢咖啡吗",
        bot_user_id="8000",
    )

    disabled_for_turn = await builder.build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        self_recall=False,
    )
    enabled_for_turn = await builder.build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        self_recall=True,
    )
    assert MemoryTargetRole.CURRENT_SELF not in {
        target.role for target in disabled_for_turn.targets
    }
    self_target = next(
        target
        for target in enabled_for_turn.targets
        if target.role is MemoryTargetRole.CURRENT_SELF
    )
    assert self_target.visibility_type is SelfMemoryVisibility.PRIVATE
    assert self_target.visibility_user_id == "1001"


@pytest.mark.asyncio
async def test_relevant_chat_adds_one_current_scope_self_episode_without_explicit_recall(
    database: Database,
) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="远野", group_id="2001")
    memories, retriever = _retriever(database)
    first = await _remember(
        memories,
        content="我记得大家第一次一起讨论海边散步时，最后把计划说得很认真",
        memory_key="self_episode:first_walk",
        user_id=None,
        scope=MemoryScopeType.SELF,
        kind=MemoryKind.EPISODE,
        category="self_episode",
        visibility_type=SelfMemoryVisibility.GROUP,
        visibility_group_id="2001",
        authority=MemoryAuthority.AGENT_REFLECTION,
    )
    second = await _remember(
        memories,
        content="后来又聊到海边散步，我发现自己其实很期待那次见面",
        memory_key="self_episode:second_walk",
        user_id=None,
        scope=MemoryScopeType.SELF,
        kind=MemoryKind.EPISODE,
        category="self_episode",
        visibility_type=SelfMemoryVisibility.GROUP,
        visibility_group_id="2001",
        authority=MemoryAuthority.AGENT_REFLECTION,
    )
    other_group = await _remember(
        memories,
        content="另一个群也提到海边散步",
        memory_key="self_episode:other_group",
        user_id=None,
        scope=MemoryScopeType.SELF,
        kind=MemoryKind.EPISODE,
        category="self_episode",
        visibility_type=SelfMemoryVisibility.GROUP,
        visibility_group_id="2002",
        authority=MemoryAuthority.AGENT_REFLECTION,
    )
    context = MemoryContextService(
        query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
        retriever=retriever,
        facts=memories,
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url, self_memory_enabled=True),
        database=database,
    ).snapshot(user_id="1001", group_id="2001")
    inbound = InboundMessage(
        message_id="natural-self-episode",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="远野"),
        text="你还记得我们聊海边散步吗",
        group_id="2001",
        bot_user_id="8000",
    )

    automatic = await context.retrieve_for_turn(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        self_recall=False,
    )
    auto_self = [hit for hit in automatic.hits if hit.target.role is MemoryTargetRole.CURRENT_SELF]
    assert len(auto_self) == 1
    assert auto_self[0].fact.id in {first.id, second.id}
    assert auto_self[0].fact.id != other_group.id

    explicit = await context.retrieve_for_turn(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        self_recall=True,
    )
    explicit_self_ids = {
        hit.fact.id for hit in explicit.hits if hit.target.role is MemoryTargetRole.CURRENT_SELF
    }
    assert {first.id, second.id} <= explicit_self_ids
    assert other_group.id not in explicit_self_ids


def _retriever(database: Database) -> tuple[MemoryFactService, MemoryRetriever]:
    repository = MemoryFactRepository(database)
    return (
        MemoryFactService(repository),
        MemoryRetriever(
            repository=repository,
            lexical_index=SQLiteMemoryFTSIndex(database),
        ),
    )


@pytest.mark.asyncio
async def test_lexical_search_hard_filters_people_groups_and_person_groups(
    database: Database,
) -> None:
    memories, retriever = _retriever(database)
    zhang = await _remember(
        memories, content="喜欢数学竞赛", memory_key="hobby:math", user_id="1001"
    )
    await _remember(memories, content="喜欢数学竞赛", memory_key="hobby:math", user_id="1002")
    first_group = await _remember(
        memories,
        content="群里讨论数学竞赛",
        memory_key="topic:math",
        user_id=None,
        group_id="2001",
        scope=MemoryScopeType.GROUP,
    )
    await _remember(
        memories,
        content="群里讨论数学竞赛",
        memory_key="topic:math",
        user_id=None,
        group_id="2002",
        scope=MemoryScopeType.GROUP,
    )
    member = await _remember(
        memories,
        content="在本群喜欢数学竞赛",
        memory_key="member:math",
        user_id="1001",
        group_id="2001",
        scope=MemoryScopeType.PERSON_GROUP,
    )
    await _remember(
        memories,
        content="在另一个群喜欢数学竞赛",
        memory_key="member:math",
        user_id="1001",
        group_id="2002",
        scope=MemoryScopeType.PERSON_GROUP,
    )

    result = await retriever.retrieve(
        _query("数学竞赛", _target("1001"), _group_target("2001"), _target("1001", group_id="2001"))
    )

    assert {hit.fact.id for hit in result.hits} == {zhang.id, first_group.id, member.id}
    assert [block.target.scope_type for block in result.blocks] == [
        MemoryScopeType.PERSON,
        MemoryScopeType.GROUP,
        MemoryScopeType.PERSON_GROUP,
    ]


@pytest.mark.asyncio
async def test_short_query_and_unsafe_symbols_stay_inside_subject(database: Database) -> None:
    memories, retriever = _retriever(database)
    expected = await _remember(memories, content="住在杭州", memory_key="city", user_id="1001")
    await _remember(memories, content="住在杭州", memory_key="city", user_id="1002")

    short = await retriever.retrieve(_query("杭州"[:2], _target("1001")))
    assert [hit.fact.id for hit in short.hits] == [expected.id]
    safe = build_safe_lexical_query('杭州" OR * (秘密) NEAR', term_limit=4)
    assert "*" not in safe.fts_expression
    assert "(" not in safe.fts_expression
    assert len(safe.terms) <= 4


@pytest.mark.asyncio
async def test_no_match_only_keeps_bounded_explicit_person_preferences(
    database: Database,
) -> None:
    memories, retriever = _retriever(database)
    await _remember(
        memories,
        content="完全无关的高重要事实",
        memory_key="unrelated",
        importance=5,
    )
    preferred = await _remember(
        memories,
        content="回答要简短",
        memory_key="reply:length",
        kind=MemoryKind.PREFERENCE,
        source=MemorySourceType.EXPLICIT,
        importance=5,
    )
    await _remember(
        memories,
        content="群偏好不应常驻",
        memory_key="group:preference",
        user_id=None,
        group_id="2001",
        scope=MemoryScopeType.GROUP,
        kind=MemoryKind.PREFERENCE,
        source=MemorySourceType.EXPLICIT,
    )

    result = await retriever.retrieve(
        _query("量子火箭发动机", _target("1001"), _group_target("2001"))
    )
    assert [hit.fact.id for hit in result.hits] == [preferred.id]
    assert result.hits[0].selection_reason == "always_on_explicit_preference"

    matched_preference = await retriever.retrieve(_query("回答要简短", _target("1001")))
    assert [hit.fact.id for hit in matched_preference.hits] == [preferred.id]


@pytest.mark.asyncio
async def test_overview_and_each_target_have_independent_limits(database: Database) -> None:
    memories, retriever = _retriever(database)
    for user_id in ("1001", "1002"):
        for index in range(3):
            await _remember(
                memories,
                content=f"{user_id} 的事实 {index}",
                memory_key=f"fact:{index}",
                user_id=user_id,
                importance=5 - index,
            )
    result = await retriever.retrieve(
        _query(
            "你记得什么",
            _target("1001"),
            _target("1002", role=MemoryTargetRole.REFERENCED_PERSON),
            mode=MemoryRetrievalMode.OVERVIEW,
            limit=2,
        )
    )
    assert [len(block.hits) for block in result.blocks] == [2, 2]
    assert all(
        hit.fact.subject_user_id == block.target.subject_user_id
        for block in result.blocks
        for hit in block.hits
    )


@pytest.mark.asyncio
async def test_target_resolver_uses_only_real_current_event_references(
    database: Database,
) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="当前", group_id="2001")
    await people.observe(user_id="1002", nickname="被提及", group_id="2001")
    await people.observe(user_id="1003", nickname="被回复", group_id="2001")
    await people.observe(user_id="1004", nickname="其他群", group_id="2002")
    inbound = InboundMessage(
        message_id="targets-1",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前"),
        text="问他们",
        group_id="2001",
        bot_user_id="8000",
        mentioned_user_ids=("1001", "8000", "1002", "1004", "1002"),
        reply_sender_user_id="1003",
    )

    targets = await MemoryTargetResolver(people).resolve(inbound, max_referenced=5)
    referenced = [
        target.subject_user_id
        for target in targets
        if target.role is MemoryTargetRole.REFERENCED_PERSON_GROUP
    ]
    assert referenced == ["1002", "1003"]
    assert all(target.group_id == "2001" for target in targets if target.group_id)

    private = replace(inbound, scope_type=ScopeType.PRIVATE, group_id=None)
    private_targets = await MemoryTargetResolver(people).resolve(private, max_referenced=5)
    assert [target.role for target in private_targets] == [MemoryTargetRole.CURRENT_PERSON]


@pytest.mark.asyncio
async def test_personal_overview_drops_referenced_people(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="当前", group_id="2001")
    await people.observe(user_id="1002", nickname="被提及", group_id="2001")
    inbound = InboundMessage(
        message_id="overview-targets",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前"),
        text="你记得我什么",
        group_id="2001",
        bot_user_id="8000",
        mentioned_user_ids=("1002",),
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot(user_id="1001", group_id="2001")
    query = await MemoryQueryBuilder(MemoryTargetResolver(people)).build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        memory_intent=MemoryQueryIntent(mode=MemoryContextMode.OVERVIEW),
    )

    assert query.mode is MemoryRetrievalMode.OVERVIEW
    assert all(
        target.role
        not in {
            MemoryTargetRole.REFERENCED_PERSON,
            MemoryTargetRole.REFERENCED_PERSON_GROUP,
        }
        for target in query.targets
    )


@pytest.mark.asyncio
async def test_planner_memory_modes_control_semantic_retrieval(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="当前用户")
    inbound = InboundMessage(
        message_id="planner-memory-mode",
        event_type="message:private:friend",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001", nickname="当前用户"),
        text="之前聊过的音乐",
        bot_user_id="8000",
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot(user_id="1001")
    builder = MemoryQueryBuilder(MemoryTargetResolver(people))

    lexical = await builder.build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        memory_mode=MemoryContextMode.LEXICAL,
    )
    hybrid = await builder.build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        memory_mode=MemoryContextMode.HYBRID,
    )
    overview = await builder.build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        memory_mode=MemoryContextMode.OVERVIEW,
    )

    assert lexical.mode is MemoryRetrievalMode.RELEVANT
    assert lexical.semantic_enabled is False
    assert hybrid.mode is MemoryRetrievalMode.RELEVANT
    assert hybrid.semantic_enabled is runtime.memory.semantic_enabled
    assert overview.mode is MemoryRetrievalMode.OVERVIEW
    assert overview.semantic_enabled is False


@pytest.mark.asyncio
async def test_none_memory_mode_returns_without_resolving_targets(database: Database) -> None:
    people = PeopleRepository(database)
    repository = MemoryFactRepository(database)
    context = MemoryContextService(
        query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
        retriever=MemoryRetriever(
            repository=repository,
            lexical_index=SQLiteMemoryFTSIndex(database),
        ),
        facts=MemoryFactService(repository),
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot(user_id="unobserved-user")
    inbound = InboundMessage(
        message_id="planner-memory-none",
        event_type="message:private:friend",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="unobserved-user", nickname=""),
        text="嗯",
        bot_user_id="8000",
    )

    result = await context.retrieve_for_turn(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        memory_mode=MemoryContextMode.NONE,
    )

    assert result.hits == ()
    assert result.blocks == ()
    assert result.semantic_status == "skipped"


@pytest.mark.asyncio
async def test_disabled_retrieval_uses_bounded_current_entities_only(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="当前", group_id="2001")
    await people.observe(user_id="1002", nickname="被提及", group_id="2001")
    repository = MemoryFactRepository(database)
    memories = MemoryFactService(repository)
    current = await _remember(
        memories, content="当前人物事实", memory_key="current", user_id="1001"
    )
    await _remember(memories, content="其他人物事实", memory_key="other", user_id="1002")
    context = MemoryContextService(
        query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
        retriever=MemoryRetriever(
            repository=repository,
            lexical_index=SQLiteMemoryFTSIndex(database),
        ),
        facts=memories,
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot(user_id="1001", group_id="2001")
    runtime = replace(runtime, memory=replace(runtime.memory, retrieval_enabled=False))
    inbound = InboundMessage(
        message_id="disabled-retrieval",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前"),
        text="无关查询",
        group_id="2001",
        bot_user_id="8000",
        mentioned_user_ids=("1002",),
    )
    result = await context.retrieve_for_turn(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
    )

    assert current.id in {hit.fact.id for hit in result.hits}
    assert all(
        block.target.role
        not in {
            MemoryTargetRole.REFERENCED_PERSON,
            MemoryTargetRole.REFERENCED_PERSON_GROUP,
        }
        for block in result.blocks
    )
    assert {hit.fact.subject_user_id for hit in result.hits if hit.fact.subject_user_id} == {"1001"}


@pytest.mark.asyncio
async def test_missing_fts_surfaces_stable_error_instead_of_full_scan(database: Database) -> None:
    _, retriever = _retriever(database)
    async with database.sessions() as session, session.begin():
        await session.execute(text("DROP TABLE memory_facts_fts"))
    with pytest.raises(MemoryRetrievalError) as captured:
        await retriever.retrieve(_query("任意查询", _target("1001")))
    assert captured.value.code == "memory_index_unavailable"


def test_ranker_uses_exact_fields_then_stable_fact_id() -> None:
    target = _target("1001")
    now = datetime(2026, 8, 1, tzinfo=UTC)

    def fact(
        fact_id: int,
        *,
        key: str,
        category: str,
        content: str,
        importance: int = 3,
        confidence: float = 0.8,
    ) -> MemoryFact:
        return MemoryFact(
            id=fact_id,
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.FACT,
            memory_key=key,
            category=category,
            content=content,
            normalized_content=content.casefold(),
            importance=importance,
            confidence=confidence,
            source_type=MemorySourceType.AUTOMATIC,
            status="active",
            created_at=now,
            updated_at=now,
        )

    facts = (
        fact(4, key="other", category="other", content="数学竞赛相关"),
        fact(3, key="other", category="数学竞赛", content="相关内容"),
        fact(2, key="other", category="other", content="数学竞赛"),
        fact(1, key="数学竞赛", category="other", content="相关内容"),
    )
    candidates = tuple(
        MemoryLexicalCandidate(fact_id=item.id, target=target, fts_rank=1.0) for item in facts
    )
    ranked = MemoryRanker().rank_lexical(
        facts=facts,
        candidates=candidates,
        target=target,
        normalized_query="数学竞赛",
        limit=10,
    )
    assert [hit.fact.id for hit in ranked] == [1, 2, 3, 4]
    assert [hit.selection_reason for hit in ranked[:3]] == [
        "memory_key_exact",
        "content_exact",
        "category_exact",
    ]

    tied = (
        fact(9, key="other", category="other", content="匹配词条", importance=4, confidence=0.7),
        fact(8, key="other", category="other", content="匹配词条", importance=5),
        fact(7, key="other", category="other", content="匹配词条", importance=4, confidence=0.9),
        fact(6, key="other", category="other", content="匹配词条", importance=4, confidence=0.9),
    )
    tied_candidates = tuple(
        MemoryLexicalCandidate(fact_id=item.id, target=target, fts_rank=1.0) for item in tied
    )
    stable = MemoryRanker().rank_lexical(
        facts=tied,
        candidates=tied_candidates,
        target=target,
        normalized_query="别的查询",
        limit=10,
    )
    assert [hit.fact.id for hit in stable] == [8, 6, 7, 9]


@pytest.mark.asyncio
async def test_superseded_fact_is_physically_indexed_but_not_retrieved(
    database: Database,
) -> None:
    memories, retriever = _retriever(database)
    old = await _remember(memories, content="准备考研", memory_key="education:plan", user_id="1001")
    new = await _remember(
        memories, content="决定直接工作", memory_key="education:plan", user_id="1001"
    )
    old_result = await retriever.retrieve(_query("准备考研", _target("1001")))
    new_result = await retriever.retrieve(_query("直接工作", _target("1001")))
    assert old.id not in {hit.fact.id for hit in old_result.hits}
    assert [hit.fact.id for hit in new_result.hits] == [new.id]


@pytest.mark.asyncio
async def test_ten_thousand_fact_regression_never_crosses_person_scope(
    database: Database,
) -> None:
    now = datetime.now(UTC).isoformat()
    people = [
        {
            "user_id": f"u{person:03d}",
            "nickname": f"用户{person}",
            "first_seen": now,
            "last_seen": now,
        }
        for person in range(100)
    ]
    facts = [
        {
            "scope": "person",
            "user_id": f"u{person:03d}",
            "kind": "fact",
            "memory_key": f"bulk:{index}",
            "category": "performance",
            "content": f"共同关键词 性能事实 {person}-{index}",
            "normalized": f"共同关键词 性能事实 {person}-{index}",
            "importance": 3,
            "confidence": 0.8,
            "source": "automatic",
            "status": "active",
            "created": now,
            "updated": now,
        }
        for person in range(100)
        for index in range(100)
    ]
    async with database.sessions() as session, session.begin():
        await session.execute(
            text(
                """
                INSERT INTO people (
                    user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at
                ) VALUES (
                    :user_id, :nickname, 1, 0, :first_seen, :last_seen
                )
                """
            ),
            people,
        )
        await session.execute(
            text(
                """
                INSERT INTO memory_facts (
                    scope_type, subject_user_id, kind, memory_key, category,
                    content, normalized_content, importance, confidence,
                    source_type, status, created_at, updated_at
                ) VALUES (
                    :scope, :user_id, :kind, :memory_key, :category,
                    :content, :normalized, :importance, :confidence,
                    :source, :status, :created, :updated
                )
                """
            ),
            facts,
        )

    repository = MemoryFactRepository(database)
    retriever = MemoryRetriever(
        repository=repository,
        lexical_index=SQLiteMemoryFTSIndex(database),
    )
    result = await retriever.retrieve(_query("共同关键词", _target("u042"), limit=7))
    assert len(result.hits) == 7
    assert result.candidate_count == 50
    assert {hit.fact.subject_user_id for hit in result.hits} == {"u042"}
