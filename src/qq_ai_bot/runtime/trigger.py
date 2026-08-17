"""Turn trigger discriminated union.

A turn is caused by exactly one of four trigger shapes.  ``InboundMessage``
only exists on real message turns; synthetic inbound messages for scheduled /
plugin-background turns are forbidden by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.runtime.errors import InvalidTurnTriggerError
from qq_ai_bot.runtime.origin import TurnOrigin

_MESSAGE_ORIGINS = frozenset({TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP})
_TARGET_TYPES = frozenset({"group", "private"})


@dataclass(frozen=True, slots=True)
class MessageTurnTrigger:
    """A turn caused by one or more real inbound messages.

    Covers direct user-message turns and autonomous group turns (which react
    to the latest observed message).  ``ledger_event_id`` anchors the turn to
    the persisted chat event that admitted it.
    """

    origin: TurnOrigin
    inbound: InboundMessage
    ledger_event_id: int
    profile: UserProfileSnapshot | None = None

    def __post_init__(self) -> None:
        if self.origin not in _MESSAGE_ORIGINS:
            raise InvalidTurnTriggerError(
                f"message trigger origin must be user_message/autonomous_group, got {self.origin}"
            )
        if self.ledger_event_id <= 0:
            raise InvalidTurnTriggerError("message trigger requires a persisted ledger event id")


@dataclass(frozen=True, slots=True)
class ExternalEventTurnTrigger:
    """A plugin-background turn caused by an external event (outbox job)."""

    plugin_id: str
    source_event_id: int
    target_type: str
    target_id: str
    origin: TurnOrigin = field(default=TurnOrigin.PLUGIN_BACKGROUND)

    def __post_init__(self) -> None:
        if self.origin is not TurnOrigin.PLUGIN_BACKGROUND:
            raise InvalidTurnTriggerError("external event trigger origin must be plugin_background")
        if not self.plugin_id:
            raise InvalidTurnTriggerError("external event trigger requires a plugin id")
        if self.target_type not in _TARGET_TYPES:
            raise InvalidTurnTriggerError(
                f"unknown external event target type: {self.target_type!r}"
            )
        if not self.target_id:
            raise InvalidTurnTriggerError("external event trigger requires a target id")


@dataclass(frozen=True, slots=True)
class ScheduledTurnTrigger:
    """A turn caused by a due scheduled automation."""

    automation_id: int
    creator_user_id: str
    scheduled_for: datetime
    origin: TurnOrigin = field(default=TurnOrigin.SCHEDULED_AUTOMATION)

    def __post_init__(self) -> None:
        if self.origin is not TurnOrigin.SCHEDULED_AUTOMATION:
            raise InvalidTurnTriggerError("scheduled trigger origin must be scheduled_automation")
        if self.automation_id <= 0:
            raise InvalidTurnTriggerError("scheduled trigger requires a persisted automation id")
        if not self.creator_user_id:
            raise InvalidTurnTriggerError("scheduled trigger requires the creator user id")


@dataclass(frozen=True, slots=True)
class PluginSessionTurnTrigger:
    """A turn running inside an interactive plugin session."""

    plugin_id: str
    session_id: str
    actor_user_id: str
    origin: TurnOrigin = field(default=TurnOrigin.PLUGIN_SESSION)

    def __post_init__(self) -> None:
        if self.origin is not TurnOrigin.PLUGIN_SESSION:
            raise InvalidTurnTriggerError("plugin session trigger origin must be plugin_session")
        if not self.plugin_id or not self.session_id:
            raise InvalidTurnTriggerError("plugin session trigger requires plugin and session ids")
        if not self.actor_user_id:
            raise InvalidTurnTriggerError("plugin session trigger requires the actor user id")


TurnTrigger = (
    MessageTurnTrigger | ExternalEventTurnTrigger | ScheduledTurnTrigger | PluginSessionTurnTrigger
)
