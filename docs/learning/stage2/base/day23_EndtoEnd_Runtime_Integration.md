已纳入新规则：

> 当总体目标涉及大量跨模块修改时，主动拆成多轮 Codex 任务。每轮独立限定范围、测试和结果文档，经过审查后再继续，但最终不能遗漏原总体目标。

# 阶段二第 23 天：End-to-End Runtime Integration 与默认入口迁移

**当前进度：第 23/25 天。**

本日不修改 Tool Call（工具调用）的执行、重试、幂等或副作用语义，因此不触发复杂模拟 Tool 的暂停规则。

## 本日计划调整

第 23 天是阶段二代码改动量最大的一天，拆为四轮：

```text
第一轮：Runtime 生产装配与模式选择
第二轮：默认 /api/chat 迁移与流式协议适配
第三轮：Client Disconnect、Cancellation 与 Shutdown
第四轮：完整 E2E 矩阵、回归和最终文档
```

每轮审查通过后再进入下一轮。

------

# 一、本日最终目标

当前系统已经具备：

```text
RunContext
→ State Machine
→ Planner
→ Scheduler / ParallelExecutor
→ Model / Tool / RAG
→ RuntimeEvent
→ Event Journal
→ Observability
→ Trace
→ Snapshot / Recovery Assessment
```

但默认入口仍然是：

```text
/api/chat
→ Legacy Text Stream
```

第 23 天最终需要变成：

```text
/api/chat
→ Runtime Mode Selection
→ Coordinated Runtime
→ RuntimeEvent Stream
→ Compatibility Adapter
→ 现有客户端文本协议
```

最终真实链路：

```text
HTTP Request
→ ChatService
→ RunContext
→ RunCoordinator
→ Planner
→ PlanGraph
→ Scheduler
→ ParallelExecutor
→ Model / Tool / Retrieval
→ RuntimeEvent
→ Event Journal
→ Observability / Trace
→ Stream Compatibility Adapter
→ HTTP Client
```

------

# 二、本日必须掌握

## 1. End-to-End Integration

End-to-End Integration（端到端集成）不是把已经完成的模块全部 import 进来，而是确认：

- 每个对象由谁创建；
  -每个对象由谁关闭；
  -一次请求只产生一个 Run；
  -错误如何穿过全部层级；
  -取消如何从 HTTP 传播到 Worker；
  -最终输出如何从 Runtime Event 变成客户端文本；
  -任何失败是否会导致重复执行。

## 2. Runtime Mode Selection

使用显式模式：

```text
CHAT_RUNTIME_MODE=coordinated|legacy
```

基本规则：

```text
一个请求
→ 请求开始前选择一次 Runtime
→ 整个请求期间不改变
```

禁止：

```text
Coordinated 执行到一半失败
→ 静默重新走 Legacy
```

否则可能：

-模型调用两次；
-Tool 副作用执行两次；
-RAG 查询两次；
-Budget 统计错误；
-返回两套流式内容；
-出现两个 `run_id`。

## 3. Compatibility Adapter

客户端目前可能只认识：

-普通文本 Chunk；
-`[[ORCH]]` 协议；
-最终文本；
-现有错误格式。

Coordinated Runtime 输出的是强类型 `RuntimeEvent`，因此需要：

```text
RuntimeEvent
→ ChatStreamCompatibilityAdapter
→ 现有客户端可理解的输出
```

Adapter 只负责传输协议兼容，不负责：

-决定 RunStatus；
-触发 Retry；
-调用 Tool；
-修改 AgentState；
-补发丢失的业务事件；
-创建第二个最终输出。

## 4. Graceful Shutdown

Graceful Shutdown（优雅关闭）不能只执行：

```python
await model.close()
```

需要先阻止新请求，再处理活跃 Run 和后台 Worker。

------

# 三、核心不变量

## 1. 一次请求只走一个 Runtime

必须满足：

```text
legacy_selected XOR coordinated_selected
```

不能两者都执行，也不能中途切换。

## 2. 一次请求只创建一个 RunContext

以下身份必须只有一个 Owner：

```text
run_id
session_id
trace_id
CancellationSource
Deadline
```

API、ChatService 和 RunCoordinator 不能分别创建一套。

