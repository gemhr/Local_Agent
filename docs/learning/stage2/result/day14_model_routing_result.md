# 阶段二第 14 天改造结果

## 1. 本次目标与调整

本次先建立统一 Model Invocation Boundary，再在边界内实现 Model Routing、
Escalation、Fallback 和 Circuit Breaker。生产目标链路为：

```text
RunCoordinator
→ _CoordinatedSingleAgentDriver
→ AgentRouter.complete_single_agent
→ ModelSelectionPolicy
→ ModelRoutingPolicy
→ ModelInvocationRouter
→ ModelCircuitBreakerRegistry
→ BudgetLedger.reserve
→ ModelAdapterResolver
→ ModelAdapter.invoke
```

本次没有实现 Retry、Backoff、Jitter、Retry-After 或 Retry Budget。同一个
Profile 在一个 Routing Chain 内最多产生一个 Attempt。

## 2. 修改前模型选择和调用边界

修改前 `_complete_final_response` 先调用 `ModelSelectionPolicy`，随后仍由
`AgentRouter` 自行执行 `ModelResolver.resolve(...).generate(...)`，预算预留也
由 Router 的 `_reserve_model_call` 单独完成。Selection 与真实 Client 之间没有
统一执行边界，Fallback、跨 Run Breaker 和统一 Failure Taxonomy 均不存在。

真实调用入口盘点如下：

| 入口 | 底层调用 | 本次状态 |
|---|---|---|
| Coordinated 非流式单 Agent 最终回答 | 原 `_complete_final_response` | 已迁移到 InvocationRouter |
| 默认 `_stream_final_response` | `selected_model.generate` | Legacy，未迁移 |
| Legacy `_complete_final_response` 分支 | `selected_model.generate` | Legacy，未迁移 |
| `_collect_model_response` | `self.llm.generate` | Legacy，未迁移 |
| 对话摘要 `_distill_summary` | 经 `_collect_model_response` | Legacy，未迁移 |
| 知识查询改写 `_rewrite_knowledge_query` | 经 `_collect_model_response` | Legacy，未迁移 |
| Tool Planner `_plan_tool_call` | 经 `_collect_model_response` | Legacy，未迁移 |
| 多 Agent 编排规划 `_plan_orchestration` | 经 `_collect_model_response` | Legacy，未迁移 |
| Local Adapter | `LocalLLMEngine.generate` → llama.cpp | 仅迁移入口经统一 Adapter |
| Remote Adapter | `RemoteLLMEngine.generate` → OpenAI-compatible HTTP | 仅迁移入口经统一 Adapter |

## 3. 三个模型身份

- `capability_preferred_profile_id`：能力和质量层面的首选，来自 Selection；
- `initial_selected_profile_id`：预算、目标和偏好调整后的首次调用 Profile；
- `executed_profile_id`：唯一成功产生最终结果的 Profile。

`ModelRoutingDecision` 保存前两个身份，`ModelInvocationResult` 在成功时补充
`executed_profile_id`。`ModelInvocationFailure.executed_profile_id` 固定为
`None`，不会把最后一个失败 Profile 误记为 executed。

## 4. Provider Failure Taxonomy

`ModelFailureCategory` 包含：

- `TRANSIENT_PROVIDER_FAILURE`
- `RATE_LIMITED`
- `PROVIDER_TIMEOUT`
- `PROVIDER_CONFIGURATION_ERROR`
- `CONTEXT_LIMIT_EXCEEDED`
- `INVALID_REQUEST`
- `OUTPUT_VALIDATION_FAILED`
- `SAFETY_REFUSAL`
- `BUSINESS_FAILURE`
- `CANCELLED`
- `DEADLINE_EXCEEDED`
- `BUDGET_EXHAUSTED`
- `CIRCUIT_OPEN`
- `UNKNOWN_FAILURE`

`classify_model_failure` 仅使用异常类型、HTTP 状态码及
`model_failure_category`、`safety_refusal`、`safe_error_code` 等安全属性。
它不检查异常正文。Remote Client 的 HTTP/JSON/空输出异常已改成
`RemoteLLMError`，不再保存 Provider 原始响应正文。

`CIRCUIT_OPEN` 是当前候选在调用前的 admission 拒绝，不是一次 Provider
Failure。它不调用 Adapter、不取得 Permit、不预留模型预算，也不增加 Breaker
failure count；在 FORCE 和硬能力仍允许时，路由可以跳到下一个唯一候选。因此
它不属于“普通失败允许 Fallback”，而是“未开始候选的安全跳过”。

