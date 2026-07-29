# 第 22 天第一轮：Snapshot Foundation

## 1. 本轮目标

本轮只建立版本化、不可变、可校验且不保存业务正文的 Snapshot 基础：

- `RunSnapshot` v1；
- Safe Plan / AgentState / Budget 投影；
- `PlanFingerprinter` 唯一 Owner；
- Snapshot SHA-256 v1 digest；
- append-only `InMemorySnapshotStore` 与 `SQLiteSnapshotStore`；
- Duplicate、Conflict、Corruption 和敏感数据测试。

本轮没有接入运行中的 Coordinator，也没有实现 Checkpoint Barrier、Scheduler
Pause、Journal Tail Reducer、Recovery Validator、Tool Reconciliation、自动 Replay
或自动 Recovery。

## 2. 修改前对象审计

### Plan / PlanStep

真实定义位于 `core/runtime/planning.py`。

`Plan` 当前字段：

- `plan_id: str`；
- `version: int`；
- `task_summary: str`；
- `steps: tuple[PlanStep, ...]`；
- `created_at: datetime`；
- `source: PlanSource`。

`PlanStep` 当前字段：

- `step_id: str`；
- `title: str`；
- `description: str`；
- `depends_on: tuple[str, ...]`；
- `completion_criteria: str`；
- `preferred_agent: str`；
- `capability_requirements: TaskCapabilityRequirements`。

`TaskCapabilityRequirements` 是静态有限配置，含七个 `requires_*` bool、
`risk_level` 和 `estimated_steps`。`Plan` 不含 `StepStatus`、结果、错误、attempt、
span 或 event sequence。

静态定义字段为 `plan_id/version/source/task_summary/steps` 及每个 Step 的真实字段。
`created_at` 是构造时间，不属于静态执行语义，因此不进入 fingerprint。

可能由用户输入派生的正文为 `task_summary/title/description/completion_criteria`。
项目目前没有独立的 `static_input` 或 `execution_kind` 字段。本轮没有给真实 Plan
新增这些字段；Safe Snapshot 对正文只保存摘要，并把当前唯一执行形态标记为
`AGENT`。

### AgentState / StepState

真实定义位于 `core/runtime/state.py`。

`AgentState` 当前字段：

- `run_id/schema_version/status/created_at/updated_at`；
- `steps/active_step_ids/stop_reason`；
- `final_output/error_code/error_message`。

`StepState` 当前字段：

- `step_id/name/status/created_at/started_at/ended_at`；
- `error_code/error_message`。

可能含正文的字段为 `final_output`、`AgentState.error_message`、
`StepState.error_message` 和 Step 的 `name`。`StepState` 没有 result，也没有真实
attempt counter。Snapshot builder 可由未来的真实 Owner 显式传入
`attempt_counts/step_results`；未传入权威 Attempt Count 时固定保存
`attempt_count=null`。`execution_started` 只表达 `started_at` 是否存在，两者不得
互相推导。

`AgentState` 没有独立 cancellation reason 字段；本轮只在 `stop_reason` 属于三个
真实取消原因时派生同值的 `cancellation_reason`。

### StepStatus 真实边界

真实值只有：

- `PENDING`；
- `RUNNING`；
- `SUCCEEDED`；
- `FAILED`；
- `CANCELLED`；
- `BLOCKED`；
- `SKIPPED`。

不存在 `CLAIMED`，本轮没有新增。唯一 in-flight status 是 `RUNNING`。
`BLOCKED` 表示前置条件失败、取消或无法满足，Step 未启动且在当前 Run 不再执行；
它是终态，不等同于 `RUNNING` 或等待中的 `PENDING`。

### Budget

真实结构位于 `core/runtime/budget.py`：

- Limits：`RunBudget` 的 `max_*` 字段，包括 Step、Model、Tool、Token、Cost、
  Retry、Retrieval、Document、Context、Elapsed 和 Concurrency；
