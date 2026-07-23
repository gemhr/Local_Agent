# 阶段二第 11 天改造结果

## 1. 本次计划调整与目标
在第 10 天并行执行基础上，实现单 Run 原子 Budget Reservation，消除并发调用的 check-then-act race；不实现 Retry、Fallback 或运行中主动取消。

## 2. 修改前限制和预算现状
现有三类限制：`AgentLoopPolicy.max_steps`（Legacy Loop 局部 Guard）、ContextBuilder/模型窗口与输出 token（上下文安全 Guard）、`ParallelExecutionPolicy.max_concurrency`（执行容量 Guard）。它们并非统一 Run Budget。本次 `max_step_starts` 是 Scheduler 总启动额度；Legacy `max_steps` 保留，二者不双写。并发容量在 `execute_ready` 取 Policy 与 Budget 的更严格值。

## 3. Budget 所有权与架构
`ChatService` 为每个 Run 创建一个 `BudgetLedger`，通过 `RunContext` 注入；没有全局账本。Scheduler、ParallelExecutor 与迁移的 Router 模型/Tool 边界使用同一实例。

## 4. RunBudget
不可变字段：step/model/remote model/tool/input/output/total token/cost/retry 的上限、elapsed、concurrency。`None` 为不限；整数拒绝 bool/负数；时间有限且大于零；成本是整数 cost units。

## 5. BudgetUsage
不可变非负计数；`total_tokens` 不得小于 input + output，以表达独立总量剩余额度。

## 6. BudgetReservation
包含安全 UUID、类型、可选 step id、预留 Usage、UTC 创建时间；没有 Prompt、参数或响应。

## 7. BudgetLedger
一 Run 一 Ledger，`threading.Lock` 保护 committed、active reservation；锁内没有 await，也不改 AgentState/RunStatus。

## 8. BudgetSnapshot
Snapshot 给出 budget、committed、reserved、扣除二者后的 remaining、monotonic elapsed、有效剩余时间、活跃数和耗尽维度。

## 9. Reserve / Commit / Release
`reserve` 在同一临界区检查并加入预留；`commit` 移除预留并加入 actual，未用部分自然返还；`release` 只用于开始前。未知、重复、过期凭据均失败。Provider Usage 缺失时以保守预留并标记 `ESTIMATED`，不记零或伪造精确值。

## 10. 并发预算原子性
两个线程各预留 `model_calls=1`（限额 1）或 total token=700（限额 1000）都只允许一个成功。

## 11. Scheduler Step Budget
标准 `claim_ready(..., budget_ledger=...)` 在任何 STARTED 前批量预留稳定拓扑前缀；STARTED 成功后只提交成功数量，部分失败释放余量。唯一 Step 记账边界是 Scheduler STARTED；Executor 不记 Step。

## 12. Model 调用预算
知识专家及单 Agent 最终流式回答在真实 `generate` 前预留 model/input/output/total，并在请求已发出后 finally 保守结算。当前基础 Profile 未配置 remote 成本/远程标记，故该 Legacy 路径不自动计 remote/cost；部署装配后应补齐。

## 13. Tool 调用预算
`AgentRouter._prepare_answer_messages` 的已注册 Tool 真正调用前预留 `tool_calls=1`，finally 结算，失败也计数。Tool planner 模型调用及其他非 Router Tool 入口尚未迁移。

## 14. 时间与并发预算
Ledger 使用 `time.monotonic()`，有效剩余取 Budget 与 RunContext deadline 的 min；仅在 reserve/snapshot 边界拒绝新工作，绝不主动取消运行中任务。

## 15. 模型成本配置
新增 `ModelCostProfile`（Profile 装配来源）：固定调用、每千 input/output cost units、remote 标记、估算延迟，均为非负整数；未配置成本明确为 0 配置值，不推断真实货币价格。

## 16. Budget 参与 Model Selection
Selection 先 Force、Context/能力，再用 `BudgetPolicy` 过滤，随后按 SPEED/QUALITY/COST 排序。Policy 只读 Snapshot，不记账、不调用模型。

## 17. 首选模型与最终模型
Decision 保留 capability preferred 与 selected 字段及预算调整/质量披露/确认字段；当前兼容决策两者相同。FORCE_REMOTE 不可行时 fail closed，绝不静默本地。完整降级确认 UI 尚未实现。

## 18. Budget Exhausted 与 StopReason
Ledger/Scheduler/Selection 均不改状态；AgentLoop 捕获 `BudgetExceededError` 并映射 `RunEventType.BUDGET_EXHAUSTED`/`StopReason.BUDGET_EXHAUSTED`。

