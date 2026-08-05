# LocalAgent Stage 2.5 Multi-Agent WP2 Implementation Result

## 1. Executive Summary

WP2 已完成。默认 Coordinated API 现在统一进入 `PlanResolver`，Planning 已成为正式 Run 阶段，并实现 `RUN_STARTED -> PLANNING_STARTED -> PLAN_CREATED`、一次性 Plan freeze、freeze 后执行组件初始化、`POST_PLAN_PRE_EXECUTION` checkpoint、`PLANNING_FAILED` 映射、统一模型调用、fingerprint v2、Bindings 终态清理以及单 Step 动态执行。

当前可执行形态：Core direct、显式 knowledge/code/data entry、唯一 delegated knowledge direct。多 Step、`SYNTHESIS` 或 `INTERNAL` Plan 会在任何 Step 开始前以 `MULTI_AGENT_EXECUTION_NOT_READY` fail closed；这是 WP3 前的临时准入保护，不代表多 Agent 已可执行。

本轮未实现 WP3～WP6：没有 `MultiAgentDriver`、Adapter Factory、StepResultStore、Synthesis Runtime、OutputGate、DeliveryStatus、INTERNAL 结果通道或前端 DAG。Legacy 显式模式保持原链路。

最终证据：WP1 基线 `68 passed`；WP2 新增专项 `20 passed`；关键 Runtime/E2E/Snapshot/Recovery 回归 `97 passed, 4 subtests passed`（新增指标测试另行覆盖）；全仓在指标补充前为 `1183 passed, 42 subtests passed`，指标补充后的最终全仓结果见第 13 节。

## 2. Source Audit Before Changes

以下为实施前对真实源码的审计结论，不是从架构文档反推：

| 审计项 | 变更前真实事实 |
|---|---|
| Factory 初始化顺序 | `create_run_scope` 先创建 Context/Cancellation、BudgetLedger、ActivityTracker、AgentState；随后立即调用 `build_single_agent_plan`，再构造 StateMachine、Policy、Channel/Emitter、Scheduler、ParallelExecutor、RunHandle、RunCoordinator、单 Agent Driver、Scope，最后绑定 abort 并注册 RunHandle。变更前关键位置为 `runtime_factory.py` 的旧行 254、305、326-343、390。 |
| Coordinator concrete 依赖 | 公开 `RunCoordinator.__init__` 强制接收 concrete `Plan`、`SerialScheduler`、`ParallelExecutor`；构造期校验/注册所有 Step，并在启用 Snapshot 时立即创建 Plan-bound `CheckpointCoordinator`。旧关键位置为 `run_coordinator.py` 行 143、204。 |
| 组件构造时机 | AgentState Step、Scheduler、Executor、CheckpointCoordinator 全部在 `scope.execute()` 前存在，无法表达尚未规划的 dynamic Run。 |
| RUN_STARTED | `RunCoordinator.execute` 把 AgentState 置为 RUNNING 后调用 `_emit_run_started`；旧关键位置为 `run_coordinator.py` 行 307、662-667。 |
| Scope / RunHandle | Factory 返回 Scope 前已把 RunHandle 注册到 application-scoped RunRegistry；Coordinator execute 只在缺失时补注册。当前对应位置为 `runtime_factory.py:570`、`run_coordinator.py:615`。因此 Planning 期间可通过同一个 Handle 取消。 |
| ChatService 并发顺序 | ChatService 先创建 scope，再创建 producer task 并绑定，随后才创建/消费 EventChannel iterator，见 `core/chat_service.py:269,318,345-356`。 |
| Cancellation / Deadline / Budget | Factory 创建同一 RunContext/CancellationToken，BudgetLedger 通过 `remaining_seconds` 绑定总 deadline；Coordinator 在 RUN_STARTED 后启动 deadline watcher；执行和模型调用持续调用 `raise_if_inactive`。当前对应位置为 `runtime_factory.py:426-430`、`run_coordinator.py:1161`。 |
| 旧 Driver Binding | `CoordinatedSingleAgentDriver` 构造期固化 `step_id=answer`、selected agent 和原 query；claim 不匹配即失败，无法消费动态 Binding。 |
| Snapshot / fingerprint | 变更前 `PLAN_SNAPSHOT_SCHEMA_VERSION=1`，`static_execution_kind` 被硬编码为 `AGENT`，没有 `output_policy`；fingerprint schema 为 1。旧位置为 `snapshot_contract.py` 行 31、243。 |
| Static/Recovery/fixtures | static Factory、Recovery 和大量测试直接依赖 `RunCoordinator(plan=...)`；因此保留兼容构造器并新增显式 classmethod，而不是删除旧入口。 |
| Terminal/stream/reducer | `RunCompletedPayload` 已以字符串承载 status/stop_reason；Stream adapter 使用 control-event allowlist；Journal reducer区分 reduced/ignored event。新增 StopReason/Event 必须同步这些穷举边界。 |
| Checkpoint phase | 变更前 `CheckpointKind` 没有能准确表达 Plan 已冻结但 Step 未开始的 phase，不能用 `PRE_RUN` 冒充。 |

