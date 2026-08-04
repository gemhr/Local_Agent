# LocalAgent Stage 2.5 Multi-Agent WP1 面试材料

> 适用范围：Stage 2.5 架构评审与 WP1 实现。本文不会把 WP1 描述成已经接入默认 API，也不会把测试构造描述成真实生产事故。

## 1. 推荐的面试材料模板

一份可信、可追问的工程面试材料，建议固定包含：

1. **真实性声明**：区分真实用户复现、源码审计发现、测试期发现和假设构造。
2. **一句话项目定义**：说明解决什么问题，不先堆技术名词。
3. **真实用户场景**：谁在什么入口提出什么请求，期望链路是什么。
4. **故障证据链**：请求、日志、状态、调用链及缺失事件分别证明什么。
5. **问题抽象**：从一次故障提炼身份、路由、权限、数据和失败合同。
6. **候选方案讨论**：方案、优点、缺点、拒绝原因和最终选择。
7. **最终架构**：组件、owner、数据流、合法图形和 fail-closed 规则。
8. **实际实现**：只写已经落地的代码、接口和测试。
9. **关键难点**：解释为什么难、错误方案为何危险、如何验证。
10. **Bad Cases**：统一格式并明确真实性。
11. **验证证据**：精确命令、通过数、失败修复过程和未执行项。
12. **能力边界与下一步**：说明当前不能做什么。
13. **面试表达版本**：准备 30 秒、2 分钟和深入追问三种粒度。

可复用骨架：

```markdown
# 项目 / 工作包名称
## 真实性声明
## 一句话项目定义
## 真实用户场景
## 故障证据链
## 问题抽象
## 候选方案与取舍
## 最终架构和 Owner
## 实际实现
## 关键难点
## Bad Cases
## 测试和验收证据
## 当前边界与下一步
## 30 秒 / 2 分钟 / 深入追问表达
```

Bad Case 一律使用：

```markdown
### Bad Case X：名称

- 类型：真实发现 / 假设构造
- 触发条件：
- 故障表现：
- 根因分析：
- 修复方案：
- 回归测试：
- 对应知识点：
- 面试表达：
- 当前状态：
```

## 2. 一句话项目定义

LocalAgent Stage 2.5 的目标不是“多调用几个模型”，而是把自然语言请求可靠地编译成经过 Agent 权限校验、固定图形校验和敏感数据隔离的不可变 Runtime Plan，并保证失败时不由主 Agent 静默补答。

WP1 完成的是规划与编译合同：`AgentRegistry + PlanningDecision + strict parser + PlanResolver + PlanCompiler + StepInvocationBindings`。默认 API 接入、动态 Run 生命周期和真实多 Agent 执行仍属于后续工作。

## 3. 真实用户场景与实际问题

### 3.1 用户场景

真实使用者在 LocalAgent 主 Agent 入口输入：

```text
调用知识专家，总结 cdt_field_mapping.md
```

也尝试过“讲讲 cdt_field_mapping.md”“查找 cdt_field_mapping.md”。用户期望主 Agent 实际调用 `knowledge_expert`，执行 retrieval 并依据本地信源总结；找不到信源时应明确失败，不能依靠主 Agent 常识补写。

实际观察：直接选择 `knowledge_expert` 时回答符合要求；从主 Agent 请求委派时没有正确分发，主 Agent 自行生成答案。这里的“答案存在编造问题”来自用户的真实反馈；日志能直接证明主 Agent Run 没有 retrieval，而直接知识专家 Run 执行了 retrieval，但日志没有回答正文，因此本文不虚构具体错误内容。

真实性：这是**用户提供的本地真实复现**，不是公开生产环境事故。

### 3.2 日志证据链

主 Agent Run `0ba401a2...`：

```text
RUN_STARTED -> STEP_STARTED(answer)
MODEL_STARTED -> MODEL_COMPLETED
OUTPUT_DELTA -> STEP_COMPLETED -> RUN_COMPLETED(SUCCEEDED)
```

该 Run 没有 `RETRIEVAL_STARTED`、`RETRIEVE`、`DOCUMENT_LOAD` 或 citation 事件。它只能证明模型调用成功，不能证明知识专家或知识库被调用。

