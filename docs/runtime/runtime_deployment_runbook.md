# Runtime Deployment Runbook — Windows Native

Stage 3 WP1-B 正式冻结的部署合同。**Windows Native 是当前唯一 certified 部署目标**。

## 1. Deployment Topology

```text
Windows host（Windows 11 / Windows Server）
│
├─ LocalAgent FastAPI Server
│    └─ exactly one LocalAgent server application process（uv run python server.py）
│
└─ PyQt6 Desktop Client
     └─ separate native Windows process（uv run python main.py）
```

- 两个独立 Windows 进程，各自进程启动时执行一次 `Settings.load()`。
- 无 Docker / Compose / WSL2 依赖。
- Server 单进程是合同：**每个部署实例必须且只能有一个 LocalAgent server application process**。

### 禁止的启动方式

```text
uvicorn --workers N
gunicorn multi-worker
multi-process Runtime
```

多进程会破坏以下 process-local Owner（RunRegistry 取消、OutputGate terminal 唯一性、StepResultStore 可见性、Circuit breaker state、Budget ledger、Tool lease、GracefulShutdownCoordinator 唯一 shutdown 编排）。`uvicorn.run` 当前不传 `workers=` 参数，`server.py` 与 `README` 中的 `uvicorn server:app` 均为单进程。

## 2. Windows Prerequisites

| 项 | 要求 |
| --- | --- |
| OS | Windows 11 / Windows Server |
| Python | `>=3.12,<3.13`（以 `pyproject.toml` 为准） |
| uv | 当前 lock/install 工作流以 `uv` 为标准 |
| llama wheel | 当前 Windows 本机 wheel 前置：`llama_cpp_python-0.2.90-cp312-cp312-win_amd64.whl`（`pyproject.toml`/`uv.lock` 引用，与目标平台一致） |
| Linux / Docker | **不要求**；不是当前 certification 目标 |

## 3. Installation

以当前真实 lock/install 工作流为准：

```powershell
uv sync
```

不得修改 `pyproject.toml` 或 `uv.lock`。本地模型（GGUF）与 Embedding 模型属于 Deployment Artifact，需按 §8 单独放置。

## 4. Server Startup

```powershell
uv run python server.py
```

启动链：`Settings.load()` → `validate_role_configuration(SERVER_ROLE)` → lifespan 装配 → `READY`。

明确**单进程**：本命令启动且只启动一个 server application process。

> Server 就绪以 lifespan READY 为界；`GET /readyz` 返回 200（`READY` / `READY_DEGRADED`）即可被 Client startup handshake 视为 ready。Server 本身无 continuous monitoring。

可选替代启动（同样单进程）：

```powershell
uv run uvicorn server:app
```

## 5. Client Startup

Server 应优先启动（Server preferred first），再执行：

```powershell
uv run python main.py
```

Client 启动链：`Settings.load()` → `validate_role_configuration(CLIENT_ROLE)` → UI 装配 → 启动一次 background `ReadinessWorker(QThread)` 做 bounded `GET /readyz` probe。

**Startup readiness handshake = SUPPORTED**：

```text
Client 构造 UI（不阻塞 Qt event loop）
→ background QThread 执行 bounded /readyz probe（request timeout 1.0s /
  total deadline 30.0s / retry interval 0.5s / jitter=none）
→ ready（HTTP 200 + typed four-field body + status ∈ {READY, READY_DEGRADED}）
   → 只触发一次首屏 history fetch
→ unavailable（deadline 耗尽 / 404 / malformed body / 200 + non-ready status）
   → 追加一次固定 safe 系统消息，worker 退出
```

- 成功与失败均为 terminal：worker 一次 probe 后退出，不做 continuous monitoring、不做 auto reconnect、不提供 manual readiness button。
- 每次 request timeout = `min(1.0s, remaining_deadline)`；sleep 也不跨过 deadline。客户端进程退出时 `requestInterruption()` + bounded wait。
- Worker Session 显式使用 `settings.client_trust_env`（与聊天/历史/搜索/取消/记忆 Session 一致），不重新读取 env。
- 版本兼容 / fingerprint：**NOT_IMPLEMENTED（DEFER_TO_WP4）**；WP1-C handshake 只有 `GET /readyz`，没有 `/metadata` / `/version`。

> Client 启动时若 Server 尚未就绪：UI 照常可用，首屏 history 不启动，聊天请求沿用既有单次失败提示。部署建议仍是 Server 先启动。

## 6. Configuration Injection

配置输入只有环境变量（PowerShell 示例），经 `Settings.load()` 唯一解析：

