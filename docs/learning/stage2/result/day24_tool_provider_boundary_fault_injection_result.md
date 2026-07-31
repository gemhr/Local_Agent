# 第 24 天第二轮 B2a：Tool Provider Boundary

## 1. 本轮目标

本轮只把 `TOOL_BEFORE_SIDE_EFFECT_COMMIT` 与
`TOOL_AFTER_PROVIDER_RETURN` 接入真实 Tool Attempt。实现复用
`AttemptSideEffectTracker`、BudgetLedger、RetryExecutor、Tool Event 与 Trace 的既有
owner，不新增第二套状态机，不自动补偿、恢复或 replay，也不调用真实外部副作用。

## 2. 修改前 Provider / Side-effect 顺序

修改前真实顺序如下；`before_side_effect()` 只推进 `NOT_STARTED -> STARTED`，并不代表
外部操作已经成功：

```text
TOOL_STARTED(provider_started=false, side_effect_state=NOT_STARTED)
→ TOOL_BEFORE_PROVIDER_CALL
→ provider_started=true
→ Adapter.invoke_once() entered
→ context.before_side_effect()
→ cancellation/deadline check
→ AttemptSideEffectTracker: NOT_STARTED -> STARTED
→ Adapter 执行内存/外部操作
→ Adapter return ToolAdapterResponse
→ Budget reservation commit(ESTIMATED)
→ Runtime 按 Adapter 权威响应 resolve/observe Side-effect State
→ cancellation/deadline check
→ output/result normalization
→ TOOL_COMPLETED
```

本轮后的实际顺序是：

```text
TOOL_STARTED
→ TOOL_BEFORE_PROVIDER_CALL
→ provider_started=true
→ Adapter entered
→ before_side_effect callback entered
→ TOOL_BEFORE_SIDE_EFFECT_COMMIT
→ Tracker: NOT_STARTED -> STARTED
→ external operation
→ Adapter return
→ Budget commit
→ Adapter 权威 Side-effect State 写入 Tracker
→ TOOL_AFTER_PROVIDER_RETURN
→ Result/Error normalization
→ Retry policy / TOOL_COMPLETED
```

Adapter 负责执行操作并返回权威结果；Runtime 的
`AttemptSideEffectTracker.resolve_authoritative()` 负责把 `STARTED` 收口到
`COMMITTED`、`NOT_STARTED`、`COMPENSATED` 或 `UNKNOWN`。Adapter 返回后、最终结果
构造前仍可能在 fault seam、取消/Deadline 复验、输出校验或完成事件发布处失败。

## 3. Dangerous Window Contract

以下四点现在都属于固定 `DANGEROUS_FAULT_POINTS`，Rule 未显式设置
`dangerous_window=true` 时在构造阶段失败：

```text
TOOL_BEFORE_SIDE_EFFECT_COMMIT
TOOL_AFTER_PROVIDER_RETURN
TOOL_AFTER_SIDE_EFFECT_COMMIT
TOOL_BEFORE_COMPLETION_EVENT
```

本轮只调用前两个；后两个仅保留合同。

## 4. Phase-aware Fake Tool

`tests/tool_fault_test_support.py::PhaseAwareToolAdapter` 是测试专用内存 Fake。它不读写
文件、不访问网络或数据库、不调用真实 Tool，只维护以下计数：

```text
provider_entered_count
before_side_effect_called_count
side_effect_marker_committed_count
external_effect_applied_count
provider_returned_count
compensation_called_count
detached_worker_count
```

Fake 可模拟 read-only、natural idempotent、带 Key 的 idempotent、non-idempotent，支持
稳定 Key replay，并可用 `PhaseBarrier` 在 provider entered、before callback、marker、
external effect 与 provider return 五个阶段阻塞。`response_state` 可权威返回
`NOT_STARTED`、`COMMITTED` 或 `UNKNOWN`。Fault Controller 不持有也不修改这些计数。

这里的 `side_effect_marker_committed_count` 表示 Adapter 已成功越过正式
`before_side_effect()` checkpoint；真实 Tracker 此刻为 `STARTED`，不是
`ToolSideEffectState.COMMITTED`。

## 5. TOOL_BEFORE_SIDE_EFFECT_COMMIT

