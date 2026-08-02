# 阶段二第 25 天第二轮：Release Candidate Validation

## 1. 本轮目标

冻结 `Stage2 Runtime RC1` 的范围、Release Gate、20 个必选场景和离线资源基线；不新增 Runtime 架构能力，不实现自动 recovery/replay/resume。

## 2. 第一轮契约修正

1. 将 Fault Injection 拆为三项：Deterministic Fault Injection=`SUPPORTED` + `TEST_SCOPE/explicit operation scope` + production_enablement=false；Production Fault Enablement=`NOT_IMPLEMENTED`；Random / Probabilistic Chaos=`NOT_IMPLEMENTED`。
2. 真实代码证明 `AgentState` 有 `AGENT_STATE_SCHEMA_VERSION=1`、`to_dict()` 和严格 `from_dict()`，因此保留 `PUBLIC_VERSIONED`，并补入 Schema Matrix 与 fail-closed 回归。

## 3. RC 范围

RC identifier：`Stage2 Runtime RC1`。包含 Coordinated 默认入口、显式 Legacy rollback、Context/State、deadline/cancel、parallel、budget、model retry/fallback/circuit、tool/evidence、retrieval、streaming、Journal-first、trace/observability、snapshot opt-in、recovery validation only、disconnect、worker 与 shutdown、确定性 FI 测试。

不包含 automatic recovery、replay/resume、result rehydration、random chaos、production fault activation、cross-process registry、exactly-once、automatic compensation 和分布式 durable execution。

## 4. Release Gate

Gate 见 `docs/runtime/runtime_release_gate.md`。当前 P0=0、P1=0、P2=1、Known Limitations=7；20/20 REQUIRED 场景、契约、全仓、资源和安全检查均通过时才能 PASS。

## 5. RC Scenario Matrix

`docs/runtime/runtime_rc_scenario_matrix.md` 记录 20 个 REQUIRED 场景的入口、路径、Event/Journal/Budget/资源/shutdown 期望与真实 `test_id`。当前 20 passed、0 failed。

## 6. Coordinated Normal Path

RC-01 覆盖 `/api/chat -> COORDINATED -> factory -> coordinator -> model -> RUN_COMPLETED`；一个 Runtime selection、RunContext、sequence owner 和 terminal，最终 Registry/Channel/Span 归零，业务文本不含 `[[ORCH]]`。

## 7. Model Retry / Fallback

RC-02 锁定 RetryExecutor 对 attempt 的唯一所有权；RC-03 只在 Coordinated Model candidate 内 fallback，不切 Legacy。调用数、attempt identity、budget 和 span 可解释。

## 8. Retrieval Degradation / Failure

RC-04 的 rewrite 故障按现有合同降级为 original query；RC-05 的 embedding/vector failure fail closed，不伪装空结果、不自动二次搜索。

## 9. Tool Success / Retry / Committed Failure

RC-06 覆盖只读成功和安全 evidence；RC-07 保持 invocation identity、变更 attempt identity；RC-08 在非幂等副作用 `COMMITTED` 后故障时 provider 仅一次，不 retry、不 compensation，结果保守为 unsafe/manual。

## 10. Parallel Best-effort / Fail-fast

RC-09 验证 step 状态独立与稳定聚合；RC-10 验证首个失败取消等待 sibling、first-wins 且无孤儿 Task/双 terminal。

## 11. Budget / Deadline / Cancellation

RC-11 在 adapter 前拒绝超额预算；RC-12 在可取消等待中区分 deadline 与 provider-started 事实；取消批次证明 first-wins 和无跨 Run 污染。

## 12. Client Disconnect

RC-13 由 transport owner 取消并 bounded drain/abort；断开后不再输出 SAFE_ERROR，watcher、producer 和 channel owner 收口。

## 13. Snapshot / Recovery Validation

RC-14 验证 opt-in checkpoint 的 quiescence、digest、watermark 和 save；RC-15/16 只读 Snapshot+Journal，不调 Model/Tool/Retrieval，不改 AgentState，不生成 replay plan，不从 live Registry 回填证据。

