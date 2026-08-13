#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4-A Trace Contract Fingerprint 确定性/易变排除/语义敏感回归。

Phase 3.2 基础 + H-3 Remediation R2（P1-04）：指纹语义源必须覆盖
value-domain（合同允许值域）与 compatibility 行为；实例值不得进入指纹。
只测试 fingerprint Owner 的纯语义描述与 digest；不读取 live SpanRecord，
不包含业务正文。
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from core.runtime.snapshot_serialization import canonical_json
from core.runtime.trace_contract_fingerprint import (
    TRACE_CONTRACT_FINGERPRINT,
    TRACE_CONTRACT_FINGERPRINT_ALGORITHM,
    TRACE_CONTRACT_FINGERPRINT_CANONICAL_ENCODING,
    TraceContractFingerprinter,
)
from core.runtime.trace_export_contract import (
    CompatibilityReason,
    TRACE_EXPORT_CONTRACT_VERSION,
    TraceCompatibilityEvaluator,
    project_span,
)
from core.runtime.tracing import SpanRecord, SpanStatus

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def base_descriptor() -> dict[str, object]:
    return copy.deepcopy(dict(TraceContractFingerprinter.semantic_descriptor()))


# --- 格式与算法 -----------------------------------------------------------


def test_fingerprint_format_and_algorithm_facts() -> None:
    assert _HEX64.fullmatch(TRACE_CONTRACT_FINGERPRINT)
    assert TRACE_CONTRACT_FINGERPRINT_ALGORITHM == "sha256"
    assert TRACE_CONTRACT_FINGERPRINT_CANONICAL_ENCODING == "canonical_json_v1"
    assert (
        TraceContractFingerprinter.fingerprint()
        == TraceContractFingerprinter.fingerprint()
    )


def test_fingerprint_equals_digest_of_semantic_descriptor() -> None:
    descriptor = TraceContractFingerprinter.semantic_descriptor()
    assert (
        TRACE_CONTRACT_FINGERPRINT
        == TraceContractFingerprinter.fingerprint_from_semantic_descriptor(
            descriptor
        )
    )


# --- 确定性 ---------------------------------------------------------------


def test_same_semantic_facts_same_fingerprint() -> None:
    first = TraceContractFingerprinter.fingerprint_from_semantic_descriptor(
        base_descriptor()
    )
    second = TraceContractFingerprinter.fingerprint_from_semantic_descriptor(
        base_descriptor()
    )
    assert first == second


def test_reordered_maps_same_fingerprint() -> None:
    descriptor = base_descriptor()
    # 程序化重排 dict 键：语义相同、键插入顺序不同 → 同一指纹。
    reordered = {
        key: descriptor[key] for key in reversed(tuple(descriptor.keys()))
    }
    assert (
        TraceContractFingerprinter.fingerprint_from_semantic_descriptor(
            descriptor
        )
        == TraceContractFingerprinter.fingerprint_from_semantic_descriptor(
            reordered
        )
    )


def test_reordered_unordered_collections_same_fingerprint() -> None:
    ordered = {"vocabulary": ("OK", "ERROR", "CANCELLED", "TIMED_OUT")}
    shuffled = {"vocabulary": ("CANCELLED", "TIMED_OUT", "OK", "ERROR")}
    assert TraceContractFingerprinter.fingerprint_from_semantic_descriptor(
        ordered
    ) == TraceContractFingerprinter.fingerprint_from_semantic_descriptor(
        shuffled
    )


# --- 实例易变值排除 -------------------------------------------------------


def test_semantic_descriptor_contains_no_instance_values() -> None:
    descriptor = TraceContractFingerprinter.semantic_descriptor()
    payload = canonical_json(descriptor)
    for volatile in (
        "run-a-1",
        "span-a-1",
        "trace-a-1",
        "step-final",
        "run-ok",
        "span-ok",
    ):
        assert volatile not in payload
    # 时间/duration 不是语义源的一部分。
    assert '"2026-' not in payload
    # 合同值域（而非实例值）必须进入指纹：final_status 的 RunStatus 词汇表
    # 包含 "SUCCEEDED"，它现在以 contract semantic 身份出现。
    run_schema = descriptor["category_schemas"]["run"]
    assert "SUCCEEDED" in run_schema["final_status"]["domain"]["values"]


