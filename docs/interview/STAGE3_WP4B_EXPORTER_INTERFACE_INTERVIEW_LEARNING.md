# 1. 一句话项目 / 工作包定义

### 面试版

> 我在 LocalAgent Runtime（运行时）里实现了一套**与业务执行解耦的 Trace Exporter Interface（追踪导出接口）**：把内部完成态 `SpanRecord` 经过冻结的安全投影转换成 `TraceExportEnvelope`，通过有界非阻塞队列交给独立 worker（工作线程）发送，同时补齐 recorder lifecycle（记录器生命周期）、shutdown barrier（关闭屏障）、失败隔离、health/metrics（健康状态/指标）和严格的故障语义。

核心链路最终是：

```text
SpanHandle._end()
        ↓
InMemorySpanRecorder
        ↓
本地 bookkeeping
        ↓
释放 recorder lock
        ↓
single completion observer
        ↓
TraceExportDispatcher
        ↓
project_span()
        ↓
TraceCompatibilityEvaluator
        ↓
bounded queue.Queue
        ↓
single worker
        ↓
TraceExporter.send(TraceExportEnvelope)
```

这是最终源码和独立 Gate 都确认的唯一导出链路。

------

# 2. 为什么要做 WP4-B

WP4-A 已经解决了一个问题：

> **什么 Trace 数据可以安全、稳定地暴露给外部消费者？**

它冻结了：

- `TraceExportEnvelope`
- version
- schema / value domain
- compatibility
- contract fingerprint（合同指纹）

但 WP4-A 没解决：

> **Runtime 真正产生 Span 后，谁负责把这些 Envelope 异步、安全地送出去？**

Scout（源码侦察）阶段确认，当时还没有 export queue、worker、retry、batch、durable spool、AgentEvalOps adapter 或 OTel SDK；同时远端发送不能进入 Run、Tool、Model、OutputGate、Memory 或 `SpanHandle._end()` 的关键路径。

因此 WP4-B 的核心目标不是“写 HTTP 请求”，而是建立：

> **Runtime → 安全公共 Trace 数据 → 非阻塞导出基础设施**

WP4-C 才负责真正接 AgentEvalOps。

------

# 3. 真实性与完成边界

这是面试时必须说清楚的部分。

### 已真实实现

- `TraceExporter` envelope-only Protocol（仅 Envelope 协议）
- `TraceExportDispatcher`
- bounded `queue.Queue`
- 单 daemon worker
- `project_span()` + compatibility fail-closed
- `InMemorySpanRecorder` 单 completion observer
- local-record-first + observer-outside-lock
- producer barrier
- `ApplicationRuntimeServices` 生命周期接线
- flush / close
- component truth
- exporter metrics
- bounded metric vocabulary
- high-cardinality label protection
- worker fatal failure handling
- `worker_unavailable` drop reason
- 正式 architecture / owner / capability / security 文档。

### 已真实测试

最终 Gate：

```text
WP4-B targeted        89 passed
WP4-A                113 passed
Lifecycle             19 passed
Metrics               42 passed
Trace                 23 passed
Default/Lifespan/ASGI 14 passed
WP3 Security         183 passed
Formal Docs           17 passed
Combined             202 passed

Full collection      2394
Full regression      2394 passed
Subtests               42 passed
failed                  0
```

并且 `compileall`、`uv lock --check`、`git diff --check` 全通过。

### 未实现

必须明确说：

- Production external delivery（生产外部投递）
- AgentEvalOps Adapter
- HTTP exporter
- Settings enable / endpoint / auth
- `server.py` production wiring
- retry
- batching
- durable delivery
- generic wire serializer
- OpenTelemetry / OTLP
- exporter Recovery。

所以不能说：

> “我已经让 LocalAgent 把 Trace 上报到 AgentEvalOps。”

目前正确表述是：

> “我已经完成了 consumer-neutral（消费者无关）的 Trace 导出基础设施，生产 adapter 在 WP4-C 接入。”

