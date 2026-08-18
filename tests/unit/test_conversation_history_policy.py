"""Pure conversation history compaction policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from qq_ai_bot.conversation.history.policy import ChildSummaryView, HistoryCompactionPolicy
from qq_ai_bot.conversation.history.source import (
    ConversationSourceSnapshot,
    build_source_snapshot,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.context_assembler import ContextAssembler

_NOW = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)


def _event(
    event_id: int,
    *,
    content: str,
    visual_summary: str = "",
    occurred_at: datetime | None = None,
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
        sender_nickname="远野",
        private_peer_user_id="1001",
    )


def _snapshot(
    count: int,
    *,
    body: str,
    start: int = 1,
    state_id: int = 3,
) -> ConversationSourceSnapshot:
    events = tuple(_event(index, content=body) for index in range(start, start + count))
    return build_source_snapshot(
        state_id=state_id,
        reset_at=None,
        scope_type=ScopeType.PRIVATE,
        events=events,
    )


def _rendered(snapshot: ConversationSourceSnapshot):
    return tuple(
        (item.event_id, (item.event_id,), ChatMessage(role="user", content=item.content))
        for item in snapshot.events
    )


def test_hot_tail_protects_last_48_events_when_that_range_is_wider() -> None:
    snapshot = _snapshot(80, body="x" * 100)
    boundary = HistoryCompactionPolicy().hot_tail_boundary(snapshot)
    assert boundary.first_protected_event_id == snapshot.events[32].event_id
    assert boundary.protected_event_ids == tuple(item.event_id for item in snapshot.events[32:])
    assert len(boundary.protected_event_ids) == 48


def test_hot_tail_protects_last_3600_characters_when_that_range_is_wider() -> None:
    snapshot = _snapshot(80, body="y" * 50)
    boundary = HistoryCompactionPolicy().hot_tail_boundary(snapshot)
    assert boundary.first_protected_event_id == snapshot.events[8].event_id
    assert len(boundary.protected_event_ids) == 72


def test_hot_tail_uses_the_wider_of_event_and_character_protection() -> None:
    policy = HistoryCompactionPolicy()
    by_events = policy.hot_tail_boundary(_snapshot(80, body="z" * 100))
    by_chars = policy.hot_tail_boundary(_snapshot(80, body="z" * 50))
    assert by_events.first_protected_event_id is not None
    assert by_chars.first_protected_event_id is not None
    assert by_chars.first_protected_event_id < by_events.first_protected_event_id
    assert len(by_chars.protected_event_ids) > len(by_events.protected_event_ids)


def test_allow_raw_window_shift_requires_active_coverage() -> None:
    policy = HistoryCompactionPolicy()
    assert policy.allow_raw_window_shift(has_active_coverage=False) is False
    assert policy.allow_raw_window_shift(has_active_coverage=True) is True


def test_must_roll_prefix_matches_existing_history_window_clock() -> None:
    snapshot = _snapshot(20, body="n" * 40)
    rendered = _rendered(snapshot)
    policy = HistoryCompactionPolicy()
    dropped = policy.must_roll_prefix(
        rendered,
        anchor_event_id=snapshot.events[0].event_id,
        high_event_limit=8,
        high_character_limit=10_000,
        fallback_anchor_event_id=None,
    )
    selection = ContextAssembler._select_history_window(
        rendered,
        anchor_event_id=snapshot.events[0].event_id,
        high_event_limit=8,
        high_character_limit=10_000,
        low_watermark_ratio=policy.config.history_window_low_watermark_ratio,
        fallback_anchor_event_id=None,
    )
    kept = set(selection.event_ids)
    expected = tuple(
        event_id for _, event_ids, _ in rendered for event_id in event_ids if event_id not in kept
    )
    assert dropped == expected
    assert dropped


def test_must_roll_selects_extractive_candidate_from_dropped_prefix() -> None:
    snapshot = _snapshot(68, body="w" * 500)
    policy = HistoryCompactionPolicy()
    rendered = _rendered(snapshot)
    dropped = policy.must_roll_prefix(
        rendered,
        anchor_event_id=snapshot.events[0].event_id,
        high_event_limit=50,
        high_character_limit=100_000,
        fallback_anchor_event_id=None,
    )
    candidate = policy.select_l0_candidate(
        snapshot,
        coverage_end_event_id=0,
        dropped_prefix_ids=dropped,
        must_roll=True,
    )
    assert candidate is not None
    assert candidate.prefetch is False
    assert candidate.extractive_text
    assert candidate.start_event_id == snapshot.events[0].event_id
    assert candidate.end_event_id < snapshot.events[-48].event_id
    assert "#1" in candidate.extractive_text


def test_prefetch_starts_after_uncovered_range_reaches_32_events_or_8000_chars() -> None:
    snapshot = _snapshot(68, body="p" * 500)
    candidate = HistoryCompactionPolicy().select_l0_candidate(
        snapshot,
        coverage_end_event_id=0,
        dropped_prefix_ids=(),
        must_roll=False,
    )
    assert candidate is not None
    assert candidate.prefetch is True
    assert candidate.character_count >= 8000
    assert candidate.event_ids[0] == snapshot.events[0].event_id


def test_l0_candidate_respects_100_event_and_16000_character_job_caps() -> None:
    policy = HistoryCompactionPolicy()
    by_events = policy.select_l0_candidate(
        _snapshot(160, body="e" * 80),
        coverage_end_event_id=0,
        dropped_prefix_ids=(),
        must_roll=False,
    )
    by_chars = policy.select_l0_candidate(
        _snapshot(200, body="c" * 200),
        coverage_end_event_id=0,
        dropped_prefix_ids=(),
        must_roll=False,
    )
    assert by_events is not None
    assert by_chars is not None
    assert len(by_events.event_ids) == 100
    assert by_chars.character_count <= 16_000
    assert by_chars.character_count + 200 > 16_000


def test_identical_snapshots_yield_identical_candidates_and_fingerprints() -> None:
    first = _snapshot(68, body="s" * 500)
    second = _snapshot(68, body="s" * 500)
    policy = HistoryCompactionPolicy()
    left = policy.select_l0_candidate(
        first, coverage_end_event_id=0, dropped_prefix_ids=(), must_roll=False
    )
    right = policy.select_l0_candidate(
        second, coverage_end_event_id=0, dropped_prefix_ids=(), must_roll=False
    )
    assert left is not None
    assert right is not None
    assert left.fingerprint == right.fingerprint
    assert left.event_ids == right.event_ids
    assert left.extractive_text == right.extractive_text


def test_select_parent_candidate_requires_contiguous_same_level_children() -> None:
    policy = HistoryCompactionPolicy()
    children = tuple(
        ChildSummaryView(
            summary_id=index,
            level=0,
            start_event_id=index * 10 + 1,
            end_event_id=(index + 1) * 10,
            rendered_text="child",
        )
        for index in range(8)
    )
    selected = policy.select_parent_candidate(children)
    assert selected is not None
    assert selected.child_ids == tuple(range(8))
    assert selected.level == 1
    assert selected.start_event_id == 1
    assert selected.end_event_id == 80

    gapped = (
        *children[:3],
        ChildSummaryView(
            summary_id=99,
            level=0,
            start_event_id=50,
            end_event_id=60,
            rendered_text="x" * 2400,
        ),
        ChildSummaryView(
            summary_id=100,
            level=0,
            start_event_id=61,
            end_event_id=70,
            rendered_text="y" * 2400,
        ),
    )
    gapped_selected = policy.select_parent_candidate(gapped)
    assert gapped_selected is not None
    assert gapped_selected.child_ids == (99, 100)