def test_outcome_and_instance_facts_do_not_change_fingerprint() -> None:
    from datetime import UTC, datetime, timedelta

    from core.runtime.tracing import SpanRecord, SpanStatus
    from core.runtime.trace_export_contract import project_span

    def envelope(run_id: str, span_id: str, status: SpanStatus, error_code):
        started = datetime(2026, 1, 1, tzinfo=UTC)
        record = SpanRecord(
            trace_id="trace-x-1",
            span_id=span_id,
            parent_span_id=None,
            run_id=run_id,
            step_id=None,
            component="runtime",
            operation="runtime.run",
            started_at=started,
            completed_at=started + timedelta(milliseconds=5),
            duration_ms=5.0,
            status=status,
            error_code=error_code,
            attributes={},
        )
        return project_span(record)

    ok = envelope("run-ok", "span-ok", SpanStatus.OK, None)
    failed = envelope("run-fail", "span-fail", SpanStatus.ERROR, "RUN_FAILED")
    assert ok.contract_fingerprint == failed.contract_fingerprint
    assert ok.contract_fingerprint == TRACE_CONTRACT_FINGERPRINT


# --- 语义敏感 -------------------------------------------------------------


def test_required_field_rule_change_changes_fingerprint() -> None:
    original = base_descriptor()
    changed = base_descriptor()
    changed["common_field_rules"] = dict(changed["common_field_rules"])
    changed["common_field_rules"]["step_id"] = (
        "required safe identifier for every operation"
    )
    assert _different(original, changed)


def test_field_domain_change_changes_fingerprint() -> None:
    original = base_descriptor()
    changed = base_descriptor()
    changed["category_schemas"] = copy.deepcopy(changed["category_schemas"])
    step = changed["category_schemas"]["step"]  # type: ignore[index]
    step["dependency_count"] = {"type": "STRING_TOKEN", "presence": "OPTIONAL"}
    assert _different(original, changed)


def test_stable_operation_semantic_change_changes_fingerprint() -> None:
    original = base_descriptor()
    changed = base_descriptor()
    changed["stable_operations"] = copy.deepcopy(changed["stable_operations"])
    changed["stable_operations"]["runtime.run"]["step_bound"] = True  # type: ignore[index]
    assert _different(original, changed)


def test_category_export_policy_change_changes_fingerprint() -> None:
    original = base_descriptor()
    changed = base_descriptor()
    changed["category_schemas"] = copy.deepcopy(changed["category_schemas"])
    run_schema = changed["category_schemas"]["run"]  # type: ignore[index]
    del run_schema["final_status"]
    assert _different(original, changed)


def test_security_policy_change_changes_fingerprint() -> None:
    original = base_descriptor()
    changed = base_descriptor()
    changed["security_policy"] = dict(changed["security_policy"])
    changed["security_policy"]["boundary"] = "permissive"
    assert _different(original, changed)


def _different(first: dict[str, object], second: dict[str, object]) -> bool:
    return (
        TraceContractFingerprinter.fingerprint_from_semantic_descriptor(first)
        != TraceContractFingerprinter.fingerprint_from_semantic_descriptor(second)
    )


# --- 合约 vs 实例指纹 / 边界 ----------------------------------------------


def test_trace_instance_fingerprint_not_implemented() -> None:
    import core.runtime.trace_contract_fingerprint as module

    assert not hasattr(module, "trace_instance_fingerprint")
    assert not hasattr(module, "trace_instance_fingerprinter")


def test_run_configuration_fingerprint_not_implemented() -> None:
    import core.runtime.trace_contract_fingerprint as module

    assert not hasattr(module, "run_configuration_fingerprint")
    assert not hasattr(module, "run_configuration_fingerprinter")


def test_plan_fingerprint_owner_unchanged() -> None:
    from core.runtime.plan_fingerprint import PlanFingerprinter

    assert hasattr(PlanFingerprinter, "fingerprint")
    assert hasattr(PlanFingerprinter, "fingerprint_snapshot")


def test_semantic_source_is_finite_and_stable() -> None:
    first = TraceContractFingerprinter.semantic_descriptor()
    second = TraceContractFingerprinter.semantic_descriptor()
    assert canonical_json(first) == canonical_json(second)
    assert len(first["stable_operations"]) == 6  # type: ignore[index]
    assert len(first["terminal_statuses"]) == 4  # type: ignore[index]
    assert set(first["terminal_statuses"]) == {  # type: ignore[index]
        "OK",
        "ERROR",
        "CANCELLED",
        "TIMED_OUT",
    }


def test_unknown_fingerprint_is_rejected_not_accepted() -> None:
    from core.runtime.trace_export_contract import (
        TRACE_EXPORT_CONTRACT_IDENTITY,
        TRACE_EXPORT_CONTRACT_VERSION,
        TraceCompatibilityEvaluator,
        TraceExportEnvelope,
    )
    from core.runtime.tracing import SpanStatus
    from datetime import UTC, datetime, timedelta

    started_at = datetime.now(UTC)
    envelope = TraceExportEnvelope(
        contract_identity=TRACE_EXPORT_CONTRACT_IDENTITY,
        contract_version=TRACE_EXPORT_CONTRACT_VERSION,
        contract_fingerprint="d" * 64,
        run_id="run-1",
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id=None,
        step_id=None,
        operation="runtime.run",
        component="runtime",
        started_at=started_at,
        completed_at=started_at + timedelta(milliseconds=5),
        duration_ms=5.0,
        status=SpanStatus.OK,
        error_code=None,
        attributes={},
    )
    decision = TraceCompatibilityEvaluator.evaluate(envelope)
    assert decision.accepted is False


