# Stage 2.5 WP6 最终验收结果（Multi-Agent 完整闭环与 RC Gate）

## 1. Executive Summary

WP6 完成。Stage 2.5 Multi-Agent 的完整闭环
（Planning -> Plan freeze -> Parallel specialists -> StepResultStore ->
Synthesis -> OutputGate -> Final Memory atomic commit -> Trace/Journal/
Metrics/Frontend projection -> Run terminal）在成功、失败、取消、超时、
部分持久化和恢复场景下均通过合同级验收。

- 必选 RC 场景：54/54 通过（成功 Shape 0～3、Planning/Execution/Delivery/
  Memory 失败、Recovery、Security、Parallel 场景）。
- Fault Injection Catalog v2：111 个目录场景；RC 必选 fault point 30 个
  全部 `SUPPORTED` 且有确定性测试；其余 81 个为 `CONTRACT_ONLY` 合同级覆盖。
- 全仓测试：1412 passed, 42 subtests passed（WP6 前基线 1346 passed）。
- P0 findings：0；P1 findings：0；P2 findings：1（Planning executor 饥饿，
  判定 `ACCEPTED_P2`）。
- 实施中发现并最小修复 1 处合同缺陷：Planning 准入等待同步占用事件循环，
  导致无 deadline 且 executor 占满时同一 loop 的取消无法传播；修复为把
  planning submit 的同步准入等待移到 `asyncio.to_thread`，不新增线程池、
  不改变准入语义（详见第 15 节）。
- RC Gate：PASS。`Ready to freeze Stage 2.5: YES`。

## 2. Source Audit Before Changes

WP6 实施前的真实源码审计（基线为 WP5 提交 `a7b611a`）：

### 2.1 FaultPoint 现状

- `FaultPoint` 枚举 42 个：32 `SUPPORTED`、10 `CONTRACT_ONLY`、0
  `NOT_APPLICABLE`；全部 SUPPORTED 有测试证据。
- 既有接缝覆盖 Model/Tool/Retrieval/Event/Journal/Snapshot/Recovery/
  Observability/Trace/Shutdown；`EXECUTOR_BEFORE_SUBMIT` 与
  `EXECUTOR_AFTER_SUBMIT` 为 CONTRACT_ONLY（无运行缝）。

### 2.2 WP2～WP5 新增链路的注入点缺口

| 链路 | 缺口 | WP6 动作 |
|---|---|---|
| Planning | 无 planning 阶段运行缝；PLAN_CREATED 发布失败、POST_PLAN checkpoint 失败无专项 | 新增 `PLANNING_BEFORE_RESOLVE`、`PLANNING_BEFORE_PLAN_CREATED`；POST_PLAN 用既有 `SNAPSHOT_BEFORE_SAVE` 接缝 |
| Specialist 执行 | 无 driver 前缝；binding/adapter mismatch 无运行缝 | 新增 `STEP_BEFORE_DRIVER_EXECUTE` |
| Store/Completion | 无 Store 运行缝；EXECUTOR_BEFORE_SUBMIT 无运行缝 | 新增 `STORE_BEFORE_WRITE_PREPARED`、`STORE_BEFORE_MARK_READABLE`、`STORE_BEFORE_DEPENDENCY_READ`；接线 `EXECUTOR_BEFORE_SUBMIT` |
| OutputGate | 无 gate 前缝（pre-journal 失败靠 EventChannel 缝间接覆盖） | 新增 `OUTPUT_BEFORE_PUBLISH` |
| Memory | 无 exchange 事务缝 | 新增 `MEMORY_BEFORE_EXCHANGE_BEGIN/USER_INSERT/ASSISTANT_INSERT/EXCHANGE_COMMIT` |

### 2.3 其他审计结论

- RunCoordinator 异常优先级：`BudgetExceededError` -> `RunDeadlineExceededError`
  -> `RunCancelledError` -> Planning/Registry/Compile -> CancelledError ->
  Infrastructure -> Generic。
