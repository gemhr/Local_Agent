# 阶段二第 16 天改造结果

## 1. 本次目标与调整

本次在既有 `RunCoordinator / ParallelExecutor / ModelInvocationRouter` 真实调用链上加入强类型 Runtime Event、per-run 有界内存 Channel、Run/Step Emitter、安全序列化和文本 Transport Adapter。默认 Legacy `stream_chat`、UI 和 HTTP 入口不切换；新增 Coordinated 事件流与文本适配入口。

没有实现 Event Journal、持久化、Replay、Snapshot、OpenTelemetry、WebSocket、多 Subscriber 或事件丢弃策略。

## 2. 修改前流与 ORCH 现状

修改前生产与消费位置如下：

| 类型 | 位置 | 状态 |
| --- | --- | --- |
| Legacy 生成 | `core/agent_router.py::AgentRouter._build_orchestration_event()` | 保留，默认 Legacy 编排流仍使用 |
| Legacy 过滤 | `core/runtime/agent_loop.py::LegacyAgentRouterDriver.execute()` | 保留，只防止控制文本污染 `final_output` |
| Context 安全过滤 | `core/runtime/model_context.py::ContextBuilder._normalize()` | 保留，拒绝控制标记进入模型上下文 |
| UI 解析 | `main.py::ApiWorker._emit_stream_payload()` | 保留，继续兼容当前桌面 UI |
| 新 Runtime 适配生成 | `core/runtime/stream_adapter.py::RuntimeEventTextAdapter.encode()` | 新增，Coordinated 路径唯一新生成位置 |

生产代码中没有其他 `[[ORCH]]` 生成点。新事件核心、Channel、Emitter、Coordinator、Executor 和 Model Router 均不拼接或解析该文本。

## 3. Runtime Event 与 Transport Text

Runtime 内部只传递不可变 `RuntimeEvent`。`RuntimeEventTextAdapter` 是边界：`OUTPUT_DELTA` 转为普通正文 chunk；其他事件转为 `[[ORCH]]{json}\n`。调用链为：

`RunCoordinator / Executor / ModelInvocationRouter → RuntimeEventChannel → RuntimeEventTextAdapter → 当前自定义纯文本块`

## 4. Event Envelope

`RuntimeEvent` 字段：

- `schema_version`
- `event_id`
- `run_id`
- `trace_id`
- `sequence`
- `event_type`
- `emitted_at`
- `step_id`
- `step_sequence`
- `component`
- `payload`

第一版 schema 为 `1`；`event_id` 使用 UUID；`emitted_at` 为 UTC；对象使用 frozen dataclass。

## 5. Event Type

已实现：

- `RUN_STARTED`
- `STEP_STARTED`
- `MODEL_STARTED`
- `MODEL_COMPLETED`
- `TOOL_STARTED`
- `TOOL_COMPLETED`
- `OUTPUT_DELTA`
- `STEP_COMPLETED`
- `ERROR`
- `CANCELLATION`
- `RUN_COMPLETED`

`RUN_COMPLETED` 是唯一 Run terminal event，并携带最终 `status / stop_reason`。

## 6. Payload

已建立对应的不可变强类型 Payload：

`RunStartedPayload`、`StepStartedPayload`、`ModelStartedPayload`、`ModelCompletedPayload`、`ToolStartedPayload`、`ToolCompletedPayload`、`OutputDeltaPayload`、`StepCompletedPayload`、`ErrorPayload`、`CancellationPayload`、`RunCompletedPayload`。

事件构造时校验 Event Type 与 Payload 类型映射，不接受无约束业务 `dict`。Model 元数据只含 profile、candidate/retry index、routing adjustment、breaker key 和安全结果；Error 只含固定安全字段；Tool 类型已定义但真实 Tool 执行本次未接入。

## 7. 安全序列化

`RuntimeEvent.to_safe_dict(include_output=False)` 默认隐藏 `OutputDeltaPayload.text`，仅输出 `text_length` 与信封安全字段。只有 Transport Adapter 显式读取正文。

