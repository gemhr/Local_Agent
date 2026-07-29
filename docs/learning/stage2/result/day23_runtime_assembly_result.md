# 第 23 天第一轮：Runtime Assembly

## 1. 本轮目标

本轮只建立 Runtime Mode、应用级依赖容器、请求级 Coordinated Run Scope、生产 Snapshot Store 装配和基础生命周期合同。未迁移默认聊天入口，未修改 Streaming Protocol、Model/Tool/RAG 业务语义，也未加入自动 Checkpoint、Recovery 或 Replay。

## 2. 修改前默认入口

真实调用链为：

```text
/api/chat
→ server.chat_endpoint()
→ ChatService.stream_chat()
→ create_run_context()
→ LegacyAgentRouterDriver
→ AgentLoop.run_stream()
```

API 层只生成或校验 `run_id`，`RunContext` 和 `CancellationSource` 由 `ChatService.stream_chat()` 创建，不存在 API 与 ChatService 各创建一套 Run 身份的事实。

## 3. 修改前 Coordinated 入口

真实调用链为：

```text
测试或旁路入口
→ ChatService.run_coordinated_agent()
  或 ChatService.stream_coordinated_agent_events()
→ ChatService 内手工创建 RunContext / CancellationSource / AgentState
→ 手工创建 RuntimeEventChannel / Emitter / Scheduler / Executor
→ RunCoordinator.execute()
```

修改前 Coordinated 依赖散落在 `ChatService`：Journal、Observability、Gauge 是应用级引用；RunContext、CancellationSource、AgentState、Channel、Scheduler、Executor、Coordinator 是请求级对象。Snapshot Store 和 RecoveryValidator 未进入生产装配。

## 4. Runtime Mode Contract

- 固定 Enum：`ChatRuntimeMode.LEGACY`、`ChatRuntimeMode.COORDINATED`。
- 配置键：`CHAT_RUNTIME_MODE`。
- 临时默认值：`LEGACY`。
- 空值：显式使用临时默认值。
- 大小写：去除首尾空白后统一转为大写，再匹配 Enum。
- 未知值：`Settings.load()` 抛出 `ValueError`，启动失败。
- 请求捕获：`chat_endpoint()` 在请求入口调用一次 `selected_runtime_mode()`；Selector 保存不可变 Enum。
- 路由行为：第一轮只捕获合同，不按 Mode 改写默认执行路径。
- Fallback：不存在自动 fallback，也不接受业务代码自由字符串比较。

## 5. ApplicationRuntimeServices

`ApplicationRuntimeServices` 使用冻结 dataclass 保存不可重新绑定的应用级依赖引用，并用内部受控状态实现生命周期。它持有：

- EventJournal、ObservabilityDispatcher、StructuredRuntimeLogger；
- RuntimeMetricsRecorder、SpanRecorder；
- SnapshotStore、RecoveryValidator；
- ModelInvocationRouter、ToolExecutionService、RetrievalExecutionService；
- BlockingExecutor、WorkerTracker、RunRegistry；
- per-run `RuntimeActivityTracker` 工厂。

容器不提供 AgentState、RunContext、RuntimeEventChannel 或请求正文槽位，并在构造时拒绝把已知请求级对象放入应用级字段。`repr` 只输出 component、lifecycle state、snapshot enabled、安全版本和对象数量。

## 6. Request Run Scope

`CoordinatedRunScope` 每次创建并强持有：

- 唯一 RunContext 和 CancellationSource；
- AgentState、BudgetLedger、RuntimeActivityTracker；
- Plan、AgentStateMachine、Scheduler、ParallelExecutor；
- RuntimeEventChannel 和共享该 Channel 的 RuntimeEventEmitter；
- RunCoordinator 和单 Agent Driver；
- 由 Coordinator 显式绑定但不会自动运行的 CheckpointCoordinator。

RunContext 只暴露 CancellationToken；CancellationSource 只由 Run Scope 强持有。未执行 Scope 可以正常 close 或 abort，close 幂等；Scope 不进入应用容器。

## 7. CoordinatedRuntimeFactory

`CoordinatedRuntimeFactory` 是应用级无状态工厂，不缓存返回值。`create_run_scope()` 只调用一次 `create_run_context()`，然后把同一组 run_id、session_id、trace_id 传给 State、Channel、Emitter、Coordinator 和 Trace。

Channel 是唯一 Event Sequence Owner。构造中途失败时，Factory 注销 Gauge Channel 并 abort 已创建 Channel。生产 `ChatService` 的 Coordinated 旁路已使用该 Factory；无 Factory 的旧测试兼容路径暂时保留。

