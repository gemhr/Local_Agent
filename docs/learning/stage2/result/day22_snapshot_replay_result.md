# 阶段二第 22 天改造结果

## 1. 本日目标与准确能力边界

第 22 天完成的是“可验证快照 + 有限 Journal Tail 归约 + Tool 副作用恢复决策 + 恢复前置条件评估”。系统可以回答某个快照是否损坏、是否匹配当前 Plan、Journal Tail 是否冲突、Tool Invocation 是否需要人工对账，以及 Step Boundary 是否缺少后续执行所需结果。

`RESUMABLE` 只表示未来恢复的安全前置条件满足；`TERMINAL` 只表示 Run 已安全终结。两者都不表示 Runtime 已经能够恢复执行或重新输出完整回答。本日没有调用 Model、Tool、RAG 或 Compensation，也没有重建 Runtime Shell。

## 2. Snapshot Contract

`RunSnapshot` v1 是不可变、安全、无业务正文的恢复输入，包含 Plan/State/Budget 投影、Checkpoint Kind、Runtime Activity、Journal Watermark 与 SHA-256 v1 摘要。Plan、Result、Final Output 等可能含正文的字段只保存 `present/length/digest`。

## 3. Plan Fingerprint

`PlanFingerprinter` 是唯一 Owner。Fingerprint 覆盖静态 Plan 语义，不含 `created_at`、StepStatus、运行时 attempt、budget、span 或 event sequence。持久化 PlanSnapshot 与当前 Plan 分别计算后比较；不匹配固定返回 `PLAN_MISMATCH`。

## 4. Safe State / Budget Projection

State 投影保存真实 Run/Step 状态、时间、安全错误码和正文摘要；Budget 投影保存 limits/used/reserved/remaining 与 reservation count。恢复分析不清零 reservation、不递增 attempt、不根据摘要反推正文，也不把 `attempt_count=null` 当作 0。

## 5. Snapshot Store

`InMemorySnapshotStore` 与 `SQLiteSnapshotStore` 均为 append-only。相同 ID 和相同有效内容是 Duplicate；相同 ID、不同内容是 Conflict；读取时重新验证 schema、envelope 与 digest。生产环境应装配哪一种 Store 仍未决定。

## 6. Claim Gate

Checkpoint 通过 `SchedulerClaimGate` 阻止新 Claim 进入捕获窗口。Gate 只解决 Claim 与捕获的并发边界，不暂停 Model/Tool/RAG，也不是恢复 Scheduler。

## 7. Checkpoint Barrier

Barrier 按固定顺序关闭 Claim、等待既有 Claim、采样 Activity/State/Budget、捕获 Journal Watermark、再次采样并保存快照。`REQUIRE_QUIESCENT` 不满足稳定条件时不保存；`AUDIT_ONLY` 只能形成非静止审计证据。

## 8. Runtime Activity

Activity Snapshot 包含 Claim、running step、budget reservation、Model/Tool/Retrieval、detached worker、event publication、step worker 和 state-event transition 的计数与 epoch。它是运行时真实 Tracker 的内容无关投影，不从 AgentState 猜测外部 Worker。

## 9. Quiescent Detection

静止要求所有活动计数为 0、无 running step、无 reservation、无 detached worker，并且两次采样之间 state-event transition epoch 未变化。瞬时计数经历 `0 -> 1 -> 0` 也会被 epoch 检出，避免 ABA 式误判。

## 10. Journal Watermark

Event Channel 采用 journal-first；Watermark 在 publish lock 内捕获，不越过半次 publication。Snapshot 保存 `last_journal_sequence`，Recovery 只读取其后的 Tail。合法 sequence 只要求严格递增，不要求数值连续。

## 11. Recovery Contract

`RecoveryStatus` 固定为 `CORRUPTED / INCOMPATIBLE_SCHEMA / PLAN_MISMATCH / JOURNAL_GAP_OR_CONFLICT / UNSUPPORTED / REQUIRES_RECONCILIATION / TERMINAL / RESUMABLE`。`RecoveryAssessment` 现在同时返回 Tool Decisions 与 `ResumeDataAvailability`，并固定关闭所有执行和 replay 能力。