- StepCompletionPipeline 失败优先级：result 校验 -> Store PREPARED -> Step
  状态 -> Store READABLE -> OutputGate -> Memory -> STEP_COMPLETED。
- OutputGate 终态：`NOT_STARTED/PUBLISHING/PUBLISHED/FAILED/OUTCOME_UNKNOWN`
  全部有测试。
- `RunFinalMemoryWriter` 为 write-once + 单事务；RecoveryValidator 只读、
  不恢复 Store/Bindings/Gate。
- 默认 API 的真实 Shape 0～3 路径全部经 typed pipeline（`_is_typed_multi_step_plan`
  对 dynamic Plan 为真）；static/Legacy 不创建 Gate。
- 前端共享状态模型：`RuntimeProjectionBuilder` + `format_frontend_status`，
  可自动测试。

## 3. Files Changed

### 生产代码（最小合同修复与故障缝）

| 文件 | 职责 |
|---|---|
| `core/runtime/fault_injection_contract.py` | 新增 11 个 Stage 2.5 故障点（Planning 2、Driver 1、Store 3、Output 1、Memory 4） |
| `core/runtime/fault_injection.py` | 新增 `evaluate_sync_fault`（同步 RAISE-only 接缝助手） |
| `core/runtime/fault_reports.py` | 支持报告新增 12 个 SUPPORTED 条目；`EXECUTOR_BEFORE_SUBMIT` 由 CONTRACT_ONLY 升级 SUPPORTED；同步缝仅声明 RAISE_TYPED_ERROR |
| `core/runtime/run_coordinator.py` | 动态 resolver 注入 fault_controller；`PLANNING_BEFORE_RESOLVE`/`PLANNING_BEFORE_PLAN_CREATED` 缝；POST_PLAN checkpoint 透传 fault_controller；Store/Gate 注入 fault_controller |
| `core/runtime/planning_model_adapter.py` | 最小修复：planning submit 的同步准入等待移到 `asyncio.to_thread`，释放事件循环 |
| `core/runtime/multi_agent_driver.py` | `STEP_BEFORE_DRIVER_EXECUTE` 缝 |
| `core/runtime/step_result_store.py` | `STORE_BEFORE_WRITE_PREPARED/MARK_READABLE/DEPENDENCY_READ` 缝 |
| `core/runtime/step_completion.py` | Store 故障（InjectedFaultError）映射为 STEP_RESULT_PREPARE_FAILED / STEP_RESULT_COMMIT_FAILED |
| `core/runtime/output_gate.py` | `OUTPUT_BEFORE_PUBLISH` 缝 |
| `core/runtime/parallel_execution.py` | `EXECUTOR_BEFORE_SUBMIT` 运行缝 |
| `core/memory_manager.py` | `MEMORY_BEFORE_*` 4 个 exchange 事务缝（延迟导入避免循环依赖） |
| `core/runtime/runtime_factory.py` | fault_controller 透传到 ParallelExecutor 与 RunCoordinator |

### 测试与数据源

| 文件 | 职责 |
|---|---|
| `tests/_stage2_5_wp6_catalog.py`（新增） | 目录 v2 与 RC 场景唯一数据源 |
| `tests/_stage2_5_wp6_fixtures.py`（新增） | WP6 确定性 controller 与 gated planner |
| `tests/test_stage2_5_wp6_e2e.py` | Shape 0～3 成功主链（含 3 specialist） |
| `tests/test_stage2_5_wp6_planning_faults.py` | Planning 故障矩阵 |
| `tests/test_stage2_5_wp6_execution_faults.py` | Specialist/Store/Synthesis 故障矩阵 |
| `tests/test_stage2_5_wp6_delivery_faults.py` | OutputGate/terminal 事件故障 |
| `tests/test_stage2_5_wp6_memory_faults.py` | Memory 事务故障 |
| `tests/test_stage2_5_wp6_recovery.py` | Recovery 补充（PREPARED 中断 + no-replay 不变量） |
| `tests/test_stage2_5_wp6_security.py` | 失败路径安全矩阵 |
| `tests/test_stage2_5_wp6_parallelism.py` | 串行/预算并发/取消传播/迟到 worker |
| `tests/test_stage2_5_wp6_starvation.py` | Planning 饥饿容量（恢复/取消/deadline 分类） |
| `tests/test_stage2_5_wp6_frontend.py` | 终态前缺失 control event 投影 |
| `tests/test_stage2_5_rc_gate.py` | RC Gate 聚合 |
| `tests/test_fault_point_support_report.py`、`test_fault_coverage_report.py`、`test_fault_injection_contract.py` | 计数与枚举更新（53/44/9） |

