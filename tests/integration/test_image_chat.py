"""End-to-end explicit image chat using isolated fake providers."""

from __future__ import annotations

import asyncio
import base64
import io

import pytest
from PIL import Image
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.conversation.features import AdmissionFeatureBuilder
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    ChatRequest,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
from qq_ai_bot.vision.base import VisionError
from qq_ai_bot.vision.fake import FakeVisionProvider
from qq_ai_bot.vision.models import (
    VisualCharacterCandidate,
    VisualItemObservation,
    VisualObservation,
)


def _inline_png(color: tuple[int, int, int] = (20, 30, 40)) -> str:
    output = io.BytesIO()
    Image.new("RGB", (10, 8), color).save(output, format="PNG")
    return "base64://" + base64.b64encode(output.getvalue()).decode("ascii")


def _inline_gif() -> str:
    output = io.BytesIO()
    frames = [Image.new("RGB", (10, 8), (20 * index, 40, 90)) for index in range(6)]
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=40,
        loop=0,
    )
    return "base64://" + base64.b64encode(output.getvalue()).decode("ascii")


def _attachment(file: str, index: int = 0) -> MessageAttachment:
    return MessageAttachment(
        kind=AttachmentKind.IMAGE,
        label="image",
        segment_index=index,
        file=file,
    )


def _private(
    message_id: str,
    *,
    text: str = "",
    attachments: tuple[MessageAttachment, ...],
    user_id: str = "1001",
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:private:friend",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname="测试用户"),
        text=text,
        bot_user_id="9999",
        attachments=attachments,
    )


def _group(
    message_id: str,
    *,
    mentions_bot: bool,
    text: str = "看看",
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="测试用户"),
        text=text,
        bot_user_id="9999",
        group_id="2001",
        mentions_bot=mentions_bot,
        attachments=(_attachment(_inline_png()),),
    )


def _vision_settings(database_url: str, **updates: object):
    values: dict[str, object] = {
        "vision_enabled": True,
        "vision_provider": "fake",
        "vision_base_url": "https://vision.invalid/v1",
        "vision_api_key": "test-key",
        "vision_model": "fake-vision",
    }
    values.update(updates)
    return make_settings(database_url, **values)


@pytest.mark.asyncio
async def test_private_pure_image_flows_vision_text_into_deepseek(database) -> None:
    def reply(request: ChatRequest) -> str:
        visual = next(
            message.content or ""
            for message in request.messages
            if '"id":"modality.visual"' in (message.content or "")
        )
        assert "测试视觉观察" in visual
        assert "data:image" not in visual
        assert "base64://" not in visual
        return "看起来是一张测试图片。"

    llm = FakeLLMProvider(reply)
    vision = FakeVisionProvider()
    settings = _vision_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, llm, vision_provider=vision)
    sender = MemorySender()

    result = await harness.processor.handle(
        _private("image-1", attachments=(_attachment(_inline_png()),)),
        sender,
    )

    assert result.reason == "chat"
    assert [message.text for message in sender.messages] == ["看起来是一张测试图片。"]
    assert len(vision.requests) == 1
    assert vision.requests[0][1].startswith("请描述图片主要内容")
    current = llm.requests[-1].messages[-1].content or ""
    assert "[测试用户|QQ:1001]\n" in current
    assert current.endswith(
        "[当前消息仅包含图片；后端视觉识别已成功，请根据本轮视觉观察直接回应图片内容]"
    )


@pytest.mark.asyncio
async def test_image_question_and_three_images_use_one_visual_request(database) -> None:
    llm = FakeLLMProvider(lambda _request: "三张图片已经一起看过了。")
    vision = FakeVisionProvider()
    settings = _vision_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, llm, vision_provider=vision)

    result = await harness.processor.handle(
        _private(
            "image-2",
            text="它们有什么关系？",
            attachments=tuple(
                _attachment(_inline_png((index * 30, 10, 10)), index) for index in range(1, 4)
            ),
        ),
        MemorySender(),
    )

    assert result.reason == "chat"
    assert len(vision.requests) == 1
    assert len(vision.requests[0][0]) == 3
    assert vision.requests[0][1] == "它们有什么关系？"


