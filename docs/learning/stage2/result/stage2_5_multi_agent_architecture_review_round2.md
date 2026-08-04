# Stage 2.5 Multi-Agent Architecture Review — Round 2

> 状态：Round 2 合同收敛稿。本轮只审查源码和设计，没有修改生产代码。
> 输入：Round 1 评审及 GPT 对 C1–C7、P0-1～P0-3、P1-1～P1-5 的正式回应。

## 1. Executive Summary

### 1.1 Round 1 后新增发现

1. **Round 1 遗漏了合法的 Direct Answer。** 当前 `/api/chat` 请求体明确携带 `agent_id`（`server.py:603-609`），默认 Coordinated 工厂把它直接编译成单 `answer` Step（`core/runtime/runtime_factory.py:254-307`）。因此 core_router 普通聊天、显式选择专业 Agent 的现有单 Agent行为都是必须兼容的合法路径，不能把“不委派”误判为规划失败。
2. **当前 Plan 没有用户问题原文，但把新 `instruction` 直接放进去会扩大泄漏面。** `create_single_step_plan` 使用常量 title/description/summary（`core/runtime/planning.py:99-110`）；Snapshot 对这些文本只保存 length/digest（`core/runtime/snapshot_contract.py:228-252,312-325`）。然而 `Plan`/`PlanStep` 是默认 dataclass repr，原始 instruction 若进入 Plan，仍可能经调试、异常包装或未来日志泄漏。应把原文与可持久化执行合同分离。
3. **GPT 对 Driver 写 Store 的反对成立。** `StepExecutionDriver` 当前合同已经写明“只执行业务，不得修改 AgentState、发送 STARTED 或决定调度”（`core/runtime/parallel_execution.py:105-107`）。结果提交应由统一 completion pipeline 所有。
4. **当前不存在 Step 级 Retry。** ParallelExecutor 每个 claim 只调用一次 `_invoke`（`core/runtime/parallel_execution.py:205-218,359-380`）；模型 Retry 位于 `ModelInvocationService` 内部，且在最终返回前完成预算结算（`core/runtime/model_invocation.py:538-783`）。所以 Store/Gate 必须位于 Driver 完整返回之后，不能位于 attempt 内。
5. **`StepExecutionOutcome.result` 会成为第二条原文泄漏路径。** 当前 batch report 会携带任意 `Any`（`core/runtime/parallel_execution.py:71-102`）。引入 Store 后，report 不应再保留 raw StepResult，只保留安全 metadata/status。
6. **Output、Store、状态和 EventChannel 无法构成真正原子事务。** EventChannel 是 journal-first；Journal 成功后、入队前仍可能失败（`core/runtime/event_channel.py:279-355`）。最终输出一旦尝试发布就不能安全重试，否则可能重复。因此 OutputGate 需要 `OUTCOME_UNKNOWN` 终态，而不只是布尔 `published`。

### 1.2 已达成共识

- C1：Planning 属于正式 Run；选择受控方案 A；Coordinator 不包含业务 Planner/Compiler 逻辑。
- C2：Run-scoped StepResultStore 的所有权、单写、授权读、容量、seal/clear、不持久化和不恢复边界。
- C3：`INTERNAL / FINAL_PASSTHROUGH / FINAL_SYNTHESIS`，且进入不可变 fingerprint 合同。
- C4：MVP 所有依赖 required，拒绝 optional。
- C5：动态规划首个 checkpoint 是 `POST_PLAN_PRE_EXECUTION`。
- C6：专业结果不写 Memory。
- C7：required/Synthesis 失败均 fail-closed，无 core_router 补写或拼接降级。
- D2：ParallelExecutor 负责调用独立 OutputGate 的时机，Gate 独占授权决策。

### 1.3 本轮推荐

- 使用 `PlanResolver`，返回已经验证和编译的 `ResolvedPlan`；Coordinator 不直接依赖 `PlanCompiler`。
- 默认 API 的所有请求进入 Resolver，但显式 `selected_agent != core_router` 走确定性分支并完全绕过模型 Planner。现有 static-plan 路径保留为兼容构造入口，不作为失败 fallback。
- 允许四种最终执行形态：core direct、authorized specialist direct、single specialist+synthesis、N specialist fan-out+synthesis。
- 选择 Plan/调用原文分离：Plan 只含安全静态合同和 `input_digest`；raw instruction 放入 run-scoped、read-only、不可 repr 的 `StepInvocationBindings`。
- Driver 只返回 StepResult；新增小型 `StepCompletionPipeline` 作为 Store 写入、Gate 调用、状态终结和安全 report 的唯一 owner。
- fingerprint 选择扩展现有 `PlanStep`，不新增并行的 CompiledPlan 事实源。
- StopReason **只新增 `PLANNING_FAILED`**；专业/Synthesis 都复用 `UNHANDLED_ERROR`，用稳定 error_code 区分。

### 1.4 是否建议现在实施

**NO。** 设计主干已收敛，但仍需 GPT 对以下真实 P0/P1 作最后确认：

- P0：`OUTPUT_DELTA` journal 已提交但 channel enqueue 失败时的 at-most-once/`OUTCOME_UNKNOWN` 语义。
- P1：只新增 `PLANNING_FAILED` StopReason。
- P1：扩展 PlanStep 而非新增 CompiledPlan。
- P1：默认 API 全部经过 Resolver，以及哪些显式可选 specialist 被授权 direct passthrough。

用户也尚未发出“开始实施”。

## 2. Response to GPT Decisions

