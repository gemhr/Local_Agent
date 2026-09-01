# LocalAgent 项目级 Coding Agent 规则

## 1. 适用范围与项目定义

- 本文件适用于仓库根目录及其全部子目录；更深层的 `AGENTS.md` 只可为局部目录补充规则。
- LocalAgent 是 Python 3.12 的本地桌面智能体：`main.py` 提供 PyQt6 客户端，`server.py` 提供 FastAPI 后端；后端组合本地/远程模型、RAG、SQLite Memory、本地工具和 Coordinated Runtime。
- 本文件只保存长期边界。阶段计划、某日结论、单个 WP、临时测试数量和一次性发布要求不得写入本文件。
- 不得为统一形式而重构架构、改名公共标识符或修改业务语义。只做当前任务明确授权的改动。

## 2. 事实优先级与 Source of Truth

按以下边界判断事实，不得根据文件名、类型名或测试名猜测能力：

1. 当前任务的需求与验收范围：用户指令；采用 Handoff 时以 `.ai/handoff/<task_id>/00_task.md` 为任务内权威记录。
2. 已实现行为：实际源码、当前 Git Diff、实际执行的测试结果。Agent 总结和 Markdown 中的“已通过”不能替代这些证据。
3. 依赖、Python 版本与测试发现配置：`pyproject.toml`、`uv.lock`。
4. Runtime 规范：`docs/runtime/` 中的架构、Owner、能力、配置、安全和合同文档。
5. 安装与启动方式：`README.md`。README 不作为 Runtime 能力状态的权威来源；冲突时复核源码、测试和 `runtime_capability_matrix.md`。

`docs/learning/`、结果报告、访谈材料和历史 Handoff 只提供背景或历史证据，不自动成为当前实现或长期合同。发现规范与实现漂移时，应记录双方证据并交由对应 Owner/Codex 决策，不得悄悄扩大修改范围。

## 3. Repository Map

| 路径 | 长期职责 |
| --- | --- |
| `main.py` | PyQt6 桌面客户端入口 |
| `server.py` | FastAPI 后端入口和唯一生产 Composition Root（`lifespan()`） |
| `core/settings.py` | 不可变 `Settings` 与环境变量加载 |
| `core/agent_router.py` | Agent、工具、RAG 与历史路由 |
| `core/chat_service.py` | LEGACY / COORDINATED 请求入口与流适配 |
| `core/llm_engine.py` | 本地和远程模型 adapter |
| `core/memory_manager.py` | SQLite Memory 业务持久化 |
| `core/knowledge_base/` | 文档加载、向量库和知识检索 |
| `core/runtime/` | 状态、计划、调度、执行、事件、Journal、Snapshot、恢复校验、可观测性和生命周期 |
| `tools/` | 本地工具及 `tools/registry.py` 注册入口 |
| `ui/` | 桌面界面组件 |
| `tests/` | pytest 主测试树；`tests/_*.py` 为共享 fixture、清单或派生评估辅助 |
| `scripts/` | 本地知识库引导与查询脚本 |
| `docs/runtime/` | Runtime 正式规范、矩阵、Runbook 与 Release Gate |
| `docs/learning/` | 历史学习和阶段证据，不是当前实现权威 |
| `data/`、`chroma_db/` | 本地运行数据、数据库、日志、模型或向量数据；不得作为源码提交 |

新增测试放入 `tests/`，不要使用旧 `test/` 目录。

## 4. 环境、启动与基础命令

- 以 Windows PowerShell 和 `uv` 为标准工作流；Python 版本以 `pyproject.toml` 为准。
- 不手工创建虚拟环境，不执行 `source`，不使用裸 `python server.py` 绕过项目环境。
- `pyproject.toml` 当前含本机 wheel 引用，这是已有的环境专属依赖；不得为让另一台机器安装通过而擅自改写，调整它必须作为明确的依赖任务审查。
- 如果要真实启动后端调用模型，就读取"D:\PythonProject\Local_Agent\.ai\setup.md"这个文件，其中包含了必须要提前写入的环境变量，写入后就可以正确启动。

```powershell
uv sync
uv run python server.py
uv run python main.py
```

