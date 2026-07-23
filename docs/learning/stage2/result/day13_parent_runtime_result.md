# 阶段二第 13 天改造结果

## 1. 本次目标

本次为单次 Run 建立统一、per-run、单次使用的 Parent Runtime：
`RunCoordinator`。它在整个生命周期内持续持有 Context、Plan、State、
Budget、Handle、Scheduler、Executor、Registry 和并行策略；它不保存用户正文，
不修改不可变 Plan，也不实现 Retry、Fallback、Runtime Event、Tool Registry、
完整 RAG Contract、Snapshot 或 Replay。

核心目标是把“批次执行完成”和“整个 Run 可以终结”分开。单个
`ParallelExecutionReport` 只说明本批已 Claim Step 的结果；只有重新传播
BLOCKED、重算全 Plan 快照并确认所有 Step 成功且没有 active Step，Run 才成功。

## 2. 修改前生命周期所有权

修改前的生产路径所有权如下：

| 对象或状态 | 修改前 Owner / 写入者 |
| --- | --- |
| RunContext、CancellationSource、BudgetLedger | `ChatService.stream_chat` |
| RunStatus、StopReason | Legacy `AgentLoop` 通过 `AgentStateMachine` 写入 |
| StepStatus | `AgentLoop`、`SerialScheduler`、`ParallelExecutor` 通过 `AgentStateMachine` 写入 |
| Plan | `_select_model()` 临时创建单步骤 Plan，选择完成后丢弃；Scheduler 测试或独立调用方另行持有传入 Plan |
| Registry register/unregister | `ChatService.stream_chat` |
| Deadline watcher | `ChatService.stream_chat` |
| 模型流、Tool、RAG、Memory | `AgentRouter` 及其业务调用链 |

`AgentState` 仍保留兼容的直接 `mark_*` 方法，但当前生产 Runtime 通过
`AgentStateMachine` 施加 Guard 和终态保护。

## 3. Application Service 与 Runtime Owner

`ChatService` 是 Application Service：它负责选择 Legacy 或 Coordinated 入口、
装配一次 Run 的对象，并把业务参数交给 Adapter。它不应在 Coordinated 路径中
自行终结 Run 或注销 Registry。

`RunCoordinator` 是 Coordinated 路径的 Runtime Owner：它负责注册 Handle、
发送 Run STARTED、循环调度、决定终态、清理 Run 级资源并最后注销 Handle。

默认 `stream_chat` 仍是 Legacy 路径，生命周期 Owner 仍为
`ChatService + AgentLoop`。新旧入口必须二选一；同一个 run_id 不得同时进入两条
路径。

## 4. RunCoordinator

文件：`core/runtime/run_coordinator.py`

入口：

```python
await RunCoordinator.execute(
    driver=driver,
    execution_mode=StepExecutionMode.ASYNC,
)
```

Coordinator 持有：

- `RunContext`
- 同一个不可变 `Plan`
- `AgentState`
- Context 已绑定的同一个 `BudgetLedger`
- 使用同一 CancellationSource 和 AgentState 的 `RunHandle`
- `SerialScheduler`
- `ParallelExecutor`
- `RunRegistry`
- `ParallelExecutionPolicy`
- `AgentStateMachine`
- 自己创建的 deadline watcher、Executor Task 和 Run cleanup callbacks

构造后会校验 Context、State、Handle 的 run_id、Handle 的 State 身份、
Context/Handle 的 CancellationSource 身份和 Context/Coordinator 的 Ledger
身份。对象以同步锁原子标记为已启动，第二次 `execute()` 抛出安全的
`RunCoordinatorError`。

## 5. Plan 长期 Owner

Coordinated 路径先由 `AgentRouter.build_single_agent_plan()` 创建一次确定性
单步骤 Plan，再把同一对象传给 Coordinator。Coordinator 在整个 Run 中持续持有
它；prepare、evaluate、每次 claim 和最终 Result 都使用同一 plan_id、version
和对象身份。

真实 Driver 从 `StepClaim.capability_requirements` 获取能力需求，并传给模型选择，
因此 Coordinated 路径不会在 `_select_model()` 中再生成第二个 Plan。