直接知识专家 Run `33474a4a...`：

```text
RUN_STARTED -> STEP_STARTED(answer)
RETRIEVAL_STARTED(collection_count=1, top_k=8)
QUERY_REWRITE -> EMBEDDING -> RETRIEVE(output_count=8)
RERANK -> DOCUMENT_LOAD -> CONTEXT_BUILD
RETRIEVAL_COMPLETED(chunk_count=1, citation_count=1)
MODEL_STARTED -> MODEL_COMPLETED
OUTPUT_DELTA -> STEP_COMPLETED -> RUN_COMPLETED(SUCCEEDED)
```

对比结论：知识能力本身可用，缺陷位于主入口的 resolution/delegation 边界，而不是 retrieval 或知识专家模型链路。

## 4. 从一次故障抽象出的工程问题

这不是补一条关键词就能完整解决的问题，至少包含：

1. **身份合同**：系统有哪些 Agent，谁可作为 entry，谁只能被委派。
2. **规划合同**：显式 Agent、确定性规则和模型 Planner 的优先级。
3. **权限合同**：Planner 只能提出任务，不能授予 final output 权限。
4. **图合同**：只允许哪些图，如何拒绝环、多 final、无 final和非法依赖。
5. **数据合同**：raw instruction 放在哪里，如何避免进入 Plan/Snapshot/日志。
6. **失败合同**：unknown Agent、schema failure 或 specialist failure是否回退 Core。
7. **真实性合同**：类型实现、默认入口接线和生产执行能力分别陈述。

## 5. 方案讨论与取舍

| 方案 | 优点 | 主要问题 | 结论 |
|---|---|---|---|
| 只修改 Core prompt | 改动小 | 模型仍可忽略委派；无法证明调用发生 | 拒绝作为架构方案 |
| 增加更多关键词规则 | 少量固定指令确定性强 | Agent 越多规则越散；权限、图形、失败语义仍缺失 | 只保留高置信 deterministic rule |
| 复用 Legacy `Delegate: agent | task` | 已有代码、成本低 | 非法行被忽略；无版本、权限和严格 schema | 拒绝进入新 Runtime Plan |
| Planner 直接生成完整 DAG/policy | 灵活 | 模型可发明 Agent、依赖和 final 权限 | 拒绝 |
| 所有请求强制模型 Planner | 路径统一 | 显式 Agent 也增加成本与不确定性 | 拒绝 |
| Registry + Resolver + typed Decision + Compiler | 权限集中、显式路由确定、模型输出可严格拒绝 | 合同和测试工作量较大 | 采用 |

### 5.1 为什么不是“关键词路由即可”

关键词路由能修复 `讲讲 xxx.md` 这一类明确场景，却不能回答代码专家能否直接输出、模型能否声明 FINAL、多个专家如何汇总、unknown Agent 如何失败、instruction 是否进入持久化对象。它只能是 Resolver 的确定性分支。

### 5.2 为什么 Planner 不能决定 output policy

`FINAL_PASSTHROUGH` 是向用户发布正文的能力。如果让模型返回 policy，就等于让不可信输入给自己授权。WP1 中 Planner 只能提出 direct/delegate task，Compiler 根据 Registry 决定 `INTERNAL`、`FINAL_PASSTHROUGH` 或 `FINAL_SYNTHESIS`。

### 5.3 为什么 raw instruction 不进入 Plan

Plan 可能进入 repr、Snapshot、fingerprint、Journal 或诊断链。用户请求可能含文件路径、查询词和业务数据。WP1 将调度合同与调用正文分离：Plan 只保存安全结构，raw instruction 只在 run-scoped Bindings 中按 Step/Agent 读取。

## 6. 最终架构与四种合法图

```text
PlanningRequest
  -> PlanResolver
       -> explicit entry: deterministic, 0 planner calls
       -> high-confidence core rule
       -> unresolved: PlanningModel Protocol
  -> StrictPlanningDecisionParser
  -> PlanCompiler
       -> AgentRegistry permission check
       -> fixed graph construction
       -> PlanGraphValidator
       -> resource/security limits
  -> ResolvedPlan
       -> immutable safe Plan
       -> run-scoped StepInvocationBindings
```

