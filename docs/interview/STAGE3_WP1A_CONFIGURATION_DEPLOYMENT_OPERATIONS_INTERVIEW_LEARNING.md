# LocalAgent Stage 3 WP1 — Configuration / Deployment / Operations 总体面试学习材料

# 1. 一句话项目 / 工作包定义

我在 LocalAgent Stage 3 的 WP1 中，把原本偏开发态的单机 Agent 服务补齐为一套最小、可验证的 Configuration / Deployment / Lifecycle / Persistence Operations（配置 / 部署 / 生命周期 / 持久化运维）基础。

WP1 最终覆盖四个子工作包：

```text
WP1-A Configuration Foundation
WP1-B Deployment Foundation
WP1-C Health / Readiness / Lifecycle
WP1-D Migration / Operations Closure
```

最终 Aggregate Closure（总体闭环核验）：

```text
WP1-A = PASS
WP1-B = PASS
WP1-C = PASS
WP1-D = PASS

WP1 Aggregate Closure = PASS
WP1 complete = YES
Allowed to start WP2 = YES
```

最终总体 Gate：

```text
P0 = 0
P1 = 0
P2 = 1
DOC_ONLY = 0
TEST_GAP = 0
ENVIRONMENT_BLOCKED = 0
```

其中唯一 `P2=1` 是此前已经接受的 Planning executor starvation（规划执行器饥饿），不是 WP1 新增问题。

------

# 2. 为什么要做

Stage 2 / Stage 2.5 已经把 LocalAgent 的 Runtime（运行时）主干做起来了：

```text
RunContext
AgentState
Planning
Scheduler
Parallel execution
Budget / Timeout / Cancellation
Journal
Trace
Snapshot
Multi-Agent orchestration
OutputGate
```

但这些能力主要回答的是：

> 一个 Agent Run 应该怎样被正确执行？

WP1 要回答的是另一组生产化问题：

```text
服务拿什么配置启动？
部署拓扑是什么？
什么状态才算 READY？
什么时候应该拒绝新 Run？
Client 怎样知道 Server 真正可用？
服务如何优雅关闭？
持久数据升级后还能不能安全使用？
失败时怎样 Backup / Restore / Rollback？
```

也就是说：

```text
Stage 2 / 2.5
解决 Runtime correctness

WP1
解决 Runtime 周围的运行环境与运维基础
```

没有 WP1，即使 Runtime 内部逻辑很完整，也仍然容易出现：

```text
配置来源混乱
不同环境安全策略不一致
Client 比 Server 启动得快
服务尚未 READY 就接请求
Shutdown 过程中仍接新 Run
数据库版本不兼容却继续启动
升级后无法安全回滚
```

WP1 Aggregate Closure 最终确认，这些问题已经在当前 **Windows Native + single-server-process（Windows 原生 + 单服务进程）**范围内形成一致闭环。

------

# 3. 真实性与完成边界

| 能力                                   | 状态             |
| -------------------------------------- | ---------------- |
| 唯一 `Settings` 配置 Owner             | 已真实实现并核验 |
| LOCAL / TEST / PRODUCTION profiles     | 已真实实现       |
| Production TLS / trust policy          | 已真实实现       |
| Windows Native 部署拓扑                | 已真实冻结       |
| Server / Client 独立进程               | 已真实冻结       |
| `/health`                              | 已真实实现       |
| `/readyz`                              | 已真实实现       |
| Client startup readiness handshake     | 已真实实现       |
| Lifecycle Authority                    | 已真实实现       |
| Admission Gate                         | 已真实实现       |
| Graceful Shutdown                      | 已真实实现       |
| Persistence read-only Preflight        | 已真实实现       |
| Explicit Migration CLI                 | 已真实实现       |
| Manual stopped-server Backup / Restore | 已冻结运维合同   |
| Forward-only Rollback                  | 已冻结           |
| Automatic Backup                       | NOT_IMPLEMENTED  |
| Automatic Restore / DR                 | NOT_IMPLEMENTED  |
| Docker / Compose                       | NOT_IMPLEMENTED  |
| Windows Service wrapper                | NOT_IMPLEMENTED  |
| Linux certification                    | NOT_IMPLEMENTED  |
| Multi-worker deployment                | NOT_IMPLEMENTED  |
| Continuous readiness monitoring        | NOT_IMPLEMENTED  |
| Auto reconnect                         | NOT_IMPLEMENTED  |
| Client/Server version fingerprint      | DEFER_TO_WP4     |
| Runtime automatic replay / resume      | NOT_IMPLEMENTED  |
| HA / rolling / zero-downtime migration | NOT_IMPLEMENTED  |

WP1 PASS 只表示：

```text
Configuration / Deployment / Operations Foundation
```

完成。

不代表：

```text
Stage 3 complete
Security complete
Tool platform complete
Observability exporter complete
Production Certified
HA Ready
Cloud Ready
```

