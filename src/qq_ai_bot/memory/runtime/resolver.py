"""Trusted memory scope resolution.

The memory partition for a turn is derived from trusted host facts only —
never from model output.  R2 may extend resolution with evidence-based
narrowing; the pure default below matches the 3.5.3 memory worker partitions
(``group:{g}`` / ``private:{u}``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.keys import ResolvedMemoryScope

if TYPE_CHECKING:
    from qq_ai_bot.domain.conversations import ConversationIdentity


class MemoryScopeResolver(Protocol):
    """Resolves the trusted memory partition for one turn."""

    async def resolve(
        self,
        *,
        authority: TurnAuthority,
        scene: TurnSceneFacts,
        conversation: ConversationIdentity | None,
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
