# Stage 3 Contract Freeze v1

- **Status**: `FROZEN_CANDIDATE_PENDING_FINAL_GATE`
- **Scope**: LocalAgent Stage 2 / Stage 3 stabilized contracts，按 Stage3.5-WP1 Contract Inventory（`STAGE3_5_WP1_CONTRACT_INVENTORY = PASS`）接受。
- **Authority 边界**: WP2 只创建本冻结文档并修复已知文档漂移，不宣布最终 `FROZEN/PASS`。最终冻结状态由 WP3 Final Gate 决定。
- **Source of Truth 优先级**: 本文件以 current production source、current tests、独立 Stage 3 Gates 与 `10_codex_contract_inventory.md` 为准；若当前源码与本文件描述出现分歧，以源码为准并上报 `CONTRACT_SOURCE_DRIFT`。

## 1. Current Authoritative Status

| 项 | 值 |
| --- | --- |
| Stage3 | PASS |
| Stage3.5-WP1 | PASS |
| Contract ambiguity | 0 |
| Freeze candidates reviewed | 30 |
| FREEZE_V1 | 22 |
| DO_NOT_FREEZE | 8 |
| P0 | 0 |
| P1 | 0 |
| P2 | 6 |
| DOC_DRIFT（WP2 前） | 1 |
| DOC_DRIFT（WP2 后） | 0 |

## 2. Truth Boundary

Stage 3 Minimal Necessary Productionization = **PASS**（最小必要生产化）

Real Local Cross-System E2E = **VERIFIED**（真实本地双系统 E2E，非模拟）

Production proven = **NO**

Exactly-once = **NO**

Durable delivery = **NO**

Automatic recovery = **NO**

Production fault activation = **NO**

本文件不升级任何能力措辞。

## 3. Classification Model

| Class | 含义 | 冻结策略 |
| --- | --- | --- |
| `PUBLIC_STABLE` | 项目代码可依赖且未单独版本化的稳定类型/语义；不自动等于网络 API | 冻结语义，无独立 version |
| `PUBLIC_VERSIONED` | 有明确 schema/contract version 或 fingerprint 的跨边界合同 | 冻结语义 + 版本/指纹规则 |
| `PROTECTED_INTERNAL_CONTRACT` | 不公开为数据协议，但 Owner、顺序或失败语义必须保护 | 冻结 Owner/语义，不冻结内部构造 |
| `INTERNAL_IMPLEMENTATION` | 允许重构，只需保持冻结行为 | 不冻结（DO_NOT_FREEZE） |
| `NOT_FROZEN / DEFERRED` | 当前未实现或明确不进入 v1 | 不冻结（DO_NOT_FREEZE） |

## 4. Freeze Summary

- Reviewed candidates：**30**
- FREEZE_V1：**22**
- DO_NOT_FREEZE：**8**

FREEZE_V1 分布：

| Class | 数量 | 候选 |
| --- | --- | --- |
| PUBLIC_STABLE | 5 | C01–C05 |
| PUBLIC_VERSIONED | 7 | C06–C12 |
| PROTECTED_INTERNAL_CONTRACT | 10 | C13–C22 |
| INTERNAL_IMPLEMENTATION | 4 | C23–C26 |
| NOT_FROZEN / DEFERRED | 4 | C27–C30 |

> `INTERNAL_IMPLEMENTATION` 与 `NOT_FROZEN / DEFERRED` 均不进入 FREEZE_V1（合计 8 = DO_NOT_FREEZE）。

## 5. PUBLIC_STABLE（C01–C05，5 项）

按 WP1 定义原样冻结，不发明 schema version：

| # | Contract | Canonical Source | Frozen Semantics |
| --- | --- | --- | --- |
| C01 | RunContext | `core/runtime/context.py` | 单 Run 的进程内安全上下文与安全元数据边界 |
| C02 | Plan / PlanStep | `core/runtime/planning.py` | 不可变静态定义，不保存 runtime status/attempt/error |
| C03 | ModelInvocationResult | `core/runtime/model_invocation.py` | typed 调用结果语义；adapter plumbing 内部化 |
| C04 | Tool invocation / result / error | `core/runtime/tool_contract.py` | JSON-safe typed invocation/result/error 边界；raw 参数不得进入 Journal/Wire |
| C05 | RetrievalExecutionResult | `core/runtime/retrieval_contract.py` | typed result 与正文/安全投影边界 |

