"""Deterministic source projection for conversation history rollup."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.persistence.repository_records import EventRecord


class _SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TerminalToolOutcome(_SourceModel):
    tool: str
    outcome: str
    durable_effect: str
    public_result: str


class TerminalToolOutcomeSource(Protocol):
    """Future adapter over Agent Action / tool receipts. Unused until those fields are audited."""

    def terminal_outcomes_for_range(
        self, start_event_id: int, end_event_id: int
    ) -> tuple[TerminalToolOutcome, ...]: ...


class SourceEventProjection(_SourceModel):
    event_id: int
    occurred_at: datetime
    direction: str
    origin: str
    event_kind: str
    sender_user_id: str
    sender_label: str
    content: str
    visual_summary: str
    reply_to_message_id: str | None
    content_hash: str
    external_untrusted: bool = False


class ConversationSourceSnapshot(_SourceModel):
    state_id: int
    reset_epoch: str
    scope_type: ScopeType
    events: tuple[SourceEventProjection, ...] = ()
    tool_outcomes: tuple[TerminalToolOutcome, ...] = ()

    @property
    def event_ids(self) -> tuple[int, ...]:
        return tuple(item.event_id for item in self.events)


def event_content_hash(row: EventRecord) -> str:
    payload = "\0".join(
        (
            str(row.id),
            row.direction,
            row.event_kind,
            row.origin,
            row.content,
            row.visual_summary,
            row.reply_to_message_id or "",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def project_event(row: EventRecord) -> SourceEventProjection:
    label = row.sender_group_card or row.sender_nickname or row.sender_user_id
    content = row.content.strip()
    visual = row.visual_summary.strip()
    return SourceEventProjection(
        event_id=row.id,
        occurred_at=row.occurred_at,
        direction=row.direction,
        origin=row.origin,
        event_kind=row.event_kind,
        sender_user_id=row.sender_user_id,
        sender_label=label,
        content=content,
        visual_summary=visual,
        reply_to_message_id=row.reply_to_message_id,
        content_hash=event_content_hash(row),
        external_untrusted=row.event_kind == "external_event",
    )


def build_source_snapshot(
    *,
    state_id: int,
    reset_at: datetime | None,
    scope_type: ScopeType,
    events: tuple[EventRecord, ...],
    tool_outcomes: tuple[TerminalToolOutcome, ...] = (),
) -> ConversationSourceSnapshot:
    ordered = tuple(sorted(events, key=lambda row: (row.occurred_at, row.id)))
    if reset_at is not None:
        ordered = tuple(row for row in ordered if row.occurred_at >= reset_at)
    return ConversationSourceSnapshot(
        state_id=state_id,
        reset_epoch="none" if reset_at is None else reset_at.isoformat(),
        scope_type=scope_type,
        events=tuple(project_event(row) for row in ordered),
        tool_outcomes=tool_outcomes,
    )


def source_fingerprint(snapshot: ConversationSourceSnapshot) -> str:
    payload = "\0".join(
        (
            str(snapshot.state_id),
            snapshot.reset_epoch,
            ",".join(str(item.event_id) for item in snapshot.events),
            ",".join(item.content_hash for item in snapshot.events),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extractive_compact(
    snapshot: ConversationSourceSnapshot,
    *,
    max_characters: int,
) -> str:
    if max_characters <= 0:
        raise ValueError("extractive max_characters must be positive")
    lines: list[str] = []
    for item in snapshot.events:
        pieces = [f"#{item.event_id}", item.direction, item.sender_label]
        if item.external_untrusted:
            pieces.append("external_untrusted")
        if item.reply_to_message_id:
            pieces.append(f"reply:{item.reply_to_message_id}")
        header = " ".join(pieces)
        body = item.content
        if item.visual_summary:
            body = f"{body}\n{item.visual_summary}".strip()
        lines.append(f"{header}\n{body}".strip())
    while lines and sum(len(line) + 1 for line in lines) > max_characters:
        lines.pop(0)
    rendered = "\n".join(lines)
    return rendered[:max_characters]


class ExtractiveProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=0)
    fingerprint: str
    event_ids: tuple[int, ...]
