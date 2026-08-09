# LocalAgent Stage 3 WP1-C — Health / Readiness / Lifecycle 面试总结与学习

## 1. 一句话项目 / 工作包定义

我在 LocalAgent 的 Windows 部署基础完成以后，为 Server 和 Desktop Client 补齐了一套最小可信的 Health（健康检查）、Readiness（就绪检查）和 Lifecycle Diagnostic Projection（生命周期诊断投影）机制：

- Server 提供 `GET /health` 与 `GET /readyz`；
- Readiness 不自己维护状态，而是读取真实 Runtime Lifecycle（运行时生命周期）和 Admission Gate（准入门）；
- Knowledge Base（知识库）允许降级时使用 `READY_DEGRADED` 作为**派生诊断状态**，不污染 Runtime 生命周期状态机；
- Desktop Client 通过一次 startup-only（仅启动阶段）的后台 `QThread` 有界探测 `/readyz`，避免 Qt 主线程阻塞；
- 并通过真实 Composition Root（组合根）测试证明 `/readyz`、`/api/chat`、RuntimeFactory 和 ShutdownCoordinator 消费的是同一个 Application-level AdmissionGate。

最终 Gate：

```text
P0 = 0
P1 = 0
P2 = 0
TEST_GAP = 0
DOC_ONLY = 0
ENVIRONMENT_BLOCKED = 0

WP1-C Final Re-Gate = PASS
WP1-C complete = YES
```

最终全量：

```text
1678 passed
0 skipped
4 warnings
42 subtests passed
```

------

# 2. 为什么要做

## 2.1 修改前并不是“完全没有生命周期”

WP1-C 开始前，LocalAgent 已经存在真实的 Runtime Lifecycle：

```text
STARTING
READY
SHUTTING_DOWN
CLOSED
```

权威状态位于：

```text
ApplicationRuntimeServices._LifecycleControl
```

同时也已经存在：

```text
RuntimeAdmissionGate

ACCEPTING
→ DRAINING
→ CLOSED
```

所以 WP1-C 并不是重新发明一个 lifecycle state machine（生命周期状态机）。

真正的问题是：

> **这些 Runtime 内部已经存在的事实，没有形成面向运维和 Client 的可信诊断合同。**

------

## 2.2 修改前 `/api/chat` 能拒绝请求，但没有正式 Readiness

修改前：

```text
/api/chat
```

已经会检查：

```text
admission_gate.accepts_new_runs
```

当 Shutdown 开始、Admission 进入 `DRAINING` 后，会返回：

```text
503 RUNTIME_SHUTTING_DOWN
```

但是当时并不存在：

```text
/health
/readyz
```

因此外部 Client 无法提前回答：

> Server 现在到底能不能安全接收一个新的 Run？

Scout Audit 明确确认：

```text
/health      NOT_IMPLEMENTED
/readyz      NOT_IMPLEMENTED
/version     NOT_IMPLEMENTED
/metadata    NOT_IMPLEMENTED
```

------

## 2.3 “进程活着”不等于“服务就绪”

这是 WP1-C 最核心的问题。

例如 Shutdown 过程中：

```text
Application 仍然活着
```

但：

```text
Admission = DRAINING
```

此时：

- 系统还有能力完成剩余 Run 的取消、Drain、Flush 和 Close；
- 但不能接受新的 Run。

因此：

```text
Healthy
≠
Ready
```

这正是最终：

```text
DRAINING:
    /health = 200
    /readyz = 503
```

的来源。

------

# 3. 真实性与完成边界

| 内容                                                | 真实性                           | 当前状态             |
| --------------------------------------------------- | -------------------------------- | -------------------- |
| 原项目已有 STARTING/READY/SHUTTING_DOWN/CLOSED      | 源码审查发现                     | 已确认               |
| RuntimeAdmissionGate 已有 ACCEPTING/DRAINING/CLOSED | 源码审查发现                     | 已确认               |
| 修改前无 `/health`、`/readyz`                       | 源码审查发现                     | 已确认               |
| 修改前 Client 无 wait-ready / retry / handshake     | 源码审查发现                     | 已确认               |
| `GET /health`                                       | 本 WP 实现                       | 已实现并测试         |
| `GET /readyz`                                       | 本 WP 实现                       | 已实现并测试         |
| `READY_DEGRADED`                                    | 本 WP 实现                       | 仅 DiagnosticStatus  |
| StartupDependencySnapshot                           | 本 WP 实现                       | 已实现               |
| Client `ReadinessWorker(QThread)`                   | 本 WP 实现                       | 已实现               |
| Client bounded retry 1s / 30s / 0.5s                | 本 WP 实现                       | 已测试               |
| Client typed readiness validator                    | 本 WP 实现后 Final Gate 发现缺陷 | 已修复               |
| Healthy KB broad catch 测试缺陷                     | Final Gate 真实发现              | 已修复               |
| AdmissionGate identity 无持久测试                   | Final Gate 真实发现的测试缺口    | 已修复               |
| Server Startup Smoke                                | 实际测试                         | 已运行并 PASS        |
| Client GUI Smoke                                    | 未执行                           | NOT_RUN              |
| Continuous Health monitoring                        | 未来能力                         | 未实现               |
| 自动 reconnect                                      | 未来能力                         | 未实现               |
| Post-start dependency aggregate health              | 未来能力                         | 未实现               |
| Version compatibility                               | 未来能力                         | 未实现，DEFER_TO_WP4 |
| `/metadata` / `/version`                            | 未来能力                         | 未实现               |
| Multi-worker                                        | 当前部署限制                     | 未实现               |
| Docker / Linux deployment                           | 当前 Scope 外                    | 未实现               |

