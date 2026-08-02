from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "docs/runtime/stage2_runtime_evidence_manifest.md"
ALLOWED_LEVELS = {
    "API_E2E", "RUNTIME_E2E", "SUBSYSTEM_INTEGRATION", "CONTRACT",
    "STATIC_AUDIT", "NEGATIVE_ASSERTION",
}


def _text() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def test_manifest_has_schema_unique_claims_and_allowed_evidence_levels() -> None:
    text = _text()
    header = "| claim_id | claim | capability | status | authoritative_code_owner | primary_test_ids | supporting_document | evidence_level | known_limitations |"
    assert header in text
    rows = [line for line in text.splitlines() if re.match(r"\| S2-\d{3} \|", line)]
    ids = [line.split("|")[1].strip() for line in rows]
    assert len(ids) == len(set(ids)) >= 43
    assert {line.split("|")[8].strip() for line in rows} <= ALLOWED_LEVELS


def test_every_manifest_test_id_resolves_to_a_real_test_node() -> None:
    for test_id in re.findall(r"`(tests/[^`]+)`", _text()):
        file_name, *nodes = test_id.split("::")
        path = ROOT / file_name
        assert path.is_file(), test_id
        source = path.read_text(encoding="utf-8")
        assert all(node in source for node in nodes), test_id


def test_manifest_keeps_unimplemented_capabilities_negative() -> None:
    rows = [line for line in _text().splitlines() if re.match(r"\| S2-\d{3} \|", line)]
    by_id = {row.split("|")[1].strip(): row for row in rows}
    for claim_id in ("S2-023", "S2-032", "S2-033", "S2-040", "S2-041", "S2-042", "S2-043"):
        assert "NOT_IMPLEMENTED" in by_id[claim_id]
