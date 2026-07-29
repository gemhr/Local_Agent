本日计划**略作调整**。

第 22 天同时涉及序列化、调度屏障、Journal 对齐、状态归约和 Tool 副作用判断，单次交给 Codex 容易出现职责混乱。因此拆成三轮：

```text
第一轮：Snapshot Contract、Plan Fingerprint、Snapshot Store
第二轮：Checkpoint Barrier、Quiescent Snapshot、Runtime 集成
第三轮：Recovery Validator、Limited Replay、Tool Reconciliation
```

每轮完成后先审查，再进入下一轮。今天不修改 Tool Call（工具调用）业务逻辑，因此不触发复杂模拟 Tool 的暂停规则。

# 阶段二第 22 天：Snapshot、Checkpoint、Recovery Validation 与受限 Replay

**当前进度：第 22/25 天。**

核心目标：

> 保存一个经过版本化、安全裁剪、可校验的 Runtime 状态视图，并结合后续 Journal Tail（日志尾部）判断 Run 是否可恢复、需要对账或已经终结。

本日准确架构为：

```text
Versioned RunSnapshot
+ Plan Fingerprint
+ Journal Tail Validation
+ Limited State Reduction
+ Tool Side-effect Assessment
→ RecoveryAssessment
```

本日不会得到：

```text
Snapshot
→ 自动重新创建 Run
→ 自动继续 Scheduler
→ 自动重新执行 Model / Tool / RAG
```

------

# 一、Snapshot、Checkpoint 与 Replay 的区别

## 1. Snapshot

Snapshot（快照）是某个时间点的安全状态投影。

它回答：

> 在 Journal Sequence=N 时，这个 Run 的计划、状态、预算和恢复风险是什么？

Snapshot 不是 Python 对象的内存拷贝，不能直接保存：

```python
pickle.dumps(runtime)
```

因为 Runtime 中包含：

- Lock；
- Semaphore；
- Task；
- Generator；
  -数据库连接；
  -Model Adapter；
  -Thread Pool；
  -EventChannel；
  -ContextVar Token。

正确方式是：

```text
Runtime Objects
→ Safe Snapshot Projection
→ Canonical JSON
→ Digest
→ Snapshot Store
```

## 2. Checkpoint

Checkpoint（检查点）是一套生成可信 Snapshot 的协调流程。

它不仅是：

```text
state.to_dict()
```

而是：

```text
暂停新 Step Claim
→ 等待状态提交完成
→ 确认当前活跃工作
→ 获取 Journal 水位
→ 捕获 Plan / State / Budget
→ 验证内部一致性
→ 持久化 Snapshot
→ 恢复调度
```

## 3. Replay

Replay（回放）是按顺序读取已记录事实，并将它们应用到一个 Reducer（归约器）。

本日只允许：

```text
安全 Snapshot Projection
+ 已知 Journal Terminal Metadata
→ Recovery Projection
```

不允许：

```text
MODEL_STARTED
→ 再次请求模型

TOOL_STARTED
→ 再次执行 Tool

RETRIEVAL_STARTED
→ 再次访问向量库
```

------

# 二、Snapshot 不是新的状态 Owner

当前事实来源仍然是：

```text
运行期间：
AgentState / StateMachine

持久化事件：
RuntimeEvent Journal

恢复评估输入：
RunSnapshot + Journal Tail
```

Snapshot 只是某个时间点的不可变投影。

不能出现：

```text
AgentState.status = RUNNING
Snapshot.status = FAILED
→ Runtime 根据 Snapshot 反向修改 AgentState
```

本日的 Recovery Validator（恢复校验器）只返回评估结果，不直接修改活动 Run。

------

# 三、RunSnapshot 设计

建议使用不可变结构：

```python
@dataclass(frozen=True, slots=True)
class RunSnapshot:
    snapshot_schema_version: int
    snapshot_id: str

    run_id: str
    trace_id: str

    plan_snapshot: PlanSnapshot
    plan_fingerprint: str

    state_snapshot: AgentStateSnapshot
    budget_snapshot: BudgetSnapshot

    last_journal_sequence: int

    run_status: str
    stop_reason: str | None
    cancellation_reason: str | None

    step_states: tuple[StepStateSnapshot, ...]
    runtime_metadata: RuntimeSnapshotMetadata

    checkpoint_kind: str
    quiescent: bool

    created_at: datetime
    payload_digest: str
```

## Snapshot 中的身份

允许保存：

- `snapshot_id`
- `run_id`
- `trace_id`
  -静态 Step ID
  -Journal Sequence
  -安全 Invocation 摘要

不得保存：

- `span_id` 的活动对象；
  -ContextVar；
  -Thread ID；
  -Task ID；
  -Future ID；
  -内存地址。

Trace ID 可以保存，因为它是稳定的 Run 关联身份；活动 Span 无法在恢复后继续使用。

------

