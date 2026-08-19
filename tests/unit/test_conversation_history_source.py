"""Deterministic conversation history source snapshots."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

from qq_ai_bot.conversation.history.source import (
    ConversationSourceSnapshot,
    build_source_snapshot,
    extractive_compact,
    source_event_prompt_characters,
    source_fingerprint,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.persistence.repository_records import EventRecord

_NOW = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)


def _event(
    event_id: int,
    *,
    content: str = "hello",
    visual_summary: str = "",
    occurred_at: datetime | None = None,
    event_kind: str = "message",
    reply_to_message_id: str | None = None,
    sender_nickname: str = "远野",
    sender_group_card: str = "",
) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="bot-1",
        platform_message_id=f"m-{event_id}",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content=content,
        visual_summary=visual_summary,
        segments=(),
        occurred_at=occurred_at or (_NOW + timedelta(seconds=event_id)),
        sender_nickname=sender_nickname,
        sender_group_card=sender_group_card,
        private_peer_user_id="1001",
        reply_to_message_id=reply_to_message_id,
        event_kind=event_kind,
    )


def _snapshot(
    events: tuple[EventRecord, ...],
    *,
    state_id: int = 7,
    reset_at: datetime | None = None,
) -> ConversationSourceSnapshot:
    return build_source_snapshot(
        state_id=state_id,
        reset_at=reset_at,
        scope_type=ScopeType.PRIVATE,
        events=events,
    )


def test_source_fingerprint_is_stable_for_identical_snapshots() -> None:
    events = (_event(3, content="alpha"), _event(4, content="beta"))
    first = source_fingerprint(_snapshot(events))
    second = source_fingerprint(_snapshot(events))
    assert first == second
    assert len(first) == 64


def test_source_fingerprint_does_not_include_summarizer_version() -> None:
    source = inspect.getsource(source_fingerprint)
    assert "summarizer_version" not in source
    events = (_event(1, content="same"),)
    assert source_fingerprint(_snapshot(events, state_id=1)) == source_fingerprint(
        _snapshot(events, state_id=1)
    )


def test_source_fingerprint_includes_content_hash_and_event_ids() -> None:
    original = source_fingerprint(_snapshot((_event(1, content="alpha"),)))
    edited = source_fingerprint(_snapshot((_event(1, content="beta"),)))
    swapped = source_fingerprint(_snapshot((_event(2, content="alpha"),)))
    assert original != edited
    assert original != swapped


def test_source_fingerprint_changes_across_conversations() -> None:
    events = (_event(1, content="shared"),)
    private = source_fingerprint(_snapshot(events, state_id=11))
    other = source_fingerprint(_snapshot(events, state_id=12))
    assert private != other


def test_build_source_snapshot_does_not_cross_reset() -> None:
    reset_at = _NOW + timedelta(seconds=5)
    snapshot = _snapshot(
        (
            _event(1, content="before", occurred_at=_NOW + timedelta(seconds=1)),
            _event(2, content="after", occurred_at=_NOW + timedelta(seconds=9)),
        ),
        reset_at=reset_at,
    )
    assert snapshot.event_ids == (2,)
    assert snapshot.reset_epoch == reset_at.isoformat()


def test_build_source_snapshot_orders_tied_timestamps_by_event_id() -> None:
    tied = _NOW + timedelta(seconds=3)
    snapshot = _snapshot(
        (
            _event(9, content="later-id", occurred_at=tied),
            _event(2, content="earlier-id", occurred_at=tied),
        )
    )
    assert snapshot.event_ids == (2, 9)


def test_project_event_marks_external_untrusted_and_keeps_reply() -> None:
    snapshot = _snapshot(
        (
            _event(
                4,
                content="plugin ping",
                event_kind="external_event",
                reply_to_message_id="m-1",
                sender_group_card="群名片",
            ),
        )
    )
    item = snapshot.events[0]
    assert item.external_untrusted is True
    assert item.reply_to_message_id == "m-1"
    assert item.sender_label == "群名片"


def test_extractive_compact_drops_oldest_lines_first() -> None:
    snapshot = _snapshot(
        (
            _event(1, content="oldest-block"),
            _event(2, content="middle-block"),
            _event(3, content="newest-block"),
        )
    )
    rendered = extractive_compact(snapshot, max_characters=40)
    assert "oldest-block" not in rendered
    assert "newest-block" in rendered
    assert len(rendered) <= 40


def test_extractive_compact_keeps_direction_sender_and_visual_summary() -> None:
    snapshot = _snapshot((_event(8, content="photo", visual_summary="一只猫坐在窗边"),))
    rendered = extractive_compact(snapshot, max_characters=200)
    assert "#8" in rendered
    assert "inbound" in rendered
    assert "远野" in rendered
    assert "一只猫坐在窗边" in rendered


def test_source_snapshot_is_immutable() -> None:
    snapshot = _snapshot((_event(1),))
    assert snapshot.model_config.get("frozen") is True
    try:
        snapshot.state_id = 99  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("source snapshot must reject mutation")


def test_source_event_prompt_characters_count_envelope_not_bare_content() -> None:
    snapshot = _snapshot((_event(12, content="hi", visual_summary="一张自拍"),))
    item = snapshot.events[0]
    counted = source_event_prompt_characters(item)
    assert counted > len(item.content)
    assert counted == len(
        f"[发送者:{item.sender_label}|QQ:{item.sender_user_id}|消息:{item.event_id}] "
    ) + len(f"{item.content}\n{item.visual_summary}")
