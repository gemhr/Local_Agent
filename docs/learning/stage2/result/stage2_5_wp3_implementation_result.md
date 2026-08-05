# LocalAgent Stage 2.5 Multi-Agent WP3 Implementation Result

## 1. Executive Summary

WP3 已完成。多 Step Plan 现在通过真实主链执行：

```text
Frozen Plan
  -> Scheduler 并行 claim
  -> MultiAgentDriver
  -> AgentAdapterFactory
  -> specialist Agent
  -> typed StepResult
  -> StepResultStore
  -> dependency-scoped result view
  -> synthesis_agent
```

- 真实多 Agent 是否执行：是。Shape 2（`code_expert -> synthesis`）与 Shape 3（`knowledge_expert + code_expert -> synthesis`）的 specialist 均真实调用一次，synthesis 真实调用一次。
- Specialist 是否并行：是。测试用两方 barrier、共享 active 计数和事件顺序证明两个 specialist 执行区间重叠，synthesis 只在全部 specialist SUCCEEDED 且结果 READABLE 后开始。
- Synthesis 是否执行：是，且恰好一次。
- 是否有用户可见多 Agent final：否。INTERNAL 与 synthesis 均不产生 `OUTPUT_DELTA`；多 Step Run 在所有 Step 成功且唯一 final StepResult READABLE 后命中 WP4 前临时保护 `FINAL_OUTPUT_PIPELINE_NOT_READY`，Run 以 `FAILED / UNHANDLED_ERROR` 结束。
- 是否触及 WP4：否。未实现 OutputGate、DeliveryStatus、delivery 重试、partial publication 修复或最终回答写 Memory。
- 测试总结：WP3 专项 98 passed；WP1+WP2 关键回归 204 passed, 13 subtests passed；全仓 `1265 passed, 42 subtests passed`；`compileall` 与 `git diff --check` 通过（仅 Git LF/CRLF 提示，无 whitespace error）。

## 2. Source Audit Before Changes

以下为变更前对真实源码的审计，不是从架构文档反推：

| 审计项 | 变更前真实事实 |
| --- | --- |
| Agent 统一入口 | `AgentRouter.complete_single_agent`（`core/agent_router.py:1499`）走 `_run_agent_once`（`:1452`），已有 `persist: bool = True` 参数；`memory_manager.add_message` 仅在 `if persist:` 分支内调用；`history_scope` 默认 `DIRECT_MEMORY_SCOPE`；返回 `str`，不会返回 Generator/async iterator。 |
| 单 Step Driver | `ResolvedSingleStepDriver`（`core/runtime/runtime_factory.py:85-123`）要求 frozen Plan 恰好一个 Step，按 claim 读取 Binding 后调用 `complete_single_agent`，保留旧 `OUTPUT_DELTA` 行为；不创建 AdapterFactory/Store/OutputGate。 |
| ParallelExecutor | `_invoke` 在 `SYNC_BLOCKING` 模式下经 bounded blocking executor 执行同步 driver；`worker` 在 `driver.emits_user_output and isinstance(result, str)` 时发布 `OUTPUT_DELTA`，随后 `_terminal` 提交 Step 终态并发布 `STEP_COMPLETED`；`StepExecutionOutcome.result` 持有 raw 返回值。 |
| Batch report | `ParallelExecutionReport` 按 claim 顺序聚合 outcome；`RunCoordinator._execute_batches` 之前 `await self._executor_task` 后直接丢弃报告。 |
| Scheduler | `claim_ready` 在同一批内按稳定拓扑顺序 claim 不超过 `max_parallelism` 的全部 ready Step；`_propagate_blocked` 在依赖为 FAILED/CANCELLED/BLOCKED/SKIPPED 时将 PENDING 依赖者收敛为 BLOCKED。 |
| max concurrency | Factory 构造 `ParallelExecutionPolicy(max_concurrency=1)`；effective concurrency 为 `min(policy.max_concurrency, budget.max_concurrency)`。 |
| WP2 临时 gate | `RunCoordinator._multi_step_execution_not_ready`（旧 `run_coordinator.py:500` 附近）在 `step_count>1` 或存在 `SYNTHESIS`/`INTERNAL` 时于任何 Step 前以 `MULTI_AGENT_EXECUTION_NOT_READY` fail closed，含 WP3 removal marker。 |
| cleanup owner | Invocation Bindings 由 `RunCoordinator._clear_invocation_bindings` 在 terminal cleanup 中 `close_and_clear()`；Channel/transport 由 `CoordinatedRunScope` 持有；Activity tracker 由 RunContext 持有。 |
| supports_parallel | Registry 字段存在；`PlanCompiler` 已对 `len(specialists)>1 and any(not supports_parallel)` 拒绝 fan-out；Runtime 此前不使用该字段。 |
| Planning 与执行资源 | Planning 和执行共享 `process_blocking_executor`（4 workers / 8 pending）；`PLANNING_MODEL` 仅是 task kind 标签，没有独立或保底容量；Planner timeout 由 `asyncio.wait_for` 包住 `handle.result_async()`，因此包含排队时间；已有 `runtime_blocking_executor_wait_seconds` 直方图与 `runtime_blocking_executor_pending` 仪表。 |

