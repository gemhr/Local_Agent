# 第 23 天第三轮：Cancellation 与 Shutdown

## 1. 本轮目标

本轮只改造 Runtime 的取消、断连与关机生命周期，不改变 Tool Retry、Model Retry/Fallback、Retrieval、Checkpoint、Recovery、Replay、Compensation 或 Wire Protocol 语义。

完成范围：

- `/api/chat` 的 Client Disconnect 主动检测与生成器取消兜底；
- canonical Cancellation Reason 与 first-wins；
- Coordinated cancel-and-drain、超时 force abort；
- Legacy 独立 CancellationSource 与显式 generator close；
- application-scoped Run Admission Gate；
- RunRegistry 安全控制句柄；
- GracefulShutdownCoordinator 与有界组件关闭；
- active/detached Worker 的真实跟踪；
- Journal、Observability、Trace、Snapshot、Model、Executor 的关闭顺序；
- 断连、背压、准入、关机与失败隔离测试。

本地真实依赖版本：

```text
Python 3.12.6
FastAPI 0.139.2
Starlette 1.3.1
```

## 2. 修改前 Disconnect 行为

修改前 `/api/chat` 已在两条模式路径中调用 `request.is_disconnected()`，也显式处理了 `asyncio.CancelledError`、`BrokenPipeError` 和 `ConnectionResetError`，因此不能描述成“完全没有断连处理”。

真实缺口是：

- Legacy 只在每次进入阻塞 `asyncio.to_thread(next)` 之前轮询；Model/Tool 同步调用长时间不返回时，不能及时进入下一次轮询；
- Coordinated 只在收到下一个 Runtime Event 后轮询；长 Model Attempt 期间同样存在检测空窗；
- 没有独立 watcher 与 watcher lifecycle owner；
- Coordinated consumer 退出后直接 abort Channel、cancel producer，没有 drain-to-discard；
- `except Exception` 的安全错误分支没有与“Socket 已断开”状态统一仲裁；
- lifespan 只有 `cancel_all + wait_until_empty + services.close`，没有 admission gate、force abort 和独立 shutdown owner。

## 3. Cancellation Reason

生产 owner 只使用以下固定原因：

```text
CLIENT_DISCONNECTED
SERVER_SHUTDOWN
REQUEST_CANCELLED
REQUEST_DEADLINE_EXCEEDED
STREAM_ENCODING_FAILED
```

`CancellationSource` 继续保持跨线程 first-wins：

- 首次取消写入 reason 与 UTC `cancelled_at`；
- 后续取消返回 `False`；
- Client Disconnect 不覆盖已存在的 Request Cancel；
- Server Shutdown 不覆盖 Client Disconnect；
- 已终态 Run 的 `CoordinatedRunScope.request_cancel()` 返回 `False`；
- 原始异常正文不进入 reason。

旧 `USER_CANCELLED`、`SYSTEM_SHUTDOWN`、`DEADLINE_EXCEEDED` 仅保留给已有测试和旧调用方解析。旧 Context 测试允许存储诊断短字符串，但生产逻辑从不按自由文本分支。

## 4. Disconnect Detection

每个 HTTP 请求只有一个 `_watch_request_disconnect()`：

```text
request.is_disconnected()
        │
        ├─ true -> CLIENT_DISCONNECTED -> RunRegistry control handle
        └─ false -> 等待 stop event 或下一次有界检查
```

同时保留以下独立信号：

- Starlette `StreamingResponse` 对 body iterator task 的取消；
- `asyncio.CancelledError`；
- async generator `aclose()` / `GeneratorExit`；
- `BrokenPipeError`；
- `ConnectionResetError`；
- ASGI/Socket 写失败导致的 task cancellation。

Watcher 由 HTTP stream owner 创建；正常完成、断连、异常和取消路径都执行 `cancel + await`。Watcher 不进入 `ApplicationRuntimeServices`，也不会持有 Request 超过 stream 生命周期。

## 5. Cancel-and-drain

Coordinated transport 提前退出时执行：

```text
request_cancel(reason)
-> transport 停止 yield
-> RuntimeEventChannel.drain_to_discard()
-> producer 发布 CANCELLATION / RUN_COMPLETED
-> producer close channel
-> scope.close()
```

