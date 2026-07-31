# 第 24 天第二轮 B1：Tool Pre-call Fault Injection

## 1. 本轮目标

本轮只把 `TOOL_BEFORE_INVOCATION`、`TOOL_BEFORE_ATTEMPT`、`TOOL_BEFORE_PROVIDER_CALL` 接入真实 Tool Execution 链，并保持 Retry、幂等、Resource Lease、Budget、Event、Trace、Side-effect 与 Worker owner 不变。未接入任何 provider-return、side-effect commit 或 completion-event 危险窗口，也未增加生产 Settings/API/Header。

## 2. 修改前 Tool 调用链

```text
CoordinatedRunScope
-> CoordinatedSingleAgentDriver
-> AgentRouter._prepare_answer_messages()
-> ToolAdapter.build_invocation()
-> ToolExecutionService.execute_sync()/execute()
-> RetryExecutor.execute_async()
-> ToolAttemptExecutor.execute()
-> ToolConcurrencyController.acquire()
-> BudgetLedger.reserve()
-> TOOL_STARTED
-> ToolAttemptExecutor._invoke_adapter()
-> ToolAdapter.invoke_once()                 # 唯一 Provider 调用
-> Budget commit / result mapping / TOOL_COMPLETED
```

迁移 Tool 走上述链；未迁移 Legacy Tool 仍走原直接调用分支，本轮未改变它。

## 3. Tool Invocation / Attempt Owner

- Invocation identity 由 `ToolInvocation.create()` 建立；`ToolExecutionService` 是 invocation、Retry 与 invocation span owner。
- `RetryExecutor` 是唯一 Retry policy/loop owner。
- `ToolAttemptExecutor` 是 attempt identity、attempt span、Permit/Lease、Budget reservation、Side-effect tracker、started/completed event 与清理 owner。
- Provider 唯一调用位置是 `ToolAttemptExecutor._invoke_adapter()` 中的 `adapter.invoke_once()`；同步 Adapter 的 detached worker 也只由该 executor 与 `ToolConcurrencyController` 管理。
- Permit 与 Resource Lease 由 `ToolConcurrencyController.acquire()` 一次取得，并在 attempt `finally` 或既有 detached-worker 回收路径释放。
- `AttemptSideEffectTracker` 在真实 attempt 建立时创建；`provider_started` 只在 `_invoke_adapter()` 前的最后安全点写为 `True`；Side-effect 状态只由 `context.before_side_effect()` 与 Adapter 权威结果推进。
- 幂等 key/record 的通用身份属于 `ToolInvocation`/Spec；真实 complex-workflow record/replay 属于 Adapter 的 `WorkflowStateStore`，本轮未建立第二个 store。

## 4. Controller 传递

显式 request-scoped 链为：

```text
CoordinatedRuntimeFactory.create_run_scope(fault_controller=...)
-> CoordinatedRunScope
-> CoordinatedSingleAgentDriver
-> AgentRouter._prepare_answer_messages(fault_controller=...)
-> ToolExecutionService.execute_sync(..., fault_controller=...)
-> ToolExecutionService / ToolAttemptExecutor
```

参数默认 `None`。`ToolExecutionService`、Tool Registry、Spec、Invocation、RunContext、AgentState、Event、Journal 与 Wire 均不缓存 controller。测试验证 application-scoped service 可连续服务不同 controller/run，且没有 `fault_controller` 实例字段。

## 5. TOOL_BEFORE_INVOCATION

位置在 Adapter 已解析静态 Spec 并完成 contract validation 后、进入 `RetryExecutor` 前。Invocation identity 已由上游真实 owner 建立，但 attempt 数为零；不建立 attempt span/tracker，不取 Permit/Lease，不 reserve Budget，不发 `TOOL_STARTED`，不创建 Worker。命中返回现有 `ToolExecutionError`，`attempt_id=None`。

## 6. TOOL_BEFORE_ATTEMPT

