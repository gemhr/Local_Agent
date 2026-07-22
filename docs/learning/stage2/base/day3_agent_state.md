# 阶段二第 3 天：AgentState、状态枚举与状态不变量

**今日状态：进行中。**

第 2 天建立的 RunContext（运行上下文）回答的是：

> 这是谁的一次运行、什么时候截止、是否应该取消。

今天的 AgentState（智能体状态）回答的是：

> 这次运行现在执行到了哪里、已经发生了什么、最终为什么停止。

两者的核心区别：

```text
RunContext：运行环境，创建后基本保持稳定
AgentState：执行事实，随运行过程不断演进
```

------

# 1. 当天目标

今天必须完成：

1. 定义 AgentState 的职责和数据边界。
2. 定义 RunStatus（运行状态）。
3. 定义 StepStatus（步骤状态）。
4. 定义 StopReason（终止原因）。
5. 明确状态序列化格式。
6. 引入 `schema_version`，支持未来版本演进。
7. 建立状态不变量，阻止矛盾状态进入系统。
8. 在 LocalAgent 中最小接入真实执行链。
9. 不提前实现 State Machine（状态机）。

## 今天不处理

今天暂不实现：

- 基于事件的完整状态机；
- 状态转移表；
- Agent Loop（智能体循环）；
- Planner 和 PlanStep；
- DAG（有向无环图）；
- Scheduler（调度器）；
- Checkpoint（检查点）；
- 状态数据库；
- 多进程状态同步；
- 乐观锁和并发更新；
- 人工审批状态；
- Retry（重试）和 Budget（预算）。

------

# 2. RunContext 与 AgentState 的边界

## 2.1 RunContext

第 2 天已经完成，保存：

- `run_id`
- `session_id`
- `trace_id`
- 创建时间
- Deadline（截止时间）
- Cancellation Token（取消令牌）
- Clock（时钟）等运行依赖

它不应该因为执行了一次 Tool 或完成了一个 Step 而不断改变。

## 2.2 AgentState

AgentState 保存执行过程中不断变化的事实：

- 当前 RunStatus；
- 有哪些步骤；
- 哪些步骤正在运行；
- 哪些步骤已经成功或失败；
- 是否已经产生最终结果；
- 是否存在结构化错误；
- 最终为什么停止；
- 状态最后更新时间。

推荐关系：

```text
RunContext
    run_id = run-001
    trace_id = trace-001
    deadline = ...

AgentState
    run_id = run-001
    status = RUNNING
    steps = [...]
    active_step_ids = [...]
    stop_reason = None
```

两者通过 `run_id` 关联。

------

# 3. 为什么不能用日志代替 AgentState

LocalAgent 当前已经有：

- 普通文本输出；
- `[[ORCH]]` 编排事件；
- SQLite Memory；
- 错误字符串；
- 日志。

但这些都不能作为可信 AgentState。

例如日志中可能出现：

```text
开始调用模型
工具执行完成
模型生成失败
```

你仍然无法可靠回答：

- Run 最终是成功还是失败？
- 哪个 Step 当前正在运行？
- 一个失败 Step 是否已经结束？
- 用户取消和系统异常是否被区分？
- 当前状态能否恢复？
- 是否出现了非法状态组合？

日志是对状态变化的描述，AgentState 才是当前执行事实的结构化表示。

正确关系应该是：

```text
状态发生变化
→ 更新 AgentState
→ 生成日志或 Runtime Event
```

而不是：

```text
输出了一条日志
→ 猜测当前是什么状态
```

------

# 4. RunStatus：Run 级生命周期

推荐第一个版本保持少而稳定：

```python
class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

## 4.1 为什么不立刻增加几十种状态

暂时不建议增加：

- `TIMED_OUT`
- `BUDGET_EXHAUSTED`
- `WAITING_APPROVAL`
- `RETRYING`
- `PLANNING`
- `EXECUTING_TOOL`
- `RETRIEVING`

因为其中有些是：

- StopReason；
- Step 状态；
- 当前阶段；
- Runtime Event；
- 后续功能才会出现的状态。

如果把所有信息都塞进 RunStatus，很快会产生状态爆炸：

```text
RUNNING_MODEL
RUNNING_TOOL
RUNNING_RAG
RUNNING_RETRY
RUNNING_REPLAN
WAITING_TOOL_RETRY
...
```

更合理的是：

```text
RunStatus：生命周期大类
StepStatus：具体步骤状态
StopReason：最终停止原因
Runtime Event：过程事实
```

------

# 5. StopReason：为什么停止

StopReason 只用于解释终态，不用于表达正在执行的阶段。

建议第一个版本定义：

```python
class StopReason(str, Enum):
    COMPLETED = "completed"

    UNHANDLED_ERROR = "unhandled_error"
    DEADLINE_EXCEEDED = "deadline_exceeded"

    USER_CANCELLED = "user_cancelled"
    CLIENT_DISCONNECTED = "client_disconnected"
    SYSTEM_SHUTDOWN = "system_shutdown"

    MAX_STEPS_REACHED = "max_steps_reached"
    NO_ACTION = "no_action"
    REPEATED_ACTION = "repeated_action"
    BUDGET_EXHAUSTED = "budget_exhausted"
