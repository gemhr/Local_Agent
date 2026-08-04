# Stage 2.5 Multi-Agent Architecture Review — Round 1

> 状态：架构评审稿，不是实施说明；本轮没有修改生产代码。
> 目标：把默认 Coordinated Runtime 中缺失的真实多 Agent 主链路补到一个可运行、可验证、边界明确的 MVP+，并把仍需 GPT 裁决的分歧显式保留。

## 1. Executive Summary

### 结论

建议修复，而且应作为当前 LocalAgent 进入“最小必要生产化”之前的 P0 功能缺口处理。用户提供的两组日志已经给出直接证据：通过主 Agent 请求知识专家的 Run 没有任何 `RETRIEVAL_*` 事件，只有一次回答模型调用；直接请求 `knowledge_expert` 的 Run 则完整执行了 query rewrite、embedding、retrieve、document load、context build。两个 Run 都显示 `SUCCEEDED`，所以问题不是知识检索失败，而是默认主链路从未把请求编译为知识专家 Step。主 Agent 的回答因此没有知识检索来源约束，属于“成功执行了错误的单 Agent 计划”，而不是运行时异常。

建议只做 **Stage 2.5 Multi-Agent MVP+**，不扩成完整 V1。MVP+ 支持静态注册的任意数量候选 Agent，但单次 Plan 只允许三种扁平形态、一个最终输出源、全 required 依赖；动态注册、递归委派、运行中改 Plan、可选依赖和 StepResult 恢复均延期。

推荐采用 **方案 A 的受控版本：由 `RunCoordinator` 管理正式 Run 内的延迟规划生命周期**。不是让 Coordinator 自己理解业务 Planner，而是注入一次性 `PlanProvider`/编译入口；Plan 冻结后才创建 Scheduler、Plan fingerprint、Snapshot/Checkpoint 绑定。保留现有“构造时已有 Plan”的兼容入口，避免现有单 Agent 场景退化。

不推荐纯方案 B。它表面上保留 `RunCoordinator(plan=...)` 构造期不变量，但按当前装配顺序，Bootstrap 规划发生在事件消费者、RunHandle 注册和 Coordinator 正式启动之前；规划失败只能表现为 scope creation failure，无法拥有一致的 `RUN_STARTED -> PLANNING_* -> RUN_COMPLETED`、取消、预算和流式语义。如果为方案 B 补齐这些能力，Bootstrap 实际会演变为第二个 Parent Runtime，职责和状态机会比方案 A 更复杂。

### 真实改动规模

MVP+ 预计 **15–22 人日**（熟悉代码库的 1 名工程师，含集成缓冲），约 3–4.5 周：

| 工作包 | 估算 |
|---|---:|
| 契约、事件和版本设计 | 1.5–2 人日 |
| 静态 Registry、typed Planner、PlanCompiler | 2.5–4 人日 |
| Coordinator 延迟冻结 Plan、Checkpoint/Snapshot 时机调整 | 2–3 人日 |
| MultiAgentDriver、StepResultStore、OutputGate、Synthesis | 3–4 人日 |
| Streaming、前端状态、Memory/日志安全边界 | 1.5–2.5 人日 |
| 单元、故障注入、回归、真实主链路 E2E、文档 | 3–4.5 人日 |
| 集成缓冲 | 1.5–2 人日 |

预计新增 5–8 个生产文件、修改 12–18 个生产文件；生产代码约 900–1,500 行，测试约 1,500–2,500 行。此前若按 7–10 人日估算，低估了正式 Run 的规划事件、Checkpoint 时机、输出防泄漏和默认 API E2E；若按 19–29 人日估算，在本评审删掉 optional dependency 和动态编排后偏保守，但仍可能成为外部模型/检索 mock 不稳定时的上界。

若“任意多 Agent”指运行时动态安装任意实现、递归组网、可恢复结果和多轮协商，则不是本 MVP+：在本方案之上还需约 **30–50 人日**，且需要新的安全、版本、隔离、恢复和治理设计。静态 Registry 本身不会把数量写死为 5，但必须设置每 Run 最大 Agent 数、总结果大小和并发上限。

### 最大风险

1. 把规划放在 Coordinator 外却仍宣称它属于正式 Run，会形成双生命周期和无法终结的规划失败。
2. 当前 Executor 以“结果是字符串 + driver 全局开关”发布 `OUTPUT_DELTA`；多 Agent 后会直接泄漏中间结果。
3. 当前 Coordinator 丢弃 batch report，且 Snapshot/Recovery 明确不保存业务结果；若没有独立 Run-scoped store，Synthesis 没有可靠输入。
4. 仅靠 synthesis prompt 不能保证“不新增事实”。工程上只能保证输入白名单、来源边界、缺失依赖 fail-closed 和不调用未授权数据源，不能宣称模型绝不幻觉。

## 2. Current Code Reality

### 2.1 默认 `/api/chat` 的 Coordinated 调用链

实际链路如下：

```text
server.py /api/chat
  -> ChatService.stream_coordinated_agent_text
  -> ChatService.stream_coordinated_agent_events
  -> RuntimeFactory.create_run_scope
  -> AgentRouter.build_single_agent_plan
  -> RunCoordinator(plan=single answer step)
  -> CoordinatedSingleAgentDriver
  -> AgentRouter.complete_single_agent
  -> ParallelExecutor 自动把字符串结果发布为 OUTPUT_DELTA
```

源码证据：

- `server.py:663-674` 定义 `/api/chat` 并读取一次 runtime 选择；`server.py:691-697` 是显式 Legacy 分支，`server.py:784-794` 是默认 Coordinated 文本流分支。
- `core/chat_service.py:269-298` 提供 Coordinated event surface；`core/chat_service.py:300-324` 调用 factory 建立 scope；`core/chat_service.py:336-359` 才启动 `scope.execute()` producer；`core/chat_service.py:414-445` 再把事件适配成文本流。
- `core/runtime/runtime_factory.py:254-305` 创建 RunContext、BudgetLedger、AgentState 后直接调用 `router.build_single_agent_plan`；`core/runtime/runtime_factory.py:307` 把最大并发固定为 1；`core/runtime/runtime_factory.py:343-369` 用已存在 Plan 构造 Coordinator，并固定绑定 `CoordinatedSingleAgentDriver` 和 step id `answer`。
- `core/runtime/runtime_factory.py:36-76` 的 driver 只接受 `answer` 和请求时选择的 Agent，调用 `router.complete_single_agent`，并声明 `emits_user_output=True`。
- `core/agent_router.py:970-974` 的 `build_single_agent_plan` 只生成单 Step；`core/agent_router.py:1517-1540` 的 `complete_single_agent` 只执行一次指定 Agent，不进入 Legacy orchestration。

