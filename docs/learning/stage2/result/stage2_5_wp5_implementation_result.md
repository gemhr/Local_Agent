# Stage 2.5 WP5 实施结果（Observability / Persistent Facts / Security Audit / Client Interpretation Layer）

## 1. Executive Summary

WP5 完成。本阶段在不触碰 WP6（完整故障矩阵、RC Gate、最终验收）的前提下，
冻结了 Stage 2.5 的 Trace Contract v1，补齐 Journal 安全事实投影、Final
Memory 原子写入、Delivery/Memory 观测分层、Runtime Projection、前端最小
多 Agent 状态展示，并强化 Snapshot/Recovery 交付边界（不重发、不重写）。

状态：

- Trace Contract v1：启用（`docs/runtime/stage2_5_trace_contract_v1.md`）。
- Journal 安全投影：启用（OUTPUT 只保存 digest/length；reducer 投影分层事实）。
- Memory 一致性：启用 SQLite 单事务 `append_exchange_atomic` + 幂等键。
- Runtime Projection：启用（纯事件投影，幂等 + sequence 合同）。
- 前端：启用共享状态文案模型（兼容 Legacy 事件）。
- Recovery：POST_PLAN 与 delivery/Memory 边界 fail closed，不重发、不重写。

测试总结：全仓 `1346 passed, 42 subtests passed`；WP5 专项 10 个测试文件
全部通过；`compileall` 与 `git diff --check` 通过。

## 2. Source Audit Before Changes

审计结论（真实源码位置）：

| 组件 | 审计前 owner / 行为 |
|---|---|
| `core/runtime/events.py` | 已具备 Journal allowlist 与 `to_journal_dict`；OUTPUT_DELTA 已只保存 digest/length；STEP/PLAN/RUN 事件缺少分层事实字段 |
| `core/runtime/journal_tail_reducer.py` | 已投影 Step/Run/模型/Tool/Retrieval；PLANNING_STARTED/PLAN_CREATED 在 IGNORED 集合，reducer 未投影规划事实与 delivery/Memory 分层事实 |
| `core/runtime/tracing.py` | Span 原语、SAFE/DENIED 属性集合已存在；operation 校验不允许 `.`，无法承载 `runtime.run` 等合同命名；无 delivery/Memory span |
| `core/runtime/metrics.py` | 已有 planning/step/run/model/tool/retrieval 指标；缺少 step 分类标签、multi-agent、synthesis、delivery、Memory 指标 |
| `core/runtime/output_gate.py` | 已有 DeliveryStatus/OutputGateState；无 delivery span、无 delivery 指标 |
| `core/runtime/final_memory_writer.py` | WP4 delivered-only 已建立；user/assistant 两次 `add_message` 非事务，`_written` 失败后重置（同一 Run 可再次调用） |
| `core/memory_manager.py` | SQLite 单条写入自提交；无 exchange 表、无幂等键、无原子提交；历史读取不过滤不完整 exchange |
| `core/runtime/recovery_validation.py` / `recovery_contract.py` | 已 fail closed；无 delivery/Memory 分层判定与原因码 |
| `core/runtime/stream_adapter.py` | control allowlist 已存在；RUN_COMPLETED 未携带 safe_error_code/分层事实 |
| `main.py`（PyQt6 前端） | 只处理 Legacy 编排事件字符串，未消费 runtime `event_type` 事件 |

## 3. Files Changed

