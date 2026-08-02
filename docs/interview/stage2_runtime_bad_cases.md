# Stage2 Runtime High-value Bad Cases

下列“真实发现”均指开发/审计或自动化测试中发现的真实代码/文档问题，不是生产事故；“机制风险”表示由架构窗口推导并被测试锁定的风险；“假设构造”不冒充真实事件。

## 1. `[[ORCH]]` 污染 final output

- 场景：控制行与普通文本共用 compatibility stream。
- 触发：聚合所有 chunk 作为 AgentState final output。
- 风险：控制 JSON 污染业务答案与 Memory。
- 根因：Wire control 与业务 output 未在适配层分离。
- 修复：只聚合 OutputDelta，控制行仅用于 transport。
- 回归：`tests/test_agent_loop.py::AgentLoopTests::test_legacy_driver_calls_router_once_and_keeps_chunks_and_context` 与 RC-01。
- 设计原则：协议投影不等于业务事实。
- 面试表达：我把控制事件留在边界，不进入最终业务输出。
- 真实性边界：真实发现；开发测试问题，非生产事故。

## 2. CancellationSource 所有权丢失

- 场景：RunContext 工厂只返回 token。
- 触发：内部创建 Source 后不向上游返回强引用。
- 风险：上游无法发起取消，owner 生命周期不明确。
- 根因：读权限与写权限拆分不完整。
- 修复：`create_run_context()` 返回 Context + Source，Context 只暴露 Token。
- 回归：`tests/test_runtime_context.py`。
- 设计原则：取消能力必须有唯一强 owner。
- 面试表达：Token 可传播，Source 只交给生命周期 owner。
- 真实性边界：真实发现；代码审计中修复。

## 3. Plan 与 AgentState 双写风险

- 场景：把 runtime status 加入 PlanStep。
- 触发：Scheduler 与 StateMachine 分别更新同一 step 事实。
- 风险：Snapshot、调度与 terminal 判断分叉。
- 根因：定义模型和运行状态模型边界模糊。
- 修复：Plan immutable，AgentState 经 StateMachine 唯一写入。
- 回归：`tests/test_runtime_owner_matrix.py`。
- 设计原则：一个事实一个 owner。
- 面试表达：Plan 回答做什么，State 回答执行到哪里。
- 真实性边界：机制风险；未描述为已发生生产故障。

## 4. 未执行 RunScope 直接 close 导致 Registry 泄漏

- 场景：factory 创建 scope 后业务尚未 execute 就关闭。
- 触发：create 后异常/取消进入 close。
- 风险：active registry handle 永久残留。
- 根因：unregister 只放在 execute finally。
- 修复：scope close/abort 也幂等注销 registration。
- 回归：`tests/test_coordinated_runtime_factory.py`、`tests/test_runtime_scope_matrix.py`。
- 设计原则：资源创建成功即必须有对称释放路径。
- 面试表达：我覆盖了“创建但未执行”的生命周期窗口。
- 真实性边界：真实发现；测试窗口中复现。

## 5. FaultPlan 接受未知高版本

- 场景：schema_version 只验证为正整数。
- 触发：构造 version=999。
- 风险：测试计划被错误解释，契约不再 fail closed。
- 根因：有版本字段但没有 supported-version gate。
- 修复：只接受 `FAULT_PLAN_SCHEMA_VERSION=1`。
- 回归：`tests/test_runtime_schema_matrix.py`。
- 设计原则：版本化合同必须拒绝未知版本。
- 面试表达：版本字段不是装饰，reader 必须明确支持集合。
- 真实性边界：真实发现；代码契约问题。

## 6. Test Fixture 进入 production public path

- 场景：Tool completion gap fixture 位于 core package 并被导出。
- 触发：生产 import surface 可见测试 oracle。
- 风险：Recovery 可能接受非持久证据。
- 根因：fixture 与 validator 证据边界未隔离。
- 修复：fixture 移到 tests，production Validator 只收 Snapshot + Journal。
- 回归：`tests/test_runtime_contract_freeze.py::test_recovery_validator_is_read_only_and_test_fixture_is_not_public`。
- 设计原则：Test Oracle 不能成为 Production Authority。
- 面试表达：我从 public surface 移除了会污染恢复权威的测试夹具。
- 真实性边界：真实发现；静态审计中确认。

## 7. Terminal 同时执行两个 pre-append seam

- 场景：terminal event 经过 generic 与 terminal-specific fault point。
- 触发：发布 RUN_COMPLETED。
- 风险：一次逻辑窗口被注入两次，计数和故障语义失真。
- 根因：通用路径未排除 terminal 特殊路径。
- 修复：按 event type 选择唯一 pre-append seam。
- 回归：`tests/test_journal_fault_injection.py`。
- 设计原则：一个物理窗口只能有一个权威 fault seam。
- 面试表达：我消除了 terminal 双重注入的歧义。
- 真实性边界：真实发现；Fault audit 中修复。

## 8. EventPublicationError 持有完整 RuntimeEvent