## 3. Files Changed

### 生产代码

| 文件 | WP2 职责 |
|---|---|
| `core/runtime/runtime_factory.py` | 默认 dynamic scope、显式 static scope、延迟 execution factory、Resolver/Compiler/adapter 接线、单 Step Driver |
| `core/runtime/run_coordinator.py` | static/dynamic 构造、Plan freeze 状态机、Planning 生命周期、checkpoint、错误映射、gate、Bindings 清理、Planning metrics |
| `core/runtime/planning_model_adapter.py` | 通过既有 bounded blocking executor 调用 AgentRouter 统一模型入口 |
| `core/agent_router.py` | 新增 strict Planner prompt 和 `complete_planning_decision`，内部复用 `_invoke_model_contract` |
| `core/runtime/multi_agent_planning.py` | 增加 `PLANNER_TIMEOUT`；允许 `asyncio.CancelledError` 原样传播 |
| `core/runtime/model_invocation.py` | Model events 支持 Run-level emitter，使 Planning model 不伪造 Step |
| `core/runtime/blocking_executor.py` | 增加安全任务类别 `PLANNING_MODEL` |
| `core/runtime/events.py` | 新增 PLANNING_STARTED / PLAN_CREATED 及安全 payload |
| `core/runtime/stream_adapter.py` | Planning events 作为 control event 编码，不进入正文 |
| `core/runtime/journal_tail_reducer.py` | Planning facts 为 recovery-safe ignored events |
| `core/runtime/state.py` | 新增 `StopReason.PLANNING_FAILED` |
| `core/runtime/checkpoint_contract.py`、`checkpoint.py` | 新增并验证 `POST_PLAN_PRE_EXECUTION` |
| `core/runtime/snapshot_contract.py` | Plan Snapshot v2；持久化 execution kind/output policy；v1 读取兼容 |
| `core/runtime/plan_fingerprint.py` | fingerprint v2 与 legacy v1 重建 |
| `core/runtime/recovery_validation.py` | v1 fingerprint 校验；POST checkpoint 验证后明确不可恢复 |
| `core/runtime/metrics.py` | 安全低基数 Planning source/status counter 和 duration histogram |
| `core/runtime/__init__.py` | 导出 WP2 稳定合同 |

### 测试与 fixture

- 新增 `tests/test_dynamic_planning_lifecycle.py`、`tests/test_planning_model_adapter.py`。
- 修改 Factory、Streaming、Runtime Event、Runtime Mode/E2E、Coordinator、Model Invocation、Snapshot/fingerprint、Recovery、Metrics 和故障注入相关测试，使旧 static 假设明确迁移为 dynamic 或显式 static。
- 未删除测试，未放宽终态、安全或事件顺序断言。

## 4. Dynamic Lifecycle Contract

### Dynamic 默认路径

```text
/api/chat
-> ChatService.stream_coordinated_agent_events
-> CoordinatedRuntimeFactory.create_run_scope
-> RunContext / AgentState / Budget / EventChannel / RunHandle
-> RunCoordinator.for_dynamic_resolver
-> scope.execute
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
-> execution admission check
-> allowed single Step execution
```

Factory 接线见 `core/runtime/runtime_factory.py:322-349,461,511-523`；Coordinator 的互斥入口、状态和 freeze 见 `core/runtime/run_coordinator.py:101,201-206,348-401`。dynamic 构造期 `plan/scheduler/executor/checkpoint_coordinator` 均为空；`ResolvedPlan` 到达后只允许 `RESOLVING -> FROZEN`。Plan 与 Bindings 对外只有只读 property，第二次 prepare/freeze 明确失败。

