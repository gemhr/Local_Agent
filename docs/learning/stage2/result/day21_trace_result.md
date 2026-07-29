# 阶段二第 21 天改造结果

## 1. 本次目标
在 Coordinated Runtime 中建立安全、失败隔离的本地调用链；Trace 只观察事实，不拥有或修改 RunState。

## 2. 修改前 Trace 状态
`RunContext` 已创建/承载 `trace_id`，Event、Journal、Log 可关联 trace，但没有 span。Run 生命周期在 `RunCoordinator.execute`，Step TaskGroup 在 `ParallelExecutor.execute`；Model Invocation/Attempt 由 `ModelInvocationRouter`/Retry 回调拥有，Tool 由 `ToolExecutionService`/`ToolAttemptExecutor` 拥有，Retrieval Stage 由 `RetrievalExecutionService` 拥有。同步入口为 `BoundedBlockingExecutor.submit`。Query Rewrite 经 retrieval adapter 调用内部 model router。`core/chat_service.py` 与 `server.py` 的默认 `/api/chat` 仍为 Legacy。

审计了 context/run_coordinator/parallel_execution/scheduler/model_invocation/retry/tool_execution/retrieval_execution/retrieval_adapters/retrieval_context/blocking_executor/events/event_channel/event_journal/event_journal_store/structured_logging/metrics/chat_service/server，以及第 16～20 天结果和对应测试。RunContext 的创建入口包括 `create_run_context`、`RunContext.create` 及 Coordinated Runtime 的装配测试/服务；Legacy 入口不拥有完整 Span。

## 3. Trace ID Owner
唯一 Owner 仍是 `RunContext.identifiers.trace_id`；Root、child、Event 均复用它，不创建第二套 Trace ID。

## 4. TraceContext
不可变对象包含 trace/span/parent/run/step 身份并校验安全字符、空值、长度与自引用；不含 Attributes，不接触状态。

## 5. SpanRecord
不可变完成记录使用 UTC 展示时间与 monotonic duration，状态为 UNSET/OK/ERROR/CANCELLED/TIMED_OUT，不保存 Exception，拒绝非有限 duration。

## 6. SpanRecorder
提供 Protocol、InMemory、Noop、OpenTelemetry-compatible adapter；Handle 提供安全属性及各终态 API。重复结束幂等且第一次结果获胜。

## 7. Span Lifecycle
创建 Handle 后安装 ContextVar token，退出时结束并 reset；sink end 失败被隔离，start 通过 `start_span_safely` 降级 Noop。close 幂等且不等待远端。

## 8. Run / Planner / Step Span
Coordinator 创建唯一 Run root 与 Planner child；ParallelExecutor 为真实启动的 Step 建 child。Span 读取真实结果，但不写 RunStatus/StepStatus。

## 9. Parallel TaskGroup Span
TaskGroup 创建于 `ParallelExecutor.execute`；每个 worker 从创建时 Run context 生成独立兄弟 Step，任务 ContextVar 副本隔离。

## 10. Model Invocation / Attempt Span
契约支持 Invocation → Attempt；当前基础 recorder/context 已可由 Model Router 边界使用。Model 的真实 Owner 仍为 Router/Retry，不由 Trace 接管，也禁止正文属性。

## 11. Tool Invocation / Attempt Span
契约支持 Invocation → Attempt，允许安全 side-effect/detached 字段。Tool 业务语义未修改；真实 Owner 仍为 Service/AttemptExecutor。超时 Handle 第一次关闭后 worker 不可覆盖。

## 12. Retrieval / Stage Span
契约支持 Retrieval → 实际 Stage；统一策略是不为 SKIPPED 伪造执行 Span，DEGRADED 用 `degraded` 安全属性。Stage Owner 仍为 RetrievalExecutionService。

## 13. Query Rewrite Model Span
正确装配要求 Query Rewrite context 激活期间调用 Router，因此内部 Model parent 为 Query Rewrite，不回退到 Step；本日没有迁移 retrieval 业务语义。

