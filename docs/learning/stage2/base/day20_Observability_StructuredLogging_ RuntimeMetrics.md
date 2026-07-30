本日计划**按原样执行，并补充两项工程约束**：

1. Logger（日志记录器）和 Metrics Recorder（指标记录器）分别使用独立的幂等消费 Checkpoint（检查点），避免一个 Sink 成功、另一个失败时出现重复或丢失。
2. Counter（计数器）和 Histogram（直方图）主要由 Runtime Event（运行时事件）投影；Gauge（仪表）优先读取 Runtime 组件实时状态，避免因事件丢弃导致数值永久漂移。

# 阶段二第 20 天：Observability、Structured Logging 与 Runtime Metrics

**当前进度：第 20/25 天。**

核心目标：

> 将 RuntimeEvent 投影成安全、可关联、可去重的日志和指标，但不让可观测性系统成为新的状态所有者或业务失败来源。

目标结构：

```text
RuntimeEvent
→ Event Journal
→ RuntimeObservabilityDispatcher
   ├── StructuredLogProjector
   │   └── Idempotent Consumer / Logger Checkpoint
   └── MetricsProjector
       └── Idempotent Consumer / Metrics Checkpoint
```

实时投递和可观测性之间不能争抢同一个单消费者 EventChannel：

```text
                     ┌→ Client EventChannel
Journaled Event ─────┤
                     └→ Observability Dispatcher
```

------

# 一、Observability 三大支柱

## 1. Logging

Logging（日志）回答：

> 某次运行具体发生了什么？

适合保存：

- Run、Step、Attempt 身份；
  -事件类型；
  -状态；
  -安全错误码；
  -时间和耗时；
  -有限、可控的执行元数据。

日志面向故障排查，但不能保存业务正文和 Secret（密钥）。

------

## 2. Metrics

Metrics（指标）回答：

> 系统整体表现如何，问题出现得多不多？

例如：

```text
过去 5 分钟取消率是多少？
Model Attempt 的 p95 延迟是多少？
当前有多少 Detached Worker？
Budget Exhausted 出现了多少次？
```

指标适合聚合，不适合保存单次请求身份。

------

## 3. Trace

Trace（链路追踪）回答：

> 一次请求内部，各组件之间是怎样调用和耗时的？

第 20 天只保留 `trace_id` 关联字段，不创建 Span（跨度）。完整 Trace 在第 21 天实现。

------

# 二、为什么使用 Event-driven Observability

当前已经存在：

```text
Run / Step / Model / Tool / Retrieval
→ RuntimeEvent
→ Journal
```

如果再在每个组件里直接写：

```python
metrics.inc(...)
logger.info(...)
```

会形成两套统计逻辑：

```text
Runtime Event 记录 Model Attempt=2
Model 组件内部 Metrics 记录 Attempt=3
```

因此今天采用：

```text
业务组件
→ 只负责产生真实 RuntimeEvent

Observability Projector
→ 根据 Event 映射日志和 Counter/Histogram
```

## 允许的例外

有些信息无法从成功发布的 Event 得到：

- Journal Append 失败；
- Journal Duplicate；
- EventChannel 当前队列长度；
- Blocking Executor 当前活动数；
- Detached Worker 数；
- Circuit Breaker 当前 OPEN 数。

这些属于 Runtime Infrastructure（运行时基础设施）实时状态，可以通过受控的：

```text
RuntimeGaugeProvider
RuntimeInfrastructureMetricsHook
```

采集。

这不是第二套业务事实来源，因为它们描述的是基础设施本身，而不是业务操作结果。

------

# 三、Structured Runtime Logger

## 1. 日志记录结构

建议建立不可变结构：

```python
@dataclass(frozen=True, slots=True)
class RuntimeLogRecord:
    timestamp: datetime
    level: RuntimeLogLevel
    run_id: str
    trace_id: str
    step_id: str | None
    component: str
    event_id: str
    event_type: str
    status: str | None
    error_code: str | None
    retry_index: int | None
    duration_ms: int | None
    fields: Mapping[str, JsonValue]
```

## 2. 必须字段

```text
timestamp
level
run_id
trace_id
step_id
component
event_id
event_type
status
error_code
retry_index
duration_ms
```

字段不存在时使用 `null`，不要：

-伪造空字符串；
-使用 `"unknown user query"`；
-将正文塞进 `fields`。

## 3. 允许的扩展字段

```text
model_profile
candidate_index
tool_name
retrieval_stage
cancellation_reason
side_effect_state
retry_disposition
budget_dimension
worker_terminated
execution_detached
degraded
citation_count
```

所有值必须来自有限枚举或受控名称。

## 4. 日志级别映射

建议：

| Event                      | Level                           |
| -------------------------- | ------------------------------- |
| Started /正常 Completed    | `INFO`                          |
| Retry / Degraded / Partial | `WARNING`                       |
| Cancelled                  | `INFO` 或 `WARNING`，按原因固定 |
| Timeout                    | `WARNING`                       |
| Budget Exhausted           | `WARNING`                       |
| Runtime Internal Error     | `ERROR`                         |
| Journal Corruption         | `ERROR`                         |
| Safety Violation           | `ERROR`                         |

Cancellation 不能全部标为 Error。

例如用户主动停止：

```text
reason = USER
level = INFO
```

系统 Shutdown：

```text
reason = SYSTEM
level = WARNING
```

------

# 四、安全日志投影