位置在 `ToolExecutionContext.before_side_effect()` 中：先做取消/Deadline 检查，再执行
同步、可取消的 fault callback，再次检查取消/Deadline，最后才调用
`AttemptSideEffectTracker.before_side_effect()`。

命中 one-attempt non-idempotent 用例的事实为：Provider 进入 1 次、callback 进入 1 次、
Tracker marker 0 次、外部 effect 0 次、Provider return 0 次、compensation 0 次；错误保留
`provider_started=true` 与 `side_effect_state=NOT_STARTED`。同步 Worker、Permit、Lease 与
activity 均沿原 finally 收口，无 detached worker。

## 6. TOOL_AFTER_PROVIDER_RETURN

位置在 `adapter.invoke_once()` 正常返回、Budget commit、Adapter 权威 Side-effect State
写入真实 Tracker 之后，输出与 `ToolExecutionResult/Error` 最终归一化之前。Adapter 抛出
异常时不会命中此点。

这样读取到的状态不是 Fault Controller 的推断：read-only 为 `NOT_STARTED`，成功写操作
为 `COMMITTED`，权威不明为 `UNKNOWN`。命中时 Provider entered/returned 均为 1；取消、
Delay 与 Block 继续使用 attempt 的 cancellation/deadline，而不是创建 detached task。

## 7. Post-provider Error Mapping

映射仍使用既有 `ToolErrorCategory`：

| 已知事实 | Category | RetryDisposition |
| --- | --- | --- |
| read-only、`NOT_STARTED`、注入 transient/timeout/permanent | 对应既有 `TRANSIENT`/`TIMEOUT`/`INTERNAL` | 由 `retry_disposition_for()` 决定 |
| Adapter return 后为 `COMMITTED` | `POST_COMMIT_RESPONSE_FAILURE` | Key/replay 矩阵决定，默认 `UNSAFE` |
| Adapter return 后为 `UNKNOWN`，或非权威 `STARTED` | `POST_COMMIT_RESPONSE_FAILURE` | `OUTCOME_UNKNOWN` |

安全 post-provider 错误码固定为 `TOOL_POST_PROVIDER_FAILURE`。错误对象不包含 Rule ID、
arguments、output、resource key、idempotency key 明文或原始异常。Controller 只产生 typed
fault，不决定 Category、Retry、Compensation 或 Side-effect State。

## 8. Retry / Idempotency

- read-only transient：既有 RetryPolicy 可建立下一 Attempt；测试中 `max_hits=1` 后第二次
  Provider 正常完成。
- natural idempotent：`NOT_STARTED` 可按现有 policy 安全重试；若已 `COMMITTED`，现有矩阵
  保守返回 `UNSAFE`。
- idempotent with stable key：只有 `COMMITTED + POST_COMMIT_RESPONSE_FAILURE + stable key +
  supports replay` 才为 `SAFE_WITH_IDEMPOTENCY_KEY`。Fake 验证第二次 Provider 是 replay，
  外部 effect 总数仍为 1。
- non-idempotent committed：`UNSAFE`，Provider 只调用 1 次。
- unknown side-effect/idempotency：fail closed；`UNKNOWN` 为 `OUTCOME_UNKNOWN`。

Fault hit count 与 Retry Budget 独立；Controller 从不调用 RetryExecutor。

## 9. Budget / Permit / Lease

Provider 开始后，`adapter.invoke_once()` 外层 finally 按原合同提交该 Attempt 的
`tool_calls=1` Estimated usage，即使 fault 在 before-side-effect callback 中抛出也不把它
release 成“未调用”。Provider 前失败仍 release reservation。

read-only transient 重试测试提交 2 个 tool call 与 1 个 retry；before-commit 失败提交 1 个
tool call。所有测试结束 `active_reservation_count=0`、active worker 为 0，Permit/Lease 释放。
同一 reservation 只 commit 一次。

## 10. Side-effect State

集中不变量如下：

```text
before seam 命中、external_effect_applied=0
→ NOT_STARTED

external_effect_applied=1 且 Adapter 权威确认
→ COMMITTED，不允许回退 NOT_STARTED

Adapter 权威 UNKNOWN 或非权威 STARTED 后 Runtime 失败
→ UNKNOWN + OUTCOME_UNKNOWN

compensation_called_count=0
→ 不允许 COMPENSATED
```