- Used：`BudgetSnapshot.committed_usage: BudgetUsage`；
- Reserved：`BudgetSnapshot.reserved_usage: BudgetUsage`；
- Remaining：`BudgetSnapshot.remaining: BudgetUsage`；
- Ledger：`BudgetLedger._committed` 与受 Lock 保护的
  `_reservations: dict[str, BudgetReservation]`；
- Runtime Snapshot 还含 elapsed、remaining time、active reservation count、
  exhausted dimensions 和 generated time。

真实 Ledger 没有持久化版本字段，所以安全投影的 `ledger_version=1` 是 Snapshot
投影版本，不伪称为运行账本的恢复序列。

### Tool Side Effect

Side Effect 的真实 Owner 不在 `AgentState`：

- 枚举 `ToolSideEffectKind/ToolSideEffectState` 位于
  `core/runtime/tool_contract.py`；
- attempt 内的实时状态位于
  `core/runtime/tool_execution.py::AttemptSideEffectTracker`；
- `ToolExecutionResult/ToolExecutionError`、Runtime Event 和 Trace 只承载其安全
  结果或观测。

本轮没有把 Tool Side Effect 状态臆造进 State Snapshot，也没有实现
Reconciliation。

### Canonical JSON / Digest 与 SQLite 模式

修改前已有领域专用实现：

- `tool_contract.py::canonical_json_digest` 服务 Tool arguments；
- `event_journal.py::canonical_json/_digest` 服务 Journal record。

没有跨领域通用 Owner。Snapshot 新增自己的固定 v1 canonical JSON / SHA-256
实现，避免改变 Tool 或 Journal 的旧摘要兼容语义。

现有 SQLite Store 使用：

- `check_same_thread=False`、`isolation_level=None`、30 秒 timeout；
- `sqlite3.Row`；
- WAL 与 `synchronous=FULL`；
- `BEGIN IMMEDIATE` + 显式 `COMMIT/ROLLBACK`；
- `RLock`；
- typed safe error，错误消息不带 SQL、Payload 或 DB 路径；
- idempotent `close()`。

Snapshot Store 沿用该模式。

## 3. Snapshot Schema

`RunSnapshot` v1 包含：

- `snapshot_schema_version/snapshot_id/run_id/trace_id`；
- `plan_snapshot/plan_fingerprint`；
- `state_snapshot/budget_snapshot`；
- `last_journal_sequence`；
- `run_status/stop_reason/cancellation_reason/step_states`；
- `runtime_metadata/checkpoint_kind/quiescent/created_at`；
- `payload_digest`。

契约使用 `frozen=True, slots=True`，Mapping 在构造时转为
`MappingProxyType`。所有时间要求 timezone-aware UTC。version、sequence、
count 均显式拒绝 bool；数字拒绝 NaN/Infinity；journal sequence 不得小于 0。

`checkpoint_kind` 只是安全 token，`quiescent` 是调用方提供的事实位。本轮没有
Barrier，也没有自动判断 Runtime 是否 quiescent。

`RunSnapshot.__repr__` 只显示 schema、身份、sequence、status、checkpoint、
quiescent、时间和 digest，不展开 Plan/State/Budget。

## 4. Safe Plan Snapshot

`PlanSnapshot/PlanStepSnapshot` 只保存：

- plan schema、ID、version、source；
- task summary 的 present/length/digest；
- 排序后的 Step ID；
- agent ID；
- 排序后的 dependency IDs；
- 当前静态执行形态 `AGENT`；
- `TaskCapabilityRequirements` 的有限配置；
- completion criteria 的 present/length/digest；
- title/description 的 present/length/digest。

普通安全 ID 原样保留；不符合安全 token 语法的 ID 只保存 SHA-256 派生身份。
不保存 query、prompt、文件正文、Tool arguments、RAG/Memory 正文或自由文本异常。

项目没有真实 static input mapping，所以没有为了匹配任务说明而修改 Plan。

## 5. Plan Fingerprint

唯一 Owner 是 `core/runtime/plan_fingerprint.py::PlanFingerprinter`。

Fingerprint 包含：

