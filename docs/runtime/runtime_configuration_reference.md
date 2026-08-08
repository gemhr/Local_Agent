# Runtime Configuration Reference

## Configuration Source

唯一项目级来源是 `core.settings.Settings.load()`。仓库根目录提供 `.env.example` 作为配置名称/模板文档，**Application 不自动加载该文件**（不存在 dotenv loader）；operator 可将其内容复制为 PowerShell 环境变量模板。环境变量在 `server.py`/`main.py` import/进程启动时读取，运行中的请求不动态重载，因此下表均为 `restart_required=yes`。

配置解析统一严格：显式 bool 只接受大小写无关的 `1/0/true/false`；显式 int/float 严格词法解析并要求有限值；显式 enum/profile/backend 未知或空值直接失败。数值字段在 Settings Semantic Validation 中按真实 consumer contract 校验 range（timeout/capacity/窗口/计数类 ≥1，cost 类 ≥0，GPU layers ≥-1（`-1`=全部层 offload、`0`=CPU、正整数=指定 offload 层数），port 1..65535）。非法显式值不再静默变 False、clamp 或回落到默认，全部 fail closed；只有缺失 env 才应用默认值。`SettingsValidationError` 是唯一 Settings 级异常类型（`ValueError` 子类），只保存安全码、env 名与 reason code。

配置 precedence（只由 `Settings.load()` 执行一次）：

```text
code safe default < environment profile default < model resource preset < explicit environment variable < derived value
```

Environment Profile 只管理少量字段的默认值；Model Profile 只管理 fast/balanced/deep 的资源字段；两套字段集合不重叠。显式 env 始终最高优先，但不能绕过 Production 安全不变量（如 Production remote 必须 HTTPS 且 TLS verification=True）。

下表的路径示例均为仓库相对占位值，远端地址和密钥故意不提供真实示例。

