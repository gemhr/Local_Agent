# LocalAgent Runtime Architecture v1

本文冻结阶段二第 25 天第一轮结束时的 Runtime 架构事实。它描述当前实现，不承诺未实现能力，也不替代各类型源码中的校验规则。

## 1. Composition Root

唯一生产 Composition Root 是 `server.py` 的 FastAPI `lifespan()`：

```text
Settings.load()
-> lifespan() 创建 Application 资源
-> ApplicationRuntimeServices 收拢依赖与 close ownership
-> CoordinatedRuntimeFactory 创建每请求 CoordinatedRunScope
-> ChatService 持有不可变 Runtime selector 与两条显式入口
-> /api/chat 在请求入口捕获一次 mode
-> GracefulShutdownCoordinator 在 lifespan 退出时编排唯一 shutdown
```

装配事实：

| 问题 | 冻结结论 |
|---|---|
| Runtime mode 在哪里选择 | `Settings.load()` 解析 `CHAT_RUNTIME_MODE`；`chat_endpoint()` 调用 `ChatService.selected_runtime_mode()` 捕获请求快照 |
| 一次请求选择次数 | 一次；流开始后不重读环境变量或 Settings |
| 默认 Runtime | `COORDINATED` |
| Legacy 启用 | 只能显式设置 `CHAT_RUNTIME_MODE=LEGACY` |
| 跨 Runtime fallback | 不存在；所选路径失败后只输出安全错误并收口 |
| Application services 装配 | 每个 FastAPI lifespan 一次 |
| Run 对象 | `CoordinatedRuntimeFactory.create_run_scope()` 每请求新建；Legacy 也每请求新建 Context/State/Ledger |
| Operation controller 缓存 | Application services 不缓存；Fault controller 仅由显式测试调用参数传入 |
| 模块级 current service | `server.py` 保留 `chat_service` 与 `application_runtime_services` 两个 lifespan 兼容句柄；不保存 Run/Controller，生命周期真值同时发布到 `app.state` |
| 测试 Fixture 进入生产装配 | 否；`ToolCompletionGapFixture` 已移到 `tests/_tool_completion_gap_fixtures.py` |
| 生产 FaultPlan 入口 | 无 Settings、环境变量、HTTP header/body、Prompt、Tool arguments 入口 |
| Shutdown owner | `GracefulShutdownCoordinator` |

依赖方向固定为：

```text
server / ChatService
  -> Runtime factory / application services
    -> coordinator / scheduler / executor
      -> model / tool / retrieval contracts
        -> event channel -> journal -> observability projections
        -> checkpoint -> snapshot store
recovery validator -> snapshot + journal (read only)
```

Observability、Trace、Report、RecoveryValidator 不反向修改 AgentState、Journal 或生命周期 owner。生产代码不得新增第二套手工 Coordinated 装配。

## 2. Scope Matrix

固定 scope 词汇：`APPLICATION_SCOPE`、`RUN_SCOPE`、`OPERATION_SCOPE`、`INVOCATION_SCOPE`、`ATTEMPT_SCOPE`、`COMPONENT_SCOPE`、`TEST_SCOPE`。

