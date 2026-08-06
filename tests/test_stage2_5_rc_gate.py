"""Stage 2.5 WP6 RC Gate: aggregate real mappings, catalog counts and checks.

The gate aggregates instead of re-implementing business logic: every required
RC scenario and every required fault-catalog entry must map to a real test,
and the fault support report must agree with the WP6 catalog.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.runtime import (
    FaultPointSupportStatus,
    build_fault_coverage_report,
    build_fault_point_support_report,
)
from tests._runtime_release_gate import assess_release_gate
from tests._stage2_5_wp6_catalog import (
    RC_SCENARIOS_25,
    STAGE25_FAULT_CATALOG,
    assert_catalog_test_ids_exist,
    assert_real_test_mappings,
    fault_catalog_counts,
    required_catalog_entries,
    _catalog_test_file,
)


ROOT = Path(__file__).parents[1]
WP6_DOC = ROOT / (
    "docs/learning/stage2/result/stage2_5_wp6_final_acceptance_result.md"
)


def _scenario_results() -> dict[str, bool]:
    return {scenario.scenario_id: True for scenario in RC_SCENARIOS_25}


def test_all_required_rc_scenarios_map_to_real_tests() -> None:
    for scenario in RC_SCENARIOS_25:
        assert_real_test_mappings(scenario.scenario_id)
    assert len(RC_SCENARIOS_25) >= 30


def test_catalog_counts_match_support_report_and_required_supported() -> None:
    counts = fault_catalog_counts()
    report = build_fault_point_support_report()

    assert counts["total"] == len(STAGE25_FAULT_CATALOG)
    assert counts["required"] > 0
    # WP6 必选 fault point 必须为 SUPPORTED 或 CONTRACT_ONLY 且有测试证据。
    required = required_catalog_entries()
    assert all(
        entry.support_status
        in {
            FaultPointSupportStatus.SUPPORTED,
            FaultPointSupportStatus.CONTRACT_ONLY,
        }
        and entry.test_ids
        for entry in required
    )
    # 全量 FaultPoint 支持报告必须包含 WP6 新缝。
    assert report.total_fault_points == 53
    assert report.supported_count == 44
    assert report.contract_only_count == 9
    assert report.not_applicable_count == 0
    coverage = build_fault_coverage_report(report)
    assert coverage.tested_supported_count == coverage.supported_count
    assert coverage.fully_covered is True


def test_all_catalog_test_ids_reference_real_files() -> None:
    assert_catalog_test_ids_exist()


def test_required_supported_fault_points_have_deterministic_tests() -> None:
    supported_required = [
        entry
        for entry in STAGE25_FAULT_CATALOG
        if entry.required
        and entry.support_status is FaultPointSupportStatus.SUPPORTED
    ]
    assert supported_required
    for entry in supported_required:
        for test_id in entry.test_ids:
            assert _catalog_test_file(test_id).is_file(), (
                f"{entry.catalog_id} 缺少测试 {_catalog_test_file(test_id)}"
            )


def test_rc_gate_pass_is_derived_from_actual_checks() -> None:
    report = assess_release_gate(
        scenario_results=_scenario_results(),
        p0_blockers=(),
        p1_blockers=(),
        p2_findings=("P2-01",),
        known_limitations=("KL-01",),
        contract_tests_passed=True,
        operations_docs_tests_passed=True,
        full_suite_passed=True,
        resource_invariants_passed=True,
        security_scan_passed=True,
    )

    assert report.release_gate_status == "PASS"
    assert (
        report.rc_scenarios_passed
        == report.rc_scenarios_required
        == len(RC_SCENARIOS_25)
    )
    assert not report.p0_blockers and not report.p1_blockers
    assert "tests/" not in repr(report)


@pytest.mark.parametrize(
    "change",
    (
        {"p0_blockers": ("P0-01",)},
        {"p1_blockers": ("P1-01",)},
        {"full_suite_passed": False},
        {"contract_tests_passed": False},
        {"operations_docs_tests_passed": False},
        {"resource_invariants_passed": False},
        {"security_scan_passed": False},
        {"scenario_results": {"RC25-S-01": False, "RC25-S-02": False}},
    ),
)
def test_each_hard_gate_failure_forces_fail(change) -> None:
    values = {
        "scenario_results": _scenario_results(),
        "contract_tests_passed": True,
        "operations_docs_tests_passed": True,
        "full_suite_passed": True,
        "resource_invariants_passed": True,
        "security_scan_passed": True,
    }
    values.update(change)
    assert assess_release_gate(**values).release_gate_status == "FAIL"


def test_wp6_doc_final_status_block_is_truthful_and_complete() -> None:
    text = WP6_DOC.read_text(encoding="utf-8")
    assert "WP6 status: PASS" in text
    assert "Stage 2.5 final status: PASS" in text
    assert "P0 findings: 0" in text
    assert "P1 findings: 0" in text
    assert "P2 findings: 1" in text
    assert "Architecture deviations: 0" in text
    assert "RC Gate: PASS" in text
    assert "Ready to freeze Stage 2.5: YES" in text
    assert "Planning executor starvation: ACCEPTED_P2" in text
    assert "Ready for GPT final review: YES" in text
    assert "exactly-once" not in text.casefold() or "不承诺" in text
