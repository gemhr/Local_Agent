# LocalAgent Stage 2.5 Multi-Agent WP4 Implementation Result

## 1. Executive Summary

WP4 已完成。默认 Dynamic Coordinated 路径（Shape 0～3）现已统一走 typed completion pipeline：

```text
typed StepResult
  -> Store PREPARED
  -> Step SUCCEEDED
  -> Store READABLE
  -> OutputGate at-most-once publish
  -> STEP_COMPLETED(SUCCEEDED)
  -> safe completion report
  -> Coordinator 先消费 delivery
  -> Run SUCCEEDED 或显式 delivery failure
```

- 用户可见多 Agent final output：已启用。Shape 2/3 的 synthesis final 正文恰好在唯一 `OUTPUT_DELTA` 事件中发布一次。
- Shape 0～3 状态：Core direct、explicit entry、delegated knowledge direct、single specialist + synthesis、fan-out specialists + synthesis 全部通过真实主链执行并有正式 final delivery。
- delivery failure 分层：Final Step 执行成功与用户交付结果已分离；`FINAL_OUTPUT_DELIVERY_FAILED` / `FINAL_OUTPUT_DELIVERY_UNKNOWN` 不再伪装成 Agent 失败，Final Step 保持 `SUCCEEDED`。
- Memory 边界：只有确认 `DELIVERED` 的 final 才由 Run 级 owner 写入现有 entry Agent 的 direct scope；specialist/Synthesis raw、FAILED/UNKNOWN final 均不写入。
- 测试总结：WP4 专项 26 passed；全仓 `1299 passed, 42 subtests passed`；`compileall` 与 `git diff --check` 通过。

## 2. Source Audit Before Changes

实施前对真实源码的审计（变更前事实）。

### 2.1 旧最终输出路径

- `ResolvedSingleStepDriver`（`core/runtime/runtime_factory.py`）用于 dynamic 单 Step，按 claim 读取 Binding 后调用 `router.complete_single_agent(..., persist=persist)`，返回值类型为 `str`。
- `ParallelExecutor.worker` 在 `driver.emits_user_output and isinstance(result, str)` 时自动发布 `OUTPUT_DELTA`，随后 `_terminal` 提交 Step 状态并发布 `STEP_COMPLETED`。
- 字符串结果发布 `OUTPUT_DELTA` 的时机在 Step 变为 `SUCCEEDED` 之前；`STEP_COMPLETED` 由 worker 在状态提交后发布。
- 单 Step AgentRouter 在 `persist=True` 时于调用前写 user message、调用后写 assistant message（`core/agent_router.py` `_run_agent_once`）；ChatService 没有独立的 final Memory owner。
- 变更前 `OUTPUT_DELTA` 一次发布完整字符串（无 chunk 循环）。

### 2.2 WP3 typed 路径

- `StepResultCommitter`（`core/runtime/step_completion.py`）实现 result/state 分支：PREPARED -> Step SUCCEEDED -> READABLE -> `STEP_COMPLETED` -> `StepCompletionResult`；无 OutputGate/delivery。
- `ParallelExecutionReport.completion_results` 携带安全完成 metadata；`RunCoordinator._execute_batches` 在 executor task 返回后调用 `_decision_from_batch_report`。
- `final_result_ready` 由 committer 对唯一 final Step 设置；Coordinator 在 `_decision_from_snapshot` 中于所有 Step SUCCEEDED 且 final READABLE 时返回临时保护 `FINAL_OUTPUT_PIPELINE_NOT_READY`（含 WP4 REMOVAL MARKER）。
- Store 的 final entry 读取接口只有 `has_readable`；清理时机为 Run terminal cleanup：先 `seal` 再 `clear`。

### 2.3 EventChannel / Emitter

- `RuntimeEventChannel.publish` 是 journal-first：journal append 成功后即消费 run sequence，随后 enqueue；enqueue 失败抛出 `EventPublicationError(partially_persisted=True)`。
- run sequence 与 step sequence 分别生成：run sequence 由 Channel 持有，step sequence 由 `StepEventEmitter` 本地持有。
- 旧 `StepEventEmitter` 仅在 publish 正常返回时递增本地 step sequence；若 `OUTPUT_DELTA` 已 journaled 但 enqueue 失败，下一次 `STEP_COMPLETED` 会重用该 sequence。
- `STEP_COMPLETED` 发布时 `close=True` 关闭 StepEmitter；enqueue 失败后仍可发布后续事件（journal 保留、sequence 不重复）。
- terminal publication failure 沿既有合同：Coordinator 在状态已终态时尝试发布 terminal 事件，失败以 `RUNTIME_TERMINAL_PUBLICATION_FAILED` 暴露。

