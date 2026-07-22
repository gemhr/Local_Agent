# 阶段二第 5 天：State Machine（状态机）

**当前进度：第 5/25 天。**

前几天已经完成：

```text
第 1 天：Runtime 边界
第 2 天：RunContext
第 3 天：AgentState
第 4 天：Agent Loop
```

当前执行链已经具备：

```text
RunContext
    提供运行环境
        ↓
AgentLoop
    决策、执行、观察、终止
        ↓
AgentState
    保存 Run 和 Step 的当前状态
```

但目前 Agent Loop 仍然直接调用：

```python
state.mark_running(...)
state.start_step(...)
state.succeed_step(...)
state.mark_failed(...)
```

这意味着：

> Agent Loop 既负责执行流程，又在直接决定状态如何变化。

第 5 天的目标是将状态修改收口为：

```text
Agent Loop
→ 发出 State Event（状态事件）
→ State Machine 校验
→ 修改 AgentState
```

------

# 一、当天目标

今天必须掌握：

1. State（状态）、Event（事件）、Transition（转移）、Guard（转换守卫）的区别。
2. State Machine 与 Agent Loop 的职责边界。
3. Run 级状态转移。
4. Step 级状态转移。
5. 合法和非法状态转移。
6. Terminal State（终态）保护。
7. 事件携带什么数据，不能携带什么数据。
8. 状态不变量与状态机的关系。
9. 如何将 Agent Loop 的直接状态修改迁移到状态机。
10. 如何设计高价值状态转移 Bad Case。

## 今天不处理

暂不实现：

- Planner（规划器）；
- PlanStep；
- Scheduler（调度器）；
- DAG（有向无环图）；
- 并行执行；
- Budget（预算）；
- Retry（重试）；
- Checkpoint（检查点）；
- Resume（恢复执行）；
- 持久化 Event Store（事件存储）；
- Event Sourcing（事件溯源）；
- Runtime Event 总线；
- Trace（追踪）；
- 前端事件协议改造；
- 模型轻重路由。

今天的状态事件仅用于：

> 驱动 AgentState 的内存内状态转移。

它还不是第 21 天的可观测 Runtime Event。

------

# 二、为什么 AgentState 有校验方法还不够

第 3 天已经有：

```python
AgentState.validate()
StepState.validate()
```

它可以阻止最终产生矛盾状态，例如：

```text
Run = SUCCEEDED
stop_reason = UNHANDLED_ERROR
```

但仅有 `validate()` 仍然存在问题。

## 1. 只能判断结果是否合法，不能表达过程是否合法

例如：

```text
CREATED → SUCCEEDED
```

最终状态本身可能满足：

```text
status = SUCCEEDED
stop_reason = COMPLETED
active_step_ids = empty
```

但从业务流程看，它可能绕过了：

```text
CREATED → RUNNING → SUCCEEDED
```

`validate()` 只看当前快照，很难判断中间发生过什么。

状态机可以明确规定：

```text
CREATED 只能进入 RUNNING、FAILED 或 CANCELLED
```

------

## 2. 多个调用方可能用不同方式修改状态

如果 Agent Loop、ChatService、Scheduler、Retry 模块都直接调用状态修改方法，很容易出现：

```text
Agent Loop 认为 Step 已成功
Scheduler 认为 Step 仍在运行
Cancellation 代码又将 Step 标记为取消
```

状态机通过统一入口控制：

```text
apply(event)
```

使所有状态转移遵循相同规则。

------

## 3. 直接调用目标状态，缺少事件语义

例如：

```python
state.mark_failed(...)
```

只能说明：

> 状态现在失败了。

但无法明确说明：

- 是执行异常？
- Deadline 到期？
- 最大步骤耗尽？
- 无动作？
- 重复动作？
- 状态校验失败？

状态事件能表达：

```text
RUN_DEADLINE_EXCEEDED
RUN_MAX_STEPS_REACHED
RUN_FAILED
```

状态机再将它们映射到：

```text
RunStatus.FAILED
StopReason.DEADLINE_EXCEEDED
```

------

# 三、State、Event、Transition、Guard

## 1. State（状态）

表示当前事实：

```text
RunStatus.RUNNING
StepStatus.PENDING
```

状态回答：

> 现在是什么情况？

------

## 2. Event（事件）

表示发生了什么：

```text
RUN_STARTED
STEP_STARTED
STEP_SUCCEEDED
RUN_COMPLETED
RUN_CANCELLED
```

事件回答：

> 刚才发生了什么？

事件通常使用过去式或完成语义，而不是命令语气。

例如：

```text
STEP_STARTED
```

表示 Step 已经被允许开始，需要将状态从 `PENDING` 转成 `RUNNING`。

------

## 3. Transition（状态转移）

表示：

```text
当前状态 + 事件 → 新状态
```

例如：

```text
PENDING + STEP_STARTED → RUNNING
RUNNING + STEP_SUCCEEDED → SUCCEEDED
```

------

## 4. Guard（转换守卫）

Guard 是状态转移前必须成立的额外条件。

例如：

```text
RunStatus.RUNNING
+ RUN_COMPLETED
```

还必须满足：

```text
没有 active Step
所有需要完成的 Step 已结束
存在最终输出或明确的空输出策略
```

否则即使状态表允许：

```text
RUNNING → SUCCEEDED
```

也不能执行。

------

# 四、Command 与 Event 的区别

这是面试中很容易被追问的地方。

## Command（命令）

表示请求系统做某件事：

```text
StartStep
CancelRun
CompleteRun
```

命令可能被拒绝。

## Event（事件）

表示系统已经接受并确认发生的事实：

```text
StepStarted
RunCancelled
RunCompleted
```

事件不应该描述一个尚未成功的意图。

------

## 当前阶段如何处理

为了避免今天引入完整 Command Bus（命令总线），可以简化为：

```text
Agent Loop
→ 调用 State Machine 的 transition(event)
```

事件对象实际承担：

- 转移意图；
- 转移所需的最小数据。

严格来说它介于 Command 和 Domain Event（领域事件）之间。

结果文档应诚实说明：

> 当前是内存内状态转移事件，不是完整事件驱动架构，也不是 Event Sourcing。

不要宣称已经实现完整 CQRS（命令查询职责分离）或事件溯源。

------

# 五、Run 状态机设计

当前 RunStatus：

```text
CREATED
RUNNING
SUCCEEDED
FAILED
CANCELLED
```

其中终态：

```text
SUCCEEDED
FAILED
CANCELLED
```

