# 阶段二第 25 天第三轮：Runtime Operations

## 1. 本轮目标

把 Stage2 Runtime RC1 的真实代码能力转化为可执行、不过度承诺的 Operations、Runbook、Configuration、Error Catalog、Recovery/Security Boundary 与 Release Checklist；不新增 Runtime 架构能力。

## 2. 第二轮文档修正

1. 性能数字改名为 **Runtime orchestration overhead baseline / 运行时编排开销基线**，明确 Offline Fake、不含外部 I/O、不是生产 SLA，硬 Gate 是 Owner/资源事实。
2. RC Matrix 新增合法 `test_level`：API_E2E=4、RUNTIME_E2E=3、SUBSYSTEM_INTEGRATION=13、CONTRACT=0。
3. Release Gate 明确 PASS 仅表示 `Stage2 Runtime RC1 code-level gate passed`，不代表容量、外部依赖、跨进程容灾、Soak、渗透测试或无条件发布完成。

## 3. 配置入口审计

唯一项目配置入口为 `core.settings.Settings.load()`；仓库无独立 `.env.example`。审计覆盖 Settings、server/lifespan、Runtime mode、本地/远端 Model、HTTP timeout/TLS、模型成本/circuit、Memory/KB/RAG、Journal/Snapshot、Observability、shutdown、host/port。Parallel policy 固定为 factory `max_concurrency=1`，Blocking executor 默认 4 workers/8 pending，均无 Settings 环境变量。

`requests.Session` 未由项目提供 trust-environment/proxy 开关；库的环境代理行为未被覆盖，需由部署环境审计。生产 Fault 配置入口不存在。

## 4. Configuration Reference

`docs/runtime/runtime_configuration_reference.md` 逐项记录真实 name、owner、type、default、allowed values、required、scope、restart、security、failure behavior 与安全示例。所有环境配置均在进程启动时读取，修改需要重启；不存在运行中动态重载。

## 5. Startup Runbook

按配置校验→路径/权限→lifespan→ApplicationRuntimeServices→Admission ACCEPTING→Offline smoke→Journal/Observability/Trace health→默认 COORDINATED 的顺序执行。启动失败不跨 Runtime 重跑同一请求。

## 6. Shutdown Runbook

记录真实顺序：SHUTTING_DOWN/DRAINING→lease settle→run cancel/drain/force abort→worker admission/drain→Observability/Trace flush→component/Snapshot/Journal close→Model safety gate→CLOSED。判断同时使用 orchestration、fully closed、failure/deferred 与 active/detached/unknown worker 真值。

## 7. Error Code Catalog

目录收录 41 个真实枚举/固定字符串，覆盖 Runtime、Admission、Model、Retrieval、Tool、Budget、Publication、Journal、Snapshot、Recovery、Observability、Trace、Shutdown、Worker 与 Legacy compatibility。配置域当前没有统一固定 safe code，因此明确记录该缺口而不发明代码。

## 8. Health / Metrics / Trace

Observability Health 使用真实 drop/logger/metrics/worker/duplicate/record/flush counters、status 与 last safe code；Trace 使用 active/completed/dropped、start/end/flush failures、status 与 last safe code。Runtime gauge 仅列出代码已有 8 个名称；reservation/permit/watcher/producer/channel ownership 仍通过 owner snapshot/专项测试验证。当前是进程内能力，不等于 Prometheus/Grafana。

## 9. 常见故障处置

Operations Runbook 使用统一十字段模板覆盖：请求 timeout、Client Disconnect、Journal partial publication、Snapshot partial save、Recovery failure/corruption、Tool completion gap、Observability/Trace degraded、Detached Worker、Shutdown partial failure、Runtime configuration error。每项明确禁止操作、人工升级条件与真实测试。

## 10. Snapshot / Recovery Runbook

当前严格为 Recovery validation only。Snapshot 检查 schema/digest/run identity/watermark/state/budget/activity；Journal tail 检查 sequence/digest/terminal/started-completed/schema/gap/corruption。不得自动 Resume/Replay、升级写回或把损坏 tail 当空 tail。