## 14. Async Context Propagation
使用 ContextVar token set/reset；并发 asyncio Task 各有独立上下文，不使用全局可变 current span。

## 15. Thread Context Propagation
`BoundedBlockingExecutor.submit` 用 `copy_context()` 在提交时捕获并只在 worker operation 内安装；返回后由 Context.run 清理。既有 nested submission 错误保持不变。

## 16. RuntimeEvent / Journal Correlation
Event/Draft 与 JournalRecord 增加 nullable span_id/parent_span_id；Emitter 只读取当前 Span，Journal 不生成 ID。摘要覆盖新身份；SQLite 启动时安全增加 nullable 列；旧记录保持 null。

## 17. Structured Log Correlation
Structured Log 投影增加 nullable span/parent 字段，满足 Event、Journal、Log 身份相等。

## 18. Metrics Boundary
未重复记录第 20 天指标；现有 Metrics label policy 继续拒绝 trace_id/span_id/parent_span_id，未把高基数身份注册为 label。

## 19. Attribute Safety
API allowlist 包括 component/operation/status/error_code/retry/candidate/model/retrieval/budget/timeout/cancellation/side-effect/provider/detached/worker/degraded/count 及安全 tool_name；prompt/messages/output/query/vector/chunk/memory/secret/key/url/path/exception/traceback 等在 API 层拒绝。

## 20. OpenTelemetry Compatibility
本地 Adapter 映射 trace/span/parent/name/start/end/status/safe attributes；没有 Collector、Exporter 或远端依赖。

## 21. Failure Isolation
Recorder start 降级 Noop；sink record 异常被 Handle 隔离；不会导致业务 Retry、状态变更或二次执行。Handle 本地先原子关闭，后调用 sink。

## 22. Runtime 真实接入
真实 Run、Planner、Parallel Step、Emitter、Journal、Log 和 Blocking Executor 已接入；并非独立 demo。Model/Tool/Retrieval 的专用 Invocation/Stage 包装仍为后续风险项。

## 23. Legacy 边界
默认 `/api/chat` 保留原 trace 日志，不伪造 Span、不迁移 API；Coordinated Runtime 才有完整 root/step 链。

## 24. 重点 Bad Case

### Bad Case 1：Retry Attempt 创建新根 Trace
- 类型：假设构造
- 触发条件：Attempt 忽略 parent。
- 故障表现：同一 Invocation 被拆链。
- 根因分析：重新生成 trace_id。
- 修复方案：复用 RunContext trace 与 Invocation parent。
- 回归测试：层级契约。
- 对应知识点：Trace ownership。
- 面试表达：Retry 是 child，不是新请求。
- 当前状态：契约已防护。

### Bad Case 2：并行 Step 串成父子
- 类型：真实风险
- 触发条件：在前一 Step context 内创建下一 Step。
- 故障表现：A→B→C。
- 根因分析：隐式 current parent 使用错误。
- 修复方案：显式捕获共同 parent。
- 回归测试：async sibling 测试。
- 对应知识点：TaskGroup context。
- 面试表达：并行单元必须共享父而非共享 current。
- 当前状态：已修复。

### Bad Case 3：线程池丢失 Context
- 类型：真实发现
- 触发条件：ThreadPoolExecutor 原生 submit。
- 故障表现：worker 日志无 span。
- 根因分析：ContextVar 不自动跨线程。
- 修复方案：提交时 copy_context。
- 回归测试：传播测试与既有 executor 测试。
- 对应知识点：thread propagation。
- 面试表达：async 自动复制不等于线程自动复制。
- 当前状态：已修复。

### Bad Case 4：ContextVar 未 reset
- 类型：假设构造
- 触发条件：异常退出遗漏 token reset。
- 故障表现：后续操作串链。
- 根因分析：只 set 未 finally reset。
- 修复方案：contextmanager finally reset。
- 回归测试：context reset。
- 对应知识点：dynamic scope。
- 面试表达：token 是成对资源。
- 当前状态：已防护。

