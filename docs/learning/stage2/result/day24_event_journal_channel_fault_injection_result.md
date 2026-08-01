# 第 24 天第三轮 A：Event / Journal / Channel Fault Injection

## 1. 本轮目标

本轮只接入 Event、Journal、Channel 的 request/test-scoped 确定性故障点，验证 Journal-first、Sequence、Terminal、Consumer Ownership、Tool Completion Gap 和历史 Event 兼容性。不调用 `RecoveryValidator`，不 Replay，不重跑 Model/Tool/Retrieval，不补发 Terminal，不接入 Snapshot、Recovery、Observability、Trace 或 Shutdown fault point。

## 2. 修改前 Event Publish 顺序

代码审计确认真实顺序为：

```text
RuntimeEventChannel 在 publish lock 内分配 identity / sequence
→ RuntimeEvent.from_draft() 构造不可变 Event
→ RunEventJournal.append(event)
→ 对新 JournalRecord 尝试 Observability dispatcher 投影
→ asyncio.Queue.put(event)
→ Consumer 在 __anext__ / drain loop 中 receive
```

`publish()` 返回只表示 Event 已完成 Journal-first append 和 Channel enqueue，不表示客户端已经 receive、编码或写入网络。

## 3. Sequence Owner

`RuntimeEventChannel` 是每个 Run 唯一的 sequence owner。sequence 在 Journal append 前、持有 publish lock 时分配；Event ID 同时由 `RuntimeEvent.from_draft()` 创建。append 前失败不推进 Channel watermark；append 成功后立即推进，后续 enqueue 失败也不得复用。Fault Controller 不分配 sequence，不创建 Event。

## 4. Controller 传递

真实传递路径为：

```text
CoordinatedRuntimeFactory.create_run_scope(fault_controller=...)
→ request-owned RuntimeEventChannel(fault_controller=...)
→ publish / receive / drain handoff seam
```

生产默认值是 `None`。`ApplicationRuntimeServices`、Journal 和模块全局均不缓存 Controller；Controller 不进入 payload、JournalRecord、digest、Wire、Trace 或日志。Run A 与 Run B 分别持有不同 Channel/Controller。

## 5. EVENT_BEFORE_JOURNAL_APPEND

接入点位于 Event identity/sequence 已构造、Journal 尚未调用的位置，仅在存在 Journal 时执行。命中后抛出固定安全的 `EventPublicationError(error_code="EVENT_PUBLICATION_FAILED", partially_persisted=False)`；异常保留内部不可变 Event 供事实审计，但 Journal 与 Channel 均无该 Event，业务不会由发布层重跑。

真实 Event 家族回归覆盖普通 Run/Step、Model、`TOOL_COMPLETED`、Retrieval 和 `RUN_COMPLETED`。

## 6. EVENT_AFTER_JOURNAL_APPEND

接入点紧跟同步 `journal.append()` 成功返回并推进 watermark 之后，早于 Observability 投影和 Channel enqueue。规则必须设置 `dangerous_window=true`。命中事实为：

```text
journal_record_count += 1
channel_event_count += 0
partially_persisted = true
```

SQLite 回归通过关闭并重新打开数据库证明记录已经 commit，进程内异常不能回滚它。

## 7. EVENT_BEFORE_CHANNEL_ENQUEUE

该点与 AFTER_APPEND 可区分：两者之间存在新 JournalRecord 的 Observability dispatcher 投影。`EVENT_BEFORE_CHANNEL_ENQUEUE` 位于投影尝试之后、`Queue.put()` 之前，也要求 `dangerous_window=true`。两点均真实支持，未在同一行重复执行。

在本项目生产装配中 Channel 总是带 Journal，因此该点失败也是 `partially_persisted=true`；裸测试 Channel 无 Journal 时则准确报告 false。

## 8. JOURNAL_BEFORE_TERMINAL_APPEND