Final Re-Gate 明确只允许说：

> **WP1-C Health / Readiness / Lifecycle Foundation completed。**

不能说：

> WP1 完成、Stage 3 Production Ready、完整生产监控完成。

------

# 4. 修改前架构与根因

## 4.1 原有 Lifecycle

修改前真实结构：

```text
server.py::lifespan()
        │
        ├── app.state.runtime_lifecycle_state
        │
        └── ApplicationRuntimeServices
                  │
                  └── _LifecycleControl
```

其中真正 Runtime Authority 是：

```text
ApplicationRuntimeServices.lifecycle_state
```

`app.state.runtime_lifecycle_state` 是 lifespan 发布出来的一个 view（视图），不是新的生命周期 Owner。

------

## 4.2 原有 Admission

```text
ApplicationRuntimeServices
       │
       └── RuntimeAdmissionGate
             │
             ├── ChatService
             ├── CoordinatedRuntimeFactory
             └── GracefulShutdownCoordinator
```

状态：

```text
ACCEPTING
→ DRAINING
→ CLOSED
```

Shutdown：

```text
close_admission()
→ DRAINING
```

之后新 Run 必须拒绝。

------

## 4.3 原来的 `require_service()` 不能充当 Readiness

已有：

```text
require_service()
```

只检查：

```text
chat_service is not None
```

它不检查：

```text
lifecycle
admission
dependency
```

因此：

```text
ChatService object exists
```

不能推出：

```text
Server can accept new Run
```

这也是为什么最终没有简单把：

```text
require_service
```

改名成：

```text
require_ready
```

而是新增独立 Diagnostic Projection。

------

# 5. 方案讨论与技术取舍

## 5.1 Health 和 Readiness 为什么必须分开

最终定义：

### Health

回答：

> 当前 Server Application 是否仍处于一个非 terminal / 非 fatal-unavailable 的可工作生命周期中？

它**不证明**：

- 能接收新 Run；
- 所有依赖正常；
- 所有 Circuit 都关闭；
- Journal 完全正常；
- Executor 没有饱和。

### Readiness

回答：

> 当前是否可以安全尝试接受新的 Run？

公式冻结为：

```text
services available
AND lifecycle == READY
AND admission == ACCEPTING
```

------

## 5.2 为什么不新增 `is_ready`

最简单的实现似乎是：

```python
is_ready = True
```

Startup 完成改 True，Shutdown 改 False。

但是这样会制造：

```text
Lifecycle state
Admission state
is_ready
```

三套可能互相矛盾的状态。

例如：

```text
lifecycle = READY
admission = DRAINING
is_ready = True
```

谁才是真的？

最终选择：

```text
真实 Authority
    ↓
Derived Projection
```

而不是：

```text
真实 Authority
+
新的 mutable readiness Authority
```

这体现的是：

> **Read Model（读模型）不应该自动升级成 Write Authority（写权威）。**

------

## 5.3 为什么 `READY_DEGRADED` 不进入 RuntimeLifecycleState

原 Runtime 生命周期：

```text
STARTING
READY
SHUTTING_DOWN
CLOSED
```

如果直接增加：

```text
READY_DEGRADED
```

那么：

```text
RuntimeFactory
Scheduler
RunCoordinator
Admission
```

未来都需要理解第五种 Runtime state。

但实际 KB degraded 的底层真实情况仍然是：

```text
lifecycle = READY
admission = ACCEPTING
KB = degraded
```

所以最终：

```text
RuntimeLifecycleState = READY
DiagnosticStatus = READY_DEGRADED
```

`READY_DEGRADED` 只是展示层的组合结果。

这是本 WP 最重要的架构取舍之一。

------

## 5.4 为什么不做完整 Dependency Health Manager

当前 Runtime 没有一个统一 Authority 能回答：

```text
Model 是否健康？
Journal 是否健康？
Executor 是否健康？
Observability 是否健康？
```

这些组件的局部失败目前分别属于自己的 Owner。

如果 Health endpoint 临时收集：

```text
Circuit breaker
executor queue
journal status
trace status
```

然后自己决定 Application 是否健康，就相当于创建了一个新的 Application Health Authority。

因此第一版明确只覆盖：

```text
startup completion
lifecycle
admission
allowlisted startup degradation
```

Post-start dependency aggregate health 继续：

```text
NOT_IMPLEMENTED
```

------

# 6. 最终架构

## 6.1 Server

