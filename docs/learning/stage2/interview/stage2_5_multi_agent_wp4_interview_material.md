# LocalAgent Stage 2.5 Multi-Agent WP4 面试材料

> 适用范围：Stage 2.5 Multi-Agent WP4——完成唯一最终输出链：typed StepResult -> Store PREPARED -> Step SUCCEEDED -> Store READABLE -> OutputGate at-most-once publish -> STEP_COMPLETED(SUCCEEDED) -> safe completion report -> Coordinator 先消费 delivery -> Run SUCCEEDED 或显式 delivery failure。
>
> 真实性声明：本文中的“真实发现”仅指本地项目源码审查、实施或测试中实际观察到的问题，不等同于线上生产事故；“假设构造”只用于风险推演，不描述成真实事故。

## 1. 推荐的面试材料模板

一份可信、便于追问的工程面试材料应包含：

1. 一句话项目定义：先说明解决的用户问题，再介绍技术方案。
2. 真实性与边界：区分用户真实复现、源码审查发现、实施测试发现和假设构造。
3. 原始用户场景：给出真实输入、预期链路、实际链路和风险。
4. 架构演进：说明旧架构为什么无法满足要求，以及 WP1、WP2、WP3、WP4 分别解决什么。
5. 方案讨论：展示候选方案、取舍标准和被拒绝方案。
6. 核心状态机与时序：说明成功、失败、取消、超时的确定性行为。
7. 数据与权限边界：谁有权决定 Agent、instruction、Plan、结果写入和最终输出。
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

WP4 的目标是把 WP3 的“执行成功但用户可见交付未备”状态彻底收尾：默认 Dynamic Coordinated 的 Shape 0～3 全部统一为 typed completion pipeline + OutputGate，交付结果与 Step 执行成功分层，只有确认 DELIVERED 的 final 才写入 Memory，并修复 EventChannel partial publication 下的 Step sequence 重用问题。

WP4 完成后的诚实表述：默认 Coordinated 多 Agent 可向用户输出唯一 final answer；delivery failure 不再伪装成 Agent failure；但 WP5 的完整 Trace/Journal/前端安全审计尚未完成，Stage 2.5 仍不能宣称全部完成。
## 3. 真实用户场景与问题价值

用户在主 Agent 入口输入：

```text
分别让代码专家和知识专家审查这个模块，然后综合两份结论给出最终建议
```

用户期望：

```text
主入口 -> 识别 code + knowledge 两个任务
-> 两个 specialist 并行执行
-> 两份 INTERNAL 结果只在本 Run 内传递
-> synthesis 基于两份结果生成唯一 final answer
-> 唯一 OUTPUT_DELTA 交付给用户
```

WP3 之后的真实状态：多 Agent 内部执行已可用（specialist 并行、Store once-write、synthesis 恰好一次），但所有 multi-step Step 都不产生 `OUTPUT_DELTA`，当所有 Step 成功且 final READABLE 后 Run 以 `FINAL_OUTPUT_PIPELINE_NOT_READY` 失败。WP4 的价值不是“多一个发布功能”，而是把“谁能发布最终文本”“发布失败如何分类”“什么时候才能写 Memory”“部分持久化后序事件序号如何保持一致”变成可测试合同。

## 4. 架构演进

### 4.1 WP1 已提供的能力

- `AgentRegistry`：Agent 身份、entry/delegated 权限和执行适配标识（`execution_adapter_id`）的事实源。
- `PlanResolver` / `StrictPlanningDecisionParser`：显式 Agent、确定性规则与模型规划的统一 typed decision。
- `PlanCompiler`：按 Registry 和四种固定图规则生成安全 Plan，并根据 typed decision 设置调用角色（WP4 新增）。

### 4.2 WP2 已提供的能力

- 默认 Coordinated API 强制进入 Resolver；dynamic Plan 生命周期 `UNRESOLVED -> RESOLVING -> FROZEN`。
- 单 Step 动态执行（`ResolvedSingleStepDriver`）保留旧 `OUTPUT_DELTA`。

### 4.3 WP3 已提供的能力