只在 `event_type == RUN_COMPLETED` 时执行，位置是最终 `AgentState` 已由 `RunCoordinator` 提交、Terminal Event 已构造、Journal 尚未 append。命中后：

- `AgentState` 权威终态不回退；
- Journal/Channel 均无 Terminal；
- 不创建第二个 RunContext 或 Terminal；
- cleanup callback、Trace reset 和 Registry unregister 沿第 23 天既有 finally 路径继续；
- Coordinator 最终抛出固定 `RUNTIME_TERMINAL_PUBLICATION_FAILED`。

## 9. CHANNEL_BEFORE_RECEIVE

Transport 的 `__anext__()` 和 Drain loop 都在 `queue.get()` 前执行该点，支持 `RAISE_TYPED_ERROR`、`DELAY`、`BLOCK_UNTIL_RELEASED`。Delay/Block 与 Run cancellation、Channel abort 竞争；取消保持 first-wins reason。故障发生时尚未移除队列条目，Transport lease 释放后可由现有 Drain owner 接管，不写 RuntimeEvent Journal，也不 ack Event。

## 10. CHANNEL_BEFORE_DRAIN_HANDOFF

接入点位于 Transport 已释放 ownership、Drain 尚未取得 ownership 之间。等待期间 owner 明确为 `RELEASED`，不会出现双 Consumer；capacity=1 回归证明 Producer 可以暂时受 backpressure，Barrier 释放后 Drain 原子取得 owner 并解除阻塞。取消/abort 会取消 fault wait，并由现有 force-abort/close 合同收口。

## 11. Journal-first Matrix

| 窗口 | Journal | Channel | 业务调用 | Sequence |
| --- | ---: | ---: | ---: | --- |
| append 前失败 | 无 | 无 | 不重跑 | Event 已临时分配；watermark 不推进 |
| append 成功后失败 | 有 | 无 | 不重跑 | 已消费，不复用 |
| enqueue 前失败 | 有 | 无 | 不重跑 | 已消费，不复用 |
| enqueue 成功 | 有 | 有 | 一次 | 已消费 |
| receive 前失败 | 有 | 队列条目仍在，或 abort 按既有合同清空 | 不重跑 | 不改变 |
| terminal append 前失败 | 无 Terminal | 无 Terminal | 不重跑 | 不补发 |

矩阵使用真实 `RuntimeEvent`、InMemory Journal 与 SQLite Journal 验证，不只检查 Mock call。

## 12. Partial Publication

`EventPublicationError` 使用固定 `error_code/safe_message`，并明确携带 `partially_persisted`。内部 `event` 允许调用者确认 event_id、sequence 和 event_type；`repr` 不渲染 payload。部分持久化后不删除 Record、不 Replay、不用新 sequence 重发同一事实。Observability live dispatcher 在 AFTER_APPEND 故障时尚未收到投影，但 Journal 保留权威记录。

## 13. Tool Completion Gap Fixture

新增冻结的 `ToolCompletionGapFixture`，仅保存：

```text
started_event_present
completed_event_present
run_terminal_present
local_completion_evidence_present
provider_started
side_effect_state
retry_disposition
outcome_classification
started_event_valid
```

真实 B2b 回归由非幂等 Tool 执行一次，得到 `TOOL_STARTED` 已持久化、`TOOL_COMPLETED` 缺失、本地 completion evidence 已冻结、provider/side effect 各一次。另构造 `NOT_STARTED`、`COMMITTED + UNSAFE`、`UNKNOWN + OUTCOME_UNKNOWN`、本地 Evidence 丢失、Started Record 损坏五类安全 fixture。本轮未调用 RecoveryValidator。

## 14. Event Schema Compatibility

新 `ToolCompletedPayload` 使用 `result_present/result_digest` 区分无 Result 与空 Result；digest 来自 canonical JSON/结构化 ToolOutput digest，不使用 Python `repr`，相同内容（不同 key 顺序）稳定、不同内容不同，且不保存原始 Result。

