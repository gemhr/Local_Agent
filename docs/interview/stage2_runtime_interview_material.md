# Stage2 Runtime Interview Material

首次出现的术语：Runtime（运行时）、Coordinated Runtime（协调式运行时）、Journal-first（日志先行）、Recovery Validation（恢复验证）、Graceful Shutdown（优雅关闭）、Fault Injection（故障注入）。本材料只描述代码级完成事实，不声明生产规模或完整生产认证。

## 1. 30 秒项目介绍

我把 LocalAgent 原本职责混杂的调用链改造成默认 Coordinated Runtime：每次请求拥有独立 Context、State、预算、取消和事件通道，由 Coordinator/Scheduler 统一驱动 Model、Tool、Retrieval，并通过 Journal-first、可观测性、Snapshot opt-in、只读恢复验证和安全关闭保持状态与资源事实。阶段二以 1000+ 自动化回归、确定性故障注入矩阵、契约冻结和 code-level RC Gate 收口；它不是生产容量或容灾认证。

## 2. 2～3 分钟架构介绍

入口是 API / ChatService，Settings 在进程启动时选择一次 Runtime，默认 Coordinated，Legacy 只能显式回滚且没有失败后跨 Runtime fallback。Coordinated factory 为每个请求创建 RunScope / RunContext / AgentState / CancellationSource / Budget / EventChannel，RunCoordinator 与 Scheduler 驱动执行。

Model 层把 Retry（重试）交给 RetryExecutor，把候选模型 Fallback（回退）限制在同一 Runtime；Tool 层用 invocation/attempt identity、幂等合同和单调 side-effect evidence 防止不安全重试；Retrieval 层区分 rewrite 可控降级与 search fail-closed。所有 RuntimeEvent 先进入 append-only Journal，再进入 channel/transport，部分发布不重跑业务。

Observability（可观测性）和 Trace（链路追踪）是 best-effort 诊断系统，不反向改变业务。Snapshot 默认关闭，启用后只提供版本化证据；RecoveryValidator 只读 Snapshot + Journal，不执行恢复。Client Disconnect 触发 first-wins cancellation 和 bounded cleanup；Shutdown 关闭 admission、drain run/worker、flush、close，并在 worker 未证明 idle 时延迟关闭 Model。最终以 code-level RC Gate 和 Operations Runbook 冻结边界。

## 3. 十个设计决策

1. 不做跨 Runtime fallback：避免同一请求业务与副作用重复，Legacy 只作为请求前部署回滚。
2. Plan 与 AgentState 分离：Plan 描述“做什么”，AgentState 唯一拥有“执行到哪里”。
3. Journal-first：先形成持久权威事实，再尝试传输；Channel 失败不回滚业务。
4. Recovery 只做 validation：没有 result rehydration 与 durable executor 时，自动 resume 会伪造安全性。
5. Tool side-effect 使用单调状态：NOT_STARTED→STARTED/COMMITTED/UNKNOWN 不允许回退，重试必须看证据。
6. Fault Controller 不拥有 Retry：它只决定测试故障命中，策略仍由 RetryExecutor/业务合同拥有。
7. 诊断系统 best effort：日志或 Span 失败不能改变业务结果，也不能触发 Model/Tool 重跑。
8. Detached Worker 不清记录：同步线程不能安全强杀，删除记录只会伪造 idle。
9. Model close 需要 worker gate：共享 client 在活跃/未知 worker 下关闭会破坏正在执行的调用。
10. orchestration 与 fully closed 分离：流程走完不代表资源关净，报告必须同时表达两类事实。

## 4. 高频面试题

### 1）你如何设计生产级 Agent Runtime？

先划清入口、运行、调用、持久化、诊断和关闭 scope；为状态、重试、副作用、sequence、terminal 指定唯一 owner；再用强类型合同、fail-closed persistence、资源快照与 fault tests 验证组合不变量。我的实现完成 code-level RC，但生产容量与容灾仍需独立验证。

### 2）取消和超时怎么传播？

RunContext 持只读 CancellationToken，RunScope 持 CancellationSource；reason first-wins。各等待点同时检查 token 与 monotonic deadline，provider_started、reservation、worker detached 等事实按真实阶段结算。

### 3）如何避免 Tool 重复副作用？

区分 invocation 与 attempt identity，使用 idempotency contract 和单调 side-effect evidence。非幂等 COMMITTED 或 UNKNOWN 不自动 retry；Started 无 Completed 进入人工 reconciliation。

### 4）Retry 和 Fallback 如何区分？

Retry 是同一 candidate 的 attempt policy，由 RetryExecutor 拥有；Fallback 是 ModelInvocationRouter 在候选模型之间切换；Runtime mode 从不因失败切换。

### 5）Event 为什么采用 Journal-first？

它让“已发生的持久事实”和“是否成功送到客户端”分离。append 成功而 channel 失败时保留 record、sequence 与 partial evidence，不重做业务。

### 6）如何处理 Client Disconnect？

Transport owner 触发 CLIENT_DISCONNECTED，停止 wire 写入，取消并 bounded drain producer；后台 worker 事实继续保留，不能因断连伪造终止。

### 7）Snapshot 和 Recovery 做到什么程度？

Snapshot 是默认关闭的 v1 opt-in 安全投影；RecoveryValidator 校验 schema、digest、watermark、tail 和 side-effect evidence。没有自动 resume/replay/result rehydration。

### 8）如何设计 Observability 和 Trace？

Journal 是业务权威，Observability/Trace 是有界、低基数、敏感字段 allowlist 的派生诊断。失败只更新 health/drop counters，不修改 AgentState 或重跑业务。

### 9）Shutdown 如何确保资源安全？

先关闭 admission，再 cancel/drain run、force abort remaining、关闭 worker admission并 drain，然后 flush/close。Model 只有在 worker idle 被证明后关闭；最终同时看 fully_closed 与 deferred/unknown。

### 10）Fault Injection 如何不污染生产逻辑？

Controller 只能由测试显式 Scope 注入，生产 Settings/API/Prompt/Tool 参数没有入口，默认 controller=None。它不拥有 retry/recovery，32 个真实 seam 全有确定性测试，10 个只保留 contract。

### 11）遇到过哪些最难的 Bad Case？

高价值案例包括 terminal publication 异常跳过 cleanup、after-save 部分持久化、EventPublicationError 意外持有完整 Event、Trace start/end fault 破坏 context，以及 detached worker 下误关 Model。共同做法是回到 authority、owner 和不可逆阶段。

### 12）当前系统还有哪些限制？

无自动 recovery/replay、step result rehydration、cross-process registry、exactly-once、自动补偿、生产 fault activation、随机 chaos 和分布式 durable execution；自定义流协议、进程内 metrics/trace 与生产验证也仍需下一阶段处理。