## 8. Dependency Ownership

| 对象 | Scope | Create Owner | Close Owner | 是否可复用 |
| --- | --- | --- | --- | --- |
| Model Client | Application | Server lifespan | ApplicationRuntimeServices | 是 |
| ModelInvocationRouter | Application | AgentRouter assembly | ApplicationRuntimeServices | 是 |
| ToolExecutionService | Application | AgentRouter assembly | ApplicationRuntimeServices | 是 |
| RetrievalExecutionService | Application | AgentRouter assembly | ApplicationRuntimeServices | 是 |
| EventJournal | Application | Server lifespan | ApplicationRuntimeServices | 是 |
| ObservabilityDispatcher | Application | Server lifespan | ApplicationRuntimeServices | 是 |
| StructuredRuntimeLogger | Application | Server lifespan | ApplicationRuntimeServices | 是 |
| RuntimeMetricsRecorder | Application | Server lifespan | ApplicationRuntimeServices | 是 |
| SpanRecorder | Application | Server lifespan | ApplicationRuntimeServices | 是 |
| SnapshotStore | Application | Server lifespan | ApplicationRuntimeServices | 是 |
| RecoveryValidator | Application | Server lifespan | ApplicationRuntimeServices | 是 |
| RunRegistry | Application | Server lifespan | ApplicationRuntimeServices | 是 |
| RuntimeEventChannel | Request | CoordinatedRuntimeFactory | CoordinatedRunScope | 否 |
| RunContext | Request | CoordinatedRuntimeFactory | CoordinatedRunScope/GC | 否 |
| CancellationSource | Request | CoordinatedRuntimeFactory | CoordinatedRunScope/GC | 否 |
| RunCoordinator | Request | CoordinatedRuntimeFactory | CoordinatedRunScope/GC | 否 |
| AgentState | Request | CoordinatedRuntimeFactory | CoordinatedRunScope/GC | 否 |
| Scheduler / Executor | Request | CoordinatedRuntimeFactory | CoordinatedRunScope/GC | 否 |
| BlockingExecutor | Application | Server lifespan | ApplicationRuntimeServices | 是 |
| WorkerTracker | Application | Tool/Blocking assembly | ApplicationRuntimeServices | 是 |
| RuntimeActivityTracker | Request | CoordinatedRuntimeFactory | CoordinatedRunScope/GC | 否 |

## 9. Snapshot Store 生产装配

生产 owner 是 Server lifespan。默认 `LOCAL_AGENT_SNAPSHOT_ENABLED=true`，路径来自独立配置 `LOCAL_AGENT_SNAPSHOT_DB_PATH`，默认位于安全数据目录的 `runtime_snapshots.db`。

启用时直接构造单例 `SQLiteSnapshotStore`；构造失败即 fail fast，不回退 InMemory。显式关闭配置时注入受控的 disabled capability（`snapshot_store=None`、`snapshot_enabled=False`）。测试使用 `InMemorySnapshotStore`。本轮没有自动 checkpoint，也没有把 Store 放入普通聊天热路径。

## 10. RecoveryValidator 装配

RecoveryValidator 由 Server lifespan 创建并放入 ApplicationRuntimeServices，引用同一个 SnapshotStore 和 EventJournal。它不自动执行、不读取当前活跃 Run、不调用 Model/Tool/RAG Adapter、不 Resume，也不进入默认聊天热路径。

## 11. Server Lifespan

状态固定为：

```text
STARTING → READY → SHUTTING_DOWN → CLOSED
```

实际初始化顺序为：

```text
Settings
→ Memory / Blocking Executor / Span Recorder
→ Knowledge Base
→ Model Clients / Model Profiles / Circuit Registry
→ AgentRouter / Model-Tool-Retrieval Services / Tool Registry
→ Metrics / Structured Logger / RunRegistry / Gauge
→ Observability Checkpoint Stores / Dispatcher
→ EventJournal / SnapshotStore / RecoveryValidator
→ ApplicationRuntimeServices
→ CoordinatedRuntimeFactory
→ ChatService
→ READY
```

应用对象同时放入 `app.state`，没有新增可变模块级请求缓存。

## 12. Initialization Failure Cleanup

`RuntimeInitializationStack` 在每个生产构造步骤成功后立即登记资源。任一步失败时：

1. 按构造逆序清理；
2. 单个清理失败不跳过其余资源；
3. 只返回固定安全错误码；
4. 不输出路径、Secret、对象 repr 或原始异常；
5. 不进入 READY。

