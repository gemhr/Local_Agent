# 阶段二第 19 天改造结果

## 1. 本次目标

本次为既有 Coordinated Runtime 增加 append-only Event Journal、Event ID
幂等追加、per-run 有序读取、幂等 Consumer 与 Replay 前置读取。没有实现
Run Recovery、Snapshot、State Reducer、自动 Replay、Event Sourcing、Kafka
或分布式 Journal。

目标链路已经落为：

```text
StateMachine commit
→ RuntimeEvent
→ RunEventJournal.append
→ RuntimeEventChannel enqueue
→ Consumer
```

## 2. 修改前 Event 流

修改前的事实如下：

- `RuntimeEvent.from_draft()` 是 `event_id` Owner，使用 `uuid4().hex`。
- 每个 `RuntimeEventChannel` 是所属 Run 的 global `sequence` Owner。
- `StepEventEmitter` 只拥有各 Step 独立的 `step_sequence`，不生成 global
  sequence。
- `RunCoordinator` 在 Run 状态提交为 `RUNNING` 后发布 `RUN_STARTED`。
- `ParallelExecutor` 在 Scheduler 已把 Step 提交为 `RUNNING` 后发布
  `STEP_STARTED`，在 State Machine 提交 Step 终态后发布
  `STEP_COMPLETED`。
- `RunCoordinator` 提交最终 Run 状态后，按需发布 `ERROR` 或
  `CANCELLATION`，最后发布唯一 `RUN_COMPLETED`。
- Model、Tool、Retrieval 的 Started 事件发生在真实外部调用前；Completed
  事件发生在对应 Attempt/Execution 已有结果后。
- `close()` 是保留队列、在 accepted publisher 之后追加 End Sentinel 的幂等
  正常关闭；`abort()` 清空实时队列、唤醒阻塞 Publisher，且不修改
  `AgentState`。
- `ChatService.stream_coordinated_agent_events()` 拥有 Run/Step Emitter；
  Model、Tool、Retrieval 通过同一个可选 `StepEventEmitter` 接入。
- 默认 `stream_chat()`、Legacy `AgentLoop` 和 Legacy `[[ORCH]]` 文本没有
  `RuntimeEvent`，因此也没有 Journal。
- 项目已有 `MemoryManager` 的 SQLite 使用，但它与消息/摘要领域、连接生命周期
  和 schema 耦合，未作为公共 Journal Store 复用。本次复用标准库
  `sqlite3`，建立独立 schema 和数据库文件。

## 3. RuntimeEvent 复用

Journal 只接受最终 `RuntimeEvent`，保留原有 `event_id`、`run_id`、
`trace_id`、`sequence`、`step_id` 和 `step_sequence`。Journal 不生成第二个
Event ID，不把 `RuntimeEventDraft` 当持久化事实，也不重新编号。

`RuntimeEvent.to_journal_dict()` 是 Journal 专用安全投影。普通 Transport
仍可使用原有 `to_safe_dict()`，二者职责不同。

## 4. Sequence Owner

global sequence Owner 没有迁移，仍为 per-run `RuntimeEventChannel`。修改后的
同一 `_publish_lock` 临界区执行：

```text
读取 Channel 当前 sequence
→ sequence + 1
→ RuntimeEvent.from_draft
→ Journal.append
→ 标记 sequence 已消费
→ Queue.put
```

Journal 成功后即使 Channel 随后 abort，该 sequence 也不会复用。Journal 与
Emitter 都不生成 global sequence。不同 Run 的 Channel 独立；序号必须严格
递增但允许 Gap。Channel 使用既有 Journal 时只读取该 Run 的
`last_sequence()` 作为初始水位，这不是第二套计数器，也不执行 Run Recovery。

## 5. RunEventJournal Contract

`RunEventJournal` Protocol 提供：

```python
append(event)
read_after(run_id, sequence, limit)
get_by_event_id(event_id)
last_sequence(run_id)
close()
```

公共接口没有 Update 或 Delete。`read_after` 只查询一个 Run、按 sequence
升序返回，并严格校验非负起始 sequence、正整数 limit、bool 和最大 limit
1000。

## 6. JournalRecord

`JournalRecord` 包含：

- `journal_schema_version`
- `event_schema_version`
- `event_id`
- `run_id`
- `trace_id`
- `sequence`
- `emitted_at`
- `journaled_at`
- `event_type`
- `component`
- `step_id`
- `step_sequence`
- `safe_payload`
- `payload_digest`
- `event_digest`