| 文件 | 职责 |
|---|---|
| `core/runtime/trace_contract.py`（新增） | Trace Contract v1 常量、属性键集合、安全属性批量助手 |
| `core/runtime/tracing.py` | operation 校验允许 `.`；扩展 SAFE_SPAN_ATTRIBUTES 到全部 WP5 属性 |
| `core/runtime/planning.py` | 新增 `compute_plan_shape`（四种合法图 0/1/2/3，未知返回 `unknown`） |
| `core/runtime/events.py` | STEP_STARTED/STEP_COMPLETED/PLAN_CREATED/ERROR/RUN_COMPLETED 增加分层安全字段（legacy 可选） |
| `core/runtime/run_coordinator.py` | run/planning span 命名与属性；PLAN_CREATED shape；ERROR/RUN_COMPLETED 分层终态事实；OutputGate/MemoryWriter 注入 recorder |
| `core/runtime/parallel_execution.py` | `runtime.step` span 与属性；STEP_STARTED/STEP_COMPLETED 携带 agent/execution_kind/output_policy/delivery 字段 |
| `core/runtime/step_completion.py` | STEP_COMPLETED 携带 result_char_count/delivery_status/delivery_duration/execution_kind/output_policy |
| `core/runtime/output_gate.py` | `runtime.output_delivery` span；delivery 指标；部分持久化标记 |
| `core/runtime/final_memory_writer.py` | 单事务 `append_exchange_atomic`；写一次不可重试；`runtime.final_memory_commit` span；Memory 指标 |
| `core/memory_manager.py` | `append_exchange_atomic`、exchange 表、幂等键、历史读取过滤不完整 exchange |
| `core/runtime/multi_agent_driver.py` | `runtime.synthesis` span（真实 Shape 3 主链） |
| `core/runtime/metrics.py` | 新指标描述符、标签 allowlist（execution_kind/output_policy/shape/delivery_status/memory_commit_status/agent_id）、投影器扩展 |
| `core/runtime/recovery_contract.py` | RecoveryProjection 分层字段；新增 RecoveryReason |
| `core/runtime/journal_tail_reducer.py` | 投影 planning/plan_created/plan_shape/delivery/Memory 事实；PLANNING_STARTED/PLAN_CREATED 进入 REDUCED |
| `core/runtime/recovery_validation.py` | POST_PLAN fail closed；delivery/Memory 边界判定（UNSUPPORTED / REQUIRES_RECONCILIATION） |
| `core/runtime/stream_adapter.py` | STEP/PLAN/RUN 新安全字段进入 control allowlist |
| `core/runtime/runtime_projection.py`（新增） | 共享纯投影对象与 sequence 合同 |
| `core/runtime/multi_agent_status.py`（新增） | 前端状态文案模型与安全错误映射 |
| `main.py` | 前端改走共享状态文案模型 |
| `docs/runtime/runtime_error_code_catalog.md` | 补齐 WP5 错误码与 Recovery 原因码 |
| `docs/runtime/stage2_5_trace_contract_v1.md`（新增） | Trace Contract v1 文档 |
| 测试（10 个 WP5 专项 + 夹具/断言演进） | 见第 15 节 |

## 4. Trace Contract v1

见 `docs/runtime/stage2_5_trace_contract_v1.md`。核心字段：Run root、
Planning、Step、Synthesis、Output delivery、Final Memory 六类 span，
父子关系与敏感边界已冻结；缺失版本字段显式写 `not_configured`，不虚构。

## 5. Multi-Agent Span Topology

- Shape 0/1（单 Step）：`runtime.run -> runtime.planning -> runtime.step`
  -> `runtime.output_delivery` -> `runtime.final_memory_commit`。
- Shape 2（单 specialist -> synthesis）：specialist step span 完成后
  synthesis step span（内含 `runtime.synthesis`）开始。
- Shape 3（N 个 specialist -> synthesis）：每个 specialist 独立 step span
  且为 sibling；synthesis span 在所有依赖 span 结束后开始；delivery 与
  Memory span 挂在 final step span 下。

## 6. Journal Safe Fact Contract

逐事件 allowlist（`events._JOURNAL_PAYLOAD_FIELDS` + `validate_journal_payload`）：

- `PLANNING_STARTED`：schema/timeout 安全字段，无 query/prompt。
- `PLAN_CREATED`：plan_id/version/fingerprint/step_count/planning_source/shape。
- `STEP_STARTED`：status/agent_id/execution_kind/output_policy/dependency_count。
- `STEP_COMPLETED`：status/duration/result_char_count/delivery_status/
  delivery_duration/execution_kind/output_policy/safe_error_code，无 StepResult 正文。
