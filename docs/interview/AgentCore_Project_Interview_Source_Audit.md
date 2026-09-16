# AgentCore Project Interview Source Audit

> 审计日期：2026-09-16  
> 展示名称：AgentCore  
> 源码仓库：LocalAgent / `Local_Agent`  
> 方法：只读源码审计；未启动 Backend、未调用真实 Provider/MCP、未运行 pytest。测试项只证明“存在对应测试”，不代表本轮重新执行通过。

# 1. PROJECT_POSITIONING

AgentCore 当前是一个以 FastAPI 为服务入口、以 Coordinated Runtime 为执行内核的 **Agent Runtime + Agent Harness + AI Backend 组合平台**。它仍保留 PyQt6 客户端入口，但生产事实中心已经不是“单机端侧聊天应用”，而是支持持久化控制面、多实例 Run 所有权、模型路由、工具治理、人工审批、恢复证据、检索与记忆的服务端平台。

主要场景是：认证用户通过 HTTP 发起流式聊天或结构化 Run；Runtime 解析/编译计划，调度单 Agent 或受控多 Agent 步骤，调用模型、RAG、Memory 和 Tool，并把安全事件写入 PostgreSQL Journal，最终通过唯一 Output Gate 发布结果。AgentEvalOps 通过受限 SERVICE JWT 调用 evaluation-v2，而不是共享 AgentCore 内部对象。

Canonical Production Path：

```text
FastAPI /api/chat 或 /api/runtime/execute
→ ChatService
→ CoordinatedRuntimeFactory.create_run_scope
→ PostgreSQL DurableRunControlService.claim
→ RunCoordinator
→ Planner / PlanCompiler / SerialScheduler / ParallelExecutor
→ ModelInvocationRouter / Retrieval / Memory / Tool Runtime
→ EventEmitter → PostgreSQL Journal → EventChannel
→ OutputGate → terminal event + fenced control close
```

真实实现能力包括：严格 AgentStateMachine、动态/静态计划、受限并行多 Agent、绝对 Deadline 与取消、模型 retry/fallback/circuit、原生 HTTP SSE typed delta、工具 typed invocation/validation/governance/HITL、PostgreSQL lease/fencing、durable approval/execution claim、side-effect ledger、stdio MCP resilient lifecycle、Dense/BM25/Hybrid RRF、PostgreSQL conversation/semantic/episodic/project memory、JWT HUMAN/SERVICE 授权、Redis cache/rate-limit、Kafka transactional-outbox evaluation pipeline、Prometheus/OpenTelemetry、graceful shutdown。

仅测试或隔离 evaluation 能力：fault injection、deterministic failed/success plan、episodic fixture/replay/capture 等只从 evaluation-v3/v4 或注入 seam 可达，不能说成普通用户可控生产功能。

Legacy 结论：旧 `stream_chat` 不是 `/api/chat` 的生产 fallback；SQLite memory 类仍在源码和大量旧测试中，但 `server.py::lifespan()` 生产装配使用 PostgreSQL bridge；`requests` 仍用于桌面 readiness/脚本，不是 RemoteLLMEngine Provider transport；旧 wire/schema compatibility 读取仍存在，不能笼统说“所有 legacy 已删除”。

明确边界：没有通用外部副作用 exactly-once；没有 Saga/TCC/2PC；没有 MCP HTTP/Streamable HTTP；MCP 工具清单是 startup snapshot，不支持运行期 hot reload；远端取消只能中止本地等待/关闭 HTTP 流，不能证明 Provider 已停止计算；没有 resumable user streaming；Redis/Kafka 都不是 Run Truth；cross-encoder 不是当前生产默认路径。

# 2. REAL_ARCHITECTURE

```text
Human / SERVICE Principal
  → FastAPI middleware（JWT、role/scope、object ownership）
  → /api/chat | /api/runtime/execute | evaluation-v2
  → ChatService（transport、disconnect、event consumption）
  → CoordinatedRuntimeFactory（per-run scope）
  → PostgreSQL RunControl claim + fencing token
  → RunCoordinator（唯一 Run 生命周期协调者）
      → PlanningModelAdapter → PlanCompiler → frozen Plan
      → SerialScheduler / ParallelExecutor
      → Agent adapter / Synthesis adapter
          → ModelInvocationRouter → RemoteLLMEngine native SSE
          → RetrievalExecution → Dense/BM25/Hybrid RRF
          → MemoryRetrievalService → PostgreSQL memory stores
          → ToolRegistry → typed validation → Governance
              → durable HITL → execution claim → ToolExecutionService
              → durable side-effect state / MCP stdio adapter
      → StepResultStore → OutputGate
  → PostgreSQL Journal / terminal + control close
  → EventChannel → HTTP stream / observability projections
```

## A. Model Invocation Chain

`RunCoordinator/AgentRouter` → `ModelInvocationRouter.ainvoke()` → `ModelRoutingPolicy.route()` → circuit permit → budget reservation → `MODEL_STARTED` journal → `GeneratorModelAdapter.ainvoke()` → `RemoteLLMEngine.agenerate()` → `httpx.AsyncClient.stream()` → `_SSEDecoder` → `TextDelta/ToolCallDelta/UsageDelta/Finish` → Runtime acceptance sink。Retry/fallback 仅在第一段被 Runtime 接受的输出之前允许；Adapter 不拥有跨 Provider 重试策略。

## B. Tool Execution Chain

模型 native tool call 或 planner 选择 → Registry 精确解析 → Adapter `build_invocation()`（typed validation，最多一次参数修复）→ `ToolGovernanceService.authorize_tool/evaluate_invocation` → 必要时 durable approval → fenced execution claim → `ToolExecutionService` 校验 Run ownership/spec → durable PREPARED/STARTED → Adapter/provider call → COMMITTED 或 UNKNOWN → typed Tool result 回填模型 continuation。顺序是 Validation → Governance → Approval → Claim → Execution。

## C. MCP Tool Chain

lifespan 读取 JSON 配置 → `McpIntegrationComponent.start()` → `StdioMcpClient.initialize()` → `tools/list` → 本地 canonical name/policy 映射 → Registry/Policy freeze → 普通 Tool Runtime → `mcp.adapter` → generation-bound stdio session → `tools/call`。重连由 lifecycle 单一 owner 管理；候选 session 只有通过 identity 与 frozen schema digest 校验才发布。只读 transport disconnect 最多在新 generation replay 一次；副作用调用不 replay。

## D. HITL Chain

Governance 返回 REQUIRE_APPROVAL → durable approval PENDING → HTTP approve/reject 以 invocation binding digest 做 CAS、first-wins → worker 观察 durable 决策 → APPROVED 后以当前 Run lease/fence 创建唯一 execution claim → 执行。Approval 只是授权事实，不等于执行权；REJECTED/INVALIDATED 永不执行。

## E. Side-effect Recovery Chain

稳定 invocation + idempotency identity → PostgreSQL PREPARED → Provider 前 STARTED → 成功后 COMMITTED；已进入 Provider 但结果不明则 UNKNOWN。恢复时新 owner 先取得新 fence，再由 provider-specific reconciler 查询 operation identity，将 UNKNOWN CAS 到 COMMITTED/NOT_COMMITTED；没有 reconciler 就保持 UNKNOWN，禁止 generic retry。

## F. Evaluation-v2 Cross-repo Chain

AgentEvalOps SERVICE principal → Bearer JWT → middleware 校验 SERVICE + `evaluation:execute` scope → `/api/runtime/evaluation-execute/v2` → 与生产相同的 `ChatService.run_coordinated_agent_evaluation()` → 返回 bounded evaluation DTO、RAG artifact/provenance 和 final-answer capture。AgentCore 生产 artifact；AgentEvalOps 负责外部评测与发布决策。跨仓真实部署结果本轮未重跑。

# 3. TRUTH / OWNER / AUTHORITY

