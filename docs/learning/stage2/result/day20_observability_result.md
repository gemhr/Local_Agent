# 阶段二第 20 天改造结果

## 1. 本次目标

本次在既有强类型 `RuntimeEvent`、Journal-first Transport 和幂等 Consumer
之上增加结构化日志、进程内 Metrics、基础设施指标 Hook、真实 Gauge 快照与
有界 Observability Dispatcher。没有创建第二套业务事实来源，没有从单消费者
`RuntimeEventChannel` 争抢事件，也没有实现 Trace Span、Prometheus 或自动
Journal Replay。

最终链路为：

```text
RuntimeEvent
→ RunEventJournal.append
→ RuntimeObservabilityDispatcher.try_submit(JournalRecord)
→ RuntimeEventChannel enqueue
```

Dispatcher 内部固定为两个独立 sink：

```text
JournalRecord
├── StructuredLogProjector
│   └── runtime_structured_logger_v1 checkpoint
└── RuntimeMetricsProjector
    └── runtime_metrics_projector_v1 checkpoint
```

## 2. 修改前 Logging / Metrics

审计到的修改前日志入口如下：

- `core/runtime/agent_loop.py` 使用标准 `logging`：
  - 多处把完整 `AgentState.to_dict()` 放入 `extra`。其中可能含
    `final_output`、Step result/error 等业务正文，属于高风险正文入口。
  - `logger.exception("Agent loop execution failed")` 会输出原始异常堆栈，
    异常消息可能携带 Prompt、Provider 响应或本地细节。
- `server.py` 使用标准 `logging`：
  - KB 初始化日志直接记录 Chroma 路径、Embedding Model 路径和原始异常；
  - Model close 使用 `exc_info=True`；
  - Shutdown 日志连接输出活跃 `run_id`。
  这些均属于 Legacy/基础设施日志，不是本次新增的 Runtime Structured Log。
- `core/agent_router.py` 使用三个 Summary `print()`，内容为 agent 标识和计数，
  当前未直接打印消息正文，但仍是非结构化 Legacy 日志。
- `core/llm_engine.py` 使用加载进度 `print()`，包含模型文件 basename。
- Model/Tool/Retrieval Runtime 本身没有统一的结构化日志 sink。

修改前不存在 Metrics recorder、descriptor、counter、gauge 或 histogram。
`RuntimeEventChannel` 明确限制为单 Consumer；对同一 Channel 第二次
`__aiter__()` 会失败。因此 Observability 不能调用该 Channel 的 `get()` 或参与
其迭代。

最终安全收尾已经清除上述生产日志风险：

- `agent_loop.py` 不再记录完整 `AgentState.to_dict()`，改为
  `safe_agent_state_summary()`，只包含 run/trace identity、Run 状态、终止原因、
  Step 状态计数、有限 Budget usage 和 terminal 标记；
- Runtime/Server 不再使用 `logger.exception()`、`exc_info=True`、
  `str(exception)` 或 `repr(exception)` 输出异常；
- KB 日志不再输出 Chroma、Embedding Model 或数据库完整路径，只记录固定
  error code、component、phase、status、configured 和 storage type；
- `agent_router.py`、`llm_engine.py` 的生产 `print()` 已改为安全标准日志；
- caplog 回归使用 Prompt、Tool Output、RAG Chunk、Memory、用户路径和 Provider
  异常敏感标记，确认标准日志与 Structured Log 均不包含这些标记。

## 3. Event-driven Observability

Observability 的业务输入只接受已 Journal 化事件的安全表示
`JournalRecord`。`RuntimeEventChannel.publish()` 仍是 sequence Owner，并在
同一 publish 临界区完成：

1. 构造唯一 `RuntimeEvent`；
2. Journal append；
3. 从该事件构造等价、可校验的 `JournalRecord` 并非阻塞提交 Dispatcher；
4. 向原 Runtime EventChannel 入队。

第 3 步失败会被隔离，第 4 步仍继续；第 2 步失败仍按原语义终止发布。这样
Journal 仍是事实来源，Observability 也不会消费 UI/Client 的事件副本。

## 4. Structured Runtime Logger

实现：

