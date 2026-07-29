# 第 22 天第二轮：Checkpoint Barrier

## 1. 本轮目标

本轮在第一轮 Snapshot Foundation 上增加可注入的 checkpoint 安全边界：
修正 Snapshot 契约、暂停新 Step Claim、等待真实业务活动收口、捕获一致的
State/Budget/Journal 视图，并保存静止 Snapshot 或显式的非静止审计 Snapshot。

没有实现 Recovery Validator、Journal Tail Reducer、Replay、Resume、Model/Tool/RAG
重放、Runtime 恢复或默认 `/api/chat` 迁移。

## 2. 第一轮 Contract 修正

`StepStateSnapshot` 现在使用：

```text
execution_started: bool
attempt_count: int | None
```

真实 `StepState` 没有 Step Attempt Counter，因此默认 `attempt_count=None`，JSON
明确保存 `null`。`execution_started` 只由 `started_at` 是否存在表达。提供精确数值
时只接受非负整数、拒绝 bool，并要求 0/正数与未启动/已启动一致。Model、Tool 内部
Retry 不会被投影成 Step Attempt。

`RunSnapshot.validate_consistency()` 统一验证：

- 顶层 run status、stop reason、cancellation reason 与 `state_snapshot` 相同；
- 顶层 `step_states` 与 `state_snapshot.step_states` 语义相同；
- Plan、State、顶层 Step ID 的唯一、排序后集合相同；
- `PlanFingerprinter.fingerprint_snapshot(plan_snapshot)` 与
  `plan_fingerprint` 相同。

创建、反序列化、Store save/read 都会经过构造校验或 `verify_digest()`。错误只包含
固定结构说明，不包含 Step 正文或完整 Payload。

本次仍保持 Schema v1，是因为 Snapshot 尚未对外发布、未接入服务持久化生命周期，
第一轮 Store 也没有被生产入口使用；本次 v1 契约修正不承担已发布数据兼容义务。

## 3. 修改前 Claim/State/Event 边界

- 新 Step Claim 真实 owner：`SerialScheduler.claim_ready()`。
- PENDING → RUNNING 及所有终态提交真实 owner：`AgentStateMachine`。
- TaskGroup worker 创建 owner：`ParallelExecutor.execute()`。
- Event publish 与 per-run sequence 真实 owner：`RuntimeEventChannel.publish()`。
- Journal sequence 的持久记录 owner：注入的 `RunEventJournal`；Channel 消费并推进
  自己唯一的 per-run sequence，不增加第二套 counter。
- Budget reservation owner：`BudgetLedger`。
- Run 生命周期与最终状态提交 owner：`RunCoordinator`。
- Run 注册 owner：`RunRegistry`。
- Span 活跃视图只用于观测，不参与 quiescent 判定。

修改前 `AgentState` 没有显式一致读锁，EventChannel 没有 publication in-flight 或
watermark API；Tool 的 `ToolConcurrencyController` worker 视图也没有 run_id，
只能视为应用级数据，不能单独证明某个 Run 静止。

## 4. Scheduler Claim Gate

`SchedulerClaimGate` 是每个 Scheduler/Run 独占的 admission gate，状态为：

```text
OPEN → PAUSING → PAUSED → RESUMING → OPEN
                         ↘ CLOSED
```

它只控制新 Claim，等待已进入临界区的 Claim 退出。`SerialScheduler.claim_ready()`
执行顺序固定为：

```text
enter gate
→ 取得 Scheduler claim lock
→ 在 AgentState runtime lock 内重新计算 ready/claimable
→ 预算预留
→ AgentStateMachine 提交 RUNNING
→ 预算结算
→ 释放 state/claim lock
→ exit gate
```

PAUSING/PAUSED 时同步 Scheduler 入口返回空 claims，不修改状态；CLOSED 明确拒绝。
Gate 不取消 worker、不提交终态、不释放预算，也不是 StepStatus owner。

## 5. Checkpoint Barrier 状态机

`CheckpointBarrier` 状态为：