| Truth / Decision | Current Owner | Durable? | Source Evidence | Notes |
|---|---|---:|---|---|
| Run ownership | `DurableRunControlService` + PostgreSQL | 是 | `core/runtime/run_control.py:DurableRunControlService.claim` | process-local registry 仅加速唤醒 |
| Run terminal | fenced `finalize_terminal` + Journal unique terminal | 是 | `core/runtime/run_control.py:finalize_terminal`; migration `0001` | 同事务关 control |
| Runtime event | `RunEventEmitter` / PostgreSQL Journal | 是 | `core/runtime/event_emitter.py`; `event_journal_store.py` | EventChannel 是交付通道，不是 Truth |
| Deadline | `RunContext.Deadline` / RunCoordinator watcher | 否（运行时） | `core/runtime/context.py`; `run_coordinator.py:_start_deadline_watcher` | 绝对截止时间向下传播 |
| Cancellation | PostgreSQL cancel command；local token 执行 | 命令是 | `run_control.py:request_cancel`; `runtime_factory.py:_maintain_durable_control` | 跨实例轮询 durable intent |
| Model retry/fallback | `ModelInvocationRouter` | 否 | `model_invocation.py:ainvoke`; `model_routing.py:can_fallback` | output-started 后 fail-stop |
| Provider transport | `RemoteLLMEngine` | 否 | `core/llm_engine.py:agenerate` | application-scoped httpx client |
| Tool identity | frozen `ToolRegistry` | 否（startup snapshot） | `tool_registry.py`; `tools/registry.py` | MCP 本地名在 startup 固化 |
| Tool arguments | Adapter typed builder + spec validation | 否 | `agent_router.py:_build_valid_tool_invocation`; `tool_execution.py:_validate_invocation_against_spec` | 模型参数不可信 |
| Tool risk | code-owned `ToolPolicyCatalog` | 否 | `tool_governance.py` | MCP hint 不能放宽本地 policy |
| Approval | `DurableApprovalService` | 是 | `durable_approval.py:decide` | first-wins + exact binding |
| Execution Claim | `DurableApprovalService.claim_execution` | 是 | `durable_approval.py:138` | current fence 下唯一 claim |
| Side-effect outcome | `DurableToolInvocationService` + provider evidence | 是 | `tool_idempotency.py` | UNKNOWN 是权威不确定状态 |
| Reconciliation | provider-specific reconciler + durable CAS | 是 | `tool_idempotency.py:reconcile` | 非通用补偿事务 |
| MCP session | `McpIntegrationComponent` | 生命周期内 | `mcp/lifecycle.py` | generation-bound handle |
| RAG index identity | active descriptor + immutable provenance | 文件持久 | `retrieval_index_provenance.py` | atomic active publication |
| Memory | PostgreSQL memory stores | 是 | `core/persistence/memory.py` | 不是 Runtime safety truth |
| Evaluation artifact | AgentCore collector/endpoint | 响应/外部保存 | `server.py:runtime_evaluation_execute_v2_endpoint` | Release decision 属于 AgentEvalOps |
| Release Decision | AgentEvalOps（仓外） | 仓外 | AgentCore 无 release-decision owner | 本仓不得冒领 |

未发现生产路径上两个组件同时拥有同一核心 Truth；local `RunRegistry` 与 durable control 看似重叠，但源码明确前者仅为 wake-up acceleration，因此不是 `AUTHORITY_CONFLICT`。SQLite memory 实现与 PostgreSQL memory 同时存在于仓库，但 production composition 只装配 PostgreSQL，属于 legacy/test surface，不构成生产 Authority 冲突。

# 4. IMPLEMENTATION_EVIDENCE

| Capability | Status | Source Evidence | Test Evidence | Interview-safe Claim |
|---|---|---|---|---|
| Coordinated Runtime / Agent Loop | IMPLEMENTED_AND_TESTED | `runtime_factory.py`; `run_coordinator.py` | `test_runtime_full_e2e.py` | 生产 HTTP 统一进入 coordinated scope |
| State Machine | IMPLEMENTED_AND_TESTED | `state_machine.py:AgentStateMachine` | `test_state_machine.py` | 不只是 Enum，事件转换会 clone/guard/commit |
| Planner / Compiler | IMPLEMENTED_AND_TESTED | `planning_model_adapter.py`; `plan_compiler.py` | `test_multi_agent_planning.py` | 动态决策编译成 frozen typed Plan |
| Multi-Agent / Scheduler / Synthesis | IMPLEMENTED_AND_TESTED | `parallel_execution.py`; `synthesis.py` | `test_multi_agent_execution.py` | 有界并行 specialist，依赖完成后 synthesis |
| RunContext / Budget / Absolute Deadline | IMPLEMENTED_AND_TESTED | `context.py`; `budget.py` | `test_runtime_context.py`; `test_budget.py` | Deadline/Budget 分离 |
| Cancellation | IMPLEMENTED_AND_TESTED | `run_control.py`; `cancellation.py` | `test_stage7_wp1_run_control.py` | durable intent 跨实例；远端计算终止不保证 |
| Retry / Fallback / Circuit | IMPLEMENTED_AND_TESTED | `model_invocation.py`; `retry.py`; `circuit_breaker.py` | `test_wp4_provider_streaming.py` | 首个接受输出前才 retry/fallback |
| Native SSE / Typed Delta | IMPLEMENTED_AND_TESTED | `llm_engine.py:_SSEDecoder,agenerate` | `test_wp4_provider_streaming.py` | 真正 async HTTP SSE，不是伪切片 |
| Output Barrier | IMPLEMENTED_AND_TESTED | `output_gate.py` | `test_output_gate.py` | per-run single-use、最终来源唯一、at-most-once attempt |
| PostgreSQL Run Control / Lease / Fencing | IMPLEMENTED_AND_TESTED | `run_control.py`; migration `0007` | `test_stage7_wp1_run_control.py` | 数据库 lease + 单调 fencing，不是共识协议 |
| Journal-first / terminal | IMPLEMENTED_AND_TESTED | `event_journal.py`; `finalize_terminal` | `test_event_journal_integration.py` | terminal 与 control close 同事务 |
| Run Recovery | IMPLEMENTED_PARTIALLY | `recovery_validation.py`; snapshots/journal | `test_recovery_integration.py` | 有校验/恢复决策，不承诺任意中断透明续跑 |
| ToolRegistry / Typed Validation | IMPLEMENTED_AND_TESTED | `tool_registry.py`; `tool_contract.py` | `test_tool_execution_integration.py` | 7 个内置生产工具，MCP 可追加 |
| Tool Governance | IMPLEMENTED_AND_TESTED | `tool_governance.py` | `test_tool_governance.py` | code-owned risk/policy，不信任模型或 MCP |
| HITL / Durable Approval / Claim | IMPLEMENTED_AND_TESTED | `durable_approval.py`; migration `0008` | `test_stage7_wp2_durable_hitl.py` | 跨实例、first-wins、binding + fence |
| Side-effect Ledger / UNKNOWN / Reconciliation | IMPLEMENTED_AND_TESTED | `tool_idempotency.py`; migration `0010` | `test_stage7_wp5_tool_side_effect_reconciliation.py` | provider-specific reconciliation |
| Generic Exactly-once | NOT_IMPLEMENTED | 无通用 provider transaction | 对应测试验证“不重放/保持 UNKNOWN” | 只能说本地控制面 at-most-once + 幂等/对账边界 |
| Dense RAG | IMPLEMENTED_AND_TESTED | `vector_db_manager.py` | `test_vector_db_manager.py` | Chroma dense retrieval |
| BM25 | IMPLEMENTED_AND_TESTED | `bm25_sparse_index.py` | `test_bm25_sparse_index.py` | 生产 artifact 可构建/校验 |
| Hybrid RRF | IMPLEMENTED_AND_TESTED | `hybrid_retrieval_adapter.py` | `test_hybrid_rrf_retriever.py` | 同 generation 双通道 RRF |
| Immutable Index Generation | IMPLEMENTED_AND_TESTED | `retrieval_index_provenance.py` | `test_production_build.py` | active descriptor 原子切换 |
| Cross-Encoder | IMPLEMENTED_PARTIALLY | `cross_encoder_reranker.py` | `test_cross_encoder_reranker.py` | 存在组件，不是默认 production strategy |
| Memory | IMPLEMENTED_AND_TESTED | `core/persistence/memory.py`; `memory_retrieval.py` | `test_stage6_wp1_postgres_memory.py` | conversation + semantic + episodic + project memory |
| MCP stdio | IMPLEMENTED_AND_TESTED | `mcp/client.py` | `test_mcp_runtime_integration.py` | initialize/list/call/close |
| MCP HTTP / Streamable HTTP | NOT_IMPLEMENTED | `mcp/client.py` 明示不实现 | 无 | 不可声称支持 |
| MCP Reconnect / Generation / Schema Revalidation | IMPLEMENTED_AND_TESTED | `mcp/lifecycle.py` | `test_mcp_resilient_lifecycle.py` | bounded backoff+jitter+singleflight |
| Redis | IMPLEMENTED_AND_TESTED | `redis_service.py` | `test_stage6_wp3_redis.py` | cache/admission，非 Truth |
| Kafka / Outbox | IMPLEMENTED_AND_TESTED | `outbox_publisher.py`; `kafka_event_sink.py` | `test_stage6_wp5_kafka_integration.py` | evaluation jobs transport，非 Runtime Journal |
| Authentication / SERVICE / Scope | IMPLEMENTED_AND_TESTED | `auth.py`; migration `0009` | `test_stage6_wp2_auth.py` | HUMAN/SERVICE 分离，service 窄 scope |
| Observability | IMPLEMENTED_AND_TESTED | `observability.py`; runtime tracing | `test_stage6_wp6_observability_foundation.py` | Prometheus + optional OTLP + safe projections |
| Graceful Shutdown | IMPLEMENTED_AND_TESTED | `application_services.py`; `shutdown.py` | `test_graceful_shutdown.py` | lifespan owner 有界关闭 application resources |
| AgentEvalOps HTTP Evaluation Target | IMPLEMENTED_AND_TESTED | `server.py:evaluation_execute_v2` | `test_runtime_evaluation_execute_endpoint.py` | 本仓 target 已实现；跨仓当前部署未验收 |
| Legacy Cleanup | IMPLEMENTED_PARTIALLY | canonical routes 无 legacy fallback；SQLite/compat code 尚存 | `test_runtime_full_e2e.py` | 生产主路径已清理，仓库并非零 legacy |