```

这些原因不代表今天已经实现了对应机制，只是建立状态语义。

例如：

```text
RunStatus = SUCCEEDED
StopReason = COMPLETED
RunStatus = FAILED
StopReason = DEADLINE_EXCEEDED
RunStatus = CANCELLED
StopReason = USER_CANCELLED
```

------

# 6. 为什么 Timeout 不一定要成为 RunStatus

有两种常见设计：

## 方案一：状态非常细

```text
SUCCEEDED
FAILED
CANCELLED
TIMED_OUT
BUDGET_EXHAUSTED
```

优点是查询直观，缺点是状态数量容易膨胀。

## 方案二：状态表示大类，原因表示细节

```text
RunStatus = FAILED
StopReason = DEADLINE_EXCEEDED
RunStatus = FAILED
StopReason = BUDGET_EXHAUSTED
```

LocalAgent 当前更适合方案二，因为：

- 系统规模尚小；
- 状态机还未建立；
- 可以避免过早增加状态；
- 后续统计仍可以按 StopReason 聚合。

------

# 7. StepStatus：步骤状态

推荐定义：

```python
class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
```

## 各状态含义

### PENDING

步骤已经存在，但还没有开始。

### RUNNING

步骤当前正在执行。

### SUCCEEDED

步骤已经成功完成。

### FAILED

步骤执行失败。

### CANCELLED

由于 Run 取消或父任务取消，步骤停止。

### BLOCKED

由于依赖未满足或前置步骤失败，暂时不能执行。

### SKIPPED

Runtime 明确决定不再执行该步骤。

------

## 7.1 为什么不建议把 READY 做成持久化状态

第 8 天 Scheduler（调度器）会判断 Ready Step（就绪步骤）。

`READY` 通常可以通过以下条件计算：

```text
status == PENDING
并且
所有依赖步骤已经成功
```

因此它更像一个派生状态，而不一定需要永久保存。

如果同时保存：

```text
status = READY
```

和依赖状态，就可能出现：

```text
某个依赖已经失败
但步骤仍然保存为 READY
```

第一版建议把 Ready 作为调度器计算结果。

------

# 8. StepState 应包含什么

最小 StepState（步骤状态）可以包含：

```python
@dataclass(slots=True)
class StepState:
    step_id: str
    name: str
    status: StepStatus

    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None

    error_code: str | None = None
    error_message: str | None = None
```

今天不建议加入：

- Tool 具体参数；
- 完整 Prompt；
- RAG 全量文档；
- 模型响应正文；
- Plan 依赖关系；
- Retry 次数；
- Token 消耗；
- 成本；
- Checkpoint；
- 审批信息。

它们将在对应学习日进入更合适的数据结构。

------

# 9. AgentState 推荐模型

```python
@dataclass(slots=True)
class AgentState:
    schema_version: int
    run_id: str
    status: RunStatus

    created_at: datetime
    updated_at: datetime

    steps: dict[str, StepState]
    active_step_ids: set[str]

    stop_reason: StopReason | None = None
    final_output: str | None = None

    error_code: str | None = None
    error_message: str | None = None
