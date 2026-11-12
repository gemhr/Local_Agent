# Stage20｜WP1 独立 Gate 与架构方向复核

日期：2026-11-05（Asia/Shanghai）。角色：Codex-A。本报告是 Stage20 中间决策，不是 Stage20 Final Gate。

**结论：WP1 PASS，受控功能基线 VERIFIED；性能证据 INSUFFICIENT。原 WP2 DEFERRED，唯一推荐下一步是一次限定边界的耗时归因实验，Owner 为 CODEX_B。当前不授权并发参数或调度架构实现。**

## A. WP1 Evidence Review

### 审查基线与证据等级

- LocalAgent：`D:\PythonProject\Local_Agent`，HEAD `f2d630091c942b1e57b0d4c14c1e0b6e8208b894`；tracked/staged diff 空，原有未跟踪项为 `data/` 与 `tests/test_ci_investigation_baseline.py`。
- AgentEvalOps：`D:\PythonProject\AgentEvalOps`，HEAD `0fab7fbcfc8475992f5979a892193d13c6118fe0`；工作树干净。
- 已读双仓根规则、`.ai/setup.md`、首次架构审计、Codex-B 报告、完整新增测试、JSON 及直接依赖源码；相关目标目录未发现局部 AGENTS.md。
- 本轮只运行文件读取、Git 查询、JSON 解析/重算及 `git diff --check`（退出0）。**pytest、Go 测试、CI、服务、数据库连接、真实模型与真实企业 CI 均 NOT_EXECUTED。** 未读取 secret、OperatorOnly/Holdout 或业务数据库。
- 下文“源码”表示当前实现可直接证明；“产物”表示本轮读取并核对保存数据；“B 运行记录”表示 Codex-B 报告中的命令/退出码，非本轮独立执行。

### 正式执行链与受控装配

新增测试 `tests/test_ci_investigation_baseline.py:24` 复用 `test_ci_adaptive_stop.build()`，在 `:105` 调用原 `CIInvestigationService.start()`，经原 Supervisor、AgentApplicationService、ChatService、CoordinatedRuntimeFactory、RunCoordinator、AgentLoopDriverV2、ToolExecutionService 和 PostgreSQL repository 执行。没有替代 Runtime 或直接伪造终态。

`core/agent_platform/ci_diagnosis.py:105` 保留授权、admission、Run/Conversation 绑定和服务级投影；`:138` 从正式 repository 装载终态，并调用原 `validate_report()` 后封存。`validate_report:58` 检查 Run/Agent/Session、唯一 ROOT Frame 与成功 Step、FINAL decision/DELIVERED receipt、内容与 Step digest，再通过 `ci_evidence.observed_sources()` 核对 COMMITTED 工具来源、Action projection/source digest、已观察批次及原始 evidence manifest，最后检查 minimum_coverage。原 OutputGate/StepCompletion 装配仍在 RunCoordinator 中，测试没有替换 Gate 或 Validator。

这证明的是**进程内 CI 业务服务级受控全链路**，不包括 `server.py::lifespan()` 完整生产装配、HTTP 认证路由、真实远程 Provider 或 AgentEvalOps Go 评测。`assembly()` 使用正式 ToolGovernanceService、DurableToolInvocationService、DurableApprovalService、RunControl 与执行 repository；权限目录为测试装配，不等同于生产完整配置。

### 模型 Fixture 与准入

`tests/fixtures/adaptive_execution_server.py:36::ObservedInvestigator` 只从传入 transcript 的工具消息解析 Observation（`:46`），先取 context，再按其中 evidence_id 产生合法 NativeToolCall（`:56`）；实际观察后复制 digest 并组装确定性报告。它没有执行 manifest/GT 字段，也未调用 `InvestigationProvider.report()`。`build()` 创建的原模型对象虽持有公开合成 manifest，本测试将其 `invoke_request` 替换为调用独立 ObservedInvestigator 的采样函数，原模型报告路径不被执行。

Fixture 的策略预先选定 PARSER_LOG/ENVIRONMENT_RECORD/CASE_REQUIREMENTS，且最多取 pending[:2]；这是合法受控策略，不证明真实模型的发现能力、诊断质量或双工具调用频率。

`real_model=True` 的真实语义是通过 CI 服务的可用性开关（`ci_diagnosis.py:106`），不是远程调用证明。实际 Profile 为 synthetic、`BOUNDED_ESTIMATE / utf8-byte-upper-bound.v1`，context_window=65536、max_tokens=512；未传 request_counting_exception。测试保留正式准入，`:124` 断言四条 assessment 均 reliable/admissible；产物 remote_model_calls=0，符合受控模型性质。

