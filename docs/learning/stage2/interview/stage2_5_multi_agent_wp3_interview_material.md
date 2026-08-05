# LocalAgent Stage 2.5 Multi-Agent WP3 面试材料

> 适用范围：Stage 2.5 Multi-Agent WP3——建立真实的多 Agent 执行与运行期结果数据流：Scheduler 并行 claim -> MultiAgentDriver -> AgentAdapterFactory -> specialist -> typed StepResult -> StepResultStore -> dependency-scoped 结果视图 -> synthesis；并在 WP4 交付前用 `FINAL_OUTPUT_PIPELINE_NOT_READY` 保护用户输出边界。
>
> 真实性声明：本文中的“真实发现”仅指本地项目源码审查、实施或测试中实际观察到的问题，不等同于线上生产事故；“假设构造”只用于风险推演，不描述成真实事故。

## 1. 推荐的面试材料模板

一份可信、便于追问的工程面试材料应包含：

1. 一句话项目定义：先说明解决的用户问题，再介绍技术方案。
2. 真实性与边界：区分用户真实复现、源码审查发现、实施测试发现和假设构造。
3. 原始用户场景：给出真实输入、预期链路、实际链路和风险。
4. 架构演进：说明旧架构为什么无法满足要求，以及 WP1、WP2、WP3 分别解决什么。
5. 方案讨论：展示候选方案、取舍标准和被拒绝方案。
6. 核心状态机与时序：说明成功、失败、取消、超时的确定性行为。
7. 数据与权限边界：谁有权决定 Agent、instruction、结果写入和最终输出。
8. 兼容策略：旧入口、单 Step、静态 Plan、Snapshot 和 Streaming 如何迁移。
9. Bad Cases：统一格式，明确真实性，写清触发、根因、修复和回归。
10. 验收证据：给出测试命令、通过数量和仍未实现的能力。
11. 面试表达：准备 30 秒、2 分钟和深入追问三个版本。

可复用骨架：

```markdown
# 项目 / 工作包名称

## 真实性声明
## 一句话项目定义
## 真实用户场景
## 旧架构与故障证据
## 方案讨论与取舍
## 核心架构和状态机
## 实际实现
## 安全、失败与兼容合同
## Bad Cases
## 测试和验收证据
## 当前边界与下一步
## 面试表达
```

Bad Case 固定使用：

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

## 2. 一句话项目定义

WP3 的目标是把 WP2 的“能规划但不能执行的多 Step Plan”升级为真实执行链：多个 specialist 由 Scheduler 并行 claim，经 `MultiAgentDriver -> AgentAdapterFactory` 调用真实 Agent，产生 typed `StepResult`，由最小提交骨架写入 `StepResultStore`（PREPARED -> READABLE），synthesis 只通过 dependency-scoped 视图消费显式依赖结果并恰好生成一次 final candidate。

WP3 并没有开放用户可见的多 Agent 闭环。所有 multi-step Step 都不产生 `OUTPUT_DELTA`；当所有 Step 成功且唯一 final StepResult READABLE 后，Run 以 `FAILED / UNHANDLED_ERROR / FINAL_OUTPUT_PIPELINE_NOT_READY` 结束——这是 WP3 与 WP4 之间的安全施工边界，不是最终共识中的正式业务错误。

## 3. 真实用户场景与问题价值

用户在主 Agent 输入：

```text
分别让代码专家和知识专家审查这个模块，然后综合两份结论给出最终建议
```

用户期望：

```text
主入口 -> 识别 code + knowledge 两个任务
-> 两个 specialist 并行执行
-> 两份 INTERNAL 结果只在本 Run 内传递
-> synthesis 只基于两份结果生成唯一 final answer（WP4 交付）
```

WP2 之前该场景的真实行为：合法多 Step Plan 会在任何 Step 开始前以 `MULTI_AGENT_EXECUTION_NOT_READY` fail closed，系统只能声称“规划已接入”，不能声称“专业能力已执行”。WP3 的价值不是多写一个路由，而是把“能力是否真的被执行、结果是否真的在 Run 内传递、最终输出是否真的由 synthesis 生成”变成有状态机、有 ACL、有容量限制、有安全边界、有测试证据的合同。

WP3 之后的诚实表述：多 Agent 内部执行已真实可用；用户可见的多 Agent final 尚未交付（WP4 OutputGate），因此仍不能宣称 Stage 2.5 多 Agent 闭环完成。

## 4. 架构演进

### 4.1 WP1 已提供的能力

