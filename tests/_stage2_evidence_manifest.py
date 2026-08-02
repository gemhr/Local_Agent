from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


ALLOWED_STATUSES = {
    "SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "CONTRACT_ONLY",
    "NOT_IMPLEMENTED",
}
ALLOWED_LEVELS = {
    "API_E2E",
    "RUNTIME_E2E",
    "SUBSYSTEM_INTEGRATION",
    "CONTRACT",
    "STATIC_AUDIT",
    "NEGATIVE_ASSERTION",
}
ALLOWED_EXECUTION_EVIDENCE = {
    "TEST_TARGET_EXISTS",
    "EXECUTED_IN_RC_GATE",
    "EXECUTED_IN_FULL_SUITE",
    "STATIC_AUDIT_ONLY",
    "NEGATIVE_ASSERTION_EXECUTED",
}


@dataclass(frozen=True, slots=True)
class ManifestClaim:
    claim_id: str
    claim: str
    capability: str
    status: str
    authority_or_derivation_owner: str
    primary_test_targets: tuple[str, ...]
    supporting_document: str
    evidence_level: str
    execution_evidence: tuple[str, ...]
    known_limitations: str


def _plain(value: str) -> str:
    return value.strip().strip("`")


def parse_manifest(text: str) -> tuple[ManifestClaim, ...]:
    claims: list[ManifestClaim] = []
    for line in text.splitlines():
        if re.match(r"^\| S2-\d{3} \|", line) is None:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 10:
            raise AssertionError(f"invalid manifest row width: {line}")
        targets = tuple(_plain(item) for item in cells[5].split("<br>"))
        execution = tuple(_plain(item) for item in cells[8].split("<br>"))
        claims.append(
            ManifestClaim(
                claim_id=cells[0],
                claim=cells[1],
                capability=cells[2],
                status=cells[3],
                authority_or_derivation_owner=cells[4],
                primary_test_targets=targets,
                supporting_document=_plain(cells[6]),
                evidence_level=cells[7],
                execution_evidence=execution,
                known_limitations=cells[9],
            )
        )
    return tuple(claims)


def validation_summary(claims: tuple[ManifestClaim, ...]) -> dict[str, int]:
    ids = [claim.claim_id for claim in claims]
    numeric_ids = sorted(
        int(match.group(1))
        for claim_id in set(ids)
        if (match := re.fullmatch(r"S2-(\d{3})", claim_id)) is not None
    )
    expected = set(range(1, max(numeric_ids, default=0) + 1))
    actual = set(numeric_ids)
    return {
        "total_claims": len(claims),
        "supported_claims": sum(c.status == "SUPPORTED" for c in claims),
        "partially_supported_claims": sum(c.status == "PARTIALLY_SUPPORTED" for c in claims),
        "contract_only_claims": sum(c.status == "CONTRACT_ONLY" for c in claims),
        "not_implemented_claims": sum(c.status == "NOT_IMPLEMENTED" for c in claims),
        "api_e2e_claims": sum(c.evidence_level == "API_E2E" for c in claims),
        "runtime_e2e_claims": sum(c.evidence_level == "RUNTIME_E2E" for c in claims),
        "subsystem_integration_claims": sum(c.evidence_level == "SUBSYSTEM_INTEGRATION" for c in claims),
        "contract_claims": sum(c.evidence_level == "CONTRACT" for c in claims),
        "static_audit_claims": sum(c.evidence_level == "STATIC_AUDIT" for c in claims),
        "negative_assertion_claims": sum(c.evidence_level == "NEGATIVE_ASSERTION" for c in claims),
        "claims_with_specific_node_ids": sum(any("::" in target for target in c.primary_test_targets) for c in claims),
        "claims_with_file_level_targets": sum(any("::" not in target for target in c.primary_test_targets) for c in claims),
        "claims_executed_in_rc_gate": sum("EXECUTED_IN_RC_GATE" in c.execution_evidence for c in claims),
        "claims_executed_in_full_suite": sum("EXECUTED_IN_FULL_SUITE" in c.execution_evidence for c in claims),
        "duplicate_claim_ids": len(ids) - len(set(ids)),
        "missing_claim_ids": len(expected - actual),
        "invalid_evidence_levels": sum(c.evidence_level not in ALLOWED_LEVELS for c in claims),
        "invalid_capability_statuses": sum(c.status not in ALLOWED_STATUSES for c in claims),
    }


def render_validation_summary(claims: tuple[ManifestClaim, ...]) -> str:
    values = validation_summary(claims)
    return "\n".join(f"- {key}: `{value}`" for key, value in values.items())


def resolve_supporting_document(root: Path, target: str) -> Path:
    path = Path(target)
    if path.parts and path.parts[0] == "docs":
        return root / path
    return root / "docs/runtime" / path