## 11. Tool Manual Reconciliation

对 `TOOL_STARTED` 无 `TOOL_COMPLETED` 执行八步流程：停止自动动作、保留原件、分类幂等性、检查持久证据、外部权威人工确认、记录 NOT_STARTED/COMMITTED/UNKNOWN、授权人员决定新身份流程、Validator 保持只读。禁止自动调用 Tool/compensation。

## 12. Legacy Rollback

Legacy 是新请求开始前修改真实 `CHAT_RUNTIME_MODE`、重启和 smoke test 的显式兼容路径。不得对已开始/失败的 Coordinated Run 动态 fallback，不复用原 Context/State，不宣称拥有完整 Journal/Snapshot/Recovery，也不能绕过 Budget/Cancellation/安全策略。

## 13. Security Boundary

区分 Prompt、output、Tool/RAG/Memory/path/provider/key/resource 等敏感数据与 digest/enum/safe code/count/status/timestamp/schema 安全事实。Runtime projection 禁止复制正文，但正常聊天 Wire 与 MemoryManager/KB 的独立业务正文路径真实存在，未作“所有正文绝不持久化”的虚假承诺。

## 14. Release Checklist

Checklist 分 Pre-release、Startup、Runtime、Shutdown、Rollback，所有项目引用真实测试、RC ID 或文档章节。它是 code-level evidence checklist，不是纯人工勾选，也不替代生产验证。

## 15. CI Artifact Boundary

本轮未连接真实 CI。允许未来离线输出 12 个固定安全字段：RC id、P0/P1 count、P2/KL ids、required/passed counts、五个检查布尔与 status。禁止路径、原始异常、正文、Rule ID、Provider 配置和秘密；Artifact 是 Derived Report，不是控制面。

## 16. Runtime 真实接入

文档连接真实 `Settings.load()`、FastAPI lifespan、ApplicationRuntimeServices、SQLite Journal/Snapshot、dispatcher/span health、gauge provider、GracefulShutdownCoordinator、RecoveryValidator 与 test manifest。未创建 dashboard、exporter、自动 recovery 或生产 Fault API。

## 17. Legacy Boundary

默认 COORDINATED；LEGACY 仅请求前显式选择，失败后无跨 Runtime fallback。两条路径共享 Application resource close-once，但 Legacy 能力不与 Coordinated 等价。

## 18. Bad Case

### Bad Case 1：离线 Fake 延迟被写成生产 P95

- 类型：真实发现
- 触发条件：第二轮资源文档展示毫秒 P95，但标题仅称 Resource Baseline。
- 故障表现：读者可能把编排开销误解为真实 LLM/Tool 生产延迟。
- 根因分析：基线命名未显式限定测量边界。
- 修复方案：改名 Runtime orchestration overhead baseline，并列出全部未包含外部依赖。
- 回归测试：`test_baseline_and_gate_scope_cannot_be_read_as_production_validation`。
- 对应知识点：基线名称必须包含 workload 与排除项。
- 面试表达：我测的是离线编排开销，不是端到端生产 P95。
- 当前状态：已修复

### Bad Case 2：20 个 RC 场景被全部称为 API E2E

- 类型：假设构造
- 触发条件：场景矩阵没有 test level，汇报时统一使用“端到端”。
- 故障表现：Subsystem/Runtime 测试被夸大为 HTTP API E2E。
- 根因分析：缺少标准测试层级字段。
- 修复方案：新增四值 test_level 并按真实节点分为 4/3/13/0。
- 回归测试：`test_rc_matrix_has_exact_legal_test_levels_and_truthful_counts`。
- 对应知识点：测试可信度来自入口层级，不只来自场景名称。
- 面试表达：20 个 RC 场景都有真实链路，但只有 4 个是 API E2E。
- 当前状态：已防护

### Bad Case 3：Release Gate PASS 被理解成容量和容灾全部完成