## 19. 重点 Bad Case
### Bad Case 1：并发任务同时检查预算造成超支
- 类型：假设构造。
- 触发条件：两个线程先读剩余再各发请求。
- 故障表现：超过单次调用额度。
- 根因分析：检查与占用不原子。
- 修复方案：Ledger 锁内 reserve。
- 回归测试：多线程 model/token 测试。
- 对应知识点：临界区。
- 面试表达：预留把授权与检查合并。
- 当前状态：已防护。
### Bad Case 2：取消后 Reservation 泄漏
- 类型：假设构造。
- 触发条件：请求未开始即取消。
- 故障表现：remaining 永久减少。
- 根因分析：未 release。
- 修复方案：开始前 release。
- 回归测试：release/重复 release。
- 对应知识点：资源生命周期。
- 面试表达：凭据只可结算或释放一次。
- 当前状态：组件已支持。
### Bad Case 3：实际 Token 高于预估
- 类型：假设构造。
- 触发条件：Provider 返回更高 usage。
- 故障表现：后续额度被误判。
- 根因分析：估算不是实际。
- 修复方案：有 Usage 时 actual commit，无 Usage 保守预留。
- 回归测试：commit 小于预留。
- 对应知识点：保守记账。
- 面试表达：不把未知伪装成零。
- 当前状态：已支持接口。
### Bad Case 4：预算降级后声称质量不变
- 类型：假设构造。
- 触发条件：远程不可行。
- 故障表现：错误质量承诺。
- 根因分析：选择与披露脱节。
- 修复方案：Decision 质量取舍字段，Force remote fail closed。
- 回归测试：选择兼容回归。
- 对应知识点：诚实降级。
- 面试表达：能力硬约束优先。
- 当前状态：UI 未实施。
### Bad Case 5：Scheduler 和 Executor 重复记录 Step
- 类型：假设构造。
- 触发条件：两层各加 step starts。
- 故障表现：额度过早耗尽。
- 根因分析：边界不唯一。
- 修复方案：仅 STARTED 成功的 Scheduler 记账。
- 回归测试：Scheduler/Parallel 回归。
- 对应知识点：单一事实边界。
- 面试表达：Executor 只执行 Claim。
- 当前状态：已防护。
### Bad Case 6：使用系统时间计算持续时间
- 类型：假设构造。
- 触发条件：系统时钟回拨。
- 故障表现：时间预算增加。
- 根因分析：wall clock 非单调。
- 修复方案：monotonic。
- 回归测试：time exhausted 测试。
- 对应知识点：duration clock。
- 面试表达：UTC 用于审计，monotonic 用于时长。
- 当前状态：已防护。

## 20. 测试命令和结果
目标 pytest、unittest、compileall、diff check 已执行并通过；全仓 pytest 结果见本次变更记录。

## 21. 未完成事项和已知风险
不主动取消运行任务；Retry 字段未使用；无确认 UI；Legacy AgentLoop/Parallel Parent Runtime 未完全统一；未覆盖所有 Model/Tool 路径；Actual Token 依赖 Provider Usage；缺失 Usage 用估算；未持久化；仅单进程锁；成本配置需部署确认。

## 22. 面试表达
“我将 Run 预算建模成带 Reservation 的单进程 Ledger：先在锁内检查并预留，再在真实边界 commit/release，因而并发调用不会因快照过期而超支。”

## 23. 需要带回 ChatGPT 审查的信息
请审查：day11 将第 10 天并行场景加入原子预留；入口为 ChatService/RunContext/BudgetLedger；字段、锁、生命周期、committed/reserved/remaining、actual/estimated、Scheduler/Model/Tool 边界、monotonic 时间、并发/retry/cost 配置、Selection 三目标与 FORCE_REMOTE、preferred/selected、BUDGET_EXHAUSTED、已/未接入路径、测试和六项 Bad Case。需人工确认生产 Settings 的成本/remote Profile，以及是否将完整 Provider Usage 与所有调用路径迁移。

## 24. 补充审查：模型元数据、Actual 与流式边界

### 未配置 ModelCostProfile
缺失 `ModelCostProfile` 不再被解释为本地、免费或低延迟。只要 Run 设置 remote/cost 上限，或目标为 `COST_FIRST`，选择即以 `MODEL_BUDGET_METADATA_MISSING` fail closed；实际调用边界也重复检查。显式本地零成本必须带 `is_remote=False`，远程必须显式 `is_remote=True`，不会从 Profile ID 或模型名称推断。

### Actual 超 Reservation
commit 永远关闭已发生调用的 Reservation，并接受未截断的 Actual；Actual 超过总量或 input/output/cost 限制时 committed 如实增长、对外 remaining 压为 0、耗尽维度出现，下一次 reserve 失败。Actual 小于预留自然返还差额。commit 绝不因超限拒绝，从而不会泄漏 Reservation。

### total_tokens 权威语义
total_tokens 是 Provider 报告或保守估算的权威总量，允许包含没有单独拆分的额外 token；无 Provider total 时取 input + output。input、output、total 的 remaining 各自独立检查；成本只按成本 Profile 的 input/output 计价，绝不再把 total 重复相加。普通 Usage 拒绝 total 小于 input + output；仅 Snapshot 的独立 remaining 视图允许三个可用额度不满足该关系。

### 流式请求开始边界
`BudgetedModelStream` 把首次底层 `next()` 定义为请求已开始：创建惰性 generator 后未迭代即 close 会 release，完全不记调用/token/cost；首次迭代抛错、部分 chunk 后 close、正常完成均 commit，缺 Usage 时按 Reservation 的 ESTIMATED 保守结算。Router 最终流式回答使用该包装器；同步完整回答和 Tool planner 模型调用仍未迁移。

### preferred / selected 及竞态
新增测试证明：能力/质量首选 remote、local 满足硬能力且 remote 预算不足时，`DEGRADE` 返回 preferred remote、selected local、`DOWNGRADE_TO_LOCAL` 和质量取舍披露；FORCE_REMOTE 仍 fail closed。`REQUIRE_CONFIRMATION` 仅产生未执行 Decision（selected=None），不解析或调用 Client。Selection 只读 Snapshot，真实调用仍在 generate 前 Ledger reserve，因此旧 Snapshot 不授权调用。

### 补充测试结果
补充后的聚焦 pytest：54 passed，9 subtests passed；完整指定 pytest/unittest 命令的结果见本次提交验证。全仓 pytest 仍在 collection 时因缺少 `requests` 与 `langchain_chroma` 失败，未将环境问题掩盖为通过。
