# 阶段二第 8 天：Scheduler（调度器）

**当前进度：第 8/25 天。**

前七天已经完成：

```text
Planner
→ 生成不可变 Plan 和 PlanStep

AgentState
→ 保存 Step 的实际运行状态

State Machine
→ 校验并执行状态转移

ModelSelectionPolicy
→ 根据能力需求与上下文选择模型
```

今天要补上中间这一层：

```text
Plan + AgentState
        ↓
Scheduler
        ↓
当前可以执行哪个 PlanStep
```

第 8 天只实现**最小串行 Scheduler**：

- 一次最多认领一个 Step；
- 不实现并行执行；
- 不实现优先级抢占；
- 不实现 DAG（有向无环图）环检测；
- 不实现分布式锁；
- 不直接选择模型。

------

# 一、当天目标

今天必须掌握并落地：

1. Planner 与 Scheduler 的职责边界；
2. Ready Step（就绪步骤）的定义；
3. `PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED`、`BLOCKED`、`SKIPPED` 对调度的影响；
4. Scheduler 如何联合读取 `Plan` 和 `AgentState`；
5. Step Claim（步骤认领）；
6. 防止同一个 Step 重复调度；
7. 串行调度约束；
8. 前置依赖完成后如何释放下游 Step；
9. 前置依赖失败后如何传播 `BLOCKED`；
10. Ready Step 的确定性顺序和基础公平性；
11. 无 Ready Step 时如何区分：
    - 正在等待；
    - 已经完成；
    - 存在无法推进的未决状态；
12. Scheduler 与 Model Selection Policy 的边界；
13. 增加至少 3 个高价值 Bad Case。

------

# 二、Planner 与 Scheduler 的边界

## 1. Planner

Planner 回答：

> 为完成任务，应该有哪些步骤，这些步骤之间有什么依赖？

例如：

```text
step-1：检索知识库
step-2：分析检索结果，依赖 step-1
step-3：生成最终答案，依赖 step-2
```

Planner 负责静态定义：

- Step ID；
- 标题和描述；
- `depends_on`；
- 完成条件；
- 能力需求；
- 推荐 Agent。

Planner 不关心：

- 当前 Step 是否已经运行；
- 哪个 Step 已失败；
- 下一秒应该执行哪个 Step；
- 当前是否有 Step 正在占用执行权。

------

## 2. Scheduler

Scheduler 回答：

> 根据静态 Plan 和当前 AgentState，现在可以认领哪个 Step？

它读取：

```text
Plan
├── Step 定义
└── depends_on

AgentState
├── StepStatus
└── active_step_ids
```

然后计算：

```text
Ready Steps
Blocked Steps
当前是否已有 Running Step
下一步可以认领哪个 Step
```

------

## 3. Scheduler 不执行 Step

Scheduler 只负责：

```text
找出 Step
→ 认领 Step
→ 将其转为 RUNNING
```

实际模型、Tool、RAG 或 Agent 调用由 Executor / AgentLoop 执行。

------

# 三、Plan 与 AgentState 如何联合使用

## Plan 提供静态信息

```text
step_id
depends_on
capability_requirements
preferred_agent
completion_criteria
```

## AgentState 提供动态信息

```text
PENDING
RUNNING
SUCCEEDED
FAILED
CANCELLED
BLOCKED
SKIPPED
```

不能只读取其中一个。

例如 Plan 中：

```text
step-2 depends_on step-1
```

但只有 AgentState 能告诉 Scheduler：

```text
step-1 当前是 SUCCEEDED 还是 RUNNING
```

------

# 四、Ready Step 的正式定义

一个 PlanStep 是 Ready Step，必须同时满足：

```text
1. Step 已经注册到 AgentState
2. StepState.status == PENDING
3. Step 不在 active_step_ids
4. RunStatus == RUNNING
5. depends_on 中所有前置 Step 都是 SUCCEEDED
6. 当前串行 Scheduler 没有其他 RUNNING Step
```

可以表示为：

```text
ready(step)
=
step.status == PENDING
AND all(dependency.status == SUCCEEDED)
AND no_running_step
```

------

## 不能只判断依赖“已经终止”

错误判断：

```text
依赖不是 RUNNING
→ 当前 Step Ready
```

因为依赖可能是：

```text
FAILED
CANCELLED
BLOCKED
SKIPPED
```

这些状态都不能满足当前版本的依赖。

第 8 天没有 Optional Dependency（可选依赖），因此：

```text
只有 SUCCEEDED 才算依赖满足
```

------

# 五、不同 StepStatus 对 Scheduler 的含义

| StepStatus  | 调度含义                                           |
| ----------- | -------------------------------------------------- |
| `PENDING`   | 尚未执行，可能在未来变成 Ready                     |
| `RUNNING`   | 已被认领，不能再次调度                             |
| `SUCCEEDED` | 依赖条件已满足，可以释放下游                       |
| `FAILED`    | 本 Step 不再执行，依赖它的下游应 BLOCKED           |
| `CANCELLED` | 本次 Run 不再完成该 Step，下游应 BLOCKED           |
| `BLOCKED`   | 确定无法执行，下游继续传播 BLOCKED                 |
| `SKIPPED`   | 本次明确不执行；当前无可选依赖语义，下游应 BLOCKED |

这里的 `SKIPPED` 语义需要特别说明：

> 当前 Plan 的依赖都是强依赖，所以前置 Step 被 SKIPPED 后，下游不能假定依赖已经满足。

未来如果增加：

```text
optional_depends_on
```

才可以调整。

------

# 六、Step 注册

Planner 产生的是 `PlanStep`，但 AgentState 必须有对应 `StepState`。

建议 Scheduler 提供准备入口：

```python
class SerialScheduler:
    def prepare(
        self,
        plan: Plan,
        state: AgentState,
        occurred_at: datetime,
    ) -> None:
        """将 Plan 中尚未注册的步骤注册为 PENDING。"""
```

## prepare 的规则

- Run 必须是 `RUNNING`；
- 通过 `AgentStateMachine.add_step()` 注册；
- 不直接修改 `state.steps`；
- Step ID 已存在时不能重复添加；
- 如果同 ID 的现有 Step 与 Plan 标题不一致，应明确报错；
- 不能把 Plan 的完整 description、Prompt 或敏感内容写进 AgentState；
- StepState 的 name 可以使用简短中文 `PlanStep.title`。

------

## prepare 是否幂等

