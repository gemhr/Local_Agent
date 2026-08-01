# 阶段二第 24 天：Fault Injection / Chaos Matrix

## 1. 本日目标与准确范围

第 24 天完成了测试专用、离线、确定性的 Fault Injection 体系。最终轮聚焦三项关闭语义硬化、Fault Point 真实性清单、Rule 优先级、跨组件组合矩阵、集中不变量、禁用态对等、安全扫描和覆盖率验收。

明确不在范围内：生产配置/API/Header、随机概率 Chaos、真实外部依赖破坏、跨进程控制器、自动补偿、自动 Recovery/Replay/Resume、Exactly-once，以及任何第 25 天能力。

## 2. 修改前故障测试方式

修改前主要依赖单组件 Fake、直接抛异常和零散断言。它们能验证局部错误映射，却不能统一回答：故障是否发生在真实物理窗口、多个 Rule 谁获胜、部分持久化是否保持、诊断故障是否改变业务事实、Shutdown 编排完成是否等于资源完全关闭。第 24 天把这些问题收敛为不可变契约、显式 Controller、物理 Seam 和派生报告。

## 3. Fault Injection Foundation

基础层由不可变 `FaultPlan`、`FaultRule`、`FaultMatchContext`、固定 `FaultAction`/`FaultTrigger`、请求级 `FaultInjectionController` 和只记录安全字段的 Recorder 组成。Controller 不持有 Runtime 状态，也不拥有 Retry、Fallback、Compensation、Recovery、Event、Journal、Trace 或 Worker。

所有错误向外只使用固定安全码；Rule 匹配只使用摘要、枚举和安全 token；测试动作保持有界且可取消。

## 4. Fault Plan / Rule / Priority

规则顺序固定为 `priority ASC -> rule_id ASC`。Controller 在一个物理 Seam 只执行第一条可执行 Rule；高优先级不匹配、禁用或达到 `max_hits` 后，下一条规则才能接管。并发计数在锁内完成，不同 Run 摘要、Step、Invocation、Attempt 和 Component 的条件互不消费。

最终矩阵覆盖：同点冲突、异点同一业务流、条件不匹配、`max_hits` 接管、并发 sibling、不同 Run、关闭竞态、危险/普通 Rule 混合和禁用 Rule。

## 5. Controller / Scope / Recorder

Controller 生命周期显式注入，缺省和 disabled 都是零行为路径。Scope 只管理测试等待器和 Controller 清理。Recorder 只接收已命中的安全 Decision；最终 Support/Coverage/Invariant 报告不携带 Rule ID，避免测试配置进入长期结果。

Controller 被关闭后不再命中；关闭 Controller 不关闭 Runtime 资源，也不影响其他请求的 Controller。

## 6. Fault Point Support Report

`FaultPointSupportReport` 为冻结值对象，逐一分类全部 42 个 Fault Point：

| 状态 | 数量 | 语义 |
|---|---:|---|
| SUPPORTED | 32 | 存在真实 Runtime 调用窗口，并有边界状态断言 |
| CONTRACT_ONLY | 10 | 契约已声明，但当前没有真实物理调用窗口 |
| NOT_APPLICABLE | 0 | 当前没有归入该类的点 |

Contract-only 清单：Model provider 成功后、Model usage commit 前/后、Tool side-effect commit 后、Retrieval rewrite 后、Retrieval search 后、Retrieval result commit 前、Journal 通用 read 前、Executor submit 前/后。尤其 `TOOL_AFTER_SIDE_EFFECT_COMMIT` 因 Adapter 尚无权威 commit callback，不能宣称 Supported。

每项只保存 point、状态、物理 owner/location 安全 token、危险窗口标记、支持动作、逻辑测试 ID 和固定说明码，不保存绝对路径、对象 repr 或业务正文。

## 7. Model Fault Injection

支持 invocation 前和 provider call 前两个真实窗口。注入的 transient/rate-limit/timeout/permanent failure 仍由原 Model 分类、RetryExecutor、Fallback 和预算规则处理。Controller 不增加尝试；provider 调用数和预算提交数与既有 Policy 一致。

组合验证中，首次 pre-provider transient 被原两次尝试策略吸收，provider 实际调用一次；同时 Observability record 失败只让诊断健康度降级，不改变输出和 Journal。

## 8. Retrieval Fault Injection