## 5. ModelRoutingPolicy

`ModelRoutingPolicy.route` 是纯策略函数。输入包括 Selection Decision、硬能力、
最终 Context 需求、Profile、FORCE/AUTO、BudgetSnapshot 和 BreakerSnapshot；
输出稳定、去重的 `ModelRoutingDecision`，不调用模型、不预留预算、不修改
Breaker 或 AgentState。

候选顺序固定为 initial selected 在前，其余 Profile 按装配顺序追加。每个候选
重新检查 Context 和 tools/structured output/code reasoning/long reasoning 硬能力。
本地/远程使用 `ModelProfile.is_remote`，Breaker 使用
`ModelProfile.breaker_key`；不从模型名称推断。旧 Profile 仅为兼容测试使用固定
`ModelProfileId` 契约，生产装配均显式提供这两个字段。

未知成本在 Invocation 预留中使用非零保守占位；当 Run 配置成本或远程调用上限
而 Profile 缺少成本元数据时，路由判定为不可行。Fallback 有 Deadline 时若候选
延迟未知，则不会把未知延迟当成零后继续调用。

## 6. Fallback Chain

默认允许切换：

- `TRANSIENT_PROVIDER_FAILURE`
- `PROVIDER_TIMEOUT`
- `RATE_LIMITED`

条件允许：

- `CONTEXT_LIMIT_EXCEEDED`：下一 Profile 必须有更大的显式 context window；
- `CIRCUIT_OPEN`：跳过当前未开始候选；在 FORCE 和硬能力允许时可检查下一个
  唯一候选，Open 拒绝不计 Provider Failure。

默认禁止切换：

- `PROVIDER_CONFIGURATION_ERROR`
- `INVALID_REQUEST`
- `OUTPUT_VALIDATION_FAILED`
- `SAFETY_REFUSAL`
- `BUSINESS_FAILURE`
- `CANCELLED`
- `DEADLINE_EXCEEDED`
- `BUDGET_EXHAUSTED`
- `UNKNOWN_FAILURE`

Router 不等待、不重试相同 Profile；Fallback 只前进到候选链中的下一个唯一
Profile。AUTO 下 local OPEN 可升级到可行 remote；FORCE_LOCAL 下不得逃逸到
remote；FORCE_REMOTE 下可以在多个显式 remote Profile 之间切换。没有合法候选
时终止 Chain。

## 7. Escalation 与降级

- Local → Remote：`ESCALATE_TO_REMOTE`，仅 AUTO 且
  `allow_escalation=True` 时加入候选；
- Remote → Local：`DOWNGRADE_TO_LOCAL`，仅 AUTO 且
  `allow_downgrade=True`、硬能力和 Context 均满足时加入；
- 同范围切换：`SWITCH_SAME_TIER`；
- 首次候选：`NONE`。

真正执行远程降级本地，或 Selection 已因预算把 capability preferred remote
调整为 initial local 时，成功结果的 `quality_tradeoff_disclosed=True`。仅仅在
链尾存在一个未执行的本地候选，不会把 remote 首次成功误记为发生了质量降级。

## 8. FORCE_LOCAL / FORCE_REMOTE

- `FORCE_LOCAL` 只保留 `is_remote=False` 的显式 Profile，本地失败不能升级；
- `FORCE_REMOTE` 只保留 `is_remote=True` 的显式 Profile，远程失败不能降级；
- `AUTO` 才能按配置跨范围切换。

策略可配置 `require_confirmation_for_downgrade=True`。此时 Decision 为
`confirmation_required=True`、候选为空、executed 不存在，InvocationRouter
直接拒绝调用。

## 9. ModelAdapterResolver

`ModelAdapterResolver` 保存显式 `profile_id → ModelAdapter` 映射。未知 Profile
抛 `ModelAdapterResolutionError`，`provider_started=False`，释放已预留预算并
abandon Permit。Resolver 不从 Profile 名称推断 Client。

`GeneratorModelAdapter` 只调用一次既有 Engine 的 `generate` 并收集非流式
输出；它不做 Retry、Fallback、RunStatus 修改或独立 BudgetLedger 创建。

## 10. ModelInvocationRouter

统一入口为 `ModelInvocationRouter.invoke(...)`，接收 RunContext、BudgetLedger、
RoutingDecision、messages、AdapterResolver、BreakerRegistry、token estimate、
max tokens 和 `output_started`。

