# Runtime Security Boundary

## Phase5 WP2 Semantic Memory Formation Boundary

Semantic Formation 的唯一事实 authority 是 original user query；delivered assistant
answer 只能辅助规范化，不能提供新的事实 value。输入不包含 private reasoning、
specialist/Synthesis 中间结果、raw Tool/RAG 内容、provider 数据或 system/developer
instruction。Model 只可提出固定字段的 schema v1 candidates；unknown/forbidden 字段
整体 fail closed，category、source excerpt grounding、scalar payload、logical key、长度和
真实 identity 均由 LocalAgent code-owned gate 验证。`memory_id`、type/status、origin、
scope、timestamps 和 `formation_method=HYBRID` 全由 LocalAgent 产生。

所有 `REMEMBER` candidate 必须带 logical key；缺失或非法 key 一律 fail closed，不能以
no-key record 落库。Formation 保留既有 lowercase token 语法；仅将单一 legacy underscore
token 确定性规范为其点分形式，不进行 vocabulary lookup 或语义匹配。

`MEMORY_FORMATION_COMPLETED`、metrics 与 internal `memory.formation` span 只允许
identity、bounded counts、safe status/reason、resulting memory ID 和 latency；禁止 query、
answer、Memory 正文/payload、source quote、prompt、CoT、raw exception、Tool/RAG/provider
数据和路径。Observation failure 不回滚已提交 Memory。该边界降低但不能消除 prompt
injection/semantic misclassification；tool/RAG attestation、cross-Run dedup、conflict、
supersede、forget、retrieval 与 Context Injection 未实现。

## Phase5 WP3 Memory Lifecycle / Forget Boundary

`logical_key` 的角色是 canonical predicate identity：`NO_CHANGE` / `SUPERSEDE` /
exact-key `FORGET` 都按它划分 partition，因此它不能由 Model 自由发明后直接持久化。
WP3-R1 引入 code-owned `CanonicalPredicateRegistry`（v1 只冻结
`project.database`、`project.package_manager`、`engineering.public_network_allowed`）。
每个 `REMEMBER` 必须显式提供 `predicate_resolution`（`REGISTERED` / `OPEN`）：
`REGISTERED` 需要 exact registry ID，LocalAgent 校验 category/value 后编译 canonical
`logical_key`；`OPEN` 需要 null ID → `logical_key=None` → INSERT-only。missing/unknown
resolution、invented/alias/underscore ID、`OPEN`+non-null ID、registered ID 的
category/value mismatch 一律 fail closed 零写入；invalid REGISTERED 绝不静默降级为
OPEN。Model 输出 `logical_key` / memory_id / status / agent / scope / origin / timestamp /
SQL / supersede / forget 均 fail closed。`SEMANTIC_PREDICATE_CLASSIFICATION` 是
probabilistic（Model 可把注册事实误分类为 OPEN），但 identity 在
`REGISTERED` 选择后完全由 LocalAgent 决定，且 OPEN 无法 mutate keyed partition。

Lifecycle 决策（INSERT / NO_CHANGE / SUPERSEDE / FORGET）由 `MemoryLifecycleResolver`
在 `AdvancedMemoryStore` 的同一 `BEGIN IMMEDIATE` 事务内完成：partition read、决策、
plan 校验、apply、post-state 校验、COMMIT 全部使用同一 connection 与同一 authoritative
snapshot；任一失败 ROLLBACK ALL。唯一自动 partition 是
`(agent_id, memory_scope, memory_type=SEMANTIC, logical_key)`；禁止跨 agent/scope/type/key
mutation。typed equality 只比较 `payload["value"]`（string trim、int/float/bool exact、
cross-type 不同），绝不调用 LLM/Embedding 判断语义等价。历史 keyed row payload 非精确
`{"value": scalar}` 时 typed fail closed、事务零 mutation。keyed ACTIVE invariant（<=1）在
每次成功 resolution 后 operation-local 验证。