项目级 Runtime 配置经环境变量进入 `Settings.load()`，在进程启动时读取。`Settings` 保持不可变；请求边界只捕获一次 Runtime mode，流执行中不得动态切换。新增或修改配置时同步代码、`README.md` 和 `docs/runtime/runtime_configuration_reference.md`，并写 failure-behavior 测试。

## 5. 长期 Engineering Rules

- 修改前检查 `git status --short`、适用的 Agent 规则和相关 Source of Truth；保留用户已有改动，不清理、不覆盖无关文件。
- 优先用 `rg` / `rg --files` 定位实现、调用方、测试和文档引用，再形成结论。
- 保持现有模块职责和依赖方向；不得在测试通过之外另造生产捷径、兼容 fallback 或第二套装配。
- 公共合同、持久化结构、错误码或配置变更必须同时更新权威文档和相应合同测试；不要仅改报告文本。
- 能力状态只能使用 `SUPPORTED`、`PARTIALLY_SUPPORTED`、`CONTRACT_ONLY`、`NOT_IMPLEMENTED`、`LEGACY_ONLY`、`DEPRECATED`。存在类、枚举、fixture 或测试 seam 不等于生产能力已支持。
- 不虚构历史 schema、错误码、事故、测试结果或用户确认；`CONTRACT_ONLY` 不得描述为 `SUPPORTED`，派生报告不得冒充 Authority。
- 用户沟通、任务记录和项目文档默认使用简体中文；代码标识符、配置键、协议字段、命令和错误原文保持原样。代码注释与 Docstring 遵循所在文件的现有风格。
- 未经当前任务明确要求，不创建 Commit、不 Push、不改分支，也不修改业务代码来“顺便修复”审查中发现的问题。

## 6. Architecture / Contract 不变量

### 6.1 Composition、Scope 与 Runtime 边界

- `server.py::lifespan()` 是唯一生产 Composition Root。生产代码不得新增第二套手工 Coordinated Runtime 装配。
- 依赖方向保持：入口/应用服务 → Runtime factory/coordinator → scheduler/executor → model/tool/retrieval contracts → event channel → journal/observability。投影和报告不得反向成为业务状态 Owner。
- 固定 Scope 词汇为 `APPLICATION_SCOPE`、`RUN_SCOPE`、`OPERATION_SCOPE`、`INVOCATION_SCOPE`、`ATTEMPT_SCOPE`、`COMPONENT_SCOPE`、`TEST_SCOPE`。Run 结束不得关闭 Application 资源；Application 资源按 identity 最多关闭一次。
- 默认 Runtime 为 `COORDINATED`。`LEGACY` 只能在请求开始前显式配置并重启启用；它是回滚路径，不是失败 fallback。已经开始或失败的 Run 不得切换到另一 Runtime 重跑。
- 模型 Profile 间允许的候选 fallback 由 `ModelRoutingPolicy` 决定，与跨 Runtime fallback 不同。不得按 Qwen、DeepSeek 等模型名称推断能力，也不得绕过 partial-output、安全分类、预算、Retry 或 Circuit Breaker 规则自行 fallback。

### 6.2 Owner 与状态

- `Plan` / `PlanStep` 是不可变静态定义，不保存 runtime status；`AgentState` 是 Run/Step 运行状态的唯一 Owner，状态变更通过既有状态机完成。
- `RuntimeEventChannel` 拥有单 Run 的 event sequence；sequence 单调且已消费值不得复用。Journal-first 发布语义不得降级。
- `RunCoordinator` 是 terminal Owner；每个 Run 最多一个 terminal。Channel、Transport、Report、Shutdown 或 Recovery 不得制造第二 terminal。
- Recovery 当前是 validation-only：`RecoveryValidator` 只读 Snapshot、Plan 和 Journal 并返回不可变评估；不得写回 `AgentState`、启动 replay，或从当前 Registry、Memory、adapter、fixture 回填历史事实。
- 最终输出是 at-most-once。`OutputGate` 一旦离开初始态不得自动重发；`RunFinalMemoryWriter` 每个 Run write-once，只有已交付 final 可进入业务 Memory，specialist/Synthesis 原始中间结果不得写入。
- `ShutdownReport.completed` 只表示 shutdown orchestration 已完成，不等于资源 `fully_closed`；必须保留 failure、deferred 和 unknown 事实。