Drain 只消费内部事件：

- 不调用 `ChatStreamCompatibilityAdapter`；
- 不向 Client 输出；
- 不聚合 `OUTPUT_DELTA`；
- 不发布新 Event；
- 不创建第二个 Terminal；
- 不启动 Legacy 或第二个 Coordinated Runtime。

`STREAM_ENCODING_FAILED` 继续保持第二轮已确立的 fail-fast transport contract：立即 force abort 当前 request scope，并输出一个固定安全错误。

## 6. Backpressure

`RuntimeEventChannel` 保持：

- per-run；
- 单 consumer；
- bounded；
- blocking producer；
- Journal-first；
- abort 可解除阻塞 publisher。

新增 `drain_to_discard()` 后，容量为 1、队列已满、transport consumer 已退出时，内部 drain 仍持续读取，producer 可以发布 Cancellation/Terminal 并 close。目标测试验证 Journal 中 `CANCELLATION` 与 `RUN_COMPLETED` 各一次。

## 7. Coordinated Scope Lifecycle

`CoordinatedRunScope` 现在明确提供：

```text
request_cancel(reason)
drain_and_close(timeout)
force_abort(reason)
close()
```

- `request_cancel`：只写 cooperative token，不关闭 Channel/Journal，不等待；
- `drain_and_close`：有界 drain Channel、等待 producer、正常 close；
- `force_abort`：取消 request-owned producer、abort Channel、注销 Registry，不关闭 application dependency；
- `close`：正常收口，幂等，不重新发取消。

清理使用小范围 bounded shielding；`asyncio.CancelledError` 在必要清理完成后重新抛给 ASGI。

## 8. Legacy Disconnect

Legacy 每个请求继续独立创建：

- `RunContext`；
- `CancellationSource`；
- `AgentState`；
- `ActiveRunControlHandle`；
- `AgentLoop`；
- deadline timer。

Client Disconnect 通过 Legacy 自己的 source 传递，然后显式 close generator。Legacy 不创建 Coordinated Scope、不共享 Coordinated token、不改变正常文本协议。

限制：Python 无法抢占已经进入第三方同步函数或 C 扩展的线程。HTTP owner 会取消 asyncio waiter 并停止输出，但底层 Legacy 默认 executor 线程只能依赖原有 cooperative check 或自然返回。本轮没有声称 Legacy 具备与 Coordinated tracked worker 相同的强生命周期保证。

## 9. Runtime Admission Gate

新增 `RuntimeAdmissionGate`：

```text
ACCEPTING -> DRAINING -> CLOSED
```

- `close_admission()` 幂等；
- admission lease 覆盖“检查准入 → 创建/注册”的短窗口；
- Shutdown 先关闭准入，再等待 pending admission settle；
- Factory 在创建 `RunContext` 前取得 lease；
- `/api/chat` 在响应边界提前返回固定 `503 RUNTIME_SHUTTING_DOWN`；
- race 中进入 Factory 的请求仍由 Gate 拒绝；
- Coordinated 和显式 Legacy 都受同一个 Gate 约束。

拒绝路径不创建 Scope、Channel、Registry Handle、Snapshot 或 Model Invocation。

## 10. RunRegistry Control Handle

生产 Registry 保存 `ActiveRunControlHandle`，字段/能力为：

```text
run_id
runtime_mode
started_at
request_cancel(reason)
wait_completed(timeout)
is_completed
force_abort(reason)
```

它不保存 Prompt、AgentState 正文、Tool/RAG 数据、API Key、路径或完整 Scope repr。Force-abort callback 是内部控制回调，不出现在 snapshot。

旧 `RunHandle` 只作为 Day 12～22 单元测试的构造兼容层；生产 Factory 与 Legacy ChatService 都注册 `ActiveRunControlHandle`。

Registry 注册/注销各一次；注销设置 completion event。Observability snapshot 只公开计数，不向普通日志输出 active run_id 列表。

## 11. Graceful Shutdown Coordinator

新增唯一 shutdown owner：

```text
GracefulShutdownCoordinator
ShutdownReport
```

