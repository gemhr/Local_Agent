# Stage20 首次双仓执行架构审计与初始决策

日期：2026-11-05（Asia/Shanghai）。角色：Codex-A，独立只读审计。**本报告不是 Stage20 Final Gate，不授权自动进入实现。**

结论：推荐 **A：保留现有 Agent Loop，先验证并利用已有局部并发能力**。确认存在整批调度屏障、同步 Step 占用线程、CI 取证串行配置与同 Run 无独立跨 Worker Claim 的结构限制，但没有当前运行数据证明这些已经形成生产吞吐瓶颈。现有 Loop 已能持久挂起、释放执行资源、跨进程接管；不需要为此重新建设事件平台。首次切片应是两个独立 CI 证据读取的受控并发，而非拆分 Run Authority。

证据标记：**S**＝当前源码直接证明；**T**＝阅读了当前测试断言，未执行；**H**＝历史报告宣称；**I**＝静态推断，待测量。本文所有测试运行状态均为 **NOT_EXECUTED**；没有使用历史 PASS 代替本轮结果。路径前缀 LocalAgent/、AgentEvalOps/ 分别对应下述绝对根目录，行号基于本次工作树。

## A. Repository Baseline

| 项目 | LocalAgent / AgentCore | AgentEvalOps |
|---|---|---|
| 实际路径 | `D:\PythonProject\Local_Agent` | `D:\PythonProject\AgentEvalOps` |
| Branch | `main` | `stage12-go-productionization` |
| HEAD | `f2d630091c942b1e57b0d4c14c1e0b6e8208b894` | `0fab7fbcfc8475992f5979a892193d13c6118fe0` |
| 已跟踪未提交修改 | unstaged / staged 均无 | unstaged / staged 均无 |
| 未跟踪 | `data/` 下 12 个 Stage8 execution_lists `.xls` | 无 |
| 主要目录 | `server.py`、`core/runtime`、`core/agent_platform`、`core/incidents`、`core/persistence`、`alembic`、`tests` | `go-backend/cmd`、`internal/bootstrap`、`internal/worker`、`internal/postgres`、`internal/evaluation`、`frontend`、`backend/migrations` |
| 依赖事实 | `pyproject.toml:5` Python >=3.12,<3.13；FastAPI/HTTPX/SQLAlchemy 路径 | `go-backend/go.mod:3` Go 1.26.0，pgx/v5 v5.7.6；HTTP API/独立 Worker |

第二仓通过枚举 `D:\PythonProject` 后核对 AGENTS、Git、Go 入口定位，不以相邻 `AgentEvalOps_OperatorOnly` 或历史 PandaProbe 替代。未读取 operator-only/Holdout、`.env`、业务数据库或 data 正文。LocalAgent 未跟踪文件均为 `data/stage8/execution_lists/` 下 `m-wp3*`、`m-wp9*` 工件，不是本轮源码差异；保留原状。

只读命令包括 `git status --short`、`git branch --show-current`、`git rev-parse HEAD`、`git diff --stat/--name-only`、cached 对应命令、`git log -6 --oneline`、`git show --stat HEAD`、`git ls-files --others --exclude-standard`、`rg`、UTF-8 文件读取。Git 查询成功；个别按猜测路径的搜索返回不存在，已通过目录/文件搜索定位实际入口，不作为缺失能力证据。无测试、依赖安装、服务启动、迁移、模型调用、真实 CI 调用或 Git 修改。

### Stage19 当前事实

- LocalAgent HEAD 已包含 CI 最终输出/Step 终态修复及标准模型准入恢复；前序 `92e11f5`、`135b769` 包含自适应评测与查询/调查分流。AgentEvalOps HEAD `0fab7fb` 与前序 `452fcb4` 包含自适应跨仓评测与 Provider usage 校验。**本次读到的正式实现均在 HEAD，不是未提交候选。**
- 当前源码搜索范围为 `core/`、根 `server.py`、`scripts/`、`tests/`、`docs/runtime/`。实验特批标识只在 `tests/test_ci_json_output_and_step_terminal.py:193` 的拒绝旧字段/旧协议断言中出现。没有发现当前生产实验准入入口。`request_counting_admission.py:295` 后仍有受控旧 SERVICE 合同，不能将“实验特批已撤销”扩大为“一切既有受控路径都删除”。
- H：`.ai/handoff/stage19_adaptive_execution/240_codex_experimental_admission_rollback.md` 记录撤销实验豁免；`230_solB_d1_final_live_pilot.md` 记录两组业务均 REJECTED、Quality Gate BLOCKED、真实收益 NOT_DEMONSTRATED、Stage19 Final Gate PENDING。本文只把它们视为历史记录，没有重跑其 runner。
- Stage18 Final Gate 按任务及上述历史记录保持 BLOCKED。Stage19 的真实模型效果不能作为本阶段成功基线；旧实验 runner 依赖已撤销入口，不属于当前可直接复用执行链。
- 本轮没有能够支持生产 P50/P95、吞吐、DB 压力或真实容量的观测数据。没有检查实际进程环境，因此配置默认值不等于部署值。

## B. Current Execution Architecture

### B1. 从入口到终态的真实链路

CI 主路径（S）：

```text
server.py::lifespan（622；1493；1660）装配数据库、Tool Runtime、Factory、Supervisor、CI 服务
→ /api/ci/scenarios/{instance_id}/executions（core/incidents/http.py:39）生成模拟执行及故障 Incident
→ /api/ci/incidents/{incident_id}/investigations（http.py:58）
→ CIInvestigationService.start（core/agent_platform/ci_diagnosis.py:105）
→ Supervisor.acquire_slot → incidents.admit → 绑定 RUN/CONVERSATION → Supervisor.spawn
→ AgentApplicationService.stream（application.py:161）
→ ChatService / create_run_scope → _consume_scope_events（chat_service.py:514）
→ CoordinatedRunScope.execute（runtime/runtime_factory.py:430）
→ RunCoordinator.execute → _execute_batches（run_coordinator.py:1548、1864）
→ SerialScheduler.claim_ready → ParallelExecutor.execute_ready/execute
→ durable-agent-loop.v2 Driver.execute（agent_loop/driver_v2.py:20）
→ Model → accept_loop_batch → Tool/Child → commit_batch → 下一轮 Model
→ StepCompletion / OutputGate → 原子 Root/Journal/RunControl 终态
→ CIInvestigationService.project_if_terminal → validate_report → incidents.seal
```

