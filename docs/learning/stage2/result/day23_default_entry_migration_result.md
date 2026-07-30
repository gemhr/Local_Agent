# 第 23 天第二轮：默认入口迁移

## 1. 本轮目标

本轮把 `/api/chat` 的默认生产入口从 Legacy 迁移到 Coordinated Runtime，并在不改变请求体、Media Type、Tool/Model/RAG 业务语义的前提下完成客户端兼容适配、唯一正文、唯一 Terminal、无跨 Runtime fallback，以及正常和普通失败路径的请求级资源清理。

本轮没有实现新的 Client Disconnect 语义、完整 Graceful Shutdown、自动 Checkpoint、自动 Recovery/Replay、Fault Injection 或第 24 天内容。

## 2. 第一轮合同修正

| 合同 | 修正结果 |
|---|---|
| Snapshot 默认策略 | `LOCAL_AGENT_SNAPSHOT_ENABLED=false`；显式启用才创建 SQLite Store |
| Recovery Capability | disabled 时 Store、Validator 均为 `None`，两个 capability flag 均为 `false` |
| Request Scope Owner | `CoordinatedRunScope` 是请求级对象的唯一强生命周期 Owner；第一轮文档已删除 GC Close Owner 表述 |
| Factory-less 路径 | `ChatService` 的生产 Coordinated 入口不再手工创建 Context、Channel、Scheduler、Executor 或 Coordinator |

测试需要 Coordinated Runtime 时，通过 `tests/_runtime_assembly_fixtures.py::make_coordinated_chat_service()` 显式注入 Factory。该 helper 位于测试代码，不构成生产 fallback。

## 3. 修改前 Wire Protocol

修改前 `/api/chat` 返回 `StreamingResponse(..., media_type="text/plain")`。线协议由两类自定义文本块混合组成：

- 普通正文字符串；
- `[[ORCH]]{json}\n` 控制行。

桌面端 `ApiWorker._emit_stream_payload()` 使用 `[[ORCH]]` 前缀和换行定位控制消息，JSON 解析成功后发到 `status_signal`；其他内容发到 `chunk_signal` 作为正文。Parser 会保留不完整前缀和未闭合控制行，所以 HTTP 任意拆分控制消息时仍能重组。

这不是标准 SSE：Media Type 不是 `text/event-stream`，wire 中也没有 `data:`、`event:` 或 SSE frame。

修改前 `RuntimeEventTextAdapter` 对 `OUTPUT_DELTA` 输出正文，对所有其他 RuntimeEvent 输出 `to_safe_dict()`。它没有 Terminal 状态，也不会拒绝重复 Terminal、Terminal 后事件或缺失 Terminal；Tool/Retrieval 控制投影也比桌面聊天实际需要的字段更宽。

## 4. Snapshot / Recovery Capability

默认配置：

```text
snapshot_store = None
recovery_validator = None
snapshot_enabled = false
recovery_enabled = false
```

显式设置 `LOCAL_AGENT_SNAPSHOT_ENABLED=true` 时，lifespan 才创建 `SQLiteSnapshotStore`，随后创建引用同一 Store 和 EventJournal 的 `RecoveryValidator`。Store 初始化失败由 `RuntimeInitializationStack` 安全 fail fast，不回退 InMemory。

请求热路径不调用 RecoveryValidator。本轮没有自动 Checkpoint、Recovery 或 Replay。

## 5. ChatRuntimeMode 默认值

`ChatRuntimeMode.parse(None)`、空字符串和 `Settings.load()` 的默认值均为 `COORDINATED`。显式 `legacy` 保持支持；未知非空值启动失败。

## 6. 默认 API 分流

`server.py::chat_endpoint()` 在请求入口调用一次：

```text
mode = service.selected_runtime_mode()
```

随后执行严格互斥分支：

```text
LEGACY      -> ChatService.stream_chat()
COORDINATED -> ChatService.stream_coordinated_agent_text()
```

同一请求只创建所选分支的 stream。请求执行期间不重新读取 Settings，不先探测另一分支，也不在异常后切换分支。

