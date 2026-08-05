# LocalAgent Stage 2.5 Multi-Agent WP2 面试材料

> 适用范围：Stage 2.5 Multi-Agent WP2——把 WP1 的规划合同接入默认 Coordinated Runtime，并完成动态 Plan 生命周期、单 Step 执行兼容及多 Step fail-closed。
>
> 真实性声明：本文中的“真实发现”仅指本地项目源码审查、实施或测试中实际观察到的问题，不等同于线上生产事故；“假设构造”只用于风险推演，不描述成真实事故。

## 1. 推荐的面试材料模板

一份可信、便于追问的工程面试材料应包含：

1. 一句话项目定义：先说明解决的用户问题，再介绍技术方案。
2. 真实性与边界：区分用户真实复现、源码审查发现、实施测试发现和假设构造。
3. 原始用户场景：给出真实输入、预期链路、实际链路和风险。
4. 架构演进：说明旧架构为什么无法满足要求，以及 WP1、WP2 分别解决什么。
5. 方案讨论：展示候选方案、取舍标准和被拒绝方案。
6. 核心状态机与时序：说明成功、失败、取消、超时的确定性行为。
7. 数据与权限边界：谁有权决定 Agent、instruction、Plan 和最终输出。
8. 兼容策略：旧入口、静态 Plan、Snapshot 和 Streaming 如何迁移。
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

WP2 的目标是把 WP1 已经冻结的 `AgentRegistry + PlanResolver + PlanCompiler + StepInvocationBindings` 从“可独立调用的规划组件”升级为默认 Coordinated Runtime 的真实入口：每个请求必须先规划、校验并冻结 Plan，才能进入执行；任何规划失败都显式终止，主 Agent 不得静默回退并自行补答。

WP2 并没有实现完整多 Agent 执行。它只允许动态的单 Agent、单 Step Plan 执行；合法的多 Step Plan 会在执行前 fail-closed，等待 WP3 的 `MultiAgentDriver` 和 Adapter Factory。

## 3. 真实用户场景与问题价值

用户在主 Agent 输入：

```text
调用知识专家，总结 cdt_field_mapping.md
```

用户期望：

```text
主入口
-> 识别知识任务
-> 选择 knowledge_expert
-> 执行 retrieval / document load / context build
-> 基于真实文档回答
```

原始观察中，直接使用知识专家会出现完整 retrieval 链路；从主 Agent 请求委派时却只有模型调用和直接输出，没有 retrieval 事件。问题的高价值不在于“路由关键词少了一条”，而在于系统曾把“模型输出成功”误当成“正确专业能力已执行”。对于知识库、代码分析、数据查询等需要事实来源的任务，这会放大幻觉风险。

WP1 解决的是规划和权限合同；WP2 解决的是默认入口是否真的强制经过该合同。只有两者同时成立，才能证明主 Agent 不会绕过专业能力自行作答。

## 4. 架构演进

### 4.1 WP1 已提供的能力

- `AgentRegistry`：Agent 身份、entry/delegated 权限和执行适配标识的事实源。
- `PlanResolver`：处理显式 Agent、确定性规则和模型规划。
- `StrictPlanningDecisionParser`：严格解析 Planner 的 typed decision。
- `PlanCompiler`：根据 Registry 和固定图规则生成安全 Plan。
- `StepInvocationBindings`：在内存中保存真实 instruction，不写入 Plan 或 Snapshot。

### 4.2 WP2 补齐的运行时断点

旧 Runtime 在 Factory 构造阶段就持有固定 Plan、Scheduler、Executor 和 Driver；Driver 也固化 `step_id=answer`、selected agent 和原始 query。这与“Plan 必须在 Run 内动态解析”冲突。

WP2 将构造期和规划后的对象分开：

```text
构造期：RunContext + AgentState + Budget + EventChannel + Resolver
                       |
                       v
执行期：PLANNING_STARTED -> resolve -> freeze Plan/Bindings
                       |
                       v
       register Steps -> Scheduler/Executor/Checkpoint -> execute
```

动态构造时 `plan/scheduler/executor/checkpoint_coordinator` 尚不存在；只有成功得到并冻结 `ResolvedPlan` 后才能创建。Plan 与 Bindings 对外只读，且只允许冻结一次。

