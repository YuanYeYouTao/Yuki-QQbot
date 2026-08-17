"""Deterministic quality suite, metric, gate, baseline, and report contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from qq_ai_bot.memory.quality.baseline import baseline_from_report, load_baseline, write_baseline
from qq_ai_bot.memory.quality.contracts import contract_catalog, validate_contract_snapshot
from qq_ai_bot.memory.quality.evaluator import MemoryQualityEvaluator
from qq_ai_bot.memory.quality.fake import QualityFakeModel
from qq_ai_bot.memory.quality.gates import (
    compare_baseline,
    evaluate_gates,
    load_gate_configuration,
)
from qq_ai_bot.memory.quality.loader import compute_dataset_hash, load_quality_suite
from qq_ai_bot.memory.quality.metrics import percentile, ratio
from qq_ai_bot.memory.quality.models import (
    MemoryQualityCase,
    QualityEvent,
    QualityMetricValue,
    QualityObservation,
    QualitySuiteMode,
)
from qq_ai_bot.memory.quality.report import write_reports
from qq_ai_bot.memory.quality.runner import MemoryQualityRunner
from qq_ai_bot.model_runtime.executor import LegacyTaskModelExecutor

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests/fixtures/memory_quality/v1"
GATES = ROOT / "config/memory_quality_gates.toml"


def test_dataset_is_strict_versioned_and_hash_stable() -> None:
    suite = load_quality_suite(FIXTURES)
    assert suite.manifest.schema_version == "1"
    assert suite.manifest.suite_version == "memory-v2-quality-v1"
    assert len(suite.cases) == 18
    assert suite.computed_hash == suite.manifest.dataset_hash
    assert suite.computed_hash == compute_dataset_hash(suite.cases)
    assert len({item.case_id for item in suite.cases}) == len(suite.cases)


def test_dataset_uses_only_synthetic_symbolic_identities() -> None:
    suite = load_quality_suite(FIXTURES)
    assert set(suite.manifest.symbolic_identities) == {
        "person_a",
        "person_b",
        "person_c",
        "group_a",
        "group_b",
        "bot",
    }
    serialized = json.dumps(
        [item.model_dump(mode="json") for item in suite.cases],
        ensure_ascii=False,
    )
    assert "SUPERUSERS" not in serialized
    assert "api_key" not in serialized.casefold()


def test_case_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryQualityCase.model_validate(
            {
                "case_id": "strict_case",
                "category": "identity",
                "description": "synthetic",
                "unexpected": True,
            }
        )


def test_loader_rejects_hash_mismatch(tmp_path: Path) -> None:
    destination = tmp_path / "v1"
    destination.mkdir()
    for source in FIXTURES.iterdir():
        destination.joinpath(source.name).write_bytes(source.read_bytes())
    manifest = destination / "manifest.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            load_quality_suite(FIXTURES).computed_hash,
            "0" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        load_quality_suite(destination)


def test_ratio_has_null_value_without_denominator() -> None:
    metric = ratio(0, 0)
    assert metric.value is None
    assert metric.numerator == 0
    assert metric.denominator == 0


def test_p95_is_null_until_tail_sample_is_statistically_meaningful() -> None:
    assert percentile([float(index) for index in range(19)], 0.95) is None
    assert percentile([float(index) for index in range(20)], 0.95) == 18.0


def test_evaluator_uses_exact_structured_comparison() -> None:
    case = MemoryQualityCase(
        case_id="exact_comparison",
        category="identity",
        description="synthetic",
        events=(
            QualityEvent(
                event_ref="event_exact",
                speaker="person_a",
                scope_type="private",
                content="synthetic",
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
        expected_claims=("one",),
    )
    result = MemoryQualityEvaluator().evaluate(
        case,
        QualityObservation(case_id=case.case_id, claims=("one-ish",)),
    )
    assert not result.passed
    assert result.failures == ("claims:missing:one", "claims:unexpected:one-ish")


def test_gate_configuration_is_external_and_strict() -> None:
    configuration = load_gate_configuration(GATES)
    assert configuration.max_absolute_drop == 0.01
    assert configuration.max_latency_ratio == 1.25
    assert configuration.max_model_request_ratio == 1.10
    assert len(configuration.gates) >= 20
    metrics = {
        item.metric: QualityMetricValue(
            value=item.threshold,
            numerator=item.threshold,
            denominator=1,
        )
        for item in configuration.gates
    }
    assert all(item.passed for item in evaluate_gates(metrics, configuration))


def test_gate_rejects_null_when_not_explicitly_allowed() -> None:
    configuration = load_gate_configuration(GATES)
    metrics = {
        item.metric: QualityMetricValue(value=None, numerator=0, denominator=0)
        for item in configuration.gates
    }
    assert not any(item.passed for item in evaluate_gates(metrics, configuration))


@pytest.mark.asyncio
async def test_full_quality_runner_passes_without_network(tmp_path: Path) -> None:
    suite = load_quality_suite(FIXTURES)
    gates = load_gate_configuration(GATES)
    report = await MemoryQualityRunner(
        suite=suite,
        gates=gates,
        repository_root=ROOT,
    ).run(mode=QualitySuiteMode.FULL)
    assert report.case_count == 18
    assert report.passed_count == 18
    assert report.failed_count == 0
    assert report.passed
    assert report.metrics["cross_person_contamination_rate"].value == 0
    assert report.metrics["cross_group_contamination_rate"].value == 0
    assert report.metrics["precision_at_k"].value == 1
    assert report.metrics["recall_at_k"].value == 1
    assert report.metrics["rebuild_receipt_accuracy"].value == 1
    json_path, markdown_path, junit_path = write_reports(tmp_path, report)
    assert json_path.exists() and markdown_path.exists() and junit_path.exists()
    assert "99000001" not in markdown_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_explicit_real_model_path_is_separate_from_deterministic_baseline() -> None:
    suite = load_quality_suite(FIXTURES)
    outputs: dict[str, tuple[dict[str, object], ...]] = {}
    for case in suite.cases:
        for event in case.events:
            outputs[event.content] = tuple(
                dict(
                    claim.model_dump(
                        mode="json",
                        exclude={"event_ref"},
                        exclude_none=True,
                    )
                )
                for claim in case.fake_model_outputs
                if claim.event_ref == event.event_ref
            )
    executor = LegacyTaskModelExecutor(QualityFakeModel(outputs), model="opt-in-test")
    report = await MemoryQualityRunner(
        suite=suite,
        gates=load_gate_configuration(GATES),
        repository_root=ROOT,
        model_executor=executor,
        model_provider_id="synthetic/opt-in-test",
    ).run(mode=QualitySuiteMode.PIPELINE)
    assert not report.deterministic
    assert report.model_provider_id == "synthetic/opt-in-test"
    assert report.embedding_provider_id is None
    assert report.case_count > 0


@pytest.mark.asyncio
async def test_baseline_round_trip_and_regression_detection(tmp_path: Path) -> None:
    suite = load_quality_suite(FIXTURES)
    gates = load_gate_configuration(GATES)
    report = await MemoryQualityRunner(
        suite=suite,
        gates=gates,
        repository_root=ROOT,
    ).run()
    path = tmp_path / "baseline.json"
    written = write_baseline(path, report)
    assert load_baseline(path) == written
    baseline = baseline_from_report(report).model_copy(
        update={
            "metrics": {
                **baseline_from_report(report).metrics,
                "recall_at_k": 1.0,
            }
        }
    )
    degraded = dict(report.metrics)
    degraded["recall_at_k"] = QualityMetricValue(
        value=0.9,
        numerator=9,
        denominator=10,
    )
    assert compare_baseline(degraded, baseline, gates) == ("recall_at_k:drop=0.1000",)


def test_contract_snapshot_freezes_plugin_api_v2() -> None:
    catalog = contract_catalog(ROOT / "config/memory_contracts.toml")
    assert catalog["plugin_api"] == {
        "version": "2.0",
        "memory_facade_methods": [
            "add",
            "delete",
            "list_group",
            "list_person",
            "search",
            "update",
        ],
        "forbidden_capabilities": [
            "raw_vectors",
            "memory_rebuild",
            "quality_dataset",
            "global_production_audit",
            "other_user_evidence",
            "provider_keys",
        ],
    }
    valid, _ = validate_contract_snapshot(
        ROOT / "config/memory_contracts.toml",
        ROOT / "tests/contracts/memory_v2/contracts.json",
    )
    assert valid