- `StructuredRuntimeLogger` Protocol；
- `JsonStructuredRuntimeLogger`；
- `InMemoryStructuredRuntimeLogger`；
- `NoopStructuredRuntimeLogger`；
- `StructuredLogProjector`。

JSON logger 每次只写一行合法 JSON，使用 `allow_nan=False` 并立即 flush。
生产 Runtime 与 Server 日志不调用 `repr(event)`、`asdict(event)`、
`logger.exception()`、`exc_info=True`、`str(exception)` 或
`repr(exception)`。

## 5. Log Schema

不可变 `RuntimeLogRecord` 包含：

- `timestamp`：事件实际产生的 `JournalRecord.emitted_at` UTC 时间；
- `journaled_at`：Journal 接受事件的 UTC 时间；
- `journal_latency_ms`：`journaled_at - timestamp` 的非负毫秒数；
- `level`；
- `run_id`、`trace_id`、`step_id`；
- `component`；
- `event_id`、`event_type`；
- `status`；
- `error_code`；
- `retry_index`；
- `duration_ms`；
- `safe_fields`。

`safe_fields` 在构造后转换为只读 Mapping。身份字段只用于日志关联，不会转为
Metric Label。Replay 使用 Journal 内原始 `emitted_at` 与 `journaled_at`，
不会以 Replay 时间覆盖主时间，因此日志可按 `timestamp` 与 Runtime Event 和
后续 Trace 对齐。

## 6. Log Level Policy

Level 由 `runtime_log_level()` 固定计算，调用方不能临时选择：

- 普通 Started/Completed：`INFO`；
- retry、degraded、partial、timeout、budget：`WARNING`；
- `USER_REQUESTED`、`CLIENT_DISCONNECTED` 取消：`INFO`；
- System/Deadline 等其他取消：`WARNING`；
- `ERROR` 事件：`ERROR`。

取消没有统一提升为 ERROR，也没有混入普通 Failure Counter。

## 7. Sensitive Data Redaction

日志投影只读取 `JournalRecord.safe_payload`，并再次执行日志专用 allowlist。
明确不输出：

- Prompt、Messages、Output Delta 正文；
- Tool args、Tool output；
- RAG Query、Chunk、Citation 正文；
- Memory 正文；
- Secret；
- `safe_message`；
- `query_digest`、`resource_key_digest`；
- invocation/attempt/retrieval 等内部 ID；
- 原始异常及其堆栈。

`OUTPUT_DELTA` 只保留 `text_length`。Journal 中存在的安全摘要或内部 ID 不代表
它们适合日志或 Label，因此 Structured Log 使用更窄投影。

## 8. Metrics Recorder

实现：

- `RuntimeMetricsRecorder` Protocol；
- `InMemoryMetricsRecorder`；
- `NoopMetricsRecorder`；
- `RuntimeMetricsSnapshot`。

Recorder 支持 `increment_counter`、`set_gauge`、
`observe_histogram`、`snapshot`。它拒绝未注册指标、错误 Metric 类型、bool、
NaN、Infinity、负 Counter 增量和负 Histogram 样本。本次没有启动远程
Exporter 或 Prometheus Server。

## 9. Metric Descriptor

`MetricDescriptor` 定义 `name`、`type`、`description`、`unit`、
`allowed_labels`、`required_labels` 和可选 `bounded_values`。
`MetricType` 固定为 COUNTER、GAUGE、HISTOGRAM。

所有 descriptor 集中注册在 `RUNTIME_METRIC_DESCRIPTORS` 和
`DEFAULT_RUNTIME_METRIC_REGISTRY`，Recorder 不接受临时指标名。

## 10. Counter

已注册并投影：

- `runtime_runs_total`：仅 `RUN_COMPLETED`；
- `runtime_runs_started_total`：仅 `RUN_STARTED`；
- `runtime_steps_total`：仅 `STEP_COMPLETED`；
- `runtime_model_attempts_total`：仅 `MODEL_STARTED`；
- `runtime_tool_attempts_total`：仅 `TOOL_STARTED`；
- `runtime_retrievals_total`：仅 `RETRIEVAL_STARTED`；
- `runtime_retries_total`：Model/Tool Started 且 `retry_index > 0`；
- `runtime_budget_exhaustions_total`：Run 级由 `BUDGET_EXHAUSTED`，
  组件级由 Completed outcome；
