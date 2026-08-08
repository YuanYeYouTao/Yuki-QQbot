"""Immutable trusted-reference models shared by prompt and tool boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from qq_ai_bot.references.errors import ReferenceErrorCode, ReferenceResolutionError

_REFERENCE_TOKEN = re.compile(r"(?<![A-Za-z0-9_])(?:u|q|g|m)[1-9][0-9]*(?![A-Za-z0-9_])")


class ReferenceProvenance(StrEnum):
    CURRENT_SENDER = "current_sender"
    CURRENT_MENTION = "current_mention"
    CURRENT_REPLY = "current_reply"
    HISTORY = "history"
    EXPLICIT_CURRENT_MESSAGE = "explicit_current_message"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class UserReference:
    ref: str
    user_id: str
    display_label: str
    provenance: ReferenceProvenance
    group_id: str | None
    visible: bool
    mutable_target: bool
    current_group_member: bool | None = None


@dataclass(frozen=True, slots=True)
class MessageReference:
    ref: str
    event_id: int
    platform_message_id: str
    sender_user_ref: str
    provenance: ReferenceProvenance
    visible: bool
    mutable_target: bool = False


@dataclass(frozen=True, slots=True)
class GroupReference:
    ref: str
    group_id: str
    provenance: ReferenceProvenance
    visible: bool


@dataclass(frozen=True, slots=True)
class TurnReferenceRegistry:
    """One immutable mapping shared by every model/tool round in an Agent turn."""

    users: tuple[UserReference, ...]
    messages: tuple[MessageReference, ...]
    groups: tuple[GroupReference, ...]
    current_event_id: int
    epoch_id: str
    stale_refs: frozenset[str] = frozenset()
    epoch_rolled: bool = False
    literal_user_tokens: frozenset[str] = frozenset()

    def user(self, ref: str) -> UserReference:
        item = next((candidate for candidate in self.users if candidate.ref == ref), None)
        if item is None:
            self._raise_missing(ref, ReferenceErrorCode.UNKNOWN_USER_REF)
        assert item is not None
        return item

    def user_for_id(self, user_id: str) -> UserReference | None:
        return next((item for item in self.users if item.user_id == user_id), None)

    def group(self, ref: str) -> GroupReference:
        item = next((candidate for candidate in self.groups if candidate.ref == ref), None)
        if item is None:
            self._raise_missing(ref, ReferenceErrorCode.UNKNOWN_GROUP_REF)
        assert item is not None
        return item

    def group_for_id(self, group_id: str) -> GroupReference | None:
        return next((item for item in self.groups if item.group_id == group_id), None)

    def message(self, ref: str) -> MessageReference:
        item = next((candidate for candidate in self.messages if candidate.ref == ref), None)
        if item is None:
            self._raise_missing(ref, ReferenceErrorCode.UNKNOWN_MESSAGE_REF)
        assert item is not None
        return item

    def message_for_platform_id(self, message_id: str) -> MessageReference | None:
        return next(
            (item for item in self.messages if item.platform_message_id == message_id),
            None,
        )

    def model_context(self) -> dict[str, object]:
        return {
            "epoch": self.epoch_id,
            "current_event": "current_event",
            "users": [
                {
                    "ref": item.ref,
                    "label": item.display_label,
                    "source": item.provenance.value,
                    "mutable_target": item.mutable_target,
                }
                for item in self.users
                if item.visible
            ],
            "groups": [
                {"ref": item.ref, "label": "当前群", "source": item.provenance.value}
                for item in self.groups
                if item.visible
            ],
            "policy": (
                "工具只能使用这里列出的 user_ref/group_ref/message_ref；"
                "不得猜测、构造或要求恢复真实 QQ、群号和消息号。"
            ),
        }

    def project_value(self, value: Any, *, key: str = "") -> Any:
        """Remove registered real identifiers from one model-visible value."""

        if isinstance(value, dict):
            projected: dict[str, Any] = {}
            for raw_key, child in value.items():
                name = str(raw_key)
                projected[self._project_key(name)] = self.project_value(child, key=name)
            return projected
        if isinstance(value, list):
            return [self.project_value(item, key=key) for item in value]
        if isinstance(value, tuple):
            return [self.project_value(item, key=key) for item in value]
        if isinstance(value, str):
            direct = self._reference_for_identifier(value, key)
            if direct is not None:
                return direct
            return self.redact_text(value)
        return value

    def redact_text(self, text: str) -> str:
        replacements: list[tuple[str, str]] = []
        for message_item in self.messages:
            replacements.append((message_item.platform_message_id, message_item.ref))
        for user_item in self.users:
            replacements.append((user_item.user_id, user_item.ref))
        for group_item in self.groups:
            replacements.append((group_item.group_id, group_item.ref))
        rendered = text
        for identifier, ref in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
            if not identifier:
                continue
            rendered = re.sub(
                rf"(?<![0-9]){re.escape(identifier)}(?![0-9])",
                ref,
                rendered,
            )
        return rendered

    def clean_output(self, text: str) -> tuple[str, bool]:
        """Replace only registered internal tokens in final user-visible text."""

        labels = {item.ref: item.display_label for item in self.users}
        labels.update({item.ref: "当前群" for item in self.groups})
        labels.update({item.ref: "那条消息" for item in self.messages})
        leaked = False

        def replace(match: re.Match[str]) -> str:
            nonlocal leaked
            token = match.group(0)
            if token in self.literal_user_tokens:
                return token
            replacement = labels.get(token)
            if replacement is None:
                return token
            leaked = True
            return replacement

        cleaned = re.sub(
            r"(?m)^\[(?:u|q)[1-9][0-9]*=[^\]\r\n]{1,160}\]\s*$",
            "",
            text,
        )
        cleaned = re.sub(
            r"\[current_event\|sender:(?:u|q)[1-9][0-9]*"
            r"(?:=[^|\]\r\n]{1,160})?(?:\|[^\]\r\n]{1,240})?\]\s*",
            "",
            cleaned,
        )
        leaked = cleaned != text
        cleaned = _REFERENCE_TOKEN.sub(replace, cleaned)
        if "current_event" in cleaned:
            cleaned = cleaned.replace("current_event", "当前消息")
            leaked = True
        return cleaned.strip(), leaked

    def _reference_for_identifier(self, value: str, key: str) -> str | None:
        lowered = key.casefold()
        if "message" in lowered and lowered.endswith(("id", "ids")):
            message_item = self.message_for_platform_id(value)
            return message_item.ref if message_item is not None else "unavailable_message"
        if "group" in lowered and lowered.endswith(("id", "ids")):
            group_item = self.group_for_id(value)
            return group_item.ref if group_item is not None else "unavailable_group"
        if ("user" in lowered or "actor" in lowered or "sender" in lowered) and lowered.endswith(
            ("id", "ids")
        ):
            user_item = self.user_for_id(value)
            return user_item.ref if user_item is not None else "unavailable_user"
        return None

    @staticmethod
    def _project_key(key: str) -> str:
        replacements = {
            "user_id": "user_ref",
            "subject_user_id": "subject_user_ref",
            "visibility_user_id": "visibility_user_ref",
            "sender_user_id": "sender_user_ref",
            "actor_user_id": "actor_user_ref",
            "group_id": "group_ref",
            "visibility_group_id": "visibility_group_ref",
            "message_id": "message_ref",
            "platform_message_id": "message_ref",
            "reply_to_message_id": "reply_to_message_ref",
            "trigger_message_id": "trigger_message_ref",
        }
        mapped = replacements.get(key)
        if mapped is not None:
            return mapped
        for suffix, replacement in (
            ("user_id", "user_ref"),
            ("group_id", "group_ref"),
            ("message_id", "message_ref"),
        ):
            if key == suffix or key.endswith(f"_{suffix}"):
                return f"{key.removesuffix(suffix)}{replacement}"
        for suffix, replacement in (
            ("user_ids", "user_refs"),
            ("group_ids", "group_refs"),
            ("message_ids", "message_refs"),
        ):
            if key == suffix or key.endswith(f"_{suffix}"):
                return f"{key.removesuffix(suffix)}{replacement}"
        return key

    def _raise_missing(self, ref: str, unknown: ReferenceErrorCode) -> None:
        if ref in self.stale_refs:
            raise ReferenceResolutionError(
                ReferenceErrorCode.STALE_REFERENCE,
                "该引用属于已经滚动或重置的历史窗口",
            )
        raise ReferenceResolutionError(unknown, "当前轮没有注册该可信引用")