- fingerprint schema v1；
- plan schema、ID、version、source；
- task summary digest 摘要；
- 按 `step_id` 排序的 Steps；
- agent、排序后的 dependencies、静态执行形态；
- 按 key 规范化的 capability 配置；
- completion criteria digest；
- title/description digest。

Fingerprint 排除：

- `Plan.created_at`；
- 所有 `StepStatus`；
- runtime timestamp、attempt、result、error；
- budget usage；
- model retry/fallback 结果；
- span ID、event sequence。

规范化使用 UTF-8、sorted mapping key、Enum `.value`、canonical JSON、
`allow_nan=False` 和 lowercase SHA-256。Step 和 dependency 顺序变化不影响结果；
静态字段或敏感正文 digest 变化会改变结果。

## 6. Safe AgentState Snapshot

`AgentStateSnapshot` 保存：

- state snapshot schema、真实 run status；
- stop/cancellation reason；
- 排序后的 `StepStateSnapshot`；
- final output 的 present/length/digest；
- safe run error code；
- AgentState schema version；
- updated_at。

它不调用或持久化完整 `AgentState.to_dict()`。`final_output`、error message 和
Step name 正文均不保存。

## 7. StepStatus 真实边界

`StepStateSnapshot` 保存：

- step ID、真实 status、in-flight 标记；
- attempt count；
- started/completed UTC 时间、duration_ms；
- safe error code；
- result 的 present/length/digest。

默认 `attempt_count=null`，不从 `started_at` 推导 0/1；Step 是否启动只由
`execution_started` 表达。未来接入时应由真实 Attempt Owner 传入精确 count。
Result 同理只能由真实 Owner 传入后做摘要。
错误码只允许大写安全 code 语法；不安全值降级为 `UNSAFE_ERROR_CODE`。

`RUNNING` 才是 in-flight；`BLOCKED` 强制为非 in-flight。

## 8. Budget Snapshot

安全 `BudgetSnapshot` 与 runtime 同名类型分属不同模块；包级导出名为
`SafeBudgetSnapshot`，避免覆盖现有 runtime `BudgetSnapshot`。

它按真实计量维度保存：

- `limits/used/reserved/remaining`；
- budget snapshot schema；
- ledger projection version；
- reservation count；
- generated_at。

`None` 明确表示 unlimited，remaining 也为 `None`，不使用 Infinity。有限维度
强制 `used + reserved <= limit`，并校验
`remaining == limit - used - reserved`。所有数值必须有限且非负。

Reserved 原样投影，绝不因 `quiescent` 标记而清零。不保存 Reservation 对象、
Lock、owner 或 callback。`max_concurrency` 是调度 admission limit，不是当前
`BudgetUsage` 的计量维度，因此不伪造成 used/reserved/remaining。

## 9. Runtime Metadata

严格 allowlist 只有：

- `runtime_schema_version`；
- `runtime_mode`；
- `planner_version`；
- `scheduler_version`；
- `model_routing_policy_version`；
- `tool_contract_version`；
- `retrieval_contract_version`；
- `event_schema_version`；
- `journal_schema_version`。

反序列化拒绝缺失或多余字段。值只允许有限长度的安全 token 或合法正整数版本；
环境变量、完整路径、用户目录、DB 连接串、API Key、Provider URL 和 Thread/Task
ID 无入口。

## 10. Snapshot Digest

`SNAPSHOT_DIGEST_ALGORITHM = "sha256-v1"`。Digest 覆盖除
`payload_digest` 自身以外的全部 v1 字段，包括顶层 `step_states`。

写入前、内存读取时和 SQLite 读取时都会重算。反序列化使用 strict JSON parser，
拒绝非标准 NaN/Infinity。任何结构或摘要不匹配都 fail closed 为
`SNAPSHOT_CORRUPTED`；不支持的 schema 为 `SNAPSHOT_SCHEMA_UNSUPPORTED`。

错误文本不拼接原始异常、Payload、路径或 SQL。

## 11. InMemory Store

`InMemorySnapshotStore` 实现公共 `SnapshotStore` Protocol：

- `save(snapshot)`；
- `get(snapshot_id)`；
- `latest(run_id)`；
- `list_for_run(run_id, limit)`；
- `close()`。

