"""Immutable contracts for single-checkpoint rollup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.persistence.repository_records import EventRecord


class RollupKind(StrEnum):
    MODEL = "model"
    EXTRACTIVE = "extractive"


class RollupJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"


@dataclass(frozen=True, slots=True)
class RollupPolicyConfig:
    raw_tail_events: int = 768
    raw_tail_characters: int = 65_536
    trigger_events: int = 512
    trigger_characters: int = 49_152
    stop_events: int = 192
    stop_characters: int = 16_384
    batch_max_events: int = 256
    batch_max_characters: int = 32_768
    summary_max_characters: int = 1200

    def __post_init__(self) -> None:
        positive = (
            self.raw_tail_events,
            self.raw_tail_characters,
            self.trigger_events,
            self.trigger_characters,
            self.batch_max_events,
            self.batch_max_characters,
            self.summary_max_characters,
        )
        if any(value < 1 for value in positive):
            raise ValueError("positive rollup settings must be at least one")
        if self.trigger_events < 2:
            raise ValueError("trigger_events must be at least two")
        if self.stop_events < 0 or self.stop_characters < 0:
            raise ValueError("rollup low watermarks must not be negative")
        if self.trigger_events <= self.stop_events:
            raise ValueError("trigger_events must be greater than stop_events")
        if self.trigger_characters <= self.stop_characters:
            raise ValueError("trigger_characters must be greater than stop_characters")


@dataclass(frozen=True, slots=True)
class ConversationScopeState:
    id: int
    scope: ConversationScope
    generation: int
    starts_after_event_id: int
    last_event_id: int
    last_generation_change_event_id: int
    uncovered_event_count: int
    uncovered_character_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationRollupState:
    scope_id: int
    generation: int
    covered_through_event_id: int
    summary_text: str
    summary_kind: RollupKind
    source_fingerprint: str
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RollupJobClaim:
    scope_id: int
    generation: int
    claimed_signal_revision: int
    failure_count: int
    lease_owner: str
    lease_token: str
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class RollupCandidate:
    scope_id: int
    generation: int
    source_coverage: int
    source_rollup_revision: int
    previous_summary: str
    events: tuple[EventRecord, ...]
    event_count: int
    projection_characters: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ConversationPromptSnapshot:
    scope: ConversationScopeState
    rollup: ConversationRollupState | None
    raw_events: tuple[EventRecord, ...]
    effective_coverage: int
    raw_tail_end_event_id: int


@dataclass(frozen=True, slots=True)
class RollupCommitResult:
    rollup: ConversationRollupState
    claim_retained: bool
