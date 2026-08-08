"""Bounded per-conversation epoch state for append-stable short references."""

from __future__ import annotations

import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from qq_ai_bot.domain.messages import InboundMessage, sanitize_display_name
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.references.models import (
    GroupReference,
    MessageReference,
    ReferenceProvenance,
    TurnReferenceRegistry,
    UserReference,
)

_EXPLICIT_IDENTIFIER = re.compile(r"(?<![0-9])([1-9][0-9]{4,19})(?![0-9])")
_LITERAL_REFERENCE_TOKEN = re.compile(r"(?<![A-Za-z0-9_])(?:u|q|g|m)[1-9][0-9]*(?![A-Za-z0-9_])")
_MUTABLE_USER_SOURCES = frozenset(
    {
        ReferenceProvenance.CURRENT_MENTION,
        ReferenceProvenance.CURRENT_REPLY,
        ReferenceProvenance.EXPLICIT_CURRENT_MESSAGE,
        ReferenceProvenance.SYSTEM,
    }
)


@dataclass(slots=True)
class _EpochState:
    epoch_id: str
    anchor_event_id: int | None
    reset_marker: str
    scope_marker: str
    user_refs: dict[str, str] = field(default_factory=dict)
    user_labels: dict[str, str] = field(default_factory=dict)
    message_refs: dict[int, str] = field(default_factory=dict)
    message_ids: dict[int, str] = field(default_factory=dict)
    message_senders: dict[int, str] = field(default_factory=dict)
    next_user: int = 1
    next_message: int = 1
    stale_refs: frozenset[str] = frozenset()


