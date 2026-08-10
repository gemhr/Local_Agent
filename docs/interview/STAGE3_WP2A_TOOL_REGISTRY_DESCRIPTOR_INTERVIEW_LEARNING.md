# LocalAgent Stage 3 WP2-A — Tool Registry / Descriptor 工程面试学习材料

# 1. 一句话项目 / 工作包定义

WP2-A 的目标，是把 LocalAgent 原本分散在 `AgentRouter`、Tool 注册函数和 Adapter（适配器）里的工具信息，收敛成一个明确的 **ToolRegistry（工具注册表）+ ToolDescriptor（工具描述符）+ ToolRegistration（工具注册记录）**平台边界。

最终形成：

```text
ToolRegistry
= 当前有哪些 Tool 的唯一事实源

ToolDescriptor
= Tool 的静态身份与描述

ToolRegistration
= Descriptor + application-scoped ToolAdapter

AgentRouter
= Tool resolution / orchestration

ToolExecutionService
= 所有生产 Tool 的唯一执行 Owner
```

当前 4 个真实生产 Tool：

```text
list_files
analyze_excel
get_system_status
complex_workflow_simulator
```

最终全部进入：

```text
ToolRegistry
→ ToolRegistration
→ ToolAdapter
→ ToolExecutionService
```

统一执行路径。

最终：

```text
WP2-A Final Re-Gate = PASS
WP2-A completed = YES

P0 = 0
P1 = 0
P2 = 0
TEST_GAP = 1
```

唯一剩余 TEST_GAP 是完整 Coordinated Runtime（协调运行时）入口到 Tool execution（工具执行）的 E2E（端到端测试），已明确留给 WP2-C。

------

# 2. 为什么要做

Stage 2 已经解决了一个很重要的问题：

> **Tool 调用之后应该怎样可靠执行？**

已经存在：

```text
ToolInvocation
ToolExecutionSpec
ToolExecutionResult
ToolExecutionError

Idempotency
Lease
Side Effect
Timeout
Cancellation
Evidence
RuntimeEvent
Recovery Validation
```

但 WP2-A Scout Audit（侦察审计）发现：

> **“Tool 怎么执行”已经比较生产化，但“系统到底有哪些 Tool”还没有真正平台化。**

修改前：

```text
AgentRouter.tools
= mutable dict
```

既承担：

```text
Tool name
description
func
adapter
```

又参与：

```text
planner prompt
Tool lookup
execution dispatch
```

同时 `_tool_intent_likely` 还维护了一部分 Tool 名称和 Tool 相关关键词。

更严重的是，当时 4 个 Tool 中只有两个走完整 Runtime Tool Contract（运行时工具合同）：

```text
get_system_status
complex_workflow_simulator
```

而：

```text
list_files
analyze_excel
```

仍然：

```text
func(tool_args)
```

直接调用，绕过 `ToolExecutionService`。

所以原架构实际上存在两个层面的问题：

```text
Catalog / Registry truth 不统一

+

Execution path 不统一
```

WP2-A 就是把这两件事收口。

------

# 3. 真实性与完成边界

## 已真实实现

| 能力                                      | 当前状态 |
| ----------------------------------------- | -------- |
| 独立 `ToolRegistry`                       | 已实现   |
| `ToolDescriptor`                          | 已实现   |
| `ToolRegistration`                        | 已实现   |
| Tool Registry startup registration        | 已实现   |
| Registry freeze                           | 已实现   |
| Duplicate fail-closed                     | 已实现   |
| Unknown trusted lookup fail-closed        | 已实现   |
| Deterministic enumeration                 | 已实现   |
| `AgentRouter` 注入 Registry               | 已实现   |
| Planner prompt 从 Descriptor 派生         | 已实现   |
| `AgentRouter.tools` 只读兼容视图          | 已实现   |
| 4/4 Tool adapter-backed                   | 已实现   |
| Legacy direct-call path 删除              | 已实现   |
| `list_files` Runtime 化                   | 已实现   |
| `analyze_excel` Runtime 化                | 已实现   |
| Registry typed error codes                | 已实现   |
| Unicode control-character Descriptor 校验 | 已实现   |

## 已真实测试

最终：

```text
ToolRegistry targeted       41 passed
WP2-A targeted              131 passed
Tool Runtime regression     239 passed
WP1 regression              134 passed
Critical Runtime regression 101 passed

Full collection             1813
Full regression             1813 passed
Skipped                     0
Subtests                    42 passed
```

同时：

```text
compileall PASS
uv lock --check PASS
git diff --check PASS
packaging diff EMPTY
```

## 只完成设计 / 未实现

以下没有在 WP2-A 实现：

```text
Tool input schema platform
Tool output schema platform
Tool version
Tool namespace
Tool alias
dynamic Tool registration
external Tool discovery API
/api/tools
function calling
OpenAI-style tool schema
```