# 5. INTERVIEW QUESTION BANK

以下 44 题均以当前源码为边界。

## Q01 [P0] AgentCore 到底是什么？
### 面试官为什么会问
检验项目定位是否稳定。
### 推荐回答
我把 AgentCore 定位为 Agent Runtime、Harness 和 AI Backend 的组合平台。它从本地聊天应用演进而来，但当前生产事实中心是 FastAPI 服务和 Coordinated Runtime，负责 Run 生命周期、计划调度、模型与工具安全边界、持久化控制面以及可观测性。
### Source Evidence
`server.py:lifespan`; `core/runtime/runtime_factory.py:CoordinatedRuntimeFactory`; `tests/test_runtime_full_e2e.py:test_default_composition_root_model_output_and_terminal_matrix`
### Claim Boundary
`CAN_SAY:` 服务端 Agent 平台。  
`CANNOT_SAY:` 纯端侧 Agent 或通用分布式工作流引擎。
### Likely Follow-up
Canonical path 是什么？为何仍有 PyQt6？

## Q02 [P0] 一次 Run 如何走到终态？
### 面试官为什么会问
检查是否真正理解主链。
### 推荐回答
HTTP 先认证并绑定 ownership，ChatService 创建 coordinated scope，Factory 从 PostgreSQL claim Run lease，RunCoordinator 冻结计划并驱动 Scheduler，步骤结果写入 StepResultStore，唯一 final step 经 OutputGate 发布；最后 fenced terminal append 与 run-control close 在同一数据库事务完成。
### Source Evidence
`server.py:chat_endpoint`; `runtime_factory.py:create_run_scope`; `run_control.py:finalize_terminal`; `tests/test_stage7_wp1_run_control.py:test_terminal_append_and_control_close_share_one_transaction`
### Claim Boundary
`CAN_SAY:` terminal 有 durable 原子边界。  
`CANNOT_SAY:` 整个 Run 是一个数据库事务。
### Likely Follow-up
EventChannel 失败怎么办？

## Q03 [P0] Agent Loop 与 Runtime 的关系？
### 面试官为什么会问
区分业务推理与执行治理。
### 推荐回答
Agent Loop 是模型、检索、工具和 continuation 的业务循环；Runtime 是外层执行控制面，拥有 RunContext、状态机、调度、取消、deadline、事件和终态。AgentRouter 可以做一次 agent completion，但不能自行决定 Run Truth。
### Source Evidence
`core/agent_router.py:AgentRouter`; `run_coordinator.py:RunCoordinator`; `tests/test_runtime_full_e2e.py`
### Claim Boundary
`CAN_SAY:` Runtime 包住并约束 Agent Loop。  
`CANNOT_SAY:` Router 拥有 Run terminal。
### Likely Follow-up
为何不只串几个函数？

## Q04 [P0] State Machine 只是 Enum 吗？
### 面试官为什么会问
验证状态治理深度。
### 推荐回答
不是。Enum 只定义状态值，`AgentStateMachine` 对 run/step event 做时间、前置状态和活动步骤 guard，先 clone candidate，验证后再 commit，非法转换抛 typed error。
### Source Evidence
`state_machine.py:AgentStateMachine`; `tests/test_state_machine.py`
### Claim Boundary
`CAN_SAY:` 有严格 transition guard。  
`CANNOT_SAY:` 是数据库驱动的全局状态机。
### Likely Follow-up
状态与 Journal 谁是 Truth？

## Q05 [P0] Planner、Scheduler、Runtime 如何分工？
### 面试官为什么会问
考查职责边界。
### 推荐回答
Planner 产出决策，PlanCompiler 把它变成经过校验的 frozen Plan；Scheduler 只根据依赖和状态 claim ready step；Runtime/Coordinator 拥有生命周期、事件、终态和清理。三者不会互相冒充 Owner。
### Source Evidence
`plan_compiler.py:PlanCompiler`; `scheduler.py:SerialScheduler`; `run_coordinator.py`
### Claim Boundary
`CAN_SAY:` 计划、调度、生命周期分离。  
`CANNOT_SAY:` Planner 直接执行工具。
### Likely Follow-up
动态计划何时冻结？

## Q06 [P0] 多 Agent 如何并发？
### 面试官为什么会问
判断 multi-agent 是否只是标签。
### 推荐回答
编译后的 Plan 显式表达 specialist 依赖；Scheduler 找 ready steps，ParallelExecutor 按 max parallelism 并发执行，synthesis 等依赖结果全部可读后才运行。任何 specialist 失败、取消或超时都会阻断 synthesis。
### Source Evidence
`parallel_execution.py`; `synthesis.py`; `tests/test_multi_agent_execution.py:test_shape3_specialists_overlap_and_synthesis_waits`
### Claim Boundary
`CAN_SAY:` 单 Run 内有界并行。  
`CANNOT_SAY:` 任意自治 Agent 群体或跨节点 task stealing。
### Likely Follow-up
结果如何传给 synthesis？

## Q07 [P0] Output Gate 为什么存在？
### 面试官为什么会问
输出重复是流式系统常见问题。
### 推荐回答
它把用户可见输出从普通 step result 中隔离，只允许 frozen Plan 中唯一 final step、合法 claim、SUCCEEDED 且可读的结果发布，并且每个 Run 只尝试一次。部分持久化失败会标为 OUTCOME_UNKNOWN，而不是盲目重发。
### Source Evidence
`output_gate.py:OutputGate`; `tests/test_output_gate.py:test_concurrent_duplicate_attempts_allow_only_one_publish`
### Claim Boundary
`CAN_SAY:` at-most-once publish attempt。  
`CANNOT_SAY:` 网络端用户一定只看到一次字节。
### Likely Follow-up
流式 delta 如何进入 gate？

## Q08 [P0] 多实例下谁拥有 Run？
### 面试官为什么会问
检验是否把内存 registry 当 Truth。
### 推荐回答
PostgreSQL 的 `runtime_run_control` 由 `DurableRunControlService` 唯一决定 owner、lease 和 fencing token。`RunRegistry` 只保存本实例活跃 handle，用于快速 cancel/wakeup，不是跨实例 Truth。
### Source Evidence
`run_control.py:DurableRunControlService`; migration `0007`; `tests/test_stage7_wp1_run_control.py:test_competing_claim_and_stale_release_do_not_replace_owner`
### Claim Boundary
`CAN_SAY:` 数据库条件更新实现 owner 竞争。  
`CANNOT_SAY:` 实现了 Raft/Paxos。
### Likely Follow-up
请求落到错误实例怎么办？

## Q09 [P0] Lease 和 Fencing 区别？
### 面试官为什么会问
常见分布式正确性考点。
### 推荐回答
Lease 表示 owner 在一段时间内有执行资格；fencing token 是每次 takeover 单调递增的代际号。即使旧 owner 没及时停止，所有关键持久化 mutation 仍要匹配当前 token，因此 stale executor 不能写 terminal、approval claim 或 side-effect state。
### Source Evidence
`run_control.py:_takeover_locked,assert_current`; `tests/test_stage7_wp1_run_control.py:test_takeover_increments_fence_and_stale_renew_fails`
### Claim Boundary
`CAN_SAY:` 防陈旧写。  
`CANNOT_SAY:` 消除所有外部 Provider 并发副作用。
### Likely Follow-up
Provider 不支持 fence 怎么办？

