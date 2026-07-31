# 第 24 天第二轮 B2b：Tool Post-commit / Completion Publication

## 1. 本轮目标

本轮只审计并实现 Tool post-commit 与 completion publication 边界。没有接入通用
EventJournal、RuntimeEventChannel、Snapshot、Recovery、Observability、Trace、Shutdown
Fault Point，也没有增加生产开关、自动 Retry、自动 Compensation 或自动 Replay。

## 2. Side-effect Commit 权威点审计

审计结论如下：

- 外部副作用发生在 Adapter/Tool 内部，不发生在 Generic Tool Runtime 内部。
- `ComplexWorkflowSimulationTool` 通过 `WorkflowStateStore` 写入状态；Runtime 只向
  `ComplexWorkflowToolAdapter` 注入 `context.before_side_effect`。
- `before_side_effect()` 的语义是提交前检查并将 Tracker 从 `NOT_STARTED` 推进到
  `STARTED`，不是提交成功通知。
- 现有 `ToolAdapterContext` 没有 `after_side_effect`、`commit_side_effect` 或等价 callback。
- Runtime 只能在 Adapter 正常 return 后，从权威 `ToolAdapterResponse.side_effect_state`
  得知 `COMMITTED`；Adapter 抛出 `ToolAdapterInvocationError` 时也可通过
  `side_effect_state_authoritative=true` 提供权威终态。
- `AttemptSideEffectTracker.COMMITTED` 可由 Runtime 在解析权威 response/error 后写入；
  非权威 observation 也可推进明确状态，但本轮精确替代点只接受最终 Tracker 为
  `COMMITTED` 的正常 response 路径。
- Adapter response 的 `COMMITTED` 表示 Adapter 明确确认已提交，不是“可能提交”；
  无权威事实的 `STARTED` 会保守收口为 `UNKNOWN`。
- 可以设计可选 callback，但当前真实 Adapter 和 `WorkflowStateStore` 合同没有该通知，
  本轮没有为了测试窗口修改业务语义。
- 不调用 callback 的 Adapter 保持原行为：只在 return/error 后由 Runtime 解析权威状态。

## 3. Fault Point Support Decision

选择方案 B。

`TOOL_AFTER_SIDE_EFFECT_COMMIT` 保持 contract-only。Rule 可以构造且仍要求
`dangerous_window=true`，但真实 Runtime 不调用它，测试证明 `match_count=0`、
`hit_count=0`。

新增准确替代点：

```text
TOOL_AFTER_AUTHORITATIVE_SIDE_EFFECT_RESOLUTION
```

它同样要求 `dangerous_window=true`，准确顺序是：

```text
Adapter 正常 return
→ Runtime 解析权威 ToolAdapterResponse
→ Tracker == COMMITTED
→ TOOL_AFTER_AUTHORITATIVE_SIDE_EFFECT_RESOLUTION
```

它不是外部 Commit 瞬间。

## 4. TOOL_AFTER_SIDE_EFFECT_COMMIT

该点没有接入执行路径。Enum 继续保留合同能力，但没有用
`TOOL_AFTER_PROVIDER_RETURN`、Tracker resolution 或 Tool 名称猜测来冒充精确窗口。

## 5. Authoritative Resolution Alternative

替代点由 `ToolAttemptExecutor` 在正常 Adapter return 且权威状态已写入 Tracker 后调用。
只有 `tracker.state is COMMITTED` 才进入；`NOT_STARTED`、`STARTED`、`UNKNOWN`、
`COMPENSATED` 不进入。

命中抛错时沿既有 post-provider 映射收口为
`POST_COMMIT_RESPONSE_FAILURE / TOOL_POST_PROVIDER_FAILURE`。测试 Fake 的事实为：

```text
provider_entered_count = 1
external_effect_applied_count = 1
provider_returned_count = 1
side_effect_state = COMMITTED
```

`provider_returned_count=1` 是替代点与精确 after-commit callback 的关键差异。

## 6. Post-commit Mapping

- `COMMITTED` 不回退到 `UNKNOWN` 或 `NOT_STARTED`。
- Non-idempotent 为 `UNSAFE`，不重跑。
- Stable key + replay support 是否可由后续调用重放，仍由既有 policy 决定。
- `UNKNOWN` 继续是 `OUTCOME_UNKNOWN`，本轮不虚构提交事实。
- Fault Controller 不推进 Tracker，也不调用 Adapter compensation。

## 7. Completion Evidence

