# 第 24 天第三轮 B：Snapshot / Recovery Fault Injection

## 1. 本轮目标

本轮先修正第三轮 A 的 Terminal pre-append 与 Event publication evidence 两个契约，再把五个确定性 Fault Point 接入真实 Snapshot/Recovery 调用链：`SNAPSHOT_BEFORE_SAVE`、`SNAPSHOT_AFTER_SAVE`、`SNAPSHOT_BEFORE_READ`、`RECOVERY_BEFORE_TAIL_READ`、`RECOVERY_AFTER_TAIL_READ`。同时以测试专用副本覆盖 Snapshot/Journal 损坏、截断、版本兼容、Tool Completion Gap、取消、Disabled parity 和隔离。

本轮只做读取、校验和安全判定，不实现自动 Resume/Replay、Step Result Rehydration、Tool 重执行/Compensation，也不调用 Model、Tool 或 Retrieval Adapter，不修改 `AgentState` 或 Tool side-effect state。

## 2. 第三轮 A 前置修正

真实代码审计发现并修复两项问题，二者都是仓库代码发现，不描述为生产事故：

1. `RUN_COMPLETED` 原来先执行 generic `EVENT_BEFORE_JOURNAL_APPEND`，再执行 `JOURNAL_BEFORE_TERMINAL_APPEND`。现改为互斥分支：Terminal 只执行 terminal-specific seam，普通 Event 只执行 generic seam。
2. `EventPublicationError` 原来保存完整 `RuntimeEvent`。现改为冻结的 `EventPublicationEvidence`，字段严格限定为 event_id、sequence、event_type、publication_stage、partially_persisted；错误对象没有 `.event` 或 payload 属性。

双规则、普通 Event、Terminal Event、Disabled Controller 和敏感 Payload 的回归均已补齐。

## 3. 修改前 Snapshot 链路

真实调用链为：

```text
RunCoordinator.create_checkpoint
→ CheckpointCoordinator.capture
→ pause SchedulerClaimGate
→ wait/check quiescence
→ RuntimeEventChannel.capture_journal_watermark
→ AgentState.snapshot_copy + BudgetLedger.snapshot + RuntimeActivityProvider.capture
→ RunSnapshot.create + verify_digest
→ SnapshotStore.save
→ InMemory insertion 或 SQLite BEGIN IMMEDIATE / INSERT / COMMIT
```

所有权审计结论：

- Snapshot capture owner：`CheckpointCoordinator`。
- Snapshot save 调用 owner：`CheckpointCoordinator`；持久化 owner：具体 `SnapshotStore`。
- Snapshot schema version owner：`RunSnapshot` / `SNAPSHOT_SCHEMA_VERSION`。
- 每 Run Snapshot version owner：当前没有。Store 以 snapshot_id、created_at 和 immutable payload 管理多份快照，没有单独递增 version；`SnapshotPublicationEvidence.snapshot_version` 因此为 `None`，没有虚构计数器。
- Digest owner：`RunSnapshot.create` 通过版本化 canonical payload 计算，Store 保存并在读取时复验。
- SQLite commit：`SQLiteSnapshotStore.save` 的 `INSERT` 之后显式 `COMMIT`；`SNAPSHOT_AFTER_SAVE` 位于该调用成功返回之后。

## 4. 修改前 Recovery 链路

真实调用链为：

```text
RecoveryValidator.validate / assess
→ SnapshotStore.get
→ snapshot schema / digest / cross-field / activity validation
→ persisted Plan 与 current Plan 分别计算 fingerprint
→ RunEventJournal.last_sequence
→ RecoveryValidator._read_tail 分页 read_after
→ JournalTailValidator.validate
→ checkpoint/tail consistency validation
→ LimitedJournalTailReducer.reduce
→ ToolRecoveryDecisionEngine.decide
→ RecoveryAssessment
```

`RecoveryValidator` 是只读判断器。Journal tail reader owner 是 `RecoveryValidator._read_tail`，底层记录由注入的 `RunEventJournal` 提供。判定只使用 Snapshot 与 Journal 中的持久字段；不会调用 Model/Tool/Retrieval，不修改 `AgentState`，没有 Replay Plan。

已有 `RecoveryStatus`、`RecoveryReason`、`ToolRecoveryDecisionStatus` 足以表达损坏、不兼容、证据不足和人工对账。本轮只最小增加四个固定失败原因，用于区分 Snapshot read 未完成、Tail read 未执行、Recovery validation 未完成及取消；没有新增动作型 Decision Enum。

## 5. Controller 传递

Controller 显式按 operation 传递：