通用 Run HTTP 入口同样通过 Supervisor，在 `server.py:3110` 先申请 Slot，`3132` 派发消费流。`ChatService` 的 producer Task 与 SSE/事件消费并非执行权威；客户端连接不等于 Run 本身。原子终态在 `core/persistence/repositories/execution.py:621`：当前 fence 校验、Root 状态、Journal `RUN_COMPLETED`、Client Feed 投影及 RunControl close 位于同一事务；提交后通知投影读者。`OutputGate.attempt_publish`（`core/runtime/output_gate.py:344`）保留交付授权与唯一输出职责。Runtime SUCCEEDED 与 CI 业务报告有效是两层判断，不能互相代替。

Mission 由 `core/stage8/service.py:49` 起独立版本/CAS 状态迁移，`65` 的 `attach_run_reference` 仅登记引用及用途，不能从 Mission 活跃推断有对应执行线程。Workflow 是 Registry/Definition 的执行绑定，`application.py:133` 校验期望版本，`runtime_factory.py:865` 恢复时核对冻结 Workflow 身份；不是另一套 Durable Worker。CI 调查是一个顶层 AGENT Step 的 v2 Loop，并非自动展开为 Mission DAG。

### B2. 资源占用必须分别看

| 对象 | 当前行为与证据 | 判断 |
|---|---|---|
| 数据库活跃 Run | 挂起时 Root ACTIVE、Step 可保持 RUNNING，保存等待原因/next_recovery_at | 不要求始终有本地 Task |
| asyncio Task | 正常执行期间 scope/producer/control Task 存活；LoopSuspended 沿异常返回并关闭 scope | await 等待不等于占用 event-loop CPU |
| Python 工作线程 | Factory 明确 `SYNC_BLOCKING`（runtime_factory.py:446）；ParallelExecutor `692`–`724` 把整个 driver.execute 放进 blocking executor | 整个 Step 未返回前占一条执行线程，含模型/工具等待 |
| 同步桥 | `event_emitter.py:144`–`170` 用 run_coroutine_threadsafe + future.result；`model_invocation.py:1143`–`1157` 将模型异步调用交回 Loop | AsyncHTTP I/O 让出事件循环，但同步调用方线程仍等候 |
| Run admission Slot | `server.py:311` 获取，`336`–`346` 直到流结束才释放 | 正常外部等待仍占 Slot；挂起退出后释放 |
| Step/Tool Slot | `parallel_execution.py:295` 许可覆盖 invoke；ToolConcurrency 有独立全局/每工具/资源许可（tool_concurrency.py:79） | 等待正在执行的外部 I/O 通常仍计为在途容量，不能擅自释放并过量派发 |
| DB 连接 | Repository 以短 transaction/session 包围读写；Tool start 返回后才调用 Adapter（tool_execution.py:504–525） | 所审路径未把整个外部请求包进 DB 事务；不能声称所有 await 持连接 |
| 外部等待 | 原生模型 HTTP、同步 Tool bridge、审批/UNKNOWN/bounded polling 各不相同 | 前者可保留线程/Slot，后几类已有可持久挂起路径 |

`core/settings.py:915` 默认 blocking workers=4、max_active_runs=12、pending=8；`server.py:770、779` 创建分开的 blocking/Step executor。这里只是默认容量与结构，不是已观测饱和。`BlockingTaskHandle.result_async`（blocking_executor.py:104）不额外占等待线程，但不释放执行 driver 的线程。

**回答核心问题：正常执行的 Run 确实依赖存活的 Task，并可占用整个 Step 线程及 admission Slot；持久挂起的 Run 不需要。** 不能说“Python asyncio 已完全消除线程等待”，也不能说“每个 ACTIVE Run 都必须一直占线程”。

### B3. DAG、Multi-Tool Batch、Handoff

- `PlanStep.depends_on`（planning.py:69）表达 DAG；`scheduler.py:370` 只把所有前置 SUCCEEDED 的 PENDING Step 判为 ready；`claim_ready:188` 通过本地状态机领取。这不是 PostgreSQL 独立 Step Worker Claim。
- `ParallelExecutor.execute:191` 支持多个 Step、全局/资源许可和 FAIL_FAST/BEST_EFFORT，`470` TaskGroup 启动多个 worker。typed multi-step 在 `run_coordinator.py:1901` 为独立 Specialist 配置各自 resource key，避免默认共享许可串行。
- **DAG 整批屏障已确认**：Coordinator `1913`–`1928` await 整个 executor；Executor 等 TaskGroup 结束才返回。若 A/B 同批，C 只依赖已完成的 A，而 B 很慢，C 要等 B 才进入下一批。这是可构造的调度延迟，不是已测生产瓶颈，也不是忙轮询。
- v2 Batch（`agent_loop/batch.py:16、49、122`）：先冻结 recipes、action identity 与整批决定，后执行。只读 + spec.max_concurrency>=2 + 资源独立才允许两项重叠；每波最多两项，gather 等波结束。FAIL_FAST 控制未启动项，不能撤销已开始副作用；BEST_EFFORT 仍要求写工具的只读前置全部成功。
- `commit_batch`（repositories/agent_loop.py:181）按 ordinal 验证每个 COMMITTED 来源/NOT_EXECUTED 证明，整组一次提交 Observation 与 cursor。每个 Action 完成**不必**立即构建新模型上下文，反而不能提前提交残缺消息组。
- Parent/Child 在 `handoff.py:77、96`；Parent WAIT_CHILD，直接调用同一 driver 执行 Child，继承 Run lease/cancel、限定 deadline 和权限。Child 独立 Seed/Snapshot、禁 Memory/Formation、禁再次委派；ChildResult 不是根 Final。当前不是并行多 Child，也没有独立 Child Run/Worker。正式 `docs/runtime/agent_execution_v2.md` 限制深度1、累计2、同时1，与实现一致。

能力分层：单进程多 Run 并发＝SUPPORTED；单 Run 多 Step 异步重叠＝SUPPORTED（受整批屏障）；v2 多工具有限重叠＝SUPPORTED；多个独立 Specialist＝DAG 路径支持；v2 并行 Child＝NOT_IMPLEMENTED；Run 跨进程接管＝SUPPORTED（静态及测试源码证据）；同 Run 独立 Step/Action 跨 Worker 领取和动态分发＝NOT_IMPLEMENTED。

### B4. 持久化、恢复、关闭

