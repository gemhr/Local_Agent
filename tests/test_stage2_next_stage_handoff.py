from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
DOC = ROOT / "docs/runtime/stage2_known_limitations_and_next_stage.md"


def test_each_next_stage_item_is_explicitly_not_started_and_complete() -> None:
    text = DOC.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if re.match(r"\| (P0_NEXT|P1_NEXT|P2_LATER|RESEARCH_ONLY) \|", line)]
    assert rows
    assert all(row.count("|") >= 8 and row.rstrip().endswith(" true |") for row in rows)
    assert {row.split("|")[1].strip() for row in rows} == {"P0_NEXT", "P1_NEXT", "P2_LATER", "RESEARCH_ONLY"}


def test_final_documents_keep_security_and_semantic_boundaries() -> None:
    paths = [
        ROOT / "docs/runtime/stage2_runtime_evidence_manifest.md",
        ROOT / "docs/runtime/stage2_known_limitations_and_next_stage.md",
        ROOT / "docs/interview/stage2_runtime_interview_material.md",
        ROOT / "docs/interview/stage2_runtime_bad_cases.md",
        ROOT / "docs/interview/stage2_runtime_resume_material.md",
        ROOT / "docs/learning/stage2/result/day25_stage2_final_acceptance_result.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert not re.search(r"[A-Za-z]:\\", combined)
    assert "provider_url=" not in combined.lower()
    recovery = (ROOT / "docs/runtime/runtime_recovery_runbook.md").read_text(encoding="utf-8")
    preservation = next(line for line in recovery.splitlines() if "不得改写原始 JournalRecord" in line)
    assert all(phrase in preservation for phrase in ("已有 RunSnapshot", "历史 AgentState", "不得补造", "TOOL_COMPLETED"))
    error_catalog = (ROOT / "docs/runtime/runtime_error_code_catalog.md").read_text(encoding="utf-8")
    assert "RUNTIME_CONFIGURATION_ERROR" in error_catalog and "尚未完整建立" in error_catalog