```powershell
# 非 secret 配置
$env:LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION"
$env:LOCAL_AGENT_ENVIRONMENT_ID="prod-region-1"
$env:LOCAL_AGENT_LLM_BACKEND="local"
$env:LOCAL_AGENT_CLIENT_TRUST_ENV="0"
```

配置名称/模板同步见仓库根 `.env.example`（**Application 不自动加载该文件**，只作为 operator 模板文档；不存在 dotenv loader）。

Environment ID 合同（WP1-A）：

```text
LOCAL_AGENT_ENVIRONMENT_ID = operator-provided deployment identity
```

Production 必填。不得自动派生 `hostname`、`machine name`、Windows SID、`instance_id` 或 `git sha`。

## 7. Secrets

第一版 Production Secret Interface = **Environment-only**。以下 secret 只通过进程环境变量注入，不进入源码、日志、Trace/Event/Journal、配置错误原始值或启动脚本：

```text
LOCAL_AGENT_REMOTE_API_KEY    # provider credential（provider-dependent）
LOCAL_AGENT_WIKI_COOKIE       # 仅 client sync 使用
LOCAL_AGENT_REMOTE_API_BASE_URL  # 按 internal endpoint 对待
```

示例（**只展示名称与 placeholder，不展示真实值**）：

```powershell
$env:LOCAL_AGENT_REMOTE_API_KEY="<secret-store-reference>"
$env:LOCAL_AGENT_WIKI_COOKIE="<secret-store-reference>"
```

不实现 Vault/KMS、不实现 `*_FILE` secret interface。`.env.example` 不含 secret 字面值。

## 8. Persistent State / Deployment Artifacts

### Durable State（Windows 持久化路径）

| 数据 | 默认路径 | 分类 | 说明 |
| --- | --- | --- | --- |
| Memory DB | `data/database/agent_memory.db` | Durable State（required） | 业务 Memory；初始化失败 fail fast |
| Runtime Event Journal | `data/database/runtime_event_journal.db` | Durable State（required） | append-only；损坏/追加失败 fail closed |
| Observability checkpoint | `data/database/runtime_observability_checkpoint.db` | Durable State（required） | consumer checkpoint store |
| Snapshot DB | `data/database/runtime_snapshots.db` | Durable State（opt-in） | 仅 `LOCAL_AGENT_SNAPSHOT_ENABLED=true` 时装配 |
| Chroma | `chroma_db/` | Durable State | KB 配置相关；缺失时走 allowlisted degradation（PRODUCTION 默认 required） |
| Knowledge Base | `data/knowledge_base/` | Durable State | 业务 KB；脚本/同步写入 |

### Deployment Artifact（非 Runtime durable state）

| 项 | 默认路径 | 分类 |
| --- | --- | --- |
| GGUF model | `data/models/qwen2.5-7b-instruct-q4_k_m.gguf` | Deployment Artifact（只读加载） |
| Embedding model | `data/models/bge-large-zh-v1.5/` | Deployment Artifact（只读加载） |

模型视为 **deployment artifact，不是 durable state**；模型文件变化时由 operator 更新，不依赖自动恢复。

### Client-local log

| 项 | 路径 |
| --- | --- |
| Client crash log | `data/logs/ui_crash.log`（client 进程启动即打开 append） |

### 边界（禁止声称）

```text
backup implemented            → NOT_IMPLEMENTED
restore implemented           → NOT_IMPLEMENTED
automatic recovery            → NOT_IMPLEMENTED
disaster recovery             → NOT_IMPLEMENTED
```

持久化目录存在**不等于** Runtime Recovery 或 automatic resume。

## 9. Client HTTP Proxy Governance

两个 transport scope，完全独立：

```text
LOCAL_AGENT_REMOTE_TRUST_ENV  = Server → Remote LLM Session
LOCAL_AGENT_CLIENT_TRUST_ENV  = Desktop Client → LocalAgent Server Session
```

`LOCAL_AGENT_CLIENT_TRUST_ENV` 控制 **Desktop Client → LocalAgent Server 的所有 Client HTTP Session** 的 `requests.Session.trust_env`：为 `true`（默认）时继承进程系统 proxy；为 `false` 时不继承。覆盖聊天（`/api/chat`）、历史分页（`/api/history`）、搜索（`/api/search`）、取消（`/api/runtime/runs/{run_id}/cancel`）与记忆管理（`/api/memory`）五类传输；`/api/memory` Session 由 `main.py` 启动快照经 `ChatPanel` 透传给 `MemoryManagerDialog`。所有 Client Session 显式使用 `settings.client_trust_env`，不重新读 env。修改其中一个不得改变另一个。`LOCAL_AGENT_REMOTE_TRUST_ENV` 仍只属于 Server → Remote LLM transport，与 Client proxy 无关。

PowerShell 示例：