```text
Runtime Authorities
│
├── ApplicationRuntimeServices.lifecycle_state
│
├── RuntimeAdmissionGate.state
│
└── StartupDependencySnapshot
          │
          ▼
core/runtime/health.py
Pure Diagnostic Projector
          │
          ├── GET /health
          └── GET /readyz
```

`core/runtime/health.py` 是 Diagnostic Projection Owner（诊断投影计算负责人），但**不是 Runtime State Owner**。

------

## 6.2 StartupDependencySnapshot

新增：

```text
StartupDependencySnapshot
```

第一版仅包含：

```text
knowledge_base_degraded: bool
```

特点：

```text
frozen
application-scope
startup 构造一次
runtime 不修改
不持久化
不保存 raw exception
```

------

## 6.3 Client

```text
MainController
      │
      └── ReadinessWorker(QThread)
              │
              └── GET /readyz
                     │
                     ├── ready
                     │      ↓
                     │  initial history fetch
                     │
                     └── deadline failure
                            ↓
                     safe unavailable message
```

Worker 是：

```text
startup-only
```

不是长期监控线程。

------

# 7. 核心状态机和时序

## 7.1 Runtime Lifecycle

仍然只有：

```text
STARTING
   ↓
READY
   ↓
SHUTTING_DOWN
   ↓
CLOSED
```

Admission：

```text
ACCEPTING
   ↓
DRAINING
   ↓
CLOSED
```

------

## 7.2 Diagnostic Projection

最终矩阵：

| Runtime Fact                    | Diagnostic     | `/health` | `/readyz` |
| ------------------------------- | -------------- | --------- | --------- |
| pre-services STARTING           | STARTING       | 200       | 503       |
| READY + ACCEPTING               | READY          | 200       | 200       |
| READY + ACCEPTING + KB degraded | READY_DEGRADED | 200       | 200       |
| READY + DRAINING                | DRAINING       | 200       | 503       |
| SHUTTING_DOWN + DRAINING        | DRAINING       | 200       | 503       |
| CLOSED + CLOSED                 | CLOSED         | 503       | 503       |
| inconsistent / unavailable      | UNAVAILABLE    | 503       | 503       |

------

## 7.3 为什么 DRAINING 的 Health 是 200

DRAINING 时：

```text
Application still running
```

它还需要完成：

```text
cancel
run drain
worker drain
flush
close
```

所以：

```text
health = 200
```

合理。

但：

```text
accepts_new_runs = false
```

所以：

```text
readyz = 503
```

这就是 Health / Readiness 分离最直观的场景。

------

## 7.4 Client Startup

```text
Settings.load()
    ↓
Client role validation
    ↓
QApplication / MainController
    ↓
UI displayed
    ↓
ReadinessWorker.start()
    ↓
GET /readyz
```

成功：

```text
READY or READY_DEGRADED
    ↓
ready signal
    ↓
initial history fetch
```

失败：

```text
30s deadline
    ↓
unavailable signal
    ↓
"Server unavailable; retry later."
```

UI 不退出。

------

# 8. 数据、权限与 Owner 边界

## Lifecycle Authority

```text
ApplicationRuntimeServices._LifecycleControl
```

------

## Admission Authority

```text
ApplicationRuntimeServices.admission_gate
```

------

## Startup degradation fact

```text
ApplicationRuntimeServices.startup_dependency_snapshot
```

------

## Diagnostic Projector

```text
core/runtime/health.py
```

职责：

```text
READ
DERIVE
SERIALIZE
```

不负责：

```text
WRITE
RECOVER
RETRY
CHANGE ADMISSION
```

------

## FastAPI Endpoint

```text
/health
/readyz
```

只是 Reader。

------

## Desktop Client

```text
ReadinessWorker
```

也只是远程 Reader。

这一点在最终的 Production Composition Root 测试中得到持久化验证：

```text
services.admission_gate
is chat_service.admission_gate
is app.state.runtime_admission_gate
```

并且 Factory 和 ShutdownCoordinator 同样消费这一对象。

------

# 9. 兼容策略

## 9.1 `/api/chat` 不改语义

WP1-C 没有把 `/api/chat` 改造成通过 `/readyz` 调用。

它仍然：

```text
require_service()
→ existing admission check
→ existing Runtime routing
```

Health/Readiness 是 additive（新增式）能力。

------

## 9.2 非 Run Endpoint 不加 AdmissionGate

以下继续保持旧行为：

```text
/api/history
/api/search
/api/memory
/api/runtime/runs/.../cancel
```

因为：

```text
Readiness
=
can accept a new Run
```

不是：

```text
every HTTP endpoint must be available
```

------

## 9.3 Client 不做版本协商

Client handshake 当前只代表：

```text
Server is ready
```

不代表：

```text
Client / Server version compatible
```

因此旧 Server 返回：

```text
404 /readyz
```

当前只视为：

```text
startup readiness attempt failed
```

不会判断：

```text
VERSION_INCOMPATIBLE
```

Version compatibility 明确推迟到 WP4。

------

# 10. Bad Cases