### 2.4 Memory

- 真实 owner：`AgentRouter._run_agent_once` 内 `memory_manager.add_message(agent_id, "user"/"assistant", ..., memory_scope="direct")`，受 `persist` 开关控制。
- Agent scope/session scope 真实键：`agent_id` + `memory_scope="direct"`（`DIRECT_MEMORY_SCOPE`）。
- 单 Agent direct（dynamic 或 static）当前 `persist=True` 时写 user + assistant 两条消息到 entry Agent 的 direct scope。
- 多 Agent final 应保存到 entry Agent（请求发起者）现有 direct scope，沿用既有会话约定，未发明新 scope。
- 现有 Memory 无幂等 message ID / write-once；`persist=True` 路径的写失败会使调用抛错并导致 Run 失败（best-effort 不存在）。
- Memory 写失败处理：沿用「写失败使 Run 失败」的产品合同，并以稳定错误码 `FINAL_OUTPUT_MEMORY_COMMIT_FAILED` 区分，不回滚 Step 成功、不重发正文。

### 2.5 Static Plan 风险

- `PlanStep.output_policy` 兼容默认 `FINAL_PASSTHROUGH` 属实。
- 测试/fixture 中存在未显式填写 policy 的多 Step static Plan。
- 结论：新 OutputGate 仅由 Coordinator 的 typed runtime 创建，而 typed runtime 只对 dynamic Plan 初始化；static 路径不创建 Gate，因此不受影响（策略 A，见第 4 节）。

## 3. Files Changed

| 文件 | WP4 职责 |
| --- | --- |
| `core/runtime/invocation_bindings.py` | 新增 `InvocationRole`（ENTRY/DELEGATED/SYNTHESIS）与 `history_policy_for_role`；`AgentInvocationSpec` 增加 role 字段及 history_policy 派生属性。 |
| `core/runtime/plan_compiler.py` | 按 typed decision 设置 role：direct=ENTRY、delegated passthrough=DELEGATED、specialists=DELEGATED、synthesis=SYNTHESIS；不按 step ID/Agent 名猜测。 |
| `core/runtime/agent_adapter_factory.py` | `AgentExecutionRequest` 增加 invocation_role/history_policy；`AgentRouterSingleAgentAdapter` 按 request.history_policy 调用 `complete_single_agent`，始终 `persist=False`。 |
| `core/runtime/multi_agent_driver.py` | 将 binding 的 role/history_policy 传入 `AgentExecutionRequest`。 |
| `core/runtime/output_gate.py` | 新增 Run 级 `OutputGate` 状态机（NOT_STARTED/PUBLISHING/PUBLISHED/FAILED/OUTCOME_UNKNOWN）、`DeliveryStatus`、授权校验、at-most-once 与安全 repr。 |
| `core/runtime/event_emitter.py` | `StepEventEmitter` 捕获 `EventPublicationError(partially_persisted=True)` 时消费本地 step sequence 后重抛，保证后续 `STEP_COMPLETED` 使用下一 sequence。 |
| `core/runtime/event_channel.py` | 发布故障的 `FaultMatchContext` 增加 `step_id`，支持按 Step 注入故障（供测试与运维定位）。 |
| `core/runtime/step_completion.py` | `StepResultCommitter` 演进为完整 `StepCompletionPipeline`：INTERNAL/FINAL 分支、OutputGate delivery、delivered-only Memory 写入、安全 `StepCompletionResult`（delivery 与 completion event 错误可区分）。 |
| `core/runtime/step_result_store.py` | 新增 `read_final_content(final_step_id)`：仅唯一 final、仅 READABLE、仅 OPEN 时的受限读取，供 Run 级 final Memory writer 使用。 |
| `core/runtime/final_memory_writer.py` | 新增 Run 级 `RunFinalMemoryWriter`：只有 `DELIVERED` 才写原始 user message 与唯一 final assistant message 到 entry Agent direct scope；write-once、失败重置。 |
| `core/runtime/run_coordinator.py` | dynamic Plan 全部走 typed runtime；创建 OutputGate 与 memory writer；`_decision_from_batch_report` 优先消费 delivery/内存错误；删除 `FINAL_OUTPUT_PIPELINE_NOT_READY`；暴露 `output_gate`。 |
| `core/runtime/runtime_factory.py` | 将 `persist` 传入 dynamic resolver，供 Run 级 final Memory owner 使用。 |
| `core/runtime/__init__.py` | 导出 WP4 稳定合同（OutputGate、DeliveryStatus、InvocationRole、RunFinalMemoryWriter 等）。 |
| `docs/runtime/runtime_error_code_catalog.md` | 补充 4 个 WP4 稳定错误码及其语义。 |
| 对应 tests/fixtures | 新增 5 个 WP4 专项测试文件；更新 dynamic lifecycle、multi-agent execution、WP3 history boundary、step-result security、diagnostic fault isolation 与共享 fixture。 |