仅允许：

```text
0. core_router [FINAL_PASSTHROUGH]
1. authorized entry specialist [FINAL_PASSTHROUGH]
2. specialist [INTERNAL] -> synthesis_agent [FINAL_SYNTHESIS]
3. specialist_1..N [INTERNAL] -> synthesis_agent [FINAL_SYNTHESIS]
```

| 事实 | Owner |
|---|---|
| Agent 身份、entry/delegated 权限、capability | `AgentRegistry` |
| direct/delegate task 提议 | `PlanningDecision` |
| Planner schema | `StrictPlanningDecisionParser` |
| policy、execution kind、固定依赖、合法图 | `PlanCompiler` |
| DAG 环/依赖结构 | 既有 `PlanGraphValidator` |
| raw instruction | `StepInvocationBindings` |
| Plan/Binding parity | `ResolvedPlan` |

## 7. WP1 实际实现

### 7.1 Registry 与 Resolver

- 五个初始 Agent：core、data、code、knowledge、synthesis。
- entry 与 delegated permission 分离。
- knowledge 只有“唯一 delegated task、无其他 Step”可 direct passthrough。
- code/data delegated 为 INTERNAL，并生成 synthesis。
- synthesis 禁止 entry，只由 Compiler 创建。
- 显式 knowledge/code/data 规划调用模型次数为 0。
- 未决请求才调用 `PlanningModel`；任一失败不生成 fallback Plan。

### 7.2 Strict Planner Schema

- schema version 固定为 1，只接受 `DIRECT_ANSWER` 或 `DELEGATE`。
- unknown field 明确拒绝。
- 拒绝 policy、execution kind、dependency、optional dependency、callable、driver、provider、Runtime state 和 output/result type。
- raw model output 不进入异常文本或 cause。

### 7.3 Compiler 与数据边界

- 稳定 task/step ID，不使用 Python random hash，也不使用 raw instruction digest。
- 复用现有 DAG validator。
- 检查权限、唯一 final、唯一 sink、fan-out、synthesis 依赖全集。
- 硬限制 8 个 specialist、9 个 Step、单 instruction 8000 字符、总 instruction 24000 字符。
- raw-bearing Request/Decision/InvocationSpec 不使用 dataclass，因为 `repr=False` 不能阻止 `asdict()` 导出字段。
- Bindings 无 `get_all()`、不可 pickle，关闭后清空 raw 引用。

## 8. 高价值 Bad Cases

### Bad Case 1：主 Agent 回答了知识文档问题，但实际没有调用知识专家

- 类型：真实发现（用户提供的本地真实复现，不是公开生产事故）
- 触发条件：用户在主 Agent 输入“调用知识专家，总结 cdt_field_mapping.md”或类似文档请求。
- 故障表现：主 Agent Run 只有 model events，没有 retrieval；直接知识专家 Run 有完整 retrieval/citation 链。用户报告主 Agent 答案不符合信源要求。
- 根因分析：默认主入口没有强制经过 typed Resolver/Compiler；“模型回答成功”被误当成“专业能力已执行”。
- 修复方案：WP1 实现 deterministic document/explicit-agent resolution、Registry 权限和固定图 Compiler；默认 API 接线留给 WP2。
- 回归测试：`test_core_deterministic_direct_knowledge_code_and_fanout_rules` 覆盖显式知识指令和 `讲讲 *.md`。
- 对应知识点：路由可观测性、能力调用证明、fail closed、RAG grounding。
- 面试表达：我没有用答案文本猜路由是否成功，而是对比 Runtime event，证明缺的是 delegation，不是 retrieval 能力。
- 当前状态：WP1 规划合同已修复并通过测试；默认 API 尚未接线，不能宣称用户入口已修复。

### Bad Case 2：Legacy 自由文本 Delegate 解析失败后退化成 Core 自答