- 类型：真实发现
- 触发条件：第二轮 Gate 未设置独立 Scope/Assumptions/Out-of-scope 章节。
- 故障表现：code-level PASS 可能被当成无条件生产发布授权。
- 根因分析：Gate 证据范围与发布决策范围没有显式分离。
- 修复方案：增加三个固定章节并逐项排除容量、外部依赖、容灾、Soak 与渗透测试。
- 回归测试：`test_baseline_and_gate_scope_cannot_be_read_as_production_validation`。
- 对应知识点：Gate PASS 只对其证据域有效。
- 面试表达：RC1 通过代码级 Gate，不等于所有生产验证完成。
- 当前状态：已修复

### Bad Case 4：配置文档写入不存在的环境变量

- 类型：假设构造
- 触发条件：凭经验补写 parallel、worker 或 Fault 环境变量。
- 故障表现：运维设置无效配置并错误相信行为已改变。
- 根因分析：文档没有以 Settings 源码为 allowlist。
- 修复方案：表格配置名必须是 `Settings.load()` 字面量；无入口的能力写成代码固定事实。
- 回归测试：`test_every_documented_configuration_name_comes_from_real_settings`。
- 对应知识点：Configuration reference 应可由源码反向验证。
- 面试表达：我没有为尚未配置化的能力虚构环境变量。
- 当前状态：已防护

### Bad Case 5：错误目录把所有错误都建议重试

- 类型：假设构造
- 触发条件：operator action 统一写“重试请求”。
- 故障表现：Committed Tool、partial publication、corruption 可能被重复执行或覆盖。
- 根因分析：忽略 retry policy、side effect 与 evidence 分类。
- 修复方案：每码分别记录 retry/side-effect 语义，并定义六类操作语义。
- 回归测试：`test_catalog_covers_critical_domains_without_universal_retry_advice`。
- 对应知识点：可重试性不是错误本身的单一属性。
- 面试表达：错误目录同时回答是否重试和副作用是否已发生。
- 当前状态：已防护

### Bad Case 6：Journal 部分发布后建议重跑业务

- 类型：假设构造
- 触发条件：Journal 已 append、Channel 未投递。
- 故障表现：Model/Tool 被重做，sequence 或 terminal 重复。
- 根因分析：把 transport 可见性当成业务执行权威。
- 修复方案：以 Journal 为 committed authority，标记 Partial Publication，禁止重做业务。
- 回归测试：`test_event_journal_integration.py::test_channel_failure_does_not_repeat_committed_business_work`。
- 对应知识点：Journal-first 将持久事实与传输结果分离。
- 面试表达：通道失败不回滚已提交 Journal，也不触发业务重跑。
- 当前状态：已防护

### Bad Case 7：Snapshot 部分保存后建议自动再保存

- 类型：假设构造
- 触发条件：after-save 故障且 evidence 显示 partially persisted。
- 故障表现：同一 snapshot 被覆盖、冲突或误删。
- 根因分析：忽略 `persisted/retry_allowed` 证据。
- 修复方案：保留原件、禁止自动重存，进入只读 validation。
- 回归测试：`test_snapshot_partial_persistence.py::test_after_save_delay_cancellation_never_deletes_committed_snapshot`。
- 对应知识点：持久化调用失败不等于没有提交。
- 面试表达：save 后窗口必须依据 publication evidence，而非异常类型决定重试。
- 当前状态：已防护

### Bad Case 8：Recovery Runbook 使用当前 Registry 回填历史

- 类型：假设构造
- 触发条件：历史 evidence 缺失时读取 live RunRegistry。
- 故障表现：当前进程状态被伪装成历史持久事实。
- 根因分析：混淆权威持久输入与运行中诊断信息。
- 修复方案：Recovery 权威输入仅 Snapshot + Journal，禁止 live backfill。
- 回归测试：`test_recovery_runbook_is_validation_only_and_uses_persisted_authority`。
- 对应知识点：恢复验证需要时间一致的证据边界。
- 面试表达：live Registry 只能诊断当前状态，不能改写历史。
- 当前状态：已防护