------

## 1. 推荐 Run Events

第一版保持精简：

```python
class RunEventType(str, Enum):
    STARTED = "run_started"
    COMPLETED = "run_completed"

    FAILED = "run_failed"
    DEADLINE_EXCEEDED = "run_deadline_exceeded"
    MAX_STEPS_REACHED = "run_max_steps_reached"
    NO_ACTION = "run_no_action"
    REPEATED_ACTION = "run_repeated_action"
    BUDGET_EXHAUSTED = "run_budget_exhausted"

    CANCELLED = "run_cancelled"
```

这里定义 `BUDGET_EXHAUSTED` 是为了对应已有 StopReason，但今天不会实现 Budget。

也可以先不在 Agent Loop 中使用它。

------

## 2. Run 转移表

### CREATED

允许：

```text
CREATED + STARTED
→ RUNNING
```

可以考虑允许：

```text
CREATED + CANCELLED
→ CANCELLED
```

例如用户在真正执行前立即取消。

可以允许：

```text
CREATED + FAILED
→ FAILED
```

例如初始化、配置或 Context 校验失败。

不允许：

```text
CREATED + COMPLETED
→ SUCCEEDED
```

因为当前设计要求 Run 必须先进入运行状态。

------

### RUNNING

允许：

```text
RUNNING + COMPLETED
→ SUCCEEDED
RUNNING + FAILED
→ FAILED
RUNNING + DEADLINE_EXCEEDED
→ FAILED
RUNNING + MAX_STEPS_REACHED
→ FAILED
RUNNING + NO_ACTION
→ FAILED
RUNNING + REPEATED_ACTION
→ FAILED
RUNNING + BUDGET_EXHAUSTED
→ FAILED
RUNNING + CANCELLED
→ CANCELLED
```

不允许：

```text
RUNNING + STARTED
```

因为同一个 Run 不应该重复启动。

------

### 终态

终态默认不允许任何进一步转移：

```text
SUCCEEDED + 任意事件
→ 拒绝

FAILED + 任意事件
→ 拒绝

CANCELLED + 任意事件
→ 拒绝
```

这是 Terminal State Protection（终态保护）。

------

# 六、Run Event 与 StopReason 映射

状态机负责统一映射：

| Run Event           | 新状态      | StopReason                                 |
| ------------------- | ----------- | ------------------------------------------ |
| `STARTED`           | `RUNNING`   | `None`                                     |
| `COMPLETED`         | `SUCCEEDED` | `COMPLETED`                                |
| `FAILED`            | `FAILED`    | `UNHANDLED_ERROR` 或事件指定的合法失败原因 |
| `DEADLINE_EXCEEDED` | `FAILED`    | `DEADLINE_EXCEEDED`                        |
| `MAX_STEPS_REACHED` | `FAILED`    | `MAX_STEPS_REACHED`                        |
| `NO_ACTION`         | `FAILED`    | `NO_ACTION`                                |
| `REPEATED_ACTION`   | `FAILED`    | `REPEATED_ACTION`                          |
| `BUDGET_EXHAUSTED`  | `FAILED`    | `BUDGET_EXHAUSTED`                         |
| `CANCELLED`         | `CANCELLED` | 合法取消原因                               |

这样 Agent Loop 不需要重复写：

```python
state.mark_failed(
    reason=StopReason.MAX_STEPS_REACHED,
    ...
)
```

只需要发出：

```text
RUN_MAX_STEPS_REACHED
```

------

# 七、Step 状态机设计

当前 StepStatus：

```text
PENDING
RUNNING
SUCCEEDED
FAILED
CANCELLED
BLOCKED
SKIPPED
```

终态通常包括：

```text
SUCCEEDED
FAILED
CANCELLED
BLOCKED
SKIPPED
```

------

## 1. 推荐 Step Events

```python
class StepEventType(str, Enum):
    STARTED = "step_started"
    SUCCEEDED = "step_succeeded"
    FAILED = "step_failed"
    CANCELLED = "step_cancelled"
    BLOCKED = "step_blocked"
    SKIPPED = "step_skipped"
```

------

## 2. Step 转移表

### PENDING

允许：

```text
PENDING + STARTED
→ RUNNING
PENDING + CANCELLED
→ CANCELLED
PENDING + BLOCKED
→ BLOCKED
PENDING + SKIPPED
→ SKIPPED
```

通常不允许：

```text
PENDING + SUCCEEDED
```

因为 Step 没有开始就直接成功，会绕过执行生命周期。

通常不允许：

```text
PENDING + FAILED
```

这里可以有两种设计。

#### 方案 A：严格模式

初始化失败也应先：

```text
PENDING → RUNNING → FAILED
```

优点是生命周期一致。

#### 方案 B：允许启动前失败

例如执行前参数校验失败：

```text
PENDING → FAILED
```

优点是能准确表达“从未真正执行”。

当前 LocalAgent 第一版建议采用**严格模式**：

> 只要 Action 已被接受并创建 Step，就先将 Step 转为 RUNNING，再进入执行或失败。

这样状态模型更简单。

------

### RUNNING

允许：

```text
RUNNING + SUCCEEDED
→ SUCCEEDED
RUNNING + FAILED
→ FAILED
RUNNING + CANCELLED
→ CANCELLED
```

通常不允许：

```text
RUNNING + BLOCKED
```

因为 `BLOCKED` 语义是从未开始且确定不能执行。

通常不允许：

```text
RUNNING + SKIPPED
```

因为已经开始的 Step 不能再说“跳过未执行”。

------

### Step 终态

默认拒绝全部后续事件：

```text
SUCCEEDED + FAILED
→ 非法

FAILED + SUCCEEDED
→ 非法

CANCELLED + STARTED
→ 非法

BLOCKED + STARTED
→ 非法
```

Resume 阶段如果需要重新执行失败或运行中 Step，应创建：

- 新 Attempt；
- 新 Step 实例；
- 或由 Resume 策略显式重建状态。

不能在今天开放终态回退。

------

# 八、Guard 设计

状态转移表只能表达：

```text
哪些状态可以转成哪些状态
```

Guard 负责表达：

```text
这次具体转移是否满足条件
```

------

## 1. Run Completed Guard

在：

```text
RUNNING + COMPLETED
```

前检查：

- `active_step_ids` 为空；
- 不存在 `RUNNING` Step；
- 所有当前必要 Step 已经终止；
- 没有已有错误状态；
- `final_output` 满足当前策略。

当前 Legacy AgentRouter 正常完成时，流程应该是：

