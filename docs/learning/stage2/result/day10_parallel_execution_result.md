# 阶段二第 10 天改造结果

## 1. 本次任务调整与目标
在并行主线前补齐 `StepClaim → ParallelExecutor → StepExecutionOutcome`：Scheduler 独占 `PENDING → RUNNING`，避免 Legacy AgentLoop 重复发送 STARTED。

## 2. 修改前并发和生命周期现状
State Machine 的 `active_step_ids` 是集合，允许多个 RUNNING Step，未发现单 active Step Guard；但旧 Scheduler 因 Ready 计算含 RUNNING 检查而串行。Legacy AgentLoop 自行注册/STARTED，故未接入本执行器。CancellationToken 只有合作式同步检查，无异步 wait。LocalLLMEngine 已以线程锁串行同一实例；未发现 Runtime Semaphore，模型客户端并发能力未作推断。

## 3. Scheduler、Executor、Driver 所有权
Scheduler 只发 STARTED；ParallelExecutor 只经 State Machine 发 SUCCEEDED/FAILED/CANCELLED；Driver 只做业务执行，不能改 State、RunStatus、Claim 或 fallback；父 Runtime 根据 Report 决定 Run 终态。两者均不改 RunStatus。

## 4. 最终架构
`SerialScheduler.claim_ready()` 在 Scheduler Lock 内校验 DAG、对齐状态、传播 BLOCKED、预检候选并稳定批量 STARTED；`ParallelExecutor.execute()` 以 TaskGroup 执行已 Claim 项，返回结构化 Report。推荐 `ParallelExecutor.execute_ready(..., policy=ParallelExecutionPolicy(...))`，同一 Policy 同时传给 Claim 容量和 Executor 容量。

## 5. 新增文件
- `core/runtime/parallel_execution.py`
- `tests/test_parallel_execution.py`
- 本文档

## 6. 修改文件
- `core/runtime/scheduler.py`
- `core/runtime/__init__.py`
- `tests/test_scheduler.py`

## 7. Scheduler 多 Ready / 多 Claim
`claim_ready(plan, state, max_parallelism, occurred_at)` 以 `running_count` 计算槽位，按 `PlanGraph.topological_order` Claim 稳定前缀。`claim_next()` 委托 `max_parallelism=1` 保持兼容。

## 8. Ready 与 Claimable 语义
Ready 为 RUNNING Run 中 PENDING、非 active、依赖均 SUCCEEDED 的所有步骤；不因其他 RUNNING 消失。Claimable 是 Ready 的容量前缀。Snapshot 提供 `claimable_step_ids`、`max_parallelism`、`available_slots`。RUNNING 表示“已被认领并进入执行生命周期”，可能正在执行 Driver，也可能等待全局或资源许可；不表示必然已占用模型或 Tool。本次不新增 CLAIMED/WAITING_RESOURCE 状态。

## 9. ParallelExecutor
入口为 `ParallelExecutor.execute(...)`；Outcome 为 frozen、UTC、仅三种终态且只保存安全中文错误摘要；result 仅在调用期内。

## 10. TaskGroup 和结构化并发
必须由 `asyncio.TaskGroup` 持有并等待全部子任务，不创建游离后台 Task。业务失败不向调用者泄漏 ExceptionGroup。

## 11. 全局与资源并发限制
Executor 使用全局 `asyncio.Semaphore(max_concurrency)`，并维护本实例 resource key→Semaphore。`ParallelExecutionPolicy(max_concurrency, failure_mode)` 是标准安全调用的同源容量；低层 `claim_ready` 与 `execute` 仍允许独立参数，属于高级接口，调用方负责一致性。相同 key 必须使用相同 limit，冲突在 Driver 前取消所有输入 RUNNING Claim 并抛出 `RESOURCE_LIMIT_CONFLICT`；不同 key 可并行，默认 spec 为 `default/1`。

## 12. Fail-fast
首个业务失败先写 FAILED，再用内部信号令 TaskGroup 取消兄弟；兄弟写 CANCELLED，已成功保持成功，未 Claim 仍 PENDING。

## 13. Best-effort
业务异常在子任务内转 FAILED Outcome，不逃逸 TaskGroup，不取消兄弟；所有 Claim 均进入终态。

## 14. 状态终结与异常聚合
终态一律通过 AgentStateMachine，active 集合随终态移除。Report 按输入 Claim 顺序而非完成顺序聚合失败、成功和取消 ID。

## 15. 取消传播
批次和每个 Step 开始前均调用 RunContext 检查，Driver 接收同一 context。父协程取消会取消 TaskGroup、子项写 CANCELLED，并继续抛出 CancelledError；Token 仍是合作式基础，不 busy poll。

## 16. 同步阻塞任务隔离
`SYNC_BLOCKING` 直接以 `asyncio.to_thread(driver.execute, ...)` 运行，避免先在事件循环调用同步函数。取消等待不强制停止底层线程；副作用 Tool 必须合作式取消或幂等。本次未实现线程强杀或 ProcessPool。

## 17. 模型和 Tool 并发边界
不修改模型选择、不做 fallback。资源 guard 仅单进程 Executor 实例，不是 Provider 或分布式限流。

## 18. 与 Legacy AgentLoop 和流式输出的边界
当前完成并行 Runtime 组件闭环，尚未将默认聊天请求切换为多步骤并行执行。不并行写 ChatService、final_output、`[[ORCH]]` 或前端流。