推荐语义：

```text
同一 Plan 重复 prepare
+ 已有 Step ID、名称一致
→ 安全跳过
```

但：

```text
已有相同 Step ID
+ 名称或计划归属不一致
→ SchedulerPlanStateMismatchError
```

不能简单忽略所有重复 ID，否则可能把两个不同 Plan 错误绑定到同一 StepState。

------

# 七、Step Claim（步骤认领）

## 1. 为什么需要 Claim

如果 Scheduler 只是返回：

```text
step-1 可以执行
```

但没有立即更新状态，那么两个调用者可能同时读取：

```text
step-1 = PENDING
```

然后都将它加入执行队列。

结果：

- 模型调用两次；
- Tool 执行两次；
- 文件重复修改；
- 同一个 Step 收到两次成功事件。

因此 Ready 计算和状态认领必须形成一个受保护的操作。

------

## 2. Claim 的核心语义

```text
检查 Ready
→ 选择一个 Step
→ PENDING → RUNNING
→ 返回 StepClaim
```

只有状态转移成功，才能返回 Claim。

建议结构：

```python
@dataclass(frozen=True, slots=True)
class StepClaim:
    """表示 Scheduler 已成功认领一个 PlanStep。"""

    plan_id: str
    plan_version: int
    step_id: str
    claimed_at: datetime
    capability_requirements: TaskCapabilityRequirements
    preferred_agent: str | None
```

不需要保存：

- 模型对象；
- Prompt；
- 用户输入；
- Tool 参数；
- API Key；
- Runtime 错误对象。

------

## 3. Claim 必须经过 State Machine

Scheduler 不能直接：

```python
step.status = StepStatus.RUNNING
state.active_step_ids.add(step_id)
```

应调用：

```text
AgentStateMachine
+ StepEventType.STARTED
```

这样继续复用第 5 天：

- 合法转移；
- active Step 不变量；
- 原子更新；
- 终态保护。

------

# 八、串行 Scheduler

第 8 天只允许：

```text
同一 Run 最多一个 RUNNING Plan Step
```

因此 `claim_next()` 前必须检查：

```text
是否已经存在 RUNNING Plan Step
```

存在时：

```text
→ 不再认领新 Step
```

建议返回 Scheduler Snapshot（调度快照），而不是将它误判成无任务。

------

# 九、推荐的 Scheduler 接口

```python
class SerialScheduler:
    """根据 Plan 和 AgentState 执行确定性的串行调度。"""

    def prepare(
        self,
        plan: Plan,
        state: AgentState,
        occurred_at: datetime,
    ) -> None:
        ...

    def evaluate(
        self,
        plan: Plan,
        state: AgentState,
    ) -> SchedulerSnapshot:
        ...

    def claim_next(
        self,
        plan: Plan,
        state: AgentState,
        occurred_at: datetime,
    ) -> StepClaim | None:
        ...
```

------

## SchedulerSnapshot

```python
@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    """描述当前 Plan 的调度视图，不保存执行状态副本。"""

    ready_step_ids: tuple[str, ...]
    running_step_ids: tuple[str, ...]
    pending_step_ids: tuple[str, ...]
    blocked_step_ids: tuple[str, ...]
    terminal_step_ids: tuple[str, ...]
    is_complete: bool
    is_waiting: bool
    has_unresolved_pending: bool
```

这个结构是计算结果，不应写入 AgentState。

------

# 十、确定性顺序与调度公平性

如果同时有多个 Ready Step：

```text
step-a
step-b
step-c
```

第 8 天按 `Plan.steps` 中的顺序选择第一个。

```text
Plan tuple order
→ 稳定 Ready 顺序
→ claim 第一个
```

不能使用：

```python
next(iter(set(ready_steps)))
```

因为 Set 的顺序不应该成为调度语义。

------

## 当前的公平性

串行、有限、不可变 Plan 下：

```text
每次选择 Plan 中最靠前的 Ready Step
```

可以保证确定性，也不会发生永久饥饿，因为已认领的 Step 最终会进入终态。

今天不实现：

- Priority；
- Aging（老化）；
- 时间片；
- 抢占；
- 动态插队；
- 多队列公平。

------

# 十一、依赖完成后的释放

假设：

```text
step-2 depends_on step-1
```

初始：

```text
step-1 = PENDING
step-2 = PENDING
```

Scheduler：

```text
claim step-1
→ step-1 = RUNNING
```

执行完成：

```text
AgentLoop / Executor
→ STEP_SUCCEEDED
→ step-1 = SUCCEEDED
```

下次 Scheduler evaluate：

```text
step-2 的全部依赖为 SUCCEEDED
→ step-2 变为 Ready
```

注意：

> Scheduler 不需要显式将 step-2 从 WAITING 改成 READY。

因为当前没有持久化的 `READY` 状态。

`READY` 是由：

```text
Plan + AgentState
```

动态计算出来的派生状态。

------

# 十二、为什么不新增 READY 状态

如果将 READY 写入 AgentState：

```text
PENDING
→ READY
→ RUNNING
```

就需要额外处理：

- 依赖重新变化；
- 取消；
- Blocked 传播；
- READY 是否过期；
- 多线程 Claim；
- 状态转移增加。

当前更简单可靠的设计是：

```text
PENDING
+ 依赖全部成功
= 计算上的 Ready
```

真正 Claim 时直接：

```text
PENDING → RUNNING
```

------

# 十三、BLOCKED 传播

## 1. 直接传播

Plan：

```text
step-b depends_on step-a
```

如果：

```text
step-a = FAILED
```

则：

```text
step-b = BLOCKED
```

------

## 2. 传递传播

Plan：

```text
step-a → step-b → step-c
```

如果：

```text
step-a = FAILED
```

第一次传播：

```text
step-b = BLOCKED
```

第二次传播：

```text
step-c 依赖 step-b
step-b = BLOCKED
→ step-c = BLOCKED
```

所以 Scheduler 需要执行到稳定状态：

```text
重复扫描
→ 直到本轮没有新增 BLOCKED
```

这叫 Fixed Point（不动点）传播。

------

## 3. 哪些依赖状态会阻断下游

当前强依赖规则：

```text
FAILED
CANCELLED
BLOCKED
SKIPPED
```

都会导致下游 `BLOCKED`。

以下状态不会立刻 Block：

```text
PENDING
RUNNING
```

因为这些依赖未来仍可能成功。

------

## 4. BLOCKED 更新必须经过 State Machine

