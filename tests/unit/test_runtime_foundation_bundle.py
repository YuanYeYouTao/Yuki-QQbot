"""RuntimeFoundationBundle construction and freeze barrier (R1 §9)."""

from __future__ import annotations

from qq_ai_bot.application.modules.runtime_foundation import RuntimeFoundationModule
from qq_ai_bot.application.provider_registry import ProviderRegistry
from qq_ai_bot.persistence.turn_observations import RuntimeTurnObservationRepository
from qq_ai_bot.runtime.errors import (
    ProviderRegistryFrozenError,
    ProviderRegistryNotFrozenError,
)
from qq_ai_bot.runtime.origin import TurnOrigin


def test_bundle_exposes_host_authority_and_empty_core_factory(database) -> None:
    bundle = RuntimeFoundationModule(
        turn_observability=RuntimeTurnObservationRepository(database),
        superusers=("1001",),
    ).build()
    assert bundle.turn_runtime_core_factory is None
    assert bundle.authority_factory.is_superuser("1001")
    assert not bundle.authority_factory.is_superuser("2002")
    authority = bundle.authority_factory.build(
        actor_user_id="1001",
        bot_user_id="8000",
        origin=TurnOrigin.USER_MESSAGE,
        permission_ceiling=frozenset({"web.search"}),
    )
    assert authority.permission_ceiling == frozenset({"web.search"})
    assert authority.origin is TurnOrigin.USER_MESSAGE


def test_provider_registry_rejects_reads_before_freeze_and_writes_after() -> None:
    registry = ProviderRegistry()
    registry.register("observability", object())
    try:
        registry.get("observability")
    except ProviderRegistryNotFrozenError:
        pass
    else:
        raise AssertionError("lookup must fail before freeze")
    assert registry.freeze() == 1
    assert registry.get("observability") is not None
    try:
        registry.register("late", object())
    except ProviderRegistryFrozenError:
        return
    raise AssertionError("registration must fail after freeze")
