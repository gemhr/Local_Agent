# 阶段二第 25 天第一轮：Runtime Contract Freeze

## 1. 本轮目标

本轮完成 Runtime Architecture Audit 与 Contract Freeze，不新增 Runtime、Tool、Recovery/Replay 或生产 Fault 能力。审计覆盖 Composition Root、scope、唯一 owner、合同分类、schema/digest、Legacy 边界、Fault 生产隔离、报告权威性、能力与兼容边界，并只修复审计确认的最小问题。

正式矩阵位置：

- `docs/runtime/runtime_architecture_v1.md`：Composition Root、Scope Matrix、合同分类、Schema Matrix、Legacy/Fault/Authority/Compatibility 边界；
- `docs/runtime/runtime_owner_matrix.md`：唯一事实 Owner Matrix；
- `docs/runtime/runtime_capability_matrix.md`：Capability Matrix；
- 本文：第一轮变更、Bad Case、验证与交接。

`docs/learning/stage2/result/day25_stage2_final_acceptance_result.md` 按要求留到第 25 天第四轮，本轮没有创建。

## 2. 修改前 Composition Root

修改前唯一生产装配根已经是 `server.py::lifespan()`：Settings、Memory、Model engines、AgentRouter、Tool Registry、Model/Tool/Retrieval services、Journal、可选 Snapshot/Recovery、Observability、Trace、RunRegistry、ApplicationRuntimeServices、CoordinatedRuntimeFactory、ChatService 与 GracefulShutdownCoordinator 均在此装配。

审计确认：

1. 没有第二套生产 Coordinated Composition Root；
2. `ChatService` 缺少 factory 时返回固定 `RUNTIME_CONFIGURATION_ERROR`，不手工装配；
3. ApplicationRuntimeServices 每 lifespan 创建一次，不保存 RunContext、AgentState、EventChannel 或 Fault controller；
4. Coordinated factory 每请求新建 Scope；Legacy ChatService 每请求新建 Context/State/Ledger；
5. Shutdown 只由 GracefulShutdownCoordinator 编排；
6. `server.py` 的两个 module-level service 变量是 lifespan 兼容句柄，未缓存 Run/operation 数据；`app.state` 同步保存 application lifecycle 引用。

## 3. Runtime Mode Selection

| 项目 | 冻结结论 |
|---|---|
| Runtime selection owner | ChatRuntimeSelector；请求入口局部 `mode` 是不可变快照 |
| Default runtime | COORDINATED |
| Legacy activation | 仅 `CHAT_RUNTIME_MODE=LEGACY` 显式启用 |
| Selection count | 每 `/api/chat` 请求一次 |
| Cross-runtime fallback | 无；Coordinated/Legacy 失败均不调用另一条路径 |
| Model fallback | 保留在所选 Runtime 内，由 ModelInvocationRouter/Policy 拥有 |
| Unknown mode | Settings load fail closed |

生产 `/api/chat` 在任何 Run/stream 对象创建前选择 mode，两个分支互斥。异常处理只产生固定安全 transport error，不重新执行请求。

## 4. Scope Matrix

完整 Scope Matrix 已冻结在 `docs/runtime/runtime_architecture_v1.md`。

摘要：

- APPLICATION_SCOPE：Settings、ApplicationRuntimeServices、Model router/adapter、Tool/Retrieval services、Journal、SnapshotStore、Observability、SpanRecorder、RunRegistry、GracefulShutdownCoordinator；
- RUN_SCOPE：RunContext、CancellationSource、AgentState、BudgetLedger、EventChannel、Emitter、CoordinatedRunScope、ActiveRunControlHandle；
- OPERATION_SCOPE：Recovery validation、Shutdown operation、显式 Fault controller；
- INVOCATION_SCOPE：Model/Tool invocation、Retrieval execution/stages、相关 worker handle；
- ATTEMPT_SCOPE：Model/Tool attempt、ToolResourceLease、AttemptSideEffectTracker；
- COMPONENT_SCOPE：adapter/recorder/executor 与其进程内 lifecycle；
- TEST_SCOPE：FaultInjectionScope、test controller/recorder/blocker/mutator、ToolCompletionGapFixture、corruption fixtures、test fakes。