ApplicationRuntimeServices 建立后，旧构造栈释放所有权，再由只登记容器的新栈覆盖 Factory/ChatService 构造窗口，从而避免容器和旧栈重复关闭同一资源。

## 13. Close Contract

ApplicationRuntimeServices 提供有界异步 `flush(timeout)` 和 `close(timeout)`：

- close 幂等；并发/重复调用返回同一 report；
- 每个去重后的对象最多调用一次 close/shutdown；
- 一个组件失败或超时后继续处理其余组件；
- 固定错误码为 `RUNTIME_COMPONENT_*_FAILED/TIMEOUT`；
- 生命周期最终进入 CLOSED；
- `try/finally` 保证 lifespan 用户区异常时仍运行关闭基础；
- 完整的第三轮 Shutdown 顺序仍未实现。

## 14. Security

- 容器和 Factory/Scope 的 repr 不包含 API Key、Provider URL、DB/Model/KB 路径、Prompt、Tool/RAG/Memory 正文或对象完整 repr。
- Snapshot 初始化失败不记录完整路径，也不记录原始异常/traceback。
- Shutdown 日志只记录组件、状态、固定错误码和对象计数，不打印活跃 run_id 列表。
- Mode 是有限 Enum，可作为低基数 `runtime_mode` 维度，但本轮未新增该指标。

## 15. Legacy Boundary

默认入口仍为：

```text
/api/chat → ChatService.stream_chat() → Legacy AgentLoop
```

即使 Selector 捕获到 COORDINATED，第一轮也不会切换、双跑或 fallback。CoordinatedRuntimeFactory 只供已有旁路和测试使用；第二轮才允许迁移默认路由。

## 16. Bad Case

### Bad Case 1：API、ChatService 各自创建 RunContext

- 类型：假设构造。
- 触发条件：API 先创建完整 RunContext，ChatService 又按同一请求创建第二套身份。
- 故障表现：run_id、trace_id、CancellationToken 和 Registry 记录发生分裂。
- 根因分析：请求身份 Create Owner 不唯一。
- 修复方案：API 只生成/校验 run_id；Legacy 由 ChatService 创建一次，Coordinated 由 Factory 创建一次。
- 回归测试：`test_default_chat_endpoint_captures_mode_once_but_stays_legacy`、`test_factory_creates_isolated_single_identity_run_scopes`。
- 对应知识点：Identity Ownership、Request Scope。
- 面试表达：入口可以提供 ID，但完整 RunContext 必须只有一个工厂 owner。
- 当前状态：已覆盖。

### Bad Case 2：应用容器保存当前 AgentState

- 类型：假设构造。
- 触发条件：为方便访问，把当前请求 AgentState 写入 ApplicationRuntimeServices。
- 故障表现：并发请求互相覆盖状态，且请求结束后仍被强引用。
- 根因分析：应用级与请求级生命周期边界混淆。
- 修复方案：容器无请求对象字段，并拒绝已知 per-run 类型；AgentState 只归 Run Scope。
- 回归测试：`test_repr_is_safe_and_container_rejects_per_run_state`、`test_factory_does_not_cache_request_scope_or_auto_checkpoint`。
- 对应知识点：Dependency Scope、State Isolation。
- 面试表达：共享的是无请求正文的服务，状态永远跟着 Run Scope 走。
- 当前状态：已覆盖。

### Bad Case 3：每请求创建 SQLite Journal

- 类型：假设构造。
- 触发条件：Coordinated Factory 在每次 `create_run_scope()` 时打开 SQLite Journal。
- 故障表现：连接数量增长、锁竞争加重，关闭 owner 不明确。
- 根因分析：把持久化 Store 错判为请求资源。
- 修复方案：Journal 由 lifespan 创建一次，Factory 只引用 ApplicationRuntimeServices 中的同一实例。
- 回归测试：`test_factory_creates_isolated_single_identity_run_scopes`、`test_factory_does_not_cache_request_scope_or_auto_checkpoint`。
- 对应知识点：Application-scoped Store、Connection Lifecycle。
- 面试表达：Run 独占 Channel 和 sequence，不独占持久化 Journal 实例。
- 当前状态：已覆盖。

### Bad Case 4：Snapshot Store 初始化失败后静默使用内存