当前 `journal_schema_version=1`。版本与序号拒绝 bool；所有时间要求
timezone-aware UTC；JSON 递归校验并拒绝 NaN/Infinity。`payload_digest` 和
`event_digest` 使用 lowercase SHA-256。`repr(record)` 只展示身份、类型和
摘要，不展示 Payload。

## 7. SQLite / InMemory Store

实现了 `InMemoryRunEventJournal` 与 `SQLiteRunEventJournal`。

SQLite 主表关键约束为：

```sql
PRIMARY KEY (run_id, sequence)
UNIQUE (event_id)
```

Append 在 `BEGIN IMMEDIATE` 事务内完成 Event ID、Run sequence、最大 sequence
和 terminal 检查后插入。连接使用进程内锁保护，读取严格排序。测试覆盖同一
SQLite 文件关闭并重新打开后仍能识别 Duplicate。

生产 `server.py` 使用
`LOCAL_AGENT_EVENT_JOURNAL_DB_PATH`，默认路径是
`data/database/runtime_event_journal.db`，创建一个共享
`SQLiteRunEventJournal` 注入 `ChatService`，在应用 shutdown 时幂等关闭。

## 8. Append Idempotency

`JournalAppendStatus`：

- `APPENDED`：新记录成功追加；
- `DUPLICATE`：完全相同记录已经存在，没有新增记录。

新事件必须同时满足 Event ID 未出现、`run_id + sequence` 未占用、sequence
高于该 Run 当前最大值、Run 尚未 terminal。Gap 合法，不强制连续。

## 9. Duplicate / Conflict

完全 Duplicate 必须具有相同 Event ID、Run、sequence 和 Event Digest。

类型化错误包括：

- `EVENT_ID_CONFLICT`：同 Event ID 但内容、Run 或 sequence 不同；
- `SEQUENCE_CONFLICT`：同 Run + sequence 被另一 Event ID 占用；
- `OUT_OF_ORDER`：未知 Event ID 的 sequence 不高于当前最大值；
- `RUN_ALREADY_TERMINAL`：Run terminal 后继续追加；
- `JOURNAL_APPEND_FAILED`：安全序列化、连接或事务追加失败；
- `JOURNAL_CORRUPTED`：读取时结构、摘要或 terminal 不变量损坏。

错误消息不包含 SQL、数据库路径、原始异常、Payload 或 traceback。

## 10. Terminal 不变量

唯一 terminal Runtime Event 是既有 `RUN_COMPLETED`，没有新增 Journal 专用
终态事件。

- 一个 Run 最多一个不同 terminal；
- 完全相同 terminal 再次 append 返回 `DUPLICATE`；
- 第二个不同 terminal 返回 `RUN_ALREADY_TERMINAL`；
- terminal 后任何新事件都被拒绝；
- 读取和 `last_sequence()` 会校验 terminal 必须是该 Run 最后 sequence；
- 损坏数据不做猜测或修复，统一 fail closed。

## 11. 安全 Payload

`RuntimeEvent.to_journal_dict()` 按 Payload 具体类型的字段 allowlist 逐字段
投影，不接受无约束业务 dict，也不使用 `asdict(event)`。

- Model：只保存 profile、候选/重试序号、路由调整、breaker 安全标识和安全
  错误码；不保存 Prompt、Messages、URL、API Key 或完整输出。
- Tool：只保存工具名、调用/Attempt 身份、重试与副作用状态、安全错误码和
  resource key digest；不保存 arguments、key 正文或 output content。
- Retrieval：只保存 retrieval 身份、query digest、计数、阶段、耗时、预算
  计数、状态和安全错误码；不保存 Query、Embedding、Chunk、Memory 或
  canonical path。
- Error：只保存 `safe_error_code`、`safe_message`、`component`、`fatal`。
- Cancellation：只保存安全 reason code 与 component。

## 12. Journal-first Transport

有 Journal 的 Channel 严格先 `append(event)`，成功后才 `Queue.put(event)`。

Journal 失败时：

- Event 不进入 Channel；
- 错误为安全 `JournalError`；
- Started 事件写入失败时不会进入对应 Model/Tool/Retrieval 调用；
- 该错误不会被当作普通 Provider/Tool/Retrieval 故障透明 retry/fallback。

Journal 成功、Channel 失败时：

