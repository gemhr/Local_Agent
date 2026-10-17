# Runtime Owner Matrix

## Scoped Memory Lifecycle Owners

| 事实 | 唯一 Owner | 持久化及边界 |
| --- | --- | --- |
| 可信身份/Session | Auth Principal → typed MemoryAccessContext | tenant/user 不由 agent/body/model 授予；CONVERSATION 为 SESSION identity |
| Scope、CAS、TTL、source suppression | 原 PG Store 的 MemoryLifecycleRepository | memory_scopes/operations + 原 Memory/Exchange 表；Scope 锁、record_version、ACTIVE key 唯一约束 |
| delivered Exchange | RunFinalMemoryWriter | 仅已 DELIVERED 写入；独立于长期 Formation；旧源 replay 不重建正文 |
| Formation | SemanticMemoryFormation + MemoryService | prepare 在提取前；原 ModelInvocationRouter/预算；稳定候选和原子 CAS；失败不改 Final |
| scoped Retrieval/History | MemoryService + PG SQL → ContextBuilder | Scope/TTL/来源先过滤再 LIMIT；不使用全局摘要、缓存或全局 fallback |
| Run Memory 当前使用资格 | MemoryContextGuard + 原 attempt/Decision/Tool/Final 事务 | 复用 control/fence/锁序；只阻止新使用，不接管 terminal/reconciliation |
| 生命周期 Evidence | memory_evidence 安全投影 | 只读真实事实并封存；缺授权/来源/版本/传播证据必须 PARTIAL |

完整合同、Breaking Change 和限制见 [Scoped Memory Lifecycle v1](scoped_memory_lifecycle_v1.md)。旧 agent-only authorizer、PG 全局 Memory API、自动 episode/滚动摘要不是新生产 Authority；SQLite Domain helper 仅本地/测试用途。`memory.formation` 不扩展冻结 Trace v2。

## Stage 3 WP3 Resource Authorization Owner

| Fact | Authority / Scope | Readers | Construction / mutation | Persistence | Forbidden behavior | Contract |
| --- | --- | --- | --- | --- | --- | --- |
| Arbitrary File Tool filesystem READ authorization | `ResourceAuthorizationService` + frozen `FilesystemResourcePolicy` / `ToolResourceExtractorCatalog`（APPLICATION_SCOPE） | AgentRouter pre-execution Gate、startup validation、测试 | 仅 `server.py::lifespan()` 从 Settings snapshot 与 frozen ToolRegistry 构造；运行期只读 | 无 | Tool function/Adapter/Governance 自行 allow；请求扩大 roots；raw path 进入安全投影 | INTERNAL_RC |

Tool Governance 仍拥有 Agent→Tool Permission/Risk/Approval，`ToolExecutionService` 仍拥有实际执行；Resource Authorization 不成为第二 ToolRegistry，也不修改 `ToolInvocation` / `ToolExecutionSpec` / RuntimeEvent / Journal。`workspace_read_file` / `workspace_write_file` 不属于 arbitrary filesystem read：其 Adapter 只接受相对 path，并在业务 I/O 前以 resolved containment 固定生产 Demo Root；该固定边界不授予任意 read root，也不是通用 Sandbox。

## Stage 3 WP3-B HTTP Payload Owners

| Fact | Authority / Scope | Readers | Construction / mutation | Persistence | Forbidden behavior | Contract |
| --- | --- | --- | --- | --- | --- | --- |
| 冻结 payload limits（含 history `limit` default `10` / range `1..100` 与 `offset` default `0` / range `0..100000`） | `RequestPayloadPolicy` / `REQUEST_PAYLOAD_POLICY`（APPLICATION_SCOPE，唯一 numeric facts Owner） | `RequestBodyLimitMiddleware`、`server.py` FastAPI/Pydantic schema | import 时构造一次；所有值必须等于冻结默认值；route 只引用 policy facts | 无 | route 复制 numeric default；Settings/env/request 覆盖；Runtime Budget 反向拥有 HTTP limit | INTERNAL_RC |
| raw HTTP body bytes 与 `Content-Length` 合法性 | `RequestBodyLimitMiddleware`（REQUEST boundary） | 下游 ASGI app 仅接收校验后 replay | 对单请求完整缓冲、计数、拒绝或原序 replay | 无；不进入 Event/Journal/Log | 只信声明长度；超限后调用 endpoint；记录 raw body/header | INTERNAL_RC |
| endpoint 字段长度/数量/范围 | FastAPI/Pydantic schema（REQUEST boundary） | endpoint / ChatService / MemoryManager | request validation；成功后只传已校验模型/参数 | 仅既有业务路径；校验失败无 mutation | 把字段 Gate 当 Tool authorization、human IAM、DLP 或 Runtime Budget | INTERNAL_RC |

