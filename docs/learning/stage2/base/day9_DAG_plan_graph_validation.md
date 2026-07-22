# 阶段二第 9 天：DAG（有向无环图）与 Plan Graph Validation（计划图校验）

**当前进度：第 9/25 天。**

前两天已经建立：

```text
Planner
→ 生成不可变 Plan 和依赖关系

Scheduler
→ 根据 Plan + AgentState 计算 Ready Step
→ Claim 一个 Step
```

但当前 Scheduler 默认相信：

```text
Plan.depends_on 一定合法
```

今天要在 Planner 和 Scheduler 之间增加静态验证边界：

```text
Planner
→ Plan
→ PlanGraphValidator
→ Validated PlanGraph
→ Scheduler
```

核心目标是：

> Scheduler 不应该在运行过程中才发现 Plan 存在缺失依赖或依赖环。

------

# 一、当天目标

今天必须掌握并落地：

1. Dependency Graph（依赖图）的基本结构；
2. Directed Graph（有向图）中边的方向；
3. In-degree（入度）；
4. Topological Sort（拓扑排序）；
5. Kahn Algorithm（Kahn 算法）；
6. Cycle Detection（环检测）；
7. 缺失依赖、自依赖和重复依赖；
8. 重复 Step ID；
9. 稳定拓扑顺序；
10. “不可达步骤”的准确语义；
11. DAG Validator（DAG 校验器）与 Scheduler 的关系；
12. 静态图校验与 Runtime 校验的区别；
13. 非法 Plan 必须在修改 AgentState 之前被拒绝；
14. 不实现并行执行。

------

# 二、什么是 Plan 的依赖图

假设 Plan 为：

```text
step-a：读取代码
step-b：分析代码，依赖 step-a
step-c：生成修改方案，依赖 step-b
```

将每个 Step 看作一个节点：

```text
step-a
step-b
step-c
```

依赖关系：

```text
step-b depends_on step-a
```

可以表达为有向边：

```text
step-a → step-b
```

完整图：

```text
step-a → step-b → step-c
```

边的方向表示：

> 前置步骤完成后，后续步骤才可能执行。

------

# 三、DAG 的含义

DAG 是：

```text
Directed Acyclic Graph
有向无环图
```

它同时满足：

- 边有方向；
- 图中不存在环。

合法：

```text
A → B → C
```

合法：

```text
A ─→ C
B ─→ C
```

非法：

```text
A → B → C
↑       ↓
└───────┘
```

因为：

```text
A 依赖 C
C 又间接依赖 A
```

没有任何 Step 能率先执行。

------

# 四、为什么 Plan 必须是 DAG

如果 Plan 有环：

```text
step-a depends_on step-c
step-b depends_on step-a
step-c depends_on step-b
```

Scheduler 看到：

```text
step-a：等待 step-c
step-b：等待 step-a
step-c：等待 step-b
```

结果：

```text
无 Ready Step
无 Running Step
所有 Step 仍为 PENDING
```

第 8 天 Scheduler 只能返回：

```text
has_unresolved_pending = True
```

但它无法判断这是：

- 依赖环；
- Plan 与 State 不一致；
- 尚未支持的依赖语义。

因此环应该在 Scheduler 执行前被静态拒绝。

------

# 五、入度

一个节点的 In-degree（入度）表示：

> 有多少条依赖边指向这个节点。

例如：

```text
A → C
B → C
```

入度：

```text
A = 0
B = 0
C = 2
```

在 Plan 中可以直接理解为：

```text
indegree(step)
=
len(step.depends_on)
```

但前提是：

- 依赖 ID 存在；
- 没有重复依赖；
- 没有自依赖。

------

# 六、拓扑排序

Topological Order（拓扑顺序）必须保证：

> 每个依赖 Step 都出现在依赖它的 Step 之前。

例如：

```text
A → C
B → C
```

合法顺序可以是：

```text
A, B, C
```

也可以是：

```text
B, A, C
```

但不能是：

```text
C, A, B
```

拓扑顺序通常不唯一，因此 LocalAgent 必须定义**稳定决胜规则**。

------

# 七、稳定拓扑顺序

第 8 天 Scheduler 已经使用：

```text
Plan.steps tuple 顺序
```

作为稳定调度顺序。

第 9 天拓扑排序也应保持这一语义：

> 当多个零入度 Step 同时可选时，优先选择它们在 `Plan.steps` 中更靠前的 Step。

例如 Plan 原始顺序：

```text
step-c，依赖 step-a
step-b，无依赖
step-a，无依赖
step-d，无依赖
```

初始零入度 Step：

```text
step-b
step-a
step-d
```

稳定拓扑顺序：

```text
step-b
step-a
step-c
step-d
```

因为 `step-c` 在 `step-a` 完成后被释放，并且它在原 Plan 中的位置早于 `step-d`。

这与第 8 天 Scheduler 每轮按 Plan 顺序扫描 Ready Step 的行为一致。

------

# 八、Kahn 拓扑排序算法

## 1. 初始化入度

```text
A：0
B：1
C：1
```

## 2. 收集所有零入度节点

```text
ready = [A]
```

## 3. 取出一个零入度节点

```text
topological_order = [A]
```

## 4. 删除它发出的边

如果：

```text
A → B
```

则：

```text
B.indegree -= 1
```

## 5. 新的零入度节点加入候选集合

```text
ready = [B]
```

## 6. 重复直到没有候选节点

最终：

```text
topological_order = [A, B, C]
```

------

## 环检测

如果最终：

```text
len(topological_order) < len(plan.steps)
```

则说明图中存在环。

因为剩余节点的入度永远无法降为零。

------

# 九、Kahn 剩余节点不一定都是环节点

这是实现环检测时非常容易犯的错误。

假设：

```text
A → B → A
B → C
```