真实审计发现：严格 evidence allowlist 原先会拒绝缺少新字段的历史 v1/v2 Tool Completed 记录。修复后读取端把两个新字段视为历史可选字段，缺失保持 Unknown；旧 safe payload 与旧 Record digest 不改写，也不查当前 Tool Registry 回填。新事件仍序列化出这两个字段。

## 15. SQLite Journal

覆盖 append 前 fault 无记录、append 后 fault 已 commit 且 reopen 可见、触发器制造 INSERT 失败后的事务 rollback、后续正常 commit、重复 Event ID、重复 Sequence、合法 gap、out-of-order、损坏 Record、close 后 append、多线程原子重复 append 和不同 Run 隔离。Fault seam 位于既有 Journal API 外围，没有绕过 `BEGIN IMMEDIATE`、digest 或 append-only 合同。

## 16. Terminal Uniqueness

`RunCoordinator` 仍是唯一 `RUN_COMPLETED` owner。对 terminal append 前、append 后和 enqueue 前三个窗口的实测均满足：

```text
RUN_COMPLETED journal count <= 1
RUN_COMPLETED channel count <= 1
```

前一窗口计数为 `0/0`；后两窗口为 `1/0`。Controller、Journal、Channel、HTTP 与 Adapter 均未创建替代 Terminal。

## 17. Cancellation / Disconnect

Receive Delay 在 Client Disconnect 取消后及时退出，second cancellation 不覆盖 first-wins reason；队列条目在 receive fault 时未被静默取走。既有 disconnect、cancel-and-drain、force-abort、shutdown 回归继续通过。Disconnect 后没有新增 SAFE_ERROR 网络输出，Registry 最终清理，所有本轮等待均受 blocker timeout、Run cancellation、Channel abort 或外层 drain timeout 约束。

## 18. Disabled Parity

Disabled Controller 不执行 evaluate match，计数为 `match_count=0, hit_count=0`。真实回归确认 Event 实例在 Journal 与 Channel 中保持同一 event_id/sequence/type，顺序、Terminal、close 与 consumer ownership 不改变。全量回归同时验证最终 Runtime Result 不变。

## 19. Run Isolation

Run A 的 RUN_STARTED append fault 只使 Run A 进入既有安全失败合同；Run B 正常成功并拥有独立 sequence、Journal records、Channel 和 Terminal。关闭 Run A 不关闭 Run B，Application-scoped Journal 不保存 Controller。

## 20. Security

FaultMatchContext 只使用 run_id SHA-256、event_type、component 和固定 operation_kind。Event publication error 的 `repr` 不含 payload；Completion Gap Fixture 的字段集合封闭且冻结。安全测试确认 Tool Argument、Tool Output、raw idempotency/resource key、provider 原始错误以及题目列出的敏感标记不会进入新增 Event、Journal、Wire 或 fixture。

## 21. Runtime 真实接入

接入发生在生产真实 `CoordinatedRuntimeFactory → RuntimeEventChannel` 路径；Run、Step、Model、Tool、Retrieval emitters 继续共享同一个 `RunEventEmitter/StepEventEmitter → channel.publish()` 入口。Observability dispatcher 继续消费成功 append 后的 JournalRecord 投影，没有接入 Observability fault point。

## 22. Legacy Boundary

未修改 Legacy 业务语义，未改变 Model/Tool/Retrieval 调用、Retry 或 Compensation。裸 `RuntimeEventChannel` 默认 Controller 为 None；旧 Event v1/v2、Journal v1/v2 和旧 Tool evidence 均保持可读。没有生产 Fault Settings/API/Header 或概率 Chaos。

## 23. Bad Case

### Bad Case 1：append 成功后异常回滚已提交记录

