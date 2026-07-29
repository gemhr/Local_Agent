# 第 22 天第三轮 A：Recovery Validation

## 1. 本轮目标

本轮建立只读恢复评估边界：加载版本化 `RunSnapshot`，按固定优先级验证
Snapshot、Plan 与 Journal，对 Snapshot Watermark 之后的安全事件元数据做有限
归约，最后返回不可变 `RecoveryAssessment`。

`RESUMABLE` 只表示未来 Resume 的安全前置条件满足。本轮没有自动 Resume、自动
Replay、Runtime Shell、Scheduler 重建、Model/Tool/RAG 再执行、Event Sourcing
或默认 `/api/chat` 迁移。

## 2. Snapshot Activity 持久化审计

审计结论：

- `RuntimeActivitySnapshot` 已真实进入 `RunSnapshot.activity_snapshot`；
- Activity 位于 `digest_source()`，因此全部 Activity 字段进入 Snapshot Digest；
- `checkpoint_kind` 使用固定 `CheckpointKind` Enum，Payload 保存其固定字符串值；
- coordinated checkpoint 的 `quiescent` 只由 `CheckpointCoordinator` 根据
  Activity、Step State 与 Budget Reservation 共同计算；公共 Snapshot Contract
  仍负责 fail-closed 交叉校验；
- Non-quiescent Audit 持久化具体安全计数与 `activity_unknown`，不是只有一个
  布尔值；
- State、Budget、Journal Watermark 和 Activity 在 Claim Gate 暂停后形成短捕获
  边界；
- Snapshot v1 已被本仓库 JSON/SQLite Store 和包级 API 使用，但尚未形成正式外部
  兼容包，也没有进入默认生产聊天路径。

v1 当前持久化：

`claim_in_progress`、`running_step_count`、`budget_reservation_count`、
`model_attempts_active`、`tool_attempts_active`、`retrievals_active`、
`detached_tool_workers`、`detached_retrieval_workers`、
`event_publications_in_flight`、`step_workers_active`、
`state_event_transitions_in_flight`、`state_event_transition_epoch`、
`state_event_transition_observed`、`activity_unknown`、`captured_at`。

新增的短生命周期 `state_event_transitions_in_flight` 修正 Run/cleanup State Commit
到对应 Event Publish 之间的窗口；单调 `state_event_transition_epoch` 用于识别一次
转换在两个采样点之间完整开始并结束、计数已回到 0 的情况。当前 v1 尚无正式外部
兼容包，因此本轮修正 v1；Digest 语义随字段显式改变，没有静默沿用旧 Digest。
没有持久化 Worker ID、Task ID、线程 ID、Prompt、Tool/RAG 正文或路径。

Attempt 契约统一为：

```text
execution_started: bool
attempt_count: int | null
```

真实 `StepState` 没有权威 Attempt Owner，所以默认 `attempt_count=null`。Reducer
观察到 `STEP_STARTED` 时同样保持 `null`，不从 `started_at` 或事件存在性推导
0/1。旧 foundation 文档中的 0/1 表述已修正。

## 3. Recovery Contract

新增：

- `core/runtime/recovery_contract.py`；
- `core/runtime/recovery_validation.py`；
- `core/runtime/journal_tail_reducer.py`。

公共不可变结构包括 `RecoveryAssessment`、`RecoveryProjection`、
`ToolRecoveryEvidence`、`JournalTailValidation` 与 `JournalTailReduction`。恢复
错误只携带固定 Status/Reason，不携带原始异常或 Payload。

## 4. RecoveryStatus

固定状态：

```text
TERMINAL
RESUMABLE
REQUIRES_RECONCILIATION
INCOMPATIBLE_SCHEMA
PLAN_MISMATCH
CORRUPTED
JOURNAL_GAP_OR_CONFLICT
UNSUPPORTED
```

显式优先级：

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

代码通过 `RECOVERY_STATUS_PRIORITY` 与 `select_recovery_status()` 表达顺序，不依赖
Enum 值或声明次序。

## 5. RecoveryReason

已实现题目要求的全部固定 Reason，并为每个 Reason 建立唯一固定安全文本。另补充
Snapshot not found、Journal/Event schema unsupported、legacy checkpoint kind、
unmatched Model/Retrieval 和通用 runtime activity 等固定码。