使用：

```text
StepEventType.BLOCKED
```

Scheduler 不能直接修改 StepState。

如果现有 `StepState` 支持安全错误摘要，可以使用固定内容：

```text
error_code = "DEPENDENCY_NOT_SUCCESSFUL"
error_message = "前置步骤未成功，当前步骤无法执行"
```

如果现有状态机不允许 `BLOCKED` 携带错误字段，则不要为了保存依赖详情修改 AgentState Schema。

可以在 Scheduler 返回值或安全日志中记录：

```text
blocked step_id
blocking dependency step_id
dependency status
```

但不能包含用户正文。

------

# 十四、无 Ready Step 不等于 Plan 已完成

这是 Scheduler 中很重要的判断。

## 场景 1：有 Step 正在运行

```text
ready = empty
running = step-1
```

含义：

```text
Scheduler 正在等待 step-1 完成
```

不能认为完成，也不能标记失败。

------

## 场景 2：所有 Step 都成功

```text
所有 Step = SUCCEEDED
```

含义：

```text
Plan 已完成
```

Scheduler 可以返回：

```text
is_complete = True
```

但是否将 Run 标记为 `SUCCEEDED`，仍由 AgentLoop / Runtime 决定。

------

## 场景 3：存在 Failed / Blocked

```text
step-a = FAILED
step-b = BLOCKED
```

含义：

```text
Plan 无法全部成功
```

Scheduler 可以返回失败视图，但不直接结束 Run。

------

## 场景 4：全部 PENDING，却没有 Ready

例如：

```text
step-a depends_on step-b
step-b depends_on step-a
```

今天不做 DAG 环检测，所以 Scheduler 只能识别：

```text
无 RUNNING
无 READY
仍有 PENDING
```

应返回：

```text
has_unresolved_pending = True
```

不能：

- 把 Plan 标记为完成；
- 自动把所有 Step 标记为 BLOCKED；
- 宣称检测到了 Cycle（环）。

环检测属于第 9 天。

------

# 十五、防止重复调度的并发边界

即使今天是串行 Scheduler，也应防止同一进程内的两个调用者同时 Claim。

建议：

```python
class SerialScheduler:
    def __init__(self, state_machine: AgentStateMachine) -> None:
        self._state_machine = state_machine
        self._claim_lock = threading.Lock()
```

`claim_next()` 内：

```text
获取 Lock
→ 传播 BLOCKED
→ 检查 RUNNING
→ 计算 Ready
→ State Machine STARTED
→ 返回 Claim
→ 释放 Lock
```

要求：

- Scheduler 实例按 Run 创建；
- 不使用全局 Singleton（单例）；
- Lock 不写入 AgentState；
- Lock 不参与序列化。

------

## 今天的 Claim 不能解决什么

它只能解决：

```text
同一进程
+ 同一个 Scheduler 实例
```

的重复认领。

不能解决：

- 两个进程同时调度；
- 两台机器同时调度；
- Scheduler 重启后的 Claim 恢复；
- 数据库级分布式锁；
- Lease（租约）过期。

这些属于 Durable Execution（持久化执行）和后续生产化阶段。

------

# 十六、Scheduler 与模型选择的边界

Scheduler 可以读取：

```text
PlanStep.capability_requirements
PlanStep.preferred_agent
```

并随 `StepClaim` 返回给执行层。

但 Scheduler 不能调用：

```text
ModelSelectionPolicy.select()
```

也不能保存：

```text
selected_profile
deepseek
qwen
LOCAL_FAST
REMOTE_ADVANCED
```

正确链路：

```text
Scheduler
→ Claim PlanStep
→ Executor 读取 capability_requirements
→ ContextBuilder
→ ModelSelectionPolicy
→ ModelResolver
```

------

# 十七、LocalAgent 最小落地策略

建议新增：

```text
core/runtime/scheduler.py
tests/test_scheduler.py
docs/learning/stage2/result/day08_scheduler_result.md
```

预计修改：

```text
core/runtime/__init__.py
```

可能根据实际结构修改：

```text
core/runtime/planning.py
core/runtime/state_machine.py
tests/test_planning.py
```

原则上不应修改：

```text
core/agent_router.py
模型选择核心规则
API
Memory Schema
AgentState Schema
流式协议
[[ORCH]]
```

------

## 最小真实接入

Codex 必须先检查当前 Plan 的所有权和 AgentLoop 的 Step 生命周期。

优先顺序：

### 方案 A：存在干净的 Plan 执行扩展点

将 Scheduler 作为可选组件注入 Runtime：

```text
Plan
→ Scheduler.prepare()
→ Scheduler.claim_next()
→ 执行层执行被认领 Step
```

### 方案 B：当前 AgentLoop 的 Step 生命周期与 Scheduler Claim 会重复

如果 AgentLoop 仍然自行：

```text
add_step
→ STEP_STARTED
```

则不要强行接入默认生产链路，否则会出现同一个 Step 被启动两次。

这种情况下：

- 完整实现 Scheduler；
- 使用真实 `Plan`、`AgentState`、`AgentStateMachine` 做集成测试；
- 增加一个独立、可选的串行调度适配层；
- 结果文档明确当前默认 Legacy AgentLoop 尚未消费 Scheduler；
- 不为了“接入”而制造双重生命周期。

第 8 天的重点是：

> Scheduler 语义正确，而不是强行重写 AgentLoop。

------

# 十八、第 8 天高价值 Bad Case

## Bad Case 1：Ready 检查和 Step Claim 分离，导致重复执行

- **类型：假设构造**

### 触发条件

两个线程同时执行：

```text
evaluate()
→ 都看到 step-1 = PENDING
→ 都将 step-1 发送给 Executor
```

### 故障表现

- 同一个模型调用两次；
- Tool 副作用重复；
- Step 收到两次完成事件；
- 第二次状态转移因终态保护失败，但副作用已经发生。

### 根因分析

Ready 计算和 `PENDING → RUNNING` 不在同一原子边界。

### 修复方案

- `claim_next()` 内部完成 Ready 计算和 State Machine STARTED；
- 使用单 Run Scheduler Lock；
- 只有状态转移成功才返回 `StepClaim`。

### 回归测试

两个线程使用 Barrier 同时调用 `claim_next()`：

- 只有一个获得 `StepClaim`；
- 另一个返回 `None` 或 Busy；
- Step 最终只有一个 `RUNNING`；
- `active_step_ids` 只包含一次；
- Executor 调用次数为 1。

