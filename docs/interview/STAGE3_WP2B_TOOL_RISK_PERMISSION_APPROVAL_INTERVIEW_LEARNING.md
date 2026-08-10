# LocalAgent Stage 3 WP2-B — Tool Risk / Permission / Approval 工程面试学习材料

# 1. 一句话项目 / 工作包定义

WP2-B 的目标，是在 WP2-A 已经建立的 Tool Registry（工具注册表）和统一 Tool Execution（工具执行）链路之上，增加一层**不可绕过、fail-closed（失败关闭）的 Tool Governance（工具治理）边界**，明确回答三个不同的问题：

```text
这个 Tool 存不存在？
→ ToolRegistry

当前实际执行 Agent 能不能使用它？
→ Permission

这次具体 invocation 风险是多少，是否要求审批？
→ Risk / Approval
```

最终形成：

```text
ToolRegistry
        ↓
ToolGovernanceService
        ├── Permission Gate
        ↓
build ToolInvocation
        ↓
ToolExecutionSpec / spec_for()
        ↓
ToolGovernanceService
        ├── Risk / Approval Gate
        ↓
ALLOW only
        ↓
ToolExecutionService
```

最终 Re-Gate（重新门禁）结果：

```text
WP2-B Final Re-Gate = PASS

P0 = 0
P1 = 0
P2 = 0
TEST_GAP = 1
DOC_ONLY = 0
ENVIRONMENT_BLOCKED = 0

WP2-B completed = YES
Allowed to continue WP2-C = YES
```

唯一剩余 TEST_GAP 是完整：

```text
HTTP / CoordinatedRuntime
→ Tool execution E2E
```

已冻结到 WP2-C，不是 WP2-B blocker。

------

# 2. 为什么要做

WP2-A 已经解决：

```text
What Tools exist?
How does a Tool execute?
```

也就是：

```text
ToolRegistry
→ inventory / binding

ToolExecutionService
→ actual execution
```

但 WP2-B Scout Audit（侦察审计）发现，进入本阶段时：

```text
Current Tool permission enforcement = NO
Current Tool risk taxonomy = NOT_IMPLEMENTED
Current Tool approval gate = NOT_IMPLEMENTED
```

也就是说，当时系统虽然已经知道：

```text
“这个 Tool 存在”
```

并且能可靠执行，但还没有回答：

```text
“这个 Agent 有资格用吗？”
“这次调用有多大风险？”
“风险达到什么程度需要额外审批？”
```

更重要的是，Tool Registry 中有 Tool：

```text
registry.contains(tool) == true
```

只能说明 Tool 存在。

它绝不等价于：

```text
agent may invoke tool
```

这就是 WP2-B 最核心的问题。

------

# 3. 真实性与完成边界

## 3.1 已真实实现

当前已经真实实现：

| 能力                                   | 状态   |
| -------------------------------------- | ------ |
| `ToolPolicy`                           | 已实现 |
| `ToolPolicyCatalog`                    | 已实现 |
| `ToolGovernanceContext`                | 已实现 |
| `ToolGovernanceDecision`               | 已实现 |
| `ToolGovernanceService`                | 已实现 |
| Agent-scoped Permission                | 已实现 |
| 4 Tool × 5 Agent 显式授权              | 已实现 |
| Unknown principal fail-closed          | 已实现 |
| Missing policy fail-closed             | 已实现 |
| Static Permission Gate                 | 已实现 |
| Invocation Risk Gate                   | 已实现 |
| Approval requirement Gate              | 已实现 |
| Tool Risk Level LOW/MEDIUM/HIGH        | 已实现 |
| Exact full-combination risk mapping    | 已实现 |
| Undefined Risk combination fail-closed | 已实现 |
| Startup policy validation/freeze       | 已实现 |
| LEGACY enforcement                     | 已实现 |
| COORDINATED-facing enforcement         | 已实现 |
| Safe denial                            | 已实现 |
| No Tool execution/event on denial      | 已实现 |

最终 Owner 和核心合同均通过 Codex Re-Gate。

------

## 3.2 已真实测试

Final Re-Gate 实际重新执行：

```text
Governance targeted     56 passed
WP2-B targeted          127 passed
WP2-A regression        74 passed
Tool Runtime            295 passed
WP1 regression          136 passed
Critical Runtime        113 passed

Full collection         1872
Full regression         1872 passed
Skipped                 0
Subtests                42 passed
```

以及：

```text
compileall       PASS
uv lock --check  PASS
git diff --check PASS
packaging diff   EMPTY
```

------

## 3.3 只完成设计但没有实现的能力

以下没有在 WP2-B 实现：

```text
Approval evidence
approval token
ApprovalStore
approved=True request field

Human Review UI
approve / reject interaction

pause
wait
resume

durable approval workflow

user IAM
tenant
organization
enterprise RBAC / ABAC
```

Architecture Decision 明确选择的是 synchronous require-or-deny（同步要求或拒绝），而不是完整 Human-in-the-Loop（人工在环）系统。

------

## 3.4 Known Limitations（已知限制）

当前仍存在：