### Static 兼容路径

```text
internal caller
-> CoordinatedRuntimeFactory.create_static_run_scope
-> trusted compatible single-step Plan
-> RunCoordinator.for_static_plan
-> existing Scheduler / ParallelExecutor lifecycle
```

Static path 不发布 Planning event，不是 schema/model/unknown-agent/执行失败 fallback。Factory static 兼容入口只接受一个 `answer + AGENT + FINAL_PASSTHROUGH` Step；更一般的内部 static Plan 仍可直接使用 `RunCoordinator.for_static_plan` 与匹配 Driver。

## 5. Planning Model Adapter

`UnifiedPlanningModelAdapter` 位于 `core/runtime/planning_model_adapter.py:13-48`：

- 使用 application-scoped `coordinated_step_executor`，任务类别为 `PLANNING_MODEL`；没有新增 requests/httpx/provider SDK。
- 调用 `AgentRouter.complete_planning_decision`，后者在 `core/agent_router.py:1524-1570` 复用 `_invoke_model_contract`，因此沿用 ModelInvocationRouter 的 BudgetLedger、候选路由、Retry、Circuit、Trace、MODEL_STARTED/MODEL_COMPLETED 和 provider fallback。
- 同一 `PlanningRequest.user_request` 只进入模型 messages 和内存态 parser；raw response 不进入异常、事件、Journal、Trace、Snapshot 或日志。
- Direct schema 禁止 instruction；有效 Agent 集合和 specialist 职责在 prompt 中明确，Compiler 仍是最终权限 owner。
- adapter 收到 task cancellation 时调用既有 `cancel_or_detach`；不可强杀的同步 provider worker 继续由 bounded executor/application shutdown owner 管理，不创建第二套线程池或 retry。

Planning cap 使用：

```text
min(run_context.remaining_seconds(), configured planning timeout)
```

若最小值来自总 deadline，稳定映射 `DEADLINE_EXCEEDED`；若来自独立 cap，映射 `PLANNING_FAILED / PLANNER_TIMEOUT`。实施中发现并修复了边界竞态：`wait_for` 可能略早于 deadline watcher 返回，现通过“哪个上限实际限制 effective timeout”确定分类，不再依赖抖动后的零值比较。

Deterministic resolution 不调用 Planning model、不产生 model reservation；Planning source/status 和时长记录在 `runtime_planning_total`、`runtime_planning_duration_seconds`，定义见 `core/runtime/metrics.py:192,221`，记录点见 `core/runtime/run_coordinator.py:440-493`。指标标签仅为固定 source/status，不含请求或 ID。

## 6. Event Contract

Payload 合同位于 `core/runtime/events.py:23-24,61-83`：

- `PLANNING_STARTED`：仅 schema version 与 configured timeout；不记录 user request，也不直接记录未经 Registry 验证的 selected-agent 原文。
- `PLAN_CREATED`：plan ID/version、fingerprint、step count、planning source；不含标题、instruction、Binding、query/path。

事件序列：

```text
success:
RUN_STARTED -> PLANNING_STARTED -> [MODEL_*] -> PLAN_CREATED
-> STEP_STARTED -> ... -> RUN_COMPLETED

planning failure:
RUN_STARTED -> PLANNING_STARTED -> [MODEL_*] -> ERROR
-> RUN_COMPLETED(FAILED)

multi-step not ready:
RUN_STARTED -> PLANNING_STARTED -> [MODEL_*] -> PLAN_CREATED
-> ERROR(MULTI_AGENT_EXECUTION_NOT_READY) -> RUN_COMPLETED(FAILED)
```

Planning events 被 Stream adapter 作为 control JSON 处理，见 `core/runtime/stream_adapter.py:51-75`；Reducer 将其作为已验证但不改变恢复投影的事实，见 `core/runtime/journal_tail_reducer.py:50-51`。

## 7. StopReason and Error Mapping