## Bad Case 1：DiagnosticStatus 错误使用不存在的 RuntimeAdmissionState.UNAVAILABLE

### 类型

**实施过程中真实发现的实现缺陷。**

不是线上事故。

### 原因

RuntimeAdmissionState 实际只有：

```text
ACCEPTING
DRAINING
CLOSED
```

但 Diagnostic Projection 需要：

```text
UNAVAILABLE
```

它属于诊断层，并不属于真实 Admission 状态机。

### 根因

把：

```text
Diagnostic value
```

误认为：

```text
Runtime Authority enum value
```

### 修复

Diagnostic snapshot 使用安全字符串 / Diagnostic 层值表示 `UNAVAILABLE`，不修改 RuntimeAdmissionState。

### 知识点

> **Projection schema 不应该反向污染 Domain state machine。**

------

## Bad Case 2：没有 lifespan 的测试访问 app.state 导致 AttributeError

### 类型

**实施过程中真实发现的实现缺陷。**

### 触发

部分 ASGI 测试没有真实跑完整 lifespan，因此：

```text
app.state.runtime_lifecycle_state
```

可能还不存在。

旧 endpoint 急切读取会抛：

```text
AttributeError
```

### 修复

改为有限 fallback：

```text
getattr(app.state, "runtime_lifecycle_state", None)
```

无法确认时：

```text
UNAVAILABLE
health 503
readyz 503
```

而不是 500。

### 知识点

**Fail Closed（失败时安全拒绝）**。

------

## Bad Case 3：Client 收到 malformed body 却错误认为 Ready

### 类型

**Final Gate 真实发现的 P1。**

不是生产事故。

旧 validator 接受：

```text
status = READY
lifecycle = BOGUS
admission = BOGUS
degraded = false
```

也接受：

```text
status = READY_DEGRADED
lifecycle = CLOSED
admission = CLOSED
degraded = false
```

### 根因

只验证：

```text
字段存在
Python 类型正确
status 看起来是 ready
```

没有验证：

```text
跨字段语义一致性
```

### 最终合法组合仅两个

```text
READY
READY
ACCEPTING
false
```

或者：

```text
READY_DEGRADED
READY
ACCEPTING
true
```

其他全部 fail closed。

### 面试知识点

**Schema validation（模式校验）还不够，还需要 semantic validation（语义校验）。**

------

## Bad Case 4：测试把 AssertionError 吞掉然后 skip

### 类型

**Final Gate 真实发现的测试缺陷 / blocking TEST_GAP。**

旧结构：

```text
try:
    run real lifespan
    assert contract
except Exception:
    pytest.skip()
```

问题：

```text
AssertionError
```

也是 `Exception`。

所以真正实现回归可能被：

```text
skip
```

伪装成“环境问题”。

### 最终修复

先显式检查 repository-local 模型：

```text
data/models/Qwen3-Embedding-0.6B
```

若 prerequisite 不存在：

```text
skip
```

一旦进入 lifespan：

```text
任何生产异常
任何 AssertionError
```

都直接 FAIL。

### 面试知识点

> **测试的 skip 边界本身也是 Test Contract。**

------

## Bad Case 5：当前行为正确，但没有持久测试锁定 Admission Identity

### 类型

**Final Gate 真实发现的 TEST_GAP。**

Codex 临时探针已经证明：

```text
services gate
ChatService gate
RuntimeFactory gate
ShutdownCoordinator gate
app.state gate
```

实际上是 SAME_OBJECT。

但是没有正式测试。

这意味着未来某次重构可能变成：

```text
readyz reads Gate A
chat reads Gate B
```

两个 Gate 恰好今天状态一样，但语义已经分叉。

### 最终测试

真实 Production Composition Root：

```text
server.py::lifespan()
```

启动后验证 object identity。

随后：

```text
active_runs = 0
close_admission()
→ /readyz 503
→ /api/chat 503 RUNTIME_SHUTTING_DOWN
→ active_runs still 0
```

### 面试知识点

> **Value equality 不足以证明 Single Authority；有时必须测试 object identity。**

------

# 11. 测试与验收

最终 Re-Gate 实际结果：

```text
Startup configuration
25 passed

WP1-C targeted
165 passed
0 skipped

Critical Runtime regression
42 passed

Collect-only
1678 collected

Full regression
1678 passed
0 skipped
4 warnings
42 subtests passed

compileall
PASS

uv lock --check
PASS

git diff --check
PASS

pyproject.toml / uv.lock diff
EMPTY
```

------

## Server Smoke

此前 Final Gate 真实运行过 Server：

```text
lifespan startup
→ READY
→ /health 200
→ /readyz 200
→ aliases 404
→ graceful shutdown
```

并且还曾真实跑出：

```text
READY_DEGRADED
```

场景。

最后一次 test-only Re-Gate：

```text
Server Smoke = NOT_RERUN
```

因为没有再次修改 Server 生产代码。

这个边界不能省略。

------

## Client GUI Smoke

```text
NOT_RUN
```

Client 的 QThread、retry、signal、trust_env、history gating 是通过自动化行为测试验证的。

不能说：

