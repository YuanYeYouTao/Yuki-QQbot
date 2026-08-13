"""SQLAlchemy schema for the person-centric event ledger and memories."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative metadata root."""


class PersonModel(Base):
    """One human identity permanently keyed by a QQ number string."""

    __tablename__ = "people"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    aliases: Mapped[list[PersonAliasModel]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    memberships: Mapped[list[MembershipModel]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    relationship_state: Mapped[PersonRelationshipModel | None] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    time_setting: Mapped[PersonTimeSettingModel | None] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    automations: Mapped[list[AutomationModel]] = relationship(
        back_populates="creator",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class PersonAliasModel(Base):
    """A nickname or group card previously observed for one QQ identity."""

    __tablename__ = "person_aliases"
    __table_args__ = (
        UniqueConstraint("user_id", "group_scope", "alias", name="uq_person_alias_scope"),
        Index("ix_person_aliases_user_last_seen", "user_id", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    group_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    alias_type: Mapped[str] = mapped_column(String(24), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    person: Mapped[PersonModel] = relationship(back_populates="aliases")


class GroupModel(Base):
    """A QQ group and its observation/participation settings."""

    __tablename__ = "groups"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    require_mention: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    autonomous_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    memberships: Mapped[list[MembershipModel]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MembershipModel(Base):
    """One person as known inside one exact group."""

    __tablename__ = "memberships"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), primary_key=True
    )
    group_card: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    person: Mapped[PersonModel] = relationship(back_populates="memberships")
    group: Mapped[GroupModel] = relationship(back_populates="memberships")


class ChatEventModel(Base):
    """An immutable message or explicitly typed external conversation event."""

    __tablename__ = "chat_events"
    __table_args__ = (
        UniqueConstraint(
            "bot_user_id",
            "platform_message_id",
            name="uq_chat_events_bot_platform_message",
        ),
        Index("ix_chat_events_scope_time", "scope_type", "occurred_at"),
        Index("ix_chat_events_group_time", "group_id", "occurred_at"),
        Index("ix_chat_events_sender_time", "sender_user_id", "occurred_at"),
        Index("ix_chat_events_private_peer_time", "private_peer_user_id", "occurred_at"),
        Index("ix_chat_events_automation", "automation_id", "automation_run_id"),
        Index(
            "uq_chat_events_external_event_target",
            "source_plugin_id",
            "external_event_key",
            "scope_type",
            "external_target_id",
            unique=True,
            sqlite_where=text("event_kind = 'external_event'"),
        ),
        CheckConstraint(
            "(event_kind = 'message' AND source_plugin_id IS NULL "
            "AND external_source IS NULL AND external_event_key IS NULL "
            "AND external_event_type IS NULL AND external_payload_json IS NULL "
            "AND external_target_id IS NULL) OR "
            "(event_kind = 'external_event' AND source_plugin_id IS NOT NULL "
            "AND external_source IS NOT NULL AND external_event_key IS NOT NULL "
            "AND external_event_type IS NOT NULL AND external_payload_json IS NOT NULL "
            "AND external_target_id IS NOT NULL AND origin = 'plugin_background' "
            "AND direction = 'external')",
            name="ck_chat_events_kind_payload",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=True
    )
    private_peer_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=True
    )
    sender_user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    sender_nickname: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=text("''")
    )
    sender_group_card: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=text("''")
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    event_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="message",
        server_default=text("'message'"),
    )
    source_plugin_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_event_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_event_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    visual_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    segments_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reply_to_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user_message")
    automation_id: Mapped[int | None] = mapped_column(
        ForeignKey("automations.id", ondelete="SET NULL"), nullable=True
    )
    automation_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="SET NULL"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MediaAnalysisModel(Base):
    """A short-lived structured visual observation without source image data."""

    __tablename__ = "media_analyses"
    __table_args__ = (
        CheckConstraint(
            "analysis_mode IN ('general', 'meme', 'ocr', 'question')",
            name="ck_media_analyses_analysis_mode",
        ),
        CheckConstraint(
            "segment_index >= 0",
            name="ck_media_analyses_segment_index",
        ),
        UniqueConstraint(
            "content_hash",
            "analysis_mode",
            "question_hash",
            "model",
            "prompt_version",
            name="uq_media_analyses_cache_key",
        ),
        Index("ix_media_analyses_content_hash", "content_hash"),
        Index(
            "ix_media_analyses_source_event_segment",
            "source_event_id",
            "segment_index",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=True
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmojiDescriptionModel(Base):
    """A durable description indexed by a stable QQ emoji identity."""

    __tablename__ = "emoji_descriptions"
    __table_args__ = (
        CheckConstraint(
            "analysis_mode IN ('general', 'meme', 'ocr', 'question')",
            name="ck_emoji_descriptions_analysis_mode",
        ),
        CheckConstraint("hit_count >= 0", name="ck_emoji_descriptions_hit_count"),
        UniqueConstraint(
            "emoji_key",
            "analysis_mode",
            "question_hash",
            "model",
            "prompt_version",
            name="uq_emoji_descriptions_lookup",
        ),
        Index("ix_emoji_descriptions_key", "emoji_key"),
        Index("ix_emoji_descriptions_last_used", "last_used_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    emoji_key: Mapped[str] = mapped_column(String(255), nullable=False)
    analysis_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    observation_json: Mapped[str] = mapped_column(Text, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryFactModel(Base):
    """A versioned fact with a backend-owned person/group scope."""

    __tablename__ = "memory_facts"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('person', 'person_group', 'group', 'self')",
            name="ck_memory_facts_scope_type",
        ),
        CheckConstraint(
            "kind IN ('fact', 'preference', 'episode')",
            name="ck_memory_facts_kind",
        ),
        CheckConstraint(
            "source_type IN ('automatic', 'explicit', 'rebuild')",
            name="ck_memory_facts_source_type",
        ),
        CheckConstraint(
            "status IN ('active', 'contested', 'superseded', 'invalidated')",
            name="ck_memory_facts_status",
        ),
        CheckConstraint(
            "review_state IN ('legacy_unreviewed', 'verified', 'quarantined')",
            name="ck_memory_facts_review_state",
        ),
        CheckConstraint(
            "authority IN ('explicit', 'self_report', 'group_report', 'third_party', "
            "'agent_reflection')",
            name="ck_memory_facts_authority",
        ),
        CheckConstraint(
            "authority != 'agent_reflection' OR scope_type = 'self'",
            name="ck_memory_facts_agent_reflection_scope",
        ),
        CheckConstraint(
            "conflict_state IN ('clear', 'contested')",
            name="ck_memory_facts_conflict_state",
        ),
        CheckConstraint(
            "status != 'contested' OR conflict_state = 'contested'",
            name="ck_memory_facts_contested_state",
        ),
        CheckConstraint(
            "(status = 'invalidated' AND invalidated_reason IS NOT NULL) OR "
            "(status != 'invalidated' AND invalidated_reason IS NULL)",
            name="ck_memory_facts_invalidation_reason",
        ),
        CheckConstraint("importance BETWEEN 1 AND 5", name="ck_memory_facts_importance"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_memory_facts_confidence"),
        CheckConstraint(
            "(scope_type = 'person' AND subject_user_id IS NOT NULL AND group_id IS NULL) OR "
            "(scope_type = 'person_group' AND subject_user_id IS NOT NULL "
            "AND group_id IS NOT NULL) OR "
            "(scope_type = 'group' AND subject_user_id IS NULL AND group_id IS NOT NULL) OR "
            "(scope_type = 'self' AND subject_user_id IS NULL AND group_id IS NULL)",
            name="ck_memory_facts_scope_identity",
        ),
        CheckConstraint(
            "(scope_type != 'self' AND visibility_type IS NULL AND "
            "visibility_user_id IS NULL AND visibility_group_id IS NULL) OR "
            "(scope_type = 'self' AND ("
            "(visibility_type = 'global' AND visibility_user_id IS NULL AND "
            "visibility_group_id IS NULL) OR "
            "(visibility_type = 'private' AND visibility_user_id IS NOT NULL AND "
            "visibility_group_id IS NULL) OR "
            "(visibility_type = 'group' AND visibility_user_id IS NULL AND "
            "visibility_group_id IS NOT NULL)))",
            name="ck_memory_facts_self_visibility",
        ),
        Index(
            "uq_memory_facts_active_person_key",
            "subject_user_id",
            "kind",
            "memory_key",
            unique=True,
            sqlite_where=text("status = 'active' AND scope_type = 'person'"),
        ),
        Index(
            "uq_memory_facts_active_person_group_key",
            "subject_user_id",
            "group_id",
            "kind",
            "memory_key",
            unique=True,
            sqlite_where=text("status = 'active' AND scope_type = 'person_group'"),
        ),
        Index(
            "uq_memory_facts_active_group_key",
            "group_id",
            "kind",
            "memory_key",
            unique=True,
            sqlite_where=text("status = 'active' AND scope_type = 'group'"),
        ),
        Index(
            "uq_memory_facts_active_self_key",
            "memory_key",
            "visibility_type",
            text("COALESCE(visibility_user_id, '')"),
            text("COALESCE(visibility_group_id, '')"),
            unique=True,
            sqlite_where=text("status = 'active' AND scope_type = 'self'"),
        ),
        Index(
            "ix_memory_facts_scope_status_updated",
            "scope_type",
            "subject_user_id",
            "group_id",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=True
    )
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=True
    )
    visibility_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    visibility_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visibility_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    authority: Mapped[str] = mapped_column(
        String(16), nullable=False, default="self_report", server_default="self_report"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    conflict_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="clear", server_default="clear"
    )
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="SET NULL"), nullable=True
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    invalidated_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validation_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="memory-v2-quality-v1", server_default="legacy"
    )
    last_audited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_state: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="verified",
        server_default="legacy_unreviewed",
    )


class MemoryEvidenceModel(Base):
    """One immutable chat event or trusted tool receipt supporting a fact."""

    __tablename__ = "memory_evidence"
    __table_args__ = (
        UniqueConstraint("fact_id", "event_id", name="uq_memory_evidence_fact_event"),
        UniqueConstraint("fact_id", "tool_receipt_id", name="uq_memory_evidence_fact_tool_receipt"),
        CheckConstraint(
            "(event_id IS NOT NULL AND tool_receipt_id IS NULL) OR "
            "(event_id IS NULL AND tool_receipt_id IS NOT NULL)",
            name="ck_memory_evidence_source",
        ),
        CheckConstraint(
            "relation IN ('self_statement', 'group_statement', 'third_party_statement', "
            "'explicit_command', 'confirmation', 'correction', 'retraction', 'rebuild', "
            "'agent_reflection')",
            name="ck_memory_evidence_relation",
        ),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_memory_evidence_confidence"),
        CheckConstraint(
            "authority IN ('explicit', 'self_report', 'group_report', 'third_party', "
            "'agent_reflection')",
            name="ck_memory_evidence_authority",
        ),
        Index("ix_memory_evidence_fact", "fact_id"),
        Index("ix_memory_evidence_event", "event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fact_id: Mapped[int] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=True
    )
    tool_receipt_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_tool_receipts.id", ondelete="CASCADE"), nullable=True
    )
    source_speaker_user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    relation: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1.0"
    )
    authority: Mapped[str] = mapped_column(
        String(16), nullable=False, default="self_report", server_default="self_report"
    )
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryFactRelationModel(Base):
    """A directed, immutable semantic relationship between same-target facts."""

    __tablename__ = "memory_fact_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_fact_id",
            "target_fact_id",
            "relation_type",
            name="uq_memory_fact_relations_pair_type",
        ),
        CheckConstraint(
            "source_fact_id != target_fact_id",
            name="ck_memory_fact_relations_distinct",
        ),
        CheckConstraint(
            "relation_type IN ('supports', 'contradicts', 'refines', 'equivalent')",
            name="ck_memory_fact_relations_type",
        ),
        CheckConstraint(
            "confidence BETWEEN 0 AND 1",
            name="ck_memory_fact_relations_confidence",
        ),
        Index("ix_memory_fact_relations_source", "source_fact_id"),
        Index("ix_memory_fact_relations_target", "target_fact_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_fact_id: Mapped[int] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="CASCADE"), nullable=False
    )
    target_fact_id: Mapped[int] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryFactStateEventModel(Base):
    """Content-free audit record for one fact state transition."""

    __tablename__ = "memory_fact_state_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('created', 'confirmed', 'superseded', 'contested', "
            "'conflict_cleared', 'invalidated', 'restored', 'merged', 'expired', "
            "'stale_invalidated')",
            name="ck_memory_fact_state_events_action",
        ),
        Index("ix_memory_fact_state_events_fact_created", "fact_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fact_id: Mapped[int] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    from_conflict_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_conflict_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="SET NULL"), nullable=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryMutationReceiptModel(Base):
    """One atomic result and provenance record for a Memory V2 mutation."""

    __tablename__ = "memory_mutation_receipts"
    __table_args__ = (
        UniqueConstraint("mutation_id", name="uq_memory_mutation_receipts_mutation_id"),
        UniqueConstraint("idempotency_key", name="uq_memory_mutation_receipts_idempotency"),
        UniqueConstraint("claim_fingerprint", name="uq_memory_mutation_receipts_claim"),
        CheckConstraint(
            "decision_actor_type IN "
            "('agent','worker','command','admin','plugin','reflection','system')",
            name="ck_memory_mutation_decision_actor_type",
        ),
        CheckConstraint(
            "requested_operation IN "
            "('create','correct','invalidate','restore','contest','merge','reassign',"
            "'update_metadata')",
            name="ck_memory_mutation_requested_operation",
        ),
        CheckConstraint(
            "applied_operation IN "
            "('create','correct','invalidate','restore','contest','merge','reassign',"
            "'update_metadata','merge_evidence','noop')",
            name="ck_memory_mutation_applied_operation",
        ),
        CheckConstraint(
            "outcome IN "
            "('processing','committed','committed_as_contested','deduplicated',"
            "'no_change','rejected')",
            name="ck_memory_mutation_outcome",
        ),
        CheckConstraint(
            "(trigger_source_type = 'chat_event' AND trigger_event_id IS NOT NULL "
            "AND dream_operation_id IS NULL) OR "
            "(trigger_source_type = 'dream_operation' AND trigger_event_id IS NULL "
            "AND dream_operation_id IS NOT NULL)",
            name="ck_memory_mutation_trigger_source",
        ),
        Index(
            "ix_memory_mutation_receipts_event_created",
            "trigger_event_id",
            "created_at",
        ),
        Index(
            "ix_memory_mutation_receipts_target_created",
            "target_fingerprint",
            "created_at",
        ),
        Index("ix_memory_mutation_receipts_old_fact", "old_fact_id"),
        Index("ix_memory_mutation_receipts_new_fact", "new_fact_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mutation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_source_type: Mapped[str] = mapped_column(
        String(24), nullable=False, default="chat_event", server_default="chat_event"
    )
    trigger_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=True
    )
    dream_operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_dream_operations.id", ondelete="CASCADE"), nullable=True
    )
    conversation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    current_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=True
    )
    turn_origin: Mapped[str] = mapped_column(String(32), nullable=False)
    delegation_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_actor_user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    decision_actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executed_by_bot_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="SET NULL"), nullable=True
    )
    requested_operation: Mapped[str] = mapped_column(String(24), nullable=False)
    applied_operation: Mapped[str] = mapped_column(String(24), nullable=False)
    old_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="SET NULL"), nullable=True
    )
    new_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="SET NULL"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryClaimCandidateModel(Base):
    """A short-lived claim that is deliberately excluded from normal retrieval."""

    __tablename__ = "memory_claim_candidates"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_memory_claim_candidates_fingerprint"),
        CheckConstraint(
            "candidate_type IN ('memory','self')",
            name="ck_memory_claim_candidates_type",
        ),
        CheckConstraint(
            "status IN ('pending','accepted','rejected','expired')",
            name="ck_memory_claim_candidates_status",
        ),
        CheckConstraint("evidence_count >= 1", name="ck_memory_claim_candidates_evidence"),
        Index("ix_memory_claim_candidates_status_expiry", "status", "expires_at"),
        Index("ix_memory_claim_candidates_target", "target_fingerprint", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    subject_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    retention: Mapped[str] = mapped_column(String(32), nullable=False)
    source_style: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryClaimCandidateEvidenceModel(Base):
    __tablename__ = "memory_claim_candidate_evidence"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "event_id",
            name="uq_memory_claim_candidate_evidence",
        ),
        Index("ix_memory_claim_candidate_evidence_event", "event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("memory_claim_candidates.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryToolReceiptModel(Base):
    """A bounded, redacted result that SELF reflection may cite as evidence."""

    __tablename__ = "memory_tool_receipts"
    __table_args__ = (
        CheckConstraint("result_characters >= 0", name="ck_memory_tool_receipts_size"),
        Index(
            "ix_memory_tool_receipts_conversation_created",
            "conversation_key_hash",
            "created_at",
        ),
        Index("ix_memory_tool_receipts_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=False
    )
    bot_user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    result_characters: Mapped[int] = mapped_column(Integer, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemorySelfReflectionStateModel(Base):
    """Persistent per-conversation cursor; no chat body is duplicated here."""

    __tablename__ = "memory_self_reflection_states"
    __table_args__ = (
        UniqueConstraint(
            "conversation_key_hash",
            "bot_user_id",
            name="uq_memory_self_reflection_state_key_bot",
        ),
        CheckConstraint("scope_type IN ('private','group')", name="ck_self_reflection_state_scope"),
        CheckConstraint(
            "pending_events >= 0 AND pending_characters >= 0",
            name="ck_self_reflection_state_pending",
        ),
        Index("ix_memory_self_reflection_state_pending", "pending_since", "last_event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    private_peer_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    latest_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_characters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    has_yuki_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_tool_result: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    high_value_signal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemorySelfReflectionRuntimeModel(Base):
    """Singleton scan cursor initialized at deployment to avoid historical reflection."""

    __tablename__ = "memory_self_reflection_runtime"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_scanned_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemorySelfReflectionRunModel(Base):
    """Content-free scheduled invocation record used for limits and idempotency."""

    __tablename__ = "memory_self_reflection_runs"
    __table_args__ = (
        UniqueConstraint(
            "conversation_key_hash",
            "bot_user_id",
            "scheduled_slot",
            name="uq_self_reflection_run_slot_bot",
        ),
        CheckConstraint(
            "status IN ('processing','completed','failed')",
            name="ck_self_reflection_run_status",
        ),
        Index("ix_memory_self_reflection_runs_slot", "scheduled_slot", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduled_slot: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    first_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    last_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    proposal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    committed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryReflectionJobModel(Base):
    """One restart-safe bounded governance task over existing memory evidence."""

    __tablename__ = "memory_reflection_jobs"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_memory_reflection_jobs_fingerprint"),
        CheckConstraint(
            "issue_type IN ('duplicate','contested','attribution')",
            name="ck_memory_reflection_jobs_issue_type",
        ),
        CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_memory_reflection_jobs_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts BETWEEN 1 AND 20",
            name="ck_memory_reflection_jobs_attempts",
        ),
        Index("ix_memory_reflection_jobs_status_next", "status", "next_attempt_at"),
        Index("ix_memory_reflection_jobs_fact_issue", "fact_id", "issue_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_type: Mapped[str] = mapped_column(String(24), nullable=False)
    fact_id: Mapped[int] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="CASCADE"), nullable=False
    )
    related_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryRebuildRunModel(Base):
    """Administrator-created immutable historical rebuild snapshot."""

    __tablename__ = "memory_rebuild_runs"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_memory_rebuild_runs_public_id"),
        CheckConstraint("snapshot_max_event_id >= 0", name="ck_memory_rebuild_snapshot"),
        CheckConstraint(
            "extraction_requests >= 0 AND consolidation_requests >= 0 "
            "AND input_tokens >= 0 AND output_tokens >= 0 AND latency_milliseconds >= 0",
            name="ck_memory_rebuild_usage_nonnegative",
        ),
        CheckConstraint(
            "status IN ('planned','extracting','extraction_paused','review','committing',"
            "'commit_paused','completed','cancelled','failed')",
            name="ck_memory_rebuild_run_status",
        ),
        Index("ix_memory_rebuild_runs_status_created", "status", "created_at"),
        Index("ix_memory_rebuild_runs_created_by", "created_by_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    selection_json: Mapped[str] = mapped_column(Text, nullable=False)
    selection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_max_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scan_checkpoint_occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scan_checkpoint_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commit_checkpoint_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commit_checkpoint_claim_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="SET NULL"), nullable=True
    )
    extraction_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_statistics_json: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consolidation_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_milliseconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    commit_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryRebuildItemModel(Base):
    """Per-event extraction state inside one rebuild run."""

    __tablename__ = "memory_rebuild_items"
    __table_args__ = (
        UniqueConstraint("run_id", "event_id", name="uq_memory_rebuild_items_run_event"),
        CheckConstraint("attempts >= 0", name="ck_memory_rebuild_items_attempts"),
        CheckConstraint("claim_count >= 0", name="ck_memory_rebuild_items_claim_count"),
        CheckConstraint(
            "status IN ('pending','extracting','staged','no_claims',"
            "'skipped','failed','committed')",
            name="ck_memory_rebuild_item_status",
        ),
        Index("ix_memory_rebuild_items_run_status_event", "run_id", "status", "event_id"),
        Index("ix_memory_rebuild_items_event", "event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("memory_rebuild_runs.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryRebuildProposalModel(Base):
    """Reviewed canonical claim staged before historical commit."""

    __tablename__ = "memory_rebuild_proposals"
    __table_args__ = (
        UniqueConstraint("item_id", "claim_index", name="uq_memory_rebuild_proposal_claim"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_memory_rebuild_confidence"),
        CheckConstraint(
            "review_status IN ('pending','approved','rejected')",
            name="ck_memory_rebuild_review_status",
        ),
        CheckConstraint(
            "commit_status IN ('pending','committed','skipped','failed')",
            name="ck_memory_rebuild_commit_status",
        ),
        Index("ix_memory_rebuild_proposals_run_review", "run_id", "review_status"),
        Index("ix_memory_rebuild_proposals_run_commit", "run_id", "commit_status"),
        Index("ix_memory_rebuild_proposals_subject", "subject_user_id"),
        Index("ix_memory_rebuild_proposals_group", "group_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("memory_rebuild_runs.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("memory_rebuild_items.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=False
    )
    claim_index: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_json: Mapped[str] = mapped_column(Text, nullable=False)
    claim_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=True
    )
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=True
    )
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    authority: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    commit_status: Mapped[str] = mapped_column(String(16), nullable=False)
    actual_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="SET NULL"), nullable=True
    )
    actual_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actual_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="SET NULL"), nullable=True
    )
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryJobModel(Base):
    """A restart-safe Memory V2 job for exactly one inbound event."""

    __tablename__ = "memory_jobs"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_memory_jobs_event"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')",
            name="ck_memory_jobs_status",
        ),
        CheckConstraint(
            "processing_source IN ('live', 'rebuild')",
            name="ck_memory_jobs_processing_source",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('claims_applied', 'candidates_staged', 'no_claims', 'all_rejected', "
            "'already_processed')",
            name="ck_memory_jobs_outcome",
        ),
        Index("ix_memory_jobs_status_next", "status", "next_attempt_at"),
        Index("ix_memory_jobs_conversation", "conversation_key", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=False
    )
    conversation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processing_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="live", server_default="live"
    )
    rebuild_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("memory_rebuild_runs.id", ondelete="SET NULL"), nullable=True
    )
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryEmbeddingProfileModel(Base):
    """Immutable, non-secret identity for one embedding representation."""

    __tablename__ = "memory_embedding_profiles"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_memory_embedding_profiles_fingerprint"),
        CheckConstraint("dimensions > 0", name="ck_memory_embedding_profiles_dimensions"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    output_type: Mapped[str] = mapped_column(String(16), nullable=False)
    document_template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    endpoint_identity: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryEmbeddingModel(Base):
    """Rebuildable float32 vector for one fact and one immutable profile."""

    __tablename__ = "memory_embeddings"
    __table_args__ = (
        UniqueConstraint("fact_id", "profile_id", name="uq_memory_embeddings_fact_profile"),
        CheckConstraint(
            "length(content_hash) = 64", name="ck_memory_embeddings_content_hash_length"
        ),
        CheckConstraint("length(vector_blob) > 0", name="ck_memory_embeddings_vector_nonempty"),
        Index("ix_memory_embeddings_profile_fact", "profile_id", "fact_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fact_id: Mapped[int] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("memory_embedding_profiles.id", ondelete="CASCADE"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vector_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryEmbeddingJobModel(Base):
    """Persistent document-indexing work without fact text or provider payloads."""

    __tablename__ = "memory_embedding_jobs"
    __table_args__ = (
        UniqueConstraint("fact_id", "profile_id", name="uq_memory_embedding_jobs_fact_profile"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')",
            name="ck_memory_embedding_jobs_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_memory_embedding_jobs_attempts"),
        CheckConstraint(
            "length(content_hash) = 64", name="ck_memory_embedding_jobs_content_hash_length"
        ),
        Index("ix_memory_embedding_jobs_status_next", "status", "next_attempt_at"),
        Index("ix_memory_embedding_jobs_profile_status", "profile_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fact_id: Mapped[int] = mapped_column(
        ForeignKey("memory_facts.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("memory_embedding_profiles.id", ondelete="CASCADE"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PersonRelationshipModel(Base):
    """Persistent affection and trust scores for one QQ identity."""

    __tablename__ = "person_relationships"
    __table_args__ = (
        CheckConstraint(
            "affection_score >= 0 AND affection_score <= 100",
            name="ck_person_relationships_affection_range",
        ),
        CheckConstraint(
            "trust_score >= 0 AND trust_score <= 100",
            name="ck_person_relationships_trust_range",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), primary_key=True
    )
    affection_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    trust_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_automatic_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    person: Mapped[PersonModel] = relationship(back_populates="relationship_state")


class RelationshipEventModel(Base):
    """Auditable automatic or administrator-issued relationship change."""

    __tablename__ = "relationship_events"
    __table_args__ = (
        CheckConstraint(
            "change_type IN ('automatic', 'manual')",
            name="ck_relationship_events_change_type",
        ),
        Index("ix_relationship_events_user_created", "user_id", "created_at"),
        Index(
            "uq_relationship_events_automatic_source",
            "source_event_id",
            unique=True,
            sqlite_where=text("source_event_id IS NOT NULL AND change_type = 'automatic'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="SET NULL"), nullable=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    affection_before: Mapped[int] = mapped_column(Integer, nullable=False)
    affection_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    affection_after: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_before: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RelationshipJobModel(Base):
    """Persistent restart-safe relationship evaluation job."""

    __tablename__ = "relationship_jobs"
    __table_args__ = (
        UniqueConstraint("trigger_event_id", name="uq_relationship_jobs_trigger_event"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_relationship_jobs_status",
        ),
        Index("ix_relationship_jobs_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger_event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    conversation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContextResetModel(Base):
    """A context cutoff that preserves the permanent event ledger."""

    __tablename__ = "context_resets"

    context_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=True
    )
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProcessedEventModel(Base):
    """Durable idempotency record for incoming OneBot events."""

    __tablename__ = "processed_events"
    __table_args__ = (Index("ix_processed_events_expires_at", "expires_at"),)

    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentActionModel(Base):
    """A bounded audit entry for a model-issued OneBot action."""

    __tablename__ = "agent_actions"
    __table_args__ = (Index("ix_agent_actions_actor_created", "actor_user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeConfigOverrideModel(Base):
    """One validated runtime configuration override at an exact scope."""

    __tablename__ = "runtime_config_overrides"
    __table_args__ = (
        UniqueConstraint(
            "config_key",
            "scope_type",
            "scope_id",
            name="uq_runtime_config_override_scope",
        ),
        CheckConstraint(
            "scope_type IN ('global', 'group', 'user')",
            name="ck_runtime_config_overrides_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'global' AND scope_id = '') OR "
            "(scope_type IN ('group', 'user') AND scope_id <> '')",
            name="ck_runtime_config_overrides_scope_id",
        ),
        CheckConstraint(
            "value_type IN ('string', 'integer', 'number', 'boolean', 'enum')",
            name="ck_runtime_config_overrides_value_type",
        ),
        CheckConstraint(
            "apply_mode IN ('hot', 'future_only', 'restart_required')",
            name="ck_runtime_config_overrides_apply_mode",
        ),
        CheckConstraint("version >= 1", name="ck_runtime_config_overrides_version"),
        Index(
            "ix_runtime_config_overrides_scope_key",
            "scope_type",
            "scope_id",
            "config_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_key: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    apply_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)


class AdminOperationEventModel(Base):
    """A redacted, append-only audit event for administrator capabilities."""

    __tablename__ = "admin_operation_events"
    __table_args__ = (
        CheckConstraint(
            "duration_seconds >= 0",
            name="ck_admin_operation_events_duration",
        ),
        Index(
            "ix_admin_operation_events_actor_created",
            "actor_user_id",
            "created_at",
        ),
        Index(
            "ix_admin_operation_events_target_created",
            "target_type",
            "target_id",
            "created_at",
        ),
        Index(
            "ix_admin_operation_events_capability_created",
            "capability",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_message_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    conversation_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    before_json: Mapped[str] = mapped_column(Text, nullable=False, default="null")
    after_json: Mapped[str] = mapped_column(Text, nullable=False, default="null")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WebSearchRunModel(Base):
    """One successful Agent web tool call in an isolated conversation."""

    __tablename__ = "web_search_runs"
    __table_args__ = (
        Index(
            "ix_web_search_runs_conversation_created",
            "conversation_key",
            "created_at",
        ),
        Index(
            "ix_web_search_runs_conversation_trigger",
            "conversation_key",
            "trigger_message_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    partial_failure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    sources: Mapped[list[WebSearchSourceModel]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WebSearchSourceModel(Base):
    """Display-safe metadata for one real source used by a web tool."""

    __tablename__ = "web_search_sources"
    __table_args__ = (
        UniqueConstraint("run_id", "url", name="uq_web_search_sources_run_url"),
        Index("ix_web_search_sources_run_ordinal", "run_id", "ordinal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("web_search_runs.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    run: Mapped[WebSearchRunModel] = relationship(back_populates="sources")


class PersonTimeSettingModel(Base):
    """The preferred IANA timezone for one globally identified person."""

    __tablename__ = "person_time_settings"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), primary_key=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    person: Mapped[PersonModel] = relationship(back_populates="time_setting")


class AutomationModel(Base):
    """A validated, persistent declaration of one scheduled automation."""

    __tablename__ = "automations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'cancelled', 'failed', 'blocked')",
            name="ck_automations_status",
        ),
        CheckConstraint("run_count >= 0", name="ck_automations_run_count"),
        CheckConstraint("consecutive_failures >= 0", name="ck_automations_consecutive_failures"),
        Index("ix_automations_status_next", "status", "next_run_at"),
        Index("ix_automations_creator_updated", "creator_user_id", "updated_at"),
        Index("ix_automations_claim", "claimed_until", "claimed_by"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    creator_user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    schedule_json: Mapped[str] = mapped_column(Text, nullable=False)
    script_json: Mapped[str] = mapped_column(Text, nullable=False)
    script_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    required_capabilities_json: Mapped[str] = mapped_column(Text, nullable=False)
    authority_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_from_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1800)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    creator: Mapped[PersonModel] = relationship(back_populates="automations")
    versions: Mapped[list[AutomationVersionModel]] = relationship(
        back_populates="automation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    runs: Mapped[list[AutomationRunModel]] = relationship(
        back_populates="automation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AutomationVersionModel(Base):
    """An immutable script revision for an automation."""

    __tablename__ = "automation_versions"
    __table_args__ = (
        UniqueConstraint("automation_id", "version", name="uq_automation_versions_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    script_json: Mapped[str] = mapped_column(Text, nullable=False)
    script_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    automation: Mapped[AutomationModel] = relationship(back_populates="versions")


class AutomationRunModel(Base):
    """One idempotent scheduled or manual execution attempt."""

    __tablename__ = "automation_runs"
    __table_args__ = (
        UniqueConstraint("automation_id", "scheduled_for", name="uq_automation_runs_scheduled_for"),
        UniqueConstraint("idempotency_key", name="uq_automation_runs_idempotency_key"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'skipped', 'missed', "
            "'uncertain', 'blocked')",
            name="ck_automation_runs_status",
        ),
        Index("ix_automation_runs_automation_created", "automation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    steps_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    automation: Mapped[AutomationModel] = relationship(back_populates="runs")
    step_runs: Mapped[list[AutomationStepRunModel]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AutomationStepRunModel(Base):
    """A redacted audit record for one executed DSL step."""

    __tablename__ = "automation_step_runs"
    __table_args__ = (Index("ix_automation_step_runs_run_step", "run_id", "step_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output_summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    run: Mapped[AutomationRunModel] = relationship(back_populates="step_runs")


class MCPServerStateModel(Base):
    """Secret-free lifecycle metadata for one configured MCP server."""

    __tablename__ = "mcp_server_states"

    server_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    server_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    server_version: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    server_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MCPToolCacheModel(Base):
    """Cached MCP tools/list metadata; never stores credentials or results."""

    __tablename__ = "mcp_tool_cache"
    __table_args__ = (
        UniqueConstraint("server_id", "remote_tool_name", name="uq_mcp_tool_cache_server_tool"),
        Index("ix_mcp_tool_cache_server", "server_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    compact_description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    input_schema_json: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    annotations_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    metadata_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolArtifactModel(Base):
    """Handle metadata for an oversized tool result stored outside SQLite."""

    __tablename__ = "tool_artifacts"
    __table_args__ = (Index("ix_tool_artifacts_expires", "expires_at"),)

    handle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolInvocationModel(Base):
    """Content-free audit metrics for all provider-neutral tool executions."""

    __tablename__ = "tool_invocations"
    __table_args__ = (
        CheckConstraint("latency_seconds >= 0", name="ck_tool_invocations_latency"),
        CheckConstraint("result_size >= 0", name="ck_tool_invocations_result_size"),
        Index("ix_tool_invocations_provider_created", "provider_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    result_size: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_created: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Source-compatibility aliases for integrations that only inspect the old profile types.
UserProfileModel = PersonModel
UserGroupProfileModel = MembershipModel
