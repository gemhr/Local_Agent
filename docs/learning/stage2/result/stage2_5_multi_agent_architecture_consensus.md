# Stage 2.5 Multi-Agent Architecture Consensus

> 状态：架构共识终稿。本文档是 Stage 2.5 Multi-Agent 实施的唯一设计基线；Round 1–3 仅保留为审查过程证据。

## 1. Problem Statement

当前默认聊天入口并没有可靠地把“讲讲/查找某文档”“调用知识专家”等请求编译成知识专家执行。主 Agent 可以直接生成答案，导致路由意图未被执行、知识检索链未发生，却仍向用户返回看似成功的内容。这违反“禁止主 Agent 在应调用专家时自行编造”的约束。

根因不是单一提示词，而是缺少一组可验证的运行时合同：统一 Resolver、typed Registry、可冻结动态 Plan、运行期 invocation bindings、受 ACL 约束的结果存储、唯一最终输出 Gate，以及执行成功与输出交付结果的分层表达。

## 2. Goals and Non-goals

目标：

- 默认 API 的每次 Coordinated 请求都经过正式 Resolution，得到可校验的 `ResolvedPlan`。
- 显式 specialist 选择确定性执行，隐式意图可由 Planner 规划，二者进入同一编译与运行合同。
- 支持任意注册 Agent 的并行、依赖、汇总和唯一最终输出。
- 专家失败、规划失败、依赖阻塞、交付失败均 fail closed，不允许 Core 静默兜底。
- raw instruction、专家中间结果和敏感检索内容不进入 Plan/Snapshot/Journal/Trace/日志。
- 保持 Legacy 显式模式与既有单 Agent/静态 Coordinated 路径的兼容边界。

非目标：

- 本阶段不实现跨进程恢复专家结果或 invocation bindings。
- 不把任意自然语言路由规则硬编码到 Core。
- 不允许动态新增 Agent 绕过 Registry、Compiler 或 capability policy。
- 不改变外部 `/api/chat` 主要请求/响应形状。
- 本文档不授予生产代码修改权限。

## 3. Current Code Reality

- `RunCoordinator` 校验 Plan、准备 Scheduler、按批执行；当前会等待但丢弃 `ParallelExecutionReport`，随后 Scheduler 看到所有 Step 成功即可把 Run 判为成功。
- `ParallelExecutor` 当前顺序为 `STEP_STARTED -> driver -> OUTPUT_DELTA -> Step SUCCEEDED -> STEP_COMPLETED -> report`。
- `SchedulerSnapshot.is_complete` 仅由全部 Step 为 `SUCCEEDED` 决定，因此最终交付结果必须在 Coordinator 进入下一次 Scheduler 判定前处理。
- 状态机允许所有 Step 已成功、Run 因基础设施交付错误从 `RUNNING` 转为 `FAILED`；Run 终态不要求必须存在 FAILED Step。
- `StepEmitter` 在 `STEP_COMPLETED` 后关闭；现有适配器逐事件处理文本与控制事件，既有集成测试固定 `OUTPUT_DELTA < STEP_COMPLETED`。
- EventChannel 是 journal-first；enqueue 失败可能已产生 `partially_persisted=True`。此时 StepEmitter 必须消费本地 step sequence，避免后续事件复用序号。
- 当前 Plan fingerprint 基于安全的 PlanSnapshot；Scheduler binding、Checkpoint、Recovery 均没有必须持久化 raw instruction digest 的用途。
- 当前 Recovery 不恢复专家 raw result，也不重建动态 invocation bindings。

## 4. Final Architecture

```text
API / ChatService
  -> PlanResolver
       -> explicit specialist: deterministic decision
       -> default request: Planner decision
  -> AgentRegistry policy validation
  -> PlanCompiler
       -> one immutable Plan
       -> run-scoped StepInvocationBindings
  -> Plan freeze + fingerprint
  -> Scheduler / Checkpoint / Snapshot initialization
  -> RunCoordinator
       -> ParallelExecutor
            -> MultiAgentDriver
            -> StepResultStore
            -> OutputGate (FINAL only)
       -> inspect ParallelExecutionReport
       -> dependency scheduling / terminal decision
  -> exactly one user-visible final output or explicit Run failure
```

合法执行图只有四种：

