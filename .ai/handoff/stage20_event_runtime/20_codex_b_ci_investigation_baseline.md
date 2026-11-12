# Stage20 WP1：CI 调查受控基线与并发必要性验证

日期：2026-11-05（Asia/Shanghai）。角色：Codex-B。仅 WP1，未进入 WP2；不是 Final Gate。

结论：**WP1 PASS；WP2 INCONCLUSIVE，当前证据不支持直接 GO。** 原业务 start → 原 Runtime → 原工具 → 原 Validator → Investigation READY/COMPLETED 路径已运行成功。两个 Evidence 读取具有并发安全前提，但实际生产模型形成双调用的频率及生产连接池下的收益未知。下面所有性能数值仅属于本机合成受控实验。

## A. Execution Baseline

| 仓库 | 分支 / HEAD | 开始时状态 | 结束时状态 |
|---|---|---|---|
| `D:\PythonProject\Local_Agent` | `main` / `f2d630091c942b1e57b0d4c14c1e0b6e8208b894` | tracked/staged diff 空；仅原有 `data/` 未跟踪 | 新增一个测试文件；原有 data 原样保留；Handoff 在现有 ignore 范围 |
| `D:\PythonProject\AgentEvalOps` | `stage12-go-productionization` / `0fab7fbcfc8475992f5979a892193d13c6118fe0` | 干净 | 干净 |

已读取两仓根 AGENTS.md、`.ai/setup.md` 及 `10_codex_a_execution_architecture_audit.md`，目标路径内无更深 AGENTS.md（LocalAgent `rg --files -g AGENTS.md` 仅根文件）。Git status、branch、HEAD、diff、cached diff 均核对；未切分支、提交、推送、覆盖用户文件。

审计与源码一致：`core/agent_platform/ci_diagnosis.py::definition()` 当前 max_batch_size=2、max_batch_concurrency=1；`ci_diagnosis_tools.py::CIDiagnosisTool` 沿用 `ToolExecutionSpec.max_concurrency=1` 默认值（`core/runtime/tool_contract.py:357`）。`agent_loop/batch.py` 仍为有限 wave + 整批提交。没有发现阻断 WP1 的关键路径漂移。

环境核对：现有测试配置经 `tests.conftest` 安全加载，不输出 secret；DSN 经 make_url 检查为本机 `127.0.0.1:5433/localagent_test`、后缀 `_test`，只读连通性查询确认 PostgreSQL 16.15。监听进程为 postgres。Docker daemon 未运行，但本机现有 PostgreSQL 可用，不需要 Docker，也未安装/启动新服务。未读取 `.env` 原文或打印密码。

使用现有 `tests/_pg_fixtures.py`：pg_schema 只在测试数据库重建既有 schema，clean_database 在测试之间清理合成数据；本轮没有修改 migration。Database 使用 NullPool，测试专用配置不等于生产 pool。没有启动完整 backend，没有 HTTP 服务、真实模型/企业 CI/外部重跑、OperatorOnly/Holdout 访问或全局环境修改。

## B. Canonical CI Investigation

实际运行入口为新增测试中的 **`CIInvestigationService.start(principal, incident_id, request_id)`**，非孤立工具测试：

```text
公开 report_pipeline_failure 场景
→ IncidentService.create_scenario / execute（现有 setup_incident）
→ CIInvestigationService.start
→ 原 RunExecutionSupervisor.acquire_slot / spawn
→ 原 incidents.admit + RUN/CONVERSATION 授权绑定
→ AgentApplicationService.stream → ChatService
→ CoordinatedRuntimeFactory → RunCoordinator → ParallelExecutor
→ AgentLoopDriverV2 → 合法 synthetic ModelProfile / 原模型准入
→ Context Tool → 同批 Parser/Environment 两 Evidence → 下一批 Case Evidence
→ 原 COMMITTED Action / 整批 Observation → Final Output / OutputGate
→ CIInvestigationService.project_if_terminal → 原 validate_report
→ IncidentService.seal → service.get：COMPLETED / SUPPORTED
```

