"""Public Yuki Plugin API v2 with lazy imports and no Host side effects."""

from __future__ import annotations

from typing import Any

from yuki_plugin_sdk.api import DEFAULT_FEATURES, PLUGIN_API_VERSION

__all__ = [
    "DEFAULT_FEATURES",
    "PLUGIN_API_VERSION",
    "AgentSessionFacade",
    "Plugin",
    "PluginContext",
    "PluginRegistrar",
]


def __getattr__(name: str) -> Any:
    if name == "Plugin":
        from yuki_plugin_sdk.plugin import Plugin

        return Plugin
    if name == "PluginContext":
        from yuki_plugin_sdk.context import PluginContext

        return PluginContext
    if name == "PluginRegistrar":
        from yuki_plugin_sdk.registrar import PluginRegistrar

        return PluginRegistrar
    if name == "AgentSessionFacade":
        from yuki_plugin_sdk.sessions import AgentSessionFacade

        return AgentSessionFacade
    raise AttributeError(name)