### Bad Case 5：Recorder 失败导致 Run 失败
- 类型：假设构造
- 触发条件：adapter start/record 抛错。
- 故障表现：业务失败或 Retry。
- 根因分析：观测异常穿透。
- 修复方案：safe start 与 sink isolation。
- 回归测试：Noop/failure contract。
- 对应知识点：failure isolation。
- 面试表达：Telemetry 必须 fail-open。
- 当前状态：已防护。

### Bad Case 6：Timeout Span 未结束
- 类型：假设构造
- 触发条件：TimeoutError 离开 scope。
- 故障表现：UNSET 永久挂起。
- 根因分析：无终态 finally。
- 修复方案：scope 映射 TIMED_OUT。
- 回归测试：Span lifecycle。
- 对应知识点：terminal completeness。
- 面试表达：停止等待就是 attempt 终点。
- 当前状态：已防护。

### Bad Case 7：Detached Worker 修改已关闭 Span
- 类型：真实风险
- 触发条件：同步 timeout 后 worker 完成回调。
- 故障表现：TIMED_OUT 被 OK 覆盖。
- 根因分析：多方拥有终态。
- 修复方案：Handle first-end-wins。
- 回归测试：重复结束契约。
- 对应知识点：detached lifecycle。
- 面试表达：worker 只清理资源，不重开 span。
- 当前状态：已防护。

### Bad Case 8：Span 保存 Prompt
- 类型：假设构造
- 触发条件：调用者传 prompt attribute。
- 故障表现：敏感正文泄漏。
- 根因分析：自由字典属性。
- 修复方案：API allowlist。
- 回归测试：unsafe attribute rejection。
- 对应知识点：data minimization。
- 面试表达：安全靠边界，不靠约定。
- 当前状态：已防护。

### Bad Case 9：Trace 成为状态 Owner
- 类型：假设构造
- 触发条件：Span 回调写 RunStatus。
- 故障表现：观测改变业务结果。
- 根因分析：职责倒置。
- 修复方案：只从最终 Result 映射状态。
- 回归测试：Coordinator 状态回归。
- 对应知识点：single owner。
- 面试表达：Trace 是投影而非事实源。
- 当前状态：已防护。

### Bad Case 10：Query Rewrite Model 父节点错误
- 类型：真实风险
- 触发条件：rewrite scope 未激活。
- 故障表现：Model 挂到 Step。
- 根因分析：内部调用边界丢 parent。
- 修复方案：rewrite scope 内显式 parent。
- 回归测试：嵌套层级测试待增强。
- 对应知识点：nested invocation。
- 面试表达：内部模型属于 stage。
- 当前状态：契约完成，专用接入待增强。

### Bad Case 11：旧 Event 伪造 span_id
- 类型：真实兼容风险
- 触发条件：读取旧 SQLite 行。
- 故障表现：出现虚假链路。
- 根因分析：迁移时生成 ID。
- 修复方案：nullable 列与 null 默认。
- 回归测试：Journal 兼容测试。
- 对应知识点：schema evolution。
- 面试表达：缺失身份不能推测。
- 当前状态：已修复。

### Bad Case 12：Trace ID 作为 Metrics Label
- 类型：假设构造
- 触发条件：把关联身份传入 label。
- 故障表现：高基数爆炸。
- 根因分析：混淆 logs/traces 与 metrics。
- 修复方案：label policy 拒绝三种 span/trace 字段。
- 回归测试：metrics policy 回归。
- 对应知识点：cardinality。
- 面试表达：指标聚合，Trace 精确关联。
- 当前状态：已防护。

## 25. 测试结果
新增 contract/hierarchy/propagation/integration 四组离线测试；目标测试在当前环境因 uv 锁中 Windows 本地 wheel 不可解析而无法由 uv 启动，系统 Python 的可运行子集通过。