```text
Approval evidence absent
Human approval workflow absent
Durable approval pause/resume absent

Filesystem/path authorization absent
Sandbox absent

Dedicated governance RuntimeEvent absent
Governance Journal fact absent

Governance contracts = INTERNAL_RC

Full Coordinated Tool E2E
→ deferred WP2-C

AgentRouter deny-all compatibility seam

spec_for() double evaluation
→ current adapters 已验证 pure / deterministic
```

------

## 3.5 假设构造 Bad Case

以下不是生产事故，而是：

```text
HYPOTHETICAL_BAD_CASE
regression-covered
```

包括：

```text
wrong principal
missing policy
unknown principal
approval required
model self-approval
dynamic risk
deny emits TOOL_STARTED
LEGACY / COORDINATED governance bypass
```

------

## 3.6 真实发现的问题

本阶段唯一非常值得面试讲的真实实现问题：

```text
P1-01
generic risk algebra
```

分类：

```text
SOURCE_AUDIT_FINDING
+
DIRECT_PROBE_FINDING
```

不是用户生产事故。

------

# 4. 修改前架构与根因

进入 WP2-B 前：

```text
Planner
   ↓
ToolRegistry.require(tool_name)
   ↓
adapter.build_invocation(...)
   ↓
ToolExecutionService.execute_sync(...)
   ↓
adapter.spec_for(invocation)
   ↓
adapter.invoke_once(...)
```

链路已经比较规范，但中间没有：

```text
Permission Authority
Risk Authority
Approval Authority
```

所以只要：

```text
Tool exists
```

Planner 又选中了它，就可以进入 Tool execution。

------

## 根因 1：Existence 与 Authorization 没分开

原系统回答：

```text
Tool exists?
```

但没有回答：

```text
Who may execute it?
```

这是 Registry 系统最容易犯的一个设计错误：

```text
registered
≠
authorized
```

------

## 根因 2：没有稳定的 Principal 定义

Permission 首先需要知道：

> 谁正在调用？

Scout 发现系统存在很多身份：

```text
entry_agent_id
preferred_agent
agent_id
run_id
step_id
session_id
```

如果 principal 定义错，整个权限系统都会错。

------

## 根因 3：Risk 不是纯 Tool 静态属性

例如：

```text
complex_workflow_simulator
```

同一个 Tool：

```text
DRY_RUN
IDEMPOTENT_COMMIT
NON_IDEMPOTENT_SIMULATION
```

执行语义完全不同。

因此：

```text
Tool risk = HIGH
```

这样的静态标签不足以描述真实 invocation。

------

# 5. 方案讨论与技术取舍

# 5.1 Principal 选谁？

候选包括：

```text
actual agent_id
entry_agent_id
user_id
```

最终采用：

```text
actual executing agent_id
```

原因非常重要。

------

## 为什么不能用 entry_agent_id？

在多 Agent 委派场景：

```text
entry Agent
```

可能是：

```text
core_router
```

但真正执行 Tool 的是：

```text
data_analyst
```

如果 Permission 使用：

```text
entry_agent_id
```

就会发生：

```text
权限检查的是 A
真正执行的是 B
```

这是典型 principal confusion（主体混淆）。

COORDINATED Runtime 当前通过三源校验：

```text
claim.preferred_agent
==
plan_step.preferred_agent
==
binding / registration agent
```

最终 actual `agent_id` 可以稳定到达 Tool 执行 seam。

------

## 为什么不能用 user_id？

因为当前系统根本没有：

```text
authenticated user
user_id
tenant
organization
role
```

所以不能为了“看起来生产级”虚构一个用户 IAM（身份与访问管理）体系。

因此：

```text
WP2-B v1
= Agent authorization
```

而不是：

```text
User authorization
```

------

# 5.2 Governance Gate 放在哪里？

Scout 找到一个非常关键的事实：

生产：

```text
ToolExecutionService.execute_sync
```

只有 **一个 caller**：

```text
AgentRouter
```

而且 LEGACY 和 COORDINATED 都经过同一个位置。

这个位置：

```text
ToolRegistry.require()
之后

ToolExecutionService.execute_sync()
之前
```

能够同时拿到：

```text
actual agent_id
ToolRegistration
RunContext
step_id
```

所以它是天然的 governance join point。

------

# 5.3 为什么不是把 Governance 放进 ToolExecutionService？

因为：

```text
ToolExecutionService
```

当前输入中没有 actual executing `agent_id`。

而且它已经负责：

```text
Retry
Timeout
Cancellation
Lease
Side Effect
Idempotency
Evidence
```

如果再把：

```text
Permission
Risk policy
Approval
```

塞进去，就重新变成大而全的 Service。

Architecture 最终保持：

```text
ToolGovernanceService
= can this invocation run?

ToolExecutionService
= how does it run?
```

------

# 5.4 为什么需要两级 Gate？

这是 WP2-B 很值得讲的设计。

最终没有只做：

```text
build invocation
→ permission
→ risk
```

也没有：

```text
permission + risk
→ build
```

而是：