Tracker 的 `STARTED` 是保守危险窗口状态；`COMMITTED` 在本 Fake 与正式 Adapter 响应中表示
Adapter 权威确认已经提交。

## 11. Event / Trace

Controller 不发布 Event 或 Span。两个 seam 均在 `TOOL_STARTED` 后，原 Attempt owner 对每个
命中发布恰好一个 `TOOL_COMPLETED`。终止 payload 保留真实 `provider_started`、Side-effect
State、固定安全错误码、RetryDisposition、worker/detached 状态与 recovery evidence。

non-idempotent after-return 测试得到：

```text
TOOL_STARTED
→ TOOL_COMPLETED(
    provider_started=true,
    side_effect_state=COMMITTED,
    outcome_classification=POST_COMMIT_RESPONSE_FAILURE,
    retry_disposition=UNSAFE,
    execution_detached=false
  )
```

Rule ID、参数、输出、Resource Key、Key 明文与原始异常均未进入事件；Attempt/Invocation Span
结束后 `active_span_count=0`。

## 12. Recovery Evidence

本轮没有调用 Recovery Validator。事件证据直接来自既有 Tool payload owner：

- before commit：`provider_started=true`、`NOT_STARTED`、`NOT_ATTEMPTED` compensation、无
  detached worker；
- after return committed：`provider_started=true`、`COMMITTED`、
  `POST_COMMIT_RESPONSE_FAILURE`、`UNSAFE`、无 compensation。

Fault Rule 不进入 evidence。测试证明 committed 事实不会在失败映射中丢失。

## 13. Cancellation

两个 seam 的 `BLOCK_UNTIL_RELEASED` 都覆盖 Run Cancellation。before commit 取消后事件保留
`provider_started=true + NOT_STARTED`；after return 取消后事件保留
`provider_started=true + COMMITTED`。两个 seam 也分别覆盖可取消 Delay 与 attempt deadline：
before seam 超时仍为 `NOT_STARTED`，after-return 超时仍为 `COMMITTED`。取消不会触发
compensation，不继续构造 Client output；Budget 已按 Provider started 合同 commit，
reservation、Permit、Lease、activity 与 worker 最终归零。Delay/Block 的轮询同时检查 Run、
Attempt token 和 effective deadline；scope close 会释放 blocker。

## 14. Disabled Parity

No Controller 与 disabled Controller 的 Provider 调用、权威 Side-effect State、输出、事件
类型、Budget、worker 与最终结果一致。包含危险 Rule 但 controller `enabled=false` 时，
`match_count=0`、`hit_count=0`，不会进入 callback 或改变状态。

## 15. Isolation

Controller 仍由请求显式传入，无模块全局安装。Run A 的 after-return fault 不影响 Run B；
Invocation A 的 `COMMITTED` Tracker 由 attempt 局部创建，不污染 Invocation B。Fake 的 stable
key replay 集合仅属于该 Fake 实例；匹配上下文只含 invocation digest，不含 Key。Lease 仍由
独立 Attempt 持有，Controller close 不修改已写入 Tracker 的事实。

## 16. Security

生产 Settings、API 与 Header 未增加 fault 开关。Fault match context 只使用 run/invocation 的
SHA-256 digest、attempt number、component 与固定 phase token。Tool arguments、output、Resource
Key、Idempotency Key 明文和原始异常不进入 Fault plan recording、Tool Event、Trace 或安全错误。

## 17. Runtime 真实接入

真实接入仅位于：

- `ToolExecutionContext.before_side_effect()`：调用
  `TOOL_BEFORE_SIDE_EFFECT_COMMIT`，在 Tracker 转换前；
- `ToolAttemptExecutor._execute_impl()`：Adapter 正常返回并写入权威 Tracker 后调用
  `TOOL_AFTER_PROVIDER_RETURN`；
- `_tool_injected_error()`：根据 `provider_started`、真实 Side-effect State 与 post-provider
  状态局部映射；
- `DANGEROUS_FAULT_POINTS`：补入 before-side-effect seam。

未修改 `retry_disposition_for()`、RetryExecutor、Compensation owner 或 Tool Error Taxonomy。

## 18. Legacy Boundary