### Bad Case 9：Tool Started 无 Completed 时自动调用 Tool

- 类型：假设构造
- 触发条件：completion gap 被误判为“没有执行”。
- 故障表现：非幂等副作用可能执行两次。
- 根因分析：缺失 completion 不等于 NOT_STARTED。
- 修复方案：停止自动动作，执行八步人工对账并保留 UNKNOWN。
- 回归测试：`test_tool_manual_reconciliation_has_all_eight_safe_steps`。
- 对应知识点：不完整 evidence 的默认结论应保守。
- 面试表达：Started 无 Completed 是 reconciliation 问题，不是自动 retry 信号。
- 当前状态：已防护

### Bad Case 10：Observability 缺失被解释为业务没执行

- 类型：假设构造
- 触发条件：日志或 Trace 缺少 completion record。
- 故障表现：运维重跑实际已经执行的业务。
- 根因分析：把 Derived diagnostic 当成业务 authority。
- 修复方案：业务事实读 AgentState/Journal，diagnostic health 单独标 DEGRADED。
- 回归测试：`test_runbook_uses_real_health_fields_and_safe_shutdown_truth`。
- 对应知识点：可观测性失败与业务失败正交。
- 面试表达：没有 Span 不代表没有副作用，必须回到权威证据。
- 当前状态：已防护

### Bad Case 11：Detached Worker 被建议清空记录

- 类型：假设构造
- 触发条件：shutdown 后 worker count 非零。
- 故障表现：计数看似归零但线程仍运行，Model 被提前关闭。
- 根因分析：把记录删除误当成 worker 终止。
- 修复方案：保留 snapshot、defer Model close、等待真实 callback。
- 回归测试：`test_runbook_uses_real_health_fields_and_safe_shutdown_truth`、RC-18。
- 对应知识点：观测记录必须保持资源真值。
- 面试表达：Detached 是真实生命周期状态，不能靠删记录修复。
- 当前状态：已防护

### Bad Case 12：Shutdown 只检查 completed

- 类型：假设构造
- 触发条件：只读取兼容字段 `completed=true`。
- 故障表现：deferred、failure、remaining 或 unknown 被忽略。
- 根因分析：混淆 orchestration completion 与 resource closure。
- 修复方案：同时检查 fully_closed、failures、deferred 与 worker counts。
- 回归测试：`test_checklist_requires_fully_closed_and_forbids_cross_runtime_rerun`。
- 对应知识点：关闭报告必须区分流程结果和资源结果。
- 面试表达：completed 只说明编排走完，fully_closed 才说明关键资源关净。
- 当前状态：已防护

### Bad Case 13：Legacy 回滚被写成失败后的动态 fallback

- 类型：假设构造
- 触发条件：Coordinated 请求失败后复用输入立即调用 Legacy。
- 故障表现：业务或非幂等副作用被重复执行。
- 根因分析：把部署级回滚与请求级 fallback 混淆。
- 修复方案：只允许请求前配置、重启、新身份 smoke/new request。
- 回归测试：`test_checklist_requires_fully_closed_and_forbids_cross_runtime_rerun`、RC-19/20。
- 对应知识点：Rollback 与 runtime fallback 的时间边界不同。
- 面试表达：Legacy 是部署兼容开关，不是失败请求的第二次执行器。
- 当前状态：已防护

### Bad Case 14：Security 文档声称所有业务正文绝不持久化

- 类型：假设构造
- 触发条件：把 Runtime projection allowlist 推广到整个产品。
- 故障表现：与 MemoryManager/KB 的独立业务持久化事实冲突。
- 根因分析：没有区分 Runtime evidence 面与业务数据面。
- 修复方案：明确正常 Wire、Memory、KB 的独立 owner，同时禁止复制到 Runtime diagnostic surfaces。
- 回归测试：`test_security_boundary_distinguishes_runtime_projection_from_business_storage`。
- 对应知识点：安全承诺必须标明数据面和输出面。
- 面试表达：Runtime Journal 不存正文，不代表产品没有受控业务持久化。
- 当前状态：已防护