- `AgentRegistry`：Agent 身份、entry/delegated 权限和执行适配标识（`execution_adapter_id`）的事实源。
- `PlanResolver` / `StrictPlanningDecisionParser`：显式 Agent、确定性规则与模型规划的统一 typed decision。
- `PlanCompiler`：按 Registry 和四种固定图规则生成安全 Plan，并拒绝非法 fan-out（`supports_parallel=False`）。
- `StepInvocationBindings`：run-scoped 保存真实 instruction，不进入 Plan/Snapshot。

### 4.2 WP2 补齐的运行时断点

- 默认 Coordinated API 强制进入 Resolver；dynamic Plan 生命周期 `UNRESOLVED -> RESOLVING -> FROZEN`。
- 单 Step 动态执行（`ResolvedSingleStepDriver`）保留旧 `OUTPUT_DELTA`。
- 合法多 Step Plan 在任意 Step 前 fail closed（`MULTI_AGENT_EXECUTION_NOT_READY`），等待 WP3 的执行链。

### 4.3 WP3 补齐的执行链

```text
Frozen Plan
  -> Scheduler 并行 claim
  -> MultiAgentDriver
  -> AgentAdapterFactory
  -> specialist Agent（persist=False）
  -> typed StepResult
  -> StepResultCommitter（唯一写权）
  -> StepResultStore（PREPARED -> READABLE）
  -> dependency-scoped result view
  -> synthesis_agent
  -> typed final StepResult（仅存在于 Store）
  -> FINAL_OUTPUT_PIPELINE_NOT_READY（WP4 前临时保护）
```

WP2 gate 随以上能力在同一变更中原子删除；单 Step 与 Legacy 行为保持不变。

## 5. 方案讨论与取舍

| 方案 | 优点 | 风险或不足 | 结论 |
|---|---|---|---|
| 让 MultiAgentDriver 直接写 Store | 代码路径最短 | 违反“Driver 只调用、不拥有结果存储”；一次绕过后所有 ACL/once-write 都失去意义 | 拒绝 |
| 让 Adapter 写 Store 或 AgentState | Adapter 复用现有 Agent 调用 | Adapter 会持有 Run 生命周期职责，无法保持无 Run 状态 | 拒绝 |
| 在 Driver 内按 agent_id 写 `if/elif` 分支 | 实现直观 | 新增 Agent 必须改 Driver；违反 Registry/Factory 符号绑定 | 拒绝，改为 `AgentAdapterFactory` 按 `execution_adapter_id` 解析 |
| 保留 WP2 gate 逐步补能力 | 风险最小 | 双轨执行 + 能力声明不诚实；WP2 文档明确要求 gate 与能力同一变更删除 | 拒绝，原子删除 gate 并同时加入 final-output 保护 |
| 直接实现 WP4 OutputGate/DeliveryStatus | 一步到位 | 超出 WP3 授权边界；delivery 语义（unknown/partial publication）尚未定稿 | 拒绝，用 `FINAL_OUTPUT_PIPELINE_NOT_READY` 临时保护并留 WP4 removal marker |
| 用“返回字符串=final、返回对象=internal”决定输出 | 无新参数 | 隐式技巧脆弱，类型错误会静默改变输出语义 | 拒绝，Executor 显式 typed mode |
| 异步 `AgentExecutionAdapter.execute` | 符合常见异步风格 | 现有 `complete_single_agent` 是同步合同，且必须在 bounded executor 线程中执行 | 采用同步 execute（文档说明） |
| 所有 Step 共用默认 resource key（limit=1） | 实现简单 | 会把独立 specialist 串行化，违背真实并行 | 拒绝，typed mode 每 Step 独立 resource key，全局 `max_concurrency` 仍约束批次 |

关键选择是：结果存储的所有权必须单一化。Store 只能由最小提交骨架写，Driver 只负责调用并返回 typed result，Synthesis 只读显式依赖。

## 6. 核心数据流与状态机

### 6.1 Store 状态机

```text
条目：PREPARED -> READABLE
Store：OPEN -> SEALED -> CLEARED
```

- 每 logical Step 只允许一次成功 prepare；重复写入/重复 completion 回调 fail closed。
- `SEALED` 在 Run 终结时立即生效，拒绝新写入与读取；`CLEARED` 在无存活 worker 的安全生命周期点幂等执行并释放 raw content。

### 6.2 提交顺序（INTERNAL 与 synthesis 相同）

```text
Driver returns StepResult
-> acquire completion guard
-> validate claim/result
-> Store.write_prepared
-> Step RUNNING -> SUCCEEDED
-> Store.mark_readable
-> STEP_COMPLETED(SUCCEEDED)
-> safe StepCompletionResult（synthesis: final_result_ready=True）
```

