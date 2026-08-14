"""Context budgets, batch projections, and SQLite worker-safety tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage, SenderIdentity
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.memory.enums import MemoryScopeType, MemorySourceType
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository, PeopleRepository
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.context_assembler import ContextAssembler


def test_current_self_metadata_is_emitted_only_with_selected_facts() -> None:
    base = {
        "current_person": {
            "user_id": "1001",
            "nickname": "用户",
            "display_name": "用户",
            "aliases": [],
            "facts": [],
        },
        "scene": {"type": "private", "group_id": None, "group_card": ""},
        "available_memory_subjects": [
            {"subject_ref": "current_speaker", "display_name": "用户"},
            {"subject_ref": "self", "display_name": "Yuki"},
        ],
    }
    empty, _ = ContextAssembler._fit_metadata(
        {**base, "current_self": {"facts": []}},
        4000,
    )
    assert "current_self" not in {item["id"] for item in empty["items"]}

    visible_fact = {
        "fact_id": 7,
        "kind": "preference",
        "category": "self_preference",
        "content": "我偏好先理解问题再回答",
        "confidence": 0.8,
        "importance": 4,
    }
    selected, fact_ids = ContextAssembler._fit_metadata(
        {**base, "current_self": {"facts": [visible_fact]}},
        4000,
    )
    blocks = {item["id"]: item["data"] for item in selected["items"]}
    assert blocks["current_self"] == {"facts": [visible_fact]}
    assert fact_ids == (7,)
    assert "visibility_user_id" not in json.dumps(blocks["current_self"])


@pytest.mark.asyncio
async def test_context_assembler_enforces_one_dynamic_character_budget(
    database: Database,
) -> None:
    settings = make_settings(database.url, max_context_characters=1200)
    harness = build_harness(database, settings)
    memories = MemoryFactService(MemoryFactRepository(database))
    for index in range(30):
        await memories.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id="1001",
                kind="fact",
                category="fact",
                source_type=MemorySourceType.AUTOMATIC,
                confidence=0.9,
                memory_key=f"person-{index}",
                content=f"人物事实 {index} " + "很长的内容" * 80,
                importance=5 if index == 0 else 1,
            ),
            limit=100,
        )
        await memories.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.GROUP,
                group_id="2001",
                kind="fact",
                category="fact",
                source_type=MemorySourceType.AUTOMATIC,
                confidence=0.9,
                memory_key=f"group-{index}",
                content=f"群事实 {index} " + "另一段很长的内容" * 80,
                importance=5 if index == 0 else 1,
            ),
            limit=100,
        )
    await harness.groups.set_enabled("2001", True)

    message = InboundMessage(
        message_id="bounded-context",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="测试用户", group_card="测试名片"),
        text="请根据已有信息简短回答",
        group_id="2001",
        mentions_bot=True,
        bot_user_id="9999",
    )
    await harness.processor.handle(message, MemorySender())

    request = harness.provider.requests[0]  # type: ignore[attr-defined]
    metadata_index = next(
        index
        for index, item in enumerate(request.messages)
        if item.role == "system" and '"id":"context.people_and_scene"' in (item.content or "")
    )
    envelope = request.messages[metadata_index].content or ""
    envelope_items = json.loads(envelope[envelope.index("[") :])
    context_item = next(item for item in envelope_items if item["id"] == "context.people_and_scene")
    payload = context_item["data"]
    payload_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    payload_items = {item["id"]: item["data"] for item in payload["items"]}
    history_characters = sum(
        len(item.content or "") for item in request.messages[metadata_index + 1 :]
    )

    assert len(payload_text) <= settings.max_context_characters * 55 // 100
    assert len(payload_text) + history_characters <= settings.max_context_characters
    current_history = request.messages[-1].content or ""
    assert current_history.startswith("[测试名片|QQ:1001]\n#")
    assert current_history.endswith(">请根据已有信息简短回答")
    assert payload_items["current_person"]["user_id"] == "1001"
    assert len(payload_items["current_person"]["facts"]) < 30
    assert len(payload_items["current_group"]["facts"]) < 30
    assert not any(key.startswith("person_memory.") for key in payload_items)
    assert not any(key.startswith("current_group.fact.") for key in payload_items)


@pytest.mark.asyncio
async def test_context_exposes_event_bound_memory_subject_refs(database: Database) -> None:
    settings = make_settings(database.url, self_memory_enabled=True)
    harness = build_harness(database, settings)
    await harness.groups.set_enabled("2001", True)
    await harness.profiles.observe(
        user_id="1002",
        nickname="群友昵称",
        group_id="2001",
        group_card="查无此人",
    )
    message = InboundMessage(
        message_id="memory-subject-refs",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="提问者"),
        text="查一下被提及群友的记忆",
        group_id="2001",
        mentions_bot=True,
        bot_user_id="9999",
        mentioned_user_ids=("9999", "1002"),
        reply_sender_user_id="1002",
    )

    await harness.processor.handle(message, MemorySender())

    request = harness.provider.requests[0]  # type: ignore[attr-defined]
    envelope = next(
        item.content or ""
        for item in request.messages
        if item.role == "system" and '"id":"context.people_and_scene"' in (item.content or "")
    )
    envelope_items = json.loads(envelope[envelope.index("[") :])
    context_item = next(item for item in envelope_items if item["id"] == "context.people_and_scene")
    payload_items = {item["id"]: item["data"] for item in context_item["data"]["items"]}
    subjects = payload_items["available_memory_subjects"]

    assert subjects == [
        {"subject_ref": "current_speaker", "display_name": "提问者"},
        {"subject_ref": "self", "display_name": "Yuki"},
        {"subject_ref": "mentioned_user_1", "display_name": "查无此人"},
        {"subject_ref": "replied_message_author", "display_name": "查无此人"},
    ]
    assert all("user_id" not in subject for subject in subjects)


@pytest.mark.asyncio
async def test_chat_event_preserves_sender_identity_snapshot_for_prompt(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    message = InboundMessage(
        message_id="identity-snapshot",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(
            user_id="1001",
            nickname="平台昵称",
            group_card="发言时群名片",
        ),
        text="这是我说的话",
        group_id="2001",
        bot_user_id="9999",
    )

    event, created = await ledger.append_inbound(message, bot_user_id="9999")

    assert created
    assert event.sender_nickname == "平台昵称"
    assert event.sender_group_card == "发言时群名片"
    await PeopleRepository(database).observe(
        user_id="1001",
        nickname="后来昵称",
        group_id="2001",
        group_card="后来群名片",
    )
    persisted = await ledger.get_event(event.id)
    assert persisted is not None
    assert persisted.sender_nickname == "平台昵称"
    assert persisted.sender_group_card == "发言时群名片"
    rendered = ChatEventPromptRenderer((persisted,)).render_event(persisted)
    assert rendered == "[发送者:发言时群名片|QQ:1001|消息:identity-snapshot] 这是我说的话"


def test_history_prompt_keeps_speakers_and_reply_target_self_contained() -> None:
    now = datetime.now(UTC)
    events = (
        EventRecord(
            id=1,
            bot_user_id="9999",
            platform_message_id="member-message",
            scope_type=ScopeType.GROUP,
            sender_user_id="1002",
            sender_nickname="池宇健",
            sender_group_card="池宇健",
            direction="inbound",
            content="这个项目完结",
            visual_summary="",
            segments=(),
            occurred_at=now,
            group_id="2001",
        ),
        EventRecord(
            id=2,
            bot_user_id="9999",
            platform_message_id="yuki-message",
            scope_type=ScopeType.GROUP,
            sender_user_id="9999",
            direction="outbound",
            content="说好的完结呢",
            visual_summary="",
            segments=(),
            occurred_at=now,
            group_id="2001",
        ),
        EventRecord(
            id=3,
            bot_user_id="9999",
            platform_message_id="current-message",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            sender_nickname="远野",
            sender_group_card="远野",
            direction="inbound",
            content="完结的不是我啊",
            visual_summary="",
            segments=(),
            occurred_at=now,
            group_id="2001",
            reply_to_message_id="yuki-message",
            reply_sender_user_id="9999",
            mentioned_user_ids=("1002", "9999"),
        ),
    )
    inbound = InboundMessage(
        message_id="current-message",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="远野", group_card="远野"),
        text="完结的不是我啊",
        group_id="2001",
        bot_user_id="9999",
        reply_to_message_id="yuki-message",
        reply_sender_user_id="9999",
        mentioned_user_ids=("1002", "9999"),
    )

    bounded = ContextAssembler._bounded_history(
        events,
        inbound=inbound,
        content=inbound.text,
        character_budget=10_000,
        event_limit=30,
        low_watermark_ratio=0.67,
        anchor_event_id=None,
    )

    history = bounded.history_messages
    assert [item.role for item in history] == ["user", "assistant"]
    assert history[0].content == "[池宇健|QQ:1002]\n#1>这个项目完结"
    assert history[1].content == "[Yuki|QQ:9999]\n#2>说好的完结呢"
    assert bounded.current_message.content == (
        "[远野|QQ:1001]\n#3|回复:#2/Yuki/QQ:9999|提及:池宇健/QQ:1002,Yuki/QQ:9999>完结的不是我啊"
    )


def test_history_renderer_uses_configured_bot_name_for_missing_event_identity() -> None:
    event = EventRecord(
        id=8,
        bot_user_id="9999",
        platform_message_id="bot-message",
        scope_type=ScopeType.GROUP,
        sender_user_id="9999",
        direction="outbound",
        content="我在这里",
        visual_summary="",
        segments=(),
        occurred_at=datetime.now(UTC),
        group_id="2001",
    )

    rendered = ChatEventPromptRenderer(
        (event,),
        bot_display_name="Mika",
    ).render_reference_event(event)

    assert rendered == "[Mika|QQ:9999]\n#8>我在这里"


def test_main_agent_history_groups_adjacent_messages_from_the_same_identity() -> None:
    now = datetime.now(UTC)
    events = (
        EventRecord(
            id=48217,
            bot_user_id="9999",
            platform_message_id="qq-message-1",
            scope_type=ScopeType.GROUP,
            sender_user_id="2186567848",
            sender_group_card="远野",
            direction="inbound",
            content="你觉得这样设计怎么样？",
            visual_summary="",
            segments=(),
            occurred_at=now,
            group_id="2001",
        ),
        EventRecord(
            id=48219,
            bot_user_id="9999",
            platform_message_id="qq-message-2",
            scope_type=ScopeType.GROUP,
            sender_user_id="2186567848",
            sender_group_card="远野",
            direction="inbound",
            content="那就按这个方向做吧。",
            visual_summary="",
            segments=(),
            occurred_at=now,
            group_id="2001",
        ),
    )

    history = ChatEventPromptRenderer(events).main_agent_history(events)

    assert history == (
        (
            48217,
            (48217, 48219),
            ChatMessage(
                role="user",
                content=(
                    "[远野|QQ:2186567848]\n"
                    "#48217>你觉得这样设计怎么样？\n"
                    "#48219>那就按这个方向做吧。"
                ),
            ),
        ),
    )


def test_history_window_rolls_in_blocks_between_high_and_low_watermarks() -> None:
    def rendered(
        start: int,
        end: int,
    ) -> tuple[tuple[int, tuple[int, ...], ChatMessage], ...]:
        return tuple(
            (
                event_id,
                (event_id,),
                ChatMessage(role="user", content=f"message-{event_id}"),
            )
            for event_id in range(start, end + 1)
        )

    seeded = ContextAssembler._select_history_window(
        rendered(1, 5),
        anchor_event_id=None,
        high_event_limit=5,
        high_character_limit=10_000,
        low_watermark_ratio=0.6,
        fallback_anchor_event_id=6,
    )
    assert [item.content for item in seeded.messages] == ["message-3", "message-4", "message-5"]
    assert seeded.anchor_event_id == 3
    assert seeded.event_ids == (3, 4, 5)
    assert not seeded.rolled

    appended = ContextAssembler._select_history_window(
        rendered(3, 6),
        anchor_event_id=seeded.anchor_event_id,
        high_event_limit=5,
        high_character_limit=10_000,
        low_watermark_ratio=0.6,
        fallback_anchor_event_id=7,
    )
    assert [item.content for item in appended.messages] == [
        "message-3",
        "message-4",
        "message-5",
        "message-6",
    ]
    assert appended.anchor_event_id == 3
    assert appended.event_ids == (3, 4, 5, 6)
    assert not appended.rolled

    rolled = ContextAssembler._select_history_window(
        rendered(3, 8),
        anchor_event_id=appended.anchor_event_id,
        high_event_limit=5,
        high_character_limit=10_000,
        low_watermark_ratio=0.6,
        fallback_anchor_event_id=9,
    )
    assert [item.content for item in rolled.messages] == ["message-6", "message-7", "message-8"]
    assert rolled.anchor_event_id == 6
    assert rolled.event_ids == (6, 7, 8)
    assert rolled.rolled


def test_history_window_character_roll_keeps_a_contiguous_recent_block() -> None:
    rendered = tuple(
        (event_id, (event_id,), ChatMessage(role="user", content=str(event_id) * 30))
        for event_id in range(1, 6)
    )
    selection = ContextAssembler._select_history_window(
        rendered,
        anchor_event_id=1,
        high_event_limit=10,
        high_character_limit=100,
        low_watermark_ratio=0.6,
        fallback_anchor_event_id=6,
    )

    assert [item.content for item in selection.messages] == ["4" * 30, "5" * 30]
    assert selection.anchor_event_id == 4
    assert selection.event_ids == (4, 5)
    assert selection.rolled


@pytest.mark.asyncio
async def test_profile_batch_queries_keep_group_cards_isolated(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(
        user_id="1001",
        nickname="甲",
        group_id="2001",
        group_card="一群名片",
    )
    await people.observe(
        user_id="1001",
        nickname="甲",
        group_id="2002",
        group_card="二群名片",
    )
    await people.observe(
        user_id="1002",
        nickname="乙",
        group_id="2001",
        group_card="乙的一群名片",
    )

    first_group = await people.get_many(("1001", "1002"), group_id="2001")
    second_group = await people.get_many(("1001", "1002"), group_id="2002")

    assert first_group["1001"].display_name == "一群名片"
    assert first_group["1002"].display_name == "乙的一群名片"
    assert second_group["1001"].display_name == "二群名片"
    assert second_group["1002"].display_name == "乙"


@pytest.mark.asyncio
async def test_exact_name_lookup_is_unique_to_the_current_group(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(
        user_id="1001",
        nickname="旧昵称",
        group_id="2001",
        group_card="本群名片",
    )
    await people.observe(
        user_id="1001",
        nickname="新昵称",
        group_id="2002",
        group_card="别群名片",
    )
    await people.observe(
        user_id="1002",
        nickname="同名",
        group_id="2001",
        group_card="本群同名",
    )
    await people.observe(
        user_id="1003",
        nickname="同名",
        group_id="2001",
        group_card="另一个同名",
    )

    assert await people.find_group_members_by_exact_name("本群名片", "2001") == ("1001",)
    assert await people.find_group_members_by_exact_name("旧昵称", "2001") == ("1001",)
    assert await people.find_group_members_by_exact_name("别群名片", "2001") == ()
    assert await people.find_group_members_by_exact_name("同名", "2001") == (
        "1002",
        "1003",
    )

    exact = await people.search_group_member_names(" 本群名片 ", "2001")
    assert exact[0].user_id == "1001"
    assert exact[0].exact

    fuzzy = await people.search_group_member_names("本群名片片", "2001")
    assert fuzzy[0].user_id == "1001"
    assert not fuzzy[0].exact
    assert fuzzy[0].score >= 0.35


@pytest.mark.asyncio
async def test_sqlite_connections_enable_wal_and_bounded_busy_wait(database: Database) -> None:
    async with database.sessions() as session:
        journal_mode = await session.scalar(text("PRAGMA journal_mode"))
        busy_timeout = await session.scalar(text("PRAGMA busy_timeout"))
        foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))

    assert str(journal_mode).casefold() == "wal"
    assert int(busy_timeout or 0) == 5000
    assert int(foreign_keys or 0) == 1


@pytest.mark.asyncio
async def test_only_facts_surviving_context_budget_are_marked_injected(
    database: Database,
) -> None:
    repository = MemoryFactRepository(database)
    memories = MemoryFactService(repository)
    selected = await memories.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind="fact",
            category="profile",
            source_type=MemorySourceType.AUTOMATIC,
            confidence=0.9,
            memory_key="selected",
            content="短事实",
            importance=5,
        )
    )
    omitted = await memories.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind="fact",
            category="profile",
            source_type=MemorySourceType.AUTOMATIC,
            confidence=0.9,
            memory_key="omitted",
            content="不会进入预算的事实" * 200,
            importance=1,
        )
    )
    context = {
        "current_person": {
            "user_id": "1001",
            "nickname": "测试",
            "display_name": "测试",
            "facts": [
                {"fact_id": selected.id, "content": selected.content, "importance": 5},
                {"fact_id": omitted.id, "content": omitted.content, "importance": 1},
            ],
        },
        "scene": {"type": "private", "group_id": None},
    }
    contributions = ContextAssembler._context_contributions(context)
    required_cost = sum(item.cost for item in contributions if item.required)
    selected_cost = next(item.cost for item in contributions if item.id == "person_memory.0")
    _, fact_ids = ContextAssembler._fit_metadata(
        context,
        required_cost + selected_cost,
    )
    await memories.mark_injected(fact_ids)

    selected_row = await repository.get_fact(selected.id)
    omitted_row = await repository.get_fact(omitted.id)
    assert fact_ids == (selected.id,)
    assert selected_row is not None and selected_row.last_injected_at is not None
    assert omitted_row is not None and omitted_row.last_injected_at is None


def test_memory_budget_uses_rerank_score_without_exposing_internal_fields() -> None:
    context = {
        "current_person": {
            "user_id": "1001",
            "facts": [
                {
                    "fact_id": 1,
                    "content": "低分且很短",
                    "importance": 5,
                    "_retrieval_score": 0.1,
                },
                {
                    "fact_id": 2,
                    "content": "高分事实内容",
                    "importance": 1,
                    "_retrieval_score": 0.9,
                },
            ],
        },
        "scene": {"type": "private", "group_id": None},
    }
    contributions = ContextAssembler._context_contributions(context)
    required_cost = sum(item.cost for item in contributions if item.required)
    high_score = next(item for item in contributions if item.id == "person_memory.1")
    rendered, fact_ids = ContextAssembler._fit_metadata(
        context,
        required_cost + high_score.cost,
    )

    assert fact_ids == (2,)
    assert "_retrieval_score" not in str(rendered)


def test_recent_delivery_projects_only_confirmed_transport_metadata() -> None:
    now = datetime.now(UTC)

    def event(
        event_id: int,
        platform_id: str,
        segments: tuple[dict[str, object], ...],
        *,
        direction: str = "outbound",
    ) -> EventRecord:
        return EventRecord(
            id=event_id,
            bot_user_id="9000",
            platform_message_id=platform_id,
            scope_type=ScopeType.GROUP,
            sender_user_id="9000",
            direction=direction,
            content="模型可能声称发过表情，但这里不能作为证据",
            visual_summary="不应投影的图片描述",
            segments=segments,
            occurred_at=now,
            group_id="2001",
        )

    recent = ContextAssembler._recent_delivery(
        (
            event(1, "out-fake", ({"type": "image", "data": {"emoji_id": "fake"}},)),
            event(2, "real-text", ({"type": "text", "data": {"text": "你好"}},)),
            event(
                3,
                "real-emoji",
                ({"type": "image", "data": {"emoji_id": "secret-id", "summary": "秘密"}},),
            ),
            event(4, "real-voice", ({"type": "record", "data": {"summary": "秘密"}},)),
            event(5, "real-image", ({"type": "image", "data": {"emoji_id": ""}},)),
        )
    )

    assert [item["platform_message_id"] for item in recent] == [
        "real-emoji",
        "real-voice",
        "real-image",
    ]
    assert [item["media_kinds"] for item in recent] == [
        ["emoji_image"],
        ["voice"],
        ["image"],
    ]
    serialized = json.dumps(recent, ensure_ascii=False)
    assert "secret-id" not in serialized
    assert "秘密" not in serialized
    assert "模型可能" not in serialized


@pytest.mark.asyncio
async def test_recent_delivery_is_trusted_and_exact_conversation_only(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    for group_id, platform_id in (("2001", "same-group"), ("2002", "other-group")):
        await ledger.append(
            bot_user_id="9000",
            platform_message_id=platform_id,
            scope_type=ScopeType.GROUP,
            sender_user_id="9000",
            direction="outbound",
            content="",
            segments=(
                {
                    "type": "image",
                    "data": {"emoji_id": f"hidden-{group_id}", "summary": "不可信描述"},
                },
            ),
            group_id=group_id,
            private_peer_user_id=None,
            sender_is_bot=True,
        )
    harness = build_harness(database, make_settings(database.url))
    await harness.groups.set_enabled("2001", True)
    await harness.processor.handle(
        InboundMessage(
            message_id="delivery-context-current",
            event_type="message:group:normal",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity(user_id="1001"),
            text="刚才发了吗",
            group_id="2001",
            mentions_bot=True,
            bot_user_id="9000",
        ),
        MemorySender(),
    )

    request = harness.provider.requests[0]  # type: ignore[attr-defined]
    prompt = "\n".join(message.content or "" for message in request.messages)
    assert '"id":"runtime.recent_delivery"' in prompt
    assert "same-group" in prompt
    assert "other-group" not in prompt
    assert "emoji_image" in prompt
    assert "hidden-2001" not in prompt
    assert "不可信描述" not in prompt