## 4. Dynamic Output Unification

- 默认 Dynamic Coordinated（Shape 0～3）全部使用 typed pipeline：Driver 返回 `StepResult` -> StepCompletionPipeline -> OutputGate（仅 FINAL）。
- 关键证据：`RunCoordinator._is_typed_multi_step_plan`（`core/runtime/run_coordinator.py:416`）对 dynamic Plan 返回 `self._dynamic`；`_initialize_typed_runtime`（`run_coordinator.py:436`）创建 `StepResultStore`、`OutputGate`、`RunFinalMemoryWriter` 并注入 `StepResultCommitter`；`ParallelExecutor` typed mode 不再依据 `isinstance(result, str)` 发布用户文本。
- 不再允许默认 dynamic 单 Step 走 `driver returns str -> ParallelExecutor 自动 OUTPUT_DELTA`。
- 迁移形态：Core direct、explicit knowledge/code/data entry、delegated knowledge direct、single specialist + synthesis、fan-out specialists + synthesis。
- `ResolvedSingleStepDriver` 保留用于 Legacy、显式 static compatibility 与内部旧测试，但默认 dynamic 路径不再依赖它发布用户文本。
- Legacy：显式 LEGACY 模式不创建 Coordinated scope，保持既有输出与 Memory 行为，不发布 Planning 事件，不受 WP4 迁移影响。
- Static Coordinated：采用策略 A —— static compatibility 路径不使用新 Gate（`CoordinatedRuntimeFactory.create_static_run_scope` 创建 static coordinator 时 `_dynamic=False`，typed runtime 不初始化，`output_gate` 保持 `None`）；旧输出行为保持；static multi-step 不被宣称具有 WP4 delivery 语义。已实测 static scope `output_gate is None` 且 Run SUCCEEDED。
- 因新 Gate 实际受影响的 static fixtures：无（Gate 不作用于 static 路径）。

## 5. Invocation Role and History Policy

- 新增显式调用角色合同（`core/runtime/invocation_bindings.py:14`）：

```python
class InvocationRole(str, Enum):
    ENTRY = "ENTRY"
    DELEGATED = "DELEGATED"
    SYNTHESIS = "SYNTHESIS"
```

- Compiler 根据 typed decision 设置 role（`core/runtime/plan_compiler.py` `_compile_direct`/`_compile_delegated`）；信息存放在 run-scoped Binding metadata（`AgentInvocationSpec.role`），不包含 raw instruction，不按 step ID/Agent 名猜测，未扩大 Snapshot。
- 映射（`history_policy_for_role`，`invocation_bindings.py:30`）：

| 调用角色 | HistoryPolicy | persist |
| --- | --- | --- |
| Core direct | AGENT_SCOPE | False |
| 用户显式 entry specialist | AGENT_SCOPE | False |
| delegated specialist（含单 knowledge passthrough） | NONE | False |
| synthesis | NONE | False |

- 所有 Adapter 调用均 `persist=False`（`agent_adapter_factory.py` `AgentRouterSingleAgentAdapter.execute` 内 `persist=False, history_policy=request.history_policy`）；最终 Memory 由 Run-level final owner 在 `DELIVERED` 后统一提交。
- 专项测试覆盖：explicit entry 仍读取其直接对话历史；delegated single knowledge passthrough 不读取旧 knowledge history（`history_policy=NONE` 由 kwargs 断言）；Core direct 仍读取 Core 直接历史；所有路径交付前 `persist=False`。

## 6. OutputGate Contract

- 所有权：每个 Run 一个；由 Dynamic RunScope/Coordinator 拥有；仅 `StepCompletionPipeline` 调用；Driver/Adapter/Synthesis/Scheduler 无调用权限；不进 Snapshot/Checkpoint；不跨进程恢复；不保存到 Journal；清理后不可再次发布。
- 状态机（`core/runtime/output_gate.py:41`）：

```text
NOT_STARTED
-> PUBLISHING
-> PUBLISHED | FAILED | OUTCOME_UNKNOWN
```