Logger 不能直接执行：

```python
logger.info("%r", runtime_event)
logger.info(asdict(runtime_event))
logger.exception(...)
```

必须从：

```text
JournalRecord.safe_payload
```

或 Runtime Event 的专用安全投影生成日志。

## 禁止字段

- 用户问题正文；
- System Prompt；
- Model Messages；
  -模型完整回复；
  -Tool arguments；
  -Tool output；
  -Idempotency Key 正文；
  -Resource Key 正文；
  -RAG Query；
  -RAG Chunk；
  -canonical path；
  -Memory Summary；
  -历史消息；
  -Provider URL；
  -API Key；
  -本地敏感路径；
  -原始 Exception；
  -Traceback。

## Error 日志

只记录：

```text
safe_error_code
safe_message
component
phase
fatal
```

不记录：

```text
repr(exception)
str(exception)
traceback.format_exc()
```

底层原始异常可以在开发调试模式受控处理，但不能进入生产 Runtime Logger。

------

# 五、Logger Contract

建议：

```python
class StructuredRuntimeLogger(Protocol):
    async def write(self, record: RuntimeLogRecord) -> None:
        ...

    async def flush(self) -> None:
        ...

    async def close(self) -> None:
        ...
```

实现：

```text
JsonStructuredRuntimeLogger
InMemoryStructuredRuntimeLogger
NoopStructuredRuntimeLogger
FailingStructuredRuntimeLogger（测试）
```

`JsonStructuredRuntimeLogger` 每行输出一个合法 JSON Object。

禁止拼接为：

```text
run=... user said=...
```

------

# 六、Metrics 类型

## 1. Counter

Counter 只能递增，适合累计次数：

```text
runtime_runs_total
runtime_steps_total
runtime_model_attempts_total
runtime_tool_attempts_total
runtime_retrievals_total
runtime_retries_total
runtime_budget_exhaustions_total
runtime_timeouts_total
runtime_cancellations_total
runtime_journal_append_failures_total
runtime_event_duplicates_total
runtime_observability_dropped_records_total
```

不能用 Counter 表示当前活跃 Run 数。

------

## 2. Gauge

Gauge 可以增加、减少或直接设置，表示当前状态：

```text
runtime_active_runs
runtime_active_steps
runtime_detached_tool_workers
runtime_detached_retrieval_workers
runtime_blocking_executor_active
runtime_blocking_executor_pending
runtime_event_channel_buffered
runtime_circuit_breakers_open
```

## Gauge 的优先数据来源

Gauge 优先从实际 Runtime Component 获取 Snapshot：

```text
RunRegistry
Tool Worker Tracker
BoundedBlockingExecutor
EventChannel
Circuit Breaker Registry
```

而不是仅根据：

```text
RUN_STARTED +1
RUN_COMPLETED -1
```

原因是：

```text
RUN_COMPLETED Event 投影失败
→ Gauge 永久多 1
```

可以保留事件投影的活跃集合用于测试，但生产 Gauge Snapshot 应尽可能读取实际状态。

------

## 3. Histogram

Histogram 记录样本分布：

```text
runtime_run_duration_seconds
runtime_step_duration_seconds
runtime_model_duration_seconds
runtime_tool_duration_seconds
runtime_retrieval_duration_seconds
runtime_journal_append_duration_seconds
runtime_blocking_executor_wait_seconds
```

所有 Histogram 的内部单位统一使用：

```text
seconds
```

日志仍可显示：

```text
duration_ms
```

不能同一个 Histogram 有时写毫秒、有时写秒。

------

# 七、Metrics Contract

建议：

```python
class RuntimeMetricsRecorder(Protocol):
    def increment_counter(
        self,
        name: str,
        value: int = 1,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        ...

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        ...

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        ...

    def snapshot(self) -> RuntimeMetricsSnapshot:
        ...
```

实现：

```text
InMemoryMetricsRecorder
NoopMetricsRecorder
FailingMetricsRecorder（测试）
```

本日不接入真实 Prometheus Server 或 Pushgateway。

------

# 八、Metric Descriptor 与命名规范

建议集中声明：

```python
@dataclass(frozen=True, slots=True)
class MetricDescriptor:
    name: str
    metric_type: MetricType
    description: str
    unit: str | None
    allowed_label_names: tuple[str, ...]
```

禁止调用方临时构造任意指标名称。

## 命名规则

### Counter

必须以：

```text
_total
```

结尾。

### Duration Histogram

必须以：

```text
_seconds
```

结尾。

### Gauge

名称表达当前状态，不使用 `_total`。

### 统一前缀

继续使用：

```text
runtime_
```

避免同一项目出现：

```text
agent_runtime_x
local_agent_x
runtime_x
```

三套命名。

------

# 九、高基数 Label Policy

## 1. 明确禁止

以下字段即使存在于日志中，也不能作为 Metrics Label：

```text
run_id
trace_id
event_id
step_id
invocation_id
attempt_id
retrieval_id
query_digest
resource_key_digest
source_id
chunk_id
citation_id
用户输入
文件名
文件路径
URL
safe_message
```

错误消息也不能作为 Label，因为文本组合数量不可控。

------

## 2. 允许 Label

必须来自受控集合：

```text
component
event_type
status
error_code
model_profile
retry_disposition
retrieval_stage
cancellation_reason
side_effect_state
runtime_mode
```

