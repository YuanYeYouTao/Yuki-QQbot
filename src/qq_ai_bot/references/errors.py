"""Stable model-facing errors for trusted reference resolution."""

from __future__ import annotations

from enum import StrEnum


class ReferenceErrorCode(StrEnum):
    UNKNOWN_USER_REF = "unknown_user_ref"
    UNKNOWN_GROUP_REF = "unknown_group_ref"
    UNKNOWN_MESSAGE_REF = "unknown_message_ref"
    STALE_REFERENCE = "stale_reference"
    REFERENCE_EPOCH_MISMATCH = "reference_epoch_mismatch"
    TARGET_NOT_VISIBLE = "target_not_visible"
    TARGET_NOT_AUTHORIZED = "target_not_authorized"
    TARGET_NOT_GROUP_MEMBER = "target_not_group_member"
    TARGET_NOT_MUTABLE = "target_not_mutable"
    AMBIGUOUS_TARGET = "ambiguous_target"
    EXPLICIT_IDENTIFIER_NOT_IN_CURRENT_EVENT = "explicit_identifier_not_in_current_event"
    RAW_IDENTIFIER_NOT_ALLOWED = "raw_identifier_not_allowed"


class ReferenceResolutionError(ValueError):
    """Reject one model reference without exposing its trusted backing identifier."""

    def __init__(self, code: ReferenceErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