### Bad Case 15：CI Artifact 输出路径或原始错误

- 类型：假设构造
- 触发条件：直接序列化 pytest/report exception。
- 故障表现：CI artifact 泄露路径、Provider 配置或业务信息。
- 根因分析：缺少派生报告字段 allowlist。
- 修复方案：只允许 12 个固定字段，错误只输出固定 ID/count/status。
- 回归测试：`test_ci_artifact_allowlist_excludes_paths_errors_and_fault_control`。
- 对应知识点：CI 产物也是安全输出面。
- 面试表达：原始证据参与 Gate 计算，但 artifact 只发布安全摘要。
- 当前状态：已防护

### Bad Case 16：Runbook 建议强杀同步线程

- 类型：假设构造
- 触发条件：Python/C Extension worker 超过 drain timeout。
- 故障表现：进程/共享 client 状态损坏，副作用结果未知。
- 根因分析：误以为同步线程可被安全异步取消。
- 修复方案：bounded wait、记录 detached、关闭 admission、defer Model，人工升级。
- 回归测试：`test_runbook_uses_real_health_fields_and_safe_shutdown_truth`。
- 对应知识点：线程取消是协作式边界，不是强制终止保证。
- 面试表达：不能安全强杀的 worker 必须保真、隔离和延迟资源关闭。
- 当前状态：已防护

## 19. 测试结果

- 新增文档真实性测试：6 个文件，15 passed。
- 前两轮组合回归：46 passed。
- 关键回归：48 passed。
- 全仓：1074 passed，42 subtests passed。
- compileall、`uv lock --check`、`git diff --check`、Operations 正式文档敏感 marker 扫描：全部通过；diff 仅有工作区 LF/CRLF 提示。

## 20. 未完成事项

未实现自动 Recovery/Replay/Resume、生产 Fault 激活、随机 Chaos、Exactly-once、自动补偿、真实外部依赖验证、容量/Soak/渗透测试、Prometheus/Grafana exporter 或真实 CI 接入。

## 21. 第四轮最终验收接入点

第四轮可汇总三轮 code-level evidence、Operations 文档真实性、Release Gate 安全摘要与 Known Limitations，生成最终验收文档；不得把未完成生产验证提升为 PASS。

## 22. 需要带回 ChatGPT 审查的信息

- Baseline naming：Runtime orchestration overhead baseline / 运行时编排开销基线。
- RC test levels：API 4、Runtime 3、Subsystem 13、Contract 0。
- Release gate scope：Stage2 Runtime RC1 code-level gate。
- Configuration source：`Settings.load()`，无独立 `.env.example`。
- Runtime mode：默认 COORDINATED；LEGACY 请求前配置并重启。
- Snapshot：默认关闭、SQLite opt-in、schema v1、fail closed。
- Journal：SQLite、schema v2 reader v1/v2、append-only。
- Observability/Trace：进程内 health/recording，不等于外部 exporter。
- Shutdown：grace/component timeout、worker drain、Model safety gate、truthful report。
- Fault production configuration：无，controller=None。
- Startup/Shutdown checks：见 Operations Runbook。
- Error code count：41。
- Retryable categories：仅 policy+budget+deadline+idempotency 共同允许的 provider failure。
- Manual reconciliation：Tool outcome unknown/insufficient、partial persistence、corruption/unknown worker。
- Recovery authority：Snapshot + Journal；Automatic recovery：无。
- Legacy rollback：配置、重启、新请求；Cross-runtime retry：禁止。
- Security allowed facts：digest/enum/safe code/count/status/time/schema；禁止正文、路径、secret、raw error/key。
- Release checklist：五阶段真实 evidence 引用。
- CI artifact：仅安全 allowlist，本轮未接 CI。
- 测试与人工确认：见第 19 节最终数据；人工仍需决定第四轮后是否启动独立生产验证计划。
