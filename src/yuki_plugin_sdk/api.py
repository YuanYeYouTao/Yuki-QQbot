"""Plugin API version and stable feature identifiers."""

from __future__ import annotations

import re

PLUGIN_API_VERSION = "2.0"
_API_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

DEFAULT_FEATURES: frozenset[str] = frozenset(
    {
        "message.normalized.v1",
        "message.current.mentions.v1",
        "prompt.fragment.v1",
        "admission.signal.v1",
        "automation.action.v1",
        "plugin.agent_session.v1",
        "emoji.facade.v1",
        "emoji.selection_signals.v1",
        "speech.facade.v1",
        "speech.tts_provider.v1",
        "mcp.facade.v1",
        "notification.facade.v1",
        "media.artifact.v1",
        "http.credential.v1",
    }
)


def api_major(version: str) -> int:
    match = _API_VERSION.fullmatch(version.strip())
    if match is None:
        raise ValueError("plugin API version must use MAJOR.MINOR")
    return int(match.group(1))


def is_api_compatible(requested: str, host: str = PLUGIN_API_VERSION) -> bool:
    """Plugin API is compatible only within the same major version."""

    return api_major(requested) == api_major(host)
