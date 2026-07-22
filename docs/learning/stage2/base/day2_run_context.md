# 第 2 天 Codex 结果审查

## 一、审查结论

**整体实现质量较好，但暂时只能判定为“条件通过”。**

已经完成的核心内容包括：

- 建立 `RunContext`（运行上下文）基础模型；
- 区分 `run_id`、`session_id`、`trace_id`；
- 使用 UTC 时间和 Monotonic Clock（单调时钟）实现 Deadline（截止时间）；
- 使用 `CancellationSource`（取消源）与 `CancellationToken`（取消令牌）分离；
- 在 `ChatService` 创建根上下文，并显式传递给 `AgentRouter`；
- 保持 API、Memory Schema（记忆数据库结构）和流式协议兼容；
- 使用 `unittest` 和 `FakeClock` 完成基础测试；
- 没有提前引入 AgentState、状态机、Scheduler 等第 3 天以后内容。

但是有一个**必须确认的设计问题**和一个**应该补充的测试问题**：

1. `CancellationSource` 的所有权和生命周期没有说清楚；
2. 缺少对 `ChatService → AgentRouter` 真实上下文传递的行为测试。

在这两个问题解决前，不建议直接进入第 3 天代码实操。

------

# 二、做得正确的部分

## 1. RunContext 创建位置正确

将根 RunContext 创建在：

```text
core/chat_service.py::ChatService.stream_chat()
```

是当前阶段合理的选择。

它满足：

- 比 FastAPI 路由更接近应用层；
- 比 `AgentRouter` 更靠近执行入口；
- 不要求前端理解 Runtime；
- 不需要修改 `/api/chat` 请求体；
- 可以在后续自然迁移到 RunService。

当前链路：

```text
server.py
→ ChatService.stream_chat()
→ AgentRouter.chat_stream(run_context=...)
```

符合第 2 天目标。

------

## 2. 没有错误映射 `session_id`

Codex 没有把：

```text
session_id = run_id
```

也没有为每个 Run 随机创建所谓 Session。

而是明确指出当前项目不存在真实会话标识，暂时使用：

```text
legacy-default
```

这属于合理的兼容策略。

但需要明确：

> `legacy-default` 只是兼容占位符，不能作为用户身份、租户隔离或权限边界。

LocalAgent 当前主要是单用户桌面应用，这种临时方案可以接受。

------

## 3. Deadline 设计正确

Codex正确区分了：

```text
deadline_at
```

用于：

- 序列化；
- 日志；
- 未来持久化；
- 跨进程恢复时重新计算。

以及：

```text
monotonic deadline
```

用于：

- 当前进程内计算剩余时间；
- 避免系统时间调整造成误判。

同时没有在每个调用层重新设置完整 Timeout（超时时长），这一点非常重要。

当前主链路不设置默认 Timeout，意味着：

> 已经建立 Deadline 能力，但生产聊天请求暂未默认启用截止时间。

这是可以接受的，因为今天不应该擅自改变原有长文本生成行为。完整多层 Timeout 属于第 12 天。

------

## 4. Cancellation 使用了合适的同步原语

当前执行链以：

- 同步 Generator（生成器）；
- PyQt 线程；
- 阻塞模型调用；
- 同步 Tool 调用

为主，所以选择：

```text
threading.Event
threading.Lock
```

比只使用 `asyncio.Event` 更合适。

同时实现了：

- Source 与 Token 权限分离；
- 重复取消幂等；
- 第一个取消原因保留；
- 明确的取消异常。

设计方向正确。

------

## 5. 序列化边界清晰

`RunContext.to_dict()` 只输出：

- `run_id`
- `session_id`
- `trace_id`
- `created_at`
- `deadline_at`
- `entry_agent_id`

没有尝试序列化：

- Clock；
- Token；
- Event；
- Lock；
- Model；
- 数据库连接；
- Chroma Client；
- Generator；
- 回调。

这为第 16 天 Checkpoint（检查点）奠定了正确基础。

------

## 6. 检查点选择基本合理

在以下边界调用：

```python
run_context.raise_if_inactive()
```

是合理的：

- 用户消息持久化前；
- 编排开始前；
- Tool 调用前后；
- Model 调用前；
- 流式 Chunk（数据块）之间；
- 委派 Agent 前后；
- assistant 消息持久化前。

它能做到：

> 在进入下一段工作前停止执行。

但不能做到：

> 强制中断已经进入的同步 Tool 或本地模型推理。

Codex已经在风险说明中如实记录，没有夸大实现效果。

------

# 三、必须确认的问题：CancellationSource 被谁持有

这是本次最关键的审查点。

