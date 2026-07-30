# 第 24 天第二轮 A：Model / Retrieval Fault Injection

## 1. 本轮目标

本轮在第一轮 Fault Injection Foundation 上完成五项基础合同修正，并只把
`MODEL_BEFORE_INVOCATION`、`MODEL_BEFORE_PROVIDER_CALL`、
`RETRIEVAL_BEFORE_REWRITE`、`RETRIEVAL_BEFORE_SEARCH` 接到真实调用链。
Controller 保持 request/test-scoped，生产入口默认 `None`。本轮没有接 Tool、
post-provider、post-search、Event/Journal、Snapshot/Recovery、Observability、
Shutdown、生产 Settings/API、概率 Chaos 或自动 Replay。

## 2. Foundation 合同修正

- `FaultPlan.to_safe_dict()/to_safe_json()` 继续包含 UTC `created_at`；
  `digest_source()` 只包含 schema version、plan ID 和 canonical rules，因此相同
  语义的 Plan 不受创建时间影响。
- `FaultRule.priority` 默认为 1000，拒绝 bool，范围为 `0..1_000_000`。Plan 和
  Controller 都使用 `(priority ASC, rule_id ASC)`；priority 进入 Digest。
- 删除 `UNTIL_MAX_HITS`。`FIRST_MATCH` 与 `ON_NTH_MATCH` 强制
  `max_hits=1`；`ALWAYS` 和 `AFTER_N_MATCHES` 仍受正整数 `max_hits` 约束。
- Recorder 默认改为 `REJECT_NEW`；显式保留 `DROP_OLDEST`。Snapshot 同时提供
  `overflowed`、`rejected_count`、`dropped_count`。
- 固定公开 `DANGEROUS_FAULT_POINTS`：Model provider success/usage commit 后、
  Tool provider return/side-effect commit/completion event 前、Journal append 后、
  Channel enqueue 前、Snapshot save 后与 Executor submit 后。构造这些 Rule
  必须 `dangerous_window=true`，本轮没有接入。

## 3. 修改前 Model 调用链

真实链路为：

```text
CoordinatedRunScope
-> CoordinatedSingleAgentDriver
-> AgentRouter.complete_single_agent()
-> AgentRouter._invoke_model_contract()
-> ModelInvocationRouter.invoke()
-> ModelInvocationRouter._invoke_impl()
-> ModelAdapterResolver.resolve()
-> ModelAdapter.invoke()  # 唯一 Provider call
```

- Model Invocation Owner：`ModelInvocationRouter.invoke()`。
- Model Attempt Owner：`ModelInvocationRouter._invoke_impl()`。
- Provider Call 唯一位置：解析出的 `ModelAdapter.invoke()`；Generator 兼容路径
  仍由 `GeneratorModelAdapter.invoke()` 调用真实 `generate()`。
- Retry 判定读取 `classify_model_failure()` 产生的 `ModelFailureCategory`，唯一
  Policy/Decision Owner 是既有 `RetryExecutor`。
- Fallback 判定读取同一个 `ModelFailureCategory`，由既有
  `ModelRoutingPolicy.can_fallback()` 和候选链决定。
- Budget reserve 位于 Permit、上下文和截止时间检查之后；Provider 未开始失败
  release，开始后失败保守 commit，成功按 actual/estimated usage commit。
- `ModelInvocationRouter`、Adapter Resolver、Breaker Registry、RetryExecutor
  是 application-scoped；RunContext、BudgetLedger、messages、event emitter、
  generation options 与本轮 Controller 是 request-scoped 调用参数。

## 4. 修改前 Retrieval 调用链

真实链路为：

```text
AgentRouter._execute_knowledge_retrieval()
-> RetrievalExecutionService.execute()
-> RetrievalExecutionContext
-> QUERY_REWRITE
-> EMBEDDING
-> RETRIEVE (vector + optional keyword)
-> RERANK
-> DOCUMENT_LOAD
-> CONTEXT_BUILD
```

- Rewrite Owner：`RetrievalExecutionService` 的 `QUERY_REWRITE` Stage；真实
  Knowledge Adapter 回调 `AgentRouter._rewrite_knowledge_query()`，并复用同一个
  `ModelInvocationRouter`，没有第二套 Model Retry。
