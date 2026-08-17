## 1. 一句话项目 / 工作包定义

WP5 是 Stage 3 的最终 Production Readiness Gate（生产准备门禁）：

> **不再开发新功能，而是对配置、部署、Runtime、Tool、安全、持久化、Trace Export、AgentEvalOps 和 Shutdown 等已经完成的能力做一次聚合验收，确认它们组合在真实默认生产入口后仍能正确启动、执行、失败和关闭，从而证明 Stage 3 达到了“最小必要生产化”边界。**

这里最重要的关键词不是“功能实现”，而是：

```text
Integration Confidence
（集成可信度）

Production Claim Boundary
（生产声明边界）

Regression Protection
（回归保护）
```

------

## 2. 为什么要做

WP1～WP4 分别独立 PASS，并不自动意味着：

```text
Stage 3 = PASS
```

因为系统可能出现典型的**局部正确、组合错误**。

例如：

```text
WP1 Settings 单独正确
WP2 Tool 单独正确
WP3 Security 单独正确
WP4 Trace 单独正确

但组合后可能出现：

startup owner 冲突
Tool governance 被绕过
Trace exporter 影响 Runtime
shutdown 顺序死锁
optional dependency 变 mandatory
Output / Memory ownership 被破坏
```

所以 WP5 要回答的不是：

> “每个模块自己的测试通过了吗？”

而是：

> **“这些模块在真正的 Composition Root 下组合以后，Stage 3 的最小生产链是否仍然成立？”**

这是 WP5 的核心工程价值。

------

## 3. 真实性与完成边界

### 已真实执行

WP5 使用当前源码进行了：

- 默认真实 Uvicorn lifespan 启动；
- AgentEvalOps enabled 启动；
- 默认 Coordinated Runtime（协调运行时）Smoke；
- Scheduler / Multi-Agent targeted regression；
- Tool allow / deny 路径；
- Security Baseline 回归；
- Persistence preflight；
- Trace / Exporter 回归；
- external export failure isolation；
- disabled exporter；
- startup → shutdown；
- OutputGate / Final Memory；
- `375` 项聚焦回归；
- 一次全量 `2467 passed + 42 subtests`；
- `compileall / uv lock --check / git diff --check`。

### 没有新增生产能力

WP5 是只读 Gate。

没有新增：

```text
retry
durable outbox
automatic recovery
production chaos
HA
multi-process
Kubernetes
WAF
generic sandbox
```

### 不能宣称

WP5 PASS **不等于**：

```text
Production proven
Distributed production ready
Exactly-once
Durable delivery
Automatic recovery
HA ready
```

最终报告明确：

```text
Production proven = NO
```



------

## 4. 修改前架构与根因

这里的“修改前”不是某一段代码，而是进入 WP5 前整个 Stage 3 的状态：

```text
WP1 ✅
WP2 ✅
WP3 ✅
WP4 ✅

但 Stage3 ❌ 尚未 PASS
```

原因是所有 WP 的 Gate 都是**局部 Scope（范围）Gate**。

例如：

```text
WP1
只证明 Configuration / Deployment / Operations

WP2
只证明 Tool Platformization

WP3
只证明 Security Baseline

WP4
只证明 Observability / Trace Exporter
```

所以需要 WP5 建立：

```text
Aggregate production composition
             ↓
       one final verdict
```

本质根因就是：

> **局部不变量成立，不代表聚合系统不变量成立。**

------

## 5. 方案讨论与取舍

WP5 最值得学习的不是某个算法，而是 Gate 范围控制。

### 方案 A：重新把 WP1～WP4 全部重测一遍

没有采用。

因为会产生大量重复工作，而且不增加多少新的信心。

例如 WP4-C 已经独立完成：

```text
LocalAgent → AgentEvalOps
REAL LOCAL CROSS-SYSTEM E2E VERIFIED
```

WP5 没必要重新攻击 5000 位 JSON integer、numeric canonicalization 等全部历史边界。

------

### 方案 B：继续增加生产化能力后再 Gate

没有采用。

例如：

```text
durable retry
outbox
HA
proxy
custom CA
production chaos
```

都可以继续做，但不是当前 Stage 3 最小生产化的必要条件。

------

### 最终方案：Risk-based Aggregate Gate（基于风险的聚合门禁）

只重新验证可能因为**组合**而出问题的主干：

```text
Composition Root
Startup
Core Runtime
Scheduler
Tool
Security
Persistence
Trace
Exporter failure isolation
Disabled mode
Shutdown
Output / Memory
```

