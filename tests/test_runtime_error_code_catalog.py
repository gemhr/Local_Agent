from pathlib import Path
import re


DOC = Path("docs/runtime/runtime_error_code_catalog.md")


def _catalog_codes() -> tuple[str, ...]:
    codes = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("| `"):
            codes.append(line.split("`")[1])
    return tuple(codes)


def test_catalog_codes_all_exist_in_real_code() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (*Path("core").rglob("*.py"), Path("server.py"))
    )
    codes = _catalog_codes()

    assert len(codes) >= 35
    assert len(codes) == len(set(codes))
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]+", code) for code in codes)
    assert all(f'"{code}"' in source or f".{code}" in source for code in codes)


def test_catalog_covers_critical_domains_without_universal_retry_advice() -> None:
    text = DOC.read_text(encoding="utf-8")
    domains = (
        "Runtime",
        "Admission",
        "Model",
        "Retrieval",
        "Tool",
        "Budget",
        "Journal",
        "Snapshot",
        "Recovery",
        "Observability",
        "Trace",
        "Shutdown",
        "Worker",
        "Legacy compatibility",
    )
    assert all(domain in text for domain in domains)
    assert "`RUNTIME_CONFIGURATION_ERROR` 是真实固定码" in text
    assert "仅覆盖 ChatService 缺少 Coordinated factory" in text
    assert "尚未完整建立统一 Settings Validation Error Taxonomy" in text
    assert "不 universally retryable" not in text
    assert "no automatic Tool call" in text
    assert "do not repeat search automatically" in text
    assert "Diagnostic degraded" in text
