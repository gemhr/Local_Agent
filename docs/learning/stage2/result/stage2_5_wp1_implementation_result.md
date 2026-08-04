# LocalAgent Stage 2.5 Multi-Agent WP1 Implementation Result

## 1. Executive Summary

WP1 已完成：实现了不可变 `AgentRegistry`、typed planning contracts、严格 Planner JSON parser、run-scoped `StepInvocationBindings`、`PlanCompiler`、未接线的 `PlanResolver`、四种合法图以及拒绝矩阵/安全测试。

本轮修改了 WP1 范围内生产代码和测试；默认 `/api/chat`、`RunCoordinator`、Scheduler 生命周期、Snapshot/Checkpoint、Streaming、Recovery、Memory 均未接入或改变。当前生产 Runtime 仍不会执行这里生成的动态多 Agent Plan。

最终证据：WP1 专项 66 passed；关键 Plan/Scheduler 回归 47 passed、9 subtests passed；AgentRouter/知识路由/运行模式回归 39 passed、3 subtests passed；Stage2 Coordinator/Event/Snapshot/Recovery 回归 66 passed、4 subtests passed；最终全仓结果见第 8 节。

## 2. Source Audit Before Changes

| 审计面 | 变更前真实情况 | WP1 采用方式 |
|---|---|---|
| Agent 配置 | `core/agent_router.py` 的 `agents_config` 保存四个 entry Agent 的展示元数据；`delegate_agent_ids` 另列三个 specialist | Registry 收口身份、展示元数据和权限；Legacy 从 Registry 派生相同配置和相同顺序 |
| Agent ID | `core_router`、`data_analyst`、`code_expert`、`knowledge_expert`；尚无生产 `synthesis_agent` 注册 | Registry 增加受 Compiler 专用、禁止 entry 的 `synthesis_agent` |
| Plan/PlanStep | `PlanStep` 已有 ID、描述、依赖、`preferred_agent`、`TaskCapabilityRequirements`，没有 raw instruction | 兼容地增加有默认值的 `execution_kind` 与 `output_policy`；不增加 instruction/digest |
| 基础校验 | `PlanValidator` 校验静态字段；此前未校验 `preferred_agent` 及新增枚举 | 补齐安全静态字段校验 |
| DAG 校验 | `PlanGraphValidator` 已集中处理重复 Step、缺失依赖、自依赖、重复依赖、环和稳定拓扑序 | Compiler 直接复用，不复制环检测 |
| Snapshot/fingerprint | `PlanStepSnapshot.from_plan_step` 当前硬编码 `static_execution_kind="AGENT"`，fingerprint 基于现有 Snapshot schema | WP1 不修改；dynamic schema/fingerprint 接线留给 WP2/合同硬化 |
| 模型统一入口 | `ModelInvocationRouter` 已负责候选、预算、取消、deadline、重试和 Trace；Legacy 由 `AgentRouter._invoke_model_contract` 使用 | WP1 定义异步 `PlanningModel` Protocol，不直接调用 provider，也不强迫修改 Runtime 生命周期 |
| Legacy deterministic | `_resolve_explicit_knowledge_delegate` 和自由文本 `Delegate:` parser 存在；自由文本非法项会被忽略 | 只借鉴高置信别名/文档规则，不复用自由文本 Delegate 作为 Plan |

与候选设计的一个实现差异：包含 raw instruction 的 Request/Decision/InvocationSpec 使用带 `__slots__` 的不可变普通类，而不是 dataclass。原因是 `repr=False` 不能阻止 `dataclasses.asdict()` 导出原文；当前实现让默认 `asdict()` 直接拒绝这些对象。

## 3. Files Changed