复用既有 frozen `ToolCompletedPayload` 作为唯一 Completion Evidence 格式，没有建立第二套
payload。Owner 是 `ToolAttemptExecutor._emit_completed()`。

Evidence 在 fault seam 前构造，并由同一个 frozen object 同时提供给：

- 本地 `ToolExecutionResult.completion_evidence`；
- 本地 `ToolExecutionError.completion_evidence`；
- `TOOL_COMPLETED` event payload。

字段包括 invocation/attempt digest、provider started、side-effect kind/state、idempotency
kind/key digest、replay support、compensation、retry disposition、outcome、worker lifecycle、
safe error code，以及新增的 `result_present`、`result_digest`。它不保存 argument、output、
resource key、原始异常或 Fault Rule ID。

## 8. TOOL_BEFORE_COMPLETION_EVENT

真实顺序为：

```text
Tool result/error 已归一化
→ side-effect/retry/worker 事实已确定
→ frozen ToolCompletedPayload 已构造并挂到本地 result/error
→ TOOL_BEFORE_COMPLETION_EVENT
→ event_emitter.emit(TOOL_COMPLETED)
```

该点要求 `dangerous_window=true`。没有 event emitter 时不存在 publication 边界，因此不会
执行该点，但本地 Completion Evidence 仍会冻结。

## 9. Completion Publication Failure

Fault seam 或实际 emitter 在冻结后、发布前失败时，Attempt Owner 返回固定安全错误：

```text
category = INTERNAL
phase = EVENT
safe_error_code = TOOL_COMPLETION_PUBLICATION_FAILED
retry_disposition = UNSAFE
```

不发布替代 terminal，不第二次调用 `_emit_completed()`，不重新进入 Tool Retry，不重跑
Adapter，不补偿。原始 `InjectedFaultError`、Rule ID 和原始 emitter 异常不进入错误或 Wire。

命中测试事件计数为：

```text
TOOL_STARTED = 1
TOOL_COMPLETED = 0
```

## 10. Error Priority

固定优先级为：

1. 已冻结的业务 side-effect/outcome 事实保留在 Completion Evidence；
2. 对调用方返回 `TOOL_COMPLETION_PUBLICATION_FAILED` 基础设施错误；
3. 原 Provider error 只以冻结前已归一化的安全 code/classification 保留，不保存原异常。

回归测试覆盖“业务先失败、publication 再失败”：最终 code 是 publication failure，Evidence
仍保留原 `TOOL_BUSINESS_FAILURE / TRANSIENT / COMMITTED`，敏感原始 message 不泄漏。

## 11. Retry / Idempotency

- Publication failure 固定 `UNSAFE`，所以 read-only、idempotent、non-idempotent 均不重跑。
- Retry budget 不增加；命中次数不是 Runtime retry。
- Non-idempotent `COMMITTED` 保持 `UNSAFE`。
- Stable key 只作为 digest evidence，不触发本轮 Replay。
- Post-authoritative-resolution fault 仍使用原 post-commit policy；本轮没有改变既有矩阵。

## 12. Budget / Permit / Lease

Provider 已开始时，Tool budget 在 `_invoke_adapter()` 的 `finally` 中提交。Publication fault
发生得更晚，因此测试结果固定为 `tool_calls=1, retries=0`，active reservation 为零。

Attempt 外层 `finally` 继续释放 Lease/Permit 并注销同步 Worker。测试结束
`active_worker_count=0`、`tool_attempts_active=0`，没有 Detached Worker 假归零。

## 13. Compensation

本轮 `automatic_compensation=false`。Post-commit fault 和 completion publication failure 都不
调用 compensation；测试 Fake 的 `compensation_called_count=0`。

## 14. Event / Trace

- 替代 post-commit fault：`TOOL_STARTED=1`、`TOOL_COMPLETED=1`，terminal 保留
  `COMMITTED`。
- Completion publication fault：`TOOL_STARTED=1`、`TOOL_COMPLETED=0`，不补造 terminal。
- Fault Controller 不写 Journal/Event/Trace。
- Attempt 与 Invocation span 均正常结束；测试后 `active_span_count=0`。

## 15. Recovery Boundary

Publication failure 为第三轮保留真实输入：Journal/Event 侧可看到 Started 而没有 Completed，
本地 Error 的 frozen Evidence 仍保存真实 side-effect state，业务调用次数保持一。