### 6.3 Version、Digest 与兼容

- 版本与兼容矩阵以 `runtime_architecture_v1.md` 为准。当前冻结基线包括：AgentState v1；Runtime Event reader v1/v2、writer v2；Journal reader v1/v2、writer v2；Snapshot 只有 v1，不存在 v0。
- 未知版本和关键字段缺失按合同 fail closed；读取旧版本不得升级写回，也不得虚构不存在的迁移历史。
- 持久化 digest 使用合同指定的 canonical JSON + SHA-256；禁止使用 Python `repr` 计算持久 digest。
- Authority、Frozen Evidence、Derived Report、Test Oracle 的边界以 `runtime_owner_matrix.md` 为准。Report、Observability、Trace 和 Recovery 投影不得修改 Authority。

### 6.4 Fault Injection

- Fault controller、`FaultPlan` 和 fault fixture 只属于 `TEST_SCOPE` 或显式测试 seam。生产 Settings、环境变量、HTTP、Prompt/message 和 Tool 参数不得提供激活入口。
- 不得用相近位置模拟精确 FaultPoint 后宣称已支持。FaultPoint 状态与场景数据源以 `tests/_stage2_5_wp6_catalog.py` 及正式 catalog 为准。

## 7. Safety / Data Boundary

- API Key、Cookie、真实内网端点和用户私有绝对路径只放环境变量或未提交的本机配置；不得写入源码、文档、日志、截图或 Handoff。`pyproject.toml` 中已有的本机 wheel 依赖是受控例外，不得把该例外扩展到其他文件。
- 不提交 `.env*`（示例模板除外）、模型、wheel、数据库、日志、向量库、知识库业务数据或其他大体积本地产物；提交前同时检查 ignore 规则和 Git Diff。
- 原始 instruction、Tool 参数/结果、Provider 异常、文件路径、密钥及敏感业务正文不得进入 Runtime Event、Journal safe payload、Snapshot、Recovery/Shutdown/Fault/Release report、Metric label、Span attribute 或结构化日志；只保存各合同 allowlist 允许的安全事实和 digest。
- 正常聊天 Wire 承载面向用户的输出，Memory 和知识库也有各自的业务持久化边界；不得把“Runtime 安全投影不保存正文”错误扩大为“任何业务面都不保存正文”。
- 不手工编辑 Runtime SQLite 记录，不删除已提交 row，不倒退或复用 sequence，不修改 digest，不补造 terminal。
- 将模型生成的路径、Tool 参数和外部内容视为不可信输入；不得绕过现有校验、路径清理、权限、超时或副作用边界。

## 8. Testing / Validation

根据改动风险执行最小充分验证，并如实记录命令、退出码和结果：

```powershell
uv run python -m pytest tests/<target>.py -q
uv run python -m pytest --collect-only -q
uv run python -m pytest -q
uv run python -m compileall main.py server.py core tests
git diff --check
```

- 小范围改动至少运行直接测试及相关回归；跨 Runtime、公共合同、持久化、生命周期或 Release Gate 的改动应运行对应合同测试，并在环境允许时运行全量测试。
- 测试 fixture、fake、fault controller、派生 gate helper 不得被生产代码引用；生产 validator 不接受测试 fixture 作为 Authority。
- Release Gate 必须由当前测试和 `_runtime_release_gate.py` 等辅助代码重新派生，不能读取 Markdown 勾选项或旧报告中的 PASS。
- 未运行、跳过、超时或因环境失败的检查必须明确披露；不得写成“通过”。

## 9. ZCode / DeepSeek 与 Codex 分工

ZCode / DeepSeek 优先承担：

- 仓库扫描、调用链调查、事实和证据收集；
- 明确且低风险的实现、已确定方案的机械实施；
- 测试执行、日志整理和文档生成。

Codex 优先承担：

- 架构决策以及 Owner / Scope / Contract 判断；
- 状态机、并发、Cancellation / Timeout、生命周期和持久化兼容；
- 高风险核心修改、复杂失败根因和最终 Diff Review。