审计真实发现：未执行的 CoordinatedRunScope 调用普通 `close()` 时原先只关闭 Channel/Gauge，不注销已经注册的 Run handle。已在 Scope 唯一 cleanup 边界按 handle identity 注销，避免 close 与 coordinator/shutdown 双写。

## 5. Owner Matrix

完整 Owner Matrix 已冻结在 `docs/runtime/runtime_owner_matrix.md`，共覆盖 runtime/identity/state/plan/retry/fallback/circuit/budget/model/tool/resource/worker/event/journal/snapshot/recovery/health/shutdown/fault 等事实。

关键唯一 owner：

| 事实 | Owner |
|---|---|
| State / step / stop reason | AgentState，经 AgentStateMachine 写入 |
| Retry | RetryExecutor + RetryPolicy |
| Model fallback | ModelInvocationRouter + ModelRoutingPolicy |
| Side-effect | AttemptSideEffectTracker；完成后冻结 evidence |
| Event sequence | RuntimeEventChannel |
| Terminal event | RunCoordinator |
| Journal append | RuntimeEventChannel 调用 RunEventJournal |
| Snapshot capture | CheckpointCoordinator / RunSnapshot.create |
| Snapshot digest | RunSnapshot.digest_source + snapshot canonicalizer |
| Recovery | RecoveryValidator，只读派生 assessment |
| Shutdown | GracefulShutdownCoordinator |
| Fault match/hit count | 显式 test/operation-scoped FaultInjectionController |

未发现 Plan/AgentState 双写、Fault controller 决定 retry、RecoveryValidator 写 AgentState、Observability/Trace 写 Journal、ShutdownReport 写 lifecycle 等重复 owner。

## 6. Contract Classification

分类词冻结为：`PUBLIC_STABLE`、`PUBLIC_VERSIONED`、`INTERNAL_STABLE`、`INTERNAL_EVOLVING`、`TEST_ONLY`、`DEPRECATED`。

- PUBLIC_STABLE：RunContext、Plan/PlanStep、ToolInvocation、ToolExecutionResult/Error、RetrievalExecutionResult、ModelInvocationResult；
- PUBLIC_VERSIONED：AgentState、RuntimeEvent、JournalRecord、RunSnapshot、Snapshot 内 SafeBudgetSnapshot、ToolCompletedPayload 的 evidence 子合同；
- INTERNAL_STABLE：运行 BudgetSnapshot、RecoveryAssessment、ShutdownReport、Event/Snapshot publication evidence；
- INTERNAL_EVOLVING：RuntimeEventDraft 及 operation/attempt 临时上下文；不得直接进入 Wire；
- TEST_ONLY：FaultPlan/Rule/Decision、Fault reports、ToolCompletionGapFixture、test fake/mutator/oracle；
- DEPRECATED：`ShutdownReport.completed` 是兼容别名；能力级没有新增 deprecated 能力。

审计真实发现：ToolCompletionGapFixture 曾位于 `core.runtime` 并由 package `__all__` 公开。它仅被测试使用，已移动到 `tests/_tool_completion_gap_fixtures.py`，生产 RecoveryValidator 的输入仍严格为 RunSnapshot + Plan + Journal。

## 7. Schema / Version / Digest Matrix

完整矩阵见 `docs/runtime/runtime_architecture_v1.md`。

| Contract | Reader | Writer | Digest owner | Unknown high version |
|---|---|---|---|---|
| RuntimeEvent | v1/v2 | v2 | Journal 对持久投影计算 | fail closed |
| JournalRecord | v1/v2 | v2 | event_journal canonical JSON SHA-256 | fail closed |
| RunSnapshot | v1 | v1 | snapshot_serialization + RunSnapshot | fail closed |
| Tool evidence | missing legacy / v1 | v1 | tool canonical JSON result digest | fail closed；missing 保持 Unknown |
| Recovery evidence | Event v1/v2 + Snapshot v1 | 无独立 writer | 复用上游 digest | 上游 fail closed |
| FaultPlan | v1 | v1，TEST_ONLY | FaultPlan semantic canonical JSON | fail closed |
| ShutdownReport | 未版本化、不持久化 | 进程内 | 无 | 不适用 |