## Stage 3 WP3-C Context Trust / Denial Owners

| Fact | Authority / Scope | Readers | Construction / mutation | Persistence | Forbidden behavior | Contract |
| --- | --- | --- | --- | --- | --- | --- |
| Model Context source/trust到role绑定 | `ContextBuilder`（INVOCATION_SCOPE） | AgentRouter、Synthesis adapter、tests | code构造typed `ContextItem`；`bind_messages()`确定role | 无独立持久化 | 调用方自行把data升为system；从正文/label猜authority；raw history注入system | INTERNAL_RC |
| Tool observation trust | `ContextItem(TOOL_RESULT, UNTRUSTED_EXTERNAL)` | AgentRouter model invocation | Tool执行结果仅作为user/data绑定；code-owned control单独保留system | 仅既有业务Wire/Memory边界；不进入安全投影 | raw Tool Result进入任一system message | INTERNAL_RC |
| Synthesis dependency trust | `ContextItem(STEP_RESULT, USER_CONTENT)` | `SynthesisAgentAdapter` / `ContextBuilder` | 每个`DependencyResultEntry`独立构造user/data item | StepResultStore仅Run内；Snapshot不rehydrate正文 | Specialist正文成为system instruction或security authority | INTERNAL_RC |
| Security denial disposition/code | actual `ToolGovernanceError` / `ResourceAuthorizationError`在Agent adapter boundary映射的`ResultDisposition` + `SecurityDenialCode` | StepResult committer/store、DependencyResultView、Synthesis | 只由实际code-owned Gate exception产生并单调传播 | 当前Run内StepResultStore；无dedicated Event/Journal/Snapshot fact | string/regex/keyword matching生成denial；模型或正文清除/覆盖denial | INTERNAL_RC |
| Denial dominance | `SynthesisAgentAdapter` | RunCoordinator、OutputGate、delivered-only Memory | 在context build/model前检查required dependency disposition；任一denial返回fixed safe result | 仅既有final delivery/Memory；无新schema | post-denial prompt build/model selection/model invocation；mixed success覆盖denial | INTERNAL_RC |

`ContextBuilder`不拥有Tool Permission、Risk/Approval或Resource Authorization；它只拥有role binding。`SynthesisAgentAdapter`不成为新的security Gate，它只消费typed denial事实并执行`DENIAL_DOMINATES`。OutputGate、RuntimeEvent、Journal、Snapshot与Recovery Owner均未改变；Recovery不能从现有持久事实重建runtime-internal typed denial。

## Stage 3 WP3-D SQL Statement Authority Owners

| Fact | Authority / Scope | Readers | Construction / mutation | Persistence | Forbidden behavior | Contract |
| --- | --- | --- | --- | --- | --- | --- |
| SQL structure owner | code within current LocalAgent PostgreSQL persistence inventory（APPLICATION/OPERATION/INVOCATION scope按既有Store边界） | Memory/Journal/Snapshot/Checkpoint stores、Alembic migration owner | fixed literals、SQLAlchemy expression trees、bound parameters、code-owned order mapping | 复用既有 PostgreSQL stores；无新增security state | User/Model/RAG/Tool/Memory/HTTP text成为statement、identifier、keyword或ordering authority | SUPPORTED（scoped） |
| Untrusted SQL values | owning Store/repository SQLAlchemy callsite；HTTP route schema先拥有请求类型/范围校验 | asyncpg / SQLAlchemy | bound parameters；值与statement structure分离 | 按既有业务/Runtime Store合同 | f-string、`+`、`%`、`.format()`或unresolved helper拼接untrusted values | SUPPORTED（scoped） |
| Direct PostgreSQL owner inventory | `core/persistence/` 与 `core/persistence/repositories/` | test-only AST Gate、reviewer | SQLAlchemy/text sink inventory冻结owner discovery；新增owner必须显式re-gate | 无 | 未登记owner或未知receiver静默获得SQL authority | TEST_ORACLE |
| schema-metadata PRAGMA exception | 精确startup/internal/read-only helper | persistence preflight/migration | fixed metadata name、narrow shape、`sqlite3.Error` fail closed | 无业务正文 | 通用identifier interpolation、请求/模型控制PRAGMA、扩大exception shape | INTERNAL_RC |
| Chroma internal persistence | `VectorDBManager`/Chroma自身合同；不是LocalAgent direct SQL owner | startup compatibility marker readers | 仅既有collection metadata API | Chroma persistence | WP3-D AST Gate声称认证Chroma internal SQLite/schema | NOT_LOCAL_SCHEMA_OWNER |

