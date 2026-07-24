# 阶段二第 17 天改造结果

## 1. 本次目标

本次建立最小但完整的 Tool Execution Contract，把 Tool Invocation、Attempt、Budget、Cancellation、Timeout、Concurrency、Resource Lease、Retry Safety、Output Limit 和 Runtime Event 收敛到 Runtime。没有创建 Tool Registry、Skill、MCP、A2A、Discovery 或远程 Marketplace。

真实迁移范围严格限定为：

- 有副作用样本：`complex_workflow_simulator`
- 只读对照：`get_system_status`

`list_files` 与 `analyze_excel` 仍保留 Legacy 直接调用。

## 2. 修改前 Tool 调用链

修改前真实链路为：

```text
server lifespan
-> tools.registry.register_all_tools
-> AgentRouter.register_tool
-> Tool Planner 输出 CALL: tool_name(argument_text)
-> AgentRouter._parse_tool_call
-> AgentRouter._prepare_answer_messages
-> self.tools[tool_name]["func"](tool_args)
-> str(observation)
-> 截断后注入模型 Context
```

修改前的全部真实入口与旁路检查结果：

- `tools/registry.py`：四个内置 Tool 的硬编码注册入口。
- `AgentRouter._plan_tool_call()`：Planner 选择入口。
- `AgentRouter._parse_tool_call()`：`CALL:`/兼容 `Action:` 解析入口。
- `AgentRouter._prepare_answer_messages()`：唯一生产 Tool 调用入口。
- `complex_workflow_simulator(argument_text)`：复杂 Tool 的 Legacy JSON Wrapper。
- `ComplexWorkflowSimulationTool.execute(request)`：强类型模拟器入口；此前只被 Wrapper 和测试直接调用。
- 测试会直接调用 Tool/Adapter，但未发现第二条生产调用链。

修改前预算由 AgentRouter 手工预留，Tool 没有统一 Result/Error、正式 Timeout、正式 CancellationToken、Resource Lease 或 Tool Event。

## 3. Tool Execution Contract

新增模块：

```text
core/runtime/tool_contract.py
core/runtime/tool_execution.py
core/runtime/tool_adapters.py
core/runtime/tool_concurrency.py
```

目标链路已经形成：

```text
AgentRouter / Step Driver
-> ToolExecutionService
-> RetryExecutor
-> ToolAttemptExecutor
-> ToolConcurrencyController / ToolResourceLease
-> BudgetLedger
-> Cancellation / effective deadline
-> ToolAdapter.invoke_once
-> ToolExecutionResult / ToolExecutionError
-> TOOL_STARTED / TOOL_COMPLETED
```

`ToolExecutionService` 不解析 Planner 自由文本；文本解析仍属于 AgentRouter/Adapter 的 Transport 边界。Adapter 只执行一次，不拥有 Retry、Budget、RunStatus 或 StepStatus。

## 4. ToolInvocation

`ToolInvocation` 是 frozen dataclass，字段为：

- `invocation_id`
- `tool_name`
- `arguments`
- `arguments_digest`
- `idempotency_key`
- `resource_key`
- `requested_timeout_seconds`

`arguments` 只接受 JSON-safe object，递归冻结为只读 Mapping/tuple；拒绝 bytes、任意对象、非字符串 object key、NaN 和 Infinity。摘要使用排序 Key、固定分隔符、`ensure_ascii=True`、`allow_nan=False` 的规范 JSON 后计算 SHA-256。

同一 Invocation 的 Retry 复用原对象，所以 `invocation_id`、`arguments_digest`、Idempotency Key 和 Resource Key 均保持稳定。每次 Attempt 由 `ToolAttemptExecutor` 新建不同的 `attempt_id`。

安全序列化不包含 arguments 正文，只包含 arguments、Idempotency Key 和 Resource Key 的安全 Digest。

## 5. ToolExecutionSpec

每个 Adapter 携带不可变 `ToolExecutionSpec`：

- `tool_name`
- `side_effect_kind`
- `idempotency`
- `requires_resource_key`
- `supports_cooperative_cancellation`
- `supports_side_effect_checkpoint`
- `default_timeout_seconds`
- `max_output_bytes`
- `max_concurrency`
- `supports_idempotency_replay`

有限正数、正整数和 bool 均做严格类型校验，整数位置拒绝 bool。未知属性默认使用 `UNKNOWN`，不会乐观推断可重试。

复杂 Tool 的 Spec 会依据当前 Invocation 的 execution mode 动态派生；这只是单个 Adapter 的执行元数据，不是 Registry。

## 6. ToolExecutionContext

不可变 `ToolExecutionContext` 提供：

- `RunContext`
- `step_id`
- `attempt_id`
- `retry_index`
- `BudgetLedger`
- `StepEventEmitter`
- `ToolConcurrencyController`
- effective monotonic deadline
- Attempt 级只读 CancellationToken
- Attempt Side Effect Tracker

