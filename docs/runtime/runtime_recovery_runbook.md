# Runtime Recovery / Reconciliation Runbook

## Capability Boundary

当前能力严格为 **Recovery validation only**。不提供自动 Resume、Replay、Tool Retry、Compensation 或 Step Result Rehydration。`RecoveryValidator` 只读 Snapshot + Journal 并返回 immutable assessment；它不写 AgentState、不调用 Model/Tool/Retrieval、不生成 replay plan。

## Authority and Preservation

生产恢复权威输入仅为原始 `RunSnapshot` 与 `JournalRecord`。开始诊断前冻结副本并保留原件；不得手工修改 SQLite、digest、sequence、watermark，不得从当前 Registry、Memory、adapter 或测试 fixture 回填历史事实。

边界必须分开：Historical Authority 只有 Snapshot 与 Journal；New Reconciliation Evidence 是独立人工审计记录、外部权威系统确认，以及审批后创建的新操作身份。人工结论写入外部工单或未来独立的 Incident / Reconciliation Record，作为后续人工操作输入。仓库当前没有 Reconciliation Store，本轮不实现 Store。该新记录不得改写原始 JournalRecord、已有 RunSnapshot、历史 AgentState，不得补造 `TOOL_COMPLETED`。

## Snapshot Check

1. 校验 snapshot schema version=1，未知版本为 `UNSUPPORTED/INCOMPATIBLE_SCHEMA`。
2. 重新验证 canonical digest，不匹配为 `CORRUPTED`。
3. 核对 run identity 与请求的目标 run 一致。
4. 核对 Journal watermark 不超前。
5. 检查 AgentState terminal/status/stop reason 组合是否合法。
6. 检查 SafeBudgetSnapshot 的 committed/reserved/remaining 投影。
7. 检查 activity counters：reservation、worker、model/tool/retrieval、event publication。
8. 非 quiescent、detached 或 unknown activity 进入 reconciliation，不推断安全 resume。

## Journal Tail Check

1. 从 snapshot watermark 后只读 tail；不把读取失败当空 tail。
2. 校验每条 journal/event schema、digest、run identity 与单调 sequence。
3. 检查 terminal 唯一且位于末尾；禁止补造 terminal。
4. 配对 Model/Tool/Retrieval started/completed facts。
5. 检查 Tool evidence schema、provider_started、side_effect_state、retry disposition 与 compensation state。
6. 未知版本、截断、gap、conflict 或损坏分别返回固定 RecoveryStatus/Reason，禁止升级写回。

## Assessment Interpretation

- `FAILED`：validation 操作本身未完成；不代表可重试业务。
- `UNSUPPORTED` / `INCOMPATIBLE_SCHEMA`：当前 reader 不支持；不得猜测字段。
- `CORRUPTED`：digest/记录损坏；保留原件并升级。
- `JOURNAL_GAP_OR_CONFLICT`：sequence/terminal 不可靠；禁止 replay。
- `REQUIRES_RECONCILIATION`：存在 side effect/activity/unknown outcome，需要人工确认。
- `RESUMABLE`：仅表示未来 resume 的安全前置可满足；当前系统仍不会 resume。
- `TERMINAL`：历史 run 已终态；不得重建第二 terminal。

## Tool Manual Reconciliation

当 `TOOL_STARTED` 存在而 `TOOL_COMPLETED` 缺失时：

1. 停止所有自动动作；不得调用 Tool。
2. 保留 Snapshot 和 Journal 原件及其 digest/identity。
3. 确认 Tool 是 read-only、idempotent 还是 non-idempotent。
4. 仅检查持久化 Completion Evidence；不使用当前 Registry 或测试 oracle。
5. 通过外部权威系统由人工确认副作用是否发生；不得把查询结果写回原 Journal。
6. 将人工结论记录为 `NOT_STARTED`、`COMMITTED` 或 `UNKNOWN` 的独立 Incident / Reconciliation Record；该记录属于外部工单或未来能力，不写回历史权威数据。
7. 由授权人员把该新证据作为输入，决定是否创建审批后的全新操作身份；非幂等 `COMMITTED/UNKNOWN` 默认不重试。
8. `RecoveryValidator` 始终只读，不自动执行 Tool/compensation。

升级条件：外部系统不可查询、证据冲突、补偿失败、non-idempotent outcome unknown、worker 仍 detached。相关测试：RC-15/16、`test_recovery_integration.py`、`test_recovery_tool_completion_gap.py`、`test_recovery_version_compatibility.py`。

## Durable Agent Loop v1 的执行恢复

本节专用于 root `execution_policy=durable-agent-loop.v1` 的当前执行聚合；上文 Snapshot/RecoveryValidator 的只读评估合同保持原适用范围。已有 fixed-plan、DAG、Triage 不套用 Loop 恢复规则。

接管先经原 RunControl claim，在 fenced hydrate 事务中递增一次 `resume_generation`（同 fence 重复 hydrate 不递增）。Frame/seed 缺失、未知版本、digest 或 pin 不匹配时保留事实并明确失败，不重新选择首轮 Memory/RAG。已接受决策不重选；同一 iteration 已完成模型 winner 可恢复复用，STARTED 无结果转 UNKNOWN，同轮最多一次 crash retry。`model_recovery_count` 仅计接管时仍 STARTED 的遗弃批次；已由 Router 收口的 transport UNKNOWN 计费但不算 crash。迟到 generation 不得提交 Decision、Observation、预算或 Final。

