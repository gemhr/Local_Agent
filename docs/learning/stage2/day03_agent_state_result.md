# 阶段二第 3 天改造结果

## 1. 本次任务目标

本次目标是在不拆分 `AgentRouter`、不实现完整 Runtime、状态机、Scheduler、Checkpoint 或持久化仓库的前提下，为真实聊天 Run 建立一个最小、明确、可序列化、可验证的 `AgentState` 模型。

该模型需要表达：Run 生命周期、Step 状态、当前运行步骤集合、最终停止原因、最终输出或安全错误摘要、状态结构版本，以及关键状态不变量。

## 2. 修改前现状

- `server.py::chat_endpoint()` 是 `/api/chat` 的 FastAPI 流式入口，通过 `StreamingResponse` 桥接应用服务。
- `core/chat_service.py::ChatService.stream_chat()` 是当前应用服务入口，负责拼接可选 `file_path` 文本，创建 `RunContext`，并把 Context 显式传给 `AgentRouter.chat_stream()`。
- `core/agent_router.py::AgentRouter.chat_stream()` 是遗留执行控制入口，继续承担用户消息持久化、单 Agent 回答、多 Agent 编排、RAG、Tool、Memory 和 `[[ORCH]]` 输出等职责。
- 阶段二第 2 天已存在 `RunContext`、`CancellationSource`、`CancellationToken`、UTC deadline 与 monotonic deadline，但没有 Run 状态对象。
- `ChatService.stream_chat()` 调用方只有 `server.py::chat_endpoint()` 和测试；`AgentRouter.chat_stream()` 真实生产调用方是 `ChatService.stream_chat()`，测试中也有 Fake Router 使用该兼容签名。
- 现有日志方式混合：服务启动中仍有少量 `print` 兼容输出，本次新增正式状态日志使用 `logging.getLogger(__name__)`。

## 3. 发现的问题

- RunContext 只描述标识、deadline 和取消 token，不适合保存生命周期状态、Step 状态或最终 StopReason。
- 遗留 `AgentRouter` 当前仍是一个大执行入口，无法在本阶段拆分出 Planner、Tool Step、RAG Step 等真实多步骤结构。
- 当前没有状态仓库、Checkpoint、Resume 或状态持久化机制。
- 当前取消来源没有接入真实 UI cancel API 或 HTTP 客户端断开信号，因此无法区分用户取消、客户端断开和系统关闭。
- 如果不加测试观察钩子，`AgentState` 只存在于 generator 生命周期内，测试无法可靠观测最终状态。

## 4. 最终设计方案

- 新增 `core/runtime/state.py`，提供最小状态模型：`AgentState`、`StepState`、`RunStatus`、`StepStatus`、`StopReason`。
- `AgentState` 和 `RunContext` 明确分工：
  - `RunContext` 保存运行标识、deadline、cancellation token、clock 等进程内依赖。
  - `AgentState` 只保存可序列化运行状态、Step 状态、StopReason、最终输出和安全错误摘要。
- `ChatService.stream_chat()` 在创建 `RunContext` 后创建同 run_id 的 `AgentState`，并验证二者 run_id 一致。
- 当前整个遗留 `AgentRouter.chat_stream()` 被表示为一个兼容 Step：`legacy-agent-router` / `Legacy AgentRouter execution`。
- `active_step_ids` 使用 `set[str]` 表达运行时可并行 active steps；序列化为稳定排序的 list。
- `steps` 运行时使用 `dict[str, StepState]`；序列化时按 step_id 稳定排序为 list。
- 通过明确方法修改状态，例如 `mark_running()`、`add_step()`、`start_step()`、`succeed_step()`、`fail_step()`、`cancel_step()`、`mark_succeeded()`、`mark_failed()`、`mark_cancelled()` 和 `validate()`。
- 为测试和诊断增加可选 `state_observer` 回调，但不在 `ChatService` 上保存 `_last_state` 或任何共享状态仓库。

## 5. 新增文件

- `core/runtime/state.py`
- `tests/test_agent_state.py`
- `docs/learning/stage2/day03_agent_state_result.md`

## 6. 修改文件

- `core/runtime/__init__.py`
- `core/chat_service.py`

## 7. 核心类、接口和数据结构