> “真实 GUI Smoke PASS”。

------

# 12. Known Limitations

## 1. Startup-only readiness

只在 Client 启动时 probe。

不是持续 Health monitor。

------

## 2. 无 Continuous Monitoring

Server 启动后如果后来挂掉：

Client 仍然依赖：

```text
chat/search/memory
```

等真实业务请求失败来发现。

------

## 3. 无自动 reconnect

没有：

```text
reconnect state machine
manual readiness retry button
background reconnect loop
```

------

## 4. 无 post-start aggregate dependency health

以下不会自动把 Application 标记为 unhealthy：

```text
one Model circuit open
one Journal append failure
Executor saturation
Observability consumer failure
one Run failure
```

------

## 5. Version compatibility 未实现

`/readyz` 只证明 ready。

不证明：

```text
Client version == Server compatible version
```

Version fingerprint / compatibility：

```text
DEFER_TO_WP4
```

------

## 6. STARTING / CLOSED 不保证网络可观察

因为 Uvicorn lifespan startup 完成之前：

HTTP Server 未必已经正常接受 request。

同理 CLOSED 后 Server 通常已经停止。

因此：

```text
STARTING → health 200
CLOSED → health 503
```

主要是**纯投影合同**，不是保证网络上一定能捕获的窗口。

------

## 7. Startup failure 没有独立 out-of-process diagnostic channel

如果 required dependency 在 Server startup 阶段直接 fail：

Server 可能根本无法提供 `/health`。

目前没有另一个 supervisor endpoint 告诉 Client：

```text
具体 startup 为什么失败
```

------

## 8. 仍然 single-process

这来自 WP1-B 部署边界。

Health/Readiness 没有解决 distributed deployment。

------

## 9. Windows-only

仍然只是 Windows certified target。

------

## 10. 其他跨 WP Known Limitations

仍包括：

```text
无 Docker/Compose
无 Windows Service wrapper
无 automatic backup/restore
无 migration runner
无 automatic deployment rollback
force-kill 可绕过 graceful shutdown
Planning executor starvation accepted P2
既有 deprecation warning family
```

------

# 13. 这次修改体现的工程能力

## 13.1 Health ≠ Readiness

这是后端生产化里非常典型的问题。

不是：

```text
HTTP Server alive = Ready
```

而是：

```text
Health:
还能运行/收口吗？

Readiness:
还能接新工作吗？
```

------

## 13.2 Derived State（派生状态）设计

`READY_DEGRADED` 没有直接塞进 Runtime 状态机。

这是很典型的：

```text
Domain state
→ View state
```

设计。

------

## 13.3 Single Source of Truth（单一事实来源）

Readiness 不创建：

```text
is_ready
```

而是复用：

```text
Lifecycle Authority
+
Admission Authority
+
Startup dependency fact
```

------

## 13.4 Graceful Shutdown 与 Readiness 联动

Shutdown 开始：

```text
Admission → DRAINING
```

Readiness 立即：

```text
503
```

从而阻止新的 Run。

但 Health：

```text
200
```

允许剩余 shutdown workflow 正常完成。

------

## 13.5 UI / Network Thread Boundary

Client readiness 请求没有放在 Qt UI 主线程。

而是：

```text
QThread
```

避免：

```text
30s startup retry
```

直接把桌面 UI 卡死。

------

## 13.6 Bounded Retry（有界重试）

不是：

```python
while True:
```

而是：

```text
single request max = 1s
overall deadline = 30s
interval = 0.5s
monotonic clock
```

这属于生产级 retry 的基本边界意识。

------

## 13.7 Test Gate 真正验证架构

本 WP 最后真正卡住它的甚至已经不是生产实现。

而是：

```text
TEST_GAP
```

Final Gate 要求：

> 当前行为正确还不够，核心 Architecture Invariant（架构不变量）必须拥有可以持续运行的回归证据。

------

# 14. 30 秒面试表达

我在 LocalAgent 最小生产化阶段做过 Health、Readiness 和生命周期治理。项目本身已经有 `STARTING/READY/SHUTTING_DOWN/CLOSED` 生命周期和 `ACCEPTING/DRAINING/CLOSED` AdmissionGate，所以我没有重新维护一个 `is_ready`，而是做了一个只读 Diagnostic Projection。`/readyz` 只有在 Runtime READY 且 Admission ACCEPTING 时才返回 200，KB 是允许降级依赖，因此 KB 不可用但允许 degraded 时返回 `READY_DEGRADED`，仍可 ready。Shutdown 时则是 health 200、readyz 503。桌面 Client 用 startup-only QThread 做 30 秒有界 readiness probe，不阻塞 UI。Final Gate 还发现过 Client 对 `/readyz` body 语义校验过宽，以及 AdmissionGate identity 缺少持久测试，最后都补齐后全量 1678 passed、0 skipped。

------

# 15. 2 分钟面试表达

我做这个 WP 的时候，重点不是简单增加两个 API。

