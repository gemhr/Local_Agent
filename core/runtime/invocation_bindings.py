#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run-scoped raw invocation 数据边界；不得持久化或日志化。"""

from __future__ import annotations

from enum import Enum
import threading
from typing import Iterable


class InvocationBindingErrorCode(str, Enum):
    UNKNOWN_STEP = "UNKNOWN_STEP"
    AGENT_MISMATCH = "AGENT_MISMATCH"
    BINDINGS_CLOSED = "BINDINGS_CLOSED"
    DUPLICATE_STEP = "DUPLICATE_STEP"
    INVALID_BINDING = "INVALID_BINDING"


class InvocationBindingError(LookupError):
    def __init__(self, error_code: InvocationBindingErrorCode, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


class AgentInvocationSpec:
    """Raw instruction spec；刻意不是 dataclass，避免默认 asdict 泄漏。"""

    __slots__ = ("_step_id", "_agent_id", "_instruction", "_input_type", "_locked")

    def __init__(
        self,
        step_id: str,
        agent_id: str,
        instruction: str,
        input_type: str = "text",
    ) -> None:
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError("step_id 不能为空")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id 不能为空")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction 不能为空")
        if not isinstance(input_type, str) or not input_type.strip():
            raise ValueError("input_type 不能为空")
        object.__setattr__(self, "_step_id", step_id.strip())
        object.__setattr__(self, "_agent_id", agent_id.strip())
        object.__setattr__(self, "_instruction", instruction)
        object.__setattr__(self, "_input_type", input_type.strip())
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("AgentInvocationSpec 是不可变对象")
        object.__setattr__(self, name, value)

    @property
    def step_id(self) -> str:
        return self._step_id

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def instruction(self) -> str:
        return self._instruction

    @property
    def input_type(self) -> str:
        return self._input_type

    def __repr__(self) -> str:
        return (
            "AgentInvocationSpec("
            f"step_id={self.step_id!r}, agent_id={self.agent_id!r}, "
            f"instruction=<redacted>, input_type={self.input_type!r})"
        )


class StepInvocationBindings:
    """构造后只读；仅按 Step 和预期 Agent 解析单条 Binding。"""

    __slots__ = ("_bindings", "_closed", "_lock", "_step_ids")

    def __init__(self, bindings: Iterable[AgentInvocationSpec]) -> None:
        mapping: dict[str, AgentInvocationSpec] = {}
        for binding in bindings:
            if not isinstance(binding, AgentInvocationSpec):
                raise InvocationBindingError(
                    InvocationBindingErrorCode.INVALID_BINDING,
                    "Bindings 只能包含合法 AgentInvocationSpec",
                )
            if binding.step_id in mapping:
                raise InvocationBindingError(
                    InvocationBindingErrorCode.DUPLICATE_STEP,
                    "Binding step_id 不允许重复",
                )
            mapping[binding.step_id] = binding
        if not mapping:
            raise InvocationBindingError(
                InvocationBindingErrorCode.INVALID_BINDING,
                "Bindings 至少需要一个条目",
            )
        self._bindings = mapping
        self._step_ids = tuple(mapping)
        self._closed = False
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"StepInvocationBindings(count={len(self._step_ids)}, closed={self.closed})"

    def __getstate__(self):
        raise TypeError("StepInvocationBindings 不允许序列化")

    @property
    def step_ids(self) -> tuple[str, ...]:
        return self._step_ids

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def resolve_for_step(
        self,
        step_id: str,
        *,
        expected_agent_id: str | None = None,
    ) -> AgentInvocationSpec:
        with self._lock:
            if self._closed:
                raise InvocationBindingError(
                    InvocationBindingErrorCode.BINDINGS_CLOSED,
                    "Bindings 已关闭",
                )
            binding = self._bindings.get(step_id)
            if binding is None:
                raise InvocationBindingError(
                    InvocationBindingErrorCode.UNKNOWN_STEP,
                    "Step 没有调用 Binding",
                )
            if expected_agent_id is not None and binding.agent_id != expected_agent_id:
                raise InvocationBindingError(
                    InvocationBindingErrorCode.AGENT_MISMATCH,
                    "Binding Agent 与预期不一致",
                )
            return binding

    def close_and_clear(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._bindings.clear()


__all__ = [
    "AgentInvocationSpec",
    "InvocationBindingError",
    "InvocationBindingErrorCode",
    "StepInvocationBindings",
]