```powershell
# 禁止 Client 继承系统 proxy
$env:LOCAL_AGENT_CLIENT_TRUST_ENV="0"
# Server → Remote LLM 独立控制
$env:LOCAL_AGENT_REMOTE_TRUST_ENV="0"
```

## 10. Shutdown Operations

### 正常关闭（graceful）

```text
Ctrl+C / graceful process stop
→ uvicorn
→ FastAPI lifespan exit
→ GracefulShutdownCoordinator
```

完整链：admission settle → run cancel → run drain（`RUNTIME_SHUTDOWN_GRACE_SECONDS`，默认 5.0s）→ force abort remaining → worker drain（`RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS`，默认 5.0s）→ observability/trace flush → model safety gate → component close → CLOSED。

外部进程托管器必须给予足够 shutdown grace（≥ run drain + component close 的坏上界，即默认 5.0 + 5.0 = 10.0s），不能立即强制 kill。

### 强制终止（不保证 graceful）

```text
Stop-Process -Force
taskkill /F
```

**无法保证 graceful shutdown**：不运行 lifespan finally、不做 graceful drain。这是 operator 必须避免的操作。

### Shutdown Truth

```text
ShutdownReport.completed     # 只是 orchestration_completed 兼容别名
ShutdownReport.fully_closed  # 运维判断完整安全关闭应看这个
```

`completed != fully_closed`。判断完整资源关闭必须查看 `fully_closed`（额外要求 remaining run=0、active/detached/unknown worker=0、worker drain IDLE、无 deferred 资源、无 required failure）。

## 11. Windows Process Management

LocalAgent 当前**不提供** Windows Service wrapper，也不引入第三方 wrapper（NSSM、WinSW 等）。正式部署合同是 **single foreground process**；由 operator / 企业内部运行环境负责托管 foreground server process（如企业监控、计划任务、手动启动）。不得虚构企业实际使用了哪种工具。

## 12. Backup / Restore Boundary

Backup/restore 只是 **operational boundary**，不是 automatic recovery：

- 应备份的数据：Memory DB、Journal、Snapshot DB、Chroma DB、Business KB、Observability checkpoint。
- SQLite 文件建议在 stopped/quiesced 状态备份，或使用 SQLite backup API。
- 本阶段**不提供**自动备份/恢复脚本。
- 禁止把复制 SQLite 文件说成 Runtime Recovery；禁止把 Snapshot DB 说成 automatic resume。

## 13. Deployment Rollback

Deployment Rollback 是**人工操作边界**：

```text
known-good code/artifact
+ known-good environment configuration
+ persistent-data compatibility check（SQLite schema 版本兼容）
+ smoke validation（新 identity 请求）
```

与 `CHAT_RUNTIME_MODE=legacy` 的 **Runtime Legacy Rollback** 严格区分：后者是 emergency control，只影响新请求，需要修改 runtime mode 后重启。两者不得混为一谈。

**不实现 automatic deployment rollback。**

## 14. Unsupported

| 项 | 状态 |
| --- | --- |
| Linux certification | Out of Scope（Stage 3 Windows-only） |
| Docker | NOT_IMPLEMENTED（Stage 3 不引入） |
| Docker Compose | NOT_IMPLEMENTED（与 Docker 同步撤销） |
| Multi-worker / multi-process | NOT_IMPLEMENTED（单进程合同） |
| Windows Service wrapper | NOT_IMPLEMENTED（NSSM/WinSW/Task Scheduler 集成代码不提供） |
| Continuous Health/Readiness monitoring | NOT_IMPLEMENTED（Health / Readiness endpoint 与 startup readiness handshake 已 SUPPORTED，但无连续轮询 / auto reconnect / manual readiness button） |
| version compatibility / fingerprint | NOT_IMPLEMENTED（DEFER_TO_WP4；无 `/metadata` / `/version`，无 version compatibility contract） |
| Automatic backup | DEFER TO WP1-D（只定义 boundary） |
| Migration runner / schema migration | DEFER TO WP1-D |

## 15. Known Limitations

- Windows-only certified target；Linux certification Out of Scope，不代表永久不支持。
- single server process only。
- 无 Docker / Compose。
- 无 Windows Service wrapper。
- Continuous Health/Readiness monitoring NOT_IMPLEMENTED（Health / Readiness endpoint 与 startup handshake 为 SUPPORTED，但仅 startup-only，无连续轮询）。
- version compatibility / fingerprint NOT_IMPLEMENTED（DEFER_TO_WP4）。
- 无 automatic backup / restore。
- 无 migration runner。
- 无 automatic deployment rollback。
- force kill（`taskkill /F`、`Stop-Process -Force`）绕过 graceful shutdown。
- Planning executor starvation remains accepted P2。