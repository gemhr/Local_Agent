#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Consumer-neutral Trace export contract：不可变 envelope + 严格投影。

WP4-A 的公共消费边界：只把"已完成且通过严格校验的内部 SpanRecord"投影为
不可变、安全、版本化的公共 envelope。它不拥有 Run lifecycle、Event
sequence、Journal durability 或任何 exporter transport；WP4-B 才能消费。

属性投影是 metadata-first 的严格 allowlist：只有各 category schema 明确批准
的键可以跨出观察边界，``SAFE_SPAN_ATTRIBUTES`` 只是内部最大安全记录集合，
绝不自动等于公共导出集合。

公共 envelope 不变量只有**一条** code-owned 校验路径（``_validate_envelope_semantics``）：
``TraceExportEnvelope`` 直接构造、``project_span()`` 与 ``TraceCompatibilityEvaluator``
都必须经过它，因此不存在绕过 projection 的公共构造路径。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from types import MappingProxyType

from core.runtime.tracing import SpanRecord, SpanStatus
from core.runtime.trace_contract import (
    RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
    RUNTIME_OUTPUT_DELIVERY_SPAN,
    RUNTIME_PLANNING_SPAN,
    RUNTIME_RUN_SPAN,
    RUNTIME_STEP_SPAN,
    RUNTIME_SYNTHESIS_SPAN,
    RUNTIME_TRACE_CONTRACT_VERSION,
)
from core.runtime.output_gate import DeliveryStatus, OutputGateState
from core.runtime.planning import ExecutionKind, OutputPolicy, PlanSource
from core.runtime.state import RunStatus, StopReason


# 显式、有限、code-owned 的 export contract identity（与 AgentEvalOps/transport
# 无关；不是 module path、class repr、Git SHA 或 run identity）。
TRACE_EXPORT_CONTRACT_IDENTITY = "localagent.runtime.trace_export"
# 第一个 consumer-neutral Trace export contract。
TRACE_EXPORT_CONTRACT_VERSION = 1

# Exact code-owned v1 duration upper bound.  This is a fixed contract constant,
# not derived from Python float range or sys.float_info at runtime.
MAX_V1_DURATION_INT = 2**1024 - 2**970 - 1

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OPERATION = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

_PUBLIC_TERMINAL_STATUSES = frozenset(
    {
        SpanStatus.OK,
        SpanStatus.ERROR,
        SpanStatus.CANCELLED,
        SpanStatus.TIMED_OUT,
    }
)
_PUBLIC_STATUS_ORDER = (
    SpanStatus.OK,
    SpanStatus.ERROR,
    SpanStatus.CANCELLED,
    SpanStatus.TIMED_OUT,
)


class ExportAttributeType(str, Enum):
    BOOL = "BOOL"
    NON_NEGATIVE_INT = "NON_NEGATIVE_INT"
    FINITE_FLOAT = "FINITE_FLOAT"
    SAFE_IDENTIFIER = "SAFE_IDENTIFIER"
    DIGEST = "DIGEST"


class ExportAttributePresence(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    CONDITIONAL = "CONDITIONAL"
    INTERNAL_ONLY = "INTERNAL_ONLY"


@dataclass(frozen=True, slots=True)
class ValueDomain:
    """字段级 value-domain 约束（kind: ``vocabulary`` | ``range``）。"""

    kind: str
    values: frozenset[str] = frozenset()
    minimum: int | None = None
    maximum: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"vocabulary", "range"}:
            raise ValueError("ValueDomain.kind 必须是 vocabulary 或 range")
        if self.kind == "vocabulary" and not self.values:
            raise ValueError("vocabulary domain 必须非空")


# --- 六类稳定 operation 的严格 category 导出 schema -----------------------
#
# 依据当前 production writers 审计（Phase 3.1 报告 §21 同源）：writer 对同一
# category 的属性写是条件性的（None 值由 set_span_attributes 跳过、typed 管道
# 才写 state/result_char_count、异常传播路径可能完全没有属性），因此导出键以
# OPTIONAL/CONDITIONAL 为主；INTERNAL_ONLY 记录"内部安全但禁止公共导出"的键。
# 每个值都是 (ExportAttributeType, ExportAttributePresence)。

