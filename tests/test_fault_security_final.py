from __future__ import annotations

import json

from core.runtime import (
    FaultAction,
    FaultCoverageReport,
    FaultMatchContext,
    FaultPoint,
    FaultPointSupportReport,
    FaultRuntimeInvariantReport,
    build_fault_coverage_report,
    build_fault_point_support_report,
    build_fault_runtime_invariant_report,
)


SENSITIVE_MARKERS = (
    "SECRET_PROMPT_TEXT",
    "MODEL_OUTPUT_SECRET",
    "TOOL_ARGUMENT_SECRET",
    "TOOL_OUTPUT_SECRET",
    "RAG_CHUNK_SECRET",
    "MEMORY_SECRET",
    r"C:\Users\private-user",
    "provider-secret-error",
    "raw-idempotency-key",
    "raw-resource-key",
    "raw-snapshot-payload",
    "run-id-plaintext",
    "thread-id-plaintext",
    "fault-rule-secret",
)


def _render(value) -> str:
    return repr(value) + json.dumps(
        value,
        default=lambda item: getattr(item, "value", str(item)),
        sort_keys=True,
    )


def test_final_reports_are_value_only_immutable_and_sensitive_text_free() -> None:
    reports = (
        build_fault_point_support_report(),
        build_fault_coverage_report(),
        build_fault_runtime_invariant_report(),
    )
    assert isinstance(reports[0], FaultPointSupportReport)
    assert isinstance(reports[1], FaultCoverageReport)
    assert isinstance(reports[2], FaultRuntimeInvariantReport)
    rendered = "\n".join(_render(report) for report in reports)
    assert not any(marker in rendered for marker in SENSITIVE_MARKERS)
    assert "C:\\" not in rendered
    assert "tests/" not in rendered and "tests\\" not in rendered
    assert "run_id=" not in rendered and "thread_id=" not in rendered


def test_fault_context_and_support_report_never_hold_business_payloads() -> None:
    context = FaultMatchContext(
        fault_point=FaultPoint.TOOL_BEFORE_PROVIDER_CALL,
        component="tool",
        run_id_digest="a" * 64,
        invocation_id_digest="b" * 64,
        operation_kind="execute",
    )
    entry = build_fault_point_support_report().entry_for(
        FaultPoint.TOOL_BEFORE_PROVIDER_CALL
    )
    assert entry.supported_actions == (
        FaultAction.RAISE_TYPED_ERROR,
        FaultAction.DELAY,
        FaultAction.BLOCK_UNTIL_RELEASED,
    )
    rendered = _render((context, entry))
    assert not any(marker in rendered for marker in SENSITIVE_MARKERS)
    for forbidden_field in ("prompt", "output", "arguments", "payload", "exception"):
        assert not hasattr(context, forbidden_field)
        assert not hasattr(entry, forbidden_field)


def test_fault_reports_do_not_publish_raw_rule_or_runtime_identifiers() -> None:
    support = build_fault_point_support_report()
    coverage = build_fault_coverage_report(support)
    invariant = build_fault_runtime_invariant_report()
    for report in (support, coverage, invariant):
        assert not hasattr(report, "rule_id")
        assert not hasattr(report, "run_id")
        assert not hasattr(report, "thread_id")
        assert not hasattr(report, "source_path")