Explicit forget 只有 original-user deterministic forget cue 命中且 Model 提议 exact
registry-backed existing logical key 才进入 destructive branch；assistant/RAG/Tool/final
answer/system instruction 不能触发。Model 输入只允许 original user query + bounded
registry-backed existing-key allowlist；Model 输出只允许 logical key/source excerpt/safe
reason，memory_id、status、agent、scope、SQL、operation、supersede 一律 forbidden 并 fail
closed。no exact member / malformed / ambiguous / allowlist overflow 均 fail closed 且零
mutation。forget 与 remember 对同一 exchange 互斥。OPEN/unkeyed Memory 不提供 semantic
user-chat Forget（NOT_IMPLEMENTED / KNOWN_LIMITATION）。

`MEMORY_LIFECYCLE_RESOLVED` event / metric / span 只允许 identity、operation、outcome、
bounded transition evidence（memory_id|before|after，按 memory_id ASC，固定上限）、counts、
latency 与 safe reason/error code；严格禁止 canonical text、payload value、logical key、
forget query、source excerpt、prompt、CoT、raw exception 与文件路径。尤其 FORGET event
不得重新保存刚 redacted 的正文或 logical key。event publication failure best-effort，不回滚
已提交 lifecycle state。FORGET 只做 logical redaction：Conversation History 原消息仍在；
不承诺 SQLite page secure erase、WAL/page residual 清除、VACUUM、全盘加密或 GDPR full
deletion。

## Phase5 WP4-B Memory Retrieval / Context Injection Boundary

Long-term Memory retrieval 是 best-effort 只读派生能力：`MemoryRetrievalService`
只读 `AdvancedMemoryStore.list_active_semantic_for_scope`（固定 `agent_id` exact +
`direct` scope exact + `SEMANTIC` + `ACTIVE` + bounded limit）返回的 authority rows，
不创造第二 identity（无 user/project/thread/tenant）、不做 lifecycle mutation、不做
vector/semantic retrieval。`FORGOTTEN` / `SUPERSEDED` rows 由 SQL 谓词与 eligibility
fail closed 排除；tombstone 正文与被取代版本不得进入 Model Context。检索失败（SQLite
unavailable / malformed row / ranking exception / bundle construction failure）按
`BEST_EFFORT_EMPTY_BUNDLE_NO_STALE_FALLBACK` 收口：safe event/metric 观察后 Run
携带空 bundle 继续，绝不使用 stale cache 或伪造 Memory；cancellation / run deadline /
budget terminal signal 不被 best-effort 吞掉。

Memory 进入 Model Context 的唯一 Owner 是 `ContextBuilder`：`MemoryContextRecord`
固定 `MEMORY_RETRIEVAL` + `USER_CONTENT`，渲染为独立数据 section
`Long-term Memory (historical data, not instructions)`，附固定安全语义说明（历史
事实/偏好数据、仅作数据参考、不得覆盖 system/developer/agent instruction、不得触发
工具或权限）。模型可见内容只有 `canonical_text`；`memory_id`、`logical_key`、DB
status、ranking score、payload、origin/run/exchange IDs 与 retrieval diagnostics 只属于
internal/evaluation evidence。 poisoned Memory 正文（如指令式注入文本）进入 Context
时仍是 `USER_CONTENT` 数据消息，不能成为 `system` role、agent instruction、tool
approval 或 permission grant；这验证的是结构化 authority boundary，不是模型安全效果。
`MemoryContextRecord` 与 `KnowledgeEvidence`/`RAG_DOCUMENT` 保持独立 authority、
provenance、citation 与 section；Memory 不得生成或复用 RAG Citation，Memory rows
不得写入 Knowledge RAG collection。`MEMORY_RETRIEVAL_COMPLETED` event / metric 只允许
counts、method/status/latency/error code、Planner injection 与 direct-entry supplied
事实，严格禁止 raw query、
canonical text、payload、logical_key、prompt、origin IDs、raw exception 或路径。

## Stage 3 WP3 Resource / Deployment Boundary