`build()` 原默认 total_tokens=131072，本测试显式传回 definition 的32768（`:25–26`、`:120`），没有提高预算。max_batch_size=2、max_batch_concurrency=1、8次模型/工具上限及180s deadline 不变。产物 model_calls=4、tool_calls=4、total_tokens=28257。Skills 与 skill tools 被既有 build 移除，这是已披露的测试边界；不能据此宣称完整生产 Skills 路径通过或耗时相同。

### 正确性、批次和隔离

- 测试 `:115–124` 独立断言 Root/Step SUCCEEDED、Final DELIVERED、Investigation COMPLETED/SUPPORTED、safe_error_code=None、全部 Tool COMMITTED。JSON 保留业务结果、receipt、预算、资源采样；受控等待样本另保留 Root/Step/Tool/Model 持久时间回读。
- `:125–131` 核对第二轮 ordinal=[0,1]、两个不同 ID、当前 concurrent_safe=False、串行时间线及整批提交晚于第二读取完成。`repositories/agent_loop.py:181::commit_batch` 按 ordinal 锁定动作，检查全部 COMMITTED 来源与 digest，一次写完整 Observation 并递增 cursor；产物为 OBSERVED cursor=1,2,3，第四轮 FINAL CLOSED。测试未改变此合同。
- “Action COMMITTED”应精确理解为关联 DurableToolInvocation.state=COMMITTED，Action 保存 projection/source digest；不引入不存在的 Action.state 字段。
- ToolExecutionService 保留许可、预算 reservation、deadline/cancellation、durable start/commit；测试只包装原 Adapter/读取/commit，未 monkeypatch Governance 或安全判断。此轮成功路径不能代替 HITL、UNKNOWN、Fencing、恢复的动态验证，但相关生产模块无改动，不因此要求全量回归。
- `IncidentService.bound_execution:154` 每调用独立 database.session，检查 tenant/user/agent/version/session/Run ownership；只读查询，不更新共享 Investigation。Adapter 只保存 service/bridge/spec，调用内构造结果；两个 evidence_id 相互无数据依赖。这足以支持当前读取路径的并发安全前提，不代表并行性能或容量已验证。
- `_pg_fixtures.py:47` 拒绝非 `_test` 库名；pg_schema 重建测试 schema，clean_database `:195` 每例 TRUNCATE。B 报告记录运行前校验本机 `localagent_test`；本轮未重新连接验证环境。新增代码没有访问生产 DSN 或扩大既有清理范围。
- **隔离边界是专用测试数据库，不是每个 case 独立 schema。** 它会删除该测试库既有 CI 调查数据，不能与依赖同库的其他进程/pytest-xdist worker 并行运行。正常顺序 fixture 隔离符合此次两样本用法；不能笼统承诺“不影响任何其他测试数据”。build 的预备调查未执行，service.start 新建本次调查，report_version=2 合理。

### 运行记录可信度与首次失败

B 记录第一次命令为新增两样本加两项来源/准入相关既有测试：2 failed、2 passed，退出1；修正后仅重跑新增失败子集：2 passed，退出0。未重复运行已通过两项，符合最小验证原则。原始 pytest 完整 stdout/退出码未作为独立日志附入 JSON，本轮将这些计数标为 B 运行记录。

保存 JSON 的两样本与仍存在的 `pytest-2321/test_ci_investigation_serial_r0/ci_baseline.json`、`...r1/ci_baseline.json` 做了解析后字段核对，均一致（r1 本身也已附 persisted_timing_readback）。测试在所有业务断言之后才写 JSON，增强成功断言与产物之间的可追溯性。它不是签名审计日志；JSON 未保存完整模型 assessment、全部 Action 与原始 Observation payload，不能声称仅靠产物就独立重放所有 digest 校验。

首次失败后的修正正确：已封存的 `project_if_terminal()` 在 projection_status!=PENDING 时返回 `(row,None)`，因此后续 get.runtime_status=None 是现有合同；改为独立 repository.load().root.status 断言 SUCCEEDED，并保留业务/Final/Step断言，符合当前源码，没有放宽生产合同。没有最初测试文件快照，本轮无法独立逐行重建修正 diff；当前文件、保存产物和源码行为相互一致。

