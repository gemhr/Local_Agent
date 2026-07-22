# 阶段二第 8 天改造结果

## 1. 本次任务目标

实现一个最小串行 `SerialScheduler`：根据不可变 `Plan` 与可变 `AgentState` 注册步骤、动态计算 Ready Step、传递传播 `BLOCKED`，并在单进程单实例锁内通过 `AgentStateMachine` 原子 Claim 一个 Step。本次不执行模型、Tool、RAG 或 Agent，不修改 Run 终态、API、Memory、流式协议和模型选择规则。

## 2. 修改前调度现状

修改前没有执行队列、Step queue、Ready 派生计算或 Scheduler。真实 Step 生命周期位置如下：

- Plan 创建：`core/agent_router.py` 的 `_select_model()` 调用 `create_single_step_plan()`，只验证/生成单步骤 Plan，返回值没有被长期保存或执行。
- Step 创建：`core/runtime/agent_loop.py` 的默认 `AgentLoop.run_stream()` 调用 `AgentStateMachine.add_step()`。
- Step 启动：同一 Loop 紧接着发送 `StepEventType.STARTED`。
- Step 成功：Executor 返回正常 `AgentObservation` 后，Loop 发送 `StepEventType.SUCCEEDED`。
- Step 失败：执行失败、Deadline 或未处理异常时，Loop 发送 `StepEventType.FAILED`。
- Step 取消：Run 取消时，Loop 发送 `StepEventType.CANCELLED`。
- `BLOCKED`、`SKIPPED`：状态机支持，默认 AgentLoop 业务路径没有发送这两类事件。
- `active_step_ids`：由 State Machine 的 Step 事件同步维护；`STARTED` 加入，终止事件移除。

当前 Plan 没有长期 Runtime 所有者：`AgentRouter` 在模型选择调用栈内临时创建后丢弃。当前 AgentState 由 `ChatService.stream_chat()` 创建并在单次生成器调用栈内持有，`AgentLoop` 负责变更，`ChatService` 只通过可选 observer 暴露临时快照而不保存状态。

## 3. Planner、Scheduler、AgentLoop 边界

- Planner：只定义 `PlanStep`、`depends_on`、完成条件、能力需求和首选 Agent，不保存执行状态。
- Scheduler：只注册 Plan Step、传播 `BLOCKED`、计算 Ready、发送一次 `STARTED` 并返回 `StepClaim`/`SchedulerSnapshot`。
- AgentState：只保存真实 Run/Step 执行状态，不保存 Plan，也没有新增字段。
- Executor/AgentLoop：实际执行模型、Tool、RAG 或 Agent，并发送 `SUCCEEDED`、`FAILED`、`CANCELLED`，最后决定 Run 是否终止。

Scheduler 不调用模型选择、模型客户端、Tool 或 RAG，不写流式 Chunk、`final_output` 或 `RunStatus`。

## 4. 最终设计方案

新增 `core/runtime/scheduler.py`，集中放置异常、Claim、Snapshot 和 `SerialScheduler`，避免拆成大量小文件。每个 `SerialScheduler` 实例持有一个非全局 `threading.Lock` 和一个内存绑定签名；绑定包含 `run_id`、`plan_id`、Plan version 以及调度相关 Step 语义，用于拒绝同一实例切换 Run、Plan 版本或步骤绑定。

三个公开入口为：

- `prepare(plan, state, occurred_at)`：预检全部冲突，通过 `AgentStateMachine.add_step()` 幂等注册缺失步骤，传播可确定的 `BLOCKED` 并返回 Snapshot。
- `evaluate(plan, state)`：校验已经准备好的 Plan/State，传播 `BLOCKED` 后动态返回 Snapshot。
- `claim_next(plan, state, occurred_at)`：在同一 Lock 内完成 prepare、阻断传播、RUNNING 检查、Ready 计算、稳定选择和 `STARTED`。

该入口是独立、可选的串行 Plan 调度入口，不被默认 Legacy AgentLoop 消费。

## 5. 新增文件

- `core/runtime/scheduler.py`
- `tests/test_scheduler.py`
- `docs/learning/stage2/result/day08_scheduler_result.md`

## 6. 修改文件

- `core/runtime/__init__.py`：导出 Scheduler 类型和异常。

没有修改 `AgentState` Schema、AgentLoop、AgentRouter、ChatService、API、Memory、流式协议或 Model Selection。

## 7. 核心类型

### SerialScheduler

单进程、单实例、单 Run 使用的最小串行调度器。它不保存完整 Plan，只保存安全的调度绑定签名和非序列化 Lock。

