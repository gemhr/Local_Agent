# Memory Boundary

## Final answer
RunFinalMemoryWriter 每个 Run write-once，只有已经交付的 final answer 可以进入业务 Memory。

## Intermediate output
Specialist 原始中间结果和 synthesis 草稿不能直接写入最终聊天 Memory。

## Evaluation isolation
RAG Evaluation case 使用 fresh session 与 empty evaluation memory，不能读取历史 summary 或 FTS memory 改变答案。