# 四、Plan Snapshot 与 Plan Fingerprint

## 1. Plan Snapshot 不能直接保存完整 Plan

Plan 中的标题、描述或 Static Input（静态输入）可能来自用户 Prompt（提示词）。

因此不能简单执行：

```python
asdict(plan)
```

建议分为：

```text
PlanFingerprint Input
→ 完整静态结构的规范表示
→ 只用于内存计算 Hash

PlanSnapshot
→ 安全静态结构
→ 可以持久化
```

## 2. Fingerprint 应覆盖

- Plan Schema Version；
- Step ID；
  -Agent / Capability；
  -依赖关系；
  -静态执行类型；
  -Completion Criteria（完成条件）；
  -静态配置；
  -允许持久化的参数；
  -敏感静态输入的 Digest；
  -Plan Version。

## 3. Fingerprint 不应覆盖

- StepStatus；
  -开始、结束时间；
  -Attempt Count；
  -执行 Result；
  -错误；
  -Budget Usage；
  -当前 Model Profile；
  -Retry 结果；
  -动态 Span ID。

## 4. Canonical Plan

推荐规范化：

```text
Step 按 step_id 排序
Dependencies 排序
Mapping Key 排序
Enum 使用 value
时间不得进入
空值表示固定
JSON 禁止 NaN / Infinity
UTF-8 编码
SHA-256 lowercase
```

## 5. 用户正文处理

如果 `static_input` 包含：

```text
用户原始问题
文件内容
Prompt
RAG Context
```

Snapshot 只能保存：

```text
input_present = true
input_length
input_digest
input_type
```

不得保存正文。

## 6. Plan 匹配

恢复评估前：

```text
calculate_fingerprint(current_plan)
==
snapshot.plan_fingerprint
```

不一致时：

```text
RecoveryStatus.PLAN_MISMATCH
```

不能根据“Step 数量差不多”继续恢复。

------

# 五、AgentStateSnapshot

Snapshot 不能保存完整 `AgentState.to_dict()`，因为可能包含：

- `final_output`
- Step Result；
  -原始 Error；
  -Tool Output；
  -RAG Context；
  -用户正文。

建议建立安全结构：

```python
@dataclass(frozen=True, slots=True)
class AgentStateSnapshot:
    run_status: str
    stop_reason: str | None
    cancellation_reason: str | None

    step_states: tuple[StepStateSnapshot, ...]

    final_output_present: bool
    final_output_length: int
    final_output_digest: str | None

    created_at: datetime
    updated_at: datetime
    state_version: int
```

Step Snapshot 建议包含：

```text
step_id
status
attempt_count
started_at
completed_at
duration_ms
safe_error_code
result_present
result_length
result_digest
```

不得保存：

- Result 正文；
  -Error Message；
  -Traceback；
  -Tool Output；
  -Model Output；
  -RAG Chunk。

------

# 六、StepStatus 的真实边界

原计划写了：

```text
CLAIMED / RUNNING
```

但 Codex 必须先审计项目真实的 `StepStatus`。

不得为了匹配计划临时新增 `CLAIMED`。

需要识别真实的 In-flight Status（执行中状态），例如：

```text
RUNNING
```

如果项目确实存在：

```text
CLAIMED
DISPATCHED
STARTING
```

再将它们纳入恢复判断。

之前已经确定：

```text
BLOCKED
```

是不可执行状态，不等于“已 Claim 正在运行”，不能统一按 In-flight 处理。

------

# 七、Budget Snapshot

Budget Snapshot（预算快照）至少包含：

```text
limits
used
reserved
remaining
ledger_version
```

## Quiescent Snapshot 要求

静止快照中：

```text
reserved == 0
```

如果仍存在 Reservation（预留）：

```text
quiescent = false
```

或直接拒绝创建 Quiescent Snapshot。

不能为了让校验通过而将 Reserved 强行归零。

## Budget 一致性

至少验证：

```text
used >= 0
reserved >= 0
used + reserved <= limit
remaining = limit - used - reserved
```

对于无限预算或禁用维度，需要使用明确的 Schema 表示，不能使用 `Infinity`。

------

# 八、Runtime Metadata

允许保存的 Runtime Metadata（运行时元数据）：

```text
runtime_schema_version
runtime_mode
planner_version
scheduler_version
model_routing_policy_version
tool_contract_version
retrieval_contract_version
event_schema_version
journal_schema_version
host_process_generation，可选
```

不得保存：

-主机完整路径；
-进程环境变量；
-API Key；
-Provider URL；
-数据库连接串；
-线程 ID；
-用户目录。

------

# 九、Snapshot Store

建议实现：

```text
SnapshotStore Protocol
├── InMemorySnapshotStore
└── SQLiteSnapshotStore
```

公共接口：

