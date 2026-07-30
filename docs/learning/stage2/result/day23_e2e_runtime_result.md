# 阶段二第 23 天改造结果

## 1. 本日目标与准确范围

第 23 天完成 Runtime 从“能力存在”到“默认生产入口、请求生命周期、关机生命周期和离线 E2E 证据闭环”的迁移。

本日四轮最终覆盖：

- 默认 `/api/chat` 选择 Coordinated Runtime，显式 `legacy` 可回滚；
- application-scoped services、factory、per-run scope 与依赖关闭顺序；
- `OUTPUT_DELTA` 唯一正文、`RUN_COMPLETED` 唯一 Terminal；
- Client Disconnect、cancel-and-drain、admission gate、active run control；
- Coordinated 与 Legacy 同步 worker 的真实 pending/running/detached 生命周期；
- `RuntimeEventChannel` 的单 Consumer ownership；
- 直接调用 FastAPI ASGI callable 的 HTTP streaming/disconnect/send-failure 测试；
- 默认/Legacy/失败/Snapshot/Journal/Shutdown/不变量回归；
- 安全投影扫描和全仓回归。

本日没有改变 Model/Tool/Retrieval 的业务重试、副作用或降级语义，也没有实现第 24 天 Fault Injection。

## 2. 修改前默认入口

第 23 天开始前，Runtime 基础组件、Planner、Scheduler、Model/Tool/Retrieval、Journal、Trace、Snapshot/Recovery 基础能力已经存在，但 `/api/chat` 仍主要由 Legacy `ChatService.stream_chat()` 承担。

迁移前存在三类边界问题：

- 能力已实现不代表默认入口已迁移；
- 请求 task 结束不代表底层同步 worker 已结束；
- transport consumer 退出不代表 drain 可以无条件并发读取同一 Channel。

## 3. Runtime Mode 与默认迁移

`ChatRuntimeMode` 的默认值为 `COORDINATED`。请求入口只读取一次 mode，并进入严格互斥分支：

```text
COORDINATED -> stream_coordinated_agent_text
LEGACY      -> stream_chat
```

未知 mode 启动失败；任何运行时失败都不会触发跨 Runtime fallback。Model Router 内既有的 model retry/fallback 仍属于同一 Runtime 内的 attempt 选择，不是跨 Runtime fallback。

## 4. Application Runtime Assembly

`server.py::lifespan()` 是生产 Composition Root。它构造并持有：

- Journal、Observability、Trace、Snapshot/Recovery capability；
- Model/Tool/Retrieval services；
- 通用 blocking executor；
- 独立 `coordinated_step_executor`；
- 独立 `legacy_step_executor`；
- RunRegistry、AdmissionGate；
- CoordinatedRuntimeFactory；
- GracefulShutdownCoordinator。

`ApplicationRuntimeServices` 不保存当前请求的 RunContext、AgentState、Channel 或业务正文。

## 5. Coordinated Run Scope

`CoordinatedRuntimeFactory.create_run_scope()` 为每个 Coordinated 请求创建一套且仅一套：

- RunContext；
- CancellationSource；
- BudgetLedger；
- AgentState；
- RuntimeActivityTracker；
- RuntimeEventChannel；
- RunEventEmitter 与 sequence owner；
- Scheduler、Executor、Driver、RunCoordinator；
- ActiveRunControlHandle。

`CoordinatedRunScope` 是这些请求级对象的强生命周期 owner。正常结束执行 `close()`；断连先 cancel-and-drain，超时才 force abort。GC 不是资源关闭 owner。

## 6. Dependency Ownership

依赖分为三层：

| 层级 | Owner | 示例 |
|---|---|---|
| application | lifespan / ApplicationRuntimeServices | model client、journal、executor、store |
| request | CoordinatedRunScope 或 Legacy generator frame | context、source、state、channel |
| worker | 独立 executor record / future callback | pending、running、detached completion |

RunRegistry 只持有安全控制 handle，不持有完整 scope、prompt、output、Tool/RAG 数据或路径。

## 7. Streaming Compatibility

现有 wire 继续为 `text/plain`：

```text
普通正文 chunk
[[ORCH]]{safe-json}\n
[runtime-error] FIXED_ERROR_CODE\n
```

这不是标准 SSE 或 WebSocket。`ChatStreamCompatibilityAdapter` 继续适配桌面客户端，未修改请求 Schema 和既有控制行前缀。