真正的环是：

```text
A → B → A
```

但 Kahn 算法结束后，剩余节点可能是：

```text
A
B
C
```

其中 `C` 不在环内，只是依赖了环。

因此不能把所有剩余节点直接命名为：

```text
cycle_step_ids
```

更准确的是：

```text
unresolved_step_ids
```

若要输出真正的环，需要在剩余子图上继续执行稳定 DFS（深度优先搜索），提取一个实际 Cycle Path（环路径）：

```text
A → B → A
```

错误信息应区分：

```text
cycle_path
unresolved_step_ids
```

------

# 十、非法依赖类型

## 1. 重复 Step ID

非法：

```text
step-1
step-1
```

如果允许重复：

- 依赖查找结果不唯一；
- AgentState 只能保存一个同名 Step；
- Scheduler 无法判断执行哪一个；
- 图节点会被覆盖。

必须在图构建前拒绝。

------

## 2. 缺失依赖

非法：

```text
step-b depends_on step-x
```

但 Plan 中没有 `step-x`。

不能自动创建：

```text
phantom step-x
```

否则 Planner 错误会被静默隐藏。

------

## 3. 自依赖

非法：

```text
step-a depends_on step-a
```

这是长度为 1 的环，应提供比通用环错误更明确的错误原因：

```text
SELF_DEPENDENCY
```

------

## 4. 重复依赖

非法：

```python
depends_on=("step-a", "step-a")
```

如果不拒绝，入度可能被计算为：

```text
2
```

但 `step-a` 成功时只释放一次，导致该 Step 永远无法归零。

因此重复依赖必须在计算入度前拒绝。

------

# 十一、“不可达步骤”的准确含义

用户给出的学习内容包含：

```text
不可达步骤
```

但这里必须区分图论定义和当前 Plan Schema。

## 当前 Plan 没有显式入口

当前 `Plan` 没有：

```text
entry_step_ids
```

也没有：

```text
goal_step_id
```

同时允许多个零入度 Step：

```text
A → B

C → D
```

这是一张包含两个独立组件的合法 DAG。

Scheduler 可以依次执行：

```text
A
B
C
D
```

因此不能将 `C、D` 判断为不可达。

------

## 在当前语义下

任何合法有限 DAG 中，每个节点都可以从至少一个零入度 Root Step（根步骤）到达。

所以：

> 在“所有零入度 Step 都是合法入口”的语义下，不存在传统意义上的不可达 Step。

当前可以输出诊断：

```text
root_step_ids
leaf_step_ids
component_count
```

但不能因为图不连通就拒绝 Plan。

------

## 什么时候才能真正检测不可达

需要 Plan 显式定义：

```text
entry_step_ids
```

此时才可以判断：

```text
不能从任何 entry 到达的 Step
→ unreachable
```

或者定义：

```text
goal_step_ids
```

此时可以判断：

```text
无法贡献到任何 goal 的 Step
→ irrelevant / dead branch
```

当前第 9 天不修改 Plan Schema，因此：

- 不拒绝多根 DAG；
- 不拒绝不连通组件；
- 不虚构 `unreachable_step_ids`；
- 在结果文档说明这一设计边界。

------

# 十二、PlanGraph 建议结构

建议新增：

```python
@dataclass(frozen=True, slots=True)
class PlanGraph:
    """表示经过静态校验的 Plan 依赖图。"""

    plan_id: str
    plan_version: int
    step_ids: tuple[str, ...]
    topological_order: tuple[str, ...]
    root_step_ids: tuple[str, ...]
    leaf_step_ids: tuple[str, ...]
    dependencies: tuple[tuple[str, tuple[str, ...]], ...]
    dependents: tuple[tuple[str, tuple[str, ...]], ...]
```

注意：

- `PlanGraph` 不保存 StepStatus；
- 不保存 AgentState；
- 不保存 Model Profile；
- 不保存用户正文；
- 不保存完整 Prompt；
- 不修改原始 Plan；
- 拓扑顺序必须稳定。

------

# 十三、DAG Validator 建议接口

```python
class PlanGraphValidator:
    """构建并验证 Plan 的静态依赖图。"""

    def validate(self, plan: Plan) -> PlanGraph:
        ...
```

推荐流程：

```text
Plan 基础字段校验
→ Step ID 唯一校验
→ 依赖存在性校验
→ 自依赖校验
→ 重复依赖校验
→ 构建 dependencies / dependents
→ 计算 indegree
→ 稳定 Kahn 拓扑排序
→ 检测环
→ 必要时提取稳定 cycle_path
→ 构建不可变 PlanGraph
```

------

# 十四、PlanValidator 与 DAG Validator 的关系

第 7 天已有 `PlanValidator`。

不要出现两个相互冲突的校验器：

```text
PlanValidator 认为合法
PlanGraphValidator 认为同一基础字段非法
```

推荐边界：

## PlanValidator

负责：

- Plan ID；
- version；
- task summary；
- Step 字段；
- datetime；
- capability requirements；
- 至少一个 Step；
- 基础类型不变量。

## PlanGraphValidator

负责：

- 重复 Step ID；
- 缺失依赖；
- 自依赖；
- 重复依赖；
- 图构建；
- 拓扑排序；
- 环检测。

如果第 7 天的 `PlanValidator` 已经校验部分依赖规则，可以：

- 由 `PlanGraphValidator` 先调用 `PlanValidator`；
- 再执行图算法；
- 或抽取共享私有函数。

不能复制两套独立实现后逐渐产生差异。

------

# 十五、DAG Validator 与 Scheduler 的关系

正确边界：

```text
Planner
→ Plan
→ PlanGraphValidator.validate()
→ PlanGraph
→ SerialScheduler
```

Scheduler 不应该自己实现：