## B. WP1 Independent Gate

**PASS；WP1_FUNCTIONAL_BASELINE=VERIFIED（上述受控服务级边界）；性能测量仍为 PARTIAL。**

原业务入口、原执行/持久化/Gate/Validator、合法模型准入和未提高预算均有当前代码与产物支撑；未发现阻断此功能基线的缺陷。连接统计范围修正及缺少生产分布不构成 WP1 REWORK。P0/BLOCKING_P1 在本次审查范围内未发现，不表示全仓或生产认证。

本轮没有重跑任何测试。B 的四项有效定向通过记录、产物核对、源码合同审查，是不同证据，不将其混称为本轮测试通过。

## C. Performance Evidence Assessment

| 重算项 | 无延迟样本 ms | 30ms受控等待样本 ms |
|---|---:|---:|
| service.start前至producer结束 envelope | 13397.999 | 13263.959 |
| Model Adapter合计 | 0.456 | 121.293 |
| 全部Tool Adapter合计 | 224.223 | 264.187 |
| 其余残差（相减口径） | 13173.320 | 12878.479 |
| 两候选Evidence Adapter合计 | 110.119 | 138.171 |
| 两读取本体完美重叠理论节省 | 54.089 | 67.990 |
| 第一读取完成至第二读取开始间隔 | 720.178 | 752.141 |

54.089ms约为13.398s的0.40%，只是读取本体的理想重叠收益；没有执行 Candidate，**已证实的优化收益为零项，而非已证明收益为零**。720ms间隔及Action外围事务可能存在更大可重叠部分；也可能有 Frame/Root锁、许可、线程与pool竞争，不能把间隔全算作可节省时间，也不能把54ms当完整收益上界。

### 连接指标的独立修正

两份 JSON 均有209对 checkout/checkin，峰值同时2，累计持有3942.906/3763.089ms，与B报告数值一致。**但监听器在 finished=clock() 后仍启用，后续 service.get、repo.load 和 actions 回读也被计入。** 两样本各4对完全位于 envelope 结束之后。

| 精确范围 | 无延迟 | 受控等待 |
|---|---:|---:|
| envelope内完整连接持有区间数 | 205 | 205 |
| envelope内累计持有 ms | 3903.391 | 3724.269 |
| envelope后验证回读区间数 | 4 | 4 |
| 全部观测最后checkin相对原点 ms | 13619.591 | 13480.044 |

因此209是**整段测试采样**数量，不能直接描述为13秒执行区间连接数。它不是事务计数，也不是SQL CPU或锁等待统计。checkout发生于建立连接之后；checkin也不覆盖完整物理关闭阶段。NullPool 使重复物理连接的建立/关闭成为真实候选解释，但目前未测其时间，不能直接将约9秒残差归给握手。

DB持有区间与Adapter和彼此可能重叠，不能与Adapter累计时间相加来分解总时长；后续测量必须使用相同 envelope 并按区间交集/并集处理。13秒残差也包含上下文、准入、事件投影、治理、持久化和清理，当前不能认定某个Owner主导。

只有一次无延迟、一次人工等待且性质不同的样本，没有可靠P95、自然噪声区间或生产吞吐数据。额外210ms人工等待被其他波动掩盖，不是加延迟更快的证据。定点Step/Slot held采样证明等待时资源仍在途，未证明等待排队、CPU饱和或生产容量不足。NullPool、关闭Skills、synthetic模型及max_tokens512进一步限制外推。

## D. Original WP2 Decision

`ORIGINAL_WP2=DEFERRED`；`CI_PARALLEL_OPTIMIZATION=NEED_MORE_EVIDENCE`。

1. 源码 `agent_loop/batch.py:122` 在policy>=2、两recipe concurrent_safe且资源条件满足时走两项wave；将业务batch与指定只读spec上限改为2，可允许这组合法调用重叠，仍受原线程/Tool许可约束。安全可达性成立，实际重叠未运行验证。
2. 本体收益约54ms，业务关键路径价值未证明；外围开销尚未量化，不能认定总收益必小。
3. Fixture固定产生两调用，无法估计真实频率；本轮也不另开真实模型质量/频率实验。
4. 并发可能增加瞬时连接需求、Frame/Root行锁争用及`asyncio.to_thread`/Tool线程压力。单Run串行样本峰值2不能证明并行或多Run容量安全。
5. WP1 PASS不自动授权WP2。延后不是永久否定，也不保留另一条可同时实施的WP2。

