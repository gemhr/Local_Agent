# LocalAgent 项目级 Coding Agent 规则

## 1. 适用范围与长期目标

- 本文件适用于仓库根目录及其全部子目录；更深层的 `AGENTS.md` 只可补充局部规则，不得静默推翻本文件的项目级边界。
- LocalAgent 是 Python 3.12 的本地 AI Agent 工程：`main.py` 提供 PyQt6 客户端，`server.py` 提供 FastAPI 后端；当前主线围绕 Coordinated Runtime、RAG、Memory、Evaluation、Tool Runtime、HITL、MCP 以及后续后端/分布式基础设施持续演进。
- 本文件只保存跨 Stage 长期有效的工程规则。阶段计划、单个 WP、临时测试数量、一次性结论和某轮 Gate 结果写入对应 Handoff / 阶段文档，不写入本文件。
- 当前用户任务可以明确授权架构调整、Owner/Contract 变更、重构或破坏性修改；不得因为历史实现、旧测试或兼容成本自行缩小已授权 Scope。
- 默认目标优先级：正确完成当前任务 > 真实可运行/可测试 > Architecture / Owner / Contract 清晰 > Production Awareness > 历史兼容与改动体量。

## 2. 事实优先级与 Source of Truth

判断能力和当前事实时，不得根据文件名、类名、测试名或历史总结猜测：