```text
IDLE
→ PAUSING_CLAIMS
→ WAITING_FOR_QUIESCENCE（REQUIRE 模式）
→ CAPTURING
→ SAVING
→ RESUMING_CLAIMS
→ IDLE
```

取消、超时、损坏和 Store 失败都通过 `finally` 恢复 Claim Gate。Barrier 等待期间
不持有 AgentState、Budget、Journal publish 或 Snapshot Store 锁。

## 6. Checkpoint Coordinator

`CheckpointCoordinator` 是单 Run checkpoint owner。第二个同 Run 请求固定返回
`CHECKPOINT_ALREADY_IN_PROGRESS`；不同 Run 使用不同 Coordinator 和 Lock，可并发。

`CheckpointResult` 只返回固定状态、snapshot ID、quiescent、固定 checkpoint kind、
journal sequence、安全 activity summary 与 safe error code，不返回原始 Exception。

## 7. Runtime Activity Snapshot

`RuntimeActivitySnapshot` 是 frozen、slots、只含计数的内容安全对象：

```text
claim_in_progress
running_step_count
budget_reservation_count
model_attempts_active
tool_attempts_active
retrievals_active
detached_tool_workers
detached_retrieval_workers
event_publications_in_flight
step_workers_active
activity_unknown
captured_at
```

不保存 Prompt、Tool 参数、RAG 正文、Worker/Thread/Task ID 或本地路径。
`RuntimeActivityTracker` 一实例只属于一个 run_id；Model/Tool/Retrieval 与
Parallel worker 使用 try/finally 增减计数。

## 8. Quiescent 判定

Quiescent 必须同时满足所有 activity count 为 0、`activity_unknown=false`、
AgentState 无 RUNNING、Budget reservation count 为 0、所有 reserved 维度为 0，
并成功捕获 Journal watermark。

Terminal 状态不会覆盖以上条件；仍有 detached worker 时保持
`quiescent=false`。Run root span、Observability dispatcher backlog 和已 Journal
的 Channel buffer 不代表业务工作，不参与阻塞。

## 9. REQUIRE_QUIESCENT

先暂停新 Claim，再有界轮询真实 per-run activity。只有活动归零且随后捕获的
State/Budget 仍满足不变量时保存。到 timeout 仍不静止返回
`CHECKPOINT_NOT_QUIESCENT`，不保存 Snapshot，不取消 worker、不清零预算、不修改
StepStatus。

## 10. NON_QUIESCENT_AUDIT

`ALLOW_NON_QUIESCENT_AUDIT` 在暂停新 Claim 后允许捕获活动中的安全视图。若仍有
业务活动，强制：

```text
quiescent=false
checkpoint_kind=NON_QUIESCENT_AUDIT
status=SAVED_NON_QUIESCENT_AUDIT
```

Activity summary 随 Snapshot 保存。该 Snapshot 只用于人工审计，本轮没有恢复入口，
也没有任何“可自动恢复”标志。

## 11. Journal Watermark

`RuntimeEventChannel.capture_journal_watermark()` 与 `publish()` 复用同一
`_publish_lock`，读取既有 `_sequence`，并与 `journal.last_sequence(run_id)` 核对。
0 表示没有事件；只比较最后序号，因此合法 numeric gap 被允许。

Journal append 成功、enqueue 失败时，现有 publish 流程已经消费 sequence；watermark
仍以 Channel/Journal 已确认值为准，不复用序号。Channel ABORTED 不单独否决
watermark；只要两侧一致仍可生成审计 Snapshot。

## 12. State 一致捕获

`AgentState` 增加进程内 `RLock`，不进入序列化。`AgentStateMachine` 的候选校验与
提交、Scheduler 的 prepare/evaluate/claim 事务、`snapshot_copy()` 都使用这一个
最小同步边界。Snapshot 只接收脱离原对象的 clone，不持有可变 State 引用。

运行 worker 结束不会等待 checkpoint 长持有 State Lock；锁只覆盖同步 clone 或
同步状态提交。

## 13. Budget 一致捕获

继续复用 `BudgetLedger.snapshot()` 的原锁，一次读取 limits、used、reserved、
remaining、reservation count。Checkpoint 不取得 Reservation 对象，不
commit/release/refund，不修改 Ledger，也不把 unlimited 转成 Infinity。

