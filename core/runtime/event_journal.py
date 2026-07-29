#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Append-only Runtime Event Journal 的公共契约与安全记录。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from core.runtime.events import (
    RuntimeEvent,
    RuntimeEventType,
    validate_journal_payload,
)


JOURNAL_SCHEMA_VERSION = 1
MAX_READ_LIMIT = 1000


class JournalAppendStatus(str, Enum):
    APPENDED = "APPENDED"
    DUPLICATE = "DUPLICATE"


class JournalErrorCode(str, Enum):
    EVENT_ID_CONFLICT = "EVENT_ID_CONFLICT"
    SEQUENCE_CONFLICT = "SEQUENCE_CONFLICT"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    RUN_ALREADY_TERMINAL = "RUN_ALREADY_TERMINAL"
    JOURNAL_APPEND_FAILED = "JOURNAL_APPEND_FAILED"
    JOURNAL_CORRUPTED = "JOURNAL_CORRUPTED"


class JournalError(Exception):
    """不暴露 SQL、文件路径或原始异常的类型化 Journal 错误。"""

    def __init__(self, error_code: JournalErrorCode, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")

    def __repr__(self) -> str:
        return (
            f"JournalError(error_code={self.error_code.value!r}, "
            f"safe_message={self.safe_message!r})"
        )


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} 必须是正整数且不能是 bool")


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")


def _require_utc(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
    ):
        raise ValueError(f"{field_name} 必须是 timezone-aware UTC")


def _validate_json_value(value: object, field_name: str = "safe_payload") -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} 不允许 NaN 或 Infinity")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, field_name)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} 的 key 必须是字符串")
            _validate_json_value(item, field_name)
        return
    raise ValueError(f"{field_name} 包含不支持的 JSON 值")