| 对象 | Scope | 创建者 | 关闭/终止 owner | 禁止行为 |
|---|---|---|---|---|
| Settings | APPLICATION_SCOPE | `Settings.load()` | 无显式 close | 保存 Run 数据 |
| ApplicationRuntimeServices | APPLICATION_SCOPE | `lifespan()` | GracefulShutdownCoordinator | 保存 current Run/Controller |
| ModelInvocationRouter | APPLICATION_SCOPE | AgentRouter 装配 | ApplicationRuntimeServices | 保存 invocation/attempt 状态 |
| Model Adapter | APPLICATION_SCOPE / COMPONENT_SCOPE | model resolver 装配 | ApplicationRuntimeServices | 关闭 Run 资源 |
| RetrievalExecutionService | APPLICATION_SCOPE | AgentRouter 装配 | ApplicationRuntimeServices | 缓存 Run/Fault controller |
| ToolExecutionService | APPLICATION_SCOPE | AgentRouter 装配 | ApplicationRuntimeServices | 缓存 invocation/attempt |
| EventJournal | APPLICATION_SCOPE | lifespan | ApplicationRuntimeServices | 创建 Runtime sequence |
| SnapshotStore | APPLICATION_SCOPE | lifespan，显式 opt-in | ApplicationRuntimeServices | 推断当前 Registry 为历史事实 |
| ObservabilityDispatcher | APPLICATION_SCOPE | lifespan | ApplicationRuntimeServices | 修改 Journal/AgentState |
| SpanRecorder | APPLICATION_SCOPE / COMPONENT_SCOPE | lifespan | ApplicationRuntimeServices | 由 Run facade 关闭 |
| RunRegistry | APPLICATION_SCOPE | lifespan | ApplicationRuntimeServices / Shutdown | 成为 run state owner |
| GracefulShutdownCoordinator | APPLICATION_SCOPE | lifespan | lifespan | 被 Run scope 缓存或关闭 |
| RunContext | RUN_SCOPE | Coordinated factory / Legacy ChatService | 对应 Run owner | 关闭 application 资源 |
| AgentState | RUN_SCOPE | 对应 Parent Runtime | RunCoordinator 或 Legacy AgentLoop 写入 | Plan/Report 双写状态 |
| RuntimeEventChannel | RUN_SCOPE | Coordinated factory | CoordinatedRunScope | 创建第二 terminal |
| BudgetLedger | RUN_SCOPE | 对应 Parent Runtime | 对应 Run owner | 泄漏 reservation 到 Plan |
| Run Event Emitter | RUN_SCOPE | Coordinated factory | 随 Scope 释放 | 自行分配全局 sequence |
| CoordinatedRunScope | RUN_SCOPE | Coordinated factory | 自身 `close/force_abort` | 关闭 Application recorder/store |
| CancellationSource | RUN_SCOPE | Context factory | Run handle / shutdown 请求取消 | 进入 Snapshot/Wire |
| ActiveRunControlHandle | RUN_SCOPE | Parent Runtime | RunCoordinator/Scope/Shutdown 注销 | 成为 AgentState owner |
| FaultInjectionController | OPERATION_SCOPE + TEST_SCOPE | 显式测试 | FaultInjectionScope/测试 | 进入 Settings/API 或决定 retry |
| FaultInjectionScope | TEST_SCOPE | 测试 | 自身 `aclose()` | 进入生产 lifespan |
| Recovery operation | OPERATION_SCOPE | 显式调用方 | 调用栈 | 写 AgentState/启动 replay |
| Shutdown operation | OPERATION_SCOPE | GracefulShutdownCoordinator | Coordinator | 把 Report 当 lifecycle owner |
| Model Invocation | INVOCATION_SCOPE | ModelInvocationRouter | Router | 泄漏到 Plan/Snapshot |
| Model Attempt | ATTEMPT_SCOPE | RetryExecutor/Router | attempt 调用栈 | 成为 retry owner |
| Tool Invocation | INVOCATION_SCOPE | ToolExecutionService | Service | 被 Fault controller 缓存 |
| Tool Attempt | ATTEMPT_SCOPE | ToolAttemptExecutor | attempt 调用栈 | 泄漏原始参数/输出 |
| Retrieval Stage | INVOCATION_SCOPE | RetrievalExecutionService | invocation 调用栈 | 成为 Application 状态 |
| ToolResourceLease | ATTEMPT_SCOPE / COMPONENT_SCOPE | ToolConcurrencyController | ToolAttemptExecutor；detached 时 callback 延后释放 | 在 worker 结束前提前释放 |
| Worker Handle | INVOCATION_SCOPE / ATTEMPT_SCOPE | bounded/tool executor | 对应 worker owner | 被 Report 持有 |
| StartupDependencySnapshot | APPLICATION_SCOPE | `server.py::lifespan()` 从同一次真实 KB 初始化结果构造一次 | 不可变（frozen）；无显式 close | 被运行时修改、持久化或升级为 dependency health manager |
| ApplicationDiagnosticSnapshot | APPLICATION_SCOPE（projection） | `core/runtime/health.py` resolver | 无（derived value，不缓存） | 被 endpoint / Client 当作 writable state 或 Authority |

共享对象按 Python identity 去重关闭一次。Run scope 只关闭 request-owned Channel、Task、Registry registration 与 Gauge registration；Application resource 只能由 ApplicationRuntimeServices/GracefulShutdownCoordinator 关闭。