- at-most-once 逐项说明：
  - 只有 `NOT_STARTED` 可开始 publish；`attempt_publish`（`output_gate.py:263`）在锁内先校验状态，`PUBLISHING` 或任意终态均 fail closed。
  - `PUBLISHING` 中到达的第二次 attempt 直接返回 `OUTPUT_GATE_DUPLICATE_ATTEMPT`（不等待、不重试）。
  - `PUBLISHED`、`FAILED`、`OUTCOME_UNKNOWN` 均禁止第二次 attempt；`FAILED` 同样禁止重试（attempt 终态不可逆）。
  - concurrent duplicate：两协程并发时只有一个能进入 `PUBLISHING`，另一个立即得到 duplicate 结果；测试 `test_concurrent_duplicate_attempts_allow_only_one_publish` 断言仅 1 个 `OUTPUT_DELTA`。
  - duplicate 稳定 error code：`OUTPUT_GATE_DUPLICATE_ATTEMPT`。
  - Driver、Adapter、Scheduler 无法调用 Gate：Gate 仅由 Coordinator 在 typed runtime 中创建并注入 `StepResultCommitter`，Driver/Adapter/Scheduler 的构造与调用路径中不存在 Gate 引用；`attempt_publish` 需要的 claim/store/state 校验也只出现在 completion pipeline 调用栈中。
  - Gate 不进 Snapshot/Recovery：Gate 是纯内存 Run 级对象，未出现在 `snapshot_contract`、checkpoint 或 journal 的任何字段中。
- 授权校验（`_authorize_locked`，`output_gate.py:192`）：Step 已由 Scheduler claim 且已提交 `SUCCEEDED`；Step 在冻结 Plan 中；Store entry `READABLE`；`output_policy` 为 `FINAL_PASSTHROUGH` 或 `FINAL_SYNTHESIS`；Step 是 Plan 唯一 final source；Gate 尚未尝试；Store 未 seal；Run 仍处于允许完成的 active 状态。INTERNAL Step 调用 Gate 被拒绝（`OUTPUT_GATE_INTERNAL_STEP`）。
- 发布内容（`_publish_output`，`output_gate.py:373`）：只发布 `OUTPUT_DELTA`，payload 为 final StepResult.content；一次发布完整 final candidate；不 token/chunk 循环；不拼接多个 specialist 结果；不加内部 step/agent metadata；不修改 final 文本；不调用模型；不写 Memory；不返回 raw 内容到 report。
- `DELIVERED` 准确含义：Runtime 的 EventChannel publish 正常返回，正文事件已进入当前交付通道；不代表前端已展示，也不代表最终用户确认阅读。

## 7. DeliveryStatus Contract

```python
class DeliveryStatus(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
```

- 分类依据（源码级，`output_gate.py:308-361` 结合 `event_channel.py:279` publish 的 journal-first 顺序）：
  - `EventPublicationError.partially_persisted=False`（journal append 前失败）-> `FAILED` / `FINAL_OUTPUT_DELIVERY_FAILED`；
  - `EventPublicationError.partially_persisted=True`（journal append 后 enqueue 失败）-> `OUTCOME_UNKNOWN` / `FINAL_OUTPUT_DELIVERY_UNKNOWN`；
  - publish 正常返回 -> `DELIVERED`；
  - INTERNAL Step 永不调用 Gate -> `NOT_APPLICABLE`。
- 不得把 unknown 当作 failed 后重试。

## 8. StepCompletionPipeline

- `StepResultCommitter`（`core/runtime/step_completion.py:121`）已演进为唯一 Step 完成 owner（完整 `StepCompletionPipeline`），未保留第二个相互竞争的完成 owner。

INTERNAL Step：

```text
Driver result
-> completion guard
-> validate
-> Store PREPARED
-> Step RUNNING -> SUCCEEDED
-> Store READABLE
-> STEP_COMPLETED(SUCCEEDED)
-> StepCompletionResult(delivery=NOT_APPLICABLE)
```

FINAL Step（`commit`，`step_completion.py:193`）：

```text
Driver result
-> completion guard
-> validate
-> Store PREPARED
-> Step RUNNING -> SUCCEEDED
-> Store READABLE
-> OutputGate.attempt_publish
   -> DELIVERED / FAILED / OUTCOME_UNKNOWN
-> [DELIVERED only] Run-level final Memory writer
-> STEP_COMPLETED(SUCCEEDED)
-> safe StepCompletionResult
```