```text
STEP_SUCCEEDED
→ active step 清空
→ RUN_COMPLETED
```

顺序不能反过来。

------

## 2. Run Failure Guard

Run 失败前需要处理所有 active Step。

例如：

```text
Deadline 到期
```

应先：

```text
当前 Step → FAILED
```

然后：

```text
Run → FAILED
```

否则会违反：

```text
终态 Run 不允许存在 active Step
```

状态机可以拒绝：

```text
active_step_ids 非空
+ RUN_FAILED
```

这迫使调用方先结束 active Step。

------

## 3. Run Cancellation Guard

Run 取消前，所有 active Step 必须先：

```text
RUNNING → CANCELLED
```

然后 Run 才能：

```text
RUNNING → CANCELLED
```

当前只有一个 active Step，但规则应支持未来多个并行 Step。

------

## 4. Step Start Guard

```text
STEP_STARTED
```

前检查：

- Step ID 存在；
- 当前状态为 `PENDING`；
- Step 不在 active 集合；
- Run 当前为 `RUNNING`；
- RunContext 仍然有效。

这里要注意职责：

- 状态机可以检查 AgentState；
- Deadline 和 Cancellation 仍主要由 Agent Loop / Runtime 检查。

不要让 State Machine 依赖 Clock、CancellationToken 或 Model。

------

## 5. Step Success Guard

```text
STEP_SUCCEEDED
```

前检查：

- Step 当前为 `RUNNING`；
- Step 在 `active_step_ids`；
- 没有错误摘要；
- `ended_at` 合法。

------

# 九、事件数据结构

## 1. 不要使用万能字典

不建议：

```python
@dataclass
class StateEvent:
    event_type: str
    payload: dict[str, Any]
```

这会导致：

- 缺少类型约束；
- 各调用方随意塞字段；
- 运行时错误；
- 敏感数据进入事件；
- 难以测试；
- 后续版本演进困难。

------

## 2. 推荐最小类型化事件

可以使用独立事件类：

```python
@dataclass(frozen=True, slots=True)
class RunStarted:
    occurred_at: datetime
@dataclass(frozen=True, slots=True)
class RunCompleted:
    occurred_at: datetime
    final_output: str
@dataclass(frozen=True, slots=True)
class RunFailed:
    occurred_at: datetime
    stop_reason: StopReason
    error_code: str
    error_message: str
@dataclass(frozen=True, slots=True)
class StepStarted:
    step_id: str
    occurred_at: datetime
@dataclass(frozen=True, slots=True)
class StepSucceeded:
    step_id: str
    occurred_at: datetime
```

但如果类太多，也可以使用两个类型化统一结构：

```python
@dataclass(frozen=True, slots=True)
class RunStateEvent:
    event_type: RunEventType
    occurred_at: datetime
    stop_reason: StopReason | None = None
    final_output: str | None = None
    error_code: str | None = None
    error_message: str | None = None
@dataclass(frozen=True, slots=True)
class StepStateEvent:
    event_type: StepEventType
    step_id: str
    occurred_at: datetime
    error_code: str | None = None
    error_message: str | None = None
```

第一版推荐第二种，文件更简单。

但必须通过事件自身校验避免非法组合，例如：

```text
event_type = RUN_COMPLETED
error_code = UNHANDLED_ERROR
```

应直接拒绝。

------

# 十、事件与 Runtime Event 的区别

今天的 `RunStateEvent` / `StepStateEvent`：

```text
用途：
驱动内存内 AgentState 转移

消费者：
StateMachine

是否持久化：
否

是否发送前端：
否

是否用于 Trace：
暂时否
```

第 21 天的 Runtime Event：

```text
用途：
可观测、Trace、前端事件适配、Replay

消费者：
日志、Trace、UI Adapter

是否持久化：
可能

是否发送前端：
经过 Adapter 后可以
```

两者未来可以关联，但今天不能直接合并。

否则容易把状态机变成：

- 日志系统；
- 前端协议系统；
- Trace 系统；
- Replay 系统。

------

# 十一、状态机的所有权

推荐：

```text
AgentLoop
    持有或接收 StateMachine
```

运行时：

```text
AgentLoop
→ state_machine.apply_run_event(...)
→ state_machine.apply_step_event(...)
```

State Machine：

- 不持有全局状态；
- 不使用 Singleton（单例）；
- 不保存跨 Run 的共享数据；
- 不访问数据库；
- 不调用模型；
- 不执行 Tool；
- 不输出 Chunk。

推荐使用无状态对象：

```python
class AgentStateMachine:
    def apply_run_event(
        self,
        state: AgentState,
        event: RunStateEvent,
    ) -> None:
        ...

    def apply_step_event(
        self,
        state: AgentState,
        event: StepStateEvent,
    ) -> None:
        ...
```

或者将方法设为静态函数。

------

# 十二、状态修改的唯一入口

改造完成后，生产调用链中不应再出现：

```python
agent_loop.py:
    state.mark_running(...)
    state.start_step(...)
    state.succeed_step(...)
```

应改为：

```python
state_machine.apply_run_event(
    state,
    RunStateEvent.started(...),
)
state_machine.apply_step_event(
    state,
    StepStateEvent.started(...),
)
```

AgentState 原有 mutation 方法可以暂时保留为：

- 状态机内部实现细节；
- 兼容测试；
- 后续逐步收口。

但必须做到：

> Agent Loop 不再绕过 State Machine。

如果现有测试大量直接调用 mutation 方法，可以先保留它们，不要求今天全部删除。

结果文档要说明：

- 哪些生产调用已迁移；
- 哪些方法仍因兼容保留；
- 是否存在外部调用仍能绕过状态机。

------

# 十三、非法转移异常

推荐定义：

```python
class InvalidStateTransitionError(RuntimeError):
    ...
```

异常中可以包含：

- 实体类型：Run / Step；
- 当前状态；
- 事件类型；
- `run_id` 或 `step_id`；
- 安全原因码。

不要包含：

- 用户输入；
- Tool 参数；
- Prompt；
- 内部路径；
- 敏感上下文。

例如：

```text
Invalid Run transition:
state=succeeded, event=run_failed
```

------

# 十四、是否允许重复事件

这是状态机中非常关键的问题。

例如由于代码重入或重复回调，可能发生：

```text
STEP_SUCCEEDED
STEP_SUCCEEDED
```

有两种策略。

## 策略 A：严格拒绝

第二次事件抛出：

```text
InvalidStateTransitionError
```

优点：