## 7. Coordinated 唯一生产入口

唯一生产链为：

```text
ChatService.stream_coordinated_agent_text()
-> ChatService.stream_coordinated_agent_events()
-> CoordinatedRuntimeFactory.create_run_scope()
-> CoordinatedRunScope.execute()
-> RunCoordinator
-> RuntimeEventChannel
-> ChatStreamCompatibilityAdapter
-> 客户端文本块
```

Factory 缺失返回固定 `RUNTIME_CONFIGURATION_ERROR`。Scope 构造失败返回固定 `RUNTIME_SCOPE_CREATION_FAILED`。`ChatService` 不再包含手工 Coordinated Runtime 装配分支。

## 8. ChatStreamCompatibilityAdapter

新增：

```text
ChatStreamCompatibilityAdapter
ChatStreamChunk
ChatStreamChunkKind
```

Chunk Kind 固定为：

```text
TEXT
CONTROL
SAFE_ERROR
```

兼容保留 `RuntimeEventTextAdapter.encode()`，但生产聊天入口使用有状态的 `ChatStreamCompatibilityAdapter`。Adapter 只负责事件映射、协议编码、安全错误和 Terminal 传输收口；不修改 AgentState、不决定 RunStatus、不执行 Retry、不调用 Model/Tool/RAG、不创建 RuntimeEvent。

## 9. OUTPUT_DELTA

`OUTPUT_DELTA.text` 是唯一正常正文来源。空 Delta 固定忽略。

成功路径不追加 `RunCoordinatorResult.final_output`、`AgentState.final_output`、`RUN_COMPLETED` payload、Legacy 聚合文本或控制消息。因此：

```text
wire_text == 按 sequence 顺序拼接所有非空 OUTPUT_DELTA.text
```

## 10. Control Message

控制事件使用固定 allowlist。每条控制消息由 Adapter 一次编码为完整的：

```text
[[ORCH]]{"run_id":"...","sequence":1,"event_type":"...","step_id":null,"payload":{...}}\n
```

投影不包含 trace_id、span_id、parent_span_id、event_id、时间戳、Prompt、Model Output、Tool 参数、RAG Chunk、Memory、路径、Secret、Snapshot 或 RecoveryAssessment。

Tool/Retrieval 仅保留桌面状态需要的低风险状态和计数字段；Tool Evidence 身份、资源摘要和 Retrieval query digest 不进入聊天线协议。

## 11. Safe Error Mapping

固定 Transport 错误为：

| 错误码 | 触发点 |
|---|---|
| `RUNTIME_CONFIGURATION_ERROR` | Coordinated Factory 缺失 |
| `RUNTIME_SCOPE_CREATION_FAILED` | Factory 创建 Scope 失败且尚未发布 RuntimeEvent |
| `RUNTIME_EXECUTION_FAILED` | Runtime ERROR 或未分类执行失败 |
| `RUNTIME_STREAM_ENCODING_FAILED` | Adapter 编码、重复 Terminal 或 Terminal 后事件 |
| `RUNTIME_TERMINAL_MISSING` | 事件流正常耗尽但没有 RUN_COMPLETED |

客户端安全文本固定为：

```text
[runtime-error] SAFE_ERROR_CODE\n
```

不输出原始异常、traceback、Provider Error 或路径。桌面 `ApiWorker` 的 HTTP 异常提示也改为固定 `API request failed`，不拼接 requests 原始异常。

## 12. Final Output 唯一性

RunCoordinator 仍拥有 `AgentState.final_output` 的状态提交；Transport 不读取该字段补正文。Control 和 SAFE_ERROR Chunk 不计入成功正文。

离线 E2E 验证客户端成功正文与 `OUTPUT_DELTA` 完全一致，Model 只调用一次。

## 13. Terminal 唯一性

`RunCoordinator` 是 `RUN_COMPLETED` 的唯一创建者。HTTP、ChatService 和 Adapter 均不创建 Terminal。