- 生产装配 PostgreSQL Journal/Snapshot/Client Feed（server.py:1394 起）、DurableExecutionRepository（1493）、RunControl（719）。Snapshot/Journal 是历史投影和证据；当前执行恢复还依赖冻结 root/step/model/tool/frame/action/context 聚合，不能从摘要 Snapshot 单独重建所有正文。
- `run_control.py:58` claim 在锁定 RunControl 后续租或到期接管；`:280` takeover 增加 fencing_token。`:186` 的事务内 assert 检查 ACTIVE、owner/token/lease 并加行锁。续租/取消监听在 `runtime_factory.py:463`；`RunContext.raise_if_inactive:467` 检查 cancellation/deadline，恢复使用绝对 deadline（context.py:534），不会重置剩余时长。
- `RecoveryCoordinator.scan_once:80` → stale_run_ids → Run claim → prepare_recovery → `server.py:1514` rehydrate_and_execute → 原 Factory/scope.execute。**分发/接管单位是 Run lease；恢复复用的事实粒度细到 Model winner、Tool receipt、Action、ChildResult、ContextSnapshot。细粒度事实不意味着独立细粒度调度。**
- fixed-plan 恢复 `execution.py:321`：遗弃模型 STARTED→UNKNOWN；在途工具不确定或模型恢复耗尽则 Step BLOCKED，审批等待保留，其他可恢复工作回 PENDING。不是任意 Step 可无条件重放。
- v2 `recover_v2`（repositories/agent_loop.py:247、281）：同 fence 幂等，整棵受限 Frame 树一次递增 generation；遗弃模型 UNKNOWN、保守预算结算、同轮有限恢复；已接受 Batch/Child 不重新让模型决策。旧 fence/generation/row_version 拒绝迟到权威提交。
- `batch.py:56、100` 对 STARTED/UNKNOWN 挂起；bounded poll 提前执行在 `82` 挂起；Coordinator `1625` 持久 suspend；Factory `451` release lease；`stale_run_ids:183` 查询到期、控制意图、未决审批/工具等。`suspend_loop:1056` 默认回查时间约5秒，scan 默认 cadence 也是5秒，但实际恢复时延还受 lease、DB、队列和重建成本影响，不能把默认值作为 SLO。
- graceful shutdown：RecoveryCoordinator request_stop/stop 停止发现、bounded drain/cancel，Factory.close 释放 scope；server.py:1708 关闭 admission 后执行统一关闭。非协作阻塞线程无法被 asyncio cancel 强制停止，既有 detach/迟到 fence 仍须保留。
- **容量边界差异**：RecoveryCoordinator.scan_once 每次 batch_size 限制发现数量，但 `_local_tasks` 只有追踪、没有像 reconciliation lane 那样按存活数限制启动；恢复也不经过 HTTP Supervisor.acquire_slot。CONFIRMED 是“两条 admission 路径不同”，LIKELY 风险是恢复堆积造成队列压力；线程执行器仍有边界，不能称为无限线程。尚无运行证据证明故障风暴。

### B5. Tool Governance 与三阶段语义

Registry/ToolResolutionSnapshot 定义可用工具；真实执行仍通过 AgentRouter → ToolExecutionService，保留 schema/type、agent permission、risk/HITL、resource、budget、retry、timeout/cancel。`batch.py:18–41` 冻结 invocation/spec digest，执行时再次匹配；不是模型输出工具名就能执行。

| 阶段 | 现有权威 | 与副作用的关系 |
|---|---|---|
| 已批准 | DurableApprovalService.decide（durable_approval.py:100），APPROVED，绑定 invocation digest | 只是执行授权，不代表已派发 |
| 已领取/开始 | claim_execution（:138）独立 durable claim + Run fence；Tool PREPARED→STARTED（tool_idempotency.py:246） | 领取、开始仍不能推断外部提交 |
| 已确认 | Tool committed（:275）保存结果/digest；业务 Adapter 明确回执或 reconcile 权威来源 | 只读 COMMITTED 是确认结果，写操作必须依据副作用合同，不是任意 HTTP200 |

UNKNOWN（:308）与 NOT_COMMITTED/COMMITTED 区分；reconcile_durable_record（:412）在事务外查询 Provider、事务内以当前 fence 收口，未决保留 backoff/manual。`tool_execution.py:600` 保存结果、`:623` 起将 Journal/输出验证/Provider 不确定分支保守处理；不把失联当作未执行。RetryPolicy/RetryDisposition 与 BudgetLedger 是原 Owner。不存在需要新建 APPROVED→CLAIMED→COMMITTED 第二状态机的理由：当前已由审批行、claim 行和 invocation 行分工表达。

Tool resource lock 是进程内机制（tool_concurrency.py:88），不是跨进程全局互斥。Run fence 拒绝过期 Worker 写 DB，但不能撤销已发出的远端副作用；恰好一次外部执行仍需要 Provider 幂等/receipt/对账，不能靠 Lease 保证。

### B6. Context、Compaction、Memory、Skills

`driver_v2.py:32–60` 在当前 frame 下串行生成下一轮决策；`compaction.py:162` 基于已提交 observation cursor、冻结来源与 Snapshot 准备请求。Snapshot 接受有 row_version/generation；`execution.py:681` 锁 Root/Frame 并检查 Run lease。跨 Worker 可以通过持久读取检查事实，**不能多个 Worker 同时各自推进同一模型 transcript**。

Batch 的 copy(context) 只隔离当前 invocation 指针，底层预算/取消等仍共享并由原 Owner 管理；这不是跨进程共享上下文方案。Child 自己的 Seed/Context、父层只接受内部结果（handoff.py:82、100）避免直接并发写父会话。Skills 冻结在 pinned_config，恢复依赖匹配的 Registry、工具、模型配置、Memory binding 与 digest，不能随恢复热换。

本次方案不新增 tokenizer、token counting、Provider、模型路由、推理服务或计费底座。受控模型 fixture 必须满足原准入；真实统一模型 API 若不能满足当前可靠请求评估合同，应单独暴露阻断，不能复活 Stage19 实验豁免。

### B7. 测试源码证据与文档适用范围

阅读的高价值断言（T，全部 NOT_EXECUTED）：

