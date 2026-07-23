# 阶段二第 9 天改造结果

## 1. 本次任务目标

为不可变 `Plan` 构建静态、不可变的 `PlanGraph`，在 Runtime 注册任何 Step 前拒绝重复 Step ID、缺失依赖、自依赖、重复依赖和依赖环，并给出稳定拓扑顺序。

## 2. 修改前依赖校验现状

修改前 `PlanValidator` 同时负责基础字段和重复 ID、缺失依赖、自依赖、重复依赖；`SerialScheduler` 又直接扫描 `PlanStep.depends_on` 进行 Ready 和 BLOCKED 判断，但没有环检测。`prepare()` 在旧校验通过后才预检名称并注册；`claim_next()` 隐式调用 `_prepare_locked()`。Ready/Claim 顺序按原始 `Plan.steps` 扫描；没有任何环检测，环只会表现为 unresolved pending。

## 3. PlanValidator 与 DAG Validator 边界

`PlanValidator` 现在只校验 Plan/PlanStep 基础字段：标识、版本、UTC 时间、非空字段、基础类型和 capability requirements。`PlanGraphValidator.validate(plan)` 先调用它，再集中处理所有图规则；因此图规则只有一个实现，不会与兼容入口发生漂移。

## 4. 最终设计方案

新增单文件 `core/runtime/plan_graph.py`，提供 `PlanGraph`、`PlanGraphValidator` 和安全的 `PlanGraphValidationError`。Scheduler 只消费验证完成的图，未重新实现 Kahn 或环检测。

## 5. 新增文件

- `core/runtime/plan_graph.py`
- `tests/test_plan_graph.py`
- `docs/learning/stage2/result/day09_dag_result.md`

## 6. 修改文件

- `core/runtime/planning.py`
- `core/runtime/scheduler.py`
- `core/runtime/__init__.py`
- `tests/test_scheduler.py`

未修改 Plan/AgentState Schema、默认 Legacy AgentLoop、模型选择、API、Memory 或流式协议。

## 7. PlanGraph

`PlanGraph` 是 frozen、slots dataclass，保存 `plan_id`、`plan_version`、`step_ids`、`topological_order`、root/leaf IDs 和只读 dependency 映射。映射值均为 tuple，并通过 `MappingProxyType` 只读暴露；它不保存 Runtime 状态、用户正文、Prompt、模型或 AgentState，也不修改原 Plan。

## 8. 图构建与边方向

构图前固定按重复 Step ID、缺失依赖、自依赖、重复依赖检查。边方向严格为 `dependency → dependent`；例如 `b depends_on a` 构造 `a → b`。dependents 按 Plan 原始索引排序。

## 9. 入度与稳定拓扑排序

入度为 `len(depends_on)`。使用 heap 驱动的 Kahn：初始零入度节点和后续释放节点都以 Plan 原始索引作为唯一稳定决胜规则，不依赖 Set 迭代，也不会每轮全量排序。Scheduler 的 Ready、Snapshot 和 Claim 候选直接按 `graph.topological_order` 遍历，因此与该规则兼容。

## 10. 环检测和 cycle path

Kahn 输出数少于步骤数时，`unresolved_step_ids` 保存全部未处理节点（可含环的下游）。随后在剩余图按 Plan 索引和稳定邻接顺序 DFS，提取一个真实闭环 `cycle_path`；下游不被写入 path。自依赖在 DFS 前以 `SELF_DEPENDENCY` 拒绝。未实现所有环枚举或 SCC。

## 11. Root、Leaf 和不可达语义

root 是入度为零的步骤，leaf 是没有 dependent 的步骤。多 root、多组件 DAG 都合法。**不可达必须相对于显式入口或目标定义，当前 Schema 不具备该语义。** 因此没有虚构 `unreachable_step_ids`，也不会拒绝不连通组件。

## 12. Scheduler 接入边界

`prepare()` 和 `claim_next()` 共同经过 `_prepare_locked()`：`PlanGraphValidator.validate(plan) → Plan/State 对齐预检 → AgentStateMachine.add_step()`。`evaluate()` 也先验证图再读取 Runtime 状态。Scheduler 继续负责 Runtime 的 PENDING/SUCCEEDED、RUNNING、BLOCKED 传播和单 Step Claim。

## 13. 非法 Plan 的状态原子性

静态 DAG 校验位于 `state.validate()`、绑定和 `add_step()` 之前。因而非法 Plan 不注册任何 Step、不发送 STARTED/BLOCKED 事件，`AgentState.to_dict()` 前后一致。名称冲突仍由完整预检在任何缺失 Step 注册前拒绝，所以不存在可预先发现的部分注册。

## 14. 静态校验与 Runtime 校验区别

Graph validator 不读取 RunStatus、StepStatus、active IDs、Deadline、CancellationToken 或 Model Profile。Runtime Scheduler/StateMachine 才判断状态、依赖是否已成功、阻断传播和状态转换；静态合法 DAG 不表示 Runtime State 必然合法。

## 15. 时间与空间复杂度

构图、入度、root/leaf 和 Kahn 的基础遍历为 `O(V + E)`；为使用 Plan 原始索引的 heap，稳定拓扑的候选入队/出队为 `O(V log V)`。当前实现还会按 Plan 原始索引排序每个步骤的 dependent 列表，最坏为 `O(E log V)`；环诊断启动 DFS 前排序 unresolved 步骤，最坏为 `O(V log V)`。因此完整 `PlanGraphValidator.validate()` 的保守时间复杂度上界为 `O(V + E + V log V + E log V)`，即 `O((V + E) log V)`；空间复杂度为 `O(V + E)`。稳定 DFS 本体最坏为 `O(V + E)`。

