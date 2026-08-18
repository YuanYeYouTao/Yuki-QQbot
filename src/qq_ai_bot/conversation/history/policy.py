"""Pure compaction policy for conversation history rollup."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from qq_ai_bot.conversation.history.source import (
    ConversationSourceSnapshot,
    SourceEventProjection,
    extractive_compact,
    source_fingerprint,
)
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.services.context_assembler import ContextAssembler

RenderedHistory = tuple[tuple[int, tuple[int, ...], ChatMessage], ...]


class HistoryCompactionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_tail_events: int = Field(default=48, ge=1)
    raw_tail_characters: int = Field(default=3600, ge=1)
    l0_min_events: int = Field(default=32, ge=1)
    l0_min_characters: int = Field(default=8000, ge=1)
    l0_max_events: int = Field(default=100, ge=1)
    l0_max_characters: int = Field(default=16_000, ge=1)
    extractive_max_characters: int = Field(default=1200, ge=1)
    fan_in: int = Field(default=8, ge=2)
    fan_in_characters: int = Field(default=4800, ge=1)
    history_window_low_watermark_ratio: float = Field(default=0.67, gt=0, lt=1)


class HotTailBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    first_protected_event_id: int | None
    protected_event_ids: tuple[int, ...]


class RawRangeCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_ids: tuple[int, ...]
    start_event_id: int
    end_event_id: int
    character_count: int
    fingerprint: str
    extractive_text: str
    prefetch: bool


class SummaryRollupCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    child_ids: tuple[int, ...]
    start_event_id: int
    end_event_id: int
    level: int


class ChildSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary_id: int
    level: int
    start_event_id: int
    end_event_id: int
    rendered_text: str


class HistoryCompactionPolicy:
    """Decide what to compress. No model calls."""

    def __init__(self, config: HistoryCompactionConfig | None = None) -> None:
        self.config = config or HistoryCompactionConfig()

    def hot_tail_boundary(self, snapshot: ConversationSourceSnapshot) -> HotTailBoundary:
        events = snapshot.events
        if not events:
            return HotTailBoundary(first_protected_event_id=None, protected_event_ids=())
        event_start = events[max(0, len(events) - self.config.raw_tail_events)].event_id
        used = 0
        char_index = len(events)
        for index in range(len(events) - 1, -1, -1):
            used += len(events[index].content) + len(events[index].visual_summary)
            char_index = index
            if used >= self.config.raw_tail_characters:
                break
        char_start = events[char_index].event_id
        first_protected = min(event_start, char_start)
        protected = tuple(item.event_id for item in events if item.event_id >= first_protected)
        return HotTailBoundary(
            first_protected_event_id=first_protected,
            protected_event_ids=protected,
        )

    def allow_raw_window_shift(self, *, has_active_coverage: bool) -> bool:
        return has_active_coverage

    def must_roll_prefix(
        self,
        rendered: RenderedHistory,
        *,
        anchor_event_id: int | None,
        high_event_limit: int,
        high_character_limit: int,
        fallback_anchor_event_id: int | None,
    ) -> tuple[int, ...]:
        selection = ContextAssembler._select_history_window(
            rendered,
            anchor_event_id=anchor_event_id,
            high_event_limit=high_event_limit,
            high_character_limit=high_character_limit,
            low_watermark_ratio=self.config.history_window_low_watermark_ratio,
            fallback_anchor_event_id=fallback_anchor_event_id,
        )
        candidate = rendered
        if anchor_event_id is not None:
            anchor_index = next(
                (index for index, item in enumerate(rendered) if item[0] == anchor_event_id),
                None,
            )
            if anchor_index is not None:
                candidate = rendered[anchor_index:]
        candidate_characters = sum(len(item.content or "") for _, _, item in candidate)
        must_roll = (
            anchor_event_id is None
            or next(
                (index for index, item in enumerate(rendered) if item[0] == anchor_event_id),
                None,
            )
            is None
            or len(candidate) > high_event_limit
            or candidate_characters > high_character_limit
        )
        if not must_roll:
            return ()
        kept = set(selection.event_ids)
        return tuple(
            event_id
            for _, event_ids, _ in candidate
            for event_id in event_ids
            if event_id not in kept
        )

    def select_l0_candidate(
        self,
        snapshot: ConversationSourceSnapshot,
        *,
        coverage_end_event_id: int,
        dropped_prefix_ids: tuple[int, ...],
        must_roll: bool,
    ) -> RawRangeCandidate | None:
        tail = self.hot_tail_boundary(snapshot)
        compressible = tuple(
            item
            for item in snapshot.events
            if item.event_id > coverage_end_event_id
            and (
                tail.first_protected_event_id is None
                or item.event_id < tail.first_protected_event_id
            )
        )
        if must_roll:
            dropped = set(dropped_prefix_ids)
            source = tuple(item for item in compressible if item.event_id in dropped)
            if not self._meets_minimum(source):
                return None
            return self._slice_l0(snapshot, source, prefetch=False)
        if not self._meets_minimum(compressible):
            return None
        return self._slice_l0(snapshot, compressible, prefetch=True)

    def select_parent_candidate(
        self, children: tuple[ChildSummaryView, ...]
    ) -> SummaryRollupCandidate | None:
        if not children:
            return None
        ordered = tuple(sorted(children, key=lambda item: item.start_event_id))
        level = ordered[0].level
        run: list[ChildSummaryView] = []
        for child in ordered:
            if child.level != level:
                break
            if run and child.start_event_id != run[-1].end_event_id + 1:
                if self._parent_ready(run):
                    return self._parent_candidate(run)
                run = [child]
                continue
            run.append(child)
        if self._parent_ready(run):
            return self._parent_candidate(run)
        return None

    def _meets_minimum(self, events: tuple[SourceEventProjection, ...]) -> bool:
        if len(events) >= self.config.l0_min_events:
            return True
        characters = sum(len(item.content) + len(item.visual_summary) for item in events)
        return characters >= self.config.l0_min_characters

    def _slice_l0(
        self,
        snapshot: ConversationSourceSnapshot,
        events: tuple[SourceEventProjection, ...],
        *,
        prefetch: bool,
    ) -> RawRangeCandidate | None:
        selected: list[SourceEventProjection] = []
        characters = 0
        for item in events:
            size = len(item.content) + len(item.visual_summary)
            if selected and (
                len(selected) >= self.config.l0_max_events
                or characters + size > self.config.l0_max_characters
            ):
                break
            selected.append(item)
            characters += size
        if not selected:
            return None
        event_ids = tuple(item.event_id for item in selected)
        sliced = ConversationSourceSnapshot(
            state_id=snapshot.state_id,
            reset_epoch=snapshot.reset_epoch,
            scope_type=snapshot.scope_type,
            events=tuple(selected),
            tool_outcomes=snapshot.tool_outcomes,
        )
        return RawRangeCandidate(
            event_ids=event_ids,
            start_event_id=event_ids[0],
            end_event_id=event_ids[-1],
            character_count=characters,
            fingerprint=source_fingerprint(sliced),
            extractive_text=extractive_compact(
                sliced, max_characters=self.config.extractive_max_characters
            ),
            prefetch=prefetch,
        )

    def _parent_ready(self, run: list[ChildSummaryView]) -> bool:
        if len(run) >= self.config.fan_in:
            return True
        rendered = sum(len(item.rendered_text) for item in run)
        return len(run) >= 2 and rendered >= self.config.fan_in_characters

    def _parent_candidate(self, run: list[ChildSummaryView]) -> SummaryRollupCandidate:
        if len(run) >= self.config.fan_in:
            chosen = tuple(run[: self.config.fan_in])
        else:
            chosen = tuple(run)
        return SummaryRollupCandidate(
            child_ids=tuple(item.summary_id for item in chosen),
            start_event_id=chosen[0].start_event_id,
            end_event_id=chosen[-1].end_event_id,
            level=chosen[0].level + 1,
        )