- `MultiAgentDriver` + `AgentAdapterFactory`：specialist 真实并行执行。
- `StepResultStore`：PREPARED -> READABLE、once-write、ACL、容量、seal/clear。
- `StepResultCommitter`：最小结果提交骨架；synthesis 只读显式依赖视图。
- 临时保护：`FINAL_OUTPUT_PIPELINE_NOT_READY`（含 WP4 removal marker）。

### 4.4 WP4 补齐的执行链

```text
Frozen Plan
  -> Scheduler 并行 claim
  -> MultiAgentDriver -> AgentAdapterFactory
  -> specialist / synthesis Agent（persist=False）
  -> typed StepResult
  -> StepCompletionPipeline（完整版）
       INTERNAL: Store PREPARED -> Step SUCCEEDED -> READABLE -> STEP_COMPLETED
       FINAL:    ... -> READABLE -> OutputGate.attempt_publish
                 -> DELIVERED / FAILED / OUTCOME_UNKNOWN
                 -> [DELIVERED only] RunFinalMemoryWriter
                 -> STEP_COMPLETED -> safe StepCompletionResult
  -> Coordinator 先消费 delivery report
  -> Run SUCCEEDED 或显式 delivery failure
```

WP3 的 `FINAL_OUTPUT_PIPELINE_NOT_READY` 临时保护在同一变更中原子删除；单 Step 与 Legacy 行为保持不变。

## 5. 方案讨论与取舍

| 方案 | 优点 | 风险或不足 | 结论 |
| --- | --- | --- | --- |
| 保留 dynamic 单 Step 字符串自动输出路径 | 改动小 | 单叠多 Step 最终输出合同不统一；`isinstance(result, str)` 匹性决定输出语义易被类型变化破坏 | 拒绝，默认 dynamic 全部走 typed pipeline |
| 让 Driver 或 Adapter 发布 `OUTPUT_DELTA` | 路径短 | 发布权与执行权混合，Gate 的 at-most-once 和授权校验都失去意义 | 拒绝，发布只能由 OutputGate 执行 |
| 让 Scheduler 决定是否输出 | 与调度合并 | Scheduler 只管 claim，不应拥有用户输出权；调度状态不能代替 delivery 合同 | 拒绝 |
| unknown 当作 failed 后重试发布 | 表面可用性 | 正文可能已 journaled，重试会重复发布用户可见文本，违反 at-most-once | 拒绝，unknown 不重试 |
| 让 Adapter 以 `persist=True` 写 Memory | 保留旧 direct 持久化 | 写入发生在交付确认前，FAILED/UNKNOWN 也可能写入 | 拒绝，由 Run-level final owner 在 DELIVERED 后统一提交 |
| Static 也迁移 OutputGate（策略 B） | 路径统一 | 必须迁移所有静态 multi-step fixture 为 INTERNAL + 唯一 FINAL，扩大本轮兼容范围 | 拒绝，采用策略 A：static compatibility 不使用新 Gate |
| 用字符串/对象类型匹性决定输出 | 无新参数 | 隐式技巧脆弱，类型错误会静默改变输出语义 | 拒绝，Executor 使用显式 typed mode |
| 用每 Step 并发 attempt 中的重试来提高可用性 | 易实现 | Gate 状态一旦终态不可重开；duplicate attempt 必须 fail closed | 拒绝，保证 at-most-once publish attempt |

关键选择是：发布权单一化。OutputGate 是每 Run 唯一的发布 owner，只有 StepCompletionPipeline 能调用它；而交付结果与 Step 执行成功分层，一个 Run 最多有一个 terminal outcome。
## 6. 核心架构和状态机

### 6.1 OutputGate 状态机（`core/runtime/output_gate.py:41`）

```text
NOT_STARTED
-> PUBLISHING
-> PUBLISHED | FAILED | OUTCOME_UNKNOWN
```