| 形态 | 图 | 最终策略 |
|---|---|---|
| 0 | Core direct | `FINAL_PASSTHROUGH` |
| 1 | 单个获准 entry specialist | `FINAL_PASSTHROUGH` |
| 2 | 单个 specialist -> synthesis | specialist `INTERNAL`，synthesis `FINAL_SYNTHESIS` |
| 3 | N 个 specialist -> synthesis | specialists `INTERNAL`，唯一 synthesis `FINAL_SYNTHESIS` |

Direct answer 是正常决策，不是任何失败路径的 fallback。

## 5. Component Ownership

| Component | 唯一职责 | 明确禁止 |
|---|---|---|
| API/ChatService | 构造请求上下文、调用 Resolver/Runtime | 自行猜测专家或拼接答案 |
| PlanResolver | 产出 typed resolution decision | 执行 Agent、发布输出 |
| AgentRegistry | Agent 身份、entry/delegated policy、capability、result type | 保存 Run 数据 |
| PlanCompiler | 校验决策并编译四种合法图 | 调用模型、运行 Step |
| RuntimeFactory | 静态或动态初始化路径二选一 | freeze 后改 Plan |
| RunCoordinator | 生命周期、批次、report 判定、Run 终态 | 读取 raw result |
| Scheduler | 依赖就绪、claim、并发调度 | 判定最终交付成功 |
| StepInvocationBindings | Run 内 raw instruction 的唯一所有者 | 持久化或恢复 |
| MultiAgentDriver | 调用指定 Agent，返回 typed result | 写 Store、调用 Gate、发用户输出 |
| StepResultStore | once-write、状态、ACL、容量、清理 | `get_all`、持久化 |
| Completion Pipeline | 校验结果、提交 Step、开放 Store、调用 Gate | 重试未知交付 |
| OutputGate | Run 级唯一最终发布、at-most-once | 发布 INTERNAL 结果 |
| Synthesis Agent | 只读取显式依赖结果并生成唯一最终结果 | 读取全量 Memory/Journal |
| EventChannel/Emitter | journal-first 事件发布与序号合同 | 以事件代替状态机提交 |

## 6. Planning and Direct Answer Contract

- 每个 Coordinated Run 都必须有正式 Resolution；默认 API 全部进入 `PlanResolver`。
- 显式选择 specialist 时采用确定性 resolution，不调用 Planner model，但仍经过 Registry、Compiler、Plan freeze 和 Runtime。
- 默认请求可由 Planner 选择 direct answer、单专家透传或专家加 synthesis。
- Resolver 只返回 typed `ResolvedPlanDecision`；Planner 的自由文本、工具输出或 JSON 不可直接作为运行 Plan。
- Compiler 只接受四种合法执行图，拒绝未知 Agent、缺失依赖、环、多最终 Step、无最终 Step、越权 output policy 和超限 Plan。
- Plan 只 freeze 一次；动态 Runtime 的 Scheduler、Checkpoint、Snapshot 必须在 freeze 后初始化。
- 动态 Plan checkpoint 点为 `POST_PLAN_PRE_EXECUTION`；规划失败时没有 Plan snapshot/checkpoint。
- 静态 factory 是内部兼容路径；静态与动态初始化互斥并有 guard。
- Planner 受同一 Run cancel、总 deadline 和预算控制。Planner 自身上限命中与 Run 总 deadline 命中必须区别映射。
- 任何规划、schema、编译或执行失败都不得回退到 Core 直接回答。

## 7. Registry Contract

Registry 必须是 typed、只读策略源，并分别声明 entry 与 delegated policy：

| Agent | Entry policy | Delegated policy |
|---|---|---|
| `core_router` | 允许 `FINAL_PASSTHROUGH` | 不作为 specialist |
| `knowledge_expert` | 允许 `FINAL_PASSTHROUGH` | 仅“唯一 knowledge task、无其他 Step”可 direct；其他为 `INTERNAL` |
| `code_expert` | 允许 `FINAL_PASSTHROUGH` | `INTERNAL` |
| `data_analyst` | 允许 `FINAL_PASSTHROUGH` | `INTERNAL` |
| `synthesis_agent` | 禁止 entry | 仅 `FINAL_SYNTHESIS` |

Registry 至少声明：稳定 `agent_id`、driver/provider、entry/delegated policy、输入/输出 type、capabilities、并发与预算上限。未知、禁用或不具备 capability 的 Agent 必须在编译期失败。支持任意多 Agent 指“按 Registry 扩展”，不代表模型可凭空发明 Agent。