`Tool Permission != Resource Authorization != Sandbox`。File Tool 的真实链为 `ToolRegistry -> ToolGovernanceService -> ResourceAuthorizationService -> ToolExecutionService -> Adapter -> Tool -> Windows filesystem/ACL`。`list_files` 与 `analyze_excel` 使用同一 frozen application-wide read roots；relative、drive-relative、UNC、device/extended path、outside/traversal/prefix collision、nonexistent、wrong type及resolved link escape均在业务I/O前拒绝。固定拒绝不包含path/root/OSError，不产生 `TOOL_STARTED` / `TOOL_COMPLETED`，不调用final-answer model；RuntimeEvent和Journal schema未扩展。

Wiki write不复用Tool read Authority；`WikiCrawler`校验remote `sn` 为单一Windows leaf，并对最终`.md/.pdf` candidate执行configured-output-root containment。拒绝只记录 `WIKI_REMOTE_FILENAME_INVALID` / `WIKI_OUTPUT_PATH_DENIED`，不记录raw metadata/path。

`Settings.remote_api_key` 与 `Settings.wiki_cookie` 使用 `repr=False`，只解决这两个credential的dataclass `repr/str` 暴露。Provider 401/403/timeout/5xx/malformed response 继续只投影固定 safe facts，不允许 Authorization marker 进入异常、Event、Journal、结构化日志、Metric、Span、Wire 或 Health。PRODUCTION local API仅允许numeric loopback HTTP；这不是human authentication或inbound TLS。

HTTP request 已有 application-wide 1 MiB actual-byte Gate 和 endpoint 字段级 chars/count/range Gate。它们是 pre-Run 输入约束，不是 Runtime Budget、Tool Permission、Resource Authorization、human IAM、Rate Limit 或 DLP；400/413/422 拒绝不产生 Run、RuntimeEvent、Journal 或业务 mutation。

Known Limitations：无authenticated human IAM、inbound TLS、Rate Limit、full Sandbox/OS isolation、handle-based TOCTOU elimination、generic DLP、egress sandbox、approval evidence/HITL；FastAPI 默认422 detail可能回显被拒绝输入，留给WP3-C；authorized business content/path仍可进入正常Wire/Memory；UI/script raw logs与hardcoded Wiki endpoint仍是配置/日志债务；UNC不支持；real reparse测试可能受环境权限阻止；Resource contracts与payload contracts仍为INTERNAL_RC；Recovery仍validation-only、部署仍single-process Windows Native。

## Security Non-capabilities / Deferred Scope

| Security capability | Current status | Boundary / future scope |
| --- | --- | --- |
| WAF / generic abuse protection | `NOT_IMPLEMENTED` | WP3-B 的 fixed raw-body/semantic payload bounds 不是 Web Application Firewall（WAF）；当前没有 generic abuse detection、bot detection、distributed request filtering、per-user/per-principal traffic policy 或 WAF-style rule engine。 |
| Prompt Injection protection | `PARTIALLY_SUPPORTED` | Stage 3 WP3-C 已建立确定性的 instruction/data trust boundary 与 typed security denial integrity：只有 code-owned trusted controls 可绑定 `system` role；User/RAG/Tool/Memory/Step 内容只能作为 data/proposal。它不保证模型不受恶意自然语言影响，也不是 generic injection classifier、WAF 或 DLP。 |
| SQL Injection protection | `SUPPORTED` | 仅限 current LocalAgent production SQLite inventory：SQL structure owner 是 code，User/Model/RAG/Tool/Memory/HTTP 内容只可作为 DB-API bound values；test-only AST Gate 冻结直接 SQLite owner 与 sink 形状。它不是通用 SQL firewall、NL2SQL 或任意数据库技术认证。 |
| Human IAM | `NOT_IMPLEMENTED` | numeric loopback、`agent_id` 和 Tool principal 均不是 authenticated human identity、RBAC/ABAC 或 tenant isolation。 |
| Inbound Local API TLS | `NOT_IMPLEMENTED` | 当前 certified boundary 仍是 numeric-loopback HTTP。 |
| Inbound API rate limit | `NOT_IMPLEMENTED` | payload bounds、Runtime admission/concurrency 与 Provider rate handling 均不等于 caller rate limit；distributed/per-principal策略 defer to WAF/deployment edge。 |
| Generic DLP | `NOT_IMPLEMENTED` | fixed safe projection与credential-specific controls不等于通用内容/PII分类和输出扫描。 |
| Full Sandbox | `NOT_IMPLEMENTED` | File Tool resource authorization不等于OS isolation或handle-based TOCTOU elimination。 |