### 文档

| 文件 | 职责 |
|---|---|
| `docs/runtime/stage2_5_fault_injection_catalog_v2.md`（新增） | 故障目录 v2 |
| `docs/runtime/stage2_5_operations_runbook.md`（新增） | 运维处置 |
| `docs/runtime/runtime_error_code_catalog.md` | 补齐 WP6 错误码行 |
| `docs/learning/stage2/result/stage2_5_wp6_final_acceptance_result.md`（本文档） | 最终验收 |

`docs/runtime/stage2_5_trace_contract_v1.md` 无真实字段变化，未更新。

## 4. Fault Injection Catalog v2

见 [stage2_5_fault_injection_catalog_v2.md](../../../runtime/stage2_5_fault_injection_catalog_v2.md)。

汇总（由 `tests/_stage2_5_wp6_catalog.py` 与
`core/runtime/fault_reports.py` 派生）：

- 目录场景总数：111（Planning 14、Specialist 14、Store 11、Synthesis 10、
  Output 14、Memory 13、Observability 11、Frontend 12、Recovery 12）。
- RC 必选 fault point：30，全部 `SUPPORTED`，全部有确定性测试（30/30）。
- 目录 `SUPPORTED`：30；`CONTRACT_ONLY`：81；`NOT_APPLICABLE`：0。
- 底层 FaultPoint 支持报告：总 53；`SUPPORTED` 44；`CONTRACT_ONLY` 9；
  `NOT_APPLICABLE` 0；全部 SUPPORTED 有测试（tested 44/44）。
- `EXECUTOR_BEFORE_SUBMIT` 由 CONTRACT_ONLY 升级为 SUPPORTED。

## 5. Required RC Scenario Matrix

共 54 个必选 RC 场景，逐条映射真实测试（`RC_SCENARIOS_25`，RC Gate 测试
逐条校验映射存在）：

| 类别 | 场景数 | 代表场景与测试 |
|---|---|---|
| SUCCESS | 11 | Shape 0/1/2/3 成功主链（`test_final_output_delivery.py`、`test_stage2_5_wp6_e2e.py`） |
| PLANNING_FAILURE | 11 | schema/unknown agent/timeout/budget/cancel/deadline/model failure/PLAN_CREATED/checkpoint/disconnect/shutdown |
| EXECUTION_FAILURE | 6 | specialist 失败/timeout/result 过大/Store 提交失败/dependency 不可读/synthesis 失败 |
| DELIVERY | 5 | known failed/unknown/duplicate/completion event/terminal event |
| MEMORY | 3 | rollback/duplicate exchange/commit failure |
| RECOVERY | 8 | POST_PLAN/specialist 中断/final 无 OUTPUT 事实/journaled 无 terminal/unknown/delivered+memory unknown/committed 无 terminal/terminal |
| SECURITY | 4 | 成功/specialist 失败/delivery unknown/Memory 失败安全矩阵 |
| PARALLEL | 6 | max_concurrency=1/2、budget 并发、取消传播、迟到 worker、synthesis 等待 |

`tests/test_stage2_5_rc_gate.py::test_all_required_rc_scenarios_map_to_real_tests`
与 `test_rc_gate_pass_is_derived_from_actual_checks` 保证 54/54 映射且全部
计入 PASS。

## 6. Shape 0～3 E2E

成功主链证据（`tests/test_stage2_5_wp6_e2e.py`、`test_final_output_delivery.py`）：