synthesis 不调用 OutputGate、不发布 `OUTPUT_DELTA`；Coordinator 在所有 Step 成功且 final READABLE 后返回 `FINAL_OUTPUT_PIPELINE_NOT_READY`。

### 6.3 容量与并发

- 单结果 20_000 字符、Run 总 60_000 字符、条目 16（Factory 可配置）；越限 fail closed，不静默截断。
- `max_concurrency` 默认 2（effective 为 `min(policy.max_concurrency, budget.max_concurrency)`）；typed mode 每个 Step 使用独立 resource key。

## 7. 最小结果提交骨架与并行执行

### 7.1 StepResultCommitter

Store 不能由 Driver 写，因此 WP3 建立受限提交 owner，只负责：

```text
result validation
-> Store PREPARED
-> Step state terminal
-> Store READABLE
-> STEP_COMPLETED
-> safe result report
```

文档明确：WP3 只实现 result/state 分支；WP4 才实现 OutputGate 和 delivery 分支。

### 7.2 ParallelExecutor typed mode

- 构造/调用时注入 completion owner；typed mode 下 Driver 必须返回 `StepResult`，Executor 不按 `isinstance(result, str)` 发布输出。
- INTERNAL 与 synthesis 都不产生 `OUTPUT_DELTA`；raw `StepResult` 不进入 `StepExecutionOutcome` raw 字段和 Batch report。
- Batch report 只包含安全完成 metadata（`StepCompletionResult`）；Coordinator 不再丢弃 multi-step report，并在下一次 Scheduler 成功判断前消费提交失败。
- 取消/超时/预算沿用既有语义；`_cancel_unfinished` 跳过已终态 Step，避免重复提交。

### 7.3 并行证据

Shape 3 测试用两方 `threading.Barrier(2)`、共享 active 计数（`max_active >= 2`）和 enter/exit 事件顺序证明：两个 specialist 同时位于执行入口、均在任一 specialist 退出前进入；synthesis 只在两个 specialist 全部 SUCCEEDED 且结果 READABLE 后开始。不用单纯总耗时作为唯一证据。

## 8. 多 Step 防线与 WP2 gate 移除

WP2 的 `MULTI_AGENT_EXECUTION_NOT_READY` gate 已随以下能力在同一变更中全部可用后原子删除：

- MultiAgentDriver；AgentAdapterFactory；StepResultStore；result commit；INTERNAL 无输出；synthesis dependency view；required fail-closed；final-output not-ready 临时保护。

删除后：

- 不再出现 `MULTI_AGENT_EXECUTION_NOT_READY`；
- specialist 真实开始、synthesis 真实执行；
- 仍无 `OUTPUT_DELTA`；
- 最终以 `FINAL_OUTPUT_PIPELINE_NOT_READY` 失败（带 WP4 removal marker）。

required dependency fail-closed：任一 specialist FAILED/CANCELLED/TIMEOUT/结果 prepare 失败/不可 READABLE/容量失败时，synthesis 不得调用，其 Step 收敛为 BLOCKED，Run 以 `REQUIRED_DEPENDENCY_FAILED` 失败；不读取部分结果、不删除失败依赖、不回退 Core、不拼接。

## 9. 事件、错误和安全合同

### 9.1 事件序列（E2E 真实验证）

```text
RUN_STARTED
PLANNING_STARTED
PLAN_CREATED
STEP_STARTED(task-code)
STEP_STARTED(task-knowledge)
STEP_COMPLETED(task-code, SUCCEEDED)
STEP_COMPLETED(task-knowledge, SUCCEEDED)
STEP_STARTED(synthesis)
STEP_COMPLETED(synthesis, SUCCEEDED)
ERROR(FINAL_OUTPUT_PIPELINE_NOT_READY)
RUN_COMPLETED(FAILED)
```

单 Step（Core direct、explicit entry、单 delegated knowledge direct）继续走 `ResolvedSingleStepDriver` 旧字符串输出，事件顺序未变。

### 9.2 错误分类

| 失败点 | Step 状态 | Run error |
|---|---|---|
| Driver 失败 | FAILED | AGENT_STEP_FAILED / SYNTHESIS_FAILED |
| result 非法 | FAILED | STEP_RESULT_INVALID |
| prepare 失败（含容量） | FAILED | STEP_RESULT_PREPARE_FAILED |
| Step 成功状态提交失败 | RUNNING（终态 settle） | STEP_STATE_COMMIT_FAILED |
| mark readable 失败 | SUCCEEDED | STEP_RESULT_COMMIT_FAILED |
| STEP_COMPLETED 事件失败 | 已 terminal | STEP_COMPLETION_EVENT_FAILED |
| 重复 completion / 迟到结果 | 按实际状态 | STEP_RESULT_DUPLICATE_COMMIT / STEP_RESULT_LATE_COMMIT |
| producer 失败导致 synthesis BLOCKED | 失败者 FAILED、synthesis BLOCKED | REQUIRED_DEPENDENCY_FAILED |
| 所有 Step 成功且 final READABLE（WP4 前） | 全部 SUCCEEDED | FINAL_OUTPUT_PIPELINE_NOT_READY |

