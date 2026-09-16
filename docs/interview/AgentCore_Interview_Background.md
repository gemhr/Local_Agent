# AgentCore（平台项目）面试背景

## 1. 一句话定位

AgentCore 是一个以 Coordinated Runtime 为核心、同时承担 Agent Harness 与 AI Backend 职责的平台：它把模型、检索、记忆和工具调用纳入统一的 Run 生命周期、安全治理、持久化控制面和可观测性边界。

## 2. 项目演进

项目源码仍名为 LocalAgent / `Local_Agent`，最初以 PyQt6 本地助手为主。当前 production composition 已演进为 FastAPI + PostgreSQL 的服务端平台；桌面客户端仍可作为入口，但不再拥有核心 Runtime Truth。

## 3. 当前架构

```text
API/Auth
→ ChatService
→ CoordinatedRuntimeFactory / RunCoordinator
→ Planner / PlanCompiler / Scheduler
→ Model + RAG + Memory + Tool
→ OutputGate
→ PostgreSQL Journal + terminal truth
```

## 4. Owner / Truth

- PostgreSQL：Run ownership、terminal、Journal、approval、execution claim、tool invocation、memory、identity、evaluation jobs/outbox。
- RunCoordinator：单 Run 生命周期和 finalization。
- ModelInvocationRouter：retry/fallback/circuit 决策。
- ToolPolicyCatalog / GovernanceService：工具风险与授权。
- McpIntegrationComponent：MCP session/reconnect/generation。
- Redis：cache 与 rate-limit，不是 Truth。
- Kafka：evaluation job transport，不是 Run Truth。
- Observability：投影，不反向决定业务状态。

## 5. Runtime

Runtime 为每个 Run 建立 identity、absolute deadline、cancellation、budget 和 durable lease。AgentStateMachine 是有 guard 的真实状态机；Planner 产出决策，Compiler 冻结 Plan，Scheduler claim ready steps，OutputGate 只允许唯一 final source 发布。

## 6. Multi-instance

`DurableRunControlService` 使用 PostgreSQL lease 和单调 fencing token 控制 owner。跨实例 cancel 先写 durable command；local registry 只用于加速。它不是共识协议，也没有宣称智能请求路由到 owner 实例。

## 7. Provider

Remote provider 使用 application-scoped `httpx.AsyncClient` 与原生 SSE，解析 typed delta。Retry/fallback 由 Runtime Router 统一管理，只允许发生在第一段被 Runtime 接受的输出之前；mid-stream failure fail-stop。关闭本地流不等于保证 Provider 停止计算。

## 8. Tool

链路是：模型 proposal → Registry → typed validation → Governance → 必要时 HITL → Execution Claim → ToolExecutionService → typed result → model continuation。内置生产 Tool 静态共 7 个，MCP 可在 startup discovery 后追加。

## 9. HITL

审批在 PostgreSQL 持久化为 PENDING/APPROVED/REJECTED/INVALIDATED。决策 first-wins，同向重复幂等；approval 与完整 invocation binding digest 绑定。APPROVED 后仍需 current fenced execution claim，审批本身不是执行权。

## 10. Side Effect

Durable ledger 表达 PREPARED、STARTED、COMMITTED、UNKNOWN、NOT_COMMITTED。Provider 调用前必须 STARTED；调用后失去响应不能写 FAILED，而要写 UNKNOWN。只有 provider-specific status/reconciliation 能安全收口；项目不承诺 generic exactly-once、Saga、TCC 或 2PC。

## 11. MCP

当前只支持 subprocess stdio，不支持 Streamable HTTP/SSE。Lifecycle 是 application-scoped 单一 owner，支持 bounded backoff、jitter、singleflight、session generation、identity 和 frozen schema revalidation。只读 disconnect 最多在新 generation replay 一次；side-effect 不 replay。Registry 是 startup snapshot，不支持 hot reload。

## 12. RAG

生产可选择 Dense baseline 或 Hybrid RRF。Hybrid 将同一 immutable generation 的 Dense 与 BM25 结果按 rank 融合，active descriptor 原子发布，cache identity 包含 authz domain、index generation、policy 和 normalized query digest。Cross-encoder 有组件但不是当前 production default。

## 13. Memory

生产装配使用 PostgreSQL conversation/message、private semantic、episodic 和 project semantic memory。MemoryRetrievalService 负责筛选与注入，Memory 不拥有 Run terminal、approval 或 side-effect Truth。SQLite 实现仍留在仓库的 legacy/test/migration surface，不能说源码已完全删除 SQLite。

## 14. Backend Foundation