- `tests/test_agent_batch_handoff.py:443` 用 Barrier 证明两个只读工具重叠，并断言一次 OBSERVED/最终 receipt；`:323` 对 crash 后 COMMITTED/STARTED、对账与派发次数；`:351` 验证 ChildResult 复用及统一 generation。
- `tests/test_agent_loop_persistence.py:37` 验证 accepted decision、旧 fence/旧 generation 拒绝、重复 hydrate 不再递增；`:76` 验证 UNKNOWN 保守结算和恢复上限。
- `tests/test_stage7_wp1_run_control.py:44、97` 包含竞争 claim、terminal/Journal 原子回滚等定向测试；`test_stage7_wp2_durable_hitl.py:288、311` 覆盖单 durable claim 与 terminal 后审批不能复活。
- `tests/test_ci_incident_diagnosis.py:62` 为报告绑定/已观察来源；`tests/test_ci_json_output_and_step_terminal.py` 为当前 HEAD 的最终 JSON 和 Step/Journal 收口回归。

`docs/runtime/runtime_recovery_runbook.md:5` 旧开头写 validation only，但 `58` 起区分执行聚合恢复，`84` 起列 v2 Batch/Delegation；不能只引用第一段否认当前恢复能力。Owner matrix 的早期 Snapshot/typed result 条目也有旧适用范围；与当前 factory/repository 的执行事实一起阅读，不把历史文字提升为新架构禁令。此次不改文档。

## C. AgentEvalOps Durable Worker

### C1. 真实生产链与事实所有权

`go-backend/cmd/worker/main.go` → `bootstrap.Worker`（internal/bootstrap/worker.go:35）→ schema/writer/role 校验、pgx pool、Target/Evaluator 装配 → `worker.Runtime.Run:72`。Go API `httpapi/product.go:189` 注册 `/runs` 创建命令，经 Snapshot/Target/Retry 冻结进入 Evaluation Kernel；不是旧 backend Python/Celery 自动 fallback。

`EvaluationRun` 冻结 cases/evaluators/target；`ExecutionAttempt` 是受控外部执行尝试；成功后创建 `EvaluatorWork`；最终 `EvaluationResult` 追加写。CI 可选择 `CIBusinessTarget` 或独立的 AdaptiveTarget，前者 `provider/ci_business.go:95` 调 Core `/api/ci/quality/evaluate`，后者 `provider/adaptive_execution.go:68` 调 `/api/runtime/evaluation-execute/adaptive/v1`。bootstrap 在 `LOCALAGENT_ADAPTIVE_ENABLED` 等配置间排斥不兼容组合；不是默认同时开启全部路径。

AdaptiveTarget `100` 解码版本化 Evidence，核对 identity/strategy/digest，`107` 绑定 Evidence，按 Runtime/Business outcome 分类。平台管理 Evaluation Truth；外部 Run 的审批/终态/副作用仍由 AgentCore 管理。跨仓只走受控 HTTP 与版本化 Evidence，不共享执行状态表，不直接复用 Python 状态机实现。

### C2. Claim、Lease、事务、恢复

| 能力 | S 证据 | 边界 |
|---|---|---|
| 原子领取 | postgres/evaluation_execution.go:144 transact；:402 ClaimExecutionAttempt；evaluation_work.go:29 ClaimEvaluatorWork | 先候选扫描，事务锁 Run/Attempt/Work，再 conditional UPDATE + 新 token；不是扫描即持有、也不是 SKIP LOCKED 队列 |
| Fencing | execution.go:155 writer epoch；:211 owned；:223 fencedExec | token + epoch + DB clock lease + 活跃状态；最终 UPDATE 再检查 fence |
| 续租 | execution.go:454；work.go:64；worker/runtime.go:303 | 独立 goroutine 续租，丢失 ownership 取消操作，不抹除外部事实 |
| 成功交接 | execution.go:490–546 | Attempt success 与全部 evaluator slots 在同一事务插入，提交后才可扫描 |
| 执行崩溃 | execution.go:549–574 | 到期终结为 OUTCOME_UNKNOWN；不默认重新执行 |
| 判分崩溃 | work.go:148、203 | 保留调用 provenance，按冻结有限预算/退避重领 Work，新 token 拒绝旧提交 |
| 结果追加 | work.go:366、471、475 | 同事务 INSERT result + fenced complete work；稳定 command receipt 支持重复提交判断 |
| 人工/自动 retry | evaluation/kernel.go:95；postgres/evaluation_run.go:55 | UNKNOWN 禁自动 retry，只允许受权确认且 policy 允许的新 fork；不能直接套到工具副作用 |
| 发现 | evaluation_run.go:211；worker/runtime.go:139 | bounded keyset scan、cursor wrap、poll/backoff，存在重复候选竞争；不是 push-only 事件系统 |
| 退出 | runtime.go:118–135 | 停 admission/poll，保留续租 drain；到期取消 operation，再 join |

事务仅包 DB 命令；`worker/runtime.go:329` 的 Target.Execute 在 Claim/Start/ReadRunState 事务结束后调用，`:458` evaluator 也在短命令之间运行。固定 ExecutionConcurrency/EvaluatorConcurrency 循环在任务返回前不领取新工作（`:94`–`103`），**外部 HTTP/Judge 等待仍占对应 Slot**；Go goroutine 挂起不代表业务并发许可释放。

同一个 Run 的多数变更在 `transact` 中锁 Run 行（execution.go:173），所以外部工作可并行而权威提交串行；不要把它宣传成无争用分布式 DAG。DB 读写量包括每 Slot 候选页、claim/start/renew/finalize、协调/过期扫描、Provider begin/finish；本轮没有 EXPLAIN、事务计数或负载实测。

Migration `backend/migrations/versions/c12a00200001_add_go_durable_evaluation_kernel.py:55、71` 有 Work 唯一 slot 与 one_result_per_work；`:72–75` 有 pending/expired/running 部分索引及 immutable/atomic guards。Work scan 按 created_at/id，pending index 以 next_available_at/id 开头；这提示需测真实查询计划，不能静态认定已有严重索引瓶颈。bootstrap 的最小角色禁止 result UPDATE/DELETE/TRUNCATE（worker.go:83 附近），追加事实不由普通 Worker 重写。

### C3. 测试证据与可借鉴部分

T：`postgres/evaluation_integration_test.go:247` 两独立 session 竞争只能一个 Applied；`:274` 起错误/过期 token 拒绝；`:305` 附近检验成功与 slot 插入失败回滚；`:398–432` Work 重领换 token 和迟到提交拒绝；`:471–502` 结果与 Work 原子性；`:584` UNKNOWN 重试授权。

