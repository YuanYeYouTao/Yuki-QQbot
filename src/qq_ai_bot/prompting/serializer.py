"""Compact deterministic serialization for dynamic prompt envelopes."""

from __future__ import annotations

import hashlib
import json

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.prompting.models import PromptContribution

DYNAMIC_ENVELOPE_HEADER = "本轮运行资料（按 trust 字段区分可信度）："


def serialize_dynamic(contributions: tuple[PromptContribution, ...]) -> str:
    """Serialize non-static contributions without empty sections."""

    items: list[dict[str, object]] = []
    for contribution in contributions:
        item: dict[str, object] = {
            "id": contribution.id,
            "channel": contribution.channel.value,
            "trust": contribution.trust.value,
        }
        item["data"] = (
            contribution.payload if contribution.payload is not None else contribution.content
        )
        items.append(item)
    if not items:
        return ""
    return DYNAMIC_ENVELOPE_HEADER + json.dumps(
        items,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def strip_dynamic_prefix(content: str) -> str:
    """Return the event body after a compiler-attached dynamic envelope."""

    if not content.startswith(DYNAMIC_ENVELOPE_HEADER):
        return content
    _header, separator, body = content.partition("\n\n")
    return body if separator else content


def serialized_characters(contribution: PromptContribution) -> int:
    """Return deterministic selection cost for one contribution."""

    return len(
        json.dumps(
            contribution.payload if contribution.payload is not None else contribution.content,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


def serialized_messages_hash(messages: tuple[ChatMessage, ...]) -> str:
    """Hash the actual canonical bytes of a provider-neutral message prefix."""

    payload = [
        {
            "role": message.role,
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ],
            "tool_call_id": message.tool_call_id,
            "reasoning_content": message.reasoning_content,
        }
        for message in messages
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