```python
class SnapshotStore(Protocol):
    async def save(
        self,
        snapshot: RunSnapshot,
    ) -> SnapshotSaveResult:
        ...

    async def get(
        self,
        snapshot_id: str,
    ) -> RunSnapshot | None:
        ...

    async def latest(
        self,
        run_id: str,
    ) -> RunSnapshot | None:
        ...

    async def list_for_run(
        self,
        run_id: str,
        limit: int,
    ) -> tuple[RunSnapshot, ...]:
        ...

    async def close(self) -> None:
        ...
```

不提供：

```text
update_snapshot
patch_snapshot
replace_snapshot
delete_snapshot
```

Snapshot 创建后不可变。

## SQLite Schema

建议：

```sql
CREATE TABLE runtime_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    snapshot_schema_version INTEGER NOT NULL,
    last_journal_sequence INTEGER NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    quiescent INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL
);

CREATE INDEX idx_runtime_snapshots_run_created
ON runtime_snapshots(run_id, created_at DESC);
```

## 原子写入

使用单事务：

```text
BEGIN IMMEDIATE
→ 检查 Snapshot ID
→ 插入完整 Payload
→ COMMIT
```

不允许：

```text
先写半行 Metadata
→ 再更新 Payload
```

## Duplicate 与 Conflict

相同 `snapshot_id`、相同 Digest：

```text
DUPLICATE
```

相同 `snapshot_id`、不同 Digest：

```text
SNAPSHOT_ID_CONFLICT
```

------

# 十、Snapshot Digest

Digest 至少覆盖：

```text
snapshot_schema_version
snapshot_id
run_id
trace_id
plan_fingerprint
safe plan snapshot
safe state snapshot
budget snapshot
last_journal_sequence
run_status
stop_reason
cancellation_reason
runtime metadata
checkpoint kind
quiescent
created_at
```

必须使用版本化算法：

```text
Snapshot Schema v1
→ v1 canonical digest
```

未来升级不能用新算法重新解释旧 Snapshot。

------

# 十一、Checkpoint Barrier

这是第二轮 Codex 的重点，第一轮先不接入。

## 推荐状态

```text
OPEN
PAUSING
PAUSED
RESUMING
CLOSED
```

## Checkpoint 流程

```text
Checkpoint request
→ 独占 checkpoint lock
→ Scheduler pause new claims
→ 等待正在进行的 claim/state commit 完成
→ 等待已运行 Step 到达安全边界，或超过 timeout
→ 捕获 AgentState 与 Budget
→ 序列化 Event publication
→ 读取 Journal watermark
→ 判断 quiescent
→ 创建并保存 Snapshot
→ finally 恢复 Scheduler
```

## Barrier 需要阻止什么

只阻止：

```text
新的 Step Claim
```

不能中止：

-已经运行的 Model；
-Tool；
-RAG；
-正在提交的 State Transition。

## Barrier Timeout

如果等待安全边界超时：

可以选择：

### 方案 A：拒绝 Snapshot

```text
CHECKPOINT_NOT_QUIESCENT
```

### 方案 B：保存审计型非静止 Snapshot

```text
quiescent = false
```

推荐支持两种 Mode：

```text
REQUIRE_QUIESCENT
ALLOW_NON_QUIESCENT_AUDIT
```

非静止 Snapshot 只能用于审计和人工对账，不能返回 `RESUMABLE`。

------

# 十二、Journal Watermark

Snapshot 必须记录：

```text
last_journal_sequence
```

但不能直接在 Barrier 外调用：

```python
journal.last_sequence(run_id)
```

因为读取期间可能还有 Event 正在发布。

需要与 Event Publish 临界区协调：

```text
暂停新 Claim
→ 等待状态和 Event 发布完成
→ 在 Channel Publish Lock 下捕获 sequence watermark
→ 验证 Journal last_sequence
```

可以增加类似：

```text
RuntimeEventChannel.capture_journal_watermark()
```

但不能创建新的 Sequence Owner。

## Watermark 不等于恢复

读取 Journal Sequence 只是确认：

> Snapshot 包含了截至哪个持久化事件的状态视图。

不会自动重新建立 Channel、Emitter 或 Span。

------

# 十三、Quiescent Snapshot

Quiescent（静止）至少要求：

```text
没有正在 Claim 的 Step
没有 RUNNING Step
没有正在提交的 State Transition
Budget reserved = 0
没有未完成的 Event Publication
没有活动的 Runtime-owned Model/Tool/RAG Attempt
```

Detached Worker 是特殊情况。

Runtime 已停止等待，但底层 Worker 仍执行时：

```text
quiescent = false
```

即使 Step 已经变成 `TIMED_OUT`，外部副作用结果仍可能未知。

## Terminal Snapshot

Run 已终结时也不能无条件认为 Quiescent。

例如：

```text
Run = TIMED_OUT
Tool Worker = Detached
```

Run 是 Terminal，但 Snapshot 仍需：

```text
quiescent = false
requires_reconciliation = true
```

