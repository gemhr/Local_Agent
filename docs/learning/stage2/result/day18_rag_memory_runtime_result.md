# 阶段二第 18 天改造结果

## 1. 本次目标

本次建立 RAG 与 Memory 的最小 Runtime Contract，并将 Knowledge Expert 的真实检索入口迁移到统一的 `RetrievalExecutionService`。范围覆盖 Query Rewrite、同步 Embedding、Chroma Vector Retrieve、既有启发式 Rerank、内容物化、Context Build、Budget、Timeout、Cancellation、Provenance、Citation Binding、Memory Trust Level 和 Controlled Degradation。

本次没有实现高级 Hybrid Search、Query Expansion、Multi-query Retrieval、新 Reranker/Cross Encoder、索引重建、AgentEvalOps、Tool Registry、Skill、MCP、A2A 或 Event Journal。

目标链路已经形成：

```text
Knowledge Expert
-> RetrievalExecutionService
-> QUERY_REWRITE
-> EMBEDDING
-> RETRIEVE
-> RERANK
-> DOCUMENT_LOAD
-> CONTEXT_BUILD
-> RetrievalExecutionResult / RetrievalExecutionError
-> Citation Binding
-> ContextBuilder
-> Model Context
```

## 2. 修改前 Retrieval Pipeline

修改前真实生产装配和调用链为：

```text
server.py lifespan
-> VectorDBManager(HuggingFaceEmbeddings + Chroma)
-> AgentRouter(db_manager=...)
-> Knowledge Expert _build_messages()
-> _build_rag_context()
-> _rewrite_knowledge_query() 调用现有 LLM
-> 对 rewritten query 与 original query 各调用 search_with_scores()
-> 失败时退回 search()，再次失败被吞为 []
-> keyword_search() 补充 Chroma where_document 精确术语召回
-> _score_rag_candidate() 词法/Metadata 启发式重排
-> 动态 relevance floor
-> 正文前 200 字符去重
-> rag_doc_max_chars / rag_context_max_chars 字符截断
-> 拼接 "[来源: ...]\n正文"
-> 单个 RAG_DOCUMENT ContextItem，固定 citation_id="local-kb"
-> ContextBuilder
-> 模型
```

审计结论：

| 检查项 | 修改前真实状态 |
| --- | --- |
| Query Rewrite | 存在；`_rewrite_knowledge_query()` 使用现有模型同步提取检索词。 |
| Embedding | 存在但藏在 LangChain Chroma `similarity_search_with_score()` 内；HuggingFace/Sentence Transformers 为同步调用。 |
| Vector Store | `VectorDBManager.search_with_scores()` -> `similarity_search_with_scores()` -> Chroma `similarity_search_with_score()`。 |
| 多路召回 | rewritten/original 两次向量查询，加既有 `keyword_search()`；本次只迁移，没有新增算法。 |
| 真实 Rerank | 存在；是 `_score_rag_candidate()` 的确定性词法、词频、Metadata、长度与标题惩罚启发式，不是模型 Reranker。 |
| Document Load | Chroma 已直接保存 Chunk 文本；查询返回 `Document.page_content`，没有二次文件读取。 |
| Chunk | `document_loader.split_documents()` 生成稳定 `chunk_id`、`chunk_index`、content hash、字符偏移、章节和页码 Metadata。 |
| Citation | 最终只给整块拼接后的 RAG 字符串分配固定 `local-kb`，没有 Chunk 级一一绑定、Context Hash 或来源强类型。 |
| Memory | SQLite 保存消息与 Rolling Summary；FTS5 搜索位于 `MemoryManager.search_messages()`。知识专家摘要已是 `USER_CONTENT`，一般回答和编排规划仍把摘要拼入 system prompt。 |
| 错误语义 | 向量查询、Fallback 和 Keyword 异常多处被吞成空列表，Embedding/Vector 失败可能被错误解释为“未找到”。 |

文档入库 Metadata 已包含 `doc_id`、`file_name`、`file_hash`、`source`、`source_type`、`chunk_id`、`chunk_index`、`content_hash`、页码、章节路径、字符偏移和 Parser/Chunker 版本。Source ID 的质量仍取决于这些 Metadata。

修改前及当前所有生产“未找到”相关文案位置：

- `core/agent_router.py::_build_system_prompt()`：知识专家约束“未找到对应信源”。
- `core/agent_router.py::_build_messages()`：仅在类型化 `EMPTY` 后抛出“未找到足够相关的本地知识库信源”。
- `core/agent_router.py::_build_synthesis_query()`：要求 Core Router 原样保留知识专家报告的“知识库不可用或未找到相关来源”。

`KnowledgeRetrievalFailedError` 的说明明确禁止把未完成检索写成“未找到”。

## 3. Retrieval Runtime Contract

新增：

```text
core/runtime/retrieval_contract.py
core/runtime/retrieval_context.py
core/runtime/retrieval_adapters.py
core/runtime/retrieval_execution.py
```

合约使用 frozen dataclass、Enum、严格数值校验和安全序列化。Query、Chunk 正文、完整向量、Memory 和原始异常不会进入 `to_safe_dict()` 或 Runtime Event。敏感正文字段使用 `repr=False`，降低对象被普通诊断输出时的泄漏风险。

## 4. RetrievalInvocation

`RetrievalInvocation` 包含：

- `retrieval_id`
- `original_query`
- `query_digest`
- `collection_names`
- `top_k`
- `rerank_top_k`
- `requested_timeout_seconds`
- `filters`

Query Digest 对折叠空白后的 Query 使用 SHA-256；同一逻辑重试可显式复用 `retrieval_id`。`top_k/rerank_top_k` 拒绝 bool 和非正整数；`filters` 递归校验 JSON-safe 并冻结，拒绝 NaN、Infinity、非字符串 Key 和任意对象。安全字典只输出 Query/Filter Digest、集合、计数和 Timeout，不输出正文。

## 5. RetrievalExecutionSpec

`RetrievalExecutionSpec` 配置并严格校验：

- Retrieval total timeout
- 六个 Stage timeout
- `max_candidates`
- `max_context_chunks`
- `max_context_chars`
- `max_single_chunk_chars`
- `max_document_reads`
- `allow_partial_document_load`

有效 Stage Timeout 为：

```text
min(stage timeout, retrieval remaining, run deadline remaining)
```

单 Chunk 上限不得大于总 Context 字符上限；最大 Context Chunk 不得大于最大文档读取数；Stage Timeout 必须完整覆盖全部 Stage。

## 6. RetrievalExecutionContext

`RetrievalExecutionContext` 持有：

- `RunContext`
- `step_id`
- `BudgetLedger`
- 可选 `StepEventEmitter`
- Retrieval monotonic deadline
- `RetrievalExecutionSpec`

方法为 `raise_if_cancelled()`、`remaining_seconds()` 和 `before_stage()`。它只检查 Run Cancellation、Run Deadline 与 Retrieval Deadline，不修改 RunStatus、StepStatus，也不取得或覆盖 `CancellationSource/Reason`。

## 7. Retrieval Stage

阶段：

```text
QUERY_REWRITE
EMBEDDING
RETRIEVE
RERANK
DOCUMENT_LOAD
CONTEXT_BUILD
```

状态：

```text
PENDING
RUNNING
SUCCEEDED
SKIPPED
FAILED
CANCELLED
TIMED_OUT
```