## 14. Attempt/Worker 活动追踪

- Model：`ModelInvocationRouter.invoke()` 的真实调用边界；
- Tool：`ToolAttemptExecutor.execute()`，并单独跟踪 detached sync worker；
- Retrieval：`RetrievalExecutionService.execute()`，并通过
  `BlockingTaskHandle` done callback 跟踪 detached blocking worker；
- Step：TaskGroup worker 从启动到终态 Event 发布完成。

所有计数在成功、失败、取消、超时路径均由 try/finally 或完成 callback 清理。
Retry、Fallback、Circuit、Side Effect、Idempotency、Budget 与 Timeout 语义未改。

## 15. Cancellation / Timeout

Coordinator 同时检查调用方/Run cancellation token 与可选 shutdown token。
取消后停止等待、不强杀 worker、不保存 Snapshot；timeout 后返回安全状态。
Gate 与 checkpoint lock 都在 finally 中释放。

## 16. Lock Order

实际顺序：

```text
per-run checkpoint lock
→ Claim Gate pause（不持锁等待）
→ Scheduler claim lock
→ AgentState runtime lock
→ Budget lock（仅 Claim 的短预留/结算）
```

捕获顺序：

```text
activity short reads
→ EventChannel publish lock 下 watermark
→ AgentState runtime lock 下 clone
→ Budget lock 下 snapshot
→ 释放全部 Runtime locks
→ 构造/验证 RunSnapshot
→ Snapshot Store lock
→ finally resume Claim Gate
```

不会持有 Claim Gate Lock 等 Event、State Lock 等 worker、Journal Lock 等 Tool、
Budget Lock 调 Store，或 Store Lock 恢复 Scheduler。

## 17. Store Failure

Store 抛错映射为 `STORE_FAILED/SNAPSHOT_STORE_FAILED`。不返回成功 snapshot ID，
不修改 State/Budget/Journal，不触发 Model/Tool/RAG Retry，错误不携带路径、SQL
或原始异常；Gate 在 finally 恢复。

## 18. Runtime 真实接入

`RunCoordinator` 现在总是为 Coordinated Runtime 绑定 per-run activity tracker；
只有显式注入 `snapshot_store` 时才创建 `CheckpointCoordinator`，并提供：

```text
await RunCoordinator.create_checkpoint(...)
```

显式 checkpoint 装配会通过 `AgentStateMachine.register_plan_step()` 在 CREATED
状态登记 PENDING Plan 投影，因此 PRE_RUN Snapshot 不需要伪造第二套 State，也不会
把 Run 提前改为 RUNNING。

没有自动周期保存。`ChatService`、`server.py` 与默认 `/api/chat` 未注入 Store，
所以生产默认路径不会新增持久化、API 或用户可见行为。

## 19. Legacy 边界

Legacy AgentLoop、旧同步 Router、UI 与默认聊天入口未迁移。原
`ToolConcurrencyController.worker_snapshot()` 仍是应用级视图；checkpoint 的可靠
per-run detached 数量来自新 tracker。没有 tracker/event channel 的自定义 legacy
activity provider 必须设置 `activity_unknown=true`，不能推断“无活动”。

## 20. 重点 Bad Case

以下“真实审计”指修改前确有的边界，“假设构造”用于 fail-closed 回归。

### Bad Case 1：attempt_count=0/1 被误当精确值

- 类型：真实审计。
- 风险：由 `started_at` 伪造权威 attempt 次数。
- 处理：默认 `None/null`，另存 `execution_started`。

### Bad Case 2：顶层状态与 StateSnapshot 不一致

- 类型：假设构造。
- 风险：恢复方无法判断哪个字段可信。
- 处理：构造、反序列化、Store save/read 全部 fail closed。

### Bad Case 3：PlanSnapshot 与 Fingerprint 不一致

- 类型：真实审计缺口。
- 风险：digest 合法但静态 Plan 身份错误。
- 处理：统一重算 `fingerprint_snapshot()`，不能只验 payload digest。

### Bad Case 4：Barrier 暂停后仍 Claim 新 Step