本轮不运行或修改 Recovery Validator，不虚构最终判定。后续应由真实 Journal/Recovery Fault
Matrix 在 `REQUIRES_RECONCILIATION` 与 `INSUFFICIENT_EVIDENCE` 之间决定。

## 16. Cancellation

替代 post-commit point 覆盖 Delay、Blocker、Run Cancellation、Attempt Deadline 和 Scope
close。Cancellation/Deadline 发生时已提交事实保持 `COMMITTED`；budget 已提交、无第二次
Provider 调用、无 compensation，Lease/Worker 最终清理。Scope close 只释放测试 blocker，
不回滚业务状态。

## 17. Disabled Parity

No Controller 与 Disabled Controller 保持原结果、调用次数、状态、预算、事件、Trace 和资源
收口。Disabled dangerous rule 不进入 evaluate，`match_count=0`、`hit_count=0`。

## 18. Isolation

- Controller 仍按请求显式传递，不挂到共享 `ToolExecutionService`。
- Completion fault 通过 invocation digest 只命中目标 invocation；同名 Tool 的第二次调用正常。
- Run A 的 post-commit fault 不影响 Run B。
- Controller close 不修改已经冻结并由本地 Error 引用的 Evidence。
- Resource Lease 和 rule counter 不跨 Controller/Run 串联。

## 19. Security

Fault Match Context 只有 run/invocation digest、attempt number、component 和固定 phase token。
Completion Evidence 只保存 digest、Enum/token、bool 与计数，不保存 Tool argument/output、原始
idempotency/resource key、原始异常或 Rule ID。安全测试检查了附件列出的敏感 marker，事件、
Evidence 和最终错误中均无正文泄漏。

## 20. Runtime 真实接入

真实修改点：

- `core/runtime/fault_injection_contract.py`：新增替代 Fault Point 并标记 dangerous；
- `core/runtime/tool_execution.py`：权威解析后替代 seam、evidence-first completion seam、固定
  publication error 与资源收口；
- `core/runtime/events.py`：Completion Evidence 增加 `result_present/result_digest` 且保持
  Journal allowlist/validation；
- `core/runtime/tool_contract.py`：本地 result/error 引用 frozen Completion Evidence。

## 21. Legacy Boundary

`LegacyStringToolAdapter` 是只读 Adapter，不调用 `before_side_effect()`，也没有 after-commit
callback。它不会进入 authoritative-committed 替代点。它只有在真实 completion event
publication 存在时才会进入 `TOOL_BEFORE_COMPLETION_EVENT`，且即使命中也不会重跑。

## 22. Bad Case

这里的“真实发现”仅表示本轮在仓库代码/合同审计中真实观察到，不表示真实生产事故。

### Bad Case 1：把 Runtime 解析 COMMITTED 冒充外部 Commit 瞬间

- 类型：真实发现
- 触发条件：Adapter return 后，Runtime 执行 `resolve_authoritative(COMMITTED)`。
- 故障表现：若将该行命名为 after-commit，会错误宣称 Runtime 看到了外部提交瞬间。
- 根因分析：外部写入在 Adapter/Tool 内部，Runtime 只在 response 到达后观察到终态。
- 修复方案：新增 `TOOL_AFTER_AUTHORITATIVE_SIDE_EFFECT_RESOLUTION`，名称与时序明确限定。
- 回归测试：断言替代点命中时 `provider_returned_count=1`。
- 对应知识点：事实 Owner、观察时刻与发生时刻、命名真实性。
- 面试表达：我会区分“提交发生”与“Runtime 得知已提交”，避免用近似时刻伪造精确语义。
- 当前状态：已修复；替代点文档和测试均声明不是外部 Commit 瞬间。

### Bad Case 2：无权威 Hook 却强行接入 TOOL_AFTER_SIDE_EFFECT_COMMIT

- 类型：真实发现
- 触发条件：Enum 已存在，但 Adapter context 只有 `before_side_effect()`。
- 故障表现：若仅凭 Enum 接线，会在错误时刻注入 fault。
- 根因分析：现有 Adapter、complex workflow 和 state store 均无提交成功 callback。
- 修复方案：精确点保持 contract-only，不新增会改变 Adapter 业务语义的伪 callback。
- 回归测试：Rule 可构造，但真实执行后 `match_count=0`、`hit_count=0`。
- 对应知识点：能力合同不等于 Runtime 支持、authoritative hook。
- 面试表达：枚举存在只说明协议预留，必须找到真实 Owner callback 才能宣称支持。
- 当前状态：已防护；另有独立 support test。

