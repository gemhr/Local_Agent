#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Runtime Metrics descriptor、内存 recorder、事件投影与 Gauge 快照。"""

from __future__ import annotations

import math
import re
import threading
import weakref
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol

from core.runtime.blocking_executor import (
    BlockingTaskKind,
    BoundedBlockingExecutor,
)
from core.runtime.circuit_breaker import (
    ModelCircuitBreakerRegistry,
    ModelCircuitState,
)
from core.runtime.event_channel import RuntimeEventChannel
from core.runtime.event_journal import JournalRecord
from core.runtime.events import RuntimeEventType
from core.runtime.observability import (
    RuntimeGaugeProvider,
    RuntimeInfrastructureMetricsHook,
)
from core.runtime.run_registry import RunRegistry
from core.runtime.tool_concurrency import ToolConcurrencyController


class MetricType(str, Enum):
    COUNTER = "COUNTER"
    GAUGE = "GAUGE"
    HISTOGRAM = "HISTOGRAM"


_DENIED_LABELS = frozenset(
    {
        "run_id",
        "trace_id",
        "event_id",
        "step_id",
        "invocation_id",
        "attempt_id",
        "retrieval_id",
        "query_digest",
        "resource_key_digest",
        "source_id",
        "chunk_id",
        "citation_id",
        "safe_message",
        "user_input",
        "filename",
        "file_path",
        "url",
    }
)
_GLOBAL_ALLOWED_LABELS = frozenset(
    {
        "component",
        "event_type",
        "status",
        "error_code",
        "model_profile",
        "retry_disposition",
        "retrieval_stage",
        "cancellation_reason",
        "side_effect_state",
        "runtime_mode",
        "tool_name",
        "budget_dimension",
        "planning_source",
        "reason",
        "execution_kind",
        "output_policy",
        "shape",
        "delivery_status",
        "memory_commit_status",
        "agent_id",
    }
)
_SAFE_LABEL_VALUE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


_DELIVERY_ERROR_CODES = frozenset(
    {
        "OK",
        "FINAL_OUTPUT_DELIVERY_FAILED",
        "FINAL_OUTPUT_DELIVERY_UNKNOWN",
        "OUTPUT_GATE_DUPLICATE_ATTEMPT",
        "OUTPUT_GATE_INTERNAL_STEP",
        "OUTPUT_GATE_NOT_FINAL",
        "OUTPUT_GATE_STEP_NOT_CLAIMED",
        "OUTPUT_GATE_STEP_NOT_SUCCEEDED",
        "OUTPUT_GATE_STORE_NOT_READABLE",
        "OUTPUT_GATE_STORE_SEALED",
        "OUTPUT_GATE_RUN_NOT_ACTIVE",
        "OUTPUT_GATE_CLAIM_MISMATCH",
        "OUTPUT_GATE_CLOSED",
    }
)
_MEMORY_ERROR_CODES = frozenset(
    {
        "OK",
        "FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
        "DUPLICATE_EXCHANGE",
        "MEMORY_EXCHANGE_INVALID_ARGUMENT",
        "MEMORY_EXCHANGE_FAILED",
        "MEMORY_MANAGER_UNAVAILABLE",
    }
)
_EXECUTION_KIND_VALUES = frozenset({"AGENT", "SYNTHESIS", "unknown"})
_OUTPUT_POLICY_VALUES = frozenset(
    {"INTERNAL", "FINAL_PASSTHROUGH", "FINAL_SYNTHESIS", "unknown"}
)
_SHAPE_VALUES = frozenset({"0", "1", "2", "3", "unknown"})
_DELIVERY_STATUS_VALUES = frozenset(
    {"DELIVERED", "FAILED", "OUTCOME_UNKNOWN", "NOT_APPLICABLE", "unknown"}
)
_MEMORY_STATUS_VALUES = frozenset(
    {"SUCCEEDED", "FAILED", "NOT_ATTEMPTED", "unknown"}
)