- 场景：异常对象引用带 payload 的 Event。
- 触发：journal/channel publication failure。
- 风险：repr/report 泄露正文并延长对象生命周期。
- 根因：错误诊断直接携带 authority object。
- 修复：只保存冻结的 payload-free publication evidence。
- 回归：`tests/test_event_partial_publication.py`、`tests/test_journal_fault_injection.py`。
- 设计原则：错误对象保存安全证据，不保存业务对象。
- 面试表达：我用 allowlist evidence 替换异常里的完整 Event。
- 真实性边界：真实发现；安全审计问题。

## 9. Recovery 运行失败误标 Unsupported

- 场景：snapshot/tail read 未执行或 validator 被取消。
- 触发：operation fault/cancellation。
- 风险：运维把暂时执行失败当成能力不支持。
- 根因：状态优先级与原因分类不精确。
- 修复：区分 FAILED、UNSUPPORTED、CORRUPTED 与 reconciliation。
- 回归：`tests/test_recovery_fault_injection.py`。
- 设计原则：能力缺失、证据损坏和操作失败必须分型。
- 面试表达：恢复结论需要精确到失败阶段，而不是统一 Unsupported。
- 真实性边界：真实发现；测试中发现的分类问题。

## 10. Trace failed start 擦除父 context

- 场景：子 span start 故障返回 no-op handle。
- 触发：嵌套调用后 reset 错误 token/context。
- 风险：父 trace context 丢失，后续 sibling 关联错误。
- 根因：失败路径未保持 ContextVar token 对称。
- 修复：失败 start 不覆盖父 context，所有 end/reset finally 对称。
- 回归：`tests/test_trace_lifecycle_fault.py`。
- 设计原则：ContextVar 安装与恢复必须栈式对称。
- 面试表达：即使 Span 创建失败，也不能破坏调用链上下文。
- 真实性边界：真实发现；fault test 中复现。

## 11. Run cancel 批量 generator 被一个异常中断

- 场景：shutdown 逐个取消 active handles。
- 触发：一个 cancel callback 抛异常。
- 风险：后续 Run 未收到取消，drain 行为不完整。
- 根因：批量循环缺少逐项异常隔离。
- 修复：每个 handle 独立 try/report，继续处理其余 Run。
- 回归：`tests/test_shutdown_run_cancel_fault.py`。
- 设计原则：批量生命周期操作必须逐项隔离。
- 面试表达：一个坏 callback 不能阻断整个 shutdown cancel batch。
- 真实性边界：真实发现；确定性 fault test 覆盖。

## 12. Worker drain 未执行却被当 idle

- 场景：before-worker-drain seam 失败。
- 触发：shutdown 跳过真实 wait。
- 风险：Model 被关闭，仍运行 worker 使用已关闭 client。
- 根因：把“没有得到非 idle”误当成“已证明 idle”。
- 修复：未执行/unknown 保守计数，Model close deferred。
- 回归：`tests/test_shutdown_report_truthfulness.py::test_report_distinguishes_unexecuted_drain_and_deferred_model`。
- 设计原则：absence of evidence 不是安全证明。
- 面试表达：只有证明 idle 才能过 Model close gate。
- 真实性边界：真实发现；shutdown fault 审计中修复。

## 13. Model alias 绕过 close gate

- 场景：同一 Model resource 通过多个 component alias 进入 close 列表。
- 触发：worker 活跃时只 defer 一个名称。
- 风险：另一个 alias 仍关闭同一 identity。
- 根因：安全 gate 按名称而非对象 identity。
- 修复：共享资源按 identity 去重并统一应用 Model gate。
- 回归：`tests/test_application_runtime_services.py`、`tests/test_shutdown_component_fault.py`。
- 设计原则：资源所有权与关闭应按 identity，不按别名。
- 面试表达：我防止了同一 client 通过别名绕过 worker safety gate。
- 真实性边界：真实发现；资源审计问题。

## 14. Shutdown completed 被误解为 fully closed

- 场景：兼容字段 completed=true，但存在 deferred resource。
- 触发：orchestration 走完且 worker 未 idle。
- 风险：运维误报安全关闭并重启/释放共享依赖。
- 根因：流程完成与资源结果共用一个词。
- 修复：引入 `orchestration_completed` 与 `fully_closed`，保留 completed 仅作兼容别名。
- 回归：`tests/test_shutdown_report_truthfulness.py::test_shutdown_top_level_semantics_distinguish_orchestration_and_closure`。
- 设计原则：报告语义必须区分流程与事实。
- 面试表达：编排完成不代表资源已关净。
- 真实性边界：真实发现；属于语义审计，非生产事故。

## 15. Module-level compatibility handle 保留已关闭对象

- 场景：module global 清空但 app.state 仍引用 closed services。
- 触发：lifespan 退出后下一次测试/诊断读取 app.state。
- 风险：关闭对象被误用或生命周期真值分叉。
- 根因：两个兼容发布面只清理一侧。
- 修复：启动同步 publish，关闭同步置 None。
- 回归：`tests/test_server_compatibility_handles.py`。
- 设计原则：兼容句柄必须共享同一 lifecycle truth。
- 面试表达：我保留了兼容入口，但消除了 stale handle。
- 真实性边界：真实发现；代码审计中修复。