# --- H-3 Remediation R2：P1-04 指纹 value-domain / 兼容语义覆盖回归 ---------


OLD_FINGERPRINT = (
    "3e19161d4f59d6b802be151d513b9bccf1d69733260efec249a645bf781b47e2"
)


def _make_record(
    operation: str = "runtime.run",
    component: str = "runtime",
    step_id: str | None = None,
    attributes: dict[str, object] | None = None,
    status: SpanStatus = SpanStatus.OK,
    error_code: str | None = None,
) -> SpanRecord:
    started_at = datetime.now(UTC)
    return SpanRecord(
        trace_id="trace-a-1",
        span_id="span-a-1",
        parent_span_id=None,
        run_id="run-a-1",
        step_id=step_id,
        component=component,
        operation=operation,
        started_at=started_at,
        completed_at=started_at + timedelta(milliseconds=5),
        duration_ms=5.0,
        status=status,
        error_code=error_code,
        attributes=dict(attributes or {}),
    )


def test_authoritative_descriptor_contains_actual_value_domains() -> None:
    descriptor = base_descriptor()
    delivery = descriptor["category_schemas"]["delivery"]["delivery_status"]["domain"]
    assert delivery == {
        "kind": "vocabulary",
        "values": ["DELIVERED", "FAILED", "OUTCOME_UNKNOWN"],
    }
    # 完成的 delivery span 上 NOT_APPLICABLE 不进入公共子集。
    assert "NOT_APPLICABLE" not in delivery["values"]
    gate = descriptor["category_schemas"]["delivery"]["gate_terminal_state"]["domain"]
    assert set(gate["values"]) == {"PUBLISHED", "FAILED", "OUTCOME_UNKNOWN"}
    publish = descriptor["category_schemas"]["delivery"]["publish_attempt_count"][
        "domain"
    ]
    assert publish == {"kind": "range", "minimum": 0, "maximum": 1}
    source = descriptor["category_schemas"]["run"]["planning_source"]["domain"][
        "values"
    ]
    assert set(source) == {"deterministic", "model_generated", "legacy_adapter", "unknown"}
    kind = descriptor["category_schemas"]["step"]["execution_kind"]["domain"]["values"]
    assert set(kind) == {"AGENT", "SYNTHESIS"}
    scope = descriptor["category_schemas"]["memory"]["memory_scope"]["domain"]["values"]
    assert scope == ["direct"]


def test_delivery_status_vocabulary_change_changes_fingerprint() -> None:
    changed = base_descriptor()
    domain = changed["category_schemas"]["delivery"]["delivery_status"]["domain"]
    domain["values"] = ["DELIVERED", "FAILED", "NOT_APPLICABLE", "OUTCOME_UNKNOWN"]
    assert _different(base_descriptor(), changed)
    # 同一集合不同顺序 → 指纹不变。
    reordered = base_descriptor()
    reordered["category_schemas"]["delivery"]["delivery_status"]["domain"][
        "values"
    ] = ["OUTCOME_UNKNOWN", "DELIVERED", "FAILED"]
    assert not _different(base_descriptor(), reordered)


def test_output_gate_state_change_changes_fingerprint() -> None:
    changed = base_descriptor()
    changed["category_schemas"]["delivery"]["gate_terminal_state"]["domain"][
        "values"
    ].append("NOT_STARTED")
    assert _different(base_descriptor(), changed)


def test_publish_attempt_count_range_change_changes_fingerprint() -> None:
    changed = base_descriptor()
    changed["category_schemas"]["delivery"]["publish_attempt_count"]["domain"][
        "maximum"
    ] = 2
    assert _different(base_descriptor(), changed)


@pytest.mark.parametrize("vocab", ["execution_kind", "output_policy"])
def test_execution_kind_output_policy_vocabulary_change(vocab: str) -> None:
    changed = base_descriptor()
    changed["category_schemas"]["step"][vocab]["domain"]["values"].append(
        "FABRICATED_KIND"
    )
    assert _different(base_descriptor(), changed)