顺序为：

1. 检查确认状态与 Token 参数；
2. 检查 Cancellation 和 Run Deadline；
3. 取唯一候选并复验 Context；
4. 获取 Circuit Permit；
5. 原子 reserve 本 Attempt 的 Budget；
6. 再次检查 Cancellation、Deadline 和候选延迟；
7. 解析 Adapter 并执行一次调用；
8. 已开始调用无论成功失败均 commit Actual 或保守 Estimated；
9. 未开始调用则 release Reservation、abandon Permit；
10. 记录 Breaker Outcome；
11. 成功返回 executed Profile；
12. 失败分类并按策略决定是否进入下一个 Profile；
13. 候选耗尽后抛只含安全元数据的 `ModelInvocationChainError`。

## 11. Attempt 与安全结果

`ModelInvocationAttempt` 字段为：

- `attempt_index`
- `profile_id`
- `breaker_key`
- `started`
- `succeeded`
- `failure_category`
- `safe_error_code`
- `routing_adjustment`
- `usage_source`

Attempt 不保存 messages、Prompt、用户正文、模型输出、Tool 参数、RAG、Memory、
Secret 或原始异常。Circuit Open、Budget reserve 失败和 Adapter 未解析均记录
`started=False`；只有成功 Profile 才进入 executed。

## 12. Budget / Deadline / Cancellation

每个真实 Attempt 独立预留：

- `model_calls=1`
- `remote_model_calls`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `cost_units`

Primary 开始后失败会 commit Actual 或保守 Estimated，不会 release；Fallback
重新读取 Ledger 并单独 reserve，因此 Selection Snapshot 不构成调用授权。
Circuit Open 在 reserve 之前拒绝，不消费模型、Token 或成本预算。Half-open
Permit 取得后若 reserve 失败会 abandon，不记录 Provider 失败。

`USER_CANCELLED`、`CLIENT_DISCONNECTED`、`SYSTEM_SHUTDOWN`、
`DEADLINE_EXCEEDED` 和 `BUDGET_EXHAUSTED` 立即终止候选链。它们继续以原异常
类型返回 Executor/Coordinator，不会泛化为 Business Failure。

## 13. Partial Output 规则

第一版规则固定为：

```text
output_started=False → 可按安全策略 Fallback
output_started=True  → 禁止透明 Fallback
```

`GeneratorModelAdapter` 在收集到任意 chunk 后发生异常，会把
`output_started=True` 写入安全 Adapter 异常。InvocationRouter 不拼接两个模型
的正文。当前迁移入口是非流式；默认 HTTP 自定义分块流未修改。

## 14. Circuit Breaker

`ModelCircuitBreaker` 使用注入的 monotonic clock、同步锁和连续合格失败计数。
锁内只有状态判断、计数和 Permit 管理，没有 Provider 调用或 await。

默认计入：

- `TRANSIENT_PROVIDER_FAILURE`
- `PROVIDER_TIMEOUT`
- `RATE_LIMITED`（由固定配置 `count_rate_limited=True` 控制）

默认不计入 Safety、业务、取消、Deadline、Budget、Circuit Open、配置、参数、
输出校验和 Unknown 错误。

Routing Failure 与 Circuit Health Outcome 独立。`CircuitHealthOutcome` 包含：

- `NOT_STARTED`：Provider 从未开始，仅此情况允许 `Permit.abandon()`；
- `HEALTHY_COMPLETION`：Provider 已正常响应，但 Routing 不接受结果；
- `QUALIFYING_PROVIDER_FAILURE`：瞬时故障、Timeout，以及按配置计数的 Rate
  Limit；
- `INDETERMINATE_COMPLETION`：Provider 已开始，但无法证明健康或基础设施故障。

Safety Refusal、Business Failure、Output Validation Failure，以及明确收到
Provider 响应的 Invalid Request 使用 `HEALTHY_COMPLETION`。它们仍是 Routing
Failure、仍禁止 Fallback，但 CLOSED 下会重置连续基础设施失败计数，HALF_OPEN
下会完成 Probe 并回到 CLOSED。Provider 已开始但结果不确定时使用
`record_indeterminate()`：CLOSED 保留原计数，HALF_OPEN 释放 Probe 后保守回到
OPEN，绝不使用 abandon。

## 15. CLOSED / OPEN / HALF_OPEN

