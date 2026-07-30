# 阶段二第 24 天：Fault Injection、Chaos Matrix 与 Resilience Validation

**当前进度：第 24/25 天。**

第 23 天已经完成默认 Coordinated Runtime（协调式运行时）、客户端断连、Worker 生命周期、Graceful Shutdown（优雅关闭）和离线 ASGI（异步服务器网关接口）闭环，但明确没有实现第 24 天的 Fault Injection Framework（故障注入框架）。

第 24 天的目标是：

> 不再只依赖手工编写的单点失败测试，而是建立一个确定性、可审计、默认关闭的故障注入框架，用统一故障矩阵证明 Runtime 在各类失败窗口下仍然遵守状态、预算、副作用、事件和资源清理不变量。

------

# 一、本日准确能力边界

本日最终实现：

```text
Deterministic Fault Injection
+ Failure Scenario Matrix
+ Runtime Invariant Validation
+ Resilience Report
→ 可重复的韧性验证
```

本日不实现：

-生产环境随机 Chaos；
-对外 Fault Injection API；
-真实网络断网、丢包或限速；
-进程 Kill、机器重启、磁盘拔出；
-真实模型、真实 Tool 或真实外部服务破坏；
-自动 Recovery / Replay；
-自动 Compensation；
-Exactly-once；
-跨进程故障协调；
-修改 Model、Tool、RAG 的业务语义。

## Tool Call 规则

本日不会修改 Tool Call（工具调用）的重试、幂等、副作用或补偿逻辑。

只在 Runtime 服务边界增加可注入接缝，并使用现有 Fake Tool（模拟工具）或本地安全模拟器验证，因此不触发“暂停学习、先生成复杂 Tool”的规则。

------

# 二、为什么需要 Fault Injection

当前已有很多失败测试，但这些测试通常是：

```text
某个 Fake 固定抛异常
→ 验证一个目标行为
```

问题在于：

-不同模块各自定义失败方式；
-触发时间点不统一；
-很难表达“第 N 次调用失败”；
-很难复现 State Commit 与 Event Publish 之间的窗口；
-难以确认同一故障是否影响 Model、Tool、Journal、Shutdown 等多个层级；
-缺少一份完整 Failure Matrix（故障矩阵）。

故障注入框架需要把失败描述为：

```text
在哪里失败
+ 什么时候失败
+ 第几次失败
+ 失败前是否已有副作用
+ 失败持续多久
+ 最多触发几次
+ 预期 Runtime 如何收口
```

------

# 三、Fault Injection 与 Chaos Testing 的区别

## Fault Injection

Fault Injection（故障注入）强调：

-确定的注入点；
-确定的触发条件；
-确定的故障行为；
-可重复结果；
-可精确断言。

例如：

```text
MODEL_PROVIDER_CALL
第 1 次调用
调用前抛 TRANSIENT_ERROR
最多触发 1 次
```

## Chaos Testing

Chaos Testing（混沌测试）更强调：

-系统性扰动；
-多组件组合；
-随机或概率触发；
-长期运行；
-生产或类生产环境韧性。

第 24 天只实现：

```text
Deterministic Fault Injection
+ Offline Chaos Matrix
```

不实现生产随机 Chaos。

------

# 四、故障注入的核心架构

建议：

```text
FaultInjectionController
├── FaultPlan
├── FaultRule
├── FaultMatchContext
├── FaultAction
├── FaultDecision
├── FaultInjectionRecorder
└── FaultInjectionScope
```

调用链：

```text
Runtime Component
→ reach fault point
→ controller.evaluate(context)
→ no match：正常执行
→ match：执行 FaultAction
→ 记录安全 FaultDecision
→ Runtime 按原有失败合同处理
```

故障框架不能接管 Runtime 状态机。

它只负责：

```text
注入一个预先定义的失败
```

后续：

- Retry；
  -Fallback；
  -Cancellation；
  -RunStatus；
  -Terminal；
  -Budget；
  -Journal；
  -Tool Side Effect；

仍由原来的 Owner 决定。

------

# 五、Fault Point

建立固定 `FaultPoint` Enum（枚举），禁止业务代码使用自由字符串。

建议至少覆盖以下区域。

## 1. Model

```text
MODEL_BEFORE_INVOCATION
MODEL_BEFORE_PROVIDER_CALL
MODEL_AFTER_PROVIDER_SUCCESS
MODEL_BEFORE_USAGE_COMMIT
MODEL_AFTER_USAGE_COMMIT
MODEL_BEFORE_ATTEMPT_EVENT
```

## 2. Tool