新增 `_RequestOwnedStreamingResponse` 只强化请求 body iterator 的 `aclose()` 所有权：正常结束、task cancellation、`BrokenPipeError`、`ConnectionResetError` 均能进入同一清理路径，不改变 wire。

## 8. Output / Terminal Ownership

`OUTPUT_DELTA.text` 是唯一正常正文来源。Transport 不追加 `final_output`，Control、Tool Evidence、RAG 内容和 Terminal payload 不进入正文。

`RunCoordinator` 是 `RUN_COMPLETED` 的唯一创建者。HTTP、Adapter、Journal 和 Shutdown 不补造第二个 Terminal。Terminal 后业务事件与重复 Terminal 由 Adapter fail closed。

## 9. Client Disconnect

每请求只有一个 LocalAgent logical disconnect watcher：

```text
request.is_disconnected()
-> RunRegistry.cancel(run_id, CLIENT_DISCONNECTED)
-> per-run CancellationSource first-wins
```

`CancelledError`、GeneratorExit、BrokenPipe 和 ConnectionReset 是同一请求生命周期的兜底信号，不创建第二套取消源。Watcher 在所有出口 cancel + await。

真实 ASGI harness 直接构造 HTTP scope、`http.request`、可控 `http.disconnect` 和捕获型 `send()`；它不是外部网络或 Uvicorn socket 测试。

## 10. Cancel-and-drain

Coordinated transport 停止读取后执行：

```text
request_cancel
-> transport consumer release
-> drain consumer atomic acquire
-> drain to terminal/closed
-> bounded producer join
-> scope close
```

超出 disconnect grace 时才 force abort。Drain 只丢弃内部生命周期事件，不适配正文，也不继续向客户端发送 SAFE_ERROR。

## 11. Runtime Admission

Shutdown 首先关闭 application admission。Factory 在创建 RunContext 前取得 admission lease，Legacy generator 在创建 run identity 前也检查同一 gate。

状态为：

```text
ACCEPTING -> DRAINING -> CLOSED
```

Shutdown 与新请求竞态时，新请求固定得到 `RUNTIME_SHUTTING_DOWN`，不会创建第二个 Runtime 或部分 Scope。

## 12. RunRegistry Control Handle

生产注册项为 `ActiveRunControlHandle`，只包含：

- run_id 与 runtime_mode；
- started_at；
- CancellationSource；
- force-abort capability；
- 低基数 active-step count callback；
- completion event。

它不包含 prompt、messages、model output、tool arguments/output、RAG chunk、Snapshot payload 或完整 AgentState。

## 13. Graceful Shutdown

`GracefulShutdownCoordinator` 是唯一 application shutdown owner：

1. 关闭 admission；
2. 等待 admission lease settle；
3. 对活跃 run 请求 `SERVER_SHUTDOWN`；
4. 有界等待 run drain；
5. 对剩余 run 执行有界 force abort；
6. 关闭 worker admission；
7. 有界等待真实 worker；
8. flush；
9. 按依赖顺序 close；
10. gate 与 lifecycle 进入 CLOSED。

重复调用返回同一份缓存 report。

## 14. Shutdown Order

集中断言的逻辑顺序为：

```text
admission close
<
run cancellation/drain
<
worker drain
<
observability flush
<
trace flush
<
snapshot close
<
journal close
<
model close
<
executor/store close
```

单组件失败使用固定安全错误码记录，并继续处理后续组件。

## 15. Worker / Detached Worker

`BoundedBlockingExecutor` 同时限制 running 与 pending，submit 时传播 ContextVar，Future done callback 是 permit 与 record 的最终清理点。

`cancel_or_detach()` 的事实语义：

- pending future 成功取消：worker 未运行；
- future 已结束：worker 已终止；
- running future 不能取消：记录 `detached=True`；
- 自然完成后 callback 才将 tracker 归零。

取消 asyncio waiter 不会伪装成 Python thread 已终止。

## 16. Legacy Worker Boundary

Legacy HTTP bridge 不再直接使用 `asyncio.to_thread(next)`。每次 generator advance 由独立 application-level `legacy_step_executor` 执行：

- 非阻塞、有界 admission；
- `LEGACY_STREAM_STEP` 独立 kind；
- pending/running/detached 真实计数；
- ContextVar 传播；
- waiter 取消后 cancel-or-detach；
- worker 完成后安全关闭 generator；
- 不与 `coordinated_step_executor` 混用 worker identity。