## 8. Plan and Invocation Data Boundary

持久化 Plan 仅保存安全调度合同，例如：

- `step_id`、title/安全摘要、显式 `depends_on`
- `preferred_agent`
- `execution_kind`
- `output_policy`
- capability 与安全预算/上限
- schema/contract version

raw instruction、完整查询、文件路径和敏感参数只存在 `StepInvocationBindings`：

- Run scoped、只读、键集合与 Plan Step 一致。
- repr/log safe；不得出现在异常文本、事件、Trace、Journal、Snapshot、Checkpoint。
- 只允许当前被 claim 的 Step/Driver 读取。
- Run 结束、取消或 detached worker 最终退出后清理。
- 不持久化、不恢复；Recovery 遇到需要 bindings 的未完成动态 Run 必须 fail closed。
- 可做内存内一致性校验，但 MVP 不生成或持久化 raw instruction 的 SHA/digest。

## 9. Result Store Contract

`StepResultStore` 是 Run scope、内存态、由 Completion Pipeline 唯一写入：

- 状态至少为 `PREPARED -> READABLE`；每个 producer Step 只能成功写入一次。
- 写入前校验 producer、result type、完整性、大小和 Run/Step 身份。
- 只有 producer Step 已提交 `SUCCEEDED` 后，entry 才能转为 `READABLE`。
- consumer 必须已 claim，且 producer 必须在 consumer 的显式 `depends_on` 中。
- 禁止 `get_all`；Synthesis 也只能按依赖读取。
- 设单结果、Run 总结果、条目数等硬上限，越限 fail closed。
- Snapshot、Checkpoint、Journal、Trace、日志与 Recovery 不保存/重建 Store 内容。
- 正常终态立即 seal/clear；存在 detached worker 时先 seal，待 worker 退出后完成清理。

## 10. Completion and Delivery Contract

执行成功与用户输出交付是两个不同维度。

INTERNAL Step：

```text
Driver result
  -> validate
  -> Store PREPARED
  -> Step RUNNING -> SUCCEEDED
  -> Store READABLE
  -> STEP_COMPLETED(SUCCEEDED)
  -> report delivery=NOT_APPLICABLE
```

FINAL Step：

```text
Driver result
  -> validate
  -> Store PREPARED
  -> Step RUNNING -> SUCCEEDED
  -> Store READABLE
  -> OutputGate attempt
       -> DELIVERED | FAILED | OUTCOME_UNKNOWN
  -> STEP_COMPLETED(SUCCEEDED)
  -> safe ParallelExecutionReport
  -> Coordinator checks delivery before next Scheduler success decision
```

`StepCompletionResult`/batch report 是瞬时安全控制数据，不含 raw result，至少表达：`step_id`、Step 是否已提交、output policy、delivery status、safe error code。它不进入 Snapshot、Checkpoint 或恢复合同。

已知交付失败或结果未知时，Final Step 仍为 `SUCCEEDED`，Run 为 `FAILED`。不得改写为 `AGENT_STEP_FAILED`，不得重跑 Agent，亦不得重新发布。

## 11. Output and Streaming Contract

- `INTERNAL` Step 永不产生用户文本事件；模型/tool/retrieval 的观测事件必须是安全元数据。
- 只有 `FINAL_PASSTHROUGH` 或 `FINAL_SYNTHESIS` Step 可调用 OutputGate。
- OutputGate 为 Run 级 at-most-once 状态机：`NOT_STARTED -> PUBLISHING -> PUBLISHED`；失败落入 `FAILED`，无法确认则为 `OUTCOME_UNKNOWN`。
- `PUBLISHING/PUBLISHED/OUTCOME_UNKNOWN` 均禁止重试；duplicate attempt 必须拒绝。
- 为兼容现有 EventEmitter 和测试，外部事件顺序保持 `OUTPUT_DELTA < STEP_COMPLETED`；内部 Step 状态在 OutputGate 前已经提交成功。
- 若 `OUTPUT_DELTA` journal 已持久化但 enqueue 失败，结果为 `OUTCOME_UNKNOWN`。Emitter 捕获 `partially_persisted=True` 时必须消费本地 step sequence 后再抛出，防止后续 `STEP_COMPLETED` 复用序号。
- 交付失败通过安全 `ERROR` 与 `RUN_COMPLETED(FAILED)` 表达；`STEP_COMPLETED` 仍表达 Step 执行成功。
- 只有确认 `DELIVERED` 的最终文本可提交进对话 Memory；INTERNAL 或未知交付结果不得写入。