```text
TOOL_BEFORE_INVOCATION
TOOL_BEFORE_ATTEMPT
TOOL_BEFORE_PROVIDER_CALL
TOOL_AFTER_PROVIDER_RETURN
TOOL_BEFORE_SIDE_EFFECT_COMMIT
TOOL_AFTER_SIDE_EFFECT_COMMIT
TOOL_BEFORE_COMPLETION_EVENT
```

`TOOL_AFTER_SIDE_EFFECT_COMMIT` 风险很高，只允许：

-测试模式；
-明确安全模拟 Tool；
-显式开启危险窗口；
-不得对真实外部 Tool 使用。

## 3. Retrieval

```text
RETRIEVAL_BEFORE_REWRITE
RETRIEVAL_AFTER_REWRITE
RETRIEVAL_BEFORE_SEARCH
RETRIEVAL_AFTER_SEARCH
RETRIEVAL_BEFORE_STAGE_EVENT
RETRIEVAL_BEFORE_RESULT_COMMIT
```

## 4. Runtime Event / Journal

```text
EVENT_BEFORE_JOURNAL_APPEND
EVENT_AFTER_JOURNAL_APPEND
EVENT_BEFORE_CHANNEL_ENQUEUE
JOURNAL_BEFORE_READ
JOURNAL_BEFORE_TERMINAL_APPEND
```

## 5. Snapshot / Recovery

```text
SNAPSHOT_BEFORE_SAVE
SNAPSHOT_AFTER_SAVE
SNAPSHOT_BEFORE_READ
RECOVERY_BEFORE_TAIL_READ
RECOVERY_AFTER_TAIL_READ
```

本日可以测试失败，但不能自动 Recovery。

## 6. Executor / Channel

```text
EXECUTOR_BEFORE_SUBMIT
EXECUTOR_AFTER_SUBMIT
CHANNEL_BEFORE_PUBLISH
CHANNEL_BEFORE_RECEIVE
CHANNEL_BEFORE_DRAIN_HANDOFF
```

## 7. Observability / Trace

```text
OBSERVABILITY_BEFORE_RECORD
OBSERVABILITY_BEFORE_FLUSH
TRACE_BEFORE_SPAN_START
TRACE_BEFORE_SPAN_END
TRACE_BEFORE_FLUSH
```

## 8. Shutdown

```text
SHUTDOWN_BEFORE_RUN_CANCEL
SHUTDOWN_BEFORE_WORKER_DRAIN
SHUTDOWN_BEFORE_JOURNAL_CLOSE
SHUTDOWN_BEFORE_MODEL_CLOSE
SHUTDOWN_COMPONENT_CLOSE
```

------

# 六、Fault Action

建议支持以下确定性动作。

## 1. Raise

```text
RAISE_TYPED_ERROR
```

只抛预定义安全异常，例如：

```text
TRANSIENT
RATE_LIMIT
TIMEOUT
PERMANENT
JOURNAL_FAILED
STORE_FAILED
ENCODING_FAILED
COMPONENT_CLOSE_FAILED
```

不得直接传播用户提供的异常对象。

## 2. Delay

```text
DELAY
```

要求：

-使用有限秒数；
-拒绝负数、NaN、Infinity 和 bool；
-等待可响应 Cancellation；
-测试优先使用 Barrier，而非真实 sleep。

## 3. Block

```text
BLOCK_UNTIL_RELEASED
```

用于确定性构造竞态：

```text
State 已提交
→ Event 尚未发布
```

必须由测试显式释放，且自身有超时。

## 4. Return Safe Failure

```text
RETURN_TYPED_FAILURE
```

用于 Adapter 或 Store 等已经采用返回值合同的组件。

不能把原本抛异常的接口随意改成返回错误。

## 5. Corrupt Test Fixture

```text
CORRUPT_TEST_FIXTURE
```

只允许作用于：

-内存副本；
-临时 SQLite；
-临时 Snapshot；
-测试 Journal Record。

不得直接修改生产 Store。

## 6. Drop

默认不实现通用：

```text
DROP_EVENT
```

因为静默丢弃事件很容易绕过 Journal、Sequence 和 Terminal 合同。

需要测试丢失时，应使用明确的：

```text
EVENT_ENQUEUE_FAILED
JOURNAL_APPEND_FAILED
```

而不是静默 Drop。

------

# 七、Fault Trigger

建议支持：

```text
ALWAYS
ON_NTH_MATCH
FIRST_MATCH
AFTER_N_MATCHES
UNTIL_MAX_HITS
```

例如：

```text
FaultPoint.MODEL_BEFORE_PROVIDER_CALL
ON_NTH_MATCH = 2
max_hits = 1
```

表示：