这是**进程内业务服务入口 E2E**，不是完整 server.py::lifespan + HTTP 路由/认证端到端验收；Supervisor、Application、Runtime、工具、授权持久化、Validator 和 PostgreSQL 都使用正式实现。

隔离 Fixture 边界必须保留：复用 `tests/test_ci_adaptive_stop.py::build()`，该既有测试装配移除 skills 与 skill tools，只保留两个 CI 工具；本次显式把其 131072 测试 token 额度还原为 **32768**，max_batch_size=2/concurrency=1、180s deadline、8 次模型/工具上限未提高。模型为原合法 synthetic `BOUNDED_ESTIMATE / utf8-byte-upper-bound.v1`，context_window=65536、max_tokens=512，非真实远程模型 4096 输出配置。该受控 Profile 不能证明真实 Provider 质量或完整 skills 装配的生产耗时。

`ObservedInvestigator`（`tests/fixtures/adaptive_execution_server.py:36`）不持有执行 manifest、GT 或预制报告；其输入仅为原模型 transcript 中的真实已提交工具 Observation。先从 context 取得 evidence_id，再读原文；minimum_coverage 通过后才基于观察组装确定性报告。未用 `InvestigationProvider.report()` 的 manifest 预制答案制造成功。`real_model=True` 只是让这次合法受控模型装配通过 CI 服务可用性开关，不代表进行了真实模型调用，也未绕过 ModelProfile/request assessment。

build 本身会建立一个未执行的预备 Investigation；start 新建并执行第二个 Investigation，所以 report_version=2。测试没有把预备行伪造成已完成事实，所有 action/report 均绑定 start 返回的新 run_id。此差异不作为生产多调查吞吐样本。

| 结果层 | 无人工延迟 | 受控等待样本 |
|---|---|---|
| 持久 Root / Step | SUCCEEDED / SUCCEEDED | SUCCEEDED / SUCCEEDED |
| Agent Final receipt | DELIVERED | DELIVERED |
| 原 CI Validator | PASS | PASS |
| Investigation | READY，查询 COMPLETED / SUPPORTED | READY，查询 COMPLETED / SUPPORTED |
| 模型 / 工具次数 | 4 / 4 | 4 / 4 |
| 标准模型准入 | 4 条 reliable=true/admissible=true | 同左 |
| 整批 Observation cursor | 1,2,3；第4轮 FINAL CLOSED | 同左 |
| token 使用 / 上限 | 28257 / 32768 | 28257 / 32768 |
| AgentEvalOps 真实评测 | NOT_EXECUTED | NOT_EXECUTED |

注意当前查询合同：已 READY 的 `project_if_terminal()` 直接返回 `(row, None)`，故后续 get 的 runtime_status=None。不能据此认定 Runtime 未完成；本次从独立 repository.load().root.status 验证 SUCCEEDED。未修改此生产行为。

可追溯身份：

| 字段 | 无人工延迟 | 受控等待样本 |
|---|---|---|
| Run | `7e990092-c93f-4811-9413-4811bba096ef` | `c53c50aa-d260-40dd-9bf0-4f910b43d9db` |
| Investigation | `13e52eff-70f6-4450-aa56-cd9931aa00f6` | `413d16c8-6b1f-4af7-9ba0-f629ec10de27` |
| Incident | `4928e6b7-a65c-41fb-9dcc-064081934b06` | `c33e9612-b4e9-4b3a-8c40-c81ebdc19f68` |
| report_version | 2 | 2 |

完整 execution/session/action/evidence/final decision/digest 身份及采样见同目录 `20_codex_b_ci_investigation_baseline_observations.json`。原始 pytest 临时路径为 `C:\Users\GemHr\AppData\Local\Temp\pytest-of-GemHr\pytest-2321\test_ci_investigation_serial_r0\ci_baseline.json` 与同级 `test_ci_investigation_serial_r1\ci_baseline.json`，可由测试打印的 BASELINE_ARTIFACT 复核；测试库会被未来 clean_database 清理，报告与 JSON 才是本轮保留证据，不依赖库长期保留。

