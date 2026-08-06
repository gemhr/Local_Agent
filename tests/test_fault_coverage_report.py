from __future__ import annotations

from core.runtime import build_fault_coverage_report


def test_fault_coverage_counts_only_supported_points_with_test_evidence():
    report = build_fault_coverage_report()

    assert report.total_fault_points == 53
    assert report.supported_count == 44
    assert report.contract_only_count == 9
    assert report.not_applicable_count == 0
    assert report.tested_supported_count == 44
    assert report.untested_supported_count == 0
    assert report.dangerous_supported_count == 8
    assert report.fully_covered is True


def test_fault_coverage_cross_cutting_evidence_is_explicit():
    report = build_fault_coverage_report()

    assert report.disabled_parity_covered is True
    assert report.cancellation_covered is True
    assert report.concurrency_covered is True
    assert report.partial_persistence_covered is True
    assert report.security_covered is True
    assert "rule_id" not in repr(report)
    assert "tests\\" not in repr(report)