`error_code` 必须来自固定 Error Enum，不能直接使用异常消息。

## 3. Tool Name

`tool_name` 只有满足以下条件才允许：

- 已注册 Tool 数量有限；
  -名称经过规范化；
  -不存在用户动态 Tool Name；
  -不存在完整 MCP Server/Function 路径；
- Label Policy 显式 Allowlist。

当前只迁移少量受控 Tool，可以支持有限 Allowlist；未知 Tool 统一映射为：

```text
other
```

------

# 十、Runtime Event 到 Metrics 的映射

## Run

### `RUN_STARTED`

- 可选增加 `runtime_runs_started_total`
- 不直接增加 `runtime_runs_total`
- 实时 Gauge 由 RunRegistry Snapshot 提供

### `RUN_COMPLETED`

```text
runtime_runs_total{
    status,
    stop_reason
} += 1
```

同时：

```text
runtime_run_duration_seconds.observe(duration)
```

每个 Run 只在唯一 Terminal Event 上统计一次结果。

------

## Step

### `STEP_COMPLETED`

```text
runtime_steps_total{
    status,
    component
} += 1
```

并记录：

```text
runtime_step_duration_seconds
```

不要在 Started 和 Completed 都增加 `runtime_steps_total`。

------

## Model

### `MODEL_STARTED`

```text
runtime_model_attempts_total{
    model_profile,
    retry="true|false"
} += 1
```

如果：

```text
retry_index > 0
```

则：

```text
runtime_retries_total{component="model"} += 1
```

### `MODEL_COMPLETED`

只记录：

- duration histogram；
- status / error outcome；
- timeout/budget/cancel 分类。

不能再次增加 Attempt Counter。

------

## Tool

### `TOOL_STARTED`

```text
runtime_tool_attempts_total{
    tool_name,
    retry_disposition
} += 1
```

Retry 仍在 Attempt Started 时计数。

### `TOOL_COMPLETED`

记录：

- Tool Duration；
- Timeout；
  -Cancellation；
  -Side Effect State；
  -Detached Worker 结果。

------

## Retrieval

### `RETRIEVAL_STARTED`

```text
runtime_retrievals_total += 1
```

### `RETRIEVAL_STAGE_COMPLETED`

记录：

```text
retrieval_stage
status
degraded
duration
```

### `RETRIEVAL_COMPLETED`

记录：

- Retrieval 总耗时；
  -最终状态；
  -Citation 数量只能作为 Histogram/Sample，不作为高基数 Label；
  -EMPTY、FAILED、DEGRADED 分开。

------

## Budget

如果 Event 的安全错误码为：

```text
BUDGET_EXHAUSTED
```

增加：

```text
runtime_budget_exhaustions_total{
    component,
    budget_dimension
}
```

同一 Event 只统计一次。

------

## Timeout 与 Cancellation

Timeout：

```text
runtime_timeouts_total{
    component,
    phase
}
```

Cancellation：

```text
runtime_cancellations_total{
    component,
    cancellation_reason
}
```

Cancellation 不额外统计成普通 Error，除非存在独立的 Infrastructure Error。

------

# 十一、Journal 指标的特殊边界

`runtime_journal_append_failures_total` 无法由成功写入 Journal 的 Event 产生，因为：

```text
Journal Append 失败
→ Event 没有进入 Journal
```

因此它必须由 EventChannel / Journal 边界的窄接口记录：

```text
RuntimeInfrastructureMetricsHook.on_journal_append_failed()
```

同理：

```text
runtime_event_duplicates_total
```

可以根据：

```text
JournalAppendStatus.DUPLICATE
```

直接记录。

这些 Hook：

-不能修改业务状态；
-不能抛出导致第二次 Journal Append；
-不能递归生成新的 RuntimeEvent；
-不能造成 Metrics → Event → Metrics 循环。

------

# 十二、幂等 Observability Consumer

## 为什么 Logger 和 Metrics 分开 Checkpoint

场景：

```text
Logger 成功
→ Metrics 失败
```

如果两者共享同一个 Checkpoint：

### 成功后写 Checkpoint

Metrics 失败时不写：

```text
Replay
→ Logger 再写一次
```

### Logger 成功后立即写 Checkpoint

```text
Metrics 永远无法补处理
```

因此需要独立 Consumer ID：

```text
runtime_structured_logger_v1
runtime_metrics_projector_v1
```

每个 Sink 分别使用：

```text
IdempotentEventConsumer
EventConsumptionCheckpointStore
```

## 消费语义

```text
检查 Sink Checkpoint
→ 执行 Sink Projector
→ Sink 成功
→ 写 Sink Checkpoint
```

一个 Sink 失败不影响另一个 Sink。

------

# 十三、Observability Dispatcher

第 16 天 EventChannel 是单消费者 Channel，不能让 UI 和 Observability 同时从同一队列 `get()`。

建议建立应用级：

```text
RuntimeObservabilityDispatcher
```

## 工作方式

```text
Journal Append 成功
→ Dispatcher.try_submit(JournalRecord)
→ EventChannel enqueue RuntimeEvent
```

`try_submit()` 使用有界 Queue，不无限阻塞 Runtime Publisher。

建议配置：

```text
OBSERVABILITY_QUEUE_CAPACITY
OBSERVABILITY_WORKERS
OBSERVABILITY_FLUSH_TIMEOUT
```

## Queue 满

Queue 满时：

