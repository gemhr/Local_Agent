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

## 24. 远程阶段测试命令和结果（历史记录）
远程阶段新增了零延迟真实入口 Retry、非零 delay 安全停止、Rate Limit 分支、候选唯一性与 max_retries 原子竞争等集成/并发断言；当时仅完成部分 unittest、compileall 和 diff check。该阶段的局部结果不构成正式验收：ModelInvocation 集成、Runtime 指定回归和全仓 pytest 仍须按本文末尾的本地最终验证流程执行，不得据此把第 15 天标记为正式完成。

## 25. 未完成事项和已知风险
仅部分 Model 路径接入；Tool/RAG 仅契约；默认流式未迁移；Partial Output 不重试；状态不持久化；多 Worker 不共享状态但预算 per-run；backoff 不保证恢复；Rate Limit、延迟、Circuit 参数需生产确认；Actual Usage 未完整接入；输出事件待后续 Runtime Event。

## 26. 面试表达
将 Retry 限制为同 Profile、单一 Owner，并用原子预算、Deadline、取消、Circuit 和幂等性共同约束；Fallback 始终在 Retry 耗尽后才发生。

## 27. 需要带回 ChatGPT 审查的信息
新增 `retry.py`、Policy/Decision/Executor、Idempotency 契约和测试；修改 Invocation/exports。需审查同步入口的零等待 Retry 过渡方案、生产 Rate Limit 策略、延迟估算、实际 Usage 与 Circuit 参数；后续建议迁移流式/Tool/RAG，但不实施第 16 天内容。

## 28. 本地最终验证

### 28.1 验证边界与当前状态

- 远程阶段只完成了部分 unittest、compileall 和 diff check，不能替代本地正式验收。
- 2026-07-24 已在本地依次完成依赖检查、专项 pytest、Runtime 指定回归、全仓 pytest、compileall 和 diff check。
- 本次实际结果满足第 31 节全部正式通过标准，第 15 天状态已由“条件通过”更新为“正式通过”。
- 回到本地后先确认工作区，再拉取最新代码，并使用项目原有依赖环境执行测试。
- Windows llama.cpp Wheel 或可选依赖失败属于环境排查边界；不得通过修改 Retry 业务代码、删除 `llama_cpp` 生产代码、删除测试或 Mock Runtime 集成结构来绕过。

### 28.2 拉取与环境检查

按顺序执行：

```powershell
git status
git pull
python --version
uv --version
uv sync
uv run python -c "import pytest, requests; print('pytest/requests ok')"
```

执行 `git pull` 前必须确认没有会被更新覆盖的未提交改动。如果项目已有可运行的 `.venv`，可先执行最后一条 import 检查。

如果 `uv sync` 因平台锁定的 llama.cpp Wheel 失败：

1. 保存执行命令和完整错误栈。
2. 不修改 Retry、Fallback、Circuit Breaker 或 `llama_cpp` 业务代码。
3. 优先使用项目原有可运行虚拟环境，或只补齐当前测试明确缺少的依赖。
4. 补齐依赖后重新执行同一条失败命令。
5. 不为通过 collection 改写导入结构，除非导入结构本身已被确认是独立缺陷。

### 28.3 第 15 天专项 pytest

`tests/test_retry_model_integration.py` 承载零延迟 Retry、同步非零延迟拒绝、Rate Limit 分支、Candidate/Attempt 索引和 Retry/Circuit 组合断言，因此必须与原专项清单一起执行：

```powershell
uv run python -m pytest `
  tests/test_retry_policy.py `
  tests/test_retry_executor.py `
  tests/test_retry_model_integration.py `
  tests/test_model_invocation.py `
  tests/test_model_routing.py `
  tests/test_model_circuit_breaker.py `
  tests/test_budget.py -q
```

对应的跨平台写法：

```bash
uv run python -m pytest \
  tests/test_retry_policy.py \
  tests/test_retry_executor.py \
  tests/test_retry_model_integration.py \
  tests/test_model_invocation.py \
  tests/test_model_routing.py \
  tests/test_model_circuit_breaker.py \
  tests/test_budget.py -q
```

### 28.4 Runtime 指定 unittest

```powershell
uv run python -m unittest `
  tests.test_runtime_context `
  tests.test_agent_state `
  tests.test_agent_loop `
  tests.test_state_machine `
  tests.test_model_context `
  tests.test_planning `
  tests.test_model_selection `
  tests.test_scheduler `
  tests.test_plan_graph `
  tests.test_parallel_execution `
  tests.test_budget `
  tests.test_run_registry `
  tests.test_timeout_cancellation `
  tests.test_run_coordinator `
  tests.test_model_routing `
  tests.test_model_circuit_breaker `
  tests.test_model_invocation `
  tests.test_retry_policy `
  tests.test_retry_executor `
  tests.test_retry_model_integration -q
