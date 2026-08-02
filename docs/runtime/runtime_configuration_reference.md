# Runtime Configuration Reference

## Configuration Source

唯一项目级来源是 `core.settings.Settings.load()`；仓库当前没有独立 `.env.example`。环境变量在 `server.py` import/进程启动时读取，运行中的请求不动态重载，因此下表均为 `restart_required=yes`。整数转换失败、严格布尔值非法、Runtime mode 不支持或 remote 必填项缺失时均 fail closed；不会自动切换 Runtime。

下表的路径示例均为仓库相对占位值，远端地址和密钥故意不提供真实示例。

| name | owner | type | default | allowed_values | required | scope | restart_required | security_classification | failure_behavior | example_safe_value |
|---|---|---|---|---|---|---|---|---|---|---|
| `LOCAL_AGENT_API_HOST` | Settings/server | string | `127.0.0.1` | valid bind host | no | APPLICATION_SCOPE | yes | internal config | bind/start failure | `127.0.0.1` |
| `LOCAL_AGENT_API_PORT` | Settings/server | int | `8000` | integer port | no | APPLICATION_SCOPE | yes | internal config | parse/bind failure | `8000` |
| `LOCAL_AGENT_API_BASE_URL` | Settings/client config | string | derived host/port | valid client base URL | no | APPLICATION_SCOPE | yes | internal endpoint | client connection failure | `http://127.0.0.1:8000` |
| `CHAT_RUNTIME_MODE` | ChatRuntimeSelector | enum | `COORDINATED` | `COORDINATED`,`LEGACY` | no | APPLICATION_SCOPE/request snapshot | yes | public-safe enum | unsupported value fails load | `COORDINATED` |
| `LOCAL_AGENT_MODEL_PROFILE` | Settings presets | enum | `balanced` | `fast`,`balanced`,`deep`; other→balanced | no | APPLICATION_SCOPE | yes | public-safe enum | unknown uses balanced | `balanced` |
| `LOCAL_AGENT_LLM_BACKEND` | lifespan model assembly | enum | `remote` | `local`,`remote`,`hybrid` | yes | APPLICATION_SCOPE | yes | internal config | invalid/empty engine set fails startup | `local` |
| `LOCAL_AGENT_MODEL_PATH` | LocalLLMEngine | path | project-relative GGUF | readable GGUF path | local/hybrid | APPLICATION_SCOPE | yes | sensitive path | model load fails startup | `data/models/model.gguf` |
| `LOCAL_AGENT_MODEL_THREADS` | LocalLLMEngine | int | profile-derived | integer | no | APPLICATION_SCOPE | yes | internal config | engine validation/load failure | `8` |
| `LOCAL_AGENT_MODEL_CONTEXT` | LocalLLMEngine/profile | int | profile-derived | positive practical window | no | APPLICATION_SCOPE | yes | internal config | selection/inference failure | `4096` |
| `LOCAL_AGENT_MODEL_GPU_LAYERS` | LocalLLMEngine | int | `0` | integer supported by backend | no | APPLICATION_SCOPE | yes | internal config | model load failure | `0` |
| `LOCAL_AGENT_MODEL_MAX_TOKENS` | model profiles | int | profile-derived | positive integer | no | APPLICATION_SCOPE | yes | internal config | invalid capacity/inference failure | `1024` |
| `LOCAL_AGENT_REMOTE_MODEL_NAME` | RemoteLLMEngine | string | `Qwen3.5-27B` | provider model identifier | remote/hybrid | APPLICATION_SCOPE | yes | sensitive provider config | provider request failure | `example-model` |
| `LOCAL_AGENT_REMOTE_PROVIDER_KIND` | RemoteLLMEngine | string | `openai_compatible` | implemented provider kinds | no | APPLICATION_SCOPE | yes | internal config | incompatible payload behavior | `openai_compatible` |
| `LOCAL_AGENT_REMOTE_API_BASE_URL` | RemoteLLMEngine | string | empty | valid configured endpoint | remote/hybrid | APPLICATION_SCOPE | yes | secret/internal endpoint | startup fails when required and empty | `<configured-outside-docs>` |
| `LOCAL_AGENT_REMOTE_API_KEY` | RemoteLLMEngine | string | empty | provider credential | provider-dependent | APPLICATION_SCOPE | yes | secret | authentication/provider failure | `<secret-store-reference>` |
| `LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS` | HTTP transport | int seconds | `120` | integer | no | APPLICATION_SCOPE | yes | internal config | request timeout | `120` |
| `LOCAL_AGENT_REMOTE_VERIFY_TLS` | HTTP transport | bool | `0` | `0`,`1` | no | APPLICATION_SCOPE | yes | security critical | TLS verification behavior | `1` |
| `LOCAL_AGENT_REMOTE_ENABLE_THINKING` | HTTP payload | bool | `0` | `0`,`1` | no | APPLICATION_SCOPE | yes | internal config | provider compatibility failure | `0` |
| `LOCAL_AGENT_REMOTE_CONTEXT_WINDOW` | model profile | int | `32768` | positive practical window | no | APPLICATION_SCOPE | yes | internal config | routing/capacity mismatch | `32768` |
| `LOCAL_AGENT_LOCAL_FIXED_CALL_COST_UNITS` | ModelCostProfile | int | `1` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | clamped to minimum | `1` |
| `LOCAL_AGENT_LOCAL_INPUT_COST_UNITS_PER_1K_TOKENS` | ModelCostProfile | int | `1` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | clamped to minimum | `1` |
| `LOCAL_AGENT_LOCAL_OUTPUT_COST_UNITS_PER_1K_TOKENS` | ModelCostProfile | int | `1` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | clamped to minimum | `1` |
| `LOCAL_AGENT_LOCAL_ESTIMATED_LATENCY_MS` | ModelCostProfile | int | `1000` | integer ≥1 | no | APPLICATION_SCOPE | yes | public-safe count | clamped to minimum | `1000` |
| `LOCAL_AGENT_REMOTE_FIXED_CALL_COST_UNITS` | ModelCostProfile | int | `10` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | clamped to minimum | `10` |
| `LOCAL_AGENT_REMOTE_INPUT_COST_UNITS_PER_1K_TOKENS` | ModelCostProfile | int | `2` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | clamped to minimum | `2` |
| `LOCAL_AGENT_REMOTE_OUTPUT_COST_UNITS_PER_1K_TOKENS` | ModelCostProfile | int | `4` | integer ≥0 | no | APPLICATION_SCOPE | yes | public-safe count | clamped to minimum | `4` |
| `LOCAL_AGENT_REMOTE_ESTIMATED_LATENCY_MS` | ModelCostProfile | int | `3000` | integer ≥1 | no | APPLICATION_SCOPE | yes | public-safe count | clamped to minimum | `3000` |
| `LOCAL_AGENT_MODEL_BREAKER_FAILURE_THRESHOLD` | circuit registry | int | `3` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | clamped to minimum | `3` |
| `LOCAL_AGENT_MODEL_BREAKER_RECOVERY_TIMEOUT_SECONDS` | circuit registry | int | `30` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | clamped to minimum | `30` |
| `LOCAL_AGENT_MODEL_BREAKER_HALF_OPEN_MAX_CALLS` | circuit registry | int | `1` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | clamped to minimum | `1` |
| `LOCAL_AGENT_MODEL_BREAKER_COUNT_RATE_LIMITED` | circuit registry | bool | `1` | `0`,`1` | no | APPLICATION_SCOPE | yes | internal config | non-1 is false | `1` |
| `LOCAL_AGENT_MEMORY_DB_PATH` | MemoryManager | path | project database path | writable SQLite path | no | APPLICATION_SCOPE | yes | sensitive path/data | initialization/I/O failure | `data/database/memory.db` |
| `LOCAL_AGENT_CHROMA_DIR` | VectorDBManager | path | project chroma dir | readable/writable directory | no | APPLICATION_SCOPE | yes | sensitive path/data | KB degrades with safe code | `data/vector_store` |
| `LOCAL_AGENT_EMBEDDING_MODEL_PATH` | VectorDBManager | path | project model dir | readable model directory | no | APPLICATION_SCOPE | yes | sensitive path | KB degrades with safe code | `data/models/embedding` |
| `LOCAL_AGENT_EMBEDDING_QUERY_PROMPT_NAME` | embedding adapter | string | empty | backend-supported name | no | APPLICATION_SCOPE | yes | internal config | adapter behavior/failure | `query` |
| `LOCAL_AGENT_EMBEDDING_BATCH_SIZE` | embedding adapter | int | `8` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | clamped to minimum | `8` |
| `LOCAL_AGENT_EVENT_JOURNAL_DB_PATH` | SQLiteRunEventJournal | path | project database path | writable SQLite path | no | APPLICATION_SCOPE | yes | sensitive path/data | startup or append fails closed | `data/database/runtime_journal.db` |
| `LOCAL_AGENT_SNAPSHOT_ENABLED` | snapshot assembly | strict bool | `false` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | public-safe flag | typo fails Settings load | `false` |
| `LOCAL_AGENT_SNAPSHOT_DB_PATH` | SQLiteSnapshotStore | path | project database path | writable SQLite path | when enabled | APPLICATION_SCOPE | yes | sensitive path/data | startup/read/write fails closed | `data/database/runtime_snapshot.db` |
| `LOCAL_AGENT_OBSERVABILITY_CHECKPOINT_DB_PATH` | checkpoint stores | path | project database path | writable SQLite path | no | APPLICATION_SCOPE | yes | sensitive path/data | observability degraded/start failure | `data/database/runtime_observability.db` |
| `LOCAL_AGENT_OBSERVABILITY_QUEUE_CAPACITY` | dispatcher | int | `256` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | clamped; overflow drops/rejects diagnostically | `256` |
| `LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS` | Settings | int | `5` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | currently loaded but shutdown uses component timeout | `5` |
| `RUNTIME_DISCONNECT_GRACE_SECONDS` | HTTP disconnect cleanup | finite float | `0.75` | ≥0 | no | APPLICATION_SCOPE | yes | internal config | invalid value fails load | `0.75` |
| `RUNTIME_SHUTDOWN_GRACE_SECONDS` | GracefulShutdownCoordinator | finite float | `5.0` | ≥0 | no | APPLICATION_SCOPE | yes | internal config | invalid value fails load | `5.0` |
| `RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS` | shutdown/component close | finite float | `5.0` | ≥0 | no | APPLICATION_SCOPE | yes | internal config | invalid value fails load | `5.0` |
| `LOCAL_AGENT_METRICS_TOOL_NAME_ALLOWLIST` | MetricLabelPolicy | CSV string | empty | approved low-cardinality tool names | no | APPLICATION_SCOPE | yes | security allowlist | names outside allowlist not labeled | `calculator` |
| `LOCAL_AGENT_HISTORY_WINDOW_SIZE` | MemoryManager/router | int | profile-derived | integer | no | APPLICATION_SCOPE | yes | internal config | request/context behavior | `12` |
| `LOCAL_AGENT_SUMMARY_TRIGGER_MESSAGES` | MemoryManager/router | int | profile-derived | integer | no | APPLICATION_SCOPE | yes | internal config | summary behavior | `20` |
| `LOCAL_AGENT_SUMMARY_KEEP_RECENT` | MemoryManager/router | int | profile-derived | integer | no | APPLICATION_SCOPE | yes | internal config | summary behavior | `12` |
| `LOCAL_AGENT_SUMMARY_MAX_CHARS` | MemoryManager/router | int | profile-derived | integer | no | APPLICATION_SCOPE | yes | internal config | summary behavior | `1600` |
| `LOCAL_AGENT_RAG_TOP_K` | retrieval | int | profile-derived | integer | no | APPLICATION_SCOPE | yes | internal config | retrieval behavior | `3` |
| `LOCAL_AGENT_RAG_MIN_SCORE` | retrieval | float | `0.55` | clamped 0..1 | no | APPLICATION_SCOPE | yes | internal config | clamped to range | `0.55` |
| `LOCAL_AGENT_RAG_DOC_MAX_CHARS` | retrieval | int | profile-derived | integer | no | APPLICATION_SCOPE | yes | internal config | context behavior | `1000` |
| `LOCAL_AGENT_RAG_CONTEXT_MAX_CHARS` | retrieval | int | profile-derived | integer | no | APPLICATION_SCOPE | yes | internal config | context behavior | `2400` |
| `LOCAL_AGENT_ORCHESTRATION_ENABLED` | AgentRouter | bool | `1` | `0`,`1` | no | APPLICATION_SCOPE | yes | public-safe flag | non-1 is false | `1` |
| `LOCAL_AGENT_ORCHESTRATION_MAX_AGENTS` | AgentRouter | int | `3` | integer | no | APPLICATION_SCOPE | yes | internal config | orchestration behavior | `3` |
| `LOCAL_AGENT_SYNC_ENABLED` | sync feature | bool | `0` | `0`,`1` | no | APPLICATION_SCOPE | yes | internal config | non-1 is false | `0` |
| `LOCAL_AGENT_WIKI_COOKIE` | sync client | string | empty | credential | feature-dependent | APPLICATION_SCOPE | yes | secret | authentication/sync failure | `<secret-store-reference>` |
| `LOCAL_AGENT_LOCAL_KB_DIR` | KB scripts/router | path | project KB dir | readable directory | no | APPLICATION_SCOPE | yes | sensitive path/data | ingestion/retrieval failure | `data/knowledge_base` |
| `LOCAL_AGENT_KB_COLLECTION` | VectorDBManager | string | `huawei_wiki_collection` | safe collection name | no | APPLICATION_SCOPE | yes | internal identifier | backend behavior/failure | `knowledge_collection` |

