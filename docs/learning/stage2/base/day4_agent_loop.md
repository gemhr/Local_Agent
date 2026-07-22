# 阶段二第 4 天：Agent Loop（智能体循环）

**当前进度：第 4/25 天，理论与架构设计开始。**

前三天已经建立了：

```text
第 1 天：Runtime 边界
第 2 天：RunContext
第 3 天：AgentState
```

今天把它们串成一条真正的执行主线：

```text
RunContext 提供运行环境
        +
AgentState 保存执行事实
        +
Agent Loop 持续推进任务
```

------

# 一、当天目标

今天必须解决：

1. Agent Loop 的标准执行周期是什么。
2. Agent、Router、Planner、Runtime 和 Agent Loop 分别负责什么。
3. 循环如何完成：
   - 初始化；
   - 决策；
   - 执行；
   - 观察；
   - 状态更新；
   - 终止。
4. 如何防止：
   - 无限循环；
   - 连续无动作；
   - 重复执行相同行动。
5. 成功、失败、取消和 Deadline（截止时间）如何从循环退出。
6. 如何在不拆分现有 `AgentRouter` 的情况下，为 LocalAgent 建立最小 Agent Loop。
7. 如何让今天的结构兼容后续模型轻重路由，而不在循环中硬编码本地或远程模型。

## 今天不处理

今天暂不实现：

- State Machine（状态机）；
- 通用状态事件和状态转移表；
- 结构化 Planner（规划器）；
- PlanStep（计划步骤）；
- Scheduler（调度器）；
- DAG（有向无环图）；
- 并行执行；
- Budget（预算）；
- Retry（重试）；
- 模型轻重路由的正式策略；
- Checkpoint（检查点）；
- Resume（恢复执行）。

第 4 天只解决：

> Runtime 怎样以统一循环持续推进一次 Run。

------

# 二、为什么必须有 Agent Loop

当前 LocalAgent 的执行方式更接近：

```text
ChatService
→ AgentRouter.chat_stream()
→ Router 内部自行完成规划、Tool、RAG、模型调用和输出
```

外部系统只能看到：

```text
调用开始
→ 返回数据
→ 调用结束或抛出异常
```

它不能统一控制：

- 已经执行了多少次行动；
- Agent 下一步准备做什么；
- 上一次行动得到了什么观察结果；
- 是否一直没有产生有效行动；
- 是否重复调用相同 Tool；
- 是否应该继续；
- 为什么停止；
- 当前 Run 与 Step 状态如何更新。

Agent Loop 的作用是将执行过程规范为：

```text
初始化
→ 决策
→ 执行动作
→ 获得观察
→ 更新状态
→ 判断终止
→ 再次决策
```

可以抽象为：

```text
while Run 仍然有效:
    检查取消、Deadline 和循环限制
    获取下一步 Action
    执行 Action
    获得 Observation
    更新 AgentState
    判断是否完成
```

------

# 三、Agent Loop 的六个阶段

## 1. Initialize（初始化）

循环开始前需要：

- 校验 `RunContext.run_id == AgentState.run_id`；
- 检查 Run 尚未进入终态；
- 将 Run 从 `CREATED` 更新为 `RUNNING`；
- 初始化循环计数器；
- 初始化无动作计数器；
- 初始化重复动作检测信息；
- 检查 Cancellation Token（取消令牌）；
- 检查 Deadline。

初始化不应该：

- 选择本地或远程模型；
- 拼接 Prompt；
- 执行 Tool；
- 修改 Memory；
- 创建 Planner 计划。

这些属于后续决策或执行阶段。

------

## 2. Decide（决策）

决策阶段回答：

> 下一步应该执行什么动作？

当前阶段可以定义一个最小 `AgentAction`：

```python
@dataclass(frozen=True, slots=True)
class AgentAction:
    step_id: str
    name: str
    action_type: str
    dedup_key: str
```

字段含义：

- `step_id`：对应 AgentState 中的 Step；
- `name`：便于日志和调试；
- `action_type`：动作类型；
- `dedup_key`：检测重复动作的安全标识。

未来动作可能包括：

```text
CALL_MODEL
CALL_TOOL
RETRIEVE
DELEGATE_AGENT
REQUEST_REPLAN
GENERATE_FINAL
```

但今天只需要：

```text
EXECUTE_LEGACY_AGENT_ROUTER
```

即把现有整个 `AgentRouter.chat_stream()` 包装成一个兼容动作。

### 决策阶段不应该直接执行动作

错误设计：

```python
def decide():
    result = tool.execute()
    return result
```

这把决策与执行混在了一起。

正确设计：

```python
def decide() -> AgentAction | None:
    return AgentAction(...)
```

执行由 Agent Loop 的执行阶段负责。

------

## 3. Execute（执行）

执行阶段根据 Action 调用真正的能力：

```text
AgentAction
→ Action Executor
→ Model / Tool / RAG / AgentRouter
```

当前兼容执行：

```text
EXECUTE_LEGACY_AGENT_ROUTER
→ AgentRouter.chat_stream()
```

因为 LocalAgent 使用同步 Generator（生成器）进行流式输出，执行接口可以采用：

```python
from collections.abc import Generator

def execute(
    action: AgentAction,
    context: RunContext,
) -> Generator[str, None, AgentObservation]:
    ...
```

这个接口既能：

- 通过 `yield` 输出文本流；
- 又能通过 Generator 的返回值产生最终 Observation（观察结果）。

Agent Loop 中可以写成：

```python
observation = yield from executor.execute(
    action=action,
    context=context,
)
```

这样不会破坏现有流式输出。

------

## 4. Observe（观察）

Observation 不是给用户看的普通文本，而是 Runtime 对一次动作结果的结构化认识。

可以定义：

```python
class ActionOutcome(str, Enum):
    CONTINUE = "continue"
    COMPLETED = "completed"
    FAILED = "failed"
@dataclass(frozen=True, slots=True)
class AgentObservation:
    outcome: ActionOutcome
    final_output: str | None = None
    error_code: str | None = None
    error_message: str | None = None
```