项目原来已经有 Runtime Lifecycle 和 AdmissionGate。Runtime Lifecycle 是 `STARTING、READY、SHUTTING_DOWN、CLOSED`，Admission 是 `ACCEPTING、DRAINING、CLOSED`。所以我先把一个原则冻结下来：Health 和 Readiness 都只是 Derived Diagnostic Projection，不能再成为新的 Runtime Authority。

Health 回答的是 Application 是否还活着并能够完成当前生命周期工作；Readiness 回答的是能不能接新的 Run。所以 shutdown 进入 DRAINING 后，我设计成 `/health=200`，因为进程还要完成 cancellation、drain、flush 和 close，但 `/readyz=503`，因为已经不能接新 Run。

另外知识库在项目里是唯一允许 startup degradation 的依赖。如果配置允许降级，KB 初始化失败以后 Runtime 仍然是 READY、Admission 仍然 ACCEPTING，我没有把 RuntimeLifecycleState 加一个 `READY_DEGRADED`，而是在诊断层派生 `READY_DEGRADED`。这样不会污染原 Runtime 状态机。

Client 侧我实现了一个 startup-only 的 QThread readiness worker，每次请求最长 1 秒，总 deadline 30 秒，每 0.5 秒重试一次。成功以后才拉首屏 history，失败 UI 保持打开并提示 Server unavailable。

这次 Final Gate 还发现过一个比较有价值的问题：Client 最初虽然检查四字段，但会接受 `status=READY, lifecycle=BOGUS` 这种语义矛盾 body。后来我把 success contract 收紧成只有两种合法组合。另外 Gate 还要求持久测试证明 `/readyz` 和 `/api/chat` 真的使用同一个 AdmissionGate。最终跑到 1678 个测试全部通过，0 skip。

------

# 16. 深入版本

如果面试官继续追问，可以从四层展开。

## 第一层：Health 和 Readiness 的语义

Health：

```text
application still alive enough
```

Readiness：

```text
safe to accept new work
```

二者最关键的区别场景就是 shutdown draining。

------

## 第二层：为什么不用 Boolean

因为：

```text
is_ready
```

本质也是状态。

只要可以被独立写入，就可能和：

```text
lifecycle
admission
```

产生双写。

所以我把 Readiness 定义成函数：

```text
f(lifecycle, admission, startup_dependency_snapshot)
```

而不是新的变量。

------

## 第三层：为什么 READY_DEGRADED 是 Projection

因为真实 Runtime 行为仍然允许新 Run：

```text
lifecycle = READY
admission = ACCEPTING
```

只是部分能力，比如 KB Retrieval 不可用。

所以 `READY_DEGRADED` 是：

```text
用户/运维需要看到的信息
```

而不是：

```text
Runtime 调度需要的新状态
```

------

## 第四层：为什么 Client validator 要做跨字段校验

HTTP schema：

```text
status: str
lifecycle: str
admission: str
degraded: bool
```

只能证明类型。

但是：

```text
READY + CLOSED + CLOSED
```

类型全部合法，语义却完全冲突。

所以最终 Client 只接受两个完整 tuple：

```text
(READY, READY, ACCEPTING, false)

(READY_DEGRADED, READY, ACCEPTING, true)
```

其他全部 retry。

这是：

> **syntactic validity（语法有效）和 semantic validity（语义有效）的区别。**

------

# 17. 高频追问与参考答案

## Q1：Health 和 Readiness 有什么区别？

Health 表示当前服务本身是否仍处于可工作的生命周期；Readiness 表示能不能接新的业务请求。

我项目里最典型的是 Shutdown：

```text
Admission = DRAINING
```

此时：

```text
health 200
readyz 503
```

因为 Server 还需要完成已有 Run 的 shutdown，但不能接新 Run。

------

## Q2：为什么不直接用 `/health` 判断能不能发请求？

因为 Health 不等于 Run Admission。

如果这么做，Shutdown 时要么：

- Health 过早变 503，无法表达服务还在正常 Drain；
- 要么 Health 一直 200，Client 又错误继续提交新 Run。

所以分成两个 Contract。

------

## Q3：Readiness 的判断条件是什么？

项目当前第一版严格是：

```text
ApplicationRuntimeServices exists
AND lifecycle == READY
AND admission == ACCEPTING
```

KB degraded 如果是允许降级的情况，不阻止 readiness。

------

## Q4：为什么 KB degraded 还能 Ready？

因为这个行为在 WP1-A 已经冻结：

```text
knowledge_base_required = false
```

意味着 KB 是允许降级能力。

如果 KB required：

初始化失败会直接 Startup fail。

如果允许 degraded：

Server 仍然可以处理不依赖 KB 的能力，因此：

```text
READY_DEGRADED
readyz = 200
```

------

## Q5：`READY_DEGRADED` 为什么不加入 RuntimeLifecycleState？

因为它不是新的 Runtime 执行阶段。

底层仍然：

```text
READY + ACCEPTING
```

只是 StartupDependencySnapshot 表示：

```text
KB degraded=true
```

因此它属于 Diagnostic layer。

如果加入 Runtime enum，就会让 Scheduler、Factory、Shutdown 等 Runtime 核心都被迫理解额外状态。