Prompt、Messages、用户请求、RAG、Memory、Tool 原始参数、Provider 原始异常、Traceback、URL、API Key 和 Secret 均不进入事件元数据。普通日志、Registry Snapshot 和失败摘要不应调用 `include_output=True`。

## 8. Sequence

Global sequence owner 是每个 `RuntimeEventChannel`。Channel 在单个 `asyncio.Lock` 内分配序号并入队，保证单 Run 严格递增、并发唯一和真实 publish 顺序；Run 之间各自从 1 开始。

Step sequence owner 是同一 `StepEventEmitter`。`RunEventEmitter.for_step(step_id)` 缓存并返回同一实例；不同 Step 可以交错，同一 Step 严格递增。取消 publish 不会留下孤儿 `Queue.put`；实现允许将来出现合法序号空洞，但当前成功入队后才提交 owner 计数。

## 9. RunEventEmitter / StepEventEmitter

`RunEventEmitter` 固定 `run_id / trace_id / channel / event loop`，调用方不能伪造其他 Run ID。

`StepEventEmitter` 固定 `step_id` 并持有 step sequence。发布 `STEP_COMPLETED` 时关闭，之后拒绝 `OUTPUT_DELTA` 或任何新事件。`RunEventEmitter.for_step()` 使用短临界区 `threading.Lock` 保护缓存身份；锁内不 await、不调用业务代码，也不参与 Queue backpressure。并发调用同一 step_id 始终得到同一对象；Step 自身以 `asyncio.Lock` 串行分配 step sequence，并发 `STEP_COMPLETED` 只有一个成功，其余旧引用稳定失败。

同步模型 Worker 通过 `run_coroutine_threadsafe` 回到所属 loop，参与同一个有界 Channel backpressure。同步 API 只允许没有运行 Event Loop 的 Worker Thread 调用；Owner Loop 调用立即抛出固定 `EventEmitterSyncError`，async 调用方必须 `await emit()`。所属 loop 已关闭或未运行时也返回固定安全错误，不包含业务正文。

## 10. Event Channel

`RuntimeEventChannel` 是 per-run、单进程、内存级、单 Consumer Channel，包含：

- `capacity`
- `state`
- `publish`
- `close`
- `abort`
- async iterator
- `buffered_count`
- `is_closed`
- `consumer_attached`

Channel 校验事件 `run_id`，不暴露 Queue 内容。

## 11. Bounded Queue

底层使用 `asyncio.Queue(maxsize=capacity)`。`capacity` 必须为正整数并明确拒绝 bool；不存在无界配置。测试证明 `buffered_count` 不超过 capacity。

## 12. Backpressure

第一版策略只有 `BLOCK_PRODUCER`：Queue 满时 `publish` await，不丢事件、不忙轮询、不实现 drop newest/oldest。Consumer 取走条目后 Publisher 恢复。

等待可被 Task cancellation、Channel abort 和 Run cancellation 解锁。取消 Publisher 时会同步取消内部 `Queue.put` Task，不会在调用方退出后偷偷入队。

## 13. 慢 Consumer

受控测试以 capacity 1/2 发布超过容量的事件，Consumer 主动让出 loop。结果证明 Queue 有界、Producer 阻塞、恢复后继续、事件不丢失、global sequence 连续有序；Consumer 中止后 blocked Producer 退出。

## 14. Channel close / abort

状态为 `OPEN / CLOSING / CLOSED / ABORTED`。

正常 `close` 幂等：先把状态改为 `CLOSING`，使全新 publish 在等待锁前快速失败；随后取得同一个 publish lock，等待所有已经通过锁内 OPEN 校验的 accepted/in-flight Publisher 完成或取消，最后才放入 End Sentinel。publish lock 因而是 accepted-publisher barrier。在 `capacity=1`、A 已入队、B 已 accepted 并阻塞时，顺序严格为 `A → B → End Sentinel`，不会出现 Sentinel 抢先或 B 遗留在 Sentinel 后。Sentinel 不计入 `buffered_count`，Consumer 正常结束后没有业务事件残留。集成路径先发布 `RUN_COMPLETED`，再由 Producer `finally` close。