语义如下。

### CONTINUE

本次动作成功，但整个 Run 还没完成：

```text
Tool 执行成功
→ 产生 Tool Result
→ 继续让模型决策
```

### COMPLETED

本次动作已经产生最终结果：

```text
最终回答生成完成
→ Run 成功终止
```

### FAILED

本次动作失败，循环不再继续：

```text
Step FAILED
Run FAILED
```

当前兼容 `AgentRouter` 一次调用就会产生最终回答，因此一般返回：

```text
ActionOutcome.COMPLETED
```

------

## 5. Update（状态更新）

Agent Loop 根据 Observation 更新：

- StepStatus（步骤状态）；
- `active_step_ids`；
- RunStatus（运行状态）；
- StopReason（终止原因）；
- 最终输出；
- 安全错误摘要。

### CONTINUE

```text
当前 Step → SUCCEEDED
Run 保持 RUNNING
继续下一轮
```

### COMPLETED

```text
当前 Step → SUCCEEDED
Run → SUCCEEDED
StopReason → COMPLETED
```

### FAILED

```text
当前 Step → FAILED
Run → FAILED
StopReason → UNHANDLED_ERROR 或明确失败原因
```

今天仍然由 Agent Loop 调用第 3 天的受约束状态方法。

第 5 天会进一步改成：

```text
Agent Loop 发出 Event
→ State Machine 更新 AgentState
```

------

## 6. Terminate（终止）

终止条件分成两类。

### 正常终止

```text
Observation = COMPLETED
→ Run SUCCEEDED
```

### 保护性或异常终止

```text
达到最大步骤
连续无动作
重复相同动作
Deadline 到期
收到取消
执行异常
```

Agent Loop 必须保证：

> 无论从哪个出口离开，都不能错误地执行成功收尾。

------

# 四、Agent Loop 与其他模块的边界

## 1. Agent Loop 与 Agent

Agent 负责：

```text
理解任务
决定下一步动作
解释观察结果
决定是否完成
```

Agent Loop 负责：

```text
反复调用决策
执行动作
更新状态
检查限制
决定是否允许继续
```

一句话区分：

> Agent 产生行为意图，Agent Loop 控制行为如何持续执行。

------

## 2. Agent Loop 与 Router

Router（路由器）回答：

> 应该由哪个 Agent 或处理路径负责？

Agent Loop 回答：

> 下一轮如何推进，是否允许继续？

当前 `AgentRouter` 同时承担两种职责。今天不会立即拆分，而是把它作为兼容动作执行。

------

## 3. Agent Loop 与 Planner

Planner 负责生成结构化计划：

```text
步骤有哪些
步骤依赖什么
完成条件是什么
```

Agent Loop 负责重复推进整个执行周期。

第 7 天以后，Loop 会从 Planner 或 Scheduler 获取下一步动作；今天没有结构化计划。

------

## 4. Agent Loop 与 Scheduler

Scheduler 回答：

> 当前哪些步骤已经 Ready，可以被执行？

Agent Loop 回答：

> 获得步骤后如何执行、观察、更新并继续。

Scheduler 属于第 8 天。

------

## 5. Agent Loop 与状态机

Agent Loop 负责执行流程：

```text
决定
→ 执行
→ 观察
```

状态机负责：

```text
当前状态能否转移到目标状态
```

今天 Agent Loop 暂时调用：

```python
state.start_step(...)
state.succeed_step(...)
state.mark_failed(...)
```

第 5 天改为：

```text
Agent Loop
→ 发出 STEP_STARTED 事件
→ State Machine 校验并修改状态
```

------

# 五、最大步骤限制

## 1. 为什么需要

Agent 可能不断：

- 重新规划；
- 重复调用 Tool；
- 重复检索；
- 在多个 Agent 间互相委派；
- 一直无法形成最终答案。

没有限制时，可能产生：

- 无限循环；
- Token 浪费；
- 远程调用费用失控；
- UI 长时间无响应；
- 资源无法释放。

------

## 2. 什么算一个 Step

今天建议定义：

> 一个非空 AgentAction 被接受并准备执行时，计为一个 Step。

计数应在执行前增加：

```text
接受 Action
→ step_count += 1
→ 执行 Action
```

这样即使执行失败，也会消耗一次 Step。

不能只在成功后计数，否则失败动作可能无限重试而不增加计数。

------

## 3. 检查时机

假设：

```text
max_steps = 3
```

允许执行三个 Action。

流程：

```text
第 1 个 Action：允许
第 2 个 Action：允许
第 3 个 Action：允许
下一轮仍要求继续：
→ MAX_STEPS_REACHED
```

对应：

```text
RunStatus = FAILED
StopReason = MAX_STEPS_REACHED
```

当前步骤已经结束，因此终止时不应存在 active Step。

------

## 4. 与 Budget 的区别

最大步骤本质上属于一种预算，但今天先作为 Loop Policy（循环策略）实现。

第 11 天会统一纳入 Budget：

```text
max_steps
max_model_calls
max_tool_calls
max_tokens
max_cost
```

今天不要提前创建完整 Budget 模块。

------

# 六、无动作检测

## 1. 什么叫无动作

决策阶段返回：

```python
None
```

或者明确表示：

```text
没有可执行动作
但也没有产生最终回答
```

这代表 Agent 无法推进。

常见原因：

- 模型输出格式不合法；
- Planner 没有生成步骤；
- Router 无法选择 Agent；
- 所有分支都被过滤；
- Agent 只给出思考，没有 Action；
- 解析器无法提取 Tool Call。

------

## 2. 为什么不能无限重新询问

错误处理：

```text
没有 Action
→ 再问一次模型
→ 仍没有 Action
→ 再问一次
→ 无限循环
```

建议配置：

```python
max_consecutive_no_action: int = 1
```

第一版默认一次无动作就终止：