## E. Stage20 Architecture Direction

**唯一推荐：TARGETED_MEASUREMENT。** 先区分当前13秒是否主要受测试连接生命周期影响，还是存在可定位的执行器调度等待；不立即修改调度。

SYNC_BLOCKING完整driver占线程、正常在途Run占admission Slot、持久挂起可释放资源、Run级跨进程恢复、恢复/HTTP容量入口差异、同Run无独立跨Worker claim及DAG整批屏障，仍是首次审计中的结构事实。当前CI为单Agent Step，不能把13秒自动归到DAG屏障；无真实长外部I/O、恢复压力或多Run负载，无法证明需要事件化/跨Worker分发。

本次取代首次报告中“优先局部并发”的条件性方向：新增运行证据优先要求辨明测试装配成本。并发优化、等待模型调整和数据库重构均未开放实施。不引入Kafka/Redis Streams/Temporal、新Worker、第二执行平台或新模型/计量底座。

## F. Next Task Contract

### 目标、进入条件与现有能力

任务名：**CI单次调查连接生命周期与Step排队耗时归因**。Owner：CODEX_B。

唯一核心问题：**当前约13秒是否主要由测试NullPool连接生命周期造成，还是存在在普通pool下仍显著的Step执行器排队，从而有证据值得重新讨论调度/等待模型？** 不要求将所有残差完整归因。

进入条件：用户/网页总控下发该任务；当前WP1代码及合法fixture可用；明确独占本机`_test`数据库，无其他依赖同库的测试/服务；遵守setup并安全验证目标，不打印DSN凭据。当前已存在原服务全链路、Adapter/批次时间线、checkout/checkin与资源采样，不重新建设profiling系统。

### 可修改与不可修改范围

- 仅新增一个按能力命名的定向测试，如`tests/test_ci_execution_timing_attribution.py`，复用既有build/ObservedInvestigator；允许本任务Handoff及小型JSON证据。保留WP1原测试与产物不变。
- 允许测试内对独立Database实例使用现有`DatabaseConfig(use_null_pool=False)`，采用现有默认pool参数；只在该测试自己的同一async loop中创建、使用、dispose，绝不跨fixture loop共享连接或修改全局_pg_fixtures。
- 不改任何生产源码、并发/spec/业务版本、Settings、schema、预算、deadline、准入/Validator、Skills、Provider、依赖或评测侧协议。不并行派发Evidence、不做负载/恢复/HITL矩阵，不调用远程模型/企业CI。

### 两个且仅两个新增测量边界

1. **连接生命周期**：仅针对本测试engine，以测试级包装记录原dialect.connect与do_close实际调用的进入/返回（保留原参数、返回、异常和await桥语义）；复用checkout/checkin，记录相对service.start原点的区间。必须覆盖真正驱动连接调用，不能仅用pool connect事件回调耗时冒充握手耗时；若无法无侵入测到，明确缺口并停止，不扩建底座。按envelope裁剪并分开“连接建立/关闭”“checkout持有”，交叠不得重复加总。
2. **Step执行器边界**：仅在测试实例包装`ParallelExecutor._invoke:692`以及传入driver.execute，保留原调用，记录_invoke进入、driver实际开始/结束、_invoke返回。将前段标记“执行器提交/排队包络”，不冒称纯线程队列时间；driver结束后的返回差额单列。沿用已有Model/Tool/Batch时间锚点，不再向Context/Prompt/每个repository方法加埋点。

service.start原点、producer结束为统一envelope，验证回读排除；两处包装在finally恢复，连接按实际开始时间关联，不以当前全局phase标签猜测并发归属。保留正常Run Truth与业务验证，采样不能改变结果或控制执行。

### 最小运行与次数

单Run、无人工sleep、同一公开场景/synthetic策略/Skills关闭/32768预算。**最多4个定向样本：NullPool、普通pool、NullPool、普通pool，顺序运行**；每样本独立engine，在所属loop内dispose，各自正常清理测试数据。schema准备与engine构造不计入envelope；计时起点前不预热pool，标记冷启动和执行中复用，避免人为偏袒普通pool。

预期一次命令：`uv run python -m pytest tests/test_ci_execution_timing_attribution.py -q -s`（4样本，不是全仓测试）。两次重复只用于识别数量级与方向一致性，不计算P95或统计显著性。若首样本功能/采样失败，停止定位，只重跑受影响样本；不补跑大型矩阵。

