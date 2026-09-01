# Runtime Operations Runbook

## Deployment Boundaries

- **Windows Native 是 Stage 3 唯一 certified 部署目标**（Windows 11 / Windows Server + Python 3.12 + uv）。
- **Single-process contract**：每个部署实例必须且只能有一个 LocalAgent server application process（`uv run python server.py`）。禁止 `uvicorn --workers N`、gunicorn、multi-process Runtime。多进程会破坏 RunRegistry 取消、OutputGate terminal 唯一性、StepResultStore 可见性、并发配额与 Shutdown 编排（这些 Owner 全部 process-local）。
- 无 Docker / Compose / WSL2 依赖；无 Windows Service wrapper（operator/企业内部环境可托管 foreground process，LocalAgent 不提供 wrapper）。
- 完整 Windows 部署、Rollback、持久化数据、Secret、Proxy 与 Shutdown 运维参见 `runtime_deployment_runbook.md`。
- Health / Readiness：`GET /health` 与 `GET /readyz`（SUPPORTED）；状态矩阵见下方「Health / Readiness」章节。Continuous monitoring / version compatibility：NOT_IMPLEMENTED。

## Startup Runbook

1. 用 `Settings.load()` 完成 Settings Parse（所有显式 bool/int/float/enum 严格解析，非法值 fail closed）与 Semantic Validation（range/finite/cross-field/Profile/backend/metadata identifier）。
2. entrypoint（server/client）执行 Process-role Startup Validation：SERVER role 要求 remote/hybrid backend 具备 endpoint，PRODUCTION 要求 HTTPS 与 `remote_verify_tls=True`；CLIENT role 不要求 server-only 字段。
3. 启动 FastAPI lifespan，等待 lifecycle 从 `STARTING` 到 `READY`；所有 Parse/Semantic/Role failure 发生在首个 Application Resource 构造之前。
4. 资源 availability 由真实 Resource Constructor 验证（`RuntimeInitializationStack`）：Memory、Model engine、Journal、checkpoint stores 失败 fail fast 并逆序 rollback（`RUNTIME_INITIALIZATION_FAILED`）。VectorDB 是唯一 allowlisted optional degradation：Local/Test 默认可 degraded，PRODUCTION 默认 `LOCAL_AGENT_KB_REQUIRED=true` 使 KB 失败阻止 READY，显式 `false` 才允许 degraded startup。
5. 确认 `ApplicationRuntimeServices`、model/router、Journal、dispatcher、registry、factory、ChatService 与 shutdown coordinator 仅装配一次；三个 `BoundedBlockingExecutor` 统一使用 `LOCAL_AGENT_BLOCKING_MAX_WORKERS`/`LOCAL_AGENT_BLOCKING_MAX_PENDING_TASKS`。
6. 确认 admission 为 `ACCEPTING`，module compatibility handle 与 `app.state` identity 一致；`app.state.application_metadata` 已发布（environment profile/id、service version、per-process instance id）。
7. 执行 Offline Fake 或已批准的安全 smoke test；本地 RC smoke 不要求访问生产外部服务。
8. 检查 Journal 可追加/读取、Observability health、Trace health 与进程内 gauges。
9. 确认默认 Runtime 为 `COORDINATED`，请求只读取一次 mode。
10. 检索策略（`LOCAL_AGENT_RETRIEVAL_STRATEGY`）在 startup 捕获一次：`BASELINE` 保持既有 v1 collection 行为；`HYBRID_RRF` 在 Router 构造前执行完整 active-generation provenance 校验（active.json schema → locator containment → manifest/provenance digest → Dense v2 marker → embedding asset-tree digest → BM25 artifact digest/冻结契约 → 共享 provenance 精确相等）。WP1 边界内即使校验通过也以 `RETRIEVAL_STRATEGY_NOT_IMPLEMENTED` 安全失败（Hybrid query 接线在 WP2），不回退 baseline；校验失败按 `knowledge_base_required` 决定 fail/degraded。启动绝不自动 rebuild、不重建 BM25 artifact、不修改 active.json。