- 环检测；
- Kahn 算法；
- 缺失依赖检查；
- 自依赖检查。

Scheduler只消费已经验证的图，并负责：

- Ready 计算；
- BLOCKED 传播；
- Step Claim。

------

## 当前最小接入方案

当前 Plan 仍缺少长期所有者，因此可以保持 Scheduler API 接收 `Plan`，但在 Scheduler 边界强制：

```text
validate(plan)
→ 验证成功
→ 才允许 prepare / evaluate / claim
```

或者让 Scheduler 内部缓存本次：

```text
PlanGraph
```

关键要求是：

> 非法 Plan 必须在任何 `AgentStateMachine.add_step()` 之前被拒绝。

------

# 十六、为什么必须先校验，再注册 Step

错误流程：

```text
注册 step-a
注册 step-b
发现 step-c 存在环
抛出异常
```

此时 AgentState 已经包含：

```text
step-a = PENDING
step-b = PENDING
```

但 Scheduler 拒绝执行该 Plan。

这会留下 Partial Registration（部分注册）：

- State 中存在无法执行的孤立 Step；
- 重试时出现重复 ID；
- UI 可能显示错误 PENDING；
- 后续 Plan 可能错误接管这些 Step。

正确流程：

```text
完整验证 PlanGraph
→ 完整检查 Plan / State 对齐
→ 确认全部合法
→ 再注册所有缺失 Step
```

这属于第 9 天非常重要的不变量。

------

# 十七、拓扑顺序与 Scheduler 顺序

稳定拓扑顺序可以用于：

- 调试；
- 结果文档；
- Plan 可解释性；
- Scheduler 候选顺序；
- 后续并行层级计算；
- Checkpoint 恢复诊断。

但今天不实现：

```text
按拓扑层并行执行
```

当前仍然是：

```text
一次 Claim 一个 Step
```

Scheduler 可以使用 `PlanGraph.topological_order` 作为稳定候选顺序，或者继续使用与其等价的 Plan 顺序扫描。

必须保证：

```text
相同 Plan
→ 相同 topological_order
→ 相同串行 Claim 顺序
```

------

# 十八、静态 DAG 校验与 Runtime 校验的区别

## 静态 DAG 校验

发生在运行前，检查：

- Step ID 是否唯一；
- 依赖是否存在；
- 是否自依赖；
- 是否重复依赖；
- 是否有环；
- 拓扑顺序是否可生成。

它不读取：

- StepStatus；
- active_step_ids；
- RunStatus；
- Deadline；
- Cancellation。

------

## Runtime 校验

发生在执行中，检查：

- 当前 Step 是否为 PENDING；
- 依赖是否已经 SUCCEEDED；
- 是否已有 RUNNING Step；
- Claim 是否重复；
- 状态转移是否合法；
- 下游是否应 BLOCKED。

一张合法 DAG 仍可能因为 Runtime State 损坏而无法推进。

反过来，Runtime Scheduler 不应承担静态环检测责任。

------

# 十九、复杂度

对于：

```text
V = Step 数量
E = 依赖边数量
```

构图和拓扑排序复杂度：

```text
O(V + E)
```

空间复杂度：

```text
O(V + E)
```

这适合 Plan 通常只有几十个 Step 的 Agent 场景。

不能使用反复全图搜索的低效实现：

```text
每取一个 Step
→ 扫描全部 Step 和全部依赖
```

造成不必要的高阶复杂度。

------

# 二十、第 9 天高价值 Bad Case

## Bad Case 1：注册部分 Step 后才发现 Plan 有环

- **类型：假设构造**

### 触发条件

Scheduler 一边遍历 Plan，一边调用：

```text
AgentStateMachine.add_step()
```

处理到后半部分时才执行环检测。

### 故障表现

Plan 被拒绝，但 AgentState 已经留下部分 `PENDING` Step：

- 重试出现重复 ID；
- 状态与 Plan 不一致；
- 后续 Scheduler 可能错误接管；
- Run 无法正确清理。

### 根因分析

静态图验证发生在 Runtime 状态变更之后。

### 修复方案

```text
完整 DAG 校验
→ 完整 Plan/State 预检查
→ 再执行 Step 注册
```

### 回归测试

构造含环 Plan：

- `prepare()` 抛出 DAG 错误；
- AgentState 序列化前后完全一致；
- `steps` 和 `active_step_ids` 均未变化；
- 不发送任何 Step Event。

### 对应知识点

- Validate Before Mutate（先校验后修改）；
- 原子边界；
- Partial State；
- 静态验证与运行时状态分离。

### 面试表达

> DAG 校验必须发生在 Step 注册前，否则处理到后半部分才发现环时，AgentState 已经被部分污染。我先完整构图和拓扑校验，再做 Plan/State 对齐预检查，最后才注册 Step。

------

## Bad Case 2：把 Kahn 剩余节点全部当成环节点

- **类型：假设构造**

### 触发条件

图为：

```text
A → B → A
B → C
```

Kahn 结束后剩余：

```text
A、B、C
```

错误实现把三者都输出为 `cycle_step_ids`。

### 故障表现

错误报告宣称：

```text
C 位于依赖环
```

但 C 只是依赖环，不在环内。

这会误导 Planner 修复错误的 Step。

### 根因分析

混淆了：

```text
未能完成拓扑排序的节点
真正位于环中的节点
```

### 修复方案

- Kahn 剩余节点命名为 `unresolved_step_ids`；
- 在剩余图中用稳定 DFS 提取真实 `cycle_path`；
- 错误信息分别输出两者。

### 回归测试

验证：

```text
cycle_path = A → B → A
unresolved_step_ids = A、B、C
```

不能将 C 标记为真正环成员。

### 对应知识点

- Kahn Algorithm；
- Cycle Path；
- 图诊断准确性；
- Root Cause Reporting（根因报告）。