```text
RunStatus = FAILED
StopReason = NO_ACTION
```

以后如果某些模型偶尔格式错误，可以配置为 2，但必须防止同步空转。

------

## 3. 无动作不应创建虚假 Step

如果根本没有 Action，就不要创建：

```text
step-no-action
```

因为没有真实执行动作。

直接终止 Run 即可。

------

# 七、重复动作检测

## 1. 什么叫重复动作

例如 Agent 连续决定：

```text
调用 search_docs(query="runtime")
调用 search_docs(query="runtime")
调用 search_docs(query="runtime")
```

或者：

```text
委派给 code_expert
委派给 code_expert
委派给 code_expert
```

如果 Observation 没有带来变化，却一直执行相同动作，很可能已经进入死循环。

------

## 2. 不应直接比较完整对象

完整 Action 可能包含：

- 用户输入；
- Prompt；
- Tool 参数；
- 文件路径；
- 大量文本；
- 敏感数据。

不应把这些内容直接写入日志或状态作为去重依据。

建议由决策层生成安全的：

```text
dedup_key
```

例如：

```text
tool:search_docs:query_hash
agent:code_expert:task_hash
legacy:agent_router
```

第一版也可以只是明确的非敏感动作键。

------

## 3. 为什么不使用 Python `hash()`

Python 内置 `hash()`：

- 不保证跨进程稳定；
- 可能因随机化产生不同值；
- 不适合未来 Checkpoint 和 Replay。

未来需要稳定指纹时，可以使用：

```text
规范化 JSON
→ SHA-256
```

但今天不必构建完整 Action Fingerprint（动作指纹）系统。

------

## 4. 重复阈值语义

建议定义：

```python
max_consecutive_same_action: int = 2
```

含义是：

- 第一次：执行；
- 第二次相同：允许执行；
- 第三次连续相同：在执行前终止。

对应：

```text
RunStatus = FAILED
StopReason = REPEATED_ACTION
```

这样允许偶尔重复尝试一次，又能防止无限重复。

Retry 属于第 13 天，今天不能把重复动作检测当成重试系统。

------

# 八、异常与终止映射

## 1. 正常完成

```text
Observation.COMPLETED
→ Step SUCCEEDED
→ Run SUCCEEDED
→ COMPLETED
```

## 2. 最大步骤

```text
达到 max_steps
→ Run FAILED
→ MAX_STEPS_REACHED
```

## 3. 无动作

```text
连续无动作达到阈值
→ Run FAILED
→ NO_ACTION
```

## 4. 重复动作

```text
连续相同动作超过阈值
→ Run FAILED
→ REPEATED_ACTION
```

## 5. Deadline

```text
RunDeadlineExceededError
→ 当前 Step FAILED（如果存在）
→ Run FAILED
→ DEADLINE_EXCEEDED
```

## 6. Cancellation

```text
RunCancelledError
→ 当前 Step CANCELLED（如果存在）
→ Run CANCELLED
→ 当前兼容 USER_CANCELLED
```

真实取消来源区分留到第 12 天。

## 7. 未知异常

```text
未知 Exception
→ logger.exception(...)
→ 当前 Step FAILED
→ Run FAILED
→ UNHANDLED_ERROR
→ 继续向上抛出原异常
```

安全状态摘要继续使用第 3 天约定：

```text
error_code = UNHANDLED_ERROR
error_message = Agent execution failed
```

## 8. GeneratorExit

必须：

```text
不标记成功
不吞掉 GeneratorExit
不伪造 CLIENT_DISCONNECTED
```

继续保留第 3 天已知限制，完整处理在第 12 天。

------

# 九、推荐的最小数据结构

以下是架构示意，不要求 Codex逐字照搬。

```python
from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ActionOutcome(str, Enum):
    CONTINUE = "continue"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentAction:
    step_id: str
    name: str
    action_type: str
    dedup_key: str


@dataclass(frozen=True, slots=True)
class AgentObservation:
    outcome: ActionOutcome
    final_output: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class AgentLoopPolicy:
    max_steps: int = 8
    max_consecutive_no_action: int = 1
    max_consecutive_same_action: int = 2


class AgentLoopDriver(Protocol):
    def decide(
        self,
        *,
        context: RunContext,
        state: AgentState,
        previous_observation: AgentObservation | None,
    ) -> AgentAction | None:
        ...

    def execute(
        self,
        *,
        action: AgentAction,
        context: RunContext,
    ) -> Generator[str, None, AgentObservation]:
        ...
```

这里的 Driver（循环驱动器）暂时负责：

- 给 Loop 提供动作；
- 执行动作；
- 将现有 AgentRouter 适配成 Loop 可以使用的接口。

未来可以逐步拆成：

```text
Decision Provider
Action Executor
Planner
Scheduler
```

今天不要拆得过细。

------

# 十、最小 Agent Loop 流程

伪代码如下：

```python
def run_stream(
    *,
    context: RunContext,
    state: AgentState,
    driver: AgentLoopDriver,
    policy: AgentLoopPolicy,
) -> Generator[str, None, None]:
    ensure_same_run_id(context, state)

    state.mark_running(now=context.clock.utc_now())

    executed_steps = 0
    consecutive_no_action = 0
    last_action_key: str | None = None
    consecutive_same_action = 0
    previous_observation: AgentObservation | None = None

    while True:
        context.raise_if_inactive()

        if executed_steps >= policy.max_steps:
            state.mark_failed(
                reason=StopReason.MAX_STEPS_REACHED,
                ...
            )
            return

        action = driver.decide(
            context=context,
            state=state,
            previous_observation=previous_observation,
        )

        if action is None:
            consecutive_no_action += 1

            if (
                consecutive_no_action
                >= policy.max_consecutive_no_action
            ):
                state.mark_failed(
                    reason=StopReason.NO_ACTION,
                    ...
                )
                return

            continue

        consecutive_no_action = 0

        if action.dedup_key == last_action_key:
            consecutive_same_action += 1
        else:
            last_action_key = action.dedup_key
            consecutive_same_action = 1

        if (
            consecutive_same_action
            > policy.max_consecutive_same_action
        ):
            state.mark_failed(
                reason=StopReason.REPEATED_ACTION,
                ...
            )
            return

        executed_steps += 1

        state.add_step(...)
        state.start_step(...)

        try:
            observation = yield from driver.execute(
                action=action,
                context=context,
            )
        except RunDeadlineExceededError:
            ...
            raise
        except RunCancelledError:
            ...
            raise
        except GeneratorExit:
            raise
        except Exception:
            ...
            raise

        if observation.outcome is ActionOutcome.CONTINUE:
            state.succeed_step(...)
            previous_observation = observation
            continue

        if observation.outcome is ActionOutcome.COMPLETED:
            state.succeed_step(...)
            state.mark_succeeded(...)
            return

        state.fail_step(...)
        state.mark_failed(...)
        return
```