启动失败时禁止切换 Runtime 后重跑同一请求。配置异常保持 `SettingsValidationError` 固定安全码（`SETTINGS_PARSE_ERROR`/`SETTINGS_VALIDATION_ERROR`/`SETTINGS_SECURITY_POLICY_ERROR`/`STARTUP_CONFIGURATION_ERROR`），资源失败保持 `RUNTIME_INITIALIZATION_FAILED`；错误对象和日志不输出原始路径、密钥或 Provider URL。Legacy rollback 是修改 `CHAT_RUNTIME_MODE` 后重启并只影响新请求，不是某次失败后的动态动作。

## Health / Metrics / Trace

- Observability health：`status`、`dropped_records`、`logger_failures`、`metrics_failures`、`worker_failures`、`duplicate_records`、`record_failures`、`flush_failures`、`last_safe_error_code`。
- Trace health：`active_span_count`、`completed_span_count`、`dropped_span_count`、`start_failures`、`end_failures`、`flush_failures`、`status`、`last_safe_error_code`。
- Runtime gauges：`runtime_active_runs`、`runtime_active_steps`、`runtime_detached_tool_workers`、`runtime_detached_retrieval_workers`、`runtime_blocking_executor_active`、`runtime_blocking_executor_pending`、`runtime_event_channel_buffered`、`runtime_circuit_breakers_open`。
- Reservation/permit、pending disconnect watcher、request producer 与 channel ownership 目前主要由 owner snapshot/专项不变量测试读取，不虚构 Metric 名。
- 当前为进程内快照/记录能力，不等于已接 Prometheus/Grafana。高基数或敏感 label 禁止；Tool name 仅由配置 allowlist 放行。

## Health / Readiness

Health 只证明当前可达的 Server Application 尚未进入 terminal CLOSED / fatal unavailable；**不证明**可以接受新 Run、所有依赖健康、所有 endpoint 可用或 Model Circuit 正常。

Readiness 证明可以安全尝试接受一个新的 Run：`ApplicationRuntimeServices` 可用 + lifecycle `READY` + admission `ACCEPTING`。唯一 allowlisted startup degradation 是 `knowledge_base_required=false` + KB 初始化/import 失败（此时 readiness 仍 200，diagnostic status = `READY_DEGRADED`）。

```text
GET /health    # 200 表示 application 尚未 terminal CLOSED / fatal unavailable
GET /readyz    # 200 表示可以安全尝试接受新的 Run
```

状态矩阵（两个 endpoint 返回同一四字段 body，仅 HTTP 判定语义不同）：

| Source facts | Diagnostic `status` | `/health` | `/readyz` |
|---|---|---|---|
| services 尚未构造，fallback `STARTING` | `STARTING` | 200 | 503 |
| lifecycle=`READY`，admission=`ACCEPTING`，KB 正常 | `READY` | 200 | 200 |
| lifecycle=`READY`，admission=`ACCEPTING`，allowed KB degraded | `READY_DEGRADED` | 200 | 200 |
| lifecycle=`READY`，admission=`DRAINING` | `DRAINING` | 200 | 503 |
| lifecycle=`SHUTTING_DOWN`，admission=`DRAINING` | `DRAINING` | 200 | 503 |
| lifecycle=`CLOSED`，admission=`CLOSED` | `CLOSED` | 503 | 503 |
| 无法安全确认（inconsistent / unknown / 无 fallback） | `UNAVAILABLE` | 503 | 503 |

响应 body 固定四字段：`{"status": "...", "lifecycle": "...", "admission": "...", "degraded": false}`。body 不含 error / reason / error_code / path / URL / exception / version / environment / instance / 时间戳。

特别说明：

- **DRAINING 窗口**：`/health = 200` 且 `/readyz = 503`。application 仍足以完成有界关闭，但不再接受新 Run；不得把两者都变成 503。
- **STARTING / CLOSED 网络可观察窗口不保证**：矩阵定义的是 pure projection / 已接受请求语义，不承诺 lifespan startup window 或 shutdown 完成后一定能从网络请求到 `/health`。
- Endpoint 是只读投影，不修改 lifecycle / admission / dependency，不触发 recovery/retry。无法安全确认的事实 fail closed 为 `UNAVAILABLE` / 503，不使用 500。

## Shutdown Runbook