- `OUTPUT_DELTA`：仅 `text_digest` + `text_length`（永不保存正文）。
- `ERROR` / `RUN_COMPLETED`：RunStatus/StopReason/safe_error_code/
  delivery_status/final_step_status/memory_commit_status。

Reducer（`LimitedJournalTailReducer`）：

- 投影 planning_started/plan_created/plan_shape、step states、final step
  成功、output publication attempted/journaled、delivery known/unknown、
  Memory commit 结果、Run terminal；
- 不把 OUTPUT digest 当作正文恢复；unknown 事件类型 fail closed 或安全忽略；
- sequence 重复/倒退拒绝（Validator）；partial persisted 事件保留事实；
- reducer 不产生第二次交付动作；不能把 `PUBLISHED` 推断为用户确认阅读。

## 7. Runtime Projection

`core/runtime/runtime_projection.py`：

- `RunProjection`：planning_status/plan_status/plan_shape/active_steps/
  completed_steps/synthesis_status/delivery_status/memory_commit_status/
  run_status/stop_reason/safe_error_code/output_journaled；
- 只由 Runtime Events 构建，无 raw 内容；
- 同一事件重复输入幂等；sequence 冲突/倒退拒绝；未知 control event 安全忽略；
- 不触发执行、重试或 Memory 写入；供前端与测试共享。

## 8. Metrics Contract

新增/确认：

- Planning：`runtime_planning_total{planning_source,status}`、
  `runtime_planning_duration_seconds{planning_source,status}`（确认既有）。
- Step：`runtime_step_total{execution_kind,output_policy,status}`、
  `runtime_step_duration_seconds{execution_kind,output_policy,status}`；
  不以 step_id 为 label。
- Multi-Agent：`runtime_multi_agent_runs_total{shape,status}`、
  `runtime_specialist_count{shape}`（Histogram）、
  `runtime_synthesis_total{status}`。
- Delivery：`runtime_output_delivery_total{status,error_code}`、
  `runtime_output_delivery_duration_seconds{status}`、
  `runtime_output_partial_persisted_total`。
- Memory：`runtime_final_memory_commit_total{status,error_code}`、
  `runtime_final_memory_commit_duration_seconds{status}`。
- Executor：`runtime_blocking_executor_pending`、
  `runtime_blocking_executor_wait_seconds`（保留，P2 仍可用）。

标签策略：error_code 来自有限枚举（bounded_values）；agent_id 只有固定
Registry Agent 时作为受控标签（否则聚合为 `other`）；禁止 run_id/trace_id/
step_id/session_id/path/query 作为标签。

## 9. Final Memory Consistency

- `MemoryManager.append_exchange_atomic`：单 SQLite 事务提交 user+assistant，
  任一写入失败整体回滚；
- 幂等键：`exchange_id` / `run_id` 唯一约束；同一 Run 重复提交抛
  `DUPLICATE_EXCHANGE`，绝不重发用户正文；
- `RunFinalMemoryWriter` 写一次不可重试：失败后 `_written` 保持 True，
  同一 Run 再次调用被拒绝，不自动重试；
- 历史读取（get_chat_history/get_messages_for_summary/get_all_messages/
  search_messages）只返回 legacy 消息或 `COMMITTED` exchange，不读取不完整
  exchange；
- 不存储 raw StepResult；只保存最终 delivered exchange。

## 10. Delivery / Memory Observability

四层事实在 Event/Trace/Journal/前端中区分：

```text
Final Step: SUCCEEDED
Delivery: DELIVERED
Memory: FAILED
Run: FAILED
error_code: FINAL_OUTPUT_MEMORY_COMMIT_FAILED
```

前端文案映射（`multi_agent_status.py`）：