| name | owner | type | default | allowed_values | required | scope | restart_required | security_classification | failure_behavior | example_safe_value |
|---|---|---|---|---|---|---|---|---|---|---|
| `LOCAL_AGENT_ENVIRONMENT_PROFILE` | EnvironmentProfile | enum | `LOCAL` | `LOCAL`,`TEST`,`PRODUCTION` | no | APPLICATION_SCOPE | yes | public-safe enum | unknown/blank 显式值 fail closed | `LOCAL` |
| `LOCAL_AGENT_ENVIRONMENT_ID` | Settings metadata | string | profile-derived：LOCAL=`local`、TEST=`test`、PRODUCTION=无（必填） | 安全低基数 identifier `^[a-z0-9][a-z0-9._-]{0,63}$` | PRODUCTION | APPLICATION_SCOPE | yes | security identifier | 非法/缺失 identifier fail closed | `prod-region-1` |
| `LOCAL_AGENT_API_HOST` | Settings/server | string | `127.0.0.1` | valid bind host | no | APPLICATION_SCOPE | yes | internal config | bind/start failure | `127.0.0.1` |
| `LOCAL_AGENT_API_PORT` | Settings/server | int | `8000` | integer port 1..65535 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed；OS bind 失败 | `8000` |
| `LOCAL_AGENT_API_BASE_URL` | Settings/client config | string | derived host/port | valid client base URL | no | APPLICATION_SCOPE | yes | internal endpoint | client connection failure | `http://127.0.0.1:8000` |
| `CHAT_RUNTIME_MODE` | ChatRuntimeSelector | enum | `COORDINATED` | `COORDINATED`,`LEGACY` | no | APPLICATION_SCOPE/request snapshot | yes | public-safe enum | unsupported value fails load | `COORDINATED` |
| `LOCAL_AGENT_MODEL_PROFILE` | Settings presets | enum | `balanced` | `fast`,`balanced`,`deep` | no | APPLICATION_SCOPE | yes | public-safe enum | unknown/blank 显式值 fail closed | `balanced` |
| `LOCAL_AGENT_LLM_BACKEND` | lifespan model assembly | enum | `remote` | `local`,`remote`,`hybrid` | yes | APPLICATION_SCOPE | yes | internal config | invalid/empty backend fails load；SERVER role 缺 endpoint 时 startup fail | `local` |
| `LOCAL_AGENT_MODEL_PATH` | LocalLLMEngine | path | project-relative GGUF | readable GGUF path | local/hybrid | APPLICATION_SCOPE | yes | sensitive path | model load fails startup | `data/models/model.gguf` |
| `LOCAL_AGENT_MODEL_THREADS` | LocalLLMEngine | int | profile-derived | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed；engine load failure | `8` |
| `LOCAL_AGENT_MODEL_CONTEXT` | LocalLLMEngine/profile | int | profile-derived | positive practical window | no | APPLICATION_SCOPE | yes | internal config | selection/inference failure | `4096` |
| `LOCAL_AGENT_MODEL_GPU_LAYERS` | LocalLLMEngine | int | `0` | integer ≥-1（`-1`=全部层 offload、`0`=CPU、正数=指定 offload 层数） | no | APPLICATION_SCOPE | yes | internal config | 小于 `-1` 的显式值 fail closed；model load failure | `0` |
| `LOCAL_AGENT_MODEL_MAX_TOKENS` | model profiles | int | profile-derived | positive integer | no | APPLICATION_SCOPE | yes | internal config | invalid capacity/inference failure | `1024` |
| `LOCAL_AGENT_REMOTE_MODEL_NAME` | RemoteLLMEngine | string | `Qwen3.5-27B` | provider model identifier | remote/hybrid | APPLICATION_SCOPE | yes | sensitive provider config | provider request failure | `example-model` |
| `LOCAL_AGENT_REMOTE_PROVIDER_KIND` | RemoteLLMEngine | string | `openai_compatible` | implemented provider kinds | no | APPLICATION_SCOPE | yes | internal config | incompatible payload behavior | `openai_compatible` |
| `LOCAL_AGENT_REMOTE_API_BASE_URL` | RemoteLLMEngine | string | empty | valid configured endpoint | remote/hybrid（SERVER role） | APPLICATION_SCOPE | yes | secret/internal endpoint | SERVER role 缺 endpoint fail closed；PRODUCTION 必须 HTTPS | `<configured-outside-docs>` |
| `LOCAL_AGENT_REMOTE_API_KEY` | RemoteLLMEngine | string | empty | provider credential | provider-dependent | APPLICATION_SCOPE | yes | secret | authentication/provider failure | `<secret-store-reference>` |
| `LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS` | HTTP transport | int seconds | `120` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值在 Settings 期 fail closed（不进入 requests） | `120` |
| `LOCAL_AGENT_REMOTE_VERIFY_TLS` | HTTP transport | strict bool | profile-derived：LOCAL=`0`、TEST=`1`、PRODUCTION=`1` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | security critical | 非法显式值 fail closed；PRODUCTION 显式关闭为 security policy failure | `1` |
| `LOCAL_AGENT_REMOTE_TRUST_ENV` | HTTP transport | strict bool | profile-derived：LOCAL=`1`、TEST=`0`、PRODUCTION=`0` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | security critical | 非法显式值 fail closed；决定 Server → Remote LLM Session 是否继承系统 proxy | `0` |
| `LOCAL_AGENT_CLIENT_TRUST_ENV` | HTTP transport | strict bool | `1`（所有 Profile 一致） | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | security critical | 非法显式值 fail closed；决定 Desktop Client → LocalAgent Server Session 是否继承系统 proxy；与 `LOCAL_AGENT_REMOTE_TRUST_ENV` 完全独立 | `1` |
| `LOCAL_AGENT_REMOTE_ENABLE_THINKING` | HTTP payload | strict bool | `0` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | internal config | 非法显式值 fail closed | `0` |
| `LOCAL_AGENT_REMOTE_CONTEXT_WINDOW` | model profile | int | `32768` | positive practical window | no | APPLICATION_SCOPE | yes | internal config | routing/capacity mismatch | `32768` |
| `LOCAL_AGENT_LOCAL_FIXED_CALL_COST_UNITS` | ModelCostProfile | int | `1` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | 越界显式值 fail closed | `1` |
| `LOCAL_AGENT_LOCAL_INPUT_COST_UNITS_PER_1K_TOKENS` | ModelCostProfile | int | `1` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | 越界显式值 fail closed | `1` |
| `LOCAL_AGENT_LOCAL_OUTPUT_COST_UNITS_PER_1K_TOKENS` | ModelCostProfile | int | `1` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | 越界显式值 fail closed | `1` |
| `LOCAL_AGENT_LOCAL_ESTIMATED_LATENCY_MS` | ModelCostProfile | int | `1000` | integer ≥1 | no | APPLICATION_SCOPE | yes | public-safe count | 越界显式值 fail closed | `1000` |
| `LOCAL_AGENT_REMOTE_FIXED_CALL_COST_UNITS` | ModelCostProfile | int | `10` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | 越界显式值 fail closed | `10` |
| `LOCAL_AGENT_REMOTE_INPUT_COST_UNITS_PER_1K_TOKENS` | ModelCostProfile | int | `2` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | 越界显式值 fail closed | `2` |
| `LOCAL_AGENT_REMOTE_OUTPUT_COST_UNITS_PER_1K_TOKENS` | ModelCostProfile | int | `4` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | 越界显式值 fail closed | `4` |
| `LOCAL_AGENT_REMOTE_ESTIMATED_LATENCY_MS` | ModelCostProfile | int | `3000` | integer ≥1 | no | APPLICATION_SCOPE | yes | public-safe count | 越界显式值 fail closed | `3000` |
| `LOCAL_AGENT_MODEL_BREAKER_FAILURE_THRESHOLD` | circuit registry | int | `3` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `3` |
| `LOCAL_AGENT_MODEL_BREAKER_RECOVERY_TIMEOUT_SECONDS` | circuit registry | int | `30` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `30` |
| `LOCAL_AGENT_MODEL_BREAKER_HALF_OPEN_MAX_CALLS` | circuit registry | int | `1` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `1` |
| `LOCAL_AGENT_MODEL_BREAKER_COUNT_RATE_LIMITED` | circuit registry | strict bool | `1` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | internal config | 非法显式值 fail closed | `1` |
| `LOCAL_AGENT_MEMORY_DB_PATH` | MemoryManager | path | project database path | writable SQLite path | no | APPLICATION_SCOPE | yes | sensitive path/data | initialization/I/O failure | `data/database/memory.db` |
| `LOCAL_AGENT_CHROMA_DIR` | VectorDBManager | path | project chroma dir | readable/writable directory | no | APPLICATION_SCOPE | yes | sensitive path/data | KB degrades with safe code；PRODUCTION 默认 required | `data/vector_store` |
| `LOCAL_AGENT_EMBEDDING_MODEL_PATH` | VectorDBManager | path | project model dir | readable model directory | no | APPLICATION_SCOPE | yes | sensitive path | KB degrades with safe code | `data/models/embedding` |
| `LOCAL_AGENT_EMBEDDING_QUERY_PROMPT_NAME` | embedding adapter | string | empty | backend-supported name | no | APPLICATION_SCOPE | yes | internal config | adapter behavior/failure | `query` |
| `LOCAL_AGENT_EMBEDDING_BATCH_SIZE` | embedding adapter | int | `8` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `8` |
| `LOCAL_AGENT_EVENT_JOURNAL_DB_PATH` | SQLiteRunEventJournal | path | project database path | writable SQLite path | no | APPLICATION_SCOPE | yes | sensitive path/data | startup or append fails closed | `data/database/runtime_journal.db` |
| `LOCAL_AGENT_SNAPSHOT_ENABLED` | snapshot assembly | strict bool | `false` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | public-safe flag | typo fails Settings load | `false` |
| `LOCAL_AGENT_SNAPSHOT_DB_PATH` | SQLiteSnapshotStore | path | project database path | writable SQLite path | when enabled | APPLICATION_SCOPE | yes | sensitive path/data | startup/read/write fails closed | `data/database/runtime_snapshot.db` |
| `LOCAL_AGENT_OBSERVABILITY_CHECKPOINT_DB_PATH` | checkpoint stores | path | project database path | writable SQLite path | no | APPLICATION_SCOPE | yes | sensitive path/data | observability degraded/start failure | `data/database/runtime_observability.db` |
| `LOCAL_AGENT_OBSERVABILITY_QUEUE_CAPACITY` | dispatcher | int | `256` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed；overflow drops/rejects diagnostically | `256` |
| `LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS` | Settings | int | `5` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | **DEPRECATED**；严格解析但无行为接线，显式配置产生安全 warning；replacement 为 `RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS` | `5` |
| `RUNTIME_DISCONNECT_GRACE_SECONDS` | HTTP disconnect cleanup | finite float | `0.75` | ≥0 | no | APPLICATION_SCOPE | yes | internal config | invalid value fails load | `0.75` |
| `RUNTIME_SHUTDOWN_GRACE_SECONDS` | GracefulShutdownCoordinator | finite float | `5.0` | ≥0 | no | APPLICATION_SCOPE | yes | internal config | invalid value fails load | `5.0` |
| `RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS` | shutdown/component close | finite float | `5.0` | ≥0 | no | APPLICATION_SCOPE | yes | internal config | invalid value fails load | `5.0` |
| `LOCAL_AGENT_METRICS_TOOL_NAME_ALLOWLIST` | MetricLabelPolicy | CSV string | empty | approved low-cardinality tool names | no | APPLICATION_SCOPE | yes | security allowlist | names outside allowlist not labeled | `calculator` |
| `LOCAL_AGENT_HISTORY_WINDOW_SIZE` | MemoryManager/router | int | profile-derived | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `12` |
| `LOCAL_AGENT_SUMMARY_TRIGGER_MESSAGES` | MemoryManager/router | int | profile-derived | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `20` |
| `LOCAL_AGENT_SUMMARY_KEEP_RECENT` | MemoryManager/router | int | profile-derived | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `12` |
| `LOCAL_AGENT_SUMMARY_MAX_CHARS` | MemoryManager/router | int | profile-derived | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `1600` |
| `LOCAL_AGENT_RAG_TOP_K` | retrieval | int | profile-derived | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `3` |
| `LOCAL_AGENT_RAG_MIN_SCORE` | retrieval | float | `0.55` | 0..1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `0.55` |
| `LOCAL_AGENT_RAG_DOC_MAX_CHARS` | retrieval | int | profile-derived | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `1000` |
| `LOCAL_AGENT_RAG_CONTEXT_MAX_CHARS` | retrieval | int | profile-derived | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `2400` |
| `LOCAL_AGENT_ORCHESTRATION_ENABLED` | AgentRouter | strict bool | `1` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | public-safe flag | 非法显式值 fail closed | `1` |
| `LOCAL_AGENT_ORCHESTRATION_MAX_AGENTS` | AgentRouter | int | `3` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `3` |
| `LOCAL_AGENT_SYNC_ENABLED` | sync feature | strict bool | `0` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | internal config | 非法显式值 fail closed | `0` |
| `LOCAL_AGENT_WIKI_COOKIE` | sync client | string | empty | credential | feature-dependent | APPLICATION_SCOPE | yes | secret | authentication/sync failure | `<secret-store-reference>` |
| `LOCAL_AGENT_LOCAL_KB_DIR` | KB scripts/router | path | project KB dir | readable directory | no | APPLICATION_SCOPE | yes | sensitive path/data | ingestion/retrieval failure | `data/knowledge_base` |
| `LOCAL_AGENT_KB_COLLECTION` | VectorDBManager | string | `huawei_wiki_collection` | safe collection name | no | APPLICATION_SCOPE | yes | internal identifier | backend behavior/failure | `knowledge_collection` |
| `LOCAL_AGENT_KB_REQUIRED` | startup policy | strict bool | profile-derived：LOCAL=`0`、TEST=`0`、PRODUCTION=`1` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | public-safe flag | PRODUCTION 默认 KB 失败阻止 startup；显式 `false` 才允许 degraded | `1` |
| `LOCAL_AGENT_BLOCKING_MAX_WORKERS` | BoundedBlockingExecutor | int | `4` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed；三个 lifespan executor 统一使用 | `4` |
| `LOCAL_AGENT_BLOCKING_MAX_PENDING_TASKS` | BoundedBlockingExecutor | int | `8` | integer ≥0 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `8` |
| `LOCAL_AGENT_EVENT_CHANNEL_CAPACITY` | RuntimeEventChannel（经 Factory） | int | `32` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `32` |
| `LOCAL_AGENT_PLANNING_TIMEOUT_SECONDS` | RunCoordinator planning | finite float | `15.0` | finite >0 | no | APPLICATION_SCOPE | yes | internal config | 非正数/NaN/Inf 显式值 fail closed | `15.0` |
| `LOCAL_AGENT_STEP_RESULT_PER_RESULT_CHARS` | StepResultStore | int | `20000` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `20000` |
| `LOCAL_AGENT_STEP_RESULT_RUN_TOTAL_CHARS` | StepResultStore | int | `60000` | integer ≥1 且 ≥ per-result | no | APPLICATION_SCOPE | yes | internal config | 越界或小于 per-result 显式值 fail closed | `60000` |
| `LOCAL_AGENT_STEP_RESULT_MAX_ENTRIES` | StepResultStore | int | `16` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `16` |