Adapter 在消费第一个 `RUN_COMPLETED` 后关闭业务事件接收：

- 重复 Terminal：`RUNTIME_STREAM_ENCODING_FAILED`；
- Terminal 后业务事件：`RUNTIME_STREAM_ENCODING_FAILED`；
- 缺失 Terminal：`RUNTIME_TERMINAL_MISSING`，不伪造成功或 Terminal。

正常和 Runtime 失败 E2E 均验证 `RUN_COMPLETED count == 1`。

## 14. Request Scope 生命周期

正常路径：

```text
RUN_COMPLETED consumed
-> producer completed
-> channel drained/closed
-> scope.close()
-> RunRegistry empty
```

运行失败由 RunCoordinator 发布安全 ERROR 和唯一 RUN_COMPLETED，随后执行相同清理。

Adapter 编码失败会停止编码、关闭外层事件生成器、向内传播 `aclose()`、abort 当前 Scope、有界等待 producer，并清空 Registry 和 Gauge。

审计额外发现并修复了一个真实问题：外层 `stream_coordinated_agent_events()` 原来在 Consumer `aclose()` 时没有显式关闭 Factory 内层生成器，容量受限 Channel 下可遗留 RunRegistry handle。现在委托边界使用 `finally: await events.aclose()`。

`CoordinatedRunScope.close()`、`abort()` 均幂等。纯数据对象由 Scope 强持有到请求结束；GC 不是 Close Owner。

## 15. Explicit Legacy

显式：

```text
CHAT_RUNTIME_MODE=legacy
```

仍执行：

```text
/api/chat -> ChatService.stream_chat() -> Legacy AgentLoop
```

Legacy 不访问 Coordinated Factory、Snapshot 或 Recovery，不创建 Coordinated Scope。正常文本和既有 `[[ORCH]]` 兼容保持不变；意外异常映射为固定 `RUNTIME_EXECUTION_FAILED`，不返回原始异常。

## 16. No Fallback

不存在 Coordinated 到 Legacy 或 Legacy 到 Coordinated 的动态 fallback。

- Coordinated 失败不会调用 `stream_chat()`；
- Legacy 失败不会调用 `stream_coordinated_agent_text()`；
- Factory 缺失不会手工装配，也不会切到 Legacy；
- 一个请求只产生一套 identity、Channel、Registry 注册和 Event Sequence。

Model Router 自身既有的 Model retry/fallback 不属于跨 Runtime fallback，本轮没有修改其语义。

## 17. Media Type 兼容

响应继续使用：

```text
Content-Type: text/plain
```

wire format 继续是自定义文本分块协议，不是标准 SSE。请求体 Schema、桌面 Parser、HTTP transport 类型均未改变。

HTTP/ASGI 可以在任意字节边界拆分 Chunk；测试把完整 `[[ORCH]]` 控制行在每个字符边界拆开，桌面 Parser 均只产生一个控制事件且不污染正文。

## 18. Security

- Runtime ERROR 只输出固定 Transport 错误；
- 控制 payload 使用事件类型字段 allowlist；
- trace/span/event identity 默认不发到 UI；
- Tool Evidence、Retrieval query digest、Snapshot、Recovery 数据不进入聊天；
- 原始异常和路径不进入 HTTP body；
- `[[ORCH]]` 控制消息不进入 AgentState.final_output；
- RuntimeEvent JSON 不作为正常正文输出。

本轮未新增 Metrics，避免重复定义第 20 天 Descriptor。

## 19. Runtime 真实接入

离线真实链路使用 Fake Model 和本地 InMemory/SQLite 测试资源，不访问网络、在线模型、Chroma、外部 Tool 或真实 UI：

```text
/api/chat
-> Coordinated selector
-> Factory
-> Scope
-> Coordinator
-> Model adapter
-> OUTPUT_DELTA
-> RUN_COMPLETED
-> desktop-compatible chunks
```

Happy Path 验证正文唯一、Terminal 唯一、Model 调用一次、Registry 归零。