- `FINAL_OUTPUT_DELIVERY_FAILED` -> 最终回答未能进入消息通道。
- `FINAL_OUTPUT_DELIVERY_UNKNOWN` -> 最终回答的交付状态无法确认。请先检查当前对话，避免重复执行。
- `FINAL_OUTPUT_MEMORY_COMMIT_FAILED` -> 回答已经交付，但未能保存到对话记忆。

unknown 文案绝不鼓励立即重试。

## 11. Frontend Status Model

- `main.py` 不再自行字符串拼装，改由 `format_frontend_status` 共享模型；
- 多 Agent 并行时多个 `STEP_STARTED` 显示多个 active specialist；
- OUTPUT_DELTA 继续只进入聊天正文；control event 只更新状态组件；
- RUN_COMPLETED 输出分层终态文案（已交付/交付未知/记忆失败/运行失败）；
- Legacy 事件（planning_started/delegate_*/synthesis_started）保持兼容；
- 不显示 specialist raw 结果与原始 instruction。

## 12. Snapshot / Recovery Boundary

- Bindings/StepResultStore/OutputGate/raw final 均不持久化、不恢复；
- DeliveryStatus 只能从 Journal 事实验证，不恢复 Gate；
- Memory commit 不能由 Recovery 自动重试；
- `OUTCOME_UNKNOWN` 永远 fail closed；
- POST_PLAN_PRE_EXECUTION 因 bindings 缺失不能 resume（fail closed）；
- Final Step SUCCEEDED 但无 OUTPUT journal 事实 -> UNSUPPORTED；
- OUTPUT journaled 但无 terminal / delivery unknown -> 标记交付不确定，
  不重发、不自动写 Memory；
- DELIVERED 已知、Memory commit 未知 -> 人工协调，防止重复 exchange；
- Memory commit 成功但 terminal 缺失 -> 不重写 Memory、不重新输出；
- 新增 RecoveryReason：`FINAL_OUTPUT_JOURNAL_FACT_MISSING`、
  `FINAL_OUTPUT_DELIVERY_UNKNOWN`、`FINAL_OUTPUT_MEMORY_COMMIT_UNKNOWN`、
  `MEMORY_COMMITTED_WITHOUT_TERMINAL`、`POST_PLAN_BINDINGS_NOT_RECOVERABLE`。

## 13. Security Matrix

真实 Shape 3 主链（`test_wp5_security_matrix.py`）使用
`SECRET_USER_INSTRUCTION`、`SECRET_SPECIALIST_RESULT`、
`SECRET_SYNTHESIS_INPUT`、`SECRET_FINAL_OUTPUT`、
`\\internal\private\case.dat` 扫描：

| 通道 | User instruction | Specialist result | Synthesis input | Final output |
|---|:---:|:---:|:---:|:---:|
| Binding 内存 | 允许 | N/A | 允许 | N/A |
| StepResultStore | 否 | 允许 | N/A | 允许 |
| OUTPUT 事件 | 否 | 否 | 否 | 允许一次 |
| Journal | 否 | 否 | 否 | digest/length only |
| Trace | 否 | 否 | 否 | length/status only |
| Metrics | 否 | 否 | 否 | 否 |
| Snapshot / Checkpoint | 否 | 否 | 否 | 否 |
| Error/repr/log | 否 | 否 | 否 | 否 |
| Memory | 原始 user 仅 delivered exchange | 否 | 否 | delivered final only |
| Frontend control state | 否 | 否 | 否 | 否 |
| Chat 正文 | 用户原输入由 UI 已有 | 否 | 否 | 允许一次 |

全部通过。

## 14. Shape 0～3 Observability Evidence

- Shape 0/1：单 Step 链路的 run/planning/step/delivery/memory span、
  STEP_STARTED/STEP_COMPLETED 安全字段、RUN_COMPLETED 分层事实。
- Shape 2/3：`test_multi_agent_trace.py` 证明 3 个 specialist step span 为
  sibling、synthesis span 在依赖结束后开始、delivery/memory span 挂 final
  step；`test_wp5_security_matrix.py` 证明全通道无泄漏。