| Shape | 形态 | 专项测试 |
|---|---|---|
| 0 | Core direct | `test_shape0_full_contract`（planning 1 次、INTERNAL 输出 0、final OUTPUT 1、Memory exchange 1、Trace 拓扑、Journal 无正文、frontend 终态） |
| 1 | explicit knowledge/data/code | `test_shape1_explicit_*`（planner 0 次、entry 调用 1 次、唯一 OUTPUT） |
| 1 | delegated knowledge passthrough | `test_shape1_delegated_knowledge_direct_single_output`（history_policy=NONE） |
| 2 | code/data -> synthesis | `test_shape2_*`（specialist 1 + synthesis 1、唯一 final） |
| 3 | knowledge+code / knowledge+data / code+data / 三 specialist | `test_shape3_*`（并行 specialist、唯一 synthesis、唯一 final） |

每场景断言：Planner 调用次数、编译 shape（planning span attribute）、Agent
调用次数、并行关系（shape3 用 barrier/active counter）、Synthesis 次数、
INTERNAL 输出数 = 0、final OUTPUT 数 = 1、Final Step SUCCEEDED、Run
SUCCEEDED、Memory 完整 exchange = 1、Trace 拓扑、Journal 无正文、frontend
terminal 状态。全部通过。

## 7. Planning Failure Matrix

| 场景 | StopReason | error_code | 证据测试 |
|---|---|---|---|
| malformed schema | PLANNING_FAILED | PLANNER_SCHEMA_INVALID | `test_multi_agent_planning.py` |
| unknown/disabled Agent | PLANNING_FAILED | UNKNOWN_AGENT | `test_multi_agent_planning.py` |
| Planner 独立 timeout | PLANNING_FAILED | PLANNER_TIMEOUT | `test_dynamic_planning_lifecycle.py` |
| budget exhausted | BUDGET_EXHAUSTED | BUDGET_EXHAUSTED | `test_dynamic_planning_lifecycle.py` |
| cancel during planning | CANCELLED | REQUEST_CANCELLED | `test_dynamic_planning_lifecycle.py` |
| deadline | DEADLINE_EXCEEDED | DEADLINE_EXCEEDED | `test_dynamic_planning_lifecycle.py` |
| model failure（注入缝） | PLANNING_FAILED | PLANNING_MODEL_FAILED | `test_stage2_5_wp6_planning_faults.py::test_planning_resolve_injected_fault_is_planning_failed` |
| PLAN_CREATED journal 失败 | UNHANDLED_ERROR | COORDINATOR_INFRASTRUCTURE_ERROR | `test_plan_created_publication_failure_has_no_steps` / `test_plan_created_journal_append_failure_has_no_steps` |
| POST_PLAN checkpoint 失败 | UNHANDLED_ERROR | POST_PLAN_PRE_EXECUTION_CHECKPOINT_FAILED | `test_post_plan_checkpoint_failure_fails_closed` |
| client disconnect / shutdown | CANCELLED | CLIENT_DISCONNECTED / SERVER_SHUTDOWN | `test_client_disconnect_during_planning_is_cancelled` / `test_shutdown_during_planning_is_cancelled` |

全部断言：无 PLAN_CREATED（或按故障点预期）、无 STEP_STARTED、无 OUTPUT、
无 Memory、单 terminal、StopReason/error_code 正确。

## 8. Specialist / Store / Synthesis Failure Matrix

