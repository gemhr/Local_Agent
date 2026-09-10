# Runtime Configuration Reference

## Stage 3 WP3 Security Configuration

| env | consumer | type | profile default | valid values | required | scope | restart | classification | failure behavior | example |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS` | ResourceAuthorizationService | semicolon-separated Windows paths -> `tuple[str,...]` | LOCAL=canonical project root；TEST=empty；PRODUCTION=无 | existing drive-qualified absolute local directories；拒绝relative/drive-relative/UNC/device/extended/file/nonexistent；canonical去重 | PRODUCTION | APPLICATION_SCOPE | yes | sensitive path/security policy | unmatched quote、NUL、empty middle segment、invalid/unavailable root或PRODUCTION missing/empty -> `SETTINGS_SECURITY_POLICY_ERROR`；LOCAL/TEST explicit empty=deny all | `<absolute-local-root-1>;<absolute-local-root-2>` |

PRODUCTION 的 `LOCAL_AGENT_API_HOST` 必须是 numeric loopback（IPv4 `127.0.0.0/8` 或 `::1`）；derived IPv6 URL使用brackets。`LOCAL_AGENT_API_BASE_URL` 必须为 `http`、numeric loopback、无userinfo/query/fragment，path仅empty或`/`。LOCAL/TEST不强制loopback，但只属于无认证、无inbound TLS的开发边界。

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
| `LOCAL_AGENT_MODEL_MAX_TOKENS` | model profiles | int | profile/backend-derived：remote-only fast=`2048`、balanced=`4096`、deep=`8192`；local/hybrid 保持 `640/1024/1536` | positive integer | no | APPLICATION_SCOPE | yes | internal config | invalid capacity/inference failure | `4096` |
| `LOCAL_AGENT_REMOTE_MODEL_NAME` | RemoteLLMEngine | string | `deepseek-v4-flash` | provider model identifier | remote/hybrid | APPLICATION_SCOPE | yes | sensitive provider config | provider request failure | `deepseek-v4-flash` |
| `LOCAL_AGENT_REMOTE_PROVIDER_KIND` | RemoteLLMEngine | string | `deepseek` | implemented provider kinds | no | APPLICATION_SCOPE | yes | internal config | incompatible payload behavior；其他 OpenAI-compatible Provider 必须显式覆盖 | `deepseek` |
| `LOCAL_AGENT_REMOTE_API_BASE_URL` | RemoteLLMEngine | string | empty | valid configured endpoint | remote/hybrid（SERVER role） | APPLICATION_SCOPE | yes | secret/internal endpoint | SERVER role 缺 endpoint fail closed；PRODUCTION 必须 HTTPS | `<configured-outside-docs>` |
| `LOCAL_AGENT_REMOTE_API_KEY` | RemoteLLMEngine | string | empty | provider credential | provider-dependent | APPLICATION_SCOPE | yes | secret | authentication/provider failure | `<secret-store-reference>` |
| `LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS` | HTTP transport | int seconds | `120` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值在 Settings 期 fail closed（不进入 requests） | `120` |
| `LOCAL_AGENT_REMOTE_VERIFY_TLS` | HTTP transport | strict bool | profile-derived：LOCAL=`0`、TEST=`1`、PRODUCTION=`1` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | security critical | 非法显式值 fail closed；PRODUCTION 显式关闭为 security policy failure | `1` |
| `LOCAL_AGENT_REMOTE_TRUST_ENV` | HTTP transport | strict bool | profile-derived：LOCAL=`1`、TEST=`0`、PRODUCTION=`0` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | security critical | 非法显式值 fail closed；决定 Server → Remote LLM Session 是否继承系统 proxy | `0` |
| `LOCAL_AGENT_CLIENT_TRUST_ENV` | HTTP transport | strict bool | `1`（所有 Profile 一致） | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | security critical | 非法显式值 fail closed；决定 Desktop Client → LocalAgent Server Session 是否继承系统 proxy；与 `LOCAL_AGENT_REMOTE_TRUST_ENV` 完全独立 | `1` |
| `LOCAL_AGENT_REMOTE_ENABLE_THINKING` | HTTP payload | strict bool | `0` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | internal config | 非法显式值 fail closed | `0` |
| `LOCAL_AGENT_REMOTE_CONTEXT_WINDOW` | model profile | int | `1000000` | positive practical window；应与真实 Provider capacity 一致 | no | APPLICATION_SCOPE | yes | internal config | routing/capacity mismatch | `1000000` |
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
| `LOCAL_AGENT_CHROMA_DIR` | VectorDBManager | path | project chroma dir | readable/writable directory | no | APPLICATION_SCOPE | yes | sensitive path/data | KB degrades with safe code；PRODUCTION 默认 required | `data/vector_store` |
| `LOCAL_AGENT_EMBEDDING_MODEL_PATH` | VectorDBManager | path | `data/models/Qwen3-Embedding-0.6B` | readable local model directory；相对路径按 project root 解析 | no | APPLICATION_SCOPE | yes | sensitive path | 缺失时离线 fail fast；KB 按 required policy 失败或降级 | `data/models/embedding` |
| `LOCAL_AGENT_EMBEDDING_QUERY_PROMPT_NAME` | embedding adapter | string | empty | backend-supported name | no | APPLICATION_SCOPE | yes | internal config | adapter behavior/failure | `query` |
| `LOCAL_AGENT_EMBEDDING_BATCH_SIZE` | embedding adapter | int | `8` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `8` |
| `LOCAL_AGENT_SNAPSHOT_ENABLED` | PostgresSnapshotStore assembly | strict bool | `false` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | public-safe flag | typo fails Settings load | `false` |
| `LOCAL_AGENT_DATABASE_URL` | PostgreSQL persistence（canonical） | DSN (`postgresql+asyncpg`) | 无（SERVER 必填） | 仅接受 `postgresql+asyncpg://` | SERVER/SCRIPT | APPLICATION_SCOPE | yes | **secret**：绝不进入 repr/log/trace/错误正文 | 空值或非 asyncpg driver 在 Settings 阶段 fail closed；不可达 / schema 未就绪阻止 startup READY | `<secret-store-reference>` |
| `LOCAL_AGENT_DB_POOL_SIZE` | Database（pool） | int | `5` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `5` |
| `LOCAL_AGENT_DB_MAX_OVERFLOW` | Database（pool） | int | `5` | integer ≥0 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `5` |
| `LOCAL_AGENT_DB_POOL_TIMEOUT_SECONDS` | Database（pool acquire） | finite float | `5.0` | >0 | no | APPLICATION_SCOPE | yes | internal config | pool 耗尽映射为 typed `DATABASE_POOL_EXHAUSTED`（HTTP 503），不无限等待 | `5.0` |
| `LOCAL_AGENT_DB_POOL_RECYCLE_SECONDS` | Database（pool） | int | `1800` | integer ≥0 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `1800` |
| `LOCAL_AGENT_DB_CONNECT_TIMEOUT_SECONDS` | Database（connect） | finite float | `5.0` | >0 | no | APPLICATION_SCOPE | yes | internal config | 不可达映射为 typed `DATABASE_UNAVAILABLE` | `5.0` |
| `LOCAL_AGENT_DB_STATEMENT_TIMEOUT_MS` | Database（per-connection） | int | `15000` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 超时映射为 typed `DATABASE_STATEMENT_TIMEOUT` 并回滚事务 | `15000` |
| `LOCAL_AGENT_DB_LOCK_TIMEOUT_MS` | Database（per-connection） | int | `5000` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 超时映射为 typed `DATABASE_LOCK_TIMEOUT` | `5000` |
| `LOCAL_AGENT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | Database（per-connection） | int | `10000` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 空闲事务被终止并映射为 typed timeout | `10000` |
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
| `LOCAL_AGENT_KB_CHUNK_SIZE` | production KB build | int | `1400` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | production 构建 chunk policy 唯一 authority；production CLI 禁止覆盖；越界显式值 fail closed | `1400` |
| `LOCAL_AGENT_KB_CHUNK_OVERLAP` | production KB build | int | `180` | integer ≥0 且 < chunk size | no | APPLICATION_SCOPE | yes | internal config | `>= chunk_size` 显式值 fail closed（`overlap_not_below_size`） | `180` |
| `LOCAL_AGENT_RETRIEVAL_STRATEGY` | lifespan composition | enum | `BASELINE` | `BASELINE`,`HYBRID_RRF` | no | APPLICATION_SCOPE | yes | public-safe enum | 默认 `BASELINE`；`HYBRID_RRF` 已生产可达但非默认，要求完整 v2 generation 与已验证 BM25 artifact。依赖不可用且 KB optional 时可 READY-degraded，但该策略的请求仍以 `HYBRID_STRATEGY_UNAVAILABLE` fail closed，绝不回退 baseline。Hybrid `rag_top_k > 8` 配置 fail closed | `BASELINE` |
| `LOCAL_AGENT_KB_REQUIRED` | startup policy | strict bool | profile-derived：LOCAL=`0`、TEST=`0`、PRODUCTION=`1` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | public-safe flag | PRODUCTION 默认 KB 失败阻止 startup；显式 `false` 才允许 degraded | `1` |
| `LOCAL_AGENT_BLOCKING_MAX_WORKERS` | BoundedBlockingExecutor | int | `4` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed；三个 lifespan executor 统一使用 | `4` |
| `LOCAL_AGENT_BLOCKING_MAX_PENDING_TASKS` | BoundedBlockingExecutor | int | `8` | integer ≥0 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `8` |
| `LOCAL_AGENT_EVENT_CHANNEL_CAPACITY` | RuntimeEventChannel（经 Factory） | int | `32` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `32` |
| `LOCAL_AGENT_PLANNING_TIMEOUT_SECONDS` | RunCoordinator planning | finite float | `15.0` | finite >0 | no | APPLICATION_SCOPE | yes | internal config | 非正数/NaN/Inf 显式值 fail closed | `15.0` |
| `LOCAL_AGENT_STEP_RESULT_PER_RESULT_CHARS` | StepResultStore | int | `20000` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `20000` |
| `LOCAL_AGENT_STEP_RESULT_RUN_TOTAL_CHARS` | StepResultStore | int | `60000` | integer ≥1 且 ≥ per-result | no | APPLICATION_SCOPE | yes | internal config | 越界或小于 per-result 显式值 fail closed | `60000` |
| `LOCAL_AGENT_STEP_RESULT_MAX_ENTRIES` | StepResultStore | int | `16` | integer ≥1 | no | APPLICATION_SCOPE | yes | internal config | 越界显式值 fail closed | `16` |
| `LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED` | AgentEvalOpsTraceExporter | strict bool | `0` | `1`,`0`,`true`,`false` | no | APPLICATION_SCOPE | yes | public-safe flag | enabled 时缺 base_url/api_key/project_id、无效 timeout、deadline 不满足 close invariant 均 startup-fatal | `0` |
| `LOCAL_AGENT_AGENTEVALOPS_BASE_URL` | AgentEvalOpsTraceExporter | http(s) origin URL | empty | http/https origin（无 path/query/fragment/userinfo） | when enabled | APPLICATION_SCOPE | yes | internal endpoint | enabled 时缺失/非法 startup-fatal；PRODUCTION 强制 https | `http://127.0.0.1:8001` |
| `LOCAL_AGENT_AGENTEVALOPS_API_KEY` | AgentEvalOpsTraceExporter | string | empty | credential | when enabled | APPLICATION_SCOPE | yes | secret（`repr=False`） | enabled 时缺失 startup-fatal；不进日志/异常/health | `<secret-store-reference>` |
| `LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID` | AgentEvalOpsTraceExporter | string | empty | non-empty identifier | when enabled | APPLICATION_SCOPE | yes | internal identifier | enabled 时缺失 startup-fatal | `project-1` |
| `LOCAL_AGENT_AGENTEVALOPS_CONNECT_TIMEOUT_SECONDS` | AgentEvalOpsTraceExporter | finite float | `0.5` | finite >0 且可转换为整数 ms | when enabled | APPLICATION_SCOPE | yes | internal config | 非正/非有限/小数毫秒/超过 total deadline 显式值 fail closed | `0.5` |
| `LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_TOTAL_DEADLINE_SECONDS` | AgentEvalOpsTraceExporter | finite float | `3.0` | finite >0 且可转换为整数 ms | when enabled | APPLICATION_SCOPE | yes | internal config | 非正/非有限/小数毫秒 fail closed；total + 0.5s cleanup margin 必须 `< RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS` | `3.0` |
| `LOCAL_AGENT_MCP_CONFIG_PATH` | mcp.config / lifespan MCP assembly | path（JSON 文件） | empty | operator 本地 JSON 文件，schema `localagent-mcp-config.v1`（server identity/command/arguments/environment/enabled + 可选 per-tool `tools` local mapping，fail-closed 校验） | no | APPLICATION_SCOPE | yes | sensitive local operator config（environment 值通常含 secret，文件不得提交） | 留空=默认关闭，不构造 MCP 集成组件、不启动子进程；文件缺失/非法/schema 不匹配/字段校验失败为 `MCP_CONFIG_INVALID` startup fatal | `<local-operator-path>` |
| `LOCAL_AGENT_MCP_CONNECT_TIMEOUT_SECONDS` | mcp.client（stdio spawn + initialize） | finite float | `5.0` | finite >0 | no | APPLICATION_SCOPE | yes | internal config | 非正/非有限/非法显式值 fail closed；超时按 MCP transport 失败处理（单 server 降级，不重试/重连） | `5.0` |
| `LOCAL_AGENT_MCP_REQUEST_TIMEOUT_SECONDS` | mcp.client（`tools/list` / `tools/call` 单请求上界） | finite float | `10.0` | finite >0 | no | APPLICATION_SCOPE | yes | internal config | 非正/非有限/非法显式值 fail closed；底层 bounded IO 机制，Runtime effective deadline（`ToolExecutionService`）为真正 authority，adapter 取两者较小值 | `10.0` |

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