这是逻辑结构示意，实际实现需要根据第 3 天现有方法签名调整。

------

# 十一、LocalAgent 如何最小落地

## 1. 当前状态

目前 `ChatService.stream_chat()` 大致负责：

```text
创建 RunContext
创建 AgentState
创建 legacy Step
开始 Run 和 Step
调用 AgentRouter
处理成功、失败、Deadline 和 Cancellation
```

今天之后应调整为：

```text
ChatService.stream_chat()
├── 创建 RunContext
├── 创建 AgentState
├── 创建 LegacyAgentLoopDriver
└── yield from AgentLoop.run_stream()
```

Agent Loop 接管：

- Run 启动；
- Step 创建和启动；
- AgentRouter 执行；
- Observation 处理；
- Run/Step 成功或失败；
- 最大步骤；
- 无动作；
- 重复动作；
- Deadline 与 Cancellation 出口。

这样 `ChatService` 不再亲自维护大量状态生命周期。

------

## 2. 兼容 Driver

当前可以建立：

```text
LegacyAgentRouterLoopDriver
```

其第一次 `decide()` 返回：

```text
AgentAction(
    step_id="legacy-agent-router",
    name="Legacy AgentRouter execution",
    action_type="execute_legacy_agent_router",
    dedup_key="legacy-agent-router",
)
```

其 `execute()`：

```text
调用 AgentRouter.chat_stream()
→ 原样 yield 现有文本和 [[ORCH]]
→ 聚合最终文本
→ 返回 AgentObservation(COMPLETED)
```

当前只会执行一次，因此：

- 最大步骤逻辑主要通过 Fake Driver 测试；
- 无动作逻辑主要通过 Fake Driver 测试；
- 重复动作逻辑主要通过 Fake Driver 测试。

不要为了展示多轮循环而强迫现有 AgentRouter 重复执行。

------

## 3. 为模型轻重路由预留边界

今天 Agent Loop 不应出现：

```python
if task_is_simple:
    use_local_model()
else:
    use_remote_model()
```

正确设计应是：

```text
Agent Loop
→ 接收 AgentAction
→ Driver / 后续 ModelSelectionPolicy 决定具体模型需求
→ Executor 执行模型
```

未来 `AgentAction` 可以携带：

```text
model_requirement
model_profile_hint
```

但今天不需要加入正式字段。

必须保证 Loop 只理解：

```text
Action
Observation
State
Termination
```

不理解具体 `qwen`、`deepseek`、本地或远程模型配置。

------

# 十二、本次架构方案

## 改造目标

建立一个最小、可测试的 Agent Loop，使：

```text
RunContext
+ AgentState
+ Legacy AgentRouter
```

通过统一循环执行，并支持：

- 最大步骤；
- 无动作检测；
- 重复动作检测；
- 成功、失败、Deadline 和 Cancellation 终止；
- 流式输出兼容。

## 预计新增文件

建议：

```text
core/runtime/agent_loop.py
tests/test_agent_loop.py
docs/learning/stage2/day04_agent_loop_result.md
```

可能还需要：

```text
core/runtime/legacy_agent_loop_driver.py
```

如果文件内容较少，也可以把兼容 Driver 放在合适的现有模块中，不要制造大量空文件。

## 预计修改文件

```text
core/runtime/__init__.py
core/chat_service.py
```

原则上不需要大规模修改：

```text
core/agent_router.py
```

如果需要调整调用签名，必须说明原因。

## 兼容要求

必须保持：

- `/api/chat` 请求体不变；
- StreamingResponse 行为不变；
- 普通文本 Chunk 不变；
- `[[ORCH]]` 格式不变；
- Memory Schema 不变；
- 当前 AgentRouter 业务行为不变；
- 本地和远程模型行为不变；
- Tool、RAG 和多 Agent 编排功能不变。

------

# 十三、测试方案

## 1. Policy 校验

测试：

- `max_steps <= 0` 被拒绝；
- `max_consecutive_no_action <= 0` 被拒绝；
- `max_consecutive_same_action <= 0` 被拒绝；
- 布尔值不能当作整数配置；
- 非整数配置被拒绝。

## 2. 正常完成

Fake Driver：

```text
返回一个 Action
→ execute 返回 COMPLETED
```

验证：

- Run 成功；
- Step 成功；
- StopReason 为 `COMPLETED`；
- 输出文本不变；
- active steps 为空。

## 3. CONTINUE

Fake Driver：

```text
Action 1 → CONTINUE
Action 2 → COMPLETED
```

验证：

- 两个 Step 均成功；
- 最后 Run 成功；
- 前一个 Observation 传入下一次决策；
- `max_steps=2` 时允许执行两个动作。

## 4. 最大步骤

Fake Driver 始终返回不同 Action，Observation 始终 `CONTINUE`。

验证：

```text
超过 max_steps 前停止
Run FAILED
StopReason MAX_STEPS_REACHED
没有 active Step
```

## 5. 无动作

Fake Driver 返回 `None`。

验证：