| 场景 | Run error_code | 证据 |
|---|---|---|
| 单 specialist 失败（driver 缝） | REQUIRED_DEPENDENCY_FAILED（synthesis BLOCKED） | `test_step_before_driver_execute_fails_specialist` |
| specialist timeout | DEADLINE_EXCEEDED | `test_multi_agent_execution.py::test_specialist_deadline_blocks_synthesis` |
| result 过大 | STEP_RESULT_PREPARE_FAILED | `test_multi_agent_execution.py::test_result_too_large_fails_prepare_closed` |
| write_prepared 失败 | STEP_RESULT_PREPARE_FAILED | `test_store_write_prepared_failure_fails_closed` |
| mark_readable 失败 | STEP_RESULT_COMMIT_FAILED（Final Step 保持 SUCCEEDED） | `test_mark_readable_failure_keeps_final_succeeded` |
| dependency 读取失败 | SYNTHESIS_FAILED（synthesis 未调用） | `test_dependency_read_failure_blocks_synthesis` |
| synthesis 模型失败 | SYNTHESIS_FAILED | `test_synthesis_driver_fault_is_synthesis_failed` |
| bounded executor 提交失败 | REQUIRED_DEPENDENCY_FAILED | `test_executor_submit_failure_fails_closed` |
| binding/adapter identity mismatch | REGISTRY_MISMATCH（fail closed） | `test_verify_triple_identity_rejects_mismatch` |
| duplicate/late commit | STEP_RESULT_DUPLICATE_COMMIT / STEP_RESULT_LATE_COMMIT | `test_step_completion.py` |

全部断言：synthesis model call = 0（依赖失败时）、无 partial final、无 Memory、
required dependency fail-closed、无 Core fallback。

## 9. Delivery Failure Matrix

| 场景 | Gate 终态 | error_code | 正文 attempt | 证据 |
|---|---|---|---|---|
| journal append 前失败 | FAILED | FINAL_OUTPUT_DELIVERY_FAILED | 0（无正文事实） | `test_output_before_publish_failure_is_failed_not_unknown`、`test_pre_journal_failure_does_not_consume_sequence_and_fails_known` |
| journal 成功 enqueue 失败 | OUTCOME_UNKNOWN | FINAL_OUTPUT_DELIVERY_UNKNOWN | 1（已 journaled） | `test_partial_persisted_output_consumes_sequence_and_fails_unknown` |
| concurrent/PUBLISHED/FAILED/UNKNOWN 后重复 | 终态 | OUTPUT_GATE_DUPLICATE_ATTEMPT | 1 | `test_output_gate.py` |
| STEP_COMPLETED 在 delivery 后失败 | PUBLISHED | STEP_COMPLETION_EVENT_FAILED | 1 | `test_step_completed_after_delivery_failure_keeps_delivery` |
| terminal event 在 delivery 后失败 | PUBLISHED（Memory 已写） | RUNTIME_TERMINAL_PUBLICATION_FAILED | 1 | `test_terminal_publication_failure_after_delivery` |

at-most-once：所有场景正文发布尝试最多 1 次；`test_success_delivery_is_exactly_once`
证明成功主链仅一个 OUTPUT 事实、Gate 单一终态。

## 10. Memory Failure Matrix

| 场景 | 结果 | 证据 |
|---|---|---|
| exchange 事务开始失败 | Run FAILED（FINAL_OUTPUT_MEMORY_COMMIT_FAILED），Gate PUBLISHED，正文 1 次，无 message 行 | `test_memory_exchange_faults_fail_run_without_resend` |
| user insert 失败 | 同上，整体回滚 | 同上 |
| assistant insert 失败 | 同上，无半个 exchange | 同上 |
| commit 失败 | 同上；后续新 run_id 可正常提交（回滚未损坏库） | `test_commit_failure_fails_run_without_resend`、`test_rollback_does_not_corrupt_next_exchange` |
| duplicate exchange | DUPLICATE_EXCHANGE，历史不变 | `test_duplicate_exchange_is_rejected_without_duplication` |
| writer 失败后再次调用 | 拒绝（write-once），不自动重试 | `test_writer_failure_then_reinvoke_is_rejected` |
| delivered + Memory 失败前端文案 | “回答已交付，记忆保存失败。” | `test_delivered_memory_failed_frontend_text` |

证明：不存在半个已提交 exchange；不自动重试；不重新发布 final；
history 不读取不完整 exchange（既有 `test_final_memory_atomicity.py`）。

## 11. Cancellation / Deadline / Budget / Shutdown

