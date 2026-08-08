# LocalAgent Stage 3 WP1-B — Windows Deployment / Operations Foundation 面试总结与学习

## 1. 一句话项目 / 工作包定义

我在 LocalAgent 已完成 Agent Runtime（智能体运行时）之后，把原本“开发机上能启动”的运行方式收敛成了一套明确的 **Windows Native Deployment（Windows 原生部署）合同**：确定单进程 Server / 独立桌面 Client 的部署拓扑，补齐配置与 Secret（密钥）注入、Client Proxy（客户端代理）治理、持久化数据边界、优雅关闭和人工回滚 Runbook（运行手册），并通过 Deployment Contract Test（部署合同测试）和全量回归保证这些约束不会漂移。

最终 WP1-B Final Documentation Re-Gate：

```text
P0 = 0
P1 = 0
P2 = 2
DOC_ONLY = 0

WP1-B = PASS
WP1-B completed = YES
```

最终全量测试为：

```text
1596 passed
3 warnings
42 subtests passed
```

------

# 2. 为什么要做这次修改

## 2.1 修改前真实状态

这次工作**不是由一个用户线上事故直接触发的**，主要来源于 Stage 3 Production Readiness Audit（生产就绪审计）。

源码审计发现，当时 LocalAgent 虽然已经能通过：

```text
uv run python server.py
uv run python main.py
```

运行，但“如何部署”仍主要是隐式事实，而不是正式 Contract（合同）。

审计确认：

- Server 是单 Uvicorn 进程；
- PyQt6 Client 与 FastAPI Server 是两个独立进程；
- RunRegistry、CancellationSource、OutputGate、StepResultStore、Circuit Breaker State、Budget Ledger、Tool Lease 等都是 process-local（进程内）状态；
- 没有 Docker / Compose；
- 没有 Windows Service wrapper；
- Client 启动后直接访问 Server，没有 wait-ready / startup retry / version handshake；
- Client 的 HTTP Session 默认继承系统 Proxy，没有项目级 Client Proxy 配置。

所以真正的问题不是：

> “加一个启动脚本。”

而是：

> **现有 Runtime 的状态所有权决定了什么样的部署拓扑才是安全的，而这个拓扑、配置输入、持久化目录和关闭语义此前没有形成可测试的工程合同。**

------

## 2.2 为什么不能简单开多个 Worker

审计发现大量核心状态都是 process-local：

```text
RunRegistry
CancellationSource
OutputGate
StepResultStore
Circuit breaker state
Budget ledger
Tool lease / concurrency state
GracefulShutdownCoordinator
```

例如跨两个 Uvicorn Worker：

```text
Worker A:
RunRegistry A
OutputGate A

Worker B:
RunRegistry B
OutputGate B
```

这两个 Worker 之间没有共享的：

```text
Run Registry
Terminal Owner
Cancellation State
Step Result State
```

于是可能出现：

- 一个进程无法取消另一个进程中的 Run；
- OutputGate 的 at-most-once（至多一次）语义只在单进程 Run scope 内成立；
- StepResultStore 无法跨进程共享；
- Circuit / Budget / Tool concurrency 不再是全局一致状态；
- Shutdown 会出现多个 Application-level Owner。

因此 WP1-B 没有“顺手支持多 Worker”，而是把：

```text
exactly one LocalAgent server application process
```

正式冻结为当前部署合同。

------

# 3. 真实性与完成边界