### StepClaim

`frozen=True, slots=True` 的不可变 dataclass，包含 `plan_id`、`plan_version`、`step_id`、UTC `claimed_at`、`capability_requirements` 和 `preferred_agent`。它不包含 Prompt、用户原文、Model Client 或 selected profile。

### SchedulerSnapshot

`frozen=True, slots=True` 的不可变派生视图，按 Plan tuple 顺序包含 `ready_step_ids`、`running_step_ids`、`pending_step_ids`、`blocked_step_ids`、`terminal_step_ids` 以及 `is_complete`、`is_waiting`、`has_unresolved_pending`。所有 ID 集合均返回 tuple；Snapshot 不写回 AgentState，也不保存完整 Step 内容。

### Scheduler Error

安全异常层级为 `SchedulerError`、`SchedulerPlanStateMismatchError` 和 `SchedulerClaimError`。异常仅携带固定 `error_code`、`plan_id`、Plan version、可选 `step_id`、当前安全状态和中文安全说明，不包含用户正文、Prompt、RAG、Tool 参数、Secret 或路径。

## 8. Plan 和 AgentState 绑定

`prepare()` 先调用 `PlanValidator.validate()` 和 `AgentState.validate()`。首次成功准备后，Scheduler 实例绑定当前 Run、Plan ID/version 和调度相关步骤签名；复用同一实例调度其他 Run 或 Plan 会安全失败。

Plan Step 只能通过 `AgentStateMachine.add_step()` 注册。新 Step 名称只取简短 `PlanStep.title`，初始为 `PENDING`；description、completion criteria、task summary、用户输入和 Prompt 均不会进入 AgentState。相同 ID/相同名称会跳过；相同 ID/不同名称会在任何新增发生前失败，避免部分注册。

由于 AgentState Schema 按要求不保存 `plan_id`/version，新建另一个 Scheduler 实例接管已有同名 Step 时，无法仅从 AgentState 证明其历史 Plan 来源；该限制已列入人工确认项。

## 9. Ready Step 规则

Ready 是动态派生状态，不新增或持久化 `StepStatus.READY`。PlanStep 同时满足以下条件才 Ready：

1. AgentState 存在对应 StepState；
2. 状态为 `PENDING`；
3. Step 不在 `active_step_ids`；
4. Run 为 `RUNNING`；
5. 所有 `depends_on` 状态均为 `SUCCEEDED`；
6. 当前没有其他 `RUNNING` Plan Step。

依赖 `PENDING`/`RUNNING` 时继续等待；依赖 `FAILED`、`CANCELLED`、`BLOCKED` 或 `SKIPPED` 时传播阻断。

## 10. Step Claim 与重复调度防护

`claim_next()` 的关键路径在同一个实例 Lock 内完成：

```text
prepare
→ BLOCKED 不动点传播
→ 检查 RUNNING Plan Step
→ 计算 Ready Steps
→ 按 Plan.steps tuple 选择第一个
→ AgentStateMachine.apply_step_event(STARTED)
→ 校验 RUNNING + active_step_ids
→ 返回 StepClaim
```

只有 `STARTED` 成功且状态机确认 Step 为 `RUNNING`、位于 `active_step_ids` 后才返回 Claim。STARTED 抛错时转换为 `SchedulerClaimError`，不返回伪造 Claim。线程竞争测试验证同一实例上的两个线程最多一个成功。

## 11. BLOCKED 传播

阻断依赖状态为 `FAILED`、`CANCELLED`、`BLOCKED`、`SKIPPED`。Scheduler 只处理仍为 `PENDING` 的下游，并通过：

```text
AgentStateMachine.apply_step_event(
    StepStateEvent(
        StepEventType.BLOCKED,
        error_code="DEPENDENCY_NOT_SUCCESSFUL",
        error_message="前置步骤未成功，当前步骤无法执行",
    )
)
```

应用状态。实现按 Plan 顺序反复扫描，直到一轮没有新增 `BLOCKED`，因此多层依赖链会传播到稳定点。已 `BLOCKED` 的步骤不会重复发送事件。Scheduler 不修改 RunStatus。

## 12. 串行调度与公平性

串行语义只限制同一 Plan 同时最多一个由 Scheduler Claim 的 RUNNING Step；它不实现并行执行。多个 Ready Step 按不可变 `Plan.steps` tuple 顺序返回并 Claim 第一个，不使用 Set 遍历、随机、Priority、Aging 或抢占。

