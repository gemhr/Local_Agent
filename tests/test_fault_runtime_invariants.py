from dataclasses import FrozenInstanceError

import pytest

from core.runtime import (
    FaultRuntimeInvariantReport,
    build_fault_runtime_invariant_report,
)


def test_fault_runtime_invariant_report_passes_only_derived_safe_counts() -> None:
    report = build_fault_runtime_invariant_report()
    assert isinstance(report, FaultRuntimeInvariantReport)
    assert report.passed is True
    assert report.violation_codes == ()
    assert not hasattr(report, "run_context")
    assert not hasattr(report, "event_channel")
    assert not hasattr(report, "fault_controller")
    with pytest.raises(FrozenInstanceError):
        report.active_span_count = 1  # type: ignore[misc]


def test_fault_runtime_invariant_report_detects_identity_execution_and_cleanup() -> None:
    report = build_fault_runtime_invariant_report(
        run_context_count=2,
        business_rerun_count=1,
        cross_runtime_fallback_count=1,
        automatic_compensation_count=1,
        automatic_recovery_action_count=1,
        terminal_journal_count=2,
        terminal_channel_count=2,
        sequence_reuse_count=1,
        active_span_count=1,
        registry_handle_count=1,
        request_producer_count=1,
        active_reservation_count=1,
        active_permit_count=1,
        detached_worker_count=1,
    )
    assert report.passed is False
    assert "FAULT_INVARIANT_RUN_CONTEXT_COUNT" in report.violation_codes
    assert "FAULT_INVARIANT_BUSINESS_RERUN_COUNT" in report.violation_codes
    assert "FAULT_INVARIANT_TERMINAL_JOURNAL_COUNT" in report.violation_codes
    assert "FAULT_INVARIANT_ACTIVE_SPAN_COUNT" in report.violation_codes
    assert not any("DETACHED_WORKER" in code for code in report.violation_codes)


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_fault_runtime_invariant_report_rejects_non_counter_values(value) -> None:
    with pytest.raises(ValueError):
        build_fault_runtime_invariant_report(
            active_span_count=value  # type: ignore[arg-type]
        )