已经被独立 Gate 强证明过的细节，则复用最新权威证据。

这是一个非常重要的工程思想：

> **最终 Gate 不应该等于“把所有历史测试再执行一次”，而应该围绕系统级风险重新组织证据。**

------

## 6. 最终架构

WP5 验证的最终生产组合可以理解为：

```text
server.py::lifespan()
        │
        ├─ Settings
        │
        ├─ Persistence Preflight
        │
        ├─ Memory
        │
        ├─ Tool Registry
        │
        ├─ Tool Governance
        │
        ├─ Resource Authorization
        │
        ├─ ToolExecutionService
        │
        ├─ Agent Router / Runtime
        │
        ├─ Trace Recorder
        │
        ├─ TraceExportDispatcher
        │
        ├─ AgentEvalOpsTraceExporter [optional]
        │
        ├─ Journal
        │
        ├─ Snapshot
        │
        ├─ RecoveryValidator
        │
        └─ ApplicationRuntimeServices
                  │
                  └─ Shutdown Coordinator
```

最终 Gate 再次确认：

```text
server.py::lifespan()
= 唯一 Production Composition Root
```

没有出现第二个 production owner。

------

## 7. 核心状态机和时序

WP5 最重要的不是业务状态机，而是 production lifecycle（生产生命周期）。

### 正常启动

```text
Process Start
   ↓
Settings validation
   ↓
Persistence preflight
   ↓
Component construction
   ↓
Runtime composition
   ↓
READY
   ↓
Request processing
```

### 非法配置

```text
Invalid Settings
   ↓
startup validation
   ↓
FAIL CLOSED
   ↓
Never READY
```

### 持久化 schema 不兼容

```text
Persistence Preflight
   ↓
MIGRATION_REQUIRED / UNSUPPORTED
   ↓
Never READY
```

### Trace export 不可用

```text
Primary Runtime
   ↓
Trace Export
   ↓
transport failure
   ↓
BEST_EFFORT failure isolation
   ↓
Primary Runtime remains usable
```

### Shutdown

```text
Stop accepting work
   ↓
Runtime / workers
   ↓
Recorder producer barrier
   ↓
Trace dispatcher / exporter
   ↓
Journal / Snapshot / persistence
   ↓
CLOSED
```

最终真实 enabled / disabled startup → shutdown 均在 Gate 的 15 秒 probe bound 内结束。

------

## 8. 数据 / 权限 / Owner

WP5 再次确认了几个非常适合面试追问的 Owner：

| 能力                      | Owner                                |
| ------------------------- | ------------------------------------ |
| Production Composition    | `server.py::lifespan()`              |
| Runtime lifecycle         | Runtime / ApplicationRuntimeServices |
| Tool identity             | Tool Registry                        |
| Tool policy               | Tool Governance                      |
| Resource permission       | Resource Authorization               |
| 实际 Tool 执行            | `ToolExecutionService`               |
| Final Output              | `OutputGate`                         |
| Final Memory              | `RunFinalMemoryWriter`               |
| Trace producer            | Span Recorder                        |
| Trace export queue        | `TraceExportDispatcher`              |
| External HTTP             | `AgentEvalOpsTraceExporter`          |
| Recovery                  | `RecoveryValidator`                  |
| Persistence startup truth | Persistence preflight                |

其中面试最值得强调：

```text
Tool Registry
≠ Tool permission

Tool Governance
≠ Resource Authorization

OutputGate
≠ Memory Writer

RecoveryValidator
≠ Recovery Executor
```

职责分离依然成立。

------

## 9. 兼容策略

WP5 的兼容策略可以概括成：

> **Optional Capability（可选能力）不能破坏 Default Production Path（默认生产路径）。**

最典型就是 AgentEvalOps exporter。

默认：

```text
LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED=false
```

必须满足：

```text
no exporter
no external delivery dependency
startup succeeds
runtime usable
```

启用时：

```text
valid configuration
→ exporter + dispatcher wired
→ startup succeeds
```

错误配置：

```text
fail before READY
```

远端 unavailable：

```text
best-effort export failure
≠ primary runtime failure
```

这就是典型的 optional integration compatibility contract。

------

## 10. Bad Cases

### Bad Case 1：可选 Trace Exporter 变成启动硬依赖

**真实性：本轮通过真实 disabled startup 主动防守，没有发现当前缺陷。**

假设：

```text
export disabled
但 startup 仍 import/configure/connect AgentEvalOps
```

风险：

> 外部 observability 系统故障导致核心 Agent 不能启动。