- 类型：假设构造
- 触发条件：SQLite append 已 commit，随后 AFTER_APPEND fault 命中。
- 故障表现：错误处理误删或回滚权威 JournalRecord。
- 根因分析：把跨事务 publication 误当成一个可整体回滚的原子操作。
- 修复方案：fault seam 放在 append 返回之后；不提供删除/回滚路径，异常标记 partially persisted。
- 回归测试：关闭并 reopen SQLite 后仍能按 event_id/sequence 读取记录。
- 对应知识点：事务边界、Journal-first、不可逆事实。
- 面试表达：提交后的 Journal 是权威事实，在线投递失败只能记录 partial publication，不能篡改历史。
- 当前状态：已防护并通过真实 SQLite 回归；不是生产事故。

### Bad Case 2：append 后 enqueue 失败时重新执行 Tool

- 类型：假设构造
- 触发条件：Tool side effect 已发生，Completion Event 已 append，但 enqueue 前失败。
- 故障表现：为了补在线事件重新调用 provider，产生重复副作用。
- 根因分析：混淆业务事实与事件 Transport 成功。
- 修复方案：Event fault 层只抛安全 publication error，不 Retry、Replay、Compensate 或调用业务。
- 回归测试：真实非幂等 Tool gap fixture 验证 provider 与 side effect 均为一次。
- 对应知识点：副作用幂等性、事实冻结、publication gap。
- 面试表达：发布失败不等于业务未发生，我保留冻结 Evidence 并禁止从 Transport 层重跑 Tool。
- 当前状态：已防护；假设风险，无生产事故证据。

### Bad Case 3：重新分配 Sequence 重发同一 Event

- 类型：假设构造
- 触发条件：Journal 已消费 sequence，Channel 未收到 Event。
- 故障表现：同一事实以新 event_id/sequence 再次 append，形成重复语义。
- 根因分析：让 Fault Controller 或 retry path 成为第二个 sequence owner。
- 修复方案：唯一 owner 仍是 Channel；partial failure 后 watermark 前进且不自动重发。
- 回归测试：失败 Event 为 sequence 1，下一独立 Event 为 sequence 2，Channel 只见后者。
- 对应知识点：单写者、单调 watermark、exactly-once fact identity。
- 面试表达：我不承诺跨介质原子投递，但保证序号不复用和同一事实不伪造重发。
- 当前状态：已防护；假设风险。

### Bad Case 4：Event Fault Controller 创建第二个 Terminal

- 类型：假设构造
- 触发条件：RUN_COMPLETED 发布窗口故障。
- 故障表现：Controller 补发另一个 RUN_COMPLETED，导致双终态。
- 根因分析：基础设施故障处理越权取得 Terminal ownership。
- 修复方案：Controller 只返回/抛出 fault decision；Terminal 仍只由 RunCoordinator 构造一次。
- 回归测试：三个 terminal 窗口的 Journal/Channel count 均不超过 1。
- 对应知识点：Terminal uniqueness、owner boundary。
- 面试表达：终态是状态机事实，不由 Journal、Channel 或测试 Controller 补造。
- 当前状态：已防护；假设风险。

### Bad Case 5：Terminal append 失败跳过 Registry cleanup

- 类型：假设构造
- 触发条件：最终状态已提交，JOURNAL_BEFORE_TERMINAL_APPEND 命中。
- 故障表现：RunHandle 永久留在 Registry，Trace/cleanup 泄漏。
- 根因分析：把清理放在成功发布 Terminal 之后的普通控制流，而不是 finally。
- 修复方案：复用第 23 天 finally cleanup；发布失败只设置固定 terminal publication error。
- 回归测试：异常后 AgentState 仍 SUCCEEDED，Registry 查询为空，in-flight publication 为零。
- 对应知识点：cleanup guarantee、authoritative terminal state。
- 面试表达：Terminal transport 失败不能撤销状态，也不能阻断资源清理。
- 当前状态：已防护；假设风险。

### Bad Case 6：Transport 与 Drain 同时 receive