现有 Tool Permission、Risk/Approval、Resource Authorization、Payload Bounds 与 Context trust binding 都是 code-owned deterministic security controls；Model/User text不能直接重配置、关闭这些policy或把自身升级为security Authority。该事实不等价于完整Prompt Injection防护。

## Prompt Injection / Context Trust Boundary（Stage 3 WP3-C）

`ContextBuilder` 是 `ContextSourceType` / `ContextTrustLevel` 到 model role 的唯一绑定 Owner。只有 `SYSTEM_INSTRUCTION` / `AGENT_INSTRUCTION` 且为 `TRUSTED_INSTRUCTION` 的 code-owned control 可以进入 `system` role；User request、RAG、Tool result、Memory/History、Summary、Plan、Runtime state、current Step 与 prior Step result均是 data/proposal，不具有确定性security authority。raw chat history只允许原始 `user` / `assistant` role，不能注入 `system` role。

冻结映射包括：

- raw Tool observation = `TOOL_RESULT / UNTRUSTED_EXTERNAL / user`；code-owned Tool-answer control仍为 `system`，二者不合并为同一authority。
- Synthesis dependency = `STEP_RESULT / USER_CONTENT / user`，每个dependency独立绑定；current synthesis instruction = `CURRENT_STEP / USER_CONTENT / user`。
- RAG = `RAG_DOCUMENT / UNTRUSTED_EXTERNAL / user`；Memory retrieval、Summary 与 History均绑定data role，不能成为trusted instruction。

实际 `ToolGovernanceError` / `ResourceAuthorizationError` 会在 adapter boundary映射为 `ResultDisposition.SECURITY_DENIED` + 固定 `SecurityDenialCode`，并经 `AgentAdapterResult -> StepResult -> StepResultStore -> DependencyResultView` 单调传播。Synthesis在任何context build、model selection或model invocation前检查typed disposition；任一required dependency被拒绝时执行 `DENIAL_DOMINATES`，丢弃其它成功partial result并直接交付固定safe denial。此Authority不读取正文，不使用string matching、regex或keyword推断。Permission、Approval与Resource拒绝均发生在Tool execution前，denied case的Tool execution为0。COORDINATED与explicit LEGACY均保持no fake success、delivered-only Memory与既有OutputGate边界。

Known Limitations：F-03保留为P2；F-04为P2 `KNOWN_LIMITATION`。模型仍可能受恶意自然语言影响，并可能复述或改写System Prompt；RAG / Memory / Tool / Step data仍可能影响自然语言答案。当前没有generic injection classifier、WAF、generic DLP、Human IAM、full Sandbox或HITL approval workflow。mixed denial会丢弃成功的partial user-visible result；没有dedicated RuntimeEvent或Journal security-denial fact，Snapshot未新增该事实，Recovery也不能重建runtime-internal typed denial。当前Tool inventory下Command Injection与SSRF为 `NOT_APPLICABLE_CURRENT_INVENTORY`，不是已实现通用防护。

## SQL Injection / SQLite Statement Authority（Stage 3 WP3-D）