## 26. 未完成事项与风险
没有 Collector；没有远程 Exporter；没有跨服务 Header；没有完整 Sampling；Trace Store 仅内存/Noop；默认 Legacy API 尚无完整 Trace；Detached Worker 后续生命周期不修改已关闭 Span；旧 Journal Event 可能没有 span_id；Trace 不参与 Snapshot/Recovery；不保证跨进程 Trace Context。Model/Tool/Retrieval 专用 span 的逐边界真实接入和更完整 timeout worker 测试仍需后续增强。

## 27. 面试表达
我把 RunContext 保持为 trace_id 唯一 Owner，以 immutable context、ContextVar token 和 first-end-wins record 建立树；异步显式共同父、同步 submit 捕获 context；Event/Journal/Log 只关联 nullable 身份；所有 recorder 故障 fail-open，状态机仍是业务事实 Owner。

## 28. 需要带回 ChatGPT 审查的信息
重点复核：Model/Tool/Retrieval 专用边界尚未全面包装；uv.lock 引用 Windows wheel 导致 Linux 环境不能执行 uv 测试；SQLite nullable migration 与旧摘要兼容策略。

## 29. 本地应用冲突诊断

本提交的直接父提交是 Day 20 `667941f`。将 `b015e9f` 生成的 patch 应用到该父提交时，`git apply --check` 成功；因此提交本身不是损坏 patch。

若在已经包含 Day 21 改动的工作树再次应用同一 patch，会同时出现两类错误：

1. `core/runtime/__init__.py`、`blocking_executor.py`、`event_emitter.py`、`event_journal*.py`、`events.py`、`parallel_execution.py`、`run_coordinator.py`、`structured_logging.py` 报 `patch does not apply`，因为对应上下文已经被同一提交修改；
2. `tracing.py`、`trace_context.py`、`span_recorder.py`、`trace_propagation.py`、四个 Trace 测试和本文档报 `already exists`，因为新增文件已经存在。

这组错误组合表示“重复应用同一个 Day 21 patch”，而不是尚未解决的 Git merge conflict。当前分支 `git status --porcelain=v2` 为空，且 `git diff --name-only --diff-filter=U` 无输出，没有 unmerged index entry。

本地处理建议：

- 若 `git log --oneline` 已包含 `b015e9f`（或内容等价的 Day 21 commit），不要再次 `git apply` / `cherry-pick`；
- 若正在 cherry-pick 同一提交且确认本地已含全部内容，使用 `git cherry-pick --skip`，不要把所有文件盲目标成 ours/theirs；
- 若本地基线不是 `667941f`，先保存工作区并将本地 Day 20 分支与 `667941f` 对齐，再 cherry-pick；如本地确有独立修改，应按文件语义合并，而不是重复加入新增文件；
- 可用 `git patch-id --stable` 或 `git cherry` 判断不同 commit hash 是否其实包含相同 patch。

---

## 30. 本轮增量完成：真实 Invocation / Attempt / Stage 接入

> 本节为 2026-07-29 在本地真实路径上的继续实现记录。以上 1～29 节为原始 Day 21 文档，保持不改写；本轮结果仅在此后增量追加。

### 1. 完成范围

本次完成 Coordinated Runtime 的真实 Model、Tool、Retrieval 调用链接入，以及 Event/Journal/Log 身份、Journal v1/v2 摘要兼容、Dropped Span 指标和 Active Span 不变量。

未实现 Snapshot、Replay、默认 API 迁移、Prometheus、Collector、Jaeger 或第 22/23 天内容。附件第八点“跨平台 uv 测试环境”按用户说明忽略：当前在本地真实 Windows 路径运行，未修改 `pyproject.toml`、`uv.lock` 或本地 `llama_cpp_python` wheel 策略。

### 2. Trace 所有权与传播