取消沿用 `RunStatus.CANCELLED`；deadline 沿用 `DEADLINE_EXCEEDED`；budget 沿用 `BUDGET_EXHAUSTED`。

### 9.3 敏感数据边界

安全测试使用 `SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST` 与 `\\internal\private\file.dat`，断言其不出现于 Runtime Event、Journal、Snapshot、Trace、structured log、Batch report、异常字符串和 Memory。

允许出现的位置仅限：`StepResult`、Store 内存、dependency result view、Synthesis model 输入、synthesis Adapter 调用栈。

- specialist 与 synthesis 调用一律 `persist=False`；统一入口的 Memory 写入由 `if persist:` 守卫。
- `AgentExecutionRequest`/`AgentAdapterResult`/`StepResult`/`DependencyResultView` 均非 dataclass、安全 repr、不可 pickle。
- Store/Bindings 不持久化、不恢复；Run 终结时 Store seal/clear、Bindings `close_and_clear()`。

## 10. Snapshot、Checkpoint 与恢复兼容

- Plan Snapshot/checkpoint 合同未变（WP2 的 v2 与 `POST_PLAN_PRE_EXECUTION`）。
- Store、Bindings、raw StepResult 有意不持久化；需要它们的动态 Run 恢复继续 fail closed，不伪称可恢复。
- 多 Step 执行不改变 Plan/fingerprint 的“只含安全执行合同、不含 instruction/result/digest”边界。

## 11. 高价值 Bad Cases

### Bad Case 1：默认共享 resource key 把独立 specialist 串行化

- 类型：真实发现（WP3 实施测试中观察到，不是生产事故）
- 触发条件：Shape 3 两个独立 specialist 进入同一 ready batch；未配置 concurrency_specs 时两者使用默认 `StepConcurrencySpec()`（resource_key="default", limit=1）。
- 故障表现：并行证据测试失败——第二个 specialist 一直等待共享信号量，两方 barrier 超时，Run 以 `REQUIRED_DEPENDENCY_FAILED` 结束。
- 根因分析：Executor 默认把“default”当作全 Step 共享资源，串行化所有 Step；这对旧单 Step 场景无害，但会破坏“独立 specialist 必须真实并行”的合同。
- 修复方案：typed mode 为每个 Step 分配独立 resource key（`step:<id>`, limit=1），全局 `max_concurrency` 仍约束批次容量；保留显式 concurrency_specs 覆盖能力。
- 回归测试：Shape 3 barrier + active 计数 + 事件顺序断言通过；原有 resource-limit 冲突与并发测试通过。
- 对应知识点：信号量语义、资源隔离、默认值对并发语义的影响。
- 面试表达：复用现有并发原语时，默认 key 的含义会改变行为；我把它改成每 Step 独立 key，而不是放宽全局上限。
- 当前状态：已修复并纳入全量回归。

### Bad Case 2：接线后 Executor 尚未接收 completion_owner 参数

- 类型：真实发现（WP3 接线回归测试中观察到，不是生产事故）
- 触发条件：Coordinator 以 typed mode 调用 `execute_ready(..., completion_owner=...)`，但 `execute_ready` 签名尚未补齐该参数。
- 故障表现：`RunCoordinator` 一组既有用例报 `TypeError: unexpected keyword argument`，Run 被吞成 `COORDINATOR_INFRASTRUCTURE_ERROR`。
- 根因分析：先改了调用方、后补被调方签名，接线顺序不一致。
- 修复方案：给 `execute_ready`/`execute` 增加 `completion_owner` 参数并统一透传逻辑。
- 回归测试：Coordinator/Executor 回归 64 passed 后继续扩展 WP3 用例。
- 对应知识点：接口演化、参数透传、接线顺序。
- 面试表达：接线类错误要用真实调用链测试尽早暴露，而不是靠类型推断。
- 当前状态：已修复。

### Bad Case 3：Windows 时钟粒度导致“并行证据”不可信