## Q10 [P0] Cancel 如何跨实例？
### 面试官为什么会问
检查控制面是否 durable。
### 推荐回答
Cancel endpoint 先把幂等 CANCEL command 写入 PostgreSQL，再尝试本地 registry 唤醒；scope 的 control maintainer 会轮询 durable intent 并触发 local cancellation token。因此 registry miss 不丢取消意图。
### Source Evidence
`server.py:cancel_run_endpoint`; `runtime_factory.py:_maintain_durable_control`; `tests/test_stage7_wp1_run_control.py:test_production_scope_observes_cross_instance_cancel`
### Claim Boundary
`CAN_SAY:` 跨实例取消意图 durable。  
`CANNOT_SAY:` 能保证远端模型立刻停止计算。
### Likely Follow-up
Cancel 与 terminal race 谁赢？

## Q11 [P0] Provider Streaming 是真实 SSE 吗？
### 面试官为什么会问
很多项目只是把完整结果切片。
### 推荐回答
是。RemoteLLMEngine 使用 application-scoped `httpx.AsyncClient.stream()`，增量解析跨网络 chunk 的 SSE framing，并输出 provider-neutral 的 TextDelta、ToolCallDelta、UsageDelta 和 Finish。
### Source Evidence
`core/llm_engine.py:_SSEDecoder,RemoteLLMEngine.agenerate`; `tests/test_wp4_provider_streaming.py:test_real_http_sse_fragmented_and_multiple_events_are_normalized`
### Claim Boundary
`CAN_SAY:` native async SSE。  
`CANNOT_SAY:` 所有 provider 协议都支持。
### Likely Follow-up
非法 UTF-8 或半截事件如何处理？

## Q12 [P0] Retry/Fallback 的 Owner 是谁？
### 面试官为什么会问
避免 Adapter 偷重试造成重复。
### 推荐回答
Owner 是 `ModelInvocationRouter` 配合 `RetryExecutor` 和 routing policy。Provider Adapter 只报告 typed failure 与 output_started；Router 结合绝对 deadline、预算、circuit 和候选模型决定 retry 或 fallback。
### Source Evidence
`model_invocation.py:ModelInvocationRouter`; `retry.py`; `tests/test_wp4_provider_streaming.py:test_async_router_retries_before_first_accepted_output`
### Claim Boundary
`CAN_SAY:` Runtime 统一重试。  
`CANNOT_SAY:` 任意失败都自动重试。
### Likely Follow-up
Rate limit 怎么处理？

## Q13 [P0] 为什么 output-started 后不 fallback？
### 面试官为什么会问
关注重复/混合输出风险。
### 推荐回答
一旦 Runtime 接受了首段输出，换 Provider 可能把两份答案拼接或重复副作用。Router 因此把 Runtime acceptance 作为 retry barrier，mid-stream failure fail-stop，并保留 typed error。
### Source Evidence
`model_routing.py:can_fallback`; `model_invocation.py:ainvoke`; `tests/test_wp4_provider_streaming.py:test_async_router_fail_stops_after_accepted_output_without_fallback`
### Claim Boundary
`CAN_SAY:` 首段接受后禁止 fallback。  
`CANNOT_SAY:` 支持断点续传。
### Likely Follow-up
Provider 自报 output_started 是否可信？

## Q14 [P0] 模型选 Tool 后为何不能直接执行？
### 面试官为什么会问
考查 trust boundary。
### 推荐回答
模型输出是不可信 proposal。AgentCore 先做 registry identity 和 typed argument validation，再做 code-owned risk/governance；高风险操作还要 durable approval 和 fenced execution claim，最后 ToolExecutionService 才能跨 Provider boundary。
### Source Evidence
`agent_router.py:_build_valid_tool_invocation,_run_tool`; `tool_execution.py:_execute_impl`; `tests/test_tool_execution_integration.py`
### Claim Boundary
`CAN_SAY:` 模型没有执行 Authority。  
`CANNOT_SAY:` prompt 本身能保证工具安全。
### Likely Follow-up
验证失败是否可修复？

## Q15 [P0] 为什么 Validation 必须在 Governance 前？
### 面试官为什么会问
风险判断依赖规范化参数。
### 推荐回答
Governance 要基于稳定、typed 的 invocation 做资源、side-effect 和 risk 判断；如果先授权原始字符串，再修复参数，批准的可能不是实际执行内容。实现只允许一次有界 repair，然后重新 validation。
### Source Evidence
`agent_router.py:_build_valid_tool_invocation`; `tests/test_tool_execution_integration.py:test_bounded_validation_repair_revalidates_once`
### Claim Boundary
`CAN_SAY:` 授权绑定 validated invocation。  
`CANNOT_SAY:` 任意 malformed 参数都能自动修好。
### Likely Follow-up
binding digest 包含什么？

## Q16 [P0] Approval 为什么 durable 且 first-wins？
### 面试官为什么会问
多实例人工审批的核心语义。
### 推荐回答
审批可能跨请求、跨实例、跨 worker 生命周期，所以不能只放 Future。PostgreSQL row 记录 PENDING/APPROVED/REJECTED/INVALIDATED，CAS 保证第一个有效决定胜出，同向重复幂等、反向决定冲突。
### Source Evidence
`durable_approval.py:decide`; migration `0008`; `tests/test_stage7_wp2_durable_hitl.py:test_reject_first_wins_and_duplicate_is_idempotent`
### Claim Boundary
`CAN_SAY:` durable first-wins。  
`CANNOT_SAY:` 审批等同执行一次。
### Likely Follow-up
controller 丢失后怎么办？

## Q17 [P0] Approval A 如何防止执行 Invocation B？
### 面试官为什么会问
防止 TOCTOU 与替换攻击。
### 推荐回答
审批记录和 HTTP 决策都带 canonical invocation binding digest；claim_execution 再校验 approval_id、run_id、binding、APPROVED 状态和当前 fencing token，任一不匹配 fail closed。
### Source Evidence
`approval.py:compute_invocation_binding_digest`; `durable_approval.py:claim_execution`; `tests/test_stage7_wp2_durable_hitl.py:test_decision_race_and_binding_mismatch_fail_closed`
### Claim Boundary
`CAN_SAY:` 精确 invocation binding。  
`CANNOT_SAY:` 只按 tool name 审批。
### Likely Follow-up
Execution Claim 有何额外价值？

## Q18 [P0] STARTED 为什么必须在 Provider Call 前落库？
### 面试官为什么会问
副作用恢复的关键窗口。
### 推荐回答
PREPARED 只说明本地意图已建立；Provider 前先写 STARTED，崩溃恢复时才能知道调用可能已经越过副作用边界。若调用后才写，就会把“可能已执行”误判成“尚未执行”并重放。
### Source Evidence
`tool_idempotency.py:start`; `tool_execution.py:ToolAttemptExecutor`; `tests/test_stage7_wp5_tool_side_effect_reconciliation.py:test_durable_invocation_state_machine_and_stable_identity`
### Claim Boundary
`CAN_SAY:` 缩小并显式化不确定窗口。  
`CANNOT_SAY:` 消除网络双写窗口。
### Likely Follow-up
本地 STARTED 写成功后进程崩溃怎么办？

## Q19 [P0] Timeout 为什么可能是 UNKNOWN？
### 面试官为什么会问
检验副作用语义是否成熟。
### 推荐回答
如果请求已经越过 Provider boundary，timeout 只说明本地没拿到确定响应，不能证明对方没提交。AgentCore 将这类结果落为 UNKNOWN，阻止 generic retry，等待 provider-specific reconciliation。
### Source Evidence
`tool_execution.py:AttemptSideEffectTracker`; `tool_idempotency.py:unknown`; `tests/test_stage7_wp5_tool_side_effect_reconciliation.py:test_durable_provider_failure_is_unknown_and_not_retried`
### Claim Boundary
`CAN_SAY:` 区分失败与未知。  
`CANNOT_SAY:` timeout 等于 NOT_COMMITTED。
### Likely Follow-up
没有 status API 怎么办？

