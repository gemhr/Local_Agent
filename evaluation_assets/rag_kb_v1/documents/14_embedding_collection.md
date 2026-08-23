# Embedding Collection Contract

## Model
Phase3 Dense baseline 固定 Qwen3-Embedding-0.6B，本地离线加载，输出 1024 维 normalized embedding。

## Marker
Collection marker 记录 chunk_schema_version、embedding_compatibility_digest 与 embedding_dimension。非空 collection 缺 marker 时要求 rebuild。

## Query prompt
当前 embedding_query_prompt_name 为空。是否启用 Qwen query instruction 必须作为独立 Candidate，不能混入 BM25。