审计真实发现：FaultPlan 原先只要求 schema_version 为正整数，因此会接受 999。已强制仅接受 `FAULT_PLAN_SCHEMA_VERSION == 1`。本轮没有虚构 Snapshot v0、Recovery evidence schema 或 ShutdownReport schema/digest version。

## 8. Legacy / Coordinated Boundary

| 能力 | Coordinated | Legacy |
|---|---|---|
| 默认入口 | 是 | 否 |
| 显式配置 | COORDINATED | LEGACY |
| RunContext / AgentState | 完整接入 | 基础接入 |
| Event Journal / Snapshot / Recovery | Journal；Snapshot/validation opt-in | 未接入完整能力 |
| Fault Injection | 仅测试 seam | 生产未启用 |
| Graceful Shutdown / Worker | 覆盖 | 覆盖 dedicated legacy worker |
| Tool Contract | 强类型 Runtime contract | 通过既有 router/adapter，不能夸大等价性 |
| Streaming | RuntimeEvent -> compatibility adapter | 同步文本 generator bridge |

Coordinated 不把 Legacy 当内部 fallback；Legacy 也不创建 Coordinated Scope。共享 model/tool/retrieval/recorder 等 application resource 由 ApplicationRuntimeServices 关闭一次。

## 9. Fault Injection Production Isolation

生产默认严格为 `fault_controller=None`：

- Settings 无 fault/chaos 字段；
- 环境变量不能创建 FaultPlan；
- HTTP header 与 ChatRequest body 无 fault 字段；
- Prompt/message、file path、Tool arguments 不参与 fault selection；
- 无 module-global controller；
- 无 current fault ContextVar；
- ApplicationRuntimeServices 不保存 controller；
- 生产 ChatService 调用 factory 时不传 controller；
- lifespan shutdown 不传 controller；
- Journal/Snapshot/Wire 不保存 FaultRule 或 rule id。

Fault Plan/Controller/Scope/Recorder/Report 是 TEST_ONLY/显式 operation seam。它们可以从 Runtime 子模块被测试导入，但不能由生产请求或配置创建。

## 10. Authority / Evidence / Report / Fixture

- Authority：AgentState、JournalRecord、RunSnapshot、RunRegistry/worker owner snapshot；
- Frozen Evidence：ToolCompletedPayload、EventPublicationEvidence、SnapshotPublicationEvidence；
- Derived Report：ShutdownReport、FaultCoverageReport、FaultRuntimeInvariantReport、Observability/Trace health；
- Test Oracle / Fixture：ToolCompletionGapFixture、corruption fixture、PhaseAwareToolAdapter counters。

报告是 immutable value，不持有 RunContext、AgentState、EventChannel、RunRegistry 或 Application services，也不通过错误字符串推断 owner state。Frozen Evidence 只保留 allowlist 安全字段/digest；Test Oracle 不进入 Production Validator。

## 11. Capability Matrix

完整矩阵见 `docs/runtime/runtime_capability_matrix.md`。

- SUPPORTED：Coordinated、显式 Legacy、Parallel engine、Budget、Retry、Model fallback、Circuit breaker、Tool idempotency/evidence/lease、Retrieval runtime、Event streaming、Journal-first、Observability、Trace、Disconnect、Worker tracking、Graceful shutdown；
- PARTIALLY_SUPPORTED：Snapshot、Recovery validation；
- CONTRACT_ONLY：Fault Injection test seam；
- NOT_IMPLEMENTED：Recovery execution、Replay、Random Chaos、Cross-process Registry、Exactly-once、Automatic compensation、Step result rehydration；
- LEGACY_ONLY：无；
- DEPRECATED：能力级无。

