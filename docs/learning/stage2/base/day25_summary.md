# 阶段二第 25 天：Runtime Production Readiness

## 今日主题

第 25 天进入阶段二最终收口：

**Production Readiness Review（生产就绪评审） + Contract Freeze（契约冻结） + Release Candidate Validation（发布候选版本验证） + Interview Packaging（面试材料封装）**

第 24 天已经完成确定性 Fault Injection（故障注入）体系，42 个 Fault Point（故障点）中，32 个存在真实 Runtime（运行时）接缝且全部完成边界测试，10 个如实保留为 Contract-only（仅契约存在）；全仓达到 `1013 passed, 42 subtests passed`。

第 25 天不再新增大型 Runtime 能力，而是回答四个最终问题：

1. 阶段二各模块的 Owner（所有者）是否唯一？
2. 对外 Contract（契约）是否已经稳定、版本化且无语义冲突？
3. 现有功能组合后是否能作为 Release Candidate（发布候选版本）运行？
4. 项目材料能否真实支撑简历、系统设计和 Agent 面试？

------

# 一、今日学习目标

## 1. 理解“功能完成”与“生产就绪”的区别

功能完成通常只证明：

```text
正常输入
→ 能得到正常输出
```

生产就绪还必须证明：

```text
异常、取消、超时、并发、部分持久化、进程关闭
→ 状态仍真实
→ 资源仍收口
→ 事实 Owner 不重复
→ 结果可诊断
```

对于 LocalAgent，目前重点不是继续新增功能，而是验证已有能力能否共同成立。

------

## 2. 完成 Runtime Contract Freeze

冻结的不是“以后不能修改”，而是明确当前版本：

```text
哪些类型是正式 Contract
哪些字段已经持久化
哪些行为属于兼容承诺
哪些能力仍是 Contract-only
哪些内容只是测试 Fixture
哪些内容不能进入生产 API
```

需要重点检查：

- `RunContext`
- `AgentState`
- `RunStatus` / `StepStatus`
- `RunSnapshot`
- `RuntimeEvent`
- `JournalRecord`
- `ToolExecutionResult/Error`
- `RecoveryAssessment`
- `ShutdownReport`
- Fault Injection 的 Plan、Rule、Evidence 和 Report

------

## 3. 建立最终 Ownership Matrix

整个 Runtime 最容易出现的问题不是代码错误，而是两个组件同时认为自己是 Owner。

最终必须明确：

| 事实             | 唯一 Owner                                    |
| ---------------- | --------------------------------------------- |
| Runtime 模式选择 | Composition Root / Runtime Factory            |
| Run 身份         | `RunContext`                                  |
| Run/Step 状态    | `AgentState`                                  |
| Retry            | `RetryExecutor`                               |
| Model Fallback   | `ModelRoutingPolicy`                          |
| Tool Side-effect | `AttemptSideEffectTracker` + Adapter 权威响应 |
| Event Sequence   | `RuntimeEventChannel`                         |
| Journal append   | `RunEventJournal`                             |
| Snapshot capture | `CheckpointCoordinator`                       |
| Recovery 判断    | `RecoveryValidator`                           |
| Span Identity    | `SpanRecorder`                                |
| Worker 事实      | Executor / Worker Controller                  |
| Shutdown 编排    | `GracefulShutdownCoordinator`                 |
| Fault 命中计数   | `FaultInjectionController`                    |

任何 Report、Facade、Controller 或 Fixture 都只能读取或派生事实，不能成为第二 Owner。

------

# 二、第 25 天四轮安排

## 第一轮：Architecture Audit 与 Contract Freeze

完成：

- 阶段二架构全量审计；
  -Application / Run / Operation Scope（应用级、运行级、操作级作用域）整理；
  -Owner Matrix；
  -Contract / Schema / Compatibility Matrix；
  -Legacy 与 Coordinated Runtime 边界；
  -生产入口与测试入口隔离；
  -能力清单和明确未实现项；
  -清理真实契约冲突。

