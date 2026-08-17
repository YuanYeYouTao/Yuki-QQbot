"""Metadata-driven capability visibility policy.

Authority is origin, actor permissions, scene isolation and the memory
capability view.  Namespace is not a permission and is never used as a hard
filter here.
"""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.models import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityRisk,
)
from qq_ai_bot.runtime.contracts import MemoryCapabilityView

_WRITE_EFFECTS = frozenset(
    {
        CapabilityEffect.WRITE_STATE,
        CapabilityEffect.PLATFORM_SEND,
        CapabilityEffect.PLATFORM_MUTATE,
    }
)
_READ_EFFECTS = frozenset(
    {
        CapabilityEffect.READ_STATE,
        CapabilityEffect.EXTERNAL_READ,
        CapabilityEffect.REPLY_EFFECT,
    }
)


@dataclass(frozen=True, slots=True)
class CapabilityPolicyContext:
    authority: AuthorityContext
    origin: TurnOrigin
    contains_images: bool = False
    web_was_used: bool = False
    conversation_open: bool = True
    tools_closed: bool = False
    read_only: bool = False
    memory_view: MemoryCapabilityView | None = None
    artifact_available: bool = False
    reply_target_available: bool = False


class CapabilityPolicyEngine:
    """Intersect backend descriptors with current-turn authority and scene."""

    def visible(
        self,
        descriptors: tuple[CapabilityDescriptor, ...],
        context: CapabilityPolicyContext,
    ) -> tuple[CapabilityDescriptor, ...]:
        if not context.conversation_open or context.tools_closed:
            return ()
        granted = set(context.authority.permissions)
        if context.authority.is_superuser:
            granted.add("superuser")
        hidden_namespaces = (
            frozenset(context.memory_view.hidden_namespaces)
            if context.memory_view is not None
            else frozenset()
        )
        exclusive = (
            context.memory_view.exclusive_namespace if context.memory_view is not None else None
        )
        visible: list[CapabilityDescriptor] = []
        for descriptor in descriptors:
            if context.origin not in descriptor.allowed_origins:
                continue
            if not descriptor.required_permissions.issubset(granted):
                continue
            if context.read_only and descriptor.effect not in _READ_EFFECTS:
                continue
            if context.contains_images and descriptor.effect in {
                CapabilityEffect.WRITE_STATE,
                CapabilityEffect.PLATFORM_MUTATE,
            }:
                continue
            if descriptor.namespace_id in hidden_namespaces:
                continue
            if exclusive is not None and descriptor.effect in _WRITE_EFFECTS:
                if descriptor.namespace_id != exclusive:
                    continue
            if (
                descriptor.model_name == "read_tool_artifact"
                and not context.artifact_available
            ):
                continue
            if (
                descriptor.model_name == "set_reply_target"
                and not context.reply_target_available
            ):
                continue
            if descriptor.risk is CapabilityRisk.DESTRUCTIVE and context.origin not in {
                TurnOrigin.USER_MESSAGE
            }:
                continue
            visible.append(descriptor)
        return tuple(visible)

    def requestable_ids(
        self,
        descriptors: tuple[CapabilityDescriptor, ...],
        context: CapabilityPolicyContext,
    ) -> frozenset[str]:
        return frozenset(item.model_name for item in self.visible(descriptors, context))