- 只有 `NOT_STARTED` 可以开始 publish；`attempt_publish`（`output_gate.py:263`）在锁内先校验状态，`PUBLISHING` 或任意终态均 fail closed。
- `PUBLISHING` 中到达的第二次 attempt 直接返回 `OUTPUT_GATE_DUPLICATE_ATTEMPT`（不等待、不重试）。
- `PUBLISHED`、`FAILED`、`OUTCOME_UNKNOWN` 均禁止第二次 attempt；`FAILED` 同样禁止重试（attempt 终态不可逆）。
- concurrent duplicate：两协程并发时只有一个能进入 `PUBLISHING`，另一个立即得到 duplicate 结果；测试 `test_concurrent_duplicate_attempts_allow_only_one_publish` 断言仅 1 个 `OUTPUT_DELTA`。
- duplicate 稳定 error code：`OUTPUT_GATE_DUPLICATE_ATTEMPT`。

授权校验（`_authorize_locked`，`output_gate.py:192`）：Step 已由 Scheduler claim 且已提交 `SUCCEEDED`；Step 在冻结 Plan 中；Store entry `READABLE`；`output_policy` 为 `FINAL_PASSTHROUGH` 或 `FINAL_SYNTHESIS`；Step 是 Plan 唯一 final source；Gate 尚未尝试；Store 未 seal；Run 仍处于允许完成的 active 状态。INTERNAL Step 调用 Gate 被拒绝（`OUTPUT_GATE_INTERNAL_STEP`）。

### 6.2 DeliveryStatus 分类依据

```python
class DeliveryStatus(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
```

EventChannel 是 journal-first（`event_channel.py:279` publish 内 `self._journal.append(event)` 后 `self._sequence = sequence`，再 enqueue）。因此（`output_gate.py:308-361`）：

| 分类依据 | DeliveryStatus | error_code |
| --- | --- | --- |
| publish 正常返回 | DELIVERED | 无 |
| `EventPublicationError.partially_persisted=False`（journal append 前失败） | FAILED | FINAL_OUTPUT_DELIVERY_FAILED |
| `EventPublicationError.partially_persisted=True`（journal append 后 enqueue 失败） | OUTCOME_UNKNOWN | FINAL_OUTPUT_DELIVERY_UNKNOWN |
| INTERNAL Step（Gate 永不调用） | NOT_APPLICABLE | 无 |

`DELIVERED` 的准确含义：Runtime 的 EventChannel publish 正常返回，正文事件已进入当前交付通道；不代表前端已展示，也不代表最终用户确认阅读。

### 6.3 StepCompletionPipeline（`core/runtime/step_completion.py:121`）

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

- 内部 Step 状态在 Gate 前已 `SUCCEEDED`；外部事件仍是 `OUTPUT_DELTA < STEP_COMPLETED`；Gate 失败不得将 Step 改为 FAILED；不得映射为 `AGENT_STEP_FAILED`；不得重跑 Agent/Synthesis；不得重新发布正文。
- 安全报告 `StepCompletionResult`（`step_completion.py:97`）至少表达：`step_id`、`commit_status`、`final_result_ready`、`output_policy`、`delivery_status`、`delivery_error_code`、`event_emitted`、`completion_error_code`；不含 raw result、不含 output 正文、不含 Binding、不进 Snapshot/Checkpoint、repr 安全。
- delivery 错误与 completion event 错误可区分：`FINAL_OUTPUT_DELIVERY_FAILED` / `FINAL_OUTPUT_DELIVERY_UNKNOWN` / `OUTPUT_GATE_DUPLICATE_ATTEMPT` / `FINAL_OUTPUT_MEMORY_COMMIT_FAILED` 与 `STEP_COMPLETION_EVENT_FAILED` 不使用单一模糊 error string 覆盖。

### 6.4 Partial publication 与 Step sequence

`StepEventEmitter.emit`（`event_emitter.py:140`）的修复：

```text
try publish OUTPUT_DELTA
except EventPublicationError as exc:
    if exc.partially_persisted:
        consume/increment local step sequence
    re-raise or return classified outcome
```

真实故障注入序列（`tests/test_partial_publication_sequence.py`，EVENT_BEFORE_CHANNEL_ENQUEUE）：

```text
STEP_STARTED(synthesis) sequence=1
OUTPUT_DELTA(synthesis) sequence=2 已写 Journal
enqueue 失败 -> partially_persisted=True
STEP_COMPLETED(synthesis) sequence=3
ERROR(FINAL_OUTPUT_DELIVERY_UNKNOWN)
RUN_COMPLETED(FAILED)
```