### 面试表达

> Kahn 算法的剩余节点不一定全部在环内，也可能只是依赖了环。我把它们定义为 unresolved nodes，并额外通过稳定 DFS 提取真实环路径，避免误导 Planner。

------

## Bad Case 3：将不连通的多根 DAG 误判为不可达

- **类型：假设构造**

### 触发条件

Plan：

```text
A → B

C → D
```

实现默认只从第一个 Root `A` 开始 DFS，将 `C、D` 判断为不可达。

### 故障表现

合法的多工作流 Plan 被错误拒绝。

### 根因分析

当前 Plan 没有声明唯一入口，但校验器擅自把第一个 Step 当作唯一入口。

### 修复方案

- 当前所有零入度 Step 都视为合法 Root；
- 不拒绝不连通 DAG；
- 输出 `root_step_ids` 和组件诊断；
- 只有未来增加 `entry_step_ids` 后才检测不可达。

### 回归测试

多根不连通 DAG：

- 验证通过；
- 稳定输出两个 Root；
- 所有 Step 都进入拓扑顺序；
- Scheduler 可以串行执行全部 Step。

### 对应知识点

- Reachability（可达性）；
- 多入口 DAG；
- Schema Semantics（Schema 语义）；
- 避免隐式假设。

### 面试表达

> 不可达节点必须相对于显式入口定义。当前 Plan 没有 entry_step_ids，因此所有零入度节点都是合法入口，我不会把不连通组件误判为不可达。

------

## Bad Case 4：使用 Set 生成拓扑顺序，结果不稳定

- **类型：假设构造**

### 触发条件

零入度节点保存在：

```python
set[str]
```

然后使用任意遍历顺序。

### 故障表现

同一 Plan 在不同运行中得到不同拓扑顺序：

- Scheduler Claim 顺序变化；
- Tool 副作用顺序变化；
- 测试偶发失败；
- 线上问题难以复现。

### 根因分析

拓扑排序本身不唯一，但系统没有定义稳定决胜规则。

### 修复方案

使用 Plan 原始索引作为稳定优先级：

```text
plan_index 越小
→ 越先出队
```

### 回归测试

相同 Plan 重复验证多次：

- 拓扑顺序完全一致；
- 多个 Root 的顺序遵循 Plan；
- 新释放节点和已有 Root 的选择也遵循原 Plan 索引。

### 对应知识点

- Stable Topological Sort（稳定拓扑排序）；
- Determinism（确定性）；
- 可复现调度；
- Tie-breaking（平局决胜）。

### 面试表达

> 拓扑顺序通常不唯一，但 Agent Runtime 需要可复现性。我使用 Plan 原始索引作为零入度节点的稳定决胜规则，确保相同输入得到相同执行顺序。

------

## Bad Case 5：重复依赖导致入度永远无法归零

- **类型：假设构造**

### 触发条件

```python
step_b.depends_on = ("step-a", "step-a")
```

构图时入度计算为 2，但 dependents 使用 Set 去重后只释放一次。

### 故障表现

`step-b` 最终入度为 1：

- 拓扑排序错误判定存在环；
- Scheduler 永远无法执行 step-b；
- 错误信息难以定位。

### 根因分析

重复边在构图前未被拒绝，且正向图与反向图使用了不同的去重语义。

### 修复方案

在构图前拒绝同一 Step 内的重复依赖，不允许自动去重。

### 回归测试

重复依赖必须得到：

```text
DUPLICATE_DEPENDENCY
```

不能静默修复，也不能误报为 Cycle。

### 对应知识点

- Graph Edge Invariant（图边不变量）；
- 入度一致性；
- Fail Fast（快速失败）；
- 错误分类。

------

# 二十一、测试方案

## 基础构图

1. 单 Step Plan；
2. 线性依赖链；
3. 多根 DAG；
4. 汇聚依赖；
5. 分叉依赖；
6. 不连通但合法的多个组件；
7. Plan 保持不可变。

## 静态错误

1. 重复 Step ID；
2. 缺失依赖；
3. 自依赖；
4. 重复依赖；
5. 两节点环；
6. 三节点环；
7. 多个独立环；
8. 环加下游节点；
9. 错误中不包含 Step description 或用户正文。

## 拓扑排序

1. 依赖一定排在下游前；
2. 多 Root 按 Plan 顺序；
3. 新释放节点按 Plan 索引稳定排序；
4. 同一 Plan 多次结果一致；
5. 所有 Step 恰好出现一次；
6. 不依赖 Set 顺序；
7. 复杂图仍为 `O(V+E)` 风格实现。

## 环诊断

1. Kahn 剩余节点标记为 unresolved；
2. cycle path 只包含实际环；
3. 下游节点不被错误标为环成员；
4. cycle path 顺序稳定；
5. 自依赖得到专用错误。

## Root、Leaf 与不可达语义

1. 正确输出 root step IDs；
2. 正确输出 leaf step IDs；
3. 不连通 DAG 不被拒绝；
4. 不虚构唯一 entry；
5. 不输出误导性的 unreachable。

## Scheduler 集成

1. Scheduler 在注册前验证 DAG；
2. 非法 Plan 不注册任何 Step；
3. 非法 Plan 不修改 AgentState；
4. 合法 Plan 可以正常 prepare；
5. 稳定拓扑顺序与 Claim 顺序一致；
6. DAG Validator 不读取 StepStatus；
7. Scheduler 不重复实现环检测；
8. 不实现并行执行。

## 回归

1. Scheduler 测试通过；
2. Planning 测试通过；
3. State Machine 测试通过；
4. Model Selection 测试通过；
5. Context Builder 测试通过；
6. AgentLoop 测试通过；
7. 全仓测试通过。

------

# 二十二、Codex 实操提示词

