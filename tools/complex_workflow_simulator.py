#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用于演练复杂执行语义的确定性纯本地工具。

本模块有意不依赖运行时工具、重试、预算或取消契约。它是一个贴近生产形态的
模拟对象，后续可以再适配到这些契约。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Protocol


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_PARALLEL_ITEMS = 16
_MAX_PROCESSING_DELAY_MS = 5_000
_SAFE_MESSAGES = {
    "TOOL_VALIDATION_ERROR": "The simulated request is invalid.",
    "TOOL_RESOURCE_CONFLICT": "The simulated resource is already in use.",
    "TOOL_TRANSIENT_FAILURE": "A transient simulated failure occurred before commit.",
    "TOOL_TIMEOUT": "The simulated operation timed out before commit.",
    "TOOL_CANCELLED": "The simulated operation was cancelled.",
    "TOOL_IDEMPOTENCY_CONFLICT": "The idempotency key was used with a different request.",
    "TOOL_PARTIAL_FAILURE": "One or more simulated items failed.",
    "TOOL_SIDE_EFFECT_FAILURE": "The response failed after the simulated side effect committed.",
    "TOOL_COMPENSATION_FAILURE": "The simulated compensation attempt failed.",
    "TOOL_UNKNOWN_FAILURE": "An unknown simulated failure occurred.",
}


class WorkflowExecutionMode(str, Enum):
    DRY_RUN = "DRY_RUN"
    IDEMPOTENT_COMMIT = "IDEMPOTENT_COMMIT"
    NON_IDEMPOTENT_SIMULATION = "NON_IDEMPOTENT_SIMULATION"


class WorkflowFailureInjection(str, Enum):
    NONE = "NONE"
    TRANSIENT_BEFORE_SIDE_EFFECT = "TRANSIENT_BEFORE_SIDE_EFFECT"
    TIMEOUT_BEFORE_SIDE_EFFECT = "TIMEOUT_BEFORE_SIDE_EFFECT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"
    PARTIAL_ITEM_FAILURE = "PARTIAL_ITEM_FAILURE"
    FAIL_AFTER_SIDE_EFFECT = "FAIL_AFTER_SIDE_EFFECT"
    COMPENSATION_FAILURE = "COMPENSATION_FAILURE"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class WorkflowStage(str, Enum):
    VALIDATE_REQUEST = "VALIDATE_REQUEST"
    LOAD_EXISTING_STATE = "LOAD_EXISTING_STATE"
    ACQUIRE_RESOURCE = "ACQUIRE_RESOURCE"
    CREATE_SNAPSHOT = "CREATE_SNAPSHOT"
    PREPARE_ITEMS = "PREPARE_ITEMS"
    PROCESS_ITEMS = "PROCESS_ITEMS"
    VALIDATE_PROCESSED_ITEMS = "VALIDATE_PROCESSED_ITEMS"
    COMMIT_SIDE_EFFECTS = "COMMIT_SIDE_EFFECTS"
    CREATE_AUDIT_RECORD = "CREATE_AUDIT_RECORD"
    FINALIZE = "FINALIZE"
    COMPENSATE_COMMITTED_CHANGES = "COMPENSATE_COMMITTED_CHANGES"
    RELEASE_RESOURCE = "RELEASE_RESOURCE"
    FINALIZE_FAILURE = "FINALIZE_FAILURE"
    # 兼容学习计划中使用的粗粒度阶段词汇；序列化时仍保留上方更精确的值。
    VALIDATING = "VALIDATE_REQUEST"
    LOADING_STATE = "LOAD_EXISTING_STATE"
    ACQUIRING_RESOURCE = "ACQUIRE_RESOURCE"
    CREATING_SNAPSHOT = "CREATE_SNAPSHOT"
    PROCESSING_ITEMS = "PROCESS_ITEMS"
    VALIDATING_ITEMS = "VALIDATE_PROCESSED_ITEMS"
    COMMITTING = "COMMIT_SIDE_EFFECTS"
    AUDITING = "CREATE_AUDIT_RECORD"
    COMPENSATING = "COMPENSATE_COMMITTED_CHANGES"
    COMPLETED = "FINALIZE"
    FAILED = "FINALIZE_FAILURE"


class WorkflowStageStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class WorkflowResultStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_SUCCEEDED = "PARTIALLY_SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    IDEMPOTENCY_REPLAY = "IDEMPOTENCY_REPLAY"


class WorkflowItemStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


@dataclass(frozen=True)
class WorkflowItem:
    item_id: str
    action: str
    quantity: int
    priority: int = 0
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_identifier(self.item_id, "item_id")
        _require_text(self.action, "action")
        _require_integer(self.quantity, "quantity", minimum=1)
        _require_integer(self.priority, "priority", minimum=0)
        if not isinstance(self.attributes, Mapping):
            raise ValueError("attributes must be a mapping")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkflowItem":
        if not isinstance(value, Mapping):
            raise ValueError("each item must be an object")
        return cls(
            item_id=value.get("item_id"),
            action=value.get("action"),
            quantity=value.get("quantity"),
            priority=value.get("priority", 0),
            attributes=value.get("attributes", {}),
        )


