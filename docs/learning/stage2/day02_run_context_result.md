# 阶段二第 2 天改造结果

## 1. 本次任务目标

为 LocalAgent 建立最小、明确、可测试的 RunContext 基础，使一次聊天请求具备 `run_id`、`session_id`、`trace_id`、创建时间、Deadline、Cancellation Token，并明确可序列化运行数据与进程内依赖对象的边界。

## 2. 修改前现状

- `server.py::chat_endpoint()` 是 `/api/chat` 的 FastAPI 流式入口，直接桥接 `ChatService.stream_chat()`。
- `core/chat_service.py::ChatService.stream_chat()` 只负责拼接可选 `file_path` 文本，然后调用 `AgentRouter.chat_stream()`。
- `core/agent_router.py::AgentRouter.chat_stream()` 同时承担用户消息持久化、单 Agent 回答、多 Agent 编排分支、RAG 注入、Tool 调用、模型调用和助手消息持久化等职责。
- `MemoryManager` 只有 `memory_scope`（如 `direct`、`orchestration`）用于记忆作用域，没有稳定的请求级或会话级 session 字段。
- 项目没有独立 Run、RunContext、AgentState 或 Runtime。
- `pyproject.toml` 未声明 pytest；因此本次测试采用标准库 `unittest`。

## 3. 发现的问题

- 每次聊天请求缺少稳定的运行标识，后续难以关联日志、取消、超时和追踪。
- 当前执行链没有 Deadline 或协作式 Cancellation 的共同检查入口。
- Router 内部职责混杂，但现阶段整体拆分风险高，适合通过兼容参数逐步接入上下文。
- 现有 Memory Scope 不是 session 概念，不能将其静默当作用户会话 ID。
- 本地测试环境缺少部分项目依赖（如 `langchain_chroma`、`requests`），原先 `AgentRouter` 顶层类型导入会影响轻量可导入性检查。

## 4. 最终设计方案

- 新增 `core/runtime/`，集中放置最小 Runtime Context 基础能力。
- 在 `ChatService.stream_chat()` 创建根 `RunContext`，并显式传入 `AgentRouter.chat_stream()`。
- `AgentRouter.chat_stream()` 保持兼容签名：新增可选 `run_context: RunContext | None = None`，正常主入口始终传入 Context。
- `session_id` 采用 `legacy-default` 兼容策略，不修改 `/api/chat` 请求体和 Memory 数据库 Schema。
- Deadline 同时保存 UTC `deadline_at` 和 monotonic deadline；当前主入口不设置默认 timeout，避免改变现有响应行为。
- Cancellation 使用 `threading.Event` 与 `threading.Lock`，建立同步生成器、线程和阻塞调用链均可观察的协作式基础。
- 序列化只输出明确数据字段，不包含 Clock、Token、Event、Lock、模型、DB、回调等依赖对象。

## 5. 新增文件

- `core/runtime/__init__.py`
- `core/runtime/cancellation.py`
- `core/runtime/context.py`
- `tests/test_runtime_context.py`
- `docs/learning/stage2/day02_run_context_result.md`

## 6. 修改文件

- `core/chat_service.py`
- `core/agent_router.py`

## 7. 核心类、接口和数据结构

- `RunContext`：一次 Run 的上下文对象，组合可序列化数据、Deadline、Cancellation Token 和 Clock，提供 `remaining_seconds()`、`raise_if_inactive()`、`to_dict()`。
- `RunContextData`：只保存可序列化运行数据：`RunIdentifiers`、`created_at`、`deadline_at`、`entry_agent_id`。
- `RunIdentifiers`：明确区分 `run_id`（一次 Agent 执行）、`session_id`（连续会话兼容标识）、`trace_id`（端到端追踪关联标识）。
- `CancellationSource`：拥有取消权限，`cancel()` 幂等，首次取消原因保留。
- `CancellationToken`：只读观察取消状态，提供 `is_cancelled()`、`raise_if_cancelled()`。
- `Deadline`：同时持有可序列化 UTC `deadline_at` 和进程内 monotonic deadline，提供剩余时间与过期检查。
- `Clock`：最小 Protocol，支持 `utc_now()` 与 `monotonic()`，测试中使用 `FakeClock`。
- 异常类型：`RunCancelledError`、`RunDeadlineExceededError`。

