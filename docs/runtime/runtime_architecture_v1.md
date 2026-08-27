# LocalAgent Runtime Architecture v1

本文冻结阶段二第 25 天第一轮结束时的 Runtime 架构事实。它描述当前实现，不承诺未实现能力，也不替代各类型源码中的校验规则。

## 1. Composition Root

唯一生产 Composition Root 是 `server.py` 的 FastAPI `lifespan()`：

```text
Settings.load()
-> lifespan() 创建 Application 资源
   -> ToolRegistry：construct -> register_all_tools -> freeze -> 注入 AgentRouter
   -> ToolPolicyCatalog：construct -> register_default_tool_policies -> validate -> freeze
   -> ToolGovernanceService：唯一 invocation-time Authority -> 注入 AgentRouter
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
| Tool 身份/描述/枚举/绑定来源 | `ToolRegistry`（APPLICATION_SCOPE；lifespan 内 populate + freeze 后注入 AgentRouter，运行期只读） |
| Tool 静态 policy 来源 | `ToolPolicyCatalog`（APPLICATION_SCOPE；lifespan 内 register_default_tool_policies + validate + freeze；任一校验失败 -> startup fail，never READY） |
| Tool governance authority | `ToolGovernanceService`（唯一 invocation-time Authority；仅解释 Catalog 与 `ToolExecutionSpec`；AgentRouter 只调用它） |
| Tool filesystem resource authority | `ResourceAuthorizationService`（APPLICATION_SCOPE；解释 frozen multiple read roots + frozen extractor catalog；仅覆盖 `list_files` / `analyze_excel`） |
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
| ToolRegistry | APPLICATION_SCOPE | `server.py::lifespan()`（populate + freeze） | ApplicationRuntimeServices（随 application 释放） | 运行期注册/变更；暴露内部 mutable mapping；进入 Run 状态 |
| ToolPolicyCatalog | APPLICATION_SCOPE | `server.py::lifespan()`（register + validate + freeze） | ApplicationRuntimeServices（随 application 释放） | 运行期 register/mutation/hot reload；成为第二 ToolRegistry；保存 execution spec 字段 |
| ToolGovernanceService | APPLICATION_SCOPE | `server.py::lifespan()` | ApplicationRuntimeServices（随 application 释放） | 成为第二 ToolRegistry / Agent capability owner；保存 raw arguments/path |
| ResourceAuthorizationService / FilesystemResourcePolicy / ToolResourceExtractorCatalog | APPLICATION_SCOPE | `server.py::lifespan()`（Registry freeze 后 validate + freeze） | ApplicationRuntimeServices（随 application 释放，无独立 close） | 运行期 mutation/hot reload；成为 Tool Permission 或 Tool execution owner；保存 raw path 到安全投影 |
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

### 2.1 Tool Registry / Descriptor（WP2-A，INTERNAL_RC）

`ToolRegistry` 是进程级（APPLICATION_SCOPE）Tool 身份、描述、枚举与执行绑定的唯一事实源。生命周期固定为静态 startup 注册：

```text
construct -> register（4 个 ToolRegistration）-> freeze -> 注入 AgentRouter -> 运行期只读
```

- `ToolDescriptor`（frozen）只含 `name` + `description`；name 必须匹配 `^[a-z][a-z0-9_]{0,63}$`（case-sensitive）；description 为 trim 后非空、无控制字符的安全字符串。Descriptor 不复制 `ToolExecutionSpec` 任何字段。
- `ToolRegistration`（frozen）只含 `descriptor` + `adapter`；注册时校验 `descriptor.name == adapter.spec.tool_name`（invocation-independent identity，以 `ToolExecutionSpec` 为准）。
- `freeze()` 幂等；freeze 前 read API fail closed（`TOOL_REGISTRY_NOT_FROZEN`）；freeze 后 `register()` fail closed（`TOOL_REGISTRY_FROZEN`）；重复 canonical name fail closed（`TOOL_REGISTRY_DUPLICATE`，保留 original binding，无 last-write-wins）。
- **Tool resolution owner**：`AgentRouter`（untrusted planner 输出经 `resolve()` 拒绝为 no-tool；trusted internal 经 `require()` fail closed）。**Tool execution owner**：`ToolExecutionService`（生产四个 Tool 全部经 `ToolAdapter -> ToolExecutionService`，为 sole production execution owner；不做 name 解析，handler 由调用方传入）。
- `ComplexWorkflowToolAdapter.spec_for(invocation)` 保留按 invocation 动态派生 side-effect/idempotency 执行 truth；Descriptor 不固化动态执行语义。
- `AgentRouter.tools` 降级为只读兼容视图（由 frozen Registry 派生），不再是 mutable 注册事实源；不提供动态注册 / hot reload / `/api/tools` / 跨进程 Registry。
- risk / permission / approval 属 WP2-B，不进 Descriptor。

### 2.2 Tool Governance v1（WP2-B，INTERNAL_RC）

`ToolPolicyCatalog` 是静态 Tool governance policy facts 的唯一事实源（APPLICATION_SCOPE / process-local）。生命周期固定：`construct -> register_default_tool_policies（4 条）-> validate（对 frozen ToolRegistry 与 DEFAULT_AGENT_REGISTRY 只读校验）-> freeze -> 运行期只读`。freeze 幂等；freeze 前 read fail closed（`TOOL_GOVERNANCE_NOT_FROZEN`）；freeze 后 register fail closed（`TOOL_GOVERNANCE_FROZEN`）；重复 Tool policy fail closed（`TOOL_GOVERNANCE_DUPLICATE`）。任一 registered Tool 缺 policy、policy 引用未知 Tool / 未知或未启用 Agent、allowed set 为空、risk fact / approval rule 非法 -> `TOOL_GOVERNANCE_INVALID` 使 startup fail（never READY）。

`ToolGovernanceService` 是唯一 invocation-time Authority，只解释 frozen Catalog 与已解析 `ToolExecutionSpec`，不保存 execution spec 字段（不复制 side-effect / idempotency / timeout / concurrency）。

- **Principal**：`actual executing agent_id`（AgentRouter 执行参数）；`RunContext.entry_agent_id` 不被重解释为 execution principal；无 user/tenant IAM。
- **两级 Gate**（`AgentRouter._prepare_answer_messages`，唯一 production Tool execution seam，覆盖 LEGACY 与 COORDINATED）：
  ```text
  ToolRegistry.require(tool_name)
  -> ToolGovernanceService.authorize_tool(context, registration)   # 静态 Permission：ALLOW / DENY
  -> adapter.build_invocation(tool_args)
  -> adapter.spec_for(invocation)
  -> ToolGovernanceService.evaluate_invocation(context, registration, invocation, execution_spec)  # Risk/Approval
  -> 仅 ALLOW 调用 ToolExecutionService.execute_sync(...)
  ```
  任一阶段非 `ALLOW` 立即以固定 safe denial 终止当前 Tool attempt；两类 non-ALLOW 的 build/spec 事实不同：
  - 静态 Permission non-ALLOW（`authorize_tool` 返回 DENY）：`build_invocation = NOT CALLED`、`spec_for = NOT CALLED`；
  - 动态 governance non-ALLOW（`evaluate_invocation` 返回 `APPROVAL_REQUIRED` / `TOOL_RISK_UNCLASSIFIED`）：发生于 build/spec 之后，`build_invocation = ALREADY CALLED`、`spec_for = ALREADY CALLED`。
  两类均保证：`execute_sync = NOT CALLED`、`invoke_once = NOT CALLED`、`TOOL_STARTED = 0`、`TOOL_COMPLETED = 0`、不重试 planner、不换 Tool、不调用 final-answer model、不跨 Runtime fallback。
- **Permission**：per-Tool explicit allowed Agent IDs（当前 4 Tool × 5 Agent = 20 条显式授权关系，无 implicit default allow）；未知 principal / missing policy / explicit deny 均 fail closed。
- **Risk**：只按 Architecture Decision 冻结的 exact full-combination allowlist 分类。完整 key = `(frozenset(static_risk_facts), side_effect_kind, idempotency)`，只有 5 个唯一组合被批准：`{ARBITRARY_LOCAL_FILESYSTEM_READ}+NONE+READ_ONLY -> MEDIUM`、`{SYSTEM_INFORMATION_READ}+NONE+READ_ONLY -> LOW`、`{}+NONE+READ_ONLY -> LOW`、`{}+LOCAL_STATE_MUTATION+IDEMPOTENT_WITH_KEY -> MEDIUM`、`{}+LOCAL_STATE_MUTATION+NON_IDEMPOTENT -> HIGH`。任何其它完整组合（含 static/dynamic 各自已知但组合未冻结、multiple static facts、未知 dynamic enum）一律 `TOOL_RISK_UNCLASSIFIED` fail closed；不实现通用 risk algebra（不取 max、不按 baseline 推断）。`ToolExecutionSpec` 仍是 side_effect_kind / idempotency 唯一 source of truth。
- **Approval**：effective risk >= policy threshold（HIGH）-> `APPROVAL_REQUIRED`；v1 无 approval evidence capability，因此是 terminal pre-execution safe denial（不是可恢复 PENDING）。无 human approval workflow、无 durable pause/resume、无 approval endpoint。
- **Known Limitation（Observability）**：WP2-B v1 不产生 dedicated governance RuntimeEvent 或 governance Journal fact；`DENY` / `APPROVAL_REQUIRED` 不会伪造 `TOOL_STARTED` / `TOOL_COMPLETED`（Tool 未执行）。rich governance observability 延后（WP4 候选），不为此新增 RuntimeEvent / Journal schema。
- **Boundary**：`ToolExecutionService` 仍是 sole actual execution owner（non-ALLOW 时绝不调用）；`ToolRegistry` 仍只回答“What Tools exist?”；`AgentRegistry` capability 不变成 authorization；governance 不是 filesystem sandbox / path authorization（WP3）。

### 2.3 File Tool Resource Authorization（Stage 3 WP3，INTERNAL_RC）

`ResourceAuthorizationService` 是 application-wide filesystem read 的唯一 Authority。启动装配固定为 `frozen ToolRegistry -> construct/register/validate/freeze ToolResourceExtractorCatalog -> FilesystemResourcePolicy -> ResourceAuthorizationService -> AgentRouter`。当前 descriptor 恰好为 `list_files: argument_text/DIRECTORY/READ` 与 `analyze_excel: argument_text/FILE/READ`；非文件 Tool 显式无资源请求。

执行顺序冻结为：`ToolRegistry.require -> ToolGovernanceService.authorize_tool -> adapter.build_invocation -> adapter.spec_for -> ToolGovernanceService.evaluate_invocation -> extractor.extract -> ResourceAuthorizationService.require_authorized -> ToolExecutionService.execute_sync`。`ToolGovernanceService`、`ToolExecutionService`、`ToolInvocation`、`ToolExecutionSpec`、RuntimeEvent 与 Journal schema 均未改变。拒绝固定为 `TOOL_RESOURCE_DENIED`，不产生 Tool events、不调用 final-answer model，并按既有 OutputGate / delivered-only Memory 语义交付安全文本。

Windows 判定在 I/O 前拒绝 relative、drive-relative、UNC、device/extended namespace；existing candidate 经 `Path.resolve(strict=True)` 后按 kind 校验，并以 `ntpath.normcase/normpath + commonpath` 对多个 canonical roots 做 component-aware containment。此边界不是 OS Sandbox，仍保留 authorization-to-open TOCTOU 限制。

### 2.4 HTTP Request Payload Boundary（Stage 3 WP3-B，INTERNAL_RC）

生产入口链冻结为：

```text
HTTP request
-> RequestBodyLimitMiddleware（raw ASGI body，实际 bytes，application-wide）
-> FastAPI / Pydantic（endpoint 字段语义与 chars/count/range）
-> endpoint / ChatService / MemoryManager
-> Coordinated Runtime（仅在前置校验成功后创建 Run）
```

`RequestPayloadPolicy` 是不可变的 APPLICATION_SCOPE 常量 Authority，不读取 Settings、环境变量、header 或 body，也不允许构造时覆盖冻结值。`RequestBodyLimitMiddleware` 在下游执行前完整缓冲并按实际 body bytes 计数；`Content-Length` 只用于提前拒绝，缺失、前导零、低报或等于上限均不绕过实际计数。重复或非法 `Content-Length` 固定返回 HTTP 400 `{"detail":"Invalid Content-Length"}`；声明值或实际值超过 `1,048,576` bytes 固定返回 HTTP 413 `{"detail":"Payload Too Large"}`。disconnect 或非 `http.request` 消息停止下游调用且不记录正文。

字段边界由 FastAPI/Pydantic 在 endpoint 前执行：`query=32,768` chars、`file_path=4,096` chars、`agent_id=64` chars、`run_id=45` chars、`keyword=1,024` chars；历史分页 `limit=1..100`（default `10`）、`offset=0..100000`（default `0`）；删除 ID 集合最多 `1,000` 个且每个 ID 为 `1..2^63-1`。所有数值（包括两个 history defaults）均只由 `RequestPayloadPolicy` 拥有，route 只消费 policy facts。字符上限使用 Python 字符长度，与 UTF-8 byte 上限相互独立。既有 empty/whitespace/NUL、UUID 解析、unknown-field ignore 和 `delete_all` 语义未被扩大修改。

HTTP payload Gate 是 pre-Run transport/application validation，不是 Runtime Budget、Tool Permission、Resource Authorization、Rate Limit 或 DLP。被 HTTP 400/413/422 拒绝的请求不创建 Run，不产生 RuntimeEvent/Journal/Memory mutation；HTTP 422 仍采用 FastAPI 默认 validation detail，当前会回显被拒绝字段输入，是已记录的 WP3-C 候选边界。

### 2.5 Context Trust / Typed Security Denial（Stage 3 WP3-C，INTERNAL_RC）

Model Context的安全边界由typed source/trust而不是正文内容决定。`ContextBuilder`是source/trust到model role的唯一绑定 Owner：只有code-owned `SYSTEM_INSTRUCTION` / `AGENT_INSTRUCTION` + `TRUSTED_INSTRUCTION`进入`system`；User、RAG、Tool、Memory/History、Summary、Plan、Runtime state、current Step与Step Result均为data/proposal并进入data role。raw history只接受`user` / `assistant`，不能创建`system`消息。

关键冻结映射为：

```text
Tool observation -> TOOL_RESULT / UNTRUSTED_EXTERNAL / user
Synthesis dependency -> STEP_RESULT / USER_CONTENT / user
Code-owned Tool/Synthesis control -> SYSTEM_INSTRUCTION / TRUSTED_INSTRUCTION / system
```

Tool denial integrity链为：

```text
actual ToolGovernanceError / ResourceAuthorizationError
-> AgentAdapterResult(ResultDisposition.SECURITY_DENIED, SecurityDenialCode)
-> StepResult
-> StepResultStore
-> DependencyResultView
-> Synthesis DENIAL_DOMINATES
```

该链不按正文做string matching、regex或keyword判断。Synthesis在context build、model selection与model invocation前检查typed denial；任一required dependency被拒绝即返回固定safe denial，成功partial result不进入用户可见合成。COORDINATED与explicit LEGACY均不允许denial被后续模型改写成success；OutputGate、RuntimeEvent、Journal与Snapshot合同未修改。

能力状态为`PARTIALLY_SUPPORTED`：模型仍可能受恶意自然语言影响，System Prompt可能被复述/改写，RAG/Memory/Tool/Step data仍可能影响自然语言回答。无generic injection classifier、WAF、generic DLP、Human IAM、full Sandbox或HITL；无dedicated security-denial RuntimeEvent/Journal/Snapshot fact，Recovery不能重建runtime-internal typed denial。Command Injection与SSRF在当前Tool inventory中为`NOT_APPLICABLE_CURRENT_INVENTORY`。

### 2.6 SQLite Statement Authority（Stage 3 WP3-D）

current LocalAgent production SQLite inventory 采用单向 authority：SQL structure owner 是 code，User、Model output、RAG、Tool result、Memory text 与 HTTP payload 只能经 DB-API parameter binding 成为 values，不得提供 statement、identifier、keyword 或 ordering authority。直接 SQLite owner 冻结为 `core/memory_manager.py`、`core/persistence_migration.py`、`core/runtime/event_journal_store.py`、`core/runtime/event_consumer.py` 与 `core/runtime/snapshot_store.py`；排序由 code-owned boolean 映射，动态 `IN` 仅生成 `?` placeholders，固定 module constants保持immutable code structure。

test-only AST Gate 对完整 production Python surface 做 owner discovery、receiver resolution 与 SQL sink classification；新增 owner、未知 receiver、动态 statement、`executescript`、未解析 helper 或 exception shape drift 均 fail closed。schema-metadata PRAGMA 只允许精确 startup/internal/read-only helper shape、固定 metadata identifier与`sqlite3.Error` fail-closed行为，不构成通用 identifier interpolation。Chroma internal persistence不是LocalAgent direct SQL owner。

能力状态为`SUPPORTED`，但只覆盖 current LocalAgent production SQLite inventory。No generic SQL firewall/parser，No NL2SQL validator/feature，No SQL Tool；未来新增 Tool/NL2SQL/direct SQLite owner/数据库技术必须重新过 Gate。FTS query-language semantics 与 LIKE wildcard semantics 保持搜索语义；用户可见固定错误不代表 internal logs 已实现 generic DLP。本节不宣称 WP3-D Final Gate、WP3 aggregate 或 Stage 3 PASS。

### 2.7 WP2 Tool Platform Integration Gate（offline deterministic E2E）

production Tool chain 在本 Gate 前已经实现；WP2-C 不新增 production integration 或第二套装配，只以 `tests/test_stage3_wp2_tool_e2e.py` 增加确定性全链验证。当前被测试的 topology 为：

```text
server.app（POST /api/chat）
-> server.py::lifespan() production composition root
-> default COORDINATED ChatService
-> dynamic PlanResolver / StrictPlanningDecisionParser / PlanCompiler
-> RunCoordinator / Scheduler / ParallelExecutor / MultiAgentDriver
-> AgentRouterSingleAgentAdapter / AgentRouter
-> frozen production ToolRegistry
-> frozen ToolPolicyCatalog / ToolGovernanceService
-> ToolExecutionService / ToolAdapter / production Tool
-> StepResultStore / StepResultCommitter
-> OutputGate / RunFinalMemoryWriter
-> committed exchange receipt / SemanticMemoryFormation
-> RuntimeEventChannel + SQLite Journal
-> ChatStreamCompatibilityAdapter
-> user-visible text/plain TEXT
```

- success 场景由 `core_router` 动态规划到单步回答，真实执行 `list_files`；临时目录中的已知文件名先出现在 production Tool observation，再进入 final-answer model 和唯一用户 TEXT。`TOOL_STARTED` / `TOOL_COMPLETED`、Journal、delivered-only Memory 与唯一 `OUTPUT_DELTA` 同时提供证据。
- governance 场景由同一路径规划 `complex_workflow_simulator` 的 `NON_IDEMPOTENT_SIMULATION`；production exact-combination risk 判定为 `HIGH / APPROVAL_REQUIRED`，以固定 safe denial 正常交付，且 Tool events、Journal Tool facts、Tool state mutation 和 final-answer model invocation 均为零。
- 测试只在既有 `server.LocalLLMEngine` constructor seam 注入按 prompt 语义响应的 FakeModel；Router、Planner、Registry、Governance、execution service、adapter、Tool、OutputGate、Memory 与 Journal 均为 production 对象。每个场景使用独立临时持久化路径，不访问 Internet、remote LLM、外部服务或真实开发数据库。
- `OUTPUT_DELTA` 在 Journal 中保留安全事实，经 `ChatStreamCompatibilityAdapter` 转换为 TEXT；它不是 `[[ORCH]]` CONTROL chunk。测试保留原始 ASGI body message 边界并消费至 `more_body=false`，据此验证 CONTROL 与用户 TEXT 分离。
- 该测试证据不改变现有 Owner、contract classification 或安全能力，也不代表 human approval、approval evidence、durable approval resume、filesystem/path authorization、sandbox 或 dedicated governance event 已实现。

### 2.5 Phase5 Semantic Memory Formation

默认 `COORDINATED` 路径在唯一 final Step 的 `OutputGate=DELIVERED` 且
`RunFinalMemoryWriter.write_delivered()` 成功提交 canonical conversation exchange
后，使用不可变 committed exchange receipt 触发 awaited、bounded、run-scoped
`SemanticMemoryFormation`。Formation 只消费 original user query、仅供规范化辅助的
delivered answer，以及真实 run/exchange/entry-agent/`direct` scope identity；经统一
Model Invocation 产生严格 schema v1 proposal，再由 LocalAgent code-owned gate 校验
category、grounding、字段形状和 authoritative identity，最终通过
`AdvancedMemoryStore.create()` 按 candidate 独立 transaction 创建 `ACTIVE SEMANTIC`
record。

Formation 的 `FAILED` / `PARTIAL` / `CANCELLED` / `TIMED_OUT` 不改变已交付正文、
final Step、Run terminal，也不触发再次交付。`MEMORY_FORMATION_COMPLETED` 是 Event v2
内新增的 content-minimized typed outcome；其 payload 自带 formation schema version 1，
只保存 identity、count、safe outcome/reason、memory ID 和 latency。指标从该 Journal-first
event 投影；`memory.formation` span 是 `INTERNAL_RC` extension operation，不是 Trace
Contract v1 的第七个公共 operation，公共 trace export 对它保持 fail closed。

WP2 只保证同一 execution 内 prepared-record persistence retry 幂等；不实现跨进程
replay、cross-Run dedup、Conflict Resolution、NO_CHANGE、supersede、forget、retrieval
或 Context Injection。

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
| MemoryFormationCompletedPayload | PUBLIC_VERSIONED | Event v2 内嵌 Formation outcome schema v1；content-minimized，失败不改变 delivery/terminal |
| ToolRegistry | INTERNAL_RC | 进程级 Tool 身份/描述/枚举/绑定唯一事实源；startup 冻结后只读 |
| ToolDescriptor | INTERNAL_RC | 不可变 name + description；不承载执行状态 |
| ToolRegistration | INTERNAL_RC | 不可变 Descriptor + ToolAdapter 绑定 |
| ToolPolicy | INTERNAL_RC | 静态 per-Tool policy（explicit allowed Agent IDs + risk facts + approval threshold）；frozen |
| ToolPolicyCatalog | INTERNAL_RC | 静态 policy facts 唯一事实源；startup 冻结后只读 |
| ToolGovernanceContext | INTERNAL_RC | frozen；principal_agent_id + run_id + step_id；不保存正文 |
| ToolGovernanceDecision | INTERNAL_RC | frozen；outcome + risk_level + risk_facts + safe_error_code |
| ToolGovernanceService | INTERNAL_RC | 唯一 invocation-time Authority；两级 Gate |
| ToolRiskFact / ToolRiskLevel / ToolGovernanceOutcome | INTERNAL_RC | 固定治理枚举 |
| ToolGovernanceError / ErrorCode | INTERNAL_RC | 固定 code + fixed safe message；pre-execution governance failure |
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
| Trace Contract v1 | PUBLIC_VERSIONED | 六个稳定 operation 语义冻结；内部 Span 模型（`TraceContext`/`SpanHandle`/`SpanRecord`）为 INTERNAL，不是公共 exporter payload |
| Consumer-neutral Trace export contract | PUBLIC_VERSIONED | `core/runtime/trace_export_contract.py`：identity/version、不可变 `TraceExportEnvelope`、严格 completed-only 投影、六类 category 导出 schema（type/presence/value-domain）、`TraceCompatibilityEvaluator`；同时是 Export Contract Semantic Owner，唯一构建权威规范语义描述符（`export_contract_semantic_descriptor()`）。消费者包括已实现的 `TraceExportDispatcher`（WP4-B） |
| Trace Contract Fingerprint | PUBLIC_VERSIONED | `core/runtime/trace_contract_fingerprint.py`：`TraceContractFingerprinter` 只做 canonicalize+digest（sha256 + canonical_json_v1，lowercase 64-hex）；语义描述符由 export contract owner 消费，识别 schema+语义兼容性（含 value-domain 与 compatibility 行为），不识别 Trace 实例/Run/配置 |
| TraceExporter protocol | INTERNAL_RC | `core/runtime/trace_exporter.py`：envelope-only transport-neutral Protocol（`send(TraceExportEnvelope)` + `close(timeout_seconds)`）；adapter 只接受公共 envelope，禁止 raw `SpanRecord`/dict/mapping |
| TraceExportDispatcher | INTERNAL_RC | `core/runtime/trace_export_dispatcher.py`：APPLICATION_SCOPE bounded 分发器（projection invocation、compatibility consumption、queue、worker、drop/health、flush/close）；`TraceExportHealthSnapshot` 为不可变 content-free 内部快照 |
| CompletedSpanObserver | INTERNAL_RC | `InMemorySpanRecorder` 单个可选 completion observer（`Callable[[SpanRecord], bool | None]`）；非公共 fan-out/plugin 机制 |

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
| Trace export contract | 1 | Trace Contract Fingerprint（canonical JSON SHA-256，lowercase 64-hex） | export semantic descriptor（`trace_export_contract.py`）→ canonicalize/digest（`trace_contract_fingerprint.py` + `snapshot_serialization`） | 1 | 1 | fail closed | 未知/缺失 identity/version/fingerprint 按合同 fail closed | 不写回 |

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
- TraceExportDispatcher 是侧通道 observability 能力：绝不修改 Run terminal status、AgentState、OutputGate/DeliveryStatus、Memory commit、Journal、Snapshot、Recovery 或 Tool/Retrieval。
- `span_recorder.close()` 成功返回 = trace export producer barrier；`ApplicationRuntimeServices` target 顺序冻结为 `span_recorder < trace_export_dispatcher < snapshot_store`，不得反转。
- Exporter metric 禁止高基数 label（`run_id/trace_id/span_id/step_id/fingerprint/contract_fingerprint/endpoint/url/raw_status/raw_exception`）；fingerprint 是 contract identity，不是 metric dimension。

## 11. Persistence / Migration / Operations Closure（WP1-D）

### 11.1 Persistent Store Inventory

| Store | Logical role | Physical unit | Classification | LocalAgent schema owner? | Rebuildable? |
| --- | --- | --- | --- | --- | --- |
| Memory | 业务对话、摘要、delivered-only exchange | `agent_memory.db` | DURABLE_APPLICATION_STATE | YES，`MemoryManager` | NO |
| Runtime Event Journal | append-only Runtime 安全事实、sequence、terminal evidence | `runtime_event_journal.db` | DURABLE_APPLICATION_STATE | YES，`SQLiteRunEventJournal` + `JournalRecord` | NO |
| Snapshot | opt-in 历史 `RunSnapshot` v1 | `runtime_snapshots.db`（仅 enabled） | DURABLE_APPLICATION_STATE_OPT_IN | YES，`SQLiteSnapshotStore` + Snapshot contract | NO（历史 snapshot） |
| Observability Checkpoint | logger/metrics 幂等消费 offset | `runtime_observability_checkpoint.db` | REBUILDABLE_DERIVED_STATE | YES，`SQLiteEventConsumptionCheckpointStore` | YES（缺口可接受） |
| Chroma collection | KB 派生向量索引 | `chroma_db/` | REBUILDABLE_DERIVED_STATE | Chroma internal = NO；LocalAgent collection/chunk contract = YES | YES（需 KB source + 匹配 embedding artifact） |
| KB source | Chroma rebuild 的业务源 | `data/knowledge_base/` | SOURCE_DATA | 文件/loader contract 由 LocalAgent 管理 | 不应假设可从其他 Store 重建 |
| GGUF / Embedding model | 生成/向量语义依赖 | `data/models/` | DEPLOYMENT_ARTIFACT | NO | 由已批准 artifact 重新部署 |

逻辑 Store 数 = 5，物理 backup 单元 = 5（Memory、Journal、Snapshot、Checkpoint、Chroma）。

### 11.2 Schema / Version / Migration Policy

| Store | Version mechanism | Migration | Historical rewrite |
| --- | --- | --- | --- |
| Memory | `PRAGMA user_version=2`（SQLite physical marker；v2 新增独立 `long_term_memory` 结构） | 显式 SCRIPT_ROLE：current-unversioned → 版本 2 adoption；v1（无 `long_term_memory`）→ additive 新增 Long-term Memory 结构；唯一 allowlisted pre-additive legacy → additive columns + backfill + tables/indexes/FTS/triggers + Long-term Memory | 不修改业务 row 正文；version 与 schema change 同事务原子提交 |
| Journal | exact physical signature（无 DB-level version）；row v1/v2 | 显式 SCRIPT_ROLE：仅允许缺 nullable `span_id`/`parent_span_id` 的 legacy → 单事务 ADD 两列 + index | **FORBIDDEN**（不 UPDATE/DELETE/rewrite 历史 row、version、digest、sequence、terminal ordering） |
| Snapshot | row/payload v1（`snapshot_schema_version=1`）；exact table shape | 无 migration | FORBIDDEN（v0 不存在；未知版本 fail closed；不写回） |
| Checkpoint | exact table shape（无版本） | 显式 SCRIPT_ROLE：不兼容 → 单事务 drop/recreate derived table | 可丢弃整个 derived Store（历史 offset 丢弃，不改变业务 Authority） |
| Chroma | LocalAgent collection metadata marker（`localagent_collection_contract_version=1` + `chunk_schema_version=kb_chunk_schema_v2` + `embedding_compatibility_digest` + `embedding_dimension`） | marker validation + operator rebuild；不碰 Chroma internal SQLite | 不做 internal row migration |

`record/payload schema version != SQLite physical schema version`。`journal_schema_version=2` 只说明 record digest/payload contract，不是 DB physical version；Memory 的 `PRAGMA user_version=2` 才是 physical marker。

### 11.3 Migration Boundary（frozen）

```text
Migration Runner = Minimal Persistence Migration Coordinator（core/persistence_migration.py）
Server startup   = automatic READ-ONLY preflight（PRAGMA quick_check + physical shape + 版本事实）
Existing-data migration = explicit SCRIPT_ROLE command only（manage_persistence.py migrate --backup-confirmed）
Backup           = manual stopped-server only（.db + 任何 -wal 为同一 unit；-shm 不要求）
Restore          = manual stopped-server set replacement + explicit full preflight
Downgrade        = NOT_IMPLEMENTED（forward-only）
Rollback after schema mutation = restore matching pre-migration backup（binary-only rollback NOT ASSUMED）
```

- Coordinator 只做 preflight orchestration、migration ordering、safe result aggregation、safe error/result model；
  不得成为 Memory/Journal/Checkpoint/Chroma schema owner。Store-specific SQL/transaction 保留在对应 Store module。
- 每个支持 mutation 的 SQLite Store 使用独立单 Store transaction（`BEGIN IMMEDIATE → revalidate from-state → change → version marker（Memory）→ COMMIT`；失败 ROLLBACK）。
- 无 cross-store atomic transaction / distributed transaction / two-phase commit。多个 Store 部分 commit 后失败：overall FAIL + partial committed facts；rerun 从实际 facts 继续（idempotent / safely re-runnable，不宣称 exactly-once）。
- Migration 是 forward-only：schema-changing migration 提交后 old binary compatibility NOT ASSUMED。无 reverse SQL / downgrade。
- Server startup 绝不自动迁移已有数据；preflight 发现 MIGRATION_REQUIRED / UNSUPPORTED / FAILED → `never READY`。
- 三个新增 safe error code：`PERSISTENCE_SCHEMA_UNSUPPORTED`、`PERSISTENCE_PREFLIGHT_FAILED`、`PERSISTENCE_MIGRATION_FAILED`。

### 11.4 Chroma Third-Party Boundary

```text
Chroma internal schema migration = NOT_LOCAL_SCHEMA_OWNER
```

LocalAgent 不读取/不修改 Chroma internal SQLite schema。LocalAgent 拥有 collection/chunk/embedding compatibility contract：
空 collection 可初始化 marker；非空缺 marker / digest / dimension mismatch → REBUILD_REQUIRED（required KB 阻止 READY，显式 optional KB 允许 READY_DEGRADED）。Startup 绝不自动 clear/rebuild。
`bootstrap_local_kb.py --rebuild` 是唯一 operator-triggered destructive rebuild：invalidate/remove marker → destructive clear → ingest complete source → verify → **最后发布匹配 marker**；任何失败不得保留“看似有效”的旧 marker。
`embedding_compatibility_digest` 是 configured compatibility descriptor digest（embedding identity / normalization / query prompt 的 canonical JSON → SHA-256），不是 model artifact 的 cryptographic attestation；raw path 不持久化。

### 11.5 Backup / Restore / Rollback Contract

- MUST_BACKUP（同一 Server-stopped backup epoch）：Memory DB、Journal DB、Snapshot DB（仅 enabled/存在）、KB source、known-good config reference。
- OPTIONAL_BACKUP：Chroma directory（可加速 restore，correctness 依赖 source + matching embedding artifact rebuild）。
- BACKUP_OPTIONAL / RECREATE：Observability checkpoint（derived，startup 仍 required）。
- Restore success 至少要求：显式 full preflight PASS、Server `READY`（或 allowlisted `READY_DEGRADED`）、required durable Stores 可读、health/readiness smoke PASS。
- 代码回滚与数据回滚是两件事；`CHAT_RUNTIME_MODE=legacy` 不能替代 data rollback。

### 11.6 Migration vs Recovery

```text
Deployment Migration != Runtime Recovery Validation
```

Migration 处理 deployment upgrade 中的 Store schema/compatibility（显式 SCRIPT_ROLE，Server stopped）。Recovery 仍为 validation-only：`RecoveryValidator` 只读 Snapshot + Journal 返回 immutable `RecoveryAssessment`；不写 AgentState、不启动 replay/resume、不从 Registry/Memory/adapter 回填历史事实。Migration/backup/restore 均不是 Runtime Recovery，也不改变其 validation-only 边界。

## 12. Trace Export Dispatch / Exporter（WP4-B）

WP4-B 是 application-scoped、consumer-neutral 的 Trace export 分发能力：把 WP4-A
已校验的公共 `TraceExportEnvelope` 值以 bounded、非阻塞、best-effort 方式交给
transport-neutral adapter。本部分只描述已实现能力；`production external
delivery` 仍 `NOT_IMPLEMENTED`（WP4-C）。

### 12.1 Final Export Pipeline

```text
SpanHandle._end()
  -> InMemorySpanRecorder.record(completed SpanRecord)
     -> local recorder bookkeeping FIRST（lock 内：移除 active、append 或 closed drop）
     -> recorder lock released
     -> single optional completion observer
        -> TraceExportDispatcher.observe_completed_span(record)
           -> project_span(record)                 [WP4-A Owner]
           -> TraceCompatibilityEvaluator          [WP4-A Owner]
           -> bounded queue.Queue[TraceExportEnvelope]
              -> single application-owned daemon worker
                 -> TraceExporter.send(TraceExportEnvelope)