```text
Static Permission Gate
        ↓
build_invocation
        ↓
spec_for
        ↓
Dynamic Risk / Approval Gate
```

原因：

### Permission

只需要：

```text
agent_id
tool_name
```

所以应该尽早判断。

如果 Agent 根本没权限：

```text
build_invocation = 0
```

------

### Risk

Risk 可能依赖：

```text
execution_mode
```

因此必须先：

```text
build_invocation
spec_for(invocation)
```

才能知道真正 execution semantics。

所以两级 Gate 是：

```text
最早拒绝
+
动态风险
```

之间的平衡。

------

# 6. 最终架构

最终生产路径：

```text
Planner
   ↓
ToolRegistry.require(tool_name)
   ↓
ToolGovernanceService.authorize_tool(
    actual_agent_id,
    registration
)
   │
   ├── DENY
   │      ↓
   │   Safe denial
   │   NO EXECUTION
   │
   ▼
adapter.build_invocation(tool_args)
   ↓
adapter.spec_for(invocation)
   ↓
ToolGovernanceService.evaluate_invocation(
    context,
    registration,
    invocation,
    execution_spec
)
   │
   ├── DENY
   │
   ├── APPROVAL_REQUIRED
   │
   ▼
ALLOW
   ↓
ToolExecutionService.execute_sync(...)
   ↓
adapter.invoke_once(...)
```

这条链最终通过 Re-Gate。

------

# 7. 核心状态机与执行时序

## 7.1 Policy Catalog 生命周期

```text
CONSTRUCT
   ↓
REGISTER
   ↓
VALIDATE
   ↓
FREEZE
   ↓
PUBLISH
   ↓
READ ONLY
```

行为：

### freeze 前读取

```text
TOOL_GOVERNANCE_NOT_FROZEN
```

### duplicate

```text
TOOL_GOVERNANCE_DUPLICATE
```

### freeze 后注册

```text
TOOL_GOVERNANCE_FROZEN
```

### 配置非法

```text
TOOL_GOVERNANCE_INVALID
```

------

## 7.2 Invocation Governance 状态

可以把一次 Tool governance 简化成：

```text
STATIC_PERMISSION
       │
       ├── DENY
       │
       ▼
     ALLOW
       │
       ▼
BUILD_INVOCATION
       │
       ▼
DERIVE_EXECUTION_SPEC
       │
       ▼
RISK_EVALUATION
       │
       ├── DENY
       ├── APPROVAL_REQUIRED
       │
       ▼
     ALLOW
       │
       ▼
EXECUTION
```

注意：

```text
APPROVAL_REQUIRED
```

当前不是：

```text
PENDING
```

而是 terminal pre-execution decision（执行前终止决策）。

------

# 8. 数据、权限与 Owner 边界

最终 Owner Matrix：

| Concern                     | Owner                            |
| --------------------------- | -------------------------------- |
| Tool inventory              | `ToolRegistry`                   |
| Tool binding                | `ToolRegistration`               |
| Static Tool policy          | `ToolPolicyCatalog`              |
| Permission evaluation       | `ToolGovernanceService`          |
| Effective Risk              | `ToolGovernanceService`          |
| Approval requirement        | `ToolGovernanceService`          |
| Actual principal            | execution `agent_id`             |
| Dynamic execution semantics | `ToolExecutionSpec / spec_for()` |
| Tool execution              | `ToolExecutionService`           |
| Agent capability            | `AgentRegistry`                  |
| Human approval evidence     | NOT_IMPLEMENTED                  |
| Filesystem authorization    | NOT_IMPLEMENTED                  |

------

## 最重要的几个“不等于”

必须熟练：

```text
Tool registered
!= Tool authorized
Agent capability
!= Tool permission
Side effect
!= Risk
Idempotent
!= Safe
Permission granted
!= Approval granted
Approval required
!= Approval available
Tool permission
!= Filesystem path authorization
```

------

# 9. 兼容策略

WP2-B 采用了比较保守的兼容策略。

当前生产 Agent：

```text
core_router
data_analyst
code_expert
knowledge_expert
synthesis_agent
```

当前生产 Tool：

```text
list_files
analyze_excel
get_system_status
complex_workflow_simulator
```

最终显式建立：

```text
5 × 4 = 20
```

条 authorization relationships（授权关系）。

所有 5 个 Agent 对当前 4 个 Tool 都显式 ALLOW。

------

## 为什么这么做？

不是因为：

```text
所有 Agent 都应该永远有所有 Tool 权限
```

而是因为此前系统根本没有权限限制。

如果 WP2-B 第一次上线就凭：

```text
“data_analyst 看起来应该只能用 Excel”
```

之类主观判断限制 Tool，会改变历史行为。

因此第一版策略是：

> **先建立治理机制，不在没有业务证据时改变已有功能行为。**

------

## 关键区别

这不是：

```text
known agent → allow
```

而是：

```text
每条 ToolPolicy
明确列 5 个 agent_id
```

因此：

```text
unknown agent
missing policy
```

仍然 fail closed。

------

# 10. Bad Cases

# Bad Case 1：Wrong Principal

真实性：