------

# 十四、Recovery Assessment

建议返回不可变结果：

```python
@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    status: RecoveryStatus
    snapshot_id: str
    run_id: str

    validated_journal_sequence: int
    reasons: tuple[RecoveryReason, ...]

    blocking_step_ids: tuple[str, ...]
    tool_decisions: tuple[ToolRecoveryDecision, ...]

    reduced_projection: RecoveryProjection | None

    automatic_resume_allowed: bool
    model_replay_allowed: bool
    tool_replay_allowed: bool
    retrieval_replay_allowed: bool
```

本日后三项始终是：

```text
false
```

`automatic_resume_allowed` 只有非常严格的条件下才可以为 `true`，而本日也不会真正执行恢复。

------

# 十五、RecoveryStatus 优先级

建议：

```text
CORRUPTED
INCOMPATIBLE_SCHEMA
PLAN_MISMATCH
JOURNAL_GAP_OR_CONFLICT
UNSUPPORTED
TERMINAL
REQUIRES_RECONCILIATION
RESUMABLE
```

这里是严重程度排序，不代表代码必须使用该 Enum 顺序。

## CORRUPTED

- Snapshot Digest 不匹配；
  -非法 JSON；
  -非法时间；
  -State/Plan Step ID 不一致；
  -Budget 违反不变量。

## INCOMPATIBLE_SCHEMA

Snapshot Schema 超出当前支持范围。

## PLAN_MISMATCH

当前 Plan Fingerprint 与 Snapshot 不一致。

## JOURNAL_GAP_OR_CONFLICT

需要特别注意：

> 第 19 天允许合法 Numeric Sequence Gap（数字序号空洞）。

因此不能因为：

```text
100 → 102
```

就判定损坏。

该状态仅用于：

- Snapshot Watermark 高于 Journal Last Sequence；
  -同 Sequence 冲突；
  -Terminal 后还有 Event；
  -读取 Tail 时摘要或顺序不变量失败；
  -必要因果记录缺失；
  -Snapshot 声称包含某事件但 Journal 不存在。

## UNSUPPORTED

Journal Tail 出现当前 Reducer 无法安全解释的状态变化。

不能猜测执行。

## TERMINAL

Snapshot 或 Journal Tail 已证明 Run 终结，并且不需要恢复。

如果存在 Detached Worker 或 Unknown Side Effect，可以仍返回：

```text
REQUIRES_RECONCILIATION
```

而不是 `TERMINAL`。

## RESUMABLE

第一版只有非常保守的场景：

```text
quiescent = true
无 In-flight Step
无 Unknown Side Effect
无 Detached Worker
Plan 匹配
Journal 对齐
Budget 一致
Reducer 支持全部 Tail
Run 非 Terminal
```

即便返回 `RESUMABLE`，本日也只表示：

> 具备未来创建恢复流程的前置条件。

不是已经自动恢复。

------

# 十六、Limited State Reducer

建议不要直接修改 `AgentState`。

建立：

```text
LimitedJournalTailReducer
→ RecoveryProjection
```

例如：

```python
@dataclass(frozen=True, slots=True)
class RecoveryProjection:
    run_status: str
    stop_reason: str | None
    cancellation_reason: str | None
    step_states: tuple[ReducedStepState, ...]
    budget_snapshot: BudgetSnapshot
    last_applied_sequence: int
```

允许归约：

- `STEP_COMPLETED`
- `RUN_COMPLETED`
- Run-level `CANCELLATION`
- Run-level `TIMEOUT`
- Run-level `BUDGET_EXHAUSTED`
  -安全、权威的 Budget Snapshot Event（只有项目真实存在时）

## 不应归约

- `OUTPUT_DELTA`
- `MODEL_STARTED`
- `MODEL_COMPLETED` 正文；
- `TOOL_STARTED` 为重新执行；
- `RETRIEVAL_STARTED` 为重新检索；
  -未知自定义 Event；
  -Legacy `[[ORCH]]`。

## Budget 注意事项

如果当前 Event 不包含权威累计 Budget Snapshot：

```text
不能根据 token/call count 推测 Budget
```

应：

-保留 Snapshot 中的 Budget；
-将 Tail 中相关预算变化标为 `UNSUPPORTED`；
-或者只使用明确的 Run-level Budget Exhausted 事实。

不能虚构精确预算。

------

# 十七、Tool Side-effect Reconciliation

建议建立：

```text
ToolRecoveryStatus
```

例如：

```text
NO_SIDE_EFFECT
SAFE_RETRY_CANDIDATE
DO_NOT_RETRY
REQUIRES_MANUAL_RECONCILIATION
INSUFFICIENT_EVIDENCE
```

## 安全判断

### 无副作用

```text
side_effect_kind = NONE
```

可以成为未来重新执行候选，但本日不执行。

### 幂等 + 稳定 Key + 明确 Replay 支持