## 3. Contract Classification

| Contract | Classification | 说明 |
|---|---|---|
| RunContext | PUBLIC_STABLE | 进程内 Run 边界；`to_dict()` 仅安全元数据 |
| AgentState | PUBLIC_VERSIONED | `schema_version` 严格校验；Runtime 状态唯一 owner |
| Plan / PlanStep | PUBLIC_STABLE | 不可变静态定义；不得含 runtime status |
| RuntimeEvent | PUBLIC_VERSIONED | Event v1/v2 reader，v2 writer |
| RuntimeEventDraft | INTERNAL_EVOLVING | Channel 分配 identity/sequence 前的内部事实；不得直接进入 Wire |
| JournalRecord | PUBLIC_VERSIONED | Journal v1/v2 reader，v2 writer，append-only 安全事实 |
| RunSnapshot | PUBLIC_VERSIONED | Snapshot v1，严格字段和 digest |
| BudgetSnapshot（运行账本） | INTERNAL_STABLE | 进程内派生读视图 |
| SafeBudgetSnapshot（Snapshot 子结构） | PUBLIC_VERSIONED | Snapshot v1 内的 budget schema v1 |
| ToolInvocation | PUBLIC_STABLE | 调用边界，原始 arguments 不进入 Journal/Wire |
| ToolExecutionResult / ToolExecutionError | PUBLIC_STABLE | 强类型执行结果；不是 state owner |
| ToolCompletedPayload | PUBLIC_VERSIONED | Event 内嵌 evidence v1；旧缺失字段保持 Unknown |
| RetrievalExecutionResult | PUBLIC_STABLE | invocation 结果；不持久化正文 |
| ModelInvocationResult | PUBLIC_STABLE | routing/retry 结果；不拥有 retry policy |
| RecoveryAssessment | INTERNAL_STABLE | 只读派生评估；不执行 recovery/replay |
| ShutdownReport | INTERNAL_STABLE | 派生报告；不写回 lifecycle |
| FaultPlan / FaultRule | TEST_ONLY | 测试 fault seam 配置；schema v1；无生产创建入口 |
| FaultDecision | TEST_ONLY | Controller 的瞬时派生决策 |
| FaultCoverageReport / FaultRuntimeInvariantReport | TEST_ONLY | 派生测试/文档报告 |
| EventPublicationEvidence / SnapshotPublicationEvidence | INTERNAL_STABLE | 冻结安全事实，不保存正文或 live owner |
| ToolCompletionGapFixture | TEST_ONLY | 仅存在于 `tests`；生产 RecoveryValidator 不接受 |
| Test Fake / Test Mutator | TEST_ONLY | 只能由测试显式创建/注入 |

`PUBLIC_*` 指阶段二项目代码可依赖的合同，不等于网络 API。只有明确的安全投影可以进入 Wire；内部 evolving 对象不能直接序列化。

## 4. Schema / Version / Digest Matrix

| contract | schema_version | digest_version | canonicalization_owner | reader_versions | writer_version | unknown_version_behavior | missing_field_behavior | write_back_behavior |
|---|---:|---|---|---|---:|---|---|---|
| AgentState | 1 | 无 | `AgentState.to_dict()` | 1 | 1 | `AgentState.from_dict()` fail closed | `schema_version` 缺失 fail closed；其他字段按当前 v1 校验/默认处理 | 不写回 |
| RuntimeEvent | 2 | 无独立 event digest | RuntimeEvent 安全投影；Journal 负责持久 digest | 1, 2 | 2 | 构造/Recovery consumer fail closed | v1/v2 可选字段使用既有默认或 Unknown | 不写回 |
| JournalRecord | 2 | 随 journal schema 的 canonical JSON SHA-256 | `event_journal.canonical_json/_digest` | 1, 2 | 2 | journal schema fail closed；未知 event schema 在 Recovery fail closed | v1 无 span 字段按 v1 digest 规则读取 | 不写回 |
| RunSnapshot | 1 | Snapshot v1 canonical JSON SHA-256 | `snapshot_serialization` + `RunSnapshot.digest_source()` | 1 | 1 | fail closed | 严格 v1 字段集合；不存在 Snapshot v0 | 不写回 |
| ToolCompletedPayload | Event 内 `tool_evidence_schema_version=1` | `result_digest` 使用 canonical JSON SHA-256，无单独版本号 | tool contract canonicalizer / Event allowlist | evidence 缺失（legacy Unknown）, 1 | 1 | 非 1 evidence fail closed | `result_present/result_digest` 缺失为 Unknown | 不写回 |
| Recovery evidence | 无独立 schema | 复用 Journal/Snapshot digest | Journal tail reducer / RecoveryValidator | Event 1/2 + Snapshot 1 | 不独立写入 | 上游未知版本 fail closed | 缺失历史工具证据保持 Unknown/需协调 | 不写回、不从 Registry 回填 |
| FaultPlan | 1 | semantic plan canonical JSON SHA-256（随 schema 1） | `FaultPlan.digest_source()` | 1 | 1 | fail closed | 规则字段由 dataclass 默认和严格校验处理 | 测试对象，不写回生产存储 |
| ShutdownReport | 未版本化 | 无 | 不适用 | 进程内当前类型 | 不持久化 | 不适用 | `completed` 兼容别名，不能解释为 fully closed | 不写回 |