Recovery validation 没有被写成 Automatic recovery；Fault Injection test seam 没有被写成 Production chaos platform。

## 12. Deprecated / Compatibility Matrix

完整表见 `docs/runtime/runtime_architecture_v1.md`。本轮审计并保留：

- `ShutdownReport.completed`：等于 orchestration completion，不等于 fully closed；
- 旧 Event evidence 缺失字段：保持 Unknown，不回填；
- Legacy Runtime mode：显式兼容，非默认/非 fallback；
- 旧 Tool adapter method signature 与 Model generator：保留现有 adapter compatibility；
- Event v1/v2 reader 与 v2 writer；
- Snapshot v1 是唯一真实版本；不存在 v0；
- 当前 Fault Trigger 全为确定性 trigger，没有虚构“已删除 trigger”兼容；
- publication error 使用冻结 `evidence`，不持有 RuntimeEvent；
- 仍有调用方的 constructor positional arguments 未删除。

## 13. 真实代码修复

本轮只有三项最小修复：

1. **错误 Scope / Registry 泄漏**：`CoordinatedRunScope._finish_close()` 现在仅在 Registry 中仍是自身 handle 时注销；修复 unexecuted normal close，保持 coordinator/force-abort identity 幂等。
2. **未知持久/版本合同未 fail closed**：FaultPlan 只接受 schema v1，拒绝未知高版本。
3. **Test Fixture 进入 production import path**：ToolCompletionGapFixture 从 `core/runtime` 移至 `tests/_tool_completion_gap_fixtures.py`，移除 `core.runtime` public export。

未修改 Model/Tool/Retrieval 业务语义、Retry/Fallback/Compensation 策略、Legacy 路径、Event/Journal/Snapshot 技术或目录架构。

## 14. 安全边界

Runtime Contract repr、三份 Runtime 文档、本文、Report/Evidence、Wire、structured logs、metric labels 与 span attributes 只允许安全字段、固定 error code、计数和 digest。不得输出 Prompt、Model/Tool/RAG/Memory 正文、原始 idempotency/resource key、原始 snapshot payload、Provider secret error、私有用户路径、Provider URL 或密钥。

安全扫描覆盖任务定义的全部禁止 marker。测试源码会使用合成 marker 构造负向安全用例，但这些值不得进入 Runtime 输出或正式文档；扫描结果按“命中位置与语义”区分测试输入与真实泄漏。

## 15. Bad Case

### Bad Case 1：Application service 缓存 Run Controller

- 真实性类型：风险审计，当前未发现。
- 触发条件：把 current scope、AgentState、EventChannel 或 Fault controller 存入 ApplicationRuntimeServices/Factory。
- 故障表现：并发请求串扰、错误取消、跨 Run counter 污染、shutdown 关闭错误对象。
- 根因：混淆 APPLICATION_SCOPE 与 RUN/OPERATION_SCOPE。
- 修复：容器字段禁止 per-run 类型，factory 不缓存返回 scope；controller 只作显式参数。
- 回归：`test_runtime_contract_freeze.py`、`test_runtime_scope_matrix.py`、既有 application service isolation tests。
- 知识点：Dependency Injection、scope safety、request isolation。
- 面试表达：Application container 只能保存可共享依赖，current request 必须由局部 Scope 强持有。
- 当前状态：已由字段、slots、并发和 factory fresh-instance 测试防止。

### Bad Case 2：Plan 与 AgentState 双写 Step status

- 真实性类型：风险审计，当前未发现。
- 触发条件：给 PlanStep 增加 status/attempt/error 并与 AgentState 同时更新。
- 故障表现：Scheduler、Snapshot 与 UI 读取到冲突状态。
- 根因：把静态计划定义当运行状态容器。
- 修复：Plan/PlanStep immutable 且不含 runtime 字段；AgentState/StepState 唯一拥有状态。
- 回归：`test_runtime_contract_freeze.py`。
- 知识点：Single Source of Truth、CQRS 式定义/状态分离。
- 面试表达：Plan 回答“做什么”，AgentState 回答“执行到哪里”，两者不能双写同一事实。
- 当前状态：合同冻结。

