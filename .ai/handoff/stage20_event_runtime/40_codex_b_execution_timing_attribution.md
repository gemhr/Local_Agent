# Stage20：连接生命周期与 Step 执行器等待定向归因

交付日期：2026-11-06（Asia/Shanghai）。Codex-B。合同：30_codex_a_wp1_baseline_gate_and_scope_decision.md 与本轮用户提示。

**整体 INCONCLUSIVE：实际执行4次调查，4次均通过已执行的业务正确性断言；首样本缺少 Driver 时间锚点，只有3份完整测量，不能标实验 PASS。** 其余样本显示 NullPool 物理连接生命周期成本主导，普通 Pool 未出现显著 Step 提交包络。当前没有支持调度架构改造的证据；原 WP2 继续 DEFERRED。没有追加第五次调查或第三个测量边界。

## A. Scope and Environment

- LocalAgent：`D:\PythonProject\Local_Agent`，main，HEAD `f2d630091c942b1e57b0d4c14c1e0b6e8208b894`。初始 tracked/staged diff 空，未跟踪 data/ 与上一WP的 tests/test_ci_investigation_baseline.py。本轮只新增 tests/test_ci_execution_timing_attribution.py 和本报告/JSON。
- AgentEvalOps：`D:\PythonProject\AgentEvalOps`，stage12-go-productionization，HEAD `0fab7fbcfc8475992f5979a892193d13c6118fe0`，初始及结束干净。
- 已读根规则、setup、10/20/30 Handoff；上一WP JSON 已解析。旧测试及10/20/30产物 SHA256 前后完全一致，摘要见JSON。原 data/ 未改。未提交/推送/切分支。
- 本机127.0.0.1:5433/localagent_test，经make_url限制本机及_test；进程枚举未发现其他Python/uv/pytest/backend进程，预检pg_stat_activity无其他client backend。每次清理前再次检查其他连接数为0；拒绝xdist，顺序执行。这是共享测试库内顺序TRUNCATE，不是每例独立schema，也不是阻止外部未来连接的数据库级独占锁。
- 测试本地fixture复用 _truncate_all，不调用pg_schema；只读核对现存alembic_version与ScriptDirectory heads相同（0042_loop_adaptive_decision），未执行任何migration、创建schema或启动服务。
- 每样本独立Database在所属async测试loop创建/使用/dispose。普通Pool为DatabaseConfig(use_null_pool=False)默认参数：size5、overflow5、timeout5s、recycle1800s，NullPool仅改变use_null_pool。build的必要数据准备后执行engine.dispose重置空池；普通Pool验证checkedin=checkedout=0，t0前不再查询/预热。Dialect已在必要setup初始化，冷启动指物理连接从空池首次建立，不代表整个Python进程/SQLAlchemy首次初始化。
- 原build关闭Skills、synthetic BOUNDED_ESTIMATE、max_tokens512、公开report_pipeline_failure场景；32768预算、batch size2/concurrency1、Evidence spec1、原180s deadline与工具/模型上限不变。无真实模型、企业CI或OperatorOnly/Holdout访问。

## B. Instrumentation

边界A仅包装本样本engine.sync_engine.dialect实例connect与do_close。当前安装SQLAlchemy 2.0.52源码显示connect同步调用loaded_dbapi.connect，do_close调用dbapi_connection.close；包装保留参数/返回、原greenlet await桥和异常（try/finally只记时间，不吞异常）。建立返回后分配物理connection_id；checkout/checkin按connection record配对，不依赖全局phase猜测。物理建立/关闭、checkout持有均保存独立区间；SQL执行/锁等待不是本次新增测量边界，NOT_MEASURED。

边界B在本次scope.coordinator.executor实例包装_invoke，并在其中对实际传入的driver实例execute包装。原Driver始终执行；记录invoke_enter、driver_start/end、invoke_return。Factory为slots只读，测试使用临时类级create_run_scope安装钩子，identity检查仅对本次factory创建的scope安装实例包装，其余factory原样返回。所有monkeypatch由context恢复；Driver钩子在_invoke finally恢复。

submit_envelope=driver_start-invoke_enter；driver_duration=driver_end-driver_start；return_overhead=invoke_return-driver_end。前段包括提交前工作、asyncio.to_thread提交与执行器内部成本，**不是纯排队时间**。没有新增实际ThreadPoolExecutor.submit埋点，因此不能精确分解排队；但整个包络不到1ms已排除本次500ms筛选门槛。invoke前snapshot active=0/pending=0是定点采样，不代表完整容量分析。