## 12. Dependency Failure Contract

- MVP 中所有 `depends_on` 都是 required，不提供 optional dependency。
- 任一 required producer 失败、取消、超时或结果不可读，其下游变为 `BLOCKED`，不得调用 Driver。
- Synthesis 的任一 required 输入失败时，Synthesis 不执行，Run 以 `REQUIRED_DEPENDENCY_FAILED` 失败。
- 不允许删除失败依赖后继续 synthesis，也不允许以 Core 回答或拼接已成功的局部结果兜底。
- blocked 传播必须终止，不得留下永远 pending 的 Step。

## 13. Budget/Timeout/Cancellation

- Planner、全部 specialist、synthesis、completion 和 delivery 计入同一 Run 总 deadline/预算框架。
- Registry/Compiler 强制 `max_agents`、`max_steps`、并发数、Planner 调用数、每 Step 与 Run 总结果大小等硬上限。
- Provider 已返回但 schema/compile 失败，已消耗预算仍计费。
- Planner 自身 cap 命中映射 `PLANNER_TIMEOUT`；Run 总 deadline 命中沿用 `DEADLINE_EXCEEDED`。
- cancellation 沿用现有 Run/Step 语义，停止新 claim，取消可取消工作，detached worker 受 seal/最终清理约束。
- timeout/cancel 后不得调用 fallback Agent，不得重新打开 OutputGate。

## 14. Event Sequences

动态成功 Run：

```text
RUN_STARTED
PLANNING_STARTED
PLAN_CREATED
STEP_STARTED...             # 可并行
safe model/tool/retrieval events...
STEP_COMPLETED...           # INTERNAL，无 OUTPUT_DELTA
STEP_STARTED(synthesis/final)
OUTPUT_DELTA                # 唯一用户文本
STEP_COMPLETED(SUCCEEDED)
RUN_COMPLETED(SUCCEEDED)
```

Direct/显式 entry 成功 Run：

```text
RUN_STARTED
PLANNING_STARTED
PLAN_CREATED
STEP_STARTED
OUTPUT_DELTA
STEP_COMPLETED(SUCCEEDED)
RUN_COMPLETED(SUCCEEDED)
```

规划失败：

```text
RUN_STARTED
PLANNING_STARTED
ERROR(safe planning code)
RUN_COMPLETED(FAILED)
```

规划失败不得出现 `PLAN_CREATED`、`STEP_STARTED` 或 Plan snapshot。

最终交付已知失败/未知：

```text
... final driver success
[OUTPUT_DELTA may be absent or partially persisted]
STEP_COMPLETED(SUCCEEDED)
ERROR(FINAL_OUTPUT_DELIVERY_FAILED | FINAL_OUTPUT_DELIVERY_UNKNOWN)
RUN_COMPLETED(FAILED)
```

Run terminal event 仍由 Coordinator 唯一发布；Journal 中 `RUN_COMPLETED` 是重放终态的权威事件。

## 15. State/StopReason/Error Mapping

只新增一个 `StopReason.PLANNING_FAILED`。

| 场景 | Step state | Run state | StopReason | error_code |
|---|---|---|---|---|
| planning schema/compile fail | 无 Step 启动 | FAILED | PLANNING_FAILED | 具体 planning/compile code |
| Planner Run 总 deadline | 无或当前状态 | FAILED | DEADLINE_EXCEEDED | 既有 deadline code |
| Planner 自身 cap | 无 Step 启动 | FAILED | PLANNING_FAILED | PLANNER_TIMEOUT |
| cancellation | 沿用既有语义 | CANCELLED/既有 | 既有 | 既有 |
| specialist 执行失败 | FAILED | FAILED | UNHANDLED_ERROR | AGENT_STEP_FAILED |
| synthesis 执行失败 | FAILED | FAILED | UNHANDLED_ERROR | SYNTHESIS_FAILED |
| required dependency blocked | BLOCKED | FAILED | UNHANDLED_ERROR | REQUIRED_DEPENDENCY_FAILED |
| Final output 已知失败 | SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_DELIVERY_FAILED |
| Final output 结果未知 | SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_DELIVERY_UNKNOWN |

