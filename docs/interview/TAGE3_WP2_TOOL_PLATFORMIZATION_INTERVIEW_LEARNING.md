# LocalAgent Stage 3 WP2 — Tool Platformization 工程面试学习材料

# 1. 一句话项目 / 工作包定义

WP2 Tool Platformization（工具平台化）的目标，是把 LocalAgent 原来“能调用几个工具”的功能，演进成一条有明确 Owner（所有者）、统一执行合同、权限与风险治理，并且能从默认 COORDINATED Runtime（协调运行时）真实入口端到端验证的 Tool Platform（工具平台）链路。

最终形成三个递进阶段：

```text
WP2-A
ToolRegistry / Descriptor
+ 4/4 Tool unified execution

        ↓

WP2-B
Permission
+ Risk
+ Approval requirement

        ↓

WP2-C
default /api/chat
→ COORDINATED
→ real Tool
→ final output
完整 E2E 证明
```

最终 Codex 明确确认：

```text
WP2-A completed = YES
WP2-B completed = YES
WP2-C completed = YES

No remaining WP2-scoped P0/P1 = YES
No remaining WP2-scoped TEST_GAP = YES

WP2 Tool Platformization completed = YES
```

但 Stage 3 仍未结束，Tool Registry / Governance（治理）合同仍为 `INTERNAL_RC`，最终冻结属于 Stage 3.5。

------

# 2. 为什么要做

WP2 不是为了“再加几个 Tool”。

真正的问题是原系统的 Tool 能力存在几个工程层面的缺口。

## 2.1 最初只是“工具能跑”

在 WP2-A 之前，Tool 的存在性、描述、绑定、Planner（规划器）展示以及部分调用行为集中在 `AgentRouter` 的可变结构中，而且不同 Tool 还存在不同执行路径。

因此系统缺少统一回答：

```text
系统当前到底有哪些 Tool？
Tool 的 canonical identity 是谁维护？
某个 Tool 绑定哪个 Adapter？
Tool 到底由谁真正执行？
```

WP2-A 首先把这些问题收口成 Registry（注册表）和统一 Execution（执行）路径。

## 2.2 “Tool 存在”不等于“Agent 可以执行”

WP2-A 完成后已经能够回答：

```text
Does this Tool exist?
```

但还不能回答：

```text
May this Agent use it?
How risky is this invocation?
Does this invocation require approval?
```

因此 WP2-B 又把 Tool 存在、权限、风险和审批要求拆开。Final Gate 最终重新确认：当前生产是 4 个 Tool、5 个 Agent、20 条显式授权关系，并采用完整 Risk 组合精确匹配。

## 2.3 分层测试都通过，不代表真实入口已经被证明

WP2-A 和 WP2-B 都完成之后，还长期保留一个明确 TEST_GAP（测试缺口）：

```text
full Coordinated Runtime entry
→ Tool execution
```

Scout 最终发现：

```text
Production chain exists end-to-end = YES
Production code change required = NO
```

真正缺的是：

> 没有一条单独的、离线、确定性的测试，同时跨越 HTTP/ASGI（异步服务器网关接口）入口、COORDINATED Runtime、动态 Planning、真实 Registry、真实 Governance、真实 ToolExecutionService、真实 Tool、OutputGate、Journal、Memory 和最终用户输出。

所以 WP2-C 是“补全可信证据”，而不是重新实现生产链。

------

# 3. 真实性与完成边界

## 3.1 已真实实现

WP2 aggregate 最终真实具备：

| 能力                                            | 状态              |
| ----------------------------------------------- | ----------------- |
| Canonical Tool Registry                         | 已实现            |
| Tool Descriptor / Registration                  | 已实现            |
| 4/4 production Tool adapter-backed              | 已实现            |
| ToolExecutionService 唯一执行 Owner             | 已实现            |
| Agent-scoped Tool Permission                    | 已实现            |
| Explicit Tool policies                          | 已实现            |
| Exact-combination Risk                          | 已实现            |
| Approval requirement gate                       | 已实现            |
| Default COORDINATED Tool full-chain integration | 已实现且 E2E 验证 |
| Success Tool result → final output              | 已真实验证        |
| Governance non-ALLOW → no execution             | 已真实验证        |

WP2-C 没有新增 production code；production chain 在此之前已经存在。WP2-C 新增的是 full-chain deterministic verification（确定性全链验证）。

------

## 3.2 已真实测试

最终 WP2-C Gate：

```text
P0 = 0
P1 = 0
P2 = 0 new WP2-C finding
TEST_GAP = 0
DOC_ONLY = 0
ENVIRONMENT_BLOCKED = 0
```

实际 Final Gate 回归：

```text
WP2-C E2E
2 tests × 3 consecutive runs PASS

WP2-A regression
133 passed

WP2-B regression
58 passed

Tool Runtime
297 passed

Stage2.5 regression
52 passed

WP1 regression
136 passed

Critical Runtime
115 passed

Full collection
1874

Full regression
1874 passed
0 failed
0 skipped
42 subtests passed
```

