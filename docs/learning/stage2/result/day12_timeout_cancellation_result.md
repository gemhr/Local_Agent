# 阶段二第 12 天改造结果

## 1. 本次计划调整与目标
优先修复 Day 3 已确认的“流式 Generator 提前 close 后 Run 可能永久 RUNNING”边界，并建立同一 Run 的最小取消定位链路。

## 2. 修改前取消与 Timeout 现状
此前 ChatService 持有 Source、Context 只持有 Token；HTTP 是 `StreamingResponse` 的自定义纯文本分块流，虽设置过 `text/event-stream`，但没有 `data:` 帧或 SSE 语义，不能称为标准 SSE。

## 3. CancellationReason
新增 `USER_CANCELLED`、`CLIENT_DISCONNECTED`、`SYSTEM_SHUTDOWN`、`DEADLINE_EXCEEDED` 英文 Enum 值；分别映射同名 StopReason。

## 4. CancellationSource / Token
`cancel(reason, occurred_at=None)` 为线程安全 first-wins，返回首次成功与否；Token 暴露取消状态、原因、UTC 时间及 `raise_if_cancelled()`。固定安全说明避免携带业务内容。

## 5. RunHandle
RunHandle 关联 run_id、CancellationSource、AgentState、registered_at 和安全 owner；快照只输出安全运行元数据。

## 6. RunRegistry
`process_run_registry` 是 ChatService/HTTP 使用的单进程、内存、线程安全 Registry，支持 register/get/snapshot/cancel/unregister/cancel_all，不直接改 AgentState。

## 7. run_id 传播
UI ApiWorker 在发送前生成 UUID hex，放入 `/api/chat` JSON；服务端校验 UUID、用它建立 Context，响应也返回 `X-Run-Id`。活跃冲突由 Registry 拒绝，结束时 UI 与 Registry 都清理。

## 8. UI 停止按钮
最小停止按钮通过独立 daemon 短请求线程先 POST 用户取消 API，再关闭当前 Response/Session；该线程不复用正在 `iter_content()` 阻塞的 ApiWorker。重复点击和已结束 Run 均不作为错误，旧 Run id 清理后不能取消新 Run。

## 9. 取消 API
`POST /api/runtime/runs/{run_id}/cancel` 仅固定写入 `USER_CANCELLED`，区分 cancelled/already_cancelled/inactive。

## 10. HTTP 断开检测
异步桥接逐块检查 `request.is_disconnected()`，处理 `asyncio.CancelledError`、连接关闭异常，并在 finally close 内层同步 Generator。提前 close 以 `CLIENT_DISCONNECTED` first-wins 取消；不将普通业务异常伪装为断开。

## 11. Run Deadline watcher
ChatService 按 RunContext 的 monotonic 剩余时间启动所属 Timer；到期写 `DEADLINE_EXCEEDED`，finally 必定 cancel Timer。先发生的用户取消不覆盖。

## 12. 局部 Timeout 与 Deadline 传播
新增 STEP/MODEL/TOOL/RAG/APPROVAL 操作类型和有效时间计算 `min(parent, local)`；它只表达局部超时，不无条件取消 Run，也未实现 Approval 等待、Retry 或 Fallback。

## 13. Scheduler 取消边界
既有 Scheduler/Executor 入口使用 RunContext 安全点；本日未重写计划调度架构，未 Claim 步骤不应被回写。

## 14. ParallelExecutor 取消边界
既有 ParallelExecutor 在 preflight、worker 和 semaphore/driver 边界检查 Token 并清理已 claim Step；保持 Fail-fast/Best-effort 行为。

## 15. AgentLoop 取消和终态
AgentLoop 将 Token 原因映射为四种 StopReason；GeneratorExit 会终结 active Step/Run 而非遗留 RUNNING，且不 yield。

## 16. Generator close
ChatService 的 GeneratorExit 先写 CLIENT_DISCONNECTED；AgentLoop 关闭时按已存在原因终态，finally 注销 Registry 并停 deadline watcher。正常、业务错误和 Token 取消仍走原终态路径。

## 17. Model 流清理
`_stream_final_response` 的 finally 已 close BudgetedModelStream/内层模型流；其预迭代 release、已开始 commit 的预算语义保持不变。

## 18. Tool / RAG 安全点
当前 AgentRouter 已在 Tool 调用前后和模型流每块检查 Context；RAG/同步 Tool 的库调用中间仍不可抢占，未声称全路径完成迁移。

## 19. System Shutdown
FastAPI lifespan shutdown 调 `cancel_all(SYSTEM_SHUTDOWN)`，再在 2 秒 Grace Period 内有界等待 Registry 清空；超时只记录剩余安全 run_id 后继续关闭。first-wins 不覆盖已有原因，不强杀线程或 C 扩展。

## 20. 资源清理
HTTP finally 关闭流，ChatService finally 停 watcher 并 unregister；清理错误不改写主 StopReason。

