#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4-A Phase 3.2：Consumer-neutral Trace export contract 回归。

只测试不可变 envelope、严格投影、category schema、敏感内容边界与兼容判断。
本文件只使用 safe synthetic IDs、fixed error codes 与合成 marker，不携带
真实密钥或业务正文。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from core.runtime.tracing import SpanRecord, SpanStatus
from core.runtime.trace_export_contract import (
    CATEGORY_EXPORT_SCHEMAS,
    CompatibilityReason,
    STABLE_OPERATION_SCHEMAS,
    TRACE_EXPORT_CONTRACT_IDENTITY,
    TRACE_EXPORT_CONTRACT_VERSION,
    TraceCompatibilityEvaluator,
    TraceExportEnvelope,
    TraceExportEnvelopeError,
    TraceExportProjectionError,
    project_span,
)
from core.runtime.trace_contract import (
    RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
    RUNTIME_OUTPUT_DELIVERY_SPAN,
    RUNTIME_PLANNING_SPAN,
    RUNTIME_RUN_SPAN,
    RUNTIME_STEP_SPAN,
    RUNTIME_SYNTHESIS_SPAN,
)

MARKERS = {
    "prompt": "MARKER_PROMPT_9F31",
    "messages": "MARKER_MESSAGES_9F31",
    "user_input": "MARKER_USER_INPUT_9F31",
    "model_output": "MARKER_MODEL_OUTPUT_9F31",
    "tool_arguments": "MARKER_TOOL_ARGS_9F31",
    "tool_output": "MARKER_TOOL_RESULT_9F31",
    "rag_chunk": "MARKER_RAG_CHUNK_9F31",
    "memory": "MARKER_MEMORY_9F31",
    "canonical_path": "C:\\\\private\\\\resource\\\\path",
    "provider_url": "https://provider.example.invalid/api",
    "api_key": "MARKER_API_KEY_9F31",
    "cookie": "MARKER_COOKIE_9F31",
    "authorization": "MARKER_AUTH_HEADER_9F31",
    "exception_message": "MARKER_EXCEPTION_9F31",
    "traceback": "MARKER_TRACEBACK_9F31",
}


