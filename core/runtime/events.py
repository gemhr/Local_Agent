#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Runtime 内部使用的强类型事件信封与安全 Payload。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import ClassVar, TypeAlias
from uuid import uuid4


RUNTIME_EVENT_SCHEMA_VERSION = 1


class RuntimeEventType(str, Enum):
    """第一版 Runtime Event 的固定类型。"""

    RUN_STARTED = "RUN_STARTED"
    STEP_STARTED = "STEP_STARTED"
    MODEL_STARTED = "MODEL_STARTED"
    MODEL_COMPLETED = "MODEL_COMPLETED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    OUTPUT_DELTA = "OUTPUT_DELTA"
    STEP_COMPLETED = "STEP_COMPLETED"
    ERROR = "ERROR"
    CANCELLATION = "CANCELLATION"
    RUN_COMPLETED = "RUN_COMPLETED"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")


def _require_index(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须是非负整数")


@dataclass(frozen=True, slots=True)
class RunStartedPayload:
    status: str

    def __post_init__(self) -> None:
        _require_text(self.status, "status")


@dataclass(frozen=True, slots=True)
class StepStartedPayload:
    status: str

    def __post_init__(self) -> None:
        _require_text(self.status, "status")


@dataclass(frozen=True, slots=True)
class ModelStartedPayload:
    profile_id: str
    candidate_index: int
    retry_index: int
    routing_adjustment: str
    breaker_key: str

    def __post_init__(self) -> None:
        _require_text(self.profile_id, "profile_id")
        _require_index(self.candidate_index, "candidate_index")
        _require_index(self.retry_index, "retry_index")
        _require_text(self.routing_adjustment, "routing_adjustment")
        _require_text(self.breaker_key, "breaker_key")


@dataclass(frozen=True, slots=True)
class ModelCompletedPayload:
    profile_id: str
    candidate_index: int
    retry_index: int
    succeeded: bool
    safe_error_code: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.profile_id, "profile_id")
        _require_index(self.candidate_index, "candidate_index")
        _require_index(self.retry_index, "retry_index")
        if not isinstance(self.succeeded, bool):
            raise TypeError("succeeded 必须是 bool")
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")


@dataclass(frozen=True, slots=True)
class ToolStartedPayload:
    tool_name: str

    def __post_init__(self) -> None:
        _require_text(self.tool_name, "tool_name")


@dataclass(frozen=True, slots=True)
class ToolCompletedPayload:
    tool_name: str
    succeeded: bool
    safe_error_code: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.tool_name, "tool_name")
        if not isinstance(self.succeeded, bool):
            raise TypeError("succeeded 必须是 bool")
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")


@dataclass(frozen=True, slots=True)
class OutputDeltaPayload:
    """唯一允许承载最终用户可见正文的 Payload。"""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text 必须是字符串")


@dataclass(frozen=True, slots=True)
class StepCompletedPayload:
    status: str
    safe_error_code: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.status, "status")
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")


@dataclass(frozen=True, slots=True)
class ErrorPayload:
    safe_error_code: str
    safe_message: str
    component: str
    fatal: bool

    def __post_init__(self) -> None:
        _require_text(self.safe_error_code, "safe_error_code")
        _require_text(self.safe_message, "safe_message")
        _require_text(self.component, "component")
        if not isinstance(self.fatal, bool):
            raise TypeError("fatal 必须是 bool")


@dataclass(frozen=True, slots=True)
class CancellationPayload:
    reason: str
    component: str

    def __post_init__(self) -> None:
        _require_text(self.reason, "reason")
        _require_text(self.component, "component")


@dataclass(frozen=True, slots=True)
class RunCompletedPayload:
    status: str
    stop_reason: str

    def __post_init__(self) -> None:
        _require_text(self.status, "status")
        _require_text(self.stop_reason, "stop_reason")


RuntimeEventPayload: TypeAlias = (
    RunStartedPayload
    | StepStartedPayload
    | ModelStartedPayload
    | ModelCompletedPayload
    | ToolStartedPayload
    | ToolCompletedPayload
    | OutputDeltaPayload
    | StepCompletedPayload
    | ErrorPayload
    | CancellationPayload
    | RunCompletedPayload
)