@dataclass(frozen=True)
class WorkflowProcessingOptions:
    max_parallel_items: int = 1
    processing_delay_ms: int = 0
    allow_partial_success: bool = False
    enable_compensation: bool = True

    def __post_init__(self) -> None:
        _require_integer(
            self.max_parallel_items,
            "max_parallel_items",
            minimum=1,
            maximum=_MAX_PARALLEL_ITEMS,
        )
        _require_integer(
            self.processing_delay_ms,
            "processing_delay_ms",
            minimum=0,
            maximum=_MAX_PROCESSING_DELAY_MS,
        )
        if not isinstance(self.allow_partial_success, bool):
            raise ValueError("allow_partial_success must be a bool")
        if not isinstance(self.enable_compensation, bool):
            raise ValueError("enable_compensation must be a bool")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "WorkflowProcessingOptions":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("processing_options must be an object")
        return cls(
            max_parallel_items=value.get("max_parallel_items", 1),
            processing_delay_ms=value.get("processing_delay_ms", 0),
            allow_partial_success=value.get("allow_partial_success", False),
            enable_compensation=value.get("enable_compensation", True),
        )


@dataclass(frozen=True)
class ComplexWorkflowRequest:
    operation_id: str
    resource_key: str
    idempotency_key: str | None
    execution_mode: WorkflowExecutionMode
    items: tuple[WorkflowItem, ...]
    failure_injection: WorkflowFailureInjection = WorkflowFailureInjection.NONE
    failure_stage: WorkflowStage | None = None
    failure_item_id: str | None = None
    processing_options: WorkflowProcessingOptions = field(default_factory=WorkflowProcessingOptions)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_identifier(self.operation_id, "operation_id")
        _require_identifier(self.resource_key, "resource_key")
        if self.idempotency_key is not None:
            _require_identifier(self.idempotency_key, "idempotency_key")
        if not isinstance(self.execution_mode, WorkflowExecutionMode):
            raise ValueError("execution_mode must be a WorkflowExecutionMode")
        if not isinstance(self.items, tuple) or not self.items:
            raise ValueError("items must be a non-empty tuple")
        if any(not isinstance(item, WorkflowItem) for item in self.items):
            raise ValueError("items must contain WorkflowItem values")
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("item_id values must be unique")
        if not isinstance(self.failure_injection, WorkflowFailureInjection):
            raise ValueError("failure_injection must be a WorkflowFailureInjection")
        if self.failure_stage is not None and not isinstance(self.failure_stage, WorkflowStage):
            raise ValueError("failure_stage must be a WorkflowStage")
        if self.failure_item_id is not None and self.failure_item_id not in set(item_ids):
            raise ValueError("failure_item_id must identify an item in this request")
        if not isinstance(self.processing_options, WorkflowProcessingOptions):
            raise ValueError("processing_options must be WorkflowProcessingOptions")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        if (
            self.execution_mode == WorkflowExecutionMode.IDEMPOTENT_COMMIT
            and self.idempotency_key is None
        ):
            raise ValueError("idempotency_key is required for IDEMPOTENT_COMMIT")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ComplexWorkflowRequest":
        if not isinstance(value, Mapping):
            raise ValueError("request must be a JSON object")
        try:
            execution_mode = WorkflowExecutionMode(value.get("execution_mode"))
            raw_failure = value.get(
                "failure_injection", WorkflowFailureInjection.NONE.value
            )
            if isinstance(raw_failure, Mapping):
                failure_value = raw_failure.get(
                    "type",
                    raw_failure.get(
                        "kind",
                        raw_failure.get(
                            "mode", WorkflowFailureInjection.NONE.value
                        ),
                    ),
                )
                raw_stage = value.get(
                    "failure_stage", raw_failure.get("failure_stage")
                )
                failure_item_id = value.get(
                    "failure_item_id", raw_failure.get("failure_item_id")
                )
            else:
                failure_value = raw_failure
                raw_stage = value.get("failure_stage")
                failure_item_id = value.get("failure_item_id")
            failure_injection = WorkflowFailureInjection(failure_value)
            failure_stage = _workflow_stage(raw_stage) if raw_stage is not None else None
        except (TypeError, ValueError) as exc:
            raise ValueError("request contains an unsupported enum value") from exc
        raw_items = value.get("items")
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            raise ValueError("items must be an array")
        return cls(
            operation_id=value.get("operation_id"),
            resource_key=value.get("resource_key"),
            idempotency_key=value.get("idempotency_key"),
            execution_mode=execution_mode,
            items=tuple(WorkflowItem.from_dict(item) for item in raw_items),
            failure_injection=failure_injection,
            failure_stage=failure_stage,
            failure_item_id=failure_item_id,
            processing_options=WorkflowProcessingOptions.from_dict(
                value.get("processing_options")
            ),
            metadata=value.get("metadata", {}),
        )


@dataclass(frozen=True)
class WorkflowItemResult:
    item_id: str
    status: WorkflowItemStatus
    safe_code: str
    attempted: bool
    side_effect_committed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "status": self.status.value,
            "safe_code": self.safe_code,
            "attempted": self.attempted,
            "side_effect_committed": self.side_effect_committed,
        }


@dataclass(frozen=True)
class WorkflowStageRecord:
    stage: WorkflowStage
    started_at: str
    completed_at: str
    status: WorkflowStageStatus
    safe_code: str
    processed_item_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status.value,
            "safe_code": self.safe_code,
            "processed_item_count": self.processed_item_count,
        }