- `RunContext.trace_id` 仍是唯一 Trace ID Owner。
- `RunCoordinator` 创建 Run root span，并把当前 Span Recorder 与 Trace Context 一起安装到 ContextVar。
- `ParallelExecutor` 的真实 worker 创建 Step span；并行 Step 共享 Run parent，不互相串联。
- `BoundedBlockingExecutor` 继续通过 `copy_context()` 把 Trace Context 和当前 Recorder 传播到同步 worker。
- 所有 token 均在 `finally` 中 reset；不同 Run 不共享当前 span。
- Recorder start 失败降级为无身份 Noop handle，Event 使用 `span_id=null`，不会伪造 span ID。

### 3. Model Invocation / Attempt

真实 Owner：

```text
ModelInvocationRouter
└── Model Invocation Span
    ├── Model Attempt Span (candidate/retry 0)
    ├── Model Attempt Span (retry 1)
    └── Model Attempt Span (fallback candidate)
```

- 一次 `invoke` 只有一个 Invocation span。
- 每个 retry、fallback、circuit-open、context-limit、budget-exhausted 或真实 provider attempt 都有独立 Attempt span。
- Attempt 的 `parent_span_id` 等于 Invocation span ID；Invocation parent 来自当前 Step 或 Query Rewrite Stage。
- Circuit Open 等 provider 未启动路径记录 `provider_started=false`，不记录 token、cost 或 provider duration。
- Provider attempt 的 `MODEL_STARTED` 与 `MODEL_COMPLETED` 在 Attempt span 作用域内发布，Completed 在 span end/reset 前发布。
- Recorder 失败不进入 Model retry/fallback 判定。
- Span 只保存 profile、candidate/retry index、provider_started 和安全错误码，不保存 prompt、messages、output、URL、API key 或异常正文。

### 4. Tool Invocation / Attempt

真实 Owner：

```text
ToolExecutionService
└── Tool Invocation Span
    ├── ToolAttemptExecutor / Tool Attempt Span (retry 0)
    └── ToolAttemptExecutor / Tool Attempt Span (idempotent replay/retry 1)
```

- 稳定 `ToolInvocation` 对应一个 Invocation span，每个真实 Attempt 对应独立子 span。
- Retry/幂等 replay 保持同一 Invocation parent；NON_IDEMPOTENT 的既有禁止重试语义不变。
- Attempt span 只记录 tool name、retry index、provider_started、side-effect state、retry disposition 和 detached 元数据。
- `TOOL_STARTED`/`TOOL_COMPLETED` 在对应 Attempt span 内发布。
- 同步 timeout 在 Runtime 停止等待时结束 Attempt span；detached worker 后续只释放 permit/lease，不修改或重开 span。
- Resource Conflict、Output Too Large、Cancellation、Timeout、Compensation Failure 和 Outcome Unknown 沿用既有业务状态映射。

### 5. Retrieval / Stage 与 Query Rewrite

真实 Owner：

```text
RetrievalExecutionService
└── Retrieval Span
    ├── Query Rewrite Stage Span
    │   └── Model Invocation Span
    │       └── Model Attempt Span
    ├── Embedding Stage Span
    ├── Vector Retrieve Stage Span
    ├── Keyword Retrieve Stage Span
    ├── Rerank Stage Span
    ├── Document Load Stage Span
    └── Context Build Stage Span
```

- `_run_stage` 只为实际执行的 Stage 创建 span；`SKIPPED` 只保留结果记录，不创建 span，也不发布虚假的 Stage Completed event。
- Stage span 记录 stage、input/output count 和 degraded 标记，不记录 query、embedding、chunk、citation 正文、memory 或路径。
- Query Rewrite Stage 激活期间调用内部 `ModelInvocationRouter`，所以 Model Invocation 的实际 parent 是 Query Rewrite span，而不是 Step。
- `RETRIEVAL_STARTED`/`RETRIEVAL_COMPLETED` 使用 Retrieval span 身份。
- `RETRIEVAL_STAGE_COMPLETED` 使用对应 Stage span 身份。
- 延迟 Context Binding 路径保留 Retrieval/Context Build handle，先发布完成事件，再结束并 reset span。
- EMPTY、FAILED、CANCELLED、TIMED_OUT、DEGRADED 继续按真实结果映射；detached worker 不覆盖已结束 span。

