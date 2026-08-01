# 第 24 天第三轮 C1：Observability / Trace Fault Injection

## 1. 本轮目标

本轮先修正 Snapshot 部分持久化与 Recovery 运行失败两个前置合同，再将五个确定性 Fault Point 接入真实 Observability/Trace 调用链。核心目标是证明诊断系统失败不会改变 Journal、Snapshot、Recovery 或业务执行事实，同时保持 Span 生命周期、取消、Disabled parity、Run 隔离与安全边界。

本轮没有接入 Shutdown Fault Point，没有修改 GracefulShutdownCoordinator 顺序，没有增加生产 Fault 开关、概率 Chaos、自动 Retry、Recovery 或 Replay。

## 2. Snapshot / Recovery 前置修正

`SnapshotPublicationEvidence` 现在通过三个布尔字段机器可读地区分结果：before-save 为 `persisted=false, partially_persisted=false, retry_allowed=false`；成功为 `true, false, false`；after-save failure 为 `true, true, false`。错误字符串不再承担状态判定，after-save 不自动 Retry，显式重复保存继续服从 Store 的 duplicate 合同，evidence/repr 不含 Snapshot payload。

`RecoveryStatus` 增加 `FAILED`。Snapshot read、Journal tail read 与 validation 注入失败分别返回 `FAILED + SNAPSHOT_READ_FAILED`、`FAILED + JOURNAL_TAIL_READ_NOT_EXECUTED`、`FAILED + RECOVERY_VALIDATION_FAILED`；取消仍使用现有安全取消原因。`UNSUPPORTED` 只保留给 schema、version 或 capability 不支持，未知 Snapshot schema 仍为 `INCOMPATIBLE_SCHEMA + SNAPSHOT_SCHEMA_UNSUPPORTED`。

## 3. 修改前 Observability 链路

真实顺序为：

```text
RuntimeEventChannel Journal append success
→ JournalRecord.from_event 安全投影
→ application-scoped RuntimeObservabilityDispatcher queue
→ dispatcher-owned worker consume
→ StructuredLogProjector / RuntimeMetricsProjector
→ logger / metrics recorder
→ dispatcher flush / close
```

Record owner 是 EventChannel 调用的 Dispatcher；queue 与 worker owner 是 Dispatcher；MetricDescriptor 与 label policy owner 是 metrics 模块；counter/histogram 由 JournalRecord consumer 更新，应用级 gauge 由 `ApplicationRuntimeGaugeProvider` 从真实 owner 快照采集；Health owner、flush owner、close owner均是 Dispatcher/其内部 Health，而不是 Fault Controller。

Observability 消费已验证的 `JournalRecord`，不是原始 `RuntimeEvent`。现有策略是 best effort：queue 满会 drop 并记 health；单个 logger/metrics sink 抛错会被 worker 隔离并降级 health；不会撤销 Journal、让 Event publication 失败或触发业务重跑。ApplicationRuntimeServices 在 close 前先 flush；本轮未改变 Shutdown 顺序。

## 4. 修改前 Trace 链路

RunCoordinator 创建 run root/planner span，ParallelExecutor 创建 step span，Model/Tool/Retrieval service 创建 invocation、attempt 或 stage span。`InMemorySpanRecorder` 生成 span identity、解析显式或当前 parent、保存 active/completed span；SpanHandle owner 负责 end；调用方通过 ContextVar token 安装并 reset 当前 span；Recorder health 的 active 数量是 active gauge 权威来源。

Trace 不依赖 Journal。应用服务拥有共享 recorder 的 flush/close；每个 Run 仅持有 facade，不拥有或关闭应用 recorder。Detached worker 仍由原执行 owner 在 finally/上下文退出处结束 span。审计发现嵌套 service 原先优先使用 application recorder，可能绕过 Run facade；现改为优先使用当前 trace context 的 recorder，再回退到应用 recorder。

## 5. Controller 传递

Dispatcher 与底层 Recorder 保持 application-scoped，不缓存当前 Run Controller。EventChannel 每次 submit 显式传入其 Run controller/token；RuntimeFactory 为传入 controller 的 Run 创建 `OperationScopedSpanRecorder` facade，facade 只代理 fault seam，不创建 identity、不选择 parent、不进入 Span，也不关闭共享 recorder。

不存在全局 current fault controller；Fault 配置不进入 Journal payload、Span attribute、Metric label、Health 或 wire。

## 6. OBSERVABILITY_BEFORE_RECORD