## Q20 [P0] 是否实现 generic exactly-once？
### 面试官为什么会问
这是最容易夸大的点。
### 推荐回答
没有。我们实现的是 fenced local authority、唯一 execution claim、durable invocation ledger、幂等键和 provider-specific reconciliation。外部系统没有幂等/status 合同时，UNKNOWN 会保留并转人工，不能承诺通用 exactly-once。
### Source Evidence
`tool_idempotency.py`; `tool_recovery.py`; `tests/test_stage7_wp5_tool_side_effect_reconciliation.py:test_missing_provider_reconciler_fails_closed_and_keeps_unknown`
### Claim Boundary
`CAN_SAY:` 有条件的 effectively-once。  
`CANNOT_SAY:` 任意外部工具 exactly-once。
### Likely Follow-up
与 Outbox 的 exactly-once 有何区别？

## Q21 [P0] MCP 为什么不能成为 Safety Authority？
### 面试官为什么会问
外部 metadata 不可信。
### 推荐回答
MCP 只提供能力发现和调用协议。remote read-only hint、schema 和 description 都要映射到本地 canonical registration 与 code-owned policy；实际执行仍走相同的 validation、governance、approval、claim 和 side-effect ledger。
### Source Evidence
`mcp/registration.py`; `server.py:_build_tool_governance`; `tests/test_mcp_runtime_integration.py:test_read_only_hint_cannot_override_local_policy_or_spec`
### Claim Boundary
`CAN_SAY:` MCP 复用现有 Tool Runtime。  
`CANNOT_SAY:` MCP server 决定风险等级。
### Likely Follow-up
schema drift 怎么处理？

## Q22 [P0] 当前 MCP Transport 是什么？
### 面试官为什么会问
防止把协议生态能力算成项目能力。
### 推荐回答
当前只有本地 subprocess stdio JSON-RPC，支持 initialize、tools/list、tools/call 和 close。源码明确不实现 Streamable HTTP/SSE，也没有多 transport framework。
### Source Evidence
`mcp/client.py:StdioMcpClient`; `tests/test_mcp_client.py`
### Claim Boundary
`CAN_SAY:` stdio。  
`CANNOT_SAY:` MCP HTTP。
### Likely Follow-up
子进程由谁关闭？

## Q23 [P0] MCP Reconnect 如何保证安全？
### 面试官为什么会问
重连容易把新 session 当旧 session。
### 推荐回答
Application-scoped lifecycle 是唯一重连 owner，按 server singleflight 做 bounded exponential backoff+jitter。新 client 完成 initialize/list 后，还要校验 remote identity 和 frozen tool/schema digest，合格才以新 generation 发布；旧 invocation 绑定不会自动换 session。
### Source Evidence
`mcp/lifecycle.py:McpIntegrationComponent`; `tests/test_mcp_resilient_lifecycle.py:test_reconnect_preserves_frozen_registry_policy_and_spec_identity`
### Claim Boundary
`CAN_SAY:` generation-safe reconnect。  
`CANNOT_SAY:` dynamic hot reload。
### Likely Follow-up
remote version drift 是否允许？

## Q24 [P0] MCP 重连后能重放 Tool 吗？
### 面试官为什么会问
连接恢复与业务结果恢复常被混淆。
### 推荐回答
只读调用在满足 deadline/cancellation 条件时最多对新 generation replay 一次；side-effect 调用 disconnect 后保持 UNKNOWN，不因连接恢复而重放。连接健康不代表旧操作结果已知。
### Source Evidence
`mcp/lifecycle.py`; `tests/test_mcp_resilient_lifecycle.py:test_read_only_disconnect_replays_once_on_new_generation`; `...:test_side_effect_disconnect_is_not_replayed`
### Claim Boundary
`CAN_SAY:` 分类 replay。  
`CANNOT_SAY:` reconnect 自动恢复所有调用。
### Likely Follow-up
为什么最多一次？

## Q25 [P0] RAG 当前生产策略是什么？
### 面试官为什么会问
检验是否混淆实验组件与生产默认。
### 推荐回答
策略由 `RetrievalStrategy` 配置选择 BASELINE 或 HYBRID_RRF。Baseline 使用 Dense；Hybrid 使用同一 immutable generation 的 Dense merged channel 与 BM25 channel，再以固定 RRF 合并。cross-encoder 有实现和测试，但不是当前默认 production stage。
### Source Evidence
`core/settings.py:RetrievalStrategy`; `hybrid_retrieval_adapter.py`; `tests/test_hybrid_strategy_startup.py`
### Claim Boundary
`CAN_SAY:` Dense/BM25/Hybrid RRF 可生产装配。  
`CANNOT_SAY:` 默认 cross-encoder rerank。
### Likely Follow-up
为什么选择 RRF？

## Q26 [P0] Index Generation 为什么 immutable？
### 面试官为什么会问
关注检索可复现性。
### 推荐回答
Dense 和 BM25 必须共享 generation_id、chunk manifest 和 provenance digest；构建完成并验证后才用原子 `os.replace` 切换 active descriptor。失败构建不会污染旧 active generation。
### Source Evidence
`retrieval_index_provenance.py:publish_active_descriptor`; `production_build.py`; `tests/test_production_build.py:test_failed_build_preserves_old_active_descriptor`
### Claim Boundary
`CAN_SAY:` 本地 artifact immutable generation。  
`CANNOT_SAY:` 分布式索引发布协议。
### Likely Follow-up
Cache key 为什么要含 generation？

## Q27 [P0] Memory 当前是什么能力？
### 面试官为什么会问
区分聊天历史、语义记忆和运行时 Truth。
### 推荐回答
生产装配使用 PostgreSQL：conversation/message、private semantic、episodic 和 project semantic memory 分别由明确 store/service 管理，检索注入顺序固定。Memory 是上下文和长期知识，不是 Run owner、approval 或 side-effect Truth。
### Source Evidence
`server.py:lifespan` memory assembly; `core/persistence/memory.py`; `memory_retrieval.py:MemoryRetrievalService`
### Claim Boundary
`CAN_SAY:` PostgreSQL durable memory。  
`CANNOT_SAY:` SQLite 是当前生产 authority。
### Likely Follow-up
项目记忆如何授权？

## Q28 [P0] PostgreSQL、Redis、Kafka 各负责什么？
### 面试官为什么会问
检查基础设施边界。
### 推荐回答
PostgreSQL 是业务与 Runtime Authority，持有 journal、run control、approval、tool ledger、memory、identity、jobs/outbox。Redis 只做 RAG cache 和 token-bucket admission；Kafka 传输 evaluation job 事件，可靠性由 PostgreSQL transactional outbox、claim/fence 和 consumer dedup 支撑。
### Source Evidence
`server.py:lifespan`; `redis_service.py`; `kafka_event_sink.py`; migrations `0001-0010`
### Claim Boundary
`CAN_SAY:` 三者职责分离。  
`CANNOT_SAY:` Redis/Kafka 是 Run Truth。
### Likely Follow-up
Redis 挂了系统如何表现？

## Q29 [P0] Outbox 解决什么？
### 面试官为什么会问
数据库与消息队列双写一致性。
### 推荐回答
提交 evaluation job 时，job row 和最小 outbox intent 在一个 PostgreSQL 事务写入；publisher 用 claim token/deadline 取出，Kafka ACK 后才标 PUBLISHED。Broker 失败时 intent 保持 pending，可 reclaim，避免数据库成功但消息丢失。
### Source Evidence
`core/evaluation_jobs.py`; `outbox_publisher.py`; migration `0004`; `tests/test_stage6_wp4_job_outbox.py:test_submission_is_atomic_and_payload_is_minimal`
### Claim Boundary
`CAN_SAY:` at-least-once transport + consumer dedup。  
`CANNOT_SAY:` Kafka 与 PostgreSQL 2PC。
### Likely Follow-up
重复消息怎么处理？

## Q30 [P0] Auth 为什么区分 HUMAN 和 SERVICE？
### 面试官为什么会问
服务身份不应借用管理员语义。
### 推荐回答
Human 通过角色和对象 ownership 使用交互 API；Service principal 只能带数据库配置允许的窄 scopes，例如 `evaluation:execute`。Service 不能借 admin role 越权访问其他 owner 的对象。
### Source Evidence
`auth.py:Principal,require_scope,require_owned`; migration `0009`; `tests/test_stage6_wp2_auth.py:test_service_principal_cannot_use_admin_override_for_foreign_ownership`
### Claim Boundary
`CAN_SAY:` JWT + DB principal lookup + scope/ownership。  
`CANNOT_SAY:` 完整 OAuth authorization server。
### Likely Follow-up
Token algorithm confusion 如何防？