@pytest.mark.asyncio
async def test_animated_meme_is_sampled_and_explained_end_to_end(database) -> None:
    def observe(_inputs, _question):
        return VisualObservation(
            items=(
                VisualItemObservation(
                    index=1,
                    description="角色连续点头",
                    expression="开心赞同",
                    meme_intent="常用于表示同意，具体语境可能不同",
                    recognized_character="奶龙",
                    franchise="奶龙",
                    character_candidates=(
                        VisualCharacterCandidate(
                            name="奶龙",
                            work="奶龙",
                            evidence="黄色小恐龙",
                            confidence=0.93,
                        ),
                    ),
                    confidence=0.82,
                ),
            ),
            overall_description="一个表达赞同的动态表情",
            provider="fake",
            model="fake-vision",
        )

    def answer(request: ChatRequest) -> str:
        payload = "\n".join(message.content or "" for message in request.messages)
        assert "开心赞同" in payload
        assert "常用于表示同意" in payload
        assert '"recognized_character":"奶龙"' in payload
        assert '"franchise":"奶龙"' in payload
        return "看起来是在开心地点头，通常可以表示赞同。"

    llm = FakeLLMProvider(answer)
    vision = FakeVisionProvider(observe)
    settings = _vision_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, llm, vision_provider=vision)
    attachment = MessageAttachment(
        kind=AttachmentKind.IMAGE,
        label="image",
        segment_index=0,
        file=_inline_gif(),
        summary="[动画表情]",
        emoji_id="meme-1",
    )
    sender = MemorySender()

    result = await harness.processor.handle(
        _private("animated-meme", text="这个表情是什么意思？", attachments=(attachment,)),
        sender,
    )

    assert result.reason == "chat"
    assert vision.requests[0][0][0].animated
    assert len(vision.requests[0][0][0].frames) == 6
    assert vision.request_options[0].analysis_mode == "meme"
    assert not vision.request_options[0].thinking_enabled
    assert sender.messages[-1].text == "看起来是在开心地点头，通常可以表示赞同。"


@pytest.mark.asyncio
async def test_group_image_only_analyzed_when_existing_policy_triggers(database) -> None:
    llm = FakeLLMProvider(lambda _request: "群图片回复")
    vision = FakeVisionProvider()
    settings = _vision_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, llm, vision_provider=vision)

    ignored = await harness.processor.handle(
        _group("group-image-1", mentions_bot=False), MemorySender()
    )
    handled = await harness.processor.handle(
        _group("group-image-2", mentions_bot=True), MemorySender()
    )

    assert ignored.reason == "group_observed"
    assert handled.reason == "chat"
    assert len(vision.requests) == 1


@pytest.mark.asyncio
async def test_autonomous_group_batch_never_analyzes_observed_images(database) -> None:
    def reply(_request: ChatRequest) -> str:
        return "自主文字回复"

    llm = FakeLLMProvider(reply)
    vision = FakeVisionProvider()
    settings = _vision_settings(
        "sqlite+aiosqlite:///:memory:",
        conversation_autonomous_debounce_seconds=0.01,
        daily_chat_message_delay_min_seconds=0,
        daily_chat_message_delay_max_seconds=0,
    )
    harness = build_harness(database, settings, llm, vision_provider=vision)
    autonomous = AutonomousGroupService(
        chat=harness.processor._chat,
        admission_features=AdmissionFeatureBuilder(
            ledger=harness.processor._ledger,
            relationships=harness.processor._relationships,
        ),
        runtime_config=harness.processor._runtime_config,
    )
    harness.processor._autonomous = autonomous
    sender = MemorySender()

    result = await harness.processor.handle(
        _group("autonomous-image", mentions_bot=False, text="大家觉得这张图怎么样？"),
        sender,
    )
    for _ in range(20):
        if sender.messages:
            break
        await asyncio.sleep(0.05)

    assert result.reason == "group_observed"
    assert sender.messages[-1].text == "自主文字回复"
    assert vision.requests == []
    await autonomous.close()