真实顺序：Lifecycle→`SHUTTING_DOWN`；Admission→`DRAINING`；等待 admission lease；请求取消 active Run；bounded run drain；force abort remaining；关闭 worker admission；worker drain；Observability flush；Trace flush；组件 close（含 Snapshot/Journal）；Model safety gate；remaining close；Admission/Lifecycle→`CLOSED`。

判断字段必须同时查看：`orchestration_completed`、`fully_closed`、`has_failures`、`has_deferred_resources`、`active_worker_count`、`detached_worker_count`、`unknown_worker_count`。`completed` 只是 `orchestration_completed` 兼容别名。Detached worker 不可清记录；Model deferred 时不能强关共享 client；同步 close 为 UNKNOWN 时不能自动 double close。正常完成后的再次 shutdown 返回同一缓存报告；中途取消后的安全重入由 `test_shutdown_cancellation_reentry.py` 锁定，不能假定所有组件重新关闭。

## Common Incident Runbooks

### 1. 请求卡住或超时

- 症状：请求超过预期、无新输出或返回 deadline/timeout。
- 权威事实源：RunContext deadline/cancellation、AgentState、BudgetLedger、Registry、worker/channel snapshots。
- 可能原因：deadline 到期、provider 已开始且阻塞、reservation/permit 等待、channel backpressure。
- 禁止操作：禁止清空 Registry/Worker record，禁止切 Legacy 重跑同一请求。
- 诊断步骤：检查 deadline→first-wins reason→provider_started→active/detached worker→reservation/permit→channel owner。
- 安全处置：协作式取消、bounded drain；保留 started/commit 事实。
- 恢复条件：owner 归零或 detached 状态已人工接管；新请求有新身份。
- 需要人工升级的条件：worker detached/unknown、非幂等副作用可能已提交。
- 相关错误码：`RETRIEVAL_TIMEOUT`、`TOOL_TIMEOUT`、`TOOL_DEADLINE_EXCEEDED`、`BUDGET_EXHAUSTED`。
- 相关测试：RC-11、RC-12、`test_run_coordinator.py::test_driver_deadline_error_keeps_deadline_stop_reason`。

### 2. Client Disconnect

- 症状：客户端断开，server 仍有短暂后台清理。
- 权威事实源：CancellationToken first-wins reason、disconnect watcher、producer/channel/worker snapshot。
- 可能原因：网络关闭、ASGI task cancellation、worker 未及时协作停止。
- 禁止操作：断开后继续写 Wire；删除后台 worker 记录。
- 诊断步骤：确认 `CLIENT_DISCONNECTED`/first-wins→transport 停止→drain/abort→后台 worker truth。
- 安全处置：cancel-and-drain，超时 force abort transport owner；不追加 SAFE_ERROR。
- 恢复条件：watcher/producer/channel owner 归零，worker truth 明确。
- 需要人工升级的条件：detached worker 或可能副作用未完成。
- 相关错误码：固定 cancellation reason；无额外业务重试码。
- 相关测试：RC-13、`test_client_disconnect.py::test_disconnect_watcher_stops_output_and_is_awaited`。

### 3. Journal 有记录但 Channel 无事件

- 症状：Journal append 成功，channel/transport 未观察到事件。
- 权威事实源：JournalRecord 与 EventPublicationEvidence。
- 可能原因：journal-first 后 enqueue/transport 失败。
- 禁止操作：删除 Journal、重用 sequence、重执行 Model/Tool、补造第二 terminal。
- 诊断步骤：校验 record digest/sequence→检查 publication evidence→检查 channel/transport health。
- 安全处置：标记 Partial Publication，保留 committed Journal 事实。
- 恢复条件：由新订阅/诊断流程读取安全事实；不更改原 Run。
- 需要人工升级的条件：Tool outcome 或 terminal 对外可见性影响业务流程。
- 相关错误码：`JOURNAL_APPEND_FAILED`、`TOOL_COMPLETION_PUBLICATION_FAILED`、`RETRIEVAL_EVENT_EMISSION_FAILED`。
- 相关测试：`test_event_journal_integration.py::test_channel_failure_does_not_repeat_committed_business_work`。

