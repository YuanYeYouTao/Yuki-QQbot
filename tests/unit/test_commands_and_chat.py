"""Command behavior, cancellation, and send-failure semantics."""

from __future__ import annotations

import asyncio

import pytest
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    OutboundMedia,
    OutboundMessage,
    SenderIdentity,
)
from qq_ai_bot.emoji.models import (
    EmojiIntent,
    EmojiPlacement,
    EmojiPreparationResult,
    EmojiPreparationStatus,
    EmojiReplyMode,
    EmojiReplyPlan,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.enums import MemoryScopeType, MemorySourceType
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.planner import (
    DeliveryMode,
    FakePlannerProvider,
    PlannerDecision,
    PlannerObservability,
    PlannerReasonCode,
    ToolMode,
    TurnPlan,
)
from qq_ai_bot.planner.models import ToolSelection
from qq_ai_bot.planner.service import PlannerService
from qq_ai_bot.services.processor import MENTION_ONLY_CONTEXT, _vision_failure_message


def inbound(
    text: str,
    *,
    message_id: str,
    user_id: str = "1001",
    group_id: str | None = None,
    mentions_bot: bool = False,
    unsupported: bool = False,
) -> InboundMessage:
    from qq_ai_bot.domain.messages import AttachmentKind, MessageAttachment

    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id),
        text=text,
        group_id=group_id,
        mentions_bot=mentions_bot,
        attachments=(MessageAttachment(AttachmentKind.IMAGE, "image"),) if unsupported else (),
    )


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        ("media_download_timeout", "图片下载超时"),
        ("get_image_failed", "NapCat 未能取得图片资源"),
        ("download_failed", "图片资源下载失败"),
        ("private_url", "图片资源下载失败"),
        ("corrupt_image", "图片文件无法解析"),
        ("too_large", "超过处理范围"),
        ("queue_timeout", "图片识别任务较多"),
        ("timeout", "视觉模型响应超时"),
        ("provider_unavailable", "视觉模型暂时不可用"),
    ],
)
def test_visual_failures_have_distinct_user_messages(
    error_code: str,
    expected: str,
) -> None:
    assert expected in _vision_failure_message(error_code, reply_only=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("help", "QQ AI 助手命令"),
        ("status", "服务版本"),
        ("ping", "pong"),
        ("stop", "当前没有正在处理"),
    ],
)
async def test_basic_commands(
    database: Database,
    command: str,
    expected: str,
) -> None:
    harness = build_harness(database, make_settings(database.url))
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound(f"/ai {command}", message_id=f"cmd-{command}"), sender
    )
    assert result.handled and result.sent_messages == 1
    assert expected in sender.messages[0].text


@pytest.mark.asyncio
async def test_capabilities_reports_complete_range_for_current_real_qq(
    database: Database,
) -> None:
    harness = build_harness(database, make_settings(database.url))

    user_sender = MemorySender()
    await harness.processor.handle(
        inbound("/ai capabilities", message_id="user-capabilities"),
        user_sender,
    )
    user_text = user_sender.messages[0].text
    assert "当前权限：普通用户" in user_text
    assert "可修改运行时配置参数：0 项" in user_text
    assert "本人确定性自助接口：37 项，其中修改型 17 项" in user_text
    assert "memory.add" in user_text
    assert "planner.max_pending_messages" not in user_text

    admin_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai capabilities",
            message_id="admin-capabilities",
            user_id="9000",
        ),
        admin_sender,
    )
    admin_text = admin_sender.messages[0].text
    assert "当前权限：超级管理员" in admin_text
    assert "可修改运行时配置参数：219 项" in admin_text
    assert "管理员业务接口：44 项，其中修改型 33 项" in admin_text
    assert "planner.max_pending_messages" in admin_text
    assert "relationship.set_affection" in admin_text
    assert "受保护配置（12 项，不可修改）" in admin_text
    assert "NapCat/OneBot 通用全接口网关：1 项" in admin_text
    assert "call_onebot_api:any_public_action" in admin_text


@pytest.mark.asyncio
async def test_superuser_memory_search_and_index_diagnostics(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))
    fact = await MemoryFactService(MemoryFactRepository(database)).remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="10001",
            kind="fact",
            memory_key="plan:travel",
            category="plan",
            content="计划去杭州旅行",
            importance=4,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    search_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai memory search person 10001 杭州旅行",
            message_id="memory-search-admin",
            user_id="9000",
        ),
        search_sender,
    )
    assert f"{fact.id}. [lexical_match] 计划去杭州旅行" in search_sender.messages[0].text

    status_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai memory index status",
            message_id="memory-index-admin",
            user_id="9000",
        ),
        status_sender,
    )
    assert "缺失 0，孤儿 0" in status_sender.messages[0].text