```text
HYPOTHETICAL_BAD_CASE
regression-covered
```

场景：

```text
Tool policy only allows data_analyst

core_router
→ same Tool
```

结果：

```text
TOOL_PERMISSION_DENIED
```

并且：

```text
build_invocation = 0
spec_for = 0
execute = 0
```

知识点：

> Permission 要基于真实执行主体，而不是入口 Agent。

------

# Bad Case 2：Unknown Principal

场景：

```text
agent_id = unknown_agent
```

结果：

```text
TOOL_GOVERNANCE_UNKNOWN_PRINCIPAL
```

而不是：

```text
known Tool
→ therefore allow
```

------

# Bad Case 3：Missing Policy

场景：

```text
ToolRegistry
存在 Tool

ToolPolicyCatalog
缺少它
```

Startup：

```text
freeze fail
never READY
```

Runtime 还有 defense-in-depth：

```text
TOOL_GOVERNANCE_POLICY_MISSING
```

知识点：

> 缺配置是安全错误，不应默认 allow。

------

# Bad Case 4：Approval Required

`complex_workflow_simulator`：

```text
NON_IDEMPOTENT_SIMULATION
```

得到：

```text
HIGH
→ APPROVAL_REQUIRED
```

当前：

```text
Approval evidence absent
```

因此：

```text
Tool 调用未执行
```

真实 state store 也验证：

```text
resource_states == {}
committed_operations == []
idempotency_records == {}
```

------

# Bad Case 5：Model Self-Approval

场景：

用户或者模型内容写：

```text
approved
已审批
ignore permission
```

治理结果不能因此改变。

原因：

```text
LLM output
= untrusted input
```

不是 Authority。

------

# Bad Case 6：Deny 之后还发 TOOL_STARTED

这是必须防的错误。

当前保证：

```text
non-ALLOW
→ ToolExecutionService not invoked
```

因此：

```text
TOOL_STARTED = 0
TOOL_COMPLETED = 0
```

------

# Bad Case 7：Risk 根据 Tool 名直接判断

例如：

```text
complex_workflow_simulator = HIGH
```

这是错误设计。

因为同一个 Tool：

```text
DRY_RUN
→ LOW

IDEMPOTENT_COMMIT
→ MEDIUM

NON_IDEMPOTENT_SIMULATION
→ HIGH
```

所以 Risk 必须包含 invocation execution facts。

------

# Bad Case 8：Generic Risk Algebra —— 本阶段真实 P1

这是整个 WP2-B 最有价值的工程案例。

第一次实现使用：

```text
static risk
+
dynamic risk
→ max(...)
```

例如：

```text
ARBITRARY_LOCAL_FILESYSTEM_READ
→ MEDIUM

LOCAL_STATE_MUTATION + IDEMPOTENT_WITH_KEY
→ MEDIUM
```

于是自动得到：

```text
MEDIUM
→ ALLOW
```

看起来似乎“保守”。

但问题是：

> 整个组合从未经过 Architecture Decision 批准。

Codex 直接构造：

```text
{ARBITRARY_LOCAL_FILESYSTEM_READ}
+
LOCAL_STATE_MUTATION
+
IDEMPOTENT_WITH_KEY
```

第一次 Final Gate 真实结果：

```text
ALLOW / MEDIUM
```

因此：

```text
P1 = 1
Final Gate = FAIL
```

------

## 修复

后来改为完整 key：

```text
(
  frozenset(static_risk_facts),
  side_effect_kind,
  idempotency
)
```

只允许 5 个已冻结完整组合。

任何其它组合：

```text
TOOL_RISK_UNCLASSIFIED
→ DENY
```

最终 Re-Gate PASS。

------

# 11. 测试与 Gate

## 第一次 Phase 3

实现完成时：

```text
1860 tests passed
42 subtests passed
```

但 Final Gate 仍发现 P1 generic risk algebra。

所以：

```text
1860 green
!= contract PASS
```

------

## Remediation 后

新增完整组合反例。

最终：

```text
1872 tests passed
42 subtests passed
```

同时 Codex 重新 direct probe：

```text
original P1 combination
→ TOOL_RISK_UNCLASSIFIED

second cross combination
→ TOOL_RISK_UNCLASSIFIED

multiple static facts
→ TOOL_RISK_UNCLASSIFIED

unknown dynamic
→ TOOL_RISK_UNCLASSIFIED
```

------

## 最终测试

```text
Governance                56
WP2-B targeted            127
WP2-A regression           74
Tool Runtime              295
WP1                       136
Critical Runtime          113

Full                     1872
Subtests                    42
```

------

# 12. Known Limitations

# 12.1 没有真正人工审批

当前只能：

```text
HIGH
→ APPROVAL_REQUIRED
→ current version cannot grant approval
→ deny
```

没有：

```text
Human clicks approve
→ resume
```

------

# 12.2 没有 durable approval

当前 Runtime：

```text
Recovery = validation-only
```

没有：

```text
pause
persist pending run
resume later
```

------

# 12.3 Tool Permission 不是文件系统权限

例如：