Legacy 流式路径尚未迁移：当调用方没有传入 Coordinator 已持有的能力需求时，
`_select_model()` 仍会通过 `build_single_agent_plan()` 创建临时 Plan，并在模型
选择完成后丢弃。本次未持久化 Plan，也没有向 `PlanStep` 写入运行状态。

## 6. RunFinalizationDecision

`RunFinalizationDecision` 是 `frozen=True` 的不可变值对象，字段为：

- `status`
- `stop_reason`
- `error_code`
- `safe_message`

它只表达决策，不直接修改 State。构造时校验成功、失败、取消状态与 StopReason
组合，拒绝非终态状态和非法取消原因。所有 safe_message 都是固定安全中文说明，
不包含 Prompt、模型输出、Tool 参数、RAG、Memory、Secret 或原始异常。

## 7. RunCoordinatorResult

`RunCoordinatorResult` 是不可变安全结果，包含：

- `run_id`
- `plan_id`
- `status`
- `stop_reason`
- `succeeded_step_ids`
- `failed_step_ids`
- `cancelled_step_ids`
- `blocked_step_ids`
- `budget_snapshot`
- `cleanup_error_codes`
- 可选安全 `error_code` 和 `safe_message`

它不保存 Step result、用户正文、模型输出或原始异常。业务回答由 Driver 暂时持有，
不进入 Coordinator Result。

## 8. Scheduler / Executor 循环

标准执行顺序：

1. 原子标记 Coordinator 已启动并校验所有权；
2. Registry 注册 Handle；
3. State Machine 发送 Run STARTED；
4. 启动 per-run deadline watcher；
5. 校验 PlanGraph；
6. Scheduler prepare，注册 Plan Step；
7. 检查 Token 和 Deadline；
8. Scheduler evaluate，传播 BLOCKED 并生成全 Plan 快照；
9. 若有 claimable Step，Executor 创建受管 Task 执行一批；
10. 忽略“本批全成功即 Run 成功”的错误假设，回到第 7 步；
11. 全 Plan 收敛后决定终态；
12. 停止 Executor、收口 active Step、exactly-once finalize；
13. 停 watcher，逆序运行 cleanup callbacks；
14. 检查 Budget Reservation；
15. Registry unregister；
16. 返回安全 Result。

循环支持串行多批、fork、join 和 best-effort 独立分支。没有 Ready、没有 Running
且 Plan 未完成时立即 NO_ACTION，不做轮询或忙循环。

## 9. ParallelExecutionReport → RunStatus

| 批次或全局事实 | Run 处理 |
| --- | --- |
| Batch 全成功 | 不直接终结；重新 evaluate 全 Plan |
| 全 Plan Step SUCCEEDED 且 active 为空 | `SUCCEEDED / COMPLETED` |
| 普通 Step FAILED，调度收敛 | `FAILED / UNHANDLED_ERROR` |
| Fail-fast 为 FAILED + sibling CANCELLED，Token 未取消 | Run 为 FAILED，不误判 CANCELLED |
| Best-effort 出现失败 | 独立 Ready Step 继续；收敛后 Run FAILED |
| Token 为用户/断开/关闭取消 | Run CANCELLED |
| Token 或 Driver 表达 Deadline | Run FAILED / DEADLINE_EXCEEDED |
| Driver 或 Scheduler 抛 BudgetExceededError | Run FAILED / BUDGET_EXHAUSTED |
| Executor/Coordinator 基础设施错误 | Run FAILED / UNHANDLED_ERROR |
| 无 Ready、无 Running、Plan 未完成 | Run FAILED / NO_ACTION |

Executor 对 Driver 内的 `BudgetExceededError` 和 `RunDeadlineExceededError` 不再
泛化为普通 Step 失败。它会取消本批未完成 Step、清空 active 后，将运行级信号
交还 Coordinator。

## 10. Cancellation / Budget / Error 优先级

实现的终态优先级：

1. 已存在终态由 `_finalize_once()` 原样保留；
2. Token first-wins 取消原因；
3. `BudgetExceededError`；
4. Coordinator、Scheduler、Executor 基础设施错误；
5. 全 Plan 成功；
6. Step 失败且调度收敛；
7. NO_ACTION。

取消映射：

