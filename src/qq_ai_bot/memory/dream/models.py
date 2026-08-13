"""Stable contracts for Memory Dream planning, model decisions, and observability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DreamRunMode(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"


class DreamRunStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    PARTIAL_FAILED = "partial_failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class DreamClusterStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class DreamOperationType(StrEnum):
    KEEP = "keep"
    MERGE = "merge"
    SYNTHESIZE = "synthesize"
    RECOMPOSE = "recompose"
    CONTEST = "contest"
    RESOLVE = "resolve"


class DreamOperationStatus(StrEnum):
    PROCESSING = "processing"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


class _DreamModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DreamEvidenceInput(_DreamModel):
    occurred_at: datetime | None = None
    relation: str
    excerpt: str = Field(max_length=2000)


class DreamMemoryInput(_DreamModel):
    ref: str = Field(pattern=r"^memory_[1-6]$")
    kind: str
    category: str
    memory_key: str
    content: str
    importance: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    source_type: str
    authority: str
    status: str
    conflict_state: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    evidence: tuple[DreamEvidenceInput, ...] = ()


class DreamInput(_DreamModel):
    scope_type: str
    subject_user_id: str | None = None
    group_id: str | None = None
    visibility_type: str | None = None
    visibility_user_id: str | None = None
    visibility_group_id: str | None = None
    kind: str
    memories: tuple[DreamMemoryInput, ...] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def _single_memory_is_episode_only(self) -> DreamInput:
        if len(self.memories) == 1 and self.kind != "episode":
            raise ValueError("only an episode Dream input may contain one memory")
        return self


class DreamRecomposeOutput(_DreamModel):
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=6)
    content: str = Field(min_length=1, max_length=4000)
    importance: int = Field(ge=1, le=5)

    @field_validator("source_refs")
    @classmethod
    def _unique_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("dream recompose output source refs must be unique")
        return value


class DreamAction(_DreamModel):
    operation: DreamOperationType
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=6)
    anchor_ref: str | None = None
    content: str | None = Field(default=None, max_length=4000)
    importance: int | None = Field(default=None, ge=1, le=5)
    outputs: tuple[DreamRecomposeOutput, ...] = Field(default=(), max_length=4)

    @field_validator("source_refs")
    @classmethod
    def _unique_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("dream action source refs must be unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> DreamAction:
        if self.operation in {
            DreamOperationType.MERGE,
            DreamOperationType.SYNTHESIZE,
            DreamOperationType.RESOLVE,
        }:
            if len(self.source_refs) < 2:
                raise ValueError("dream merge, synthesis, and resolution need two sources")
            if self.anchor_ref not in self.source_refs:
                raise ValueError("dream anchor must be one of the source refs")
        elif self.anchor_ref is not None:
            raise ValueError("dream keep and contest actions do not use an anchor")
        if self.operation is DreamOperationType.RECOMPOSE:
            if not self.outputs:
                raise ValueError("dream recompose requires one or more outputs")
            if self.content is not None or self.importance is not None:
                raise ValueError("dream recompose uses outputs instead of content")
            sources = set(self.source_refs)
            output_sources = {ref for output in self.outputs for ref in output.source_refs}
            if output_sources != sources:
                raise ValueError("dream recompose outputs must cover exactly all action sources")
            if any(not set(output.source_refs).issubset(sources) for output in self.outputs):
                raise ValueError("dream recompose output referenced a source outside the action")
        elif self.outputs:
            raise ValueError("only dream recompose may emit multiple outputs")
        elif self.operation is DreamOperationType.SYNTHESIZE:
            if self.content is None or not self.content.strip():
                raise ValueError("dream synthesis requires content")
        elif self.content is not None or self.importance is not None:
            raise ValueError("only dream synthesis may emit content or importance")
        return self


class DreamOutput(_DreamModel):
    actions: tuple[DreamAction, ...] = Field(default=(), max_length=6)

    @model_validator(mode="after")
    def _disjoint(self) -> DreamOutput:
        used: set[str] = set()
        for action in self.actions:
            overlap = used.intersection(action.source_refs)
            if overlap:
                raise ValueError("dream actions must use disjoint source refs")
            used.update(action.source_refs)
        return self


class DreamPlanStatistics(_DreamModel):
    eligible_facts: int = Field(ge=0)
    ready_facts: int = Field(ge=0)
    missing_embeddings: int = Field(ge=0)
    ambiguous_bot_facts: int = Field(ge=0)
    partitions: int = Field(ge=0)
    candidate_clusters: int = Field(ge=0)
    isolated_facts: int = Field(ge=0)
    estimated_model_calls: int = Field(ge=0)


class DreamRun(_DreamModel):
    public_id: str
    mode: DreamRunMode
    status: DreamRunStatus
    scheduled_slot: str | None = None
    snapshot_max_fact_id: int = Field(ge=0)
    snapshot_created_at: datetime
    statistics: DreamPlanStatistics
    model_calls: int = Field(ge=0)
    completed_clusters: int = Field(ge=0)
    failed_clusters: int = Field(ge=0)
    error_category: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    rolled_back_at: datetime | None = None

    @field_validator(
        "snapshot_created_at",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "cancelled_at",
        "rolled_back_at",
        mode="after",
    )
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class DreamCluster(_DreamModel):
    id: int = Field(gt=0)
    run_id: int = Field(gt=0)
    cluster_key: str
    partition_key: str
    bot_user_id: str
    kind: str
    status: DreamClusterStatus
    fact_ids: tuple[int, ...] = Field(min_length=1)
    fingerprint: str
    attempts: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    operation_count: int = Field(ge=0)
    error_category: str | None = None


class DreamOperationSummary(_DreamModel):
    public_id: str
    cluster_id: int = Field(gt=0)
    operation: DreamOperationType
    status: DreamOperationStatus
    source_fact_ids: tuple[int, ...]
    anchor_fact_id: int | None = None
    output_fact_id: int | None = None
    output_fact_ids: tuple[int, ...] = ()


class DreamRunPage(_DreamModel):
    clusters: tuple[DreamCluster, ...]
    operations: tuple[DreamOperationSummary, ...]


class DreamClusterPreview(_DreamModel):
    run_public_id: str
    cluster_id: int = Field(gt=0)
    fact_ids: tuple[int, ...]
    source_characters: int = Field(ge=0)
    output_characters: int = Field(ge=0)
    compression_ratio: float = Field(ge=0)
    actions: tuple[DreamAction, ...]


class DreamHealth(_DreamModel):
    enabled: bool
    running: bool
    active_run_id: str | None = None
    pending_clusters: int = Field(ge=0)
    failed_clusters: int = Field(ge=0)
    last_completed_at: datetime | None = None
    last_error_category: str | None = None


@dataclass(frozen=True, slots=True)
class DreamMutationResult:
    operation_id: int
    output_fact_ids: tuple[int, ...]
    added_evidence_ids: tuple[int, ...]
    added_relation_ids: tuple[int, ...]
    changed: bool

    @property
    def output_fact_id(self) -> int | None:
        return self.output_fact_ids[0] if self.output_fact_ids else None
