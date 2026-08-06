# LocalAgent Stage 2.5 Multi-Agent WP5 面试材料

> 适用范围：Stage 2.5 Multi-Agent WP5——完成可观测性、持久事实、安全审计和客户端解释层：统一 Trace 合同（Planning/Step/Delivery/Memory/Run）、多 Agent Span 父子关系、Journal 安全事实与 reducer 投影、delivered-only Memory 原子性、Delivery/Memory 分层观测、Runtime Projection、前端最小多 Agent 状态展示、Snapshot/Recovery 交付边界强化与安全矩阵。
>
> 真实性声明：本文中的“真实发现”仅指本地项目源码审查、实施或测试中实际观察到的问题，不等同于线上生产事故；“假设构造”只用于风险推演，不描述成真实事故。

## 1. 推荐的面试材料模板

一份可信、便于追问的工程面试材料应包含：

1. 一句话项目定义：先说明解决的用户问题，再介绍技术方案。
2. 真实性与边界：区分用户真实复现、源码审查发现、实施测试发现和假设构造。
3. 原始用户场景：给出真实输入、预期链路、实际链路和风险。
4. 架构演进：说明旧架构为什么无法满足要求，以及 WP1、WP2、WP3、WP4、WP5 分别解决什么。
5. 方案讨论：展示候选方案、取舍标准和被拒绝方案。
6. 核心状态机与时序：说明成功、失败、取消、超时的确定性行为。
7. 数据与权限边界：谁有权决定 Agent、instruction、Plan、结果写入和最终输出。
8. 兼容策略：旧入口、单 Step、静态 Plan、Snapshot 和 Streaming 如何迁移。
9. Bad Cases：统一格式，明确真实性，写清触发、根因、修复和回归。
10. 验收证据：给出测试命令、通过数量和仍未实现的能力。
11. 面试表达：准备 30 秒、2 分钟和深入追问三个版本。
12. 核心追问必须有参考答案：在深入追问章节中，对提出的每个问题给出简短、可验证的真实回答（源码位置、测试证据或合同条款），不允许只列问题不答；这保证材料经得起“追问到底”而不是背稿。

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

WP5 的目标是给 Stage 2.5 多 Agent 运行时补齐“可观测、可审计、客户端可解释”的最后一层：冻结 Trace Contract v1（run/planning/step/synthesis/delivery/memory 六类 span 与安全属性边界），让 Journal 只保存安全事实（OUTPUT 仅 digest/length），把 delivered-only Memory 变成单事务原子提交，建立供前端与测试共享的 Runtime Projection，并对 Snapshot/Recovery 明确“交付与 Memory 不重发、不重写”的边界。

WP5 完成后的诚实表述：运维可以根据 Trace/Journal 判断失败属于 Planner、specialist、Store、Synthesis、delivery 还是 Memory；用户能看到多 Agent 处于规划、执行、综合、交付或失败的哪一阶段；原始 instruction/result/final 不进入不允许的观测与持久化通道；但完整故障矩阵、RC Gate 与 Stage 2.5 最终验收仍属于 WP6，不能宣称 Stage 2.5 全部完成。
## 3. 真实用户场景与问题价值

用户在主 Agent 入口输入：

```text
分别让代码专家和知识专家审查这个模块，然后综合两份结论给出最终建议
```

用户期望：

```text
主入口 -> 识别 code + knowledge 两个任务
-> 两个 specialist 并行执行
-> synthesis 基于两份结果生成唯一 final answer
-> 唯一 OUTPUT_DELTA 交付给用户
-> 若交付成功但记忆保存失败，用户能看到“回答已交付，记忆保存失败”
   而不是笼统的“运行失败”
```

WP4 之后的真实状态：唯一最终输出链已可用（OutputGate at-most-once、delivered-only Memory、partial publication sequence 修复），但所有观测通道仍不完整：span 命名不允许点号、STEP/PLAN/RUN 事件没有分层事实、Memory 两次写入非事务、Recovery 对“OUTPUT 已 journaled 但无 terminal”没有专门判定、前端只消费 Legacy 编排事件。WP5 的价值不是新增业务能力，而是把“失败到底属于哪一层”“交付与记忆是否可区分”“原始内容是否泄漏到不允许的通道”变成可测试合同。

## 4. 架构演进

### 4.1 WP1 已提供的能力

- `AgentRegistry`：Agent 身份、entry/delegated 权限和执行适配标识的事实源。
- `PlanResolver` / `StrictPlanningDecisionParser`：显式 Agent、确定性规则与模型规划的统一 typed decision。
- `PlanCompiler`：按 Registry 和四种固定图规则生成安全 Plan。

### 4.2 WP2 已提供的能力

- 默认 Coordinated API 强制进入 Resolver；dynamic Plan 生命周期 `UNRESOLVED -> RESOLVING -> FROZEN`；Plan freeze + fingerprint；POST_PLAN_PRE_EXECUTION checkpoint。

### 4.3 WP3 已提供的能力