T：`postgres/worker_integration_test.go:221` 包含真实 Worker subprocess，执行 kill→UNKNOWN、evaluator kill→新 token、双 coordinator、容量/drain；`worker/runtime_test.go:208、268、325` 有有界容量、ownership loss、DB 故障与 drain。这里只确认测试源码及断言存在，**本轮没有验证这些测试当前通过**。

可参考：短事务、数据库时钟、稳定 command receipt、后续工作与状态提交同事务、扫描仅作发现、续租与操作取消分离、明确的未知结果。不要直接复制：evaluation writer epoch/cutover 体系、固定 case×evaluator slot schema、评测结果/判分重试语义。AgentCore 动态 Loop 的下一批工作由模型决定、带上下文/预算/HITL/副作用；Eval 的判分目标与输入通常预先冻结，成功 artifact 可供多个 evaluator 读取。复用原则比跨仓共用调度库更合理。

## D. Confirmed Limitations

| 候选问题 | 分类 | 事实与影响 |
|---|---|---|
| Run 级协调限制同 Run 跨 Worker 推进 | CONFIRMED | 单 Run lease，Scheduler 本地 claim；无独立 Work ownership。业务是否需要该能力＝NOT_CONFIRMED |
| 正常外部等待占 Step 线程及许可 | CONFIRMED | SYNC_BLOCKING + 同步桥；吞吐是否被其主导＝LIKELY，未测 |
| 所有 await 阻塞事件循环或长期 DB 连接 | NOT_APPLICABLE | 原生 AsyncHTTP/短 session 不支持该笼统描述 |
| 所有 ACTIVE Run 必须活 Task | ALREADY_SOLVED（指定等待场景） | LoopSuspended + durable backoff + Run 接管已存在；不能推广为任意 HTTP 都可挂起 |
| DAG 慢分支延迟其他下游 | CONFIRMED | 整批 TaskGroup 屏障；当前 CI 单 Step 不直接受该 DAG 限制 |
| v2 三动作中的慢工具拖住下一波 | CONFIRMED | 最多两项一波且等待整波；第三项不会在第一项完成时补入 |
| CI 取证没有利用已有并发 | CONFIRMED | 业务 concurrency=1，Tool spec 默认1；取证实际是否足够慢值得并发＝NOT_CONFIRMED |
| 模型依赖的下一步必须等结果 | NOT_APPLICABLE（作为错误调度问题） | 依赖与完整消息组是合同，不应为吞吐提前打破 |
| Run 跨进程恢复缺失 | ALREADY_SOLVED（有限合同） | lease/fence + canonical hydrate；不是任意副作用可自动重放 |
| 恢复扫描具备与 HTTP 相同 admission | CONFIRMED 缺口 | scan batch 不等于 active cap；LIKELY 恢复压力风险，未证实饱和 |
| PostgreSQL 写放大/压力过大 | NOT_CONFIRMED | 已有多短事务、renew 和扫描；C 会再增加 Claim/Work/通知成本，没有量化依据 |
| 多 Worker 同写上下文会破坏假设 | LIKELY（若引入 C） | 当前单 Owner/generation 与完整组合同已有保护；开放并行 owner 需重新证明 |
| 业务结果质量问题可由调度解决 | NOT_CONFIRMED | 历史真实输出失败/准入限制与调度不是同一问题 |

上述 CONFIRMED 指源码结构成立，不表示性能已实测。Stage19 历史异常 Frame 收尾与错误分类记录继续保留为既有风险，不在本轮修复，也不凭历史单次失败扩大 Runtime 重写。

## E. Architecture Alternatives

| 维度 | A 保留 Loop/局部优化 | B 局部事件化/唤醒 | C PostgreSQL Durable Work | D 外部队列/工作流引擎 |
|---|---|---|---|---|
| 可解决已确认问题 | 启用 CI 合法只读并发；必要时另开局部调度改进 | 缩短已有持久等待的发现延迟；在精确安全边界释放协调等待 | 独立可领取工作跨 Worker 分发 | 外部通知/调度吞吐，但当前无已确认需求 |
| 不能自动解决 | 全局跨 Worker 分发、整个 Step 等模型占线程 | 任意在途网络调用无损迁移、上下文多写、模型质量 | 等待 Slot 自动释放、外部恰好一次、模型质量 | 状态 Authority、幂等、UNKNOWN、模型质量 |
| 侵入 | 小；先用已有 spec/policy | 中；需安全挂起点与原恢复 Owner 接线 | 大；需新工作身份/claim 与 Run fence 合同 | 很大，增加外部系统与失败模型 |
| Owner 影响 | 不变 | Run 单写者不变，通知只唤醒 | 必须明确 Work 与 Run/Tool 权威的嵌套，不能第二状态机 | 引擎不能与 PG 同时成为执行事实权威 |
| 并发正确性 | 复用现有上限/资源锁/批次提交 | lost wakeup、重复通知、terminal 后唤醒 | 多 writer、依赖提交、预算与 context 竞争、迟到结果 | 另外增加重复投递、分区、双写一致性 |
| Tool Governance | 原链无绕过 | 原链；在途 UNKNOWN 不可重派 | 必须原 Tool 服务执行、审批 claim/receipt 不复制 | 同左，消息不是执行授权 |
| PG 成本 | 事务大体不变，峰值并发可能上升 | 可减少发现延迟；仍须持久扫描兜底 | 增加 work claim/renew/finish/依赖事务及索引 | PG 执行事实仍需保存，不能靠队列消除 |
| 运维 | 无新基础设施 | 小幅复杂化 | 扫描、公平性、回收、积压管理 | Kafka/Redis Streams/Temporal 等显著增加运维面 |
| 回滚 | 停新准入、drain 新配置 Run、恢复原配置 | 关通知、保留扫描，drain 新等待合同 | 活跃 Work 需兼容读取/drain，不可直接删表 | 引擎任务迁移/版本回退难 |
| 收益可验证性 | 可直接测 CI 取证重叠和报告质量 | 需真实等待/发现时延基线 | 需先证明单 Run 分发业务收益超过成本 | 当前低，无引入理由 |

D 的说明仅是本任务内的方案取舍，不假定任何产品的当前版本能力。消息通知、任务 Claim、权威完成提交是三种不同操作；任何选项均不能让“收到消息”替代已提交事实。

## F. Recommended Architecture

