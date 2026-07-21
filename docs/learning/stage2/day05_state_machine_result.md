# 阶段二第 5 天改造结果

## 1. 本次任务目标

在现有 `RunContext`、`AgentState` 和 `AgentLoop` 之上增加一个最小、同步、内存内 `AgentStateMachine`。`AgentLoop` 只产生 Run/Step 状态事件，状态机统一校验转移规则、Guard 和终态保护，并原子更新 `AgentState`。

本次保持流式文本、`[[ORCH]]` 协议、API、Memory Schema 和 `AgentState schema_version = 1` 不变；不实现 Event Sourcing、Runtime Event 总线、Planner、Scheduler、Budget 执行或状态持久化。

## 2. 修改前现状

- `ChatService.stream_chat()` 创建 `RunContext`、`AgentState`、`LegacyAgentRouterDriver` 和 `AgentLoop`。
- `AgentLoop.run_stream()` 已负责最大步骤、无动作、重复动作、Deadline、Cancellation 和未知异常处理。
- `AgentLoop` 直接调用 `AgentState.mark_running()`、`add_step()`、`start_step()`、`succeed_step()`、`fail_step()`、`cancel_step()`、`mark_succeeded()`、`mark_failed()` 和 `mark_cancelled()`。
- `AgentState` 与 `StepState` 已有完整不变量、UTC 时间和 JSON 友好序列化能力，但没有显式 Event、Transition、Guard 和终态事件保护。

## 3. 发现的问题

- 生产 Loop 可以直接绕过统一转移规则修改状态，无法集中审查合法事件和事件顺序。
- 现有 mutation 采用“先修改、再 `validate()`”的实现方式；如果修改后校验失败，原对象可能留下部分修改。
- `StepStatus.SKIPPED` 已存在，但没有对应 mutation，继续扩张一组离散 mutation 会让状态规则更分散。
- `AgentState.validate()` 能保证最终快照自洽，但不能单独表达“终态后拒绝迟到事件”或“重复终态事件严格失败”。
- Run 完成、失败或取消前必须先处理 active Step，这个调用顺序此前主要依赖 `AgentLoop` 自觉遵守。

## 4. 最终设计方案

新增单文件 `core/runtime/state_machine.py`，集中定义：

- `RunEventType`、`StepEventType`
- `RunStateEvent`、`StepStateEvent`
- `InvalidStateTransitionError`
- `AgentStateMachine`
- 不可变 Run/Step 转移表和 StopReason 映射
- Event 字段校验、Terminal State 保护、Guard 和原子提交

状态机不保存 Run 状态，不访问数据库、Model、Tool、RAG、CancellationToken，也不输出流式 chunk。每个 `AgentLoop` 默认创建自己的状态机实例，也可通过构造器注入测试实例。

Step 注册通过 `AgentStateMachine.add_step()` 收口；它不是新的生命周期 Event，只负责在 `RUNNING` Run 中原子注册 `PENDING` Step。注册后，Loop 再发送 `StepEventType.STARTED`。

## 5. 新增文件

- `core/runtime/state_machine.py`
- `tests/test_state_machine.py`
- `docs/learning/stage2/day05_state_machine_result.md`

## 6. 修改文件

- `core/runtime/agent_loop.py`：注入并调用 `AgentStateMachine`，将直接 lifecycle mutation 改为 Run/Step Event。
- `core/runtime/__init__.py`：导出状态机公共类型。
- `tests/test_agent_loop.py`：增加真实状态机事件顺序、策略终止事件和无直接 mutation 调用测试。

`core/runtime/state.py`、`core/chat_service.py`、API、Memory Schema 和流式协议未修改。

## 7. 核心类、接口和数据结构

### AgentStateMachine

公共入口：

```python
add_step(state, step_id=..., name=...)
apply_run_event(state, event)
apply_step_event(state, event)
```

状态机仅接收调用方传入的 `AgentState` 和强类型 Event，不持有全局 Run 状态。

### RunEventType

最终枚举为：

```text
STARTED
COMPLETED
FAILED
DEADLINE_EXCEEDED
MAX_STEPS_REACHED
NO_ACTION
REPEATED_ACTION
BUDGET_EXHAUSTED
CANCELLED
```

`BUDGET_EXHAUSTED` 仅建立 `FAILED / StopReason.BUDGET_EXHAUSTED` 状态语义，本次没有 Budget 计量或终止逻辑。

### StepEventType