你正在继续改造 LocalAgent 项目的阶段二 Runtime。

## 一、代码语言规范

继续遵守：

- 代码注释使用中文；
- Docstring 优先使用中文；
- 自然语言 Prompt、错误说明、日志说明和设计说明优先使用中文；
- 图校验错误的开发者说明使用中文；
- 变量名、函数名、类名、枚举名、字段名、模块名、配置 Key、协议标识和常见技术搭配继续使用英文；
- 不要将标准标识符翻译成拼音；
- `error_code`、Enum value 等稳定标识使用英文；
- Step ID、Plan ID 和错误码不得翻译。

## 二、结果文档位置

阶段二结果文档统一位于：

```text
docs/learning/stage2/result/
```

本次结果文档：

```text
docs/learning/stage2/result/day09_dag_result.md
```

无需关注 Push、Git remote、make_pr 或 PR 创建结果，用户会自行处理 GitHub 操作。本次只关注代码、架构、测试和结果文档。

## 三、项目背景

LocalAgent 已完成：

### 第 7 天

- 不可变 Plan / PlanStep；
- TaskCapabilityRequirements；
- PlanValidator；
- Model Selection Policy。

### 第 8 天

- SerialScheduler；
- prepare / evaluate / claim_next；
- Ready Step；
- StepClaim；
- BLOCKED 不动点传播；
- 单实例 Claim Lock；
- 稳定串行调度。

当前限制：

- 默认 Legacy AgentLoop 尚未接入 Scheduler；
- Plan 目前仍由 `AgentRouter._select_model()` 临时创建并丢弃；
- AgentState 由 ChatService 单次调用栈持有；
- Scheduler 尚未实现 DAG 环检测；
- unresolved pending 目前不能区分环与其他状态问题。

本次任务：

“阶段二第 9 天：为 Plan 构建 PlanGraph 和 DAG Validator，在 Scheduler 注册 Step 前拒绝环、缺失依赖、自依赖、重复依赖和重复 Step ID，并输出稳定拓扑顺序。”

## 四、固定工作流

严格执行：

第一步：阅读 Plan、PlanValidator 和 Scheduler
第二步：总结当前依赖校验位置及重复逻辑
第三步：设计 PlanGraph 和 DAG Validator
第四步：实现稳定拓扑排序与环检测
第五步：将校验接入 Scheduler 注册前边界
第六步：补充非法 Plan 和稳定顺序测试
第七步：运行专项与全仓测试
第八步：生成结果文档
第九步：补充至少 4 个重点 Bad Case

不得跳过真实代码检查直接重写 Planning 或 Scheduler。

## 五、修改前必须检查

至少检查：

- `core/runtime/planning.py`
- `core/runtime/scheduler.py`
- `core/runtime/state.py`
- `core/runtime/state_machine.py`
- `core/runtime/__init__.py`
- `tests/test_planning.py`
- `tests/test_scheduler.py`
- 第 7 天结果文档
- 第 8 天结果文档
- 所有 PlanValidator 调用位置
- 所有 depends_on 校验位置
- Scheduler.prepare() 是否在校验前修改 AgentState
- Scheduler.claim_next() 是否隐式调用 prepare()
- 当前 Ready 顺序与 Plan 顺序关系
- 当前是否有任何环检测代码

结果文档必须说明：

- 哪些校验已经存在；
- 哪些校验发生重复；
- 本次如何避免两套规则漂移；
- DAG 校验最终在哪个边界执行；
- 非法 Plan 是否可能产生部分 Step 注册。

## 六、职责边界

### PlanValidator

负责 Plan 和 PlanStep 的基础字段不变量，例如：

- Plan ID；
- version；
- datetime；
- 非空字段；
- capability requirements；
- 基础类型。

### PlanGraphValidator

负责图结构不变量：

- Step ID 唯一；
- 缺失依赖；
- 自依赖；
- 重复依赖；
- 图构建；
- 入度；
- 稳定拓扑排序；
- 环检测；
- Root / Leaf 诊断。

如果当前 PlanValidator 已经处理了部分图规则，可以：

- 由 PlanGraphValidator 调用 PlanValidator；
- 抽取共享校验函数；
- 或保留兼容入口但委托给单一实现。

不得维护两套内容相同但彼此独立的图校验逻辑。

### Scheduler

只消费经过验证的 PlanGraph，负责：

- Step 注册；
- Ready 计算；
- BLOCKED 传播；
- Claim。

Scheduler 不得重新实现 Kahn 算法或 Cycle Detection。

## 七、建议新增文件

建议新增：

```text
core/runtime/plan_graph.py
tests/test_plan_graph.py
docs/learning/stage2/result/day09_dag_result.md
```

根据现有项目风格也可以使用等价文件名，但不要拆成大量小文件。

预计修改：

```text
core/runtime/scheduler.py
core/runtime/planning.py
core/runtime/__init__.py
tests/test_scheduler.py
```

不得大规模修改 AgentRouter、模型选择、API 或 AgentState Schema。

## 八、PlanGraph

建议实现不可变 `PlanGraph`，至少包含：

- `plan_id`
- `plan_version`
- `step_ids`
- `topological_order`
- `root_step_ids`
- `leaf_step_ids`
- `dependencies`
- `dependents`

要求：

- frozen dataclass 或等价不可变结构；
- 所有外部集合使用 tuple 或只读结构；
- 不保存 StepStatus；
- 不保存 AgentState；
- 不保存用户正文；
- 不保存完整 Prompt；
- 不保存模型对象；
- 不修改原始 Plan；
- 同一 Plan 必须产生完全相同的 PlanGraph。

可以增加安全查询方法：

- `dependencies_of(step_id)`
- `dependents_of(step_id)`
- `contains_step(step_id)`

不得暴露可修改内部 dict。

## 九、DAG 校验错误