- StepEmitter 在 `event_emitter.py:185-192` 消费 sequence：仅当 `partially_persisted=True` 时执行 `self._sequence = step_sequence`（在锁内且随后重抛）。
- 未持久化失败不消费：`partially_persisted=False` 时不修改本地 sequence，下一次事件使用同一号（该号未写 Journal，无冲突）。
- 防止重复消费：消费发生在 StepEmitter 的 `asyncio.Lock` 内，只在该次 publish 的异常路径执行一次；正常路径在 `self._sequence = step_sequence` 处递增。
- Journal 中 sequence 唯一单调：测试断言 synthesis 的 `step_sequence` 无重复且按序排列同于原序。
- 为什么 unknown 不重试：正文可能已 journaled/部分提交，无法证明消费者未收到；重试会重复发布用户可见文本。

### 6.5 Coordinator delivery 决策

`_execute_batches` 在 `await executor_task` 后、下一次 Scheduler `is_complete` 成功判断之前调用 `_decision_from_batch_report`（`run_coordinator.py:1018`），检查顺序：result/state commit failure -> completion event failure -> delivery status。

| delivery | Final Step | RunStatus | StopReason | error_code |
| --- | --- | --- | --- | --- |
| DELIVERED | SUCCEEDED | 正常继续并最终 SUCCEEDED | COMPLETED | null |
| FAILED | SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_DELIVERY_FAILED |
| OUTCOME_UNKNOWN | SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_DELIVERY_UNKNOWN |
| duplicate attempt | 按实际 | FAILED | UNHANDLED_ERROR | OUTPUT_GATE_DUPLICATE_ATTEMPT |
| Memory commit failed | SUCCEEDED | FAILED | UNHANDLED_ERROR | FINAL_OUTPUT_MEMORY_COMMIT_FAILED |

因此即使所有 Step 已 `SUCCEEDED`，只要 final delivery 为 FAILED/UNKNOWN（或 Memory 提交失败），Run 也不会误报 success；不产生 `RUN_COMPLETED(SUCCEEDED)`，Step 仍发布 `STEP_COMPLETED(SUCCEEDED)`，Coordinator 发布 safe `ERROR`，一个 Run 只有一个 terminal outcome。
## 7. 实际实现

### 7.1 Dynamic 单 Step 迁移 typed pipeline

- `RunCoordinator._is_typed_multi_step_plan`（`run_coordinator.py:416`）对 dynamic Plan 返回 `self._dynamic`；`_initialize_typed_runtime`（`run_coordinator.py:436`）创建 `StepResultStore` + `OutputGate` + `RunFinalMemoryWriter` 并注入 `StepResultCommitter`。
- Shape 0（Core direct）、Shape 1（显式 entry / delegated knowledge direct）、Shape 2（single specialist + synthesis）、Shape 3（fan-out specialists + synthesis）全部返回 `StepResult` 并进入 StepCompletionPipeline + OutputGate。
- `ResolvedSingleStepDriver` 仅保留用于 Legacy、显式 static compatibility 和内部旧测试；默认 dynamic 路径不再依赖它发布用户文本。

### 7.2 InvocationRole 与 HistoryPolicy

`core/runtime/invocation_bindings.py:14`：

```python
class InvocationRole(str, Enum):
    ENTRY = "ENTRY"
    DELEGATED = "DELEGATED"
    SYNTHESIS = "SYNTHESIS"
```

Compiler 根据 typed decision 设置 role；赋值存在 run-scoped Binding metadata（`AgentInvocationSpec.role`），不含 raw instruction、不按 step ID/Agent 名猜测、不扩大 Snapshot。映射（`history_policy_for_role`，`invocation_bindings.py:30`）：

| 调用角色 | HistoryPolicy | persist |
| --- | --- | --- |
| Core direct | AGENT_SCOPE | False |
| 用户显式 entry specialist | AGENT_SCOPE | False |
| delegated specialist（含单 knowledge passthrough） | NONE | False |
| synthesis | NONE | False |

所有 Dynamic Adapter 调用 `persist=False`（`agent_adapter_factory.py` `AgentRouterSingleAgentAdapter.execute` 内 `persist=False, history_policy=request.history_policy`）；最终 Memory 由 Run-level final owner 在 `DELIVERED` 后统一提交。