### 对应知识点

- Check-then-act Race（先检查后执行竞态）；
- 原子认领；
- 至少一次执行；
- 幂等与副作用。

### 面试表达

> Scheduler 不能先计算 Ready 再由外部修改状态，因为多个调度者可能同时看到 PENDING。我把 Ready 检查和 PENDING 到 RUNNING 的状态转移收口到 claim_next，并使用 Run 级锁保证进程内只返回一个 Claim。

------

## Bad Case 2：只传播一层 BLOCKED，深层步骤永久 PENDING

- **类型：假设构造**

### 触发条件

Plan：

```text
step-a → step-b → step-c
```

`step-a` 失败，Scheduler 只扫描一次：

```text
step-b → BLOCKED
```

但没有继续处理：

```text
step-c depends_on step-b
```

### 故障表现

`step-c` 永久保持 `PENDING`：

- 没有 Ready Step；
- 没有 Running Step；
- Plan 无法结束；
- Runtime 可能误判为死锁或继续空轮询。

### 根因分析

Blocked 传播只做单轮扫描，没有计算传递闭包。

### 修复方案

重复传播直到本轮没有新增 `BLOCKED`。

### 回归测试

构造三层或四层依赖链：

- 根 Step `FAILED`；
- 所有下游最终为 `BLOCKED`；
- 不剩下错误的 `PENDING`；
- 每个 Step 只发生一次 BLOCKED 转移。

### 对应知识点

- Fixed Point；
- 依赖传播；
- 状态收敛；
- 图上的失败传播。

### 面试表达

> 依赖失败不能只阻断直接子节点，否则深层下游会永久停在 PENDING。我通过不动点传播持续扫描，直到本轮没有新增 BLOCKED，保证失败沿依赖链完全收敛。

------

## Bad Case 3：没有 Ready Step，就错误判定 Plan 完成

- **类型：假设构造**

### 触发条件

当前：

```text
step-a = RUNNING
step-b = PENDING，依赖 step-a
ready_steps = empty
```

Scheduler 将：

```text
ready_steps 为空
```

等价成：

```text
Plan 完成
```

### 故障表现

- Run 提前成功；
- `step-a` 仍处于 RUNNING；
- `step-b` 永远没有执行；
- 违反 State Machine active Step Guard。

### 根因分析

混淆了：

```text
当前没有可调度 Step
计划已经全部完成
```

### 修复方案

SchedulerSnapshot 分别表达：

- `is_waiting`
- `is_complete`
- `has_unresolved_pending`
- `running_step_ids`

只有全部 Step 达到合法成功终态，才能判定完成。

### 回归测试

- 有 Running、无 Ready：`is_waiting=True`，`is_complete=False`；
- 全部 Succeeded：`is_complete=True`；
- 有 Pending、无 Running、无 Ready：`has_unresolved_pending=True`；
- 不修改 Run 终态。

### 对应知识点

- 派生状态；
- Liveness（活性）；
- Completion Detection（完成检测）；
- 父子生命周期。

### 面试表达

> Ready 队列为空不代表任务完成，可能只是某个依赖仍在运行。我将 waiting、complete 和 unresolved pending 分开建模，避免 Scheduler 在没有 Ready Step 时提前结束 Run。

------

## Bad Case 4：使用 Set 选择 Ready Step，调度顺序不稳定

- **类型：假设构造**

### 触发条件

实现使用：

```python
ready_steps = set(...)
selected = next(iter(ready_steps))
```

### 故障表现

同一 Plan 在不同运行或 Python 环境中可能先执行不同 Step：

- 测试偶发失败；
- 日志难以复现；
- 不同 Step 的 Tool 副作用顺序变化；
- 用户结果不稳定。

### 根因分析

将无序集合的遍历顺序当作调度策略。

### 修复方案

按：

```text
Plan.steps 原始顺序
→ step_id 稳定决胜
```

返回 Ready Step。

### 回归测试

相同 Plan 和 State 连续 evaluate 多次：

- Ready 顺序完全一致；
- claim 顺序与 Plan 顺序一致；
- 不依赖 Hash 顺序。

### 对应知识点

- Deterministic Scheduling（确定性调度）；
- 可复现性；
- 公平性基础；
- 稳定排序。

### 面试表达

> 第一版串行 Scheduler 采用 Plan 顺序作为稳定公平策略，不使用 Set 遍历顺序。这样相同输入始终产生相同 Claim 顺序，便于回归和故障复现。

------

# 十九、测试方案

## Plan 与 State 绑定

1. 合法 Plan Step 注册；
2. 重复 `prepare()` 保持幂等；
3. 相同 ID、不同名称明确失败；
4. Run 非 `RUNNING` 时不能注册；
5. 不直接修改 `state.steps`；
6. AgentState Schema 不变。

## Ready Step

1. 无依赖 PENDING Step 为 Ready；
2. 所有依赖 SUCCEEDED 时 Ready；
3. 依赖 PENDING 时不 Ready；
4. 依赖 RUNNING 时不 Ready；
5. 依赖 FAILED 时下游 BLOCKED；
6. 依赖 CANCELLED 时下游 BLOCKED；
7. 依赖 BLOCKED 时下游 BLOCKED；
8. 依赖 SKIPPED 时下游 BLOCKED；
9. RUNNING Step 不能再次 Ready；
10. 终态 Step 不能 Ready。

## Claim

1. `claim_next()` 将 PENDING 原子转为 RUNNING；
2. Claim 返回正确 Plan / Step 信息；
3. 已有 RUNNING Step 时不认领新 Step；
4. 同一个 Step 不会重复 Claim；
5. 两线程竞争只有一个成功；
6. Claim 通过 State Machine；
7. Claim 失败后状态不变。

## BLOCKED 传播

1. 一层依赖失败；
2. 多层传递传播；
3. 多个依赖中一个失败；
4. 依赖 PENDING 不传播；
5. 依赖 RUNNING 不传播；
6. BLOCKED Step 不重复转移；
7. 传播结果稳定。

## 完成和等待判断

1. Running 存在时 `is_waiting=True`；
2. 全部成功时 `is_complete=True`；
3. Failed / Blocked 不算成功完成；
4. PENDING 且无 Ready 时为 unresolved；
5. Scheduler 不直接修改 RunStatus。

## 确定性与公平