- 保持：内部 Step 状态在 Gate 前已 `SUCCEEDED`；外部事件仍是 `OUTPUT_DELTA < STEP_COMPLETED`；Gate 失败不得将 Step 改为 FAILED；不得映射为 `AGENT_STEP_FAILED`；不得重跑 Agent/Synthesis；不得重新发布正文。
- 安全报告 `StepCompletionResult`（`step_completion.py:97`）至少表达：`step_id`、`commit_status`、`final_result_ready`、`output_policy`、`delivery_status`、`delivery_error_code`、`event_emitted`、`completion_error_code`；不含 raw result、不含 output 正文、不含 Binding、不进 Snapshot/Checkpoint、可进 transient batch report、repr 安全。
- delivery 错误（`FINAL_OUTPUT_DELIVERY_FAILED`/`FINAL_OUTPUT_DELIVERY_UNKNOWN`/`OUTPUT_GATE_DUPLICATE_ATTEMPT`/`FINAL_OUTPUT_MEMORY_COMMIT_FAILED`）与 `STEP_COMPLETED` 事件错误（`STEP_COMPLETION_EVENT_FAILED`）可区分，不使用单一模糊 error string 覆盖。

## 9. Partial Publication Sequence

- EventChannel 保持 journal-first：`journal append -> channel enqueue`（`event_channel.py:279` publish 内 `self._journal.append(event)` 后 `self._sequence = sequence`，再 enqueue）。
- `StepEventEmitter.emit`（`event_emitter.py:140`）修复：

```text
try publish OUTPUT_DELTA
except EventPublicationError as exc:
    if exc.partially_persisted:
        consume/increment local step sequence
    re-raise or return classified outcome
```

- 真实故障注入序列（`tests/test_partial_publication_sequence.py`，EVENT_BEFORE_CHANNEL_ENQUEUE）：

```text
STEP_STARTED(synthesis) sequence=1
OUTPUT_DELTA(synthesis) sequence=2 已写 Journal
enqueue 失败 -> partially_persisted=True
STEP_COMPLETED(synthesis) sequence=3
ERROR(FINAL_OUTPUT_DELIVERY_UNKNOWN)
RUN_COMPLETED(FAILED)
```

- StepEmitter 在何处消费 sequence：`event_emitter.py:185-192`，仅当 `exc.partially_persisted` 时执行 `self._sequence = step_sequence`（在锁内，且随后重抛）。
- 未持久化失败为什么不消费：`partially_persisted=False` 时不修改本地 sequence，下一次事件继续使用同一 step_sequence（该序号未写入 Journal，无冲突）。对应测试：pre-journal 失败后 `STEP_COMPLETED` 使用 sequence=2（STEP_STARTED=1 后的下一可用序号），Journal 无 OUTPUT 记录。
- 如何防止重复消费：消费发生在 StepEmitter 的 `asyncio.Lock` 内，且只在该次 publish 的异常路径执行一次；正常路径在 `self._sequence = step_sequence` 处递增，两条路径互斥。
- Journal 中 sequence 唯一、单调：测试断言 synthesis 的 `step_sequence` 列表 `sorted == 原序` 且无重复；run sequence 由 Channel 在 journal-first 下保持唯一单调。
- 为什么 unknown 不重试：正文可能已 journaled/部分提交，无法证明消费者未收到；重试会重复发布用户可见文本，违反 at-most-once publish attempt。

## 10. Coordinator Delivery Decision

- `_execute_batches` 在 `await executor_task` 后、下一次 Scheduler `is_complete` 成功判断之前调用 `_decision_from_batch_report`（`run_coordinator.py:1018`），检查顺序：result/state commit failure -> completion event failure -> delivery status（report 中 completion.error_code 的稳定顺序即此）。
- 映射：

| delivery | Final Step | RunStatus | StopReason | error_code |
| --- | --- | --- | --- | --- |
| DELIVERED | SUCCEEDED | 正常继续并最终 SUCCEEDED | COMPLETED | null |
| FAILED | SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_DELIVERY_FAILED |
| OUTCOME_UNKNOWN | SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_DELIVERY_UNKNOWN |
| duplicate attempt | 按实际 | FAILED | UNHANDLED_ERROR | OUTPUT_GATE_DUPLICATE_ATTEMPT |
| Memory commit failed | SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_MEMORY_COMMIT_FAILED |

- delivery failure 优先于 Scheduler 全部 Step 成功判断：batch report 在每批 executor 返回后即被消费；只有 report 无错误时循环才进入下一次 Scheduler 评估。因此即使所有 Step 已 `SUCCEEDED`，只要 final delivery 为 FAILED/UNKNOWN（或 Memory 提交失败），Run 也不会误报 success。
- 不产生 `RUN_COMPLETED(SUCCEEDED)`；Step 仍发布 `STEP_COMPLETED(SUCCEEDED)`；Coordinator 发布 safe `ERROR`；Run terminal 仍由 Coordinator 唯一发布；一个 Run 只有一个 terminal outcome。
- 若 `STEP_COMPLETED` publication 也失败：不重试 OUTPUT；Run 必须失败；error mapping 使用稳定 `STEP_COMPLETION_EVENT_FAILED`；safe report 仍保留 delivery 状态用于诊断；不允许第二次 Gate 调用。