@dataclass(frozen=True, slots=True)
class MetricLabelPolicy:
    allowed_labels: frozenset[str] = _GLOBAL_ALLOWED_LABELS
    denied_labels: frozenset[str] = _DENIED_LABELS
    tool_name_allowlist: frozenset[str] = frozenset()
    agent_id_allowlist: frozenset[str] = frozenset()
    maximum_value_length: int = 64

    def normalize(
        self, labels: Mapping[str, str], descriptor: "MetricDescriptor"
    ) -> tuple[tuple[str, str], ...]:
        if not isinstance(labels, Mapping):
            raise TypeError("labels 必须是 Mapping")
        supplied = set(labels)
        if supplied & self.denied_labels:
            raise ValueError("Metric Label 包含高基数禁止字段")
        if not supplied <= self.allowed_labels:
            raise ValueError("Metric Label 不在全局 allowlist")
        if not supplied <= descriptor.allowed_labels:
            raise ValueError("Metric Label 未被 descriptor 允许")
        if not descriptor.required_labels <= supplied:
            raise ValueError("Metric 缺少 required label")
        normalized: list[tuple[str, str]] = []
        bounded = descriptor.bounded_values or {}
        for key, raw_value in labels.items():
            if not isinstance(raw_value, str):
                raise TypeError("Metric Label Value 必须是字符串")
            value = raw_value
            if key == "tool_name" and value not in self.tool_name_allowlist:
                value = "other"
            if key == "agent_id" and value not in self.agent_id_allowlist:
                value = "other"
            if (
                len(value) > self.maximum_value_length
                or not _SAFE_LABEL_VALUE.fullmatch(value)
            ):
                raise ValueError("Metric Label Value 不是安全有限字符串")
            allowed_values = bounded.get(key)
            if allowed_values is not None and value not in allowed_values:
                raise ValueError("Metric Label Value 不在 descriptor 有限集合")
            normalized.append((key, value))
        return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class MetricDescriptor:
    name: str
    type: MetricType
    description: str
    unit: str
    allowed_labels: frozenset[str] = frozenset()
    required_labels: frozenset[str] = frozenset()
    bounded_values: Mapping[str, frozenset[str]] | None = None

    def __post_init__(self) -> None:
        if not self.name.startswith("runtime_"):
            raise ValueError("Metric 名称必须使用 runtime_ 前缀")
        if self.type is MetricType.COUNTER and not self.name.endswith("_total"):
            raise ValueError("Counter 必须以 _total 结尾")
        if self.type is MetricType.GAUGE and self.name.endswith("_total"):
            raise ValueError("Gauge 不能以 _total 结尾")
        if self.type is MetricType.HISTOGRAM and self.unit == "seconds":
            if not self.name.endswith("_seconds"):
                raise ValueError("Duration Histogram 必须以 _seconds 结尾")
        if self.required_labels - self.allowed_labels:
            raise ValueError("required_labels 必须属于 allowed_labels")
        if self.allowed_labels & _DENIED_LABELS:
            raise ValueError("descriptor 不得允许高基数 Label")
        if not self.allowed_labels <= _GLOBAL_ALLOWED_LABELS:
            raise ValueError("descriptor Label 不在全局 allowlist")
        if self.bounded_values is not None and not set(
            self.bounded_values
        ) <= self.allowed_labels:
            raise ValueError("bounded_values 只能约束 allowed label")
        if self.bounded_values is not None:
            object.__setattr__(
                self,
                "bounded_values",
                MappingProxyType(
                    {
                        key: frozenset(values)
                        for key, values in self.bounded_values.items()
                    }
                ),
            )


def _descriptor(
    name: str,
    type_: MetricType,
    description: str,
    unit: str,
    *labels: str,
) -> MetricDescriptor:
    return MetricDescriptor(
        name=name,
        type=type_,
        description=description,
        unit=unit,
        allowed_labels=frozenset(labels),
        required_labels=frozenset(labels),
    )