------

# 4. 修改前架构与根因

WP1 之前的问题不是某一个单点 Bug，而是多个“生产外围能力”没有统一 Owner 和 Contract（合同）。

可以概括为四类。

## 4.1 配置问题

如果每个组件直接：

```python
os.getenv(...)
```

会形成多个隐式配置 Owner。

后果：

```text
同一个配置在不同组件解释不同
Production 安全策略难统一
测试难构造确定性环境
配置错误可能拖到运行中才暴露
```

------

## 4.2 部署问题

虽然系统能运行，但如果没有明确：

```text
Server process
Client process
process-local Runtime Authority
supported OS
worker count
```

那部署人员可能：

```text
开多个 Server worker
把 Client 当成 Runtime owner
使用未验证 Linux / Docker 部署
```

最终破坏 Runtime 原本依赖的进程内唯一 Owner 假设。

------

## 4.3 Lifecycle 问题

只有：

```text
进程启动成功
```

不等于：

```text
服务可以接请求
```

如果缺 Health / Readiness（健康 / 就绪）：

```text
Server socket 可连接
但 Runtime resources 还没完成装配
```

Client 可能过早开始业务请求。

同样在 Shutdown：

```text
进程还活着
```

不代表：

```text
仍然应该接受新 Run
```

------

## 4.4 Persistence 问题

以前存在：

```text
constructor open
→ compatibility ALTER
```

这种隐式行为。

这会把：

```text
detect schema
```

和：

```text
mutate schema
```

混在 Server startup。

一旦升级失败：

```text
数据已经被修改
但新版本没启动成功
```

code rollback 的安全性就变得不明确。

所以 WP1 的核心根因其实是一句话：

> **缺少围绕 Runtime 的唯一 Owner、显式状态和失败关闭边界。**

------

# 5. 方案讨论与技术取舍

# 5.1 为什么 WP1 没有直接 Docker 化

当前 Runtime Authority、Admission、Lifecycle、Journal 等很多核心语义都建立在：

```text
single process
process-local authority
```

基础上。

如果直接上：

```text
Docker
multi-worker
orchestration
```

并不会自动变成生产化，反而会引入：

```text
多进程状态一致性
共享 Admission
跨进程 Shutdown
shared Store locking
```

等新的架构问题。

因此 WP1 先冻结：

```text
Windows Native
single Server process
separate PyQt Client
```

这是比“为了生产化而容器化”更稳健的取舍。

------

# 5.2 为什么配置统一成 Settings

最终：

```text
core.settings.Settings
```

是唯一 raw environment/configuration Owner。

优势：

```text
环境变量只解析一次
启动阶段统一校验
不同 Role 使用同一配置快照
安全策略集中
测试可构造固定 Settings
```

Aggregate Closure 扫描确认，WP1-B/C/D 没有重新引入第二个 raw environment loader。

------

# 5.3 为什么 Health 和 Readiness 分开

Health：

> 应用进程是否仍然活着、未进入不可用终态。

Readiness：

> 当前是否允许接收新的业务工作。

因此 Shutdown 时：

```text
Lifecycle = SHUTTING_DOWN
Admission = DRAINING

/health  = 200
/readyz  = 503
```

这比：

```text
shutdown 一开始 health 就 503
```

更准确。

因为：

```text
应用还活着
正在正常完成 drain
只是不能接新 Run
```

------

# 5.4 为什么 READY_DEGRADED 不是 Lifecycle State

Runtime Lifecycle（运行时生命周期）仍只有：

```text
STARTING
READY
SHUTTING_DOWN
CLOSED
```

而：

```text
READY_DEGRADED
```

属于 DiagnosticStatus（诊断状态）。

这是为了防止把：

```text
业务生命周期状态
```

和：

```text
依赖健康描述
```

混成一个状态机。

例如 KB optional 且不可用：

```text
Runtime lifecycle = READY
Diagnostic status = READY_DEGRADED
Admission = ACCEPTING
```

这是一个非常清晰的职责分离。

------

# 5.5 为什么 Client readiness 是 startup-only

WP1-C 没有一开始就做：

```text
持续监控
自动重连
连接状态机
```

而只是：

```text
Client startup
→ ReadinessWorker
→ bounded retry
→ Server ready
→ initial history fetch
```

因为第一版目标只是解决：

```text
Client 启动早于 Server
```

这个真实部署问题。

没有把它扩大成完整 Connection Management Platform（连接管理平台）。

------

# 5.6 为什么 Persistence 选择显式 Migration

核心取舍：

```text
Server startup
= automatic read-only preflight

Mutation
= explicit operator command
```

原因：

> 在没有自动 Backup 的情况下，Server 无法证明修改已有数据前存在可恢复备份。

所以自动 Migration 反而会降低 rollback 安全性。

------

# 6. 最终架构

从总体上看，WP1 把 LocalAgent 的启动路径变成：

