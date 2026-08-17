# Stage 3.5 — WP1 Contract Inventory & Freeze Boundary 学习 / 面试总结

------

# 1. 一句话项目 / 工作包定义

Stage3.5-WP1 的目标是：

> **基于当前生产源码、测试和最新独立 Gate，对 LocalAgent Stage 2～3 已稳定的 Contract（合同）、Owner（所有权）、Behavioral Semantics（行为语义）进行一次权威盘点，区分哪些应该冻结成 v1，哪些必须保持内部可重构，从而为 Stage3.5 Contract Freeze v1 建立准确边界。**

它不是开发新功能，而是在回答：

```text
什么必须稳定？
什么可以继续改？
什么变化算 Breaking Change？
```

------

# 2. 为什么做

如果直接进入 Freeze，很容易犯两个相反错误。

第一种是**冻结不足**：

```text
ToolExecutionService 的唯一执行 Owner
OutputGate 的 Final Publication Owner
Recovery = VALIDATION_ONLY
```

这种重要架构边界如果没冻结，后续 MCP、Skill、Memory 等开发很可能重新引入第二套 Owner。

第二种是**过度冻结**：

```text
scheduler 排序算法
queue.Queue
thread implementation
PycURL easy-handle reuse
private helper
```

如果这些都当成 v1 Contract，以后任何内部优化都变成 Breaking Change。

所以 WP1 的核心作用就是：

> **Freeze semantics，而不是 freeze implementation。**

------

# 3. 真实性与完成边界

## 已真实完成

本 WP 实际完成了：

- 当前源码审计；
- 当前正式合同文档审计；
- Owner 识别；
- Stability Level（稳定等级）分类；
- Breaking Change Trigger（破坏性变更触发条件）定义；
- 当前 P2 / Known Limitation 复核；
- DOC_DRIFT 定位；
- Freeze v1 候选表；
- WP2 最小文档方案。

最终：

```text
Contract ambiguity = 0
NEEDS_RESOLUTION = 0
```



## 已真实测试

本轮执行：

```text
LocalAgent focused:
307 passed

AgentEvalOps focused unit:
143 passed
```

AgentEvalOps Integration 没有本轮重复执行，而是复用了相同 HEAD 下 WP4-C Final Gate 已通过的：

```text
481 unit
355 integration
real cross-system E2E PASS
```



## 尚未完成

还没有：

```text
STAGE3_CONTRACT_FREEZE_V1.md
正式 Freeze
DOC_DRIFT 修复
Freeze Final Gate
```

所以：

```text
Stage3.5 Complete = NO
```

------

# 4. 修改前架构与根因

进入 WP1 前，系统已经有大量“事实上的合同”：

```text
RunContext
AgentState
Plan
RuntimeEvent
Journal
Snapshot
Tool Contract
Trace Contract
OutputGate
AgentEvalOps API
...
```

但这些内容散落在：

```text
production source
tests
runtime docs
Stage2/2.5 contract docs
Stage3 Gates
```

缺少一个统一的：

```text
Contract Inventory
```

因此存在三个风险：

### 风险一：Owner 漂移

例如未来 MCP 增加一套 Tool 执行入口，绕过：

```text
ToolExecutionService
```

### 风险二：能力声明漂移

例如把：

```text
Recovery = VALIDATION_ONLY
```

逐渐写成：

```text
Automatic Recovery
```

### 风险三：实现细节被误认成 Contract

例如把：

```text
PycURL easy handle reuse
```

当成不可改变的架构。

WP1 就是为了解决这三个问题。

------

# 5. 方案讨论与取舍

## 方案 A：所有重要类都冻结

拒绝。

因为：

```text
Class existence
≠ Contract
```

一个类可能只是当前实现载体。

------

## 方案 B：只冻结 Public API

也不够。

因为 LocalAgent 有很多最关键边界并不是 public wire API，例如：

```text
OutputGate = final publication owner

ToolExecutionService
= sole production tool execution owner

Recovery
= validation-only
```

这些都属于：

```text
PROTECTED_INTERNAL_CONTRACT
```

------