### Bad Case 3：Post-commit Fault 把 COMMITTED 重置为 UNKNOWN

- 类型：假设构造
- 触发条件：post-commit fault 被通用异常 catch，catch 无条件重置 side-effect state。
- 故障表现：Recovery evidence 丢失确定提交事实，可能做出错误重放决定。
- 根因分析：把异常不确定性错误覆盖到已经权威确定的业务事实。
- 修复方案：`mark_unknown_if_started()` 只转换 `STARTED`；`COMMITTED` 原样保留。
- 回归测试：替代点 raise/delay/block/deadline 测试均断言 `COMMITTED`。
- 对应知识点：单调状态机、证据优先级、保守但不失真。
- 面试表达：保守不等于把所有状态改成 UNKNOWN，权威终态必须单调保留。
- 当前状态：未在生产中发现；已由状态机与回归测试防护。

### Bad Case 4：Cancellation 覆盖已提交事实

- 类型：假设构造
- 触发条件：post-commit blocker 等待期间 Run 被取消。
- 故障表现：取消结果错误显示 `NOT_STARTED/UNKNOWN`，掩盖外部效果已经发生。
- 根因分析：控制流终止被误当成业务事实回滚。
- 修复方案：Cancellation 只终止等待和输出，不覆盖 Tracker 的 `COMMITTED`。
- 回归测试：blocker + Run Cancellation 断言 effect=1、event evidence=`COMMITTED`。
- 对应知识点：Cancellation 与业务原子性的正交关系。
- 面试表达：取消只能改变后续控制流，不能改写取消前已发生的权威副作用。
- 当前状态：未在生产中发现；已覆盖。

### Bad Case 5：Completion Publication Failure 重新执行 Tool

- 类型：假设构造
- 触发条件：`TOOL_COMPLETED` 缺失被当成 Tool 没执行。
- 故障表现：业务副作用重复发生。
- 根因分析：混淆业务完成与完成事件发布。
- 修复方案：publication failure 在 Attempt 内直接返回固定 infrastructure error。
- 回归测试：read-only/non-idempotent 均断言 provider call=1、effect 最大为 1。
- 对应知识点：transactional outbox gap、业务事实与发布事实分离。
- 面试表达：完成事件丢失不等于业务没执行，不能通过重跑来补事件。
- 当前状态：未在生产中发现；已防护。

### Bad Case 6：Completion Publication Failure 进入 Retry

- 类型：假设构造
- 触发条件：publication error 被标记为 transient provider failure。
- 故障表现：RetryExecutor 再次调用 Adapter，retry budget 增加。
- 根因分析：错误分类和 retry disposition 未区分 EVENT 与 INVOCATION。
- 修复方案：固定 `INTERNAL/EVENT/UNSAFE`，在 Attempt 内收口。
- 回归测试：断言 `tool_calls=1`、`retries=0`，read-only 同样不重跑。
- 对应知识点：错误域、Retry safety gate。
- 面试表达：Retry 由业务幂等性和失败阶段共同决定，事件发布失败不能沿 Provider retry 路径走。
- 当前状态：未在生产中发现；已防护。

### Bad Case 7：Completion Publication Failure 自动 Compensation

- 类型：假设构造
- 触发条件：基础设施发布失败触发业务补偿器。
- 故障表现：已成功业务被擅自回滚，且可能产生第二次副作用。
- 根因分析：Fault Controller 越权成为 compensation owner。
- 修复方案：本轮固定 `automatic_compensation=false`，只保留 Adapter 权威状态。
- 回归测试：所有 fault 路径断言 `compensation_called_count=0`。
- 对应知识点：补偿 Owner、Saga 边界。
- 面试表达：观测或发布失败不能自动解释为需要业务补偿。
- 当前状态：未在生产中发现；已防护。

### Bad Case 8：为事件配对补发第二个 Terminal

- 类型：假设构造
- 触发条件：旧断言要求 Started/Completed 必须成对。
- 故障表现：系统伪造 terminal，掩盖真实 publication gap，或产生两个 terminal。
- 根因分析：把观测完整性置于事实真实性之上。
- 修复方案：命中 completion seam 后不再调用 emitter，也不生成替代事件。
- 回归测试：断言 `TOOL_STARTED=1`、`TOOL_COMPLETED=0`。
- 对应知识点：事件序列真实性、reconciliation input。
- 面试表达：不完整但真实的日志优于完整但伪造的日志。
- 当前状态：未在生产中发现；已防护。