```

对应的跨平台写法：

```bash
uv run python -m unittest \
  tests.test_runtime_context \
  tests.test_agent_state \
  tests.test_agent_loop \
  tests.test_state_machine \
  tests.test_model_context \
  tests.test_planning \
  tests.test_model_selection \
  tests.test_scheduler \
  tests.test_plan_graph \
  tests.test_parallel_execution \
  tests.test_budget \
  tests.test_run_registry \
  tests.test_timeout_cancellation \
  tests.test_run_coordinator \
  tests.test_model_routing \
  tests.test_model_circuit_breaker \
  tests.test_model_invocation \
  tests.test_retry_policy \
  tests.test_retry_executor \
  tests.test_retry_model_integration -q
```

### 28.5 全仓回归与静态检查

```powershell
uv run python -m pytest -q
uv run python -m compileall -q core tests
git diff --check
```

正式执行顺序必须是：

1. import / dependency 检查；
2. 第 15 天专项 pytest；
3. Runtime 指定 unittest；
4. 全仓 pytest；
5. compileall；
6. `git diff --check`。

任一步失败时先停止，保留该步命令、首个失败测试和完整错误栈，不得跳过失败直接记录“全部通过”。

### 28.6 验收场景与真实测试位置

| # | 验收场景 | 测试位置 |
|---|---|---|
| 1 | 两个并发 Retry 竞争 `max_retries=1`，只有一个 Reservation 成功 | `tests/test_retry_policy.py::RetryBudgetAtomicityTests::test_two_concurrent_retry_reservations_allow_only_one` |
| 2 | 取得 Retry Budget 后才进入 Adapter | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_retry_budget_must_be_reserved_before_adapter_call` |
| 3 | 零延迟同 Profile Retry 成功 | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_zero_delay_retry_success_and_stable_indices` |
| 4 | 非零延迟返回 `SYNC_RETRY_DELAY_UNSUPPORTED` | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_nonzero_delay_never_calls_adapter_twice` |
| 5 | 非零延迟时 Adapter 只调用一次 | 同上 |
| 6 | `FALLBACK_FIRST` 有候选时直接 Fallback | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_rate_limit_modes` |
| 7 | `FALLBACK_FIRST` 无候选时 Retry 当前 Profile | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_rate_limit_without_fallback_retries_current` |
| 8 | `RETRY_CURRENT_FIRST` 先 Retry | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_rate_limit_retry_current_first_and_stop` |
| 9 | `STOP` 不 Retry、不 Fallback | 同上 |
| 10 | Fallback Initial 的 `retry_index=0`、`retries=0` | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_rate_limit_modes` |
| 11 | 同 Profile Retry 保持相同 `candidate_index` | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_zero_delay_retry_success_and_stable_indices` |
| 12 | Retry 时 `retry_index` 递增 | 同上及 `test_retry_exhausted_before_fallback_initial_attempt` |
| 13 | 重复 Profile 在 Adapter 调用前拒绝 | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_duplicate_candidate_chain_is_rejected` |
| 14 | Backoff 中取消后不产生下一次 Adapter 调用 | `tests/test_retry_executor.py::RetrySleeperTests::test_backoff_cancellation_starts_no_adapter_or_retry_budget` |
| 15 | Backoff 中取消后不提交 Retry Budget | 同上 |
| 16 | Circuit OPEN 后不开始新的 Retry Provider 调用 | `tests/test_model_invocation.py::ModelInvocationTests::test_open_circuit_blocks_retry_before_fallback` |
| 17 | HALF_OPEN Probe 失败后不立即 Retry 同一 Provider | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_half_open_probe_failure_does_not_retry_provider` |
| 18 | Partial Output 后不 Retry、不 Fallback | `tests/test_model_invocation.py::ModelInvocationTests::test_partial_output_forbids_transparent_fallback` |
| 19 | Retry Exhausted 后才进入 Fallback | `tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_retry_exhausted_before_fallback_initial_attempt` |
| 20 | 所有候选失败后 Step / Run 正确终结 | `tests/test_model_invocation.py::CoordinatedInvocationIntegrationTests::test_all_candidates_failed_marks_step_and_run_failed` |

十个指定检查文件均应存在、被 Git 跟踪并可被本地环境收集：

```text
tests/test_retry_policy.py
tests/test_retry_executor.py
tests/test_model_invocation.py
tests/test_model_routing.py
tests/test_model_circuit_breaker.py
tests/test_budget.py
tests/test_run_coordinator.py
tests/test_timeout_cancellation.py
tests/test_parallel_execution.py
tests/test_scheduler.py
```

## 29. 失败分类

### A. Collection / Dependency Failure

示例：`ModuleNotFoundError: requests`、`ModuleNotFoundError: pytest`、`ModuleNotFoundError: langchain_chroma`、llama.cpp Wheel 平台不兼容。

处理原则：记录缺失模块、失败命令和完整错误；不修改 Retry 业务逻辑；补齐依赖后重新执行同一命令。

### B. Retry Policy 单元失败

检查 `max_attempts`、Backoff 公式、Jitter 边界、Rate Limit Mode、Idempotency 和 Deadline 计算。

### C. ModelInvocation 集成失败

检查 RetryExecutor 是否为唯一 Retry Owner、同 Profile 的 `candidate_index` 是否稳定、`retry_index` 是否递增、Fallback Initial 是否为 `retries=0`、非零 delay 是否发生二次 Adapter 调用、`FALLBACK_FIRST` 是否跳过 Retry，以及 `STOP` 是否错误进入 Fallback。

### D. Budget 并发失败

检查 Retry Reservation 是否原子、是否只有一个竞争者成功、未取得 Reservation 的 Attempt 是否仍进入 Adapter、Reservation 是否泄漏，以及 committed / reserved 最终值是否正确。

### E. Circuit 协同失败

检查每个 Attempt 是否重新取得 Permit、OPEN 时是否仍进入 Adapter、HALF_OPEN Probe 失败后是否继续 Retry、Provider 已开始后是否错误 abandon，以及 Retry 成功是否重置连续失败计数。

### F. Runtime 状态失败

检查 Step 是否残留 RUNNING、Run 是否正确进入 FAILED / CANCELLED / SUCCEEDED、Registry 是否最终注销、Budget Reservation 是否泄漏，以及 StopReason 是否被清理错误覆盖。

## 30. 本地最终验证结果

执行日期：2026-07-24。以下数字均来自本地实际命令输出。

### 环境

- 操作系统：Microsoft Windows NT 10.0.26200.0
- Python：3.12.6
- uv：0.11.0
- Git Commit：`1e3b7be4cfabca00dd46539062365dfdbb803be4`
- 虚拟环境：`D:\PythonProject\Local_Agent\.venv`
- 依赖检查：`uv sync` 成功，检查 134 个包；`pytest`、`requests` 导入成功

### 第 15 天专项 pytest

- 命令：见 28.3
- 结果：通过
- passed：58
- failed：0
- errors：0
- subtests：12 passed
- 失败摘要：无

### Runtime 指定 unittest

- 命令：见 28.4
- 结果：通过
- tests：186
- failures：0
- errors：0
- 失败摘要：无

### 全仓 pytest

- 命令：`uv run python -m pytest -q`
- 结果：通过
- passed：279
- failed：0
- errors：0
- subtests：42 passed
- collection 状态：正常
- 失败摘要：无

### 静态检查

- compileall：通过
- `git diff --check`：通过；仅提示工作区 LF 将在 Git 后续处理时转换为 CRLF，无 whitespace error

### 第 15 天最终状态

- [x] 正式通过
- [ ] 仍为条件通过

### 需要带回 ChatGPT 的信息

- 专项测试结果：58 passed，12 subtests passed
- Runtime 回归结果：186 tests，OK
- 全仓结果：279 passed，42 subtests passed
- 首个失败测试：无
- 完整错误栈：无
- 是否为环境问题：否
- 是否修改业务代码：否

## 31. 正式通过标准

只有以下条件全部满足，才能把第 15 天标记为“正式通过”：

1. 第 15 天专项 pytest 完成且无失败。
2. Runtime 指定 unittest 完成且无失败。
3. 全仓 pytest 完成且无新增回归。
4. compileall 通过。
5. `git diff --check` 通过。
6. 并发 Retry Budget 场景实际通过。
7. ModelInvocation Retry / Fallback 集成实际通过。
8. Circuit / Retry 协同实际通过。
9. Candidate 与 Attempt 索引语义实际通过。
10. 未通过关闭核心断言、删除测试或改变既有 Retry / Fallback / Circuit 语义来换取通过。

如果全仓 pytest 仅被已有且与本次无关的可选依赖问题阻塞，必须准确记录阻塞命令、依赖和完整错误，状态仍保持“条件通过”，不得直接标记正式通过。