生产顺序：

1. lifespan state 置 `SHUTTING_DOWN`；
2. Admission Gate 置 `DRAINING`；
3. 等待短 admission window settle；
4. snapshot active control handles；
5. `request_cancel(SERVER_SHUTDOWN)`；
6. 有界等待 Registry drain；
7. 对超时 handle 执行 `force_abort`；
8. 确认 per-run Registry/Channel 收口；
9. 关闭 Blocking/Tool Worker admission；
10. 有界等待 Worker，保留 detached 事实；
11. flush Observability；
12. flush Span Recorder（组件支持时）；
13. close Observability/Span；
14. close Snapshot Store；
15. close EventJournal；
16. close Model Client（仅当 tracked worker 已收口）；
17. close remaining stores/executors；
18. Admission Gate 与 lifespan state 置 `CLOSED`。

`shutdown()` 幂等，多次调用返回同一个 `ShutdownReport`。

## 12. Shutdown Order

`ApplicationRuntimeServices.close()` 仍是底层组件关闭执行器，但完整顺序由 `GracefulShutdownCoordinator` 决定。

关键保证：

- active run drain 在 Journal close 前；
- Observability/Trace flush 在 Journal close 前；
- Snapshot 不进入 request cancellation；
- 共享对象按 identity 去重；
- 单组件失败不会跳过后续组件；
- 每个组件调用都有 timeout；
- report 只记录 `component/status/duration/error_code`，不记录异常正文、路径、traceback 或 active ID 列表。

## 13. Journal / Observability / Trace

正常 cooperative cancellation 顺序：

```text
CANCELLATION journaled
-> RUN_COMPLETED journaled
-> Observability dispatcher 获得 record
-> active run unregister
-> Observability flush
-> Span flush/close
-> Journal close
```

Channel publish 仍保持 Journal-first。Transport 已不可用时，terminal 不发给 Client，但内部 drain 给 producer 留出持久化与收口机会。

Journal append 失败继续按既有 Runtime failure contract 收口，不做 Legacy fallback、不重跑业务。

## 14. Worker / Detached Worker

Tool 同步 worker 继续由 `ToolConcurrencyController` 跟踪；Retrieval blocking worker 继续由 `BoundedBlockingExecutor` 跟踪。

本轮真实发现并修复：生产 Coordinated 单 Agent Driver 原先经裸 `asyncio.to_thread` 执行，force cancel 后底层线程可能继续运行，却不在 application worker snapshot 中。

现在生产 Factory 使用独立 `coordinated_step_executor`：

- application-scoped；
- bounded admission；
- 记录 pending/running/detached；
- cancellation 时调用 `cancel_or_detach()`；
- Shutdown 统一停止 admission 和有限 drain；
- 后台真实完成时 Future callback 清理 tracker/gauge。

若期限后仍有 tracked worker，Shutdown 不把它标成 terminated，也不抢先 close Model Client，而是报告：

```text
RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER
```

## 15. Timeout / Force Abort

配置：

```text
RUNTIME_DISCONNECT_GRACE_SECONDS=0.75
RUNTIME_SHUTDOWN_GRACE_SECONDS=5.0
RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS=5.0
```

解析拒绝：

- 负数；
- NaN；
- Infinity；
- bool（内部构造 API）。

所有 deadline 使用 `time.monotonic()`。Disconnect grace 短于 Shutdown grace。Force abort 只处理 request-owned task/channel/registry，不关闭 Journal、Snapshot、Model Client、Blocking Executor，也不修改已提交 Tool side effect。

## 16. Asyncio Cancellation

`asyncio.CancelledError` 单独捕获：

```python
except asyncio.CancelledError:
    await bounded_cleanup()
    raise
```

Server body iterator、ChatService、RunCoordinator 均不把 task cancellation 转换成成功 EOF 或 SAFE_ERROR。RunCoordinator 在完成 terminal/registry/trace 清理后重新抛出 task cancellation。

## 17. Failure Isolation