因此，“调用知识专家”在默认 core_router 请求中不会自然变成知识专家 Step。现有 Run 的成功状态只能证明 core_router 单 Step 完成，不能证明路由正确。

### 2.2 Legacy 多 Agent 链路仍存在，但没有迁移

- `core/chat_service.py:163-218` 构造 LegacyAgentRouterDriver 和 AgentLoop。
- `core/runtime/agent_loop.py:213-224` 调用 `router.chat_stream`，同时从 final output 中剥离 `[[ORCH]]` 控制文本。
- `core/agent_router.py:1762-1782` 在 core_router 且 `_should_orchestrate` 成立时进入 `_stream_core_with_orchestration`。
- `core/agent_router.py:1547-1574` 先做知识请求的确定性路由，否则调用模型产生自由文本 Delegate 计划；`core/agent_router.py:1377-1410` 解析 `Delegate: agent_id | task` 并使用硬编码代理集合校验。
- `core/agent_router.py:1629-1760` 顺序执行 delegates；单 knowledge 结果在 `1706-1730` 原样透传，其他情况在 `1732-1760` 再调用 core_router 做 synthesis。
- `core/agent_router.py:1576-1605` 的 synthesis 事实约束是 prompt 约束，不是运行时可验证的输入权限。

现有 `agents_config`/delegate 列表（`core/agent_router.py:191-213`）可作为静态 Registry 的迁移数据源，但它不是 typed Registry：没有统一的输入/输出契约、并发能力、最终输出权限和 driver factory。

Legacy 实现还有两个不应直接复制的性质：执行是顺序的；专业结果会写入 orchestration memory scope。MVP+ 明确不持久化原始专业结果，因此只应复用底层 Agent 调用适配，不应整体搬运 Legacy orchestration。

### 2.3 Plan、Scheduler、Coordinator 的真实不变量

- `core/runtime/planning.py:48-68` 定义 frozen `PlanStep`/`Plan`，`core/runtime/planning.py:71-96` 做基础校验，`core/runtime/planning.py:99-110` 只提供确定性的单 Step 计划。
- `core/runtime/plan_graph.py:64-101` 已能校验缺失依赖、自依赖、重复依赖和环，可复用。
- `core/runtime/run_coordinator.py:140-171` 构造时必须收到 concrete Plan；启用 Snapshot 时，`core/runtime/run_coordinator.py:178-214` 会在构造期验证 Plan、注册 Step，并立刻创建绑定该 Plan 的 CheckpointCoordinator。
- `core/runtime/scheduler.py:320-334` 的 scheduler binding 包含 plan id/version 和 step 属性，运行中替换 Plan 会被识别为不一致。
- `core/runtime/scheduler.py:336-390` 只有全部依赖 `SUCCEEDED` 才 ready；任一依赖失败或取消会把下游标记为 `BLOCKED/DEPENDENCY_NOT_SUCCESSFUL`。
- `core/runtime/scheduler.py:400-425` 只有所有 Step 成功才视为完成。

这意味着现有 Scheduler 已经提供“不按 agent 名称硬编码”的 required fail-closed，**但不支持 optional dependency**。当前依赖边没有 required/optional 语义，不能把验收项 14 当作已有能力。

### 2.4 Step 结果和最终输出的真实路径

- `core/runtime/parallel_execution.py:71-91` 的 `StepExecutionOutcome.result: Any` 明确是非持久化、进程内结果。
- `core/runtime/parallel_execution.py:181-233` 执行 driver；`223-232` 在全局 driver 声明可输出且结果为字符串时自动发布 `OUTPUT_DELTA`。
- `core/runtime/parallel_execution.py:268-292` 生成有序 batch report；`core/runtime/run_coordinator.py:399-440` 等待 batch task 后在 `437` 行丢弃 report。
- `core/runtime/run_coordinator.py:119-134` 的 RunCoordinatorResult 只有安全状态和 step id，没有业务结果。
- 当前 Coordinated 链路没有把最终文本写入 AgentState.final_output；ChatService 依赖 `OUTPUT_DELTA` 重建用户输出。

所以不能仅靠扩大 Plan 获得多 Agent：中间结果既没有依赖读取通道，又会因字符串类型被误发到用户流。

### 2.5 Snapshot、Journal、Trace 与 Memory 边界

- `core/runtime/checkpoint.py:203-257` 在 checkpoint 时把 frozen Plan 转成 PlanSnapshot 并计算 fingerprint。
- `core/runtime/snapshot_contract.py:152-177` 只存文本 presence/length/digest；`core/runtime/snapshot_contract.py:343-428` 即使接收 result 也只做摘要，而实际 checkpoint 调用没有传业务结果。
- `core/runtime/recovery_validation.py:689-724` 明确记录当前没有 result body store/rehydration owner，不支持 result 恢复和 output reconstruction。
- `core/runtime/events.py:711-732` 对 `OUTPUT_DELTA` 的 journal projection 只保存长度和 SHA-256，不保存文本；`core/runtime/event_channel.py:300-355` 先 journal/observability，再进入流队列。
- `core/runtime/structured_logging.py:213-250` 使用字段白名单；`core/runtime/tracing.py:27-40` 明确拒绝 prompt、messages、user input、model/tool output、query、RAG、memory 等原文属性。
- `core/memory_manager.py:155-180` 的 `add_message` 会持久化原始 content，所以专业结果不能沿用 Legacy 的默认持久化行为。

结论：原始专业结果最合适的边界是独立 Run-scoped 内存服务；不应塞入 AgentState/StepState、Journal、Snapshot、普通日志、Trace 或 Memory。

## 3. Findings on the Three Core Problems

### 3.1 Plan 生命周期：选择受控方案 A

#### A/B 对比