## 14. Clean / Degraded Shutdown

RC-17 清洁关闭为 `orchestration_completed=true` 且 `fully_closed=true`；RC-18 保留 detached worker 真值，Model close deferred，`fully_closed=false`。

## 15. Legacy Rollback

RC-19 仅通过请求前显式 `LEGACY` 选择；不创建 Coordinated Scope，不虚构 Journal/Snapshot/Recovery 能力。

## 16. No Cross-runtime Fallback

RC-20 证明 Coordinated 失败仅在已选路径安全收口；Legacy 调用数为 0，业务调用一次、terminal 一个。

## 17. Resource Baseline

50 次离线串行 Run：总耗时 0.088393 s，平均 0.001768 s，中位 0.001744 s，P95 0.002181 s；每 Run 5 条 Journal，Snapshot rows=0。这只是当前机器基线。

Registry=0、Reservation=0、Permit=0、active Span=0、active Worker=0、Watcher=0、Producer=0、Channel owner=0。未启用的 Tool/Snapshot 路径计数为 0，不伪造调用。

## 18. Concurrent Baseline

10 个并发 Run 全部完成，10 个 unique run_id，每 Run sequence 独立从 1 开始，最终 Application 不保留 Run/Controller。取消基线为 10 个 Context 取消 5 个，未取消 5 个不受影响。

## 19. Memory Baseline

warm-up 后两个 10-run batch 的 tracemalloc retained trend 为 `+185438 bytes` 和 `+107334 bytes`。只记录趋势，不设 SLA/阻塞阈值；Journal 预期增长不视为 owner leak。

## 20. Compatibility Handles

`server.py` 保留 `chat_service` 和 `application_runtime_services`。lifespan 内它们与 `app.state` 指向同一 Application-scope 对象，不保存 Run/Operation owner；退出时两侧同步置 `None`，`require_service()` 对关闭句柄返回 503。该兼容项本轮保留。

## 21. Release Gate Assessment

`ReleaseGateAssessment` 为 frozen test helper，仅存固定 ID、计数和布尔检查。当前：P0=0、P1=0、P2=1、known limitations=7、RC=20/20、status=`PASS`。PASS 由实际测试/检查派生，不读 Markdown 勾选项。

## 22. Security

扫描 Result、Event/Journal、Snapshot/Recovery、Trace/Observability、ShutdownReport、ReleaseGateAssessment、baseline、RC 文档、Wire/Log/Metric/Span。敏感 marker 未进入正式输出；Gate Report 拒绝路径和原始错误作为 finding ID。

## 23. Bad Case

### Bad Case 1：Fault Injection 测试能力被标为 Contract-only

- 类型：真实发现
- 触发条件：第一轮 Capability Matrix 用单行概括整个 Fault Injection。
- 故障表现：已有确定性测试能力被降格为 `CONTRACT_ONLY`。
- 根因分析：混淆了测试 seam 可执行性与生产激活边界。
- 修复方案：拆成 Deterministic=`SUPPORTED`、Production Enablement/Random Chaos=`NOT_IMPLEMENTED`。
- 回归测试：`test_deterministic_fault_injection_is_supported_only_by_explicit_test_scope`。
- 对应知识点：Capability 分类应同时表达“能做什么”和“哪里能做”。
- 面试表达：我把确定性 FI 测试能力与生产 Chaos 激活分开冻结。
- 当前状态：已修复

### Bad Case 2：AgentState 没有独立 Schema 却被声明为 Versioned