> 第二次匹配该调用点时注入一次故障。

## Scope

必须支持：

```text
GLOBAL_TEST_SCOPE
RUN_SCOPE
INVOCATION_SCOPE
ATTEMPT_SCOPE
STEP_SCOPE
COMPONENT_SCOPE
```

生产默认只能是：

```text
disabled
```

不能通过全局可变变量让一个测试的故障泄漏到另一个并行测试。

------

# 八、匹配上下文

`FaultMatchContext` 只允许安全字段：

```text
fault_point
component
run_id_digest
step_id
invocation_id_digest
attempt_number
runtime_mode
event_type
operation_kind
side_effect_phase
checkpoint_kind
shutdown_component
```

不得包含：

- Prompt；
  -Message；
  -Model Output；
  -Tool Arguments；
  -Tool Output；
  -RAG Chunk；
  -Memory；
  -API Key；
  -Provider URL；
  -本地路径；
  -原始异常。

一般测试匹配优先使用：

```text
fault_point
component
attempt_number
event_type
```

避免依赖高基数身份。

------

# 九、故障框架必须满足的不变量

## 1. 默认关闭

```text
FaultInjection disabled
→ 不增加业务分支行为
→ 不改变正常结果
```

## 2. 明确启用

只能通过：

-测试 Fixture；
-构造函数显式注入；
-受控测试配置。

不得读取：

```text
用户请求参数
HTTP Header
Prompt
Tool Argument
```

来启用故障。

## 3. 不成为业务 Owner

Fault Controller 不得：

-修改 `AgentState`；
-修改 Budget；
-发布 Terminal；
-执行 Retry；
-执行 Compensation；
-查询 Tool Registry；
-修改 Side Effect State；
-重新调用 Model/Tool/RAG。

## 4. 可重复

同一 Fault Plan 与同一调用序列必须得到相同触发结果。

## 5. 并发安全

多个 Step 并行匹配同一 Rule 时：

-命中计数原子；
-`max_hits=1` 只能触发一次；
-不能两个 Worker 都认为自己是第一次；
-不同 Run 的 run-scoped Rule 隔离。

## 6. 清理安全

测试结束后：

```text
FaultInjectionScope.close()
→ 所有 Blocker 被释放
→ 所有 Rule 停止生效
→ 无 Watcher/Task 泄漏
```

------

# 十、故障矩阵

## Model

| 注入位置              | 故障      | 预期                  |
| --------------------- | --------- | --------------------- |
| Provider 前           | Transient | 进入原 Retry          |
| Provider 前           | Permanent | Attempt 失败          |
| Provider 后、Usage 前 | Failure   | 不伪造 Usage          |
| Usage Commit 后       | Failure   | 不重复记账            |
| Fallback Attempt      | Failure   | 不进入 Legacy Runtime |

## Tool

| 注入位置               | 故障            | 预期                   |
| ---------------------- | --------------- | ---------------------- |
| Invocation 前          | Failure         | Tool 不调用            |
| Provider 前            | Timeout         | 按原 Tool Timeout      |
| Provider 后、Commit 前 | Failure         | Side Effect 未提交     |
| Commit 后              | Failure         | Outcome Unknown / 对账 |
| Completion Event 前    | Journal Failure | 不重新执行 Tool        |
| Detached Worker        | Timeout         | 保留真实 Worker        |

## Retrieval

| 注入位置    | 故障            | 预期                 |
| ----------- | --------------- | -------------------- |
| Rewrite 前  | Failure         | 按既有降级合同       |
| Rewrite 后  | Failure         | 不重复 Model Rewrite |
| Search 前   | Timeout         | 无向量查询结果       |
| Search 后   | Event Failure   | 不重新搜索           |
| Stage Event | Journal Failure | 不重复 RAG           |

## Event / Journal

| 注入位置              | 故障            | 预期                     |
| --------------------- | --------------- | ------------------------ |
| Append 前             | Journal failure | Event 未持久化           |
| Append 后、Enqueue 前 | Enqueue failure | Sequence 已消费          |
| Terminal append       | Failure         | Cleanup 继续，不重跑业务 |
| Tail read             | Corruption      | Recovery fail closed     |

## Shutdown

| 注入位置            | 故障     | 预期                   |
| ------------------- | -------- | ---------------------- |
| Run cancel          | Failure  | 继续取消其他 Run       |
| Worker drain        | Timeout  | 保留 Detached          |
| Observability flush | Failure  | 继续后续关闭           |
| Journal close       | Failure  | 继续 Model/Store close |
| Model close         | Deferred | Report 不伪报成功      |

------

# 十一、本日四轮拆分

第 24 天代码量较大，继续拆为四轮。