结果文档写道：

```text
ChatService.stream_chat()
调用 RunContext.create(...)
```

同时又说明：

```text
RunContext 包含 CancellationToken
CancellationSource 拥有取消权限
```

但没有说明：

> `RunContext.create()` 创建 CancellationSource 后，Source 最终返回给了谁、保存在哪里。

假设代码类似：

```python
@classmethod
def create(...) -> RunContext:
    source = CancellationSource()

    return RunContext(
        cancellation_token=source.token,
        ...
    )
```

那么方法返回后：

```text
CancellationSource 引用丢失
```

系统虽然拥有一个可以观察取消的 Token，但没有任何对象能够调用：

```python
source.cancel(...)
```

这会导致 Cancellation Token 永远不会进入取消状态。

## 正确的所有权关系

应该是：

```text
ChatService / 未来的 RunService / Runtime
    持有 CancellationSource

RunContext
    只持有 CancellationToken

AgentRouter / Model / Tool / RAG
    只读取 CancellationToken
```

一个最小工厂可以返回：

```python
def create_run_context(...) -> tuple[RunContext, CancellationSource]:
    ...
```

或者定义明确的创建结果：

```python
@dataclass(frozen=True, slots=True)
class CreatedRun:
    context: RunContext
    cancellation_source: CancellationSource
```

不过当前阶段使用元组已经足够，不需要新增复杂抽象。

在正式取消传播尚未实现时，`ChatService` 可以暂时作为 Source 的所有者：

```python
run_context, cancellation_source = create_run_context(...)
```

即使今天还没有调用 `cancel()`，也必须明确 Source 没有被丢弃。未来引入 RunService 后，再将所有权迁移到 RunService 或 Runtime。

------

# 四、应该补充的问题：真实传递链没有被行为测试覆盖

文档中的第 15 项测试是：

> `ChatService` 与 `AgentRouter` 轻量可导入性检查。

可导入性检查只能证明：

- 没有语法错误；
- 基础 Import（导入）可完成。

它不能证明：

- ChatService 确实创建了 Context；
- ChatService 把同一个 Context 传给 AgentRouter；
- `entry_agent_id` 使用了正确的 `agent_id`；
- `session_id` 是 `legacy-default`；
- `AgentRouter` 没有重新创建 Context；
- Generator 迭代时 Context 仍然有效。

至少应新增一个不启动模型的轻量集成测试。

例如使用 Fake Router（伪路由器）：

```python
class FakeRouter:
    def __init__(self) -> None:
        self.received_context: RunContext | None = None

    def chat_stream(
        self,
        user_query: str,
        agent_id: str,
        run_context: RunContext | None = None,
    ):
        self.received_context = run_context
        yield "ok"
```

然后测试：

```python
router = FakeRouter()
service = ChatService(router=router)

chunks = list(
    service.stream_chat(
        agent_id="core_router",
        query="hello",
    )
)

self.assertEqual(chunks, ["ok"])
self.assertIsNotNone(router.received_context)
self.assertEqual(
    router.received_context.data.entry_agent_id,
    "core_router",
)
self.assertEqual(
    router.received_context.session_id,
    "legacy-default",
)
```

具体构造方式要根据真实 `ChatService` 构造函数调整，不能照搬假设。

------

# 五、需要重点复查的 `TYPE_CHECKING` 修改

Codex提到：

> 为解决轻量导入环境缺少 `langchain_chroma`、`requests`，将 AgentRouter 中仅用于类型标注的重依赖改为 `TYPE_CHECKING` 导入。

这种改法可以是正确的，但必须满足：

1. 对应类真的只用于类型标注；
2. 文件启用了延迟解析注解，例如：

```python
from __future__ import annotations
```

1. 运行时代码不存在：

```python
isinstance(value, VectorDBManager)
```

1. 运行时代码不存在：

```python
VectorDBManager(...)
```

1. 没有通过移除运行时 Import，掩盖真实缺失依赖。

这一修改略微超出 RunContext 本身，但如果只是纠正类型导入边界，属于合理的小型改进。

需要 Codex 在补充结果中列出：

- 哪些 Import 被移动；
- 它们只出现在哪些类型注解中；
- 为什么运行时不需要这些名称；
- 是否添加或已有 `from __future__ import annotations`。

------

# 六、非阻塞改进项

这些不需要阻塞第 2 天，但建议记录。

## 1. 标识和 Context 数据应不可变

建议确认以下对象是否使用：

```python
@dataclass(frozen=True, slots=True)
```

至少：

- `RunIdentifiers`
- `RunContextData`

应该不可变。

否则运行中可能出现：