@dataclass(frozen=True)
class ComplexWorkflowResult:
    operation_id: str
    resource_key: str
    idempotency_key: str | None
    execution_mode: WorkflowExecutionMode
    status: WorkflowResultStatus
    completed_stages: tuple[WorkflowStageRecord, ...]
    item_results: tuple[WorkflowItemResult, ...]
    side_effect_committed: bool
    compensation_attempted: bool
    compensation_succeeded: bool
    idempotency_replayed: bool
    audit_digest: str | None
    safe_error_code: str | None
    safe_message: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "resource_key": self.resource_key,
            "idempotency_key": self.idempotency_key,
            "execution_mode": self.execution_mode.value,
            "status": self.status.value,
            "completed_stages": [record.to_dict() for record in self.completed_stages],
            "item_results": [item.to_dict() for item in self.item_results],
            "side_effect_committed": self.side_effect_committed,
            "compensation_attempted": self.compensation_attempted,
            "compensation_succeeded": self.compensation_succeeded,
            "idempotency_replayed": self.idempotency_replayed,
            "audit_digest": self.audit_digest,
            "safe_error_code": self.safe_error_code,
            "safe_message": self.safe_message,
            "duration_ms": self.duration_ms,
        }


class WorkflowSimulationError(RuntimeError):
    """经过净化的内部错误，不保存异常对象或任意输入内容。"""

    def __init__(
        self,
        safe_error_code: str,
        stage: WorkflowStage,
        operation_id: str,
        *,
        side_effect_committed: bool = False,
        compensation_attempted: bool = False,
        compensation_succeeded: bool = False,
    ) -> None:
        self.safe_error_code = safe_error_code
        self.safe_message = _SAFE_MESSAGES[safe_error_code]
        self.stage = stage
        self.operation_id = operation_id
        self.side_effect_committed = side_effect_committed
        self.compensation_attempted = compensation_attempted
        self.compensation_succeeded = compensation_succeeded
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class IdempotencyRecord:
    request_digest: str
    result: ComplexWorkflowResult


class WorkflowStateStore(Protocol):
    def get_resource_state(self, resource_key: str) -> int:
        """返回当前模拟资源版本。"""

    def get_idempotency_record(self, key: str) -> IdempotencyRecord | None:
        """返回不可变的幂等记录；不存在时返回空值。"""

    def commit(
        self,
        *,
        request: ComplexWorkflowRequest,
        successful_item_ids: tuple[str, ...],
        request_digest: str,
    ) -> int:
        """提交一条本地模拟记录并返回其序号。"""

    def save_idempotency_result(
        self, key: str, request_digest: str, result: ComplexWorkflowResult
    ) -> None:
        """保存最终可回放结果。"""

    def add_audit_record(self, record: Mapping[str, Any]) -> None:
        """追加一条安全审计记录。"""

    def compensate(self, operation_id: str, resource_key: str) -> None:
        """撤销指定操作最近一次尚未补偿的提交。"""

    def add_compensation_record(self, record: Mapping[str, Any]) -> None:
        """追加一条安全补偿记录。"""