- `AgentState`：一次 Run 的可序列化状态对象，包含 `schema_version`、`run_id`、`status`、`created_at`、`updated_at`、`steps`、`active_step_ids`、`stop_reason`、`final_output`、`error_code`、`error_message`。
- `StepState`：一次 Step 的可序列化状态对象，包含 `step_id`、`name`、`status`、`created_at`、`started_at`、`ended_at`、`error_code`、`error_message`。
- `RunStatus`：`CREATED`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED`。
- `StepStatus`：`PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED`、`BLOCKED`、`SKIPPED`。
- `StopReason`：`COMPLETED`、`UNHANDLED_ERROR`、`DEADLINE_EXCEEDED`、`USER_CANCELLED`、`CLIENT_DISCONNECTED`、`SYSTEM_SHUTDOWN`、`MAX_STEPS_REACHED`、`NO_ACTION`、`REPEATED_ACTION`、`BUDGET_EXHAUSTED`。
- `schema_version`：当前固定为 `1`，常量名为 `AGENT_STATE_SCHEMA_VERSION`。
- 状态校验异常：`AgentStateValidationError`，用于状态不变量失败。
- `UnsupportedStateVersionError`：用于 `from_dict()` 遇到未知或过高 `schema_version`。

## 8. 关键执行流程

### AgentState 创建

`ChatService.stream_chat()` 调用 `create_run_context()` 后立即创建 `AgentState.for_run_context(run_context.run_id)`，并调用 `assert_matches_run_context()` 校验 `agent_state.run_id == run_context.run_id`。

### Run 启动

`AgentState` 初始状态为 `CREATED`。ChatService 添加兼容 Step 后调用 `mark_running()`，Run 进入 `RUNNING`。

### 兼容 Step 启动

ChatService 添加 `legacy-agent-router` Step，并调用 `start_step("legacy-agent-router")`，Step 从 `PENDING` 进入 `RUNNING`，同时加入 `active_step_ids`。

### 正常成功

`AgentRouter.chat_stream()` 正常迭代结束后：

- 兼容 Step 调用 `succeed_step()` 进入 `SUCCEEDED`；
- Run 调用 `mark_succeeded(final_output=...)` 进入 `SUCCEEDED`；
- `stop_reason` 设置为 `COMPLETED`；
- 文本 chunk 原样 yield，不改变现有流式协议或 `[[ORCH]]` 格式。

### Deadline 失败

捕获 `RunDeadlineExceededError` 后：

- 兼容 Step 调用 `fail_step(..., error_code="DEADLINE_EXCEEDED")`；
- Run 调用 `mark_failed(stop_reason=StopReason.DEADLINE_EXCEEDED, error_code="DEADLINE_EXCEEDED")`；
- 原异常继续向上抛出，由现有 API 错误边界处理。

### Cancellation

捕获 `RunCancelledError` 后：

- 兼容 Step 调用 `cancel_step(..., error_code="USER_CANCELLED")`；
- Run 调用 `mark_cancelled(stop_reason=StopReason.USER_CANCELLED)`；
- 当前无法区分真实 UI cancel、客户端断开或系统关闭，因此兼容映射为 `USER_CANCELLED`，不伪造 `CLIENT_DISCONNECTED`；
- 原异常继续向上抛出。

### 普通异常

捕获其他 `Exception` 后：

- 兼容 Step 调用 `fail_step(..., error_code="UNHANDLED_ERROR")`；
- Run 调用 `mark_failed(stop_reason=StopReason.UNHANDLED_ERROR, error_code="UNHANDLED_ERROR")`；
- 保存简短、无 traceback 的错误摘要；
- 原异常继续向上抛出。

### 序列化与反序列化

- `AgentState.to_dict()` 输出 JSON 友好的 dict。
- Enum 序列化为字符串值。
- datetime 序列化为 UTC ISO 8601 字符串。
- `active_step_ids` 稳定排序为 list。
- `steps` 按 step_id 稳定排序为 list。
- `AgentState.from_dict()` 校验 `schema_version`，重建 Enum 和 datetime，反序列化后重新执行 `validate()`。
- 不使用 pickle，不包含 Clock、Token、Event、Lock、异常对象或连接对象。

## 9. 与现有功能的兼容方式

- 未修改 `/api/chat` 请求体。
- 未修改 `StreamingResponse` 输出类型。
- 未修改现有文本流 chunk 内容。
- 未修改 `[[ORCH]]` 协议。
- 未修改 `AgentRouter` 的内部职责或拆分结构。
- 未修改 SQLite Memory Schema。
- 未新增状态表、状态仓库、Checkpoint 或 Resume。
- `AgentRouter.chat_stream()` 仍接收 `run_context` 兼容参数，ChatService 仍是 Runtime 最小接入边界。

## 10. 异常处理和边界情况

- `RunDeadlineExceededError` 映射为 Run `FAILED` + `DEADLINE_EXCEEDED`。
- `RunCancelledError` 映射为 Run `CANCELLED` + `USER_CANCELLED`。
- 其他异常映射为 Run `FAILED` + `UNHANDLED_ERROR`。
- 所有异常都会继续向上抛出，避免吞掉原异常。
- 错误摘要使用压缩空白并截断到 500 字符的简短字符串，不保存 traceback、异常对象或敏感上下文对象。
- 终态 Run 会清空 `active_step_ids`，保证终态没有 active steps。
- 当前不实现并发状态写入锁，状态只在单次 generator 同步执行路径内修改。

## 11. 测试内容

新增 `tests/test_agent_state.py`，覆盖：

- 初始状态与 `schema_version`。
- Run `CREATED -> RUNNING -> SUCCEEDED/FAILED/CANCELLED`。
- Step 添加、启动、成功、失败、取消。
- 终态和非终态 StopReason 不变量。
- 成功、失败、取消 StopReason 搭配规则。
- 终态无 active steps。
- 重复 Step ID、空 run_id、空 step_id、空 name 被拒绝。
- active step 必须存在且必须是 `RUNNING`。
- 所有 `RUNNING` Step 必须出现在 `active_step_ids`。
- 允许多个 Step 同时 `RUNNING`。
- Step 时间顺序和 UTC datetime 校验。
- 成功 Step 不得含错误信息，失败 Step 必须含错误摘要。
- `json.dumps(state.to_dict())` 成功。
- Enum、datetime、active_step_ids、steps 的序列化格式。
- `to_dict -> from_dict` 往返一致。
- 未知 schema version、非法 enum、反序列化非法状态被拒绝。
- ChatService 使用 Fake Router 验证正常、普通异常、Deadline、Cancellation 映射，验证原异常继续抛出、文本输出不变、不保存 `_last_state`。

## 12. 实际执行的测试命令

```bash
python -m unittest tests.test_agent_state -v
python -m unittest tests.test_runtime_context tests.test_agent_state -v
python -m compileall core tests
```

## 13. 测试结果

- `python -m unittest tests.test_agent_state -v`：通过，16 个测试通过。
- `python -m unittest tests.test_runtime_context tests.test_agent_state -v`：通过，31 个测试通过。
- `python -m compileall core tests`：通过。

## 14. 未完成事项

- 未实现完整 Agent Runtime。
- 未实现完整状态机、事件系统或状态转移表。
- 未实现 Agent Loop。
- 未实现 Planner、PlanStep、DAG 或 Scheduler。
- 未实现 Budget、Retry、Fallback、Circuit Breaker。
- 未实现 Checkpoint、Resume 或状态持久化。
- 未实现状态仓库。
- 未修改 Memory Schema。
- 未接入真实 UI cancel API、HTTP 客户端断开或系统 shutdown 信号。

## 15. 已知风险

- AgentState 当前只存在于 Generator 生命周期。
- AgentState 尚未持久化。
- 尚无状态仓库。
- 当前只有一个兼容 legacy step。
- 尚未实现完整 Agent Loop。
- 尚未实现状态机。
- 尚未实现并发状态写入控制。
- 取消来源仍未接入真实 UI 或连接断开。
- `session_id` 仍为 `legacy-default`。

## 16. 设计权衡

- 选择在 `ChatService` 创建和拥有 `AgentState`，因为它是当前 Runtime 最小接入边界，既能看到 `RunContext`，又不需要拆分 `AgentRouter`。
- 选择一个兼容 Step 表示整个遗留 `AgentRouter`，避免在第 3 天提前实现 Planner、Scheduler 或 Tool/RAG 子步骤。
- 选择 `set[str]` 作为运行时 `active_step_ids`，表达未来多个并行 Step；序列化时转为稳定排序 list 以保证 JSON 友好和测试稳定。
- 选择明确方法而非通用 `transition(event)`，避免提前引入第 5 天的完整状态机设计。
- 选择可选 `state_observer` 用于测试观察临时状态，避免新增生产级状态仓库或 `_last_state` 共享字段。

## 17. 可用于面试的项目描述

在 LocalAgent 的阶段二 Runtime 演进中，我为本地 AI Agent 项目补充了一个最小可序列化 `AgentState` 层，用于表达一次聊天 Run 的生命周期、兼容 Step 状态、StopReason、最终输出和安全错误摘要。该设计明确区分了 `RunContext` 的进程内依赖与 `AgentState` 的可序列化状态，提供 schema version、JSON 友好序列化、反序列化版本校验和状态不变量测试。当前没有声称实现完整状态机、持久化执行、Checkpoint、多步骤 Scheduler 或完整 Runtime，而是在不破坏现有流式输出和 `AgentRouter` 的前提下，为后续 Runtime 拆分建立了安全边界。

## 18. 需要带回 ChatGPT 审查的信息

- AgentState 的真实创建位置：`core/chat_service.py::ChatService.stream_chat()`。
- AgentState 的所有者：当前由 `ChatService` 在单次 generator 生命周期内临时拥有。
- 状态修改发生位置：`ChatService.stream_chat()` 调用 `AgentState` 的受约束方法；具体不变量和 mutation 方法在 `core/runtime/state.py`。
- RunStatus 最终枚举：`CREATED`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED`。
- StepStatus 最终枚举：`PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED`、`BLOCKED`、`SKIPPED`。
- StopReason 最终枚举：`COMPLETED`、`UNHANDLED_ERROR`、`DEADLINE_EXCEEDED`、`USER_CANCELLED`、`CLIENT_DISCONNECTED`、`SYSTEM_SHUTDOWN`、`MAX_STEPS_REACHED`、`NO_ACTION`、`REPEATED_ACTION`、`BUDGET_EXHAUSTED`。
- 状态不变量实现位置：`core/runtime/state.py::AgentState.validate()` 和 `core/runtime/state.py::StepState.validate()`。
- `active_step_ids` 数据结构：运行时为 `set[str]`，序列化为稳定排序 `list[str]`。
- 是否允许多个 RUNNING Step：允许，测试已覆盖两个 Step 同时 `RUNNING`。
- schema_version：`1`。
- 序列化格式：`AgentState.to_dict()` 输出 JSON 友好 dict，Enum 为字符串，datetime 为 UTC ISO 8601，steps 和 active ids 稳定排序。
- from_dict 如何处理未知版本：抛出 `UnsupportedStateVersionError`。
- 兼容 Step 的 ID 和语义：`legacy-agent-router`，语义为整个遗留 `AgentRouter.chat_stream()` 执行。
- 成功映射：Step `SUCCEEDED`，Run `SUCCEEDED`，StopReason `COMPLETED`。
- 普通失败映射：Step `FAILED`，Run `FAILED`，StopReason `UNHANDLED_ERROR`。
- Deadline 映射：Step `FAILED`，Run `FAILED`，StopReason `DEADLINE_EXCEEDED`。
- Cancellation 映射：Step `CANCELLED`，Run `CANCELLED`，StopReason `USER_CANCELLED`；当前不伪造客户端断开。
- 错误信息如何脱敏：保存压缩空白、最多 500 字符的简短字符串，不保存 traceback 或异常对象。
- 修改前真实调用链：`server.py::chat_endpoint()` → `ChatService.stream_chat()` → `AgentRouter.chat_stream()`。
- 修改后真实调用链：`server.py::chat_endpoint()` → `ChatService.stream_chat()` 创建 `RunContext` 和 `AgentState` → 兼容 Step 包裹 `AgentRouter.chat_stream()`。
- 新增文件：`core/runtime/state.py`、`tests/test_agent_state.py`、`docs/learning/stage2/day03_agent_state_result.md`。
- 修改文件：`core/runtime/__init__.py`、`core/chat_service.py`。
- 测试命令和测试结果：`python -m unittest tests.test_runtime_context tests.test_agent_state -v` 通过；`python -m compileall core tests` 通过。
- 测试失败或无法执行原因：无失败；未执行真实模型、Chroma、PyQt6 UI、外部网络或真实数据库写入。
- 尚不确定的问题：未来如何从 UI 或 HTTP 层准确区分用户取消、客户端断开和系统关闭；未来状态持久化边界如何设计。
- 后续建议：第 4 天可以继续讨论状态事件或 Runtime 边界，但不要直接在本次实现完整状态机、Checkpoint 或 Scheduler。