## 5. 方案讨论与取舍

| 方案 | 优点 | 风险或不足 | 结论 |
|---|---|---|---|
| ChatService 中先调用 Resolver，再创建旧 Runtime | 改动表面较小 | Planning 不在 Run 生命周期内，难以统一取消、预算、事件、deadline 和 checkpoint | 拒绝 |
| Factory 构造时同步解析 Plan | 可以继续复用固定构造器 | 构造阶段无法自然消费异步模型规划；RunHandle 也无法覆盖 Planning 阶段 | 拒绝 |
| 规划失败时回退旧 static/Core 路径 | 表面可用性高 | 重现主 Agent 静默补答，破坏用户“严禁编造”的要求 | 拒绝 |
| 直接在 WP2 实现多 Agent Driver | 一次性可执行多 Step | 越过 WP2 授权，混入 Adapter、Store、Synthesis 和 OutputGate 生命周期 | 拒绝 |
| Runtime 内两阶段动态装配 | Planning 与执行共享 RunContext、预算、取消、事件和 deadline；边界清晰 | 需要迁移大量 static 测试假设 | 采用 |

关键选择是：Planning 不是 API 前置辅助函数，而是一次 Run 的正式阶段。这样请求从一开始就受同一 `RunHandle`、BudgetLedger、deadline 和终态发布规则约束。

## 6. 默认动态生命周期

完整调用链：

```text
/api/chat
-> ChatService.stream_coordinated_agent_events
-> CoordinatedRuntimeFactory.create_run_scope
-> RunCoordinator.for_dynamic_resolver
-> RUN_STARTED
-> PLANNING_STARTED
-> PlanResolver.resolve(PlanningRequest, same RunContext)
-> ResolvedPlan
-> freeze Plan + StepInvocationBindings exactly once
-> register AgentState Steps
-> create Scheduler + ParallelExecutor
-> create Plan-bound CheckpointCoordinator
-> PLAN_CREATED
-> POST_PLAN_PRE_EXECUTION checkpoint
-> execution admission gate
-> allowed single-Step execution
```

对应状态机：

```text
UNRESOLVED -> RESOLVING -> FROZEN
                    \
                     -> FAILED
```

任何 freeze 前的 schema、Registry、Compiler、预算、deadline、取消或模型失败都会进入失败终态，不产生 fallback Plan。第二次 prepare/freeze 也会失败，防止运行中替换 Plan。

## 7. Planning Model 的统一治理

WP2 新增 `UnifiedPlanningModelAdapter`，但没有再造模型调用栈。它通过现有 bounded executor 和 `AgentRouter.complete_planning_decision` 进入统一的 ModelInvocation 合同，因此复用：

- 总预算与 Planning 独立 cap；
- 候选模型路由、重试、熔断和 provider fallback；
- Trace 和 `MODEL_STARTED/MODEL_COMPLETED`；
- cancel-or-detach 与 application shutdown ownership。

规划超时取：

```text
min(run_context.remaining_seconds(), configured_planning_timeout)
```

如果限制来源是总 deadline，则映射 `DEADLINE_EXCEEDED`；如果是独立 Planning cap，则映射 `PLANNING_FAILED / PLANNER_TIMEOUT`。分类依据是“哪个上限限制了 effective timeout”，而不是超时发生后再比较两个接近零的时间值。

## 8. 单 Step 执行与多 Step 防线

### 8.1 单 Step 执行

`ResolvedSingleStepDriver` 只做四件事：

1. 验证 frozen Plan 恰好一个 Step。
2. 使用 claim 的 `step_id + preferred_agent` 解析 Binding。
3. 将 Binding instruction 交给现有单 Agent 执行链。
4. 保留原有 `OUTPUT_DELTA` 行为。

它不按 Agent ID 编写散落的 `if/elif`，也不创建 WP3 的 Adapter Factory。

### 8.2 多 Step 临时 admission gate

以下任一条件命中即拒绝执行：

```text
step_count > 1
OR execution_kind == SYNTHESIS
OR output_policy == INTERNAL
```

此时 Plan 已合法解析、冻结并发布 `PLAN_CREATED`，也完成 post-plan checkpoint；随后以 `MULTI_AGENT_EXECUTION_NOT_READY` 失败。系统不会产生 `STEP_STARTED`、不会调用 specialist/Core，也不会产生 `OUTPUT_DELTA`。这比“先执行一部分再发现能力不足”更安全。

