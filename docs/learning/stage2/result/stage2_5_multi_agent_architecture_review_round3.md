# Stage 2.5 Multi-Agent Architecture Review — Round 3

> 状态：最后一轮源码核验与合同收敛。本轮未修改生产代码。

## 1. Executive Summary

Round 3 接受 GPT 的两个实质修正：

1. **执行成功与输出交付失败分层。** Agent 已生成并通过结果校验后，Final Step 应提交 `SUCCEEDED`；OutputGate 失败只使 Run 以 `FINAL_OUTPUT_DELIVERY_FAILED/UNKNOWN` 失败，不得把 Step 改为 FAILED，也不得归类为 `AGENT_STEP_FAILED`。
2. **raw instruction digest 不进入持久化 fingerprint。** Scheduler、Checkpoint、Recovery、Synthesis 和 Store ACL 都不需要它；动态 Run 又不恢复 Bindings/StepResult。Plan fingerprint 只覆盖可恢复的安全执行合同。

事件层保留现有兼容顺序 `OUTPUT_DELTA < STEP_COMPLETED`，但内部顺序改为 `Store PREPARED -> Step SUCCEEDED -> Store READABLE -> output delivery -> STEP_COMPLETED -> report -> Run terminal`。这不会混淆职责：事件顺序只是传输兼容，不是状态提交顺序。

源码确认没有新的 P0/P1：

- AgentStateMachine 允许所有 Step 都 SUCCEEDED、Run 仍因基础设施/交付错误从 RUNNING 进入 FAILED；Run 校验只要求没有 active Step（`core/runtime/state.py:333-344`; `core/runtime/state_machine.py:263-293`）。
- 当前 Coordinator 的确会在下一轮 Scheduler snapshot 看到全部成功后直接返回 SUCCEEDED（`core/runtime/run_coordinator.py:399-456`），但最小修复只是保留并优先检查当前被丢弃的 `ParallelExecutionReport`（`435-440`），无需改 Scheduler。
- Event adapter 逐事件处理，不要求 OUTPUT/STEP_COMPLETED 相对顺序（`core/runtime/stream_adapter.py:166-214`）；但 StepEmitter 在完成事件后关闭（`core/runtime/event_emitter.py:155-186`），真实 E2E/事件测试明确断言 OUTPUT 在 STEP_COMPLETED 前（`tests/test_runtime_event_integration.py:71-103,593-604`）。保留现有顺序回归风险最低。
- 普通 instruction digest 对现有 Scheduler binding、PlanSnapshot fingerprint 和 Recovery 没有必要用途（`core/runtime/scheduler.py:320-334`; `core/runtime/plan_fingerprint.py:16-32`; `core/runtime/recovery_validation.py:689-724`）。

因此：**Remaining P0=0，Remaining P1=0，可以在同一轮生成最终 Consensus 文档。** 这只表示设计达成共识，不表示获得实施授权。

## 2. Response to Final GPT Decisions

| 议题 | Codex 回应 | 最终决定 | 源码依据 |
|---|---|---|---|
| execution/delivery 分层 | **ACCEPT** | Final Step 先 SUCCEEDED；Gate failure 只影响 Run | `state.py:333-344`; `state_machine.py:206-227,263-293` |
| instruction digest 移出持久化 fingerprint | **ACCEPT** | Plan/PlanSnapshot 不含 raw instruction digest；Bindings 仅内存校验 | `scheduler.py:320-334`; `plan_fingerprint.py:16-32`; `recovery_validation.py:689-724` |
| event/internal state 顺序分离 | **ACCEPT WITH COMPATIBILITY CHOICE** | 内部先 Step success，外部仍 OUTPUT_DELTA 后 STEP_COMPLETED | `parallel_execution.py:223-236`; `event_emitter.py:155-186`; `test_runtime_event_integration.py:71-103` |
| StopReason | **ACCEPT** | 只新增 PLANNING_FAILED；交付失败/未知使用 UNHANDLED_ERROR + 专用 error_code | `state.py:51-63`; `run_coordinator.py:459-467,573-580` |
| Entry/Delegated Registry | **ACCEPT** | 区分 entry/delegated policy；单 delegated knowledge 且无其他 Step 才允许 passthrough | 当前 `agent_router.py:191-213` 只有不完整 map，需 typed Registry |
| 默认 API Resolver | **ACCEPT** | 默认 API 全部进 Resolver；显式 specialist 是 deterministic、零 Planner model call | `server.py:603-609,784-794`; `runtime_factory.py:254-307` |