最终枚举为：

```text
STARTED
SUCCEEDED
FAILED
CANCELLED
BLOCKED
SKIPPED
```

### RunStateEvent

包含 `event_type`、`occurred_at`、`stop_reason`、`final_output`、`error_code` 和 `error_message`。Event 是 frozen dataclass，不接受异常对象或通用 payload。

校验包括：UTC aware 时间、非空安全 error code、单行且不含 traceback 的安全摘要、COMPLETED 不携带错误、失败类事件使用合法失败 StopReason、专用失败事件与 StopReason 一一对应、CANCELLED 只使用合法取消 StopReason。

### StepStateEvent

包含 `event_type`、`step_id`、`occurred_at`、`error_code` 和 `error_message`。空 Step ID 被拒绝；SUCCEEDED、STARTED 和 SKIPPED 不允许错误字段；FAILED 至少包含 error code 或安全摘要。

### InvalidStateTransitionError

仅包含安全转移上下文：

- `entity_type`
- `current_status`
- `event_type`
- `entity_id`（run_id 或 step_id）
- 固定安全 `reason`

异常不拼接用户输入、Prompt、Tool 参数、文件路径、原始异常或 traceback。

### Guard

Guard 位于 `AgentStateMachine.apply_run_event()`、`apply_step_event()` 及其私有辅助方法中，在候选状态修改前执行。

### Terminal State

`SUCCEEDED / FAILED / CANCELLED` Run 和 `SUCCEEDED / FAILED / CANCELLED / BLOCKED / SKIPPED` Step 拒绝任何后续事件。Run 进入终态后也拒绝所有子 Step 事件。重复终态事件不会静默视为成功。

## 8. Run 状态转移表

| 当前状态 | Event | 目标状态 | 说明 |
|---|---|---|---|
| CREATED | STARTED | RUNNING | 正常启动 |
| CREATED | FAILED | FAILED | 启动前普通失败 |
| CREATED | CANCELLED | CANCELLED | 启动前取消 |
| RUNNING | COMPLETED | SUCCEEDED | 必须无 active/running Step |
| RUNNING | FAILED | FAILED | 必须无 active/running Step |
| RUNNING | DEADLINE_EXCEEDED | FAILED | StopReason 固定映射 |
| RUNNING | MAX_STEPS_REACHED | FAILED | StopReason 固定映射 |
| RUNNING | NO_ACTION | FAILED | StopReason 固定映射 |
| RUNNING | REPEATED_ACTION | FAILED | StopReason 固定映射 |
| RUNNING | BUDGET_EXHAUSTED | FAILED | 仅状态语义 |
| RUNNING | CANCELLED | CANCELLED | 必须无 active/running Step |
| 任意终态 | 任意 Event | 拒绝 | 严格终态保护 |

除表中组合外均抛出 `InvalidStateTransitionError`。特别地，`CREATED -> SUCCEEDED` 和 `RUNNING -> STARTED` 不合法。

## 9. Step 状态转移表

| 当前状态 | Event | 目标状态 | 说明 |
|---|---|---|---|
| PENDING | STARTED | RUNNING | Run 必须为 RUNNING，加入 active 集合 |
| PENDING | CANCELLED | CANCELLED | 不加入 active 集合 |
| PENDING | BLOCKED | BLOCKED | 从未启动，不在 active 集合 |
| PENDING | SKIPPED | SKIPPED | 从未启动，不在 active 集合 |
| RUNNING | SUCCEEDED | SUCCEEDED | 必须位于 active 集合 |
| RUNNING | FAILED | FAILED | 必须位于 active 集合 |
| RUNNING | CANCELLED | CANCELLED | 必须位于 active 集合 |
| 任意终态 | 任意 Event | 拒绝 | 严格终态保护 |

除表中组合外均不合法。特别地，`PENDING -> SUCCEEDED/FAILED`、`RUNNING -> BLOCKED/SKIPPED` 和重复 STARTED 均被拒绝。

## 10. Guard 和原子性

已实现的主要 Guard：

- Run 完成、失败或取消前，`active_step_ids` 必须为空且不得存在 `RUNNING` Step。
- Run 完成时，通过候选状态的 `validate()` 校验 final output 和全部现有 AgentState 不变量。
- Step 必须存在，事件时间不得早于状态更新时间或 Step 创建/启动时间。
- Run 进入终态后不得再应用任何 Step 事件。
- Step STARTED 前，Run 必须为 RUNNING、Step 必须为 PENDING 且不在 active 集合。
- RUNNING Step 完成、失败或取消前，必须位于 active 集合。
- BLOCKED/SKIPPED 只能作用于未启动且不在 active 集合的 PENDING Step。
- 状态机注册新 Step 时，Run 必须为 RUNNING。