```text
RunCoordinator.create_checkpoint(..., fault_controller=None)
→ CheckpointCoordinator.capture(..., fault_controller=None)

RecoveryValidator.validate/assess/assess_snapshot(
    ..., fault_controller=None, cancellation_token=None
)
```

生产默认值为 `None`。`SnapshotStore` 和 `RecoveryValidator.__init__` 都不缓存当前 Controller；Controller 不进入 Snapshot、Journal、RecoveryAssessment、AgentState、Wire 或全局 `ContextVar`。同一 Validator 的下一次 operation 不传 Controller 时恢复正常行为。

## 6. SNAPSHOT_BEFORE_SAVE

位置固定在 `RunSnapshot.create` 与 `verify_digest` 完成之后、`SnapshotStore.save` 调用之前。命中时：

- Store 无新记录，旧 Snapshot 原样保留；
- 不重 Capture、不推进虚构 version、不 Retry save、不写 Journal；
- 当前活跃 Run/AgentState 不修改；
- 返回 `CheckpointStatus.STORE_FAILED` 与固定 `SNAPSHOT_SAVE_INJECTED_FAILURE`；
- `SnapshotPublicationEvidence.partially_persisted=false`，对外不返回 snapshot_id。

InMemory、共享 SQLite、已有旧 Snapshot、取消和 Run 隔离均使用真实 Store 验证。

## 7. SNAPSHOT_AFTER_SAVE

位置固定在 `SnapshotStore.save` 成功返回之后、Checkpoint 成功结果返回之前。SQLite 此时已经 commit，是 `dangerous_window=true` 的部分持久化窗口。命中事实为：

```text
snapshot persisted = true
caller observed failure = true
partially_persisted = true
```

返回固定 `SNAPSHOT_SAVE_PARTIALLY_PERSISTED`、已提交 snapshot_id 与安全 `SnapshotPublicationEvidence`。错误路径不删除、不覆盖、不保存第二份、不重 Capture、不自动 Recovery。SQLite close/reopen 后记录仍可读；同一 Snapshot 再 save 服从原 Store duplicate 合同。

## 8. SNAPSHOT_BEFORE_READ

位置在显式 Recovery read 已开始、`SnapshotStore.get` 尚未执行。命中后 Store get count、Journal last/read count 均为零，返回 fail-closed `UNSUPPORTED + SNAPSHOT_READ_FAILED`，不使用缓存旧 Snapshot、不降级到其他 Store、不 Retry read。

该 fault 只适用于按 snapshot_id 读取的 `validate/assess`；调用者已经持有 `RunSnapshot` 并直接调用 `assess_snapshot` 时没有第二次 Snapshot read，因此不会虚构此 seam。

## 9. RECOVERY_BEFORE_TAIL_READ

位置在 Snapshot schema、digest、内部一致性、activity、checkpoint kind 与 Plan fingerprint 全部通过之后，且在 Journal `last_sequence/read_after` 之前。命中后：

- Snapshot identity 已安全保留；
- Journal 调用计数为零；
- 返回 `UNSUPPORTED + JOURNAL_TAIL_READ_NOT_EXECUTED`；
- reduced projection、Tool decisions、Replay/Resume action 均不存在；
- 不修改 `AgentState`，不调用任何业务 Adapter。

## 10. RECOVERY_AFTER_TAIL_READ

位置在 `_read_tail` 已完整读取并再次确认 last sequence 之后、`JournalTailValidator` 与最终 Decision 之前。命中后不会把已读 Tail 当成空 Tail，也不会重新读取；返回 `UNSUPPORTED + RECOVERY_VALIDATION_FAILED`，仅保留安全 snapshot/journal sequence 事实，不返回 reduced projection 或 Tool decisions。

该点不是持久化危险窗口，但 Raise/Delay/Block 同样可取消、有固定结果、无 Replay。

## 11. Snapshot Corruption

`CORRUPT_TEST_FIXTURE` 只由测试显式调用 test-only mutator，目标仅为 `tmp_path` SQLite payload/row。生产 Snapshot path、应用默认目录、真实运行 Store 和用户路径都没有暴露给 mutator。

覆盖矩阵：digest mismatch、unknown schema、missing field、run_id/snapshot_id mismatch、非法 Agent status、非法 Step status、非法 Budget、非法 Tool activity evidence、truncated payload、SQLite row digest damage。所有情况均 fail closed；未知 schema 返回 `INCOMPATIBLE_SCHEMA`，其余损坏返回 `CORRUPTED`。Validator 不用当前 `AgentState`、Tool Registry 或旧 Snapshot 修复坏字段，错误中没有原始 payload/path。