## 最终分类模型

采用五级：

```text
A. PUBLIC_STABLE

B. PUBLIC_VERSIONED

C. PROTECTED_INTERNAL_CONTRACT

D. INTERNAL_IMPLEMENTATION

E. NOT_FROZEN / DEFERRED
```



这是本 WP 最值得学习的设计。

------

# 6. 最终架构

最终 30 个候选被分类为：

```text
PUBLIC_STABLE = 5

PUBLIC_VERSIONED = 7

PROTECTED_INTERNAL_CONTRACT = 10

INTERNAL_IMPLEMENTATION = 4

NOT_FROZEN / DEFERRED = 4
```

其中真正：

```text
FREEZE_V1 = 22
DO_NOT_FREEZE = 8
```



核心结构可以理解为：

```text
                    Contract Freeze v1
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
 Public Contract     Protected Internal    Non-Frozen
        │               Invariant             │
 wire/schema/type      Owner/semantic      implementation
 version/fingerprint   lifecycle/order       mechanics
```

------

# 7. 核心状态机和时序

WP1 没有新增 Runtime 状态机，但它冻结了几个关键生命周期语义。

## Runtime lifecycle

```text
server.py::lifespan()
        ↓
ApplicationRuntimeServices
        ↓
RunCoordinator
        ↓
GracefulShutdownCoordinator
```

关键 Owner：

```text
server.py::lifespan()
= unique production Composition Root

RunCoordinator
= per-run terminal owner

GracefulShutdownCoordinator
= shutdown orchestration owner
```



## Event / Journal

```text
RuntimeEvent
        ↓
RuntimeEventChannel sequence
        ↓
Journal first
        ↓
other observers / Trace
```

冻结：

```text
Journal
= durable event facts

Trace
≠ durable event store
```

------

# 8. 数据 / 权限 / Owner

这是 WP1 最核心的一部分。

## Runtime

```text
Plan / PlanStep
= static definition

AgentState
= runtime mutable state Single Source of Truth
```



## Tool

```text
ToolRegistry
= identity / discovery / descriptor / adapter binding

ToolGovernanceService
= permission / risk / approval authority

ResourceAuthorizationService
= resource/path authorization

ToolExecutionService
= sole production execution owner
```

同时：

```text
AgentRegistry
≠ Tool permission owner
```



## Output / Memory

```text
OutputGate
= final publication owner

RunFinalMemoryWriter
= final business memory commit owner
```

Memory 必须：

```text
DELIVERED-only
per-run write-once
```

## Trace

```text
Trace Contract semantic owner
= trace_contract.py + trace_export_contract.py

Wire serializer owner
= serialize_trace_export_envelope(...)
```

------

# 9. 兼容策略

WP1 给出的核心兼容原则是：

> **Breaking Change 取决于冻结语义是否变化，而不是代码文件是否变化。**

例如下面属于 Breaking：

```text
删除 public field

改变 accepted semantic domain

改变 required / optional presence

改变 Contract version/fingerprint semantics

改变 canonical Owner

改变 delivery semantics

改变 Tool execution ownership

改变 OutputGate / Memory ownership

改变 Recovery 的 validation-only 定义
```

而下面通常不属于：

```text
更换 queue implementation

更换 scheduler algorithm

更换 thread/executor

调整 PycURL handle reuse

private helper refactor

logging / metrics internal changes
```

前提是 frozen behavior 不变。

------

# 10. Bad Cases

## Bad Case 1：Plan 和 AgentState 职责重新混淆

**真实性：当前 WP 作为 protected historical boundary 复核，没有发现当前回归。**

错误设计：

```text
PlanStep.status
AgentState.status
```

形成双写。

冻结后：

```text
Plan / PlanStep
= static

AgentState
= runtime truth
```

------

## Bad Case 2：AgentRegistry 开始控制 Tool 权限

风险：

```text
Agent Registry
同时负责 Agent identity
又负责 Tool permission
```

会让 Tool Governance Owner 变得模糊。

冻结：

```text
AgentRegistry
= identity / capability / delegation only
```