- `MultiAgentDriver` + `AgentAdapterFactory`：specialist 真实并行执行。
- `StepResultStore`：PREPARED -> READABLE、once-write、ACL、容量、seal/clear。
- Synthesis 只读显式依赖视图；raw instruction 只存在于 Run-scoped Bindings。

### 4.4 WP4 已提供的能力

- StepCompletionPipeline 完整版：OutputGate at-most-once publish、DeliveryStatus 分类、delivered-only Memory、partial publication Step sequence 修复、Coordinator 先消费 delivery report。

### 4.5 WP5 补齐的观测与一致性链

```text
Planning
  -> Plan freeze
  -> Specialist execution
  -> StepResult commit
  -> Synthesis
  -> OutputGate delivery
  -> Final Memory commit
  -> Run terminal
       |
       +-> Runtime Events（分层安全 payload）
       +-> Journal facts（OUTPUT 仅 digest/length）
       +-> Trace / Span（Trace Contract v1）
       +-> Metrics（低基数标签）
       +-> Streaming control events
       +-> Frontend state projection（RuntimeProjection + 共享文案模型）
```

WP4 的 `RunFinalMemoryWriter` 在同一变更中升级为单事务原子提交；`FINAL_OUTPUT_MEMORY_COMMIT_FAILED` 等分层错误码保持不变，但事件/Journal/前端都能区分 Step/Delivery/Memory/Run 四层状态。

## 5. 方案讨论与取舍

| 方案 | 优点 | 风险或不足 | 结论 |
| --- | --- | --- | --- |
| span operation 继续沿用无点号命名 | 改动最小 | 合同建议 `runtime.run` 等分层命名，无点号无法表达“类型.子类型”；纯数字/下划线命名可读性差 | 拒绝，operation 校验放开 `.`（`tracing.py:_OPERATION`），ID 校验保持严格 |
| 新建独立 Trace Owner 直接消费 EventChannel | 解耦 | 与已有 Event/Trace/Reducer 形成第二套 owner，状态双写 | 拒绝，span 仍由各运行 owner（Coordinator/Executor/Gate/MemoryWriter）创建，`trace_contract.py` 只提供常量与属性助手 |
| Memory 继续两次 `add_message` 并允许失败重试 | 改动小 | 第一条写入成功第二条失败会留下只有 user 没有 assistant 的完整历史；`_written` 失败重置使同一 Run 可再次调用 | 拒绝，改为 SQLite 单事务 `append_exchange_atomic` + 写一次不可重试 |
| 前端继续用字符串判断所有事件 | 无新模块 | 状态逻辑散落在 UI 线程，无法单测；控制事件与正文混判 | 拒绝，新增 `runtime_projection.py` 与 `multi_agent_status.py`，前端只调共享文案模型 |
| Recovery 对“OUTPUT journaled 但无 terminal”保持 RESUMABLE | 不破坏旧测试 | 交付结果可能已发生，resume 可能重复回答 | 拒绝，标记 delivery outcome 不确定，REQUIRES_RECONCILIATION，不重发不重写 |
| 用正文 hash 代替所有泄漏扫描 | 测试简单 | 只证明 digest 匹配，不能证明 raw 不进 Trace/Journal/前端 | 拒绝，安全矩阵用真实 Shape 3 主链 + 敏感标记扫描全通道 |
| 给 delivery 指标直接加 step_id/run_id 标签 | 可观测粒度细 | 高基数标签，违反 Metrics 合同 | 拒绝，error_code 有限枚举、agent_id 受控 allowlist、禁止高基数标签 |

关键选择是：观测只是旁路，永远不改变 Runtime 行为。Span/指标/投影失败都被隔离吞掉或走健康计数，不影响执行、重试与 Memory 写入；而所有状态判定只来源于真实事件事实，不虚构版本、不把 digest 当正文恢复。
## 6. 核心架构和状态机

### 6.1 Trace Contract v1（`core/runtime/trace_contract.py`）

六类 span 固定命名与父子关系：

| Span | operation | 创建 owner | 父节点 |
| --- | --- | --- | --- |
| Run root | `runtime.run` | RunCoordinator.execute | 无 |
| Planning | `runtime.planning` | RunCoordinator.execute | Run root |
| Step | `runtime.step` | ParallelExecutor worker | Run root |
| Synthesis | `runtime.synthesis` | MultiAgentDriver | Step span（synthesis step） |
| Output delivery | `runtime.output_delivery` | OutputGate.attempt_publish | 当前 Step span |
| Final Memory commit | `runtime.final_memory_commit` | RunFinalMemoryWriter | 当前 Step span |

Shape 3 并行 specialist 必须满足：每个 specialist 独立 `runtime.step` span，parent 为 Run root，彼此 sibling 不嵌套；synthesis span 在依赖 specialist span 结束后开始；不伪造成嵌套 specialist（`test_multi_agent_trace.py` 用真实 Shape 3 主链断言 span 起止时间与 parent 关系）。