当前公平性仅是在有限静态 Plan 中提供确定、可重复的稳定顺序，不提供动态任务间或长期等待任务的高级公平保证；高级公平策略留给后续生产化。

## 13. 完成、等待和 unresolved 判断

- `is_complete`：仅当所有 Plan Step 都是 `SUCCEEDED` 时为 True；`SKIPPED` 不算成功完成。
- `is_waiting`：存在 RUNNING Plan Step，且当前没有可 Claim 的 Ready Step。
- `has_unresolved_pending`：仍有 PENDING Step、没有 RUNNING、没有 Ready，并且阻断依赖已经传播完成。

`has_unresolved_pending` 可能来自依赖环或尚未实现的依赖语义，但本次没有实现或宣称 DAG 环检测。没有 Ready 不等于完成。Scheduler 只报告派生判断，不改变 RunStatus。

## 14. 与 AgentLoop 的接入方式

默认 Legacy AgentLoop 已自行执行 `add_step → STARTED → execute → SUCCEEDED/FAILED/CANCELLED`。如果 Scheduler 先 Claim 同一个 Step，当前 Loop 会再次 `add_step` 和 `STARTED`，导致重复 ID 或非法状态转移，并形成双重生命周期所有权。

因此本次没有把 Scheduler 注入 `ChatService` 或默认 `AgentLoop`。实际接入方式是从 `core.runtime` 独立导入 `SerialScheduler`，由未来显式 Plan Executor 使用。当前默认路径不存在实际双重 STARTED，因为它完全没有消费 Scheduler；但如果未来仅在外围增加 Claim 而不迁移 AgentLoop 的 Step 创建/STARTED 所有权，双重 STARTED 风险真实存在。

未来安全接入必须让 Scheduler 独占 Plan Step 的注册与 STARTED，执行层接收 `StepClaim` 后只执行并发送 `SUCCEEDED/FAILED/CANCELLED`；不能再让 Legacy AgentLoop 为同一步骤创建和启动生命周期。本次不实施第 9 天迁移。

## 15. 与 Model Selection 的边界

`StepClaim` 只透传 `capability_requirements` 和 `preferred_agent`，方便后续执行层选择模型。Scheduler 不调用 `ModelSelectionPolicy.select()`、`ModelResolver.resolve()`，不保存 selected profile，不判断 DeepSeek/Qwen，不实现 Retry 或 fallback。第 7 天模型选择规则与 hybrid eager loading 均未修改。

## 16. 测试命令和结果

仓库 `.venv/Scripts/python.exe` 指向已不存在的 uv Python 路径；备用 Codex Python 本身没有 pytest。因此测试使用可用的 Codex Python，并通过 `PYTHONPATH=.venv/Lib/site-packages` 复用仓库现有测试包。未下载依赖、未访问网络、未启动真实模型、GGUF、Chroma、PyQt6、FastAPI 或数据库。

目标 pytest：

```powershell
$env:PYTHONPATH='D:\PythonProject\Local_Agent\.venv\Lib\site-packages'
& '<Codex Python>' -m pytest tests/test_scheduler.py tests/test_planning.py tests/test_model_selection.py -q
```

结果：`38 passed, 9 subtests passed`。首次运行仅有 pytest cache 路径权限 warning，不影响测试。

指定 unittest：

```powershell
& '<Codex Python>' -m unittest `
  tests.test_runtime_context `
  tests.test_agent_state `
  tests.test_agent_loop `
  tests.test_state_machine `
  tests.test_model_context `
  tests.test_planning `
  tests.test_model_selection `
  tests.test_scheduler -q
```

结果：`Ran 99 tests ... OK`。

全仓 pytest 使用获准写入的隔离 `--basetemp` 并禁用 cache provider：

```powershell
& '<Codex Python>' -m pytest -q -p no:cacheprovider --basetemp '<writable isolated pytest_tmp>'
```

结果：`155 passed, 26 subtests passed`。首次未指定 `--basetemp` 时有 12 个测试因系统 Temp 目录权限在 fixture setup 阶段报错；切换隔离目录后全部通过，证明不是业务失败。

## 17. 未完成事项和已知风险

- 未实现并行执行、Priority、Aging 或抢占。
- 未实现 DAG 环检测；环只能表现为 `has_unresolved_pending=True`。
- 未实现多进程、多机器、数据库锁、Lease 或分布式 Claim。
- 未实现 Scheduler/AgentState 持久化；进程退出后绑定和状态均丢失。
- Scheduler Lock 只保护单进程中的单个 `SerialScheduler` 实例；同一 Run 必须只使用该实例。
- 默认 Legacy AgentLoop 尚未消费 Scheduler。
- AgentState 仍只由当前调用栈持有并通过 observer 暴露临时快照。
- AgentLoop 捕获 `GeneratorExit` 后直接重新抛出，生成器 close 时 active Step 可能保持 RUNNING；该既有风险仍存在，本次未扩大范围修改。
- 未实现 Retry、Fallback 或生产调度队列。