## 11. Event Sequences

- Multi-Agent 成功：

```text
RUN_STARTED
PLANNING_STARTED
PLAN_CREATED
STEP_STARTED(specialists...)
STEP_COMPLETED(specialists, SUCCEEDED)...
STEP_STARTED(synthesis)
OUTPUT_DELTA
STEP_COMPLETED(synthesis, SUCCEEDED)
RUN_COMPLETED(SUCCEEDED)
```

- Dynamic Core/entry direct 成功：

```text
RUN_STARTED
PLANNING_STARTED
PLAN_CREATED
STEP_STARTED
OUTPUT_DELTA
STEP_COMPLETED(SUCCEEDED)
RUN_COMPLETED(SUCCEEDED)
```

- Known delivery failure：

```text
... final Driver success
[StepState already SUCCEEDED, Store READABLE]
OUTPUT_DELTA attempt fails before journal append
STEP_COMPLETED(SUCCEEDED)
ERROR(FINAL_OUTPUT_DELIVERY_FAILED)
RUN_COMPLETED(FAILED)
```

- Outcome unknown：

```text
... final Driver success
OUTPUT_DELTA journaled
enqueue fails
StepEmitter consumes used sequence
STEP_COMPLETED(SUCCEEDED) using next sequence
ERROR(FINAL_OUTPUT_DELIVERY_UNKNOWN)
RUN_COMPLETED(FAILED)
```

- Duplicate completion / retry：first Gate attempt -> terminal Gate state；duplicate callback/attempt -> rejected；无第二个 `OUTPUT_DELTA`；Run 失败或保留既有失败。

## 12. Final Memory Boundary

- 只有确认 `DELIVERED` 的 final output 可以写入 Memory；INTERNAL、FAILED、OUTCOME_UNKNOWN 不得写入。
- Dynamic Adapter 为什么全部 `persist=False`：最终 Memory 必须由 Run-level final owner 在确认 delivered 后统一提交；Adapter 调用发生在交付确认前，若以 `persist=True` 写入会违反「delivered 前不写 final Memory」。
- `RunFinalMemoryWriter`（`core/runtime/final_memory_writer.py:19`）真实字段：`_router`、`_entry_agent_id`、`_user_request`、`_persist`、`_write_lock`、`_written`。write-once 机制：`write_delivered`（`:55`）在锁内检查 `_written`，已写则抛错拒绝重复写入；写失败在 `except BaseException` 中重置 `_written=False`。
- user message 是否此前已由其他 owner 写入：否。WP4 之后 Dynamic Adapter 全部 `persist=False`，`AgentRouter._run_agent_once` 的 user/assistant 写入均被关闭；user message 只在 `write_delivered` 内由 RunFinalMemoryWriter 写入一次。
- 为什么不会重复写 user message：completion guard（`_completed_steps`）保证每个 Step 只 commit 一次；writer 的 `_written` write-once 保证一个 Run 最多写一次；duplicate completion 返回 `STEP_RESULT_DUPLICATE_COMMIT`，不会再次进入 DELIVERED 写入路径。
- assistant final 何时写入：Gate 返回 `DELIVERED` 后、`STEP_COMPLETED` 发布前，由 completion pipeline 调用 `write_delivered`。
- `DELIVERED` 写入；`FAILED` 不写入；`OUTCOME_UNKNOWN` 不写入（对应 `tests/test_final_memory_boundary.py` 参数化故障注入测试）。
- Memory 写失败时：Gate 保持 `PUBLISHED`；Step 保持 `SUCCEEDED`（不回滚）；Run 以 `FINAL_OUTPUT_MEMORY_COMMIT_FAILED` 失败并记录「已交付、Memory 提交失败」的分层事实；正文已 delivered 时不重发。
- scope 与 agent（真实既有约定，未发明新 scope）：
  - Core direct：`core_router` 的 `direct` scope（`planning_request.selected_agent_id`）。
  - explicit entry specialist：所选 entry Agent（如 `code_expert`）的 `direct` scope。
  - delegated knowledge direct：发起请求的 entry Agent（默认 `core_router`）的 `direct` scope；`knowledge_expert` 不写入。
  - multi-agent default：发起请求的 entry Agent（默认 `core_router`）的 `direct` scope；specialist/synthesis 均不写入。
- Legacy/static 行为不变：Legacy 不创建 Coordinated scope，沿用 AgentLoop 原路径；static 路径仍由 `CoordinatedSingleAgentDriver` 以既有 `persist` 参数调用 router，不经过 `RunFinalMemoryWriter`。

## 13. Shape 0～3 E2E Evidence