- Journal 记录保留；
- Event 不回滚；
- 已提交的 Runtime State 不回滚；
- 已执行的业务不重跑；
- sequence 已消费且不复用；
- 可由未来/人工 `read_after` 读取，但本次不自动重放。

## 13. State / Event Consistency

真实 `RunCoordinator + ParallelExecutor` 集成测试使用
`StateAssertingJournal` 在 append 当下断言：

- `RUN_STARTED` 对应 `RunStatus.RUNNING`；
- `STEP_STARTED` 对应 `StepStatus.RUNNING`；
- `STEP_COMPLETED` 对应最终 `StepStatus.SUCCEEDED`；
- `RUN_COMPLETED` 对应最终 `RunStatus.SUCCEEDED` 与
  `StopReason.COMPLETED`；
- terminal safe payload 与已提交 State 一致。

正常路径是 commit-then-event-then-journal。Journal 是事实记录者，不是
`AgentState` Owner。

## 14. Crash Window

State 与 Journal 不在同一个持久事务，不能宣称原子一致：

1. State commit 后、Journal append 前崩溃，可能存在已提交状态但缺少事件；
2. Journal append 后、Channel enqueue/消费前崩溃，事件已持久化但未实时
   投递；
3. Handler 成功后、Checkpoint 保存前崩溃，Handler 可能在恢复投递时再次
   执行。

第二个窗口可由未来 Replay/Dispatcher 利用 Journal 修复投递；第一个窗口需要
未来更高层的一致性设计。本次没有掩盖这些窗口。

## 15. Idempotent Consumer

`IdempotentEventConsumer.consume(record)` 的顺序为：

```text
校验 record digest
→ 按 consumer_id + run_id 取得进程内处理锁
→ 检查 Event ID Duplicate
→ 检查 last sequence
→ 调用 Handler
→ Handler 成功后保存 Checkpoint
```

相同 Consumer + Event ID 只应用一次；不同 Consumer 与不同 Run 独立。并发
Duplicate 在共享 Store/Consumer 进程内只执行一个 Handler。Gap 允许；未知且
不高于 last sequence 的 Event 返回 `OUT_OF_ORDER`。

## 16. Checkpoint Store

实现：

- `EventConsumptionCheckpointStore` Protocol；
- `InMemoryEventConsumptionCheckpointStore`；
- `SQLiteEventConsumptionCheckpointStore`。

Checkpoint 只保存 `consumer_id`、`event_id`、`run_id`、`sequence`、
`processed_at`，不保存 Payload。SQLite 主键是
`consumer_id + event_id`，并对 `consumer_id + run_id + sequence` 建唯一
约束。Handler 抛错时不写 Checkpoint。

## 17. At-least-once / Exactly-once

语义声明：

```text
Journal storage: Event ID 幂等追加
Delivery: At-least-once
Consumer: Event ID 幂等消费基础
End-to-end business side effect: 不保证 Exactly-once
```

这里的 At-least-once 要求投递方可从 Journal 重读并允许重复交付；本日提供了
读取与幂等消费前置条件，但没有实现自动 Dispatcher/Replay。因此实时 Channel
失败后记录只保证留在 Journal，仍需未来或人工驱动再次投递。

Handler 成功、Checkpoint 前崩溃仍可能重复副作用。Handler 必须自身幂等，或
未来把业务状态与 Checkpoint 纳入同一个事务。代码和文档均不宣称 Exactly-once。

## 18. Replay 前置条件

`read_after(run_id, sequence, limit)`：

- 只查询指定 Run；
- 严格按 sequence 升序；
- 允许合法 Gap；
- 校验 Payload Digest、Event Digest 和 terminal 不变量；
- 损坏时 `JOURNAL_CORRUPTED`，不返回部分猜测结果。

该 API 只返回安全 `JournalRecord`。它不调用 Model、Tool 或 Retrieval，不修改
`AgentState`，不做 State Reduce，不创建 Snapshot，也不恢复 Run。

## 19. Runtime 真实接入

生产装配路径为：

```text
server lifespan
→ SQLiteRunEventJournal
→ ChatService
→ RuntimeEventChannel(journal=...)
→ RunEventEmitter / StepEventEmitter
```

因此 Coordinated Runtime 的 Run、Step、Model、Tool、Retrieval 与
OutputDelta 都经过同一个 Channel Journal-first 发布路径。测试还用同一
StepEmitter 写入所有事件族，证明没有为 Tool/Retrieval/Output 创建第二套
Journal。