## 16. 测试命令和结果

- `python -m pytest tests/test_plan_graph.py tests/test_scheduler.py tests/test_planning.py -q`：40 passed, 9 subtests passed。
- `python -m unittest tests.test_runtime_context tests.test_agent_state tests.test_agent_loop tests.test_state_machine tests.test_model_context tests.test_planning tests.test_model_selection tests.test_scheduler tests.test_plan_graph -q`：Ran 100 tests，OK。
- `python -m pytest -q`：collection 阶段因环境缺少 `requests` 与 `langchain_chroma` 失败，未执行到 DAG 相关断言；未安装依赖或修改业务代码。
- `python -m compileall -q core tests`、`git diff --check`：通过。上述检查均不调用模型、GGUF、Chroma 服务、UI、API、数据库或网络。

## 17. 未完成事项和已知风险

- 未实现 entry/goal，故未实现传统不可达检测。
- 未实现所有环枚举或 SCC。
- 未实现并行、拓扑层并行、优先级或抢占。
- 默认 Legacy AgentLoop 尚未消费 Scheduler。
- Plan 尚未持久化，Plan/AgentState 尚未持久绑定。
- 跨进程 Claim 尚未实现。

## 18. 设计权衡和面试描述

选择“基础校验与图校验分层、图规则集中、Scheduler 消费图”的小边界，而不是把 DAG 算法散落在 Scheduler。面试可表述：先以稳定 Kahn 在静态边界拒绝不可能调度的 Plan，再以状态机维护执行事实；Kahn 剩余集合用于诊断，DFS path 才是实际环，避免把下游误判为环成员。

## 19. 重点 Bad Case

### Bad Case 1：注册部分 Step 后才发现 Plan 有环

- 类型：假设构造
- 触发条件：两个 Step 相互依赖。
- 故障表现：旧行为可注册后永久 pending。
- 根因分析：环检测缺失且注册在图校验之前。
- 修复方案：先 `PlanGraphValidator.validate()`，再进行任何 State 变更。
- 回归测试：`test_cycle_is_rejected_before_any_step_registration`。
- 对应知识点：失败前置、状态原子性。
- 面试表达：静态非法输入不能留下 Runtime 残留。
- 当前状态：已覆盖，State 序列化不变。

### Bad Case 2：把 Kahn 剩余节点全部当成环节点

- 类型：假设构造
- 触发条件：环后还有仅依赖环的 downstream。
- 故障表现：下游被错误报告为环成员。
- 根因分析：unresolved 是 Kahn 诊断集合，不是 SCC。
- 修复方案：稳定 DFS 只返回真实 `cycle_path`。
- 回归测试：`test_cycle_diagnostics_exclude_downstream_and_are_stable`。
- 对应知识点：拓扑诊断与环见证。
- 面试表达：remaining nodes 包含影响范围，cycle path 才是见证。
- 当前状态：已覆盖。

### Bad Case 3：将不连通多根 DAG 误判为不可达

- 类型：假设构造
- 触发条件：多个 root 或独立组件。
- 故障表现：合法 Plan 被拒绝。
- 根因分析：错误假设第一个 root 是唯一入口。
- 修复方案：所有 root 合法；不产生 unreachable 错误。
- 回归测试：`test_legal_graphs_are_stable` 的多 root/多组件参数。
- 对应知识点：可达性必须有入口语义。
- 面试表达：没有 entry/goal 时不能定义传统不可达。
- 当前状态：已覆盖。

### Bad Case 4：使用 Set 导致拓扑顺序不稳定

- 类型：假设构造
- 触发条件：多个 root 或多个同时释放节点。
- 故障表现：重复运行顺序不同。
- 根因分析：把无序成员集合当作调度队列。
- 修复方案：heap key 使用 Plan 原始索引，邻接也稳定排序。
- 回归测试：`test_legal_graphs_are_stable`、`test_cycle_diagnostics_exclude_downstream_and_are_stable`。
- 对应知识点：确定性调度。
- 面试表达：成员检查可用 Set，决策顺序必须有稳定来源。
- 当前状态：已覆盖。

### Bad Case 5：重复依赖导致入度永远无法归零

- 类型：假设构造
- 触发条件：`b depends_on (a, a)`。
- 故障表现：若建边去重但按原列表计入度，会错误看似有环。
- 根因分析：边集合和入度口径不一致。
- 修复方案：构图前以 `DUPLICATE_DEPENDENCY` 拒绝，绝不静默去重。
- 回归测试：`test_illegal_graphs_have_safe_error_codes`。
- 对应知识点：输入规范化与算法前置条件。
- 面试表达：重复边是非法输入，不应由拓扑算法猜测修复。
- 当前状态：已覆盖。

## 20. 需要带回 ChatGPT 审查的信息

- PlanGraph 入口：`core/runtime/plan_graph.py: PlanGraphValidator.validate`，并由 `core.runtime` 导出。
- DAG Validator 先调用基础 `PlanValidator`；边为 dependency 到 dependent；入度为依赖数。
- 使用 heap Kahn，Plan 原始索引稳定决胜；root/leaf 为纯结构诊断。
- 无 entry/goal，故没有不可达判定；Kahn + 稳定 DFS 提供 unresolved 与 cycle path。
- Scheduler 在注册前校验，非法 Plan 不修改 AgentState，并用 topological_order 排序；没有并行，也没有 Schema 修改。
- 请人工确认未来显式 Executor 如何接收 StepClaim、何时迁移 Legacy Loop 生命周期所有权，以及 Plan/State 持久绑定和跨进程 Claim 的生产方案；后续可讨论但本次不实施第 10 天内容。