```text
data_analyst
→ allowed list_files
```

只代表：

```text
can invoke this Tool
```

不代表：

```text
can read D:\
can only read workspace
cannot read secrets
```

路径授权留给 WP3。

------

# 12.4 没有 dedicated governance events

当前 DENY：

```text
不发 TOOL_STARTED
不发 TOOL_COMPLETED
```

但也没有：

```text
TOOL_PERMISSION_DENIED Event
TOOL_APPROVAL_REQUIRED Event
```

没有 dedicated governance Journal fact。

丰富可观测性后续再做。

------

# 12.5 Governance contract 仍是 INTERNAL_RC

当前：

```text
ToolPolicy
ToolPolicyCatalog
ToolGovernanceService
ToolGovernanceDecision
ToolGovernanceContext
```

都没有进入 Public Stable Contract。

------

# 12.6 spec_for 双调用

Router 为治理调用：

```text
spec_for()
```

ToolExecutionService 旧执行合同又调用：

```text
spec_for()
```

当前 Adapter 已验证：

```text
pure
deterministic
```

所以可接受。

但这是 Known Limitation。

------

# 13. 这次体现的工程能力

# 13.1 Authentication 与 Authorization 分离意识

虽然当前没有 Authentication（认证），但已经清楚区分：

```text
identity
permission
```

系统当前 principal 是：

```text
Agent identity
```

而不是伪造 User identity。

------

# 13.2 Existence 与 Authorization 分离

这是平台设计核心：

```text
ToolRegistry
→ exists

ToolPolicyCatalog
→ policy

ToolGovernanceService
→ decision
```

------

# 13.3 Static Fact 与 Dynamic Fact 分离

Static：

```text
filesystem read
system info read
```

Dynamic：

```text
side effect
idempotency
```

Static policy不能复制 dynamic execution truth。

------

# 13.4 Policy 与 Mechanism 分离

```text
ToolPolicyCatalog
= policy facts

ToolGovernanceService
= decision mechanism
```

类似经典安全架构：

```text
policy
≠
enforcement mechanism
```

------

# 13.5 Fail Closed

多个层面都使用：

```text
fail closed
```

例如：

```text
unknown principal
missing policy
unknown risk combination
approval required but unavailable
startup coverage incomplete
```

都不会自动 allow。

------

# 13.6 Closed-World Risk Model

这是本阶段非常重要的知识。

第一版 generic max 相当于：

```text
Open-world inference
```

即：

> 没见过这个组合，但我可以推测。

修复后：

```text
Closed-world classification
```

即：

> 没有明确批准的组合就是未知，不能执行。

对安全控制而言，第二种通常更可控。

------

# 13.7 Governance Before Execution

核心安全不变量：

```text
non-ALLOW
→ execute = 0
```

而不是：

```text
execute
→ 然后再判断是否合法
```

------

# 14. 30 秒面试表达

我在 LocalAgent 的 Tool Registry 完成后又做了一层 Tool Governance。之前系统能知道 Tool 是否存在，也能统一经过 ToolExecutionService执行，但没有真正的 Agent级权限、风险和审批门禁。

我最终把实际执行 `agent_id` 作为 principal，用 ToolPolicyCatalog维护静态冻结策略，用 ToolGovernanceService作为唯一 Permission、Risk 和 Approval Authority。在 AgentRouter唯一 Tool执行入口做了两级 Gate：先按 Agent和Tool做静态权限检查，通过后再构造 invocation，根据 `spec_for()` 得到真实 side effect和idempotency，然后做动态风险和审批判断，只有ALLOW才进入ToolExecutionService。

当前4个Tool和5个Agent建立了20条显式授权关系，高风险调用返回APPROVAL_REQUIRED，但由于当前没有审批凭据和pause/resume，所以会在执行前安全拒绝。

第一次Final Gate还发现我们把静态风险和动态风险直接取max，会自动批准未冻结组合，所以Gate以P1失败。后来改成完整风险组合精确白名单，未定义组合统一fail closed，最终1872个测试和Re-Gate全部通过。

------

# 15. 2 分钟面试表达

WP2-B主要解决的是Tool已经平台化，但治理还缺失的问题。

WP2-A之后系统已经有ToolRegistry和ToolExecutionService，4个生产Tool也全部走统一Runtime contract，但Scout发现没有Tool Permission、Risk taxonomy和Approval gate。

第一步我先确定principal。因为项目是多Agent架构，不能简单拿entry_agent_id，委派执行时入口Agent和实际执行Agent可能不同。最后用了实际执行agent_id，COORDINATED路径通过PlanStep、StepClaim和binding三源校验，LEGACY也能把同一个agent_id传到AgentRouter。

然后在AgentRouter里找到唯一Tool执行join point。ToolExecutionService生产调用点只有一个，所以不需要到处加权限检查。我没有把权限塞进ToolExecutionService，而是增加ToolPolicyCatalog和ToolGovernanceService，把Tool存在、治理和执行拆成三个Owner。

Gate分两层：第一层只用agent_id+tool_name做Permission，拒绝时连build_invocation都不做；通过后再build invocation并调用spec_for拿到真正的side effect和idempotency，再做Risk和Approval判断。