Failure Path 验证固定安全错误、Terminal 唯一、Model 调用一次、无 Legacy fallback、资源收口。

## 20. Legacy Boundary

Legacy AgentLoop、Legacy `AgentRouter` 的编排文本生成和正常流协议保持原状。迁移只发生在 `/api/chat` 的模式选择边界和 Coordinated transport 适配边界。

本轮没有修改 Tool retry/幂等/副作用、Model retry/fallback、Retrieval、Checkpoint 或 Recovery 业务合同。

## 21. Bad Case

### Bad Case 1：Coordinated 失败后静默 Legacy 双跑

- 类型：假设构造
- 触发条件：捕获 Coordinated 异常后调用 `stream_chat()` 重跑同一请求
- 故障表现：Model/Tool 副作用可能重复，同一 run_id 出现两套状态
- 根因分析：把跨 Runtime 重跑误当成可用性 fallback
- 修复方案：入口只选择一次；失败在所选 Runtime 内安全收口
- 回归测试：`test_neither_runtime_falls_back_to_the_other_after_failure`、`test_api_coordinated_failure_has_no_legacy_fallback`
- 对应知识点：Exactly-once 边界、失败域、跨 Runtime fallback
- 面试表达：Runtime 选择是请求级不可变决策，不能在发生副作用后换执行器重跑
- 当前状态：已由互斥分支和负向测试防止

### Bad Case 2：默认配置仍走 Legacy

- 类型：真实发现
- 触发条件：未设置 `CHAT_RUNTIME_MODE`
- 故障表现：第一轮 `Settings.load()` 和 Selector 默认返回 LEGACY，API 也固定调用 `stream_chat()`
- 根因分析：第一轮只建立 Mode 合同，尚未完成默认入口迁移
- 修复方案：默认值切换为 COORDINATED，API 按捕获的 Enum 正式分流
- 回归测试：`test_default_settings_select_coordinated_and_snapshot_disabled`、`test_default_chat_endpoint_captures_mode_once_and_routes_coordinated`
- 对应知识点：配置默认值、迁移开关、生产入口
- 面试表达：能力接入完成不等于默认流量已迁移，必须用入口测试证明
- 当前状态：已修复

### Bad Case 3：请求中途重新读取 Mode

- 类型：假设构造
- 触发条件：流式请求执行期间重复读取环境变量或 Settings
- 故障表现：同一请求可能因配置变化进入不一致分支
- 根因分析：缺少请求入口的不可变配置快照
- 修复方案：`chat_endpoint()` 只调用一次 `selected_runtime_mode()`
- 回归测试：`test_coordinated_and_legacy_routes_are_strictly_mutually_exclusive`
- 对应知识点：TOCTOU、Request Snapshot
- 面试表达：长流请求的模式必须在入口冻结，不能边执行边读配置
- 当前状态：已由单次捕获防止

### Bad Case 4：Factory 缺失后手工装配 Runtime

- 类型：真实发现
- 触发条件：`ChatService._coordinated_runtime_factory is None`
- 故障表现：旧实现直接创建 Context、Ledger、State、Channel、Emitter、Scheduler、Executor 和 Coordinator
- 根因分析：为兼容旧测试保留了生产 factory-less 路径
- 修复方案：删除手工分支；缺失 Factory 返回 `RUNTIME_CONFIGURATION_ERROR`
- 回归测试：`test_factory_missing_returns_fixed_configuration_error`、`test_coordinated_entry_contains_no_manual_runtime_assembly_fallback`
- 对应知识点：Composition Root、唯一装配链、依赖注入
- 面试表达：测试便利不能成为第二套生产装配路径
- 当前状态：已修复

### Bad Case 5：OUTPUT_DELTA 与 final_output 重复输出