当前 Snapshot 不持久化逐次 `ToolRecoveryEvidence`；它只持久化安全 activity counters。真正的 Tool started/completed recovery evidence 位于 Journal。测试据此损坏 Snapshot 中实际存在的 `tool_attempts_active`，没有虚构 Snapshot 字段。

## 12. Journal Tail Corruption

测试使用真实 `RuntimeEvent → JournalRecord.from_event` 与 SQLite Event Decoder，不以 dict mock 替代记录。覆盖：连续 Tail、合法 numeric gap、重复 sequence、out-of-order、digest 损坏、未知 Event schema、payload allowlist 失败、Terminal 缺失、重复 Terminal、Terminal 后业务 Event、Started 无 Completed、Completed 无 Started、跨 Run、记录不高于 Snapshot watermark、Tail 截断。

判定规则：合法 gap 保留；sequence ownership/order 冲突返回 `JOURNAL_GAP_OR_CONFLICT`；digest/decoder 失败返回 `CORRUPTED`；未知版本返回 `UNSUPPORTED`；Terminal 冲突 fail closed；截断不能降级为空 Tail。Tail reader 只按 snapshot.run_id 查询，其他 Run 的记录不会跨入。

## 13. Tool Completion Gap Recovery

生产 Recovery 权威输入严格为：

```text
RunSnapshot + Journal tail
```

`ToolCompletionGapFixture.local_completion_evidence_present` 只用于测试 Oracle，不是 `RecoveryValidator` 参数。Started 已持久化而 Completed 缺失时，真实 Validator 从 Journal 生成 Tool evidence，返回 `REQUIRES_RECONCILIATION`，使用既有 `MANUAL_RECONCILIATION` 或历史证据场景的 `INSUFFICIENT_EVIDENCE`；所有 automatic action 均为 false。

关键真实性测试分别维护：

- `expected_real_world_fact`：本地冻结 Evidence 显示 side effect 已 `COMMITTED`；
- `durable_recovery_input`：重启后只有 Snapshot + Started Journal record。

本地 Evidence 存在或丢失不会改变 Validator 结果，因为它从未成为生产输入。损坏 Started record 返回 `CORRUPTED`，不能当成 Started 不存在。Completed 已持久化时，完成事实只来自 Journal，不从当前 Tool Registry 回填。

## 14. Recovery Decision

复用现有：`RecoveryAssessment`、`RecoveryStatus`、`RecoveryReason`、`ToolRecoveryDecisionStatus`。`RESUMABLE` 只表示未来恢复所需的静态前提满足，不等于已执行 Resume；`SAFE_RETRY_CANDIDATE` 也只是分析分类，`automatic_action_allowed=false`。

所有 Assessment 强制：

```text
automatic_resume_supported = false
model_replay_allowed = false
tool_replay_allowed = false
retrieval_replay_allowed = false
```

不创建 Replay Plan，不触发 Tool、Model、Retrieval、Compensation 或资源租约动作。

## 15. Version Compatibility

Snapshot 当前只有 v1。v1 canonical JSON round-trip 后 bytes 与 digest 稳定；读取与验证不写回 Store。未知高版本在当前字段解释前 fail closed 为 `SNAPSHOT_SCHEMA_UNSUPPORTED`。仓库没有 v0 reader，因此没有宣称不存在的旧 Snapshot 版本兼容；当前可验证的历史边界就是持久化 v1 按 v1 字段/digest 读取，不从当前 AgentState 补字段。

Journal Event v1/v2 均保持可读。历史 Tool evidence 缺少新 result 字段时保持 Unknown；未知 Event version fail closed。读取不会查询当前 Tool Registry、Model Profile、Prompt 或 Memory，也不会把旧记录迁移为新记录写回 Journal。

## 16. Partial Persistence

`SnapshotPublicationEvidence` 是冻结且 payload-free 的结构，只包含 run_id_digest、snapshot_version、schema_version、snapshot_digest、partially_persisted。当前没有 Snapshot version owner，因此 version 为 `None`。

before-save 为未持久化；after-save 为已持久化但调用者见失败。取消或超时发生在 after-save wait 时仍返回已提交 snapshot_id/evidence，不删除 commit；before-save 取消则 Store 为空。

## 17. Cancellation

Snapshot before/after save 的 Delay wait 会轮询 Run/operation/shutdown cancellation 与 deadline。before-save 取消不写入；after-save 取消保留已提交 Snapshot。Recovery 三个 read/tail 点的 Block 均响应 cancellation，返回 `RECOVERY_VALIDATION_CANCELLED`，不返回可恢复结论。