1. Ready 顺序遵循 Plan 顺序；
2. 多次 evaluate 结果一致；
3. Step 成功后按稳定顺序释放下游；
4. 不使用无序集合决定 Claim。

## 模型选择边界

1. StepClaim 携带 capability requirements；
2. Scheduler 不调用 ModelSelectionPolicy；
3. Scheduler 不包含具体 DeepSeek / Qwen 名称；
4. Scheduler 不创建 Model Client；
5. Scheduler 不实现 Fallback。

## 回归

1. Planner 测试通过；
2. Model Selection 测试通过；
3. Context Builder 测试通过；
4. State Machine 测试通过；
5. AgentLoop 测试通过；
6. API、Memory 和 Stream 不变。

------

# 二十、Codex 实操提示词

你正在继续改造 LocalAgent 项目的阶段二 Runtime。

## 一、代码语言规范

本次及后续修改继续遵守：

- 代码注释使用中文；
- Docstring 优先使用中文；
- 自然语言 Prompt、错误说明、日志说明和设计说明优先使用中文；
- Scheduler 的状态说明、错误消息和面向开发者的提示优先使用中文；
- 变量名、函数名、类名、枚举名、字段名、模块名、配置 Key、协议标识和常见技术搭配继续使用英文；
- 不要将标准标识符翻译成拼音；
- `error_code`、`reason_code`、Enum value 等稳定标识继续使用英文；
- 中文说明与英文标识符混合时，保持常规 Python 风格。

## 二、结果文档位置

阶段二结果文档统一放在：

```text
docs/learning/stage2/result/
```

本次结果文档：

```text
docs/learning/stage2/result/day08_scheduler_result.md
```

无需关注 Push、Git remote、make_pr 或 PR 创建是否成功，用户会自行完成 GitHub 操作。本次重点是代码、测试、架构边界和结果文档。

## 三、项目背景

LocalAgent 已完成：

### 第 1～5 天

- Runtime 边界；
- RunContext；
- AgentState；
- AgentLoop；
- State Machine。

### 第 6 天

- ContextBuilder；
- Source / Trust；
- Token 预算；
- raw / final 上下文需求；
- 知识专家 RAG、Memory 和用户请求迁移。

### 第 7 天

- 不可变 Plan / PlanStep；
- TaskCapabilityRequirements；
- PlanValidator；
- LOCAL_FAST / REMOTE_ADVANCED Profile；
- ModelSelectionPolicy；
- ModelResolver；
- 知识专家真实模型选择接入；
- raw 用于信息保留偏好；
- final 用于执行可行性；
- 无 Fallback。

本次任务：

“阶段二第 8 天：实现最小串行 Scheduler，根据 Plan 和 AgentState 计算 Ready Step，执行原子 Step Claim，防止重复调度，并传播 BLOCKED。”

## 四、固定工作流

严格执行：

第一步：阅读 Plan、AgentState、State Machine 和 AgentLoop
第二步：列出当前 Step 创建、启动、完成和失败位置
第三步：分析 Scheduler 可以接入的真实边界
第四步：设计最小串行 Scheduler
第五步：实现 Prepare、Evaluate、Claim 和 Blocked 传播
第六步：进行最小安全集成
第七步：补充单元和集成测试
第八步：运行测试和检查
第九步：更新结果文档
第十步：增加 1～4 个重点 Bad Case

不得跳过真实代码检查直接整体重写。

## 五、修改前必须检查

至少检查：

- `core/runtime/planning.py`
- `core/runtime/state.py`
- `core/runtime/state_machine.py`
- `core/runtime/agent_loop.py`
- `core/runtime/model_selection.py`
- `core/runtime/__init__.py`
- `core/chat_service.py`
- `core/agent_router.py`
- 第 7 天知识专家单步 Plan 创建位置
- 所有 `AgentStateMachine.add_step()` 调用
- 所有 `StepEventType.STARTED` 调用
- 所有 `STEP_SUCCEEDED / FAILED / CANCELLED / BLOCKED / SKIPPED` 调用
- `active_step_ids` 使用位置
- 当前是否存在执行队列、Step queue 或 Ready 计算
- 现有 Planner / AgentLoop Driver 接口
- 第 7 天结果文档

结果文档必须说明：

- 当前 Plan 的所有者；
- 当前 AgentState 的所有者；
- 当前 Step 生命周期由谁管理；
- Scheduler 是否能安全进入默认 AgentLoop；
- 是否存在 Scheduler Claim 与 AgentLoop STARTED 重复的风险；
- 本次最终采用的接入方式。

## 六、职责边界

### Planner

只定义：

- Step；
- depends_on；
- completion criteria；
- capability requirements；
- preferred agent。

### AgentState

只保存实际执行状态。

### Scheduler

只负责：

- 注册 Plan Step；
- 计算 Ready Step；
- 传播 BLOCKED；
- 原子 Claim 一个 Step；
- 返回 StepClaim 和调度快照。

### Executor / AgentLoop

负责：

- 实际执行模型、Tool、RAG 或 Agent；
- 将 Step 标记为 SUCCEEDED / FAILED / CANCELLED；
- 决定 Run 是否结束。

Scheduler 不得：

- 执行 Tool；
- 调用模型；
- 调用 RAG；
- 选择 Model Profile；
- 修改 RunStatus；
- 输出流式 Chunk；
- 写入 final_output。

## 七、建议新增文件

建议新增：

```text
core/runtime/scheduler.py
tests/test_scheduler.py
docs/learning/stage2/result/day08_scheduler_result.md
```

并修改：

```text
core/runtime/__init__.py
```

根据真实接入情况，可以最小修改其他 Runtime 文件，但不得大规模重写 AgentRouter。

## 八、核心类型

### 1. SchedulerError

建立安全异常基类或等价异常。

建议至少包括：

- `SchedulerPlanStateMismatchError`
- `SchedulerClaimError`

异常只包含：

- plan_id；
- plan version；
- step_id；
- 当前安全状态；
- 固定 error code；
- 中文安全说明。

不得包含：

- 用户正文；
- Prompt；
- RAG；
- Tool 参数；
- Secret；
- 绝对敏感路径。

### 2. StepClaim

至少包含：

- `plan_id`
- `plan_version`
- `step_id`
- `claimed_at`
- `capability_requirements`
- `preferred_agent`

要求：

- frozen dataclass；
- `claimed_at` 为 timezone-aware UTC；
- 不包含 Model Client、Prompt 或原始用户输入。