- `runtime_timeouts_total`：Run 级由 `TIMEOUT`，组件级由 Completed outcome；
- `runtime_cancellations_total`：Run 级由 `CANCELLATION`，组件级由
  Completed outcome；
- `runtime_journal_append_failures_total`；
- `runtime_event_duplicates_total`；
- `runtime_observability_dropped_records_total`。

为使预算和 Deadline 成为明确事实，Runtime Event 增加了安全、
无正文的 `BUDGET_EXHAUSTED` 与 `TIMEOUT` 类型；Coordinator 在既有终态决策
提交后发布它们，之后仍发布唯一 `RUN_COMPLETED`。

Metric Ownership Table：

| 指标事实 | 唯一 Event Owner | component |
|---|---|---|
| Run timeout | `TIMEOUT` | `run` |
| Model/Tool/Retrieval timeout | 各组件 `*_COMPLETED` safe outcome | `model` / `tool` / `retrieval` |
| Run budget exhaustion | `BUDGET_EXHAUSTED` | `run` |
| Component budget exhaustion | 各组件 `*_COMPLETED` safe outcome | 对应组件 |
| Run cancellation | `CANCELLATION` | `run` |
| Model/Tool/Retrieval cancellation | 各组件 `*_COMPLETED` safe outcome | 对应组件 |

方案 B 的约束是：专用 signal 只统计 Run 级事实；非 Run component 的专用
signal 被 Projector 忽略。Completed 只拥有组件级结果，因此 Completed 与专用
Event 同时存在时不会对同一 component 重复计数。

## 11. Gauge

支持：

- `runtime_active_runs`；
- `runtime_active_steps`；
- `runtime_detached_tool_workers`；
- `runtime_detached_retrieval_workers`；
- `runtime_blocking_executor_active`；
- `runtime_blocking_executor_pending`；
- `runtime_event_channel_buffered`；
- `runtime_circuit_breakers_open`。

`ApplicationRuntimeGaugeProvider` 组合真实组件快照。除 Dispatcher 消费后和
关闭前刷新外，`InMemoryMetricsRecorder.snapshot(gauge_provider=...)` 与
`RuntimeMetricsCollector.collect_snapshot()` 会在每次读取时重新采集。Tool 或
Retrieval detached worker 自然结束且没有新 Event 时，下一次 collect 仍会返回
0；RunRegistry、BlockingExecutor、EventChannel 和 Circuit 状态也按读取时事实
返回。Gauge collect 失败被隔离，不影响 Counter/Histogram，也不生成
RuntimeEvent。Gauge 没有 `run_id` 或 `step_id` Label。

## 12. Histogram

支持：

- `runtime_run_duration_seconds`；
- `runtime_step_duration_seconds`；
- `runtime_model_duration_seconds`；
- `runtime_tool_duration_seconds`；
- `runtime_retrieval_duration_seconds`；
- `runtime_retrieval_stage_duration_seconds`；
- `runtime_journal_append_duration_seconds`；
- `runtime_blocking_executor_wait_seconds`。

Run、Step、Model、Tool、Retrieval 和 Retrieval Stage 的 Completed Payload
均携带权威 `duration_ms`。Projector 直接除以 1000 写入 seconds Histogram，
不需要先看到 Started。Started correlation map 已完全移除，因此 Completed
单独 Replay 可以恢复 Histogram，Queue Drop、Completed 丢失或 sink failure
也不会形成高基数 Map 泄漏。日志仍保留便于阅读的 `duration_ms`，不会原样
写入 `_seconds` Histogram。

收尾前已写入的 schema v1 Journal Record 允许缺少新 duration 字段并可继续
校验/消费，但历史记录没有权威时长时不会伪造 Histogram。

## 13. Naming Convention

- 所有指标使用 `runtime_` 前缀；
- Counter 必须以 `_total` 结尾；
- Duration Histogram 必须以 `_seconds` 结尾；
- Gauge 禁止以 `_total` 结尾；
- 名称必须预注册，运行时不能动态创建。