| 维度 | 方案 A：Coordinator 内延迟规划 | 方案 B：外部 Bootstrap 规划 | Round 1 结论 |
|---|---|---|---|
| 构造期不变量 | Plan 在构造时未冻结；需把“未规划”限制在私有状态，并只允许一次冻结 | 完全保留现有 `RunCoordinator(plan=...)` | B 表面更强；A 需新增严格 once-only invariant |
| 状态机复杂度 | Coordinator 多 `PLANNING` 阶段，但仍只有一个 Parent Runtime | 若只做工厂则状态简单；若补齐正式 Run 语义，会形成 Bootstrap + Coordinator 两层状态机 | 完整语义下 A 更低 |
| 事件语义 | 可自然保证 `RUN_STARTED` 后规划，失败也有 terminal | 当前 consumer/handle/Coordinator 尚未启动，规划事件没有正式 owner | A |
| Snapshot | Plan 冻结前不能生成 PlanSnapshot；成功后开始 | 进入 Coordinator 时可沿用现状 | B 较小，但 A 可明确延迟生效 |
| Checkpoint | 动态规划 Run 的 PRE_RUN checkpoint 需改为 PLAN_CREATED 后的 pre-execution checkpoint | 保持现有 PRE_RUN | B 较小；A 必须版本化说明 |
| Recovery | 规划失败 Run 无可恢复 Plan；规划成功后沿用当前验证 | Coordinator 只看到成功编译的 Plan；Bootstrap 失败无法作为标准 Run 恢复 | A 的失败语义更完整；两者都不恢复 StepResult |
| Cancellation | RunHandle 注册后可取消 Planner | 当前 factory 在规划完成后才注册 handle，Bootstrap 阶段不可按 Run 取消 | A |
| Budget | Planner 与 Steps 共用一个正式 BudgetLedger | 可共用 ledger，但在 Coordinator 外，需要 Bootstrap 自己负责结算/终止 | A |
| API Streaming | producer 已启动后发布 planning events | 当前 ChatService 等 scope 建好后才启动 producer，无法实时流式规划 | A |
| 规划错误 | 正式 `PLANNING_FAILED -> ERROR -> RUN_COMPLETED` | 易退化为 `RUNTIME_SCOPE_CREATION_FAILED`，缺少正式 Run 事件 | A |
| 测试改动 | Coordinator、factory、checkpoint、event contract 都需扩展 | happy path 改动较少；完整失败/取消/流式仍需 Bootstrap 测试 | 纯 B 小，完整 B 不小 |
| 生产代码改动 | 中等，核心契约有改动 | 简化版小；完整语义版中到大并引入新 owner | A 更一致 |
| 回归风险 | 触及冻结的 Coordinator 契约；需保留 static-plan 兼容路径 | 容易保持现有 Coordinator，但风险转移到 API/生命周期裂缝 | 两者风险类型不同 |
| 后续扩展性 | 正式 Run 从 planning 到 terminal 一致，可扩展 planning timeout/cancel/metrics | Bootstrap 若继续扩展会成为事实上的第二 Parent Runtime | A |

**明确选择：A，不选 B，也不选含糊的折中。** 这里的 A 是兼容式双入口：现有静态单 Agent Plan 仍可直接传入；新增多 Agent 路径传 `PlanProvider`，两者互斥。Coordinator 内部只保存一个“尚未冻结/已冻结”状态，不把 Planner 业务逻辑塞进 Coordinator。

推荐顺序：

```text
创建 RunContext / AgentState / Budget / Channel / RunHandle
  -> 构造 Coordinator(plan XOR plan_provider)
  -> 注册并启动正式 Run
  -> RUN_STARTED
  -> PLANNING_STARTED
  -> PlanProvider -> AgentTaskSpec
  -> PlanCompiler -> immutable Plan
  -> PLAN_CREATED（仅安全元数据 + fingerprint）
  -> 注册 StepState / 初始化 Scheduler / CheckpointCoordinator
  -> pre-execution checkpoint（动态规划 Run）
  -> 执行
```

Snapshot、fingerprint、Checkpoint、Recovery validation 从 `PLAN_CREATED` 后生效。规划失败的 Run 有 Journal 和 terminal event，但没有 Plan snapshot，也不可 resume。现有 static-plan Run 保留原 PRE_RUN checkpoint 语义。

### 3.2 StepResult：必须有独立 Run-scoped Store

`StepExecutionOutcome.result` 只在一个 batch report 中存在，而且 Coordinator 当前丢弃 report。让 Coordinator 收集并跨 batch 传递结果会把业务数据、访问控制、容量和清理责任塞进控制平面；放入 AgentState/StepState 会污染状态/快照契约；放入 Journal/Snapshot 会与“不持久化原始结果”和当前 Recovery 能力冲突。

推荐 `StepResultStore`：

- 由 `CoordinatedRunScope`/run-scoped services 所有，不由 Coordinator 所有；Coordinator 只负责生命周期 seal/close 的协调。
- `step_id` 单写，线程安全；写入时校验当前 claim 的 producer，重复写入失败。
- 只保存成功且完整的 typed text result。MVP 只支持 `TEXT`/`MARKDOWN`，不接受任意 `Any` 或二进制。
- 读取必须带 consumer step id，并由 compiled dependency whitelist 授权；没有 `get_all()`。
- 每结果、每 Run 有硬限制；超限使该 Step 失败，不静默截断 required 事实。
- Run 开始终结时先 seal，拒绝新读写；没有 detached worker 后 clear。若同步底层调用无法硬杀而成为 detached worker，则必须先 seal，待 worker 终止回调后最终清理，不能一边仍可能写入一边销毁。
- 原始 content 不出现在 `repr`、异常、event、Journal、Snapshot、日志、Trace 或 Memory。事件只记录 step id、producer、安全 content type、length、digest、complete。
- crash 后不恢复；恢复验证明确报告 `STEP_RESULT_REHYDRATION_UNAVAILABLE`。任何需要 Synthesis 的已中断 Run 都不可从中间 Step 继续。

### 3.3 输出权限和事实约束：必须显式 OutputPolicy + OutputGate

同意引入：

```text
INTERNAL
FINAL_PASSTHROUGH
FINAL_SYNTHESIS
```

但策略的静态来源必须是编译后的 Plan/Step 合同，不能由 Driver 返回值类型决定，也不能信任 Planner 自报。`PlanCompiler` 根据 Registry capability 和允许的三种 Plan 形态决定/校验 policy，并保证每个 Run 恰好一个 final source。

推荐独立 `OutputGate`，由 ParallelExecutor 在 driver 返回 StepResult 后调用。Executor 负责调用机制和保持 `OUTPUT_DELTA` 在 `STEP_COMPLETED` 前的现有顺序，但不再自行根据 `isinstance(result, str)` 决策；OutputGate 才负责 policy 校验、唯一发布、大小/完整性检查和事件发布。这样比让 Coordinator 等整个 batch report 后再输出改动更小，也不会改变终端流顺序。

边界如下：

- Driver 永不直接发布用户可见 `OUTPUT_DELTA`。
- INTERNAL 专业 Step 可以发布模型、检索、工具和进度元数据事件，但不允许把 raw token/content 放入用户输出通道。
- `FINAL_PASSTHROUGH` 仅允许 Registry 明示 `allows_final_passthrough=True` 的 Agent；MVP 仅 knowledge_expert，且必须是唯一 Step、结果完整、无失败依赖、原样一次发布。
- `FINAL_SYNTHESIS` 仅 synthesis_agent 使用，读取显式依赖白名单，只发布一次完整结果。
- Synthesis 失败不允许拼接专家结果降级；Run 失败且用户流没有伪最终答案。