**推荐 A。EVENT_DRIVEN_NECESSITY=CONDITIONAL：存在可优化的调度/等待结构，但本轮证据不支持默认新建事件驱动执行平台。**

首个可回滚垂直切片：在原 CI 工具/Definition 范围内，核验 `ci_read_execution_evidence` 无共享可变状态且每次 session 独立后，将其明确声明为最多2并发，并将 CI batch concurrency 从1调整为2。首片保持 max_batch_size=2、FAIL_FAST、模型/工具总预算、deadline、审批、结果观察顺序和 Validator 不变；上下文目录读取先完成，再并行读取两个互不依赖证据 ID，第三个保持下一批。不要为了“三分支”直接突破现有合同或引入第三个工具。

这是**条件性提案**：先做 WP1，确认两个读取确实可独立执行、完整报告能稳定生成、读操作时间可测、不会只是用工具延迟桩制造线上收益。如果真实业务几乎全耗时在模型，或有限并发增加 DB 争用却无收益，则取消 WP2，保持现状。

最小改变边界：受信业务 Tool spec/Definition 的现有并发参数与必要身份版本，以及针对该行为的少量测试。无需新 Schema、新 Worker、新状态机或新 dependency。模型请求仍使用公司现有统一服务 API 合同；本阶段先用公开确定性模型 fixture，不能宣称验证了真实 Provider 质量。

为什么不选 B/C/D：审批/UNKNOWN/轮询已有挂起，尚无唤醒时延证据；跨 Worker 同 Run 不是当前 CI 必需；Go 方案也保留外部等待 Slot；引入更多 Owner 会扩大预算、上下文、副作用证明范围。

次级问题只记录：若 WP1 显示 Step 等模型占线程为主导，则由 Codex-A 单独冻结**现有单 Owner 内的等待边界**，优先消除具体同步桥长等待或改善既有挂起，而不是将所有 Step 转 Work。若证实 DAG 屏障影响真实业务，可单独设计完成即补位调度，但必须明确 FAIL_FAST admission 语义，不夹带到本次 CI spec 切片。

回滚：停止新 Candidate 准入，保留其冻结 Definition/Tool 身份直到活跃 Run drain；之后恢复 Baseline 配置。不能在活跃 Run 中热改 spec digest，也不能删除已写回执；已有 Factory pin 校验会拒绝不匹配恢复。

关键风险：真实模型未必一次输出两个独立调用；Tool 并发提高会增大 DB 瞬时负载；线程瓶颈未消除；Tool resource lock 仍本地；旧 Run pin 的 drain；有效报告/来源正确性比耗时优先。

## G. Execution Invariants

| 不变量 | 当前保证（S/T） | 若以后 B/C 需要新增/强化 |
|---|---|---|
| Run/Step 合法迁移、唯一终态 | AgentStateMachine/Coordinator；execution.py:238、364、621；Step 与 terminal 定向测试 | 保留原 State Owner，Work 不直接封 Run |
| 迟到 Worker 不复活 terminal | RunControl ACTIVE/fence/lease；generation/row_version；terminal scan 排除 | Work commit 还须检查父 Run terminal 与当前授权代际 |
| 并行结果不互相覆盖 | 独立 Action ID，record_action 验 COMMITTED source/digest；事务锁 Frame | 多 Worker 只能提交自己受权结果，不并发重写 transcript |
| 依赖只基于确认结果 | Scheduler SUCCEEDED；Batch 完整结果检查；ChildResult CAS | 完成与新工作可见性事务边界必须明确 |
| Claim 原子 | Run 行锁/条件写；Eval Run/Attempt/Work 锁与 token | 独立 Work claim 若新增须 DB 原子，不用进程锁冒充 |
| Lease 过期不等于没执行 | STARTED/UNKNOWN/reconcile；Eval OUTCOME_UNKNOWN | 不因重领触发不可幂等副作用重放 |
| 事务不跨外部网络等待 | Tool start/commit、Model attempt、Eval Target 分隔 | 继续使用短事务，不在锁内 await 工具 |
| 重复完成不重复推进 | commit_batch OBSERVED 直接返回；一致 receipt/digest 比较；Eval command receipt | 当前没有通用外部 completion 消费者，不能声称任意乱序事件已覆盖；通知仅提示重读 |
| 崩溃不依赖内存通知 | durable state + 扫描恢复 | B 通知必须有扫描兜底；C commit 与依赖可见性原子 |
| 审批不可绕过 | APPROVED 与独立执行 claim、Tool start 分离 | 禁用消息本身授予批准/执行权 |
| 已提交工具不重做、UNKNOWN 不乱重试 | committed reuse、bounded retry、原 reconciliation/manual | 外部保证取决于 Provider 幂等/权威对账；不承诺外部 exactly-once |
| context 单写 | 一 Run lease，Frame/generation，整组 observation | Worker 读冻结输入，聚合者唯一推进 Model/Compaction/Memory |
| 输出质量与评测归属 | OutputGate + CI validate_report；Eval result 追加 | 不放宽 Validator；Eval 不能回写 Core Run Truth |

本轮不为假想的通用事件乱序建立防御框架。只对拟修改路径复用上述保护；若 C 真有必要，必须重新审计预算、Memory binding、HITL、输出与跨进程资源互斥，不能仅新增一个 work 表就宣称完成。

## H. CI Incident Experiment Design

### H1. 当前业务边界

`core/incidents/http.py:39` → IncidentService.execute → 现有 CI simulation 生成执行/证据；`:58` 建 investigation。`ci_diagnosis.py:112` 绑定 Incident→Investigation→run_id，Run 进入一个 v2 AGENT Step。`ci_diagnosis_tools.py:11` 仅有 context 与 evidence 两个工具；`service.py:154` 每次校验 Run owner、tenant/user、agent/version/session 并读取当前 execution manifest。

日志/环境/Case requirements 是 PARSER_LOG、ENVIRONMENT_RECORD、CASE_REQUIREMENTS 等证据项。当前链路不是三个真实企业 CI API，也未证明有独立历史故障查询工具；历史故障相关外部检索标记 NOT_VERIFIED，不捏造生产接线。context 提供目录后，多个不同 evidence_id 的只读读取可以作为独立分支；最终报告依赖确认的来源覆盖。`ci_diagnosis.py:58、138` 校验 schema、身份、已读证据、digest/coverage 后 seal。

用户提供的3000套环境、每日9000版本执行/90万Case作为业务背景；120夜间 Agent Run 只是模拟假设，均不是本轮生产容量证据。