```

## 为什么是 `active_step_ids`，而不是 `current_step_id`

当前 AgentRouter 主要串行执行，使用：

```text
current_step_id
```

看起来更简单。

但第 10 天会进入 Parallel Execution（并行执行），届时可能同时存在：

```text
step-A = RUNNING
step-B = RUNNING
step-C = RUNNING
```

如果现在设计成单个 `current_step_id`，之后必须重构状态模型。

因此建议从第一版就使用：

```python
active_step_ids: set[str]
```

序列化时转换成稳定排序的列表：

```python
"active_step_ids": sorted(self.active_step_ids)
```

------

# 10. 状态所有权

生产级设计中应遵循：

> Runtime 是 AgentState 的唯一权威写入者。

Agent、Model、Tool 和 RAG 可以返回执行结果，但不应该随意写全局状态。

错误方式：

```python
tool.execute()
state.status = RunStatus.SUCCEEDED
```

Tool 不知道整个 Run 是否完成。

正确方向：

```text
Tool
→ 返回 ToolResult
→ Runtime 判断结果
→ Runtime 更新 StepState
→ Runtime 判断是否更新 RunStatus
```

当前 LocalAgent 还没有独立 Runtime，因此第 3 天可以暂时让：

```text
ChatService
```

承担最外层 Run 状态生命周期更新。

但这只是过渡方案，后续会迁移到 Runtime。

------

# 11. 状态不变量

状态不变量是：

> 无论执行流程走到哪里，都必须始终成立的规则。

没有状态不变量，系统可能出现逻辑上不可能的状态。

------

## 11.1 Run 终态不变量

终态包括：

```text
SUCCEEDED
FAILED
CANCELLED
```

必须满足：

```text
终态 Run 必须有 stop_reason
终态 Run 不能存在 active step
```

错误示例：

```text
status = SUCCEEDED
stop_reason = None
status = FAILED
active_step_ids = {"tool-1"}
```

------

## 11.2 非终态不变量

```text
CREATED
RUNNING
```

必须满足：

```text
stop_reason = None
```

错误示例：

```text
status = RUNNING
stop_reason = COMPLETED
```

------

## 11.3 Status 与 StopReason 对应关系

推荐约束：

### SUCCEEDED

只能搭配：

```text
COMPLETED
```

### CANCELLED

只能搭配：

```text
USER_CANCELLED
CLIENT_DISCONNECTED
SYSTEM_SHUTDOWN
```

### FAILED

可以搭配：

```text
UNHANDLED_ERROR
DEADLINE_EXCEEDED
MAX_STEPS_REACHED
NO_ACTION
REPEATED_ACTION
BUDGET_EXHAUSTED
```

不要出现：

```text
status = SUCCEEDED
stop_reason = UNHANDLED_ERROR
```

------

## 11.4 Step 时间不变量

### PENDING

```text
started_at = None
ended_at = None
```

### RUNNING

```text
started_at != None
ended_at = None
```

### SUCCEEDED / FAILED / CANCELLED / SKIPPED

```text
ended_at != None
```

并且：

```text
created_at <= started_at <= ended_at
```

------

## 11.5 Step 错误不变量

### SUCCEEDED

```text
error_code = None
error_message = None
```

### FAILED

至少应该有一个：

```text
error_code
error_message
```

不要要求保存完整异常堆栈，因为状态可能持久化，也可能展示给用户。

完整 Traceback（异常堆栈）应该进入日志或 Trace 系统，不应直接进入 AgentState。

------

## 11.6 Active Step 不变量

必须满足：

```text
active_step_ids 中的每个 step_id 都必须存在
```

并且：

```text
step_id 在 active_step_ids
等价于
step.status == RUNNING
```

这里不能限制：

```text
active_step_ids 数量最多为 1
```

否则会阻塞第 10 天的并行执行设计。

------

## 11.7 Run ID 不变量

必须满足：

```text
agent_state.run_id == run_context.run_id
```

任何不一致都应该立即失败，而不是继续执行。

------

# 12. 状态修改应该如何实现

今天不实现完整状态机，但也不应该允许所有调用方直接随意修改字段。

可以提供少量受约束的方法：

```python
state.mark_running(now)
state.add_step(step)
state.start_step(step_id, now)
state.succeed_step(step_id, now)
state.fail_step(step_id, now, error_code, error_message)
state.mark_succeeded(now, final_output)
state.mark_failed(now, reason, error_code, error_message)
state.mark_cancelled(now, reason)
state.validate()
```

这些方法的作用是：

- 更新必要字段；
- 维护时间；
- 维护 `active_step_ids`；
- 调用 `validate()`；
- 防止明显矛盾。

但今天不能创建：

- Event 类型；
- 状态转移矩阵；
- Guard（转换守卫）体系；
- 通用 `transition(event)` 方法。

这些属于第 5 天 State Machine。

------

# 13. 状态序列化

## 13.1 推荐使用 JSON 友好格式

枚举保存字符串：

```json
{
  "status": "running"
}
```

时间保存 UTC ISO 8601：

```json
{
  "created_at": "2026-07-17T01:30:00+00:00"
}
```

Step 使用稳定对象或列表：

```json
{
  "steps": [
    {
      "step_id": "legacy-agent-router",
      "name": "Legacy AgentRouter execution",
      "status": "running"
    }
  ]
}
```

## 13.2 不要使用 pickle

原因包括：

- 安全风险；
- Python 类型和代码版本耦合；
- 难以跨语言；
- 难以审查；
- 难以做 Schema Migration（结构迁移）；
- 不适合作为长期持久化格式。

------

# 14. 状态版本

状态版本至少需要区分两个概念。

## 14.1 `schema_version`

表示数据结构版本：

```python
schema_version = 1
```

例如未来增加：

- 计划版本；
- Step 依赖；
- 审批信息；
- Budget 快照；

可能升级为：

```python
schema_version = 2
```

## 14.2 `revision`

表示同一份状态被修改了多少次：

```text
revision = 15
```

它以后可以用于：

- 乐观并发控制；
- Checkpoint 覆盖保护；
- 判断状态是否陈旧。

但 `revision` 与第 16 天 Checkpoint 和并发控制关系更强，今天可以理解，不要求正式实现。

------

# 15. 版本反序列化策略

第一版应做到：

```python
if schema_version != SUPPORTED_SCHEMA_VERSION:
    raise UnsupportedStateVersionError(...)
```

不要静默忽略未知版本。

错误方式：

```python
schema_version = data.get("schema_version", 1)
```

如果一个未来版本被旧代码加载，很可能丢失关键字段后继续执行。

更安全的方式是：

```text
版本缺失
→ 明确拒绝，或仅对明确的历史格式兼容

版本过高
→ 拒绝加载

