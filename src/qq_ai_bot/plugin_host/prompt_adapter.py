"""Bridge approved SDK Prompt Fragments into the core stable registry."""

from __future__ import annotations

from typing import cast

from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.services.prompt_registry import (
    PromptFragment,
    PromptRegistry,
    PromptStage,
    PromptTarget,
    TrustedLevel,
)
from yuki_plugin_sdk.models import PromptFragment as SdkPromptFragment

_DELETED_PLUGIN_STAGES = frozenset({"planner_plan"})
_DELETED_PLUGIN_TARGETS = frozenset({"planner", "both"})


class PluginPromptAdapter:
    def __init__(self, extensions: ExtensionRegistry, core: PromptRegistry) -> None:
        self._extensions = extensions
        self._core = core

    def activate(self, plugin_id: str, *, max_characters: int | None = None) -> int:
        """Replace all static fragments for one successfully started plugin."""

        self._core.unregister_plugin(plugin_id)
        if max_characters is not None:
            self._core.set_plugin_budget(plugin_id, max_characters)
        count = 0
        for item in self._extensions.list(
            plugin_id=plugin_id,
            kind=ExtensionKind.PROMPT_FRAGMENT,
        ):
            fragment = cast(SdkPromptFragment, item.registration)
            stage_value = getattr(fragment.stage, "value", fragment.stage)
            target_value = getattr(fragment.target, "value", fragment.target)
            if stage_value in _DELETED_PLUGIN_STAGES or target_value in _DELETED_PLUGIN_TARGETS:
                continue
            # Independent plugin sessions own a separate prompt pipeline.
            if target_value == "plugin_session":
                continue
            self._core.register(
                PromptFragment(
                    id=f"plugin.{plugin_id}.{fragment.id}",
                    stage=PromptStage(fragment.stage.value),
                    content=fragment.content,
                    plugin_id=plugin_id,
                    priority=fragment.priority,
                    trusted_level=TrustedLevel.UNTRUSTED,
                    max_characters=fragment.max_characters,
                    target=PromptTarget(fragment.target.value),
                    source=fragment.source,
                    cache_key=fragment.cache_key or "",
                )
            )
            count += 1
        return count

    def deactivate(self, plugin_id: str) -> int:
        return self._core.unregister_plugin(plugin_id)


__all__ = ["PluginPromptAdapter"]