异步 Snapshot task 与同步 Recovery worker 都有确定性退出路径；未关闭的 blocker 仍有 timeout，Scope/controller close 只释放测试等待，不关闭 Store。

## 18. Disabled Parity

No Controller 与 Disabled Controller 比较结果：

- 固定 capture 输入下 Snapshot canonical bytes、digest、schema/version evidence 完全一致；
- 同一 snapshot_id 第二次 save 按 Store duplicate 合同，row count 仍为 1；
- RecoveryAssessment、reasons、Tail records 与 Journal read count 一致；
- Model/Tool/Retrieval/Replay 计数均为零；
- Disabled rule `match_count=0, hit_count=0`。

Controller 不参与 Snapshot payload/digest，也不进入 Recovery decision。

## 19. Run / Operation Isolation

共享 SQLite Store 上并发执行时，Run A before-save fault 不影响 Run B 正常保存；A 无记录、B 一条记录。关闭 Controller A 后 Store 仍可读取 B。Store 不缓存 Controller，counter 不跨 operation 共享。

共享 `RecoveryValidator` 上 Operation A 传入 fault controller 失败，Operation B 不传 Controller 时正常判定；Snapshot read 与 Journal tail 都按目标 run_id，错误 Run/跨 Run 记录不能读入其他 Run。

当前 Store 没有每 Run version counter，因此“不同 Snapshot Version 不串联”落实为 immutable snapshot_id/digest 与按 run_id 查询隔离，而不是新增虚构 version 状态。

## 20. Security

Fault context 只保存 component、operation kind、checkpoint kind、安全 digest 和计数。`EventPublicationEvidence`、`SnapshotPublicationEvidence`、RecoveryAssessment/Reasons 均不保存 Snapshot/Event payload、FaultRule、Recorder、SQL、路径、Tool Argument/Output、Prompt/Memory 或 provider 原始错误。

测试扫描以下标记不会通过新增错误/evidence 暴露：`SECRET_PROMPT_TEXT`、`MODEL_OUTPUT_SECRET`、`TOOL_ARGUMENT_SECRET`、`TOOL_OUTPUT_SECRET`、`RAG_CHUNK_SECRET`、`MEMORY_SECRET`、私有用户路径、provider secret error、raw idempotency/resource key、raw snapshot payload。

## 21. Runtime 真实接入

Snapshot save points 接入真实 `RunCoordinator → CheckpointCoordinator → SnapshotStore`；Recovery points 接入真实 `RecoveryValidator → SnapshotStore/RunEventJournal → TailValidator/Reducer/DecisionEngine`。测试使用 InMemory 与 SQLite 的真实实现、真实 RuntimeEvent/JournalRecord 和真实 RecoveryAssessment。

Fault Point 不只存在于 Enum：每个点都有命中 counter 与边界两侧状态断言。没有接入 Observability、Trace 或 Shutdown fault point，也没有生产 Settings/API/Header。

## 22. Legacy Boundary

未修改 Legacy 业务执行、默认 Snapshot 自动策略、Journal append-only 语义、Tool retry/compensation、Model/Retrieval 调用或 Runtime 状态机。没有 Step Result Rehydration：Snapshot 只保存结果存在性/摘要，不能重建业务正文；Validator 会以现有 `DEPENDENCY_OUTPUT_UNAVAILABLE` / `STEP_RESULT_REHYDRATION_UNSUPPORTED` fail closed。

第三轮 A 的旧 `.event` 调用已迁移为 `.evidence`。除这一明确契约修正外，No Controller 路径保持兼容。

## 23. Bad Case

### Bad Case 1：Terminal Event 同时经过两个同义 Pre-append Seam

- 类型：真实发现
- 触发条件：审计第三轮 A 的 `RuntimeEventChannel.publish`，发布 `RUN_COMPLETED` 且两个规则同时存在。
- 故障表现：同一物理 append 前窗口先评估 generic point，再评估 terminal-specific point，counter 和命中语义不唯一。
- 根因分析：Terminal 特化 seam 以追加调用实现，而不是与 generic seam 互斥分支。
- 修复方案：按 event type 选择唯一 pre-append point；Terminal 只走 `JOURNAL_BEFORE_TERMINAL_APPEND`。
- 回归测试：双规则下 generic counter 为 0、terminal counter 为 1；Disabled 时二者均 0/0。
- 对应知识点：故障点唯一性、物理边界、Terminal ownership。
- 面试表达：一个物理窗口只能对应一个可观测 seam，否则测试计数不能代表真实执行次数。
- 当前状态：仓库代码真实缺口已修复；由代码审计发现，不是生产事故。