## 3. Coordinated 失败不静默回退

失败处理：

```text
Coordinated failure
→ Coordinated typed error / terminal event
→ 结束请求
```

不能：

```text
Coordinated failure
→ Legacy stream_chat()
```

Legacy 只能由请求开始前的显式模式选择进入。

## 4. Final Output 唯一

最终正文只能来自：

```text
允许面向用户的 OutputDelta / Final Output
```

不得包括：

- `[[ORCH]]`；
  -Runtime Event JSON；
  -Trace ID；
  -Journal Metadata；
  -Tool Evidence；
  -RAG 内部 Context；
  -Observability 字段；
  -Snapshot 信息。

## 5. Terminal Event 唯一

一次 Run 最多有一个：

```text
RUN_COMPLETED
```

HTTP 层不能再创建第二个伪 Terminal。

## 6. Client Disconnect 是取消，不是普通异常

客户端断开时：

```text
CLIENT_DISCONNECTED
→ CancellationSource.cancel()
→ Runtime 停止启动新工作
→ 已运行工作按既有边界收口
```

不能仅关闭 HTTP Generator，而让 Runtime 在后台继续完整执行。

------

# 四、Runtime 生产装配

## 1. 应用级对象

以下对象一般由 Server Lifespan（服务生命周期）持有：

```text
EventJournal
ObservabilityDispatcher
StructuredRuntimeLogger
RuntimeMetricsRecorder
SpanRecorder
SnapshotStore
RecoveryValidator
ModelInvocationRouter
ToolExecutionService
RetrievalExecutionService
BlockingExecutors
WorkerTrackers
RunRegistry
CoordinatedRuntimeFactory
```

## 2. 请求级对象

每次请求创建：

```text
RunContext
CancellationSource
AgentState
Plan
Scheduler
ParallelExecutor Run Scope
RuntimeEventChannel
RuntimeEventEmitter
RunCoordinator
```

不得把请求级可变状态放进全局单例。

## 3. 工厂而非全局变量

建议建立：

```text
ApplicationRuntimeServices
CoordinatedRuntimeFactory
```

职责：

```text
ApplicationRuntimeServices
→ 持有应用级依赖

CoordinatedRuntimeFactory
→ 为每个请求创建独立 Run Scope
```

不能使用模块级：

```python
CURRENT_RUN_CONTEXT = ...
CURRENT_AGENT_STATE = ...
```

------

# 五、Snapshot Store 的生产装配决策

第 22 天遗留了 Snapshot Store 装配位置。

第 23 天建议固定：

```text
Server Lifespan
→ 创建 Application-scoped SQLiteSnapshotStore
→ 注入 Coordinated Runtime
→ Shutdown 时关闭
```

但仍然：

-不自动创建 Checkpoint；
-不自动 Resume；
-不暴露恢复 API；
-不将 Snapshot Store 失败转换成聊天失败；
-只为显式 `create_checkpoint()` 和未来恢复能力提供基础设施。

测试中继续使用：

```text
InMemorySnapshotStore
```

------

# 六、默认入口迁移策略

## 最终配置

建议：

```text
CHAT_RUNTIME_MODE=coordinated
```

作为最终默认值。

显式回滚：

```text
CHAT_RUNTIME_MODE=legacy
```

## 迁移过程

第一轮不修改 `/api/chat`，只准备生产装配。

第二轮才执行：

```text
/api/chat
→ ChatRuntimeSelector
→ Coordinated 或 Legacy
```

在第二轮测试完整通过前，不提前切换默认值。

------

# 七、流式输出适配

## 1. 允许面向客户端的事件

至少包括：

```text
OUTPUT_DELTA
RUN_COMPLETED
安全用户错误
可选进度事件
```

## 2. 内部事件

以下默认不进入用户文本：

```text
MODEL_STARTED
MODEL_COMPLETED
TOOL_STARTED
TOOL_COMPLETED
RETRIEVAL_STARTED
RETRIEVAL_STAGE_COMPLETED
BUDGET_EXHAUSTED
TIMEOUT
CANCELLATION
Trace / Journal / Snapshot Metadata
```

这些可以用于 UI 状态面板，但不能混入回答正文。