| 文件 | 类型 | WP1 职责 | WP2 接口 |
|---|---|---|---|
| `core/runtime/planning.py` | 修改 | 新增 `ExecutionKind`、`OutputPolicy`，扩展兼容 PlanStep 静态合同 | WP2 可读取新增字段 |
| `core/runtime/agent_registry.py` | 新增 | 进程级不可变 Agent 身份、能力、entry/delegated/synthesis 权限事实源 | Resolver/Compiler/未来 Driver 消费 |
| `core/runtime/invocation_bindings.py` | 新增 | raw instruction 的 run-scoped 单步解析与清理边界 | WP2 在真实 Step claim 后消费 |
| `core/runtime/multi_agent_planning.py` | 新增 | Request/Decision、strict parser、ResolvedPlan、PlanningModel Protocol、Resolver | WP2 调用 `PlanResolver.resolve` |
| `core/runtime/plan_compiler.py` | 新增 | 四种固定图、Registry/资源/安全/DAG/唯一 final 校验 | WP2 接收 `ResolvedPlan` |
| `core/runtime/__init__.py` | 修改 | 导出 WP1 稳定公共合同 | 后续代码使用统一 import surface |
| `core/agent_router.py` | 修改 | Legacy 展示配置和 delegate ID 从 Registry 派生 | 没有接入 Resolver；行为不变 |
| `tests/test_agent_registry.py` | 新增 | Registry 合同与非法注册 | 无 |
| `tests/test_invocation_bindings.py` | 新增 | Binding 生命周期、ACL 前置校验、安全性 | 无 |
| `tests/test_plan_compiler.py` | 新增 | 四图、拒绝矩阵、限制、DAG、安全性 | 无 |
| `tests/test_multi_agent_planning.py` | 新增 | parser、deterministic/model Resolver、无 fallback、安全性 | 无 |
| 本文档 | 新增 | 实施与证据记录 | WP2 审查输入 |

未修改 `server.py`、`core/chat_service.py`、`run_coordinator.py`、`scheduler.py`、Snapshot/Checkpoint/Event/Streaming/Recovery/Memory/前端文件。

## 4. Final Contracts

### AgentRegistration / AgentRegistry

`AgentRegistration` 是 frozen+slots 配置，包含：身份/展示元数据、enabled、entry permission/policy、model-direct permission、delegation permission/policy、单 delegated passthrough 例外、synthesis-only、parallel support、input/result types、capabilities、deterministic aliases。

`AgentRegistry`：

- 复制并冻结构造输入，拒绝空/重复/非法 registration。
- `resolve`、`require_entry`、`require_delegated` 均返回 typed registration 或稳定错误码。
- `synthesis_registration()` 要求恰好一个 enabled synthesis Agent。
- 不保存 Run、用户请求、结果、driver/factory 或 secret。
- `legacy_display_config()` 只为尚未迁移的 Legacy 返回同源展示配置副本。

### OutputPolicy / ExecutionKind

```text
OutputPolicy: INTERNAL | FINAL_PASSTHROUGH | FINAL_SYNTHESIS
ExecutionKind: AGENT | SYNTHESIS
```

二者进入不可变 `PlanStep`。旧位置参数构造和 `create_single_step_plan` 通过默认 `AGENT + FINAL_PASSTHROUGH` 保持兼容。

### PlanningRequest / PlanningDecision

- `PlanningRequest(selected_agent_id, user_request)`：不可变，repr 隐去正文。
- `DirectAnswerDecision(agent_id, instruction, reason_code)`：用于 Core direct 或确定性 explicit entry。
- `DelegatedTaskDecision(task_id, agent_id, instruction, input_type, required_capabilities)`：Planner 只提出任务。
- `DelegatedPlanDecision(tasks, synthesis_required)`：不包含 output policy、依赖图、driver 或 Runtime state。

### PlanningModel Protocol / Parser

`PlanningModel.generate_plan(request, run_context) -> str` 是异步 Protocol。实现必须使用统一模型服务并承担预算/取消/deadline；WP1 只使用 fake 测试。

`StrictPlanningDecisionParser` 要求 schema v1、顶层对象、严格 decision enum 和字段集合。它拒绝 policy、execution kind、dependency、optional dependency、callable、driver、provider/model、Runtime status 和 output/result type 等越权字段；异常不保存 raw output。

### StepInvocationBindings

