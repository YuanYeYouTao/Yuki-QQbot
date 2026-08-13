"""Model-safe contracts for bounded Yuki self-reflection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryKind
from qq_ai_bot.persistence.repository_records import EventRecord


class SelfReflectionOperation(StrEnum):
    CREATE = "create"
    CORRECT = "correct"
    MERGE = "merge"
    CONTEST = "contest"
    INVALIDATE = "invalidate"
    NOOP = "noop"


class SelfReflectionVisibility(StrEnum):
    CURRENT_SCOPE = "current_scope"
    GLOBAL = "global"


class SelfCandidateDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SelfReflectionEvent(_Contract):
    ref: str = Field(pattern=r"^event_[1-9]\d*$")
    occurred_at: datetime
    direction: str = Field(pattern=r"^(?:inbound|outbound)$")
    rendered: str = Field(min_length=1, max_length=9000)


class SelfReflectionContextEvent(_Contract):
    ref: str = Field(pattern=r"^context_[1-9]\d*$")
    occurred_at: datetime
    direction: str = Field(pattern=r"^(?:inbound|outbound)$")
    rendered: str = Field(min_length=1, max_length=3000)


class SelfReflectionToolReceipt(_Contract):
    ref: str = Field(pattern=r"^tool_[1-9]\d*$")
    tool_name: str = Field(min_length=1, max_length=255)
    success: bool
    result_excerpt: str = Field(max_length=2000)


class SelfReflectionFact(_Contract):
    ref: str = Field(pattern=r"^(?:fact|candidate)_[1-9]\d*$")
    category: str = Field(max_length=64)
    memory_key: str = Field(max_length=128)
    content: str = Field(max_length=4000)
    status: str = Field(max_length=32)


class SelfReflectionPreviousEpisode(_Contract):
    content: str = Field(min_length=1, max_length=4000)
    valid_from: datetime | None = None
    importance: int = Field(ge=1, le=5)


class SelfReflectionInput(_Contract):
    scope_type: ScopeType
    group_id: str | None = Field(default=None, max_length=64)
    private_peer_user_id: str | None = Field(default=None, max_length=64)
    context_events: tuple[SelfReflectionContextEvent, ...] = ()
    events: tuple[SelfReflectionEvent, ...]
    tool_receipts: tuple[SelfReflectionToolReceipt, ...] = ()
    previous_episode: SelfReflectionPreviousEpisode | None = None
    self_facts: tuple[SelfReflectionFact, ...] = ()
    self_candidates: tuple[SelfReflectionFact, ...] = ()

    @model_validator(mode="after")
    def _scope_identity(self) -> SelfReflectionInput:
        if self.scope_type is ScopeType.GROUP:
            if self.group_id is None or self.private_peer_user_id is not None:
                raise ValueError("group reflection input requires only group_id")
        elif self.private_peer_user_id is None or self.group_id is not None:
            raise ValueError("private reflection input requires only private_peer_user_id")
        return self


class SelfReflectionProposal(_Contract):
    operation: SelfReflectionOperation
    fact_ref: str | None = Field(default=None, pattern=r"^fact_[1-9]\d*$")
    merge_fact_ref: str | None = Field(default=None, pattern=r"^fact_[1-9]\d*$")
    candidate_ref: str | None = Field(default=None, pattern=r"^candidate_[1-9]\d*$")
    candidate_decision: SelfCandidateDecision | None = None
    evidence_refs: tuple[str, ...] = ()
    visibility: SelfReflectionVisibility = SelfReflectionVisibility.CURRENT_SCOPE
    category: (
        Literal[
            "self_fact",
            "self_preference",
            "self_reflection",
            "self_principle",
        ]
        | None
    ) = None
    kind: Literal[MemoryKind.FACT, MemoryKind.PREFERENCE] | None = None
    memory_key: str | None = Field(default=None, max_length=128)
    content: str | None = Field(default=None, max_length=4000)
    reason: str = Field(min_length=1, max_length=500)
    confidence: float = Field(default=0.85, ge=0, le=1)
    importance: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def _shape(self) -> SelfReflectionProposal:
        if self.candidate_ref is None and self.candidate_decision is not None:
            raise ValueError("candidate decision requires candidate_ref")
        if self.operation is SelfReflectionOperation.NOOP:
            if self.fact_ref or self.merge_fact_ref or self.evidence_refs:
                raise ValueError("noop cannot reference facts or evidence")
            if self.candidate_decision is SelfCandidateDecision.ACCEPT:
                raise ValueError("candidate acceptance requires a memory mutation")
            return self
        if self.candidate_decision in {
            SelfCandidateDecision.REJECT,
            SelfCandidateDecision.DEFER,
        }:
            raise ValueError("candidate rejection or deferral requires noop")
        if not self.evidence_refs:
            raise ValueError("self-reflection mutations require trusted evidence aliases")
        if self.operation is SelfReflectionOperation.CREATE:
            if self.fact_ref or self.merge_fact_ref:
                raise ValueError("create cannot reference an existing fact")
            if not all((self.category, self.kind, self.memory_key, self.content)):
                raise ValueError("create requires category, kind, key, and content")
        elif self.fact_ref is None:
            raise ValueError("existing-fact operation requires fact_ref")
        if self.operation is SelfReflectionOperation.MERGE and self.merge_fact_ref is None:
            raise ValueError("merge requires merge_fact_ref")
        return self


class SelfEpisodeProposal(_Contract):
    content: str = Field(min_length=1, max_length=4000)
    importance: int = Field(default=3, ge=1, le=5)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _trusted_evidence_aliases(self) -> SelfEpisodeProposal:
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("episode evidence aliases must be unique")
        if any(
            not (ref.startswith("event_") or ref.startswith("tool_")) for ref in self.evidence_refs
        ):
            raise ValueError("episode evidence may only reference event or tool aliases")
        return self


class SelfReflectionOutput(_Contract):
    proposals: tuple[SelfReflectionProposal, ...] = ()
    episodes: tuple[SelfEpisodeProposal, ...] = ()

    @model_validator(mode="after")
    def _bounded(self) -> SelfReflectionOutput:
        if len(self.proposals) > 8:
            raise ValueError("one self-reflection batch may emit at most eight proposals")
        if len(self.episodes) > 1:
            raise ValueError("one self-reflection batch may emit at most one episode")
        return self


@dataclass(frozen=True, slots=True)
class SelfReflectionState:
    id: int
    conversation_key_hash: str
    bot_user_id: str
    scope_type: ScopeType
    group_id: str | None
    private_peer_user_id: str | None
    last_event_id: int
    latest_event_id: int
    pending_events: int
    pending_characters: int
    pending_since: datetime | None
    has_yuki_reply: bool
    has_tool_result: bool
    high_value_signal: bool


@dataclass(frozen=True, slots=True)
class SelfReflectionBatch:
    state: SelfReflectionState
    events: tuple[EventRecord, ...]
    context_events: tuple[EventRecord, ...]
    trigger_reason: str
    scheduled_slot: str
    run_id: int
    max_input_characters: int


@dataclass(frozen=True, slots=True)
class StoredToolReceipt:
    id: int
    trigger_event_id: int
    tool_name: str
    success: bool
    result_excerpt: str


class SelfReflectionHealth(_Contract):
    """Content-free scheduler state exposed to administrators and health checks."""

    enabled: bool
    running: bool
    schedule_hours: tuple[int, ...]
    timezone: str
    pending_conversations: int = Field(ge=0)
    calls_today: int = Field(ge=0)
    last_run_status: str | None = None
    last_run_completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SelfReflectionCycleResult:
    """Content-free outcome of one bounded worker cycle."""

    attempted_batches: int = 0
    completed_batches: int = 0
    failed_batches: int = 0
    proposal_count: int = 0
    committed_count: int = 0


@dataclass(frozen=True, slots=True)
class SelfReflectionManualRun:
    """Content-free result returned by the explicit administrator command."""

    attempted_batches: int
    completed_batches: int
    failed_batches: int
    proposal_count: int
    committed_count: int
    health: SelfReflectionHealth
    max_daily_calls: int