## 3. `[[ORCH]]` 边界

如果现有前端仍依赖 `[[ORCH]]`，应由 Compatibility Adapter 根据强类型事件生成有限协议消息。

禁止：

```text
RuntimeEvent
→ 先序列化成 [[ORCH]]
→ Driver 又把 [[ORCH]] 聚合进 final_output
```

最终正文聚合必须只接受业务文本。

------

# 八、Client Disconnect 与 Generator 生命周期

HTTP Streaming Generator（流式生成器）必须在：

-正常完成；
-客户端断开；
-请求取消；
-服务器 Shutdown；
-序列化错误；
-流式发送错误；

所有路径执行：

```text
cancel if required
→ close runtime event consumer
→ close generator
→ wait bounded runtime cleanup
→ unregister run
```

不能只依赖 Python Garbage Collection（垃圾回收）。

------

# 九、Shutdown 顺序

最终建议顺序：

```text
1. 标记服务 SHUTTING_DOWN
2. 停止接受新的 Coordinated Run
3. 请求取消活跃 Run
4. 有界等待 RunRegistry 清空
5. 等待/标记 Detached Tool Workers
6. 等待/关闭 Retrieval Blocking Executor
7. 关闭剩余 per-run EventChannel
8. Flush / Close Observability Dispatcher
9. Flush / Close Span Recorder
10. Close Snapshot Store
11. Close Event Journal
12. Close Model / HTTP Client
13. Close 其他 Store
```

## 为什么 Journal 较晚关闭

活跃 Run 在取消收口时仍可能需要 Journal：

```text
CANCELLATION
STEP_COMPLETED
RUN_COMPLETED
```

如果先关闭 Journal，终态事件无法持久化。

## 为什么 Model 较晚关闭

活跃 Model Attempt 可能正在响应取消或结束清理。过早关闭共享 Client 会制造额外错误。

------

# 十、Runtime Mode 失败策略

## 配置非法

```text
CHAT_RUNTIME_MODE=unknown
```

服务启动应 Fail Fast（快速失败），不能静默回退到 Legacy。

## Coordinated 初始化失败

服务启动失败或明确进入不可用状态，不能请求时偷偷使用 Legacy。

## 请求执行失败

返回 Coordinated Runtime 的安全失败结果，不重新调用 Legacy。

## 显式 Legacy

只有配置明确为：

```text
legacy
```

才进入旧路径。

------

# 十一、本日重点 Bad Case

## Bad Case 1：Coordinated 失败后双跑 Legacy

- **类型：严重副作用风险**
- 修复：请求开始前一次性 Runtime Mode 决策。

## Bad Case 2：API 与 ChatService 分别创建 RunContext

- **类型：身份一致性风险**
- 修复：单一 Run Scope Factory。

## Bad Case 3：Runtime Mode 在流式执行中改变

- **类型：配置竞态**
- 修复：请求开始时捕获不可变 Mode。

## Bad Case 4：默认 API 实际仍走 Legacy

- **类型：真实性错误**
- 修复：E2E 测试断言真实 Coordinated 组件被调用。

## Bad Case 5：`[[ORCH]]` 污染 Final Output

- **类型：真实历史 Bad Case**
- 修复：传输协议与正文聚合分离。

## Bad Case 6：Client Disconnect 只关闭 Socket

- **类型：资源泄漏**
- 修复：传播 `CLIENT_DISCONNECTED` Cancellation。

## Bad Case 7：Disconnect 后 Tool 重新执行

- **类型：副作用风险**
- 修复：取消后不得启动新 Attempt。

## Bad Case 8：Shutdown 先关闭 Journal

- **类型：终态丢失**
- 修复：Journal 在活跃 Run 收口后关闭。

## Bad Case 9：Shutdown 无限等待同步 Worker

- **类型：服务无法退出**
- 修复：有界 Grace Period 和 Detached 状态。

## Bad Case 10：EventChannel 关闭后仍发布 Event

- **类型：生命周期竞态**
- 修复：唯一 Channel Owner 和终态顺序。

## Bad Case 11：HTTP 层伪造第二个 RUN_COMPLETED

- **类型：事实源冲突**
- 修复：Terminal 只由 RunCoordinator 发布。

