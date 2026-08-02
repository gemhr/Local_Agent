from pathlib import Path

from tests._runtime_invariants import build_runtime_invariant_report
from tests._runtime_release_gate import assess_release_gate


ROOT = Path(__file__).parents[1]
DOC = ROOT / "docs/learning/stage2/result/day25_stage2_final_acceptance_result.md"


def test_final_acceptance_has_exact_numbered_sections_and_truthful_scope() -> None:
    text = DOC.read_text(encoding="utf-8")
    for number in range(1, 33):
        assert f"## {number}. " in text
    assert "Stage2 Runtime RC1 code-level gate PASS" in text
    assert "生产验证仍未完成" in text
    assert "Production Ready Certified" not in text
    assert "Fault Rule ID" not in text


def test_final_gate_includes_every_required_hard_condition() -> None:
    result = assess_release_gate(
        scenario_results={f"RC-{index:02d}": True for index in range(1, 21)},
        p2_findings=("P2-01",), known_limitations=tuple(f"KL-{i:02d}" for i in range(1, 8)),
        contract_tests_passed=True, operations_docs_tests_passed=True,
        full_suite_passed=True, resource_invariants_passed=True, security_scan_passed=True,
    )
    assert result.release_gate_status == "PASS"
    assert result.rc_scenarios_passed == result.rc_scenarios_required == 20
    assert result.operations_docs_tests_passed is True


def test_final_invariant_report_is_counts_only_and_detects_detached_truth() -> None:
    # A terminal event is exercised by the existing runtime invariant suite; this final
    # audit verifies the extended resource fields and fixed-code diagnostics.
    report = build_runtime_invariant_report([], detached_worker_count=1)
    assert report.detached_worker_count == 1
    assert "detached_worker_count" in report.violations
    assert not hasattr(report, "runtime") and not hasattr(report, "run_context")