| 共识项 | Codex 回应 | 源码依据 | Round 2 收敛 |
|---|---|---|---|
| C1 受控方案 A | **ACCEPT WITH MODIFICATION** | 当前 Coordinator 构造期持有 Plan（`run_coordinator.py:140-171`），Snapshot 分支立即绑定 Plan（`178-214`）；scope 在 Coordinator 执行前创建（`chat_service.py:300-359`） | 名称采用 `PlanResolver`；Resolver 返回完整 `ResolvedPlan`，Coordinator 不调用 Compiler。用两个 public classmethod/factory 隔离 static/dynamic 入口 |
| C2 StepResultStore | **ACCEPT** | report 结果非持久化且 Coordinator 丢弃（`parallel_execution.py:71-102`; `run_coordinator.py:435-440`）；Recovery 明确无结果 owner（`recovery_validation.py:689-724`） | 保持 Round 1；增加“读取同时验证 producer 已 SUCCEEDED”以及 report 不携带原文 |
| C3 OutputPolicy | **ACCEPT** | 当前字符串判断在 `parallel_execution.py:223-232`；fingerprint 只来自 PlanSnapshot（`plan_fingerprint.py:16-32`） | Gate 增加 `NEW/PUBLISHING/PUBLISHED/OUTCOME_UNKNOWN` 状态；任何发布调用开始后不得 retry |
| C4 不支持 optional | **ACCEPT** | Scheduler 仅在所有依赖 SUCCEEDED 时 ready，失败依赖会 BLOCKED（`scheduler.py:336-390`） | `required=False` 是 compile error；不改 Scheduler edge 语义 |
| C5 Dynamic checkpoint | **ACCEPT** | CheckpointCoordinator 必须持有 concrete Plan（`checkpoint.py:89-123`），捕获时从 Plan 生成 snapshot/fingerprint（`203-257`） | 动态路径：freeze/init -> PLAN_CREATED -> POST_PLAN_PRE_EXECUTION；规划失败无 snapshot |
| C6 Memory 边界 | **ACCEPT** | `MemoryManager.add_message` 保存原文（`memory_manager.py:155-180`） | specialist raw output 只在 Store；user+唯一 final 才能持久化；metadata 单独 allowlist |
| C7 Synthesis fail-closed | **ACCEPT** | Scheduler 已能让失败依赖下游 BLOCKED（`scheduler.py:336-368`）；Legacy synthesis 目前仅是 prompt 约束（`agent_router.py:1576-1605`） | 不启动/不回退/不拼接；Synthesis 只拿 dependency-scoped result view |

关于 D2：标记为 **原则达成一致**。剩余问题不是 Gate owner，而是不可原子跨越 Store、Output Event 和 AgentState 的提交顺序。

## 3. Direct Answer and Delegation Model

### 3.1 当前行为核验

- API 强制请求方提供 `agent_id`（`server.py:603-609`），并原样交给 Legacy 或 Coordinated（`server.py:691-697,784-794`）。
- 当前 Coordinated 无论 agent_id 是什么，都调用 `build_single_agent_plan(agent_id, query)`（`runtime_factory.py:304-307`），然后由固定 `CoordinatedSingleAgentDriver` 执行（`362-369`）。
- `build_single_agent_plan` 最终使用 `create_single_step_plan`（`agent_router.py:959-974`）；因此现在的 core_router 普通回答和显式专业 Agent 回答都是合法单 Step。
- AgentRouter 的配置查询目前会对未知 ID 回退 core_router（`agent_router.py:240-245,1784-1786`）。新 Registry 必须移除这种静默行为；未知 ID 要显式失败。

### 3.2 PlanningDecision

```python
@dataclass(frozen=True, slots=True)
class DirectAnswerDecision:
    agent_id: str
    instruction: str = field(repr=False)
    reason_code: str                 # DETERMINISTIC_DIRECT / MODEL_DIRECT


@dataclass(frozen=True, slots=True)
class DelegatedTaskDecision:
    task_id: str
    agent_id: str
    instruction: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DelegatedPlanDecision:
    tasks: tuple[DelegatedTaskDecision, ...]
    synthesis_required: bool


PlanningDecision = DirectAnswerDecision | DelegatedPlanDecision
```

这是 Resolver 内部的短生命周期中间合同，不是 Runtime Plan，也不进入 Journal/Snapshot/Trace。

### 3.3 四种合法执行形态

```text
0. core_router [FINAL_PASSTHROUGH]

1. authorized specialist [FINAL_PASSTHROUGH]

2. specialist [INTERNAL]
       -> synthesis_agent [FINAL_SYNTHESIS]

3. specialist_1 [INTERNAL] --\
   specialist_2 [INTERNAL] ----> synthesis_agent [FINAL_SYNTHESIS]
   specialist_N [INTERNAL] --/
```

第 0 种是必须补回的合法 Direct Answer。第 1 种用于用户在 API/UI 中显式选择现有专业 Agent，以及 Registry 明确授权的单专家透传。为兼容当前行为，建议 `knowledge_expert`、`code_expert`、`data_analyst` 在“显式 entry selection”场景允许 passthrough；`synthesis_agent` 不可作为 entry agent。core_router 自然语言触发的委派则使用 Registry 的 `delegated_default_output`：knowledge 可 passthrough，code/data 默认经 synthesis。

### 3.4 合法 Direct 与非法 fallback

| 情况 | 结果 |
|---|---|
| deterministic rule 明确判定问候/普通聊天 | 成功的 `DirectAnswerDecision(core_router)` |
| model Planner 返回合法 `DIRECT_ANSWER` | 成功的 `DirectAnswerDecision(core_router)` |
| selected_agent 是允许 entry 的专业 Agent | 确定性 direct plan；不调用 Planner model |
| Planner schema 失败 | `PLANNING_FAILED`；不得 core_router 自答 |
| Compiler 拒绝未知 Agent/环/非法 policy | `PLANNING_FAILED`；不得 core_router 自答 |
| Planner timeout/cancel | deadline/cancel terminal；不得 core_router 自答 |

默认 `/api/chat` 的所有请求都进入 `PlanResolver`，但不是所有请求都调用模型 Planner。这样 unknown agent、显式选择和 direct/delegate 都在正式 Run 中产生一致事件。现有 static plan 通过 `RunCoordinator.for_static_plan(...)` 保留，供内部兼容/测试/明确已编译调用使用，不是 Resolver 失败后的逃生口。

## 4. Plan and Invocation Data Boundary

### 4.1 选择方案 II：Plan 与原始调用参数分离

当前 `PlanStep` 字段是 `step_id/title/description/depends_on/completion_criteria/preferred_agent/capability_requirements`（`planning.py:48-57`）。当前单 Step Plan 的文本均为常量，不包含 query（`planning.py:99-110`）。`server.py:785-789` 还会把真实 file_path 拼入 query，因此未来 instruction 可能包含文件路径和敏感业务文本。

PlanSnapshot 目前不会持久化这些文本原文：description/title/completion criteria/task summary 都转为 `TextSummary`（`snapshot_contract.py:228-252,312-325`）。Runtime Event 当前也没有 PLAN/PLANNING 类型。但如果原始 instruction 直接进入 frozen dataclass Plan：