只读 `LegacyStringToolAdapter` 不调用 `before_side_effect()`，因此不会虚构 before-commit
checkpoint；它正常返回后可以命中 after-return seam，真实状态仍为 `NOT_STARTED`。复杂流程
Adapter 继续通过既有 `context.before_side_effect` 进入正式 Tracker。未改 Legacy JSON wrapper、
真实 Tool 或第 25 天边界。

## 19. Bad Case

以下“真实发现”均指本轮对仓库代码/测试基础设施的实际审计发现，不代表真实生产事故。

### Bad Case 1：Before-side-effect 危险点未强制显式 opt-in

- 类型：真实发现
- 触发条件：构造 `TOOL_BEFORE_SIDE_EFFECT_COMMIT` Rule 时省略 `dangerous_window=true`。
- 故障表现：修改前合同会接受该 Rule，危险窗口缺少显式确认。
- 根因分析：固定危险点集合已包含其余 post-provider/post-commit 点，但漏了 before-side-effect 点。
- 修复方案：把该点加入 `DANGEROUS_FAULT_POINTS`。
- 回归测试：四个 Tool 危险点参数化验证缺少标记即构造失败。
- 对应知识点：fail-fast 配置校验、危险能力显式授权。
- 面试表达：我在合同层发现一个真实漏项并在运行前拒绝危险配置，而不是等到副作用窗口才兜底。
- 当前状态：已修复。

### Bad Case 2：Provider 已开始却 Release 全部调用 Budget

- 类型：假设构造
- 触发条件：before-side-effect fault 从 Adapter 抛出后沿用 Provider 前失败的 reservation release。
- 故障表现：已经发生的 Tool 调用不计费，Budget 与真实 Provider calls 不一致。
- 根因分析：错误地用 fault point 名称代替 `provider_started` 事实判断计费。
- 修复方案：保留 `_invoke_adapter()` 外层 finally commit；仅 Provider 前路径 release。
- 回归测试：before-commit fault 断言 committed tool calls 为 1、active reservation 为 0。
- 对应知识点：reservation 两阶段结算、事实驱动计费。
- 面试表达：只要 Provider 已进入，失败也必须按既有合同结算，不能把调用回滚成未发生。
- 当前状态：已由测试阻断。

### Bad Case 3：Provider 返回后仍标记 provider_started=false

- 类型：真实发现
- 触发条件：直接复用原 `_tool_injected_error()` 处理新 seam。
- 故障表现：原 helper 固定输出 `provider_started=false + NOT_STARTED`，不能表达 Provider boundary。
- 根因分析：该 helper 原本只服务 B1 pre-call seam，状态参数被硬编码。
- 修复方案：显式传入 `provider_started` 与真实 Tracker state，并按 post-provider 局部映射。
- 回归测试：两个新 seam 的 error/event 均断言 `provider_started=true`。
- 对应知识点：phase-aware error mapping、避免复用错误抽象。
- 面试表达：我发现 B1 mapper 的前置假设不适用于 B2a，因此扩展输入事实而不是伪造返回状态。
- 当前状态：已修复；不是生产事故。

### Bad Case 4：Non-idempotent committed 被映射成 transient

- 类型：假设构造
- 触发条件：after-return 注入 transient，Adapter 已确认 non-idempotent effect committed。
- 故障表现：RetryExecutor 可能再次执行不可逆操作。
- 根因分析：只按注入 code 分类，没有联合 Side-effect State 与 idempotency。
- 修复方案：映射为既有 `POST_COMMIT_RESPONSE_FAILURE`，RetryDisposition 为 `UNSAFE`。
- 回归测试：Provider/return/effect 都为 1，Category 固定且无第二 Attempt。
- 对应知识点：副作用感知重试、fail closed。
- 面试表达：transient 描述故障持续时间，不代表业务操作可安全重试。
- 当前状态：已由实现与测试阻断。

### Bad Case 5：Post-provider Fault 自动再次调用 Adapter

- 类型：假设构造
- 触发条件：Fault Controller 自己实现 retry 或绕过 RetryExecutor。
- 故障表现：Provider 调用次数与 Retry Budget、Event、Lease 身份脱节。
- 根因分析：Controller 越权成为重试 owner。
- 修复方案：Controller 只抛 typed fault；唯一 Retry owner 仍是 RetryExecutor。
- 回归测试：non-idempotent committed provider count 为 1；read-only/key replay 只按原 policy 重试。
- 对应知识点：单一 owner、policy/mechanism 分离。
- 面试表达：fault injection 只制造失败，不拥有恢复动作。
- 当前状态：已由架构和测试阻断。