## 12. Recovery Validation Priority

优先级固定为：

```text
CORRUPTED
INCOMPATIBLE_SCHEMA
PLAN_MISMATCH
JOURNAL_GAP_OR_CONFLICT
UNSUPPORTED
REQUIRES_RECONCILIATION
TERMINAL
RESUMABLE
```

Tool 的人工对账或证据不足不能覆盖更高优先级的损坏、schema、Plan 或 Journal 错误；Step Boundary 缺少依赖输出采用固定 `UNSUPPORTED` 语义。

## 13. Journal Tail Validation

Validator 验证 run ownership、严格递增 sequence、Journal/Event schema、record digest、支持的 event type 和 terminal ordering。旧 Journal v1/v2 继续按原 digest source 验证；新 Tool Evidence 进入 safe payload 后自然参与新的 payload/event digest。append-only 旧记录不改写。

## 14. Limited Reducer

Reducer 只归约 Run/Step 状态、安全错误码、输出存在性、Budget Exhausted 元数据和 Tool Evidence。它不归约正文、不精确累计 Budget、不调用 Registry、不调用任何业务 Adapter。

## 15. RecoveryProjection

Projection 包含 reduced run status、stop/cancellation reason、Step 状态、原 Budget Snapshot、last applied sequence、terminal fact、output presence 与 budget exhausted。`output_available=true` 只说明存在摘要或 OutputDelta 元数据，不等于输出正文可重建。

## 16. Tool Event Evidence

项目保持 Runtime Event schema v2 与 Journal schema v2，并在 Tool payload 内增加显式版本：

```text
tool_evidence_schema_version = 1
```

新 Tool Started/Completed Journal payload 安全携带：

```text
tool_evidence_schema_version
side_effect_kind
idempotency_kind
idempotency_key_digest
replay_supported
side_effect_state
compensation_state
retry_disposition
outcome_classification
execution_detached
worker_terminated
provider_started
safe_error_code
invocation_identity_digest
attempt_identity_digest
```

新事件不持久化 raw invocation/attempt ID、原始 idempotency key、resource key/digest、arguments、output、路径、原始异常或 compensation 正文。旧 v1/v2 Tool payload 仍按旧 allowlist 和旧 digest 读取；缺失字段明确为 Unknown/`None`，不会查询当前 Tool Registry 补齐。相同 Event ID 但恢复字段不同会因 event digest 不同返回 `EVENT_ID_CONFLICT`。

真实字段 Owner 如下：

| 恢复事实 | 真实 Owner |
|---|---|
| `side_effect_kind` | `ToolExecutionSpec.side_effect_kind`，由执行时 Adapter Contract 提供 |
| `idempotency_kind` | `ToolExecutionSpec.idempotency` / `OperationIdempotency` |
| `idempotency_key` | `ToolInvocation.idempotency_key`；Event 只保存其 digest |
| `replay_supported` | `ToolExecutionSpec.supports_idempotency_replay` |
| `side_effect_state` | `AttemptSideEffectTracker`，并仅接受 Adapter 的权威状态解析 |
| `compensation_state` | `ToolExecutionError.compensation_attempted/compensation_succeeded` 的安全映射 |
| `retry_disposition` | `retry_disposition_for` 与最终 `ToolExecutionResult/Error` |
| `outcome_classification` | 成功时 `ToolExecutionStatus`；失败时 `ToolErrorCategory` |
| `execution_detached` | Attempt Executor 的 timeout/cancellation worker 生命周期判断 |
| `worker_terminated` | Attempt Executor/Concurrency Controller 的 worker 生命周期事实 |

Trace Attributes 只是上述安全事实的观察投影，不是业务 Owner。

## 17. Tool Recovery Decision

新增不可变 `ToolRecoveryDecision` 与纯分析 `ToolRecoveryDecisionEngine`。Decision 固定为：

```text
NO_ACTION_REQUIRED
SAFE_RETRY_CANDIDATE
DO_NOT_RETRY
MANUAL_RECONCILIATION
INSUFFICIENT_EVIDENCE
```