-不阻塞业务无限等待；
-不修改 RunStatus；
-不重新执行事件；
-增加内部 `dropped_records`；
-记录最小安全告警；
-后续可通过 Journal 补处理，但本日不实现自动 Dispatcher Replay。

## 不创建 Event Bus

Dispatcher 只负责：

-安全 JournalRecord；
-固定 Logger Sink；
-固定 Metrics Sink。

不实现：

-动态订阅；
-Topic；
-远程 Producer；
-Kafka；
-通用消息总线。

------

# 十四、Observability 故障隔离

## Logger 失败

```text
Logger.write()
→ 抛异常
```

处理：

-不改变 Runtime Result；
-不修改 Event Journal；
-不向业务抛出原始异常；
-不写 Logger Checkpoint；
-增加 Dropped Logger Record；
-允许未来 Journal Replay 再处理。

## Metrics 失败

同样：

-不改变业务结果；
-不重新处理 Model/Tool/RAG；
-不修改 AgentState；
-不写 Metrics Checkpoint；
-不影响 Logger Sink。

## Dispatcher Worker 崩溃

必须：

-捕获安全异常；
-保持 Worker 循环或显式重启；
-不能静默永久停止；
-关闭时有界 Flush；
-无法 Flush 的数量计入 Dropped Records。

------

# 十五、Gauge Provider

建议：

```python
class RuntimeGaugeProvider(Protocol):
    def collect(self) -> Mapping[str, GaugeSample]:
        ...
```

提供：

```text
RunRegistryGaugeProvider
ToolWorkerGaugeProvider
RetrievalExecutorGaugeProvider
EventChannelGaugeProvider
CircuitBreakerGaugeProvider
```

## Gauge Snapshot

Gauge Snapshot 不需要 Event ID 去重，因为它表示当前值。

例如：

```text
runtime_detached_tool_workers = tracker.detached_count
```

而不是：

```text
TOOL_TIMEOUT +1
WORKER_FINISHED -1
```

后者容易因事件投影失败漂移。

------

# 十六、可观测性安全不变量

1. Logger 和 Metrics 只消费安全 JournalRecord。
2. 不直接访问 Prompt、Tool 参数或 RAG 正文。
3. Metrics Label 必须通过 Descriptor 校验。
4. 未知 Label 拒绝。
5. 缺失 Label 可按 Descriptor 使用固定 `"unknown"`，但不能使用正文补齐。
6. Counter 不能减少。
7. Histogram 只接受有限非负数。
8. Gauge 必须接受有限数，拒绝 NaN/Infinity。
9. Logger/Metrics 失败不影响 Runtime 状态。
10. Event Duplicate 不重复投影。
11. Observability 不生成新的业务 Runtime Event。
12. Observability 不成为 Run、Step 或 Journal 生命周期 Owner。

------

# 十七、第 20 天重点 Bad Case

## Bad Case 1：重复 Event 使 Counter 增加两次

- **类型：真实分布式风险**
- 触发：实时消费后又从 Journal 重投。
- 修复：Sink 独立 Event ID Checkpoint。

## Bad Case 2：run_id 作为 Label

- **类型：真实运维风险**
- 表现：每个 Run 创建一组新时间序列。
- 修复：run_id 只进入日志和 Trace。

## Bad Case 3：Tool Output 写入结构化日志

- **类型：真实安全风险**
- 修复：只消费 Journal 安全投影。

## Bad Case 4：Metrics Exporter 失败导致 Run FAILED

- **类型：架构风险**
- 修复：Observability Failure Isolation。

## Bad Case 5：Gauge 事件投影漂移

- **类型：假设构造**
- 触发：Started 投影成功，Completed 投影丢失。
- 修复：Gauge 优先读取组件 Snapshot。

## Bad Case 6：RUN_COMPLETED Duplicate 重复统计 Run

- **类型：假设构造**
- 修复：Metrics Consumer 幂等 Checkpoint。

## Bad Case 7：Logger 使用 `repr(exception)`

- **类型：真实安全风险**
- 修复：只记录 safe error code/message。

## Bad Case 8：Model Retry 重复计数

- **类型：假设构造**
- 触发：Started 与 Completed 都增加 Attempt Counter。
- 修复：Attempt 只在 Started 统计一次。

## Bad Case 9：Logger 和 Metrics 共用 Checkpoint

- **类型：假设构造**
- 表现：一个 Sink 失败导致重复或永久丢失。
- 修复：每个 Sink 独立 Consumer ID。

## Bad Case 10：Histogram 单位混用

- **类型：真实数据质量风险**
- 修复：指标统一 seconds，日志使用 milliseconds。

## Bad Case 11：Cancellation 计入普通 Failure

- **类型：语义错误**
- 修复：Cancellation 单独统计，Reason 使用有限枚举。

## Bad Case 12：Observability 争抢 EventChannel

- **类型：真实架构风险**
- 表现：UI 或 Logger 只有一方收到 Event。
- 修复：Journal 后独立 Dispatcher，不调用同一 Channel `get()`。

------

# 十八、测试方案

## Structured Logging

1. 必须字段完整；
2. UTC 时间；
   3.日志级别映射；
3. Run/Trace/Event 身份一致；
4. Model 安全字段；
5. Tool 安全字段；
6. Retrieval 安全字段；
7. Cancellation 不错误升级；
   9.无 Prompt；
   10.无 Tool Output；
   11.无 RAG Chunk；
   12.无 Memory；
   13.无 Secret；
   14.无 Traceback；
   15.JSON 每行可解析。