- 立即暴露重复调用；
- 不隐藏状态管理 Bug；
- 适合当前开发阶段。

## 策略 B：幂等忽略

第二次相同事件直接返回，不修改状态。

优点：

- 适合分布式消息系统；
- 可容忍消息重复投递。

当前阶段推荐：

> 严格拒绝重复终态事件。

原因是现在尚未进入：

- Durable Execution；
- 消息重复投递；
- Event Store；
- At-least-once（至少一次）语义。

第 19 天再讨论幂等事件消费。

------

# 十五、Agent Loop 修改后的流程

## 正常路径

```text
AgentLoop 初始化
→ RUN_STARTED
→ Run: CREATED → RUNNING

决定 Action
→ 添加 StepState(PENDING)

执行前
→ STEP_STARTED
→ Step: PENDING → RUNNING

执行 Action
→ Observation.COMPLETED

→ STEP_SUCCEEDED
→ Step: RUNNING → SUCCEEDED

→ RUN_COMPLETED
→ Run: RUNNING → SUCCEEDED
```

------

## Deadline 路径

```text
Action 执行中
→ RunDeadlineExceededError

→ STEP_FAILED
→ Step: RUNNING → FAILED

→ RUN_DEADLINE_EXCEEDED
→ Run: RUNNING → FAILED
→ StopReason.DEADLINE_EXCEEDED

→ 原异常按现有约定继续传播
```

------

## Cancellation 路径

```text
Action 执行中
→ RunCancelledError

→ STEP_CANCELLED
→ Step: RUNNING → CANCELLED

→ RUN_CANCELLED
→ Run: RUNNING → CANCELLED
→ 当前兼容 StopReason.USER_CANCELLED
```

------

## 最大步骤

```text
已完成 max_steps
→ 下一轮仍需执行

→ RUN_MAX_STEPS_REACHED
→ Run FAILED
→ StopReason.MAX_STEPS_REACHED
```

此时不能有 active Step。

------

# 十六、LocalAgent 最小落地方案

## 预计新增文件

```text
core/runtime/state_machine.py
tests/test_state_machine.py
docs/learning/stage2/day05_state_machine_result.md
```

## 预计修改文件

```text
core/runtime/__init__.py
core/runtime/agent_loop.py
```

根据真实代码可能修改：

```text
core/runtime/state.py
tests/test_agent_state.py
tests/test_agent_loop.py
```

原则上不需要修改：

```text
core/chat_service.py
core/agent_router.py
```

除非现有依赖注入方式要求创建 State Machine。

------

## AgentState Schema

今天原则上不应修改：

```text
schema_version = 1
```

因为状态机只是改变状态如何更新，没有新增必须持久化的字段。

事件也不写入 AgentState。

------

# 十七、第 5 天高价值 Bad Case

今天至少设计三个。

------

## Bad Case 1：Run 先成功，Step 后成功

- **类型：假设构造**

### 触发条件

Agent Loop 收到 `COMPLETED` Observation 后先发：

```text
RUN_COMPLETED
```

随后才发：

```text
STEP_SUCCEEDED
```

### 故障表现

状态机处理 `RUN_COMPLETED` 时发现：

```text
active_step_ids 非空
存在 RUNNING Step
```

如果没有 Guard，可能出现：

```text
Run = SUCCEEDED
Step = RUNNING
```

### 根因

终态转移顺序错误，没有保证局部 Step 先结束，再结束整个 Run。

### 修复方案

固定顺序：

```text
STEP_SUCCEEDED
→ RUN_COMPLETED
```

并在 `RUN_COMPLETED` Guard 中强制：

```text
active_step_ids 必须为空
```

### 回归测试

- 直接对有 active Step 的 Run 应用 `RUN_COMPLETED`；
- 状态机抛 `InvalidStateTransitionError`；
- 原状态保持不变；
- 正确顺序可以完成。

### 对应知识点

- 状态不变量；
- 转换守卫；
- 父子生命周期；
- 原子状态更新。

### 面试表达

> 我构造了一个 Run 先成功、Step 后成功的顺序错误场景。没有 Guard 时会出现 Run 已完成但子步骤仍在运行。我通过先结束 Step、再完成 Run，并在 Run 完成转移上增加 active-step 守卫，从状态机层阻止这种矛盾状态。

------

## Bad Case 2：终态之后又收到失败事件

- **类型：假设构造**

### 触发条件

Run 已经：

```text
SUCCEEDED
```

随后由于延迟回调或错误的异常处理，再收到：

```text
RUN_FAILED
```

### 故障表现

如果允许覆盖：

```text
SUCCEEDED → FAILED
```

用户已经收到成功结果，但最终状态变成失败。

或者反过来：

```text
FAILED → SUCCEEDED
```

掩盖真实错误。

### 根因

缺少终态保护，多个异步或异常出口都能修改状态。

### 修复方案

所有终态默认不可再转移：

```text
SUCCEEDED / FAILED / CANCELLED
+ 任意 Event
→ InvalidStateTransitionError
```

### 回归测试

分别验证：

- `SUCCEEDED + FAILED`
- `FAILED + COMPLETED`
- `CANCELLED + STARTED`

全部被拒绝，状态不变。

### 对应知识点

- Terminal State；
- Late Event（迟到事件）；
- 并发竞态；
- 单一写入者。

### 面试表达

> 我在状态机中加入了终态保护，防止迟到的异常回调覆盖已经成功的 Run，也防止后续成功逻辑覆盖真实失败。非法事件会被显式拒绝，而不是静默改写状态。

------

## Bad Case 3：状态转移失败但对象已经被部分修改

- **类型：假设构造，重点高价值**

### 触发条件

状态机先执行：

```python
state.status = RunStatus.SUCCEEDED
state.stop_reason = StopReason.COMPLETED
```

然后调用：

```python
state.validate()
```

发现仍有 active Step，于是抛异常。

### 故障表现

虽然调用方收到异常，但 AgentState 已经被部分修改：

```text
RunStatus = SUCCEEDED
active_step_ids 非空
```

系统进入损坏状态。

### 根因

采用“先修改，后校验”的非原子转移方式，失败时没有回滚。

### 修复方案

推荐二选一：

#### 方案 A：修改前完整 Guard 校验

```text
先检查所有条件
→ 全部通过
→ 一次性修改
→ validate()
```

#### 方案 B：复制后修改

```text
复制状态
→ 在副本上转移和校验
→ 成功后替换
```

当前项目优先方案 A，成本更低。

### 回归测试

