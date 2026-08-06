# Stage 2.5 Fault Injection Catalog v2

> 数据源：`tests/_stage2_5_wp6_catalog.py`（WP6 验收矩阵、RC Gate 与本文档计数均由该文件派生，避免文档与测试漂移）。

目录场景总数：111；RC 必选 fault point：30（全部 SUPPORTED 且有确定性测试）；目录 SUPPORTED：30；CONTRACT_ONLY：81；NOT_APPLICABLE：0；必选且 SUPPORTED：30；必选 CONTRACT_ONLY：0。

状态语义：
- `SUPPORTED`：有确定性故障注入缝与测试（RAISE_TYPED_ERROR 或事件/检查点接缝）。
- `CONTRACT_ONLY`：无独立注入缝；通过 fake/状态夹具或既有接缝以合同级确定性测试覆盖（非 RC 必选）。
- `NOT_APPLICABLE`：本阶段不适用。

## 全量目录

| ID | 场景 | 类别 | FaultPoint | 状态 | RC 必选 | 预期 error_code | 测试文件 |
|---|---|---|---|---|---|---|---|
| FP-PLAN-01 | Resolver deterministic rule 失败 | PLANNING | `PLANNING_BEFORE_RESOLVE` | SUPPORTED | 是 | PLANNING_MODEL_FAILED | `stage2_5_wp6_planning_faults` |
| FP-PLAN-02 | Planning model 普通失败 | PLANNING | `PLANNING_BEFORE_RESOLVE` | SUPPORTED | 是 | PLANNING_MODEL_FAILED | `stage2_5_wp6_planning_faults` |
| FP-PLAN-03 | Planner 独立 timeout | PLANNING | `-` | CONTRACT_ONLY | 否 | PLANNER_TIMEOUT | `test_dynamic_planning_lifecycle` |
| FP-PLAN-04 | Run deadline | PLANNING | `-` | CONTRACT_ONLY | 否 | DEADLINE_EXCEEDED | `test_dynamic_planning_lifecycle` |
| FP-PLAN-05 | budget exhausted | PLANNING | `-` | CONTRACT_ONLY | 否 | BUDGET_EXHAUSTED | `test_dynamic_planning_lifecycle` |
| FP-PLAN-06 | malformed schema | PLANNING | `-` | CONTRACT_ONLY | 否 | PLANNER_SCHEMA_INVALID | `test_multi_agent_planning` |
| FP-PLAN-07 | forbidden field | PLANNING | `-` | CONTRACT_ONLY | 否 | PLANNER_FIELD_FORBIDDEN | `test_multi_agent_planning` |
| FP-PLAN-08 | unknown/disabled Agent | PLANNING | `-` | CONTRACT_ONLY | 否 | UNKNOWN_AGENT | `test_multi_agent_planning` |
| FP-PLAN-09 | Compiler 失败 | PLANNING | `-` | CONTRACT_ONLY | 否 | COMPILE_* | `test_multi_agent_planning` |
| FP-PLAN-10 | PLAN_CREATED journal append 失败 | PLANNING | `PLANNING_BEFORE_PLAN_CREATED` | SUPPORTED | 是 | COORDINATOR_INFRASTRUCTURE_ERROR | `stage2_5_wp6_planning_faults` |
| FP-PLAN-11 | POST_PLAN checkpoint 失败 | PLANNING | `SNAPSHOT_BEFORE_SAVE` | SUPPORTED | 是 | POST_PLAN_PRE_EXECUTION_CHECKPOINT_FAILED | `stage2_5_wp6_planning_faults` |
| FP-PLAN-12 | cancel during planning | PLANNING | `-` | CONTRACT_ONLY | 否 | REQUEST_CANCELLED | `test_dynamic_planning_lifecycle`, `stage2_5_wp6_planning_faults` |
| FP-PLAN-13 | client disconnect during planning | PLANNING | `-` | CONTRACT_ONLY | 否 | CLIENT_DISCONNECTED | `stage2_5_wp6_planning_faults` |
| FP-PLAN-14 | shutdown during planning | PLANNING | `-` | CONTRACT_ONLY | 否 | SERVER_SHUTDOWN | `stage2_5_wp6_planning_faults` |
| FP-SPEC-01 | 单 specialist 模型失败 | SPECIALIST | `STEP_BEFORE_DRIVER_EXECUTE` | SUPPORTED | 是 | REQUIRED_DEPENDENCY_FAILED | `stage2_5_wp6_execution_faults` |
| FP-SPEC-02 | Shape 3 一个 specialist 失败 | SPECIALIST | `STEP_BEFORE_DRIVER_EXECUTE` | SUPPORTED | 是 | REQUIRED_DEPENDENCY_FAILED | `stage2_5_wp6_execution_faults` |
| FP-SPEC-03 | 一个 specialist timeout | SPECIALIST | `-` | CONTRACT_ONLY | 否 | DEADLINE_EXCEEDED | `test_multi_agent_execution` |
| FP-SPEC-04 | 一个 specialist cancellation | SPECIALIST | `-` | CONTRACT_ONLY | 否 | STEP_CANCELLED | `test_multi_agent_execution` |
| FP-SPEC-05 | Tool 失败 | SPECIALIST | `TOOL_BEFORE_INVOCATION` | SUPPORTED | 是 | TOOL_EXECUTION_FAILED | `tool_fault_injection` |
| FP-SPEC-06 | Retrieval 失败 | SPECIALIST | `RETRIEVAL_BEFORE_SEARCH` | SUPPORTED | 是 | RETRIEVAL_FAILED | `retrieval_fault_injection` |
| FP-SPEC-07 | adapter identity mismatch | SPECIALIST | `STEP_BEFORE_DRIVER_EXECUTE` | SUPPORTED | 是 | AGENT_STEP_FAILED | `stage2_5_wp6_execution_faults` |
| FP-SPEC-08 | Binding mismatch | SPECIALIST | `-` | CONTRACT_ONLY | 否 | AGENT_STEP_FAILED | `stage2_5_wp6_execution_faults` |
| FP-SPEC-09 | result 非法 | SPECIALIST | `-` | CONTRACT_ONLY | 否 | STEP_RESULT_INVALID | `stage2_5_wp6_execution_faults` |
| FP-SPEC-10 | result 过大 | SPECIALIST | `-` | CONTRACT_ONLY | 否 | STEP_RESULT_PREPARE_FAILED | `test_multi_agent_execution` |
| FP-SPEC-11 | duplicate result | SPECIALIST | `-` | CONTRACT_ONLY | 否 | STEP_RESULT_DUPLICATE_COMMIT | `stage2_5_wp6_execution_faults` |
| FP-SPEC-12 | late result | SPECIALIST | `-` | CONTRACT_ONLY | 否 | STEP_RESULT_LATE_COMMIT | `stage2_5_wp6_execution_faults` |
| FP-SPEC-13 | bounded executor 提交失败 | SPECIALIST | `EXECUTOR_BEFORE_SUBMIT` | SUPPORTED | 是 | REQUIRED_DEPENDENCY_FAILED | `stage2_5_wp6_execution_faults`, `stage2_5_wp6_starvation` |
| FP-SPEC-14 | detached worker 在 Run 终态后返回 | SPECIALIST | `-` | CONTRACT_ONLY | 否 | STEP_RESULT_LATE_COMMIT | `stage2_5_wp6_execution_faults` |
| FP-STORE-01 | write_prepared 失败 | STORE | `STORE_BEFORE_WRITE_PREPARED` | SUPPORTED | 是 | STEP_RESULT_PREPARE_FAILED | `stage2_5_wp6_execution_faults` |
| FP-STORE-02 | Step 状态提交失败 | STORE | `-` | CONTRACT_ONLY | 否 | STEP_STATE_COMMIT_FAILED | `test_step_completion` |
| FP-STORE-03 | mark_readable 失败 | STORE | `STORE_BEFORE_MARK_READABLE` | SUPPORTED | 是 | STEP_RESULT_COMMIT_FAILED | `stage2_5_wp6_execution_faults` |
| FP-STORE-04 | STEP_COMPLETED journal 失败 | STORE | `EVENT_BEFORE_JOURNAL_APPEND` | SUPPORTED | 是 | STEP_COMPLETION_EVENT_FAILED | `stage2_5_wp6_execution_faults` |
| FP-STORE-05 | STEP_COMPLETED enqueue 部分持久化 | STORE | `EVENT_BEFORE_CHANNEL_ENQUEUE` | SUPPORTED | 是 | STEP_COMPLETION_EVENT_FAILED | `test_partial_publication_sequence` |
| FP-STORE-06 | completion 重复回调 | STORE | `-` | CONTRACT_ONLY | 否 | STEP_RESULT_DUPLICATE_COMMIT | `test_step_completion`, `stage2_5_wp6_execution_faults` |
| FP-STORE-07 | Store seal 后写入 | STORE | `-` | CONTRACT_ONLY | 否 | STEP_RESULT_LATE_COMMIT | `test_step_result_store` |
| FP-STORE-08 | Store clear 后访问 | STORE | `-` | CONTRACT_ONLY | 否 | STEP_RESULT_LATE_COMMIT | `test_step_result_store` |
| FP-STORE-09 | ACL 拒绝无依赖读取 | STORE | `-` | CONTRACT_ONLY | 否 | READ_NOT_ALLOWED | `test_step_result_store` |
| FP-STORE-10 | synthesis 读取 PREPARED 结果 | STORE | `STORE_BEFORE_DEPENDENCY_READ` | SUPPORTED | 是 | SYNTHESIS_FAILED | `stage2_5_wp6_execution_faults` |
| FP-STORE-11 | producer SUCCEEDED 但 result 不可读 | STORE | `STORE_BEFORE_MARK_READABLE` | SUPPORTED | 是 | STEP_RESULT_COMMIT_FAILED | `stage2_5_wp6_execution_faults` |
| FP-SYNTH-01 | 缺少 required dependency | SYNTHESIS | `-` | CONTRACT_ONLY | 否 | SYNTHESIS_FAILED | `test_synthesis_adapter` |
| FP-SYNTH-02 | dependency 失败 | SYNTHESIS | `-` | CONTRACT_ONLY | 否 | REQUIRED_DEPENDENCY_FAILED | `test_multi_agent_execution` |
| FP-SYNTH-03 | dependency cancelled | SYNTHESIS | `-` | CONTRACT_ONLY | 否 | REQUIRED_DEPENDENCY_FAILED | `test_multi_agent_execution` |
| FP-SYNTH-04 | dependency result 不完整 | SYNTHESIS | `-` | CONTRACT_ONLY | 否 | SYNTHESIS_FAILED | `test_synthesis_adapter` |
| FP-SYNTH-05 | synthesis 模型失败 | SYNTHESIS | `STEP_BEFORE_DRIVER_EXECUTE` | SUPPORTED | 是 | SYNTHESIS_FAILED | `stage2_5_wp6_execution_faults` |
| FP-SYNTH-06 | synthesis timeout | SYNTHESIS | `-` | CONTRACT_ONLY | 否 | DEADLINE_EXCEEDED | `stage2_5_wp6_execution_faults` |
| FP-SYNTH-07 | synthesis result 非法 | SYNTHESIS | `-` | CONTRACT_ONLY | 否 | SYNTHESIS_FAILED | `test_synthesis_adapter` |
| FP-SYNTH-08 | synthesis Memory/history 隔离 | SYNTHESIS | `-` | CONTRACT_ONLY | 否 | - | `test_wp3_history_boundary` |
| FP-SYNTH-09 | synthesis 不得 fallback | SYNTHESIS | `-` | CONTRACT_ONLY | 否 | SYNTHESIS_FAILED | `test_multi_agent_execution` |
| FP-SYNTH-10 | synthesis 恰好一次 | SYNTHESIS | `-` | CONTRACT_ONLY | 否 | - | `stage2_5_wp6_e2e` |
| FP-OUT-01 | journal append 前失败 | OUTPUT | `OUTPUT_BEFORE_PUBLISH` | SUPPORTED | 是 | FINAL_OUTPUT_DELIVERY_FAILED | `stage2_5_wp6_delivery_faults` |
| FP-OUT-02 | journal 成功 enqueue 失败 | OUTPUT | `EVENT_BEFORE_CHANNEL_ENQUEUE` | SUPPORTED | 是 | FINAL_OUTPUT_DELIVERY_UNKNOWN | `test_partial_publication_sequence` |
| FP-OUT-03 | concurrent duplicate | OUTPUT | `-` | CONTRACT_ONLY | 否 | OUTPUT_GATE_DUPLICATE_ATTEMPT | `test_output_gate` |
| FP-OUT-04 | PUBLISHED 后重复 | OUTPUT | `-` | CONTRACT_ONLY | 否 | OUTPUT_GATE_DUPLICATE_ATTEMPT | `test_output_gate`, `stage2_5_wp6_delivery_faults` |
| FP-OUT-05 | FAILED 后重复 | OUTPUT | `-` | CONTRACT_ONLY | 否 | OUTPUT_GATE_DUPLICATE_ATTEMPT | `test_output_gate` |
| FP-OUT-06 | UNKNOWN 后重复 | OUTPUT | `-` | CONTRACT_ONLY | 否 | OUTPUT_GATE_DUPLICATE_ATTEMPT | `test_output_gate` |
| FP-OUT-07 | INTERNAL 调用 | OUTPUT | `-` | CONTRACT_ONLY | 否 | OUTPUT_GATE_INTERNAL_STEP | `test_output_gate` |
| FP-OUT-08 | 非唯一 final 调用 | OUTPUT | `-` | CONTRACT_ONLY | 否 | OUTPUT_GATE_NOT_FINAL | `test_output_gate` |
| FP-OUT-09 | Step 未 SUCCEEDED | OUTPUT | `-` | CONTRACT_ONLY | 否 | OUTPUT_GATE_STEP_NOT_SUCCEEDED | `test_output_gate` |
| FP-OUT-10 | Store 未 READABLE | OUTPUT | `-` | CONTRACT_ONLY | 否 | OUTPUT_GATE_STORE_NOT_READABLE | `test_output_gate` |
| FP-OUT-11 | Store 已 seal | OUTPUT | `-` | CONTRACT_ONLY | 否 | OUTPUT_GATE_STORE_SEALED | `test_output_gate` |
| FP-OUT-12 | Run inactive | OUTPUT | `-` | CONTRACT_ONLY | 否 | OUTPUT_GATE_RUN_NOT_ACTIVE | `test_output_gate` |
| FP-OUT-13 | STEP_COMPLETED 在 delivery 后失败 | OUTPUT | `EVENT_BEFORE_JOURNAL_APPEND` | SUPPORTED | 是 | STEP_COMPLETION_EVENT_FAILED | `stage2_5_wp6_delivery_faults` |
| FP-OUT-14 | terminal event 在 delivery 后失败 | OUTPUT | `JOURNAL_BEFORE_TERMINAL_APPEND` | SUPPORTED | 是 | RUNTIME_TERMINAL_PUBLICATION_FAILED | `stage2_5_wp6_delivery_faults` |
| FP-MEM-01 | 原子事务开始失败 | MEMORY | `MEMORY_BEFORE_EXCHANGE_BEGIN` | SUPPORTED | 是 | FINAL_OUTPUT_MEMORY_COMMIT_FAILED | `stage2_5_wp6_memory_faults` |
| FP-MEM-02 | user insert 失败 | MEMORY | `MEMORY_BEFORE_USER_INSERT` | SUPPORTED | 是 | FINAL_OUTPUT_MEMORY_COMMIT_FAILED | `stage2_5_wp6_memory_faults` |
| FP-MEM-03 | assistant insert 失败 | MEMORY | `MEMORY_BEFORE_ASSISTANT_INSERT` | SUPPORTED | 是 | FINAL_OUTPUT_MEMORY_COMMIT_FAILED | `stage2_5_wp6_memory_faults` |
| FP-MEM-04 | commit 失败 | MEMORY | `MEMORY_BEFORE_EXCHANGE_COMMIT` | SUPPORTED | 是 | FINAL_OUTPUT_MEMORY_COMMIT_FAILED | `stage2_5_wp6_memory_faults` |
| FP-MEM-05 | duplicate exchange | MEMORY | `-` | CONTRACT_ONLY | 否 | DUPLICATE_EXCHANGE | `test_final_memory_atomicity`, `stage2_5_wp6_memory_faults` |
| FP-MEM-06 | delivered 前禁止写 | MEMORY | `-` | CONTRACT_ONLY | 否 | - | `test_final_memory_boundary` |
| FP-MEM-07 | delivery failed 不写 | MEMORY | `-` | CONTRACT_ONLY | 否 | - | `test_final_memory_boundary` |
| FP-MEM-08 | delivery unknown 不写 | MEMORY | `-` | CONTRACT_ONLY | 否 | - | `test_final_memory_boundary` |
| FP-MEM-09 | specialist raw 不写 | MEMORY | `-` | CONTRACT_ONLY | 否 | - | `test_final_memory_boundary`, `test_wp5_security_matrix` |
| FP-MEM-10 | writer 重复调用 | MEMORY | `-` | CONTRACT_ONLY | 否 | FINAL_OUTPUT_MEMORY_COMMIT_FAILED | `test_final_memory_atomicity`, `stage2_5_wp6_memory_faults` |
| FP-MEM-11 | writer 失败后再次调用 | MEMORY | `-` | CONTRACT_ONLY | 否 | FINAL_OUTPUT_MEMORY_COMMIT_FAILED | `stage2_5_wp6_memory_faults` |
| FP-MEM-12 | Memory 成功但 terminal 失败 | MEMORY | `JOURNAL_BEFORE_TERMINAL_APPEND` | SUPPORTED | 是 | RUNTIME_TERMINAL_PUBLICATION_FAILED | `stage2_5_wp6_memory_faults` |
| FP-MEM-13 | delivered 成功但 Memory 失败 | MEMORY | `MEMORY_BEFORE_EXCHANGE_BEGIN` | SUPPORTED | 是 | FINAL_OUTPUT_MEMORY_COMMIT_FAILED | `stage2_5_wp6_memory_faults` |
| FP-OBS-01 | Span 创建失败 | OBSERVABILITY | `TRACE_BEFORE_SPAN_START` | SUPPORTED | 是 | - | `trace_fault_injection` |
| FP-OBS-02 | Span 结束失败 | OBSERVABILITY | `TRACE_BEFORE_SPAN_END` | SUPPORTED | 是 | - | `trace_lifecycle_fault` |
| FP-OBS-03 | recorder 失败不能泄漏正文 | OBSERVABILITY | `OBSERVABILITY_BEFORE_RECORD` | SUPPORTED | 是 | - | `observability_fault_injection`, `test_wp5_security_matrix` |
| FP-OBS-04 | Journal sequence 重复/倒退 | OBSERVABILITY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_tail_corruption`, `test_recovery_validation` |
| FP-OBS-05 | unknown event | OBSERVABILITY | `-` | CONTRACT_ONLY | 否 | - | `test_runtime_projection` |
| FP-OBS-06 | safe payload 非法字段 | OBSERVABILITY | `-` | CONTRACT_ONLY | 否 | - | `test_journal_safe_projection` |
| FP-OBS-07 | OUTPUT 正文不得进入 Journal | OBSERVABILITY | `-` | CONTRACT_ONLY | 否 | - | `test_journal_safe_projection`, `test_wp5_security_matrix` |
| FP-OBS-08 | Metrics recorder 失败不得重复执行 | OBSERVABILITY | `-` | CONTRACT_ONLY | 否 | - | `test_metrics_label_policy_wp5` |
| FP-OBS-09 | 高基数 label 拒绝/规范化 | OBSERVABILITY | `-` | CONTRACT_ONLY | 否 | - | `test_metrics_label_policy_wp5` |
| FP-OBS-10 | Trace attributes 敏感值拒绝 | OBSERVABILITY | `-` | CONTRACT_ONLY | 否 | - | `test_trace_contract`, `test_wp5_security_matrix` |
| FP-OBS-11 | partial persisted 事实可诊断 | OBSERVABILITY | `-` | CONTRACT_ONLY | 否 | - | `test_partial_publication_sequence` |
| FP-FE-01 | duplicate event | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_runtime_projection` |
| FP-FE-02 | sequence 倒退 | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_runtime_projection` |
| FP-FE-03 | 未知 control event | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_runtime_projection` |
| FP-FE-04 | 并行 specialist active 状态 | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_frontend_multi_agent_status` |
| FP-FE-05 | synthesis 状态 | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_frontend_multi_agent_status` |
| FP-FE-06 | delivery failed/unknown 文案 | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_frontend_multi_agent_status` |
| FP-FE-07 | delivered + Memory failed | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_frontend_multi_agent_status`, `test_stage2_5_wp6_security` |
| FP-FE-08 | RUN_COMPLETED 前缺失 control event | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_stage2_5_wp6_frontend` |
| FP-FE-09 | Legacy 事件兼容 | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_frontend_multi_agent_status` |
| FP-FE-10 | OUTPUT 只进入正文一次 | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_frontend_multi_agent_status`, `test_wp5_security_matrix` |
| FP-FE-11 | unknown 文案不建议立即重试 | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_frontend_multi_agent_status` |
| FP-FE-12 | specialist raw 不显示 | FRONTEND | `-` | CONTRACT_ONLY | 否 | - | `test_frontend_multi_agent_status`, `test_wp5_security_matrix` |
| FP-REC-01 | POST_PLAN_PRE_EXECUTION | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_delivery_boundary` |
| FP-REC-02 | specialist 执行中断 | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_delivery_boundary` |
| FP-REC-03 | Store PREPARED 中断 | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `stage2_5_wp6_recovery` |
| FP-REC-04 | final Step SUCCEEDED 无 OUTPUT 事实 | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_delivery_boundary` |
| FP-REC-05 | OUTPUT journaled 无 terminal | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_delivery_boundary` |
| FP-REC-06 | delivery unknown | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_delivery_boundary` |
| FP-REC-07 | delivered 已知 Memory 未知 | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_delivery_boundary` |
| FP-REC-08 | Memory committed 无 terminal | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_delivery_boundary` |
| FP-REC-09 | terminal 成功 | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_delivery_boundary` |
| FP-REC-10 | corrupt/duplicate/out-of-order Journal | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_tail_corruption` |
| FP-REC-11 | v1/v2 Snapshot 兼容 | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_version_compatibility` |
| FP-REC-12 | fingerprint mismatch | RECOVERY | `-` | CONTRACT_ONLY | 否 | - | `test_recovery_version_compatibility` |

