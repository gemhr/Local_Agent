#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""局部操作 Timeout 的计算；不会擅自取消整个 Run。"""

from __future__ import annotations

from enum import Enum

from core.runtime.context import RunContext


class OperationType(str, Enum):
    STEP = "STEP"
    MODEL = "MODEL"
    TOOL = "TOOL"
    RAG = "RAG"
    APPROVAL = "APPROVAL"


class OperationTimeoutError(TimeoutError):
    """局部操作先于 Run deadline 超时。"""

    def __init__(self, operation_type: OperationType) -> None:
        self.operation_type = operation_type
        super().__init__("局部操作超时")


def effective_timeout_seconds(run_context: RunContext, local_timeout_seconds: float | None) -> float | None:
    """取父 Run 剩余时间与局部 Timeout 的较早者。"""
    if local_timeout_seconds is not None and local_timeout_seconds <= 0:
        raise ValueError("local_timeout_seconds must be positive")
    parent = run_context.remaining_seconds()
    if parent is None:
        return local_timeout_seconds
    return min(parent, local_timeout_seconds) if local_timeout_seconds is not None else parent