| 内容                                       | 类型                  | 当前状态                   | 证据                                            |
| ------------------------------------------ | --------------------- | -------------------------- | ----------------------------------------------- |
| Server / Client 为两个独立进程             | 源码审查发现          | 已确认                     | `server.py` / `main.py` 审计                    |
| Runtime 大量关键状态 process-local         | 源码审查发现          | 已确认                     | RunRegistry、OutputGate、StepResultStore 等审计 |
| 多 Worker 当前不安全                       | 源码审查 + 架构判断   | 正式冻结为 NOT_IMPLEMENTED | Deployment Contract                             |
| Windows Native 为 Stage 3 certified target | 架构决策              | 已实现并验收               | Windows Addendum + Final Gate                   |
| Docker / Compose                           | 原方案讨论            | REJECTED / 未实现          | Windows Addendum                                |
| `LOCAL_AGENT_CLIENT_TRUST_ENV`             | 实施                  | 已实现                     | Settings + Client Session                       |
| Client / Remote Proxy scope 分离           | 实施                  | 已实现且测试               | Deployment tests                                |
| `/api/memory` 第四 Session 漏接 Proxy 配置 | Final Gate 真实发现   | 已修复                     | P1 remediation                                  |
| 测试 inventory 只扫描 main.py              | Final Gate 真实发现   | 已修复                     | inventory 扩展至 `main.py + ui/`                |
| 正式文档仍停留 main.py-only                | Re-Gate 真实发现      | 已修复                     | DOC_ONLY remediation                            |
| Server READY + graceful shutdown           | 实际测试              | 已验证                     | Phase 3 / Codex smoke                           |
| Health / Readiness                         | 后续规划              | 未实现                     | WP1-C                                           |
| Startup handshake / retry                  | 后续规划              | 未实现                     | WP1-C                                           |
| Migration Runner                           | 后续规划              | 未实现                     | WP1-D                                           |
| Automatic backup / restore                 | 后续规划              | 未实现                     | Known Limitation                                |
| Windows Service wrapper                    | 当前非目标            | 未实现                     | Known Limitation                                |
| Linux / Docker certification               | 当前 Out of Scope     | 未实现                     | Windows Addendum                                |
| Planning executor starvation               | 既有 Known Limitation | Accepted P2                | Final Gate                                      |
| 3 条 ChatService deprecated warning        | 既有问题              | P2                         | Final Gate                                      |

最终 Gate 明确只允许说：

> **Windows Deployment / Operations Foundation completed。**

不能因此说：

> WP1 complete、Stage 3 Production Ready、Linux Supported、Distributed Runtime。

------

# 4. 修改前架构与根因

## 4.1 修改前部署链

```text
Windows
│
├── uv run python server.py
│      ↓
│   FastAPI / Uvicorn
│      ↓
│   server.py::lifespan()
│      ↓
│   Runtime
│
└── uv run python main.py
       ↓
    PyQt6 Client
       ↓
    requests.Session()
       ↓
    LocalAgent Server
```

这个架构本身已经存在。

真正缺失的是：

```text
“事实存在”
≠
“部署合同已经建立”
```

------

## 4.2 根因一：Runtime 是单进程 Owner 模型

RunRegistry 等组件不是 Distributed Registry（分布式注册表），而是进程内对象。

所以部署层不能脱离 Runtime 架构随意决定：

```text
workers = 4
```

这实际上会改变系统语义。

核心知识：

> **Deployment topology（部署拓扑）并不是 Runtime 架构之外的纯运维问题。**

如果 Runtime 的一致性边界是：

```text
process
```

那么 Deployment 也必须尊重：

```text
process
```

------

## 4.3 根因二：Client Proxy 没有显式 Owner

修改前：

```text
requests.Session()
```

使用 Requests 默认：

```text
trust_env = True
```

也就是说是否使用：

```text
HTTP_PROXY
HTTPS_PROXY
NO_PROXY
```

由进程环境隐式决定。

Server → Remote LLM 已经有：

```text
LOCAL_AGENT_REMOTE_TRUST_ENV
```

但 Desktop Client → LocalAgent Server 没有对应配置。

如果直接复用 Remote 配置，就会混淆两个 Transport Scope（传输作用域）：

```text
Desktop Client → LocalAgent Server

Server → Remote LLM
```

所以最终新增独立：

```text
LOCAL_AGENT_CLIENT_TRUST_ENV
```

------

# 5. 方案讨论与技术取舍

## 5.1 Docker vs Windows Native

初版 Architecture Decision 曾考虑：

```text
Docker
Compose
Linux container
```

但随后真实目标被明确为：

```text
只要求 Windows 部署
```

因此 Windows Addendum 正式覆盖初版决定：

```text
Docker = REJECTED
Compose = REJECTED
Linux certification = OUT OF SCOPE
Windows Native = certified target
```

同时撤销了原来因为 Windows `llama-cpp-python` wheel 无法在 Linux 使用而产生的 blocker。

这是一个很好的面试 Trade-off（权衡）：

> 我没有为了显得“生产化”就强行加 Docker，而是根据真实运行目标选择 Windows Native。因为本地模型、PyQt Client、SQLite、Chroma 和现有 Windows wheel 本来就在 Windows 环境中运行，容器化在这个阶段增加的复杂度超过了收益。

------

## 5.2 为什么不做 Windows Service

实际选择：

```text
single foreground process
```