所有 Decision 都有 `automatic_action_allowed=false`。Evidence 只按 `invocation_identity_digest` 分组；Attempt 按 event sequence 配对并按 retry sequence 排序。同名 Tool 的不同 Invocation 不合并；缺身份不按 Tool 名猜测；Completed 无 Started、重复 Completed 或身份冲突均 fail closed。

## 18. Tool Decision Matrix

| 条件 | Decision | 说明 |
|---|---|---|
| Tool 成功 Completed、Step 权威成功、无 unknown/detached/compensation failure | `NO_ACTION_REQUIRED` | 已完成，不因存在副作用而重跑 |
| `side_effect_kind=NONE` 且 Invocation 未完成、无 committed/unknown/detached | `SAFE_RETRY_CANDIDATE` | 仅是未来候选，不执行 |
| `IDEMPOTENT_WITH_KEY` + key digest + replay supported，无风险 | `SAFE_RETRY_CANDIDATE` | 不代表 arguments 可恢复 |
| `NON_IDEMPOTENT` + `COMMITTED` + Tool/Step 权威完成 | `DO_NOT_RETRY` | 不阻止 Run 成为 Terminal |
| `NON_IDEMPOTENT` + `COMMITTED` 但缺权威完成 | `MANUAL_RECONCILIATION` | 禁止重试 |
| Outcome Unknown、post-commit response failure、仍运行 detached worker | `MANUAL_RECONCILIATION` | 需要外部对账 |
| Compensation Failed | `MANUAL_RECONCILIATION` | 不自动再次补偿 |
| 未完成 Invocation 缺 side effect/idempotency/稳定身份/关键 outcome | `INSUFFICIENT_EVIDENCE` | 不查询当前 Registry 猜测 |

## 19. Step Result / Dependency Output Boundary

真实审计结果：

- `ParallelExecutor` 的 `StepExecutionOutcome.result` 只存在于当次 `ParallelExecutionReport`，不会写入 `AgentState`。
- `AgentState` 只持有 StepStatus 与最终 `final_output`，没有 Step Result Store。
- `StepExecutionDriver.execute` 收到 `StepClaim` 与 `RunContext`，没有标准化的依赖结果重建输入。
- `CheckpointCoordinator` 调用 `AgentStateSnapshot.from_agent_state` 时没有传入 `step_results`。
- Snapshot 中即使存在 Result Digest，也只有摘要，不能作为结果正文。
- `RunCoordinatorResult` 汇总状态与 ID，不重建最终用户回答。

`ResumeDataAvailability` 固定包含：

```text
pending_steps_present
completed_dependency_results_required
completed_dependency_results_available
result_rehydration_supported
output_reconstruction_supported
```

PRE_RUN 没有历史结果需求时可满足 `RESUMABLE` 前置条件。STEP_BOUNDARY 若 Pending Step 依赖已完成 Step，则当前没有权威结果正文或 Rehydration Owner，固定返回 `UNSUPPORTED`，理由为 `DEPENDENCY_OUTPUT_UNAVAILABLE` 与 `STEP_RESULT_REHYDRATION_UNSUPPORTED`。TERMINAL 可安全终结，但不宣称完整输出可重建。

## 20. RecoveryAssessment

最终 Assessment 至少包含 status、reasons、tool decisions、resume data、resume prerequisites 与所有 replay flags。本日固定：

```text
automatic_resume_supported = false
model_replay_allowed = false
tool_replay_allowed = false
retrieval_replay_allowed = false
output_reconstruction_supported = false
```

`MANUAL_RECONCILIATION` 与 `INSUFFICIENT_EVIDENCE` 映射为 `REQUIRES_RECONCILIATION`；`SAFE_RETRY_CANDIDATE` 不触发动作；已权威完成的 `DO_NOT_RETRY` 和 `NO_ACTION_REQUIRED` 不单独阻止 `TERMINAL`。

## 21. Crash Window