逐种图（`tests/test_final_output_delivery.py`，真实主链；真实 AgentRouter/MemoryManager 或录制型 router）：

| Shape | Planner/Compiler 形态 | Agent 调用 | Synthesis 调用 | INTERNAL OUTPUT | final OUTPUT_DELTA | final 文本 | Final Step | Run | Memory 写入 | 测试 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 Core direct | DIRECT_ANSWER -> 单 Step `answer` FINAL_PASSTHROUGH | core_router x1 | 0 | 0 | 1 | result-core_router（digest 校验） | SUCCEEDED | SUCCEEDED | core_router direct: user+assistant 各 1 | `test_shape0_core_direct_uses_typed_pipeline_and_single_output` |
| 1 explicit entry | EXPLICIT_ENTRY -> 单 Step `answer` FINAL_PASSTHROUGH | code_expert x1 | 0 | 0 | 1 | result-code_expert（digest 校验） | SUCCEEDED | SUCCEEDED | code_expert direct: user+assistant 各 1 | `test_shape1_explicit_entry_specialist_single_output` |
| 1 delegated knowledge direct | DELEGATE(1 task, synthesis=False) -> `task-knowledge` FINAL_PASSTHROUGH | knowledge_expert x1（history_policy=NONE） | 0 | 0 | 1 | result-knowledge_expert（digest 校验） | SUCCEEDED | SUCCEEDED | core_router direct: user+assistant 各 1（knowledge_expert 0） | `test_shape1_delegated_knowledge_direct_single_output` |
| 2 single specialist + synthesis | DELEGATE(1 task, synthesis=True) -> `task-code` INTERNAL + `synthesis` FINAL_SYNTHESIS | code_expert x1 | synthesis_agent x1 | 0 | 1 | SHAPE2_FINAL_CANDIDATE | SUCCEEDED | SUCCEEDED | entry direct（persist 默认） | `test_shape2_single_specialist_plus_synthesis_single_output` |
| 3 fan-out + synthesis | DELEGATE(2 tasks, synthesis=True) -> 2 INTERNAL + `synthesis` FINAL_SYNTHESIS | code_expert x1 + knowledge_expert x1 | synthesis_agent x1 | 0 | 1 | SHAPE3_FINAL_CANDIDATE | SUCCEEDED | SUCCEEDED | entry direct（persist 默认） | `test_shape3_fanout_specialists_plus_synthesis_single_output` |

- 断言：final 正文恰好一个 `OUTPUT_DELTA`；specialist INTERNAL 正文为零；Synthesis 调用次数正确；最终 Run SUCCEEDED；无 `FINAL_OUTPUT_PIPELINE_NOT_READY`；final 文本与 candidate 一致（journal digest 校验）；无 Core fallback。

## 14. Security Boundary

- 安全标记：`SECRET_FINAL_CANDIDATE`、`SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST`、`\internal\private\final.txt`。
- 断言（`tests/test_step_result_security.py`、`tests/test_final_memory_boundary.py`、`tests/test_partial_publication_sequence.py`）：
  - final candidate 只出现在唯一 OUTPUT 事件（journal 仅存 `text_digest`）与允许的 delivered Memory；
  - internal/specialist raw 不进入用户输出或 Memory；
  - failed/unknown final 不进入 Memory；
  - report/repr/error/Trace/Snapshot/Journal 不含正文；
  - Gate 状态与安全 error 可观测但无正文。

## 15. Tests and Commands

### WP4 专项

| 命令 | 结果 |
| --- | --- |
| `uv run pytest -q tests/test_output_gate.py` | 10 passed |
| `uv run pytest -q tests/test_partial_publication_sequence.py` | 2 passed |
| `uv run pytest -q tests/test_step_completion_delivery.py` | 5 passed |
| `uv run pytest -q tests/test_final_output_delivery.py` | 5 passed |
| `uv run pytest -q tests/test_final_memory_boundary.py` | 4 passed |
| WP4 专项合计 | 26 passed |

### 回归与全仓

| 命令 | 结果 |
| --- | --- |
| WP1-WP3 关键回归（dynamic lifecycle、multi-agent execution、WP3 history boundary、step-result security、coordinated factory、event 等） | 全部通过 |
| `uv run pytest -q` | 1299 passed, 42 subtests passed |
| `uv run python -m compileall -q core tests server.py main.py` | PASS |
| `git diff --check` | PASS（仅 Git LF/CRLF 提示，无 whitespace error） |

### 实施中发现并修复的问题

