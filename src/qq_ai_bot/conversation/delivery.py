"""Reply-sequence specification owned by Conversation Runtime (R4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SplitHint = Literal["auto", "sentence", "paragraph"]


@dataclass(frozen=True, slots=True)
class ReplySequenceSpec:
    """Platform delivery policy for one turn.

    Replaces Planner ``TurnPlan.delivery_mode`` / ``desired_messages``.
    ``max_messages`` is already clamped to ``1..reply.hard_max_messages``.
    The default is "do not predict a message count": ``max_messages`` equals
    the hard cap and ``split_hint='auto'`` lets the existing line splitter work.
    """

    max_messages: int
    split_hint: SplitHint = "auto"
    suppress_text: bool = False


def default_reply_spec(*, hard_max_messages: int) -> ReplySequenceSpec:
    """Return the no-prediction default used when the agent did not set layout."""

    return ReplySequenceSpec(max_messages=max(1, hard_max_messages), split_hint="auto")


@dataclass(slots=True)
class ReplyControlState:
    """Mutable per-turn reply-effect and layout ledger shared with tools."""

    spec: ReplySequenceSpec
    declined: bool = False
    decline_reason: str = ""
    layout_applied: bool = False
    had_effect: bool = False
    voice_request_basis: str = "none"
    text_sent: bool = False
    voice_sent: bool = False
    emoji_sent: bool = False

    def mark_effect(self) -> None:
        self.had_effect = True