## 20. Legacy 与未接入路径

默认 HTTP `/api/chat` 当前仍调用 `ChatService.stream_chat()` 的 Legacy 文本
流；它没有 `RuntimeEvent`，所以不 Journal。Legacy `[[ORCH]]` 仍由既有
`AgentRouter` 文本协议生成，没有单独 Journal。

本次只接入 `stream_coordinated_agent_events()` 及其上层 coordinated text /
result 路径。没有伪造 Legacy Event，也没有为 Legacy 文本建立第二套身份、
sequence 或日志。

## 21. 重点 Bad Case

### Bad Case 1：Journal 重新生成 Event ID

- 类型：假设构造；身份所有权错误
- 触发条件：Journal append 时再次调用 UUID 生成器
- 故障表现：实时 Event 与持久记录无法按同一身份去重
- 根因分析：把 Journal 错当成事件创建者
- 修复方案：只接受最终 `RuntimeEvent` 并原样保存 `event_id`
- 回归测试：append 后 Record Event ID 等于原 Event；重启后 Duplicate
- 对应知识点：Single identity owner
- 面试表达：持久层保存事实身份，不创造第二个事实身份
- 当前状态：已由类型边界和测试阻止

### Bad Case 2：Channel 与 Journal 分别生成 Sequence

- 类型：真实审计风险；顺序所有权错误
- 触发条件：新增 Journal 自增序号而未确认现有 Channel Owner
- 故障表现：传输顺序与持久顺序分叉
- 根因分析：同一聚合存在两个计数器
- 修复方案：Channel 在唯一 publish lock 中分配一次，Journal 只验证和保存
- 回归测试：Journal/Channel sequence 相同；并发发布回归通过
- 对应知识点：Per-aggregate sequencer
- 面试表达：Run 内顺序只能有一个写入 Owner
- 当前状态：已避免；Owner 未迁移

### Bad Case 3：相同 Event ID 不同 Payload 当 Duplicate

- 类型：假设构造；幂等键误用
- 触发条件：只检查 Event ID 存在，不比较 Event Digest
- 故障表现：篡改或调用错误被静默吞掉
- 根因分析：把幂等误解为无条件忽略重复键
- 修复方案：Run、sequence、Event Digest 全部相同才返回 `DUPLICATE`
- 回归测试：同 ID、不同 Payload 返回 `EVENT_ID_CONFLICT`
- 对应知识点：Idempotency versus conflict
- 面试表达：Duplicate 是同一事实重试，不是同一键任意覆盖
- 当前状态：已修复并覆盖 InMemory/SQLite

### Bad Case 4：Journal 保存 Prompt/RAG 正文

- 类型：真实安全边界；数据泄漏
- 触发条件：直接 `asdict(event)` 或持久化 Tool/Retrieval 业务对象
- 故障表现：Prompt、Query、Chunk、Memory 或 Secret 落盘
- 根因分析：缺少持久化专用 allowlist
- 修复方案：强类型逐字段投影；OutputDelta 只存长度和摘要
- 回归测试：正文不出现在 safe payload、Record repr 或 SQLite 读取结果
- 对应知识点：Data minimization
- 面试表达：观测事实与业务正文应采用不同数据分类
- 当前状态：已修复

### Bad Case 5：Journal 失败仍投递

- 类型：假设构造；可靠性顺序错误
- 触发条件：先 enqueue，再 append，或吞掉 append 错误
- 故障表现：Consumer 看见无法 Replay 的事件
- 根因分析：Transport-first
- 修复方案：Channel 锁内严格 Journal-first
- 回归测试：Failing Journal 后 buffered count 为 0
- 对应知识点：Write-ahead delivery
- 面试表达：先建立持久事实，再尝试易失实时投递
- 当前状态：已修复

### Bad Case 6：Handler 前写 Checkpoint

- 类型：假设构造；消费确认过早
- 触发条件：为了并发去重先保存已处理标记
- 故障表现：Handler 失败后 Event 永久被跳过
- 根因分析：把处理中当成已成功
- 修复方案：本地处理锁去重，Handler 成功后才 save
- 回归测试：Handler 连续失败两次均执行且无 Checkpoint
- 对应知识点：Ack after processing
- 面试表达：Checkpoint 表达成功应用，不表达开始尝试
- 当前状态：已修复

### Bad Case 7：重复 Event 改状态两次

