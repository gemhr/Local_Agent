#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool Execution Contract 的不可变数据模型与安全序列化。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from core.runtime.retry import OperationIdempotency


class ToolSideEffectKind(str, Enum):
    NONE = "NONE"
    LOCAL_STATE_MUTATION = "LOCAL_STATE_MUTATION"
    EXTERNAL_STATE_MUTATION = "EXTERNAL_STATE_MUTATION"
    IRREVERSIBLE = "IRREVERSIBLE"
    UNKNOWN = "UNKNOWN"


class ToolSideEffectState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    STARTED = "STARTED"
    COMMITTED = "COMMITTED"
    COMPENSATED = "COMPENSATED"
    UNKNOWN = "UNKNOWN"


class RetryDisposition(str, Enum):
    SAFE = "SAFE"
    SAFE_WITH_IDEMPOTENCY_KEY = "SAFE_WITH_IDEMPOTENCY_KEY"
    UNSAFE = "UNSAFE"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class ToolExecutionStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_SUCCEEDED = "PARTIALLY_SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class ToolErrorCategory(str, Enum):
    VALIDATION = "VALIDATION"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"
    TRANSIENT = "TRANSIENT"
    POST_COMMIT_RESPONSE_FAILURE = "POST_COMMIT_RESPONSE_FAILURE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    SIDE_EFFECT_UNKNOWN = "SIDE_EFFECT_UNKNOWN"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    INTERNAL = "INTERNAL"


class ToolExecutionPhase(str, Enum):
    VALIDATION = "VALIDATION"
    RESOURCE_WAIT = "RESOURCE_WAIT"
    BUDGET = "BUDGET"
    EVENT = "EVENT"
    INVOCATION = "INVOCATION"
    OUTPUT = "OUTPUT"
    COMPENSATION = "COMPENSATION"
    COMPLETION = "COMPLETION"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")


def _require_positive_number(value: int | float, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field_name} 必须是有限正数")


def _require_positive_integer(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} 必须是正整数")


