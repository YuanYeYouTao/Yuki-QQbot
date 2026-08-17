"""Errors raised by the memory turn runtime.

``MemoryContractError`` stays a ``ValueError`` so existing contract tests that
catch ``ValueError`` keep working.  Transition errors are not ValueErrors:
they are illegal session moves, not bad composition.
"""

from __future__ import annotations


class MemoryRuntimeError(Exception):
    """Base class for memory-runtime failures."""


class MemoryContractError(MemoryRuntimeError, ValueError):
    """A memory contract violated a composition or authority rule."""


class IllegalMemoryTransitionError(MemoryRuntimeError):
    """A session machine refused a phase or state move."""

    def __init__(self, current: str, requested: str, detail: str = "") -> None:
        message = f"illegal memory transition: {current} -> {requested}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.current = current
        self.requested = requested


class MemorySessionClosedError(MemoryRuntimeError):
    """The memory session was used after ``close()``."""


class MemoryLocatorRetryExhaustedError(MemoryRuntimeError):
    """A second locator-read retry was requested after the one allowed attempt."""