## Environment Profile

`LOCAL_AGENT_ENVIRONMENT_PROFILE` 是部署环境 Profile，与 `LOCAL_AGENT_MODEL_PROFILE`（模型资源预设）完全独立。只管理下列字段默认值：

| setting | LOCAL | TEST | PRODUCTION |
|---|---:|---:|---:|
| `LOCAL_AGENT_REMOTE_VERIFY_TLS` | `0` | `1` | `1`（不可显式关闭） |
| `LOCAL_AGENT_REMOTE_TRUST_ENV` | `1` | `0` | `0`（可显式 `true`） |
| `LOCAL_AGENT_CLIENT_TRUST_ENV` | `1` | `1` | `1`（可显式 `0`/`false`） |
| `LOCAL_AGENT_KB_REQUIRED` | `0` | `0` | `1` |
| `LOCAL_AGENT_ENVIRONMENT_ID` | `local` | `test` | 无默认，必须显式提供 |

Production 安全不变量（SERVER role）：backend 为 remote/hybrid 时 endpoint 必须 HTTPS 且 `remote_verify_tls=True`，否则 `SETTINGS_SECURITY_POLICY_ERROR` 并在任何资源构造前阻止启动。显式 env 不能绕过该不变量。解析严格性与 Profile 无关：相同显式文本在三个 Profile 中要么得到相同 typed value，要么得到相同解析失败。