- 构造 active Step；
- 尝试 `RUN_COMPLETED`；
- 转移失败；
- 断言转移前后 `AgentState.to_dict()` 完全一致。

### 对应知识点

- Atomic Transition（原子转移）；
- 强异常安全保证；
- Guard；
- 状态回滚。

### 面试表达

> 我特别测试了非法转移是否会留下半修改状态。状态机不能先改字段再 validate，否则异常发生后对象已经损坏。我将 Guard 放在 mutation 前，并通过转移失败前后的状态快照一致性测试保证强异常安全。

------

# 十八、测试方案

## 1. Run 正常转移

- `CREATED + STARTED → RUNNING`
- `RUNNING + COMPLETED → SUCCEEDED`
- `RUNNING + FAILED → FAILED`
- `RUNNING + CANCELLED → CANCELLED`
- Deadline、最大步骤、无动作、重复动作映射正确。

## 2. Step 正常转移

- `PENDING + STARTED → RUNNING`
- `RUNNING + SUCCEEDED → SUCCEEDED`
- `RUNNING + FAILED → FAILED`
- `RUNNING + CANCELLED → CANCELLED`
- `PENDING + BLOCKED → BLOCKED`
- `PENDING + SKIPPED → SKIPPED`

## 3. 非法转移

- `CREATED + COMPLETED`
- `RUNNING + STARTED`
- `PENDING + SUCCEEDED`
- `RUNNING + BLOCKED`
- 终态后任何事件
- 不存在的 Step ID
- Step 与 Run 状态不匹配。

## 4. Guard

- active Step 存在时不能完成 Run；
- active Step 存在时不能直接失败 Run；
- Run 非 RUNNING 时不能开始 Step；
- Step 不在 active 集合却收到成功事件；
- Step 已在 active 集合时重复启动。

## 5. 原子性

每个非法转移测试都应验证：

```text
失败前状态 == 失败后状态
```

至少比较：

```python
before = state.to_dict()
...
after = state.to_dict()
self.assertEqual(before, after)
```

## 6. Event 数据校验

- 空 Step ID；
- naive datetime（无时区时间）；
- 错误 Event 与错误字段组合；
- `RUN_COMPLETED` 携带 error；
- `STEP_SUCCEEDED` 携带 error；
- `RUN_FAILED` 没有安全错误摘要；
- 不支持的 StopReason。

## 7. Agent Loop 集成

验证 Agent Loop 不再直接调用 AgentState mutation 方法。

通过 Fake State Machine 或 Mock 验证事件顺序：

### 正常完成

```text
RUN_STARTED
STEP_STARTED
STEP_SUCCEEDED
RUN_COMPLETED
```

### Deadline

```text
RUN_STARTED
STEP_STARTED
STEP_FAILED
RUN_DEADLINE_EXCEEDED
```

### Cancellation

```text
RUN_STARTED
STEP_STARTED
STEP_CANCELLED
RUN_CANCELLED
```

### 最大步骤

```text
RUN_STARTED
STEP_STARTED
STEP_SUCCEEDED
...
RUN_MAX_STEPS_REACHED
```

------

# 十九、Codex 实操提示词

你正在继续改造 LocalAgent 项目的阶段二 Runtime。

## 一、项目背景

LocalAgent 当前包括：

- PyQt6 前端；
- FastAPI 后端；
- 本地与远程模型；
- Router、Planner 和多 Agent 编排；
- Tool；
- RAG；
- SQLite Memory；
- Chroma；
- 自定义流式 HTTP 输出；
- `[[ORCH]]` 编排状态标记。

已经完成：

### 第 1 天：Runtime 边界

- `server.py::chat_endpoint()` 是聊天 API 入口；
- `core/chat_service.py::ChatService.stream_chat()` 是当前应用服务入口；
- `core/agent_router.py::AgentRouter.chat_stream()` 是遗留编排执行入口；
- 当前采用逐步包装遗留实现的方式，不大规模拆分 AgentRouter。

### 第 2 天：RunContext

已经实现：

- `RunContext`
- `run_id`
- `session_id`
- `trace_id`
- Deadline
- `CancellationSource`
- `CancellationToken`
- `create_run_context()`

### 第 3 天：AgentState

已经实现：

- `AgentState`
- `StepState`
- `RunStatus`
- `StepStatus`
- `StopReason`
- 状态不变量
- JSON 友好序列化
- `schema_version = 1`
- 安全错误摘要
- `BLOCKED` 语义

### 第 4 天：Agent Loop

已经实现：

- `AgentLoop`
- `AgentLoopPolicy`
- `AgentAction`
- `AgentObservation`
- `ActionOutcome`
- Legacy AgentRouter Driver
- 最大步骤
- 无动作检测
- 重复动作检测
- Deadline、Cancellation、未知异常处理
- `GeneratorExit` 不成功收尾
- 控制协议与语义输出分离
- `[[ORCH]]` 仍原样流式传输，但不进入 `AgentState.final_output`

当前 Agent Loop 仍然直接调用 AgentState 的受约束 mutation 方法。

本次任务是：

“阶段二第 5 天：State Machine，包括 State、Event、Transition、Guard、合法和非法状态转移，以及将 AgentState 修改统一收口到状态机。”

## 二、固定工作流

严格执行：

第一步：阅读项目结构和相关代码
第二步：总结现状和问题
第三步：给出最小改造方案
第四步：实施修改
第五步：补充或更新测试
第六步：运行测试和检查
第七步：输出结果文档
第八步：补充 1～3 个重点 Bad Case

不得跳过分析直接重写。

## 三、本次目标

建立一个最小、同步、内存内 AgentStateMachine，使：

```text
AgentLoop
→ 产生 Run / Step 状态事件
→ AgentStateMachine 校验当前状态、事件和 Guard
→ 原子地更新 AgentState
```

本次目标包括：

- Run 状态转移；
- Step 状态转移；
- Terminal State 保护；
- Guard；
- 非法转移异常；
- 转移失败不留下部分修改；
- Agent Loop 使用状态机，而不再直接修改 AgentState；
- 保持现有流式输出、AgentState Schema 和业务行为不变。

本次不是完整事件驱动架构，不是 Event Sourcing，不是 Runtime Event 总线。

## 四、修改前必须检查

请至少检查：

- `core/runtime/state.py`
- `core/runtime/agent_loop.py`
- `core/runtime/context.py`
- `core/runtime/__init__.py`
- `core/chat_service.py`
- `tests/test_agent_state.py`
- `tests/test_agent_loop.py`
- 第 3、4 天结果文档
- 所有 AgentState mutation 方法调用位置
- 所有 RunStatus、StepStatus、StopReason 修改位置