`abort` 用于断开或不可恢复 Transport 关闭：在 `OPEN/CLOSING` 阶段设置 ABORTED、清空未消费事件、唤醒 blocked Publisher，不修改 `AgentState`，也不强制向断开的客户端投递 terminal event。close 等待期间 abort 会解除 Publisher、close 和 Consumer 的等待；若 close 已先完成为 CLOSED，迟到 abort 不改写稳定终态。

## 15. Cancellation / Disconnect

`ChatService.stream_coordinated_agent_events()` 使用 Producer Task + Channel Consumer。处理 `GeneratorExit`、`asyncio.CancelledError`、Consumer 提前 close、Producer 异常、正常 close。

断开清理顺序为：

`CancellationSource.cancel(CLIENT_DISCONNECTED) → channel.abort() → producer_task.cancel() → await cleanup`

`CancellationSource` 保持 first-wins；若先发生 `USER_CANCELLED`，断开清理不会覆盖原因。若 Consumer 仍在线，取消路径在状态终结后发布 `CANCELLATION` 和唯一 `RUN_COMPLETED`。

## 16. RuntimeEventTextAdapter

`RuntimeEventTextAdapter.encode()`：

- `OUTPUT_DELTA`：返回原始最终用户可见文本；
- 其他事件：返回安全 JSON 的 `[[ORCH]]{json}\n`；
- JSON 使用标准转义；
- 不读取 Prompt、业务请求或 OutputDelta 之外的正文；
- 不实现 `data:`、`event:` 等 SSE frame。

`ChatService.stream_coordinated_agent_text()` 是真实 Adapter 入口。

## 17. `[[ORCH]]` 边界

已迁移：

- 新 Coordinated Run/Step/Model 代表性事件；
- Coordinated 最终回答的单个 `OUTPUT_DELTA`；
- `RuntimeEventTextAdapter` 的新控制文本生成；
- `ChatService.stream_coordinated_agent_text()` 的适配调用。

仍保留：

- `AgentRouter._build_orchestration_event()`：Legacy 默认编排流；
- `LegacyAgentRouterDriver`：Legacy final output 过滤；
- `ContextBuilder`：Legacy/安全兼容过滤；
- `main.py::ApiWorker`：UI 解析。

因此不能宣称全项目 `[[ORCH]]` 已清除。

## 18. 当前协议与 SSE 声明

当前 `server.py::chat_endpoint()` 返回 `StreamingResponse(..., media_type="text/plain")`，内容是普通文本 chunk 与可能的 `[[ORCH]]{json}\n` 控制行。历史版本即使曾使用 `text/event-stream`，也没有完整 SSE frame 语义。

结论：当前 HTTP 仍是自定义纯文本分块协议，不是标准 SSE，本次没有实现或宣称标准 SSE。

## 19. RunCoordinator 集成

状态机成功执行 `CREATED → RUNNING` 后发布 `RUN_STARTED`。终结时先 settle active Step，再由状态机提交最终 Run 状态，之后按需要发布 `ERROR` 或 `CANCELLATION`，最后发布一次 `RUN_COMPLETED`。

`RUN_COMPLETED.status / stop_reason` 读取最终 `AgentState`。Event Channel 不是状态机；事件投递故障不会重跑 Run，也不会让 cleanup error 覆盖主终态。

## 20. Step / Parallel 集成

Scheduler 成功把 `PENDING → RUNNING` 并返回 claim 后，Executor 发布 `STEP_STARTED`。最终正文在该 Step 的 `STEP_COMPLETED` 之前发布。状态机成功写入 Step 终态后才发布 `STEP_COMPLETED` 并关闭 StepEmitter。