安全属性：`tracing.SAFE_SPAN_ATTRIBUTES` 是唯一 allowed set；`DENIED_SPAN_ATTRIBUTES` 覆盖 prompt/messages/user_input/model_output/tool_arguments/tool_output/query/rag_chunk/memory/secret/api_key/provider_url/canonical_path/exception_message/traceback 等；缺失版本字段显式写 `not_configured`，不虚构版本。

### 6.2 Journal 安全事实与 reducer（`core/runtime/events.py`、`journal_tail_reducer.py`）

逐事件 allowlist（`_JOURNAL_PAYLOAD_FIELDS` + `validate_journal_payload`）：

| 事件 | 允许字段 | 禁止 |
| --- | --- | --- |
| PLANNING_STARTED | schema/timeout | query、prompt |
| PLAN_CREATED | plan_id/version/fingerprint/step_count/planning_source/shape | title/description/instruction |
| STEP_STARTED | status/agent_id/execution_kind/output_policy/dependency_count | instruction |
| STEP_COMPLETED | status/duration/result_char_count/delivery_status/delivery_duration/execution_kind/output_policy/safe_error_code | StepResult 正文 |
| OUTPUT_DELTA | 仅 `text_digest` + `text_length` | 正文永不保存 |
| ERROR / RUN_COMPLETED | RunStatus/StopReason/safe_error_code/delivery_status/final_step_status/memory_commit_status | exception 原文、路径、结果正文 |

新字段全部加入 `_LEGACY_OPTIONAL_JOURNAL_FIELDS`，旧 schema v1/v2 记录可继续读取；写端仍产出完整字段。

Reducer（`LimitedJournalTailReducer`）新增投影：planning_started、plan_created、plan_shape、final step 状态、output publication attempted/journaled、delivery known/unknown、Memory commit 结果、Run terminal。要求：

- 不把 OUTPUT digest 当作正文恢复；
- unknown 事件类型 fail closed 或安全忽略；
- sequence 重复/倒退拒绝（`JournalTailValidator`）；
- partial persisted 事件保留事实；
- reducer 不产生第二次交付动作；不能把 `PUBLISHED` 推断为用户确认阅读。

### 6.3 Memory 原子性与幂等（`core/memory_manager.py`、`final_memory_writer.py`）

`MemoryManager.append_exchange_atomic`：

- 单连接 `BEGIN IMMEDIATE` 事务，user + assistant 全成功或全失败；
- `message_exchanges` 表 + `exchange_id`/`run_id` 唯一约束作为幂等键；
- 同一 Run 重复提交抛 `DUPLICATE_EXCHANGE`，绝不重发用户正文；
- 历史读取（get_chat_history/get_messages_for_summary/get_all_messages/search_messages）只返回 legacy 消息或 `COMMITTED` exchange，不读取不完整 exchange。

`RunFinalMemoryWriter.write_delivered`：

- 只在 OutputGate 返回 DELIVERED 后被调用；
- 写一次不可重试：失败后 `_written` 保持 True，同一 Run 再次调用被拒绝；
- 不存储 raw StepResult，只保存最终 delivered exchange；
- 每次提交产生 `runtime.final_memory_commit` span 与 `runtime_final_memory_commit_*` 指标。

### 6.4 Delivery/Memory 分层与前端展示

四层状态在 Event/Trace/Journal/前端中区分：

```text
Final Step: SUCCEEDED
Delivery: DELIVERED
Memory: FAILED
Run: FAILED
error_code: FINAL_OUTPUT_MEMORY_COMMIT_FAILED
```

前端文案（`multi_agent_status.py`）：

- `FINAL_OUTPUT_DELIVERY_FAILED` -> 最终回答未能进入消息通道。
- `FINAL_OUTPUT_DELIVERY_UNKNOWN` -> 最终回答的交付状态无法确认。请先检查当前对话，避免重复执行。
- `FINAL_OUTPUT_MEMORY_COMMIT_FAILED` -> 回答已经交付，但未能保存到对话记忆。

unknown 文案绝不鼓励立即重试。

### 6.5 Runtime Projection（`core/runtime/runtime_projection.py`）

`RunProjection` 字段：planning_status/plan_status/plan_shape/active_steps/completed_steps/synthesis_status/delivery_status/memory_commit_status/run_status/stop_reason/safe_error_code/output_journaled。

`RuntimeProjectionBuilder` 合同：

- 同一 (sequence, event_id) 重复输入幂等；
- 同一 sequence 不同 event_id 拒绝；sequence 倒退拒绝；
- 未知 control event 安全忽略；
- 只由 Runtime Events 构建，无 raw 内容；不触发执行、重试或 Memory 写入。

### 6.6 Snapshot / Recovery 边界（`recovery_contract.py`、`recovery_validation.py`）

明确不可恢复边界：Bindings/StepResultStore/OutputGate/raw final 不持久化；DeliveryStatus 只能从 Journal 事实验证；Memory commit 不能由 Recovery 自动重试；`OUTCOME_UNKNOWN` 永远 fail closed。

Recovery 判定（新增 RecoveryReason）：

