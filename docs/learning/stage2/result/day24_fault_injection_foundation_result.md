# 第 24 天第一轮：Fault Injection Foundation

## 1. 本轮目标

本轮建立独立、确定性、测试专用的 Fault Injection Foundation：

- 固定 `FaultPoint`、`FaultAction`、`FaultTrigger`、`FaultScope` 和
  `InjectedFaultCode`；
- 建立不可变 `FaultMatchContext`、`FaultRule`、`FaultPlan`、
  `FaultDecision` 与 `InjectedFailureResult`；
- 由 `FaultInjectionController` 独占并发安全的 match/hit 计数；
- 由 `FaultInjectionRecorder` 保存有界、内容无关的命中记录；
- 由 `FaultInjectionScope` 显式拥有 Controller、Recorder、Blocker 和
  Sleeper 生命周期；
- 验证默认关闭、安全输出、并发命中、取消和清理。

本轮没有接入 Model、Tool、Retrieval、Event、Journal、Snapshot、Recovery、
Observability、Trace、Shutdown 或 `/api/chat`。没有实现生产开关、HTTP/API
入口、概率 Chaos、Retry/Fallback/Compensation 或 Recovery/Replay。

## 2. 修改前故障测试接缝

审计范围覆盖第 13～23 天结果文档、相关 Runtime 模块和测试。修改前接缝如下：

| 模块 | 修改前如何构造失败 | 可复用接缝 | 本轮处理 |
| --- | --- | --- | --- |
| Model | `RecordingAdapter(outcomes)` 按调用顺序返回值或抛异常；并发 Adapter 使用 `threading.Event` | 脚本化 outcome、固定 Clock、Event/Barrier | 保留领域 Adapter，不迁移到生产类 |
| Tool | `ScriptedAdapter(script)`、`CheckpointFailureAdapter`、`BlockingAdapter`、`AsyncCancellationAdapter` | 脚本结果、typed `ToolAdapterInvocationError`、Event gate | 保留 Tool 幂等性和副作用语义 |
| Retrieval | `FakeRetrievalAdapter` 的 `rewrite_failure`、`embedding_failure`、`vector_failure`、`rerank_failure`、`failed_load_ids` | 阶段 Fake、typed `RetrievalAdapterError`、blocking executor gate | 保留阶段语义，不改 Retrieval |
| EventJournal | `FailingJournal.append()`、`RecordingJournal`、`StateAssertingJournal`；SQLite 测试直接构造损坏记录 | `InMemoryRunEventJournal`、临时 SQLite、typed `JournalErrorCode` | 不接入、不写 Journal |
| RuntimeEventChannel | 小容量队列、close/abort、单 consumer、失败 Journal；测试内 `asyncio.Event` 控制发布/消费窗口 | 有界 queue、Event gate、journal-first 断言 | 不增加 Hook，不发布 Event |
| Snapshot | `InMemorySnapshotStore`、`:memory:` SQLite、`tmp_path` SQLite、`_FailingStore.save()`、直接损坏数据库 | 临时 Store、typed `SnapshotErrorCode`、`tmp_path` 隔离 | 不改 Schema/Store |
| Observability / Trace | `FailingLogger`、`FailingRecorder`、`BrokenMetrics`，以及 InMemory logger/metrics/span recorder | InMemory recorder、失败实现、health snapshot | Recorder 独立，不注册 metrics |
| Graceful Shutdown | `_Resource(fail=True)` 在 flush/close 抛原始异常；`_Executor(idle=...)` 控制 drain | 显式资源对象、固定调用序列、typed lifecycle report | 不接入 shutdown coordinator |
| Barrier / Event / Clock | 测试中散落 `asyncio.Event`、`threading.Event`、`threading.Barrier`、`asyncio.Barrier`；`test_runtime_context.py` 与 `test_model_circuit_breaker.py` 各有局部 `FakeClock` | Barrier/Event 的确定性编排；`Clock` Protocol | 新并发测试复用模式，不建立全局工具 |
| Settings | 只加载 Runtime mode、KB、预算等生产配置 | 严格 env 解析方式 | 不增加 Fault 配置或环境变量 |
| ApplicationRuntimeServices | 测试 fixture 注入 `FakeRouter`、`FakeDispatcher`、InMemory Journal/Snapshot；服务字段固定 | 显式依赖注入和每测新实例 | 不增加 Controller 字段 |