## Q31 [P0] lifespan 为什么是 Composition Root？
### 面试官为什么会问
考查资源生命周期。
### 推荐回答
它按依赖顺序构造 database、Redis、memory、model clients、MCP、registry/policy、journal、runtime services 和 factory；失败时 initialization stack 回滚，shutdown 时 coordinator 先停止 admission、取消/收拢 Runs，再有界关闭 application-scoped clients。
### Source Evidence
`server.py:lifespan`; `application_services.py:RuntimeInitializationStack,ApplicationRuntimeServices`; `tests/test_runtime_lifespan.py`
### Claim Boundary
`CAN_SAY:` application owner 清晰。  
`CANNOT_SAY:` 每个 Run 自己关闭共享 client。
### Likely Follow-up
关闭顺序是什么？

## Q32 [P1] RunContext 有什么？
### 面试官为什么会问
检查横切控制如何下传。
### 推荐回答
RunContext 聚合 run/session/trace identity、绝对 Deadline、CancellationToken、budget 和 durable lease，并提供 ownership validation。下游模型、工具和 retrieval 都消费同一个控制上下文。
### Source Evidence
`context.py:RunContext,create_run_context`; `tests/test_runtime_context.py`
### Claim Boundary
`CAN_SAY:` request/run-scoped 控制载体。  
`CANNOT_SAY:` 持久化完整业务状态。
### Likely Follow-up
为什么用 absolute deadline？

## Q33 [P1] Absolute Deadline 如何避免层层续命？
### 面试官为什么会问
相对 timeout 容易累计超时。
### 推荐回答
入口计算一次绝对截止时间；每个下游操作只取 remaining budget，并用它约束连接、读流、backoff 和 tool timeout。重试不会重新获得完整 timeout。
### Source Evidence
`context.py:Deadline`; `llm_engine.py:_remaining_budget`; `tests/test_wp4_provider_streaming.py:test_total_invocation_cap_is_not_renewed_per_stream_read`
### Claim Boundary
`CAN_SAY:` 端到端时间上界。  
`CANNOT_SAY:` 能终止不合作的远端计算。
### Likely Follow-up
blocking tool 怎么办？

## Q34 [P1] Circuit Breaker 是全局的吗？
### 面试官为什么会问
状态隔离与恢复语义。
### 推荐回答
Registry 按 breaker key 管理 model circuit，状态在 application lifetime 内共享；permit 明确记录 success/failure/indeterminate，HALF_OPEN 有并发上限。它是进程内保护，不是跨实例 durable circuit。
### Source Evidence
`circuit_breaker.py:ModelCircuitBreakerRegistry`; `tests/test_model_circuit_breaker.py`
### Claim Boundary
`CAN_SAY:` application-scoped circuit。  
`CANNOT_SAY:` 集群全局 circuit。
### Likely Follow-up
哪些失败计数？

## Q35 [P1] Tool Result 如何回到模型？
### 面试官为什么会问
确认 tool calling 闭环。
### 推荐回答
ToolExecutionService 返回 typed result，AgentRouter 将结果包装成 provider/native tool response 或受控文本上下文，再执行 model continuation。Result 的 content type、大小、错误码和 side-effect evidence 都在 Runtime contract 中，而不是直接拼原始异常。
### Source Evidence
`agent_router.py` native continuation path; `tool_contract.py:ToolExecutionResult`; `tests/test_tool_execution_integration.py:test_native_tool_result_wrapper_preserves_function_call_protocol`
### Claim Boundary
`CAN_SAY:` typed continuation。  
`CANNOT_SAY:` 原始 provider payload 全量回显。
### Likely Follow-up
输出截断如何处理？

## Q36 [P1] 如何防目录穿越？
### 面试官为什么会问
文件工具是高风险入口。
### 推荐回答
Workspace 工具只接收相对路径，Adapter resolve 后验证仍在 Demo Root；`..`、绝对路径和越界路径 fail closed。旧任意路径读工具则走独立 allowlisted read-root authorization。
### Source Evidence
`workspace_tool_adapters.py`; `resource_authorization.py`; `tests/test_phase8_wp2_workspace_tools.py`
### Claim Boundary
`CAN_SAY:` 两类文件工具有不同资源边界。  
`CANNOT_SAY:` 任意系统文件访问。
### Likely Follow-up
symlink 如何处理？

## Q37 [P1] Recovery 到什么程度？
### 面试官为什么会问
恢复能力容易夸大。
### 推荐回答
系统有 snapshot、journal tail validation、checkpoint 和 recovery decision，可识别 terminal、部分发布与 tool completion gap；但它不是任意位置透明续跑，副作用 UNKNOWN 仍需要专用对账或人工处理。
### Source Evidence
`recovery_validation.py`; `journal_tail_reducer.py`; `tests/test_recovery_integration.py`
### Claim Boundary
`CAN_SAY:` evidence-based recovery validation。  
`CANNOT_SAY:` universal replay/resume。
### Likely Follow-up
Snapshot 是 Truth 吗？

## Q38 [P1] Observability 会成为业务 Truth 吗？
### 面试官为什么会问
投影不可反向决定状态。
### 推荐回答
不会。Journal 是持久化事件证据，structured log、metrics、trace 和 AgentEvalOps export 都是异步/安全投影；它们失败可以降级，但不能制造 terminal、approval 或 execution claim。
### Source Evidence
`observability_dispatcher.py`; `observability.py`; `tests/test_runtime_report_authority.py`
### Claim Boundary
`CAN_SAY:` Prometheus/OpenTelemetry 可观测。  
`CANNOT_SAY:` trace 是业务状态源。
### Likely Follow-up
敏感字段如何控制？

## Q39 [P1] AgentCore 与 AgentEvalOps 怎么分工？
### 面试官为什么会问
跨仓能力归属。
### 推荐回答
AgentCore 是被测生产 Target，执行真实 coordinated run 并产生 bounded artifact/provenance；AgentEvalOps 负责数据集、评分、报告和 release decision，通过 SERVICE JWT 调用 evaluation-v2。评测平台能力不能算成 AgentCore 内部模块。
### Source Evidence
`server.py:runtime_evaluation_execute_v2_endpoint`; `auth.py:EVALUATION_EXECUTE_SCOPE`; `tests/test_runtime_evaluation_execute_endpoint.py`
### Claim Boundary
`CAN_SAY:` authenticated HTTP contract 存在。  
`CANNOT_SAY:` 本轮证明跨仓部署 E2E 当前通过。
### Likely Follow-up
为什么不用进程内调用？

## Q40 [P1] SQLite 是否仍是 Runtime Authority？
### 面试官为什么会问
仓库里仍能搜索到 sqlite3。
### 推荐回答
不是当前 production composition 的 Authority。`server.py::lifespan()` 装配 PostgreSQL memory、journal、snapshot、run control 等；SQLite 类仍作为历史实现、迁移与测试 surface 存在，因此我不会说“仓库已经完全删除 SQLite”。
### Source Evidence
`server.py:537-547`; `core/memory_manager.py`; `core/advanced_memory.py`; `tests/test_stage6_wp1_postgres_memory.py`
### Claim Boundary
`CAN_SAY:` 生产 Authority 已迁到 PostgreSQL。  
`CANNOT_SAY:` 源码零 SQLite。
### Likely Follow-up
为什么保留旧实现？

## Q41 [P2] Journal 与 EventChannel 的关系？
### 面试官为什么会问
持久化和实时交付的双边界。
### 推荐回答
Emitter 先形成 typed event，并由 Journal 分配 durable sequence，再投递 EventChannel 给实时消费者。若 journal 已写但 channel 投递失败，结果是 partial publication/unknown，而不是回滚已经持久化的事实或重跑业务。
### Source Evidence
`event_emitter.py`; `event_journal.py`; `tests/test_event_partial_publication.py`
### Claim Boundary
`CAN_SAY:` journal-first publication。  
`CANNOT_SAY:` channel 是 durable queue。
### Likely Follow-up
消费者 checkpoint 存哪里？

## Q42 [P2] RRF 为什么比直接混分更稳？
### 面试官为什么会问
检索工程取舍。
### 推荐回答
Dense cosine score 与 BM25 score 尺度不同，直接加权要求额外标定。RRF 只使用各通道 rank，以固定 `1/(k+rank)` 融合，并保留每个候选的双通道 provenance，工程上更可审计、对 score scale 更鲁棒。
### Source Evidence
`hybrid_rrf_retriever.py:HybridRrfRetriever.fuse`; `tests/test_hybrid_rrf_retriever.py:test_two_channels_same_ranking_uses_exact_formula_and_rank_starts_at_one`
### Claim Boundary
`CAN_SAY:` 固定 RRF 实现。  
`CANNOT_SAY:` 对所有数据集都优于 cross-encoder。
### Likely Follow-up
RRF k 为什么是 60？

