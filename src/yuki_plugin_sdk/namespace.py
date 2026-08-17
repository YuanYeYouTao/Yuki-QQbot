"""Plugin capability namespace ids. Copied so the SDK does not import qq_ai_bot."""

from __future__ import annotations

import re

NAMESPACE_ID_MAX_LENGTH = 64
NAMESPACE_MAX_DEPTH = 5
_SEGMENT_PATTERN = r"[a-z][a-z0-9_]*"
NAMESPACE_ID_REGEX = re.compile(rf"^{_SEGMENT_PATTERN}(\.{_SEGMENT_PATTERN})*$")
_UNSAFE_SEGMENT = re.compile(r"[^a-z0-9]+")

RESERVED_PLUGIN_NAMESPACE_PREFIXES: frozenset[str] = frozenset(
    {
        "kernel",
        "memory",
        "web",
        "qq",
        "reply",
        "relationship",
        "automation",
        "admin",
        "core",
        "system",
        "yuki",
    }
)


def is_valid_namespace_id(value: str) -> bool:
    """Lowercase dot-separated hierarchy, e.g. ``plugin.com.example.echo``."""

    if not value or len(value) > NAMESPACE_ID_MAX_LENGTH:
        return False
    if value.count(".") + 1 > NAMESPACE_MAX_DEPTH:
        return False
    return NAMESPACE_ID_REGEX.fullmatch(value) is not None


def is_reserved_plugin_namespace(namespace_id: str) -> bool:
    prefix = namespace_id.split(".", 1)[0]
    return prefix in RESERVED_PLUGIN_NAMESPACE_PREFIXES


def sanitize_namespace_segment(value: str) -> str:
    """Collapse an arbitrary plugin id into one valid namespace segment."""

    cleaned = _UNSAFE_SEGMENT.sub("_", value.strip().lower()).strip("_")
    if not cleaned:
        cleaned = "unnamed"
    if cleaned[0].isdigit():
        cleaned = f"p_{cleaned}"
    max_len = NAMESPACE_ID_MAX_LENGTH - len("plugin.")
    cleaned = cleaned[:max_len].rstrip("_")
    if not cleaned or NAMESPACE_ID_REGEX.fullmatch(cleaned) is None:
        return "unnamed"
    return cleaned


def default_plugin_namespace(plugin_id: str) -> str:
    """Prefer ``plugin.{plugin_id}``; otherwise ``plugin.{safe_id}``."""

    candidate = f"plugin.{plugin_id}"
    if is_valid_namespace_id(candidate):
        return candidate
    return f"plugin.{sanitize_namespace_segment(plugin_id)}"