`RetrievalStageRecord` 只保存 Stage、状态、UTC 时间、duration、输入/输出数量、安全错误码、Retrieval Budget Usage、degraded，以及 `worker_terminated/execution_detached/background_work_pending` 生命周期事实。它不保存 Query、向量、Chunk 正文或原始异常。当前真实管线有启发式 Rerank，因此 RERANK 为真实执行；没有 Reranker 的 Adapter 明确记录 `SKIPPED`。

## 8. Query Rewrite

Knowledge Expert 继续复用 `_rewrite_knowledge_query()`，但真实模型调用不再使用旧 `_collect_model_response()/generate()` 直连。`RuntimeKnowledgeRetrievalAdapter` 注入同一个 `ModelInvocationRouter` 路径，依次复用 Model Selection/Router、Model Adapter、Budget、Circuit Breaker、RetryExecutor 和 MODEL_STARTED/MODEL_COMPLETED。调用目的仍是现有 Query Rewrite，没有建立第二套 Retry、Fallback、Circuit 或 Budget。

每次真实 Rewrite Attempt 消耗 `model_calls`、Token 与 Cost；Circuit Open 在 Adapter 前阻断，不收费；Transient Retry 仍由既有 RetryExecutor 决策，candidate/profile/breaker metadata 在 Retry 中稳定。Rewrite 使用 `max_tokens=128`、`temperature=0.1`、`enable_thinking=False`，这些生成选项通过统一 Invocation 传给既有 Generator Adapter。安全 Model/Retrieval Event 只含 Profile、candidate/retry index、breaker key、状态和 Digest，不含原 Query。

改写异常允许受控降级：

```text
QUERY_REWRITE FAILED
-> 保留安全错误码
-> 使用 original query
-> 后续 Stage 继续接受 Budget/Deadline/Cancellation 控制
-> 最终结果标记 DEGRADED（若有可用 Context）
```

只有普通可降级模型失败（例如 transient、rate limit、circuit open）会使用 original query。Stage/Provider Timeout、Run Deadline、Cancellation、Budget Exhausted、Safety/Validation 都不会降级继续；模型成功完成后才会进入 Embedding。

## 9. Embedding

`VectorDBManager.embed_query()` 显式调用既有 `HuggingFaceEmbeddings.embed_query()`，没有更换模型、Prompt、归一化配置或索引参数。输出由 `QueryEmbedding` 检查：

- 非空
- Dimension 大于零
- 所有元素为有限数
- 拒绝 NaN/Infinity/bool
- 记录 model ID、dimension、query digest 和 vector digest

完整向量只在进程内传给 Vector Store，不进入 Event 或安全字典。Embedding 是同步调用；同一 `VectorDBManager` 使用锁串行化 Query Embedding，Runtime 使用应用级 `BoundedBlockingExecutor` 控制运行与排队上限，因此串行锁前不会形成无限 Future 队列。

## 10. Vector Retrieve

当前锁定版本的 LangChain Chroma `similarity_search_by_vector_with_relevance_scores()` 名称虽含 relevance，实际直接返回 Chroma collection 的 raw distance。该事实由 `chroma_by_vector_score_semantics=RAW_DISTANCE` 显式声明；`VectorDBManager` 在公共边界恰好执行一次 `1 / (1 + max(0, distance))`，随后声明 `vector_score_semantics=NORMALIZED_RELEVANCE`。Runtime Adapter 只按显式语义转换：已归一化分数不再转换，确定性 Fake 的 raw distance 才在 Adapter 边界转换一次。最终分数夹在 `[0, 1]`，排序与 dynamic relevance floor 都使用这一个最终 relevance score。Metadata Filter 传给 Chroma；旧 `keyword_search()` 不支持 Metadata Filter，因此有 Filter 时安全跳过补召回。

候选 `RetrievalCandidate` 包含稳定 candidate/source/chunk identity、原始分数、original rank、冻结 Metadata、内容定位器和进程内文本句柄。Candidate ID 由 Source ID + Chunk ID 派生，不使用 Rank。Vector Store 异常返回 `VECTOR_STORE_FAILED/FAILED`；只有合法完成并返回零候选才是 `EMPTY`。

## 11. Rerank

项目存在真实启发式实现，因此 `_score_rag_candidate()` 被迁入 RERANK Stage。它综合向量分数、Query term 覆盖、词频、Metadata 命中、长度奖励和标题惩罚，并稳定排序。

Runtime 验证 Rerank：

- 候选 ID 集合完整且唯一
- Source ID、Chunk ID、original rank 未被修改
- 每项有真实 reranked score/rank
- reranked rank 连续且唯一
- 不允许清空全部候选

Rerank 失败时保留全部原候选与 original rank，记录 `RERANK_FAILED` 和 DEGRADED；不伪造 reranked score。没有实现 Cross Encoder、新模型 Reranker 或高级语义排序。

## 12. Document Load

当前 Chroma 查询已经返回 Chunk 文本，因此 `DOCUMENT_LOAD` 明确实现为 **content materialization**，没有从 Metadata 任意读取文件或访问网络。

每个候选物化前检查 Cancellation、Run/Retrieval Deadline 和 `document_reads` 预算。`MaterializedDocument` 计算并校验原文 SHA-256。单个读取失败在配置允许时使用成功来源并标记部分降级；全部失败返回 `DOCUMENT_LOAD_ALL_FAILED/FAILED`。

## 13. Source Metadata

`SourceMetadata` 包含：

- `source_id`
- `source_type`
- `collection`
- `canonical_uri`
- `display_name`
- `document_version`
- `page`
- `section_path`
- `chunk_id`
- `chunk_index`

优先使用入库 `doc_id`；缺失时使用 collection + canonical source 计算稳定 Source ID。Document version 优先使用 `file_hash/document_version/content_hash`。Source ID 不依赖 Query 或 Rank；Chunk ID 来自入库 Metadata，不使用 Rank。缺少 Source/Chunk 身份、页码或 Chunk Index 非法时返回 `METADATA_INVALID`。安全字典不输出 canonical path。

## 14. Provenance

`RetrievalProvenance` 记录：

- Source/Chunk ID
- original/reranked rank
- retrieval score
- transformations
- original content hash
- context content hash

Transformation 支持：

```text
LOADED
NORMALIZED
DEDUPLICATED
TRUNCATED
RERANKED
CONTEXT_SELECTED
```

精确正文去重发生后，保留的 Winner 记录 `DEDUPLICATED`；被删除候选不生成 Citation。截断后重新计算 Context Hash，合约拒绝 `TRUNCATED` 却仍声称原文 Hash 等于 Context Hash。

## 15. Citation Binding

Citation 只在 Context normalize、deduplicate、truncate 和 select 全部完成后生成。`CitationBinding` 包含 Citation ID、Source/Chunk ID、Context Block ID、Context content hash、显示标签、页码和章节路径。

`RetrievalExecutionResult` 强制：

- 每个最终 Chunk 有且只有一个 Citation
- Citation 顺序与最终 Chunk 完全一致
- Citation ID 不重复
- Source/Chunk/Context Block/Hash 与最终 Chunk 匹配
- 被过滤、去重或未选中的候选没有孤儿 Citation

Citation ID 在本次 Result 内按稳定输出顺序生成；Source ID 跨检索稳定。

`context_content_hash` 精确绑定 Retrieval normalize/truncate/select 后的 `RetrievedChunk.text` Payload，不包含 `[来源]`、`[引用]`、章节标题或不可信数据提示等固定包装。Knowledge Expert 将该 Payload 以 `preserve_content=True + payload_content_hash` 放入 `ContextItem`；ContextBuilder 对它不再 strip、折叠换行或截断，只添加固定包装。多行、Unicode、Tab、连续空行和尾随空白均按 Payload 原样进入最终模型消息。