- 类型：假设构造；非幂等消费
- 触发条件：At-least-once 重投但 Consumer 不检查 Event ID
- 故障表现：计数、通知或状态迁移重复
- 根因分析：把传输去重错误地当成业务去重
- 修复方案：Consumer + Event ID Checkpoint
- 回归测试：20 个并发 Duplicate 只有一个 Handler 执行
- 对应知识点：Idempotent consumer
- 面试表达：At-least-once 必须在消费端吸收 Duplicate
- 当前状态：已提供基础设施；业务 Handler 仍需自身幂等

### Bad Case 8：宣称 Exactly-once

- 类型：真实设计边界；语义夸大
- 触发条件：只看到 Journal 与 Checkpoint 就宣称端到端一次
- 故障表现：忽略 Handler 成功后、Checkpoint 前的崩溃窗口
- 根因分析：混淆存储幂等、消费去重与业务事务
- 修复方案：明确 At-least-once；业务副作用不保证 Exactly-once
- 回归测试：Handler failure 无 Checkpoint；文档审查
- 对应知识点：Exactly-once illusion
- 面试表达：没有共享事务就不能承诺端到端 Exactly-once
- 当前状态：已准确声明

### Bad Case 9：RUN_COMPLETED 后继续追加

- 类型：假设构造；聚合终态破坏
- 触发条件：append 只验证 sequence 未占用
- 故障表现：terminal 不再是 Run 最后事实
- 根因分析：未把 terminal 当聚合不变量
- 修复方案：事务内检查 terminal，读时再次验证
- 回归测试：Duplicate terminal 允许；第二 terminal 与任意后续事件拒绝
- 对应知识点：Terminal invariant
- 面试表达：终态不仅唯一，还必须封闭后续写入
- 当前状态：已修复

### Bad Case 10：强制 Sequence 连续

- 类型：假设构造；过强约束
- 触发条件：要求新 sequence 必须等于 max + 1
- 故障表现：合法 Gap 无法写入或消费
- 根因分析：混淆严格递增与连续
- 修复方案：只要求新 sequence 大于当前最大值
- 回归测试：1 后直接 append/consume 3 或 5 成功
- 对应知识点：Monotonic ordering
- 面试表达：Gap 可以表达已保留或未实时投递的序号
- 当前状态：已修复

### Bad Case 11：Replay 重新调用 Model/Tool

- 类型：假设构造；副作用重复
- 触发条件：把读取 Journal 等同于重新执行 Runtime
- 故障表现：重复请求 Provider、Tool 或 Retrieval
- 根因分析：没有区分事实 Replay 与命令重执行
- 修复方案：`read_after` 只返回安全 Record，不连接执行服务
- 回归测试：读取测试只访问 SQLite/InMemory，无 Fake Adapter 调用
- 对应知识点：Event versus command
- 面试表达：回放事实不能偷偷重放副作用命令
- 当前状态：已阻止；自动 Replay 未实现

### Bad Case 12：把 Journal 当成完整恢复系统

- 类型：真实范围边界；能力夸大
- 触发条件：有持久事件后直接宣称可恢复 Run
- 故障表现：没有 Snapshot/Reducer 时无法重建完整 `AgentState`
- 根因分析：忽略恢复所需版本、归约与副作用协调
- 修复方案：仅声明 Replay prerequisites 和 crash windows
- 回归测试：代码中不存在 restore/reducer/snapshot API
- 对应知识点：Recovery architecture
- 面试表达：可读日志是恢复前提，不等于恢复系统
- 当前状态：按范围明确未实现

## 22. 测试命令和结果

新增测试：

- `tests/test_event_journal.py`
- `tests/test_event_journal_integration.py`
- `tests/test_idempotent_event_consumer.py`

覆盖新 Event、Duplicate、两类 Conflict、Out-of-order、Gap、不同 Run、并发
Append、SQLite 重启、terminal、安全投影、损坏记录、Journal-first、Journal
Failure、Channel abort、单一 sequence Owner、业务不重跑、真实状态提交顺序、
首次/重复/并发消费、Handler failure、Checkpoint、Gap、多 Consumer 与多 Run。

已执行结果：

- 新增三组：`28 passed`
- 附件指定目标组合：`128 passed, 4 subtests passed`
- 全仓：`475 passed, 42 subtests passed`
- `uv run python -m compileall -q core tools tests`：通过
- `uv lock --check`：通过，`Resolved 157 packages`
- `git diff --check`：通过（仅 Git 的 LF/CRLF 工作区提示）