冻结原则：不得使用 Python `repr` 计算持久 digest；不得虚构历史版本；不得在读取旧版本时升级写回；不得使用当前 Registry 或 live adapter 回填历史 evidence。

## 5. Legacy / Coordinated Boundary

| 能力 | Coordinated | Legacy |
|---|---|---|
| 默认入口 | 是 | 否 |
| 显式配置 | `COORDINATED` | `LEGACY` |
| RunContext | 是 | 是 |
| AgentState | 是，RunCoordinator 写 | 是，AgentLoop 写 |
| Event Journal | 是 | 未接入完整 Runtime journal |
| Snapshot | 可选接入 | 未接入 |
| Recovery | 只读 validation，可选 | 未接入 |
| Fault Injection | 仅显式测试 seam | 生产未接入 |
| Graceful Shutdown | Registry + coordinated worker | Registry + legacy step worker |
| Worker Tracking | 是 | dedicated legacy executor |
| Tool Contract | 是 | 通过既有 AgentRouter/adapter，能力不等同完整 Coordinated contract |
| Streaming | RuntimeEvent -> 兼容文本 adapter | 既有同步文本 generator bridge |

Model retry/fallback 是所选 Runtime 内的 Model Router 策略，不是跨 Runtime fallback。两条路径共享 Application resource 时，ApplicationRuntimeServices 是 close owner；Run facade 无权关闭共享 recorder、store、engine 或 executor。

## 6. Fault Injection Production Isolation

生产默认事实为 `fault_controller=None`。Settings、环境变量、HTTP header、`ChatRequest` body、Prompt/message、Tool arguments 均不能创建或选择 FaultPlan。没有 module-global controller，也没有 current fault `ContextVar`；ApplicationRuntimeServices 不保存 controller；Journal/Snapshot/Wire 不持久化 FaultRule 或 rule id。

Fault seam 通过显式参数存在于 Runtime 组件，只有测试使用 `FaultInjectionController.for_test()` / `FaultInjectionScope` 创建。Coverage/Support/Invariant report 是 TEST_ONLY 派生产物，不进入生产请求路径。

## 7. Authority / Evidence / Report / Fixture

| 类别 | 对象 | 权限边界 |
|---|---|---|
| Authority | AgentState、JournalRecord、RunSnapshot、RunRegistry/worker snapshots | 各自唯一 owner 写入；其他组件只读/投影 |
| Frozen Evidence | ToolCompletedPayload、EventPublicationEvidence、SnapshotPublicationEvidence | 只保存 allowlist 安全事实、digest 和必要 identity；不保存正文/live object |
| Derived Report | ShutdownReport、FaultCoverageReport、FaultRuntimeInvariantReport、Observability/Trace health | 只读派生；不得写回 Authority，不持有 Runtime owner |
| Test Oracle / Fixture | ToolCompletionGapFixture、corruption fixture、PhaseAwareToolAdapter counters | 只在 `tests`；生产 Validator 不接受 |

## 8. Deprecated / Compatibility Matrix