RUN_EXPORT_SCHEMA = MappingProxyType(
    {
        "plan_id": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "plan_version": (ExportAttributeType.NON_NEGATIVE_INT, ExportAttributePresence.OPTIONAL),
        "plan_fingerprint": (ExportAttributeType.DIGEST, ExportAttributePresence.OPTIONAL),
        "planning_source": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "step_count": (ExportAttributeType.NON_NEGATIVE_INT, ExportAttributePresence.OPTIONAL),
        "selected_entry_agent_id": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "runtime_mode": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "final_status": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.CONDITIONAL),
        "stop_reason": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.CONDITIONAL),
        "shape": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        # not_configured 占位归因与未写出的内部键：NOT_PART_OF_WP4A，禁止导出。
        "runtime_version": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.INTERNAL_ONLY),
        "prompt_version": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.INTERNAL_ONLY),
        "model_config_hash": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.INTERNAL_ONLY),
        "toolset_hash": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.INTERNAL_ONLY),
        "kb_version": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.INTERNAL_ONLY),
        "session_id": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.INTERNAL_ONLY),
    }
)

PLANNING_EXPORT_SCHEMA = MappingProxyType(
    {
        "planning_source": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "schema_version": (ExportAttributeType.NON_NEGATIVE_INT, ExportAttributePresence.OPTIONAL),
        "planner_model_invoked": (ExportAttributeType.BOOL, ExportAttributePresence.OPTIONAL),
        "compiled_shape": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "specialist_count": (ExportAttributeType.NON_NEGATIVE_INT, ExportAttributePresence.OPTIONAL),
        "synthesis_required": (ExportAttributeType.BOOL, ExportAttributePresence.OPTIONAL),
        # 内部安全键但当前无 production writer，禁止导出。
        "planner_attempt_count": (ExportAttributeType.NON_NEGATIVE_INT, ExportAttributePresence.INTERNAL_ONLY),
        "planner_timeout_source": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.INTERNAL_ONLY),
    }
)

STEP_EXPORT_SCHEMA = MappingProxyType(
    {
        "preferred_agent": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "execution_kind": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "output_policy": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "dependency_count": (ExportAttributeType.NON_NEGATIVE_INT, ExportAttributePresence.OPTIONAL),
        "state": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.CONDITIONAL),
        "result_char_count": (ExportAttributeType.NON_NEGATIVE_INT, ExportAttributePresence.CONDITIONAL),
        "invocation_role": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.INTERNAL_ONLY),
        "content_type": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.INTERNAL_ONLY),
    }
)

SYNTHESIS_EXPORT_SCHEMA = MappingProxyType(
    {
        "state": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "execution_kind": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
    }
)

DELIVERY_EXPORT_SCHEMA = MappingProxyType(
    {
        "final_step_id": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "output_policy": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "delivery_status": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "gate_terminal_state": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "publish_attempt_count": (ExportAttributeType.NON_NEGATIVE_INT, ExportAttributePresence.OPTIONAL),
        "partially_persisted": (ExportAttributeType.BOOL, ExportAttributePresence.OPTIONAL),
        "output_char_count": (ExportAttributeType.NON_NEGATIVE_INT, ExportAttributePresence.OPTIONAL),
    }
)

MEMORY_EXPORT_SCHEMA = MappingProxyType(
    {
        "persist_enabled": (ExportAttributeType.BOOL, ExportAttributePresence.OPTIONAL),
        "entry_agent_id": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "memory_scope": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "delivery_status": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "user_write_status": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "assistant_write_status": (ExportAttributeType.SAFE_IDENTIFIER, ExportAttributePresence.OPTIONAL),
        "transaction_used": (ExportAttributeType.BOOL, ExportAttributePresence.OPTIONAL),
    }
)

CATEGORY_EXPORT_SCHEMAS = MappingProxyType(
    {
        "run": RUN_EXPORT_SCHEMA,
        "planning": PLANNING_EXPORT_SCHEMA,
        "step": STEP_EXPORT_SCHEMA,
        "synthesis": SYNTHESIS_EXPORT_SCHEMA,
        "delivery": DELIVERY_EXPORT_SCHEMA,
        "memory": MEMORY_EXPORT_SCHEMA,
    }
)


@dataclass(frozen=True, slots=True)
class OperationExportSchema:
    category: str
    step_bound: bool