------

## 3.3 WP2-C 没有真实实现的新 production 功能

这一点必须记牢：

```text
WP2-C production diff = EMPTY
```

所以不能说：

> “我在 WP2-C 实现了 HTTP 到 Tool 的生产链路。”

正确说法：

> “生产链在 Scout 中已经确认存在；WP2-C 为它补充了一条可信的离线确定性全链 E2E，并通过 Final Gate 关闭此前 TEST_GAP。”

------

## 3.4 尚未实现

WP2 完成不代表以下能力完成：

```text
User IAM
RBAC / ABAC

Human approval workflow
Approval evidence
Durable approval resume

Filesystem/path authorization
Sandbox

MCP
Skills
A2A

Dynamic Tool plugin
Dynamic Registry
Dynamic policy
General Tool schema platform
```

------

## 3.5 合同状态

当前：

```text
ToolRegistry
ToolDescriptor
ToolRegistration

ToolPolicy
ToolPolicyCatalog
ToolGovernanceService
```

仍属于：

```text
INTERNAL_RC
```

不是 `PUBLIC_STABLE`。

Contract Freeze（合同冻结）仍属于 Stage 3.5。

------

# 4. 修改前架构与根因

这里需要分 WP2-A 和 WP2-C 两个时间点理解。

## 4.1 WP2-A 之前

原始 Tool 体系大致是：

```text
server lifespan
    ↓
AgentRouter
    ↓
mutable tools dictionary
    ↓
Tool planning / lookup
    ↓
legacy direct callable
OR
ToolAdapter
    ↓
ToolExecutionService
```

主要问题：

### 问题一：Registry 职责没有独立

Tool identity、description、binding 和 mutable registration 混在 AgentRouter。

### 问题二：存在双执行路径

部分 Tool 已经过 Adapter / ToolExecutionService，部分 Tool 仍存在 legacy direct callable。

### 问题三：重复注册可能覆盖

原 mutable dictionary 没有真正 fail-closed duplicate semantics（重复注册失败关闭语义）。

### 问题四：Agent Registry 与 Tool Registry 边界不清晰

但 Agent capability（能力）实际上并不等价于 Tool permission（工具权限）。

------

## 4.2 WP2-B 之前

WP2-A 已经解决：

```text
What Tools exist?
How are they bound?
Who executes them?
```

但还没有：

```text
Who may use them?
How risky is this invocation?
Does it require extra authorization?
```

------

## 4.3 WP2-C 之前

WP2-A/B 已经在 unit / integration / internal COORDINATED-facing 路径上被大量验证。

但是：

```text
HTTP
→ COORDINATED
→ Planning
→ AgentRouter
→ Registry
→ Governance
→ ToolExecutionService
→ Tool
→ OutputGate
→ final output
```

没有一个 single test（单一测试）全部跨越。

因此 WP2-C 的根因不是 production implementation gap，而是：

```text
TEST EVIDENCE GAP
```

。

------

# 5. 方案讨论与技术取舍

# 5.1 为什么 WP2-C 不继续改 production？

Scout 已确认：

```text
Production chain exists end-to-end = YES
P0 architecture blocker = 0
P1 architecture blocker = 0
Production code change required = NO
```

因此 Architecture Decision 直接冻结：

```text
WP2-C work type =
TEST + DOC + FINAL GATE

Production file allowlist =
EMPTY
```

这是一个重要工程原则：

> **没有 production defect，就不要为了“看起来做了很多工作”而制造 production diff。**

------

# 5.2 为什么不用真实 LLM 做 E2E？

真实 LLM 会引入：

```text
网络依赖
模型可用性
输出随机性
Planner 路由波动
Tool call 文本变化
```

这样 E2E 会变 flaky（不稳定）。

但如果把 AgentRouter、Planner、ToolExecutionService 都 mock 掉，又会变成 fake E2E（伪端到端测试）。

最终取舍：

```text
只 mock external model output

Runtime / Planning parser /
Registry / Governance /
Execution / Tool / Output
全部真实
```

------

# 5.3 为什么 FakeModel 不能按第 1/2/3 次调用返回结果？

因为：

```text
call #1 = planning
call #2 = tool
call #3 = final
```

这种测试对实现细节高度耦合。

一旦合法新增一个 Model invocation（模型调用），整个测试可能误判。

所以 FakeModel 根据 system message（系统消息）语义区分：

```text
PLANNING
TOOL_PLANNER
FINAL_ANSWER
```

任何未知调用直接：

```text
AssertionError
```

Final Gate 确认成功场景调用计数为：

```text
1 / 1 / 1
```

治理拒绝场景：

```text
1 / 1 / 0
```

。

------

# 5.4 为什么 success E2E 选择 `list_files`？

Architecture 比较了四个当前 Tool。

最终选 `list_files`，因为：

```text
real production implementation
单字符串参数
可以用 tmp_path
结果中可以放唯一 marker
无外部服务
read-only
执行速度稳定
```