安全方法为：

- `raise_if_cancelled()`
- `remaining_seconds()`
- `before_side_effect()`

Context 不暴露 Run/Step 状态机写能力，也不创建新的 BudgetLedger。

## 7. Side Effect Kind / State

Tool 固有声明：

```text
NONE
LOCAL_STATE_MUTATION
EXTERNAL_STATE_MUTATION
IRREVERSIBLE
UNKNOWN
```

单次 Attempt 实际状态：

```text
NOT_STARTED
STARTED
COMMITTED
COMPENSATED
UNKNOWN
```

Kind 来自 Spec；State 来自 Attempt Tracker 与 Adapter 的实际结果映射。异常类型不能证明副作用未发生。`before_side_effect()` 把状态推进到 `STARTED` 后，普通异常、Timeout、Cancellation 或任何没有权威副作用结果的错误都会保守收口为 `UNKNOWN + OUTCOME_UNKNOWN`，不会把 `STARTED` 当成终态，也不会退回 `NOT_STARTED`。只有 Adapter 显式声明结果具有权威性时，Runtime 才接受 `COMMITTED`、`COMPENSATED` 或 `NOT_STARTED`。同步 Worker 超时且未停止时同样强制进入 `UNKNOWN`。

## 8. Idempotency

复用第 15 天的：

```text
READ_ONLY
IDEMPOTENT
IDEMPOTENT_WITH_KEY
NON_IDEMPOTENT
UNKNOWN
```

规则：

- `IDEMPOTENT_WITH_KEY` 没有稳定非空 Key 时 Contract 校验失败。
- READ_ONLY/IDEMPOTENT 只有在错误分类可恢复且副作用状态安全时才允许 Retry。
- `IDEMPOTENT_WITH_KEY` 在副作用尚未提交且错误可恢复时返回 `SAFE_WITH_IDEMPOTENCY_KEY`。
- `COMMITTED` 不再统一禁止 Retry。只有 `IDEMPOTENT_WITH_KEY`、稳定非空 Key、相同不可变 Invocation/arguments digest、Tool 明确支持相同 Key Replay、错误明确为 `POST_COMMIT_RESPONSE_FAILURE`、没有 Partial Output、没有补偿失败，并且 Budget/Deadline/Cancellation 仍允许时，才返回 `SAFE_WITH_IDEMPOTENCY_KEY`。
- `NON_IDEMPOTENT`、`UNKNOWN`、缺少 Key、Key 冲突、arguments digest 不一致、不可重复 Output 已开始、`UNKNOWN` outcome、补偿失败以及 Validation/Permission/Safety 类错误均禁止自动 Retry。
- `TOOL_SIDE_EFFECT_FAILURE` 只在复杂模拟器中明确映射到安全分类 `POST_COMMIT_RESPONSE_FAILURE`；没有把其他提交后错误笼统改成 transient。

复杂 Tool 映射：

- `DRY_RUN -> READ_ONLY`
- `IDEMPOTENT_COMMIT -> IDEMPOTENT_WITH_KEY`
- `NON_IDEMPOTENT_SIMULATION -> NON_IDEMPOTENT`

## 9. Resource Key

Resource Key 表示业务资源互斥身份，Idempotency Key 表示请求去重身份，二者职责分离，不能混用。

Key 正文只存在于执行对象内；事件和安全序列化默认只记录 SHA-256 Digest。复杂 Tool 要求 Resource Key；只读 Tool 不要求 Resource Key。

## 10. Tool Concurrency

`ToolConcurrencyController` 是应用级、线程安全、Event Loop 无关的轻量 Controller：

- 全局 BoundedSemaphore。
- 每 Tool BoundedSemaphore。
- Resource Key 进程内互斥集合。
- 可取消、Deadline-aware 轮询等待。
- 成功、错误和取消后的幂等释放。
- 同 Key 跨 Run 互斥，不同 Key 可并行。
- 不含业务正文的同步 Worker Tracker。

Controller 由应用生命周期的 `AgentRouter -> ToolExecutionService` 持有。它不是 Scheduler，不持久化，不实现跨进程或分布式锁。

组合 Lease 同时拥有 Global Permit、Per-tool Permit 与 Resource Key Permit。同步 Tool 超时后若 Worker 未结束，三者均保持占用；Future callback 只在 Worker 真正结束后 exactly once 释放整个 Lease。重复 cleanup 和重复 `release()` 不会过度释放。测试分别用全局上限 1、每 Tool 上限 1 和相同 Resource Key 证明三类许可均未被提前释放。

Controller 以应用生命周期跟踪同步 Worker，安全快照包含 `active_worker_count`、`detached_worker_count`、Invocation/Attempt ID、UTC `started_at`、Tool 名称、Resource Key Digest 和 cleanup state；不保存 arguments、output 或原始异常。`wait_until_idle(timeout)` 支持正常等待和有界超时。Worker 后续结束时只释放许可并从 Tracker 注销，不再发布 `TOOL_COMPLETED`，也不写用户输出或修改 Run/Step 状态。