| item | current_behavior | replacement | compatibility_period | removal_precondition | tests |
|---|---|---|---|---|---|
| `ShutdownReport.completed` | `orchestration_completed` 的兼容别名，不表示资源 fully closed | `orchestration_completed` 或 `fully_closed` | 当前阶段保留 | 所有调用者按真实语义迁移 | `test_shutdown_report_truthfulness.py`, `test_runtime_report_authority.py` |
| 旧 Event evidence 缺失 `result_*` | 读取为 Unknown，不从当前对象回填 | v1 tool evidence 字段 | Event v1/v2 reader 期间 | 历史数据退役且迁移策略明确 | `test_event_schema_compatibility.py` |
| Legacy Runtime mode | 显式回滚路径继续支持，不作为默认或 fallback | Coordinated 默认入口 | 阶段二保留 | 产品确认 Legacy 调用方全部迁移 | `test_runtime_legacy_boundary.py`, `test_default_runtime_entry.py` |
| 旧 Tool Adapter method signature | adapter 边界保留兼容调用方式；不改变 Tool 业务语义 | 当前强类型 Tool adapter/context | 现有 adapter 调用方存在期间 | 调用方与测试全部迁移 | tool adapter/execution tests |
| 旧 Model Generator compatibility | Generator adapter 继续映射当前 Model contract | 当前 ModelAdapter result/stream contract | 现有本地/测试 generator 存在期间 | model adapter 全部迁移 | model invocation/event integration tests |
| Event v1/v2 | reader 接受 1/2，writer 固定 2 | Event v2 | 历史 Journal 仍需读取期间 | v1 数据正式退役 | event/recovery compatibility tests |
| Snapshot v1 | 唯一真实 Snapshot 版本；不存在 v0 | 后续版本尚未定义 | 当前冻结期 | 新版本有正式迁移设计 | snapshot/recovery version tests |
| 旧 Fault Trigger | 当前只有确定性 trigger；没有已删除 trigger 的运行时兼容入口 | 当前 `FaultTrigger` | 不适用 | 不虚构兼容项 | fault contract tests |
| 旧 `.event` publication error access | 当前 `EventPublicationError` 暴露冻结 `evidence`，不持有 RuntimeEvent | `error.evidence` | 兼容调用方存在期间 | 调用方迁移且安全审计通过 | event fault tests |
| 旧 constructor positional arguments | 已有 dataclass/handle 的位置参数保持兼容 | 新调用优先关键字参数 | 当前调用方存在期间 | 全部调用点迁移并有 deprecation 周期 | contract/run registry tests |

## 9. Health / Readiness Diagnostic Projection

WP1-C 冻结的 Health / Readiness 是**纯只读诊断投影**，不是 Runtime Authority，也不是 Lifecycle / Admission 的第二个 owner。

```text
Diagnostic Projection != Lifecycle Authority
```

职责边界：

| 对象 | Owner / 角色 |
|---|---|
| Lifecycle Authority | `ApplicationRuntimeServices._LifecycleControl`（只经 `begin_shutdown()` / `close()` 写） |
| Admission Authority | `ApplicationRuntimeServices.admission_gate`（`RuntimeAdmissionGate`） |
| Startup Dependency Snapshot | `ApplicationRuntimeServices.startup_dependency_snapshot`（frozen `StartupDependencySnapshot`；lifespan 构造一次，运行中不修改、不持久化） |
| Diagnostic Projector | `core/runtime/health.py`（`resolve_application_diagnostic` + `ApplicationDiagnosticSnapshot` + `DiagnosticStatus`） |
| Endpoint | FastAPI `GET /health`、`GET /readyz` = Reader |
| Client probe | `core/client_readiness.py::ReadinessWorker`（QThread，经 HTTP `GET /readyz`）= Reader |

- Endpoint 与 Client probe 只读不写：不修改 lifecycle、admission、dependency 或 Run state，不触发 recovery/retry，不缓存诊断结果。
- Resolver 只读取既有事实；services 尚不存在时，仅允许 `app.state.runtime_lifecycle_state` 作为有限 fallback（pre-services `STARTING` / shutdown / closed 纯投影视图），不写回 services / app.state。
- `DiagnosticStatus` 是派生状态（`STARTING` / `READY` / `READY_DEGRADED` / `DRAINING` / `CLOSED` / `UNAVAILABLE`），不得与 `RuntimeLifecycleState`（仍恰为 `STARTING` / `READY` / `SHUTTING_DOWN` / `CLOSED` 四值）混淆；`READY_DEGRADED` 不进入 RuntimeFactory / AdmissionGate 消费。

