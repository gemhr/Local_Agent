from __future__ import annotations

from core.runtime.multi_agent_status import (
    format_frontend_status,
    safe_error_text,
)


def test_runtime_status_texts():
    assert (
        format_frontend_status({"event_type": "PLANNING_STARTED"})
        == "正在规划…"
    )
    assert (
        format_frontend_status({"event_type": "PLAN_CREATED"})
        == "已生成执行计划。"
    )
    assert (
        format_frontend_status(
            {
                "event_type": "STEP_STARTED",
                "payload": {"agent_id": "knowledge_expert"},
            }
        )
        == "知识专家 执行中…"
    )
    assert (
        format_frontend_status(
            {
                "event_type": "STEP_STARTED",
                "payload": {"agent_id": "code_expert"},
            }
        )
        == "代码专家 执行中…"
    )


def test_run_completed_layered_status_texts():
    delivered_memory_failed = format_frontend_status(
        {
            "event_type": "RUN_COMPLETED",
            "payload": {
                "status": "FAILED",
                "delivery_status": "DELIVERED",
                "memory_commit_status": "FAILED",
                "safe_error_code": "FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
            },
        }
    )
    assert delivered_memory_failed == "回答已交付，记忆保存失败。"

    unknown = format_frontend_status(
        {
            "event_type": "RUN_COMPLETED",
            "payload": {
                "status": "FAILED",
                "delivery_status": "OUTCOME_UNKNOWN",
                "memory_commit_status": "NOT_ATTEMPTED",
                "safe_error_code": "FINAL_OUTPUT_DELIVERY_UNKNOWN",
            },
        }
    )
    assert "交付状态不确定" in unknown
    assert "避免重复执行" in unknown
    assert "重试" not in unknown

    succeeded = format_frontend_status(
        {
            "event_type": "RUN_COMPLETED",
            "payload": {"status": "SUCCEEDED"},
        }
    )
    assert succeeded == "回答已交付。"


def test_unknown_error_text_never_encourages_retry():
    text = safe_error_text("UNKNOWN_INTERNAL_CODE")
    assert text == "运行失败。"
    assert "重试" not in text
    assert (
        safe_error_text("FINAL_OUTPUT_DELIVERY_FAILED")
        == "最终回答未能进入消息通道。"
    )
    assert (
        safe_error_text("FINAL_OUTPUT_DELIVERY_UNKNOWN")
        == "最终回答的交付状态无法确认。请先检查当前对话，避免重复执行。"
    )
    assert (
        safe_error_text("FINAL_OUTPUT_MEMORY_COMMIT_FAILED")
        == "回答已经交付，但未能保存到对话记忆。"
    )


def test_planning_error_texts_are_explicit():
    """planning 失败无副作用，文案明确可换说法重试。"""
    assert (
        safe_error_text("INVALID_CAPABILITY")
        == "规划结果包含未支持的专家能力，请换一种说法再试。"
    )
    assert (
        safe_error_text("PLANNING_MODEL_FAILED")
        == "规划模型调用失败，请重试或换个说法。"
    )
    assert (
        safe_error_text("PLANNER_SCHEMA_INVALID")
        == "规划结果格式不被支持，请换个说法再试。"
    )
    rendered = format_frontend_status(
        {
            "event_type": "RUN_COMPLETED",
            "payload": {
                "status": "FAILED",
                "safe_error_code": "INVALID_CAPABILITY",
            },
        }
    )
    assert (
        rendered
        == "运行失败：规划结果包含未支持的专家能力，请换一种说法再试。"
    )


def test_legacy_events_remain_compatible():
    assert (
        format_frontend_status({"type": "planning_started"})
        == "核心 Agent 正在判断是否需要委派专属 Agent。"
    )
    assert (
        format_frontend_status({"type": "delegate_started", "agent_id": "k"})
        == "k 开始处理子任务。"
    )
    assert (
        format_frontend_status({"type": "synthesis_started"})
        == "核心 Agent 正在汇总专属 Agent 的结果。"
    )


def test_output_delta_and_unknown_events_never_enter_status():
    assert (
        format_frontend_status(
            {"event_type": "OUTPUT_DELTA", "payload": {"text": "hello"}}
        )
        is None
    )
    assert (
        format_frontend_status({"event_type": "MODEL_COMPLETED"}) is None
    )
    assert format_frontend_status({"event_type": "UNKNOWN_EVENT"}) is None
    assert format_frontend_status(None) is None
    assert format_frontend_status("not-a-dict") is None