本次没有修改 Server lifespan。后续 Shutdown 接入点必须按“等待 RunRegistry → 等待 Detached Tool Worker grace period → 超时后只记录安全 Worker ID”执行；不得强杀 Python Thread，也不得把 `RunRegistry` 已空解释为所有底层 Tool Worker 已结束。

## 11. Tool Adapter

`ToolAdapter.invoke_once()` 的约束：

- 只执行一次。
- 不 Retry。
- 不 reserve Budget。
- 不修改 RunStatus/StepStatus。
- 不编码 `[[ORCH]]`。
- 不创建 Run CancellationSource。
- 不返回原始 Exception。

`ComplexWorkflowToolAdapter` 直接调用 `ComplexWorkflowSimulationTool.execute()`，没有绕回 Legacy JSON Wrapper。它把 Runtime Context 的取消检查和正式 `before_side_effect` 注入强类型模拟器。

`LegacyStringToolAdapter` 只包裹已确认只读的 `get_system_status`，校验返回类型和配置的错误字符串前缀，并把输出大小限制交给统一 Attempt Executor。

## 12. ToolExecutionResult

结果字段：

- Invocation/Attempt/Tool 身份
- `status`
- `output`
- `safe_summary`
- `side_effect_state`
- `idempotency_replayed`
- `retry_disposition`
- `resource_key_digest`
- UTC 起止时间和 `duration_ms`
- `retry_index`
- `worker_terminated`
- `execution_detached`
- `resource_release_pending`

安全序列化默认只输出 ToolOutput 的类型、大小、截断标记和 Digest，不输出 `output.content`。

`ToolOutput` 包含：

- `content_type`
- `content`
- `original_size_bytes`
- `returned_size_bytes`
- `truncated`
- `digest`

输出策略按 `content_type` 区分，Digest 始终基于完整原文：

- `text/plain`：允许 UTF-8 字节边界安全截断，保证返回字符串仍是合法 UTF-8。
- `application/json` 与 `+json`：先验证 JSON；未超限时保持原结构和类型，超限时返回 `TOOL_OUTPUT_TOO_LARGE`，不产生被截断的非法 JSON。
- binary/unknown：返回 `TOOL_OUTPUT_CONTENT_TYPE_UNSUPPORTED`，不让正文进入模型 Context。

错误、安全序列化和 Runtime Event 只携带 content type、完整大小和 Digest 等安全元数据，不携带结构化正文。

## 13. ToolExecutionError

错误是安全强类型数据，不保存原始异常、traceback、arguments 或 Tool Output 正文。分类覆盖：

```text
VALIDATION
NOT_FOUND
PERMISSION_DENIED
RESOURCE_CONFLICT
TRANSIENT
POST_COMMIT_RESPONSE_FAILURE
TIMEOUT
CANCELLED
DEADLINE_EXCEEDED
BUDGET_EXHAUSTED
OUTPUT_INVALID
OUTPUT_TOO_LARGE
SIDE_EFFECT_UNKNOWN
COMPENSATION_FAILED
INTERNAL
```

字段包括 category、safe code/message、phase、provider_started、side effect state、retry disposition、最小安全 partial result、补偿事实、Attempt 身份，以及 `worker_terminated`、`execution_detached`、`resource_release_pending`。

## 14. RetryDisposition

固定值：

```text
SAFE
SAFE_WITH_IDEMPOTENCY_KEY
UNSAFE
OUTCOME_UNKNOWN
```

判定同时考虑 Error Category、OperationIdempotency、Key、SideEffectState、arguments digest、Output 是否已开始、Replay 能力和 Compensation。一般只有 transient、timeout、resource conflict 进入进一步安全判定；已提交副作用的唯一恢复入口是明确的 `POST_COMMIT_RESPONSE_FAILURE + IDEMPOTENT_WITH_KEY + Replay Support`。“TRANSIENT”本身不等于可重试。

## 15. Budget

每个真实 Attempt 原子预留：

```text
tool_calls = 1
retries = 0（initial）或 1（retry）
```

顺序为：

1. Contract 校验。
2. Cancellation/Deadline 检查。
3. 全局/每 Tool Permit。
4. Resource Lease。
5. Budget reserve。
6. Cancellation/Deadline 复验。
7. `TOOL_STARTED`。
8. `invoke_once`。
9. Budget commit。
10. `TOOL_COMPLETED`。
11. Lease release；runaway sync Worker 延迟释放。

预算不足时 Adapter 未调用，也不发布 `TOOL_STARTED`。未开始的 Reservation release；进入真实 Adapter 边界后保守 commit。

## 16. Timeout

有效 Timeout 为：