@pytest.mark.asyncio
async def test_new_clears_only_current_conversation(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))
    first = ConversationIdentity.private("1001")
    second = ConversationIdentity.private("1002")
    await harness.conversations.add_message(first, role="user", content="one")
    await harness.conversations.add_message(second, role="user", content="two")
    sender = MemorySender()
    await harness.processor.handle(inbound("/ai new", message_id="new-1"), sender)
    assert await harness.conversations.count_messages(first) == 0
    assert await harness.conversations.count_messages(second) == 1


@pytest.mark.asyncio
async def test_superuser_on_off_and_permission(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))
    super_sender = MemorySender()
    await harness.processor.handle(
        inbound("/ai on", message_id="on", user_id="9000", group_id="2999"),
        super_sender,
    )
    assert (await harness.groups.get("2999")).enabled  # type: ignore[union-attr]
    await harness.processor.handle(
        inbound("/ai off", message_id="off", user_id="9000", group_id="2999"),
        super_sender,
    )
    assert not (await harness.groups.get("2999")).enabled  # type: ignore[union-attr]

    denied_sender = MemorySender()
    await harness.processor.handle(
        inbound("/ai on", message_id="denied", user_id="1001", group_id="2001"),
        denied_sender,
    )
    assert "权限不足" in denied_sender.messages[0].text


@pytest.mark.asyncio
async def test_superuser_can_persistently_toggle_private_users(database: Database) -> None:
    harness = build_harness(
        database,
        make_settings(database.url),
    )

    enabled_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai private 12345678 on",
            message_id="private-on",
            user_id="9000",
        ),
        enabled_sender,
    )
    assert enabled_sender.messages[0].text == "已开启指定 QQ 用户的私聊权限。"
    assert "12345678" not in enabled_sender.messages[0].text

    target_sender = MemorySender()
    allowed = await harness.processor.handle(
        inbound("hello", message_id="new-private-user", user_id="12345678"),
        target_sender,
    )
    assert allowed.reason == "chat"

    disabled_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai private 10010001 off",
            message_id="private-off",
            user_id="9000",
        ),
        disabled_sender,
    )
    denied = await harness.processor.handle(
        inbound("hello", message_id="env-user-disabled", user_id="10010001"),
        MemorySender(),
    )
    assert not denied.handled and denied.reason == "private_not_allowed"


@pytest.mark.asyncio
async def test_superuser_can_toggle_any_group_by_id(database: Database) -> None:
    harness = build_harness(
        database,
        make_settings(database.url, enabled_groups_csv="20010001"),
    )

    await harness.processor.handle(
        inbound(
            "/ai group 29999999 on",
            message_id="target-group-on",
            user_id="9000",
        ),
        MemorySender(),
    )
    enabled = await harness.processor.handle(
        inbound(
            "hello",
            message_id="new-group-message",
            group_id="29999999",
            mentions_bot=True,
        ),
        MemorySender(),
    )
    assert enabled.reason == "chat"

    await harness.processor.handle(
        inbound(
            "/ai group 20010001 off",
            message_id="target-group-off",
            user_id="9000",
        ),
        MemorySender(),
    )
    disabled = await harness.processor.handle(
        inbound(
            "hello",
            message_id="env-group-disabled",
            group_id="20010001",
            mentions_bot=True,
        ),
        MemorySender(),
    )
    assert not disabled.handled and disabled.reason == "group_disabled"


@pytest.mark.asyncio
async def test_access_commands_validate_permission_target_and_switch(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))

    non_admin_sender = MemorySender()
    await harness.processor.handle(
        inbound("/ai private 12345678 on", message_id="not-admin"),
        non_admin_sender,
    )
    assert "权限不足" in non_admin_sender.messages[0].text
    assert await harness.private_users.get("12345678") is None

    invalid_sender = MemorySender()
    await harness.processor.handle(
        inbound(
            "/ai group not-a-group maybe",
            message_id="invalid-group",
            user_id="9000",
        ),
        invalid_sender,
    )
    assert "格式错误" in invalid_sender.messages[0].text

    protected_harness = build_harness(
        database,
        make_settings(database.url, superusers_csv="90000"),
    )
    protected_sender = MemorySender()
    await protected_harness.processor.handle(
        inbound(
            "/ai private 90000 off",
            message_id="protected-superuser",
            user_id="90000",
        ),
        protected_sender,
    )
    assert protected_sender.messages[0].text == "不能关闭超级用户的私聊权限。"
    protected_setting = await protected_harness.private_users.get("90000")
    assert protected_setting is not None and protected_setting.enabled


