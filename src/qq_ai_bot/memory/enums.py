"""Closed vocabularies for the Memory V2 domain."""

from enum import StrEnum


class MemoryScopeType(StrEnum):
    PERSON = "person"
    PERSON_GROUP = "person_group"
    GROUP = "group"
    SELF = "self"


class SelfMemoryVisibility(StrEnum):
    """Conversation boundary for one single-instance Yuki self memory."""

    GLOBAL = "global"
    PRIVATE = "private"
    GROUP = "group"


class MemoryKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    EPISODE = "episode"


class MemorySourceType(StrEnum):
    AUTOMATIC = "automatic"
    EXPLICIT = "explicit"
    REBUILD = "rebuild"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    CONTESTED = "contested"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class MemoryClaimOperation(StrEnum):
    ASSERT = "assert"
    CONFIRM = "confirm"
    CORRECT = "correct"
    RETRACT = "retract"


class MemorySubjectBasis(StrEnum):
    """Model-declared subject evidence; backend policy remains authoritative."""

    FIRST_PERSON = "first_person"
    OMITTED_SELF = "omitted_self"
    ADDRESSED_SECOND_PERSON = "addressed_second_person"
    MENTIONED_SUBJECT = "mentioned_subject"
    REPLY_SUBJECT = "reply_subject"
    NAMED_UNRESOLVED = "named_unresolved"
    GROUP = "group"
    ABOUT_YUKI = "about_yuki"


class MemoryRetention(StrEnum):
    """Whether a claim belongs in durable memory without an explicit request."""

    DURABLE = "durable"
    MEANINGFUL_EPISODE = "meaningful_episode"
    TRANSIENT = "transient"


class MemorySourceStyle(StrEnum):
    """Semantic style of the quoted source rather than its transport origin."""

    NATURAL_STATEMENT = "natural_statement"
    INSTRUCTION = "instruction"
    ROLEPLAY = "roleplay"
    GENERATED_RESULT = "generated_result"
    QUOTED_TEXT = "quoted_text"


class MemoryReviewState(StrEnum):
    LEGACY_UNREVIEWED = "legacy_unreviewed"
    VERIFIED = "verified"
    QUARANTINED = "quarantined"


class MemoryAuthority(StrEnum):
    EXPLICIT = "explicit"
    SELF_REPORT = "self_report"
    GROUP_REPORT = "group_report"
    THIRD_PARTY = "third_party"
    AGENT_REFLECTION = "agent_reflection"


class MemoryConflictState(StrEnum):
    CLEAR = "clear"
    CONTESTED = "contested"