Reason 文本不参与逻辑决策，也不包含 Snapshot JSON、Journal Payload、正文、路径、
SQL、原始异常或 Traceback。

## 6. Validation Priority

固定执行顺序：

1. Snapshot Schema；
2. Snapshot Digest；
3. Snapshot 内部交叉字段；
4. Activity；
5. persisted PlanSnapshot fingerprint；
6. current Plan fingerprint；
7. Journal `last_sequence`；
8. Snapshot/Journal sequence；
9. `read_after()`；
10. Tail Validator；
11. Limited Reducer；
12. Tool Evidence；
13. Assessment。

任一前置阶段失败立即生成高优先级 Assessment，不继续低优先级归约。

## 7. Snapshot Validation

Digest 校验直接比较规范 `digest_source()`，不会先运行低优先级恢复逻辑。随后验证
顶层 State 字段与 `AgentStateSnapshot` 一致、Plan/State Step ID 集一致、Activity
与 Step/Budget 计数一致，以及 quiescent Snapshot 不含运行 Step 或 Reservation。

Store 读取错误映射为固定 Recovery Status/Reason，不暴露 Store 原始错误。

## 8. Plan Validation

同时验证：

```text
fingerprint(snapshot.plan_snapshot) == snapshot.plan_fingerprint
fingerprint(current_plan) == snapshot.plan_fingerprint
```

前者失败是 `CORRUPTED`，后者失败是 `PLAN_MISMATCH`。没有使用 Plan ID、Version、
Step 数量等弱比较代替完整 fingerprint。

## 9. Checkpoint Kind Validation

- `PRE_RUN`：必须是 CREATED、sequence 0、无 execution_started/RUNNING Step 且
  quiescent；
- `STEP_BOUNDARY`：必须 quiescent、Run 非终态且无 RUNNING Step；
- `TERMINAL`：Snapshot 已终态，或 Tail 有唯一权威 `RUN_COMPLETED`；
- `NON_QUIESCENT_AUDIT`：必须 `quiescent=false`，Activity 必须能解释非静止。

Checkpoint capture 现在也在保存前校验 Kind；调用方传入不一致 Kind 返回
`CheckpointStatus.CORRUPTED`，不会保存或静默改写成另一个 quiescent Kind。
`OBSERVATION` 仅为未发布 foundation fixture 的 legacy 值，不是 Recovery 候选。

## 10. Snapshot / Journal Alignment

验证：

```text
snapshot.last_journal_sequence <= journal.last_sequence(run_id)
```

Snapshot 超前返回 `JOURNAL_GAP_OR_CONFLICT`。Journal 为空按 sequence 0 处理。
读取期间 Journal Watermark 变化会 fail closed，避免评估混用两个 Tail 边界。

## 11. Journal Tail Validator

逐条验证 run ownership、严格递增、sequence 大于 Snapshot Watermark、Journal/Event
schema、既有 `JournalRecord.verify()` Digest、支持的 Event Type，以及 terminal
唯一且最后。

Numeric sequence 只要求严格递增，不要求连续；`10 → 12 → 18` 合法。旧 v1 Event
缺少 span_id 继续可读。未知 schema/type fail closed。Validator 复用
`JournalRecord.verify()` 和 Store 的 terminal 约束，没有复制另一套 Digest 算法。

## 12. Limited Journal Tail Reducer

允许归约：

- `RUN_STARTED`；
- `STEP_STARTED`；
- `STEP_COMPLETED`；
- `CANCELLATION`；
- `TIMEOUT`；
- `BUDGET_EXHAUSTED`；
- `RUN_COMPLETED`。

明确验证后忽略业务执行效果：

- `OUTPUT_DELTA`；
- `MODEL_STARTED` / `MODEL_COMPLETED`；
- `TOOL_STARTED` / `TOOL_COMPLETED`；
- `RETRIEVAL_STARTED` / `RETRIEVAL_STAGE_COMPLETED` /
  `RETRIEVAL_COMPLETED`；
- `ERROR`（只把 `RUN_COMPLETED` 视为权威终态）。

这里的“忽略”不等于跳过验证：这些记录仍做 schema、Digest、顺序和配对检查。
Reducer 不 import、不持有、不调用任何 Adapter，也不修改真实 `AgentState`。