def make_record(
    *,
    operation: str = RUNTIME_RUN_SPAN,
    component: str = "runtime",
    step_id: str | None = None,
    status: SpanStatus = SpanStatus.OK,
    error_code: str | None = None,
    attributes: dict[str, object] | None = None,
    parent_span_id: str | None = None,
) -> SpanRecord:
    started_at = datetime.now(UTC)
    return SpanRecord(
        trace_id="trace-a-1",
        span_id="span-a-1",
        parent_span_id=parent_span_id,
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


def tamper(record: SpanRecord, **values: object) -> SpanRecord:
    """测试专用：绕过 frozen 校验注入非法字段值以验证投影 fail-closed。"""
    for name, value in values.items():
        object.__setattr__(record, name, value)
    return record


def assert_projection_error(record: SpanRecord, code: str) -> None:
    with pytest.raises(TraceExportProjectionError) as exc_info:
        project_span(record)
    assert exc_info.value.error_code == code


# --- 六类稳定 operation 的有效投影 ----------------------------------------


def test_valid_run_projection() -> None:
    record = make_record(
        attributes={
            "plan_id": "plan-1",
            "plan_version": 1,
            "plan_fingerprint": "a" * 64,
            "planning_source": "deterministic",
            "step_count": 2,
            "selected_entry_agent_id": "core_router",
            "runtime_mode": "COORDINATED",
            "final_status": "SUCCEEDED",
            "stop_reason": "COMPLETED",
            "shape": "2",
        }
    )
    envelope = project_span(record)
    assert envelope.contract_identity == TRACE_EXPORT_CONTRACT_IDENTITY
    assert envelope.contract_version == TRACE_EXPORT_CONTRACT_VERSION
    assert envelope.run_id == "run-a-1"
    assert envelope.trace_id == "trace-a-1"
    assert envelope.span_id == "span-a-1"
    assert envelope.parent_span_id is None
    assert envelope.step_id is None
    assert envelope.operation == RUNTIME_RUN_SPAN
    assert envelope.component == "runtime"
    assert envelope.status is SpanStatus.OK
    assert envelope.error_code is None
    assert envelope.duration_ms == 5.0
    assert envelope.attributes["plan_id"] == "plan-1"
    assert envelope.attributes["plan_version"] == 1
    assert envelope.attributes["plan_fingerprint"] == "a" * 64
    assert envelope.attributes["shape"] == "2"
    assert len(envelope.contract_fingerprint) == 64


def test_valid_planning_projection() -> None:
    record = make_record(
        operation=RUNTIME_PLANNING_SPAN,
        component="planner",
        attributes={
            "planning_source": "model_generated",
            "schema_version": 1,
            "planner_model_invoked": True,
            "compiled_shape": "3",
            "specialist_count": 2,
            "synthesis_required": True,
        },
    )
    envelope = project_span(record)
    assert envelope.operation == RUNTIME_PLANNING_SPAN
    assert envelope.attributes["schema_version"] == 1
    assert envelope.attributes["planner_model_invoked"] is True
    assert envelope.attributes["specialist_count"] == 2
    assert envelope.attributes["synthesis_required"] is True


def test_valid_step_projection() -> None:
    record = make_record(
        operation=RUNTIME_STEP_SPAN,
        component="step",
        step_id="step-1",
        attributes={
            "preferred_agent": "knowledge_expert",
            "execution_kind": "AGENT",
            "output_policy": "INTERNAL",
            "dependency_count": 2,
            "state": "SUCCEEDED",
            "result_char_count": 128,
        },
    )
    envelope = project_span(record)
    assert envelope.operation == RUNTIME_STEP_SPAN
    assert envelope.step_id == "step-1"
    assert envelope.attributes["preferred_agent"] == "knowledge_expert"
    assert envelope.attributes["dependency_count"] == 2
    assert envelope.attributes["state"] == "SUCCEEDED"
    assert envelope.attributes["result_char_count"] == 128


def test_valid_synthesis_projection() -> None:
    record = make_record(
        operation=RUNTIME_SYNTHESIS_SPAN,
        component="synthesis",
        step_id="step-final",
        attributes={"state": "SUCCEEDED", "execution_kind": "SYNTHESIS"},
    )
    envelope = project_span(record)
    assert envelope.operation == RUNTIME_SYNTHESIS_SPAN
    assert envelope.attributes["state"] == "SUCCEEDED"
    assert envelope.attributes["execution_kind"] == "SYNTHESIS"


def test_valid_output_delivery_projection() -> None:
    record = make_record(
        operation=RUNTIME_OUTPUT_DELIVERY_SPAN,
        component="output_gate",
        step_id="step-final",
        attributes={
            "final_step_id": "step-final",
            "output_policy": "FINAL_PASSTHROUGH",
            "delivery_status": "DELIVERED",
            "gate_terminal_state": "PUBLISHED",
            "publish_attempt_count": 1,
            "partially_persisted": False,
            "output_char_count": 42,
        },
    )
    envelope = project_span(record)
    assert envelope.operation == RUNTIME_OUTPUT_DELIVERY_SPAN
    assert envelope.attributes["delivery_status"] == "DELIVERED"
    assert envelope.attributes["gate_terminal_state"] == "PUBLISHED"
    assert envelope.attributes["publish_attempt_count"] == 1
    assert envelope.attributes["partially_persisted"] is False


def test_valid_final_memory_commit_projection() -> None:
    record = make_record(
        operation=RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
        component="final_memory",
        step_id="step-final",
        attributes={
            "persist_enabled": True,
            "entry_agent_id": "synthesis_agent",
            "memory_scope": "direct",
            "delivery_status": "DELIVERED",
            "user_write_status": "WRITTEN",
            "assistant_write_status": "WRITTEN",
            "transaction_used": True,
        },
    )
    envelope = project_span(record)
    assert envelope.operation == RUNTIME_FINAL_MEMORY_COMMIT_SPAN
    assert envelope.attributes["persist_enabled"] is True
    assert envelope.attributes["entry_agent_id"] == "synthesis_agent"
    assert envelope.attributes["user_write_status"] == "WRITTEN"
    assert envelope.attributes["transaction_used"] is True


# --- 已完成记录拒绝规则 ---------------------------------------------------


def test_incomplete_span_rejected() -> None:
    record = tamper(make_record(), completed_at=None)
    assert_projection_error(record, "SPAN_NOT_COMPLETED")


def test_missing_duration_rejected() -> None:
    record = tamper(make_record(), duration_ms=None)
    assert_projection_error(record, "SPAN_DURATION_MISSING")


def test_unset_status_rejected() -> None:
    record = tamper(make_record(), status=SpanStatus.UNSET)
    assert_projection_error(record, "SPAN_STATUS_UNSET")


def test_invalid_utc_start_rejected() -> None:
    record = tamper(make_record(), started_at=datetime.now())
    assert_projection_error(record, "SPAN_TIME_INVALID")


def test_invalid_utc_end_rejected() -> None:
    # naive datetime（无 tzinfo）不得作为 completed_at。
    record = tamper(
        make_record(),
        completed_at=datetime(2030, 1, 1, tzinfo=None),
    )
    assert_projection_error(record, "SPAN_TIME_INVALID")


def test_end_before_start_rejected() -> None:
    started_at = datetime.now(UTC)
    record = tamper(
        make_record(),
        completed_at=started_at - timedelta(seconds=1),
    )
    assert_projection_error(record, "SPAN_TIME_ORDER_INVALID")


def test_negative_duration_rejected() -> None:
    record = tamper(make_record(), duration_ms=-1.0)
    assert_projection_error(record, "SPAN_DURATION_INVALID")


def test_nonfinite_duration_rejected() -> None:
    record = tamper(make_record(), duration_ms=float("nan"))
    assert_projection_error(record, "SPAN_DURATION_INVALID")


def test_ok_with_error_code_rejected() -> None:
    record = make_record(status=SpanStatus.OK, error_code="SOMETHING")
    assert_projection_error(record, "ERROR_CODE_ON_OK")


def test_error_without_error_code_rejected() -> None:
    record = make_record(status=SpanStatus.ERROR, error_code=None)
    assert_projection_error(record, "ERROR_CODE_MISSING")


def test_invalid_identity_rejected() -> None:
    record = tamper(make_record(), trace_id="bad id with space")
    assert_projection_error(record, "INVALID_IDENTITY")


def test_step_bound_operation_without_step_rejected() -> None:
    record = make_record(operation=RUNTIME_STEP_SPAN, component="step")
    assert_projection_error(record, "STEP_CORRELATION_MISSING")


def test_non_step_operation_with_step_rejected() -> None:
    record = make_record(operation=RUNTIME_RUN_SPAN, step_id="step-1")
    assert_projection_error(record, "STEP_CORRELATION_INVALID")


def test_unsupported_extension_operation_rejected() -> None:
    record = make_record(
        operation="model.invoke",
        component="model_invocation",
        status=SpanStatus.OK,
    )
    assert_projection_error(record, "UNSUPPORTED_OPERATION")


# --- category 严格投影 -----------------------------------------------------


def test_unknown_internal_attribute_omitted() -> None:
    record = make_record(attributes={"internal_unknown_key": "value"})
    envelope = project_span(record)
    assert "internal_unknown_key" not in envelope.attributes


def test_wrong_type_approved_attribute_rejected() -> None:
    record = make_record(
        operation=RUNTIME_STEP_SPAN,
        component="step",
        step_id="step-1",
        attributes={"dependency_count": "many"},
    )
    assert_projection_error(record, "ATTRIBUTE_TYPE_INVALID")


def test_bool_not_accepted_as_int() -> None:
    record = make_record(attributes={"step_count": True})
    assert_projection_error(record, "ATTRIBUTE_TYPE_INVALID")


def test_non_negative_int_rule_enforced() -> None:
    record = make_record(attributes={"step_count": -1})
    assert_projection_error(record, "ATTRIBUTE_TYPE_INVALID")


def test_not_configured_placeholders_never_exported() -> None:
    record = make_record(
        attributes={
            "runtime_version": "not_configured",
            "prompt_version": "not_configured",
            "model_config_hash": "not_configured",
            "toolset_hash": "not_configured",
            "kb_version": "not_configured",
        }
    )
    envelope = project_span(record)
    for key in (
        "runtime_version",
        "prompt_version",
        "model_config_hash",
        "toolset_hash",
        "kb_version",
    ):
        assert key not in envelope.attributes


def test_internal_only_keys_never_exported() -> None:
    record = make_record(
        operation=RUNTIME_PLANNING_SPAN,
        component="planner",
        attributes={
            "planner_attempt_count": 3,
            "planner_timeout_source": "planner",
        },
    )
    envelope = project_span(record)
    assert "planner_attempt_count" not in envelope.attributes
    assert "planner_timeout_source" not in envelope.attributes


# --- 敏感内容边界 ---------------------------------------------------------


def test_sensitive_markers_absent_from_envelope_and_repr() -> None:
    record = make_record(attributes=dict(MARKERS))
    envelope = project_span(record)
    rendered = repr(envelope)
    for marker in MARKERS.values():
        assert marker not in rendered
        assert all(marker not in str(value) for value in envelope.attributes.values())
    assert all(key not in envelope.attributes for key in MARKERS)


def test_forbidden_content_cannot_enter_projection() -> None:
    record = make_record(attributes=dict(MARKERS))
    envelope = project_span(record)
    assert set(envelope.attributes) == set()


def test_projection_error_is_content_free() -> None:
    record = make_record(
        operation=RUNTIME_STEP_SPAN,
        component="step",
        step_id="step-1",
        attributes={"dependency_count": MARKERS["api_key"]},
    )
    with pytest.raises(TraceExportProjectionError) as exc_info:
        project_span(record)
    assert MARKERS["api_key"] not in str(exc_info.value)
    assert MARKERS["api_key"] not in repr(exc_info.value)
    assert exc_info.value.error_code == "ATTRIBUTE_TYPE_INVALID"


# --- 不可变与失败隔离 -----------------------------------------------------


def test_projection_does_not_mutate_record() -> None:
    attributes = {"plan_id": "plan-1", "final_status": "SUCCEEDED"}
    record = make_record(attributes=attributes)
    before_started = record.started_at
    before_completed = record.completed_at
    before_status = record.status
    before_attributes = dict(record.attributes)
    project_span(record)
    assert record.started_at == before_started
    assert record.completed_at == before_completed
    assert record.status is before_status
    assert dict(record.attributes) == before_attributes


def test_envelope_is_immutable_value() -> None:
    envelope = project_span(make_record(attributes={"plan_id": "plan-1"}))
    assert isinstance(envelope.attributes, MappingProxyType)
    with pytest.raises(AttributeError):
        envelope.run_id = "other-run"  # type: ignore[misc]


def test_envelope_rejects_invalid_identity_shape() -> None:
    started_at = datetime.now(UTC)
    with pytest.raises(TraceExportEnvelopeError) as exc_info:
        TraceExportEnvelope(
            contract_identity=TRACE_EXPORT_CONTRACT_IDENTITY,
            contract_version=TRACE_EXPORT_CONTRACT_VERSION,
            contract_fingerprint="0" * 64,
            run_id="run-1",
            trace_id="bad id with space",
            span_id="span-1",
            parent_span_id=None,
            step_id=None,
            operation=RUNTIME_RUN_SPAN,
            component="runtime",
            started_at=started_at,
            completed_at=started_at + timedelta(milliseconds=5),
            duration_ms=5.0,
            status=SpanStatus.OK,
            error_code=None,
            attributes={},
        )
    assert exc_info.value.error_code == "INVALID_IDENTITY"


# --- 兼容判断 -------------------------------------------------------------


def test_compat_known_contract_accepted() -> None:
    envelope = project_span(make_record())
    decision = TraceCompatibilityEvaluator.evaluate(envelope)
    assert decision.accepted is True
    assert decision.reason is CompatibilityReason.ACCEPTED


def test_compat_missing_identity_rejected() -> None:
    envelope = project_span(make_record())
    altered = _replace(envelope, contract_identity="")
    decision = TraceCompatibilityEvaluator.evaluate(altered)
    assert decision.accepted is False
    assert decision.reason is CompatibilityReason.IDENTITY_MISSING


def test_compat_wrong_identity_rejected() -> None:
    envelope = project_span(make_record())
    altered = _replace(envelope, contract_identity="other.vendor.trace")
    decision = TraceCompatibilityEvaluator.evaluate(altered)
    assert decision.accepted is False
    assert decision.reason is CompatibilityReason.IDENTITY_MISMATCH


def test_compat_unsupported_version_rejected() -> None:
    envelope = project_span(make_record())
    altered = _replace(envelope, contract_version=999)
    decision = TraceCompatibilityEvaluator.evaluate(altered)
    assert decision.accepted is False
    assert decision.reason is CompatibilityReason.VERSION_UNSUPPORTED


def test_compat_missing_fingerprint_rejected() -> None:
    envelope = project_span(make_record())
    altered = _replace(envelope, contract_fingerprint="")
    decision = TraceCompatibilityEvaluator.evaluate(altered)
    assert decision.accepted is False
    assert decision.reason is CompatibilityReason.FINGERPRINT_MISSING


def test_compat_malformed_fingerprint_rejected() -> None:
    envelope = project_span(make_record())
    altered = _replace(envelope, contract_fingerprint="not-a-digest")
    decision = TraceCompatibilityEvaluator.evaluate(altered)
    assert decision.accepted is False
    assert decision.reason is CompatibilityReason.FINGERPRINT_MALFORMED


def test_compat_unknown_fingerprint_rejected() -> None:
    envelope = project_span(make_record())
    altered = _replace(envelope, contract_fingerprint="b" * 64)
    decision = TraceCompatibilityEvaluator.evaluate(altered)
    assert decision.accepted is False
    assert decision.reason is CompatibilityReason.FINGERPRINT_UNSUPPORTED


def test_compat_reason_is_content_free() -> None:
    envelope = project_span(make_record())
    altered = _replace(envelope, contract_identity="vendor-" + MARKERS["api_key"])
    decision = TraceCompatibilityEvaluator.evaluate(altered)
    assert MARKERS["api_key"] not in str(decision.reason)
    assert MARKERS["api_key"] not in repr(decision)


def _replace(envelope: TraceExportEnvelope, **values: object) -> TraceExportEnvelope:
    import dataclasses

    return dataclasses.replace(envelope, **values)


# --- fingerprint 与实例独立性 ---------------------------------------------


def test_two_instances_share_same_contract_fingerprint() -> None:
    first = project_span(
        make_record(
            operation=RUNTIME_STEP_SPAN,
            component="step",
            step_id="step-1",
            status=SpanStatus.OK,
            attributes={"execution_kind": "AGENT"},
        )
    )
    second = project_span(
        make_record(
            operation=RUNTIME_STEP_SPAN,
            component="step",
            step_id="step-2",
            status=SpanStatus.ERROR,
            error_code="STEP_EXECUTION_FAILED",
            attributes={"execution_kind": "AGENT"},
        )
    )
    # 不同 run/span 身份、不同时间、不同 status 结果必须仍携带同一 contract fingerprint。
    assert first.contract_fingerprint == second.contract_fingerprint
    assert first.contract_fingerprint == project_span(make_record()).contract_fingerprint


def test_contract_version_and_identity_are_distinct_facts() -> None:
    from core.runtime.trace_contract import RUNTIME_TRACE_CONTRACT_VERSION

    assert TRACE_EXPORT_CONTRACT_VERSION == 1
    assert RUNTIME_TRACE_CONTRACT_VERSION == 1
    # 两者独立：export version 属于新 export contract，不是复用 Runtime Trace v1。
    assert STABLE_OPERATION_SCHEMAS and CATEGORY_EXPORT_SCHEMAS


# --- H-3 Remediation R1：P1-02 / P1-03 回归 -------------------------------


SYNTHETIC_MARKER = "SYNTHETIC_SECRET_DO_NOT_EXPORT_7F3A"


def make_envelope_kwargs(**over: object) -> dict[str, object]:
    """构造 TraceExportEnvelope 的直接输入（fingerprint 形状合法，值由 evaluate 判定）。"""
    started_at = datetime.now(UTC)
    kwargs: dict[str, object] = dict(
        contract_identity=TRACE_EXPORT_CONTRACT_IDENTITY,
        contract_version=TRACE_EXPORT_CONTRACT_VERSION,
        contract_fingerprint="0" * 64,
        run_id="run-1",
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id=None,
        step_id=None,
        operation=RUNTIME_RUN_SPAN,
        component="runtime",
        started_at=started_at,
        completed_at=started_at + timedelta(milliseconds=5),
        duration_ms=5.0,
        status=SpanStatus.OK,
        error_code=None,
        attributes={},
    )
    kwargs.update(over)
    return kwargs


def assert_envelope_error(expected_code: str, **kwargs: object) -> None:
    with pytest.raises(TraceExportEnvelopeError) as exc_info:
        TraceExportEnvelope(**make_envelope_kwargs(**kwargs))
    assert exc_info.value.error_code == expected_code


def test_direct_constructor_rejects_raw_marker_attribute() -> None:
    # §34：直接构造带 raw marker 的 envelope 必须被拒绝，且错误 content-free。
    with pytest.raises(TraceExportEnvelopeError) as exc_info:
        TraceExportEnvelope(
            **make_envelope_kwargs(attributes={"prompt": SYNTHETIC_MARKER})
        )
    assert exc_info.value.error_code == "UNKNOWN_ATTRIBUTE_KEY"
    assert SYNTHETIC_MARKER not in str(exc_info.value)
    assert SYNTHETIC_MARKER not in repr(exc_info.value)


@pytest.mark.parametrize(
    "bad",
    [
        ["nested", "list"],
        {"nested": "dict"},
        {"set_element"},
        bytearray(b"raw"),
    ],
)
def test_direct_constructor_rejects_nested_mutable_values(bad: object) -> None:
    # §35：嵌套可变值在任何公共构造路径上都不允许成为 envelope 属性。
    assert_envelope_error(
        "ATTRIBUTE_TYPE_INVALID", attributes={"plan_id": bad}
    )


def test_valid_envelope_cannot_be_mutated_from_outside() -> None:
    # §35：外部引用不能改变合法 envelope 的任何公共值。
    envelope = project_span(make_record(attributes={"plan_id": "plan-1"}))
    assert isinstance(envelope.attributes, MappingProxyType)
    with pytest.raises(TypeError):
        envelope.attributes["plan_id"] = "other"  # type: ignore[index]
    with pytest.raises(AttributeError):
        envelope.span_id = "other-span"  # type: ignore[misc]


def test_direct_step_without_step_id_rejected() -> None:
    # §36：直接构造不能绕过 step correlation。
    assert_envelope_error(
        "STEP_CORRELATION_MISSING",
        operation=RUNTIME_STEP_SPAN,
        component="step",
    )


def test_direct_unknown_safe_looking_key_rejected() -> None:
    # §37：看似无害的未知键不能直接进入 envelope。
    assert_envelope_error(
        "UNKNOWN_ATTRIBUTE_KEY",
        attributes={"totally_safe_key": "SAFE_TOKEN"},
    )


def test_raw_marker_absent_from_valid_envelope_repr_and_errors() -> None:
    # §38：合成 marker 不得出现在合法 envelope repr / compatibility / 错误文本。
    record = make_record(attributes={"prompt": SYNTHETIC_MARKER})
    envelope = project_span(record)
    assert SYNTHETIC_MARKER not in repr(envelope)
    assert SYNTHETIC_MARKER not in str(envelope)
    decision = TraceCompatibilityEvaluator.evaluate(envelope)
    assert SYNTHETIC_MARKER not in repr(decision)


def test_delivery_status_vocabulary_enforced() -> None:
    # §39：delivery_status 绑定既有 DeliveryStatus 词汇表（排除 NOT_APPLICABLE）。
    for value in ("DELIVERED", "FAILED", "OUTCOME_UNKNOWN"):
        record = make_record(
            operation=RUNTIME_OUTPUT_DELIVERY_SPAN,
            component="output_gate",
            step_id="step-final",
            attributes={"delivery_status": value},
        )
        assert project_span(record).attributes["delivery_status"] == value
    for value in ("FABRICATED_SAFE_TOKEN", "NOT_APPLICABLE"):
        record = make_record(
            operation=RUNTIME_OUTPUT_DELIVERY_SPAN,
            component="output_gate",
            step_id="step-final",
            attributes={"delivery_status": value},
        )
        assert_projection_error(record, "ATTRIBUTE_DOMAIN_INVALID")
    # final_memory_commit 的 delivery_status 同样绑定 DeliveryStatus。
    record = make_record(
        operation=RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
        component="final_memory",
        step_id="step-final",
        attributes={"delivery_status": "DELIVERED"},
    )
    assert project_span(record).attributes["delivery_status"] == "DELIVERED"


def test_publish_attempt_count_domain() -> None:
    # §40：at-most-once 语义 → publish_attempt_count ∈ {0, 1}。
    for value in (0, 1):
        record = make_record(
            operation=RUNTIME_OUTPUT_DELIVERY_SPAN,
            component="output_gate",
            step_id="step-final",
            attributes={
                "publish_attempt_count": value,
                "delivery_status": "DELIVERED",
            },
        )
        assert (
            project_span(record).attributes["publish_attempt_count"] == value
        )
    for value in (2, -1, True):
        record = make_record(
            operation=RUNTIME_OUTPUT_DELIVERY_SPAN,
            component="output_gate",
            step_id="step-final",
            attributes={
                "publish_attempt_count": value,
                "delivery_status": "DELIVERED",
            },
        )
        expected = (
            "ATTRIBUTE_DOMAIN_INVALID" if value == 2 else "ATTRIBUTE_TYPE_INVALID"
        )
        assert_projection_error(record, expected)


def test_gate_terminal_state_vocabulary() -> None:
    # §41：gate_terminal_state 绑定 OutputGateState 终态子集。
    for value in ("PUBLISHED", "FAILED", "OUTCOME_UNKNOWN"):
        record = make_record(
            operation=RUNTIME_OUTPUT_DELIVERY_SPAN,
            component="output_gate",
            step_id="step-final",
            attributes={"gate_terminal_state": value},
        )
        assert project_span(record).attributes["gate_terminal_state"] == value
    for value in ("NOT_STARTED", "PUBLISHING", "FABRICATED_STATE"):
        record = make_record(
            operation=RUNTIME_OUTPUT_DELIVERY_SPAN,
            component="output_gate",
            step_id="step-final",
            attributes={"gate_terminal_state": value},
        )
        assert_projection_error(record, "ATTRIBUTE_DOMAIN_INVALID")


@pytest.mark.parametrize(
    "operation, component, step_id, key, valid, invalid",
    [
        (
            RUNTIME_RUN_SPAN,
            "runtime",
            None,
            "planning_source",
            ("deterministic", "model_generated", "legacy_adapter", "unknown"),
            "FABRICATED_SOURCE",
        ),
        (
            RUNTIME_RUN_SPAN,
            "runtime",
            None,
            "final_status",
            ("SUCCEEDED", "FAILED", "CANCELLED", "CREATED", "RUNNING"),
            "FABRICATED_STATUS",
        ),
        (
            RUNTIME_RUN_SPAN,
            "runtime",
            None,
            "stop_reason",
            ("COMPLETED", "NO_ACTION", "BUDGET_EXHAUSTED", "PLANNING_FAILED"),
            "FABRICATED_REASON",
        ),
        (
            RUNTIME_RUN_SPAN,
            "runtime",
            None,
            "shape",
            ("0", "1", "2", "3", "unknown"),
            "7",
        ),
        (
            RUNTIME_PLANNING_SPAN,
            "planner",
            None,
            "compiled_shape",
            ("0", "3", "unknown"),
            "9",
        ),
        (
            RUNTIME_STEP_SPAN,
            "step",
            "step-1",
            "execution_kind",
            ("AGENT", "SYNTHESIS"),
            "FABRICATED_KIND",
        ),
        (
            RUNTIME_STEP_SPAN,
            "step",
            "step-1",
            "output_policy",
            ("INTERNAL", "FINAL_PASSTHROUGH", "FINAL_SYNTHESIS"),
            "FABRICATED_POLICY",
        ),
        (
            RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
            "final_memory",
            "step-final",
            "memory_scope",
            ("direct",),
            "nested_scope",
        ),
        (
            RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
            "final_memory",
            "step-final",
            "user_write_status",
            ("NOT_ATTEMPTED", "WRITTEN", "FAILED"),
            "FABRICATED_WRITE",
        ),
    ],
)
def test_enum_like_domain_binding(
    operation: str,
    component: str,
    step_id: str | None,
    key: str,
    valid: tuple[str, ...],
    invalid: str,
) -> None:
    # §42：每个绑定 Owner 词汇表的字段：全部合法值接受、伪造值拒绝。
    for value in valid:
        record = make_record(
            operation=operation,
            component=component,
            step_id=step_id,
            attributes={key: value},
        )
        assert project_span(record).attributes[key] == value
    record = make_record(
        operation=operation,
        component=component,
        step_id=step_id,
        attributes={key: invalid},
    )
    assert_projection_error(record, "ATTRIBUTE_DOMAIN_INVALID")


def test_compat_known_valid_envelope_still_accepted() -> None:
    # §44：project_span() 产生的合法 envelope（identity/version/fingerprint/
    # 语义全部正确）必须仍 ACCEPT。
    envelope = project_span(make_record(attributes={"plan_id": "plan-1"}))
    decision = TraceCompatibilityEvaluator.evaluate(envelope)
    assert decision.accepted is True
    assert decision.reason is CompatibilityReason.ACCEPTED


def test_compat_known_fingerprint_invalid_envelope_rejected() -> None:
    # §45：identity/version/fingerprint 全匹配但语义非法的 envelope 必须 REJECT，
    # 直接关闭 Final Gate bypass。
    envelope = project_span(make_record(attributes={"plan_id": "plan-1"}))
    # 绕过 __post_init__ 注入未知键（测试专用）：evaluate 是语义消费前最后关卡。
    object.__setattr__(
        envelope, "attributes", {"totally_safe_key": "SAFE_TOKEN"}
    )
    decision = TraceCompatibilityEvaluator.evaluate(envelope)
    assert decision.accepted is False
    assert decision.reason is CompatibilityReason.ENVELOPE_INVALID


@pytest.mark.parametrize(
    "attribute",
    [
        {"prompt": SYNTHETIC_MARKER},
        {"shape": ["nested"]},
        {"delivery_status": "FABRICATED_SAFE_TOKEN"},
    ],
)
def test_compat_invalid_envelope_reason_is_content_free(
    attribute: dict[str, object],
) -> None:
    envelope = project_span(make_record(attributes={"plan_id": "plan-1"}))
    object.__setattr__(envelope, "attributes", attribute)
    decision = TraceCompatibilityEvaluator.evaluate(envelope)
    assert decision.accepted is False
    assert decision.reason is CompatibilityReason.ENVELOPE_INVALID
    assert SYNTHETIC_MARKER not in repr(decision)
    assert "FABRICATED_SAFE_TOKEN" not in repr(decision)


def test_cross_category_attributes_rejected() -> None:
    # §27：delivery 字段不能进入 runtime.run；category schema 不能跨类并集。
    # 直接构造路径拒绝跨类键；projection 路径对未知键按 §26 省略。
    assert_envelope_error(
        "UNKNOWN_ATTRIBUTE_KEY",
        attributes={"delivery_status": "DELIVERED"},
    )
    record = make_record(attributes={"delivery_status": "DELIVERED"})
    assert "delivery_status" not in project_span(record).attributes