### Bad Case 3：Fault Controller 成为 Retry owner

- 真实性类型：风险审计，当前未发现。
- 触发条件：Controller 根据 hit count 直接选择下一 attempt 或 candidate。
- 故障表现：测试开关改变 Retry/Fallback 业务语义，disabled parity 失效。
- 根因：把 fault injection 执行器提升为业务策略 owner。
- 修复：Controller 只返回/抛出注入结果；RetryExecutor/Policy 继续决定 retry。
- 回归：Fault disabled parity、tool/model fault retry tests、Owner Matrix tests。
- 知识点：test seam、policy ownership、behavioral parity。
- 面试表达：故障注入只能改变一次调用的结果，不能接管生产重试策略。
- 当前状态：已隔离。

### Bad Case 4：RecoveryValidator 修改 AgentState

- 真实性类型：风险审计，当前未发现。
- 触发条件：Validator 在 assessment 过程中调用 state transition 或启动 adapter。
- 故障表现：一次“检查”改变 live run，可能重复 Model/Tool 副作用。
- 根因：把 validation 与 recovery execution 合并。
- 修复：输入严格为 Snapshot/Plan/Journal；输出 immutable RecoveryAssessment；所有 replay flag 为 false。
- 回归：`test_runtime_contract_freeze.py`、`test_runtime_capability_matrix.py`、Recovery contract tests。
- 知识点：Command/query separation、safe recovery boundary。
- 面试表达：RecoveryValidator 是只读判定器，不是恢复执行器。
- 当前状态：validation-only，execution 未实现。

### Bad Case 5：Report 被当作 Authority

- 真实性类型：风险审计，当前未发现。
- 触发条件：用 ShutdownReport/Fault report 的字段反向修改 lifecycle、Registry 或 AgentState。
- 故障表现：报告生成时序改变真实状态，重复 owner。
- 根因：混淆 derived projection 与 authoritative state。
- 修复：Report 为 frozen value，只含安全计数/结果；不持有 live owner。
- 回归：`test_runtime_report_authority.py`、fault security tests。
- 知识点：materialized view、authority boundary。
- 面试表达：报告可以晚到或不完整，因此只能描述 owner，不能取代 owner。
- 当前状态：已冻结为 Derived Report。

### Bad Case 6：Test Fixture 进入生产 Recovery 输入

- 真实性类型：真实发现（公开路径），未发现真实生产调用。
- 触发条件：ToolCompletionGapFixture 位于 `core.runtime` public export，调用者误传给 Validator。
- 故障表现：测试 oracle 可能绕过持久化事实，虚构可恢复性。
- 根因：测试 fixture 放入 production package 并命名为可复用 contract。
- 修复：移动至 `tests/_tool_completion_gap_fixtures.py`，移除 core public export；Validator 仍只接受 RunSnapshot。
- 回归：`test_runtime_contract_freeze.py`、`test_runtime_report_authority.py`、原 gap tests。
- 知识点：test double isolation、historical evidence authority。
- 面试表达：测试 oracle 可以表达预期，但生产恢复只能相信版本化 Snapshot 与 Journal。
- 当前状态：已修复。

### Bad Case 7：Coordinated 失败自动切 Legacy

- 真实性类型：风险审计，当前未发现。
- 触发条件：捕获 Coordinated 异常后调用 `stream_chat()` 重跑。
- 故障表现：同一请求两套 identity/state，Model/Tool side effect 可能重复。
- 根因：把跨 Runtime 重跑误认为 fallback。
- 修复：请求入口只选择一次，两个分支互斥；失败就地安全收口。
- 回归：`test_runtime_legacy_boundary.py`、`test_default_runtime_entry.py`。
- 知识点：failure domain、exactly-once boundary、TOCTOU。
- 面试表达：Runtime 选择必须在副作用发生前冻结，失败后不能换执行器重跑。
- 当前状态：已由真实 endpoint 负向测试防止。

### Bad Case 8：Legacy 被文档夸大为完整 Runtime