## 14. Label Policy

全局允许：

`component`、`event_type`、`status`、`error_code`、`model_profile`、
`retry_disposition`、`retrieval_stage`、`cancellation_reason`、
`side_effect_state`、`runtime_mode`、`tool_name`。

固定禁止：

`run_id`、`trace_id`、`event_id`、`step_id`、`invocation_id`、
`attempt_id`、`retrieval_id`、`query_digest`、`resource_key_digest`、
`source_id`、`chunk_id`、`citation_id`、`safe_message`、用户输入、
文件名、文件路径和 URL。

每个 descriptor 还有自己的 Label allowlist；Label Value 必须匹配有限安全字符
集且不超过 64 字符。`tool_name` 只接受配置
`LOCAL_AGENT_METRICS_TOOL_NAME_ALLOWLIST` 中的值，其他值统一映射为
`other`。

## 15. Event Projection

`RuntimeMetricsProjector` 是唯一 Event → Counter/Histogram 映射所有者：

- Run/Step Completed 投影终态计数和时长；
- Model/Tool Started 投影 Attempt，retry 只在 Started 统计一次；
- Model/Tool Completed 只投影时长，不重复增加 Attempt；
- Retrieval Started 投影调用计数；
- Retrieval Stage Completed 投影 stage 时长和结果；
- Retrieval Completed 投影总时长和结果；
- Budget、Timeout、Cancellation 各自投影独立 Counter。

Projector 不生成 RuntimeEvent，也不修改 Runtime 状态。

## 16. Infrastructure Metrics

`RuntimeInfrastructureMetricsHook` 是非递归窄接口。Journal Store 用它记录：

- append duration；
- append failure；
- Journal duplicate。

`BoundedBlockingExecutor` 用它记录 admission wait seconds。Dispatcher 用它记录
Queue Full/drop 和消费 duplicate。`runtime_event_duplicates_total` 的精确定义是
“Duplicate detection occurrences”，不是全局唯一重复 Event 数。它使用有限
`component` Label：

- `journal`：Journal append 检测到一次 Duplicate；
- `structured_logger`：Logger Consumer 检测到一次 Duplicate；
- `metrics_projector`：Metrics Consumer 检测到一次 Duplicate。

同一检测点只增加一次；Journal Duplicate 不再提交 Dispatcher；Consumer
Duplicate 不执行 Sink Handler。所有 Hook 调用都捕获自身异常；Hook 不调用
Journal、不生成事件、不触发业务 Retry。

## 17. Idempotent Consumption

两个 sink 分别复用 `IdempotentEventConsumer`。Handler 成功后才写 checkpoint；
同一 event live 提交、手工 replay 或并发重复提交时，每个 sink 的 Handler 最多
执行一次。当前仍保留既有的 handler-success/checkpoint-save 崩溃窗口，因此
这是幂等消费基础，不宣称跨进程 exactly-once。

## 18. Sink Checkpoint

- Logger consumer ID：`runtime_structured_logger_v1`；
- Metrics consumer ID：`runtime_metrics_projector_v1`。

Server 为二者创建独立 `SQLiteEventConsumptionCheckpointStore` 实例，使用同一
数据库 schema 中由 `consumer_id` 隔离的记录。Logger 成功、Metrics 失败时只
保留 Logger checkpoint；反向亦然。不存在一个共享 checkpoint 代表两个 sink。

## 19. Observability Dispatcher

`RuntimeObservabilityDispatcher` 是 application-scoped 固定双 sink
Dispatcher：

- 输入仅接受 `JournalRecord`；
- Queue 容量由 `LOCAL_AGENT_OBSERVABILITY_QUEUE_CAPACITY` 配置，默认 256；
- `try_submit` 使用 `put_nowait`，Queue Full 时立即返回 `False`；
- Queue Full 同时写入本地 `ObservabilityHealth` 和独立基础设施 Counter；
- 每个 sink 分别捕获失败，不因一个失败跳过另一个；
- `flush(timeout)`、`close(timeout)` 有界且幂等；
- Shutdown timeout 默认 5 秒；
- 不提供动态 Topic、Subscriber 或自动 Replay。