## Q43 [P2] Kafka 是否是 Agent Run 事件总线？
### 面试官为什么会问
避免把基础设施说大。
### 推荐回答
不是。当前 Kafka 主要承载 evaluation job/outbox 消息；Agent Run 的权威事件在 PostgreSQL runtime journal，实时 HTTP 消费走 process-local EventChannel。Kafka 不决定 Run terminal。
### Source Evidence
`kafka_event_sink.py`; `core/evaluation_worker.py`; `event_journal_store.py`
### Claim Boundary
`CAN_SAY:` Kafka 支撑后台评测任务。  
`CANNOT_SAY:` 全部 Runtime event 都上 Kafka。
### Likely Follow-up
Consumer 如何去重？

## Q44 [P2] Graceful Shutdown 的真实保证？
### 面试官为什么会问
生命周期收口决定生产可靠性。
### 推荐回答
Shutdown 先拒绝新 Run，向活跃 Run 发 SERVER_SHUTDOWN cancel，等待 grace period，必要时 force abort；随后按 owner 顺序关闭 dispatcher、blocking workers、model HTTP client、MCP subprocess、Redis 和 PostgreSQL。单组件关闭失败会记录 safe issue，不阻断其余清理。
### Source Evidence
`shutdown.py:GracefulShutdownCoordinator`; `application_services.py:close`; `tests/test_graceful_shutdown.py:test_shutdown_cancels_active_run_forces_timeout_and_is_idempotent`
### Claim Boundary
`CAN_SAY:` bounded、幂等、有清理报告。  
`CANNOT_SAY:` 所有外部副作用都能回滚。
### Likely Follow-up
强制中止后如何恢复？

# 6. BAD CASE BANK

| 类型 | 场景 | 错误实现 | 为什么危险 | 当前实现 | 当前限制 | 30 秒面试回答 |
|---|---|---|---|---|---|---|
| REAL_DESIGN_RISK_WITH_TEST | Legacy production bypass | coordinated 失败后回退旧 `stream_chat` | 两套 Authority、重复执行 | canonical route 无 fallback，错误安全终态 | 仍保留兼容代码/测试 | “生产路径只有 Coordinated Runtime，错误不回退第二套执行链。” |
| REAL_DESIGN_RISK_WITH_TEST | Cross-instance ownership | 用内存 map 判断 owner | 另一实例不可见 | PostgreSQL claim/lease/fence | 非共识协议 | “内存 registry 只唤醒，数据库才是 owner Truth。” |
| REAL_DESIGN_RISK_WITH_TEST | Stale lease holder | lease 过期后旧 worker 继续写 | 双 terminal/双副作用 | 每个关键 mutation 校验 fencing token | 外部 provider 不识别 fence | “fence 防本地陈旧写，外部还需幂等/对账。” |
| REAL_DESIGN_RISK_WITH_TEST | Cancel race | 只 cancel 本地 task | registry miss 丢请求 | durable CANCEL first，local wakeup second | 远端计算不保证停止 | “取消意图 durable，执行停止是协作式。” |
| REAL_DESIGN_RISK_WITH_TEST | Output delta race | 多 step 直接向用户写 | 混合/重复答案 | unique final source + single-use OutputGate | 网络层 exactly-once 不保证 | “Gate 管发布资格和 attempt，不夸大客户端字节交付。” |
| REAL_DESIGN_RISK_WITH_TEST | Provider retry duplication | mid-stream 换模型 | 两份输出拼接 | Runtime acceptance 后禁止 retry/fallback | 无 resumable stream | “首段输出就是重试屏障。” |
| REAL_DESIGN_RISK_WITH_TEST | Tool directory traversal | 直接 join 用户路径 | 逃逸 workspace | resolve containment / allowlisted roots | symlink 依赖具体 adapter policy | “文件资源先规范化再授权。” |
| REAL_DESIGN_RISK_WITH_TEST | Approval binding mismatch | 只按 approval_id/tool name | 批 A 执 B | invocation digest + run + fence | 人工仍需理解展示摘要 | “批准绑定完整 invocation，而不是工具名。” |
| REAL_DESIGN_RISK_WITH_TEST | Side-effect response lost | timeout 当失败并重试 | 重复扣款/写入 | STARTED→UNKNOWN→provider reconciliation | 无 status API 时人工 | “未知不是失败，禁止 generic retry。” |
| REAL_DESIGN_RISK_WITH_TEST | MCP reconnect replay | 新连接自动重放旧 mutation | 重复副作用 | 只读最多一次；side-effect 不 replay | UNKNOWN 需 ledger/reconciler | “连接恢复不等于业务结果已知。” |
| REAL_DESIGN_RISK_WITH_TEST | MCP schema drift | 新 session 无校验直接替换 | 同名工具语义改变 | frozen identity/schema digest revalidation | 不支持 hot reload | “不兼容 reconnect fail closed。” |
| REAL_DESIGN_RISK_WITH_TEST | Outbox dual-write | DB commit 后直接发 Kafka | 一边成功一边失败 | job+intent 同事务，ACK 后 publish | at-least-once，需 dedup | “Outbox 消除丢消息，不承诺 2PC。” |
| REAL_DESIGN_RISK_WITH_TEST | SERVICE ownership bypass | service 借 admin role 越权 | 机器身份扩大权限 | SERVICE narrow scope，不能 admin override | 依赖密钥/JWT 运营安全 | “服务身份和人类角色分离。” |
| REAL_DESIGN_RISK_WITH_TEST | Lifecycle shutdown race | 先关 DB/client 再停 Run | 活跃任务写已关闭资源 | admission→cancel/drain→close resources | 外部调用不可回滚 | “资源按 owner 反向有界关闭。” |
| HYPOTHETICAL_BAD_CASE | Evaluation path bypass | evaluation fixture 混入 `/api/chat` | 测试 seam 变生产后门 | v3/v4 typed control 隔离 | 仍需路由审计 | “普通 API 不接受 evaluation control。” |

没有可靠源码证据证明上述场景是线上事故，因此不标 `REAL_FIXED_BUG`；它们是被测试锁定的真实设计风险或明确的假设风险。

# 7. INTERVIEW_RISK_AUDIT

| Risk | Real Source Fact | Unsafe Claim | Safe Claim |
|---|---|---|---|
| 端侧 vs 云端 | 有 PyQt6 客户端，但生产 authority 在 FastAPI/PostgreSQL | “纯端侧 Agent” | “从本地应用演进为服务端 Agent 平台” |
| Redis/Kafka 必要性 | Redis cache/admission；Kafka evaluation transport | “Run 依赖 Redis/Kafka 才正确” | “核心 Truth 在 PostgreSQL，Redis/Kafka职责受限” |
| Multi-instance routing | durable ownership/cancel 已实现；未见网关 sticky-routing owner | “完整智能路由到 owner 实例” | “任意实例可写 cancel/approval，执行权由 DB fence 控制” |
| Lease/Fencing | PostgreSQL lease/token | “共识协议” | “数据库条件更新与 fencing” |
| State Machine | 有真实 transition guards | “只是 Enum” | “Enum + guarded state machine” |
| Exactly-once | provider-specific | “外部副作用 exactly-once” | “UNKNOWN + 幂等 + reconciliation” |
| Saga/TCC/2PC | 无实现证据 | “实现分布式事务” | “Outbox 与专用对账” |
| MCP HTTP | 仅 stdio | “支持 MCP HTTP” | “当前支持 stdio” |
| MCP hot reload | startup snapshot/frozen registry | “动态工具热更新” | “重连不改变 frozen contract” |
| Remote cancellation | 关闭本地 HTTP 流 | “保证 Provider 停算” | “停止本地等待与接收” |
| Resumable Streaming | 未实现 | “断线续传” | “journaled event 可审计，但用户流不续传” |
| Redis Authority | 源码明示不是 Truth | “Redis 管 Run 状态” | “缓存/限流” |
| SQLite Authority | 生产装配 PostgreSQL | “完全删除 SQLite” | “生产 Authority 已迁移，legacy code 仍在” |
| Cross-Encoder | 组件存在 | “production default” | “非默认、可测试组件” |
| AgentEvalOps | 仓外 release/eval owner | “AgentCore 自带完整评测平台” | “AgentCore 提供 authenticated target/artifact” |
| Legacy Path | canonical bypass 已清理，但 compatibility seams 存在 | “零 legacy” | “生产主链无 legacy fallback” |
| Fixture | evaluation-only 控制 | “生产可动态故障注入” | “测试/evaluation seam” |
| Historical Gate | 本轮未跑 pytest | “当前 2370 tests passed” | “静态发现 2370 个 test 函数；本轮未执行” |