审计结论：

1. 存在散落的 failure flag、脚本化异常、`Event` 和 `Barrier`，但没有统一
   `fail_on_call`/`raise_on_*` 公共协议；
2. 这些 Fake 多数承载明确领域语义，尤其 Tool side effect、Retrieval stage、
   Journal append 和 Shutdown order，不应为“统一”而删除；
3. 可复用的是显式依赖注入、typed safe error、InMemory/临时 Store、
   Event/Barrier 确定性编排和 Clock Protocol；
4. 修改前已有多个领域安全 Error Code/Category，但没有通用 Fault Injection
   Error Code；
5. 有测试 Clock，但它们是测试文件局部实现，不是共享进程全局；
6. 修改前不存在模块级 Fault/Chaos 全局开关；
7. 并行隔离主要依靠每测试新建 Adapter、Store、Controller、Event、Barrier，
   SQLite 使用 `:memory:` 或 `tmp_path`；本轮沿用对象级隔离。

## 3. Fault Injection 范围

Foundation 只接受显式 Python 对象构造和显式依赖注入。生产默认没有
Controller；`FaultInjectionController()`/`disabled()` 是 Null Object，固定返回
同一个不可变 `NO_FAULT_DECISION`。

没有从 Settings、环境变量、HTTP Header、Request、Prompt、Message、Tool
Argument 或任何用户 JSON 创建 Rule/Plan 的路径。模块没有
`ContextVar`、`current_fault_scope` 或进程全局 Controller。

## 4. FaultPoint

`FaultPoint` 是 `str, Enum`，共 41 个稳定英文标识，覆盖未来 Model、Tool、
Retrieval、Event/Journal、Snapshot/Recovery、Executor/Channel、
Observability/Trace 和 Shutdown 接入窗口。本轮只定义分类，不调用任何现有
组件。

高风险集合当前只包含 `TOOL_AFTER_SIDE_EFFECT_COMMIT`；在未来真实接入前，
仍需逐点审查其他 post-commit 窗口。

## 5. FaultAction

固定支持五类 Action：

- `RAISE_TYPED_ERROR`：只抛 `InjectedFaultError(InjectedFaultCode)`，异常没有
  payload、路径、原始异常或正文；
- `DELAY`：要求有限、非负、非 bool 的 `delay_seconds`，通过可注入
  `FaultSleeper` 执行；
- `BLOCK_UNTIL_RELEASED`：通过 Plan 外部的 `FaultBlocker` 执行，等待严格有界；
- `RETURN_TYPED_FAILURE`：返回不可变 `InjectedFailureResult`；
- `CORRUPT_TEST_FIXTURE`：Plan 只保存安全 mutation descriptor；必须显式注入
  test fixture mutator，否则以固定配置码 fail closed。

Action 执行器没有修改 Store、AgentState、Budget、Retry 或 Tool side effect。

## 6. FaultTrigger

固定支持 `ALWAYS`、`FIRST_MATCH`、`ON_NTH_MATCH`、`AFTER_N_MATCHES`、
`UNTIL_MAX_HITS`。每条 Rule 都必须有正整数 `max_hits`，所以第一版没有无限
触发；所有 count 拒绝 bool、零和负数。

`ON_NTH_MATCH` 与 `AFTER_N_MATCHES` 必须显式提供正整数 `match_number`。
没有概率、随机、jitter 或隐式时间条件。

## 7. FaultScope

固定 Scope 为 `GLOBAL_TEST_SCOPE`、`RUN_SCOPE`、`STEP_SCOPE`、
`INVOCATION_SCOPE`、`ATTEMPT_SCOPE`、`COMPONENT_SCOPE`。

`GLOBAL_TEST_SCOPE` 只描述当前显式 `FaultInjectionScope`，没有进程全局实例。
Rule 可匹配的字段严格限于 run/invocation digest、step、attempt、component、
event/operation/side-effect/shutdown 等内容无关标识。