| 场景 | RunStatus | StopReason | error_code |
|---|---|---|---|
| schema 非法 | FAILED | PLANNING_FAILED | PLANNER_SCHEMA_INVALID |
| schema version | FAILED | PLANNING_FAILED | PLANNER_SCHEMA_VERSION_UNSUPPORTED |
| unknown/disabled/permission Agent | FAILED | PLANNING_FAILED | Registry safe code |
| Compiler 拒绝 | FAILED | PLANNING_FAILED | PlanCompileErrorCode |
| model ordinary failure | FAILED | PLANNING_FAILED | PLANNING_MODEL_FAILED |
| Planner independent cap | FAILED | PLANNING_FAILED | PLANNER_TIMEOUT |
| Run total deadline | FAILED | DEADLINE_EXCEEDED | DEADLINE_EXCEEDED |
| Budget exhausted | FAILED | BUDGET_EXHAUSTED | BUDGET_EXHAUSTED |
| request/client/shutdown cancellation | CANCELLED/既有 | 既有 cancellation StopReason | 既有 code |
| WP3 前多 Step | FAILED | UNHANDLED_ERROR | MULTI_AGENT_EXECUTION_NOT_READY |

`PLANNING_FAILED` 定义在 `core/runtime/state.py:64`；Coordinator typed mapping 位于 `core/runtime/run_coordinator.py:921` 附近。Cancellation、Deadline、Budget 的 catch 顺序先于 PlanningError，不会被归并。

## 8. Plan Freeze, Snapshot and Checkpoint

- 状态：`UNRESOLVED -> RESOLVING -> FROZEN`；任何 freeze 前失败/取消/预算/deadline 转为 `FAILED`。
- Snapshot Plan schema 从 1 升为 2，见 `core/runtime/snapshot_contract.py:37,197-289`。
- v2 覆盖：Plan identity/version/source、step ID、dependencies、preferred agent、真实 execution kind、output policy、capabilities 和既有安全 TextSummary。
- 禁止：raw instruction、instruction digest、Binding、Planner raw response、query/path、StepResult、runtime execution order。
- fingerprint schema v2 见 `core/runtime/plan_fingerprint.py:26-34`；agent/execution/output/dependency/capability/schema 改变均改变 fingerprint。
- v1 payload 缺少 output policy 时仍可读取并用 legacy schema 重建 fingerprint，见 `snapshot_contract.py:361-387`、`plan_fingerprint.py:39-55`、`recovery_validation.py:228-229`；未知 Plan schema 和非法执行枚举 fail closed。
- Dynamic checkpoint 类型定义/验证见 `checkpoint_contract.py:36`、`checkpoint.py:474-484`。它在 PLAN_CREATED 后、Step 前持久化，不包含 Binding。
- Recovery 可验证 `POST_PLAN_PRE_EXECUTION` 的 v2/quiescent/RUNNING/no-step-started 事实，但因为 Bindings 故意不持久化，当前返回 `UNSUPPORTED`，见 `recovery_validation.py:620-631`。v1 不会被误认为具备 v2 dynamic multi-agent resume 能力。

## 9. Default API Integration

- `core/chat_service.py:318` 继续调用 Factory 默认 `create_run_scope`；Factory 默认方法现在只走 dynamic Resolver。
- selected `core_router` 的 greeting/deterministic 或 model direct、selected knowledge/code/data entry 全部进入同一 lifecycle。
- 显式 specialist 由 Registry deterministic direct：Planner model call 为 0，但仍有 PLANNING_STARTED、PLAN_CREATED、Compiler、freeze、checkpoint 和相同预算/取消/deadline。
- unknown selected Agent、schema、Compiler 或 model failure均无 static/Core fallback。
- ChatService 的外部请求/响应形状未改变；显式 Legacy runtime selector 不创建 Coordinated scope，相关 E2E 通过。

## 10. Single-Step Execution Compatibility

`ResolvedSingleStepDriver` 位于 `core/runtime/runtime_factory.py:85-123`：

1. 要求 frozen Plan 恰好一个 Step。
2. 用 claim 的 `step_id + preferred_agent` 调用 `StepInvocationBindings.resolve_for_step`。
3. 将 Binding instruction 交给现有 `AgentRouter.complete_single_agent`。
4. 不按 Agent ID 写 if/elif，不创建 AdapterFactory/Store/OutputGate，不支持 synthesis 或多 Step。
5. 保留现有 OUTPUT_DELTA 行为。

测试证明：Core direct 使用原始 PlanningRequest；显式 knowledge/code/data 调用真实 preferred_agent 且 Planning model 为 0；“调用知识专家，总结 cdt_field_mapping.md” deterministic 编译为唯一 knowledge Step，并把原请求作为 Binding instruction 执行。

## 11. Multi-Step Admission Gate

