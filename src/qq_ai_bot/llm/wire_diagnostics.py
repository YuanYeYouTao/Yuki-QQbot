"""Content-free diagnostics computed from the final HTTP JSON, not a prompt proxy."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.runtime.observability import current_runtime_turn_correlation

logger = logging.getLogger(__name__)


def wire_hash(value: object) -> str:
    # Preserve array and object insertion order, as the outgoing JSON does.
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class WireFingerprint:
    instructions: str
    tools: str
    settings: str
    inputs: tuple[str, ...]
    contract_revision: str
    execution_controls: str

    @classmethod
    def of(
        cls, payload: dict[str, Any], protocol: str, *, provider: str = "unspecified"
    ) -> WireFingerprint:
        if protocol == "responses":
            instructions = payload.get("instructions")
            inputs = payload.get("input", [])
        elif protocol == "anthropic_messages":
            instructions = payload.get("system")
            inputs = payload.get("messages", [])
        elif protocol == "gemini":
            instructions = payload.get("systemInstruction")
            inputs = payload.get("contents", [])
        else:
            messages = payload.get("messages", [])
            boundary = 0
            while boundary < len(messages) and messages[boundary].get("role") in {
                "system",
                "developer",
            }:
                boundary += 1
            instructions, inputs = messages[:boundary], messages[boundary:]
        settings = {
            k: v
            for k, v in payload.items()
            if k
            not in {
                "instructions",
                "input",
                "messages",
                "tools",
                "system",
                "systemInstruction",
                "contents",
            }
        }
        # tool_choice is a per-request execution restriction. It must remain
        # observable, but auto -> none does not redefine the declared contract.
        controls = {k: v for k, v in settings.items() if k == "tool_choice"}
        stable_settings = {k: v for k, v in settings.items() if k != "tool_choice"}
        contract_revision = wire_hash(
            {
                "version": 1,
                "provider": provider,
                "protocol": protocol,
                "instructions": instructions,
                "tools": payload.get("tools"),
                "settings": stable_settings,
            }
        )
        return cls(
            wire_hash(instructions),
            wire_hash(payload.get("tools")),
            wire_hash(settings),
            tuple(wire_hash(item) for item in inputs),
            contract_revision,
            wire_hash(controls),
        )


class WireRequestObserver:
    """Bounded per-provider comparison; holds hashes only, never prompt bodies."""

    def __init__(self) -> None:
        self._recent: OrderedDict[tuple[str, str, str], WireFingerprint] = OrderedDict()

    def observe(
        self,
        payload: dict[str, Any],
        protocol: str,
        *,
        chain_id: str = "",
        provider: str = "unspecified",
    ) -> dict[str, object]:
        correlation = current_runtime_turn_correlation()
        current = WireFingerprint.of(payload, protocol, provider=provider)
        # A runtime turn can contain independent internal model tasks. Compare only
        # the explicit Agent transcript, not arbitrary requests sharing an actor.
        key = (chain_id, protocol, provider) if chain_id else None
        previous = self._recent.get(key) if key else None
        changes: list[str] = []
        first_difference: int | None = None
        relation = "unbound" if key is None else "first_observation"
        if previous is not None:
            for name in ("instructions", "tools", "settings"):
                if getattr(current, name) != getattr(previous, name):
                    changes.append(name)
            common = min(len(previous.inputs), len(current.inputs))
            first_difference = next(
                (i for i in range(common) if previous.inputs[i] != current.inputs[i]), None
            )
            if first_difference is None and len(current.inputs) < len(previous.inputs):
                first_difference = len(current.inputs)
            if first_difference is not None:
                relation = "input_rewritten"
            elif len(current.inputs) > len(previous.inputs):
                relation = "append"
            else:
                relation = "same_input"
        if key:
            self._recent[key] = current
            self._recent.move_to_end(key)
            while len(self._recent) > 64:
                self._recent.popitem(last=False)
        result: dict[str, object] = {
            "correlation_id": correlation.turn_id if correlation else "unbound",
            "protocol": protocol,
            "provider": provider,
            "stage": "dispatch_attempt",
            "chain_hash": wire_hash(chain_id) if chain_id else "unbound",
            "origin": correlation.origin.value if correlation else "unknown",
            "instructions_hash": current.instructions,
            "tools_hash": current.tools,
            "settings_hash": current.settings,
            "contract_revision": current.contract_revision,
            "contract_change": (
                "unbound"
                if key is None
                else "first_observation"
                if previous is None
                else "unchanged"
                if previous.contract_revision == current.contract_revision
                else "changed"
            ),
            "execution_controls_hash": current.execution_controls,
            "execution_controls_changed": (
                previous is not None and previous.execution_controls != current.execution_controls
            ),
            "input_hash": wire_hash(current.inputs),
            "input_items": len(current.inputs),
            "relation": relation,
            "changed_fields": changes,
            "first_difference_index": first_difference,
        }
        logger.info("provider_wire_request %s", json.dumps(result, separators=(",", ":")))
        return result