支持 rewrite 前和 search 前窗口。timeout/failure 按原 Retrieval fail-closed 状态返回；注入点不自动重写、不自动搜索、不触发跨 Runtime fallback。rewrite 后、search 后和 result commit 前因没有真实调用点，如实列为 Contract-only。

Retrieval + Trace 组合验证规则隔离、Trace 逻辑关闭和 `active_span_count == 0`，Snapshot/Journal authority 不发生转移。

## 9. Tool Pre-call

支持 Tool invocation 前、attempt 前和 provider 前窗口。pre-provider 注入时本次 attempt 的 provider 调用为零；后续尝试只来自原 Tool Retry Policy。Fault Controller 不拥有 Tool 执行，也不会因 Event 发布失败额外重跑 Tool。

## 10. Tool Side-effect Boundary

支持 side-effect commit 前、provider return 后和权威 side-effect resolution 后窗口。Side-effect Evidence 单调：`COMMITTED` 不回退，`UNKNOWN` 不伪装成 `NOT_STARTED`，取消不改写已提交事实。非幂等 Tool 不被自动 replay，也没有自动 compensation。

commit 后的精确 Adapter 回调仍未实现，因此对应点保持 Contract-only，未虚构 exactly-once 能力。

## 11. Tool Completion Publication

支持 completion event 前故障。非幂等 Tool 已 `COMMITTED` 后若 completion 发布失败，本地冻结 Evidence 仍保留；`TOOL_STARTED=1`、`TOOL_COMPLETED=0` 可被准确观察。随后 Client Disconnect 不输出敏感错误正文，不触发 Retry 或 Compensation，Registry/Worker 按原生命周期收口。

## 12. Event / Journal / Channel

支持普通事件 append 前、append 后、enqueue 前，Terminal append 前，以及 Channel receive/drain handoff 前。Journal-first 保持不变：append 后故障会形成“Journal 有、Channel 无”的部分发布证据；sequence 已消费且永不复用。Terminal 只走专用 append Seam，最多一个 Journal Terminal 和一个 Channel Terminal，不创建替代 Terminal owner。

## 13. Snapshot / Recovery

支持 Snapshot save 前/后、Snapshot read 前、Recovery tail read 前/后。after-save 注入保留已持久化快照和 partial evidence，不自动重存。Recovery 只信任 Snapshot + Journal；损坏 Tail 始终 fail closed，诊断失败不能把损坏证据改写为空，也不会触发 Replay/Resume/Adapter 调用。

## 14. Observability / Trace

Observability record/flush 与 Trace start/end/flush 均有真实窗口。诊断链路遵循 best effort：失败可以形成固定 health code，但不能删除 Journal、修改 AgentState 或泄漏进用户正文。Trace end 故障仍逻辑关闭 Span，恢复 ContextVar，最终 active span 为零。

## 15. Graceful Shutdown

Shutdown 支持 run cancel、worker drain、Journal close、Model close 和 generic component close。多组件组合一次配置六项故障：Observability flush、Trace flush、Snapshot close、Journal close、Model close、remaining close；每项均获得一次有界尝试，前项失败不会跳过后项，Admission/Lifecycle 最终到 CLOSED。

Worker gate 保持权威：未证明 idle 时 Model 必须 deferred，不能因别名或后续 generic close 被关闭。

## 16. Shutdown Report Semantics

`ShutdownReport` 现在区分：

- `orchestration_completed`：Admission 与 Lifecycle 均已到 CLOSED；
- `completed`：为旧调用方保留，兼容含义等同 `orchestration_completed`；
- `has_failures`：由结构化 component status/error 派生；
- `has_deferred_resources`：由 `DEFERRED` 事实派生；
- `fully_closed`：编排完成、无 remaining run、无 active/detached/unknown worker、worker 为 IDLE、无 deferred、无必需关闭失败。

Flush failure 会令 `has_failures=true`，但不单独否定物理必需资源已关闭；Journal/Model close failure、worker drain failure、remaining run 和 Model deferred 均令 `fully_closed=false`。判断不解析错误字符串。

## 17. Shutdown Cancellation Re-entry

采用“第二次调用从单调状态继续收口”的语义。Operation A 在 run/worker/flush/close 阶段被取消时传播 `CancelledError`；Operation B 不恢复 Admission/Lifecycle，不返回 A 的伪成功，也不重复已成功关闭的物理对象。尚未执行物理 close 的组件仍获得一次有界机会，只有完整终态 Report 才缓存。