## Known Limitation（已知限制）

当前：

```text
AgentRouter.tools
仍保留 read-only compatibility view

ToolRegistry / Descriptor / Registration
仍是 INTERNAL_RC

Coordinated Runtime → Tool 完整 E2E
仍 defer 到 WP2-C

test helper legacy_function
仍是一次性 compatibility seam
```

## 明确属于未来阶段

```text
Tool Risk
Tool Permission
Tool Approval
Agent allowed tools
HITL Tool approval
```

全部属于 WP2-B，而不是 WP2-A。

------

# 4. 修改前架构与根因

修改前可以抽象成：

```text
server.py::lifespan()
        │
        ▼
AgentRouter
        │
        ├── self.tools = {}
        │
        ├── register_tool(...)
        │
        └── attach_tool_adapter(...)
        │
        ▼
AgentRouter.tools
{
    name: {
        func,
        description,
        adapter
    }
}
```

Tool 调用时：

```text
planner
  ↓
tool_name
  ↓
AgentRouter.tools[name]
  ↓
adapter?
 ┌──────────────┴──────────────┐
 YES                            NO
 ↓                              ↓
ToolExecutionService            func(tool_args)
```

这意味着：

```text
ToolExecutionService
```

虽然理论上是 Tool Runtime 核心执行层，但并没有真正覆盖全部 production Tool。

根因不是简单：

> “缺一个 Registry 类”。

真正根因是 **Owner 边界混乱**。

原来的 `AgentRouter` 同时知道：

```text
有哪些 Tool
Tool 描述是什么
Tool implementation 是什么
Tool Adapter 是什么
Tool 怎么选
Tool 怎么执行
```

职责过多。

同时：

```text
description
adapter.spec
_tool_intent_likely
```

又形成了不同 Tool knowledge source（工具知识源）。

------

# 5. 方案讨论与技术取舍

Architecture Decision 一共比较了三种 Registry Owner 方案。

## 方案 A：继续使用 AgentRouter.tools

思路：

```text
AgentRouter.tools
→ 强类型化
→ freeze
```

优点：

```text
改动少
```

但问题：

```text
Registry 仍绑死在 AgentRouter
Catalog Owner 和 Consumer 是同一个组件
未来 WP2-B permission policy 继续耦合 Router
```

最终拒绝。

------

## 方案 B：独立 ToolRegistry

最终采用：

```text
ToolRegistry
```

独立 application-scoped（应用级）对象。

由：

```text
server.py::lifespan()
```

创建、填充、freeze，再注入 `AgentRouter`。

优点：

```text
Tool catalog 有唯一 Owner
Router 只是 Consumer
ToolExecutionService 仍保持纯执行 Owner
未来 WP2-B 可以建立在 Registry 上
```

最终采用。

------

## 方案 C：ToolExecutionService 自己拥有 Registry

被拒绝。

因为当前 `ToolExecutionService` 的接口设计就是：

```text
caller resolves ToolAdapter
→ execute(invocation, adapter, ...)
```

Service 负责的是：

```text
Retry
Lease
Timeout
Cancellation
Side Effect
Evidence
Result/Error
```

如果再让它做：

```text
tool_name → Tool lookup
```

就会把：

```text
discovery / catalog
```

和：

```text
execution
```

重新耦合。

------

# 6. 最终架构

最终架构：

```text
server.py::lifespan()
        │
        ▼
ToolRegistry()
        │
        ▼
register_all_tools(registry)
        │
        ▼
registry.freeze()
        │
        ▼
AgentRouter(tool_registry=registry)
        │
        ├──────────── Planner
        │                 │
        │                 ▼
        │        registry.descriptors()
        │                 │
        │           name + description
        │
        ▼
tool_name
        │
        ▼
ToolRegistry.resolve / require
        │
        ▼
ToolRegistration
        │
        ├── ToolDescriptor
        │
        └── ToolAdapter
                │
                ▼
        ToolExecutionService
                │
                ▼
 ToolExecutionResult / Error / Evidence
```

这里最重要的是 Owner 拆分：

```text
ToolRegistry
→ What tools exist?

AgentRouter
→ Which requested Tool should be used?

ToolExecutionService
→ How should this Tool invocation execute?
```

------

# 7. 核心状态机和时序

## Registry 生命周期

```text
CONSTRUCT
   │
   ▼
REGISTER
   │
   ▼
VALIDATE
   │
   ▼
FREEZE
   │
   ▼
PUBLISH / INJECT
   │
   ▼
READ-ONLY RUNTIME
```

这里虽然没有定义显式 enum 状态，但行为合同已经冻结。

### Freeze 前

允许：

```text
register()
```

不允许：

