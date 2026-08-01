# 第 24 天第三轮 C2：Graceful Shutdown Fault Matrix

## 1. 本轮目标

本轮在既有 GracefulShutdownCoordinator 真实关闭链上接入五个 Shutdown Fault Point，并复用 C1 的 Observability/Trace Flush seam。目标是让 Run cancel、worker drain、Journal/Model/其他组件 close 的失败彼此隔离，使 ShutdownReport 准确表达已执行、未执行、失败、超时、deferred 与最终 worker 事实。

本轮没有修改 Model/Tool/Retrieval 业务语义，没有强杀线程、删除 worker record、重跑业务、自动 Recovery/Replay/Compensation，也没有增加生产 Fault Settings/API/Header、概率 Chaos 或跨进程协调。

## 2. 修改前 Shutdown 顺序

审计后的真实物理顺序为：

```text
lifecycle -> SHUTTING_DOWN
→ admission -> DRAINING
→ wait admission leases
→ snapshot active run handles
→ request SERVER_SHUTDOWN cancellation
→ bounded run drain
→ force abort remaining runs + registry cleanup
→ close worker admission
→ bounded worker drain
→ observability flush
→ trace flush
→ observability / trace close
→ snapshot close
→ journal close
→ model close if worker-safe
→ remaining executor / store close
→ admission CLOSED
→ lifecycle CLOSED
```

组件的物理先后顺序没有为 Fault Injection 重排。C2 只把 lifecycle 的 `SHUTTING_DOWN` 状态在 coordinator 开始时明确发布，避免关闭前半段仍显示 READY。

## 3. Shutdown Owner

唯一编排 owner 是 `GracefulShutdownCoordinator`。ApplicationRuntimeServices 拥有 application component identity、flush/close 调用与 lifecycle；RuntimeAdmissionGate 拥有 admission；RunRegistry 拥有 active handle 集合；ActiveRunControlHandle/CancellationSource 拥有 per-run cancel 与 first-wins reason；worker controller/executor 拥有 admission、idle、active/detached 事实；Dispatcher/SpanRecorder 拥有 flush；SnapshotStore/Journal/Model/remaining component 拥有自己的 close。

Coordinator 拥有 component timeout、force-abort 编排、Model safety gate 与最终 ShutdownReport。ApplicationRuntimeServices 继续按 object identity 去重 close。

## 4. Controller 传递

接口为 `GracefulShutdownCoordinator.shutdown(fault_controller=None)`。Controller 只作为本次 shutdown operation 参数继续显式传给 run cancel、worker drain、flush 与 close seam；Coordinator、ApplicationRuntimeServices、RunRegistry 和 application component 均不缓存它。

生产默认值是 `None`。没有 ContextVar、模块全局或 Run facade 复用；Trace flush 使用面向本次 Shutdown operation 的临时 facade，底层 application recorder identity 与 lifecycle owner不变。

## 5. Fault Point 唯一性

互斥映射为：Run cancel 使用 `SHUTDOWN_BEFORE_RUN_CANCEL`；worker drain 使用 `SHUTDOWN_BEFORE_WORKER_DRAIN`；Journal close 使用 `SHUTDOWN_BEFORE_JOURNAL_CLOSE`；符合 safety gate 的 Model close 使用 `SHUTDOWN_BEFORE_MODEL_CLOSE`；其他真实 close 使用 `SHUTDOWN_COMPONENT_CLOSE`。

Journal/Model 不执行 generic seam；不存在 callable close/shutdown 时不 evaluate；共享 identity 只保留第一次真实 close。集中测试确认 specific counter 为 1 时 generic counter 为 0。

## 6. Run Cancel Fault

active handles 在 admission settle 后一次性快照。每个未完成 handle 分别 evaluate，Fault Context 只包含 SHA-256 `run_id_digest`、固定 runtime mode、component 与 operation kind。单 Run failure 只产生固定 `RUNTIME_RUN_CANCEL_INJECTED_FAILURE/TIMEOUT`，继续处理其他 Run。