- 默认 dataclass `repr` 会包含它；
- PlanGraph/Scheduler 异常目前只打印安全 plan/step id（`plan_graph.py:24-32`; `scheduler.py:49-61`），但无法保证未来调用方不会记录 Plan；
- Snapshot 将需要为该字段重新证明安全投影；
- Plan 的调度事实与业务 payload 生命周期被混在一起。

因此不选择方案 I。

### 4.2 合同

```python
@dataclass(frozen=True, slots=True, repr=False)
class AgentInvocationSpec:
    step_id: str
    agent_id: str
    instruction: str
    input_type: str


class StepInvocationBindings:
    """构造后只读；只有 RunScope 可 close/clear；repr 不显示 items。"""
    def resolve_for_claim(self, claim: StepClaim) -> AgentInvocationSpec: ...
    def close_and_clear(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ResolvedPlan:
    plan: Plan
    invocation_bindings: StepInvocationBindings = field(repr=False)
    planning_source: str
```

`StepInvocationBindings` 是小型 immutable/read-only mapping capability，不是第二个通用 Store：构造后没有 write API、没有动态 key、没有查询全部原文的公共 API。内部可在 Run 结束时清空引用，以满足生命周期清理。

### 4.3 Plan 中保留什么

建议在 PlanStep 现有静态字段上增加：

```text
execution_kind
output_policy
input_digest
```

`preferred_agent` 已是实际 agent id（Scheduler 在 `scheduler.py:253` 把它写入 StepClaim），不再重复新增 `agent_id`。`input_digest` 是 instruction 的 SHA-256 摘要，只用于 fingerprint/一致性，不可用于恢复原文。PlanCreated event 只发布 plan id/version/fingerprint/step count/source，不发布 title/description/instruction/digest 之外的业务内容；为降低短文本字典攻击风险，普通日志甚至不需要发布 input_digest。

### 4.4 生命周期和访问

```text
PlanningRequest(raw user request)
  -> PlanResolver / transient PlanningDecision
  -> PlanCompiler
       |- Plan(safe static contract + input_digest)
       `- StepInvocationBindings(raw, repr=False)
  -> ResolvedPlan
  -> RunScope owns bindings
  -> MultiAgentDriver resolves only current claim
  -> Run terminal: close + clear references
```

PlanSnapshot 只保存安全 Plan 合同；Bindings 不进入 Checkpoint、Journal、Trace、普通日志或 Memory。Crash 后 bindings 不恢复，所以任何尚未执行的 Step 或需要后续 synthesis 的 Run 都不可继续；这与 `recovery_validation.py:689-724` 的现有 result rehydration 限制一致。

## 5. Result Commit Pipeline

### 5.1 Owner 决策

接受 GPT 的 P0-3：MultiAgentDriver 不持有 Store/Gate。新增一个小型 `StepCompletionPipeline`，由 ParallelExecutor 独占调用。它可以先作为 `parallel_execution.py` 内部类实现，避免过早形成通用框架；合同稳定后再决定是否独立文件。

| 职责 | 唯一 owner |
|---|---|
| Agent 调用、输入/输出适配 | MultiAgentDriver |
| provider/model/tool attempt retry | 已有 ModelInvocation/ToolExecution 服务 |
| logical Step 结果校验 | StepCompletionPipeline |
| Store write-once | StepCompletionPipeline |
| OutputGate 调用时机 | StepCompletionPipeline（由 Executor 驱动） |
| 输出授权/at-most-once | OutputGate |
| Step terminal AgentState 提交 | StepCompletionPipeline 经 AgentStateMachine |
| STEP_COMPLETED 发布 | StepCompletionPipeline |
| 安全 Batch report | ParallelExecutor |

### 5.2 完整序列

```text
Scheduler.claim_ready
  -> AgentState: PENDING -> RUNNING
  -> STEP_STARTED
  -> ParallelExecutor worker
       -> MultiAgentDriver.execute(claim, context)
            -> bindings.resolve_for_claim(claim)
            -> Agent adapter
                 -> internal model/tool/retrieval attempts and retries
            -> return StepResult              # Driver 无 Store/Gate 引用
       -> StepCompletionPipeline.commit(...)
            1. verify claim still RUNNING and acquire per-step completion lock
            2. validate StepResult type/producer/complete/content/size
            3. Store.write_once(step_id, result)       # 尚不可被下游读取
            4. OutputGate.publish_if_allowed(contract, result)
                 INTERNAL -> no-op
                 FINAL_* -> prevalidate + NEW->PUBLISHING
                              -> emit OUTPUT_DELTA
                              -> PUBLISHED or OUTCOME_UNKNOWN
            5. AgentStateMachine: RUNNING -> SUCCEEDED
            6. emit STEP_COMPLETED(SUCCEEDED)
            7. return safe StepExecutionOutcome        # 无 raw result
  -> ParallelExecutionReport(safe metadata only)
  -> Coordinator consumes safe report/final Scheduler snapshot