| 场景 | 结果 |
| --- | --- |
| POST_PLAN_PRE_EXECUTION（bindings 缺失） | UNSUPPORTED，fail closed |
| specialist 执行中断 | REQUIRES_RECONCILIATION，不恢复 Store/result |
| Final Step SUCCEEDED 但无 OUTPUT journal 事实 | UNSUPPORTED（FINAL_OUTPUT_JOURNAL_FACT_MISSING） |
| OUTPUT journaled 但无 terminal | REQUIRES_RECONCILIATION（FINAL_OUTPUT_DELIVERY_UNKNOWN），不重发、不自动写 Memory |
| DELIVERED 已知、Memory commit 未知 | REQUIRES_RECONCILIATION（FINAL_OUTPUT_MEMORY_COMMIT_UNKNOWN），人工协调、防重复 exchange |
| Memory commit 成功但 terminal 缺失 | REQUIRES_RECONCILIATION（MEMORY_COMMITTED_WITHOUT_TERMINAL），不重写、不重发 |

## 7. 实际实现

### 7.1 事件与 Journal

- `events.py`：STEP_STARTED/STEP_COMPLETED/PLAN_CREATED/ERROR/RUN_COMPLETED 增加分层安全字段（全部 legacy 可选）；`to_journal_dict` 对 OUTPUT_DELTA 只写 `text_length` + `text_digest`。
- `run_coordinator.py`：PLAN_CREATED 带 shape；ERROR/RUN_COMPLETED 通过 `_layered_terminal_facts` 推导四层终态事实（只在可证明边界填写，不虚构）。
- `stream_adapter.py`：STEP/PLAN/RUN 新安全字段进入 control allowlist，RUN_COMPLETED 携带 safe_error_code 与分层事实。

### 7.2 Trace

- `tracing.py`：operation 校验放开 `.`（`_OPERATION = ^[A-Za-z0-9_.-]{1,128}$`）；SAFE_SPAN_ATTRIBUTES 扩展到全部 WP5 属性。
- `run_coordinator.py`：run span 用 `runtime.run`、planner span 用 `runtime.planning`，并写入 plan/planning 安全属性。
- `parallel_execution.py`：step span 用 `runtime.step`，写入 preferred_agent/execution_kind/output_policy/dependency_count/state/result_char_count。
- `multi_agent_driver.py`：synthesis 步骤创建 `runtime.synthesis` span（child 为 step span）。
- `output_gate.py`：`runtime.output_delivery` span + delivery 指标 + `partially_persisted` 标记。
- `final_memory_writer.py`：`runtime.final_memory_commit` span + Memory 指标。

### 7.3 Metrics

新增/确认：

- Step：`runtime_step_total{execution_kind,output_policy,status}`、`runtime_step_duration_seconds{execution_kind,output_policy,status}`；
- Multi-Agent：`runtime_multi_agent_runs_total{shape,status}`、`runtime_specialist_count{shape}`（Histogram）、`runtime_synthesis_total{status}`；
- Delivery：`runtime_output_delivery_total{status,error_code}`、`runtime_output_delivery_duration_seconds{status}`、`runtime_output_partial_persisted_total`；
- Memory：`runtime_final_memory_commit_total{status,error_code}`、`runtime_final_memory_commit_duration_seconds{status}`；
- Executor：`runtime_blocking_executor_pending`、`runtime_blocking_executor_wait_seconds`（保留）。

标签策略：error_code 来自有限枚举（bounded_values）；agent_id 只有固定 Registry Agent 才可作为受控标签（否则聚合为 `other`）；禁止 run_id/trace_id/step_id/session_id/path/query 作为标签。

### 7.4 前端

- `main.py` 不再自行字符串拼装状态，改由 `format_frontend_status` 共享模型处理；
- 多 Agent 并行时多个 `STEP_STARTED` 显示多个 active specialist；
- OUTPUT_DELTA 继续只进入聊天正文；control event 只更新状态组件；
- RUN_COMPLETED 输出分层终态文案；Legacy 编排事件保持兼容。

## 8. 安全、失败与兼容合同

### 8.1 事件序列（WP5 E2E 真实验证）

Multi-Agent 成功（Shape 3）：

```text
RUN_STARTED
PLANNING_STARTED
PLAN_CREATED(shape=3)
STEP_STARTED(code) STEP_STARTED(knowledge)      # 并行 sibling
STEP_COMPLETED(code, SUCCEEDED)
STEP_COMPLETED(knowledge, SUCCEEDED)
STEP_STARTED(synthesis)
OUTPUT_DELTA                                   # journal 只存 digest/length
STEP_COMPLETED(synthesis, SUCCEEDED, delivery_status=DELIVERED)
RUN_COMPLETED(SUCCEEDED, delivery=DELIVERED, memory=SUCCEEDED, shape=3)
```

DELIVERED + Memory failed：

```text
... final Driver success
OUTPUT_DELTA journaled
STEP_COMPLETED(synthesis, SUCCEEDED, delivery_status=DELIVERED)
ERROR(FINAL_OUTPUT_MEMORY_COMMIT_FAILED, delivery=DELIVERED, memory=FAILED)
RUN_COMPLETED(FAILED, delivery=DELIVERED, memory=FAILED)
```