产物：

```text
docs/learning/stage2/result/day25_runtime_contract_freeze_result.md
docs/runtime/runtime_architecture_v1.md
docs/runtime/runtime_owner_matrix.md
docs/runtime/runtime_capability_matrix.md
```

## 第二轮：Release Candidate End-to-End Validation

完成：

-正式 Release Candidate（发布候选版本）场景集；
-正常、失败、取消、超时、断连、恢复、关闭的完整验证；
-并行与资源收口；
-Legacy 回滚验证；
-性能与资源基线；
-测试分层与发布 Gate。

## 第三轮：Operations 与 Documentation

完成：

-部署与启动说明；
-环境变量和配置边界；
-运行时排障手册；
-故障码和健康状态说明；
-数据安全边界；
-恢复与人工对账 Runbook（操作手册）；
-明确生产未支持能力。

## 第四轮：阶段二总验收与面试材料

完成：

-阶段二最终结果文档；
-项目架构介绍；
-核心设计决策；
-高价值 Bad Case；
-面试回答材料；
-简历项目描述；
-阶段三入口与技术债清单。

------

# 三、第一轮核心原则

## 不进行“大扫除式重构”

本轮不是为了让目录更漂亮而移动大量文件。

只修复以下真实问题：

-重复 Owner；
-错误 Scope；
-持久化 Contract 不稳定；
-正式代码依赖测试 Helper；
-Legacy 与 Coordinated 边界不清；
-文档与真实代码不一致；
-兼容字段语义冲突；
-生产入口可以启用测试 Fault；
-Report 把派生事实当权威状态。

## 不能删除真实兼容路径

Legacy Runtime（旧运行时）虽然不是默认入口，但仍然承担显式回滚能力。

本轮只能明确：

```text
默认：Coordinated Runtime
显式配置：Legacy Runtime
```

不得为了“架构统一”直接删除 Legacy。

## 不虚构版本

仓库中没有的历史版本不能创建假兼容测试。

例如 Snapshot 只有 v1，就应该写：

```text
当前支持 v1
未知高版本 fail closed
不存在 v0 reader
```

不能宣称兼容不存在的 v0。

------

# 第 25 天第一轮 Codex 提示词

下面提示词直接交给 Codex。

你正在继续改造 LocalAgent 项目的阶段二 Runtime。

这是阶段二第 25 天第一轮：

Runtime Architecture Audit / Contract Freeze。

第 24 天已经完成确定性 Fault Injection、Chaos Matrix、Fault Coverage、Runtime Invariants 和 Graceful Shutdown Fault Matrix。

当前阶段二已存在的主要能力包括：

- RunContext / Cancellation / Deadline
- AgentState / RunStatus / StepStatus / StopReason
- AgentLoop
- Scheduler / ParallelExecutor
- BudgetLedger
- Model Routing / Retry / Fallback / Circuit Breaker
- Tool Execution Contract / Idempotency / Side-effect Tracker
- Retrieval Runtime Contract
- RuntimeEvent / Channel / Journal
- Observability / Metrics
- Trace / Span
- Snapshot / Checkpoint
- RecoveryValidator
- Coordinated Runtime 默认入口
- Legacy Runtime 显式回滚
- Client Disconnect / Cancel-and-drain
- Worker Lifecycle
- GracefulShutdownCoordinator
- Fault Injection / Chaos Matrix

本轮不新增大型能力，只进行架构审计、Contract Freeze 和必要的最小修复。

本轮结果文档：

```text
docs/learning/stage2/result/day25_runtime_contract_freeze_result.md
```

同时创建或更新：

```text
docs/runtime/runtime_architecture_v1.md
docs/runtime/runtime_owner_matrix.md
docs/runtime/runtime_capability_matrix.md
```

最终第 25 天文档留到第四轮：