- 类型：假设构造
- 触发条件：只看第一轮 Schema Matrix 漏项，未审计 `AgentState` 真实 reader/writer。
- 故障表现：可能错误降级合同，或无证据地保留 Versioned。
- 根因分析：文档缺失被误当成代码能力缺失。
- 修复方案：审计并记录 v1、`to_dict/from_dict`、fail-closed 行为。
- 回归测试：`test_agent_state_has_independent_v1_reader_writer_and_fails_closed`。
- 对应知识点：合同分类必须由代码和负向测试支撑。
- 面试表达：我没把矩阵漏项当成 schema 缺失，而是用真实 reader/writer 证明 v1 合同。
- 当前状态：已排除假设，矩阵已修正

### Bad Case 3：Release Gate 只检查全仓 pytest

- 类型：假设构造
- 触发条件：以 full suite 绿色作为唯一发布信号。
- 故障表现：资源 owner 持续非零仍可被判 PASS。
- 根因分析：功能测试与运行时不变量未组合。
- 修复方案：Gate 必须同时读取 RC、full suite、resource invariants 和 security 结果。
- 回归测试：`test_each_hard_gate_failure_forces_fail`。
- 对应知识点：Release Gate 是多信号合取。
- 面试表达：我把资源和安全不变量提升为独立硬 Gate。
- 当前状态：已防护

### Bad Case 4：E2E 成功但 Registry Handle 未清理

- 类型：假设构造
- 触发条件：返回正常文本后遗漏 unregister。
- 故障表现：业务成功但 active_runs 持续增长。
- 根因分析：将输出完成误当为 scope 关闭。
- 修复方案：Run scope finally 注销，基线每轮检查 owner 计数。
- 回归测试：RC-01 与 `test_sequential_50_run_offline_machine_baseline_and_cleanup`。
- 对应知识点：成功结果不等于资源已收口。
- 面试表达：我在 E2E 后检查 Registry/Channel/Span 真值，不只看返回值。
- 当前状态：未在真实代码复现，已防护

### Bad Case 5：并发 Run 共用 Event sequence

- 类型：假设构造
- 触发条件：Application component 缓存 sequence counter。
- 故障表现：Run B 从 Run A 的序号继续或发生冲突。
- 根因分析：sequence owner 被错放到 Application scope。
- 修复方案：每 Run 由 RuntimeEventChannel 独立分配 sequence。
- 回归测试：`test_concurrent_10_run_baseline_has_independent_sequences_and_zero_owners`。
- 对应知识点：并发正确性需要明确的 identity scope。
- 面试表达：10 个并发 Run 都有独立 run_id 和从 1 开始的 sequence。
- 当前状态：未在真实代码复现，已防护

### Bad Case 6：Model retry 被统计成跨 Runtime fallback

- 类型：假设构造
- 触发条件：指标只看二次 provider call，不看 runtime/candidate/attempt identity。
- 故障表现：正常 retry 被误报为 Legacy fallback。
- 根因分析：混淆 Runtime selection、candidate fallback 和 attempt retry。
- 修复方案：按 owner 和 identity 分层统计。
- 回归测试：RC-02/03/20。
- 对应知识点：Retry 与 fallback 必须在路由层级上区分。
- 面试表达：我用 runtime mode、candidate id 和 attempt id 区分三类路径。
- 当前状态：已防护

### Bad Case 7：Tool committed failure 被自动 Retry

- 类型：假设构造
- 触发条件：非幂等 Tool 已 COMMITTED 后返回失败。
- 故障表现：副作用执行两次。
- 根因分析：Retry policy 忽略副作用证据。
- 修复方案：COMMITTED+non-idempotent 保守为 UNSAFE/manual，不 retry/不自动补偿。
- 回归测试：RC-08。
- 对应知识点：重试安全性取决于幂等和 commit boundary。
- 面试表达：我把副作用证据作为 retry 的硬前置。
- 当前状态：已防护

### Bad Case 8：Client disconnect 后继续输出错误

- 类型：假设构造
- 触发条件：transport 已释放后业务 finally 追加 SAFE_ERROR。
- 故障表现：断开后仍尝试写 wire，并可能覆盖取消原因。
- 根因分析：传输所有权与 Runtime terminal 所有权未分离。
- 修复方案：disconnect 后 cancel-and-drain/abort，禁止新 wire output。
- 回归测试：RC-13。
- 对应知识点：Transport lifecycle 与业务 lifecycle 不等价。
- 面试表达：我保留后台 worker 真值，但断开后不再写输出。
- 当前状态：已防护