- `AgentInvocationSpec` 非 dataclass、不可变、安全 repr。
- Bindings 只按 `step_id + expected_agent_id` 解析单条 spec。
- 没有 `get_all()`，不支持 pickle，unknown/mismatch/closed 都是稳定安全错误。
- `close_and_clear()` 幂等并清空内部 raw 引用；WP2 再增加真实 claim 校验。

### ResolvedPlan

`ResolvedPlan(plan, invocation_bindings, planning_source)` 为 frozen+slots，repr 排除 bindings。构造时调用 `PlanGraphValidator`，校验 Step ID 与 Binding key 集合完全相等、数量一致、Agent 一致以及 WP1 input type。

### PlanCompileError / Compiler

`PlanCompileError` 持有 `PlanCompileErrorCode` 与 safe message，不包含 instruction/path/raw model response。`PlanCompileConfig` 当前上限为 8 specialist、9 Step、单 instruction 8000 字符、总 instruction 24000 字符。

Compiler 决定所有 output policy、execution kind、固定依赖和 synthesis。Plan ID 只对 safe graph identity 做稳定 SHA-256 后截断，不含 instruction 或其 digest；Step ID 来自经严格校验并稳定排序的 task ID。

### Resolver

顺序为：

1. Registry 校验 selected Agent。
2. 非 model-direct entry Agent：确定性 DirectAnswerDecision，模型调用为 0。
3. Core 高置信 greeting、显式 Agent 别名或受限文档后缀规则。
4. 未决请求调用 PlanningModel 恰好一次。
5. strict parse。
6. Compiler。
7. 任一失败直接抛出；不创建 Core fallback Plan。

## 5. Four Legal Graphs

| 形态 | 输入示例 | 编译 Step | Policy / dependency | Binding | 测试 |
|---|---|---|---|---|---|
| 0 Core direct | model `DIRECT_ANSWER(core_router)` 或 greeting | `answer/core_router` | FINAL_PASSTHROUGH / 无依赖 | `answer` 保存 raw instruction | `test_shape_0_core_direct`; Resolver direct 测试 |
| 1 Entry specialist | selected knowledge/code/data | `answer/<selected>` | FINAL_PASSTHROUGH / 无依赖 | `answer` | `test_shape_1_authorized_explicit_entry_specialist` |
| 1 Delegated knowledge direct | 唯一 knowledge task、`synthesis_required=false` | `task-knowledge/knowledge_expert` | FINAL_PASSTHROUGH / 无依赖 | `task-knowledge` | `test_shape_1_single_delegated_knowledge_direct` |
| 2 Single+synthesis | 单 code/data 或显式要求 synthesis | `task-*`、`synthesis` | INTERNAL；FINAL_SYNTHESIS 依赖 specialist | 两个 key | `test_shape_2_single_specialist_and_synthesis` |
| 3 Fan-out+synthesis | N>=2 specialist | 按 task ID 稳定排序的 `task-*`、唯一 `synthesis` | roots 全 INTERNAL；synthesis 依赖全部 roots | 每 Step 一条 | `test_shape_3_fanout_is_stably_sorted_and_has_one_final_source` |

每个测试都断言 Step ID、Agent、depends_on、execution_kind、output_policy、唯一 final、Binding parity，并证明 raw instruction 不在 Plan repr。

## 6. Rejection Matrix