```text
resolve
require
registrations
descriptors
contains
```

否则：

```text
TOOL_REGISTRY_NOT_FROZEN
```

### Freeze 后

允许：

```text
resolve
require
registrations
descriptors
contains
```

不允许：

```text
register
```

否则：

```text
TOOL_REGISTRY_FROZEN
```

------

## Production startup 时序

```text
ToolRegistry()
   ↓
register Tool #1
   ↓
register Tool #2
   ↓
register Tool #3
   ↓
register Tool #4
   ↓
freeze()
   ↓
AgentRouter(registry)
   ↓
Application Runtime continues startup
```

如果中间：

```text
duplicate
invalid Descriptor
binding mismatch
```

则：

```text
startup fails
never READY
```

不会：

```text
“坏掉一个 Tool，剩下三个照常启动”
```

这是典型的 fail-closed（失败关闭）设计。

------

# 8. 数据、权限与 Owner 边界

最终 Owner Map：

| Concern                             | Owner                              |
| ----------------------------------- | ---------------------------------- |
| Tool inventory                      | `ToolRegistry`                     |
| Tool identity                       | `ToolDescriptor.name` / Registry   |
| Tool description                    | `ToolDescriptor.description`       |
| Tool execution binding              | `ToolRegistration.adapter`         |
| Tool resolution                     | `AgentRouter`                      |
| Tool execution                      | `ToolExecutionService`             |
| Invocation-specific execution truth | `ToolExecutionSpec` / `spec_for()` |
| Side-effect state                   | 既有 Runtime Owner                 |
| Lease                               | `ToolConcurrencyController`        |
| Evidence                            | Runtime Tool execution layer       |
| Agent capabilities                  | `AgentRegistry`                    |
| Tool permission                     | 尚未实现，WP2-B                    |

尤其要记住：

```text
AgentRegistry != ToolRegistry
```

以及：

```text
Tool availability != Tool permission
```

当前“Tool 已注册”只能说明：

> 系统存在这个 Tool。

不能说明：

> 某个 Agent 有权限调用这个 Tool。

后者属于 WP2-B。

------

# 9. 兼容策略

WP2-A 没有选择“一次把所有 Tool 系统重写掉”，而是做了比较克制的兼容改造。

## Tool name 保持兼容

仍然使用：

```text
tool_name: str
```

作为 canonical identity（规范身份）。

没有新增：

```text
namespace
version
alias
case folding
```

这样不破坏现有 `ToolInvocation.tool_name` 合同。

Tool name contract：

```text
^[a-z][a-z0-9_]{0,63}$
```

且大小写敏感。

------

## AgentRouter.tools 暂时保留

没有立即删除：

```text
router.tools
```

而改成：

```text
read-only compatibility view
```

它从 frozen Registry 派生，不再是第二个 mutable dict。

这样可以：

```text
减少旧测试/旧调用方一次性 churn
```

同时保证：

```text
ToolRegistry
```

仍是唯一事实源。

------

## Legacy Tool 的业务函数不重写

`list_files` 和 `analyze_excel` 的业务函数没有修改。

只是：

```text
plain callable
```

外面包上现有：

```text
LegacyStringToolAdapter
```

然后进入 Runtime contract。

这是一个很好的“渐进迁移”案例：

> 不重写业务逻辑，只替换执行边界。

------

# 10. Bad Cases

# Bad Case 1：Duplicate Tool 静默覆盖

### 真实性

**SOURCE_AUDIT_FINDING（源码审查发现）**

修改前：

```python
self.tools[name] = ...
```

同名 Tool 再注册：

```text
last-write-wins
```

没有异常、没有 warning。

### 风险

例如：

```text
list_files
```

本应绑定 A implementation，

但第二次注册成 B：

```text
Tool name 没变
真正执行代码却变了
```

Planner、日志、Metric 都可能仍认为自己调用的是同一个 Tool。

### 修复

现在：

```text
TOOL_REGISTRY_DUPLICATE
```

并保留原 registration。

### 知识点

> Registry 的 identity collision（身份冲突）不应该采用 last-write-wins。

------

# Bad Case 2：Registry 运行时可修改

### 真实性

最初是 **HYPOTHETICAL BAD CASE（假设构造 Bad Case）**。

Scout 发现 `AgentRouter.tools` 是 mutable dict，但 production 实际只在 startup 注册，没有真实运行期 mutation 事故。

### 修复

现在：

```text
startup mutation
→ freeze
→ runtime read-only
```

### 回归

freeze 后注册：

```text
TOOL_REGISTRY_FROZEN
```

------

# Bad Case 3：两个 Tool 绕过 Runtime Tool Contract

### 真实性

**SOURCE_AUDIT_FINDING**

```text
list_files
analyze_excel
```

直接：