若取消发生在同步物理 close 已提交到线程之后，该 identity 记为 `UNKNOWN` 并禁止二次调用，因为等待者取消不等于线程终止；这样避免 double-close，同时诚实暴露不确定状态。

## 18. Component Identity Reservation

Target planning 预先按 object identity 保留专用 owner：Journal identity 只能由 Journal-specific close 处理；Model identity 只能由 Model-specific close 处理。即使专用 close 注入失败、超时、deferred 或真实异常，同一对象也不能通过 remaining/generic alias 绕过。

回归验证 Journal 和 Model 两类 alias：specific counter 命中一次、generic counter 为零、物理 close 为零。

## 19. Deterministic Chaos Matrix

最终矩阵包含 12 组：Model+Observability、Retrieval+Trace、Tool pre-provider+Event partial publication、Tool committed+completion failure+disconnect、Terminal Journal+Trace end、Snapshot after-save+Shutdown Journal close、Recovery corruption+diagnostics、Worker drain+Model alias、Model close fault+alias、多组件 Shutdown、Shutdown cancellation re-entry、Parallel fault isolation。

矩阵不使用随机数。每组同时验证规则路由和既有物理 Seam 的状态断言；其中 Model+Observability 与六组件 Shutdown 以同一 Controller 做完整组合执行，其余组合复用相同真实 Seam 的专项边界测试与组合路由断言。

## 20. Runtime Invariants

`FaultRuntimeInvariantReport` 是冻结值对象，只保存派生计数与固定 violation code。正常基线要求一个 runtime selection、RunContext、CancellationSource、EventChannel、sequence owner、Registry registration、root span 和 Terminal owner。它不持有任何 Runtime/Controller 对象。

组合故障未创建第二 Runtime、State、Sequence、Worker 或 Shutdown owner。

## 21. Side-effect Invariants

Business rerun、cross-runtime fallback、automatic compensation、automatic recovery action 的正常计数均为零。Model/Tool/Retrieval provider 调用严格服从原 Policy。Tool side-effect phase 单调，取消和诊断错误都不能重写 `COMMITTED/UNKNOWN` 事实。

## 22. Journal / Sequence Invariants

Journal append 事实不可变；部分发布不会删除已写记录；sequence reuse count 为零；Terminal Journal/Channel count 均不大于一；不存在 synthetic replacement terminal。Snapshot after-save 与 Recovery corruption 都不会改变 Journal authority。

## 23. Worker / Shutdown Invariants

Waiter cancellation 不等于 worker termination；detached worker 必须继续可见；未执行 drain 不可报告 IDLE；Model 仅在 gate 证明后关闭。专用 component identity 不允许 generic alias；Shutdown 报告明确区分“编排结束”和“完全关闭”。

清理检查覆盖 registry handle、channel owner、watcher、request producer、reservation、permit 和 active span。Detached worker 可暂时非零，但必须进入报告并令 `fully_closed=false`。

## 24. Disabled Parity

完整 coordinated run 使用固定 run/trace identity 比较 no-controller 与 disabled-controller：业务结果、输出、预算提交、Event sequence/order/payload、Journal safe payload、Observability 记录、Worker snapshot、Registry 清理及 Shutdown 结构化语义一致。允许忽略 duration/timestamp 等时间字段；disabled controller 的 match/hit 计数均为零，Recorder 无记录。

Model、Retrieval、Tool、Event、Snapshot/Recovery、Observability、Trace 与 Shutdown 另有各自强对等专项测试，防止完整链路比较掩盖局部差异。

## 25. Fault Coverage

`FaultCoverageReport` 最终结果：total 42、supported 32、contract-only 10、not-applicable 0、supported-and-tested 32、untested-supported 0、dangerous-supported 8。并明确记录 disabled parity、cancellation、concurrency、partial persistence、security 均已覆盖。

覆盖率不是 Enum 数量；只有存在真实调用命中且断言边界状态的 Supported point 才可计入 Tested。Contract-only 不参与 supported coverage，未测试 Supported 为零才允许第 24 天通过。

## 26. Security

最终扫描覆盖 Fault Context/Decision/Recorder、Support/Coverage/Invariant Report、Event/Journal error、Snapshot/Recovery evidence、Observability/Trace health、ShutdownReport、repr/safe JSON、Runtime wire、structured log、metric label 和 span attribute。

