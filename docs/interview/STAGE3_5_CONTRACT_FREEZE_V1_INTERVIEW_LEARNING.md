# Stage 3.5 — Contract Freeze v1 全阶段学习 / 面试总结

先冻结 **整个 Stage 3.5** 的最终事实：

```text
Stage 3.5 — Contract Freeze v1
= PASS

Stage3.5-WP1 Contract Inventory
= PASS

Stage3.5-WP2 Contract Freeze Artifacts & Documentation Sync
= PASS

Stage3.5-WP3 Contract Freeze Final Gate
= PASS

Contract Freeze v1
= FROZEN

Stage 3
= FROZEN_BASELINE_V1

Reviewed candidates = 30
FREEZE_V1 = 22
DO_NOT_FREEZE = 8

PUBLIC_STABLE = 5
PUBLIC_VERSIONED = 7
PROTECTED_INTERNAL_CONTRACT = 10
INTERNAL_IMPLEMENTATION = 4
NOT_FROZEN / DEFERRED = 4

Contract ambiguity = 0
Owner conflict = 0

P0 = 0
P1 = 0
P2 = 6

DOC_DRIFT = 0

Ready for subsequent AgentEvalOps / MCP / RAG / Memory development
= YES

Production proven = NO
```

最终 Final Gate（最终门禁）直接验证了冻结文档与当前生产源码、Owner、版本、Fingerprint（指纹）、Wire Contract（传输合同）、Recovery、Tool、Output/Memory、Trace Delivery 以及能力声明的一致性；聚焦测试 `546 passed`，全仓 `2467 passed + 42 subtests`。

------

# 1. 一句话项目 / 阶段定义

Stage 3.5 的目标不是增加新功能，而是：

> **在 LocalAgent 完成 Stage 3 最小必要生产化之后，把已经真实实现、测试并通过独立 Gate 的核心 Contract（合同）、Architectural Owner（架构所有权）和 Behavioral Semantics（行为语义）正式冻结为 v1，同时明确哪些内部实现不属于合同、未来仍可以自由重构。**

最终形成：

```text
Stage 3
→ FROZEN_BASELINE_V1
```

以后 AgentEvalOps、MCP、Skill、高级 RAG、Memory 等开发都应该以这条冻结基线为兼容边界。

------

# 2. 为什么做 Stage 3.5

Stage 3 已经：

```text
Minimal Necessary Productionization
= PASS
```

理论上可以直接继续开发。

但继续开发会出现一个越来越严重的问题：

> **后续功能可能无意中破坏前面已经验证通过的 Runtime 边界。**

例如未来做 MCP 时，很容易重新出现：

```text
MCP Tool Executor
→ 第二套 Tool 执行 Owner
```

做高级 Memory 时可能出现：

```text
Specialist raw output
→ 直接写业务 Memory
```

做 Recovery 时可能把：

```text
RecoveryValidator
```

慢慢扩成：

```text
RecoveryExecutor
```

却没有重新定义 side-effect replay（副作用重放）等合同。

所以 Stage 3.5 的核心目的不是“锁代码”，而是：

> **锁住已经证明正确的语义，给后续开发划出不能无声跨越的边界。**

------

# 3. 真实性与完成边界

## 已真实完成

整个 Stage 3.5 真正完成了三件事。

### WP1：合同盘点

真实审计当前源码、测试、正式文档和最新 Gate，识别：

```text
30 candidates

22 → FREEZE_V1
8 → DO_NOT_FREEZE
```

并确认：

```text
Contract ambiguity = 0
NEEDS_RESOLUTION = 0
P0 = 0
P1 = 0
```



### WP2：冻结文档落地

创建唯一权威文档：

```text
docs/contracts/STAGE3_CONTRACT_FREEZE_V1.md
```

并最小同步：

```text
docs/runtime/runtime_capability_matrix.md
docs/runtime/stage2_5_trace_contract_v1.md
```

将：

```text
DOC_DRIFT = 1
```

修复为：

```text
DOC_DRIFT = 0
```

没有修改生产代码、测试或依赖。

### WP3：独立 Final Gate

重新基于当前生产源码做：

- Owner 验证；
- Version/Fingerprint 验证；
- Wire schema 验证；
- Recovery/Tool/Output/Memory 验证；
- direct source probe；
- targeted regression；
- full regression；
- static checks。

最终：

```text
PASS
P0 = 0
P1 = 0
Owner conflict = 0
Contract ambiguity = 0
```