```text
达到阈值
Run FAILED
StopReason NO_ACTION
没有虚假 Step
```

## 6. 重复动作

Fake Driver 连续返回相同 `dedup_key`。

验证：

- 达到允许次数后停止；
- 超限动作不被执行；
- StopReason 为 `REPEATED_ACTION`；
- 之前已完成的 Step 保持成功。

## 7. Deadline

执行阶段抛出 `RunDeadlineExceededError`。

验证：

- 当前 Step 失败；
- Run 失败；
- StopReason 为 `DEADLINE_EXCEEDED`；
- 原异常按当前既有策略继续传播。

## 8. Cancellation

执行阶段抛出 `RunCancelledError`。

验证：

- 当前 Step 取消；
- Run 取消；
- StopReason 使用当前兼容映射；
- 原异常继续传播。

## 9. 未知异常

验证：

- 当前 Step 失败；
- Run 失败；
- AgentState 中不出现原始异常文本；
- 原异常继续向上传播；
- 日志记录异常。

## 10. Generator close

验证：

- 不执行成功收尾；
- 不吞掉 `GeneratorExit`；
- 不伪造客户端断开；
- 不修改现有第 3 天约定。

## 11. Legacy Driver 集成

使用 Fake Router：

- 现有流式文本完整输出；
- `[[ORCH]]` 内容不变；
- 最终状态成功；
- 只执行一次 legacy action；
- 同一个 RunContext 被传入 Router。

------

# 十四、Codex 实操提示词

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
- AgentRouter 当前同时承担 Router、Planner、Model、Tool、RAG、Memory、多 Agent 编排和事件输出；
- 当前采用逐步包装遗留实现的方式，不直接大规模拆分 AgentRouter。

### 第 2 天：RunContext

已经实现：

- `RunContext`
- `run_id`
- `session_id`
- `trace_id`
- UTC 和 monotonic Deadline
- `CancellationSource`
- `CancellationToken`
- `create_run_context()`
- ChatService 创建 Context 并传给 AgentRouter
- `session_id` 暂时使用 `legacy-default`

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
- `legacy-agent-router` 兼容 Step
- 成功、失败、Deadline、Cancellation 映射
- 未知异常使用安全固定摘要
- `BLOCKED` Step 语义
- Generator 提前关闭不执行成功收尾

目前 AgentState 由 `ChatService.stream_chat()` 在单次 Generator 生命周期内创建和修改。

本次任务是：

“阶段二第 4 天：Agent Loop，包括初始化、决策、执行、观察、状态更新、终止、最大步骤、无动作和重复动作检测。”

## 二、固定工作流

严格执行：

第一步：阅读项目结构和相关代码
第二步：总结现状和问题
第三步：给出最小改造方案
第四步：实施修改
第五步：补充或更新测试
第六步：运行相关测试和检查
第七步：输出结果信息文档

不得跳过分析直接整体重写。

## 三、本次目标

建立最小、明确、可测试的 Agent Loop，使现有：

```text
RunContext
+ AgentState
+ AgentRouter.chat_stream()
```

通过统一循环执行。

Agent Loop 至少需要覆盖：

- 初始化；
- 决策；
- 动作执行；
- Observation；
- AgentState 更新；
- 正常终止；
- 最大步骤终止；
- 连续无动作终止；
- 重复动作终止；
- Deadline；
- Cancellation；
- 未知异常；
- Generator 提前关闭。

本次不得拆分 AgentRouter 内部的 Router、Tool、RAG、Model 或多 Agent 编排逻辑。当前整个 AgentRouter 仍作为一个兼容 Action 执行。

## 四、修改前必须检查

请检查真实代码，至少包括：

- `core/runtime/context.py`
- `core/runtime/cancellation.py`
- `core/runtime/state.py`
- `core/runtime/__init__.py`
- `core/chat_service.py`
- `core/agent_router.py`
- `server.py`
- `tests/test_runtime_context.py`
- `tests/test_agent_state.py`
- 第 2、3 天结果文档
- 所有 `ChatService.stream_chat()` 调用方
- 所有 `AgentRouter.chat_stream()` 调用方
- 现有 logger 和异常处理
- 当前 Generator 关闭行为

必须根据真实方法签名和状态方法实现调整设计，不能照抄提示词中的示例。

## 五、核心概念

建议新增：

```text
core/runtime/agent_loop.py
```

根据项目风格实现以下最小概念。

### 1. AgentAction

至少表达：

- `step_id`
- `name`
- `action_type`
- `dedup_key`

所有字符串必须拒绝空值。

`dedup_key` 用于重复动作检测，不得包含：

- 完整用户输入；
- Prompt；
- 文件路径；
- API Key；
- Tool 原始参数；
- 公司信息；
- 其他敏感数据。

不要在本次建立完整 Action Fingerprint 系统。

### 2. AgentObservation

至少表达：

- 动作结果类型；
- 是否继续；
- 最终输出；
- 安全错误码；
- 安全错误摘要。

可以定义最小：

```text
CONTINUE
COMPLETED
FAILED
```

不得保存异常对象、完整 traceback 或未经安全映射的 `str(exc)`。

### 3. AgentLoopPolicy

至少包括：

- `max_steps`
- `max_consecutive_no_action`
- `max_consecutive_same_action`

要求：

- 必须是正整数；
- 拒绝 `bool`；
- 拒绝非整数；
- 使用合理默认值；
- 不读取无约束全局变量。

建议默认值可根据项目情况确定，例如：

```text
max_steps = 8
max_consecutive_no_action = 1
max_consecutive_same_action = 2
```

在结果文档中说明默认值及其权衡。

### 4. Driver 或等价兼容接口

可以定义最小 AgentLoopDriver Protocol，提供：

- `decide(...) -> AgentAction | None`
- `execute(...) -> Generator[str, None, AgentObservation]`

也可以采用等价的最小抽象。

不要为了展示架构拆成大量接口。

当前需要实现一个遗留兼容 Driver，将：