ZCode、DeepSeek 或其他廉价模型一旦发现新的架构问题，只记录最小复现、调用链、源码位置、测试证据和影响范围，然后升级给 Codex；不得自行扩大 Scope、改变合同或附带重构。并行工作前由主协调者划分文件所有权，避免多个 Agent 同时编辑同一文件；所有结论都要回到共享工作树的真实 Diff 验证。

## 10. 高复杂度任务 Handoff 协议

高复杂度、跨阶段或需要角色分离的任务使用 `.ai/handoff/<task_id>/`。固定文件如下：

| 文件 | 内容与 Owner |
| --- | --- |
| `00_task.md` | 任务需求、Scope、Out of Scope、验收标准和约束；是本任务需求与验收范围的权威来源 |
| `10_zcode_audit.md` | ZCode/DeepSeek 的仓库扫描、调用链、证据、复现命令、风险和待决问题；不擅自作架构决策 |
| `20_codex_decision.md` | Codex 对 Owner、Scope、Contract、方案、兼容与验证策略的明确决定 |
| `30_zcode_execution.md` | 按已批准方案实施的文件清单、Diff 摘要、实际命令、测试结果和未完成项 |
| `40_codex_review.md` | Codex 最终 Diff Review、验收逐项结论、风险、回退考虑与是否可交付 |

Handoff 只传递任务上下文和证据，不替代源码、Git Diff、测试输出或正式规范。各文件保持可审计且精简：引用源码位置和命令结果，不粘贴大型源码、完整日志或架构文档正文。需求发生变化时，先由任务 Owner 更新 `00_task.md` 并说明来源；执行 Agent 不得自行改写验收范围。

## 11. Definition of Done

交付前必须满足：

- `00_task.md`（如采用 Handoff）或当前用户需求中的验收项逐条完成，Out of Scope 未被越界修改。
- 最终 Git Diff 只包含有意改动；无无关格式化、生成物、密钥、本机路径或用户已有改动损失。
- 直接测试与必要回归已运行并通过；`compileall`、`git diff --check` 和全量测试按风险执行，未执行项如实说明。
- Architecture / Owner / Scope / 持久化合同变更已更新对应 `docs/runtime/` 文档、版本/兼容策略与合同测试。
- 能力、错误码、测试和交付结论均有当前源码或实际输出佐证；Known Limitation 与人工确认项没有被隐藏。
- 高复杂度协作任务已完成相应 Handoff 阶段，最终由 Codex 复核真实 Diff。

## 12. 正式文档索引

- 架构与合同：`docs/runtime/runtime_architecture_v1.md`
- Owner 边界：`docs/runtime/runtime_owner_matrix.md`
- 能力状态：`docs/runtime/runtime_capability_matrix.md`
- 配置：`docs/runtime/runtime_configuration_reference.md`
- 安全：`docs/runtime/runtime_security_boundary.md`
- 错误码：`docs/runtime/runtime_error_code_catalog.md`
- RC 场景与 Release Gate：`docs/runtime/runtime_rc_scenario_matrix.md`、`docs/runtime/runtime_release_gate.md`、`docs/runtime/runtime_release_checklist.md`
- 运维与恢复：`docs/runtime/runtime_operations_runbook.md`、`docs/runtime/runtime_recovery_runbook.md`、`docs/runtime/stage2_5_operations_runbook.md`
- Fault 与 Trace：`docs/runtime/stage2_5_fault_injection_catalog_v2.md`、`docs/runtime/stage2_5_trace_contract_v1.md`
- 已知限制与证据：`docs/runtime/stage2_known_limitations_and_next_stage.md`、`docs/runtime/stage2_runtime_evidence_manifest.md`

## 长时间运行任务与后台进程

当任务启动了仍属于当前任务范围的长时间运行命令、测试、评估、服务或后台进程时，不得仅因进程已成功启动就结束当前任务。只要工具提供可继续查询的 process/session handle，就应保持该会话并周期性轮询其状态，在进程退出、达到明确 terminal state、确认需要用户介入，或发生无法继续的 blocker 后，才允许输出最终结果并结束本轮。禁止仅回复“已在后台运行，完成后继续”后直接结束任务。