而：

- `get_system_status` 的 CPU / Memory 数值不够稳定；
- `analyze_excel` 需要额外 Excel fixture 和依赖；
- `complex_workflow_simulator` 更适合做 Risk / Approval 场景。

------

# 5.5 为什么治理场景选 NON_IDEMPOTENT_SIMULATION？

因为它在当前 production contract 中天然对应：

```text
{}
+
LOCAL_STATE_MUTATION
+
NON_IDEMPOTENT

→ HIGH
→ APPROVAL_REQUIRED
```

这不需要人为篡改 production permission policy，就能验证 WP2-B Governance 真正集成到了 HTTP → COORDINATED 全链。

------

# 6. 最终架构

WP2 aggregate 完成后的核心 Tool 链可以理解为：

```text
POST /api/chat
      ↓
ChatService
      ↓
default COORDINATED
      ↓
PlanResolver
      ↓
StrictPlanningDecisionParser
      ↓
PlanCompiler
      ↓
RunCoordinator
      ↓
Scheduler / ParallelExecutor
      ↓
MultiAgentDriver
      ↓
AgentRouterSingleAgentAdapter
      ↓
AgentRouter
      ↓
ToolRegistry
      │
      ├── canonical identity
      ├── descriptor
      └── adapter binding
      ↓
ToolGovernanceService
      │
      ├── Permission
      ↓
build ToolInvocation
      ↓
adapter.spec_for()
      ↓
ToolGovernanceService
      │
      ├── Risk
      └── Approval requirement
      ↓
ALLOW only
      ↓
ToolExecutionService
      ↓
ToolAdapter
      ↓
production Tool
      ↓
Tool result
      ↓
Agent result
      ↓
StepResultStore / Committer
      ↓
OutputGate
      ↓
Journal / Memory / Stream
      ↓
User-visible TEXT
```

WP2-C Final Gate 已验证这条 success chain 的关键节点全部使用真实 production 对象。

------

# 7. 核心状态机和时序

## 7.1 Success path

真实成功场景：

```text
HTTP request
  ↓
RUN_STARTED
  ↓
PLANNING_STARTED
  ↓
PLAN_CREATED
  ↓
STEP_STARTED
  ↓
Tool Permission ALLOW
  ↓
Tool Risk MEDIUM
  ↓
Governance ALLOW
  ↓
TOOL_STARTED
  ↓
real list_files
  ↓
TOOL_COMPLETED
  ↓
Tool Observation
  ↓
FINAL_ANSWER model
  ↓
OUTPUT_DELTA
  ↓
STEP_COMPLETED
  ↓
RUN_COMPLETED
```

最终关键 partial order（部分顺序）真实验证：

```text
RUN_STARTED
<
PLANNING_STARTED
<
PLAN_CREATED
<
STEP_STARTED
STEP_STARTED
<
TOOL_STARTED
<
TOOL_COMPLETED
TOOL_COMPLETED
<
OUTPUT_DELTA
<
STEP_COMPLETED
<
RUN_COMPLETED
```

------

## 7.2 Governance non-ALLOW path

治理场景：

```text
Permission ALLOW
      ↓
build invocation
      ↓
spec_for()
      ↓
Risk HIGH
      ↓
APPROVAL_REQUIRED
      ↓
STOP BEFORE EXECUTION
      ↓
fixed safe denial
      ↓
OutputGate
      ↓
RUN COMPLETED
```

关键不变量：

```text
TOOL_STARTED = 0
TOOL_COMPLETED = 0

state mutation = 0

FINAL_ANSWER model = 0
```

但：

```text
STEP = SUCCEEDED / DELIVERED
RUN  = SUCCEEDED / DELIVERED
```

因为它当前被定义为一个“安全业务拒绝结果”，而不是 Runtime failure。

------

# 8. 数据、权限和 Owner 边界

这是 WP2 最重要的 Owner Map（所有权映射）。

| Concern                                    | Owner                            |
| ------------------------------------------ | -------------------------------- |
| Tool identity / inventory                  | `ToolRegistry`                   |
| Tool description                           | `ToolDescriptor`                 |
| Descriptor + Adapter binding               | `ToolRegistration`               |
| Tool resolution                            | `AgentRouter + ToolRegistry`     |
| Static Tool policy                         | `ToolPolicyCatalog`              |
| Permission decision                        | `ToolGovernanceService`          |
| Effective Risk                             | `ToolGovernanceService`          |
| Approval requirement                       | `ToolGovernanceService`          |
| Invocation side-effect / idempotency truth | `ToolExecutionSpec / spec_for()` |
| Actual Tool execution                      | `ToolExecutionService`           |
| Agent identity / capability                | `AgentRegistry`                  |
| Final publication                          | `OutputGate`                     |
| Runtime facts                              | Event / Journal                  |
| Final delivered memory                     | Final Memory Writer              |

------

## 必须熟练的“不等于”