## 8. 关键执行流程

### RunContext 创建流程

1. `/api/chat` 请求体保持不变。
2. `server.py::chat_endpoint()` 调用 `ChatService.stream_chat()`。
3. `ChatService.stream_chat()` 拼接原有 `file_path` 兼容文本后，调用 `RunContext.create(entry_agent_id=agent_id, session_id="legacy-default")`。
4. `RunContext.create()` 使用 `uuid.uuid4().hex` 生成 `run_id` 和 `trace_id`，使用 `SystemClock` 记录 `created_at`。

### ChatService 到 AgentRouter 的传递流程

`ChatService.stream_chat()` 通过显式参数将 `run_context` 传入 `AgentRouter.chat_stream(user_query=..., agent_id=..., run_context=...)`。Router 内部向单 Agent、编排、委派执行、最终回答生成等关键边界继续传递同一个 Context，不在深层创建新的根 Context。

### Deadline 检查流程

- 创建 `Deadline` 时，如果 `timeout_seconds is None`，表示无 Deadline。
- 如果提供零或负 timeout，立即抛出 `ValueError`。
- 如果提供正 timeout，UTC `deadline_at` 用于序列化，monotonic deadline 用于当前进程内剩余时间计算。
- `RunContext.raise_if_inactive()` 会先检查取消，再检查 Deadline 是否过期。

### Cancellation 检查流程

- `CancellationSource.cancel()` 只有首次调用返回 `True`，重复取消返回 `False`。
- `CancellationToken` 只能观察，不能发出取消。
- Router 在用户消息持久化前、编排开始前、Tool 调用前后、模型调用前、流式 chunk 之间、助手消息持久化前等边界调用 `raise_if_inactive()`。

### 序列化流程

`RunContext.to_dict()` 委托 `RunContextData.to_dict()`，只输出 `run_id`、`session_id`、`trace_id`、`created_at`、`deadline_at`、`entry_agent_id`。Clock、Token、Event、Lock、模型实例、数据库连接、Generator、回调等进程内对象明确排除。

## 9. 与现有功能的兼容方式

- `/api/chat` 请求体未修改。
- FastAPI 路由未修改。
- `StreamingResponse` 和现有自定义文本流未修改。
- `[[ORCH]]{json}\n` 格式未修改。
- Memory 数据库 Schema 未修改。
- Tool、RAG、多 Agent 编排入口未删除，只增加轻量上下文检查。
- `AgentRouter.chat_stream()` 允许 `run_context=None` 作为临时兼容参数；可在后续所有调用方迁移后移除。

## 10. 异常处理和边界情况

- Deadline 到期抛出 `RunDeadlineExceededError`。
- Token 取消后抛出 `RunCancelledError`。
- 零或负 timeout 被拒绝。
- 无 Deadline 时 `remaining_seconds()` 返回 `None`。
- 当前异常仍会由现有 `server.py` 流式错误文本兜底输出，本次未系统性改造错误协议。

## 11. 测试内容

新增 `tests/test_runtime_context.py`，覆盖：

1. `run_id`、`trace_id` 非空；
2. 不同 Run 的 `run_id` 不同；
3. 同一 Context 内标识稳定；
4. `session_id` 使用 `legacy-default`；
5. 无 Deadline 时 `remaining_seconds()` 返回 `None`；
6. Deadline 剩余时间计算；
7. Deadline 到期后抛出明确异常；
8. 零或负 timeout 被拒绝；
9. CancellationSource 首次取消返回成功；
10. 重复取消保持幂等；
11. 第一个取消原因不被覆盖；
12. Token 取消后抛出明确异常；
13. 序列化输出不包含 Clock、Token、Event、Lock；
14. 使用 FakeClock，不真实等待；
15. `ChatService` 与 `AgentRouter` 轻量可导入性检查。

## 12. 实际执行的测试命令

- `python -m unittest tests.test_runtime_context -v`
- `python -m compileall core tests`

## 13. 测试结果

- `python -m unittest tests.test_runtime_context -v`：通过，11 个测试全部 OK。
- `python -m compileall core tests`：通过。

## 14. 未完成事项