| 场景 | 结果 | 证据 |
|---|---|---|
| planning 期间取消/断连/关闭 | CANCELLED（REQUEST_CANCELLED/CLIENT_DISCONNECTED/SERVER_SHUTDOWN），无 PLAN_CREATED/STEP | planning faults 专项 |
| specialist 执行中取消 | CANCELLED，synthesis 不调用，无正文 | `test_multi_agent_execution.py`、`test_cancellation_propagates_to_running_steps` |
| 多个 running Step 取消传播 | 两个 specialist 均进入后取消，全部收敛，无 RUNNING 泄漏、无 OUTPUT | `test_cancellation_propagates_to_running_steps` |
| deadline | DEADLINE_EXCEEDED（含 starvation 下的 planning 准入超时分类） | `test_starvation_deadline_is_classified_correctly` |
| budget exhausted | BUDGET_EXHAUSTED | `test_dynamic_planning_lifecycle.py` |
| shutdown | SYSTEM_SHUTDOWN（planning 阶段）；既有 shutdown 全套回归通过 | `test_shutdown_during_planning_is_cancelled` |

## 12. Recovery Matrix

所有 recovery 测试断言：不重发 final、不重写 Memory、不恢复 Gate、不恢复
Store/Bindings。

| 场景 | RecoveryStatus | 证据 |
|---|---|---|
| POST_PLAN_PRE_EXECUTION | UNSUPPORTED（bindings 不可恢复） | `test_recovery_delivery_boundary.py::test_post_plan_checkpoint_is_not_resumable_without_bindings` |
| specialist 执行中断 / Store PREPARED 中断 | REQUIRES_RECONCILIATION（无 result 再水合） | `test_specialist_interruption_fails_closed_without_result_rehydration`、`test_store_prepared_interruption_fails_closed` |
| final Step SUCCEEDED 无 OUTPUT 事实 | UNSUPPORTED | `test_final_step_succeeded_without_output_journal_is_unsupported` |
| OUTPUT journaled 无 terminal | REQUIRES_RECONCILIATION（delivery unknown） | `test_output_journaled_without_terminal_is_delivery_unknown` |
| delivery unknown | REQUIRES_RECONCILIATION，不重发 | `test_delivery_unknown_never_resumes_or_resends` |
| delivered 已知、Memory 未知 | REQUIRES_RECONCILIATION（人工协调） | `test_delivered_with_unknown_memory_requires_manual_coordination` |
| Memory committed 无 terminal | REQUIRES_RECONCILIATION，不重写 | `test_memory_committed_without_terminal_never_rewrites_or_resends` |
| terminal 成功（delivered + Memory failed） | TERMINAL，不恢复 | `test_terminal_delivered_memory_failed_is_terminal_not_resumed` |
| corrupt/duplicate/out-of-order Journal、v1/v2 Snapshot、fingerprint mismatch | CORRUPTED / INCOMPATIBLE_SCHEMA / fail closed | `test_recovery_tail_corruption.py`、`test_recovery_version_compatibility.py` |

`test_recovery_validator_has_no_write_or_deliver_capability` 证明
RecoveryValidator 只读，无任何重发/重写/恢复方法。

## 13. Trace / Journal / Frontend Evidence

- Trace：span start/end 故障既有缝回归通过；失败路径（specialist 失败、
  delivery unknown、Memory 失败）的 Trace 无正文泄漏（`test_stage2_5_wp6_security.py`）。
- Journal：sequence 唯一单调、重复/倒退拒绝（`test_partial_publication_sequence.py`、
  `test_recovery_validation.py`）；OUTPUT 只保存 digest/length；safe payload
  非法字段拒绝（`test_journal_safe_projection.py`）。
- Metrics：高基数 label 拒绝/规范化、Metrics recorder 失败不重复执行业务
  （`test_metrics_label_policy_wp5.py`）；blocking executor wait 指标可观测
  （`test_stage2_5_wp6_starvation.py`）。
- Frontend/Projection：duplicate 幂等、sequence 倒退拒绝、未知 control
  event 安全忽略（`test_runtime_projection.py`）；RUN_COMPLETED 前缺失
  control event 仍给出终态分层事实且文案不鼓励重试
  （`test_stage2_5_wp6_frontend.py`）；specialist raw 不显示
  （`test_frontend_multi_agent_status.py`、`test_wp5_security_matrix.py`）。

