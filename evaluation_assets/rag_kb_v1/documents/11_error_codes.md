# Stable Error Codes

## Retrieval metadata
候选缺少 source 或 chunk identity 时使用 METADATA_INVALID，不能依靠正文猜测身份。

## Embedding asset
配置的本地 embedding 目录缺失时，稳定错误码是 EMBEDDING_MODEL_ASSET_INVALID，并在 adapter 加载前 fail fast。

## Query rewrite
Query rewrite 返回空字符串时使用 QUERY_REWRITE_EMPTY，并按既有 degradation contract 回退 original query。
