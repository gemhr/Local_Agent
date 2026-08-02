from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
DOC = ROOT / "docs/interview/stage2_runtime_bad_cases.md"


def test_bad_cases_are_high_value_and_each_has_truth_label() -> None:
    text = DOC.read_text(encoding="utf-8")
    cases = re.split(r"(?m)^## \d+\. ", text)[1:]
    assert 10 <= len(cases) <= 15
    required = ("场景：", "触发：", "风险：", "根因：", "修复：", "回归：", "设计原则：", "面试表达：", "真实性边界：")
    assert all(all(field in case for field in required) for case in cases)
    assert all(any(label in case for label in ("真实发现", "机制风险", "假设构造")) for case in cases)
    assert "非生产事故" in text


def test_bad_case_test_ids_resolve() -> None:
    for test_id in re.findall(r"`(tests/[^`]+)`", DOC.read_text(encoding="utf-8")):
        file_name, *nodes = test_id.split("::")
        path = ROOT / file_name
        assert path.is_file(), test_id
        source = path.read_text(encoding="utf-8")
        assert all(node in source for node in nodes), test_id
