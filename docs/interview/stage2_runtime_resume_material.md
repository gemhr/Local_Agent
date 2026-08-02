# Stage2 Runtime Resume Material

以下素材只描述代码、文档和自动化测试证据，不包含生产用户规模、商业结果、性能提升百分比或生产延迟。

## 1. 简历精简版

- 将 LocalAgent 默认请求路径迁移到 Coordinated Runtime（协调式运行时），建立 RunContext、AgentState、预算、取消、并行调度和资源所有权边界，同时保留显式 Legacy 回滚且禁止失败后跨 Runtime 重跑。
- 设计 Model/Tool/Retrieval 强类型执行合同：统一 Retry/Fallback owner，以幂等与单调 side-effect evidence 约束 Tool 重试，区分 Retrieval 可控降级与 fail-closed 失败。
- 建设 Journal-first 事件流、Snapshot opt-in 与只读 Recovery Validation；覆盖部分持久化、证据损坏、Client Disconnect、Detached Worker 和 Graceful Shutdown（优雅关闭）。
- 建立测试专用确定性 Fault Injection，审计 42 个 Fault Point（32 Supported、10 Contract-only），并完成 Contract Freeze、20 场景 RC Gate 与 Operations/Security Runbook。
- 阶段二最终通过 `1089` 个自动化测试及 42 个附加 subtests；该数字来自本轮 `pytest --collect-only` 固定生成输入并由全仓执行复核；结论为 code-level RC PASS，不等同生产容量或容灾认证。

## 2. 项目详细版

项目目标是把原本由 Router/AgentLoop 混合承担的调用、状态、事件和关闭职责拆成可验证 Runtime。生产 Composition Root 统一在 FastAPI lifespan，Application 资源与 Run/Invocation/Attempt scope 分离；每个运行只有一个状态、sequence、terminal 与 cancellation owner。

执行侧实现 Model routing/retry/candidate fallback/circuit、Tool invocation/attempt/idempotency/evidence、Retrieval stage runtime、budget/deadline/cancellation 与 ParallelExecutor。持久与诊断侧实现 append-only Journal-first、版本化 Snapshot、只读 Recovery Validation、低基数 Observability/Trace、disconnect cleanup 和 worker-aware shutdown。

可靠性侧没有宣称 exactly-once 或自动恢复，而是对 committed/unknown/partial/corrupted 等不可逆窗口 fail closed；使用显式测试 Scope 的确定性 Fault Injection 覆盖调用、持久化、诊断和关闭 seam，最终通过 evidence manifest、capability/owner/schema matrix、RC scenario matrix、Release Gate 和 Operations Runbook 冻结边界。

## 3. 面试口述版（约 2 分钟）

我在 LocalAgent 阶段二主要做了一次 Runtime 工程化改造。原系统能调用模型和工具，但状态、取消、事件和资源关闭的 owner 不够清楚。我先建立 RunContext、AgentState 和 Scope Matrix，让每个请求只有一个运行身份、取消源、事件通道、sequence 和 terminal owner，再由 Coordinator/Scheduler 驱动 Model、Tool 和 Retrieval。

Model 的 retry 只由 RetryExecutor 决定，candidate fallback 不会切 Legacy；Tool 用幂等合同和单调副作用证据避免 committed 后重试；Retrieval 把 rewrite 失败做受控降级，但 search 错误 fail closed。事件采用 Journal-first，先落安全持久事实再发 channel，所以部分发布不会重做业务。Snapshot 是默认关闭的 opt-in 能力，Recovery 目前只做只读 validation，没有自动 replay/resume。

我还补齐了 Observability、Trace、Client Disconnect 和 Graceful Shutdown。尤其是同步 worker 无法安全强杀，所以 detached 必须保留记录，Model close 只有在 worker idle 被证明后才允许。最后用 42 个确定性 Fault Point 合同、20 个 RC 场景、`1089` 个自动化测试和 Operations Runbook 收口。测试数来自本轮固定收集输入并由全仓执行复核。最终结论是 Stage2 Runtime RC1 code-level gate PASS，生产容量、外部依赖和容灾仍需下一阶段验证。