并发 Step 可以交错，不按 Step ID 重排；同一 Step 保持 `STARTED → OUTPUT_DELTA（若有）→ COMPLETED`。所有 StepCompleted 位于 RunCompleted 之前。预检、取消、deadline/budget cleanup 路径也补发状态提交后的 StepCompleted。

## 21. Model / Retry 集成

`ModelInvocationRouter.invoke()` 接受可选 `StepEventEmitter`。每个真实 Provider Attempt 对应 `MODEL_STARTED / MODEL_COMPLETED`。

`MODEL_STARTED` 的发布 Owner 统一为 `ModelInvocationRouter`。只有 Candidate、Context 复验、Circuit Permit、Budget Reservation、Cancellation/Deadline 复验和 Adapter resolution 全部成功后，Router 才在进入 `Adapter.invoke()` 前发布唯一 Started。生产 `GeneratorModelAdapter` 继续保留真实 provider started callback，但 callback 只确认事实，不重复发布。无 callback 的第三方同步 Adapter 因而也具有真实调用前时间语义，不再事后合成 Started。

Adapter resolve 失败、Budget reserve 失败、Circuit Open、context window 不足等未进入 `Adapter.invoke()` 的候选不发布 Started。进入 invoke 后无论成功、异常或 Adapter 报告 `provider_started=False`，都会为已经发布的 Started 产生对应 Completed；Completed 不会孤立出现。

事件包含稳定的原始 `candidate_index`、同 Profile `retry_index`、routing adjustment 和 breaker key。事件发布异常被隔离，不能透明重复已发生的 Model Attempt。

## 22. OutputDelta

只有 `OutputDeltaPayload.text` 能承载最终用户可见正文。当前最小迁移是“一次完整模型回答对应一个 OutputDelta”，不提供 token-level streaming。

`run_coordinated_agent()` 不再把 Driver 可变字段作为唯一输出来源，而是消费 Event Channel 并聚合 OutputDelta；Driver 字段只保留兼容诊断。

## 23. 最小真实入口迁移

真实入口：

- `ChatService.stream_coordinated_agent_events()`：结构化事件流；
- `ChatService.stream_coordinated_agent_text()`：经 Adapter 的自定义文本流；
- `ChatService.run_coordinated_agent()`：消费同一事件流并返回兼容结果。

默认 `/api/chat → ChatService.stream_chat()` 未切换，避免重写 Legacy HTTP/UI。

## 24. Legacy 与未迁移路径

已迁移：Coordinated 非流式单 Agent、Run/Step/Model 代表性事件、最终单 OutputDelta、文本 Adapter。

未迁移：默认 `stream_chat`、token-level Model Stream、`core_router` 多 Agent、Tool Planner、真实 Tool/RAG 执行、摘要/知识改写及其他直接 Model Engine 调用。Tool Event 只有 schema，没有伪造集成。

## 25. 重点 Bad Case

### Bad Case 1：Runtime 直接拼接 `[[ORCH]]`

- 类型：分层错误
- 触发条件：Coordinator 或 Executor 直接生成控制文本
- 故障表现：内部事件与 UI 协议再次耦合
- 根因分析：缺少独立 Transport Adapter
- 修复方案：Runtime 只发布强类型事件，由 `RuntimeEventTextAdapter` 编码
- 回归测试：非 Output 事件仅经 Adapter 产生 marker，OutputDelta 保持普通文本
- 对应知识点：Ports/Adapters、协议边界
- 面试表达：内部事实模型不应等于传输表示
- 当前状态：已修复；Legacy 生成点明确保留

### Bad Case 2：无界 Queue

- 类型：资源耗尽
- 触发条件：Producer 快于 Consumer 且 Queue 没有 maxsize
- 故障表现：内存持续增长
- 根因分析：没有背压和容量不变量
- 修复方案：强制正整数 capacity，使用 `asyncio.Queue(maxsize=capacity)`
- 回归测试：拒绝 0、负数和 bool；buffered_count 不超过 capacity
- 对应知识点：Bounded Buffer
- 面试表达：有限资源必须把压力反馈给上游
- 当前状态：已修复