```python
context.data.identifiers.run_id = "another-run"
```

导致日志和执行链失去一致性。

------

## 2. UTC 时间必须是时区感知对象

应确认：

```python
datetime.now(timezone.utc)
```

而不是：

```python
datetime.utcnow()
```

后者返回无时区信息的 naive datetime（无时区时间）。

测试应至少断言：

```python
self.assertIsNotNone(context.data.created_at.tzinfo)
```

------

## 3. Timeout 应检查有限数值

当前只检查零或负数，理论上还可能收到：

```python
float("nan")
float("inf")
```

可以使用：

```python
math.isfinite(timeout_seconds)
```

这不是当前阻塞项，但属于良好的输入校验。

------

## 4. 取消可能产生半写入 Memory

当前执行链可能发生：

```text
用户消息已经写入
→ 用户取消
→ assistant 消息未写入
```

这会形成只有用户问题、没有回答的记录。

今天不需要解决，因为它涉及 AgentState、StopReason 和事务边界，但要作为后续问题保留。

------

## 5. 可选 Context 可能形成绕过路径

现在：

```python
run_context: RunContext | None = None
```

用于兼容是合理的。

但需要明确 `None` 时如何处理。

如果 `None` 时完全跳过所有检查，那么其他调用方可能永久绕开 Runtime Context。

建议：

- 已确认所有正式调用方迁移后，将参数改为必填；
- 兼容期只允许测试或明确的遗留入口传 `None`；
- 不要在 `AgentRouter` 深层静默创建新的 Context。

------

# 七、第 2 天补充 Codex 提示词

这次不需要重新设计，也不需要大规模修改。只确认并修复关键缺口。

基于已经完成的阶段二第 2 天 RunContext 改造，请进行一次小范围补充审查和修正。

本次不得重新设计架构，不得实施第 3 天 AgentState，不得修改 API、Memory Schema 或流式协议。

## 一、首先检查 CancellationSource 所有权

请检查当前 RunContext 创建代码，明确回答：

1. `CancellationSource` 在哪里创建？
2. `RunContext.create()` 或对应工厂返回什么？
3. 创建完成后，哪个对象持有 `CancellationSource`？
4. 是否存在 Source 创建后引用丢失，导致无人能够调用 `cancel()` 的问题？
5. RunContext 是否只持有 Token，而不是持有取消权限？

正确的目标关系是：

- ChatService、未来的 RunService 或 Runtime 持有 CancellationSource；
- RunContext 只持有 CancellationToken；
- AgentRouter 及其下游只能观察 Token。

如果当前 Source 创建后被丢弃，请进行最小修复。

允许的最小方案：

```text
create_run_context(...) -> tuple[RunContext, CancellationSource]
```

或者等价的明确返回结构。

当前阶段可以由 ChatService 暂时持有 CancellationSource，即使尚未接入客户端取消，也不能让 Source 在创建后丢失。

不要新增运行注册表、cancel API 或完整取消传播。

## 二、补充 ChatService 到 AgentRouter 的行为测试

新增一个不启动真实模型、Tool、RAG、Chroma、数据库或 UI 的轻量测试，验证：

1. `ChatService.stream_chat()` 创建了非空 RunContext；
2. Context 被传给 `AgentRouter.chat_stream()` 或 Fake Router；
3. 传递的是同一个 Context；
4. `entry_agent_id` 等于请求中的 `agent_id`；
5. `session_id` 等于当前兼容值 `legacy-default`；
6. `run_id` 和 `trace_id` 非空；
7. 迭代流式 Generator 时仍然使用该 Context；
8. 不修改现有输出内容。

根据真实 ChatService 构造方式实现 Fake、Mock 或 Patch，不要为了测试重构生产代码。

## 三、检查 TYPE_CHECKING 修改

请列出本次为了轻量导入而移动到 `TYPE_CHECKING` 下的所有 Import，并确认：

- 这些类型只用于类型标注；
- 运行时代码没有实例化它们；
- 没有用于 `isinstance()`；
- 文件已使用延迟注解，或不会在运行时解析缺失名称；
- 该修改不会掩盖项目正常运行所需的真实依赖。

如果发现某个 Import 实际运行时需要，请恢复正确的运行时导入，不要为了让导入测试通过而隐藏依赖问题。

## 四、补充基础不变量检查

检查并在必要时最小修正：

- `RunIdentifiers` 和 `RunContextData` 是否不可变；
- `created_at` 和 `deadline_at` 是否为 timezone-aware UTC datetime；
- `entry_agent_id`、`run_id`、`trace_id` 是否拒绝空字符串；
- timeout 是否拒绝 NaN 和正负无穷；
- 序列化结果是否只包含 JSON 友好数据。

