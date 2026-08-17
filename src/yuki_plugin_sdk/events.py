"""Typed event names and immutable notification envelopes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field

from yuki_plugin_sdk.models import JsonValue, StrictModel


class EventName(StrEnum):
    APPLICATION_STARTING = "application.starting"
    APPLICATION_STARTED = "application.started"
    APPLICATION_STOPPING = "application.stopping"
    MESSAGE_NORMALIZED = "message.normalized"
    MESSAGE_RECORDED = "message.recorded"
    MESSAGE_OBSERVED = "message.observed"
    MESSAGE_TRIGGERED = "message.triggered"
    TURN_ADMITTED = "turn.admitted"
    TURN_REJECTED = "turn.rejected"
    AUTONOMOUS_DECLINED = "turn.autonomous_declined"
    CAPABILITY_SEARCHED = "capability.searched"
    TURN_CLOSED = "turn.closed"
    CONTEXT_ASSEMBLED = "context.assembled"
    PROMPT_COLLECTING = "prompt.collecting"
    PROMPT_COMPOSED = "prompt.composed"
    AGENT_STARTING = "agent.starting"
    AGENT_TOOL_CALLED = "agent.tool_called"
    AGENT_TOOL_COMPLETED = "agent.tool_completed"
    AGENT_FINISHED = "agent.finished"
    AGENT_INTERRUPTED = "agent.interrupted"
    REPLY_PLANNED = "reply.planned"
    REPLY_GENERATED = "reply.generated"
    REPLY_SENDING = "reply.sending"
    REPLY_SENT = "reply.sent"
    REPLY_CANCELLED = "reply.cancelled"
    REPLY_FAILED = "reply.failed"
    MEMORY_CREATED = "memory.created"
    MEMORY_UPDATED = "memory.updated"
    MEMORY_DELETED = "memory.deleted"
    RELATIONSHIP_CHANGED = "relationship.changed"
    VISION_COMPLETED = "vision.completed"
    VISION_FAILED = "vision.failed"
    WEB_SEARCH_COMPLETED = "web.search_completed"
    WEB_READ_COMPLETED = "web.read_completed"
    AUTOMATION_CREATED = "automation.created"
    AUTOMATION_STARTED = "automation.started"
    AUTOMATION_COMPLETED = "automation.completed"
    AUTOMATION_FAILED = "automation.failed"
    EMOJI_CANDIDATE_COLLECTED = "emoji.candidate_collected"
    EMOJI_ANALYSIS_COMPLETED = "emoji.analysis_completed"
    EMOJI_ADOPTED = "emoji.adopted"
    EMOJI_REJECTED = "emoji.rejected"
    EMOJI_BANNED = "emoji.banned"
    EMOJI_SELECTION_STARTED = "emoji.selection_started"
    EMOJI_SELECTED = "emoji.selected"
    EMOJI_SENT = "emoji.sent"
    EMOJI_COLLECTED = "emoji.collected"
    EMOJI_ANALYZED = "emoji.analyzed"
    EMOJI_UNADOPTED = "emoji.unadopted"
    EMOJI_BEFORE_SELECT = "emoji.before_select"
    EMOJI_AFTER_SELECT = "emoji.after_select"
    EMOJI_QUEUED = "emoji.queued"
    EMOJI_PREPARE_READY = "emoji.prepare_ready"
    EMOJI_PREPARE_NO_CANDIDATE = "emoji.prepare_no_candidate"
    EMOJI_PREPARE_FAILED = "emoji.prepare_failed"
    EMOJI_SEND_ATTEMPTED = "emoji.send_attempted"
    EMOJI_SEND_ACCEPTED = "emoji.send_accepted"
    EMOJI_SEND_FAILED = "emoji.send_failed"
    EMOJI_USAGE_RECORDED = "emoji.usage_recorded"
    EMOJI_USAGE_RECORD_FAILED = "emoji.usage_record_failed"
    EMOJI_FALLBACK_TEXT_SENT = "emoji.fallback_text_sent"
    EMOJI_MISSING = "emoji.missing"
    EMOJI_RESTORED = "emoji.restored"
    SPEECH_WORKER_STARTED = "speech.worker_started"
    SPEECH_WORKER_STOPPED = "speech.worker_stopped"
    SPEECH_PROFILE_LOADED = "speech.profile_loaded"
    SPEECH_PROFILE_FAILED = "speech.profile_failed"
    SPEECH_GENERATION_STARTED = "speech.generation_started"
    SPEECH_GENERATION_COMPLETED = "speech.generation_completed"
    SPEECH_GENERATION_FAILED = "speech.generation_failed"
    SPEECH_GENERATION_CANCELLED = "speech.generation_cancelled"
    SPEECH_QUEUED = "speech.queued"
    SPEECH_SENT = "speech.sent"
    SPEECH_SEND_FAILED = "speech.send_failed"


class EventEnvelope(StrictModel):
    event_id: UUID = Field(default_factory=uuid4)
    name: EventName
    schema_version: int = Field(default=1, ge=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: Mapping[str, JsonValue] = Field(default_factory=dict)


NotificationHandler = Callable[[EventEnvelope], Awaitable[None]]


class HookExecution(StrictModel):
    plugin_id: str
    hook_id: str
    success: bool
    duration_seconds: float = Field(ge=0)
    error_category: str | None = None