## 16. Context Build

统一步骤为：

```text
候选
-> Rerank
-> Materialize
-> Validate
-> 精确 Deduplicate
-> Truncate
-> Stable Select
-> Citation Binding
-> RetrievedChunk
-> ContextBuilder
```

Runtime 同时执行最大 Chunk、总字符、单 Chunk、读取数和 Context chars Budget 限制。Knowledge Expert 把每个 `RetrievedChunk` 作为独立 `RAG_DOCUMENT + UNTRUSTED_EXTERNAL` Context Item，使用最终 Citation ID；它们被标为 mandatory 且正文不可变，防止 Model Context 再次静默截断/删除后留下错误 Citation。若最终模型窗口无法完整容纳 mandatory Retrieval Payload，AgentRouter 返回类型化 `CONTEXT_BUILD_FAILED/FAILED`，清空最终 Chunk/Citation，不会降级为 EMPTY。Knowledge Expert 路径会延迟唯一的 CONTEXT_BUILD Stage 与 Retrieval Completed Event，直到该最终绑定完成，因此溢出事件同样是 FAILED，不会先发布成功再补发失败。知识库中的指令文本只能作为不可信数据，不能覆盖 System/Agent 指令。

## 17. Retrieval Result

不可变 `RetrievalExecutionResult` 包含：

- Retrieval ID
- `SUCCEEDED/EMPTY/DEGRADED/FAILED/CANCELLED/TIMED_OUT`
- rewritten query digest
- final chunks
- citations
- stage records
- degraded 与原因
- budget usage
- started/completed/duration
- 失败时的安全 `RetrievalExecutionError`

`to_safe_dict()` 只输出 Digest、状态、数量、阶段、预算、时间和安全错误，不输出正文。`rendered_context` 仅是旧测试/兼容调用方的字符串视图；Knowledge Expert 主路径直接消费强类型 Chunk。

## 18. Retrieval Error

错误分类：

```text
VALIDATION
QUERY_REWRITE_FAILED
EMBEDDING_FAILED
VECTOR_STORE_FAILED
DOCUMENT_LOAD_FAILED
RERANK_FAILED
CONTEXT_BUILD_FAILED
METADATA_INVALID
BUDGET_EXHAUSTED
TIMEOUT
CANCELLED
DEADLINE_EXCEEDED
INTERNAL
```

Error 只包含 category、安全错误码、安全中文说明、可选 Stage 和失败来源数；不包含 Query、Chunk、Memory、Embedding、原始异常或 Traceback。

## 19. Retrieval Budget

复用现有 `BudgetLedger`，扩展 `RunBudget/BudgetUsage`：

- `retrieval_calls`
- `embedding_calls`
- `vector_queries`
- `keyword_queries`
- `document_reads`
- `context_chars`

Query Rewrite 的每个真实 Model Attempt 使用原有 `model_calls/input_tokens/output_tokens/total_tokens/cost_units/retries`，不复制到 Retrieval 维度。Embedding、Vector、Keyword 与 Document Materialization 每个真实调用分别计数；rewritten/original 相同会先去重，只收费一次。

真实调用前原子 reserve；同步 Worker 真正进入 Provider wrapper 时 commit；未取得 Admission、排队 Future 被取消、或 Provider wrapper 尚未开始时 release。Worker 已开始后即使 Runtime 超时并 detached 也不退款。Context Build 预留预计字符并以实际最终字符原子结算：实际更小自动退款，实际更大必须在同一把锁内补差并重新校验，任何有限预算都不能被实际值突破。并发 Reservation 继续由单 Run Ledger 原子保护。预算不足时 Stage 不执行，返回 `BUDGET_EXHAUSTED/FAILED`，不会返回 EMPTY。

## 20. Timeout / Cancellation

检查点包括：

- Retrieval 开始前
- 每个 Stage 前后
- 每次 Embedding 前后
- 每次 Vector Query 前后
- 每次 Document Materialization 前后
- Context Build 前后
- 最终 Result 返回前

同步调用由应用级 `BoundedBlockingExecutor` 承载，默认 `max_workers=4`、`max_pending_tasks=8`。Admission Semaphore 的总准入容量为 12，但 ThreadPool 内最多只有 8 个 pending Future；未取得 Permit 不得 submit。Admission 等待和 Future 等待都每 50ms 检查 Run Cancellation、Run Deadline、Retrieval/Stage Deadline。

未开始的排队 Future 取消后不会执行，并释放 Budget 与 Admission Permit。已经进入 Sentence Transformer、Chroma 或模型 Adapter 的 Worker 不能被 Python 安全强杀；Stage Timeout 表示 Runtime 停止等待，而不是 Worker 已终止，此时 Stage 与 Retrieval Completed 明确记录：

```text
worker_terminated = false
execution_detached = true
background_work_pending = true
```

迟到结果不会进入 Context、不会修改已完成 Result、不会重跑 Stage，也不会发布第二个 Retrieval Stage Completed；Worker 真正结束后只从 Tracker 注销并释放 Permit。Tracker 独立于 RunRegistry，只保存 task ID、kind、run ID、operation ID 和时间/状态，不保存 Query、Embedding、Chunk 或原始异常。`wait_until_idle(timeout)` 与 `shutdown(wait=True, timeout=...)` 可以有界等待真实 Worker 清理。

## 21. Controlled Degradation

允许且显式记录：

- Query Rewrite 失败 -> 使用原 Query
- Rerank 失败 -> 保留原始排序和完整候选
- 部分 Document Load 失败 -> 只使用成功来源

Result 记录 `degraded=True`、原因、失败 Stage Record、部分失败数量和最终 Citation 数。以下情况不降级：

- Embedding 失败
- Vector Store 失败
- 全部 Document Load 失败
- Metadata 无法绑定
- Citation/Context 不变量失败
- Budget 不足
- Cancellation
- Deadline/Timeout

## 22. Memory Runtime Boundary

本次没有重写 SQLite/FTS5 Memory Store。新增 `MemoryProvenance` 与 `MemoryContextRecord`，为 Rolling Summary、Phase Summary 或 FTS5 Retrieved Memory 提供独立 `memory_id/memory_type/record_id`，且不冒充 RAG `SourceMetadata`。

Memory：

- 不生成或复用 RAG Citation
- 不使用 RAG SourceMetadata
- 检索失败状态与 RAG 独立
- 内容中的指令不能覆盖系统策略
- 当前 FTS5 `MemoryManager.search_messages()` 仍是 UI/Service 的独立搜索入口，没有被强行合并到 RAG Pipeline

## 23. Memory Trust Level

`ContextItem` 现在强制 `MEMORY_SUMMARY/MEMORY_RETRIEVAL/CHAT_HISTORY` 只能使用 `USER_CONTENT`，并拒绝 Citation。Knowledge Expert、一般 Agent `_build_messages()` 和 `_build_orchestration_messages()` 都不再把 Rolling Summary 拼入 system prompt，而是通过 ContextBuilder 的 `Relevant Memory` 数据区进入模型输入。

结果：Rolling Summary、未来 Phase Summary 和 FTS5 Retrieved Memory 的 Context Contract 都不能升级为 `TRUSTED_INSTRUCTION/SYSTEM/DEVELOPER`。

## 24. Runtime Event

新增强类型事件：

```text
RETRIEVAL_STARTED
RETRIEVAL_STAGE_COMPLETED
RETRIEVAL_COMPLETED
```