工具 STARTED/UNKNOWN 等待原 reconciliation lane，只读查证后才能消费 COMMITTED 结果；缺结果以 `LOOP_RESULT_UNAVAILABLE` 停止，禁止重执行取结果。审批与 UNKNOWN 等待保持 root ACTIVE / Step RUNNING，`suspend_loop` 保存 backoff 或 manual_required；scan 在有确认结果/审批裁决时恢复，取消/deadline 可越过 manual/backoff 进入控制收口。合法 bounded polling 使用同一持久 backoff 与绝对 deadline。

恢复将 `budget_operations` 未结算预留持久标记为 `SETTLED/RECOVERY_CONSERVATIVE` 并计入 consumed；后续接管不能再收费或退款。Final receipt 已存在则继续原 completion pipeline，重用原 event/cursor。终态只读查询或 seal Evidence，不恢复执行业务。关闭新 Loop admission 时须保留认识 Loop schema 的 worker；不得删表、清 receipt 或把活跃 Loop 改回 fixed-plan。

Loop 的 `persist` 意图随 root 冻结。交付后 Conversation exchange 仍由原 FinalMemoryWriter 提交；恢复可对同一 `run_id/exchange_id` 读取匹配且已 COMMITTED 的 PG exchange，只有 Agent、scope、user/assistant 正文均一致时才复用 receipt。不创建第二条消息对，不改变 fixed-plan 的重复提交行为。有界轮询以原工具 `committed_at` 和冻结最小间隔确定下次可调度时间。

### Context Snapshot 恢复

先部署增量 `0032_context_compaction` 再启用新 Policy；不重写旧 migration，也不改写旧活跃 Loop 的 Seed/config 身份。旧无 Policy Run 不强制绑定 Snapshot。Context Snapshot 缺失时新 Policy 明确失败，不回退旧上下文路径。

NEED_DECISION 先重建/验证原提交源并检查 no-progress；已有 OPEN 使用原 Snapshot、先恢复 completed DECISION winner。压缩先保存 PREPARED operation（精确 request/source/parent/cursor），原 Model Owner 提交响应/结算，再保存 RESULT_READY 验证候选，接受时原子推进不可变 Snapshot + Frame 引用 + Iteration 绑定。接管不创造新内容版本；completed COMPACTION 响应由新 Owner 校验复用，不重新调用/收费。旧 generation 的 finish/validate/accept 被拒绝；未完成压缩 Attempt 按原 UNKNOWN 保守结算，剩余额度最多一次，不增加业务 Iteration 的 model_recovery_count。

只有带可靠显式 Provider context 拒绝事实的窄 REJECTED 状态允许原 OPEN Iteration 一次安全重绑定；STARTED/UNKNOWN/COMPLETED 均不能据此重绑定。第二次拒绝停止，不新建 Iteration 规避预算；单纯413/超时仍属不确定。实际 usage 超出 Run 上限时保留原 Attempt 的 reported usage 与 budget failure，保守结算、不产生 winner。Snapshot/summary 不改写已接受 Decision、Observation、工具、Final Receipt 或 terminal。停止新 admission 不代表旧 Snapshot Run 可以交给不支持新 schema 的 worker。

## Memory 来源撤销后的恢复

恢复只读原 runtime_memory_bindings，验证当前 User/Scope/epoch/version/TTL；不得用当前用户猜旧 Seed 归属，不重新检索或修改 Seed/Snapshot。新 Model/Tool dispatch、Decision 接受和未提交 Final 被拒绝；原 Coordinator typed stop。STARTED/UNKNOWN 沿原 Owner 结算/reconcile/suspend，不重发。已有 Final receipt 保持 event/cursor；失效源 Exchange/Formation 跳过。pre-root Planner 缺可恢复正文时停止为 UNVERIFIABLE，不重新收费。旧无 Binding Run 先 drain，不能补造 STATELESS。灾备开放 Memory 前必须核验 operation/source suppression；未知删除水位的备份禁止直接启用 Memory。见 [合同](scoped_memory_lifecycle_v1.md)。

已提交 FORMATION 模型响应、候选尚未保存的进程退出窗口：rehydrated Step completion 重新装配原 SemanticMemoryFormation，按相同 operation 复用完成响应，不派发新模型或重复结算。USER_ASSERTION 独立来源由代码保证提取请求仅含原始用户消息；操作准入仍核验完整 Run 依赖，不以新 UUID 绕过已撤销来源。

## v2 Batch / Delegation 恢复

复用原 RecoveryCoordinator/Run lease；一次接管在唯一 ROOT 与绑定 Child 树上使用同一新 generation，旧 Owner 拒绝。已接受 Batch 不重选，COMMITTED 工具不重做，UNKNOWN/审批等待仍沿原 lane，完整组确认后才继续模型。WAIT_CHILD 优先推进原 Child，已有 ChildResult 直接消费；根 OutputGate 保持原 event/cursor。DELEGATION_ALLOCATION 未使用部分保持预留，不能当成崩溃 I/O 消费；安全收口后释放。详见 [合同](agent_execution_v2.md)。