### 4. Snapshot Save 部分持久化

- 症状：evidence 为 `persisted=true`、`partially_persisted=true`、`retry_allowed=false`。
- 权威事实源：SnapshotPublicationEvidence 与 SnapshotStore 原记录。
- 可能原因：save 已提交后 after-save 阶段失败/取消。
- 禁止操作：自动重存、删除已提交 row、修改 digest。
- 诊断步骤：核对 snapshot id/digest→store read→publication flags→Journal watermark。
- 安全处置：保留原件并进入 validation/reconciliation。
- 恢复条件：只读验证给出安全 assessment。
- 需要人工升级的条件：digest/identity 冲突或存储状态不确定。
- 相关错误码：`SNAPSHOT_STORE_FAILED`、`SNAPSHOT_ID_CONFLICT`。
- 相关测试：`test_snapshot_partial_persistence.py::test_after_save_delay_cancellation_never_deletes_committed_snapshot`。

### 5. Recovery 读取失败或证据损坏

- 症状：`FAILED`、`UNSUPPORTED`、`CORRUPTED`、`INSUFFICIENT_EVIDENCE` 或 `REQUIRES_RECONCILIATION`。
- 权威事实源：原始 Snapshot + Journal。
- 可能原因：schema 不兼容、digest 损坏、tail gap/conflict、activity/evidence 不足。
- 禁止操作：把损坏 tail 当空 tail；升级写回；从 live Registry 回填。
- 诊断步骤：按 recovery runbook 校验 schema/digest/identity/watermark/tail。
- 安全处置：fail closed，保存原件与固定 assessment。
- 恢复条件：兼容 reader 可只读验证，或人工对账完成。
- 需要人工升级的条件：CORRUPTED、UNKNOWN side effect、schema unsupported。
- 相关错误码：`SNAPSHOT_CORRUPTED`、`JOURNAL_CORRUPTED`、`TOOL_EVIDENCE_INSUFFICIENT`。
- 相关测试：`test_recovery_validation.py`、`test_recovery_tail_corruption.py`。

### 6. Tool Started 无 Completed

- 症状：持久化 `TOOL_STARTED` 存在而 `TOOL_COMPLETED` 缺失。
- 权威事实源：仅 Snapshot + Journal；外部权威系统只用于人工对账。
- 可能原因：worker 中断、completion publication gap、进程退出。
- 禁止操作：调用 Tool、使用测试 fixture/live Registry 回填、自动补偿。
- 诊断步骤：执行 `runtime_recovery_runbook.md` 的八步人工对账。
- 安全处置：把 NOT_STARTED/COMMITTED/UNKNOWN 写入独立外部 Incident / Reconciliation Record，作为审批后新操作身份的输入；不写回 Journal/Snapshot/AgentState，不补造 `TOOL_COMPLETED`。
- 恢复条件：持久化 evidence 或外部权威确认足以支持人工决策。
- 需要人工升级的条件：非幂等、COMMITTED/UNKNOWN、补偿失败。
- 相关错误码：`TOOL_OUTCOME_UNKNOWN`、`TOOL_EVIDENCE_INSUFFICIENT`。
- 相关测试：RC-16、`test_recovery_tool_completion_gap.py`。

### 7. Observability / Trace degraded

- 症状：health=`DEGRADED`、drop/failure counter 增长。
- 权威事实源：Observability/Trace health；业务事实仍是 AgentState/Journal。
- 可能原因：queue full、projector/checkpoint/flush/span recorder 失败。
- 禁止操作：因日志/Trace 缺失重执行 Model/Tool。
- 诊断步骤：读取 fixed counters/last safe code→检查 pending/drop→检查 flush/close report。
- 安全处置：修复诊断组件；保持业务结果不变。
- 恢复条件：新诊断记录正常，health 趋势稳定；历史缺口不伪造。
- 需要人工升级的条件：持续 drop 导致审计不可接受。
- 相关错误码：`OBSERVABILITY_RECORD_FAILED`、`OBSERVABILITY_FLUSH_FAILED`、`TRACE_SPAN_END_FAILED`、`TRACE_FLUSH_FAILED`。
- 相关测试：`test_observability_integration.py`、`test_trace_integration.py`。

