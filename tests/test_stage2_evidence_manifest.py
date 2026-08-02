from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

from tests._stage2_evidence_manifest import (
    ALLOWED_EXECUTION_EVIDENCE,
    ALLOWED_LEVELS,
    ALLOWED_STATUSES,
    parse_manifest,
    render_validation_summary,
    resolve_supporting_document,
    validation_summary,
)


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "docs/runtime/stage2_runtime_evidence_manifest.md"
API_ENTRY_TARGETS = {
    "tests/test_runtime_mode_e2e.py::test_explicit_legacy_api_does_not_create_coordinated_scope",
    "tests/test_runtime_lifespan.py::test_default_chat_endpoint_captures_mode_once_and_routes_coordinated",
    "tests/test_runtime_mode_e2e.py::test_api_coordinated_failure_has_no_legacy_fallback",
    "tests/test_client_disconnect.py::test_disconnect_watcher_stops_output_and_is_awaited",
}


def _text() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def _claims():
    return parse_manifest(_text())


def test_manifest_schema_semantics_ids_enums_and_generated_summary_are_exact() -> None:
    text = _text()
    claims = _claims()
    expected_ids = tuple(f"S2-{index:03d}" for index in range(1, 45))
    assert tuple(claim.claim_id for claim in claims) == expected_ids
    assert len(set(expected_ids)) == len(claims) == 44
    assert {claim.status for claim in claims} <= ALLOWED_STATUSES
    assert {claim.evidence_level for claim in claims} <= ALLOWED_LEVELS
    assert {
        item for claim in claims for item in claim.execution_evidence
    } <= ALLOWED_EXECUTION_EVIDENCE
    assert "status` 表示 Claim 所涉及 Capability 的实现状态" in text
    assert "Claim 是否已经验证不由 `status` 表达" in text
    assert "`CONTRACT_ONLY` 不表示 Claim 未验证" in text

    summary = validation_summary(claims)
    assert summary == {
        "total_claims": 44,
        "supported_claims": 33,
        "partially_supported_claims": 3,
        "contract_only_claims": 1,
        "not_implemented_claims": 7,
        "api_e2e_claims": 4,
        "runtime_e2e_claims": 7,
        "subsystem_integration_claims": 13,
        "contract_claims": 7,
        "static_audit_claims": 7,
        "negative_assertion_claims": 6,
        "claims_with_specific_node_ids": 43,
        "claims_with_file_level_targets": 2,
        "claims_executed_in_rc_gate": 13,
        "claims_executed_in_full_suite": 44,
        "duplicate_claim_ids": 0,
        "missing_claim_ids": 0,
        "invalid_evidence_levels": 0,
        "invalid_capability_statuses": 0,
    }
    summary_section = text.split("## Manifest Validation Summary", 1)[1]
    assert render_validation_summary(claims) in summary_section


def test_targets_collect_documents_exist_and_api_levels_use_real_entry_tests() -> None:
    claims = _claims()
    targets = tuple(dict.fromkeys(target for claim in claims for target in claim.primary_test_targets))
    for target in targets:
        file_name = target.split("::", 1)[0]
        assert (ROOT / file_name).is_file(), target
    for claim in claims:
        assert resolve_supporting_document(ROOT, claim.supporting_document).is_file(), claim.claim_id

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *targets],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    node_ids = set(collected.stdout.splitlines())
    for target in targets:
        if "::" in target:
            assert any(node == target or node.startswith(f"{target}[") for node in node_ids), target
        else:
            assert any(node.startswith(f"{target}::") for node in node_ids), target

    api_claims = [claim for claim in claims if claim.evidence_level == "API_E2E"]
    assert {claim.claim_id for claim in api_claims} == {"S2-002", "S2-003", "S2-004", "S2-027"}
    assert all(any(target in API_ENTRY_TARGETS for target in claim.primary_test_targets) for claim in api_claims)
    assert all("server" in (ROOT / target.split("::", 1)[0]).read_text(encoding="utf-8") for claim in api_claims for target in claim.primary_test_targets)


def test_claim_evidence_authority_security_and_capability_boundaries_are_truthful() -> None:
    text = _text()
    by_id = {claim.claim_id: claim for claim in _claims()}
    assert by_id["S2-001"].evidence_level == "CONTRACT"
    assert len(by_id["S2-001"].primary_test_targets) == 2
    assert by_id["S2-013"].evidence_level == "RUNTIME_E2E"
    assert "RunScope" in by_id["S2-013"].known_limitations

    assert by_id["S2-036"].claim.startswith("20 个 RC Scenario 均定义为 REQUIRED")
    assert by_id["S2-037"].claim.endswith("20/20 passed")
    assert set(by_id["S2-036"].primary_test_targets).isdisjoint(by_id["S2-037"].primary_test_targets)
    assert "EXECUTED_IN_RC_GATE" not in by_id["S2-036"].execution_evidence
    assert {"EXECUTED_IN_RC_GATE", "EXECUTED_IN_FULL_SUITE"} <= set(by_id["S2-037"].execution_evidence)

    report_claims = ("S2-030", "S2-034", "S2-035", "S2-037", "S2-038", "S2-039")
    assert all("derivation" in by_id[claim_id].authority_or_derivation_owner.lower() for claim_id in report_claims)
    assert "authoritative_code_owner" not in text
    assert "primary_test_ids" not in text
    assert "FaultPointSupportReport` |" not in text

    not_implemented = [claim for claim in by_id.values() if claim.status == "NOT_IMPLEMENTED"]
    assert all(claim.evidence_level in {"NEGATIVE_ASSERTION", "STATIC_AUDIT"} for claim in not_implemented)
    assert all(
        {"NEGATIVE_ASSERTION_EXECUTED", "STATIC_AUDIT_ONLY"}.intersection(claim.execution_evidence)
        for claim in not_implemented
    )
    assert by_id["S2-034"].claim.startswith("32 个 Fault Point Supported")
    assert by_id["S2-035"].claim.startswith("10 个 Fault Point Contract-only")

    assert re.search(r"[A-Za-z]:\\", text) is None
    assert "provider_url=" not in text.lower()
    assert "Production Ready Certified" not in text
    assert "生产执行认证" in by_id["S2-037"].known_limitations
    assert "故障注入规则标识" in text