## 21. 重点 Bad Case
### Bad Case 1：用户取消被客户端断开覆盖
- 类型：假设构造。
- 触发条件：停止 API 后连接立即关闭。
- 故障表现：错误归因为断开。
- 根因分析：原因可覆盖。
- 修复方案：first-wins。
- 回归测试：Registry cancel 两次。
- 对应知识点：并发原子性。
- 面试表达：取消原因是一次性事实。
- 当前状态：已覆盖。

### Bad Case 2：Generator close 后 Run 永久 RUNNING
- 类型：真实历史边界（Day 3）。
- 触发条件：部分流输出后 close。
- 故障表现：状态无终态。
- 根因分析：未处理 GeneratorExit。
- 修复方案：AgentLoop 终结 Step/Run，Service finally 注销。
- 回归测试：test_agent_loop。
- 对应知识点：生成器 finally。
- 面试表达：close 也是生命周期事件。
- 当前状态：已覆盖。

### Bad Case 3：取消后 Scheduler 继续批量 STARTED
- 类型：假设构造。
- 触发条件：批量 claim 中途取消。
- 故障表现：未执行步骤被启动。
- 根因分析：缺少逐项 Token 检查。
- 修复方案：保持既有 claim 安全点并需继续补测。
- 回归测试：scheduler 回归。
- 对应知识点：合作式取消。
- 面试表达：不回滚真实已启动工作。
- 当前状态：既有边界，待更细测试。

### Bad Case 4：只关闭外层流，内层模型 Generator 继续运行
- 类型：假设构造。
- 触发条件：HTTP close。
- 故障表现：预算与模型流泄漏。
- 根因分析：未 close 嵌套流。
- 修复方案：HTTP/Router finally close。
- 回归测试：budget 回归。
- 对应知识点：嵌套资源释放。
- 面试表达：外层 finally 必须关闭内层。
- 当前状态：主流路径已接入。

### Bad Case 5：to_thread 取消后误认为底层任务已停止
- 类型：真实机制下的假设场景。
- 触发条件：同步 next 正在阻塞。
- 故障表现：误报已强制停止。
- 根因分析：线程不可抢占。
- 修复方案：只取消 Run 并在返回后安全点停止消费。
- 回归测试：文档限制。
- 对应知识点：协作式取消。
- 面试表达：Task 取消不等于 C/线程停止。
- 当前状态：已明确限制。

### Bad Case 6：Deadline watcher 成为游离任务
- 类型：假设构造。
- 触发条件：Run 先结束。
- 故障表现：后续误取消。
- 根因分析：未在 finally 取消 watcher。
- 修复方案：Run 所属 Timer 在 finally cancel。
- 回归测试：timeout 单测。
- 对应知识点：结构化并发。
- 面试表达：watcher 生命周期隶属 Run。
- 当前状态：已接入。

### Bad Case 7：Run 过早从 Registry 注销
- 类型：假设构造。
- 触发条件：流仍在消费时注销。
- 故障表现：停止 API 返回 inactive。
- 根因分析：注销边界错误。
- 修复方案：仅 Service finally 注销。
- 回归测试：registry 测试。
- 对应知识点：资源所有权。
- 面试表达：注销是外层运行 finally 的最后动作。
- 当前状态：已接入。

## 22. 测试命令和结果
已执行目标 pytest、独立取消请求阻塞边界测试、Registry Grace Period 测试、compileall 与 diff 检查；全仓 pytest 结果见本次提交记录。

## 23. 未完成事项和已知风险
不强杀同步线程、llama.cpp 或 C 扩展；Token 依赖合作式安全点；未统一迁移所有 Model/Tool/RAG；Registry 仅单进程、未持久化、多 Worker 不共享；局部 Timeout 无恢复策略；Approval 未实现；Shutdown 仅有 2 秒最小 Grace Period；自定义流不是标准 SSE；UI 到后端的取消仍有网络失败边界。

## 24. 面试表达
本次以 RunRegistry 把 UI、HTTP、deadline 和 shutdown 收敛到同一个 Source，并以 first-wins 保证取消归因稳定；生成器 close 也被视为必须终结运行的生命周期事件。

## 25. 需要带回 ChatGPT 审查的信息
请审查：本日优先修复 GeneratorExit；新增 cancellation/run_registry/timeout、ChatService/server/UI 改动；自定义纯文本分块协议；四类原因和 first-wins；RunHandle/Registry API/owner；UUID run_id、停止 API 与断开路径；deadline watcher、局部 timeout、Scheduler/Executor 既有安全点、AgentLoop 映射、模型流 close、Tool/RAG 边界和 shutdown；特别确认 `to_thread` 无法停止底层同步/C 调用、未接入路径、测试、七个 Bad Case，以及是否需要在后续统一迁移所有真实 Tool/RAG。