------

# 4. 修改前架构与根因

WP4-A 后已经存在：

```text
Internal SpanRecord
        ↓
project_span()
        ↓
TraceExportEnvelope
```

但没有 production execution owner（生产执行所有者）。

如果直接让 recorder 或 SpanHandle 调外部 exporter：

```text
SpanHandle.end()
        ↓
HTTP / external exporter
```

问题非常大。

### 问题一：transport 会污染 Runtime critical path

网络慢、远端不可用、超时都会拖住：

```text
Span completion
→ Runtime completion
→ Tool / Model / Run lifecycle
```

### 问题二：raw Span 安全边界容易被绕过

Scout 还发现历史的：

```
OpenTelemetryCompatibleSpanAdapter.export_snapshot()
```

可以直接复制内部 `SpanRecord.attributes`。

它当时没有装配 transport，因此不是生产事故，但如果拿来直接做 exporter，就可能绕过 WP4-A 的 `TraceExportEnvelope` allowlist。

### 问题三：shutdown 会丢最后一批 Span

因为 recorder 在 shutdown 时还可能生成：

```text
CANCELLED
error_code = RECORDER_CLOSED
```

如果先关闭 exporter，再关闭 recorder：

```text
exporter.close()
span_recorder.close()
```

最后产生的 Span 就没有消费者了。

这也是为什么 WP4-B 后来必须建立 producer barrier。

------

# 5. 方案讨论与取舍

## 方案一：recorder 直接调用 exporter

拒绝。

原因：

- recorder 获得 transport responsibility（传输职责）
- Runtime 与外部系统耦合
- lock 边界危险
- 很难控制 backpressure（背压）

------

## 方案二：复用现有 RuntimeObservabilityDispatcher

也没有采用。

因为现有 dispatcher 面向 JournalRecord / Runtime Event，其 sequence（序列）还有自己的语义。

Trace export：

- 数据类型不同
- backpressure 策略不同
- 生命周期不同
- 顺序保证不同

复用会导致 owner 混乱。

------

## 方案三：`asyncio.Queue`

最终也没选。

一个关键工程事实是：

> Span completion 可能来自任意线程。

因此需要：

```text
arbitrary producer thread
        ↓
thread-safe synchronous nonblocking handoff
```

标准库 `queue.Queue(maxsize=N)` 更符合这个边界。

------

## 最终方案

Architecture Decision 最终冻结：

```text
SpanRecord
    ↓
application-scoped TraceExportDispatcher
    ↓
project_span()
    ↓
compatibility
    ↓
queue.Queue.put_nowait()
    ↓
single daemon worker
    ↓
TraceExporter.send(envelope)
```

producer 侧不允许 network、sleep、await、retry、blocking put。

------

# 6. 最终架构

可以把 WP4-B 分为五层理解。

### 第一层：Internal Trace

```text
SpanHandle
SpanRecord
InMemorySpanRecorder
```

负责 Runtime 内部 Trace truth（事实）。

### 第二层：WP4-A Projection

```text
project_span()
TraceCompatibilityEvaluator
TraceExportEnvelope
```

负责：

> “这个 Span 是否允许成为公共导出数据？”

### 第三层：WP4-B Dispatcher

```text
TraceExportDispatcher
```

负责：

- projection 调用时机
- compatibility consumption
- queue
- worker
- health
- drop
- flush
- close

### 第四层：Transport Protocol

```text
TraceExporter.send(TraceExportEnvelope)
TraceExporter.close(timeout_seconds)
```

只负责一次 transport attempt（传输尝试）。

### 第五层：Application Lifecycle

```text
ApplicationRuntimeServices
```

负责正确的 resource ordering（资源关闭顺序）。

关键点：

> **Schema Owner、Dispatcher Owner、Transport Owner、Lifecycle Owner 是四个不同职责。**

这是这个 WP 非常值得面试讲的地方。