## Bad Case 12：Legacy 与 Coordinated 共用 CancellationSource

- **类型：跨请求污染**
- 修复：每个请求独立 Run Scope。

## Bad Case 13：Snapshot Store 失败导致聊天失败

- **类型：非关键基础设施耦合**
- 修复：无自动 Checkpoint 时不参与请求热路径。

## Bad Case 14：配置错误静默进入 Legacy

- **类型：可运维性错误**
- 修复：启动时严格校验。

------

# 十二、四轮任务划分

## 第一轮：Runtime Assembly 与 Mode Contract

完成：

-审计当前默认入口和 Coordinated 入口；
-`ChatRuntimeMode`；
-严格 Settings；
-`ApplicationRuntimeServices`；
-`CoordinatedRuntimeFactory`；
-Snapshot Store 生产装配；
-应用级生命周期；
-依赖所有权文档；
-不迁移 `/api/chat`。

## 第二轮：默认入口与 Streaming Adapter

完成：

- `/api/chat` 模式选择；
  -默认切到 Coordinated；
  -Legacy 显式回滚；
  -Event → 文本协议；
  -Final Output；
  -无静默 Fallback；
  -无双 Run。

## 第三轮：Cancellation、Disconnect、Shutdown

完成：

- Client Disconnect；
  -Generator close；
  -RunRegistry 收口；
  -Shutdown Gate；
  -有界 Grace Period；
  -Worker/Journal/Observability/Trace/Store 关闭顺序。

## 第四轮：E2E 与最终验收

完成：

- Model；
  -RAG；
  -Tool；
  -并行 Step；
  -Retry/Fallback；
  -Budget；
  -Timeout；
  -Cancellation；
  -Journal Failure；
  -Observability/Trace；
  -Legacy Mode；
  -Shutdown；
  -最终结果文档。

------

# 十三、第一轮 Codex 任务

你正在继续改造 LocalAgent 项目的阶段二 Runtime。

这是第 23 天第一轮任务。

本轮只实现：

- 默认入口与 Coordinated Runtime 现状审计
- ChatRuntimeMode Contract
- Runtime Mode Settings 严格校验
- ApplicationRuntimeServices
- CoordinatedRuntimeFactory / Request Run Scope Factory
- 应用级 Runtime 依赖装配
- Snapshot Store 生产生命周期装配
- Runtime Lifecycle Ownership
- Close / Flush 基础合同
- 第一轮离线测试

本轮不得：

- 修改默认 `/api/chat` 的真实执行路径
- 将默认模式切换为 Coordinated
- 实现 Client Disconnect 传播
- 修改 Streaming Protocol
- 实现完整 Shutdown 顺序
- 修改 Tool Call 业务逻辑
- 修改 Model Retry/Fallback 语义
- 修改 RAG 执行语义
- 实现自动 Checkpoint
- 实现自动 Recovery/Replay
- 实施第 24 天 Fault Injection

结果文档：

```text
docs/learning/stage2/result/day23_runtime_assembly_result.md
```

最终第 23 天文档留到第四轮：

```text
docs/learning/stage2/result/day23_e2e_runtime_result.md
```

## 一、审计当前入口

至少检查：

- `server.py`
- `/api/chat` 路由
- `core/chat_service.py`
- `stream_chat()`
- `stream_coordinated_agent_events()` 或真实等价入口
- RunContext 创建位置
- CancellationSource 创建位置
- RunCoordinator 创建位置
- RuntimeEventChannel 创建位置
- EventJournal / Observability / Trace 创建与关闭位置
- Model / Tool / Retrieval 应用级实例
- RunRegistry
- SnapshotStore / RecoveryValidator
- Settings
- Server lifespan
- 第 13～22 天结果文档及相关测试

结果文档必须画出现有两条真实调用链：

```text
/api/chat → Legacy
```

以及：

```text
测试或旁路入口 → Coordinated Runtime
```

明确：

- 当前默认 `/api/chat` 调用了哪个方法；
- 当前 RunContext 在哪里创建；
- 是否存在重复身份创建风险；
- Coordinated Runtime 的应用级依赖目前由谁持有；
  -哪些对象每请求创建；
  -哪些对象应用级复用；
  -当前 Shutdown 如何关闭；
  -Snapshot Store 是否已经装配；
  -默认路径是否仍是 Legacy。