如果 Shutdown 时 Legacy worker 仍活跃，Model client close 被标记为 `RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER`，不得伪报已关闭。

Python 不能强杀已运行的 Python/C Extension thread；这里只保证边界、跟踪与安全延迟关闭。

## 17. ASGI E2E

`tests/test_runtime_asgi_e2e.py` 直接调用 `server.app(scope, receive, send)`，覆盖：

- 正常完整请求与 streaming body；
- model/stream 等待期间 `http.disconnect`；
- `BrokenPipeError`；
- `ConnectionResetError`；
- response body task cancellation；
- watcher、stream、producer 的最终 join/close；
- 断连后无 late body、无 SAFE_ERROR body。

`send()` 失败测试真实触发 Starlette `ClientDisconnect`，并验证 request-owned body iterator 已关闭。

## 18. Model E2E

默认 Composition Root 测试注入 Fake Model/Router，不访问网络：

```text
/api/chat
-> Coordinated
-> Planner
-> Step
-> Model adapter
-> OUTPUT_DELTA
-> RUN_COMPLETED
```

验证 Model 一次、正文唯一、Terminal 唯一、Registry/Channel gauge 归零。

## 19. Parallel E2E

既有 ParallelExecutor/RunCoordinator 测试验证：

- 兄弟 Step 受并发上限约束；
- 一个 RunContext、一个 Run root span；
- Step span 为兄弟节点；
- 共享 run_id/trace_id；
- 最终只有一个 RUN_COMPLETED。

本轮未为 parallel step 创建第二套 context、source、channel 或 registry。

## 20. Retry / Fallback E2E

Model invocation 测试继续验证一个 invocation 下多个 sibling attempt。Model fallback 不进入 Legacy，不创建第二个 RunContext，也不重复输出正文。

本轮没有修改 RetryPolicy、CircuitBreaker、Model selection 或 fallback 业务语义。

## 21. Retrieval E2E

既有 Retrieval 测试覆盖 stage span、query rewrite 的 model 父子关系、预算、取消、timeout、degradation 和 detached worker。

RAG chunk、query digest 和 evidence 不进入聊天正文；最终正文仍只来自 `OUTPUT_DELTA`。

## 22. Tool E2E

既有 Tool 测试覆盖 invocation/attempt、只读 transient retry、非幂等不重试、budget、timeout、outcome unknown 和 detached permit。

本轮没有新增 retry、compensation 或 side-effect 语义。Tool arguments、output 与 evidence identity 不进入聊天 wire。

## 23. Budget / Timeout / Cancellation

集中回归验证：

- Budget exhausted 使用固定控制事件和单 Terminal；
- Timeout 使用 canonical reason，停止启动新工作；
- Cancellation reason first-wins；
- 取消后不继续发送正文；
- detached worker 保持真实并最终归零；
- 不跨 Runtime fallback。

## 24. Failure E2E

覆盖 Runtime error、Adapter encoding failure、Journal terminal publication failure：

- 原始异常不进入 wire；
- Adapter failure force abort 当前 scope；
- Journal failure 不重跑已执行业务；
- Journal failure 不补造第二 Terminal；
- terminal publication 异常不再跳过 registry unregister、trace reset 和 cleanup；
- Shutdown 仍可继续。

当 Journal 无法持久化 Terminal 时，不伪造成功 Terminal；Transport 返回固定 `RUNTIME_EXECUTION_FAILED`。

## 25. Snapshot / Recovery Hot-path Boundary

默认：

```text
snapshot_store = None
recovery_validator = None
snapshot_enabled = false
recovery_enabled = false
```

显式启用时 Store 和 Validator application-scoped 创建一次，但普通聊天仍不自动 checkpoint、不自动 recovery/replay，也不读取或写入 Snapshot Store。Store 启动失败 fail fast。

## 26. Runtime Invariants

测试侧新增 immutable `RuntimeInvariantReport`。它只接收已派生计数和事件序列，输出 violations，不保存 live RunContext、AgentState、Channel 或 Registry，因此不是第二套 state owner。

集中断言包括：

- 一个请求一个 runtime selection；
- 一个 Coordinated 请求一个 context/source/channel/sequence owner/registration/root span；
- 一个 RUN_COMPLETED；
- Terminal 后无业务事件；
- 正常结束 active registry/channel/span 为 0；
- 无 pending watcher 或 request-owned producer。