测试没有调用真实模型、网络、外部数据库、Chroma、真实 Tool 或 UI；SQLite
测试只使用临时本地文件。

## 23. 未完成事项和已知风险

- 没有完整 Run Recovery。
- 没有 Snapshot。
- 没有 State Reducer。
- 不是 Event Sourcing，Journal 不是 `AgentState` 唯一事实来源。
- State 与 Journal 不在同一个事务。
- 存在 State commit 后、Journal append 前的崩溃窗口。
- OutputDelta 正文不保存，只保存长度与摘要。
- RAG/Memory/Prompt/Tool 正文不保存。
- Delivery 采用 At-least-once 语义，但本日没有自动 Replay/Dispatcher。
- 不保证 Exactly-once。
- Consumer Handler 仍需自身幂等。
- 只接入 Runtime Event 路径。
- Legacy 文本流没有单独 Journal。
- SQLite/Journal 是本地单机能力。
- 不支持跨进程 Consumer 处理锁或分布式 lease。
- 不实现分布式日志、Kafka 或 Event Bus。

## 24. 面试表达

我保留了现有 RuntimeEvent 身份模型：Event ID 由 Event Envelope 创建，Run 内
global sequence 仍由 per-run Channel 单点分配。Channel 在同一发布锁内执行
Journal-first，再尝试有界队列投递；所以持久化失败不会投递，投递失败也不会
回滚已经提交的 State 或重跑业务。SQLite 用 Event ID 和 Run sequence 双唯一
约束实现事务型幂等追加，并在读取时校验规范 JSON 摘要与 terminal 不变量。
消费端在 Handler 成功后才写 Event ID Checkpoint，支持 At-least-once 下的
Duplicate 吸收，但 Handler 与 Checkpoint 不在同一业务事务，因此明确不承诺
Exactly-once。本日只建立 Replay 前置条件，不把 Journal 夸大为恢复系统。

## 25. 需要带回 ChatGPT 审查的信息

- 新增文件：`event_journal.py`、`event_journal_store.py`、
  `event_consumer.py` 和三组测试。
- 修改文件：`events.py`、`event_channel.py`、`model_invocation.py`、
  `tool_execution.py`、`retrieval_execution.py`、Runtime exports、
  `chat_service.py`、`settings.py`、`server.py`。
- Event ID Owner：`RuntimeEvent.from_draft()`。
- Sequence Owner：per-run `RuntimeEventChannel`；未迁移。
- Journal Schema：版本 1；`PRIMARY KEY(run_id, sequence)`；
  `UNIQUE(event_id)`。
- Append 状态：`APPENDED / DUPLICATE`。
- Duplicate：身份、Run、sequence、digest 全同。
- Conflict：Event ID、Run sequence、Out-of-order、terminal 均类型化。
- Terminal：唯一既有 `RUN_COMPLETED`，并封闭后续 append。
- Safe Payload：按强类型 allowlist。
- OutputDelta：只保存 `text_length / text_digest`。
- Journal-first：append 成功后才 enqueue。
- Journal Failure：不投递、不透明重试业务，返回安全错误。
- Channel Failure：Journal 保留、State 不回滚、业务不重跑、sequence 不复用。
- State/Event 顺序：commit → event → journal。
- Crash Window：State/Journal 非同事务；Handler/Checkpoint 非同事务。
- Consumer：Event ID 幂等基础，按 Consumer/Run 独立。
- Checkpoint：Handler 成功后写；无 Payload。
- Delivery：At-least-once 语义。
- Exactly-once：端到端不保证。
- Read API：`read_after` 有界、排序、摘要校验、fail closed。
- Replay 边界：无自动执行、无状态归约、无恢复。
- 真实接入路径：Server SQLite → ChatService → coordinated Channel。
- Legacy：默认文本流仍无 Runtime Event/Journal。
- 测试结果：全仓通过；最终命令结果需与本文件第 22 节最终复跑一致。
- Bad Case：十二项，已区分真实审计风险与假设构造。
- 人工确认问题：何时把默认 `/api/chat` 从 Legacy 切换到 Coordinated
  Runtime；何时实现实际 Journal Dispatcher；跨进程消费是否需要 lease。
- 后续建议：先设计可观测的投递 Dispatcher 与 crash reconciliation，再讨论
  Snapshot/Reducer；不得直接把本 Journal 当完整恢复能力。