```text
docs/learning/stage2/result/day25_stage2_final_acceptance_result.md
```

## 一、本轮目标

完成：

1. 阶段二 Runtime 模块全量审计；
2. Composition Root 与依赖方向审计；
3. Application / Run / Operation / Attempt Scope 分类；
4. 唯一 Owner Matrix；
5. Public Contract 与 Internal Contract 分类；
6. Schema / Version / Digest Matrix；
7. Legacy / Coordinated 边界；
8. Fault Injection 测试能力与生产能力隔离；
9. Report / Evidence / Fixture 的权威性分类；
10. Capability Matrix；
11. Deprecated / Compatibility Matrix；
12. 必要的最小代码修复；
13. 契约级回归测试；
14. 第一轮结果文档。

本轮不得：

-新增 Runtime 功能；
-新增 Tool；
-新增 Recovery/Replay；
-新增生产 Fault API；
-删除 Legacy Runtime；
-大规模移动目录；
-大规模重命名公共类型；
-改变 Model/Tool/Retrieval 业务语义；
-修改 Retry/Fallback/Compensation 策略；
-实现第 26 天内容。

## 二、审计 Composition Root

至少检查：

- `server.py`
- Application lifespan
- Settings 装配
- `ApplicationRuntimeServices`
- `CoordinatedRuntimeFactory`
- `CoordinatedRunScope`
- Legacy Runtime Factory / Router
- `/api/chat`
- Runtime mode selection
- Model Adapter Resolver
- Tool Registry
- Retrieval Service
- Journal / Snapshot Store
- Observability Dispatcher
- Span Recorder
- RunRegistry
- GracefulShutdownCoordinator

必须回答：

1. Runtime mode 在哪里选择；
   2.一次请求是否只选择一次 Runtime；
   3.默认是否固定为 Coordinated；
   4.Legacy 是否只能显式启用；
   5.是否存在 Coordinated 失败后自动跨 Runtime fallback；
   6.Application services 是否只装配一次；
   7.每 Run 对象是否每请求新建；
   8.Operation-scoped Controller 是否被错误缓存；
   9.是否存在模块级 current service；
   10.测试 Fixture 是否进入生产装配；
   11.生产是否存在 Fault Plan 创建入口；
   12.Shutdown 是否由唯一 Coordinator 编排。

不得新建第二个 Composition Root。

## 三、Scope Matrix

建立固定 Scope 分类：

```text
APPLICATION_SCOPE
RUN_SCOPE
OPERATION_SCOPE
INVOCATION_SCOPE
ATTEMPT_SCOPE
COMPONENT_SCOPE
TEST_SCOPE
```

至少分类以下对象：

- Settings
- ApplicationRuntimeServices
- ModelInvocationRouter
- Model Adapter
- RetrievalExecutionService
- ToolExecutionService
- EventJournal
- SnapshotStore
- ObservabilityDispatcher
- SpanRecorder
- RunRegistry
- GracefulShutdownCoordinator
- RunContext
- AgentState
- RuntimeEventChannel
- BudgetLedger
- Run Event Emitter
- CoordinatedRunScope
- FaultInjectionController
- FaultInjectionScope
- Recovery operation
- Shutdown operation
- Model Invocation
- Model Attempt
- Tool Invocation
- Tool Attempt
- Retrieval Stage
- Resource Lease
- CancellationSource
- Worker Handle

检查：

- Application-scoped 对象是否缓存 Run 数据；
  -Run-scoped 对象是否关闭 Application 资源；
  -Operation-scoped Controller 是否进入 Application service；
  -Attempt 对象是否泄漏到 Plan/Snapshot；
  -Test Scope 是否可以由 HTTP/Prompt/Settings 创建；
  -共享对象是否按 identity 关闭一次。

创建：

```text
docs/runtime/runtime_scope_matrix.md
```

如不希望增加第四个正式文档，也可以纳入 architecture 文档，但结果文档必须说明位置。