knowledge delegated passthrough 的规则由 Compiler 固定表达，不由模型猜测：仅当 typed decision 只有一个 `knowledge_expert` task、无依赖、无其他 Step，且 Registry 的 delegated policy 允许 `FINAL_PASSTHROUGH` 时编译为形态 1；其他 delegated knowledge 一律 INTERNAL，并由唯一 synthesis 收口。

## 3. Current Completion/Event Ordering

当前真实顺序：

```text
Scheduler.claim_ready
  -> AgentState PENDING -> RUNNING
ParallelExecutor worker
  -> STEP_STARTED
  -> Driver invocation
       -> MODEL/TOOL/RETRIEVAL events
  -> OUTPUT_DELTA                   # 字符串 + emits_user_output
  -> AgentState RUNNING -> SUCCEEDED
  -> STEP_COMPLETED
  -> StepExecutionOutcome
ParallelExecutionReport
RunCoordinator discards report
  -> next Scheduler.evaluate
  -> all steps SUCCEEDED => success decision
  -> Run AgentState -> SUCCEEDED
  -> RUN_COMPLETED(SUCCEEDED)
```

证据：

- claim 在 `core/runtime/parallel_execution.py:140-145` 完成；worker 在 `202` 发布 STEP_STARTED。
- Driver 在 `205-218` 执行。
- 当前 OUTPUT_DELTA 在 `223-232`，Step state terminal 在 `233`，STEP_COMPLETED 在 `234-236`。
- `_terminal` 在 `382-389` 通过 AgentStateMachine 原子提交 Step 状态。
- report 在 `290-292` 返回，但 Coordinator 在 `run_coordinator.py:435-440` 只 await，不保存。
- 下一轮 `scheduler.evaluate` 后，`snapshot.is_complete` 直接生成成功决策（`run_coordinator.py:406-456`; `scheduler.py:419-422`）。
- Run 状态先在 `_finalize_once` 提交，再发布 terminal event（`run_coordinator.py:760-803`）。

## 4. Final Step Completion Contract

### 4.1 INTERNAL Step

```text
Driver returns StepResult
  -> completion lock + RUNNING guard
  -> validate producer/type/completeness/size
  -> StepResultStore.write_prepared
  -> AgentState RUNNING -> SUCCEEDED
  -> StepResultStore.mark_readable
  -> STEP_COMPLETED(SUCCEEDED)
  -> StepCompletionResult(delivery=NOT_APPLICABLE)
  -> safe batch report
```

下游读取必须同时满足：entry 存在、READABLE、producer AgentState=SUCCEEDED、consumer 显式 depends_on producer、consumer 已 claim、Store 未 sealed。

### 4.2 FINAL Step

```text
Driver returns StepResult
  -> completion lock + RUNNING guard
  -> validate result
  -> Store.write_prepared
  -> AgentState RUNNING -> SUCCEEDED
  -> Store.mark_readable
  -> OutputGate.attempt_publish
       -> DELIVERED / FAILED / OUTCOME_UNKNOWN
  -> STEP_COMPLETED(SUCCEEDED)
  -> StepCompletionResult(step_state_committed=True, delivery_status=...)
  -> safe batch report
  -> Coordinator checks delivery before next Scheduler snapshot
       DELIVERED -> normal success evaluation
       FAILED/UNKNOWN -> Run FAILED delivery decision
```

伪代码：

```python
async def complete(claim, result, contract):
    validate(result, contract)
    store.write_prepared(claim.step_id, result)
    state_machine.apply_step_event(SUCCEEDED)
    store.mark_readable(claim.step_id)

    delivery = DeliveryStatus.NOT_APPLICABLE
    delivery_code = None
    if contract.output_policy.is_final:
        delivery, delivery_code = await output_gate.attempt(contract, result)

    await emit_step_completed(status="SUCCEEDED")
    return StepCompletionResult(
        step_id=claim.step_id,
        step_state_committed=True,
        result_readable=True,
        delivery_status=delivery,
        safe_error_code=delivery_code,
    )
```