- 类型：真实发现（源码路径；用户复现与风险一致，但未单独统计生产事故）
- 触发条件：模型未严格输出 `Delegate: agent_id | task`，或输出 unknown Agent、附加说明、格式漂移。
- 故障表现：Legacy parser 忽略非法行，delegate 列表为空；后续路径可能按“无需委派”继续 Core 回答。
- 根因分析：自由文本 parser 把“解析失败”和“合法 direct answer”合并成同一个空列表状态。
- 修复方案：新 Planner 使用版本化 strict JSON；schema failure 是显式错误，绝不转换为 DirectAnswerDecision。
- 回归测试：malformed JSON、unknown enum、forbidden field、unknown Planner Agent、model exception 均断言失败且无 fallback。
- 对应知识点：sum type、错误状态不可折叠、strict parsing、fail closed。
- 面试表达：空列表不能同时表示“不需要专家”和“规划解析失败”，我用 typed decision 消除了这个二义性。
- 当前状态：WP1 新路径已防止；Legacy 行为未修改，新 Resolver 尚未替代默认入口。

### Bad Case 3：ResolvedPlan 对 Binding Agent mismatch 透出底层异常

- 类型：真实发现（WP1 首轮测试发现，不是生产事故）
- 触发条件：PlanStep 的 `preferred_agent` 与同 step_id Binding 的 `agent_id` 不一致。
- 故障表现：首轮专项测试为 58 passed、1 failed；`ResolvedPlan` 直接透出 `InvocationBindingError`，没有在聚合合同边界收口。
- 根因分析：底层错误虽安全，但 `ResolvedPlan` 构造器没有统一自身的不变量失败语义。
- 修复方案：捕获底层 Binding 错误并转换为固定安全 `ValueError`；Compiler 对内部不一致映射 `BINDING_MISMATCH`。
- 回归测试：`test_resolved_plan_rejects_missing_extra_and_agent_mismatch_bindings`。
- 对应知识点：聚合根不变量、异常抽象层次、错误信息最小化。
- 面试表达：我把 Plan 与 Binding 一致性放在聚合构造边界验证，避免上层依赖底层异常细节。
- 当前状态：已修复；最终 WP1 66 passed，全仓 1155 passed、42 subtests passed。

### Bad Case 4：公开 validate_plan 接受 Core 作为 specialist

- 类型：真实发现（最终源码审计发现，不是生产事故）
- 触发条件：绕过正常 `compile()`，手工构造 `core_router [INTERNAL] -> synthesis_agent` 后调用公开 `validate_plan()`。
- 故障表现：正常 Compiler 路径已拒绝 Core delegation，但防御性 validator 最初只检查 DAG/形态，可能接受无 delegated permission 的手工候选。
- 根因分析：权限校验只存在于构建路径，没有在公开验证边界重复执行。
- 修复方案：`validate_plan()` 对 direct entry、delegated permission、parallel support、上限和 capability 再做 defense-in-depth 校验。
- 回归测试：defensive graph rejection matrix 新增 Core specialist，期望 `DELEGATED_AGENT_NOT_ALLOWED`。
- 对应知识点：TOCTOU、defense in depth、构建与验证路径一致性。
- 面试表达：不能假设所有 Plan 都由当前 builder 产生；公开 validator 必须独立验证权限。
- 当前状态：已修复并纳入最终 66 个 WP1 专项测试。

### Bad Case 5：Registry 对外只读，但内部槽位仍可重绑

- 类型：真实发现（WP1 安全审计发现，不是生产事故）
- 触发条件：调用方直接给 Registry 私有槽位重新赋值。
- 故障表现：MappingProxy 能防止 map item 修改，却不能自动防止 Registry 的 `_registrations/_ordered_ids` 被整体替换。
- 根因分析：只冻结了容器内容，没有冻结 owner 对象本身。
- 修复方案：Registry 增加锁定槽位和 `__setattr__` 防护；Registration 使用 frozen dataclass。
- 回归测试：`test_registry_and_registrations_are_immutable_and_repr_safe` 尝试公开属性和私有槽位重绑。
- 对应知识点：浅不可变与深不可变、MappingProxy、配置 authority。
- 面试表达：只读视图不等于 owner 不可变，我同时冻结 value、mapping 和 Registry identity。
- 当前状态：已修复并通过回归。

### Bad Case 6：`repr=False` 被误认为能阻止 asdict 泄漏