## 8. FaultMatchContext

`FaultMatchContext` 使用 `@dataclass(frozen=True, slots=True)`，只包含固定字段，
没有 arbitrary metadata mapping，也没有 Runtime/AgentState 引用。

- digest 字段只接受 64 位 lowercase SHA-256；
-其他 identity 只接受最长 128 字符的安全 Token；
-安全 Token 额外拒绝 `secret` 标记，校验错误不回显原值；
-attempt 只接受正整数且拒绝 bool；
-不存在 Prompt、Message、Tool input/output、RAG query/chunk、Memory、路径、
  API key、Provider URL 或 exception 字段。

## 9. FaultRule

`FaultRule` 不可变且使用 slots。Rule 保存固定合同、匹配条件、有界触发参数和
Action 描述，不保存 counter、Lock、Event、Task、Blocker 或 mutator。

显式测试 Plan 中 Rule 默认 `enabled=true`。`dangerous_window=false` 是默认值；
`TOOL_AFTER_SIDE_EFFECT_COMMIT` 在没有 `dangerous_window=true` 时拒绝构造。
Foundation 只验证危险窗口，不执行真实 Tool commit 后动作。

## 10. FaultPlan

`FaultPlan` 不可变，字段为 `plan_id`、`schema_version=1`、`rules` 和 UTC
`created_at`。创建时将 Rule 按 `rule_id` 规范排序并拒绝重复 ID。

`to_safe_json()` 使用 sorted-key canonical JSON；`digest` 是该安全 JSON 的
lowercase SHA-256。Plan 不包含 Blocker、Lock、Event、Task、mutator 或 Runtime
对象。普通 `repr` 只显示 plan ID、schema、rule count 和 digest，不展开匹配
条件。

没有 `from_request`/`from_json`/环境变量装配路径。

## 11. FaultInjectionController

Controller 是唯一 match/hit 计数 Owner。每个 Controller 有私有
`threading.Lock` 与每 Rule 独立计数；Rule/Plan 保持不可变。

`evaluate(context)` 在同一临界区完成 closed/enabled 检查、match ordinal、
trigger 判断、`max_hits` 校验和 hit ordinal 提交。多个规则同时可执行时，按
Plan 的规范化 `rule_id` 顺序选择第一条，只执行一个 Action。

`snapshot()` 返回不可变安全计数快照；`close()` 幂等，关闭后固定
NO_FAULT。Controller 不持有 Runtime state，不发布 Event，不调用 Retry/Fallback，
也不缓存 Model/Tool/Retrieval 响应。

## 12. FaultDecision

`FaultDecision` 不可变，命中时只包含 rule/point/action、match/hit ordinal、
固定 fault code 和 UTC timestamp。

`NO_FAULT_DECISION` 是单一不可变对象，除 `matched=false` 外所有字段都是
`None`，没有虚构 Rule。Decision 没有异常正文、payload、输入、路径或业务响应。

## 13. FaultInjectionRecorder

Recorder 只保存 `plan_id`、`rule_id`、`fault_point`、`component`、`action`、
match/hit ordinal、固定 fault code 和 timestamp。手工调用 Recorder 也会重新
验证安全 Token、枚举、ordinal 和 UTC timestamp。

Recorder 使用私有锁和有界 deque。容量策略显式为：

- 默认 `DROP_OLDEST`，并累计 `dropped_count`；
- 可选 `REJECT_NEW`，并累计 `rejected_count`。

关闭后不接受新记录。Recorder 没有 Journal、RuntimeEvent、AgentState、wire、
metrics 或日志依赖；普通 `repr` 只显示容量、数量、策略和计数。

## 14. FaultInjectionScope

`FaultInjectionScope` 是 async context manager，也是本次测试故障生命周期
Owner。每个 Scope 新建独立 Controller、Recorder 和每 blocking Rule 一个
Blocker；Sleeper、fixture mutator 与 Clock 显式注入。

`aclose()` 由 async lock 保证幂等，依次关闭 Controller、释放所有 Blocker、
取消或释放 Sleeper 拥有的等待、关闭 Recorder。Scope 不依赖 GC，也不写模块级
current scope。