- 类型：假设构造
- 触发条件：断开时 Drain 在 Transport 尚未释放 lease 前抢占。
- 故障表现：两个 Consumer 并发移除队列，顺序和 ownership 失真。
- 根因分析：handoff 缺少显式 RELEASED 中间态和锁内原子 claim。
- 修复方案：Transport 先释放；fault window 中 owner=RELEASED；Drain 随后在 consumer lock 内 claim。
- 回归测试：Barrier 阻塞 handoff 时 owner 为 RELEASED，第二 Consumer claim 被拒绝。
- 对应知识点：single consumer lease、atomic handoff。
- 面试表达：我把交接窗口显式建模成无 owner，而不是短暂双 owner。
- 当前状态：已防护；假设风险。

### Bad Case 7：Receive Fault 在出队后抛错造成静默丢失

- 类型：假设构造
- 触发条件：先执行 queue.get，再运行 fault seam。
- 故障表现：Consumer 报错但 Event 已被移除，Drain 无法恢复队列内事实。
- 根因分析：故障点位置晚于不可逆 dequeue。
- 修复方案：Transport/Drain 都在每次 queue.get 前执行 CHANNEL_BEFORE_RECEIVE。
- 回归测试：typed error 后 buffered_count 仍为 1，随后 Drain 正常清空一次。
- 对应知识点：receive boundary、at-most-once removal。
- 面试表达：接收故障必须发生在 dequeue 前，才能区分“没取到”和“取到后处理失败”。
- 当前状态：已防护；假设风险。

### Bad Case 8：Drain handoff Delay 导致永久 Backpressure

- 类型：假设构造
- 触发条件：capacity=1 已满，Drain handoff 被 Delay/Block。
- 故障表现：Producer 永久等待，关闭流程悬挂。
- 根因分析：Barrier 无界且不响应 cancellation/abort/timeout。
- 修复方案：FaultBlocker 有界，fault wait 与 cancellation/abort 竞争，外层 drain 仍有 timeout/force-abort。
- 回归测试：Barrier 期间 Producer 确认阻塞；释放后 sequence 2 入队并完成 close/drain。
- 对应知识点：bounded waiting、backpressure、structured cancellation。
- 面试表达：故障注入可以制造背压，但必须保留确定性释放和取消路径。
- 当前状态：已防护；假设风险。

### Bad Case 9：Tool Completion Gap Fixture 保存 Tool Output

- 类型：假设构造
- 触发条件：为了给下一轮恢复判断提供上下文，直接序列化本地 Tool Result。
- 故障表现：Tool Argument/Output、raw key 或 provider error 泄漏到 fixture。
- 根因分析：把恢复所需事实与业务正文混为一体。
- 修复方案：冻结 dataclass 只允许布尔值和安全枚举/分类字符串，不定义正文槽位。
- 回归测试：字段集合精确断言与敏感标记负向扫描。
- 对应知识点：data minimization、安全派生事实。
- 面试表达：恢复输入只保留决策所需 Evidence，不保留可重放的敏感正文。
- 当前状态：已防护；假设风险。

### Bad Case 10：旧 Event 缺新字段时用当前 Registry 回填

- 类型：真实发现
- 触发条件：代码审计并构造历史 v1/v2 Tool Completed evidence，safe payload 缺少 `result_present/result_digest`。
- 故障表现：严格 allowlist 拒绝历史记录；若用当前 Registry 回填还会伪造历史语义。
- 根因分析：新增字段未在读取端声明为历史可选字段。
- 修复方案：读取兼容层把两个字段缺失映射为 Unknown，不查 Registry，不修改旧 payload/digest。
- 回归测试：v1/v2 缺字段 payload 均通过严格验证；新 Event 仍包含字段。
- 对应知识点：schema evolution、unknown preservation、historical truth。
- 面试表达：兼容不是用今天的配置补昨天的事实，而是保留 Unknown 和原始 digest。
- 当前状态：已修复的真实代码兼容性缺口；由仓库测试发现，不是生产事故。