@pytest.mark.asyncio
async def test_stop_cancels_only_current_task(database: Database) -> None:
    provider = FakeLLMProvider(delay_seconds=5)
    harness = build_harness(database, make_settings(database.url), provider)
    chat_sender = MemorySender()
    chat_task = asyncio.create_task(
        harness.processor.handle(inbound("slow", message_id="slow"), chat_sender)
    )
    identity = ConversationIdentity.private("1001")
    for _ in range(500):
        if harness.concurrency.is_processing(identity.key):
            break
        await asyncio.sleep(0.01)
    assert harness.concurrency.is_processing(identity.key)

    stop_sender = MemorySender()
    await harness.processor.handle(inbound("/ai stop", message_id="stop"), stop_sender)
    result = await chat_task
    assert result.reason == "cancelled"
    assert "已取消" in stop_sender.messages[0].text
    assert not harness.concurrency.is_processing(identity.key)


@pytest.mark.asyncio
async def test_empty_model_response_is_user_safe(database: Database) -> None:
    provider = FakeLLMProvider(lambda _request: "   ")
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()
    result = await harness.processor.handle(inbound("hello", message_id="empty"), sender)
    assert result.reason == "empty_llm_response"
    assert "空内容" in sender.messages[0].text


@pytest.mark.asyncio
async def test_planner_none_keeps_generic_tool_request_gateway(
    database: Database,
) -> None:
    provider = FakeLLMProvider(lambda _request: "我会按工具回执确认是否记住。")
    harness = build_harness(database, make_settings(database.url), provider)
    harness.processor._chat._tools._memory_mutations = object()  # type: ignore[assignment]
    plan = TurnPlan(
        decision=PlannerDecision.REPLY,
        intent="回应用户",
        delivery_mode=DeliveryMode.SINGLE,
        desired_messages=1,
        tool_selection=ToolSelection(mode=ToolMode.NONE, scopes=()),
        confidence=1.0,
        reason_code=PlannerReasonCode.DIRECT_REQUEST,
    )
    harness.processor._planner = PlannerService(
        provider=FakePlannerProvider(plan),
        observability=PlannerObservability(),
    )

    await harness.processor.handle(
        inbound("请记住我喜欢美式咖啡", message_id="memory-scope-fallback"),
        MemorySender(),
    )

    assert provider.requests
    tool_names = {tool.name for tool in provider.requests[-1].tools}
    assert "request_tools" in tool_names
    assert "memory_change" not in tool_names