class InMemoryWorkflowStateStore:
    """线程安全的模拟状态存储，仅保留经过净化的可持久化形态数据。"""

    def __init__(self) -> None:
        self.resource_states: dict[str, int] = {}
        self.committed_operations: list[dict[str, Any]] = []
        self.idempotency_records: dict[str, IdempotencyRecord] = {}
        self.audit_records: list[dict[str, Any]] = []
        self.compensation_records: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._sequence = 0

    def get_resource_state(self, resource_key: str) -> int:
        with self._lock:
            return self.resource_states.get(resource_key, 0)

    def get_idempotency_record(self, key: str) -> IdempotencyRecord | None:
        with self._lock:
            return self.idempotency_records.get(key)

    def commit(
        self,
        *,
        request: ComplexWorkflowRequest,
        successful_item_ids: tuple[str, ...],
        request_digest: str,
    ) -> int:
        with self._lock:
            self._sequence += 1
            before = self.resource_states.get(request.resource_key, 0)
            after = before + 1
            self.resource_states[request.resource_key] = after
            self.committed_operations.append(
                {
                    "sequence": self._sequence,
                    "operation_id": request.operation_id,
                    "resource_key": request.resource_key,
                    "execution_mode": request.execution_mode.value,
                    "request_digest": request_digest,
                    "successful_item_ids": list(successful_item_ids),
                    "resource_version_before": before,
                    "resource_version_after": after,
                    "compensated": False,
                }
            )
            self._after_mutation()
            return self._sequence

    def save_idempotency_result(
        self, key: str, request_digest: str, result: ComplexWorkflowResult
    ) -> None:
        with self._lock:
            existing = self.idempotency_records.get(key)
            if existing is not None and existing.request_digest != request_digest:
                raise WorkflowSimulationError(
                    "TOOL_IDEMPOTENCY_CONFLICT",
                    WorkflowStage.FINALIZE,
                    result.operation_id,
                    side_effect_committed=result.side_effect_committed,
                )
            self.idempotency_records[key] = IdempotencyRecord(request_digest, result)
            self._after_mutation()

    def add_audit_record(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self.audit_records.append(dict(record))
            self._after_mutation()

    def compensate(self, operation_id: str, resource_key: str) -> None:
        with self._lock:
            for record in reversed(self.committed_operations):
                if (
                    record["operation_id"] == operation_id
                    and record["resource_key"] == resource_key
                    and not record["compensated"]
                ):
                    record["compensated"] = True
                    self.resource_states[resource_key] = record["resource_version_before"]
                    self._after_mutation()
                    return
            raise WorkflowSimulationError(
                "TOOL_COMPENSATION_FAILURE",
                WorkflowStage.COMPENSATE_COMMITTED_CHANGES,
                operation_id,
                side_effect_committed=True,
                compensation_attempted=True,
            )

    def add_compensation_record(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self.compensation_records.append(dict(record))
            self._after_mutation()

    def _after_mutation(self) -> None:
        pass


class JsonFileWorkflowStateStore(InMemoryWorkflowStateStore):
    """可选的原子 JSON 存储，仅使用调用方明确提供的目录。"""

    FILE_NAME = "complex_workflow_state.json"

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        if not isinstance(directory, (str, os.PathLike)):
            raise ValueError("directory must be an explicit filesystem path")
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("directory must already exist")
        self.directory = root
        self.file_path = root / self.FILE_NAME
        super().__init__()
        if self.file_path.exists():
            self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("workflow state file is invalid") from exc
        self.resource_states = {
            str(key): int(value) for key, value in payload.get("resource_states", {}).items()
        }
        self.committed_operations = list(payload.get("committed_operations", []))
        self.audit_records = list(payload.get("audit_records", []))
        self.compensation_records = list(payload.get("compensation_records", []))
        self._sequence = int(payload.get("sequence", len(self.committed_operations)))
        self.idempotency_records = {
            str(key): IdempotencyRecord(
                request_digest=str(value["request_digest"]),
                result=_result_from_dict(value["result"]),
            )
            for key, value in payload.get("idempotency_records", {}).items()
        }

    def _after_mutation(self) -> None:
        payload = {
            "resource_states": self.resource_states,
            "committed_operations": self.committed_operations,
            "idempotency_records": {
                key: {
                    "request_digest": value.request_digest,
                    "result": value.result.to_dict(),
                }
                for key, value in self.idempotency_records.items()
            },
            "audit_records": self.audit_records,
            "compensation_records": self.compensation_records,
            "sequence": self._sequence,
        }
        fd, temp_name = tempfile.mkstemp(
            prefix=".complex-workflow-", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.file_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


class WorkflowResourceLockManager:
    """进程内非阻塞的资源键互斥管理器。"""

    def __init__(self) -> None:
        self._held: set[str] = set()
        self._lock = threading.Lock()

    def acquire(self, resource_key: str) -> bool:
        with self._lock:
            if resource_key in self._held:
                return False
            self._held.add(resource_key)
            return True

    def release(self, resource_key: str) -> None:
        with self._lock:
            self._held.discard(resource_key)

    def is_locked(self, resource_key: str) -> bool:
        with self._lock:
            return resource_key in self._held


@dataclass
class _ExecutionState:
    records: list[WorkflowStageRecord] = field(default_factory=list)
    item_results: list[WorkflowItemResult] = field(default_factory=list)
    side_effect_committed: bool = False
    compensation_attempted: bool = False
    compensation_succeeded: bool = False
    audit_digest: str | None = None
    resource_acquired: bool = False


class ComplexWorkflowSimulationTool:
    """同步且有明确边界的复杂流程模拟工具。"""

    def __init__(
        self,
        *,
        state_store: WorkflowStateStore | None = None,
        lock_manager: WorkflowResourceLockManager | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        cancellation_probe: Callable[[], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = state_store or InMemoryWorkflowStateStore()
        self.lock_manager = lock_manager or WorkflowResourceLockManager()
        self._sleeper = sleeper
        self._cancellation_probe = cancellation_probe or (lambda: False)
        self._monotonic = monotonic
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))

    def execute(self, request: ComplexWorkflowRequest) -> ComplexWorkflowResult:
        if not isinstance(request, ComplexWorkflowRequest):
            raise ValueError("request must be a ComplexWorkflowRequest")
        started = self._monotonic()
        state = _ExecutionState()
        request_digest = _request_digest(request)

        replay = self._check_idempotency(request, request_digest, started)
        if replay is not None:
            return replay

        try:
            self._stage(state, WorkflowStage.VALIDATE_REQUEST)
            if request.failure_injection == WorkflowFailureInjection.VALIDATION_ERROR:
                self._fail("TOOL_VALIDATION_ERROR", WorkflowStage.VALIDATE_REQUEST, request, state)

            self._stage(state, WorkflowStage.LOAD_EXISTING_STATE)
            self.state_store.get_resource_state(request.resource_key)

            self._raise_if_cancelled(WorkflowStage.ACQUIRE_RESOURCE, request, state)
            if request.failure_injection == WorkflowFailureInjection.RESOURCE_CONFLICT:
                self._fail("TOOL_RESOURCE_CONFLICT", WorkflowStage.ACQUIRE_RESOURCE, request, state)
            if not self.lock_manager.acquire(request.resource_key):
                self._fail("TOOL_RESOURCE_CONFLICT", WorkflowStage.ACQUIRE_RESOURCE, request, state)
            state.resource_acquired = True
            self._stage(state, WorkflowStage.ACQUIRE_RESOURCE)

            self._stage(state, WorkflowStage.CREATE_SNAPSHOT)
            self.state_store.get_resource_state(request.resource_key)
            self._stage(state, WorkflowStage.PREPARE_ITEMS)

            self._inject_before_side_effect(request, state, WorkflowStage.PROCESS_ITEMS)
            state.item_results = self._process_items(request, state)
            self._stage(
                state,
                WorkflowStage.PROCESS_ITEMS,
                processed_item_count=len(state.item_results),
            )

            failed_items = [
                item for item in state.item_results if item.status == WorkflowItemStatus.FAILED
            ]
            self._stage(
                state,
                WorkflowStage.VALIDATE_PROCESSED_ITEMS,
                processed_item_count=len(state.item_results),
            )
            self._inject_before_side_effect(
                request, state, WorkflowStage.VALIDATE_PROCESSED_ITEMS
            )
            if failed_items and not request.processing_options.allow_partial_success:
                self._fail(
                    "TOOL_PARTIAL_FAILURE",
                    WorkflowStage.VALIDATE_PROCESSED_ITEMS,
                    request,
                    state,
                )

            self._raise_if_cancelled(WorkflowStage.COMMIT_SIDE_EFFECTS, request, state)
            self._inject_before_side_effect(
                request, state, WorkflowStage.COMMIT_SIDE_EFFECTS
            )
            if request.execution_mode != WorkflowExecutionMode.DRY_RUN:
                successful_ids = tuple(
                    result.item_id
                    for result in state.item_results
                    if result.status == WorkflowItemStatus.SUCCEEDED
                )
                self.state_store.commit(
                    request=request,
                    successful_item_ids=successful_ids,
                    request_digest=request_digest,
                )
                state.side_effect_committed = True
                state.item_results = [
                    replace(
                        item,
                        side_effect_committed=item.status == WorkflowItemStatus.SUCCEEDED,
                    )
                    for item in state.item_results
                ]
            self._stage(
                state,
                WorkflowStage.COMMIT_SIDE_EFFECTS,
                processed_item_count=len(state.item_results),
            )

            if request.failure_injection in {
                WorkflowFailureInjection.FAIL_AFTER_SIDE_EFFECT,
                WorkflowFailureInjection.COMPENSATION_FAILURE,
            }:
                self._fail(
                    "TOOL_SIDE_EFFECT_FAILURE",
                    WorkflowStage.CREATE_AUDIT_RECORD,
                    request,
                    state,
                )

            state.audit_digest = self._create_audit_digest(request, state.item_results)
            if request.execution_mode != WorkflowExecutionMode.DRY_RUN:
                self.state_store.add_audit_record(
                    {
                        "operation_id": request.operation_id,
                        "resource_key": request.resource_key,
                        "audit_digest": state.audit_digest,
                        "status": (
                            WorkflowResultStatus.PARTIALLY_SUCCEEDED.value
                            if failed_items
                            else WorkflowResultStatus.SUCCEEDED.value
                        ),
                    }
                )
            self._stage(
                state,
                WorkflowStage.CREATE_AUDIT_RECORD,
                processed_item_count=len(state.item_results),
            )
            self._stage(
                state,
                WorkflowStage.FINALIZE,
                processed_item_count=len(state.item_results),
            )
            self._raise_if_cancelled(WorkflowStage.FINALIZE, request, state)
            self._release_resource(request, state)

            result = self._build_result(
                request,
                state,
                (
                    WorkflowResultStatus.PARTIALLY_SUCCEEDED
                    if failed_items
                    else WorkflowResultStatus.SUCCEEDED
                ),
                started,
                safe_error_code=(
                    "TOOL_PARTIAL_FAILURE" if failed_items else None
                ),
            )
            self._save_idempotency(request, request_digest, result)
            return result
        except WorkflowSimulationError as error:
            return self._handle_failure(request, request_digest, state, error, started)
        except Exception:
            error = WorkflowSimulationError(
                "TOOL_UNKNOWN_FAILURE",
                WorkflowStage.FINALIZE_FAILURE,
                request.operation_id,
                side_effect_committed=state.side_effect_committed,
                compensation_attempted=state.compensation_attempted,
                compensation_succeeded=state.compensation_succeeded,
            )
            return self._handle_failure(request, request_digest, state, error, started)
        finally:
            if state.resource_acquired:
                self.lock_manager.release(request.resource_key)
                state.resource_acquired = False

    def _check_idempotency(
        self, request: ComplexWorkflowRequest, request_digest: str, started: float
    ) -> ComplexWorkflowResult | None:
        if request.execution_mode != WorkflowExecutionMode.IDEMPOTENT_COMMIT:
            return None
        assert request.idempotency_key is not None
        record = self.state_store.get_idempotency_record(request.idempotency_key)
        if record is None:
            return None
        if record.request_digest != request_digest:
            return ComplexWorkflowResult(
                operation_id=request.operation_id,
                resource_key=request.resource_key,
                idempotency_key=request.idempotency_key,
                execution_mode=request.execution_mode,
                status=WorkflowResultStatus.FAILED,
                completed_stages=(),
                item_results=(),
                side_effect_committed=False,
                compensation_attempted=False,
                compensation_succeeded=False,
                idempotency_replayed=False,
                audit_digest=None,
                safe_error_code="TOOL_IDEMPOTENCY_CONFLICT",
                safe_message=_SAFE_MESSAGES["TOOL_IDEMPOTENCY_CONFLICT"],
                duration_ms=self._duration_ms(started),
            )
        historical = record.result
        return replace(
            historical,
            status=WorkflowResultStatus.IDEMPOTENCY_REPLAY,
            idempotency_replayed=True,
            safe_error_code=None,
            safe_message="The previous simulated result was replayed.",
            duration_ms=self._duration_ms(started),
        )

    def _process_items(
        self, request: ComplexWorkflowRequest, state: _ExecutionState
    ) -> list[WorkflowItemResult]:
        prepared: list[WorkflowItem] = []
        for item in request.items:
            self._raise_if_cancelled(WorkflowStage.PROCESS_ITEMS, request, state)
            prepared.append(item)

        workers = min(request.processing_options.max_parallel_items, len(prepared))
        results: list[WorkflowItemResult | None] = [None] * len(prepared)
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="workflow-simulation"
        ) as executor:
            futures = {
                executor.submit(self._process_one_item, request, item): index
                for index, item in enumerate(prepared)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception:
                    results[index] = WorkflowItemResult(
                        item_id=prepared[index].item_id,
                        status=WorkflowItemStatus.FAILED,
                        safe_code="ITEM_PROCESSING_FAILED",
                        attempted=True,
                    )
        return [result for result in results if result is not None]

    def _process_one_item(
        self, request: ComplexWorkflowRequest, item: WorkflowItem
    ) -> WorkflowItemResult:
        delay = request.processing_options.processing_delay_ms / 1000
        if delay:
            self._sleeper(delay)
        selected_failure_id = request.failure_item_id or request.items[0].item_id
        if (
            request.failure_injection == WorkflowFailureInjection.PARTIAL_ITEM_FAILURE
            and item.item_id == selected_failure_id
        ):
            return WorkflowItemResult(
                item_id=item.item_id,
                status=WorkflowItemStatus.FAILED,
                safe_code="ITEM_INJECTED_FAILURE",
                attempted=True,
            )
        return WorkflowItemResult(
            item_id=item.item_id,
            status=WorkflowItemStatus.SUCCEEDED,
            safe_code="ITEM_PROCESSED",
            attempted=True,
        )

    def _inject_before_side_effect(
        self,
        request: ComplexWorkflowRequest,
        state: _ExecutionState,
        current_stage: WorkflowStage,
    ) -> None:
        configured_stage = request.failure_stage
        if configured_stage is not None and configured_stage != current_stage:
            return
        if request.failure_injection == WorkflowFailureInjection.TRANSIENT_BEFORE_SIDE_EFFECT:
            self._fail("TOOL_TRANSIENT_FAILURE", current_stage, request, state)
        if request.failure_injection == WorkflowFailureInjection.TIMEOUT_BEFORE_SIDE_EFFECT:
            self._fail("TOOL_TIMEOUT", current_stage, request, state)
        if request.failure_injection == WorkflowFailureInjection.UNKNOWN_FAILURE:
            self._fail("TOOL_UNKNOWN_FAILURE", current_stage, request, state)

    def _handle_failure(
        self,
        request: ComplexWorkflowRequest,
        request_digest: str,
        state: _ExecutionState,
        error: WorkflowSimulationError,
        started: float,
    ) -> ComplexWorkflowResult:
        final_error = error
        self._stage(
            state,
            error.stage,
            status=WorkflowStageStatus.FAILED,
            safe_code=error.safe_error_code,
            processed_item_count=len(state.item_results),
        )
        if state.side_effect_committed and request.processing_options.enable_compensation:
            state.compensation_attempted = True
            try:
                # 仍然执行取消检查，但已取消的调用在提交后必须获准完成安全清理。
                if error.safe_error_code != "TOOL_CANCELLED":
                    self._raise_if_cancelled(
                        WorkflowStage.COMPENSATE_COMMITTED_CHANGES, request, state
                    )
                else:
                    self._probe_cancelled()
                if (
                    request.failure_injection
                    == WorkflowFailureInjection.COMPENSATION_FAILURE
                ):
                    self._fail(
                        "TOOL_COMPENSATION_FAILURE",
                        WorkflowStage.COMPENSATE_COMMITTED_CHANGES,
                        request,
                        state,
                    )
                self.state_store.compensate(request.operation_id, request.resource_key)
                state.compensation_succeeded = True
                state.side_effect_committed = False
                state.item_results = [
                    replace(item, side_effect_committed=False)
                    for item in state.item_results
                ]
                self.state_store.add_compensation_record(
                    {
                        "operation_id": request.operation_id,
                        "resource_key": request.resource_key,
                        "status": "SUCCEEDED",
                    }
                )
                self._stage(
                    state,
                    WorkflowStage.COMPENSATE_COMMITTED_CHANGES,
                    processed_item_count=len(state.item_results),
                )
            except WorkflowSimulationError as compensation_error:
                final_error = compensation_error
                state.compensation_succeeded = False
                self.state_store.add_compensation_record(
                    {
                        "operation_id": request.operation_id,
                        "resource_key": request.resource_key,
                        "status": "FAILED",
                        "safe_code": "TOOL_COMPENSATION_FAILURE",
                    }
                )
                self._stage(
                    state,
                    WorkflowStage.COMPENSATE_COMMITTED_CHANGES,
                    status=WorkflowStageStatus.FAILED,
                    safe_code="TOOL_COMPENSATION_FAILURE",
                    processed_item_count=len(state.item_results),
                )

        self._stage(
            state,
            WorkflowStage.FINALIZE_FAILURE,
            status=WorkflowStageStatus.FAILED,
            safe_code=final_error.safe_error_code,
            processed_item_count=len(state.item_results),
        )
        self._release_resource(request, state)
        status = WorkflowResultStatus.FAILED
        if final_error.safe_error_code == "TOOL_TIMEOUT":
            status = WorkflowResultStatus.TIMED_OUT
        elif final_error.safe_error_code == "TOOL_CANCELLED":
            status = WorkflowResultStatus.CANCELLED
        result = self._build_result(
            request,
            state,
            status,
            started,
            safe_error_code=final_error.safe_error_code,
        )
        self._save_idempotency(request, request_digest, result)
        return result

    def _save_idempotency(
        self,
        request: ComplexWorkflowRequest,
        request_digest: str,
        result: ComplexWorkflowResult,
    ) -> None:
        if (
            request.execution_mode == WorkflowExecutionMode.IDEMPOTENT_COMMIT
            and request.idempotency_key is not None
            and (
                result.status
                in {
                    WorkflowResultStatus.SUCCEEDED,
                    WorkflowResultStatus.PARTIALLY_SUCCEEDED,
                }
                or result.side_effect_committed
                or result.compensation_attempted
            )
        ):
            self.state_store.save_idempotency_result(
                request.idempotency_key, request_digest, result
            )

    def _build_result(
        self,
        request: ComplexWorkflowRequest,
        state: _ExecutionState,
        status: WorkflowResultStatus,
        started: float,
        *,
        safe_error_code: str | None,
    ) -> ComplexWorkflowResult:
        return ComplexWorkflowResult(
            operation_id=request.operation_id,
            resource_key=request.resource_key,
            idempotency_key=request.idempotency_key,
            execution_mode=request.execution_mode,
            status=status,
            completed_stages=tuple(state.records),
            item_results=tuple(state.item_results),
            side_effect_committed=state.side_effect_committed,
            compensation_attempted=state.compensation_attempted,
            compensation_succeeded=state.compensation_succeeded,
            idempotency_replayed=False,
            audit_digest=state.audit_digest,
            safe_error_code=safe_error_code,
            safe_message=(
                _SAFE_MESSAGES[safe_error_code]
                if safe_error_code is not None
                else "The simulated workflow completed."
            ),
            duration_ms=self._duration_ms(started),
        )

    def _stage(
        self,
        state: _ExecutionState,
        stage: WorkflowStage,
        *,
        status: WorkflowStageStatus = WorkflowStageStatus.SUCCEEDED,
        safe_code: str = "STAGE_COMPLETED",
        processed_item_count: int = 0,
    ) -> None:
        now = self._utc_now().astimezone(timezone.utc).isoformat()
        state.records.append(
            WorkflowStageRecord(
                stage=stage,
                started_at=now,
                completed_at=now,
                status=status,
                safe_code=safe_code,
                processed_item_count=processed_item_count,
            )
        )

    def _raise_if_cancelled(
        self,
        stage: WorkflowStage,
        request: ComplexWorkflowRequest,
        state: _ExecutionState,
    ) -> None:
        if self._probe_cancelled():
            self._fail("TOOL_CANCELLED", stage, request, state)

    def _probe_cancelled(self) -> bool:
        try:
            return bool(self._cancellation_probe())
        except Exception:
            return True

    def _release_resource(
        self, request: ComplexWorkflowRequest, state: _ExecutionState
    ) -> None:
        if not state.resource_acquired:
            return
        self.lock_manager.release(request.resource_key)
        state.resource_acquired = False
        self._stage(
            state,
            WorkflowStage.RELEASE_RESOURCE,
            processed_item_count=len(state.item_results),
        )

    @staticmethod
    def _fail(
        code: str,
        stage: WorkflowStage,
        request: ComplexWorkflowRequest,
        state: _ExecutionState,
    ) -> None:
        raise WorkflowSimulationError(
            code,
            stage,
            request.operation_id,
            side_effect_committed=state.side_effect_committed,
            compensation_attempted=state.compensation_attempted,
            compensation_succeeded=state.compensation_succeeded,
        )

    @staticmethod
    def _create_audit_digest(
        request: ComplexWorkflowRequest, items: Sequence[WorkflowItemResult]
    ) -> str:
        safe_summary = {
            "operation_id": request.operation_id,
            "resource_key": request.resource_key,
            "execution_mode": request.execution_mode.value,
            "item_codes": [
                [item.item_id, item.status.value, item.safe_code] for item in items
            ],
        }
        encoded = json.dumps(
            safe_summary, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _duration_ms(self, started: float) -> int:
        return max(0, int((self._monotonic() - started) * 1000))


def _request_digest(request: ComplexWorkflowRequest) -> str:
    """哈希与执行相关的数据，不保留元数据或属性原文。"""
    normalized = {
        "operation_id": request.operation_id,
        "resource_key": request.resource_key,
        "execution_mode": request.execution_mode.value,
        "items": [
            {
                "item_id": item.item_id,
                "action": item.action,
                "quantity": item.quantity,
                "priority": item.priority,
                "attributes_digest": hashlib.sha256(
                    json.dumps(
                        item.attributes,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=lambda _: "<unsupported>",
                    ).encode("utf-8")
                ).hexdigest(),
            }
            for item in request.items
        ],
        "failure_injection": request.failure_injection.value,
        "failure_stage": (
            request.failure_stage.value if request.failure_stage is not None else None
        ),
        "failure_item_id": request.failure_item_id,
        "processing_options": {
            "max_parallel_items": request.processing_options.max_parallel_items,
            "processing_delay_ms": request.processing_options.processing_delay_ms,
            "allow_partial_success": request.processing_options.allow_partial_success,
            "enable_compensation": request.processing_options.enable_compensation,
        },
    }
    return hashlib.sha256(
        json.dumps(
            normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _result_from_dict(value: Mapping[str, Any]) -> ComplexWorkflowResult:
    """仅重建模拟器自身的安全序列化结果结构。"""
    return ComplexWorkflowResult(
        operation_id=value["operation_id"],
        resource_key=value["resource_key"],
        idempotency_key=value.get("idempotency_key"),
        execution_mode=WorkflowExecutionMode(value["execution_mode"]),
        status=WorkflowResultStatus(value["status"]),
        completed_stages=tuple(
            WorkflowStageRecord(
                stage=WorkflowStage(record["stage"]),
                started_at=record["started_at"],
                completed_at=record["completed_at"],
                status=WorkflowStageStatus(record["status"]),
                safe_code=record["safe_code"],
                processed_item_count=int(record["processed_item_count"]),
            )
            for record in value.get("completed_stages", [])
        ),
        item_results=tuple(
            WorkflowItemResult(
                item_id=item["item_id"],
                status=WorkflowItemStatus(item["status"]),
                safe_code=item["safe_code"],
                attempted=bool(item["attempted"]),
                side_effect_committed=bool(item["side_effect_committed"]),
            )
            for item in value.get("item_results", [])
        ),
        side_effect_committed=bool(value["side_effect_committed"]),
        compensation_attempted=bool(value["compensation_attempted"]),
        compensation_succeeded=bool(value["compensation_succeeded"]),
        idempotency_replayed=bool(value["idempotency_replayed"]),
        audit_digest=value.get("audit_digest"),
        safe_error_code=value.get("safe_error_code"),
        safe_message=value["safe_message"],
        duration_ms=int(value["duration_ms"]),
    )


def _workflow_stage(value: Any) -> WorkflowStage:
    if isinstance(value, WorkflowStage):
        return value
    if isinstance(value, str) and value in WorkflowStage.__members__:
        return WorkflowStage.__members__[value]
    return WorkflowStage(value)


def _require_text(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_identifier(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a stable safe identifier")


def _require_integer(
    value: Any,
    field_name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} must be <= {maximum}")


_LEGACY_STATE_STORE = InMemoryWorkflowStateStore()
_LEGACY_TOOL = ComplexWorkflowSimulationTool(state_store=_LEGACY_STATE_STORE)


def complex_workflow_simulator(argument_text: str) -> str:
    """接受单个 JSON 对象的旧版 ``Callable[[str], str]`` 包装器。"""
    operation_id = "invalid-request"
    resource_key = "invalid-resource"
    try:
        payload = json.loads(argument_text)
        if isinstance(payload, Mapping):
            raw_operation_id = payload.get("operation_id")
            raw_resource_key = payload.get("resource_key")
            if isinstance(raw_operation_id, str) and _SAFE_IDENTIFIER.fullmatch(raw_operation_id):
                operation_id = raw_operation_id
            if isinstance(raw_resource_key, str) and _SAFE_IDENTIFIER.fullmatch(raw_resource_key):
                resource_key = raw_resource_key
        request = ComplexWorkflowRequest.from_dict(payload)
        result = _LEGACY_TOOL.execute(request)
        response = result.to_dict()
    except (json.JSONDecodeError, ValueError, TypeError):
        response = {
            "operation_id": operation_id,
            "resource_key": resource_key,
            "idempotency_key": None,
            "execution_mode": None,
            "status": WorkflowResultStatus.FAILED.value,
            "completed_stages": [],
            "item_results": [],
            "side_effect_committed": False,
            "compensation_attempted": False,
            "compensation_succeeded": False,
            "idempotency_replayed": False,
            "audit_digest": None,
            "safe_error_code": "TOOL_VALIDATION_ERROR",
            "safe_message": _SAFE_MESSAGES["TOOL_VALIDATION_ERROR"],
            "duration_ms": 0,
        }
    return json.dumps(response, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


__all__ = [
    "ComplexWorkflowRequest",
    "ComplexWorkflowResult",
    "ComplexWorkflowSimulationTool",
    "IdempotencyRecord",
    "InMemoryWorkflowStateStore",
    "JsonFileWorkflowStateStore",
    "WorkflowExecutionMode",
    "WorkflowFailureInjection",
    "WorkflowItem",
    "WorkflowItemResult",
    "WorkflowItemStatus",
    "WorkflowProcessingOptions",
    "WorkflowResourceLockManager",
    "WorkflowResultStatus",
    "WorkflowSimulationError",
    "WorkflowStage",
    "WorkflowStageRecord",
    "WorkflowStageStatus",
    "WorkflowStateStore",
    "complex_workflow_simulator",
]