| CancellationReason | RunStatus | StopReason |
| --- | --- | --- |
| `USER_CANCELLED` | CANCELLED | USER_CANCELLED |
| `CLIENT_DISCONNECTED` | CANCELLED | CLIENT_DISCONNECTED |
| `SYSTEM_SHUTDOWN` | CANCELLED | SYSTEM_SHUTDOWN |
| `DEADLINE_EXCEEDED` | FAILED | DEADLINE_EXCEEDED |

Deadline 保持现有 AgentState Schema 和 State Machine 语义：它是专用失败事件，
不是三类可取消终态之一。本次没有修改 AgentState Schema。

Budget 继续使用现有 `RunEventType.BUDGET_EXHAUSTED` 和
`StopReason.BUDGET_EXHAUSTED`，没有建立第二套规则。Windows 下 Ledger 的时长
时钟改用同样单调但分辨率更高的 `perf_counter()`，保证毫秒级预算测试稳定。

## 11. Exactly-once Finalize

`_finalize_once()` 使用同步 `threading.Lock` 实现 first-wins：

- 第一个终态决策应用到 State Machine；
- 后续成功、取消、晚到异常或清理请求返回第一个决策；
- 已处于终态的 State 被读取并保留，不覆盖；
- 锁内只有同步状态检查和事件应用，没有 await；
- State Machine 自身仍拒绝终态 Run 的后续事件；
- 测试覆盖双请求竞争和“成功后晚到错误”；
- cleanup error 只进入 `cleanup_error_codes`，不修改主 StopReason。

## 12. active Step 收口

Run 终态写入前，Coordinator 先停止仍存活的 Executor Task，再按稳定 step_id 顺序
对所有 RUNNING Step 发送 State Machine CANCELLED 事件。

- 已 Claim 且仍 RUNNING 的 Step 变为 CANCELLED；
- 未 Claim 的 PENDING Step 保持 PENDING；
- Executor 已完成的失败 Step 保持 FAILED；
- Scheduler 根据失败/取消依赖传播 BLOCKED；
- 不把未执行 Step 伪装成 FAILED；
- 成功路径要求所有 Plan Step SUCCEEDED；
- State Machine 的 Run 终态 Guard 再次验证 active 集合为空。

## 13. Registry 生命周期

Coordinated 路径：

```text
RunCoordinator
  → register
  → STARTED / execute / finalize
  → stop watcher
  → cleanup callbacks
  → budget snapshot
  → unregister
```

cleanup callback 执行时 Handle 仍在 Registry 中；注销是 Coordinator cleanup 的
最后一步。注销失败只记录安全错误码，不覆盖主终态。

Legacy 路径仍由 `ChatService.stream_chat` register/unregister。本次没有让
ChatService 在 Coordinated 路径中重复注销。

## 14. Budget 与资源清理

清理顺序符合本次边界：

1. 不再开始新调度；
2. 取消并等待 Coordinator 创建的 Executor Task；
3. 通过 State Machine 终结 active Step；
4. exactly-once 写 Run 终态；
5. 停止 deadline watcher；
6. cleanup callbacks 逆序执行，一个失败不阻止后续回调；
7. 获取 BudgetSnapshot；
8. Registry unregister。

Coordinator 只检查 `active_reservation_count`。发现 Reservation 泄漏时记录
`BUDGET_RESERVATION_LEAK`，不会统一 release Driver 所有的模型、Tool、RAG 或
单调用 Reservation。模型流、Tool、RAG 和单调用 Reservation 仍由 Driver/
AgentRouter 负责。

## 15. 最小真实入口迁移

已迁移生产调用结构：

```text
ChatService.run_coordinated_agent
  → AgentRouter.build_single_agent_plan
  → RunCoordinator.execute
  → SerialScheduler.claim_ready
  → ParallelExecutor.execute_ready
  → _CoordinatedSingleAgentDriver.execute
  → AgentRouter.complete_single_agent
  → AgentRouter._run_agent_once
  → AgentRouter._complete_final_response
  → 真实 Model Adapter
```

这是非流式单 Agent 内部入口。Driver 不发送 STARTED、不写 RunStatus、不注册
Step、不注销 Registry；它只暂时持有业务查询和最终回答。测试经过真实
ChatService、AgentRouter、模型选择、消息构建和 Model Adapter 结构，模型端使用
Fake Model，未访问远程网络。