工程上能够保证：Synthesis 输入只来自成功的显式依赖；没有全量 Memory/Journal/未执行 Agent；required 缺失时不调用 synthesis；不会输出内部专业结果；最终输出唯一。工程上不能保证：语言模型绝不生成输入中没有的新事实。对外只能宣称“输入来源受限和失败闭合”，不能宣称“零幻觉”。

## 4. GPT Proposal Review

| GPT 建议 | Codex 结论 | 源码依据 | 修改建议 | 主要风险 |
|---|---|---|---|---|
| Coordinator 外增加 Bootstrap/Factory 完成规划 | 不同意作为最终方案 | `runtime_factory.py:300-390` 在 scope 创建完后才执行/注册；`chat_service.py:336-359` 更晚才启动 producer | 采用正式 Run 内一次性 PlanProvider；保留 static-plan 构造兼容 | 纯 B 无正式 planning stream/cancel/terminal；完整 B 形成第二 Parent Runtime |
| Planner 输出 typed `AgentTaskSpec`，再由 PlanCompiler 编译 | 同意 | 现有 `planning.py:48-96` 是 Runtime Plan 和基础校验；Legacy `agent_router.py:1377-1394` 是自由文本解析 | 中间合同与 Runtime Plan 分层；复用 PlanGraphValidator | 若把模型输出直接反序列化成 Runtime Plan，会绕过权限和唯一输出校验 |
| 静态 AgentRegistry | 同意 | `agent_router.py:191-213` 只有配置 map/list，不含完整能力合同 | 从现有配置迁移，Registry 是唯一 agent/capability/output permission 来源 | 双份配置漂移 |
| 独立 Run-scoped StepResultStore | 同意 | `parallel_execution.py:71-91` 结果非持久化；`run_coordinator.py:399-440` 丢弃 report；`recovery_validation.py:689-724` 无 rehydration owner | run scope 所有、single-write、依赖授权读取、seal/clear | 原文通过 repr/exception/event 泄漏；detached worker 清理竞态 |
| 用 `INTERNAL/FINAL_PASSTHROUGH/FINAL_SYNTHESIS` 代替字符串判断 | 同意 | `parallel_execution.py:223-232` 当前按字符串和 driver 全局开关发布 | policy 进入可 fingerprint 的编译合同；OutputGate 唯一发布 | 若 policy 只放 sidecar 且不 fingerprint，恢复/执行语义可能漂移 |
| Executor 只返回结果，Coordinator 或 OutputGate 发布 | 部分同意 | Coordinator 当前不消费 report；文本流依赖 step 完成前的 `OUTPUT_DELTA` | 选“Executor 调用注入的 OutputGate”；Executor 不拥有策略 | 让 Coordinator 输出会改变事件顺序并扩大 batch/result 改造 |
| required/optional 依赖按策略传播 | 部分同意 | `scheduler.py:336-390` 只支持所有依赖成功 | MVP+ 只允许 required；`required=False` 编译失败，optional 后续单独设计 | 现在硬塞 optional 会影响 DAG、Scheduler、Run completion、Synthesis 输入和测试矩阵 |
| Synthesis 只读显式依赖结果 | 同意 | 当前 Legacy synthesis prompt（`agent_router.py:1576-1605`）没有运行时读取白名单 | 给 synthesis dependency-scoped read view，不提供全局 store/Memory | prompt 仍可能生成新事实，不能过度承诺 |
| Synthesis 失败不拼接专业结果 | 同意 | 当前没有 Coordinated 多 Agent fallback 合同 | fail-closed：ERROR + failed terminal，无 OUTPUT_DELTA | 可用性下降，但比把未经整理的内部结果冒充最终答复更安全 |

## 5. Recommended Architecture

### 5.1 组件关系

```text
/api/chat
  -> CoordinatedRunFactory
       |- RunContext / BudgetLedger / AgentState / EventChannel / RunHandle
       |- AgentRegistry (process-static, immutable)
       |- PlanProvider (run-scoped, once)
       |- PlanCompiler
       |- StepResultStore (run-scoped)
       |- OutputGate (run-scoped)
       `- RunCoordinator(plan XOR plan_provider)
            -> Scheduler
            -> ParallelExecutor
                 -> MultiAgentDriver
                      -> AgentRegistry adapter
                      -> specialist / synthesis invocation
                      -> StepResultStore.write_once
                 -> OutputGate.publish_if_allowed
            -> CheckpointCoordinator (only after Plan frozen)
```

### 5.2 Registry 与 Agent 调用复用

Registry 初始注册 `core_router`、`knowledge_expert`、`code_expert`、`data_analyst`、`synthesis_agent`。它可以容纳更多静态 Agent，架构不按 agent 名称分支；但 MVP 的 PlanCompiler 设置每 Run 最大任务数和并发上限。

MultiAgentDriver 不复制 Agent 实现。它通过 adapter 调用现有统一模型/检索路径，并传入现有 RunContext、Budget、Timeout、Cancellation。专业调用必须 `persist=False`；只在成功终结时持久化原始用户消息和唯一 final response。`synthesis_agent` 是独立注册的逻辑角色，可以复用 core model profile，但使用专用合同/prompt，并禁止再次委派。

### 5.3 Planner 与 PlanCompiler

1. 对“调用知识专家/代码专家/数据分析专家”等显式请求先做确定性解析；这类请求不应依赖 Planner 模型猜测。
2. 其余复合请求才调用 Planner model；该调用必须使用同一 RunContext、Budget、deadline 和 cancellation。
3. Planner 只返回严格 JSON 的 `AgentTaskSpec[]`，不返回 Python/Runtime 对象。
4. PlanCompiler 从 Registry 补全/校验能力和 output permission，限制为三种固定图形，复用 PlanGraphValidator，生成 frozen Plan 和 fingerprint。
5. output policy、execution kind 和依赖语义必须进入 fingerprint 覆盖范围。最小方案是版本化扩展 PlanStep/PlanStepSnapshot；不建议用未参与 fingerprint 的可变 sidecar。

### 5.4 三种允许的 Plan

```text
A. knowledge_expert [FINAL_PASSTHROUGH]

B. specialist [INTERNAL] -> synthesis_agent [FINAL_SYNTHESIS]

C. specialist_1 [INTERNAL] --\
   specialist_2 [INTERNAL] ----> synthesis_agent [FINAL_SYNTHESIS]
   specialist_N [INTERNAL] --/