不得因为目标是迁移而把当前状态描述成已经完成。

## 二、ChatRuntimeMode

建立固定 Enum：

```text
LEGACY
COORDINATED
```

配置键：

```text
CHAT_RUNTIME_MODE
```

要求：

- 严格大小写规范化；
- 空值按明确默认处理；
- 未知值启动失败；
- 不静默回退；
- 不接受自由字符串在业务代码中到处比较；
- 请求开始时捕获不可变 Mode；
- Mode 不作为高基数 Metrics Label；
  -可以作为有限 `runtime_mode` Label。

本轮默认值可以暂时保留当前 Legacy，第二轮完成迁移后再改为 Coordinated。结果文档必须写明临时默认值。

## 三、ApplicationRuntimeServices

建立应用级容器，名称可根据项目调整，例如：

```text
ApplicationRuntimeServices
```

至少持有：

- EventJournal
- ObservabilityDispatcher
- StructuredRuntimeLogger
- RuntimeMetricsRecorder
- SpanRecorder
- SnapshotStore
- RecoveryValidator
- ModelInvocationRouter 或其应用级依赖
- ToolExecutionService
- RetrievalExecutionService
- BlockingExecutors
- WorkerTrackers
- RunRegistry
- RuntimeActivityTracker
- 其他真实应用级 Store

要求：

- 明确 immutable reference / controlled lifecycle；
- 不保存当前 AgentState；
- 不保存当前 RunContext；
- 不保存当前 EventChannel；
- 不保存请求正文；
  -不使用模块级可变全局变量；
  -生产创建一次；
  -测试可替换为 InMemory/Fake；
  -close 幂等。

如果某个对象实际应该请求级创建，不得为了容器方便强行提升为应用级。

## 四、Request Run Scope

建立等价结构：

```text
CoordinatedRunScope
CoordinatedRuntimeFactory
```

每请求创建：

- RunContext
- CancellationSource
- AgentState
- Scheduler / Parallel Run Scope
- RuntimeEventChannel
- RuntimeEventEmitter
- RunCoordinator
- Request Trace Root 入口
- per-run Checkpoint Coordinator 依赖绑定

要求：

- 同一请求只创建一次 run_id/session_id/trace_id；
- CancellationSource 的强引用由 Run Scope 或 ChatService Owner 持有；
- RunContext 只持 CancellationToken；
  -不将 per-run 对象缓存到应用级容器；
  -Run Scope close 幂等；
  -Run Scope 未执行也可安全关闭；
  -失败构造时清理已创建资源；
  -不得创建第二个 Event Sequence Owner。

## 五、依赖所有权表

结果文档必须列出：

| 对象 | Scope | Create Owner | Close Owner | 是否可复用 |
| ---- | ----- | ------------ | ----------- | ---------- |
|      |       |              |             |            |

至少包括：

- Model Client
- ModelInvocationRouter
- ToolExecutionService
- RetrievalExecutionService
- EventJournal
- ObservabilityDispatcher
- SpanRecorder
- SnapshotStore
- RunRegistry
- RuntimeEventChannel
- RunContext
- CancellationSource
- RunCoordinator
- AgentState
- Scheduler
- BlockingExecutor
- WorkerTracker

## 六、Snapshot Store 生产装配

第 22 天遗留的生产装配位置在本轮解决。

建议：

```text
Server lifespan
→ SQLiteSnapshotStore
→ ApplicationRuntimeServices
```

要求：

- 使用独立配置路径或复用安全数据目录；
- 日志不输出完整路径；
  -应用关闭时 close；
  -测试使用 InMemorySnapshotStore；
  -无自动 Checkpoint；
  -不进入普通聊天请求热路径；
  -Snapshot Store 初始化失败的启动策略必须固定。

策略可选择：

### Fail Fast

如果已配置启用 Snapshot 能力但 Store 无法创建，服务启动失败。

### Explicitly Disabled

增加明确配置关闭 Snapshot，使用受控 disabled capability。

不得：

- 初始化失败后静默使用内存 Store；
  -声称快照已持久化但实际只在内存；
  -每个请求创建一个 SQLite Store。