### Bad Case 2：EventPublicationError 保存完整 RuntimeEvent

- 类型：真实发现
- 触发条件：审计第三轮 A 的 publication error 属性。
- 故障表现：调用方可通过 `.event.payload` 访问完整业务 Payload，安全边界依赖调用方自律。
- 根因分析：为审计 identity 直接保存了过宽的事件对象。
- 修复方案：替换为字段封闭、冻结、payload-free 的 `EventPublicationEvidence`。
- 回归测试：错误无 `.event`，Evidence 无 payload，敏感 Output 不进入属性或 repr。
- 对应知识点：最小披露、capability narrowing、不可变 Evidence。
- 面试表达：审计只需要 identity、sequence、stage 和持久化事实，不需要持有业务事件本体。
- 当前状态：仓库代码真实缺口已修复；没有证据表明曾形成生产泄漏事故。

### Bad Case 3：After-save Fault 删除已提交 Snapshot

- 类型：假设构造
- 触发条件：SQLite commit 后 `SNAPSHOT_AFTER_SAVE` 命中。
- 故障表现：错误处理试图恢复调用者视角，删除已经持久化的事实。
- 根因分析：混淆事务提交与 API 返回成功，错误追求跨边界伪原子性。
- 修复方案：保留 commit，返回 partially persisted evidence，不提供删除路径。
- 回归测试：fault 后 close/reopen SQLite 仍可读取唯一 Snapshot。
- 对应知识点：事务提交点、partial persistence、不可逆事实。
- 面试表达：提交后失败只能如实报告“已持久化但调用者见失败”，不能重写历史。
- 当前状态：已由机制与真实 SQLite 回归防护；是假设风险。

### Bad Case 4：After-save Fault 自动重新保存

- 类型：假设构造
- 触发条件：首次 save 已成功，after-save fault 被误判为 Store 未写入。
- 故障表现：生成第二个 Snapshot 或触发 snapshot_id/version 冲突。
- 根因分析：没有区分 save 调用失败与 save 返回后 publication failure。
- 修复方案：after-save 路径不 Retry、不重新 Capture；后续显式 save 服从原 Store duplicate/conflict 合同。
- 回归测试：fault 后 row count 为 1；同对象 save 返回 DUPLICATE。
- 对应知识点：retry safety、idempotency boundary。
- 面试表达：重试资格由持久层合同决定，故障注入层不能替调用方猜测提交结果。
- 当前状态：已防护；是假设风险。

### Bad Case 5：Snapshot 损坏后静默使用当前 AgentState

- 类型：假设构造
- 触发条件：Snapshot digest、字段或状态校验失败，但进程中仍有同 run 的 AgentState。
- 故障表现：Validator 用当前内存状态补洞并返回可恢复，历史事实被覆盖。
- 根因分析：混淆 recovery authority 与 live runtime cache。
- 修复方案：Snapshot 损坏直接 fail closed；Validator 没有 AgentState/Registry 输入。
- 回归测试：11 类损坏均返回 CORRUPTED/INCOMPATIBLE，未调用 Journal 或业务 Adapter 修复。
- 对应知识点：authority boundary、fail closed、historical truth。
- 面试表达：恢复只能相信可验证的持久事实，不能用今天的内存对象“修好”昨天的记录。
- 当前状态：已防护；是假设风险。

### Bad Case 6：未知 Snapshot 版本按当前版本解析

- 类型：假设构造
- 触发条件：存储记录声明 snapshot schema 999。
- 故障表现：先按 v1 字段解释，再因偶然字段兼容给出恢复结论。
- 根因分析：版本检查晚于语义解码或默认映射到 current schema。
- 修复方案：Recovery/Store 在当前字段判断前拒绝未知版本，不做 Registry 回填。
- 回归测试：未知高版本稳定返回 `INCOMPATIBLE_SCHEMA + SNAPSHOT_SCHEMA_UNSUPPORTED`。
- 对应知识点：schema negotiation、fail closed。
- 面试表达：未知版本不是“尽力解析”，而是缺少可证明的 digest/字段语义。
- 当前状态：已防护；是假设风险。

### Bad Case 7：损坏 Tail 被当成空 Tail

- 类型：假设构造
- 触发条件：Journal last sequence 表示存在记录，但 read_after 返回损坏或不完整页面。
- 故障表现：Validator 丢弃异常并按无新事件返回 RESUMABLE。
- 根因分析：把“读取失败/截断”和“权威空集合”合并。
- 修复方案：decoder/digest 失败返回 CORRUPTED；last/read 不一致返回 sequence conflict。
- 回归测试：digest damage、allowlist failure 和 truncated tail 均无 reduced projection/恢复结论。
- 对应知识点：absence vs unknown、log completeness。
- 面试表达：空 Tail 是一个经过完整读取证明的事实，读取失败不能冒充空。
- 当前状态：已防护；是假设风险。