```text
Agent Registry
!= Tool Registry
Tool exists
!= Agent may use Tool
Agent capability
!= Tool permission
Tool permission
!= filesystem path authorization
Side effect
!= Risk
Idempotency
!= Permission
APPROVAL_REQUIRED
!= approval capability exists
```

------

# 9. 兼容策略

WP2 的兼容策略不是一次重写 Tool 系统，而是分阶段收口。

## WP2-A

旧：

```text
legacy direct callable
```

迁移为：

```text
LegacyStringToolAdapter
→ ToolExecutionService
```

Tool business function 本身不重写。

最终四个 production Tool 全部 adapter-backed。

------

## WP2-B

为了不在没有业务证据时改变历史行为：

当前 5 个 production Agent：

```text
core_router
data_analyst
code_expert
knowledge_expert
synthesis_agent
```

对 4 个 production Tool 都建立 explicit ALLOW（显式允许）：

```text
5 × 4 = 20
```

注意：

```text
explicit allow
!= implicit known-agent allow
```

未来可以收紧，但 WP2-B 第一版先建立治理机制，不凭主观猜测改变原功能。

------

## WP2-C

不改变生产行为：

```text
production diff = EMPTY
```

只增加：

```text
tests/test_stage3_wp2_tool_e2e.py
```

和两份文档更新。

这是兼容风险最低的收口方式。

------

# 10. Bad Cases

# Bad Case 1：名字叫 E2E，实际 Mock 了核心系统

真实性：

```text
HYPOTHETICAL_BAD_CASE
regression-covered / Final Gate reviewed
```

错误测试：

```text
HTTP
→ Fake AgentRouter
→ Fake ToolExecutionService
→ PASS
```

这无法证明真实 Tool 平台。

WP2-C 明确禁止 Mock：

```text
AgentRouter
Registry
Governance
ToolExecutionService
ToolAdapter
production Tool
OutputGate
Journal
```

------

# Bad Case 2：Static Plan 冒充动态 Runtime E2E

如果直接：

```text
construct Plan
→ execute
```

就绕过：

```text
PlanResolver
StrictPlanningDecisionParser
PlanCompiler
```

所以主测试禁止：

```text
create_static_run_scope
trusted_plan
direct Plan construction
```

Final Gate 真实确认不存在这些 shortcut。

------

# Bad Case 3：Tool 执行了，但最终答案根本没用 Tool result

这是最常见的“伪 Tool E2E”。

错误：

```text
Tool called = YES
final model hardcodes correct answer
```

于是即使 Tool result 被丢了，测试仍 PASS。

------

## WP2-C 的解决方式

真实目录中放：

```text
marker_file_7f3a.txt
```

query 本身**不包含 filename**。

FakeModel 只有在真实：

```text
工具观察结果：
...
marker_file_7f3a.txt
```

出现后，才能返回：

```text
找到 marker_file_7f3a.txt
```

否则：

```text
AssertionError
```

Codex 又专门做 adversarial probe：

```text
observation label存在
marker不存在
```

实际：

```text
MARKER_ORACLE_FAIL_CLOSED=True
```

这证明 final marker 不能凭空生成。

------

# Bad Case 4：Governance 已拒绝但 Tool 仍开始执行

治理场景必须同时证明：

```text
TOOL_STARTED = 0
TOOL_COMPLETED = 0

Journal TOOL_STARTED = 0
Journal TOOL_COMPLETED = 0

state mutation = 0
FINAL_ANSWER = 0
```

只验证其中一个证据还不够。

------

# Bad Case 5：把“默认 COORDINATED”说得过头

WP2-C 新 E2E 为隔离测试环境，在 settings snapshot 中显式保持：

```text
chat_runtime_mode = COORDINATED
```

所以新测试**本身**不能单独证明：

```text
Settings default parsing = COORDINATED
```

最终证据是组合的：

```text
Settings source + existing default regression
→ default = COORDINATED

WP2-C full E2E
→ COORDINATED Tool full chain works
```

Final Gate 特别复核了这个真实性边界。

------

# Bad Case 6：把 OUTPUT_DELTA 当成 CONTROL

这是本阶段真实发生的 test implementation finding（测试实现发现）。

Phase 3 首轮 targeted test 曾出现：

```text
2 failed
```

原因是测试错误假设：

```text
OUTPUT_DELTA
→ [[ORCH]] CONTROL
```

但真实合同是：

```text
Journal:
OUTPUT_DELTA RuntimeEvent

Streaming:
ChatStreamCompatibilityAdapter
→ user-visible TEXT
```

修正 test oracle 后测试通过。

真实性：

```text
TEST_IMPLEMENTATION_FINDING
FIXED
regression-covered
```

不是 production Runtime defect。

------

# Bad Case 7：为了 E2E 新增 production test hook

例如：

```text
?force_tool=list_files
?force_agent=core_router
```

会污染 production contract。

最终 WP2-C：

```text
production diff = EMPTY
```

没有新增这类接口。

------