## 七、RecoveryValidator 装配

RecoveryValidator 可以作为应用级无状态服务复用，但：

- 不自动运行；
  -不在聊天请求完成时自动评估；
  -不读取当前活跃 Run；
  -不自动 Resume；
  -不持有 Model/Tool/RAG Adapter；
  -不进入默认请求热路径。

## 八、Server Lifespan 装配

建立明确阶段：

```text
STARTING
READY
SHUTTING_DOWN
CLOSED
```

本轮只建立基础状态和对象装配，不实现第三轮完整 Shutdown。

启动顺序必须符合依赖关系，例如：

```text
Settings
→ Storage
→ Journal / Snapshot Store
→ Metrics / Logger / Trace
→ Model / Tool / Retrieval
→ Observability Dispatcher
→ RunRegistry
→ Runtime Services
→ ChatService
→ READY
```

如果真实依赖关系不同，按实际调整并记录。

构造中途失败时：

-逆序关闭已成功创建的对象；
-不留下半初始化全局状态；
-不泄漏路径、Secret 或原始异常；
-服务不得进入 READY。

## 九、基础 Close Contract

为应用级容器提供幂等：

```text
flush(timeout)
close(timeout)
```

本轮只要求：

- 每个对象最多关闭一次；
  -部分关闭失败不跳过其他对象；
  -使用固定安全错误码；
  -不无限等待；
  -记录尚未完成的完整 Shutdown 顺序为第三轮事项。

不得在本轮主动取消活跃 Run。

## 十、模式选择接口

可以建立：

```text
ChatRuntimeSelector
```

或在 ChatService 中使用固定方法：

```text
selected_runtime_mode()
```

本轮只返回模式，不迁移路由。

要求：

- Mode 在请求开始时读取一次；
  -不在 Generator 每次迭代中重新读取 Settings；
  -不根据异常动态切换；
  -不把 Coordinated 不可用自动映射为 Legacy；
  -未知模式不能到达请求层。

## 十一、Legacy 边界

本轮必须保持：

```text
/api/chat → Legacy
```

并增加回归测试证明没有提前迁移。

Coordinated Factory 可以被测试或现有旁路调用，但不能替换默认路由。

结果文档必须明确：

- 默认入口仍 Legacy；
  -第二轮才迁移；
  -不存在自动 Fallback；
  -本轮只完成生产装配基础。

## 十二、安全要求

Application Runtime Container 的 repr/log 不得包含：

- API Key
- Provider URL
- DB 路径
- Model 路径
- KB 路径
- Prompt
- Tool/RAG/Memory 正文
- Runtime 对象完整 repr
- 原始异常或 traceback

只允许：

- component
- lifecycle state
- enabled/disabled
  -安全版本
  -对象计数
  -固定 error code

## 十三、重点 Bad Case

结果文档至少包含：

1. API、ChatService 各自创建 RunContext；
2. 应用容器保存当前 AgentState；
3. 每请求创建 SQLite Journal；
4. Snapshot Store 初始化失败后静默使用内存；
5. 未知 Runtime Mode 静默回退 Legacy；
6. Generator 执行中重新读取 Mode；
7. Coordinated 初始化失败后请求双跑 Legacy；
8. 构造中途失败未逆序关闭；
9. Close 一个组件失败后跳过其余组件；
10. 应用级容器日志输出数据库路径；
11. Request Scope 被缓存到全局；
12. Snapshot Store 自动进入请求热路径。

使用固定 Bad Case 模板，区分真实审计与假设构造。

## 十四、测试

建议新增：

```text
tests/test_runtime_mode.py
tests/test_application_runtime_services.py
tests/test_coordinated_runtime_factory.py
tests/test_runtime_lifespan.py
```

至少覆盖：

### Runtime Mode

- legacy；
  -coordinated；
  -大小写；
  -空值默认；
  -未知值失败；
  -请求内不可变；
  -不动态 fallback。

### Application Services

-应用级创建一次；
-请求级对象不进入容器；
-close 幂等；
-部分 close 失败继续；
-安全 repr；
-初始化失败逆序清理。

### Run Scope