@pytest.mark.parametrize(
    "category, key, extra",
    [
        ("run", "planning_source", "fabricated_source"),
        ("run", "final_status", "FABRICATED_STATUS"),
        ("run", "stop_reason", "FABRICATED_REASON"),
        ("run", "shape", "9"),
        ("planning", "compiled_shape", "9"),
        ("memory", "memory_scope", "nested_scope"),
        ("memory", "user_write_status", "FABRICATED_WRITE"),
        ("memory", "assistant_write_status", "FABRICATED_WRITE"),
    ],
)
def test_additional_domain_sensitivity(
    category: str, key: str, extra: str
) -> None:
    changed = base_descriptor()
    changed["category_schemas"][category][key]["domain"]["values"].append(extra)
    assert _different(base_descriptor(), changed)


def test_compatibility_behavior_change_changes_fingerprint() -> None:
    changed = base_descriptor()
    changed["compatibility_behavior"][
        "known_identity_version_fingerprint_invalid_envelope_semantics"
    ] = "ACCEPT"
    assert _different(base_descriptor(), changed)
    removed = base_descriptor()
    removed["compatibility_reasons"] = [
        reason
        for reason in removed["compatibility_reasons"]
        if reason != "ENVELOPE_INVALID"
    ]
    assert _different(base_descriptor(), removed)


def test_unknown_attribute_policy_change_changes_fingerprint() -> None:
    changed = base_descriptor()
    changed["unknown_attribute_behavior"][
        "direct_constructor_unknown_attribute"
    ] = "ACCEPT"
    assert _different(base_descriptor(), changed)


def test_runtime_outcome_independence() -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)

    def envelope(
        run_id: str,
        span_id: str,
        status: SpanStatus,
        error_code,
        attributes: dict[str, object],
        duration_ms: float = 5.0,
    ):
        record = SpanRecord(
            trace_id="trace-x-1",
            span_id=span_id,
            parent_span_id=None,
            run_id=run_id,
            step_id=None,
            component="runtime",
            operation="runtime.run",
            started_at=started,
            completed_at=started + timedelta(milliseconds=5),
            duration_ms=duration_ms,
            status=status,
            error_code=error_code,
            attributes=attributes,
        )
        return project_span(record)

    ok = envelope(
        "run-ok",
        "span-ok",
        SpanStatus.OK,
        None,
        {"planning_source": "deterministic", "final_status": "SUCCEEDED"},
    )
    failed = envelope(
        "run-fail",
        "span-fail",
        SpanStatus.ERROR,
        "RUN_FAILED",
        {"planning_source": "deterministic", "final_status": "FAILED"},
        duration_ms=123.0,
    )
    assert ok.contract_fingerprint == failed.contract_fingerprint
    assert ok.contract_fingerprint == TRACE_CONTRACT_FINGERPRINT
    # 合法 publish_attempt_count 实例值 0 vs 1 → 同一指纹。
    delivered_0 = project_span(
        _make_record(
            operation="runtime.output_delivery",
            component="output_gate",
            step_id="step-final",
            attributes={
                "publish_attempt_count": 0,
                "delivery_status": "DELIVERED",
            },
        )
    )
    delivered_1 = project_span(
        _make_record(
            operation="runtime.output_delivery",
            component="output_gate",
            step_id="step-final",
            attributes={
                "publish_attempt_count": 1,
                "delivery_status": "DELIVERED",
            },
        )
    )
    assert delivered_0.contract_fingerprint == delivered_1.contract_fingerprint


def test_cross_process_determinism() -> None:
    script = (
        "from core.runtime.trace_contract_fingerprint import "
        "TRACE_CONTRACT_FINGERPRINT; print(TRACE_CONTRACT_FINGERPRINT)"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    first = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    second = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert first.returncode == 0 and second.returncode == 0
    assert first.stdout.strip() == second.stdout.strip() == TRACE_CONTRACT_FINGERPRINT


def test_old_fingerprint_rejected() -> None:
    import dataclasses

    envelope = project_span(_make_record(attributes={"plan_id": "plan-1"}))
    altered = dataclasses.replace(envelope, contract_fingerprint=OLD_FINGERPRINT)
    decision = TraceCompatibilityEvaluator.evaluate(altered)
    assert decision.accepted is False
    assert decision.reason is CompatibilityReason.FINGERPRINT_UNSUPPORTED


def test_export_and_runtime_versions_unchanged() -> None:
    from core.runtime.trace_contract import RUNTIME_TRACE_CONTRACT_VERSION

    assert TRACE_EXPORT_CONTRACT_VERSION == 1
    assert RUNTIME_TRACE_CONTRACT_VERSION == 1
    # 指纹改变来自语义覆盖修正，不是版本 bump。
    assert TRACE_CONTRACT_FINGERPRINT != OLD_FINGERPRINT
    assert _HEX64.fullmatch(TRACE_CONTRACT_FINGERPRINT)