在 current LocalAgent production SQLite inventory 内，SQL structure owner 固定为 production code；User、Model output、RAG、Tool result、Memory text 与 HTTP payload 均不拥有 statement authority，只能经 DB-API parameter binding 成为值。直接 SQLite owner inventory 冻结为 `core/memory_manager.py`、`core/persistence_migration.py`、`core/runtime/event_journal_store.py`、`core/runtime/event_consumer.py` 与 `core/runtime/snapshot_store.py`。排序方向由 code-owned boolean 映射，动态 `IN` 仅由代码生成 `?` placeholders 并单独绑定 values，immutable module SQL constants仍由代码拥有结构。

test-only AST Gate 扫描 `main.py`、`server.py`、`core/**`、`tools/**`、`ui/**` 与 `scripts/**`，并冻结 owner discovery、SQLite receiver 与 SQL sink 分类。新增直接 SQLite owner、未知 receiver、动态 statement、`executescript`、exception shape drift 或未解析 sink 都 fail closed。启动/内部只读检查所需的 schema-metadata PRAGMA 仅在精确 helper、固定 metadata 名称、无正文输入并对 `sqlite3.Error` fail closed 的形状下例外；该例外不授予通用 identifier interpolation authority。

Known Limitations：No generic SQL firewall 或 parser；No NL2SQL validator/feature；No SQL Tool。未来新增 SQL Tool、NL2SQL、直接 SQLite owner 或新数据库技术必须重新过 Gate。FTS query-language semantics（包括 `OR`、`NOT`、`NEAR` 与 malformed query）和 LIKE wildcard semantics（`%`、`_`）仍是搜索语义，不等于 statement structure authority。Chroma 的内部持久化不属于 LocalAgent direct SQL owner，本结论不认证其内部 schema/实现。用户可见错误保持固定非泄漏不代表 internal logs 已具备 generic DLP；日志仍受各自 safe projection 合同约束。本节记录实现与测试事实，不宣称 WP3-D Final Gate、WP3 aggregate 或 Stage 3 PASS。

## HTTP Payload Boundary（WP3-B）

| Surface | Frozen limit | Failure |
| --- | --- | --- |
| raw ASGI request body | `1,048,576` actual bytes | invalid/duplicate `Content-Length` -> 400；declared/actual over limit -> 413 |
| chat `query` / `file_path` / `agent_id` / `run_id` | `32,768` / `4,096` / `64` / `45` Python chars | FastAPI 422，service未调用 |
| search `keyword` | `1,024` Python chars | FastAPI 422 |
| history `limit` / `offset` | `1..100` / `0..100000` | FastAPI 422 |
| delete `message_ids` | 最多 `1,000`；每项 `1..2^63-1` | FastAPI 422，Memory未修改 |

raw-body Gate 在解析 JSON 前完整缓冲并按实际 bytes 计数；缺失、前导零、低报或等于上限的 `Content-Length` 都不能绕过实际计数。字段长度按 Python Unicode code point 计数，所以 astral 字符仍按一个 char 计。拒绝体是固定 JSON（400/413）或框架 validation detail（422），middleware 不记录 raw header/body。

## Trace Export Security Boundary（WP4-A）

WP4-A 公共 Trace export 是 metadata-first 的严格 allowlist 边界：

- 只有已完成且通过严格校验的内部 `SpanRecord` 可投影为不可变 `TraceExportEnvelope`；投影不写回 Runtime、不写 Journal、不发 RuntimeEvent、不影响 Tool/Output/Memory。
- `SAFE_SPAN_ATTRIBUTES`（内部最大安全记录集合）≠ 公共导出集合。公共导出只使用六类 operation/category 严格 schema；未知内部安全键默认省略，绝不自动导出。
- 公共导出禁止 raw：user input/body、system/agent instruction、planner response、model prompt/messages/output、Tool args/result、RAG query/chunk/citation、Memory/history/summary、filesystem/resource/DB path、provider URL、header/cookie/key、raw exception、traceback。
- 五个配置归因占位（`runtime_version`/`prompt_version`/`model_config_hash`/`toolset_hash`/`kb_version`，恒为 `not_configured`）不是真实版本归因，不进入公共导出；它们不是 Run Configuration Fingerprint。
- 兼容判断失败 content-free：固定 reason codes（`ACCEPTED`/`IDENTITY_MISSING`/`IDENTITY_MISMATCH`/`VERSION_UNSUPPORTED`/`FINGERPRINT_MISSING`/`FINGERPRINT_MALFORMED`/`FINGERPRINT_UNSUPPORTED`/`ENVELOPE_INVALID`），拒绝原因不携带 envelope/raw 值；已知 identity/version/fingerprint 但 envelope 语义非法时返回 `ENVELOPE_INVALID`。
- 该边界是严格 contract allowlisting，不是 generic DLP、generic secret scanner 或完整隐私保护。