-单一 RunContext；
-单一 CancellationSource；
-独立请求身份；
-失败构造清理；
-close 幂等；
-不共享 AgentState；
-不共享 EventChannel；
-不创建第二个 Sequence Owner。

### Snapshot Store

-生产 SQLite 装配；
-测试 InMemory；
-禁用策略；
-失败策略；
-无自动 checkpoint；
-close。

### Legacy Boundary

-默认 `/api/chat` 仍调用 Legacy；
-Coordinated Factory 可独立调用；
-不双跑；
-不自动 fallback。

测试不得调用真实网络、在线模型、Chroma、外部 Tool 或 UI。

## 十五、测试命令

执行：

```text
uv run python -m pytest \
  tests/test_runtime_mode.py \
  tests/test_application_runtime_services.py \
  tests/test_coordinated_runtime_factory.py \
  tests/test_runtime_lifespan.py \
  tests/test_run_context.py \
  tests/test_run_coordinator.py \
  tests/test_event_channel.py \
  tests/test_event_journal.py \
  tests/test_observability_integration.py \
  tests/test_trace_integration.py \
  tests/test_snapshot_store.py \
  tests/test_checkpoint_integration.py \
  tests/test_recovery_integration.py -q
```

执行全仓：

```text
uv run python -m pytest -q
uv run python -m compileall -q core tools tests
uv lock --check
git diff --check
```

## 十六、禁止事项

不得：

-迁移默认 `/api/chat`；
-修改客户端流式协议；
-实现 Client Disconnect；
-实现完整 Shutdown；
-修改 Model/Tool/RAG 业务逻辑；
-创建第二套 Runtime；
-自动 fallback；
-自动 checkpoint；
-自动 recovery；
-自动 replay；
-实现第 24 天 Fault Injection；
-保存敏感正文。

## 十七、结果文档

创建：

```text
docs/learning/stage2/result/day23_runtime_assembly_result.md
```

必须包含：

# 第 23 天第一轮：Runtime Assembly

## 1. 本轮目标

## 2. 修改前默认入口

## 3. 修改前 Coordinated 入口

## 4. Runtime Mode Contract

## 5. ApplicationRuntimeServices

## 6. Request Run Scope

## 7. CoordinatedRuntimeFactory

## 8. Dependency Ownership

## 9. Snapshot Store 生产装配

## 10. RecoveryValidator 装配

## 11. Server Lifespan

## 12. Initialization Failure Cleanup

## 13. Close Contract

## 14. Security

## 15. Legacy Boundary

## 16. Bad Case

## 17. 测试结果

## 18. 未完成事项

## 19. 第二轮接入点

## 20. 需要带回 ChatGPT 审查的信息

## 十八、完成后输出

Default API current path：

Coordinated current path：

Runtime mode enum：

Temporary default mode：

Invalid mode：

Mode capture：

Fallback policy：

Application services：

Application-scoped objects：

Request-scoped objects：

RunContext owner：

CancellationSource owner：

EventChannel owner：

Sequence owner：

Runtime factory：

Snapshot store production owner：

Snapshot store failure policy：

Recovery validator owner：

Lifespan states：

Initialization order：

Initialization failure cleanup：

Close order foundation：

Legacy boundary：

新增测试：

目标 pytest：

全仓 pytest：

compileall：

lock check：

diff check：

需要人工确认的问题：

# 十四、第 23 天当前进度

## 第一轮待完成

-  Runtime Mode Contract
-  ApplicationRuntimeServices
-  Request Run Scope
-  CoordinatedRuntimeFactory
-  Snapshot Store 生产装配
-  Lifespan 基础
-  第一轮审查

## 第二轮待完成

-  默认 `/api/chat` 迁移
-  Streaming Compatibility Adapter
-  Final Output 唯一
-  Legacy 显式回滚
-  无自动 Fallback

## 第三轮待完成

-  Client Disconnect
-  Cancellation Propagation
-  Generator Cleanup
-  Graceful Shutdown
-  Shutdown Order

## 第四轮待完成

-  E2E Happy Path
-  E2E Failure Path
-  Runtime 不变量
-  全仓回归
-  第 23 天最终文档

**阶段二第 23/25 天：完成理论和总体拆分，进入第一轮 Runtime Assembly。**