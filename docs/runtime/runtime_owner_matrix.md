# Runtime Owner Matrix

本矩阵冻结 Runtime 事实的唯一权威 owner。`readers` 可以投影事实，`writers` 只能通过 owner 的受控 API 修改；Report、Evidence 与测试 Oracle 均不得升级为 owner。

| fact | authoritative_owner | readers | writers | persistence_location | lifecycle_scope | must_not_own |
|---|---|---|---|---|---|---|
| runtime selection | ChatRuntimeSelector 的不可变 mode + 请求入口局部快照 | `/api/chat`, ChatService | Settings/lifespan 装配一次 | Settings 进程内值 | APPLICATION_SCOPE，读取为 request entry snapshot | Runtime failure handler、Model router、HTTP retry |
| run identity | RunIdentifiers / RunContext | State、Event、Trace、Registry、Journal | `create_run_context()` | Event/Journal/Snapshot 中的安全 identity | RUN_SCOPE | Report、Fault controller、Adapter |
| trace identity | RunIdentifiers / RunContext | Emitter、Span recorder、Journal | `create_run_context()` | Event/Journal/Snapshot | RUN_SCOPE | Model/Tool adapter、Report |
| run state | AgentState | Coordinator、Scheduler、Snapshot、observer | AgentStateMachine，经 Parent Runtime 调用 | AgentState schema / RunSnapshot 投影 | RUN_SCOPE | Plan、Report、RecoveryValidator |
| step state | AgentState.steps / StepState | Scheduler、Coordinator、Snapshot | AgentStateMachine | AgentState schema / RunSnapshot 投影 | RUN_SCOPE | PlanStep、Event payload、Fault controller |
| stop reason | AgentState.stop_reason | Coordinator、Snapshot、Terminal payload | AgentStateMachine | AgentState / Snapshot / terminal safe payload | RUN_SCOPE | ShutdownReport、RecoveryAssessment |
| cancellation reason | CancellationSource/Token 首次取消事实 | RunContext、Coordinator、Worker、Shutdown | Run handle 所有者调用 `cancel()` | Snapshot 的安全 cancellation projection；不持久化 source | RUN_SCOPE | Event channel、Report、Fault controller |
| deadline | RunContext Deadline | Model/Tool/Retrieval/Budget/Coordinator | Context factory 初始化一次 | RunContextData.deadline_at / Snapshot 安全元数据 | RUN_SCOPE | Retry sleeper、Adapter、Report |
| plan definition | immutable Plan / PlanStep | Scheduler、Coordinator、Snapshot fingerprinter | Planner / deterministic plan factory | PlanSnapshot 仅安全静态投影 | RUN_SCOPE immutable definition | AgentState、Scheduler status、Recovery report |
| runtime step status | AgentState StepState | Scheduler、Coordinator、Event projection | AgentStateMachine | AgentState / Snapshot | RUN_SCOPE | PlanStep、RuntimeEventDraft、Report |
| retry | RetryExecutor + RetryPolicy；调用服务提供安全判定输入 | Model/Tool execution | RetryExecutor 决定 attempt progression | 不作为独立持久状态；事件只记录 index/result | INVOCATION_SCOPE | Fault controller、Error mapper、Circuit breaker |
| fallback | ModelInvocationRouter + ModelRoutingPolicy | Model invocation、Metrics | Router 按既有策略选择 candidate | Model event 安全路由事实 | INVOCATION_SCOPE | Runtime selector、HTTP handler、Fault controller |
| circuit state | ModelCircuitBreaker / Registry | Model router、Gauge/Metrics | Circuit breaker permit/result API | 进程内 registry snapshot | APPLICATION_SCOPE / COMPONENT_SCOPE | Model adapter、Report、Run scope |
| budget reservation | BudgetLedger | Coordinator、Model/Tool/Retrieval | BudgetLedger.reserve/release | RunSnapshot budget projection | RUN_SCOPE | Attempt result、Event、Fault controller |
| budget commit | BudgetLedger | Coordinator、Snapshot、Metrics | BudgetLedger.commit | RunSnapshot budget projection | RUN_SCOPE | Model/Tool result、Report |
| model usage | ModelInvocationResult（invocation）与 BudgetLedger（run aggregate） | Router、Budget、Event/Trace | Model invocation flow；ledger commit API | 安全 event counts / Snapshot aggregate | INVOCATION_SCOPE + RUN_SCOPE aggregate | Adapter、Fault controller、Metrics report |
| tool invocation identity | ToolInvocation | Tool service、Event evidence、Trace | ToolInvocation factory | 仅 digest 进入 Journal evidence | INVOCATION_SCOPE | Fault rule、Report、Recovery fixture |
| tool attempt identity | Tool attempt execution context/result | Tool service、Event evidence、Recovery reducer | ToolAttemptExecutor | 仅 digest 进入 Journal evidence | ATTEMPT_SCOPE | ToolInvocation、Fault controller、Report |
| tool side-effect state | AttemptSideEffectTracker；完成后冻结到 result/evidence | Retry safety、Recovery、Event projection | Tool attempt/provider authoritative response | ToolCompletedPayload safe evidence / Journal | ATTEMPT_SCOPE，evidence 持久化 | Tool error mapper、Fault controller、Report |
| tool compensation state | Tool attempt/adapter compensation result，完成后冻结 evidence | Retry safety、Recovery | Tool execution flow | ToolCompletedPayload safe evidence / Journal | ATTEMPT_SCOPE | Fault controller、RecoveryValidator、Report |
| resource lease | ToolConcurrencyController + ToolResourceLease identity | Tool attempt、Worker tracker、Shutdown | Controller acquire；lease release（detached 时 callback） | 进程内 worker snapshot；不持久化 raw key | ATTEMPT_SCOPE / COMPONENT_SCOPE | Report、Fault controller、Run facade |
| worker active/detached state | BoundedBlockingExecutor / ToolConcurrencyController | Run handle、Gauge、Shutdown | Worker lifecycle API/callback | 进程内 snapshot；ShutdownReport 为派生 | COMPONENT_SCOPE / INVOCATION_SCOPE | ShutdownReport、Application service fields |
| event identity | RuntimeEventChannel 通过 RuntimeEvent.from_draft 创建 | Journal、Transport、Observability | Channel publish critical section | RuntimeEvent / JournalRecord | RUN_SCOPE | Emitter caller、Observability、HTTP adapter |
| event sequence | RuntimeEventChannel `_sequence` | Journal、Transport、Checkpoint watermark | Channel publish lock 内唯一递增 | JournalRecord.sequence | RUN_SCOPE | Journal、Emitter、RecoveryValidator |
| terminal event | RunCoordinator | Channel、Journal、Adapter、Metrics | RunCoordinator terminal path | RuntimeEvent/JournalRecord | RUN_SCOPE | EventChannel close、HTTP EOF、Adapter、Report |
| journal append | RunEventJournal implementation | Recovery、Observability consumer、tests | RuntimeEventChannel journal-first publish | InMemory/SQLite journal | APPLICATION_SCOPE store，记录属 RUN | Observability/Trace、RecoveryValidator |
| snapshot capture | CheckpointCoordinator / RunSnapshot.create | SnapshotStore、RecoveryValidator | Checkpoint operation | SnapshotStore | OPERATION_SCOPE output，APPLICATION store | RecoveryValidator、Report、Fault controller |
| snapshot digest | RunSnapshot.digest_source + snapshot_serialization canonicalizer | SnapshotStore、RecoveryValidator | RunSnapshot.create | RunSnapshot.payload_digest | PUBLIC_VERSIONED artifact | Store registry、repr、Recovery report |
| recovery assessment | RecoveryValidator 生成 immutable RecoveryAssessment | 调用方、测试、文档 | RecoveryValidator 只读计算 | 默认不持久化 | OPERATION_SCOPE | AgentState、Snapshot、Journal、test fixture |
| observability health | RuntimeObservabilityDispatcher / recorder snapshots | Gauge provider、Shutdown、tests | Dispatcher/consumer lifecycle | checkpoint store + in-memory health snapshot | APPLICATION_SCOPE | Journal、AgentState、ShutdownReport |
| trace health | SpanRecorder | Shutdown、tests、diagnostics | Span lifecycle API | recorder backend / safe snapshot | APPLICATION_SCOPE / COMPONENT_SCOPE | Journal、AgentState、Report |
| shutdown orchestration | GracefulShutdownCoordinator | lifespan、tests | Coordinator 单次受锁 `shutdown()` | ShutdownReport 仅派生，不持久化 | OPERATION_SCOPE under APPLICATION | ShutdownReport、Run facade、component close callback |
| component close result | ApplicationRuntimeServices close ledger；Coordinator 聚合 | ShutdownReport、logs | ApplicationRuntimeServices bounded close | 进程内 RuntimeComponentResult | APPLICATION_SCOPE shutdown operation | component alias、Report、Run scope |
| fault match/hit count | FaultInjectionController | Fault reports、tests | Controller.evaluate under lock | FaultControllerSnapshot；不进生产 Journal/Wire | OPERATION_SCOPE + TEST_SCOPE | RetryExecutor、Application services、Fault report |
| client http session trust_env | Settings.client_trust_env（唯一解析快照） | main.py client sessions（聊天/历史/搜索/取消）+ ui/chat_panel.py plumbing + ui/memory_dialog.py memory session | `session.trust_env = settings.client_trust_env`（main.py 直接消费）；`ChatPanel` 仅透传快照值，`MemoryManagerDialog.http.trust_env = client_trust_env`（ui/ 不读 env） | Settings 进程内值；不持久化 | APPLICATION_SCOPE | Server 侧 Remote LLM Session、requests 默认行为、脚本 |
| server process topology | 部署合同：exactly one LocalAgent server application process per deployment instance | operator、deployment runbook、capability matrix | 运维以 `uv run python server.py` 启动；禁止 `--workers N`/gunicorn/multi-process | 无 | APPLICATION_SCOPE | Windows Service wrapper、Docker/Compose、multi-process Runtime |
| deployment rollback identity | 人工 Deployment Rollback（known-good artifact/env + persistence compatibility + smoke） | operator、deployment runbook | 人工执行；不提供 automatic rollback | 无 | OPERATION_SCOPE（运维流程） | `CHAT_RUNTIME_MODE=legacy` Runtime Legacy Rollback、automatic recovery |
| runtime lifecycle | `ApplicationRuntimeServices._LifecycleControl`（只经 `begin_shutdown()`/`close()` 写） | diagnostic resolver、RuntimeFactory、GracefulShutdownCoordinator、tests | `begin_shutdown()`、`close()`；`server.py::lifespan()` 写 `app.state.runtime_lifecycle_state`（发布视图，非独立 owner） | 进程内状态；不直接持久化 | APPLICATION_SCOPE | endpoint、response、Client probe、HealthManager、第二 lifecycle state machine |
| new Run admission | `ApplicationRuntimeServices.admission_gate`（`RuntimeAdmissionGate`） | diagnostic resolver、ChatService、RuntimeFactory、GracefulShutdownCoordinator | `close_admission()`、`mark_closed()` | 进程内状态 | APPLICATION_SCOPE | readiness bool、endpoint cache、第二 Admission owner |
| startup dependency snapshot | `ApplicationRuntimeServices.startup_dependency_snapshot`（frozen `StartupDependencySnapshot`） | diagnostic resolver | `server.py::lifespan()` 从同一次真实 KB 初始化结果构造一次 | 进程内 immutable；不持久化 | APPLICATION_SCOPE | AgentRouter.knowledge_base_error、endpoint、Client probe、dependency health manager |
| diagnostic projection | `core/runtime/health.py`（`resolve_application_diagnostic` / `ApplicationDiagnosticSnapshot` / `DiagnosticStatus`） | FastAPI `/health`、`/readyz` endpoint、Client readiness probe（经 HTTP）、tests | 无（只读投影；不写回 lifecycle/admission/snapshot） | 无；response 是 derived value，不缓存 | APPLICATION_SCOPE（projection） | Runtime lifecycle、Admission、StartupDependencySnapshot、Run state |
| client readiness worker | `core/client_readiness.py`（`ReadinessWorker(QThread)`，一次 startup probe；Owner = MainController） | MainController、ChatPanel（经 signal） | `requestInterruption()`；terminal 后自动退出 | 无 | APPLICATION_SCOPE，active lifetime 仅一次 probe | Server lifecycle、Admission、continuous monitor、manual readiness button |