Payload 为 `RetrievalStartedPayload`、`RetrievalStageCompletedPayload`、`RetrievalCompletedPayload` 和嵌套的 `RetrievalBudgetPayload`。只包含 Retrieval ID、Query Digest、状态、Stage、计数、duration、degraded、Citation 数、预算、安全错误码，以及真实 Worker 的 terminated/detached/background pending 状态。

事件不包含 Query、Chunk、Memory、Prompt、Embedding、原始异常或 Traceback。事件发布失败返回安全 Internal Failure，已经执行的 Stage 不会透明重跑。Coordinated Knowledge Expert 路径可发布事件；默认 Legacy 文本流没有 StepEventEmitter，因此不会发布 Retrieval Event。

## 25. Knowledge Expert 真实迁移

真实入口已经迁移：

```text
AgentRouter._build_messages(agent_id="knowledge_expert")
-> _execute_knowledge_retrieval()
-> RetrievalExecutionService.execute()
-> RetrievalExecutionResult
-> 每个 RetrievedChunk 转成独立 ContextItem
-> ContextBuilder
-> Model Context
```

`_prepare_answer_messages()` 继续把 RunContext 和 StepEventEmitter 传入该路径。`_build_rag_context()` 只保留为旧测试/兼容调用方的字符串视图，内部同样调用 Runtime Service，不再拥有第二套检索算法。

验证覆盖成功、EMPTY、Embedding 失败、Vector 失败、部分降级、Citation、Memory、Cancellation、Budget 和 Timeout。RAG FAILED 不再统一变成“未找到”。

## 26. EMPTY / FAILED 区分

| 状态 | 语义 | 上层文案边界 |
| --- | --- | --- |
| `EMPTY` | 所有必要 Stage 合法完成后没有候选，或合法过滤/去重后为空。 | “知识库中未检索到相关资料。” |
| `FAILED` | Retrieval 未完成，不能判断资料是否存在。 | 报告检索失败，不得写“未找到”。 |
| `DEGRADED` | 使用原 Query/原排序/成功物化的部分来源完成。 | 明确只使用成功部分。 |
| `CANCELLED` | Run Token 已取消。 | 报告取消并停止读取。 |
| `TIMED_OUT` | Run/Retrieval/Stage Deadline 到期。 | 报告超时，不得写“无资料”。 |

AgentRouter 仅把 `EMPTY` 映射为 `KnowledgeSourceNotFoundError`；FAILED/TIMED_OUT 使用 `KnowledgeRetrievalFailedError`；CANCELLED 恢复为统一 `RunCancelledError`。

## 27. Legacy 与未迁移路径

已迁移：

- Knowledge Expert `_build_messages()` 真实主入口
- 兼容 `_build_rag_context()`（内部也走 Runtime）
- 既有模型 Query Rewrite
- HuggingFace Query Embedding
- Chroma Vector Query
- 既有 Keyword 补召回
- 既有启发式 Rerank
- Chroma 内容物化
- RAG Context/Citation
- Rolling Summary 的 Context Trust 边界

仍为 Legacy 或独立入口：

- `scripts/query_local_kb.py` 直接调用 `VectorDBManager.search_with_scores()`，属于运维查询 CLI，不经过 Runtime。
- `VectorDBManager.search()/search_with_scores()/similarity_search*()` 公共兼容 API 仍可被测试或脚本直接调用。
- 默认 `ChatService.stream_chat()` 使用 Legacy AgentLoop，虽然 Knowledge Expert 检索本身已走 Runtime，但没有 StepEventEmitter，因此没有 Retrieval Event。
- `MemoryManager.search_messages()` 的 FTS5 UI 搜索仍是独立 Memory 能力，没有接入 RAG Citation 或 Result。
- Tool Result 仍有旧 system content 注入风险，不属于本日 Retrieval/Memory 改造范围。

## 28. 重点 Bad Case

### Bad Case 1：Embedding 异常报告为无结果

- 类型：真实发现。
- 触发条件：旧 `search_with_scores()` 内部 Embedding/Chroma 抛错，Fallback 也失败。
- 故障表现：异常被吞为 `[]`，最终显示“未找到”。
- 根因分析：调用完成状态与候选数量共用空列表表达。
- 修复方案：显式 EMBEDDING/RETRIEVE Stage 与 `EMBEDDING_FAILED/VECTOR_STORE_FAILED`。
- 回归测试：`test_embedding_and_vector_failures_are_not_reported_as_empty`、`test_knowledge_expert_distinguishes_empty_from_embedding_failure`。
- 对应知识点：失败语义、fail closed。
- 面试表达：只有成功执行后的零候选才是 EMPTY，执行失败不能证明数据不存在。
- 当前状态：已修复。

### Bad Case 2：Citation 在 Rerank 前分配

- 类型：假设构造；旧实现没有真正 Chunk Citation，只有固定 `local-kb`。
- 触发条件：候选一取回就生成 Citation，随后 Rerank/过滤/去重。
- 故障表现：被删除候选留下孤儿 Citation，排序与编号不对应。
- 根因分析：Citation 生命周期早于最终 Context 生命周期。
- 修复方案：只在 Context Build 最终 Select 后一一 Binding。
- 回归测试：`test_deduplicated_candidate_has_no_orphan_citation`、`test_result_rejects_orphan_or_reordered_citation`。
- 对应知识点：引用完整性、派生数据生命周期。
- 面试表达：Citation 是最终上下文块的派生物，不是召回候选的属性。
- 当前状态：已防护。

### Bad Case 3：Memory Summary 升级为 System

- 类型：真实发现。
- 触发条件：一般回答或编排规划存在 Rolling Summary。
- 故障表现：摘要被拼入 system prompt，摘要中的指令获得高权限。
- 根因分析：历史兼容拼接没有显式 Trust Level。
- 修复方案：全部摘要改为 `MEMORY_SUMMARY + USER_CONTENT` Context Item。
- 回归测试：`test_memory_context_boundary_is_user_content_and_has_no_rag_citation`、既有 `test_knowledge_summary_is_untrusted_relevant_memory_not_system_message`。
- 对应知识点：Prompt Injection、信任分层。
- 面试表达：Memory 是压缩后的用户数据，不因“摘要”二字获得系统权限。
- 当前状态：已修复。

### Bad Case 4：Source ID 不稳定

- 类型：真实风险。
- 触发条件：Metadata 缺少 `doc_id/source/chunk_id`，调用方用 Rank 或 Query 生成身份。
- 故障表现：同一文档跨检索 Source/Citation 身份变化。
- 根因分析：检索时身份与本次排序耦合。
- 修复方案：优先入库 doc/chunk identity，Source fallback 只依赖 collection + canonical source，关键字段缺失失败。
- 回归测试：`test_source_metadata_has_stable_identity_separate_from_rank`。
- 对应知识点：稳定标识、可追溯性。
- 面试表达：Source/Chunk 身份来自数据生命周期，Rank 只属于本次 Retrieval。
- 当前状态：已防护；仍依赖 Metadata 质量。

### Bad Case 5：截断后 Hash 未更新

- 类型：真实旧缺口。
- 触发条件：旧 RAG 按字符截断 Chunk。
- 故障表现：没有原文/Context Hash，无法证明 Citation 引用的是哪段最终文本。
- 根因分析：截断只是字符串操作，没有 Provenance Contract。
- 修复方案：截断记录 `TRUNCATED` 并重新计算 Context Hash；合约拒绝相同 Hash。
- 回归测试：`test_truncation_requires_new_context_hash`。
- 对应知识点：内容寻址、派生数据完整性。
- 面试表达：任何内容变换都必须改变对应内容摘要并进入来源链。
- 当前状态：已修复。