### 8. Detached Worker

- 症状：active/detached worker 非零，Model close deferred。
- 权威事实源：BlockingExecutor/Tool worker snapshot 与 ShutdownReport。
- 可能原因：同步 Python/C Extension 调用不能被协作取消。
- 禁止操作：强杀线程、删除记录伪造 idle、强关共享 Model client。
- 诊断步骤：读取 active/detached/unknown→核对 attempt/run digest→等待 bounded idle。
- 安全处置：保留记录、隔离新 admission、defer model close。
- 恢复条件：真实 worker completion callback 注销记录。
- 需要人工升级的条件：超过运维窗口仍未结束或副作用未知。
- 相关错误码：`RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER`。
- 相关测试：RC-18、`test_shutdown_report_truthfulness.py`。

### 9. Shutdown Partial Failure

- 症状：orchestration 已结束但 failure/deferred/remaining 非零。
- 权威事实源：完整 ShutdownReport。
- 可能原因：run/worker drain、flush、close 或 model safety gate 失败。
- 禁止操作：只检查 `completed`；对 UNKNOWN 资源自动 double close。
- 诊断步骤：查看 `orchestration_completed`、`fully_closed`、`has_failures`、`has_deferred_resources` 和 worker counts。
- 安全处置：按 component result 定点处理；保留 deferred truth。
- 恢复条件：所有 required close 操作完成且 remaining/worker 为零。
- 需要人工升级的条件：unknown worker/resource 或 model deferred。
- 相关错误码：`RUNTIME_RUN_DRAIN_TIMEOUT`、`RUNTIME_RUN_FORCE_ABORT_FAILED`、`RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER`。
- 相关测试：RC-17/18、`test_shutdown_cancellation_reentry.py`。

### 10. Runtime Configuration Error

- 症状：Settings load 或 lifespan startup fail closed。
- 权威事实源：`SettingsValidationError`（安全码 + env 名 + reason code）与初始化 stack 的固定组件结果。
- 可能原因：非法 bool/int/float、NaN/Inf、越界、未知 Environment/Model Profile/backend、cross-field 冲突、PRODUCTION 缺 environment_id 或 remote 非 HTTPS/verify_tls 关闭、SERVER role 缺 remote endpoint、路径/权限错误。
- 禁止操作：自动切 Legacy、输出密钥/URL/绝对路径/raw value、对同一请求重跑。
- 诊断步骤：对照 configuration reference 逐项离线校验；只记录字段名与安全分类。
- 安全处置：修正配置并重启，再执行新身份 smoke test。
- 恢复条件：lifespan READY、admission ACCEPTING、health 正常。
- 需要人工升级的条件：权限、模型加载或持久化损坏无法安全解决。
- 相关错误码：`SETTINGS_PARSE_ERROR`、`SETTINGS_VALIDATION_ERROR`、`SETTINGS_SECURITY_POLICY_ERROR`、`STARTUP_CONFIGURATION_ERROR`、`RUNTIME_INITIALIZATION_FAILED`；`RUNTIME_CONFIGURATION_ERROR` 只覆盖 ChatService/Coordinated factory 缺失的局部配置失败，不得扩大解释。
- 相关测试：`test_settings_validation.py`、`test_environment_profile.py`、`test_startup_configuration.py`、`test_runtime_lifespan.py`。

## Legacy Rollback Runbook

适合：Coordinated 默认入口存在启动/兼容问题、目标请求尚未开始、且已确认 Legacy 覆盖基础场景。不适合：非幂等副作用后重跑、需要 Snapshot/Recovery evidence、修复 Journal 损坏、绕过 Budget/Cancellation/安全策略。

流程：停止接收新请求→等待/处置现有 Run→修改真实 `CHAT_RUNTIME_MODE` 为 `LEGACY`→重启→请求前确认 mode→执行安全 smoke test。回滚后使用新 RunContext/AgentState；不能对已开始或失败的 Coordinated 请求动态切换，不能宣称 Legacy 拥有完整 Journal/Snapshot/Recovery。共享 Application resource 仍按 identity close once。恢复 Coordinated 时执行同样的请求前配置、重启与 smoke test。