- Watcher 自身异常：安全结束 watcher，不取消无法确认已断开的正常 Run；
- Drain timeout：force abort 当前 Scope；
- Component flush/close 异常：固定错误码并继续；
- Worker timeout：保留 detached；
- Model close 因活跃 worker 延后：显式 report，不假装成功；
- Scope construction 失败：abort partial Channel、注销 gauge、释放 admission；
- Shutdown 重复调用：返回缓存 report；
- 不执行跨 Runtime fallback。

## 18. Security

- Cancellation reason 为固定低基数枚举；
- Shutdown report 不含原始异常；
- 普通日志不输出 active run_id 列表；
- Registry handle 不含 prompt/output/tool/RAG/path/API key；
- Metrics label 不新增 run_id、trace_id、span_id、URL 或 error message；
- Client 只收到固定安全错误码；
- Disconnect 后不继续向不可用 Socket 输出 SAFE_ERROR。

本轮未新增 Metrics Descriptor，避免在没有明确消费需求时扩张指标面。

## 19. Runtime 真实接入

真实生产接入点：

- `server.py::lifespan` 创建 step executor、application services、factory、shutdown coordinator；
- `server.py::chat_endpoint` 创建唯一 disconnect watcher；
- `ChatService.stream_chat` 接入 Legacy admission/control handle；
- `ChatService.stream_coordinated_agent_events` 接入 cancel-and-drain；
- `CoordinatedRuntimeFactory.create_run_scope` 在 RunContext 前检查 admission；
- `ParallelExecutor` 在生产 Factory 中使用 tracked step executor；
- `ApplicationRuntimeServices` 执行有界 flush/close；
- `GracefulShutdownCoordinator` 成为 shutdown 顺序 owner。

## 20. Legacy Boundary

未修改：

- Legacy 正常 wire chunks；
- AgentLoop 行为与状态机业务语义；
- Tool Retry/Side Effect；
- Model Retry/Fallback；
- Retrieval 执行；
- Checkpoint/Recovery/Replay；
- 标准 SSE/WebSocket。

Legacy 的同步线程不可抢占限制被明确保留，不宣称与 Coordinated tracked worker 等价。

## 21. Bad Case

### Bad Case 1：长 Model Attempt 期间轮询不到 Disconnect

- 类型：真实发现
- 触发条件：修改前 Legacy 阻塞于 `to_thread(next)`，或 Coordinated 长时间没有下一个 Runtime Event。
- 故障表现：`request.is_disconnected()` 不能及时再次执行，后端继续占用 Model/Worker。
- 根因分析：Disconnect 检测绑定在“取得下一 chunk/event”之后，没有独立 watcher。
- 修复方案：每请求创建一个 watcher，统一写入现有 Run control handle。
- 回归测试：`tests/test_client_disconnect.py::test_disconnect_watcher_stops_output_and_is_awaited`。
- 对应知识点：ASGI disconnect、transport owner、并发信号仲裁。
- 面试表达：轮询本身不是错，错在轮询进度依赖业务流继续产出。
- 当前状态：已修复

### Bad Case 2：Consumer 退出后立即 Abort，Terminal 无 drain 机会

- 类型：真实发现
- 触发条件：修改前 Coordinated async generator 被 `aclose()`、`GeneratorExit` 或 task cancellation 终止。
- 故障表现：直接 abort Channel 并 cancel producer，容量已满时没有 drain-to-discard 生命周期。
- 根因分析：Transport cleanup 与 Runtime producer cleanup 被合并成“立即销毁”。
- 修复方案：先 request_cancel，再内部 drain，超时才 force abort。
- 回归测试：`tests/test_stream_cancellation.py::test_external_aclose_cancels_and_drains_capacity_one_channel`。
- 对应知识点：bounded queue、structured concurrency、Journal-first terminal。
- 面试表达：客户端不再消费，不等于 Runtime 内部也应停止消费生命周期事件。
- 当前状态：已修复

### Bad Case 3：断连竞态中向 Socket 输出 SAFE_ERROR