```

raw `SpanRecord` 永不到达 exporter adapter；adapter 输入严格为
`TraceExportEnvelope`。

### 12.2 TraceExportDispatcher Owner（APPLICATION_SCOPE）

Owns：`project_span()` invocation 时机、`TraceCompatibilityEvaluator` consumption、
bounded export queue（`queue.Queue(maxsize=capacity)`，显式正整数 capacity）、
单 daemon worker、submission/drop 计数、content-free health、bounded
flush/close 生命周期。

Does NOT own：Trace export 语义合同/schema/value-domain、fingerprint、
compatibility 语义（均仍归 WP4-A Owner）、Runtime outcome、Journal、Snapshot、
Recovery、AgentEvalOps mapping、retry/batch/durability、transport-specific
mapping。

Producer 路径（`observe_completed_span`）只做 SpanRecord type check、projection、
compatibility、状态/计数同步与 `put_nowait`：无 I/O、sleep、await、retry、
blocking put；绝不调用 exporter。worker 是 adapter 的唯一调用者：串行 `send`，
per-item 至多一次 transport attempt；adapter 普通异常被隔离并继续，只有
dispatcher 内部不变量失败才进入 `FAILED`。

### 12.3 TraceExporter Protocol（envelope-only）

`core/runtime/trace_exporter.py`：transport-neutral `Protocol`：

- `send(envelope: TraceExportEnvelope) -> None`：对单 envelope 一次 transport
  attempt；成功返回不代表 remote durable persistence；
- `close(timeout_seconds: float) -> bool`：bounded 物理关闭，至多一次。

无 `start/open`、无 adapter `flush`、无 queue/retry/batch。禁止输入 raw
`SpanRecord`、OTel-shaped mapping、dict 或 JSON 字符串。

### 12.4 Recorder Observer Seam

`InMemorySpanRecorder` 有且只有一个可选 `completion_observer`
（`Callable[[SpanRecord], bool | None]`，构造注入，默认 `None`）：

- 在权威本地 bookkeeping 之后、recorder lock 释放之后 best-effort 调用；
- observer 普通异常被隔离，不改变本地 recorder truth；返回值不影响任何业务
  行为（False 只表示导出侧通道拒绝，不是本地 recorder 失败）；
- 无 generic fan-out、observer registry、plugin framework 或多 sink 排序。

### 12.5 RECORDER_CLOSED Semantics

`recorder.close()` 会把 close-start 存在的 active spans 以
`status=CANCELLED`、`error_code=RECORDER_CLOSED` 收口。这些 completed
`SpanRecord` 遵循既有 recorder closed/drop bookkeeping（不进正常 completed
snapshot，`dropped_span_count` 递增），**且**仍通知 completion observer——它们
是经正常 record seam 的真实终态。

### 12.6 Producer Barrier

`span_recorder.close()` 成功返回 = exact producer barrier：close-start 所有
active handles 已同步完成 end → local record/drop → observer invocation；之后
新 `start_span()` 只返回 noop handle，不再产生真实 exportable `SpanRecord`。

### 12.7 Application Lifecycle Ordering

`ApplicationRuntimeServices` 冻结 target 顺序：

```text
observability_dispatcher -> span_recorder -> trace_export_dispatcher
-> snapshot_store -> event_journal -> remaining targets
```

- Flush：trace exporter flush 是 pre-close drain，不替代
  `dispatcher.close()` 内嵌 final drain；
- Close：`span_recorder.close()` → producer barrier →
  `trace_export_dispatcher.close()`（stop accepting → final accepted barrier →
  bounded drain → worker shutdown → `adapter.close()` → bounded join）。

`trace_export_dispatcher` 是 optional dependency（默认 `None` = disabled-by-
absence）：absent 时不产生任何额外 target/component result/worker/queue/drop，
不创建 Noop exporter 或 disabled dispatcher 对象。

### 12.8 GracefulShutdown Boundary

`GracefulShutdownCoordinator` 仍是整体 shutdown orchestration Owner。WP4-B 未
新增 shutdown orchestration phase、未改 ShutdownReport schema、未建第二 shutdown
Owner；`ApplicationRuntimeServices` 的 ordered targets 已足够表达 exporter
lifecycle。

### 12.9 Component Truth

Exporter lifecycle 以独立 `RuntimeComponentResult` 表达：
component=`trace_export_dispatcher`、operation=`FLUSH`/`CLOSE`。不 merge 入
`observability_flush_status`/`trace_flush_status`，不新增 ShutdownReport 字段。

Close 结果按 dispatcher content-free 生命周期事实分类（Phase 3.3 R1 + R2 修复）：

- 物理 lifecycle 已结束（`state == CLOSED`）但 adapter close 返回 False /
  抛异常（被隔离）→ `RUNTIME_TRACE_EXPORT_CLOSE_FAILED`；
- worker fatal（`state == FAILED`，worker 已死，close 不经 deadline 耗尽
  立即返回 False）→ 也是失败 → `RUNTIME_TRACE_EXPORT_CLOSE_FAILED`
  （不是 timeout）；
- deadline 到期而 lifecycle 未完成（`state == CLOSING`，worker 可能仍存活）→
  `RUNTIME_TRACE_EXPORT_CLOSE_TIMEOUT`。

不把每个 `close(False)` 都报为 timeout；不把已知 worker fatal 误报为
timeout。`CLOSED` 状态可能与 close result False 共存（adapter 物理 close
失败）。真实 `_invoke_bounded` deadline 过期由调用方 TimeoutError 分支映射为
TIMEOUT，不被 post-call 状态分类覆盖。独立 Final Re-Gate 将复核 worker
fatal / FAILED 状态的 component truth（本文档不预先声明该探针结果）。

### 12.10 Delivery Semantics

- overall delivery = `BEST_EFFORT`；
- 每个 accepted envelope = 至多一次 transport attempt；
- queue acceptance ≠ attempted ≠ sent ≠ remote durable/ack；send 成功不代表
  remote durable persistence；
- 进程崩溃可丢失 queued/in-flight envelopes；
- 不承诺 at-most-once delivery、at-least-once、exactly-once 或跨进程顺序。

### 12.11 Queue / Backpressure

`queue.Queue` thread-safe、显式正 bounded capacity；producer `put_nowait`
非阻塞；full 时 `DROP_NEWEST / REJECT_INCOMING`（不 evict 已 accepted item）。
Exporter backpressure 绝不阻塞 Runtime business path。不复用
`RuntimeEventChannel`/`asyncio.Queue`。

### 12.12 Concurrency / Ordering

Span completion 可来自多个线程；producer 路径 thread-safe、bounded、
CPU/local-only。单 export worker 串行 adapter send（最大并发 1）。Queue FIFO
只是本进程 local handling order，不承诺 parent-before-child、sibling 顺序或
`RuntimeEventChannel.sequence` 语义顺序。

### 12.13 Flush Semantics

`flush(timeout)` 捕获 `accepted_total` barrier；成功表示 barrier 前所有
accepted envelopes 已完成其单次 attempt 处理（success 或 transport failure），
不代表全部 sent/remote durable/ack。timeout 返回 False、flush_failures 递增、
worker 继续；不立即 drop pending。

### 12.14 Close Semantics

`close(timeout)`：停止接受 → 捕获 final barrier → bounded drain → worker
shutdown control（单 sentinel）→ `adapter.close(remaining)` → bounded join。
幂等、并发 safe（单 physical close owner）。timeout 且 worker 仍存活时不得
标记 CLOSED；后续 close 可继续 bounded wait。`CLOSED` 表示物理生命周期结束，
不表示 close error-free。

### 12.15 Retry / Batch / Durable / Serialization

- retry = `NOT_IMPLEMENTED`（每 accepted envelope 至多一次 transport attempt；
  现有 Model `RetryExecutor` 属 invocation 语义，与 exporter 无关）；
- batching = `NOT_IMPLEMENTED`；
- durable delivery = `NOT_IMPLEMENTED`（无 outbox/spool/ack log；进程崩溃可
  丢失 queue/in-flight；无 Trace/Journal/Snapshot replay）；
- generic consumer-neutral wire serialization = `NOT_IMPLEMENTED / DEFERRED`
  （保持 typed-envelope protocol；无 `to_json`/stable wire JSON/HTTP payload；
  WP4-C 拥有 AgentEvalOps-specific mapping）。

### 12.16 Disabled / Production Enable Boundary

WP4-B disabled mode = dispatcher absent（`completion_observer=None` +
`trace_export_dispatcher=None`），不创建 worker/queue/adapter。WP4-B 自身目前
没有 concrete production external adapter、没有 `server.py` wiring、没有
endpoint/auth 配置；这些属于 WP4-C（唯一 Composition Root
`server.py::lifespan()` 注入）。

### 12.17 Metric Drop Reason Vocabulary

`runtime_trace_export_dropped_total{reason}` 使用 dispatcher code-owned 有限词表
（`TraceExportDispatcher.TRACE_EXPORT_DROP_REASONS`，恰好 7 个值；
`core/runtime/metrics.py` 只 import 同一常量做 descriptor bounded_values，不复制
词表）。冻结语义：

- `projection_failed` / `incompatible` / `queue_full` / `closed` /
  `transport_failed`：projection/compatibility/入队/关闭/transport 阶段的
  真实丢失或拒绝；
- `shutdown_timeout`：final dispatcher shutdown deadline 实际到期，且 queued
  envelope 因此（而非其他原因）被 final lifecycle 永久放弃；
- `worker_unavailable`：唯一 export worker 不可用/已死，queue 无法再被 drain，
  queued envelope 在 finalization 中被永久放弃。

**`failures{stage=worker}`（回答"哪个组件失败"）与
`dropped{reason=worker_unavailable}`（回答"为什么该 envelope 丢失"）是互补
事实**，不是同义重复；worker fatal 只发 `stage=worker` failure，其 abandonment
drop 只使用 `worker_unavailable` reason（不得使用 `shutdown_timeout`）。该词表
属于 WP4-B `TraceExportDispatcher` 的 `INTERNAL_RC` operational contract，不是
WP4-A `PUBLIC_VERSIONED` Trace Export Contract；不修改
`TraceExportEnvelope`/fingerprint/health schema/lifecycle。