### Bad Case 11：旧 Record Digest 被新 Schema 改写

- 类型：假设构造
- 触发条件：读取旧记录后按当前字段集合重新计算并覆盖 digest。
- 故障表现：原始记录无法校验，或旧记录伪装成新 schema。
- 根因分析：把 read compatibility 实现成 destructive migration。
- 修复方案：JournalRecord 校验继续以存储的 schema/version/field set 为准；缺失字段不写回。
- 回归测试：既有真实 v1 SQLite fixture 保留 legacy digest；全量 Journal 回归通过。
- 对应知识点：append-only log、versioned digest。
- 面试表达：Schema 升级不能重写审计链，读旧版本必须用旧版本规则。
- 当前状态：既有保护继续有效；假设风险。

### Bad Case 12：Disabled Controller 改变 Sequence

- 类型：假设构造
- 触发条件：即使 disabled，也预创建替代 Event 或推进计数/序号。
- 故障表现：No Controller 与 Disabled Controller 的事件 identity/order 不一致。
- 根因分析：把 fault plumbing 放到 identity owner 之外或在 disabled path 执行副作用。
- 修复方案：Controller 只在已构造 Event 的 seam evaluate；disabled evaluate 立即返回且计数为零。
- 回归测试：Journal 与 Channel 引用同一 event_id/sequence/type，counter 为 0/0。
- 对应知识点：disabled parity、zero-impact instrumentation。
- 面试表达：禁用故障注入时必须是语义零开销路径，尤其不能改变 identity 与 sequence。
- 当前状态：已防护；假设风险。

### Bad Case 13：Run A Event Fault 关闭 Run B Channel

- 类型：假设构造
- 触发条件：Controller 或 Channel 被错误缓存为 application-global 当前对象。
- 故障表现：Run A 失败后 Run B 被 abort 或丢 Terminal。
- 根因分析：request-scoped 状态越界共享。
- 修复方案：Factory 每 Run 注入独立 Controller/Channel，Application services 不缓存当前 Controller。
- 回归测试：Run A 安全失败、Run B SUCCEEDED 且唯一 Terminal，关闭 A 不关闭 B。
- 对应知识点：request isolation、lifecycle ownership。
- 面试表达：应用服务可共享 Journal，但 fault state、counter 和 Channel 必须按 Run 隔离。
- 当前状态：已防护；假设风险。

### Bad Case 14：Disconnect 后继续输出安全错误

- 类型：假设构造
- 触发条件：receive fault 与 Client Disconnect 同时发生。
- 故障表现：Runtime 在已废弃 Transport 上继续编码/发送 SAFE_ERROR。
- 根因分析：把内部 publication/receive error 当成客户端仍在线的响应机会。
- 修复方案：保留既有 disconnect owner；取消 fault wait、释放 lease、Drain/abort，不新增 Wire Event。
- 回归测试：client disconnect、stream cancellation、graceful shutdown 与 first-wins reason 全部通过。
- 对应知识点：disconnect semantics、no-write-after-close。
- 面试表达：断开后的错误只用于内部安全收口，不能再尝试向客户端写响应。
- 当前状态：已防护；假设风险。

### Bad Case 15：Contract-only Event Point 被错误宣称为已支持

- 类型：真实发现
- 触发条件：本轮开始时审计 `FaultPoint` Enum 与真实调用链。
- 故障表现：五个 Event/Journal/Channel 名称已在枚举中，但 `RuntimeEventChannel` 没有 Controller 参数或 execute seam；配置规则不会命中。
- 根因分析：契约声明早于 Runtime 接线，文档若只检查 Enum 会产生假能力。
- 修复方案：沿 Factory 的 request scope 注入真实 Channel，并在 append/enqueue/receive/handoff 精确位置执行。
- 回归测试：每个点均有 hit counter、真实 Event/Journal/queue 状态断言和目标回归。
- 对应知识点：contract vs implementation、capability audit。
- 面试表达：我用调用链和状态变化证明 fault point 可用，而不是把枚举存在当成实现完成。
- 当前状态：真实接线缺口已修复；是代码审计发现，不是生产事故。