## Runtime Selection

默认 `COORDINATED`。`LEGACY` 只能在新请求开始前通过 `CHAT_RUNTIME_MODE` 显式选择并重启；endpoint 对每个请求只捕获一次 mode。运行中不动态切换，任何已选路径失败都不会跨 Runtime fallback。

## Model Configuration

Model routing/retry/fallback 属于 Runtime policy；HTTP transport、本地模型加载和共享 Session 并发属于 adapter/application resource。Remote 引擎将 requests/urllib3 自动 retry 显式设为 0，由 RetryExecutor 统一拥有重试。代码没有项目级 trust-environment/proxy Settings 开关；`requests.Session` 的环境代理行为未被项目覆盖，部署时必须由外部环境审计，不能在本文假定禁用。

默认 Coordinated factory 的 Parallel policy 为 `max_concurrency=1`；Blocking executor 代码默认 `max_workers=4`、`max_pending_tasks=8`，当前没有对应 Settings 环境变量。

## Journal / Snapshot

Journal 在生产 lifespan 中固定装配 `SQLiteRunEventJournal`，schema v2、reader v1/v2，append-only 且损坏 fail closed；不得手工编辑 SQLite 记录。Snapshot 默认关闭，显式 opt-in 后装配 `SQLiteSnapshotStore`，schema v1 且 digest 严格校验；损坏、未知版本或部分持久化均 fail closed，不自动重存、不自动恢复。

## Observability / Trace

Observability 使用有界进程内队列、两个 SQLite consumer checkpoint store 和 best-effort projector；Health 为 `HEALTHY/DEGRADED`，记录 dropped/logger/metrics/worker/duplicate/record/flush failures 与 last safe code。Trace 当前是进程内 `InMemorySpanRecorder`，记录 active/completed/dropped 与 start/end/flush failures。两者故障不改变业务权威结果。禁止 Prompt、run id、路径、原始 Tool 名等高基数或敏感 label；Tool label 仅由 allowlist 开放。当前不等于已接 Prometheus/Grafana。

## Shutdown

Run drain 使用 `RUNTIME_SHUTDOWN_GRACE_SECONDS`，单组件关闭/worker drain 使用 `RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS`。存在 active/detached/unknown worker 时 Model close deferred，报告必须查看 `fully_closed`；`completed` 仅为 orchestration completion 兼容别名。Shutdown 同一 coordinator 成功完成后重入返回缓存报告；取消中的重入语义由专项测试覆盖，未知同步 close 状态不自动 double close。

## Fault Injection

生产配置入口：无。默认 `controller=None`。测试只能显式构造 test Scope/Controller；不存在 Settings、环境变量、HTTP、Prompt 或 Tool 参数激活方式。