## C. Evidence Independence

静态审查结论：**CONFIRMED（当前两个只读读取的并发安全前提），未运行 Candidate 并行派发。**

1. `ci_diagnosis_tools.py::invoke_once()` 只校验取消、经 SyncPersistenceBridge 调用 bound_execution、Pydantic 校验后 JSON 序列化。共享 adapter 仅保存 service/bridge/spec，无调用间累加可变状态。
2. `core/incidents/service.py:154::bound_execution()` 每次独立 `async with database.session()`；`core/persistence/database.py:202` 每次由 sessionmaker 新建 AsyncSession，autoflush=False，结束关闭。不同读取不共享 Session/ORM identity map。
3. 每次独立读取 Investigation、Incident、RUN Ownership、CIExecution；只做 SELECT，不调用 admit/seal，不做 FOR UPDATE，不修改 Incident/Investigation/Run 状态，也不更新“上次读取”缓存。Tool Runtime 的 start/commit/action 持久化是另外的执行事实，仍由原 Owner 管理。
4. `evidence()` 通过当前授权 execution 的 evidence_manifest 精确选择 ID，返回新顶层 dict；内部嵌套值只用于验证/序列化，不修改。Session 也不跨两次调用复用。两个 ID 均来自本次 context，后一个读取不依赖前一个更新运行时数据。
5. owner/tenant/user/agent/version/session 检查每次仍执行；证据返回保留 execution_id、digest、availability、来源，未绕过 `ci_evidence.observed_sources()` 的 committed source/digest/Observation 及最终 minimum_coverage 校验。
6. ToolInvocation.arguments 可明确表达两个不同 evidence_id；resource_key 当前 **None**、requires_resource_key=False，两个读取不竞争业务资源锁。无 resource_key 表示没有显式互斥约束，不能单靠它证明安全；安全依据是上述真实只读路径。共享的是进程级 tool semaphore 与 PostgreSQL pool，非相互依赖的业务状态。
7. 当前 spec=1 和 CI concurrency=1 都阻止重叠；batch recipe.concurrent_safe=False 已由本轮持久 Action 验证。未来若两处改2，NONE/read-only 与不同 arguments 满足现有 wave 规则，无需新增资源锁或改变 evidence identity。未修改 spec、版本或参数。
8. 原 ToolExecutionService 在工具调用外持有独立 invocation 身份、许可、预算与 durable fence；同一 Run 的 Frame/Root 提交仍会串行锁定，未来提并发不能消除这类争用。

依赖分类：context 先完成是本受控 Agent 获取合法 ID 的业务依赖（INSTRUCTIONS 要求先目录；现工具本身并无“必须先 context”的计数开关）；双 Evidence 互相无依赖，当前串行来自配置/调度。最终模型须等整批 Observation；`core/runtime/agent_loop/batch.py:122` wave gather 屏障和 `core/persistence/repositories/agent_loop.py:181::commit_batch` 保留完整 ordinal group，不能提前提交半批结果。后者逐项核对 COMMITTED 来源后一次递增 cursor。

## D. Baseline Timeline

下表均是单调时钟相对 `service.start()` 调用前采样原点的毫秒。started/finished 是 **Adapter.invoke_once 边界**，不是把整个 ToolExecutionService 持久化成本算进读取；Batch committed 是原数据库提交函数返回时刻。

| 动作 | 无延迟 start → finish | 受控 start → finish |
|---|---|---|
| Context | 3705.752 → 3765.177 | 3447.293 → 3504.774 |
| Evidence A / PARSER_LOG | 6690.326 → 6746.356 | 6358.628 → 6426.618 |
| Evidence B / ENVIRONMENT_RECORD | 7466.534 → 7520.623 | 7178.759 → 7248.940 |
| 第二轮整批提交 | 7898.952 | 7644.247 |
| CASE_REQUIREMENTS | 10343.515 → 10398.194 | 10232.817 → 10301.352 |
| 三个工具批次提交 | 4144.314 / 7898.952 / 10787.400 | 3876.297 / 7644.247 / 10694.284 |
| producer 完成 / 资源释放后观察 | 13397.999 | 13263.959 |