- Search Owner：`RetrievalExecutionService` 的 Embedding/`RETRIEVE` Stage；
  Vector/Keyword 的底层唯一调用在 `RetrievalAdapter`。
- Degradation Owner：现有 `RetrievalExecutionService`。当前只有普通
  `QUERY_REWRITE_FAILED` 降级为 original query、Rerank 失败降级为原排序和允许的
  partial document load；Vector Search 失败按原合同 fail closed。
- `RetrievalExecutionService`、Adapter、`BoundedBlockingExecutor` 是
  application-scoped；Invocation、RunContext、BudgetLedger、StepEventEmitter、
  RetrievalExecutionContext 与 Controller 是 request-scoped。
- `BoundedBlockingExecutor` 仍只承载同步叶子调用；`RuntimeActivityTracker`
  继续由真实 Model/Retrieval Owner 增减计数，本轮没有改 Worker 生命周期。

## 5. Fault Controller 传递

显式传递链为：

```text
CoordinatedRuntimeFactory.create_run_scope(fault_controller=...)
-> CoordinatedRunScope
-> CoordinatedSingleAgentDriver
-> AgentRouter
-> ModelInvocationRouter.invoke(fault_controller=...)
-> RetrievalExecutionService.execute(fault_controller=...)
-> RetrievalExecutionContext.fault_controller
```

生产调用不传参数时固定为 `None`。`ApplicationRuntimeServices`、
`ModelInvocationRouter` 和 `RetrievalExecutionService` 都不缓存当前 Run 的
Controller。Controller 不进入 RunContext 核心身份、AgentState、Snapshot、
RuntimeEvent、Journal 或 Wire；没有 ContextVar、模块全局、HTTP Header、
Prompt 或环境变量入口。

Query Rewrite 的真实 Model 调用也通过显式参数继续传入同一 request Controller；
兼容旧 Adapter 时只在方法明确支持该参数时传递，不创建隐式 current controller。

## 6. Model Fault Points

`MODEL_BEFORE_INVOCATION` 位于 Invocation Span/身份建立后、`_invoke_impl()` 和
任何 Budget/Attempt/Provider 之前。命中 typed error 时返回现有
`ModelInvocationChainError`，attempts 为空、Provider 调用为零、Usage 为零。

`MODEL_BEFORE_PROVIDER_CALL` 位于 Permit、Budget reservation、Attempt Span 和
Adapter resolution 完成后，`MODEL_STARTED` 与 `adapter.invoke()` 之前。注入异常
被现有 Attempt 失败路径处理：Provider 未开始、reservation release、Permit
abandon、Attempt Span 安全结束；之后才由既有 Retry/Fallback 决策。

本轮没有调用任何 `MODEL_AFTER_*` 点。

## 7. Model Error Mapping

映射在 Model seam 本地完成，Controller 不认识领域错误：

| InjectedFaultCode | ModelFailureCategory | 安全错误码 |
| --- | --- | --- |
| `INJECTED_TRANSIENT_FAILURE` | `TRANSIENT_PROVIDER_FAILURE` | `MODEL_INJECTED_TRANSIENT_FAILURE` |
| `INJECTED_RATE_LIMIT` | `RATE_LIMITED` | `MODEL_INJECTED_RATE_LIMIT` |
| `INJECTED_TIMEOUT` | `PROVIDER_TIMEOUT` | `MODEL_INJECTED_TIMEOUT` |
| `INJECTED_PERMANENT_FAILURE` | `BUSINESS_FAILURE` | `MODEL_INJECTED_PERMANENT_FAILURE` |

Permanent 明确为 non-retryable，没有被错误映射为 transient。原始
`InjectedFaultError` 不穿透到 Model Wire；错误文本不包含 Rule、Prompt、
Profile、Provider 原始错误或路径。

## 8. Model Retry / Fallback