该 gate 是 WP3 前的阶段性保护，WP3 接入真实 MultiAgentDriver 后必须移除，不能把它误当成长久架构。

## 9. 事件、错误和安全合同

### 9.1 事件序列

```text
成功：
RUN_STARTED -> PLANNING_STARTED -> [MODEL_*] -> PLAN_CREATED
-> STEP_STARTED -> ... -> RUN_COMPLETED

规划失败：
RUN_STARTED -> PLANNING_STARTED -> [MODEL_*] -> ERROR
-> RUN_COMPLETED(FAILED)

多 Step 未就绪：
RUN_STARTED -> PLANNING_STARTED -> [MODEL_*] -> PLAN_CREATED
-> ERROR(MULTI_AGENT_EXECUTION_NOT_READY) -> RUN_COMPLETED(FAILED)
```

Planning event 在 Stream 中属于 control JSON，不会混入用户正文。

### 9.2 错误分类

| 场景 | StopReason | 安全错误码示例 |
|---|---|---|
| Planner schema 非法 | `PLANNING_FAILED` | `PLANNER_SCHEMA_INVALID` |
| Registry/权限拒绝 | `PLANNING_FAILED` | Registry safe code |
| Compiler 拒绝 | `PLANNING_FAILED` | PlanCompileErrorCode |
| Planning model 普通失败 | `PLANNING_FAILED` | `PLANNING_MODEL_FAILED` |
| Planning 独立 cap | `PLANNING_FAILED` | `PLANNER_TIMEOUT` |
| Run 总 deadline | `DEADLINE_EXCEEDED` | `DEADLINE_EXCEEDED` |
| 预算耗尽 | `BUDGET_EXHAUSTED` | `BUDGET_EXHAUSTED` |

取消、总 deadline 和预算异常优先于普通 PlanningError 捕获，不能被错误归并成模型规划失败。

### 9.3 敏感数据边界

- `PLANNING_STARTED` 不记录 user request 或未经 Registry 校验的 selected agent 原文。
- `PLAN_CREATED` 只记录安全结构信息，不记录 instruction、Binding、query 或 path。
- Planner raw response 不进入异常、Journal、Trace、Snapshot 或日志。
- Plan/Snapshot/fingerprint 不保存 Binding 或 instruction digest。
- Dynamic Bindings 在成功、失败、取消、deadline、budget、multi-step gate 和 terminal publication 异常路径统一 `close_and_clear()`。

## 10. Snapshot、Checkpoint 与恢复兼容

WP2 将 Plan Snapshot schema 升级到 v2，使其覆盖真实的 `execution_kind` 和 `output_policy`；fingerprint v2 同时绑定 Agent、执行类型、输出策略、依赖、能力和 schema。

兼容策略不是把 v1 假装成 v2：

- v1 Snapshot 仍可读取，并按 legacy schema 重建 fingerprint。
- 未知 schema 和非法枚举 fail-closed。
- dynamic Plan 在 `PLAN_CREATED` 后、Step 前捕获 `POST_PLAN_PRE_EXECUTION` checkpoint。
- 因 Bindings 故意不持久化，当前恢复只能验证该 checkpoint，不能 resume，明确返回 `UNSUPPORTED`。

这体现了一个重要原则：持久化结构安全事实，不持久化包含用户原文的执行 Binding；不能恢复时显式拒绝，不能伪造完整恢复能力。

## 11. 高价值 Bad Cases

### Bad Case 1：默认 Runtime 仍在构造期固化 static Plan