位置在 `RetryExecutor` 的真实 attempt callback 内、调用 `ToolAttemptExecutor.execute()` 前。它使用真实 retry index 做 `attempt_number` 匹配，但不伪造 attempt identity、span、Permit、Lease、Budget 或 Event。Transient/timeout 只产生一次 typed attempt failure，是否建立下一 attempt 仍由现有 `RetryExecutor` 与 `retry_disposition_for()` 决定。

## 7. TOOL_BEFORE_PROVIDER_CALL

真实顺序为：attempt identity/tracker/span -> Permit/Resource Lease -> Budget reservation -> `TOOL_STARTED` -> 本 fault point -> `provider_started=True` -> `_invoke_adapter()`。因此命中时 Adapter 与外部副作用均为零，`provider_started=false`；已取得的 reservation 与 Lease 沿原 `finally` 路径 release，Permit 随 Lease 返回。Started event 由原 owner 配对一个安全 `TOOL_COMPLETED` failure event。

## 8. Tool Error Mapping

映射只存在于 Tool seam；Controller 不认识 Tool taxonomy。

| Injected code | Tool category | safe error code | status |
| --- | --- | --- | --- |
| `INJECTED_TRANSIENT_FAILURE` | `TRANSIENT` | `TOOL_INJECTED_TRANSIENT_FAILURE` | `FAILED` |
| `INJECTED_TIMEOUT` | `TIMEOUT` | `TOOL_INJECTED_TIMEOUT` | `TIMED_OUT` |
| `INJECTED_PERMANENT_FAILURE` | `INTERNAL`（现有 non-retryable 类） | `TOOL_INJECTED_PERMANENT_FAILURE` | `FAILED` |

原始 `InjectedFaultError`、rule ID、arguments、resource/idempotency key、Adapter error 与路径均不进入安全错误。Tool taxonomy 没有 rate-limit 类，本轮未接 `INJECTED_RATE_LIMIT` 专用映射。

## 9. Retry Disposition

Transient/timeout 调用现有 `retry_disposition_for()`；permanent 因 category 非可恢复而固定 `UNSAFE`。Controller 不调用 Retry。只读 transient 在 `max_hits=1` 后由原 policy 建立下一 attempt；non-idempotent transient 即使 Provider 未开始也仍为 `UNSAFE`，不会因测试新增 Retry。Controller hit count 与 retry index 分离。

## 10. Idempotency

READ_ONLY、天然 IDEMPOTENT、`IDEMPOTENT_WITH_KEY`、NON_IDEMPOTENT 与 UNKNOWN 继续服从现有枚举和 validation。Provider 前故障不会调用 Adapter，因此不会读取/写入 complex-workflow idempotency record，不会生成 completed result，也不会触发 replay。后续未注入 attempt 仍按原 Adapter/Store 合同工作。缺 key 在 fault seam 前由真实 contract validation 拒绝；replay unsupported 的语义未改变。

## 11. Resource Lease

Invocation/attempt 前故障不获取 Lease。Provider 前故障已经获取 Lease，随后由 attempt `finally` 释放；测试用资源 key 验证 `is_resource_held()==False`，无 stale lock。未移动 seam 到资源获取之前来规避清理证明。

## 12. Budget / Permit / Activity

- Invocation/attempt 前故障无 Tool reservation。
- Provider 前故障的 reservation 被 release，不把未发生的 Provider 调用提交为 `tool_calls`。
- 若只读 transient 随后 Retry 成功，仅真实 Provider attempt 提交一个 `tool_calls`；真实第二 attempt 提交 `retries=1`。
- Permit/Lease 最终归还，`active_reservation_count=0`，Worker snapshot 为零。
- `RuntimeActivityTracker.tool_attempts_active` 仍只由 `ToolAttemptExecutor` 增减，并在 `finally` 归零；没有伪造 detached-worker 归零。

## 13. Side-effect 零提交