HTTP 状态矩阵（两个 endpoint 返回同一四字段 body，仅 HTTP 判定语义不同）：

| Source facts | Diagnostic `status` | `/health` | `/readyz` |
|---|---|---|---|
| services 尚未构造，fallback `STARTING` | `STARTING` | 200 | 503 |
| lifecycle=`READY`，admission=`ACCEPTING`，KB 正常 | `READY` | 200 | 200 |
| lifecycle=`READY`，admission=`ACCEPTING`，allowed KB degraded | `READY_DEGRADED` | 200 | 200 |
| lifecycle=`READY`，admission=`DRAINING` | `DRAINING` | 200 | 503 |
| lifecycle=`SHUTTING_DOWN`，admission=`DRAINING` | `DRAINING` | 200 | 503 |
| lifecycle=`CLOSED`，admission=`CLOSED` | `CLOSED` | 503 | 503 |
| 无法安全确认（inconsistent / unknown / 无 fallback） | `UNAVAILABLE` | 503 | 503 |

响应 body 固定四字段：

```json
{
  "status": "...",
  "lifecycle": "...",
  "admission": "...",
  "degraded": false
}
```

安全边界：body 不含 error / reason / error_code / path / URL / exception / version / environment / instance / run count / timestamp；序列化字段顺序固定，不使用 `vars` / `__dict__` / raw Enum repr。

KB degraded 语义：`knowledge_base_required=false` 且 KB 初始化/import 失败 → `knowledge_base_degraded=true`（lifespan 从同一次真实 KB 初始化结果构造 snapshot）；`knowledge_base_required=true` 的 KB 失败在 lifespan fail fast，不到达 READY。不得把 `AgentRouter.knowledge_base_error` 升级为 diagnostic Authority。

第一版边界：

- startup-only；无 continuous monitoring、无 auto reconnect、无 manual readiness button；
- 无 post-start dependency aggregate health（Model circuit、Journal append failure、Observability consumer、Executor saturation 均不参与动态诊断）；
- 无 version compatibility / fingerprint（DEFER_TO_WP4）；
- STARTING / CLOSED / draining 的完整 HTTP 网络可观察窗口不保证（矩阵定义 pure projection / 已接受请求语义）。

## 10. Frozen Invariants

- PlanStep 不保存 runtime status；AgentState 是运行状态 owner。
- RetryExecutor/ModelInvocationRouter/ToolExecutionService 拥有 retry；Fault controller 只注入结果，不决定策略。
- ModelRoutingPolicy/Router 拥有 model fallback；不存在跨 Runtime fallback。
- AttemptSideEffectTracker 与 Tool attempt flow 拥有 side-effect/compensation facts；Error mapper 只生成类型化结果。
- RuntimeEventChannel 是 per-run event sequence owner；RunCoordinator 是 terminal owner。
- Checkpoint/RunSnapshot 是 snapshot capture 与 digest owner；RecoveryValidator 只读评估。
- GracefulShutdownCoordinator 是 shutdown orchestration owner；ShutdownReport 只是派生报告。
- FaultInjectionController 是 match/hit counter owner，但只存在于显式测试/operation scope。
- Application resource 按 identity 关闭一次；Run scope 不关闭 Application resource。
- `RuntimeLifecycleState` 恰为 `STARTING`/`READY`/`SHUTTING_DOWN`/`CLOSED` 四值；`DiagnosticStatus` 是派生只读状态，不得成为 writable lifecycle。
- `StartupDependencySnapshot` 是 frozen、application-scope、lifespan 构造一次的 immutable 启动事实；运行中不修改、不持久化。
- Health / Readiness endpoint 与 Client readiness probe 只读投影；不得写回 lifecycle / admission / snapshot，不触发 recovery/retry。
- `/readyz` 的 readiness 结论必须与 `/api/chat` 消费的同一 `ApplicationRuntimeServices.admission_gate` identity 一致。