- 类型：假设构造
- 触发条件：Transport 先输出 Delta，Terminal 时再追加 `final_output`
- 故障表现：客户端看到重复回答
- 根因分析：状态结果与流式事实的正文所有权不清
- 修复方案：只有 OUTPUT_DELTA 映射为正常正文
- 回归测试：`test_output_delta_is_the_only_plain_text_source_and_empty_is_ignored`、`test_run_completed_is_control_only_and_does_not_repeat_final_output`
- 对应知识点：Single Source of Truth、流式聚合
- 面试表达：状态里的 final_output 用于终态，不是 Transport 的第二正文源
- 当前状态：已由 Adapter 不变量防止

### Bad Case 6：`[[ORCH]]` 污染正文

- 类型：假设构造
- 触发条件：把控制 Chunk 拼入 AgentState.final_output 或成功正文聚合
- 故障表现：回答中出现控制 JSON，Memory 和后续 Prompt 被协议内容污染
- 根因分析：混淆内部事件、线协议和业务正文
- 修复方案：CONTROL 使用独立 Chunk Kind，正文只来自 OUTPUT_DELTA
- 回归测试：`test_output_delta_is_the_only_plain_text_source_and_empty_is_ignored`、`test_api_to_factory_to_output_delta_to_terminal_happy_path`
- 对应知识点：Protocol Boundary、数据面与控制面
- 面试表达：控制面可以与文本共用 transport，但不能共用正文所有权
- 当前状态：已由类型和聚合测试防止

### Bad Case 7：HTTP 层创建第二个 RUN_COMPLETED

- 类型：假设构造
- 触发条件：HTTP 层看到流结束后补发一个 Runtime Terminal
- 故障表现：Journal、Metrics、UI 收到两个终态
- 根因分析：把 transport EOF 误当成 Runtime 状态事实
- 修复方案：只有 RunCoordinator 创建 RUN_COMPLETED；HTTP 只转发
- 回归测试：`test_api_to_factory_to_output_delta_to_terminal_happy_path`、`test_runtime_failure_is_safe_has_one_terminal_and_does_not_double_run`
- 对应知识点：Terminal Owner、事件事实与传输状态
- 面试表达：EOF 不是领域终态，HTTP 无权制造 RuntimeEvent
- 当前状态：已由唯一 Owner 测试防止

### Bad Case 8：Terminal 后继续输出文本

- 类型：真实发现
- 触发条件：旧的无状态 `RuntimeEventTextAdapter.encode()` 在 RUN_COMPLETED 后继续接收事件
- 故障表现：终态后仍可能出现正文或控制消息
- 根因分析：Adapter 没有 terminal_seen 状态
- 修复方案：有状态 Adapter 在 Terminal 后拒绝所有事件
- 回归测试：`test_duplicate_terminal_and_business_event_after_terminal_are_rejected`
- 对应知识点：Terminal State、协议状态机
- 面试表达：流式 Adapter 不是纯序列化器，它还要执行终态封口合同
- 当前状态：已修复

### Bad Case 9：缺失 Terminal 时伪造成功

- 类型：真实发现
- 触发条件：旧事件流在没有 RUN_COMPLETED 时正常耗尽
- 故障表现：Transport 静默结束，客户端可能把 EOF 当成功
- 根因分析：旧 Adapter 没有 finish 校验
- 修复方案：返回 `RUNTIME_TERMINAL_MISSING`，但不创建 RUN_COMPLETED
- 回归测试：`test_missing_terminal_returns_transport_error_without_fabricating_terminal`
- 对应知识点：Fail Closed、EOF 与 Terminal 区分
- 面试表达：缺失终态应报协议错误，不能用伪造成功修补
- 当前状态：已修复

### Bad Case 10：Adapter 输出 Tool/RAG 正文

- 类型：假设构造
- 触发条件：把 Tool result、RAG Chunk 或 Memory 内容当聊天正文输出
- 故障表现：敏感数据泄漏并破坏 final output 唯一性
- 根因分析：Transport 为补正文而读取其他子系统对象
- 修复方案：Tool/Retrieval 只允许有限控制字段，正文只认 OUTPUT_DELTA
- 回归测试：`test_control_projection_omits_trace_span_event_identity_and_evidence`
- 对应知识点：安全投影、最小权限、正文 Owner
- 面试表达：可观测状态可以投影，业务结果不能越权进入聊天正文
- 当前状态：当前审计未发现 Tool/RAG 正文事故；已增加字段白名单防护