```text
Environment
   │
   ▼
Settings.load()
   │
   ├── parse
   ├── semantic validation
   ├── security policy
   └── role validation
   │
   ▼
Lifecycle STARTING
   │
   ▼
Persistence Preflight
(read-only)
   │
   ▼
Required Resources
   │
   ├── Memory
   ├── Executors
   ├── Span recorder
   ├── Chroma
   ├── Models
   ├── Router
   ├── Tool registry
   ├── Metrics
   ├── Journal
   ├── Snapshot
   └── RecoveryValidator
   │
   ▼
ApplicationRuntimeServices
   │
   ├── Lifecycle Authority
   └── AdmissionGate
   │
   ▼
CoordinatedRuntimeFactory
ChatService
GracefulShutdownCoordinator
   │
   ▼
Lifecycle READY
Admission ACCEPTING
```

Aggregate Closure 根据当前 `server.py::lifespan()` 复核了真实顺序，而不是简单沿用旧 Handoff。

------

# 7. 核心状态机和时序

# 7.1 Lifecycle

```text
STARTING
   │
   ▼
 READY
   │
   ▼
SHUTTING_DOWN
   │
   ▼
 CLOSED
```

没有：

```text
MIGRATING
DEGRADED
PREFLIGHT
```

这些 Runtime Lifecycle State。

------

# 7.2 Admission

```text
ACCEPTING
   │
   ▼
DRAINING
   │
   ▼
CLOSED
```

它控制：

```text
还能不能接收新的 Run
```

而不是：

```text
进程是否活着
```

------

# 7.3 Health / Readiness

正常：

```text
Lifecycle READY
Admission ACCEPTING

health = 200
readyz = 200
```

KB optional degraded：

```text
Lifecycle READY
Admission ACCEPTING
Diagnostic READY_DEGRADED

health = 200
readyz = 200
```

Shutdown：

```text
Lifecycle SHUTTING_DOWN
Admission DRAINING

health = 200
readyz = 503
```

Closed：

```text
Lifecycle CLOSED

health = 503
readyz = 503
```

------

# 7.4 Client startup

```text
Client process starts
        │
        ▼
ReadinessWorker(QThread)
        │
        ├── timeout = 1s/request
        ├── total deadline = 30s
        ├── interval = 0.5s
        └── jitter = none
        │
        ▼
GET /readyz
        │
   ┌────┴─────┐
   │          │
 ready      timeout/fail
   │          │
   ▼          ▼
history    safe unavailable
fetch      UI stays open
```

当前不是 continuous monitor。

------

# 7.5 Shutdown

总体顺序：

```text
Lifecycle → SHUTTING_DOWN
        │
        ▼
Admission → DRAINING
        │
        ▼
reject new Run
        │
        ▼
cancel / drain active Runs
        │
        ▼
force-abort remaining work if necessary
        │
        ▼
close worker admission
        │
        ▼
drain workers
        │
        ▼
flush observability / trace
        │
        ▼
close resources
        │
        ▼
Admission CLOSED
Lifecycle CLOSED
```

Aggregate Closure 特别确认：

```text
ShutdownReport.completed
```

不能简单等价于：

```text
fully_closed
```

------

# 8. 数据、权限与 Owner 边界

WP1 最重要的一件事，是最终 Owner Map（所有权映射）变得清楚。

| Concern                     | Owner                           |
| --------------------------- | ------------------------------- |
| Raw env / configuration     | `Settings`                      |
| Production Composition Root | `server.py::lifespan()`         |
| Shared runtime services     | `ApplicationRuntimeServices`    |
| Lifecycle                   | `_LifecycleControl`             |
| Admission                   | `RuntimeAdmissionGate`          |
| Health projection           | `core/runtime/health.py`        |
| Migration orchestration     | `core/persistence_migration.py` |
| Memory schema               | `MemoryManager`                 |
| Journal schema/history      | Journal owner                   |
| Snapshot schema             | Snapshot owner                  |
| Checkpoint schema           | Checkpoint owner                |
| Chroma compatibility        | `VectorDBManager`               |
| Shutdown orchestration      | `GracefulShutdownCoordinator`   |

Aggregate Closure 没发现 duplicate owner（重复 Owner）。

------

# 9. 兼容策略

WP1 总体不是只讲数据库兼容，它实际上有四层 Compatibility（兼容性）。

## 9.1 Configuration Compatibility

不同环境：

```text
LOCAL
TEST
PRODUCTION
```

共享同一个 Settings schema，但安全策略不同。

例如 Production remote/hybrid model：

```text
HTTPS
TLS verification
```

必须满足更严格策略。

------

## 9.2 Deployment Compatibility

当前只承诺：

```text
Windows 11 / Windows Server
Python 3.12
uv
single Server process
separate PyQt Client
```

没有承诺：

```text
Linux
Docker
multi-worker
```

------

## 9.3 Lifecycle Compatibility

