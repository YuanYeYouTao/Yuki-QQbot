"""Content-free observability for Memory V2 retrieval."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from qq_ai_bot.memory.enums import (
    MemoryContextMode,
    MemoryRecallPurpose,
    MemoryRetrievalMode,
)

logger = logging.getLogger(__name__)

OPERATIONAL_RETRIEVAL_COUNTERS = (
    "memory_context_fact_count",
    "memory_context_target_count",
    "memory_retrieval_empty_count",
    "memory_retrieval_fts_count",
    "memory_retrieval_hybrid_count",
    "memory_retrieval_semantic_count",
)

OPERATIONAL_LIFECYCLE_COUNTERS = (
    "memory_audit_issue_count",
    "memory_contested_context_suppressed",
    "memory_cross_target_rejections",
    "memory_fact_state_transitions",
    "memory_hygiene_invalidated_count",
    "memory_live_claims",
    "memory_unknown_subject_rejections",
)

ADAPTIVE_MEMORY_STAGES = ("candidate", "selected", "injected", "used", "reinforced")
ADAPTIVE_USAGE_REPORT_OUTCOMES = ("valid", "empty", "missing", "invalid")
ADAPTIVE_REINFORCEMENT_SKIP_REASONS = (
    "disabled",
    "activation_unavailable",
    "alpha_zero",
    "not_used",
    "fact_ineligible",
)
ADAPTIVE_ACTIVATION_BUCKETS = ("0_025", "025_050", "050_075", "075_100")


@dataclass(frozen=True, slots=True)
class MemoryRetrievalMetric:
    mode: MemoryRetrievalMode
    query_hash: str
    target_count: int
    candidate_count: int
    selected_count: int
    context_selected_count: int
    fts_latency: float
    total_latency: float
    overview_used: bool
    short_query_fallback_used: bool
    referenced_person_count: int
    semantic_candidate_count: int = 0
    semantic_selected_count: int = 0
    hybrid_selected_count: int = 0
    semantic_degraded: bool = False
    semantic_search_latency: float = 0
    hybrid_rank_latency: float = 0
    intent_rerank_latency: float = 0


class MemoryRetrievalMetrics:
    """Retain only the latest redacted metric and emit counts to debug logs."""

    def __init__(self) -> None:
        self._latest: MemoryRetrievalMetric | None = None
        self._counts: Counter[str] = Counter()

    @property
    def latest(self) -> MemoryRetrievalMetric | None:
        return self._latest

    def record(self, metric: MemoryRetrievalMetric) -> None:
        self._latest = metric
        self._counts["context_target_count"] += metric.target_count
        self._counts["context_fact_count"] += metric.context_selected_count
        if metric.selected_count == 0:
            self._counts["retrieval_empty"] += 1
        elif metric.hybrid_selected_count:
            self._counts["retrieval_hybrid"] += 1
        elif metric.semantic_selected_count:
            self._counts["retrieval_semantic"] += 1
        else:
            self._counts["retrieval_fts"] += 1
        logger.debug(
            "memory_retrieval mode=%s query_hash=%s targets=%d candidates=%d "
            "selected=%d context_selected=%d fts_latency=%.6f total_latency=%.6f "
            "overview=%s short_fallback=%s referenced_people=%d semantic_candidates=%d "
            "semantic_selected=%d hybrid_selected=%d semantic_degraded=%s "
            "semantic_latency=%.6f hybrid_latency=%.6f rerank_latency=%.6f",
            metric.mode.value,
            metric.query_hash,
            metric.target_count,
            metric.candidate_count,
            metric.selected_count,
            metric.context_selected_count,
            metric.fts_latency,
            metric.total_latency,
            metric.overview_used,
            metric.short_query_fallback_used,
            metric.referenced_person_count,
            metric.semantic_candidate_count,
            metric.semantic_selected_count,
            metric.hybrid_selected_count,
            metric.semantic_degraded,
            metric.semantic_search_latency,
            metric.hybrid_rank_latency,
            metric.intent_rerank_latency,
        )

    def operational_snapshot(self) -> dict[str, int]:
        values = {
            "memory_context_fact_count": self._counts["context_fact_count"],
            "memory_context_target_count": self._counts["context_target_count"],
            "memory_retrieval_empty_count": self._counts["retrieval_empty"],
            "memory_retrieval_fts_count": self._counts["retrieval_fts"],
            "memory_retrieval_hybrid_count": self._counts["retrieval_hybrid"],
            "memory_retrieval_semantic_count": self._counts["retrieval_semantic"],
        }
        return {name: int(values[name]) for name in OPERATIONAL_RETRIEVAL_COUNTERS}

    def record_context_selected(self, metric: MemoryRetrievalMetric) -> None:
        """Update the latest query projection without counting the query twice."""

        previous = self._latest.context_selected_count if self._latest is not None else 0
        self._latest = metric
        self._counts["context_fact_count"] += max(
            0,
            metric.context_selected_count - previous,
        )


class MemoryLifecycleMetrics:
    """Content-free counters and timestamps shared by consolidation and maintenance."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self.classifier_recent_errors = 0
        self.maintenance_last_success_at: datetime | None = None

    def increment(self, name: str, count: int = 1) -> None:
        self._counts[name] += count

    def count(self, name: str) -> int:
        return self._counts[name]

    def operational_snapshot(self) -> dict[str, int]:
        transitions = sum(
            self._counts[name]
            for name in (
                "facts_confirmed",
                "facts_contested",
                "facts_invalidated",
                "facts_merged",
                "facts_restored",
                "facts_superseded",
            )
        )
        values = {
            "memory_audit_issue_count": self._counts["audit_issue_count"],
            "memory_contested_context_suppressed": self._counts["contested_context_suppressed"],
            "memory_cross_target_rejections": self._counts["cross_target_rejections"],
            "memory_fact_state_transitions": transitions,
            "memory_hygiene_invalidated_count": self._counts["hygiene_invalidated_count"],
            "memory_live_claims": self._counts["claims_extracted"],
            "memory_unknown_subject_rejections": self._counts["unknown_subject_rejections"],
        }
        return {name: int(values[name]) for name in OPERATIONAL_LIFECYCLE_COUNTERS}

    def adaptive_snapshot(self) -> dict[str, int]:
        """Return a fixed-cardinality, content-free lifecycle metric projection."""

        names = [
            *(f"memory_intent_mode_{mode.value}" for mode in MemoryContextMode),
            *(f"memory_intent_purpose_{purpose.value}" for purpose in MemoryRecallPurpose),
            *(f"memory_recall_{stage}_count" for stage in ADAPTIVE_MEMORY_STAGES),
            *(f"memory_usage_report_{outcome}_count" for outcome in ADAPTIVE_USAGE_REPORT_OUTCOMES),
            *(
                f"memory_reinforcement_skipped_{reason}_count"
                for reason in ADAPTIVE_REINFORCEMENT_SKIP_REASONS
            ),
            *(f"memory_activation_bucket_{bucket}_count" for bucket in ADAPTIVE_ACTIVATION_BUCKETS),
            "memory_activation_state_missing_count",
            "memory_recall_receipts_cleaned_count",
            "memory_usage_report_extra_model_request_count",
        ]
        return {name: int(self._counts[name]) for name in names}

    def record_intent(self, *, mode: MemoryContextMode, purpose: MemoryRecallPurpose) -> None:
        self.increment(f"memory_intent_mode_{mode.value}")
        self.increment(f"memory_intent_purpose_{purpose.value}")

    def record_recall_stage(self, stage: str, count: int) -> None:
        if stage not in ADAPTIVE_MEMORY_STAGES:
            raise ValueError(f"unsupported memory recall stage: {stage}")
        self.increment(f"memory_recall_{stage}_count", count)

    def record_usage_report(self, outcome: str) -> None:
        if outcome not in ADAPTIVE_USAGE_REPORT_OUTCOMES:
            raise ValueError(f"unsupported memory usage report outcome: {outcome}")
        self.increment(f"memory_usage_report_{outcome}_count")

    def record_reinforcement_skip(self, reason: str, count: int = 1) -> None:
        if reason not in ADAPTIVE_REINFORCEMENT_SKIP_REASONS:
            raise ValueError(f"unsupported reinforcement skip reason: {reason}")
        self.increment(f"memory_reinforcement_skipped_{reason}_count", count)

    def record_activation(self, value: float) -> None:
        if value < 0.25:
            bucket = "0_025"
        elif value < 0.5:
            bucket = "025_050"
        elif value < 0.75:
            bucket = "050_075"
        else:
            bucket = "075_100"
        self.increment(f"memory_activation_bucket_{bucket}_count")

    def record_classifier_error(self) -> None:
        self.classifier_recent_errors += 1

    def record_maintenance_success(self, occurred_at: datetime) -> None:
        self.maintenance_last_success_at = occurred_at