Breaking：删除/重命名稳定字段、改变 typed success/failure semantic domain、扩大敏感正文投影。

## 6. PUBLIC_VERSIONED（C06–C12，7 项）

版本语义完全采用 WP1 的当前精确值：

| # | Contract | Version / Fingerprint | Owner |
| --- | --- | --- | --- |
| C06 | AgentState | schema **v1** | AgentState（`core/runtime/state.py`） |
| C07 | RuntimeEvent | reader **v1 / v2**；writer **v2** | RuntimeEvent + RuntimeEventChannel |
| C08 | JournalRecord | reader **v1 / v2**；writer **v2** | RunEventJournal |
| C09 | RunSnapshot | schema **v1**（无 v0） | Snapshot contract/store |
| C10 | Trace Contract | version **1**；fingerprint `6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab` | trace/export semantic owner |
| C11 | TraceExportEnvelope / wire | version **1**；同一 fingerprint | export contract + sole serializer |
| C12 | AgentEvalOps ingest API | endpoint **v1** | AgentEvalOps route/service/repo |

### 6.1 AgentState

- AgentState = runtime mutable state 的 **Single Source of Truth**；schema version **v1**。
- Plan / PlanStep 只是静态定义，不承载 runtime status；AgentState 不重复存在于 Plan。
- 未知/缺失关键版本 fail closed 且不写回。

### 6.2 RuntimeEvent

- reader **v1 / v2**，writer **v2**。
- RuntimeEventChannel = per-run identity / sequence owner；已消费 sequence 不复用。

### 6.3 Journal

- JournalRecord reader **v1 / v2**，writer **v2**。
- Journal = append-only durable event safe facts。
- 发布保持 **journal-first**。
- Trace **不得**描述为 durable event truth。

### 6.4 Snapshot / Recovery

- RunSnapshot schema **v1**；Snapshot 为 **opt-in**（默认关闭）。
- Recovery = **VALIDATION_ONLY**。
- 明确不支持：resume execution、side-effect replay、AgentState writeback、automatic continue、automatic recovery。

## 7. PROTECTED_INTERNAL_CONTRACT（C13–C22，10 项）

冻结语义所有权，不冻结无关构造顺序。

### 7.1 Protected Runtime Ownership（C13）

| Owner | Frozen Semantics |
| --- | --- |
| `server.py::lifespan()` | 唯一生产 Composition Root |
| `RunCoordinator` | per-run lifecycle / terminal owner（每 Run 最多一个 terminal） |
| `GracefulShutdownCoordinator` | shutdown orchestration owner |
| `ApplicationRuntimeServices` | application-scoped runtime service owner |

不冻结 incidental construction ordering（除非正确性必需）。Run close 不得关闭 Application resource。

### 7.2 Planning / Scheduling（C14，C16）

冻结行为语义：

- typed `PlanResolver` decision；
- dependency satisfied 后才能 claim；
- policy-limited parallel execution；
- unique final source；
- `StepResultStore` 只包含已完成 step results；
- Synthesis 只消费 permitted dependency view。

**不冻结**：scheduler scanning algorithm、sorting strategy、lock implementation、executor/thread implementation。

### 7.3 Agent Registry（C15）

`AgentRegistry` 拥有：identity、capability、entry/delegation/output policy。

`AgentRegistry` **不拥有** Tool permission。

### 7.4 Tool Platform Ownership（C17）

| Owner | Frozen Semantics |
| --- | --- |
| `ToolRegistry` | Tool identity / discovery / descriptor / adapter binding（APPLICATION_SCOPE，startup freeze 后只读） |
| `ToolGovernanceService` | Agent→Tool permission / risk / approval 的唯一 invocation-time Authority |
| `ResourceAuthorizationService` | resource/path authorization |
| `ToolExecutionService` | sole production Tool execution owner |

同时冻结：**model cannot self-approve**。

`ResourceAuthorizationService` **不得**描述为 Sandbox。

### 7.5 Event / Journal Ordering（C18）

- channel sequence ownership + journal-first 语义随 C07/C08 冻结；
- Trace 是旁路 observability，不能替代或修改 Journal。

### 7.6 Recovery Validation-Only（C19）

- `RecoveryValidator` 只读 Snapshot + current Plan + Journal，返回不可变 assessment；
- 改变 validation-only 语义需另行 versioned architecture decision，不得在 v1 内静默扩展。

