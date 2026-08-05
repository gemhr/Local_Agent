# Stage 2.5 WP1 Acceptance Supplement

## 1. Direct Answer instruction 数据流

### 审查前事实

本轮发现并修复了一个真实 WP1 权限缺口：模型返回 `DIRECT_ANSWER` 时，旧实现由 `StrictPlanningDecisionParser` 读取模型的 `instruction`，存入 `DirectAnswerDecision`，再由 `PlanCompiler._compile_direct` 直接写入 `core_router` 的 `AgentInvocationSpec`。因此旧答案是 **PLANNER_OUTPUT**，不符合“Planner 只决定路由、无权改写用户请求”的合同。

### 修复后的完整路径

```text
PlanningModel raw response
  core/runtime/multi_agent_planning.py:222-224, 398
-> StrictPlanningDecisionParser.parse
  core/runtime/multi_agent_planning.py:238-282
-> DirectAnswerDecision(agent_id, reason_code)
  core/runtime/multi_agent_planning.py:89-112
-> PlanResolver 保留 PlanningRequest.user_request 并显式传给 Compiler
  core/runtime/multi_agent_planning.py:362-422
-> PlanCompiler.compile / _compile_direct
  core/runtime/plan_compiler.py:108-125, 212-235
-> AgentInvocationSpec -> StepInvocationBindings
  core/runtime/plan_compiler.py:230-233
  core/runtime/invocation_bindings.py:27-78, 82-100
```

最终绑定给 `core_router` 的 instruction 是原始 `PlanningRequest.user_request`，不是模型输出。具体权限边界如下：

- `PlanningRequest.user_request` 的唯一原始请求字段位于 `core/runtime/multi_agent_planning.py:63-69, 81-83`。
- Direct schema 只允许 `schema_version/decision/agent_id/reason_code`；若模型附带 `instruction`，在 `core/runtime/multi_agent_planning.py:266-275` 以 `PLANNER_FIELD_FORBIDDEN` fail closed。
- `DirectAnswerDecision` 在 `core/runtime/multi_agent_planning.py:89-112` 已删除 instruction，仅表达 direct 决策。
- `PlanResolver.resolve` 在 explicit、deterministic direct、model direct 三条路径分别把原始请求作为 `direct_instruction` 传入 Compiler，见 `core/runtime/multi_agent_planning.py:370-394, 413-422`。
- `PlanCompiler.compile` 拒绝“Direct 无原始 instruction”以及“Delegated 携带 direct_instruction”，见 `core/runtime/plan_compiler.py:108-124`；`_compile_direct` 最终在 `core/runtime/plan_compiler.py:212-233` 构造 Binding。
- `DelegatedTaskDecision.instruction` 未删除；Compiler 仍在 `core/runtime/plan_compiler.py:279, 294` 按 typed task 构造 specialist Binding。

结论：Direct Answer instruction authority 为 **ORIGINAL_REQUEST**；Planner 的 Direct instruction 字段采用“删除并禁止”，不是静默忽略。

## 2. 实际代码与测试证据

新增/强化的关键证据：

- `tests/test_multi_agent_planning.py:174-197`：model direct 的 Binding 严格等于原始请求；delegated specialist Binding 仍等于 Planner typed task instruction。
- `tests/test_multi_agent_planning.py:201-231`：模型伪造 Direct instruction 被拒绝，Compiler 调用次数为 0，伪造正文不进入 Plan、Binding、异常或日志。
- `tests/test_multi_agent_planning.py:69-79`：不带 instruction 的 Direct schema 与带 instruction 的 Delegated schema 均能解析为正确 typed decision。
- `tests/test_plan_compiler.py:53-73`：Direct/explicit entry 图必须由调用方显式提供原始 direct instruction。
- `tests/test_agent_registry.py:14-40, 85-103`：默认 Adapter ID 映射唯一、稳定，非法符号 ID 被拒绝。

不存在“解析模型伪造 instruction 后继续执行”的兼容路径；这是刻意的 fail-closed 选择。若模型遵守 Direct schema，Binding 使用原始请求；若模型越权附带 instruction，则整次 planning 失败且不产生 Plan/Binding。

## 3. Registry / Adapter 最终合同

选择 **方案 A：AgentRegistry 保存符号执行引用**。

`AgentRegistration` 的当前完整实际字段位于 `core/runtime/agent_registry.py:42-61`：

```text
agent_id
execution_adapter_id
display_name
role
avatar
enabled
entry_allowed
entry_output_policy
model_direct_allowed
delegation_allowed
delegated_output_policy
allows_single_delegated_passthrough
synthesis_only
supports_parallel
accepted_input_types
produced_result_types
capabilities
deterministic_aliases
```