- 类型：真实发现（WP2 实施前源码审查，不是生产事故）
- 触发条件：默认 Factory 创建 Coordinated scope 时，调用者尚未经过动态 `PlanResolver`。
- 故障表现：Plan、Scheduler、Executor 和旧 Driver 在 Run 开始前已经固定；Driver 固化 `answer` Step、selected agent 和原 query，无法消费 WP1 的动态 Binding。
- 根因分析：旧架构假设“一次请求永远只有一个预先知道的 Step”，动态规划却要求 Plan 在 Run 内解析后才能创建执行组件。
- 修复方案：增加 dynamic/static 互斥构造入口；默认 `create_run_scope` 使用 Resolver，Plan 冻结后再延迟创建 Scheduler、Executor 和 CheckpointCoordinator。
- 回归测试：动态构造期对象为空、freeze 后初始化、第二次 freeze 拒绝、static path 不发布 Planning event。
- 对应知识点：两阶段初始化、状态机、不变量、依赖延迟绑定。
- 面试表达：我没有在旧 Driver 前临时插一个路由函数，而是把 Runtime 从“固定计划构造”改成“Run 内规划后装配”，让 Planning 正式进入生命周期。
- 当前状态：已修复；默认 Coordinated API 使用动态 Resolver，显式 static 兼容入口保留。

### Bad Case 2：规划失败后回退 Core 会重新引入编造回答

- 类型：假设构造（针对原始用户风险的设计验证，不是实际生产事故）
- 触发条件：Planner 返回非法 JSON、unknown Agent、越权 Agent 或模型异常。
- 故障表现：如果系统把失败折叠成“无委派任务”，主 Agent 会继续直接回答，用户无法区分真实知识检索和模型补写。
- 根因分析：把“合法 DIRECT_ANSWER”和“规划失败”编码成同一空结果，错误状态发生折叠。
- 修复方案：所有 Planning 失败使用 typed error 和 `PLANNING_FAILED` 终止；禁止 static/Core fallback。
- 回归测试：schema、Registry、Compiler、model failure 均断言无 Step、无输出、无 fallback Plan，且 raw model output 不泄漏。
- 对应知识点：fail-closed、sum type、错误状态不可折叠、RAG grounding。
- 面试表达：高风险任务里，可用性不能靠静默降级换取；如果无法证明专业能力被调用，系统应该明确失败。
- 当前状态：防线已实现并通过测试；该 Bad Case 是风险推演，不声称曾发生线上事故。

### Bad Case 3：总 deadline 与 Planning cap 的超时分类发生竞态

- 类型：真实发现（WP2 实施测试中发现，不是生产事故）
- 触发条件：Run 剩余 deadline 与独立 Planning timeout 很接近，`asyncio.wait_for` 略早于 deadline watcher 返回。
- 故障表现：同一种总 deadline 耗尽可能偶发映射成 `PLANNER_TIMEOUT`，导致错误指标和客户端语义不稳定。
- 根因分析：首版在异常发生后比较已经抖动到接近零的剩余时间，而没有保存 effective timeout 的限制来源。
- 修复方案：在等待前记录是总 deadline 还是独立 cap 限制了最小超时，并按来源稳定映射。
- 回归测试：分别覆盖独立 cap 和总 deadline，断言 `StopReason`、error code、terminal 数量和无执行行为。
- 对应知识点：异步竞态、deadline propagation、稳定错误语义、time-of-check/time-of-use。
- 面试表达：超时不只是抛一个 TimeoutError；我把“谁拥有超时”作为显式状态保存，避免根据异常后的时钟抖动猜原因。
- 当前状态：已修复并通过回归。

### Bad Case 4：Snapshot v1 兼容投影破坏 typed 对象合同

- 类型：真实发现（WP2 首版实现测试中发现，不是生产事故）
- 触发条件：为 v1 Snapshot 重建 legacy digest 时，首版实现将 `PlanSnapshot` 本体替换成 dict。
- 故障表现：定向测试出现 22 个失败；后续代码期待 typed `PlanSnapshot`，实际却收到 dict。
- 根因分析：把“序列化兼容视图”和“领域对象本体”混成一个变量，兼容处理越过了序列化边界。
- 修复方案：保持 typed 对象不变，只在 canonical digest/serialization view 层做 v1/v2 contract 投影。
- 回归测试：v1 可读、legacy fingerprint 可重建、v2 execution/output 字段完整、未知 schema fail-closed。
- 对应知识点：Anti-Corruption Layer、领域对象与 DTO 分离、向后兼容、canonical serialization。
- 面试表达：兼容旧格式应该发生在边界投影层，不能让 legacy dict 污染内部 typed contract。
- 当前状态：已修复；最终全量测试通过。

### Bad Case 5：测试 Fake 把执行回答返回给 strict Planner