### 6. Event / Journal / Structured Log 身份

已验证：

```text
RUN_STARTED.span_id == RUN_COMPLETED.span_id == Run Span ID
STEP_STARTED.span_id == STEP_COMPLETED.span_id == Step Span ID
MODEL_STARTED.span_id == MODEL_COMPLETED.span_id == Model Attempt Span ID
TOOL_STARTED.span_id == TOOL_COMPLETED.span_id == Tool Attempt Span ID
RETRIEVAL_STARTED.span_id == RETRIEVAL_COMPLETED.span_id == Retrieval Span ID
RETRIEVAL_STAGE_COMPLETED.span_id == 对应 Stage Span ID
```

Event、JournalRecord 与 Structured Log 保留相同的 nullable `span_id`/`parent_span_id`。Event 只读取当前 span，不创建 ID；Recorder 降级时身份为 null。

### 7. Journal Schema 与 Digest 兼容

- 新 Runtime Event schema：v2。
- 新 Journal schema：v2。
- v1 Runtime Event 仍可读取，用于旧记录兼容。
- v1 Journal digest 严格按旧字段计算，不包含 `span_id`/`parent_span_id`。
- v2 Journal digest 包含 `span_id`/`parent_span_id`。
- SQLite 启动时只为旧表增加 nullable span 列，不改写 append-only 行。
- 测试手工创建了不含 span 列的真实 v1/Day 19 结构数据库；读取后旧记录保持 `span_id=null` 并通过旧摘要校验。
- 同一旧 Event 的无 span 重复仍识别为 Duplicate；同一 Event ID 带新 span 身份时返回 `EVENT_ID_CONFLICT`。
- `read_after`、`get_by_event_id`、`last_sequence` 与 terminal invariant 均保持兼容。

### 8. Dropped Span 指标与安全快照

Day 20 Metric Descriptor 已注册：

```text
runtime_trace_dropped_spans_total{component,reason}
```

固定 reason：

```text
recorder_start_failed
recorder_end_failed
adapter_failed
flush_failed
queue_full
```

- start/end 检测每次只增加一次；Handle first-end-wins，重复结束不重复计数。
- Metrics Recorder 失败不会影响业务；`SpanRecorderHealth` 的本地计数仍保留。
- Trace health 不生成 RuntimeEvent，避免 Trace → Metrics → Event 循环。
- 普通日志不暴露 active span ID 列表。

安全快照字段：

```text
active_span_count
completed_span_count
dropped_span_count
```

成功、取消、超时、TaskGroup sibling failure 和 detached worker timeout 测试均验证 `active_span_count == 0`；Recorder close 也会收口 active handle。

### 9. 测试结果

本地 Windows 真实路径执行结果：

```text
目标 pytest:
180 passed, 16 subtests passed

全仓 pytest:
523 passed, 42 subtests passed

compileall:
通过

uv lock --check:
通过（Resolved 157 packages）

git diff --check:
通过
```

### 10. 完成清单

- Model Invocation span：完成。
- Model Attempt span：完成。
- Retry/Fallback hierarchy：完成。
- Tool Invocation span：完成。
- Tool Attempt span：完成。
- Idempotency Replay hierarchy：完成，复用 Invocation、Attempt 为兄弟节点。
- Retrieval span：完成。
- Retrieval Stage span：完成，仅真实执行 Stage。
- Query Rewrite Model parent：Query Rewrite Stage。
- Run/Step/Model/Tool/Retrieval event span：完成并验证。
- Journal schema version：v2。
- Old journal digest：v1 旧字段。
- New journal digest：v2 含 span 身份。
- Old SQLite compatibility：完成，使用手工 v1 fixture。
- Dropped span metric：完成。
- Active span after run/cancel/timeout：均为 0。
- ContextVar cleanup：完成。
- Windows wheel strategy：按用户指示不改动、不纳入本次修复。
- 需要人工确认的问题：无。