```

所有依赖在 MVP+ 中均为 required；不允许空 synthesis、synthesis 后再执行专业 Agent、多个 final step、非 synthesis 的 fan-in、递归或运行中新增 Step。

### 5.5 任意数量 Agent 的完善路径

本 MVP+ 的 Registry/Compiler/Driver 设计在“静态注册数量”上是 N 元的，不应出现 `if agent_id == knowledge_expert`。但生产运行必须有 `max_agents_per_run`、`max_parallel_agents`、`max_result_chars_per_step`、`max_total_result_chars` 和总预算，因此“任意多”表示架构不写死数量，不表示无限制执行。

进一步支持运行时动态 Agent 需要新增：注册包签名和 schema 版本、capability 权限模型、隔离执行、租户边界、健康探测、动态 adapter 生命周期、兼容矩阵、审计、回滚和配额治理，约额外 15–25 人日。若再加递归/动态 Plan、optional edge、持久化 StepResult/恢复和多轮协商，整体还需额外 30–50 人日；这已经是 Multi-Agent V1/V2，而不是 Stage 2.5。

## 6. Contract Drafts

以下仅为设计草案，不代表已实现。

```python
class OutputPolicy(str, Enum):
    INTERNAL = "INTERNAL"
    FINAL_PASSTHROUGH = "FINAL_PASSTHROUGH"
    FINAL_SYNTHESIS = "FINAL_SYNTHESIS"


class ResultContentType(str, Enum):
    TEXT = "TEXT"
    MARKDOWN = "MARKDOWN"


@dataclass(frozen=True)
class AgentRegistration:
    agent_id: str
    capabilities: frozenset[str]
    adapter_id: str                 # Registry 内解析，不允许 Planner 注入 callable
    supports_parallel: bool
    accepted_input_types: frozenset[str]
    produced_result_types: frozenset[ResultContentType]
    allows_final_passthrough: bool
    allows_synthesis: bool = False


@dataclass(frozen=True)
class AgentTaskSpec:
    task_id: str
    agent_id: str
    instruction: str
    depends_on: tuple[str, ...]
    required: bool                  # MVP+ 必须为 True；False 编译失败
    output_policy: OutputPolicy


@dataclass(frozen=True)
class StepResult:
    step_id: str
    producer_agent_id: str
    content_type: ResultContentType
    content: str
    complete: bool
    # char_count/digest 由 Store 计算，不接受调用方伪造


@dataclass(frozen=True)
class DependencyPolicy:
    required: bool = True           # MVP+ 唯一允许值


@dataclass(frozen=True)
class PlanningSuccess:
    plan: Plan
    fingerprint: str
    source: Literal["DETERMINISTIC", "MODEL"]


@dataclass(frozen=True)
class PlanningFailure:
    error_code: str                 # 安全、稳定、可枚举
    safe_detail: str | None = None  # 不含 Planner 原始输出/prompt


@dataclass(frozen=True)
class SynthesisInputItem:
    producer_agent_id: str
    step_id: str
    content_type: ResultContentType
    content: str                    # 仅在内存中传给 synthesis
    complete: bool
    succeeded: bool
    summary: str | None = None


@dataclass(frozen=True)
class SynthesisInput:
    user_request: str
    items: tuple[SynthesisInputItem, ...]  # 仅显式 depends_on，稳定排序