### H2. Baseline / Candidate 与最小场景组

Baseline 使用当前 HEAD、当前 CI Definition（batch size2/concurrency1/spec1）。Candidate 仅是 F 中的只读并发切片。固定公开合成 Incident、相同证据/输入、模型返回轨迹、预算、deadline、Token/Provider profile、连接池、blocking workers、硬件与请求到达序列；模型 fixture 不得注入根因答案或跳过正式 Validator。采用分开的隔离测试库与新 Run identity，不手工篡改生产状态。

| 场景组 | 最小可重复安排 | 必须观察的结果 |
|---|---|---|
| 正常业务/多 Incident | 先1个，再不超过默认 admission 的一批；更大批次仅在容量目标明确后 | 合法报告、来源和预算一致；有效成功吞吐，所有失败计入分母 |
| 三独立证据与慢读取 | context 后两条证据同批、第三条下一批；给其中一条受控延迟，保留原两并发上限 | Candidate 两条确实重叠；第三条仍受批次合同，不伪称三并发/滚动补位 |
| 三动作调度结构对照 | 另用现有 v2 fixture（允许size3但并发2）显示快/慢/第三项的 wave 屏障 | 仅证明 Runtime 结构，不算 canonical CI 的容量或首片交付；不必新增生产工具 |
| 外部等待与恢复 | 复用 bounded-poll/HITL/UNKNOWN fixture，挂起后另 Run 可执行；真实 kill 在已提交动作边界 | 无在途资源错误释放；新 Owner 合法接管；绝对 deadline 不重置 |
| 重复、迟到与不确定副作用 | 复用 batch/handoff 恢复夹具；重复原完成命令、旧 fence 提交、受控 receipt 已发生但响应丢失 | cursor/terminal 不重复，旧 owner 被拒绝，UNKNOWN 未经对账不得重派，副作用计数不增加 |

不是把以上每行扩为一套矩阵。首片优先1个业务并发回归与1个资源/失败路径；恢复/副作用优先选择已有精确测试。没有新事件消费者时，“重复完成通知”通过原幂等提交/重复扫描验证，并明确它不是消息总线测试。不得触发真实企业CI/生产副作用。

### H3. 指标与采集

本轮**所有指标均无当前实测样本**。表中“已有”只表示源码已有计量点/可派生字段；历史 Stage19 RT 是失败实验观察，不能填入本轮成功基线。

| 指标 | 采集位置/现有基础 | 最小新增采集 | 对照与判定 |
|---|---|---|---|
| Incident P50/P95 | HTTP提交、Investigation seal、Root terminal时间；Go client observed RT 是另一层 | 测试记录 admission前/受理后至报告seal两种时长 | 相同批次/到达序列；样本不足不声称可靠P95，错误率不得隐藏 |
| 有效吞吐 | 成功且通过 validate_report 的数量/固定实验窗口 | harness汇总即可 | 失败/不足报告单列；不能仅数 Runtime SUCCEEDED |
| Slot占用/释放 | Supervisor admission wait metric；BlockingExecutor snapshot；Eval claim/task_finished日志 | harness定点采样 active/pending/等待与挂起释放时间 | 相同容量，不靠增线程伪装调度收益；区分Run/Step/Tool/Go Slot |
| 工具并发利用率 | Tool started/completed事实、span、测试Barrier | 测试级在途计数及时间轴 | 确认重叠且<=2；顺序/内容与原Observation一致 |
| PG事务/连接池 | 短transaction边界，pool配置 | 隔离实验读pool统计/事务计数；必要时测试连接事件钩子 | 相同样本/池；比较每有效Incident事务数、峰值checked-out、等待；不采正文 |
| Claim/扫描成本 | recovery_scan、Eval Candidates、claims/lost_races counters | 记录扫描次数/返回数/耗时/竞争失败，必要时隔离库EXPLAIN | 区分空扫与成功claim；A不应新增工作表/扫描器 |
| 崩溃恢复时间 | kill时间、lease到期、claim、首个恢复推进 | harness时间戳 | 固定lease/cadence/故障点，分别报告发现与执行重建耗时 |
| 重复事件结果 | Action/Observation cursor、command receipt | 复用测试断言 | 重复提交无新执行/新terminal；冲突payload应拒绝 |
| 副作用重复次数 | 既有crash fixture receipt/dispatch日志 | 使用已有合成计数 | 重复外部副作用=0；UNKNOWN保持UNKNOWN或权威对账，无盲重试 |
| 最终报告质量 | 原validate_report；Go版本化Evidence、Result/Gate | 公开有可验证来源的fixture与原evaluator | schema/来源/coverage不能降低；无语义GT则语义质量INCONCLUSIVE |

验收先看可重复的执行顺序与正确性，再报告延迟分布。收益阈值与重复次数应在 WP1 依据基线噪声冻结，不能现在杜撰提升百分比。只在性能差异超出基线自然波动、失败率/质量/DB资源没有不可接受退化时认可优化；否则保持A现状或取消切片。模拟实验结果只证明受控场景，不能宣称120 Run或真实生产容量验收。

## I. Formal WP Plan

本次完成的是 WP1 的静态审计部分。取消默认“独立工作分发/通用依赖唤醒”WP；不安排 C/D 平台实现。每个 WP 结束停止，由总控选择下一步。

### WP1：受控基线与切片条件冻结（下一任务）

1. **目标**：补齐真实执行资源证据，确认 CI 两条证据独立可并发、原报告稳定可验；本报告已完成静态必要性判断。
2. **前置**：总控接受A提案并下发实验任务；先读 `.ai/setup.md`；隔离PG和公开fixture；不恢复实验模型准入。
3. **允许模块**：复用/少量扩展 `tests/fixtures/adaptive_execution_server.py`、CI/Batch测试夹具与一个按能力命名的实验入口；只增加必要计数，记录基线证据。
4. **禁止**：生产Runtime/Tool/配置/Schema修改；真实CI/模型；Eval operator-only；新增框架。
5. **合同**：原Definition、Tool spec、预算、deadline、Validator、Evidence、Run ownership完整保持。
6. **最小交付物**：一条context→证据→合法报告的canonical路径基线；线程/Slot/工具时间线；隔离DB成本；准入与测量条件冻结。
7. **轻量测试**：现有CI定向用例及一个可重复基线；最多少量已选恢复断言，不全仓回归。
8. **PASS**：确认同样原业务输入可稳定产出有效报告，得到资源占用事实与噪声范围；独立两读取无共享可变状态；明确Candidate仅改并发参数。
9. **REWORK/BLOCKED**：只得到fake Runtime或绕过Validator＝REWORK；原准入无法构造合法受控模型、隔离PG缺失、业务报告无可验来源＝BLOCKED。局部并发无收益空间则记录NO-GO，取消WP2而非扩大架构。
10. **后续依赖/Owner**：Codex-B承担基线执行，Codex-A只在证据触及Owner/Contract或必要性变化时作决策。WP2必须等待基线结论。