必须根据真实代码调整，不得假设方法签名。

## 五、建议新增文件

建议新增：

```text
core/runtime/state_machine.py
tests/test_state_machine.py
docs/learning/stage2/day05_state_machine_result.md
```

不要拆成大量事件文件。

## 六、核心类型

### 1. RunEventType

第一版至少包括：

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

`BUDGET_EXHAUSTED` 只建立状态语义，本次不实现 Budget。

### 2. StepEventType

至少包括：

```text
STARTED
SUCCEEDED
FAILED
CANCELLED
BLOCKED
SKIPPED
```

### 3. RunStateEvent

至少包含：

- `event_type`
- `occurred_at`
- `stop_reason`
- `final_output`
- `error_code`
- `error_message`

可以根据事件类型限制字段组合。

要求：

- `occurred_at` 必须是 timezone-aware UTC datetime；
- 不允许空错误码；
- 不允许原始异常对象；
- 不允许 traceback；
- 不允许敏感数据；
- `COMPLETED` 不得携带错误；
- `FAILED` 类事件必须使用合法失败 StopReason；
- `CANCELLED` 只能使用合法取消 StopReason。

### 4. StepStateEvent

至少包含：

- `event_type`
- `step_id`
- `occurred_at`
- `error_code`
- `error_message`

要求：

- Step ID 非空；
- `SUCCEEDED` 不得携带错误；
- `FAILED` 至少有安全 error code 或安全摘要；
- 不保存异常对象或自由 payload。

### 5. AgentStateMachine

至少提供：

```text
apply_run_event(state, event)
apply_step_event(state, event)
```

要求：

- 不持有全局状态；
- 不访问数据库；
- 不执行 Model、Tool、RAG；
- 不输出流式 Chunk；
- 不访问 CancellationToken；
- 不依赖具体本地或远程模型；
- 不使用全局单例；
- 不使用 `dict[str, Any]` 通用 payload。

### 6. InvalidStateTransitionError

包含安全的：

- 实体类型；
- 当前状态；
- 事件类型；
- run_id 或 step_id；
- 安全原因。

不得包含用户输入、Prompt、Tool 参数、文件路径或敏感配置。

## 七、Run 状态转移规则

### CREATED

允许：

```text
STARTED → RUNNING
FAILED → FAILED
CANCELLED → CANCELLED
```

禁止：

```text
COMPLETED
DEADLINE_EXCEEDED
MAX_STEPS_REACHED
NO_ACTION
REPEATED_ACTION
BUDGET_EXHAUSTED
```

除非真实代码存在充分理由，否则 `CREATED` 不允许直接成功。

### RUNNING

允许：

```text
COMPLETED → SUCCEEDED
FAILED → FAILED
DEADLINE_EXCEEDED → FAILED
MAX_STEPS_REACHED → FAILED
NO_ACTION → FAILED
REPEATED_ACTION → FAILED
BUDGET_EXHAUSTED → FAILED
CANCELLED → CANCELLED
```

禁止再次 `STARTED`。

### SUCCEEDED / FAILED / CANCELLED

默认禁止所有后续事件。

本次使用严格模式，不把重复终态事件静默当作幂等成功。

幂等事件消费留到后续阶段。

## 八、Step 状态转移规则

### PENDING

允许：

```text
STARTED → RUNNING
CANCELLED → CANCELLED
BLOCKED → BLOCKED
SKIPPED → SKIPPED
```

禁止：

```text
SUCCEEDED
FAILED
```

当前采用严格模式：Action 被接受并创建 Step 后先进入 RUNNING，再成功或失败。

### RUNNING

允许：

```text
SUCCEEDED → SUCCEEDED
FAILED → FAILED
CANCELLED → CANCELLED
```

禁止：

```text
STARTED
BLOCKED
SKIPPED
```

### 终态

`SUCCEEDED`、`FAILED`、`CANCELLED`、`BLOCKED`、`SKIPPED` 默认禁止任何后续事件。

## 九、Guard 要求

至少实现：

### Run Completed Guard

`RUN_COMPLETED` 前必须：

- `active_step_ids` 为空；
- 没有 RUNNING Step；
- Run 当前是 RUNNING；
- final output 符合当前 AgentState 规则。

### Run Failure Guard

Run 进入 FAILED 前：

- 不能仍有 active Step；
- 调用方必须先结束所有 active Step。

### Run Cancellation Guard

Run 进入 CANCELLED 前：

- 不能仍有 active Step；
- 调用方必须先取消 active Step。

### Step Started Guard

- Step 存在；
- Step 当前 PENDING；
- Step 不在 active 集合；
- Run 当前 RUNNING。

### Step Completed Guard

- Step 当前 RUNNING；
- Step 位于 active 集合。

### Step Blocked / Skipped Guard

- Step 当前 PENDING；
- Step 不在 active 集合；
- 不允许将已经运行的 Step 标为 BLOCKED 或 SKIPPED。

## 十、原子性要求

状态转移必须满足：

> 转移失败时，AgentState 和 StepState 不得被部分修改。

优先实现方式：

1. 先检查当前状态、事件字段和全部 Guard；
2. 全部通过后再修改；
3. 修改完成后执行现有 `validate()` 作为最终防线。

至少为非法转移增加状态快照测试：

```text
before = state.to_dict()
apply_event() 抛异常
after = state.to_dict()
before == after
```

不得采用先改字段、再 validate、失败后不回滚的方式。

## 十一、Agent Loop 集成

将 Agent Loop 中直接调用的：

- `mark_running`
- `start_step`
- `succeed_step`
- `fail_step`
- `cancel_step`
- `mark_succeeded`
- `mark_failed`
- `mark_cancelled`

迁移为状态事件。

建议正常完成事件顺序：

```text
RUN_STARTED
STEP_STARTED
STEP_SUCCEEDED
RUN_COMPLETED
```

Deadline：

```text
RUN_STARTED
STEP_STARTED
STEP_FAILED
RUN_DEADLINE_EXCEEDED
```

Cancellation：

```text
RUN_STARTED
STEP_STARTED
STEP_CANCELLED
RUN_CANCELLED
```

最大步骤：

```text
已有 Step 全部终止
RUN_MAX_STEPS_REACHED
```

无动作时不创建 Step，直接：

```text
RUN_NO_ACTION
```