- 类型：假设构造。
- 处理：Claim Gate 位于真实 `claim_ready()` 外层，PAUSED 后不提交 RUNNING。

### Bad Case 5：等待 Quiescent 时持有 State Lock

- 类型：假设构造。
- 处理：等待只读 activity；State Lock 只在最终 clone 短持有。

### Bad Case 6：Store 失败后未恢复 Scheduler

- 类型：假设构造。
- 处理：Store 在 Runtime locks 释放后调用，Gate 由 finally 恢复。

### Bad Case 7：Barrier Timeout 后永久 PAUSED

- 类型：假设构造。
- 处理：timeout/NOT_QUIESCENT 路径均恢复 OPEN。

### Bad Case 8：Terminal + Detached Worker 被判 Quiescent

- 类型：真实风险。
- 处理：终态不覆盖 detached 计数。

### Bad Case 9：Budget reserved 被强制清零

- 类型：假设构造。
- 处理：只读真实 reserved；非零时非静止。

### Bad Case 10：Watermark 捕获时 Event 正在 Append

- 类型：真实竞态。
- 处理：capture 与 publish 共用 publish lock。

### Bad Case 11：合法 Sequence Gap 被判损坏

- 类型：假设构造。
- 处理：只核对 Channel/Journal 最后 sequence，不要求连续。

### Bad Case 12：无法获得 per-run Activity 时默认无活动

- 类型：真实审计。
- 处理：缺少可靠 source 时 `activity_unknown=true`，强制非静止。

### Bad Case 13：两个 Checkpoint 同时修改同一 Run

- 类型：假设构造。
- 处理：同 Run 第二请求固定返回 already-in-progress；不同 Run 无全局锁。

### Bad Case 14：Non-quiescent Audit 被标记可恢复

- 类型：假设构造。
- 处理：固定 kind/status、`quiescent=false`；本轮无 Recovery API。

## 21. 测试结果

新增：

- `tests/test_checkpoint_claim_gate.py`
- `tests/test_checkpoint_barrier.py`
- `tests/test_checkpoint_quiescence.py`
- `tests/test_checkpoint_integration.py`
- `tests/test_snapshot_contract.py` 的前置契约回归

目标 checkpoint/scheduler/coordinator 回归：`79 passed, 13 subtests passed`。
包含第一轮 Snapshot 与 Model/Tool/Retrieval/Trace 的目标集合：
`143 passed, 4 subtests passed`。任务清单中的 `tests/test_runtime_budget.py` 在仓库
中不存在，实际等价文件是 `tests/test_budget.py`，目标命令据此替换。

最终验证：

- `uv run python -m pytest -q`：`562 passed, 42 subtests passed`；
- `uv run python -m compileall -q core tools tests`：通过；
- `uv lock --check`：通过，`Resolved 157 packages`；
- `git diff --check`：通过。

## 22. 未完成事项

按边界明确未实现：Recovery Validator、Journal Tail 归约、Tool side-effect
recovery decision、自动 Replay/Resume、Model/Tool/RAG 重放、Snapshot 恢复
Runtime、默认 API 迁移、分布式 Lock/Store 与第 23 天内容。

## 23. 第三轮接入点

后续若批准，可在显式 Runtime 装配层注入 Snapshot Store，选择 PRE_RUN、
STEP_BOUNDARY、TERMINAL 的调用时机；恢复工作必须先独立实现 Validator 与人工对账
策略，不能直接把 `RunSnapshot` 反序列化成运行中对象。

## 24. 需要带回 ChatGPT 审查的信息

- Snapshot v1 本轮仍属未发布内部契约，新增了 `execution_started` 与
  `activity_snapshot`。
- 同 Run 并发策略是立即拒绝，不排队。
- Tool 原应用级 worker tracker 未改业务语义；可靠 per-run 统计由 RunContext
  tracker 补充。
- Model 当前同步 Router 没有可抢占 worker；其整个真实 invocation 边界保守计入
  active。
- Non-quiescent Audit 明确不可自动恢复。
- 默认 API 没有注入 Store，也没有自动 checkpoint。
