# Retrieval Evaluation Metrics

## Recall
Recall@K 等于 top K 检索结果命中的 relevant chunk 数除以 relevant chunk 总数。它评价召回覆盖率。

## MRR
MRR 使用第一个 relevant item 的 rank 倒数。若首个相关项在第三位，RR 等于 1/3。

## NDCG
NDCG 使用 graded relevance、指数 gain 2^rel minus 1 与 log2 rank discount，同时评价相关性等级与排序位置。