## Duplicate-owner audit

| 风险 | 结论 |
|---|---|
| PlanStep 保存 runtime status | 已禁止；字段合同不含 status/attempt/error |
| AgentState 与 Plan 双写 | 未发现；Plan 不可变，状态只在 AgentState |
| Fault Controller 决定 Retry | 未发现；Controller 只返回/抛出注入结果，RetryExecutor 仍做决策 |
| Tool Error Mapper 修改 Side-effect Tracker | 未发现；Tracker 在 attempt flow 更新，mapper 冻结结果 |
| Event Channel 创建第二 Terminal | 未发现；Channel 只排序/持久化，RunCoordinator 创建 terminal |
| RecoveryValidator 修改 AgentState | 未发现；输入只有 Snapshot/Plan/Journal，输出 Assessment |
| Observability/Trace 修改 Journal | 未发现；仅消费 JournalRecord 或记录 Span |
| ShutdownReport 修改 Lifecycle | 未发现；Report 是 immutable derived value |
| Run facade 关闭 Application Recorder | 未发现；Scope 只收口 request-owned 资源 |
| Application Service 缓存 current Run Controller | 未发现；容器字段和 factory slots 均无 current scope/controller |
| 未执行 Scope 正常 close 遗留 Registry handle | 审计中真实发现并修复；`_finish_close()` 按 handle identity 注销 |
| 同一共享资源由不同名字关闭两次 | identity 去重；契约测试覆盖 alias close once |
