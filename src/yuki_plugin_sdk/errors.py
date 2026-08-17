"""Public exception hierarchy for Plugin API v2."""

from __future__ import annotations


class PluginError(RuntimeError):
    """Base for errors plugins may safely catch."""


class ManifestValidationError(PluginError):
    pass


class PluginPermissionError(PluginError):
    pass


class RegistrationError(PluginError):
    pass


class FeatureUnavailableError(PluginError):
    pass


class PluginLifecycleError(PluginError):
    pass


class HookTimeoutError(PluginError):
    pass


class PluginSessionError(PluginError):
    pass
