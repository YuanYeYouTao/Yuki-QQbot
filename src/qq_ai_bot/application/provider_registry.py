"""Freezable provider registry (R1 §9).

Modules register their providers during container construction; a lifecycle
hook freezes the registry into an immutable revision before any turn runs.
After freezing, registration fails; before freezing, lookups fail — this
prevents both late mutation and service-locator-style early reads.
"""

from __future__ import annotations

from qq_ai_bot.runtime.errors import (
    ProviderRegistryFrozenError,
    ProviderRegistryNotFrozenError,
)


class ProviderRegistry:
    """Named provider registry with an atomic freeze barrier."""

    __slots__ = ("_providers", "_revision")

    def __init__(self) -> None:
        self._providers: dict[str, object] = {}
        self._revision: int | None = None

    @property
    def frozen(self) -> bool:
        return self._revision is not None

    @property
    def revision(self) -> int | None:
        return self._revision

    def register(self, name: str, provider: object) -> None:
        if self._revision is not None:
            raise ProviderRegistryFrozenError(
                f"registry frozen at revision {self._revision}; cannot register {name!r}"
            )
        if not name:
            raise ValueError("provider name must not be empty")
        if name in self._providers:
            raise ValueError(f"provider {name!r} already registered")
        self._providers[name] = provider

    def freeze(self) -> int:
        """Freeze the registry exactly once and return its revision."""

        if self._revision is not None:
            raise ProviderRegistryFrozenError(
                f"registry already frozen at revision {self._revision}"
            )
        self._revision = 1
        return self._revision

    def get(self, name: str) -> object:
        if self._revision is None:
            raise ProviderRegistryNotFrozenError(
                f"registry not frozen yet; cannot read {name!r} during construction"
            )
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"unknown provider {name!r}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))