## 20. Failure Isolation

Logger、Metrics、Gauge、Hook 或 Dispatcher 失败均不会：

- 修改 RunStatus/StepStatus；
- 触发 Model/Tool/RAG Retry；
- 重新执行 Provider 或业务；
- 回滚已成功的 Journal；
- 阻塞 Runtime 无限等待；
- 把原始异常写入结构化日志；
- 为失败 sink 写 checkpoint。

`ObservabilityHealth` 独立维护 dropped、logger failure、metrics failure、worker
failure 和 duplicate 计数，即使 Metrics recorder 本身失败，最小自健康事实仍
存在。

## 21. Runtime 真实接入

真实接入点为 `ChatService.stream_coordinated_agent_events()` 创建的 per-run
Channel。Server lifespan 创建并持有：

- InMemory Metrics recorder；
- JSON Structured Runtime Logger；
- 两个 SQLite checkpoint store；
- Gauge provider；
- Infrastructure hook；
- Observability Dispatcher；
- 带 hook 的 SQLite Event Journal。

Channel 创建时注入 Dispatcher，生命周期内注册到 Gauge provider，消费结束或
abort 后注销。Runtime Event 家族仍通过原 Emitter → Channel → Journal 链进入，
没有建立独立 Demo 事实链。

## 22. Legacy 边界

默认 `/api/chat` 仍调用 `ChatService.stream_chat()`，属于 Legacy Text Stream；
它没有被伪造为 RuntimeEvent，也没有接入本次 Event-driven Observability。
`[[ORCH]]` 文本协议保持原状。默认 API 的事实边界没有迁移，但其共用生产代码
中的 `print`、完整 AgentState、原始异常和路径日志已经完成安全清理；没有为
Legacy 文本伪造 RuntimeEvent。

## 23. 重点 Bad Case

以下“真实发现”来自修改前仓库；“假设构造”用于回归验证设计边界。

### Bad Case 1：Duplicate Event 使 Counter 增加两次

- 类型：假设构造。
- 触发条件：同一 JournalRecord live + replay 或并发提交。
- 故障表现：Counter 重复增加。
- 根因分析：指标 Handler 没有独立 event checkpoint。
- 修复方案：Metrics 使用独立 IdempotentEventConsumer。
- 回归测试：`test_live_duplicate_and_concurrent_duplicate_are_idempotent_per_sink`。
- 对应知识点：幂等消费、checkpoint-after-success。
- 面试表达：投影可以 at-least-once 输入，但副作用必须按 sink 去重。
- 当前状态：已修复。

### Bad Case 2：run_id 作为 Label

- 类型：假设构造。
- 触发条件：把 Event identity 直接转为 Label。
- 故障表现：时间序列无限增长。
- 根因分析：混淆日志关联字段与 Metric 维度。
- 修复方案：全局 denylist 与 descriptor 二次 allowlist。
- 回归测试：`test_type_number_counter_and_label_validation`。
- 对应知识点：High Cardinality。
- 面试表达：run_id 可进日志，不可进指标 Label。
- 当前状态：已阻止。

### Bad Case 3：Tool Output 写日志

- 类型：假设构造。
- 触发条件：序列化整个 RuntimeEvent 或 Payload。
- 故障表现：Tool/RAG/Memory 正文或 Secret 泄漏。
- 根因分析：缺少日志专用安全投影。
- 修复方案：只读 Journal safe payload，再使用更窄字段 allowlist。
- 回归测试：`test_safe_projection_excludes_output_identifiers_and_raw_content`。
- 对应知识点：Sensitive Data Redaction。
- 面试表达：安全存储投影不等于安全日志投影。
- 当前状态：已阻止。

### Bad Case 4：Metrics 失败导致 Run 失败

- 类型：假设构造。
- 触发条件：Recorder 抛异常。
- 故障表现：业务 Run 或 Event Transport 失败。
- 根因分析：同步把观测结果纳入业务成功条件。
- 修复方案：Channel 与 Dispatcher 两层捕获，两个 sink 独立执行。
- 回归测试：`test_metrics_failure_does_not_rollback_logger_checkpoint`。
- 对应知识点：Failure Isolation。
- 面试表达：Observability 是旁路，不参与业务状态机。
- 当前状态：已隔离。