def canonical_json(value: object) -> str:
    """生成用于摘要的规范 JSON，并拒绝非有限数字。"""
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class JournalRecord:
    journal_schema_version: int
    event_schema_version: int
    event_id: str
    run_id: str
    trace_id: str
    sequence: int
    emitted_at: datetime
    journaled_at: datetime
    event_type: RuntimeEventType
    component: str
    safe_payload: dict[str, object]
    payload_digest: str
    event_digest: str
    step_id: str | None = None
    step_sequence: int | None = None
    span_id: str | None = None
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.journal_schema_version, "journal_schema_version")
        if self.journal_schema_version != JOURNAL_SCHEMA_VERSION:
            raise ValueError("不支持的 journal_schema_version")
        _require_positive_int(self.event_schema_version, "event_schema_version")
        _require_text(self.event_id, "event_id")
        _require_text(self.run_id, "run_id")
        _require_text(self.trace_id, "trace_id")
        _require_positive_int(self.sequence, "sequence")
        _require_utc(self.emitted_at, "emitted_at")
        _require_utc(self.journaled_at, "journaled_at")
        if not isinstance(self.event_type, RuntimeEventType):
            raise TypeError("event_type 必须是 RuntimeEventType")
        _require_text(self.component, "component")
        if not isinstance(self.safe_payload, dict):
            raise TypeError("safe_payload 必须是 dict")
        _validate_json_value(self.safe_payload)
        validate_journal_payload(self.event_type, self.safe_payload)
        for value, name in (
            (self.payload_digest, "payload_digest"),
            (self.event_digest, "event_digest"),
        ):
            _require_text(value, name)
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError(f"{name} 必须是小写 SHA-256")
        if self.step_id is None:
            if self.step_sequence is not None:
                raise ValueError("没有 step_id 时不能设置 step_sequence")
        else:
            _require_text(self.step_id, "step_id")
            _require_positive_int(self.step_sequence, "step_sequence")  # type: ignore[arg-type]
        for value, name in ((self.span_id, "span_id"), (self.parent_span_id, "parent_span_id")):
            if value is not None:
                _require_text(value, name)

    @classmethod
    def from_event(
        cls,
        event: RuntimeEvent,
        *,
        journaled_at: datetime | None = None,
    ) -> "JournalRecord":
        """保留既有事件身份与序号，生成安全且可校验的 Journal 记录。"""
        if not isinstance(event, RuntimeEvent):
            raise TypeError("append 只接受 RuntimeEvent")
        projection = event.to_journal_dict()
        safe_payload = projection["safe_payload"]
        assert isinstance(safe_payload, dict)
        payload_digest = _digest(safe_payload)
        digest_source = {
            "journal_schema_version": JOURNAL_SCHEMA_VERSION,
            "event_schema_version": event.schema_version,
            "event_id": event.event_id,
            "run_id": event.run_id,
            "trace_id": event.trace_id,
            "span_id": event.span_id,
            "parent_span_id": event.parent_span_id,
            "sequence": event.sequence,
            "emitted_at": event.emitted_at.isoformat(),
            "event_type": event.event_type.value,
            "component": event.component,
            "step_id": event.step_id,
            "step_sequence": event.step_sequence,
            "safe_payload": safe_payload,
            "payload_digest": payload_digest,
        }
        return cls(
            journal_schema_version=JOURNAL_SCHEMA_VERSION,
            event_schema_version=event.schema_version,
            event_id=event.event_id,
            run_id=event.run_id,
            trace_id=event.trace_id,
            sequence=event.sequence,
            emitted_at=event.emitted_at,
            journaled_at=journaled_at or datetime.now(UTC),
            event_type=event.event_type,
            component=event.component,
            safe_payload=dict(safe_payload),
            payload_digest=payload_digest,
            event_digest=_digest(digest_source),
            step_id=event.step_id,
            step_sequence=event.step_sequence,
            span_id=event.span_id,
            parent_span_id=event.parent_span_id,
        )

    def verify(self) -> None:
        """读取时校验 Payload 与 Event 摘要；损坏时 fail closed。"""
        expected_payload = _digest(self.safe_payload)
        expected_event = _digest(self._event_digest_source(expected_payload))
        if (
            expected_payload != self.payload_digest
            or expected_event != self.event_digest
        ):
            raise JournalError(
                JournalErrorCode.JOURNAL_CORRUPTED,
                "Journal 记录摘要校验失败",
            )

    def _event_digest_source(self, payload_digest: str) -> dict[str, object]:
        return {
            "journal_schema_version": self.journal_schema_version,
            "event_schema_version": self.event_schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "sequence": self.sequence,
            "emitted_at": self.emitted_at.isoformat(),
            "event_type": self.event_type.value,
            "component": self.component,
            "step_id": self.step_id,
            "step_sequence": self.step_sequence,
            "safe_payload": self.safe_payload,
            "payload_digest": payload_digest,
        }

    def __repr__(self) -> str:
        return (
            "JournalRecord("
            f"event_id={self.event_id!r}, run_id={self.run_id!r}, "
            f"sequence={self.sequence}, event_type={self.event_type.value!r}, "
            f"payload_digest={self.payload_digest!r}, "
            f"event_digest={self.event_digest!r})"
        )


def validate_read_arguments(run_id: str, sequence: int, limit: int) -> None:
    _require_text(run_id, "run_id")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("sequence 必须是非负整数且不能是 bool")
    _require_positive_int(limit, "limit")
    if limit > MAX_READ_LIMIT:
        raise ValueError(f"limit 不能大于 {MAX_READ_LIMIT}")


@runtime_checkable
class RunEventJournal(Protocol):
    def append(self, event: RuntimeEvent) -> JournalAppendStatus: ...

    def read_after(
        self, run_id: str, sequence: int, limit: int
    ) -> tuple[JournalRecord, ...]: ...

    def get_by_event_id(self, event_id: str) -> JournalRecord | None: ...

    def last_sequence(self, run_id: str) -> int | None: ...

    def close(self) -> None: ...