```text
func(tool_args)
```

绕过：

```text
ToolExecutionService
```

因此缺少：

```text
ToolExecutionResult/Error
Timeout
Lease
Idempotency
Evidence
RuntimeEvent
```

### 修复

两个 Tool 都改为：

```text
LegacyStringToolAdapter
→ ToolExecutionService
```

现在 4/4 Tool 都统一。

------

# Bad Case 4：Legacy Tool 把错误字符串当成功结果

修改前业务函数可能返回：

```text
List files failed: xxx
Excel analysis failed: xxx
```

但这是普通字符串，所以 AgentRouter 会把它当：

```text
successful Tool observation
```

加入模型上下文。

迁移后配置 error prefix：

```text
list_files:
Path does not exist:
List files failed:

analyze_excel:
File not found:
Excel analysis failed:
```

这些被 Adapter 收敛成：

```text
LEGACY_TOOL_REPORTED_ERROR
```

而不是成功结果。

------

# Bad Case 5：Descriptor 和 Adapter 名字不一致

例如：

```text
Descriptor.name = list_files

adapter.spec.tool_name = analyze_excel
```

如果允许启动：

```text
Registry 描述的 Tool
!=
真正执行的 Tool
```

现在：

```text
TOOL_REGISTRY_INVALID
```

启动阶段 fail closed。

------

# Bad Case 6：Planner hallucinate 一个不存在的 Tool

这里专门区分 trust boundary（信任边界）。

模型返回：

```text
CALL: delete_database(...)
```

但 Registry 没有该 Tool。

这是：

```text
untrusted model output
```

所以：

```text
resolve()
→ None
→ reject candidate
```

不会执行。

但内部代码写：

```text
registry.require("missing_tool")
```

这说明：

```text
internal programming/configuration error
```

因此：

```text
TOOL_NOT_REGISTERED
```

直接 fail closed。

这是很典型的：

> **同一个“找不到”问题，在不同 trust boundary 下应该有不同错误语义。**

------

# Bad Case 7：ToolDescriptor 控制字符漏检

### 真实性

这是本阶段最重要的 **Final Gate 真实发现**。

Phase 3 实现后：

```text
1810 tests passed
```

但 Final Gate direct probe 发现：

```text
U+007F DEL → accepted
U+0085 NEL → accepted
```

因为代码只判断：

```python
ord(char) < 32
```

只能覆盖 ASCII C0 控制字符。

### Gate

因此：

```text
P1 = 1
WP2-A Final Gate = FAIL
```

尽管：

```text
1810 tests passed
```

也不能覆盖这个事实。

### 修复

改为：

```python
unicodedata.category(char) == "Cc"
```

并新增：

```text
U+007F
U+0085
```

回归。

最终：

```text
1813 passed
Final Re-Gate PASS
```

### 面试价值

这个案例非常值得重点讲：

> **测试全绿不是合同正确性的充分条件，对 contract-driven system（合同驱动系统）仍然需要 adversarial counterexample（对抗反例）。**

------

# 11. 测试与验收

最终 Re-Gate 实际测试：

## ToolRegistry Core

```text
41 passed
```

## WP2-A Targeted

```text
131 passed
```

## Tool Runtime

```text
239 passed
1574 deselected
```

## WP1 Regression

```text
134 passed
```

用于证明新的 Tool Registry Composition Root（组合根）装配没有破坏：

```text
startup
health/readiness
runtime lifespan
graceful shutdown
client readiness
ApplicationRuntimeServices
```

## Critical Runtime

```text
101 passed
```

## Full Regression

```text
1813 collected
1813 passed
0 failed
0 skipped
42 subtests passed
4 warnings
```

尤其重要：

第一次 Final Gate 时：

```text
1810 passed
```

仍然 FAIL。

第二次：

```text
1813 passed
+
direct adversarial probe
+
P1 contract aligned
```

才 PASS。

------

# 12. Known Limitations

当前 WP2-A 完成后仍有：

## 1. ToolRegistry 是 INTERNAL_RC

```text
ToolRegistry
ToolDescriptor
ToolRegistration
```

尚未：

```text
PUBLIC_STABLE
```

需要到 Stage 3.5 aggregate freeze 再判断。

## 2. AgentRouter.tools 兼容视图仍存在

只是：

```text
read-only compatibility projection
```

不是第二 Registry。

------

## 3. Coordinated Tool E2E 尚未完成

```text
HTTP / Coordinated Runtime
→ Tool execution
```

完整端到端留给 WP2-C。

------

## 4. Tool Schema 尚未平台化

当前没有：

```text
formal input schema
formal output schema
function calling schema
```

Adapter 仍负责具体 parsing。

------

## 5. Tool Permission 尚未实现

不存在：

```text
ToolPermission
ToolRiskLevel
ToolApprovalPolicy
allowed_tools
```