```text
AgentRouter.chat_stream()
```

包装成一个 Action。

## 六、Agent Loop 流程要求

### 1. 初始化

Agent Loop 启动时：

- 校验 `RunContext.run_id == AgentState.run_id`；
- 拒绝已经终止的 AgentState；
- 将 Run 从 `CREATED` 进入 `RUNNING`；
- 初始化循环局部计数器；
- 检查 Cancellation 和 Deadline。

今天不要把循环计数器加入 AgentState Schema，也不要升级 `schema_version`，除非真实实现证明无法避免；如必须修改 Schema，先停止实现并在聊天中说明原因。

### 2. 决策

每轮调用 Driver 的决策入口。

返回：

- `AgentAction`：继续执行；
- `None`：本轮无动作。

决策阶段不得直接执行 Tool、Model、RAG 或 AgentRouter。

### 3. 无动作检测

连续无动作次数达到：

```text
max_consecutive_no_action
```

后：

- Run → `FAILED`
- StopReason → `NO_ACTION`
- 不创建虚假 Step
- 不继续空转

### 4. 重复动作检测

通过安全 `dedup_key` 比较连续动作。

建议语义：

- 第一次出现计数 1；
- 相同动作继续出现则递增；
- 当计数大于 `max_consecutive_same_action` 时，在执行该超限动作前终止。

终止时：

- Run → `FAILED`
- StopReason → `REPEATED_ACTION`
- 超限 Action 不得执行
- 不创建对应 Step

如果采用不同但合理的阈值语义，必须在文档和测试中精确定义。

### 5. 最大步骤

一个非空 Action 被接受并准备执行时，计为一个 Step。

计数必须在执行前增加，使失败动作也消耗 Step。

达到 `max_steps` 后，如果 Observation 仍要求继续，下一轮在执行新 Action 前终止：

- Run → `FAILED`
- StopReason → `MAX_STEPS_REACHED`

不得执行第 `max_steps + 1` 个 Action。

### 6. Step 生命周期

对每个被接受的 Action：

- 创建对应 StepState；
- 加入 AgentState；
- Step → `RUNNING`；
- 加入 `active_step_ids`；
- 执行结束后根据 Observation 更新。

不要重复使用相同 `step_id` 创建多个 Step。Fake Driver 测试中的多轮 Action 必须提供唯一 Step ID。

### 7. Observation 映射

#### CONTINUE

- 当前 Step → `SUCCEEDED`
- Run 保持 `RUNNING`
- 保存为下一轮决策的 previous observation
- 继续循环

#### COMPLETED

- 当前 Step → `SUCCEEDED`
- Run → `SUCCEEDED`
- StopReason → `COMPLETED`
- 保存 final output
- 结束循环

#### FAILED

- 当前 Step → `FAILED`
- Run → `FAILED`
- 使用安全错误摘要
- 默认 StopReason 可使用 `UNHANDLED_ERROR`
- 结束循环

### 8. Deadline

捕获 `RunDeadlineExceededError` 时：

- 当前 active Step（如存在）→ `FAILED`
- Run → `FAILED`
- StopReason → `DEADLINE_EXCEEDED`
- 使用固定安全错误摘要
- 按现有 API 错误边界需求决定是否继续抛出；必须与第 3 天行为保持一致
- 不得转成成功 Observation

### 9. Cancellation

捕获 `RunCancelledError` 时：

- 当前 active Step（如存在）→ `CANCELLED`
- Run → `CANCELLED`
- 当前兼容 StopReason 可继续为 `USER_CANCELLED`
- 原异常传播行为保持与第 3 天一致
- 不伪造客户端断开

真实取消来源区分属于第 12 天。

### 10. 未知异常

未知异常时：

- 使用现有 logger 记录原始异常；
- AgentState 只保存：
  - `error_code = "UNHANDLED_ERROR"`
  - `error_message = "Agent execution failed"`
- 当前 Step → `FAILED`
- Run → `FAILED`
- StopReason → `UNHANDLED_ERROR`
- 必须继续向上抛出原异常；
- 不得保存通用 `str(exc)`。

### 11. GeneratorExit

必须：

- 不执行正常成功收尾；
- 不吞掉 `GeneratorExit`；
- 不擅自标记 `CLIENT_DISCONNECTED`；
- 不建设状态仓库、cancel API 或完整断开传播；
- 保持第 3 天已知限制。

## 七、Legacy AgentRouter 兼容

当前不要把 AgentRouter 内部操作拆成多个 Action。

实现一个兼容 Driver 或等价适配器：

### decide()

首次调用返回：

```text
step_id = "legacy-agent-router"
name = "Legacy AgentRouter execution"
action_type = "execute_legacy_agent_router"
dedup_key = "legacy-agent-router"
```

执行完成后应通过 Observation 结束整个 Run，不要再次调用 AgentRouter。

### execute()

- 调用现有 `AgentRouter.chat_stream()`；
- 原样 yield 所有普通文本；
- 原样 yield 所有 `[[ORCH]]` 内容；
- 使用同一个 RunContext；
- 聚合现有最终输出；
- 正常结束后返回 `COMPLETED` Observation。

不得修改现有文本流格式。

## 八、ChatService 调整

将当前由 `ChatService.stream_chat()` 直接维护的：

- Run 启动；
- legacy Step 创建和启动；
- 成功更新；
- Deadline 更新；
- Cancellation 更新；
- 普通异常更新；

迁移到 Agent Loop。

调整后 ChatService 主要负责：

1. 创建 RunContext 与 CancellationSource；
2. 创建相同 `run_id` 的 AgentState；
3. 创建 Legacy Driver；
4. 调用 `yield from agent_loop.run_stream(...)`。

不要在 ChatService 上保存共享 `_last_state`。

AgentState 仍只存在于单次 Generator 生命周期。

## 九、模型轻重路由兼容要求

本次不实现本地/远程模型动态选择。

Agent Loop 不得：