安全计数 Adapter 覆盖 `provider_call_count`、`external_side_effect_count`、`side_effect_commit_count`、`compensation_call_count`、`detached_worker_count`。三个 pre-call point 的注入 attempt 均证明五项为零。没有通过测试重置 tracker；Provider 前 tracker 的真实状态始终是 `NOT_STARTED`。

## 14. Event / Trace

Controller 不发布 Event/Span。Invocation 前故障没有 Tool event，仅真实 invocation span 安全结束；attempt 前故障没有伪造 attempt span/event；Provider 前故障已有 `TOOL_STARTED`，原 attempt owner 恰好发布一个 `TOOL_COMPLETED` failure terminal event，payload 为 `provider_started=false`。测试结束 `active_span_count=0`。

## 15. Cancellation / Delay / Block

Tool seam 只允许 `RAISE_TYPED_ERROR`、`DELAY`、`BLOCK_UNTIL_RELEASED`。异步 controller action 由短轮询同时检查真实 run cancellation/deadline；取消时 action task 被取消并回收，`RunCancelledError` 按原合同传播，不映射 permanent、不 Retry。Provider、side effect、Worker 为零，Lease/Permit/Budget 收口；`FaultInjectionScope.aclose()` 仍释放 blocker/sleeper。

## 16. Disabled Parity

No Controller 与 `enabled=false` Controller 比较了 status、output、Provider calls、committed usage 与 active reservation，语义相同。廉价 guard 不构造 match context、不更新 counter。全仓 Event/Trace/E2E 回归同时通过。

## 17. Run / Invocation Isolation

共享同一个 `ToolExecutionService`：Run A 显式 controller 命中，Run B 传 `None` 正常调用；service 不缓存 controller。Invocation rule 只使用 SHA-256 invocation digest 匹配，arguments 不参与选择；同名 Tool 的 Invocation A 命中而 B 不命中。Counter/recorder 属于各 controller，Lease/record 不跨 invocation 串联。

## 18. Dangerous Window Boundary

以下点仍只在 contract 中存在，生产源码没有调用：

```text
TOOL_AFTER_PROVIDER_RETURN
TOOL_BEFORE_SIDE_EFFECT_COMMIT
TOOL_AFTER_SIDE_EFFECT_COMMIT
TOOL_BEFORE_COMPLETION_EVENT
```

回归分别配置这些 rule 并完成正常 Tool 调用，四者 `match_count` 均为零。未使用文件系统、网络、数据库或不可逆副作用验证。

## 19. Security

Match context 仅含固定 component、run/invocation SHA-256 digest 与 attempt number。`TOOL_ARGUMENT_SECRET`、`TOOL_OUTPUT_SECRET`、明文 idempotency/resource key、compensation payload 与原始 Adapter exception 不进入 Decision、Recorder、Runtime Event、Journal、Wire、Trace attribute 或安全错误。Tool 结果/事件继续使用既有安全摘要与 digest。

## 20. Runtime 真实接入

真实 `AgentRouter._prepare_answer_messages()` 已把同一个 request controller 传给 `ToolExecutionService.execute_sync()`；集成测试证明 migrated Adapter 在 Provider 前命中、Provider 为零且不会回退到 Legacy function。`CoordinatedRuntimeFactory` 现有 scope/driver 传递链因此同时覆盖 Model、Retrieval 与 Tool。

## 21. Legacy Boundary

未迁移 Tool 的直接调用分支未接 fault controller，也未改变其 Budget/输出语义。没有第二个全局入口、ContextVar、Registry controller 或生产配置。

## 22. Bad Case