后续 WP1-D 没有为了 Migration 新增：

```text
MIGRATING
```

等 Lifecycle State。

这保护了 WP1-C contract。

------

## 9.4 Persistence Compatibility

Memory：

```text
new
current v1
current-unversioned
known legacy
future/malformed
```

不同分类。

Journal：

```text
record v1/v2
physical current / known legacy
```

分离。

Snapshot：

```text
v1 only
unknown fail closed
```

Chroma：

```text
marker compatibility
not internal DB migration
```

------

# 10. Bad Cases

# Bad Case 1：第四个 Client Session 漏掉统一 trust policy

### 真实性

WP1-B Final Review 中真实发现。

Memory Dialog 使用了独立 HTTP Session，但最初没有纳入完整 Client transport inventory。

### 风险

形成：

```text
Chat / History / Search
→ one proxy policy

Memory
→ another policy
```

### 修复

所有 Desktop Client transport 统一使用：

```text
settings.client_trust_env
```

WP1-C 后新增的 ReadinessWorker Session 也继续复用同一 Settings 快照。Aggregate Closure 最终统计出五个 Session creation point，并确认没有 drift。

### 面试知识点

> Transport policy 应该以“所有出站路径”做 inventory，而不是只检查主聊天路径。

------

# Bad Case 2：Health 和 Readiness 被当成同一个东西

### 真实性

WP1-C 设计阶段重点处理的问题。

### 风险

如果：

```text
DRAINING
```

直接：

```text
health = unhealthy
```

运维系统可能认为服务崩溃。

实际上：

```text
进程正常
只是拒绝新请求
```

### 修复

分离：

```text
Health
Readiness
Admission
Lifecycle
```

------

# Bad Case 3：Client 只验证 JSON 格式，不验证跨字段语义

### 真实性

WP1-C Initial Final Gate 真实 P1。

Client readiness validator 曾接受类似：

```text
status=READY
lifecycle=BOGUS
admission=BOGUS
```

因为只验证了字段存在 / 类型，没有验证组合是否合法。

### 根因

只做：

```text
syntactic validation
```

没做：

```text
semantic validation
```

### 修复

Client 只接受两个合法 success body：

```text
READY / READY / ACCEPTING / false

READY_DEGRADED / READY / ACCEPTING / true
```

------

# Bad Case 4：测试写了，但宽泛 except 把 assertion 吞掉

### 真实性

WP1-C Final Gate 真实 TEST_GAP。

Healthy KB regression 中：

```python
try:
    ...
    assert ...
except Exception:
    skip(...)
```

有可能连 assertion failure 都被捕获后跳过。

### 修复

Skip 只能发生在：

```text
已知前置环境缺失
```

而 assertion 放在 exception boundary 外。

### 面试知识点

> 测试本身也需要 fail closed。

------

# Bad Case 5：AdmissionGate 语义正确，但缺少 object identity 回归

### 真实性

WP1-C Re-Gate 真实 TEST_GAP。

虽然代码设计上：

```text
ChatService
RuntimeFactory
ShutdownCoordinator
```

都应该共享同一个 AdmissionGate，

但之前没有 durable test 证明：

```text
same object identity
```

### 修复

增加真实 Composition Root lifespan 测试，验证多处引用确实是同一对象。

### 面试知识点

> 对唯一 Authority 来说，仅仅“值相同”不够，有时必须证明“就是同一个 Owner 对象”。

------

# Bad Case 6：Schema 列名一样，但真正约束已经坏了

详见 WP1-D 学习材料。

核心：

```text
columns match
!=
schema compatible
```

例如：

```text
PK missing
UNIQUE missing
index wrong
trigger no-op
```

但旧 detector 仍 `CURRENT`。

------

# Bad Case 7：测试全绿但 semantic UNIQUE 仍 fail open

WP1-D 第一次修复后：

```text
1751 passed
```

但：

```text
extra UNIQUE(trace_id)
```

仍会被认为 compatible。

说明：

> required facts 存在，不代表实际 semantic constraint set 正确。

------

# Bad Case 8：全局 `.lower()` 破坏 SQL literal 语义

WP1-D Re-Gate 真实发现。

```text
'delete'
```

和：

```text
'DELETE'
```

被错误 canonicalize 成一样。

最终改成 quote-aware canonicalization。

------

# 11. 测试与验收

WP1 Aggregate Closure 实际执行：

## Aggregate Targeted Regression

覆盖：

```text
Settings / configuration
Deployment contract
Startup configuration
Health / Readiness
Client readiness
Runtime lifespan
Graceful shutdown
Persistence preflight / migration
Chroma persistence
```

结果：

```text
343 passed
```

------

## Critical Runtime Regression

覆盖：

```text
Runtime mode
Runtime mode E2E
Default Runtime entry
Fault production isolation
Stage2.5 RC
WP6 E2E
```

结果：