留给 WP2-B。

------

## 6. Dynamic Tool Registration 不支持

当前：

```text
STATIC STARTUP REGISTRATION
```

不是插件平台。

------

# 13. 这次体现的工程能力

## 13.1 Registry 和 Executor 职责分离

最重要的系统设计知识：

```text
Registry
≠
Executor
```

Registry 回答：

```text
What exists?
```

Executor 回答：

```text
How does it run?
```

如果把 Registry 放进 `ToolExecutionService`：

Service 就同时承担：

```text
Catalog
Resolution
Retry
Lease
Timeout
Side effect
Evidence
```

职责过重。

------

## 13.2 Descriptor 和 Registration 分离

最终不是：

```text
ToolDescriptor(
    name,
    description,
    callable,
    adapter
)
```

而是：

```text
ToolDescriptor
= metadata

ToolRegistration
= Descriptor + Adapter
```

原因：

> 描述信息和执行对象不是同一种生命周期的数据。

Descriptor 可以稳定、immutable。

Adapter 则可能包含 application-scoped state。

------

## 13.3 Static Metadata 和 Dynamic Execution Truth 分离

`ToolDescriptor` 没有放：

```text
side_effect_kind
idempotency
timeout
```

因为这些已经有 Owner：

```text
ToolExecutionSpec
```

而 `ComplexWorkflowToolAdapter.spec_for(invocation)` 甚至可以根据 invocation 动态变化。

这体现：

> **不要为了让 Descriptor“信息丰富”而复制 Runtime truth。**

------

## 13.4 Startup Mutation + Runtime Immutability

对于当前：

```text
single process
static production tools
```

根本没必要做：

```text
concurrent mutable registry
lock-heavy plugin system
```

更简单可靠的是：

```text
startup register
→ freeze
→ concurrent read
```

------

## 13.5 Fail Closed

关键异常：

```text
TOOL_REGISTRY_INVALID
TOOL_REGISTRY_DUPLICATE
TOOL_REGISTRY_FROZEN
TOOL_REGISTRY_NOT_FROZEN
TOOL_NOT_REGISTERED
```

全部明确 fail closed。

------

## 13.6 Composition Root 是平台能力落地点

最终 Registry 不是：

```text
module global singleton
```

而是：

```text
server.py::lifespan()
```

创建：

```text
ToolRegistry
→ population
→ freeze
→ inject
```

这与 WP1 已经建立的 Composition Root 原则一致。

------

# 14. 30 秒面试表达

在 LocalAgent 的 Tool 系统里，Stage 2 已经有比较完整的 ToolExecutionService，包括重试、超时、租约、副作用和 Evidence，但我后来审计发现“系统有哪些 Tool”仍然由 AgentRouter 里的 mutable dict 管理，而且 4 个生产 Tool 里还有 2 个直接调用函数，绕过 Runtime contract。

所以我在 WP2-A 增加了独立的 ToolRegistry、不可变 ToolDescriptor 和 ToolRegistration。Registry 在 Composition Root 启动阶段一次性注册后 freeze，运行期只读；AgentRouter只负责从 Registry resolve Tool，而 ToolExecutionService继续只负责执行。原来 `list_files` 和 `analyze_excel` 也通过现有 LegacyStringToolAdapter 迁入统一执行链。

最终 4 个 production Tool 全部通过 ToolExecutionService，重复注册从静默覆盖改成 fail closed，而且 Planner 的 Tool name/description 也只从 Registry派生。Final Gate还通过 Unicode control-character反例抓到一个测试全绿但合同未完全实现的问题，修复后最终1813个测试全部通过。

------

# 15. 2 分钟面试表达

这次主要解决的是 Tool execution 已经生产化，但 Tool catalog 仍然开发态的问题。

原来 Tool 注册是在 `AgentRouter.tools` 这个 mutable dict 里，结构类似 name、func、description、adapter。它既是 Tool清单，也是 planner prompt来源，还直接参与执行分发。更大的问题是当时 `get_system_status` 和 `complex_workflow_simulator` 已经经过 ToolAdapter 和 ToolExecutionService，但 `list_files`、`analyze_excel` 还直接调用函数，所以 ToolExecutionService并不是真正覆盖所有生产 Tool。

方案上我没有把 Registry 塞进 ToolExecutionService，因为 Service 已经负责 retry、lease、timeout、side effect 和 evidence，继续承担 discovery 会让职责混乱。我单独建立 application-scoped ToolRegistry，由 `server.py::lifespan()` 创建，startup注册所有 Tool 后 freeze，再注入 AgentRouter。