已防护 State Commit 到 Event Publish、两次 Activity 采样之间完整 transition、Journal append 到 Channel enqueue、以及 Snapshot Watermark 到 Tail read 的变化窗口。仍未解决的是跨进程 Runtime Shell、外部 Tool 的 exactly-once、业务结果正文存储与生产级 Snapshot Store 装配。

## 22. Zero Replay Proof

Decision Engine 只接收 `ToolRecoveryEvidence + RecoveryProjection`；Recovery Validator 只接收 `RunSnapshot + Journal Tail + Current Plan`。`AccountingFake` 的 `model/tool/retrieval/compensation/resource_lease/idempotency_store_write` 六类计数全部保持 0。所有能力开关由 Contract 拒绝构造为 true。

## 23. Security

恢复路径不读取 Tool output 正文、不重建 arguments、不保存 raw key、resource key、路径或原始异常。身份与 idempotency key 只保存 lowercase SHA-256。Reason 文本使用固定 allowlist，不拼接 payload、SQL、路径或 traceback。新旧 payload 都由 event-type allowlist 和 digest 校验。

## 24. Legacy Boundary

旧 Runtime Event v1/v2、Journal v1/v2、旧 SQLite Journal 与旧 digest 继续可读。旧 Tool 事件缺失的新字段保持 Unknown，不能使用当前 Tool Registry 推断历史事实。新旧记录可混合出现在 Tail，每条记录按自身版本验证。

## 25. Bad Case

固定模板字段为“类型 / 风险 / 处理 / 验证”；“真实审计”来自当前代码数据流，“假设构造”用于 fail-closed 回归。

| # | Bad Case | 类型 | 风险 | 处理 | 验证 |
|---|---|---|---|---|---|
| 1 | Snapshot Plan 与当前 Plan 不同仍恢复 | 假设构造 | 错误执行定义 | `PLAN_MISMATCH` | fingerprint 测试 |
| 2 | Snapshot sequence 高于 Journal | 假设构造 | 丢失历史 | `JOURNAL_GAP_OR_CONFLICT` | recovery validation |
| 3 | 合法 numeric gap 被当损坏 | 假设构造 | 拒绝合法 Tail | 只要求严格递增 | reducer 测试 |
| 4 | RUNNING Step 自动改回 PENDING | 假设构造 | 重复执行 | 保持 RUNNING 并对账 | recovery state 测试 |
| 5 | Non-quiescent Audit 返回 RESUMABLE | 假设构造 | 活动任务被重放 | `REQUIRES_RECONCILIATION` | quiescence 测试 |
| 6 | OutputDelta 被当完整回答 | 真实审计 | 泄漏/伪恢复 | 只投影 presence | terminal 测试 |
| 7 | BudgetExhausted 推算精确账本 | 假设构造 | 预算漂移 | 保留 Snapshot Budget | reducer 测试 |
| 8 | Recovery 调用真实 Model | 假设构造 | 重复计费 | replay flag false | zero replay 测试 |
| 9 | Tool Started 被直接重跑 | 假设构造 | 重复副作用 | 只形成 Decision | decision 测试 |
| 10 | 第二个 RunCompleted 被接受 | 假设构造 | terminal 冲突 | Tail fail closed | tail validator |
| 11 | 旧 Event 缺 span 被判损坏 | 真实兼容边界 | 旧数据不可读 | 旧 digest 规则 | Journal v1 测试 |
| 12 | 未知 Event 静默跳过 | 假设构造 | 状态遗漏 | `UNSUPPORTED` | event type 测试 |
| 13 | 用当前 Tool Registry 猜旧 Event | 真实审计风险 | 篡改历史语义 | 缺字段为 Unknown | legacy evidence 测试 |
| 14 | NON_IDEMPOTENT + COMMITTED 标安全重试 | 假设构造 | 重复外部写 | Completed 为 `DO_NOT_RETRY`，否则 Manual | decision matrix |
| 15 | 幂等 Tool 缺 Key 仍标安全 | 假设构造 | 无法去重 | Manual | 参数化测试 |
| 16 | 已成功 Tool 统一要求人工对账 | 假设构造 | 阻塞正常终态 | `NO_ACTION_REQUIRED`/`DO_NOT_RETRY` | terminal integration |
| 17 | Completed 无 Started 被静默接受 | 假设构造 | 丢失 attempt 边界 | Manual fail closed | pairing 测试 |
| 18 | 同名 Tool 的不同 Invocation 合并 | 假设构造 | 错误关联 | 只按 invocation digest | grouping 测试 |
| 19 | Result Digest 当真实 Result | 真实审计边界 | 后续 Step 输入为空 | Digest 永不等于正文 | resume data 测试 |
| 20 | Step Boundary 丢依赖输出仍 RESUMABLE | 假设构造 | 错误继续执行 | `UNSUPPORTED` | dependency gate |
| 21 | Terminal 被描述成可恢复完整回答 | 真实审计边界 | 用户输出伪恢复 | output reconstruction false | terminal 测试 |
| 22 | SAFE_RETRY_CANDIDATE 被直接执行 | 假设构造 | 重复调用 | automatic action false | immutable contract |
| 23 | Compensation Failure 自动再补偿 | 假设构造 | 二次破坏 | Manual，不调用 compensation | decision/zero-call 测试 |
| 24 | Tool Decision 修改 Runtime 状态 | 假设构造 | 分析产生副作用 | 纯不可变输入输出 | zero-call 测试 |