@pytest.mark.asyncio
async def test_planner_preferred_emoji_can_complete_without_text(database: Database) -> None:
    provider = FakeLLMProvider(lambda _request: "   ")
    harness = build_harness(
        database,
        make_settings(database.url, emoji_enabled=True),
        provider,
    )
    plan = TurnPlan(
        decision=PlannerDecision.REPLY,
        intent="直接发送一个轻松的表情回应用户",
        delivery_mode=DeliveryMode.SINGLE,
        desired_messages=1,
        tool_mode=ToolMode.NONE,
        confidence=1.0,
        reason_code=PlannerReasonCode.DIRECT_REQUEST,
        emoji=EmojiReplyPlan(
            intent=EmojiIntent.EXPLICIT_REQUEST,
            mode=EmojiReplyMode.PREFERRED,
            placement=EmojiPlacement.AFTER_TEXT,
            goal="随便发个表情",
        ),
    )
    harness.processor._planner = PlannerService(
        provider=FakePlannerProvider(plan),
        observability=PlannerObservability(),
    )

    class PreparedEmojiEffect:
        async def prepare(self, *_args: object, **_kwargs: object) -> EmojiPreparationResult:
            message = OutboundMessage(
                media=(
                    OutboundMedia(
                        kind=AttachmentKind.IMAGE,
                        content=b"GIF89a",
                        mime_type="image/gif",
                        summary="测试表情",
                        emoji_id="emoji-test",
                        animated=True,
                    ),
                )
            )
            return EmojiPreparationResult(
                status=EmojiPreparationStatus.READY,
                message=message,
                emoji_id="emoji-test",
                reason_code="selected",
            )

        async def record_send_attempted(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def record_send_accepted(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def record_success(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def record_failure(self, *_args: object, **_kwargs: object) -> None:
            return None

    harness.processor._chat._emoji_effects = PreparedEmojiEffect()  # type: ignore[assignment]
    sender = MemorySender()

    result = await harness.processor.handle(
        inbound("发个表情", message_id="planner-emoji-no-text"),
        sender,
    )

    assert result.reason == "chat"
    assert result.sent_messages == 1
    assert provider.requests == []
    assert sender.messages[0].text == ""
    assert sender.messages[0].media[0].emoji_id == "emoji-test"


@pytest.mark.asyncio
async def test_planner_emoji_only_skips_agent_context_and_embedding(database: Database) -> None:
    provider = FakeLLMProvider(lambda _request: "must not be called")
    harness = build_harness(
        database,
        make_settings(database.url, emoji_enabled=True),
        provider,
    )
    plan = TurnPlan(
        decision=PlannerDecision.REPLY,
        intent="只发送表情",
        delivery_mode=DeliveryMode.SINGLE,
        desired_messages=1,
        tool_mode=ToolMode.NONE,
        confidence=1.0,
        reason_code=PlannerReasonCode.DIRECT_REQUEST,
        emoji=EmojiReplyPlan(
            intent=EmojiIntent.EXPLICIT_REQUEST,
            mode=EmojiReplyMode.EMOJI_ONLY,
            placement=EmojiPlacement.ONLY,
            goal="轻松回应",
        ),
    )
    harness.processor._planner = PlannerService(
        provider=FakePlannerProvider(plan),
        observability=PlannerObservability(),
    )

    class ContextMustNotRun:
        async def assemble(self, **_kwargs: object) -> object:
            raise AssertionError("emoji-only reply must not assemble Agent context")

    class PreparedEmojiEffect:
        async def prepare(self, *_args: object, **_kwargs: object) -> EmojiPreparationResult:
            message = OutboundMessage(
                media=(
                    OutboundMedia(
                        kind=AttachmentKind.IMAGE,
                        content=b"GIF89a",
                        mime_type="image/gif",
                        summary="测试表情",
                        emoji_id="emoji-only-test",
                        animated=True,
                    ),
                )
            )
            return EmojiPreparationResult(
                status=EmojiPreparationStatus.READY,
                message=message,
                emoji_id="emoji-only-test",
                reason_code="selected",
            )

        async def record_send_attempted(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def record_send_accepted(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def record_success(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def record_failure(self, *_args: object, **_kwargs: object) -> None:
            return None

    harness.processor._chat._context_assembler = ContextMustNotRun()  # type: ignore[assignment]
    harness.processor._chat._emoji_effects = PreparedEmojiEffect()  # type: ignore[assignment]
    sender = MemorySender()

    result = await harness.processor.handle(
        inbound("发个表情", message_id="planner-emoji-only-no-context"),
        sender,
    )

    assert result.reason == "chat"
    assert result.sent_messages == 1
    assert provider.requests == []
    assert sender.messages[0].media[0].emoji_id == "emoji-only-test"


@pytest.mark.asyncio
async def test_group_mention_without_text_starts_a_natural_chat_turn(database: Database) -> None:
    provider = FakeLLMProvider(lambda _request: "在呢，怎么啦？")
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()

    result = await harness.processor.handle(
        inbound(
            "",
            message_id="mention-only",
            group_id="2001",
            mentions_bot=True,
        ),
        sender,
    )

    assert result.reason == "chat"
    assert sender.messages[0].text == "在呢，怎么啦？"
    request = provider.requests[0]
    assert request.messages[-1].role == "user"
    assert request.messages[-1].content.endswith(MENTION_ONLY_CONTEXT)
    events = await EventLedgerRepository(database).list_recent(
        scope_type=ScopeType.GROUP,
        user_id="1001",
        group_id="2001",
        limit=10,
    )
    inbound_event = next(row for row in events if row.direction == "inbound")
    assert inbound_event.content == ""


@pytest.mark.asyncio
async def test_unsupported_message_degrades_without_calling_llm(database: Database) -> None:
    provider = FakeLLMProvider()
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("", message_id="image", unsupported=True), sender
    )
    assert result.reason == "vision_not_configured"
    assert "暂时没有识别成功" in sender.messages[0].text
    assert not provider.requests


@pytest.mark.asyncio
async def test_send_failure_is_not_retried_or_persisted_as_assistant(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))
    sender = MemorySender(fail=True)
    result = await harness.processor.handle(inbound("hello", message_id="send-fail"), sender)
    assert result.reason == "send_or_storage_failure"
    assert sender.calls == 1
    identity = ConversationIdentity.private("1001")
    history = await harness.conversations.list_context(
        identity, max_messages=10, max_characters=1000
    )
    assert [(item.role, item.content) for item in history] == [("user", "hello")]