## 四、Owner Matrix

建立唯一事实 Owner，至少包括：

```text
runtime selection
run identity
trace identity
run state
step state
stop reason
cancellation reason
deadline
plan definition
runtime step status
retry
fallback
circuit state
budget reservation
budget commit
model usage
tool invocation identity
tool attempt identity
tool side-effect state
tool compensation state
resource lease
worker active/detached state
event identity
event sequence
terminal event
journal append
snapshot capture
snapshot digest
recovery assessment
observability health
trace health
shutdown orchestration
component close result
fault match/hit count
```

每项必须记录：

```text
fact
authoritative_owner
readers
writers
persistence_location
lifecycle_scope
must_not_own
```

重点检查以下重复 Owner 风险：

- PlanStep 保存 Runtime status；
  -AgentState 与 Plan 双写；
  -Fault Controller 决定 Retry；
  -Tool Error Mapper 修改 Side-effect Tracker；
  -Event Channel 创建第二 Terminal；
  -RecoveryValidator 修改 AgentState；
  -Observability/Trace 修改 Journal；
  -ShutdownReport 修改 Lifecycle；
  -Run facade 关闭 Application Recorder；
  -Application Service 缓存当前 Run Controller。

发现真实重复 Owner 时只做最小修复。

## 五、Contract 分类

建立：

```text
PUBLIC_STABLE
PUBLIC_VERSIONED
INTERNAL_STABLE
INTERNAL_EVOLVING
TEST_ONLY
DEPRECATED
```

至少审计：

- RunContext
- AgentState
- Plan / PlanStep
- RuntimeEvent
- RuntimeEventDraft
- JournalRecord
- RunSnapshot
- BudgetSnapshot
- ToolInvocation
- ToolExecutionResult
- ToolExecutionError
- ToolCompletedPayload
- RetrievalExecutionResult
- ModelInvocationResult
- RecoveryAssessment
- ShutdownReport
- FaultPlan / FaultRule
- FaultDecision
- FaultCoverageReport
- FaultRuntimeInvariantReport
- EventPublicationEvidence
- SnapshotPublicationEvidence
- ToolCompletionGapFixture
- Test Fake / Test Mutator

要求：

- Test Fixture 不能被标为 Public；
  -Report 是派生事实，不是状态 Owner；
  -Evidence 只能保存安全事实；
  -版本化持久化类型必须声明 schema/version；
  -Internal evolving 类型不得直接进入 Wire；
  -Deprecated 字段必须说明兼容语义。

## 六、Schema / Version / Digest Matrix

建立以下矩阵：

```text
contract
schema_version
digest_version
canonicalization_owner
reader_versions
writer_version
unknown_version_behavior
missing_field_behavior
write_back_behavior
```

至少包含：

- RuntimeEvent
- JournalRecord
- RunSnapshot
- ToolCompletedPayload
- Recovery evidence
- FaultPlan
- ShutdownReport，如当前已版本化

要求：

-未知高版本 fail closed；
-旧字段缺失保持 Unknown 或既有兼容值；
-不得使用当前 Registry 回填历史；
-不得使用 Python repr 计算持久化 digest；
-读取旧版本不写回；
-不存在的历史版本不得虚构；
-Digest owner 唯一。

如果 ShutdownReport 当前没有持久化或跨进程 Wire，不要为了本轮强行增加 schema version，只记录真实情况。

## 七、Legacy / Coordinated Boundary

建立明确矩阵：

| 能力              | Coordinated | Legacy |
| ----------------- | ----------- | ------ |
| 默认入口          |             |        |
| 显式配置          |             |        |
| RunContext        |             |        |
| AgentState        |             |        |
| Event Journal     |             |        |
| Snapshot          |             |        |
| Recovery          |             |        |
| Fault Injection   |             |        |
| Graceful Shutdown |             |        |
| Worker Tracking   |             |        |
| Tool Contract     |             |        |
| Streaming         |             |        |