### 4.3 失败表

| 位置 | StepState | entry | Run 结果 |
|---|---|---|---|
| result validation 失败 | FAILED | 无 | UNHANDLED_ERROR / STEP_RESULT_INVALID |
| Store PREPARED 写失败 | FAILED | 无 | UNHANDLED_ERROR / STEP_RESULT_PREPARE_FAILED |
| Step SUCCEEDED 状态提交失败 | RUNNING，Coordinator settle | PREPARED、不可读 | UNHANDLED_ERROR / STEP_STATE_COMMIT_FAILED |
| Store mark READABLE 失败 | SUCCEEDED | PREPARED、不可读 | UNHANDLED_ERROR / STEP_RESULT_COMMIT_FAILED；不调下游 |
| final output known failed | SUCCEEDED | READABLE | UNHANDLED_ERROR / FINAL_OUTPUT_DELIVERY_FAILED |
| final output outcome unknown | SUCCEEDED | READABLE | UNHANDLED_ERROR / FINAL_OUTPUT_DELIVERY_UNKNOWN |
| STEP_COMPLETED 发布失败 | SUCCEEDED | READABLE | UNHANDLED_ERROR / STEP_COMPLETION_EVENT_FAILED |

业务 Step 只有在“结果尚未提交为成功”前发生 validation/PREPARED 失败才 FAILED。输出交付发生在 Step SUCCEEDED 之后，因此永不改回 FAILED。

### 4.4 Coordinator 最小改法

`ParallelExecutor.execute_ready` 返回安全 report 后，`RunCoordinator._execute_batches` 必须：

```text
report = await executor_task
delivery_decision = decision_from_delivery(report)
if delivery_decision is not None:
    return delivery_decision
continue  # 只有无 delivery/commit failure 才再次 evaluate Scheduler
```

不修改 Scheduler 的 `is_complete` 定义；Coordinator 只是在下一次 success snapshot 前优先处理交付结果。`StepExecutionOutcome.result` 移除 raw body，换成安全 `StepCompletionResult/ResultMetadata`。

## 5. Delivery Outcome Contract

```python
class DeliveryStatus(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True, slots=True)
class StepCompletionResult:
    step_id: str
    step_state_committed: bool
    result_readable: bool
    delivery_status: DeliveryStatus
    safe_error_code: str | None
```

语义：

- `NOT_APPLICABLE`：INTERNAL Step，未尝试用户交付。
- `DELIVERED`：OutputGate 的 channel publish 正常返回；事件已入队。
- `FAILED`：可证明在 Journal append 前失败，用户正文未通过该事件提交。
- `OUTCOME_UNKNOWN`：Journal 已写但 channel enqueue 失败，或异常无法证明未提交。

它只存在于 **run-scoped completion result 和安全 batch report**，不新增 Snapshot 字段、不保存原文。Runtime Event 不新增 delivery_status 字段：

- Step 仍发布 `STEP_COMPLETED(status=SUCCEEDED)`；
- Coordinator 对 FAILED/UNKNOWN 发布现有 `ERROR`，携带 `FINAL_OUTPUT_DELIVERY_FAILED/UNKNOWN`；
- `RUN_COMPLETED` 携带 FAILED/UNHANDLED_ERROR；
- AgentState/RunSnapshot 只持久化最终 Run error_code，足以诊断。

OutputGate 状态 `PUBLISHING/PUBLISHED/OUTCOME_UNKNOWN` 均拒绝再次发布。`FAILED` 也不 retry，因为该 Run 的 final delivery 已终结；产品级重发应是新的 Run，而不是重用旧 Gate。

### partial publication 的 step sequence

EventChannel 在 Journal append 后、入队前失败会消费 run sequence（`core/runtime/event_channel.py:285-355`），`EventPublicationError` 提供 `partially_persisted` 和安全 evidence（`55-135`）。当前 StepEventEmitter 只有 publish 正常返回才递增 step sequence（`event_emitter.py:167-185`）。实施时必须让 StepEventEmitter 在捕获 `partially_persisted=True` 时也消费本次本地 step sequence，然后再抛出；否则后续 STEP_COMPLETED 会在 Journal 中复用 step_sequence。这个修正只维护序号事实，不把 delivery unknown 误标为 delivered。