## 3. Files Changed

### 生产代码

| 文件 | WP3 职责 |
| --- | --- |
| `core/runtime/step_result.py` | 新增 typed `StepResult` / `ResultContentType`（TEXT/MARKDOWN）与安全校验；非 dataclass、安全 repr、不可 pickle。 |
| `core/runtime/step_result_store.py` | 新增 Run-scoped `StepResultStore`：PREPARED/READABLE 条目状态、SEALED/CLEARED 存储级状态、once-write、容量限制、依赖 ACL、`DependencyResultView`、幂等清理。 |
| `core/runtime/agent_adapter_factory.py` | 新增 process-scoped 不可变 `AgentAdapterFactory`、`AgentExecutionAdapter` Protocol、raw-bearing `AgentExecutionRequest`/`AgentAdapterResult`、通用 `AgentRouterSingleAgentAdapter`。 |
| `core/runtime/step_completion.py` | 新增最小结果提交骨架 `StepResultCommitter` 与安全 `StepCompletionResult`；只实现 result/state 分支，不含 OutputGate/delivery。 |
| `core/runtime/multi_agent_driver.py` | 新增 `MultiAgentDriver`：claim -> Plan -> Binding -> Registry -> AdapterFactory -> adapter -> `StepResult`；不写 Store/Gate/State，不发布事件与用户文本。 |
| `core/runtime/synthesis.py` | 新增 `SynthesisAgentAdapter`：只消费依赖视图，严格 Prompt，缺 required 结果不调用模型。 |
| `core/runtime/parallel_execution.py` | ParallelExecutor 增加 typed mode：注入 completion owner 后不发布 `OUTPUT_DELTA`，raw `StepResult` 不进 outcome/report，报告携带安全完成 metadata；`_cancel_unfinished` 跳过已终态 Step。 |
| `core/runtime/run_coordinator.py` | 删除 WP2 gate；freeze 后按 Plan 形态初始化 typed runtime；消费 batch report 中的提交失败；required-dependency fail-closed 映射；final-output WP4 前临时保护；Store seal/clear 生命周期；`user_request` 与 `attach_multi_agent_runtime`。 |
| `core/runtime/runtime_factory.py` | 应用级 `AgentAdapterFactory` 装配；默认 `max_concurrency=2`；Store 限额配置；动态 Run 注入 `MultiAgentDriver`。 |
| `core/runtime/__init__.py` | 导出 WP3 稳定合同。 |

### 测试与 fixture

- 新增 `tests/_wp3_fixtures.py`（共享 planner JSON 与记录型 router）。
- 新增 `tests/test_step_result.py`、`tests/test_step_result_store.py`、`tests/test_agent_adapter_factory.py`、`tests/test_multi_agent_driver.py`、`tests/test_synthesis_adapter.py`、`tests/test_step_completion.py`、`tests/test_multi_agent_execution.py`、`tests/test_step_result_security.py`。
- 修改 `tests/test_dynamic_planning_lifecycle.py`：原 WP2 gate 测试原地更新为 WP3 行为断言（specialist/synthesis 真实执行、`FINAL_OUTPUT_PIPELINE_NOT_READY`、Store 最终清理），未删除原测试。

## 4. Agent Adapter Contract

### Factory

```text
agent_id -> AgentRegistry.resolve -> execution_adapter_id
         -> AgentAdapterFactory.resolve -> AgentExecutionAdapter
```