- 类型：假设构造。
- 触发条件：SQLiteSnapshotStore 构造失败并被捕获，然后替换为 InMemorySnapshotStore。
- 故障表现：系统声称可持久恢复，但进程退出后快照全部丢失。
- 根因分析：容灾策略把能力降级伪装成成功。
- 修复方案：启用时 fail fast；只有显式配置关闭才进入 disabled capability。
- 回归测试：`test_snapshot_production_assembly_is_fail_fast_and_independently_configured`、`test_initialization_failure_is_projected_without_raw_exception`。
- 对应知识点：Fail-fast、Capability Truthfulness。
- 面试表达：持久化能力不能 silent downgrade，关闭必须显式且可审计。
- 当前状态：已覆盖；生产构造无内存 fallback。

### Bad Case 5：未知 Runtime Mode 静默回退 Legacy

- 类型：假设构造。
- 触发条件：`CHAT_RUNTIME_MODE=legacy-if-coordinated-fails` 或拼写错误。
- 故障表现：错误配置未被发现，实际路径与运维预期不同。
- 根因分析：把未知配置当默认值。
- 修复方案：只对 None/空白使用默认；未知非空值启动失败。
- 回归测试：`test_runtime_mode_rejects_unknown_value`、`test_settings_uses_exact_mode_key_and_fails_closed`。
- 对应知识点：Strict Configuration、Fail-closed。
- 面试表达：默认值只解决缺省，不负责掩盖非法值。
- 当前状态：已覆盖。

### Bad Case 6：Generator 执行中重新读取 Mode

- 类型：假设构造。
- 触发条件：每次 yield 前从环境或可变 Settings 重新读取 Mode。
- 故障表现：同一请求中途切换 Runtime，身份、协议和清理 owner 不一致。
- 根因分析：请求配置未做不可变快照。
- 修复方案：请求入口只调用一次 Selector；Selector 内部保存 ChatRuntimeMode Enum。
- 回归测试：`test_selector_is_an_immutable_request_snapshot`、`test_default_chat_endpoint_captures_mode_once_but_stays_legacy`。
- 对应知识点：Request Snapshot、TOCTOU。
- 面试表达：路由决策是请求级快照，不是流迭代级动态开关。
- 当前状态：已覆盖。

### Bad Case 7：Coordinated 初始化失败后请求双跑 Legacy

- 类型：假设构造。
- 触发条件：Factory 构造异常被捕获，随后自动调用 `stream_chat()`。
- 故障表现：部分副作用可能已经发生，同一请求又从 Legacy 重跑。
- 根因分析：把异常恢复错误实现成跨 Runtime 自动 fallback。
- 修复方案：Factory 失败清理后原样失败；默认入口本轮固定 Legacy，不存在双路径尝试。
- 回归测试：`test_default_chat_endpoint_captures_mode_once_but_stays_legacy`、`test_unexecuted_scope_can_be_aborted_safely`。
- 对应知识点：At-most-once Intent、Fallback Safety。
- 面试表达：跨 Runtime fallback 不是普通重试，未经副作用证明不能自动执行。
- 当前状态：已覆盖。

### Bad Case 8：构造中途失败未逆序关闭

- 类型：真实审计。
- 触发条件：修改前 lifespan 在 Model、Dispatcher、Journal 等任一后续构造点抛出异常。
- 故障表现：已创建 Session、线程池、后台 Dispatcher 或 SQLite 连接可能残留。
- 根因分析：初始化过程没有统一资源登记和 rollback owner。
- 修复方案：引入 RuntimeInitializationStack，逐项登记并在失败时逆序、best-effort 清理。
- 回归测试：`test_initialization_failure_cleanup_is_reverse_and_best_effort`、`test_initialization_failure_is_projected_without_raw_exception`。
- 对应知识点：Transactional Initialization、RAII/Exit Stack。
- 面试表达：启动装配也应像事务，成功转移所有权，失败按逆序回滚。
- 当前状态：已修复并覆盖。

### Bad Case 9：Close 一个组件失败后跳过其余组件

- 类型：真实审计。
- 触发条件：修改前 lifespan 中 `event_journal.close()` 等直接调用抛出异常。
- 故障表现：后续 checkpoint store 或其他资源不再关闭。
- 根因分析：关闭步骤缺少逐组件故障隔离和统一 report。
- 修复方案：ApplicationRuntimeServices 对去重目标逐个有界关闭，收集固定错误码并继续。
- 回归测试：`test_close_is_idempotent_and_continues_after_component_failure`。
- 对应知识点：Best-effort Shutdown、Error Aggregation。
- 面试表达：Shutdown 的单点失败不能夺走其他资源的清理机会。
- 当前状态：已修复并覆盖。

### Bad Case 10：应用级容器日志输出数据库路径