1. 旧测试 `FakeRouter` 无 `memory_manager`，导致默认 `persist=True` 时 delivered-only Memory 写失败；为共享 fixture 增加内存版 memory 桩（真实 Memory 合同测试仍使用 SQLite `MemoryManager`）。
2. Gate 的 claim 校验曾要求 Step 在 `active_step_ids`，但 Step 提交 `SUCCEEDED` 时已移出 active 集合；修正为校验 Step 状态 `SUCCEEDED`（claim 在执行期持有，提交后释放）。
3. `EventChannel` 发布故障的匹配上下文缺少 `step_id`，导致按 Step 的故障注入永不命中；为 `FaultMatchContext` 补充 `step_id`。
4. Store `seal` 后 `has_readable` 返回 False，Gate 的 sealed 校验顺序调整为先校验 sealed。
5. 测试断言序列时 `types.index(STEP_COMPLETED)` 命中首个（specialist）完成事件；改为取最后一个 `STEP_COMPLETED` 校验 `OUTPUT_DELTA < STEP_COMPLETED`。

## 16. Compatibility and Regression

- WP1：Registry/Bindings/Compiler/planning 测试通过。
- WP2：dynamic lifecycle、planning adapter、fingerprint/snapshot/recovery、coordinator/event/metrics 通过。
- WP3：typed driver/store/completion/synthesis/security/history boundary 通过（相关断言已按 WP4 交付语义前移：多 Step Run 成功并发布唯一 final）。
- Legacy：显式 LEGACY 路径保持既有输出与 Memory 行为，测试通过。
- static：策略 A —— static Coordinated 不创建 OutputGate，`output_gate is None`，旧输出行为保持，测试通过。
- Snapshot/Recovery：Gate/Store/Bindings 不进 Snapshot，Recovery 合同未变，相关测试通过。
- Streaming/Event：外部事件保持 `OUTPUT_DELTA < STEP_COMPLETED`，stream adapter 测试通过。
- Cancellation/Shutdown：Gate 不重开、unknown 不重试，相关测试通过。

## 17. Deviations from Consensus

No deviations from the Stage 2.5 architecture consensus were introduced in WP4.

实现层面说明（不改变架构语义）：
1. `StepResultCommitter` 保留类名并原位演进为完整 `StepCompletionPipeline`，避免同时存在两个竞争完成 owner，也避免破坏 WP3 测试契约。
2. 为 Run 级 delivered-only Memory 写入新增 `RunFinalMemoryWriter`，这是共识要求的必要最小 owner；scope 沿用 entry Agent 现有 `direct` 会话约定，未发明新 scope。
3. `StepResultStore.read_final_content` 是唯一受限 final 读取逃生口，仅供 Memory writer 在 DELIVERED 后使用。

## 18. Known Limitations After WP4

- 用户可见多 Agent final 已启用。
- 只保证 at-most-once publish attempt，不承诺 exactly-once delivery。
- `DELIVERED` 不是用户确认阅读。
- unknown 不重试。
- OutputGate 不恢复（不持久化、不跨进程）。
- Store/Bindings 不恢复。
- 前端完整状态展示尚未完成。
- WP5 完整 Memory/Journal/Trace 安全审计尚未完成。
- Planning executor 饥饿 P2 仍存在（Planning 与 specialist 共享 bounded executor，`PLANNING_MODEL` 无保底容量），按 WP3 边界保持记录，不在本工作包扩建调度系统。
- Stage 2.5 尚未完成。

## 19. WP5 Interface Needs

- final delivery 状态（`DeliveryStatus`、Gate 终态、safe report 中的 delivery_error_code）。
- delivered Memory 事实（已交付 final 的 scope 与一次写入证据）。
- Planning/Step/Run events（既有事件序列，含唯一 `OUTPUT_DELTA`）。
- Trace/Journal 安全元数据（text_digest/text_length、sequence 单调性）。
- frontend 状态需求（Step/Run/terminal 展示、delivery 分层展示）。
- 安全审计点（Memory/Journal/Trace 全文边界、partial publication 的可观测性、Gate 不可恢复语义）。

不得开始 WP5。

## 20. Final Status

```text
WP4 status: PASS
Architecture deviations: 0
P0 findings: 0
P1 findings: 0
P2 findings: 1
Dynamic single-step uses typed completion pipeline: YES
OutputGate enabled: YES
At-most-once publish attempt enforced: YES
User-visible multi-agent final output enabled: YES
Internal specialist output hidden: YES
Known delivery failure mapped correctly: YES
Unknown delivery mapped correctly: YES
Final Step remains SUCCEEDED on delivery failure: YES
Partial-persisted step sequence fixed: YES
Only delivered final persisted to Memory: YES
WP3 temporary final-output gate removed: YES
Ready for GPT review: YES
Ready to start WP5: YES
```