位置在真实 JournalRecord 已生成且安全投影可用之后、Dispatcher enqueue 之前。Raise/Delay/Block 命中均按既有 best-effort 合同降级 Observability health 并返回未记录，不删除 Journal、不重发 Event、不改变 sequence/AgentState、不调用 Model/Tool/Retrieval，也不向用户正文传播诊断错误。

回归覆盖普通 Step、Model、Tool、Retrieval、RunStarted 与 RunCompleted 六类真实 Event。Terminal JournalRecord、digest 与 sequence 在 record fault 后仍可验证。

## 7. OBSERVABILITY_BEFORE_FLUSH

位置在 flush operation 开始、实际 `queue.join` 之前。Raise/Delay/Block 返回固定安全错误 `OBSERVABILITY_FLUSH_FAILED` 或 `False`，等待受 timeout/cancellation 限制；已记录数据不删除、不伪报 flushed，不关闭 Journal，也不阻止后续显式 flush/close。

该点执行前没有新增不可逆状态，不标记 dangerous window。单次失败不触发任何业务重试。

## 8. TRACE_BEFORE_SPAN_START

位置在安全 span metadata 准备完成、Recorder 生成 identity/register active span 之前。命中后返回无 identity 的 noop handle，不创建 active span、不 push ContextVar、不增加 gauge、不产生孤立 end；业务按既有非关键路径策略继续，Trace health 使用固定安全码降级。

覆盖 Run root、Planner、Step、Model invocation、Tool attempt、Retrieval stage。若父 span 真实存在，失败 start 不覆盖当前 context，后续真实子 span仍使用最近可用真实 parent。

## 9. TRACE_BEFORE_SPAN_END

该点位于 Span 已存在、最终安全状态已准备、Recorder end 尚未提交的窗口，已加入 `DANGEROUS_FAULT_POINTS`。命中后不伪造 recorded end，而是执行 `logical closed + recorded end failed`：移除 active span、固定记录 end failure、保持 completed span 不新增、ContextVar 由原 owner reset。

该路径不创建第二个 Span、不改变业务 Result、不泄漏原始异常。active gauge 最终为零，health 如实降级。

## 10. TRACE_BEFORE_FLUSH

位置在 Recorder 已有记录、实际 flush 前。Raise/Delay/Block 失败使用固定 `TRACE_FLUSH_FAILED`，不删除或改写 span，不伪报成功，不创建新 span；等待有界，后续正常 flush/close 仍服从原 recorder 合同。该点不是不可逆持久化窗口。

## 11. Failure Isolation

| 故障 | Journal | Runtime Result | Health | 用户正文 |
| --- | --- | --- | --- | --- |
| Observability record 前失败 | 保留并可验证 | 不重跑业务 | degraded / record failure | 不泄漏 |
| logger/metrics sink 失败 | 保留 | 不重跑业务 | degraded / fixed safe code | 不泄漏 |
| Observability flush 前失败 | 保留 | 原结果不变 | flush failure | 不泄漏 |
| queue 满 | 保留 | 不重跑业务 | drop/backpressure 计数 | 不泄漏 |
| Trace start/end/flush 失败 | 保留 | 原结果不变 | degraded / fixed safe code | 不泄漏 |

Observability 与 Trace 均保持诊断 best effort；Fault Injection 没有把诊断失败升级为业务 fatal。

## 12. Journal Authority

组合测试在 Observability record fault 与 Trace root end fault 同时发生时，仍验证：业务仅执行一次、Terminal Journal 连续且 digest 有效、显式 checkpoint 成功保存、RecoveryValidator 返回 `TERMINAL`。诊断 failure 不删除记录、不改 sequence/digest、不补造 Terminal、不重跑 Model/Tool/Retrieval，也不改变 Snapshot/Recovery 只信任 Snapshot + Journal 的 authority。

## 13. Span Lifecycle

Span identity 与 parent 只由真实 recorder 创建；facade/controller 无此能力。Start fault 无 active/context；End fault 逻辑关闭并从 active 集合移除；调用 owner 的 token reset 恢复最近真实 context。Nested end fault、取消、Run root end fault、不同 Run trace tree 均保持 `active_span_count=0`，没有第二 Span或跨 Run parent。

`install_trace_context(None)` 与 `reset_trace_context(None)` 现在是 no-op，避免 noop/failed start 擦除真实父 context。

## 14. Health / Report

