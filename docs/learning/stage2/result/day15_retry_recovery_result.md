# 阶段二第 15 天改造结果

## 1. 本次目标
建立不泄露业务内容的 RetryPolicy、RetryDecision、RetryExecutor 与 Tool/RAG 幂等性契约。

## 2. 修改前 Retry 现状
Remote HTTP Session 已配置 `max_retries=0`；Adapter、Driver、Coordinator 没有同 Profile 循环。本次检查未发现额外隐藏重试。

## 3. Retry、Fallback、Circuit 边界
Router 遍历唯一候选链；同 Profile 的 Retry 由 RetryExecutor 策略决定，耗尽后才按原 RoutingPolicy 进入 Fallback。每次真实 Attempt 都重新取得 Circuit Permit 和预算预留。

## 4. Retry Owner
唯一 Retry Owner 是 `RetryExecutor`；Adapter 仍只作一次底层调用。

## 5. RetryPolicy
不可变 Policy 包含 max_attempts、基础/最大延迟、乘数、Jitter、最小 Attempt 时间、不可变失败分类和 Rate Limit 策略。`max_attempts` 包含 initial attempt。

## 6. Failure 分类
默认仅 transient、provider timeout、rate limited 可重试；配置、上下文、请求、安全、业务、取消、截止、预算、Circuit Open 和 unknown 均保守停止。

## 7. Backoff
`min(max_delay, base_delay * multiplier ** (retry_index - 1))`，使用有界迭代避免溢出。

## 8. Jitter
NONE、FULL、EQUAL 均通过可注入 RandomSource 计算，测试使用固定随机源。

## 9. Retry Budget
真正开始的 retry Attempt 预留 `retries=1` 和完整模型预算；initial 与 Fallback initial 均为零。预算预留是原子的，未开始 Provider 时释放。

## 10. Deadline-aware Retry
Decision 要求剩余时间大于 backoff 加 `max(minimum_attempt_seconds, profile latency)`；等待后必须再次检查。

## 11. Cancellation
CancellableRetrySleeper 同时等待 delay、CancellationToken 与 deadline 中最早事件，使用 asyncio Task/to_thread Event wait 而非 time.sleep 或忙轮询；取消后不会进入下一次 Permit、预算或 Adapter 调用。

## 12. Idempotency
MODEL 默认可重试（但消耗配额）；READ_ONLY/IDEMPOTENT 可重试；WITH_KEY 需要稳定非空 Key；NON_IDEMPOTENT、UNKNOWN 或已提交副作用禁止。

## 13. Model Retry
真实协调同步入口对 delay=0 执行同 Profile Retry；若 Policy 给出 delay>0，稳定停止并记录 `SYNC_RETRY_DELAY_UNSUPPORTED`，绝不忽略 delay 后立即调用。非零 Backoff 仅由 async RetryExecutor/CancellableRetrySleeper 支持，尚未迁移该真实入口。

## 14. Tool Retry 契约
只提供通用 Idempotency 判定和 Fake 测试，未迁移真实 Tool 系统。

## 15. RAG Retry 契约
只读检索与带稳定上下文的 embedding 可用契约重试；格式错误、上下文限制不重试；未重写 RAG 流程。

## 16. Circuit Breaker 协同
每次 Attempt 新 Permit；OPEN 不消费预算；HALF_OPEN 失败依既有 Breaker 回到 OPEN，不继续同 Provider Retry。

## 17. Partial Output
`output_started=True` 时禁止 Retry 和 Fallback，绝不拼接 Attempt 输出。

## 18. Attempt 记录
Attempt 扩展了 candidate/retry index、backoff 与 circuit state 安全字段；不保存 Prompt、输出或 Provider 原始异常。

## 19. Retry Exhausted 与 Fallback
通常 Retry 耗尽返回安全失败，Router 决定下一 Profile；但 RATE_LIMITED 的 FALLBACK_FIRST 有合法候选时立即 Fallback，RETRY_CURRENT_FIRST 先 Retry，STOP 则既不 Retry 也不 Fallback。

## 20. 隐藏 Retry 检查
HTTP `max_retries=0` 保持；Adapter 单次 invoke；Driver、Coordinator、AgentLoop 未增加 Retry。

## 21. 已迁移入口
`ModelInvocationRouter` 的 coordinated 非流式模型调用预算计数已接入。

## 22. 未迁移入口
默认流式聊天、多 Agent、真实 Tool/RAG 仍为 Legacy，Retry 状态未持久化。

## 23. 重点 Bad Case
### Bad Case 1：多层隐藏 Retry
- 类型：假设构造
- 触发条件：Client/Adapter/Router 同时重试
- 故障表现：调用倍增
- 根因分析：Owner 不唯一
- 修复方案：HTTP 保持零、唯一 Executor
- 回归测试：Adapter single-call 检查
- 对应知识点：Retry Owner
- 面试表达：重试必须单点拥有
- 当前状态：已防护