Risk还有一个真实踩坑。第一版我们把静态风险和动态风险取max，看起来比较保守，但Codex Final Gate构造了一个未冻结的“文件系统读取+本地状态修改”组合，它仍然被自动判MEDIUM并ALLOW。这其实破坏了fail-closed合同。所以后来改成完整组合精确白名单，只有5个Architecture批准组合可以得到风险等级，其他全部TOOL_RISK_UNCLASSIFIED并拒绝。

最终Re-Gate是P0/P1/P2全0，全量1872通过。当前审批只做到“判断需要审批并阻止执行”，没有人工审批UI、approval token和durable resume，我不会把它描述成完整HITL系统。

------

# 16. 深入版本：这套治理模型到底怎么工作

可以把整个 WP2-B 理解成四层。

## 第一层：Identity

回答：

```text
Who?
```

当前：

```text
actual executing agent_id
```

------

## 第二层：Static Permission

回答：

```text
Can this Agent use this Tool at all?
```

输入：

```text
agent_id
tool_name
```

结果：

```text
ALLOW
DENY
```

------

## 第三层：Invocation Risk

回答：

```text
This specific invocation is how risky?
```

输入：

```text
static Tool risk facts

+

ToolExecutionSpec.side_effect_kind
ToolExecutionSpec.idempotency
```

输出：

```text
LOW
MEDIUM
HIGH
```

但前提是 full combination 已被明确批准。

------

## 第四层：Approval Requirement

当前规则：

```text
LOW
→ ALLOW

MEDIUM
→ ALLOW

HIGH
→ APPROVAL_REQUIRED
```

由于：

```text
approval evidence absent
```

所以：

```text
APPROVAL_REQUIRED
→ stop before execution
```

整个链：

```text
IDENTITY
   ↓
PERMISSION
   ↓
INVOCATION FACTS
   ↓
RISK
   ↓
APPROVAL REQUIREMENT
   ↓
EXECUTION
```

------

# 17. 高频追问与参考答案

## Q1：Permission、Risk、Approval有什么区别？

Permission回答：

```text
谁能不能使用Tool
```

Risk回答：

```text
这次调用可能造成什么程度影响
```

Approval回答：

```text
即使有权限，这次调用是否还需要额外授权
```

因此：

```text
Permission = ALLOW
```

不代表：

```text
Approval not required
```

------

## Q2：为什么ToolRegistry不直接保存allowed_agents？

因为：

```text
ToolRegistry
```

负责：

```text
Tool existence / binding
```

而：

```text
allowed_agents
```

属于治理 Policy。

把两者混合会导致：

```text
exists
```

与：

```text
authorized
```

职责耦合。

------

## Q3：为什么不把allowed_tools放到AgentRegistry？

同样因为：

```text
AgentRegistry
```

当前拥有 Agent identity、capability、delegation。

它的 capability：

```text
data_analysis
rag
code_reasoning
```

不是 security permission。

把 Tool permission直接塞进去会把规划能力和授权混为一谈。

------

## Q4：为什么principal用actual agent_id，而不是entry_agent_id？

因为多Agent委派时：

```text
entry Agent != execution Agent
```

权限必须检查真正执行Tool的人。

------

## Q5：run_id能不能当principal？

不能。

```text
agent_id
= who

run_id
= which execution
```

run_id是scope，不是identity。

------

## Q6：为什么要两层Gate？

因为Permission不需要解析Tool参数，可以尽早拒绝。

Risk却可能依赖：

```text
execution_mode
```

所以必须先build invocation和derive spec。

------

## Q7：为什么build_invocation可以发生在Risk Gate前？

因为Scout和Architecture审计确认当前4个Adapter的build只做：

```text
parse
validate
construct immutable ToolInvocation
```

没有IO、状态修改或Tool执行。

------

## Q8：为什么spec_for也能在Risk Gate前运行？

因为它就是动态Risk的source truth。

当前Adapter的spec_for也被Final Gate验证为：

```text
pure
deterministic
```

------

## Q9：为什么Risk不直接根据side_effect_kind判断？

因为：

```text
SideEffect
!=
Risk
```

例如：

```text
list_files
```

side effect：

```text
NONE
```

但它可以读取任意本地路径，所以仍被定为：

```text
MEDIUM
```

------

## Q10：为什么read-only Tool还能是MEDIUM？

因为：

```text
read only
```

只说明没有写副作用。

不代表：

```text
data is non-sensitive
```

读取本地敏感数据仍是安全风险。

------

## Q11：为什么idempotent不代表安全？

Idempotency（幂等性）只回答：

> 重复执行会不会造成额外变化？

它不回答：

> 第一次执行本身是否允许？

------

## Q12：为什么不能用max合并Risk？不是更保守吗？

这是很重要的追问。

`max`只是在已知等级之间做数学比较。

但是安全合同真正的问题是：

```text
这个完整组合有没有被审批过？
```

如果组合从未被Architecture定义，系统不应该自行推导。

所以：

