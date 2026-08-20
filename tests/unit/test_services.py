"""Deduplication, rate limiting, rendering, and persistence tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository, ProcessedEventRepository
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.renderer import (
    clean_model_output,
    sanitize_input,
    split_qq_message,
)


def test_application_cli_import_has_no_service_cycle() -> None:
    """The installed CLI must import the complete application graph."""

    from qq_ai_bot.main import run

    assert callable(run)


@pytest.mark.asyncio
async def test_duplicate_event_is_claimed_once(database: Database) -> None:
    service = DeduplicationService(ProcessedEventRepository(database), ttl_seconds=60)
    assert await service.claim("same-event")
    assert not await service.claim("same-event")


@pytest.mark.asyncio
async def test_expired_events_can_be_cleaned(database: Database) -> None:
    repository = ProcessedEventRepository(database)
    await repository.claim("old", expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert await repository.cleanup_expired() == 1


@pytest.mark.asyncio
async def test_user_and_group_rate_limits_have_separate_scopes() -> None:
    user_limiter = SlidingWindowRateLimiter(per_user=1, per_group=10)
    assert (await user_limiter.check(user_id="1", group_id="9", category="chat")).allowed
    denied_user = await user_limiter.check(user_id="1", group_id="10", category="chat")
    assert not denied_user.allowed and denied_user.scope == "user"

    group_limiter = SlidingWindowRateLimiter(per_user=10, per_group=1)
    assert (await group_limiter.check(user_id="1", group_id="9", category="chat")).allowed
    denied_group = await group_limiter.check(user_id="2", group_id="9", category="chat")
    assert not denied_group.allowed and denied_group.scope == "group"
    assert (await group_limiter.check(user_id="2", group_id="9", category="command")).allowed


def test_long_reply_splits_by_paragraph_sentence_and_character() -> None:
    text = "第一段。第二句。\n\n" + "😀" * 25
    chunks = split_qq_message(text, limit=10)
    assert chunks
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_markdown_cleanup_and_control_character_sanitization() -> None:
    assert (
        clean_model_output("# 标题\n[链接](https://example.test)", max_characters=100)
        == "标题\n链接 (https://example.test)"
    )
    assert sanitize_input("a\x00b\r\nc") == "ab\nc"


def test_model_output_never_exposes_internal_history_timestamps() -> None:
    text = (
        "[21:10] 先确认一下。[07-27 03:14] 五分钟后提醒你。\n"
        "[07-27 03:13 QQ 2186567848] 这也是内部历史标记。"
    )

    assert clean_model_output(text, max_characters=200) == (
        "先确认一下。五分钟后提醒你。\n这也是内部历史标记。"
    )


def test_model_output_never_exposes_event_identity_envelopes() -> None:
    text = (
        "前缀 [发送者:奶鼠|QQ:2186567848|消息:1742835379|"
        "时间:2026-08-05T15:39:05.884399] 看到了。\n"
        "[发送者:远野|QQ:2186567848|消息:1742835380|回复:Yuki/消息:1742835379] "
        "第二句。"
    )

    assert clean_model_output(text, max_characters=200) == "前缀 看到了。\n第二句。"


def test_model_output_removes_shortened_event_identity_envelopes() -> None:
    text = (
        "[发送者:Yuki|QQ:380726517] 安全组只放行常用 IP。\n"
        "前缀 [发送者:远野|QQ:2186567848|时间:2026-08-06T15:00:00] 第二句。"
    )

    assert clean_model_output(text, max_characters=200) == ("安全组只放行常用 IP。\n前缀 第二句。")


def test_model_output_removes_main_agent_event_envelopes_and_prefixes() -> None:
    text = (
        "[远野|QQ:2186567848]\n"
        "#48217>你觉得这样设计怎么样？\n"
        "#48219>那就按这个方向做吧。\n"
        "[Yuki|QQ:380726517]\n"
        "#48220|回复:#48219/远野/QQ:2186567848|提及:远野/QQ:2186567848>已经处理好了。"
    )

    assert clean_model_output(text, max_characters=200) == (
        "你觉得这样设计怎么样？\n那就按这个方向做吧。\n已经处理好了。"
    )


def test_model_output_keeps_event_like_text_inside_an_ordinary_sentence() -> None:
    text = "我说的 #48217> 只是正文中的普通示例，不是泄漏的行首信封。"

    assert clean_model_output(text, max_characters=200) == text


def test_prompt_history_keeps_blockquote_body() -> None:
    from datetime import UTC, datetime

    from qq_ai_bot.domain.conversations import ScopeType
    from qq_ai_bot.event_prompt import ChatEventPromptRenderer
    from qq_ai_bot.persistence.repository_records import EventRecord

    event = EventRecord(
        id=32915,
        bot_user_id="380726517",
        platform_message_id="m-32915",
        scope_type=ScopeType.GROUP,
        sender_user_id="380726517",
        direction="outbound",
        content="> 你俩一个揭穿一个补刀，配合得还挺默契\n> 不过反正格洛腾迪克本人也犯过这错",
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 8, 20, 11, 21, 42, tzinfo=UTC),
        sender_group_card="Yuki",
        group_id="1049765710",
    )
    rendered = ChatEventPromptRenderer((event,), bot_display_name="Yuki").render_reference_event(
        event
    )

    assert "> 你俩一个揭穿一个补刀，配合得还挺默契" in rendered
    assert clean_model_output(event.content, max_characters=200) == (
        "你俩一个揭穿一个补刀，配合得还挺默契\n不过反正格洛腾迪克本人也犯过这错"
    )


def test_model_output_strips_echoed_blockquote_prefixes() -> None:
    text = (
        "> Diana 这是被你自己的烤肉账撑爆了吗，连网关都502了\n"
        "> 那这局算我赢啦，趁你宕机我先溜一步，嘿嘿……啊不是，喵！"
    )

    assert clean_model_output(text, max_characters=200) == (
        "Diana 这是被你自己的烤肉账撑爆了吗，连网关都502了\n"
        "那这局算我赢啦，趁你宕机我先溜一步，嘿嘿……啊不是，喵！"
    )


def test_model_output_keeps_comparison_greater_than_in_a_sentence() -> None:
    text = "分数 > 80 才算过，这不是引用前缀。"

    assert clean_model_output(text, max_characters=200) == text


@pytest.mark.asyncio
async def test_bot_aware_private_scope_isolation(database: Database) -> None:
    repository = EventLedgerRepository(database)
    first = ConversationScope.private("bot-a", "1")
    second = ConversationScope.private("bot-b", "1")
    for index, scope in enumerate((first, second), start=1):
        await repository.append(
            bot_user_id=scope.bot_user_id,
            platform_message_id=f"message-{index}",
            scope_type=ScopeType.PRIVATE,
            private_peer_user_id="1",
            sender_user_id="1",
            direction="inbound",
            content=scope.bot_user_id,
        )
    assert [row.content for row in await repository.list_scope_recent(first, limit=10)] == ["bot-a"]
    assert [row.content for row in await repository.list_scope_recent(second, limit=10)] == [
        "bot-b"
    ]


@pytest.mark.asyncio
async def test_database_restart_restores_history(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'restart.db').as_posix()}"
    first_db = Database(url)
    await first_db.create_schema()
    identity = ConversationScope.private("bot", "42")
    writer = ScopedEventLedgerUnitOfWork(first_db, config=RollupPolicyConfig())
    await writer.append(
        scope=identity,
        platform_message_id="durable-1",
        sender_user_id="42",
        direction="inbound",
        content="durable",
    )
    await first_db.close()

    second_db = Database(url)
    try:
        history = await EventLedgerRepository(second_db).list_scope_recent(identity, limit=10)
        assert [item.content for item in history] == ["durable"]
    finally:
        await second_db.close()