建议建立安全错误类型或等价错误码：

- `DUPLICATE_STEP_ID`
- `MISSING_DEPENDENCY`
- `SELF_DEPENDENCY`
- `DUPLICATE_DEPENDENCY`
- `DEPENDENCY_CYCLE`

错误只包含：

- plan_id；
- plan_version；
- step_id；
- dependency step_id；
- cycle path；
- unresolved step IDs；
- 中文安全说明。

不得包含：

- task_summary 全文；
- description；
- completion criteria；
- 用户输入；
- RAG；
- Memory；
- Tool 参数；
- Secret。

## 十、图构建规则

构图前依次检查：

1. Step ID 唯一；
2. 每个 dependency ID 存在；
3. Step 不依赖自己；
4. 同一 Step 内不允许重复 dependency；
5. 每条依赖边只出现一次。

边方向固定为：

```text
dependency → dependent
```

例如：

```text
step-b depends_on step-a
```

构建：

```text
step-a → step-b
```

不得反向构图。

## 十一、稳定 Kahn 拓扑排序

实现 `O(V + E)` 风格的 Kahn 拓扑排序。

要求：

- `indegree[step_id] = len(depends_on)`；
- 初始零入度节点按 Plan 原始索引排序；
- 后续新释放节点也使用 Plan 原始索引作为稳定决胜规则；
- 不使用 Set 的任意遍历顺序；
- 每个 Step 在拓扑顺序中恰好出现一次；
- 依赖 Step 必须排在 dependent 之前。

可以使用：

- 按 Plan 索引维护的 heap；
- 或等价稳定候选结构。

不要每轮无条件排序全部节点造成不必要复杂度。

## 十二、环检测与诊断

当：

```text
len(topological_order) < len(step_ids)
```

说明存在环。

必须区分：

### unresolved_step_ids

Kahn 算法未能处理的全部节点。

其中可能包含：

- 真正处于环中的节点；
- 仅仅依赖环的下游节点。

### cycle_path

通过稳定 DFS 或等价算法，从剩余图中提取一个真实环路径，例如：

```text
step-a → step-b → step-a
```

要求：

- cycle_path 首尾可以使用相同 Step ID，明确闭环；
- 不得把仅依赖环的下游节点标为环成员；
- DFS 邻接顺序遵循 Plan 原始顺序；
- 相同 Plan 每次输出相同 cycle path；
- 自依赖优先返回 SELF_DEPENDENCY，而不是通用 Cycle。

不要为本次实现 Tarjan SCC、所有环枚举或最短环搜索。

## 十三、Root、Leaf 与不可达语义

输出：

- `root_step_ids`：入度为 0；
- `leaf_step_ids`：没有 dependent。

当前 Plan 没有：

- `entry_step_ids`
- `goal_step_ids`

因此：

- 所有 Root 都是合法入口；
- 多根 DAG 合法；
- 不连通的多个 DAG 组件合法；
- 不得将除第一个 Root 外的组件标记为 unreachable；
- 不得因为图不连通拒绝 Plan；
- 不得新增虚假的 `unreachable_step_ids`。

结果文档必须明确：

> 不可达必须相对于显式入口或目标定义，当前 Schema 不具备该语义。

可以输出组件数量作为诊断，但不能将多个组件视为错误。

## 十四、Scheduler 接入

DAG 校验必须发生在任何 Runtime 状态变更之前。

正确顺序：

```text
PlanGraphValidator.validate(plan)
→ 完整 Plan / AgentState 对齐预检查
→ AgentStateMachine.add_step()
```

要求：

- 非法 Plan 不得注册任何 Step；
- 非法 Plan 不得发送 STARTED / BLOCKED 等 Step Event；
- 非法 Plan 前后 AgentState 序列化完全一致；
- `prepare()`、`claim_next()` 的真实调用路径都不能绕过 DAG 校验；
- 如果 `claim_next()` 内部调用 `prepare()`，只需保证统一入口验证，不重复构图产生不同结果；
- 不修改 AgentState Schema。

建议 Scheduler 在单次调用或单实例范围复用已验证的 PlanGraph，但不得仅按 plan_id 缓存而忽略 version 或 Plan 内容变化。

如果实现缓存，Key 至少应包含：

- plan_id；
- version；
- 稳定图 fingerprint。

本次也可以不缓存，优先保证正确性。

## 十五、Plan / State 注册预检查

在添加任何缺失 Step 前，先完整检查：

- 已存在 Step ID 的名称是否与 PlanStep.title 一致；
- 所有已有 Step 是否满足当前 Scheduler 接管要求；
- 不存在明显 Plan / State 冲突。

只有全部预检查通过，才开始注册缺失 Step。

目标是避免：

```text
注册前三个 Step
→ 第四个发现名称冲突
→ AgentState 留下部分注册
```

不要求本次实现跨多个 State Machine Event 的通用事务，但必须消除可预先发现的部分注册错误。

## 十六、拓扑顺序与 Scheduler

Scheduler 的稳定 Ready / Claim 顺序应与 `PlanGraph.topological_order` 保持兼容。

可以：

- 直接使用 `topological_order` 作为候选遍历顺序；
- 或证明当前 Plan 顺序扫描与稳定拓扑决胜语义等价。

结果文档必须说明真实实现。

不得：

- 依据拓扑层进行并行；
- 一次 Claim 多个 Step；
- 新增线程池；
- 实现优先级抢占。

## 十七、静态与 Runtime 校验分离

PlanGraphValidator 不得读取：

- RunStatus；
- StepStatus；
- active_step_ids；
- Deadline；
- CancellationToken；
- Model Profile。

Scheduler / State Machine 继续负责：

- Step 是否 PENDING；
- 依赖是否在 Runtime 中 SUCCEEDED；
- 是否已有 RUNNING；
- BLOCKED 传播；
- Claim；
- 状态转移。