最终 Gate：

```text
disabled
→ dispatcher=None
→ READY
```

PASS。

------

### Bad Case 2：Exporter 失败拖垮 Runtime

**真实性：真实 failure probe。**

测试：

```text
AgentEvalOps enabled
→ closed loopback target
```

结果：

```text
Trace transport fails
Primary Runtime remains usable
Shutdown bounded
```

所以 Observability failure 没有升级成 Runtime failure。

------

### Bad Case 3：非法配置直到请求期才炸

Stage 3 前面的 Configuration 工作已经修复过这种模式。

WP5 再验证：

```text
missing AgentEvalOps config
invalid URL
invalid timeout relation
```

都会在 startup 阶段 fail closed。

核心原则：

> **可以在启动时发现的问题，不应该拖到请求期。**

------

### Bad Case 4：Tool Governance 被聚合接线绕过

如果真实 Router 直接：

```text
ToolExecutionService.execute()
```

而跳过 Governance / Authorization，就会造成典型 Composition regression。

WP5 真实验证：

```text
ToolRegistry
→ ToolGovernance
→ ResourceAuthorization
→ ToolExecutionService
```

允许路径成功，拒绝路径在执行前失败。

------

### Bad Case 5：Final Output 与 Memory ownership 发生回归

如果 specialist raw output 或 failed output 被写进业务 Memory，会造成长期污染。

最终仍保持：

```text
OutputGate
= final publication owner

RunFinalMemoryWriter
= DELIVERED only
```

------

### Bad Case 6：Recovery 文档被夸大

当前真实能力仍然是：

```text
Recovery
= VALIDATION_ONLY
```

只读取：

```text
Snapshot
Journal
```

形成 assessment。

没有：

```text
resume
automatic replay
automatic writeback
automatic recovery
```

------

## 11. Tests / Gate：只写真实执行过的

### Targeted

```text
375 passed
1 deselected
9 subtests passed
exit 0
```

范围包括：

```text
startup/config
coordinated lifecycle
scheduler
parallel/synthesis
output/memory
WP2 Tool
WP3 Security
recovery/fault truth
Trace contract
serializer
dispatcher
exporter
shutdown
```

### Full Regression

```text
2467 passed
13 deselected
4 warnings
42 subtests passed
exit 0
```

13 个高资源真实模型测试按照仓库 marker 默认排除，本轮没有重新运行；报告保留了之前的独立执行证据。

### Static

```text
compileall        PASS
uv lock --check   PASS
git diff --check  PASS
```

------

## 12. Known Limitations

WP5 最终保留的非阻断限制包括：

```text
Planning executor starvation
No durable trace outbox
No trace retry / batching
Recovery validation-only
No automatic recovery
No production fault activation
Prompt Injection partially supported
No generic WAF
No generic DLP
No generic Sandbox
Single-process Windows-native deployment
Force-kill bypasses graceful shutdown
No HA / multi-process production proof
No capacity / SLA proof
No continuous dependency health
Local Memory store currently requires operator migration
```

这里要特别注意：

> **Known Limitation 不等于缺陷，也不等于一定要继续开发。**

只要它准确地限制了能力声明，而且不破坏当前 Stage 的目标，就可以冻结。

------

## 13. 体现出的工程能力

WP5 主要体现的不是“又写了多少代码”，而是以下能力。

### Production Readiness Thinking（生产准备思维）

能够判断：

```text
哪些东西是真正的 release blocker
哪些只是 future hardening
```

------

### Integration Risk Management（集成风险管理）

不把所有模块重新从头验证，而是重点寻找：

```text
ownership collision
lifecycle conflict
optional dependency leakage
failure propagation
shutdown interaction
```

------

### Fail-closed Design（失败关闭设计）

在 Settings、Persistence、Security 等关键边界：

```text
不确定
→ 不 READY / 不执行
```

而不是继续运行。

------

### Graceful Degradation（优雅降级）

Trace/AgentEvalOps 属于：

```text
optional observability path
```

外部失败不会摧毁主 Runtime。

------

### Truthful Capability Management（真实能力管理）

能够明确：

```text
Recovery = VALIDATION_ONLY

Real E2E = YES
Production Proven = NO
```

这对于工程面试非常重要。

------

## 14. 30 秒面试版本