### 7.3 delivered-only Memory

`RunFinalMemoryWriter`（`core/runtime/final_memory_writer.py:19`）实现：

- 字段：`_router`、`_entry_agent_id`、`_user_request`、`_persist`、`_write_lock`、`_written`。
- `write_delivered`（`:55`）在锁内检查 `_written`，已写则拒绝重复写入；写失败在 `except BaseException` 中重置 `_written=False`。
- 写入内容：原始 user message 一次 + 确认 delivered 的唯一 final assistant message 一次，均写到 entry Agent 的现有 `direct` scope；不写 specialist/Synthesis raw 结果。
- 用户消息不会重复写：Dynamic Adapter 全部 `persist=False`，`AgentRouter._run_agent_once` 的 user/assistant 写入均关闭；completion guard + writer write-once 保证一个 Run 最多写一次。
- 写入时机：Gate 返回 `DELIVERED` 后、`STEP_COMPLETED` 发布前。
- `DELIVERED` 写入；`FAILED` 不写入；`OUTCOME_UNKNOWN` 不写入。
- Memory 写失败时：Gate 保持 `PUBLISHED`；Step 保持 `SUCCEEDED`（不回滚）；Run 以 `FINAL_OUTPUT_MEMORY_COMMIT_FAILED` 失败，记录“已交付、Memory 提交失败”的分层事实；正文已 delivered 时不重发。

具体 scope （延用现有会话约定，未发明新 scope）：

| 形态 | Memory scope |
| --- | --- |
| Core direct | `core_router` 的 `direct` scope |
| 显式 entry specialist | 所选 entry Agent（如 `code_expert`）的 `direct` scope |
| delegated knowledge direct | 发起请求的 entry Agent（默认 `core_router`）的 `direct` scope；`knowledge_expert` 不写入 |
| multi-agent default | 发起请求的 entry Agent（默认 `core_router`）的 `direct` scope；specialist/synthesis 均不写入 |

### 7.4 其他实现要点

- `core/runtime/event_channel.py`：发布故障的 `FaultMatchContext` 增加 `step_id`，支持按 Step 注入故障。
- `core/runtime/step_result_store.py:487`：新增 `read_final_content(final_step_id)`：仅唯一 final、仅 READABLE、仅 OPEN 时可读，仅供 Run-level final Memory writer 使用。
- `core/runtime/run_coordinator.py`：删除 `FINAL_OUTPUT_PIPELINE_NOT_READY` 临时保护；static scope 不创建 OutputGate（`output_gate is None`）。

## 8. 安全、失败与兼容合同

### 8.1 事件序列（E2E 真实验证）

Multi-Agent 成功：

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

Known delivery failure：

```text
... final Driver success
[StepState already SUCCEEDED, Store READABLE]
OUTPUT_DELTA attempt fails before journal append
STEP_COMPLETED(SUCCEEDED)
ERROR(FINAL_OUTPUT_DELIVERY_FAILED)
RUN_COMPLETED(FAILED)
```

Outcome unknown：

```text
... final Driver success
OUTPUT_DELTA journaled
enqueue fails
StepEmitter consumes used sequence
STEP_COMPLETED(SUCCEEDED) using next sequence
ERROR(FINAL_OUTPUT_DELIVERY_UNKNOWN)
RUN_COMPLETED(FAILED)
```

### 8.2 Step / delivery 分层

| 场景 | Final Step | Store | Delivery | Run |
| --- | --- | --- | --- | --- |
| delivered | SUCCEEDED | READABLE | DELIVERED | SUCCEEDED |
| known failed | SUCCEEDED | READABLE | FAILED | FAILED |
| unknown | SUCCEEDED | READABLE | OUTCOME_UNKNOWN | FAILED |
| Memory commit failed | SUCCEEDED | READABLE | DELIVERED | FAILED |

delivery failure 不映射为 `AGENT_STEP_FAILED` 或 `SYNTHESIS_FAILED`：Step 提交成功后再进入 Gate，而 `AGENT_STEP_FAILED`/`SYNTHESIS_FAILED` 只在 Driver/Adapter 执行失败时产生。