- 真实性类型：文档风险，当前矩阵已纠正。
- 触发条件：看到 Legacy 使用 RunContext/AgentState 就宣称拥有 Journal/Snapshot/Recovery/Fault 全能力。
- 故障表现：发布与回滚决策基于不存在的保障。
- 根因：把共享基础类型等同于端到端 capability。
- 修复：Legacy/Coordinated matrix 逐项记录真实接入，Legacy 仅为显式兼容路径。
- 回归：Capability/Legacy 文档审计 + endpoint/code tests。
- 知识点：capability evidence、partial integration。
- 面试表达：类型存在不代表路径已接线，能力必须由默认调用链和失败行为证明。
- 当前状态：边界已冻结。

### Bad Case 9：未知 Schema 按当前版本解析

- 真实性类型：真实发现（FaultPlan）。
- 触发条件：FaultPlan schema_version 只校验正整数，999 被当作当前结构使用。
- 故障表现：未知语义参与 digest/match，产生错误测试证据。
- 根因：版本字段存在但缺少 supported-version gate。
- 修复：FaultPlan 仅接受 v1；Event/Journal/Snapshot 继续按各自 reader 集合 fail closed。
- 回归：`test_runtime_schema_matrix.py`。
- 知识点：forward incompatibility、fail closed、schema gate。
- 面试表达：未知高版本不能“尽力解析”，否则字段同名也可能语义不同。
- 当前状态：已修复。

### Bad Case 10：当前 Registry 回填历史 Evidence

- 真实性类型：风险审计，当前未发现。
- 触发条件：Journal 缺字段时查询当前 adapter/registry/worker 推断历史完成状态。
- 故障表现：重启后同一历史记录得到不同 assessment，可能错误 replay。
- 根因：混淆 live state 与 persisted evidence 的时间边界。
- 修复：缺失字段保持 Unknown/INSUFFICIENT_EVIDENCE；RecoveryValidator 不依赖 RunRegistry。
- 回归：Recovery version compatibility/tool completion gap tests。
- 知识点：temporal consistency、evidence provenance。
- 面试表达：历史恢复必须只使用当时冻结的证据，不能拿现在的 Registry 补过去。
- 当前状态：已防止。

### Bad Case 11：`ShutdownReport.completed` 被理解为 fully closed

- 真实性类型：已知兼容语义风险。
- 触发条件：调用者只检查 `completed`，忽略 active/detached/unknown worker 或 deferred model close。
- 故障表现：编排已结束但资源仍未完全关闭，被误报为成功。
- 根因：旧字段名过于宽泛。
- 修复：保留 alias 兼容；明确 replacement 为 `orchestration_completed` / `fully_closed`。
- 回归：`test_runtime_report_authority.py`、`test_shutdown_report_truthfulness.py`。
- 知识点：truthful reporting、compatibility semantics。
- 面试表达：shutdown 完成编排不等于所有物理资源已终止，尤其 detached thread 只能保守报告。
- 当前状态：兼容保留并已文档化，未删除字段。

### Bad Case 12：Production Settings 可以启用 Fault

- 真实性类型：风险审计，当前未发现。
- 触发条件：新增 env/header/body/prompt/tool arg 开关创建 FaultPlan。
- 故障表现：外部请求可延迟、阻塞、注入错误或变更 fixture。
- 根因：把测试 seam 暴露成生产控制面。
- 修复：生产无入口，默认参数为 None，只有测试显式构造 scope/controller。
- 回归：`test_runtime_fault_production_isolation.py`、Fault security tests。
- 知识点：attack surface、configuration isolation、secure default。
- 面试表达：测试故障注入可以编译进代码，但绝不能从生产配置或请求激活。
- 当前状态：生产隔离通过。

### Bad Case 13：共享 resource 被不同名称关闭两次