### Bad Case 9：Recovery validation 被写成 automatic recovery

- 类型：假设构造
- 触发条件：文档把 `RESUMABLE` assessment 解释为已会自动 resume。
- 故障表现：读者误以为 Runtime 会 replay adapter 或重建 result。
- 根因分析：混淆诊断结果与执行能力。
- 修复方案：始终使用 `Recovery validation only`，明示 replay flags=false。
- 回归测试：RC-15/16 与 capability negative tests。
- 对应知识点：Assessment 不是 command。
- 面试表达：当前只能判断是否可恢复，不会自动恢复。
- 当前状态：已防护

### Bad Case 10：Detached Worker 被当作普通泄漏清空

- 类型：假设构造
- 触发条件：shutdown 超时后直接删除 worker record。
- 故障表现：计数归零但线程仍在运行，随后错误关闭 model。
- 根因分析：把观测记录当成可强制终止的 worker。
- 修复方案：保留 DETACHED snapshot，defer model close，`fully_closed=false`。
- 回归测试：RC-18。
- 对应知识点：Python 同步线程不能被安全强杀。
- 面试表达：我优先保持 worker 真值，不用清记录伪造关闭成功。
- 当前状态：已防护

### Bad Case 11：Shutdown orchestration completed 被当作 fully closed

- 类型：假设构造
- 触发条件：只检查兼容字段 `completed`。
- 故障表现：编排已走完但仍有 deferred/failed resource 时被误报成功。
- 根因分析：混淆流程完成和资源关闭事实。
- 修复方案：Gate 必须检查 `fully_closed`、remaining 和 worker snapshot。
- 回归测试：RC-17/18。
- 对应知识点：Process outcome 与 resource outcome 需独立建模。
- 面试表达：`orchestration_completed` 只表示流程走完，是否关净要看 `fully_closed`。
- 当前状态：已防护

### Bad Case 12：Legacy 被当成 Coordinated 等价实现

- 类型：假设构造
- 触发条件：因 Legacy 可运行就宣称拥有完整 Journal/Snapshot/Recovery。
- 故障表现：回滚路径能力被夸大，发布预期失真。
- 根因分析：把兼容性与能力对等混为一谈。
- 修复方案：独立列出 Legacy 能力边界，仅显式选择。
- 回归测试：RC-19 与 legacy boundary tests。
- 对应知识点：Rollback path 的价值是兼容，不是架构对等。
- 面试表达：Legacy 是显式回滚入口，不假装拥有 Coordinated 的完整证据链。
- 当前状态：已防护

### Bad Case 13：性能基线使用真实外部服务

- 类型：假设构造
- 触发条件：基线直连真实 Model/Tool/DB/网络。
- 故障表现：数据受网络、限流、账号与成本影响，无法复现。
- 根因分析：把端到端真实性误解为必须调外部系统。
- 修复方案：只使用 offline Fake，耗时仅作本机趋势。
- 回归测试：`test_runtime_resource_baseline.py`。
- 对应知识点：可复现基线与生产压测是不同层次。
- 面试表达：RC Gate 先锁 owner 不变量，真实容量规划留给受控环境。
- 当前状态：已防护

### Bad Case 14：预期 Journal/Snapshot 增长被误判为内存泄漏

- 类型：假设构造
- 触发条件：tracemalloc 显示 retained bytes 增长，但未区分持久记录与 live owner。
- 故障表现：合理的 Journal/Snapshot 保留被当成 blocker。
- 根因分析：仅看字节趋势，不看对象语义。
- 修复方案：内存趋势只记录，硬 Gate 使用 owner 计数和 worker 真值。
- 回归测试：`test_tracemalloc_warmup_and_repeated_batches_report_trend_without_sla`。
- 对应知识点：Retained memory 不自动等于 leak。
- 面试表达：我先识别 owner 是否应该存活，再解读内存趋势。
- 当前状态：已防护