### 8.3 敏感数据边界

安全标记：`SECRET_FINAL_CANDIDATE`、`SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST`、`\\internal\\private\\final.txt`。断言：

- final candidate 只出现在唯一 OUTPUT 事件（journal 仅存 `text_digest`）与允许的 delivered Memory；
- internal/specialist raw 不进入用户输出或 Memory；
- failed/unknown final 不进入 Memory；
- report/repr/error/Trace/Snapshot/Journal 不含正文；Gate 状态与安全 error 可观测但无正文。

### 8.4 Static / Legacy 兼容（策略 A）

- static compatibility 不创建 OutputGate；`CoordinatedRuntimeFactory.create_static_run_scope` 创建的 coordinator `_dynamic=False`，typed runtime 不初始化，`output_gate is None`。
- static multi-step 的旧默认 `FINAL_PASSTHROUGH` 不会被 WP4 Gate 误解释（Gate 不作用于 static 路径）；static multi-step 不被宣称具有 WP4 delivery 语义。
- Legacy：显式 LEGACY selector 不创建 Coordinated scope，原输出与 Memory 行为不变；static 路径仍由 `CoordinatedSingleAgentDriver` 以既有 `persist` 参数调用 router，不经过 `RunFinalMemoryWriter`。

## 9. 高价值 Bad Cases

### Bad Case 1：缺少 memory_manager 的测试容器导致 delivered-only Memory 写入失败

- 类型：真实发现（WP4 实施测试中观察到，不是生产事故）
- 触发条件：共享 fixture 的 `FakeRouter` 没有 `memory_manager`，而 `create_run_scope` 默认 `persist=True`。
- 故障表现：Gate 已 DELIVERED 后，RunFinalMemoryWriter 找不到 memory_manager，Run 以 `FINAL_OUTPUT_MEMORY_COMMIT_FAILED` 失败，而不是预期的 SUCCEEDED。
- 根因分析：WP4 引入了“只有 DELIVERED 才写 Memory”的 Run 级 owner，但旧 fixture 仍然以为“无 memory 也能运行”。
- 修复方案：为共享 `FakeRouter` 增加内存版 `FakeMemoryManager` 桩；真实 Memory 合同测试仍使用 SQLite `MemoryManager`。
- 回归测试：WP4 专项与全仓回归全部通过。
- 对应知识点：测试替身合同、依赖注入、交付后的副作用写入。
- 面试表达：这个失败反而证明 delivered-only 写入确实在 Gate 之后执行；我修的是 fixture 而不是放宽生产合同。
- 当前状态：已修复。

### Bad Case 2：Gate 的 claim 校验错误要求 Step 仍在 active 集合

- 类型：真实发现（WP4 实施中发现，不是生产事故）
- 触发条件：Step 提交 `SUCCEEDED` 时会从 `active_step_ids` 移除；若 Gate 在此后要求“仍在 active”，将拒绝所有正常发布。
- 故障表现：Gate 无法通过授权，FINAL Step 永远不产生 `OUTPUT_DELTA`。
- 根因分析：把“claim 在执行期持有”和“提交后释放”混为“提交后仍在 active”。
- 修复方案：Gate 校验 Step 状态为 `SUCCEEDED`（claim 已在提交前完成使用）。
- 回归测试：OutputGate 授权与 Shape E2E 全部通过。
- 对应知识点：状态机时机、claim 生命周期、交付前的完成验证。
- 面试表达：Step 成功与 delivery 是两个时刻；Gate 必须在 Step 已确认成功后才能发布。
- 当前状态：已修复。

### Bad Case 3：EventChannel 故障匹配上下文缺少 `step_id`

- 类型：真实发现（WP4 实施测试中发现，不是生产事故）
- 触发条件：测试希望只对 synthesis 的 `STEP_COMPLETED` 注入故障，但 Channel 的 `FaultMatchContext` 未携带 `step_id`，规则永不命中。
- 故障表现：按 Step 的故障注入静默失效，Run 以成功结束而不是预期失败。
- 根因分析：故障匹配上下文缺少字段，测试不能精确定位到 Step 。
- 修复方案：`event_channel.py` 的发布故障上下文增加 `step_id=event.step_id`。
- 回归测试：`test_step_completion_delivery.py` 中按 Step 故障用例通过；原有 fault injection 测试无回退。
- 对应知识点：故障注入匹配维度、测试可定位性。
- 面试表达：故障注入不能只靠事件类型匹配，要能精确定位到某个 Step 才能验证层分语义。
- 当前状态：已修复。