## 6. Final Fingerprint Contract

### 6.1 进入 fingerprint

Plan fingerprint schema v2 的 canonical 输入：

- `fingerprint_schema_version`
- `plan_schema_version`
- `plan_id`
- `plan_version`
- `source`
- 安全 `task_summary` TextSummary（现有常量/安全摘要）
- 每个 Step（按 step_id 稳定排序）：
  - `step_id`
  - `preferred_agent`
  - `depends_on`（稳定排序）
  - `execution_kind`
  - `output_policy`
  - `capability_requirements`
  - 安全 completion criteria/title/description TextSummary
- 相应 plan/execution contract schema version

现有 fingerprinter 已以 PlanSnapshot 为唯一输入（`core/runtime/plan_fingerprint.py:16-32`），Snapshot 已包含 agent、dependencies、capabilities 和安全文本摘要（`snapshot_contract.py:190-252`）；实现只需增加 execution/output 字段并升级 schema。

### 6.2 明确不进入 fingerprint

- raw user query、file path、raw task instruction
- `SHA-256(raw instruction)` 或其他无密钥 digest
- StepInvocationBindings 内容
- StepResult/raw model/retrieval/tool output
- Memory、Journal payload、Trace、运行状态、delivery status
- Retry attempt、时间戳、并发完成顺序

选择 GPT 方案 A。Scheduler binding 只需要 step/depends_on/agent/capability（`scheduler.py:320-334`）；Store ACL 只需 compiled dependencies、step id 和 state；Synthesis 读 Store；Recovery 明确不能恢复结果/输出（`recovery_validation.py:689-724`）。Agent binding 一致性在 ResolvedPlan 构造时校验 key set、step id、preferred_agent 和 input type，运行时按 claim 再校验，不需要持久化 digest。MVP 不引入 HMAC/key rotation。

## 7. Final Event Ordering

外部事件继续保持 `OUTPUT_DELTA < STEP_COMPLETED`。Stream adapter 对 OUTPUT 独立转文本、对 control 独立编码（`stream_adapter.py:166-214`），并不从语义上强制顺序；保留顺序是为了 StepEmitter close 合同和现有 E2E 测试兼容。

### 7.1 Direct Answer success

```text
RUN_STARTED -> PLANNING_STARTED -> PLAN_CREATED
-> STEP_STARTED(core/entry specialist)
-> internal model/retrieval events
-> [internal StepState SUCCEEDED, Store READABLE]
-> OUTPUT_DELTA
-> STEP_COMPLETED(SUCCEEDED)
-> RUN_COMPLETED(SUCCEEDED, COMPLETED)
```

### 7.2 Multi-agent success

```text
RUN_STARTED -> PLANNING_STARTED -> PLAN_CREATED
-> specialist STEP_STARTED... (parallel)
-> specialist STEP_COMPLETED(SUCCEEDED)...      # 无 OUTPUT_DELTA
-> synthesis STEP_STARTED
-> [synthesis StepState SUCCEEDED, Store READABLE]
-> OUTPUT_DELTA
-> STEP_COMPLETED(synthesis, SUCCEEDED)
-> RUN_COMPLETED(SUCCEEDED, COMPLETED)
```

### 7.3 Final delivery failed

```text
... final Driver success
-> [Final StepState SUCCEEDED, Store READABLE]
-> OUTPUT_DELTA attempt fails before Journal
-> STEP_COMPLETED(SUCCEEDED)
-> ERROR(FINAL_OUTPUT_DELIVERY_FAILED)
-> RUN_COMPLETED(FAILED, UNHANDLED_ERROR)
```

### 7.4 Final delivery unknown

```text
... final Driver success
-> [Final StepState SUCCEEDED, Store READABLE]
-> OUTPUT_DELTA journaled but not confirmed enqueued
-> StepEmitter consumes failed publication step_sequence
-> STEP_COMPLETED(SUCCEEDED)
-> ERROR(FINAL_OUTPUT_DELIVERY_UNKNOWN)
-> RUN_COMPLETED(FAILED, UNHANDLED_ERROR)
```

### 7.5 Specialist failed