Journal 在生产 lifespan 中固定装配 `PostgresRunEventJournal`，由 Alembic canonical schema 和 typed digest 校验拥有 append-only 事实；读取损坏、未知版本或约束冲突均 fail closed。Snapshot 默认关闭，显式 opt-in 后装配 `PostgresSnapshotStore`，schema/digest 严格校验；不自动重存、不自动恢复。Memory、Journal、Snapshot、Checkpoint 均使用同一 application-scope PostgreSQL `Database` engine/pool/session factory；生产路径不再读取或构造 SQLite Memory。

## Observability / Trace

Observability 使用有界进程内队列、两个 PostgreSQL consumer checkpoint store 和 best-effort projector；Health 为 `HEALTHY/DEGRADED`，记录 dropped/logger/metrics/worker/duplicate/record/flush failures 与 last safe code。Trace 当前是进程内 `InMemorySpanRecorder`，记录 active/completed/dropped 与 start/end/flush failures。两者故障不改变业务权威结果。禁止 Prompt、run id、路径、原始 Tool 名等高基数或敏感 label；Tool label 仅由 allowlist 开放。当前不等于已接 Prometheus/Grafana。

## Shutdown

Run drain 使用 `RUNTIME_SHUTDOWN_GRACE_SECONDS`，单组件关闭/worker drain 使用 `RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS`。存在 active/detached/unknown worker 时 Model close deferred，报告必须查看 `fully_closed`；`completed` 仅为 orchestration completion 兼容别名。Shutdown 同一 coordinator 成功完成后重入返回缓存报告；取消中的重入语义由专项测试覆盖，未知同步 close 状态不自动 double close。