------

## 没有做什么

Stage 3.5 没有：

```text
新增 Runtime 能力
新增 Tool 能力
修复 6 个 P2
增加 retry/outbox
增加 automatic recovery
增加 HA
增加 distributed execution
增加 production chaos
增加 WAF/DLP/Sandbox
```

所以它是：

> **Contract management stage（合同治理阶段），不是 feature development stage（功能开发阶段）。**

------

# 4. Stage 3.5 前的问题与根因

进入 Stage 3.5 时，系统实际上已经有很多“事实合同”。

例如：

```text
AgentState
Plan
RuntimeEvent
Journal
Snapshot
Tool Contract
OutputGate
Trace Contract
AgentEvalOps API
```

但它们分散在：

```text
source code
tests
runtime docs
Stage2/2.5 frozen docs
Stage3 implementation reports
independent Gate reports
```

这产生三个核心风险。

------

## 风险一：合同边界不清

例如：

```text
TraceExportEnvelope
```

显然是合同。

但：

```text
PycURL easy-handle reuse
```

是不是合同？

答案是否定的。

如果不先分类，很容易把实现细节冻死。

------

## 风险二：Owner 漂移

Agent 系统特别容易出现多个组件“都觉得自己负责某件事”。

例如：

```text
ToolRegistry
ToolGovernance
ResourceAuthorization
AgentRegistry
```

如果职责没有冻结，后续很容易形成：

```text
Permission Owner x2
Execution Owner x2
```

------

## 风险三：能力声明漂移

软件没变，文档却可能从：

```text
Recovery = VALIDATION_ONLY
```

逐渐写成：

```text
Recovery Supported
```

最后面试时就容易把未实现能力讲成已完成。

所以 Stage 3.5 同时冻结：

```text
Capability Claim Boundary
```

------

# 5. 方案讨论与取舍

这是 Stage 3.5 最值得学习的部分。

## 方案 A：把所有核心类都冻结

拒绝。

因为：

```text
Class
≠ Contract
```

例如：

```text
TraceContext
SpanHandle
SpanRecord
```

虽然重要，但当前只是内部表示。

最终仍属于：

```text
INTERNAL_IMPLEMENTATION
```



------

## 方案 B：只冻结 Public API

也拒绝。

因为很多最重要的系统不变量根本不是网络 API。

例如：

```text
ToolExecutionService
= sole production execution owner

OutputGate
= final publication owner

AgentState
= runtime mutable state Single Source of Truth
```

这些都是必须长期保护的架构语义。

所以引入：

```text
PROTECTED_INTERNAL_CONTRACT
```

这一层。

------

## 最终采用五级分类

```text
PUBLIC_STABLE

PUBLIC_VERSIONED

PROTECTED_INTERNAL_CONTRACT

INTERNAL_IMPLEMENTATION

NOT_FROZEN / DEFERRED
```



核心思想：

> **不要求跨进程公开，才叫 Contract。只要其他组件依赖这个语义或者 Owner，它就可能值得冻结。**

------

# 6. 最终 Freeze 架构

整个 Freeze v1 可以看成三层。

```text
                    Stage3 Contract Freeze v1
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
    Public Contract     Protected Semantics    Non-Frozen
          │                   │                   │
     Data / Wire          Owner / Behavior     Implementation
```

### Public

```text
RunContext
Plan / PlanStep
ModelInvocationResult
Tool typed contract
RetrievalExecutionResult

AgentState v1
RuntimeEvent v1/v2
JournalRecord v1/v2
RunSnapshot v1
Trace Contract v1
TraceExportEnvelope v1
AgentEvalOps API v1
```

### Protected Internal

```text
Lifecycle / Composition
Planning / Scheduling semantics
AgentRegistry ownership
StepResultStore / Synthesis boundary
Tool Platform ownership
Event / Journal ordering
Recovery validation-only
OutputGate
Final Memory
Trace Delivery
```

### Non-Frozen

```text
Scheduler implementation
queue/thread/executor
PycURL handle reuse
logging/metrics internals
private DTO helpers
TraceContext/SpanHandle/SpanRecord
fault test mechanics
column-local JSONB implementation
```

最终 Gate 确认三层边界没有混淆。

------

# 7. 核心状态机与时序

Stage 3.5 没有增加业务状态机，但冻结了多条关键状态转换。

## Runtime

