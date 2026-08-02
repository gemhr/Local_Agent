from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
DOC = ROOT / "docs/interview/stage2_runtime_resume_material.md"


def test_resume_has_three_versions_and_fixed_collected_test_count() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert all(title in text for title in ("简历精简版", "项目详细版", "面试口述版"))
    assert "{{FINAL_PYTEST_COUNT}}" not in text
    counts = [int(value) for value in re.findall(r"`(\d+)` 个自动化测试", text)]
    assert counts and len(set(counts)) == 1 and counts[0] > 1000


def test_resume_has_no_invented_scale_performance_or_capability_claims() -> None:
    text = DOC.read_text(encoding="utf-8").lower()
    assert not re.search(r"\d+(?:\.\d+)?%", text)
    for forbidden in ("万用户", "生产 p95", "实现自动恢复", "实现 exactly-once", "生产 chaos 平台", "分布式 durable runtime"):
        assert forbidden not in text
