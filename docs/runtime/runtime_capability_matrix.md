# Runtime Capability Matrix

状态词仅允许：`SUPPORTED`、`PARTIALLY_SUPPORTED`、`CONTRACT_ONLY`、`NOT_IMPLEMENTED`、`LEGACY_ONLY`、`DEPRECATED`。存在类型、枚举或测试 seam 不等于生产能力已支持。

| capability | status | default_path | owner | persistence | failure_behavior | recovery_behavior | legacy_support | tests | known_limitations |
|---|---|---|---|---|---|---|---|---|---|
| Coordinated Runtime | SUPPORTED | 是 | ChatRuntimeSelector + CoordinatedRuntimeFactory + RunCoordinator | Journal；Snapshot opt-in | 所选路径内安全失败并发 terminal | 仅可做 validation | 否 | runtime full e2e/default entry | 当前 `/api/chat` 为单 Agent、max concurrency 1 装配 |
| Legacy rollback | SUPPORTED | 否，显式 `LEGACY` | ChatService Legacy AgentLoop | 既有 Memory；无完整 Runtime Journal/Snapshot | Legacy 内失败并安全映射 | 无 Runtime recovery | 是 | runtime legacy boundary/mode e2e | 仅兼容回滚路径，不宣称完整 Coordinated 能力 |
| Parallel execution | SUPPORTED | 默认单 Agent 路径不并行 | ParallelExecutor + Scheduler | Runtime events/state | 单 step 失败按现有 policy 收口 | 无自动恢复 | 部分/非统一 | parallel execution/scheduler | 默认 factory policy `max_concurrency=1` |
| Budget | SUPPORTED | 是 | BudgetLedger | Snapshot budget projection；event counts | 超限 fail closed | Snapshot 仅供 validation | Legacy 有基础 Run budget | budget/runtime e2e | 进程内 ledger，不是跨进程配额 |
| Retry | SUPPORTED | 是 | RetryExecutor + policy | attempt index/evidence，不持久化 policy state | 按类型、幂等与预算判断 | 不自动重放历史 attempt | 既有调用链部分支持 | retry model/tool tests | 不提供跨进程 retry continuation |
| Fallback | SUPPORTED | Model routing 内 | ModelInvocationRouter + ModelRoutingPolicy | Model event 安全事实 | candidates 耗尽后类型化失败 | 不切换 Runtime | Model 层可用 | model routing/invocation | 仅 Model candidate fallback，不是 Runtime fallback |
| Circuit breaker | SUPPORTED | 是 | ModelCircuitBreakerRegistry | 进程内 state/snapshot | open 时 fail fast/route | timeout 后 half-open | 共享 model layer | model circuit breaker | 非分布式 circuit state |
| Tool idempotency | SUPPORTED | Tool contract | ToolExecutionService + invocation contract | key 仅 digest evidence | 不安全结果禁止 retry | Recovery 只评估 evidence | 通过 adapter 部分支持 | tool contract/recovery | 不构成全系统 exactly-once |
| Tool side-effect evidence | SUPPORTED | Tool completion | AttemptSideEffectTracker + ToolCompletedPayload | Journal safe evidence | Unknown/committed 均保守处理 | 可给出 reconciliation decision | 部分 | tool evidence/recovery | 不保存原始参数/输出；不自动补偿 |
| Resource lease | SUPPORTED | Tool attempt | ToolConcurrencyController + ToolResourceLease | 进程内 worker snapshot | timeout/detached 延后释放 | Shutdown 统计/等待，不恢复 lease | 部分 | tool concurrency/resources | 不是跨进程 lease |
| Retrieval stage runtime | SUPPORTED | 是 | RetrievalExecutionService | 安全 stage events/counts | 类型化失败、预算/取消传播 | 只读 validation 不 replay | Legacy 通过既有 router 部分支持 | retrieval integration/execution | 不持久化 chunk/query 正文 |
| Event streaming | SUPPORTED | 是 | RuntimeEventChannel + ChatStreamCompatibilityAdapter | Journal-first 后 transport | backpressure、abort、安全 transport error | 不重放 stream | Legacy 有旧文本 stream | event channel/stream adapter | 自定义 `text/plain` 协议，不是 SSE/WebSocket |
| Journal-first | SUPPORTED | Coordinated | RuntimeEventChannel + RunEventJournal | InMemory/SQLite append-only | append 失败阻止 transport；部分发布有 evidence | Recovery 读取 tail | Legacy 未完整接入 | event journal/partial publication | 不保证跨进程 exactly-once consumer |
| Snapshot | PARTIALLY_SUPPORTED | 默认关闭，显式 opt-in | CheckpointCoordinator + SnapshotStore | SQLite/InMemory Snapshot v1 | 严格 schema/digest，失败不伪造 | 提供 validation 输入 | 否 | snapshot/checkpoint | 未自动用于继续执行；默认 `LOCAL_AGENT_SNAPSHOT_ENABLED=false` |
| Recovery validation | PARTIALLY_SUPPORTED | Snapshot 启用时可用 | RecoveryValidator | 读取 Snapshot v1 + Event v1/v2 | 不兼容/损坏/不足证据 fail closed | 只返回 Assessment | 否 | recovery integration/version compatibility | 不等于 automatic recovery |
| Recovery execution | NOT_IMPLEMENTED | 否 | 无 | 无 | 不启动任何 adapter | 不适用 | 否 | negative recovery contract tests | Validator 明确无 AgentState writer |
| Replay | NOT_IMPLEMENTED | 否 | 无 | 无 | 所有 replay flags 为 false | 不适用 | 否 | recovery contract/capability matrix | 无 model/tool/retrieval replay |
| Observability | SUPPORTED | 是 | RuntimeObservabilityDispatcher + projectors | consumer checkpoint + recorder | 失败隔离，不改变 Runtime/Journal | 无业务恢复 | shared application resource | observability integration/fault | in-process queue；指标标签受 allowlist 限制 |
| Trace | SUPPORTED | 是 | SpanRecorder + trace context | recorder backend/safe snapshots | trace 失败隔离 | 无业务恢复 | shared model/router layer | trace integration/fault | 默认 recorder 为进程内实现 |
| Client disconnect | SUPPORTED | 是 | HTTP disconnect watcher + RunRegistry cancel | 不持久化 watcher | cancel-and-drain；超时 force abort | 无自动 resume | 两条路径覆盖 | client/server stream cancellation | 依赖 cooperative cancellation 与 bounded grace |
| Worker tracking | SUPPORTED | 是 | BoundedBlockingExecutor / ToolConcurrencyController | 进程内 snapshots | active/detached/unknown 分类 | Shutdown 等待或保守 deferred close | 两类 worker 覆盖 | worker lifecycle/shutdown | 已 detached 的同步线程不能被 Python 强杀 |
| Graceful shutdown | SUPPORTED | lifespan exit | GracefulShutdownCoordinator | ShutdownReport 进程内派生 | 关闭 admission、取消、drain、flush、close；失败继续 | 不自动重启/恢复 | Coordinated + Legacy workers | graceful shutdown/truthfulness | `completed` 只表示 orchestration；应看 `fully_closed` |
| Fault Injection | CONTRACT_ONLY | 生产关闭 | 显式测试 FaultInjectionScope/Controller | test recorder/report；不进生产存储 | 确定性 typed fault；生产 `None` | 不拥有 recovery | 生产未启用 | day24 fault matrix + production isolation | 是测试 seam，不是 production chaos platform |
| Random Chaos | NOT_IMPLEMENTED | 否 | 无 | 无 | 无随机触发 | 无 | 否 | capability negative assertion | FaultTrigger 只有确定性触发 |
| Cross-process Registry | NOT_IMPLEMENTED | 否 | 无 | 无 | 进程退出即丢失 | 无 | 否 | capability negative assertion | RunRegistry 是进程内对象 |
| Exactly-once | NOT_IMPLEMENTED | 否 | 无系统级 owner | 局部 idempotency/journal evidence | 仅局部去重与保守失败 | 无跨进程 reconcile executor | 否 | idempotency/event consumer tests | 局部 at-most-once/幂等证据不能提升为全系统 guarantee |
| Automatic compensation | NOT_IMPLEMENTED | 否 | 无自动编排 owner | 仅 compensation evidence | 不自动触发补偿 | 只报告/评估 | 否 | tool contract/fault invariant | adapter 可报告结果，但 Runtime 不自动执行策略 |
| Step result rehydration | NOT_IMPLEMENTED | 否 | 无 | Snapshot 只保存 TextSummary/digest | 缺正文时保持 unavailable | `output_reconstruction_supported=false` | 否 | snapshot/recovery capability tests | 不从当前 Registry/Memory 回填历史正文 |

## Status summary

- SUPPORTED：Coordinated、显式 Legacy、Parallel engine、Budget、Retry、Model fallback、Circuit breaker、Tool contract/evidence/lease、Retrieval、Streaming、Journal-first、Observability、Trace、Disconnect、Worker tracking、Graceful shutdown。
- PARTIALLY_SUPPORTED：Snapshot、Recovery validation（均为 opt-in/validation-only）。
- CONTRACT_ONLY：Fault Injection test seam。
- NOT_IMPLEMENTED：Recovery execution、Replay、Random Chaos、Cross-process Registry、Exactly-once、Automatic compensation、Step result rehydration。
- LEGACY_ONLY：当前矩阵没有仅 Legacy 拥有且 Coordinated 缺失的目标能力。
- DEPRECATED：能力级无；兼容字段见架构文档的 Deprecated / Compatibility Matrix。