```text
IDEMPOTENT
+ key_digest present
+ replay_supported = true
+ side_effect_state 可确认
```

返回：

```text
SAFE_RETRY_CANDIDATE
```

注意只是候选。

### 非幂等已提交

```text
NON_IDEMPOTENT
+ COMMITTED
```

返回：

```text
DO_NOT_RETRY
```

### Outcome Unknown

```text
OUTCOME_UNKNOWN
execution_detached
POST_COMMIT_RESPONSE_FAILURE 且证据不足
```

返回：

```text
REQUIRES_MANUAL_RECONCILIATION
```

### 补偿失败

```text
COMPENSATION_FAILED
```

也必须人工处理。

## 禁止行为

Recovery Validator 不得：

-读取 Idempotency Key 正文；
-重新获取 Resource Lease；
-调用 Tool Store 改状态；
-执行 Compensation；
-再次提交业务；
-将 Unknown 自动转成 Failed。

------

# 十八、Crash Window

## Window 1

```text
State commit
→ 进程崩溃
→ Journal append 尚未完成
```

Snapshot + Journal 可能无法发现全部状态变化。

本日不能解决。

## Window 2

```text
Journal append
→ Channel / Observability 尚未处理
→ 进程崩溃
```

Journal Tail 可以发现事件，但本日不自动投递。

## Window 3

```text
Tool Side Effect committed
→ Tool Completed Event 尚未 Journal
→ 进程崩溃
```

恢复时可能无法判断副作用是否发生。

必须：

```text
REQUIRES_MANUAL_RECONCILIATION
```

## Window 4

```text
Snapshot captured
→ Snapshot Store commit 前崩溃
```

没有完整 Snapshot，Store 必须通过原子事务避免半条记录。

## Window 5

```text
Snapshot Store 成功
→ Scheduler resume 前崩溃
```

Snapshot 有效，但原进程已经终止。未来恢复必须重新创建 Runtime Shell，不能恢复原 Lock/Task。

------

# 十九、本日三轮 Codex 任务

## 第一轮：Snapshot Foundation

完成：

-安全 Snapshot Contract；
-Plan Fingerprint；
-AgentState/Budget 安全投影；
-Snapshot Digest；
-InMemory/SQLite Snapshot Store；
-严格序列化；
-安全和损坏测试。

不接入 Scheduler，不创建 Barrier，不实现 Recovery Reducer。

## 第二轮：Checkpoint Barrier

完成：

-Claim Gate；
-Checkpoint Coordinator；
-Journal Watermark；
-Quiescent 检测；
-非静止审计 Snapshot；
-RunCoordinator/Scheduler 集成；
-并发和取消测试。

## 第三轮：Recovery Validation

完成：

-Plan/Schema/Digest/Journal 验证；
-Limited Journal Tail Reducer；
-Tool Side-effect Assessment；
-RecoveryAssessment；
-零 Model/Tool/RAG 调用证明；
-最终全仓回归。

------

# 二十、第 22 天重点 Bad Case

## Bad Case 1：Snapshot 保存 Lock/Future

- **类型：真实序列化风险**
- 修复：显式安全 Projection。

## Bad Case 2：Plan 改变后继续恢复

- **类型：真实一致性风险**
- 修复：Canonical Plan Fingerprint。

## Bad Case 3：RUNNING Step 自动重新执行

- **类型：严重副作用风险**
- 修复：进入 `REQUIRES_RECONCILIATION`。

## Bad Case 4：非幂等 Tool 已提交后重放

- **类型：严重业务风险**
- 修复：`DO_NOT_RETRY`。

## Bad Case 5：Snapshot Watermark 高于 Journal

- **类型：持久化损坏**
- 修复：Fail Closed。

## Bad Case 6：非静止 Snapshot 判定 RESUMABLE

- **类型：架构错误**
- 修复：`quiescent=false` 强制对账。

## Bad Case 7：损坏 Snapshot 自动修复

- **类型：数据完整性风险**
- 修复：`CORRUPTED`，不猜测。

## Bad Case 8：Replay 调用真实 Model

- **类型：范围和成本风险**
- 修复：纯 Reducer，不依赖 Adapter。

## Bad Case 9：第二个 Terminal Event

- **类型：Journal 生命周期冲突**
- 修复：Terminal 不变量验证。

## Bad Case 10：旧 Schema 强行读取

- **类型：兼容性风险**
- 修复：Versioned Decoder。

## Bad Case 11：Budget Snapshot 与 AgentState 不一致

- **类型：资源恢复风险**
- 修复：创建和恢复前双重校验。

## Bad Case 12：将受限 Replay 描述为完整恢复

- **类型：真实性错误**
- 修复：明确 Capability Boundary。

------

# 二十一、第一轮 Codex 实操提示词

你正在继续改造 LocalAgent 项目的阶段二 Runtime。