### PASS / GO / NO-GO / BLOCKED 与停止规则

- **实验PASS**：四样本原业务、Root/Step/Final、准入/预算、COMMITTED/ordinal/cursor与Validator合同成立；两个边界和统一envelope数据完整，输出可复算。PASS允许结论为NO-GO，不表示性能改善或实现批准。
- **支持GO（仅进入Codex-A最小调度决策，不是实现GO）**：两个普通pool样本均出现同一可定位的执行器提交/排队包络，且每次均至少500ms并占该envelope至少10%；连接生命周期不能解释这一等待，并能从已有锚点排除将driver内部工作误算为排队。阈值是本任务预先约定的筛选标准，不是生产SLO。若包络不能区分提交成本和真实排队，不能据此GO。
- **支持NO-GO（当前调度改造）**：普通pool两样本中执行器前段等待均不满足上述实质性门槛，且NullPool/普通pool差异方向一致、连接生命周期区间支持解释主要数量级差异；保持生产现状。若连接建立/关闭区间并集占NullPool envelope至少50%，可明确标记测试连接成本主导，但不能据此改生产pool或SQL。
- **不确定时停止**：若多数残差仍在driver内部、样本方向冲突、差异被噪声掩盖或测量无法解释，不再追加测试/埋点。交回`INCONCLUSIVE`并建议`NO_JUSTIFIED_CHANGE`，不以残差推测Context/数据库/事件平台问题，不自动开展第三实验。
- **BLOCKED**：无法确认测试库独占/安全目标、合法准入或原业务失败、无法保留原语义测量。保存最小证据并停止，不改合同制造通过。

本实验没有并发Candidate，**任何结果都不直接把原WP2变为GO**。若发现的显著成本属于Action周边，也只记录为待Codex-A判断的证据；不得自动增加第三个测量边界或跨入优化实现。

### 不变量、回滚和证据

始终保留AgentCore Run/Step Execution Truth、单Run权威Owner、Tool Governance、Approval/HITL、Idempotency、UNKNOWN/Reconciliation、Lease/Fencing、Context/Observation一致性、OutputGate/CI Validator与AgentEvalOps Evaluation Truth。保留完整批次提交、授权证据来源、budget/deadline及恢复合同。

无需独立生产回滚机制：不改变生产行为，测试包装在finally恢复、测试engine在同loop关闭即可；不对活跃业务库执行回滚/改写。

保存四样本JSON（身份、pool配置安全摘要、统一时间原点、两个边界完整区间、既有Adapter/Batch锚点、业务/准入/预算/动作/Observation核验摘要）、定向命令/真实退出码/pytest摘要、Git差异与单一决策。仅公开合成数据，不保存secret或完整Prompt。不生成长期监控框架或大篇设计文档。完成该工作包后停止，交独立复核，不实施下一WP。

## G. Final Decision

```ini
WP1_INDEPENDENT_GATE = PASS
WP1_FUNCTIONAL_BASELINE = VERIFIED
PERFORMANCE_EVIDENCE = INSUFFICIENT
CI_PARALLEL_OPTIMIZATION = NEED_MORE_EVIDENCE
ORIGINAL_WP2 = DEFERRED
STAGE20_DIRECTION = TARGETED_MEASUREMENT
NEXT_TASK_OWNER = CODEX_B
NEXT_TASK = CI单次调查连接生命周期与Step排队耗时归因（两个测量边界，最多4个定向样本）
IMPLEMENTATION_READY = NO
UNRESOLVED_RISKS = [NullPool物理连接耗时未测, driver内部残差未归因, 真实双Evidence调用频率未知, 并行与生产容量收益未验证, 测试库须独占]
```

`IMPLEMENTATION_READY=NO`指生产并发/调度改造尚未就绪；下一次测试级归因实验的任务边界已具体化，等待总控下发，不在本轮执行。

```ini
BREAKING_CHANGE = NO
OLD_BEHAVIOR = CI batch concurrency=1 / Evidence spec默认max_concurrency=1
NEW_CANONICAL_BEHAVIOR = 未改变；本轮只读复核并新增本报告
OLD_TESTS_REMOVED_OR_ARCHIVED = NONE
COMPATIBILITY_REQUIRED = NO（无接口变更）
```

本轮未修改生产代码、既有/新增测试、Codex-B报告或JSON；未commit/push/切换分支。AgentEvalOps无修改。本次审查到此停止。
