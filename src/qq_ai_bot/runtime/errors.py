"""Errors raised by the runtime turn domain."""

from __future__ import annotations


class RuntimeDomainError(Exception):
    """Base class for all runtime turn domain errors."""


class InvalidTurnTriggerError(RuntimeDomainError):
    """A turn trigger was constructed with inconsistent fields."""


class InvalidTurnContextError(RuntimeDomainError):
    """A turn context violated a construction invariant."""


class IllegalTurnTransitionError(RuntimeDomainError):
    """A turn phase transition outside the legal transition table."""

    def __init__(self, current: str, requested: str, detail: str = "") -> None:
        message = f"illegal turn transition: {current} -> {requested}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.current = current
        self.requested = requested


class DurableEffectViolationError(RuntimeDomainError):
    """A turn tried to regress or misreport its durable effect state."""


class UntrustedFinalizationError(RuntimeDomainError):
    """Terminal finalization metadata came from an untrusted source."""


class ProviderRegistryFrozenError(RuntimeDomainError):
    """Registration attempted after the provider registry was frozen."""


class ProviderRegistryNotFrozenError(RuntimeDomainError):
    """Lookup attempted before the provider registry was frozen."""