## 24. 测试结果

新增：

- `tests/test_event_fault_injection.py`
- `tests/test_journal_fault_injection.py`
- `tests/test_channel_fault_injection.py`
- `tests/test_event_partial_publication.py`
- `tests/test_tool_completion_gap_fixture.py`
- `tests/test_event_schema_compatibility.py`
- `tests/_event_fault_fixtures.py`

结果：

```text
目标 pytest: 168 passed, 4 subtests passed
全仓 pytest: 860 passed, 42 subtests passed
compileall: passed
uv lock --check: passed
git diff --check: passed（仅 Git 的 CRLF 提示，无 whitespace error）
```

## 25. 未完成事项

- 不实现 Event replay、automatic recovery 或 compensation。
- 不调用 RecoveryValidator；Completion Gap Fixture 留给第三轮 B。
- 不接 Snapshot/Recovery/Observability/Trace/Shutdown fault point。
- 不增加生产 Fault API/Header/Settings 或概率 Chaos。
- 不实现第 25 天内容。

## 26. 第三轮 B 接入点

第三轮 B 可直接消费 `ToolCompletionGapFixture` 与权威 Journal tail，区分 Started presence/validity、Completion absence、本地 Evidence presence、provider started、side effect state 和 retry disposition。B 必须继续以 Journal/Snapshot 的版本化事实判断，不得从当前 Registry 回填历史，也不得默认 Replay。

## 27. 需要带回 ChatGPT 审查的信息

| 问题 | 结论 |
| --- | --- |
| Event sequence owner | 每 Run 的 `RuntimeEventChannel` |
| Sequence allocation timing | publish lock 内、append 前 |
| Journal append owner | 注入的 `RunEventJournal`；调用 owner 是 Channel publish |
| Channel enqueue owner | `RuntimeEventChannel` |
| Publish success meaning | Journal-first append + enqueue；不代表客户端 receive |
| Before append fault | Journal 0、Channel 0、partially persisted false |
| After append fault | Journal 1、Channel 0、partially persisted true |
| Before enqueue fault | Journal 1、Channel 0、partially persisted true |
| After append / before enqueue support | 两者均真实支持，中间隔着 Observability 投影 |
| Terminal append fault | 权威状态保留、无 Terminal record/event、cleanup 完成、固定错误 |
| Receive fault | dequeue 前；Transport/Drain 均覆盖；支持 Raise/Delay/Block |
| Drain handoff fault | RELEASED 与 DRAIN 原子 claim 之间；无双 Consumer |
| Journal-first result | 各故障窗口均保持事实保真，不 Replay |
| Partial publication representation | `EventPublicationError.partially_persisted` |
| Business rerun count | 0；真实 Tool provider/business 总调用仍为 1 |
| Terminal journal/channel count | 各自均 <= 1；窗口结果为 0/0 或 1/0 |
| Completion gap fixtures | 5 类安全 fixture + 真实 Tool gap 输入 |
| Tool evidence compatibility | 新字段安全 digest；旧缺失字段为 Unknown |
| Old schema behavior | 保留旧 field set/digest，不查 Registry，不伪装新 schema |
| Result digest | canonical、稳定、内容敏感；不保存原 Result |
| SQLite transaction | append 后 fault 已 commit；INSERT failure rollback；后续可 commit |
| Disabled parity | counter 0/0，identity/order/terminal 不变 |
| Run isolation | A failure 不关闭/取消 B |
| Disconnect | first-wins reason，不在断开后输出错误 |
| Shutdown interaction | 复用既有有界 cancel/drain/force-abort，未接 shutdown fault |
| Fault data in event/journal/wire | 无 |
| 需要人工确认 | 无阻塞项；第三轮 B 的恢复判定策略不在本轮范围 |