Detached worker 不要求立即为 0，但必须由 executor snapshot 如实记录，并在自然完成后归零。

## 27. Security

本轮扫描生产代码的 wire、日志、repr 和 ShutdownReport 投影，确认新增路径不输出：

```text
prompt / messages / model output
tool arguments / tool output / evidence identity
RAG chunk / memory 正文
API key / provider URL
DB / model / KB path
raw exception / traceback
Snapshot payload / RecoveryAssessment
active run_id list
```

允许输出固定安全错误码与低基数计数。未发现 `[server-error] raw_exception` 拼接；ASGI send failure 后不发送错误正文；ShutdownReport 只有计数与 component safe code。

## 28. Legacy Boundary

显式 `CHAT_RUNTIME_MODE=legacy`：

- 不创建 Coordinated Scope；
- 不访问 Coordinated Factory；
- 不访问 Snapshot/Recovery；
- 使用独立 RunContext、CancellationSource 与 worker executor；
- 不跨 Runtime fallback；
- 保持 Legacy 正常文本和既有 `[[ORCH]]` 行为。

Legacy 的保证是“有界准入、真实跟踪、安全关闭门禁”，不是“同步/C 扩展线程可被强制终止”，也不宣称与 Coordinated event wire 等价。

## 29. Bad Case

### Bad Case 1：Legacy waiter 结束但底层线程仍使用 Model Client

- 类型：真实发现
- 触发条件：Legacy `next(stream)` 在同步 Model 调用中阻塞，同时客户端断连或 response task 取消。
- 故障表现：async waiter 已结束，底层线程继续运行；旧路径无法作为 application worker 被 Shutdown 看见。
- 根因分析：直接 `asyncio.to_thread(next)` 只有 waiter 生命周期，没有项目级 worker record。
- 修复方案：使用独立 `legacy_step_executor`，取消 waiter 时 cancel-or-detach，由 Future done callback 最终清理。
- 回归测试：`test_cancelled_waiter_detaches_until_true_worker_completion`、`test_shutdown_defers_model_close_while_legacy_worker_is_active`。
- 对应知识点：线程不可抢占、detached worker、依赖关闭安全。
- 面试表达：取消等待者不等于取消线程，Model Client 必须等真实 worker 结束后才能关闭。
- 当前状态：已修复；不可强杀线程的限制明确保留。

### Bad Case 2：Transport 与 Drain 同时消费 Channel

- 类型：真实发现
- 触发条件：transport async iterator 尚未释放时，cancel cleanup 直接调用 `drain_to_discard()`。
- 故障表现：两个 consumer 竞争队列，事件可能被错误 owner 取走，顺序与 Terminal 处理不可证明。
- 根因分析：旧实现只限制第二个 transport iterator，drain 绕过 `_consumer_attached`。
- 修复方案：建立 `TRANSPORT / DRAIN / RELEASED / ABORTED` ownership，并要求显式 release 后原子 handoff。
- 回归测试：`test_transport_must_release_before_drain_can_take_ownership`。
- 对应知识点：single consumer、linearization point、最小锁。
- 面试表达：队列线程安全不代表消费协议安全，必须定义谁有权 remove。
- 当前状态：已修复。

### Bad Case 3：Fake disconnect 测试未触发真实 ASGI send failure

- 类型：真实发现
- 触发条件：只直接迭代 endpoint 返回的 body iterator，不通过 ASGI `send()`。
- 故障表现：BrokenPipe/ConnectionReset 下的 Starlette `ClientDisconnect` 与 iterator cleanup 路径未被测试。
- 根因分析：Fake Request 能覆盖 watcher，不能覆盖真实 ASGI response transport。
- 修复方案：新增直接 ASGI harness，构造 scope/receive/send，并注入 send exception。
- 回归测试：`test_real_asgi_send_failure_closes_stream_without_safe_error_body`。
- 对应知识点：ASGI callable、StreamingResponse、transport failure。
- 面试表达：测试业务 generator 不等于测试 ASGI streaming 生命周期。
- 当前状态：已修复；仍不是外部 Uvicorn socket 测试。

### Bad Case 4：Shutdown CLOSED 但 Model Client 被错误标记为已关闭