```text
unknown combination
→ fail closed
```

比：

```text
猜一个较高等级
```

更符合当前合同。

------

## Q13：为什么不用HIGH兜底未知Risk？

因为：

```text
UNKNOWN
!=
HIGH
```

HIGH表示：

> 已经知道这个组合，并且它被分类为高风险。

UNKNOWN表示：

> 这个组合根本没有合同定义。

把UNKNOWN变HIGH是在偷偷建立新语义。

------

## Q14：Approval现在能真正批准吗？

不能。

当前只能：

```text
detect approval requirement
→ deny safely
```

没有：

```text
grant approval
resume execution
```

------

## Q15：那为什么还叫Approval Gate？

因为系统真实实现了：

```text
是否需要审批
```

这个 policy decision。

只是：

```text
审批授予能力
```

尚未实现。

------

## Q16：为什么不顺便做HITL？

因为当前Runtime没有：

```text
pause
persistent pending state
human interaction
resume
```

如果硬做，会扩大到Durable Execution和UI workflow，不再是“最小生产化”。

------

## Q17：Permission deny后Run是不是可能SUCCESS？

可能作为安全业务结果进入当前既有结果链。

关键是它明确写：

```text
Tool 调用未执行
```

同时：

```text
没有Tool events
没有Tool evidence
没有Tool success observation
```

所以不会伪造Tool成功。

------

## Q18：为什么没有TOOL_DENIED事件？

WP2-B选择不修改：

```text
PUBLIC_VERSIONED RuntimeEvent
```

当前 correctness依靠typed governance decision、no-execution invariant和safe response保证。

专门治理可观测性留后续阶段。

------

## Q19：ToolGovernanceService是不是又变成一个大Service？

当前它只负责：

```text
permission
effective risk
approval requirement
```

不拥有：

```text
Tool inventory
Tool implementation
Tool execution
Agent capability
```

所以边界仍然清晰。

------

## Q20：为什么Policy Catalog也要freeze？

因为当前Tool和Agent inventory都是static startup。

没有dynamic policy需求时：

```text
startup mutation
→ validate
→ freeze
→ runtime read-only
```

比运行期可变更可靠。

------

# 18. 容易答错或夸大的问题

## 错误 1

“我们已经有完整RBAC。”

错误。

当前只是：

```text
per-Tool explicit allowed Agent IDs
```

------

## 错误 2

“支持用户权限。”

错误。

当前principal是：

```text
Agent
```

不是authenticated user。

------

## 错误 3

“已经实现HITL审批。”

错误。

没有approval evidence、UI、pause/resume。

------

## 错误 4

“HIGH风险Tool可以等用户批准后继续。”

错误。

当前：

```text
HIGH
→ APPROVAL_REQUIRED
→ Tool不执行
```

没有resume。

------

## 错误 5

“Permission限制了文件路径。”

错误。

只控制：

```text
who may invoke list_files
```

不控制：

```text
which path may be read
```

------

## 错误 6

“Risk就是ToolExecutionSpec.side_effect_kind。”

错误。

两者不同。

------

## 错误 7

“只读Tool风险都是LOW。”

错误。

`list_files`、`analyze_excel`：

```text
READ_ONLY
+
arbitrary local filesystem read
→ MEDIUM
```

------

## 错误 8

“未识别Risk就当HIGH最安全。”

不符合当前冻结合同。

当前：

```text
unclassified
→ TOOL_RISK_UNCLASSIFIED
→ DENY
```

------

## 错误 9

“ToolGovernanceService负责Tool执行。”

错误。

它只负责governance。

实际执行：

```text
ToolExecutionService
```

------

## 错误 10

“Final Gate一次就通过。”

错误。

第一次：

```text
1860 passed
P1 = 1
Final Gate FAIL
```

修复 exact full-combination 后：

```text
1872 passed
P1 = 0
Re-Gate PASS
```

------

# 19. P0 / P1 / P2 复习重点

# P0：必须掌握

## 1. Principal

```text
actual executing agent_id
```

为什么不是entry_agent_id。

------

## 2. 三个Owner

```text
ToolRegistry
ToolGovernanceService
ToolExecutionService
```

分别回答：

```text
exists?
allowed?
execute?
```

------

## 3. 两级Gate

```text
Permission
→ build/spec
→ Risk/Approval
→ Execution
```

------

## 4. Permission != Capability

必须能解释：

```text
data_analysis capability
```

为什么不是：

```text
analyze_excel permission
```

------

## 5. Risk != Side Effect

特别拿：

```text
list_files
```

举例。

------

## 6. Approval能力边界

必须明确：

```text
approval requirement
YES

approval grant
NO
```

------

## 7. Exact Full Combination

这是本阶段最关键的安全思想：

```text
known full combination
→ classify

everything else
→ fail closed
```

------

## 8. P1 generic risk algebra

必须能完整讲：

```text
问题
为什么测试没发现
为什么max不安全
怎么修
怎么回归
```

------

# P1：建议熟练

## 9. Catalog freeze

为什么：

```text
partial policy
```

不能进入Runtime。

------