### Bad Case 6：取消后继续读取

- 类型：真实旧边界。
- 触发条件：Run 在同步 RAG 查询或候选循环中取消。
- 故障表现：旧 `_build_rag_context()` 没有 RunContext 检查，继续查询或拼接。
- 根因分析：RAG 不在 Runtime Cancellation Contract 内。
- 修复方案：Stage 前后、每次 Embedding/Vector/Document Read 和最终返回前检查 Token。
- 回归测试：`test_budget_cancellation_and_timeout_have_distinct_statuses`。
- 对应知识点：协作式取消、安全点。
- 面试表达：同步依赖不能强杀，但 Runtime 必须停止启动新工作并拒绝消费迟到结果。
- 当前状态：Runtime 已防护；已进入的同步线程仍有不可抢占限制。

### Bad Case 7：Rerank 失败清空候选

- 类型：假设构造。
- 触发条件：Reranker 异常或返回空/缺失候选。
- 故障表现：本可用的向量候选变成 EMPTY。
- 根因分析：降级没有保持候选完整性。
- 修复方案：验证候选集合与身份；失败回退 original rank，记录 DEGRADED。
- 回归测试：`test_query_rewrite_and_rerank_failures_are_controlled_degradation`。
- 对应知识点：受控降级、不变量校验。
- 面试表达：可选优化失败不能抹掉已成功的基础召回。
- 当前状态：已防护。

### Bad Case 8：预算不足报告无资料

- 类型：假设构造。
- 触发条件：Embedding/Vector/Read 前 reserve 失败。
- 故障表现：Stage 未执行却返回 EMPTY。
- 根因分析：Budget Exhausted 与零候选共用结果。
- 修复方案：返回 `BUDGET_EXHAUSTED/FAILED`，不调用 Provider。
- 回归测试：`test_budget_cancellation_and_timeout_have_distinct_statuses`。
- 对应知识点：授权与业务结果分离。
- 面试表达：没有预算执行查询，就没有证据判断知识库是否为空。
- 当前状态：已防护。

### Bad Case 9：知识库 Prompt Injection 获得指令权

- 类型：真实安全风险；Day 6 已部分防护，本次细化到 Chunk。
- 触发条件：知识库正文包含“忽略系统指令”。
- 故障表现：RAG 数据被当作可信规则。
- 根因分析：来源和信任等级没有强制绑定。
- 修复方案：RetrievedChunk 只允许 `UNTRUSTED_EXTERNAL`，ContextBuilder 在外部数据区渲染固定边界提示。
- 回归测试：`test_chunk_rejects_wrong_citation_hash_or_elevated_trust`、既有 `test_external_rendering_is_data_section`。
- 对应知识点：数据/指令隔离。
- 面试表达：检索相关性不等于指令可信度。
- 当前状态：已防护。

### Bad Case 10：去重后保留孤儿 Citation

- 类型：假设构造。
- 触发条件：两个候选正文相同，先分配 Citation 再去重。
- 故障表现：Result 有两个 Citation、只有一个 Context Chunk。
- 根因分析：Citation 与候选而非最终 Chunk 绑定。
- 修复方案：去重先于 Citation；Result 强制一一对应和顺序一致。
- 回归测试：`test_deduplicated_candidate_has_no_orphan_citation`。
- 对应知识点：引用基数、不变量。
- 面试表达：引用集合应从最终 Context 映射生成，不能独立维护。
- 当前状态：已防护。

### Bad Case 11：部分读取失败声称完整成功

- 类型：真实旧风险。
- 触发条件：旧双路查询或 Keyword 补召回部分异常。
- 故障表现：异常被吞，仍按正常成功输出，用户不知道来源不完整。
- 根因分析：没有 degradation 字段和失败来源计数。
- 修复方案：部分 Document Load 返回 DEGRADED、原因和失败数量；全失败为 FAILED。
- 回归测试：`test_partial_document_failure_degrades_but_all_failed_is_fatal`。
- 对应知识点：质量披露、部分成功。
- 面试表达：能继续回答不代表可以声称完整成功。
- 当前状态：已修复。

### Bad Case 12：Memory 与 RAG 共用 Citation

- 类型：真实 Contract 缺口；旧 `ContextItem` 曾允许 Memory 设置 citation_id。
- 触发条件：调用方给 MEMORY_RETRIEVAL 分配 RAG Citation。
- 故障表现：用户无法区分会话记忆和知识库信源，Memory 冒充可外部追溯文档。
- 根因分析：ContextItem 没有按 Source Type 限制 Citation。
- 修复方案：Memory Context 强制 USER_CONTENT 并拒绝 Citation，使用独立 MemoryProvenance。
- 回归测试：`test_memory_context_boundary_is_user_content_and_has_no_rag_citation`。
- 对应知识点：来源域隔离。
- 面试表达：Memory provenance 解释“来自哪段会话”，RAG Citation 解释“来自哪份文档”，两者不能混用。
- 当前状态：已修复。

### Bad Case 13：Query Rewrite 绕过统一模型运行时

- 类型：本次补查真实发现。
- 故障表现：旧 `_collect_model_response()` 直接调用 `generate()`，Rewrite 不受统一 Model Budget、Circuit、Retry 与 Model Event 约束。
- 修复方案：注入并复用 `ModelInvocationRouter`；删除 Rewrite 的旧直连执行路径。
- 回归测试：统一 Rewrite 的 Event/预算顺序、Circuit Open、Budget 拒绝、Retry metadata、普通失败降级与 Cancel/Timeout/Safety fail closed。
- 当前状态：已修复。

### Bad Case 14：`max_workers` 被误当成有界提交队列

- 类型：本次补查真实发现。
- 故障表现：标准 ThreadPoolExecutor 虽限制运行线程，但内部 pending queue 可无限增长。
- 修复方案：应用级 Admission Semaphore 同时限制 active 与 pending；独立 Tracker 管理 pending/running/detached。
- 回归测试：`max_workers=1/max_pending=1` 第三个任务等待准入、等待时取消、排队 Future 永不执行、detached 清理与 idle/shutdown。
- 当前状态：已修复。

### Bad Case 15：Citation Hash 与最终模型正文不一致

- 类型：本次补查真实风险。
- 故障表现：Retrieval 生成 Hash 后，ContextBuilder 仍可能 strip 或折叠换行，Hash 对应的文本不是模型实际收到的正文。
- 修复方案：采用方案 A；`RetrievedChunk.text` 是不可变 Payload，ContextBuilder 只添加固定包装，Hash 不包含包装。
- 回归测试：多行/Unicode/Tab/连续空行/尾随空白保持一致，mandatory overflow 返回 `CONTEXT_BUILD_FAILED`。
- 当前状态：已修复。

### Bad Case 16：方法名导致 Vector Score 二次归一化

- 类型：本次补查真实风险。
- 故障表现：底层 API 名称含 relevance，但当前版本实际返回 raw distance；缺少显式语义时容易重复或漏做 `1/(1+d)`。
- 修复方案：引入 `RAW_DISTANCE/NORMALIZED_RELEVANCE`；每个边界声明输入语义，只转换一次。
- 回归测试：已知 raw distance 与 relevance Fake、合法范围、排序和 dynamic floor。
- 当前状态：已修复。

### Bad Case 17：预算计划值与真实调用数不一致

- 类型：本次补查真实发现。
- 故障表现：Keyword 没有独立维度，失败阶段的 Result 可能只记录计划数；实际值大于 Reservation 时曾可突破上限。
- 修复方案：新增 `keyword_queries`，Result 从 Ledger delta 读取真实已提交调用；commit 原子补差/退款并拒绝越限。
- 回归测试：同 Query 去重、Keyword 零预算、排队取消不收费、运行超时不退款、Context chars 原子补差、并发 Reservation。
- 当前状态：已修复。