`execution_adapter_id` 是经 `_SAFE_TYPE_ID` 校验的稳定字符串，验证见 `core/runtime/agent_registry.py:63-67`；默认注册表在 `core/runtime/agent_registry.py:253-291` 为五个 Agent 明确配置唯一 Adapter ID。该字段不保存 callable、实例、provider secret 或生命周期对象。

WP3 冻结调用合同：

```text
MultiAgentDriver receives claimed agent_id
-> AgentRegistry.resolve(agent_id)
-> registration.execution_adapter_id
-> independent AgentAdapterFactory.resolve(execution_adapter_id)
-> typed Agent adapter
```

- `AgentRegistry` 是 Agent 身份、权限、能力和“执行适配符号”的事实源。
- 独立 `AgentAdapterFactory` 负责 Adapter 实现解析、构造与进程级生命周期；`MultiAgentDriver` 只消费，不拥有注册规则。
- 禁止 `MultiAgentDriver` 按 `agent_id` 编写散落的 `if/elif`，也禁止把 callable、实例或 secret 塞进 `AgentRegistration`。
- WP1 只冻结并验证符号合同，没有实现真实 Adapter、Factory 或 MultiAgentDriver；不能声称 WP1 Registry 已经是完整的可执行实例事实源。

Registry execution binding contract：**RESOLVED**。

## 4. Static Plan 默认策略风险

源码核验结果：

- `PlanStep.output_policy` 的兼容默认确实是 `FINAL_PASSTHROUGH`，见 `core/runtime/planning.py:63-74`。
- 当前生产静态入口 `AgentRouter.build_single_agent_plan` 只调用 `create_single_step_plan`，见 `core/agent_router.py:952-956`，未发现生产代码手写的多 Step static Plan。
- 仓库测试/fixture 存在多 Step static Plan，并依赖该默认值。例如 `tests/test_planning.py:9-14` 的两个 Step 和 `tests/test_parallel_execution.py:125-133` 的两个 Step 都未显式指定 output policy；此外 Scheduler、Recovery、Checkpoint、Snapshot 等测试 fixture 也存在同类构造。

当前尚未接入 OutputGate，因此不会在现有 Runtime 中实际触发“多个旧 Step 均作为 final 发布”。但 WP4 若直接按 `output_policy != INTERNAL` 接入 Gate，上述多 Step 旧构造会被解释为多个 final。

分类：**DEFERRED_TO_WP4**。WP4 必须在接入 OutputGate 同一变更中审计并迁移所有多 Step static Plan/fixture：中间 Step 显式标记 `INTERNAL`，唯一终结 Step 显式标记 `FINAL_PASSTHROUGH` 或 `FINAL_SYNTHESIS`。本轮不提前实现 OutputGate，也不修改兼容默认。

## 5. 实际修改文件

生产代码（严格限于 WP1 planning/registry/compiler）：

- `core/runtime/multi_agent_planning.py`
- `core/runtime/plan_compiler.py`
- `core/runtime/agent_registry.py`

测试：

- `tests/test_multi_agent_planning.py`
- `tests/test_plan_compiler.py`
- `tests/test_agent_registry.py`

结果文档：

- `docs/learning/stage2/result/stage2_5_wp1_implementation_result.md`
- `docs/learning/stage2/result/stage2_5_wp1_acceptance_supplement.md`

## 6. 测试命令和数量

| 命令 | 结果 |
|---|---|
| `uv run pytest -q tests/test_agent_registry.py tests/test_plan_compiler.py tests/test_multi_agent_planning.py tests/test_invocation_bindings.py` | 68 passed |
| `uv run pytest -q` | 1157 passed, 42 subtests passed |
| `uv run python -m compileall -q core tests server.py main.py` | PASS |
| `git diff --check` | PASS；仅有 Git 的 LF/CRLF 工作区提示，无 whitespace error |

## 7. 是否修改了生产代码

**是。** 修复了真实存在的 WP1 Direct instruction authority 缺口，并为 Registry 增加 WP3 所需的稳定符号 Adapter ID。修改仅发生在允许的 WP1 planning/registry/compiler 范围。

## 8. 是否触及 WP2 范围

**否。** 未修改 Runtime 生命周期、默认 API、Coordinator、Scheduler、Snapshot、Streaming、Store、Driver、OutputGate、Event、Recovery、Memory 或前端；未接线 Resolver，未实现 Adapter Factory/MultiAgentDriver，未开始 WP2。

```text
WP1 supplementary review: PASS
Direct Answer instruction authority: ORIGINAL_REQUEST
Registry execution binding contract: RESOLVED
Static Plan output-policy risk: DEFERRED_TO_WP4
P0 findings: 0
P1 findings: 0
Ready to start WP2: YES
```
