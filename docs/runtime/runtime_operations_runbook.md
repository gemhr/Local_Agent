# Runtime Operations Runbook

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

启动失败时禁止切换 Runtime 后重跑同一请求。配置异常保持 `SettingsValidationError` 固定安全码（`SETTINGS_PARSE_ERROR`/`SETTINGS_VALIDATION_ERROR`/`SETTINGS_SECURITY_POLICY_ERROR`/`STARTUP_CONFIGURATION_ERROR`），资源失败保持 `RUNTIME_INITIALIZATION_FAILED`；错误对象和日志不输出原始路径、密钥或 Provider URL。Legacy rollback 是修改 `CHAT_RUNTIME_MODE` 后重启并只影响新请求，不是某次失败后的动态动作。

## Health / Metrics / Trace

- Observability health：`status`、`dropped_records`、`logger_failures`、`metrics_failures`、`worker_failures`、`duplicate_records`、`record_failures`、`flush_failures`、`last_safe_error_code`。
- Trace health：`active_span_count`、`completed_span_count`、`dropped_span_count`、`start_failures`、`end_failures`、`flush_failures`、`status`、`last_safe_error_code`。
- Runtime gauges：`runtime_active_runs`、`runtime_active_steps`、`runtime_detached_tool_workers`、`runtime_detached_retrieval_workers`、`runtime_blocking_executor_active`、`runtime_blocking_executor_pending`、`runtime_event_channel_buffered`、`runtime_circuit_breakers_open`。
- Reservation/permit、pending disconnect watcher、request producer 与 channel ownership 目前主要由 owner snapshot/专项不变量测试读取，不虚构 Metric 名。
- 当前为进程内快照/记录能力，不等于已接 Prometheus/Grafana。高基数或敏感 label 禁止；Tool name 仅由配置 allowlist 放行。

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