```text
Plan
= immutable static definition

        ↓ execution

AgentState
= mutable runtime truth
```

禁止：

```text
Plan.status
+
AgentState.status
```

形成双 Owner。

------

## Event

```text
RuntimeEvent
      ↓
RuntimeEventChannel
      ↓
sequence ownership
      ↓
Journal first
      ↓
other consumers / Trace
```

冻结：

```text
Journal = durable event truth
Trace   = observability side-channel
```



------

## Output / Memory

```text
Final candidate
      ↓
OutputGate
      ↓
DELIVERED ?
 ┌────┴────┐
YES        NO
 ↓          ↓
Memory    No final
Writer    business memory
```

冻结：

```text
OutputGate
= final publication owner

RunFinalMemoryWriter
= DELIVERED_ONLY
```

------

## Trace Delivery

```text
accepted envelope
       ↓
queue
       ↓
transport attempt
       ↓
0 or 1 attempt
```

冻结的是：

```text
BEST_EFFORT
+
AT_MOST_ONE_TRANSPORT_ATTEMPT_PER_ACCEPTED_ENVELOPE
```

而不是：

```text
exactly-once
```

------

# 8. 数据 / 权限 / Owner

整个 Stage 3.5 最适合面试追问的就是 Owner Matrix。

## Runtime

```text
Plan / PlanStep
→ static definition Owner

AgentState
→ runtime state Owner
```

------

## Lifecycle

```text
server.py::lifespan()
→ unique Production Composition Root

RunCoordinator
→ per-run terminal Owner

GracefulShutdownCoordinator
→ shutdown orchestration Owner

ApplicationRuntimeServices
→ application-scoped services Owner
```



------

## Tool

```text
ToolRegistry
→ identity / discovery / descriptor / binding

ToolGovernanceService
→ permission / risk / approval

ResourceAuthorizationService
→ resource/path authorization

ToolExecutionService
→ sole production execution Owner
```

而：

```text
AgentRegistry
≠ Tool permission Owner
```



------

## Event / Observability

```text
RuntimeEventChannel
→ event sequence Owner

Journal
→ durable event facts Owner

Trace
→ observability
```

------

## Output

```text
OutputGate
→ final publication Owner

RunFinalMemoryWriter
→ final business Memory commit Owner
```

------

## Cross-system Trace

```text
Trace Contract
→ LocalAgent semantic Owner

serialize_trace_export_envelope()
→ sole wire serialization Owner

AgentEvalOps sidecar
→ authoritative persisted envelope truth

legacy Trace / Span
→ compatibility projection
```

------

# 9. 兼容策略

Stage 3.5 真正冻结的是：

> **行为兼容，而不是源码兼容。**

如果未来修改：

```text
queue.Queue
→ asyncio.Queue
```

只要冻结语义仍成立，它可能不是 Breaking Change（破坏性变更）。

但是：

```text
AgentState
不再是唯一 runtime truth
```

即使 API 一个字段都没变，也属于 Breaking Change。

------

## 当前 Breaking Change 规则

最终 Freeze 明确以下属于 Breaking：

- 删除/重命名 frozen public field；
- 改 accepted semantic domain；
- 改 required/optional presence；
- 改 wire field set；
- 改 payload bound；
- 改 version/fingerprint semantics；
- 改 canonical Owner；
- 改 Tool execution Owner；
- 改 RuntimeEvent sequence Owner；
- 改 journal-first；
- Recovery 超过 validation-only；
- 改 OutputGate Owner；
- 改 DELIVERED-only Memory；
- 改 Trace delivery guarantee；
- 改 AgentEvalOps endpoint/status/commit/sidecar authority。

------

## Non-breaking

只要行为保持：

```text
private helper refactor
Scheduler algorithm replacement
queue/thread replacement
logging changes
metrics changes
PycURL handle reuse change
private DTO rewrite
```

通常不需要 version bump。

------

# 10. Bad Cases

## Bad Case 1：把内部实现误冻成 API

错误：

```text
PycURL easy-handle reuse
= frozen architecture
```

问题：

以后连连接策略都不能优化。

最终：

```text
INTERNAL_IMPLEMENTATION
DO_NOT_FREEZE
```

------

## Bad Case 2：只冻结 Wire，不冻结 Owner

如果只关心 JSON schema，却不冻结：

```text
ToolExecutionService
= sole execution Owner
```

未来 MCP 很可能新增第二执行路径。