### WP2：CI 两条只读取证的最小并发切片（首个实现 WP，条件性）

1. **目标**：用现有Multi-Tool Batch减少独立取证串行等待，不改变整个Loop驱动方式。
2. **前置**：WP1 PASS、总控接受本片；精确冻结工具并发安全性与可回滚身份。
3. **允许模块**：`core/agent_platform/ci_diagnosis.py` 的业务并发/必要版本，`ci_diagnosis_tools.py` 的指定只读工具spec；对应少量测试；仅受影响正式合同说明。
4. **禁止**：`run_coordinator`、Scheduler、ParallelExecutor、ToolExecutionService、Repository/Schema、通用模型层、Memory/Skills、其他业务、Go Worker、公共状态机；发现需改这些则停在Scope决策，不顺手扩写。
5. **合同**：batch size仍2、并发最多2；FAIL_FAST、原顺序Observation、Budget、HITL、UNKNOWN、lease/fence、OutputGate/Validator不变；不能把局部并发当跨Worker资源锁。
6. **最小交付物**：真实CI路径两证据读取重叠并完成合法报告；Definition/Tool pin升级与drain回滚说明。无需新生产文件或阶段目录。
7. **轻量测试**：一个业务并发回归（可用Barrier）＋一个失败/取消或资源冲突回归；复用相关Batch/CI断言，不超过必要少量用例。
8. **PASS**：2条读取实际重叠且不超上限；来源/输出不变；无额外模型调用、无新增重复工具执行、预算正确；目标测试一次通过。
9. **REWORK/BLOCKED**：只改数字但生产路径不重叠＝REWORK；为了通过改Validator/热换活跃pin＝REWORK；必须触及状态/恢复Owner才能实现＝BLOCKED，交Codex-A重新决策。
10. **后续依赖/Owner**：Codex-B实现，完成即停；WP3审查实际diff和实验。

### WP3：CI 对照实验与 Stage20 定向 Final Gate

1. **目标**：证明收益来自该切片且原执行合同未受损；允许无收益结论。
2. **前置**：WP2稳定或WP1明确NO-GO；同硬件/数据/到达序列/额度，双仓HEAD与配置可追溯。
3. **允许模块**：测试/harness/当前Handoff证据；若版本化Evidence已有字段不足，只记录缺口，协议修改另作决策。
4. **禁止**：新架构/Schema、真实企业CI/生产副作用、扩大预算、换模型或修质量失败以美化性能；不重新打开无关历史Gate。
5. **合同**：G中不变量；AgentCore Execution/Business Truth 与 AgentEvalOps Evaluation Truth 分离。
6. **最小交付物**：Baseline/Candidate时间线和报告、必要恢复/迟到/UNKNOWN证据、指标结论、Known Limitation及回滚状态。
7. **轻量测试**：运行受影响CI/Batch/生命周期定向测试；选择现有crash/recovery精确场景。该WP明确Final Gate时才允许有理由扩大，但不要求全仓、不重复已通过大套件。
8. **PASS**：正确性无退化；有效报告/来源校验通过；性能差异超过冻结噪声且PG/失败率可接受；模拟/生产结论分开。无收益但正确＝NO-BENEFIT，回退/停止，不标优化PASS。
9. **REWORK/BLOCKED**：重复副作用、迟到提交、terminal复活、来源伪造＝REWORK；缺质量可比性/可靠测量＝BLOCKED；真实模型质量仍NOT_VERIFIED不冒充生产通过。
10. **后续依赖/Owner**：Codex-A Final Gate。默认阶段结束；B/C只有新业务证据和新明确授权才另开设计，不自动成为WP4。

## J. Stage20 Initial Decision

```ini
SOURCE_AUDIT = COMPLETE
EVENT_DRIVEN_NECESSITY = CONDITIONAL
RECOMMENDED_OPTION = A
ARCHITECTURE_DECISION = PROPOSED
FIRST_IMPLEMENTATION_WP = WP2
IMPLEMENTATION_READY = NO
UNRESOLVED_RISKS = [没有当前性能基线, 同步Step线程等待可能主导吞吐, 恢复任务与HTTP准入容量边界不同, CI只读取证并发收益未测, 活跃Run配置pin回滚约束, 当前真实模型业务质量及可靠准入未验证, 同Run跨Worker资源与上下文写入未设计且本次不实施]
NEXT_ACTION = 交Codex-B执行WP1受控基线；总控据证据决定是否下发WP2；若改变Owner或合同则回Codex-A
```

COMPLETE 仅指请求的双仓静态源码审计、架构比较和工作包规划已完成；不意味着所有能力经过运行验证。IMPLEMENTATION_READY=NO 表示尚未冻结性能基线和首片实验条件，不是第二仓缺失或需要再做形式审批。本报告已直接交付，无需用户再次批准本轮只读工作。

- 当前无仓库定位阻断；运行收益、真实Provider业务质量、线上容量、DB真实压力均 NOT_VERIFIED。
- 测试/HTTP E2E/模型/迁移/真实CI/容量实验全部 NOT_EXECUTED。
- 历史 Stage18 BLOCKED、Stage19 Final Gate PENDING/真实收益未证明不能改写为PASS。
- 本轮仅新建本报告；未修改正式源码、配置、依赖、测试、数据库或历史Handoff；未Commit/Push/切分支。
- BREAKING_CHANGE = NO；原执行行为保持不变；无旧测试删除/归档。本轮不宣称已实现事件平台或优化收益。
交付静态复核：A–J 章节及初始决策字段齐全；报告 UTF-8 可读。写入后双仓已跟踪 diff 仍为空，LocalAgent 仍仅显示原有 data/ 未跟踪项，AgentEvalOps 仍干净。该 Handoff 位于现有 Git ignore 范围，已保存到磁盘但未加入 Git；未改变 ignore 规则。