- 类型：假设构造（源于候选设计审查，没有观察到真实生产泄漏）
- 触发条件：raw instruction 放在 `dataclass(field(repr=False))`，通用日志代码随后调用 `dataclasses.asdict()`。
- 故障表现：repr 看不到正文，但 asdict 仍导出 instruction，可能继续进入日志或持久化对象。
- 根因分析：`repr=False` 只控制 `__repr__`，不是序列化或数据流安全策略。
- 修复方案：raw-bearing 类型不用 dataclass；Bindings 禁止 pickle；Plan 完全不含 raw instruction。
- 回归测试：PlanningRequest、DirectAnswerDecision、AgentInvocationSpec 的 `asdict()` 必须抛出 TypeError；repr 不含敏感哨兵。
- 对应知识点：数据最小化、序列化攻击面、敏感 DTO 设计。
- 面试表达：我把“看不见”与“导不出”分开验证，避免把 repr 配置误当安全边界。
- 当前状态：设计层已预防；没有真实生产泄漏证据。

### Bad Case 7：Planner 自己声明 FINAL 权限

- 类型：假设构造（拒绝矩阵测试，不是生产事故）
- 触发条件：模型 JSON 返回 `output_policy=FINAL_PASSTHROUGH`、`FINAL_SYNTHESIS` 或 `execution_kind`。
- 故障表现：如果直接采信，specialist 可以绕过 synthesis 或 INTERNAL 隔离，产生多个用户输出。
- 根因分析：把不可信 Planner 输出当成授权事实，而不是任务提议。
- 修复方案：Parser 把 policy/execution/dependency 字段列为 forbidden；Compiler 根据 Registry 独占生成 policy。
- 回归测试：strict parser forbidden-field matrix，期望 `PLANNER_FIELD_FORBIDDEN`。
- 对应知识点：policy enforcement point、confused deputy、capability security。
- 面试表达：Planner 可以提需求，不能给自己发权限；授权只来自 Registry 和 Compiler。
- 当前状态：WP1 已预防并测试；没有真实生产触发记录。

### Bad Case 8：模型发明 unknown Agent 后静默回退 Core

- 类型：假设构造（拒绝矩阵测试；原始用户问题体现静默补答风险，但未提供 unknown Agent 事故）
- 触发条件：selected Agent 或 Planner task 包含未注册/disabled Agent。
- 故障表现：错误实现会 `.get(..., core_router)`，让用户以为专家任务完成，实际由 Core 回答。
- 根因分析：身份查询和 fallback 合并，unknown 被当成默认值。
- 修复方案：Registry 返回稳定错误；Compiler/Resolver 不创建 fallback Plan。
- 回归测试：unknown selected、unknown Planner Agent、disabled registration 和 schema/compile/model failure 无 fallback。
- 对应知识点：total function、显式错误、权限默认拒绝。
- 面试表达：Agent Registry 的默认值不是 Core，而是错误；否则可用性 fallback 会变成事实伪造。
- 当前状态：WP1 已预防并测试；默认 API 尚未消费该合同。

### Bad Case 9：把 raw instruction 或普通 SHA-256 放进 Plan

- 类型：假设构造（架构评审风险，不是生产事故）
- 触发条件：为稳定 ID、fingerprint 或恢复，把原文或 `SHA-256(raw instruction)` 写入 Plan/Snapshot。
- 故障表现：路径/query 泄漏；短指令和固定业务词可被字典枚举；同时 WP1 不恢复 Bindings，digest 没有执行 owner。
- 根因分析：把“可计算摘要”误当成“安全且必要的持久化事实”。
- 修复方案：Plan ID 只使用安全 graph identity；raw instruction 只在 Bindings；当前 MVP 不持久化 instruction digest。
- 回归测试：不同 raw instruction 产生相同安全 Plan ID/Step ID；PlanStep 不含 `instruction/input_digest`。
- 对应知识点：data minimization、hash dictionary attack、恢复合同。
- 面试表达：没有 owner 和恢复用途的 digest 不是资产，而是额外攻击面。
- 当前状态：WP1 已预防；Snapshot/fingerprint v2 留给后续工作包。

### Bad Case 10：类型和测试完成后就宣称默认多 Agent 已上线