### Bad Case 11：Legacy 模式仍创建 Coordinated Scope

- 类型：假设构造
- 触发条件：入口先创建 Coordinated Scope，再根据 Mode 选择 Legacy
- 故障表现：无用注册、Channel 和取消源泄漏，甚至产生双跑
- 根因分析：模式选择晚于资源创建
- 修复方案：先捕获 Mode，再只创建所选 stream
- 回归测试：`test_explicit_legacy_api_does_not_create_coordinated_scope`
- 对应知识点：Lazy Construction、互斥分支
- 面试表达：互斥不仅是调用互斥，也必须是资源创建互斥
- 当前状态：已由显式 Legacy E2E 防止

### Bad Case 12：原始异常通过 `[server-error]` 返回客户端

- 类型：假设构造
- 触发条件：捕获异常后拼接 `[server-error] {raw_exception}`
- 故障表现：Provider 信息、路径、Token 或内部实现泄漏
- 根因分析：把日志异常直接复用为用户文本
- 修复方案：服务端只发送固定安全错误；桌面 HTTP 异常提示也不拼原始异常
- 回归测试：`test_neither_runtime_falls_back_to_the_other_after_failure`、`test_runtime_error_maps_to_one_fixed_safe_error_without_raw_message`
- 对应知识点：Error Projection、信息泄漏
- 面试表达：用户错误、结构化日志和原始异常必须是三个不同的数据面
- 当前状态：当前生产搜索未发现 `[server-error]` 拼接；相邻的桌面原始 requests 异常展示已修复

### Bad Case 13：Snapshot 默认启用导致未使用能力阻塞启动

- 类型：真实发现
- 触发条件：未设置 `LOCAL_AGENT_SNAPSHOT_ENABLED`
- 故障表现：第一轮默认创建 SQLiteSnapshotStore，未使用 Snapshot 的部署也依赖其初始化
- 根因分析：基础设施能力默认值设为 true
- 修复方案：默认 false；显式 true 仍 fail fast
- 回归测试：`test_default_settings_select_coordinated_and_snapshot_disabled`、`test_snapshot_enabled_constructs_matching_recovery_capability`
- 对应知识点：Opt-in Capability、启动依赖
- 面试表达：尚未进入热路径的持久化能力应显式启用，而不是默认扩大启动失败面
- 当前状态：已修复

### Bad Case 14：Recovery disabled 但 Validator 仍看似可用

- 类型：真实发现
- 触发条件：Snapshot disabled 时仍构造 `RecoveryValidator(snapshot_store=None, ...)`
- 故障表现：调用者通过非空接口误判 Recovery 可用
- 根因分析：只关闭底层 Store，没有关闭上层 Capability
- 修复方案：Store、Validator 和两个 enabled flag 强制自洽
- 回归测试：`test_snapshot_and_recovery_disabled_capabilities_are_truly_unavailable`、`test_recovery_cannot_appear_enabled_without_snapshot`
- 对应知识点：Capability Modeling、非法状态不可表示
- 面试表达：disabled 不应是内部调用后才报错，而应在类型和装配状态上不可用
- 当前状态：已修复

### Bad Case 15：请求级对象依赖 GC 清理

- 类型：真实发现
- 触发条件：第一轮结果文档把请求级资源 Close Owner 写为 `CoordinatedRunScope/GC`
- 故障表现：生命周期责任不清，容易容忍 Registry、Channel 或 Task 延迟清理
- 根因分析：把内存回收和外部资源关闭混为一谈
- 修复方案：文档改为 Scope 唯一 Owner；close/abort 显式且幂等
- 回归测试：`test_scope_close_and_abort_are_bounded_idempotent_owners`
- 对应知识点：RAII、Ownership、GC 与 Close 区分
- 面试表达：GC 只能回收对象内存，不能承担确定性的协议和注册表收口
- 当前状态：文档和实现合同已修正