@pytest.mark.asyncio
async def test_visual_failure_falls_back_to_text_but_pure_image_is_deterministic(database) -> None:
    def fail(_inputs, _question):
        raise VisionError("provider_unavailable", "视觉服务暂不可用")

    llm = FakeLLMProvider(lambda _request: "仍然可以回答你的文字。")
    vision = FakeVisionProvider(fail)
    settings = _vision_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, llm, vision_provider=vision)

    text_sender = MemorySender()
    text_result = await harness.processor.handle(
        _private(
            "image-fail-text",
            text="先回答我后半句：你好",
            attachments=(_attachment(_inline_png()),),
        ),
        text_sender,
    )
    pure_sender = MemorySender()
    pure_result = await harness.processor.handle(
        _private("image-fail-pure", attachments=(_attachment(_inline_png((1, 2, 3))),)),
        pure_sender,
    )

    assert text_result.reason == "chat"
    assert text_sender.messages[-1].text == "仍然可以回答你的文字。"
    assert any(
        '"id":"modality.visual_failure"' in (message.content or "")
        and '"visual_status":"unavailable"' in (message.content or "")
        for message in llm.requests[-1].messages
    )
    assert pure_result.reason == "vision_provider_unavailable"
    assert pure_sender.messages[-1].text == ("图片已取得，但视觉模型暂时不可用，请稍后再试。")


@pytest.mark.asyncio
async def test_unexpected_vision_error_cannot_escape_message_processing(database) -> None:
    def fail_unexpectedly(_inputs, _question):
        raise RuntimeError("signed-url-must-not-be-logged")

    llm = FakeLLMProvider(lambda _request: "图片没读到，但文字仍能回答。")
    vision = FakeVisionProvider(fail_unexpectedly)
    settings = _vision_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, llm, vision_provider=vision)
    sender = MemorySender()

    result = await harness.processor.handle(
        _private(
            "image-unexpected-failure",
            text="只回答这句文字",
            attachments=(_attachment(_inline_png()),),
        ),
        sender,
    )

    assert result.reason == "chat"
    assert sender.messages[-1].text == "图片没读到，但文字仍能回答。"


@pytest.mark.asyncio
async def test_visual_observation_never_enters_memory_or_relationship_jobs(database) -> None:
    llm = FakeLLMProvider(lambda _request: "正常回答")
    vision = FakeVisionProvider()
    settings = _vision_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, llm, vision_provider=vision)

    await harness.processor.handle(
        _private(
            "image-worker-boundary",
            text="这是真实用户文字",
            attachments=(_attachment(_inline_png()),),
        ),
        MemorySender(),
    )

    memory_jobs = await MemoryJobRepository(database).claim(limit=10)
    relationship_jobs = await harness.relationship_jobs.claim(limit=10)
    assert [job.event.content for job in memory_jobs] == ["这是真实用户文字"]
    assert [job.trigger_event.content for job in relationship_jobs] == ["这是真实用户文字"]
    worker_payload = "\n".join(
        [job.event.content for job in memory_jobs]
        + [event.content for job in relationship_jobs for event in job.recent_events]
    )
    assert "测试视觉观察" not in worker_payload
    assert "description" not in worker_payload


@pytest.mark.asyncio
async def test_prior_image_summary_is_restored_in_the_next_chat_turn(database) -> None:
    requests: list[ChatRequest] = []

    def reply(request: ChatRequest) -> str:
        requests.append(request)
        if len(requests) == 1:
            return "我已经看过这张图片。"
        payload = "\n".join(message.content or "" for message in request.messages)
        assert "历史图片识别摘要" in payload
        assert "测试视觉观察" in payload
        assert "data:image" not in payload
        assert "base64://" not in payload
        return "还记得，刚才的识图摘要仍在当前上下文里。"

    llm = FakeLLMProvider(reply)
    vision = FakeVisionProvider()
    settings = _vision_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings, llm, vision_provider=vision)

    await harness.processor.handle(
        _private("remember-image", attachments=(_attachment(_inline_png()),)),
        MemorySender(),
    )
    sender = MemorySender()
    result = await harness.processor.handle(
        _private("ask-image", text="刚才图片里是什么？", attachments=()),
        sender,
    )

    assert result.reason == "chat"
    assert sender.messages[-1].text == "还记得，刚才的识图摘要仍在当前上下文里。"
    assert len(vision.requests) == 1