所以：

> **Owner 本身也是 Contract。**

------

## Bad Case 3：AgentState 与 Plan 双写

未来如果：

```text
PlanStep.status
```

重新出现，就破坏：

```text
Plan = static
AgentState = mutable runtime truth
```

属于 Breaking Change。

------

## Bad Case 4：Tool Registry 兼任 Permission Owner

错误架构：

```text
ToolRegistry
= discovery
+ permission
+ execution
```

最终冻结四层 Owner，防止职责重新聚合。

------

## Bad Case 5：Resource Authorization 被说成 Sandbox

冻结明确：

```text
Resource Authorization
≠ Sandbox
```

否则安全能力会被文档夸大。



------

## Bad Case 6：Recovery 逐渐“偷偷升级”

当前：

```text
RecoveryValidator
→ assessment
```

如果以后直接增加：

```text
resume()
```

这不是普通 implementation enhancement（实现增强），而是 Contract change。

因为它引入：

```text
side-effect replay
external consistency
idempotency
execution ownership
```

新问题。

------

## Bad Case 7：Output at-most-once 被误写成 Transport at-most-once

冻结特别防止这个混淆。

```text
OutputGate:
final publication at-most-once
```

和：

```text
Trace Delivery:
at most one TRANSPORT ATTEMPT
```

完全不是同一个语义。

------

## Bad Case 8：文档比代码跑得快

WP1 审计发现：

```text
DOC_DRIFT = 1
```

例如正式文档仍写：

```text
WP4-C future
pending Gate
```

但真实系统已经跨系统 E2E PASS。

WP2 专门将其修成：

```text
DOC_DRIFT = 0
```



------

# 11. Tests / Gates：只列真实执行

## WP1

```text
LocalAgent focused
307 passed

AgentEvalOps focused unit
143 passed
```



------

## WP2

文档实施阶段没有跑全仓。

真实执行：

```text
git diff --check
PASS
```

以及轻量源码探针验证：

```text
Trace version
Fingerprint
WIRE_FIELDS = 16
payload bound = 16384
MAX_V1_DURATION_INT
```



------

## WP3 Final Gate

### Direct Contract Probe

PASS。

实际验证包括：

```text
AgentState schema = 1
RuntimeEvent writer = 2
Journal writer = 2
Snapshot = 1
Trace Contract = 1
Trace identity
Trace fingerprint
6 operations
Envelope identity/version
16 fields
16384 bound
MAX_V1_DURATION_INT
```



### Targeted

```text
546 passed
1 deselected
9 subtests passed
exit 0
```

### Full Regression

```text
2467 passed
13 deselected
4 warnings
42 subtests passed
exit 0
```

### Static

```text
compileall
PASS

uv lock --check
PASS

git diff --check
PASS
```



------

# 12. Known Limitations

Stage 3.5 **没有消灭 Known Limitations**，而是把它们正式纳入冻结边界。

## P2 = 6

1. planning executor starvation；
2. untrusted natural-language/data semantic influence；
3. System Prompt disclosure/rewriting risk；
4. delivery/final-memory negative path 缺 symmetric spans；
5. planning/step error taxonomy 可能折叠到 `UNHANDLED_ERROR`；
6. AgentEvalOps legacy delete divergence。

------

## 其他 Known Limitations

```text
single-process Windows-native

force-kill 可绕过 graceful shutdown

Trace export queue ephemeral

无 production deployment / SLA / HA / capacity proof

Snapshot opt-in

Recovery validation-only

无 durable human approval pause/resume

无 generic WAF

无 generic DLP

无 full Sandbox
```

------

## Deferred

```text
automatic recovery / replay

durable trace retry / batching / outbox

production fault / chaos

generic consumer-neutral / OTLP wire

multi-process / HA
```

这些不是 Stage 3.5 blocker。

------

# 13. 体现的工程能力

Stage 3.5 和前几个开发 WP 的面试价值完全不同。

## ① Contract Engineering（合同工程）

能够回答：

```text
什么是 API？
什么是 Contract？
什么只是实现？
```

这是中大型系统维护的重要能力。

------

## ② Ownership Design（所有权设计）

能明确系统中：

```text
谁拥有 State
谁拥有 Sequence
谁拥有 Execution
谁拥有 Publication
谁拥有 Memory
谁拥有 Durable Truth
```

------

## ③ Compatibility Engineering（兼容性工程）

能够定义：

