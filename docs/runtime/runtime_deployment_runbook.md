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

| 数据 | 默认路径 | 分类 | Schema/version | 说明 |
| --- | --- | --- | --- | --- |
| Memory DB | `data/database/agent_memory.db` | Durable State（required） | Memory SQLite `PRAGMA user_version=1` | 业务 Memory；startup preflight 通过后才构造；初始化失败 fail fast |
| Runtime Event Journal | `data/database/runtime_event_journal.db` | Durable State（required） | Journal exact physical signature（无 DB-level version）；row v1/v2 | append-only；损坏/追加失败 fail closed；legacy 缺 span 列需显式 migrate |
| Observability checkpoint | `data/database/runtime_observability_checkpoint.db` | Rebuildable derived state（startup required） | Checkpoint exact table shape（无版本） | 不兼容时显式 recreate；backup optional |
| Snapshot DB | `data/database/runtime_snapshots.db` | Durable State（opt-in） | Snapshot v1（`snapshot_schema_version=1`） | 仅 `LOCAL_AGENT_SNAPSHOT_ENABLED=true` 时装配；无 migration |
| Chroma | `chroma_db/` | Rebuildable derived state（startup required 视 KB_REQUIRED） | LocalAgent collection marker（`localagent_collection_contract_version=1` + `chunk_schema_version=kb_chunk_schema_v2` + embedding digest/dimension） | 缺失时 allowlisted degradation（PRODUCTION 默认 required）；marker mismatch → REBUILD_REQUIRED |
| Knowledge Base | `data/knowledge_base/` | SOURCE_DATA（MUST_BACKUP） | 文件/loader contract（chunk `schema_version=kb_chunk_schema_v2`） | 业务 KB source；Chroma rebuild 的唯一业务输入 |

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
automatic backup              → NOT_IMPLEMENTED（manual stopped-server only）
automatic restore             → NOT_IMPLEMENTED（manual stopped-server set replacement + validation）
automatic deployment rollback → NOT_IMPLEMENTED（code/artifact rollback 需匹配 pre-migration data restore）
downgrade migration           → NOT_IMPLEMENTED
online backup                 → NOT_IMPLEMENTED（live raw copy unsupported）
automatic recovery            → NOT_IMPLEMENTED
disaster recovery             → NOT_IMPLEMENTED
Chroma internal schema migration → NOT_LOCAL_SCHEMA_OWNER（LocalAgent 不修改 Chroma internal SQLite）
```

## 8b. Persistence Preflight / Migration

### Server startup preflight（automatic，READ ONLY）

每次 Server 启动、在任何持久 Store constructor 之前自动执行 SQLite preflight：

```text
Settings Parse / Semantic Validation → SERVER_ROLE Validation → lifecycle STARTING
→ automatic SQLite persistence preflight（PRAGMA quick_check + physical shape + 版本事实；不创建/不修改任何 DB 文件）
→ required Resource Construction → Chroma open + marker validation → 其余构造 → READY
```

- Memory `MIGRATION_REQUIRED` / `UNSUPPORTED` / `FAILED`，或 Journal legacy `MIGRATION_REQUIRED`，
  或 Checkpoint 不兼容，或 Snapshot（enabled）unsupported，或 `PRAGMA quick_check` 非 `ok`：
  startup fail，`never READY`，safe code `PERSISTENCE_*` 由 startup failure boundary 包装。
- Chroma marker mismatch：`knowledge_base_required=true` → 阻止 READY；显式 `false` → `READY_DEGRADED`。
  Startup 绝不自动 clear / rebuild Chroma，绝不自动迁移已有数据。
- `/health`、`/readyz` 不执行 preflight / migration / repair / restore / rebuild（保持只读投影）。

### Explicit migration（SCRIPT_ROLE only，Server stopped）

```powershell
uv run python scripts/manage_persistence.py preflight
uv run python scripts/manage_persistence.py migrate --backup-confirmed
```

- `preflight`：只读；输出每 Store `NEW / CURRENT / MIGRATION_REQUIRED / REBUILD_REQUIRED / UNSUPPORTED / FAILED`。
- `migrate`：先全 Store preflight；任何 UNSUPPORTED/FAILED 或缺少 `--backup-confirmed`（已有数据需要 mutation 时）
  都 non-zero 且零 mutation。每 Store 独立单事务（Memory `user_version=1` 与 schema change 同事务原子提交；
  Journal 只加 nullable span 列绝不 rewrite 历史 row；Checkpoint 只 drop/recreate derived table）。
- `--backup-confirmed` 只是 Operator acknowledgement，不证明备份内容正确；备份正确性由副本 preflight 验证。
- Migration 是 forward-only：迁移提交后 `old binary compatibility NOT ASSUMED`。无 downgrade migration。
- Client 进程绝不打开 / preflight / migrate Server persistence。

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

Backup/restore 是 **manual stopped-server operational contract**，不是 automatic recovery。

### MUST_BACKUP（correctness，必须来自同一次 Server-stopped backup epoch）

1. Memory DB（`data/database/agent_memory.db` + 任何存在的 `-wal`）；
2. Event Journal DB（`data/database/runtime_event_journal.db` + 任何存在的 `-wal`）；
3. Snapshot DB（仅 enabled/存在时：`data/database/runtime_snapshots.db` + `-wal`）；
4. KB source data（`data/knowledge_base/`）；
5. 同一次 deployment 的 known-good environment configuration reference（不记录 secret 明文到报告/仓库）。

Memory / Journal / Snapshot 必须来自同一 backup epoch；Journal 与 Snapshot 不得分别从两个不同时间点恢复后宣称 recovery evidence 一致。

### OPTIONAL_BACKUP

- Chroma directory：可加速 restore，但 correctness 可由 KB source + matching embedding artifact rebuild。

### BACKUP_OPTIONAL / RECREATE

- Observability checkpoint：derived，可 recreate；不是 correctness backup requirement。

### WAL Contract

```text
live raw copy                = unsupported（Server 运行中复制 .db 是非一致快照）
stopped-server backup unit   = 主 .db + 任何存在的 -wal 一起复制
-shm                         = 可重建 coordination sidecar，不作为 correctness 必需文件
backup 文件创建成功          != 备份可恢复；副本必须通过显式 full preflight 才算门禁通过
```

### Restore（manual）

```text
stop Server（确认进程退出；force-kill 不算可信前置）
→ 把当前失败/待调查数据整体移动到隔离位置（不直接覆盖）
→ restore target 为空或已完成整组替换（禁止目录内混合覆盖）
→ 从同一 backup epoch 恢复 Memory / Journal / Snapshot（if enabled）/ KB source
→ 恢复兼容 Chroma（整体恢复并验证 marker）或使用匹配 embedding artifact 从 source 显式 rebuild
→ checkpoint 默认 recreate（即使恢复旧 checkpoint 也必须通过 exact-shape preflight）
→ 显式 full preflight（Server 启动前；不兼容则不启动）
→ 启动 known-compatible code/config/artifact
→ /health + /readyz + Memory/Journal/KB safe functional smoke
```

任一步失败都停止；不对备份原件执行修复/迁移。`files copied != restore validated`。

## 13. Deployment Rollback

Deployment Rollback 是**人工操作边界**，且必须区分 **code/artifact rollback** 与 **persistent-data rollback**：

```text
known-good code/artifact
+ known-good environment configuration
+ persistent-data compatibility check（SQLite schema 版本兼容）
+ smoke validation（新 identity 请求）
```

**Forward-only migration 数据安全合同：**

```text
schema-changing migration committed
→ old binary compatibility NOT ASSUMED
→ binary-only rollback UNSAFE / NOT ASSUMED
→ code rollback 必须同时恢复 matching pre-migration MUST_BACKUP set
```

- 任何 schema migration 提交前必须已取并验证 backup；migration 失败（在任何 commit 前）可只回滚 code/artifact/config。
- 不实现 downgrade migration / reverse SQL；需要回滚旧 binary 时恢复 pre-migration backup set。
- 与 `CHAT_RUNTIME_MODE=legacy` 的 **Runtime Legacy Rollback** 严格区分：后者是 emergency control，只影响新请求，需要修改 runtime mode 后重启；它不能替代 data rollback。两者不得混为一谈。

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
| Automatic backup | NOT_IMPLEMENTED（manual stopped-server only；见 §12） |
| Automatic restore | NOT_IMPLEMENTED（manual stopped-server set replacement + full preflight；见 §12） |
| Automatic deployment rollback | NOT_IMPLEMENTED（见 §13） |
| Downgrade migration | NOT_IMPLEMENTED（forward-only） |
| Online backup | NOT_IMPLEMENTED（live raw copy unsupported） |
| Chroma internal schema migration | NOT_LOCAL_SCHEMA_OWNER（LocalAgent 不修改 Chroma internal SQLite；operator rebuild） |

## 15. Known Limitations

- Windows-only certified target；Linux certification Out of Scope，不代表永久不支持。
- single server process only。
- 无 Docker / Compose。
- 无 Windows Service wrapper。
- Continuous Health/Readiness monitoring NOT_IMPLEMENTED（Health / Readiness endpoint 与 startup handshake 为 SUPPORTED，但仅 startup-only，无连续轮询）。
- version compatibility / fingerprint NOT_IMPLEMENTED（DEFER_TO_WP4）。
- 无 automatic backup / restore（manual stopped-server only）。
- 无 automatic deployment rollback；无 downgrade migration。
- force kill（`taskkill /F`、`Stop-Process -Force`）绕过 graceful shutdown。
- Planning executor starvation remains accepted P2。