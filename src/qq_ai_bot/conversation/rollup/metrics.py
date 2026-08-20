"""Low-cardinality process metrics for rollup diagnostics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ConversationRollupMetrics:
    jobs_claimed: int = 0
    model_summaries: int = 0
    extractive_fallbacks: int = 0
    coverage_commits: int = 0
    lease_conflicts: int = 0
    source_conflicts: int = 0
    infrastructure_retries: int = 0
    late_visual_after_coverage: int = 0
    foreground_batches: int = 0
    counter_repairs: int = 0
    counter_reconcile_failures: int = 0
    scoped_append_repairs: int = 0