Delivery unknown：

```text
... final Driver success
OUTPUT_DELTA journaled
enqueue 失败 -> partially_persisted
STEP_COMPLETED(synthesis, SUCCEEDED, delivery_status=OUTCOME_UNKNOWN)
ERROR(FINAL_OUTPUT_DELIVERY_UNKNOWN)
RUN_COMPLETED(FAILED, delivery=OUTCOME_UNKNOWN, memory=NOT_ATTEMPTED)
```

### 8.2 四层状态映射

| 场景 | Final Step | Delivery | Memory | Run | error_code |
| --- | --- | --- | --- | --- | --- |
| delivered | SUCCEEDED | DELIVERED | SUCCEEDED | SUCCEEDED | 无 |
| known failed | SUCCEEDED | FAILED | NOT_ATTEMPTED | FAILED | FINAL_OUTPUT_DELIVERY_FAILED |
| unknown | SUCCEEDED | OUTCOME_UNKNOWN | NOT_ATTEMPTED | FAILED | FINAL_OUTPUT_DELIVERY_UNKNOWN |
| Memory commit failed | SUCCEEDED | DELIVERED | FAILED | FAILED | FINAL_OUTPUT_MEMORY_COMMIT_FAILED |

Memory 失败不得描述为 Agent 执行失败；DELIVERED 不描述为用户确认阅读。

### 8.3 敏感数据边界

安全标记：`SECRET_USER_INSTRUCTION`、`SECRET_SPECIALIST_RESULT`、`SECRET_SYNTHESIS_INPUT`、`SECRET_FINAL_OUTPUT`、`\\internal\private\case.dat`。真实 Shape 3 主链扫描（`test_wp5_security_matrix.py`）：

| 通道 | User instruction | Specialist result | Synthesis input | Final output |
| --- | :---: | :---: | :---: | :---: |
| Binding 内存 | 允许 | N/A | 允许 | N/A |
| StepResultStore | 否 | 允许 | N/A | 允许 |
| OUTPUT 事件 | 否 | 否 | 否 | 允许一次 |
| Journal | 否 | 否 | 否 | digest/length only |
| Trace | 否 | 否 | 否 | length/status only |
| Metrics | 否 | 否 | 否 | 否 |
| Snapshot / Checkpoint | 否 | 否 | 否 | 否 |
| Error/repr/log | 否 | 否 | 否 | 否 |
| Memory | 原始 user 仅 delivered exchange | 否 | 否 | delivered final only |
| Frontend control state | 否 | 否 | 否 | 否 |
| Chat 正文 | 用户原输入由 UI 已有 | 否 | 否 | 允许一次 |

### 8.4 兼容策略

- 事件 schema 保持 v2；新增 payload 字段全部 legacy 可选，旧记录可读；
- Memory schema 通过 `ALTER TABLE` 迁移（exchange_id/run_id/sequence + message_exchanges 表），旧数据库可直接升级；
- Legacy 编排事件（planning_started/delegate_*/synthesis_started）文案保持；
- static/Legacy 路径不创建 OutputGate，行为不变。

## 9. 高价值 Bad Cases

### Bad Case 1：Span operation 校验不允许点号，合同命名 span 全部丢失

- 类型：真实发现（WP5 实施测试中观察到，不是生产事故）
- 触发条件：Run root span 改用 `runtime.run` 命名后，`tracing._identifier` 的正则 `^[A-Za-z0-9_-]{1,128}$` 不允许 `.`。
- 故障表现：`start_span_safely` 捕获 ValueError 后返回 Noop span，recorder.snapshot() 里找不到任何 `runtime` span，既有断言 `next(... component=="runtime")` 抛 StopIteration。
- 根因分析：把“身份 ID”与“可读 operation 命名”共用同一严格校验；点号被误当作不安全字符。
- 修复方案：新增 `_OPERATION = ^[A-Za-z0-9_.-]{1,128}$`，SpanRecord 的 operation 单独校验；trace_id/span_id/run_id 等身份字段保持原严格规则。
- 回归测试：`test_trace_contract.py` 新增 WP5 operation 命名用例；全仓 Trace/Coordinator 测试通过。
- 对应知识点：校验边界按字段语义分开，而不是一刀切。
- 面试表达：合同命名 `runtime.run` 需要点号，但身份 ID 不允许；我把两类校验拆开，避免为可读性放松身份安全。
- 当前状态：已修复。

### Bad Case 2：测试桩缺少 `append_exchange_atomic`，Run 全部以 Memory 失败收场