### Bad Case 8：Started 无 Completed 时自动 Replay Tool

- 类型：假设构造
- 触发条件：Journal 只持久化 `TOOL_STARTED`。
- 故障表现：Validator 假定 provider 未执行并自动重跑，可能重复外部副作用。
- 根因分析：把缺失 completion 当成失败前事实，而不是 outcome unknown。
- 修复方案：返回 reconciliation/insufficient evidence；所有 automatic action flag 为 false。
- 回归测试：真实 Journal started-only assessment 不 Replay，Tool decision 仅作分析。
- 对应知识点：completion gap、at-least-once side effect、evidence insufficiency。
- 面试表达：Started 证明尝试开始，不证明副作用未提交；默认动作必须是停止和对账。
- 当前状态：已防护；是假设风险。

### Bad Case 9：测试本地 Completion Evidence 被当成持久化权威

- 类型：假设构造
- 触发条件：把 `ToolCompletionGapFixture` 直接传给生产 Validator。
- 故障表现：进程重启后不存在的本地对象改变恢复判定。
- 根因分析：混合 `expected_real_world_fact` 与 `durable_recovery_input`。
- 修复方案：Validator API 不接受 fixture；fixture 只作为测试 Oracle。
- 回归测试：本地 Evidence 存在/丢失两种 Oracle 对同一 Snapshot+Journal 得到完全相同 Assessment。
- 对应知识点：test oracle、durability boundary。
- 面试表达：Oracle 用来判断实现是否保守，不能反过来成为生产恢复数据源。
- 当前状态：已防护；是假设风险。

### Bad Case 10：重启后 Validator 猜出 COMMITTED

- 类型：假设构造
- 触发条件：真实 Tool 已提交，但 Journal 缺 `TOOL_COMPLETED`，只剩 Started。
- 故障表现：Validator 根据 Tool 类型、时间或经验猜测 side effect 为 COMMITTED/NOT_STARTED。
- 根因分析：把概率推断冒充持久化事实。
- 修复方案：durable evidence 保持 unknown/reconciliation，不吸收本地 COMMITTED Oracle。
- 回归测试：Oracle 显示 COMMITTED，但 Assessment 仍只由 Started Journal 决定。
- 对应知识点：epistemic safety、unknown preservation。
- 面试表达：真实世界可能已提交，但恢复系统若没有 durable proof，就必须明确说不知道。
- 当前状态：已防护；是假设风险。

### Bad Case 11：使用当前 Tool Registry 回填历史 Evidence

- 类型：假设构造
- 触发条件：历史 v1/v2 Tool record 缺少新 evidence 字段。
- 故障表现：当前 Registry 的幂等性/副作用配置被写成历史事实。
- 根因分析：把当前配置当作事件发生时的权威版本。
- 修复方案：缺失字段保持 None/Unknown，Decision 使用 INSUFFICIENT_EVIDENCE。
- 回归测试：历史 v1/v2 均可读，evidence version、side_effect_kind、idempotency_kind 保持 None。
- 对应知识点：temporal consistency、schema evolution。
- 面试表达：今天的 Registry 只能描述今天，不能补写过去一次调用的真实语义。
- 当前状态：已防护；是假设风险。

### Bad Case 12：Recovery Fault 修改 AgentState

- 类型：假设构造
- 触发条件：before/after tail fault 为了“回滚校验进度”操作 live state。
- 故障表现：只读验证改变正在运行的 Run 或 Step 状态。
- 根因分析：Recovery 分析器持有了执行状态机能力。
- 修复方案：Validator 只接收 immutable Snapshot、Plan、Journal 与 operation controller。
- 回归测试：fault/cancellation 前后 Snapshot digest 和外部 state 均不变，业务调用计数为零。
- 对应知识点：CQRS、read-only validator、capability isolation。
- 面试表达：恢复判定和恢复执行必须分层；本轮只允许前者。
- 当前状态：已防护；是假设风险。

### Bad Case 13：Recovery Controller 创建 Replay Plan