普通 callback exception 也逐 Run 隔离为 `RUNTIME_RUN_CANCEL_FAILED`。已存在 CLIENT_DISCONNECTED reason 不被 SERVER_SHUTDOWN 覆盖；不创建第二 CancellationSource，不修改 AgentState，也不向 client 输出 fault error。

## 7. Run Drain / Force Abort

无论 cancel seam 是否成功，都会执行 bounded registry drain，再对 remaining handle逐一 force abort。force callback failure/timeout 使用固定码，finally 只清理 request-owned registry handle；不会关闭 application Journal、Model 或 Store，也不会修改已提交 Tool side effect。

Report 区分 `active_run_count`、`cancel_requested_count`、`cancelled_run_count`、`cancel_failed_count`、`gracefully_drained_count`、`forced_run_count` 与 `remaining_run_count`。cancel fault 后不会伪报 graceful completion。

## 8. Worker Drain Fault

所有 worker admission close 已尝试后，只有存在真实 `wait_until_idle` owner 时才 evaluate `SHUTDOWN_BEFORE_WORKER_DRAIN`。命中后不调用 wait、不篡改 active/detached record、不伪报 IDLE，等待受 component timeout 限制，并继续后续 Flush/Close。

Report 同时保留本次 drain operation 的 `FAILED` 事实和 shutdown 末尾重新读取的 active/detached/unknown snapshot；即使 worker 在 fault 后自然归零，也不会把未执行的 drain 改写为 IDLE。

## 9. Model Close Gate

Model close eligibility 必须同时满足：worker drain 已真实执行并成功，且 active、detached、unknown worker 均为零。否则每个真实 Model close target 返回 `DEFERRED + RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER`。

共享 Model/HTTP object 的 identity 会被保护：当 Model deferred 时，同一对象不能通过 `remaining_store` 等别名被 close。没有强杀线程、删除 worker record或把 asyncio waiter 完成当作线程终止。

## 10. Observability / Trace Flush

Shutdown 将同一 operation controller 显式传给已有 `OBSERVABILITY_BEFORE_FLUSH` 与 `TRACE_BEFORE_FLUSH`。Observability 失败后仍执行 Trace；Trace 失败后仍执行 component close、Snapshot 与 Journal。两个 Fault 可同时命中，分别报告 `RUNTIME_OBSERVABILITY_FLUSH_FAILED/TIMEOUT` 与 `RUNTIME_TRACE_FLUSH_FAILED/TIMEOUT`。

没有增加 Shutdown-specific 同义 flush seam，没有删除 Journal、重跑业务或输出用户错误。

## 11. Journal Close Fault

`SHUTDOWN_BEFORE_JOURNAL_CLOSE` 位于 Observability/Trace flush 与 Snapshot close 尝试之后、真实 `event_journal.close()` 前。命中后不调用 close，Report 明确 FAILED，继续 Model 与 remaining component；不自动重试。

InMemory 与 SQLite 回归均在 fault 后重新读取并 verify 原 JournalRecord，确认 sequence、digest 与 Terminal authority 不被修改；测试最后显式关闭资源。

## 12. Model Close Fault

`SHUTDOWN_BEFORE_MODEL_CLOSE` 只在 worker safety gate 通过且存在真实 Model close 时 evaluate。命中后 Model 不关闭，Report 使用 `RUNTIME_MODEL_CLOSE_INJECTED_FAILURE/TIMEOUT`，不会误报 active-worker deferred，并继续关闭独立 remaining component。

Worker active/deferred 时该 point counter 为 0，避免消费不存在的物理 close。

## 13. Generic Component Close

`SHUTDOWN_COMPONENT_CLOSE` 覆盖 Observability close、Trace recorder close、Snapshot Store、remaining store/HTTP client、blocking executor shutdown 与其他真实 close owner。每个组件使用独立 timeout；fault 不调用真实 close，一个 failure 不跳过后续组件。

匹配只使用低基数 `shutdown_component`。没有 callable operation 的对象不进入 close report、不消费 rule；共享 object identity 只 close 一次。

## 14. Shutdown Report