## 19. 重点 Bad Case
### Bad Case 1：Claim 数量超过执行容量
- 类型：假设构造
- 触发条件：已有 RUNNING 后继续 Claim。
- 故障表现：超额运行。
- 根因分析：未从 State 计算槽位。
- 修复方案：`max_parallelism - running_count`。
- 回归测试：`test_claim_ready_claims_stable_prefix_and_respects_existing_running`。
- 对应知识点：背压。
- 面试表达：Scheduler 容量是第一道防线，Semaphore 是防御层。
- 当前状态：已覆盖。
### Bad Case 2：Best-effort 意外退化为 Fail-fast
- 类型：假设构造
- 触发条件：业务异常逃出子任务。
- 故障表现：兄弟被取消。
- 根因分析：TaskGroup 默认传播异常。
- 修复方案：Best-effort 内部转 FAILED Outcome。
- 回归测试：`test_best_effort_stable_order_and_state_terminal`。
- 对应知识点：结构化并发。
- 面试表达：业务错误与基础设施错误分层。
- 当前状态：已覆盖。
### Bad Case 3：Fail-fast 后兄弟永久停在 RUNNING
- 类型：假设构造
- 触发条件：CancelledError 未终结状态。
- 故障表现：active 泄漏。
- 根因分析：取消不是自动状态事件。
- 修复方案：取消处理发送 CANCELLED。
- 回归测试：专项测试与状态机断言。
- 对应知识点：取消清理。
- 面试表达：每个 Claim 必须恰好一个终态。
- 当前状态：已实现。
### Bad Case 4：取消 to_thread 后底层副作用继续
- 类型：真实机制下的假设场景
- 触发条件：取消等待中的线程任务。
- 故障表现：线程仍可能完成副作用。
- 根因分析：Task.cancel 不会杀线程。
- 修复方案：合作式取消、幂等性。
- 回归测试：同步线程隔离测试。
- 对应知识点：线程取消限制。
- 面试表达：to_thread 保证事件循环响应，不保证副作用回滚。
- 当前状态：已明确限制。
### Bad Case 5：同一模型实例并发调用
- 类型：假设构造
- 触发条件：共享非线程安全实例。
- 故障表现：竞态。
- 根因分析：以名称猜测线程安全。
- 修复方案：显式 resource key/limit=1。
- 回归测试：`test_resource_and_global_limits`。
- 对应知识点：资源隔离。
- 面试表达：资源身份来自配置，不来自 provider 名称。
- 当前状态：已提供机制。
### Bad Case 6：按完成顺序聚合导致结果不稳定
- 类型：假设构造
- 触发条件：不同完成时序。
- 故障表现：Report 顺序漂移。
- 根因分析：使用完成队列或 Set。
- 修复方案：按 claims 重建 outcomes。
- 回归测试：`test_best_effort_stable_order_and_state_terminal`。
- 对应知识点：确定性。
- 面试表达：调度顺序和汇总顺序都必须显式定义。
- 当前状态：已覆盖。

### Bad Case 7：取消发生在 Semaphore 等待阶段
- 类型：假设构造
- 触发条件：Step 在全局或资源 Semaphore 上等待时被 fail-fast 或父任务取消。
- 故障表现：若取消捕获只包住 Driver，Step 会永久 RUNNING。
- 根因分析：许可获取也是可取消 await 点。
- 修复方案：Worker 的 try/except 覆盖全局许可、资源许可、Driver、to_thread 等待和终态提交前阶段。
- 回归测试：`test_fail_fast_cancels_global_semaphore_waiter`、`test_fail_fast_cancels_resource_semaphore_waiter`、`test_parent_cancel_cleans_semaphore_waiter_and_reraises`。
- 对应知识点：结构化取消。
- 面试表达：任何 await 点都必须纳入生命周期清理范围。
- 当前状态：已覆盖。
### Bad Case 8：相同 resource key 使用不同 resource limit
- 类型：假设构造
- 触发条件：同一批次为同一 key 给出 limit=1 和 limit=2。
- 故障表现：first-wins/last-wins 会使实际保护依赖遍历顺序。
- 根因分析：资源身份与限制没有一致性校验。
- 修复方案：执行前完整预检；冲突取消所有已 Claim 输入并抛出 `RESOURCE_LIMIT_CONFLICT`。
- 回归测试：`test_resource_conflict_and_driver_modes_cleanup_claims`。
- 对应知识点：资源不变量。
- 面试表达：资源 key 的限制是配置契约，不能由运行顺序决定。
- 当前状态：已覆盖。

## 20. 测试命令和结果
专项 pytest、unittest、compileall 和 diff 检查见提交前命令记录。

## 21. 未完成事项和已知风险
默认 Legacy AgentLoop 未接入；Plan 未长期持有；未实现多 Step 流聚合、模型 fallback、分布式并发、Tool 幂等/冲突检测或 Outcome 持久化；to_thread 不能强杀线程；resource guard 仅单进程；取消依赖合作式检查。

## 22. 面试表达
用 Scheduler 锁保证 Claim 的状态所有权，用 TaskGroup 管理执行生命周期，用全局与资源双层 Semaphore 控制并发，并把业务失败安全地聚合为稳定 Report。

## 23. 需要带回 ChatGPT 审查的信息
本日按“先生命周期边界再并行”调整；入口 `ParallelExecutor.execute`，多 Claim 入口 `claim_ready`；State Machine 支持多 RUNNING。批量 Claim 在单锁内完成预检和 STARTED，部分失败抛出含已成功 IDs 的 Typed Error。TaskGroup 提供 fail-fast/best-effort，资源 key、to_thread、父取消、Token、稳定 Outcome/Report 均已实现。未改 RunStatus、Legacy AgentLoop、流输出或 fallback。人工应确认未来长期 Plan owner、Driver 的资源配置来源、同步 Tool 幂等契约与父 Runtime 的 Report→RunStatus 策略；后续可在第 11 天设计接入，本文不实施。