Observability health 新增 `record_failures`、`flush_failures`、`status`、`last_safe_error_code`；Trace health 新增 `start_failures`、`end_failures`、`flush_failures`、`status`、`last_safe_error_code`，并保留 active/completed/dropped 等计数。字段均为固定状态、低基数计数或固定安全码。

Health、Metric Label 与错误 repr 不保存 run_id、trace_id、span_id、event 列表、Fault Rule ID、路径、Provider URL、payload 或原始异常。Fault Rule ID 只存在测试 Fault Recorder 内。

## 15. Cancellation

Observability record/flush 与 Trace start/end/flush 的 Delay/Block 均覆盖 cancellation/timeout。Observability 不吞掉业务链路的 Run cancellation；Trace fault 不改写 first-wins cancellation reason。Span end 即使在 cancellation 下仍逻辑关闭并 reset context；flush 等待有界；scope close 释放测试 blocker，不泄漏 active span 或 dispatcher worker。

## 16. Disabled Parity

No Controller 与 Disabled Controller 比较了 JournalRecord/sequence、Observability log/metric/health、固定 span identity/hierarchy/status、span count、context reset 与 Runtime Result。两条路径等价，Disabled rule 的 `match_count=0, hit_count=0`。

## 17. Run / Operation Isolation

Run A Observability fault 不影响共享 Dispatcher 上 Run B 的正常记录；Run A Trace fault 不关闭共享 Recorder；controller/facade close 不关闭 application component；后续 operation 可正常 record/flush。Run B root span 不继承 Run A parent，各 tree 隔离。Gauge 继续由共享真实 owner 汇总，Controller 不参与 gauge ownership。

## 18. Security

安全扫描覆盖 Prompt、Model output、Tool argument/output、RAG chunk、Memory、私有路径、provider error、raw idempotency/resource key 与 raw Snapshot payload 标记。它们均未进入 Fault Context/Decision/Recorder、Observability/Trace Health、Span attribute、Metric Label、flush error、wire 或新增日志。结构化日志继续使用既有 allowlist 投影。

## 19. Runtime 真实接入

Observability seam 接入 `RuntimeEventChannel → RuntimeObservabilityDispatcher` 的真实 Journal-first 路径；Trace seam 经 RuntimeFactory 的 Run-owned facade 覆盖 RunCoordinator、ParallelExecutor 及 Model/Tool/Retrieval 嵌套 span。测试使用真实 JournalRecord、Dispatcher worker、metrics/logger consumer、InMemorySpanRecorder、RuntimeFactory、Checkpoint 与 RecoveryValidator，不以仅验证 Enum 的 fake 代替调用链。

## 20. Legacy Boundary

未修改 Model/Tool/Retrieval 业务语义、Journal append-only、Snapshot/Recovery authority、AgentState 状态机、Tool retry/compensation、自动 Recovery/Replay、生产 Settings/API/Header 或 GracefulShutdownCoordinator 顺序。共享 Dispatcher/Recorder 的 No Controller 路径保持兼容。

## 21. Bad Case

### Bad Case 1：After-save 部分持久化仍标记为普通 Store Failure

- 类型：真实发现
- 触发条件：审计原 `SnapshotPublicationEvidence` 与成功/after-save failure 路径。
- 故障表现：只有 `partially_persisted`，调用方不能直接机器判定已经持久化且禁止重试。
- 根因分析：持久化事实、部分发布事实与重试资格没有形成完整状态合同。
- 修复方案：增加并约束 `persisted`、`partially_persisted`、`retry_allowed` 三个布尔字段。
- 回归测试：before-save、成功、after-save、取消、duplicate 与 repr 测试均通过。
- 对应知识点：partial persistence、retry safety、typed evidence。
- 面试表达：提交后的发布失败必须如实报告持久化事实，不能靠错误字符串让调用方猜测。
- 当前状态：仓库真实合同缺口已修复；由代码审计发现，不是已证实的生产事故。

### Bad Case 2：Recovery 运行失败被标记为 Unsupported

- 类型：真实发现
- 触发条件：Snapshot read、Tail read 或 validation Fault 命中。
- 故障表现：原实现返回 `UNSUPPORTED`，混淆运行故障与 schema/capability 不支持。
- 根因分析：RecoveryStatus 缺少通用运行失败状态，故障路径复用了错误语义。
- 修复方案：增加 `FAILED`，运行 fault 返回 FAILED，保留 UNSUPPORTED 的版本/能力语义。
- 回归测试：三类 fault、取消、unknown schema 与正常 decision 回归通过。
- 对应知识点：错误分类、fail closed、协议语义稳定性。
- 面试表达：Unsupported 是能力判断，读取失败是运行结果；二者必须让机器明确区分。
- 当前状态：仓库真实合同缺口已修复；不是已证实的生产事故。