- process-scoped、不可变；构造期校验 Registry 所有 enabled Agent 的 `execution_adapter_id` 可解析，否则 `ADAPTER_NOT_RESOLVABLE` fail closed。
- 稳定 adapter ID：`core_router_adapter`、`data_analyst_adapter`、`code_expert_adapter`、`knowledge_expert_adapter` 使用同一个 `AgentRouterSingleAgentAdapter` 类（Factory 配置，而非 Driver 条件分支）；`synthesis_agent_adapter` 使用 `SynthesisAgentAdapter`。
- unknown adapter ID 明确失败（`UNKNOWN_ADAPTER`）；重复 ID 构造失败（`DUPLICATE_ADAPTER`）。
- Factory/adapter 不保存用户请求、Run 状态或结果；adapter 实例不持有 Run-scoped raw 数据。

### Protocol 与生命周期

```python
class AgentExecutionAdapter(Protocol):
    def execute(self, request: AgentExecutionRequest, run_context: RunContext) -> AgentAdapterResult: ...
```

实现说明：具体生产 adapter 采用同步 `execute`，因为现有统一入口 `AgentRouter.complete_single_agent` 是同步合同，且 ParallelExecutor 在 `SYNC_BLOCKING` 模式下将其放入 bounded blocking executor 线程执行；这复用既有 Model/Tool/Retrieval 事件与 Budget/Circuit/Retry 语义，不新建第二套线程池。

`AgentExecutionRequest` 字段：`step_id`、`agent_id`、`instruction`、`execution_kind`、`input_type`、`capability_requirements`、`content_type`、`dependency_results`（仅 synthesis）、`event_emitter`、`fault_controller`。request 不是 dataclass，`asdict()` 直接拒绝；`repr` 对 instruction 与 dependency 正文脱敏；不可 pickle；不进入异常字符串。

`AgentAdapterResult` 只存在于 Driver 调用栈，随后由 Driver 转换为 `StepResult`。

`AgentRouterSingleAgentAdapter` 要求：不按 Agent ID 分支；`persist=False`；不写 orchestration Memory；不调用 Legacy Delegate；不允许内部再委派；不产生 `[[ORCH]]`；不负责 Store/Step 状态/OutputPolicy。

## 5. StepResult Contract

```python
class ResultContentType(str, Enum):
    TEXT = "TEXT"
    MARKDOWN = "MARKDOWN"

class StepResult:
    step_id: str
    producer_agent_id: str
    content_type: ResultContentType
    content: str
    complete: bool
```

- 非 dataclass、不可变（slots + lock）；`repr` 显示 `content=<redacted>`，不泄露正文；`__getstate__` 抛 `TypeError`（不可 pickle）；`dataclasses.asdict` 直接拒绝。
- `content` 必须是非空有限字符串，禁止 `Any`/`bytes`/文件对象；允许构造期 `max_content_chars` 上限。
- `complete=False` 在 MVP 中不允许提交成功（committer 拒绝）。
- producer/step 必须与 claim/Plan 一致（committer 与 Store 双重校验）。
- 不允许调用方伪造 length/digest metadata：`char_count` 是只读派生属性。
- raw content 不进入 Runtime Event、Journal、Snapshot、Trace、普通日志、`StepExecutionOutcome` raw 字段。
- 说明：Registry 原有 `ResultContentType(TEXT/STRUCTURED)` 与运行期 `ResultContentType(TEXT/MARKDOWN)` 是两个独立枚举；Driver 将 Registry 的 `TEXT` 映射为运行期 `TEXT`、`STRUCTURED` 映射为 `MARKDOWN`。默认注册 Agent 均产出 `TEXT`。

## 6. StepResultStore Contract

- 所有权：每个 Dynamic Run 一个 Store；由 Coordinator 生命周期拥有；只有 `StepResultCommitter` 可写；Driver/Adapter 无写权限；Synthesis 只经依赖 ACL 读取。
- 状态机：条目 `PREPARED -> READABLE`；Store 级 `OPEN -> SEALED -> CLEARED`；clear 后拒绝读写。
- 写入：`write_prepared(entry, expected_agent_id)` 校验 producer 属于冻结 Plan、identity 严格匹配、once-write、单结果大小、Run 总字符、条目数上限；不静默截断，容量失败用稳定 `CAPACITY_EXCEEDED`。`mark_readable(step_id, agent_state)` 仅在 producer Step 为 `SUCCEEDED` 后放行。
- 读取 ACL：`dependency_view_for(consumer_claim, agent_state)` 同时验证 consumer 已获 Scheduler claim（plan_id/step/agent 一致）、producer 在 compiled `depends_on` 中、producer StepState==SUCCEEDED、entry==READABLE、Store 未 seal、producer identity 匹配；按 `depends_on` 稳定顺序返回只读 `DependencyResultView`。无 `get_all()`、无按 Agent 查询、无任意扫描、无返回正文的调试接口。
- 生命周期：正常终态 `seal -> clear`；异常/取消立即 seal 拒绝迟到写入与读取，在无存活 worker 的安全点 clear；`clear()` 幂等。
- 默认限额：单结果 20_000 字符、Run 总 60_000 字符、条目 16（可由 Factory 配置）。