class MemoryFactRelationType(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    REFINES = "refines"
    EQUIVALENT = "equivalent"


class MemoryEvidenceRelation(StrEnum):
    SELF_STATEMENT = "self_statement"
    GROUP_STATEMENT = "group_statement"
    THIRD_PARTY_STATEMENT = "third_party_statement"
    EXPLICIT_COMMAND = "explicit_command"
    CONFIRMATION = "confirmation"
    CORRECTION = "correction"
    RETRACTION = "retraction"
    REBUILD = "rebuild"
    AGENT_REFLECTION = "agent_reflection"


class MemoryStateAction(StrEnum):
    CREATED = "created"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    CONTESTED = "contested"
    CONFLICT_CLEARED = "conflict_cleared"
    INVALIDATED = "invalidated"
    RESTORED = "restored"
    MERGED = "merged"
    EXPIRED = "expired"
    STALE_INVALIDATED = "stale_invalidated"


class MemoryInvalidationReason(StrEnum):
    USER_RETRACTED = "user_retracted"
    ADMINISTRATOR_INVALIDATED = "administrator_invalidated"
    EXPIRED = "expired"
    STALE = "stale"
    MERGED = "merged"
    PRIVACY_DELETION = "privacy_deletion"
    CONFLICT_RESOLUTION = "conflict_resolution"
    PLUGIN_EXPLICIT_INVALIDATION = "plugin_explicit_invalidation"
    DREAM_ROLLBACK = "dream_rollback"


class MemoryTemporalMode(StrEnum):
    PERSISTENT = "persistent"
    TEMPORARY = "temporary"
    EPISODE = "episode"


class MemorySemanticRelation(StrEnum):
    SAME_CLAIM = "same_claim"
    CONFIRMS = "confirms"
    SUPERSEDES = "supersedes"
    CONTRADICTS = "contradicts"
    COEXISTS = "coexists"
    UNRELATED = "unrelated"
    RETRACTS = "retracts"


class MemoryResolutionAction(StrEnum):
    CREATE = "create"
    MERGE_EVIDENCE = "merge_evidence"
    SUPERSEDE = "supersede"
    CONTEST = "contest"
    INVALIDATE = "invalidate"
    RESTORE = "restore"
    MERGE = "merge"
    NOOP = "noop"


class MemoryJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class MemoryProcessingSource(StrEnum):
    LIVE = "live"
    REBUILD = "rebuild"


class MemoryRebuildRunStatus(StrEnum):
    PLANNED = "planned"
    EXTRACTING = "extracting"
    EXTRACTION_PAUSED = "extraction_paused"
    REVIEW = "review"
    COMMITTING = "committing"
    COMMIT_PAUSED = "commit_paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class MemoryRebuildItemStatus(StrEnum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    STAGED = "staged"
    NO_CLAIMS = "no_claims"
    SKIPPED = "skipped"
    FAILED = "failed"
    COMMITTED = "committed"


class MemoryRebuildReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class MemoryRebuildCommitStatus(StrEnum):
    PENDING = "pending"
    COMMITTED = "committed"
    SKIPPED = "skipped"
    FAILED = "failed"


class MemoryRebuildJobOutcome(StrEnum):
    CLAIMS_APPLIED = "claims_applied"
    CANDIDATES_STAGED = "candidates_staged"
    NO_CLAIMS = "no_claims"
    ALL_REJECTED = "all_rejected"
    ALREADY_PROCESSED = "already_processed"


class MemoryRebuildThirdPartyMode(StrEnum):
    DISABLED = "disabled"
    TRUSTED_METADATA = "trusted_metadata"


class MemoryRebuildExpiredClaimPolicy(StrEnum):
    SKIP = "skip"
    STAGE_INVALIDATED = "stage_invalidated"


class MemoryContextMode(StrEnum):
    """Planner-selected amount of long-term memory needed for one reply."""

    NONE = "none"
    LEXICAL = "lexical"
    HYBRID = "hybrid"
    OVERVIEW = "overview"


class MemoryAccessMode(StrEnum):
    """Historical metrics bucket for one turn's memory participation.

    Business paths must inspect ``MemoryTurnContract`` / session state.
    This enum remains only so existing ``memory_access_*`` counters stay stable.
    """

    NONE = "none"
    AUTOMATIC = "automatic"
    TOOL = "tool"
    MUTATION = "mutation"


class MemoryRecallPurpose(StrEnum):
    """Planner-owned semantic reason for recalling long-term memory."""

    BACKGROUND = "background"
    RECALL = "recall"
    CONTINUATION = "continuation"
    VERIFY = "verify"
    CORRECT = "correct"


class MemorySubjectRole(StrEnum):
    """Semantic subject hints; these values never grant an identity scope."""

    CURRENT_PERSON = "current_person"
    CURRENT_GROUP = "current_group"
    REFERENCED_PERSON = "referenced_person"
    CURRENT_SELF = "current_self"


class MemoryTemporalIntentMode(StrEnum):
    UNSPECIFIED = "unspecified"
    RECENT = "recent"
    HISTORICAL = "historical"
    RANGE = "range"


class MemoryTemporalConstraint(StrEnum):
    """Whether temporal intent only ranks facts or excludes unverifiable dates."""

    SOFT = "soft"
    STRICT = "strict"


class MemoryRetrievalMode(StrEnum):
    RELEVANT = "relevant"
    OVERVIEW = "overview"


class MemoryTargetRole(StrEnum):
    CURRENT_SELF = "current_self"
    CURRENT_PERSON = "current_person"
    CURRENT_PERSON_GROUP = "current_person_group"
    CURRENT_GROUP = "current_group"
    REFERENCED_PERSON = "referenced_person"
    REFERENCED_PERSON_GROUP = "referenced_person_group"