- 类型：真实发现（WP5 实施测试中观察到，不是生产事故）
- 触发条件：共享 fixture 的 `FakeMemoryManager` 只有旧 `add_message`，没有 WP5 新增的 `append_exchange_atomic`。
- 故障表现：Gate 已 DELIVERED 后 writer 调用 `append_exchange_atomic` 抛 AttributeError，Run 以 `FINAL_OUTPUT_MEMORY_COMMIT_FAILED` 失败，而不是预期 SUCCEEDED。
- 根因分析：生产合同升级为原子提交，但测试替身仍停留在“写入即成功”的旧假设。
- 修复方案：为 `FakeMemoryManager` 增加同语义的原子 exchange 桩（同 run_id 只允许一次，重复抛 DUPLICATE_EXCHANGE）；真实原子性测试仍用 SQLite `MemoryManager`。
- 回归测试：`test_final_memory_atomicity.py` 与全仓回归全部通过。
- 对应知识点：测试替身合同必须跟随生产合同演进。
- 面试表达：这次失败反而证明原子写入确实发生在 Gate 之后；我修的是 fixture，而不是放宽生产合同。
- 当前状态：已修复。

### Bad Case 3：历史读取 JOIN 后列名歧义

- 类型：真实发现（WP5 实施测试中观察到，不是生产事故）
- 触发条件：`get_chat_history` 增加 `LEFT JOIN message_exchanges me` 过滤不完整 exchange 后，`agent_id` 同时存在于 messages 与 message_exchanges。
- 故障表现：`sqlite3.OperationalError: ambiguous column name: agent_id`，4 个 Memory 测试失败。
- 根因分析：JOIN 引入同名列但 WHERE/SELECT 未加表别名。
- 修复方案：所有 JOIN 查询改为 `FROM messages m`，SELECT/WHERE/ORDER BY 全部用 `m.` 限定；exchange 过滤统一走 `_committed_exchange_join`/`_committed_exchange_filter`。
- 回归测试：`test_final_memory_boundary.py` 与 `test_final_memory_atomicity.py` 通过。
- 对应知识点：SQL JOIN 的列限定、复用过滤表达式的可读性。
- 面试表达：加 JOIN 必须同步限定列名，否则过滤逻辑本身没写错也会被 SQL 解析挡下。
- 当前状态：已修复。

### Bad Case 4：Driver 单元测试的 stub coordinator 缺少 `span_recorder`

- 类型：真实发现（WP5 实施测试中观察到，不是生产事故）
- 触发条件：`multi_agent_driver.py` 新增 `runtime.synthesis` span 时直接访问 `self._coordinator.span_recorder`。
- 故障表现：`_StubCoordinator` 没有该属性，synthesis 用例抛 AttributeError。
- 根因分析：生产 coordinator 有该字段，但测试替身没有；直接属性访问把实现细节泄漏进调用方。
- 修复方案：改为 `getattr(self._coordinator, "span_recorder", None)`，无 recorder 时跳过 span（旁路观测，不影响执行）。
- 回归测试：`test_multi_agent_driver.py` 与全仓通过。
- 对应知识点：可选旁路依赖的防御式访问。
- 面试表达：span 是可选的观测旁路，缺 recorder 不应改变 Driver 行为。
- 当前状态：已修复。

### Bad Case 5：旧断言把“OUTPUT journaled 无 terminal”当作 RESUMABLE

- 类型：真实发现（WP5 合同演进，不是生产事故）
- 触发条件：`test_recovery_validation.py` 原有用例只在 Journal 追加 OUTPUT_DELTA 后断言 RESUMABLE。
- 故障表现：按 WP5 新语义，OUTPUT journaled 但无 terminal 表示交付结果不确定，必须 REQUIRES_RECONCILIATION；旧断言与新合同冲突。
- 根因分析：WP4 之前没有“交付尝试已发生但终态未知”的 Recovery 判定，旧测试固化的是旧语义。
- 修复方案：按 WP5 合同更新该用例为 REQUIRES_RECONCILIATION + FINAL_OUTPUT_DELIVERY_UNKNOWN；新增 `test_recovery_delivery_boundary.py` 覆盖全部交付边界。
- 回归测试：Recovery 专项与全仓通过。
- 对应知识点：合同演进时旧断言必须显式迁移，而不是用 skip 掩盖。
- 面试表达：语义升级比加功能更容易踩到旧测试；我把断言更新为与合同一致，并保留真实证据。
- 当前状态：已修复。

## 10. 测试和验收证据

WP5 专项：

| 测试文件 | 重点 | 结果 |
| --- | --- | --- |
| `tests/test_trace_contract.py`（扩展） | 合同命名、安全属性集、delivery/memory span 属性 | 通过 |
| `tests/test_multi_agent_trace.py` | Shape 3 sibling/synthesis/delivery/memory span 拓扑、唯一终结 | 通过 |
| `tests/test_delivery_observability.py` | delivery span、delivery/partial 指标、duplicate 拒绝 | 通过 |
| `tests/test_journal_safe_projection.py` | OUTPUT digest/length、allowlist、sequence、reducer 幂等 | 通过 |
| `tests/test_runtime_projection.py` | 全链路投影、幂等、sequence 冲突/倒退、无 raw | 通过 |
| `tests/test_final_memory_atomicity.py` | 原子提交、重复拒绝、不完整 exchange 过滤、写一次不重试 | 通过 |
| `tests/test_recovery_delivery_boundary.py` | POST_PLAN、specialist 中断、delivery/Memory 边界不重发不重写 | 通过 |
| `tests/test_frontend_multi_agent_status.py` | 分层文案、unknown 不鼓励重试、Legacy 兼容 | 通过 |
| `tests/test_wp5_security_matrix.py` | 真实 Shape 3 主链全通道泄漏扫描 | 通过 |
| `tests/test_metrics_label_policy_wp5.py` | 新指标、bounded error_code、agent_id 受控、无高基数 | 通过 |