- 类型：真实发现（WP3 测试侧观察到，不是产品缺陷）
- 触发条件：用 `time.monotonic()` 时间戳断言两个 specialist 执行区间重叠。
- 故障表现：本机 `time.monotonic()` 粒度约 15ms，所有 enter/exit 时间戳相同，断言退化为恒真/恒假。
- 根因分析：把“并行”等同于时间戳比较，忽略了时钟分辨率。
- 修复方案：改用两方 `threading.Barrier(2)` + 共享 active 计数 + enter/exit 事件顺序作为并行证据；barrier 本身就是“两者同时在场”的结构性证明。
- 回归测试：Shape 3 断言 `max_active >= 2`、两个 enter 先于任一 exit、synthesis enter 晚于全部 specialist exit。
- 对应知识点：事件顺序 vs 墙钟、并发证据设计、测试可复现性。
- 面试表达：并行证明应基于同步原语的进入/退出事件，而不是总耗时。
- 当前状态：已修复。

### Bad Case 4：WP2 gate 测试仍断言旧错误码

- 类型：真实发现（WP3 测试侧观察到，不是生产事故）
- 触发条件：WP2 的 `test_multi_step_plan_is_frozen_then_fails_closed_before_any_step` 仍断言 `MULTI_AGENT_EXECUTION_NOT_READY` 且 `agent_calls == []`。
- 故障表现：WP3 让 specialist/synthesis 真实执行后该用例失败。
- 根因分析：测试固化了“多 Step 不得执行”的阶段性假设，而该假设本身就是要被 WP3 删除的 gate。
- 修复方案：原地更新为 WP3 行为断言（三个 Agent 各调用一次、3 个 STEP_STARTED/STEP_COMPLETED、无 `OUTPUT_DELTA`、`FINAL_OUTPUT_PIPELINE_NOT_READY`、Store 最终 CLEARED）；未删除原测试。
- 回归测试：dynamic lifecycle 17 passed；全量 1265 passed。
- 对应知识点：测试即文档、阶段性 gate 的退役流程、删除能力要同步退役断言。
- 面试表达：删除 gate 的同时更新断言，而不是删测试掩盖失败。
- 当前状态：已更新并通过。

### Bad Case 5：Driver 直接写 Store 会破坏所有权

- 类型：假设构造（风险推演，不是实际事故）
- 触发条件：为省一次跳转，让 `MultiAgentDriver` 在返回前调用 `write_prepared`。
- 故障表现：Store 的 once-write、ACL 和容量校验全部可以被绕开；Driver 变成第二写入者，提交顺序与事件顺序失去唯一 owner。
- 根因分析：把“写结果”和“调用 Agent”耦合在同一个组件，所有权边界消失。
- 修复方案：Driver 只返回 typed `StepResult`；写入只由 `StepResultCommitter` 执行；测试断言 Driver 无 `write_prepared/mark_readable/seal/clear/output_gate` 方法。
- 回归测试：Driver 无写权测试、提交顺序测试、Store once-write 测试。
- 对应知识点：单一所有权、写入路径审计、能力最小化。
- 面试表达：谁写 Store 不是实现细节，而是安全合同；我把它固化成唯一提交 owner 和负向 API 断言。
- 当前状态：防线上已实现（假设构造，不声称曾发生泄漏）。

### Bad Case 6：迟到结果在 Run 终结后提交

- 类型：假设构造（风险推演，不是实际事故）
- 触发条件：detached worker 在 Run 已终态后返回结果，或重复 completion 回调再次触发 commit。
- 故障表现：可能把过期结果写入已终结 Run 的 Store，或对同一 Step 二次提交。
- 根因分析：缺少“Run 终结即拒绝写入”的守卫。
- 修复方案：Run 终结先 seal Store 再清理；completion guard 对同一 Step 只放行一次；迟到/重复提交映射为 `STEP_RESULT_LATE_COMMIT` / `STEP_RESULT_DUPLICATE_COMMIT`。
- 回归测试：committer 的 duplicate/late 用例；Store sealed 后拒绝读写用例。
- 对应知识点：生命周期守卫、幂等、迟到结果处理。
- 面试表达：清理不是成功后的附加动作，而是终态共享的不变量。
- 当前状态：防线上已实现并测试。

### Bad Case 7：Synthesis 读取全量 Store 或未依赖结果