重复动作超限时不创建超限 Step，直接：

```text
RUN_REPEATED_ACTION
```

未知异常：

```text
STEP_FAILED
RUN_FAILED
```

原始异常仍进入受控日志并继续向上传播。

## 十二、AgentState mutation 方法

现有 AgentState mutation 方法可以暂时保留，用作：

- State Machine 内部实现；
- 兼容旧测试；
- 后续迁移。

但生产 Agent Loop 不得再绕过 State Machine。

在结果文档中列出：

- 哪些生产调用已迁移；
- 哪些 mutation 方法仍保留；
- 是否仍有生产调用绕过状态机。

不要为了删除所有旧方法大规模重写测试。

## 十三、AgentState Schema

本次原则上保持：

```text
schema_version = 1
```

不得将事件历史写入 AgentState。

不得新增：

- Event Log；
- Transition History；
- Revision；
- Checkpoint 字段。

如果真实实现必须修改 Schema，请停止代码修改并在结果中说明，不得擅自升级。

## 十四、Bad Case 要求

结果文档必须新增：

```markdown
## 19. 重点 Bad Case
```

至少包含以下三个。

### Bad Case 1：Run 先成功，Step 后成功

- 类型：假设构造
- active Step 存在时收到 RUN_COMPLETED
- State Machine 必须拒绝
- 状态失败前后完全一致
- 正确顺序为 STEP_SUCCEEDED → RUN_COMPLETED

### Bad Case 2：终态后收到迟到失败事件

- 类型：假设构造
- Run 已 SUCCEEDED，随后收到 RUN_FAILED
- 必须通过终态保护拒绝
- 不允许成功被迟到异常覆盖
- 测试状态不变

### Bad Case 3：非法转移留下半修改状态

- 类型：假设构造，除非真实代码检查后发现确实存在
- 测试先修改后 validate 的风险
- 使用 mutation 前 Guard 保证原子性
- 转移失败前后 `to_dict()` 必须一致

每个 Bad Case 使用固定格式：

```markdown
### Bad Case X：名称

- 类型：真实发现 / 假设构造
- 触发条件：
- 故障表现：
- 根因分析：
- 修复方案：
- 回归测试：
- 对应知识点：
- 面试表达：
- 当前状态：
```

不得把假设构造描述成真实生产事故。

如果在真实代码中发现更高价值 Bad Case，可以替换或增加，但必须说明真实性。

## 十五、测试要求

继续使用 `unittest`。

至少新增：

```text
tests/test_state_machine.py
```

覆盖：

### Run 合法转移

1. CREATED → RUNNING
2. RUNNING → SUCCEEDED
3. RUNNING → FAILED
4. RUNNING → CANCELLED
5. Deadline 映射
6. 最大步骤映射
7. 无动作映射
8. 重复动作映射
9. Budget Exhausted 状态语义

### Step 合法转移

1. PENDING → RUNNING
2. RUNNING → SUCCEEDED
3. RUNNING → FAILED
4. RUNNING → CANCELLED
5. PENDING → BLOCKED
6. PENDING → SKIPPED

### 非法转移

1. CREATED → SUCCEEDED
2. RUNNING 再 STARTED
3. PENDING → SUCCEEDED
4. RUNNING → BLOCKED
5. 终态后任意事件
6. 不存在的 Step ID
7. Run 非 RUNNING 时开始 Step
8. Step 不在 active 集合却成功
9. active Step 存在时完成 Run
10. active Step 存在时失败 Run
11. active Step 存在时取消 Run

### 原子性

1. 每个典型非法 Run 转移失败后状态不变
2. 每个典型非法 Step 转移失败后状态不变

### Event 校验

1. naive datetime 被拒绝
2. 空 Step ID 被拒绝
3. COMPLETED 携带 error 被拒绝
4. SUCCEEDED Step 携带 error 被拒绝
5. FAILED 缺少安全错误信息被拒绝
6. CANCELLED 使用非法 StopReason 被拒绝

### Agent Loop 集成

1. 正常事件顺序
2. Deadline 事件顺序
3. Cancellation 事件顺序
4. 最大步骤事件
5. 无动作事件
6. 重复动作事件
7. 未知异常事件
8. Agent Loop 不直接绕过 State Machine
9. 原有流式输出不变
10. `[[ORCH]]` 不进入 final output

不要启动真实模型、Chroma、UI、FastAPI 或数据库，不得访问外部网络。

## 十六、模型轻重路由兼容

本次不得实现模型轻重路由。

State Machine 不得出现：

- 本地模型；
- 远程模型；
- Provider；
- Qwen；
- DeepSeek；
- Model Profile；
- Fallback；
- Circuit Breaker。

未来模型选择只会产生新的 Action、Observation 或错误事件，不应改变状态机核心架构。

## 十七、代码质量

要求：

- 完整类型标注；
- 清晰 Docstring；
- timezone-aware UTC datetime；
- 不使用 `Any` 通用 payload；
- 不使用全局可变状态；
- 不保存敏感信息；
- 不新增大型依赖；
- 不使用 pickle；
- 不把原始异常写入状态事件；
- 不使用 `print` 作为正式日志；
- 不大规模拆分文件。

## 十八、执行检查

至少执行：

```text
python -m unittest \
  tests.test_runtime_context \
  tests.test_agent_state \
  tests.test_agent_loop \
  tests.test_state_machine \
  -v

python -m compileall core tests

git diff --check
```

## 十九、GitHub 工作流

允许在当前指定仓库和任务分支：

- Commit；
- Push；
- 创建或更新 PR。

必须：

- 只操作当前仓库和任务分支；
- 不操作无关分支；
- 不擅自合并 PR；
- PR 说明修改范围、测试命令、测试结果和风险；
- 不上传公司内部代码、配置、日志、数据、接口地址或其他敏感信息；
- 不在 Commit、PR 或文档中包含 Secret。

## 二十、禁止事项

不得：

- 实现 Planner；
- 实现 Scheduler；
- 实现 DAG；
- 实现并行执行；
- 实现 Budget；
- 实现 Retry；
- 实现模型轻重路由；
- 实现 Fallback；
- 实现 Checkpoint；
- 实现 Resume；
- 实现 Event Store；
- 实现 Event Sourcing；
- 实现 Runtime Event 总线；
- 实现 Trace；
- 修改 API、Memory Schema 或流式协议；
- 修改 `[[ORCH]]` 协议；
- 大规模重写 AgentRouter；
- 添加状态持久化；
- 擅自合并 PR。