| 场景 | 稳定 error code | 拒绝层 | fallback | 主要测试 |
|---|---|---|---|---|
| unknown selected Agent | `UNKNOWN_AGENT` | Registry/Resolver | NO | `test_unknown_and_synthesis_selected_agents_fail_without_model_or_fallback` |
| unknown Planner Agent | `UNKNOWN_AGENT` | Compiler | NO | `test_schema_compile_and_model_failures_never_fallback_to_core` |
| disabled Agent | `AGENT_DISABLED` | Registry/Compiler | NO | Registry disabled 测试 |
| synthesis 作为 entry | `ENTRY_AGENT_NOT_ALLOWED` / `SYNTHESIS_ENTRY_FORBIDDEN` | Resolver/Compiler | NO | Registry、Compiler matrix |
| Core/synthesis 作为 specialist | `DELEGATED_AGENT_NOT_ALLOWED` | Compiler | NO | typed decision matrix |
| Planner specialist direct | `MODEL_DIRECT_AGENT_NOT_ALLOWED` | Compiler | NO | typed decision/Resolver failure |
| empty tasks | `EMPTY_TASKS` | Compiler | NO | typed decision matrix |
| duplicate task ID | `DUPLICATE_TASK_ID` | Compiler | NO | typed decision matrix |
| invalid/overlong/reserved task ID | `INVALID_TASK_ID` | Compiler | NO | typed decision matrix |
| empty instruction | `PLANNER_SCHEMA_INVALID`（模型）/`EMPTY_INSTRUCTION`（Compiler defense） | Parser/Compiler | NO | parser matrix/Compiler guard |
| instruction 过长 | `INSTRUCTION_LIMIT_EXCEEDED` | Compiler | NO | limit test |
| Plan instruction 总量超限 | `PLAN_INSTRUCTION_LIMIT_EXCEEDED` | Compiler | NO | limit test |
| Agent/Step 数超限 | `PLAN_LIMIT_EXCEEDED` | Compiler | NO | hard-limit test |
| invalid capability | `INVALID_CAPABILITY` | Compiler | NO | typed decision matrix |
| invalid input type | `INVALID_INPUT_TYPE` | Compiler | NO | typed decision matrix |
| output/result type/policy 越权字段 | `PLANNER_FIELD_FORBIDDEN` | Parser | NO | strict parser matrix |
| optional dependency | `OPTIONAL_DEPENDENCY_UNSUPPORTED` | Parser | NO | strict parser matrix |
| arbitrary dependency/self/cycle 字段 | `PLANNER_FIELD_FORBIDDEN` | Parser | NO | strict parser matrix |
| 手工候选缺失依赖 | `MISSING_DEPENDENCY` | Compiler + existing DAG validator | NO | defensive graph matrix |
| 手工候选自依赖 | `SELF_DEPENDENCY` | Compiler + existing DAG validator | NO | defensive graph matrix |
| 手工候选环 | `DEPENDENCY_CYCLE` | Compiler + existing DAG validator | NO | defensive graph matrix |
| multiple final | `MULTIPLE_FINAL_STEPS` | Compiler | NO | defensive graph matrix |
| no final | `NO_FINAL_STEP` | Compiler | NO | defensive graph matrix |
| 非法 synthesis/final policy | `FINAL_POLICY_NOT_ALLOWED` | Compiler | NO | defensive graph matrix |
| 单 code/data 要求 passthrough | `DIRECT_DELEGATION_NOT_ALLOWED` | Compiler | NO | typed decision matrix |
| multi task 不要求 synthesis | `INVALID_GRAPH_SHAPE` | Compiler | NO | typed decision matrix |
| unsupported schema/version/enum | `PLANNER_SCHEMA_INVALID` / `PLANNER_SCHEMA_VERSION_UNSUPPORTED` / `PLANNER_DECISION_UNKNOWN` | Parser | NO | strict parser matrix |
| Planner model exception | `PLANNING_MODEL_FAILED` | Resolver | NO | Resolver failure test |
| Plan/Binding key 或 Agent mismatch | safe `ValueError` / `BINDING_MISMATCH` | ResolvedPlan/Compiler | NO | Binding mismatch test |

Planner cancellation、deadline 和 budget 异常不被改写为普通 planning failure，原样传播给未来 WP2 生命周期 owner。

## 7. Security Boundary

