#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Bounded, content-free recording for test-only fault injection."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from core.runtime.fault_injection_contract import (
    FaultAction,
    FaultDecision,
    FaultPoint,
    InjectedFaultCode,
    _require_positive_int,
    _require_safe_token,
    _require_utc,
)


class RecorderOverflowPolicy(str, Enum):
    DROP_OLDEST = "DROP_OLDEST"
    REJECT_NEW = "REJECT_NEW"


@dataclass(frozen=True, slots=True)
class FaultInjectionRecord:
    plan_id: str
    rule_id: str
    fault_point: FaultPoint
    component: str | None
    action: FaultAction
    match_ordinal: int
    hit_ordinal: int
    safe_fault_code: InjectedFaultCode | None
    timestamp: datetime

    def __post_init__(self) -> None:
        _require_safe_token(self.plan_id, "plan_id", required=True)
        _require_safe_token(self.rule_id, "rule_id", required=True)
        _require_safe_token(self.component, "component")
        if not isinstance(self.fault_point, FaultPoint):
            raise TypeError("fault_point must be FaultPoint")
        if not isinstance(self.action, FaultAction):
            raise TypeError("action must be FaultAction")
        _require_positive_int(self.match_ordinal, "match_ordinal")
        _require_positive_int(self.hit_ordinal, "hit_ordinal")
        if self.safe_fault_code is not None and not isinstance(
            self.safe_fault_code, InjectedFaultCode
        ):
            raise TypeError("safe_fault_code must be InjectedFaultCode")
        _require_utc(self.timestamp, "timestamp")


@dataclass(frozen=True, slots=True)
class FaultRecorderSnapshot:
    records: tuple[FaultInjectionRecord, ...]
    capacity: int
    overflow_policy: RecorderOverflowPolicy
    dropped_count: int
    rejected_count: int
    closed: bool


class FaultInjectionRecorder:
    """Thread-safe recorder that never writes RuntimeEvent or a journal."""

    def __init__(
        self,
        *,
        capacity: int = 128,
        overflow_policy: RecorderOverflowPolicy = RecorderOverflowPolicy.DROP_OLDEST,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if not isinstance(overflow_policy, RecorderOverflowPolicy):
            raise TypeError("overflow_policy must be RecorderOverflowPolicy")
        self._capacity = capacity
        self._overflow_policy = overflow_policy
        self._records: deque[FaultInjectionRecord] = deque()
        self._dropped_count = 0
        self._rejected_count = 0
        self._closed = False
        self._lock = threading.Lock()

    def record(
        self,
        *,
        plan_id: str,
        component: str | None,
        decision: FaultDecision,
    ) -> bool:
        if not isinstance(decision, FaultDecision):
            raise TypeError("decision must be FaultDecision")
        if not decision.matched:
            return False
        record = FaultInjectionRecord(
            plan_id=plan_id,
            rule_id=decision.rule_id,
            fault_point=decision.fault_point,
            component=component,
            action=decision.action,
            match_ordinal=decision.match_ordinal,
            hit_ordinal=decision.hit_ordinal,
            safe_fault_code=decision.safe_fault_code,
            timestamp=decision.triggered_at,
        )
        with self._lock:
            if self._closed:
                return False
            if len(self._records) >= self._capacity:
                if self._overflow_policy is RecorderOverflowPolicy.REJECT_NEW:
                    self._rejected_count += 1
                    return False
                self._records.popleft()
                self._dropped_count += 1
            self._records.append(record)
            return True

    def snapshot(self) -> FaultRecorderSnapshot:
        with self._lock:
            return FaultRecorderSnapshot(
                records=tuple(self._records),
                capacity=self._capacity,
                overflow_policy=self._overflow_policy,
                dropped_count=self._dropped_count,
                rejected_count=self._rejected_count,
                closed=self._closed,
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def __repr__(self) -> str:
        snapshot = self.snapshot()
        return (
            "FaultInjectionRecorder("
            f"capacity={snapshot.capacity}, "
            f"size={len(snapshot.records)}, "
            f"overflow_policy={snapshot.overflow_policy.value}, "
            f"dropped_count={snapshot.dropped_count}, "
            f"rejected_count={snapshot.rejected_count}, "
            f"closed={snapshot.closed}"
            ")"
        )


__all__ = [
    "FaultInjectionRecord",
    "FaultInjectionRecorder",
    "FaultRecorderSnapshot",
    "RecorderOverflowPolicy",
]