## 16. Legacy 与新 Runtime 边界

```text
Legacy path
  ChatService.stream_chat
  → ChatService + AgentLoop 是生命周期 Owner

Coordinated path
  ChatService.run_coordinated_agent
  → RunCoordinator 是唯一生命周期 Owner
```

未迁移路径包括：

- 默认流式单 Agent 聊天；
- `core_router` 多 Agent 编排流；
- 编排内部的 Legacy `_run_agent_once()` 调用；
- Tool planner、同步完整回答的其他调用点；
- 尚未统一的 RAG/Tool 生命周期。

本次没有修改 API、自定义流协议、UI、`[[ORCH]]` 或并行写聊天流。

## 17. 新增文件

- `core/runtime/run_coordinator.py`
- `tests/test_run_coordinator.py`
- `docs/learning/stage2/result/day13_parent_runtime_result.md`

## 18. 修改文件

- `core/runtime/__init__.py`：导出 Coordinator、结果类型及 Budget 公共类型；
- `core/chat_service.py`：新增真实 Coordinated 单 Agent 入口和纯业务 Driver；
- `core/agent_router.py`：新增长期 Plan 创建入口、业务 Adapter，并让新路径复用
  Claim 中的能力需求；
- `core/runtime/parallel_execution.py`：保留 Driver 的 Budget/Deadline 运行级信号；
- `core/runtime/budget.py`：使用高分辨率单调时钟，稳定毫秒级时间预算。

## 19. 重点 Bad Case

### Bad Case 1：Plan 仅在模型选择期间存在

- 类型：真实代码边界。
- 触发条件：Legacy `_select_model()` 为能力选择临时创建单步骤 Plan。
- 故障表现：模型选完后 Plan 丢失，Scheduler、Result 和生命周期没有共同 Owner。
- 根因分析：Planner 产物只被当作模型选择中间变量。
- 修复方案：新路径先创建 Plan，由 Coordinator 长期持有；模型选择复用 Claim 的能力需求。
- 回归测试：`test_chat_service_real_single_agent_path_uses_one_long_lived_plan`、`test_same_plan_object_reaches_every_scheduler_call`。
- 对应知识点：聚合根；生命周期所有权；不可变值对象。
- 面试表达：Plan 是 Run 级事实，不是模型选择期临时变量。
- 当前状态：Coordinated 路径已修复；Legacy 流式路径仍保留临时 Plan，已明确记录。

### Bad Case 2：一批 Step 成功就提前终结 Run

- 类型：假设构造。
- 触发条件：依赖链第一批成功后直接把 Batch Report 映射为 Run SUCCEEDED。
- 故障表现：后续 join 或依赖 Step 尚未执行，Run 已终结。
- 根因分析：混淆批次边界与全 Plan 边界。
- 修复方案：每批后重新 evaluate 全 Plan，只有全 Step 成功且 active 为空才完成。
- 回归测试：`test_dependency_chain_uses_multiple_batches`、`test_first_successful_batch_does_not_finalize_run`。
- 对应知识点：调度收敛；局部结果与全局结果。
- 面试表达：Batch Report 是观测，不是 Run 终态命令。
- 当前状态：已覆盖。

### Bad Case 3：Fail-fast 兄弟取消导致 Run 被误判 CANCELLED

- 类型：假设构造。
- 触发条件：同批结果为 FAILED + CANCELLED，Token 没有 Run 级取消原因。
- 故障表现：Run 被错误归因为用户或系统取消。
- 根因分析：只看见 CANCELLED Step，没有区分 TaskGroup fail-fast 和 Run Token。
- 修复方案：取消终态只依据 Token；存在 Step FAILED 时收敛为 Run FAILED。
- 回归测试：`test_fail_fast_sibling_cancel_does_not_cancel_run`。
- 对应知识点：取消域；失败传播；first-wins。
- 面试表达：兄弟 Task 取消是执行策略，不等于 Run 取消。
- 当前状态：已覆盖。

### Bad Case 4：ChatService 与 Coordinator 重复终结 Run