## 14. Security Matrix

安全标记：`SECRET_USER_INSTRUCTION`、`SECRET_PLANNER_OUTPUT`、
`SECRET_SPECIALIST_RESULT`、`SECRET_SYNTHESIS_INPUT`、`SECRET_FINAL_OUTPUT`、
`SECRET_EXCEPTION_MESSAGE`、`\\internal\private\case.dat`。

真实 Shape 3 四类运行各扫一次（logs/caplog、exception/repr、Runtime Events、
Journal、Trace、Metrics、Snapshot、Checkpoint、Recovery result、frontend
status、Memory、OUTPUT 正文）：

| 运行 | 结果 |
|---|---|
| Shape 3 成功 | 全通道无泄漏；正文只进唯一 OUTPUT 与完整 Memory exchange（既有 `test_wp5_security_matrix.py`） |
| specialist 失败 | 无 OUTPUT、无 Memory；异常消息不泄漏（`test_specialist_failure_security_matrix`） |
| delivery unknown | OUTPUT 一次（Journal 仅 digest）；unknown 不写 Memory；前端“避免重复执行”且不含“重试”（`test_delivery_unknown_security_matrix`） |
| Memory 失败 | OUTPUT 一次；Memory 无 exchange；无泄漏（`test_memory_failure_security_matrix`） |

全部通过。

## 15. Planning Executor Capacity Result

测量（`tests/test_stage2_5_wp6_starvation.py`）：

- worker 数：`process_blocking_executor.max_workers=4`。
- pending 上限：`max_pending_tasks=8`（有界）。
- 占满 worker+pending 后启动新 Run planning：planning 在准入队列等待，
  planner model 未进入；`runtime_blocking_executor_wait_seconds` 记录等待。
- Run deadline 命中：`DEADLINE_EXCEEDED`（不是 AGENT/Planning 错误）。
- 释放资源后：Run 恢复并成功，无重复执行、无重复输出。
- 取消：WP6 修复后同一事件循环上的取消可传播（`test_starvation_cancel_converges_from_same_loop`）。

实施中发现并修复的合同缺陷：

```text
真实失败：planning 的 executor 准入等待是同步轮询，直接占用事件循环线程；
无 Run deadline 且 executor 被占满时，同一事件循环上的取消/断连无法到达，
Run 无法收敛（潜在 P1：无限等待/取消无法收敛）。
根因：UnifiedPlanningModelAdapter.generate_plan 在 asyncio 上下文内同步调用
BoundedBlockingExecutor.submit，准入循环不 yield。
修改：将 submit 放入 asyncio.to_thread（最小修复，不新增线程池、不改变
准入语义、不提高全局 timeout/worker 数）。
验证：取消收敛测试、deadline 分类测试、恢复测试全部通过。
```

判定：`ACCEPTED_P2`（容量/性能风险，保留为 Known Limitation；不是 P1，
因为修复后取消可收敛、pending 有界、deadline 分类正确、释放后恢复、无
重复执行/输出）。

## 16. Stability / Flakiness

重复稳定性：关键并行/部分发布/恢复测试集
（`test_stage2_5_wp6_parallelism.py`、`test_stage2_5_wp6_delivery_faults.py`、
`test_stage2_5_wp6_recovery.py`、`test_partial_publication_sequence.py`、
`test_multi_agent_execution.py`）循环 20 轮：

```text
PASS runs: 20 / 20
flaky 现象：无
```

## 17. RC Gate Result

`tests/test_stage2_5_rc_gate.py` 聚合（不重复实现业务逻辑）：

- 54/54 必选 RC 场景映射真实测试并全部 PASS。
- 目录计数与支持报告一致（FaultPoint 53/44/9；RC 必选 30/30 SUPPORTED 已测）。
- 全部目录 test id 指向真实文件。
- P0=0、P1=0、P2=1；`assess_release_gate` 硬条件全部通过 -> `PASS`。
- 任一硬条件失败（P0/P1/full suite/contract/docs/resource/security/场景）
  -> `FAIL`（参数化覆盖）。