Store 是 append-only，没有 public update/delete。每次 save/read 都验证 digest，
使用 `RLock`，排序为 `created_at DESC, snapshot_id DESC`。`close()` 幂等。

## 12. SQLite Store

`SQLiteSnapshotStore` 主表使用：

```sql
PRIMARY KEY(snapshot_id)
```

并建立：

```sql
INDEX(run_id, created_at DESC)
```

每次 save 在单个 `BEGIN IMMEDIATE` 事务中完成查询、判重和插入；异常安全回滚。
重启后仍可读取并识别 Duplicate。读取时同时校验 JSON 内部 digest 与表行 envelope
中的 schema、snapshot/run ID、created_at 和 digest。

Store 没有集成到 `server.py` 或 `ChatService`，因为本轮只建立 Foundation，不提前
实施第 23 天 API/生命周期迁移。

## 13. Duplicate / Conflict

Save status：

- `SAVED`：新 ID 原子插入；
- `DUPLICATE`：同 ID 且同 digest，并且旧记录完整校验通过。

同 ID 但不同有效内容返回 typed `SNAPSHOT_ID_CONFLICT`。旧记录即使 envelope
digest 看似相同，只要 Payload 损坏也不会当 Duplicate，而是
`SNAPSHOT_CORRUPTED`。

## 14. Sensitive Data Protection

测试覆盖 Snapshot JSON、SQLite `payload_json`、`repr` 和安全错误文本。Snapshot
正文只通过 `TextSummary(present, length, digest)` 进入契约。

以下标记不会出现在持久化 Payload 或 repr：

- `SECRET_PROMPT_TEXT`；
- `MODEL_OUTPUT_SECRET`；
- `TOOL_ARGUMENT_SECRET`；
- `TOOL_OUTPUT_SECRET`；
- `RAG_CHUNK_SECRET`；
- `MEMORY_SECRET`；
- `C:\Users\private-user\kb`；
- `provider-secret-error`。

测试只使用本地对象和临时 SQLite；没有 OCR、网络、真实 Model、Chroma 或外部
Tool。

## 15. Bad Case

以下“真实审计”来自修改前对象；“假设构造”用于验证新边界。

### Bad Case 1：Snapshot 直接 pickle Runtime

- 类型：假设构造。
- 风险：保存 Lock、owner、provider response 和任意正文，且无法安全演进。
- 处理：只允许显式 JSON Contract；没有使用 pickle。

### Bad Case 2：Plan Fingerprint 包含 StepStatus

- 类型：真实审计边界；Plan 与 State 已分离。
- 风险：同一静态 Plan 因运行状态产生不同 fingerprint。
- 处理：Owner 只接收 `Plan/PlanSnapshot`，Plan 本身没有 StepStatus。

### Bad Case 3：Plan Snapshot 保存 Prompt

- 类型：真实审计风险；task summary、title、description、criteria 可能由用户派生。
- 风险：Snapshot 与 SQLite 泄漏业务正文。
- 处理：四类字段均只保存 present/length/digest。

### Bad Case 4：AgentState Snapshot 保存 final_output

- 类型：真实审计风险；`AgentState.to_dict()` 会包含 final output。
- 风险：模型输出和用户数据进入 Snapshot。
- 处理：不调用完整 `to_dict()`；只存摘要。

### Bad Case 5：Budget reserved 被强制清零

- 类型：假设构造。
- 风险：Snapshot 虚报可用额度。
- 处理：直接投影真实 reserved；quiescent 不参与预算计算。

### Bad Case 6：Snapshot Digest 未版本化

- 类型：假设构造。
- 风险：schema 演进后无法确定摘要字段集合。
- 处理：schema v1 与固定 `sha256-v1` source。

### Bad Case 7：同 Snapshot ID 不同内容被当 Duplicate

- 类型：假设构造。
- 风险：静默覆盖或错误幂等。
- 处理：有效旧记录 digest 不同返回 `SNAPSHOT_ID_CONFLICT`。