## Deprecated Configuration

- `LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS`：DEPRECATED。保留字段与 env 一个 Stage 3 兼容周期，仍严格解析；显式配置产生一次安全 deprecation warning（只含 env 名），不改变行为。Replacement：`RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS`。
- `ChatService.event_channel_capacity`：DEPRECATED ignored constructor shim。真实 per-run channel capacity 的 Owner 是 `LOCAL_AGENT_EVENT_CHANNEL_CAPACITY` → `CoordinatedRuntimeFactory` → `RuntimeEventChannel`；该参数保留以兼容调用方，但不消费、不得接线成第二 Owner。

## Fault Injection

生产配置入口：无。默认 `controller=None`。测试只能显式构造 test Scope/Controller；不存在 Settings、环境变量、HTTP、Prompt 或 Tool 参数激活方式。
## WP3 production-target evaluation-only controls

正常运行不设置以下变量。只有正式 WP3 evaluation process 才可同时设置
`LOCAL_AGENT_EVALUATION_MODE=1`、`LOCAL_AGENT_EVALUATION_GENERATION_PIN_PATH`、
`LOCAL_AGENT_EVALUATION_REWRITE_FIXTURE_PATH` 和
`LOCAL_AGENT_EVALUATION_IDENTITY_SHA256`。进程启动时会只读校验同一 active
generation pin，并回放 immutable rewrite fixture；generation、provenance、fixture
或 identity 不一致时 fail closed。该能力不增加 request-level strategy/rewrite override，
不构建或复制 Dense index，也不改变 `BASELINE` production default。