要求：

-默认 `/api/chat` 使用 Coordinated；
-Legacy 只能显式选择；
-不存在 Coordinated 运行失败后自动切 Legacy；
-Legacy 不得伪装成拥有未接入能力；
-Coordinated 不调用 Legacy 作为内部 fallback；
-两条路径共享 Application resource 时 Owner 明确；
-Shutdown 必须覆盖两类 Worker。

## 八、Fault Injection 生产隔离

最终审计：

- Settings 是否包含 Fault 开关；
  -环境变量是否能创建 FaultPlan；
  -HTTP Header 是否能启用 Fault；
  -Request body 是否包含 Fault；
  -Prompt/Message 是否能选择 Fault；
  -Tool Argument 是否能选择 Fault；
  -是否存在 module-global Controller；
  -是否存在 ContextVar current Fault Controller；
  -Application service 是否缓存 Controller；
  -Journal/Snapshot 是否持久化 Rule；
  -Event/Wire 是否输出 Rule ID。

生产默认必须为：

```text
fault_controller = None
```

只有测试显式创建 Scope/Controller。

`FaultPointSupportReport` 与 `FaultCoverageReport` 可以作为测试/文档产物，但不得进入生产请求路径。

## 九、Report / Evidence 权威性

建立分类：

### Authority

例如：

- AgentState
- JournalRecord
- RunSnapshot
- Worker owner snapshot

### Frozen Evidence

例如：

- ToolCompletedPayload
- EventPublicationEvidence
- SnapshotPublicationEvidence

### Derived Report

例如：

- ShutdownReport
- FaultCoverageReport
- FaultRuntimeInvariantReport
- Observability Health
- Trace Health

### Test Oracle / Fixture

例如：

- ToolCompletionGapFixture
- corruption fixture
- PhaseAwareToolAdapter counters

检查：

- Derived Report 是否写回 Authority；
  -Test Oracle 是否进入 Production Validator；
  -Frozen Evidence 是否保存正文；
  -Report 是否持有 Runtime 对象；
  -Report 是否通过错误字符串推断状态；
  -Report 是否包含高基数 identity。

## 十、Capability Matrix

创建：

```text
docs/runtime/runtime_capability_matrix.md
```

每项至少记录：

```text
capability
status
default_path
owner
persistence
failure_behavior
recovery_behavior
legacy_support
tests
known_limitations
```

状态固定为：

```text
SUPPORTED
PARTIALLY_SUPPORTED
CONTRACT_ONLY
NOT_IMPLEMENTED
LEGACY_ONLY
DEPRECATED
```

至少包含：

- Coordinated Runtime
- Legacy rollback
- Parallel execution
- Budget
- Retry
- Fallback
- Circuit breaker
- Tool idempotency
- Tool side-effect evidence
- Resource lease
- Retrieval stage runtime
- Event streaming
- Journal-first
- Snapshot
- Recovery validation
- Recovery execution
- Replay
- Observability
- Trace
- Client disconnect
- Worker tracking
- Graceful shutdown
- Fault Injection
- Random Chaos
- Cross-process Registry
- Exactly-once
- Automatic compensation
- Step result rehydration

不得把：

```text
Recovery validation
```

写成：

```text
Automatic recovery
```

不得把：

```text
Fault Injection test seam
```

写成：

```text
Production chaos platform
```

## 十一、Deprecated / Compatibility Matrix

检查：

- `ShutdownReport.completed`
  -旧 Event evidence 缺失字段
  -Legacy Runtime mode
  -旧 Tool Adapter method signature
  -旧 Model Generator compatibility
  -旧 Event v1/v2
  -旧 Snapshot v1
  -旧 Fault Trigger 是否已删除
  -旧 `.event` publication error access
  -旧 constructor positional arguments

每项记录：

```text
item
current_behavior
replacement
compatibility_period
removal_precondition
tests
```