一张静态合法 DAG 不代表 Runtime State 一定合法。

## 十八、重点 Bad Case

结果文档必须包含：

```markdown
## 19. 重点 Bad Case
```

至少包含以下五个。

### Bad Case 1：注册部分 Step 后才发现 Plan 有环

- 类型：假设构造，除非真实检查发现
- 非法 Plan 必须在状态变更前拒绝
- AgentState 前后完全一致

### Bad Case 2：把 Kahn 剩余节点全部当成环节点

- 类型：假设构造
- 区分 unresolved_step_ids 和 cycle_path
- 下游节点不能误报为环成员

### Bad Case 3：将不连通多根 DAG 误判为不可达

- 类型：假设构造
- 当前无 entry / goal
- 所有 Root 都是合法入口
- 不连通组件不应被拒绝

### Bad Case 4：使用 Set 导致拓扑顺序不稳定

- 类型：假设构造，除非真实代码存在
- Plan 原始索引作为稳定决胜规则

### Bad Case 5：重复依赖导致入度永远无法归零

- 类型：假设构造
- 构图前拒绝重复 dependency
- 不能自动去重或误报 Cycle

每个 Bad Case 使用：

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

若发现更高价值真实问题，可以增加，但不得把设计场景描述成真实事故。

## 十九、测试要求

建议新增：

```text
tests/test_plan_graph.py
```

至少覆盖：

### 合法图

1. 单 Step
2. 线性依赖
3. 分叉
4. 汇聚
5. 多 Root
6. 多个不连通组件
7. dependent 在 Plan 中排在 dependency 前
8. 原 Plan 不被修改

### 非法结构

1. 重复 Step ID
2. 缺失依赖
3. 自依赖
4. 重复依赖
5. 两节点环
6. 三节点环
7. 多个独立环
8. 环加下游节点

### 拓扑顺序

1. 依赖始终在下游前
2. 多 Root 按 Plan 索引稳定
3. 新释放节点按 Plan 索引稳定
4. 多次验证结果一致
5. 每个 Step 恰好出现一次
6. 不依赖 Set 顺序

### 环诊断

1. unresolved 包含未被 Kahn 处理的节点
2. cycle_path 只包含真实环
3. 下游节点不在 cycle_path
4. cycle_path 稳定
5. 自依赖不误报通用 Cycle

### Root / Leaf / 可达性

1. Root 正确
2. Leaf 正确
3. 不连通 DAG 合法
4. 不虚构唯一入口
5. 不产生误导性 unreachable 错误

### Scheduler 集成

1. 非法环 Plan 不注册 Step
2. 缺失依赖 Plan 不注册 Step
3. 重复依赖 Plan 不注册 Step
4. 非法 Plan 前后 AgentState 一致
5. 合法 DAG 正常 prepare
6. Claim 顺序与稳定拓扑顺序兼容
7. Scheduler 不重新实现 Cycle Detection
8. 不实现并行

### 回归

1. Scheduler 测试通过
2. Planning 测试通过
3. Model Selection 测试通过
4. Context Builder 测试通过
5. State Machine 测试通过
6. AgentLoop 测试通过
7. 全仓测试通过

不得：

- 调用真实模型；
- 加载 GGUF；
- 启动 Chroma；
- 启动 UI、FastAPI 或数据库；
- 访问外部网络。

## 二十、测试与检查

执行：

```text
python -m pytest \
  tests/test_plan_graph.py \
  tests/test_scheduler.py \
  tests/test_planning.py \
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
  tests.test_plan_graph \
  -q

python -m pytest -q

python -m compileall -q core tests
git diff --check
```

当前 Linux 环境如果因 Windows 本地 `llama_cpp_python` Wheel 无法执行 `uv run`，只记录环境限制，不修改 DAG 业务代码。

## 二十一、禁止事项

不得：

- 修改 Plan Schema 增加 entry 或 goal；
- 错误实现不可达检测；
- 实现所有环枚举；
- 实现 Tarjan SCC；
- 实现并行执行；
- 实现拓扑层并行；
- 实现优先级调度；
- 实现分布式锁；
- 实现 Step Executor；
- 迁移默认 Legacy AgentLoop；
- 修改 Model Selection；
- 修改 AgentState Schema；
- 修改 API；
- 修改 Memory Schema；
- 修改流式协议；
- 修改 `[[ORCH]]`；
- 大规模重写 AgentRouter。

## 二十二、结果文档

创建：

```text
docs/learning/stage2/result/day09_dag_result.md
```

必须包含：

# 阶段二第 9 天改造结果

## 1. 本次任务目标

## 2. 修改前依赖校验现状

## 3. PlanValidator 与 DAG Validator 边界

## 4. 最终设计方案

## 5. 新增文件

## 6. 修改文件

## 7. PlanGraph

## 8. 图构建与边方向

## 9. 入度与稳定拓扑排序

## 10. 环检测和 cycle path

## 11. Root、Leaf 和不可达语义

## 12. Scheduler 接入边界

## 13. 非法 Plan 的状态原子性

## 14. 静态校验与 Runtime 校验区别

## 15. 时间与空间复杂度

## 16. 测试命令和结果

## 17. 未完成事项和已知风险

至少说明：

- 未实现 entry / goal；
- 未实现传统不可达检测；
- 未实现所有环枚举；
- 未实现 SCC；
- 未实现并行；
- 默认 Legacy AgentLoop 尚未消费 Scheduler；
- Plan 尚未持久化；
- Plan / AgentState 尚未持久绑定；
- 跨进程 Claim 尚未实现。

## 18. 设计权衡和面试描述

## 19. 重点 Bad Case

至少五个。

## 20. 需要带回 ChatGPT 审查的信息

必须包含：