### 3. SchedulerSnapshot

至少包含：

- `ready_step_ids`
- `running_step_ids`
- `pending_step_ids`
- `blocked_step_ids`
- `terminal_step_ids`
- `is_complete`
- `is_waiting`
- `has_unresolved_pending`

要求：

- 使用不可变 tuple；
- 是 Plan + AgentState 的派生视图；
- 不写回 AgentState；
- 不保存完整 Step 内容。

### 4. SerialScheduler

至少提供：

```text
prepare(plan, state, occurred_at)
evaluate(plan, state)
claim_next(plan, state, occurred_at)
```

可以增加私有：

```text
_propagate_blocked(...)
_compute_ready_steps(...)
_validate_plan_state_alignment(...)
```

不要拆成大量小文件。

## 九、Plan Step 注册

`prepare()` 应通过 `AgentStateMachine.add_step()` 注册 Plan 中尚未存在的 Step。

要求：

- Run 必须是 RUNNING；
- 不直接修改 `state.steps`；
- 新 Step 初始为 PENDING；
- Step name 使用简短 `PlanStep.title`；
- 不把 description、completion criteria、用户内容或 Prompt 写入 AgentState；
- 同一 Plan 重复 prepare 可以安全重复调用；
- 已存在相同 Step ID 且名称一致时跳过；
- 相同 Step ID 但名称或绑定语义明显不一致时抛出 `SchedulerPlanStateMismatchError`；
- 不修改 AgentState Schema；
- 不将 Plan 保存进 AgentState。

## 十、Ready Step 规则

PlanStep 只有同时满足以下条件才是 Ready：

1. AgentState 中存在对应 StepState；
2. StepStatus 为 PENDING；
3. Step 不在 active_step_ids；
4. RunStatus 为 RUNNING；
5. 所有 `depends_on` StepStatus 都是 SUCCEEDED；
6. 当前不存在其他 RUNNING Plan Step。

注意：

- Ready 是动态派生状态；
- 不新增 StepStatus.READY；
- 不将 Ready 写入 AgentState；
- 依赖 PENDING / RUNNING 时只是等待；
- 依赖 FAILED / CANCELLED / BLOCKED / SKIPPED 时进入 BLOCKED 传播。

## 十一、BLOCKED 传播

实现传递传播直到稳定。

阻断依赖状态：

```text
FAILED
CANCELLED
BLOCKED
SKIPPED
```

不会立即阻断的状态：

```text
PENDING
RUNNING
```

要求：

- 通过 `AgentStateMachine.apply_step_event(... BLOCKED ...)`；
- 不直接修改 StepState；
- 一轮传播后继续检查下游；
- 直到本轮没有新增 BLOCKED；
- 已 BLOCKED Step 不重复发送事件；
- 不修改 RunStatus；
- 不实现 Optional Dependency；
- 不实现 DAG 环检测。

如果现有 State Machine 允许 BLOCKED 携带安全错误摘要，可以使用：

```text
error_code = DEPENDENCY_NOT_SUCCESSFUL
error_message = 前置步骤未成功，当前步骤无法执行
```

如果不允许，不得为此修改 AgentState Schema；可以在 Scheduler 安全返回值或日志记录阻断依赖。

## 十二、Step Claim

`claim_next()` 必须在一个受保护的原子边界内完成：

```text
传播 BLOCKED
→ 检查是否已有 RUNNING Step
→ 计算 Ready Steps
→ 按稳定顺序选择一个
→ 通过 State Machine 执行 STARTED
→ 返回 StepClaim
```

要求：

- 只有 STARTED 成功后才返回 Claim；
- 失败时不返回伪造 Claim；
- Claim 后 StepStatus 为 RUNNING；
- `active_step_ids` 包含该 Step；
- 同一个 Step 不能重复 Claim；
- 当前串行模式下一次最多一个 RUNNING Plan Step。

## 十三、Claim 并发保护

即使当前不实现并行执行，也要避免同一进程内两个线程重复 Claim。

可以使用：

```text
threading.Lock
```

要求：

- Lock 属于单个 SerialScheduler 实例；
- Scheduler 按 Run 创建或由 Runtime 单 Run 持有；
- 不使用全局 Singleton；
- Lock 不序列化；
- `prepare / blocked propagation / ready check / STARTED` 的关键 Claim 操作在同一 Lock 内完成。

明确限制：

- 不解决多进程；
- 不解决多机器；
- 不实现数据库 Lock；
- 不实现 Lease；
- 不实现分布式 Claim。

## 十四、确定性顺序与公平性

多个 Ready Step 时：

```text
按 Plan.steps tuple 顺序
```

选择。

相同输入必须返回相同 Ready 顺序和 Claim 结果。

不得：

- 使用 Set 遍历顺序；
- 使用随机选择；
- 动态修改 Plan 顺序；
- 本次实现 Priority、Aging 或抢占。

结果文档说明：

- 当前公平性是有限静态 Plan 下的稳定顺序；
- 高级公平策略留给后续生产化。

## 十五、SchedulerSnapshot 语义

### is_complete

只有所有 Plan Step 均达到成功完成语义时才为 True。

当前建议：

```text
所有 Plan Step == SUCCEEDED
```

如果项目希望 SKIPPED 也算完成，必须明确说明并增加测试；不能隐式决定。

### is_waiting

存在 RUNNING Step，且当前没有可 Claim Step。

### has_unresolved_pending

满足：

- 仍有 PENDING Step；
- 没有 RUNNING Step；
- 没有 Ready Step；
- 未被依赖失败传播为 BLOCKED。

该状态可能来自：

- 第 9 天才处理的依赖环；
- Plan / State 不一致；
- 尚未实现的依赖语义。

本次不能宣称已经检测到 Cycle。

## 十六、Step 完成后的依赖释放

Scheduler 不负责将 Step 标记为 SUCCEEDED。

执行层通过 State Machine：

```text
RUNNING → SUCCEEDED
```

之后下一次 `evaluate()` 或 `claim_next()` 自动重新计算 Ready Steps。

不得持久化 READY 状态。

## 十七、与 Model Selection 的边界

`StepClaim` 可以携带：

- `capability_requirements`
- `preferred_agent`

Scheduler 不得：

- 调用 `ModelSelectionPolicy.select()`；
- 调用 `ModelResolver.resolve()`；
- 保存 selected profile；
- 判断 DeepSeek / Qwen；
- 实现 fallback。