- 未实现客户端断开到 Token 的传播。
- 未实现 cancel API。
- 未实现 AgentState、状态机、Scheduler、Checkpoint、Resume、Retry、Budget、Trace Span。
- 未持久化 RunContext。
- 未为每个 Tool、Model、RAG 接口增加 Context 参数。

## 15. 已知风险

- Cancellation Token 目前不能立即打断已进入的 llama.cpp 推理、同步 Tool、阻塞文件操作或第三方库调用。
- 客户端断开尚未传播到 Token。
- 尚未实现 AgentState 和状态机。
- `session_id` 当前是兼容语义 `legacy-default`，不代表真实用户会话。
- RunContext 尚未持久化，进程重启后不可恢复。

## 16. 设计权衡

- 选择在 `ChatService.stream_chat()` 创建 Context，因为它是当前统一应用服务入口，既靠近 FastAPI，又不需要改请求体。
- 选择 `legacy-default` 而非随机 session，避免把每次 Run 误建为新会话，也避免将 `run_id` 静默复用为 `session_id`。
- 选择 `threading.Event` 而非 `asyncio.Event`，因为当前核心执行路径以同步 generator 和阻塞调用为主。
- 选择可选 `run_context` 参数作为兼容过渡，避免一次性重写全部遗留编排器。
- 未引入任何大型依赖、DI 框架或全局 `CURRENT_RUN_CONTEXT`。

## 17. 可用于面试的项目描述

在 LocalAgent 的遗留编排器前建立了最小 RunContext 基础：每次聊天 Run 拥有非敏感运行标识、兼容 session 标识、trace 标识、创建时间、可测试 Deadline 和协作式 Cancellation Token。上下文从 ChatService 显式传递到 AgentRouter，并在模型、工具、编排和持久化边界增加轻量检查，为后续 Runtime 演进提供基础，但尚未实现完整取消传播、持久化执行、Checkpoint、状态机或生产级 Runtime。

## 18. 需要带回 ChatGPT 审查的信息

- 最重要设计决策：先建立最小可测试 RunContext，不拆分 AgentRouter，不实现完整 Runtime。
- 真实创建位置：`core/chat_service.py::ChatService.stream_chat()`。
- 真实传递链：`server.py::chat_endpoint()` → `ChatService.stream_chat()` → `AgentRouter.chat_stream()` → Router 内部关键边界。
- `run_id` 生成策略：`uuid.uuid4().hex`，每个 Run 新建。
- `session_id` 生成策略：固定兼容值 `legacy-default`。
- `trace_id` 生成策略：默认 `uuid.uuid4().hex`，也允许显式传入。
- session_id 与现有 Memory 的映射：现有 Memory 只有 `memory_scope`，没有真实 session 字段；因此使用 `legacy-default`，不修改 Schema。
- Deadline 实现：UTC `deadline_at` 用于序列化，`time.monotonic()` 用于当前进程剩余时间计算。
- Cancellation Token 同步原语：`threading.Event` + `threading.Lock`。
- 增加的检查点：用户消息持久化前、编排开始前、Tool 调用前后、模型调用前、流式 chunk 之间、助手消息持久化前、委派 Agent 前后。
- 序列化排除依赖：Clock、Token、Event、Lock、模型、数据库连接、HTTP client、Chroma client、Task、Generator、回调函数等。
- 修改前方法签名：`ChatService.stream_chat(agent_id, query, file_path="")`；`AgentRouter.chat_stream(user_query, agent_id="core_router")`。
- 修改后方法签名：`ChatService.stream_chat(agent_id, query, file_path="")` 保持外部不变；`AgentRouter.chat_stream(user_query, agent_id="core_router", run_context: RunContext | None = None)`。
- 测试命令及结果：`python -m unittest tests.test_runtime_context -v` 通过；`python -m compileall core tests` 通过。
- 测试失败或无法执行原因：初始导入检查暴露环境缺少 `langchain_chroma`、`requests`；已将 AgentRouter 中仅用于类型标注的重依赖改为 `TYPE_CHECKING` 导入，以支持轻量导入检查。
- 尚不确定的问题：未来真实 session 来源应来自 UI、API 网关还是 Memory 层仍需产品层确认。
- 后续建议：下一步可在不改变现有 API 的前提下讨论真实 session 来源和更完整的 Runtime 边界，但不要在本次直接实现第 3 天内容。

## 19. 补充审查与修正记录

