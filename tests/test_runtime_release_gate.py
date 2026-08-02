import pytest

from tests._runtime_rc_manifest import RC_SCENARIOS, assert_real_test_mappings
from tests._runtime_release_gate import assess_release_gate


def test_gate_pass_is_derived_from_actual_checks_and_fixed_ids() -> None:
    scenario_results = {}
    for scenario in RC_SCENARIOS:
        assert_real_test_mappings(scenario.scenario_id)
        scenario_results[scenario.scenario_id] = True

    report = assess_release_gate(
        scenario_results=scenario_results,
        p2_findings=("P2-01",),
        known_limitations=("KL-01", "KL-02"),
        contract_tests_passed=True,
        operations_docs_tests_passed=True,
        full_suite_passed=True,
        resource_invariants_passed=True,
        security_scan_passed=True,
    )

    assert report.release_gate_status == "PASS"
    assert report.rc_scenarios_passed == report.rc_scenarios_required == 20
    assert "tests/" not in repr(report)


@pytest.mark.parametrize(
    "change",
    (
        {"p0_blockers": ("P0-01",)},
        {"p1_blockers": ("P1-01",)},
        {"full_suite_passed": False},
        {"operations_docs_tests_passed": False},
        {"resource_invariants_passed": False},
        {"security_scan_passed": False},
    ),
)
def test_each_hard_gate_failure_forces_fail(change) -> None:
    values = {
        "scenario_results": {"RC-01": True},
        "contract_tests_passed": True,
        "operations_docs_tests_passed": True,
        "full_suite_passed": True,
        "resource_invariants_passed": True,
        "security_scan_passed": True,
    }
    values.update(change)
    assert assess_release_gate(**values).release_gate_status == "FAIL"


def test_gate_rejects_paths_or_raw_errors_as_finding_ids() -> None:
    with pytest.raises(ValueError, match="fixed safe IDs"):
        assess_release_gate(
            scenario_results={"RC-01": True},
            p0_blockers=(r"C:\private\provider-error",),
            contract_tests_passed=True,
            operations_docs_tests_passed=True,
            full_suite_passed=True,
            resource_invariants_passed=True,
            security_scan_passed=True,
        )