模型选择由后续执行层在获得 Claim 后完成。

## 十八、最小真实接入策略

先检查 AgentLoop 当前是否已经自行：

```text
add_step
→ STEP_STARTED
```

### 如果存在安全扩展点

可以建立可选的串行 Plan 调度入口。

### 如果直接接入会导致双重生命周期

不得强行把 Scheduler 插入默认 Legacy AgentLoop。

此时应：

- 实现完整 Scheduler；
- 使用真实 Plan、AgentState、AgentStateMachine 做集成测试；
- 提供一个独立或可选的 Scheduler Adapter；
- 明确默认 Legacy AgentLoop 尚未消费 Scheduler；
- 说明未来接入需要如何转移 Step 创建和 STARTED 所有权。

禁止出现：

```text
Scheduler STARTED
+ AgentLoop 再 STARTED
```

同一个 Step 被启动两次。

## 十九、重点 Bad Case

结果文档必须包含：

```markdown
## 19. 重点 Bad Case
```

至少包括以下四个。

### Bad Case 1：Ready 检查与 Claim 分离导致重复执行

- 类型：假设构造，除非真实检查发现
- 两个调用者同时看到 PENDING
- 只有一个 Claim 可以成功
- 增加线程竞争测试

### Bad Case 2：只传播一层 BLOCKED

- 类型：假设构造
- 多层依赖链必须全部传播
- 使用不动点扫描
- 不留下错误 PENDING

### Bad Case 3：没有 Ready Step 就误判 Plan 完成

- 类型：假设构造
- 区分 waiting、complete、unresolved pending
- Scheduler 不直接修改 RunStatus

### Bad Case 4：使用 Set 选择 Ready Step 导致顺序不稳定

- 类型：假设构造，除非真实代码存在
- Ready 顺序使用 Plan tuple
- 增加重复执行稳定性测试

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

如果发现更高价值真实问题，可以增加，但必须准确标记真实性。

## 二十、测试要求

建议新增：

```text
tests/test_scheduler.py
```

至少覆盖：

### Prepare

1. 合法注册所有 Plan Step
2. 重复 prepare 幂等
3. 相同 ID、不同名称失败
4. Run 非 RUNNING 时失败
5. Step 初始为 PENDING
6. 不修改 Plan
7. 不修改 AgentState Schema

### Ready

1. 无依赖 Step Ready
2. 全部依赖 SUCCEEDED 后 Ready
3. 依赖 PENDING 时等待
4. 依赖 RUNNING 时等待
5. RUNNING Step 不 Ready
6. 终态 Step 不 Ready

### BLOCKED

1. FAILED 阻断下游
2. CANCELLED 阻断下游
3. BLOCKED 传递阻断
4. SKIPPED 阻断下游
5. 多层传递传播
6. 一个依赖失败即可阻断
7. PENDING / RUNNING 不提前阻断
8. 已 BLOCKED 不重复事件

### Claim

1. Claim 将 PENDING 变为 RUNNING
2. Claim 返回正确 StepClaim
3. active_step_ids 正确
4. 已有 RUNNING 时不 Claim
5. 同一 Step 不重复 Claim
6. 两线程竞争只有一个成功
7. Claim 失败状态不变
8. 通过 State Machine STARTED

### Snapshot

1. Running 时 is_waiting
2. 全部成功时 is_complete
3. Failed / Blocked 不算成功完成
4. PENDING 无 Ready 时 unresolved
5. Scheduler 不修改 RunStatus

### 顺序

1. Ready 顺序遵循 Plan
2. 多次 evaluate 稳定
3. 下游释放顺序稳定
4. 不依赖 Set Hash 顺序

### 边界

1. Scheduler 不调用 Model Selection
2. StepClaim 携带 capability requirements
3. Scheduler 不包含 DeepSeek / Qwen 判断
4. 不实现 Fallback
5. 不实现并行执行
6. 不实现 DAG 环检测

### 回归

1. Planning 测试通过
2. Model Selection 测试通过
3. Context Builder 测试通过
4. State Machine 测试通过
5. AgentLoop 测试通过
6. 原 API / Memory / Stream 不变

不得调用真实模型，不加载真实 GGUF，不启动 Chroma、PyQt6、FastAPI 或数据库，不访问外部网络。

## 二十一、测试与检查

执行当前环境能够运行的：

```text
python -m pytest \
  tests/test_scheduler.py \
  tests/test_planning.py \
  tests/test_model_selection.py \
  -q

python -m unittest \
  tests.test_runtime_context \
  tests.test_agent_state \
  tests.test_agent_loop \
  tests.test_state_machine \
  tests.test_model_context \
  tests.test_planning \
  tests.test_model_selection \
  tests.test_scheduler \
  -q

python -m compileall core tests
git diff --check
```

当前 Linux 环境若继续因 Windows 本地 `llama_cpp_python` Wheel 无法执行 `uv run`，只记录依赖环境限制，不修改本次 Scheduler 业务代码。

## 二十二、禁止事项

不得：

- 实现并行执行；
- 实现优先级调度；
- 实现抢占；
- 实现 DAG 环检测；
- 实现分布式锁；
- 实现 Lease；
- 实现数据库调度队列；
- 实现 Retry；
- 实现 Fallback；
- 修改 hybrid eager loading；
- 修改模型选择规则；
- 修改 AgentState Schema；
- 修改 API；
- 修改 Memory Schema；
- 修改流式协议；
- 修改 `[[ORCH]]`；
- 大规模重写 AgentRouter；
- 将 Scheduler 结果写入 final_output。

## 二十三、结果文档

创建：

```text
docs/learning/stage2/result/day08_scheduler_result.md
```

必须包含：

# 阶段二第 8 天改造结果

## 1. 本次任务目标

## 2. 修改前调度现状

## 3. Planner、Scheduler、AgentLoop 边界

## 4. 最终设计方案

## 5. 新增文件

## 6. 修改文件

## 7. 核心类型

说明：

- SerialScheduler
- StepClaim
- SchedulerSnapshot
- Scheduler Error

## 8. Plan 和 AgentState 绑定

## 9. Ready Step 规则

## 10. Step Claim 与重复调度防护

## 11. BLOCKED 传播

## 12. 串行调度与公平性

## 13. 完成、等待和 unresolved 判断

## 14. 与 AgentLoop 的接入方式

必须说明是否存在双重 STARTED 风险。

## 15. 与 Model Selection 的边界

## 16. 测试命令和结果