Registry value 又拆成 ToolDescriptor 和 ToolRegistration：Descriptor 只保存不可变的 name 和 description，Registration才绑定 application-scoped ToolAdapter。Side effect、idempotency、timeout这些没有复制进 Descriptor，因为 `ToolExecutionSpec`已经是唯一 Owner，而且像 complex workflow 的 `spec_for()` 还能根据 invocation动态变化。

两个 Legacy Tool则直接复用已有 LegacyStringToolAdapter，把旧的错误字符串映射成安全 Tool error，从而4个生产 Tool全部走 ToolExecutionService。

在 Final Gate里还有一个比较典型的问题：1810个测试全绿，但 Codex用 U+007F 和 U+0085做直接反例，发现 Descriptor只检查了 `ord < 32`，没有真正实现“禁止所有Unicode control characters”的冻结合同，所以Gate直接FAIL。后来改成 Unicode category `Cc`并补回归，最终1813个测试和Re-Gate全部通过。

------

# 16. 深入版本

可以把 WP2-A 看成三个层次。

## 第一层：Catalog Plane

```text
ToolRegistry
ToolDescriptor
ToolRegistration
```

回答：

```text
有哪些 Tool？
叫什么？
描述是什么？
对应哪个 Adapter？
```

------

## 第二层：Routing Plane

```text
AgentRouter
```

回答：

```text
模型选中了哪个 Tool？
这个 Tool 是否真的存在？
拿到哪个 Registration？
```

这里还区分：

```text
untrusted planner output
→ resolve

trusted internal expectation
→ require
```

------

## 第三层：Execution Plane

```text
ToolExecutionService
```

回答：

```text
如何执行？
是否重试？
怎么拿 Lease？
timeout是多少？
side effect状态是什么？
怎么生成 Evidence？
```

完整职责链：

```text
Catalog
   ↓
Resolution
   ↓
Execution
```

而不是：

```text
一个 AgentRouter 全部负责
```

也不是：

```text
一个 ToolExecutionService 全部负责
```

这是 WP2-A 最核心的架构演进。

------

# 17. 高频追问与参考答案

## Q1：为什么一定要 ToolRegistry？一个 dict 不够吗？

dict 本身不是问题。

问题是：

```text
谁拥有它？
什么时候允许写？
重复怎么处理？
部分构造时能不能读？
是不是唯一事实源？
```

原来的 dict：

```text
mutable
no duplicate protection
AgentRouter-owned
runtime technically writable
```

而 Registry 把这些行为变成了显式 contract。

------

## Q2：为什么 ToolRegistry 不放到 ToolExecutionService？

因为 ToolExecutionService 是 execution owner，不是 catalog owner。

它已经负责：

```text
retry
lease
timeout
cancellation
side effect
evidence
```

如果再放 Registry：

```text
discovery + execution
```

就耦合了。

------

## Q3：为什么 Descriptor 不直接存 Adapter？

项目其实把两者拆成：

```text
ToolDescriptor
ToolRegistration
```

Descriptor 是纯静态 metadata。

Registration 是：

```text
Descriptor + ToolAdapter
```

这样描述与执行 binding 的职责更清晰。

------

## Q4：为什么 Descriptor 只有 name 和 description？

因为当前真正有消费者的静态事实只有：

```text
name
description
```

输入 schema、output schema、version、tag 等都没有当前需求。

生产化不是字段越多越好。

------

## Q5：为什么 side_effect_kind 不放 Descriptor？

因为真正执行时的 side effect可能取决于 invocation。

比如：

```text
ComplexWorkflowToolAdapter.spec_for()
```

会根据 execution mode动态生成 spec。

所以静态 Descriptor无法正确表达某一次调用的副作用事实。

------

## Q6：为什么 Registry freeze？

因为 production Tool集合当前只在 startup确定。

既然没有动态 plugin需求，就可以：

```text
startup mutation
runtime immutable
```

这样并发执行期间不存在 mutation race。

------

## Q7：为什么 freeze 之前连 resolve 都不允许？

因为否则：

```text
Registry 注册到一半
```

就可能被 Runtime读取。

比如：

```text
4个 Tool只注册了2个
```

此时系统会错误地认为：

```text
当前就只有2个 Tool
```

所以 partial construction不能成为 observable state。

------

## Q8：duplicate为什么不能 last-write-wins？

因为 Tool name 是 identity。

如果同名 Tool可以覆盖：

```text
identity没变
implementation却变了
```

这是典型的 silent configuration corruption。

------

## Q9：resolve和require为什么要分开？

因为 trust boundary不同。

模型 hallucination 一个 Tool：

```text
resolve → None
```

是合理的不可信输入。

内部代码明确要求一个不存在的 Tool：

```text
require → TOOL_NOT_REGISTERED
```

这是内部合同违约。

------

## Q10：为什么 AgentRegistry 和 ToolRegistry不能合并？

因为：

```text
Agent identity/capability
```