| # | 性质 | Bad case | 防线/证据 |
| --- | --- | --- | --- |
| 1 | 假设构造并阻断 | Controller 自行 Retry | Controller 仅执行一次 action；RetryExecutor 唯一 owner |
| 2 | 假设构造并阻断 | Provider 前故障仍调用 Adapter | 五项零副作用计数 |
| 3 | 假设构造并阻断 | Provider 未开始却标记 `OUTCOME_UNKNOWN` | 固定 `NOT_STARTED`，`provider_started=false` |
| 4 | 假设构造并阻断 | Pre-call fault 触发 compensation | compensation count 为零 |
| 5 | 假设构造并阻断 | Resource Lease 未释放 | resource-held 回归为 false |
| 6 | 假设构造并阻断 | Permit 未归还 | 同一 service 后续调用成功，worker/lease 为零 |
| 7 | 假设构造并阻断 | Budget reservation 泄漏 | `active_reservation_count=0` |
| 8 | 假设构造并阻断 | non-idempotent 被新增 Retry | disposition `UNSAFE`，Provider/committed call 为零 |
| 9 | 假设构造并阻断 | Idempotency preparation 错标 completed | pre-provider 不进入 Adapter/store |
| 10 | 真实机制澄清 | hit count 当 retry count | hit=1；真实第二 attempt 才提交 retries=1 |
| 11 | 假设构造并阻断 | `TOOL_STARTED` 无终止事件 | provider-pre event 成对测试 |
| 12 | 假设构造并阻断 | 无 started 却发布 completed | invocation-pre event 为空 |
| 13 | 假设构造并阻断 | Tool argument 进入 Recorder/error | context 无 arguments；安全序列化无 SECRET |
| 14 | 假设构造并阻断 | Run A controller 污染 B | 共享 service 的 request isolation 测试 |
| 15 | 假设构造并阻断 | 危险 post-commit 点意外调用 | 四个危险 rule match count 为零 |
| 16 | 回归中验证 | Block 取消后仍启动 Provider | 取消传播，Provider 为零且资源收口 |

## 23. 测试结果

用户清单中的 `tests/test_tool_idempotency.py` 不存在；使用真实对应文件 `tests/test_tool_execution_integration.py`、`tests/test_tool_recovery.py` 与 `tests/test_tool_event_evidence.py` 覆盖 Adapter idempotency/replay、恢复证据与 Event 合同。

```text
新增 Tool fault tests:
20 passed in 0.48s

目标 pytest（含上述真实替代文件）:
178 passed in 2.00s

全仓 pytest:
798 passed, 42 subtests passed in 9.24s

uv run python -m compileall -q core tools tests:
passed

uv lock --check:
Resolved 157 packages in 2ms

git diff --check:
passed（仅 LF -> CRLF 工作区提示，无 whitespace error）
```

## 24. 未完成事项

未接 Tool post-provider/post-commit/completion fault point；未接 EventJournal、RuntimeEventChannel、Snapshot/Recovery、Observability/Trace、Shutdown；未增加生产 Settings/API/Header、概率 Chaos、自动 Retry/Compensation/Recovery/Replay；未实现第 25 天内容。

## 25. 第二轮 B2 接入点

B2 必须单独审计 provider return 后至 side-effect commit、commit 后至 completion event 的去重、幂等 record、unknown outcome、compensation、Event/Journal 与 recovery 语义。不得直接复用本轮 pre-call 的 `NOT_STARTED` 映射，也不得在缺少真实可逆 fake 的情况下启用危险点。

## 26. 需要带回 ChatGPT 审查的信息

- Tool permanent 当前安全映射到现有 non-retryable `INTERNAL`，没有扩展 taxonomy；是否未来增加专用 permanent category 应单独决定。
- Tool taxonomy/RetryPolicy 当前没有 rate-limit 专用类别，因此本轮刻意未接 `INJECTED_RATE_LIMIT`。
- Provider 前 seam 位于 Permit/Lease、Budget reservation 与 `TOOL_STARTED` 之后，这是保留真实 attempt 顺序的选择；原清理路径已经被故障回归证明。
- Generic Tool runtime 不拥有 idempotency execution store；当前真实 record/replay owner 是 complex-workflow Adapter 的 `WorkflowStateStore`。本轮没有伪造通用 store。
- Production enablement 仍关闭：只有测试显式创建的 request scope 能携带 controller，没有 API/Header/Settings 入口。