1. **当前用户指令**：最新、最明确的用户要求优先级最高；若与旧 Handoff 或历史文档冲突，以最新用户要求为准。
2. **当前实现事实**：实际源码、当前 Git Diff、数据库/配置定义以及本轮真实执行的测试输出。
3. **任务 Handoff**：若采用 Handoff，以 `D:\PythonProject\Local_Agent\.ai\handoff\<task_id>\` 中当前任务文档记录任务上下文、决策和证据；它不能覆盖更新后的用户指令。
4. **依赖与测试发现配置**：`pyproject.toml`、`uv.lock`、pytest 配置。
5. **正式 Runtime 文档**：`docs/runtime/` 中仍被当前源码引用并保持同步的架构、Owner、能力、配置、安全和合同文档。
6. **README / 学习 / 历史材料**：只提供使用说明或历史背景，不能单独证明生产能力已实现。

发现文档与源码漂移时，应记录双方证据。涉及 Architecture / Owner / Contract / Authority 的真实冲突交给 Codex 判断；普通实现漂移可在当前已授权 Scope 内直接修复。

## 3. Repository Map

| 路径 | 长期职责 |
| --- | --- |
| `main.py` | PyQt6 桌面客户端入口 |
| `server.py` | FastAPI 后端入口和生产 Composition Root（当前以 `lifespan()` 为主） |
| `core/settings.py` | Settings 与环境变量加载 |
| `core/agent_router.py` | Agent、工具、RAG 等应用路由 |
| `core/chat_service.py` | 请求入口与 Runtime / Streaming 适配 |
| `core/llm_engine.py` | 本地/远程模型 Adapter |
| `core/memory_manager.py`、相关 Memory 模块 | Memory 业务能力；具体持久化 Authority 以当前源码为准 |
| `core/knowledge_base/` | 文档处理、索引和知识检索 |
| `core/runtime/` | 状态、计划、调度、执行、事件、Journal、Snapshot、恢复、可观测性和生命周期 |
| `tools/` | Local Tool 及 Tool Registry / Adapter 相关实现 |
| `mcp/` | MCP 外部 Tool Provider / Client 集成边界；MCP 不成为 Runtime Safety Authority |
| `ui/` | 桌面界面组件 |
| `tests/` | 当前有效 pytest 主测试树 |
| `scripts/` | 运维、数据准备、知识库及工程辅助脚本 |
| `docs/runtime/` | 当前 Runtime 正式架构/合同/Runbook 文档 |
| `docs/interview/` | 各 WP 的学习与面试材料，不作为生产实现 Authority |
| `.ai/handoff/` | 多 Agent / 高复杂度任务的交接、审计、决策和 Gate 证据 |
| `.ai/archive/` | 经明确废弃但需要保留参考的历史材料；不得参与默认 Gate |
| `data/`、`chroma_db/` | 本地运行数据、数据库、日志、模型或向量数据；默认不得作为源码提交 |

新增有效测试统一放入 `tests/`。废弃测试不要放在仍会被 pytest 默认发现的位置。

## 4. 环境、启动与基础命令

- 标准开发环境为 Windows PowerShell + `uv`；Python 版本以 `pyproject.toml` 为准。
- 不手工创建额外虚拟环境，不执行 Linux `source` 流程，不用裸 `python server.py` 绕过项目依赖环境。
- `pyproject.toml` 中若存在受控的本机 wheel / 内网依赖，不得为了通用化安装而顺手改写；依赖迁移必须是当前任务明确范围。
- **任何需要真实启动 LocalAgent backend、执行 HTTP E2E、真实模型调用、真实 MCP 或 subprocess provisioning 的任务，必须先读取：** `D:\PythonProject\Local_Agent\.ai\setup.md`，并按其中当前说明设置运行环境。
- API Key 等 secret 只使用现有安全环境变量，不要求用户重复提供，不写入日志、Handoff、源码或文档。

```powershell
uv sync
uv run python server.py
uv run python main.py
```

配置新增或变更时，应同步当前有效配置文档和相应 failure-behavior 测试；不得假设历史 `Settings` 结构永久冻结。

## 5. 开发工作流：快速推进 + 风险分级

不要默认把每个任务都串行为完整的 Scout → Decision → Execute → Final Gate。根据风险、改动范围、可验证性和上下文读取量路由：

### L — 低风险

需求明确、局部改动、容易通过测试验证：

```text
ZCode / DeepSeek 直接实施 → 目标测试 → 交付
```

### M — 中风险

存在跨文件修改，但 Architecture / Owner / Contract 已明确：

```text
ZCode / DeepSeek 实施 → Codex 轻审真实 Diff → 必要修复/回归
```

### H — 高风险

涉及 Architecture、Owner、Contract、Authority、状态机、并发、Timeout/Cancellation、安全、持久化一致性、分布式事务或其它关键边界：

```text
Codex 审计/决策 → ZCode / DeepSeek 实施 → Codex Gate
```

- 不为了形式完整重复已经有充分证据的 Scout / Regate。
- 普通 P1/P2、实现缺陷、兼容性问题或已冻结架构内的问题，默认允许边发现边修复并执行对应回归。
- 只有问题会改变已确认的 Architecture / Owner / Contract / Scope / Data Authority 时，才设置 `ARCHITECTURE_REOPEN_REQUIRED = YES`。
- 阶段式开发中，每完成一个 WP 必须停止继续实施，等待用户选择“进行本 WP 学习/面试总结”或“直接进入下一步”；不得自动跨 WP。
- 若用户选择学习，面试材料写入 `docs/interview/<lowercase_name>.md`，至少覆盖名词速览、真实实现、架构/调用链、设计取舍、工程问答、30 秒/2 分钟回答、高频追问、Bad Case、Truth/Completion Boundary 和 Known Limitation。

## 6. 长期 Engineering Rules

- 修改前检查 `git status --short`、适用的 Agent 规则和相关 Source of Truth；保留用户已有且与当前任务无关的改动。
- 优先使用 `rg` / `rg --files` 定位实现、调用方、测试和文档引用，再形成结论。
- 不得为了“让测试绿”在生产路径另造第二套 Runtime、第二套 Authority、测试专用生产捷径或未经批准的 fallback。
- 当前仍有效的公共 Contract、持久化结构、错误码或配置发生变更时，同步当前权威文档和相应测试；已明确废弃的旧 Contract 按第 7 节处理，不要求继续兼容。
- 能力状态优先使用 `SUPPORTED`、`PARTIALLY_SUPPORTED`、`CONTRACT_ONLY`、`NOT_IMPLEMENTED`、`LEGACY_ONLY`、`DEPRECATED`；存在类、fixture 或测试 seam 不等于生产可达。
- 不虚构 schema、错误码、事故、测试结果或用户确认；未运行、跳过、超时、环境失败均必须如实说明。
- 用户沟通、任务记录和项目文档默认简体中文；技术术语首次出现可使用“中文名称（English Term）”；代码标识符、配置键、协议字段、命令和错误原文保持原样。
- 未经当前任务明确要求，不创建 Commit、不 Push、不改分支。

## 7. 破坏性修改、兼容性与废弃测试

### 7.1 默认允许经过授权的 Breaking Change

除非当前任务明确要求兼容旧 API、旧数据、旧调用方或旧 Runtime，否则不默认承担历史兼容成本。

当破坏性修改（Breaking Change）是更快、更清晰或更正确的实现方式时，可以直接：

- 删除或修改旧 API / Contract；
- 修改数据库 Schema 或配置结构；
- 替换 Persistence / Runtime / Adapter 实现；
- 调整 Owner、模块职责或依赖方向；
- 删除 Legacy 路径、兼容层、fallback、双写/双读逻辑；
- 修改或删除依赖旧行为的历史测试和文档。

原则：

```text
Current Canonical Implementation
>
Legacy Compatibility
```

不得仅因为“旧测试会失败”“历史调用方可能受影响”“以前是这样实现的”就拒绝当前任务已经授权的正确修改。

### 7.2 旧测试必须同步退出默认 Gate

如果合法的 Breaking Change 使旧测试对应的 Contract 已失效，禁止长期保留红测并仅标记为 `known failure` / `expected regression` / `legacy failure`。

按以下顺序处理：

1. **旧 Contract 已废弃：优先直接删除旧测试。**
2. **仍有历史参考价值：移动到** `D:\PythonProject\Local_Agent\.ai\archive\tests\<task_id>\` **或其它 `.ai/archive/` 子目录。** 归档测试不得位于 pytest 默认 discovery 路径，也不得进入后续 Release Gate。
3. **业务语义仍然有效、只是实现改变：更新测试，使其验证新的 Canonical Contract。**

发生废弃时同时检查并清理：

- 旧 fixture / fake；
- test catalog / scenario matrix；
- Release Gate helper / allowlist；
- 旧 README / runtime 文档引用；
- 只为旧 Contract 服务的兼容代码。

目标是让后续 Coding Agent 运行全量测试或 Final Gate 时，不再重复调查已经正式废弃的历史行为。

### 7.3 Breaking Change 必须留下最小可审计记录

在当前任务 Handoff / 执行报告中记录：

```text
BREAKING_CHANGE = YES/NO
OLD_BEHAVIOR =
NEW_CANONICAL_BEHAVIOR =
OLD_TESTS_REMOVED_OR_ARCHIVED =
COMPATIBILITY_REQUIRED = YES/NO
```

若删除/归档测试，列出具体路径。后续 Agent 应将该记录视为废弃意图的证据，但仍以当前源码、当前测试发现结果和最新用户指令为最终事实。

## 8. Architecture / Contract 默认边界

以下是当前主线的默认边界。**如果当前任务明确要求重新打开对应 Architecture / Owner / Contract，可以修改；不得把本节当作禁止演进的永久冻结条款。**

### 8.1 Composition、Scope 与 Runtime

- `server.py::lifespan()` 当前是生产 Composition Root；新增基础设施优先在应用级 Composition / Lifecycle 中拥有清晰 Owner，不要偷偷新增无法统一关闭的全局资源。
- 依赖方向默认保持：入口/应用服务 → Runtime / Domain Service → Model/Tool/Retrieval/Persistence/Infrastructure Contract → Event/Journal/Observability。
- Scope 使用现有正式合同定义；Run 结束不得误关 Application-scope 资源，Application 资源应具有明确 startup/shutdown Owner。
- `COORDINATED` 是当前 Canonical Runtime。`LEGACY` 不再天然享有兼容权；除非当前任务明确要求，否则允许被破坏、删除或退出默认测试/Gate。
- Provider / MCP / 外部系统负责其协议与能力暴露，但不得绕过 Runtime 的 typed validation、risk/governance、approval、idempotency、timeout/cancellation 和 execution authority。

### 8.2 Owner 与状态

- 静态定义与运行时状态必须分离；当前状态只能由明确 State Owner 修改。
- Runtime Event / Journal sequence、terminal、final output、approval/execution claim 等关键 exactly-once / at-most-once / idempotency 语义不得由投影、报告或外部 Provider 私自制造第二 Authority。
- Recovery、Replay、Snapshot 等能力以当前源码和正式 Contract 为准；不得从当前 Registry、Memory、Adapter、fixture 反向伪造历史事实。
- Report、Observability、Trace 和派生 Evaluation 结果默认是投影或证据，不得反向成为核心业务状态 Owner，除非当前任务明确重新设计 Authority。

### 8.3 Version、Digest 与兼容

- 当前版本、Reader/Writer 兼容矩阵以仍有效的正式 Runtime Contract 和源码为准，不在 `AGENTS.md` 固化易过期的阶段版本号。
- 未知版本和关键字段缺失按照当前 Contract 处理；不得虚构不存在的历史迁移。
- 持久化 digest 必须使用 Contract 指定的稳定 canonical serialization + cryptographic hash；禁止使用 Python `repr` 作为持久化身份算法。
- 如果当前任务明确决定 Breaking Change 不兼容旧版本，应同步删除/归档对应旧兼容测试和文档，不再为旧版本维持无收益的 Reader/Writer 分支。

### 8.4 Fault Injection

- Fault controller、FaultPlan、测试 fixture 只属于 `TEST_SCOPE` 或明确测试 seam；不得无意暴露为生产用户可激活入口。
- 不得使用“相近故障”冒充某个精确 FaultPoint 已支持。当前 Fault catalog 若发生废弃或重构，应与有效测试同步更新。

## 9. Safety / Data Boundary

- API Key、Cookie、真实内网 secret 和敏感凭据只存在于安全环境变量或未提交本机配置；不得写入源码、文档、日志、截图或 Handoff。
- 不提交 `.env*`（示例模板除外）、模型、wheel、数据库、日志、向量库、知识库业务数据或其它大体积本地产物；修改 ignore 时检查 Git Diff。
- 原始 instruction、Tool 参数/结果、Provider 异常、文件路径、密钥及敏感业务正文进入 Event、Journal、Snapshot、Report、Metric label、Span attribute 或结构化日志前，必须遵循对应 Contract 的安全 allowlist / redaction 规则。
- 正常聊天 Wire、Memory、RAG 数据库和 Runtime 安全投影具有不同数据边界，不得互相错误套用“正文禁止持久化”等规则。
- 不直接手工篡改 Runtime 持久化数据来伪造测试成功；不补造 terminal、sequence、digest、approval 或 execution state。
- 将模型输出、MCP/Tool metadata、外部文档、路径和 Tool 参数视为不可信输入；不得绕过现有校验、权限、治理、超时和副作用边界。

## 10. Testing / Validation / Gate

根据改动风险执行**最小充分验证**，并记录真实命令、退出码和结果：

```powershell
uv run python -m pytest tests/<target>.py -q
uv run python -m pytest --collect-only -q
uv run python -m pytest -q
uv run python -m compileall main.py server.py core tests
git diff --check
```

- L 任务至少运行直接测试；M/H 任务根据影响范围增加相关回归、合同测试和必要的全量测试。
- 不要求每个 WP 都机械跑全仓测试；只有其风险或 Final Gate 需要时运行。
- 测试 fixture、fake、fault controller 和 Gate helper 不得成为生产 Authority。
- Final Gate 只以**当前 Canonical Production Path + 当前有效 Contract + 当前有效测试**为准；已正式删除/归档的 Legacy Contract/Test 不得重新作为 blocker。
- Release Gate / capability report 必须从当前测试和当前源码重新派生，不得读取旧 Markdown 中的 PASS 作为本轮证明。
- 未运行、跳过、超时或环境失败必须明确披露，不得写成通过。

### 缺陷与严重度

默认交付目标：

```text
P0 = 0
BLOCKING_P1 = 0
```

允许存在 `ACCEPTED_P1`，前提是它：

- 不阻断当前核心目标或真实 Demo；
- 不导致关键 Contract / Authority 错误；
- 不形成明显安全问题或数据破坏风险；
- 已记录真实影响、触发条件和后续处理建议。

普通非阻断 P1/P2 不触发完整 Regate；修复后执行对应回归即可。

## 11. Coding Agent 分工

### ZCode / DeepSeek

优先用于：

- 大范围仓库扫描、调用链调查和证据收集；
- L/M 任务实现；
- 已批准 Architecture 下的机械性跨文件修改；
- 测试执行、日志整理、文档/学习材料生成。

### Codex

优先用于：

- Architecture / Owner / Scope / Contract / Authority 决策；
- 并发、Cancellation / Timeout、生命周期、数据一致性、分布式事务、安全等 H 任务；
- 高风险核心 Diff Review；
- 阶段 Final Gate。

ZCode / DeepSeek 在实施中发现新的 H 风险时，先记录最小复现、调用链、源码位置、测试证据和影响范围，再交给 Codex；不要未经决策顺带重构整个架构。

并行工作时由主协调者划分文件所有权；所有结论最终回到共享工作树的真实 Diff 和测试验证。

## 12. Handoff 协议

### 12.1 固定根路径

LocalAgent 的所有 `.ai` 任务交接必须写为：

```text
D:\PythonProject\Local_Agent\.ai\handoff\<task_id>\...
```

必须保留 `Local_Agent` 与 `.ai` 之间的反斜杠。不要写成其它仓库路径，也不要省略该分隔符。

### 12.2 Handoff 采用“按需文件”，不再强制五件套

只有高复杂度、跨 Agent、跨会话、H 风险或需要留下可审计证据的任务才需要 Handoff。L/M 小任务如果当前 Prompt + Git Diff + 测试结果已经足够，不为了形式创建空 Handoff。

文件名前缀遵循以下语义，**只创建当前任务实际需要的文件**：

| 前缀 | 推荐用途 | 示例 |
| --- | --- | --- |
| `00_` | 可选任务 brief / scope freeze | `00_task.md` |
| `10_` | Source Audit / Scout / Readiness | `10_codex_source_audit.md` |
| `20_` | Architecture / Owner / Contract Decision 或 WP Plan | `20_codex_architecture_decision.md` |
| `30_` | 实施结果、Diff、测试和 Breaking Change 记录 | `30_zcode_execution.md` |
| `40_` | Codex Review / Final Gate | `40_codex_final_gate.md` |

允许使用更具体的文件名，例如：

```text
10_codex_backend_source_audit.md
20_codex_mcp_architecture_decision.md
40_codex_final_gate.md
```

不要求所有任务都存在 `00/10/20/30/40`；不要为了补齐编号制造无信息文档。

### 12.3 Handoff 内容要求

- Handoff 用于传递任务上下文、源码证据、已批准决策、实施状态、Breaking Change、ACCEPTED_P1 和 Gate 结论。
- Handoff 不替代源码、Git Diff、测试输出或正式 Contract。
- 文档保持可审计且精简：优先引用文件路径、类/函数、命令和关键输出，不粘贴大型源码或完整日志。
- 当前用户指令发生变化时，以最新用户要求为准，并在后续 Handoff 中注明变化；无需为了维护旧 `00_task.md` 的形式完整性而阻塞实现。
- Final Gate Agent 遇到已明确记录并已删除/归档的旧 Contract/Test，不应重新把它作为 Regression blocker。

## 13. Definition of Done

交付前确认：

- 当前用户验收目标已完成；没有因为历史 Scope 自行遗漏已授权修改。
- 最终 Git Diff 只包含有意改动；用户已有无关修改未被覆盖。
- 当前 Canonical Contract 对应的直接测试和必要回归已执行；未执行项如实说明。
- Architecture / Owner / Scope / Persistence / Security 等高风险变更已由适当 Owner/Codex 决策并同步当前有效文档。
- Breaking Change 已同步处理旧实现、旧测试、fixture、Gate catalog 和相关文档，不遗留会误导后续 Agent 的红测或假 Authority。
- 能力、错误码、测试和交付结论均由当前源码或实际输出支持。
- `P0 = 0`、`BLOCKING_P1 = 0`；所有 `ACCEPTED_P1` 和 Known Limitation 已披露。
- 使用 Handoff 的任务已写入实际需要的交接文件；H 风险或阶段 Final Gate 由 Codex 复核真实 Diff。

## 14. 正式文档与历史材料

优先检查当前仍有效的核心文档：

- `docs/runtime/runtime_architecture_v1.md`
- `docs/runtime/runtime_owner_matrix.md`
- `docs/runtime/runtime_capability_matrix.md`
- `docs/runtime/runtime_configuration_reference.md`
- `docs/runtime/runtime_security_boundary.md`
- `docs/runtime/runtime_error_code_catalog.md`
- `docs/runtime/runtime_release_gate.md`
- `docs/runtime/runtime_operations_runbook.md`
- `docs/runtime/runtime_recovery_runbook.md`

Stage2 / Stage2.5 / Stage3 / Stage4 / Stage5 等带阶段名的旧文档、旧 evidence manifest、旧 learning 文档属于历史证据；除非当前源码/Contract 仍明确引用，否则不得仅凭其旧结论约束新的 Canonical 实现。

## 15. 长时间运行任务与后台进程

当任务启动了仍属于当前任务范围的长时间运行命令、测试、评估、服务或后台进程时，不得仅因进程成功启动就结束任务。只要当前工具提供可继续查询的 process/session handle，就应继续轮询，直到：

- 进程退出；
- 达到明确 terminal state；
- 确认需要用户介入；
- 或出现无法继续的 blocker。

不得仅回复“已在后台运行，完成后继续”然后结束本轮。

## Test / Regression Execution Policy

Test execution must follow the smallest-sufficient-validation principle.

###  Default rule

Unless the current task is explicitly identified as the **Final Gate / Final Review / Final Audit of the current WP**, DO NOT run broad or full-repository regression suites.

For normal implementation, debugging, review, documentation synchronization, or intermediate verification:

- Run only tests directly related to the files, modules, contracts, or behavior changed by the current task.
- Prefer the smallest focused test set that can verify the modification.
- Do not run the full repository test suite "for safety", "for confidence", "to establish a baseline", or "to make sure nothing else broke".
- Do not expand regression scope merely because unrelated historical tests exist.
- Historical failures outside the current change scope must be recorded as pre-existing/out-of-scope unless there is direct evidence that the current change caused them.

### Broad regression is restricted

A broad regression includes, but is not limited to:

- full `pytest` / repository-wide test execution;
- all-stage or all-phase regression;
- unrelated subsystem suites;
- large integration matrices outside the affected execution path;
- repeating an already-passing large regression suite.

Broad regression is allowed only when:

1. the task explicitly states that the current step is the **WP Final Gate / Final Review / Final Audit**; or
2. the user explicitly requests a broad/full regression.

Do not infer Final Gate status from context. It must be explicitly stated.

### No repeated safety runs

Passing tests MUST NOT be repeatedly re-run merely for additional confidence.

Default execution policy:

- run the required focused suite once;
- if it passes, accept the result;
- if it fails, diagnose and fix the relevant issue;
- after a fix, re-run the smallest failed or affected subset first;
- expand only when evidence shows that a wider execution path may have been affected.

Even during Final Gate, normally run each broad regression suite at most once after the implementation is considered stable. Re-run only failed/affected subsets after fixes unless the fix materially changes the scope of the previous broad result.

Statements such as:

- "I'll run the full suite once more to be safe";
- "I'll do another complete regression for confidence";
- "I'll run all tests again to get the latest number";
- "I'll verify the already-passing gate one more time";

are explicitly prohibited unless new code changes invalidate the previous result.

### Validation scope must be justified

Before running a non-trivial test suite, briefly determine which changed behavior it validates.

If a test suite has no clear relationship to the current diff or current WP acceptance criteria, do not run it.

Validation priority:

`direct affected tests > affected subsystem regression > cross-subsystem regression > full repository regression`

Stop as soon as sufficient evidence for the current task has been obtained.

### Documentation-only or evidence-only work

For documentation-only changes, Handoff updates, audit-result synchronization, or evidence collection:

- do not automatically run production tests;
- validate the changed document/contract with the smallest relevant check;
- do not trigger a full regression simply because source code exists in the repository.

### Existing failures

When a broader test command exposes failures:

- first determine whether each failure is caused by the current diff;
- do not start fixing unrelated historical failures;
- do not repeatedly run the entire suite while investigating a small number of failures;
- isolate the failing tests and work on the smallest relevant subset.

Unrelated failures must be reported as existing/out-of-scope evidence rather than silently expanding the task.

### Final Gate exception

When the current task is explicitly a WP Final Gate, a reasonably broad regression may be executed if required by the WP acceptance criteria.

Even then:

- prefer one deliberate final regression over multiple precautionary regressions;
- do not run multiple equivalent full-suite commands;
- do not repeat a passing full regression without a code change that invalidates it;
- additional runs after fixes should normally target only the affected failures/subsystems.

The goal of testing is to obtain sufficient evidence for the current change, not to maximize the number of tests executed.
