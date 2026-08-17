"""Low-cardinality capability runtime observations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass(slots=True)
class CapabilityRuntimeMetrics:
    searches: int = 0
    zero_results: int = 0
    index_rebuilds: int = 0
    request_tools_calls: int = 0
    schema_conflicts: int = 0
    unauthorized_calls: int = 0
    namespaces_hit: Counter[str] = field(default_factory=Counter)

    def record_search(self, *, hits: int, namespace_ids: tuple[str, ...] = ()) -> None:
        self.searches += 1
        if hits <= 0:
            self.zero_results += 1
        for namespace_id in namespace_ids:
            self.namespaces_hit[namespace_id] += 1

    def record_rebuild(self) -> None:
        self.index_rebuilds += 1
