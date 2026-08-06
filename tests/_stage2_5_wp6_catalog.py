"""WP6 Fault Injection Catalog v2 authoritative data source.

Every count in the WP6 result document and the RC Gate test is derived from
this module so documentation cannot drift from the tested catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.runtime import (
    FaultPoint,
    FaultPointSupportStatus,
)


@dataclass(frozen=True, slots=True)
class Stage25FaultCatalogEntry:
    catalog_id: str
    name: str
    category: str
    fault_point: FaultPoint | None
    support_status: FaultPointSupportStatus
    required: bool
    test_ids: tuple[str, ...]
    expected_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class Stage25RCScenario:
    scenario_id: str
    name: str
    category: str
    test_id: str


def _e(catalog_id, name, category, fault_point, status, required, test_ids, error=None):
    return Stage25FaultCatalogEntry(
        catalog_id=catalog_id,
        name=name,
        category=category,
        fault_point=fault_point,
        support_status=status,
        required=required,
        test_ids=test_ids,
        expected_error_code=error,
    )


S = FaultPointSupportStatus.SUPPORTED
C = FaultPointSupportStatus.CONTRACT_ONLY
N = FaultPointSupportStatus.NOT_APPLICABLE


STAGE25_FAULT_CATALOG = (
    # 4.1 Planning
    _e("FP-PLAN-01", "Resolver deterministic rule 失败", "PLANNING", FaultPoint.PLANNING_BEFORE_RESOLVE, S, True, ("stage2_5_wp6_planning_faults",), "PLANNING_MODEL_FAILED"),
    _e("FP-PLAN-02", "Planning model 普通失败", "PLANNING", FaultPoint.PLANNING_BEFORE_RESOLVE, S, True, ("stage2_5_wp6_planning_faults",), "PLANNING_MODEL_FAILED"),
    _e("FP-PLAN-03", "Planner 独立 timeout", "PLANNING", None, C, False, ("test_dynamic_planning_lifecycle",), "PLANNER_TIMEOUT"),
    _e("FP-PLAN-04", "Run deadline", "PLANNING", None, C, False, ("test_dynamic_planning_lifecycle",), "DEADLINE_EXCEEDED"),
    _e("FP-PLAN-05", "budget exhausted", "PLANNING", None, C, False, ("test_dynamic_planning_lifecycle",), "BUDGET_EXHAUSTED"),
    _e("FP-PLAN-06", "malformed schema", "PLANNING", None, C, False, ("test_multi_agent_planning",), "PLANNER_SCHEMA_INVALID"),
    _e("FP-PLAN-07", "forbidden field", "PLANNING", None, C, False, ("test_multi_agent_planning",), "PLANNER_FIELD_FORBIDDEN"),
    _e("FP-PLAN-08", "unknown/disabled Agent", "PLANNING", None, C, False, ("test_multi_agent_planning",), "UNKNOWN_AGENT"),
    _e("FP-PLAN-09", "Compiler 失败", "PLANNING", None, C, False, ("test_multi_agent_planning",), "COMPILE_*"),
    _e("FP-PLAN-10", "PLAN_CREATED journal append 失败", "PLANNING", FaultPoint.PLANNING_BEFORE_PLAN_CREATED, S, True, ("stage2_5_wp6_planning_faults",), "COORDINATOR_INFRASTRUCTURE_ERROR"),
    _e("FP-PLAN-11", "POST_PLAN checkpoint 失败", "PLANNING", FaultPoint.SNAPSHOT_BEFORE_SAVE, S, True, ("stage2_5_wp6_planning_faults",), "POST_PLAN_PRE_EXECUTION_CHECKPOINT_FAILED"),
    _e("FP-PLAN-12", "cancel during planning", "PLANNING", None, C, False, ("test_dynamic_planning_lifecycle", "stage2_5_wp6_planning_faults"), "REQUEST_CANCELLED"),
    _e("FP-PLAN-13", "client disconnect during planning", "PLANNING", None, C, False, ("stage2_5_wp6_planning_faults",), "CLIENT_DISCONNECTED"),
    _e("FP-PLAN-14", "shutdown during planning", "PLANNING", None, C, False, ("stage2_5_wp6_planning_faults",), "SERVER_SHUTDOWN"),
    # 4.2 Specialist 执行
    _e("FP-SPEC-01", "单 specialist 模型失败", "SPECIALIST", FaultPoint.STEP_BEFORE_DRIVER_EXECUTE, S, True, ("stage2_5_wp6_execution_faults",), "REQUIRED_DEPENDENCY_FAILED"),
    _e("FP-SPEC-02", "Shape 3 一个 specialist 失败", "SPECIALIST", FaultPoint.STEP_BEFORE_DRIVER_EXECUTE, S, True, ("stage2_5_wp6_execution_faults",), "REQUIRED_DEPENDENCY_FAILED"),
    _e("FP-SPEC-03", "一个 specialist timeout", "SPECIALIST", None, C, False, ("test_multi_agent_execution",), "DEADLINE_EXCEEDED"),
    _e("FP-SPEC-04", "一个 specialist cancellation", "SPECIALIST", None, C, False, ("test_multi_agent_execution",), "STEP_CANCELLED"),
    _e("FP-SPEC-05", "Tool 失败", "SPECIALIST", FaultPoint.TOOL_BEFORE_INVOCATION, S, True, ("tool_fault_injection",), "TOOL_EXECUTION_FAILED"),
    _e("FP-SPEC-06", "Retrieval 失败", "SPECIALIST", FaultPoint.RETRIEVAL_BEFORE_SEARCH, S, True, ("retrieval_fault_injection",), "RETRIEVAL_FAILED"),
    _e("FP-SPEC-07", "adapter identity mismatch", "SPECIALIST", FaultPoint.STEP_BEFORE_DRIVER_EXECUTE, S, True, ("stage2_5_wp6_execution_faults",), "AGENT_STEP_FAILED"),
    _e("FP-SPEC-08", "Binding mismatch", "SPECIALIST", None, C, False, ("stage2_5_wp6_execution_faults",), "AGENT_STEP_FAILED"),
    _e("FP-SPEC-09", "result 非法", "SPECIALIST", None, C, False, ("stage2_5_wp6_execution_faults",), "STEP_RESULT_INVALID"),
    _e("FP-SPEC-10", "result 过大", "SPECIALIST", None, C, False, ("test_multi_agent_execution",), "STEP_RESULT_PREPARE_FAILED"),
    _e("FP-SPEC-11", "duplicate result", "SPECIALIST", None, C, False, ("stage2_5_wp6_execution_faults",), "STEP_RESULT_DUPLICATE_COMMIT"),
    _e("FP-SPEC-12", "late result", "SPECIALIST", None, C, False, ("stage2_5_wp6_execution_faults",), "STEP_RESULT_LATE_COMMIT"),
    _e("FP-SPEC-13", "bounded executor 提交失败", "SPECIALIST", FaultPoint.EXECUTOR_BEFORE_SUBMIT, S, True, ("stage2_5_wp6_execution_faults", "stage2_5_wp6_starvation"), "REQUIRED_DEPENDENCY_FAILED"),
    _e("FP-SPEC-14", "detached worker 在 Run 终态后返回", "SPECIALIST", None, C, False, ("stage2_5_wp6_execution_faults",), "STEP_RESULT_LATE_COMMIT"),
    # 4.3 Store / completion
    _e("FP-STORE-01", "write_prepared 失败", "STORE", FaultPoint.STORE_BEFORE_WRITE_PREPARED, S, True, ("stage2_5_wp6_execution_faults",), "STEP_RESULT_PREPARE_FAILED"),
    _e("FP-STORE-02", "Step 状态提交失败", "STORE", None, C, False, ("test_step_completion",), "STEP_STATE_COMMIT_FAILED"),
    _e("FP-STORE-03", "mark_readable 失败", "STORE", FaultPoint.STORE_BEFORE_MARK_READABLE, S, True, ("stage2_5_wp6_execution_faults",), "STEP_RESULT_COMMIT_FAILED"),
    _e("FP-STORE-04", "STEP_COMPLETED journal 失败", "STORE", FaultPoint.EVENT_BEFORE_JOURNAL_APPEND, S, True, ("stage2_5_wp6_execution_faults",), "STEP_COMPLETION_EVENT_FAILED"),
    _e("FP-STORE-05", "STEP_COMPLETED enqueue 部分持久化", "STORE", FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE, S, True, ("test_partial_publication_sequence",), "STEP_COMPLETION_EVENT_FAILED"),
    _e("FP-STORE-06", "completion 重复回调", "STORE", None, C, False, ("test_step_completion", "stage2_5_wp6_execution_faults"), "STEP_RESULT_DUPLICATE_COMMIT"),
    _e("FP-STORE-07", "Store seal 后写入", "STORE", None, C, False, ("test_step_result_store",), "STEP_RESULT_LATE_COMMIT"),
    _e("FP-STORE-08", "Store clear 后访问", "STORE", None, C, False, ("test_step_result_store",), "STEP_RESULT_LATE_COMMIT"),
    _e("FP-STORE-09", "ACL 拒绝无依赖读取", "STORE", None, C, False, ("test_step_result_store",), "READ_NOT_ALLOWED"),
    _e("FP-STORE-10", "synthesis 读取 PREPARED 结果", "STORE", FaultPoint.STORE_BEFORE_DEPENDENCY_READ, S, True, ("stage2_5_wp6_execution_faults",), "SYNTHESIS_FAILED"),
    _e("FP-STORE-11", "producer SUCCEEDED 但 result 不可读", "STORE", FaultPoint.STORE_BEFORE_MARK_READABLE, S, True, ("stage2_5_wp6_execution_faults",), "STEP_RESULT_COMMIT_FAILED"),
    # 4.4 Synthesis
    _e("FP-SYNTH-01", "缺少 required dependency", "SYNTHESIS", None, C, False, ("test_synthesis_adapter",), "SYNTHESIS_FAILED"),
    _e("FP-SYNTH-02", "dependency 失败", "SYNTHESIS", None, C, False, ("test_multi_agent_execution",), "REQUIRED_DEPENDENCY_FAILED"),
    _e("FP-SYNTH-03", "dependency cancelled", "SYNTHESIS", None, C, False, ("test_multi_agent_execution",), "REQUIRED_DEPENDENCY_FAILED"),
    _e("FP-SYNTH-04", "dependency result 不完整", "SYNTHESIS", None, C, False, ("test_synthesis_adapter",), "SYNTHESIS_FAILED"),
    _e("FP-SYNTH-05", "synthesis 模型失败", "SYNTHESIS", FaultPoint.STEP_BEFORE_DRIVER_EXECUTE, S, True, ("stage2_5_wp6_execution_faults",), "SYNTHESIS_FAILED"),
    _e("FP-SYNTH-06", "synthesis timeout", "SYNTHESIS", None, C, False, ("stage2_5_wp6_execution_faults",), "DEADLINE_EXCEEDED"),
    _e("FP-SYNTH-07", "synthesis result 非法", "SYNTHESIS", None, C, False, ("test_synthesis_adapter",), "SYNTHESIS_FAILED"),
    _e("FP-SYNTH-08", "synthesis Memory/history 隔离", "SYNTHESIS", None, C, False, ("test_wp3_history_boundary",), None),
    _e("FP-SYNTH-09", "synthesis 不得 fallback", "SYNTHESIS", None, C, False, ("test_multi_agent_execution",), "SYNTHESIS_FAILED"),
    _e("FP-SYNTH-10", "synthesis 恰好一次", "SYNTHESIS", None, C, False, ("stage2_5_wp6_e2e",), None),
    # 4.5 OutputGate
    _e("FP-OUT-01", "journal append 前失败", "OUTPUT", FaultPoint.OUTPUT_BEFORE_PUBLISH, S, True, ("stage2_5_wp6_delivery_faults",), "FINAL_OUTPUT_DELIVERY_FAILED"),
    _e("FP-OUT-02", "journal 成功 enqueue 失败", "OUTPUT", FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE, S, True, ("test_partial_publication_sequence",), "FINAL_OUTPUT_DELIVERY_UNKNOWN"),
    _e("FP-OUT-03", "concurrent duplicate", "OUTPUT", None, C, False, ("test_output_gate",), "OUTPUT_GATE_DUPLICATE_ATTEMPT"),
    _e("FP-OUT-04", "PUBLISHED 后重复", "OUTPUT", None, C, False, ("test_output_gate", "stage2_5_wp6_delivery_faults"), "OUTPUT_GATE_DUPLICATE_ATTEMPT"),
    _e("FP-OUT-05", "FAILED 后重复", "OUTPUT", None, C, False, ("test_output_gate",), "OUTPUT_GATE_DUPLICATE_ATTEMPT"),
    _e("FP-OUT-06", "UNKNOWN 后重复", "OUTPUT", None, C, False, ("test_output_gate",), "OUTPUT_GATE_DUPLICATE_ATTEMPT"),
    _e("FP-OUT-07", "INTERNAL 调用", "OUTPUT", None, C, False, ("test_output_gate",), "OUTPUT_GATE_INTERNAL_STEP"),
    _e("FP-OUT-08", "非唯一 final 调用", "OUTPUT", None, C, False, ("test_output_gate",), "OUTPUT_GATE_NOT_FINAL"),
    _e("FP-OUT-09", "Step 未 SUCCEEDED", "OUTPUT", None, C, False, ("test_output_gate",), "OUTPUT_GATE_STEP_NOT_SUCCEEDED"),
    _e("FP-OUT-10", "Store 未 READABLE", "OUTPUT", None, C, False, ("test_output_gate",), "OUTPUT_GATE_STORE_NOT_READABLE"),
    _e("FP-OUT-11", "Store 已 seal", "OUTPUT", None, C, False, ("test_output_gate",), "OUTPUT_GATE_STORE_SEALED"),
    _e("FP-OUT-12", "Run inactive", "OUTPUT", None, C, False, ("test_output_gate",), "OUTPUT_GATE_RUN_NOT_ACTIVE"),
    _e("FP-OUT-13", "STEP_COMPLETED 在 delivery 后失败", "OUTPUT", FaultPoint.EVENT_BEFORE_JOURNAL_APPEND, S, True, ("stage2_5_wp6_delivery_faults",), "STEP_COMPLETION_EVENT_FAILED"),
    _e("FP-OUT-14", "terminal event 在 delivery 后失败", "OUTPUT", FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND, S, True, ("stage2_5_wp6_delivery_faults",), "RUNTIME_TERMINAL_PUBLICATION_FAILED"),
    # 4.6 Memory
    _e("FP-MEM-01", "原子事务开始失败", "MEMORY", FaultPoint.MEMORY_BEFORE_EXCHANGE_BEGIN, S, True, ("stage2_5_wp6_memory_faults",), "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"),
    _e("FP-MEM-02", "user insert 失败", "MEMORY", FaultPoint.MEMORY_BEFORE_USER_INSERT, S, True, ("stage2_5_wp6_memory_faults",), "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"),
    _e("FP-MEM-03", "assistant insert 失败", "MEMORY", FaultPoint.MEMORY_BEFORE_ASSISTANT_INSERT, S, True, ("stage2_5_wp6_memory_faults",), "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"),
    _e("FP-MEM-04", "commit 失败", "MEMORY", FaultPoint.MEMORY_BEFORE_EXCHANGE_COMMIT, S, True, ("stage2_5_wp6_memory_faults",), "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"),
    _e("FP-MEM-05", "duplicate exchange", "MEMORY", None, C, False, ("test_final_memory_atomicity", "stage2_5_wp6_memory_faults"), "DUPLICATE_EXCHANGE"),
    _e("FP-MEM-06", "delivered 前禁止写", "MEMORY", None, C, False, ("test_final_memory_boundary",), None),
    _e("FP-MEM-07", "delivery failed 不写", "MEMORY", None, C, False, ("test_final_memory_boundary",), None),
    _e("FP-MEM-08", "delivery unknown 不写", "MEMORY", None, C, False, ("test_final_memory_boundary",), None),
    _e("FP-MEM-09", "specialist raw 不写", "MEMORY", None, C, False, ("test_final_memory_boundary", "test_wp5_security_matrix"), None),
    _e("FP-MEM-10", "writer 重复调用", "MEMORY", None, C, False, ("test_final_memory_atomicity", "stage2_5_wp6_memory_faults"), "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"),
    _e("FP-MEM-11", "writer 失败后再次调用", "MEMORY", None, C, False, ("stage2_5_wp6_memory_faults",), "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"),
    _e("FP-MEM-12", "Memory 成功但 terminal 失败", "MEMORY", FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND, S, True, ("stage2_5_wp6_memory_faults",), "RUNTIME_TERMINAL_PUBLICATION_FAILED"),
    _e("FP-MEM-13", "delivered 成功但 Memory 失败", "MEMORY", FaultPoint.MEMORY_BEFORE_EXCHANGE_BEGIN, S, True, ("stage2_5_wp6_memory_faults",), "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"),
    # 4.7 Trace / Journal / Metrics
    _e("FP-OBS-01", "Span 创建失败", "OBSERVABILITY", FaultPoint.TRACE_BEFORE_SPAN_START, S, True, ("trace_fault_injection",), None),
    _e("FP-OBS-02", "Span 结束失败", "OBSERVABILITY", FaultPoint.TRACE_BEFORE_SPAN_END, S, True, ("trace_lifecycle_fault",), None),
    _e("FP-OBS-03", "recorder 失败不能泄漏正文", "OBSERVABILITY", FaultPoint.OBSERVABILITY_BEFORE_RECORD, S, True, ("observability_fault_injection", "test_wp5_security_matrix"), None),
    _e("FP-OBS-04", "Journal sequence 重复/倒退", "OBSERVABILITY", None, C, False, ("test_recovery_tail_corruption", "test_recovery_validation"), None),
    _e("FP-OBS-05", "unknown event", "OBSERVABILITY", None, C, False, ("test_runtime_projection",), None),
    _e("FP-OBS-06", "safe payload 非法字段", "OBSERVABILITY", None, C, False, ("test_journal_safe_projection",), None),
    _e("FP-OBS-07", "OUTPUT 正文不得进入 Journal", "OBSERVABILITY", None, C, False, ("test_journal_safe_projection", "test_wp5_security_matrix"), None),
    _e("FP-OBS-08", "Metrics recorder 失败不得重复执行", "OBSERVABILITY", None, C, False, ("test_metrics_label_policy_wp5",), None),
    _e("FP-OBS-09", "高基数 label 拒绝/规范化", "OBSERVABILITY", None, C, False, ("test_metrics_label_policy_wp5",), None),
    _e("FP-OBS-10", "Trace attributes 敏感值拒绝", "OBSERVABILITY", None, C, False, ("test_trace_contract", "test_wp5_security_matrix"), None),
    _e("FP-OBS-11", "partial persisted 事实可诊断", "OBSERVABILITY", None, C, False, ("test_partial_publication_sequence",), None),
    # 4.8 Frontend / Projection
    _e("FP-FE-01", "duplicate event", "FRONTEND", None, C, False, ("test_runtime_projection",), None),
    _e("FP-FE-02", "sequence 倒退", "FRONTEND", None, C, False, ("test_runtime_projection",), None),
    _e("FP-FE-03", "未知 control event", "FRONTEND", None, C, False, ("test_runtime_projection",), None),
    _e("FP-FE-04", "并行 specialist active 状态", "FRONTEND", None, C, False, ("test_frontend_multi_agent_status",), None),
    _e("FP-FE-05", "synthesis 状态", "FRONTEND", None, C, False, ("test_frontend_multi_agent_status",), None),
    _e("FP-FE-06", "delivery failed/unknown 文案", "FRONTEND", None, C, False, ("test_frontend_multi_agent_status",), None),
    _e("FP-FE-07", "delivered + Memory failed", "FRONTEND", None, C, False, ("test_frontend_multi_agent_status", "test_stage2_5_wp6_security"), None),
    _e("FP-FE-08", "RUN_COMPLETED 前缺失 control event", "FRONTEND", None, C, False, ("test_stage2_5_wp6_frontend",), None),
    _e("FP-FE-09", "Legacy 事件兼容", "FRONTEND", None, C, False, ("test_frontend_multi_agent_status",), None),
    _e("FP-FE-10", "OUTPUT 只进入正文一次", "FRONTEND", None, C, False, ("test_frontend_multi_agent_status", "test_wp5_security_matrix"), None),
    _e("FP-FE-11", "unknown 文案不建议立即重试", "FRONTEND", None, C, False, ("test_frontend_multi_agent_status",), None),
    _e("FP-FE-12", "specialist raw 不显示", "FRONTEND", None, C, False, ("test_frontend_multi_agent_status", "test_wp5_security_matrix"), None),
    # 4.9 Recovery
    _e("FP-REC-01", "POST_PLAN_PRE_EXECUTION", "RECOVERY", None, C, False, ("test_recovery_delivery_boundary",), None),
    _e("FP-REC-02", "specialist 执行中断", "RECOVERY", None, C, False, ("test_recovery_delivery_boundary",), None),
    _e("FP-REC-03", "Store PREPARED 中断", "RECOVERY", None, C, False, ("stage2_5_wp6_recovery",), None),
    _e("FP-REC-04", "final Step SUCCEEDED 无 OUTPUT 事实", "RECOVERY", None, C, False, ("test_recovery_delivery_boundary",), None),
    _e("FP-REC-05", "OUTPUT journaled 无 terminal", "RECOVERY", None, C, False, ("test_recovery_delivery_boundary",), None),
    _e("FP-REC-06", "delivery unknown", "RECOVERY", None, C, False, ("test_recovery_delivery_boundary",), None),
    _e("FP-REC-07", "delivered 已知 Memory 未知", "RECOVERY", None, C, False, ("test_recovery_delivery_boundary",), None),
    _e("FP-REC-08", "Memory committed 无 terminal", "RECOVERY", None, C, False, ("test_recovery_delivery_boundary",), None),
    _e("FP-REC-09", "terminal 成功", "RECOVERY", None, C, False, ("test_recovery_delivery_boundary",), None),
    _e("FP-REC-10", "corrupt/duplicate/out-of-order Journal", "RECOVERY", None, C, False, ("test_recovery_tail_corruption",), None),
    _e("FP-REC-11", "v1/v2 Snapshot 兼容", "RECOVERY", None, C, False, ("test_recovery_version_compatibility",), None),
    _e("FP-REC-12", "fingerprint mismatch", "RECOVERY", None, C, False, ("test_recovery_version_compatibility",), None),
)


RC_SCENARIOS_25 = (
    Stage25RCScenario("RC25-S-01", "Shape 0 Core direct 成功主链", "SUCCESS", "tests/test_final_output_delivery.py::test_shape0_core_direct_uses_typed_pipeline_and_single_output"),
    Stage25RCScenario("RC25-S-02", "Shape 1 explicit code 成功主链", "SUCCESS", "tests/test_final_output_delivery.py::test_shape1_explicit_entry_specialist_single_output"),
    Stage25RCScenario("RC25-S-03", "Shape 1 explicit knowledge 成功主链", "SUCCESS", "tests/test_stage2_5_wp6_e2e.py::test_shape1_explicit_knowledge_entry_single_output"),
    Stage25RCScenario("RC25-S-04", "Shape 1 explicit data 成功主链", "SUCCESS", "tests/test_stage2_5_wp6_e2e.py::test_shape1_explicit_data_entry_single_output"),
    Stage25RCScenario("RC25-S-05", "Shape 1 delegated knowledge passthrough", "SUCCESS", "tests/test_final_output_delivery.py::test_shape1_delegated_knowledge_direct_single_output"),
    Stage25RCScenario("RC25-S-06", "Shape 2 code -> synthesis", "SUCCESS", "tests/test_final_output_delivery.py::test_shape2_single_specialist_plus_synthesis_single_output"),
    Stage25RCScenario("RC25-S-07", "Shape 2 data -> synthesis", "SUCCESS", "tests/test_stage2_5_wp6_e2e.py::test_shape2_data_analyst_plus_synthesis_single_output"),
    Stage25RCScenario("RC25-S-08", "Shape 3 knowledge + code", "SUCCESS", "tests/test_final_output_delivery.py::test_shape3_fanout_specialists_plus_synthesis_single_output"),
    Stage25RCScenario("RC25-S-09", "Shape 3 knowledge + data", "SUCCESS", "tests/test_stage2_5_wp6_e2e.py::test_shape3_knowledge_plus_data_single_output"),
    Stage25RCScenario("RC25-S-10", "Shape 3 code + data", "SUCCESS", "tests/test_stage2_5_wp6_e2e.py::test_shape3_code_plus_data_single_output"),
    Stage25RCScenario("RC25-S-11", "Shape 3 三 specialist", "SUCCESS", "tests/test_stage2_5_wp6_e2e.py::test_shape3_three_specialists_single_output"),
    Stage25RCScenario("RC25-P-01", "Planning malformed schema", "PLANNING_FAILURE", "tests/test_dynamic_planning_lifecycle.py::test_planning_failures_have_no_plan_checkpoint_step_or_raw_output"),
    Stage25RCScenario("RC25-P-02", "Planning unknown Agent", "PLANNING_FAILURE", "tests/test_multi_agent_planning.py::test_unknown_and_synthesis_selected_agents_fail_without_model_or_fallback"),
    Stage25RCScenario("RC25-P-03", "Planner timeout", "PLANNING_FAILURE", "tests/test_dynamic_planning_lifecycle.py::test_independent_planner_timeout_maps_to_planning_failed"),
    Stage25RCScenario("RC25-P-04", "budget exhausted", "PLANNING_FAILURE", "tests/test_dynamic_planning_lifecycle.py::test_planning_budget_exhaustion_keeps_existing_budget_mapping"),
    Stage25RCScenario("RC25-P-05", "cancel during planning", "PLANNING_FAILURE", "tests/test_dynamic_planning_lifecycle.py::test_user_cancellation_during_planning_is_not_planning_failed"),
    Stage25RCScenario("RC25-P-06", "deadline", "PLANNING_FAILURE", "tests/test_dynamic_planning_lifecycle.py::test_total_deadline_is_not_reclassified_as_planning_failure"),
    Stage25RCScenario("RC25-P-07", "model failure", "PLANNING_FAILURE", "tests/test_stage2_5_wp6_planning_faults.py::test_planning_resolve_injected_fault_is_planning_failed"),
    Stage25RCScenario("RC25-P-08", "PLAN_CREATED journal 失败", "PLANNING_FAILURE", "tests/test_stage2_5_wp6_planning_faults.py::test_plan_created_publication_failure_has_no_steps"),
    Stage25RCScenario("RC25-P-09", "POST_PLAN checkpoint 失败", "PLANNING_FAILURE", "tests/test_stage2_5_wp6_planning_faults.py::test_post_plan_checkpoint_failure_fails_closed"),
    Stage25RCScenario("RC25-P-10", "client disconnect during planning", "PLANNING_FAILURE", "tests/test_stage2_5_wp6_planning_faults.py::test_client_disconnect_during_planning_is_cancelled"),
    Stage25RCScenario("RC25-P-11", "shutdown during planning", "PLANNING_FAILURE", "tests/test_stage2_5_wp6_planning_faults.py::test_shutdown_during_planning_is_cancelled"),
    Stage25RCScenario("RC25-E-01", "一个 specialist 失败", "EXECUTION_FAILURE", "tests/test_multi_agent_execution.py::test_specialist_failure_blocks_synthesis_fail_closed"),
    Stage25RCScenario("RC25-E-02", "一个 specialist timeout", "EXECUTION_FAILURE", "tests/test_multi_agent_execution.py::test_specialist_deadline_blocks_synthesis"),
    Stage25RCScenario("RC25-E-03", "result 过大", "EXECUTION_FAILURE", "tests/test_multi_agent_execution.py::test_result_too_large_fails_prepare_closed"),
    Stage25RCScenario("RC25-E-04", "Store 提交失败", "EXECUTION_FAILURE", "tests/test_stage2_5_wp6_execution_faults.py::test_store_write_prepared_failure_fails_closed"),
    Stage25RCScenario("RC25-E-05", "dependency 不可读", "EXECUTION_FAILURE", "tests/test_stage2_5_wp6_execution_faults.py::test_dependency_read_failure_blocks_synthesis"),
    Stage25RCScenario("RC25-E-06", "synthesis 失败", "EXECUTION_FAILURE", "tests/test_multi_agent_execution.py::test_synthesis_failure_has_no_fallback"),
    Stage25RCScenario("RC25-D-01", "delivery known failed", "DELIVERY", "tests/test_partial_publication_sequence.py::test_pre_journal_failure_does_not_consume_sequence_and_fails_known"),
    Stage25RCScenario("RC25-D-02", "delivery outcome unknown", "DELIVERY", "tests/test_partial_publication_sequence.py::test_partial_persisted_output_consumes_sequence_and_fails_unknown"),
    Stage25RCScenario("RC25-D-03", "duplicate attempt", "DELIVERY", "tests/test_output_gate.py::test_duplicate_after_published_fails_closed"),
    Stage25RCScenario("RC25-D-04", "completion event 失败", "DELIVERY", "tests/test_step_completion_delivery.py::test_completion_event_failure_keeps_delivery_status_and_fails_run"),
    Stage25RCScenario("RC25-D-05", "terminal event 失败", "DELIVERY", "tests/test_stage2_5_wp6_delivery_faults.py::test_terminal_publication_failure_after_delivery"),
    Stage25RCScenario("RC25-M-01", "Memory transaction rollback", "MEMORY", "tests/test_stage2_5_wp6_memory_faults.py::test_memory_exchange_faults_fail_run_without_resend"),
    Stage25RCScenario("RC25-M-02", "duplicate exchange", "MEMORY", "tests/test_final_memory_atomicity.py::test_atomic_exchange_commits_both_or_nothing"),
    Stage25RCScenario("RC25-M-03", "commit failure", "MEMORY", "tests/test_stage2_5_wp6_memory_faults.py::test_memory_exchange_faults_fail_run_without_resend"),
    Stage25RCScenario("RC25-R-01", "POST_PLAN 恢复 fail closed", "RECOVERY", "tests/test_recovery_delivery_boundary.py::test_post_plan_checkpoint_is_not_resumable_without_bindings"),
    Stage25RCScenario("RC25-R-02", "specialist 中断恢复", "RECOVERY", "tests/test_recovery_delivery_boundary.py::test_specialist_interruption_fails_closed_without_result_rehydration"),
    Stage25RCScenario("RC25-R-03", "final SUCCEEDED 无 OUTPUT 事实", "RECOVERY", "tests/test_recovery_delivery_boundary.py::test_final_step_succeeded_without_output_journal_is_unsupported"),
    Stage25RCScenario("RC25-R-04", "OUTPUT journaled 无 terminal", "RECOVERY", "tests/test_recovery_delivery_boundary.py::test_output_journaled_without_terminal_is_delivery_unknown"),
    Stage25RCScenario("RC25-R-05", "delivery unknown 不重发", "RECOVERY", "tests/test_recovery_delivery_boundary.py::test_delivery_unknown_never_resumes_or_resends"),
    Stage25RCScenario("RC25-R-06", "delivered+Memory unknown 需协调", "RECOVERY", "tests/test_recovery_delivery_boundary.py::test_delivered_with_unknown_memory_requires_manual_coordination"),
    Stage25RCScenario("RC25-R-07", "Memory committed 无 terminal 不重写", "RECOVERY", "tests/test_recovery_delivery_boundary.py::test_memory_committed_without_terminal_never_rewrites_or_resends"),
    Stage25RCScenario("RC25-R-08", "terminal delivered+memory failed", "RECOVERY", "tests/test_recovery_delivery_boundary.py::test_terminal_delivered_memory_failed_is_terminal_not_resumed"),
    Stage25RCScenario("RC25-SEC-01", "Shape 3 成功安全矩阵", "SECURITY", "tests/test_wp5_security_matrix.py::test_shape3_main_chain_security_matrix"),
    Stage25RCScenario("RC25-SEC-02", "specialist 失败安全矩阵", "SECURITY", "tests/test_stage2_5_wp6_security.py::test_specialist_failure_security_matrix"),
    Stage25RCScenario("RC25-SEC-03", "delivery unknown 安全矩阵", "SECURITY", "tests/test_stage2_5_wp6_security.py::test_delivery_unknown_security_matrix"),
    Stage25RCScenario("RC25-SEC-04", "Memory 失败安全矩阵", "SECURITY", "tests/test_stage2_5_wp6_security.py::test_memory_failure_security_matrix"),
    Stage25RCScenario("RC25-PL-01", "max_concurrency=1 串行", "PARALLEL", "tests/test_stage2_5_wp6_parallelism.py::test_max_concurrency_one_is_serial"),
    Stage25RCScenario("RC25-PL-02", "max_concurrency=2 真实重叠", "PARALLEL", "tests/test_multi_agent_execution.py::test_shape3_specialists_overlap_and_synthesis_waits"),
    Stage25RCScenario("RC25-PL-03", "budget max_concurrency 生效", "PARALLEL", "tests/test_stage2_5_wp6_parallelism.py::test_budget_max_concurrency_limits_overlap"),
    Stage25RCScenario("RC25-PL-04", "取消传播到多个 running Step", "PARALLEL", "tests/test_stage2_5_wp6_parallelism.py::test_cancellation_propagates_to_running_steps"),
    Stage25RCScenario("RC25-PL-05", "迟到 worker 不得提交", "PARALLEL", "tests/test_stage2_5_wp6_parallelism.py::test_late_worker_after_run_terminal_cannot_commit"),
    Stage25RCScenario("RC25-PL-06", "synthesis 等待全部 required READABLE", "PARALLEL", "tests/test_multi_agent_execution.py::test_shape3_specialists_overlap_and_synthesis_waits"),
)


def fault_catalog_counts() -> dict[str, int]:
    entries = STAGE25_FAULT_CATALOG
    return {
        "total": len(entries),
        "required": sum(1 for entry in entries if entry.required),
        "supported": sum(
            1
            for entry in entries
            if entry.support_status is FaultPointSupportStatus.SUPPORTED
        ),
        "contract_only": sum(
            1
            for entry in entries
            if entry.support_status is FaultPointSupportStatus.CONTRACT_ONLY
        ),
        "not_applicable": sum(
            1
            for entry in entries
            if entry.support_status is FaultPointSupportStatus.NOT_APPLICABLE
        ),
        "required_supported": sum(
            1
            for entry in entries
            if entry.required
            and entry.support_status is FaultPointSupportStatus.SUPPORTED
        ),
        "required_contract_only": sum(
            1
            for entry in entries
            if entry.required
            and entry.support_status is FaultPointSupportStatus.CONTRACT_ONLY
        ),
    }


def required_catalog_entries() -> tuple[Stage25FaultCatalogEntry, ...]:
    return tuple(entry for entry in STAGE25_FAULT_CATALOG if entry.required)


def assert_real_test_mappings(*scenario_ids: str) -> None:
    selected = {item.scenario_id: item for item in RC_SCENARIOS_25}
    for scenario_id in scenario_ids:
        scenario = selected[scenario_id]
        file_name, *node_parts = scenario.test_id.split("::")
        source_path = Path(file_name)
        assert source_path.is_file(), scenario.test_id
        source = source_path.read_text(encoding="utf-8")
        assert node_parts[-1] in source, scenario.test_id


def assert_catalog_test_ids_exist() -> None:
    """Every catalog test id must correspond to a real test file."""
    for entry in STAGE25_FAULT_CATALOG:
        for test_id in entry.test_ids:
            assert _catalog_test_file(test_id).is_file(), (
                f"{entry.catalog_id} references missing "
                f"{_catalog_test_file(test_id)}"
            )


def _catalog_test_file(test_id: str) -> Path:
    base = test_id[5:] if test_id.startswith("test_") else test_id
    return Path(f"tests/test_{base}.py")


__all__ = [
    "RC_SCENARIOS_25",
    "STAGE25_FAULT_CATALOG",
    "Stage25FaultCatalogEntry",
    "Stage25RCScenario",
    "assert_catalog_test_ids_exist",
    "assert_real_test_mappings",
    "fault_catalog_counts",
    "required_catalog_entries",
]
