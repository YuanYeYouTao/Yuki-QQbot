"""Host-side JSON Schema validation before any provider binding (R3 §9)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for

from qq_ai_bot.capabilities.catalog import UnifiedToolCatalogEntry
from qq_ai_bot.capabilities.models import CapabilityDescriptor

TOOL_INPUT_VALIDATION_FAILED = "tool_input_validation_failed"
UNDECLARED_TOOL = "undeclared_tool"
SCHEMA_QUARANTINED = "capability_schema_quarantined"
MAX_SCHEMA_DEPTH = 12
MAX_SCHEMA_NODES = 256
_REMOTE_REF = re.compile(r"^https?://", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CapabilityValidationResult:
    """Outcome of validating one call's arguments against its schema."""

    ok: bool
    error_category: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.ok and self.error_category is None:
            raise ValueError("failed validation must carry an error category")


@dataclass(slots=True)
class JsonSchemaCapabilityValidator:
    """Compile-and-cache validators at catalog admission; never fetch $ref."""

    _validators: dict[str, Draft202012Validator] = field(default_factory=dict)
    _quarantined: set[str] = field(default_factory=set)

    def admit(self, entries: tuple[UnifiedToolCatalogEntry, ...]) -> tuple[str, ...]:
        """Compile schemas; return quarantined capability ids."""

        quarantined: list[str] = []
        for entry in entries:
            capability_id = entry.descriptor.model_name
            schema = _with_lifted_defs(entry.descriptor.input_schema)
            try:
                _assert_safe_schema(schema)
                validator_cls = validator_for(schema, default=Draft202012Validator)
                validator_cls.check_schema(schema)
                self._validators[capability_id] = validator_cls(schema)
                self._quarantined.discard(capability_id)
            except (SchemaError, ValueError):
                self._quarantined.add(capability_id)
                self._validators.pop(capability_id, None)
                quarantined.append(capability_id)
        return tuple(quarantined)

    def validate(self, capability_id: str, arguments_json: str) -> CapabilityValidationResult:
        if capability_id in self._quarantined:
            return CapabilityValidationResult(
                ok=False,
                error_category=SCHEMA_QUARANTINED,
                detail="tool schema is not safe to validate",
            )
        validator = self._validators.get(capability_id)
        if validator is None:
            return CapabilityValidationResult(
                ok=False,
                error_category=UNDECLARED_TOOL,
                detail="tool is not declared for this turn",
            )
        try:
            payload = json.loads(arguments_json)
        except json.JSONDecodeError:
            return CapabilityValidationResult(
                ok=False,
                error_category=TOOL_INPUT_VALIDATION_FAILED,
                detail="arguments must be a JSON object",
            )
        if not isinstance(payload, dict):
            return CapabilityValidationResult(
                ok=False,
                error_category=TOOL_INPUT_VALIDATION_FAILED,
                detail="arguments must be a JSON object",
            )
        try:
            validator.validate(payload)
        except ValidationError:
            return CapabilityValidationResult(
                ok=False,
                error_category=TOOL_INPUT_VALIDATION_FAILED,
                detail="arguments do not match the declared schema",
            )
        except Exception as exc:
            module = getattr(type(exc), "__module__", "")
            if "jsonschema" in module or "referencing" in module:
                return CapabilityValidationResult(
                    ok=False,
                    error_category=TOOL_INPUT_VALIDATION_FAILED,
                    detail="arguments do not match the declared schema",
                )
            raise
        return CapabilityValidationResult(ok=True)

    def is_quarantined(self, capability_id: str) -> bool:
        return capability_id in self._quarantined


def _assert_safe_schema(
    schema: dict[str, object], *, depth: int = 0, nodes: list[int] | None = None
) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise ValueError("schema depth exceeds host limit")
    counted = nodes if nodes is not None else [0]
    counted[0] += 1
    if counted[0] > MAX_SCHEMA_NODES:
        raise ValueError("schema size exceeds host limit")
    ref = schema.get("$ref")
    if isinstance(ref, str) and _REMOTE_REF.match(ref):
        raise ValueError("remote $ref is not allowed")
    if "$ref" in schema and isinstance(ref, str) and ref.startswith("#"):
        pass
    elif "$ref" in schema:
        raise ValueError("unsupported $ref")
    for key in ("items", "additionalProperties", "contains", "propertyNames", "not"):
        nested = schema.get(key)
        if isinstance(nested, dict):
            _assert_safe_schema(nested, depth=depth + 1, nodes=counted)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        nested = schema.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    _assert_safe_schema(item, depth=depth + 1, nodes=counted)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for item in properties.values():
            if isinstance(item, dict):
                _assert_safe_schema(item, depth=depth + 1, nodes=counted)
    defs = schema.get("$defs") or schema.get("definitions")
    if isinstance(defs, dict):
        for item in defs.values():
            if isinstance(item, dict):
                _assert_safe_schema(item, depth=depth + 1, nodes=counted)


def _with_lifted_defs(schema: dict[str, object]) -> dict[str, object]:
    """Copy nested ``$defs`` to the schema root so local ``#/$defs/...`` refs resolve."""

    lifted = dict(schema)
    collected: dict[str, object] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            nested = node.get("$defs") or node.get("definitions")
            if isinstance(nested, dict):
                for key, value in nested.items():
                    collected.setdefault(str(key), value)
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(lifted)
    if not collected:
        return lifted
    existing = lifted.get("$defs")
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, value in collected.items():
        merged.setdefault(key, value)
    lifted["$defs"] = merged
    return lifted


def domain_validate_descriptor(
    descriptor: CapabilityDescriptor, arguments: dict[str, Any]
) -> CapabilityValidationResult:
    del descriptor, arguments
    return CapabilityValidationResult(ok=True)