- `CLOSED`：阈值前继续放行；
- 达到 `failure_threshold`：转为 `OPEN`；
- `OPEN`：快速拒绝，不产生 Permit；
- monotonic 冷却达到 `recovery_timeout_seconds`：惰性转为 `HALF_OPEN`；
- Probe 成功：回到 `CLOSED` 并清零计数；
- Probe 失败：重新 `OPEN`。
- Probe 返回 Safety、Business、Output Validation 或已响应的 Invalid Request：
  Routing 失败，但 Circuit 视为健康完成并回到 `CLOSED`；
- Probe 已开始但健康结论不确定：完成 Permit 并保守回到 `OPEN`，不会永久占用
  Probe。

生产默认值由 Settings 显式提供：

- `failure_threshold=3`
- `recovery_timeout_seconds=30`
- `half_open_max_calls=1`
- `count_rate_limited=True`

这些默认值需要结合真实 Provider SLO 人工确认。

## 16. Circuit Permit

Permit 提供：

- `record_success`
- `record_failure`
- `record_indeterminate`
- `abandon`

`acquire_permission` 在 Breaker 上。Half-open 并发 Probe 数受锁保护，默认只允许
一个。Permit 只能完成一次，重复完成抛 `CircuitPermitStateError`。没有真正调用
Provider 的 Budget reserve、Adapter resolve、调用前取消、调用前 Deadline 和
显式 `provider_started=False` 使用 `abandon`。`abandon` 的唯一合法语义是
`NOT_STARTED`；Provider 一旦开始，所有路径都必须以 success、failure 或
indeterminate 完成 Permit。

## 17. Breaker Registry Owner

`server.lifespan` 创建一个 `ModelCircuitBreakerRegistry`，作为应用生命周期依赖
注入应用级 `AgentRouter`。`ChatService` 和所有 Coordinated Run 复用该 Router，
因此 Breaker 跨 Run 共享，不由 RunCoordinator per-run 创建。

测试或非 Server 装配若未显式注入，AgentRouter 也只在自身构造时创建一次
Registry；仍是 per-router，而不是 per-run。Registry 仅单进程内存级，不持久化，
多 Worker 不共享。多个 Profile 可通过同一个显式 `breaker_key` 共享 Breaker。

## 18. 与 RunCoordinator 集成

RunCoordinator 仍只拥有 Run/Step 生命周期、Scheduler/Executor 驱动、取消、
Budget 总账和最终状态；不实现 Model Fallback，也不修改 Breaker。

`_CoordinatedSingleAgentDriver` 只调用 `AgentRouter.complete_single_agent`，并
接收安全 `ModelInvocationResult`。Fallback 成功时 Driver 正常返回，Step/Run
为 SUCCEEDED；候选全部失败时异常回到 Executor，Step/Run 为 FAILED。
BudgetExceeded、Deadline 和 Cancellation 仍由既有专用 Coordinator 分支处理。

## 19. 最小真实入口迁移

已迁移：

```text
ChatService.run_coordinated_agent
→ _CoordinatedSingleAgentDriver.execute
→ AgentRouter.complete_single_agent
→ _run_agent_once(unified_invocation=True)
→ _complete_final_response(unified_invocation=True)
→ Selection → Routing → Invocation → Adapter
```

集成测试使用 Fake Local/Remote Adapter，但经过真实 ChatService、AgentRouter、
Driver、Scheduler、ParallelExecutor 和 RunCoordinator。测试证明 selected
Profile 是首个 Resolver 输入、允许故障会切换、Driver/Coordinator 没有第二套
Fallback、全部失败会使 Step/Run FAILED。

## 20. Legacy 与未迁移路径

以下路径仍绕过 InvocationRouter，也不受新 Circuit Breaker 保护：

- 默认 `ChatService.stream_chat` → `_stream_final_response`；
- Legacy `_complete_final_response` 分支；
- `_collect_model_response`；
- 对话摘要；
- 知识查询改写；
- Tool Planner；
- 多 Agent 编排规划；
- 多 Agent 专家调用与 synthesis；
- 其他直接使用 Engine 的外部调用。

这些路径仍可使用旧 Selection/Resolver 或 `self.llm`，本次没有宣称全项目模型
调用已统一。默认 HTTP/UI、自定义流协议均未修改。

## 21. 隐藏 Retry 检查

`RemoteLLMEngine` 改为持有显式 `requests.Session`，并为 HTTP/HTTPS 安装
`HTTPAdapter(max_retries=0)`。回归测试检查 `total == 0` 且 `read is False`。
`GeneratorModelAdapter` 对每个 Profile 只调用一次；Routing Chain 去重；没有
sleep、Backoff、Jitter、Retry-After 或相同 Profile 第二次调用。