- 类型：假设构造。
- 触发条件：新入口执行完后 ChatService 再发 Run 终态或 unregister。
- 故障表现：State Machine 拒绝终态覆盖，Registry 生命周期提前或重复结束。
- 根因分析：Application Service 与 Runtime Owner 职责重叠。
- 修复方案：新入口只装配和取回结果；Coordinator 独占终态和 Registry。
- 回归测试：`test_new_driver_has_no_lifecycle_or_registry_writes`、`test_registry_stays_registered_through_callbacks_then_unregisters`。
- 对应知识点：单一所有者；Application Service 边界。
- 面试表达：Service 选择 Runtime，Runtime 结束 Run。
- 当前状态：Coordinated 路径已覆盖；Legacy 保持原 Owner。

### Bad Case 5：清理异常覆盖主要 StopReason

- 类型：假设构造。
- 触发条件：Run 已成功或失败后，一个 cleanup callback 抛出异常。
- 故障表现：主终态被改成 cleanup failure，真实停止原因丢失。
- 根因分析：把资源清理结果与业务/Runtime 主决策混为一个异常通道。
- 修复方案：先 exactly-once finalize；清理异常只追加固定安全错误码，继续后续清理。
- 回归测试：`test_cleanup_callbacks_are_lifo_and_continue_after_failure`。
- 对应知识点：主错误与 suppressed cleanup error；finally。
- 面试表达：清理失败可观测，但不能重写已经发生的主事实。
- 当前状态：已覆盖。

### Bad Case 6：Run 提前从 Registry 注销

- 类型：假设构造。
- 触发条件：Executor 或 callbacks 尚未完成，Handle 已注销。
- 故障表现：取消 API 返回 inactive，资源仍在运行。
- 根因分析：注销不在最外层 Owner 的最后边界。
- 修复方案：Coordinator 在 watcher、callbacks 和预算检查后最后 unregister。
- 回归测试：`test_registry_stays_registered_through_callbacks_then_unregisters`。
- 对应知识点：资源生命周期；结构化并发。
- 面试表达：Registry 活跃性必须覆盖所有 Run 所属资源。
- 当前状态：已覆盖。

### Bad Case 7：Coordinator 统一 release 所有 Reservation

- 类型：假设构造。
- 触发条件：清理时发现 active Reservation，Coordinator 遍历并强制 release。
- 故障表现：已经开始的模型或 Tool 调用被错误按“未发生”返还预算。
- 根因分析：越过 Driver 的调用边界，无法判断 commit/release 语义。
- 修复方案：Coordinator 只获取 Snapshot 并记录泄漏码，不提供批量 release。
- 回归测试：`test_reservation_leak_is_reported_but_not_released`。
- 对应知识点：Reservation 所有权；保守记账。
- 面试表达：检测属于 Parent，结算属于实际发起调用的 Owner。
- 当前状态：已覆盖。

### Bad Case 8：同一 Coordinator 被执行两次

- 类型：假设构造。
- 触发条件：调用方复用已完成或正在执行的 per-run Coordinator。
- 故障表现：重复注册、重复 STARTED、重复调度或覆盖终态。
- 根因分析：缺少单次消费 Guard。
- 修复方案：同步锁内 first-write 标记 `_started`，第二次固定报 `COORDINATOR_ALREADY_EXECUTED`。
- 回归测试：`test_execute_twice_is_rejected`。
- 对应知识点：一次性对象；原子状态转换。
- 面试表达：per-run 不仅是作用域约定，还由原子 Guard 强制。
- 当前状态：已覆盖。

## 20. 测试命令和结果

当前环境没有可直接调用的 `python` 命令，因此使用项目锁文件对应的
`uv run python` 执行同等命令。

```text
uv run python -m pytest \
  tests/test_run_coordinator.py \
  tests/test_timeout_cancellation.py \
  tests/test_budget.py \
  tests/test_parallel_execution.py \
  tests/test_scheduler.py -q

结果：67 passed，13 subtests passed
```

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
  tests.test_run_coordinator -q