# 8. 真实项目数字

| 数字 | 分类 | 证据与安全表述 |
|---:|---|---|
| 303 个 `test_*.py` 文件 | STATIC_SOURCE_COUNT | `rg --files tests -g 'test_*.py'`，本轮静态计数 |
| 2370 个顶层 `test_`/`async test_` 函数 | STATIC_SOURCE_COUNT | 文本规则静态计数；参数化 case 数不等于此数 |
| 172 个 `core/`、`mcp/`、`tools/` Python 文件 | STATIC_SOURCE_COUNT | 本轮静态计数 |
| 7 个内置生产 Tool registration | STATIC_SOURCE_COUNT | `tools/registry.py:build_builtin_tool_registrations`；MCP 工具数由配置/发现动态增加 |
| 10 个 Alembic revision 文件 | STATIC_SOURCE_COUNT | `alembic/versions/0001...0010` |
| 3 类 provider-neutral streaming delta + Finish | STATIC_SOURCE_COUNT | Text/ToolCall/Usage + Finish，`llm_engine.py` |
| RRF `k=60`、per-channel/final top-k 上限 8 | STATIC_SOURCE_COUNT | `hybrid_rrf_retriever.py`; `settings.py` |
| Benchmark size / RAG metrics | UNKNOWN | 本轮没有把历史 artifact 数字当当前结果 |
| Test passed count | UNKNOWN | 本轮遵守只读审计要求，没有运行 pytest |
| Real provider/MCP E2E result | HISTORICAL_DOCUMENT_ONLY | 测试源码存在，但本轮未启动真实服务 |
| Cross-repo E2E result | UNKNOWN | evaluation-v2 contract 有源码与测试，本轮未连 AgentEvalOps |

# 9. 项目开场回答

## 30 秒 AgentCore 项目介绍

AgentCore 是我从本地 AI 助手逐步演进出来的 Agent 平台项目。现在它不再只是把 Prompt 发给模型，而是由 FastAPI 和 Coordinated Runtime 统一管理一次 Run 的计划、调度、模型流式调用、RAG、Memory 和 Tool，并用 PostgreSQL lease/fencing、durable HITL、side-effect ledger 和 Journal 解决多实例所有权、取消、重复执行与恢复证据问题。它更准确的定位是 Agent Runtime、Harness 与 AI Backend 的组合，而不是纯端侧 Agent。

## 90 秒 AgentCore 项目介绍

AgentCore 最初是 LocalAgent，一个 PyQt6 的本地聊天助手；随着能力增加，我把核心执行路径收敛成 FastAPI 服务背后的 Coordinated Runtime。现在一次请求先经过 JWT、scope 和 ownership 校验，然后 Runtime 为 Run 建立绝对 deadline、budget、cancellation 和 PostgreSQL lease/fencing，Planner 生成并编译 frozen Plan，Scheduler 驱动单 Agent 或有界并行的 specialist，最后由 synthesis 和唯一 Output Gate 交付结果。

模型侧使用 application-scoped async HTTP client 消费原生 SSE，Runtime 统一负责 retry、fallback 和 circuit，并在首段输出后禁止切换 Provider。工具侧把模型选择当不可信 proposal，依次经过 typed validation、code-owned governance、durable HITL、execution claim 和 ToolExecutionService；副作用以 PREPARED、STARTED、COMMITTED、UNKNOWN 表达，UNKNOWN 只能用 provider-specific reconciliation 收口，所以项目不承诺 generic exactly-once。MCP 当前只支持 stdio，但其重连、session generation 和 schema revalidation 都复用这条安全链。PostgreSQL 是权威数据面，Redis 只做缓存/限流，Kafka 只做 evaluation job 的 outbox transport。AgentEvalOps 是独立评测仓，通过 SERVICE JWT 调用 AgentCore 的 evaluation-v2；我会明确把仓外评分与 release decision 排除在 AgentCore 自身能力之外。

# 10. FINAL CONCLUSION

```ini
PROJECT_DISPLAY_NAME = AgentCore
SOURCE_REPOSITORY_NAME = LocalAgent

PROJECT_POSITIONING_CONFIDENCE = HIGH
SOURCE_AUDIT_COMPLETENESS = HIGH_FOR_STATIC_PRODUCTION_PATH; NO_LIVE_RUNTIME_REVALIDATION

CANONICAL_PRODUCTION_PATH = FastAPI -> ChatService -> CoordinatedRuntimeFactory -> PostgreSQL fenced RunCoordinator -> Planner/Scheduler -> Model/RAG/Memory/Tool -> Journal/OutputGate/Terminal
REAL_RUNTIME_STATE = IMPLEMENTED_AND_TESTED_IN_REPOSITORY; NOT_REEXECUTED_THIS_AUDIT
REAL_AGENT_LOOP_STATE = IMPLEMENTED_AND_PRODUCTION_REACHABLE
REAL_MULTI_AGENT_STATE = IMPLEMENTED_BOUNDED_IN_RUN_PARALLELISM
REAL_MULTI_INSTANCE_STATE = DURABLE_RUN_OWNERSHIP_CANCEL_APPROVAL_AND_FENCING_IMPLEMENTED; NO_CONSENSUS_OR_OWNER_ROUTING_CLAIM
REAL_PROVIDER_STREAMING_STATE = NATIVE_ASYNC_HTTP_SSE_WITH_TYPED_DELTAS
REAL_TOOL_RUNTIME_STATE = REGISTRY_TYPED_VALIDATION_GOVERNANCE_APPROVAL_CLAIM_EXECUTION_IMPLEMENTED
REAL_HITL_STATE = DURABLE_FIRST_WINS_EXACT_BINDING_CROSS_INSTANCE
REAL_SIDE_EFFECT_STATE = DURABLE_LEDGER_AND_PROVIDER_SPECIFIC_RECONCILIATION; NO_GENERIC_EXACTLY_ONCE
REAL_MCP_STATE = STDIO_ONLY_WITH_RESILIENT_GENERATION_LIFECYCLE; NO_HTTP_OR_HOT_RELOAD
REAL_RAG_STATE = DENSE_AND_BM25_AND_HYBRID_RRF_WITH_IMMUTABLE_GENERATIONS; CROSS_ENCODER_NOT_DEFAULT
REAL_MEMORY_STATE = POSTGRES_CONVERSATION_SEMANTIC_EPISODIC_PROJECT_MEMORY; NOT_RUNTIME_AUTHORITY
REAL_BACKEND_FOUNDATION_STATE = POSTGRES_AUTHORITY_REDIS_CACHE_ADMISSION_KAFKA_EVALUATION_TRANSPORT_AUTH_OBSERVABILITY_GRACEFUL_SHUTDOWN
REAL_CROSS_REPO_EVALUATION_STATE = AUTHENTICATED_EVALUATION_V2_TARGET_IMPLEMENTED; LIVE_CROSS_REPO_E2E_NOT_RUN
REAL_LEGACY_CLEANUP_STATE = CANONICAL_RUNTIME_BYPASS_REMOVED; SQLITE_AND_WIRE_COMPATIBILITY_SEAMS_REMAIN

TOP_INTERVIEW_STRENGTHS = OWNER/TRUTH_BOUNDARIES; FENCED_MULTI_INSTANCE_CONTROL; OUTPUT_STARTED_BARRIER; DURABLE_HITL; HONEST_SIDE_EFFECT_UNKNOWN; MCP_GENERATION_SAFETY; IMMUTABLE_RAG_PROVENANCE
TOP_INTERVIEW_RISKS = OVERCLAIMING_EXACTLY_ONCE; CALLING_LEASE_CONSENSUS; CLAIMING_MCP_HTTP/HOT_RELOAD; CLAIMING_REMOTE_CANCEL; CLAIMING_REDIS/KAFKA_AUTHORITY; CLAIMING_ZERO_LEGACY; BORROWING_AGENTEVALOPS_CAPABILITIES
UNSAFE_CLAIMS_COUNT = 18

CAN_USE_THIS_AUDIT_FOR_INTERVIEW_PREP = YES
```