### Bad Case 2：Fallback 初始调用计为 Retry
- 类型：假设构造
- 触发条件：候选切换
- 故障表现：误耗 retry budget
- 根因分析：按全局次数计数
- 修复方案：retry_index 按候选归零
- 回归测试：预算用例
- 对应知识点：Attempt
- 面试表达：Fallback 不是 Retry
- 当前状态：已防护

### Bad Case 3：Backoff 忽略取消
- 类型：假设构造
- 触发条件：等待中取消
- 故障表现：无效 Provider 调用
- 根因分析：缺少安全点
- 修复方案：Sleeper 前后检查 Token
- 回归测试：async Fake Sleeper
- 对应知识点：Cancellation
- 面试表达：取消优先于恢复
- 当前状态：已设计

### Bad Case 4：Partial Output 后 Retry
- 类型：已知限制
- 触发条件：Provider chunk 后失败
- 故障表现：正文拼接
- 根因分析：输出不可回滚
- 修复方案：禁止 Retry/Fallback
- 回归测试：partial-output 用例
- 对应知识点：一致性
- 面试表达：部分输出宁可失败
- 当前状态：已防护

### Bad Case 5：非幂等 Tool Retry
- 类型：假设构造
- 触发条件：写操作超时
- 故障表现：重复副作用
- 根因分析：未知是否已提交
- 修复方案：默认禁止
- 回归测试：Idempotency Fake
- 对应知识点：幂等性
- 面试表达：未知即不重试
- 当前状态：已防护

### Bad Case 6：等待后 Deadline 耗尽
- 类型：假设构造
- 触发条件：backoff 消耗剩余时间
- 故障表现：超时调用
- 根因分析：仅等待前检查
- 修复方案：等待后再检查
- 回归测试：Fake Clock
- 对应知识点：Deadline
- 面试表达：时间预算是动态的
- 当前状态：已设计

### Bad Case 7：过期预算快照
- 类型：假设构造
- 触发条件：并发 retry
- 故障表现：越额
- 根因分析：先检查后提交
- 修复方案：每 Attempt 原子 reserve
- 回归测试：并发账本测试
- 对应知识点：原子性
- 面试表达：Snapshot 不可当许可
- 当前状态：已防护

### Bad Case 8：HALF_OPEN 继续 Retry
- 类型：假设构造
- 触发条件：Probe 失败
- 故障表现：击穿恢复窗口
- 根因分析：忽略 Permit 状态
- 修复方案：Breaker 回 OPEN
- 回归测试：Circuit 回归
- 对应知识点：Circuit
- 面试表达：Probe 是稀缺资源
- 当前状态：继承既有防护

### Bad Case 9：未开始仍消耗 Retry Budget
- 类型：假设构造
- 触发条件：Permit/预算失败
- 故障表现：虚假消耗
- 根因分析：过早 commit
- 修复方案：reservation release/permit abandon
- 回归测试：账本测试
- 对应知识点：资源结算
- 面试表达：开始边界决定计费
- 当前状态：已防护

### Bad Case 10：真实随机和 sleep
- 类型：假设构造
- 触发条件：测试使用全局依赖
- 故障表现：flaky/阻塞
- 根因分析：不可控依赖
- 修复方案：注入 RandomSource/Sleeper
- 回归测试：FixedRandom
- 对应知识点：可测试性
- 面试表达：时间与随机必须可替换
- 当前状态：已防护

## 24. 测试命令和结果
新增 10 个集成/并发断言：零延迟真实入口 Retry、非零 delay 安全停止、Rate Limit 四种分支、候选唯一性与 max_retries 原子竞争；本环境 `python -m unittest tests.test_retry_policy tests.test_retry_executor tests.test_retry_model_integration -q` 共 13 项通过。指定 pytest 因系统 Python 缺少 `requests`、`.venv` 缺少 pytest 而不能完成；本次不以缺依赖修改业务代码。

## 25. 未完成事项和已知风险
仅部分 Model 路径接入；Tool/RAG 仅契约；默认流式未迁移；Partial Output 不重试；状态不持久化；多 Worker 不共享状态但预算 per-run；backoff 不保证恢复；Rate Limit、延迟、Circuit 参数需生产确认；Actual Usage 未完整接入；输出事件待后续 Runtime Event。

## 26. 面试表达
将 Retry 限制为同 Profile、单一 Owner，并用原子预算、Deadline、取消、Circuit 和幂等性共同约束；Fallback 始终在 Retry 耗尽后才发生。

## 27. 需要带回 ChatGPT 审查的信息
新增 `retry.py`、Policy/Decision/Executor、Idempotency 契约和测试；修改 Invocation/exports。需审查同步入口的零等待 Retry 过渡方案、生产 Rate Limit 策略、延迟估算、实际 Usage 与 Circuit 参数；后续建议迁移流式/Tool/RAG，但不实施第 16 天内容。