## Runtime Selection

默认 `COORDINATED`。`LEGACY` 只能在新请求开始前通过 `CHAT_RUNTIME_MODE` 显式选择并重启；endpoint 对每个请求只捕获一次 mode。运行中不动态切换，任何已选路径失败都不会跨 Runtime fallback。

## Role Boundary

server/client/script 各自进程启动时 `Settings.load()` 一次，不实现 reload，也不引入配置中心或配置文件。role validation 只校验本进程消费的必填字段：

- SERVER_ONLY：Runtime/model/memory/journal/snapshot/observability/server networking、remote endpoint/TLS 必填。
- CLIENT_ONLY：`api_base_url`、`sync_enabled`、`wiki_cookie`、client sync 目录。
- SHARED：Environment Profile schema、`environment_id`、`service_version`、KB collection identifier。

Server 不因缺少 client cookie 失败；Client 不因缺少 remote model endpoint 失败。两进程 shared metadata 在无 handshake 时明确保持 UNKNOWN/未验证。

## Model Configuration

Model routing/retry/fallback 属于 Runtime policy；HTTP transport、本地模型加载和共享 Session 并发属于 adapter/application resource。Remote 引擎将 requests/urllib3 自动 retry 显式设为 0，由 RetryExecutor 统一拥有重试。`LOCAL_AGENT_REMOTE_TRUST_ENV` 显式控制 `requests.Session.trust_env`：为 True 时继承进程系统 proxy（operator 显式选择，不记录 proxy URL/credential）；Test/Production 默认 False 不继承宿主 proxy。

