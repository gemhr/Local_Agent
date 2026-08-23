# Current RAG Pipeline

## Retrieval channels
当前检索合并 rewritten query vector、original query vector 与 Chroma where_document $contains keyword supplement。Keyword channel 使用固定 heuristic score 0.55。

## Rerank
Heuristic rerank 组合 vector relevance、正文词面命中、metadata 命中、长度 bonus 与标题 penalty。它不是 Cross-Encoder。

## Selection
候选先按 max(rag_min_score, best_score minus 0.20) 过滤，再去重、截断并稳定选择 context。