STABLE_OPERATION_SCHEMAS = MappingProxyType(
    {
        RUNTIME_RUN_SPAN: OperationExportSchema("run", False),
        RUNTIME_PLANNING_SPAN: OperationExportSchema("planning", False),
        RUNTIME_STEP_SPAN: OperationExportSchema("step", True),
        RUNTIME_SYNTHESIS_SPAN: OperationExportSchema("synthesis", True),
        RUNTIME_OUTPUT_DELIVERY_SPAN: OperationExportSchema("delivery", True),
        RUNTIME_FINAL_MEMORY_COMMIT_SPAN: OperationExportSchema("memory", True),
    }
)


# --- 字段级 value-domain 绑定（既有 Owner 词汇表 / 范围） ------------------
#
# 只对存在既有语义 Owner 的字段做 vocabulary/range 绑定；无更强 Owner 的字段
# 保持 SAFE_IDENTIFIER（形状校验），理由见报告 §15。词汇表全部由既有枚举派生，
# 不手写复制，避免与 Owner 漂移。

_PLAN_SOURCE_VALUES = frozenset(
    {item.value for item in PlanSource} | {"unknown"}
)
_PLAN_SHAPE_VALUES = frozenset({"0", "1", "2", "3", "unknown"})
_RUN_STATUS_VALUES = frozenset(item.value for item in RunStatus)
_STOP_REASON_VALUES = frozenset(item.value for item in StopReason)
_EXECUTION_KIND_VALUES = frozenset(item.value for item in ExecutionKind)
_OUTPUT_POLICY_VALUES = frozenset(item.value for item in OutputPolicy)
# 完成的 output_delivery / final_memory_commit Span 上 NOT_APPLICABLE 不可能出现。
_DELIVERY_STATUS_EXPORT_VALUES = frozenset(
    item.value
    for item in DeliveryStatus
    if item is not DeliveryStatus.NOT_APPLICABLE
)
# 完成的 output_delivery Span 只写终态（PUBLISHED/FAILED/OUTCOME_UNKNOWN）。
_GATE_TERMINAL_STATE_EXPORT_VALUES = frozenset(
    item.value
    for item in OutputGateState
    if item not in {OutputGateState.NOT_STARTED, OutputGateState.PUBLISHING}
)
# AgentRouter.DIRECT_MEMORY_SCOPE == "direct"（既有常量）。
_MEMORY_SCOPE_VALUES = frozenset({"direct"})
_MEMORY_WRITE_STATUS_VALUES = frozenset({"NOT_ATTEMPTED", "WRITTEN", "FAILED"})

CATEGORY_ATTRIBUTE_DOMAINS = MappingProxyType(
    {
        "run": MappingProxyType(
            {
                "planning_source": ValueDomain("vocabulary", _PLAN_SOURCE_VALUES),
                "final_status": ValueDomain("vocabulary", _RUN_STATUS_VALUES),
                "stop_reason": ValueDomain("vocabulary", _STOP_REASON_VALUES),
                "shape": ValueDomain("vocabulary", _PLAN_SHAPE_VALUES),
            }
        ),
        "planning": MappingProxyType(
            {
                "planning_source": ValueDomain("vocabulary", _PLAN_SOURCE_VALUES),
                "compiled_shape": ValueDomain("vocabulary", _PLAN_SHAPE_VALUES),
            }
        ),
        "step": MappingProxyType(
            {
                "execution_kind": ValueDomain("vocabulary", _EXECUTION_KIND_VALUES),
                "output_policy": ValueDomain("vocabulary", _OUTPUT_POLICY_VALUES),
            }
        ),
        "synthesis": MappingProxyType(
            {
                "execution_kind": ValueDomain("vocabulary", _EXECUTION_KIND_VALUES),
            }
        ),
        "delivery": MappingProxyType(
            {
                "output_policy": ValueDomain("vocabulary", _OUTPUT_POLICY_VALUES),
                "delivery_status": ValueDomain(
                    "vocabulary", _DELIVERY_STATUS_EXPORT_VALUES
                ),
                "gate_terminal_state": ValueDomain(
                    "vocabulary", _GATE_TERMINAL_STATE_EXPORT_VALUES
                ),
                "publish_attempt_count": ValueDomain(
                    "range", minimum=0, maximum=1
                ),
            }
        ),
        "memory": MappingProxyType(
            {
                "delivery_status": ValueDomain(
                    "vocabulary", _DELIVERY_STATUS_EXPORT_VALUES
                ),
                "memory_scope": ValueDomain("vocabulary", _MEMORY_SCOPE_VALUES),
                "user_write_status": ValueDomain(
                    "vocabulary", _MEMORY_WRITE_STATUS_VALUES
                ),
                "assistant_write_status": ValueDomain(
                    "vocabulary", _MEMORY_WRITE_STATUS_VALUES
                ),
            }
        ),
    }
)


