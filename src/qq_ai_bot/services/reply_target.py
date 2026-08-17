"""Stable, conversation-local reply-target control and resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(slots=True)
class ReplyTargetControl:
    """One bounded main-Agent override over the default (no-quote) reply target."""

    visible_event_ids: frozenset[int]
    override_applied: bool = False
    event_id: int | None = None

    def apply(self, event_id: int | None) -> tuple[bool, str]:
        """Apply one valid override; omitting the event clears the quote target."""

        if self.override_applied:
            return False, "reply_target_already_selected"
        if event_id is not None and event_id not in self.visible_event_ids:
            return False, "event_not_visible"
        self.override_applied = True
        self.event_id = event_id
        return True, "selected" if event_id is not None else "cleared"


class ReplyEventRepository(Protocol):
    """The immutable ledger lookup required by reply-target resolution."""

    async def get_event(self, event_id: int) -> EventRecord | None: ...


@dataclass(frozen=True, slots=True)
class ReplyTargetResolution:
    """A local event reference resolved to an internal OneBot message ID."""

    event_id: int
    platform_message_id: str | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.platform_message_id is not None


class ReplyTargetResolver:
    """Resolve one visible local event without allowing cross-conversation quotes."""

    def __init__(self, ledger: ReplyEventRepository) -> None:
        self._ledger = ledger

    async def resolve(
        self,
        event_id: int,
        *,
        inbound: InboundMessage,
    ) -> ReplyTargetResolution:
        event = await self._ledger.get_event(event_id)
        if event is None:
            return ReplyTargetResolution(event_id, None, "event_not_found")
        if event.event_kind != "message":
            return ReplyTargetResolution(event_id, None, "unsupported_event_kind")
        if event.bot_user_id != inbound.bot_user_id:
            return ReplyTargetResolution(event_id, None, "different_bot")
        if event.scope_type is not inbound.scope_type:
            return ReplyTargetResolution(event_id, None, "different_scope")
        if inbound.scope_type is ScopeType.GROUP:
            if not inbound.group_id or event.group_id != inbound.group_id:
                return ReplyTargetResolution(event_id, None, "different_conversation")
        elif event.private_peer_user_id != inbound.sender.user_id:
            return ReplyTargetResolution(event_id, None, "different_conversation")
        if not event.platform_message_id.isdigit():
            return ReplyTargetResolution(event_id, None, "transport_id_unavailable")
        return ReplyTargetResolution(event_id, event.platform_message_id, "resolved")