- 类型：假设构造（风险推演，不是实际事故）
- 触发条件：为方便实现，给 Synthesis 暴露 `get_all()` 或按 Agent 查询接口。
- 故障表现：synthesis 可能读取未执行 Agent 的旧结果或无关 Step 内容，破坏“只基于显式依赖回答”的合同并放大幻觉风险。
- 根因分析：读取权限按“方便”而非“依赖关系”设计。
- 修复方案：Store 只提供 `dependency_view_for(consumer_claim, agent_state)`，按 compiled `depends_on` 顺序返回只读视图；无 `get_all()`；synthesis adapter 无 Store 引用。
- 回归测试：Store ACL 用例（consumer 未 claim、非依赖不暴露、PREPARED 不可读、producer 未 SUCCEEDED、sealed 拒绝）；synthesis 只读视图用例。
- 对应知识点：最小权限、依赖即读取边界、ACL 设计。
- 面试表达：Synthesis 的“输入白名单”由 Store ACL 强制，而不是靠 prompt 自觉。
- 当前状态：防线上已实现并测试。

### Bad Case 8：保留 WP2 gate 或提前实现 WP4

- 类型：假设构造（授权与能力边界推演，不是实际事故）
- 触发条件：WP3 先补能力但保留 gate，或顺手实现 OutputGate/DeliveryStatus。
- 故障表现：前者形成双轨（Plan 合法却永不执行），后者越过 WP3 授权并引入尚未定稿的 delivery 语义（unknown/partial publication）。
- 根因分析：阶段边界没有落到代码：gate 的退役和 final-output 保护的生效必须是同一变更。
- 修复方案：同一变更内原子删除 gate 并加入 `FINAL_OUTPUT_PIPELINE_NOT_READY` 临时保护（带 WP4 removal marker）；不实现 OutputGate、DeliveryStatus、partial publication 修复、最终回答写 Memory。
- 回归测试：不再出现 `MULTI_AGENT_EXECUTION_NOT_READY`；specialist/synthesis 真实执行；无 `OUTPUT_DELTA`；最终以 `FINAL_OUTPUT_PIPELINE_NOT_READY` 失败。
- 对应知识点：阶段边界、能力声明真实性、增量交付纪律。
- 面试表达：我用“失败于用户可见交付之前”的临时保护代替“假装已经完成”，并留下明确的 WP4 删除点。
- 当前状态：WP3 边界已按此实现。

### Bad Case 9：用字符串/对象类型隐式决定输出

- 类型：假设构造（风险推演，不是实际事故）
- 触发条件：以“Driver 返回字符串=final、返回对象=internal”作为输出判据。
- 故障表现：Adapter 或 Driver 返回类型变化会静默改变输出语义；多 Step 与单 Step 共用同一路径时无法分辨。
- 根因分析：用运行时类型推断代替显式模式。
- 修复方案：ParallelExecutor 显式 typed mode（注入 completion owner 即 typed）；INTERNAL 与 synthesis 均不产生 `OUTPUT_DELTA`；单 Step legacy mode 不被意外改造成 typed mode。
- 回归测试：Shape 2/3 无 `OUTPUT_DELTA` 断言；单 Step E2E 旧字符串输出断言。
- 对应知识点：显式优于隐式、模式切换、输出语义合同。
- 面试表达：输出策略应由执行模式决定，而不是由返回值类型碰运气。
- 当前状态：已按显式模式实现。

### Bad Case 10：raw StepResult 泄漏到可观测链

- 类型：假设构造（安全审计场景，不是实际泄漏事故）
- 触发条件：把 `StepResult.content` 放进 Batch report、Journal payload、span attribute 或异常字符串。
- 故障表现：专内结果（可能含文件路径、查询和业务数据）进入持久化或观测通道。
- 根因分析：报告字段直接使用业务正文，而不是安全 metadata 投影。
- 修复方案：报告只记录 `step_id / producer_agent_id / content_type / char_count / complete / commit status`；raw-bearing 对象一律安全 repr、非 dataclass、不可 pickle；安全测试用敏感标记扫描 Journal/Snapshot/Trace/log/report/exception。
- 回归测试：`test_step_result_security.py` 三个用例 + Store/committer repr 用例。
- 对应知识点：数据最小化、观测通道 allowlist、敏感数据生命周期。
- 面试表达：我不仅限制模型能决定什么，也限制模型和用户原文能被记录到哪里。
- 当前状态：防线上已实现并测试（假设构造，不声称曾发生泄漏）。

## 12. 实施中的回归策略

WP3 同时改动执行链与测试，采用分层回归：

| 层级 | 目的 | 最终结果 |
|---|---|---|
| WP3 专项 | StepResult/Store/Factory/Driver/Synthesis/Committer/Shape2/Shape3/失败路径/安全 | 98 passed |
| WP1+WP2 关键回归 | Registry/Bindings/Compiler/Planning/Scheduler/Parallel/Coordinator/fingerprint/snapshot/recovery/event/metrics | 204 passed, 13 subtests passed |
| 全仓 | 检查旧 static 假设与跨模块兼容 | 1265 passed, 42 subtests passed |
| 静态检查 | 编译与 diff whitespace | PASS |

