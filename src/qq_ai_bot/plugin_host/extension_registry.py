"""Conflict-safe registration of approved Plugin API extensions."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from yuki_plugin_sdk.errors import PluginPermissionError, RegistrationError
from yuki_plugin_sdk.models import PromptFragment, PromptStage, PromptTarget, TrustedLevel
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import (
    AdmissionSignalRegistration,
    AutomationActionRegistration,
    BackgroundServiceRegistration,
    CommandRegistration,
    EmojiSelectionSignalRegistration,
    EventHookRegistration,
    PluginRegistrar,
    ToolRegistration,
    TTSProviderRegistration,
)

DEFAULT_RESERVED_COMMAND_ALIASES: frozenset[str] = frozenset(
    {
        "automation",
        "forgetme",
        "group",
        "help",
        "memory",
        "new",
        "off",
        "on",
        "ping",
        "plugin",
        "preference",
        "private",
        "relationship",
        "status",
        "stop",
        "whoami",
    }
)


class ExtensionKind(StrEnum):
    TOOL = "tool"
    COMMAND = "command"
    EVENT_HOOK = "event_hook"
    PROMPT_FRAGMENT = "prompt_fragment"
    AUTOMATION_ACTION = "automation_action"
    ADMISSION_SIGNAL = "admission_signal"
    EMOJI_SELECTION_SIGNAL = "emoji_selection_signal"
    CONFIG_SCHEMA = "config_schema"
    BACKGROUND_SERVICE = "background_service"
    TTS_PROVIDER = "speech.tts_provider.v1"


@dataclass(frozen=True, slots=True)
class RegisteredExtension:
    plugin_id: str
    kind: ExtensionKind
    canonical_name: str
    model_name: str | None
    registration: object


class ExtensionRegistry:
    def __init__(
        self,
        *,
        reserved_command_aliases: Iterable[str] = DEFAULT_RESERVED_COMMAND_ALIASES,
    ) -> None:
        self._items: dict[str, RegisteredExtension] = {}
        self._model_names: dict[str, str] = {}
        self._aliases: dict[str, str] = {
            alias.casefold(): f"core:{alias.casefold()}" for alias in reserved_command_aliases
        }

    def registrar(
        self,
        plugin_id: str,
        approved_permissions: Iterable[PluginPermission],
    ) -> BoundPluginRegistrar:
        return BoundPluginRegistrar(self, plugin_id, frozenset(approved_permissions))

    def list(
        self,
        *,
        plugin_id: str | None = None,
        kind: ExtensionKind | None = None,
    ) -> tuple[RegisteredExtension, ...]:
        return tuple(
            item
            for _, item in sorted(self._items.items())
            if (plugin_id is None or item.plugin_id == plugin_id)
            and (kind is None or item.kind is kind)
        )

    def get(self, canonical_name: str) -> RegisteredExtension | None:
        return self._items.get(canonical_name)

    def resolve_model_name(self, model_name: str) -> RegisteredExtension | None:
        """Resolve an OpenAI-compatible tool name without exposing registry internals."""

        canonical = self._model_names.get(model_name)
        return self._items.get(canonical) if canonical is not None else None

    def resolve_command_alias(self, alias: str) -> RegisteredExtension | None:
        """Resolve an explicitly approved short command alias case-insensitively."""

        canonical = self._aliases.get(alias.casefold())
        return self._items.get(canonical) if canonical is not None else None

    def remove_plugin(self, plugin_id: str) -> int:
        selected = [key for key, item in self._items.items() if item.plugin_id == plugin_id]
        for key in selected:
            item = self._items.pop(key)
            if item.model_name is not None:
                self._model_names.pop(item.model_name, None)
        aliases = [
            alias for alias, owner in self._aliases.items() if owner.startswith(plugin_id + ":")
        ]
        for alias in aliases:
            del self._aliases[alias]
        return len(selected)

    def _add(
        self,
        *,
        plugin_id: str,
        kind: ExtensionKind,
        local_name: str,
        registration: object,
        expose_to_model: bool = False,
        short_alias: str | None = None,
    ) -> RegisteredExtension:
        canonical = f"{plugin_id}:{local_name}"
        if canonical in self._items:
            raise RegistrationError(f"duplicate extension: {canonical}")
        model_name = _model_tool_name(plugin_id, local_name) if expose_to_model else None
        if model_name is not None and model_name in self._model_names:
            raise RegistrationError(f"duplicate model tool name: {model_name}")
        if short_alias is not None:
            alias = short_alias.casefold()
            if alias in self._aliases:
                raise RegistrationError(f"duplicate command alias: {short_alias}")
            self._aliases[alias] = canonical
        item = RegisteredExtension(plugin_id, kind, canonical, model_name, registration)
        self._items[canonical] = item
        if model_name is not None:
            self._model_names[model_name] = canonical
        return item


class BoundPluginRegistrar(PluginRegistrar):
    def __init__(
        self,
        registry: ExtensionRegistry,
        plugin_id: str,
        permissions: frozenset[PluginPermission],
    ) -> None:
        self._registry = registry
        self._plugin_id = plugin_id
        self._permissions = permissions

    def register_tool(self, registration: ToolRegistration) -> None:
        self._require(PluginPermission.TOOL_REGISTER)
        _validate_schema(registration.input_model, "tool input")
        _validate_schema(registration.output_model, "tool output")
        self._registry._add(
            plugin_id=self._plugin_id,
            kind=ExtensionKind.TOOL,
            local_name=registration.metadata.name,
            registration=registration,
            expose_to_model=True,
        )

    def register_command(self, registration: CommandRegistration) -> None:
        self._require(PluginPermission.COMMAND_REGISTER)
        _validate_schema(registration.argument_model, "command arguments")
        self._registry._add(
            plugin_id=self._plugin_id,
            kind=ExtensionKind.COMMAND,
            local_name=registration.metadata.name,
            registration=registration,
            short_alias=registration.metadata.short_alias,
        )

    def register_event_hook(self, registration: EventHookRegistration) -> None:
        self._require(PluginPermission.EVENT_SUBSCRIBE)
        self._registry._add(
            plugin_id=self._plugin_id,
            kind=ExtensionKind.EVENT_HOOK,
            local_name=registration.metadata.id,
            registration=registration,
        )

    def register_prompt_fragment(self, fragment: PromptFragment) -> None:
        if fragment.stage is PromptStage.PLUGIN_CONTEXT:
            self._require(PluginPermission.PROMPT_CONTEXT_REGISTER)
        elif fragment.stage is PromptStage.TOOL_GUIDANCE:
            self._require(PluginPermission.PROMPT_GUIDANCE_REGISTER)
        else:
            raise RegistrationError(
                "third-party plugins may only register plugin_context or tool_guidance"
            )
        if fragment.target not in {PromptTarget.AGENT, PromptTarget.PLUGIN_SESSION}:
            raise RegistrationError("third-party plugins may only target agent or plugin_session")
        if fragment.plugin_id not in {None, self._plugin_id}:
            raise RegistrationError("prompt fragment plugin_id does not match registrar")
        normalized = fragment.model_copy(
            update={
                "plugin_id": self._plugin_id,
                "trusted_level": TrustedLevel.PLUGIN_UNTRUSTED,
            }
        )
        self._registry._add(
            plugin_id=self._plugin_id,
            kind=ExtensionKind.PROMPT_FRAGMENT,
            local_name=normalized.id,
            registration=normalized,
        )

    def register_automation_action(self, registration: AutomationActionRegistration) -> None:
        self._require(PluginPermission.AUTOMATION_ACTION_REGISTER)
        _validate_schema(registration.input_model, "automation input")
        _validate_schema(registration.output_model, "automation output")
        self._registry._add(
            plugin_id=self._plugin_id,
            kind=ExtensionKind.AUTOMATION_ACTION,
            local_name=registration.metadata.name,
            registration=registration,
        )

    def register_admission_signal(self, registration: AdmissionSignalRegistration) -> None:
        self._require(PluginPermission.ADMISSION_SIGNAL_REGISTER)
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", registration.name) is None:
            raise RegistrationError("invalid admission signal name")
        self._registry._add(
            plugin_id=self._plugin_id,
            kind=ExtensionKind.ADMISSION_SIGNAL,
            local_name=registration.name,
            registration=registration,
        )

    def register_emoji_selection_signal(
        self, registration: EmojiSelectionSignalRegistration
    ) -> None:
        self._require(PluginPermission.EMOJI_HOOK)
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", registration.name) is None:
            raise RegistrationError("invalid emoji selection signal name")
        self._registry._add(
            plugin_id=self._plugin_id,
            kind=ExtensionKind.EMOJI_SELECTION_SIGNAL,
            local_name=registration.name,
            registration=registration,
        )

    def register_config_schema(self, schema: type[BaseModel]) -> None:
        if not (
            PluginPermission.PLUGIN_CONFIG_READ in self._permissions
            or PluginPermission.PLUGIN_CONFIG_WRITE in self._permissions
        ):
            raise PluginPermissionError("plugin config permission was not approved")
        _validate_schema(schema, "plugin config")
        self._registry._add(
            plugin_id=self._plugin_id,
            kind=ExtensionKind.CONFIG_SCHEMA,
            local_name="config",
            registration=schema,
        )

    def register_background_service(self, registration: BackgroundServiceRegistration) -> None:
        self._require(PluginPermission.BACKGROUND_WORKER)
        self._registry._add(
            plugin_id=self._plugin_id,
            kind=ExtensionKind.BACKGROUND_SERVICE,
            local_name=registration.metadata.name,
            registration=registration,
        )

    def register_tts_provider(self, registration: TTSProviderRegistration) -> None:
        self._require(PluginPermission.SPEECH_PROVIDER_REGISTER)
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", registration.name) is None:
            raise RegistrationError("invalid TTS provider name")
        self._registry._add(
            plugin_id=self._plugin_id,
            kind=ExtensionKind.TTS_PROVIDER,
            local_name=registration.name,
            registration=registration,
        )

    def _require(self, permission: PluginPermission) -> None:
        if permission not in self._permissions:
            raise PluginPermissionError(f"plugin permission was not approved: {permission.value}")


def _validate_schema(model: type[BaseModel], label: str) -> None:
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise RegistrationError(f"{label} must be a Pydantic model")
    if model.model_config.get("extra") != "forbid":
        raise RegistrationError(f"{label} model must set extra='forbid'")
    model.model_json_schema()


def _model_tool_name(plugin_id: str, local_name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", f"plugin__{plugin_id}__{local_name}")
    if len(normalized) <= 64:
        return normalized
    suffix = hashlib.sha256(normalized.encode()).hexdigest()[:8]
    return f"{normalized[:55]}_{suffix}"