报告仅含固定 point/component/status/code 和摘要；不含提示词、模型/工具/RAG/Memory 正文、原始异常、用户路径、原始幂等键/资源键/快照载荷、明文 Run/Thread ID。Fault Rule ID 只进入短生命周期 Fault Recorder，不进入最终三类报告。

## 27. Bad Case

### Bad Case 1：编排完成被误当作完全关闭

- 类型：真实发现
- 触发条件：Admission 与 Lifecycle 已 CLOSED，但 Journal/Model close 失败、worker 未收口或仍有 remaining run。
- 故障表现：旧 `completed` 仍为真，调用方可能误判所有物理资源已关闭。
- 根因分析：单一布尔值混合了控制面终态与资源面事实。
- 修复方案：新增 `orchestration_completed`、`fully_closed`、`has_failures`、`has_deferred_resources`，旧 `completed` 仅作编排完成兼容别名。
- 回归测试：全成功、Journal/Model failure、Model deferred、worker failure、flush failure、remaining run 和多失败组合。
- 对应知识点：派生状态、控制面与数据面分离、兼容契约。
- 面试表达：关闭流程到达终态不等于每个资源都成功关闭，报告必须表达两个维度。
- 当前状态：已修复并回归通过。

### Bad Case 2：Shutdown 任务取消后第二次调用无法继续

- 类型：真实发现
- 触发条件：第一次 shutdown 在 close fault seam 中被取消，随后再次调用 shutdown。
- 故障表现：可能等待被取消的旧任务、重复关闭，或缓存不完整成功结果。
- 根因分析：关闭进度只有整份终态缓存，没有按物理 identity 保存单调进度。
- 修复方案：缓存已完成物理 close 结果；第二次调用从当前状态继续，仅完整终态 Report 可缓存。
- 回归测试：Snapshot 已关闭后在 Journal seam 取消，再次 shutdown；Snapshot 不重复，后续组件各一次。
- 对应知识点：可重入终止协议、单调状态、取消安全。
- 面试表达：取消的是等待者，不一定是底层动作；重入必须同时避免漏关和 double-close。
- 当前状态：已修复并回归通过。

### Bad Case 3：Model 专用关闭故障被 remaining alias 绕过

- 类型：真实发现
- 触发条件：同一 Model 对象同时以 model component 和 remaining component 注册，专用 seam 注入失败。
- 故障表现：generic close 仍可能调用该对象，破坏 worker gate 和 fault 证据。
- 根因分析：旧 target planning 只按遍历中的已见 identity 去重，没有预留专用 owner。
- 修复方案：规划前收集 Journal/Model identity，禁止任何 generic alias 进入 targets。
- 回归测试：Model 与 Journal alias 均断言 specific=1、generic=0、physical close=0。
- 对应知识点：物理身份、别名分析、资源所有权。
- 面试表达：名称去重不够，资源生命周期必须以物理 identity 和专属 owner 为准。
- 当前状态：已修复并回归通过。

### Bad Case 4：Enum 存在被当作 Fault Coverage

- 类型：假设构造
- 触发条件：统计 `FaultPoint` 枚举数量并直接宣称全覆盖。
- 故障表现：没有 Runtime seam 的点也被报告为 Supported/Tested。
- 根因分析：把契约声明、实现窗口和测试证据混为一谈。
- 修复方案：逐点生成 Support Report，并把无真实调用点者标为 Contract-only。
- 回归测试：42 点唯一且完整分类，Supported 必须有 actions/test IDs，Contract-only 不得携带测试证据。
- 对应知识点：需求覆盖率、代码覆盖率与故障窗口覆盖率。
- 面试表达：能构造 Rule 不代表 Runtime 会执行它，覆盖必须从物理命中反推。
- 当前状态：防护已实现。

### Bad Case 5：同一物理 Seam 执行多个 Rule

- 类型：假设构造
- 触发条件：多个 Rule 同时匹配同一 Fault Point。
- 故障表现：一次调用同时 delay、raise 或 mutate，结果取决于遍历细节。
- 根因分析：缺少稳定优先级和 first executable wins 契约。
- 修复方案：按 priority、rule_id 排序，命中第一条后立即停止。
- 回归测试：同优先级 tie、不同优先级、disabled/mismatch/max-hits 接管。
- 对应知识点：确定性调度、冲突消解。
- 面试表达：Chaos 要可复现，单一 seam 必须最多一个动作。
- 当前状态：契约已验证。