> Stage 3 最后我做了一个 Production Readiness Gate，没有继续加功能，而是把前面完成的配置、Runtime、Tool、安全、持久化和 Trace Export 全部放回真实 `server.py::lifespan()` 组合里做最终验收。
>
> 我重点验证了默认启动、AgentEvalOps enabled/disabled、Coordinated Runtime、Tool allow/deny、安全边界、持久化 preflight、Trace export failure isolation、OutputGate、Final Memory 和 graceful shutdown。最终 375 个聚焦测试和 2467 个全量测试通过，P0/P1 都为 0，所以 Stage 3 达到了 Minimal Necessary Productionization。
>
> 但这个结论只代表最小生产化边界成立，不代表真实生产部署、HA、durable delivery 或 automatic recovery 已完成。

------

## 15. 2 分钟面试版本

> Stage 3 前面 WP1 到 WP4 分别解决了 Configuration/Operations、Tool Platform、安全基线和 Trace/AgentEvalOps，但各个 WP 独立 PASS 并不代表组合以后系统一定正确，所以 WP5 我没有继续开发新能力，而是专门做 Production Readiness Gate。
>
> 第一件事是检查唯一 Composition Root，确认仍然是 `server.py::lifespan()`，没有因为 Tool、Security 或 Trace 接入出现第二 Runtime Owner。
>
> 然后验证两类启动路径。默认 AgentEvalOps export 是关闭的，这时系统不应该依赖任何外部 Trace 服务，真实 Uvicorn 能 READY 并正常关闭。打开 export 后，只要配置合法，即使目标地址当前不可达，startup 也可以成功，因为网络 delivery 是 best-effort，而不是 readiness dependency。
>
> Runtime 层跑了默认 Coordinated E2E，同时保护 Scheduler、Parallel Specialist、Synthesis 和 OutputGate；Tool 层验证了完整 `Registry → Governance → Resource Authorization → ToolExecutionService` 链，allow 成功、deny 在执行前停止；Security、Persistence、Recovery、Trace 也分别做了最小 smoke。
>
> 最后特别验证 failure isolation 和 shutdown。AgentEvalOps 指向 closed port 时，Trace export 失败不会让主 Runtime 崩溃，而且 shutdown 有界。OutputGate 仍是 final publication owner，Final Memory 仍然只写 DELIVERED 输出。
>
> 最终 targeted 375 passed，全仓 2467 passed + 42 subtests，P0=0、P1=0，因此 Stage 3 PASS。但我仍明确保留了 no durable outbox、Recovery validation-only、single-process 等 Known Limitations，没有把 Stage 3 夸成 production-proven。

------

## 16. 深入版本：真正要掌握的三条主线

### 主线 A：局部 PASS ≠ 系统 PASS

这是 WP5 最核心的一句话。

```text
Component correctness
+
Component correctness
≠
System correctness
```

组合后还必须验证：

```text
lifecycle
ownership
dependency
failure propagation
shutdown
```

------

### 主线 B：生产准备 ≠ 功能越多越好

Minimal Productionization 的判断标准不是：

```text
feature count
```

而是：

```text
core path works
failure is bounded
unsafe state fails closed
claims are truthful
operations are understandable
```

------

### 主线 C：Release Gate 必须和 Scope 匹配

如果 Stage 3 的目标只是：

```text
Minimal Necessary Productionization
```

那么 Gate 不应该突然要求：

```text
HA
Kubernetes
outbox
production chaos
exactly-once
```

否则就是 Scope Drift（范围漂移）。

------

## 17. 高频追问

### Q1：为什么 WP1～WP4 都 PASS 了还需要 WP5？

因为各 WP 证明的是局部不变量。

WP5 验证的是：

```text
这些组件真正组合以后
是否还能共同成立
```

特别关注 lifecycle、Owner、failure propagation 和 shutdown。

------

### Q2：为什么 AgentEvalOps 不可用时还能 READY？

因为当前 AgentEvalOps Trace Export 是 optional observability capability（可选可观测能力）。

如果把它变成 startup dependency：

> 监控平台故障就会让业务 Agent 不可用。

这不符合当前架构。

------

### Q3：那怎么知道 AgentEvalOps 配置写错了？

两件事要分开：

```text
invalid local configuration
→ startup fail closed

remote currently unavailable
→ runtime best-effort failure
```

前者是本地确定性错误，应启动时发现；后者是运行期外部依赖故障。

------

### Q4：为什么 Recovery 不做到自动恢复？

当前 Stage 的需求只冻结了：

```text
Snapshot + Journal
→ recovery evidence validation
```

自动恢复需要进一步解决 side effects、tool replay、external state、idempotency 等问题，成本和风险显著增加，因此没有为面试闭环强行实现。