### Bad Case 3：Queue 满时丢 RunCompleted

- 类型：终态事件丢失
- 触发条件：用非阻塞 put 或 drop 策略发布 terminal event
- 故障表现：客户端永远不知道 Run 已结束
- 根因分析：把控制事件当成可采样日志
- 修复方案：所有 publish 统一阻塞；RunCompleted 在 Sentinel 前
- 回归测试：terminal-before-sentinel、慢 Consumer 不丢事件
- 对应知识点：Lossless control plane
- 面试表达：第一版宁可背压也不牺牲生命周期事实
- 当前状态：已修复

### Bad Case 4：并行 Step 内事件重排

- 类型：并发顺序错误
- 触发条件：按 Step ID 批量排序后输出
- 故障表现：时间线失真，OutputDelta 可能落到 StepCompleted 后
- 根因分析：混淆全局 publish 顺序与展示分组
- 修复方案：Channel 分配 global sequence；每 Step 独立 step sequence
- 回归测试：两个 Fake Step 交错且各自 1/2/3 单调
- 对应知识点：Partial Order
- 面试表达：允许跨 Step 交错，但必须保持每条因果链
- 当前状态：已修复

### Bad Case 5：状态变更前提前发布

- 类型：事实一致性错误
- 触发条件：先发 STARTED/COMPLETED 再调用状态机
- 故障表现：消费者看到不存在的状态
- 根因分析：把 Event 当成命令
- 修复方案：状态机提交成功后再 emit
- 回归测试：真实路径检查 RunStarted、StepStarted、StepCompleted、RunCompleted 顺序与最终状态
- 对应知识点：Commit-then-publish
- 面试表达：这里的 Event 是状态变化后的事实
- 当前状态：已修复

### Bad Case 6：客户端断开后 Producer 卡在满 Queue

- 类型：任务泄漏
- 触发条件：Consumer 消失但 Publisher 仍 await Queue.put
- 故障表现：Run、线程、Registry 长期不释放
- 根因分析：Queue 满等待没有 abort/cancel 竞争
- 修复方案：abort event、run cancellation 与 put Task 竞争，断开时取消并 await Producer
- 回归测试：capacity=1 提前 aclose，断言 Registry 清理和 blocked Publisher 退出
- 对应知识点：Structured Concurrency
- 面试表达：取消必须沿 Producer/Consumer 拓扑传播
- 当前状态：已修复

### Bad Case 7：OutputDelta 写入普通日志

- 类型：数据泄露
- 触发条件：日志直接 `asdict(event)`
- 故障表现：用户正文进入日志、快照或测试摘要
- 根因分析：安全序列化默认值错误
- 修复方案：`to_safe_dict(include_output=False)` 默认只给长度
- 回归测试：默认字典不含正文，显式 include 才返回 text
- 对应知识点：Secure by Default
- 面试表达：正文读取必须是显式高权限动作
- 当前状态：已修复

### Bad Case 8：跨 Run 共享 sequence

- 类型：所有权错误
- 触发条件：进程级全局计数器
- 故障表现：Run 互相影响，测试和回放边界不清
- 根因分析：Sequence Owner 放错层级
- 修复方案：每个 Channel 独占 sequence
- 回归测试：Run A/B 都从 1 开始，并发 publish 唯一
- 对应知识点：Aggregate ownership
- 面试表达：global 指单 Run 内全局，不是跨 Run 全局
- 当前状态：已修复

### Bad Case 9：Transport 错误覆盖 Run 主终态

- 类型：终态竞争
- 触发条件：Adapter/close 异常再次 finalization
- 故障表现：成功或用户取消被改写为未知失败
- 根因分析：Transport 被误当成生命周期 owner
- 修复方案：RunCoordinator 保持 first-wins；abort 不修改 AgentState
- 回归测试：用户取消先发生后 disconnect，原因仍为 USER_CANCELLED
- 对应知识点：Single terminal owner
- 面试表达：传输关闭只影响投递，不重写业务终态
- 当前状态：已修复

