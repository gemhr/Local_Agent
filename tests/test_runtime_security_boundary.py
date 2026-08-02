from pathlib import Path
import re


DOC = Path("docs/runtime/runtime_security_boundary.md")
FORMAL_DOCS = (
    DOC,
    Path("docs/runtime/runtime_operations_runbook.md"),
    Path("docs/runtime/runtime_configuration_reference.md"),
    Path("docs/runtime/runtime_error_code_catalog.md"),
    Path("docs/runtime/runtime_recovery_runbook.md"),
    Path("docs/runtime/runtime_release_checklist.md"),
    Path("docs/learning/stage2/result/day25_runtime_operations_result.md"),
)


def test_security_boundary_distinguishes_runtime_projection_from_business_storage() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "MemoryManager 有独立业务持久化路径" in text
    assert "正常聊天 Wire 必然承载面向用户的 Model output" in text
    assert "不能笼统声称“任何正文绝不持久化”" in text
    assert "无生产激活入口" in text
    assert "Rule ID 不进入业务输出" in text


def test_operations_documents_contain_no_forbidden_marker_or_real_absolute_path() -> None:
    markers = (
        "SECRET_PROMPT_TEXT",
        "MODEL_OUTPUT_SECRET",
        "TOOL_ARGUMENT_SECRET",
        "TOOL_OUTPUT_SECRET",
        "RAG_CHUNK_SECRET",
        "MEMORY_SECRET",
        "provider-secret-error",
        "raw-idempotency-key",
        "raw-resource-key",
        "raw-snapshot-payload",
        "fault-rule-secret",
    )
    for path in FORMAL_DOCS:
        text = path.read_text(encoding="utf-8")
        assert all(marker not in text for marker in markers), path
        assert re.search(r"[A-Za-z]:\\", text) is None, path
        assert re.search(r"https?://(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)", text) is None, path


def test_ci_artifact_allowlist_excludes_paths_errors_and_fault_control() -> None:
    text = Path("docs/runtime/runtime_release_checklist.md").read_text(encoding="utf-8")
    assert "唯一允许字段" in text
    assert "禁止路径、原始异常、业务正文、Rule ID、Provider 配置" in text
    assert "不是 Runtime 控制面，不能启用 Fault" in text