### Bad Case 18：用户主动停止被 ASGI 误报为未处理异常

- 类型：真实故障；Legacy Chat Stream 的 Domain Cancellation 与 HTTP Transport 边界不完整。
- 触发条件：桌面端正在读取 `/api/chat` 流式响应时，用户点击“停止”；客户端使用同一个 `run_id` 调用 `/api/runtime/runs/{run_id}/cancel`，服务端成功写入 `CancellationReason.USER_CANCELLED`。
- 故障表现：取消接口先返回 `200 OK`，生成链路随后在合作式安全点抛出 `RunCancelledError("USER_CANCELLED")`；聊天流没有把该异常转换为正常终止，Uvicorn 最终记录 `Exception in ASGI application` 和完整 Traceback。表象像服务端生成失败，但取消请求实际已经成功。
- 异常传播链：`cancel_run_endpoint()` -> `RunRegistry.cancel()` -> `CancellationToken.raise_if_cancelled()` -> `RunContext.raise_if_inactive()` -> `AgentRouter`/`AgentLoop` -> `ChatService.stream_chat()` -> `asyncio.to_thread(_next_or_none, stream)` -> `StreamingResponse` -> Starlette/ASGI。
- 根因分析：`RunCancelledError` 是项目定义的受控业务终止异常，继承自 `RuntimeError`，并不属于 `asyncio.CancelledError`。`AgentLoop` 捕获它后会先把 Step/Run 结算为 `CANCELLED`，再重新抛出以通知上层停止推进；但 `server.py::chat_endpoint.generate()` 只处理了 `asyncio.CancelledError`、`BrokenPipeError` 和 `ConnectionResetError`，遗漏了 `RunCancelledError`。响应头已经发送后，该异常无法再转换为新的 HTTP 错误响应，只能穿透流式响应并被 ASGI 当成应用异常。
- 影响边界：Run/Step 状态仍会正确落为 `CANCELLED`，`ChatService` 的 `finally` 仍会注销 Run、取消 Deadline Timer 并关闭同步流，因此没有证据表明本案例造成 RunRegistry 泄漏或错误终态；真正错误的是 Transport 把预期取消记录成服务器故障，并以异常方式截断响应流。
- 修复方案：在 `server.py::chat_endpoint.generate()` 的流式传输边界显式捕获 `RunCancelledError` 并直接结束生成器。状态结算继续由 `AgentLoop` 负责，资源回收继续由 `ChatService/server` 的 `finally` 负责；不恢复旧版 `except Exception`，保证普通 `RuntimeError` 等真实故障仍然穿透并可观测。
- 回归测试：新增 `tests/test_server_stream_cancellation.py::test_chat_stream_treats_run_cancellation_as_normal_completion`，验证流先输出部分内容、随后发生 `USER_CANCELLED` 时能够正常结束且底层同步流被关闭；新增 `test_chat_stream_does_not_hide_unexpected_errors`，验证非取消 `RuntimeError` 仍向上传播。定向回归为 `40 passed, 3 subtests passed`，当前全仓回归为 `443 passed, 42 subtests passed`。
- 对应知识点：Domain Cancellation 与 Task Cancellation 的类型区分、StreamingResponse 已发送响应头后的异常语义、状态结算与传输适配分层、合作式取消、安全清理、负向测试防止过度吞异常。
- 面试表达：取消不是失败，也不是所有层都应吞掉异常。Runtime 层先用类型化异常完成 `CANCELLED` 结算并通知停止，Transport 层再把该受控终态映射为流的正常结束；同时用反向测试保证真正的未知异常不会被取消处理器隐藏。
- 当前状态：已修复并通过全仓回归。

### Bad Case 19：Worker 已停止但发送按钮永久禁用

- 类型：真实前端状态机故障；异步 Worker 生命周期与 UI Streaming 状态没有统一终态。
- 触发条件：桌面端正在通过 `requests.Response.iter_content()` 读取聊天流时，用户第一次点击“停止”。取消线程先请求服务端取消，再调用 `ApiWorker.cancel()` 设置 Qt interruption flag，并从另一个线程关闭当前 Response 和 Session。
- 故障表现：后端正常停止且不再打印异常，`ApiWorker` 线程也已经退出，但停止按钮仍保持启用，发送按钮即使在输入新内容后也不会重新启用；切换智能体或聊天界面同样无法恢复，只能重启应用。
- 异常传播链：`ChatPanel.stop_requested` -> `MainController._handle_stop_request()` -> 独立 `cancel_then_close` 线程 -> `request_run_cancellation()` -> `ApiWorker.cancel()` -> `Response.close()` -> `iter_content()` 抛出连接读取异常 -> `ApiWorker.run()` 的 `except Exception`。
- 根因分析：主动取消造成的读取异常进入 `except Exception` 后，代码发现 `isInterruptionRequested() == True`，因此正确地不发送 `error_signal`；但自定义 `finished_signal` 只位于 `try` 的正常完成路径，异常路径也不会发送它。按钮复位和 `run_id` 清理只挂在 `_on_worker_finished()` 与 `_on_worker_error()` 上，两个 Handler 都没有被调用，最终形成“线程真实状态已结束、UI 状态仍为 Streaming”的状态分裂。
- 禁用机制：`ChatPanel._on_input_changed()` 使用 `not self.stop_btn.isEnabled() and bool(input or attachment)` 决定发送按钮状态。遗留的停止按钮一直为 Enabled，使条件第一项永久为 False；智能体切换只更新 `current_agent_id`、聊天 Stack 和历史加载，不负责修改这个全局 Streaming 状态，因此切换界面无法自愈。遗留的 `worker.run_id` 也不会被清空，而再次点击停止会因 `worker.isRunning() == False` 直接返回。
- 修复方案：将“业务成功”“真实错误”和“Worker 生命周期结束”拆成三个信号语义。`finished_signal` 只负责正常完成后的最终渲染，`error_signal` 只负责显示真实错误，新增 `settled_signal` 并在 `ApiWorker.run()` 的 `finally` 中无条件且仅一次发出；`MainController._on_worker_settled()` 统一执行 `set_streaming(False)` 和清空 `worker.run_id`。这样用户取消不会被伪装成成功或错误，而所有退出路径都能恢复 UI。
- 回归测试：新增 `tests/test_api_worker.py`。`test_interrupted_worker_emits_settled_without_success_or_error` 验证取消读取异常只发送 `settled`；`test_completed_worker_emits_success_before_settled` 验证成功顺序为 `finished -> settled`；`test_failed_worker_emits_error_before_settled` 验证真实故障顺序为 `error -> settled`；`test_worker_settled_resets_shared_streaming_state_and_run_id` 验证统一 Handler 关闭 Streaming 状态并清空 Run ID。定向回归为 `7 passed`，当前全仓回归为 `447 passed, 42 subtests passed`。
- 对应知识点：异步 UI 状态机、Worker 生命周期终态、业务结果与资源结算分离、`try/except/finally` 语义、Qt Signal 的职责拆分、取消路径测试、跨线程关闭阻塞 I/O。
- 面试表达：线程退出不等于界面自动知道任务结束。成功、错误、取消可以有不同业务表现，但必须共享唯一的生命周期结算信号；把按钮解锁只放在成功或错误 Handler 中，会漏掉“预期取消导致异常退出”这一条合法终态路径。
- 当前状态：已修复并通过全仓回归。

