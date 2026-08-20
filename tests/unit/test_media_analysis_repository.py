"""Tests for the expiring visual observation cache."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import delete, func, select

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, MediaAnalysisModel
from qq_ai_bot.persistence.repositories import EventLedgerRepository, MediaAnalysisRepository


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


async def _event(database: Database, message_id: str) -> int:
    record, inserted = await EventLedgerRepository(database).append(
        bot_user_id="9000",
        platform_message_id=message_id,
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id="1001",
        sender_user_id="1001",
        direction="inbound",
        content="[图片]",
    )
    assert inserted is True
    return record.id


@pytest.mark.asyncio
async def test_save_and_find_exact_unexpired_cache(database: Database) -> None:
    repository = MediaAnalysisRepository(database)
    now = datetime.now(UTC)
    event_id = await _event(database, "image-1")

    saved = await repository.save(
        source_event_id=event_id,
        segment_index=2,
        content_hash=_hash("image-content"),
        analysis_mode="general",
        question_hash=None,
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v1",
        observation_json={"overall_description": "一只猫", "items": []},
        created_at=now,
        expires_at=now + timedelta(days=7),
    )

    cached = await repository.find_cached(
        content_hash=_hash("image-content"),
        analysis_mode="general",
        question_hash=None,
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v1",
        now=now,
    )
    assert cached == saved
    assert cached is not None
    assert cached.question_hash == ""
    assert cached.source_event_id == event_id
    assert "一只猫" in cached.observation_json

    assert (
        await repository.find_cached(
            content_hash=_hash("image-content"),
            analysis_mode="general",
            question_hash=None,
            provider="other-provider",
            model="qwen3.7-plus",
            prompt_version="vision-v1",
            now=now,
        )
        is None
    )


@pytest.mark.asyncio
async def test_question_hashes_are_isolated_and_expiry_is_enforced(database: Database) -> None:
    repository = MediaAnalysisRepository(database)
    now = datetime.now(UTC)
    content_hash = _hash("same-image")
    common = {
        "source_event_id": None,
        "segment_index": 0,
        "content_hash": content_hash,
        "analysis_mode": "question",
        "provider": "qwen",
        "model": "qwen3.7-plus",
        "prompt_version": "vision-v1",
        "created_at": now,
    }
    first_question = _hash("图里有什么")
    second_question = _hash("图里的字是什么")
    await repository.save(
        **common,
        question_hash=first_question,
        observation_json={"overall_description": "猫"},
        expires_at=now + timedelta(days=1),
    )
    await repository.save(
        **common,
        question_hash=second_question,
        observation_json={"overall_description": "文字"},
        expires_at=now - timedelta(seconds=1),
    )

    first = await repository.find_cached(
        content_hash=content_hash,
        analysis_mode="question",
        question_hash=first_question,
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v1",
        now=now,
    )
    expired = await repository.find_cached(
        content_hash=content_hash,
        analysis_mode="question",
        question_hash=second_question,
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v1",
        now=now,
    )
    assert first is not None
    assert first.question_hash == first_question
    assert expired is None

    assert await repository.cleanup_expired(now=now) == 1


@pytest.mark.asyncio
async def test_event_lookup_preserves_owner_and_event_delete_cascades(
    database: Database,
) -> None:
    repository = MediaAnalysisRepository(database)
    now = datetime.now(UTC)
    first_event_id = await _event(database, "owner-event")
    second_event_id = await _event(database, "other-event")
    content_hash = _hash("owned-image")
    saved = await repository.save(
        source_event_id=first_event_id,
        segment_index=3,
        content_hash=content_hash,
        analysis_mode="ocr",
        question_hash="",
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v1",
        observation_json={"items": [{"ocr_text": "你好"}]},
        created_at=now,
        expires_at=now + timedelta(days=7),
    )

    by_event = await repository.find_for_event(
        first_event_id,
        3,
        analysis_mode="ocr",
        question_hash="",
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v1",
        now=now,
    )
    assert by_event == saved
    assert (
        await repository.associate_event(
            saved.id,
            source_event_id=second_event_id,
            segment_index=1,
        )
        is False
    )

    refreshed = await repository.save(
        source_event_id=second_event_id,
        segment_index=1,
        content_hash=content_hash,
        analysis_mode="ocr",
        question_hash="",
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v1",
        observation_json={"items": [{"ocr_text": "刷新"}]},
        created_at=now + timedelta(seconds=1),
        expires_at=now + timedelta(days=8),
    )
    assert refreshed.id == saved.id
    assert refreshed.source_event_id == first_event_id
    assert refreshed.segment_index == 3

    async with database.sessions() as session, session.begin():
        await session.execute(delete(ChatEventModel).where(ChatEventModel.id == first_event_id))
    async with database.sessions() as session:
        count = await session.scalar(select(func.count(MediaAnalysisModel.id)))
    assert count == 0


@pytest.mark.asyncio
async def test_unowned_cache_can_be_associated_once(database: Database) -> None:
    repository = MediaAnalysisRepository(database)
    now = datetime.now(UTC)
    event_id = await _event(database, "late-owner")
    saved = await repository.save(
        source_event_id=None,
        segment_index=0,
        content_hash=_hash("late-image"),
        analysis_mode="meme",
        question_hash="",
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v1",
        observation_json={"overall_description": "表情包"},
        expires_at=now + timedelta(days=7),
    )

    assert (
        await repository.associate_event(
            saved.id,
            source_event_id=event_id,
            segment_index=4,
        )
        is True
    )
    associated = await repository.find_for_event(
        event_id,
        4,
        analysis_mode="meme",
        question_hash="",
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v1",
        now=now,
    )
    assert associated is not None
    assert associated.id == saved.id


@pytest.mark.asyncio
async def test_repository_rejects_embedded_image_payloads(database: Database) -> None:
    repository = MediaAnalysisRepository(database)
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match="must not contain image or Base64"):
        await repository.save(
            source_event_id=None,
            segment_index=0,
            content_hash=_hash("unsafe"),
            analysis_mode="general",
            question_hash="",
            provider="qwen",
            model="qwen3.7-plus",
            prompt_version="vision-v1",
            observation_json={"image_url": "data:image/png;base64,AAAA"},
            expires_at=now + timedelta(days=7),
        )


@pytest.mark.asyncio
async def test_chat_event_visual_summary_is_persisted_without_changing_raw_text(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    event_id = await _event(database, "visual-summary")

    assert await ledger.set_visual_summary(event_id, '{"overall_description":"一只猫"}')
    recent = await ledger.list_scope_recent(
        ConversationScope.private("9000", "1001"),
        limit=10,
    )
    event = next(row for row in recent if row.id == event_id)
    assert event.content == "[图片]"
    assert event.visual_summary == '{"overall_description":"一只猫"}'

    with pytest.raises(ValueError, match="must not contain image or Base64"):
        await ledger.set_visual_summary(event_id, "data:image/png;base64,AAAA")