不得为 delivery failure 新增伪造的 Step FAILED 状态，也不得新增更多 StopReason 来替代精确 error code。

## 16. Snapshot/Fingerprint/Recovery Boundary

- Plan/PlanSnapshot 只包含安全、可恢复的执行合同，不包含 raw instruction、result、binding 或其 digest。
- fingerprint 至少覆盖：schema version、Step 身份与依赖、`preferred_agent`、`execution_kind`、`output_policy`、capability 和安全限制。
- 上述合同字段变化必须改变 fingerprint；raw instruction 变化不影响 fingerprint。
- 动态 Plan 只在 freeze 后产生一个 fingerprint，并在 `POST_PLAN_PRE_EXECUTION` checkpoint 使用。
- Recovery 只验证 Plan/状态/事件的既有安全合同；不恢复 Store、Bindings、OutputGate 的未确认交付。
- 需要未持久化 bindings/results 才能继续的恢复必须 fail closed，不可重新规划或自行补答。

## 17. Memory/Journal/Trace/Security Boundary

- 对话 Memory 只接收原始 user message 与确认已交付的唯一 final assistant message。
- specialist、synthesis 不读取完整对话 Memory；只接收经过组装的 user context 与显式依赖结果。
- Journal/Event/Trace/日志仅记录 safe metadata、计数、状态、稳定 ID、safe error code 和摘要长度等。
- raw instruction、文件路径、检索 query、文档 chunk、专家结果、synthesis 输入不得进入上述持久或观测通道。
- 异常、repr、dataclass dump、debug logging 和测试 failure message 同样受此边界约束。
- 不得把 hash 当作“已安全脱敏”的理由持久化 raw instruction 派生 digest。

## 18. Scope Gates

实施前必须再次确认以下 gate：

1. Core implementation gate：预计 11–16 人日，只用于验证 Resolver/Registry/Compiler、Bindings/Store、Driver/Synthesis、Completion/Gate、Coordinator/Event 主功能。
2. Contract-complete MVP+ gate：累计预计 18–26 人日，补齐恢复、安全、故障注入、前端状态与完整回归后，才可声明 Stage 2.5 完成。
3. 若实现试图改变外部 API、持久化 raw result/bindings、增加 optional dependency、跨进程结果恢复或交付重试，必须退出本 Stage 2.5 范围并重新评审。
4. 未通过四种合法图、失败不兜底、唯一输出、交付 unknown、部分持久化序号和安全泄漏测试，不得宣称完成。

## 19. Acceptance Criteria