## 19. 补充审查修正（错误摘要、BLOCKED、Generator close、Schema Version）

### 19.1 未知异常的安全映射

补充审查后，普通未知异常不再把 `str(exc)`、压缩后的异常文本或截断文本写入 `AgentState`。未知异常统一保存：

* `error_code = "UNHANDLED_ERROR"`
* `error_message = "Agent execution failed"`

`RunDeadlineExceededError` 使用固定安全摘要 `Run deadline exceeded`，并映射到 `StopReason.DEADLINE_EXCEEDED`；`RunCancelledError` 使用固定安全摘要 `Run cancelled`，并继续兼容映射到 `StopReason.USER_CANCELLED`。原始异常仅通过 `core.chat_service` 中的 `logger.exception("AgentRouter execution failed")` 进入项目受控日志，`AgentState` 不保存原始异常对象、完整 traceback 或通用 `str(exc)`。这里不再将“压缩和截断异常文本”称为脱敏。

### 19.2 BLOCKED 的最终语义

`StepStatus.BLOCKED` 明确定义为：由于前置依赖失败、取消或不可满足，该 Step 在本次 Run 中确定不会执行。尚未满足依赖但未来仍可能执行的 Step 应保持 `PENDING`，不能提前标为 `BLOCKED`。

BLOCKED Step 的不变量为：

