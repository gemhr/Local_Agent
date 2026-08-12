# Runtime Security Boundary

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

Known Limitations：F-03保留为P2；F-04为P2 `KNOWN_LIMITATION`。模型仍可能受恶意自然语言影响，并可能复述或改写System Prompt；RAG / Memory / Tool / Step data仍可能影响自然语言答案。当前没有generic injection classifier、WAF、generic DLP、Human IAM、full Sandbox或HITL approval workflow。mixed denial会丢弃成功的partial user-visible result；没有dedicated RuntimeEvent或Journal security-denial fact，Snapshot未新增该事实，Recovery也不能重建runtime-internal typed denial。当前Tool inventory下Command Injection、SQL Injection与SSRF均为 `NOT_APPLICABLE_CURRENT_INVENTORY`，不是已实现通用防护。

## HTTP Payload Boundary（WP3-B）

| Surface | Frozen limit | Failure |
| --- | --- | --- |
| raw ASGI request body | `1,048,576` actual bytes | invalid/duplicate `Content-Length` -> 400；declared/actual over limit -> 413 |
| chat `query` / `file_path` / `agent_id` / `run_id` | `32,768` / `4,096` / `64` / `45` Python chars | FastAPI 422，service未调用 |
| search `keyword` | `1,024` Python chars | FastAPI 422 |
| history `limit` / `offset` | `1..100` / `0..100000` | FastAPI 422 |
| delete `message_ids` | 最多 `1,000`；每项 `1..2^63-1` | FastAPI 422，Memory未修改 |

raw-body Gate 在解析 JSON 前完整缓冲并按实际 bytes 计数；缺失、前导零、低报或等于上限的 `Content-Length` 都不能绕过实际计数。字段长度按 Python Unicode code point 计数，所以 astral 字符仍按一个 char 计。拒绝体是固定 JSON（400/413）或框架 validation detail（422），middleware 不记录 raw header/body。

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