- 判断任务复杂度；
- 直接选择本地或远程模型；
- 引用具体 qwen、deepseek 或 Provider 配置；
- 实现 ModelSelectionPolicy；
- 实现 Fallback。

Loop 只理解：

- Action；
- Observation；
- AgentState；
- RunContext；
- 终止策略。

未来模型选择由 Driver、Planner 或 ModelSelectionPolicy 提供，不能把模型路由硬编码进 Loop。

## 十、状态机边界

本次 Agent Loop 可以继续调用 AgentState 受约束 mutation 方法。

不得实现：

- Runtime Event；
- 状态转移表；
- 通用 transition(event)；
- Guard 系统；
- 状态机类。

这些属于第 5 天。

## 十一、测试要求

继续使用标准库 `unittest`。

建议新增：

```text
tests/test_agent_loop.py
```

至少覆盖：

### Policy

1. 合法默认 Policy；
2. 零、负数被拒绝；
3. bool 被拒绝；
4. 非整数被拒绝。

### 正常执行

1. 单个 Action 返回 COMPLETED；
2. 输出 Chunk 保持顺序；
3. Step 成功；
4. Run 成功；
5. StopReason 为 COMPLETED；
6. active steps 清空。

### 多轮 CONTINUE

1. 第一 Action 返回 CONTINUE；
2. 第二 Action 返回 COMPLETED；
3. 两个 Step 都成功；
4. previous observation 传入下一轮；
5. Run 最终成功。

### 最大步骤

1. 最多只执行 `max_steps` 个 Action；
2. 第 `max_steps + 1` 个 Action 不执行；
3. Run FAILED；
4. StopReason 为 MAX_STEPS_REACHED；
5. 终态无 active Step。

### 无动作

1. 返回 None 时计数；
2. 达到阈值后 NO_ACTION；
3. 不创建虚假 Step；
4. 不发生无限空转。

### 重复动作

1. 相同 dedup_key 正确计数；
2. 超限 Action 不执行；
3. StopReason 为 REPEATED_ACTION；
4. 超限 Action 不创建 Step；
5. 不同 dedup_key 重置重复计数。

### 异常

1. Deadline 映射正确；
2. Cancellation 映射正确；
3. 未知异常映射正确；
4. 未知异常原文不进入 AgentState；
5. 原异常继续传播；
6. Generator close 不执行成功收尾。

### Legacy Driver

1. Fake Router 只调用一次；
2. 同一个 RunContext 传入 Router；
3. 普通文本不变；
4. `[[ORCH]]` 文本不变；
5. 最终 Run 和 legacy Step 成功。

不要启动：

- 真实本地模型；
- 远程模型；
- Chroma；
- PyQt6；
- FastAPI 服务；
- 真实数据库写入；
- 外部网络。

## 十二、代码质量

所有新增公共代码必须：

- 完整类型标注；
- 清晰 Docstring；
- 使用 timezone-aware UTC datetime；
- 避免 `Any`；
- 避免可变默认参数；
- 避免全局可变状态；
- 不使用 `print` 作为正式日志；
- 不使用 pickle；
- 不保存敏感数据；
- 不新增大型依赖；
- 不大规模拆分文件。

## 十三、执行检查

至少执行：

```text
python -m unittest \
  tests.test_runtime_context \
  tests.test_agent_state \
  tests.test_agent_loop \
  -v

python -m compileall core tests
```

根据实际测试位置调整命令，但必须覆盖前三天已有测试和本次测试。

## 十四、GitHub 工作流

本仓库通过 Codex 在用户明确指定的 GitHub 仓库中修改，允许：

- 创建本地 Commit；
- Push 到当前任务分支；
- 创建或更新本次任务的 PR。

必须遵守：

- 只能操作当前明确指定的仓库和任务分支；
- 不得操作无关仓库、无关分支；
- 不得擅自合并 PR；
- PR 中必须说明修改范围、测试命令、测试结果和已知风险；
- 不得上传公司环境产生的代码、配置、数据、日志、接口地址或其他内部信息；
- 不得在文档、Commit 或 PR 中包含 API Key、Token、密码或敏感路径。

## 十五、禁止事项

禁止：

- 大规模重写 AgentRouter；
- 实现完整 State Machine；
- 实现 Runtime Event；
- 实现 Planner、PlanStep；
- 实现 Scheduler 或 DAG；
- 实现并行执行；
- 实现 Budget；
- 实现 Retry；
- 实现模型轻重路由；
- 实现 Fallback 或熔断；
- 实现 Checkpoint 或 Resume；
- 新增状态数据库；
- 修改 Memory Schema；
- 新增 cancel API；
- 实现完整客户端断开传播；
- 修改 `/api/chat` 请求体；
- 修改普通文本流或 `[[ORCH]]` 协议；
- 引入 Tool Registry、Agent Skill、MCP、A2A 或 Sandbox；
- 把 AgentEvalOps 功能加入 LocalAgent；
- 擅自合并 PR；
- 修改无关模块。

## 十六、结果文档

必须创建：

```text
docs/learning/stage2/day04_agent_loop_result.md
```

文档结构：

# 阶段二第 4 天改造结果

## 1. 本次任务目标

## 2. 修改前现状

## 3. 发现的问题

## 4. 最终设计方案

## 5. 新增文件

## 6. 修改文件

## 7. 核心类、接口和数据结构

必须说明：

- AgentLoop
- AgentLoopPolicy
- AgentAction
- AgentObservation
- ActionOutcome
- Driver 或兼容适配器
- 循环局部计数器

## 8. 关键执行流程

必须覆盖：

- 初始化；
- 决策；
- 执行；
- 观察；
- 状态更新；
- CONTINUE；
- COMPLETED；
- 最大步骤；
- 无动作；
- 重复动作；
- Deadline；
- Cancellation；
- 未知异常；
- Generator close。

## 9. 与现有功能的兼容方式

## 10. 异常处理和边界情况

## 11. 测试内容

## 12. 实际执行的测试命令