```text
min(
    ToolExecutionSpec.default_timeout_seconds,
    ToolInvocation.requested_timeout_seconds（如有）,
    Run Deadline remaining（如有）
)
```

Async Adapter 使用可取消 Task，并在取消后等待 cleanup。当前迁移的两个 Adapter 都是同步 Adapter。

同步 Worker 超时只表示 Runtime Attempt 停止等待，不表示 Python Thread 已停止。Grace Period 后 Worker 仍活跃时，返回：

```text
status = TIMED_OUT
side_effect_state = UNKNOWN
retry_disposition = OUTCOME_UNKNOWN
worker_terminated = False
execution_detached = True
resource_release_pending = True
```

这类结果不自动 Retry。后台 Future 结束后只执行许可释放和 Tracker 注销，不修改已返回的 Error、不发布第二个 Completed，也不触碰 RunStatus。

## 17. Cancellation

检查点覆盖：

- 调用前。
- 并发许可/Resource wait 中。
- 取得 Resource 后。
- Budget reserve 后。
- `TOOL_STARTED` 后。
- `before_side_effect`。
- 同步 Worker 等待中。
- Adapter 返回后。
- 最终返回前。

Run CancellationReason 仍由现有 Run CancellationSource first-wins；Tool 不覆盖原因。复杂 Adapter 在模拟器返回后再次执行正式 Context 检查，避免模拟器自己的安全 Result 吞掉 Run Cancellation。

## 18. before_side_effect

正式检查点执行：

1. Run Cancellation。
2. Attempt Cancellation。
3. Run/Attempt Deadline。
4. `NOT_STARTED -> STARTED` 原子转换。
5. 拒绝重复和非法转换。

复杂模拟器在 `COMMIT_SIDE_EFFECTS` 的真实 Store commit 前调用注入回调。`DRY_RUN` 不调用副作用检查点。Legacy Wrapper 使用空回调，保持兼容。

## 19. RetryExecutor 协同

唯一 Retry Owner 仍是第 15 天的 `RetryExecutor`。ToolExecutionService 通过其 `execute_async()` 调度 Attempt，并把 Tool Error 映射为既有失败分类，同时额外强制检查 RetryDisposition。

每次 Retry 重新取得并发许可、Resource Lease 和 Budget Reservation，复用原 Invocation/Key，生成新 Attempt ID。Retry 耗尽返回安全 ToolExecutionError，不决定 Run 终态。

当前默认 Tool backoff 为零；非零延迟仍由注入的 RetryExecutor 策略决定。Adapter、模拟器和 Legacy function 均无内部 Retry。

## 20. Tool Runtime Event

正式接入：

- `TOOL_STARTED`
- `TOOL_COMPLETED`

Started 只在 Adapter/Spec 校验、Concurrency、Resource、Budget 和取消复验全部成功后、即将调用 Adapter 时发布。Completed 只对应已发布的 Started，成功、失败、超时和取消均携带安全元数据。

事件字段可包含 Invocation ID、Attempt ID、retry index、SideEffectState、RetryDisposition、Resource Key Digest、`worker_terminated`、`execution_detached` 和 `resource_release_pending`；不包含 arguments、Key 正文或完整 output。

`TOOL_COMPLETED` 的准确语义是“Runtime Attempt 已结束等待”；当 `execution_detached=true` 时，底层同步 Worker 可能仍在执行。Worker 后续退出不会产生第二个 Completed。

Started 发布失败时 Tool 不调用。Completed 发布失败发生在 Tool 已执行之后，会返回保守 INTERNAL Error 且禁止透明 Retry。

## 21. Complex Workflow Tool 接入

Adapter 映射包括：

- JSON object -> `ComplexWorkflowRequest`
- execution mode -> Idempotency/SideEffectKind
- Resource/Idempotency Key 一致性校验
- Runtime cancellation probe
- 正式 `before_side_effect`
- `ComplexWorkflowResult` -> Result/Error
- replay、partial success、commit、compensation 映射
- safe error code -> Error Category
- SideEffectState 与 RetryDisposition

Adapter 模式下模拟器收到无锁的 Runtime-owned lock bridge，避免模拟器内部锁再次冒充正式 Resource Contract。Legacy Wrapper 仍保留自己的旧锁，仅用于未迁移兼容调用。

真实集成用例覆盖 `IDEMPOTENT_COMMIT + FAIL_AFTER_SIDE_EFFECT + stable idempotency_key`：首次 Attempt 完成一次 Store commit 后得到 `COMMITTED + SAFE_WITH_IDEMPOTENCY_KEY`，第二次复用同一 invocation ID、Key 和 arguments digest，以新 attempt ID/retry index 得到 `IDEMPOTENCY_REPLAY`。最终成功，Store 提交记录仍只有一条，预算为 `tool_calls=2, retries=1`，并产生两组 Started/Completed。对照的 `NON_IDEMPOTENT_SIMULATION + FAIL_AFTER_SIDE_EFFECT` 只调用一次、只提交一次且不 Retry。

