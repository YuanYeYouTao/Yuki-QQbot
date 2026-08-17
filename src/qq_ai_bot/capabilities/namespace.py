"""Capability namespace model (frozen by R3 §2.2).

A namespace is a semantic discovery category — not a provider, not a
permission and not a hard routing gate.  Search results may come from the
whole catalog but must always be intersected with the turn's
authority-filtered requestable capability set.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

NAMESPACE_ID_MAX_LENGTH = 64
NAMESPACE_MAX_DEPTH = 5
_SEGMENT_PATTERN = r"[a-z][a-z0-9_]*"
NAMESPACE_ID_REGEX = re.compile(rf"^{_SEGMENT_PATTERN}(\.{_SEGMENT_PATTERN})*$")


def is_valid_namespace_id(value: str) -> bool:
    """Lowercase dot-separated hierarchy, e.g. ``memory.person.read``."""

    if not value or len(value) > NAMESPACE_ID_MAX_LENGTH:
        return False
    if value.count(".") + 1 > NAMESPACE_MAX_DEPTH:
        return False
    return NAMESPACE_ID_REGEX.fullmatch(value) is not None


class CapabilityNamespace(BaseModel):
    """One semantic category in the capability catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    parent: str | None = None
    display_name: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not is_valid_namespace_id(value):
            raise ValueError(f"invalid namespace id: {value!r}")
        return value

    @field_validator("aliases", "tags")
    @classmethod
    def _valid_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for label in value:
            if not label or label != label.lower():
                raise ValueError(f"namespace aliases/tags must be lowercase, got {label!r}")
        if len(set(value)) != len(value):
            raise ValueError("namespace aliases/tags must be unique")
        return value

    @model_validator(mode="after")
    def _valid_parent(self) -> CapabilityNamespace:
        if self.parent is not None:
            if not is_valid_namespace_id(self.parent):
                raise ValueError(f"invalid parent namespace id: {self.parent!r}")
            if not self.id.startswith(f"{self.parent}."):
                raise ValueError(
                    f"namespace {self.id!r} must be nested under its parent {self.parent!r}"
                )
        return self

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.id.split("."))

    @property
    def depth(self) -> int:
        return len(self.path)