## 13. 测试结果

## 14. 未完成事项

## 15. 已知风险

必须明确记录：

- 当前只有 legacy AgentRouter 兼容 Action；
- 当前真实主链通常只执行一轮；
- 最大步骤、无动作和重复动作主要通过 Fake Driver 验证；
- 尚未实现 Planner；
- 尚未实现 Scheduler；
- 尚未实现状态机；
- 尚未实现 Budget；
- 尚未实现模型轻重路由；
- AgentState 尚未持久化；
- Generator close 尚不能形成可靠终态；
- 真实取消来源尚未接入。

## 16. 设计权衡

## 17. 可用于面试的项目描述

不得声称已经实现：

- 完整多步规划 Agent；
- 完整状态机；
- Scheduler；
- Durable Execution；
- 轻重模型动态路由；
- 生产级客户端断开处理。

## 18. 需要带回 ChatGPT 审查的信息

必须包含：

- AgentLoop 真实文件和入口；
- AgentLoop 的所有者；
- ChatService 修改前后调用链；
- Action 和 Observation 最终结构；
- Policy 默认值；
- Step 计数时机；
- 无动作阈值语义；
- 重复动作阈值语义；
- dedup_key 安全策略；
- Legacy Driver 的真实实现；
- AgentRouter 是否只调用一次；
- CONTINUE 和 COMPLETED 映射；
- 最大步骤映射；
- 无动作映射；
- 重复动作映射；
- Deadline 和 Cancellation 映射；
- GeneratorExit 处理；
- 是否修改 AgentState Schema；
- 是否实现任何状态机内容；
- 模型轻重路由兼容方式；
- 新增和修改文件；
- 测试命令与结果；
- Commit / PR 信息；
- 未完成事项；
- 需要人工确认的问题；
- 后续建议，但不得直接实施第 5 天内容。

## 十七、聊天最终输出

完成后请输出：

结果文档路径：

本次新增文件：

本次修改文件：

AgentLoop 入口：

ChatService 修改后调用链：

Action 最终结构：

Observation 最终结构：

Policy 默认值：

Step 计数语义：

无动作阈值语义：

重复动作阈值语义：

Legacy Driver：

AgentRouter 调用次数：

测试命令：

测试是否通过：

是否修改 AgentState Schema：

是否修改 API、Memory Schema 或流式协议：

Commit：

PR：

需要人工确认的问题：

------

# 十五、Codex 结果审查重点

结果返回后，我会重点检查：

1. Agent Loop 是否真正拥有循环控制。
2. ChatService 是否不再直接维护大量状态生命周期。
3. Loop 是否没有硬编码本地或远程模型。
4. 决策阶段是否没有直接执行动作。
5. Step 是否在执行前计数。
6. 最大步骤是否不会多执行一次。
7. 无动作是否不会创建虚假 Step。
8. 重复动作是否在超限动作执行前终止。
9. `dedup_key` 是否不包含敏感信息。
10. CONTINUE 是否正确进入下一轮。
11. COMPLETED 是否正确结束 Run。
12. Deadline、Cancellation 和未知异常是否正确映射。
13. 原始异常是否只进入受控日志。
14. GeneratorExit 是否没有被吞掉。
15. 是否错误升级 AgentState Schema。
16. 是否提前实现状态机、Planner 或 Scheduler。
17. Fake Driver 测试是否真正覆盖多轮循环。
18. Legacy AgentRouter 是否仍只调用一次。
19. 现有流式输出是否完全兼容。
20. Commit 和 PR 是否只包含本次任务范围。

------

# 十六、面试高频问题

## 1. Agent Loop 的标准流程是什么？

> 初始化运行环境和状态，然后不断执行决策、动作执行、观察、状态更新和终止判断，直到成功、失败、取消、超时或触发循环保护条件。

## 2. Agent Loop 和 Planner 有什么区别？

> Planner 负责生成任务计划，Agent Loop 负责持续推进执行周期。Loop 可以调用 Planner，但不能把 Planner 和执行控制混为一体。

## 3. 最大步骤应该什么时候计数？

> Action 被接受并准备执行时计数，而不是成功后计数。否则失败动作可能无限执行却不消耗步骤限制。

## 4. 如何检测 Agent 陷入重复动作？

> 为每个 Action 生成稳定且不包含敏感信息的 `dedup_key`，比较连续动作键。当相同行动超过允许阈值时，在执行超限动作前终止 Run。

## 5. 无动作和任务完成有什么区别？

> 无动作表示 Agent 既没有产生可执行行为，也没有形成最终答案；任务完成则有明确的最终 Observation。无动作达到阈值应视为失败，而不是成功。

------

# 十七、当天验收清单

## 理论验收

-  理解 Agent Loop 六个阶段
-  区分 Agent、Loop、Planner、Scheduler 和状态机
-  理解 Action 与 Observation
-  理解最大步骤计数时机
-  理解无动作检测
-  理解重复动作检测
-  理解所有终止出口
-  理解流式 Generator 与 Agent Loop 的结合
-  理解 Loop 不应硬编码模型轻重路由

## 项目验收

-  已新增 AgentLoop
-  已新增 AgentLoopPolicy
-  已新增 AgentAction 和 AgentObservation
-  已建立 Legacy Driver
-  ChatService 已委托 Agent Loop
-  正常完成路径通过
-  CONTINUE 多轮路径通过
-  最大步骤保护通过
-  无动作保护通过
-  重复动作保护通过
-  Deadline 与 Cancellation 路径通过
-  Generator close 路径通过
-  现有流式协议保持兼容
-  未实现状态机或 Planner
-  测试全部通过
-  已生成第 4 天结果文档
-  已完成 ChatGPT 审查

## 阶段二进度

**第 4/25 天：理论与架构方案完成，等待 Codex 改造结果审查。**

下一天主题：**State Machine（状态机），包括状态、事件、转移、Guard、合法与非法状态转移，以及将状态修改统一收口。**