临时 gate 位于 `core/runtime/run_coordinator.py:500-509`，并有明确 WP3 removal marker。条件：

```text
step_count > 1
OR any execution_kind == SYNTHESIS
OR any output_policy == INTERNAL
```

命中后 Plan 仍是合法、已冻结、已发布 PLAN_CREATED 且已捕获 post-plan checkpoint；随后直接以 `UNHANDLED_ERROR / MULTI_AGENT_EXECUTION_NOT_READY` 失败。不会调用 specialist/Core，不会产生 STEP_STARTED 或 OUTPUT_DELTA，Bindings 仍清理。WP3 接入真实 MultiAgentDriver 时应删除此 gate，不得把它长期化。

## 12. Security Boundary

- Planning event/metrics/Trace 不含 query、prompt、raw response、instruction、Binding 或 path。
- unknown selected agent 在 Registry 校验前不会原样写入 PLANNING_STARTED。
- Planner parser/Compiler/Registry 异常只暴露 fixed safe code/message；模型伪造 raw text 不进入 ERROR/Journal。
- Plan/Snapshot/fingerprint 不保存 Binding 或 instruction/digest。
- Dynamic Bindings 在成功、planning 后执行失败、multi-step gate、取消、deadline、budget、producer exception 和 terminal publication cleanup 路径统一 `close_and_clear()`；随后 Coordinator 清空引用。
- static path 没有 Binding，不执行伪清理。
- 默认日志未增加 user query/path 字段；Planning control event 不污染用户正文。

## 13. Tests and Commands

### 最终验证

| 命令 | 结果 |
|---|---|
| `uv run pytest -q tests/test_agent_registry.py tests/test_invocation_bindings.py tests/test_plan_compiler.py tests/test_multi_agent_planning.py` | 68 passed |
| `uv run pytest -q tests/test_dynamic_planning_lifecycle.py tests/test_planning_model_adapter.py` | 指标补充前 19 passed；补充后 20 passed |
| 关键 Coordinator/Factory/Streaming/Event/E2E/Snapshot/fingerprint/Checkpoint/Recovery 组合 | 97 passed, 4 subtests passed |
| `uv run pytest -q tests/test_dynamic_planning_lifecycle.py tests/test_runtime_metrics.py tests/test_coordinated_runtime_factory.py` | 35 passed |
| `uv run pytest -q` | 1184 passed, 42 subtests passed |
| `uv run python -m compileall -q core tests server.py main.py` | PASS |
| `git diff --check` | PASS；仅有 Git LF/CRLF 提示，无 whitespace error |

### 实施中失败与修复

1. 首次定向命令引用不存在的 `tests/test_runtime_factory.py`，pytest 未收集；纠正为真实文件 `test_coordinated_runtime_factory.py`。这是命令名错误，不是产品失败。
2. 默认 Factory 切换 dynamic 后，4 个旧测试仍断言构造期已有 checkpoint/使用未知测试 Agent；改为验证构造期无 Plan-bound 组件，并让默认链使用 Registry 中的 `core_router`。
3. v1 Snapshot 兼容实现首版在构造 digest source 时错误地把 `PlanSnapshot` 本体替换为 dict，造成 22 个定向失败；修复为只对 canonical digest/serialization view 做 v1/v2 contract 投影，对象合同保持 typed。
4. 组合回归中真实 AgentRouter fake 第一次模型调用仍返回最终答案，导致 strict Planner 拒绝；修复 fake 依据 Planner system prompt 返回 strict JSON，第二次才返回答案，同时更新完整事件序列和模型调用次数。没有让 Runtime 绕过 Resolver。
5. 总 deadline 与 independent cap 同时接近时曾出现分类竞态；改为记录 effective timeout 的限制来源，稳定映射总 deadline。
6. 首轮全仓：`1172 passed, 42 subtests passed, 10 failed`；剩余均为未知旧测试 Agent 或执行级模型测试未使用 deterministic planning。改为 Registry 合法 Agent，模型执行专项使用 deterministic greeting，使测试仍覆盖原执行失败/重试语义。
7. 第二轮全仓：`1183 passed, 42 subtests passed`。之后补齐 Planning metrics，并再次执行最终全仓、compileall、diff check。