## RC 必选 fault point 证据

RC 必选 fault point 的全部测试位于：

- `tests/test_observability_fault_injection.py`
- `tests/test_retrieval_fault_injection.py`
- `tests/test_stage2_5_wp6_delivery_faults.py`
- `tests/test_stage2_5_wp6_execution_faults.py`
- `tests/test_stage2_5_wp6_memory_faults.py`
- `tests/test_stage2_5_wp6_planning_faults.py`
- `tests/test_stage2_5_wp6_starvation.py`
- `tests/test_test_partial_publication_sequence.py`
- `tests/test_test_wp5_security_matrix.py`
- `tests/test_tool_fault_injection.py`
- `tests/test_trace_fault_injection.py`
- `tests/test_trace_lifecycle_fault.py`

## 说明

- 同步缝（Planning/Store/Driver/Gate/Memory/Executor）只支持 `RAISE_TYPED_ERROR`，不提供 DELAY/BLOCK，避免阻塞 asyncio transport。
- `EXECUTOR_BEFORE_SUBMIT` 由 WP6 从 CONTRACT_ONLY 升级为 SUPPORTED（`parallel_executor` 缝 + 饥饿容量测试）。
- Recovery 场景为 CONTRACT_ONLY：RecoveryValidator 是只读验证器，通过状态/Journal 夹具做合同级测试，证明不重发、不重写。
- `CONTRACT_ONLY` 不得描述为 `SUPPORTED`；本文档与支持报告保持一致。