- raw instruction 只在不可变 Request/Decision/InvocationSpec/Bindings 内存对象中；Plan/PlanStep 无 `instruction`、`input_digest` 或文件路径字段。
- raw-bearing 类不是 dataclass，默认 `asdict()` 拒绝；repr 固定显示 `<redacted>`。
- `ResolvedPlan` repr 排除整个 `invocation_bindings` 字段。
- parser 不将 raw model output 放入 error、cause、日志或对象。
- compiler error 只含 enum code 和固定 safe message；测试用敏感路径验证异常/`caplog` 均无泄漏。
- Bindings 无 `get_all()`、不可 pickle、Agent mismatch/unknown/closed 均 fail closed；close 后内部映射为空。
- Plan stable ID 只使用 task ID、Agent ID、图形/source 等 safe identity；raw instruction 变化不改变 Plan ID。
- 本轮没有触及 Snapshot、Checkpoint、Journal、Trace、Event 或 Memory，因此 raw 数据没有新增持久化通路。

## 8. Tests and Commands

| 阶段 | 命令 | 结果 |
|---|---|---|
| 首轮 WP1 | `uv run pytest -q tests/test_agent_registry.py tests/test_invocation_bindings.py tests/test_plan_compiler.py tests/test_multi_agent_planning.py` | 58 passed, 1 failed；ResolvedPlan agent mismatch 透出底层 typed error |
| 修复后 WP1 | 同上 | 59 passed |
| Plan/Scheduler/Factory | `uv run pytest -q tests/test_planning.py tests/test_plan_graph.py tests/test_scheduler.py tests/test_coordinated_runtime_factory.py` | 47 passed, 9 subtests passed |
| AgentRouter/Legacy | `uv run pytest -q tests/test_agent_loop.py tests/test_knowledge_routing.py tests/test_remote_llm_engine.py tests/test_runtime_mode_e2e.py` | 39 passed, 3 subtests passed |
| WP1+Plan 合同 | `uv run pytest -q tests/test_agent_registry.py tests/test_invocation_bindings.py tests/test_plan_compiler.py tests/test_multi_agent_planning.py tests/test_planning.py tests/test_plan_graph.py` | 80 passed |
| Stage2 关键 Runtime | `uv run pytest -q tests/test_run_coordinator.py tests/test_runtime_event_integration.py tests/test_snapshot_contract.py tests/test_plan_fingerprint.py tests/test_recovery_validation.py tests/test_runtime_mode_e2e.py` | 66 passed, 4 subtests passed |
| 最终 WP1 专项 | 四个 WP1 测试文件 | 66 passed |
| 全仓 | `uv run pytest -q` | 1155 passed, 42 subtests passed |
| 编译 | `uv run python -m compileall -q core tests server.py main.py` | PASS |
| whitespace | `git diff --check` | PASS；只有 Git 的 LF/CRLF 提示，无 whitespace error |

首次失败的根因：`ResolvedPlan.__post_init__` 在 Binding Agent mismatch 时让 `InvocationBindingError` 穿透，而测试期望构造边界统一为安全 `ValueError`。修复为捕获底层错误并抛出不含 ID/正文的固定消息；未删除或放宽测试。

执行环境没有仓库级 Ruff/MyPy 配置，因此未新增或强制引入工具链。`uv` 在普通沙箱一次无法读取用户级 Python 目录，随后在已批准的相同 `uv run` 范围重跑成功；这不是测试失败。

## 9. Compatibility and Regression

- Legacy Runtime：未改编排/执行逻辑；`agents_config` 和 `delegate_agent_ids` 改为从 Registry 派生，相关回归通过。
- static Coordinated：`PlanStep` 新字段位于末尾且有兼容默认；现有 factory/scheduler/coordinator 测试通过。
- Plan/Scheduler：Scheduler 仍只读取既有 claim 字段；没有动态初始化或新 claim 行为。
- default API：没有导入或调用新 Resolver，外部请求/响应不变。
- Snapshot/fingerprint：schema 与代码均未修改；现有回归通过。新字段进入 v2 fingerprint 属于后续工作。
- Streaming/Event：没有修改事件类型、顺序、adapter 或前端。
- Recovery/Memory：没有增加恢复、结果或 instruction 持久化行为。

## 10. Deviations from Consensus

No deviations from the Stage 2.5 architecture consensus were introduced in WP1.

