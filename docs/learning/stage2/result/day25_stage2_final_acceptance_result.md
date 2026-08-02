# 阶段二第 25 天第四轮：Stage2 Final Acceptance

最终结论：**Stage2 Runtime RC1 code-level gate PASS**。这是代码、合同、离线集成与 API 场景层面的候选版本验收；生产验证仍未完成，不构成容量、容灾或外部依赖认证。

## 1. 阶段二目标

将 LocalAgent 从隐式编排路径改造成边界清楚、状态可审计、失败可分类、资源可收口的 Runtime，并保留显式 Legacy 回滚能力。

## 2. 阶段二完成范围

完成 RunContext、AgentState、调度、预算/超时/取消、Model/Tool/Retrieval 合同、Journal-first 事件、诊断、Snapshot、只读 Recovery Validation、生命周期、确定性故障注入、RC Gate 与运维文档。没有新增本轮禁止的 Runtime 能力。

## 3. 最终架构

请求从 API / ChatService 进入，只选择一次 Runtime；Coordinated 路径创建单一 RunScope，Coordinator 调度 Model、Tool 与 Retrieval，通过 Event/Journal 发布事实，并由 Observability、Trace、Snapshot 与 Shutdown 边界提供诊断和生命周期治理。

## 4. Composition Root

`server.py` lifespan 是唯一生产 Composition Root（组合根）。测试可以显式组装 fixture，但 fixture 不属于生产 public path。配置缺失在当前局部 Chat/Runtime 工厂边界映射为 `RUNTIME_CONFIGURATION_ERROR`。

## 5. Scope Matrix

每个 Coordinated Run 只有一个 RunContext、CancellationSource、EventChannel、per-run sequence owner、Registry registration 与 root span。跨进程 Registry 不在阶段二范围。

## 6. Owner Matrix

AgentState 是运行状态唯一 Owner；RetryExecutor 拥有 Model retry；ModelInvocationRouter 拥有候选模型 fallback；AttemptSideEffectTracker 持有副作用证据；EventChannel 持有 sequence；Coordinator 持有 terminal；RecoveryValidator 只拥有验证；GracefulShutdownCoordinator 拥有关闭编排。

## 7. Runtime Mode 与 Legacy Boundary

默认模式为 Coordinated。Legacy 只能通过配置显式选择，不是 Coordinated 失败后的动态 fallback；单请求不会跨 Runtime 重试。

## 8. Model Runtime

模型调用遵守候选、重试、超时、取消与熔断合同。候选模型 fallback 是同一 Runtime 内的策略，不等同跨 Runtime fallback。

## 9. Tool Runtime

工具执行使用强类型合同与单调副作用证据。非幂等调用一旦证据为 COMMITTED，不自动重试；UNKNOWN 不降级成 NOT_STARTED，需要人工对账。

## 10. Retrieval Runtime

query rewrite 与 rerank 失败可受控降级；embedding 或 vector search 失败 fail closed，不伪装为空结果。

## 11. Event / Journal / Streaming

业务事件先写 Journal，再发布到 channel。部分发布有显式证据，发布失败不会重跑已提交业务；sequence 不复用，控制事件不进入 final output。

## 12. Observability / Trace

诊断为 best effort：失败不改变业务结果、不删除 Journal，也不把诊断异常放入用户正文。Span 结束路径保证 active span 归零并恢复 ContextVar。

## 13. Snapshot / Recovery Validation

Snapshot 为 opt-in；after-save 取消保留已提交事实。Recovery 只信 Snapshot + Journal，RecoveryValidator 只读，不执行 Model、Tool、Retrieval、Replay、Compensation 或业务对账动作。

## 14. Client Disconnect / Worker

客户端断开后停止继续输出并传播 cooperative cancellation（协作式取消）。同步 worker 不能被安全强杀；detached worker 必须继续可见，不能被清成 idle。

## 15. Graceful Shutdown

先停止接收、取消活动 Run、等待 producer/watcher/worker，再关闭资源。`ShutdownReport.completed` 只是 `orchestration_completed` 的兼容别名，不表示 `fully_closed`；Model 仅在 worker safety gate 证明安全后关闭。

## 16. Fault Injection / Chaos Matrix

确定性 Fault Injection 仅用于测试：共 42 个 Fault Point，其中 32 个 Supported、10 个 Contract-only、0 个“Supported 但无测试”。生产激活与随机 Chaos 均未实现。

## 17. Contract Freeze

Runtime mode、错误映射、状态/Owner、schema、Legacy boundary、故障生产隔离与派生报告边界均由合同测试冻结；不存在用枚举或 Runbook 冒充能力实现的升级。

## 18. Release Candidate

RC1 包含 20 个必选场景：API E2E 4、Runtime E2E 3、Subsystem Integration 13、Contract 0；最终全部通过。

## 19. Operations / Runbook

配置、发布、观察、故障定位、恢复验证、回滚与安全扫描均有 Runbook。人工对账写入外部工单或未来独立 Incident / Reconciliation Record，不能改写 JournalRecord、RunSnapshot、历史 AgentState，也不能补造 TOOL_COMPLETED。

## 20. Evidence Manifest