沿用Model/Tool adapter和Batch提交锚点，仅调用原方法。单调时钟相对origin_ms；t0_ms为service.start前、t1_ms为producer完成后。验证get/repo.load/investigation/actions、JSON整理、最终dispose全部在t1后。所有区间按[max(start,t0),min(end,t1)]裁剪，排序合并求union；累计工作时间另外求和。记录原始全生命周期，保留回读及dispose而不混入执行统计。连接持有、建立/关闭、Driver、adapter区间可能交叠，不相加为互斥组成。

## C. Four-Sample Results

规定顺序实际执行：1 NullPool（业务通过，测量失败）；2普通Pool；3 NullPool；4普通Pool。首样本未保存完整trace，不补造其连接数据或Driver时间。以下均ms：

| 样本 | Pool | envelope | submit包络 | driver | return | 物理生命周期并集 | 占envelope |
|---|---|---|---|---|---|---|---|
| 2 | AsyncAdaptedQueuePool | 1323.255 | 0.414 | 1070.260 | 0.102 | 87.732 | 6.63% |
| 3 | NullPool | 12254.028 | 0.186 | 10501.254 | 0.118 | 8991.246 | 73.37% |
| 4 | AsyncAdaptedQueuePool | 1103.921 | 0.177 | 893.305 | 0.079 | 42.360 | 3.84% |

连接数据：

| 样本 | 区间 | envelope相交数量 | 累计ms | 并集ms |
|---|---|---|---|---|
| 2 | connect | 2 | 87.732 | 87.732 |
| 2 | close | 0 | 0.000 | 0.000 |
| 2 | holds | 192 | 1135.974 | 1133.693 |
| 3 | connect | 203 | 9131.572 | 8726.448 |
| 3 | close | 203 | 282.213 | 282.213 |
| 3 | holds | 203 | 3423.323 | 3410.574 |
| 4 | connect | 1 | 42.360 | 42.360 |
| 4 | close | 0 | 0.000 | 0.000 |
| 4 | holds | 191 | 939.863 | 939.863 |

- 样本2 Run `bf510147-c3a9-4484-8698-91169aa080f9`；全观测connect/close/checkout-checkin数量=2/2/197；dispose耗时3.377ms（envelope之外）。首连接开始/结束=0.214/43.370ms。
- 样本3 Run `ab17db83-11fe-469b-b983-7dee7e15cb41`；全观测connect/close/checkout-checkin数量=208/208/208；dispose耗时0.108ms（envelope之外）。首连接开始/结束=0.234/42.478ms。
- 样本4 Run `d07a3bb7-6f78-404c-a7a0-81ee44318cdf`；全观测connect/close/checkout-checkin数量=1/1/196；dispose耗时2.344ms（envelope之外）。首连接开始/结束=0.193/42.553ms。

普通Pool执行内关闭0次，最终dispose才关闭；样本2建立2条、192次checkout，样本4建立1条、191次checkout，体现正常复用。NullPool执行内203次建立/关闭/checkout，验证回读额外5次均排除；同一样本累计生命周期9413.785ms、并集8991.246ms，重叠使两者不同。connect+close各自union也不能直接相加冒充两类合并union。

三份完整样本均验证Root/Step SUCCEEDED、Final DELIVERED、Investigation READY/COMPLETED/SUPPORTED（原project_if_terminal调用原Validator）、4条可靠且可准入model assessment、预算未突破32768、4条Tool COMMITTED、两个不同Evidence与Action对应、ordinal[0,1]、Observation cursor[1,2,3]、FINAL CLOSED、串行及整批提交锚点。首样本同样走过这些业务断言，随后时间排序检查因缺driver_start_ms失败，未形成完整JSON。已封存get.runtime_status=None不被视为失败。

完整Model/Tool/Batch时间锚点、三份原始物理连接区间、Step四时点、身份、摘要与budget在同目录JSON，可直接重算；没有保存secret、Prompt或完整模型消息。

## D. Attribution

1. **NullPool成本主导：在唯一完整NullPool样本中确认。** 物理建立/关闭并集8991.246/12254.028=73.37%，超过50%筛选线。普通Pool生命周期仅87.732ms与42.360ms，整体envelope约1.32s与1.10s，对照支持连接重复建立解释主要数量级差异。与WP1约13秒、多次checkout观察一致。首个NullPool完整数据缺失，无法验证两次NullPool重复方向，不能把本轮整体标PASS或独立完成四样本归因。
2. **普通Pool前段包络不显著。** 0.414ms/1323.255ms约0.0313%，0.177ms/1103.921ms约0.0160%，两个都远低500ms且10%条件。driver内部约1070.260/893.305ms，不能当作排队。未证明纯队列等待或线程池饱和，也没有GO_FOR_ARCHITECTURE_REVIEW依据。
3. 连接持有及Driver内部仍有残差，不能据此归罪SQL、Context、Memory、Journal或Agent Loop。不新增第三边界，不拆内部模块。两个普通Pool也存在约219ms波动；各两次/一次完整样本不支持P95或统计显著性。
4. 只反映受控synthetic、Skills关闭、单Run本机实验，不证明生产DB故障或生产加速。原WP2继续延后，没有并行Candidate或真实模型分布证据。

