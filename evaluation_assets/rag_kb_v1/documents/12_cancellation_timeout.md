# Cancellation and Timeout

## Cancellation
asyncio.CancelledError 必须原样传播，不能转换成普通 evaluator failure 或伪造成功结果。

## Stage timeout
Retrieval 默认 stage timeout：Query Rewrite 8 秒、Embedding 10 秒、Retrieve 10 秒、Rerank 5 秒、Context Build 5 秒。

## Total timeout
RetrievalExecutionSpec 默认 total timeout 是 30 秒。总 deadline 与单 stage timeout 同时约束操作。