## Metric Descriptor

1. Counter 名称；
2. Gauge 名称；
3. Histogram `_seconds`；
   19.未知指标拒绝；
   20.错误类型调用拒绝；
   21.未允许 Label 拒绝；
   22.缺失必需 Label；
   23.NaN/Infinity 拒绝；
   24.Counter 负数拒绝。

## Label Policy

1. run_id 拒绝；
   26.trace_id 拒绝；
   27.event_id 拒绝；
   28.step_id 拒绝；
   29.query digest 拒绝；
   30.resource digest 拒绝；
   31.safe message 拒绝；
   32.有限 tool name；
   33.未知 tool 映射 `other`。

## Event Projection

34.RUN_COMPLETED；
35.STEP_COMPLETED；
36.MODEL_STARTED；
37.MODEL_COMPLETED；
38.Tool Started/Completed；
39.Retrieval Started/Stage/Completed；
40.Retry；
41.Budget Exhausted；
42.Timeout；
43.Cancellation；
44.Degraded；
45.Partial。

## Idempotency

46.Logger Duplicate skip；
47.Metrics Duplicate skip；
48.实时 + Journal Replay；
49.并发 Duplicate；
50.不同 Sink 独立；
51.Logger 成功/Metrics 失败；
52.Metrics 成功/Logger 失败；
53.Handler 失败无 Checkpoint；
54.成功后 Checkpoint。

## Gauge

55.RunRegistry Snapshot；
56.Active Step Snapshot；
57.Detached Tool；
58.Detached Retrieval；
59.Executor active/pending；
60.Channel buffered；
61.Circuit OPEN；
62.Snapshot 值不使用 ID Label；
63.组件关闭后 Gauge 归零。

## Failure Isolation

64.Logger 抛异常；
65.Metrics 抛异常；
66.Dispatcher Queue Full；
67.Dispatcher Worker Error；
68.Flush Timeout；
69.Close 幂等；
70.业务 Run 仍成功；
71.不重新执行 Model；
72.不重新执行 Tool；
73.不重新执行 RAG；
74.Dropped Record 计数。

## Journal / Channel

75.Journal 成功后提交 Observability；
76.Journal 失败时不提交；
77.Channel Failure 后 Observability 仍基于 Journaled Event；
78.Observability 不消费 UI Queue；
79.不生成新 RuntimeEvent；
80.无递归指标事件。

## 全仓回归

81.Event Journal；
82.Runtime Event；
83.RunCoordinator；
84.Model；
85.Tool；
86.Retrieval；
87.Budget；
88.Cancellation；
89.full pytest；
90.compileall；
91.lock check；
92.diff check。

------

# 十九、Codex 实操提示词

你正在继续改造 LocalAgent 项目的阶段二 Runtime。

## 一、本日目标

第 20 天主题：

- Observability
- Structured Logging
- Event-driven Metrics
- Counter / Gauge / Histogram
- Idempotent Observability Consumption
- High Cardinality
- Label Policy
- Sensitive Data Redaction
- Observability Failure Isolation

第 16～19 天已经完成：

- 强类型 RuntimeEvent
- per-run Event Sequence
- EventChannel
- Run/Step/Model/Tool/Retrieval Event
- SQLite Event Journal
- Journal-first Transport
- IdempotentEventConsumer
- EventConsumptionCheckpointStore

本日必须复用这些能力，不创建第二套业务事实来源，也不能让 Observability 从单消费者 EventChannel 中与 UI 争抢事件。

目标结构：

```text
RuntimeEvent
→ Journal
├── Runtime EventChannel / Client
└── RuntimeObservabilityDispatcher
    ├── StructuredLogProjector
    └── MetricsProjector
```

本次不实现 Trace Span；Trace 只保留 `trace_id` 关联，第 21 天再处理。

## 二、结果文档

创建：

```text
docs/learning/stage2/result/day20_observability_result.md
```

## 三、固定工作流

严格执行：

1. 检查当前 Runtime Event、Journal、Channel 与 Consumer；
2. 检查现有 logging、print、异常日志和 Metrics 代码；
3. 列出所有可能泄漏 Prompt/Tool/RAG/Memory 的日志位置；
4. 设计日志安全投影；
5. 设计 Metric Descriptor、命名和 Label Policy；
6. 设计 Event -> Counter/Histogram 映射；
7. 设计 Gauge Snapshot Provider；
8. 设计 Sink 独立幂等消费；
9. 建立有界 Observability Dispatcher；
10. 接入 Coordinated Runtime；
11. 补充故障隔离、安全和回归测试。

不得跳过真实事件链检查直接创建独立 Demo。

## 四、必须检查

至少检查：

- `core/runtime/events.py`
- `core/runtime/event_channel.py`
- `core/runtime/event_journal.py`
- `core/runtime/event_journal_store.py`
- `core/runtime/event_consumer.py`
- `core/runtime/run_coordinator.py`
- `core/runtime/model_invocation.py`
- `core/runtime/tool_execution.py`
- `core/runtime/retrieval_execution.py`
- `core/runtime/tool_concurrency.py`
- `core/runtime/retrieval_context.py`
- `core/runtime/budget.py`
- `core/chat_service.py`
- `server.py`
- `settings.py`
- 现有 logging 配置和所有 `print/logger/exception` 调用
- 第 16～19 天结果文档及测试