## 7. Result Commit Contract

`StepResultCommitter` 是最小结果提交骨架（`core/runtime/step_completion.py`），文档明确：

> WP3 只实现 result/state 分支；WP4 才实现 OutputGate 和 delivery 分支。

### INTERNAL / Synthesis 提交顺序

```text
Driver returns StepResult
-> acquire completion guard
-> validate claim/result
-> Store.write_prepared
-> Step RUNNING -> SUCCEEDED
-> Store.mark_readable
-> STEP_COMPLETED(SUCCEEDED)
-> safe StepCompletionResult (synthesis: final_result_ready=True)
```

Synthesis final Step 使用相同顺序，但：不调用 OutputGate、不发布 `OUTPUT_DELTA`；Coordinator 看到所有 Step 成功且 final result READABLE 后返回临时 `FINAL_OUTPUT_PIPELINE_NOT_READY`（带 WP4 removal marker）。

### 提交失败映射

| 失败点 | Step 状态 | Store | Run error |
| --- | --- | --- | --- |
| Driver 失败 | FAILED | 无 entry | AGENT_STEP_FAILED / SYNTHESIS_FAILED |
| result 非法 | FAILED | 无 entry | STEP_RESULT_INVALID |
| prepare 失败（含容量） | FAILED | 无可读 entry | STEP_RESULT_PREPARE_FAILED |
| Step 成功状态提交失败 | RUNNING，终态时 settle | PREPARED 不可读 | STEP_STATE_COMMIT_FAILED |
| mark readable 失败 | SUCCEEDED | PREPARED 不可读 | STEP_RESULT_COMMIT_FAILED |
| STEP_COMPLETED 事件失败 | 已 terminal | 按实际状态（READABLE） | STEP_COMPLETION_EVENT_FAILED |

额外稳定码（表格之外的补充，均 fail closed）：`STEP_RESULT_DUPLICATE_COMMIT`（重复 completion 回调/重复写入）、`STEP_RESULT_LATE_COMMIT`（Store 已 seal/clear 的迟到结果）。

Coordinator 在每次 batch 后、下一次 Scheduler 成功判断前消费安全 `StepCompletionResult` 中的提交失败。

## 8. MultiAgentDriver

真实调用链：

```text
receive StepClaim
-> resolve frozen PlanStep
-> bindings.resolve_for_step(step_id, expected_agent_id)
-> AgentRegistry.resolve(agent_id)
-> execution_adapter_id
-> AgentAdapterFactory.resolve(adapter_id)
-> build AgentExecutionRequest
-> if synthesis: attach dependency-scoped result view
-> adapter.execute
-> convert AgentAdapterResult to StepResult
-> return StepResult
```

- claim 是唯一执行授权；PlanStep/Binding/Registry 三方 Agent 必须一致，否则 `BINDING_MISMATCH`/`REGISTRY_MISMATCH` fail closed。
- 不按 Agent 名分支（源码级测试断言无 `agent_id ==` 或具体 Agent 字符串）；多个 adapter ID 共用同一 adapter class 时由 Factory 配置。
- Driver 无 Store/Gate 写权限、不改 AgentState、不发布 `STEP_STARTED/STEP_COMPLETED`、不发布用户文本、不持久化 Memory；`emits_user_output=False`。
- 继承同一 RunContext/Budget/Deadline/Cancellation；adapter/result mismatch fail closed。
- 迟到结果：Store 已 seal 时 committer 以 `STEP_RESULT_LATE_COMMIT` 拒绝。

## 9. Synthesis Contract

### 输入白名单

仅包含：

- 原始 user request（Coordinator 动态 Run 提供）或 synthesis Binding 指令；
- 当前 synthesis Step 的显式 `depends_on`；
- 每个依赖的 `step_id`、`producer_agent_id`、`content_type`、`complete`、`content`。