Provider 专属参数也改为显式 `remote_provider_kind` 配置，不再根据模型名称或
URL 猜测 Provider。

Remote Session 属于 application-scoped `RemoteLLMEngine`，可能被多个 Run
共享。`requests.Session` 不被假设为线程安全；Engine 使用专用同步锁包围
`Session.post`，同一 Engine 同时最多一个 Session 调用。该串行边界只保护共享
HTTP Client，不改变 ModelInvocationRouter 的 per-run Budget、Cancellation 或
共享 Breaker 语义。可控双线程测试证明两个并发 Run 均成功、各自只消费一次
模型预算，Session `max_active=1`。

`RemoteLLMEngine.close()` 使用同一把锁：会等待活跃请求返回，不主动强杀调用，
并且多次调用幂等。`server.lifespan` 先取消 Run、等待 RunRegistry Grace Period，
再关闭 Engine；单个 close 异常只记录安全日志，不覆盖 Shutdown，也不阻止其余
Engine 关闭。

## 22. 重点 Bad Case

### Bad Case 1：Selection 选远程但调用仍硬编码本地

- 类型：真实发现。
- 触发条件：Selection 返回 remote，Router 随后仍使用旧 `self.llm`。
- 故障表现：决策 Profile 与执行 Client 不一致。
- 根因分析：缺少统一 Invocation Boundary。
- 修复方案：Decision 进入 Routing，首候选 Profile 必须经 AdapterResolver。
- 回归测试：`test_initial_selection_reaches_resolver_and_adapter_once`。
- 对应知识点：Policy 与执行边界分离。
- 面试表达：Selection 只是决策，Invocation 才是唯一调用授权。
- 当前状态：Coordinated 非流式单 Agent 已修复；Legacy 仍待迁移。

### Bad Case 2：Fallback 变成同模型 Retry

- 类型：假设构造。
- 触发条件：瞬时失败后重新调用同一个 Profile。
- 故障表现：调用次数失控，Attempt 无法解释。
- 根因分析：候选链未去重或 Adapter 内隐藏重试。
- 修复方案：候选去重、seen 集合复验、HTTP max_retries=0。
- 回归测试：`test_transient_failure_falls_back_without_retry` 和隐藏 Retry 测试。
- 对应知识点：Fallback 不等于 Retry。
- 面试表达：本日只横向切换唯一 Profile，不纵向重试。
- 当前状态：已防护。

### Bad Case 3：Safety Refusal 被计入熔断

- 类型：假设构造。
- 触发条件：模型安全拒绝被当作 Provider 故障。
- 故障表现：正常安全策略导致 Provider Circuit OPEN。
- 根因分析：异常统一按 Exception 计失败。
- 修复方案：类型化分类；Safety 禁止 Fallback，但 Provider 已响应时以
  `HEALTHY_COMPLETION/record_success` 完成 Permit，不能 abandon。
- 回归测试：`test_safety_invalid_and_unknown_do_not_fallback`、
  `test_healthy_routing_failure_resets_consecutive_provider_failures`、
  `test_half_open_non_infrastructure_results_close_probe`。
- 对应知识点：业务/安全结果与基础设施健康分离。
- 面试表达：Breaker 只观察 Provider 健康，不评价业务答案。
- 当前状态：已防护。

### Bad Case 4：Half-open 探测雪崩

- 类型：假设构造。
- 触发条件：冷却结束后多个线程同时发 Probe。
- 故障表现：故障 Provider 瞬间承受大量请求。
- 根因分析：状态转换和 Probe 数量没有原子保护。
- 修复方案：锁内惰性 HALF_OPEN 转换和 Permit 计数。
- 回归测试：`test_half_open_probe_limit_is_thread_safe`。
- 对应知识点：Half-open 单探针。
- 面试表达：锁只保护许可，不包围真实网络调用。
- 当前状态：已防护。

### Bad Case 5：Primary 失败后回滚真实预算

- 类型：真实风险。
- 触发条件：Provider 已开始后抛异常，调用方 release Reservation。
- 故障表现：实际调用不计费，Fallback 可超预算。
- 根因分析：未区分调用前失败和调用后失败。
- 修复方案：`provider_started=True` 时 commit Actual 或 Estimated。
- 回归测试：`test_transient_failure_falls_back_without_retry` 检查两次 committed。
- 对应知识点：预算是真实副作用账本。
- 面试表达：开始边界之后失败也必须结算。
- 当前状态：已修复。