## 第一轮：Fault Injection Foundation

完成：

- `FaultPoint`；
  -`FaultAction`；
  -`FaultTrigger`；
  -`FaultRule`；
  -`FaultPlan`；
  -`FaultInjectionController`；
  -`FaultInjectionScope`；
  -安全 Recorder；
  -并发命中计数；
  -默认关闭；
  -不接入生产业务组件。

## 第二轮：Model、Tool、Retrieval 接入

完成：

- Model Injection Seam（注入接缝）；
  -Tool 安全注入点；
  -Retrieval 注入点；
  -Retry/Fallback/Side Effect 不变量；
  -不改变业务语义。

## 第三轮：Journal、Channel、Snapshot、Observability、Shutdown

完成：

-基础设施故障；
-Terminal Journal Failure；
-Channel Enqueue/Drain；
-Snapshot Corruption Fixture；
-Flush/Close Failure；
-Shutdown Failure Matrix。

## 第四轮：Chaos Matrix 与最终验收

完成：

-组合故障；
-并行命中；
-Cancellation 与 Failure 竞态；
-故障覆盖报告；
-Runtime Invariant Report；
-全仓回归；
-第 24 天结果文档。

------

# 十二、重点 Bad Case

## Bad Case 1：Fault Injection 默认启用

会使生产请求随机失败。

## Bad Case 2：通过用户 Prompt 选择 Fault Point

形成未授权的故障控制接口。

## Bad Case 3：随机数未固定 Seed

测试不可重复。

本日第一版直接不实现随机触发。

## Bad Case 4：Rule Hit Count 并发双触发

`max_hits=1` 在两个并行 Step 中触发两次。

## Bad Case 5：故障框架修改 AgentState

形成第二套 Runtime State Owner。

## Bad Case 6：Journal Failure 后重新执行 Tool

导致副作用重复。

## Bad Case 7：在真实 Tool Commit 后进行危险注入

可能制造真实业务不确定状态。

## Bad Case 8：Delay 不响应 Cancellation

故障测试本身导致测试永久挂起。

## Bad Case 9：Blocker 忘记释放

全仓测试卡死。

## Bad Case 10：Fault Context 保存 Tool Arguments

泄漏业务正文。

## Bad Case 11：故障注入记录进入 Final Output

污染用户回答。

## Bad Case 12：旧组件未注入 Controller 时行为变化

破坏 Backward Compatibility（向后兼容）。

## Bad Case 13：Fault Rule 跨测试泄漏

后续正常测试随机失败。

## Bad Case 14：把测试故障矩阵宣称为生产 Chaos 验证

真实性错误。

------

# 十三、第一轮 Codex 任务

你正在继续改造 LocalAgent 项目的阶段二 Runtime。

这是第 24 天第一轮任务：Fault Injection Foundation。

第 23 天已经完成：

- 默认 Coordinated Runtime 入口
- Application Runtime Assembly
- CoordinatedRunScope
- OUTPUT_DELTA 唯一正文
- RUN_COMPLETED 唯一 Terminal
- Client Disconnect / cancel-and-drain
- RuntimeEventChannel 单 Consumer ownership
- Admission Gate
- ActiveRunControlHandle
- GracefulShutdownCoordinator
- Coordinated / Legacy tracked worker
- ASGI E2E 与 Runtime Invariants

本轮只实现：

1. Fault Injection Contract；
2. FaultPoint；
3. FaultAction；
4. FaultTrigger；
5. FaultRule；
6. FaultPlan；
7. FaultMatchContext；
8. FaultDecision；
9. FaultInjectionController；
10. FaultInjectionScope；
11. FaultInjectionRecorder；
12. 并发安全的 Rule Hit Counter；
13. 默认关闭和安全配置；
14. Foundation 单元测试。

本轮不得：

- 接入 ModelInvocationRouter；
  -接入 ToolExecutionService；
  -接入 RetrievalExecutionService；
  -接入 RuntimeEventChannel；
  -接入 EventJournal；
  -接入 SnapshotStore；
  -接入 GracefulShutdownCoordinator；
  -修改默认 `/api/chat`；
  -修改 Tool/Model/RAG 业务语义；
  -实现生产 Fault Injection API；
  -实现随机或概率 Chaos；
  -实现自动 Recovery/Replay；
  -实现第 25 天内容。

结果文档：

```text
docs/learning/stage2/result/day24_fault_injection_foundation_result.md
```

最终第 24 天文档留到第四轮：

```text
docs/learning/stage2/result/day24_fault_injection_result.md
```

## 一、先审计现有测试接缝

至少检查：

