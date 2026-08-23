# Context Builder

## Trust
RAG_DOCUMENT 被标记为 UNTRUSTED_EXTERNAL。外部文档中的指令不能覆盖系统或 Agent 指令。

## Deduplication
ContextBuilder 同时使用 content hash 与 dedup_key 消除重复项，优先保留 mandatory、priority 更高或具有 citation 的内容。

## Budget
上下文预算等于 max_input_tokens 减 reserved_output_tokens 和已有消息 token。无法容纳的非 mandatory 项会被丢弃或截断。