### Bad Case 6：Disabled Controller 改变 Runtime identity

- 类型：假设构造
- 触发条件：disabled controller 仍创建额外 context/channel/span 或消耗计数。
- 故障表现：关闭 Fault 后业务结果相同但生命周期、事件身份或指标不同。
- 根因分析：把 disabled 当作“匹配后不执行”，而不是零行为快路径。
- 修复方案：`enabled` guard 直接返回 no-fault decision，不创建第二 owner。
- 回归测试：完整 coordinated parity 与逐组件 parity，match/hit 均为零。
- 对应知识点：Null Object、强语义对等。
- 面试表达：Feature disabled 的正确性不仅是输出相同，还包括身份、顺序和资源行为相同。
- 当前状态：防护已实现。

### Bad Case 7：组合 Fault 触发跨 Runtime fallback

- 类型：假设构造
- 触发条件：Model/Tool 故障与诊断故障同时发生。
- 故障表现：Coordinated Runtime 偷偷回退到 Legacy 并重复业务调用。
- 根因分析：错误恢复边界不清，把 transport recovery 当 runtime selection。
- 修复方案：Runtime selection 只发生一次，Fault Controller 不拥有 fallback。
- 回归测试：cross-runtime fallback count 为零，provider 调用服从原 policy。
- 对应知识点：单一入口、fallback ownership。
- 面试表达：失败处理不能重新做架构选择，否则会出现双执行和双 Terminal。
- 当前状态：不变量已验证。

### Bad Case 8：诊断 Fault 覆盖 Recovery corruption

- 类型：假设构造
- 触发条件：Journal tail 损坏，同时 Observability record 与 Trace flush 失败。
- 故障表现：Recovery 把损坏误报为空 Tail 或可恢复状态。
- 根因分析：诊断状态错误地参与权威证据判断。
- 修复方案：Recovery 只信 Snapshot+Journal，诊断为 best effort 旁路。
- 回归测试：corrupted tail 仍 fail closed，automatic recovery action 为零。
- 对应知识点：权威数据与诊断数据隔离。
- 面试表达：监控系统失效不能改变业务真相，只能降低可观测性。
- 当前状态：边界已验证。

### Bad Case 9：Event 部分持久化导致 Tool 重跑

- 类型：假设构造
- 触发条件：Tool 完成事件已 append，但 enqueue 前失败。
- 故障表现：上层把发布失败当 Tool 未执行并再次调用 provider。
- 根因分析：没有区分业务执行失败与事件部分发布失败。
- 修复方案：冻结 partial publication evidence，禁止 Event 层拥有业务 Retry。
- 回归测试：Journal 有、Channel 无、sequence 不复用、provider 不额外执行。
- 对应知识点：Outbox 思想、至少一次发布与业务幂等边界。
- 面试表达：持久化成功后的发送失败必须保留证据，不能用重跑业务来修复传输。
- 当前状态：边界已验证。

### Bad Case 10：Worker Drain 失败后仍关闭 Model

- 类型：假设构造
- 触发条件：worker drain 注入失败或存在 detached/unknown worker。
- 故障表现：仍在运行的 worker 使用已关闭 Model。
- 根因分析：把“尝试过 drain”误当作“已证明 idle”。
- 修复方案：Model close 必须经过 worker truth gate，未证明则 DEFERRED。
- 回归测试：worker failed、active/detached/unknown 组合均令 Model deferred、fully_closed=false。
- 对应知识点：资源依赖顺序、quiescence proof。
- 面试表达：安全关闭依赖事实证明，不依赖流程是否走到某行代码。
- 当前状态：防护已实现。

### Bad Case 11：前一关闭失败导致后续组件未尝试

- 类型：假设构造
- 触发条件：Observability/Trace/Snapshot/Journal/Model/remaining 同时配置失败。
- 故障表现：首个异常中断循环，报告缺失后续事实。
- 根因分析：资源关闭使用 fail-fast，而非独立有界收集。
- 修复方案：每个组件独立捕获固定错误并继续，最终聚合 Report。
- 回归测试：六组件组合每条 Rule 命中一次，编排结束但 fully_closed=false。
- 对应知识点：best-effort cleanup、错误聚合。
- 面试表达：Shutdown 要尽最大努力释放全部资源，不能因第一个失败放弃剩余资源。
- 当前状态：防护已实现。