- 类型：真实发现（组合回归测试中发现，不是产品或生产事故）
- 触发条件：默认 Factory 改为 dynamic 后，同一个 Fake AgentRouter 首次模型调用进入 Planner，但 Fake 仍直接返回最终自然语言答案。
- 故障表现：strict parser 正确拒绝非 JSON Planner 输出，旧组合测试失败。
- 根因分析：旧测试隐含“第一次模型调用就是答案”的 static 假设；Runtime 生命周期改变后，第一次调用可能是 Planning。
- 修复方案：Fake 根据 Planner system prompt 返回 strict typed JSON，第二次执行调用才返回答案；同步更新事件顺序和调用次数断言。
- 回归测试：组合回归、ModelInvocation、事件集成和完整调用序列均通过；没有让生产 Runtime 绕过 Resolver。
- 对应知识点：测试替身契约、交互式 mock 脆弱性、架构迁移中的测试真实性。
- 面试表达：测试失败不一定说明生产逻辑错，也可能暴露 Fake 已经固化旧架构；我修的是替身合同，而不是降低 strict parser 标准。
- 当前状态：已修复并纳入全量回归。

### Bad Case 6：合法多 Step Plan 被部分执行

- 类型：假设构造（WP3 前的风险推演，不是实际事故）
- 触发条件：Planner/Compiler 生成 `specialist INTERNAL -> synthesis FINAL_SYNTHESIS`，但 WP2 只有单 Step Driver。
- 故障表现：如果直接交给现有执行器，可能先调用 specialist，再在 synthesis 阶段失败；产生副作用、半成品输出或无法一致清理的结果。
- 根因分析：Plan 合法不等于当前 Runtime 已具备执行该 Plan 的全部能力，缺少 capability admission。
- 修复方案：Plan 冻结并记录后，在任何 Step 前检查多 Step、SYNTHESIS 和 INTERNAL，命中即以 `MULTI_AGENT_EXECUTION_NOT_READY` fail-closed。
- 回归测试：断言有 `PLAN_CREATED`，但无 `STEP_STARTED`、无 Agent 调用、无 `OUTPUT_DELTA`，Bindings 已清理且只有一个 terminal。
- 对应知识点：能力协商、admission control、原子性、避免部分执行。
- 面试表达：我把“计划是否合法”和“当前版本能否执行”拆成两道门；WP2 能生成多 Agent Plan，但不能假装已经能安全执行。
- 当前状态：临时 gate 已实现；WP3 完成 MultiAgentDriver 后应删除。

### Bad Case 7：Planning raw response 或用户请求进入可观测链

- 类型：假设构造（安全审计场景，不是实际泄漏事故）
- 触发条件：Planner 返回恶意 instruction，或用户请求包含文件路径、业务数据和敏感查询；异常处理直接拼接原文。
- 故障表现：query、path、raw response 或 Binding 可能进入 ERROR、Journal、Trace、Snapshot、metrics label 或普通日志。
- 根因分析：可观测性字段缺少 allowlist，错误信息和业务正文共用字符串通道。
- 修复方案：Planning event 使用固定 safe payload；异常只暴露稳定 code/message；Snapshot/fingerprint 不持久化 Binding；metrics 仅使用固定 source/status 标签。
- 回归测试：伪造 instruction/raw response 后检查 Plan、Binding 之外的异常、事件、Snapshot 和日志均不出现原文。
- 对应知识点：数据最小化、日志注入、低基数指标、敏感数据生命周期。
- 面试表达：我不仅限制模型能决定什么，也限制模型和用户原文能被记录到哪里；安全边界覆盖失败链和可观测链。
- 当前状态：防线已实现并测试；该案例是威胁建模，不是已发生泄漏。

### Bad Case 8：Bindings 只在成功路径清理