```text
45 passed
```

------

## Full Regression

```text
1760 collected
1760 passed
0 failed
0 skipped
42 subtests passed
4 warnings
```

------

## Static

```text
compileall PASS
uv lock --check PASS
git diff --check PASS
pyproject.toml / uv.lock diff EMPTY
```

Aggregate Closure 开始时工作树已经 CLEAN，HEAD 为：

```text
099abc40133d57f2d8e325b7429c9ecfeb2dbc71
```

------

# 12. Known Limitations

WP1 完成后仍明确保留：

## Deployment

```text
Windows-only
single Server process

no Docker / Compose
no Windows Service wrapper
```

------

## Backup / Restore

```text
no online backup
no automatic backup
no scheduled backup
no cloud backup
no automatic restore
no disaster-recovery automation
```

------

## Migration

```text
no downgrade migration
no cross-store transaction
no multi-process migration lock
no rolling migration
no zero-downtime migration
```

------

## Chroma

```text
destructive operator-triggered rebuild
internal schema = NOT_LOCAL_SCHEMA_OWNER
```

------

## Runtime Recovery

```text
validation-only
no replay
no resume
```

------

## Client Lifecycle

```text
readiness = startup-only
no continuous monitoring
no automatic reconnect
no manual reconnect button
```

------

## Version Compatibility

```text
Client/Server version fingerprint
= deferred to WP4
```

------

## Operations

```text
force-kill can bypass graceful shutdown
model artifact retention/distribution
= Operator responsibility
```

------

## Existing Technical Debt

```text
Planning executor starvation = accepted P2
existing deprecation/warning family
```

------

# 13. 这次总体体现的工程能力

# 13.1 Single Source of Truth

WP1 几乎所有设计都围绕：

```text
唯一配置 Owner
唯一 Lifecycle Owner
唯一 Admission Owner
唯一 Composition Root
唯一 Store schema Owner
```

展开。

这是生产系统比“功能能跑”更重要的一层。

------

# 13.2 Fail Fast

Configuration：

```text
invalid config
→ startup fail
```

Persistence：

```text
unsupported schema
→ startup fail
```

Required KB：

```text
incompatible
→ startup fail
```

而不是拖到请求执行时才暴露。

------

# 13.3 Fail Closed

如果系统不知道：

```text
这个 DB 是否兼容
```

就：

```text
UNSUPPORTED
```

不是：

```text
best effort open
```

------

# 13.4 Derived State 与 Authority 分离

```text
READY_DEGRADED
```

只是诊断 projection。

```text
Lifecycle
Admission
```

才是真 Authority。

同样：

```text
Observability Checkpoint
```

是 rebuildable derived state，不是业务 Authority。

------

# 13.5 Deployment Topology 是架构的一部分

不是：

```text
代码写完
部署爱怎么起怎么起
```

对于 process-local Runtime：

```text
single process
```

本身就是 correctness contract。

------

# 13.6 Graceful Shutdown 是状态机，不是 finally close()

真正 Shutdown 包含：

```text
stop admission
cancel/drain Runs
drain workers
flush observability
close resources
close lifecycle
```

而不是简单：

```python
finally:
    close()
```

------

# 13.7 Productionization 不等于堆中间件

WP1 没有为了“看起来生产级”引入：

```text
Kubernetes
Redis
Alembic
Service Mesh
```

而是先补：

```text
Owner
State
Boundary
Failure contract
Operations truth
```

这是非常好的系统设计思路。

------

# 14. 30 秒面试表达

我在 LocalAgent Stage 3 里先做了一轮最小生产化基础，不是直接上 Docker 或 Kubernetes，而是先把 Runtime 周边的 Configuration、Deployment、Lifecycle 和 Persistence Operations 补齐。配置上统一成唯一 `Settings` Owner，并区分 LOCAL、TEST、PRODUCTION；部署上冻结 Windows Native、单 Server 进程和独立 PyQt Client；生命周期上实现 `/health`、`/readyz`、AdmissionGate 和 startup-only Client readiness；持久化上实现 Server 启动只读 Preflight、Operator 显式 Migration、停服人工 Backup / Restore 和 forward-only rollback。最后还做了跨 WP Aggregate Closure，确认 Owner、Composition Root、Lifecycle、Admission 和运维文档没有出现第二事实源，最终 1760 个测试全量通过。

------

# 15. 2 分钟面试表达

Stage 2.5 完成后，我的 Runtime 内部执行链已经比较完整了，但如果直接说它生产可用还差很多外围基础。所以 Stage 3 第一部分我没有先上 Kubernetes，而是拆成了四个子工作包。

第一是 Configuration Foundation。我把所有环境变量收口到一个 frozen `Settings`，统一 LOCAL、TEST、PRODUCTION profile、角色校验和 Production TLS 策略，避免组件各自 `os.getenv` 形成第二配置源。