Tool 权限由 Tool Governance 独立拥有。

------

## Bad Case 3：Trace 替代 Journal

错误方向：

```text
Trace data
→ Recovery source of truth
```

冻结事实：

```text
Journal
= durable event facts

Trace
= observability
```

不能反转。

------

## Bad Case 4：Recovery Validator 被逐步写成 Recovery Executor

当前可能计算：

```text
resume_prerequisites_satisfied
```

但这不等于真正恢复执行。

冻结：

```text
Recovery = VALIDATION_ONLY
```

没有：

```text
resume
replay
AgentState writeback
automatic continue
```



------

## Bad Case 5：把 Output at-most-once 和 Transport exactly-once 混淆

OutputGate 有：

```text
final publication at-most-once
```

但 Trace transport 是：

```text
BEST_EFFORT
+
AT_MOST_ONE_TRANSPORT_ATTEMPT_PER_ACCEPTED_ENVELOPE
```

完全不是同一个保证。

------

## Bad Case 6：把内部实现冻死

例如：

```text
PycURL easy-handle reuse
Scheduler scan order
Thread implementation
```

如果这些都进入 Freeze v1，以后优化会非常痛苦。

因此 WP1 明确列入：

```text
INTERNAL_IMPLEMENTATION
```

------

# 11. Tests / Gate：只写真实执行过的

本 WP：

```text
LocalAgent focused
= 307 passed

AgentEvalOps focused unit
= 143 passed
```

没有重新运行 AgentEvalOps integration。

复用的最新独立证据：

```text
AgentEvalOps unit = 481 passed
AgentEvalOps integration = 355 passed
WP4-C real E2E = PASS
```



最终：

```text
P0 = 0
P1 = 0
Contract ambiguity = 0
```

------

# 12. Known Limitations

本 WP 保留 Stage3 的 6 个 P2：

1. planning executor starvation；
2. untrusted natural-language / data 影响模型语义；
3. System Prompt disclosure / rewriting risk；
4. delivery/final-memory negative path 缺 symmetric span；
5. planning/step error taxonomy 可能折叠为 `UNHANDLED_ERROR`；
6. AgentEvalOps legacy delete divergence。

另外 Known Limitations：

```text
single-process Windows-native

force-kill 可绕过 graceful shutdown

export queue ephemeral

无 production deployment / SLA / HA / capacity proof

Snapshot opt-in

Recovery validation-only

无 durable human approval pause/resume

无 generic WAF / DLP / Sandbox
```

------

# 13. 体现出的工程能力

这一 WP 主要体现五类能力。

## ① Contract Design（合同设计）

能够区分：

```text
data structure
API
semantic guarantee
ownership invariant
implementation detail
```

------

## ② API Stability Thinking（API 稳定性思维）

不是所有东西都 versioned。

例如：

```text
PUBLIC_STABLE
```

可以稳定但没有 schema version。

而：

```text
RuntimeEvent
RunSnapshot
Trace Contract
```

则属于：

```text
PUBLIC_VERSIONED
```

------

## ③ Architectural Ownership（架构所有权）

能明确：

```text
谁拥有 state
谁拥有 permission
谁拥有 execution
谁拥有 final output
谁拥有 durable truth
```

这对 Agent Runtime 非常重要。

------

## ④ Change Management（变更管理）

能够定义：

```text
什么改动需要 version bump / architecture decision
什么只是内部 refactor
```

------

## ⑤ Preventing Over-Engineering（防止过度工程）

Freeze 本来很容易变成：

```text
所有东西都不能动
```

WP1 做到的是：

> **只冻结值得稳定的行为，主动给内部实现留下重构空间。**

------

# 14. 30 秒面试版本

> Stage 3 完成后我没有直接继续开发，而是先做了一次 Contract Inventory，为 Contract Freeze v1 确认边界。核心不是把所有类都冻结，而是区分 Public Stable、Public Versioned、Protected Internal Contract 和纯 Implementation。
>
> 最终识别了 30 个候选，其中 22 个进入 Freeze v1，8 个明确不冻结。比如 AgentState 是 runtime mutable state 的 Single Source of Truth，ToolExecutionService 是唯一生产 Tool 执行 Owner，OutputGate 是 final publication Owner，Recovery 明确只支持 Validation-only；而 Scheduler 算法、queue/thread、PycURL handle 等内部实现继续允许重构。
>
> 这一步最终 P0/P1 都为 0，也没有未解决的合同歧义，为下一步正式 Contract Freeze 做好了边界。