```

Retry 位于 Driver 使用的业务服务内部，发生在第 1 次 `StepResult` 返回之前。当前 ParallelExecutor 本身没有再次调用 Driver 的 Step Retry（`parallel_execution.py:205-218`）；模型重试在 `model_invocation.py:710-729`，每个 attempt 都单独预算，只有最终成功才返回 `ModelInvocationResult`（`773-884`）。所以 attempt 不能写 Store 或触发 Gate。

### 5.3 失败语义

| 失败点 | Step 状态 | Store 可读性 | 输出 | Run 语义 |
|---|---|---|---|---|
| Driver/所有 attempts 失败 | FAILED | 无 entry | 无 | execution failure |
| Result 合同/大小失败 | FAILED，`STEP_RESULT_INVALID/TOO_LARGE` | 无 entry | 无 | execution failure |
| Store duplicate/capacity/write 失败 | FAILED，安全 Store code | 无成功可见结果 | 无 | infrastructure/execution failure；不得 retry Driver |
| OutputGate policy 校验失败 | FAILED，`OUTPUT_POLICY_VIOLATION` | entry 存在但不可读 | 无 | Run failed |
| OUTPUT_DELTA 在 Journal 前失败 | FAILED，`FINAL_OUTPUT_PUBLISH_FAILED` | 不可读 | 无 | Run failed；Gate sealed，不 retry |
| OUTPUT_DELTA Journal 后、enqueue 前失败 | Step 收敛 FAILED/CANCELLED；`FINAL_OUTPUT_OUTCOME_UNKNOWN` | 不可读 | 用户可见性未知 | Run/transport failed；绝不 retry，防重复 |
| AgentState SUCCEEDED 提交失败 | RUNNING 后由 Coordinator settle；infra error | entry 不可读 | final 可能已发布 | Run failed；用户可能看到 final+error，无法回滚 |
| STEP_COMPLETED 发布失败 | AgentState 已 SUCCEEDED | 依赖可在状态成功后读取 | final 可能已发布 | 不回滚 Step；升级为 Run infrastructure failure/terminal-missing transport |

### 5.4 Store 读取规则

Store entry 在写入后不是单靠“存在”即可读取。`read_for(consumer_step_id, producer_step_id)` 必须同时验证：

1. consumer 的编译依赖白名单包含 producer；
2. producer AgentState 当前为 `SUCCEEDED`；
3. entry producer/step id 与 compiled contract 一致；
4. Store 尚未 sealed/cleared；
5. consumer 是当前已 claim 的 synthesis/依赖 Step。

因此 Store 已写但 Gate/状态提交失败时，下游仍不可读取。无需 rollback raw entry；Run 终结统一 clear。

### 5.5 Duplicate callback 与 report

- 每 step completion lock + AgentState `RUNNING` guard 保证 first completion attempt 才能进入。
- 第二次 callback 是 `STEP_COMPLETION_DUPLICATE` 基础设施错误，不静默忽略、不重复写、不重复输出。
- 若 Run 已 first-wins terminal，迟到 callback 不能改写终态，只记录安全 metric；内容不得进入日志。
- `StepExecutionOutcome.result` 应替换为 `result_metadata`（producer、content type、length、digest、complete）或保持 None。BatchExecutionReport 不得成为 Store 的复制品。

### 5.6 不可消除的原子性边界

EventChannel 明确 journal-first，并允许“Journal 已成功、Transport 入队失败”（`event_channel.py:285-355`）。所以不存在不引入事务型 outbox 就能保证的 Store+State+用户流原子提交。MVP 的保守合同是：OutputGate 一旦进入 PUBLISHING，无论 emit 成功或异常都永久禁止第二次 publish；异常若不能证明未提交，则标记 `OUTCOME_UNKNOWN`，Run 失败。这个限制必须写入故障注入验收，不能宣称 exactly-once，只能宣称 **at-most-once attempt**。

## 6. StopReason Decision

### 6.1 当前枚举和使用

`core/runtime/state.py:51-63` 当前共有：

| StopReason | 实际用途/设置位置 |
|---|---|
| `COMPLETED` | 成功；`state.py:296`、`run_coordinator.py:450-455,778-783` |
| `UNHANDLED_ERROR` | 通用异常和任一 Step FAILED；`agent_loop.py:339,443`、`run_coordinator.py:459-467,573-580` |
| `DEADLINE_EXCEEDED` | deadline；`run_coordinator.py:527-535,564-571` |
| `USER_CANCELLED` | request/stream cancellation 映射；`run_coordinator.py:537-547` |
| `CLIENT_DISCONNECTED` | client disconnect；同上 cancellation mapping |
| `SYSTEM_SHUTDOWN` | shutdown cancellation；同上 |
| `MAX_STEPS_REACHED` | Legacy AgentLoop；`agent_loop.py:267` |
| `NO_ACTION` | 无 ready/running 且存在 blocked/unresolved；`run_coordinator.py:469-470,582-589` |
| `REPEATED_ACTION` | Legacy AgentLoop；`agent_loop.py:297` |
| `BUDGET_EXHAUSTED` | budget；`run_coordinator.py:555-562` |

当前没有 `STEP_FAILED` 或 `EXECUTION_FAILED` StopReason。任一 Step FAILED 最终统一是 `RunStatus.FAILED + StopReason.UNHANDLED_ERROR + error_code=STEP_EXECUTION_FAILED`（`run_coordinator.py:459-467`）。仅 blocked/unresolved 则是 NO_ACTION。

这些值不是内部私有细节：RunCompletedPayload 携带 stop_reason（`events.py:399-406,528`），stream adapter 暴露它（`stream_adapter.py:123`），Snapshot 保存并反序列化为 StopReason（`snapshot_contract.py:461-525,717-748`），journal reducer 比较具体值（`journal_tail_reducer.py:202,262-266,528-533`），并有大量 state/coordinator/snapshot 测试依赖。

### 6.2 最终推荐：只新增 PLANNING_FAILED

接受 GPT 的分层方向。Planning 是新的 Run phase，值得新增 StopReason；Synthesis 仍是 Step，不应为每个业务角色扩展 StopReason。为最小改动，普通执行失败继续复用 `UNHANDLED_ERROR`，通过 error_code 表达根因。

```text
规划失败:
RunStatus = FAILED
StopReason = PLANNING_FAILED
error_code = PLANNER_SCHEMA_INVALID / PLAN_COMPILE_FAILED / UNKNOWN_AGENT / ...

专业 Agent Step 失败:
RunStatus = FAILED
StopReason = UNHANDLED_ERROR
error_code = AGENT_STEP_FAILED（Step event 保留更具体 provider code）

Synthesis Step 失败:
RunStatus = FAILED
StopReason = UNHANDLED_ERROR
error_code = SYNTHESIS_FAILED