## 22. Read-only Tool 接入

选择 `get_system_status`，理由：

- 只读。
- 无外部写副作用。
- 无内部 Retry。
- 参数是安全字符串。
- 输出是有限文本且可统一限制。

其 Spec 为 `NONE + READ_ONLY`，默认 3 秒 Timeout、4096 bytes 输出上限、每 Tool 并发 2。测试用注入的本地函数覆盖 transient、错误字符串和截断；不调用外部服务。

## 23. AgentRouter 真实路径

现有 Planner 仍输出：

```text
CALL: tool_name(argument_text)
```

解析后：

- tool info 带已附着 Adapter：Adapter 构建 ToolInvocation，AgentRouter 调用 ToolExecutionService。
- tool info 没有 Adapter：继续 Legacy function 直接调用与原预算逻辑。

分支是互斥的。已迁移路径在 Service 返回后不会继续调用 `self.tools[tool_name]["func"]`，也不会执行 AgentRouter 旧的 Tool Budget Reservation；预算只由 ToolAttemptExecutor 按真实 Attempt 提交。测试覆盖两个 migrated Tool、提交后 Replay、migrated Error、未迁移 Legacy 对照和观察结果只注入一次。

`register_tool()` 仍维护原硬编码字典；`attach_tool_adapter()` 只在已注册项上附着执行元数据。没有创建第二个 Registry 或 Discovery。

Coordinated 路径从 Worker Thread 把 coroutine 提交回 StepEventEmitter 所属 Event Loop，因此 Tool Event 与 Model/Step Event 使用同一 Channel。无 Emitter 的 Legacy 路径使用本地 async bridge。

## 24. Tool 不修改 Runtime 状态

搜索与测试确认：

- Tool 不 set RunStatus。
- Tool 不 set StepStatus。
- Tool 不 finalize Run。
- Tool 不 unregister Run Registry。
- Tool 不直接取消 Run。
- Tool 不创建 BudgetLedger。
- Adapter 不 Retry。

Tool 只返回 Result 或安全 Error；Driver/ParallelExecutor/RunCoordinator 继续拥有状态收敛。

## 25. Legacy 与未迁移路径

已迁移：

- `complex_workflow_simulator`
- `get_system_status`

仍为 Legacy：

- `list_files`
- `analyze_excel`
- 复杂模拟器的公开 `complex_workflow_simulator(argument_text)` Wrapper（仅兼容直接调用；AgentRouter 已迁移路径不绕回它）
- 测试直接调用 `ComplexWorkflowSimulationTool.execute()` 的模拟器专项路径

Legacy `list_files`/`analyze_excel` 仍可能把错误编码为成功字符串，这是本次明确保留的兼容边界。当前 Tool 内部均未发现 Retry。复杂模拟器会修改自身显式 State Store，但不会修改 Runtime 状态。

Resource Key 所有权属于 ToolInvocation/调用边界；互斥所有权属于应用级 ToolConcurrencyController。

## 26. 重点 Bad Case

### Bad Case 1：失败即认为副作用未发生

- 类型：假设构造
- 触发条件：Adapter 在提交后返回失败或连接中断。
- 故障表现：调用方重复提交，产生重复副作用。
- 根因分析：用异常类型代替 SideEffectState。
- 修复方案：独立跟踪 NOT_STARTED/STARTED/COMMITTED/COMPENSATED/UNKNOWN；仅对明确支持相同 Key Replay 的提交后响应失败开放安全重放。
- 回归测试：复杂 Tool 提交后失败 Replay、NON_IDEMPOTENT 对照、补偿成功/失败及 RetryDisposition 测试。
- 对应知识点：Outcome Uncertainty。
- 面试表达：失败是传输事实，不是业务未提交证明。
- 当前状态：已防护。

### Bad Case 2：同步 Tool Timeout 后提前释放 Resource

- 类型：假设构造
- 触发条件：线程 Worker 超时但仍在执行。
- 故障表现：新调用提前取得 Global、Per-tool 或 Resource Permit，与旧 Worker 重叠。
- 根因分析：把 asyncio Timeout 误认为线程停止。
- 修复方案：触发协作取消、有限 Grace；未停止则 UNKNOWN，并由 Future callback 延迟释放组合 Lease；应用级 Tracker 记录 Detached Worker。
- 回归测试：`test_sync_timeout_keeps_resource_until_worker_finishes` 与 Global/Per-tool/Resource 三组延迟释放用例。
- 对应知识点：Thread Cancellation / Lease Lifetime。
- 面试表达：超时只停止等待，不能强杀 Python 线程。
- 当前状态：已防护。

### Bad Case 3：Retry 时重新生成 Idempotency Key