## 15. 并发命中

并发测试使用 `asyncio.Barrier` 和 `threading.Barrier` 确定性启动竞争：

- 两个 Task 同时命中 `max_hits=1`，恰好一个 matched、一个 NO_FAULT；
- match count 最终为 2，hit count 为 1；
- 8 个线程的 match/hit ordinal 都严格形成 `1..8`；
-不同 Rule 和不同 Controller 的计数互不共享；
-close/evaluate 竞态保持原子；竞态前已提交的命中最多一个，close 后永不命中。

没有用随机 sleep 制造竞态。

## 16. Cancellation / Blocker

`FaultBlocker` 暴露 `entered`、`release` 和有限 `timeout`，Plan 不保存这些运行时
对象。`wait()` 使用 `asyncio.wait_for`；超时转换为固定
`INJECTED_TIMEOUT`，Task cancellation 原样传播。

Scope 退出会 release 所有 Blocker。`ControllableFaultSleeper` 让 Delay 测试
无需墙钟等待，并原生响应 Task cancellation；Scope close 会释放其等待。默认
`AsyncioFaultSleeper` 显式追踪自己创建的 sleep Task，close 会 cancel 并 drain，
不会把长 delay 留到 Scope 之外。

## 17. Security

安全测试覆盖以下敏感标记：

`SECRET_PROMPT_TEXT`、`MODEL_OUTPUT_SECRET`、`TOOL_ARGUMENT_SECRET`、
`TOOL_OUTPUT_SECRET`、`RAG_CHUNK_SECRET`、`MEMORY_SECRET`、
`C:\Users\private-user`、`provider-secret-error`。

这些值不能进入安全 Token 字段，错误消息不回显原值。测试确认它们不出现在
Plan JSON/digest、Rule/Plan/Context/Decision/Recorder/Controller/Scope repr、
typed exception、Recorder snapshot 或日志中。

Foundation 源码没有 Settings、FastAPI、HTTP、随机数、环境变量或 ContextVar
依赖。

## 18. Backward Compatibility

本轮只新增 Foundation 模块、导出和测试：

-没有修改现有组件构造参数或 `ApplicationRuntimeServices`；
-没有修改 RuntimeEvent、Journal、Snapshot 或 Tool Evidence schema；
-没有修改默认 `/api/chat`、Budget、Retry、Event sequence 或 Tool side effect；
-没有生产 Controller 注入点，现有路径仍等价于“无 Controller”；
-Disabled Controller 自身只返回 NO_FAULT，不创建 Event、Journal、Metric 或
  用户输出；
-修改前领域 Fake 全部保留。

## 19. Bad Case

格式沿用“类型 / 风险 / 处理 / 验证”；真实审计表示修改前边界，假设构造用于
本轮 fail-closed 回归。

| # | Bad Case | 类型 | 风险 | 处理 | 验证 |
| --- | --- | --- | --- | --- | --- |
| 1 | Fault Injection 默认开启 | 真实架构审计 | 生产请求被扰动 | 无生产装配；空 Controller disabled | default controller 测试 |
| 2 | Prompt 控制 Rule | 真实边界审计 | 用户获得故障控制权 | 无 HTTP/Prompt/JSON 构造入口 | 源码依赖与字段审计 |
| 3 | 随机触发不可重复 | 假设构造 | 测试不可复现 | 固定 Trigger，无 random | enum/源码测试 |
| 4 | `max_hits=1` 并发触发两次 | 假设构造 | 故障重复 | Controller 锁内原子提交 | Task/线程 Barrier 测试 |
| 5 | Rule 保存 Lock/Event | 假设构造 | Plan 不可序列化且跨测试泄漏 | frozen slots Rule；运行对象归 Scope | slots/Plan 测试 |
| 6 | Controller 修改 AgentState | 真实依赖审计 | 状态语义被污染 | Context 无 Runtime 引用；Controller 无 State 依赖 | 字段/源码测试 |
| 7 | Recorder 写 Runtime Journal | 真实依赖审计 | 审计流污染业务 Journal | 独立 bounded deque | 源码与 Recorder 测试 |
| 8 | Fault Context 保存 Tool Argument | 假设构造 | 敏感正文泄漏 | 固定 allowlist 字段 | dataclass fields 测试 |
| 9 | Scope 退出未释放 Blocker | 假设构造 | 测试挂起 | `aclose()` release 所有 Blocker | Scope cleanup 测试 |
| 10 | 测试共享全局 Controller | 真实架构审计 | 计数跨测试污染 | 无 module singleton；每 Scope 新建 | controller isolation 测试 |
| 11 | Delay 不响应 Cancellation | 假设构造 | 取消被拖延 | async Sleeper；取消原样传播 | fake sleeper cancellation |
| 12 | Corrupt Action 修改真实 Store | 假设构造 | 真实数据损坏 | 只调用显式 test mutator；缺失即 fail closed | mutator 测试 |
| 13 | 高风险 Tool Point 无 dangerous flag | 假设构造 | post-commit 重复副作用 | 构造期拒绝 | contract 测试 |
| 14 | Disabled Controller 改变正常结果 | 真实兼容审计 | 默认行为回归 | Null Object 返回 NO_FAULT；未接业务 | disabled 测试与全仓回归 |