### Bad Case 5：Gauge 漂移

- 类型：假设构造。
- 触发条件：仅按 Started/Completed 增减；进程崩溃或丢记录。
- 故障表现：active/detached 长期不归零。
- 根因分析：把事实事件累加当作当前状态。
- 修复方案：Gauge 读取真实 Registry/Tracker/Executor/Channel/Circuit 快照。
- 回归测试：`test_gauge_provider_reads_real_component_snapshots_and_close_state`。
- 对应知识点：Gauge ownership。
- 面试表达：能读取 owner 快照时不维护影子计数。
- 当前状态：核心 Gauge 已使用真实快照；仍只代表单进程。

### Bad Case 6：RUN_COMPLETED 重复统计

- 类型：假设构造。
- 触发条件：同一终态记录重放。
- 故障表现：`runtime_runs_total` 增加两次。
- 根因分析：终态唯一性没有延伸到 projection。
- 修复方案：Journal 唯一终态 + Metrics sink event checkpoint。
- 回归测试：Dispatcher duplicate 测试与既有 Journal terminal 测试。
- 对应知识点：终态事实、幂等投影。
- 面试表达：事实唯一与消费幂等需要同时成立。
- 当前状态：已修复。

### Bad Case 7：repr(exception) 泄漏

- 类型：真实发现。
- 触发条件：Legacy `logger.exception()` 或 server 记录原始 exception。
- 故障表现：异常消息、路径、Provider 内容进入日志。
- 根因分析：Legacy 日志未区分安全错误码与原始异常。
- 修复方案：新 Structured Runtime Log 只投影 `safe_error_code`，不接收异常。
- 回归测试：`test_error_safe_message_is_not_written`。
- 对应知识点：Error redaction。
- 面试表达：运行时错误日志只允许稳定错误码和安全维度。
- 当前状态：Structured Log 与现存 Legacy/基础设施生产日志均已修复。

### Bad Case 8：Model Retry 重复计数

- 类型：假设构造。
- 触发条件：Started 与 Completed 都增加 Attempt/Retry。
- 故障表现：一次 retry 被记为两次。
- 根因分析：没有定义 Counter 的唯一事件边界。
- 修复方案：Attempt/Retry 只在 `MODEL_STARTED` 投影。
- 回归测试：`test_event_projection_counts_attempts_retries_terminal_results_and_duration`。
- 对应知识点：Event projection semantics。
- 面试表达：计数器必须绑定唯一事实边界，而非任意生命周期回调。
- 当前状态：已修复。

### Bad Case 9：Logger/Metrics 共享 Checkpoint

- 类型：假设构造。
- 触发条件：第一个 sink 成功即写共同 checkpoint。
- 故障表现：第二个失败 sink 无法 replay。
- 根因分析：把两个独立副作用当作同一消费状态。
- 修复方案：不同 consumer ID、Consumer 和 Store 实例。
- 回归测试：两个 sink failure/checkpoint 独立测试。
- 对应知识点：Sink isolation。
- 面试表达：checkpoint 的粒度必须与副作用边界一致。
- 当前状态：已修复。

### Bad Case 10：Histogram 单位混用

- 类型：假设构造。
- 触发条件：把 `duration_ms` 原值写入 `_seconds`。
- 故障表现：P95/P99 放大 1000 倍。
- 根因分析：schema 单位和指标单位未显式转换。
- 修复方案：Payload milliseconds 除以 1000；其他时长直接使用秒差。
- 回归测试：Counter/Gauge/Histogram 与 Event projection 测试。
- 对应知识点：Metric unit contract。
- 面试表达：单位是指标 schema 的一部分。
- 当前状态：已修复。

### Bad Case 11：Cancellation 统计为普通失败