由 Operator（运维人员）或企业内部进程托管环境负责。

拒绝：

```text
NSSM
WinSW
自研 Windows Service Wrapper
```

原因不是这些方案不好，而是：

> WP1-B 是最小必要生产化，不应该把进程托管框架本身变成新的工程项目。

------

## 5.3 为什么不让 UI 自己读取 Settings

第四 Session 修复时完全可以在：

```text
ui/memory_dialog.py
```

里直接：

```text
Settings.load()
```

但最终拒绝了这种方式。

原因是这会形成：

```text
main.py Settings snapshot
+
MemoryDialog Settings snapshot
```

两个配置读取点。

最终数据流是：

```text
Settings.load()
↓
main.py startup snapshot
↓
ChatPanel
↓
MemoryManagerDialog
↓
Session
```

UI 只负责传递值，不拥有配置。

------

# 6. 最终架构

## 6.1 Deployment Topology

```text
Windows Host
│
├── LocalAgent FastAPI Server
│     exactly one application process
│
└── PyQt6 Desktop Client
      separate native process
```

Server：

```text
uv run python server.py
```

Client：

```text
uv run python main.py
```

没有：

```text
Docker
Compose
multi-worker
Windows Service wrapper
```

------

## 6.2 Client Proxy 最终架构

```text
Settings
  │
  │ client_trust_env
  ▼
main.py startup snapshot
  │
  ├── ApiWorker Session
  ├── Search / Cancellation Session
  ├── History Session
  │
  └── ChatPanel
        │
        ▼
      MemoryManagerDialog
        │
        ▼
      /api/memory Session
```

最终共扫描到 4 个 Client HTTP Session creation point，并全部受同一配置治理。

关键点不是“四个 Session”，而是：

> **配置只有一个 Owner，但可以有多个 Consumer。**

------

# 7. 核心状态机和时序

WP1-B 没有新增 Runtime 状态机，但它把现有生命周期转换成了 Deployment Contract。

## 7.1 Startup

```text
Process starts
   ↓
Settings.load()
   ↓
Role Validation
   ↓
FastAPI lifespan
   ↓
Resource Construction
   ↓
READY
```

Client 当前部署顺序：

```text
Server first
↓
Client second
```

注意：

这只是操作顺序。

当前**没有**：

```text
wait-ready
startup retry
version handshake
```

这些属于 WP1-C。

------

## 7.2 Shutdown

```text
Ctrl+C / graceful process stop
       ↓
Uvicorn
       ↓
FastAPI lifespan finally
       ↓
GracefulShutdownCoordinator
       ↓
close admission
       ↓
cancel runs
       ↓
run drain
       ↓
force abort remaining
       ↓
worker drain
       ↓
flush
       ↓
close components
```

这里必须记住：

```text
ShutdownReport.completed
!=
ShutdownReport.fully_closed
```

`completed` 只说明 Shutdown orchestration 已经完成；

运维真正判断资源是否安全释放，应看：

```text
fully_closed
```

------

## 7.3 Force Kill

```text
taskkill /F
Stop-Process -Force
```

可能绕过：

```text
lifespan finally
```

因此不能声称 Force Kill 也具有 Graceful Shutdown（优雅关闭）保证。

------

# 8. 数据、权限与 Owner 边界

本 WP 最值得面试讲的是 Owner。

## Settings

Owner：

```text
Settings
```

负责：

```text
LOCAL_AGENT_CLIENT_TRUST_ENV
```

解析。

UI：

```text
ChatPanel
MemoryManagerDialog
```

都不是配置 Owner。

------

## Client Session

Session 是配置 Consumer：

```text
Session.trust_env
```

不负责决定配置。

------

## Deployment Topology

正式部署合同：

```text
single application process
```

但这不是新 Runtime Owner。

原因是 Runtime 原有：

```text
RunRegistry
OutputGate
StepResultStore
GracefulShutdownCoordinator
```

仍然保持原 Owner。

Deployment 只尊重其边界。

------

## Persistent Data

Durable State（持久状态）：

```text
agent_memory.db
runtime_event_journal.db
runtime_observability_checkpoint.db
runtime_snapshots.db  # opt-in
Chroma
Knowledge Base
```

Deployment Artifact（部署制品）：

```text
GGUF model
Embedding model
```

模型文件不是 Runtime Durable State。

------

# 9. 兼容策略

## Client Proxy 默认兼容

新增：

```text
LOCAL_AGENT_CLIENT_TRUST_ENV
```