- 类型：真实发现
- 触发条件：Legacy 同步 worker 未被 application tracker 记录，Shutdown 只看到 request waiter 已退出。
- 故障表现：Model close 与仍运行调用竞态，报告却可能呈现成功。
- 根因分析：Legacy worker 不在 `wait_workers()` 的事实集合中。
- 修复方案：将 legacy executor 纳入 services worker 集合；worker 未 idle 时 Model component 记录 DEFERRED 固定错误码。
- 回归测试：`test_shutdown_defers_model_close_while_legacy_worker_is_active`。
- 对应知识点：truthful shutdown report、deferred close。
- 面试表达：Lifespan 可以 CLOSED，但依赖关闭报告不能说谎。
- 当前状态：已修复。

### Bad Case 5：Detached Worker 被强制断言立即归零

- 类型：假设构造
- 触发条件：测试在 waiter 取消后立即要求 worker gauge 为 0。
- 故障表现：为了满足错误断言而提前删除 record，形成“假绿色”监控。
- 根因分析：混淆 asyncio task completion 与 thread completion。
- 修复方案：detached 期间允许非零，只要求自然完成 callback 后归零。
- 回归测试：`test_cancelled_waiter_detaches_until_true_worker_completion`。
- 对应知识点：eventual cleanup、truthful observability。
- 面试表达：真实的非零比虚假的零更安全。
- 当前状态：防线已验证。

### Bad Case 6：Snapshot enabled 后普通聊天自动 checkpoint

- 类型：假设构造
- 触发条件：把 capability enabled 误解释为每个请求自动保存。
- 故障表现：热路径增加 I/O，并暗中引入恢复语义。
- 根因分析：混淆基础设施可用性与自动策略。
- 修复方案：普通聊天不调用 CheckpointCoordinator 或 RecoveryValidator；策略仍需显式上层触发。
- 回归测试：`test_enabled_snapshot_and_recovery_have_no_automatic_chat_io`。
- 对应知识点：capability vs policy、opt-in persistence。
- 面试表达：Store 可用不等于系统已承诺自动 checkpoint。
- 当前状态：防线已验证。

### Bad Case 7：Journal Failure 触发业务重跑

- 类型：假设构造
- 触发条件：Terminal journal append 失败后重新执行 Model/Tool 以尝试补齐事件。
- 故障表现：正文或副作用重复，破坏 at-most-once 边界。
- 根因分析：把基础设施持久化失败误当业务 retry。
- 修复方案：不重跑业务；安全失败、继续 cleanup，不补造第二 Terminal。
- 回归测试：`test_terminal_journal_failure_never_reruns_business_and_cleans_scope`。
- 对应知识点：failure domain、at-most-once、journal-first。
- 面试表达：Journal 失败可以让本次运行失败，不能授权业务再执行一次。
- 当前状态：防线已验证。

### Bad Case 8：Parallel Step 创建多个 RunContext

- 类型：假设构造
- 触发条件：每个 sibling step 为方便隔离而创建新 RunContext。
- 故障表现：预算、取消、trace、registry 与 terminal 所有权分裂。
- 根因分析：把 Step scope 错当 Run scope。
- 修复方案：兄弟 Step 共享一个 RunContext，只创建不同 step span/claim。
- 回归测试：ParallelExecutor、trace hierarchy 与 centralized invariant tests。
- 对应知识点：structured concurrency、trace hierarchy。
- 面试表达：并行的是步骤，不是复制整个 Runtime。
- 当前状态：防线已验证。

### Bad Case 9：Model Fallback 被误判为跨 Runtime fallback

- 类型：假设构造
- 触发条件：Model Router 在同一 invocation 中选择后备 profile。
- 故障表现：错误地创建 Legacy Runtime 或第二个 RunContext。
- 根因分析：混淆 provider attempt fallback 与 Runtime mode fallback。
- 修复方案：Model attempts 始终留在原 Coordinated scope；Runtime mode 只在请求入口选择一次。
- 回归测试：model invocation/retry tests 与 no-cross-runtime fallback tests。
- 对应知识点：attempt、invocation、run 三层身份。
- 面试表达：模型降级是 invocation 内部策略，不是重新执行整个请求。
- 当前状态：防线已验证。

### Bad Case 10：RuntimeInvariantReport 成为第二套状态 Owner

