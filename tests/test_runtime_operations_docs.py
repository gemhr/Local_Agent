from pathlib import Path
import re


RUNBOOK = Path("docs/runtime/runtime_operations_runbook.md")
MATRIX = Path("docs/runtime/runtime_rc_scenario_matrix.md")
BASELINE = Path("docs/runtime/runtime_resource_baseline.md")
GATE = Path("docs/runtime/runtime_release_gate.md")


def test_rc_matrix_has_exact_legal_test_levels_and_truthful_counts() -> None:
    text = MATRIX.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| RC-")]
    levels = [row.split("|")[2].strip() for row in rows]
    allowed = {"API_E2E", "RUNTIME_E2E", "SUBSYSTEM_INTEGRATION", "CONTRACT"}

    assert len(rows) == 20
    assert set(levels) <= allowed
    assert levels.count("API_E2E") == 4
    assert levels.count("RUNTIME_E2E") == 3
    assert levels.count("SUBSYSTEM_INTEGRATION") == 13
    assert levels.count("CONTRACT") == 0


def test_baseline_and_gate_scope_cannot_be_read_as_production_validation() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    gate = GATE.read_text(encoding="utf-8")

    assert "Runtime orchestration overhead baseline" in baseline
    assert "Offline Fake Adapter" in baseline
    assert "不包含真实 LLM" in baseline
    assert "不能解释为生产延迟、吞吐或容量" in baseline
    assert "Owner 计数归零" in baseline
    assert "Stage2 Runtime RC1 code-level gate passed" in gate
    assert "Out-of-scope Production Validation" in gate
    assert all(term in gate for term in ("生产容量验证", "Soak Test", "渗透测试"))


def test_runbook_uses_real_health_fields_and_safe_shutdown_truth() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    required = (
        "dropped_records",
        "record_failures",
        "flush_failures",
        "last_safe_error_code",
        "active_span_count",
        "runtime_active_runs",
        "runtime_detached_tool_workers",
        "orchestration_completed",
        "fully_closed",
        "has_deferred_resources",
        "unknown_worker_count",
    )
    assert all(item in text for item in required)
    assert "不等于已接 Prometheus/Grafana" in text
    assert "Detached worker 不可清记录" in text
    assert "禁止操作：强杀线程、删除记录伪造 idle" in text
    assert len(re.findall(r"^- 症状：", text, re.MULTILINE)) == 10