本轮是第 22 天的第一轮任务，只实现：

- Versioned RunSnapshot Contract
- Safe Plan Snapshot
- Plan Fingerprint
- Safe AgentState Snapshot
- Safe Budget Snapshot
- Snapshot Digest
- InMemorySnapshotStore
- SQLiteSnapshotStore
- Snapshot 安全与损坏测试

本轮不得实现：

- Checkpoint Barrier
- Scheduler Pause
- Journal Tail Reducer
- Recovery Validator
- Tool Reconciliation
- 自动 Replay
- 自动 Recovery
- Snapshot 后重新执行 Model/Tool/RAG
- 第 23 天 API 迁移

## 一、结果文档

创建：

```text
docs/learning/stage2/result/day22_snapshot_foundation_result.md
```

最终第 22 天总文档将在第三轮创建：

```text
docs/learning/stage2/result/day22_snapshot_replay_result.md
```

## 二、先审计现有对象

至少检查：

- `core/runtime/plan.py` 或实际 Plan/PlanStep 定义
- `core/runtime/state.py` 或实际 AgentState/StepState 定义
- `core/runtime/budget.py`
- `core/runtime/run_context.py`
- `core/runtime/run_coordinator.py`
- `core/runtime/scheduler.py`
- `core/runtime/parallel_execution.py`
- `core/runtime/events.py`
- `core/runtime/event_journal.py`
- `core/runtime/event_journal_store.py`
- `core/runtime/tool_execution.py`
- `core/runtime/tool_contract.py` 或实际等价文件
- `core/runtime/retrieval_execution.py`
- `core/runtime/tracing.py`
- `core/chat_service.py`
- `settings.py`
- `server.py`
- 第 19～21 天结果文档和测试

结果文档必须说明：

1. Plan/PlanStep 当前真实字段；
2. 哪些字段是静态定义；
3. 哪些字段可能含用户正文；
4. AgentState/StepState 当前真实字段；
5. StepStatus 是否真实存在 `CLAIMED`；
6. 哪些 State 字段可能含正文；
7. Budget 当前 Limits/Used/Reserved/Ledger 结构；
8. Tool Side Effect 状态当前存在哪里；
9. 当前是否已有通用 Canonical JSON/Digest 工具；
10. 当前 SQLite Store 的连接和错误处理模式。

不得为了匹配任务说明而新增项目中不存在的 StepStatus。

## 三、建议新增文件

建议新增或提供等价结构：

```text
core/runtime/snapshot_contract.py
core/runtime/snapshot_serialization.py
core/runtime/snapshot_store.py
core/runtime/plan_fingerprint.py

tests/test_snapshot_contract.py
tests/test_plan_fingerprint.py
tests/test_snapshot_store.py
tests/test_snapshot_security.py
```

根据真实结构更新 `core/runtime/__init__.py`。

本轮不要修改 Scheduler、ParallelExecutor 或 RuntimeEventChannel 的执行语义。

## 四、RunSnapshot Contract

建立不可变、严格校验的结构，至少包含：

```text
snapshot_schema_version
snapshot_id
run_id
trace_id
plan_snapshot
plan_fingerprint
state_snapshot
budget_snapshot
last_journal_sequence
run_status
stop_reason
cancellation_reason
step_states
runtime_metadata
checkpoint_kind
quiescent
created_at
payload_digest
```

要求：

- 所有时间必须是 timezone-aware UTC；
- 所有版本、sequence、count 拒绝 bool；
- 拒绝 NaN/Infinity；
- `last_journal_sequence >= 0`；
- 普通 repr 不展示 Payload；
- Snapshot 创建后不可变；
- 本轮只定义 `checkpoint_kind`，不实现 Barrier。

## 五、Safe Plan Snapshot

不得直接持久化完整 Plan。

建立安全 PlanSnapshot/PlanStepSnapshot，至少保存：

- plan schema/version；
- step_id；
- agent/capability；
- dependency step IDs；
- static execution kind；
- completion criteria 的安全结构；
- static input 类型、长度和 digest；
  -允许持久化的有限配置；
  -敏感字段 present/length/digest。

不得保存：

-用户原始 Query；
-Prompt；
-文件正文；
-Tool 参数正文；
-RAG/Memory 正文；
-自由文本异常。

Plan 的 title/description/static input 如可能由用户输入派生，只能保存安全摘要或 digest。

## 六、Plan Fingerprint

建立唯一 Fingerprint Owner，例如：

```text
PlanFingerprinter
```

Fingerprint 只覆盖静态 Plan 定义。

至少覆盖：

- plan schema version；
- plan version；
- step ID；
- agent/capability；
- dependencies；
- static execution type；
- completion criteria；
  -安全静态配置；
  -敏感 static input 的 digest。

不得覆盖：

- StepStatus；
  -runtime timestamp；
  -attempt count；
  -result；
  -error；
  -budget usage；
  -model retry/fallback result；
  -span ID；
  -event sequence。