### Bad Case 6：降级到不满足 Context 的本地模型

- 类型：假设构造。
- 触发条件：remote 失败后只按成本选择小窗口 local。
- 故障表现：Fallback 必然再次 context overflow 或截断必要正文。
- 根因分析：Fallback 没有重验硬约束。
- 修复方案：Routing 与 Invocation 均检查 required context。
- 回归测试：`test_hard_capability_and_context_filter`、
  `test_context_overflow_only_switches_to_larger_window`。
- 对应知识点：降级仍须满足 hard constraints。
- 面试表达：便宜候选不是可行候选。
- 当前状态：已防护。

### Bad Case 7：部分输出后透明切换

- 类型：假设构造。
- 触发条件：首模型输出 chunk 后异常，Router 启动第二模型。
- 故障表现：两个模型正文被拼接，语义和审计均失真。
- 根因分析：未把 output started 纳入 Fallback 决策。
- 修复方案：任意输出后禁止透明切换。
- 回归测试：`test_partial_output_forbids_transparent_fallback`。
- 对应知识点：流式提交点。
- 面试表达：第一字节之后必须显式失败，不能偷偷换模型。
- 当前状态：统一 Adapter 已防护；默认 Legacy 流尚未迁移。

### Bad Case 8：每个 Run 创建独立 Breaker

- 类型：真实架构风险。
- 触发条件：RunCoordinator 构造时创建 Breaker。
- 故障表现：连续故障被每个 Run 清零，永远无法 OPEN。
- 根因分析：Registry 生命周期归属错误。
- 修复方案：Registry 由应用生命周期/AgentRouter 持有。
- 回归测试：`test_registry_returns_same_breaker_across_runs`。
- 对应知识点：健康状态必须跨请求共享。
- 面试表达：Budget per-run，Breaker cross-run。
- 当前状态：单进程内已修复。

### Bad Case 9：Selection Snapshot 过期后仍强行调用

- 类型：真实并发风险。
- 触发条件：Selection 看到预算可用，调用前其他任务已占用预算。
- 故障表现：check-then-act 导致超支。
- 根因分析：把只读 Snapshot 当作 Reservation。
- 修复方案：每个 Attempt 调用前重新由 Ledger 原子 reserve。
- 回归测试：Budget reserve/Permit abandon 与既有 Budget 并发测试。
- 对应知识点：Snapshot 只用于策略，Reservation 才是授权。
- 面试表达：决策可过期，副作用边界必须重新原子校验。
- 当前状态：已防护。

### Bad Case 10：Provider 已开始后的非基础设施结果错误 abandon Permit

- 类型：补充审查真实发现。
- 触发条件：Provider 已开始并返回 Safety Refusal、Business Failure、Output
  Validation Failure 或响应型 Invalid Request，Invocation 仅因这些类别不计
  Breaker Failure 而统一调用 `Permit.abandon()`。
- 故障表现：HALF_OPEN Probe 不能形成健康结论，可能长期停留在 HALF_OPEN；
  CLOSED 下此前的连续基础设施失败计数也不会被健康响应重置。
- 根因分析：Routing 是否接受结果与 Circuit 是否观察到健康响应共用一套二元
  success/failure 判断；`abandon` 没有限定为 `provider_started=False`。
- 修复方案：独立建立 `CircuitHealthOutcome`；已正常响应的非基础设施结果使用
  `HEALTHY_COMPLETION/record_success`，合格 Provider 故障使用
  `record_failure`，已开始但健康不确定使用 `record_indeterminate`，仅未开始允许
  `abandon`。
- 回归测试：`test_healthy_routing_failure_resets_consecutive_provider_failures`、
  `test_half_open_non_infrastructure_results_close_probe`、
  `test_provider_started_paths_never_abandon_permit`、
  `test_routing_failure_and_circuit_health_are_independent`。
- 对应知识点：Routing Failure 与 Circuit Health Outcome 分离、Half-open Probe
  生命周期、连续失败重置、Permit exactly-once。
- 面试表达：业务结果失败不代表 Provider 不健康；只要 Provider 已经开始，
  Permit 就必须以明确健康结论完成，不能 abandon。
- 当前状态：已修复。

### Bad Case 11：Application-scoped requests.Session 被无保护并发复用且缺少关闭钩子