- 类型：假设构造
- 触发条件：诊断对象保存 live Context、Channel、Registry 或可变 AgentState。
- 故障表现：延长请求数据生命周期，并形成第二套控制面。
- 根因分析：把 assertion helper 做成 runtime registry。
- 修复方案：Report 只保存不可变派生计数和 violations。
- 回归测试：`test_invariant_report_is_derived_and_detects_second_owner`。
- 对应知识点：derived view、least authority。
- 面试表达：诊断报告应观察状态，不应拥有状态。
- 当前状态：防线已验证。

### Bad Case 11：ShutdownReport 泄漏 Run ID

- 类型：假设构造
- 触发条件：为排障把 remaining run_id 列表直接放入 report 或日志。
- 故障表现：扩大请求身份数据的日志驻留面。
- 根因分析：混淆内部控制能力与运维汇总。
- 修复方案：Report 只记录 active/cancelled/forced/remaining/detached 数量和固定组件错误码。
- 回归测试：Shutdown report tests 与本轮安全扫描。
- 对应知识点：data minimization、安全可观测性。
- 面试表达：关机健康度需要计数，不需要暴露请求身份列表。
- 当前状态：防线已验证。

### Bad Case 12：把 Legacy 限制描述成 Coordinated 等价保证

- 类型：假设构造
- 触发条件：文档声称 Legacy 同步/C 扩展线程能像 asyncio task 一样被强制取消。
- 故障表现：运维错误关闭依赖，或等待不存在的强终止保证。
- 根因分析：忽略 Python thread cancellation 限制。
- 修复方案：只承诺 bounded admission、真实 tracker、detach 和 model-close gate。
- 回归测试：Legacy worker lifecycle 与 shutdown deferred-close tests。
- 对应知识点：cooperative cancellation、thread boundary。
- 面试表达：两条 Runtime 可以共享安全合同，但物理终止能力并不相同。
- 当前状态：限制已明确记录。

### Bad Case 13：Channel abort 无法唤醒空队列 Consumer

- 类型：真实发现
- 触发条件：transport 或 drain 正阻塞于空队列 `get()` 时调用 `abort()`。
- 故障表现：consumer 永久等待，request cleanup 泄漏。
- 根因分析：旧 abort 只清空队列并设置 event，consumer 的 `queue.get()` 没有等待该 event。
- 修复方案：abort 清空后放入内部 sentinel，并将 owner 原子切换为 ABORTED。
- 回归测试：`test_abort_wakes_empty_transport_consumer`。
- 对应知识点：waiter wakeup、abort semantics。
- 面试表达：改变状态不等于唤醒正在等待另一个同步原语的任务。
- 当前状态：已修复。

### Bad Case 14：ASGI send failure 后 body iterator 未及时关闭

- 类型：真实发现
- 触发条件：ASGI 2.4 `send()` 在非空 response body 上抛 BrokenPipe/ConnectionReset。
- 故障表现：Starlette 抛 `ClientDisconnect`，但请求 generator 的 finally 未及时运行。
- 根因分析：默认 response transport 没有为本项目建立明确的 body iterator close owner。
- 修复方案：`_RequestOwnedStreamingResponse.stream_response()` 在 finally 中 await `aclose()`。
- 回归测试：ASGI send failure 参数化测试。
- 对应知识点：resource ownership、async iterator finalization。
- 面试表达：发送失败不仅是异常映射问题，也是 body producer 的 join 问题。
- 当前状态：已修复。

### Bad Case 15：Terminal Journal Failure 跳过 Registry 清理

- 类型：真实发现
- 触发条件：已执行完业务，`RUN_COMPLETED` journal append 抛 `JournalError`。
- 故障表现：旧 `RunCoordinator.finally` 在 terminal emit 异常处提前退出，可能跳过 cleanup callback、budget snapshot、registry unregister 和 trace reset。
- 根因分析：终态发布位于 cleanup 序列中但没有隔离其异常。
- 修复方案：记录固定 `RUNTIME_TERMINAL_PUBLICATION_FAILED`，继续全部 cleanup，最后安全抛出。
- 回归测试：`test_terminal_journal_failure_never_reruns_business_and_cleans_scope`。
- 对应知识点：cleanup must continue、error aggregation。
- 面试表达：终态持久化失败很严重，但不能因此跳过更底层的资源收口。
- 当前状态：已修复。

## 30. 测试结果

附件指定 Runtime 目标测试：

```text
197 passed, 16 subtests passed
```

全仓：

```text
698 passed, 42 subtests passed
```

其他校验：