class TraceExportProjectionError(RuntimeError):
    """Fixed-code projection failure；绝不携带 span 内容或 raw 值。"""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)

    def __repr__(self) -> str:
        return f"TraceExportProjectionError(error_code={self.error_code!r})"


class TraceExportEnvelopeError(RuntimeError):
    """envelope 公共语义校验失败；content-free fixed code。

    与 ``TraceExportProjectionError`` 共享同一 code 词表：``project_span()``
    会把此错误转发为对应的 projection error。
    """

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)

    def __repr__(self) -> str:
        return f"TraceExportEnvelopeError(error_code={self.error_code!r})"


def _validate_attribute_value(
    value: object,
    type_: ExportAttributeType,
    domain: ValueDomain | None,
) -> str | None:
    """单个已批准属性值的形状与值域校验；返回固定 code 或 None。"""
    if type_ is ExportAttributeType.BOOL:
        if not isinstance(value, bool):
            return "ATTRIBUTE_TYPE_INVALID"
    elif type_ is ExportAttributeType.NON_NEGATIVE_INT:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            return "ATTRIBUTE_TYPE_INVALID"
    elif type_ is ExportAttributeType.FINITE_FLOAT:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            return "ATTRIBUTE_TYPE_INVALID"
    elif type_ is ExportAttributeType.SAFE_IDENTIFIER:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            return "ATTRIBUTE_TYPE_INVALID"
    elif type_ is ExportAttributeType.DIGEST:
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            return "ATTRIBUTE_TYPE_INVALID"
    else:
        return "ATTRIBUTE_SCHEMA_INVALID"
    if domain is not None:
        if domain.kind == "vocabulary":
            if value not in domain.values:
                return "ATTRIBUTE_DOMAIN_INVALID"
        elif domain.kind == "range":
            if value < domain.minimum or value > domain.maximum:
                return "ATTRIBUTE_DOMAIN_INVALID"
    return None


def _validate_duration_ms(duration_ms: object) -> str | None:
    """Return a fixed contract code for an invalid duration, else ``None``.

    This is the only duration validation Owner.  It avoids ``math.isfinite`` on
    Python ``int`` values (which can raise raw ``OverflowError`` for very large
    ints) and uses direct bounded comparisons against the code-owned
    ``MAX_V1_DURATION_INT`` constant instead.
    """
    if duration_ms is None:
        return "SPAN_DURATION_MISSING"
    if isinstance(duration_ms, bool):
        return "SPAN_DURATION_INVALID"
    if isinstance(duration_ms, int):
        if duration_ms < 0 or duration_ms > MAX_V1_DURATION_INT:
            return "SPAN_DURATION_INVALID"
        return None
    if isinstance(duration_ms, float):
        if not math.isfinite(duration_ms) or duration_ms < 0:
            return "SPAN_DURATION_INVALID"
        return None
    return "SPAN_DURATION_INVALID"