- 类型：假设构造
- 触发条件：每个 Attempt 重新构造 Invocation。
- 故障表现：Provider 把 Retry 当新请求。
- 根因分析：混淆 Invocation 和 Attempt 身份。
- 修复方案：Retry 复用 ToolInvocation，只生成新 attempt_id。
- 回归测试：Invocation ID 固定、Attempt ID 变化测试。
- 对应知识点：Stable Invocation Identity。
- 面试表达：一次意图多个 Attempt，幂等 Key 属于意图。
- 当前状态：已防护。

### Bad Case 4：Resource Key 与 Idempotency Key 混用

- 类型：假设构造
- 触发条件：用去重 Key 做业务资源锁或反之。
- 故障表现：无关请求错误互斥，或同资源并发写。
- 根因分析：两个 Key 的职责未建模。
- 修复方案：Invocation 分字段、分 Digest、分校验。
- 回归测试：同/不同 Resource Key 并发测试与 WITH_KEY Contract 测试。
- 对应知识点：Mutual Exclusion vs Deduplication。
- 面试表达：一个回答“锁谁”，一个回答“是否同一请求”。
- 当前状态：已防护。

### Bad Case 5：Tool 修改 RunStatus

- 类型：真实检查
- 触发条件：Tool/Adapter 直接收敛 Runtime 状态。
- 故障表现：RunCoordinator 与 Tool 竞争 terminal owner。
- 根因分析：业务执行越过 Driver 边界。
- 修复方案：Context 不暴露状态写能力；Result/Error 交给现有 Executor。
- 回归测试：源码边界检查及 RunCoordinator 全回归。
- 对应知识点：Single Terminal Owner。
- 面试表达：Tool 报告事实，Runtime 决定状态。
- 当前状态：未发现违规。

### Bad Case 6：NON_IDEMPOTENT Tool 自动 Retry

- 类型：假设构造
- 触发条件：非幂等调用返回 transient。
- 故障表现：副作用执行两次。
- 根因分析：只按 Error Category 判定 Retry。
- 修复方案：RetryDisposition 联合 Idempotency 和 SideEffectState。
- 回归测试：非幂等 transient 只执行一次。
- 对应知识点：Retry Safety。
- 面试表达：Transient 只表示可能恢复，不表示可以重放。
- 当前状态：已防护。

### Bad Case 7：副作用提交前不重新检查取消

- 类型：真实设计缺口修复
- 触发条件：处理阶段后、Store commit 前收到取消。
- 故障表现：已取消 Run 仍提交业务变更。
- 根因分析：仅依赖模拟器早期布尔 Probe。
- 修复方案：在 COMMIT_SIDE_EFFECTS 前注入正式 Context.before_side_effect。
- 回归测试：Side Effect Tracker 状态机、复杂 Tool 取消回归。
- 对应知识点：Commit Barrier。
- 面试表达：取消检查必须贴近不可逆边界。
- 当前状态：已修复。

### Bad Case 8：Tool Output 无限制进入 Context

- 类型：真实旧链路风险
- 触发条件：Legacy Tool 返回超大文本。
- 故障表现：模型 Context 膨胀、日志泄漏或内存压力。
- 根因分析：只做字符截断且没有完整大小/Digest。
- 修复方案：仅 `text/plain` 做 UTF-8 bytes 安全截断；JSON 超限硬失败，binary/unknown 拒绝；完整 Digest 基于原始正文，安全序列化隐藏正文。
- 回归测试：多字节安全截断、JSON 保持结构/超限失败、unknown 类型拒绝、safe dict 和事件无正文测试。
- 对应知识点：Output Boundary。
- 面试表达：Tool Output 是不可信输入，必须限流和显式进入正文。
- 当前状态：两个迁移 Tool 已修复；Legacy Tool 仍保留旧限制。

### Bad Case 9：TOOL_STARTED 在 Budget 前发布

- 类型：假设构造
- 触发条件：先发 Started，后发现预算不足。
- 故障表现：出现没有真实 Attempt 的假 Started。
- 根因分析：事件时序早于许可边界。
- 修复方案：Resource/Permit/Budget/取消复验后才发 Started。
- 回归测试：Budget failure 无 Tool Event。
- 对应知识点：Event Truthfulness。
- 面试表达：Started 必须意味着已获准且即将调用。
- 当前状态：已防护。

### Bad Case 10：模拟 Tool 内部锁冒充 Runtime Resource Contract

- 类型：真实边界问题
- 触发条件：Adapter 继续使用模拟器内部 WorkflowResourceLockManager。
- 故障表现：双重锁、冲突语义不一致，无法跨 Tool 统一。
- 根因分析：把业务样本的准备结构当成 Runtime 所有权。
- 修复方案：Adapter 注入无锁 bridge，正式互斥只由 ToolConcurrencyController 持有。
- 回归测试：同 Key 跨 Run 排他、不同 Key 并行。
- 对应知识点：Concurrency Ownership。
- 面试表达：业务对象声明资源，Runtime 持有租约。
- 当前状态：迁移路径已修复；Legacy Wrapper 保留旧锁。