## Persistence Preflight / Migration

### Startup automatic preflight（READ ONLY）

Server 每次 startup 在任何持久 Store constructor 之前自动执行只读 preflight：

```text
Settings Parse / Semantic Validation
→ SERVER_ROLE Validation
→ lifecycle STARTING
→ automatic SQLite persistence preflight（PRAGMA quick_check + physical shape + 版本事实；不创建/不修改 DB）
→ required Resource Construction → Chroma open + marker validation → 其余构造 → READY
```

- 任何 required Store 为 `MIGRATION_REQUIRED` / `UNSUPPORTED` / `FAILED` → startup fail，`never READY`；
  safe code：`PERSISTENCE_SCHEMA_UNSUPPORTED` / `PERSISTENCE_PREFLIGHT_FAILED`，由 `RUNTIME_INITIALIZATION_FAILED` 边界包装。
- Chroma marker mismatch → required KB 阻止 READY；显式 optional KB → `READY_DEGRADED`。Startup 绝不自动 clear/rebuild/migrate。
- `/health`、`/readyz` 保持只读投影，不触发 preflight/migration/repair/restore/rebuild。

### Explicit migration（SCRIPT_ROLE，Server stopped）

```powershell
uv run python scripts/manage_persistence.py preflight
uv run python scripts/manage_persistence.py migrate --backup-confirmed
```

- `preflight`：只读全 Store 检测，输出 `NEW / CURRENT / MIGRATION_REQUIRED / REBUILD_REQUIRED / UNSUPPORTED / FAILED`；非全部 `NEW/CURRENT` 返回 non-zero。
- `migrate`：先全 Store preflight；任何 UNSUPPORTED/FAILED 或已有数据需要 mutation 时缺少 `--backup-confirmed` → non-zero 且零 mutation。
- 每 Store 独立单事务：Memory additive migration（v2→v3 新增 Episodic partial indexes；v1/legacy 升至 current）+ `user_version=3` 同事务原子提交；Journal 只加 nullable span 列绝不 rewrite 历史 row；Checkpoint 只 drop/recreate derived table（历史 offset 丢弃，不影响业务 Authority）。
- 部分 Store commit 后后续失败 → overall FAIL、partial committed facts 如实报告；rerun 从实际 facts 继续（idempotent / safely re-runnable，不宣称 exactly-once）。
- Migration 是 forward-only：提交后 old binary compatibility NOT ASSUMED；无 downgrade migration。

### Upgrade Flow（Operator）

```text
1. graceful stop Server（确认进程退出；force-kill 不算可信前置）
2. 复制 MUST_BACKUP set 到同一 backup epoch（Memory/Journal/Snapshot if enabled 的 .db + 任何 -wal；KB source；known-good config reference）
3. 对备份副本执行显式 full preflight（只有 PASS 才允许 migrate）
4. deploy code/artifacts/config
5. 执行 preflight
6. 如需要：migrate --backup-confirmed
7. 如 Chroma marker mismatch：bootstrap_local_kb.py --rebuild（Server stopped、source 可用、embedding 可用）
8. 再次 preflight
9. 启动 Server → /health + /readyz + 功能 smoke
```

## Backup Runbook（manual stopped-server）

```text
计划升级 → graceful stop Server → 确认 process exited / shutdown truth 可接受
→ 复制 MUST_BACKUP set 到同一 backup epoch
  （Memory DB、Journal DB、Snapshot DB if enabled/存在、KB source、known-good config reference；
   每个 SQLite unit = 主 .db + 任何存在的 -wal；-shm 不要求）
→ 记录对应 known-good code/config/artifact identity（不记录 secret 明文）
→ 对备份副本执行显式 full preflight → 只有 PASS 才允许 migrate
```

```text
live raw copy               = unsupported（Server 运行中复制 .db 是非一致快照）
automatic backup            = NOT_IMPLEMENTED
automatic/scheduled/cloud   = NOT_IMPLEMENTED
```

## Restore Runbook（manual stopped-server）