原子性实现不是“先修改原对象再校验”，而是：

1. 校验输入 AgentState、Event、转移表和全部 Guard。
2. 通过 `AgentState.to_dict()` / `from_dict()` 创建独立候选副本。
3. 只修改候选副本并执行 `candidate.validate()`。
4. 候选完整合法后才提交到原 AgentState，并再次执行最终 `validate()`。

因此 Guard、转移或候选校验失败时，原 AgentState 与 StepState 不发生部分修改。测试使用 `before = state.to_dict()` 和失败后的 `after = state.to_dict()` 验证一致性。

## 11. Agent Loop 集成

`AgentLoop.__init__(policy=None, state_machine=None)` 支持显式注入状态机；未传入时为该 Loop 创建新的 `AgentStateMachine`，没有全局单例。

事件顺序：

- 正常完成：`RUN_STARTED -> STEP_STARTED -> STEP_SUCCEEDED -> RUN_COMPLETED`
- Deadline（已有 active Step）：`RUN_STARTED -> STEP_STARTED -> STEP_FAILED -> RUN_DEADLINE_EXCEEDED`
- Cancellation（已有 active Step）：`RUN_STARTED -> STEP_STARTED -> STEP_CANCELLED -> RUN_CANCELLED`
- 最大步骤：已有 Step 全部终止后发送 `RUN_MAX_STEPS_REACHED`
- 无动作：不创建 Step，直接发送 `RUN_NO_ACTION`
- 重复动作超限：不创建超限 Step，直接发送 `RUN_REPEATED_ACTION`
- observation 失败或未知异常：`STEP_FAILED -> RUN_FAILED`

未知异常仍通过受控 logger 记录并继续向上传播；Event 和 AgentState 只保存固定安全错误摘要。

## 12. 与现有功能兼容方式

- `LegacyAgentRouterDriver`、Router 调用次数和 RunContext 传递方式不变。
- 所有流式 chunk 继续按原顺序 yield。
- `[[ORCH]]` chunk 继续原样传输，但不进入 `AgentState.final_output`。
- Deadline 和 Cancellation 异常仍在完成状态映射后继续向上抛出。
- `GeneratorExit` 仍直接重新抛出，不伪造成功或客户端断开终态。
- `AgentState` 所有原 mutation 方法继续保留，供旧测试和后续迁移使用。
- 生产 `AgentLoop` 已不直接调用这些 mutation；Step 注册也通过状态机收口。

## 13. 测试内容

`tests/test_state_machine.py` 覆盖：

- Run 合法启动、成功、普通失败、取消及 5 种专用失败映射。
- Step STARTED、SUCCEEDED、FAILED、CANCELLED、BLOCKED 和 SKIPPED。
- CREATED 直接成功、RUNNING 重复启动、非法 Step 转移、终态迟到事件、未知 Step。
- Run 非 RUNNING 时启动 Step、RUNNING Step 不在 active 集合、active Step 阻止 Run 终态。
- 典型非法 Run/Step 转移前后状态快照一致。
- naive datetime、空 Step ID、成功事件携带错误、失败缺少安全错误、非法取消原因。
- `InvalidStateTransitionError` 的安全字段。

`tests/test_agent_loop.py` 新增：

- 正常、Deadline、Cancellation、最大步骤、无动作、重复动作和未知异常事件顺序。
- 构造器注入真实 Recording State Machine。
- `AgentLoop.run_stream()` 不直接调用 AgentState lifecycle mutation 或 `add_step()`。
- 既有流式输出和 `[[ORCH]]` 行为继续回归。

## 14. 实际测试命令

先按 `uv.lock` 重建项目 Python 3.12 环境并补齐开发依赖，再执行指定 Runtime 测试和全仓 pytest：

```powershell
uv sync --dev --frozen
uv run python -c "import pytest, langchain_chroma, langchain_core, fastapi; print('dependency imports: ok')"
uv run python -m unittest tests.test_runtime_context tests.test_agent_state tests.test_agent_loop tests.test_state_machine -v
uv run pytest -q
uv pip check
uv run python -m compileall core tests
git diff --check
```