- 真实性类型：已存在防护；本轮补充契约回归。
- 触发条件：同一 engine/executor/store 以两个 component name 进入 close targets。
- 故障表现：第二次 close 抛错、破坏仍在使用的共享资源或误报 shutdown failure。
- 根因：按名称而不是对象 identity 管理生命周期。
- 修复：ApplicationRuntimeServices `_targets()` 与 close ledger 按 identity 去重。
- 回归：`test_runtime_owner_matrix.py`、shutdown identity reservation tests。
- 知识点：resource identity、idempotent close、aliasing。
- 面试表达：组件名是诊断标签，物理关闭所有权必须绑定对象 identity。
- 当前状态：alias close once 已验证。

### Bad Case 14：枚举存在被写成 capability supported

- 真实性类型：文档风险，当前矩阵已纠正。
- 触发条件：因为存在 RecoveryStatus/FaultPoint/compensation 字段就标记自动恢复、生产 chaos 或自动补偿为 SUPPORTED。
- 故障表现：能力清单夸大，Release/运维错误依赖未实现行为。
- 根因：把 contract vocabulary 当作 executable path。
- 修复：Capability 必须同时记录 default path、owner、persistence、failure/recovery behavior 与 tests。
- 回归：`test_runtime_capability_matrix.py` + capability 文档审查。
- 知识点：contract-only vs implementation、evidence-based status。
- 面试表达：枚举只是可表达性，能力还需要入口、owner、状态与失败收口。
- 当前状态：未实现项明确标为 NOT_IMPLEMENTED。

### Bad Case 15：虚构 Snapshot v0 兼容版本

- 真实性类型：风险审计，当前未发现。
- 触发条件：为填矩阵把“缺 schema 字段”命名为 v0 并按 v1 默认解析。
- 故障表现：损坏/未知 payload 被错误接受，digest 语义不可靠。
- 根因：用文档整齐性替代真实历史版本证据。
- 修复：只记录真实 Snapshot v1；字段集合严格；缺失/未知版本 fail closed。
- 回归：`test_runtime_schema_matrix.py`、snapshot/recovery version tests。
- 知识点：schema provenance、no invented history。
- 面试表达：兼容版本必须有真实 writer/reader 和 fixture，不能凭空命名。
- 当前状态：不存在 v0。

### Bad Case 16：内部 evolving 类型直接进入 Wire

- 真实性类型：风险审计，当前未发现。
- 触发条件：对 RuntimeEventDraft、attempt context 或内部 dataclass 直接 `asdict/json.dumps`。
- 故障表现：未分配 identity/sequence、原始 payload、路径或高基数内部字段泄漏到客户端。
- 根因：绕过 RuntimeEvent/Journal/stream adapter 的安全投影。
- 修复：Wire 只由 RuntimeEvent + 固定 allowlist adapter 产生；Draft 为 INTERNAL_EVOLVING。
- 回归：stream adapter、event security、structured logging tests。
- 知识点：anti-corruption layer、安全序列化、wire contract。
- 面试表达：内部类型可以快速演进，跨边界前必须投影到版本化且最小化的 DTO。
- 当前状态：边界已冻结。

## 16. 测试结果

新增契约测试：

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

当前已完成：

```text
目标契约 pytest：24 passed
修改后受影响测试：45 passed
关键回归：49 passed
全仓 pytest：1037 passed, 42 subtests passed
compileall：通过
uv lock --check：通过（首次受沙箱权限限制，授权读取 uv 管理目录后通过）
git diff --check：通过（仅 LF/CRLF 工作区提示）
安全禁止 marker 扫描：Runtime/正式文档无命中
```

## 17. 未完成事项

本轮有意未实现：

- automatic recovery / recovery execution；
- model/tool/retrieval replay；
- step result/output rehydration；
- production Fault API / random chaos；
- cross-process Registry / distributed circuit/lease；
- system-wide exactly-once；
- automatic compensation；
- Snapshot 默认启用；
- 标准 SSE/WebSocket 协议迁移；
- 删除 Legacy 或兼容字段。

这些不是第一轮缺陷，而是 Capability Matrix 中明确的限制或后续决策项。

## 18. 第二轮 Release Candidate 接入点

第二轮应以三份冻结矩阵和本轮契约测试为 gate：