### Bad Case 16：外层异步生成器关闭未传播到 Factory 内层

- 类型：真实发现
- 触发条件：Consumer 在容量受限 Channel 收到首事件后 `aclose()` 外层事件流
- 故障表现：内层 producer 和 RunRegistry handle 可能仍存活
- 根因分析：`async for ... yield` 委托边界没有在 finally 显式关闭内层 async generator
- 修复方案：外层使用 `finally: await events.aclose()`；内层 abort Scope 并等待 producer
- 回归测试：`test_consumer_close_aborts_bounded_channel_and_cleans_registry`、`test_adapter_encoding_failure_aborts_current_scope_and_cleans_registry`
- 对应知识点：Async Generator Delegation、结构化并发、资源传播
- 面试表达：Python async generator 没有 `yield from` 的自动关闭所有权，委托边界必须显式传播 aclose
- 当前状态：已修复

## 22. 测试结果

新增测试：

```text
tests/test_chat_stream_compatibility.py
tests/test_default_runtime_entry.py
tests/test_runtime_mode_e2e.py
tests/test_coordinated_stream_lifecycle.py
```

结果：

```text
目标 pytest：151 passed, 4 subtests passed
全仓 pytest：664 passed, 42 subtests passed
compileall：通过
uv lock --check：通过
git diff --check：通过（仅 Git 的 LF/CRLF 工作区提示）
```

测试均为离线测试。

## 23. 未完成事项

- 新的 Client Disconnect、Generator close、Socket close 专用取消语义；
- 完整 Graceful Shutdown；
- 自动 Checkpoint；
- 自动 Recovery / Replay；
- Fault Injection；
- 第 24 天内容；
- 标准 SSE 或 WebSocket 迁移。

这些项目均未在本轮实现。

## 24. 第三轮接入点

第三轮可以从 `server.py` 的 disconnect 检测、`ChatService` 的 async generator 委托边界、`CoordinatedRunScope.abort()` 和 producer 有界等待继续接入专用取消传播。

需要保持的既有不变量：

- Mode 只捕获一次；
- 不跨 Runtime fallback；
- Factory 唯一装配；
- OUTPUT_DELTA 唯一正文；
- RunCoordinator 唯一 Terminal；
- Scope 唯一请求生命周期 Owner。

## 25. 需要带回 ChatGPT 审查的信息

```text
Snapshot default：false
Snapshot enabled failure：fail fast，不回退 InMemory
Recovery disabled：store=None, validator=None, flags=false
Default runtime mode：COORDINATED
Explicit legacy：保留，仅进入 Legacy
Mode capture count：每请求 1
API coordinated path：stream_coordinated_agent_text
API legacy path：stream_chat
Factory required：是
Manual coordinated fallback：已删除
RunContext count：每个 Coordinated 请求 1
CancellationSource count：每个 Coordinated 请求 1
EventChannel count：每个 Coordinated 请求 1
RunRegistry registration：注册/注销各 1
Compatibility adapter：ChatStreamCompatibilityAdapter
Wire protocol：普通文本 + [[ORCH]] JSON 控制行
Media type：text/plain
Text event：仅 OUTPUT_DELTA
Control event：固定 allowlist 和字段投影
Safe error：固定 [runtime-error] SAFE_ERROR_CODE
Output source：OUTPUT_DELTA.text
Final output duplication：无
Terminal owner：RunCoordinator
Terminal count：1
Fallback policy：无跨 Runtime fallback
Normal scope cleanup：已验证
Failure scope cleanup：已验证
Legacy compatibility：已验证
新增测试：4 个测试文件
目标 pytest：151 passed, 4 subtests passed
全仓 pytest：664 passed, 42 subtests passed
compileall：通过
lock check：通过
diff check：通过（仅 Git 的 LF/CRLF 工作区提示）
需要人工确认的问题：无阻塞项；第三轮取消传播范围仍按计划保留
```
