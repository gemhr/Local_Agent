from pathlib import Path
import re


DOC = Path("docs/runtime/runtime_release_checklist.md")


def test_release_checklist_has_all_phases_and_real_test_references() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert all(
        heading in text
        for heading in ("## Pre-release", "## Startup", "## Runtime", "## Shutdown", "## Rollback")
    )
    referenced_files = set(re.findall(r"`(tests/[^`:]+\.py)", text))
    assert len(referenced_files) >= 12
    assert all(Path(file_name).is_file() for file_name in referenced_files)
    assert all(f"RC-{index:02d}" in Path("docs/runtime/runtime_rc_scenario_matrix.md").read_text(encoding="utf-8") for index in range(1, 21))


def test_checklist_requires_fully_closed_and_forbids_cross_runtime_rerun() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "`fully_closed`" in text
    assert "deferred/unknown" in text
    assert "请求前将真实 `CHAT_RUNTIME_MODE`" in text
    assert "禁止对已失败/已开始 Run 跨 Runtime 重跑" in text
    assert "毫秒值不是硬 Gate" in text

