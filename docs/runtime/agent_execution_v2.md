# Durable Agent Execution v2

`durable-agent-loop.v2` 是受信 AgentDefinition/Composition 显式启用的执行策略；不是 v1 数据升级。仍是同一 Run、同一顶层 AGENT Step，由原 Coordinator、Scheduler、BudgetLedger、ToolExecutionService 和 OutputGate 拥有原职责。部署迁移至 `0034_agent_batches_delegations`；不自动修改业务数据库。

## 准入与决策

`LoopPolicyV2` 固定上界：每批1–4项、并发最多2、每批最多1项写；Child 深度1、累计最多2个、同时最多1个。缺省总 token/cost 上限各65536，模型/工具/retry/时长沿 v1 上限。ContextPolicyV2 默认开启，必须具备完整请求的 EXACT 或可靠 BOUNDED_ESTIMATE；HEURISTIC 拒绝新调用。所有配置在根 Seed/Frame 中冻结，恢复不热换配置。

Decision 仅有 FINAL、REFUSAL、TOOL_BATCH、HANDOFF。Native 完整有序 tool_calls 映射为一批，Structured 每项仅接受 tool_name/arguments，由代码生成 call_id。重复 call_id、空/乱 index、非法 JSON、额外字段、工具/参数不合法、混合 control/tool 调用整轮拒绝。v1 仍只接受原单调用表示。

受信 `business_options.delegation_allowlist` 列出可委派目标；目标需 enabled/delegation_allowed、非 synthesis/workflow，且显式配置 v2。原生控制函数 `__agent_handoff_v1` 不得与真实工具冲突，只能单独调用。模型只能给 target_agent_id/task，不能指定授权、预算或 Scope。

## 持久事实与工具屏障

T2 一次事务接受 winning model、Decision、assistant 和全部 `runtime_loop_actions`；提交前无 Tool prepare/dispatch。Action ID 来自 frame/iteration/ordinal，恢复不加入 generation；Tool 行仍由原 Tool Owner 创建并绑定。唯一约束、digest、CAS、lease/fencing 和原子消息组共同约束恢复。

明确 read-only、声明支持并发且资源独立的工具最多两项重叠；同资源按冻结 Action 顺序串行。写工具在只读前置确认成功后运行。FAIL_FAST/BEST_EFFORT 来自冻结 policy；确认失败后的未执行证明与已 STARTED 工作分开。审批等待、STARTED/UNKNOWN 或未确认 worker 关闭新准入，并阻止下一轮模型。UNKNOWN 只交原 reconciliation/manual lane，不能推断可重试。

完整结果按 Action 原序一次提交 Observation/cursor；结果从原 Tool COMMITTED 事实校验。NOT_EXECUTED 需事务中的未 STARTED 证明和固定原因/因果来源。并发使用局部 RunContext 视图及既有异步 worker/Tool 资源许可，不共享单 invocation 指针，不创建动态 DAG Step。

## Parent / Child

T2-H 原子提交 HANDOFF、Delegation、唯一 ChildFrame/Seed 和预算分配，Parent WAIT_CHILD。Child 使用受限 ToolResolutionSnapshot、模型能力交集及自己的不可变 Seed/Snapshot；不复制父完整 transcript，不检索或写入子 Memory，不运行 Formation。根 Principal/Memory Binding、取消 token 与 lease 共享，Child deadline 不超过根及受信局部上限。

Child FINAL 只提交最多16KiB正文的内部 ChildResult，信任为 AGENT_DERIVED_DATA；没有根 OutputGate、Conversation Exchange 或 terminal 权限。确认的局部失败/额度/局部到期可产生失败 Result；未决 I/O 不转换为失败。Parent 以 CAS 接受原 Result 为 Observation，然后必须继续决策。Result 已提交后崩溃，恢复直接消费原产物。

## 单一预算账本

`delegation:{id}` 是原 root budget_operations 的 DELEGATION_ALLOCATION；分配来自根剩余额度的一部分并保留父收尾额度，受目标更严上限约束。Child Attempt 把分配中未花费的 q 转为 Attempt 预留，根 reserved 不再加 q。结算记实际 a 一次，未使用部分退回分配；确定结束释放余款。根控制终止由原 terminal Owner 释放未使用额度。

正常维度满足 `allocation = consumed + inflight + unspent + released`，根 used 等于 Parent/Child Attempt 消耗之和。恢复只保守结算在途 MODEL/TOOL，绝不把未使用分配算作 I/O 消费；实际超额仍保留真实费用并停止后续准入。

## Context / Evidence / 兼容

ContextSnapshotV2 按完整 Batch assistant+全部 results 分组；最近两批、任何写/未知分类批次及所有 Handoff 组 mandatory。旧纯只读批次整体替换为低信任摘要，不留下孤立 result。Seed 原样保留；验证只证明来源/结构/覆盖/预算，不声称语义无损。根/Child 共用每 Run 两次压缩、每操作两次 Attempt 的上限。

本地查询 `DurableExecutionRepository.read_agent_execution_evidence(run_id)` 返回独立 `agent-execution-evidence.v1`。只含 opaque refs/digests、固定枚举、计数及预算；缺来源、UNKNOWN、未完成、权限/计量/守恒证明不足为 PARTIAL。业务失败与 coverage 分开。未新增 Go transport/Evaluator。

Trace v2 无新字段；旧 loop/context/memory Evidence 读取 v2 明确 `UNSUPPORTED_EXECUTION_POLICY`。v1、fixed-plan、DAG/Triage 不自动启用 v2，不重写旧身份、Snapshot、Decision、Final Receipt 或 Run terminal。

限制：无并行 Child/递归/独立 Child Run/后台生存；不解析同批数据依赖；Tool Resource Key 许可仍是原单进程机制，不承诺跨 Worker 全局资源互斥。外部副作用确认仍依赖原业务幂等/receipt/reconcile；已在途 I/O 不可因撤销或取消抹除。不承诺真实远端多工具 Provider、模型拆解质量或生产容量/SLO 已验证。