- 类型：假设构造（基于资源生命周期的系统性检查，不是实际泄漏事故）
- 触发条件：Planning 后发生执行失败、多 Step gate、取消、deadline、budget、producer exception 或 terminal publication failure。
- 故障表现：包含用户原始 instruction 的 run-scoped Binding 仍被 Coordinator 引用，延长敏感数据生命周期。
- 根因分析：清理逻辑若只放在正常返回分支，就无法覆盖异步系统的多种终态。
- 修复方案：在 scope/Coordinator 的统一 `finally` 中执行 `close_and_clear()`，随后清空引用；static path 没有 Binding，不执行伪清理。
- 回归测试：spy 覆盖成功、失败、gate、取消、deadline、budget 和发布异常，断言恰当清理。
- 对应知识点：RAII、finally、敏感对象生命周期、异步取消安全。
- 面试表达：敏感数据清理不是成功后的附加动作，而是所有终态共享的生命周期不变量。
- 当前状态：统一清理已实现并通过测试；案例本身为风险构造。

## 12. 实施中的回归策略

WP2 修改了默认生命周期，因此没有只跑新增测试，而是分层回归：

| 层级 | 目的 | 最终结果 |
|---|---|---|
| WP1 基线 | 确认 Registry/Bindings/Compiler/Resolver 合同未退化 | 68 passed |
| WP2 专项 | 动态生命周期、Adapter、超时、取消、清理和 gate | 20 passed |
| 关键组合 | Coordinator、Factory、Streaming、Event、E2E、Snapshot、Recovery | 97 passed, 4 subtests passed |
| 全仓 | 检查旧 static 假设和跨模块兼容 | 1184 passed, 42 subtests passed |
| 静态检查 | 编译与 diff whitespace | PASS |

全仓首轮曾出现 `1172 passed, 42 subtests passed, 10 failed`。剩余失败来自未知旧测试 Agent 或执行级模型测试仍假设第一次模型调用就是答案。修复方式是让测试使用 Registry 合法 Agent，并为执行专项选择 deterministic planning；没有通过绕开 Resolver 或放宽断言获得绿灯。

执行命令：

```text
uv run pytest -q tests/test_agent_registry.py tests/test_invocation_bindings.py tests/test_plan_compiler.py tests/test_multi_agent_planning.py
uv run pytest -q tests/test_dynamic_planning_lifecycle.py tests/test_planning_model_adapter.py
uv run pytest -q
uv run python -m compileall -q core tests server.py main.py
git diff --check
```

## 13. 兼容性策略

- Legacy：显式 LEGACY selector 不创建 Coordinated scope，原路径不变。
- Static Coordinated：保留显式 `create_static_run_scope`；不是任何动态失败的 fallback。
- Streaming：Planning 是 control event，用户正文 `OUTPUT_DELTA` 语义不变。
- Snapshot：v2 新合同与 v1 读取兼容同时验证。
- Recovery：能验证 post-plan checkpoint，但因无 Bindings 明确不可 resume。
- API：外部 `/api/chat` 请求和响应主要形状不变，内部默认链路切换为 dynamic。

兼容的核心不是“所有旧行为一字不变”，而是把旧行为分类：仍然合法的显式 Legacy/static 继续保留；会绕过新安全合同的隐式 fallback 必须删除。

## 14. 当前能力边界

WP2 已完成：

- 默认 Coordinated API 强制进入 PlanResolver。
- dynamic Plan 生命周期和 freeze-once。
- 单 Agent、单 Step 动态执行。
- 统一 Planning model 治理。
- Planning events、metrics、Snapshot v2 和 post-plan checkpoint。
- 多 Step 执行前 fail-closed。

WP2 尚未完成：

- 真实 MultiAgentDriver 与 AgentAdapterFactory。
- StepResultStore、Completion Pipeline 和 Synthesis Runtime。
- OutputGate、DeliveryStatus 和 INTERNAL 结果通道。
- Bindings/StepResult 持久化或 dynamic resume。
- 同步 provider worker 的强制终止；当前沿用 bounded executor 的 cancel-or-detach 和应用级 shutdown ownership。

因此不能宣称“Stage 2.5 多 Agent已经完成”，只能宣称“规划已接入默认 Runtime，单 Step 可执行，多 Step 在执行前安全拒绝”。

## 15. 面试高频追问

### 15.1 为什么不在 ChatService 里先规划？

因为那会让 Planning 脱离 RunHandle、BudgetLedger、deadline、cancel、Journal 和 terminal 语义。把 Planning 放进 Runtime 后，用户取消和系统 shutdown 从请求开始就能覆盖整个生命周期。

### 15.2 为什么 Plan 生成后还要 admission gate？