### Bad Case 11：Legacy 错误字符串被当作成功

- 类型：真实 Legacy 风险
- 触发条件：Tool 捕获异常并返回 `"ERROR: ..."` 字符串。
- 故障表现：Step 成功且错误正文进入模型 Context。
- 根因分析：`str -> str` 没有 Result/Error 分离。
- 修复方案：LegacyStringToolAdapter 支持显式错误前缀验证；只迁移已确认输出协议的只读 Tool。
- 回归测试：`LEGACY_TOOL_REPORTED_ERROR` 测试。
- 对应知识点：Typed Error Boundary。
- 面试表达：字符串协议必须先验证，不能凭“未抛异常”判成功。
- 当前状态：迁移的只读 Adapter 已防护；其他 Legacy Tool 仍有风险。

### Bad Case 12：补偿失败后自动 Retry

- 类型：假设构造
- 触发条件：原提交和补偿结果均不确定或补偿明确失败。
- 故障表现：状态进一步分叉，人工恢复更困难。
- 根因分析：把 compensation attempted 当作已回滚。
- 修复方案：只有明确 compensation_succeeded 才映射 COMPENSATED；失败统一 UNSAFE。
- 回归测试：复杂模拟器 compensation failure 与 RetryDisposition 回归。
- 对应知识点：Saga Compensation。
- 面试表达：补偿是另一项可能失败的副作用，不是事务回滚。
- 当前状态：已防护。

## 27. 测试命令和结果

异步测试使用正式开发依赖 `pytest-asyncio>=1.0.0`，当前锁定版本为
`1.4.0`。用例直接使用 `@pytest.mark.asyncio`，不再通过自定义
`asyncio.run` Wrapper 适配。

为避免受限环境首次运行时尝试初始化 Windows 用户目录
`%LOCALAPPDATA%\uv\cache`，项目在 `[tool.uv]` 中将 `cache-dir` 固定为
同文件系统、仓库内且被 Git 忽略的 `.uv-cache`。实际验证
`uv cache dir` 返回 `.uv-cache`，普通 `uv run` 可直接加载测试插件；
`uv lock --check` 通过，确认 `pyproject.toml` 与 `uv.lock` 同步。

实际执行：

```text
uv run python -m pytest \
  tests/test_tool_contract.py \
  tests/test_tool_execution.py \
  tests/test_tool_concurrency.py \
  tests/test_tool_execution_integration.py \
  tests/test_complex_workflow_simulator.py \
  tests/test_runtime_event_integration.py \
  tests/test_retry_executor.py \
  tests/test_budget.py -q
```

结果：`101 passed`。

附件指定 Runtime unittest：

```text
uv run python -m unittest <附件列出的 27 个模块> -q
```

结果：`Ran 226 tests ... OK`。新增四个文件使用 pytest function/asyncio
风格，因此 unittest 命令只验证其模块可导入；其真实断言由上方 pytest
命令和 `pytest-asyncio` 执行。

全仓：

```text
uv run python -m pytest -q
```

结果：`402 passed, 42 subtests passed`。

最终回归在上述 `uv run` 命令中增加 `--locked` 后再执行一次，结果相同，
确保测试没有隐式修改或绕过锁文件。

```text
uv run python -m compileall -q core tools tests
git diff --check
```

最终 `compileall` 与 `git diff --check` 均通过。

所有新增测试只使用本地 Fake、内存 Store、线程/asyncio 和临时 Runtime 对象；没有调用真实模型、网络、数据库服务、外部 Tool、Chroma 或 UI。`get_system_status` 的 Contract 测试使用注入函数，不依赖真实系统采样。

## 28. 未完成事项和已知风险

- 只有两个 Tool 迁移，其他 Tool 仍是 Legacy。
- 不存在新的 Tool Registry；现有硬编码字典仍负责名称映射。
- Resource Key 互斥仅单进程内存级。
- 同步 Tool 无法强杀；超时 Worker 可能继续占用线程与 Resource Lease。
- `RunRegistry` 已空不代表 Detached Tool Worker 已结束；Server lifespan 尚未接入 `wait_until_idle` grace period。
- Outcome Unknown 禁止自动 Retry，需要人工或业务对账。
- `text/plain` 截断可能影响业务语义；调用方应检查 `truncated`。JSON 不截断，超限直接失败。
- Tool Retry 依赖显式 Idempotency 声明；错误声明会破坏安全性。
- 默认 Planner 仍是自由文本 `CALL:` 协议。
- Tool Event 不含完整 arguments 或 output。
- 尚未验证真实外部 Tool、远程 Provider 或不可取消 C 扩展。
- 多 Worker/多进程之间不共享 Resource Lock。
- ToolConcurrencyController 不是 Scheduler，没有公平性、优先级或持久化。
- `get_system_status` 自身不支持 cooperative cancellation，线程超时遵循 Unknown outcome 规则。
- Legacy `list_files`/`analyze_excel` 仍以字符串编码部分错误。
- `.uv-cache` 是可删除的本地构建缓存，不应提交到版本库；依赖的权威来源是
  `pyproject.toml` 与 `uv.lock`。