test-only AST Gate 只是全production Python surface的owner/sink oracle，不是runtime SQL owner，也不修改生产路径。No generic SQL firewall、No NL2SQL、No SQL Tool；FTS query-language semantics 与 LIKE wildcard semantics仍属于搜索语义。未来Tool、NL2SQL、direct SQLite owner或新数据库技术必须重新过Gate；用户可见错误不泄漏不代表 internal logs 具有generic DLP。

本矩阵冻结 Runtime 事实的唯一权威 owner。`readers` 可以投影事实，`writers` 只能通过 owner 的受控 API 修改；Report、Evidence 与测试 Oracle 均不得升级为 owner。

| fact | authoritative_owner | readers | writers | persistence_location | lifecycle_scope | must_not_own |
|---|---|---|---|---|---|---|
| runtime selection | Coordinated Runtime 固定入口 | `/api/chat`, ChatService | lifespan 装配一次 | CoordinatedRuntimeFactory | APPLICATION_SCOPE | Runtime failure handler、Model router、HTTP retry |
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
| fallback | ModelInvocationRouter + ModelRoutingPolicy | Model invocation、Metrics | Router 按既有策略选择 candidate | Model event 安全路由事实 | INVOCATION_SCOPE | HTTP handler、Fault controller |
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
| tool identity/catalog | ToolRegistry（APPLICATION_SCOPE，startup 冻结后只读） | AgentRouter、planner prompt、测试 | `register_all_tools(registry)` + `registry.freeze()` | 进程内只读；不持久化 | APPLICATION_SCOPE | ToolExecutionService、Adapter、Run 状态 |
| tool policy | ToolPolicyCatalog（APPLICATION_SCOPE，startup 冻结后只读） | ToolGovernanceService、AgentRouter、测试 | `register_default_tool_policies(catalog)` + `catalog.freeze()`（coverage/reference 校验） | 进程内只读；不持久化 | APPLICATION_SCOPE | ToolRegistry、AgentRegistry、Run 状态、ExecutionSpec 字段 |
| invocation governance | ToolGovernanceService（唯一 invocation-time Authority） | AgentRouter、测试 | Service.authorize_tool / evaluate_invocation（只读解释 Catalog + ToolExecutionSpec） | 不持久化；仅固定 code/enum/risk classification | APPLICATION_SCOPE | ToolExecutionService、AgentRouter、Tool/Adapter/Model 自行判断 |
| tool descriptor | immutable ToolDescriptor（name + description） | ToolRegistry、AgentRouter、planner prompt | ToolDescriptor 构造（name/description 校验） | 进程内只读 | APPLICATION_SCOPE | 执行状态、permission/risk（WP2-B） |
| tool registration/binding | immutable ToolRegistration（descriptor + adapter） | ToolRegistry、AgentRouter | registry.register()（duplicate / name-agreement 校验） | 进程内只读 | APPLICATION_SCOPE | 复制 ToolExecutionSpec 字段、第二 mutable map |
| tool resolution | AgentRouter（untrusted `resolve()` / trusted `require()`） | planner、Tool 执行调用方 | AgentRouter 只读消费已冻结 Registry | 不持久化 | APPLICATION_SCOPE | ToolExecutionService、Fault controller |
| tool execution | ToolExecutionService（sole production execution owner） | Step、Event、Recovery、Metrics | Service.execute（handler 由调用方传入） | ToolCompletedPayload safe evidence | INVOCATION_SCOPE | ToolRegistry、AgentRouter resolution |
| tool invocation-specific spec | ToolAdapter.spec_for(invocation) / ToolExecutionSpec | ToolExecutionService、Recovery、ToolGovernanceService（只读） | Adapter spec_for 派生 | 不持久化 | INVOCATION_SCOPE | ToolDescriptor、ToolRegistry |
| sandbox policy / backend selection | code-owned immutable `SandboxExecutionPolicy` referenced by `ToolExecutionSpec` + `ToolExecutionBackendResolver` | `ToolAttemptExecutor` | `TRUSTED_IN_PROCESS` / `ISOLATED` selection at attempt seam | content-free backend/policy evidence | INVOCATION_SCOPE | Model/User/MCP/provider metadata、ToolGovernanceService |
| execution containment | `TrustedInProcessExecutionBackend` / `DockerIsolatedExecutionBackend` | `ToolAttemptExecutor` | backend-internal lifecycle; existing Runtime owns deadline/cancellation/retry | existing `ToolAdapterResponse` -> `ToolExecutionResult` | ATTEMPT_SCOPE | second Tool Runtime、second Result Contract、MCP lifecycle |
| event identity | RuntimeEventChannel 通过 RuntimeEvent.from_draft 创建 | Journal、Transport、Observability | Channel publish critical section | RuntimeEvent / JournalRecord | RUN_SCOPE | Emitter caller、Observability、HTTP adapter |
| event sequence | RuntimeEventChannel `_sequence` | Journal、Transport、Checkpoint watermark | Channel publish lock 内唯一递增 | JournalRecord.sequence | RUN_SCOPE | Journal、Emitter、RecoveryValidator |
| terminal event | RunCoordinator | Channel、Journal、Adapter、Metrics | RunCoordinator terminal path | RuntimeEvent/JournalRecord | RUN_SCOPE | EventChannel close、HTTP EOF、Adapter、Report |
| journal append | PostgresRunEventJournal | Recovery、Observability consumer、tests | RuntimeEventChannel journal-first publish | PostgreSQL `runtime_event_journal` | APPLICATION_SCOPE store，记录属 RUN | Observability/Trace、RecoveryValidator |
| snapshot capture | CheckpointCoordinator / RunSnapshot.create | SnapshotStore、RecoveryValidator | Checkpoint operation | SnapshotStore | OPERATION_SCOPE output，APPLICATION store | RecoveryValidator、Report、Fault controller |
| snapshot digest | RunSnapshot.digest_source + snapshot_serialization canonicalizer | SnapshotStore、RecoveryValidator | RunSnapshot.create | RunSnapshot.payload_digest | PUBLIC_VERSIONED artifact | Store registry、repr、Recovery report |
| recovery assessment | RecoveryValidator 生成 immutable RecoveryAssessment | 调用方、测试、文档 | RecoveryValidator 只读计算 | 默认不持久化 | OPERATION_SCOPE | AgentState、Snapshot、Journal、test fixture |
| observability health | RuntimeObservabilityDispatcher / recorder snapshots | Gauge provider、Shutdown、tests | Dispatcher/consumer lifecycle | checkpoint store + in-memory health snapshot | APPLICATION_SCOPE | Journal、AgentState、ShutdownReport |
| trace health | SpanRecorder | Shutdown、tests、diagnostics | Span lifecycle API | recorder backend / safe snapshot | APPLICATION_SCOPE / COMPONENT_SCOPE | Journal、AgentState、Report |
| shutdown orchestration | GracefulShutdownCoordinator | lifespan、tests | Coordinator 单次受锁 `shutdown()` | ShutdownReport 仅派生，不持久化 | OPERATION_SCOPE under APPLICATION | ShutdownReport、Run facade、component close callback |
| component close result | ApplicationRuntimeServices close ledger；Coordinator 聚合 | ShutdownReport、logs | ApplicationRuntimeServices bounded close | 进程内 RuntimeComponentResult | APPLICATION_SCOPE shutdown operation | component alias、Report、Run scope |
| fault match/hit count | FaultInjectionController | Fault reports、tests | Controller.evaluate under lock | FaultControllerSnapshot；不进生产 Journal/Wire | OPERATION_SCOPE + TEST_SCOPE | RetryExecutor、Application services、Fault report |
| client http session trust_env | Settings.client_trust_env（唯一解析快照） | main.py client sessions（聊天/历史/搜索/取消）+ ui/chat_panel.py plumbing + ui/memory_dialog.py memory session | `session.trust_env = settings.client_trust_env`（main.py 直接消费）；`ChatPanel` 仅透传快照值，`MemoryManagerDialog.http.trust_env = client_trust_env`（ui/ 不读 env） | Settings 进程内值；不持久化 | APPLICATION_SCOPE | Server 侧 Remote LLM Session、requests 默认行为、脚本 |
| API process topology | 部署合同：one active API replica | operator、deployment runbook、Compose/Kubernetes manifests、capability matrix | Windows Native、Compose 或 Kubernetes 启动；禁止 `--workers N`/gunicorn/API multi-replica | 无 | APPLICATION_SCOPE | Publisher/Worker overlap、Windows Service wrapper、production HA |
| deployment rollback identity | 人工 Deployment Rollback（known-good coordinated artifact/env + persistence compatibility + smoke） | operator、deployment runbook | 人工执行；不提供 automatic rollback | 无 | OPERATION_SCOPE（运维流程） | automatic recovery |
| runtime lifecycle | `ApplicationRuntimeServices._LifecycleControl`（只经 `begin_shutdown()`/`close()` 写） | diagnostic resolver、RuntimeFactory、GracefulShutdownCoordinator、tests | `begin_shutdown()`、`close()`；`server.py::lifespan()` 写 `app.state.runtime_lifecycle_state`（发布视图，非独立 owner） | 进程内状态；不直接持久化 | APPLICATION_SCOPE | endpoint、response、Client probe、HealthManager、第二 lifecycle state machine |
| new Run admission | `ApplicationRuntimeServices.admission_gate`（`RuntimeAdmissionGate`） | diagnostic resolver、ChatService、RuntimeFactory、GracefulShutdownCoordinator | `close_admission()`、`mark_closed()` | 进程内状态 | APPLICATION_SCOPE | readiness bool、endpoint cache、第二 Admission owner |
| startup dependency snapshot | `ApplicationRuntimeServices.startup_dependency_snapshot`（frozen `StartupDependencySnapshot`） | diagnostic resolver | `server.py::lifespan()` 从同一次真实 KB 初始化结果构造一次 | 进程内 immutable；不持久化 | APPLICATION_SCOPE | AgentRouter.knowledge_base_error、endpoint、Client probe、dependency health manager |
| diagnostic projection | `core/runtime/health.py`（`resolve_application_diagnostic` / `ApplicationDiagnosticSnapshot` / `DiagnosticStatus`） | FastAPI `/health`、`/readyz` endpoint、Client readiness probe（经 HTTP）、tests | 无（只读投影；不写回 lifecycle/admission/snapshot） | 无；response 是 derived value，不缓存 | APPLICATION_SCOPE（projection） | Runtime lifecycle、Admission、StartupDependencySnapshot、Run state |
| client readiness worker | `core/client_readiness.py`（`ReadinessWorker(QThread)`，一次 startup probe；Owner = MainController） | MainController、ChatPanel（经 signal） | `requestInterruption()`；terminal 后自动退出 | 无 | APPLICATION_SCOPE，active lifetime 仅一次 probe | Server lifecycle、Admission、continuous monitor、manual readiness button |
| Memory schema/version truth | Alembic `Base.metadata` + PostgreSQL persistence Store-owned | Alembic CLI、startup readiness preflight | 显式 `alembic upgrade head`（SCRIPT_ROLE）；lifespan 只读检查 | PostgreSQL canonical tables；revision `19d1ccbe8526` | APPLICATION_SCOPE | Coordinator 内嵌 Memory SQL、第二 schema owner、startup 自动 upgrade |
| Journal physical shape | `PostgresRunEventJournal` + Alembic canonical schema | `PostgresRunEventJournal`、startup readiness preflight | 显式 Alembic migration（SCRIPT_ROLE） | PostgreSQL `runtime_event_journal` | APPLICATION_SCOPE | 直接 SQL 第二 owner、历史 row rewrite、用 row version 冒充 DB version |
| Snapshot schema/digest | Snapshot contract + `PostgresSnapshotStore`（validation-only） | RecoveryValidator、startup preflight（read-only） | schema 由 Alembic 显式迁移；无 writeback/adoption | PostgreSQL `runtime_snapshots` | APPLICATION_SCOPE（opt-in） | snapshot migration、写回、v0 创建 |
| Checkpoint shape | `PostgresEventConsumptionCheckpointStore` Store-owned | `RuntimeObservabilityDispatcher`、startup readiness preflight | schema 由 Alembic 显式迁移 | PostgreSQL `event_consumption_checkpoints` | APPLICATION_SCOPE | row migration、把 derived store 当 correctness backup |
| Chroma collection/chunk/embedding compatibility | `VectorDBManager` + `document_loader.SCHEMA_VERSION`（marker 读写/校验） | server startup（marker validation）、operator preflight | `publish_collection_marker`/`remove_collection_marker`（rebuild 流程最后/最先）；marker 由 `VectorDBManager` 维护 | Chroma collection metadata marker | APPLICATION_SCOPE | Chroma internal SQLite 修改、startup 自动 clear/rebuild、未验证 marker 就当作兼容 |
| retrieval index provenance（WP1） | `core/knowledge_base/retrieval_index_provenance.py` + `production_build.py`（`RetrievalIndexProvenance`、source/chunk-policy/ordered-chunk/embedding-asset-tree digest、active descriptor、原子发布） | 生产 build coordinator（写）、`server.py::lifespan`/`hybrid_provenance_validator`（读+校验）、Chroma v2 marker、BM25 artifact metadata | 唯一 writer 是 production build；startup 只读校验，绝不改 active.json / 不自动 rebuild | `localagent_retrieval/<collection_key>/` generation 目录 + active.json | APPLICATION_SCOPE | 第二 writer、startup 自动 rebuild、未验证 generation 就当作兼容、Hybrid 失败回退 baseline |
| backup / restore / rollback | Operator runbook（manual stopped-server） | operator | 人工复制 MUST_BACKUP set / restore set / code+data rollback | backup set（文件；不进入 Runtime Authority） | OPERATION_SCOPE（运维流程） | automatic backup/restore、把 Snapshot 当 backup、online raw copy、downgrade migration |
| trace contract v1 semantics | `core/runtime/trace_contract.py`（六个稳定 operation 与属性常量）+ `core/runtime/tracing.py`（span 原语与安全属性 allowlist） | Trace writers、export projection、tests | 仅 code 冻结；Span 创建仍由各运行 owner 完成 | 无独立持久化；完整 `SpanRecord` 集合 memory-only | PUBLIC_VERSIONED contract；span 为 RUN/COMPONENT scope | SpanRecorder、RunCoordinator、RuntimeEventChannel、Journal、AgentEvalOps、future exporter |
| consumer-neutral trace export contract | `core/runtime/trace_export_contract.py`（identity/version、稳定字段投影、required/optional/conditional presence、安全导出属性策略、category value-domain、`TraceCompatibilityEvaluator`；Export Contract Semantic Owner，唯一构建权威规范语义描述符 `export_contract_semantic_descriptor()`） | TraceExportDispatcher、fingerprint owner、tests | `project_span()` 只读投影已完成 `SpanRecord`；描述符每次 fresh 构建；不写回任何 Runtime owner | 无（不可变 `TraceExportEnvelope` 值；不持久化） | APPLICATION_SCOPE contract，按 envelope 值消费 | SpanRecorder、RunCoordinator、RuntimeEventChannel、Journal、AgentEvalOps、exporter transport、fingerprint digest 算法 |
| trace contract fingerprint | `TraceContractFingerprinter`（`core/runtime/trace_contract_fingerprint.py`；只消费 export Owner 描述符并 canonicalize+digest） | export projection、compatibility evaluator、tests | 不维护第二份 field/domain/policy literals；不读取 live Span/Run/Plan/Journal/Snapshot | 无 | APPLICATION_SCOPE code-owned | Run identity、Trace instance identity、Plan fingerprint、Journal/Snapshot digest、Run configuration、public contract 语义构建 |
| trace compatibility decision | `TraceCompatibilityEvaluator`（`core/runtime/trace_export_contract.py`） | TraceExportDispatcher、tests | 只读判断已知/未知 contract；固定 reason codes | 无 | APPLICATION_SCOPE | exporter retry/queue、AgentEvalOps adapter、transport |
| trace export dispatch（projection invocation / compatibility consumption / bounded queue / worker / drop-health / flush-close） | `TraceExportDispatcher`（`core/runtime/trace_export_dispatcher.py`，APPLICATION_SCOPE） | ApplicationRuntimeServices（lifecycle）、completion observer seam、metrics projection、tests | 唯一调用 `project_span()` 与消费 `TraceCompatibilityEvaluator`；queue.Queue bounded 非阻塞 put_nowait；单 daemon worker 串行 adapter send；构造注入可选 `metrics_recorder` | 无（queue/health 进程内；不持久化） | APPLICATION_SCOPE | export contract/fingerprint 语义、Runtime outcome、Journal、Snapshot、Recovery、AgentEvalOps mapping、retry/batch/durability、transport-specific mapping |
| trace exporter adapter | concrete `TraceExporter` protocol implementation（`core/runtime/trace_exporter.py` Protocol；adapter 由 Composition Root/WP4-C 注入） | TraceExportDispatcher worker（唯一调用者） | 对单个 `TraceExportEnvelope` 执行一次 external transport attempt；`close(timeout_seconds)` 物理关闭至多一次 | 无 | APPLICATION_SCOPE resource | raw `SpanRecord`、projection、compatibility、queue、retry、delivery guarantee、export contract 语义 |
| completed-span observer notification | `InMemorySpanRecorder` 单个可选 `completion_observer`（构造注入，默认 `None`；`record()` 本地 bookkeeping 后、lock 外 best-effort 调用） | TraceExportDispatcher（observer = `observe_completed_span`）、tests | recorder `record()` 在权威本地 append/drop 之后调用 observer；observer 普通异常隔离 | 无（不持久化） | APPLICATION_SCOPE construction seam | local record/drop 事实、transport、queue、worker、generic fan-out、plugin registry |
| trace export health | `TraceExportDispatcher.health()`（不可变 `TraceExportHealthSnapshot`：state/queue depth/capacity/计数/close_flush_failures/last safe code） | diagnostics、tests、lifecycle component classification | dispatcher 在 condition lock 内派生 | 进程内 | APPLICATION_SCOPE | span/envelope ID、fingerprint、endpoint、exception、正文 |
| trace export component result | `ApplicationRuntimeServices` close/flush ledger 产生的 `RuntimeComponentResult`（component=`trace_export_dispatcher`，operation=`FLUSH`/`CLOSE`） | ShutdownReport、logs、tests | `ApplicationRuntimeServices.flush()/close()` bounded invoke 后按 dispatcher health 分类（`RUNTIME_TRACE_EXPORT_FLUSH/CLOSE_TIMEOUT/FAILED`） | 进程内 | APPLICATION_SCOPE shutdown operation | observability_flush_status、trace_flush_status merge、ShutdownReport schema 变更、raw exception/IDs |

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
| Persistence Coordinator 成为 Store schema owner | 未发现；Coordinator 不内嵌 Store SQL，Store-specific signature/version/transaction 保留在 Store module |
| Migration 创建第二 Memory/Journal/Checkpoint owner | 未发现；schema mutation 只经 Alembic migration |
| 备份/恢复混淆为 Runtime Recovery | 已禁止；Migration/backup/restore 与 Recovery validation-only 是两个独立概念 |
| Chroma internal SQLite 被修改 | 已禁止；LocalAgent 只经公开 metadata API 维护 marker，绝不 UPDATE Chroma internal tables |
| 未执行 Scope 正常 close 遗留 Registry handle | 审计中真实发现并修复；`_finish_close()` 按 handle identity 注销 |
| 同一共享资源由不同名字关闭两次 | identity 去重；契约测试覆盖 alias close once |
| Trace fingerprint 冒充 Plan/Journal/Snapshot digest | 已禁止；`TraceContractFingerprinter` 只消费 code-owned 语义描述，不读取 Journal/Snapshot/Plan 实例值 |
| Export contract 拥有 Run/Event/Journal 事实 | 已禁止；export 只投影已完成 Span 的安全事实，不写回、不创建 sequence、不持久化 |
| TraceExportDispatcher 拥有 export contract/fingerprint 语义 | 已禁止；dispatcher 只调用 `project_span()`/`TraceCompatibilityEvaluator`，语义仍归 WP4-A Owner |
| Exporter adapter 接收 raw `SpanRecord`/mapping | 已禁止；adapter 只接受 `TraceExportEnvelope`，raw Span 永不出 dispatcher |
| Recorder 拥有 transport/queue/worker | 已禁止；recorder 只调用单个 completion observer，不 import transport、不拥有 queue/worker |
| 第二 shutdown Owner 或新 ShutdownReport 字段 | 已禁止；GracefulShutdownCoordinator 仍是 orchestration Owner，exporter 结果走既有 `RuntimeComponentResult` |
| Exporter 修改 Run/AgentState/OutputGate/Journal/Snapshot/Recovery | 已禁止；exporter 是 side-channel observability，不写回任何 Runtime Authority |
| Trace export metrics 第二计数状态机 | 已禁止；dispatcher health counters 是权威内部事实，metrics 只是 best-effort projection，reason/stage 词表单 owner（dispatcher） |
| HTTP Principal / JWT verification | `core/auth.py::AuthService` | PostgreSQL users + server-verified EdDSA JWT | route 不解析 JWT、不信任 caller identity |
| Identity schema | Alembic `0002_wp2_identity` + `core/persistence/models.py` | PostgreSQL `users` / `roles` / `user_roles` | startup 只读 readiness |
| Durable Evaluation Job / Result | `EvaluationJobService`（transaction owner） | HTTP Job API、未来 WP5 Worker | PostgreSQL `evaluation_jobs` / `evaluation_results`；条件状态迁移；Result 与 `RUNNING→SUCCEEDED` 同事务 | Repository commit/rollback；Redis/RunRegistry 代替 Job authority |
| Transactional Outbox / claim lease | `EvaluationJobService`（write intent）+ `OutboxPublisherService`（delivery） | 独立 publisher process | Job + Outbox 同一 PostgreSQL transaction；`FOR UPDATE SKIP LOCKED` + DB-time lease + fresh claim token；sink 在 transaction 外 | Publisher 改 Job 状态；Recording sink 在非 TEST 使用；exactly-once 宣称 |
| Kafka transport / worker delivery | `KafkaEventSink`（producer ACK）+ `KafkaEvaluationWorker`（consumer） | 独立 publisher/worker process | `confluent-kafka`、`enable.auto.commit=false`、PG commit 后同步 offset；PG `consumer_processed_events` 是 dedup evidence | Kafka offset 成为业务 authority；DLQ publish 未 ACK 时提交原 offset；任意第二 evaluator |