### Bad Case 4：Store seal 后的 READABLE 检查顺序错误

- 类型：真实发现（WP4 实施测试中发现，不是生产事故）
- 触发条件：Store 已 `seal` 后 `has_readable` 返回 False。
- 故障表现：Gate 报 `OUTPUT_GATE_STORE_NOT_READABLE` 而不是更精确的 `OUTPUT_GATE_STORE_SEALED`。
- 根因分析：把“已封闭”和“未可读”合并成同一判断顺序。
- 修复方案：先校验 `is_sealed`，再校验 `has_readable`。
- 回归测试：OutputGate 状态机中 sealed 用例通过。
- 对应知识点：错误优先级、分层诊断。
- 面试表达：相同拒绝下不同的根因应返回不同的稳定错误码，便于运维定位。
- 当前状态：已修复。

### Bad Case 5：测试用第一个 `STEP_COMPLETED` 验证事件顺序

- 类型：真实发现（WP4 测试侧观察到，不是产品缺陷）
- 触发条件：Shape 3 中 specialist 的 `STEP_COMPLETED` 早于 synthesis 的 `OUTPUT_DELTA`；`types.index(STEP_COMPLETED)` 命中第一个。
- 故障表现：`OUTPUT_DELTA < STEP_COMPLETED` 断言错误失败。
- 根因分析：用首个匹配代替“最后一个 STEP_COMPLETED（即 final Step 的完成）”。
- 修复方案：改用“最后一个 STEP_COMPLETED 之前”验证事件顺序。
- 回归测试：Shape 2/3 E2E 与 lifecycle 断言通过。
- 对应知识点：事件顺序断言的准确定位。
- 面试表达：多 Step 下要验证的是 final Step 的完成顺序，而不是任意一个完成。
- 当前状态：已修复。
## 10. 测试和验收证据

WP4 专项：

| 测试文件 | 重点 | 结果 |
| --- | --- | --- |
| `tests/test_output_gate.py` | 状态机、授权、at-most-once、concurrent duplicate、safe repr | 10 passed |
| `tests/test_partial_publication_sequence.py` | journal/enqueue 分裂、sequence 消费与不消费、Journal 唯一单调 | 2 passed |
| `tests/test_step_completion_delivery.py` | Final Step/delivery 分层、known/unknown 分类、completion event 失败 | 5 passed |
| `tests/test_final_output_delivery.py` | Shape 0～3 唯一输出、无 Core fallback、无 `FINAL_OUTPUT_PIPELINE_NOT_READY` | 5 passed |
| `tests/test_final_memory_boundary.py` | delivered-only Memory、FAILED/UNKNOWN 不写、specialist raw 不写 | 4 passed |

回归与全仓：

| 命令 | 结果 |
| --- | --- |
| WP1-WP3 关键回归（dynamic lifecycle、multi-agent execution、WP3 history boundary、step-result security、coordinated factory、event 等） | 全部通过 |
| `uv run pytest -q` | 1299 passed, 42 subtests passed |
| `uv run python -m compileall -q core tests server.py main.py` | PASS |
| `git diff --check` | PASS（仅 Git LF/CRLF 提示，无 whitespace error） |

实施中的失败与修复都被保留：FakeRouter 缺少 memory_manager、Gate claim 校验错误、fault context 缺 `step_id`、seal 检查顺序、事件顺序断言错误（见 Bad Case 1-5）；没有删除旧测试或放宽断言。

## 11. 当前能力边界

WP4 已完成：

