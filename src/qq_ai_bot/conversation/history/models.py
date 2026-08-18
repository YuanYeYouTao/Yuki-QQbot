"""Provider-neutral contracts for conversation history rollup."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from qq_ai_bot.domain.conversations import ScopeType


class HistorySummaryStatus(StrEnum):
    ACTIVE = "active"
    ROLLED_UP = "rolled_up"
    INVALIDATED = "invalidated"


class HistorySummaryMode(StrEnum):
    EXTRACTIVE = "extractive"
    MODEL_SUMMARY = "model_summary"


class HistorySummaryTrust(StrEnum):
    EXTRACTIVE_COMPACT = "extractive_compact"
    MODEL_SUMMARY = "model_summary"


class HistoryMemberType(StrEnum):
    EVENT = "event"
    SUMMARY = "summary"


class HistoryJobKind(StrEnum):
    RAW_RANGE = "raw_range"
    SUMMARY_ROLLUP = "summary_rollup"
    REBUILD = "rebuild"


class HistoryJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class HistoryJobOutcome(StrEnum):
    SUMMARY = "summary"
    NO_CHANGE = "no_change"


class _HistoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationHistoryIdentity(_HistoryModel):
    bot_user_id: str
    scope_type: ScopeType
    private_peer_user_id: str | None = None
    group_id: str | None = None
    reset_at: datetime | None = None


class ConversationHistoryMember(_HistoryModel):
    member_type: HistoryMemberType
    ordinal: int = Field(ge=0)
    source_event_id: int | None = None
    source_summary_id: int | None = None


class ConversationHistoryState(_HistoryModel):
    id: int
    bot_user_id: str
    scope_type: str
    private_peer_user_id: str | None
    group_id: str | None
    reset_at: datetime | None
    last_seen_event_id: int
    active_frontier_end_event_id: int
    pending_event_count: int = Field(ge=0)
    pending_character_count: int = Field(ge=0)
    revision: int = Field(ge=0)


class ConversationHistorySummary(_HistoryModel):
    id: int
    state_id: int
    level: int = Field(ge=0)
    status: HistorySummaryStatus
    start_event_id: int
    end_event_id: int
    mode: HistorySummaryMode
    trust: HistorySummaryTrust
    summarizer_version: str
    source_fingerprint: str
    replaced_by_summary_id: int | None = None
    rendered_text: str = ""
    members: tuple[ConversationHistoryMember, ...] = ()


class ConversationHistoryJob(_HistoryModel):
    id: int
    state_id: int
    job_kind: HistoryJobKind
    source_level: int = Field(ge=0)
    source_start_id: int
    source_end_id: int
    source_fingerprint: str
    summarizer_version: str
    status: HistoryJobStatus
    attempts: int = Field(ge=0)
    outcome: HistoryJobOutcome | None = None
    result_summary_id: int | None = None
    lease_owner: str | None = None


class HistoryContextSnapshot(_HistoryModel):
    """Consistent frontier read. Recent raw events are attached in context compile."""

    state: ConversationHistoryState
    frontier: tuple[ConversationHistorySummary, ...]
    coverage_end_event_id: int
    revision: int