def _validate_envelope_semantics(
    *,
    run_id: object,
    trace_id: object,
    span_id: object,
    parent_span_id: object,
    step_id: object,
    operation: object,
    component: object,
    started_at: object,
    completed_at: object,
    duration_ms: object,
    status: object,
    error_code: object,
    attributes: object,
) -> str | None:
    """TraceExportEnvelope 公共不变量唯一校验路径。

    common fields、completed-span 语义、operation 合法性、step correlation、
    terminal status/error 规则、category attribute schema、属性形状与值域、
    metadata-only 边界全部在此一次性校验。返回首个违反的固定 content-free
    code；``None`` 表示合法。``TraceExportEnvelope.__post_init__``、
    ``project_span()`` 与 ``TraceCompatibilityEvaluator`` 都必须经过它。
    """
    for value in (run_id, trace_id, span_id):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            return "INVALID_IDENTITY"
    if parent_span_id is not None and (
        not isinstance(parent_span_id, str) or not _ID.fullmatch(parent_span_id)
    ):
        return "INVALID_IDENTITY"
    if step_id is not None and (
        not isinstance(step_id, str) or not _ID.fullmatch(step_id)
    ):
        return "INVALID_IDENTITY"
    if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
        return "INVALID_OPERATION"
    if not isinstance(component, str) or not _ID.fullmatch(component):
        return "INVALID_IDENTITY"
    operation_schema = STABLE_OPERATION_SCHEMAS.get(operation)
    if operation_schema is None:
        return "UNSUPPORTED_OPERATION"
    if operation_schema.step_bound and step_id is None:
        return "STEP_CORRELATION_MISSING"
    if not operation_schema.step_bound and step_id is not None:
        return "STEP_CORRELATION_INVALID"
    if (
        not isinstance(started_at, datetime)
        or started_at.tzinfo is None
        or started_at.utcoffset() != timedelta(0)
        or not isinstance(completed_at, datetime)
        or completed_at.tzinfo is None
        or completed_at.utcoffset() != timedelta(0)
    ):
        return "SPAN_TIME_INVALID"
    if completed_at < started_at:
        return "SPAN_TIME_ORDER_INVALID"
    duration_code = _validate_duration_ms(duration_ms)
    if duration_code is not None:
        return duration_code
    if status is SpanStatus.UNSET:
        return "SPAN_STATUS_UNSET"
    if status not in _PUBLIC_TERMINAL_STATUSES:
        return "STATUS_INVALID"
    if status is SpanStatus.OK:
        if error_code is not None:
            return "ERROR_CODE_ON_OK"
    else:
        if (
            error_code is None
            or not isinstance(error_code, str)
            or not _ID.fullmatch(error_code)
        ):
            return "ERROR_CODE_MISSING"
    if not isinstance(attributes, Mapping):
        return "ATTRIBUTES_NOT_MAPPING"
    schema = CATEGORY_EXPORT_SCHEMAS[operation_schema.category]
    domains = CATEGORY_ATTRIBUTE_DOMAINS[operation_schema.category]
    for key, value in attributes.items():
        if key not in schema:
            return "UNKNOWN_ATTRIBUTE_KEY"
        type_, presence = schema[key]
        if presence is ExportAttributePresence.INTERNAL_ONLY:
            return "INTERNAL_ONLY_ATTRIBUTE"
        if value is None:
            return "ATTRIBUTE_VALUE_INVALID"
        code = _validate_attribute_value(value, type_, domains.get(key))
        if code is not None:
            return code
    return None


def validate_trace_export_envelope_semantics(envelope: TraceExportEnvelope) -> None:
    """Public shared Owner wrapper: raise TraceExportEnvelopeError if invalid.

    This is the same single semantic validation path used by direct construction,
    projection and compatibility evaluation; it exists so the standalone
    serializer can validate an envelope without duplicating contract logic.
    """
    if not isinstance(envelope, TraceExportEnvelope):
        raise TypeError("envelope must be a TraceExportEnvelope")
    code = _validate_envelope_semantics(
        run_id=envelope.run_id,
        trace_id=envelope.trace_id,
        span_id=envelope.span_id,
        parent_span_id=envelope.parent_span_id,
        step_id=envelope.step_id,
        operation=envelope.operation,
        component=envelope.component,
        started_at=envelope.started_at,
        completed_at=envelope.completed_at,
        duration_ms=envelope.duration_ms,
        status=envelope.status,
        error_code=envelope.error_code,
        attributes=envelope.attributes,
    )
    if code is not None:
        raise TraceExportEnvelopeError(code)


@dataclass(frozen=True, slots=True)
class TraceExportEnvelope:
    """一个已完成 Span 的不可变公共导出值；不持有任何 live Runtime 对象。

    结构不可变：所有公共字段均为不可变值，``attributes`` 是只读
    ``MappingProxyType`` 且只允许 contract-approved 的不可变标量；嵌套可变
    值（list/dict/set/custom object）在任何公共构造路径上都被拒绝。
    """

    contract_identity: str
    contract_version: int
    contract_fingerprint: str
    run_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    step_id: str | None
    operation: str
    component: str
    started_at: datetime
    completed_at: datetime
    duration_ms: float
    status: SpanStatus
    error_code: str | None
    attributes: Mapping[str, object]

    def __post_init__(self) -> None:
        code = _validate_envelope_semantics(
            run_id=self.run_id,
            trace_id=self.trace_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            step_id=self.step_id,
            operation=self.operation,
            component=self.component,
            started_at=self.started_at,
            completed_at=self.completed_at,
            duration_ms=self.duration_ms,
            status=self.status,
            error_code=self.error_code,
            attributes=self.attributes,
        )
        if code is not None:
            raise TraceExportEnvelopeError(code)
        object.__setattr__(
            self, "attributes", MappingProxyType(dict(self.attributes))
        )