- 类型：真实发现
- 触发条件：修改前 unexpected exception 与 disconnect 同时发生，进入无条件 `except Exception: yield runtime-error`。
- 故障表现：向已经不可用的连接尝试写固定错误，产生二次写失败或噪声。
- 根因分析：异常投影没有读取统一 disconnect signal。
- 修复方案：Watcher signal 为真时所有异常分支停止 yield。
- 回归测试：`tests/test_client_disconnect.py` 验证断连后输出为空。
- 对应知识点：error projection、transport availability。
- 面试表达：安全错误只适合仍可写的 transport；断连后的正确输出是没有输出。
- 当前状态：已修复

### Bad Case 4：RunCoordinator 吞掉 Task Cancellation

- 类型：真实发现
- 触发条件：外层直接取消正在执行的 coordinator task。
- 故障表现：修改前把 `asyncio.CancelledError` 转成普通 `RunCoordinatorResult` 返回。
- 根因分析：领域取消和 asyncio task cancellation 共用了收口决策，但没有在 cleanup 后重新抛出后者。
- 修复方案：记录 task-cancelled 标志，完成 terminal、registry、trace 清理后重新抛出。
- 回归测试：`tests/test_client_disconnect.py::test_asgi_cancelled_error_is_cleaned_then_reraised` 及全仓取消测试。
- 对应知识点：BaseException、structured cancellation、cleanup-and-reraise。
- 面试表达：可以统一清理，不能统一传播语义。
- 当前状态：已修复

### Bad Case 5：Disconnect 与已成功 Terminal 竞态

- 类型：真实发现
- 触发条件：`RUN_COMPLETED` 已发布，但 async generator 尚未设置本地 `completed=True` 时发生外部 close。
- 故障表现：修改前 finally 仍调用 source.cancel，造成“状态成功、token 却是断连”的控制面不一致。
- 根因分析：transport 局部 completed 标志晚于权威 AgentState terminal。
- 修复方案：`scope.request_cancel()` 先检查权威 terminal state。
- 回归测试：正常完成、外部 close 与 first-wins 测试组合覆盖。
- 对应知识点：linearization point、authority state。
- 面试表达：终态权威应来自 Runtime 状态提交，不来自 transport 是否读完。
- 当前状态：已修复

### Bad Case 6：重复 Disconnect 发布两个 Cancellation

- 类型：假设构造
- 触发条件：ASGI cancellation、watcher、BrokenPipe 同时报告同一次断连。
- 故障表现：若每个信号独立发布，会出现重复 Cancellation/Terminal。
- 根因分析：缺少单一 logical owner 或 first-wins source。
- 修复方案：所有信号只请求同一个 source；RunCoordinator 仍是唯一 Terminal owner。
- 回归测试：canonical reason first-wins 与 RunCoordinator single-terminal 测试。
- 对应知识点：幂等、事件去重、single writer。
- 面试表达：多个物理信号可以存在，但只能映射到一个逻辑取消。
- 当前状态：防线已验证

### Bad Case 7：Grace Timeout 后仍无限等待同步 Worker

- 类型：真实发现
- 触发条件：修改前 lifespan registry grace 超时后直接 close services；Coordinated Driver 使用裸 `asyncio.to_thread` 且不受 application tracker 管理。
- 故障表现：无法报告仍运行 worker，或者错误地在活跃 attempt 期间关闭依赖。
- 根因分析：request task 与底层 thread 的完成事实被混为一谈。
- 修复方案：独立 bounded step executor、cancel-or-detach、worker drain timeout。
- 回归测试：`tests/test_stream_cancellation.py` 与 `tests/test_graceful_shutdown.py`。
- 对应知识点：thread cancellation limit、detached worker、bounded shutdown。
- 面试表达：取消 waiter 不等于取消线程；必须独立跟踪真实 worker。
- 当前状态：Coordinated 已修复；Legacy 限制已记录

### Bad Case 8：Shutdown 先关闭 Journal

- 类型：假设构造
- 触发条件：错误的关闭顺序在 active run terminal 前执行 Journal close。
- 故障表现：CANCELLATION/RUN_COMPLETED 无法持久化。
- 根因分析：application resource close 与 active run drain 缺少统一 owner。
- 修复方案：Shutdown Coordinator 固定先 drain runs，再 flush，最后 close Journal。
- 回归测试：`tests/test_shutdown_order.py`。
- 对应知识点：dependency shutdown DAG、durability boundary。
- 面试表达：创建顺序的逆序关闭不一定够，必须考虑运行中的数据流。
- 当前状态：防线已验证