### 7.7 Output / Delivery（C20）

- `OutputGate` = final publication owner；single-use；terminal 后不重发。
- final publication = **at-most-once**。
- **不得**与 HTTP/Trace transport delivery guarantees 混淆。
- `DeliveryStatus` 值：`DELIVERED`、`FAILED`、`OUTCOME_UNKNOWN`。

### 7.8 Final Memory（C21）

- `RunFinalMemoryWriter` = final business memory commit owner。
- 仅 `DELIVERED` output 有资格写入；per-run **write-once**。
- specialist raw output、failed output、unknown delivery output 不得描述为 final business memory。

### 7.9 Trace Delivery Semantics（C22）

- **`BEST_EFFORT` + `AT_MOST_ONE_TRANSPORT_ATTEMPT_PER_ACCEPTED_ENVELOPE`**。
- 明确不支持：exactly-once、at-least-once、at-most-once delivery、durable delivery、durable replay、retry、batching、durable outbox。
- queue acceptance ≠ attempt ≠ sent ≠ remote durable/ack。

## 8. Trace Contract v1（C10）精确值

- Contract version：**1**
- Fingerprint：**`6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab`**
- Stable operations 恰为：

```
runtime.run
runtime.planning
runtime.step
runtime.synthesis
runtime.output_delivery
runtime.final_memory_commit
```

- 改变 operation、export field/presence/value-domain、compatibility behavior 或 fingerprint descriptor semantics 属 breaking，要求 contract version/fingerprint 决策。

## 9. TraceExportEnvelope / Wire（C11）精确值

- identity：`localagent.runtime.trace_export`
- version：**1**
- 使用与 Trace Contract 相同的 fingerprint。
- Exact 16 wire fields：

```
contract_identity
contract_version
contract_fingerprint
run_id
trace_id
span_id
parent_span_id
step_id
operation
component
started_at
completed_at
duration_ms
status
error_code
attributes
```

- Payload bound：完整 UTF-8 envelope **≤ 16384 bytes**。
- **不得**把 15901 位数字描述为通用整数最大值（该值只是 `MAX_V1_DURATION_INT` 的十进制位数上下文，不是通用 bound）。
- 只接受 completed Span 的 safe projection；unknown/missing identity/version/fingerprint 或 envelope semantic mismatch fail closed。

## 10. Trace Serializer（C11）

- `serialize_trace_export_envelope(...)` = **sole production wire serializer**。
- 冻结外部有意义的语义：
  - deterministic JSON bytes；
  - exact integer JSON token semantics；
  - numeric semantic preservation；
  - fail closed；
  - payload bound。
- **不冻结** private encoding helper 实现（key sort 实现、整数 formatting 技巧、Decimal 解析）。

## 11. AgentEvalOps Integration（C12）

### 11.1 API

- Endpoint：**`POST /integrations/localagent/v1/trace-envelopes`**
- Semantics：
  - first accepted write + commit → **201 PERSISTED**；
  - canonical exact replay → **200 DUPLICATE_ACCEPTED**（零 mutation）；
  - identity / semantic conflict → **409 REJECTED**。
- **2xx 仅在 PostgreSQL commit 后返回**。

### 11.2 Persistence Truth

- sidecar = **authoritative** LocalAgent frozen-envelope truth。
- legacy Trace / Span = **compatibility projection**。
- 不得把 legacy Trace/Span 描述为完整 authoritative envelope storage。

## 12. Numeric Contract（C26 冻结的外部语义）

只冻结外部语义保证，不冻结实现：

- `duration_ms`：
  - Python bool **invalid**；
  - non-negative int，接受至当前 v1 MAX：`MAX_V1_DURATION_INT = 2**1024 - 2**970 - 1`；
  - finite non-negative float 接受；
  - negative / NaN / Inf **invalid**。
- Wire integer：exact JSON integer token。
- Persistence：必须 preserve accepted semantic value。

**不冻结**：Decimal parsing 的具体实现、SQLAlchemy TypeDecorator（column-local huge-int JSONB）具体实现；AgentEvalOps 的 column-local specialization 是当前兼容实现，不升级为 shared-engine 通用 JSON 行为承诺。

## 13. Security Capability Boundary

| Capability | Status |
| --- | --- |
| Prompt Injection | `PARTIALLY_SUPPORTED`（code-owned trusted controls 才可绑定 system role；恶意自然语言仍可能影响答案或导致 System Prompt 复述/改写） |
| Generic WAF | `NOT_IMPLEMENTED` |
| Generic DLP | `NOT_IMPLEMENTED` |
| Full Sandbox / OS isolation | `NOT_IMPLEMENTED` |