- Model Fake / Adapter 测试方式；
  -Tool Fake / Simulator；
  -Retrieval Fake；
  -EventJournal Fake / InMemory Store；
  -RuntimeEventChannel 测试 Hook；
  -Snapshot 临时 Store；
  -Observability / Trace Fake；
  -Graceful Shutdown 测试组件；
  -Barrier / Event / Clock 测试工具；
  -Settings；
  -ApplicationRuntimeServices；
  -第 13～23 天测试和结果文档。

结果文档必须说明：

1. 当前每个模块如何构造失败；
   2.是否存在散落的 `fail_on_call`、`raise_on_*`、`Event`、`Barrier`；
   3.哪些测试接缝可以复用；
   4.哪些接缝属于业务 Fake，不应迁移到生产代码；
   5.是否已有通用安全 Error Code；
   6.是否已有测试 Clock；
   7.是否存在模块级全局故障开关；
   8.当前并行测试隔离方式。

不得为了统一而删除有明确领域语义的 Fake。

## 二、建议新增文件

建议：

```text
core/runtime/fault_injection.py
core/runtime/fault_injection_contract.py
core/runtime/fault_injection_recording.py

tests/test_fault_injection_contract.py
tests/test_fault_injection_controller.py
tests/test_fault_injection_concurrency.py
tests/test_fault_injection_security.py
```

如真实架构适合合并文件，可以调整，但必须保持 Contract、Controller 和 Recording 职责清晰。

## 三、Fault Injection 默认状态

生产默认：

```text
disabled
```

要求：

- 没有 Controller 时正常执行；
  -注入 Disabled Controller 时也正常执行；
  -不得增加用户可见输出；
  -不得改变正常 Event Sequence；
  -不得改变 Budget；
  -不得改变 Retry；
  -不得创建 RuntimeEvent；
  -不得写 Journal；
  -不得自动注册 Metrics；
  -默认路径只有一次轻量空检查，或通过 Null Object 消除条件分支。

不能通过：

- HTTP Request；
  -Prompt；
  -Tool Argument；
  -Environment Variable 的任意动态值；

开启具体 Fault Rule。

本轮可以提供测试专用构造函数，但不得接入生产 Settings。

## 四、FaultPoint

建立固定 Enum。

本轮至少定义未来需要的完整分类，但不接入组件。

建议包含：

```text
MODEL_BEFORE_INVOCATION
MODEL_BEFORE_PROVIDER_CALL
MODEL_AFTER_PROVIDER_SUCCESS
MODEL_BEFORE_USAGE_COMMIT
MODEL_AFTER_USAGE_COMMIT

TOOL_BEFORE_INVOCATION
TOOL_BEFORE_ATTEMPT
TOOL_BEFORE_PROVIDER_CALL
TOOL_AFTER_PROVIDER_RETURN
TOOL_BEFORE_SIDE_EFFECT_COMMIT
TOOL_AFTER_SIDE_EFFECT_COMMIT
TOOL_BEFORE_COMPLETION_EVENT

RETRIEVAL_BEFORE_REWRITE
RETRIEVAL_AFTER_REWRITE
RETRIEVAL_BEFORE_SEARCH
RETRIEVAL_AFTER_SEARCH
RETRIEVAL_BEFORE_RESULT_COMMIT

EVENT_BEFORE_JOURNAL_APPEND
EVENT_AFTER_JOURNAL_APPEND
EVENT_BEFORE_CHANNEL_ENQUEUE
JOURNAL_BEFORE_READ
JOURNAL_BEFORE_TERMINAL_APPEND

SNAPSHOT_BEFORE_SAVE
SNAPSHOT_AFTER_SAVE
SNAPSHOT_BEFORE_READ
RECOVERY_BEFORE_TAIL_READ
RECOVERY_AFTER_TAIL_READ

EXECUTOR_BEFORE_SUBMIT
EXECUTOR_AFTER_SUBMIT
CHANNEL_BEFORE_RECEIVE
CHANNEL_BEFORE_DRAIN_HANDOFF

OBSERVABILITY_BEFORE_RECORD
OBSERVABILITY_BEFORE_FLUSH
TRACE_BEFORE_SPAN_START
TRACE_BEFORE_SPAN_END
TRACE_BEFORE_FLUSH

SHUTDOWN_BEFORE_RUN_CANCEL
SHUTDOWN_BEFORE_WORKER_DRAIN
SHUTDOWN_BEFORE_JOURNAL_CLOSE
SHUTDOWN_BEFORE_MODEL_CLOSE
SHUTDOWN_COMPONENT_CLOSE
```

Enum Value 使用稳定英文标识，不能包含自由文本。

