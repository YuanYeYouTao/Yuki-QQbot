"""Pure conversation history compaction policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from qq_ai_bot.conversation.history.policy import (
    ChildSummaryView,
    HistoryCompactionConfig,
    HistoryCompactionPolicy,
)
from qq_ai_bot.conversation.history.source import (
    ConversationSourceSnapshot,
    build_source_snapshot,
    source_event_prompt_characters,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.repository_records import EventRecord

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


def _hot_tail_starts(
    snapshot: ConversationSourceSnapshot,
    policy: HistoryCompactionPolicy,
) -> tuple[int, int]:
    events = snapshot.events
    event_start = events[max(0, len(events) - policy.config.raw_tail_events)].event_id
    used = 0
    char_index = len(events)
    for index in range(len(events) - 1, -1, -1):
        used += source_event_prompt_characters(events[index])
        char_index = index
        if used >= policy.config.raw_tail_characters:
            break
    return event_start, events[char_index].event_id


def test_hot_tail_keeps_the_tighter_of_event_and_rendered_character_caps() -> None:
    policy = HistoryCompactionPolicy()
    long_body = _snapshot(80, body="x" * 100)
    short_body = _snapshot(80, body="y" * 50)
    long_boundary = policy.hot_tail_boundary(long_body)
    short_boundary = policy.hot_tail_boundary(short_body)
    long_event, long_char = _hot_tail_starts(long_body, policy)
    short_event, short_char = _hot_tail_starts(short_body, policy)
    assert long_boundary.first_protected_event_id == max(long_event, long_char)
    assert short_boundary.first_protected_event_id == max(short_event, short_char)
    assert long_boundary.first_protected_event_id is not None
    assert short_boundary.first_protected_event_id is not None
    assert len(long_boundary.protected_event_ids) <= policy.config.raw_tail_events
    assert len(short_boundary.protected_event_ids) <= policy.config.raw_tail_events
    assert len(long_boundary.protected_event_ids) <= len(short_boundary.protected_event_ids)


def test_hot_tail_does_not_protect_an_entire_short_message_uncovered_range() -> None:
    snapshot = _snapshot(199, body="hi")
    policy = HistoryCompactionPolicy()
    boundary = policy.hot_tail_boundary(snapshot)
    first_protected = snapshot.events[-policy.config.raw_tail_events].event_id
    assert boundary.first_protected_event_id == first_protected
    assert len(boundary.protected_event_ids) == policy.config.raw_tail_events
    candidate = policy.select_l0_candidate(
        snapshot,
        coverage_end_event_id=0,
        dropped_prefix_ids=(),
        must_roll=False,
    )
    assert candidate is not None
    assert candidate.event_ids[0] == snapshot.events[0].event_id
    assert candidate.end_event_id < boundary.first_protected_event_id


def test_prefetch_can_slice_after_existing_coverage_on_short_messages() -> None:
    snapshot = _snapshot(120, body="ok", start=101)
    policy = HistoryCompactionPolicy()
    boundary = policy.hot_tail_boundary(snapshot)
    candidate = policy.select_l0_candidate(
        snapshot,
        coverage_end_event_id=100,
        dropped_prefix_ids=(),
        must_roll=False,
    )
    assert candidate is not None
    assert candidate.event_ids[0] == snapshot.events[0].event_id
    assert boundary.first_protected_event_id is not None
    assert candidate.end_event_id < boundary.first_protected_event_id


def test_allow_raw_window_shift_requires_active_coverage() -> None:
    policy = HistoryCompactionPolicy()
    assert policy.allow_raw_window_shift(has_active_coverage=False) is False
    assert policy.allow_raw_window_shift(has_active_coverage=True) is True


def test_must_roll_prefix_returns_left_slice_excluding_hot_tail() -> None:
    snapshot = _snapshot(80, body="n" * 40)
    rendered = _rendered(snapshot)
    policy = HistoryCompactionPolicy()
    dropped = policy.must_roll_prefix(
        rendered,
        snapshot=snapshot,
        anchor_event_id=snapshot.events[0].event_id,
        high_event_limit=8,
        high_character_limit=10_000,
        fallback_anchor_event_id=None,
    )
    boundary = policy.hot_tail_boundary(snapshot)
    assert dropped
    assert dropped[0] == snapshot.events[0].event_id
    assert dropped == tuple(range(dropped[0], dropped[-1] + 1))
    assert boundary.first_protected_event_id is not None
    assert dropped[-1] < boundary.first_protected_event_id
    assert len(dropped) <= policy.config.l0_max_events


def test_must_roll_prefix_empty_when_uncovered_fits_budget() -> None:
    snapshot = _snapshot(5, body="hi")
    dropped = HistoryCompactionPolicy().must_roll_prefix(
        _rendered(snapshot),
        snapshot=snapshot,
        anchor_event_id=snapshot.events[0].event_id,
        high_event_limit=50,
        high_character_limit=10_000,
        fallback_anchor_event_id=None,
    )
    assert dropped == ()


def test_must_roll_selects_extractive_candidate_from_dropped_prefix() -> None:
    snapshot = _snapshot(68, body="w" * 500)
    policy = HistoryCompactionPolicy()
    rendered = _rendered(snapshot)
    dropped = policy.must_roll_prefix(
        rendered,
        snapshot=snapshot,
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
    boundary = policy.hot_tail_boundary(snapshot)
    assert candidate is not None
    assert candidate.prefetch is False
    assert candidate.extractive_text
    assert candidate.start_event_id == snapshot.events[0].event_id
    assert boundary.first_protected_event_id is not None
    assert candidate.end_event_id < boundary.first_protected_event_id
    assert f"#{candidate.end_event_id}" in candidate.extractive_text


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


def test_select_parent_candidate_rolls_lowest_level_first() -> None:
    policy = HistoryCompactionPolicy()
    children = (
        ChildSummaryView(
            summary_id=1,
            level=1,
            start_event_id=1,
            end_event_id=20,
            rendered_text="parent-range",
        ),
        *(
            ChildSummaryView(
                summary_id=10 + index,
                level=0,
                start_event_id=21 + index * 10,
                end_event_id=30 + index * 10,
                rendered_text="child",
            )
            for index in range(8)
        ),
    )
    selected = policy.select_parent_candidate(children)
    assert selected is not None
    assert selected.level == 1
    assert selected.child_ids == tuple(range(10, 18))


def test_select_parent_candidate_respects_max_level() -> None:
    policy = HistoryCompactionPolicy(HistoryCompactionConfig(max_level=1))
    children = tuple(
        ChildSummaryView(
            summary_id=index,
            level=1,
            start_event_id=index * 10 + 1,
            end_event_id=(index + 1) * 10,
            rendered_text="child",
        )
        for index in range(8)
    )
    assert policy.select_parent_candidate(children) is None