```text
python -m compileall -q core tools tests  通过
uv lock --check                          通过（Resolved 157 packages）
git diff --check                         通过（仅 CRLF 转换提示）
```

新增 5 个测试/辅助文件，连同 `test_event_channel.py` 的 5 个 ownership/abort case，共新增 19 个测试函数、20 个 pytest case。所有 E2E 使用 Fake adapter/driver/store，不访问真实网络、在线模型、Chroma、外部 Tool 或 UI。

## 31. 未完成事项

明确未实现：

- 标准 SSE/WebSocket；
- 自动 Checkpoint；
- 自动 Recovery/Replay/Resume；
- Exactly-once；
- 跨进程 RunRegistry；
- 强制终止 Python/C Extension thread；
- 第 24 天 Fault Injection Framework；
- 生产 Fault Injection API；
- 自动 Compensation；
- 第二套 Runtime 或全局 Current Run；
- 默认聊天能力超出当前 Planner/Driver 实际能力；
- Snapshot Store 自动策略；
- Step Result Rehydration Owner。

真实 ASGI harness 不等于外部 Uvicorn/socket/代理链路测试；生产网络断连时序仍建议在部署环境做人工或集成验证。

## 32. 面试表达

可以这样概括本日工作：

> 我把一个已有模块集合收口成了可证明的 Runtime 生命周期。默认入口只选择一次 Runtime；Coordinated 请求由单一 scope 持有 context、channel、registry 和 producer；正文与 Terminal 都有唯一 owner。断连不是简单 cancel task，而是先释放 transport consumer，再让 drain 原子接管有界队列。对不可抢占的同步线程，我没有假装它能被取消，而是用独立 executor 记录 pending/running/detached，并用真实 worker 状态保护 Model Client 的关闭。最后通过直接 ASGI harness、Journal failure、Snapshot hot-path 和集中不变量测试证明资源最终收口，同时保持 wire 与 Model/Tool/RAG 业务语义不变。

关键取舍：

- 用真实 tracker 接受暂时非零，不用假归零；
- 用 deferred close 接受部分关闭，不冒险提前关共享 client；
- Journal failure 不授权业务重跑；
- diagnostics 只做派生视图，不成为第二 owner；
- E2E 明确是离线 ASGI callable，不冒充真实网络事故。

## 33. 需要带回 ChatGPT 审查的信息

```text
Legacy worker strategy: dedicated application-level bounded executor
Legacy worker tracked: pending/running/detached + Future done cleanup
Legacy model-close safety: active worker => fixed DEFERRED model close
Channel consumer owner: TRANSPORT / DRAIN / RELEASED / ABORTED
Transport-to-drain handoff: explicit iterator aclose then atomic drain acquire
Concurrent consumer: deterministic rejection
ASGI harness: direct FastAPI ASGI scope/receive/send
ASGI normal: passed
ASGI disconnect: passed; no late body
ASGI send failure: BrokenPipe + ConnectionReset passed
Default coordinated E2E: passed
Explicit legacy E2E: passed
Model E2E: passed
Parallel E2E: passed
Retry/fallback E2E: passed; no cross-runtime fallback
Retrieval E2E: passed
Tool E2E: passed
Budget exhausted E2E: passed
Timeout E2E: passed
Cancellation E2E: passed; reason first-wins
Journal failure E2E: passed; no business rerun; cleanup continues
Adapter failure E2E: passed; current scope aborts
Shutdown no-run: passed
Shutdown active-runs: passed
Shutdown mixed modes: covered by shared handle/worker shutdown contracts
Shutdown detached worker: passed
Shutdown order: passed
Deferred close: passed
Snapshot hot path: disabled and explicitly enabled/no-auto-I/O both passed
Recovery hot path: no automatic validation/replay passed
Runtime invariants: centralized immutable derived report passed
Sensitive data scan: passed for wire/log/repr/shutdown projections
Result document: docs/learning/stage2/result/day23_e2e_runtime_result.md
新增测试: 5 files + 5 EventChannel cases
目标 pytest: 197 passed, 16 subtests passed
全仓 pytest: 698 passed, 42 subtests passed
compileall: passed
lock check: passed, Resolved 157 packages
diff check: passed; CRLF conversion warnings only
需要人工确认: 部署环境真实 Uvicorn/socket/proxy 断连时序，以及长期 C Extension worker 的进程退出策略
```