### Bad Case 9：活跃 Model Attempt 前关闭 Model Client

- 类型：真实发现
- 触发条件：修改前 registry grace 超时但 active run 仍存在，lifespan 继续 `services.close()`。
- 故障表现：Model/HTTP Client 与仍运行的 attempt 竞态。
- 根因分析：没有 force abort、worker tracker 和 model-close gate。
- 修复方案：先 force run，再 drain tracked worker；仍 detached 时延后 Model close 并报告固定错误码。
- 回归测试：Shutdown order 与 worker timeout 测试。
- 对应知识点：resource ownership、in-flight dependency safety。
- 面试表达：有界关机不等于无条件关资源；可以有界地“明确不关并报告”。
- 当前状态：已修复

### Bad Case 10：SHUTTING_DOWN 期间仍创建 RunScope

- 类型：真实发现
- 触发条件：修改前 lifespan 设置 app state，但 Factory 依赖的 services lifecycle 在 close 前仍是 READY。
- 故障表现：cancel_all snapshot 后仍可能进入新 Run。
- 根因分析：没有 application admission gate，状态检查与 scope creation 非原子。
- 修复方案：ACCEPTING/DRAINING/CLOSED gate + admission lease。
- 回归测试：`tests/test_runtime_admission_gate.py`。
- 对应知识点：TOCTOU、admission control、shutdown quiescence。
- 面试表达：关闭不是先 snapshot 再祈祷没有新请求，而是先封门。
- 当前状态：已修复

### Bad Case 11：RunRegistry 保存完整 Scope

- 类型：假设构造
- 触发条件：为了 shutdown 方便，直接把 `CoordinatedRunScope` 放进 application registry。
- 故障表现：扩大 prompt、state、tool/RAG 数据与资源对象的驻留和泄露面。
- 根因分析：控制平面与业务对象所有权未分离。
- 修复方案：Registry 只保存 ActiveRunControlHandle 与安全 callback。
- 回归测试：Application services per-run rejection 与 Registry snapshot 测试。
- 对应知识点：capability handle、least authority、data minimization。
- 面试表达：Shutdown 需要取消能力，不需要拿到整棵对象图。
- 当前状态：防线已验证

### Bad Case 12：Registry Handle 持有 AgentState 正文

- 类型：真实发现
- 触发条件：修改前生产 `RunHandle` 直接保存 `agent_state`。
- 故障表现：application-scoped Registry 延长 per-run mutable state 生命周期。
- 根因分析：早期 Registry 同时承担状态 gauge 与取消控制。
- 修复方案：生产改为 callback 计算低基数 active-step count，不保存 state。
- 回归测试：生产 Factory/Legacy 注册类型与 Registry observability 测试。
- 对应知识点：weak control plane、state ownership。
- 面试表达：为一个 gauge 保存整个状态对象，是典型的过度持有。
- 当前状态：已修复

### Bad Case 13：Force Abort 修改已提交 Tool Side Effect

- 类型：假设构造
- 触发条件：非幂等 Tool 已 COMMITTED 后 Client Disconnect。
- 故障表现：若 abort 自动 compensation/retry，会重复或反向修改外部事实。
- 根因分析：把 runtime cleanup 误当业务事务回滚。
- 修复方案：Force abort 只关 request task/channel/registry，不改 Tool contract。
- 回归测试：既有 Tool side-effect/retry 全量测试继续通过。
- 对应知识点：at-most-once、outcome unknown、compensation boundary。
- 面试表达：取消等待者不等于撤销已经发生的世界状态。
- 当前状态：防线已验证

### Bad Case 14：Legacy 与 Coordinated 共享 CancellationSource

- 类型：假设构造
- 触发条件：为统一取消而把两个 Runtime 指向同一个全局 source。
- 故障表现：一个模式断连污染另一模式，甚至触发跨 Runtime fallback。
- 根因分析：错误扩大 application-scoped control。
- 修复方案：两种模式均 per-request 创建自己的 source，仅共享 admission gate 与 Registry 类型。
- 回归测试：Legacy/Coordinated mode boundary 测试。
- 对应知识点：isolation、scope ownership。
- 面试表达：统一控制协议，不代表共享控制实例。
- 当前状态：防线已验证