------

# 7. 核心状态机与时序

## Dispatcher 状态机

核心状态：

```text
RUNNING
CLOSING
CLOSED
FAILED
```

典型正常路径：

```text
RUNNING
  ↓ close()
CLOSING
  ↓ drain + adapter.close
CLOSED
```

真实 timeout：

```text
RUNNING
  ↓ close()
CLOSING
  ↓ deadline exceeded
CLOSING
```

不是 CLOSED。

worker fatal：

```text
RUNNING
  ↓ worker fatal
FAILED
```

最终 Application lifecycle 会报告 `CLOSE_FAILED`，而不是 timeout。

------

## flush barrier

不能简单使用：

```python
queue.empty()
```

因为 queue empty 不代表：

- worker 已完成 send
- 当前 item 的 transport attempt 已结束

最终使用的是：

```text
target = accepted_total

等待：

completed_attempt_count >= target
```

所以 flush success 表示：

> flush 调用前已经 accepted 的 envelope，都已经完成一次 attempt handling。

它不代表全部 sent，更不代表 remote durable。

------

# 8. 数据、权限与 Owner 边界

这里面试非常容易追问。

## `InMemorySpanRecorder`

拥有：

- active spans
- completed local truth
- local dropped facts
- Span lifecycle

不拥有 exporter。

------

## `TraceExportDispatcher`

拥有：

- `project_span()` invocation
- compatibility consumption
- queue
- worker
- health/drop
- flush/close

不拥有：

- public schema
- fingerprint
- Run outcome
- Journal
- Snapshot
- Recovery
- AgentEvalOps mapping。

------

## `TraceExporter`

拥有：

> 对一个 `TraceExportEnvelope` 执行一次 transport attempt。

不负责：

- projection
- queue
- retry
- compatibility
- delivery guarantee

------

## Runtime Authority

Exporter 完全是 side-channel observability（旁路可观测性）能力。

最终 Re-Gate 确认它不会写：

- Run status
- AgentState
- OutputGate
- DeliveryStatus
- Memory
- Journal
- Snapshot
- Recovery
- Tool/Retrieval。

------

# 9. 兼容策略

这里分三层。

## Trace Export Contract

WP4-A：

```
PUBLIC_VERSIONED
```

稳定公共合同。

WP4-B 不改它。

Fingerprint 最终仍为：

```
6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab
```



------

## Dispatcher operational contract

```
INTERNAL_RC
```

例如：

```text
TRACE_EXPORT_DROP_REASONS
```

属于 dispatcher 的 operational vocabulary（运行词表），不是 WP4-A fingerprint 内容。

所以后来增加：

```text
worker_unavailable
```

不需要修改 TraceExportEnvelope，也不用 bump WP4-A fingerprint。

------

## Disabled compatibility

默认：

```text
completion_observer = None
trace_export_dispatcher = None
```

也就是：

> disabled-by-absence（通过不存在来禁用）

而不是：

- NoopExporter
- DisabledDispatcher
- fake worker

这样老路径不产生额外生命周期行为。

------

# 10. Bad Cases

这一节很重要，因为 WP4-B 最大价值之一就是 Gate 真发现了问题。

## Bad Case 1：adapter close failure 被误报 timeout

**真实性：源码审查 / Gate 发现，不是生产事故。**

原逻辑：

```text
dispatcher.close() == False
→ CLOSE_TIMEOUT
```

但 `False` 可能是：

```text
adapter.close() = False
adapter.close() raises Exception
real timeout
```

修复后：

```text
CLOSED
→ CLOSE_FAILED

CLOSING
→ CLOSE_TIMEOUT
```

历史 `P1-01` 最终 CLOSED。

------

## Bad Case 2：worker fatal 被误报 timeout

**真实性：独立 Final Gate direct probe，非生产事故。**

Gate 让 exporter `send()` 抛 `SystemExit`：