## E. Test Evidence

唯一新增测试文件：tests/test_ci_execution_timing_attribution.py。新建本报告及小型原始JSON，其余文件未变。

实际执行：

- 只读安全探针与本地SQLAlchemy源码检查：uv run python -，退出0。其他DB客户端0，现schema已准备。
- AST语法检查 uv run python -，退出0（未执行调查）。
- 第一次 `uv run python -m pytest tests/test_ci_execution_timing_attribution.py -q -s -x`：退出1，1 failed/2 warnings/4.69s。安装factory实例方法时报AttributeError，只执行setup，**调查数0**。随后修正安装方式；fixture返回DatabaseConfig以避免失败repr输出DSN，报告不复制首次pytest自动输出的凭据。
- 第二次同命令：退出1，1 failed/2 warnings/16.81s。**调查数1**，业务断言通过，scope.driver与实际_invoke Driver不同导致driver_start_ms缺失。修正为包装实际传入Driver。此样本不伪报完整测量。
- 剩余样本命令：`uv run python -m pytest tests/test_ci_execution_timing_attribution.py -q -s -x -k 'not 1-null'`：退出0，**3 passed, 1 deselected, 4 warnings, 20.60s**。没有重跑已经执行的首调查；总调查数严格4，只有3份完整测量。
- JSON区间统计独立重算、旧文件SHA256保护核对：退出0。

warning：既有tests._pg_fixtures提前import的PytestAssertRewriteWarning、Alembic配置未指定path_separator的DeprecationWarning；只读取migration heads，没有运行迁移，不为warning修改无关配置。

NOT_EXECUTED：修复后样本1重跑、全仓pytest、Go/前端构建、真实模型/企业CI、完整HTTP E2E、生产压力/吞吐、HITL/恢复/UNKNOWN矩阵、任何优化Candidate。首样本缺失测量是当前未完成项；已到调查数上限，本次停止。

## F. Scope and Invariants

生产代码/并发参数/模型准入/Validator/预算/Owner/Schema均未修改。原Run/Tool/Context权威状态只经原生产函数正常推进，没有手工伪造状态或第二Authority。AgentEvalOps未修改或执行评测。

每样本Database均在自身async loop内dispose；临时Factory安装钩子、实例Driver/_invoke、dialect/adapter/repository采样由monkeypatch恢复，事件监听器移除；完整样本所有连接建立/关闭数量相等且无未归还checkout。首失败安装钩子由monkeypatch撤销，engine由外层finally dispose，未执行业务；第二失败经finally关闭engine和移除监听器。没有常驻后台任务或新增服务。

BREAKING_CHANGE=NO；OLD_BEHAVIOR=当前HEAD；NEW_CANONICAL_BEHAVIOR=未改变；OLD_TESTS_REMOVED_OR_ARCHIVED=NONE；COMPATIBILITY_REQUIRED=NO（无接口修改）。

## G. Decision

```ini
TIMING_ATTRIBUTION_STATUS = INCONCLUSIVE
CANONICAL_CI_VALIDATOR = PASS
NULLPOOL_CONNECTION_COST = DOMINANT
NORMAL_POOL_STEP_QUEUE = NOT_SIGNIFICANT
SCHEDULER_REVIEW = INCONCLUSIVE
ORIGINAL_WP2 = DEFERRED
PRODUCTION_CODE_CHANGED = NO
NEW_MEASUREMENT_BOUNDARIES = 2
SAMPLES_EXECUTED = 4
COMPLETE_MEASUREMENT_SAMPLES = 3
NEXT_ACTION = 交总控/Codex-A审阅本轮受限证据；NO_JUSTIFIED_CHANGE；停止，不追加样本/边界或实施WP2
```

DOMINANT仅指样本3完整实测；STEP_QUEUE字段表示可包含队列的提交总包络不显著，不代表测出了纯排队。由于首样本缺失，严格四样本实验完整性未达标，SCHEDULER_REVIEW保守INCONCLUSIVE；已有普通Pool证据不支持调度改造，保持生产现状。