- 类型：假设构造。
- 触发条件：所有非成功事件统一进入 failure counter/ERROR log。
- 故障表现：用户主动取消被误报为系统故障。
- 根因分析：忽略取消原因和控制流语义。
- 修复方案：独立 cancellation counter，按 user/system 原因映射日志级别。
- 回归测试：`test_fixed_log_level_policy`、`test_budget_and_cancellation_are_separate_metrics`。
- 对应知识点：Cancellation semantics。
- 面试表达：取消是控制流事实，不天然等于错误。
- 当前状态：已修复。

### Bad Case 12：Observability 争抢 EventChannel

- 类型：真实架构风险。
- 触发条件：Observability 对单 Consumer Channel 再次迭代。
- 故障表现：UI 丢事件或第二个 Consumer 直接失败。
- 根因分析：错误地把队列当广播总线。
- 修复方案：Journal append 后向独立有界 Dispatcher 提交 JournalRecord。
- 回归测试：`test_journal_first_dispatch_then_ui_channel_without_competition`。
- 对应知识点：单消费队列、旁路投影。
- 面试表达：不为观测创建 Event Bus，也不消费 UI 队列；从 Journal 安全记录分流。
- 当前状态：已修复。

## 24. 测试结果

目标 Observability + Journal/Event/Tool/Retrieval 命令：

```text
130 passed, 4 subtests passed in 5.29s
```

全仓：

```text
507 passed, 42 subtests passed in 7.11s
```

新增测试覆盖：

- 日志 identity、UTC、JSON line、安全投影、无正文/原始异常、取消级别；
- descriptor、命名、类型、数值、Label、tool allowlist、三类 Metric；
- Counter/Histogram Event projection 与 seconds 转换；
- live/replay/concurrent duplicate、独立 checkpoint、handler failure；
- Queue Full、flush timeout、close 幂等与本地 health；
- Journal-first、无 Channel 竞争、真实 Gauge、Hook failure isolation。
- Completed-only Replay、五类权威 duration、无 Started Map；
- 读取时 Gauge refresh、Tool/Retrieval worker 无事件自然归零；
- Timeout/Budget/Cancellation 唯一 Owner 与 component/status Label；
- Journal/Logger/Metrics 三个 Duplicate detection component；
- caplog 与 Structured Log 敏感标记泄漏回归。

静态与锁文件校验：

```text
compileall: passed
uv lock --check: passed (157 packages)
git diff --check: passed
```

## 25. 未完成事项与风险

- 没有真实 Prometheus Server 或 Exporter；
- 没有 Trace Span，只有 `trace_id` 日志关联；
- 没有自动 Journal Replay；
- Gauge 仅代表当前单进程；
- 默认 Legacy `/api/chat` 尚未接入；
- Queue Full 可能丢弃实时 Observability；
- 已成功的 Journal 仍可供未来手工/自动补处理；
- Observability checkpoint 与业务状态不是同一事务；
- 收尾前已持久化且缺少 `duration_ms` 的旧 Completed Record 无法恢复历史
  Histogram；新 Completed Record 可独立 Replay；
- InMemory Metrics 重启丢失，尚无持久 Exporter。

## 26. 面试表达

我没有把 Logging 和 Metrics 放到每个 Model/Tool 调用点各写一遍，而是复用
Journal-first Runtime Event 作为唯一业务事实。事件先持久化，再把安全
JournalRecord 非阻塞分流到固定双 sink；Logger 和 Metrics 各自用 event ID
checkpoint 幂等消费，所以任一 sink 失败不会遮蔽另一方，也不会触发业务重试。
Counter 绑定唯一生命周期事实，Histogram 统一 seconds，Label 采用全局 denylist
加 descriptor allowlist。Gauge 优先读取真实组件 owner 快照。Queue Full 或
Exporter 失败只影响实时观测，并由独立本地 health 计数记录，不改变 Runtime
状态。

## 27. 需要带回 ChatGPT 审查的信息

- 审查默认 Tool allowlist 为空、所有 Tool 映射 `other` 是否符合生产维度需求；
- 审查第 23 天默认 API 迁移，但不再需要等待迁移才修复 Legacy 日志；
- 审查 InMemory Metrics 的导出接口与未来 Prometheus adapter 边界；
- 审查 shutdown 超时时是丢弃剩余实时队列还是另行保存补处理标记。