- 类型：假设构造（文档真实性风险，不是生产事故）
- 触发条件：看到 Resolver/Compiler 和全仓测试通过，就写“默认 API 已支持多 Agent”。
- 故障表现：实际 `/api/chat` 未接 Resolver；Coordinator 不支持 dynamic Plan；没有 Store、Gate、Driver 或 Synthesis Runtime。
- 根因分析：把“可表达”“可编译”“已接线”“生产可用”四个层级混为一谈。
- 修复方案：结果文档固定输出 `Default API multi-agent enabled: NO`、`Production multi-agent execution enabled: NO`。
- 回归测试：范围扫描确认 server/chat service/coordinator/scheduler/snapshot/event/stream 文件未修改。
- 对应知识点：capability maturity、feature wiring、evidence truthfulness。
- 面试表达：我会明确说 WP1 完成规划编译合同，而不是把未接线的类型包装成生产能力。
- 当前状态：文档已明确边界；完成并评审后续工作包前不能改变表述。

### Bad Case 11：多个 specialist 各自向用户输出

- 类型：假设构造（Compiler 合同测试，不是生产事故）
- 触发条件：fan-out 中两个 specialist 都被标记为 `FINAL_PASSTHROUGH`，或 synthesis 之外还有 final leaf。
- 故障表现：用户看到多段冲突答案，Memory 可能保存多个 final，Run 无法定义唯一结果。
- 根因分析：没有把 final source 当成图级不变量。
- 修复方案：delegated specialist 全为 INTERNAL；多 Step 图必须有唯一 synthesis、唯一 final、唯一 sink。
- 回归测试：multiple final、no final、非法 final policy、synthesis dependency/sink 拒绝矩阵。
- 对应知识点：DAG invariants、single writer、输出收口。
- 面试表达：并行的是内部计算，不是用户输出；最终发布必须保持 single writer。
- 当前状态：WP1 Compiler 已预防；真实 OutputGate 属于后续工作。

### Bad Case 12：为非法 DAG 重新写一套环检测

- 类型：假设构造（工程实现风险，不是生产事故）
- 触发条件：Multi-Agent Compiler 自己实现 DFS/Kahn，而项目已有 `PlanGraphValidator`。
- 故障表现：两套 validator 的错误码、排序和边界不一致；Scheduler 与 Compiler 接受不同的图。
- 根因分析：没有识别既有 authority，重复实现核心不变量。
- 修复方案：Compiler 调用现有 `PlanGraphValidator`，只映射安全错误，并额外检查四形态和权限。
- 回归测试：缺失依赖、自依赖、重复 Step、环和稳定 fan-out 顺序测试。
- 对应知识点：single source of truth、validator composition、稳定拓扑序。
- 面试表达：我没有为新模块复制 DAG 算法，而是复用 Scheduler 同一验证权威，避免双标准。
- 当前状态：WP1 已按该方案实现并通过回归。

## 9. 测试与真实性证据

```text
WP1 专项：66 passed
全仓：1155 passed, 42 subtests passed
compileall：PASS
git diff --check：PASS
WP2 禁止范围文件改动：0
```

真实的中间失败也被保留：首轮 WP1 为 58 passed、1 failed，修复 ResolvedPlan 异常收口后通过。测试没有被删除、skip 或放宽。

主要测试文件：

- `tests/test_agent_registry.py`
- `tests/test_invocation_bindings.py`
- `tests/test_plan_compiler.py`
- `tests/test_multi_agent_planning.py`

## 10. 当前能力边界

已经完成：Registry 权限、显式 specialist 零模型 resolution、高置信文档规则、strict Planner schema、固定图 Compiler、资源限制和 raw instruction/Plan 分离。

尚未完成：

- 默认 `/api/chat` 接入 Resolver。
- dynamic Plan 的 Coordinator 生命周期。
- 真正并行执行 specialist。
- StepResultStore 和 dependency-scoped result read。
- OutputGate、唯一 final delivery。
- Synthesis Runtime。
- planning events、Snapshot/fingerprint v2 和 Recovery 新合同。

正确表述是“WP1 已完成生产级规划/编译模块并通过全仓回归”，而不是“LocalAgent 默认多 Agent 已上线”。