```text
version
fingerprint
semantic domain
breaking change
non-breaking refactor
```

而不是只依赖单元测试。

------

## ④ Architecture Governance（架构治理）

Stage 3.5 本质上是在防止未来代码自然腐化：

```text
Second Owner
Semantic Drift
Capability Inflation
Scope Creep
```

------

## ⑤ Scope Control（范围控制）

一个比较成熟的点是：

> **没有借 Freeze 的名义继续优化系统。**

Stage 3 已经 PASS，就停止加功能，只冻结真正需要冻结的东西。

------

# 14. 30 秒面试版本

> Stage 3 最小生产化完成后，我又做了一个轻量的 Contract Freeze v1，主要解决后续 MCP、RAG、Memory 等开发可能破坏现有 Runtime 边界的问题。
>
> 我先对当前源码做 Contract Inventory，把 30 个候选分成 Public Stable、Public Versioned、Protected Internal Contract、Internal Implementation 和 Deferred，最终只冻结其中 22 个，另外 8 个明确保留重构空间。
>
> 冻结内容不仅包括 Trace v1 这样的 Wire Contract，也包括 AgentState 是唯一 Runtime State Owner、ToolExecutionService 是唯一 Tool 执行 Owner、OutputGate 是 Final Publication Owner、Recovery 只支持 Validation-only 这些架构语义。
>
> Final Gate 最终 P0/P1、Owner conflict、Contract ambiguity 都为 0，全量 2467 个测试通过，Stage 3 正式成为 `FROZEN_BASELINE_V1`。

------

# 15. 2 分钟面试版本

> LocalAgent Stage 3 完成以后，我没有直接继续堆功能，而是加了一个 Stage 3.5 Contract Freeze，目的是给后续 AgentEvalOps、MCP、高级 RAG 和 Memory 开发提供稳定兼容基线。
>
> 第一步是 Contract Inventory。我没有把所有核心类都当 API，而是定义了五类稳定级别。最终审计 30 个候选，22 个进入 Freeze，8 个明确不冻结。
>
> Public Versioned 主要有 AgentState、RuntimeEvent、JournalRecord、Snapshot、Trace Contract、TraceExportEnvelope 和 AgentEvalOps ingest API；而 Tool Registry、Governance、Resource Authorization、ToolExecutionService、OutputGate、Final Memory、Recovery 等虽然不是公开 Wire API，但它们的 Owner 和行为属于 Protected Internal Contract。
>
> 例如我冻结了 AgentState 是 Runtime Mutable State 的 Single Source of Truth，Plan 只能保存静态定义；ToolExecutionService 是唯一生产 Tool 执行 Owner；OutputGate 是 Final Publication Owner；Final Memory 只允许 DELIVERED output；Recovery 只能做 Validation，不能静默增加 resume 或 replay。
>
> 同时我刻意没有冻结 Scheduler 算法、queue/thread、PycURL handle、TraceContext/SpanHandle 和 JSONB TypeDecorator 这种内部实现，否则未来系统基本无法演进。
>
> 最终生成唯一 `STAGE3_CONTRACT_FREEZE_V1.md`，修复了现有文档 Drift，再用独立 Final Gate 从当前源码重新验证 Owner、Version、Fingerprint、16 个 Trace Wire Fields、16384-byte payload bound 等内容。最终 targeted 546 passed，全仓 2467 passed，P0/P1、Contract ambiguity、Owner conflict 都为 0，所以 Stage 3 被正式冻结为 `FROZEN_BASELINE_V1`。

------

# 16. 深入版本：四条主线

面试深入追问时，不建议按 WP1/WP2/WP3 流水账讲。

建议讲四条主线。

## 主线 A：Contract ≠ Class

判断一个东西是不是合同，看：

```text
consumer dependency
semantic stability
compatibility obligation
ownership invariant
```

而不是：

```text
它是不是一个 class
```

------

## 主线 B：Owner is Contract

Agent Runtime 的很多 Bug 根源其实不是类型不安全，而是：

```text
Two Owners
```

例如：

```text
State Owner x2
Execution Owner x2
Final Output Owner x2
```

所以 Stage 3.5 把：

```text
Single Source of Truth
Single Execution Owner
Single Publication Owner
```

正式当成 Contract。

------

## 主线 C：Freeze Behavior, Not Mechanics

最核心设计原则：

```text
Frozen Semantics
+
Replaceable Implementation
```