### Bad Case 12：Coverage Report 泄漏 Rule ID 或路径

- 类型：假设构造
- 触发条件：直接把 Recorder 或测试文件绝对路径塞入最终覆盖报告。
- 故障表现：报告包含内部规则、用户目录或测试环境信息。
- 根因分析：把执行诊断对象当作长期审计值对象。
- 修复方案：最终报告只保存固定安全 token、逻辑 test ID 和计数。
- 回归测试：repr/safe JSON 字段扫描与敏感标记扫描。
- 对应知识点：数据最小化、安全遥测。
- 面试表达：测试报告也属于输出面，必须按最小披露设计。
- 当前状态：防护已实现。

### Bad Case 13：高优先级 Rule 达到 max_hits 后阻塞后继

- 类型：假设构造
- 触发条件：高优先级规则已用尽，但仍匹配相同 Seam。
- 故障表现：后续低优先级 Rule 永远无法执行。
- 根因分析：把“匹配”误当成“可执行”。
- 修复方案：达到 max_hits 后继续扫描下一条可执行 Rule。
- 回归测试：第一次高优先级命中，第二次低优先级接管，计数精确。
- 对应知识点：优先队列、资格判定。
- 面试表达：排序决定检查顺序，配额决定执行资格，两者必须分开。
- 当前状态：契约已验证。

### Bad Case 14：并发 sibling 互相消费 Rule

- 类型：假设构造
- 触发条件：两个并行 Step/Run 共用一个 Controller，Rule 条件不够精确。
- 故障表现：A 的 Fault 消耗 B 的 hit，结果依赖线程时序。
- 根因分析：缺少 Run/Step/Invocation 摘要条件或计数非原子。
- 修复方案：条件化匹配，计数在锁内更新，独立 Controller 为默认请求边界。
- 回归测试：并发不同 Run digest 各命中自己的 Rule 一次。
- 对应知识点：并发隔离、线性化计数。
- 面试表达：确定性故障测试必须把匹配域和计数原子性同时设计。
- 当前状态：防护已实现。

### Bad Case 15：Trace end Fault 留下 active span

- 类型：假设构造
- 触发条件：Span 结束前注入异常。
- 故障表现：active span 永久非零，ContextVar 污染后续操作。
- 根因分析：物理导出失败与逻辑 Span 生命周期绑在一起。
- 修复方案：finally 中逻辑关闭并恢复 ContextVar，导出失败只降级 health。
- 回归测试：Trace end/flush 失败后 active span=0、父子层级恢复。
- 对应知识点：作用域清理、诊断 best effort。
- 面试表达：Span 必须逻辑闭合，即使 exporter 或 recorder 自身失败。
- 当前状态：防护已实现。

### Bad Case 16：Snapshot after-save Fault 自动重存

- 类型：假设构造
- 触发条件：存储已提交后在 after-save seam 抛错。
- 故障表现：调用方重试生成第二份 Snapshot，版本或副作用重复。
- 根因分析：未表达 partial persistence，把错误当作未开始。
- 修复方案：返回 persisted/partially_persisted evidence 且 retry_allowed=false。
- 回归测试：Store 中仅一份已提交 Snapshot，caller 看见固定 partial failure。
- 对应知识点：提交点、幂等与不确定结果。
- 面试表达：提交后的故障不能用“失败”一个状态覆盖，必须带持久化证据。
- 当前状态：防护已实现。

### Bad Case 17：Terminal Journal 失败后创建替代 Terminal

- 类型：假设构造
- 触发条件：RUN_COMPLETED 在 terminal-specific append seam 失败。
- 故障表现：系统再发布一个“失败 Terminal”，形成第二 Terminal owner。
- 根因分析：把终态状态与终态事件发布混为一谈。
- 修复方案：AgentState 保留权威终态，返回固定 publication error，不合成替代事件。
- 回归测试：Journal/Channel Terminal 均不超过一，Registry 清理且业务不重跑。
- 对应知识点：单写者、终态唯一性。
- 面试表达：终态事件发布失败不能通过再造一个终态来掩盖，否则审计顺序失真。
- 当前状态：防护已实现。

### Bad Case 18：取消同步 close 后立即重复物理 close