# Bad Case 8：审批拒绝后又调用 final model 改写

当前 WP2-B 合同要求 Governance safe denial 直接作为结果。

所以：

```text
APPROVAL_REQUIRED
→ FINAL_ANSWER count = 0
```

测试里如果 final-answer branch 被调用会直接失败。

------

# 11. 测试与 Final Gate

## 11.1 WP2-C 两条核心 E2E

### Success

```text
/api/chat
→ COORDINATED
→ dynamic planning
→ core_router
→ list_files
→ Permission ALLOW
→ Risk MEDIUM
→ Governance ALLOW
→ Tool execution
→ marker observation
→ final TEXT
```

### Governance

```text
/api/chat
→ COORDINATED
→ dynamic planning
→ core_router
→ complex_workflow_simulator
→ Permission ALLOW
→ Risk HIGH
→ APPROVAL_REQUIRED
→ no Tool execution
→ fixed safe denial
```

------

## 11.2 成功场景证据不是单点

不是只断言：

```text
HTTP 200
```

而是组合：

```text
real Tool file exists

TOOL_STARTED = 1

TOOL_COMPLETED = 1

Tool observation contains marker

FakeModel sees marker

final TEXT contains marker

Journal OUTPUT_DELTA = 1

Final Memory contains marker

RUN_COMPLETED = SUCCEEDED / DELIVERED
```

------

## 11.3 治理场景同样是组合证据

```text
Risk = HIGH

Governance = APPROVAL_REQUIRED

TOOL events = 0

Journal Tool events = 0

state mutation = 0

final model = 0

safe TEXT exact match

RUN = SUCCEEDED / DELIVERED
```

------

## 11.4 Final Gate

最终：

```text
1874 passed
0 failed
0 skipped
42 subtests passed
```

且：

```text
production diff = EMPTY
existing test diff = EMPTY
packaging diff = EMPTY
```

因此正式：

```text
TEST_GAP-01 = CLOSED
Remaining WP2-scoped TEST_GAP = 0
```

------

# 12. Known Limitations

WP2 aggregate 完成后仍必须保留：

## Approval

```text
Approval evidence absent
Human approval workflow absent
Durable approval pause/resume absent
```

## Security

```text
Filesystem/path authorization absent
Sandbox absent
```

## Observability

```text
Dedicated governance RuntimeEvent absent
Governance Journal fact absent
```

## Contract

```text
Tool contracts = INTERNAL_RC
```

## Compatibility

```text
deny-all compatibility seam
```

## Execution

```text
spec_for double evaluation
```

现有 Adapter 已验证 pure / deterministic（纯 / 确定性）。

## Runtime

```text
Windows native
single server process
Recovery validation-only
historical planning executor starvation accepted P2
```

------

# 13. 这轮体现的工程能力

## 13.1 能区分“实现缺口”和“证据缺口”

Scout 没有看到 production bug，因此没有强行改 production。

这是测试和架构成熟度的重要体现：

```text
No code change
```

有时反而是最正确的工程决策。

------

## 13.2 能设计可信 E2E，而不是追求覆盖率数字

核心不是：

```text
coverage ↑
tests ↑
```

而是：

> 如果核心环节真的坏了，这个测试会不会失败？

marker adversarial probe 正是在验证 test oracle 本身。

------

## 13.3 Mock Boundary 设计

好的 E2E 并不是“完全不 Mock”。

本项目保留真实：

```text
Runtime
Planning parser
Registry
Governance
Execution
Tool
Output
Memory
Journal
```

只 Mock：

```text
external nondeterministic model output
```

这样实现：

```text
determinism
+
offline
+
production-chain fidelity
```

之间的平衡。

------

## 13.4 Owner 思维

Tool Platformization 最终不是一个大 `ToolManager`。

而是多个独立 Owner：

```text
Registry
Policy Catalog
Governance
Execution Service
OutputGate
```

职责单一。

------

## 13.5 Fail-closed 思维

WP2 多个地方都采用：

```text
duplicate registration
unknown principal
missing policy
unknown risk combination
approval required but no evidence
unknown FakeModel message shape
```

→ 不继续猜测，而是拒绝或让测试失败。

------

# 14. 30 秒面试表达

我在 LocalAgent Stage 3 做了 Tool Platformization，分成三个子阶段。

WP2-A 先把原来 AgentRouter 里的可变 Tool 注册和双执行路径收口成独立 ToolRegistry，4 个生产 Tool 全部 Adapter 化，并统一由 ToolExecutionService 执行。

WP2-B 再加入 Agent 级 Permission、Risk 和 Approval requirement，真实执行 agent_id 作为 principal，Risk 采用完整组合精确匹配，未定义组合 fail closed。

WP2-C 没有再改 production code，因为 Scout 已确认生产链本身完整。最后补了一条真正从 `/api/chat`、默认 COORDINATED、动态 Planning 到真实 Tool、Governance、OutputGate、Journal、Memory 的离线确定性 E2E。成功场景用 marker 证明 Tool result 真的进入 final output，治理场景证明 APPROVAL_REQUIRED 下 Tool events、state mutation 和 final model 都是 0。