和：

```text
Tool identity/binding
```

不是同一个领域。

更重要的是：

```text
Tool exists
```

不等于：

```text
Agent may use Tool
```

Permission属于下一阶段。

------

## Q11：list_files明明只是一个函数，为什么还要 Adapter？

因为 Adapter 是进入统一 Runtime contract的 boundary。

包上 Adapter后可以获得：

```text
typed invocation/result/error
timeout
lease
evidence
Runtime events
```

业务函数本身不需要重写。

------

## Q12：为什么 Legacy Tool错误字符串要特殊处理？

旧函数会：

```text
return "List files failed: ..."
```

如果直接当成功字符串：

模型会看到一个“成功 Tool observation”。

Adapter通过 error-prefix把它转成真正 Tool error。

------

## Q13：为什么不直接重写 list_files/analyze_excel？

因为问题不是业务实现。

问题是执行边界。

所以：

```text
preserve business function
wrap runtime boundary
```

风险更小。

------

## Q14：为什么 AgentRouter.tools还没完全删除？

兼容性。

但它已经不是：

```text
mutable authority
```

而只是：

```text
read-only projection
```

唯一事实源仍是 Registry。

------

## Q15：这次最有价值的 Bad Case是什么？

推荐回答 Descriptor control-character P1。

因为：

```text
1810 tests all green
```

但直接构造：

```text
U+007F
U+0085
```

就发现实现小于冻结合同。

这个案例能体现：

```text
contract-driven verification
adversarial testing
test oracle quality
```

------

# 18. 容易答错或夸大的问题

## 错误 1

“现在已经是完整 Tool Platform。”

错误。

只完成：

```text
Registry / Descriptor
all-current-production-Tool adapter execution
```

------

## 错误 2

“ToolDescriptor有完整Schema。”

错误。

只有：

```text
name
description
```

------

## 错误 3

“支持动态 Tool 插件。”

错误。

当前：

```text
STATIC STARTUP REGISTRATION
```

------

## 错误 4

“支持 Agent级 Tool权限。”

错误。

WP2-B 尚未完成。

------

## 错误 5

“Agent capability决定能用哪些Tool。”

错误。

当前没有 capability → Tool authorization mapping。

------

## 错误 6

“模型使用 function calling 调 Tool。”

错误。

当前仍然是：

```text
text planner prompt
CALL: tool_name(...)
```

不是 OpenAI-style structured tool calling。

------

## 错误 7

“ToolExecutionService负责Tool查找。”

错误。

Tool resolution仍然归：

```text
AgentRouter
```

Service只拿 resolved adapter。

------

## 错误 8

“ToolDescriptor是PUBLIC_STABLE。”

错误。

当前：

```text
INTERNAL_RC
```

------

## 错误 9

“Coordinated Runtime完整Tool E2E已经覆盖。”

错误。

留给 WP2-C。

------

## 错误 10

“Final Gate一次就通过。”

错误。

第一次：

```text
1810 passed
P1=1
Gate FAIL
```

修复 Unicode Cc 后：

```text
1813 passed
Re-Gate PASS
```

------

# 19. 重点复习知识点

# P0：必须熟练

## 1. Registry vs Executor

```text
ToolRegistry
→ what exists

ToolExecutionService
→ how execution happens
```

------

## 2. Descriptor vs Registration

```text
Descriptor
= metadata

Registration
= metadata + execution binding
```

------

## 3. Startup Freeze

```text
construct
register
validate
freeze
publish
```

以及为什么 partial Registry不能被读。

------

## 4. Duplicate fail-closed

```text
identity collision
!= overwrite
```

------

## 5. resolve vs require

必须会讲 trust boundary。

------

## 6. 4/4 Tool Execution Unification

修改前：

```text
2 Runtime
2 Legacy
```

修改后：

```text
4 Runtime
0 Legacy direct-call
```

------

## 7. ToolExecutionSpec Single Source of Truth

不要把：

```text
side effect
idempotency
timeout
concurrency
```

复制进 Descriptor。

------

## 8. Unicode Final Gate Bad Case

这是本 WP 最值得面试讲的真实工程问题。

------

# P1：建议掌握

## 9. Immutable Catalog

为什么 frozen Descriptor + frozen Registration适合 concurrent read。

------

## 10. Stateful Adapter Identity

为什么 `complex_workflow_simulator` lookup不能 clone Adapter。

------

## 11. Compatibility View

如何：

```text
保留旧接口
但不保留旧 Authority
```

------

## 12. Legacy Adapter Migration

业务函数不改，只迁执行边界。

------

## 13. Error Taxonomy

记住五个：

```text
TOOL_REGISTRY_INVALID
TOOL_REGISTRY_DUPLICATE
TOOL_REGISTRY_FROZEN
TOOL_REGISTRY_NOT_FROZEN
TOOL_NOT_REGISTERED
```