### Bad Case 9：Publication Failure 映射成 Provider transient

- 类型：假设构造
- 触发条件：沿用 post-provider 的 `TRANSIENT_PROVIDER_FAILURE` 分类。
- 故障表现：Retry policy 认为 Provider 可重试。
- 根因分析：publication owner 与 provider owner 混淆。
- 修复方案：最终错误固定 `INTERNAL`、phase=`EVENT`、disposition=`UNSAFE`。
- 回归测试：断言 category/phase/code/disposition 四元组。
- 对应知识点：failure domain、错误归一化。
- 面试表达：同样发生在 Provider 返回之后，post-provider 处理失败与 event publication 失败仍是两个错误域。
- 当前状态：未在生产中发现；已防护。

### Bad Case 10：Frozen Evidence 与 Event Payload 使用不同状态源

- 类型：真实发现
- 触发条件：修改前 `_emit_completed()` 临时构造 event payload，但本地 result/error 不引用该对象。
- 故障表现：后续改动可能让本地错误与事件各自重新读取可变状态而漂移。
- 根因分析：缺少显式 freeze-and-share 边界。
- 修复方案：先构造 frozen `ToolCompletedPayload`，再用同一对象 enrich result/error 并发布。
- 回归测试：断言 `result.completion_evidence is completed_event.payload`。
- 对应知识点：single source of truth、不可变快照。
- 面试表达：先冻结一次，再让本地返回值与事件共享同一事实对象，可以消除 TOCTOU 漂移。
- 当前状态：已修复。

### Bad Case 11：Rule ID 进入 Completion Evidence

- 类型：假设构造
- 触发条件：为排障把 fault decision 整体挂到 error/event。
- 故障表现：测试控制信息泄漏到 Journal 或 Wire。
- 根因分析：Recorder 与业务 evidence 边界不清。
- 修复方案：Evidence builder 不接收 controller decision；Rule ID 只进入 Fault Recorder。
- 回归测试：安全序列化中断言不存在 `tool-fault`。
- 对应知识点：数据最小化、控制面与数据面隔离。
- 面试表达：故障规则属于测试控制面，不属于业务完成证据。
- 当前状态：未在生产中发现；已防护。

### Bad Case 12：Tool Output 进入 infrastructure error

- 类型：假设构造
- 触发条件：publication failure 直接包装 result 或原始异常文本。
- 故障表现：Tool output/argument 通过错误、日志或 Wire 泄漏。
- 根因分析：错误传播复用了含正文对象。
- 修复方案：最终错误只引用安全 frozen evidence，结果只保留 SHA-256 digest。
- 回归测试：敏感 marker 不出现在 event/evidence/error repr。
- 对应知识点：safe error envelope、内容与摘要分离。
- 面试表达：基础设施错误只需要固定 code 和安全证据，不需要携带业务正文。
- 当前状态：未在生产中发现；已防护。

### Bad Case 13：Span/Lease/Budget 因 Event 失败未清理

- 类型：假设构造
- 触发条件：completion seam 抛错绕过 Attempt `finally` 或 span end。
- 故障表现：资源许可泄漏、预算 reservation 悬挂、activity/span 不归零。
- 根因分析：发布逻辑成为新的资源 owner 或提前逃逸。
- 修复方案：publication error 转成普通 Attempt failure，沿原 finally 和 span owner 收口。
- 回归测试：断言 reservation=0、worker=0、activity=0、active span=0。
- 对应知识点：结构化并发、资源 ownership、finally invariants。
- 面试表达：fault seam 只能改变结果，不能绕开既有资源 owner 的 finally。
- 当前状态：未在生产中发现；已防护。

### Bad Case 14：Disabled dangerous Rule 消耗 counter

- 类型：假设构造
- 触发条件：Disabled controller 仍调用 evaluate。
- 故障表现：`match_count` 增加，破坏 parity 和确定性。
- 根因分析：disabled fast path 不完整。
- 修复方案：seam 在 controller 未启用时直接返回。
- 回归测试：正常完成且 `match_count=0`、`hit_count=0`。
- 对应知识点：feature-off parity、deterministic test control。
- 面试表达：禁用态不仅要“不注错”，还要不产生任何 counter 或 timing 副作用。
- 当前状态：未在生产中发现；已防护。

### Bad Case 15：Contract-only Fault Point 被错误宣称已覆盖