## 15. 测试结果

- 指定 Runtime 测试：69 项通过。
- 全仓 pytest：104 项通过，另有 17 个 subtests 通过。
- `pytest`、`langchain_chroma`、`langchain_core` 和 `fastapi` 导入验证通过。
- `uv pip check`：检查 134 个已安装包，全部依赖兼容。
- `compileall core tests`：通过。
- `git diff --check`：通过。
- 未启动真实模型、Chroma、PyQt6、FastAPI 服务或数据库；测试过程未访问外部网络。

## 16. 未完成事项和已知风险

- 当前不是 Event Sourcing，事件不持久化，也没有 Event Store。
- 当前没有 Runtime Event 总线，Run/Step Event 只是同步方法入参。
- 当前没有 Planner、Scheduler、DAG、并行执行、Retry 或 Budget 实现。
- `BUDGET_EXHAUSTED` 只有状态语义，没有计量和触发来源。
- AgentState mutation 方法仍保留；生产 Loop 不再绕过状态机，但兼容单元测试仍直接使用旧方法。
- `AgentState` 仍只存在于 generator 生命周期内，不持久化。
- Generator close 仍无法形成可靠终态，也不能证明外部副作用未发生。
- 模型轻重路由、Provider fallback 和 Circuit Breaker 尚未实现。
- Event 的安全摘要校验能拒绝空值、多行文本和 traceback，但调用方仍必须只传入预先定义的安全摘要，不能把原始异常或敏感信息写入 Event。

## 17. 设计权衡

- 选择单文件状态机，避免为第一版拆分大量 Event/Guard 文件。
- 选择 frozen dataclass Event 和显式字段，而不是 `dict[str, Any]` payload。
- 选择严格终态保护，不把重复或迟到事件视为幂等成功；幂等消费留给后续阶段。
- 选择“候选副本校验后提交”保证失败原子性，代价是每次转移需要一次内存内序列化复制。当前状态规模小、同步执行，正确性优先于这部分微小开销。
- 选择保留旧 mutation，避免大规模重写第 3 天测试；通过生产调用检索和回归测试保证 AgentLoop 不再绕过状态机。
- Step 注册没有引入 `CREATED` Event，因为本次要求的 Step Event 从已存在 PENDING Step 开始；注册仍通过状态机方法收口。

## 18. 可用于面试的项目描述

我在 LocalAgent 的遗留流式 Agent Loop 外增加了一个最小同步状态机：用强类型 Run/Step Event 和显式转移表统一管理生命周期，增加 active Step、终态和时间顺序 Guard，并通过候选状态副本校验后提交保证非法转移不留下半修改状态。AgentLoop 通过构造器注入状态机并保持原有流式协议与 `[[ORCH]]` 控制内容分离。该实现是进程内状态转移边界，不是 Event Sourcing、分布式状态机或 Durable Execution。

## 19. 重点 Bad Case

### Bad Case 1：Run 先成功，Step 后成功

- 类型：假设构造
- 触发条件：Run 为 RUNNING，仍存在 active RUNNING Step，却先收到 `RUN_COMPLETED`。
- 故障表现：如果直接清空 active 集合并成功收尾，Run 会先变为 SUCCEEDED，后续 Step 成功事件将与 Run 终态冲突。
- 根因分析：Run 终态与 Step 终态存在顺序约束，单靠最终字段赋值无法表达该 Guard。
- 修复方案：Run 进入任何终态前检查 `active_step_ids` 和所有 Step 状态；存在 active/running Step 时拒绝事件。
- 回归测试：`test_active_step_blocks_all_run_terminal_transitions` 和 `test_invalid_run_and_step_transitions_are_atomic` 断言异常前后 `to_dict()` 完全一致。
- 对应知识点：聚合根不变量、Guard、父子状态机顺序、失败原子性。
- 面试表达：我把 Run 成功定义为所有 active Step 已先终止的聚合条件，正确顺序是 `STEP_SUCCEEDED -> RUN_COMPLETED`，反序事件由 Guard 拒绝。
- 当前状态：已通过状态机和回归测试解决。

### Bad Case 2：终态后收到迟到失败事件