## 二十一、结果文档

创建：

```text
docs/learning/stage2/day05_state_machine_result.md
```

必须包含：

# 阶段二第 5 天改造结果

## 1. 本次任务目标

## 2. 修改前现状

## 3. 发现的问题

## 4. 最终设计方案

## 5. 新增文件

## 6. 修改文件

## 7. 核心类、接口和数据结构

说明：

- AgentStateMachine
- RunEventType
- StepEventType
- RunStateEvent
- StepStateEvent
- InvalidStateTransitionError
- Guard
- Terminal State

## 8. Run 状态转移表

## 9. Step 状态转移表

## 10. Guard 和原子性

## 11. Agent Loop 集成

## 12. 与现有功能兼容方式

## 13. 测试内容

## 14. 实际测试命令

## 15. 测试结果

## 16. 未完成事项和已知风险

至少说明：

- 当前不是 Event Sourcing；
- 当前事件不持久化；
- 当前没有 Runtime Event 总线；
- 当前没有 Planner 或 Scheduler；
- mutation 方法是否仍保留；
- 是否仍有调用绕过状态机；
- AgentState 仍不持久化；
- Generator close 仍无法形成可靠终态；
- 模型轻重路由尚未实现。

## 17. 设计权衡

## 18. 可用于面试的项目描述

不得声称已经实现完整事件驱动架构、事件溯源或分布式状态机。

## 19. 重点 Bad Case

按固定格式输出至少三个。

## 20. 需要带回 ChatGPT 审查的信息

必须包含：

- State Machine 真实文件和入口；
- Agent Loop 如何注入或调用 State Machine；
- Run Event 最终枚举；
- Step Event 最终枚举；
- Run 转移表；
- Step 转移表；
- Guard 实现位置；
- 原子性实现方式；
- 非法转移异常内容；
- 终态保护策略；
- 重复事件策略；
- Agent Loop 的事件顺序；
- AgentState mutation 方法是否保留；
- 是否仍有生产代码绕过状态机；
- 是否修改 AgentState Schema；
- 是否实现 Event Store 或 Runtime Event；
- 新增和修改文件；
- 测试命令与结果；
- Bad Case；
- Commit / PR；
- 需要人工确认的问题；
- 后续建议，但不得实施第 6 天内容。

## 二十二、聊天最终输出

完成后输出：

结果文档路径：

本次新增文件：

本次修改文件：

State Machine 入口：

Run Event：

Step Event：

Run 转移规则：

Step 转移规则：

Guard 实现：

原子性实现：

终态保护：

重复事件策略：

Agent Loop 事件顺序：

是否仍有生产代码绕过状态机：

是否修改 AgentState Schema：

是否实现 Event Store / Runtime Event：

测试命令：

测试是否通过：

Bad Case：

Commit：

PR：

需要人工确认的问题：

------

# 二十、Codex 结果审查重点

结果回来后重点检查：

1. Agent Loop 是否真正通过状态机修改状态。
2. 是否仍存在生产代码直接调用 AgentState mutation。
3. 状态机是否无状态、无全局共享数据。
4. Run 和 Step 事件是否类型化。
5. 是否错误使用 `dict[str, Any]` payload。
6. `RUN_COMPLETED` 是否要求 active steps 为空。
7. Run 失败和取消是否先结束 active Step。
8. Step 终态是否受到保护。
9. Run 终态是否受到保护。
10. 非法转移是否抛明确异常。
11. 非法转移失败后状态是否完全不变。
12. 是否采用先修改后校验却没有回滚。
13. 事件时间是否为 UTC 时区感知时间。
14. Event 字段组合是否存在矛盾。
15. 是否把状态事件误当成 Runtime Event。
16. 是否提前实现 Event Store 或 Event Sourcing。
17. AgentState Schema 是否保持版本 1。
18. 原有 43 个测试是否继续通过。
19. 新的状态机测试是否覆盖事件顺序。
20. Bad Case 是否区分真实与假设。

------

# 二十一、面试高频问题

## 1. 状态不变量和状态机有什么区别？

> 状态不变量验证当前状态快照是否自洽；状态机验证从当前状态经过某个事件是否允许进入目标状态。前者关注结果是否合法，后者关注过程是否合法。

## 2. Agent Loop 和状态机如何分工？

> Agent Loop 负责决策、执行、观察和终止流程；状态机只负责状态转移校验和更新。Loop 发出事件，状态机决定能否转移。

## 3. 为什么终态默认不能再转移？

> 因为迟到回调、重复异常或并发出口可能覆盖已经确定的最终结果。终态保护能够防止成功被失败覆盖，也防止真实失败被后续成功逻辑掩盖。

## 4. 如何保证非法转移不会损坏 AgentState？

> 在修改前执行完整状态与 Guard 校验，全部通过后再修改，并在修改后执行不变量校验。测试会对比非法转移前后的序列化快照，确保状态完全不变。

## 5. 为什么现在不做事件溯源？

> 当前需求只是统一内存内状态转移。事件溯源还涉及事件持久化、版本、重放、幂等和副作用一致性，过早引入会增加复杂度，应该在 Checkpoint、Replay 和 Durable Execution 阶段再考虑。

------

# 二十二、当天验收清单

## 理论验收

-  理解 State、Event、Transition、Guard
-  区分 Command 和 Event
-  区分状态事件和 Runtime Event
-  理解 Run 状态转移表
-  理解 Step 状态转移表
-  理解终态保护
-  理解非法转移
-  理解转移原子性
-  理解 Agent Loop 与 State Machine 的边界
-  理解重复事件当前采用严格拒绝

## 项目验收

-  新增 AgentStateMachine
-  新增 Run Event
-  新增 Step Event
-  新增非法转移异常
-  Run 转移表完成
-  Step 转移表完成
-  Guard 完成
-  非法转移保持状态不变
-  Agent Loop 使用状态机
-  生产 Agent Loop 不再直接修改状态
-  终态保护测试通过
-  事件顺序测试通过
-  AgentState Schema 保持版本 1
-  未实现 Event Store
-  未实现 Runtime Event 总线
-  原有测试通过
-  新增状态机测试通过
-  结果文档包含 Bad Case
-  完成 ChatGPT 审查

## 阶段二进度

**第 5/25 天：理论与架构方案完成，等待 Codex 改造结果审查。**

下一天主题：**Context Engineering（上下文工程）——系统指令、历史消息、Memory、RAG、Tool Result、Plan 和运行状态如何组合、裁剪、去重并满足模型能力要求。**