```text
worker 已死
state = FAILED
close elapsed = 0
```

但 lifecycle 仍报告：

```text
CLOSE_TIMEOUT
```

明显不真实。

修复为：

```text
FAILED
→ CLOSE_FAILED
```

最终 `P1-02=CLOSED`。

------

## Bad Case 3：health 与 metric 丢包计数不一致

同一个 worker fatal：

```text
pending queue = 1

metric:
dropped += 1

health:
dropped_total = 0
```

这违反：

> health 是 authoritative internal truth（权威内部事实），metric 只是 projection。

修复后同一 abandoned envelope：

```text
health.dropped_total = 1
metric dropped = 1
```

且重复 close 不重复计数。

`P1-03=CLOSED`。

------

## Bad Case 4：worker fatal 被标记成 shutdown_timeout

这是最精彩的一个。

代码和数字已经都对了：

```text
worker dead
CLOSE_FAILED
health dropped = 1
metric dropped = 1
```

但 metric reason 还是：

```text
shutdown_timeout
```

问题是：实际上根本没有 timeout。

这被 Re-Gate 判定为 semantic contract violation（语义合同违规），不能靠改注释糊过去，于是进行了 Architecture Re-entry。

最终新增：

```text
worker_unavailable
```



这是非常好的面试故事：

> **测试全绿不代表语义合同正确。**

------

# 11. Tests / Gate：真实执行过什么

最终 Gate 不是只跑 pytest，还包含 adversarial probes（对抗探针）。

例如：

- raw Span marker → envelope 安全投影
- blocked exporter → queue saturation
- producer vs close race
- concurrent close
- adapter close False
- adapter close Exception
- worker fatal
- health/metric parity
- final drop idempotency
- cardinality violation
- fabricated reason/stage
- recorder lock re-entry
- RECORDER_CLOSED span
- producer barrier。

最终：

```text
2394 passed
42 subtests
0 failed
0 skipped
0 xfail
```

这才是面试时可以说的真实数据。

------

# 12. Known Limitations

WP4-B 完成后仍然明确保留：

1. **BEST_EFFORT**，不是可靠消息系统。
2. queue 是 memory-only，进程崩溃会丢 queued/in-flight envelope。
3. 无 retry。
4. 无 batching。
5. 单 worker，吞吐能力有限。
6. 没有 durable outbox / spool。
7. 没有 replay。
8. send success 不代表 remote durable。
9. queue FIFO 只代表本地处理顺序，不代表 Trace semantic ordering。
10. 没有 production adapter。
11. 没有 AgentEvalOps wiring。
12. 没有 HTTP。
13. 没有 OTel / OTLP。
14. raw `OpenTelemetryCompatibleSpanAdapter.export_snapshot()` 仍是 Known Limitation / Design Stop Condition，但未接入 WP4-B。
15. Recovery 不读取 exporter queue。

此外仍继承：

```text
P2-02 = ACCEPTED_P2
P2-03 = DEFERRED
```

这两个不是 WP4-B 缺陷。

------

# 13. 这个 WP 体现了什么工程能力

### 1. Ownership Design（所有权设计）

你不是把所有东西塞进一个 exporter。

而是明确：

```text
schema owner
dispatcher owner
transport owner
lifecycle owner
metrics semantic owner
```

------

### 2. Concurrency Design（并发设计）

处理了：

- arbitrary producer threads
- bounded queue
- nonblocking producer
- close race
- concurrent close
- lock boundary
- barrier

------

### 3. Lifecycle Engineering（生命周期工程）

真正困难的不是 send，而是：

> shutdown 到底谁先关？

通过 `recorder.close()` 建 producer barrier，再关闭 dispatcher。

------

### 4. Observability Semantics（可观测语义）

不是“有 metric 就行”。

最后甚至因为：

```text
shutdown_timeout
```

这个词不真实而阻断 Gate。

这说明你在做：

> semantic correctness（语义正确性），而不只是代码正确性。