默认：

```text
True
```

原因：

修改前 Requests 默认就是：

```text
trust_env=True
```

因此新配置不会改变现有行为。

------

## Constructor 兼容

`ChatPanel` / `MemoryManagerDialog`：

```text
client_trust_env=True
```

作为默认参数。

但正式生产链仍显式传：

```text
settings.client_trust_env
```

默认值只是兼容旧构造方式，不是第二配置来源。

------

## Runtime 兼容

WP1-B 没有修改：

```text
RunCoordinator
Scheduler
OutputGate
ToolExecutionService
Recovery
Trace Contract
```

这是刻意控制改动范围，而不是遗漏。

------

# 10. Bad Cases

## Bad Case 1：Client Proxy 只覆盖了三个 Session

- 类型：实施 / Final Gate 真实发现

- 触发条件：配置 `LOCAL_AGENT_CLIENT_TRUST_ENV=false`

- 故障表现：Chat、History、Search、Cancel 不使用系统 Proxy，但 Memory Dialog 仍使用 Requests 默认 `trust_env=True`

- 根因：Phase 3 的 HTTP inventory 只扫描 `main.py`，遗漏 `ui/memory_dialog.py`

- 修复方案：

  ```text
  Settings snapshot
  → ChatPanel
  → MemoryManagerDialog
  → Session.trust_env
  ```

- 回归测试：

  - `test_client_session_inventory_expects_four_sessions`
  - `test_memory_dialog_session_honors_client_trust_env`

- 对应知识点：

  - Transport Scope
  - Configuration Owner
  - Inventory-based Contract Test

- 面试表达：

> 我们第一次 Final Gate 时发现，Client Proxy 配置虽然实现了，但测试只扫描 main.py，漏掉了 UI 目录下 Memory Dialog 的 Session。这个问题说明“配置实现了”不等于“整个 transport scope 都受治理”。后来我把 Session inventory 扩展到 main.py + ui，并通过 constructor plumbing 把启动期 Settings 快照传到 Memory Dialog，没有让 UI 自己重新读取配置。

- 当前状态：**CLOSED**。

------

## Bad Case 2：功能修好了，但正式 Contract 仍然是旧事实

- 类型：Re-Gate 真实发现
- 触发条件：P1 修复后重新审查文档
- 故障表现：
  - 源码已经有 4 个 Session；
  - Configuration Reference / Runbook / Capability Matrix / Owner Matrix 仍只写 main.py Session
- 根因：
  - implementation truth 与 documentation truth 漂移
- 修复方案：
  - 更新四份正式文档；
  - 新增 4 个 Documentation Guard（文档防漂移测试）
- 回归测试：
  - Configuration Reference guard
  - Deployment Runbook guard
  - Capability Matrix guard
  - Owner Matrix guard
- 对应知识：
  - Contract as Code
  - Documentation Drift
  - Architecture Guard
- 面试表达：

> 第二轮 Re-Gate 更有意思，代码功能已经对了，但正式 Capability Matrix 和 Owner Matrix 还停留在旧 inventory。我没有把它当成无关紧要的文档问题，因为这些文档是后续设计的 Authority。最后补了局部语义 guard，保证以后新增 Client Session 时不会再出现源码和 Contract 分叉。

- 当前状态：**CLOSED**。

------

## Bad Case 3：Linux Docker Blocker 是错误目标造成的“假阻塞”

- 类型：架构决策修订，不是生产事故

- 原始情况：

  - 初版 Decision 计划 Linux Docker；
  - 项目依赖 Windows `cp312-win_amd64` llama wheel；
  - 因此出现 Cross-platform blocker。

- 后续真实约束：

  - 当前 Stage 3 只要求 Windows。

- 最终处理：

  ```text
  Docker = REJECTED
  Linux certification = OUT OF SCOPE
  packaging change = REJECTED
  ```

- 知识点：

  - Requirement-driven Architecture
  - Avoid Overengineering

- 面试表达：

> 初版部署设计曾考虑 Docker，结果会迫使我解决一个本阶段根本不需要的 Linux native dependency 问题。确认真实部署目标只有 Windows 后，我把 Docker 和跨平台 packaging 从 Stage 3 Scope 中移除，而不是为了形式上的生产化继续扩大改造范围。

- 当前状态：架构修订已冻结。

------

# 11. 测试与验收

最终 Documentation Re-Gate 实际执行：