最终全量 1874 passed，WP2 scoped P0/P1 和 TEST_GAP 都归零。

------

# 15. 2 分钟面试表达

我这个 Tool 平台化不是单纯给 Agent 加几个函数。

一开始 Tool identity、description、binding 和注册都混在 AgentRouter 的 mutable dict 里，而且 list_files、analyze_excel 和另外两个 Tool 的执行路径也不统一。

所以 WP2-A 我先建立 ToolRegistry、ToolDescriptor 和 ToolRegistration，把 Tool identity、描述和 Adapter binding独立出来；四个 production Tool 全部走 Adapter，再统一由 ToolExecutionService 执行。AgentRegistry 和 ToolRegistry也明确分离，Agent capability不再被理解成 Tool permission。

WP2-B解决治理问题。我用 actual executing agent_id作为 principal，引入 ToolPolicyCatalog和 ToolGovernanceService。Gate分两层：先做 static permission，通过后才 build invocation和spec_for，再结合 static risk facts、side-effect和idempotency做 invocation risk。这里还遇到过一个真实 P1：第一版 Risk把静态和动态等级直接取max，会自动分类从未批准过的组合。后来改成完整组合 exact allowlist，未知组合统一 TOOL_RISK_UNCLASSIFIED fail closed。

WP2-C最后解决测试证据问题。Scout发现 production chain其实已经完整，所以 production allowlist直接设为空。我们只 Mock 外部 Model output，其它包括 ASGI入口、COORDINATED Runtime、动态 Planner、AgentRouter、Registry、Governance、ToolExecutionService、真实 list_files、OutputGate、Journal和Memory全部真实运行。

Success E2E用一个随机 marker文件做 oracle：只有真实 Tool observation里看到 marker，FakeModel才能生成最终答案。Codex还专门构造无marker observation做对抗测试，确认它会fail closed。另一个 E2E验证NON_IDEMPOTENT模式得到HIGH和APPROVAL_REQUIRED后，TOOL_STARTED、Journal Tool event、state mutation和final model调用全部为0。

最后1874个测试通过，WP2 Tool Platformization正式完成。

------

# 16. 深入版本：什么才算“可信 E2E”

一个测试名字叫：

```text
test_tool_e2e
```

并不代表它是真的 E2E。

真正的问题要问：

> 它如果坏掉中间某个关键 production seam，会不会真的红？

WP2-C 的可信性来自四层。

## 第一层：真实入口

```text
server.app
POST /api/chat
```

而不是直接调 service method。

------

## 第二层：真实运行链

```text
ChatService
COORDINATED
PlanResolver
PlanCompiler
RunCoordinator
AgentRouter
Registry
Governance
Execution
Tool
```

都是真实 production component。

------

## 第三层：Mock 只切断不确定外部依赖

唯一替代：

```text
external LLM response
```

但是：

```text
Planner parser
Tool planner parser
final observation consumption
```

仍真实。

------

## 第四层：Oracle 必须依赖被测系统

如果 Tool result 没到 final answer：

```text
test MUST fail
```

marker 设计的价值就在这里。

可以抽象成：

```text
Test oracle
必须和 production fact 建立因果关系
```

而不仅是：

```text
expected value == hardcoded fake value
```

------

# 17. 高频追问与参考答案

## Q1：为什么 WP2-C 没改生产代码也算一个工程阶段？

因为 Scout 已确认 production chain 完整，缺的是可信的全链测试证据。

如果明知没有 production defect还硬改代码，反而增加 regression risk（回归风险）。

WP2-C 的交付物是：

```text
full deterministic E2E
+
formal evidence
+
Final Gate
```

并正式关闭 TEST_GAP。

------

## Q2：Integration Test 和 E2E Test 有什么区别？

在这个项目里：

WP2-A/B 的 integration test 可以从：

```text
AgentRouter
→ Governance
→ ToolExecutionService
```

开始。

但 WP2-C 的 full E2E 要从：

```text
HTTP / ASGI
```

真正跨过默认 Runtime、Planning、Tool、Output 到 final response。

------

## Q3：用了 FakeModel 还能叫 E2E 吗？

可以，因为 Mock 的是外部非确定性依赖。

被验证的 production system chain：

```text
HTTP
Runtime
Planner parser
Router
Registry
Governance
Execution
Tool
Output
Persistence
```

都没有被替换。

更准确可以说：

> offline deterministic application E2E。

------

## Q4：为什么不直接用真实 LLM？

真实 LLM 会让 Gate 依赖：

```text
network
provider
model version
sampling
prompt variance
```

这样无法作为稳定 regression gate（回归门禁）。

------

## Q5：为什么 FakeModel不能只按调用次数返回答案？

因为未来新增合法模型调用后测试会错误映射。

按 semantic stage（语义阶段）识别更接近 contract。

------