结果：136 tests，OK
```

```text
uv run python -m pytest -q
结果：225 passed，30 subtests passed
```

`uv run python -m compileall -q core tests` 通过；`git diff --check` 通过，
仅显示仓库既有的 Git 行尾转换提示，没有空白错误。

## 21. 未完成事项和已知风险

- 只迁移了一条真实的非流式单 Agent 入口；
- 默认聊天流、编排流和部分内部调用仍走 Legacy AgentLoop；
- Plan 未持久化；
- Coordinator 仅支持单进程内存对象，不是分布式 Coordinator；
- 没有 Retry、Model Fallback 或 Circuit Breaker；
- 未统一 Tool / RAG Runtime Contract；
- Runtime Event Queue 尚未实现；
- Snapshot / Replay 尚未实现；
- Budget Reservation 泄漏只能检测，Coordinator 不越权 release；
- cleanup 无法强杀正在底层线程、llama.cpp 或 C 扩展中不可抢占的工作；
- `asyncio.to_thread` 仍依赖 Driver 在返回后的合作式安全点停止；
- Registry 仍为单进程内存结构，多 Worker 不共享；
- 默认自定义 HTTP 流仍不是标准 SSE；
- 新非流式入口尚未接到现有 HTTP/UI 协议，本次按禁区不修改协议和 UI。

## 22. 面试表达

“我把 Parent Runtime 设计成 per-run、单次使用的聚合根。它长期持有同一个
Plan、State、Ledger、Handle、Scheduler 和 Executor。Executor 只终结已 Claim
Step，Batch Report 不能终结 Run；Coordinator 每批后重新传播依赖阻塞并重算
全 Plan，只有所有 Step 成功且 active 为空才完成。取消只看 Run Token，
fail-fast 的兄弟 Task 取消不会被误判为 Run 取消。最终通过锁内 first-wins
finalize、State Machine 终态 Guard 和最后注销 Registry，保证终态与清理的
exactly-once 语义。”

## 23. 需要带回 ChatGPT 审查的信息

- RunCoordinator 文件：`core/runtime/run_coordinator.py`；
- 主入口：`RunCoordinator.execute()`；
- Coordinator 持有 Context、Plan、State、Ledger、Handle、Scheduler、Executor、
  Registry、Policy、State Machine 和自己创建的 Run 级资源；
- Coordinator 是 per-run、单次使用对象，不是全局单例；
- Coordinated 路径 Plan Owner 是 RunCoordinator；
- Coordinated 路径 RunStatus/StopReason 只由 RunCoordinator 通过 State Machine
  修改；
- Coordinated 路径 StepStatus 由 SerialScheduler、ParallelExecutor 和
  Coordinator active cleanup 通过 State Machine 修改；
- Coordinated Registry Owner 是 RunCoordinator；Legacy Owner 仍是 ChatService；
- Scheduler/Executor 循环支持多批、fork、join 和 best-effort；
- Batch Report 不等于 Run Status；
- Fail-fast sibling cancel 在无 Token 原因时映射 Run FAILED；
- USER、CLIENT、SYSTEM 映射 CANCELLED；DEADLINE 按现有 Schema 映射 FAILED；
- BudgetExceeded 映射专用 BUDGET_EXHAUSTED；
- 基础设施错误映射 FAILED / UNHANDLED_ERROR；
- 无 Ready、无 Running、Plan 未完成映射 NO_ACTION；
- `_finalize_once()` 为同步锁内 first-wins，无锁内 await；
- 终态前 active Step 通过 State Machine 清空；
- cleanup error 只进入安全错误码，不覆盖 StopReason；
- Budget Reservation 只检测，不统一 release；
- 真实迁移入口：`ChatService.run_coordinated_agent()`；
- 真实业务 Adapter：`AgentRouter.complete_single_agent()`；
- 已迁移：一条非流式单 Agent 路径；
- 未迁移：默认流式、编排流、部分 Model/Tool/RAG 调用；
- 全仓当前测试结果：225 passed，30 subtests passed；
- 八项 Bad Case 已按固定格式记录；
- 需要人工确认：何时把现有 HTTP/UI 接到 Coordinated 非流式或未来流式 Adapter；
- 需要人工确认：Deadline 是否在未来 Schema 中改为 CANCELLED；本次保持现有
  `RunEventType.DEADLINE_EXCEEDED → RunStatus.FAILED` 契约；
- 需要人工确认：生产多 Worker 是否需要共享 Registry；本次不实施分布式能力；
- 后续建议：先评审 Owner 与终态契约，再规划剩余入口迁移；不得在本次提前实施
  第 14 天的 Retry、Fallback、Event、Snapshot 等内容。