- 类型：补充审查真实发现。
- 触发条件：多个并发 Run 复用 application-scoped AgentRouter 和
  `RemoteLLMEngine`，同时访问同一个 `requests.Session`；应用退出时没有显式
  close。
- 故障表现：代码隐含依赖 Session 线程安全，连接状态可能发生竞态；Shutdown
  后连接池资源无法按确定顺序释放，直接 close 又可能干扰活跃请求。
- 根因分析：Remote Engine 生命周期提升为 application scope 后，没有同步调整
  Client 并发模型和资源清理所有权。
- 修复方案：Remote Engine 使用专用同步锁串行保护 `Session.post`；
  `close()` 复用同一把锁并保持幂等；lifespan 先取消 Run、等待 RunRegistry
  Grace Period，再关闭 Engine，单个关闭异常不覆盖其余 Shutdown。
- 回归测试：
  `test_two_runs_share_remote_engine_without_concurrent_session_access`、
  `test_remote_session_close_is_idempotent`、
  `test_remote_session_close_waits_for_active_call`、
  `test_shutdown_close_errors_do_not_skip_other_engines`。
- 对应知识点：application-scoped Client 所有权、线程安全、Graceful Shutdown、
  资源关闭异常隔离。
- 面试表达：共享 Client 的生命周期和并发保证必须显式匹配；不能默认
  `requests.Session` 线程安全，也不能在活跃请求中强行关闭。
- 当前状态：已修复；串行访问对生产吞吐的影响仍需人工确认。

## 23. 测试命令和结果

使用项目可用的等价 `uv run python` 命令：

```text
uv run python -m pytest \
  tests/test_model_routing.py \
  tests/test_model_circuit_breaker.py \
  tests/test_model_invocation.py \
  tests/test_remote_llm_engine.py \
  tests/test_run_coordinator.py \
  tests/test_budget.py -q
```

结果：`76 passed, 16 subtests passed`。

```text
uv run python -m unittest \
  tests.test_runtime_context \
  tests.test_agent_state \
  tests.test_agent_loop \
  tests.test_state_machine \
  tests.test_model_context \
  tests.test_planning \
  tests.test_model_selection \
  tests.test_scheduler \
  tests.test_plan_graph \
  tests.test_parallel_execution \
  tests.test_budget \
  tests.test_run_registry \
  tests.test_timeout_cancellation \
  tests.test_run_coordinator \
  tests.test_model_routing \
  tests.test_model_circuit_breaker \
  tests.test_model_invocation \
  tests.test_remote_llm_engine -q
```

结果：`Ran 169 tests ... OK`。

```text
uv run python -m pytest -q
uv run python -m compileall -q core tests
git diff --check
```

结果：全量 `262 passed, 42 subtests passed`；compileall 通过；
`git diff --check` 通过，仅有工作区既有的 LF/CRLF 转换提示。

补充审查新增覆盖：

- AUTO / FORCE_LOCAL / FORCE_REMOTE 的 Circuit Open 跳过边界；
- Open Attempt 的零预算、零新增 failure count 与 Profile 唯一性；
- transient → Safety → transient 的连续失败重置；
- HALF_OPEN Safety / Business / Output Validation / Provider Invalid Request；
- Budget、Resolver 未开始路径的 abandon；
- Provider 已开始路径禁止 abandon；
- 两个并发 Run 共享 Remote Engine 时 Session `max_active=1`；
- Remote Session 幂等 close、等待活跃调用和关闭异常隔离。

所有新增测试均使用 Fake Adapter/Fake Clock 或本地临时数据库；没有调用真实模型、
Provider、外部网络、Chroma 或 UI。

## 24. 未完成事项和已知风险

- 本次没有 Retry；
- 只迁移 Coordinated 非流式单 Agent 最终回答；
- 默认流式聊天、多 Agent、摘要、Tool Planner、知识改写仍未接入；
- Partial Output 后统一边界不 Fallback，Legacy 流仍待后续迁移；
- Breaker 仅单进程内存级；
- 多 Worker 不共享 Breaker；
- Breaker 状态未持久化；
- Provider 错误分类依赖 Adapter 和显式安全属性；
- Unknown 错误保守终止；
- 成本、延迟、阈值、冷却时间和 breaker key 需要生产确认；
- Provider Actual Usage 尚未接入，缺失时按 Estimated 结算；
- 业务回答输出通道仍待未来 Runtime Event，本次没有实现；
- Tool / RAG Runtime Contract 未统一；
- Coordinated 入口尚未接到默认 HTTP/UI；
- 旧 Profile 的 local/remote 兼容规则仍存在，生产装配已显式化；
- 单个 Remote Engine 的共享 Session 采用串行访问，生产吞吐与资源分片策略需要
  结合 Provider 限流和多 Engine 部署方式确认；