## 29. 测试命令和结果

指定 Runtime 回归：

```text
uv run python -m pytest \
  tests/test_retrieval_contract.py \
  tests/test_retrieval_execution.py \
  tests/test_retrieval_provenance.py \
  tests/test_retrieval_integration.py \
  tests/test_model_invocation.py \
  tests/test_model_context.py \
  tests/test_runtime_event_integration.py \
  tests/test_budget.py \
  tests/test_timeout_cancellation.py -q
```

最终验收结果：`92 passed, 12 subtests passed in 4.47s`。本次补查已加入 Model Invocation、Rewrite 非递归、Blocking Admission/嵌套提交防护、单 Worker 完整 Retrieval、Citation Payload、Score Semantics 与完整 Budget Snapshot 用例。

全仓测试：

```text
uv run python -m pytest -q
```

最终验收结果：`441 passed, 42 subtests passed in 6.68s`。

静态与锁文件检查：

```text
uv run python -m compileall -q core tools tests
uv lock --check
git diff --check
```

结果：全部通过；`uv lock --check` 显示 `Resolved 157 packages in 1ms`。本次 `compileall` 直接通过；lock 检查首次因沙箱无权读取用户级 uv Python 管理目录失败，按要求在获准的非沙箱环境重跑后通过。`git diff --check` 通过，仅报告 Git 的 LF/CRLF 工作树提示，没有空白错误。

本轮最终验收新增 7 个测试：

- `test_knowledge_rewrite_is_non_recursive_and_owns_dedicated_messages`
- `test_degraded_rewrite_event_order_continues_only_current_retrieval`
- `test_single_worker_full_retrieval_and_two_concurrent_runs_do_not_deadlock`
- `test_owner_worker_nested_submission_fails_fast_without_deadlock`
- `test_queued_provider_call_deadline_releases_budget_and_never_executes`
- `test_filter_skips_keyword_without_reserving_or_charging_budget`
- `test_final_model_context_payload_hash_and_citation_order_are_exact`

同时强化 `test_vector_score_semantics_are_declared_and_converted_exactly_once`，增加 dynamic floor 与未声明枚举语义固定失败断言。测试没有访问真实网络、在线模型、外部数据库或 UI。

## 30. 未完成事项和已知风险

- 没有高级 Rerank；只有项目既有的启发式 Rerank。
- 没有调整 Embedding 模型、Prompt 或归一化配置。
- 没有重建索引，也没有修改 Chroma 索引参数。
- `scripts/query_local_kb.py` 和 VectorDBManager 公共兼容 API 仍是 Legacy/独立检索入口。
- 同步 Embedding、Vector Store 与模型 Query Rewrite 已开始后不能被 Python 安全强杀；Runtime 会如实标记 detached/background pending，停止后续工作并丢弃迟到结果。
- Source ID 和 Chunk ID 的可追溯性仍依赖入库 Metadata 质量。
- Citation 只绑定真正进入 Retrieval Context 的 Chunk；被过滤、去重或未选择候选没有 Citation。
- Memory 仍是 `USER_CONTENT`，不获得 System/Developer 权限。
- Retrieval Budget 是单 Run、单进程账本，不支持跨进程共享或分布式配额。
- 没有实现 AgentEvalOps。
- 没有实现高级召回算法、Query Expansion 或 Multi-query Retrieval。
- 没有实施 Event Journal、持久化或 Replay。
- 默认 Legacy Text Stream 没有 StepEventEmitter，因此没有 Retrieval Runtime Event。
- Tool Result 的旧 system 注入边界仍存在，但不属于第 18 天 Retrieval/Memory 范围。

## 31. 面试表达

我没有重写知识库，也没有引入新召回模型，而是先审计真实 Knowledge Expert 路径，把原有模型改写、双路 Chroma 召回、Keyword 补召回和启发式重排逐阶段迁入 Runtime。关键设计不是“搜得更聪明”，而是让每次检索都能回答：调用是否真的完成、预算和 Deadline 是否允许、失败发生在哪一层、哪些文本最终进入 Context、每条引用能否追溯、Memory 和知识库内容有没有越权。

在结果语义上，只有合法完成后的零候选才是 EMPTY；Embedding、Vector、Budget、Cancellation 和 Timeout 都有独立类型。可选优化允许显式降级，但候选完整性、原始 Rank、失败原因和最终 Citation 数必须保留。同步本地依赖无法强杀这一限制也被如实披露，而不是伪装成完全可取消。

## 32. 需要带回 ChatGPT 审查的信息

新增文件：

- `core/runtime/retrieval_contract.py`
- `core/runtime/retrieval_context.py`
- `core/runtime/retrieval_adapters.py`
- `core/runtime/retrieval_execution.py`
- `core/runtime/blocking_executor.py`
- `core/knowledge_base/vector_scores.py`
- `tests/test_retrieval_contract.py`
- `tests/test_retrieval_execution.py`
- `tests/test_retrieval_provenance.py`
- `tests/test_retrieval_integration.py`
- 本结果文档

修改文件：

- `core/agent_router.py`
- `core/knowledge_base/vector_db_manager.py`
- `core/runtime/budget.py`
- `core/runtime/events.py`
- `core/runtime/model_context.py`
- `core/runtime/model_invocation.py`
- `core/runtime/__init__.py`
- `tests/test_model_context.py`
- `tests/test_model_invocation.py`

审查摘要：

- 修改前真实 Pipeline：模型 Query Rewrite -> rewritten/original 双路 Chroma -> Keyword 补召回 -> 启发式 Rerank -> 字符去重/截断 -> 固定 `local-kb` Citation。
- 已迁移入口：Knowledge Expert `_build_messages()` 与兼容 `_build_rag_context()`。
- 未迁移入口：运维查询 CLI、VectorDBManager 公共兼容搜索 API、独立 FTS5 Memory UI 搜索。
- Invocation：不可变、稳定 Query Digest、JSON-safe Filter、重试可复用 Retrieval ID。
- Stage：六阶段与安全 Stage Record。
- Spec：总/阶段 Timeout 与候选、Context、Chunk、读取上限。
- Context：RunContext + Ledger + Emitter + monotonic deadline，不改运行状态。
- Query Rewrite：唯一 Owner 是 ModelInvocationRouter；普通失败使用原 Query，Cancel/Deadline/Budget/Safety 不降级。
- Embedding：既有 HuggingFace 同步模型，显式 Stage、锁与向量校验。
- Retrieve：已生成向量调用 Chroma；失败不是 EMPTY。
- Rerank：真实启发式；没有高级 Reranker。
- Document Load：Chroma content materialization，不从任意路径读取。
- Source/Chunk ID：优先入库 Metadata，均不依赖 Rank。
- Provenance：记录变换和双 Hash。
- Citation：最终 Context Build 后逐 Chunk 绑定。
- Context Build：稳定 normalize/deduplicate/truncate/select，不做高级语义去重。
- Budget：复用 BudgetLedger，新增六个 Retrieval 维度（含 Keyword），Model Rewrite 使用原 Model Budget。
- Timeout/Cancellation：有界 active/pending Admission；同步线程不可抢占时显式 detached。
- EMPTY：只表示合法零候选/合法过滤为空。
- FAILED：表示未完成，不能判断资料是否存在。
- DEGRADED：只允许 Rewrite/Rerank/部分内容物化三类。
- Memory Trust：Summary/FTS/History Contract 强制 USER_CONTENT，禁止 RAG Citation。
- Runtime Event：三类强类型、安全计数事件。
- Knowledge Expert：真实主入口已迁移，失败不再统一“未找到”。
- 测试结果：指定 `92 passed + 12 subtests`；全仓 `441 passed + 42 subtests`；compileall、lock、diff check 全部通过。
- Bad Case：本文件第 28 节共 19 项，已区分真实发现与假设构造。