第二是 Deployment Foundation。因为 Runtime 的很多 Owner 都是 process-local，我明确冻结成一个 FastAPI Server process 加独立 PyQt Client，而不是随意开多 worker，同时把所有 Client HTTP Session 的 proxy/trust policy统一起来。

第三是 Health / Readiness / Lifecycle。我把 Lifecycle、Admission 和 Diagnostic Status 分开：Runtime 只有 STARTING、READY、SHUTTING_DOWN、CLOSED；Admission 单独有 ACCEPTING、DRAINING、CLOSED。这样 shutdown 时 health还能200，但 readyz变503，新 Run会在注册前被拒绝。Client 也有一个 startup-only QThread readiness probe，Server 真正 Ready 后才开始拉历史。

第四是 Persistence Operations。Server 启动只做 read-only Preflight，不自动改旧数据库；Migration 必须显式 Operator执行并确认已有 Backup。Memory 正式用了 SQLite `user_version=1`；Journal 只允许已知 physical migration并保护历史 row；Snapshot不迁移；Checkpoint可显式 recreate；Chroma只管理 LocalAgent marker，不碰第三方 internal schema。

这四部分最后又做了一次 Aggregate Closure，不是简单看四个子任务 PASS，而是检查唯一 Owner、Composition Root、Lifecycle、Admission、配置、运维 Runbook和 Known Limitation有没有跨 WP 冲突。最终 targeted 343 passed，Critical Runtime 45 passed，全量1760 passed。

------

# 16. 深入版本

如果面试官深入问：

> 这四个 WP 为什么要放在一起？

可以从系统启动到退出完整走一遍。

## 第一步：配置

进程还没真正进入 Runtime 前：

```text
Settings.load()
```

完成：

```text
parse
semantic validation
security validation
role validation
```

这决定：

```text
系统以什么环境、什么角色、什么安全策略启动
```

------

## 第二步：启动 Preflight

进入：

```text
STARTING
```

之后先做 Persistence Preflight。

目的：

```text
确认当前 durable state
是否能被这个 binary 安全消费
```

------

## 第三步：资源装配

包括：

```text
Memory
Executors
Vector DB
Models
Router
Tool registry
Metrics
Journal
Snapshot
RecoveryValidator
```

如果 required dependency 不成立：

```text
never READY
```

------

## 第四步：发布 ApplicationRuntimeServices

这一步之后才真正拥有：

```text
Lifecycle Authority
Admission Authority
```

最终：

```text
READY + ACCEPTING
```

------

## 第五步：Client startup

Client 不通过：

```text
端口能连
```

判断 Server 可用。

而是：

```text
/readyz
```

typed semantic validation。

------

## 第六步：运行

新 Run 必须经过 Admission。

这是：

```text
Deployment lifecycle
```

与：

```text
Runtime lifecycle
```

之间的重要连接点。

------

## 第七步：Shutdown

先：

```text
Admission DRAINING
```

再：

```text
cancel / drain active Runs
```

最后：

```text
CLOSED
```

保证：

```text
不再接新业务
但已有工作有机会安全收尾
```

------

## 第八步：升级

停止 Server 后：

```text
backup
preflight backup
deploy new code
migrate if required
start
health/readiness
```

如果 migration 已提交而升级失败：

```text
restore pre-migration data
+
rollback code/artifacts/config
```

于是：

> WP1 实际上建立了一条从“进程启动之前”一直延伸到“升级回滚之后”的操作闭环。

------

# 17. 高频追问与参考答案

## Q1：为什么生产化第一步不是 Docker / K8s？

因为容器只是部署载体。

如果内部：

```text
配置 Owner 不唯一
Lifecycle 不清晰
没有 Readiness
Shutdown 不可控
数据升级不可回滚
```

即使放进 Kubernetes，也只是把问题容器化。

当前 Runtime 又明确依赖 process-local Owner，所以我先冻结单进程拓扑，先把单机语义做对。

------

## Q2：Health 和 Readiness 区别？

Health：

```text
进程/应用是否仍处于可工作的生命状态
```

Readiness：

```text
现在是否能接收新业务
```

例如 DRAINING：

```text
health = 200
readyz = 503
```

------

## Q3：为什么还需要 AdmissionGate？

Readiness endpoint 只是一个 observation（观测）。

真正阻止新 Run 的必须是 Runtime 内部 Authority：

```text
AdmissionGate
```

否则：

```text
/readyz = 503
```

但业务接口仍然可能继续接新 Run。

------

## Q4：Lifecycle 和 Admission 为什么分开？

因为它们不是同一个维度。

例如：

```text
SHUTTING_DOWN + DRAINING
```

是非常正常的组合。

------

## Q5：READY_DEGRADED 为什么不是 Runtime State？

因为它描述的是：

```text
dependency health / diagnostic status
```

不是 Runtime 执行生命周期。

如果把 degraded 塞进 Lifecycle，会让状态机和 Admission关系变复杂。

