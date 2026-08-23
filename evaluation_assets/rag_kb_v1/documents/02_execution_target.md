# Evaluation Execution Target

## HTTP protocol
LocalAgentHttpExecutionTarget 对 evaluation-v2 使用 POST /api/runtime/evaluation-execute/v2。每个 Attempt 只发一次请求，不做自动 retry。

## Timeout ambiguity
传输超时发生在请求已发出之后时，执行结果可能是 OUTCOME_UNKNOWN，而不是确定 FAILURE。

## Evidence
成功响应可以同时携带 rag_evaluation_artifact 与 final_answer EvidenceRef，两类 evidence 独立保存。