### Bad Case 15：Watcher Task 未取消

- 类型：假设构造
- 触发条件：正常 Client 完整消费，但 stream owner 忘记 await watcher。
- 故障表现：Task 泄漏并持有 Request。
- 根因分析：watcher 不在 structured lifecycle 中。
- 修复方案：所有出口调用 `_stop_disconnect_watcher()`。
- 回归测试：`test_disconnect_watcher_stops_output_and_is_awaited` 检查新增 task 集合为空。
- 对应知识点：structured concurrency、task ownership。
- 面试表达：后台 task 的创建点必须同时能指出其 join 点。
- 当前状态：防线已验证

### Bad Case 16：一个组件失败后跳过后续 Shutdown

- 类型：假设构造
- 触发条件：Snapshot/Model/Journal 任一 close 抛异常。
- 故障表现：后续组件不再 flush/close。
- 根因分析：串行 close 缺少 per-component failure isolation。
- 修复方案：每组件独立 timeout/exception projection，继续遍历。
- 回归测试：`tests/test_shutdown_order.py` 的 model close 故障仍继续 remaining/executor。
- 对应知识点：best-effort shutdown、error aggregation。
- 面试表达：关机报告可以失败，关机流程不能因一个失败停止。
- 当前状态：防线已验证

### Bad Case 17：ApplicationRuntimeServices 与 ShutdownCoordinator 双重关闭

- 类型：假设构造
- 触发条件：lifespan 和 coordinator 各自维护一套关闭顺序。
- 故障表现：同一对象 close 两次，顺序冲突。
- 根因分析：多个 shutdown owner。
- 修复方案：lifespan 只调用 coordinator；services close 本身缓存 report 且 identity 去重。
- 回归测试：Graceful shutdown idempotency 与 application close idempotency。
- 对应知识点：single owner、idempotent close。
- 面试表达：幂等是最后防线，唯一 owner 才是主要设计。
- 当前状态：防线已验证

### Bad Case 18：Drain 将 OUTPUT_DELTA 追加到 Final Output

- 类型：假设构造
- 触发条件：transport 退出后 drain 复用正常 adapter/aggregation 逻辑。
- 故障表现：Client 未接收的 delta 被误算作已交付正文，或继续写 Socket。
- 根因分析：内部生命周期消费与用户输出消费没有分离。
- 修复方案：Channel 提供独立 `drain_to_discard()`，不调用 adapter。
- 回归测试：容量 1 drain 测试只检查 Terminal/cleanup，不产生 late client output。
- 对应知识点：delivery semantics、discard consumer。
- 面试表达：内部处理完成与客户端成功交付是两个不同事实。
- 当前状态：防线已验证

### Bad Case 19：Detached Worker 被伪装成 Terminated

- 类型：假设构造
- 触发条件：取消 asyncio waiter 后立即删除 worker record。
- 故障表现：Gauge 归零但线程仍运行，Shutdown 错误关闭 Model Client。
- 根因分析：task completion 与 worker completion 混淆。
- 修复方案：`cancel_or_detach()` 保留 record，由 Future done callback 最终清理。
- 回归测试：Blocking executor、Tool worker、Shutdown worker 测试。
- 对应知识点：truthful observability、eventual cleanup。
- 面试表达：监控最危险的不是红色，而是假绿色。
- 当前状态：防线已验证

### Bad Case 20：裸 to_thread 的 Coordinated Model Worker 不可观测

- 类型：真实发现
- 触发条件：生产 `ParallelExecutor` 以 `SYNC_BLOCKING` 执行 single-agent driver。
- 故障表现：force cancel 只取消 waiter，底层线程不在 application worker tracker。
- 根因分析：通用 `asyncio.to_thread` 没有项目级 admission、record、detach callback。
- 修复方案：生产 Factory 注入独立 `coordinated_step_executor`。
- 回归测试：`tests/test_stream_cancellation.py` 验证 executor 最终 idle。
- 对应知识点：executor ownership、bounded work queue、detached execution。
- 面试表达：线程池不是生命周期管理；必须在它外面补 admission 与事实追踪。
- 当前状态：已修复