## 20. 测试结果

- Foundation 测试：`53 passed`；
-任务指定 Runtime 回归集合：`188 passed, 12 subtests passed`；
-全仓 pytest：`751 passed, 42 subtests passed`；
-`uv run python -m compileall -q core tools tests`：通过；
-`uv lock --check`：通过；
-`git diff --check`：通过（仅提示现有 Windows LF/CRLF 转换 warning）。

任务给出的文件名均存在，因此没有替换测试文件。

## 21. 未完成事项

-未在 Model/Tool/Retrieval/Event/Journal/Snapshot/Recovery/Executor/Channel/
  Observability/Trace/Shutdown 插入 FaultPoint；
-未把 Controller 加入 Application Runtime assembly；
-未定义生产启用策略、权限模型或运维 API；
-未实现组合 Action、概率触发、跨进程协调或持久化计数；
-未实现自动 Retry/Fallback/Compensation/Recovery/Replay；
-未执行任何真实 Store corruption 或 Tool side effect；
-最终 `day24_fault_injection_result.md` 留到第四轮。

## 22. 第二轮接入点

第二轮应先审查并选择低风险、pre-call 的显式接入点，建议顺序：

1. 由 Application Runtime/Run Scope 显式传递可选 Controller，不读
   Settings/Request/Prompt；
2. 从 `MODEL_BEFORE_INVOCATION`、`TOOL_BEFORE_INVOCATION`、
   `RETRIEVAL_BEFORE_*` 等 pre-side-effect 点开始；
3. 每个接入点先证明 disabled/no-controller 路径的 Event、Budget、Retry 与结果
   完全一致；
4. post-commit、Journal、Snapshot 和 Shutdown 窗口继续保持未接入，直到逐点
   完成 owner、重复副作用和恢复语义审查。

第二轮不得因为已有 Foundation 就默认所有 41 个点都安全可用。

## 23. 需要带回 ChatGPT 审查的信息

-请审查 Plan canonical order 采用 `rule_id` 排序、第一条可执行规则获胜是否满足
  后续编排需要；
-请审查 `ALWAYS` 与 `UNTIL_MAX_HITS` 在“所有 Rule 必须有 max_hits”的第一版中
  执行语义相同是否应保留为面向未来的不同意图；
-请审查当前 dangerous 集合只含 `TOOL_AFTER_SIDE_EFFECT_COMMIT`，第二轮开始前
  应为哪些 Event/Journal/Snapshot/usage commit 后窗口增加 dangerous 要求；
-请确认第二轮只接 pre-call 点，并继续禁止生产 Settings/API；
-请确认 Recorder 默认溢出策略 `DROP_OLDEST` 是否符合后续测试诊断偏好；
-请确认 Scope close 对 Blocker 采用 release、对默认 Sleeper 的内部等待 Task
  采用 cancel-and-drain、对 Controllable Sleeper 采用 release，是否符合下一轮
  生命周期所有权。