------

### 5. Failure Isolation（故障隔离）

外部 export 失败不能：

- 改 Run outcome
- 阻塞 Runtime
- 破坏 recorder
- 影响 readiness

这很符合生产 Agent Runtime 的设计思想。

------

# 14. 30 秒面试回答

> 我在 LocalAgent 的 Trace 系统上做过一套 Exporter Interface。原来 WP4-A 只冻结了安全的 TraceExportEnvelope，但还没有真实的导出执行层。我增加了 application-scoped TraceExportDispatcher，用 bounded queue 和单 worker 把 Trace transport 从 Runtime critical path 解耦。Recorder 只加一个 completion observer，而且必须先完成本地记录、释放锁之后再通知 exporter。Shutdown 时先关闭 recorder 建 producer barrier，再关 dispatcher，保证 RECORDER_CLOSED 产生的最后 Span 不丢。整个能力是 best-effort，每个 accepted envelope 至多一次 transport attempt，不承诺 exactly-once。最终还通过独立 Gate 修复了 worker fatal、health/metric 一致性和 drop reason 语义问题，全仓 2394 个测试通过。

------

# 15. 2 分钟面试回答

> WP4-B 的背景是我们已经在 WP4-A 冻结了 Trace Contract 和 TraceExportEnvelope，但还缺一个真正的 Runtime 导出执行层。我不希望直接从 SpanHandle 或 Recorder 调外部 HTTP，因为这样 transport latency 和失败会进入 Runtime critical path，而且内部 SpanRecord 还有敏感 attributes，直接导出会绕开 WP4-A 的安全投影。
>
> 所以最终架构是在 InMemorySpanRecorder 后面加一个单 completion observer。Recorder 先在自己的锁内完成 active/completed bookkeeping，释放锁后才调用 observer。Observer 接到 SpanRecord 后进入 TraceExportDispatcher，由 dispatcher 统一调用 project_span 和 compatibility evaluator，再通过 bounded queue.Queue 的 put_nowait 放入队列，后台单 worker 才调用 TraceExporter.send。
>
> Shutdown 上比较关键。Recorder close 还会生成 RECORDER_CLOSED 的最终 Span，所以不能先关 exporter。我们的顺序是 span_recorder.close 建 producer barrier，确保所有 active span 已完成 record 和 observer 通知，再 trace_export_dispatcher.close 做 final accepted barrier drain，最后 adapter.close。
>
> 这个 WP 最有价值的是独立 Gate 真发现了几个问题，例如 adapter close failure 被误报 timeout、worker fatal 被误报 timeout、health 和 metrics 对 dropped item 计数不一致。最后还有一次所有测试都通过，但 worker fatal 的 drop reason 仍叫 shutdown_timeout，被判定为语义合同错误。我们重新做 Architecture Re-entry，增加 worker_unavailable，把 failure 和 timeout 完全分开。最终 P0/P1 都归零，全仓 2394 个测试通过。

------

# 16. 深入版本：面试官继续追问时怎么展开

可以按照六步回答。

## 第一步：安全边界

```text
SpanRecord
≠
TraceExportEnvelope
```

SpanRecord 是内部模型。

Envelope 才是允许给 exporter 的公共边界。

------

## 第二步：关键路径隔离

```text
producer
只做：
project
compatibility
put_nowait
```

真正 transport：

```text
worker thread
```

------

## 第三步：背压

queue 满：

```text
reject incoming
DROP_NEWEST
```

而不是：

```text
block Runtime
```

------

## 第四步：生命周期

```text
recorder.close
→ producer barrier
→ dispatcher.close
→ final barrier
→ adapter.close
```

------

## 第五步：delivery 语义

只能说：

```text
BEST_EFFORT
AT_MOST_ONE_TRANSPORT_ATTEMPT
```

不能说：

```text
exactly once
at least once
at-most-once delivery
```

------

## 第六步：观测自身的真实性