## 18. 设计权衡和面试描述

本次选择“动态 Ready + State Machine 事件 + 实例锁”，而不是新增 READY 状态或执行队列。好处是 Plan 保持不可变、AgentState 继续作为执行事实来源、所有生命周期 Guard 仍统一经过 State Machine，并且线程内重复 Claim 有明确原子边界。代价是 Lock 只解决单进程单实例竞争，Plan 绑定没有持久化，公平性也只是静态顺序。

面试描述可表述为：我把 Scheduler 限制为协调层，不让它执行模型或结束 Run；Claim 在实例锁内重新计算事实状态并发送 STARTED，成功后才返回不可变凭据；依赖失败用不动点扫描传递 BLOCKED；默认旧 AgentLoop 已拥有 STARTED，所以先提供独立入口，避免用一次“集成”制造两个生命周期所有者。

## 19. 重点 Bad Case

### Bad Case 1：Ready 检查与 Claim 分离导致重复执行

- 类型：假设构造
- 触发条件：两个线程同时读取同一个 PENDING Step，并在没有共同锁的情况下分别发送 STARTED。
- 故障表现：同一步骤被执行两次，或第二次在执行前后收到非法状态转移。
- 根因分析：Ready 是瞬时派生事实，读取与状态转换不在同一个原子边界。
- 修复方案：`claim_next()` 用单实例 Lock 包住 prepare、传播、RUNNING 检查、Ready 计算、选择和 STARTED。
- 回归测试：`test_two_threads_can_only_claim_once`。
- 对应知识点：check-then-act 竞态、临界区、失败关闭。
- 面试表达：Claim 不是返回一个 ID，而是成功状态转移后才签发凭据。
- 当前状态：已覆盖；不宣称解决多进程或多机器竞争。

### Bad Case 2：只传播一层 BLOCKED

- 类型：假设构造
- 触发条件：`a` 失败，`b` 依赖 `a`，`c` 依赖 `b`，`d` 依赖 `c`。
- 故障表现：仅 `b` 被阻断，`c`、`d` 错误保留 PENDING。
- 根因分析：只扫描一轮，未把本轮新产生的 BLOCKED 当作下一轮阻断事实。
- 修复方案：按 Plan 顺序执行不动点扫描，直到一轮没有新增 BLOCKED。
- 回归测试：`test_blocked_propagates_to_fixed_point_without_duplicate_events`。
- 对应知识点：传递闭包、不动点计算、幂等事件传播。
- 面试表达：依赖失败传播必须收敛到稳定状态，不能只看原始失败节点。
- 当前状态：已覆盖，且重复 evaluate 不会重复发送 BLOCKED。

### Bad Case 3：没有 Ready Step 就误判 Plan 完成

- 类型：假设构造
- 触发条件：存在 RUNNING Step，或全部 PENDING Step 因环/未知依赖语义而没有 Ready。
- 故障表现：Scheduler 错误结束 Run 或把未执行任务报告为完成。
- 根因分析：把“当前不能 Claim”错误等同于“全部成功”。
- 修复方案：分别计算 `is_complete`、`is_waiting` 与 `has_unresolved_pending`，且 Scheduler 不修改 RunStatus。
- 回归测试：`test_dependency_pending_and_running_wait_without_blocking`、`test_cycle_is_only_reported_as_unresolved_pending`、`test_only_all_succeeded_is_complete_and_scheduler_keeps_run_running`。
- 对应知识点：状态语义正交、终止条件、未知状态保守处理。
- 面试表达：无可运行任务有三种含义，只有所有步骤成功才是完成。
- 当前状态：已覆盖；未实现环检测。

### Bad Case 4：使用 Set 选择 Ready Step 导致顺序不稳定

- 类型：假设构造
- 触发条件：多个无依赖或同时释放的 Step Ready，并按 Set/hash 顺序选择。
- 故障表现：不同进程或重复执行 Claim 不同步骤，测试与行为不可复现。
- 根因分析：无序容器被误用为调度顺序来源。
- 修复方案：Ready 计算和所有 Snapshot ID tuple 均按 `Plan.steps` 顺序生成。
- 回归测试：`test_ready_rules_and_plan_order_are_stable`、`test_success_releases_downstream_in_plan_order`。
- 对应知识点：确定性调度、可复现性、有限静态公平。
- 面试表达：Plan tuple 是唯一顺序真相，Set 只适合成员检查。
- 当前状态：已覆盖。