------

# 15. 2 分钟面试版本

> Stage 3 Production Readiness PASS 后，我增加了一个很轻量的 Stage 3.5 Contract Freeze。第一步不是直接写冻结文档，而是先做 Contract Inventory，因为我不希望把当前实现细节错误地变成长期 API。
>
> 我把合同分成五类：Public Stable、Public Versioned、Protected Internal Contract、Internal Implementation 和 Not Frozen。
>
> Public Versioned 主要包括 AgentState、RuntimeEvent、JournalRecord、RunSnapshot、Trace Contract、TraceExportEnvelope 和 AgentEvalOps ingest API；这些有明确 schema version、fingerprint 或跨系统协议。
>
> Protected Internal Contract 则主要冻结 Owner 和行为，例如 `server.py::lifespan()` 是唯一 Composition Root，ToolRegistry 只负责 identity/discovery，ToolGovernance 负责 risk/permission/approval，ResourceAuthorization 负责资源权限，ToolExecutionService 是唯一生产执行 Owner；OutputGate 是 final publication Owner，Final Memory 只写 DELIVERED output，Recovery 明确保持 validation-only。
>
> 同时我明确把 Scheduler 的扫描算法、queue/thread、PycURL easy handle、TraceContext/SpanHandle 等归为内部实现，不进入 Freeze，这样以后还可以优化。
>
> 最后确认 30 个候选里 22 个应该 Freeze v1，没有 Owner 冲突，没有 Contract ambiguity，P0/P1 都为 0。下一步只需要生成一份权威 Freeze 文档，并同步当前两个过时状态文档。

------

# 16. 深入版本：三条主线

## 主线 A：Contract 不等于 Class

```text
class Foo
≠
public contract
```

判断依据应该是：

```text
有无消费者依赖
有无稳定语义
有无 compatibility obligation
有无 Owner invariant
```

------

## 主线 B：Owner 本身就是 Contract

在 Agent 系统里很多最关键的稳定性来自：

```text
Single Source of Truth
Single Execution Owner
Single Publication Owner
```

例如：

```text
AgentState
ToolExecutionService
OutputGate
```

所以即使它们不是网络 API，也必须冻结。

------

## 主线 C：Freeze Behavior, Not Mechanics

核心思想：

```text
Behavior stable
Implementation replaceable
```

这是 Stage3.5 最重要的设计原则。

------

# 17. 高频追问

### Q1：为什么不把 Scheduler 算法冻结？

因为业务依赖的是：

```text
dependency satisfied before claim
policy-limited parallelism
single final source
```

而不是当前具体扫描顺序或数据结构。

所以冻结语义，不冻结算法。

------

### Q2：PUBLIC_STABLE 和 PUBLIC_VERSIONED 有什么区别？

`PUBLIC_STABLE`：

> 外部/项目代码可以稳定依赖，但当前没有独立 schema version。

`PUBLIC_VERSIONED`：

> 有明确版本演进和 compatibility 规则，例如 RuntimeEvent v1/v2、Snapshot v1、Trace Contract v1。

------

### Q3：为什么 Tool Platform 是 Protected Internal，而不是 Public API？

因为重点不是让外部系统直接消费这些内部 Service，而是保护生产 Owner：

```text
Registry
Governance
Authorization
Execution
```

的职责边界。

------

### Q4：为什么 Recovery 要进入 Freeze？

因为能力声明非常容易漂移。

当前真正实现的只有：

```text
validation
```

如果以后要升级为 execution/replay，应该显式重新做 architecture/version decision，而不是偷偷扩展。

------

### Q5：为什么 AgentEvalOps numeric JSONB TypeDecorator 不冻结？

因为外部合同要求的是：