关键验收测试位置：dynamic lifecycle `tests/test_dynamic_planning_lifecycle.py:84`；显式 Agent `:173`；deterministic metrics `:194`；knowledge delegated `:218`；multi-step gate `:235`；failure/no raw leak `:268`；timeout/deadline/budget/cancel/cleanup `:298-421`；统一 adapter/prompt `tests/test_planning_model_adapter.py:13`；v1/v2 Snapshot `tests/test_snapshot_contract.py:226-266`；fingerprint execution/output/schema `tests/test_plan_fingerprint.py:146`。

## 14. Compatibility and Regression

- Legacy：显式 LEGACY selector 未创建 Coordinated scope，原输出路径通过。
- Static Coordinated：公开兼容构造仍接收 concrete Plan；static Factory 单 Step兼容路径无 Planning event。
- Streaming：Planning 是 control event；OUTPUT_DELTA 文本行为未变。
- Snapshot/Recovery：v2 新合同与 v1 读取均通过；动态未完成 Run 无 Bindings 时 fail closed。
- AgentRouter：真实 ModelInvocation retry/fallback、knowledge routing 和 model event integration 回归通过。
- Cancellation/Shutdown/partial publication：RunHandle 在 Planning 前已注册；terminal publication fault 不重复业务执行，cleanup 保持。
- 外部 `/api/chat` 请求/响应主要形状未修改。

## 15. Deviations from Consensus

No deviations from the Stage 2.5 architecture consensus were introduced in WP2.

多 Step admission gate 是 WP3 前的阶段性保护，按 WP2 合同实现，不是长期架构偏差。

## 16. Known Limitations After WP2

- 多 Agent尚未执行；多 Step暂时 fail closed。
- 没有 MultiAgentDriver 或 AgentAdapterFactory。
- 没有 StepResultStore、Completion Pipeline、Synthesis Runtime。
- 没有 OutputGate、DeliveryStatus 或 INTERNAL 结果通道。
- Snapshot/Recovery 不保存或恢复 Bindings/结果；POST checkpoint 当前只可验证、不可 resume。
- 同步 provider 不可被 Python 强杀；Planner task 取消/超时沿用既有 bounded executor 的 cancel-or-detach 与 application shutdown ownership。
- 不能宣称默认多 Agent已可用，不能宣称 Stage 2.5 完成。

## 17. WP2 Acceptance Evidence

| 验收项 | 主要证据 | 状态 |
|---|---|---|
| default API 进入 Resolver | Factory/E2E、dynamic lifecycle | PASS |
| static/dynamic 互斥 | Factory 构造与 static no-event 测试 | PASS |
| event 顺序与单 terminal | lifecycle/event integration | PASS |
| freeze once / freeze 前拒绝 | lifecycle internal-state tests | PASS |
| explicit Agent 零 Planner model | knowledge/code/data 参数化测试 | PASS |
| Planner model 统一调用 | adapter + AgentRouter prompt test、真实 Model events | PASS |
| schema/Registry/Compiler/model failure 无 fallback | failure matrix | PASS |
| cap/deadline/budget/cancel 分类 | lifecycle mapping tests | PASS |
| Snapshot/fingerprint v2 + v1 read | snapshot/fingerprint tests | PASS |
| dynamic post-plan checkpoint | lifecycle + Recovery unsupported test | PASS |
| 单 Step Binding/preferred Agent | Core/explicit/delegated tests | PASS |
| 多 Step无部分执行 | gate test：无 Step/Agent/output | PASS |
| Bindings 全终态清理 | close_and_clear spy + failure/gate paths | PASS |
| 安全字段 | event/stream/raw-output/snapshot tests | PASS |
| Legacy/static/E2E/full regression | regression groups + full suite | PASS |

## 18. WP3 Interface Needs

WP3 只应消费本轮已冻结接口：

1. `RunCoordinator.plan`：不可替换的 frozen Plan。
2. `RunCoordinator.invocation_bindings`：run-scoped，按 claim step + expected agent 读取。
3. `AgentRegistry.resolve(agent_id).execution_adapter_id`：Adapter Factory 的稳定符号输入。
4. `Scheduler` 产生的 `StepClaim`：唯一执行授权。
5. Coordinator/Scope 现有 terminal cleanup：WP3 必须保持 Bindings 和未来结果资源清理。
6. 删除点：`RunCoordinator._multi_step_execution_not_ready` 的 WP3 marker。

本轮没有实现这些接口的 WP3 消费者。

## 19. Final Status

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
Ready for GPT review: YES
Ready to start WP3: YES
```