- 类型：假设构造
- 触发条件：Fault rule 命中后 Controller 直接选择 pending steps/tool retry。
- 故障表现：测试基础设施越权成为恢复编排器。
- 根因分析：把 fault decision 与 business recovery decision 合并。
- 修复方案：Controller 只 Raise/Delay/Block；Assessment 的自动动作字段永久为 false。
- 回归测试：三个 Recovery seam 的失败结果均无 projection/tool decisions/replay flags。
- 对应知识点：control-plane separation、least authority。
- 面试表达：Fault Controller 决定“在哪里失败”，不决定“失败后执行业务什么动作”。
- 当前状态：已防护；是假设风险。

### Bad Case 14：Disabled Controller 改变 Snapshot Digest

- 类型：假设构造
- 触发条件：disabled path 仍把 Controller/plan/counter 写入 Snapshot 或改变 capture timing fields。
- 故障表现：No Controller 与 Disabled Controller 的 canonical bytes/digest 不同。
- 根因分析：fault plumbing 侵入持久化模型。
- 修复方案：Controller 只作为 operation 参数，Snapshot schema 没有 fault 字段。
- 回归测试：固定 capture owner 输入后 bytes、digest、schema/version evidence、row count 完全一致，counter 0/0。
- 对应知识点：zero-impact instrumentation、determinism。
- 面试表达：禁用故障注入时，不只结果相同，持久化字节也必须相同。
- 当前状态：已防护；是假设风险。

### Bad Case 15：Run A Corruption/Fault 影响 Run B

- 类型：假设构造
- 触发条件：application-scoped SQLite Store 错误缓存 Run A Controller 或当前 run_id。
- 故障表现：A before-save fault 阻止 B 保存，或 A 关闭 Controller 时关闭共享 Store。
- 根因分析：把 operation-scoped fault state 放入 application-scoped persistence owner。
- 修复方案：Store API 不接收/保存 Controller；每次 capture 显式传递。
- 回归测试：共享 SQLite 并发时 A 失败无记录、B 成功一条；关闭 A Controller 后 B 仍可读。
- 对应知识点：scope hygiene、多租户隔离。
- 面试表达：可共享的是 Store，不可共享的是当前故障 operation 及 counter。
- 当前状态：已防护；是假设风险。

### Bad Case 16：Corrupt Action 修改生产 Store

- 类型：假设构造
- 触发条件：通过环境变量或用户路径把 mutator 指向应用数据目录。
- 故障表现：测试故障注入破坏真实 Snapshot/Journal。
- 根因分析：测试 mutator 缺少显式资源边界。
- 修复方案：生产 seam 不允许 `CORRUPT_TEST_FIXTURE`；测试直接持有 tmp SQLite 副本并显式调用 mutator。
- 回归测试：所有 corruption fixture 都位于 pytest `tmp_path`，生产 Store API 无 mutator 参数。
- 对应知识点：destructive test isolation、safe fixture ownership。
- 面试表达：损坏注入只能作用于测试拥有且可丢弃的副本，不能由运行配置选择任意路径。
- 当前状态：已防护；是假设风险。

### Bad Case 17：Started Record 损坏被降级为 Started 缺失

- 类型：假设构造
- 触发条件：TOOL_STARTED record 存在但 event digest 无效。
- 故障表现：Validator 忽略坏记录，按“从未开始”判断安全重试。
- 根因分析：校验失败被转换为过滤条件，而不是权威日志损坏。
- 修复方案：Tail validation 在 reduction 前验证每条真实 JournalRecord，损坏立即 CORRUPTED。
- 回归测试：损坏 Started 得到 `JOURNAL_RECORD_CORRUPTED`，tool evidence 为空且无自动动作。
- 对应知识点：fail closed ordering、evidence integrity。
- 面试表达：坏证据不是无证据；它意味着权威日志本身不可信，风险级别更高。
- 当前状态：已防护；是假设风险。

### Bad Case 18：读取旧版本后重写 Digest

- 类型：假设构造
- 触发条件：为了迁移旧 Snapshot/Event，在 Recovery read 路径按新 schema 重新序列化写回。
- 故障表现：审计历史改变，旧 digest 丢失或记录伪装成新版本。
- 根因分析：把兼容读取与 destructive migration 混为一体。
- 修复方案：Validator 全程只读；按存储版本校验，缺失新字段保持 Unknown。
- 回归测试：Snapshot v1 round-trip bytes/digest 稳定且 Store row count 不变；Event v1/v2 不写回。
- 对应知识点：immutable history、versioned digest、read migration boundary。
- 面试表达：兼容读取可以投影为内存 Unknown，但不能悄悄重写持久化历史。
- 当前状态：已防护；是假设风险。

## 24. 测试结果

新增：