## MCP Integration（Phase9-WP1 / WP2）

`LOCAL_AGENT_MCP_CONFIG_PATH` 指向的 JSON 文件是唯一 MCP server 配置来源；
server identity、command、arguments、environment 全部为 operator 显式配置，
模型输出、Tool 参数与请求输入不得提供 MCP command 或触发 MCP server 启动。
未配置时 Phase9 完全不参与 startup（无子进程、无组件、无 MCP Tool 注册）。
配置存在时，`mcp.discovery` 在 ToolRegistry / PolicyCatalog freeze 之前执行
`STARTUP_SNAPSHOT_ONLY` discovery；单个 enabled server 的 spawn/协议/discovery
失败按显式降级策略记为 `DISCOVERY_FAILED`（safe code：`MCP_SERVER_UNAVAILABLE`、
`MCP_TRANSPORT_TIMEOUT`、`MCP_TRANSPORT_CLOSED`、`MCP_PROTOCOL_ERROR`、
`MCP_PROTOCOL_VERSION_UNSUPPORTED`、`MCP_CAPABILITY_MISSING`、
`MCP_DISCOVERY_INVALID`），不阻止 startup、不产生第二 runtime、不放宽本地
policy；配置文件本身非法为 startup fatal。AVAILABLE server 的 client/session
属 APPLICATION_SCOPE，由 `server.py::lifespan()` 的 initialization stack /
`ApplicationRuntimeServices.extra_closeables` 拥有并在 shutdown 关闭；
Run 终止不关闭它。