需要人工确认的问题：

1. 生产环境是否需要基于负载把默认 `max_workers=4/max_pending_tasks=8` 调整为配置项；当前边界本身已是严格有界。
2. 现有 Chroma 库是否全部由当前 `document_loader` v2 Metadata 入库；旧索引若缺少 Source/Chunk 身份会按 `METADATA_INVALID` fail closed。
3. 默认 Legacy Chat Stream 是否需要在后续单独迁移到 Coordinated Event Stream；本日没有切换 UI/HTTP 协议。
4. `scripts/query_local_kb.py` 是否应长期保留为绕过 Runtime 的运维诊断入口。
5. Tool Result 的 system 注入风险应在哪一天迁移；本日没有越界修改 Tool Context。

后续建议（本次未实施）：

- 在后续既定阶段评估 Legacy Chat Stream 的 Coordinated 迁移。
- 为旧 Chroma 数据提供只读 Metadata 审计，而不是自动重建索引。
- 生产观测中只记录 Result/Event 安全字典，并监控同步 Worker 超时后的占用。
- 若未来增加真正 Reranker，继续使用现有候选完整性与 Controlled Degradation Contract。
- 若未来接入 FTS5 Retrieved Memory 到模型 Context，使用 `MemoryContextRecord`，保持与 RAG Result/Citation 独立。

## 33. 第 18 天运行边界补查结论

| 检查项 | 最终结论 |
| --- | --- |
| Query Rewrite Model Owner | `AgentRouter._invoke_model_contract()` -> 既有 `ModelInvocationRouter`。 |
| Rewrite message owner | `AgentRouter._rewrite_knowledge_query()` 预构建仅含 system/user 的专用 Model Messages；不调用 Knowledge Expert `_build_messages()`。 |
| Rewrite purpose | 等价的显式类型边界为 `QueryRewriteStrategy.EXISTING_MODEL + BlockingTaskKind.QUERY_REWRITE + _rewrite_knowledge_query()`；没有把 Rewrite 当作 Knowledge Expert 回答或新 Retrieval。 |
| Rewrite recursion | 不可能沿当前实现递归进入 Retrieval；真实 `_build_messages()` 调用次数为 1，`RETRIEVAL_STARTED` 为 1，Rewrite Attempt 为 1，Memory Retrieval 与 Tool Planning 均为 0。 |
| Nested Retrieval | Rewrite 期间无第二个 `RETRIEVAL_STARTED`、无第二个 Query Rewrite Stage，普通失败只用 original query 继续当前 Retrieval。 |
| 旧模型直接路径 | Rewrite 不再调用 `_collect_model_response()`；旧入口调用次数为零。 |
| Model Budget | 每个真实 Attempt 计 `model_calls`、Token、Cost；Retry 计 `retries`。 |
| Model Event | 成功顺序固定为 `RETRIEVAL_STARTED -> MODEL_STARTED -> MODEL_COMPLETED -> QUERY_REWRITE -> EMBEDDING -> RETRIEVAL_COMPLETED`。普通失败的 Model Completed 为 failed，Rewrite Stage 为 degraded，随后只进入当前 Retrieval 的 Embedding。 |
| Circuit / Retry | Circuit Open 在 Adapter 前阻断；Retry 只由既有 RetryExecutor 所有，metadata 稳定。 |
| Cancel / Deadline / Budget | 均不进入后续 Embedding，不返回 DEGRADED/EMPTY，也不触发旧模型或递归 Retrieval fallback。 |
| Blocking Executor | Retrieval 编排留在调用方/Event Loop bridge；应用级 `BoundedBlockingExecutor` 只接收 Query Rewrite、Embedding、Vector、Keyword、Rerank、Document Load、Context Build 的同步叶子调用。 |
| Nested executor submission | 同一 Owner Worker 调用 `submit()` 立即抛出 `BlockingExecutorNestedSubmissionError("BLOCKING_EXECUTOR_NESTED_SUBMISSION")`，不依靠真实死锁检测。 |
| max_workers=1 | `max_workers=1/max_pending_tasks=1` 下，同步 Model Adapter 的完整 Rewrite/Embedding/Vector/Context Retrieval 在 2 秒测试上限内成功。 |
| Concurrent Retrieval | 两个并发完整 Retrieval 共用单 Worker Executor 均成功；所有 submit 均来自 orchestration thread，而非 `day18-leaf` Worker。 |
| max workers | 默认 4。 |
| max pending | 默认 8；Admission 总容量为 active 4 + pending 8。 |
| Admission cancellation | 每 50ms 响应 Run Cancellation 与 Deadline；未获 Permit 不 submit。 |
| Queued deadline | Deadline 后尚未开始的 Provider 任务被取消，不执行、不 commit；第三个超限提交等待期间可取消。 |
| Detached Worker | 运行中超时标记 detached/background pending，真实结束后才注销并释放 Permit。 |
| wait_until_idle | 支持成功与超时；`shutdown(wait=True, timeout=...)` 使用相同真实 Worker 边界。 |
| Stage timeout meaning | 只表示 Runtime 停止等待，不声称同步 Worker 已终止。 |
| Citation hash target | Retrieval 最终 `RetrievedChunk.text` Payload；固定包装不计入 Hash。 |
| ContextBuilder mutation | Citation Payload 不做 strip、换行折叠或截断，只添加固定包装。 |
| Context overflow | mandatory Payload 放不下时返回 `CONTEXT_BUILD_FAILED/FAILED`，不返回 EMPTY。 |
| Vector API score semantics | 当前锁定 Chroma by-vector API 的第二项是 raw distance。 |
| Score conversion | raw distance 恰好转换一次，normalized relevance 不二次转换；排序/dynamic floor 使用最终 relevance。非 `VectorScoreSemantics` 枚举声明固定失败，不根据方法名猜测。 |
| Rewrite calls | 每次真实模型 Attempt 进入 Model Budget；普通失败降级时不会双跑旧入口。 |
| Embedding calls | rewritten/original 去重后每个唯一 Query 一次。 |
| Vector calls | 与唯一 Query 一一对应。 |
| Keyword calls | 独立 `keyword_queries`，只在真实 Keyword Search 可执行时计一次；Filter 跳过时为 0 且不 Reservation。 |
| Document reads | 与每个真实 content materialization 精确一致。 |
| Context chars | 精确等于最终 `RetrievedChunk.text` 字符总数；预计/实际原子补差或退款，不越限。 |
| Budget snapshot | 首次 Rewrite `model_calls=1`；Retry 后 `model_calls=2/retries=1`；同 Query 为 1 次 Embedding/Vector，不同 Query 为 2 次；未开始 release、已开始 Timeout commit、并发 Reservation 不越限。 |
| Citation/model payload | 多行、Unicode、首尾空格原始 Chunk 在 Retrieval normalize 后形成最终 Payload；ContextBuilder 对该 Payload 不再修改。Hash 只覆盖最终 Payload，不覆盖固定包装。 |
| Citation order | Chunk、Citation、最终 Model Context 顺序一致；mandatory overflow 返回 `CONTEXT_BUILD_FAILED` 并清空 Chunk/Citation，不生成孤儿 Citation。 |

本次补查未实现高级召回算法、AgentEvalOps、Event Journal、Tool Registry、Skill、MCP 或第 19 天内容。