最终必须保证：

```text
lifecycle fact
health fact
metric fact
docs
```

四者不互相矛盾。

这也是 P1-04 最值得讲的地方。

------

# 17. 高频追问

### Q1：为什么不用 asyncio.Queue？

因为 Span completion 可能发生在任意线程，producer 是同步 seam。标准库 thread-safe `queue.Queue` 可以直接支持 cross-thread（跨线程）非阻塞 handoff，而不要求绑定 event loop。

------

### Q2：为什么只用一个 worker？

第一版重点是：

- ownership 简单
- send concurrency 可控
- lifecycle 可证明
- close semantics 清晰

代价是 throughput 上限，因此单 worker 是 Known Limitation，不包装成高吞吐架构。

------

### Q3：为什么 queue 满了直接 drop？

因为 Trace export 是 side channel。

原则是：

> **Observability failure must not become business failure.**

如果为了“不丢 Trace”反而堵住 Tool/Model/Run，就把优先级反过来了。

------

### Q4：为什么不用 retry？

因为 retry 会立即引入：

- duplicate delivery
- idempotency
- retry budget
- backoff
- shutdown interaction
- persistence
- ambiguity

WP4-B 明确把它留在范围外。

------

### Q5：为什么 flush 不用 `queue.empty()`？

因为：

```text
queue empty
```

只能证明 item 已被 worker dequeue。

不能证明：

```text
send attempt 已经结束
```

所以使用 accepted barrier + completed attempt counter。

------

### Q6：为什么 `send()` 成功也不能说 delivered？

因为：

```text
send returns
```

只说明 adapter 本地调用成功。

远端可能：

- 收到但未持久化
- 网络 ACK 后崩溃
- downstream 再失败

所以只能叫 `sent`，不能叫 durable delivered。

------

### Q7：为什么 health 是权威，而 metric 不是？

health counter 属于 dispatcher 内部状态。

metrics 是 best-effort projection。

metrics recorder 自己也可能失败，所以不能反过来让 metrics 成为 Runtime truth。

------

### Q8：为什么 worker fatal 要有 `worker_unavailable`？

因为：

```text
failures{stage=worker}
```

回答“谁坏了”。

```text
dropped{reason=worker_unavailable}
```

回答“为什么这条 envelope 丢了”。

而 `shutdown_timeout` 必须保留给真实 deadline timeout。

------

# 18. 最容易夸大或答错的地方

### 错误 1

> “我们实现了可靠 Trace 投递。”

错。

正确：

> best-effort。

------

### 错误 2

> “每条 Trace at-most-once delivery。”

错。

正确：

> 每个 accepted envelope 至多一次 **transport attempt**。

------

### 错误 3

> “WP4-B 已经对接 AgentEvalOps。”

错。

正确：

> WP4-B 完成 consumer-neutral dispatcher，WP4-C 才做 AgentEvalOps adapter。

------

### 错误 4

> “RECORDER_CLOSED Span 会正常进入 recorder completed snapshot。”

错。

它仍遵循已有 recorder closed/drop bookkeeping，但 observer 可以看到并导出。

------

### 错误 5

> “用了 OpenTelemetry。”

错。

`OpenTelemetryCompatibleSpanAdapter` 只是历史 OTel-shaped helper。

OTel SDK / OTLP：

`NOT_IMPLEMENTED`。

------

### 错误 6

> “2394 测试全部通过，所以设计正确。”

这个 WP 恰恰证明这句话不成立。

P1-04 就是在所有机械测试已经绿色后，由 semantic audit（语义审计）发现：

```text
worker fatal
却标成
shutdown_timeout
```

最终甚至需要 Architecture Re-entry。

------

# 19. P0 / P1 / P2 复习

## 最终状态

```text
P0 = 0
P1 = 0

P1-01 CLOSED
P1-02 CLOSED
P1-03 CLOSED
P1-04 CLOSED

P2 = 2
```