不得实际删除仍有调用方的兼容字段。

## 十二、代码修复边界

只允许修复真实审计发现：

-重复 Owner；
-错误 Scope；
-Test-only 对象进入生产装配；
-生产 Fault enablement；
-持久化版本/摘要错误；
-Report 语义与真实状态冲突；
-Legacy/Coordinated 自动跨 Runtime fallback；
-共享资源重复 close；
-文档与真实行为不一致。

不得为了形式统一：

-大规模重命名；
-移动全部模块；
-引入 LangChain/LangGraph；
-新增数据库；
-替换 Event/Journal/Snapshot 技术；
-删除 Legacy；
-重写 Runtime。

## 十三、契约测试

建议新增：

```text
tests/test_runtime_contract_freeze.py
tests/test_runtime_scope_matrix.py
tests/test_runtime_owner_matrix.py
tests/test_runtime_capability_matrix.py
tests/test_runtime_schema_matrix.py
tests/test_runtime_legacy_boundary.py
tests/test_runtime_fault_production_isolation.py
tests/test_runtime_report_authority.py
```

测试应从真实类型、字段、依赖和调用链生成断言，不能只检查 Markdown 文本。

至少验证：

1. PlanStep 没有运行时 status；
   2.AgentState 是 Runtime 状态唯一 Owner；
   3.Application services 不保存 Run controller；
   4.生产 Settings/API 无 Fault enablement；
   5.Coordinated 失败不会切换 Legacy；
   6.Terminal owner 唯一；
   7.Event sequence owner 唯一；
   8.Snapshot digest owner 唯一；
   9.RecoveryValidator 无 AgentState writer；
   10.Report 不持有 Runtime owner；
   11.Test Fixture 不进入 production import path；
   12.旧 Event/Snapshot 兼容边界；
   13.Shutdown `completed` 兼容语义；
   14.共享 resource identity 去重；
   15.Capability Matrix 不夸大未实现能力。

## 十四、安全扫描

扫描：

- Runtime Contract repr；
  -Owner Matrix；
  -Capability Matrix；
  -Schema Matrix；
  -Result document；
  -Report/Evidence；
  -Wire；
  -Structured logs；
  -Metric labels；
  -Span attributes。

不得出现：

```text
SECRET_PROMPT_TEXT
MODEL_OUTPUT_SECRET
TOOL_ARGUMENT_SECRET
TOOL_OUTPUT_SECRET
RAG_CHUNK_SECRET
MEMORY_SECRET
C:\Users\private-user
provider-secret-error
raw-idempotency-key
raw-resource-key
raw-snapshot-payload
fault-rule-secret
```

文档中不得写真实用户目录、Provider URL、公司内部路径或密钥。

## 十五、重点 Bad Case

结果文档至少包含：

1. Application service 缓存 Run Controller；
   2.Plan 与 AgentState 双写 Step status；
   3.Fault Controller 成为 Retry owner；
   4.RecoveryValidator 修改 AgentState；
   5.Report 被当作 Authority；
   6.Test Fixture 进入生产 Recovery 输入；
   7.Coordinated 失败自动切 Legacy；
   8.Legacy 被文档夸大为完整 Runtime；
   9.未知 Schema 按当前版本解析；
   10.当前 Registry 回填历史 Evidence；
   11.ShutdownReport.completed 被理解为 fully closed；
   12.Production Settings 可以启用 Fault；
   13.共享 resource 被不同名称关闭两次；
   14.枚举存在被写成 capability supported；
   15.不存在的 Snapshot v0 被虚构为兼容版本；
   16.内部 evolving 类型直接进入 Wire。

使用既定 Bad Case 格式：

-真实性类型；
-触发条件；
-故障表现；
-根因；
-修复；
-回归；
-知识点；
-面试表达；
-当前状态。

## 十六、测试命令

执行新增契约测试：