### Client HTTP Proxy Governance

`LOCAL_AGENT_CLIENT_TRUST_ENV` 显式控制 **Desktop Client → LocalAgent Server 的所有 Client HTTP Session** 的 `requests.Session.trust_env`，覆盖聊天（`/api/chat`）、历史分页（`/api/history`）、搜索（`/api/search`）、取消（`/api/runtime/runs/{run_id}/cancel`）与记忆管理（`/api/memory`）五类传输。它属于 Client process 的 Application Scope 配置，由 `main.py` 在进程启动时通过唯一一次 `Settings.load()` 快照消费；消费链为 `Settings → main.py startup snapshot → ChatPanel plumbing → MemoryManagerDialog → Session.trust_env`，全部 Session 显式使用已解析值，不重新读 env。默认 `True` 保持 requests 既有行为；与 `LOCAL_AGENT_REMOTE_TRUST_ENV` 完全独立，修改其中一个不得改变另一个（两个 transport scope：Server → Remote LLM 与 Desktop Client → LocalAgent Server）。

默认 Coordinated factory 的 Parallel policy 为 `max_concurrency=2`（当前 typed multi-step 真实全局并发上限；`ParallelExecutor` 构造默认值 `1` 在生产调用链中被 policy 覆盖，属于 WP2 命名清理范围）。Blocking executor 容量由 `LOCAL_AGENT_BLOCKING_MAX_WORKERS`/`LOCAL_AGENT_BLOCKING_MAX_PENDING_TASKS` 配置，三个 lifespan executor 统一使用同一 application 默认值。