排序使用 compiled `depends_on` 顺序（Compiler 已按稳定 task ID 排序）。不得包含全量 Store、`get_all()`、全量 Memory、Journal、Trace、Snapshot、未执行 Agent、未依赖 Step、Planner raw output、specialist 异常详情、文件系统自动读取或再委派能力。

### Prompt

Prompt 明确要求：只能根据提供的专家结果回答；不得声称调用了未列出的 Agent；不得把缺失内容描述为已确认事实；多结果冲突时显式指出冲突；区分专家事实、推断和建议；不得输出内部 step ID/系统合同/Runtime metadata（除非用户明确要求）；不得再次规划或委派；不得调用 Memory、Retrieval 或 Tool。

> 输入白名单可以限制来源，但不能形式化保证模型绝不幻觉。

### 缺失结果

调用 synthesis model 前由 Store ACL 验证：entry 存在、READABLE、producer Step SUCCEEDED、consumer 显式依赖 producer、Store 未 sealed；`SynthesisAgentAdapter` 再验证 result `complete` 与 content_type 接受性。任一失败：不调用模型（model call=0）、使用安全 error code、不拼接已有结果、不回退 Core。

## 10. Parallel Execution Evidence

Shape 3 测试（`tests/test_multi_agent_execution.py::test_shape3_specialists_overlap_and_synthesis_waits`）用事件而非单纯总耗时证明：

- 两方 `threading.Barrier(2)` 在 `complete_single_agent` 内等待：若执行是顺序的，第一个 specialist 会阻塞到 barrier 超时并失败；测试通过说明两个 specialist 同时位于执行入口。
- 共享 active 计数达到 `max_active >= 2`。
- 事件顺序断言：两个 specialist 的 enter 都在任一 specialist exit 之前；synthesis 的 enter 在两个 specialist exit 之后（结果 READABLE 后）。
- 两个结果分别 once-write；synthesis 恰好一次；无中间输出、无最终输出；Run 以 `FINAL_OUTPUT_PIPELINE_NOT_READY` 临时安全失败；无 Core fallback。

并发上限复用现有 `ParallelExecutionPolicy.max_concurrency`（Factory 默认 2，effective 为 `min(policy, budget.max_concurrency)`）与 Budget/deadline/cancellation。Registry `supports_parallel=False` 的 fan-out 已由 Compiler 在编译期拒绝；typed mode 为每个 Step 分配独立 resource key，避免默认共享 key（limit=1）把独立 specialist 串行化，全局 `max_concurrency` 仍约束批次容量。

## 11. WP2 Gate Removal

WP2 的 `MULTI_AGENT_EXECUTION_NOT_READY` gate（`_multi_step_execution_not_ready`，含 WP3 removal marker）已在同一变更中随以下能力全部可用后原子删除：

- MultiAgentDriver；AgentAdapterFactory；StepResultStore；result commit（StepResultCommitter）；INTERNAL 无输出；synthesis dependency view；required fail-closed；final-output not-ready 临时保护。

删除后新增/更新测试断言：不再出现 `MULTI_AGENT_EXECUTION_NOT_READY`；specialist 真实开始；synthesis 真实执行；仍无 `OUTPUT_DELTA`；最终以 `FINAL_OUTPUT_PIPELINE_NOT_READY` 失败。原 WP2 gate 测试原地更新为上述新断言，未删除。

## 12. Event and Output Boundary

多 Step 真实事件序列（E2E 验证）：

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

- 所有 multi-step Step 均无 `OUTPUT_DELTA`：Executor typed mode 显式跳过字符串输出逻辑（不依赖 `isinstance(result, str)` 隐式技巧）。
- 单 Step（Core direct、explicit entry、单 delegated knowledge direct）继续走 `ResolvedSingleStepDriver` 旧字符串输出，行为未变。
- `STEP_COMPLETED` 由 committer 发布（state 已提交后），并携带 status/safe_error_code/duration_ms。
- Batch report 只含安全完成 metadata（`StepCompletionResult`），Coordinator 不再丢弃 multi-step report。

## 13. Security Boundary

安全测试使用 `SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST` 与 `\\internal\private\file.dat` 标记，断言其不出现于：

- Runtime Event / Journal（safe_payload 与 repr）；
- Snapshot；
- Trace（span attributes 与 repr）；
- structured log；
- Batch report / RunCoordinatorResult / Store repr / 异常字符串；
- Memory（specialist 与 synthesis 调用均 `persist=False`，且统一入口的 memory 写入由 `if persist:` 守卫）。

