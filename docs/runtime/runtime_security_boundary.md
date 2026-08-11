# Runtime Security Boundary

## Stage 3 WP3 Resource / Deployment Boundary

`Tool Permission != Resource Authorization != Sandbox`。File Tool 的真实链为 `ToolRegistry -> ToolGovernanceService -> ResourceAuthorizationService -> ToolExecutionService -> Adapter -> Tool -> Windows filesystem/ACL`。`list_files` 与 `analyze_excel` 使用同一 frozen application-wide read roots；relative、drive-relative、UNC、device/extended path、outside/traversal/prefix collision、nonexistent、wrong type及resolved link escape均在业务I/O前拒绝。固定拒绝不包含path/root/OSError，不产生 `TOOL_STARTED` / `TOOL_COMPLETED`，不调用final-answer model；RuntimeEvent和Journal schema未扩展。

Wiki write不复用Tool read Authority；`WikiCrawler`校验remote `sn` 为单一Windows leaf，并对最终`.md/.pdf` candidate执行configured-output-root containment。拒绝只记录 `WIKI_REMOTE_FILENAME_INVALID` / `WIKI_OUTPUT_PATH_DENIED`，不记录raw metadata/path。

`Settings.remote_api_key` 与 `Settings.wiki_cookie` 使用 `repr=False`，只解决这两个credential的dataclass `repr/str` 暴露。PRODUCTION local API仅允许numeric loopback HTTP；这不是human authentication或inbound TLS。

Known Limitations：无authenticated human IAM、inbound TLS、request-size limit、full Sandbox/OS isolation、handle-based TOCTOU elimination、generic DLP、egress sandbox、approval evidence/HITL；authorized business content/path仍可进入正常Wire/Memory；UI/script raw logs与hardcoded Wiki endpoint仍是配置/日志债务；UNC不支持；real reparse测试可能受环境权限阻止；Resource contracts仍为INTERNAL_RC；Recovery仍validation-only、部署仍single-process Windows Native。

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
- `ToolPermission != filesystem/path authorization`；WP3 sandbox / workspace root / path traversal / secret isolation 仍未实现。
- `ToolSideEffectKind.NONE` 不表示 permission-free；approval 不是 sandbox。approval evidence、durable pause/resume、human approval workflow 均未实现。
- **Known Limitation（Observability）**：WP2-B v1 不产生 dedicated governance RuntimeEvent / governance Journal fact；`DENY` / `APPROVAL_REQUIRED` 不会伪造 `TOOL_STARTED` / `TOOL_COMPLETED`（Tool 未执行）。rich governance observability 延后，不为此新增 RuntimeEvent / Journal schema。

