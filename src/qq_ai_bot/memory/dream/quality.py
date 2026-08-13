"""Shared deterministic quality limits for Dream decisions and mutations."""

from __future__ import annotations


def episode_compression_limit(
    source_characters: int,
    *,
    ratio: float,
    maximum: int,
) -> int:
    """Return one bounded total-output budget for an Episode recompose."""

    return max(1, min(maximum, int(source_characters * ratio)))