允许出现的位置仅限：`StepResult`、Store 内存、dependency result view、Synthesis model 输入、synthesis Adapter 调用栈。WP3 中 synthesis final result 也不得进入用户文本或 Memory（无 `OUTPUT_DELTA`，且不写 Memory）。

## 14. Failure Mapping

| 场景 | Step 状态 | Run status | StopReason | error_code |
| --- | --- | --- | --- | --- |
| specialist Driver 失败 | FAILED | FAILED | UNHANDLED_ERROR | AGENT_STEP_FAILED |
| synthesis Driver 失败 | FAILED | FAILED | UNHANDLED_ERROR | SYNTHESIS_FAILED |
| result 非法 / identity 不匹配 | FAILED | FAILED | UNHANDLED_ERROR | STEP_RESULT_INVALID |
| prepare 失败（含容量/过大） | FAILED | FAILED | UNHANDLED_ERROR | STEP_RESULT_PREPARE_FAILED |
| Step 成功状态提交失败 | RUNNING（终态 settle） | FAILED | UNHANDLED_ERROR | STEP_STATE_COMMIT_FAILED |
| mark readable 失败 | SUCCEEDED | FAILED | UNHANDLED_ERROR | STEP_RESULT_COMMIT_FAILED |
| STEP_COMPLETED 事件失败 | 已 terminal | FAILED | UNHANDLED_ERROR | STEP_COMPLETION_EVENT_FAILED |
| 重复 completion / 迟到结果 | 按实际状态 | FAILED | UNHANDLED_ERROR | STEP_RESULT_DUPLICATE_COMMIT / STEP_RESULT_LATE_COMMIT |
| producer 已知失败导致 synthesis BLOCKED | 失败者 FAILED、synthesis BLOCKED | FAILED | UNHANDLED_ERROR | REQUIRED_DEPENDENCY_FAILED |
| 所有 Step 成功且 final READABLE（WP4 前） | 全部 SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_PIPELINE_NOT_READY |

取消沿用既有 Run/Step 语义（RunStatus.CANCELLED、无 cancellation StopReason）；deadline 沿用 `DEADLINE_EXCEEDED`；budget 沿用 `BUDGET_EXHAUSTED`。所有失败路径均无 Core fallback、无局部结果输出、无拼接。

## 15. Tests and Commands

### 最终验证

| 命令 | 结果 |
| --- | --- |
| `uv run pytest -q tests/test_step_result.py tests/test_step_result_store.py tests/test_agent_adapter_factory.py tests/test_multi_agent_driver.py tests/test_synthesis_adapter.py tests/test_step_completion.py tests/test_multi_agent_execution.py tests/test_step_result_security.py tests/test_dynamic_planning_lifecycle.py` | 98 passed |
| WP1+WP2 关键回归（registry/bindings/compiler/planning/plan_graph/scheduler/parallel_execution/factory/coordinator/fingerprint/snapshot/recovery/event/metrics 组合） | 204 passed, 13 subtests passed |
| `uv run pytest -q` | 1265 passed, 42 subtests passed |
| `uv run python -m compileall -q core tests server.py main.py` | PASS |
| `git diff --check` | PASS（仅 Git LF/CRLF 提示，无 whitespace error） |

### 实施中失败与修复

1. 首次接线后 `RunCoordinator` 回归失败：`ParallelExecutor.execute_ready` 尚未接收 `completion_owner` 参数；补上参数并透传后 64 passed。
2. Shape 3 并行证据失败：默认 resource key（limit=1）把两个 specialist 串行化；typed mode 改为每 Step 独立 resource key，同时保留全局 max concurrency。
3. WP2 gate 测试断言旧错误码：原地更新为 WP3 行为断言（`FINAL_OUTPUT_PIPELINE_NOT_READY`、specialist/synthesis 真实执行、Store 最终清理）。
4. 测试侧问题：`threading.Event.wait` 阻塞事件循环导致取消用例不启动；改为 `asyncio.to_thread`。Windows 上 `time.monotonic()` 粒度约 15ms，并行证据改用 barrier + active 计数 + 事件顺序。`channel.abort()` 未 await 导致事件失败用例不生效；改为 `await`。Store 限额测试参数与构造约束冲突、char_count 手误等均已修正。
5. 调试用 traceback 打印已移除；无残留调试代码。