- shutdown close hook 已接入，但进程被强杀时仍不保证执行应用级清理。

## 25. 面试表达

第 14 天把“选哪个模型”和“真正调用哪个模型”之间补上统一执行边界：
Selection 产生首次 Profile，Routing 只生成满足 FORCE、能力、Context、预算和健康
约束的唯一候选链，Invocation 在每次副作用前原子预留预算并取得 Circuit Permit，
Adapter 只执行一个 Profile 一次。故障按安全 Taxonomy 决定是否切换，已经产生
输出、取消、Deadline、Budget、安全拒绝和 Unknown 都不会透明 Fallback。
Budget 是 per-run，Breaker 是 application-scoped cross-run；RunCoordinator
只管理生命周期，不承担模型 Fallback。

## 26. 需要带回 ChatGPT 审查的信息

- 新增文件：
  `core/runtime/model_routing.py`、
  `core/runtime/model_invocation.py`、
  `core/runtime/circuit_breaker.py`、
  `tests/test_model_routing.py`、
  `tests/test_model_circuit_breaker.py`、
  `tests/test_model_invocation.py`、
  本结果文档；
- 修改文件：
  `core/runtime/model_selection.py`、
  `core/runtime/__init__.py`、
  `core/agent_router.py`、
  `core/chat_service.py`、
  `core/llm_engine.py`、
  `core/settings.py`、
  `server.py`、
  `tests/test_remote_llm_engine.py`；
- 统一 Invocation 入口：`ModelInvocationRouter.invoke`；
- 三个身份：capability preferred / initial selected / executed；
- Candidate Chain：initial first、稳定、去重、每 Profile 最多一次；
- Failure Category：共 14 类，Unknown 保守终止；
- 允许 Fallback：transient、timeout、rate limit，context overflow 条件允许；
  Circuit Open 是未开始候选跳过，不是 Provider Failure；
- 禁止 Fallback：配置、参数、输出校验、安全、业务、取消、Deadline、Budget、
  Unknown；
- FORCE：本地和远程候选严格隔离；
- 升级：AUTO local → remote；
- 降级：AUTO remote → local，必须满足硬能力并披露质量取舍；
- Partial Output：任何输出后禁止透明切换；
- Adapter Resolver：显式 `profile_id → adapter`；
- Attempt：九个安全字段，不含正文；
- Budget：Permit 后按 Attempt reserve，开始后失败 commit，Fallback 重 reserve；
- Deadline / Cancel：链立即终止并保留专用信号；
- Circuit：CLOSED / OPEN / HALF_OPEN；
- 默认阈值：3；
- 默认恢复时间：30 秒；
- 默认 half-open Probe：1；
- Circuit Health Outcome：NOT_STARTED / HEALTHY_COMPLETION /
  QUALIFYING_PROVIDER_FAILURE / INDETERMINATE_COMPLETION；
- Circuit Permit：success / failure / indeterminate / abandon，exactly once；
- abandon：唯一适用于 `provider_started=False`；
- breaker key：生产 Profile 显式配置；
- Registry Owner：FastAPI lifespan 创建并注入 application-scoped AgentRouter；
- 跨 Run：同进程共享，多 Worker 不共享；
- RunCoordinator：不实现 Model Fallback；
- 已迁移：Coordinated 非流式单 Agent 最终回答；
- 未迁移：默认流、Legacy complete、摘要、改写、Tool Planner、多 Agent；
- Remote Session：application-scoped，由 Engine 专用锁串行保护；
- Remote Session close：Grace Period 后幂等关闭，关闭异常不覆盖 Shutdown；
- 隐藏 Retry：Session 显式 `max_retries=0`，同 Profile 不重复；
- 测试结果：目标 76、unittest 169、全量 262，均通过；
- Bad Case：十一项已按真实发现、补充审查发现或假设构造标注；
- 人工确认：生产成本/延迟、Breaker SLO、rate limit 是否计数、breaker key 共享
  范围、Provider kind、Actual Usage、Remote Session 串行吞吐、后续入口迁移顺序；
- 后续建议：评审上述生产参数和 Legacy 迁移顺序，但本次不实施第 15 天内容。