### Bad Case 3：Observability record failure 删除 JournalRecord

- 类型：假设构造
- 触发条件：Journal append 后、Dispatcher enqueue 前注入失败。
- 故障表现：诊断失败回滚或删除已经成立的业务事实。
- 根因分析：混淆 Journal authority 与可选诊断投影。
- 修复方案：保持 Journal-first；fault 仅阻止诊断 enqueue 并降级 health。
- 回归测试：六类 Event 与 Terminal 均验证 Journal record、sequence、digest 保留。
- 对应知识点：authority boundary、best effort projection。
- 面试表达：可观测性是事实的消费者，不是事实是否存在的裁判。
- 当前状态：已由真实链路测试防护；是假设风险。

### Bad Case 4：Observability failure 触发业务重跑

- 类型：假设构造
- 触发条件：record、sink 或 flush 失败被错误提升为业务 publication failure。
- 故障表现：Model/Tool/Retrieval 或整个 Run 被重复执行。
- 根因分析：诊断 best-effort 合同被错误升级为业务 fatal。
- 修复方案：隔离诊断错误，只更新固定安全 health，不影响 Runtime Result。
- 回归测试：组合测试验证业务结果与调用次数不变。
- 对应知识点：failure isolation、side-effect safety。
- 面试表达：诊断丢失可降级，业务副作用不能因为写日志失败而重放。
- 当前状态：已防护；是假设风险。

### Bad Case 5：Trace start failure 仍 push 不存在的 span

- 类型：真实发现
- 触发条件：noop/failed start 返回后仍调用原 context 安装逻辑。
- 故障表现：`install_trace_context(None)` 原行为会把真实父 context 暂时覆盖为 None。
- 根因分析：context helper 没有把“无 span”视为不应 push 的状态。
- 修复方案：None handle 的 install/reset 均为 no-op；start fault 返回无 identity noop。
- 回归测试：failed child start 后 grandchild 仍指向最近真实 root。
- 对应知识点：ContextVar token、parent continuity、failed start semantics。
- 面试表达：创建失败意味着根本没有新上下文，不能用空上下文覆盖现有父节点。
- 当前状态：仓库真实机制缺口已修复；不是已证实的生产事故。

### Bad Case 6：Trace end failure 导致 active span 永久非零

- 类型：假设构造
- 触发条件：active span 已注册、Recorder end 前注入失败。
- 故障表现：active gauge 永久偏高并造成资源/健康假象。
- 根因分析：把“end 未记录”错误等同于“span 仍逻辑运行”。
- 修复方案：执行 logical close，移除 active，并单独记录 end failure。
- 回归测试：root/nested/cancellation end fault 后 active count 均为零。
- 对应知识点：logical lifecycle、dangerous window、gauge truthfulness。
- 面试表达：遥测落盘失败不应延长业务 span 的逻辑生命周期。
- 当前状态：已防护；是假设风险。

### Bad Case 7：Trace end failure 未 reset ContextVar

- 类型：假设构造
- 触发条件：end fault 抛出后绕过调用 owner 的 finally/reset。
- 故障表现：后续 span 错误继承已经结束的 parent。
- 根因分析：将 context cleanup 绑定到 recorder end 成功。
- 修复方案：end fault 在 handle 内隔离，调用方原 token reset 始终执行。
- 回归测试：nested end fault 与取消后 context 恢复到父或 None。
- 对应知识点：exception-safe cleanup、context stack。
- 面试表达：记录结束可以失败，但上下文出栈必须是 finally 级别的不变量。
- 当前状态：已防护；是假设风险。

### Bad Case 8：为补 Trace 失败创建第二个 span

- 类型：假设构造
- 触发条件：start/end 失败后用新 span 表示诊断失败。
- 故障表现：同一业务动作出现重复 identity、错误 parent 与虚假计数。
- 根因分析：Fault Controller 越权成为 Span identity owner。
- 修复方案：Controller/facade 不生成 identity；失败只更新安全 health。
- 回归测试：每个 fault 场景验证无第二 Span、无孤立 end。
- 对应知识点：identity ownership、at-most-once instrumentation。
- 面试表达：遥测补偿不能制造另一段不存在的业务时间线。
- 当前状态：已防护；是假设风险。

### Bad Case 9：Flush failure 阻止其他组件后续关闭

