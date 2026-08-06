# Stage 2.5 Operations Runbook

> 适用范围：默认 Coordinated Runtime（动态多 Agent）。本文只描述已实现的
> 合同行为与人工处置；不得把 `DELIVERED` 描述为用户确认阅读，不得自动重试
> 未知交付，不得由 Recovery 重发 final 或重写 Memory。

## 1. 通用判定原则

1. 先读 `RUN_COMPLETED`/`ERROR` 的安全分层事实：`delivery_status`、
   `final_step_status`、`memory_commit_status`、`safe_error_code`。
2. 再核对 Journal 与 EventChannel：OUTPUT 只有 `text_digest`/`text_length`，
   正文事实只能来自用户已见正文与 Memory 完整 exchange。
3. 任何“正文可能已送达”的场景禁止自动重发；任何“Memory 可能已提交”的
   场景禁止自动重写。
4. 一次 Run 只有一个 terminal outcome；`RUN_COMPLETED` 是重放终态的权威事件。

## 2. delivery failed（`FINAL_OUTPUT_DELIVERY_FAILED`）

特征：Gate `FAILED`；正文在 journal append 前失败，Journal 无 OUTPUT 记录；
Final Step `SUCCEEDED`；Run `FAILED`；Memory 未写。

处置：

- 允许安全重试：正文从未进入任何持久化事实，重试不会重复交付。
- 重试是新请求，不是自动重发：失败 Run 的 Gate 终态不可逆。
- 检查 EventChannel 健康后再重试。

## 3. delivery unknown（`FINAL_OUTPUT_DELIVERY_UNKNOWN`）

特征：Gate `OUTCOME_UNKNOWN`；OUTPUT 已 journaled（只有 digest）但 enqueue
失败；Final Step `SUCCEEDED`；Run `FAILED`；Memory 未写。

处置：

- **绝对不能自动重试**：正文可能已被消费者看到，重发会产生重复用户文本。
- 先检查当前对话是否已出现正文：
  - 已出现 -> 不需要重试，只修复观测/通道问题；
  - 未出现且确认通道确实丢弃 -> 以新请求重试（明确的人工判断，不是自动重发）。
- 前端文案提示“先检查当前对话，避免重复执行”。

## 4. delivered + Memory failed（`FINAL_OUTPUT_MEMORY_COMMIT_FAILED`）

特征：Gate `PUBLISHED`；正文已进入交付通道一次；Memory exchange 提交失败
（事务回滚，无半个 exchange）；Final Step `SUCCEEDED`；Run `FAILED`。

处置：

- 不重发正文：正文已经交付。
- Memory 由上层人工协调补写完整 exchange（user + assistant 成对），
  或接受不持久化；不能只补 assistant 单条。
- 同一 Run 的 `RunFinalMemoryWriter` 是 write-once，失败后拒绝再次调用；
  不要试图在 Runtime 内“再试一次”。

## 5. planning starvation

特征：`process_blocking_executor` 的 worker 被 specialist/其他任务占满，
新 Run 的 planning 在准入队列等待；`runtime_blocking_executor_pending`
有界（上限 8），`runtime_blocking_executor_wait_seconds` 记录等待。

判定：

- pending 有界、释放资源后恢复、Run deadline 命中归类 `DEADLINE_EXCEEDED`
  （不是 AGENT/Planning 错误）-> 保留为 P2 Known Limitation。
- 出现无限等待、队列无界、取消无法收敛、资源释放后不恢复 ->
  升级 P1 并阻塞发布。

处置：

- 为运行设置合理的 Run deadline，保证 starvation 时按 `DEADLINE_EXCEEDED`
  收敛。
- 检查是否误用共享 executor 长时间占用 worker（例如阻塞型 specialist）。
- 不要通过提高全局 worker 数或 timeout 掩盖容量问题。

## 6. recovery requires reconciliation

特征：RecoveryValidator 返回 `REQUIRES_RECONCILIATION` / `UNSUPPORTED`，
原因码如 `FINAL_OUTPUT_DELIVERY_UNKNOWN`、
`FINAL_OUTPUT_MEMORY_COMMIT_UNKNOWN`、`MEMORY_COMMITTED_WITHOUT_TERMINAL`、
`POST_PLAN_BINDINGS_NOT_RECOVERABLE`。

处置：

- Recovery 是只读验证器：不重发 final、不重写 Memory、不恢复
  Store/Bindings/OutputGate。
- 按 reason 人工协调：
  - delivery unknown -> 先确认用户是否已见正文；
  - Memory unknown -> 人工确认 exchange 是否已提交，避免重复写入；
  - Memory committed 无 terminal -> 只补 terminal 观测，不重写 Memory；
  - POST_PLAN 缺 bindings -> 该动态 Run 不能 resume，作为新请求处理。

## 7. terminal publication failure（`RUNTIME_TERMINAL_PUBLICATION_FAILED`）

特征：Run 状态已提交终态，但 `RUN_COMPLETED`（及可能的 `ERROR`）发布失败；
Coordinator 以该稳定错误码暴露；Journal 可能缺 terminal 事实。

处置：

- 运行本身已终结：不重新执行业务，不产生第二个 terminal 动作。
- 人工核对 Journal 缺失的 terminal 事件；若正文与 Memory 已完成，只补
  观测事实。
- 判断是否可安全重试：只有证明“正文从未发布且 Memory 从未提交”时才允许
  新请求重试；否则仅协调观测。

## 8. 绝对不能自动重试的清单

- `FINAL_OUTPUT_DELIVERY_UNKNOWN`（正文可能已见）。
- `OUTPUT_GATE_DUPLICATE_ATTEMPT`（Gate 已终态）。
- `FINAL_OUTPUT_MEMORY_COMMIT_FAILED`（正文已交付，Memory 单独协调）。
- Recovery `REQUIRES_RECONCILIATION` / `UNSUPPORTED` 任何分支。
- terminal publication failure（运行已终态）。
- `STEP_RESULT_LATE_COMMIT` / `STEP_RESULT_DUPLICATE_COMMIT`（once-write）。

## 9. 可安全重试（新请求）的清单

- `FINAL_OUTPUT_DELIVERY_FAILED`（journal append 前失败，无正文事实）。
- Planning/compile/schema 失败（无 PLAN_CREATED、无 STEP 启动）。
- `AGENT_STEP_FAILED` / `SYNTHESIS_FAILED` / `REQUIRED_DEPENDENCY_FAILED`
  （无 final 交付事实）。
- `DEADLINE_EXCEEDED` / `BUDGET_EXHAUSTED`（新请求 + 调整配置）。
