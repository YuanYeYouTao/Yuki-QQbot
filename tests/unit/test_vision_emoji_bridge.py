"""Turn Vision observations must satisfy the emoji classifier cache."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.admin.models import VisionRuntimeConfig
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.emoji.classifier import EmojiClassifier
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    EmojiDescriptionRepository,
    MediaAnalysisRepository,
)
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.services.vision_rate_limit import VisionRateLimiter
from qq_ai_bot.services.vision_service import VisionService
from qq_ai_bot.vision.fake import FakeVisionProvider


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 6), (255, 0, 0)).save(output, format="PNG")
    return output.getvalue()


def _inline_png(content: bytes) -> str:
    return "base64://" + base64.b64encode(content).decode("ascii")


def _vision_runtime() -> VisionRuntimeConfig:
    return VisionRuntimeConfig(
        max_images_per_turn=3,
        max_frames_per_turn=8,
        gif_max_frames=4,
        thinking_enabled=True,
        thinking_budget=3072,
        low_confidence_retry_threshold=0.65,
        per_user_requests_per_minute=8,
        per_group_requests_per_minute=12,
        analysis_retention_days=7,
    )


def _image_message(content: bytes, *, text: str, mentions_bot: bool = False) -> InboundMessage:
    return InboundMessage(
        message_id="vision-bridge",
        event_type="message:group",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="测试用户"),
        text=text,
        bot_user_id="8000",
        group_id="2001",
        mentions_bot=mentions_bot,
        attachments=(
            MessageAttachment(
                kind=AttachmentKind.IMAGE,
                label="image",
                segment_index=0,
                file=_inline_png(content),
            ),
        ),
    )


async def test_turn_vision_question_alias_satisfies_emoji_classifier(
    database: Database, tmp_path: Path
) -> None:
    content = _png_bytes()
    vision_provider = FakeVisionProvider()
    analyses = MediaAnalysisRepository(database)
    service = VisionService(
        provider=vision_provider,
        resolver=MediaResolver(),
        preprocessor=ImagePreprocessor(),
        analyses=analyses,
        rate_limiter=VisionRateLimiter(),
        emoji_descriptions=EmojiDescriptionRepository(database),
        emoji_analysis_version="emoji-v1",
    )
    await service.analyze(
        _image_message(content, text="看看这张图"),
        question="看看这张图",
        runtime=_vision_runtime(),
        gateway=None,
        source_event_id=None,
        conversation_key="bot:8000:group:2001",
    )
    assert len(vision_provider.requests) == 1
    assert vision_provider.request_options[0].analysis_mode == "question"

    storage = EmojiStorage(tmp_path / "emoji")
    media = storage.inspect(content, near_duplicate_enabled=False)
    storage.persist(content, media)
    asset, _ = await EmojiRepository(database).record_candidate(
        media,
        source_event_id=None,
        user_id=None,
        group_id=None,
        source_sub_type="",
        source_emoji_id="",
        source_package_id="",
    )
    classifier_provider = FakeVisionProvider()
    classifier = EmojiClassifier(
        provider=classifier_provider,
        preprocessor=ImagePreprocessor(),
        storage=storage,
        analyses=analyses,
    )
    result = await classifier.classify(
        asset,
        analysis_version="emoji-v1",
        max_frames=1,
        thinking_enabled=False,
        thinking_budget=0,
    )

    assert result.description
    assert classifier_provider.requests == []


@pytest.mark.asyncio
async def test_observe_image_does_not_run_turn_vision(database: Database) -> None:
    vision = FakeVisionProvider()
    settings = make_settings(
        database.url,
        vision_enabled=True,
        vision_provider="fake",
        vision_base_url="https://vision.invalid/v1",
        vision_api_key="test-key",
        vision_model="fake-vision",
    )
    harness = build_harness(database, settings, vision_provider=vision)
    result = await harness.processor.handle(
        _image_message(_png_bytes(), text="群里随手一图", mentions_bot=False),
        MemorySender(),
    )
    assert result.reason == "group_observed"
    assert vision.requests == []