Controller 命中只产生一次当前 seam 的 typed failure，不调用 Retry。随后仍由
`RetryExecutor.decide()` 读取 category、retry index、partial output、deadline、
fallback availability 和预计耗时。测试证明 transient 可由现有 Policy 插入下一
Attempt，permanent 不 Retry；rate limit 仍服从既有 `FALLBACK_FIRST`；
`ON_NTH_MATCH=2` 使用真实 Attempt 序列而不是私建计数。

Fallback 仍只遍历 Routing candidate，不跨 Runtime，也不由 Controller选择。
本轮未修改 RetryPolicy、RateLimitRecoveryMode、Circuit 健康语义或同步非零
backoff 合同。

## 9. Model Budget / Event / Trace

Provider 前故障沿已有未开始路径 release reservation，`model_calls` 不 commit。
取消发生在 Delay/Block 等待期间时显式标记 `provider_started=false`，同样不收费。

Fault Controller 不发布 Event 或 Span。Invocation/Attempt Span 仍由
`ModelInvocationRouter` 创建和结束；`MODEL_STARTED/MODEL_COMPLETED` 仍由真实
Attempt Owner 发布。Provider 前注入不会伪造 `MODEL_STARTED`。Disabled parity
测试比较了 Span 结构；既有 Event 集成和全仓回归继续通过。

## 10. Retrieval Fault Points

`RETRIEVAL_BEFORE_REWRITE` 只在 Adapter 真实声明
`QueryRewriteStrategy.EXISTING_MODEL` 时进入 `QUERY_REWRITE` Stage；若 strategy
为 `NONE`，Stage 仍按原合同 `SKIPPED`，不会虚构 rewrite call 或消费 Rule。

`RETRIEVAL_BEFORE_SEARCH` 在显式 Embedding Adapter 场景进入 `EMBEDDING` Stage
的任何 Adapter 调用之前；兼容 Adapter-managed Embedding 时进入 `RETRIEVE`
Stage 的 Vector 调用之前。因此命中时 Embedding、Vector 和 Keyword 调用均为零。

本轮没有接 `RETRIEVAL_AFTER_REWRITE`、`RETRIEVAL_AFTER_SEARCH` 或 result commit。

## 11. Retrieval Error Mapping

映射在 Retrieval seam 本地完成：

- Rewrite transient/permanent -> 现有 `QUERY_REWRITE_FAILED`，由既有 rewrite
  degradation 决定是否使用 original query；
- Search transient/permanent -> 现有 `VECTOR_STORE_FAILED`；
- Rewrite/Search timeout -> 现有 `_StageTimedOut` 路径，最终
  `RetrievalExecutionStatus.TIMED_OUT + RetrievalErrorCategory.TIMEOUT`。

真实 Retrieval taxonomy 没有 rate-limit 类别，因此本轮没有虚构 Retrieval
rate-limit mapping；不支持的 Injected code 使用固定 Internal 安全失败。

## 12. Retrieval Degradation

Controller 不选择降级策略。普通 rewrite 注入失败由现有
`QUERY_REWRITE_FAILED` 分支使用 original query，且不重跑 Rewrite。Timeout 不
降级，直接 TIMED_OUT。Search 注入仍服从当前 Vector failure 合同，即 FAILED；
本轮没有为了测试虚构 Search fallback、空结果或第二次搜索。Rerank 和 Document
Load 原有降级分支未修改。

## 13. Retrieval Budget / Event / Trace

Retrieval call 的既有启动计费保持不变；Search pre-call 命中时 embedding/vector
维度均为零。Stage Record、Stage Event 和 Stage Span 仍由
`RetrievalExecutionService` 真实 Stage Owner 生成，Fault Controller 不发布。
Timeout、Cancellation、degraded/failed 状态沿现有 Result 合同返回。

Detached Worker 行为没有改变：seam 在 submit 前运行，没有向
`BoundedBlockingExecutor` 新增 post-submit 点，迟到 Worker 规则仍由第 18/23 天
合同负责。

## 14. Disabled Parity

建立了 No Controller 与带 Plan 但 `enabled=false` Controller 的强等价测试。
Model 比较 Result、Attempt、Provider 次数、Budget usage 和 Span 结构；Retrieval
比较 status、Stage 顺序/状态/计数/错误码、Budget、Adapter calls、Span 和最终
rendered context。Disabled Controller 先走廉价 guard，不构造匹配上下文、不更新
counter、不调用 Recorder；Recorder records 固定为空。

