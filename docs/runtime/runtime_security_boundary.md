# Runtime Security Boundary

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