```text
tests/test_deployment_contract.py
36 passed

configuration regression
110 passed

collect-only
1596 collected

full pytest
1596 passed
3 warnings
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

之前 P1 remediation 还真实执行过：

```text
shutdown regression
10 passed

critical runtime regression
52 passed
```

Server Startup Smoke（启动烟测）也真实验证过：

```text
FastAPI lifespan startup
→ READY
→ graceful shutdown
```

但最终 Documentation Re-Gate **没有重新执行** startup smoke，而是明确记录：

```text
NOT_RERUN
```

这是面试中很值得注意的真实性边界。

------

# 12. 当前 Known Limitations

## 1. Windows-only certified target

不能说：

> “跨平台部署已经完成。”

当前只验收 Windows。

未来如果需要 Linux，必须重新处理 native dependency / packaging 等问题。

------

## 2. Single server process only

不能说：

> “支持多 Worker 横向扩展。”

当前 Runtime 的重要状态仍是 process-local。

未来演进多进程必须先解决：

```text
Distributed Registry
Cross-process Cancellation
Terminal Ownership
Shared Result State
Distributed Budget / Lease
```

------

## 3. 无 Docker / Compose

这是明确 Reject / Out of Scope，不是忘了实现。

------

## 4. 无 Windows Service wrapper

当前 Server 是 foreground process。

进程托管交给外部运行环境。

------

## 5. 无 Health / Readiness

下一阶段 WP1-C。

不能把：

```text
进程存活
```

等同于：

```text
readiness
```

------

## 6. 无 startup handshake / retry

Client 仍然要求 Server 先启动。

没有自动等待 READY。

------

## 7. 无 automatic backup / restore

这里只定义了持久化数据边界。

------

## 8. 无 Migration Runner

属于 WP1-D。

------

## 9. 无 automatic deployment rollback

目前是人工 Runbook。

------

## 10. Force Kill 不保证 Graceful Shutdown

`taskkill /F` 等可能跳过 lifespan。

------

## 11. Planning executor starvation

Accepted P2，WP1-B 没处理。

------

## 12. 既有 Deprecated Warning

仍有 3 次：

```text
ChatService event_channel_capacity
```

deprecation warning。

Final Gate 将其作为一个既有 P2 family。

------

# 13. 这次修改真正体现了哪些工程能力

## 13.1 Production Readiness Audit（生产就绪审计）

不是看到：

```text
python server.py 能运行
```

就认为已经可部署。

而是审计：

```text
Process Model
State Ownership
Persistence
Secrets
Shutdown
Configuration
Client/Server dependency
```

------

## 13.2 Owner / Scope 设计

最典型的是 Client Proxy：

```text
Owner = Settings
Scope = Desktop Client → LocalAgent Server
Consumer = HTTP Sessions
```

UI 只做 plumbing。

------

## 13.3 Deployment 与 Runtime 边界判断

知道：

> 多 Worker 不是修改 Uvicorn 参数，而是会改变 Runtime consistency boundary（一致性边界）。

------

## 13.4 Contract Test

测试不只是：

```text
function returns True
```

还检查：

```text
所有 Client HTTP Session 是否都进入治理范围
正式文档是否和真实 owner 一致
single-process contract 是否漂移
secret 是否进入模板/脚本
```

------

## 13.5 Scope Control

最能体现这一点的是：

```text
不做 Docker
不做 Windows Service
不做 Health
不做 Migration
```

工程能力不只是会加功能，也包括知道什么**不应该现在做**。

------

# 14. 30 秒面试表达

我在 LocalAgent Runtime 完成后做过一轮最小生产化，WP1-B 主要解决部署边界问题。源码审计发现 Runtime 的 RunRegistry、OutputGate、Cancellation、Budget 等状态都是进程内的，所以我没有直接上多 Worker，而是把 Windows 下单 Server 进程、独立 PyQt Client 冻结成部署合同。同时补了 Client HTTP Proxy 的显式配置、持久化目录、Secret、Shutdown 和回滚 Runbook。Final Gate 还发现过一个 UI 里的 Memory Session 漏接 Proxy 配置，我通过全量 Session inventory 和 Contract Test 修掉了。最后全仓 1596 个测试通过，P0/P1 和文档问题都清零，但 Health、Migration、多 Worker 这些仍然明确留在后续阶段。

------

# 15. 2 分钟面试表达

我这个项目在 Runtime 完成以后，并不是直接说“可以生产部署了”，而是先做了一轮 Production Readiness Audit。

审计里一个很重要的发现是，LocalAgent 现在很多一致性状态都是 process-local，比如 RunRegistry、Cancellation、OutputGate、StepResultStore、Circuit Breaker 和 Budget。也就是说，如果简单把 Uvicorn 改成多个 Worker，其实会破坏取消、终态唯一性和并发配额这些 Runtime 语义。所以这一阶段我没有去做分布式改造，而是先冻结 Windows Native 的单 Server 进程部署合同，PyQt Client 作为独立进程运行。

另外我把 Client 到 Server 的 Proxy 行为也显式化了。之前 requests 默认 `trust_env=True`，会自动继承系统代理，而 Server 调远端模型已经有自己的 Remote Proxy 配置。为了避免两个 transport scope 混在一起，我新增了独立的 `LOCAL_AGENT_CLIENT_TRUST_ENV`，由 Settings 单点解析，然后把启动期快照传给所有 Client HTTP Session。

这里 Final Gate 真发现过一个问题：最初只覆盖了 main.py 里的三个 Session，漏了 Memory Dialog 里的 `/api/memory` Session。后来没有让 UI 再读一次 Settings，而是通过 `main.py → ChatPanel → MemoryManagerDialog` 显式传值，同时把测试从 main.py-only 扩成 main.py + ui 的 Session inventory。功能修完后 Re-Gate 又发现正式 Owner Matrix 和 Capability Matrix 还是旧事实，我又补了文档 guard。

最终 Gate 是 P0=0、P1=0、DOC_ONLY=0，全仓 1596 passed。这个阶段我只认为 Windows Deployment Foundation 完成了，Health/Readiness、Migration、多 Worker 和自动备份都没有包装成已经实现。

------

# 16. 深入版本

如果面试官继续追问，我会重点从三个层面展开。

第一层是 **部署为什么受到 Runtime 架构约束**。

很多人会觉得部署只是 Docker、Uvicorn 参数的问题，但我的 Runtime 里 RunRegistry、OutputGate、CancellationSource 等对象都有明确 process scope。比如 OutputGate 保证某个 Run 的 final output at-most-once，这个语义成立的前提是同一个 Run 的 terminal authority 在一个 scope 内。如果简单启动多个完全独立进程，却没有共享 Registry 或分布式协调，那么这个假设就不成立。所以我选择先冻结单进程，而不是伪造横向扩展能力。

第二层是 **配置 Owner**。

Client Proxy 不是把四个地方分别加一个环境变量读取。我要求 `Settings` 是唯一 raw env owner：

```text
Environment
→ Settings
→ Application Snapshot
→ Consumer
```

Memory Dialog 虽然最终使用配置，但只消费 bool 值，不知道环境变量叫什么。这避免了 UI、Worker 各自解析配置。

第三层是 **Contract 和 Gate**。

这次经历了两次比较有价值的 Gate failure。

第一次是运行时 P1：测试 inventory 只扫 main.py，漏了 UI Session。

第二次功能已经正确，但是 Capability Matrix、Owner Matrix 等正式文档仍然是旧 inventory。

这让我形成的原则是：

```text
Implementation truth
+
Test truth
+
Contract truth
```

三者都要一致，才能真正关闭工程改动。

------

# 17. 高频追问与参考答案

## Q1：为什么你们不支持 Uvicorn 多 Worker？

因为当前 Runtime 不是跨进程架构。RunRegistry、CancellationSource、OutputGate、StepResultStore、Circuit state、Budget 和 Tool lease 都是 process-local。直接上多 Worker 会导致一个 Worker 看不到另一个 Worker 的运行状态，也无法保持全局 terminal owner 和 cancellation 语义。所以 WP1-B 正式冻结 single application process，而没有虚构多进程能力。

------

## Q2：为什么不直接用 Docker？

最终真实部署要求只有 Windows，本地模型依赖也是 Windows wheel，而且还有原生 PyQt Client。本阶段没有 Windows Container 的真实需求，所以 Docker 增加的复杂度没有对应收益。初版曾考虑 Docker，但 Windows-only Addendum 后明确 Reject，`pyproject.toml` 和 `uv.lock` 也禁止为了 Linux 改动。

------

## Q3：`trust_env` 是什么？

【通用知识】

Requests 的 Session 可以通过 `trust_env` 决定是否信任环境中的 Proxy、认证等配置。项目里最重要的是 Proxy。

项目中：

```text
LOCAL_AGENT_CLIENT_TRUST_ENV
```

治理：

```text
Desktop Client → LocalAgent Server
```

而：

```text
LOCAL_AGENT_REMOTE_TRUST_ENV
```

治理：

```text
Server → Remote LLM
```

两个 Scope 独立。

------

## Q4：为什么不在 MemoryDialog 里直接 `Settings.load()`？

因为会破坏 Settings Single Source of Truth（单一事实来源）和 Application-scope snapshot。最终配置由 main.py 启动时解析一次，UI 组件只通过 constructor 传递 resolved value。这样配置解析和业务消费是解耦的。

------

## Q5：你们怎么保证以后新增 Session 不再遗漏？

`tests/test_deployment_contract.py` 有 Session inventory guard，扫描 `main.py + ui/*.py` 的 Client Session creation point。当前是 4 个，如果 inventory 改变就会失败，而不是静默新增一个默认继承系统 Proxy 的 Session。

------

## Q6：为什么文档错误也会导致 Gate FAIL？

因为这些不是普通 README，而包括：

```text
Configuration Reference
Capability Matrix
Owner Matrix
Deployment Runbook
```

它们是后续工程工作的正式 Authority。

如果源码已经有 Memory Session，但 Owner Matrix 还写 main.py-only，那么下一次工程师根据文档修改架构时就会得到错误事实。

所以最后还增加了 Documentation Contract Guard。

------

## Q7：Windows Native 怎么保证生产可用？

不能说已经“Production Certified”。

本阶段真实做的是：

- 明确部署拓扑；
- 明确配置输入；
- 明确 Secret boundary；
- 明确持久状态；
- 明确启动关闭；
- 真实 Startup Smoke；
- Deployment Contract tests；
- Full regression。

但 Health、Readiness、Migration、Service manager 等能力还没完成。

------

## Q8：`fully_closed` 和 `completed` 有什么区别？

`completed` 是 Shutdown orchestration 完成的兼容语义。

`fully_closed` 还要求：

```text
remaining runs = 0
workers = 0
no deferred resources
no required close failure
```

所以部署运维判断完整安全退出应该看 `fully_closed`。

------

## Q9：为什么 Proxy 配置默认 True？

为了保持兼容。

修改前 Requests 的默认就是 `trust_env=True`。如果新增配置后默认 False，会改变现有 Client 行为。

所以：

```text
absent → True
```

显式配置才改变行为。

------

## Q10：你的 Deployment Contract Test 和普通单测有什么区别？

普通单测更关注一个函数。

Deployment Contract Test 关注跨文件工程不变量，比如：

```text
Server 必须单进程
Client transport 全部受 Proxy 配置控制
UI 不成为第二 env reader
pyproject/uv.lock 不发生 packaging 漂移
正式 Owner Matrix 与源码一致
Secret 不出现在模板和启动脚本
```

它更接近 Architecture Guard（架构守卫）。

------

# 18. 容易答错或夸大的问题

## 问题：你们现在支持生产级多 Worker 吗？

错误：

> 支持，FastAPI 本身可以多 Worker。

为什么错误：

FastAPI 能启动多 Worker ≠ LocalAgent Runtime 语义支持多 Worker。

推荐：

> 当前明确只支持单 Server application process，多进程需要重新设计共享 Registry、Cancellation 和 terminal ownership。

------

## 问题：你们用 Docker 部署吗？

错误：

> 做了完整容器化。

推荐：

> 当前 Stage 3 的 certified target 是 Windows Native，Docker 是明确 Out of Scope。

------

## 问题：有自动故障恢复吗？

错误：

> 有 Snapshot 和持久化目录，所以能自动恢复。

推荐：

> Snapshot 当前仍是 validation-only recovery 体系的一部分，WP1-B 只是定义持久化边界，没有 automatic recovery。

------

## 问题：你们做了灾备吗？

错误：

> SQLite 都落盘，所以有灾备。

推荐：

> 落盘只说明 persistence；backup/restore 和 disaster recovery 没有实现。

------

## 问题：Gate 全绿是不是 Stage 3 Production Ready？

错误：

> 是。

推荐：

> 这里只是 WP1-B PASS。WP1-C Health/Readiness 和 WP1-D Migration 还没完成。

------

## 问题：第四 Session 是线上真实事故吗？

错误：

> 线上发现 Memory 接口走错代理。

推荐：

> 这是 Final Gate 源码审查发现的真实实现缺口，不是用户线上事故。

------

# 19. 本次需要重点复习的知识点

## P0：必须掌握

### 1. Process-local vs Distributed State

必须能解释：

```text
为什么 process-local RunRegistry
→ 决定当前不能安全 multi-worker
```

至少掌握：

- process memory isolation；
- cross-process coordination；
- ownership；
- consistency boundary。

------

### 2. Owner / Consumer / Scope

这是本次最核心概念。

必须能解释：

```text
Settings = Owner
main.py = snapshot consumer
ChatPanel = forwarder
MemoryDialog Session = final consumer
```

------

### 3. Deployment Contract

不要把它理解成“部署文档”。

它是：

> 对运行拓扑、状态边界、启动方式和不支持能力的可测试约束。

------

### 4. Graceful Shutdown

必须掌握：

```text
signal
→ lifespan
→ shutdown coordinator
→ drain
→ close
```

以及：

```text
completed != fully_closed
```

------

### 5. Contract Test / Architecture Guard

需要能讲出为什么：

```text
全量 pytest PASS
```

仍然可能 Final Gate FAIL。

本项目就真实发生过。

------

## P1：很可能追问

### 6. `requests.Session.trust_env`

掌握：

- 系统 Proxy；
- 环境变量；
- 为什么显式配置；
- 为什么 Client / Remote 两个 scope 分离。

------

### 7. Fail Fast / Fail Closed

例如 Server Smoke 第一轮 remote backend 没配置 endpoint 会在 Startup Validation 阶段拒绝启动。

这是正确的配置安全行为，不是 Runtime Crash。

------

### 8. Durable State vs Deployment Artifact

会区分：

```text
Memory DB
Journal
Checkpoint
```

和：

```text
GGUF model
Embedding model
```

------

### 9. Configuration Snapshot

为什么不是每次 Request 读一次环境变量。

重点理解：

```text
Application scope configuration
```

------

## P2：扩展知识

### 10. 多进程 Runtime 如何演进

【通用知识扩展，不是项目已实现】

未来可以考虑：

```text
shared/distributed Run Registry
distributed cancellation
distributed lock / lease
external durable state
global terminal arbitration
```

这属于系统设计扩展。

------

### 11. Windows Service / Process Supervisor

知道 NSSM、WinSW、Windows Service 等概念即可。

项目当前没有实现。

------

### 12. Containerization Trade-off

面试中重点不是背 Docker，而是回答：

> 什么情况下 Docker 值得引入，什么情况下属于 Overengineering（过度工程）。

------

# 20. 最终面试速查表

| 维度                | 我需要记住的核心内容                                         |
| ------------------- | ------------------------------------------------------------ |
| 问题                | Runtime 已能运行，但 Deployment topology、Proxy、Persistence、Shutdown 等仍是隐式事实 |
| 根因                | 大量 Runtime 状态 process-local；部署不能脱离 Runtime consistency boundary |
| 核心方案            | Windows Native + 单 Server process + 独立 Client + Deployment Contract |
| Proxy               | `CLIENT_TRUST_ENV` 和 `REMOTE_TRUST_ENV` 分离                |
| Owner               | Settings 唯一配置 Owner；UI 只透传 resolved value            |
| 最大设计取舍        | 不为了“生产化”强行 Docker / multi-worker / Windows Service   |
| 最难 Bad Case       | Final Gate 发现 `/api/memory` 第四 Session 漏接 Proxy        |
| 第二个关键 Bad Case | 功能已经修复，但正式 Owner/Capability 文档仍漂移             |
| Shutdown            | Uvicorn → lifespan → GracefulShutdownCoordinator；`completed != fully_closed` |
| 测试                | Deployment 36；Full 1596 passed；3 warnings；42 subtests     |
| Gate                | P0=0 / P1=0 / P2=2 / DOC_ONLY=0                              |
| Known Limitation    | Windows-only、single-process、无 Health、无 Migration、无自动 Backup/Recovery |
| 30 秒关键词         | Production readiness audit、process-local、single-process、Config Owner、Proxy Scope、Contract Test |
| 最容易被追问        | 为什么不能 multi-worker、为什么不用 Docker、第四 Session 怎么漏的、为什么文档也能阻断 Gate |
| 最不能夸大          | Production Ready、Distributed、Automatic Recovery、Docker、Linux、多 Worker |