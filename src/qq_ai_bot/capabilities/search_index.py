"""Capability search index contract (R1 shape, R3 implementation).

The index may cover the full catalog; discovery never grants authority.
Callers must intersect every hit with the turn's authority-filtered
requestable capability ids before exposing anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CapabilitySearchDocument:
    """Content-free searchable projection of one capability."""

    capability_id: str
    namespace_id: str
    canonical_name: str
    summary: str = ""
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilitySearchHit:
    """One ranked hit; carries ids and score only, never schemas."""

    capability_id: str
    namespace_id: str
    score: float


class CapabilitySearchIndex(Protocol):
    """Lexical/alias search over the capability catalog."""

    @property
    def revision(self) -> int | None:
        """Catalog revision the index was built from; ``None`` before build."""
        ...

    def rebuild(self, *, revision: int, documents: Sequence[CapabilitySearchDocument]) -> None:
        """Atomically replace the index contents for a new catalog revision."""
        ...

    def search(self, query: str, *, limit: int) -> tuple[CapabilitySearchHit, ...]: ...