- 类型：假设构造
- 触发条件：同步 close 已提交到 worker thread，等待协程被取消后再次 shutdown。
- 故障表现：同一对象被并发或连续 close 两次。
- 根因分析：把 asyncio waiter 取消误认为底层线程已停止。
- 修复方案：该 identity 标记 UNKNOWN 并禁止重入物理调用，报告固定不确定错误。
- 回归测试：取消后的 re-entry 不重复已提交 physical close。
- 对应知识点：线程取消语义、at-most-once cleanup。
- 面试表达：Python 不能强停执行中的线程，因此安全选择是保留 UNKNOWN 而非假定终止。
- 当前状态：防护已实现。

### Bad Case 19：Flush failure 被错误当作所有资源未关闭

- 类型：假设构造
- 触发条件：Trace/Observability flush 失败，但所有必需 close 成功。
- 故障表现：`fully_closed` 被错误置 false，混淆诊断数据丢失与资源泄漏。
- 根因分析：所有 error code 被无差别纳入 full-closure 判定。
- 修复方案：`has_failures=true` 保留 flush 事实，`fully_closed` 只检查必需关闭操作和资源计数。
- 回归测试：flush failure 的两个顶层派生属性分别断言。
- 对应知识点：多维状态、错误分类。
- 面试表达：失败存在与资源未关闭不是同一个问题，报告应同时表达。
- 当前状态：语义已固定。

### Bad Case 20：Fault 错误正文进入用户输出

- 类型：假设构造
- 触发条件：provider、event、snapshot 或诊断异常含原始业务/路径信息。
- 故障表现：用户正文、repr、日志或指标标签泄漏内部内容。
- 根因分析：直接传播原始异常或对象 repr。
- 修复方案：统一固定 safe code，Evidence/Report 最小字段，用户 transport 使用安全错误块。
- 回归测试：敏感标记矩阵扫描 Fault/Journal/Recovery/Trace/Shutdown 输出面。
- 对应知识点：错误净化、可观测性安全。
- 面试表达：故障注入扩大错误路径覆盖，也必须扩大敏感信息回归覆盖。
- 当前状态：防护已实现。

## 28. 测试结果

最终专项 11 文件：50 passed。第 24 天 Fault 相关测试：261 passed。全仓：1013 passed，另有 42 subtests passed。`compileall`、`uv lock --check` 与 `git diff --check` 均通过。

新增 8 个专项文件并扩展 Shutdown Report truthfulness，共新增/扩展 41 个测试 case：Chaos Matrix、Fault Runtime Invariants、Disabled Full Parity、Point Support Report、Coverage Report、Shutdown Cancellation Re-entry、Component Identity Reservation、Final Security。

## 29. 未完成事项

明确未实现：生产 Fault 入口、随机 Chaos、真实外部 Model/Tool/数据库/网络破坏、自动 Retry 业务语义、自动 Compensation、自动 Recovery/Replay/Resume、Exactly-once、跨进程 Fault Controller/RunRegistry、强制终止 Python/C Extension thread、10 个 Contract-only 物理 Seam，以及第 25 天内容。

## 30. 面试表达

我把故障注入设计成测试依赖而不是第二套 Runtime：Rule 只决定在真实物理 Seam 注入哪一个动作，原 Runtime 继续拥有 Retry、Fallback、Side-effect、Event、Recovery 和 Shutdown。覆盖率按“真实调用命中 + 边界事实断言”计算，不按 Enum 计算。最终通过派生不变量证明单一 owner、side-effect 单调、Journal authority、Terminal 唯一和 worker/model 安全。

关闭部分最关键的取舍是：`orchestration_completed` 与 `fully_closed` 分离；取消后从单调进度继续；同步 close 已提交但 waiter 取消时报告 UNKNOWN，宁可诚实暴露不确定性，也不 double-close 或伪造成功。

## 31. 需要带回 ChatGPT 审查的信息

请重点审查：

1. Flush failure 不单独否定 `fully_closed`、但令 `has_failures=true` 的语义是否符合上层运维约定。
2. 同步 physical close 已提交后取消，选择 UNKNOWN + 禁止重试是否满足各资源的 close 幂等契约。
3. 10 个 Contract-only 点是否应在未来 Adapter/Executor 权威回调出现后再升级为 Supported。
4. 组合矩阵中复用专项真实 Seam 断言的覆盖证据是否需要进一步整合为更长的端到端场景。
5. `completed` 继续表示 orchestration completion 的兼容期限与弃用计划。