```

关键约束：

- Planner 的 `agent_id`、policy、depends_on 均是不可信输入；PlanCompiler 必须重新校验。
- `FINAL_PASSTHROUGH` 只能是单 Step Plan，且 Registry 明确授权。
- `FINAL_SYNTHESIS` 只能由 synthesis_agent/允许 synthesis 的 registration 使用。
- `INTERNAL` 结果写 Store，但 OutputGate 永不发布。
- PlanCompiler 必须证明 final source 恰好为 1；Store 必须证明每 step write 恰好至多为 1；OutputGate 必须证明 publish 恰好至多为 1。
- 原始 Planner 输出不进入异常、Journal 或 Trace；只记录 schema violation code 和摘要。

## 7. Event Sequence

新增 planning 事件需要更新 `core/runtime/events.py` 的 schema、`core/runtime/stream_adapter.py:48-64` 控制事件白名单，以及 `main.py:143-175,412-433` 的 `[[ORCH]]`/事件 shape 解析。事件 payload 只含安全元数据。

### 7.1 规划成功 + 单知识透传

```text
RUN_STARTED
PLANNING_STARTED
[MODEL_STARTED / MODEL_COMPLETED，仅模型规划时]
PLAN_CREATED(plan_id, version, fingerprint, step_count=1)
STEP_STARTED(knowledge)
RETRIEVAL_STARTED / RETRIEVAL_STAGE_COMPLETED / RETRIEVAL_COMPLETED
MODEL_STARTED / MODEL_COMPLETED
OUTPUT_DELTA                 # OutputGate 原样发布一次
STEP_COMPLETED(knowledge)
RUN_COMPLETED(SUCCEEDED)
```

### 7.2 规划失败

```text
RUN_STARTED
PLANNING_STARTED
PLANNING_FAILED(error_code)
ERROR(error_code=PLANNING_FAILED 或细分 code)
RUN_COMPLETED(FAILED, stop_reason=PLANNING_FAILED)
```

没有 PlanSnapshot/Checkpoint，不调用任何 Agent，不允许 core_router 自行回答。

### 7.3 多 Agent 成功

```text
RUN_STARTED
PLANNING_STARTED
PLAN_CREATED
STEP_STARTED(knowledge)  ┐
STEP_STARTED(code)       ├─ 时间区间允许重叠
STEP_STARTED(data)       ┘
[各自内部安全事件]
STEP_COMPLETED(knowledge)
STEP_COMPLETED(code)
STEP_COMPLETED(data)
STEP_STARTED(synthesis)
MODEL_STARTED / MODEL_COMPLETED
OUTPUT_DELTA             # 唯一 final source
STEP_COMPLETED(synthesis)
RUN_COMPLETED(SUCCEEDED)
```

### 7.4 required Agent 失败

```text
... PLAN_CREATED
STEP_STARTED(required specialist)
STEP_FAILED(safe error code)
STEP_BLOCKED(synthesis, DEPENDENCY_NOT_SUCCESSFUL)
ERROR
RUN_COMPLETED(FAILED, stop_reason=AGENT_STEP_FAILED 或现有失败映射)
```

不启动 Synthesis，不产生 `OUTPUT_DELTA`，不按 agent 名称特殊判断。

### 7.5 optional Agent 失败

**MVP+ 不支持此执行语义。** `AgentTaskSpec.required=False` 在 PlanCompiler 阶段导致：

```text
RUN_STARTED
PLANNING_STARTED
PLANNING_FAILED(error_code=OPTIONAL_DEPENDENCY_UNSUPPORTED)
ERROR
RUN_COMPLETED(FAILED, stop_reason=PLANNING_FAILED)
```

以后支持 optional 时必须先扩展 dependency edge、Scheduler readiness、Run completion 和 SynthesisInput 的 missing/failed envelope，不能仅让 Scheduler 忽略失败。

### 7.6 Synthesis 失败

```text
... specialists STEP_COMPLETED
STEP_STARTED(synthesis)
MODEL_STARTED
MODEL_FAILED / STEP_FAILED(synthesis)
ERROR
RUN_COMPLETED(FAILED, stop_reason=SYNTHESIS_FAILED)
```

无 fallback 拼接，无 `OUTPUT_DELTA`。

### 7.7 用户取消

```text
RUN_STARTED
[PLANNING_STARTED 或多个 STEP_STARTED]
CANCELLATION_REQUESTED
PLANNING_FAILED(CANCELLED) 或运行中 STEP_CANCELLED
未运行的依赖 Step 收敛为 CANCELLED/BLOCKED
RUN_COMPLETED(CANCELLED, stop_reason=USER_CANCELLED)
```

同步外部调用无法硬终止时，保持现有 bounded/detached worker 规则：Run 可终结，Store 立即 seal；worker 最终退出后 clear。不得把“Run 已取消”夸大为“所有底层线程已被物理杀死”。

## 8. File Impact Estimate

以下是实施影响估算，不是本轮实际变更。

### 8.1 建议新增

| 文件 | 职责 | MVP 必需 |
|---|---|---|
| `core/runtime/agent_registry.py` | immutable AgentRegistration、查找和静态注册 | 是 |
| `core/runtime/multi_agent_planning.py` | AgentTaskSpec、PlanProvider、严格解析 | 是 |
| `core/runtime/plan_compiler.py` | 三种图形、权限、唯一输出和 DAG 编译 | 是 |
| `core/runtime/step_result_store.py` | Run-scoped 单写、授权读、容量、seal/clear | 是 |
| `core/runtime/output_gate.py` | OutputPolicy、唯一 final publish、防中间泄漏 | 是 |
| `core/runtime/multi_agent_driver.py` | claim -> Registry adapter -> StepResult | 是 |
| `core/runtime/synthesis.py` | 白名单输入构造和 synthesis adapter | 可与 driver 合并；独立更清晰 |

### 8.2 建议修改

| 文件 | 预计修改 | MVP 必需 |
|---|---|---|
| `server.py` | 不改 API 形状；确保默认主链进入新 scope | 是 |
| `core/chat_service.py` | planning 期间正式 producer/consumer 生命周期、错误映射 | 是 |
| `core/agent_router.py` | 暴露不持久化的 Agent 调用 adapter；移除默认主链对 Legacy orchestration 的依赖 | 是 |
| `core/runtime/runtime_factory.py` | 装配 Registry、PlanProvider、Store、Gate、MultiAgentDriver；并发不再固定 1 | 是 |
| `core/runtime/run_coordinator.py` | plan XOR provider、一次冻结、规划终结语义、延迟 scheduler/checkpoint | 是 |
| `core/runtime/planning.py` | 最小扩展可 fingerprint 的 output/execution 合同 | 是 |
| `core/runtime/plan_graph.py` | 复用为 compiler 校验；可能只需测试 | 视实现 |
| `core/runtime/scheduler.py` | required 路径原则上复用；增加清晰 stop mapping | 小改/可能无需 |
| `core/runtime/parallel_execution.py` | 删除字符串自动发布，接入 StepResultStore/OutputGate | 是 |
| `core/runtime/events.py` | planning/plan 事件、safe projection、schema 版本 | 是 |
| `core/runtime/stream_adapter.py` | 新控制事件和 terminal 映射，保持正文隔离 | 是 |
| `core/runtime/checkpoint.py` | 动态 Plan 冻结后才建立/捕获 checkpoint | 是 |
| `core/runtime/snapshot_contract.py` | fingerprint 覆盖新的静态 Step 执行/输出字段 | 是 |
| `core/runtime/recovery_validation.py` | 明确无 Plan/无 StepResult 时不可恢复 | 是 |
| `core/runtime/state.py` | 评估新增 PLANNING_FAILED/SYNTHESIS_FAILED stop reason | 待 GPT 决议 |
| `core/memory_manager.py` | 原则上不改；增加“不持久化 specialist result”回归测试 | 否 |
| `main.py` | 兼容 Coordinated planning/delegation event shape 的状态展示 | 是，非 DAG UI |

### 8.3 建议新增/修改测试

| 测试范围 | 重点 | MVP 必需 |
|---|---|---|
| Registry/Compiler 单测 | 未知 Agent、环、缺失依赖、非法 policy、多个 final、三种合法图 | 是 |
| Coordinator 生命周期 | static Plan 兼容、一次冻结、planning failure/cancel/timeout/budget | 是 |
| Scheduler/Executor | required fail-closed、真实并行区间、report/result 顺序 | 是 |
| StepResultStore | 并发单写、越权读、重复写、大小限制、seal/clear | 是 |
| OutputGate | INTERNAL 永不输出、透传原样、唯一发布、duplicate reject | 是 |
| Synthesis | 只读 depends_on、missing required 不调用、失败无 fallback | 是 |
| 安全回归 | Journal/Snapshot/log/trace/event/memory 不含原始结果 | 是 |
| Streaming/UI | 新事件不混入正文、terminal 唯一、前端 status shape 正确 | 是 |
| 默认 `/api/chat` E2E | 显式知识路由、双 Agent 并行+synthesis、失败路径 | 是 |
| Legacy/单 Agent 回归 | Legacy 可显式启用、现有 Coordinated 单 Agent不退化 | 是 |
| Snapshot/Recovery | static 语义保留、dynamic plan 后 checkpoint、结果不可恢复 | 是 |

## 9. Acceptance Review

### 9.1 原 30 条逐项结论

| # | 结论 | Round 1 调整/可测试判据 |
|---:|---|---|
| 1 | 保留 | 默认 `/api/chat` 的 event/journal 证明确有 knowledge step 与 retrieval，不接受仅回答文本看似正确 |
| 2 | 保留 | 证明 code_expert step 实际开始/完成 |
| 3 | 保留 | 除开始完成事件外，断言两个 Step 的执行时间区间重叠 |
| 4 | 保留 | synthesis invocation count 恰好为 1 |
| 5 | 保留 | 完整 event wire payload 与响应正文均不得出现 specialist 原文 |
| 6 | 保留 | 单 knowledge Plan 原样、一次发布；无额外 synthesis model invocation |
| 7 | 保留 | compiler + OutputGate 双重断言唯一 final source/publish |
| 8 | 保留 | 未知 agent -> planning/compile failure，不回退 core_router 自答 |
| 9 | 保留 | 不存在依赖 -> compile failure |
| 10 | 保留 | PlanGraphValidator 拒绝环 |
| 11 | 保留 | 多 final -> compile failure |
| 12 | 修改 | “非法”以 AgentRegistration 的 output permission 和三种图形为准，而非“普通专家”文字判断 |
| 13 | 保留 | 复用 Scheduler 现有失败传播；synthesis 必须 BLOCKED 且未调用 |
| 14 | 修改/延期 | MVP+ 不支持 optional；`required=False` 必须在编译期明确失败。以后另立验收矩阵 |
| 15 | 保留 | synthesis 失败无拼接、无 final `OUTPUT_DELTA` |
| 16 | 保留 | 对 async 调用断言终止；对不可硬杀的 sync 调用断言 Run 终结、worker detached 被观测、Store sealed |
| 17 | 保留 | deadline 覆盖 planner、specialist、synthesis；不可硬杀语义按现有 runtime 规则表达 |
| 18 | 保留 | Budget 覆盖 Planner、每个 specialist、synthesis，而非只覆盖 Agent Step |
| 19 | 修改 | 要求不同 safe error_code 和事件序列；是否新增专用 StopReason 枚举列为本轮分歧，不把枚举数量当事实 |
| 20 | 保留 | 搜索 journal records/持久化后端，不含原始结果 |
| 21 | 保留 | snapshot 只允许 length/digest 等摘要 |
| 22 | 保留 | caplog/结构日志中无原始结果和 Planner 原文 |
| 23 | 保留 | 除最终 `OUTPUT_DELTA` 外，RuntimeEvent 不携带原始结果；内部 Step 只发元数据 |
| 24 | 修改 | 正常路径立即 seal+clear；detached worker 路径立即 seal、worker 终止后最终 clear，需断言最终清理而非竞态销毁 |
| 25 | 保留 | consumer 不在 compiled depends_on 中时读取失败；不得提供 get_all |
| 26 | 保留 | 默认 exporter 属性中无原始结果 |
| 27 | 保留 | 显式 runtime=legacy 仍可工作 |
| 28 | 保留 | static single-agent Coordinated 路径、事件和正文不退化 |
| 29 | 保留 | 现有 Snapshot、Journal、Streaming、Cancellation、Recovery validation 全量回归 |
| 30 | 保留 | 必须走真实默认 API/RuntimeFactory/Router；CI 可 fake 外部 provider，但不得 fake 掉 Registry、Compiler、Coordinator、Driver、Store、Gate |

原 30 条中：保留 26 条，修改 4 条（12、14、19、24），删除 0 条。

### 9.2 建议新增验收

31. Planner 调用计入同一 Budget，响应 cancellation/deadline，并且所有失败路径只有一个 terminal event。
32. 每个动态规划 Run 只生成并冻结一个 Plan/fingerprint；`PLAN_CREATED` 后不可变。
33. 显式 Agent 请求走确定性规则，不额外调用 Planner model。
34. Planner 原始输出、prompt 和 schema 错误片段不进入日志/Journal/Trace。
35. OutputGate 拒绝第二次 final publish，即使 driver/executor 重试或重复回调。
36. 动态规划 Run 仅在 `PLAN_CREATED` 后创建 checkpoint；恢复验证明确拒绝缺少 StepResult 的中途恢复。
37. 专业 Agent 原始结果不写 Memory；只持久化原始用户消息和唯一 final answer。
38. 前端正确解析 Coordinated planning/delegation 事件对象，不把它们混入正文。
39. 每 Run Agent 数、并发、单结果、总结果和总预算都有硬限制并可测试。
40. 单 knowledge 失败时不回退 core_router 编造回答。
41. Synthesis 运行时没有访问全量 Memory、Journal、Registry 中未执行 Agent 结果的接口。
42. 事件顺序满足 `RUN_STARTED < PLANNING_STARTED < PLAN_CREATED < STEP_STARTED`；规划失败没有 STEP event。

## 10. Known Limitations

MVP+ 完成后仍明确不支持：

- 运行时动态 Agent 注册/卸载、插件市场和多租户 Registry。
- recursive delegation、Agent 自建子 Agent、Agent-to-Agent 自主通信、多轮协商。
- Planner 循环调用、运行中增加/删除 Step、循环工作流、synthesis 后再调用专家。
- optional dependency 的执行和降级策略；MVP 所有依赖 required。
- 多个用户可见 final source、专业 Agent 的 raw token streaming。
- 原始专业结果的持久化和 crash 后 StepResult rehydration；中途恢复到 synthesis 不可用。
- 完整前端 DAG 可视化、每 Agent 独立 Memory、HITL 审批节点、全量 chaos matrix。
- 对模型输出“绝不新增事实”的自动证明。输入白名单和 fail-closed 只能降低风险，不能形式化消灭幻觉。
- 真正无限数量并发。架构可注册 N 个 Agent，但实际执行受并发、预算、结果容量和 timeout 上限约束。

## 11. Open Disagreements for GPT

### D1（P0）：Plan 生命周期选 A 还是 B

- **Codex 当前立场：** 选受控 A，planning 属于正式 Run；保留 static-plan 兼容入口。
- **依据：** `runtime_factory.py:300-390` 与 `chat_service.py:336-359` 显示当前 scope 构造期间没有正式 producer/Coordinator 执行；纯 B 无法自然提供 Run 事件、取消和 terminal。
- **请 GPT 回答：** 是否接受“规划失败也必须是一个已开始且能正常终结的 Run”这一前提？若仍选 B，请给出不引入第二 Parent Runtime 的 RunHandle 注册、stream consumer、Budget 结算和 terminal owner。
- **无法一致时的保守决策：** A；宁可增加一次性 Coordinator 状态，也不允许双生命周期。

### D2（P0）：OutputGate 由谁调用

- **Codex 当前立场：** OutputGate 独立拥有策略，ParallelExecutor 在 driver 返回后调用它；Executor 不做 policy 决策。
- **依据：** `parallel_execution.py:223-232` 当前输出发生在 Step terminal 前；`run_coordinator.py:399-440` 当前丢弃 report。改由 Coordinator 发布会扩大 report 跨层改造并改变事件顺序。
- **请 GPT 回答：** 是否接受“调用机制在 Executor、授权决策在 Gate”的职责拆分？若坚持 Coordinator，请说明如何保持 streaming 顺序和并行 batch 中唯一输出。
- **无法一致时的保守决策：** 注入 OutputGate 到 Executor，并用合同测试证明 Executor 无策略分支。

### D3（P1）：output policy 如何进入 Plan fingerprint

- **Codex 当前立场：** 版本化扩展 PlanStep/PlanStepSnapshot 的静态执行字段，保证 output/execution 语义参与 fingerprint；不使用未 fingerprint 的 sidecar。
- **依据：** `scheduler.py:320-334` 会绑定 Plan 语义；`checkpoint.py:203-257` 只从 PlanSnapshot 计算 fingerprint。
- **请 GPT 回答：** 是接受最小扩展 PlanStep，还是提出同样 immutable、同样参与 fingerprint 的 `CompiledPlan`，并说明如何避免重复建模。
- **无法一致时的保守决策：** 扩展并 bump snapshot/event schema，避免恢复时策略漂移。

### D4（P1）：optional dependency 是否进入 MVP+

- **Codex 当前立场：** 不进入；`required=False` 明确 compile-fail。
- **依据：** `scheduler.py:336-390` 的 readiness 是所有依赖成功，没有 edge policy；加入 optional 会同时修改 Scheduler、Run completion、SynthesisInput 和失败矩阵。
- **请 GPT 回答：** 是否同意调整验收项 14？若不同意，请给出不扩大状态机的最小 edge contract 和 terminal 语义。
- **无法一致时的保守决策：** 全 required，避免错误降级。

### D5（P1）：StopReason 是新增枚举还是只用 error_code

- **Codex 当前立场：** 倾向新增 `PLANNING_FAILED`、`SYNTHESIS_FAILED`；普通 specialist 失败可用稳定 Step error + 通用 run failure，避免为每类 Agent 扩枚举。
- **依据：** `state.py:51-63` 当前无 planning/synthesis reason；`run_coordinator.py:442-470` 多数失败映射到通用 `UNHANDLED_ERROR`，不满足清晰诊断。
- **请 GPT 回答：** 是否接受两个 lifecycle 级 StopReason，还是要求 StopReason 保持粗粒度、细节全部放 error_code？
- **无法一致时的保守决策：** 新增两个明确枚举并版本化文档。

### D6（P1）：动态规划 Run 的 PRE_RUN checkpoint

- **Codex 当前立场：** Plan 冻结前不能创建 Plan checkpoint；动态规划 Run 的第一个 checkpoint 定义为 `POST_PLAN_PRE_EXECUTION`，static Plan 路径保留原 PRE_RUN。
- **依据：** `run_coordinator.py:178-214` 和 `checkpoint.py:203-257` 都要求具体 Plan；`snapshot_contract.py:190-252` 的 PlanStepSnapshot/fingerprint 不能表示“待规划”。
- **请 GPT 回答：** 是否接受这个兼容性例外，还是认为必须为 planning-only snapshot 新建 contract？
- **无法一致时的保守决策：** 不创建无 Plan snapshot，明确不可恢复 planning 阶段。

### D7（P1）：专业结果是否允许写 Memory

- **Codex 当前立场：** 不允许；MVP+ 只在 Store 保存，最终只写 user + final answer。
- **依据：** `memory_manager.py:155-180` 会保存 raw content；需求明确排除原始专业结果持久化。
- **请 GPT 回答：** 是否同意不复用 Legacy orchestration memory persistence？若需要审计，请限定为 digest/producer/length 元数据。
- **无法一致时的保守决策：** 不写 raw Memory。

### Round 1 Consensus Matrix

| 议题 | GPT 初始立场 | Codex Round 1 立场 | 是否一致 | 暂定决策 | 源码依据 |
|---|---|---|---|---|---|
| Plan 生命周期 | 优先外部 Bootstrap | 正式 Run 内受控 A | 否 | 待 GPT 回应；保守 A | `runtime_factory.py:300-390`; `chat_service.py:336-359`; `run_coordinator.py:140-214` |
| Typed Planner + Compiler | 支持 | 支持 | 是 | 保留 | `planning.py:48-96`; `plan_graph.py:64-101` |
| Static Registry | 支持 | 支持，迁移现有 map | 是 | 保留 | `agent_router.py:191-213` |
| StepResultStore | 支持 | 支持，run-scope 所有 | 基本一致 | 待确认 cleanup/detached 细节 | `parallel_execution.py:71-91`; `recovery_validation.py:689-724` |
| OutputPolicy | 支持三类策略 | 支持，必须 fingerprint | 基本一致 | 保留 | `parallel_execution.py:223-232`; `checkpoint.py:203-257` |
| Output 发布 owner | Coordinator 或 OutputGate | Executor 调用独立 Gate | 部分一致 | 待 GPT 回应 | `run_coordinator.py:399-440`; `parallel_execution.py:223-232` |
| required fail-closed | 支持 | 支持，复用 Scheduler | 是 | 保留 | `scheduler.py:336-390` |
| optional dependency | 建议按策略继续 | MVP 排除 | 否 | 待 GPT 回应；保守 compile-fail | `scheduler.py:336-390` |
| Synthesis 白名单 | 支持 | 支持，dependency-scoped view | 是 | 保留 | `agent_router.py:1576-1605`; `memory_manager.py:155-180` |
| Synthesis 失败 fallback | 默认不允许拼接 | 不允许 | 是 | fail-closed | 当前 Coordinated 无该合同 |
| StepResult crash recovery | MVP 可明确不支持 | 不支持 | 是 | 保留非目标 | `recovery_validation.py:689-724` |
| 进入实施 | 达成共识后 | 当前 NO | 是 | 等待后续轮次 | 本节 D1–D7 |

## 12. Final Codex Position

```text
Codex recommendation:
- Plan lifecycle: 选择受控方案 A；planning 属于正式 Run，Plan 仅冻结一次，static-plan 路径保持兼容
- Result store: 新增独立 Run-scoped StepResultStore；单写、依赖授权读、容量限制、终结 seal/clear、不持久化、不恢复
- Output policy: 使用 INTERNAL / FINAL_PASSTHROUGH / FINAL_SYNTHESIS；PlanCompiler 校验，OutputGate 唯一发布
- Dependency failure: MVP+ 只支持 required；失败由现有 DAG/Scheduler 传播为 synthesis BLOCKED，optional 编译期拒绝
- Synthesis constraints: 只读显式依赖结果，不读全量 Memory/Journal，不允许失败后拼接降级；只能保证输入受限，不能承诺零幻觉
- MVP boundary: 静态 Registry、typed Planner、三种扁平 Plan、真实并行、单点 Synthesis、唯一 final；不做动态注册/递归/动态 Plan/optional/结果恢复/多轮协商
- Estimated implementation effort: 15–22 人日；完整动态任意 Agent 平台在此基础上另需约 30–50 人日
- Ready for implementation: NO
```

阻塞项：D1 Plan 生命周期、D2 OutputGate 调用边界，以及 D3–D7 的 P1 合同尚未得到 GPT 回应；当前也没有用户“开始实施”的明确指令。下一步仅应把本 Round 1 提交给 GPT 审查，并在 Round 2 逐项维护 Consensus Matrix，不应修改生产代码。
