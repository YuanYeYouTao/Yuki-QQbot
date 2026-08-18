"""SQLAlchemy models owned by the local speech domain."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from qq_ai_bot.persistence.models import Base


class SpeechVoiceProfileModel(Base):
    __tablename__ = "speech_voice_profiles"
    __table_args__ = (
        CheckConstraint("provider = 'genie'", name="ck_speech_profiles_provider"),
        CheckConstraint(
            "engine_model_version IN ('v2', 'v2proplus')",
            name="ck_speech_profiles_model_version",
        ),
        Index(
            "uq_speech_profiles_one_default",
            "is_default",
            unique=True,
            sqlite_where=text("is_default = 1 AND enabled = 1"),
        ),
        Index("ix_speech_profiles_enabled_updated", "enabled", "updated_at"),
    )

    profile_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    engine_model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    supported_languages_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    model_relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    model_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    default_style: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    license_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SpeechVoiceReferenceModel(Base):
    __tablename__ = "speech_voice_references"
    __table_args__ = (
        UniqueConstraint("profile_id", "reference_key", name="uq_speech_references_profile_key"),
        Index("ix_speech_references_profile_enabled", "profile_id", "enabled", "priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("speech_voice_profiles.profile_id", ondelete="CASCADE"), nullable=False
    )
    reference_key: Mapped[str] = mapped_column(String(128), nullable=False)
    style: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    audio_relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    audio_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    transcript: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SpeechGenerationModel(Base):
    __tablename__ = "speech_generations"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_speech_generations_request_id"),
        CheckConstraint("character_count > 0", name="ck_speech_generations_character_count"),
        CheckConstraint(
            "status IN ('queued', 'generating', 'succeeded', 'failed', 'cancelled', "
            "'sent', 'expired')",
            name="ck_speech_generations_status",
        ),
        Index("ix_speech_generations_cache_key", "cache_key"),
        Index("ix_speech_generations_status_created", "status", "created_at"),
        Index("ix_speech_generations_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="SET NULL"), nullable=True
    )
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("speech_voice_profiles.profile_id", ondelete="RESTRICT"), nullable=False
    )
    reference_id: Mapped[int | None] = mapped_column(
        ForeignKey("speech_voice_references.id", ondelete="SET NULL"), nullable=True
    )
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False, default="zh")
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    output_relative_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    output_format: Mapped[str] = mapped_column(String(16), nullable=False, default="wav")
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_milliseconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PersonSpeechPreferenceModel(Base):
    """One enforceable person-level speech preference set via set_voice_preference."""

    __tablename__ = "person_speech_preferences"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('text_only', 'auto', 'prefer_voice')",
            name="ck_person_speech_preferences_mode",
        ),
        Index("ix_person_speech_preferences_updated", "updated_at"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