## 22. 测试结果

附件指定目标测试：

```text
141 passed, 4 subtests passed
```

全仓：

```text
678 passed, 42 subtests passed
```

新增文件：

```text
tests/test_client_disconnect.py
tests/test_stream_cancellation.py
tests/test_runtime_admission_gate.py
tests/test_graceful_shutdown.py
tests/test_shutdown_order.py
```

测试均使用 Fake、Barrier/Event、InMemory 组件，不调用真实网络、模型、Chroma、外部 Tool 或 UI。

## 23. 未完成事项

按本轮禁区明确未做：

- 标准 SSE/WebSocket；
- Tool/Model/RAG Retry 语义调整；
- 自动 Checkpoint；
- 自动 Recovery/Replay/Resume；
- 自动 Compensation；
- Fault Injection；
- 第二套 RunRegistry；
- 全局 Current Scope 缓存；
- 新 Metrics Descriptor。

真实限制：

- Legacy 第三方同步函数/C 扩展不可被 Python 强制抢占；
- 仍活跃的 tracked worker 会导致 Model Client close 延后到进程退出，不伪报已关闭；
- 本轮是进程内生命周期，不解决多进程 RunRegistry 协调。

## 24. 第四轮接入点

第四轮 E2E 文档可继续验证：

- Uvicorn/真实 ASGI transport 的 disconnect timing；
- Windows 下远端 HTTP read/write failure；
- 多请求 Shutdown barrier；
- 本地模型 C 扩展长期执行时的 process exit 行为；
- ShutdownReport 到运维健康页的安全投影；
- metrics descriptor 是否确有新增需求；
- 最终汇总到 `day23_e2e_runtime_result.md`。

本轮不提前实施 Fault Injection。

## 25. 需要带回 ChatGPT 审查的信息

```text
Disconnect owner: server.py 每请求唯一 watcher；生成器取消作为兜底
Disconnect signals: request.is_disconnected / CancelledError / GeneratorExit / aclose / BrokenPipe / ConnectionReset
Cancellation reason: 五个 canonical 固定枚举
Duplicate cancellation: first-wins；同一 Run 一个 source
Disconnect after terminal: 保持权威 terminal，不改成 cancelled
Disconnect client output: 断连后不输出 SAFE_ERROR
Cancel-and-drain: transport stop yield 后内部 drain-to-discard
Bounded channel: capacity=1 回归通过
Drain timeout: 有界，随后 force abort
Force abort: 仅 request task/channel/registry
RunRegistry handle: ActiveRunControlHandle，无完整 scope/state 正文
Admission states: ACCEPTING / DRAINING / CLOSED
New request during shutdown: 503 RUNTIME_SHUTTING_DOWN，scope 前拒绝
Legacy cancellation: 独立 source + generator close
Shutdown owner: GracefulShutdownCoordinator
Shutdown states: app SHUTTING_DOWN；gate DRAINING -> CLOSED
Shutdown grace: monotonic + finite config
Active run cancellation: SERVER_SHUTDOWN
Forced run count: ShutdownReport
Detached worker: 保留 tracker/gauge 事实
Journal close order: active run 与 flush 后
Observability flush order: Journal close 前
Trace flush order: Journal close 前（组件支持时）
Snapshot close order: request drain 后、Journal 前
Model close order: tracked worker idle 后；否则明确延后
Component failure: 固定 error_code，继续后续组件
CancelledError: bounded cleanup 后 re-raise
Watcher cleanup: cancel + await
RunRegistry after cleanup: 0（测试覆盖）
Lifespan final state: CLOSED
新增测试: 5 个文件
目标 pytest: 141 passed, 4 subtests passed
全仓 pytest: 678 passed, 42 subtests passed
compileall: 通过
lock check: 通过（Resolved 157 packages）
diff check: 通过（仅 Git 行尾转换 warning）
需要人工确认: Legacy 不可抢占同步/C 扩展限制是否符合部署预期
```
