from types import SimpleNamespace

import pytest

from core.capacity import capacity_model, evaluate_slo, percentile
from core.runtime.metrics import InMemoryMetricsRecorder


def test_percentile_uses_nearest_rank() -> None:
    assert percentile([7], 50) == 7
    assert percentile([4, 1, 3, 2], 50) == 2
    assert percentile([4, 1, 3, 2], 99) == 4
    samples = list(range(1, 101))
    assert percentile(samples, 50) == 50
    assert percentile(samples, 95) == 95
    assert percentile(samples, 99) == 99


def test_capacity_model_reads_existing_settings_without_new_authority() -> None:
    settings = SimpleNamespace(model_threads=10, db_pool_size=5, db_max_overflow=5, blocking_max_workers=4, blocking_max_pending_tasks=8, redis_max_connections=10)
    model = {item.resource: item for item in capacity_model(settings)}
    assert model["DB connections"].capacity == "5 pool + 5 overflow"
    assert model["Tool execution"].capacity.startswith("16 global")
    assert model["Active Runs"].capacity == "4 supervised producers"


def test_slo_evaluator_distinguishes_insufficient_evidence_and_failure() -> None:
    spec = {"required_evidence_profile": "PLATFORM_RUNTIME", "required_scenario": "runtime_run_start", "metric": "success_rate", "objective": "at_least", "threshold": 0.99, "minimum_samples": 100}
    valid = {"evidence_profile": "PLATFORM_RUNTIME", "scenario": "runtime_run_start"}
    assert evaluate_slo(spec, {**valid, "attempted": 20, "succeeded": 20})["status"] == "NOT_ENOUGH_EVIDENCE"
    assert evaluate_slo(spec, {**valid, "attempted": 100, "succeeded": 98})["status"] == "FAIL"
    assert evaluate_slo(spec, {**valid, "attempted": 100, "succeeded": 100})["status"] == "PASS"


def test_slo_evaluator_does_not_pass_missing_latency_metric() -> None:
    spec = {"metric": "latency_ms.p95", "objective": "at_most", "threshold": 500, "minimum_samples": 1}
    result = evaluate_slo(spec, {"attempted": 1})
    assert result == {"status": "NOT_ENOUGH_EVIDENCE", "reason": "missing metric latency_ms.p95"}


@pytest.mark.parametrize(
    ("metric", "reason"),
    (("success_rate", "missing metric success_rate"), ("error_rate", "missing metric error_rate")),
)
def test_slo_evaluator_rejects_missing_rate_metric(metric: str, reason: str) -> None:
    result = evaluate_slo(
        {"metric": metric, "objective": "at_most", "threshold": 1, "minimum_samples": 1},
        {"attempted": 1},
    )
    assert result == {"status": "NOT_ENOUGH_EVIDENCE", "reason": reason}


def test_runtime_slo_rejects_synthetic_or_wrong_scenario_evidence() -> None:
    spec = {"required_evidence_profile": "PLATFORM_RUNTIME", "required_scenario": "runtime_run_start", "metric": "success_rate", "objective": "at_least", "threshold": 0.99, "minimum_samples": 100}
    synthetic = {"evidence_profile": "SYNTHETIC", "scenario": "synthetic_start", "attempted": 100, "succeeded": 100}
    wrong_scenario = {**synthetic, "evidence_profile": "PLATFORM_RUNTIME"}
    assert evaluate_slo(spec, synthetic)["status"] == "NOT_ENOUGH_EVIDENCE"
    assert evaluate_slo(spec, wrong_scenario)["status"] == "NOT_ENOUGH_EVIDENCE"


def test_client_feed_metrics_are_registered() -> None:
    recorder = InMemoryMetricsRecorder()
    recorder.increment_counter("runtime_client_event_feed_write_total")
    recorder.increment_counter("runtime_client_event_feed_write_failures_total")


@pytest.mark.parametrize("bad", [[], [1]])
def test_percentile_rejects_insufficient_or_invalid_input(bad) -> None:
    if not bad:
        with pytest.raises(ValueError): percentile(bad, 50)
    else:
        with pytest.raises(ValueError): percentile(bad, 0)