A finish < B start，且第二轮 committed > B finish，ordinal=[0,1]，cursor 只增加一次。A/B 的 Action 与 Evidence ID 可在 JSON 中一一对应；无延迟 A=`eaab39fe6f4853cc8b8cf54ddddacdc6`、B=`9b6df926ab4159d7954b47aed19b5be1`。

资源实测与边界：

| 资源 | 本轮观测 | 不能据此宣称 |
|---|---|---|
| Run admission Slot | 两次模型和工具样本全为 held=true，producer_tasks=1；结束 held=false/tasks=0 | 未记录 semaphore.acquire/release 的精确时间戳；总耗时含 start 前置查询，不能当精确 Slot 秒数 |
| Step blocking executor | 每个模型/工具/async DB 读取采样 active=1、kind=RUNTIME_STEP；结束 active=0 | 定点采样不是完整连续 profiler，不表示 CPU 满载 |
| 同步模型等待 | 受控样本每轮模型入口 sleep(30ms)，前后 Step=1、Slot held；模型 adapter 总121.293ms | 不是实际模型 API 延迟，也不代表真实网络模型占比 |
| 同步 Tool 等待 | 证据服务在 event loop await sleep(30ms) 后再原样读 PG；等待前后 Step=1、Slot held、permit=1 | await 没有让事件循环忙等；只是同步调用方仍占执行线程 |
| Tool 许可 | 4 个 invoke_once 采样均1，模型时0，结束0 | adapter duration 不是整个许可生命周期，未测排队分布 |
| PostgreSQL connection | checkout→checkin 209次/样本；同时峰值2；持有时间累计3942.906 / 3763.089ms | 非事务数、非服务端 SQL CPU；不含新建连接之前的握手时间，NullPool 不代表生产 pool |
| Step/Slot 占用下界 | 从首模型到末模型结束至少9497.845 / 9663.955ms，源码表明正常 driver 未返回持续占 Step 线程 | 精确 Step 线程总占用时长 NOT_MEASURED；不能把 DB Step.updated_at-created_at 当线程时长 |
| 持久挂起/恢复 | 本轮正常路径未发生（后一个 Run 回读 recovery_attempt_count=0）；源码有 LoopSuspended 释放 lease / 返回 driver / scope清理 | 挂起释放及跨进程恢复动态实验 NOT_EXECUTED |

Step 结构依据：`runtime_factory.py:430` → SYNC_BLOCKING；`parallel_execution.py:692` 把整个 driver.execute 交 BoundedBlockingExecutor，再 await result_async；`event_emitter.py`/`sync_bridge.py` 同步等 future；Supervisor `server.py:295–346` 从准入到消费流 finally 才释放。Tool Adapter 与模型 Adapter 的采样 thread ID 可以不同，不能把 adapter thread ID 当作 Step Owner 身份。两个读取的间隙也包含治理/持久化/投影开销，不仅是 adapter 的55ms。

受控样本额外只读回查持久字段（完整见 JSON）：Root created_at=`2026-11-05 15:15:13.333723+00:00`，updated_at=`15:15:25.273949+00:00`，deadline=`15:18:12.468451+00:00`；Root/Step 都 SUCCEEDED。Evidence A durable started→committed=200.070ms，B=213.501ms，含测试延迟及持久化；这与 Adapter 的67.990/70.181ms 是不同范围，不混算。模型 durable started→completed 累计约1287.718ms，也不能与 adapter 121.293ms 相加作为互斥分解。第一次样本已被下一测试 truncate，未补造其持久时间戳。

NOT_MEASURED：精确 Step/Slot 释放时刻、CPU 利用率、PG SQL/锁等待/事务数量、生产连接池等待、外部真实 I/O、生产并发负载。没有为缺失项扩建 Metrics 系统。

## E. Performance Opportunity