### CancellationSource 创建位置和所有者

补充审查发现，初版 `RunContext.create()` 在未传入 `cancellation_source` 时会在工厂内部创建 `CancellationSource`，但只把 `source.token` 放入 `RunContext`，返回后 Source 引用会丢失。这不符合“上游持有 Source、RunContext 只持有 Token、Router 只能观察 Token”的所有权目标。

本次最小修复如下：

- 新增 `create_run_context(...) -> tuple[RunContext, CancellationSource]`；
- `create_run_context()` 创建或接收 `CancellationSource`，将 Token 注入 `RunContext`，并把 Source 显式返回给调用方；
- `ChatService.stream_chat()` 改为调用 `create_run_context()`，并在 generator frame 内保留 `cancellation_source` 引用；
- `RunContext` 仍只持有 `CancellationToken`，不持有 Source，也没有取消权限；
- `AgentRouter` 及其下游仍只接收 `RunContext`，只能通过 Token 观察取消。

修复前创建方法：

```python
RunContext.create(...) -> RunContext
```

修复后主入口创建方法：

```python
create_run_context(...) -> tuple[RunContext, CancellationSource]
```

`RunContext.create()` 保留为兼容辅助方法，但主调用链不再使用它创建根 RunContext。

### ChatService 到 AgentRouter 行为测试

新增轻量 Fake Router 测试，不启动真实模型、Tool、RAG、Chroma、数据库或 UI，验证：

- `ChatService.stream_chat()` 创建非空 `RunContext`；
- Context 被传给 Fake Router 的 `chat_stream()`；
- 流式 generator 迭代期间使用的是同一个 Context；
- `entry_agent_id` 等于请求 `agent_id`；
- `session_id` 等于 `legacy-default`；
- `run_id` 和 `trace_id` 非空；
- 输出仍为 Fake Router 原始 chunk，未改变流式内容。

### TYPE_CHECKING 修改检查结果

当前移动到 `TYPE_CHECKING` 下的 import 只有：

```python
from core.knowledge_base.vector_db_manager import VectorDBManager
from core.llm_engine import LocalLLMEngine
```

安全依据：

- `VectorDBManager` 仅用于 `AgentRouter.__init__()` 的 `db_manager` 类型标注；
- `LocalLLMEngine` 仅用于 `AgentRouter.__init__()` 的 `llm_engine` 类型标注；
- 运行时代码没有实例化这两个类型；
- 运行时代码没有对这两个类型执行 `isinstance()`；
- 文件已启用 `from __future__ import annotations`，并且对应标注使用字符串形式，缺失名称不会在运行时解析；
- 该修改不隐藏正常运行依赖：服务启动仍由 `server.py` 在运行时导入并构造真实 LLM、VectorDB 依赖；这里只是避免 Router 模块因类型标注导入而强制加载重依赖。

### 基础不变量检查结果

本次补充了小型不变量校验：

- `RunIdentifiers` 和 `RunContextData` 保持 `@dataclass(frozen=True)` 不可变；
- `RunIdentifiers` 拒绝空 `run_id`、`session_id`、`trace_id`；
- `RunContextData` 拒绝空 `entry_agent_id`；
- `RunContextData` 校验 `created_at` 和 `deadline_at` 必须为 timezone-aware UTC datetime；
- `RunContext.create()` / `create_run_context()` 拒绝空 `entry_agent_id` 和显式空 `trace_id`；
- `Deadline` 拒绝零、负数、NaN、正无穷和负无穷 timeout；
- `to_dict()` 继续只输出字符串或 `None`，保持 JSON 友好，不包含进程内依赖。

### 新增或修改的测试

- 新增 `create_run_context()` 返回 Context 与 Source 的测试，并验证 Source 取消能被 Context 观察；
- 新增 ChatService → Router 行为测试；
- 新增 NaN / Infinity timeout 拒绝测试；
- 新增空标识、空 `entry_agent_id`、空 `trace_id` 拒绝测试；
- 新增 `created_at` / `deadline_at` timezone-aware UTC 测试；
- 原有 Runtime Context、Deadline、Cancellation、序列化和导入测试继续保留。

### 实际测试命令与结果

- `python -m unittest tests.test_runtime_context -v`：通过，15 个测试全部 OK。
- `python -m compileall core tests`：通过。