RUNTIME_METRIC_DESCRIPTORS = tuple(
    [
        _descriptor("runtime_runs_total", MetricType.COUNTER, "终态 Run 数", "runs", "status"),
        _descriptor("runtime_runs_started_total", MetricType.COUNTER, "已开始 Run 数", "runs"),
        _descriptor("runtime_steps_total", MetricType.COUNTER, "终态 Step 数", "steps", "status"),
        MetricDescriptor(
            name="runtime_step_total",
            type=MetricType.COUNTER,
            description="按执行分类/输出策略统计的终态 Step 数",
            unit="steps",
            allowed_labels=frozenset(
                {"execution_kind", "output_policy", "status"}
            ),
            required_labels=frozenset(
                {"execution_kind", "output_policy", "status"}
            ),
            bounded_values={
                "execution_kind": _EXECUTION_KIND_VALUES,
                "output_policy": _OUTPUT_POLICY_VALUES,
            },
        ),
        _descriptor("runtime_model_attempts_total", MetricType.COUNTER, "Model Attempt 数", "attempts", "model_profile"),
        _descriptor("runtime_tool_attempts_total", MetricType.COUNTER, "Tool Attempt 数", "attempts", "tool_name"),
        _descriptor("runtime_retrievals_total", MetricType.COUNTER, "Retrieval 数", "retrievals"),
        _descriptor("runtime_planning_total", MetricType.COUNTER, "Planning resolution count", "resolutions", "planning_source", "status"),
        MetricDescriptor(
            name="runtime_multi_agent_runs_total",
            type=MetricType.COUNTER,
            description="按执行图 shape 统计的终态 Multi-Agent Run 数",
            unit="runs",
            allowed_labels=frozenset({"shape", "status"}),
            required_labels=frozenset({"shape", "status"}),
            bounded_values={"shape": _SHAPE_VALUES},
        ),
        MetricDescriptor(
            name="runtime_specialist_count",
            type=MetricType.HISTOGRAM,
            description="每次 Run 的 INTERNAL specialist 数量分布",
            unit="specialists",
            allowed_labels=frozenset({"shape"}),
            required_labels=frozenset({"shape"}),
            bounded_values={"shape": _SHAPE_VALUES},
        ),
        _descriptor("runtime_synthesis_total", MetricType.COUNTER, "Synthesis Step 终态数", "steps", "status"),
        MetricDescriptor(
            name="runtime_output_delivery_total",
            type=MetricType.COUNTER,
            description="Final output delivery 尝试结果",
            unit="deliveries",
            allowed_labels=frozenset({"status", "error_code"}),
            required_labels=frozenset({"status", "error_code"}),
            bounded_values={
                "status": _DELIVERY_STATUS_VALUES,
                "error_code": _DELIVERY_ERROR_CODES,
            },
        ),
        MetricDescriptor(
            name="runtime_output_delivery_duration_seconds",
            type=MetricType.HISTOGRAM,
            description="Final output delivery 时长",
            unit="seconds",
            allowed_labels=frozenset({"status"}),
            required_labels=frozenset({"status"}),
            bounded_values={"status": _DELIVERY_STATUS_VALUES},
        ),
        _descriptor(
            "runtime_output_partial_persisted_total",
            MetricType.COUNTER,
            "Final output 部分持久化次数",
            "events",
        ),
        MetricDescriptor(
            name="runtime_final_memory_commit_total",
            type=MetricType.COUNTER,
            description="Final delivered exchange Memory 提交结果",
            unit="commits",
            allowed_labels=frozenset({"status", "error_code"}),
            required_labels=frozenset({"status", "error_code"}),
            bounded_values={
                "status": _MEMORY_STATUS_VALUES,
                "error_code": _MEMORY_ERROR_CODES,
            },
        ),
        MetricDescriptor(
            name="runtime_final_memory_commit_duration_seconds",
            type=MetricType.HISTOGRAM,
            description="Final delivered exchange Memory 提交时长",
            unit="seconds",
            allowed_labels=frozenset({"status"}),
            required_labels=frozenset({"status"}),
            bounded_values={"status": _MEMORY_STATUS_VALUES},
        ),
        _descriptor("runtime_retries_total", MetricType.COUNTER, "Retry Attempt 数", "retries", "component"),
        _descriptor("runtime_budget_exhaustions_total", MetricType.COUNTER, "预算耗尽数", "events", "component", "budget_dimension", "status"),
        _descriptor("runtime_timeouts_total", MetricType.COUNTER, "超时数", "events", "component", "status"),
        _descriptor("runtime_cancellations_total", MetricType.COUNTER, "取消数", "events", "component", "cancellation_reason", "status"),
        _descriptor("runtime_journal_append_failures_total", MetricType.COUNTER, "Journal 追加失败数", "events"),
        _descriptor("runtime_event_duplicates_total", MetricType.COUNTER, "重复检测次数", "occurrences", "component"),
        _descriptor("runtime_observability_dropped_records_total", MetricType.COUNTER, "Observability 丢弃记录数", "records"),
        _descriptor(
            "runtime_trace_dropped_spans_total",
            MetricType.COUNTER,
            "Trace 丢弃 Span 数",
            "spans",
            "component",
            "reason",
        ),
        _descriptor("runtime_active_runs", MetricType.GAUGE, "当前活跃 Run", "runs"),
        _descriptor("runtime_active_steps", MetricType.GAUGE, "当前活跃 Step", "steps"),
        _descriptor("runtime_detached_tool_workers", MetricType.GAUGE, "Detached Tool Worker", "workers"),
        _descriptor("runtime_detached_retrieval_workers", MetricType.GAUGE, "Detached Retrieval Worker", "workers"),
        _descriptor("runtime_blocking_executor_active", MetricType.GAUGE, "Blocking Executor 活跃任务", "tasks"),
        _descriptor("runtime_blocking_executor_pending", MetricType.GAUGE, "Blocking Executor 等待任务", "tasks"),
        _descriptor("runtime_event_channel_buffered", MetricType.GAUGE, "Event Channel 缓冲事件", "events"),
        _descriptor("runtime_circuit_breakers_open", MetricType.GAUGE, "Open Circuit Breaker", "breakers"),
        _descriptor("runtime_run_duration_seconds", MetricType.HISTOGRAM, "Run 时长", "seconds", "status"),
        MetricDescriptor(
            name="runtime_step_duration_seconds",
            type=MetricType.HISTOGRAM,
            description="按执行分类/输出策略统计的 Step 时长",
            unit="seconds",
            allowed_labels=frozenset(
                {"execution_kind", "output_policy", "status"}
            ),
            required_labels=frozenset(
                {"execution_kind", "output_policy", "status"}
            ),
            bounded_values={
                "execution_kind": _EXECUTION_KIND_VALUES,
                "output_policy": _OUTPUT_POLICY_VALUES,
            },
        ),
        _descriptor("runtime_model_duration_seconds", MetricType.HISTOGRAM, "Model Attempt 时长", "seconds", "status", "model_profile"),
        _descriptor("runtime_tool_duration_seconds", MetricType.HISTOGRAM, "Tool Attempt 时长", "seconds", "status", "tool_name"),
        _descriptor("runtime_retrieval_duration_seconds", MetricType.HISTOGRAM, "Retrieval 时长", "seconds", "status"),
        _descriptor("runtime_planning_duration_seconds", MetricType.HISTOGRAM, "Planning resolution duration", "seconds", "planning_source", "status"),
        _descriptor("runtime_retrieval_stage_duration_seconds", MetricType.HISTOGRAM, "Retrieval Stage 时长", "seconds", "status", "retrieval_stage"),
        _descriptor("runtime_journal_append_duration_seconds", MetricType.HISTOGRAM, "Journal 追加时长", "seconds"),
        _descriptor("runtime_blocking_executor_wait_seconds", MetricType.HISTOGRAM, "Blocking Executor 等待时长", "seconds"),
    ]
)
DEFAULT_RUNTIME_METRIC_REGISTRY = MappingProxyType(
    {item.name: item for item in RUNTIME_METRIC_DESCRIPTORS}
)