- 默认 Dynamic Coordinated Shape 0～3 统一 typed completion pipeline。
- OutputGate：at-most-once publish attempt、授权校验、不恢复。
- DeliveryStatus：DELIVERED/FAILED/OUTCOME_UNKNOWN/NOT_APPLICABLE 与稳定错误码。
- Coordinator 先消费 delivery report；删除 `FINAL_OUTPUT_PIPELINE_NOT_READY`。
- partial-persisted Step sequence 修复；StepEmitter 已消费已持久化序号。
- delivered-only Memory；InvocationRole/HistoryPolicy 明确调用角色。

WP4 尚未完成（本轮不开始 WP5）：

- 前端 DAG/Agent 状态 UI、AgentEvalOps 接入、Store/Bindings 跨进程恢复、OutputGate 持久化恢复、exactly-once delivery 承诺、通用消息重投递服务、广泛 Trace/Journal/Memory 安全重构、新线程池或 Planning 容量治理。
- Planning executor 饿死 P2 仍存在（Planning 与 specialist 共享 bounded executor，`PLANNING_MODEL` 无保底容量），按 WP3 边界保持记录。

因此不能宣称“Stage 2.5 多 Agent 已完成”，只能宣称“默认 Coordinated 多 Agent 已可输出唯一 final answer，交付与执行分层，前端与安全审计 属于 WP5”。

## 12. 面试表达版本

### 12.1 30 秒版本

WP4 把 LocalAgent 多 Agent 的“执行成功但用户可见交付未备”收尾成唯一最终输出链：默认 Shape 0～3 统一走 typed pipeline，OutputGate 以 at-most-once 发布唯一 `OUTPUT_DELTA`，交付结果与 Step 执行成功分层，只有 DELIVERED 的 final 才写 Memory，并修复了 partial publication 下的 Step sequence 重用。WP4 专项 26 个测试，全仓 1299 passed + 42 subtests passed。

### 12.2 2 分钟版本

问题来自一个真实缺口：WP3 能让 specialist 并行执行并用 synthesis 生成 final candidate，但所有 multi-step Step 都不产生 `OUTPUT_DELTA`，Run 以 `FINAL_OUTPUT_PIPELINE_NOT_READY` 结束。WP4 的关键不是“多一个发布功能”，而是把交付变成可测试合同。

第一是 OutputGate：每 Run 一个，状态机 NOT_STARTED -> PUBLISHING -> PUBLISHED/FAILED/OUTCOME_UNKNOWN，只有 StepCompletionPipeline 能调用，重复 attempt 必须 fail closed，发布失败不影响 Step SUCCEEDED。第二是交付分类：journal append 前失败是 FAILED，journal 已写但 enqueue 失败是 OUTCOME_UNKNOWN，unknown 不重试。第三是 partial publication 序号修复：StepEmitter 在 partially_persisted 时消费本地 sequence，避免 STEP_COMPLETED 重用已写 Journal 的序号。第四是 delivered-only Memory：Adapter 全部 persist=False，RunFinalMemoryWriter 只在 DELIVERED 后写一次 user + final assistant，FAILED/UNKNOWN/specialist raw 不写。

实现过程中修复了几个真实问题：FakeRouter 缺少 memory_manager、Gate 的 claim 校验时机、fault 上下文缺 step_id、seal 检查顺序和事件顺序断言。最终 WP4 专项 26 passed，全仓 1299 passed + 42 subtests passed，P0/P1 为零，P2 为 1（Planning 饿死容量风险）。

### 12.3 深入追问主线

1. 画出 OutputGate 状态机，说明为什么 FAILED 也不重试。
2. 说明为什么 Driver/Adapter/Scheduler 无法调用 Gate：只有 Coordinator 创建并注入 StepCompletionPipeline。
3. 解释 DELIVERED 与“用户确认阅读”的区别。
4. 用故障注入序列说明 partial publication 的 sequence 消费与不消费，以及 unknown 不重试的原因。
5. 解释 Coordinator 为什么能在所有 Step SUCCEEDED 时仍不误报 success：batch report 在下一次 Scheduler 判断前被消费。
6. 说明 delivered-only Memory 的 write-once 与 scope 约定，以及 Memory 写失败时的分层事实。
7. 解释 InvocationRole 与 HistoryPolicy 的映射，以及为什么不按 Agent ID 推断。
8. 解释 Static/Legacy 为什么不受 Gate 影响（策略 A）。

## 13. 最终验收结论

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