这些属于小型修正，不要扩展成完整校验框架。

## 五、执行测试

至少执行：

```text
python -m unittest tests.test_runtime_context -v
python -m compileall core tests
```

如新增单独测试文件，也执行对应测试。

不要启动真实模型、Chroma、FastAPI 服务或 PyQt6 UI，不要访问外部网络。

## 六、更新结果文档

更新：

```text
docs/learning/stage2/day02_run_context_result.md
```

在文档中补充：

- CancellationSource 的创建位置和所有者；
- Source 是否曾存在引用丢失问题；
- 修复前后创建方法签名；
- ChatService 到 AgentRouter 行为测试；
- TYPE_CHECKING 修改的具体 Import 和安全依据；
- 不变量检查结果；
- 新增或修改的测试；
- 实际测试命令与结果。

## 七、禁止事项

不得：

- 实现 AgentState、RunStatus、StepStatus 或 StopReason；
- 实现状态机；
- 实现完整 Runtime；
- 新增 cancel API；
- 实现客户端断开传播；
- 新增运行注册表；
- 修改 `/api/chat` 请求体；
- 修改 Memory Schema；
- 修改现有文本流或 `[[ORCH]]`；
- 引入大型依赖；
- 执行 Git push；
- 创建 Pull Request；
- 上传公司代码或数据。

完成后只输出：

CancellationSource 创建位置：

CancellationSource 所有者：

是否发现 Source 引用丢失：

修复内容：

新增测试：

TYPE_CHECKING 修改检查结果：

测试命令：

测试是否通过：

更新文档路径：

需要人工确认的问题：

------

# 八、面试表达修正

Codex给出的面试表达总体正确，但可以稍微强化边界：

> 我先在 LocalAgent 的应用服务入口建立了最小 RunContext。每次聊天执行都会生成独立的 `run_id` 和 `trace_id`，并通过兼容的 `session_id` 关联现有单会话 Memory。Deadline 同时保留 UTC 截止时间和进程内单调时钟，Cancellation 则采用 Source 与 Token 分离的协作式模型。Context 由 ChatService 创建并显式传递到遗留 AgentRouter，在模型、工具、RAG 和持久化等关键边界检查是否仍可继续执行。当前只建立了运行上下文基础，还没有实现客户端断开传播、强制中断阻塞调用、状态机或持久化恢复。

这段表述不会夸大当前能力。

------

# 九、当天验收状态

## 已通过

-  RunContext 基础数据结构
-  `run_id`、`session_id`、`trace_id` 分离
-  Deadline UTC 与 monotonic 分离
-  Cancellation Source/Token 抽象
-  可序列化数据与运行依赖分离
-  Context 在统一入口创建
-  显式传入 AgentRouter
-  API 和 Memory Schema 保持兼容
-  FakeClock 单元测试
-  基础测试和编译检查通过
-  结果文档已生成

## 待补齐

-  确认 CancellationSource 没有在创建后丢失
-  增加 ChatService 到 AgentRouter 的行为测试
-  确认 `TYPE_CHECKING` 修改不会掩盖运行时依赖

## 阶段二进度

**第 2/25 天：条件通过，等待一次小范围补充修正。**

完成补充后即可正式进入第 3 天：**AgentState（智能体状态）、RunStatus、StepStatus、StopReason、状态版本和状态不变量。**

# 面试题

## 1. 为什么 CancellationSource 不能只在工厂内部创建？

因为如果工厂只返回 Token 所在的 Context，Source 的引用就会丢失，系统只能观察取消，却没有任何对象能够发出取消。

## 2. 为什么 RunContext 只持有 Token，不直接持有 Source？

为了限制权限。Agent、Tool 和 RAG 只应该响应取消，不应该拥有终止整个 Run 的权限。Source 应由 Runtime 或应用服务持有。

## 3. 为什么使用 `threading.Event` 而不是 `asyncio.Event`？

当前 LocalAgent 的核心执行链包含同步生成器、PyQt 线程和阻塞调用，`threading.Event` 能覆盖这些同步路径；`asyncio.Event` 只适合异步事件循环内部。

## 4. Deadline 为什么需要 Monotonic Clock？

系统时间可能被校时或人工修改。单调时钟只向前推进，更适合计算当前进程中的剩余执行时间。

## 5. 为什么当前使用固定 `legacy-default` session，而不是随机生成？

随机生成会导致每个 Run 都被误认为一个新 Session，破坏多轮会话语义。当前项目还没有真实 Session，因此使用明确兼容值更诚实。