执行命令：

```text
uv run pytest -q <WP3 专项测试>
uv run pytest -q <WP1+WP2 关键回归>
uv run pytest -q
uv run python -m compileall -q core tests server.py main.py
git diff --check
```

全仓首轮即通过（1265 passed）；此前多轮失败均来自 WP3 专项迭代（见 Bad Case 1-4），修复后全部转绿，未通过删测试或放宽断言获得。

## 13. 兼容性策略

- 单 Step：Core direct、explicit entry、单 delegated knowledge direct 继续走 `ResolvedSingleStepDriver` 与旧字符串输出；不迁移到尚不完整的多 Agent 输出链。
- Static Coordinated：公开兼容构造未变；static 无 Planning 事件路径通过。
- Legacy：显式 LEGACY selector 不创建 Coordinated scope，原链路不变。
- Streaming/Event：多 Step 无 `OUTPUT_DELTA`；单 Step 事件顺序未变。
- Snapshot/Recovery：Plan snapshot 合同未变；Store/Bindings 不持久化、不恢复。
- API：外部 `/api/chat` 请求与响应主要形态不变。

兼容的核心不是“所有旧行为一字不变”，而是把旧行为分类：仍然合法的显式 Legacy/static 保留；会绕过新安全合同的隐式路径必须删除或显式 fail closed。

## 14. 当前能力边界

WP3 已完成：

- 多 Agent specialist 真实并行执行；
- specialist 结果在当前 Run 内 once-write 传递；
- Synthesis 只读显式依赖并恰好生成一次 final candidate；
- INTERNAL 结果不泄漏；multi-step final 在 WP4 前不进入用户输出通道；
- WP2 gate 原子删除；final-output 临时保护生效；
- Store/Bindings 全终态清理；全仓回归通过。

WP3 尚未完成：

- 用户可见多 Agent final（WP4 OutputGate/DeliveryStatus）；
- partial publication 序列修复与最终回答写 Memory；
- Store/Bindings 持久化或 dynamic resume；
- 完整“specialist 不读取 Memory”边界（WP5 范围；当前沿用 `complete_single_agent` 按 Agent scope 读取历史的既有行为）。

P2 已知容量风险：Planning 与 specialist 执行共享同一 bounded executor（4 workers / 8 pending），`PLANNING_MODEL` 无独立保底容量，阻塞的 specialist 任务可能让 Planning 排队；已有 `runtime_blocking_executor_pending` gauge 与 `runtime_blocking_executor_wait_seconds` histogram 可观测，Planner timeout 已包含排队时间；按 WP3 边界未新建第二线程池。

因此不能宣称“Stage 2.5 多 Agent 闭环完成”，只能宣称“多 Agent 内部执行与结果数据流已真实可用，用户可见交付待 WP4”。

## 15. 面试高频追问

### 15.1 为什么 Driver 不能写 Store？

Store 的 once-write、ACL、容量和生命周期只有在单一写入者下才可证明。Driver 只负责“按 claim 调用 Agent 并返回 typed result”；写入由 `StepResultCommitter` 完成，否则任何一个新 Driver 都可能绕过校验。

### 15.2 为什么 synthesis 只能读依赖视图？

“只能根据提供的专家结果回答”不能靠 prompt 自觉保证，必须由 Store ACL 强制：consumer 必须先有 claim、producer 必须在 compiled `depends_on` 中、条目必须 READABLE、producer Step 必须 SUCCEEDED、Store 必须未 seal。

### 15.3 为什么多 Step Run 仍然失败？

所有 Step 都成功了，final StepResult 也 READABLE 了，但把 final candidate 交付给用户的 OutputGate 属于 WP4。WP3 用 `FINAL_OUTPUT_PIPELINE_NOT_READY` 在“执行成功”和“用户可见交付”之间显式失败，避免用旧字符串输出逻辑提前交付。

### 15.4 怎么证明 specialist 真的并行？

不用总耗时。两方 `threading.Barrier(2)` 要求两个 specialist 同时进入执行入口；共享 active 计数达到 2；事件顺序断言两个 enter 都在任一 exit 之前、synthesis enter 晚于全部 specialist exit。

### 15.5 为什么 adapter 是同步 execute？

现有统一入口 `complete_single_agent` 是同步合同，且在 bounded blocking executor 线程中执行以复用 Budget/Circuit/Retry/Model 事件。让 Protocol 同步是为了与真实执行模型一致，而不是为了迁就实现。