## 16. Compatibility and Regression

- WP1：Registry/Bindings/Compiler/planning/parser 全部测试通过（关键回归组 204 passed 含 WP1 四文件）。
- WP2：dynamic lifecycle、planning adapter、fingerprint/snapshot/recovery、coordinator/event/metrics 全部通过。
- Core direct、explicit entry、单 delegated knowledge direct：继续走 `ResolvedSingleStepDriver` 与旧字符串输出，相关 lifecycle/E2E 测试通过。
- Legacy：显式 LEGACY selector 未创建 Coordinated scope，Legacy 测试通过。
- static Coordinated：公开兼容构造未变，static 无 Planning 事件路径通过。
- Streaming/Event：多 Step 无 `OUTPUT_DELTA`；单 Step 事件顺序未变；stream adapter 测试通过。
- Snapshot/Recovery：Plan snapshot 合同未变；Store/Bindings 不持久化、不恢复；recovery 测试通过。
- Cancellation/Shutdown：Run 取消、deadline、budget 映射沿用既有语义；相关测试通过。
- 全仓 `1265 passed, 42 subtests passed`。

## 17. Deviations from Consensus

No deviations from the Stage 2.5 architecture consensus were introduced in WP3.

实现层面的两个说明（不改变架构语义）：

1. 最小 result completion 骨架（`StepResultCommitter`）属于 WP3 允许范围；文档明确 WP4 才扩展 OutputGate/delivery 分支。
2. 具体 `AgentExecutionAdapter.execute` 为同步合同，因为现有统一 Agent 入口是同步且由 bounded executor 承载；这不改变“typed adapter”语义。

## 18. Known Limitations After WP3

- 多 Agent 内部执行已实现，但用户可见多 Agent final 仍未实现。
- 多 Step Run 暂时以 `FINAL_OUTPUT_PIPELINE_NOT_READY` 失败（WP4 前施工边界，不是最终共识中的正式业务错误）。
- 无 OutputGate、无 DeliveryStatus、无 partial publication 处理；final Memory 未写。
- Store/Bindings 不恢复；进程中断后动态 Run fail closed。
- specialist 调用沿用 `complete_single_agent` 的 history 加载行为（按 Agent scope 读取各自历史），完整“specialist 不读取 Memory”边界属于 WP5 范围。
- P2/已知容量风险：Planning 与 specialist 执行共享同一 bounded executor（4 workers/8 pending），`PLANNING_MODEL` 无独立或保底容量；阻塞的 specialist 任务可能耗尽 worker 使 Planning 排队。已有可观测证据：`runtime_blocking_executor_pending` gauge 与 `runtime_blocking_executor_wait_seconds` histogram；Planner timeout 已包含排队时间。按 WP3 边界未新建第二线程池。
- 不能宣称 Stage 2.5 完成。

## 19. WP4 Interface Needs

WP4 将消费以下已有产物（本 WP3 未实现 WP4）：

- final StepResult READABLE（Store `has_readable(final_step_id)`）。
- safe completion report（`StepCompletionResult`，含 `final_result_ready`）。
- OutputPolicy（INTERNAL/FINAL_PASSTHROUGH/FINAL_SYNTHESIS）。
- final Step contract（唯一非 INTERNAL Step）。
- Coordinator report inspection（`_decision_from_batch_report` 的扩展点）。
- StepEmitter/EventChannel（STEP_COMPLETED 后的序列状态）。
- 临时 final-output gate 删除点（`run_coordinator.py` 中 `FINAL_OUTPUT_PIPELINE_NOT_READY` 的 WP4 REMOVAL MARKER）。

## 20. Final Status

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

`Ready to start WP4: YES` 依据（全部满足）：

- shape 2/3 真实执行；
- specialist 并行证据通过（barrier + active 计数 + 事件顺序）；
- Store once-write/ACL/容量/清理通过；
- Driver 无 Store/Gate 写权；
- specialist persist=False；
- synthesis 只读依赖；
- required 失败不调用 synthesis；
- 无 INTERNAL 泄漏；
- 无 multi-step final 输出；
- WP2 gate 原子删除；
- final-output 临时保护存在；
- P0=0、P1=0；
- 全仓回归通过；
- 无未批准架构偏差。

P2=1 为 Planning 饥饿容量风险（见第 18 节），不影响 WP4 准入条件（仅要求 P0/P1 为 0）。