- PlanGraph 文件和入口；
- DAG Validator 入口；
- PlanValidator 与 DAG Validator 分工；
- 边方向；
- indegree 计算；
- 稳定拓扑算法；
- 稳定决胜规则；
- root / leaf 语义；
- 不可达语义；
- cycle 检测方式；
- cycle_path；
- unresolved_step_ids；
- Scheduler 校验入口；
- 非法 Plan 是否修改 AgentState；
- Scheduler 是否使用 topological_order；
- 是否实现并行；
- 是否修改 Plan / AgentState Schema；
- 时间与空间复杂度；
- 测试命令和结果；
- Bad Case；
- 需要人工确认的问题；
- 后续建议，但不得实施第 10 天内容。

## 二十三、聊天最终输出

完成后输出：

结果文档路径：

新增文件：

修改文件：

PlanGraph 入口：

DAG Validator 入口：

PlanValidator 分工：

DAG Validator 分工：

边方向：

indegree：

稳定拓扑算法：

稳定决胜规则：

root step：

leaf step：

不可达语义：

cycle detection：

cycle_path：

unresolved_step_ids：

Scheduler 校验入口：

非法 Plan 是否修改 AgentState：

Scheduler 是否使用 topological_order：

是否实现并行：

是否修改 Plan Schema：

是否修改 AgentState Schema：

复杂度：

测试命令：

测试结果：

Bad Case：

需要人工确认的问题：

------

# 二十三、Codex 结果审查重点

结果返回后重点检查：

1. 图边方向是否为 dependency → dependent；
2. 入度是否等于 Step 的依赖数量；
3. 重复依赖是否在构图前拒绝；
4. 缺失依赖是否没有生成虚拟节点；
5. 自依赖是否使用专用错误；
6. Step ID 重复是否在节点 Map 构建前发现；
7. Kahn 候选节点是否稳定；
8. 新释放节点是否仍按 Plan 索引排序；
9. 是否错误使用 Set 决定拓扑顺序；
10. Kahn 剩余节点是否被错误称为完整 cycle；
11. cycle path 是否只包含真实环；
12. 环下游节点是否排除在 cycle path 外；
13. 多根 DAG 是否被错误判定不可达；
14. 不连通 DAG 是否仍然合法；
15. 是否擅自增加 entry / goal；
16. DAG Validator 是否不读取 AgentState；
17. Scheduler 是否不重新实现环检测；
18. 非法 Plan 是否在注册任何 Step 前失败；
19. 非法 Plan 前后 AgentState 是否完全一致；
20. Scheduler 顺序是否与稳定拓扑顺序兼容；
21. 是否提前实现拓扑层并行；
22. 是否修改 Plan 或 AgentState Schema；
23. 测试是否覆盖真实环与环下游；
24. Bad Case 是否准确区分真实与构造；
25. 注释、Docstring 和自然语言错误信息是否以中文为主。

------

# 二十四、面试高频问题

## 1. 为什么 Scheduler 不能自己处理环？

> 环属于 Plan 的静态结构错误，应在运行前拒绝。Scheduler 只负责读取合法图和 Runtime 状态，计算 Ready Step 与 Claim。

## 2. 如何实现稳定拓扑排序？

> 使用 Kahn 算法计算入度，并以 Plan 原始索引作为所有零入度节点的稳定决胜规则，确保相同 Plan 总是得到相同顺序。

## 3. Kahn 算法剩余节点是否都在环中？

> 不一定。剩余节点还可能只是依赖了环。应将它们称为 unresolved nodes，再通过稳定 DFS 提取真正的 cycle path。

## 4. 如何判断不可达 Step？

> 可达性必须相对于显式入口定义。当前 Plan 没有 entry_step_ids，所有零入度节点都是合法入口，因此不能将不连通组件判为不可达。

## 5. 为什么要在 Step 注册前完成 DAG 校验？

> 如果边注册边校验，后续发现环时 AgentState 已经留下部分 PENDING Step。完整静态验证和 Plan/State 预检查必须发生在任何状态变更前。

## 6. DAG 校验与 Runtime 校验有什么区别？

> DAG 校验检查静态依赖结构；Runtime 校验检查 Step 当前状态、依赖是否已成功、是否被重复 Claim，以及状态转移是否合法。

------

# 二十五、第 9 天验收清单

## 理论验收

-  理解依赖图
-  理解边方向
-  理解入度
-  理解 Kahn 拓扑排序
-  理解稳定拓扑顺序
-  理解环检测
-  区分 unresolved 与 cycle
-  理解多根 DAG
-  理解不可达的前置语义
-  区分静态校验与 Runtime 校验

## 项目验收

-  `PlanGraph` 已实现
-  `PlanGraphValidator` 已实现
-  重复 Step ID 被拒绝
-  缺失依赖被拒绝
-  自依赖被拒绝
-  重复依赖被拒绝
-  稳定拓扑顺序已实现
-  环检测已实现
-  cycle path 已实现
-  unresolved IDs 语义准确
-  Root / Leaf 已输出
-  多根 DAG 合法
-  不连通 DAG 合法
-  未错误实现不可达检测
-  DAG 校验在 Step 注册前执行
-  非法 Plan 不修改 AgentState
-  Scheduler 消费验证结果
-  未实现并行
-  未修改 Plan Schema
-  未修改 AgentState Schema
-  专项和全仓测试通过
-  Bad Case 完整
-  完成 ChatGPT 审查

## 阶段二进度

**第 9/25 天：理论与架构设计完成，等待 Codex 改造结果审查。**

下一天主题：**第 10 天 Executor（执行器）边界——定义 `StepClaim → Executor → Step Outcome`，迁移 Step 注册和 `STARTED` 所有权，并避免 Scheduler 与 Legacy AgentLoop 双重生命周期。**