## Trace Export Dispatch / Exporter Security Boundary（WP4-B）

WP4-B 在 WP4-A 合同之上实现 application-scoped 分发与 transport-neutral
adapter 边界，并保持同样的 content-free 原则：

- **外部 adapter 永不接收 raw `SpanRecord`**。所有 adapter 输入严格为
  `TraceExportEnvelope`（WP4-A 严格校验的不可变公共值）；raw attributes、
  OTel-shaped mapping、dict 或 JSON 字符串均被 protocol 与 dispatcher 路径禁止。
- WP4-A metadata-first 严格 allowlist 仍是强制要求：只有六类 category 导出
  schema 批准的键可以跨出观察边界；INTERNAL_ONLY 与未知键被省略，raw
  user/agent/model/tool/RAG/memory 正文、路径、URL、密钥与 raw exception
  永不导出。
- dispatcher error/health 路径 content-free：只保留固定 safe error code、
  bounded reason/stage、queue depth/capacity 与聚合计数；不含 span/envelope
  repr、attributes、IDs、fingerprint、endpoint、exception message 或 traceback。
- **Known Limitation（raw helper bypass risk）**：
  `OpenTelemetryCompatibleSpanAdapter.export_snapshot()` 可直接复制内部
  `SpanRecord.attributes` 到 OTel-shaped mapping，绕过 `TraceExportEnvelope`
  allowlist（internal-only marker 在该 snapshot 可见）。该 helper 当前未装配、
  无 transport，分类为 `KNOWN_LIMITATION + DESIGN_STOP_CONDITION`，**不是
  production incident**；它不是 WP4-B transport path，禁止被 production
  exporter 复用。
- exporter metrics 是 best-effort diagnostic projection；禁止高基数 label：
  `run_id`、`trace_id`、`span_id`、`step_id`、`fingerprint`、
  `contract_fingerprint`、`endpoint`、`url`、`raw_status`、`raw_exception`。
  fingerprint 是 contract identity，不是 metric dimension；drop reason 与
  failure stage 使用 dispatcher code-owned 有限词表。
- exporter health 不影响 `/readyz`/`/health`；disabled 无 degradation；export
  路径失败只反映在 dispatcher health/metrics/shutdown component truth，不改变
  Runtime 可用性。
- exporter 是侧通道 observability 能力：不写回 Run terminal status、
  AgentState、OutputGate/DeliveryStatus、Memory commit、Journal、Snapshot 或
  Recovery；Recovery 保持 validation-only，不读取 export queue。

## Sensitive Data Types

Prompt、Model output、Tool arguments/output、RAG chunks、Memory 正文、本地路径、Provider error/endpoint、API key/token、idempotency/resource key 均为敏感数据。Run id、session id、trace id、Tool name 在特定输出面也可能形成高基数或关联风险。

## Allowed Safe Facts

允许在对应 allowlist 合同中保存：小写 SHA-256 digest、固定 Enum、固定 Safe Error Code、计数、低基数状态、安全 UTC 时间戳、schema/version，以及经过明确字段白名单的安全布尔/分类事实。允许不等于所有输出面都可任意复制；每个 Event/Journal/Snapshot/Report/Metric/Span 仍受自己的 schema/label policy 限制。

## Prohibited Output Surfaces