全仓既有 Runtime Event、Journal、Wire 和 E2E 测试同时通过，证明默认关闭没有
改变这些路径。

## 15. Run Isolation

两个并发 Model Run 共用 application-scoped Router：Run A 使用 enabled
Controller 并失败，Run B 使用 Disabled Controller 并成功；Provider 次数、
counter 和 Recorder 不共享。两个并发 Retrieval Run 共用同一个 Service：
Run A 在 Search 前失败，Run B 正常成功。Service 没有 `fault_controller` 缓存
字段，关闭 Controller A 不影响 B。

Recorder 不保存明文 run ID；match context 只在 Controller 内使用 SHA-256
run digest，Record 本身没有 run ID 字段。

## 16. Cancellation / Delay / Block

既有 async Fault Controller 继续使用 cancellable Sleeper/Blocker。为同步的真实
Model/Retrieval Owner 增加 blocking seam action executor，只允许
`RAISE_TYPED_ERROR`、`DELAY`、`BLOCK_UNTIL_RELEASED`，以短轮询调用真实
Run/ Retrieval cancellation check；不运行 `RETURN_TYPED_FAILURE` 或
`CORRUPT_TEST_FIXTURE`。

测试证明 Model Delay 取消后 Provider 为零且 Model Budget 不提交；Retrieval
Block 取消后返回 CANCELLED，Embedding/Search 均未调用；Scope close 会关闭
Controller、释放 Blocker 并清理 Sleeper/Recorder。

## 17. Security

Fault Match Context、Decision、Recorder、异常、Event、Journal 和 Wire 都不保存：

```text
SECRET_PROMPT_TEXT
MODEL_OUTPUT_SECRET
TOOL_ARGUMENT_SECRET
TOOL_OUTPUT_SECRET
RAG_CHUNK_SECRET
MEMORY_SECRET
C:\Users\private-user
provider-secret-error
```

Model/Retrieval seam 只使用固定 component、digest、attempt number 和固定错误码。
Fault Rule ID 只存在于私有 Controller/Recorder，不进入普通 Runtime Event。
没有新增 `[[ORCH]]` 输出或任何用户可见 Fault 控制消息。

## 18. Runtime 真实接入

`CoordinatedRuntimeFactory.create_run_scope()` 提供仅供显式测试装配的可选
`fault_controller` 参数；默认生产调用不传。Run Scope 和 Driver 只持有引用，
Controller 生命周期仍由 `FaultInjectionScope`/测试拥有。真实
`AgentRouter._invoke_model_contract()`、Knowledge Retrieval 和 Query Rewrite
Model 都继续传递该引用。

没有修改 lifespan、`/api/chat`、Settings、Header、API schema 或
`ApplicationRuntimeServices`。普通生产请求仍为 No Controller。

## 19. Legacy Boundary

旧的非 Coordinated/流式入口没有新增 Fault Settings 或隐式 Controller，继续
使用默认 `None`。本轮没有迁移 legacy Tool、stream、orchestration planning 或
memory 路径，也没有给 legacy 路径建立第二套 Invocation/Retrieval 状态。

## 20. Bad Case