class ReferenceEpochManager:
    """Keep references stable until the history window itself rolls."""

    def __init__(self, *, maximum_states: int = 1024) -> None:
        if maximum_states <= 0:
            raise ValueError("maximum_states must be positive")
        self._maximum_states = maximum_states
        self._states: OrderedDict[str, _EpochState] = OrderedDict()

    def prepare(
        self,
        *,
        conversation_key: str,
        events: tuple[EventRecord, ...],
        inbound: InboundMessage,
        current_event_id: int,
        anchor_event_id: int | None,
        reset_marker: str,
        force_roll: bool = False,
    ) -> TurnReferenceRegistry:
        scope_marker = f"{inbound.scope_type.value}:{inbound.group_id or inbound.sender.user_id}"
        state = self._states.get(conversation_key)
        should_reset = bool(
            force_roll
            or state is None
            or state.reset_marker != reset_marker
            or state.scope_marker != scope_marker
            or (
                anchor_event_id is not None
                and state.anchor_event_id is not None
                and anchor_event_id > state.anchor_event_id
            )
        )
        rolled = state is not None and should_reset
        if should_reset:
            stale = (
                frozenset((*state.user_refs.values(), *state.message_refs.values()))
                if state is not None
                else frozenset()
            )
            state = _EpochState(
                epoch_id=uuid.uuid4().hex[:12],
                anchor_event_id=anchor_event_id,
                reset_marker=reset_marker,
                scope_marker=scope_marker,
                stale_refs=stale,
            )
            self._states[conversation_key] = state
        assert state is not None
        if state.anchor_event_id is None and anchor_event_id is not None:
            state.anchor_event_id = anchor_event_id
        self._register_events(state, events, inbound.bot_user_id)
        registry = self._snapshot(state, events, inbound, current_event_id, rolled=rolled)
        self._states.move_to_end(conversation_key)
        while len(self._states) > self._maximum_states:
            self._states.popitem(last=False)
        return registry

    def _register_events(
        self,
        state: _EpochState,
        events: tuple[EventRecord, ...],
        bot_user_id: str,
    ) -> None:
        for event in events:
            if event.event_kind != "external_event" and event.sender_user_id != bot_user_id:
                self._ensure_user(state, event.sender_user_id, event.sender_display_name)
            if event.id not in state.message_refs:
                state.message_refs[event.id] = f"m{state.next_message}"
                state.next_message += 1
            state.message_ids[event.id] = event.platform_message_id
            state.message_senders[event.id] = event.sender_user_id

    def _snapshot(
        self,
        state: _EpochState,
        events: tuple[EventRecord, ...],
        inbound: InboundMessage,
        current_event_id: int,
        *,
        rolled: bool,
    ) -> TurnReferenceRegistry:
        current_mentions = set(inbound.mentioned_user_ids)
        current_reply = inbound.reply_sender_user_id or ""
        self._ensure_user(
            state,
            inbound.sender.user_id,
            inbound.sender.group_card or inbound.sender.nickname or inbound.sender.user_id,
        )
        for user_id in current_mentions:
            if user_id != inbound.bot_user_id:
                self._ensure_user(state, user_id, self._event_display_name(events, user_id))
        if current_reply and current_reply != inbound.bot_user_id:
            self._ensure_user(state, current_reply, self._event_display_name(events, current_reply))

        users: list[UserReference] = []
        for user_id, ref in state.user_refs.items():
            if user_id == inbound.sender.user_id:
                source = ReferenceProvenance.CURRENT_SENDER
            elif user_id in current_mentions:
                source = ReferenceProvenance.CURRENT_MENTION
            elif user_id == current_reply:
                source = ReferenceProvenance.CURRENT_REPLY
            else:
                source = ReferenceProvenance.HISTORY
            users.append(
                UserReference(
                    ref=ref,
                    user_id=user_id,
                    display_label=state.user_labels[user_id],
                    provenance=source,
                    group_id=inbound.group_id,
                    visible=True,
                    mutable_target=source in _MUTABLE_USER_SOURCES,
                    current_group_member=(
                        True
                        if inbound.group_id is not None
                        and source
                        in {
                            ReferenceProvenance.CURRENT_SENDER,
                            ReferenceProvenance.CURRENT_MENTION,
                            ReferenceProvenance.CURRENT_REPLY,
                        }
                        else None
                    ),
                )
            )

        explicit_ids = tuple(
            user_id
            for user_id in dict.fromkeys(
                _EXPLICIT_IDENTIFIER.findall(inbound.raw_text or inbound.text)
            )
            if user_id
            not in {
                inbound.sender.user_id,
                inbound.bot_user_id,
                inbound.group_id or "",
                *state.user_refs,
            }
        )
        existing_refs = set(state.user_refs.values())
        for index, user_id in enumerate(explicit_ids, 1):
            ref = f"q{index}"
            if ref in existing_refs:
                continue
            users.append(
                UserReference(
                    ref=ref,
                    user_id=user_id,
                    display_label=f"explicit_user_{index}",
                    provenance=ReferenceProvenance.EXPLICIT_CURRENT_MESSAGE,
                    group_id=inbound.group_id,
                    visible=True,
                    mutable_target=True,
                    current_group_member=None,
                )
            )

        reply_message_id = inbound.reply_to_message_id or ""
        messages: list[MessageReference] = []
        for event in events:
            sender_ref = (
                "Yuki"
                if event.sender_user_id == inbound.bot_user_id
                else state.user_refs.get(event.sender_user_id, "unknown_user")
            )
            source = (
                ReferenceProvenance.CURRENT_REPLY
                if event.platform_message_id == reply_message_id
                else ReferenceProvenance.HISTORY
            )
            messages.append(
                MessageReference(
                    ref=state.message_refs[event.id],
                    event_id=event.id,
                    platform_message_id=event.platform_message_id,
                    sender_user_ref=sender_ref,
                    provenance=source,
                    visible=True,
                    mutable_target=source is ReferenceProvenance.CURRENT_REPLY,
                )
            )
        groups = (
            (
                GroupReference(
                    ref="g1",
                    group_id=inbound.group_id,
                    provenance=ReferenceProvenance.SYSTEM,
                    visible=True,
                ),
            )
            if inbound.group_id is not None
            else ()
        )
        return TurnReferenceRegistry(
            users=tuple(users),
            messages=tuple(messages),
            groups=groups,
            current_event_id=current_event_id,
            epoch_id=state.epoch_id,
            stale_refs=state.stale_refs,
            epoch_rolled=rolled,
            literal_user_tokens=frozenset(
                token
                for text in (
                    *(
                        event.content
                        for event in events
                        if event.direction != "outbound" and event.event_kind != "external_event"
                    ),
                    inbound.text,
                    inbound.raw_text,
                )
                for token in _LITERAL_REFERENCE_TOKEN.findall(text)
            ),
        )

    def _ensure_user(self, state: _EpochState, user_id: str, display_name: str) -> str:
        existing = state.user_refs.get(user_id)
        if existing is not None:
            return existing
        ref = f"u{state.next_user}"
        state.next_user += 1
        state.user_refs[user_id] = ref
        state.user_labels[user_id] = self._unique_label(state, user_id, display_name)
        return ref

    @staticmethod
    def _unique_label(state: _EpochState, user_id: str, display_name: str) -> str:
        cleaned = (
            _EXPLICIT_IDENTIFIER.sub(
                "编号已隐藏",
                sanitize_display_name(display_name),
            )
            or "群友"
        )
        for size in (4, 6, 8, len(user_id)):
            suffix = user_id[-size:]
            candidate = f"{cleaned}#{suffix}"
            used_suffixes = {
                label.rsplit("#", 1)[-1] for label in state.user_labels.values() if "#" in label
            }
            if suffix not in used_suffixes and candidate not in state.user_labels.values():
                return candidate
        return f"{cleaned}#{user_id}"

    @staticmethod
    def _event_display_name(events: tuple[EventRecord, ...], user_id: str) -> str:
        return next(
            (
                event.sender_display_name
                for event in reversed(events)
                if event.sender_user_id == user_id
            ),
            "群友",
        )