```text
STEP_STARTED(specialist)
-> STEP_COMPLETED(FAILED, agent-safe-code)
-> synthesis BLOCKED / never invoked
-> ERROR(AGENT_STEP_FAILED or REQUIRED_DEPENDENCY_FAILED)
-> RUN_COMPLETED(FAILED, UNHANDLED_ERROR)
```

### 7.6 Synthesis failed

```text
STEP_STARTED(synthesis)
-> STEP_COMPLETED(FAILED, SYNTHESIS_FAILED)
-> no OUTPUT_DELTA
-> ERROR(SYNTHESIS_FAILED)
-> RUN_COMPLETED(FAILED, UNHANDLED_ERROR)
```

Journal reducer 独立处理 OUTPUT、STEP_COMPLETED、RUN_COMPLETED，且声明 RUN_COMPLETED authoritative（`journal_tail_reducer.py:237-279,321-323`）；Snapshot/Recovery 不依赖 OUTPUT 相对 STEP_COMPLETED 的位置。

## 8. Final State and Error Mapping

| 场景 | StepState | RunStatus | StopReason | error_code |
|---|---|---|---|---|
| direct/multi success | 全 SUCCEEDED | SUCCEEDED | COMPLETED | null |
| planning schema/compile/permission | 无 Step 启动 | FAILED | PLANNING_FAILED | 具体 planning code |
| planning total deadline | 无/未完成 Step | FAILED | DEADLINE_EXCEEDED | DEADLINE_EXCEEDED |
| planning independent cap | 无 Step 启动 | FAILED | PLANNING_FAILED | PLANNER_TIMEOUT |
| planning cancellation | 无/未完成 Step | CANCELLED | existing cancel reason | cancel code |
| specialist invocation failed | specialist FAILED；synthesis BLOCKED | FAILED | UNHANDLED_ERROR | AGENT_STEP_FAILED |
| required dependency known failed | producer FAILED；consumer BLOCKED | FAILED | UNHANDLED_ERROR | REQUIRED_DEPENDENCY_FAILED |
| synthesis invocation failed | synthesis FAILED | FAILED | UNHANDLED_ERROR | SYNTHESIS_FAILED |
| final output known failed | final SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_DELIVERY_FAILED |
| final output unknown | final SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_DELIVERY_UNKNOWN |
| Store readable commit failed | producer SUCCEEDED | FAILED | UNHANDLED_ERROR | STEP_RESULT_COMMIT_FAILED |
| STEP_COMPLETED event failed | Step 已 terminal | FAILED | UNHANDLED_ERROR | STEP_COMPLETION_EVENT_FAILED |
| terminal event publication failed | authoritative Run state不回滚 | 已提交状态 | 已提交 reason | 现有 RUNTIME_TERMINAL_PUBLICATION_FAILED 异常 |

状态机允许 Step SUCCEEDED + Run FAILED，因为 Run FAILED 只禁止 COMPLETED/cancellation reason，并要求无 active Step（`state.py:333-344`; `state_machine.py:271-293`）。

## 9. Final Architecture

```text
/api/chat
  -> CoordinatedRunFactory / RunScope
       |- RunContext / Budget / Cancellation / EventChannel
       |- AgentRegistry(entry policy != delegated policy)
       |- RunCoordinator.for_dynamic_resolver
       |    -> PlanResolver
       |         -> deterministic routing OR model planner
       |         -> PlanningDecision
       |         -> PlanCompiler
       |         -> ResolvedPlan(Plan + StepInvocationBindings)
       |    -> freeze Plan once
       |    -> Scheduler / POST_PLAN_PRE_EXECUTION checkpoint
       |    -> ParallelExecutor
       |         -> MultiAgentDriver -> StepResult
       |         -> StepCompletionPipeline
       |              -> StepResultStore PREPARED/READABLE
       |              -> AgentStateMachine Step terminal
       |              -> OutputGate final delivery
       |              -> STEP_COMPLETED
       |              -> StepCompletionResult / safe report
       |    -> inspect report delivery before Scheduler success
       |    -> Run terminal decision/events
       `- cleanup Bindings/Store/Gate