结果文档必须列出：

- 当前日志入口；
  -哪些日志包含高风险正文；
  -当前是否存在 Metrics；
  -EventChannel 是否单消费者；
  -JournalRecord 安全字段；
  -Observability 接入位置；
  -Gauge 的实际数据源；
  -Legacy 路径是否接入。

## 五、建议新增文件

建议新增或提供等价结构：

```text
core/runtime/observability.py
core/runtime/structured_logging.py
core/runtime/metrics.py
core/runtime/observability_dispatcher.py

tests/test_structured_runtime_logging.py
tests/test_runtime_metrics.py
tests/test_observability_dispatcher.py
tests/test_observability_integration.py
```

根据真实结构修改：

```text
core/runtime/event_channel.py
core/runtime/event_journal.py
core/runtime/event_consumer.py
core/runtime/__init__.py
core/chat_service.py
server.py
settings.py
```

不得创建通用 Event Bus、Kafka Topic 或远程 Metrics 服务。

## 六、Structured Runtime Log

建立不可变 `RuntimeLogRecord`，至少包含：

- timestamp
- level
- run_id
- trace_id
- step_id
- component
- event_id
- event_type
- status
- error_code
- retry_index
- duration_ms
- safe fields

提供：

```text
StructuredRuntimeLogger
JsonStructuredRuntimeLogger
InMemoryStructuredRuntimeLogger
NoopStructuredRuntimeLogger
```

要求：

- 每行输出合法 JSON；
- 所有时间为 UTC；
- 不使用 `repr(event)`；
- 不使用 `asdict(event)`；
- 不使用 `logger.exception()` 输出 Runtime 原始异常；
- 只消费 `JournalRecord.safe_payload` 或等价安全投影；
- 不保存 Prompt、Messages、Output、Tool args、RAG Chunk、Memory 或 Secret。

## 七、Log Level Policy

建立固定映射，不由调用方临时选择：

- normal started/completed -> INFO
- degraded/partial/retry/timeout/budget -> WARNING
- user cancellation -> INFO
- system/deadline cancellation -> WARNING
- internal/journal corruption/safety -> ERROR

Cancellation 不得统一统计为 ERROR。

## 八、Metric Descriptor

建立：

```text
MetricDescriptor
MetricType
MetricLabelPolicy
```

Descriptor 至少定义：

- name
- type
- description
- unit
- allowed labels
- required labels，可选
- bounded values，可选

要求：

- Counter 以 `_total` 结尾；
- Duration Histogram 以 `_seconds` 结尾；
- Gauge 不使用 `_total`；
- 统一 `runtime_` 前缀；
- 未注册指标拒绝；
- 未允许 Label 拒绝；
- Label Value 必须是安全有限字符串；
- 数值拒绝 NaN/Infinity；
- Counter 拒绝负数。

## 九、Metrics Recorder

提供：

```text
RuntimeMetricsRecorder
InMemoryMetricsRecorder
NoopMetricsRecorder
```

至少支持：

```text
increment_counter
set_gauge
observe_histogram
snapshot
```

不接入真实 Prometheus Server。

## 十、Counter

至少建立：

```text
runtime_runs_total
runtime_steps_total
runtime_model_attempts_total
runtime_tool_attempts_total
runtime_retrievals_total
runtime_retries_total
runtime_budget_exhaustions_total
runtime_timeouts_total
runtime_cancellations_total
runtime_journal_append_failures_total
runtime_event_duplicates_total
runtime_observability_dropped_records_total
```

规则：

- Run Result 只在唯一 RUN_COMPLETED 统计；
- Step Result 只在 STEP_COMPLETED 统计；
- Model/Tool Attempt 只在 Started 统计；
- Retry 在 `retry_index > 0` 的 Attempt Started 统计；
- Completed 不再次增加 Attempt；
- Cancellation 与 Failure 分开；
  -同一 Event 只能投影一次。

可以增加明确的：

```text
runtime_runs_started_total
```

但不能改变 `runtime_runs_total` 的终态结果语义。

## 十一、Gauge

至少支持：

```text
runtime_active_runs
runtime_active_steps
runtime_detached_tool_workers
runtime_detached_retrieval_workers
runtime_blocking_executor_active
runtime_blocking_executor_pending
runtime_event_channel_buffered
runtime_circuit_breakers_open
```

Gauge 优先通过实际组件 Snapshot 获取：

- RunRegistry
- AgentState/Coordinator safe snapshot
- Tool Worker Tracker
- BoundedBlockingExecutor
- EventChannel
- Circuit Registry

不得将 run_id/step_id 作为 Gauge Label。

如果某 Gauge 只能通过事件投影实现，必须使用内部 Active Set 幂等维护，并在文档标明漂移风险。

## 十二、Histogram

至少支持：

```text
runtime_run_duration_seconds
runtime_step_duration_seconds
runtime_model_duration_seconds
runtime_tool_duration_seconds
runtime_retrieval_duration_seconds
runtime_journal_append_duration_seconds
runtime_blocking_executor_wait_seconds
```

所有样本统一为 seconds。

日志中的 `duration_ms` 不得原样写入 `_seconds` Histogram。

## 十三、高基数 Label

固定禁止：

```text
run_id
trace_id
event_id
step_id
invocation_id
attempt_id
retrieval_id
query_digest
resource_key_digest
source_id
chunk_id
citation_id
safe_message
用户输入
文件名
文件路径
URL
```

