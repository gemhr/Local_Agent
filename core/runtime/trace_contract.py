#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage 2.5 Trace Contract v1 冻结的 Span 命名与安全属性常量。

该模块只承载命名与属性边界，不创建与已有 Event/Trace/Reducer/Coordinator
竞争的第二套 owner。Span 的实际创建仍由各运行 owner（RunCoordinator、
ParallelExecutor、OutputGate、RunFinalMemoryWriter）调用 tracing 原语完成。
"""

from __future__ import annotations

from typing import Any


RUNTIME_TRACE_CONTRACT_VERSION = 1

# Span operation 稳定命名（Trace Contract v1）。
RUNTIME_RUN_SPAN = "runtime.run"
RUNTIME_PLANNING_SPAN = "runtime.planning"
RUNTIME_STEP_SPAN = "runtime.step"
RUNTIME_SYNTHESIS_SPAN = "runtime.synthesis"
RUNTIME_OUTPUT_DELIVERY_SPAN = "runtime.output_delivery"
RUNTIME_FINAL_MEMORY_COMMIT_SPAN = "runtime.final_memory_commit"

# 版本归因字段：可安全记录，缺失时使用 not_configured/unknown。
RUN_ATTRIBUTE_KEYS = frozenset(
    {
        "plan_id",
        "plan_version",
        "plan_fingerprint",
        "planning_source",
        "step_count",
        "selected_entry_agent_id",
        "runtime_mode",
        "runtime_version",
        "prompt_version",
        "model_config_hash",
        "toolset_hash",
        "kb_version",
        "final_status",
        "stop_reason",
        "session_id",
    }
)
PLANNING_ATTRIBUTE_KEYS = frozenset(
    {
        "planning_source",
        "schema_version",
        "planner_model_invoked",
        "planner_attempt_count",
        "planner_timeout_source",
        "compiled_shape",
        "specialist_count",
        "synthesis_required",
    }
)
STEP_ATTRIBUTE_KEYS = frozenset(
    {
        "preferred_agent",
        "execution_kind",
        "output_policy",
        "invocation_role",
        "dependency_count",
        "content_type",
        "result_char_count",
        "state",
    }
)
DELIVERY_ATTRIBUTE_KEYS = frozenset(
    {
        "final_step_id",
        "output_policy",
        "delivery_status",
        "gate_terminal_state",
        "publish_attempt_count",
        "partially_persisted",
        "output_char_count",
    }
)
MEMORY_ATTRIBUTE_KEYS = frozenset(
    {
        "persist_enabled",
        "entry_agent_id",
        "memory_scope",
        "delivery_status",
        "user_write_status",
        "assistant_write_status",
        "transaction_used",
    }
)


def set_span_attributes(handle: Any, **attributes: Any) -> None:
    """批量设置安全 Span 属性；None 值跳过，非法属性隔离不抛异常。

    Span 记录是旁路观测，任何单条属性失败都不允许改变 Runtime 行为。
    """
    if handle is None or getattr(handle, "context", None) is None:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            handle.set_safe_attribute(key, value)
        except Exception:
            continue


def not_configured_version() -> str:
    """未配置版本字段的显式占位；禁止虚构版本号。"""
    return "not_configured"


__all__ = [
    "DELIVERY_ATTRIBUTE_KEYS",
    "MEMORY_ATTRIBUTE_KEYS",
    "PLANNING_ATTRIBUTE_KEYS",
    "RUN_ATTRIBUTE_KEYS",
    "RUNTIME_FINAL_MEMORY_COMMIT_SPAN",
    "RUNTIME_OUTPUT_DELIVERY_SPAN",
    "RUNTIME_PLANNING_SPAN",
    "RUNTIME_RUN_SPAN",
    "RUNTIME_STEP_SPAN",
    "RUNTIME_SYNTHESIS_SPAN",
    "RUNTIME_TRACE_CONTRACT_VERSION",
    "STEP_ATTRIBUTE_KEYS",
    "not_configured_version",
    "set_span_attributes",
]