### 15.6 为什么删除 WP2 gate 而不是继续保留？

gate 是“能力尚未就绪”的临时声明；保留它会让合法 Plan 永不执行，形成双轨。WP3 在 MultiAgentDriver、Factory、Store、commit、无输出、dependency view、required fail-closed、final-output 保护全部就绪的同一变更中原子删除。

### 15.7 如何保证 synthesis 恰好一次？

三层保证：Scheduler 只 claim 一次；committer 的 completion guard 对同一 Step 只放行一次提交；Store 对每 logical Step once-write。重复回调映射为 `STEP_RESULT_DUPLICATE_COMMIT` fail closed。

### 15.8 数据安全怎么落地？

raw-bearing 对象全部安全 repr、非 dataclass、不可 pickle；报告只投影安全 metadata；specialist/synthesis 一律 `persist=False`；安全测试用敏感标记扫描所有持久化与观测通道。

## 16. 面试表达版本

### 16.1 30 秒版本

我在 LocalAgent 的 WP3 里把“能规划但不能执行”的多 Step Plan 变成真实执行链：Scheduler 并行 claim 两个 specialist，`MultiAgentDriver` 经 `AgentAdapterFactory` 调用真实 Agent，结果以 typed `StepResult` once-write 进 `StepResultStore`，synthesis 只读显式依赖并恰好生成一次 final candidate。用户可见的多 Agent 输出是 WP4 的事，所以现在所有 multi-step Run 在 final READABLE 后以 `FINAL_OUTPUT_PIPELINE_NOT_READY` 显式失败，INTERNAL 结果零泄漏。全仓 1265 个测试通过。

### 16.2 2 分钟版本

这个问题来自一个真实缺口：WP2 能生成合法的多 Step Plan，但任何 Step 都不会执行，系统只能说“规划已接入”，不能说“专业能力已执行”。WP3 的关键不是多写一个路由，而是建立一整套运行期结果数据流合同。

第一是适配层：`AgentAdapterFactory` 按 Registry 的 `execution_adapter_id` 解析 adapter，Driver 不按 agent 名写分支；specialist 调用一律 `persist=False`。第二是结果存储：`StepResultStore` 只有最小提交骨架能写，条目 PREPARED -> READABLE，容量和条目数硬上限，读取只有 dependency-scoped ACL，没有 `get_all()`。第三是提交与输出边界：committer 负责校验、写 Store、提交 Step 状态、发布 `STEP_COMPLETED` 并返回安全 report；Executor 显式 typed mode，INTERNAL 和 synthesis 都不产生 `OUTPUT_DELTA`；所有 Step 成功后 Run 以 `FINAL_OUTPUT_PIPELINE_NOT_READY` fail closed。

并行证据用 barrier 和事件顺序而不是耗时；安全测试用敏感标记扫描 Journal/Snapshot/Trace/日志/报告/异常均无泄漏。实现中还修复了“默认共享 resource key 把 specialist 串行化”和“gate 测试固化了旧错误码”两个真实问题。最终 WP3 专项 98 passed，WP1+WP2 关键回归 204 passed，全仓 1265 passed。

### 16.3 深入追问主线

1. 用事件序列对比 WP2 gate 与 WP3 真实执行，说明“执行成功”与“用户可见交付”是两个维度。
2. 画出 Store 状态机（PREPARED/READABLE/SEALED/CLEARED）与提交顺序。
3. 解释为什么只有 committer 能写 Store、为什么 synthesis 只能读依赖视图。
4. 说明 required fail-closed 如何让 synthesis BLOCKED 并以 `REQUIRED_DEPENDENCY_FAILED` 终结。
5. 展示并行证据（barrier + active 计数 + 事件顺序）。
6. 说明 `FINAL_OUTPUT_PIPELINE_NOT_READY` 是 WP3/WP4 施工边界，带 WP4 removal marker。
7. 解释数据安全边界与 `persist=False` 的落地方式。
8. 说明 P2（Planning 饥饿容量风险）与既有可观测指标。

## 17. 最终验收结论

```text
WP3 status: PASS
Architecture deviations: 0
P0 findings: 0
P1 findings: 0
P2 findings: 1
Real multi-agent specialist execution enabled: YES
Specialists execute in parallel: YES
StepResultStore enabled: YES
Synthesis execution enabled: YES
Internal specialist results hidden from user output: YES
User-visible multi-agent final output enabled: NO
WP2 multi-step admission gate removed: YES
Multi-step final fails closed before WP4 delivery: YES
Ready for GPT review: YES
Ready to start WP4: YES
```