------

## 14. Composition Root

ToolRegistry在哪里创建、freeze、inject。

------

## 15. Test Oracle

为什么：

```text
1810 green
```

仍然不等于 contract PASS。

------

# P2：了解

## 16. Dynamic plugin Registry

本项目未实现。

如果未来做，需要重新考虑：

```text
concurrent mutation
lifecycle
version
permission
unload
resource close
```

------

## 17. Structured Tool Schema

本项目当前没有。

不能把 Descriptor自动等同于 function calling schema。

------

## 18. External Discovery

当前没有：

```text
/api/tools
remote Tool discovery
```

------

# 20. 最终面试速查表

| 项目                                | 当前事实                     |
| ----------------------------------- | ---------------------------- |
| WP                                  | WP2-A                        |
| 名称                                | Tool Registry / Descriptor   |
| Final Gate                          | PASS                         |
| P0                                  | 0                            |
| P1                                  | 0                            |
| P2                                  | 0                            |
| TEST_GAP                            | 1                            |
| Production Tool count               | 4                            |
| Tool Registry Owner                 | `ToolRegistry`               |
| Scope                               | application / process-local  |
| Registration                        | static startup               |
| Runtime mutation                    | NOT_SUPPORTED                |
| Freeze                              | YES                          |
| Tool identity                       | case-sensitive string name   |
| Name regex                          | `^[a-z][a-z0-9_]{0,63}$`     |
| Duplicate                           | fail closed                  |
| Duplicate code                      | `TOOL_REGISTRY_DUPLICATE`    |
| Unknown internal                    | `TOOL_NOT_REGISTERED`        |
| Read before freeze                  | `TOOL_REGISTRY_NOT_FROZEN`   |
| Mutation after freeze               | `TOOL_REGISTRY_FROZEN`       |
| Invalid Descriptor                  | `TOOL_REGISTRY_INVALID`      |
| Descriptor fields                   | name + description           |
| Descriptor                          | immutable                    |
| Registration                        | Descriptor + ToolAdapter     |
| Registration                        | immutable                    |
| ToolExecutionSpec duplicated?       | NO                           |
| Tool resolution Owner               | AgentRouter                  |
| Tool execution Owner                | ToolExecutionService         |
| ToolExecutionService resolves Tool? | NO                           |
| Tool Adapter coverage               | 4 / 4                        |
| Legacy direct execution             | removed                      |
| list_files                          | LegacyStringToolAdapter      |
| analyze_excel                       | LegacyStringToolAdapter      |
| get_system_status                   | LegacyStringToolAdapter      |
| complex workflow                    | ComplexWorkflowToolAdapter   |
| Planner Tool description            | Registry Descriptor          |
| AgentRegistry merged?               | NO                           |
| Agent→Tool permission               | NOT_IMPLEMENTED              |
| Tool Risk                           | NOT_IMPLEMENTED              |
| Tool Approval                       | NOT_IMPLEMENTED              |
| Dynamic Registration                | NOT_IMPLEMENTED              |
| Function Calling                    | NOT_IMPLEMENTED              |
| External discovery API              | NOT_IMPLEMENTED              |
| Contract classification             | INTERNAL_RC                  |
| Registry targeted                   | 41 passed                    |
| WP2-A targeted                      | 131 passed                   |
| Tool Runtime                        | 239 passed                   |
| WP1 regression                      | 134 passed                   |
| Critical Runtime                    | 101 passed                   |
| Full suite                          | 1813 passed                  |
| Subtests                            | 42 passed                    |
| Remaining Test Gap                  | Coordinated Tool E2E → WP2-C |

## 最值得记住的一句话

> **WP2-A 的核心不是“新增了一个 ToolRegistry 类”，而是把 Tool catalog、Tool resolution 和 Tool execution 三种职责彻底拆开：Registry 成为“有哪些 Tool”的唯一事实源，AgentRouter只负责解析与编排，ToolExecutionService只负责执行；同时通过 Adapter 把原来的两个 Legacy Tool 收进同一 Runtime contract，最终真正做到 4/4 production Tool 统一执行。**

## 最值得面试讲的工程案例

> **第一次 Final Gate 时全量 1810 tests 全绿，但对抗反例发现 `ToolDescriptor` 只拦截 ASCII C0 控制字符，U+007F DEL 和 U+0085 NEL 仍能进入 Planner Descriptor，因此 Gate 直接以 P1 FAIL。修复为 Unicode `Cc` 分类并补 durable regression 后，1813 tests 全绿且 Final Re-Gate PASS。这个案例说明生产级合同不能只依赖 happy-path test count，而要验证“实现是否真正覆盖合同定义的整个语义集合”。**