required 依赖 blocked:
RunStatus = FAILED
StopReason = UNHANDLED_ERROR
error_code = REQUIRED_DEPENDENCY_FAILED
```

依赖 blocked 不应继续落到 `NO_ACTION`，因为原因已知且不是“没有动作可做”。Coordinator 的决策需先识别“failed producer 导致 final blocked”，再使用 `REQUIRED_DEPENDENCY_FAILED`。本轮不新增 `SYNTHESIS_FAILED` StopReason，也不新增 `EXECUTION_FAILED`，避免扩大枚举、Snapshot 和前端兼容范围。

Planner cancellation 仍使用取消 StopReason；Planner deadline 使用 `DEADLINE_EXCEEDED`；Planner budget 使用 `BUDGET_EXHAUSTED`，只有 schema/compile/业务规划失败使用 `PLANNING_FAILED`。

## 7. Fingerprint Contract

### 7.1 真实影响面

Plan 被 12 个 core 文件直接引用，包括 planning、graph、scheduler、executor、coordinator、checkpoint、snapshot、recovery 和 router；12 个测试/fixture 文件直接构造 Plan。PlanStep 的关键字段被 Scheduler 绑定并生成 claim（`scheduler.py:229-253,320-334`），Checkpoint 以 `PlanSnapshot.from_plan` 作为 fingerprint 唯一输入（`checkpoint.py:247-248`; `plan_fingerprint.py:16-32`）。

### 7.2 A/B 比较

| 维度 | A：扩展 PlanStep | B：Plan + CompiledPlan |
|---|---|---|
| 唯一事实源 | Plan 继续同时是 DAG 和静态执行合同 | 两个结构都含 step/依赖/agent，易漂移 |
| Scheduler | 继续绑定 Plan | 必须决定绑定 Plan 还是 CompiledPlan，并验证两者一致 |
| Checkpoint/Recovery | 版本化 PlanSnapshot 即可 | 必须新增 CompiledPlanSnapshot 或替换现有 snapshot owner |
| static path | 给新增字段兼容默认/显式构造 | 需要把所有旧 Plan 再 compile/upgrade |
| 测试影响 | 修改 Plan 构造和 fingerprint fixtures | 新类型、转换、一致性和恢复测试更多 |
| 原文边界 | raw instruction 已由 Bindings 分离 | 同样仍需要 Bindings，CompiledPlan 不能解决原文问题 |

### 7.3 明确选择 A

扩展现有 PlanStep，不新增 CompiledPlan。原因是当前 `Plan` 的文档语义就是“可供 Scheduler 使用的不可变计划”（`planning.py:60-68`），`PlanStep` 也是“静态定义，不承载 Runtime 状态”（`48-57`），正适合承载 execution/output 静态合同。

建议字段：

```text
preferred_agent        # 保留，作为 canonical agent id
execution_kind         # AGENT / SYNTHESIS
output_policy          # INTERNAL / FINAL_PASSTHROUGH / FINAL_SYNTHESIS
input_digest           # 64 hex，原文在 Bindings
```

PlanSnapshot schema 和 fingerprint schema 均升级。fingerprint 必须包含上述字段；不能把 output policy/agent 放在未 fingerprint 的 sidecar。现有 v1 snapshots 可继续读取做验证，但不能被当作具备 v2 multi-agent 执行合同的可恢复 snapshot。static `create_single_step_plan` 提供兼容默认合同；默认 API 的 dynamic Resolver 总是显式填充真实 digest/policy。

## 8. Explicit Routing and Planner Decision Table

### 8.1 优先级

```text
1. Registry 校验 selected_agent
2. selected_agent != core_router -> deterministic explicit-entry decision
3. core_router request -> deterministic explicit delegation/direct rules
4. 未决 -> model Planner
5. typed parse + PlanCompiler
6. 任一 schema/compile failure -> PLANNING_FAILED（无 fallback）
```

### 8.2 决策表

| 输入 | 是否调用模型 Planner | PlanningDecision / 终态 | 执行形态 |
|---|---:|---|---|
| `selected_agent=knowledge_expert` | 否 | deterministic direct，前提 Registry entry/passthrough 授权 | knowledge `FINAL_PASSTHROUGH` |
| `selected_agent=code_expert` | 否 | deterministic direct，兼容当前显式 Agent 对话 | code `FINAL_PASSTHROUGH` |
| `selected_agent=data_analyst` | 否 | 同上 | data `FINAL_PASSTHROUGH` |
| `selected_agent=synthesis_agent` | 否 | `UNKNOWN/ENTRY_AGENT_NOT_ALLOWED` planning failure | 无 Step |
| `selected_agent=core_router` + 明确问候/普通聊天 | 否（规则有把握时） | `DirectAnswerDecision(core_router)` | core `FINAL_PASSTHROUGH` |
| core_router + “调用知识专家，总结 x.md” | 否 | deterministic single delegation | knowledge `FINAL_PASSTHROUGH` |
| core_router + “调用代码专家……” | 否 | deterministic single delegation | code INTERNAL -> synthesis |
| core_router + “调用知识和代码专家……” | 否，若别名和任务边界明确；否则模型 | deterministic/model fan-out | knowledge+code INTERNAL -> synthesis |
| 不能确定是否委派 | 是 | model typed decision | direct 或合法 delegate plan |
| 模型 Planner 返回 `DIRECT_ANSWER` | 是 | 成功 direct，不是 fallback | core `FINAL_PASSTHROUGH` |
| Planner schema failure | 已调用 | `PLANNING_FAILED/PLANNER_SCHEMA_INVALID` | 不执行、不 core 自答 |
| unknown agent（selected 或 Planner 输出） | 视来源 | `PLANNING_FAILED/UNKNOWN_AGENT` | 不执行 |
| cyclic/missing dependency | 视来源 | `PLANNING_FAILED/PLAN_COMPILE_FAILED` | 不执行 |
| Planner timeout | 是 | `FAILED/DEADLINE_EXCEEDED/PLANNER_TIMEOUT` | 不执行 |
| Planner cancellation | 可能 | `CANCELLED/<cancel reason>` | 不执行 |
| Planner budget exhausted | 是 | `FAILED/BUDGET_EXHAUSTED` | 不执行 |

确定性自然语言解析只识别 Registry aliases 和明确命令结构，不做开放式语义推理。它可以把同一原始请求作为多个 Agent 的 instruction；若需要复杂任务拆分则调用模型 Planner。无论哪种来源，都必须经过同一个 PlanCompiler。

## 9. Planning Budget, Timeout and Cancellation

### 9.1 复用现有 Stage 2 合同

- RunContext 持有唯一 deadline/cancellation token/budget ledger（`context.py:119-157,198-210`）。Resolver 不创建子 RunContext 或第二账本。
- `BudgetLedger.reserve/commit/release` 已是原子 owner（`budget.py:92-145`）。
- ModelInvocation 在 provider 前 reserve（`model_invocation.py:538-566`）；provider 未开始则 release，已开始失败按 estimate commit（`637-651`）；成功按 actual/estimate commit（`773-783`）。Planner 必须通过这个统一入口，不能直接调用 provider。

### 9.2 具体规则

| 问题 | 决策 |
|---|---|
| deterministic planning 预算 | 不伪造 model call，不 reserve token/cost；记录 content-free planning decision metric。开始/结束都调用 `run_context.raise_if_inactive()` |
| model planning reserve | 复用 ModelInvocationService；`reservation_type=model_invocation`，计入 model/remote call、input/output token、cost、retry |
| provider 未开始失败 | release reservation |
| provider 已开始但失败 | commit estimated usage |
| provider 成功但 JSON/schema 失败 | 模型调用已经发生，保留 actual/estimated commit；解析失败不能退款 |
| Planner 业务失败后的其他未用预算 | 没有新 reservation；已完成/已开始 attempt 按现有规则结算，未开始 attempt release |
| timeout | 使用 `min(run_context.remaining_seconds(), configured_max_planning_seconds)`；独立 cap 不能延长 Run 总 deadline |
| cancellation | Resolver/模型入口前后检查同一 token；async task 取消向上传播；sync provider 按现有 bounded/detached worker 语义处理 |
| retry | 仍由 ModelInvocation RetryExecutor 决定，逐 attempt 重新 reserve，受 remaining deadline 约束（`model_invocation.py:710-729`） |

Planning timeout 若由总 Run deadline 触发，StopReason 是 `DEADLINE_EXCEEDED`；若独立 planning cap 先触发，仍建议 `PLANNING_FAILED + error_code=PLANNER_TIMEOUT`，因为 Run 总 deadline 尚未耗尽。两者必须在测试中区分。

### 9.3 Trace/Metric

需要独立 `planner/resolve` Span，作为内部 model attempt span 的 parent。只记录 tracing allowlist 已允许的安全字段（`tracing.py:27-40`）：component、operation、status、error_code、duration、retry_index，并新增或复用受控的 `planning_mode`、`decision_type`、`task_count`、`source`。不得记录 query、instruction、prompt、messages、model output、file path 或 schema failure 原文。

事件顺序：

```text
RUN_STARTED
PLANNING_STARTED
[MODEL_STARTED / MODEL_COMPLETED]
PLAN_CREATED
[POST_PLAN_PRE_EXECUTION checkpoint]
STEP_STARTED ...
```

## 10. Revised Architecture

### 10.1 组件图

```text
/api/chat
  -> CoordinatedRunFactory / RunScope
       |- RunContext + BudgetLedger + Cancellation + EventChannel
       |- AgentRegistry
       |- RunCoordinator.for_dynamic_resolver(...)
       |    -> PLANNING_STARTED
       |    -> PlanResolver
       |         -> deterministic rules OR model planner
       |         -> PlanningDecision
       |         -> PlanCompiler + AgentRegistry
       |         -> ResolvedPlan
       |              |- Plan (safe, immutable, fingerprinted)
       |              `- StepInvocationBindings (raw, run-scoped)
       |    -> freeze once / init Scheduler + Checkpoint
       |    -> PLAN_CREATED
       |    -> ParallelExecutor
       |         -> MultiAgentDriver
       |              -> current-step Binding
       |              -> registered Agent adapter
       |              -> Synthesis dependency result view when applicable
       |              `- return StepResult
       |         -> StepCompletionPipeline
       |              -> StepResultStore.write_once
       |              -> OutputGate
       |              -> AgentStateMachine terminal
       |              -> Runtime Event
       |              `- safe Batch report
       `- terminal: seal Store/Gate; clear Store + Bindings when safe