## 17. 未完成事项和已知风险

至少说明：

- 未实现并行；
- 未实现 DAG 环检测；
- 未实现分布式 Claim；
- 未实现持久化；
- Scheduler Lock 只保护单进程单实例；
- 默认 Legacy AgentLoop 是否已经消费 Scheduler；
- AgentState 仍未持久化；
- Generator close 风险仍存在。

## 18. 设计权衡和面试描述

## 19. 重点 Bad Case

至少四个。

## 20. 需要带回 ChatGPT 审查的信息

必须包含：

- Scheduler 文件和入口；
- Plan 所有者；
- AgentState 所有者；
- Step 注册入口；
- Ready 计算规则；
- 阻断依赖状态；
- BLOCKED 传播方式；
- Step Claim 原子边界；
- Claim Lock 范围；
- Ready 顺序；
- 公平性语义；
- is_complete 语义；
- is_waiting 语义；
- unresolved pending 语义；
- Scheduler 是否修改 RunStatus；
- Scheduler 是否调用 Model Selection；
- 与 AgentLoop 的实际接入；
- 是否存在双重 Step STARTED；
- 是否实现并行、DAG、分布式锁；
- 是否修改 AgentState Schema；
- 测试命令和结果；
- Bad Case；
- 需要人工确认的问题；
- 后续建议，但不得实施第 9 天。

## 二十四、聊天最终输出

完成后输出：

结果文档路径：

新增文件：

修改文件：

Scheduler 入口：

Plan 所有者：

AgentState 所有者：

Step 注册入口：

Ready 规则：

BLOCKED 传播：

Step Claim：

Claim Lock：

Ready 顺序：

is_complete：

is_waiting：

unresolved pending：

是否修改 RunStatus：

是否调用 Model Selection：

与 AgentLoop 接入方式：

是否存在双重 STARTED：

是否实现并行：

是否实现 DAG：

是否实现分布式锁：

测试命令：

测试结果：

Bad Case：

需要人工确认的问题：

------

# 二十一、Codex 结果审查重点

结果返回后重点检查：

1. Scheduler 是否只读取 Plan 和 AgentState；
2. Plan 是否仍然不可变；
3. Scheduler 是否没有复制 Runtime 状态到 Plan；
4. Ready 是否只要求依赖 `SUCCEEDED`；
5. `PENDING` 和 `RUNNING` 依赖是否只等待；
6. `FAILED/CANCELLED/BLOCKED/SKIPPED` 是否传播阻断；
7. BLOCKED 是否传递到所有下游；
8. Claim 是否通过 State Machine；
9. Ready 检查与 STARTED 是否处于同一 Lock；
10. 两线程是否只能认领一次；
11. 已有 RUNNING Step 时是否不认领第二个；
12. Scheduler 是否没有直接修改 RunStatus；
13. 无 Ready 时是否正确区分 waiting、complete 和 unresolved；
14. Ready 顺序是否确定；
15. 是否错误使用 Set 顺序；
16. Scheduler 是否没有调用 Model Selection；
17. StepClaim 是否只携带结构化能力需求；
18. 是否存在 Scheduler 和 AgentLoop 双重 STARTED；
19. 是否为了接入而大规模重写 AgentLoop；
20. 是否提前实现并行、DAG 或分布式锁；
21. AgentState Schema 是否保持不变；
22. 测试是否包含并发 Claim；
23. Bad Case 是否区分真实与假设；
24. 注释、Docstring 和自然语言说明是否优先使用中文。

------

# 二十二、面试高频问题

## 1. Ready Step 如何判断？

> Step 必须仍为 PENDING，Run 处于 RUNNING，所有强依赖都已经 SUCCEEDED，并且当前串行 Scheduler 没有其他 RUNNING Step。

## 2. 为什么不把 READY 保存到 AgentState？

> READY 可以由 Plan 依赖和 AgentState 动态计算。持久化 READY 会增加额外状态转移和失效处理，因此第一版只保存 PENDING 和 RUNNING。

## 3. 如何防止重复调度？

> 将 Ready 检查、Step 选择和 PENDING 到 RUNNING 的状态转移放入同一个 Claim 原子边界，并通过单 Run Lock 和 State Machine 保证只有一个调用者能成功获得 Claim。

## 4. 依赖失败后如何处理下游？

> 对依赖 FAILED、CANCELLED、BLOCKED 或 SKIPPED 的 PENDING Step 标记 BLOCKED，并持续传播直到没有新的 BLOCKED Step。

## 5. Ready 队列为空是否表示 Plan 完成？

> 不一定。可能有 Step 正在运行，也可能存在环或未决依赖。Scheduler 需要分别表达 waiting、complete 和 unresolved pending。

## 6. Scheduler 为什么不选择模型？

> Scheduler 只决定执行哪个 PlanStep。模型选择需要结合 Step 的能力需求、实际上下文和可用 Model Profile，属于 Model Selection Policy。

------

# 二十三、第 8 天验收清单

## 理论验收

-  区分 Planner 与 Scheduler
-  理解 Ready Step
-  理解 Step Claim
-  理解串行调度
-  理解依赖释放
-  理解 BLOCKED 传播
-  理解 Claim 原子性
-  理解确定性顺序
-  区分 waiting、complete、unresolved
-  理解 Scheduler 与 Model Selection 的边界

## 项目验收

-  `SerialScheduler` 已实现
-  `prepare()` 已实现
-  `evaluate()` 已实现
-  `claim_next()` 已实现
-  `StepClaim` 已建立
-  `SchedulerSnapshot` 已建立
-  Plan Step 注册完成
-  Ready 计算完成
-  BLOCKED 传递传播完成
-  Claim 使用 State Machine
-  同进程重复 Claim 防护完成
-  Ready 顺序确定
-  Step 成功后依赖自动释放
-  无 Ready 状态判断正确
-  Scheduler 不修改 RunStatus
-  Scheduler 不调用模型选择
-  不存在双重 STARTED
-  未实现并行
-  未实现 DAG 环检测
-  未修改 AgentState Schema
-  Scheduler 测试通过
-  Runtime 回归通过
-  Bad Case 完整
-  完成 ChatGPT 审查

## 阶段二进度

**第 8/25 天：理论与架构设计完成，等待 Codex 改造结果审查。**

下一天主题：**第 9 天 DAG（有向无环图）——拓扑排序、环检测、依赖合法性、不可达步骤和计划图验证。**