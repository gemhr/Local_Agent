from pathlib import Path


DOC = Path("docs/runtime/runtime_recovery_runbook.md")


def test_recovery_runbook_is_validation_only_and_uses_persisted_authority() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "Recovery validation only" in text
    assert "不提供自动 Resume、Replay、Tool Retry、Compensation" in text
    assert "生产恢复权威输入仅为原始 `RunSnapshot` 与 `JournalRecord`" in text
    assert "不得从当前 Registry、Memory、adapter 或测试 fixture 回填" in text
    assert "它不写 AgentState、不调用 Model/Tool/Retrieval" in text
    assert "Historical Authority 只有 Snapshot 与 Journal" in text
    assert "独立人工审计记录" in text
    assert "不得改写原始 JournalRecord、已有 RunSnapshot、历史 AgentState" in text
    assert "不得补造 `TOOL_COMPLETED`" in text


def test_tool_manual_reconciliation_has_all_eight_safe_steps() -> None:
    text = DOC.read_text(encoding="utf-8")
    block = text.split("## Tool Manual Reconciliation", 1)[1]
    assert all(f"{index}. " in block for index in range(1, 9))
    assert all(item in block for item in ("NOT_STARTED", "COMMITTED", "UNKNOWN"))
    assert "不得调用 Tool" in block
    assert "外部权威系统由人工确认" in block
    assert "始终只读" in block