## Journal / Snapshot

Journal 在生产 lifespan 中固定装配 `SQLiteRunEventJournal`，schema v2、reader v1/v2，append-only 且损坏 fail closed；不得手工编辑 SQLite 记录。Snapshot 默认关闭，显式 opt-in 后装配 `SQLiteSnapshotStore`，schema v1 且 digest 严格校验；损坏、未知版本或部分持久化均 fail closed，不自动重存、不自动恢复。

## Observability / Trace

Observability 使用有界进程内队列、两个 SQLite consumer checkpoint store 和 best-effort projector；Health 为 `HEALTHY/DEGRADED`，记录 dropped/logger/metrics/worker/duplicate/record/flush failures 与 last safe code。Trace 当前是进程内 `InMemorySpanRecorder`，记录 active/completed/dropped 与 start/end/flush failures。两者故障不改变业务权威结果。禁止 Prompt、run id、路径、原始 Tool 名等高基数或敏感 label；Tool label 仅由 allowlist 开放。当前不等于已接 Prometheus/Grafana。

## Shutdown

Run drain 使用 `RUNTIME_SHUTDOWN_GRACE_SECONDS`，单组件关闭/worker drain 使用 `RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS`。存在 active/detached/unknown worker 时 Model close deferred，报告必须查看 `fully_closed`；`completed` 仅为 orchestration completion 兼容别名。Shutdown 同一 coordinator 成功完成后重入返回缓存报告；取消中的重入语义由专项测试覆盖，未知同步 close 状态不自动 double close。

## Deprecated Configuration

- `LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS`：DEPRECATED。保留字段与 env 一个 Stage 3 兼容周期，仍严格解析；显式配置产生一次安全 deprecation warning（只含 env 名），不改变行为。Replacement：`RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS`。
- `ChatService.event_channel_capacity`：DEPRECATED ignored constructor shim。真实 per-run channel capacity 的 Owner 是 `LOCAL_AGENT_EVENT_CHANNEL_CAPACITY` → `CoordinatedRuntimeFactory` → `RuntimeEventChannel`；该参数保留以兼容调用方，但不消费、不得接线成第二 Owner。

## Fault Injection

生产配置入口：无。默认 `controller=None`。测试只能显式构造 test Scope/Controller；不存在 Settings、环境变量、HTTP、Prompt 或 Tool 参数激活方式。