复用并最小扩展现有 `ShutdownReport`，现包含 admission/lifecycle final state、active/cancel/graceful/forced/remaining Run counts、worker drain status、active/detached/unknown worker counts、Observability/Trace flush status、component results、deferred Model、total duration 与固定安全错误码。

没有创建第二套报告。每个 component result 只含低基数组件名、状态、duration 和固定 error code。

## 15. Report Truthfulness

未执行 drain 不能标 IDLE；未执行 close 不能标 COMPLETED；active/detached/unknown 非零时 Model 不能 close；injected timeout 与 ordinary close failure 使用不同固定码；单组件失败后其他独立组件仍有真实结果，而不是被统一标 NOT_ATTEMPTED。

`ShutdownReport.completed` 同时要求 Admission 与 Lifecycle 均为 CLOSED。

## 16. Idempotency

Coordinator 在第一次完整 shutdown 后缓存同一个 ShutdownReport。第二次调用直接返回同一对象，不重新 evaluate Fault Rule、cancel/force、flush 或 close；counter 与 component close count不增加。即使首次报告 partial failure/deferred，也不借幂等 shutdown 隐式重试。

## 17. Cancellation

`asyncio.CancelledError` 不被 `_record_call`、fault helper、flush 或 close 的普通 Exception 分支吞掉。Shutdown task 被取消时重新抛出；Lifecycle 保持 SHUTTING_DOWN，Admission 保持 DRAINING，不回到 READY/ACCEPTING，不伪造完整成功 Report。

Fault blocker task随 cancellation 被取消，controller scope close 释放测试 blocker；已关闭组件不会被重新打开。本轮未修改 ASGI lifespan 的传播合同。

## 18. Disabled Parity

No Controller 与 Disabled Controller 对比了关闭顺序、Lifecycle/Admission、Run counts、worker snapshot、flush/close calls、Model close、component results 与总状态。除 duration 外语义一致；Disabled rule 的 `match_count=0, hit_count=0`。

## 19. Isolation

一次 faulted Shutdown operation 不污染新构造的 application container；controller close 不关闭 Runtime component；Run controller 不参与 Shutdown seam；application component不缓存 Shutdown controller。Run A cancel failure 不阻止 Run B，component A failure 不覆盖 component B result，共享 identity 去重继续生效。

## 20. Security

ShutdownReport、Fault Context/Decision/Recorder、Health、日志和 wire 均不保存 run_id/thread ID 列表、trace/span ID、Fault Rule ID、Prompt/Output、路径、原始异常或对象 repr。Run 只以 SHA-256 digest 进入 Fault Context；Report 不保存该 digest。

安全扫描覆盖指定 Prompt、Model、Tool、RAG、Memory、私有路径、provider error、raw key/payload 与明文 run/thread 标记，均未进入 Report/repr。

## 21. Runtime 真实接入

Fault seam 接入真实 `GracefulShutdownCoordinator → RuntimeAdmissionGate/RunRegistry/ApplicationRuntimeServices` 链路。Flush 使用真实 C1 Dispatcher/Trace seam；close 作用于 application services 的真实去重 target；Journal 数据测试使用真实 InMemory/SQLite implementation；worker truth兼容 BlockingExecutor snapshot、Tool worker snapshot 与 active/detached properties。

测试不是仅检查 Enum：每个 point 都断言物理调用次数、counter、后续组件、Report 与真实资源状态。

## 22. Legacy Boundary

既有 Coordinated/Legacy worker lifecycle、stream cancellation、admission、shutdown order、runtime full E2E 与 invariants 均通过。未改变业务执行、CancellationSource first-wins、Journal/Snapshot authority、Model/Tool/Retrieval retry/side effect 或 application close 顺序。

## 23. Bad Case

### Bad Case 1：Shutdown Controller 被缓存到 Application Services

- 类型：假设构造
- 触发条件：把某次 shutdown 的 controller 保存为 application container 字段。
- 故障表现：后续 shutdown/container 继承旧规则，scope close 还可能影响真实组件。
- 根因分析：operation-scoped 控制面与 application lifecycle owner 混淆。
- 修复方案：只通过 shutdown/flush/close 参数显式传递，不缓存。
- 回归测试：新 container 正常关闭，controller close 不关闭 component。
- 对应知识点：scope hygiene、dependency lifetime。
- 面试表达：关闭 owner 可以长生命周期存在，但某次故障计划只能属于单次 operation。
- 当前状态：已防护；是假设风险。

