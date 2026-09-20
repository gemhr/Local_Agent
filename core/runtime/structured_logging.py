#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""仅从 Journal 安全投影生成的结构化 Runtime 日志。"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import IO, Mapping, Protocol
from uuid import uuid4

from core.runtime.event_journal import JournalRecord
from core.runtime.events import RuntimeEventType


class RuntimeLogLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


_SAFE_FIELD_ALLOWLIST = frozenset(
    {
        "candidate_index",
        "routing_adjustment",
        "profile_id",
        "tool_name",
        "stage",
        "input_count",
        "output_count",
        "collection_count",
        "top_k",
        "chunk_count",
        "citation_count",
        "degraded",
        "worker_terminated",
        "execution_detached",
        "resource_release_pending",
        "background_work_pending",
        "fatal",
        "stop_reason",
        "text_length",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeLogRecord:
    timestamp: datetime
    journaled_at: datetime
    level: RuntimeLogLevel
    run_id: str
    trace_id: str
    span_id: str | None
    parent_span_id: str | None
    step_id: str | None
    component: str
    event_id: str
    event_type: str
    status: str | None
    error_code: str | None
    retry_index: int | None
    duration_ms: int | None
    safe_fields: Mapping[str, object]
    # Side-effect reconciliation identity/outcome fields.  These are all
    # bounded safe metadata; raw arguments, credentials and provider payloads
    # are intentionally not representable here.
    invocation_id: str | None = None
    tool_name: str | None = None
    provider_identity: str | None = None
    attempt: int | None = None
    outcome: str | None = None
    manual_actor_id: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.timestamp, "timestamp"),
            (self.journaled_at, "journaled_at"),
        ):
            if (
                value.tzinfo is None
                or value.utcoffset() is None
                or value.utcoffset().total_seconds() != 0
            ):
                raise ValueError(f"{name} 必须是 UTC")
        if self.journaled_at < self.timestamp:
            raise ValueError("journaled_at 不得早于 Event timestamp")
        if not isinstance(self.level, RuntimeLogLevel):
            raise TypeError("level 必须是 RuntimeLogLevel")
        object.__setattr__(
            self, "safe_fields", MappingProxyType(dict(self.safe_fields))
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.astimezone(UTC).isoformat(),
            "journaled_at": self.journaled_at.astimezone(UTC).isoformat(),
            "journal_latency_ms": max(
                0,
                int(
                    (self.journaled_at - self.timestamp).total_seconds() * 1000
                ),
            ),
            "level": self.level.value,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "step_id": self.step_id,
            "component": self.component,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "status": self.status,
            "error_code": self.error_code,
            "retry_index": self.retry_index,
            "duration_ms": self.duration_ms,
            "safe_fields": dict(self.safe_fields),
            "invocation_id": self.invocation_id,
            "tool_name": self.tool_name,
            "provider_identity": self.provider_identity,
            "attempt": self.attempt,
            "outcome": self.outcome,
            "manual_actor_id": self.manual_actor_id,
        }


class StructuredRuntimeLogger(Protocol):
    def write(self, record: RuntimeLogRecord) -> None: ...

    def log(self, record: RuntimeLogRecord) -> None: ...