旧版本
→ 未来通过迁移函数转换
```

今天不需要实现完整迁移系统，只需要：

- 保存版本；
- 校验版本；
- 对不支持的版本明确报错。

------

# 16. LocalAgent 最小落地方案

## 16.1 新增文件

建议：

```text
core/runtime/state.py
```

可以包含：

- `RunStatus`
- `StepStatus`
- `StopReason`
- `StepState`
- `AgentState`
- `UnsupportedStateVersionError`
- 状态校验异常

不要因为类较多立刻拆成七八个文件。

------

## 16.2 在哪里创建 AgentState

当前建议继续在：

```text
ChatService.stream_chat()
```

中与 RunContext 一起创建：

```text
create_run_context()
create_agent_state(run_id=context.run_id)
```

然后校验：

```text
state.run_id == context.run_id
```

------

## 16.3 先创建一个兼容 Step

今天不要尝试把 AgentRouter 内部每次 Model、Tool 和 RAG 调用都拆成正式 Step。

先创建一个兼容步骤：

```text
step_id = legacy-agent-router
name = Legacy AgentRouter execution
```

完整流程：

```text
创建 RunContext
→ 创建 AgentState(CREATED)
→ 添加 legacy-agent-router Step(PENDING)
→ Run 进入 RUNNING
→ Step 进入 RUNNING
→ 调用现有 AgentRouter
```

正常完成：

```text
Step → SUCCEEDED
Run → SUCCEEDED
StopReason → COMPLETED
```

Deadline：

```text
Step → FAILED
Run → FAILED
StopReason → DEADLINE_EXCEEDED
```

取消：

```text
Step → CANCELLED
Run → CANCELLED
StopReason → 对应取消原因
```

其他异常：

```text
Step → FAILED
Run → FAILED
StopReason → UNHANDLED_ERROR
```

这能让真实执行链开始拥有结构化状态，但不会提前拆解 AgentRouter。

------

## 16.4 为什么只使用一个兼容 Step

因为当前 `AgentRouter` 内部还没有清晰的：

- Step 定义；
- Step ID；
- Step 边界；
- Planner 计划；
- Scheduler；
- 状态机。

如果今天强行把：

- Router；
- Tool；
- RAG；
- Model；
- 多 Agent 委派

全部变成正式 Step，会提前侵入第 4、7、8 天内容。

单个兼容 Step 可以先验证：

- Run 状态生命周期；
- Step 状态生命周期；
- 状态不变量；
- 错误终止原因；
- 序列化。

------

# 17. 错误信息如何进入状态

不要将：

```python
traceback.format_exc()
```

直接保存到 AgentState。

建议只保存：

```text
error_code = "UNHANDLED_ERROR"
error_message = "agent execution failed"
```

或者保存经过裁剪、脱敏的信息。

真实异常对象和堆栈应进入日志。

尤其不能把以下信息放进持久化状态：

- API Key；
- 内部地址；
- 用户真实文件路径；
- 完整 Tool 参数；
- 数据库连接信息；
- 大段用户输入；
- 内部模型请求体。

------

# 18. 本次架构方案

## 改造目标

为 LocalAgent 增加最小 AgentState 模型，并在当前真实聊天执行链中维护：

- RunStatus；
- 一个兼容 Step；
- StopReason；
- 错误摘要；
- 序列化和版本；
- 状态不变量。

## 预计影响范围

- 新增 `core/runtime/state.py`；
- 更新 `core/runtime/__init__.py`；
- 修改 `core/chat_service.py`；
- 可能对第 2 天 RunContext 工厂做极小调用调整；
- 新增状态单元测试；
- 新增 ChatService 生命周期行为测试；
- 创建第 3 天结果文档。

## 兼容要求

必须保持：

- `/api/chat` 请求体不变；
- 返回文本流不变；
- `[[ORCH]]` 格式不变；
- Memory Schema 不变；
- UI 行为不变；
- AgentRouter 业务行为不变；
- Model、Tool、RAG 接口不大规模修改。

## 风险

1. 把 AgentState 设计成万能容器。
2. 在 Agent、Tool 等下游直接修改全局状态。
3. 使用单个 `current_step_id` 阻塞未来并行执行。
4. 状态终止后仍然存在运行中 Step。
5. 将 StopReason 当作过程状态。
6. 状态对象保存完整异常和敏感信息。
7. 为实现状态生命周期提前创建状态机。
8. 状态只在成功路径更新，异常路径仍然不一致。

------

# 19. 测试方案

至少覆盖：

## Run 状态

- 新状态默认为 `CREATED`；
- `CREATED → RUNNING`；
- 成功结束；
- 失败结束；
- 取消结束；
- 终态必须包含 StopReason；
- 非终态不能包含 StopReason；
- 终态不能存在 active steps；
- 成功只能使用 `COMPLETED`；
- 取消只能使用取消类 StopReason。

## Step 状态

- 新 Step 默认为 `PENDING`；
- 开始后为 `RUNNING`；
- 成功后移出 active steps；
- 失败后保存错误摘要；
- 取消后结束；
- 重复 Step ID 被拒绝；
- active step 必须存在；
- 允许多个同时运行的 Step；
- Step 时间顺序正确；
- 成功 Step 不包含错误信息。

## 序列化

- `to_dict()` 结果 JSON 友好；
- 枚举保存为字符串；
- datetime 保存为 UTC ISO 8601；
- `to_dict → from_dict` 往返一致；
- 未知 `schema_version` 被拒绝；
- 非法枚举值被拒绝；
- 序列化结果不包含异常对象、锁、Token 等运行依赖。

## 集成

使用 Fake Router 验证：

- ChatService 创建 AgentState；
- 执行开始后 Run 和兼容 Step 进入 RUNNING；
- 正常完成后进入 SUCCEEDED；
- Fake Router 抛异常后进入 FAILED；
- Deadline 异常映射为 `DEADLINE_EXCEEDED`；
- 现有输出文本不改变；
- 异常继续向上抛出，由现有 API 错误边界处理。

------

# 20. Codex 实操提示词

你正在协助改造一个名为 LocalAgent 的本地 AI Agent 项目。

## 一、项目背景

项目包括：

- PyQt6 前端；
- FastAPI 后端；
- 本地与远程大模型；
- Router、Planner 和多 Agent 编排；
- Tool；
- RAG；
- SQLite Memory；
- Chroma；
- 自定义流式 HTTP 输出；
- `[[ORCH]]` 编排状态标记。

阶段二第 1 天已经确认：

- `server.py::chat_endpoint()` 是 FastAPI 聊天入口；
- `core/chat_service.py::ChatService.stream_chat()` 是当前应用服务入口；
- `core/agent_router.py::AgentRouter.chat_stream()` 是遗留执行控制入口；
- AgentRouter 当前同时承担 Router、Planner、Model、Tool、RAG、Memory、多 Agent 编排和事件输出等多种职责；
- Runtime 最小接入边界位于 ChatService 与 AgentRouter 之间。

阶段二第 2 天已经完成：

- `RunContext`；
- `run_id`、`session_id`、`trace_id`；
- UTC 与 monotonic Deadline；
- `CancellationSource` 与 `CancellationToken`；
- `create_run_context() -> tuple[RunContext, CancellationSource]`；
- ChatService 创建 Context 并显式传入 AgentRouter；
- `session_id` 暂时使用 `legacy-default`；
- 当前没有 cancel API，也没有客户端断开传播；
- 当前 RunContext 尚未持久化。

本次任务是：

“阶段二第 3 天：AgentState、RunStatus、StepStatus、StopReason、状态序列化、状态版本和状态不变量。”

本次允许进行最小代码改造，但禁止实现完整状态机、Agent Loop、Planner、Scheduler、Checkpoint 或持久化状态仓库。

## 二、固定工作流

严格遵循：

第一步：阅读项目结构和相关代码
第二步：总结现状和问题
第三步：给出最小改造方案
第四步：实施修改
第五步：补充或更新测试
第六步：运行相关测试和检查
第七步：输出结果信息文档

不得跳过分析直接修改。

## 三、本次目标

建立一个最小、明确、可序列化、可验证的 AgentState 模型，使真实聊天 Run 能够表达：

- Run 当前生命周期；
- Step 当前状态；
- 当前正在运行的步骤集合；
- 最终停止原因；
- 最终输出或安全错误摘要；
- 状态结构版本；
- 关键状态不变量。

并在不拆分 AgentRouter 的前提下，将当前整个遗留 AgentRouter 执行暂时表示成一个兼容 Step。

## 四、修改前检查范围

请先检查真实代码，至少包括：

- `core/runtime/context.py`
- `core/runtime/cancellation.py`
- `core/runtime/__init__.py`
- `core/chat_service.py`
- `core/agent_router.py`
- `server.py`
- `tests/test_runtime_context.py`
- 第 2 天新增或修改的全部代码
- 现有日志方式
- 所有 `ChatService.stream_chat()` 调用方
- 所有 `AgentRouter.chat_stream()` 调用方

不要根据本提示词假设现有实现细节，必须根据真实代码调整。

## 五、核心数据模型

建议在：

```text
core/runtime/state.py
```

中实现最小状态模型。

可以根据项目风格调整命名，但必须覆盖以下概念。

### 1. RunStatus

第一个版本保持精简：

```text
CREATED
RUNNING
SUCCEEDED
FAILED
CANCELLED
```

不要增加：

- WAITING_APPROVAL；
- RETRYING；
- PLANNING；
- TOOL_RUNNING；
- RAG_RUNNING；
- CHECKPOINTING；
- RESUMING。

这些属于后续阶段或其他数据维度。

### 2. StepStatus

至少包括：

```text
PENDING
RUNNING
SUCCEEDED
FAILED
CANCELLED
BLOCKED
SKIPPED
```

不要把 Ready Step 固化为必须持久化的状态；Ready 可以在后续 Scheduler 中根据依赖计算。

### 3. StopReason

至少包括：

```text
COMPLETED
UNHANDLED_ERROR
DEADLINE_EXCEEDED
USER_CANCELLED
CLIENT_DISCONNECTED
SYSTEM_SHUTDOWN
MAX_STEPS_REACHED
NO_ACTION
REPEATED_ACTION
BUDGET_EXHAUSTED
```

定义这些枚举不表示本次已经实现相应机制。

### 4. StepState

至少包含：

- `step_id`
- `name`
- `status`
- `created_at`
- `started_at`
- `ended_at`
- `error_code`
- `error_message`

不要在本次加入：

- Plan 依赖关系；
- Tool 参数；
- 完整 Prompt；
- RAG 文档；
- Token 消耗；
- Retry 次数；
- Cost；
- Approval；
- Checkpoint。

### 5. AgentState

至少包含：

- `schema_version`
- `run_id`
- `status`
- `created_at`
- `updated_at`
- `steps`
- `active_step_ids`
- `stop_reason`
- `final_output`
- `error_code`
- `error_message`

状态对象允许随执行过程变化，但必须由受约束方法更新，不能依赖调用方任意修改内部结构。

## 六、设计原则

### 1. AgentState 与 RunContext 的边界

RunContext 保存：

- 运行标识；
- Deadline；
- Cancellation Token；
- 进程内依赖。

AgentState 保存：

- 当前运行状态；
- Step 状态；
- StopReason；
- 最终结果；
- 错误摘要。

禁止把 Clock、Token、Event、Lock、Model、DB 连接、Chroma Client、Generator 或回调写入 AgentState。

必须校验：

```text
agent_state.run_id == run_context.run_id
```

### 2. active_step_ids

使用能够表达多个并行步骤的结构：

```text
active_step_ids
```

不要只设计单个 `current_step_id`。

序列化时使用稳定排序的列表，避免 Set 无法直接 JSON 序列化。

### 3. 状态所有权

当前阶段可以由 ChatService 暂时创建和更新最外层 AgentState。

AgentRouter、Tool、Model、RAG 和 Memory 不应获得任意修改全局 AgentState 的权限。

本次不要引入完整 Runtime。

### 4. 状态修改接口

可以实现少量明确的方法，例如：

- `mark_running`
- `add_step`
- `start_step`
- `succeed_step`
- `fail_step`
- `cancel_step`
- `mark_succeeded`
- `mark_failed`
- `mark_cancelled`
- `validate`

方法名称可按项目风格调整。

本次不要实现：

- 通用 Event 基类；
- `transition(event)`；
- 状态转移表；
- Guard 系统；
- 完整状态机。

这些属于第 5 天。

## 七、状态不变量

必须建立并测试以下规则。

### Run 不变量

1. `run_id` 非空。
2. `CREATED` 和 `RUNNING` 时 `stop_reason` 必须为 `None`。
3. `SUCCEEDED`、`FAILED`、`CANCELLED` 时必须有 `stop_reason`。
4. 终态 Run 不得存在 active steps。
5. `SUCCEEDED` 只能搭配 `COMPLETED`。
6. `CANCELLED` 只能搭配取消类原因：
   - `USER_CANCELLED`
   - `CLIENT_DISCONNECTED`
   - `SYSTEM_SHUTDOWN`
7. `FAILED` 不得搭配 `COMPLETED` 或取消类原因。
8. `created_at`、`updated_at` 必须是 timezone-aware UTC datetime。
9. `updated_at` 不早于 `created_at`。

### Step 不变量

1. Step ID 非空且不能重复。
2. `PENDING`：
   - `started_at is None`
   - `ended_at is None`
3. `RUNNING`：
   - `started_at is not None`
   - `ended_at is None`
4. `SUCCEEDED`、`FAILED`、`CANCELLED`、`SKIPPED`：
   - `ended_at is not None`
5. `created_at <= started_at <= ended_at`，在字段存在时成立。
6. `SUCCEEDED` 不得包含错误信息。
7. `FAILED` 至少包含安全的 `error_code` 或 `error_message`。
8. active step ID 必须存在于 steps。
9. active step ID 对应的 StepStatus 必须是 `RUNNING`。
10. 所有 `RUNNING` Step 必须出现在 active_step_ids。
11. 允许多个 Step 同时为 `RUNNING`，不要设置最多一个 active step 的限制。

### 错误信息

不要把完整 traceback、异常对象或敏感信息保存到 AgentState。

只保存安全的错误码和简短错误摘要。

## 八、状态序列化与版本

### 1. Schema Version

第一个版本：

```text
schema_version = 1
```

将版本写入序列化数据。

### 2. JSON 友好序列化

必须做到：

- Enum 序列化为字符串值；
- datetime 序列化为 UTC ISO 8601；
- active_step_ids 序列化为稳定排序列表；
- steps 使用稳定顺序；
- 不包含 Python 异常对象、锁、Token、Clock 或连接对象；
- `json.dumps(state.to_dict())` 可以成功执行。

### 3. 反序列化

实现明确的 `from_dict()` 或等价入口。

必须：

- 校验 `schema_version`；
- 未知或过高版本明确抛出 `UnsupportedStateVersionError`；
- 非法 Enum 值明确失败；
- 反序列化后重新执行状态不变量校验；
- 不使用 pickle。

本次不实现完整 Schema Migration。

## 九、真实调用链最小集成

在 `ChatService.stream_chat()` 中：

1. 创建 RunContext 和 CancellationSource；
2. 创建与 Context 使用相同 `run_id` 的 AgentState；
3. 添加一个兼容 Step，例如：

```text
step_id = legacy-agent-router
name = Legacy AgentRouter execution
```

1. Run 从 `CREATED` 进入 `RUNNING`；
2. 兼容 Step 从 `PENDING` 进入 `RUNNING`；
3. 调用现有 `AgentRouter.chat_stream()`；
4. 正常完成时：
   - Step → `SUCCEEDED`
   - Run → `SUCCEEDED`
   - StopReason → `COMPLETED`
5. `RunDeadlineExceededError` 时：
   - Step → `FAILED`
   - Run → `FAILED`
   - StopReason → `DEADLINE_EXCEEDED`
6. `RunCancelledError` 时：
   - Step → `CANCELLED`
   - Run → `CANCELLED`
   - StopReason 使用当前可明确判断的取消原因；如果现阶段无法区分，使用文档中明确说明的兼容原因，不要伪造客户端断开。
7. 其他异常时：

- Step → `FAILED`
- Run → `FAILED`
- StopReason → `UNHANDLED_ERROR`
- 保存安全错误摘要；
- 继续向上抛出原异常，由现有 API 错误边界处理。

不得吞掉原异常。

现有输出文本和 `[[ORCH]]` 格式必须完全兼容。

## 十、状态当前不持久化

本次不要：

- 修改 SQLite Schema；
- 新增状态表；
- 写 Checkpoint；
- 新增状态仓库；
- 把 AgentState 写入 Conversation Memory；
- 把状态长期保存在 ChatService 的 `_last_state` 等共享字段中。

当前 AgentState 可以只存在于一次 Generator 生命周期中。

可以记录最终状态的结构化日志，但不要重写日志系统，也不要输出敏感信息。

## 十一、测试要求

优先使用现有 `unittest`。

至少新增：

```text
tests/test_agent_state.py
```

并根据需要扩展 ChatService 集成测试。

测试必须覆盖：

### AgentState 单元测试

1. 初始状态为 `CREATED`；
2. `schema_version == 1`；
3. `CREATED → RUNNING`；
4. 添加和启动 Step；
5. Step 成功；
6. Step 失败；
7. Step 取消；
8. Run 成功；
9. Run 失败；
10. Run 取消；
11. 终态必须有 StopReason；
12. 非终态不能有 StopReason；
13. 成功只能搭配 `COMPLETED`；
14. 取消只能搭配取消类原因；
15. 终态不能保留 active steps；
16. 重复 Step ID 被拒绝；
17. active step 必须存在；
18. active step 必须是 RUNNING；
19. 所有 RUNNING Step 都在 active_step_ids；
20. 允许多个 Step 同时 RUNNING；
21. Step 时间顺序校验；
22. 成功 Step 无错误；
23. 失败 Step 有错误摘要；
24. 空 run_id、step_id、name 被拒绝；
25. UTC datetime 校验。

### 序列化测试

1. `json.dumps(to_dict())` 成功；
2. Enum 保存为字符串；
3. datetime 保存为 ISO 8601；
4. active step ID 顺序稳定；
5. Step 顺序稳定；
6. `to_dict → from_dict` 往返一致；
7. 未知 Schema Version 被拒绝；
8. 非法 Enum 值被拒绝；
9. 反序列化后的非法状态被拒绝；
10. 不包含 Clock、Token、Event、Lock、异常对象。

### ChatService 行为测试

使用 Fake Router，不启动真实模型、数据库、RAG、Tool 或 UI。

验证：

1. Context 和 AgentState 使用相同 run_id；
2. 正常完成后兼容 Step 和 Run 均成功；
3. Fake Router 抛普通异常后，Step 和 Run 均失败；
4. Deadline 异常映射正确；
5. Cancellation 异常映射正确；
6. 原异常继续向上抛出；
7. 正常文本输出保持不变；
8. 不在 ChatService 上保存共享 `_last_state`。

如测试难以观察临时 AgentState，可以使用明确、仅测试范围的 Factory 注入、Callback 或 Mock；不要为了测试新增生产级状态仓库。

## 十二、代码质量要求

新增公共代码必须：

- 完整类型标注；
- 清晰 Docstring；
- 避免 `Any`；
- 避免可变默认参数；
- 避免循环导入；
- 使用 timezone-aware UTC datetime；
- 不新增大型依赖；
- 不使用全局可变状态；
- 不使用 pickle；
- 不使用 `print` 作为正式日志；
- 不保存敏感信息。

## 十三、执行检查

至少执行：

```text
python -m unittest tests.test_runtime_context tests.test_agent_state -v
python -m compileall core tests
```

如 ChatService 测试位于其他文件，也执行对应测试。

不要：

- 启动真实模型；
- 启动 Chroma；
- 启动 PyQt6 UI；
- 访问外部网络；
- 修改真实数据库数据。

## 十四、禁止事项

禁止：

- 大规模拆分 AgentRouter；
- 实现完整 Agent Runtime；
- 实现 Agent Loop；
- 实现状态机；
- 实现状态转移表或事件系统；
- 实现 Planner、PlanStep 或 DAG；
- 实现 Scheduler；
- 实现 Budget；
- 实现 Retry、Fallback 或 Circuit Breaker；
- 实现 Checkpoint 或 Resume；
- 新增状态数据库；
- 修改 Memory Schema；
- 实现 Human Approval；
- 实现 Trace Span 或 Replay；
- 修改 `/api/chat` 请求体；
- 修改现有文本流；
- 修改 `[[ORCH]]` 协议；
- 引入 Tool Registry、Agent Skill、MCP、A2A 或 Sandbox；
- 把 AgentEvalOps 加入 LocalAgent；
- 上传代码或公司信息到外部服务；
- 执行 Git push；
- 创建 Pull Request；
- 擅自修改无关模块。

## 十五、结果文档

必须创建：

```text
docs/learning/stage2/day03_agent_state_result.md
```

结构必须包括：

# 阶段二第 3 天改造结果

## 1. 本次任务目标

## 2. 修改前现状

## 3. 发现的问题

## 4. 最终设计方案

## 5. 新增文件

## 6. 修改文件

## 7. 核心类、接口和数据结构

必须说明：

- AgentState
- StepState
- RunStatus
- StepStatus
- StopReason
- schema_version
- 状态校验异常
- UnsupportedStateVersionError

## 8. 关键执行流程

至少包含：

- AgentState 创建；
- Run 启动；
- 兼容 Step 启动；
- 正常成功；
- Deadline 失败；
- Cancellation；
- 普通异常；
- 序列化与反序列化。

## 9. 与现有功能的兼容方式

## 10. 异常处理和边界情况

## 11. 测试内容

## 12. 实际执行的测试命令

## 13. 测试结果

## 14. 未完成事项

## 15. 已知风险

必须明确记录：

- AgentState 当前只存在于 Generator 生命周期；
- 尚未持久化；
- 尚无状态仓库；
- 当前只有一个兼容 legacy step；
- 尚未实现完整 Agent Loop；
- 尚未实现状态机；
- 尚未实现并发状态写入控制；
- 取消来源仍未接入真实 UI 或连接断开；
- `session_id` 仍为 `legacy-default`。

## 16. 设计权衡

## 17. 可用于面试的项目描述

不得声称已经实现：

- 完整状态机；
- 持久化执行；
- Checkpoint；
- 多步骤 Scheduler；
- 完整 Runtime。

## 18. 需要带回 ChatGPT 审查的信息

必须包含：

- AgentState 的真实创建位置；
- AgentState 的所有者；
- 状态修改发生在哪些位置；
- RunStatus、StepStatus、StopReason 的最终枚举；
- 状态不变量实现位置；
- `active_step_ids` 数据结构；
- 是否允许多个 RUNNING Step；
- schema_version；
- 序列化格式；
- from_dict 如何处理未知版本；
- 兼容 Step 的 ID 和语义；
- 成功、失败、Deadline 和 Cancellation 的映射；
- 错误信息如何脱敏；
- 修改前后真实调用链；
- 新增和修改文件；
- 测试命令和测试结果；
- 测试失败或无法执行的原因；
- 尚不确定的问题；
- 后续建议，但不得直接实施第 4 天内容。

## 十六、聊天最终输出

完成后请输出：

结果文档路径：

本次新增文件：

本次修改文件：

AgentState 创建位置：

AgentState 所有者：

兼容 Step：

RunStatus：

StepStatus：

StopReason：

状态不变量实现位置：

状态序列化方式：

schema_version：

测试命令：

测试是否通过：

是否修改 API、Memory Schema 或流式协议：

需要人工确认的问题：

------

# 21. Codex 结果审查重点

带回结果后重点检查：

1. AgentState 是否与 RunContext 正确区分。
2. `run_id` 是否完全一致。
3. 是否错误使用单个 `current_step_id`。
4. 是否允许多个 RUNNING Step。
5. 终态是否清空 active steps。
6. StopReason 是否只在终态出现。
7. Status 与 StopReason 是否存在非法组合。
8. 异常路径是否更新状态后继续抛出。
9. 是否吞掉 Deadline 或 Cancellation 异常。
10. 是否保存了完整 Traceback 或敏感信息。
11. 序列化能否直接 `json.dumps()`。
12. `from_dict()` 是否校验版本和不变量。
13. 是否提前实现状态机。
14. 是否把 AgentState 写进 Memory。
15. 是否增加了共享 `_last_state` 导致并发污染。
16. 测试是否覆盖真实成功、失败和取消路径。

------

# 22. 面试高频问题

## 1. RunContext 和 AgentState 有什么区别？

> RunContext 保存一次运行中相对稳定的执行环境，例如运行标识、Deadline 和取消令牌；AgentState 保存执行过程中持续变化的事实，例如 RunStatus、StepStatus、StopReason 和最终结果。

## 2. 为什么 RunStatus 和 StopReason 要分开？

> RunStatus 表示生命周期大类，例如成功、失败或取消；StopReason 表示为什么进入终态，例如 Deadline 超时、用户取消或最大步骤耗尽。分开后可以避免状态数量爆炸。

## 3. 为什么不能只保存一个 current_step_id？

> 因为未来存在并行步骤。使用 `active_step_ids` 可以同时表示多个正在执行的 Step，而单个 current step 会限制调度模型。

## 4. 什么是状态不变量？

> 状态不变量是任何执行路径下都必须成立的规则。例如终态 Run 必须有 StopReason，终态 Run 不能仍有运行中的 Step，成功 Step 不能包含错误信息。

## 5. 为什么状态必须带 schema_version？

> AgentState 后续会增加计划、依赖、审批、预算和 Checkpoint 字段。没有版本，旧代码可能错误读取新状态并丢失关键语义。显式版本可以让系统拒绝或迁移不兼容数据。

------

# 23. 当天知识总结

今天最核心的关系是：

```text
RunContext
    描述运行环境