------

### Q5：为什么 full regression 有 13 个 deselected，还能 PASS？

它们属于仓库已有的 resource-intensive（资源密集型）真实模型测试默认 marker 策略。

WP5 报告没有伪装成 executed，而是明确写了本轮没重新跑，并引用已有独立执行证据。

重点是：

> **Skipped / deselected 必须解释，不能偷偷当作 PASS。**

------

### Q6：Stage 3 PASS 后是不是可以说 Production Ready？

不能这么宽泛地说。

更准确：

> **Stage 3 Minimal Necessary Productionization PASS。**

因为仍没有真实生产部署、HA、容量/SLA、durable delivery 等证据。

------

## 18. 最容易夸大 / 答错的地方

### ❌ “LocalAgent 已经经过生产验证”

正确：

```text
Real Local Cross-System E2E = YES
Production proven = NO
```

------

### ❌ “Recovery 可以自动恢复任务”

正确：

```text
Recovery = VALIDATION_ONLY
```

------

### ❌ “Trace 支持 exactly-once”

正确：

```text
BEST_EFFORT
+
AT_MOST_ONE_TRANSPORT_ATTEMPT_PER_ACCEPTED_ENVELOPE
```

------

### ❌ “WP5 又实现了一套生产化功能”

错误。

WP5 是：

```text
READ-ONLY Production Readiness Gate
```

没有修改 production code。

------

### ❌ “P2=6 就说明还有 6 个严重 Bug”

错误。

P2 是当前已经评估为**非阻断**的问题/限制，而且其中很多属于明确接受或 defer 的工程债。

------

### ❌ “所有 Known Limitations 都应该继续修”

错误。

Production engineering 里重要能力之一就是：

> **知道什么时候停止。**

不影响目标 Scope、能够准确声明并有合理降级行为的问题，可以冻结，而不是无限开发。

------

## 19. P0 / P1 / P2 复习

最终：

```text
P0 = 0
P1 = 0
P2 = 6
```

6 个 P2 是：

| P2                                              | 状态 / 含义          |
| ----------------------------------------------- | -------------------- |
| Planning executor starvation                    | `ACCEPTED_P2`        |
| 恶意自然语言 / untrusted data 影响模型语义      | WP3 已接受边界       |
| System Prompt 可能被模型复述                    | WP3 Known Limitation |
| delivery/final-memory negative path 无对称 Span | `ACCEPTED_P2`        |
| planning/step error taxonomy 可能折叠           | `DEFERRED`           |
| AgentEvalOps legacy delete divergence           | `ACCEPTED_P2`        |

面试最应该掌握的是：

> **P0/P1 是当前 Stage 是否能够发布/冻结的阻断等级；P2 可以存在，但必须理解影响、明确 Scope，并且不能偷偷升级能力声明。**

------

## 20. 速查表

| 项目                         | 最终事实                    |
| ---------------------------- | --------------------------- |
| WP                           | Stage3-WP5                  |
| 类型                         | Production Readiness Gate   |
| Production code change       | NO                          |
| Composition Root             | `server.py::lifespan()`     |
| Default startup              | PASS                        |
| AgentEvalOps enabled startup | PASS                        |
| Invalid config               | Fail closed                 |
| Default Runtime              | COORDINATED                 |
| Scheduler / Multi-Agent      | PASS                        |
| Tool allow                   | PASS                        |
| Tool deny                    | PASS                        |
| Security Baseline            | PASS                        |
| Persistence preflight        | PASS                        |
| Recovery                     | `VALIDATION_ONLY`           |
| Trace                        | PASS                        |
| AgentEvalOps exporter        | PASS                        |
| Export failure isolation     | PASS                        |
| Disabled mode                | PASS                        |
| Shutdown                     | bounded / PASS              |
| Final Output owner           | `OutputGate`                |
| Final Memory                 | `DELIVERED_ONLY`            |
| Retry                        | NO                          |
| Durable Outbox               | NO                          |
| Production Chaos             | NO                          |
| Automatic Recovery           | NO                          |
| Targeted                     | `375 passed`                |
| Full                         | `2467 passed + 42 subtests` |
| P0                           | 0                           |
| P1                           | 0                           |
| P2                           | 6                           |
| Capability Gap               | 0                           |
| Test Gap                     | 0                           |
| Doc Drift                    | 1                           |
| Production proven            | NO                          |
| Stage3-WP5                   | **COMPLETE**                |
| Stage 3                      | **PASS**                    |
| Stage3.5                     | **READY**                   |