def _current_contract_fingerprint() -> str:
    """延迟取当前 contract fingerprint，避免与 fingerprint 模块循环导入。"""
    from core.runtime.trace_contract_fingerprint import TRACE_CONTRACT_FINGERPRINT

    return TRACE_CONTRACT_FINGERPRINT


def _project_attributes(
    attributes: Mapping[str, object], category: str
) -> dict[str, object]:
    """从内部 SpanRecord 提取已批准键（省略未知键与 INTERNAL_ONLY）。

    类型/值域校验不在这里做——由 ``_validate_envelope_semantics`` 统一完成，
    保证直接构造与投影走同一条校验路径。
    """
    schema = CATEGORY_EXPORT_SCHEMAS[category]
    projected: dict[str, object] = {}
    for key, (type_, presence) in schema.items():
        if presence is ExportAttributePresence.INTERNAL_ONLY:
            continue
        if key not in attributes or attributes[key] is None:
            continue
        projected[key] = attributes[key]
    return projected


def project_span(record: SpanRecord) -> TraceExportEnvelope:
    """把一个内部 SpanRecord 严格投影为公共 envelope；失败以固定 code 拒绝。

    只接受已完成记录（completed_at/duration_ms 存在）。其余公共不变量
    （时间/时长/status/error/step correlation/属性 schema 与值域）统一由
    ``TraceExportEnvelope.__post_init__`` 的共享校验路径执行，本函数只转发
    为 ``TraceExportProjectionError``。投影不修改 SpanRecord、不写 Journal、
    不发 RuntimeEvent、不影响 Tool/Output/Memory。
    """
    if not isinstance(record, SpanRecord):
        raise TypeError("record must be a SpanRecord")
    if record.completed_at is None:
        raise TraceExportProjectionError("SPAN_NOT_COMPLETED")
    if record.duration_ms is None:
        raise TraceExportProjectionError("SPAN_DURATION_MISSING")
    operation_schema = STABLE_OPERATION_SCHEMAS.get(record.operation)
    if operation_schema is None:
        raise TraceExportProjectionError("UNSUPPORTED_OPERATION")
    attributes = _project_attributes(record.attributes, operation_schema.category)
    try:
        return TraceExportEnvelope(
            contract_identity=TRACE_EXPORT_CONTRACT_IDENTITY,
            contract_version=TRACE_EXPORT_CONTRACT_VERSION,
            contract_fingerprint=_current_contract_fingerprint(),
            run_id=record.run_id,
            trace_id=record.trace_id,
            span_id=record.span_id,
            parent_span_id=record.parent_span_id,
            step_id=record.step_id,
            operation=record.operation,
            component=record.component,
            started_at=record.started_at,
            completed_at=record.completed_at,
            duration_ms=record.duration_ms,
            status=record.status,
            error_code=record.error_code,
            attributes=attributes,
        )
    except TraceExportEnvelopeError as exc:
        raise TraceExportProjectionError(exc.error_code) from exc


class CompatibilityReason(str, Enum):
    ACCEPTED = "ACCEPTED"
    IDENTITY_MISSING = "IDENTITY_MISSING"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    VERSION_UNSUPPORTED = "VERSION_UNSUPPORTED"
    FINGERPRINT_MISSING = "FINGERPRINT_MISSING"
    FINGERPRINT_MALFORMED = "FINGERPRINT_MALFORMED"
    FINGERPRINT_UNSUPPORTED = "FINGERPRINT_UNSUPPORTED"
    ENVELOPE_INVALID = "ENVELOPE_INVALID"


@dataclass(frozen=True, slots=True)
class CompatibilityDecision:
    accepted: bool
    reason: CompatibilityReason