这样既保证兼容性，又保留演进能力。

------

## 主线 D：Truthful Capability Boundary

Freeze 不只是防代码变化。

也是防：

```text
documentation drift
interview exaggeration
capability inflation
```

所以把：

```text
Production proven = NO
Automatic recovery = NO
Exactly-once = NO
```

也正式写进冻结边界。

------

# 17. 高频追问

## Q1：为什么 Stage 3 都 PASS 了，还需要 Freeze？

因为 PASS 说明：

> 当前实现满足当前 Gate。

Freeze 解决的是：

> **未来修改时，哪些语义不能被无声改变。**

它解决的是 evolution governance（演进治理），不是当前功能正确性。

------

## Q2：为什么 Owner 也要冻结？

例如未来新增 MCP，如果 MCP 直接执行 Tool：

```text
MCP
→ adapter.execute()
```

就绕过：

```text
ToolGovernance
ResourceAuthorization
ToolExecutionService
```

即使接口类型全部没变，安全架构已经被破坏。

所以：

```text
execution ownership
```

本身就是合同。

------

## Q3：为什么 Scheduler 不冻结实现？

因为真正消费者依赖的是：

```text
dependency-before-claim
policy-limited parallel execution
unique final source
```

而不是：

```text
当前 for-loop 的顺序
```

所以算法可以替换。

------

## Q4：什么时候必须升级 Contract version？

当修改：

```text
public schema
semantic domain
wire fields
Owner
delivery guarantee
```

等 frozen semantics 时，需要显式兼容决策。

纯内部 refactor 不需要。

------

## Q5：Fingerprint 有什么作用？

Trace Contract v1 当前 fingerprint：

```text
6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab
```

它不是 hash 某个 Python 类源码，而是用于标识冻结 semantic descriptor（语义描述）。

这样 Consumer 能判断：

> Producer 的“version=1”是否真的是自己认识的那个 v1 semantics。



------

## Q6：为什么 Recovery 从 Validation 变成 Resume 是 Breaking Change？

因为 resume 不只是多调一个方法。

它会立即引入：

```text
tool side-effect replay
external state consistency
idempotency
run ownership
state reconstruction
```

所以必须重新设计，而不能在原 v1 里偷偷增加。

------

## Q7：为什么数据库 TypeDecorator 不冻结？

因为合同要求的是：

```text
accepted numeric semantic value
must survive persistence
```

而不是：

```text
必须使用当前这个 TypeDecorator
```

未来完全可以换 persistence 实现。

------

## Q8：Freeze 之后还能重构吗？

当然可以。

这正是 Explicitly Non-Frozen Internals（明确非冻结内部实现）存在的目的。

只要：

```text
Frozen semantics unchanged
```

就可以替换实现。

------

# 18. 最容易夸大的地方

### ❌ “Stage 3.5 把代码彻底冻结了”

错误。

正确：

> **冻结合同，不冻结所有代码。**

------

### ❌ “30 个候选全部被冻结”

错误。

```text
30 reviewed
22 frozen
8 explicitly not frozen
```



------

### ❌ “所有冻结合同都是 Public API”

错误。

其中：

```text
PROTECTED_INTERNAL_CONTRACT = 10
```

它们冻结的是内部架构 Owner / Semantics。

------

### ❌ “FROZEN 意味着永远不能 Breaking Change”

不是。

可以 Breaking，但必须：

```text
explicit architecture decision
compatibility/version decision
migration strategy
new gate
```

而不是偷偷修改。

------

### ❌ “Stage 3 Freeze 证明已经 Production Ready”

仍然不能这么讲。

最终明确：

```text
Production proven = NO
```



------

### ❌ “Recovery 已经支持恢复”

错误。

```text
Recovery = VALIDATION_ONLY
```

------

### ❌ “Trace 支持 exactly-once”

错误。

```text
BEST_EFFORT
+
AT_MOST_ONE_TRANSPORT_ATTEMPT_PER_ACCEPTED_ENVELOPE
```

------

### ❌ “Stage3.5 修复了全部 P2”

没有。

```text
P2 = 6
```

全部继续保留。

------

# 19. P0 / P1 / P2 复习

最终 Stage 3.5：

```text
P0 = 0
P1 = 0
P2 = 6

Contract ambiguity = 0
Owner conflict = 0
DOC_DRIFT = 0
```



6 个 P2：