回归与全仓：

| 命令 | 结果 |
| --- | --- |
| WP1-WP4 关键回归（dynamic lifecycle、multi-agent execution、step completion、recovery、observability、trace 等） | 全部通过 |
| `uv run pytest -q` | 1346 passed, 42 subtests passed |
| `uv run python -m compileall -q core tests server.py main.py` | PASS |
| `git diff --check` | PASS（仅 Git LF/CRLF 提示，无 whitespace error） |

实施中的失败与修复都被保留（见 Bad Case 1-5）；没有删除旧测试、没有用 skip 绕过 UI/Journal/Recovery、没有用正文 hash 替代泄漏扫描、没有把 unknown 提示为普通可重试失败。

## 11. 当前能力边界

WP5 已完成：

- Trace Contract v1（六类 span、父子关系、敏感属性边界、版本归因）；
- Journal 安全事实（OUTPUT digest/length、分层 allowlist、reducer 幂等投影）；
- delivered-only Memory 原子提交与幂等键；
- Delivery/Memory 分层观测与前端文案；
- Runtime Projection（幂等 + sequence 合同）；
- 前端最小多 Agent 状态展示（兼容 Legacy）；
- Snapshot/Recovery 交付边界（不重发、不重写、OUTCOME_UNKNOWN fail closed）；
- 安全矩阵（真实 Shape 3 主链）。

WP5 尚未完成（本轮不开始 WP6）：

- Planning executor 饥饿 P2 仍存在（未扩大资源调度范围）；
- Gate/Store/Bindings 不可恢复；exactly-once delivery 未实现；DELIVERED 非用户确认；
- AgentEvalOps 尚未正式接入；完整故障矩阵与 RC Gate 属于 WP6；Stage 2.5 尚未最终完成。

## 12. 面试表达版本

### 12.1 30 秒版本

WP5 给 Stage 2.5 多 Agent 补齐可观测、可审计、客户端可解释的最后一层：冻结 Trace Contract v1（run/planning/step/synthesis/delivery/memory 六类 span），Journal 只保存安全事实（OUTPUT 仅 digest/length），delivered-only Memory 升级为 SQLite 单事务原子提交，新增共享 Runtime Projection 与前端状态文案模型，并强化 Recovery“不重发、不重写”边界。WP5 专项 10 个文件全过，全仓 1346 passed + 42 subtests passed。

### 12.2 2 分钟版本

WP5 之前，多 Agent 已能输出唯一 final answer，但观测通道不完整：span 命名不允许点号、STEP/PLAN/RUN 事件没有分层事实、Memory 两次写入非事务、Recovery 对“交付已尝试但终态未知”没有专门判定、前端只消费 Legacy 事件。

第一是 Trace Contract v1：六类 span 固定命名，Shape 3 并行 specialist 是 sibling，synthesis 在依赖结束后开始；属性走 SAFE/DENIED 双集合，缺失版本写 not_configured。第二是 Journal 安全投影：OUTPUT 只存 digest/length，STEP/PLAN/RUN 增加 legacy 可选的分层字段，reducer 投影 planning/delivery/Memory 事实且幂等。第三是 Memory 原子性：append_exchange_atomic 单事务 + exchange_id/run_id 幂等键，writer 写一次不可重试，历史读取过滤不完整 exchange。第四是分层观测：delivered+Memory failed 时用户看到“回答已交付，记忆保存失败”，unknown 文案明确避免重复执行。第五是 Recovery：OUTPUT journaled 无 terminal、delivery unknown、Memory unknown 全部 fail closed，不重发不重写。

实现中修复了五个真实问题（operation 校验、fixture 缺原子方法、SQL 列歧义、stub 缺 span_recorder、旧断言与新合同冲突）。最终 WP5 专项全过、全仓 1346 passed + 42 subtests passed，P0/P1 为零，P2 为 1（Planning 饿死）。

### 12.3 深入追问主线（含参考答案）

1. Trace Contract v1 为什么用固定 operation 命名？父子关系如何保证不伪造嵌套 specialist？
   - 回答：固定命名让 AgentEvalOps 等下游无需解析自由字符串即可按类型消费（`trace_contract.py` 定义 `runtime.run` 等常量）。父子关系由 `start_span_safely(parent_context=current_trace_context())` 在创建时确定：每个 specialist 的 step span 都显式以 Run root span 为 parent，彼此 sibling；synthesis span 只在 synthesis step 内创建。`test_multi_agent_trace.py` 断言 specialist span 的 parent 都是 root、span_id 互不相同、synthesis span 的 started_at 不早于所有依赖 specialist 的 completed_at。