敏感正文与原始异常不得进入 Runtime Event、Journal safe payload、RunSnapshot、Recovery/Shutdown/Fault/Release report、Health、Metric label、Span attribute、控制 Wire 或结构化日志。正式文档也不得出现真实用户绝对路径、公司内网地址、真实 Model path、密钥或真实 Provider URL。

边界例外必须准确说明：正常聊天 Wire 必然承载面向用户的 Model output；MemoryManager 有独立业务持久化路径；本地知识库/Vector Store 也保存其业务数据。这些业务面不因 Runtime 安全投影规则而消失，但不得被复制进 Journal/Health/Metric/Trace。Runtime 的 `OUTPUT_DELTA` compatibility wire 与业务 Memory 持久化各有独立 owner 和授权边界，不能笼统声称“任何正文绝不持久化”。

## Output-specific Rules

- Event/Journal：只接受事件类型的 allowlist payload；Tool result/identity 使用 digest 或安全 evidence。
- Snapshot/Recovery：仅安全状态投影、budget/activity/evidence；无原始 step result rehydration。
- Report/Health：固定 code、component/status、计数；不含路径、SQL、异常正文。
- Metric label：固定低基数 label；Tool name 必须在 `LOCAL_AGENT_METRICS_TOOL_NAME_ALLOWLIST`。
- Span attribute：固定 component/operation/status/safe code；不放 Prompt、output、path 或 key。
- Wire：业务输出只发给当前请求；控制错误为固定 safe code，disconnect 后停止写入。
- Log：结构化 allowlist；原始 Provider error、路径与 secret 禁止。

## Fault Injection Security

无生产激活入口，生产默认 `controller=None`。Rule ID 不进入业务输出；corruption 仅作用于测试副本；block/delay 必须有界且可取消；Controller 不决定 retry/recovery，也不拥有业务状态。不得新增 Settings、环境变量、HTTP、Prompt 或 Tool argument 激活路径。

## Configuration and Path Handling

密钥只通过受控 secret mechanism 注入，不提交到仓库。文档示例使用相对占位路径、回环 API 地址或 `<redacted>`；远程 Provider endpoint 不在正式结果/报告中回显。错误处理输出字段名和固定码，不输出配置值。

## Tool Governance Security（WP2-B）

`ToolGovernanceService` / `ToolPolicyCatalog` 只处理 Agent ID、canonical Tool name、固定枚举/code、risk classification 与内部 run/step scope。

```text
允许的 governance safe facts  = risk level（LOW/MEDIUM/HIGH）、risk fact enum、
                               outcome（ALLOW/DENY/APPROVAL_REQUIRED）、
                               固定 safe error code、固定中文 safe denial 文本
禁止进入任何 Event/Journal/Snapshot/Report/Metric/Span/log =
                               raw Tool arguments、path、prompt、Tool output、
                               policy allowlist（allowed Agent 集合）、approval evidence、
                               raw principal（denial 文本不得含执行 Agent ID）
```

- governance non-ALLOW 不产生 `TOOL_STARTED`/`TOOL_COMPLETED` 或任何 Tool evidence；denial 是直接安全 Wire 文本，不伪造 execution facts。
- `ToolPermission != filesystem/path authorization`；WP3-A 已实现 frozen workspace read-root/path containment，仍不等于 OS Sandbox 或 TOCTOU elimination。两个 Settings credential 的 `repr=False` 与 Provider safe projection 已覆盖，但 generic secret isolation / DLP 仍未实现。
- `ToolSideEffectKind.NONE` 不表示 permission-free；approval 不是 sandbox。approval evidence、durable pause/resume、human approval workflow 均未实现。
- **Known Limitation（Observability）**：WP2-B v1 不产生 dedicated governance RuntimeEvent / governance Journal fact；`DENY` / `APPROVAL_REQUIRED` 不会伪造 `TOOL_STARTED` / `TOOL_COMPLETED`（Tool 未执行）。rich governance observability 延后，不为此新增 RuntimeEvent / Journal schema。