### Bad Case 6：Post-provider Fault 自动 Compensation

- 类型：假设构造
- 触发条件：看到 `COMMITTED` 后由 Controller 或 error mapper调用 compensation。
- 故障表现：未授权业务动作发生，原始提交事实被掩盖。
- 根因分析：把故障注入误当成事务协调器。
- 修复方案：本轮不调用 compensation owner，保留 `NOT_ATTEMPTED`。
- 回归测试：所有 PhaseAware 场景 `compensation_called_count=0`，事件为 `NOT_ATTEMPTED`。
- 对应知识点：补偿不是回滚、职责边界。
- 面试表达：补偿是显式业务协议，不能由测试 Controller 猜测执行。
- 当前状态：已由测试阻断。

### Bad Case 7：Fault Controller 修改 Side-effect Tracker

- 类型：假设构造
- 触发条件：Controller 命中时直接写 `COMMITTED/UNKNOWN`。
- 故障表现：状态反映 Rule 而非 Adapter 真实执行事实。
- 根因分析：为测试建立第二套状态机。
- 修复方案：Controller 只返回/抛出 fault；Tracker 仅由 Context callback 与 Adapter 权威响应更新。
- 回归测试：before seam marker/effect 为 0 时 state 仍为 `NOT_STARTED`。
- 对应知识点：single source of truth、状态机所有权。
- 面试表达：故障注入不能反向改写被观测系统的业务事实。
- 当前状态：已由代码结构阻断。

### Bad Case 8：已执行副作用被重置为 NOT_STARTED

- 类型：真实发现
- 触发条件：旧 `CountingToolAdapter` 调用 `before_side_effect()` 并增加 effect 计数，却返回默认权威 `NOT_STARTED`。
- 故障表现：测试 Fake 会让 Runtime 把真实 Tracker 从 `STARTED` 重置为 `NOT_STARTED`。
- 根因分析：测试响应使用了 `ToolAdapterResponse` 的只读默认值。
- 修复方案：写操作 Fake 显式权威返回 `COMMITTED`，并新增 phase-aware Fake。
- 回归测试：写操作成功与 after-return fault 都断言 effect=1 对应 `COMMITTED`。
- 对应知识点：Fake fidelity、权威响应语义。
- 面试表达：我发现的是测试基础设施中的真实保真缺陷，不把它包装成生产事故。
- 当前状态：已修复；真实性限定为测试代码发现。

### Bad Case 9：未执行副作用被伪造 COMMITTED

- 类型：假设构造
- 触发条件：before seam 在 Tracker 转换前命中，却由 mapper 推断为 committed。
- 故障表现：Recovery/人工处置会误以为外部操作已发生。
- 根因分析：把“Provider 已开始”错误等同于“副作用已提交”。
- 修复方案：直接读取 Tracker；callback 未越过时保持 `NOT_STARTED`。
- 回归测试：callback count=1、marker/effect=0、state=`NOT_STARTED`。
- 对应知识点：Provider state 与 side-effect state 正交。
- 面试表达：调用开始和业务提交是两个独立事实，必须分别记录。
- 当前状态：已由测试阻断。

### Bad Case 10：Outcome Unknown 被当作普通失败

- 类型：假设构造
- 触发条件：Adapter 权威返回 `UNKNOWN`，after-return mapper 仍给普通 transient retry。
- 故障表现：不确定是否提交的操作被重复执行。
- 根因分析：错误分类覆盖了权威 Side-effect State。
- 修复方案：保留 `UNKNOWN`，让既有矩阵返回 `OUTCOME_UNKNOWN`。
- 回归测试：unknown fake 断言 Provider/effect 各 1、无 retry、无 compensation。
- 对应知识点：不确定结果、fail closed。
- 面试表达：未知结果不是“失败未执行”，它需要人工或权威恢复证据。
- 当前状态：已由实现与测试阻断。

### Bad Case 11：TOOL_STARTED 缺少终止事件