规范化规则：

- Step 按 step_id 排序；
- dependencies 排序；
- Mapping Key 排序；
- Enum 使用 `.value`；
- UTF-8；
- canonical JSON；
- `allow_nan=False`；
- SHA-256 lowercase。

测试：

1. 同一 Plan 不同对象实例 Fingerprint 相同；
2. Step 输入顺序变化但静态语义相同，Fingerprint 相同；
3. dependencies 顺序变化，Fingerprint 相同；
4. Step ID 改变，Fingerprint 不同；
5. dependency 改变，Fingerprint 不同；
6. capability 改变，Fingerprint 不同；
7. completion criteria 改变，Fingerprint 不同；
8. static input 正文改变，Fingerprint 不同；
9. StepStatus 改变，Fingerprint 不变；
10. runtime timestamp 改变，Fingerprint 不变。

## 七、Safe AgentState Snapshot

不得持久化完整 `AgentState.to_dict()`。

建立安全 AgentStateSnapshot/StepStateSnapshot，至少包含：

- run_status；
- stop_reason；
- cancellation_reason；
- step_id；
- step status；
- attempt count；
- started/completed UTC 时间；
- duration_ms；
- safe error code；
- result present/length/digest；
- final output present/length/digest；
- state version；
- updated_at。

不得保存：

- final_output 正文；
  -step result 正文；
  -error message；
  -traceback；
  -Model/Tool/RAG/Memory 正文；
  -Provider 响应；
  -本地路径。

审计真实 StepStatus：

- 对实际 In-flight Status 做标记；
- 不得凭空新增 `CLAIMED`；
- `BLOCKED` 不得错误等同于 RUNNING。

## 八、Budget Snapshot

建立安全 BudgetSnapshot，按项目真实维度保存：

- limits；
- used；
- reserved；
- remaining；
- ledger version；
  -可选 reservation count。

要求：

- used/reserved/remaining 为有限非负数；
- `used + reserved <= limit`；
- remaining 与计算值一致；
  -禁用/无限预算使用明确结构，不使用 Infinity；
  -不得保存 Reservation 对象、Lock 或 Owner 引用；
  -不得为了 Quiescent 强行把 reserved 清零。

本轮只序列化，不判断 Runtime 当前是否 Quiescent。

## 九、Runtime Metadata

只允许有限字段：

```text
runtime_schema_version
runtime_mode
planner_version
scheduler_version
model_routing_policy_version
tool_contract_version
retrieval_contract_version
event_schema_version
journal_schema_version
```

不得保存：

-环境变量；
-主机完整路径；
-用户目录；
-数据库连接串；
-API Key；
-Provider URL；
-Thread/Task ID。

## 十、Snapshot Digest

建立版本化 Snapshot Digest。

Digest 至少覆盖：

- snapshot schema version；
- snapshot ID；
- run/trace ID；
- plan snapshot；
- plan fingerprint；
- state snapshot；
- budget snapshot；
- journal sequence；
- run status；
- stop/cancellation reason；
- runtime metadata；
- checkpoint kind；
- quiescent；
- created_at。

要求：

- canonical JSON；
  -SHA-256 lowercase；
  -v1 算法固定；
  -读取时重新计算并验证；
  -错误返回安全 `SNAPSHOT_CORRUPTED`；
  -错误消息不输出 Payload、路径或原始异常。

## 十一、Snapshot Store

提供：

```text
SnapshotStore Protocol
InMemorySnapshotStore
SQLiteSnapshotStore
```

公共接口至少包含：

```text
save(snapshot)
get(snapshot_id)
latest(run_id)
list_for_run(run_id, limit)
close()
```

禁止公共 Update/Delete。

Save Status：

```text
SAVED
DUPLICATE
```

类型化错误：

```text
SNAPSHOT_ID_CONFLICT
SNAPSHOT_CORRUPTED
SNAPSHOT_SCHEMA_UNSUPPORTED
SNAPSHOT_STORE_FAILED
```

SQLite 建议：

```sql
PRIMARY KEY(snapshot_id)
INDEX(run_id, created_at)
```

要求：

-单事务原子插入；
-同 ID/同 Digest 返回 DUPLICATE；
-同 ID/不同 Digest 返回 CONFLICT；
-close 幂等；
-重启后仍可读取并识别 Duplicate；
-读取按 Digest fail closed；
-错误不泄漏数据库路径或 SQL。

## 十二、安全测试

构造敏感标记：

```text
SECRET_PROMPT_TEXT
MODEL_OUTPUT_SECRET
TOOL_ARGUMENT_SECRET
TOOL_OUTPUT_SECRET
RAG_CHUNK_SECRET
MEMORY_SECRET
C:\Users\private-user\kb
provider-secret-error
```

断言：