def _freeze_json(value: Any, path: str = "arguments") -> Any:
    """校验 JSON-safe 值并递归冻结，拒绝 NaN、bytes 和任意对象。"""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} 不允许非有限浮点数")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} 的对象 Key 必须是字符串")
            frozen[key] = _freeze_json(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{path}[]") for item in value)
    raise ValueError(f"{path} 只允许 JSON-safe 值")


def thaw_json(value: Any) -> Any:
    """把冻结的 JSON-safe 值还原为普通 dict/list，供 Adapter 显式消费。"""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(
        thaw_json(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def safe_key_digest(value: str | None) -> str | None:
    if value is None:
        return None
    _require_text(value, "key")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    invocation_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    arguments_digest: str = field(init=False)
    idempotency_key: str | None = None
    resource_key: str | None = None
    requested_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        _require_text(self.invocation_id, "invocation_id")
        _require_text(self.tool_name, "tool_name")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("arguments 必须是 JSON object")
        frozen = _freeze_json(self.arguments)
        object.__setattr__(self, "arguments", frozen)
        object.__setattr__(self, "arguments_digest", canonical_json_digest(frozen))
        if self.idempotency_key is not None:
            _require_text(self.idempotency_key, "idempotency_key")
        if self.resource_key is not None:
            _require_text(self.resource_key, "resource_key")
        if self.requested_timeout_seconds is not None:
            _require_positive_number(
                self.requested_timeout_seconds, "requested_timeout_seconds"
            )

    @classmethod
    def create(
        cls,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        idempotency_key: str | None = None,
        resource_key: str | None = None,
        requested_timeout_seconds: float | None = None,
        invocation_id: str | None = None,
    ) -> "ToolInvocation":
        return cls(
            invocation_id=invocation_id or uuid4().hex,
            tool_name=tool_name,
            arguments=arguments,
            idempotency_key=idempotency_key,
            resource_key=resource_key,
            requested_timeout_seconds=requested_timeout_seconds,
        )

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "invocation_id": self.invocation_id,
            "tool_name": self.tool_name,
            "arguments_digest": self.arguments_digest,
            "idempotency_key_digest": safe_key_digest(self.idempotency_key),
            "resource_key_digest": safe_key_digest(self.resource_key),
            "requested_timeout_seconds": self.requested_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ToolExecutionSpec:
    tool_name: str
    side_effect_kind: ToolSideEffectKind = ToolSideEffectKind.UNKNOWN
    idempotency: OperationIdempotency = OperationIdempotency.UNKNOWN
    requires_resource_key: bool = False
    supports_cooperative_cancellation: bool = False
    supports_side_effect_checkpoint: bool = False
    default_timeout_seconds: float = 30.0
    max_output_bytes: int = 16_384
    max_concurrency: int = 1
    supports_idempotency_replay: bool = False

    def __post_init__(self) -> None:
        _require_text(self.tool_name, "tool_name")
        if not isinstance(self.side_effect_kind, ToolSideEffectKind):
            raise TypeError("side_effect_kind 必须是 ToolSideEffectKind")
        if not isinstance(self.idempotency, OperationIdempotency):
            raise TypeError("idempotency 必须是 OperationIdempotency")
        for field_name in (
            "requires_resource_key",
            "supports_cooperative_cancellation",
            "supports_side_effect_checkpoint",
            "supports_idempotency_replay",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} 必须是 bool")
        _require_positive_number(self.default_timeout_seconds, "default_timeout_seconds")
        _require_positive_integer(self.max_output_bytes, "max_output_bytes")
        _require_positive_integer(self.max_concurrency, "max_concurrency")


@dataclass(frozen=True, slots=True)
class ToolOutput:
    content_type: str
    content: str
    original_size_bytes: int
    returned_size_bytes: int
    truncated: bool
    digest: str

    def __post_init__(self) -> None:
        _require_text(self.content_type, "content_type")
        if not isinstance(self.content, str):
            raise TypeError("content 必须是字符串")
        for name in ("original_size_bytes", "returned_size_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if not isinstance(self.truncated, bool):
            raise TypeError("truncated 必须是 bool")
        _require_text(self.digest, "digest")

    def to_safe_dict(self, *, include_content: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "content_type": self.content_type,
            "original_size_bytes": self.original_size_bytes,
            "returned_size_bytes": self.returned_size_bytes,
            "truncated": self.truncated,
            "digest": self.digest,
        }
        if include_content:
            result["content"] = self.content
        return result


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    invocation_id: str
    attempt_id: str
    tool_name: str
    status: ToolExecutionStatus
    output: ToolOutput
    safe_summary: str
    side_effect_state: ToolSideEffectState
    idempotency_replayed: bool
    retry_disposition: RetryDisposition
    resource_key_digest: str | None
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    retry_index: int = 0
    worker_terminated: bool = True
    execution_detached: bool = False
    resource_release_pending: bool = False

    def __post_init__(self) -> None:
        _require_text(self.invocation_id, "invocation_id")
        _require_text(self.attempt_id, "attempt_id")
        _require_text(self.tool_name, "tool_name")
        _require_text(self.safe_summary, "safe_summary")
        if not isinstance(self.status, ToolExecutionStatus):
            raise TypeError("status 必须是 ToolExecutionStatus")
        if not isinstance(self.output, ToolOutput):
            raise TypeError("output 必须是 ToolOutput")
        if not isinstance(self.side_effect_state, ToolSideEffectState):
            raise TypeError("side_effect_state 必须是 ToolSideEffectState")
        if not isinstance(self.idempotency_replayed, bool):
            raise TypeError("idempotency_replayed 必须是 bool")
        if not isinstance(self.retry_disposition, RetryDisposition):
            raise TypeError("retry_disposition 必须是 RetryDisposition")
        _validate_worker_lifecycle(
            self.worker_terminated,
            self.execution_detached,
            self.resource_release_pending,
        )
        _validate_timing(self.started_at, self.completed_at, self.duration_ms)

    def to_safe_dict(self, *, include_output: bool = False) -> dict[str, object]:
        return {
            "invocation_id": self.invocation_id,
            "attempt_id": self.attempt_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "output": self.output.to_safe_dict(include_content=include_output),
            "safe_summary": self.safe_summary,
            "side_effect_state": self.side_effect_state.value,
            "idempotency_replayed": self.idempotency_replayed,
            "retry_disposition": self.retry_disposition.value,
            "resource_key_digest": self.resource_key_digest,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_ms": self.duration_ms,
            "retry_index": self.retry_index,
            "worker_terminated": self.worker_terminated,
            "execution_detached": self.execution_detached,
            "resource_release_pending": self.resource_release_pending,
        }


@dataclass(frozen=True, slots=True)
class ToolExecutionError:
    invocation_id: str
    attempt_id: str | None
    tool_name: str
    category: ToolErrorCategory
    safe_error_code: str
    safe_message: str
    phase: ToolExecutionPhase
    provider_started: bool
    side_effect_state: ToolSideEffectState
    retry_disposition: RetryDisposition
    partial_result: Mapping[str, Any] | None = None
    compensation_attempted: bool = False
    compensation_succeeded: bool = False
    retry_index: int = 0
    status: ToolExecutionStatus = ToolExecutionStatus.FAILED
    output_started: bool = False
    worker_terminated: bool = True
    execution_detached: bool = False
    resource_release_pending: bool = False

    def __post_init__(self) -> None:
        _require_text(self.invocation_id, "invocation_id")
        if self.attempt_id is not None:
            _require_text(self.attempt_id, "attempt_id")
        _require_text(self.tool_name, "tool_name")
        _require_text(self.safe_error_code, "safe_error_code")
        _require_text(self.safe_message, "safe_message")
        if not isinstance(self.category, ToolErrorCategory):
            raise TypeError("category 必须是 ToolErrorCategory")
        if not isinstance(self.phase, ToolExecutionPhase):
            raise TypeError("phase 必须是 ToolExecutionPhase")
        if not isinstance(self.provider_started, bool):
            raise TypeError("provider_started 必须是 bool")
        if not isinstance(self.side_effect_state, ToolSideEffectState):
            raise TypeError("side_effect_state 必须是 ToolSideEffectState")
        if not isinstance(self.retry_disposition, RetryDisposition):
            raise TypeError("retry_disposition 必须是 RetryDisposition")
        if not isinstance(self.status, ToolExecutionStatus):
            raise TypeError("status 必须是 ToolExecutionStatus")
        if not isinstance(self.output_started, bool):
            raise TypeError("output_started 必须是 bool")
        _validate_worker_lifecycle(
            self.worker_terminated,
            self.execution_detached,
            self.resource_release_pending,
        )
        if self.partial_result is not None:
            object.__setattr__(
                self, "partial_result", _freeze_json(self.partial_result, "partial_result")
            )

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "invocation_id": self.invocation_id,
            "attempt_id": self.attempt_id,
            "tool_name": self.tool_name,
            "category": self.category.value,
            "safe_error_code": self.safe_error_code,
            "safe_message": self.safe_message,
            "phase": self.phase.value,
            "provider_started": self.provider_started,
            "side_effect_state": self.side_effect_state.value,
            "retry_disposition": self.retry_disposition.value,
            "partial_result": thaw_json(self.partial_result),
            "compensation_attempted": self.compensation_attempted,
            "compensation_succeeded": self.compensation_succeeded,
            "retry_index": self.retry_index,
            "status": self.status.value,
            "output_started": self.output_started,
            "worker_terminated": self.worker_terminated,
            "execution_detached": self.execution_detached,
            "resource_release_pending": self.resource_release_pending,
        }


class ToolOutputValidationError(RuntimeError):
    """结构化输出超限或类型不受支持；只携带安全元数据。"""

    def __init__(
        self,
        *,
        category: ToolErrorCategory,
        safe_error_code: str,
        safe_message: str,
        content_type: str,
        original_size_bytes: int,
        digest: str,
    ) -> None:
        self.category = category
        self.safe_error_code = safe_error_code
        self.safe_message = safe_message
        self.safe_metadata = {
            "content_type": content_type,
            "original_size_bytes": original_size_bytes,
            "digest": digest,
        }
        super().__init__(safe_message)


def build_tool_output(content: str, content_type: str, max_output_bytes: int) -> ToolOutput:
    """按 Content Type 执行安全策略；JSON 永不按任意字节截断。"""
    if not isinstance(content, str):
        raise TypeError("Tool Output content 必须是字符串")
    _require_positive_integer(max_output_bytes, "max_output_bytes")
    _require_text(content_type, "content_type")
    encoded = content.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type == "application/json" or normalized_type.endswith("+json"):
        try:
            json.loads(content)
        except (TypeError, ValueError):
            raise ToolOutputValidationError(
                category=ToolErrorCategory.OUTPUT_INVALID,
                safe_error_code="TOOL_OUTPUT_INVALID_JSON",
                safe_message="Tool 返回了无效 JSON。",
                content_type=content_type,
                original_size_bytes=len(encoded),
                digest=digest,
            ) from None
        if len(encoded) > max_output_bytes:
            raise ToolOutputValidationError(
                category=ToolErrorCategory.OUTPUT_TOO_LARGE,
                safe_error_code="TOOL_OUTPUT_TOO_LARGE",
                safe_message="结构化 Tool Output 超过允许大小。",
                content_type=content_type,
                original_size_bytes=len(encoded),
                digest=digest,
            )
        return ToolOutput(
            content_type=content_type,
            content=content,
            original_size_bytes=len(encoded),
            returned_size_bytes=len(encoded),
            truncated=False,
            digest=digest,
        )
    if normalized_type != "text/plain":
        raise ToolOutputValidationError(
            category=ToolErrorCategory.OUTPUT_INVALID,
            safe_error_code="TOOL_OUTPUT_CONTENT_TYPE_UNSUPPORTED",
            safe_message="该 Tool Output 类型不能直接进入模型 Context。",
            content_type=content_type,
            original_size_bytes=len(encoded),
            digest=digest,
        )
    returned = encoded[:max_output_bytes]
    while returned:
        try:
            safe_content = returned.decode("utf-8")
            break
        except UnicodeDecodeError:
            returned = returned[:-1]
    else:
        safe_content = ""
    return ToolOutput(
        content_type=content_type,
        content=safe_content,
        original_size_bytes=len(encoded),
        returned_size_bytes=len(returned),
        truncated=len(returned) < len(encoded),
        digest=digest,
    )


def retry_disposition_for(
    *,
    category: ToolErrorCategory,
    idempotency: OperationIdempotency,
    idempotency_key: str | None,
    side_effect_state: ToolSideEffectState,
    compensation_attempted: bool = False,
    compensation_succeeded: bool = False,
    supports_idempotency_replay: bool = False,
    arguments_digest_matches: bool = True,
    output_started: bool = False,
) -> RetryDisposition:
    """联合错误、幂等、副作用与补偿事实给出保守 Retry 判定。"""
    if category not in {
        ToolErrorCategory.TRANSIENT,
        ToolErrorCategory.POST_COMMIT_RESPONSE_FAILURE,
        ToolErrorCategory.TIMEOUT,
        ToolErrorCategory.RESOURCE_CONFLICT,
    }:
        return RetryDisposition.UNSAFE
    if side_effect_state == ToolSideEffectState.UNKNOWN:
        return RetryDisposition.OUTCOME_UNKNOWN
    if compensation_attempted and not compensation_succeeded:
        return RetryDisposition.UNSAFE
    if output_started or not arguments_digest_matches:
        return RetryDisposition.UNSAFE
    if side_effect_state == ToolSideEffectState.COMMITTED:
        if (
            idempotency == OperationIdempotency.IDEMPOTENT_WITH_KEY
            and bool(idempotency_key and idempotency_key.strip())
            and supports_idempotency_replay
            and category == ToolErrorCategory.POST_COMMIT_RESPONSE_FAILURE
        ):
            return RetryDisposition.SAFE_WITH_IDEMPOTENCY_KEY
        return RetryDisposition.UNSAFE
    if side_effect_state == ToolSideEffectState.COMPENSATED and not compensation_succeeded:
        return RetryDisposition.UNSAFE
    if idempotency == OperationIdempotency.IDEMPOTENT_WITH_KEY:
        if not idempotency_key or not idempotency_key.strip():
            return RetryDisposition.UNSAFE
        return RetryDisposition.SAFE_WITH_IDEMPOTENCY_KEY
    if idempotency in {
        OperationIdempotency.READ_ONLY,
        OperationIdempotency.IDEMPOTENT,
    }:
        return RetryDisposition.SAFE
    return RetryDisposition.UNSAFE


def _validate_timing(started_at: datetime, completed_at: datetime, duration_ms: int) -> None:
    for value, name in ((started_at, "started_at"), (completed_at, "completed_at")):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(f"{name} 必须是带时区时间")
        if value.astimezone(UTC).utcoffset().total_seconds() != 0:
            raise ValueError(f"{name} 必须可转换为 UTC")
    if completed_at < started_at:
        raise ValueError("completed_at 不得早于 started_at")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise ValueError("duration_ms 必须是非负整数")


def _validate_worker_lifecycle(
    worker_terminated: bool,
    execution_detached: bool,
    resource_release_pending: bool,
) -> None:
    for value, name in (
        (worker_terminated, "worker_terminated"),
        (execution_detached, "execution_detached"),
        (resource_release_pending, "resource_release_pending"),
    ):
        if not isinstance(value, bool):
            raise TypeError(f"{name} 必须是 bool")
    if execution_detached and worker_terminated:
        raise ValueError("Detached Worker 不能标记为已终止")
    if resource_release_pending and worker_terminated:
        raise ValueError("已终止 Worker 不能仍等待资源释放")
    if execution_detached and not resource_release_pending:
        raise ValueError("Detached Worker 必须保留资源许可等待清理")


__all__ = [
    "RetryDisposition",
    "ToolErrorCategory",
    "ToolExecutionError",
    "ToolExecutionPhase",
    "ToolExecutionResult",
    "ToolExecutionSpec",
    "ToolExecutionStatus",
    "ToolInvocation",
    "ToolOutput",
    "ToolOutputValidationError",
    "ToolSideEffectKind",
    "ToolSideEffectState",
    "build_tool_output",
    "canonical_json_digest",
    "retry_disposition_for",
    "safe_key_digest",
    "thaw_json",
]
