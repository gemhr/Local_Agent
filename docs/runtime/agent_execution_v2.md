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

## CI 调查的受信动态停止

仅 `ci_incident_investigator` 根 Frame 可冻结 `adaptive_stop=ci-investigation-stop.v1`。`CIInvestigationService` 使用的 canonical Definition 对新 Run 启用该可选策略；其他 Agent、查询 Fast Path 和没有该冻结配置的旧 Run 不启用。策略和 `coverage_rule=ci-observed-minimum.v1`、`reserve_seconds=15`、canonical SHA-256 digest 进入原 pinned_config/config_digest；旧 Run 恢复不从当前 Definition 补开新策略。

只在上一批已 OBSERVED、NEED_DECISION、下一轮尚未 OPEN，且无未决 Tool/HITL/Child 时判断。`ci_evidence.py` 与最终 `validate_report` 共用已读来源提取：COMMITTED Tool 正文、Action projection/source/result digest、同 Frame OBSERVED、同 incident/execution、AVAILABLE 原 manifest 必须相符。必要覆盖为 PARSER_LOG、ENVIRONMENT_RECORD、CASE_REQUIREMENTS，环境能力字段非空。覆盖不是根因正确性或质量 verdict，不替代原报告 Validator/后续 GT 评测。

Alembic `0042_loop_adaptive_decision` 为原 RuntimeLoopFrameRow 增加可空 JSONB `adaptive_decision`；原 LoopFrame DTO 同步支持。记录 version、policy_digest、observation_cursor、evidence_digest、effective_iteration、allowed_actions=FINAL_ONLY、reason、insufficient、decision_id。reason 为 COVERAGE_SUFFICIENT 或 BUDGET_RESERVE，decision_id 由其余字段的 canonical digest 派生。原 Repository 的锁、lease/fencing、resume_generation、row_version 先提交限制，再创建 Snapshot；重复提交读取原决定。数据库触发器禁止修改或清除非空决定，迁移不允许通过 downgrade 丢弃历史限制。

余时小于等于 max(15秒、冻结 Profile 最大 estimated_latency 的两倍)，或只剩一次模型额度/无剩余工具额度时，策略可停止调查。这只是保守限制，不保证最终模型完成；不把 Tool timeout 当平均耗时。至少一个真实已读来源才能进入不足报告终结；零来源返回 LOOP_EVIDENCE_INSUFFICIENT。原模型、Token、成本、轮次和 Deadline 准入仍生效，不能补发额度或额外轮次。

下一轮 Snapshot 保存同一 adaptive_decision，native options 为 `tools=[]/tool_choice=none`，structured 请求明确只允许 FINAL/REFUSAL；完整已观察组和来源保留。终结轮不新增压缩模型调用；无法容纳完整原始组时按原上下文失败合同停止。限制进入 messages/options/snapshot/request digest，不改已 OPEN 的快照。

Driver 在 prepare_batch/prepare_handoff 前拒绝 TOOL_BATCH/HANDOFF；原 accept_batch 在模型响应与 Snapshot 绑定检查后再次拒绝，不能创建 Action/Child/Tool。非法输出沿 LOOP_DECISION_INVALID 停止，不循环重问。最多一个新的逻辑终结轮，Provider fallback/recovery attempt 仍使用原有限预算。insufficient=true 时 FINAL 必须明确 INSUFFICIENT_EVIDENCE 且 uncertainties 非空；REFUSAL 沿原拒绝分支，不伪造完整诊断成功。

恢复校验决定/策略摘要、原 observation/source 及已冻结限制快照，继续使用原决定和绝对 Deadline。COMMITTED 结果不重读，STARTED/UNKNOWN 仍交原对账流程；到期优先失败。RESULT_READY 仍走原 StepCompletion/OutputGate，CI 业务报告在原投影流程校验。Runtime SUCCEEDED 不等于业务完整诊断成功，INSUFFICIENT_EVIDENCE 必须单独识别。

## Context / Evidence / 兼容

ContextSnapshotV2 按完整 Batch assistant+全部 results 分组；最近两批、任何写/未知分类批次及所有 Handoff 组 mandatory。旧纯只读批次整体替换为低信任摘要，不留下孤立 result。Seed 原样保留；验证只证明来源/结构/覆盖/预算，不声称语义无损。根/Child 共用每 Run 两次压缩、每操作两次 Attempt 的上限。

本地查询 `DurableExecutionRepository.read_agent_execution_evidence(run_id)` 返回独立 `agent-execution-evidence.v1`。只含 opaque refs/digests、固定枚举、计数及预算；缺来源、UNKNOWN、未完成、权限/计量/守恒证明不足为 PARTIAL。业务失败与 coverage 分开。未新增 Go transport/Evaluator。

Trace v2 无新字段；旧 loop/context/memory Evidence 读取 v2 明确 `UNSUPPORTED_EXECUTION_POLICY`。v1、fixed-plan、DAG/Triage 不自动启用 v2，不重写旧身份、Snapshot、Decision、Final Receipt 或 Run terminal。

限制：无并行 Child/递归/独立 Child Run/后台生存；不解析同批数据依赖；Tool Resource Key 许可仍是原单进程机制，不承诺跨 Worker 全局资源互斥。外部副作用确认仍依赖原业务幂等/receipt/reconcile；已在途 I/O 不可因撤销或取消抹除。不承诺真实远端多工具 Provider、模型拆解质量或生产容量/SLO 已验证。
