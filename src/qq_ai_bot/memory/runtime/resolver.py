"""Trusted memory scope and access resolution.

Scope and the initial ``MemoryTurnContract`` are derived from host facts
only — never from model output or a phrase dictionary.  Ordinary natural
language always enters as passive; structured read/write commands are
supplied by the command router, not guessed from user text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryRecallPurpose
from qq_ai_bot.memory.runtime.contract import (
    MemoryTurnContract,
    active_read_contract,
    dormant_contract,
    exclusive_write_contract,
    forbidden_contract,
    passive_contract,
)
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.keys import ResolvedMemoryScope
from qq_ai_bot.runtime.origin import TurnOrigin

if TYPE_CHECKING:
    from qq_ai_bot.domain.conversations import ConversationScope

_WRITE_ORIGINS = frozenset({TurnOrigin.USER_MESSAGE})
_MESSAGE_ORIGINS = frozenset({TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP})


class MemoryScopeResolver(Protocol):
    """Resolves the trusted memory partition for one turn."""

    async def resolve(
        self,
        *,
        authority: TurnAuthority,
        scene: TurnSceneFacts,
        conversation: ConversationScope | None,
    ) -> ResolvedMemoryScope: ...


def resolve_scope_from_scene(
    *, authority: TurnAuthority, scene: TurnSceneFacts
) -> ResolvedMemoryScope:
    """Pure default resolution from trusted scene facts.

    Group scenes map to the group partition; private scenes map to the
    trusted actor's private partition.  The actor id comes from
    ``TurnAuthority`` (host-built), never from message content.
    """

    if scene.scope_type is ScopeType.GROUP:
        assert scene.group_id is not None  # enforced by TurnSceneFacts
        return ResolvedMemoryScope.for_group(scene.group_id)
    return ResolvedMemoryScope.for_private(authority.actor_user_id)


class MemoryStructuredCommand(StrEnum):
    """Host-routed command kind.  Never derived from substring matching."""

    NONE = "none"
    READ = "read"
    WRITE = "write"


class MemoryAccessReason(StrEnum):
    """Content-free reason for the initial contract.  Safe to persist."""

    AUTHORITY_FORBIDDEN = "authority_forbidden"
    ORIGIN_RESTRICTED = "origin_restricted"
    STRUCTURED_WRITE_COMMAND = "structured_write_command"
    STRUCTURED_READ_COMMAND = "structured_read_command"
    ORDINARY_NATURAL_LANGUAGE = "ordinary_natural_language"
    IMAGE_WRITE_DISABLED = "image_write_disabled"
    ORIGIN_WRITE_DENIED = "origin_write_denied"


@dataclass(frozen=True, slots=True)
class MemoryAccessDecision:
    """Resolver output: a valid contract plus why it was chosen."""

    contract: MemoryTurnContract
    reason: MemoryAccessReason
    retrieval_degraded: bool = False


def origin_allows_persistent_write(origin: TurnOrigin) -> bool:
    """3.6.0: only user-message turns may persist memory writes by default."""

    return origin in _WRITE_ORIGINS


def resolve_memory_access(
    *,
    authority: TurnAuthority,
    scene: TurnSceneFacts,
    structured_command: MemoryStructuredCommand = MemoryStructuredCommand.NONE,
    memory_available: bool = True,
    retrieval_enabled: bool = True,
) -> MemoryAccessDecision:
    """Choose the initial memory contract from trusted host evidence.

    ``retrieval_enabled=false`` never becomes FORBIDDEN; it only marks the
    decision as retrieval-degraded so the query plane can use overview
    fallback.  Image turns keep readable context and deny write.
    """

    degraded = not retrieval_enabled
    if not memory_available:
        return MemoryAccessDecision(
            contract=forbidden_contract(MemoryRecallPurpose.BACKGROUND),
            reason=MemoryAccessReason.AUTHORITY_FORBIDDEN,
            retrieval_degraded=degraded,
        )

    origin = authority.origin
    write_allowed = origin_allows_persistent_write(origin) and not scene.image_present
    if origin not in _MESSAGE_ORIGINS:
        return MemoryAccessDecision(
            contract=dormant_contract(
                persistent_write_allowed=False,
            ),
            reason=MemoryAccessReason.ORIGIN_RESTRICTED,
            retrieval_degraded=degraded,
        )

    if structured_command is MemoryStructuredCommand.WRITE:
        if write_allowed:
            return MemoryAccessDecision(
                contract=exclusive_write_contract(),
                reason=MemoryAccessReason.STRUCTURED_WRITE_COMMAND,
                retrieval_degraded=degraded,
            )
        return MemoryAccessDecision(
            contract=_passive_for_scene(scene, persistent_write_allowed=False),
            reason=(
                MemoryAccessReason.IMAGE_WRITE_DISABLED
                if scene.image_present
                else MemoryAccessReason.ORIGIN_WRITE_DENIED
            ),
            retrieval_degraded=degraded,
        )

    if structured_command is MemoryStructuredCommand.READ:
        return MemoryAccessDecision(
            contract=active_read_contract(persistent_write_allowed=write_allowed),
            reason=MemoryAccessReason.STRUCTURED_READ_COMMAND,
            retrieval_degraded=degraded,
        )

    reason = MemoryAccessReason.ORDINARY_NATURAL_LANGUAGE
    if scene.image_present:
        reason = MemoryAccessReason.IMAGE_WRITE_DISABLED
    elif not origin_allows_persistent_write(origin):
        reason = MemoryAccessReason.ORIGIN_WRITE_DENIED
    return MemoryAccessDecision(
        contract=_passive_for_scene(scene, persistent_write_allowed=write_allowed),
        reason=reason,
        retrieval_degraded=degraded,
    )


def _passive_for_scene(
    scene: TurnSceneFacts, *, persistent_write_allowed: bool
) -> MemoryTurnContract:
    purpose = (
        MemoryRecallPurpose.CONTINUATION if scene.reply_present else MemoryRecallPurpose.BACKGROUND
    )
    return passive_contract(purpose, persistent_write_allowed=persistent_write_allowed)