| 分解（互斥 adapter 口径） | 无延迟样本 ms | 受控等待样本 ms |
|---|---:|---:|
| 模型 adapter 总耗时 | 0.456 | 121.293 |
| 全部4个工具 adapter | 224.223 | 264.187 |
| 其中3个 evidence adapter | 164.798 | 206.706 |
| 其他（总耗时减上述模型及全部工具） | 13173.320 | 12878.479 |
| 全链路 envelope | 13397.999 | 13263.959 |

无延迟 evidence adapter 占比约1.23%，可双读的 A+B 合计110.119ms（约0.82%）。若仅把这两个读取本体完美重叠，静态公式 `(A+B)-max(A,B)` = **54.089ms，约该样本总耗时0.40%**。它只是读取本体、不含 Runtime 持久化的理论节省，**不是 WP2 全部优化收益的上界或已测加速比**。若 Action 周边数据库工作也可重叠，可能有额外收益，也可能因 Frame/Root 锁与 pool 争用抵消；本轮没有 Candidate，不能量化。

两个总耗时相差134.040ms（约1%），但一个样本刻意增加模型/工具等待，不能据此估算自然噪声置信区间，更不能说 sleep 使性能更好。无延迟仅一次，未建立可靠统计分布/P50/P95。额外受控延迟总210ms（4次模型+3次Evidence各30ms），仍被样本间其他成本波动淹没，说明54ms级的直接收益在当前装配里不可辨识。

“其他”占主导，仅能说原 Runtime 多次持久化/上下文构建/NullPool 连接建立等未细分部分占主导，不能把全部13秒归罪于 SQL、CPU或某个 Owner。DB累计持有约3.9秒与 adapter 范围重叠，不能再加到表中。

模型生成双 Evidence 在此确定性 Fixture 两次都出现，可证明协议与 Runtime 可达；它是预设策略，不能推导正常生产出现概率。真实模型质量、工具调用分布和真实模型耗时均 NOT_EXECUTED/NOT_MEASURED。因此不能断言“真实模型一定主导”，也不能断言“双调用频繁”。

资源上限：本 Fixture Step executor 默认4 worker/8 pending，ToolConcurrencyController 默认全局16；production Settings 默认 Run12、blocking4/8、PG pool5+overflow5，仅为源码默认，未测部署或饱和。局部工具2并发未明显超出这些静态上限，但仍需要原工具线程容量、Pool/Frame锁争用实测，不能以单 Run峰值2证明全局容量安全。

**WP2_RECOMMENDATION=INCONCLUSIVE**。可独立读取、安全前提成立；但当前量到的直接读取收益很小，生产 Batch 频率未知，现测试 NullPool 的事务/连接成本与生产不同。总控若只基于本证据决策，应暂不下发 WP2；也不据此要求事件平台、Runtime 重构或新监控框架。

## F. Tests and Changes

唯一源码树新增文件：`tests/test_ci_investigation_baseline.py`，一个按业务能力命名的测试、两个参数样本，复用原 build / ObservedInvestigator。只在测试内包装采样并调用原函数，无生产模块/spec/并发参数/Validator/token基础设施变更。

实际命令及结果：

1. 安全环境只读探针：`uv run python -`（通过 tests.conftest 加载配置，make_url 限定本机+_test，SELECT current_database()/inet_server_port()/version()）；退出0，可达。`docker ps` 失败（daemon pipe不存在），不影响本机 PG，不是测试失败。
2. `uv run python -m pytest tests/test_ci_investigation_baseline.py tests/test_ci_incident_diagnosis.py::test_investigation_binds_scope_and_seals_only_observed_report tests/test_ci_json_output_and_step_terminal.py::test_unreliable_remote_rejected_without_http_or_model_start -q -s`：退出1，**2 failed, 2 passed, 1 warning, 40.72s**。
3. 新增测试两失败原因都是错误期待“封存后的 get.runtime_status=SUCCEEDED”。真实业务已 COMPLETED，修正测试为读取 repository Root 成功状态；保留原 Investigation/Final/Validator断言。没有修改生产接口去满足测试。
4. 仅重跑失败子集：`$env:PYTHONIOENCODING='utf-8'; uv run python -m pytest tests/test_ci_investigation_baseline.py -q -s`：退出0，**2 passed, 1 warning, 34.45s**。未重跑此前已通过的两项已有测试。最终有效定向用例4项通过。
5. 只读持久时间字段回查：`uv run python -`，按受控等待样本唯一 run_id SELECT；退出0，未改业务持久数据。
6. `git diff --check`：退出0；新增未跟踪文件另用 `git diff --no-index --check -- NUL tests/test_ci_investigation_baseline.py` 检查，退出1为 no-index 有差异语义，无 whitespace error，仅现有 Git LF→CRLF 提示。两仓最终 status/diff 复核未发现生产变更。

