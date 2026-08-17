"""R1 runtime foundation bundle: observability, authority, provider registry.

Constructs the protocol/factory objects later rounds implement.  The bundle
is held by ``ApplicationContainer`` but is not wired into the 3.5.3 message
path — the only production change in R1 is turn correlation plus observation
rows, which already flow through ``PersistenceBundle.turn_observations``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from qq_ai_bot.application.provider_registry import ProviderRegistry
from qq_ai_bot.conversation.runtime import TurnRuntimeCoreFactory
from qq_ai_bot.persistence.turn_observations import RuntimeTurnObservationRepository
from qq_ai_bot.runtime.authority import DelegatedAuthoritySnapshot, TurnAuthority
from qq_ai_bot.runtime.origin import TurnOrigin


class TurnAuthorityFactory:
    """Host-only factory for ``TurnAuthority``.

    Superuser membership comes from trusted settings, never from model-visible
    fields.  ``permission_ceiling`` is supplied by the caller after intersecting
    scene/policy facts; this factory only records it.
    """

    def __init__(self, *, superusers: Iterable[str], authority_revision: int = 1) -> None:
        self._superusers = frozenset(superusers)
        self._authority_revision = authority_revision

    def is_superuser(self, user_id: str) -> bool:
        return user_id in self._superusers

    def build(
        self,
        *,
        actor_user_id: str,
        bot_user_id: str,
        origin: TurnOrigin,
        permission_ceiling: frozenset[str],
        delegated_authority: DelegatedAuthoritySnapshot | None = None,
    ) -> TurnAuthority:
        return TurnAuthority(
            actor_user_id=actor_user_id,
            bot_user_id=bot_user_id,
            origin=origin,
            permission_ceiling=permission_ceiling,
            delegated_authority=delegated_authority,
            authority_revision=self._authority_revision,
        )


@dataclass(frozen=True, slots=True)
class RuntimeFoundationBundle:
    turn_observability: RuntimeTurnObservationRepository
    authority_factory: TurnAuthorityFactory
    provider_registry: ProviderRegistry
    turn_runtime_core_factory: TurnRuntimeCoreFactory | None


class RuntimeFoundationModule:
    def __init__(
        self,
        *,
        turn_observability: RuntimeTurnObservationRepository,
        superusers: Iterable[str],
        provider_registry: ProviderRegistry | None = None,
        turn_runtime_core_factory: TurnRuntimeCoreFactory | None = None,
    ) -> None:
        self._turn_observability = turn_observability
        self._superusers = superusers
        self._provider_registry = provider_registry
        self._turn_runtime_core_factory = turn_runtime_core_factory

    def build(self) -> RuntimeFoundationBundle:
        return RuntimeFoundationBundle(
            turn_observability=self._turn_observability,
            authority_factory=TurnAuthorityFactory(superusers=self._superusers),
            provider_registry=self._provider_registry or ProviderRegistry(),
            turn_runtime_core_factory=self._turn_runtime_core_factory,
        )