class TraceCompatibilityEvaluator:
    """已知/未知 export contract 的 typed 兼容判断；固定 safe reason codes。

    在 identity/version/fingerprint 全部匹配后，还必须经过与
    ``TraceExportEnvelope`` 相同的公共语义校验路径：语义非法的 envelope
    （例如缺 step 关联、未知属性键、伪造枚举值）不能因指纹匹配而被 ACCEPT。
    只判断"该值是否符合受支持 export contract"，不查询 Journal/AgentState/
    RuntimeEvent、不调用 Tool governance、不修改或修复 envelope。
    """

    @staticmethod
    def evaluate(envelope: TraceExportEnvelope) -> CompatibilityDecision:
        if not isinstance(envelope, TraceExportEnvelope):
            raise TypeError("envelope must be a TraceExportEnvelope")
        identity = envelope.contract_identity
        version = envelope.contract_version
        fingerprint = envelope.contract_fingerprint
        if not isinstance(identity, str) or not identity:
            return CompatibilityDecision(False, CompatibilityReason.IDENTITY_MISSING)
        if identity != TRACE_EXPORT_CONTRACT_IDENTITY:
            return CompatibilityDecision(False, CompatibilityReason.IDENTITY_MISMATCH)
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != TRACE_EXPORT_CONTRACT_VERSION
        ):
            return CompatibilityDecision(False, CompatibilityReason.VERSION_UNSUPPORTED)
        if not isinstance(fingerprint, str) or not fingerprint:
            return CompatibilityDecision(False, CompatibilityReason.FINGERPRINT_MISSING)
        if not _DIGEST.fullmatch(fingerprint):
            return CompatibilityDecision(False, CompatibilityReason.FINGERPRINT_MALFORMED)
        if fingerprint != _current_contract_fingerprint():
            return CompatibilityDecision(False, CompatibilityReason.FINGERPRINT_UNSUPPORTED)
        code = _validate_envelope_semantics(
            run_id=envelope.run_id,
            trace_id=envelope.trace_id,
            span_id=envelope.span_id,
            parent_span_id=envelope.parent_span_id,
            step_id=envelope.step_id,
            operation=envelope.operation,
            component=envelope.component,
            started_at=envelope.started_at,
            completed_at=envelope.completed_at,
            duration_ms=envelope.duration_ms,
            status=envelope.status,
            error_code=envelope.error_code,
            attributes=envelope.attributes,
        )
        if code is not None:
            return CompatibilityDecision(False, CompatibilityReason.ENVELOPE_INVALID)
        return CompatibilityDecision(True, CompatibilityReason.ACCEPTED)


def _domain_descriptor(domain: ValueDomain | None) -> dict[str, object] | None:
    """把 ValueDomain 编码为 canonical 描述符形式（content-free）。"""
    if domain is None:
        return None
    if domain.kind == "vocabulary":
        # 词汇表以确定性排序序列化：集合/枚举迭代顺序不得影响指纹。
        return {"kind": "vocabulary", "values": sorted(domain.values)}
    if domain.kind == "range":
        return {"kind": "range", "minimum": domain.minimum, "maximum": domain.maximum}
    return None


def _attribute_schema_descriptor(
    key: str,
    type_: ExportAttributeType,
    presence: ExportAttributePresence,
    domain: ValueDomain | None,
) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "type": type_.value,
        "presence": presence.value,
    }
    domain_descriptor = _domain_descriptor(domain)
    if domain_descriptor is not None:
        descriptor["domain"] = domain_descriptor
    return descriptor