```text
1. stop Server 并确认进程退出；Client 不得打开 Store
2. 把当前失败/待调查数据整体移动到隔离位置（禁止直接覆盖后丢失证据）
3. restore target 为空或已完成整组替换（禁止 SQLite/Chroma 目录内混合覆盖）
4. 从同一 backup epoch 恢复 Memory / Journal / Snapshot（if enabled）/ KB source
5. Chroma：整体恢复并验证 marker，否则隔离现有 Chroma，用匹配 embedding artifact 从 source 显式 rebuild
6. Checkpoint 默认 recreate；即使恢复旧 checkpoint 也必须通过 exact-shape preflight
7. 显式 full preflight（Server 启动前；不兼容则不启动）
8. 启动 known-compatible code/config/artifact → /health + /readyz + Memory/Journal/KB 功能 smoke
```

`files copied != restore validated`。任一步失败都停止；不对备份原件执行修复/迁移。Restore success 至少要求：显式 full preflight PASS、Server `READY`（或 allowlisted `READY_DEGRADED`）、required durable Stores 可读、health/readiness smoke PASS；若 KB 为 required，Chroma 不得以 degraded 代替 restore 成功。

## Rollback Runbook（manual）

```text
Upgrade before migration：take + validate backup
Upgrade fails before any data migration commit：只回滚 code/artifact/config（数据未变，可安全保留）
Any schema migration committed：old binary compatibility NOT ASSUMED
  → stop Server
  → 保留当前 migrated data
  → 恢复 matching pre-migration MUST_BACKUP set
  → 恢复 known-good code/config/artifacts
  → preflight → start → health/readiness + functional smoke
```

```text
binary-only rollback after schema migration = UNSAFE / NOT ASSUMED
downgrade migration                         = NOT_IMPLEMENTED
automatic deployment rollback               = NOT_IMPLEMENTED
```

## KB Generation Build / Rollback Runbook（WP1）

```text
生产 generation 构建（默认 --build-purpose=production）：
  uv run python scripts/bootstrap_local_kb.py
  使用 Settings chunk policy（LOCAL_AGENT_KB_CHUNK_SIZE/OVERLAP）；
  production 模式禁止 --chunk-size/--chunk-overlap（parser error）。

开发 generation（不写 active.json，manifest 标记 purpose=development）：
  uv run python scripts/bootstrap_local_kb.py --build-purpose=development --chunk-size 800 --chunk-overlap 100

布局：
  <LOCAL_AGENT_CHROMA_DIR>/localagent_retrieval/<collection_key>/
    active.json
    generations/<generation_id>/
      retrieval_index_manifest.json
      bm25_index.json
      artifact_metadata.json

发布契约：
  - 每个 generation 使用独立物理 Dense collection：la_{collection_key}_g_{uuidhex}
  - active.json 发布为原子替换（write temp → flush → fsync → os.replace）
  - 失败 build 保持旧 active.json 不变；部分构建的 generation 不可达，可 operator 清理
  - 启动绝不自动 rebuild / 重建 BM25 artifact / 修改 active.json
```

回滚（显式操作，无自动切换）：

```text
HYBRID_RRF 回滚到 BASELINE：
  stop Server → LOCAL_AGENT_RETRIEVAL_STRATEGY=BASELINE → start（v1/v2 collection 均可 baseline 使用）

切换/回退到另一个 generation：
  operator 用同一协议写一份新的已校验 active descriptor（指向既有 validated generation）
  → 旧 generation 保持可读，可手动清理（out of scope for WP1）
```

`HYBRID_RRF` 在 WP1 边界内即使 provenance 校验通过也以 `RETRIEVAL_STRATEGY_NOT_IMPLEMENTED` 安全失败；Hybrid query 接线在 WP2。v1 collection 对 Hybrid 是 `REBUILD_REQUIRED`（显式 rebuild，不自动迁移）。

## Migration vs Recovery

```text
Deployment Migration != Runtime Recovery Validation
```

Migration 处理 deployment upgrade 中的 Store schema/compatibility（显式 SCRIPT_ROLE，Server stopped）。Runtime Recovery 仍为 validation-only：`RecoveryValidator` 只读 Snapshot + Journal 返回 immutable assessment；不写 AgentState、不启动 replay/resume、不回填历史事实。不得把备份/恢复说成 Runtime Recovery。