### Bad Case 2：Journal/Model 同时执行 specific 与 generic Seam

- 类型：假设构造
- 触发条件：close loop 先统一执行 generic，再追加 Journal/Model specific point。
- 故障表现：同一物理 close 产生两次 match/hit，报告无法对应真实动作。
- 根因分析：专用 seam 以叠加而非互斥映射实现。
- 修复方案：按组件选择唯一 FaultPoint。
- 回归测试：Journal/Model specific=1、generic=0；Snapshot generic=1。
- 对应知识点：physical seam uniqueness、specificity。
- 面试表达：一个物理边界只能有一个权威故障点，否则 counter 失真。
- 当前状态：已防护；是假设风险。

### Bad Case 3：Run cancel Fault 阻止其他 Run 取消

- 类型：真实发现
- 触发条件：原实现用 `sum(handle.request_cancel(...) for handle in handles)` 批量调用，任一 callback 抛异常。
- 故障表现：generator 提前退出，后续 Run 与整个 shutdown sequence 均可能中断。
- 根因分析：per-Run control operation 缺少独立异常边界。
- 修复方案：逐 handle 调用、逐项固定报告并继续。
- 回归测试：Run A cancel exception/fault 后 Run B 仍收到 SERVER_SHUTDOWN，之后仍 force abort。
- 对应知识点：bulkhead、per-item failure isolation。
- 面试表达：批量取消不是一个事务，单个控制句柄失败不能阻断其他租户。
- 当前状态：仓库真实代码缺口已修复；代码审计发现，不是已证实的生产事故。

### Bad Case 4：Cancel Fault 后不再 Force Abort

- 类型：假设构造
- 触发条件：cancel seam 返回失败后直接跳到 component close。
- 故障表现：目标 Run 无限存活，Registry/Channel 无法收口。
- 根因分析：把 cancel request 成功误作 bounded drain/force 的前置资格。
- 修复方案：无条件执行 registry wait 与 remaining force-abort。
- 回归测试：cancel injected timeout/failure 后 forced count 增加且 remaining 为零。
- 对应知识点：escalation ladder、bounded shutdown。
- 面试表达：协作式取消可以失败，最终仍必须进入有界强制收口层。
- 当前状态：已防护；是假设风险。

### Bad Case 5：Worker Drain Fault 被当成 Worker idle

- 类型：真实发现
- 触发条件：原 Model gate 只使用 `worker_report.completed`，没有独立表达“drain 未执行”的状态。
- 故障表现：引入 pre-drain fault 后若复用空/默认 report，可能错误允许 Model close。
- 根因分析：operation evidence 与 worker state evidence未分开。
- 修复方案：单独保存 drain completed/faulted，并读取 active/detached/unknown snapshot。
- 回归测试：drain fault 即使 worker 已自然归零，status 仍 FAILED、Model 仍 deferred。
- 对应知识点：absence of execution、state truthfulness。
- 面试表达：现在 idle 不证明这次 drain 操作执行过，两类事实要同时报告。
- 当前状态：仓库真实合同缺口已修复；不是已证实的生产事故。

### Bad Case 6：未证明 Worker idle 就关闭 Model Client

- 类型：真实发现
- 触发条件：wait 返回 truthy，但真实 worker snapshot 仍 active/detached/unknown。
- 故障表现：Model/HTTP client 在后台线程仍使用时被关闭。
- 根因分析：Model gate 只相信 wait 返回值，没有交叉核对 worker owner。
- 修复方案：drain success 与三类 worker count 全为零才 eligible。
- 回归测试：worker drain failure/timeout/detached 路径均 deferred Model。
- 对应知识点：resource safety gate、double evidence。
- 面试表达：关闭共享 client 前既要 drain operation 成功，也要真实 owner 证明没有使用者。
- 当前状态：仓库真实安全门缺口已修复；不是已证实的生产事故。