```

### Owner matrix

| 对象/决策 | Owner | 可见原文 |
|---|---|---|
| Registry/entry-delegated permissions | static AgentRegistry | 否 |
| deterministic/model planning + compile | PlanResolver | user request/Planner output，仅规划期 |
| frozen DAG/output contract | Plan | 否，仅安全摘要/metadata |
| raw instruction | StepInvocationBindings / RunScope | 是，当前 claim 读取 |
| scheduling/readiness | Scheduler | 否 |
| Agent invocation | MultiAgentDriver | 当前 instruction/允许的依赖结果 |
| result preparation/visibility | StepCompletionPipeline + StepResultStore | 当前 result，禁止日志化 |
| Step state terminal | AgentStateMachine via Pipeline | 否 |
| final authorization/at-most-once | OutputGate | 仅授权 final body |
| delivery-to-Run mapping | RunCoordinator from safe report | 否 |
| Snapshot/fingerprint/recovery | Checkpoint/Snapshot/Recovery owners | 否 |
| Memory persistence | run-level final persistence owner | 仅 user + DELIVERED final；specialist raw 禁止 |

## 10. Final Acceptance Criteria

Round 1/2 的 1–59 保留。#60 修改为：

60. `preferred_agent/execution_kind/output_policy/dependency/capability/plan-or-fingerprint schema version` 任一变化都会改变 fingerprint；raw instruction 变化不要求持久化 fingerprint 改变。

新增：

61. OutputGate known failure 时 Final Step 保持 SUCCEEDED，Run 为 FAILED/UNHANDLED_ERROR/FINAL_OUTPUT_DELIVERY_FAILED。
62. OutputGate outcome unknown 时 Final Step 保持 SUCCEEDED，Run 为 FAILED/UNHANDLED_ERROR/FINAL_OUTPUT_DELIVERY_UNKNOWN。
63. FAILED/OUTCOME_UNKNOWN/PUBLISHING/PUBLISHED 状态均不得 retry 或第二次发布。
64. delivery failure 的 Step event 不得使用 `AGENT_STEP_FAILED`，必须发布 STEP_COMPLETED(SUCCEEDED) 后由 Run ERROR 表达。
65. Store 读取必须同时验证 READABLE、producer SUCCEEDED、compiled dependency、consumer claim 和未 sealed。
66. Coordinator 必须在下一次 Scheduler complete decision 前消费 delivery report；delivery failure 不得产生 RUN_COMPLETED(SUCCEEDED)。
67. partial-persisted OUTPUT_DELTA 必须消费 step_sequence，后续 STEP_COMPLETED 不得复用序号。
68. Plan/PlanSnapshot/fingerprint 不包含 raw instruction 或其普通 SHA-256；Bindings clear 后无法从持久化数据恢复 instruction。
69. final answer 只在 `DELIVERED` 后写入 final response Memory；FAILED/UNKNOWN 不保存 raw final。
70. INTERNAL Step 的 delivery status 恒为 NOT_APPLICABLE，且不调用 OutputGate publish。

## 11. Final Scope Gates and Estimate

### Core implementation gate：11–16 人日

只用于功能开发阶段验收：默认 API 真正调用专业 Agent、并行+synthesis 数据流、中间输出不泄漏、Direct Answer/失败无 fallback。达到后不宣称 Stage 2.5 完成，不添加新 Agent/新图形，立即进入合同硬化。

### Contract-complete MVP+ gate：累计 18–26 人日

补齐事件、Plan/Snapshot/fingerprint schema、Recovery Validation、安全边界、delivery report、partial-publication step sequence、fault injection 和完整回归后，才可标记 Stage 2.5 完成、写入简历为默认 Coordinated 多 Agent 主链、并作为 AgentEvalOps 稳定 Trace 来源。

delivery 分层没有显著扩大估算：Round 2 已包含 CompletionPipeline、OutputGate 和 fault hardening；新增的是 PREPARED/READABLE 状态、safe report 检查和 step sequence 故障测试，仍可落在 18–26 人日上界内。

停止条件不变：不做 dynamic Agent registration、recursive delegation、optional dependency、dynamic Plan mutation、raw result persistence/recovery、multi-round negotiation、full DAG UI、per-Agent durable Memory、complete chaos matrix、formal zero-hallucination guarantee。

## 12. Final Consensus Matrix

| 议题 | 状态 | 最终决策 |
|---|---|---|
| Plan lifecycle | CONSENSUS | 正式 Run 内受控 A，Resolver 返回 ResolvedPlan，一次 freeze |
| 默认 API | CONSENSUS | 全部进入 Resolver；显式 Agent 是 deterministic 分支 |
| Direct Answer | CONSENSUS | 四种合法图；direct 是成功决策，失败无 fallback |
| Registry | CONSENSUS | entry/delegated policy 分离；synthesis entry forbidden |
| instruction boundary | CONSENSUS | raw 只在 Bindings，Run 后 clear，不恢复 |
| instruction digest | CONSENSUS | 不进入 Plan/Snapshot/fingerprint；MVP 不引入 HMAC |
| Plan/fingerprint owner | CONSENSUS | 扩展 PlanStep，PlanSnapshot 是唯一持久化事实源 |
| Store owner | CONSENSUS | RunScope 所有，Pipeline 唯一写，PREPARED/READABLE |
| Driver capability | CONSENSUS | 只调用 Agent/返回 StepResult，无 Store/Gate 写权 |
| OutputGate owner | CONSENSUS | Pipeline 调用，Gate 决策和 at-most-once |
| execution/delivery split | CONSENSUS | Final Step SUCCEEDED 后交付；交付失败只使 Run 失败 |
| delivery status | CONSENSUS | completion/report 内部字段，不扩 Snapshot |
| event order | CONSENSUS | 内部先状态成功；外部保留 OUTPUT_DELTA < STEP_COMPLETED |
| partial publication | CONSENSUS | OUTCOME_UNKNOWN、不 retry、消费 step sequence |
| dependency | CONSENSUS | MVP 全 required，失败使 synthesis BLOCKED |
| StopReason | CONSENSUS | 只新增 PLANNING_FAILED；业务/交付细分用 error_code |
| Memory | CONSENSUS | specialist raw 禁止；仅 delivered final 可持久化 |
| Checkpoint/Recovery | CONSENSUS | dynamic post-plan checkpoint；无 bindings/result recovery |
| Synthesis | CONSENSUS | 依赖白名单、fail-closed、无拼接/core fallback |
| Budget/timeout/cancel | CONSENSUS | 共用 RunContext/Ledger/deadline/token |
| MVP scope/estimate | CONSENSUS | Core 11–16；Contract-complete 累计 18–26；到门即停止 |

## 13. Remaining Risks

- EventChannel 只能保证 at-most-once publish attempt，不能保证 exactly-once delivery；Journal 成功/queue 失败会是 OUTCOME_UNKNOWN。
- terminal event 自身发布失败时，权威 Run state 可能已成功/失败但客户端看到 terminal missing；沿用当前 Stage 2 合同。
- raw StepResult/Bindings 不持久化，crash 后不能从专业结果或 synthesis 中间点继续。
- prompt 和输入白名单不能形式化保证模型零幻觉。
- sync provider/tool worker 取消后可能 detached；Store 先 seal，worker 退出后 clear。
- raw instruction 不进入 fingerprint，Snapshot 只能证明执行图/权限合同一致，不能证明原始 payload 可重放；这是明确非目标。

## 14. Ready for Consensus

```text
Codex final architecture position:
- Planning is a formal Run phase managed by a business-agnostic Coordinator through PlanResolver; the immutable Plan is frozen once.
- The default API always enters Resolver; explicit entry agents use deterministic routing and four compiled execution shapes.
- Raw instructions live only in run-scoped StepInvocationBindings and are absent from Plan, Snapshot, fingerprint, Journal, Trace, Event, log and Memory.
- MultiAgentDriver only returns StepResult; StepCompletionPipeline owns PREPARED/READABLE result commit and Step terminal state.
- Final Step execution success is distinct from output delivery: the Step remains SUCCEEDED when delivery fails or is unknown, while the Run fails with a delivery error code.
- OutputGate provides at-most-once publish attempts; partial publication becomes OUTCOME_UNKNOWN and is never retried.
- Persistent fingerprint covers the safe execution contract, not raw instruction or its ordinary digest.
- External event compatibility remains OUTPUT_DELTA before STEP_COMPLETED, while internal StepState is committed first.
- MVP remains static, flat, all-required, single-final and fail-closed; Contract-complete effort remains 18–26 person-days.
- Remaining P0: 0
- Remaining P1: 0
- Ready to generate consensus document: YES
```