```text
uv run python -m pytest \
  tests/test_runtime_contract_freeze.py \
  tests/test_runtime_scope_matrix.py \
  tests/test_runtime_owner_matrix.py \
  tests/test_runtime_capability_matrix.py \
  tests/test_runtime_schema_matrix.py \
  tests/test_runtime_legacy_boundary.py \
  tests/test_runtime_fault_production_isolation.py \
  tests/test_runtime_report_authority.py -q
```

再执行阶段二关键回归：

```text
uv run python -m pytest \
  tests/test_runtime_full_e2e.py \
  tests/test_runtime_invariants.py \
  tests/test_fault_runtime_invariants.py \
  tests/test_fault_disabled_full_parity.py \
  tests/test_event_journal.py \
  tests/test_snapshot_store.py \
  tests/test_recovery_integration.py \
  tests/test_observability_integration.py \
  tests/test_trace_integration.py \
  tests/test_graceful_shutdown.py \
  tests/test_shutdown_report_truthfulness.py -q
```

如果仓库文件名不同，使用真实等价文件并在结果文档说明。

最后执行：

```text
uv run python -m pytest -q
uv run python -m compileall -q core tools tests
uv lock --check
git diff --check
```

## 十七、结果文档

创建：

```text
docs/learning/stage2/result/day25_runtime_contract_freeze_result.md
```

必须包含：

# 阶段二第 25 天第一轮：Runtime Contract Freeze

## 1. 本轮目标

## 2. 修改前 Composition Root

## 3. Runtime Mode Selection

## 4. Scope Matrix

## 5. Owner Matrix

## 6. Contract Classification

## 7. Schema / Version / Digest Matrix

## 8. Legacy / Coordinated Boundary

## 9. Fault Injection Production Isolation

## 10. Authority / Evidence / Report / Fixture

## 11. Capability Matrix

## 12. Deprecated / Compatibility Matrix

## 13. 真实代码修复

## 14. 安全边界

## 15. Bad Case

## 16. 测试结果

## 17. 未完成事项

## 18. 第二轮 Release Candidate 接入点

## 19. 需要带回 ChatGPT 审查的信息

## 十八、完成后输出

Runtime selection owner：

Default runtime：

Legacy activation：

Cross-runtime fallback：

Composition root：

Application scope：

Run scope：

Operation scope：

Invocation/attempt scope：

Test-only scope：

State owner：

Retry owner：

Fallback owner：

Side-effect owner：

Sequence owner：

Terminal owner：

Snapshot owner：

Recovery owner：

Shutdown owner：

Fault counter owner：

Public stable contracts：

Versioned contracts：

Internal evolving contracts：

Test-only contracts：

Schema matrix：

Digest owners：

Legacy boundary：

Fault production enablement：

Authority objects：

Frozen evidence：

Derived reports：

Test fixtures：

Supported capabilities：

Partially supported：

Contract-only：

Not implemented：

Deprecated items：

真实代码修复：

新增测试：

目标 pytest：

关键回归：

全仓 pytest：

compileall：

lock check：

diff check：

需要人工确认的问题：

# 第 25 天当前进度

## 第一轮待完成

-  Composition Root 审计
-  Scope Matrix
-  Owner Matrix
-  Contract 分类
-  Schema / Version / Digest Matrix
-  Legacy / Coordinated 边界
-  Fault 生产隔离
-  Capability Matrix
-  Compatibility Matrix
-  Contract 测试
-  第一轮审查

## 第二轮待完成

-  Release Candidate 场景集
-  正常与退化 E2E
-  并发与资源基线
-  性能基线
-  发布 Gate

## 第三轮待完成

-  Operations Runbook
-  配置与部署文档
-  故障码说明
-  人工恢复流程
-  安全边界

## 第四轮待完成

-  阶段二总验收
-  架构面试材料
-  Bad Case 面试档案
-  简历项目描述
-  阶段三入口

**第 25 天第一轮开始。**