## 13. RecoveryProjection

不可变 Projection 包含：

```text
run_status
stop_reason
cancellation_reason
step_states
budget_snapshot
last_applied_sequence
terminal_event_seen
output_available
budget_exhausted
```

`step_states` 是只读 mapping；`budget_snapshot` 复用 Snapshot 的权威累计快照。
`output_available` 只表示 Snapshot 正文摘要存在或观察到 `OUTPUT_DELTA` 元数据，
不会重建用户正文。

## 14. In-flight Step

Snapshot 或 Reduced Projection 中的 `RUNNING` Step 保持 RUNNING，并使 Assessment
进入 `REQUIRES_RECONCILIATION`。不会改成 PENDING、成功或失败，也不会执行 Step。

`BLOCKED` 是未启动终态，不属于 in-flight。`attempt_count=null` 从不作为重试依据。

## 15. Non-quiescent Audit

`quiescent=false` 或 `checkpoint_kind=NON_QUIESCENT_AUDIT` 至少产生
`REQUIRES_RECONCILIATION`，除非已有更高优先级错误。

即使 Tail 有唯一 `RUN_COMPLETED`，Unknown Activity、Detached Worker、Tool Side
Effect 或其他非静止证据仍优先于 `TERMINAL`。

## 16. Tool Evidence

`TOOL_STARTED` / `TOOL_COMPLETED` 只生成安全 `ToolRecoveryEvidence`：

```text
tool_name
invocation_identity_digest
attempt_identity_digest
side_effect_kind
side_effect_state
retry_disposition
execution_detached
worker_terminated
safe_error_code
sequence
```

Invocation/Attempt identity 会再次 SHA-256，不保存正文。当前 Journal v2 没有
`side_effect_kind` 字段，因此该项明确为 `None`，不猜测 Tool 定义；这是第三轮 B 的
Event/Decision 接入点。

COMMITTED、UNKNOWN/OUTCOME_UNKNOWN、未结束 Detached Worker、compensation failure
和 post-commit response failure 统一返回 `REQUIRES_RECONCILIATION`。本轮没有
SAFE_RETRY/DO_NOT_RETRY/MANUAL/INSUFFICIENT_EVIDENCE 最终分类。

## 17. Budget 边界

`BUDGET_EXHAUSTED` 只设置 exhausted/stop reason 事实。Event 没有权威累计 Budget
Snapshot 时，Reducer 保留 Snapshot Budget 的 used/reserved/remaining，不做加减
或反推。

## 18. Terminal 判定

只有所有高优先级校验通过、Projection 是终态且没有非静止/副作用疑点时才返回
`TERMINAL`。第二个 `RUN_COMPLETED`、terminal 后业务事件、RunCompleted 与 Running
Step 并存都 fail closed。

终态 Assessment 同样不启动 Runtime，也不表示 Tool side effect 已自动对账。

## 19. 稳定捕获点

现有 `step_workers_active` 覆盖 Step State Commit 到 `STEP_COMPLETED` journal/入队
完成的窗口。确定性测试在 Step 已提交 SUCCEEDED、完成事件尚未 publish 时请求
checkpoint，结果为 `NOT_QUIESCENT` 且 Store 为空。

本轮增加 `state_event_transitions_in_flight`，覆盖 Run STARTED/terminal 与 cleanup
Step 的 State Commit → Event Publish 窗口；Checkpoint 同时比较捕获前后的单调
transition epoch。即使转换完整发生后计数已回到 0，epoch 变化仍将本次捕获标记为
`state_event_transition_observed=true` 并强制非静止。这些字段均进入 Activity
Payload、Digest 和 quiescence。

Event Channel 仍采用 Journal-first：append 完成后 sequence 即被消费，channel
enqueue 尚未完成时 publication 仍为 in-flight；publish lock 保证 Watermark 不会
越过半次 publish。没有长锁包围 Model、Tool 或 RAG。

## 20. Zero Replay Proof

集成测试创建会计数的 Model、Tool、Retrieval Fake。调用 `RecoveryValidator` 与
Reducer 后三者 `call_count` 都为 0。

Assessment 固定：

```text
automatic_resume_supported = false
model_replay_allowed = false
tool_replay_allowed = false
retrieval_replay_allowed = false
```