## 五、FaultAction

固定支持：

```text
RAISE_TYPED_ERROR
DELAY
BLOCK_UNTIL_RELEASED
RETURN_TYPED_FAILURE
CORRUPT_TEST_FIXTURE
```

本轮只完成 Contract 和基础执行器。

### RAISE_TYPED_ERROR

只允许固定 `InjectedFaultCode`，例如：

```text
INJECTED_TRANSIENT_FAILURE
INJECTED_PERMANENT_FAILURE
INJECTED_TIMEOUT
INJECTED_RATE_LIMIT
INJECTED_JOURNAL_FAILURE
INJECTED_STORE_FAILURE
INJECTED_ENCODING_FAILURE
INJECTED_COMPONENT_CLOSE_FAILURE
```

抛出的 `InjectedFaultError`：

-只包含固定 Code；
-不携带用户 Exception；
-不包含 Payload、路径或正文；
-普通 repr 安全。

### DELAY

字段：

```text
delay_seconds
```

要求：

-有限；
-非负；
-拒绝 bool；
-使用可注入 Sleeper/Clock；
-可以响应 Cancellation；
-测试优先使用 Fake Sleeper；
-不得在单元测试中使用长时间 sleep。

### BLOCK_UNTIL_RELEASED

需要测试专用 `FaultBlocker`：

```text
entered
release
timeout
```

要求：

-等待有界；
-Scope close 时自动 release；
-Cancellation 可中止；
-不能把 `asyncio.Event`、Task 或 Lock 持久化进 FaultPlan；
-Blocker 作为运行时测试对象单独注入。

### RETURN_TYPED_FAILURE

只返回固定不可变 `InjectedFailureResult`，不改变原接口合同。本轮只建立基础对象，不接入业务组件。

### CORRUPT_TEST_FIXTURE

本轮只建立描述，不直接修改任何 Store。执行必须要求显式传入测试 Fixture Mutator；不存在 Mutator 时 fail closed。

## 六、FaultTrigger

固定：

```text
ALWAYS
FIRST_MATCH
ON_NTH_MATCH
AFTER_N_MATCHES
UNTIL_MAX_HITS
```

每个 Rule 必须有：

```text
trigger
match_number
max_hits
```

验证：

- count 拒绝 bool；
  -必须为正整数；
  -`ON_NTH_MATCH` 必须有 match_number；
  -`max_hits` 必须大于 0；
  -不允许无限触发，除非测试显式使用严格有界 Scope；
  -第一版不实现概率或随机。

## 七、Fault Scope

固定：

```text
GLOBAL_TEST_SCOPE
RUN_SCOPE
STEP_SCOPE
INVOCATION_SCOPE
ATTEMPT_SCOPE
COMPONENT_SCOPE
```

`GLOBAL_TEST_SCOPE` 只表示当前 `FaultInjectionScope`，不能是进程全局单例。

Rule 可以包含安全匹配字段：

```text
run_id_digest
step_id
invocation_id_digest
attempt_number
component
event_type
operation_kind
side_effect_phase
shutdown_component
```

不得匹配或存储：

- Prompt；
  -Message；
  -Tool arguments；
  -Tool output；
  -RAG query 正文；
  -Memory；
  -路径；
  -API Key；
  -Provider URL；
  -原始异常。

## 八、FaultMatchContext

使用不可变 `frozen=True, slots=True`。

至少包含：

```text
fault_point
component
run_id_digest
step_id
invocation_id_digest
attempt_number
runtime_mode
event_type
operation_kind
side_effect_phase
checkpoint_kind
shutdown_component
```

验证：

-所有 Identity 只能是安全 Token 或 lowercase SHA-256；
-拒绝过长值；
-不得有 arbitrary metadata mapping；
-repr 不能泄漏正文；
-不保存真实 Runtime 对象引用。

## 九、FaultRule

不可变 Rule 至少包含：

```text
rule_id
fault_point
action
trigger
scope
match conditions
max_hits
action parameters
enabled
dangerous_window
```

要求：

- `rule_id` 为安全稳定 Token；
  -默认 `enabled=true` 只针对显式创建的测试 Plan；
  -`dangerous_window=false` 默认；
  -Tool commit 后等高风险点必须要求 `dangerous_window=true`；
  -Foundation 只验证，不执行真实高风险业务；
  -Rule 本身不保存 Counter、Lock、Event 或 Task；
  -运行计数归 Controller。

## 十、FaultPlan

不可变 Plan：

```text
plan_id
schema_version
rules
created_at
```

要求：

