"""Stable request, actor, receipt, and result contracts for memory mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qq_ai_bot.memory.enums import MemoryKind, MemoryReviewState, MemoryScopeType, MemoryStatus
from qq_ai_bot.persistence.repository_records import EventRecord


class MemoryMutationOperation(StrEnum):
    CREATE = "create"
    CORRECT = "correct"
    INVALIDATE = "invalidate"
    RESTORE = "restore"
    CONTEST = "contest"
    MERGE = "merge"
    REASSIGN = "reassign"
    UPDATE_METADATA = "update_metadata"


class MemoryMutationAppliedOperation(StrEnum):
    CREATE = "create"
    CORRECT = "correct"
    INVALIDATE = "invalidate"
    RESTORE = "restore"
    CONTEST = "contest"
    MERGE = "merge"
    REASSIGN = "reassign"
    UPDATE_METADATA = "update_metadata"
    MERGE_EVIDENCE = "merge_evidence"
    NOOP = "noop"


class MemoryMutationOutcome(StrEnum):
    PROCESSING = "processing"
    COMMITTED = "committed"
    COMMITTED_AS_CONTESTED = "committed_as_contested"
    DEDUPLICATED = "deduplicated"
    NO_CHANGE = "no_change"
    REJECTED = "rejected"


class MemoryDecisionActorType(StrEnum):
    AGENT = "agent"
    WORKER = "worker"
    COMMAND = "command"
    ADMIN = "admin"
    PLUGIN = "plugin"
    REFLECTION = "reflection"
    SYSTEM = "system"


class SelfMemoryVisibilityMode(StrEnum):
    CURRENT_SCOPE = "current_scope"
    GLOBAL = "global"


class MemoryMutationRequestBasis(StrEnum):
    USER_REQUESTED = "user_requested"
    AGENT_INITIATED = "agent_initiated"


SELF_MEMORY_CATEGORIES: tuple[str, ...] = (
    "self_fact",
    "self_preference",
    "self_episode",
    "self_reflection",
    "self_principle",
)


class _MutationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryMutationTarget(_MutationModel):
    """A model-safe alias and scope; it never carries raw QQ or group IDs."""

    subject_ref: str = Field(min_length=1, max_length=32)
    scope_type: MemoryScopeType
    subject_name: str | None = Field(default=None, min_length=1, max_length=128)
    candidate_ref: str | None = Field(
        default=None,
        pattern=r"^member_candidate_[1-5]$",
    )

    @model_validator(mode="after")
    def validate_named_target(self) -> MemoryMutationTarget:
        named = self.subject_ref.strip().casefold() == "named_member"
        if named:
            if self.scope_type is not MemoryScopeType.PERSON_GROUP or not self.subject_name:
                raise ValueError("named_member requires subject_name and person_group scope")
        elif self.subject_name is not None or self.candidate_ref is not None:
            raise ValueError("subject_name and candidate_ref require named_member")
        return self


class MemoryMutationSelector(_MutationModel):
    """A bounded, target-local selector for one existing memory fact."""

    memory_key: str | None = Field(default=None, min_length=1, max_length=128)
    old_content: str | None = Field(default=None, min_length=1, max_length=4000)
    category: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_lookup_basis(self) -> MemoryMutationSelector:
        if self.memory_key is None and self.old_content is None:
            raise ValueError("selector requires memory_key or old_content")
        return self


class MemoryMutationRequest(_MutationModel):
    """One requested semantic operation from any Memory V2 write entrypoint."""

    operation: MemoryMutationOperation
    request_basis: MemoryMutationRequestBasis = MemoryMutationRequestBasis.USER_REQUESTED
    fact_id: int | None = Field(default=None, ge=1)
    merge_fact_id: int | None = Field(default=None, ge=1)
    selector: MemoryMutationSelector | None = None
    merge_selector: MemoryMutationSelector | None = None
    target: MemoryMutationTarget | None = None
    visibility: SelfMemoryVisibilityMode | None = None
    new_content: str | None = Field(default=None, max_length=4000)
    memory_key: str | None = Field(default=None, max_length=128)
    category: str | None = Field(default=None, max_length=64)
    kind: MemoryKind | None = None
    reason: str = Field(
        default="agent_requested_memory_change",
        min_length=1,
        max_length=500,
    )
    confidence: float = Field(default=0.9, ge=0, le=1)
    importance: int | None = Field(default=None, ge=1, le=5)
    evidence_refs: tuple[str, ...] = ("current_event",)
    evidence_quote: str | None = Field(default=None, max_length=500)
    expected_fact_state: MemoryStatus | None = None
    valid_from: str | None = Field(default=None, max_length=64)
    valid_until: str | None = Field(default=None, max_length=64)
    review_state: MemoryReviewState | None = None

    @model_validator(mode="after")
    def validate_selectors(self) -> MemoryMutationRequest:
        if self.fact_id is not None and self.selector is not None:
            raise ValueError("fact_id and selector are mutually exclusive")
        if self.merge_fact_id is not None and self.merge_selector is not None:
            raise ValueError("merge_fact_id and merge_selector are mutually exclusive")
        if self.operation is MemoryMutationOperation.CREATE and (
            self.selector is not None or self.merge_selector is not None
        ):
            raise ValueError("create does not accept selectors")
        if self.operation is not MemoryMutationOperation.MERGE and self.merge_selector is not None:
            raise ValueError("merge_selector is only valid for merge")
        return self


@dataclass(frozen=True, slots=True)
class MemoryMutationContext:
    """Trusted scene and actor provenance that model arguments cannot override."""

    event: EventRecord
    conversation_key: str
    turn_origin: str
    delegation_mode: str
    trigger_actor_user_id: str
    decision_actor_type: MemoryDecisionActorType
    decision_actor_id: str | None
    executed_by_bot_user_id: str
    actor_is_superuser: bool = False
    evidence_tool_receipt_id: int | None = None


@dataclass(frozen=True, slots=True)
class MemoryMutationReceipt:
    id: int
    mutation_id: str
    idempotency_key: str
    claim_fingerprint: str
    target_fingerprint: str
    trigger_source_type: str
    trigger_event_id: int | None
    dream_operation_id: int | None
    conversation_key: str
    current_group_id: str | None
    turn_origin: str
    delegation_mode: str
    trigger_actor_user_id: str
    decision_actor_type: MemoryDecisionActorType
    decision_actor_id: str | None
    executed_by_bot_user_id: str | None
    requested_operation: MemoryMutationOperation
    applied_operation: MemoryMutationAppliedOperation
    old_fact_id: int | None
    new_fact_id: int | None
    outcome: MemoryMutationOutcome
    reason_code: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryMutationCandidate:
    fact_id: int
    memory_ref: str
    memory_key: str
    category: str
    kind: MemoryKind
    content: str
    status: MemoryStatus


@dataclass(frozen=True, slots=True)
class MemoryMutationResult:
    ok: bool
    mutation_id: str | None
    requested_operation: MemoryMutationOperation
    applied_operation: MemoryMutationAppliedOperation
    outcome: MemoryMutationOutcome
    old_fact_id: int | None = None
    new_fact_id: int | None = None
    reason_code: str = ""
    deduplicated: bool = False
    candidates: tuple[MemoryMutationCandidate, ...] = ()

    @classmethod
    def from_receipt(
        cls,
        receipt: MemoryMutationReceipt,
        *,
        deduplicated: bool,
        requested_operation: MemoryMutationOperation | None = None,
    ) -> MemoryMutationResult:
        return cls(
            ok=receipt.outcome
            in {
                MemoryMutationOutcome.COMMITTED,
                MemoryMutationOutcome.COMMITTED_AS_CONTESTED,
                MemoryMutationOutcome.DEDUPLICATED,
            },
            mutation_id=receipt.mutation_id,
            requested_operation=requested_operation or receipt.requested_operation,
            applied_operation=receipt.applied_operation,
            outcome=(MemoryMutationOutcome.DEDUPLICATED if deduplicated else receipt.outcome),
            old_fact_id=receipt.old_fact_id,
            new_fact_id=receipt.new_fact_id,
            reason_code=receipt.reason_code,
            deduplicated=deduplicated,
        )