### Bad Case 10：把当前流称为标准 SSE

- 类型：协议认知错误
- 触发条件：只根据 StreamingResponse 或 media type 命名
- 故障表现：客户端按 `data:` frame 解析失败
- 根因分析：混淆 HTTP 流式传输与 SSE frame 语义
- 修复方案：文档与代码统一称“自定义纯文本分块协议”
- 回归测试：Adapter 输出不以 `data:` 开始且没有 `event:` frame
- 对应知识点：Wire Protocol Semantics
- 面试表达：Content-Type 不能替代 frame 规范
- 当前状态：已修复声明；未实施 SSE

## 26. 测试命令和结果

执行结果：

- 本次并发边界目标 pytest：`101 passed, 16 subtests passed`
- Runtime 指定 unittest 组合：`Ran 226 tests ... OK`
- 全量 pytest：`328 passed, 42 subtests passed`
- `python -m compileall -q core tests`：通过
- `git diff --check`：通过

测试全部使用 Fake/本地对象，没有调用真实模型、Provider、网络、Chroma、数据库服务或 UI。Memory 集成测试仅使用临时 SQLite 文件。

## 27. 未完成事项和已知风险

- 默认 Legacy 流式路径尚未迁移。
- 当前 Coordinated 入口每个完整回答只有一个 OutputDelta。
- 当前协议不是标准 SSE。
- Event Channel 仅单进程内存级。
- 不支持 Replay、Snapshot 或 Event Journal。
- 不支持多 Subscriber。
- Python 协作式取消不能强停正在执行的 C 扩展或不可取消阻塞调用。
- OutputDelta 不应进入普通日志。
- Tool/RAG 尚未真实接入 Runtime Event。
- Event Journal 留到第 19 天。
- Trace 能力扩展留到第 21 天；本次只透传既有 trace_id。
- Snapshot/Replay 留到第 22 天。
- 第三方同步 Model Adapter 不需要 started callback；Router 在真实 `invoke` 调用前统一发布 Started。生产 callback 仅确认 provider started，不重复发事件。

## 28. 面试表达

我把 Runtime 内部事实与现有 UI 文本协议分开：每个 Run 拥有一个有界、无丢失、阻塞 Producer 的 Event Channel；Channel 统一分配 global sequence，StepEmitter 分配局部 sequence。状态机提交后才发事件。慢客户端通过 backpressure 限制内存，断开则用 first-wins cancellation、abort 和 structured cleanup 解锁 Producer。正文只允许进入 OutputDelta，安全序列化默认隐藏正文，最后由单独 Adapter 转为现有自定义文本块，而不是把它误称为 SSE。

## 29. 需要带回 ChatGPT 审查的信息