### Bad Case 8：SQLite 半写入

- 类型：假设构造。
- 风险：只有 envelope 或 Payload 的不完整记录可见。
- 处理：单个 immediate transaction 原子插入并安全回滚。

### Bad Case 9：损坏记录被静默跳过

- 类型：假设构造。
- 风险：`latest/list` 返回次新记录，掩盖恢复点损坏。
- 处理：任何被选中记录损坏立即 fail closed。

### Bad Case 10：BLOCKED 被误判为执行中

- 类型：真实审计边界。
- 风险：错误判断 quiescent 或重复等待永不执行的 Step。
- 处理：只有 `RUNNING` 的 `in_flight=True`；BLOCKED 显式为 false。

## 16. 测试结果

本轮新增 16 项测试，覆盖：

- Contract immutability、strict validation 和 JSON round-trip；
- Plan ordering、dependency ordering、静态字段变化；
- State/Result/Final output 摘要；
- Budget reserved/unlimited/NaN；
- InMemory/SQLite save、read、latest、list、duplicate、conflict、restart；
- SQLite corruption fail closed；
- JSON/SQLite/repr/error 的敏感标记断言。

执行结果：

- Snapshot + AgentState + Budget + Event Journal 定向回归：`56 passed`；
- 全仓：`539 passed, 42 subtests passed`。

附件给出的命令使用 `tests/test_runtime_state.py` 与
`tests/test_runtime_budget.py`，仓库真实文件名是 `tests/test_agent_state.py` 与
`tests/test_budget.py`；定向回归使用真实文件名执行。

- `uv run python -m compileall -q core tools tests`：通过；
- `uv lock --check`：通过（`Resolved 157 packages`）；
- `git diff --check`：通过，仅显示仓库既有 Windows CRLF 转换提示。

## 17. 未完成事项

按本轮禁止项，以下均未实现：

- Checkpoint Barrier；
- Scheduler Pause；
- quiescent 自动判定；
- Journal Tail Reducer / State Reducer；
- Recovery Validator；
- Tool Reconciliation；
- 自动 Replay / Recovery；
- Snapshot 后重新执行 Model/Tool/RAG；
- Event Sourcing 或分布式 Snapshot Store；
- `/api/chat` 或第 23 天 API 迁移；
- Snapshot Store 的 Server/ChatService 生命周期接入。

## 18. 下一轮接入点

后续接入应显式解决：

1. 由 Coordinator/Barrier 确定捕获时点和 `quiescent`，不能由 Serializer 猜测；
2. 从真实 Attempt Owner 提供精确 attempt count 与安全 result 输入；
3. 从配置 Owner 组装真实版本化 `RuntimeMetadata`；
4. 在 Journal append 与 Snapshot sequence 之间定义一致性边界；
5. 读取后先做 schema/digest/plan fingerprint/recovery validation；
6. Tool side effect reconciliation 完成前不得自动重放外部动作。

## 19. 需要带回 ChatGPT 审查的信息

- Plan 没有独立 static input/execution kind；本轮没有改 Plan，Snapshot 使用真实字段
  摘要和当前固定 `AGENT` 标记。
- StepState 没有 result/attempt counter；当前默认 `attempt_count=null`，精确值
  必须在后续由 Attempt Owner 注入，且不得从 `started_at` 推导。
- 不存在 `CLAIMED`；只有 `RUNNING` 是 in-flight，BLOCKED 是未启动终态。
- `AgentState` 没有独立 cancellation reason；安全投影从真实取消 stop reason
  派生。
- Tool side effect 的真实 Owner 是 Tool contract/execution，不在 AgentState。
- Runtime `BudgetSnapshot` 与安全投影同名；包级用 `SafeBudgetSnapshot` 区分。
- Unlimited budget 用 `None`，Reserved 不清零。
- Snapshot digest 与现有 Tool/Journal digest 分域，避免破坏旧摘要兼容性。
- SQLite save 是单事务 append-only；Duplicate 会先验证旧记录。
- 本轮 Foundation 未接入运行生命周期，不能宣称已支持恢复或 replay。