- Rule ID 唯一；
  -Plan 创建后不可变；
  -规则排序规范化；
  -可以计算安全 plan digest；
  -Digest 不含 Blocker、Lock、Task；
  -普通 repr 不展开所有 Match 条件；
  -不得从用户 JSON 或 HTTP Request 直接创建；
  -本轮仅测试代码和显式 Python API 可构造。

## 十一、FaultInjectionController

Controller 是运行时命中计数 Owner。

职责：

```text
evaluate(context)
execute_if_matched(context)
snapshot()
close()
```

要求：

-无匹配返回不可变 NO_FAULT Decision；
-多个 Rule 同时匹配时采用固定顺序；
-建议按 Plan 中规范化 Rule 顺序，第一条可执行 Rule 获胜；
-不得一次执行多个 Fault Action，除非未来显式支持组合；
-Hit Counter 并发安全；
-`max_hits=1` 在并发下只能触发一次；
-关闭后不再触发；
-close 幂等；
-Controller 不能修改 Runtime State；
-Controller 不能发布 Event；
-Controller 不能调用 Retry/Fallback；
-Controller 不缓存 Runtime 对象。

## 十二、FaultDecision

不可变：

```text
matched
rule_id
fault_point
action
match_ordinal
hit_ordinal
safe_fault_code
triggered_at
```

NO_FAULT Decision 不能包含虚构 Rule。

不得包含：

-异常正文；
-Payload；
-输入正文；
-路径；
-真实 Tool/Model 响应。

## 十三、FaultInjectionRecorder

Recorder 只保存安全、内容无关的记录：

```text
plan_id
rule_id
fault_point
component
action
match_ordinal
hit_ordinal
safe_fault_code
timestamp
```

要求：

-有界容量；
-容量满时采用明确策略；
-不写 Runtime Event Journal；
-不进入用户 Wire；
-不进入 AgentState；
-可以被测试读取；
-关闭后不接受新记录；
-repr 安全；
-不保存 run_id 明文，最多保存 digest。

## 十四、FaultInjectionScope

Scope 是测试故障生命周期 Owner。

负责：

- Controller；
  -Recorder；
  -Blocker；
  -Fake Sleeper；
  -关闭和自动释放；
  -测试结束隔离。

要求：

-Async Context Manager；
-同步 close 或 async close 按真实需要选择；
-退出时释放所有 Blocker；
-取消所有测试专用等待；
-Controller close；
-Recorder close；
-不依赖 GC；
-不同 Scope 完全隔离；
-禁止模块级 current_fault_scope；
-如需要 ContextVar，必须由 Scope 设置并 reset，且不能成为隐式生产入口。

优先使用显式依赖注入，不使用全局 ContextVar。

## 十五、并发安全测试

必须使用 Barrier/Event 确定性覆盖：

1. 两个 Task 同时命中 `max_hits=1`；
   2.只有一个得到 matched；
   3.另一个得到 NO_FAULT；
   4.match ordinal 单调；
   5.hit ordinal 单调；
   6.不同 Rule 独立；
   7.不同 Controller 独立；
   8.close 与 evaluate 竞态；
   9.Scope 退出释放 Blocker；
   10.取消等待 Blocker；
   11.Fake Sleeper 取消；
   12.Recorder 容量边界。

不得依赖随机 sleep 制造竞态。

## 十六、安全测试

构造敏感标记：

```text
SECRET_PROMPT_TEXT
MODEL_OUTPUT_SECRET
TOOL_ARGUMENT_SECRET
TOOL_OUTPUT_SECRET
RAG_CHUNK_SECRET
MEMORY_SECRET
C:\Users\private-user
provider-secret-error
```

断言不会进入：

- FaultPlan JSON/Digest；
  -FaultDecision；
  -Recorder；
  -repr；
  -safe exception；
  -日志；
  -测试失败消息中的业务对象 repr。

FaultMatchContext 不提供这些正文的字段入口。

## 十七、Backward Compatibility

验证：

- Controller 参数缺省时现有组件构造不变；
  -Disabled Controller 的执行结果等同于无 Controller；
  -不修改 RuntimeEvent；
  -不修改 Snapshot Schema；
  -不修改 Journal Schema；
  -不修改 Tool Evidence；
  -不修改 API；
  -不改变全仓正常测试结果。

本轮还没有接入任何业务组件，因此应只增加 Foundation 类型和测试。

## 十八、重点 Bad Case

结果文档至少包含：