## 11. 面试表达

### 11.1 30 秒版本

我在 LocalAgent 里遇到一个真实问题：用户让主 Agent 调知识专家总结本地 Markdown，但日志显示主 Run 只有模型调用，没有 retrieval，直接知识专家却有完整检索链。我的处理不是继续堆 prompt，而是设计 Registry、typed Resolver 和 Compiler，把 Agent 权限、四种合法图、唯一 final 和 raw instruction 边界做成可测试合同。WP1 有 66 个专项测试，全仓 1155 passed；我也明确保留边界——默认 API 尚未接线。

### 11.2 2 分钟版本

问题发生在主 Agent 路由，而不是知识库本身。证据是两次 Run：主 Agent 没有 retrieval event，直接 knowledge Run 有 rewrite、embedding、retrieve、rerank、document load 和 citation。旧路径依赖 prompt 和自由文本 Delegate parser，格式失败会和合法 direct 混成空 delegate，存在静默自答风险。

我比较了纯 prompt、关键词路由、Planner 直接产 DAG 和 typed compiler。最终让 Resolver 处理 explicit/deterministic/model 三种来源；Planner 只能提出任务，Registry 管身份和权限，Compiler 独占 output policy 和固定图构造，并复用现有 DAG validator。raw instruction 不进入 Plan，而是放在不可序列化的 run-scoped Bindings。

实现中有两个真实测试/审计发现：ResolvedPlan 一度透出底层 mismatch 异常；公开 validate_plan 一度没有重复检查 Core delegated permission。两者都补了边界校验。最终 WP1 66 passed，全仓 1155 passed；当前只完成规划编译合同，Runtime 接线属于 WP2。

### 11.3 深入追问：为什么这能支持任意多 Agent

“任意多”不是无界，也不是模型能发明 Agent。扩展点是 Registry：新增 Agent 必须声明身份、input/result type、capability、entry/delegated policy 和 parallel support。Planner 仍只提出任务；Compiler 按通用规则验证，并受 max_agents/max_steps 限制。当前图保持扁平 fan-out + single synthesis，不支持 recursive delegation。

### 11.4 深入追问：如何防止幻觉

无法形式化保证模型零幻觉。这里能保证的是：需要知识专家时，路由和检索调用可由 Plan 与 Runtime event 证明；unknown/schema/compile failure 不回退 Core；INTERNAL/FINAL 权限不由模型决定。内容真实性仍需要 retrieval grounding、citation 和后续 EvalOps。

### 11.5 深入追问：为什么不直接上线关键词修复

关键词只解决部分入口表达，不能解决权限、multiple final、unknown Agent、raw instruction 泄漏和失败 fallback。我保留高置信文档/Agent 别名规则，但它必须输出 typed decision，再由 Registry/Compiler 校验。

## 12. 可用于简历的诚实表述

推荐：

> 设计并实现 LocalAgent 多 Agent 规划编译层：以 immutable AgentRegistry、strict typed Planner schema 和固定图 PlanCompiler 收口 Agent 权限、DAG、唯一 final 与敏感调用参数边界；覆盖 66 个 WP1 专项测试并保持全仓 1155 tests + 42 subtests 通过。

不推荐：

> 已上线支持任意 Agent 的生产多 Agent 系统。

原因：默认 API、dynamic Coordinator、Store/Gate/Driver/Synthesis Runtime 尚未完成。

## 13. 面试前自检清单

- 能否先说用户场景，再说技术组件？
- 能否解释日志为什么证明“未调用 retrieval”，而不是仅凭回答猜测？
- 能否区分 deterministic rule、Planner decision 和 Compiler authorization？
- 能否画出四种合法图并解释唯一 final？
- 能否解释 `repr=False` 与 `asdict()` 的差异？
- 能否说明为什么 unknown Agent 不 fallback Core？
- 能否说出两项真实实现期发现，而不包装成生产事故？
- 能否准确说出 66 个专项测试和 1155 passed、42 subtests passed？
- 能否明确说默认 API 和生产多 Agent 执行仍为 NO？
- 能否说明 WP2 要接什么，而不声称已经实现？