------

## Q6：Composition Root 为什么重要？

它决定：

```text
谁创建共享资源
谁连接 Owner
谁负责 Startup / Shutdown
```

如果出现两个 Composition Root：

可能产生：

```text
两个 AdmissionGate
两个 Settings snapshot
两个 Runtime service tree
```

导致 Authority 分裂。

------

## Q7：为什么单 Server process 是 Contract，不只是部署建议？

因为当前：

```text
Lifecycle
Admission
Run registry
shared services
```

都是 process-local。

多个 worker 不会天然共享这些状态。

所以 multi-worker不是简单性能优化。

------

## Q8：Client 为什么不用“连接成功”判断 Server Ready？

TCP连接成功只说明：

```text
Server socket accepting
```

不说明：

```text
Runtime resources ready
Admission accepting
dependency contract satisfied
```

所以要读 `/readyz`。

------

## Q9：ReadinessWorker 为什么用 QThread？

因为 Desktop Client 是 PyQt。

如果主 UI thread里做：

```text
HTTP retry + sleep
```

会卡 UI event loop。

所以用独立 QThread 做有限时间 readiness probe。

------

## Q10：为什么 readiness不是永久后台监控？

当前需求只解决启动竞态。

Continuous monitoring会进一步带来：

```text
disconnect state
reconnect policy
retry storms
UI state machine
version mismatch handling
```

属于后续能力，不应该在 WP1-C无边界扩张。

------

## Q11：Graceful Shutdown 最关键的一步是什么？

我会回答：

> **先关闭 Admission，再处理已有 Run。**

如果反过来：

```text
你一边 drain
一边还有新 Run 进来
```

Shutdown 永远可能收不干净。

------

## Q12：为什么 persistence preflight 要放在 Store constructor 前？

因为旧 constructor可能执行 schema mutation。

如果先 constructor：

```text
你还没判断兼容
数据已经被改了
```

Preflight 就失去意义。

------

## Q13：为什么 migration不能自动做？

因为当前 Backup仍是人工的。

Server 没法证明：

```text
mutation before
已经有 validated backup
```

所以显式 Operator migration更安全。

------

## Q14：为什么不是所有 persistent Store都同一 migration策略？

因为数据价值不同。

```text
Memory
→ durable business data

Journal
→ append-only historical evidence

Snapshot
→ validation evidence

Checkpoint
→ rebuildable derived

Chroma
→ rebuildable third-party-backed index
```

用同一策略反而不合理。

------

## Q15：为什么 Aggregate Closure 还需要做？四个子项不都 PASS 了吗？

因为不同 WP可能独立正确，但组合后冲突。

例如：

```text
WP1-A说 Settings是唯一配置Owner
WP1-D又自己读环境变量
```

两个单项都可能测试通过，但 aggregate architecture已经漂移。

所以总体 Gate 专门检查：

```text
cross-WP ownership
cross-WP contract
historical limitation supersession
```

------

## Q16：什么是 historical limitation supersession？

例如 WP1-B 当时写：

```text
no health/readiness
```

这个历史事实是真的。

但 WP1-C之后已经实现：

```text
/health
/readyz
```

所以当前 Known Limitation不能继续复制旧文档。

Aggregate Closure就是用最新事实替换历史 limitation。

------

## Q17：WP1完成后为什么还不能说 Production Ready？

因为还缺：

```text
WP2 Tool Platformization
WP3 Security Baseline
WP4 Observability Exporter
WP5 Production Readiness Gate
```

WP1只是其中一个基础层。

------

# 18. 容易答错或夸大的问题

## 错误 1

“项目已经支持 Linux / Docker。”

错误。

当前 certified topology：

```text
Windows Native
single Server process
```

------

## 错误 2

“支持多 worker。”

错误。

------

## 错误 3

“ReadinessWorker会持续监控Server。”

错误。

```text
startup-only
```

------

## 错误 4

“READY_DEGRADED 是 Runtime Lifecycle State。”

错误。

属于 DiagnosticStatus。

------

## 错误 5

“Health=200说明可以接请求。”

错误。

还必须看 Readiness / Admission。

------

## 错误 6

“Shutdown completed就代表所有资源完全关闭。”

不能简单这么说。

需要区分：

```text
orchestration completion
fully_closed truth
```

------

## 错误 7

“Persistence Preflight 会自动修数据库。”

错误。

```text
read-only
```

------

## 错误 8

“现在有自动 Backup / Restore。”

错误。

manual stopped-server only。

------

## 错误 9

“WP1-D的Restore就是 Runtime Recovery。”

错误。

完全不同的概念。

------

## 错误 10

“WP1 PASS 表示 Stage3完成。”

错误。

后面还有 WP2～WP5。

------

# 19. 重点复习知识点

# P0：必须熟练

## 1. Settings Single Source of Truth

为什么不能多个 `os.getenv` Owner。