## 26. 测试结果

新增：

- `tests/test_tool_event_evidence.py`
- `tests/test_tool_recovery.py`
- `tests/test_recovery_decision_integration.py`
- `tests/test_tool_execution.py` 的真实事件 Owner 断言

最终验收结果见本节收尾记录：

- 指定目标 pytest：`145 passed, 4 subtests passed`。
- 指定目标加三份新增测试：`178 passed, 4 subtests passed`。
- 全仓 pytest：`616 passed, 42 subtests passed`。
- compileall：通过。
- `uv lock --check`：通过，`Resolved 157 packages`。
- `git diff --check`：通过；只有仓库既有的 LF -> CRLF 提示，无 whitespace error。

## 27. 未完成事项

- 没有自动 Resume。
- 没有完整 Replay Engine。
- 没有重新执行 Model/Tool/RAG。
- 没有自动 Tool Retry。
- 没有自动 Compensation。
- 没有 Runtime Shell 重建。
- 没有完整正文恢复。
- Snapshot Store 尚未决定生产装配位置。
- 没有自动 Checkpoint Policy。
- 默认 `/api/chat` 未迁移。
- 不是 Event Sourcing。
- 不是 Exactly-once Recovery。

## 28. 面试表达

这次改造没有把“有快照”包装成“能恢复”。先用版本化、可校验且无正文的 Snapshot/Journal 证明历史，再把 Tool Invocation 按稳定 digest 分组，基于执行时真实 Contract 与 Attempt Tracker 做副作用决策。最后单独审计 Step Result 数据链，发现结果正文没有持久化 Owner，因此在 Step Boundary 缺依赖输出时明确返回 Unsupported。整个分析面保持零执行，`RESUMABLE` 只是安全前置条件，不是 Resume 功能。

## 29. 需要带回 ChatGPT 审查的信息

- Tool Evidence 采用 Runtime Event v2 内嵌 payload schema v1，而不是静默改变旧 v2 解释。
- 新事件不保存 raw Invocation/Attempt ID，只保存 digest；旧事件仍按旧 payload/digest 兼容读取。
- `side_effect_kind/idempotency/replay_supported` 来自执行时 Spec，不能从恢复时 Registry 补齐。
- `side_effect_state` 来自 AttemptSideEffectTracker 与权威 Adapter 结果。
- Tool Decisions 全部 `automatic_action_allowed=false`。
- Step Boundary 依赖已完成 Step 时，当前没有 Result Store/Rehydration Owner，固定 `UNSUPPORTED`。
- Terminal 的 `output_available` 只是存在性；`output_reconstruction_supported=false`。
- Snapshot Store 生产装配、自动 Checkpoint Policy、Runtime Shell、Replay Engine 与默认 API 迁移均留待后续。