- FastAPI lifespan 是 Composition Root 和 application resource owner。
- JWT 认证后查 PostgreSQL principal，区分 HUMAN/SERVICE。
- HUMAN 使用 role + object ownership；SERVICE 使用窄 scope。
- Redis cache fail-open，rate-limit 故障可 fail-closed。
- Kafka + transactional outbox 支撑 evaluation jobs 的 at-least-once transport 与 dedup。
- Prometheus/OpenTelemetry、structured log、trace export 都是安全投影。
- Graceful shutdown 先停止 admission，再 cancel/drain Runs，最后有界关闭共享资源。

## 15. AgentEvalOps Boundary

AgentEvalOps 是独立评测平台。它以 SERVICE JWT 调用 `/api/runtime/evaluation-execute/v2`；AgentCore 执行真实 Run 并返回 bounded artifact/provenance，AgentEvalOps 负责数据集、评分、报告和 release decision。本轮没有启动跨仓 E2E，不能声称当前部署已验收通过。

## 16. Current Limitations

- 无 generic external exactly-once。
- 无 Saga/TCC/2PC。
- MCP 仅 stdio，无 HTTP、无动态 hot reload。
- 无 resumable user streaming。
- remote cancellation 不保证远端停算。
- multi-instance control 已实现，但没有共识协议或完整 owner-aware routing 结论。
- cross-encoder 非默认生产链。
- SQLite 与 wire compatibility seam 仍存在。
- fault/evaluation fixture 不是普通生产能力。

## 17. 简单项目 Q&A

1. **项目是什么？** Agent Runtime + Harness + Backend 的组合平台。
2. **生产入口？** `server.py::lifespan()` 装配，`/api/chat` 与 `/api/runtime/execute` 进入 Coordinated Runtime。
3. **Run Truth 在哪？** PostgreSQL run control 与 runtime journal。
4. **内存 RunRegistry 做什么？** 本实例 handle/wakeup 加速，不做跨实例 Truth。
5. **状态机是真的吗？** 是，有 transition guard、clone/validate/commit。
6. **多 Agent 怎么做？** frozen DAG、ready claim、有界并行、依赖后 synthesis。
7. **为什么 OutputGate？** 防内部 step、错误 source 和重复 publish。
8. **Provider 是真流式吗？** 是，async HTTP native SSE typed delta。
9. **何时能 fallback？** 第一段输出被 Runtime 接受之前。
10. **工具为何不能直接执行？** 模型无 Authority，必须 validation/governance/HITL/claim。
11. **审批和执行有什么区别？** 审批是授权事实，execution claim 才占用一次执行权。
12. **UNKNOWN 是什么？** Provider 可能已提交但本地无法确认。
13. **Exactly-once 做到了吗？** 只在 provider 合同支持时可接近 effectively-once，非通用保证。
14. **MCP 支持什么？** stdio initialize/list/call/close 与安全重连。
15. **RAG 为什么 RRF？** Dense/BM25 分数尺度不同，rank fusion 更稳且可审计。
16. **Redis 做什么？** RAG cache 与 admission/rate-limit。
17. **Kafka 做什么？** evaluation job/outbox transport。
18. **Memory 是 Authority 吗？** 是记忆数据 authority，不是 Runtime safety authority。
19. **AgentEvalOps 是本仓模块吗？** 不是，通过 authenticated HTTP contract 交互。
20. **最大诚实边界？** 不夸大 exactly-once、共识、MCP HTTP、远端取消和零 legacy。

## 18. 面试禁止夸大的事实

- 不说“AgentCore 是纯端侧 Agent”。
- 不说“Lease/Fencing 是共识协议”。
- 不说“Redis/Kafka 持有 Run Truth”。
- 不说“实现任意外部 Tool exactly-once”。
- 不说“实现 Saga/TCC/2PC”。
- 不说“支持 MCP HTTP/Streamable HTTP 或工具 hot reload”。
- 不说“Cancel 一定停止远端模型计算”。
- 不说“支持断线续传”。
- 不说“Cross-Encoder 是生产默认”。
- 不把 AgentEvalOps 的评分/release 能力算到 AgentCore。
- 不把 evaluation fixture/fault injection 当普通生产入口。
- 不说“仓库已完全删除 SQLite/legacy”。
- 不把历史文档中的 passed 数字说成本轮测试结果。

## 面试开场建议

稳定口径是：“AgentCore 从本地助手演进为以服务端 Coordinated Runtime 为核心的 Agent 平台，重点解决模型和工具执行进入生产后出现的 Owner、Truth、取消、重复、人工审批和恢复边界。”随后用一条真实调用链展开，不先堆技术栈。
