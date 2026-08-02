from pathlib import Path


ROOT = Path(__file__).parents[1]
DOC = ROOT / "docs/interview/stage2_runtime_interview_material.md"


def test_interview_material_has_required_packages_and_truth_boundaries() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "30 秒" in text and "2～3 分钟" in text
    decision_block = text.split("## 3. 十个设计决策", 1)[1].split("## 4.", 1)[0]
    assert len([line for line in decision_block.splitlines() if line[:2].rstrip(".").isdigit()]) >= 10
    assert len([line for line in text.splitlines() if line.startswith("### ")]) >= 12
    assert "没有失败后跨 Runtime fallback" in text
    assert "只读恢复验证" in text


def test_interview_material_does_not_claim_unimplemented_production_capabilities() -> None:
    text = DOC.read_text(encoding="utf-8").lower()
    for false_claim in ("实现了自动恢复", "实现了 exactly-once", "生产 chaos 平台", "分布式 durable runtime"):
        assert false_claim not in text