* `started_at is None`
* `ended_at is not None`
* 不得出现在 `active_step_ids`
* 可以包含安全的 `error_code` 或简短原因

本次只补充状态语义、`block_step()` 受控修改方法和校验/测试，不实现依赖图、Planner、Scheduler 或 DAG。

### 19.3 Generator 提前关闭行为

已检查 `stream = ChatService.stream_chat(...); next(stream); stream.close()` 行为。由于 `GeneratorExit` 不属于 `Exception`，当前代码不会进入普通异常映射分支，也不会执行正常完成后的 `succeed_step()` / `mark_succeeded()` 成功收尾逻辑；同时没有把提前关闭擅自标记为 `CLIENT_DISCONNECTED`。

已知限制：`AgentState` 当前只存在于 generator 生命周期中，提前关闭时无法形成可靠终态，也不会持久化一个取消/断开状态。真实客户端断开传播和可靠终态记录留到后续阶段（第 12 天方向）处理。

### 19.4 Schema Version 新增边界

`AgentState.from_dict()` 现在明确拒绝：

* 缺少 `schema_version`
* 未知版本，例如 `999`
* 布尔版本，例如 `True`，不会被当作整数 `1` 接受
* 非整数版本，例如字符串 `"1"`

反序列化完成后仍会重新执行全部状态不变量校验，包括 Run/Step 时间、终态、active step、StopReason 搭配等规则。

### 19.5 新增测试和结果

补充测试覆盖：

* 未知异常不会把原始异常文本写入 `AgentState`
* BLOCKED Step 合法状态
* BLOCKED Step 不得位于 `active_step_ids`
* Generator 提前关闭不会执行成功收尾
* 缺失、布尔、非整数和未知 `schema_version` 被拒绝

实际执行命令：

```text
python -m unittest tests.test_runtime_context tests.test_agent_state -v
python -m compileall core tests
```

结果：两条命令均通过。

### 19.6 当前仍未实现的内容

本次补充仍未实现 Agent Loop、完整状态机、状态事件、Scheduler、DAG、依赖调度、Checkpoint、状态数据库、cancel API、客户端断开传播、API 请求体变更、Memory Schema 变更、文本流协议变更或 `[[ORCH]]` 协议变更。
