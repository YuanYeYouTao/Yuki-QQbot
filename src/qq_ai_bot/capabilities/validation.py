"""Capability argument validation contract (R1 shape, R3 implementation).

Validation happens host-side before any provider executes; unknown
capabilities or schema mismatches fail closed with stable error categories.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CapabilityValidationResult:
    """Outcome of validating one call's arguments against its schema."""

    ok: bool
    error_category: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.ok and self.error_category is None:
            raise ValueError("failed validation must carry an error category")


class CapabilitySchemaValidator(Protocol):
    """Validates tool-call arguments against the declared schema version."""

    def validate(self, capability_id: str, arguments_json: str) -> CapabilityValidationResult: ...