- 类型：真实发现
- 触发条件：测试目录只验证 Enum/Rule 可构造，或用邻近 point 代替精确 point。
- 故障表现：文档声称覆盖真实 after-commit window，但运行时从未经过该窗口。
- 根因分析：把 contract coverage、support coverage 和 fault hit coverage 混为一谈。
- 修复方案：增加独立 support test，对比精确点零 match 与替代点真实 match。
- 回归测试：`test_tool_fault_point_support.py` 明确断言 0 与 1。
- 对应知识点：测试证据层级、negative capability test。
- 面试表达：对不支持的精确能力，最有价值的测试是证明它不会被误调用。
- 当前状态：已澄清并由测试锁定。

## 23. 测试结果

新增测试：

- `tests/test_tool_post_commit_fault_injection.py`
- `tests/test_tool_completion_publication_fault.py`
- `tests/test_tool_completion_evidence.py`
- `tests/test_tool_post_commit_isolation.py`
- `tests/test_tool_fault_point_support.py`

执行结果：

```text
目标 Runtime pytest：169 passed in 2.88s
全仓 pytest：830 passed, 42 subtests passed in 9.47s
compileall：通过
uv lock --check：通过（Resolved 157 packages）
git diff --check：通过（仅报告仓库既有 LF/CRLF 转换提示）
```

## 24. 未完成事项

- 未提供真实 `TOOL_AFTER_SIDE_EFFECT_COMMIT` 支持；缺少 Adapter 权威 callback，这是有意的
  真实性边界，不是遗漏。
- 未接入 Journal/Channel/Snapshot/Recovery/Trace/Shutdown fault。
- 未自动运行 Recovery Validator，未决定最终 reconciliation enum。
- 未实现真实文件、网络、数据库或外部 Tool fault。

## 25. 第三轮接入点

第三轮可消费以下真实输入：Started event 存在、Completed event 缺失、本地 frozen Completion
Evidence 存在且 side-effect state 保真、provider/business 只执行一次。届时由 Journal tail 与
Recovery Validator 的真实矩阵决定 reconciliation 状态，不应由本轮 Fault Controller 决定。

## 26. 需要带回 ChatGPT 审查的信息

- Side-effect commit owner：Adapter/Tool；complex workflow 的实际状态写入由
  `WorkflowStateStore` 完成。
- Exact commit hook：不存在。
- After-commit point support：`TOOL_AFTER_SIDE_EFFECT_COMMIT` 为 contract-only。
- Alternative point：`TOOL_AFTER_AUTHORITATIVE_SIDE_EFFECT_RESOLUTION`。
- Commit timing claim：仅声称 Adapter return 后 Runtime 已权威解析 `COMMITTED`。
- Post-commit provider calls：替代点命中时 1。
- Post-commit external effects：1。
- Post-commit state：`COMMITTED`。
- Post-commit retry：non-idempotent 为 0；既有 stable-key replay matrix 未改变。
- Post-commit compensation：0。
- Post-commit budget：`tool_calls=1`，reservation 清零。
- Completion evidence owner：`ToolAttemptExecutor`。
- Evidence frozen before event：是，同一 frozen payload 供本地 result/error 与 event 使用。
- Completion fault location：Evidence freeze 后、`emit(TOOL_COMPLETED)` 前。
- Completion event count：0。
- Started event count：1。
- Business rerun count：0；总 Provider 调用次数为 1。
- Retry count：0。
- Compensation count：0。
- Publication error：`TOOL_COMPLETION_PUBLICATION_FAILED`。
- Side-effect state preserved：是，`COMMITTED` 保留。
- Tool spans：全部结束。
- Permit/lease cleanup：完成；无 active worker/reservation/activity。
- Recovery boundary：留给第三轮决定 `REQUIRES_RECONCILIATION` 或
  `INSUFFICIENT_EVIDENCE`。
- Disabled parity：结果一致，counter 为 0/0。
- Run isolation：通过。
- Invocation isolation：通过，同名 Tool 不合并。
- Fault rule data in event/evidence/wire：无；仅 Fault Recorder 保存安全 rule identity。
- 新增测试：5 个文件，覆盖 support、post-commit、publication、evidence、isolation。
- 需要人工确认的问题：若未来真实 Adapter 愿意提供“外部副作用已完成且尚未 return”的权威
  callback，应另行评审其业务原子性和线程/取消语义后再启用精确 point；本轮不需要人工阻塞。