- 新增文件：`events.py`、`event_channel.py`、`event_emitter.py`、`stream_adapter.py` 和四个对应测试文件。
- 修改文件：`run_coordinator.py`、`parallel_execution.py`、`model_invocation.py`、`core/runtime/__init__.py`、`chat_service.py`、`agent_router.py`。
- Envelope：schema/event/run/trace/global sequence/time/step/step sequence/component/payload。
- Event Type：11 种固定类型；RunCompleted 唯一 Run terminal。
- Payload：强类型 frozen dataclass，不接收业务 dict。
- OutputDelta：唯一正文载体；安全序列化默认只给长度。
- Sequence Owner：per-run Channel；Step sequence owner 为 StepEmitter。
- Channel capacity：ChatService 默认 32，可注入正整数。
- Backpressure：只有 BLOCK_PRODUCER；满 Queue await。
- close：排空后 Sentinel，幂等，拒绝新 publish。
- abort：可丢未消费事件，解锁 Publisher，不改 AgentState。
- Producer/Consumer：Producer Task + async iterator 单 Consumer。
- Disconnect：CLIENT_DISCONNECTED → abort → cancel Producer → await。
- Cancel first-wins：USER_CANCELLED 不被 disconnect 覆盖。
- Adapter：OutputDelta 普通文本，其余安全 `[[ORCH]]` JSON。
- `[[ORCH]]` 新生成位置：只在 `RuntimeEventTextAdapter`；Legacy `AgentRouter` 生成仍保留。
- 当前协议：`text/plain` 自定义分块，不是标准 SSE。
- Run 顺序：状态 RUNNING → RUN_STARTED；最终状态 → ERROR/CANCELLATION → RUN_COMPLETED。
- Step 顺序：RUNNING → STEP_STARTED；OutputDelta；终态 → STEP_COMPLETED。
- Model Attempt：真实 Attempt 对应 started/completed；未 started 候选不伪造。
- Retry/Fallback：candidate_index / retry_index / adjustment / breaker key。
- 并行顺序：跨 Step 可交错；同 Step 单调；Run terminal 最后。
- 最小真实入口：events、text adapter、兼容 result 三个 ChatService 方法。
- 已迁移与未迁移路径：见第 23、24 节。
- 测试结果：指定组合及全量均通过。
- Bad Case：十项均按触发、根因、修复和回归记录。
- 人工确认问题：后续何时把 `/api/chat` 默认入口切换到 Coordinated 文本流；本次未擅自切换。
- 后续建议：按既定日程推进 Journal/Trace/Snapshot，不在本次实现。

## 30. 小范围并发边界补查

本次补查只覆盖 Channel close、StepEmitter、MODEL_STARTED 时间语义和同步线程发布：

- close in-flight barrier：使用既有 `asyncio.Lock` 作为 accepted-publisher barrier。close 先写入 CLOSING，随后等待 publish lock；锁内 OPEN 复验成功的 Publisher 必须先完成或取消。
- blocked publisher 与 close：capacity=1 时，A 占满 Queue、B 已 accepted 并阻塞，close 不会绕过 B。Consumer 读取 A 后，B 入队并返回，随后 Sentinel 才入队。
- Sentinel 顺序：严格为 `A → B → End Sentinel`。Sentinel 不计入 `buffered_count`，正常 Consumer 结束后无业务事件残留。
- close 与 abort：abort 在 CLOSING 期间唤醒 blocked Publisher 并使 close 退出，最终为 ABORTED；已完成 CLOSED 后迟到 abort 不改写终态。
- StepEmitter cache：`threading.Lock` 只保护 `step_id → StepEventEmitter` 缓存身份，不持有到 emit/backpressure。
- 并发 step_sequence：同 Step 的 `asyncio.Lock` 保证唯一单调；不同 Step 独立从 1 开始。
- 并发 StepCompleted：只有一个调用成功并关闭共享 Emitter，其他旧引用统一抛出稳定 RuntimeError。
- MODEL_STARTED 发布 Owner：`ModelInvocationRouter` 在进入 `Adapter.invoke()` 前发布。
- 无 callback Adapter：同样在真实调用前获得 Started；不再事后补发，也不需要 synthetic 时间字段。
- 未开始 Attempt：Adapter resolve、Budget reserve、Circuit Open 等未进入 invoke 的候选没有 Started。
- MODEL_COMPLETED：仅在对应 Started 已成功发布后产生，成功、异常及 provider_started=False 异常均保持同一 candidate/retry 元数据。
- 同步 emit 线程限制：只允许无运行 Event Loop 的 Worker Thread；Worker 仍参与 Queue backpressure。
- Owner Loop 调用：快速抛出 `EMITTER_OWNER_LOOP_SYNC_CALL`；loop 关闭或未运行返回 `EMITTER_EVENT_LOOP_UNAVAILABLE`。
- 新增测试：20 个并发边界测试，覆盖 5 个 Channel、4 个 Step cache/sequence、6 个 Model timing、5 个同步 Emitter 场景。
- 人工确认：本次四项边界无待确认实现问题；默认 `/api/chat` 是否切换 Coordinated 入口仍是既有后续产品决策，本次不处理。