------

## Q6：为什么不定期 polling `/health`？

第一版目标只解决启动依赖。

持续 polling 会引入：

```text
长期 background worker
reconnect state
timer lifecycle
cleanup
UI connection state
```

这些会明显扩大 Scope。

所以第一版是：

```text
startup-only
```

Server 启动后掉线仍由真实业务请求错误暴露。

------

## Q7：为什么 QThread，而不是直接在 UI 启动时 requests.get？

因为 Client readiness 总 deadline 是 30 秒。

如果放在 Qt UI Thread：

最坏可以冻结整个界面几十秒。

所以网络 retry 放到：

```text
ReadinessWorker(QThread)
```

UI 只消费 signal。

------

## Q8：为什么是 1 秒 / 30 秒 / 0.5 秒？

这是 WP1-C 当前冻结的最小 startup readiness policy：

```text
per request timeout = 1.0s
total deadline = 30.0s
interval = 0.5s
jitter = 0
```

它不是 operator-facing Settings，目前是代码常量。

回答时不要说这是性能调优得到的最佳值——**现有材料没有性能实验支持这一说法**。

------

## Q9：为什么要用 `time.monotonic()`？

【通用知识补充】

Retry deadline 应基于单调时钟。

系统 wall clock（墙上时钟）可能因为：

```text
NTP
手动改时间
时区
```

发生跳变。

Monotonic clock 只关注经过时长，更适合 timeout/deadline。

项目真实实现也使用了 monotonic deadline。

------

## Q10：为什么 malformed readiness body 要 retry，而不是直接报版本错误？

因为当前没有 Version Compatibility Contract。

例如旧 Server：

```text
404 /readyz
```

当前只能证明：

```text
startup handshake unsuccessful
```

不能证明：

```text
version incompatible
```

所以 404、malformed body、503 等统一在 deadline 内继续 retry。

------

## Q11：为什么 Client 只接受两种 ready body？

因为 Client 真正关心的是：

> Server 是否满足“可以接新 Run”的合同。

所以它不需要复制完整 Server Diagnostic State Machine。

只严格识别：

```text
READY / READY / ACCEPTING / false
```

和：

```text
READY_DEGRADED / READY / ACCEPTING / true
```

即可。

------

## Q12：为什么要验证 AdmissionGate object identity？

因为仅测试：

```text
readyz = 503
chat = 503
```

并不能证明二者使用同一 Gate。

可能存在：

```text
Gate A = DRAINING
Gate B = DRAINING
```

今天值相等，但未来更新可能分叉。

项目的核心合同是：

```text
Single Admission Authority
```

所以最终测试直接锁：

```text
same object
```

并配合行为链验证。

------

## Q13：为什么 TEST_GAP 也会阻止 Gate？

因为核心架构合同如果没有 durable behavioral regression：

当前虽然正确，未来很容易漂移。

比如 AdmissionGate identity 就是典型。

临时探针已经证明：

```text
今天是同一个对象
```

但如果没有测试，下一次改 Composition Root 就可能重新拆成两个对象。

------

## Q14：Client startup probe 失败后会发生什么？

当前：

```text
UI 保持开启
不拉 initial history
显示一次 Server unavailable
worker 结束
```

没有：

```text
auto reconnect
continuous polling
manual reconnect state machine
```

------

## Q15：服务 Startup 失败时 `/health` 会返回什么？

不能简单回答“503”。

如果 required dependency 在 Uvicorn lifespan startup 阶段失败：

Server 可能根本没有进入正常 HTTP serving。

所以当前不能保证外部一定能请求 `/health` 获得失败状态。

这也是 Known Limitation：

```text
startup failure
无独立 out-of-process diagnostic channel
```

------

# 18. 容易答错或夸大的问题

## 错误 1：Health 200 代表 Agent 全部功能正常

错误。

Health 不检查：

```text
所有 Model
Journal
Executor
Retrieval
```

第一版没有 post-start aggregate health。

------

## 错误 2：Readiness 就是 `chat_service is not None`

错误。

这是旧：

```text
require_service()
```

的语义。

Readiness 需要：

```text
services + READY + ACCEPTING
```

------

## 错误 3：READY_DEGRADED 是 Runtime 的第五个状态

错误。

RuntimeLifecycleState 仍然只有四个。

`READY_DEGRADED` 只是 DiagnosticStatus。

------

## 错误 4：Client 现在会持续监测 Server

错误。

只做：

```text
startup-only readiness probe
```

------

## 错误 5：Client 和 Server 有版本握手

错误。

只有 readiness handshake。

没有：

```text
version negotiation
compatibility range
fingerprint
```

------

## 错误 6：Startup 失败后 `/health` 一定返回 503

错误。

Server 可能尚未完成 ASGI startup，HTTP 本身就不可达。

------

## 错误 7：Client GUI 已经做真实启动 Smoke

错误。

Final Gate 明确：

```text
Client GUI Smoke = NOT_RUN
```

------

## 错误 8：1678 全绿后没有 Known Limitation

错误。