2. Journal 为什么只保存 OUTPUT digest/length？如何兼容旧记录？
   - 回答：Journal 是恢复与审计的事实源，不是原始数据归档；保存正文会让用户正文进入本不该进入的持久化通道，且无法保证与 Trace/Metrics 等边界一致。`to_journal_dict` 对 OUTPUT_DELTA 只写 `text_length` + `text_digest`（SHA-256）。兼容性通过 `_LEGACY_OPTIONAL_JOURNAL_FIELDS` 实现：新增字段全部 optional，`validate_journal_payload` 对旧记录允许缺字段；`test_journal_safe_projection.py` 验证旧 schema 记录仍可读。
3. Memory 原子性如何实现？为什么失败后不重试？
   - 回答：`append_exchange_atomic` 在单连接 `BEGIN IMMEDIATE` 事务里插入 exchange 行与 user/assistant 两条消息，任何失败整体 ROLLBACK；`exchange_id`/`run_id` 唯一约束防止同一 Run 重复提交。失败后不重试是因为 Gate 已 PUBLISHED、Step 已 SUCCEEDED，重试可能重复写入用户正文；`RunFinalMemoryWriter` 的 `_written` 失败后保持 True，同一 Run 再次调用被拒绝（`test_final_memory_atomicity.py` 覆盖）。
4. DELIVERED + Memory failed 为什么 Run 仍 FAILED？前端如何展示分层？
   - 回答：Run 终态是四层状态的汇总，Memory 是 delivered 后的持久化义务；写失败不等于回答未交付，但也不能宣称完整成功，所以 Run=FAILED、error_code=FINAL_OUTPUT_MEMORY_COMMIT_FAILED、Step 与 Delivery 保持 SUCCEEDED/DELIVERED。前端 `multi_agent_status.py` 对 RUN_COMPLETED 先看 memory_commit_status，FAILED 时显示“回答已交付，记忆保存失败”，而不是笼统的“运行失败”。
5. Runtime Projection 的幂等与 sequence 合同？
   - 回答：`RuntimeProjectionBuilder` 记录 (sequence, event_id)；同一 sequence+event_id 重复输入直接返回当前投影（幂等）；同一 sequence 不同 event_id 抛 PROJECTION_SEQUENCE_CONFLICT；sequence 不大于 last_sequence 抛回归错误；未知 control event 安全忽略并推进 sequence。投影只由事件构建、无 raw 内容、不触发任何执行/重试/Memory 写入（`test_runtime_projection.py`）。
6. Recovery 为什么对 delivery unknown 永远 fail closed？如何保证不重发不重写？
   - 回答：OUTPUT journaled 后无法证明消费者是否收到，重试会重复回答；因此 `OUTCOME_UNKNOWN` 与“OUTPUT journaled 但无 terminal”都判定 REQUIRES_RECONCILIATION，DELIVERED 已知但 Memory unknown 也标记人工协调。实现上 `RecoveryAssessment` 的所有 replay 标志（automatic_resume/model/tool/retrieval_replay/output_reconstruction）恒为 False，且 `test_recovery_delivery_boundary.py` 逐场景断言不重发、不重写。
7. 安全矩阵如何证明真实主链无泄漏？
   - 回答：`test_wp5_security_matrix.py` 用真实 Shape 3 主链注入五类敏感标记（user instruction/specialist result/synthesis input/final/path），运行完整 Run 后对 Journal、Trace、Metrics、Snapshot、Stream control、前端状态文案逐通道断言不含标记；正文通道只允许唯一 OUTPUT_DELTA，Memory 只允许 delivered user+final。这不是用 digest 匹配替代扫描，而是全通道真实断言。
8. 指标标签为什么低基数？agent_id 为什么受控？
   - 回答：高基数标签会让每个 Run/Step 产生独立序列，聚合与告警失去意义。`MetricLabelPolicy` 拒绝 run_id/trace_id/step_id/session_id/path/query 等标签；error_code 用 bounded_values 限定有限枚举；agent_id 只有固定 Registry Agent 才作为受控标签，未知值聚合为 `other`，保证序列数量有限（`test_metrics_label_policy_wp5.py`）。

## 13. 最终验收结论

```text
WP5 status: PASS
Architecture deviations: 0
P0 findings: 0
P1 findings: 0
P2 findings: 1
Trace Contract v1 enabled: YES
Multi-agent span topology correct: YES
Journal raw-content boundary enforced: YES
Runtime projection enabled: YES
Delivery and Memory status observable: YES
Delivered final Memory consistency protected: YES
Frontend multi-agent status enabled: YES
Delivery unknown warns against retry: YES
Recovery never re-delivers final output: YES
Recovery never re-commits final Memory: YES
WP5 security matrix passed: YES
WP6 capabilities implemented: NO
Ready for GPT review: YES
Ready to start WP6: YES
```