Contract 会拒绝把任何一个字段构造成 `true`。

## 21. Security

- Snapshot/Journal 正文不会进入 Projection、Evidence、Reason 或错误；
- OUTPUT_DELTA 只投影存在性；
- Tool identity 只保存摘要；
- Recovery Exception 只携带固定 code；
- 不返回路径、SQL、原始异常、Traceback；
- `repr(RunSnapshot)` 与 `repr(ToolRecoveryEvidence)` 不展开业务正文；
- 未知 schema/type 均 fail closed。

## 22. Bad Case

以下“真实风险”来自现有 Runtime 审计；“假设构造”用于 fail-closed 回归。

### Bad Case 1：Snapshot Plan 与当前 Plan 不一致仍恢复

- 类型：假设构造。
- 风险：在不同静态执行定义上继续运行。
- 处理：完整 fingerprint 不同返回 `PLAN_MISMATCH`。

### Bad Case 2：Snapshot Sequence 高于 Journal

- 类型：假设构造。
- 处理：返回 `JOURNAL_GAP_OR_CONFLICT`。

### Bad Case 3：合法 Numeric Gap 被当损坏

- 类型：假设构造。
- 处理：10→12→18 测试通过，只要求严格递增。

### Bad Case 4：RUNNING Step 自动改为 PENDING

- 类型：假设构造。
- 处理：Projection 保持 RUNNING，返回 `REQUIRES_RECONCILIATION`。

### Bad Case 5：Non-quiescent Audit 返回 RESUMABLE

- 类型：假设构造。
- 处理：固定由 reconciliation 优先级阻止。

### Bad Case 6：OUTPUT_DELTA 被当作完整回答

- 类型：真实数据边界。
- 处理：只设置 `output_available=true`。

### Bad Case 7：BUDGET_EXHAUSTED 被用于推算精确 Budget

- 类型：假设构造。
- 处理：Budget Snapshot 对象和值保持不变。

### Bad Case 8：Replay 调用真实 Model

- 类型：假设构造。
- 处理：Zero Replay Fake 计数为 0。

### Bad Case 9：Tool Started 被重新执行

- 类型：假设构造。
- 处理：只收集 Evidence；未配对 Started 返回 reconciliation。

### Bad Case 10：第二个 RUN_COMPLETED

- 类型：假设构造。
- 处理：Tail Validator 返回 terminal conflict。

### Bad Case 11：旧 Event 缺少 span_id 被判损坏

- 类型：真实兼容边界。
- 处理：既有 Journal v1 Digest 规则继续读取 nullable span。

### Bad Case 12：未知 Event 被静默跳过

- 类型：假设构造。
- 处理：未知 schema/type 返回 `UNSUPPORTED`。

### Bad Case 13：State Commit/Event Journal 窗口产生错误 Snapshot

- 类型：真实竞态。
- 处理：Step worker epoch 与新增 state-event transition counter 共同阻止
  quiescent 保存。

### Bad Case 14：attempt_count=null 被当作 0

- 类型：真实文档审计缺口。
- 处理：文档修正；Contract/Reducer 保持 `null`，不推导、不自增。

### Bad Case 15：Run 终态提交窗口未被 Step Worker 覆盖

- 类型：真实审计缺口。
- 触发条件：Run State 已提交终态，但对应 `RUN_COMPLETED` 尚未完成 Journal
  publication，此时并发请求 quiescent checkpoint。
- 故障表现：`step_workers_active=0`，Checkpoint 可能保存“终态 State + 尚未包含
  `RUN_COMPLETED` 的旧 Watermark”。
- 根因分析：`step_workers_active` 只覆盖 Step Worker 生命周期，不能表达 Run
  STARTED、Run terminal 和 cleanup Step 的 State Commit → Event Publish 转换。
- 修复方案：增加短生命周期 `state_event_transitions_in_flight`，用同一 scope
  包围 Run STARTED、Run terminal 和 cleanup Step 的 State Commit 与事件发布；
  该计数进入 Activity Payload、Digest 和 quiescence。
- 回归测试：checkpoint quiescence/integration 验证 transition 活跃期间不能保存
  quiescent Snapshot。
