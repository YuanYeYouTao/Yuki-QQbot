"""Stable-schema adapter and trusted argument resolver for model tools."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from qq_ai_bot.capabilities.models import CapabilityRisk
from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.references.errors import ReferenceErrorCode, ReferenceResolutionError
from qq_ai_bot.references.models import (
    ReferenceProvenance,
    TurnReferenceRegistry,
    UserReference,
)

_USER_PATTERN = r"^(u|q)[1-9][0-9]*$"
_GROUP_PATTERN = r"^g[1-9][0-9]*$"
_MESSAGE_PATTERN = r"^m[1-9][0-9]*$"
_KEY_MAP = {
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
_RAW_IDENTIFIER_KEYS = frozenset(_KEY_MAP)


class ReferenceToolAdapter:
    """Translate only the model boundary; domain tools keep their real-ID protocol."""

    def project_tool(self, tool: ChatTool) -> ChatTool:
        if tool.name == "get_person_memories":
            return replace(
                tool,
                parameters={
                    "type": "object",
                    "properties": {
                        "user_ref": self._reference_schema("user"),
                        "query": {"type": "string", "maxLength": 400},
                        "mode": {"type": "string", "enum": ["relevant", "overview"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "additionalProperties": False,
                },
                description=(
                    "Read memories for one trusted user reference in this turn. "
                    "Use the current sender's u-ref for the speaker; never infer a target by name."
                ),
            )
        projected = self._project_schema(tool.parameters)
        description = tool.description
        if projected != tool.parameters:
            description = (
                f"{description} Identifier parameters must use the trusted *_ref values "
                "listed in this turn's runtime context; never submit raw platform IDs."
            )
        return replace(tool, description=description, parameters=projected)

    def resolve_arguments(
        self,
        arguments: dict[str, Any],
        *,
        registry: TurnReferenceRegistry,
        risk: CapabilityRisk,
        tool_name: str,
        allow_current_sender: bool = False,
    ) -> dict[str, Any]:
        self._reject_raw_identifier_keys(arguments)
        resolved = self._resolve_object(
            arguments,
            registry=registry,
            risk=risk,
            allow_current_sender=allow_current_sender,
        )
        if not isinstance(resolved, dict):
            raise TypeError("resolved tool arguments must remain an object")
        if tool_name == "get_person_memories" and "user_ref" in arguments:
            item = self._resolve_user(
                str(arguments["user_ref"]),
                registry,
                risk,
                allow_current_sender=allow_current_sender,
            )
            resolved.pop("user_ref", None)
            resolved["user_id"] = item.user_id
        return resolved

    @staticmethod
    def project_result(
        result: ToolExecutionResult,
        registry: TurnReferenceRegistry,
    ) -> ToolExecutionResult:
        return replace(
            result,
            data=registry.project_value(result.data),
            content=tuple(
                registry.project_value(item) for item in result.content if isinstance(item, dict)
            ),
            metadata=(
                registry.project_value(result.metadata)
                if isinstance(result.metadata, dict)
                else result.metadata
            ),
            public_message=(
                registry.redact_text(result.public_message)
                if result.public_message is not None
                else None
            ),
        )

    def _project_schema(self, value: Any) -> Any:
        if isinstance(value, dict):
            projected: dict[str, Any] = {}
            for key, child in value.items():
                if key == "properties" and isinstance(child, dict):
                    projected[key] = {
                        self._project_key(str(name)): self._schema_property(
                            self._project_key(str(name)), schema
                        )
                        for name, schema in child.items()
                    }
                elif key == "required" and isinstance(child, list):
                    projected[key] = [self._project_key(str(item)) for item in child]
                else:
                    projected[key] = self._project_schema(child)
            return projected
        if isinstance(value, list):
            return [self._project_schema(item) for item in value]
        return value

    def _schema_property(self, key: str, schema: Any) -> Any:
        projected = self._project_schema(schema)
        if not isinstance(projected, dict):
            return projected
        if key == "user_refs" or key.endswith("_user_refs"):
            return {**projected, "type": "array", "items": self._reference_schema("user")}
        if key.endswith("user_ref") or key == "user_ref":
            return {**projected, **self._reference_schema("user")}
        if key == "group_refs" or key.endswith("_group_refs"):
            return {**projected, "type": "array", "items": self._reference_schema("group")}
        if key.endswith("group_ref") or key == "group_ref":
            return {**projected, **self._reference_schema("group")}
        if key == "message_refs" or key.endswith("_message_refs"):
            return {
                **projected,
                "type": "array",
                "items": self._reference_schema("message"),
            }
        if key.endswith("message_ref") or key == "message_ref":
            return {**projected, **self._reference_schema("message")}
        return projected

    @staticmethod
    def _reference_schema(kind: str) -> dict[str, object]:
        pattern = {
            "user": _USER_PATTERN,
            "group": _GROUP_PATTERN,
            "message": _MESSAGE_PATTERN,
        }[kind]
        return {
            "type": "string",
            "pattern": pattern,
            "description": f"Trusted {kind} reference from this turn's runtime context",
        }

    def _resolve_object(
        self,
        value: Any,
        *,
        registry: TurnReferenceRegistry,
        risk: CapabilityRisk,
        allow_current_sender: bool,
        key: str = "",
    ) -> Any:
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for raw_key, child in value.items():
                name = str(raw_key)
                if self._is_user_ref(name):
                    if isinstance(child, list):
                        output[self._raw_key(name)] = [
                            self._resolve_user(
                                str(item),
                                registry,
                                risk,
                                allow_current_sender=allow_current_sender,
                            ).user_id
                            for item in child
                        ]
                    else:
                        user_item = self._resolve_user(
                            str(child),
                            registry,
                            risk,
                            allow_current_sender=allow_current_sender,
                        )
                        output[self._raw_key(name)] = user_item.user_id
                elif self._is_group_ref(name):
                    refs = child if isinstance(child, list) else [child]
                    group_ids: list[str] = []
                    for ref in refs:
                        group_item = registry.group(str(ref))
                        if not group_item.visible:
                            raise ReferenceResolutionError(
                                ReferenceErrorCode.TARGET_NOT_VISIBLE,
                                "The group reference is not visible in this turn",
                            )
                        group_ids.append(group_item.group_id)
                    output[self._raw_key(name)] = (
                        group_ids if isinstance(child, list) else group_ids[0]
                    )
                elif self._is_message_ref(name):
                    refs = child if isinstance(child, list) else [child]
                    message_ids: list[str] = []
                    for ref in refs:
                        message_item = registry.message(str(ref))
                        if not message_item.visible:
                            raise ReferenceResolutionError(
                                ReferenceErrorCode.TARGET_NOT_VISIBLE,
                                "The message reference is not visible in this turn",
                            )
                        if risk is not CapabilityRisk.READ and not message_item.mutable_target:
                            raise ReferenceResolutionError(
                                ReferenceErrorCode.TARGET_NOT_MUTABLE,
                                "A historical message cannot be used as a mutation target",
                            )
                        message_ids.append(message_item.platform_message_id)
                    output[self._raw_key(name)] = (
                        message_ids if isinstance(child, list) else message_ids[0]
                    )
                else:
                    output[name] = self._resolve_object(
                        child,
                        registry=registry,
                        risk=risk,
                        allow_current_sender=allow_current_sender,
                        key=name,
                    )
            return output
        if isinstance(value, list):
            return [
                self._resolve_object(
                    item,
                    registry=registry,
                    risk=risk,
                    allow_current_sender=allow_current_sender,
                    key=key,
                )
                for item in value
            ]
        return value

    @staticmethod
    def _resolve_user(
        ref: str,
        registry: TurnReferenceRegistry,
        risk: CapabilityRisk,
        *,
        allow_current_sender: bool,
    ) -> UserReference:
        item = registry.user(ref)
        if not item.visible:
            raise ReferenceResolutionError(
                ReferenceErrorCode.TARGET_NOT_VISIBLE,
                "The user reference is not visible in this turn",
            )
        sender_owned = bool(
            allow_current_sender and item.provenance is ReferenceProvenance.CURRENT_SENDER
        )
        if risk is not CapabilityRisk.READ and not item.mutable_target and not sender_owned:
            raise ReferenceResolutionError(
                ReferenceErrorCode.TARGET_NOT_MUTABLE,
                "A history-only user cannot be used as a mutation target",
            )
        return item

    def _reject_raw_identifier_keys(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if self._is_raw_identifier_key(str(key)):
                    raise ReferenceResolutionError(
                        ReferenceErrorCode.RAW_IDENTIFIER_NOT_ALLOWED,
                        "Raw platform identifiers are not accepted from the model",
                    )
                self._reject_raw_identifier_keys(child)
        elif isinstance(value, list):
            for item in value:
                self._reject_raw_identifier_keys(item)

    @staticmethod
    def _is_user_ref(key: str) -> bool:
        return key in {"user_ref", "user_refs"} or key.endswith(("_user_ref", "_user_refs"))

    @staticmethod
    def _is_group_ref(key: str) -> bool:
        return key in {"group_ref", "group_refs"} or key.endswith(("_group_ref", "_group_refs"))

    @staticmethod
    def _is_message_ref(key: str) -> bool:
        return key in {"message_ref", "message_refs"} or key.endswith(
            ("_message_ref", "_message_refs")
        )

    @staticmethod
    def _raw_key(key: str) -> str:
        canonical = {
            "user_ref": "user_id",
            "subject_user_ref": "subject_user_id",
            "visibility_user_ref": "visibility_user_id",
            "sender_user_ref": "sender_user_id",
            "actor_user_ref": "actor_user_id",
            "group_ref": "group_id",
            "visibility_group_ref": "visibility_group_id",
            "message_ref": "message_id",
            "reply_to_message_ref": "reply_to_message_id",
            "trigger_message_ref": "trigger_message_id",
            "user_refs": "user_ids",
            "group_refs": "group_ids",
            "message_refs": "message_ids",
        }
        mapped = canonical.get(key)
        if mapped is not None:
            return mapped
        for suffix, replacement in (
            ("user_refs", "user_ids"),
            ("group_refs", "group_ids"),
            ("message_refs", "message_ids"),
        ):
            if key.endswith(suffix):
                return f"{key.removesuffix(suffix)}{replacement}"
        return key.removesuffix("_ref") + "_id"

    @staticmethod
    def _project_key(key: str) -> str:
        mapped = _KEY_MAP.get(key)
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

    @staticmethod
    def _is_raw_identifier_key(key: str) -> bool:
        if key in _RAW_IDENTIFIER_KEYS:
            return True
        return key.endswith(
            (
                "user_id",
                "user_ids",
                "group_id",
                "group_ids",
                "message_id",
                "message_ids",
            )
        )


__all__ = ["ReferenceToolAdapter"]