def export_contract_semantic_descriptor() -> dict[str, object]:
    """Consumer-neutral Trace Export Contract Semantic Owner 的唯一权威语义描述符。

    有限、content-free、确定性、JSON-compatible、order-independent（无序集合
    以确定性排序序列化），描述 PUBLIC_VERSIONED export contract 本身而非任何
    runtime 实例。每次调用返回 fresh 结构，调用方无法通过修改返回值改变当前
    合同。``trace_contract_fingerprint.py`` 只消费本描述符做 canonicalize+hash，
    不再独立维护第二份 field/domain/policy literals。
    """
    stable_operations = {
        operation: {
            "category": schema.category,
            "step_bound": schema.step_bound,
        }
        for operation, schema in STABLE_OPERATION_SCHEMAS.items()
    }
    category_schemas = {
        category: {
            key: _attribute_schema_descriptor(
                key,
                type_,
                presence,
                CATEGORY_ATTRIBUTE_DOMAINS[category].get(key),
            )
            for key, (type_, presence) in schema.items()
        }
        for category, schema in CATEGORY_EXPORT_SCHEMAS.items()
    }
    return {
        "contract_identity": TRACE_EXPORT_CONTRACT_IDENTITY,
        "contract_version": TRACE_EXPORT_CONTRACT_VERSION,
        "runtime_trace_contract_version": RUNTIME_TRACE_CONTRACT_VERSION,
        "terminal_statuses": {
            status.value: {} for status in _PUBLIC_STATUS_ORDER
        },
        "status_error_rule": {
            "ok": "error_code_absent",
            "non_ok": "safe_error_code_present",
        },
        "stable_operations": stable_operations,
        "common_field_rules": {
            "run_id": {"type": "SAFE_IDENTIFIER", "presence": "REQUIRED"},
            "trace_id": {"type": "SAFE_IDENTIFIER", "presence": "REQUIRED"},
            "span_id": {"type": "SAFE_IDENTIFIER", "presence": "REQUIRED"},
            "parent_span_id": {"type": "SAFE_IDENTIFIER_OR_ABSENT", "presence": "OPTIONAL"},
            "step_id": {
                "type": "SAFE_IDENTIFIER_OR_ABSENT",
                "presence": "CONDITIONAL",
                "rule": "required for step-bound operations; absent for run/planning",
            },
            "operation": {"type": "SAFE_OPERATION_IDENTIFIER", "presence": "REQUIRED"},
            "component": {"type": "SAFE_IDENTIFIER", "presence": "REQUIRED"},
            "started_at": {"type": "UTC_WALL_CLOCK", "presence": "REQUIRED"},
            "completed_at": {
                "type": "UTC_WALL_CLOCK",
                "presence": "REQUIRED",
                "rule": "not earlier than started_at",
            },
            "duration_ms": {
                "type": "FINITE_NON_NEGATIVE_NUMBER",
                "presence": "REQUIRED",
            },
            "status": {"type": "TERMINAL_SPAN_STATUS", "presence": "REQUIRED"},
            "error_code": {
                "type": "SAFE_IDENTIFIER_OR_ABSENT",
                "presence": "CONDITIONAL",
                "rule": "absent for OK; required safe identifier otherwise",
            },
            "attributes": {
                "type": "STRICT_CATEGORY_ALLOWLIST_MAPPING",
                "presence": "OPTIONAL",
            },
        },
        "category_schemas": category_schemas,
        "extension_operation_policy": {
            "classification": "INTERNAL_RC",
            "projected": False,
            "rule": (
                "lower-level model/tool/retrieval operations rejected as "
                "UNSUPPORTED_OPERATION; never stable top-level operations"
            ),
        },
        "unknown_attribute_behavior": {
            "project_span_unknown_internal_attribute": "OMIT",
            "direct_constructor_unknown_attribute": "INVALID",
        },
        "direct_constructor_policy": (
            "strict semantic validation via shared path; no public construction bypass"
        ),
        "security_policy": {
            "boundary": "metadata_first_strict_allowlist",
            "forbidden": (
                "raw user/agent/model/tool/RAG/memory content, paths, URLs, "
                "secrets, raw exceptions never exported"
            ),
            "placeholder_attribution": (
                "runtime_version/prompt_version/model_config_hash/toolset_hash/"
                "kb_version not_configured; NOT_PART_OF_WP4A; never exported"
            ),
        },
        "compatibility_behavior": {
            "known_identity_version_fingerprint_valid_semantics": "ACCEPT",
            "identity_missing_or_mismatch": "REJECT",
            "version_unsupported": "REJECT",
            "fingerprint_missing_or_malformed_or_unsupported": "REJECT",
            "known_identity_version_fingerprint_invalid_envelope_semantics": (
                "REJECT_ENVELOPE_INVALID"
            ),
        },
        "compatibility_reasons": sorted(
            reason.value for reason in CompatibilityReason
        ),
    }


__all__ = [
    "CATEGORY_ATTRIBUTE_DOMAINS",
    "CATEGORY_EXPORT_SCHEMAS",
    "CompatibilityDecision",
    "CompatibilityReason",
    "DELIVERY_EXPORT_SCHEMA",
    "ExportAttributePresence",
    "ExportAttributeType",
    "MEMORY_EXPORT_SCHEMA",
    "OperationExportSchema",
    "PLANNING_EXPORT_SCHEMA",
    "RUN_EXPORT_SCHEMA",
    "STABLE_OPERATION_SCHEMAS",
    "STEP_EXPORT_SCHEMA",
    "SYNTHESIS_EXPORT_SCHEMA",
    "MAX_V1_DURATION_INT",
    "TRACE_EXPORT_CONTRACT_IDENTITY",
    "TRACE_EXPORT_CONTRACT_VERSION",
    "TraceCompatibilityEvaluator",
    "TraceExportEnvelope",
    "TraceExportEnvelopeError",
    "TraceExportProjectionError",
    "ValueDomain",
    "export_contract_semantic_descriptor",
    "project_span",
    "validate_trace_export_envelope_semantics",
]