### Bad Case 7：Detached Worker 被删除以满足 Shutdown

- 类型：假设构造
- 触发条件：为让 count 归零而清空 worker record或强杀线程。
- 故障表现：Report 显示 idle，但后台 side effect 仍可能进行。
- 根因分析：把 bookkeeping cleanup 当作 execution termination。
- 修复方案：保留 owner snapshot，只报告 detached/active 并 defer Model。
- 回归测试：Legacy detached worker 真实测试与 drain fault snapshot 均保留 count。
- 对应知识点：thread truth、detached lifecycle。
- 面试表达：删除记录只能让仪表盘变绿，不能让线程停止。
- 当前状态：已防护；是假设风险。

### Bad Case 8：Observability Flush Fault 跳过 Trace Flush

- 类型：假设构造
- 触发条件：flush loop 遇首个异常立即返回。
- 故障表现：一个诊断 sink 的失败扩大为所有 telemetry 丢失。
- 根因分析：独立 component 没有逐项 failure isolation。
- 修复方案：每个 flush 独立报告并继续。
- 回归测试：Observability fault 后 Trace flush count 为 1，close 继续。
- 对应知识点：best effort fan-out、bulkhead。
- 面试表达：多个诊断 sink 是并列依赖，不应串成失败短路链。
- 当前状态：已防护；是假设风险。

### Bad Case 9：Trace Flush Fault 跳过 Journal Close

- 类型：假设构造
- 触发条件：Trace flush exception 越过 ApplicationRuntimeServices 边界。
- 故障表现：Journal 未 close，shutdown 无法完成或权威数据资源泄漏。
- 根因分析：诊断 failure 获得了持久化 lifecycle authority。
- 修复方案：固定报告 Trace failure，继续 Snapshot/Journal close。
- 回归测试：Trace fault 与双 flush fault 后 Journal close count 都为 1。
- 对应知识点：authority separation、failure containment。
- 面试表达：Trace 是派生数据，不能决定 Journal 是否关闭。
- 当前状态：已防护；是假设风险。

### Bad Case 10：Journal Close Fault 被伪报成功

- 类型：假设构造
- 触发条件：fault 命中后跳过 close，却沿用默认 completed result。
- 故障表现：Report 声称 Journal 已关闭，实际仍开放。
- 根因分析：未执行与成功共享默认状态。
- 修复方案：生成 specific FAILED result 与固定 injected code。
- 回归测试：Journal close count 为 0，Report FAILED，数据仍可读。
- 对应知识点：negative evidence、truthful reporting。
- 面试表达：跳过操作本身就是可报告事实，不能用成功默认值填充。
- 当前状态：已防护；是假设风险。

### Bad Case 11：Model Close Fault 被误报为 active-worker deferred

- 类型：假设构造
- 触发条件：Model 已 eligible，但 close seam failure统一映射到 deferred。
- 故障表现：运维无法区分安全门阻止与实际 close 注入失败。
- 根因分析：eligibility decision 与 execution result 合并。
- 修复方案：gate 未过用 DEFERRED；gate 通过后的 fault 用 INJECTED_FAILURE/TIMEOUT。
- 回归测试：Model fault 不含 deferred code，active worker时 point counter 为 0。
- 对应知识点：decision vs execution、error taxonomy。
- 面试表达：没资格尝试和尝试后失败是两种完全不同的关闭事实。
- 当前状态：已防护；是假设风险。

### Bad Case 12：Component Fault 阻止后续独立组件

- 类型：假设构造
- 触发条件：close loop 在第一个 injected/real exception 后中止。
- 故障表现：remaining store、HTTP client、executor 未尝试关闭。
- 根因分析：缺少 per-component timeout 与 exception boundary。
- 修复方案：每个 target 独立调用、独立结果、继续循环。
- 回归测试：real/injected/timeout component failure 后 later close count 为 1。
- 对应知识点：best-effort cleanup、structured result。
- 面试表达：关闭可以部分失败，但独立资源必须各自获得一次有界机会。
- 当前状态：已防护；是假设风险。

### Bad Case 13：重复 Shutdown 再次执行 Fault