- `tests/_snapshot_recovery_fault_fixtures.py`
- `tests/test_snapshot_fault_injection.py`
- `tests/test_snapshot_partial_persistence.py`
- `tests/test_snapshot_corruption.py`
- `tests/test_recovery_fault_injection.py`
- `tests/test_recovery_tool_completion_gap.py`
- `tests/test_recovery_tail_corruption.py`
- `tests/test_recovery_version_compatibility.py`

更新 Event publication、Journal fault 与 partial publication 回归以使用 `.evidence` 并验证 Terminal seam 唯一性。

```text
规定目标 pytest: 121 passed
本轮 Snapshot/Recovery 新增测试集合: 49 passed
全仓 pytest: 911 passed, 42 subtests passed
compileall: passed
uv lock --check: passed（Resolved 157 packages）
git diff --check: passed（仅 Git 的 CRLF 转换提示，无 whitespace error）
```

## 25. 未完成事项

- 不自动 Resume/Replay，不执行 Safe Retry Candidate。
- 不重跑 Model/Tool/Retrieval，不自动 Compensation。
- 不修改 AgentState 或 Tool side-effect state。
- 不实现 Step Result Rehydration 或 Final Output reconstruction。
- 不增加 Snapshot v0 reader；仓库没有该历史格式合同。
- 不接 Observability/Trace/Shutdown fault point。
- 不增加生产 Fault Settings/API/Header、概率 Chaos 或第 25 天内容。

## 26. 第三轮 C 接入点

第三轮 C 只应在既有 Observability/Trace/Shutdown 真实 owner 边界继续接线。不得复用本轮 Snapshot/Recovery Controller 为全局 Controller，不得让诊断系统改变 Snapshot/Journal authority，也不得借 Shutdown fault 实现自动 Recovery/Replay。可复用本轮的 operation-scoped 传递、safe evidence、dangerous window、取消和 Disabled parity 方法。

## 27. 需要带回 ChatGPT 审查的信息

| 问题 | 结论 |
| --- | --- |
| Terminal pre-append seam | Terminal 仅 terminal-specific；普通 Event 仅 generic |
| Event publication evidence | 冻结、payload-free，无 `.event` |
| Snapshot capture owner | `CheckpointCoordinator` |
| Snapshot save owner | 调用 owner 为 Coordinator；持久化 owner 为 Store |
| Snapshot version owner | schema version 属于 RunSnapshot；无每 Run version owner，evidence 为 None |
| Snapshot digest owner | `RunSnapshot.create` 的 canonical versioned payload |
| Before-save fault | 无新记录、无 recapture/retry/state mutation |
| After-save fault | commit 保留、caller 见失败、partially persisted true |
| Snapshot persisted after failure | 是；SQLite reopen 可读 |
| Before-read fault | Store/Journal read count 均 0 |
| Before-tail fault | Snapshot 已验证，Journal read 未执行 |
| After-tail fault | Tail 已完整读取，未进入 validation/decision |
| Corruption target | 仅 pytest tmp SQLite/内存副本 |
| Production store mutation | 无 |
| Snapshot corruption result | Unknown schema incompatible；其余损坏 fail closed |
| Tail corruption result | Corrupted/unsupported/conflict，绝不当空 Tail |
| Tool completion gap durable input | Snapshot + Journal tail |
| Local evidence authority | 仅测试 Oracle，不是生产输入 |
| Started-no-completed decision | Requires reconciliation / existing insufficient evidence 分类 |
| Committed oracle vs durable evidence | 现实可为 committed，durable judgment 仍 insufficient/reconciliation |
| Recovery auto replay | 无 |
| Recovery model/tool calls | 0 |
| Recovery AgentState mutation | 0 |
| Unknown snapshot version | fail closed |
| Old snapshot compatibility | 当前历史边界为 v1；按 v1 digest 读取，不补字段/写回 |
| Old event compatibility | v1/v2 可读，新字段缺失保持 Unknown |
| Partial persistence | `SnapshotPublicationEvidence` 明确表示 |
| Disabled parity | bytes/digest/assessment 一致，counter 0/0 |
| Run isolation | A fault 不影响共享 SQLite 上的 B |
| Operation isolation | Validator/Store 不缓存 Controller |
| Fault data in snapshot/journal/wire | 无 |
| 新增测试 | 8 个文件（含共享 fixture） |
| 目标 pytest | 121 passed |
| 全仓 pytest | 911 passed, 42 subtests passed |
| compileall | passed |
| lock check | passed（157 packages） |
| diff check | passed（仅 CRLF 提示） |
| 需要人工确认 | 无阻塞项；若未来需要真实 Snapshot version，须先定义独立持久化合同 |
