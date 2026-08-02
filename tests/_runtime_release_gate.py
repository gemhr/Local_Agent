from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


_SAFE_ID = re.compile(r"^(?:P[0-2]|KL)-[0-9]{2}$")


@dataclass(frozen=True, slots=True)
class ReleaseGateAssessment:
    p0_blockers: tuple[str, ...]
    p1_blockers: tuple[str, ...]
    p2_findings: tuple[str, ...]
    known_limitations: tuple[str, ...]
    contract_tests_passed: bool
    operations_docs_tests_passed: bool
    rc_scenarios_passed: int
    rc_scenarios_required: int
    full_suite_passed: bool
    resource_invariants_passed: bool
    security_scan_passed: bool
    release_gate_status: str

    def __post_init__(self) -> None:
        ids = self.p0_blockers + self.p1_blockers + self.p2_findings + self.known_limitations
        if any(_SAFE_ID.fullmatch(item) is None for item in ids):
            raise ValueError("release gate findings must contain fixed safe IDs only")
        if self.release_gate_status not in {"PASS", "FAIL"}:
            raise ValueError("release_gate_status must be PASS or FAIL")


def assess_release_gate(
    *,
    scenario_results: Mapping[str, bool],
    p0_blockers: tuple[str, ...] = (),
    p1_blockers: tuple[str, ...] = (),
    p2_findings: tuple[str, ...] = (),
    known_limitations: tuple[str, ...] = (),
    contract_tests_passed: bool,
    operations_docs_tests_passed: bool,
    full_suite_passed: bool,
    resource_invariants_passed: bool,
    security_scan_passed: bool,
) -> ReleaseGateAssessment:
    passed = sum(value is True for value in scenario_results.values())
    required = len(scenario_results)
    gate_passed = (
        not p0_blockers
        and not p1_blockers
        and required > 0
        and passed == required
        and contract_tests_passed
        and operations_docs_tests_passed
        and full_suite_passed
        and resource_invariants_passed
        and security_scan_passed
    )
    return ReleaseGateAssessment(
        p0_blockers=p0_blockers,
        p1_blockers=p1_blockers,
        p2_findings=p2_findings,
        known_limitations=known_limitations,
        contract_tests_passed=contract_tests_passed,
        operations_docs_tests_passed=operations_docs_tests_passed,
        rc_scenarios_passed=passed,
        rc_scenarios_required=required,
        full_suite_passed=full_suite_passed,
        resource_invariants_passed=resource_invariants_passed,
        security_scan_passed=security_scan_passed,
        release_gate_status="PASS" if gate_passed else "FAIL",
    )