------

## 2. Composition Root

为什么：

```text
server.py::lifespan()
```

是整个 Production object graph入口。

------

## 3. Lifecycle vs Admission

```text
STARTING / READY / SHUTTING_DOWN / CLOSED

ACCEPTING / DRAINING / CLOSED
```

------

## 4. Health vs Readiness

特别是：

```text
DRAINING
health=200
readyz=503
```

------

## 5. Client readiness

startup-only、QThread、bounded retry。

------

## 6. Graceful shutdown

为什么先关闭 Admission。

------

## 7. Read-only Preflight vs Migration

这是 WP1-D核心。

------

## 8. Manual Backup / Restore / Forward-only rollback

必须会完整走操作流程。

------

# P1：建议深入掌握

## 9. Process-local Authority

为什么 multi-worker不是简单加参数。

------

## 10. Configuration Profile

LOCAL / TEST / PRODUCTION如何影响安全策略。

------

## 11. Typed readiness

为什么 schema validation还不够，还要 cross-field semantic validation。

------

## 12. Object identity test

为什么唯一 Authority需要 identity regression。

------

## 13. Lifecycle Projection

为什么 Diagnostic state不能反过来成为 Authority。

------

## 14. Rebuildable vs Required

Checkpoint / Chroma典型例子。

------

## 15. Historical limitation supersession

工程文档维护非常重要。

------

## 16. Aggregate Gate

为什么“所有子任务 PASS”仍不自动推出系统整体 PASS。

------

# P2：扩展理解

## 17. Kubernetes probes

【通用知识补充】

未来如果容器化：

```text
livenessProbe
readinessProbe
```

可以分别对应：

```text
/health
/readyz
```

但当前项目没有 Kubernetes。

------

## 18. Blue-Green Deployment

【通用知识补充】

如果未来需要 zero-downtime：

需要考虑：

```text
old/new binary data compatibility
version negotiation
shared Store schema
traffic switch
```

当前未实现。

------

## 19. Windows Service

【通用知识补充】

如果以后把 Server包装为 Windows Service，还需要：

```text
Service Control Manager integration
startup/recovery policy
log/service account
shutdown signal mapping
```

当前没有。

------

## 20. Configuration hot reload

【通用知识补充】

当前 Settings：

```text
once per process
immutable snapshot
```

没有 hot reload。

这是刻意设计，不是遗漏。

------

# 20. 最终面试速查表

| 项目                   | 最终事实                                           |
| ---------------------- | -------------------------------------------------- |
| WP1                    | Configuration / Deployment / Operations            |
| 子项                   | WP1-A / B / C / D                                  |
| WP1 Aggregate          | PASS                                               |
| P0                     | 0                                                  |
| P1                     | 0                                                  |
| P2                     | 1，Planning executor starvation                    |
| TEST_GAP               | 0                                                  |
| Config Owner           | `Settings`                                         |
| Composition Root       | `server.py::lifespan()`                            |
| Topology               | Windows Native + one Server + separate PyQt Client |
| Multi-worker           | NOT_SUPPORTED                                      |
| Docker                 | NOT_IMPLEMENTED                                    |
| Lifecycle              | STARTING / READY / SHUTTING_DOWN / CLOSED          |
| Admission              | ACCEPTING / DRAINING / CLOSED                      |
| Health                 | `/health`                                          |
| Readiness              | `/readyz`                                          |
| READY_DEGRADED         | Diagnostic only                                    |
| Client handshake       | startup-only QThread                               |
| Request timeout        | 1s                                                 |
| Total startup deadline | 30s                                                |
| Retry interval         | 0.5s                                               |
| Persistence stores     | 5                                                  |
| Startup migration      | NO                                                 |
| Startup preflight      | automatic + read-only                              |
| Migration              | explicit SCRIPT_ROLE                               |
| Backup                 | manual stopped-server                              |
| Restore                | manual + FULL preflight                            |
| Rollback               | forward-only                                       |
| Downgrade              | NOT_IMPLEMENTED                                    |
| Runtime Recovery       | validation-only                                    |
| Chroma internal schema | NOT_LOCAL_SCHEMA_OWNER                             |
| Graceful Shutdown      | Admission drain → Runs → workers → flush → close   |
| Aggregate targeted     | 343 passed                                         |
| Critical Runtime       | 45 passed                                          |
| Full regression        | 1760 passed                                        |
| Skip                   | 0                                                  |
| Subtests               | 42 passed                                          |
| Stage 3 complete?      | NO                                                 |
| Next                   | WP2 Tool Platformization                           |

## 最值得记住的一句话

> **WP1 的核心不是增加几个运维接口，而是把配置、部署、启动、准入、关闭和持久化升级都放进明确的 Owner、状态机和 fail-closed 合同里，让 Runtime 从“代码内部能正确执行”进一步变成“在真实部署生命周期里也有可验证的运行边界”。**