- 类型：假设构造
- 触发条件：InjectedFaultError 直接越过 Attempt 的 completion owner。
- 故障表现：Journal/Trace 中存在悬空 Tool Attempt。
- 根因分析：Controller 自行返回错误，绕过 `_emit_completed()`。
- 修复方案：新 seam 仍由 `_execute_impl()` catch 并发布唯一 `TOOL_COMPLETED`。
- 回归测试：before/after/cancellation 均断言 Started 后恰好一个 Completed。
- 对应知识点：事件配对、终态所有权。
- 面试表达：注入点可以改变结果，不能破坏生命周期闭合。
- 当前状态：已由测试阻断。

### Bad Case 12：Recovery Evidence 丢失 COMMITTED

- 类型：假设构造
- 触发条件：post-provider 错误对象重建时固定使用 `NOT_STARTED`。
- 故障表现：后续 reducer 看不到已经发生的业务提交。
- 根因分析：错误 mapper 未携带真实 Tracker state。
- 修复方案：Error 与 Completed payload 都读取同一 Tracker state。
- 回归测试：recovery evidence 测试断言 `COMMITTED + POST_COMMIT_RESPONSE_FAILURE + UNSAFE`。
- 对应知识点：恢复证据保真、journal evidence。
- 面试表达：恢复系统最重要的不是错误文本，而是不能丢失已提交事实。
- 当前状态：已由测试阻断。

### Bad Case 13：Fault Rule ID 进入 Tool Event

- 类型：假设构造
- 触发条件：为诊断方便把命中的 Rule 或原始异常塞入 payload。
- 故障表现：测试配置或敏感上下文泄漏到 Event/Journal/Client。
- 根因分析：混淆 fault recorder 与业务 event 的安全边界。
- 修复方案：事件只使用固定 safe code 和既有摘要字段。
- 回归测试：safe event 中不存在 `tool-fault`、参数 secret 或 Key 明文。
- 对应知识点：最小化遥测、数据分域。
- 面试表达：测试诊断元数据应留在 fault recorder，不进入业务事件。
- 当前状态：已由安全断言阻断。

### Bad Case 14：Cancellation 覆盖已提交事实

- 类型：假设构造
- 触发条件：after-return blocker 中取消 Run，catch 统一重置为 `NOT_STARTED`。
- 故障表现：Client 停止掩盖了外部 effect 已发生的事实。
- 根因分析：把控制流取消当成业务回滚。
- 修复方案：`mark_unknown_if_started()` 不覆盖 `COMMITTED`，终止事件读取真实 Tracker。
- 回归测试：after-return cancellation 的 Completed 仍为 `provider_started=true + COMMITTED`。
- 对应知识点：取消语义、状态单调性。
- 面试表达：取消只停止后续工作，不会逆转已经发生的外部事实。
- 当前状态：已由测试阻断。

### Bad Case 15：Disabled Controller 改变 Side-effect State

- 类型：假设构造
- 触发条件：即使 disabled 仍构造/执行 callback 或消费 Rule counter。
- 故障表现：关闭故障注入也改变 Provider、状态或事件。
- 根因分析：缺少 cheap parity guard。
- 修复方案：构造 callback 与执行 seam 前检查 controller enabled。
- 回归测试：disabled 与 no-controller 结果一致，match/hit 均为 0。
- 对应知识点：feature-disabled parity、零副作用开关。
- 面试表达：测试能力关闭时必须像不存在一样，而不只是“不抛异常”。
- 当前状态：已由测试阻断。

### Bad Case 16：B2b Fault Point 被提前调用

- 类型：假设构造
- 触发条件：在 completion 或 post-commit 路径顺手调用预留枚举点。
- 故障表现：B2a 范围扩张到 Journal/Event/Recovery 危险窗口。
- 根因分析：把合同中存在的枚举误当成本轮授权接入点。
- 修复方案：只调用两个 B2a 点；B2b 点保持零 match/hit。
- 回归测试：`TOOL_AFTER_SIDE_EFFECT_COMMIT` 与 `TOOL_BEFORE_COMPLETION_EVENT` Rule 均不命中。
- 对应知识点：迁移边界、渐进式接入。
- 面试表达：枚举可提前定义，但运行时接入必须按迁移阶段显式授权。
- 当前状态：已由源码边界与测试阻断。

## 20. 测试结果

新增：

```text
tests/test_tool_provider_boundary_fault_injection.py
tests/test_tool_side_effect_commit_boundary.py
tests/test_tool_post_provider_fault_injection.py
tests/test_tool_fault_recovery_evidence.py
```