- 类型：假设构造
- 触发条件：Observability 或 Trace flush Raise/Delay/Block。
- 故障表现：关闭链无限等待或跳过其他组件 close。
- 根因分析：flush 没有 deadline/cancellation，且把单组件失败当成全局停止条件。
- 修复方案：operation flush 有界并返回固定失败；不改变应用 close owner/顺序。
- 回归测试：fault 后可再次正常 flush/close，已记录数据保留。
- 对应知识点：bounded shutdown、idempotent close、failure containment。
- 面试表达：flush 是关闭前的努力，不应成为无限期占有整个关闭流程的锁。
- 当前状态：已防护；是假设风险。

### Bad Case 10：Health 保存 run_id 或 span_id

- 类型：假设构造
- 触发条件：为了定位故障把高基数 identity 放进全局 health。
- 故障表现：内存增长、敏感标识泄漏和不可聚合状态。
- 根因分析：混淆诊断详情与健康摘要。
- 修复方案：Health 只保存低基数计数、状态和固定安全码。
- 回归测试：安全扫描确认 Health/repr 无敏感 identity/payload/path。
- 对应知识点：cardinality control、data minimization。
- 面试表达：Health 回答系统是否健康，不应变成逐请求事件仓库。
- 当前状态：已防护；是假设风险。

### Bad Case 11：Fault Rule ID 写入 Metric Label

- 类型：假设构造
- 触发条件：用 Metric Label 关联某条注入规则。
- 故障表现：高基数时序膨胀并将测试控制面暴露到生产指标。
- 根因分析：Fault Recorder 与 Metrics ownership 边界不清。
- 修复方案：Rule ID 只进入 Fault Recorder；Metrics 保持既有 descriptor allowlist。
- 回归测试：组合安全测试扫描所有 metric label。
- 对应知识点：label budget、control-plane isolation。
- 面试表达：规则命中详情属于审计 recorder，不属于业务指标维度。
- 当前状态：已防护；是假设风险。

### Bad Case 12：Disabled Controller 改变 Span ID

- 类型：假设构造
- 触发条件：禁用 controller 仍包装、重建或预分配 Span identity。
- 故障表现：No Controller 与 Disabled Controller 的 trace tree 不一致。
- 根因分析：Fault plumbing 侵入 identity owner。
- 修复方案：disabled evaluate 不命中，identity 始终由同一底层 recorder 创建。
- 回归测试：固定 identity 工厂下 span identity/hierarchy/status/health 完全一致，counter 0/0。
- 对应知识点：zero-impact instrumentation、determinism。
- 面试表达：禁用故障注入不仅结果相同，trace identity 也必须保持字节级语义等价。
- 当前状态：已防护；是假设风险。

### Bad Case 13：Run A Trace Fault 污染 Run B parent

- 类型：真实发现
- 触发条件：嵌套 Model/Tool/Retrieval service 同时持有 application recorder 与当前 Run facade。
- 故障表现：原解析顺序优先 application recorder，可能绕过 Run-scoped fault/context facade。
- 根因分析：application dependency 的优先级高于 operation trace context。
- 修复方案：优先 `current_span_recorder()`，缺失时再回退应用 recorder；共享 owner不缓存 controller。
- 回归测试：Run A fault 后 Run B root parent 为 None，共享 recorder 仍可用。
- 对应知识点：scope precedence、trace tree isolation。
- 面试表达：应用级 recorder 可共享，但当前 operation facade 必须优先，否则会跨过请求边界。
- 当前状态：仓库真实接线风险已修复；不是已证实的生产事故。

### Bad Case 14：Cancellation 被 Trace Fault 覆盖

- 类型：假设构造
- 触发条件：Delay/Block 期间 Run 已取消，Trace 返回自己的原始异常或新取消原因。
- 故障表现：first-wins cancellation reason 被诊断层改写。
- 根因分析：诊断控制流拥有了业务 cancellation authority。
- 修复方案：轮询既有 token，隔离 trace failure；end 仍逻辑关闭。
- 回归测试：start/end/flush cancellation 与 reason/context/active 不变量通过。
- 对应知识点：cancellation ownership、first-wins reason。
- 面试表达：Trace 可以观察取消，不能重新定义取消。
- 当前状态：已防护；是假设风险。

### Bad Case 15：Observability/Trace Error 输出到用户正文

