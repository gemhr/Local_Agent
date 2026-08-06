#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""前端可解释的多 Agent 状态文案模型（WP5）。

纯解析/文案模块，不依赖 Qt。前端只调用 ``format_frontend_status``，
不在客户端用字符串拼装所有状态；未知错误文案绝不鼓励立即重试。
"""

from __future__ import annotations

from typing import Any


AGENT_DISPLAY_NAMES = {
    "core_router": "核心 Agent",
    "knowledge_expert": "知识专家",
    "code_expert": "代码专家",
    "data_analyst": "数据分析专家",
    "synthesis_agent": "综合专家",
}

# 稳定错误码 -> 用户可见安全文案（不显示文件路径/raw response/异常正文）。
SAFE_ERROR_TEXT: dict[str, str] = {
    "FINAL_OUTPUT_DELIVERY_FAILED": "最终回答未能进入消息通道。",
    "FINAL_OUTPUT_DELIVERY_UNKNOWN": (
        "最终回答的交付状态无法确认。请先检查当前对话，避免重复执行。"
    ),
    "FINAL_OUTPUT_MEMORY_COMMIT_FAILED": (
        "回答已经交付，但未能保存到对话记忆。"
    ),
    "PLANNING_FAILED": "规划阶段失败，未生成执行计划。",
    "INVALID_CAPABILITY": "规划结果包含未支持的专家能力，请换一种说法再试。",
    "PLANNING_MODEL_FAILED": "规划模型调用失败，请重试或换个说法。",
    "PLANNER_SCHEMA_INVALID": "规划结果格式不被支持，请换个说法再试。",
    "AGENT_STEP_FAILED": "专家步骤执行失败。",
    "SYNTHESIS_FAILED": "结果综合失败。",
    "REQUIRED_DEPENDENCY_FAILED": "必需的依赖结果失败，运行停止。",
    "OUTPUT_GATE_DUPLICATE_ATTEMPT": "最终回答的发布被拒绝（重复尝试）。",
    "STEP_COMPLETION_EVENT_FAILED": "步骤完成事件未能发布。",
    "RUNTIME_TERMINAL_PUBLICATION_FAILED": "运行收尾事件未能发布。",
}


def agent_display_name(agent_id: str) -> str:
    return AGENT_DISPLAY_NAMES.get(agent_id, agent_id or "专属 Agent")


def safe_error_text(error_code: str | None) -> str:
    if not error_code:
        return "运行失败。"
    return SAFE_ERROR_TEXT.get(error_code, "运行失败。")


def _agent_id_from_event(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    agent_id = payload.get("agent_id") or event.get("agent_id") or ""
    return str(agent_id)


def format_frontend_status(event: dict[str, Any]) -> str | None:
    """将后端 control event（runtime 或 legacy）格式化为中文状态文案。

    OUTPUT_DELTA 永不进入状态组件；未知 event 返回 None（安全忽略）。
    """
    if not isinstance(event, dict):
        return None
    runtime_type = event.get("event_type")
    legacy_type = event.get("type")
    payload = event.get("payload") or {}

    if runtime_type:
        return _format_runtime_status(
            str(runtime_type), payload, event
        )
    if legacy_type:
        return _format_legacy_status(str(legacy_type), event)
    return None


def _format_runtime_status(
    event_type: str,
    payload: dict[str, Any],
    event: dict[str, Any],
) -> str | None:
    if event_type == "RUN_STARTED":
        return "运行已启动。"
    if event_type == "PLANNING_STARTED":
        return "正在规划…"
    if event_type == "PLAN_CREATED":
        return "已生成执行计划。"
    if event_type == "STEP_STARTED":
        name = agent_display_name(str(payload.get("agent_id") or ""))
        return f"{name} 执行中…"
    if event_type == "STEP_COMPLETED":
        name = agent_display_name(str(payload.get("agent_id") or ""))
        return f"{name} 已完成。"
    if event_type == "RUN_COMPLETED":
        return _format_run_completed(payload)
    if event_type in {
        "MODEL_STARTED",
        "MODEL_COMPLETED",
        "TOOL_STARTED",
        "TOOL_COMPLETED",
        "RETRIEVAL_STARTED",
        "RETRIEVAL_STAGE_COMPLETED",
        "RETRIEVAL_COMPLETED",
        "CANCELLATION",
        "TIMEOUT",
        "BUDGET_EXHAUSTED",
    }:
        return None
    return None


def _format_run_completed(payload: dict[str, Any]) -> str | None:
    memory_status = payload.get("memory_commit_status")
    delivery_status = payload.get("delivery_status")
    status = payload.get("status")
    if memory_status == "FAILED":
        return "回答已交付，记忆保存失败。"
    if delivery_status == "OUTCOME_UNKNOWN":
        return (
            "回答交付状态不确定，请先检查当前消息，避免重复执行。"
        )
    if status == "SUCCEEDED":
        return "回答已交付。"
    if status == "CANCELLED":
        return "运行已取消。"
    if status == "FAILED":
        error_code = payload.get("safe_error_code")
        return f"运行失败：{safe_error_text(str(error_code) if error_code else None)}"
    return None


def _format_legacy_status(event_type: str, event: dict[str, Any]) -> str | None:
    if event_type == "planning_started":
        return "核心 Agent 正在判断是否需要委派专属 Agent。"
    if event_type == "planning_skipped":
        return "本轮无需委派，核心 Agent 直接回答。"
    if event_type == "delegates_selected":
        agents = event.get("agents", [])
        names = "、".join(
            item.get("agent_name", item.get("agent_id", ""))
            for item in agents
            if item
        )
        return f"已选择协作智能体：{names}" if names else "已生成协作计划。"
    if event_type == "delegate_started":
        agent_name = event.get(
            "agent_name", event.get("agent_id", "专属 Agent")
        )
        task = event.get("task", "")
        return (
            f"{agent_name} 开始处理：{task}"
            if task
            else f"{agent_name} 开始处理子任务。"
        )
    if event_type == "delegate_finished":
        agent_name = event.get(
            "agent_name", event.get("agent_id", "专属 Agent")
        )
        summary = event.get("summary", "")
        return (
            f"{agent_name} 已返回结果：{summary}"
            if summary
            else f"{agent_name} 已完成子任务。"
        )
    if event_type == "synthesis_started":
        return "核心 Agent 正在汇总专属 Agent 的结果。"
    return None


__all__ = [
    "AGENT_DISPLAY_NAMES",
    "SAFE_ERROR_TEXT",
    "agent_display_name",
    "format_frontend_status",
    "safe_error_text",
]