- 类型：假设构造
- 触发条件：第二次 shutdown 不返回缓存 report，而是重走 seam。
- 故障表现：counter、cancel/force、flush/close count 二次增加。
- 根因分析：幂等只覆盖组件 close，未覆盖整个 coordinator operation。
- 修复方案：第一次完成后缓存并返回同一 ShutdownReport。
- 回归测试：第二次 report identity 相同，controller snapshot 不变。
- 对应知识点：operation idempotency、cached terminal result。
- 面试表达：幂等 shutdown 的单位是完整编排，不只是每个 close 方法。
- 当前状态：已防护；是假设风险。

### Bad Case 14：Shutdown Report 保存 run_id/thread_id

- 类型：假设构造
- 触发条件：为了定位失败保存 active Run/worker明文列表。
- 故障表现：敏感 identity 泄漏并造成高基数 report。
- 根因分析：健康摘要与逐实例调试数据混淆。
- 修复方案：Report 只保存 counts；Run digest 也不进入 Report。
- 回归测试：敏感标记与字段名扫描通过。
- 对应知识点：data minimization、cardinality control。
- 面试表达：关闭报告回答多少对象失败，不需要暴露它们是谁。
- 当前状态：已防护；是假设风险。

### Bad Case 15：Shutdown task cancellation 恢复 Admission

- 类型：假设构造
- 触发条件：CancelledError cleanup 将 lifecycle/admission重置为 READY/ACCEPTING。
- 故障表现：部分组件已关闭后又接受新 Run。
- 根因分析：把 shutdown cancellation 当成事务回滚。
- 修复方案：传播 CancelledError，但状态保持 SHUTTING_DOWN/DRAINING。
- 回归测试：Block seam 中取消 task 后异常传播且 admission 不重开。
- 对应知识点：monotonic lifecycle、cancellation safety。
- 面试表达：关闭是单调状态机，外层 task 取消不能让已关闭资源复活。
- 当前状态：已防护；是假设风险。

### Bad Case 16：Fault Rule ID 进入 ShutdownReport

- 类型：假设构造
- 触发条件：直接复制 FaultDecision 到 component result。
- 故障表现：测试控制面泄漏，高基数且耦合内部计划。
- 根因分析：Recorder evidence 与用户可见 lifecycle report未隔离。
- 修复方案：Report 只用固定 runtime error code；Rule ID 仅留 Fault Recorder。
- 回归测试：repr/安全扫描和字段审计通过。
- 对应知识点：control-plane separation、safe error projection。
- 面试表达：报告故障类别即可，不应公开命中哪条测试规则。
- 当前状态：已防护；是假设风险。

### Bad Case 17：不存在 Close 操作却消费 Rule

- 类型：真实发现
- 触发条件：原 `_targets` 可包含无 callable close 的对象，`_invoke_bounded` 将其当成功。
- 故障表现：接入 generic seam 后会对不存在的物理 close evaluate，counter 与 report 失真。
- 根因分析：target enumeration 与 operation capability 检查分离且默认 no-op success。
- 修复方案：在 evaluate 与 report 前过滤非 callable operation。
- 回归测试：missing component rule 的 match/hit 均为 0。
- 对应知识点：capability detection、physical operation truth。
- 面试表达：没有调用能力就没有故障窗口，不能为虚构操作计数。
- 当前状态：仓库真实实现缺口已修复；不是已证实的生产事故。

### Bad Case 18：关闭共享对象两次

- 类型：假设构造
- 触发条件：Model client 与 shared HTTP/remaining store 指向同一 object。
- 故障表现：第二次 close 抛错或破坏幂等外部 client。
- 根因分析：按组件名而非 object identity 去重。
- 修复方案：ApplicationRuntimeServices 继续按 identity 保留首个 owner。
- 回归测试：同一对象以 Model/remaining 两名注册时 eligible close count 为 1。
- 对应知识点：aliasing、resource ownership、identity deduplication。
- 面试表达：组件名可以多个，物理资源 owner 只能关闭一次。
- 当前状态：已防护；是假设风险。