Gate PASS 只证明冻结的 WP1-C Contract 达标，不代表所有生产能力都已经实现。

------

# 19. 重点复习知识点

## P0：必须熟练

### 1. Health vs Readiness

必须能脱口而出：

```text
Health = 服务是否仍正常存活/收口
Readiness = 是否可以接受新工作
```

以及：

```text
DRAINING:
health=200
readyz=503
```

------

### 2. Derived State vs Authority

必须理解：

```text
DiagnosticStatus
```

为什么不是 Runtime Authority。

这是这一 WP 最核心系统设计思想。

------

### 3. Admission Gate

必须理解：

```text
ACCEPTING
DRAINING
CLOSED
```

和：

```text
Readiness
```

之间的关系。

------

### 4. Single Source of Truth

能解释：

```text
为什么不新增 is_ready
```

以及为什么要锁 AdmissionGate identity。

------

### 5. Fail Closed

无法确认状态时：

```text
UNAVAILABLE
503
```

而不是猜 Ready。

------

### 6. Client Background Worker

必须能说明：

```text
QThread
startup-only
bounded
interruptible
```

为什么比 UI Thread 同步请求合理。

------

## P1：高概率深入追问

### 7. Retry Deadline

```text
1.0s request
30s total
0.5s interval
monotonic
```

------

### 8. Schema vs Semantic Validation

例如：

```text
READY / CLOSED / CLOSED / false
```

语法可能合法，但语义不合法。

------

### 9. Graceful Shutdown 与 Admission

Shutdown：

```text
close admission
→ cancel
→ drain
→ close
```

为什么 readiness 应该在最前面变 false。

------

### 10. KB degraded

能解释：

```text
required
vs
allowed degraded
```

------

### 11. Pure Projection

Health endpoint 为什么不主动：

```text
ping model
ping DB
ping RAG
```

------

### 12. Test Gap

理解：

```text
Implementation truth
!=
Regression protection
```

------

## P2：扩展知识

### 13. Liveness / Readiness / Startup Probe

【通用知识补充】

常见生产系统还会区分：

- Liveness（存活检查）
- Readiness（就绪检查）
- Startup Probe（启动检查）

本项目目前实现的是自己的：

```text
/health
/readyz
Client startup readiness
```

不要直接说已经实现 Kubernetes 的三类 Probe，因为项目没有 Kubernetes。

------

### 14. Continuous Monitoring

【通用知识补充】

未来若做持续监测，需要考虑：

```text
poll interval
connection state
recovery transition
UI state
worker lifecycle
shutdown cleanup
backoff
```

这也是为什么本阶段没“顺手加上”。

------

### 15. Aggregate Health

【通用知识补充】

未来真正做 Application Health Aggregate 时，需要先回答：

```text
哪些组件是 required？
failure 后何时 unhealthy？
何时恢复 healthy？
状态 Owner 是谁？
```

而不是 endpoint 临时拼局部状态。

------

# 20. 最终面试速查表

| 维度              | 核心答案                                                     |
| ----------------- | ------------------------------------------------------------ |
| 工作包            | Health / Readiness / Lifecycle Foundation                    |
| 修改前            | 有 Runtime lifecycle + admission，但无外部 diagnostic contract |
| Health            | Application 是否仍处于非 terminal/unavailable 生命周期       |
| Readiness         | `services + READY + ACCEPTING`                               |
| Endpoint          | `GET /health`、`GET /readyz`                                 |
| Runtime lifecycle | STARTING / READY / SHUTTING_DOWN / CLOSED                    |
| Admission         | ACCEPTING / DRAINING / CLOSED                                |
| READY_DEGRADED    | 只属于 DiagnosticStatus                                      |
| KB degraded       | allowed degraded 时 readyz 仍 200                            |
| DRAINING          | health 200 / readyz 503                                      |
| Projection Owner  | `core/runtime/health.py`                                     |
| Lifecycle Owner   | `ApplicationRuntimeServices._LifecycleControl`               |
| Admission Owner   | `ApplicationRuntimeServices.admission_gate`                  |
| Client Worker     | `ReadinessWorker(QThread)`                                   |
| Probe 模型        | startup-only                                                 |
| Retry             | 1s request / 30s total / 0.5s interval / no jitter           |
| 成功 body         | READY/READY/ACCEPTING/false 或 READY_DEGRADED/READY/ACCEPTING/true |
| Client 失败       | UI 保持；不拉 history；safe unavailable message              |
| 最大 P1           | malformed/inconsistent ready body 被误接受                   |
| 关键 Test Gap     | healthy KB broad catch；AdmissionGate identity 无 durable regression |
| Final test        | 1678 passed / 0 skipped / 42 subtests                        |
| Final Gate        | P0=0 / P1=0 / TEST_GAP=0                                     |
| 未实现            | continuous monitor、reconnect、post-start aggregate health、version compatibility |
| 最不能夸大        | 不要说完整生产监控、版本握手、真实 GUI smoke、distributed health |
| 面试核心词        | Health vs Readiness、Derived Projection、Single Authority、Admission Gate、Fail Closed、Bounded Retry |