1. 显式知识专家请求实际执行 `knowledge_expert`。
2. 显式代码专家请求实际执行 `code_expert`。
3. 两个独立领域 Step 的执行时间区间可证明重叠。
4. 多专家成功后 synthesis 恰好执行一次。
5. 所有 INTERNAL 中间结果不产生用户文本流。
6. 单 knowledge delegated direct 透传保持文本准确且不执行 synthesis。
7. 每个成功 Run 只有一个用户可见 final output。
8. 未知 Agent 导致 planning/compile fail，且无 fallback。
9. 缺失依赖在编译期失败。
10. 依赖环在编译期拒绝。
11. 多 final Step 被拒绝。
12. output policy 权限由 Registry 校验。
13. required Agent 失败使 synthesis BLOCKED 且不调用。
14. optional dependency 在 MVP 编译失败。
15. synthesis 失败不拼接局部结果、不输出 fallback。
16. 多 Agent cancel 符合既有 cancel/detached 语义。
17. Run timeout 覆盖 Planner、specialist、synthesis 和交付阶段。
18. Run 预算覆盖 Planner 与所有 Step。
19. 所有失败具有本合同规定的 StopReason/error code。
20. Journal 不含 raw result。
21. Snapshot/Checkpoint 不含 raw result。
22. 日志不含 raw instruction/result。
23. 除唯一 final output 外，事件不含 raw result。
24. Store 在正常终态清理；detached 情况先 seal 后最终清理。
25. 非依赖 consumer 读取被拒绝，且不存在 `get_all`。
26. Trace 不含 raw instruction/result。
27. 显式 Legacy 模式继续工作。
28. 静态/单 Agent Coordinated 路径无回归。
29. Snapshot、Journal、Streaming、Cancel、Recovery 既有测试通过。
30. 默认 API 的真实 multi-agent E2E 通过。
31. Planner 受相同预算/cancel/deadline，Run 只有一个 terminal outcome。
32. 动态 Plan 只 freeze/fingerprint 一次。
33. 显式 specialist route 为确定性路径，Planner model call 为零。
34. Planner raw 输出和 prompt 不进入持久化或日志。
35. OutputGate 拒绝重复发布。
36. checkpoint 只在 Plan freeze 后产生，Recovery 不假设 result 可用。
37. specialist 不读取完整 Memory，只得到 user context/显式输入。
38. 前端正确展示 Coordinated planning/step/run 状态。
39. Agent 数、Step 数、并发、结果大小、预算均有硬上限。
40. knowledge specialist 失败时绝不由 Core 回答。
41. synthesis 不读取全量 Memory/Journal/未执行结果。
42. 事件满足 `RUN_STARTED < PLANNING_STARTED < PLAN_CREATED < STEP_STARTED`；planning fail 无 Step 事件。
43. Core direct answer 是合法形态 0。
44. 模型主动选择 DIRECT 时可正常成功。
45. 所有失败路径均不转为 direct fallback。
46. raw instruction/路径/query 不出现在 Plan repr、Snapshot、Journal、Event、Trace、日志。
47. Bindings 仅当前 claim 可读，并在 Run 生命周期结束后清理且不恢复。
48. Step retry/重复完成不能覆盖 Store 已写结果。
49. retry/duplicate completion 不得产生第二次 OUTPUT。
50. Store entry 在 producer 未成功前不可读。
51. Driver 没有 Store 写权限或 Gate 能力。
52. ParallelExecutionReport 不含 raw result。
53. partial output publication 归类 unknown 且不重试。
54. 显式 selected agent 绕过 Planner model，但不绕过 Registry/Compiler/formal Run。
55. 未知 selected agent 不静默转 Core。
56. Compiler 仅接受四种定义的执行图。
57. provider 成功但 schema/compile 失败仍计入预算。
58. Planner cap 与 Run total deadline 映射不同。
59. 静态/动态 factory 互斥且有初始化 guard。
60. `preferred_agent`、`execution_kind`、`output_policy`、依赖、capability、schema version 变化会改变 fingerprint；raw instruction 不会。
61. final output 已知失败时 Final Step 成功、Run 失败。
62. final output unknown 时 Final Step 成功、Run 失败。
63. `PUBLISHING/PUBLISHED/OUTCOME_UNKNOWN` 状态均禁止发布重试。
64. delivery error 不映射为 agent failure，`STEP_COMPLETED` 仍为成功。
65. Store 转 `READABLE` 后才可进入 Gate。
66. Coordinator 在 Scheduler 作成功判断前检查当前 batch report。
67. partially persisted OUTPUT 会消费 StepEmitter 本地 sequence。
68. fingerprint 中既无 raw instruction，也无其 SHA/digest。
69. 只有确认 delivered 的 final output 写入 Memory。
70. INTERNAL completion 的 delivery 为 `NOT_APPLICABLE`，且绝不调用 Gate。

## 20. Known Limitations

- 动态 Run 的 bindings/results 不跨进程恢复；进程中断后只能 fail closed。
- journal-first 发送无法从本地 enqueue 失败判断消费者是否最终看见文本，因此必须保留 `OUTCOME_UNKNOWN`。
- 外部事件为兼容仍是 OUTPUT 在 STEP_COMPLETED 前；执行状态与交付状态需通过合同理解，不能只按文本顺序推断。
- 首版只支持 required dependencies，不支持部分结果降级或 optional edges。
- “任意多 Agent”受 Registry 和硬资源上限约束，不是无界执行。
- 既有 terminal-event publication 失败可能出现状态已终态但 terminal event 未完整送达；本阶段只要求安全显式失败与测试覆盖，不承诺分布式 exactly-once。

## 21. File Impact Estimate

预计新增生产模块：

- `core/runtime/agent_registry.py`
- `core/runtime/multi_agent_planning.py`
- `core/runtime/plan_compiler.py`
- `core/runtime/invocation_bindings.py`
- `core/runtime/step_result_store.py`
- `core/runtime/output_gate.py`
- `core/runtime/multi_agent_driver.py`
- `core/runtime/synthesis.py`
- 可选内部 `core/runtime/step_completion.py`