安全实现细节上的调整（raw-bearing 类型不用 dataclass）实现了同一合同，并强化了 `asdict()` 边界，不改变架构语义。

## 11. Known Limitations After WP1

- 新 Resolver 尚未接入默认 API。
- Coordinator 尚不支持 dynamic Plan 生命周期。
- 多 Agent 尚未真实执行。
- 没有 StepResultStore。
- 没有 OutputGate 或 delivery status。
- 没有 MultiAgentDriver 或 Synthesis Runtime。
- 没有 planning Runtime events。
- Snapshot/fingerprint v2 尚未实现。
- Bindings 只校验 step ID + expected Agent，尚未接入真实 Scheduler claim。
- Planner 只有 Protocol/strict parser，没有在生产入口实例化 adapter。
- 不能宣称 Stage 2.5 完成或默认 Runtime 已支持多 Agent。

## 12. WP1 Acceptance Evidence

| 验收组 | 证据 | 状态 |
|---|---|---|
| Registry 初始 Agent/唯一 ID/unknown/synthesis entry/权限分离 | `tests/test_agent_registry.py` | PASS |
| Registry immutable/repr/非法 registration | 同上 | PASS |
| 四种合法图及精确 Step/policy/dependency/binding | `tests/test_plan_compiler.py` shape tests | PASS |
| unknown/disabled/entry/delegated/policy 权限 | Registry + Compiler matrix | PASS |
| 空/重复/非法任务和资源上限 | Compiler matrix/limit tests | PASS |
| DAG 缺失、自依赖、环、多 final、无 final、非法 sink | defensive graph matrix，复用 `PlanGraphValidator` | PASS |
| optional/arbitrary dependency 与 Planner permission 字段 | strict parser matrix | PASS |
| 显式 knowledge/code/data 零 Planner 调用 | parameterized Resolver test | PASS |
| synthesis/unknown selected fail closed | Resolver test | PASS |
| Core greeting、knowledge、code、knowledge+code、`.md` 文档规则 | deterministic Resolver test | PASS |
| 未决请求一次 model call 与四类 typed model 输出 | Resolver model tests | PASS |
| schema/compile/model failure 无 Core fallback | Resolver failure matrix | PASS |
| raw instruction 不进 Plan/repr/asdict/log/error | security tests + code audit | PASS |
| raw Planner output 不进 error | strict parser tests | PASS |
| Binding parity/mismatch/unknown/close/clear/no get_all/no pickle | Binding + ResolvedPlan tests | PASS |
| Legacy/static Coordinated/Scheduler/Stage2 关键回归 | 第 8 节命令 | PASS |

## 13. WP2 Interface Needs

WP2 只应消费以下已稳定接口，不应绕过：

- `PlanResolver.resolve(PlanningRequest, RunContext) -> ResolvedPlan`
- `PlanningModel` 的统一模型服务 adapter
- `ResolvedPlan.plan`
- `ResolvedPlan.invocation_bindings`
- `PlanningSource`
- `PlanningErrorCode`、`AgentRegistryErrorCode`、`PlanCompileErrorCode` 到 Run planning failure 的映射
- Plan freeze 前后的 lifecycle hook
- dynamic 初始化时将 Plan 交给 Scheduler，并在真实 claim 后按 Step/Agent 读取 Binding
- Run terminal/cancel/detached cleanup 时调用 `close_and_clear()`

WP2 仍需完成但本轮未实现：planning events、一次 freeze、`POST_PLAN_PRE_EXECUTION` checkpoint、`StopReason.PLANNING_FAILED`、Snapshot/fingerprint v2、Coordinator error mapping 和默认 API 接线。

## 14. Final Status

```text
WP1 status: PASS
Architecture deviations: 0
P0 findings: 0
P1 findings: 0
P2 findings: 0
Default API multi-agent enabled: NO
Production multi-agent execution enabled: NO
Ready for GPT review: YES
Ready to start WP2: YES
```

`Ready to start WP2: YES` 只表示 WP1 验收条件满足；在用户另行明确授权前，不得开始 WP2。
