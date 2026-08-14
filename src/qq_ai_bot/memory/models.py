"""Strict provider-neutral domain objects for Memory V2."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryContextMode,
    MemoryEvidenceRelation,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryJobStatus,
    MemoryKind,
    MemoryProcessingSource,
    MemoryRebuildJobOutcome,
    MemoryRecallPurpose,
    MemoryResolutionAction,
    MemoryRetrievalMode,
    MemoryReviewState,
    MemoryScopeType,
    MemorySemanticRelation,
    MemorySourceType,
    MemoryStateAction,
    MemoryStatus,
    MemorySubjectRole,
    MemoryTargetRole,
    MemoryTemporalConstraint,
    MemoryTemporalIntentMode,
    SelfMemoryVisibility,
)
from qq_ai_bot.persistence.repository_records import EventRecord


class _MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryFact(_MemoryModel):
    id: int = Field(gt=0)
    scope_type: MemoryScopeType
    subject_user_id: str | None = None
    group_id: str | None = None
    visibility_type: SelfMemoryVisibility | None = None
    visibility_user_id: str | None = None
    visibility_group_id: str | None = None
    kind: MemoryKind
    memory_key: str
    category: str
    content: str
    normalized_content: str
    importance: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    source_type: MemorySourceType
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT
    status: MemoryStatus
    conflict_state: MemoryConflictState = MemoryConflictState.CLEAR
    supersedes_id: int | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_confirmed_at: datetime = Field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    invalidated_reason: MemoryInvalidationReason | None = None
    last_injected_at: datetime | None = None
    evidence_count: int = Field(default=0, ge=0)
    validation_version: str = "memory-v2-quality-v1"
    last_audited_at: datetime | None = None
    review_state: MemoryReviewState = MemoryReviewState.VERIFIED

    @field_validator(
        "valid_from",
        "valid_until",
        "created_at",
        "updated_at",
        "last_confirmed_at",
        "last_injected_at",
        "last_audited_at",
        mode="after",
    )
    @classmethod
    def _normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> MemoryFact:
        _validate_fact_identity(
            scope_type=self.scope_type,
            subject_user_id=self.subject_user_id,
            group_id=self.group_id,
            visibility_type=self.visibility_type,
            visibility_user_id=self.visibility_user_id,
            visibility_group_id=self.visibility_group_id,
        )
        _validate_fact_lifecycle(
            status=self.status,
            conflict_state=self.conflict_state,
            invalidated_reason=self.invalidated_reason,
        )
        if (
            self.authority is MemoryAuthority.AGENT_REFLECTION
            and self.scope_type is not MemoryScopeType.SELF
        ):
            raise ValueError("agent reflection authority is only valid for self memory")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_from > self.valid_until
        ):
            raise ValueError("memory valid_from must not be after valid_until")
        return self

    @property
    def user_id(self) -> str | None:
        """Compatibility projection used by Plugin API v1."""

        return self.subject_user_id

    @property
    def key(self) -> str:
        return self.memory_key

    @property
    def value(self) -> str:
        return self.content


class MemoryEvidence(_MemoryModel):
    id: int = Field(gt=0)
    fact_id: int = Field(gt=0)
    event_id: int | None = Field(default=None, gt=0)
    tool_receipt_id: int | None = Field(default=None, gt=0)
    source_speaker_user_id: str
    relation: MemoryEvidenceRelation
    confidence: float = Field(ge=0, le=1)
    authority: MemoryAuthority
    excerpt: str
    created_at: datetime


class MemoryEvidenceCreate(_MemoryModel):
    event_id: int | None = Field(default=None, gt=0)
    tool_receipt_id: int | None = Field(default=None, gt=0)
    source_speaker_user_id: str
    relation: MemoryEvidenceRelation
    confidence: float = Field(default=1.0, ge=0, le=1)
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT
    excerpt: str

    @model_validator(mode="after")
    def _one_source(self) -> MemoryEvidenceCreate:
        if (self.event_id is None) == (self.tool_receipt_id is None):
            raise ValueError("memory evidence requires exactly one source")
        return self


class MemoryFactCreate(_MemoryModel):
    scope_type: MemoryScopeType
    subject_user_id: str | None = None
    group_id: str | None = None
    visibility_type: SelfMemoryVisibility | None = None
    visibility_user_id: str | None = None
    visibility_group_id: str | None = None
    kind: MemoryKind = MemoryKind.FACT
    memory_key: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=4000)
    importance: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=1.0, ge=0, le=1)
    source_type: MemorySourceType
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT
    status: MemoryStatus = MemoryStatus.ACTIVE
    conflict_state: MemoryConflictState = MemoryConflictState.CLEAR
    invalidated_reason: MemoryInvalidationReason | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    validation_version: str = "memory-v2-quality-v1"
    last_audited_at: datetime | None = None
    review_state: MemoryReviewState = MemoryReviewState.VERIFIED

    @field_validator("valid_from", "valid_until", mode="after")
    @classmethod
    def _normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_scope(self) -> MemoryFactCreate:
        _validate_fact_identity(
            scope_type=self.scope_type,
            subject_user_id=self.subject_user_id,
            group_id=self.group_id,
            visibility_type=self.visibility_type,
            visibility_user_id=self.visibility_user_id,
            visibility_group_id=self.visibility_group_id,
        )
        _validate_fact_lifecycle(
            status=self.status,
            conflict_state=self.conflict_state,
            invalidated_reason=self.invalidated_reason,
        )
        if (
            self.authority is MemoryAuthority.AGENT_REFLECTION
            and self.scope_type is not MemoryScopeType.SELF
        ):
            raise ValueError("agent reflection authority is only valid for self memory")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_from > self.valid_until
        ):
            raise ValueError("memory valid_from must not be after valid_until")
        return self


class MemoryFactRelation(_MemoryModel):
    id: int = Field(gt=0)
    source_fact_id: int = Field(gt=0)
    target_fact_id: int = Field(gt=0)
    relation_type: MemoryFactRelationType
    confidence: float = Field(ge=0, le=1)
    source_event_id: int | None = Field(default=None, gt=0)
    created_at: datetime


class MemoryFactStateEvent(_MemoryModel):
    id: int = Field(gt=0)
    fact_id: int = Field(gt=0)
    action: MemoryStateAction
    from_status: MemoryStatus | None = None
    to_status: MemoryStatus | None = None
    from_conflict_state: MemoryConflictState | None = None
    to_conflict_state: MemoryConflictState | None = None
    reason_code: str
    source_event_id: int | None = Field(default=None, gt=0)
    actor_user_id: str | None = None
    created_at: datetime


class MemoryCandidate(_MemoryModel):
    candidate_ref: str = Field(pattern=r"^candidate_[1-9][0-9]*$")
    fact: MemoryFact
    exact_key: bool = False
    exact_content: bool = False
    relevance: float = 0


class CandidateRelation(_MemoryModel):
    candidate_ref: str = Field(pattern=r"^candidate_[1-9][0-9]*$")
    relation: MemorySemanticRelation
    confidence: float = Field(ge=0, le=1)


class MemoryRelationClassification(_MemoryModel):
    relations: tuple[CandidateRelation, ...] = ()


class MemoryResolutionPlan(_MemoryModel):
    action: MemoryResolutionAction
    existing_fact_id: int | None = Field(default=None, gt=0)
    new_fact_status: MemoryStatus | None = None
    new_conflict_state: MemoryConflictState | None = None
    existing_status: MemoryStatus | None = None
    existing_conflict_state: MemoryConflictState | None = None
    relation_types: tuple[MemoryFactRelationType, ...] = ()
    reason_code: str
    append_evidence: bool = True
    create_new_fact: bool = False


class MemoryConsistencyHealth(_MemoryModel):
    active_slot_conflicts: int = Field(ge=0)
    contested_fact_count: int = Field(ge=0)
    active_contested_count: int = Field(ge=0)
    orphan_relation_count: int = Field(ge=0)
    cross_target_relation_count: int = Field(ge=0)
    orphan_state_event_count: int = Field(ge=0)
    invalidated_without_reason_count: int = Field(ge=0)
    superseded_without_chain_count: int = Field(ge=0)
    evidence_authority_mismatch_count: int = Field(ge=0)
    expired_active_count: int = Field(ge=0)
    stale_backlog_count: int = Field(ge=0)
    classifier_recent_errors: int = Field(ge=0)
    maintenance_last_success_at: datetime | None = None

    @property
    def healthy(self) -> bool:
        return not any(
            (
                self.active_slot_conflicts,
                self.orphan_relation_count,
                self.cross_target_relation_count,
                self.orphan_state_event_count,
                self.invalidated_without_reason_count,
                self.superseded_without_chain_count,
                self.expired_active_count,
            )
        )


class MemoryFactQuery(_MemoryModel):
    scope_type: MemoryScopeType
    subject_user_id: str | None = None
    group_id: str | None = None
    visibility_type: SelfMemoryVisibility | None = None
    visibility_user_id: str | None = None
    visibility_group_id: str | None = None
    kind: MemoryKind | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE

    @model_validator(mode="after")
    def _validate_scope(self) -> MemoryFactQuery:
        MemoryFactCreate(
            scope_type=self.scope_type,
            subject_user_id=self.subject_user_id,
            group_id=self.group_id,
            visibility_type=self.visibility_type,
            visibility_user_id=self.visibility_user_id,
            visibility_group_id=self.visibility_group_id,
            kind=self.kind or MemoryKind.FACT,
            memory_key="query",
            category="query",
            content="query",
            source_type=MemorySourceType.AUTOMATIC,
        )
        return self


class MemoryJob(_MemoryModel):
    id: int = Field(gt=0)
    event_id: int = Field(gt=0)
    conversation_key: str
    status: MemoryJobStatus
    attempts: int = Field(ge=0)
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    error_category: str | None = None
    processing_source: MemoryProcessingSource = MemoryProcessingSource.LIVE
    rebuild_run_id: int | None = None
    outcome: MemoryRebuildJobOutcome | None = None
    completed_at: datetime | None = None
    event: EventRecord


class MemoryContextBlock(_MemoryModel):
    entity: MemoryScopeType
    subject_user_id: str | None = None
    group_id: str | None = None
    facts: tuple[MemoryFact, ...] = ()


class MemoryEntityTarget(_MemoryModel):
    role: MemoryTargetRole
    scope_type: MemoryScopeType
    subject_user_id: str | None = None
    group_id: str | None = None
    visibility_type: SelfMemoryVisibility | None = None
    visibility_user_id: str | None = None
    visibility_group_id: str | None = None
    block_id: str

    @model_validator(mode="after")
    def _validate_scope(self) -> MemoryEntityTarget:
        MemoryFactQuery(
            scope_type=self.scope_type,
            subject_user_id=self.subject_user_id,
            group_id=self.group_id,
            visibility_type=self.visibility_type,
            visibility_user_id=self.visibility_user_id,
            visibility_group_id=self.visibility_group_id,
        )
        expected = {
            MemoryTargetRole.CURRENT_SELF: MemoryScopeType.SELF,
            MemoryTargetRole.CURRENT_PERSON: MemoryScopeType.PERSON,
            MemoryTargetRole.CURRENT_PERSON_GROUP: MemoryScopeType.PERSON_GROUP,
            MemoryTargetRole.CURRENT_GROUP: MemoryScopeType.GROUP,
            MemoryTargetRole.REFERENCED_PERSON: MemoryScopeType.PERSON,
            MemoryTargetRole.REFERENCED_PERSON_GROUP: MemoryScopeType.PERSON_GROUP,
        }[self.role]
        if self.scope_type is not expected:
            raise ValueError("memory target role does not match its scope")
        return self


class MemoryTemporalIntent(_MemoryModel):
    mode: MemoryTemporalIntentMode = MemoryTemporalIntentMode.UNSPECIFIED
    constraint: MemoryTemporalConstraint = MemoryTemporalConstraint.SOFT
    start_at: datetime | None = None
    end_at: datetime | None = None

    @field_validator("start_at", "end_at", mode="after")
    @classmethod
    def _normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_range(self) -> MemoryTemporalIntent:
        if self.mode is MemoryTemporalIntentMode.RANGE:
            if self.start_at is None and self.end_at is None:
                raise ValueError("memory temporal range requires at least one boundary")
        elif self.start_at is not None or self.end_at is not None:
            raise ValueError("memory temporal boundaries require range mode")
        if self.start_at is not None and self.end_at is not None and self.start_at > self.end_at:
            raise ValueError("memory temporal start_at must not be after end_at")
        if (
            self.constraint is MemoryTemporalConstraint.STRICT
            and self.mode is not MemoryTemporalIntentMode.RANGE
        ):
            raise ValueError("strict memory temporal constraint requires range mode")
        return self


class MemoryQueryIntent(_MemoryModel):
    """Provider-neutral semantic recall intent; identity targets are separate."""

    mode: MemoryContextMode = MemoryContextMode.LEXICAL
    purpose: MemoryRecallPurpose = MemoryRecallPurpose.BACKGROUND
    subjects: tuple[MemorySubjectRole, ...] = Field(default=(), max_length=4)
    entities: tuple[str, ...] = Field(default=(), max_length=5)
    temporal: MemoryTemporalIntent = MemoryTemporalIntent()
    preferred_kinds: tuple[MemoryKind, ...] = Field(default=(), max_length=3)

    @field_validator("subjects", "preferred_kinds", mode="after")
    @classmethod
    def _deduplicate_enums(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return tuple(dict.fromkeys(value))

    @field_validator("entities", mode="after")
    @classmethod
    def _normalize_entities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            clean = " ".join(item.split()).strip()[:64]
            if clean and clean not in normalized:
                normalized.append(clean)
        return tuple(normalized)

    @property
    def self_recall(self) -> bool:
        return MemorySubjectRole.CURRENT_SELF in self.subjects


class MemoryQuery(_MemoryModel):
    text: str
    normalized_text: str
    mode: MemoryRetrievalMode
    targets: tuple[MemoryEntityTarget, ...]
    kinds: tuple[MemoryKind, ...] = ()
    candidate_limit: int = Field(gt=0)
    limit_per_target: int = Field(gt=0)
    always_on_explicit_preference_limit: int = Field(ge=0)
    query_term_limit: int = Field(gt=0)
    short_query_fallback_enabled: bool = True
    semantic_enabled: bool = True
    semantic_candidate_limit: int = Field(default=50, gt=0)
    semantic_min_similarity: float = Field(default=0.35, ge=-1, le=1)
    hybrid_lexical_weight: float = Field(default=1.0, ge=0)
    hybrid_semantic_weight: float = Field(default=1.0, ge=0)
    hybrid_rrf_k: int = Field(default=60, gt=0)
    intent: MemoryQueryIntent | None = None
    intent_rerank_enabled: bool = True
    activation_ranking_enabled: bool = True
    activation_half_life_episode_days: float = Field(default=14.0, gt=0)
    activation_half_life_fact_days: float = Field(default=60.0, gt=0)
    activation_half_life_preference_days: float = Field(default=120.0, gt=0)
    activation_half_life_explicit_days: float = Field(default=365.0, gt=0)
    intent_recent_window_days: int = Field(default=90, gt=0)
    recall_trace_candidate_limit: int = Field(default=20, gt=0, le=100)


class MemoryLexicalCandidate(_MemoryModel):
    fact_id: int = Field(gt=0)
    target: MemoryEntityTarget
    fts_rank: float
    exact_match: bool = False
    matched_terms: tuple[str, ...] = ()


class MemoryRetrievalHit(_MemoryModel):
    fact: MemoryFact
    target: MemoryEntityTarget
    rank: int = Field(gt=0)
    lexical_score: float | None = None
    semantic_score: float | None = None
    fusion_score: float = 0
    lexical_rank: int | None = Field(default=None, gt=0)
    semantic_rank: int | None = Field(default=None, gt=0)
    sources: tuple[str, ...] = ()
    exact_match: bool = False
    matched_terms: tuple[str, ...] = ()
    selection_reason: str
    base_rank_score: float = Field(default=0, ge=0, le=1)
    subject_score: float = Field(default=0.5, ge=0, le=1)
    entity_score: float = Field(default=0.5, ge=0, le=1)
    temporal_score: float = Field(default=0.5, ge=0, le=1)
    kind_score: float = Field(default=0.5, ge=0, le=1)
    activation_score: float = Field(default=0.5, ge=0, le=1)
    rerank_score: float = Field(default=0, ge=0, le=1)


class MemoryRetrievalBlock(_MemoryModel):
    target: MemoryEntityTarget
    hits: tuple[MemoryRetrievalHit, ...] = ()


class MemoryRetrievalResult(_MemoryModel):
    blocks: tuple[MemoryRetrievalBlock, ...]
    hits: tuple[MemoryRetrievalHit, ...]
    candidate_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    query_hash: str
    mode: MemoryRetrievalMode
    semantic_status: str = "disabled"
    semantic_degraded: bool = False
    embedding_profile: str | None = None
    trace_hits: tuple[MemoryRetrievalHit, ...] = ()


class MemoryActivationState(_MemoryModel):
    fact_id: int = Field(gt=0)
    activation: float = Field(ge=0, le=1)
    activation_updated_at: datetime
    last_recalled_at: datetime | None = None
    recall_count: int = Field(default=0, ge=0)
    revision: int = Field(default=0, ge=0)

    @field_validator("activation_updated_at", "last_recalled_at", mode="after")
    @classmethod
    def _activation_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class MemoryUsageReport(_MemoryModel):
    turn_id: str = Field(min_length=1, max_length=64)
    memory_refs: tuple[str, ...] = Field(default=(), max_length=100)

    @field_validator("memory_refs", mode="after")
    @classmethod
    def _valid_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        refs = tuple(dict.fromkeys(value))
        if any(not item.startswith("M") or not item[1:].isdigit() for item in refs):
            raise ValueError("memory refs must use M<fact_id>")
        return refs


class MemoryRecallItem(_MemoryModel):
    fact_id: int = Field(gt=0)
    target_role: MemoryTargetRole
    candidate: bool = True
    selected: bool = False
    injected: bool = False
    used: bool = False
    reinforced: bool = False
    base_rank_score: float = Field(default=0, ge=0, le=1)
    subject_score: float = Field(default=0.5, ge=0, le=1)
    entity_score: float = Field(default=0.5, ge=0, le=1)
    temporal_score: float = Field(default=0.5, ge=0, le=1)
    kind_score: float = Field(default=0.5, ge=0, le=1)
    activation_score: float = Field(default=0.5, ge=0, le=1)
    rerank_score: float = Field(default=0, ge=0, le=1)
    selection_reason: str = Field(default="", max_length=64)


class MemoryRecallReceipt(_MemoryModel):
    turn_id: str = Field(min_length=1, max_length=64)
    mode: MemoryContextMode
    purpose: MemoryRecallPurpose
    origin: str = Field(max_length=32)
    candidate_count: int = Field(default=0, ge=0)
    selected_count: int = Field(default=0, ge=0)
    injected_count: int = Field(default=0, ge=0)
    used_count: int = Field(default=0, ge=0)
    reinforced_count: int = Field(default=0, ge=0)
    items: tuple[MemoryRecallItem, ...] = ()


class MemoryIndexHealth(_MemoryModel):
    fact_count: int = Field(ge=0)
    indexed_row_count: int = Field(ge=0)
    missing_row_count: int = Field(ge=0)
    orphan_row_count: int = Field(ge=0)

    @property
    def healthy(self) -> bool:
        return self.missing_row_count == 0 and self.orphan_row_count == 0


def _validate_fact_lifecycle(
    *,
    status: MemoryStatus,
    conflict_state: MemoryConflictState,
    invalidated_reason: MemoryInvalidationReason | None,
) -> None:
    if status is MemoryStatus.CONTESTED and conflict_state is not MemoryConflictState.CONTESTED:
        raise ValueError("contested memory status requires contested conflict state")
    if status is MemoryStatus.INVALIDATED and invalidated_reason is None:
        raise ValueError("invalidated memory fact requires an invalidation reason")
    if status is not MemoryStatus.INVALIDATED and invalidated_reason is not None:
        raise ValueError("invalidation reason is only valid for invalidated memory facts")


def _validate_fact_identity(
    *,
    scope_type: MemoryScopeType,
    subject_user_id: str | None,
    group_id: str | None,
    visibility_type: SelfMemoryVisibility | None,
    visibility_user_id: str | None,
    visibility_group_id: str | None,
) -> None:
    """Keep subject identity separate from SELF conversation visibility."""

    if scope_type is MemoryScopeType.PERSON:
        identity_valid = bool(subject_user_id) and group_id is None
    elif scope_type is MemoryScopeType.PERSON_GROUP:
        identity_valid = bool(subject_user_id) and bool(group_id)
    elif scope_type is MemoryScopeType.GROUP:
        identity_valid = subject_user_id is None and bool(group_id)
    else:
        identity_valid = subject_user_id is None and group_id is None
    if not identity_valid:
        raise ValueError("memory fact identity does not match its scope")

    if scope_type is not MemoryScopeType.SELF:
        if any((visibility_type, visibility_user_id, visibility_group_id)):
            raise ValueError("non-self memory cannot carry self visibility")
        return
    if visibility_type is SelfMemoryVisibility.GLOBAL:
        visibility_valid = visibility_user_id is None and visibility_group_id is None
    elif visibility_type is SelfMemoryVisibility.PRIVATE:
        visibility_valid = bool(visibility_user_id) and visibility_group_id is None
    elif visibility_type is SelfMemoryVisibility.GROUP:
        visibility_valid = visibility_user_id is None and bool(visibility_group_id)
    else:
        visibility_valid = False
    if not visibility_valid:
        raise ValueError("self memory visibility does not match its boundary")