需要人工确认：

- 未来是否迁移 `list_files` 和 `analyze_excel`，迁移前需先定义其目录访问策略、错误协议和输出语义。
- 生产部署是否会使用多 Worker；若会，当前进程内 Resource Key 不能提供跨 Worker 一致性。
- 业务是否接受截断后的 `text/plain` 进入模型 Context，或应对部分文本 Tool 也改为 `OUTPUT_TOO_LARGE`。

## 29. 面试表达

我把 Tool 的一次业务意图建模为稳定 Invocation，把每次真实重放建模为 Attempt。Runtime 在 Adapter 外统一取得 Global/Per-tool/Resource 组合 Lease 和 Budget，发布 Started 后只调用一次 Adapter。RetryExecutor 是唯一 Retry Owner；已提交副作用只有在稳定 Key、相同 Invocation/digest、明确 Replay 能力和可恢复的提交后响应失败同时成立时才允许重放。同步线程超时不等于线程停止，因此未退出 Worker 必须标为 Unknown/Detached，全部许可延迟到 Worker 真正结束才释放，并由应用级安全 Tracker 管理。Tool 只报告 Result/Error，不修改 Run/Step 终态；文本可安全截断，JSON 超限硬失败，事件和日志只记录安全元数据与 Digest。

## 30. 需要带回 ChatGPT 审查的信息

- 新增文件：四个 Runtime Tool 模块、四个测试模块、本结果文档。
- 修改文件：AgentRouter、Runtime exports/events、复杂模拟器正式检查点、
  硬编码工具注册、`.gitignore`、`pyproject.toml` 与 `uv.lock`。
- ToolInvocation：七个要求字段，递归不可变 JSON-safe arguments。
- Invocation/Attempt：Retry 复用 invocation_id，每 Attempt 新 attempt_id。
- Spec：Adapter 不可变执行元数据；不是 Registry。
- Side Effect：Kind 五类、State 五态。
- Idempotency：复用第 15 天五类；复杂 Tool 动态映射；已提交结果只对明确的同 Key Replay 开放。
- Resource Key：与 Idempotency Key 分离；安全序列化只给 Digest。
- Concurrency Owner：应用级 ToolConcurrencyController。
- Context：Run/Step/Attempt/Budget/Event/Controller/deadline 与三个安全方法。
- before_side_effect：取消和 Deadline 复验后执行 NOT_STARTED -> STARTED。
- Adapter：只执行一次，不 Retry、不记预算、不写 Runtime 状态。
- Result/Error：强类型、安全序列化默认隐藏正文与原始异常。
- RetryDisposition：SAFE、SAFE_WITH_IDEMPOTENCY_KEY、UNSAFE、OUTCOME_UNKNOWN。
- Budget 顺序：资源后 reserve、Started 前复验、真实调用后 commit。
- Timeout：Tool default、Invocation requested、Run remaining 的最小值。
- Sync Timeout：协作取消 + grace；未停止则 Unknown + Detached，并延迟释放全部三类 Permit。
- Cancellation：Run reason first-wins；Tool 不覆盖。
- Runtime Event：Started/Completed 成对，只有安全元数据。
- Complex Tool：直接强类型 execute，不绕 Legacy Wrapper。
- Read-only Tool：`get_system_status` 经 LegacyStringToolAdapter。
- AgentRouter：已迁移 Tool 走 Service，未迁移 Tool 保留 direct func。
- 已迁移：`complex_workflow_simulator`、`get_system_status`。
- 未迁移：`list_files`、`analyze_excel`。
- Tool Registry：没有新增。
- Tool 修改 RunStatus：没有。
- Retry Owner：既有 RetryExecutor。
- Worker Tracker：应用级安全快照与 `wait_until_idle(timeout)`；不等同于 RunRegistry。
- Output Limit：text/plain 做 UTF-8 安全截断；JSON 超限失败；binary/unknown 拒绝；完整 Digest、默认隐藏 content。
- 测试结果：关键组合 101；Runtime unittest 226；全仓 pytest 402 + 42 subtests。
- 测试环境：`pytest-asyncio 1.4.0`；uv 使用项目内 `.uv-cache`，不再依赖
  Windows 用户缓存目录初始化。
- Bad Case：十二项已按类型、触发、根因、修复、测试和状态记录。
- 人工确认：Legacy 后续迁移策略、多 Worker 资源一致性、截断还是硬失败。
- 后续建议：后续日程可增加跨进程 Lease、外部 Tool 适配和按 Tool 输出策略；本次未实施第 18/19 天内容。