### P1-01

```
TRACE_EXPORT_CLOSE_RESULT_REASON_COLLAPSE
```

adapter failure 和 timeout 被混淆。

------

### P1-02

```
WORKER_FATAL_CLOSE_MISCLASSIFIED_AS_TIMEOUT
```

worker 已经死亡，却报告 timeout。

------

### P1-03

```
WORKER_FATAL_HEALTH_METRIC_DROP_DIVERGENCE
```

metric 说 dropped，health 却没计。

------

### P1-04

```
WORKER_FATAL_DROP_REASON_SEMANTIC_MISMATCH
```

worker fatal abandonment 被错误命名为 `shutdown_timeout`。

------

### P2-02

```
ACCEPTED_P2
```

delivery/memory 的 negative / not-attempted states 不制造 fake symmetric spans。

### P2-03

```
DEFERRED
```

planning / step error 仍可能 collapse 到 `UNHANDLED_ERROR`。

它们不是 WP4-B blocker。

------

# 20. 最终速查表

| 问题                      | 记忆答案                                             |
| ------------------------- | ---------------------------------------------------- |
| WP4-B 做什么              | Runtime Trace 的 consumer-neutral 非阻塞导出基础设施 |
| Exporter 输入             | `TraceExportEnvelope`                                |
| Raw Span 能否外发         | 不能                                                 |
| Projection Owner          | `TraceExportDispatcher` 调用 WP4-A `project_span()`  |
| Queue                     | bounded `queue.Queue`                                |
| Producer                  | 同步、非阻塞                                         |
| Queue full                | DROP_NEWEST / reject incoming                        |
| Worker                    | 单 daemon worker                                     |
| Adapter send 并发         | 1                                                    |
| Delivery                  | BEST_EFFORT                                          |
| Guarantee                 | at most one transport attempt per accepted envelope  |
| Retry                     | 未实现                                               |
| Batch                     | 未实现                                               |
| Durable                   | 未实现                                               |
| Recorder observer         | 单个、可选                                           |
| Observer 时机             | 本地记录完成 + lock 释放后                           |
| Observer failure          | 不影响 Runtime / recorder truth                      |
| Producer barrier          | `span_recorder.close()`                              |
| Close 顺序                | recorder → dispatcher                                |
| Flush                     | accepted barrier → completed attempts                |
| Adapter close failure     | `CLOSE_FAILED`                                       |
| 真 deadline timeout       | `CLOSE_TIMEOUT`                                      |
| Worker fatal              | `FAILED + CLOSE_FAILED`                              |
| Worker fatal drop         | `worker_unavailable`                                 |
| 真 shutdown deadline drop | `shutdown_timeout`                                   |
| Health                    | authoritative                                        |
| Metrics                   | best-effort projection                               |
| Drop reason 数            | 7                                                    |
| Failure stage 数          | 6                                                    |
| AgentEvalOps              | 未实现，WP4-C                                        |
| HTTP exporter             | 未实现                                               |
| OTel / OTLP               | 未实现                                               |
| 最终全仓测试              | 2394 passed + 42 subtests                            |
| 最终 WP4-B                | COMPLETE                                             |
| WP4 总体                  | 未完成                                               |
| Stage 3                   | 未 PASS                                              |

------

## 这一 WP 最值得真正记住的三句话

第一句：

> **Observability（可观测性）必须是 side channel，不能因为观测系统故障反过来阻塞 Runtime 主链路。**

第二句：

> **Queue acceptance、transport attempt、send success、remote durable delivery 是四个不同层级的事实。**

第三句，也是 WP4-B 最有面试价值的一句：

> **测试全绿并不代表合同语义正确；可观测系统尤其需要保证 lifecycle、health、metrics 和文档描述的是同一个真实故障事实。**

这其实就是 WP4-B 从普通“Exporter 功能开发”升级成一段有含金量的生产 Runtime 工程经历的地方。