Resource Authorization **≠** Sandbox。

## 14. Fault / Chaos Boundary

- deterministic fault injection 存在，但只用于 **`TEST_SCOPE`** / 显式测试 seam。
- Production fault activation：**NO**。
- Random production chaos：**NO**。
- Settings/env/HTTP/prompt/Tool arguments 无激活入口；生产默认 `fault_controller=None`。
- 不冻结 test controller / fixture 内部 mechanics。

## 15. Operational Semantics

- AgentEvalOps trace export default：**disabled**。
- 启用时：required configuration（base URL / API key / project ID / 合法 connect 与 total deadline）被校验。
- 非法配置：**startup fail closed**。
- persistence preflight：在 READY 前 read-only 执行。
- blocking persistence result：阻止 READY。

## 16. Explicitly Non-Frozen Internals

以下可重构且**无需 contract version bump**，只要冻结的 external/protected semantics 不变：

- Scheduler scan/sort algorithm；
- queue implementation；
- thread / executor implementation；
- PycURL easy-handle reuse/reset；
- logging implementation；
- metrics implementation；
- private DTO / mapper / decoder helpers；
- `TraceContext` / `SpanHandle` / `SpanRecord` 内部表示；
- fault test implementation mechanics；
- column-local JSONB concrete encoding implementation。

## 17. Deferred / Not Frozen（C27–C30）

- Recovery execution / replay；
- Durable Trace retry / batching / outbox；
- Production fault / chaos；
- Generic WAF / DLP / Sandbox。

上述项当前为 `NOT_IMPLEMENTED`，**不**暗示在下一阶段立即规划。

## 18. Breaking Change Rules

以下变更在适用时是 **breaking**：

- remove / rename frozen public field；
- 改变 accepted semantic domain；
- 改变 required / optional field presence；
- 改变 wire field set；
- 改变 payload bound；
- 改变 version/fingerprint descriptor semantics；
- 改变 canonical Owner；
- 改变 Tool execution ownership；
- 改变 event sequence ownership；
- 改变 journal-first semantics；
- 把 Recovery 从 validation-only 扩成执行；
- 改变 OutputGate ownership；
- 改变 DELIVERED-only Memory rule；
- 改变 Trace delivery guarantee；
- 改变 AgentEvalOps endpoint/status/commit/sidecar authority。

以下为 **non-breaking**（只要冻结的 external/protected semantics 不变，无需 version bump）：

- private helper refactor；
- scheduler algorithm replacement；
- queue/thread replacement；
- logging/metrics changes；
- PycURL handle reuse strategy；
- internal DTO implementation change。

## 19. P2（Carry Forward，6 项，本轮不修复）

1. planning executor starvation；
2. untrusted natural-language/data semantic influence；
3. System Prompt disclosure/rewriting risk；
4. delivery/final-memory negative/not-attempted paths 缺少 symmetric spans；
5. planning/step error taxonomy 可能折叠为 `UNHANDLED_ERROR`；
6. AgentEvalOps legacy delete divergence（sidecar truth 与 legacy read model）。

不新增 P2。

## 20. Known Limitations

- single-process Windows-native；
- force-kill 可能绕过 graceful shutdown；
- trace export queue ephemeral（进程崩溃可丢失 queued/in-flight）；
- 无 production/SLA/HA/capacity proof；
- Snapshot opt-in；
- Recovery validation-only；
- 无 durable human approval pause/resume；
- 无 generic WAF/DLP/Sandbox。

## 21. Current Claim Boundary

| Claim | Value |
| --- | --- |
| Stage3 Minimal Necessary Productionization | PASS |
| Real Local Cross-System E2E | VERIFIED |
| Production proven | NO |
| Exactly-once | NO |
| Durable delivery | NO |
| Automatic recovery | NO |
| Production fault activation | NO |

## 22. 关联文档

- Owner 边界：`docs/runtime/runtime_owner_matrix.md`
- 能力状态：`docs/runtime/runtime_capability_matrix.md`
- Trace 合同：`docs/runtime/stage2_5_trace_contract_v1.md`
- WP1 Inventory：`.ai/handoff/stage3_5-contract-freeze/10_codex_contract_inventory.md`