`docs/runtime/stage2_runtime_evidence_manifest.md` 记录 43 个 Claim 的权威代码 Owner、真实测试节点、证据等级与限制。它是文档级 Derived Report，不持有 Runtime 对象，也不是事实 Owner。

## 21. Final Runtime Invariants

最终测试侧报告只保存计数与固定违规码。正常场景要求 selection/context/state/cancellation/channel/sequence/registration/root span/terminal 各自唯一，Registry、Reservation、Permit、Watcher、Producer、Channel、Span 与 worker 基线归零；detached worker 场景必须非零并如实报告。业务、副作用、持久化、诊断和生命周期不变量由现有专项测试共同证明。

## 22. Final Release Gate

P0=0，P1=0，P2=1，Known Limitations=7；RC=20/20；Contract、Operations docs、full suite、resource invariants、security scan 均为 true。最终状态为 **Stage2 Runtime RC1 code-level gate PASS**。

## 23. Security Boundary

报告与清单不保存绝对路径、测试机信息、业务正文、原始异常、密钥、Provider 配置或私有地址；最终验收材料只使用逻辑模块路径、测试节点与固定安全编号。

## 24. Compatibility / Deprecated

Legacy 与 module-level compatibility handle 暂时保留；lifespan 启停同步发布与清空 handle，避免引用已关闭对象。Legacy 不会在 Coordinated 失败后自动启用。

## 25. Bad Case

### Bad Case 1：Shutdown completed 被误读为 fully closed

- 类型：真实发现
- 触发条件：关闭编排完成，但 worker 未证明 idle 或仍有 deferred resource。
- 故障表现：调用方可能把流程走完误报为资源全部关闭。
- 根因分析：流程完成与资源事实共用含混字段。
- 修复方案：分离 `orchestration_completed` 与 `fully_closed`，兼容字段只代表前者。
- 回归测试：`tests/test_shutdown_report_truthfulness.py`。
- 对应知识点：生命周期报告必须区分过程和事实。
- 面试表达：我让 shutdown 报告能诚实表达 deferred worker，而不是制造绿色结果。
- 当前状态：已修复并回归；开发审计发现，不是生产事故。

### Bad Case 2：Model alias 绕过 worker close gate

- 类型：真实发现
- 触发条件：同一资源以多个 component alias 进入关闭集合。
- 故障表现：一个 alias 被 defer，另一个仍可能关闭同一对象。
- 根因分析：安全判断按名称而非对象 identity。
- 修复方案：按 identity 去重并统一应用 worker gate。
- 回归测试：`tests/test_application_runtime_services.py`。
- 对应知识点：资源所有权与关闭必须围绕 identity。
- 面试表达：别名不能改变共享资源的生命周期事实。
- 当前状态：已修复并回归；代码审计发现，不是生产事故。

### Bad Case 3：UNKNOWN 副作用被当成未开始

- 类型：假设构造
- 触发条件：非幂等工具在提交边界附近丢失确认。
- 故障表现：若错误降级为 NOT_STARTED，自动重试可能重复副作用。
- 根因分析：把缺少确认误当成没有执行。
- 修复方案：证据状态单调，UNKNOWN 保留并转人工对账。
- 回归测试：`tests/test_tool_execution_integration.py` 与副作用专项测试。
- 对应知识点：absence of evidence 不等于 evidence of absence。
- 面试表达：宁可保留不确定性，也不伪造可重试结论。
- 当前状态：机制风险已由合同覆盖；未描述为真实生产事故。

完整 15 个案例见 `docs/interview/stage2_runtime_bad_cases.md`。

## 26. 测试结果

最终文档测试 `14` passed；Day25 测试 `76` passed；阶段二关键回归 `49` passed；全仓 `1089` passed（由本轮测试收集输入生成，并由最终执行复核）；附加 subtests 42 passed。`compileall`、`uv lock --check`、`git diff --check` 均通过。

## 27. 明确未完成

Recovery execution、Replay、Step result rehydration、Production Fault Enablement、Random Chaos、Cross-process Registry、Exactly-once、Automatic compensation 均未实现。

## 28. 生产验证仍需完成

真实外部依赖受控环境、标准 SSE 互操作、生产指标 Exporter、容量/压力/Soak、故障演练、升级回滚、灾备目标与安全运营接入尚未验证；因此生产验证仍未完成。

## 29. 面试表达

核心表达是“把隐式 Agent 调用改造成显式 Runtime 合同”：一次选择、唯一 Owner、Journal-first、单调副作用证据、诊断不影响业务、detached worker 如实可见，并主动说明 Recovery 只做到验证。

## 30. 简历素材

使用可核验的自动化测试规模与 code-level RC 结论，不写用户规模、虚构性能提升、生产 P95、自动恢复、系统级 exactly-once 或生产 Chaos。

## 31. 下一阶段入口

下一阶段按 P0_NEXT、P1_NEXT、P2_LATER、RESEARCH_ONLY 分级，全部 `not_started=true`；阶段二仅完成交接，不实现这些项目。

## 32. 需要带回 ChatGPT 审查的信息

请审查：43 项 Evidence Manifest 是否逐项可解析；P2-01 与 KL-01～KL-07 是否准确；最终测试计数是否来自本轮实际收集/执行；生产未验证项是否充分；人工对账、配置错误码、Legacy 与 shutdown 语义是否仍有夸大空间。