| P2                                                | 当前边界       |
| ------------------------------------------------- | -------------- |
| Planning executor starvation                      | `ACCEPTED_P2`  |
| Untrusted language/data semantic influence        | 已接受安全限制 |
| System Prompt disclosure/rewriting                | 已接受安全限制 |
| Negative delivery/memory paths 无 symmetric spans | `ACCEPTED_P2`  |
| Planning/step taxonomy → `UNHANDLED_ERROR`        | `DEFERRED`     |
| AgentEvalOps legacy delete divergence             | `ACCEPTED_P2`  |

Stage 3.5 最重要的是：

> **P2 可以冻结存在；P0/P1、Owner conflict、Contract ambiguity 不能带进 v1。**

------

# 20. 速查表

| 项目                        | Stage 3.5 最终事实                  |
| --------------------------- | ----------------------------------- |
| 阶段                        | Contract Freeze v1                  |
| WP1                         | Contract Inventory PASS             |
| WP2                         | Freeze Docs PASS                    |
| WP3                         | Final Gate PASS                     |
| Reviewed                    | 30                                  |
| Frozen                      | 22                                  |
| Not Frozen                  | 8                                   |
| Public Stable               | 5                                   |
| Public Versioned            | 7                                   |
| Protected Internal          | 10                                  |
| Internal Implementation     | 4                                   |
| Deferred                    | 4                                   |
| Contract ambiguity          | 0                                   |
| Owner conflict              | 0                                   |
| Runtime mutable truth       | `AgentState`                        |
| Plan                        | Static                              |
| Composition Root            | `server.py::lifespan()`             |
| Tool identity               | `ToolRegistry`                      |
| Tool policy                 | `ToolGovernanceService`             |
| Resource auth               | `ResourceAuthorizationService`      |
| Tool execution              | `ToolExecutionService`              |
| Event sequence              | `RuntimeEventChannel`               |
| Durable event truth         | Journal                             |
| Snapshot                    | v1 / opt-in                         |
| Recovery                    | `VALIDATION_ONLY`                   |
| Final publication           | `OutputGate`                        |
| Final Memory                | `DELIVERED_ONLY`                    |
| Trace Contract              | v1                                  |
| Fingerprint                 | `6fc033bb...390ab`                  |
| Trace operations            | 6                                   |
| Envelope                    | v1 / 16 fields                      |
| Payload bound               | 16384 bytes                         |
| Serializer                  | `serialize_trace_export_envelope()` |
| Delivery                    | Best effort + ≤1 attempt            |
| AgentEvalOps first write    | 201                                 |
| Replay                      | 200                                 |
| Conflict                    | 409                                 |
| Sidecar                     | Authoritative envelope truth        |
| Production fault activation | NO                                  |
| Generic WAF/DLP/Sandbox     | NOT_IMPLEMENTED                     |
| Production proven           | NO                                  |
| Targeted Final Gate         | 546 passed                          |
| Full regression             | 2467 passed + 42 subtests           |
| P0                          | 0                                   |
| P1                          | 0                                   |
| P2                          | 6                                   |
| DOC_DRIFT                   | 0                                   |
| Stage 3.5                   | **PASS**                            |
| Contract Freeze v1          | **FROZEN**                          |
| Stage 3                     | **FROZEN_BASELINE_V1**              |
| 后续开发                    | **READY**                           |

------

# 推荐面试材料文件名

整个 Stage 3.5 不再用某个 WP 的名字。

我推荐：

```text
STAGE3_5_CONTRACT_FREEZE_V1_INTERVIEW_LEARNING.md
```

这份应该成为 **Stage 3.5 的最终权威面试材料**。

另外还有最后一个很小的行政动作需要记住：

```text
docs/contracts/STAGE3_CONTRACT_FREEZE_V1.md

Status:
FROZEN_CANDIDATE_PENDING_FINAL_GATE

→

FROZEN
```

Final Gate 已明确 `STATUS_UPDATE_REQUIRED=YES`，这是 **PASS 后的一行 docs-only 状态同步**，不需要再做工程 Gate。

整个 Stage 3.5 最适合你在面试里传达的一句话是：

> **我不是把当前实现全部锁死，而是明确冻结了系统已经验证成立的 API、状态语义和唯一 Owner，同时把算法、线程、Queue、Transport 细节留在可重构范围内，这样后续 MCP、RAG、Memory 等能力可以继续演进，但不能无声破坏 Runtime 的核心合同。**