```text
合法数字语义不丢失
```

而不是：

```text
必须永远使用 TypeDecorator
```

未来可以更换 persistence implementation，只要 frozen semantic 不变。

------

# 18. 最容易夸大 / 答错的地方

### ❌ “Stage3.5 已经完成”

还没有。

当前：

```text
WP1 PASS
WP2 未开始
WP3 未开始
```

------

### ❌ “30 个东西都被冻结”

不对。

```text
30 candidates
22 FREEZE_V1
8 DO_NOT_FREEZE
```

------

### ❌ “内部类以后不能改”

错误。

`INTERNAL_IMPLEMENTATION` 就是明确保留重构空间。

------

### ❌ “TraceContext / SpanHandle 是公开合同”

当前审计结论是：

```text
INTERNAL_IMPLEMENTATION
```



------

### ❌ “Recovery v1 支持 resume”

没有。

当前：

```text
Recovery = VALIDATION_ONLY
```

------

### ❌ “ToolRegistry 决定 Agent 能不能调用 Tool”

不是。

权限 Owner 是：

```text
ToolGovernanceService
```

ToolRegistry 负责：

```text
identity / discovery / descriptor / binding
```

------

# 19. P0 / P1 / P2 复习

当前：

```text
P0 = 0
P1 = 0
P2 = 6
```

本 WP 最关键的是：

```text
Contract ambiguity = 0
NEEDS_RESOLUTION = 0
```

因为如果存在：

```text
两个 execution owner
两个 state truth
两个 serializer owner
```

即使测试暂时全绿，也不应该进入 Freeze。

本次没有发现这种问题。

------

# 20. 速查表

| 项目                    | 当前结论                                      |
| ----------------------- | --------------------------------------------- |
| WP                      | Stage3.5-WP1                                  |
| 类型                    | Contract Inventory                            |
| 生产代码修改            | NO                                            |
| Freeze candidates       | 30                                            |
| 真正 Freeze v1          | 22                                            |
| 不冻结/延后             | 8                                             |
| PUBLIC_STABLE           | 5                                             |
| PUBLIC_VERSIONED        | 7                                             |
| PROTECTED_INTERNAL      | 10                                            |
| INTERNAL_IMPLEMENTATION | 4                                             |
| NOT_FROZEN / DEFERRED   | 4                                             |
| Contract ambiguity      | 0                                             |
| Run mutable truth       | `AgentState`                                  |
| Plan                    | Static                                        |
| Composition Root        | `server.py::lifespan()`                       |
| Tool identity           | `ToolRegistry`                                |
| Tool policy             | `ToolGovernanceService`                       |
| Resource permission     | `ResourceAuthorizationService`                |
| Tool execution          | `ToolExecutionService`                        |
| Event sequence          | `RuntimeEventChannel`                         |
| Durable event facts     | Journal                                       |
| Recovery                | `VALIDATION_ONLY`                             |
| Final publication       | `OutputGate`                                  |
| Final Memory            | `DELIVERED_ONLY`                              |
| Trace version           | 1                                             |
| Trace fingerprint       | `6fc033bb...390ab`                            |
| Trace operations        | 6                                             |
| Trace delivery          | Best effort + ≤1 attempt                      |
| AgentEvalOps endpoint   | `/integrations/localagent/v1/trace-envelopes` |
| First write             | 201                                           |
| Replay                  | 200                                           |
| Conflict                | 409                                           |
| P0                      | 0                                             |
| P1                      | 0                                             |
| P2                      | 6                                             |
| DOC_DRIFT               | 1                                             |
| Ready for WP2           | YES                                           |
| Stage3.5 Complete       | NO                                            |

## 推荐面试材料文件名

```text
STAGE3_5_WP1_CONTRACT_INVENTORY_INTERVIEW_LEARNING.md
```

这个 WP 的面试价值可以概括成一句：

> **我不仅会实现 Agent Runtime，还会定义哪些行为应该长期稳定、哪些内部实现应该保持可演进，并通过 Owner、Version、Breaking Change Rule 建立真正可维护的工程边界。**