1. Release Candidate 必须继续由 `server.py::lifespan()` 单一装配；
2. `/api/chat` mode capture count 保持 1，default COORDINATED，无跨 Runtime fallback；
3. 新的 application dependency 必须进入 ApplicationRuntimeServices 的 identity-dedup close；
4. 新的 Run/operation object 不得进入 Application fields；
5. 新的持久 contract 必须先定义真实 schema/reader/writer/digest owner；
6. Recovery 仍只读，除非后续轮次单独批准 execution 设计；
7. Fault seam 继续没有生产 enablement；
8. Capability status 只能在真实默认路径、owner、failure/recovery tests 均存在时升级。

## 19. 需要带回 ChatGPT 审查的信息

```text
Runtime selection owner：ChatRuntimeSelector + request-local snapshot
Default runtime：COORDINATED
Legacy activation：仅 CHAT_RUNTIME_MODE=LEGACY
Cross-runtime fallback：无
Composition root：server.py FastAPI lifespan
Application scope：Settings、ApplicationRuntimeServices、shared services/stores/recorders/registry/shutdown coordinator
Run scope：RunContext、CancellationSource、AgentState、BudgetLedger、Channel、Emitter、Scope、Run handle
Operation scope：Recovery validation、Shutdown、显式 test Fault controller
Invocation/attempt scope：Model/Tool/Retrieval invocation；Model/Tool attempt、lease、worker handle
Test-only scope：Fault scope/controller/recorder/report、gap/corruption fixtures、fakes/mutators
State owner：AgentState through AgentStateMachine
Retry owner：RetryExecutor + RetryPolicy
Fallback owner：ModelInvocationRouter + ModelRoutingPolicy
Side-effect owner：AttemptSideEffectTracker / Tool attempt flow
Sequence owner：RuntimeEventChannel
Terminal owner：RunCoordinator
Snapshot owner：CheckpointCoordinator / RunSnapshot
Recovery owner：RecoveryValidator（只读 assessment）
Shutdown owner：GracefulShutdownCoordinator
Fault counter owner：test/operation-scoped FaultInjectionController
Public stable contracts：RunContext、Plan/PlanStep、Tool/Model/Retrieval execution contracts
Versioned contracts：AgentState、RuntimeEvent、JournalRecord、RunSnapshot、tool evidence
Internal evolving contracts：RuntimeEventDraft、operation/attempt contexts
Test-only contracts：Fault plan/decision/reports、test oracle/fixture/mutator
Schema matrix：Event 1/2->2；Journal 1/2->2；Snapshot 1->1；FaultPlan 1->1；Shutdown unversioned
Digest owners：Journal canonicalizer；Snapshot canonicalizer/RunSnapshot；Tool/Fault semantic canonicalizers
Legacy boundary：显式兼容路径，不拥有完整 Coordinated 能力
Fault production enablement：无，默认 controller=None
Authority objects：AgentState、JournalRecord、RunSnapshot、Registry/worker snapshots
Frozen evidence：ToolCompletedPayload、EventPublicationEvidence、SnapshotPublicationEvidence
Derived reports：Shutdown/Fault/Observability/Trace reports
Test fixtures：仅 tests package
Supported capabilities：见 runtime_capability_matrix.md
Partially supported：Snapshot、Recovery validation
Contract-only：Fault Injection test seam
Not implemented：Recovery execution、Replay、Random Chaos、cross-process Registry、Exactly-once、automatic compensation、step result rehydration
Deprecated items：ShutdownReport.completed；其余兼容项见 architecture matrix
真实代码修复：3 项（Scope unregister、FaultPlan version gate、test fixture isolation）
新增测试：8 个文件，24 tests
目标 pytest：24 passed
关键回归：49 passed
全仓 pytest：1037 passed, 42 subtests passed
compileall：通过
lock check：通过
diff check：通过（仅 LF/CRLF 工作区提示）
需要人工确认的问题：第二轮是否继续保留 server.py module-level application service 兼容句柄；当前无阻塞项
```
