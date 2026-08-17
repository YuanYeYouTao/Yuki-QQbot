"""Privacy-minimized notification publishing for the chat lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

from qq_ai_bot.runtime.observability import stable_identifier_hash
from yuki_plugin_sdk.events import EventEnvelope, EventName
from yuki_plugin_sdk.models import JsonValue

logger = logging.getLogger(__name__)


def content_free_turn_payload(
    *,
    origin: str,
    scope_type: str,
    conversation_key: str | None = None,
    **fields: JsonValue,
) -> dict[str, JsonValue]:
    """Build one turn/capability notification without user text or tool args."""

    payload: dict[str, JsonValue] = {
        "origin": origin,
        "scope_type": scope_type,
    }
    if conversation_key:
        payload["conversation_key_hash"] = stable_identifier_hash(
            conversation_key,
            kind="conversation",
        )
    payload.update(fields)
    return payload


class LifecycleEventPublisher(Protocol):
    """Minimal interface implemented by the host's notification EventBus."""

    async def publish(self, event: EventEnvelope) -> object: ...


async def publish_notification(
    publisher: LifecycleEventPublisher | None,
    name: EventName,
    payload: Mapping[str, JsonValue],
) -> None:
    """Publish one notification without letting plugin failures affect chat.

    Callers must provide metadata-only payloads.  Exception messages are not logged
    because a third-party publisher may include message bodies or credentials in them.
    """

    if publisher is None:
        return
    try:
        await publisher.publish(EventEnvelope(name=name, payload=payload))
    except Exception as exc:
        logger.warning(
            "plugin_lifecycle_publish_failed event=%s error_category=%s",
            name.value,
            type(exc).__name__,
        )