## Q6：怎么证明 list_files 真的执行了？

组合证据：

```text
real filesystem fixture
TOOL_STARTED = 1
TOOL_COMPLETED = 1
real observation contains marker
```

而不是 mock function call count。

------

## Q7：怎么证明 final answer真的使用了 Tool result？

Query 不含 filename。

FakeModel final branch只有看到真实 Tool observation中的：

```text
marker_file_7f3a.txt
```

才输出 final marker。

Codex adversarial probe把 marker去掉后实际触发 AssertionError。

------

## Q8：为什么 governance denial 的 Run还是 SUCCEEDED？

当前合同把它定义成：

```text
安全完成交付的业务拒绝结果
```

而不是 Runtime exception。

所以：

```text
Tool未执行
但用户得到了确定性的合法结果
```

最终仍：

```text
SUCCEEDED / DELIVERED
```

------

## Q9：那 APPROVAL_REQUIRED 为什么不是 PENDING？

因为当前根本没有：

```text
approval evidence
pause
resume
durable continuation
```

所以不能伪装成异步等待状态。

------

## Q10：为什么只测试 Permission ALLOW，没有 HTTP Permission DENY？

当前 production policy 是 5×4 全 explicit ALLOW。

为了做测试而修改 production permission 会改变 Scope。

Permission deny 已由 WP2-B regression覆盖。

------

## Q11：为什么还检查 Journal 和 Memory？

因为只看到 HTTP response 不够证明 Runtime 内部事实一致。

Journal可以证明：

```text
Tool really started/completed
OUTPUT_DELTA really published
```

Memory可以证明：

```text
最终 delivered answer 被正确持久化
内部 Tool observation 没污染 final memory
```

------

## Q12：为什么 OUTPUT_DELTA 不在 CONTROL stream？

当前 compatibility contract 是：

```text
Runtime Event OUTPUT_DELTA
→ compatibility adapter
→ user-visible TEXT
```

Journal仍保留 OUTPUT_DELTA event。

这也是 Phase 3 首轮测试 oracle真实踩过的坑。

------

## Q13：WP2-A/B/C分别解决什么？

一句话：

```text
WP2-A：Tool是什么、怎么统一执行

WP2-B：谁能执行、风险多高、是否需要审批

WP2-C：证明真实默认入口下这一整套真的连起来
```

------

## Q14：ToolExecutionService为什么不负责Permission？

因为执行机制和Policy Authority是不同职责。

当前：

```text
ToolGovernanceService
→ may it execute?

ToolExecutionService
→ how to execute it?
```

------

## Q15：Agent capability为什么不能直接当Tool permission？

Capability表示：

```text
这个Agent擅长什么
```

Permission表示：

```text
安全策略允许它做什么
```

语义完全不同。

------

## Q16：Risk为什么不能简单看side_effect？

因为：

```text
list_files
```

是：

```text
NONE + READ_ONLY
```

但它具有 arbitrary local filesystem read（任意本地文件系统读取）风险，所以仍被归为 MEDIUM。

------

## Q17：Risk为什么最终用exact full combination？

因为第一次 generic max 会给未知完整组合自动推导风险。

安全合同要求：

```text
approved combination
→ classify

unapproved combination
→ fail closed
```

------

## Q18：当前审批系统做到了什么？

只做到：

```text
approval requirement decision
+
pre-execution safe denial
```

没有真正 approval grant。

------

## Q19：WP2完成是不是Tool Security完成？

不是。

路径授权、Sandbox、secret isolation都属于后续 WP3。

------

## Q20：为什么Tool contracts还不freeze？

当前仍是：

```text
INTERNAL_RC
```

Stage3 后面还有 Security、Observability、Production Gate。

Aggregate Contract Freeze属于Stage3.5。

------

# 18. 容易夸大或答错的地方

## 错误 1

“WP2-C实现了完整 production Tool chain。”

错误。

正确：

> production chain此前已经存在，WP2-C补充全链 E2E证明。

------

## 错误 2

“整个 E2E 没有任何 Mock。”

错误。

Mock了 external Model output。

------

## 错误 3

“我们已经支持人工审批。”

错误。

当前只是：

```text
APPROVAL_REQUIRED
→ safe deny
```

------

## 错误 4

“Tool Permission已经控制文件路径。”

错误。

```text
Permission
!= path authorization
```

------

## 错误 5

“WP2-C是多 Agent E2E。”

错误。

冻结的是：

```text
single-agent COORDINATED
Synthesis = NO
```

------

## 错误 6

“WP2-C新测试自己证明默认 Runtime就是COORDINATED。”

不完整。

正确是：

```text
Settings source/default regression
+
WP2-C full chain E2E
```

组合证明。

------

## 错误 7

“OUTPUT_DELTA是[[ORCH]]事件。”

错误。

Runtime中是 Event，但 compatibility stream 中转换为 user TEXT。

------

## 错误 8

“所有 Tool 调用结果都会写入Memory。”

错误。