### WP2：per-tool local mapping（canonical name + policy 输入）

配置 schema `localagent-mcp-config.v1` 在 server 级新增可选 `tools` 字段：
`remote tool name -> { local_name, side_effect_kind, idempotency, risk_facts?,
approval_required_threshold?, default_timeout_seconds?, max_output_bytes?,
max_concurrency? }`。语义（全部 fail closed，`MCP_CONFIG_INVALID` startup
fatal）：

- `local_name` 是该 tool 在 LocalAgent 的 canonical tool name（
  `^[a-z][a-z0-9_]{0,63}$`），是 ToolRegistry / PolicyCatalog /
  ToolInvocation / model-facing identity；`server_id` + remote name 只保留
  在 adapter provenance。同一 server 内 `local_name` 重复即配置非法。
- `side_effect_kind` 只允许 `NONE` / `LOCAL_STATE_MUTATION`；
  `idempotency` 只允许 `READ_ONLY` / `IDEMPOTENT` / `IDEMPOTENT_WITH_KEY` /
  `NON_IDEMPOTENT`；`risk_facts` 只允许现有 `ToolRiskFact` 取值。
- MCP annotations / inputSchema / description 全部是 untrusted provider
  metadata，绝不参与 policy/risk/approval 推导；本地 policy 与
  `ToolAdapter.spec_for` 是唯一 Runtime safety fact 来源。
- discovery 到的每个 remote tool 都必须有 operator 映射：任一 tool 缺失
  映射（`MCP_TOOL_POLICY_MISSING`）、映射非法（`MCP_TOOL_POLICY_INVALID`）、
  组合无法被 Governance full-combination allowlist 分类
  （`MCP_TOOL_RISK_UNCLASSIFIED`）、与内置或其它 server 的 local name 冲突
  （`MCP_TOOL_NAME_COLLISION`）或 inputSchema 不可用
  （`MCP_TOOL_INPUT_SCHEMA_INVALID` / `MCP_TOOL_INPUT_SCHEMA_UNSUPPORTED`）
  时，该 configured server 的 registration 整体 fail closed（零注册），
  不部分采纳；不产生 `ToolRegistry 已注册 / PolicyCatalog 无覆盖` 中间态，
  也不阻止 application startup。
- 没有 `tools` 字段的 server 只参与 discovery，不注册任何 MCP Tool。
- 注册成功的 MCP Tool 经既有 `ToolRegistration.native_function_definition()`
  暴露给模型；execution 走既有 `ToolExecutionService -> adapter.invoke_once
  -> session_for(server_id) -> tools/call` 路径，Runtime
  timeout/cancellation authority 不变；结果归一化仅支持 text content +
  `isError`，其它内容形态 safe failure（`MCP_TOOL_RESULT_UNSUPPORTED`）。
## Stage6-WP2 Authentication

服务端配置 `LOCAL_AGENT_JWT_PUBLIC_KEY`、`LOCAL_AGENT_JWT_ISSUER`、`LOCAL_AGENT_JWT_AUDIENCE`
和可选的 `LOCAL_AGENT_JWT_CLOCK_SKEW_SECONDS`。算法固定为 `EdDSA`，API 验证路径只加载公钥。
`LOCAL_AGENT_JWT_PRIVATE_KEY` 仅供 `scripts/manage_identity.py` 在 LOCAL/TEST 中签发受控测试令牌，
不得配置到生产 API 进程。