同时更新危险点合同、B2b 未调用断言与共享测试 Fake。任务指定目标集合结果：

```text
156 passed in 2.24s
```

聚焦 B2a 回归结果：

```text
51 passed in 0.50s
```

最终验证结果：

```text
全仓 pytest：816 passed, 42 subtests passed in 9.38s
compileall：通过
uv lock --check：通过（Resolved 157 packages）
git diff --check：通过（仅 Git 的 LF→CRLF 工作区提示，无 whitespace error）
```

## 21. 未完成事项

本轮有意未完成：`TOOL_AFTER_SIDE_EFFECT_COMMIT`、
`TOOL_BEFORE_COMPLETION_EVENT`、EventJournal/RuntimeEventChannel fault、Snapshot/Recovery
fault、Observability/Trace fault、Shutdown fault、生产 fault 配置、自动 Compensation、自动
Recovery/Replay，以及任何真实文件、数据库、网络或外部 Tool 副作用。

## 22. B2b 接入点

B2b 只能在后续任务中接入：

- Adapter/Tracker 已确认提交后的 `TOOL_AFTER_SIDE_EFFECT_COMMIT`；
- 完成事实发布前的 `TOOL_BEFORE_COMPLETION_EVENT`。

本轮二者 `match_count=0`、`hit_count=0`。不得把当前 after-provider seam 误当作
after-side-effect-commit：read-only Adapter 也会 return，而不一定存在副作用提交。

## 23. 需要带回 ChatGPT 审查的信息

| 项目 | 结论 |
| --- | --- |
| Provider execution order | Started → provider flag → Adapter → before seam → Tracker STARTED → effect → return → Budget commit → authority state → after seam → normalization/event |
| Before side-effect commit location | `ToolExecutionContext.before_side_effect()` 的 Tracker 转换前 |
| After provider return location | Adapter 正常返回、Budget commit 与权威 state 写入后，output/result normalization 前 |
| Dangerous flag | 四个 Tool dangerous point 均强制 `true` |
| Phase-aware fake | 纯内存、六个要求计数、五阶段 barrier、三类状态与 replay |
| Provider calls/started | before fault 为 1/true；after fault 为 1/true；安全 retry 按原 policy 增加 |
| Before-side-effect calls | 命中为 1，marker/effect 为 0 |
| External effects | before 为 0；non-idempotent after 为 1；key replay 总计仍为 1 |
| Side-effect state before/after | `NOT_STARTED` / 权威 `COMMITTED` 或 `UNKNOWN` |
| Post-provider mapping | committed/unknown 使用既有 `POST_COMMIT_RESPONSE_FAILURE`；只读未提交按 code + 原 policy |
| Read-only retry | transient 可按原 policy retry |
| Idempotent retry | natural committed 保守；stable key + replay 可安全 replay |
| Non-idempotent retry | committed 为 `UNSAFE`，不 retry |
| Outcome unknown | `UNKNOWN + OUTCOME_UNKNOWN`，fail closed |
| Budget handling | Provider started 后 commit Estimated；Provider 前才 release |
| Permit/Lease cleanup | 全部释放，active worker/reservation 为 0 |
| Compensation calls | 0 |
| Detached worker | 0 |
| Tool events | Started 后恰好一个 Completed，安全 evidence 保真 |
| Tool spans | active span 为 0 |
| Recovery evidence | before 保留 NOT_STARTED；after 保留 COMMITTED/UNKNOWN |
| Cancellation | before 保留 NOT_STARTED；after 保留 COMMITTED；资源收口 |
| Disabled parity | 与 no-controller 一致，counter 不消费 |
| Run/Invocation isolation | 独立 context、tracker、fake state，无跨 Run 污染 |
| B2b points invoked | 0 |
| Runtime event fault data | 0；未接入 |
| Journal fault data | 0；未接入 |
| Wire fault data | 0；未接入 |
| 新增测试 | 4 个 B2a 文件，并更新 3 个既有合同/共享测试文件 |
| 目标 pytest | 156 passed in 2.24s |
| 全仓 pytest | 816 passed, 42 subtests passed in 9.38s |
| compileall | 通过 |
| lock check | 通过 |
| diff check | 通过；无 whitespace error |
| 需要人工确认的问题 | 无；B2b 接入需后续独立授权 |