允许：

```text
component
event_type
status
error_code
model_profile
retry_disposition
retrieval_stage
cancellation_reason
side_effect_state
runtime_mode
```

`tool_name` 必须经过有限 Allowlist；未知值映射为 `other`，不得直接接受动态名称。

## 十四、Event Projection

建立单一 `RuntimeMetricsProjector`：

- RUN_COMPLETED -> runs total + run duration
- STEP_COMPLETED -> steps total + step duration
- MODEL_STARTED -> model attempts / retry
- MODEL_COMPLETED -> model duration / outcome
- TOOL_STARTED -> tool attempts / retry
- TOOL_COMPLETED -> tool duration / outcome
- RETRIEVAL_STARTED -> retrieval total
- RETRIEVAL_STAGE_COMPLETED -> stage duration/outcome
- RETRIEVAL_COMPLETED -> total duration/outcome
- BUDGET_EXHAUSTED -> budget counter
- TIMEOUT -> timeout counter
- CANCELLATION -> cancellation counter

不得在不同组件中重复编写同一指标映射。

## 十五、Journal Infrastructure Metrics

以下无法仅由成功 Event 投影：

- journal append failure
- journal duplicate
- channel buffer
- detached worker
- circuit current state

建立窄接口：

```text
RuntimeInfrastructureMetricsHook
RuntimeGaugeProvider
```

要求：

- Hook 失败不能改变 Journal/业务结果；
- Hook 不生成 RuntimeEvent；
- Hook 不递归调用 Journal；
  -不能形成 Metrics -> Event -> Metrics 循环。

## 十六、Sink 独立幂等消费

Logger 与 Metrics 使用不同 `consumer_id`：

```text
runtime_structured_logger_v1
runtime_metrics_projector_v1
```

分别复用现有：

```text
IdempotentEventConsumer
EventConsumptionCheckpointStore
```

要求：

- Logger 成功、Metrics 失败时，Replay 只重试 Metrics；
- Metrics 成功、Logger 失败时，Replay 只重试 Logger；
- Handler 成功后才写 Checkpoint；
- Duplicate 不重复写日志或指标；
  -并发 Duplicate 每个 Sink 只执行一次。

不得建立一个共享 Checkpoint 覆盖两个 Sink。

## 十七、Observability Dispatcher

由于 RuntimeEventChannel 是单消费者，Observability 不得从 UI Channel 中 `get()`。

建立 application-scoped：

```text
RuntimeObservabilityDispatcher
```

要求：

- 输入只接受安全 `JournalRecord`；
- 有界 Queue；
- 固定 Logger/Metrics Sink；
- 不实现动态 Topic/Subscriber；
- Queue 满时不无限阻塞 Runtime；
  -增加 Dropped Record 自监控；
- Dispatcher 错误不改变 Run；
- close/flush 幂等；
- Shutdown 使用有界 timeout。

发布顺序：

```text
Journal append
→ Observability try_submit
→ RuntimeEventChannel enqueue
```

如果真实代码更适合先 Channel enqueue 再 try_submit，也必须保证：

- Observability 失败不影响 Channel；
- Journal 仍然在两者之前；
  -不从 UI Channel 争抢 Event；
  -结果文档准确说明顺序。

## 十八、Failure Isolation

Logger、Metrics、Dispatcher 失败时：

- 不修改 RunStatus/StepStatus；
- 不触发 Model/Tool/RAG Retry；
- 不重新执行业务；
- 不回滚 Journal；
- 不阻塞无限等待；
- 不泄漏原始异常；
  -不写 Sink Checkpoint；
  -记录 Dropped/self-health 计数。

self-health 指标不能依赖已经失败的同一个 Exporter 才能存在；至少维护本地安全计数。

## 十九、Legacy 边界

当前默认 `/api/chat` 仍可能为 Legacy Text Stream。

本日：

- 只接入 Coordinated Runtime Event 路径；
- Legacy 文本日志保持现状并准确记录；
- 不为 `[[ORCH]]` 文本伪造 Runtime Event；
- 不迁移默认 API；
- 默认 API 迁移留到第 23 天。

## 二十、重点 Bad Case

结果文档至少包含十二项：

1. Duplicate Event 使 Counter 增加两次；
2. run_id 作为 Label；
3. Tool Output 写日志；
4. Metrics 失败导致 Run 失败；
5. Gauge 漂移；
6. RUN_COMPLETED 重复统计；
7. repr(exception) 泄漏；
8. Model Retry 重复计数；
9. Logger/Metrics 共享 Checkpoint；
10. Histogram 单位混用；
11. Cancellation 统计为普通失败；
12. Observability 争抢 EventChannel。

固定格式：

```markdown
### Bad Case X：名称

- 类型：
- 触发条件：
- 故障表现：
- 根因分析：
- 修复方案：
- 回归测试：
- 对应知识点：
- 面试表达：
- 当前状态：
```

区分真实发现和假设构造。

## 二十一、测试

建议新增：

```text
tests/test_structured_runtime_logging.py
tests/test_runtime_metrics.py
tests/test_observability_dispatcher.py
tests/test_observability_integration.py
```

至少覆盖：

### Logging

- identity
- level
- safe projection
- JSON line
- no Prompt
- no Tool/RAG/Memory
- no raw exception
- cancellation level

### Metrics