- Snapshot JSON 不包含；
- SQLite Payload 不包含；
- repr 不包含；
  -错误日志不包含；
  -只有长度/digest/present 安全摘要。

禁止使用 OCR、网络、真实 Model、Chroma 或外部 Tool。

## 十三、坏案例

结果文档至少记录本轮相关 Bad Case：

1. Snapshot 直接 pickle Runtime；
2. Plan Fingerprint 包含 StepStatus；
3. Plan Snapshot 保存 Prompt；
4. AgentState Snapshot 保存 final_output；
5. Budget reserved 被强行清零；
6. Snapshot Digest 未版本化；
   7.同 Snapshot ID 不同内容被当 Duplicate；
   8.SQLite 半写入；
   9.损坏记录被静默跳过；
   10.BLOCKED 被误判为执行中。

使用既定 Bad Case 格式，并区分真实审计与假设构造。

## 十四、测试命令

执行：

```text
uv run python -m pytest \
  tests/test_snapshot_contract.py \
  tests/test_plan_fingerprint.py \
  tests/test_snapshot_store.py \
  tests/test_snapshot_security.py \
  tests/test_runtime_state.py \
  tests/test_runtime_budget.py \
  tests/test_event_journal.py -q
```

执行：

```text
uv run python -m pytest -q
uv run python -m compileall -q core tools tests
uv lock --check
git diff --check
```

## 十五、禁止事项

不得：

-实现 Scheduler Pause；
-实现 Checkpoint Barrier；
-读取 Journal Tail 做 Recovery；
-实现 State Reducer；
-实现 Tool Reconciliation；
-重新执行 Model/Tool/RAG；
-修改 Tool Call 业务语义；
-迁移 `/api/chat`；
-实现第 23 天内容；
-保存 Prompt、RAG、Memory、Tool 正文；
-使用 pickle；
-创建 Event Sourcing；
-创建分布式 Snapshot Store。

## 十六、结果文档

创建：

```text
docs/learning/stage2/result/day22_snapshot_foundation_result.md
```

必须包含：

# 第 22 天第一轮：Snapshot Foundation

## 1. 本轮目标

## 2. 修改前对象审计

## 3. Snapshot Schema

## 4. Safe Plan Snapshot

## 5. Plan Fingerprint

## 6. Safe AgentState Snapshot

## 7. StepStatus 真实边界

## 8. Budget Snapshot

## 9. Runtime Metadata

## 10. Snapshot Digest

## 11. InMemory Store

## 12. SQLite Store

## 13. Duplicate / Conflict

## 14. Sensitive Data Protection

## 15. Bad Case

## 16. 测试结果

## 17. 未完成事项

## 18. 下一轮接入点

## 19. 需要带回 ChatGPT 审查的信息

## 十七、完成后输出

结果文档路径：

新增文件：

修改文件：

Plan static fields：

Plan sensitive fields：

StepStatus actual values：

In-flight statuses：

Blocked semantics：

Snapshot schema version：

Plan snapshot：

Plan fingerprint owner：

Fingerprint includes：

Fingerprint excludes：

AgentState snapshot：

StepState snapshot：

Final output policy：

Budget snapshot：

Reserved policy：

Runtime metadata：

Snapshot digest：

InMemory store：

SQLite store：

Save status：

Duplicate：

Conflict：

Corruption：

Atomic write：

Safe payload：

新增测试：

目标 pytest：

全仓 pytest：

compileall：

lock check：

diff check：

需要人工确认的问题：

# 二十二、第 22 天当前验收清单

## 理论与架构

-  理解 Snapshot 与内存对象复制的区别
-  理解 Checkpoint Barrier
-  理解 Plan Fingerprint
-  理解 Quiescent Snapshot
-  理解 Journal Watermark
-  理解 Limited Replay
-  理解 Tool Side-effect Reconciliation
-  理解 Crash Window
-  理解 Snapshot 不等于 Recovery
-  理解合法 Sequence Gap

## 第一轮待完成

-  RunSnapshot Contract
-  Safe Plan Snapshot
-  Plan Fingerprint
-  Safe AgentState Snapshot
-  Safe Budget Snapshot
-  Runtime Metadata
-  Versioned Digest
-  InMemory Snapshot Store
-  SQLite Snapshot Store
-  Snapshot Security Tests
-  第一轮 ChatGPT 审查

## 第二轮待完成

-  Checkpoint Barrier
-  Scheduler Claim Gate
-  Journal Watermark
-  Quiescent Detection
-  Non-quiescent Audit Snapshot
-  Runtime Integration

## 第三轮待完成

-  Recovery Validator
-  Recovery Assessment
-  Limited Journal Tail Reducer
-  Tool Side-effect Decision
-  Zero Model/Tool/RAG Replay
-  第 22 天最终文档与全仓验收

**阶段二第 22/25 天：理论和架构完成，进入第一轮 Snapshot Foundation 实操。**