| # | 性质 | 故障表现 | 本轮防线/回归 |
| --- | --- | --- | --- |
| 1 | 真实发现并修复 | Plan Digest 包含 `created_at` | 独立 `digest_source()`；不同时间同 Digest |
| 2 | 真实发现并修复 | Rule ID 被当作优先级 | 显式 bounded priority；`(priority, rule_id)` |
| 3 | 真实发现并修复 | `UNTIL_MAX_HITS` 与 `ALWAYS` 重复 | 删除重复 Trigger，收紧 max hits |
| 4 | 真实发现并修复 | Recorder 默认丢弃最早因果记录 | 默认 `REJECT_NEW`，显式 overflow counters |
| 5 | 假设构造并阻断 | Controller 直接调用 Retry | Controller 只返回/抛一次；RetryExecutor 测试计数 |
| 6 | 假设构造并阻断 | Injected permanent 映射为 transient | 显式 Model/ Retrieval mapping |
| 7 | 假设构造并阻断 | Provider 前故障仍调用 Provider | Provider call count 固定为零 |
| 8 | 回归中发现并修复 | Delay 取消被误判为 Provider 已开始并提交 Usage | 取消/Deadline 标记 `provider_started=false` |
| 9 | 假设构造并阻断 | Retrieval Fault 自行选择降级 | 只映射 typed error，现有 Service 决策 |
| 10 | 假设构造并阻断 | Rewrite 故障后重复 Rewrite | Stage 只调用一次；原 Query 直接进入后续 Stage |
| 11 | 假设构造并阻断 | application service 缓存 Run Controller | Controller 仅为调用参数/ExecutionContext 字段 |
| 12 | 假设构造并阻断 | Run A Fault 命中 Run B | 并发 Run 隔离测试 |
| 13 | 假设构造并阻断 | Disabled Controller 改变 Event/Span 序列 | 强 parity + 全仓 Event/E2E 回归 |
| 14 | 假设构造并阻断 | Fault Rule ID 进入用户 Wire | 普通 Result/Event 无 Fault 字段 |
| 15 | 假设构造并阻断 | Delay/Block 不响应 Cancellation | Model Delay、Retrieval Block 取消测试 |

## 21. 测试结果

用户清单中不存在 `tests/test_model_retry.py`、
`tests/test_model_fallback.py` 和
`tests/test_retrieval_runtime_integration.py`。本轮按真实文件替换为：

- `tests/test_retry_model_integration.py`
- `tests/test_model_routing.py`
- `tests/test_model_circuit_breaker.py`
- `tests/test_retrieval_integration.py`

结果：

```text
目标专项 pytest:
163 passed, 12 subtests passed in 4.99s

全仓 pytest:
778 passed, 42 subtests passed in 9.33s

uv run python -m compileall -q core tools tests:
passed

uv lock --check:
Resolved 157 packages in 1ms

git diff --check:
passed（仅 Git 的 LF -> CRLF 工作区提示，无 whitespace error）
```

## 22. 未完成事项

- 未接任何 Tool FaultPoint。
- 未接 Model provider success/usage commit 后点。
- 未接 Retrieval rewrite/search 完成后点。
- 未接 Event/Journal、Snapshot/Recovery、Observability/Trace、Shutdown。
- 未增加生产 Settings/API/Header 或用户控制入口。
- 未实现概率 Chaos、自动 Replay/Recovery 或第 25 天内容。
- 未改变 Search 失败的现有 fail-closed 语义，也未虚构 Retrieval rate limit。

## 23. 第二轮 B 接入点

第二轮 B 若继续，应先重新审计 Tool 的一次调用、side-effect commit、completion
event 与幂等性边界，再决定仅允许的 Tool pre-call seam。危险的 post-provider、
post-commit、Event/Journal、Snapshot 和 Executor after-submit 点必须保持
`dangerous_window=true`，并在实际接入前分别证明去重、预算、事件一致性和恢复
语义；本轮不预先实现。

## 24. 需要带回 ChatGPT 审查的信息

- Model permanent 当前映射为既有 non-retryable `BUSINESS_FAILURE`；请确认未来
  是否需要新增更精确但仍 non-retryable 的领域类别，本轮没有扩展 taxonomy。
- Retrieval taxonomy 没有 transient/permanent/rate-limit 的一一对应类别；
  Search transient/permanent 均安全映射为现有 `VECTOR_STORE_FAILED`，rate limit
  未接入。
- 当前 Retrieval 只对 Query Rewrite、Rerank 和 partial document load 有既有
  降级；Search failure 仍 fail closed。本轮刻意没有改变业务语义。
- 同步 Model/Retrieval seam 使用有界短轮询响应 Cancellation；后续若真实入口
  迁移为 async，可统一回既有 async FaultSleeper/Blocker，但不能改变当前
  Retry/Fallback/Stage Owner。
- 请求级 Controller 已可由测试创建的 CoordinatedRunScope 显式携带，但生产
  enablement 仍为关闭且没有 API。