_PAYLOAD_TYPES: dict[RuntimeEventType, type[RuntimeEventPayload]] = {
    RuntimeEventType.RUN_STARTED: RunStartedPayload,
    RuntimeEventType.STEP_STARTED: StepStartedPayload,
    RuntimeEventType.MODEL_STARTED: ModelStartedPayload,
    RuntimeEventType.MODEL_COMPLETED: ModelCompletedPayload,
    RuntimeEventType.TOOL_STARTED: ToolStartedPayload,
    RuntimeEventType.TOOL_COMPLETED: ToolCompletedPayload,
    RuntimeEventType.OUTPUT_DELTA: OutputDeltaPayload,
    RuntimeEventType.STEP_COMPLETED: StepCompletedPayload,
    RuntimeEventType.ERROR: ErrorPayload,
    RuntimeEventType.CANCELLATION: CancellationPayload,
    RuntimeEventType.RUN_COMPLETED: RunCompletedPayload,
}


@dataclass(frozen=True, slots=True)
class RuntimeEventDraft:
    """尚未分配全局序号的内部事件事实。"""

    run_id: str
    trace_id: str
    event_type: RuntimeEventType
    component: str
    payload: RuntimeEventPayload
    step_id: str | None = None
    step_sequence: int | None = None

    def __post_init__(self) -> None:
        _validate_common(
            self.run_id,
            self.trace_id,
            self.event_type,
            self.component,
            self.payload,
            self.step_id,
            self.step_sequence,
        )


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """已由单 Run Channel 排序的不可变事件信封。"""

    schema_version: int
    event_id: str
    run_id: str
    trace_id: str
    sequence: int
    event_type: RuntimeEventType
    emitted_at: datetime
    component: str
    payload: RuntimeEventPayload
    step_id: str | None = None
    step_sequence: int | None = None

    CURRENT_SCHEMA_VERSION: ClassVar[int] = RUNTIME_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != self.CURRENT_SCHEMA_VERSION:
            raise ValueError("不支持的 Runtime Event schema_version")
        _require_text(self.event_id, "event_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence <= 0:
            raise ValueError("sequence 必须是正整数")
        if self.emitted_at.tzinfo is None or self.emitted_at.utcoffset() is None:
            raise ValueError("emitted_at 必须是 UTC 时间")
        if self.emitted_at.utcoffset().total_seconds() != 0:
            raise ValueError("emitted_at 必须是 UTC 时间")
        _validate_common(
            self.run_id,
            self.trace_id,
            self.event_type,
            self.component,
            self.payload,
            self.step_id,
            self.step_sequence,
        )

    @classmethod
    def from_draft(cls, draft: RuntimeEventDraft, sequence: int) -> "RuntimeEvent":
        """仅供单 Run sequence owner 在入队时创建信封。"""
        return cls(
            schema_version=RUNTIME_EVENT_SCHEMA_VERSION,
            event_id=uuid4().hex,
            run_id=draft.run_id,
            trace_id=draft.trace_id,
            sequence=sequence,
            event_type=draft.event_type,
            emitted_at=datetime.now(UTC),
            component=draft.component,
            payload=draft.payload,
            step_id=draft.step_id,
            step_sequence=draft.step_sequence,
        )

    def to_safe_dict(self, *, include_output: bool = False) -> dict[str, object]:
        """返回日志、诊断和 Transport 可显式选择的安全字典。"""
        if isinstance(self.payload, OutputDeltaPayload) and not include_output:
            return {
                "run_id": self.run_id,
                "sequence": self.sequence,
                "event_type": self.event_type.value,
                "step_id": self.step_id,
                "payload": {"text_length": len(self.payload.text)},
            }
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "emitted_at": self.emitted_at.isoformat(),
            "step_id": self.step_id,
            "step_sequence": self.step_sequence,
            "component": self.component,
        }
        if isinstance(self.payload, OutputDeltaPayload):
            result["payload"] = {
                "text_length": len(self.payload.text),
                "text": self.payload.text,
            }
        else:
            result["payload"] = asdict(self.payload)
        return result


def _validate_common(
    run_id: str,
    trace_id: str,
    event_type: RuntimeEventType,
    component: str,
    payload: RuntimeEventPayload,
    step_id: str | None,
    step_sequence: int | None,
) -> None:
    _require_text(run_id, "run_id")
    _require_text(trace_id, "trace_id")
    _require_text(component, "component")
    if not isinstance(event_type, RuntimeEventType):
        raise TypeError("event_type 必须是 RuntimeEventType")
    expected = _PAYLOAD_TYPES[event_type]
    if not isinstance(payload, expected):
        raise TypeError(
            f"{event_type.value} 必须使用 {expected.__name__}，"
            f"不能使用 {type(payload).__name__}"
        )
    if step_id is None:
        if step_sequence is not None:
            raise ValueError("没有 step_id 时不能设置 step_sequence")
    else:
        _require_text(step_id, "step_id")
        if (
            isinstance(step_sequence, bool)
            or not isinstance(step_sequence, int)
            or step_sequence <= 0
        ):
            raise ValueError("Step Event 必须使用正整数 step_sequence")