### Bad Case 5：Scheduler Claim 后 Legacy AgentLoop 再次 STARTED

- 类型：真实发现
- 触发条件：在当前 `AgentLoop.run_stream()` 外围增加 Scheduler Claim，但仍把同一 Plan Step 交给 Loop；Loop 会再次 `add_step` 和 STARTED。
- 故障表现：重复 step_id 或非法 `RUNNING → STARTED`，并可能诱发重复执行。
- 根因分析：Scheduler 与 Legacy AgentLoop 同时拥有 Step 注册和启动职责。
- 修复方案：本次不接入默认 Loop；未来集成时将 Plan Step 的 add/STARTED 所有权完整转移给 Scheduler，Executor 只负责执行和终止事件。
- 回归测试：现有 AgentLoop 测试确认 Loop 确实发送 STARTED；Scheduler 独立集成测试确认 Claim 也发送 STARTED。
- 对应知识点：生命周期单一所有者、迁移边界、兼容适配。
- 面试表达：安全集成不是多调用一个组件，而是先迁移状态转移所有权。
- 当前状态：风险已隔离，默认路径未接入 Scheduler，因此当前没有真实双重 STARTED。

## 20. 需要带回 ChatGPT 审查的信息

- Scheduler 文件和入口：`core/runtime/scheduler.py`；从 `core.runtime` 导出 `SerialScheduler.prepare/evaluate/claim_next`。
- Plan 所有者：当前真实路径由 `AgentRouter._select_model()` 临时创建后丢弃；未来显式 Plan Runtime/Executor 必须持有。
- AgentState 所有者：`ChatService.stream_chat()` 单次生成器调用栈创建并持有，`AgentLoop` 变更；未持久化。
- Step 注册入口：`SerialScheduler.prepare()`/`claim_next()` 内部调用 `AgentStateMachine.add_step()`。
- Ready 规则：PENDING、非 active、Run RUNNING、依赖全 SUCCEEDED、无其他 RUNNING Plan Step。
- 阻断依赖状态：FAILED、CANCELLED、BLOCKED、SKIPPED。
- BLOCKED 传播方式：State Machine `BLOCKED` 事件 + 按 Plan 顺序不动点扫描。
- Step Claim 原子边界：prepare → propagate → running check → ready → stable select → STARTED → Claim。
- Claim Lock 范围：单进程、单 `SerialScheduler` 实例；Lock 不序列化。
- Ready 顺序：严格按 `Plan.steps` tuple。
- 公平性语义：有限静态 Plan 的稳定顺序，无 Priority/Aging/抢占。
- `is_complete`：所有 Plan Step 都为 SUCCEEDED。
- `is_waiting`：有 RUNNING Plan Step且无 Ready。
- unresolved pending：有 PENDING、无 RUNNING、无 Ready、阻断传播已稳定；不等价于已检测环。
- Scheduler 是否修改 RunStatus：否。
- Scheduler 是否调用 Model Selection：否；只在 Claim 中透传能力需求和首选 Agent。
- 与 AgentLoop 的实际接入：独立可选入口，默认 Legacy AgentLoop 尚未消费。
- 是否存在双重 Step STARTED：当前默认路径没有；未来错误外围接入会有，必须迁移所有权。
- 是否实现并行、DAG、分布式锁：均否。
- 是否修改 AgentState Schema：否。
- 测试结果：目标 pytest 38 passed + 9 subtests；指定 unittest 99 OK；全仓 pytest 155 passed + 26 subtests。
- Bad Case：线程重复 Claim、一层 BLOCKED、无 Ready 误判完成、Set 顺序不稳、Legacy Loop 双重 STARTED。
- 需要人工确认的问题：第 9 天由哪个显式 Executor 接收 `StepClaim`；何时把默认 Loop 的 add/STARTED 所有权迁给 Scheduler；是否接受“新 Scheduler 接管同名已有 Step”无法从未扩展 Schema 的 AgentState 证明历史 Plan 来源；生产环境需采用何种持久化和跨进程 Claim 方案。
- 后续建议：先设计 `StepClaim → Executor → SUCCEEDED/FAILED/CANCELLED` 的显式接口和所有权迁移测试，再讨论持久化、DAG 诊断及分布式协调；本次没有实施第 9 天内容。
