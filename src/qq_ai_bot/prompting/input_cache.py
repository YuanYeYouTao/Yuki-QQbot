"""Append-only prompt `input` reuse across user turns.

DeepSeek Responses caches complete prefix units. Rebuilding the near window
every turn, then inserting a mutating system envelope, makes the next `input`
array a new document. Remember the items actually sent and only append.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from qq_ai_bot.domain.messages import ChatMessage

_PROMPT_INPUT_STATE_LIMIT = 1024


def _fingerprint(message: ChatMessage) -> tuple[str, str]:
    return (message.role, message.content or "")


@dataclass(frozen=True, slots=True)
class PromptInputSnapshot:
    """One conversation's last successfully composed user/assistant `input`."""

    anchor_event_id: int | None
    assembler_history: tuple[ChatMessage, ...]
    current_plain: ChatMessage
    sent_prefix: tuple[ChatMessage, ...]
    current_sent: ChatMessage


class PromptInputCache:
    """Process-local last-input memory, keyed like the history-window anchor."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, PromptInputSnapshot] = OrderedDict()

    def get(self, key: str) -> PromptInputSnapshot | None:
        snapshot = self._items.get(key)
        if snapshot is not None:
            self._items.move_to_end(key)
        return snapshot

    def remember(self, key: str, snapshot: PromptInputSnapshot) -> None:
        if not key:
            return
        self._items[key] = snapshot
        self._items.move_to_end(key)
        while len(self._items) > _PROMPT_INPUT_STATE_LIMIT:
            self._items.popitem(last=False)

    def forget(self, key: str) -> None:
        self._items.pop(key, None)


def splice_appended_input(
    previous: PromptInputSnapshot | None,
    *,
    new_history: tuple[ChatMessage, ...],
    new_current_sent: ChatMessage,
    rolled: bool,
    new_anchor: int | None,
) -> tuple[ChatMessage, ...] | None:
    """Return `sent_prefix + extras + current` when the rebuilt window is a suffix."""

    if previous is None or rolled:
        return None
    if previous.anchor_event_id != new_anchor:
        return None
    old_history = previous.assembler_history
    if len(new_history) < len(old_history):
        return None
    if any(
        _fingerprint(left) != _fingerprint(right)
        for left, right in zip(old_history, new_history, strict=False)
    ):
        return None
    extra = new_history[len(old_history) :]
    if extra:
        if _fingerprint(extra[0]) != _fingerprint(previous.current_plain):
            return None
        extra = extra[1:]
    elif _fingerprint(previous.current_plain) != ("", ""):
        return None
    return (*previous.sent_prefix, previous.current_sent, *extra, new_current_sent)
