#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LocalAgent 的最小运行上下文数据与截止时间处理。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
import math
import time
import uuid

from core.runtime.cancellation import CancellationSource, CancellationToken

LEGACY_DEFAULT_SESSION_ID = "legacy-default"


class RunDeadlineExceededError(TimeoutError):
    """运行截止时间到期时引发。"""


class Clock(Protocol):
    """用于测试截止时间计算且无需休眠的小型时钟抽象。"""

    def utc_now(self) -> datetime:
        """返回当前带时区的 UTC 时间戳。"""

    def monotonic(self) -> float:
        """返回当前单调时钟值（秒）。"""


class SystemClock:
    """由 Python 标准库实现的时钟。"""

    def utc_now(self) -> datetime:
        """返回当前带时区的 UTC 时间戳。"""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """返回当前单调时钟值（秒）。"""
        return time.monotonic()


@dataclass(frozen=True)
class RunIdentifiers:
    """用于区分运行、会话和追踪范围的非敏感标识符。"""

    run_id: str
    session_id: str
    trace_id: str

    def __post_init__(self) -> None:
        """在标识符进入运行上下文前拒绝空值。"""
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if not self.trace_id:
            raise ValueError("trace_id must not be empty")


@dataclass(frozen=True)
class RunContextData:
    """可安全持久化或输出至诊断信息的可序列化运行元数据。"""

    identifiers: RunIdentifiers
    created_at: datetime
    deadline_at: datetime | None
    entry_agent_id: str

    def __post_init__(self) -> None:
        """校验可序列化运行数据的不变量。"""
        _ensure_utc_datetime(self.created_at, "created_at")
        if self.deadline_at is not None:
            _ensure_utc_datetime(self.deadline_at, "deadline_at")
        if not self.entry_agent_id:
            raise ValueError("entry_agent_id must not be empty")

    def to_dict(self) -> dict[str, str | None]:
        """仅序列化显式数据字段，绝不序列化进程本地依赖。"""
        return {
            "run_id": self.identifiers.run_id,
            "session_id": self.identifiers.session_id,
            "trace_id": self.identifiers.trace_id,
            "created_at": self.created_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "entry_agent_id": self.entry_agent_id,
        }


class Deadline:
    """将可序列化 UTC 截止时间与进程本地单调截止时间配对。"""

    def __init__(self, timeout_seconds: float | None, clock: Clock) -> None:
        self._clock = clock
        if timeout_seconds is None:
            self.deadline_at: datetime | None = None
            self._monotonic_deadline: float | None = None
            return
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("timeout_seconds must be a positive finite number when provided")
        self.deadline_at = clock.utc_now() + timedelta(seconds=timeout_seconds)
        self._monotonic_deadline = clock.monotonic() + timeout_seconds

    def remaining_seconds(self) -> float | None:
        """返回剩余秒数；无截止时间时返回 None，到期后返回零。"""
        if self._monotonic_deadline is None:
            return None
        return max(0.0, self._monotonic_deadline - self._clock.monotonic())

    def raise_if_expired(self) -> None:
        """截止时间已到期时引发明确异常。"""
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise RunDeadlineExceededError("run deadline exceeded")


class RunContext:
    """承载可序列化数据和显式进程内依赖的单次运行上下文。"""

    def __init__(
        self,
        data: RunContextData,
        deadline: Deadline,
        cancellation_token: CancellationToken,
        clock: Clock,
    ) -> None:
        self.data = data
        self._deadline = deadline
        self._cancellation_token = cancellation_token
        self._clock = clock

    @classmethod
    def create(
        cls,
        *,
        entry_agent_id: str,
        session_id: str = LEGACY_DEFAULT_SESSION_ID,
        trace_id: str | None = None,
        timeout_seconds: float | None = None,
        cancellation_source: CancellationSource | None = None,
        clock: Clock | None = None,
    ) -> "RunContext":
        """仅创建上下文；取消源归属重要时，应优先使用 create_run_context。"""
        context, _source = create_run_context(
            entry_agent_id=entry_agent_id,
            session_id=session_id,
            trace_id=trace_id,
            timeout_seconds=timeout_seconds,
            cancellation_source=cancellation_source,
            clock=clock,
        )
        return context

    @property
    def run_id(self) -> str:
        """返回本次运行的唯一标识符。"""
        return self.data.identifiers.run_id

    @property
    def session_id(self) -> str:
        """返回兼容性会话标识符。"""
        return self.data.identifiers.session_id

    @property
    def trace_id(self) -> str:
        """返回端到端追踪关联标识符。"""
        return self.data.identifiers.trace_id

    def remaining_seconds(self) -> float | None:
        """返回截止时间剩余秒数；无截止时间时返回 None。"""
        return self._deadline.remaining_seconds()

    def raise_if_inactive(self) -> None:
        """已请求取消或截止时间到期时引发异常。"""
        self._cancellation_token.raise_if_cancelled()
        self._deadline.raise_if_expired()

    def to_dict(self) -> dict[str, str | None]:
        """仅序列化安全运行元数据，不包括令牌、时钟、锁和事件。"""
        return self.data.to_dict()


def _ensure_utc_datetime(value: datetime, field_name: str) -> None:
    """校验 datetime 是否为带时区的 UTC 时间。"""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")


def create_run_context(
    *,
    entry_agent_id: str,
    session_id: str = LEGACY_DEFAULT_SESSION_ID,
    trace_id: str | None = None,
    timeout_seconds: float | None = None,
    cancellation_source: CancellationSource | None = None,
    clock: Clock | None = None,
) -> tuple[RunContext, CancellationSource]:
    """创建 RunContext，并将其 CancellationSource 返回给调用方所有者。"""
    if not entry_agent_id:
        raise ValueError("entry_agent_id must not be empty")
    if not session_id:
        raise ValueError("session_id must not be empty")
    if trace_id is not None and not trace_id:
        raise ValueError("trace_id must not be empty")
    active_clock = clock or SystemClock()
    source = cancellation_source or CancellationSource()
    deadline = Deadline(timeout_seconds=timeout_seconds, clock=active_clock)
    identifiers = RunIdentifiers(
        run_id=uuid.uuid4().hex,
        session_id=session_id,
        trace_id=trace_id or uuid.uuid4().hex,
    )
    data = RunContextData(
        identifiers=identifiers,
        created_at=active_clock.utc_now(),
        deadline_at=deadline.deadline_at,
        entry_agent_id=entry_agent_id,
    )
    return (
        RunContext(data=data, deadline=deadline, cancellation_token=source.token, clock=active_clock),
        source,
    )