1. Fault Injection 默认开启；
   2.用户 Prompt 控制 Fault Rule；
   3.随机触发不可重复；
   4.`max_hits=1` 并发触发两次；
   5.Rule 内保存 Lock/Event；
   6.Controller 修改 AgentState；
   7.Recorder 写入 Runtime Journal；
   8.Fault Context 保存 Tool Argument；
   9.Scope 退出没有释放 Blocker；
   10.测试之间共享全局 Controller；
   11.Delay 不响应 Cancellation；
   12.Corrupt Action 修改真实 Store；
   13.高风险 Tool Point 未要求 dangerous flag；
   14.Disabled Controller 改变正常结果。

使用既定 Bad Case 格式，标明真实审计或假设构造。

## 十九、测试命令

建议新增：

```text
tests/test_fault_injection_contract.py
tests/test_fault_injection_controller.py
tests/test_fault_injection_concurrency.py
tests/test_fault_injection_security.py
```

执行：

```text
uv run python -m pytest \
  tests/test_fault_injection_contract.py \
  tests/test_fault_injection_controller.py \
  tests/test_fault_injection_concurrency.py \
  tests/test_fault_injection_security.py \
  tests/test_runtime_context.py \
  tests/test_parallel_execution.py \
  tests/test_runtime_events.py \
  tests/test_event_channel.py \
  tests/test_event_journal.py \
  tests/test_snapshot_contract.py \
  tests/test_tool_execution.py \
  tests/test_retrieval_execution.py \
  tests/test_model_invocation.py \
  tests/test_runtime_full_e2e.py \
  tests/test_runtime_invariants.py -q
```

如果真实测试文件名不同，使用仓库实际文件并在结果文档说明。

执行全仓：

```text
uv run python -m pytest -q
uv run python -m compileall -q core tools tests
uv lock --check
git diff --check
```

## 二十、禁止事项

不得：

-接入真实 Model/Tool/RAG；
-接入 EventJournal/Channel；
-接入 Snapshot/Shutdown；
-实现生产开关；
-读取 HTTP Header 或 Prompt；
-实现随机概率触发；
-修改 Runtime State；
-执行 Retry/Fallback/Compensation；
-修改 Tool Side Effect；
-实现 Fault Injection API；
-实现第 25 天内容；
-保存敏感正文。

## 二十一、结果文档

创建：

```text
docs/learning/stage2/result/day24_fault_injection_foundation_result.md
```

必须包含：

# 第 24 天第一轮：Fault Injection Foundation

## 1. 本轮目标

## 2. 修改前故障测试接缝

## 3. Fault Injection 范围

## 4. FaultPoint

## 5. FaultAction

## 6. FaultTrigger

## 7. FaultScope

## 8. FaultMatchContext

## 9. FaultRule

## 10. FaultPlan

## 11. FaultInjectionController

## 12. FaultDecision

## 13. FaultInjectionRecorder

## 14. FaultInjectionScope

## 15. 并发命中

## 16. Cancellation / Blocker

## 17. Security

## 18. Backward Compatibility

## 19. Bad Case

## 20. 测试结果

## 21. 未完成事项

## 22. 第二轮接入点

## 23. 需要带回 ChatGPT 审查的信息

## 二十二、完成后输出

Existing failure seams：

Fault injection default：

Production setting：

Fault points：

Fault actions：

Fault triggers：

Fault scopes：

Fault context：

Sensitive fields：

Fault rule：

Dangerous window：

Fault plan：

Plan digest：

Controller owner：

Rule ordering：

Match count：

Hit count：

Concurrent max_hits：

Fault decision：

Recorder：

Recorder capacity：

Scope owner：

Blocker cleanup：

Cancellation：

Global state：

Runtime event：

Journal：

Snapshot：

Tool side effect：

Backward compatibility：

新增测试：

目标 pytest：

全仓 pytest：

compileall：

lock check：

diff check：

需要人工确认的问题：

------

# 十四、第 24 天当前进度

## 第一轮待完成

-  Fault Injection Contract
-  FaultPoint
-  FaultAction
-  FaultTrigger
-  FaultRule / FaultPlan
-  FaultInjectionController
-  FaultInjectionScope
-  并发命中测试
-  安全测试
-  第一轮审查

## 第二轮待完成

-  Model Injection Seam
-  Tool Injection Seam
-  Retrieval Injection Seam
-  Retry/Fallback 不变量
-  Side-effect Failure Window

## 第三轮待完成

-  Journal Failure
-  Channel Failure
-  Snapshot Fixture Corruption
-  Observability/Trace Failure
-  Shutdown Failure Matrix

## 第四轮待完成

-  组合故障矩阵
-  并行故障
-  Cancellation/Fault 竞态
-  Runtime 不变量
-  故障覆盖报告
-  第 24 天最终文档
-  全仓最终验收

**阶段二第 24/25 天：完成理论与任务拆分，进入第一轮 Fault Injection Foundation。**