class JsonStructuredRuntimeLogger:
    """向文本流写入一条一行 JSON；不使用异常堆栈。"""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()

    def write(self, record: RuntimeLogRecord) -> None:
        if not isinstance(record, RuntimeLogRecord):
            raise TypeError("record 必须是 RuntimeLogRecord")
        line = json.dumps(
            record.to_json_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def log(self, record: RuntimeLogRecord) -> None:
        self.write(record)


class InMemoryStructuredRuntimeLogger:
    def __init__(self) -> None:
        self._records: list[RuntimeLogRecord] = []
        self._lock = threading.Lock()

    @property
    def records(self) -> tuple[RuntimeLogRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def write(self, record: RuntimeLogRecord) -> None:
        if not isinstance(record, RuntimeLogRecord):
            raise TypeError("record 必须是 RuntimeLogRecord")
        with self._lock:
            self._records.append(record)

    def log(self, record: RuntimeLogRecord) -> None:
        self.write(record)


class NoopStructuredRuntimeLogger:
    def write(self, record: RuntimeLogRecord) -> None:
        if not isinstance(record, RuntimeLogRecord):
            raise TypeError("record 必须是 RuntimeLogRecord")

    def log(self, record: RuntimeLogRecord) -> None:
        self.write(record)


def emit_reconciliation_log(
    logger: StructuredRuntimeLogger | None,
    *,
    run_id: str,
    step_id: str | None,
    invocation_id: str | None,
    tool_name: str | None,
    provider_identity: str | None,
    attempt: int | None,
    outcome: str,
    safe_error_code: str | None = None,
    manual_actor_id: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Emit one safe reconciliation record through the existing log owner.

    The reconciliation lane supplies only durable identities and bounded
    classifications.  This helper deliberately has no argument/payload
    parameter, so a provider response or credential cannot leak into logs.
    """
    if logger is None:
        return

    def safe_text(value: str | None, *, limit: int = 255) -> str | None:
        if value is None or not isinstance(value, str) or not value or len(value) > limit:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return None
        return value

    safe_run_id = safe_text(run_id)
    safe_outcome = safe_text(outcome, limit=64)
    if safe_run_id is None or safe_outcome is None:
        return
    safe_attempt = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 0 else None
    safe_duration = duration_ms if isinstance(duration_ms, int) and not isinstance(duration_ms, bool) and duration_ms >= 0 else None
    now = datetime.now(UTC)
    record = RuntimeLogRecord(
        timestamp=now,
        journaled_at=now,
        level=(RuntimeLogLevel.ERROR if safe_outcome == "FAILED" else RuntimeLogLevel.INFO),
        run_id=safe_run_id,
        trace_id="reconciliation",
        span_id=None,
        parent_span_id=None,
        step_id=safe_text(step_id),
        component="recovery_coordinator",
        event_id=uuid4().hex,
        event_type="TOOL_RECONCILIATION",
        status=safe_outcome,
        error_code=safe_text(safe_error_code, limit=128),
        retry_index=None,
        duration_ms=safe_duration,
        safe_fields={},
        invocation_id=safe_text(invocation_id),
        tool_name=safe_text(tool_name),
        provider_identity=safe_text(provider_identity),
        attempt=safe_attempt,
        outcome=safe_outcome,
        manual_actor_id=safe_text(manual_actor_id),
    )
    try:
        logger.write(record)
    except Exception:
        # Logging is a best-effort projection and must not affect convergence.
        return


def _status(payload: Mapping[str, object]) -> str | None:
    value = payload.get("status")
    if isinstance(value, str):
        return value
    succeeded = payload.get("succeeded")
    if isinstance(succeeded, bool):
        return "SUCCEEDED" if succeeded else "FAILED"
    return None


def runtime_log_level(record: JournalRecord) -> RuntimeLogLevel:
    payload = record.safe_payload
    event_type = record.event_type
    retry_index = payload.get("retry_index")
    if event_type is RuntimeEventType.CANCELLATION:
        reason = str(payload.get("reason", "")).upper()
        return (
            RuntimeLogLevel.INFO
            if reason in {"USER_REQUESTED", "CLIENT_DISCONNECTED"}
            else RuntimeLogLevel.WARNING
        )
    if event_type is RuntimeEventType.ERROR:
        return RuntimeLogLevel.ERROR
    if event_type in {
        RuntimeEventType.BUDGET_EXHAUSTED,
        RuntimeEventType.TIMEOUT,
    }:
        return RuntimeLogLevel.WARNING
    if isinstance(retry_index, int) and retry_index > 0:
        return RuntimeLogLevel.WARNING
    if payload.get("degraded") is True:
        return RuntimeLogLevel.WARNING
    status = (_status(payload) or "").upper()
    if status in {"PARTIAL", "DEGRADED", "TIMEOUT"}:
        return RuntimeLogLevel.WARNING
    return RuntimeLogLevel.INFO


class StructuredLogProjector:
    """显式 allowlist 投影；绝不序列化 RuntimeEvent 或原始异常。"""

    def __init__(self, logger: StructuredRuntimeLogger) -> None:
        self.logger = logger

    def project(self, record: JournalRecord) -> RuntimeLogRecord:
        if not isinstance(record, JournalRecord):
            raise TypeError("StructuredLogProjector 只接受 JournalRecord")
        record.verify()
        payload = record.safe_payload
        error_code = payload.get("safe_error_code")
        retry_index = payload.get("retry_index")
        duration_ms = payload.get("duration_ms")
        safe_fields = {
            key: value
            for key, value in payload.items()
            if key in _SAFE_FIELD_ALLOWLIST
        }
        projected = RuntimeLogRecord(
            timestamp=record.emitted_at,
            journaled_at=record.journaled_at,
            level=runtime_log_level(record),
            run_id=record.run_id,
            trace_id=record.trace_id,
            span_id=record.span_id,
            parent_span_id=record.parent_span_id,
            step_id=record.step_id,
            component=record.component,
            event_id=record.event_id,
            event_type=record.event_type.value,
            status=_status(payload),
            error_code=error_code if isinstance(error_code, str) else None,
            retry_index=retry_index if isinstance(retry_index, int) else None,
            duration_ms=duration_ms if isinstance(duration_ms, int) else None,
            safe_fields=safe_fields,
        )
        self.logger.write(projected)
        return projected