最终 Memory只保存 delivered final exchange，不单独保存内部 Tool observation。

------

## 错误 9

“APPROVAL_REQUIRED代表Run失败。”

当前合同不是这样。

是：

```text
safe business result
SUCCEEDED / DELIVERED
```

------

## 错误 10

“WP2完成就代表Stage3完成。”

错误。

Stage3仍有：

```text
WP3 Security
WP4 Observability
WP5 Production Readiness Gate
```

。

------

# 19. P0 / P1 / P2 复习重点

# P0：必须熟练

## 1. WP2 三阶段职责

```text
WP2-A Registry / unified execution
WP2-B Governance
WP2-C full-chain verification
```

## 2. Tool Owner Map

尤其：

```text
Registry
Governance
ExecutionService
```

三者不能混。

## 3. Full Tool success chain

能够从 `/api/chat` 口述到 final output。

## 4. FakeModel Mock boundary

什么真、什么假。

## 5. Marker Oracle

为什么它能证明 result propagation。

## 6. Governance non-ALLOW invariants

```text
events=0
mutation=0
final model=0
```

## 7. Permission / Risk / Approval三者区别。

## 8. WP2实际完成边界与 Security非目标。

------

# P1：建议熟练

## 9. 为什么WP2-C production diff为空。

## 10. ASGI真实入口和lifespan为什么重要。

## 11. Dynamic planning与static plan shortcut区别。

## 12. OUTPUT_DELTA / TEXT关系。

## 13. Journal为什么是第二证据面。

## 14. Final Memory为什么不能保存Tool observation。

## 15. `spec_for` double evaluation当前边界。

## 16. 默认COORDINATED组合证据的真实性表述。

------

# P2：了解即可

## 17. Future Human approval需要哪些额外系统。

## 18. Future path authorization / sandbox。

## 19. Dedicated Governance Event。

## 20. Stage3.5 Contract Freeze。

------

# 20. 最终速查表

| 项目                               | 当前真实状态                    |
| ---------------------------------- | ------------------------------- |
| Work Package                       | WP2 Tool Platformization        |
| WP2-A                              | PASS / completed                |
| WP2-B                              | PASS / completed                |
| WP2-C                              | PASS / completed                |
| WP2 aggregate                      | completed                       |
| WP2-C P0                           | 0                               |
| WP2-C P1                           | 0                               |
| WP2-C new P2                       | 0                               |
| WP2 scoped TEST_GAP                | 0                               |
| Production Tools                   | 4                               |
| Production Agents                  | 5                               |
| Explicit authorization             | 20                              |
| Tool inventory Owner               | ToolRegistry                    |
| Policy Owner                       | ToolPolicyCatalog               |
| Permission/Risk/Approval Authority | ToolGovernanceService           |
| Execution Owner                    | ToolExecutionService            |
| Default Runtime                    | COORDINATED                     |
| Main E2E entry                     | real ASGI `POST /api/chat`      |
| Main success Agent                 | core_router                     |
| Main success Tool                  | list_files                      |
| Success Risk                       | MEDIUM                          |
| Success governance                 | ALLOW                           |
| Result oracle                      | `marker_file_7f3a.txt`          |
| Success TOOL_STARTED               | 1                               |
| Success TOOL_COMPLETED             | 1                               |
| Final TEXT                         | 1                               |
| Journal OUTPUT_DELTA               | 1                               |
| Final memory                       | user + assistant final exchange |
| Governance Tool                    | complex_workflow_simulator      |
| Governance mode                    | NON_IDEMPOTENT_SIMULATION       |
| Governance Risk                    | HIGH                            |
| Governance outcome                 | APPROVAL_REQUIRED               |
| Governance TOOL_STARTED            | 0                               |
| Governance TOOL_COMPLETED          | 0                               |
| Governance mutation                | 0                               |
| Governance final model             | 0                               |
| Human approval                     | NOT_IMPLEMENTED                 |
| Path authorization                 | NOT_IMPLEMENTED                 |
| Sandbox                            | NOT_IMPLEMENTED                 |
| Contract classification            | INTERNAL_RC                     |
| WP2-C production diff              | EMPTY                           |
| Full regression                    | 1874 passed                     |
| Subtests                           | 42 passed                       |
| Stage3 completed                   | NO                              |

## 面试最值得记住的三句话

### 第一句

> **Tool Platformization不是“多注册几个函数”，而是把 Tool existence、binding、permission、risk、approval requirement 和 actual execution拆成有明确Owner的独立合同。**

### 第二句

> **WP2-C最重要的工程判断是Scout发现生产链已经完整，因此没有为了制造产出而改production code，而是用只Mock外部Model的offline deterministic E2E补齐真实证据缺口。**

### 第三句

> **E2E可信的关键不是测试从HTTP开始，而是oracle真的依赖被测系统：我们的final marker只有经过真实list_files → Tool observation → final model → OutputGate才能出现，Codex还用移除marker的对抗probe证明这个oracle会fail closed。**