### Bad Case 15：Module-level compatibility handle 保留已关闭对象

- 类型：真实发现
- 触发条件：lifespan 退出后模块全局已清空，但 `app.state.runtime_services` 仍保留已关闭对象。
- 故障表现：下次测试或诊断可能读到过期 Application handle。
- 根因分析：两套兼容句柄发布了同一对象，但只清理了一侧。
- 修复方案：启动时同步发布 chat/runtime services，关闭时 module global 与 `app.state` 同步置 `None`。
- 回归测试：`test_server_compatibility_handles.py`。
- 对应知识点：兼容句柄必须共享同一 lifecycle truth。
- 面试表达：我保留兼容 API，但让两个发布面同步指向和同步失效。
- 当前状态：已修复

### Bad Case 16：Release Gate Report 保存路径或原始错误

- 类型：假设构造
- 触发条件：将 pytest path、provider exception 或业务文本直接放入 report。
- 故障表现：报告泄露本地路径或敏感错误，且变得不稳定。
- 根因分析：没有区分证据计算过程与安全派生结果。
- 修复方案：Assessment 仅允许固定 finding ID、计数和布尔值。
- 回归测试：`test_gate_rejects_paths_or_raw_errors_as_finding_ids`。
- 对应知识点：Derived report 需要 allowlist schema。
- 面试表达：原始证据参与计算，但发布报告只携带固定安全 ID。
- 当前状态：已防护

## 24. 测试结果

- 新增：8 个指定 RC 测试文件，另有 2 个 test helper。
- RC 专项：20 passed。
- 第一轮契约：26 passed。
- 关键回归：49 passed。
- 全仓：1059 passed，42 subtests passed。
- compileall：通过；`uv lock --check`：通过；`git diff --check`：通过（仅工作区 LF/CRLF 提示）；敏感 marker 扫描：通过。

## 25. 未完成事项

RC 明确不完成 automatic recovery/replay/resume、result rehydration、random chaos、production fault activation、cross-process registry、exactly-once、automatic compensation 和 distributed durable execution。

## 26. 第三轮 Operations 接入点

建议第三轮将 ReleaseGateAssessment 的固定 ID/计数接入离线 CI artifact，接入 resource owner gauge 与 shutdown truth，但不让报告成为 Runtime owner，不开放生产 Fault 入口。

## 27. 需要带回 ChatGPT 审查的信息

- First-round contract fixes：2 项完成。
- RC identifier：`Stage2 Runtime RC1`。
- RC scope：如第 3 节。
- Release gate：PASS；P0=0，P1=0，P2=1，Known limitations=7。
- Required/Passed/Failed scenarios：20/20/0。
- Coordinated normal、Model retry/fallback、Retrieval degradation/failure、Tool success/retry/committed failure、Parallel best-effort/fail-fast：全部通过。
- Budget、Deadline、Cancellation、Client disconnect：全部通过。
- Snapshot / Recovery validation：通过，仍为 opt-in/validation-only。
- Clean / Degraded shutdown：通过，worker truth 保真。
- Legacy rollback：通过；Cross-runtime fallback：不存在。
- Sequential baseline：50 runs，0.088393 s total，avg 1.768 ms，median 1.744 ms，P95 2.181 ms。
- Concurrent baseline：10/10，10 unique run ids；Memory baseline：+185438/+107334 bytes，仅趋势。
- Registry/Reservation/Permit/Span cleanup：均为 0。
- Compatibility handles：保留，已同步发布与清空。
- Sensitive data scan：通过。
- 新增测试、pytest、subtests、compileall、lock check、diff check：见第 24 节最终数据。
- 需要人工确认的问题：无发布阻塞；第三轮是否把 Gate 安全摘要接入 CI artifact，但不扩展为生产控制面。