**RC Gate：PASS**

## 18. Compatibility and Full Regression

| 命令 | 结果 |
|---|---|
| `uv run pytest -q tests/test_stage2_5_wp6_*.py tests/test_stage2_5_rc_gate.py`（WP6 专项） | 66 passed |
| WP1～WP5 关键回归（dynamic lifecycle、multi-agent execution、output gate、step completion、final memory、trace、journal、recovery、frontend、fault 报告） | 全部通过 |
| `uv run pytest -q`（全仓） | 1412 passed, 42 subtests passed |
| `uv run python -m compileall -q core tests server.py main.py` | PASS |
| `git diff --check` | PASS（仅 LF/CRLF 提示，无 whitespace error） |

兼容性：事件 schema 保持 v2；Journal allowlist 不变；Legacy/static 路径不创建
Gate 的既有行为保持；Memory schema 不变（仅新增故障缝参数，默认 None）；
`docs/runtime/stage2_5_trace_contract_v1.md` 无字段变化。

## 19. Final Known Limitations

只保留真实未解决项：

- KL-01：Planning executor 饥饿 P2 保留：executor 被占满时 planning 必须等待
  准入；有界、可恢复、deadline 分类正确、取消可收敛，但容量/性能存在风险。
- KL-02：动态 Run 的 bindings/results 不跨进程恢复；进程中断后 fail closed。
- KL-03：journal-first 发送无法从本地 enqueue 失败判断消费者是否最终看见文本，
  必须保留 `OUTCOME_UNKNOWN`。
- KL-04：外部事件为兼容仍是 OUTPUT 在 STEP_COMPLETED 前；执行状态与交付状态
  需按合同理解。
- KL-05：首版只支持 required dependencies，不支持部分结果降级或 optional edges。
- KL-06：“任意多 Agent”受 Registry 与硬资源上限约束，不是无界执行。
- KL-07：不承诺分布式 exactly-once delivery；DELIVERED 不是用户确认阅读。
- KL-08：terminal-event publication 失败只保证安全显式失败，不承诺分布式
  exactly-once。
- KL-09：81 个非必选目录场景为 CONTRACT_ONLY 合同级覆盖，无独立注入缝。

## 20. Final Status

```text
WP6 status: PASS
Stage 2.5 final status: PASS
Architecture deviations: 0
P0 findings: 0
P1 findings: 0
P2 findings: 1

Required RC scenarios: 54/54 passed
Fault points total: 53
Fault points supported: 44
Fault points contract-only: 9
Required supported fault points tested: 30/30

Shape 0-3 E2E: PASS
At-most-once final output: PASS
Internal result isolation: PASS
Memory atomicity and idempotency: PASS
Recovery no-redelivery: PASS
Recovery no-memory-recommit: PASS
Trace Contract v1: PASS
Journal safe projection: PASS
Frontend projection: PASS
Security matrix: PASS
Planning executor starvation: ACCEPTED_P2

WP1-WP5 regression: PASS
Full repository tests: 1412 passed, 42 subtests passed
compileall: PASS
git diff --check: PASS
RC Gate: PASS

Ready for GPT final review: YES
Ready to freeze Stage 2.5: YES
```

说明：

- `Architecture deviations: 0`：未偏离 Stage 2.5 架构共识；第 15 节记录的是
  测试发现的合同缺陷的“最小修复”，不是架构偏差。
- “Ready to freeze Stage 2.5: YES”仅在以下条件全部满足时填写：P0=0、P1=0、
  所有必选 RC 场景通过、必选 fault point 均 SUPPORTED 且有测试、Shape 0～3
  通过、at-most-once 通过、Memory 原子性通过、Recovery 不重发/不重写、
  Trace/Journal/Frontend/Security 通过、WP1～WP5 回归通过、全仓通过、
  compileall 通过、diff check 通过、RC Gate 通过、无未批准架构偏差。上述
  条件全部满足。

完成所有代码、测试、RC Gate 和结果文档后停止，等待 GPT 最终审查。