## Stage6-WP6 Observability Owners

| Fact | Authority / Scope | Readers | Construction / mutation | Persistence | Forbidden behavior | Contract |
| --- | --- | --- | --- | --- | --- | --- |
| Prometheus Registry / metric handles | `ObservabilityService`（PROCESS_SCOPE） | API `/metrics`、publisher/worker metrics server | 每进程构造一次 private Registry；业务边界只投影 bounded outcome | 进程内，重启可丢失 | metric state 决定 Job/cache/offset/recovery；global Registry；高基数 label | INTERNAL_RC |
| OTel provider/export lifecycle | `ObservabilityService`（PROCESS_SCOPE） | OTLP exporter、tests | application/进程 composition 构造；bounded batch/flush/shutdown | exporter 外部系统可选 | exporter failure 传播业务；每请求/provider 重建；替代 Runtime semantic trace | INTERNAL_RC |
| Durable distributed trace metadata | `EvaluationJobService` 写入 + Outbox PostgreSQL row 保存 | Publisher、Kafka header injector、Worker extractor | 只允许 nullable `traceparent/tracestate`；与 Job+Outbox 同事务 | PostgreSQL `outbox_events` | baggage/JWT/Principal/query/payload；成为 delivery/consumer authority | INTERNAL_RC |
| Dependency readiness snapshot | `ComponentReadinessService`（只读 projection） | `/readyz`、healthcheck CLI、orchestrator | bounded PG SELECT 1 / Redis PING / Kafka full metadata | 无 | 写数据、auto-create topic、改变 lifecycle/admission/Outbox/offset | INTERNAL_RC |