### Bad Case 19：Model Deferred 后通过 Remaining Alias 关闭同一对象

- 类型：真实发现
- 触发条件：同一对象同时注册为 `model_*` 与非 Model component，且 `include_models=false`。
- 故障表现：原 target 过滤移除 Model 名称后，非 Model 别名仍可进入 close loop，绕过 safety gate。
- 根因分析：deferred 只按组件名过滤，没有保护 Model-owned identity。
- 修复方案：预先收集 Model identity；deferred 时排除所有同 identity alias。
- 回归测试：active worker + shared Model/remaining object 时 close count 为 0。
- 对应知识点：capability alias、safety gate bypass、identity ownership。
- 面试表达：安全门必须保护物理对象 identity，不能只保护一个注册名称。
- 当前状态：仓库真实接线风险已修复；不是已证实的生产事故。

## 24. 测试结果

- 新增：`tests/_shutdown_fault_fixtures.py`、`test_shutdown_fault_injection.py`、`test_shutdown_run_cancel_fault.py`、`test_shutdown_worker_drain_fault.py`、`test_shutdown_component_fault.py`、`test_shutdown_report_truthfulness.py`、`test_shutdown_fault_isolation.py`。
- 附件目标集：`56 passed`。
- 全仓：`972 passed, 42 subtests passed`。
- InMemory/SQLite Journal、Legacy worker、stream cancellation、C1 flush、runtime full E2E 与 invariants 均纳入回归。
- `compileall`、`uv lock --check` 与 `git diff --check` 均通过。

## 25. 未完成事项

没有实现生产 Fault enablement、概率 Chaos、跨进程 Shutdown、线程强杀、自动 Recovery/Replay/Compensation 或第 25 天内容。外部 exporter/OS process 级关闭不在本轮权威边界内。

## 26. 第四轮接入点

第四轮应基于前三轮已稳定的 operation scope、dangerous window、fixed safe evidence、Journal/Snapshot authority、diagnostic isolation 与 shutdown report继续做最终矩阵整合。不得把 Fault Controller 提升为全局 Runtime owner，也不得重新定义现有业务 retry/side-effect/cancellation 合同。

## 27. 需要带回 ChatGPT 审查的信息

| 问题 | 结论 |
| --- | --- |
| Shutdown owner | GracefulShutdownCoordinator |
| Shutdown controller scope | 单次 shutdown operation；显式传递、不缓存 |
| Production enablement | 无，默认 None |
| Shutdown order | lifecycle/admission → Run → worker → flush → close → CLOSED |
| Run cancel point | 每个未完成 ActiveRunControlHandle 调用前 |
| Run cancel failures | 固定码、逐 Run 计数 |
| Other runs cancelled | 是 |
| Force abort after cancel fault | 是 |
| Worker drain point | worker admission 停止后、真实 wait 前 |
| Worker state after drain fault | 保留 active/detached/unknown 最终快照，operation status FAILED |
| Model close eligibility | drain success 且 active/detached/unknown 全 0 |
| Model close deferred | 固定 `RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER` |
| Observability / Trace flush | 复用 C1 seam，独立失败并继续 |
| Journal / Model close point | specific seam，互斥 generic |
| Generic close point | 其他真实 callable close/shutdown |
| Specific/generic duplicate | 无 |
| Component failure isolation | 后续独立组件继续 |
| Shutdown report fields | 状态、Run/worker counts、flush、component、duration、安全码 |
| Report sensitive data | 无 |
| Shutdown idempotency | 返回同一缓存 Report |
| Fault reevaluated on second call | 否 |
| Lifecycle / Admission final state | CLOSED / CLOSED（完整 shutdown） |
| Detached worker truth | 不删除、不伪报 idle，Model deferred |
| Task cancellation | CancelledError 传播，状态不回退 |
| Disabled parity | 语义等价，match/hit 0/0 |
| Operation isolation | 新 container/controller 不受污染 |
| Fault data in report/log/wire | 无 Rule ID、正文、路径、原始异常 |
| 需要人工确认的问题 | 无阻断项；生产 enablement 与跨进程协调明确不在本轮 |