- 前端投影：`test_frontend_multi_agent_status.py` 证明并行 active specialist
  文案与分层终态文案。

## 15. Tests and Commands

新增 WP5 专项测试：

- `tests/test_trace_contract.py`（扩展）
- `tests/test_multi_agent_trace.py`
- `tests/test_delivery_observability.py`
- `tests/test_journal_safe_projection.py`
- `tests/test_runtime_projection.py`
- `tests/test_final_memory_atomicity.py`
- `tests/test_recovery_delivery_boundary.py`
- `tests/test_frontend_multi_agent_status.py`
- `tests/test_wp5_security_matrix.py`
- `tests/test_metrics_label_policy_wp5.py`

命令与结果：

```text
uv run pytest -q <WP5 专项>            # 51 passed
uv run pytest -q                       # 1346 passed, 42 subtests passed
uv run python -m compileall -q core tests server.py main.py   # OK
git diff --check                       # OK
```

回归覆盖 WP1（registry/compiler/direct）、WP2（planning lifecycle/checkpoint）、
WP3（bindings/store/driver/synthesis）、WP4（completion/gate/delivery/
partial publication）、AgentRouter/Memory、Planning/Compiler、
Scheduler/ParallelExecutor、Coordinator、EventChannel/Emitter/Journal、
Trace/Metrics、Streaming/Frontend、Snapshot/Checkpoint/Recovery、
Legacy/static、Runtime E2E、全仓测试。

## 16. Compatibility and Regression

- 事件 schema 保持 v2；新增 payload 字段全部为 legacy 可选，旧记录可读；
- Stream control allowlist 向后兼容；Legacy 编排事件文案保持；
- Memory schema 通过 `ALTER TABLE` 迁移，旧数据库可直接升级；
- `runtime_step_duration_seconds` 标签集按合同演进为
  `{execution_kind,output_policy,status}`，旧事件投影为 `unknown`；
- WP1～WP4 关键回归与全仓均通过。

## 17. Deviations from Consensus

```text
No deviations from the Stage 2.5 architecture consensus were introduced in WP5.
```

## 18. Known Limitations After WP5

- Planning executor 饥饿 P2 仍存在（未扩大资源调度范围）；
- Gate/Store/Bindings 不可恢复（跨进程恢复未实现，属于后续阶段）；
- exactly-once delivery 未实现；
- DELIVERED 非用户确认阅读；
- AgentEvalOps 尚未正式接入；
- 完整故障矩阵和 RC Gate 属于 WP6；
- Stage 2.5 尚未最终完成。

## 19. WP6 Interface Needs

仅列出（不开始 WP6）：

- fault injection 点：Trace span start/end、Journal append、
  OUTPUT_DELTA publication、Memory exchange 事务；
- E2E 场景：Shape 0～3 主链、delivery failed/unknown、Memory failed；
- recovery 场景：POST_PLAN、specialist 中断、output journaled 无 terminal、
  delivery unknown、delivered+memory unknown、memory committed 无 terminal；
- Trace/Journal 验收字段：本文件第 4/6 节；
- UI 验收状态：本文件第 11 节；
- release gate 证据：全仓测试、compileall、diff check。

## 20. Final Status

```text
WP5 status: PASS
Architecture deviations: 0
P0 findings: 0
P1 findings: 0
P2 findings: 1 (Planning executor 饥饿，WP5 不扩大资源调度范围)
Trace Contract v1 enabled: YES
Multi-agent span topology correct: YES
Journal raw-content boundary enforced: YES
Runtime projection enabled: YES
Delivery and Memory status observable: YES
Delivered final Memory consistency protected: YES
Frontend multi-agent status enabled: YES
Delivery unknown warns against retry: YES
Recovery never re-delivers final output: YES
Recovery never re-commits final Memory: YES
WP5 security matrix passed: YES
WP6 capabilities implemented: NO
Ready for GPT review: YES
Ready to start WP6: YES
```

完成代码、测试和结果文档后停止，等待 GPT 审查。