- 类型：假设构造
- 触发条件：将原始 sink/provider/fault exception 拼接进 Runtime Result。
- 故障表现：用户看到内部错误、路径或敏感业务内容。
- 根因分析：诊断 health 与业务输出通道未隔离。
- 修复方案：只记录固定安全码，诊断异常不越过 best-effort 边界。
- 回归测试：组合 fault 的最终业务输出与正常路径一致，安全标记均不存在。
- 对应知识点：error sanitization、channel separation。
- 面试表达：诊断失败属于运维平面，不能污染面向用户的业务协议。
- 当前状态：已防护；是假设风险。

### Bad Case 16：Diagnostic failure 改变 Snapshot/Recovery Authority

- 类型：假设构造
- 触发条件：诊断失败后以 Trace/Metric 缺失为由补写 Journal、改 Snapshot 或调整 Recovery decision。
- 故障表现：不可观测等同于业务未发生，历史事实被诊断数据反向改写。
- 根因分析：把派生信号提升为持久化权威。
- 修复方案：Journal/Snapshot 始终是 authority；诊断只消费或旁路记录。
- 回归测试：双诊断 fault 后 checkpoint 持久化且 Recovery 仍从 Snapshot+Journal 判定 TERMINAL。
- 对应知识点：source of truth、derived data、recovery authority。
- 面试表达：Trace 缺口只能说明没记录到，不能证明业务没执行。
- 当前状态：已防护；是假设风险。

## 22. 测试结果

- 目标测试：附件列出的 13 个文件共 `73 passed`；再加入 dispatcher、trace contract/propagation 与 fault contract 的补充回归共 `96 passed`。
- 全仓：`944 passed, 42 subtests passed`。
- 新增：`tests/_diagnostic_fault_fixtures.py`、`test_observability_fault_injection.py`、`test_observability_flush_fault.py`、`test_trace_fault_injection.py`、`test_trace_lifecycle_fault.py`、`test_diagnostic_fault_isolation.py`。
- 更新：Snapshot/Recovery/Fault contract 测试及相关现有实现。
- `compileall`、`uv lock --check`、`git diff --check` 均通过。

## 23. 未完成事项

Shutdown Fault Point、Shutdown ordering fault、生产 Fault 配置入口、概率 Chaos、第 25 天内容均未实现，按范围留给后续。当前 C1 不声称外部 telemetry exporter 的网络级可靠性；验证边界是仓库现有 logger/metrics/recorder 合同。

## 24. 第三轮 C2 接入点

C2 应只在真实 GracefulShutdownCoordinator owner 边界接入 Shutdown fault，不复用 C1 facade 去改变 shutdown ownership。可复用本轮的有界 Delay/Block、固定安全码、controller scope、logical close、Disabled parity、Run 隔离和 authority 测试方法，但不得改变既有取消、flush、close 顺序。

## 25. 需要带回 ChatGPT 审查的信息

| 问题 | 结论 |
| --- | --- |
| Snapshot partial status | after-save 为 `persisted=true, partially_persisted=true` |
| Snapshot retry allowed | `false` |
| Recovery failure status | `FAILED` |
| Unsupported reserved for | schema / version / capability |
| Observability record owner | EventChannel 调用的 RuntimeObservabilityDispatcher operation |
| Observability queue/worker owner | application-scoped Dispatcher |
| Observability flush owner | Dispatcher；ApplicationRuntimeServices 编排 |
| Observability failure policy | best effort，health degraded |
| Observability journal/runtime effect | Journal 保留，Runtime Result 与业务次数不变 |
| Observability health | 低基数状态、计数、固定安全码 |
| Trace identity/parent owner | 底层 SpanRecorder |
| Trace start/end owner | Recorder start；SpanHandle/Recorder end |
| Trace context owner | 调用 operation 的 ContextVar token/reset |
| Trace active gauge | Recorder active 集合/health |
| Span-start fault | 无 identity、active、push 或孤立 end |
| Span-end fault | logical closed，recorded end failed |
| Trace flush fault | 固定失败、有界、保留 spans，可后续 flush/close |
| Active spans after failure | 0 |
| Context restored | 是 |
| Journal authority | 不受诊断 failure 影响 |
| Runtime business rerun | 0 |
| Cancellation | 保持原 token 与 first-wins reason |
| Disabled parity | 等价；match/hit 均 0 |
| Run isolation | Controller/facade 不污染共享 owner或 parent |
| Fault data in metrics/trace/wire | 无；Rule ID 仅在 Fault Recorder |
| 需要人工确认的问题 | 无阻断项；C2 的 Shutdown seam 与顺序另行审计 |