## 10. Startup coverage validation

包括：

```text
missing Tool
unknown Tool
unknown Agent
disabled Agent
duplicate
empty allowed set
```

------

## 11. 20条 explicit authorization

为什么不是implicit allow。

------

## 12. Unknown principal vs permission denied

```text
UNKNOWN_PRINCIPAL
```

和：

```text
PERMISSION_DENIED
```

是不同语义。

------

## 13. Governance Error vs ToolExecutionError

Tool根本没开始执行，所以不要伪造：

```text
ToolExecutionError(PERMISSION_DENIED)
```

------

## 14. No Tool events

Governance deny：

```text
TOOL_STARTED = 0
```

------

## 15. spec_for double-call

为什么目前可以接受。

------

## 16. Safe denial

为什么不再调用final-answer model重新包装。

------

# P2：了解即可

## 17. User IAM

当前没有。

------

## 18. Approval evidence

未来如果做，需要考虑：

```text
principal binding
tool binding
invocation binding
expiry
replay
storage
```

但这不是当前完成内容。

------

## 19. Filesystem authorization

属于WP3。

------

## 20. Governance observability

专用event / journal / metrics当前未实现。

------

# 20. 最终面试速查表

| 项目                                | 当前事实                           |
| ----------------------------------- | ---------------------------------- |
| WP                                  | WP2-B                              |
| 名称                                | Tool Risk / Permission / Approval  |
| Final Gate                          | PASS                               |
| P0                                  | 0                                  |
| P1                                  | 0                                  |
| P2                                  | 0                                  |
| TEST_GAP                            | 1                                  |
| Principal                           | actual executing `agent_id`        |
| User IAM                            | NOT_IMPLEMENTED                    |
| Tool inventory Owner                | `ToolRegistry`                     |
| Policy Owner                        | `ToolPolicyCatalog`                |
| Governance Authority                | `ToolGovernanceService`            |
| Execution Owner                     | `ToolExecutionService`             |
| Permission model                    | explicit Agent→Tool authorization  |
| Tool policies                       | 4                                  |
| Production Agents                   | 5                                  |
| Explicit relations                  | 20                                 |
| Default allow                       | NO                                 |
| Unknown principal                   | DENY                               |
| Missing policy                      | fail closed                        |
| Policy lifecycle                    | startup validate/freeze/read-only  |
| Risk facts                          | filesystem read / system info read |
| Risk levels                         | LOW / MEDIUM / HIGH                |
| Dynamic facts                       | side effect + idempotency          |
| Risk model                          | exact full-combination             |
| Unknown combination                 | `TOOL_RISK_UNCLASSIFIED`           |
| LOW                                 | allow                              |
| MEDIUM                              | allow                              |
| HIGH                                | approval required                  |
| Approval evidence                   | NOT_IMPLEMENTED                    |
| Human approval                      | NOT_IMPLEMENTED                    |
| Pause/resume                        | NOT_SUPPORTED                      |
| Permission deny executes Tool?      | NO                                 |
| Approval-required executes Tool?    | NO                                 |
| Governance deny emits TOOL_STARTED? | NO                                 |
| RuntimeEvent governance event       | NOT_IMPLEMENTED                    |
| Governance Journal fact             | NOT_IMPLEMENTED                    |
| Filesystem path authorization       | NOT_IMPLEMENTED                    |
| Sandbox                             | NOT_IMPLEMENTED                    |
| Contract class                      | INTERNAL_RC                        |
| Full Coordinated Tool E2E           | DEFER_TO_WP2-C                     |
| Governance tests                    | 56 passed                          |
| WP2-B targeted                      | 127 passed                         |
| WP2-A regression                    | 74 passed                          |
| Tool Runtime                        | 295 passed                         |
| WP1                                 | 136 passed                         |
| Critical Runtime                    | 113 passed                         |
| Full regression                     | 1872 passed                        |
| Subtests                            | 42 passed                          |

## 最值得记住的一句话

> **WP2-B 的核心不是“给 Tool 加一个 risk_level 字段”，而是建立一个真正的执行前治理边界：用实际执行 Agent 作为 principal，用 ToolPolicyCatalog 管静态 policy，用 ToolGovernanceService 在唯一 Tool 执行 seam 上做两级 Permission 与 Risk/Approval Gate；ToolRegistry只回答存在性，ToolExecutionService只负责真实执行，所有未明确批准的权限或风险组合都 fail closed。**

## 最值得讲的工程 Bad Case

> **第一版实现把静态风险与动态风险分别分类后取最大值，虽然1860个测试全部通过，但Codex Final Gate构造了一个“文件系统读取 + 本地幂等状态修改”的未冻结完整组合，系统自动判为MEDIUM并ALLOW。问题不在风险等级高低，而在于系统擅自推导了Architecture没有批准的组合。因此Gate以P1失败。后来把Risk模型改成完整三元组精确白名单，只有5个冻结组合能够分类，其余全部TOOL_RISK_UNCLASSIFIED并阻止执行，补充交叉组合和未知组合测试后，最终1872个测试以及Final Re-Gate全部通过。**