- 类型：假设构造
- 触发条件：Run 已经 SUCCEEDED，随后收到迟到的 `RUN_FAILED`。
- 故障表现：若旧 mutation 直接覆盖字段，成功结果可能被迟到失败改写。
- 根因分析：缺少 Terminal State 保护和明确的重复/迟到事件策略。
- 修复方案：所有 Run/Step 终态拒绝全部后续事件，本阶段采用严格模式，不静默幂等。
- 回归测试：`test_terminal_run_and_step_reject_late_events` 验证抛出 `InvalidStateTransitionError` 且状态快照不变。
- 对应知识点：终态吸收、迟到事件、幂等策略、状态一致性。
- 面试表达：我明确区分“严格状态转移”和“幂等事件消费”；第一版先保护成功终态，重复事件的幂等键和消费记录留给后续持久化阶段。
- 当前状态：已通过严格终态保护解决；尚未实现幂等消费。

### Bad Case 3：非法转移留下半修改状态

- 类型：真实发现
- 触发条件：现有 `AgentState` mutation 先写入 status、时间、错误字段或 active 集合，再调用 `_touch_and_validate()`；若最终校验失败，原对象已被修改。
- 故障表现：调用方捕获异常后仍可能持有部分变更的 AgentState，后续判断基于污染状态继续执行。
- 根因分析：校验发生在原对象 mutation 之后，缺少候选状态或回滚边界。
- 修复方案：状态机先完成 Event、Transition 和 Guard 校验，再在序列化候选副本上修改并 `validate()`，全部成功后才提交原对象。
- 回归测试：典型非法 Run/Step 转移均保存 before/after `to_dict()` 并断言完全相等。
- 对应知识点：强异常安全保证、copy-on-write、事务式状态更新、不变量校验。
- 面试表达：我没有依赖“失败路径理论上不会发生”，而是把状态转移做成候选副本提交，让非法事件对原聚合状态具有强异常安全保证。
- 当前状态：生产 AgentLoop 路径已解决；旧 mutation 为兼容测试仍保留，不应被新增生产代码直接调用。

## 20. 需要带回 ChatGPT 审查的信息

- State Machine 真实文件和入口：`core/runtime/state_machine.py::AgentStateMachine.apply_run_event()`、`apply_step_event()`、`add_step()`。
- Agent Loop 注入方式：`AgentLoop.__init__(policy=None, state_machine=None)`；默认创建实例，测试可注入 Recording State Machine。
- Run Event 最终枚举：STARTED、COMPLETED、FAILED、DEADLINE_EXCEEDED、MAX_STEPS_REACHED、NO_ACTION、REPEATED_ACTION、BUDGET_EXHAUSTED、CANCELLED。
- Step Event 最终枚举：STARTED、SUCCEEDED、FAILED、CANCELLED、BLOCKED、SKIPPED。
- Run/Step 转移表：见第 8、9 节。
- Guard 实现位置：`AgentStateMachine.apply_run_event()`、`apply_step_event()`、`_guard_run_has_no_active_steps()` 和 `_guard_event_time()`。
- 原子性：原状态预校验 -> 序列化候选副本 -> 候选 mutation -> `candidate.validate()` -> 提交 -> 最终 `state.validate()`。
- 非法转移异常：安全实体类型、当前状态、事件类型、run_id/step_id 和固定原因；不含用户内容或原始异常。
- 终态保护：所有 Run/Step 终态拒绝后续事件。
- 重复事件策略：严格拒绝，不做幂等成功。
- Agent Loop 事件顺序：见第 11 节并由 Recording State Machine 测试验证。
- AgentState mutation：全部保留；生产 AgentLoop 已不直接调用，状态机内部只在候选副本上复用 `add_step()`。
- 生产代码绕过状态机：当前检索未发现 AgentLoop 或其他 `core` 调用旧 lifecycle mutation。
- AgentState Schema：未修改，仍为版本 1；没有新增事件历史、revision 或 checkpoint 字段。
- Event Store / Runtime Event：均未实现。
- 新增/修改文件：见第 5、6 节。
- 测试命令与结果：见第 14、15 节；指定 Runtime 测试、全仓 pytest、compileall 和 diff 检查全部通过。
- Bad Case：见第 19 节，其中前两个是假设构造，第三个是真实代码风险。
- Commit / PR：本次未创建 Commit、未 Push、未创建或更新 PR。
- 需要人工确认：后续是否允许旧 mutation 降级为私有 API；未来 Event 安全摘要是否需要集中错误码目录；真实客户端断开如何映射 Cancellation StopReason。
- 后续建议：下一阶段可先评审状态机 API 和错误码目录，再按既定阶段处理持久化或 Runtime Event；本次不实施第 6 天内容。