```

static compatibility：

```text
RunCoordinator.for_static_plan(plan, bindings/driver)
  -> RUN_STARTED
  -> existing PRE_RUN checkpoint
  -> execution
```

不发布 PLANNING events，也不能被 dynamic Resolver 失败路径调用。

### 10.2 原始内容可见性

| 组件 | 用户请求原文 | Planner 原始输出 | specialist 结果原文 | 权限说明 |
|---|---:|---:|---:|---|
| PlanResolver | 是 | 模型分支是 | 否 | 仅规划期，不能日志化 |
| PlanningDecision | instruction 暂存 | 解析后结构 | 否 | transient、repr=False |
| PlanCompiler | 是（为 digest/binding） | 否，只有 typed decision | 否 | 输出安全 Plan + raw bindings |
| ResolvedPlan.plan | 否 | 否 | 否 | 可 fingerprint/snapshot |
| StepInvocationBindings | 当前 Run 的 instruction | 否 | 否 | run-scoped、read-only、clear |
| RunCoordinator | 仅 opaque bindings capability，不读正文 | 否 | 否 | 只消费 Plan/安全错误 |
| AgentRegistry | 否 | 否 | 否 | 静态 metadata/factory |
| MultiAgentDriver | 当前 claim instruction | 否 | synthesis 时仅依赖白名单结果 | 无 Store/Gate 写权限 |
| ParallelExecutor/Pipeline | 不直接读 instruction | 否 | 当前 StepResult | 只验证/提交，不日志化 |
| StepResultStore | 否 | 否 | 是 | dependency-scoped |
| OutputGate | 否 | 否 | 仅授权 final result | at-most-once |
| Synthesis | user request + 显式依赖结果 | 否 | 仅 depends_on | 不得拿全量 Memory/Journal |
| AgentState | 否 | 否 | 否 | 仅状态/安全错误 |
| Snapshot/Journal/Trace/log | 否 | 否 | 否 | 只安全 metadata/digest |

## 11. Revised Acceptance Criteria

Round 1 的 1–42 条继续有效，并做以下修订：

- #6 扩展为“Registry 授权的 explicit-entry specialist 可 `FINAL_PASSTHROUGH`”；单 knowledge 仍必须原样透传。
- #19 采用本轮 StopReason 分层：只新增 PLANNING_FAILED，Synthesis 用 error_code。
- #28 同时覆盖 static constructor 兼容和默认 API deterministic direct 行为。
- #35 的 duplicate publish 包括 Gate `OUTCOME_UNKNOWN`，不能只检查 `published=True`。

新增：

43. `selected_agent=core_router` 的问候/普通聊天编译为合法 Direct Answer，并输出唯一 final。
44. model Planner 返回合法 `DIRECT_ANSWER` 时成功执行 core 单 Step；这与 schema/compile failure 明确区分。
45. Planner/schema/compiler 任一失败均不得静默 fallback 到 core_router 自答。
46. raw instruction/file path/user query 不进入 Plan repr、Snapshot、Journal、RuntimeEvent、Trace 或普通日志；Snapshot 只含允许的 digest/length。
47. `StepInvocationBindings` 只能按当前 claim 读取，Run 终结后清理，crash 后不恢复。
48. provider/model retry 不会写多次 StepResult；Store write count 对每个 logical step 至多 1。
49. retry/duplicate callback 不会产生第二次 `OUTPUT_DELTA`；Gate 状态 PUBLISHING/PUBLISHED/OUTCOME_UNKNOWN 均拒绝重发。
50. Store 有 entry 但 producer Step 未 SUCCEEDED 时，下游读取必须失败。
51. MultiAgentDriver 构造参数和接口中没有 StepResultStore/OutputGate 写 capability。
52. StepExecutionOutcome/ParallelExecutionReport 不携带 raw StepResult，只包含安全 metadata。
53. `OUTPUT_DELTA` journal-after/enqueue-before fault 导致 outcome unknown、Run/transport failure，且不重试发布。
54. 显式 selected Agent 完全绕过模型 Planner，但仍经过 Registry/Compiler 和正式 Run。
55. unknown selected agent 不再由 `agents_config.get(...core_router)` 静默替代。
56. core_router + 明确单/多 Agent 命令优先确定性路由；schema/compile 后只有四种合法 Plan 图。
57. Planner provider 成功但 schema 失败仍计费；未启动 attempt 预算释放。
58. planning 独立 cap 与 Run 总 deadline 的 StopReason/error_code 可区分。
59. static/dynamic Coordinator factories 互斥；dynamic 未 FROZEN 时调用 scheduler/checkpoint/execution guard 必须失败。
60. Plan fingerprint 的任一 `preferred_agent/execution_kind/output_policy/input_digest` 改变都会改变 fingerprint。

## 12. Scope Gates

### 12.1 Core implementation gate — 11–16 人日

只包含真实默认 API 多 Agent闭环不可缺少的工作：

- static Registry 与 entry/passthrough 权限。
- PlanResolver、PlanningDecision、四种形态、PlanCompiler。
- Coordinator dynamic planning lifecycle、一次 freeze、static factory 兼容。
- StepInvocationBindings、StepResultStore。
- MultiAgentDriver 复用现有 Agent 调用且 specialist `persist=False`。
- StepCompletionPipeline、OutputGate 和安全 batch report。
- 单/多 specialist + synthesis、required fail-closed、无 fallback。
- 默认 `/api/chat` direct、knowledge、code、fan-out+synthesis 的真实链路测试。
- 最低安全门：中间结果不进用户流，instruction/result 不进 Journal/log，retry 不重复输出。

此 gate 是“功能闭环完成”，**不是可宣布生产完成**。达到后停止新增 Agent 类型和新图形，转入合同硬化。

### 12.2 Contract-complete MVP+ gate — 累计 18–26 人日

在 Core gate 上增量约 7–10 人日：

- planning/result/output 事件 schema、stream adapter 和最小前端状态兼容。
- PlanStep/PlanSnapshot/fingerprint schema versioning。
- dynamic `POST_PLAN_PRE_EXECUTION` checkpoint 和 Recovery Validation 限制。
- StopReason/error_code 文档和 API/Journal/Snapshot 兼容。
- EventChannel partial publish、cancel/deadline/budget、detached worker 的故障注入。
- Snapshot/Journal/Trace/log/Memory 全边界安全测试。
- Legacy、static Coordinated 和 Stage 2 全量回归；能力矩阵/owner matrix/release gate 文档。

Round 1 的 15–22 人日需要上调：Direct Answer、Bindings 和统一 completion pipeline 是此前遗漏的实际工作。新的 18–26 人日更可信。

### 12.3 停止开发条件

满足 Contract-complete gate 和验收 1–60 后即停止 Stage 2.5：

- 不加入 dynamic registration、recursive delegation、optional edge、dynamic Plan mutation。
- 不持久化/恢复 raw StepResult。
- 不做 multi-round negotiation、完整 DAG UI、per-Agent durable Memory、完整 chaos matrix。
- 不做“形式化零幻觉”承诺。
- 不因 Registry 可容纳更多 Agent 就继续添加插件、租户或治理平台。

## 13. Updated Consensus Matrix

| 议题 | GPT Round 2 立场 | Codex Round 2 立场 | 是否一致 | 当前决策 | 源码依据 |
|---|---|---|---|---|---|
| Plan 生命周期 | 受控 A，正式 Run 内 resolver | 接受 | 是 | dynamic resolver + static compatibility factories | `runtime_factory.py:254-390`; `run_coordinator.py:140-214` |
| PlanResolver 职责 | 返回已验证/编译 immutable result | 接受 | 是 | Resolver 返回 ResolvedPlan；Coordinator 不依赖 Compiler | 同上 |
| Direct Answer | 必须补回第四形态 | 接受并扩展 explicit-entry | 原则一致 | 四种合法形态 | `server.py:603-609`; `planning.py:99-110` |
| Plan/instruction 边界 | 倾向分离 | 选择方案 II | 是 | Plan+digest；raw StepInvocationBindings | `snapshot_contract.py:228-252`; `server.py:785-789` |
| Store owner | RunScope | 接受 | 是 | RunScope owns，Pipeline 唯一写 | `parallel_execution.py:105-107` |
| Driver 写 Store | 反对 | 接受反驳，修正 Round 1 | 是 | Driver 只 return StepResult | `parallel_execution.py:105-107,205-218` |
| OutputGate owner | Executor 调用独立 Gate | 接受 | 原则一致 | CompletionPipeline 调 Gate | `parallel_execution.py:223-236` |
| result commit order | 要求明确 | 已给出 pipeline/失败表 | 待确认 | Store -> Gate -> State -> Step event | `parallel_execution.py:223-236,382-389` |
| partial output publish | 要求 at-most-once | 新增 OUTCOME_UNKNOWN | 待确认 | emit attempt 后永不 retry | `event_channel.py:285-355` |
| fingerprint owner | output/execution 必须进入 | 扩展 PlanStep | 待确认具体载体 | Plan/PlanSnapshot 是唯一事实源 | `plan_fingerprint.py:16-32`; `checkpoint.py:247-248` |
| optional dependency | MVP 不支持 | 接受 | 是 | compile-fail | `scheduler.py:336-390` |
| StopReason | 倾向只加 PLANNING_FAILED | 接受 | 原则一致 | 只新增 PLANNING_FAILED，Synthesis 用 error_code | `state.py:51-63`; `run_coordinator.py:459-467` |
| Memory | raw specialist 不写 | 接受 | 是 | user+唯一 final | `memory_manager.py:155-180` |
| Checkpoint | dynamic post-plan | 接受 | 是 | POST_PLAN_PRE_EXECUTION | `checkpoint.py:203-257` |
| Synthesis failure | fail-closed，无拼接 | 接受 | 是 | UNHANDLED_ERROR + SYNTHESIS_FAILED code | `scheduler.py:336-368` |
| 进入实施 | 共识后 | 当前 NO | 是 | 等待 GPT Round 3/用户授权 | 本文 §14–15 |

## 14. Remaining Disagreements for GPT

### R2-D1（P0）：partial output publish 的最终合同

- **Codex：** Gate 从 NEW 进入 PUBLISHING 后永不重试；emit 异常若无法证明 Journal/queue 均未提交，转 `OUTCOME_UNKNOWN`，Run/transport 失败。
- **依据：** `event_channel.py:285-355` 明确 Journal 成功后 enqueue 仍可能失败且 sequence 不复用。
- **请 GPT 确认：** 是否接受“at-most-once publish attempt，而非 exactly-once delivery”；是否同意用户可能看到 final 文本后又看到/推断 Run 失败是不可消除的 MVP 限制。
- **保守决策：** 不重试，宁可缺失输出也不重复/混合输出。

### R2-D2（P1）：Fingerprint 载体

- **Codex：** 扩展 PlanStep，preferred_agent 继续作为 canonical agent id；新增 execution_kind/output_policy/input_digest，升级 snapshot/fingerprint schema。
- **依据：** Plan 已被 Scheduler、Checkpoint、Recovery 作为唯一静态事实源；另建 CompiledPlan 会重复 step/dependency/agent。
- **请 GPT 确认：** 是否接受方案 A，以及 v1 snapshot 仅验证、不具备 v2 multi-agent resume 资格。
- **保守决策：** 单 Plan 事实源。

### R2-D3（P1）：StopReason

- **Codex：** 只新增 PLANNING_FAILED；专业/Synthesis/required blocked 均用 UNHANDLED_ERROR + 细分 error_code。
- **依据：** 当前没有 execution generic reason，新增任何 enum 都影响 Event/Snapshot/Reducer/tests；当前 Step FAILED 已映射 UNHANDLED_ERROR。
- **请 GPT 确认：** required blocked 是否同意使用 `UNHANDLED_ERROR + REQUIRED_DEPENDENCY_FAILED`，而不是现有 NO_ACTION。
- **保守决策：** 不增加 Synthesis/Agent 专属 StopReason。

### R2-D4（P1）：explicit-entry specialist passthrough 权限

- **Codex：** 为兼容当前显式 Agent chat，knowledge/code/data 在 `selected_agent` 明确选择时允许 passthrough；同一 Agent 被 core_router 委派时可以使用不同的 delegated default（code/data -> synthesis）。
- **依据：** 当前 API 直接接受任意 agent_id 并单 Step 输出；若全部强制 synthesis 会破坏现有 Coordinated 单 Agent 行为。
- **请 GPT 确认：** Registry 是否应区分 `entry_output_policy` 与 `delegated_output_policy`，还是所有上下文只有一个 `allows_final_passthrough`。
- **保守决策：** 区分调用上下文，权限仍由 Registry/Compiler 决定，不由 Planner 决定。

### R2-D5（P1）：默认 API 是否全部经过 Resolver

- **Codex：** 是。显式 non-core 只是 Resolver 的确定性分支，不调用模型；static factory 仅保留内部兼容。
- **依据：** 否则 unknown selected agent、planning events 和 Registry 校验会在 API 中出现两套生命周期。
- **请 GPT 确认：** 是否接受 selected specialist 也发布 `PLANNING_STARTED/PLAN_CREATED`（deterministic、零 model call）。
- **保守决策：** 默认 API 单一 resolver 入口，避免双行为。

## 15. Final Codex Position

```text
Codex recommendation:
- Plan lifecycle: 正式 Run 内受控方案 A；PlanResolver 负责业务规划/编译，Coordinator 只管理事件、一次冻结和执行初始化
- Direct answer path: 增加合法 core_router Direct Answer；默认 API 全部经过 Resolver，显式 Agent 走确定性分支而非模型 Planner
- Planning contract: PlanningDecision 是 DirectAnswerDecision | DelegatedPlanDecision；Resolver 返回已验证的 ResolvedPlan
- Invocation data boundary: raw instruction 不进入 Plan；保存在 run-scoped、read-only、repr-safe 的 StepInvocationBindings，Run 后清理且不恢复
- Result commit owner: MultiAgentDriver 只返回 StepResult；ParallelExecutor 调用的 StepCompletionPipeline 是 Store 写入、状态终结和安全 report 的唯一 owner
- Output owner: OutputGate 独占授权和 at-most-once 状态；PUBLISHING 后异常进入 OUTCOME_UNKNOWN，禁止重试
- Fingerprint contract: 扩展现有 PlanStep/PlanSnapshot，preferred_agent + execution_kind + output_policy + input_digest 共同参与 fingerprint；不新增 CompiledPlan 双事实源
- StopReason: 只新增 PLANNING_FAILED；专业 Agent、Synthesis 和 required dependency failure 复用 UNHANDLED_ERROR，以稳定 error_code 区分
- Dependency failure: MVP 全 required；失败使 Synthesis BLOCKED，Run fail-closed，无 core_router 或拼接 fallback
- Checkpoint: dynamic 首个 checkpoint 为 POST_PLAN_PRE_EXECUTION；static 保留 PRE_RUN；planning failure 无 snapshot
- Memory: specialist raw result 不写 Memory；只持久化原始用户消息和唯一 final answer
- MVP boundary: 四种扁平 Plan、静态 Registry、真实并行、单点 Synthesis；不做动态注册/递归/optional/动态 Plan/结果恢复/多轮协商/完整 DAG UI/零幻觉保证
- Core implementation gate: 11–16 人日，仅完成功能闭环和最低安全门，不作为生产完成声明
- Contract-complete gate: 累计 18–26 人日，补齐事件、fingerprint/snapshot/recovery、安全、故障注入和完整回归后停止 Stage 2.5
- Ready for implementation: NO
```

阻塞实施的 P0/P1：R2-D1 partial output publish 合同、R2-D2 fingerprint 载体、R2-D3 StopReason 映射、R2-D4 explicit-entry passthrough 权限、R2-D5 默认 API 单 Resolver 入口尚待 GPT 确认；同时尚无用户实施授权。