- 对应知识点：一致性边界、状态提交与事件发布、临界窗口。
- 面试表达：Step Worker 不是 Run 状态转换的 Owner，必须为 State/Event 双写窗口
  建立独立的短生命周期一致性信号。
- 当前状态：已防护。

### Bad Case 16：转换在两次采样之间完成后计数归零

- 类型：真实竞态。
- 触发条件：Checkpoint 已取得第一次 Activity 样本；随后一次 State/Event 转换
  完整执行 `0 → 1 → 0`；Checkpoint 再读取 State、Budget 和最终 Activity。
- 故障表现：两次可见的 in-flight 计数都是 0，但 State 或 Journal Watermark 已在
  采样窗口内变化，可能被误判为稳定 quiescent 捕获点。
- 根因分析：瞬时计数只能描述“采样时是否正在执行”，不能证明“两次采样之间没有
  完整发生过转换”，存在 ABA 式观测问题。
- 修复方案：为 State/Event 转换维护单调 `state_event_transition_epoch`；
  Checkpoint 比较捕获前后 epoch，发生变化时设置
  `state_event_transition_observed=true` 并强制本次捕获为 non-quiescent。
- 回归测试：
  `test_completed_transition_between_capture_samples_is_detected` 在 Watermark 捕获期间
  完整执行一次 transition，断言返回 `NOT_QUIESCENT`、Activity 记录 observed，且
  Snapshot Store 为空。
- 对应知识点：ABA 问题、乐观一致性校验、单调版本号、稳定快照。
- 面试表达：计数归零不代表观察区间没有变化；用单调 epoch 验证捕获前后版本，
  才能识别完整发生并结束的短事务。
- 当前状态：已防护。

## 23. 测试结果

新增测试文件：

- `tests/test_recovery_contract.py`；
- `tests/test_recovery_validation.py`；
- `tests/test_journal_tail_reducer.py`；
- `tests/test_recovery_integration.py`。

并扩充 checkpoint quiescence/integration 与旧 Snapshot 文档回归。实际执行结果：

- 指定目标 pytest：`127 passed, 12 subtests passed`；
- 全仓 pytest：`583 passed, 42 subtests passed`；
- `uv run python -m compileall -q core tools tests`：通过；
- `uv lock --check`：通过，157 packages resolved；
- `git diff --check`：通过（仅显示仓库既有 Windows LF→CRLF 提示，无 whitespace
  error）。

## 24. 未完成事项

- 不自动 Resume 或 Replay；
- 不重建 Runtime Shell/Scheduler；
- 不做完整 `ToolRecoveryDecision`；
- 不根据 Tool Evidence 自动重试或补偿；
- 不为 Journal v2 缺失的 `side_effect_kind` 猜测值；
- 不迁移默认 `/api/chat`；
- 不创建最终 `day22_snapshot_replay_result.md`。

## 25. 第三轮 B 接入点

第三轮 B 可在现有 `ToolRecoveryEvidence` 之上：

1. 审计并版本化 Tool Event 的 `side_effect_kind` 权威来源；
2. 关联 Invocation/Attempt identity digest；
3. 区分 SAFE_RETRY_CANDIDATE、DO_NOT_RETRY、MANUAL_RECONCILIATION 与
   INSUFFICIENT_EVIDENCE；
4. 定义 post-commit、compensation 和 detached worker 的对账 Owner；
5. 在仍保持默认零 Replay 的前提下设计显式人工/策略决策接口。

## 26. 需要带回 ChatGPT 审查的信息

- Snapshot v1 已在仓库 API/Store 内使用，但尚无正式外部兼容包与生产默认入口；
  本轮为修复稳定捕获窗口显式修正 v1 Activity/Digest。
- `quiescent` 的运行时权威 Owner 是 `CheckpointCoordinator`；Contract 只负责不可变
  持久化和 fail-closed 交叉验证。
- Step Attempt 没有权威 Owner，固定 `attempt_count=null`。
- Numeric gap 合法，Recovery 不实现 sequence 连续性假设。
- Journal v2 没有 Tool `side_effect_kind`，Evidence 明确使用 `None`，不猜测。
- `RESUMABLE` 只是未来安全前置条件满足，四个 Resume/Replay 能力字段固定 false。
- Tool 最终恢复决策和最终 Day 22 总文档留给第三轮 B。