warning 为既有 `PytestAssertRewriteWarning: Module already imported so cannot be rewritten; tests._pg_fixtures`，来自现有 test fixture import，不影响执行；不为此扩大清理。

本轮运行了已有来源/权限/被篡改 digest 拒绝测试，以及不可靠远程准入在发 HTTP/model START 前拒绝的测试。新测试验证完整业务、Run/Step/Final、持久COMMITTED动作、ordinal与整批cursor、合法预算/准入、Slot/Step/permit占用及释放。没有重新开发 Approval/HITL/UNKNOWN/fencing矩阵。

NOT_EXECUTED：真实模型、完整 HTTP E2E、AgentEvalOps Go Worker/实际评测、生产容量、Candidate并行、crash/recovery/HITL/UNKNOWN运行测试、全仓pytest/完整CI。批次恢复/审批/fence 只引用当前生产源码及审计已有静态证据，不能冒称本轮动态回归。

AgentEvalOps 仅确认 `internal/provider/ci_business.go` 受控HTTP/Evidence接入、`internal/agentquality/ci_diagnosis_v2.go` / `ci_diagnosis_sources.go` 的严格来源与digest读取规则、`adaptive_execution.go` 的版本化消费；未读取 operator资产，未改跨仓协议或共享执行状态表。AgentCore继续拥有Execution/CI Business Truth，Evaluation Truth仍归AgentEvalOps。

Breaking Change 记录：

```ini
BREAKING_CHANGE = NO
OLD_BEHAVIOR = 当前 HEAD CI concurrency=1 / Evidence spec=1
NEW_CANONICAL_BEHAVIOR = 未改变；新增测试观测
OLD_TESTS_REMOVED_OR_ARCHIVED = NONE
COMPATIBILITY_REQUIRED = NO（本轮未发生生产接口变更）
```

## G. WP1 Decision

```ini
WP1_STATUS = PASS
CANONICAL_CI_PATH = PASS
BUSINESS_VALIDATOR = PASS
EVIDENCE_PARALLEL_SAFETY = CONFIRMED
BASELINE_MEASUREMENT = PARTIAL
WP2_RECOMMENDATION = INCONCLUSIVE
PRODUCTION_CODE_CHANGED = NO
AGENTEVALOPS_EVALUATION = NOT_EXECUTED
ARCHITECTURE_REOPEN_REQUIRED = NO
NEXT_ACTION = 将本报告交总控/Codex-A审阅收益证据与Fixture适用边界；停止，不执行WP2
```

PARTIAL 指精确线程/Slot时长、生产pool/锁压力和统计噪声未测，不是基线时间线缺失；现有可用采样、真实业务路径、Validator、安全前提及可复现测试已满足本WP最小判定，未声称生产性能改进。

需要 Codex-A / 总控判断的问题（本轮均未自行修改）：

- 是否值得为约54ms读取本体理论节省继续 WP2；Action持久化周边是否有足够可重叠成本，需按业务价值判断。
- 现有 synthetic/skills关闭/NullPool/max_tokens512 受控装配与正式生产模型/skills/pool的适用边界，是否足以支持下一WP；不得拿本报告直接作真实生产加速Gate。
- 已封存 get.runtime_status=None 是现合同行为，本次没有造成正确性阻断，不要求本WP修API。同步Step等待、持久恢复admission差异仍仅记录，未触发架构修改。

本次工作已停止在WP1；未提交、推送或修改生产并发配置。