@dataclass(frozen=True, slots=True)
class RuntimeMetricsSnapshot:
    counters: Mapping[tuple[str, tuple[tuple[str, str], ...]], float]
    gauges: Mapping[tuple[str, tuple[tuple[str, str], ...]], float]
    histograms: Mapping[
        tuple[str, tuple[tuple[str, str], ...]], tuple[float, ...]
    ]

    def counter(self, name: str, labels: Mapping[str, str] | None = None) -> float:
        return self.counters.get((name, tuple(sorted((labels or {}).items()))), 0.0)

    def gauge(self, name: str, labels: Mapping[str, str] | None = None) -> float:
        return self.gauges.get((name, tuple(sorted((labels or {}).items()))), 0.0)

    def histogram(
        self, name: str, labels: Mapping[str, str] | None = None
    ) -> tuple[float, ...]:
        return self.histograms.get(
            (name, tuple(sorted((labels or {}).items()))), ()
        )


class RuntimeMetricsRecorder(Protocol):
    label_policy: MetricLabelPolicy

    def increment_counter(
        self, name: str, value: float = 1, *, labels: Mapping[str, str] | None = None
    ) -> None: ...

    def set_gauge(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None: ...

    def observe_histogram(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None: ...

    def snapshot(
        self, *, gauge_provider: RuntimeGaugeProvider | None = None
    ) -> RuntimeMetricsSnapshot: ...


class InMemoryMetricsRecorder:
    def __init__(
        self,
        descriptors: Iterable[MetricDescriptor] = RUNTIME_METRIC_DESCRIPTORS,
        *,
        label_policy: MetricLabelPolicy | None = None,
    ) -> None:
        values = tuple(descriptors)
        self.descriptors = {item.name: item for item in values}
        if len(self.descriptors) != len(values):
            raise ValueError("Metric descriptor 名称重复")
        self.label_policy = label_policy or MetricLabelPolicy()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[
            tuple[str, tuple[tuple[str, str], ...]], list[float]
        ] = {}
        self._lock = threading.RLock()

    def _key(
        self, name: str, expected: MetricType, labels: Mapping[str, str] | None
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        descriptor = self.descriptors.get(name)
        if descriptor is None:
            raise ValueError("未注册 Metric")
        if descriptor.type is not expected:
            raise ValueError("Metric 类型不匹配")
        return name, self.label_policy.normalize(labels or {}, descriptor)

    @staticmethod
    def _number(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("Metric Value 必须是数字")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Metric Value 必须有限")
        return result

    def increment_counter(
        self, name: str, value: float = 1, *, labels: Mapping[str, str] | None = None
    ) -> None:
        number = self._number(value)
        if number < 0:
            raise ValueError("Counter 不能增加负数")
        key = self._key(name, MetricType.COUNTER, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + number

    def set_gauge(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        number = self._number(value)
        key = self._key(name, MetricType.GAUGE, labels)
        with self._lock:
            self._gauges[key] = number

    def observe_histogram(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        number = self._number(value)
        if number < 0:
            raise ValueError("Histogram 样本不能为负")
        key = self._key(name, MetricType.HISTOGRAM, labels)
        with self._lock:
            self._histograms.setdefault(key, []).append(number)

    def snapshot(
        self, *, gauge_provider: RuntimeGaugeProvider | None = None
    ) -> RuntimeMetricsSnapshot:
        if gauge_provider is not None:
            try:
                record_gauge_snapshot(self, gauge_provider)
            except Exception:
                # Gauge collect 是独立读路径，失败不得影响累计指标。
                pass
        with self._lock:
            return RuntimeMetricsSnapshot(
                counters=MappingProxyType(dict(self._counters)),
                gauges=MappingProxyType(dict(self._gauges)),
                histograms=MappingProxyType(
                    {key: tuple(value) for key, value in self._histograms.items()}
                ),
            )


class NoopMetricsRecorder:
    label_policy = MetricLabelPolicy()

    def increment_counter(
        self, name: str, value: float = 1, *, labels: Mapping[str, str] | None = None
    ) -> None:
        return None

    def set_gauge(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        return None

    def observe_histogram(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        return None

    def snapshot(
        self, *, gauge_provider: RuntimeGaugeProvider | None = None
    ) -> RuntimeMetricsSnapshot:
        empty = MappingProxyType({})
        return RuntimeMetricsSnapshot(empty, empty, empty)


class RuntimeMetricsProjector:
    """Event -> Metrics 的唯一映射所有者。"""

    def __init__(self, recorder: RuntimeMetricsRecorder) -> None:
        self.recorder = recorder

    @staticmethod
    def _status(payload: Mapping[str, object]) -> str:
        status = payload.get("status")
        if isinstance(status, str):
            return status
        succeeded = payload.get("succeeded")
        if isinstance(succeeded, bool):
            return "SUCCEEDED" if succeeded else "FAILED"
        return "UNKNOWN"

    @staticmethod
    def _duration_seconds(payload: Mapping[str, object]) -> float | None:
        value = payload.get("duration_ms")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Completed duration_ms 必须是非负整数")
        result = float(value) / 1000.0
        if not math.isfinite(result):
            raise ValueError("Completed duration 必须是有限数")
        return result

    @staticmethod
    def _outcome_kind(payload: Mapping[str, object]) -> str | None:
        status = RuntimeMetricsProjector._status(payload).upper()
        code = str(payload.get("safe_error_code", "")).upper()
        if "CANCEL" in code or status == "CANCELLED":
            return "cancellation"
        if "BUDGET" in code:
            return "budget"
        if (
            "TIMEOUT" in code
            or "DEADLINE" in code
            or status in {"TIMED_OUT", "TIMEOUT"}
        ):
            return "timeout"
        return None

    def _project_component_outcome(
        self, component: str, payload: Mapping[str, object]
    ) -> None:
        kind = self._outcome_kind(payload)
        status = self._status(payload)
        if kind == "timeout":
            self.recorder.increment_counter(
                "runtime_timeouts_total",
                labels={"component": component, "status": status},
            )
        elif kind == "budget":
            self.recorder.increment_counter(
                "runtime_budget_exhaustions_total",
                labels={
                    "component": component,
                    "budget_dimension": "unknown",
                    "status": status,
                },
            )
        elif kind == "cancellation":
            self.recorder.increment_counter(
                "runtime_cancellations_total",
                labels={
                    "component": component,
                    "cancellation_reason": str(
                        payload.get("safe_error_code", "UNKNOWN")
                    ),
                    "status": status,
                },
            )

    def project(self, record: JournalRecord) -> None:
        if not isinstance(record, JournalRecord):
            raise TypeError("RuntimeMetricsProjector 只接受 JournalRecord")
        record.verify()
        event_type = record.event_type
        payload = record.safe_payload
        if event_type is RuntimeEventType.RUN_STARTED:
            self.recorder.increment_counter("runtime_runs_started_total")
        elif event_type is RuntimeEventType.RUN_COMPLETED:
            status = self._status(payload)
            self.recorder.increment_counter(
                "runtime_runs_total", labels={"status": status}
            )
            shape = payload.get("shape")
            if not isinstance(shape, str) or shape not in _SHAPE_VALUES:
                shape = "unknown"
            self.recorder.increment_counter(
                "runtime_multi_agent_runs_total",
                labels={"shape": shape, "status": status},
            )
            duration = self._duration_seconds(payload)
            if duration is not None:
                self.recorder.observe_histogram(
                    "runtime_run_duration_seconds",
                    duration,
                    labels={"status": status},
                )
        elif event_type is RuntimeEventType.STEP_COMPLETED:
            status = self._status(payload)
            self.recorder.increment_counter(
                "runtime_steps_total", labels={"status": status}
            )
            execution_kind = payload.get("execution_kind")
            output_policy = payload.get("output_policy")
            execution_kind = (
                execution_kind
                if isinstance(execution_kind, str)
                and execution_kind in _EXECUTION_KIND_VALUES
                else "unknown"
            )
            output_policy = (
                output_policy
                if isinstance(output_policy, str)
                and output_policy in _OUTPUT_POLICY_VALUES
                else "unknown"
            )
            self.recorder.increment_counter(
                "runtime_step_total",
                labels={
                    "execution_kind": execution_kind,
                    "output_policy": output_policy,
                    "status": status,
                },
            )
            if execution_kind == "SYNTHESIS":
                self.recorder.increment_counter(
                    "runtime_synthesis_total", labels={"status": status}
                )
            duration = self._duration_seconds(payload)
            if duration is not None:
                self.recorder.observe_histogram(
                    "runtime_step_duration_seconds",
                    duration,
                    labels={
                        "execution_kind": execution_kind,
                        "output_policy": output_policy,
                        "status": status,
                    },
                )
        elif event_type is RuntimeEventType.PLAN_CREATED:
            shape = payload.get("shape")
            if not isinstance(shape, str) or shape not in _SHAPE_VALUES:
                shape = "unknown"
            specialist_count = int(payload.get("step_count", 0) or 0)
            if shape == "2":
                specialist_count = 1
            elif shape == "3":
                specialist_count = max(0, specialist_count - 1)
            elif shape in {"0", "1"}:
                specialist_count = 0
            else:
                specialist_count = 0
            self.recorder.observe_histogram(
                "runtime_specialist_count",
                float(specialist_count),
                labels={"shape": shape},
            )
        elif event_type is RuntimeEventType.MODEL_STARTED:
            profile = str(payload.get("profile_id", "unknown"))
            self.recorder.increment_counter(
                "runtime_model_attempts_total",
                labels={"model_profile": profile},
            )
            retry = payload.get("retry_index")
            if isinstance(retry, int) and retry > 0:
                self.recorder.increment_counter(
                    "runtime_retries_total", labels={"component": "model"}
                )
        elif event_type is RuntimeEventType.MODEL_COMPLETED:
            profile = str(payload.get("profile_id", "unknown"))
            status = self._status(payload)
            duration = self._duration_seconds(payload)
            if duration is not None:
                self.recorder.observe_histogram(
                    "runtime_model_duration_seconds",
                    duration,
                    labels={"status": status, "model_profile": profile},
                )
            self._project_component_outcome("model", payload)
        elif event_type is RuntimeEventType.TOOL_STARTED:
            tool = str(payload.get("tool_name", "other"))
            self.recorder.increment_counter(
                "runtime_tool_attempts_total", labels={"tool_name": tool}
            )
            retry = payload.get("retry_index")
            if isinstance(retry, int) and retry > 0:
                self.recorder.increment_counter(
                    "runtime_retries_total", labels={"component": "tool"}
                )
        elif event_type is RuntimeEventType.TOOL_COMPLETED:
            tool = str(payload.get("tool_name", "other"))
            status = self._status(payload)
            duration = self._duration_seconds(payload)
            if duration is not None:
                self.recorder.observe_histogram(
                    "runtime_tool_duration_seconds",
                    duration,
                    labels={"status": status, "tool_name": tool},
                )
            self._project_component_outcome("tool", payload)
        elif event_type is RuntimeEventType.RETRIEVAL_STARTED:
            self.recorder.increment_counter("runtime_retrievals_total")
        elif event_type is RuntimeEventType.RETRIEVAL_STAGE_COMPLETED:
            duration = self._duration_seconds(payload)
            if duration is not None:
                self.recorder.observe_histogram(
                    "runtime_retrieval_stage_duration_seconds",
                    duration,
                    labels={
                        "status": self._status(payload),
                        "retrieval_stage": str(payload.get("stage", "unknown")),
                    },
                )
        elif event_type is RuntimeEventType.RETRIEVAL_COMPLETED:
            duration = self._duration_seconds(payload)
            if duration is not None:
                self.recorder.observe_histogram(
                    "runtime_retrieval_duration_seconds",
                    duration,
                    labels={"status": self._status(payload)},
                )
            self._project_component_outcome("retrieval", payload)
        elif event_type is RuntimeEventType.BUDGET_EXHAUSTED:
            if payload.get("component") in {"run", "run_coordinator"}:
                self.recorder.increment_counter(
                    "runtime_budget_exhaustions_total",
                    labels={
                        "component": "run",
                        "budget_dimension": str(
                            payload.get("dimension", "unknown")
                        ),
                        "status": "EXHAUSTED",
                    },
                )
        elif event_type is RuntimeEventType.TIMEOUT:
            if payload.get("component") in {"run", "run_coordinator"}:
                self.recorder.increment_counter(
                    "runtime_timeouts_total",
                    labels={"component": "run", "status": "TIMED_OUT"},
                )
        elif event_type is RuntimeEventType.CANCELLATION:
            if payload.get("component") in {"run", "run_coordinator"}:
                self.recorder.increment_counter(
                    "runtime_cancellations_total",
                    labels={
                        "component": "run",
                        "cancellation_reason": str(
                            payload.get("reason", "UNKNOWN")
                        ),
                        "status": "CANCELLED",
                    },
                )

    @property
    def correlation_state_size(self) -> int:
        """Completed 自带权威时长，因此不保留任何高基数 Started Map。"""
        return 0

    def clear_correlation_state(self) -> None:
        """兼容 Dispatcher close 的显式清理契约；当前实现无状态。"""
        return None


class RecorderInfrastructureMetricsHook(RuntimeInfrastructureMetricsHook):
    """直接记录基础设施事实；失败被吞掉且不生成 RuntimeEvent。"""

    def __init__(self, recorder: RuntimeMetricsRecorder) -> None:
        self.recorder = recorder

    def _counter(
        self, name: str, *, labels: Mapping[str, str] | None = None
    ) -> None:
        try:
            self.recorder.increment_counter(name, labels=labels)
        except Exception:
            return

    def journal_append_succeeded(
        self, *, duration_seconds: float, duplicate: bool
    ) -> None:
        try:
            self.recorder.observe_histogram(
                "runtime_journal_append_duration_seconds", duration_seconds
            )
        except Exception:
            pass
        if duplicate:
            self._counter(
                "runtime_event_duplicates_total",
                labels={"component": "journal"},
            )

    def journal_append_failed(self, *, duration_seconds: float) -> None:
        self._counter("runtime_journal_append_failures_total")
        try:
            self.recorder.observe_histogram(
                "runtime_journal_append_duration_seconds", duration_seconds
            )
        except Exception:
            return

    def observability_record_dropped(self) -> None:
        self._counter("runtime_observability_dropped_records_total")

    def event_duplicate_observed(self, *, component: str) -> None:
        self._counter(
            "runtime_event_duplicates_total",
            labels={"component": component},
        )

    def blocking_executor_wait_observed(
        self, *, duration_seconds: float
    ) -> None:
        try:
            self.recorder.observe_histogram(
                "runtime_blocking_executor_wait_seconds", duration_seconds
            )
        except Exception:
            return


class ApplicationRuntimeGaugeProvider(RuntimeGaugeProvider):
    """组合现有组件快照，不从 EventChannel 消费事件。"""

    def __init__(
        self,
        *,
        run_registry: RunRegistry,
        blocking_executor: BoundedBlockingExecutor,
        tool_workers: ToolConcurrencyController | None = None,
        circuit_registry: ModelCircuitBreakerRegistry | None = None,
    ) -> None:
        self.run_registry = run_registry
        self.blocking_executor = blocking_executor
        self.tool_workers = tool_workers
        self.circuit_registry = circuit_registry
        self._channels: weakref.WeakSet[RuntimeEventChannel] = weakref.WeakSet()

    def register_channel(self, channel: RuntimeEventChannel) -> None:
        self._channels.add(channel)

    def unregister_channel(self, channel: RuntimeEventChannel) -> None:
        self._channels.discard(channel)

    def snapshot(self) -> dict[str, float]:
        runs = self.run_registry.observability_snapshot()
        blocking = self.blocking_executor.snapshot()
        circuits = (
            self.circuit_registry.snapshot_all()
            if self.circuit_registry is not None
            else ()
        )
        retrieval_detached = sum(
            task.detached and task.kind in set(BlockingTaskKind)
            for task in blocking.tasks
        )
        return {
            "runtime_active_runs": float(runs["active_runs"]),
            "runtime_active_steps": float(runs["active_steps"]),
            "runtime_detached_tool_workers": float(
                self.tool_workers.detached_worker_count
                if self.tool_workers is not None
                else 0
            ),
            "runtime_detached_retrieval_workers": float(retrieval_detached),
            "runtime_blocking_executor_active": float(blocking.active_count),
            "runtime_blocking_executor_pending": float(blocking.pending_count),
            "runtime_event_channel_buffered": float(
                sum(channel.buffered_count for channel in tuple(self._channels))
            ),
            "runtime_circuit_breakers_open": float(
                sum(item.state is ModelCircuitState.OPEN for item in circuits)
            ),
        }


class RuntimeMetricsCollector:
    """读取时采集 Gauge；Recorder 不持有 Runtime 生命周期对象。"""

    def __init__(
        self,
        recorder: RuntimeMetricsRecorder,
        gauge_provider: RuntimeGaugeProvider,
    ) -> None:
        self.recorder = recorder
        self.gauge_provider = gauge_provider

    def collect_snapshot(self) -> RuntimeMetricsSnapshot:
        return self.recorder.snapshot(gauge_provider=self.gauge_provider)


def record_gauge_snapshot(
    recorder: RuntimeMetricsRecorder, provider: RuntimeGaugeProvider
) -> None:
    values = provider.snapshot()
    for name, value in values.items():
        recorder.set_gauge(name, value)