Compiler 证明的是图和权限合法；admission gate 证明的是当前版本具备执行能力。这两个命题不同。WP2 能合法描述多 Step 图，但 WP3 之前没有可靠的 Adapter、结果存储和 synthesis 执行链，所以必须在任何副作用前拒绝。

### 15.3 为什么不持久化 Binding 来支持恢复？

Binding 包含用户原始 instruction，可能有路径、查询和业务数据。WP2 优先保证数据最小化，只持久化安全结构事实。没有安全的 Binding 恢复合同前，恢复明确返回 unsupported。

### 15.4 为什么 deterministic 路由也发布 PLANNING_STARTED？

因为它仍属于统一 Planning lifecycle，只是 model call 数为零。这样显式 specialist、规则命中和模型规划共享 freeze、事件、checkpoint、预算和失败合同，避免形成第二条隐式捷径。

### 15.5 如何证明没有再次编造回答？

不能只看最终文本，要看事件和执行证据。知识任务应解析成 knowledge Step，并由真实 preferred agent 执行；schema/Registry/Compiler/model 失败必须没有 Step、Agent 调用和输出；多 Step 未就绪必须在任何 Step 前拒绝。

## 16. 面试表达版本

### 16.1 30 秒版本

我在 LocalAgent 的 WP2 中把 WP1 的多 Agent 规划合同接入默认 Runtime。过去主 Agent 可能直接回答本应由知识专家处理的问题，系统只能证明模型调用成功，不能证明专业能力执行。我的方案把 Planning 变成 Run 的正式阶段，统一预算、取消、deadline、事件和 checkpoint；Plan 成功后只冻结一次，单 Step 可以执行，多 Step 在 WP3 前严格 fail-closed。最终全仓 1184 个测试和 42 个子测试通过，没有把规划失败降级成 Core 自答。

### 16.2 2 分钟版本

这个问题来自一个真实本地复现：用户让主 Agent 调用知识专家总结 Markdown，直接选知识专家时有 retrieval 链路，从主 Agent 委派时却只有普通模型输出。WP1 已经建立 Registry、typed decision、Compiler 和 Binding，但还没有进入默认 API。

WP2 的关键不是多写一个路由判断，而是重构运行时装配时机。旧 Factory 在构造期就固定 Plan 和 Driver，动态规划无法接入。我把它改成两阶段：Run 先发布 `PLANNING_STARTED`，Resolver 在同一个 RunContext 内解析并校验，成功后一次性冻结 Plan/Bindings，再创建 Scheduler、Executor 和 checkpoint。Planning model 复用既有模型调用治理，没有另起 provider 或线程池。

安全上我坚持 fail-closed：非法 schema、unknown Agent、权限、Compiler 或模型失败都终止，绝不回退 Core。WP2 还不能执行多 Step，所以合法多 Agent Plan 会在任何 Step 前以明确错误拒绝，避免部分执行。实施中还修复了 deadline 与 planning cap 的分类竞态，以及 Snapshot v1 兼容层污染 typed 对象的问题。最后全仓 1184 passed、42 subtests passed，P0/P1/P2 均为零。

### 16.3 深入追问主线

如果面试官继续追问，可按以下顺序展开：

1. 用事件对比证明原问题是 delegation 缺失，不是 retrieval 故障。
2. 解释为什么 Planning 必须属于 Run 生命周期。
3. 画出 `UNRESOLVED -> RESOLVING -> FROZEN/FAILED` 状态机。
4. 说明 Planner、Compiler、Registry、Binding 各自的 authority。
5. 解释 planning cap 与 total deadline 的稳定分类。
6. 说明为什么“Plan 合法”不代表“当前 Runtime 可执行”。
7. 解释 Snapshot v2、legacy v1 投影和 Binding 不持久化的取舍。
8. 用分层回归说明如何避免为适配新架构而削弱旧测试。

## 17. 最终验收结论

```text
WP2 status: PASS
Architecture deviations: 0
P0 findings: 0
P1 findings: 0
P2 findings: 0
Default Coordinated API uses PlanResolver: YES
Dynamic Plan lifecycle enabled: YES
Single-step dynamic execution enabled: YES
Multi-agent execution enabled: NO
Multi-step plans fail closed before execution: YES
Ready to start WP3: YES
```