## Durable Agent Loop v1 Owners

| 事实 | Owner | 持久位置与边界 |
| --- | --- | --- |
| 单 Step 多轮推进与冻结上下文 | `AgentLoopDriver` / `LoopContextInputV1` | `core/runtime/agent_loop/`；不创建新 Scheduler，不改 Memory Authority |
| Frame、接受决策、Observation、operation 预算 | `DurableExecutionRepository` | 原 execution aggregate；control → root → frame → iteration → model/tool 锁序，lease/fence/generation/CAS 保护 |
| 模型 attempts、retry/fallback 与计量 | `ModelInvocationRouter` + `BudgetLedger` | 模型行绑定同一 iteration，真实 attempt 在 I/O 前持久预留，结果与结算同事务 |
| Tool、审批、幂等与 UNKNOWN | 原 ToolExecutionService / durable Tool 与 Approval Owner | action 仅绑定唯一 invocation；Loop read-only 结果也持久化，不复制 Tool 状态机 |
| Final 输出事实 | 原 OutputGate / EventChannel | Frame receipt + Journal + ClientEventFeed 原子提交，sequence Owner 不变 |
| Step 完成与 Run terminal | 原 completion/coordinator Owner | Loop Suspension 不提交 StepResult 或 terminal |
| Loop Evidence | `core/runtime/agent_loop/evidence.py` 的安全投影 | terminal 后允许无 lease 的 seal-only 事务；不可反向启动执行或补造历史 |

### Context Snapshot Owner

- Context/Compaction：纯来源构造、严格摘要验证和有界准备；没有工具、Run terminal、预算结算或 Snapshot 接受权限。
- DurableExecutionRepository：持有 Snapshot/Operation、当前版本引用、Iteration 绑定及 CAS/Fencing 的唯一接受权限；模型开始/完成仍复用原预算事务。
- ModelInvocationRouter：实际请求 assessment、purpose 分组、有限 Retry/Fallback、计费及已完成响应恢复。
- 原 Coordinator：接收 typed stop 并提交原 Run terminal；`context_evidence.py` 只投影事实，seal 不改变执行结果。

### Durable Batch / Handoff

同 Step 内 v2 Frame/Action/Delegation 的接受仍归 DurableExecutionRepository；批次与 Child Driver 仅推进。ToolExecutionService 继续拥有所有工具状态与 I/O，BudgetLedger/root budget_operations 继续拥有额度及结算；仅 ROOT 调用原 OutputGate。内部 ChildResult 不是 Run terminal。详见 [v2 合同](agent_execution_v2.md)。