- 类型：假设构造。
- 触发条件：直接记录 dataclass 默认 repr 或完整依赖对象 repr。
- 故障表现：DB、模型、KB 或 Provider 配置路径进入日志。
- 根因分析：诊断输出没有安全投影。
- 修复方案：手写安全 repr，生命周期 report 只包含 component 和固定错误码。
- 回归测试：`test_repr_is_safe_and_container_rejects_per_run_state`。
- 对应知识点：Safe Projection、Observability Data Minimization。
- 面试表达：可观测性记录状态和计数，不序列化基础设施对象。
- 当前状态：已覆盖。

### Bad Case 11：Request Scope 被缓存到全局

- 类型：假设构造。
- 触发条件：Factory 把最后创建的 Scope、Context 或 Channel 保存为成员/模块变量。
- 故障表现：跨请求引用泄漏、取消错对象、Channel 串流。
- 根因分析：Factory 被误写成当前请求 Registry。
- 修复方案：Factory 只保存 Router、ApplicationRuntimeServices 和容量；每次返回全新 Scope。
- 回归测试：`test_factory_creates_isolated_single_identity_run_scopes`、`test_factory_does_not_cache_request_scope_or_auto_checkpoint`。
- 对应知识点：Stateless Factory、Request Isolation。
- 面试表达：应用级 Factory 可以复用，Factory 生产的 Scope 绝不能被它缓存。
- 当前状态：已覆盖。

### Bad Case 12：Snapshot Store 自动进入请求热路径

- 类型：假设构造。
- 触发条件：Factory 创建 Scope 或普通聊天完成时自动保存 Snapshot。
- 故障表现：请求延迟和失败面扩大，并提前引入未批准的 Checkpoint Policy。
- 根因分析：把“能力已装配”误写成“策略已启用”。
- 修复方案：仅把 Store 绑定到显式 CheckpointCoordinator；不调用 capture。
- 回归测试：`test_factory_does_not_cache_request_scope_or_auto_checkpoint`。
- 对应知识点：Mechanism vs Policy、Explicit Opt-in。
- 面试表达：先装配能力，再由独立策略决定何时使用；两者不能混为一谈。
- 当前状态：已覆盖。

## 17. 测试结果

- 新增测试：`test_runtime_mode.py`、`test_application_runtime_services.py`、`test_coordinated_runtime_factory.py`、`test_runtime_lifespan.py`。
- 本轮新增测试：20 passed。
- 本轮专项与默认入口补充回归合并执行：22 passed。
- 目标离线回归：118 passed，4 subtests passed。
- 附件命令中的 `tests/test_run_context.py` 在仓库中实际名称为 `tests/test_runtime_context.py`，目标回归使用真实文件名。
- 全仓 pytest：636 passed，42 subtests passed。
- `python -m compileall -q core tools tests`：通过。
- `uv lock --check`：通过，157 packages resolved。
- `git diff --check`：通过；仅提示工作区现有 LF/CRLF 转换策略。

## 18. 未完成事项

- 未迁移默认 `/api/chat`。
- 未实现新的 Client Disconnect 传播或修改流协议。
- 未实现完整 Shutdown 顺序和跨进程 Drain。
- 未修改 Model Retry/Fallback、Tool Call 或 RAG 业务语义。
- 未实现自动 Checkpoint、Recovery 或 Replay。
- 未实施第 24 天 Fault Injection。
- Knowledge Base 初始化仍沿用既有受控降级策略，不属于本轮 Snapshot fail-fast 合同。

## 19. 第二轮接入点

第二轮可在 `chat_endpoint()` 已捕获的 ChatRuntimeMode 上建立一次性分支：

```text
LEGACY      → ChatService.stream_chat()
COORDINATED → ChatService.stream_coordinated_agent_text()
```

分支必须保持单选、无双跑、无动态 fallback，并复用现有 CoordinatedRuntimeFactory。迁移前还需明确 Transport 兼容、错误映射和客户端断开 owner。

## 20. 需要带回 ChatGPT 审查的信息

- 确认第二轮是否允许在 `/api/chat` 按 Enum 正式分流。
- 确认 Snapshot 默认启用且失败即启动失败是否符合部署预期。
- 审查完整 Shutdown 是否应先停止准入、等待 Run，再关闭 Worker/Dispatcher/Store。
- 审查 Knowledge Base 的既有受控降级是否需要在后续统一为能力状态。
- 审查 Coordinated 路径迁移前是否需要新增协议兼容层，而不是直接复用纯文本 Adapter。