预计修改：

- `core/chat_service.py`、`core/agent_router.py`
- `core/runtime/runtime_factory.py`、`run_coordinator.py`、`planning.py`
- `core/runtime/parallel_execution.py`、`scheduler.py`
- `core/runtime/events.py`、`event_emitter.py`、必要时 `event_channel.py`
- `core/runtime/state.py`、`state_machine.py`
- `core/runtime/snapshot_contract.py`、`plan_fingerprint.py`、`checkpoint.py`、`recovery_validation.py`
- `core/runtime/stream_adapter.py`、前端 `main.py`
- 对应 unit/contract/integration/fault/recovery/security 测试与结果文档。

按上面的候选归属，预计触及约 20–24 个生产文件，另有对应测试与证据文档；其中部分新增职责可在实现前复核后合并到现有 owner，但不能以减少文件数为由混淆职责。工作量估算独立于文件数：Core gate 为 11–16 人日，Contract-complete 累计为 18–26 人日。delivery 分层新增的 PREPARED/READABLE、safe report 与 partial-publication 序号测试仍可落入该上界，不需上调。

## 22. Implementation Work Packages

| WP | 内容 | 主要退出条件 |
|---|---|---|
| WP1 | Registry、Resolver、Planner decision、Compiler、direct/explicit contracts | 四种图及拒绝矩阵测试通过 |
| WP2 | RuntimeFactory、Plan freeze、Coordinator planning lifecycle、checkpoint | 动态/静态互斥，事件与 fingerprint 合同通过 |
| WP3 | Bindings、Store、Driver、Synthesis、ACL/容量/清理 | raw 数据边界与依赖读取测试通过 |
| WP4 | Completion Pipeline、OutputGate、delivery report、sequence 修正 | 已知失败/unknown/duplicate/partial publication 测试通过 |
| WP5 | Memory、Journal、Trace、stream/frontend、安全审计 | 无泄漏且 UI/事件兼容测试通过 |
| WP6 | E2E、故障注入、cancel/timeout/recovery、完整回归与证据文档 | 70 条标准逐项有真实证据 |

推荐按 WP1→WP2→WP3→WP4→WP5→WP6 推进；可在合同冻结后并行编写不重叠的测试，但不得在上游合同未稳定时分叉实现不同语义。

## 23. Final Consensus Matrix

| 议题 | 最终共识 | 状态 |
|---|---|---|
| 默认路由 | 所有 Coordinated 请求经 Resolver | CONSENSUS |
| 显式专家 | deterministic，零 Planner model call，仍走正式 Run | CONSENSUS |
| 执行图 | 仅四种合法形态 | CONSENSUS |
| Registry | entry/delegated policy 分离 | CONSENSUS |
| 失败兜底 | 全部禁止 | CONSENSUS |
| raw instruction | 仅 Run-scoped Bindings；不持久化 digest | CONSENSUS |
| 中间结果 | Run-scoped Store、once-write、dependency ACL | CONSENSUS |
| 用户输出 | 唯一 OutputGate、at-most-once | CONSENSUS |
| Step/交付分层 | Step 可成功而 Run 因 delivery 失败 | CONSENSUS |
| 事件兼容 | 外部 OUTPUT_DELTA 在 STEP_COMPLETED 前 | CONSENSUS |
| partial publication | unknown、不重试、消费已持久化 sequence | CONSENSUS |
| 依赖失败 | required、BLOCKED、无降级 | CONSENSUS |
| StopReason | 仅新增 PLANNING_FAILED | CONSENSUS |
| fingerprint | 覆盖安全执行合同，不含 raw instruction/digest | CONSENSUS |
| Recovery | 不恢复 Bindings/Store/未知交付 | CONSENSUS |
| 剩余 P0 | 0 | CLOSED |
| 剩余 P1 | 0 | CLOSED |

本文档取代各轮评审中的候选方案，成为后续实现、测试、验收和审查的唯一设计依据。若实现发现必须偏离本文合同，应停止相关实现并重新进行架构评审。

## 24. Implementation Authorization Boundary

该文档只表示架构达成共识。
未收到用户明确“开始实施”前，不得修改生产代码。