- descriptors
- type validation
- naming
- label policy
- counter
- gauge
- histogram
- seconds conversion
- Event projection

### Idempotency

- live + replay
- concurrent duplicate
- sink independent checkpoints
- handler failure
- checkpoint after success

### Gauge

- RunRegistry
- Tool detached
- Retrieval detached
- executor active/pending
- channel buffered
- circuit open
- close returns zero

### Failure

- logger failure
- metrics failure
- queue full
- worker failure
- flush timeout
- close idempotency
- runtime still succeeds
- no business retry

### Integration

- journal-first
- no UI Channel competition
- all Runtime Event families
- observability no new Event
- no recursion

测试不得调用真实模型、网络、Chroma、外部 Tool 或 UI。

## 二十二、测试命令

执行：

```text
uv run python -m pytest \
  tests/test_structured_runtime_logging.py \
  tests/test_runtime_metrics.py \
  tests/test_observability_dispatcher.py \
  tests/test_observability_integration.py \
  tests/test_event_journal.py \
  tests/test_event_journal_integration.py \
  tests/test_idempotent_event_consumer.py \
  tests/test_runtime_event_integration.py \
  tests/test_tool_execution.py \
  tests/test_retrieval_integration.py -q
```

执行全仓：

```text
uv run python -m pytest -q
uv run python -m compileall -q core tools tests
uv lock --check
git diff --check
```

## 二十三、禁止事项

不得：

- 实现 Trace Span；
  -部署 Prometheus；
  -部署 OpenTelemetry Collector；
  -创建 Event Bus；
  -创建动态 Subscriber；
  -实现自动 Journal Dispatcher Replay；
  -修改 AgentState Schema；
  -让 Metrics 修改 Runtime 状态；
  -记录 Prompt、Tool/RAG/Memory 正文；
  -使用高基数用户 Label；
  -为 Legacy 文本伪造 Runtime Event；
  -迁移默认 `/api/chat`；
  -实施第 21 天或第 23 天内容；
  -创建 Tool Registry、Skill、MCP。

## 二十四、结果文档

创建：

```text
docs/learning/stage2/result/day20_observability_result.md
```

必须包含：

# 阶段二第 20 天改造结果

## 1. 本次目标

## 2. 修改前 Logging / Metrics

## 3. Event-driven Observability

## 4. Structured Runtime Logger

## 5. Log Schema

## 6. Log Level Policy

## 7. Sensitive Data Redaction

## 8. Metrics Recorder

## 9. Metric Descriptor

## 10. Counter

## 11. Gauge

## 12. Histogram

## 13. Naming Convention

## 14. Label Policy

## 15. Event Projection

## 16. Infrastructure Metrics

## 17. Idempotent Consumption

## 18. Sink Checkpoint

## 19. Observability Dispatcher

## 20. Failure Isolation

## 21. Runtime 真实接入

## 22. Legacy 边界

## 23. 重点 Bad Case

## 24. 测试结果

## 25. 未完成事项与风险

## 26. 面试表达

## 27. 需要带回 ChatGPT 审查的信息

未完成事项至少说明：

- 没有真实 Prometheus；
  -没有 Trace Span；
  -没有自动 Journal Replay；
  -Gauge 仅单进程；
  -默认 Legacy 路径尚未接入；
  -Queue Full 可能丢弃实时 Observability；
  -Journal 仍可供未来补处理；
  -Observability Checkpoint 与业务状态非同一事务。

## 二十五、完成后输出

结果文档路径：

新增文件：

修改文件：

修改前日志：

修改前 Metrics：

Observability input：

Logger：

Log schema：

Log level：

Safe projection：

Metrics recorder：

Metric descriptor：

Counter：

Gauge：

Gauge owner：

Histogram：

Duration unit：

Label allowlist：

Label denylist：

Tool name policy：

Event projection：

Journal metrics：

Logger consumer ID：

Metrics consumer ID：

Checkpoint：

Dispatcher：

Queue capacity：

Queue full：

Failure isolation：

Runtime integration：

Legacy：

新增测试：

目标 pytest：

全仓 pytest：

compileall：

lock check：

diff check：

需要人工确认的问题：

# 二十、第 20 天验收清单

## 理论验收

-  理解 Logging、Metrics、Trace 的区别
-  理解 Structured Logging
-  理解 Counter、Gauge、Histogram
-  理解 Event-driven Metrics
-  理解 Gauge Snapshot
-  理解高基数 Label 风险
-  理解幂等指标消费
-  理解 Sink 独立 Checkpoint
-  理解安全日志投影
-  理解 Observability Failure Isolation

## 项目验收

-  Structured Runtime Logger
-  RuntimeLogRecord
-  Log Level Policy
-  Sensitive Data Redaction
-  Metric Descriptor
-  Label Policy
-  RuntimeMetricsRecorder
-  Counter
-  Gauge Provider
-  Histogram
-  Event Metrics Projector
-  Infrastructure Metrics Hook
-  Logger 幂等 Consumer
-  Metrics 幂等 Consumer
-  Sink 独立 Checkpoint
-  Observability Dispatcher
-  有界 Queue
-  Queue Full 降级
-  Failure Isolation
-  Coordinated Runtime 接入
-  Legacy 边界
-  十二个 Bad Case
-  专项及全仓测试
-  完成 ChatGPT 审查

**阶段二第 20/25 天：理论、架构和 Codex 实操方案完成，等待改造结果审查。**