AgentState
    描述当前执行事实

RunStatus
    描述 Run 生命周期大类

StepStatus
    描述局部步骤状态

StopReason
    描述最终为什么停止
```

生产级状态设计的关键不在于枚举数量，而在于：

```text
语义不重叠
状态可序列化
版本可识别
变更有唯一所有者
非法组合无法存在
未来并行不被模型限制
```

------

# 24. 当天验收清单

## 理论验收

-  区分 RunContext 与 AgentState
-  区分 RunStatus、StepStatus 和 StopReason
-  理解状态不变量
-  理解 `active_step_ids` 对未来并行的意义
-  理解状态序列化边界
-  理解 `schema_version`
-  理解状态日志与真实状态的区别
-  理解 Runtime 应成为状态唯一写入者

## 项目验收

-  已新增 AgentState 模型
-  已新增 StepState
-  已定义三个枚举
-  已实现状态不变量
-  已实现 JSON 友好序列化
-  已实现版本校验
-  已创建兼容 legacy step
-  成功路径更新正确
-  异常路径更新正确
-  Cancellation 路径更新正确
-  没有修改 API 和 Memory Schema
-  单元测试通过
-  已生成第 3 天结果文档
-  已完成 ChatGPT 审查

## 阶段二进度

**第 3/25 天：理论和架构方案完成，等待 Codex 改造结果审查。**

下一天主题：**Agent Loop（智能体循环），包括初始化、决策、执行、观察、更新、终止、最大步骤、无动作检测和重复动作检测。**