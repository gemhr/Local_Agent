# Runtime Observability

## Trace
Trace 是执行观察证据，不是 Evaluation Dataset 或 AgentState 的 owner。Trace 投影不能修改 authority。

## Metrics
Metric label 只允许安全、低基数事实，不能写入 query、文档正文、工具参数或 secret。

## Journal
Runtime Event 使用 journal-first 发布语义。Journal 写入失败时不能假装事件已经可靠发布。
