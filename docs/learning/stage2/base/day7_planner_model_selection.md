# 阶段二第 7 天：Planner（规划器）与 Model Selection Policy（模型选择策略）

**当前进度：第 7/25 天。**

前六天已经建立：

```text
RunContext
→ 描述运行环境

AgentState
→ 保存执行事实

AgentLoop
→ 推进执行周期

State Machine
→ 约束生命周期转移

ContextBuilder
→ 构造模型输入并输出上下文能力特征
```

今天开始解决两个问题：

```text
这个任务应该拆成哪些步骤？
        ↓
这些步骤需要什么模型能力？
        ↓
本次模型调用优先使用本地轻量模型还是远程高级模型？
```

今天会首次建立轻重模型选择策略，但暂不实现：

- Retry（重试）；
- Fallback（降级）；
- Circuit Breaker（熔断器）；
- 模型健康检查；
- Budget（预算）参与选择；
- Deadline（截止时间）参与选择；
- Scheduler（调度器）执行计划；
- 并行 PlanStep。

------

# 一、当天目标

今天必须完成：

1. 区分 Planner、Plan、PlanStep、AgentAction 和 Scheduler。
2. 建立结构化、不可变的 Plan。
3. 明确计划中应该保存什么，不应该保存什么。
4. 定义 Task Capability Requirements（任务能力需求）。
5. 定义本地轻量模型和远程高级模型的抽象 Model Profile（模型档案）。
6. 建立确定性的规则型 Model Selection Policy。
7. 使用第 6 天的 `ModelContextRequirements` 参与模型选择。
8. 区分：
   - 用户偏好；
   - 硬能力约束；
   - 任务复杂度；
   - 模型默认选择。
9. 为每次选择返回可解释 Reason Code（原因码）。
10. 最小接入一条真实模型调用路径。
11. 不把模型名称和选择逻辑写进 Agent Loop。
12. 增加至少 3 个高价值 Bad Case。

------

# 二、Planner 在当前架构中的位置

完整目标链路：

```text
用户请求
    ↓
Router
判断任务类型和目标 Agent
    ↓
Planner
生成结构化 Plan 和能力需求
    ↓
ContextBuilder
构造实际模型输入并统计上下文需求
    ↓
ModelSelectionPolicy
选择首选 Model Profile
    ↓
Model Resolver / 现有模型适配层
解析到本地或远程模型
    ↓
模型调用
```

当前第 7 天暂时不会让 Plan 驱动 Scheduler。

因此今天的实际关系是：

```text
Planner
→ 生成并校验 Plan
→ 为当前模型调用提供 TaskCapabilityRequirements

AgentLoop
→ 仍执行 legacy AgentRouter Action
```

第 8 天 Scheduler 才会真正读取 Plan 并选择 Ready Step（就绪步骤）。

------

# 三、Planner、Agent Loop 和 Scheduler 的区别

## 1. Planner

Planner 回答：

> 为完成任务，需要执行哪些步骤，每个步骤需要什么能力？

例如：

```text
步骤 1：检索 LocalAgent Runtime 相关代码
步骤 2：分析状态转移风险
步骤 3：给出修改方案
步骤 4：生成最终回答
```

Planner 不执行模型、Tool 或 RAG。

------

## 2. Agent Loop

Agent Loop 回答：

> 执行周期如何持续推进，以及何时终止？

见第 4 天。

------

## 3. Scheduler

Scheduler 回答：

> 当前 Plan 中哪些 Step 已满足依赖，可以开始执行？

属于第 8 天。

------

## 4. AgentAction

AgentAction 是当前一轮真正准备执行的动作：

```text
CALL_MODEL
CALL_TOOL
RETRIEVE
DELEGATE_AGENT
```

PlanStep 是计划中的逻辑步骤，AgentAction 是 Runtime 某轮真正执行的动作。

二者不是同一个概念。

------

# 四、结构化 Plan 应保存什么

建议建立：

```python
@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    version: int
    task_summary: str
    steps: tuple[PlanStep, ...]
    created_at: datetime
    source: PlanSource
```

## Plan 字段说明

### plan_id

标识本次计划。

不必等于 `run_id`，因为未来一个 Run 可能 Replan（重新规划）多次。

### version

初始为：

```text
1
```

Replan 后可以：

```text
2、3、4……
```

必须拒绝：

- `bool`；
- 零；
- 负数；
- 非整数。

### task_summary

使用简短中文描述当前目标，例如：

```text
分析用户提供的 Python 代码并给出修改建议
```

它是自然语言数据，不应包含完整用户输入或敏感内容。

### steps

不可变的 `PlanStep` 列表。

### source

区分计划来源：

```python
class PlanSource(str, Enum):
    DETERMINISTIC = "deterministic"
    LEGACY_ADAPTER = "legacy_adapter"
    MODEL_GENERATED = "model_generated"
```

今天尽量复用已有 Router / Planner 结果，不额外新增一次大模型规划调用。

------

# 五、PlanStep 设计

建议：

```python
@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    title: str
    description: str
    depends_on: tuple[str, ...]
    completion_criteria: str
    preferred_agent: str | None
    capability_requirements: TaskCapabilityRequirements
```

## 1. title

简短中文标题：

```text
检索知识库
分析代码风险
生成最终答案
```

## 2. description

说明应该完成什么，不写具体执行代码。

## 3. depends_on

表达逻辑依赖。

今天只做基本校验：

- Step ID 唯一；
- 依赖存在；
- 不能依赖自己；
- 依赖不能重复。

今天不做：

- 拓扑排序；
- 环检测；
- 不可达节点检查。

这些属于第 9 天 DAG（有向无环图）。

## 4. completion_criteria

用中文明确步骤完成条件，例如：

```text
已返回至少一个包含来源标识的有效检索结果
```

不能只写：

```text
完成
成功
处理完毕
```

## 5. preferred_agent

例如：

```text
knowledge_expert
code_expert
data_analyst
```

这是 Agent 偏好，不是模型选择。

## 6. capability_requirements

表达这个 Step 需要什么能力，不直接选择具体模型。

------

# 六、Plan 中不能保存执行状态

错误设计：

```python
@dataclass
class PlanStep:
    status: StepStatus
    started_at: datetime
    error_message: str
```

因为执行状态已经由：

```text
AgentState / StepState
```

统一管理。

如果 Plan 和 AgentState 都保存状态，就会出现：

```text
PlanStep.status = PENDING
StepState.status = RUNNING
```

两个事实源互相冲突。

正确边界：

```text
Plan
→ 任务应该怎样执行

AgentState
→ 当前实际执行到哪里
```

Plan 今天应保持不可变。

------

# 七、任务能力需求

模型选择不应该只看：

```text
问题长度
是否包含代码
是否使用 RAG
```

建议建立：

```python
@dataclass(frozen=True, slots=True)
class TaskCapabilityRequirements:
    requires_planning: bool = False
    requires_tools: bool = False
    requires_rag: bool = False
    requires_multi_agent: bool = False
    requires_code_reasoning: bool = False
    requires_structured_output: bool = False
    requires_long_reasoning: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    estimated_steps: int = 1
```

## RiskLevel

```python
class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
```

高风险不代表只要换远程模型就安全。

后续仍可能需要：

- Human Approval（人工审批）；
- Guardrail（护栏）；
- Tool 权限；
- Sandbox（沙箱）。

今天高风险只作为模型能力需求特征。

------

# 八、能力需求从哪里来

能力需求可以来自：

1. Router 已识别的任务类型；
2. 结构化 Plan；
3. 当前 Agent 类型；
4. ContextBuilder 的上下文特征；
5. 用户显式要求。

例如：

```text
普通知识问答
→ requires_planning = False
→ estimated_steps = 1
→ risk_level = LOW
复杂代码架构审查
→ requires_code_reasoning = True
→ requires_long_reasoning = True
→ estimated_steps = 4
多 Agent 数据分析
→ requires_tools = True
→ requires_multi_agent = True
→ estimated_steps = 5
```

不能把用户输入中的：

```text
“请使用高级模型”
```

直接解析成可信模型选择，除非它来自明确的用户偏好入口。

自然语言内容和系统级 User Preference（用户偏好）需要分开。

------

# 九、Model Profile（模型档案）

Planner 不应该认识：

- `deepseek-chat`；
- `qwen2.5`；
- URL；
- API Key；
- 本地 GGUF 路径。

Planner 只表达能力。

模型层可以建立抽象 Profile：

```python
class ModelProfileId(str, Enum):
    LOCAL_FAST = "local_fast"
    REMOTE_ADVANCED = "remote_advanced"
```

建议结构：

```python
@dataclass(frozen=True, slots=True)
class ModelProfile:
    profile_id: ModelProfileId
    context_window: int
    max_output_tokens: int
    supports_tools: bool
    supports_structured_output: bool
    supports_code_reasoning: bool
    supports_long_reasoning: bool
    quality_tier: int
    latency_tier: int
```

## 不直接写死 Qwen 和 DeepSeek

Profile 与真实模型的映射放在配置或 Resolver 中：

```text
LOCAL_FAST
→ 当前配置的本地模型

REMOTE_ADVANCED
→ 当前配置的远程高级模型
```

至于当前具体是：

- Qwen 本地、DeepSeek 远程；
- Qwen 远程、DeepSeek 远程；
- 其他模型；

由现有 Settings 和模型适配层决定。

策略层不能写：

```python
if complex_task:
    return "deepseek"
```

------

# 十、User Model Preference（用户模型偏好）

建议定义：

```python
class ModelPreference(str, Enum):
    AUTO = "auto"
    FORCE_LOCAL = "force_local"
    FORCE_REMOTE = "force_remote"
```

今天不修改聊天 API，可以先在内部 Request 中支持。

## AUTO

由策略自动选择。

## FORCE_LOCAL

用户明确要求仅使用本地模型。

这通常可能涉及：

- 隐私；
- 成本；
- 离线环境；
- 数据不得外传。

因此本地模型能力不足时，不能静默切换远程。

正确行为：

```text
用户 FORCE_LOCAL
+ 本地能力不足
→ 明确拒绝或返回 ModelSelectionError
```

不能：

```text
静默调用远程模型
```

## FORCE_REMOTE

用户明确要求高级远程模型。

如果远程模型当前配置不存在，今天明确失败，不做 Fallback。

------

# 十一、Model Selection Request

建议：

```python
@dataclass(frozen=True, slots=True)
class ModelSelectionRequest:
    agent_id: str
    capability_requirements: TaskCapabilityRequirements
    context_requirements: ModelContextRequirements
    preference: ModelPreference
    available_profiles: tuple[ModelProfile, ...]
```

不要包含：

- 完整用户输入；
- 完整 Prompt；
- RAG 正文；
- Tool 参数；
- API Key；
- 内部路径。

模型选择只需要结构化特征。

------

# 十二、Model Selection Decision

建议：

```python
@dataclass(frozen=True, slots=True)
class ModelSelectionDecision:
    selected_profile: ModelProfileId
    reason_code: ModelSelectionReason
    reason_text: str
    matched_rules: tuple[str, ...]
    fallback_allowed: bool
```

其中 `reason_text` 使用中文，便于：

- 日志排查；
- 测试失败阅读；
- DeepSeek / Qwen 处理；
- 后续 Trace 展示。

## Reason Code

建议：

```python
class ModelSelectionReason(str, Enum):
    USER_FORCED_LOCAL = "user_forced_local"
    USER_FORCED_REMOTE = "user_forced_remote"
    LOCAL_SUFFICIENT = "local_sufficient"
    CONTEXT_WINDOW_REQUIRED = "context_window_required"
    TOOL_CAPABILITY_REQUIRED = "tool_capability_required"
    STRUCTURED_OUTPUT_REQUIRED = "structured_output_required"
    CODE_REASONING_REQUIRED = "code_reasoning_required"
    LONG_REASONING_REQUIRED = "long_reasoning_required"
    MULTI_STEP_PLAN = "multi_step_plan"
    MULTI_AGENT_REQUIRED = "multi_agent_required"
    HIGH_RISK_TASK = "high_risk_task"
```

Reason Code 保持英文稳定标识。

Reason Text 使用中文，例如：

```text
当前任务需要多步骤规划和代码推理，本地轻量模型能力不足，选择远程高级模型。
```

------

# 十三、模型选择规则优先级

模型路由必须确定性执行。

建议顺序如下。

## 规则 1：校验 Profile

先检查：

- `profile_id` 唯一；
- context window 合法；
- 至少存在一个 Profile；
- FORCE_LOCAL 时存在本地 Profile；
- FORCE_REMOTE 时存在远程 Profile。

------

## 规则 2：用户强制偏好

### FORCE_LOCAL

本地模型满足硬需求：

```text
→ 选择 LOCAL_FAST
```

本地模型不满足：

```text
→ ModelSelectionError
```

不允许静默远程升级。

### FORCE_REMOTE

存在远程高级模型：

```text
→ 选择 REMOTE_ADVANCED
```

不存在：

```text
→ ModelSelectionError
```

------

## 规则 3：上下文窗口硬约束

使用第 6 天：

```text
minimum_context_window
```

与 Model Profile 的可用窗口比较。

由于 Token 是近似估算，需要安全余量。

例如定义：

```text
usable_context_window
=
context_window × safety_ratio
```

`safety_ratio` 应配置化，默认值需要 Codex 根据现有配置给出，并在文档说明。

不能只比较：

```text
minimum_context_window <= context_window
```

因为还存在 Chat Template 和 Token 估算误差。

如果本地窗口不足、远程窗口足够：

```text
→ REMOTE_ADVANCED
→ CONTEXT_WINDOW_REQUIRED
```

如果所有 Profile 都不足：

```text
→ ModelSelectionError
```

------

## 规则 4：硬能力约束

例如：

```text
requires_tools = True
```

则选择必须满足：

```text
supports_tools = True
```

同理：

- structured output；
- code reasoning；
- long reasoning。

Profile 不满足硬能力时不能选择。

------

## 规则 5：任务复杂度

如果以下条件成立，可优先远程高级模型：

```text
requires_multi_agent = True
estimated_steps >= 阈值
requires_long_reasoning = True
risk_level = HIGH
```

注意：

```text
requires_rag = True
```

不能单独作为远程模型条件。

简单 RAG 问答完全可以由本地模型处理。

同样：

```text
contains_code = True
```

也不能单独强制远程。

简单语法问题可能本地模型足够。

------

## 规则 6：默认本地

如果：

- 单步骤；
- 上下文较短；
- 无特殊硬能力；
- 风险较低；
- 本地 Profile 满足要求；

则：

```text
→ LOCAL_FAST
→ LOCAL_SUFFICIENT
```

这才实现：

> 轻量任务优先本地快速响应。

------

# 十四、模型选择不是 Fallback

今天只处理：

```text
任务开始前
→ 选择首选模型
```

不处理：

```text
首选模型调用失败
→ 切换备用模型
```

后者属于第 14 天。

今天的 `fallback_allowed` 只是一项策略提示，不执行实际降级。

例如：

```text
用户 FORCE_LOCAL
→ fallback_allowed = False
AUTO 且普通知识问答
→ 可以记录 fallback_allowed = True
```

但实际切换链路今天不能实现。

------

# 十五、按 Run 选择还是按 Step 选择

最终架构应该支持：

```text
Router 分类
→ 本地轻量模型

Planner 规划
→ 远程高级模型

简单 Tool 参数提取
→ 本地轻量模型

复杂最终总结
→ 远程高级模型
```

因此 Model Selection 应面向：

> 一次模型调用或一个 PlanStep。

不应在 Run 开始时选择一次，然后整个 Run 永远锁定同一模型。

今天即使只接入一条模型调用路径，API 设计也必须支持未来每个 Step 独立调用：

```python
policy.select(request)
```

------

# 十六、Planner 的最小落地策略

今天不建议新增一次 LLM 规划请求。

Codex 应先检查现有 AgentRouter 中是否已有：

- 任务分解；
- delegations；
- steps；
- tool plan；
- agent assignment。

优先使用：

```text
Legacy Planner Adapter
```

将已有结构转换成新的 `Plan`。

如果当前路径没有显式计划，可以使用确定性单步 Plan：

```text
Plan
└── Step 1：由 knowledge_expert 回答当前知识问题
```

但不能为了展示 Planner，强行把简单任务拆成多个伪步骤。

------

# 十七、PlanValidator 的边界

今天至少校验：

- plan_id 非空；
- version 合法；
- task_summary 非空；
- 至少一个 Step；
- step_id 唯一；
  -依赖 ID 存在；
- 不允许自依赖；
- 同一个依赖不重复；
- completion criteria 非空；
- estimated_steps 合法；
- 时间为 UTC aware。

今天不做：

- 环检测；
- 拓扑排序；
- Ready Step；
- 依赖完成判断；
- Blocked 传播。

------

# 十八、LocalAgent 最小集成建议

## 建议新增

```text
core/runtime/planning.py
core/runtime/model_selection.py
tests/test_planning.py
tests/test_model_selection.py
docs/learning/stage2/day07_planner_model_selection_result.md
```

如项目风格适合，也可合并文件，但不要把所有内容塞进 `agent_router.py`。

------

## 最小真实接入路径

优先使用第 6 天已经迁移的知识专家路径。

建议流程：

```text
知识专家收到任务
→ 构建或适配最小 Plan
→ 从 PlanStep 得到 TaskCapabilityRequirements
→ ContextBuilder 得到 ModelContextRequirements
→ ModelSelectionPolicy.select()
→ 得到 LOCAL_FAST / REMOTE_ADVANCED
→ Model Resolver 解析成现有模型客户端
→ 执行原有模型调用
```

今天不得实现调用失败后的切换。

------

## 模型 Resolver

如果项目当前已有：

- 本地模型实例；
- 远程 OpenAI-compatible Client；
- Provider 配置；

可以建立最小映射：

```text
ModelProfileId
→ 现有模型执行对象
```

Resolver 不做复杂度判断。

Selection Policy 不创建网络客户端。

------

# 十九、第 7 天高价值 Bad Case

## Bad Case 1：Plan 和 AgentState 同时保存 Step 状态

- **类型：假设构造**

### 触发条件

`PlanStep` 中保存：

```text
status = PENDING
```

AgentState 中对应 Step 已经：

```text
status = RUNNING
```

### 故障表现

Planner、Scheduler 和 Runtime 读取到不同执行状态：

- Scheduler 可能重复调度；
- UI 显示错误状态；
- Resume 无法判断可信来源。

### 根因分析

混淆了：

```text
计划定义
执行事实
```

### 修复方案

- Plan 不保存运行状态；
- Plan 保持不可变；
- 所有执行状态只存 AgentState；
- Scheduler 将来通过 `plan + AgentState` 计算 Ready Step。

### 回归测试

- 验证 `PlanStep` 不包含 `status`、`started_at`、`error_message`；
- Plan 创建后不可修改；
- AgentState 状态变化不影响 Plan 内容。

### 对应知识点

- Single Source of Truth（单一事实来源）；
- Immutable Plan（不可变计划）；
- Plan / Runtime State 分离。

### 面试表达

> 我没有把执行状态放进 PlanStep，因为 AgentState 已经是 Runtime 执行事实的唯一来源。Plan 只描述应该做什么，状态只描述实际执行到哪里，避免 Scheduler 因双重状态源重复调度。

------

## Bad Case 2：用户问题很短，却错误选择本地小模型

- **类型：假设构造**

### 触发条件

用户只输入：

```text
为什么？
```

但当前调用还包含：

- 长 History；
- Memory；
- 多个 RAG Chunk；
- 复杂代码上下文。

策略只根据：

```python
len(user_query)
```

选择本地模型。

### 故障表现

- 本地模型上下文窗口不足；
- Prompt 被继续裁剪；
- 回答缺少关键历史；
- 请求直接被模型服务拒绝。

### 根因分析

模型选择使用用户输入长度，而不是第 6 天完整的 `ModelContextRequirements`。

### 修复方案

选择必须使用：

```text
minimum_context_window
estimated_input_tokens
was_truncated
requires_long_context
```

并为近似 Token 预留安全余量。

### 回归测试

- 构造三字用户请求；
- 完整 messages 超过本地可用窗口；
- 远程窗口足够；
- 必须选择 `REMOTE_ADVANCED`；
- Reason Code 为 `CONTEXT_WINDOW_REQUIRED`。

### 对应知识点

- Context-aware Routing（上下文感知路由）；
- 模型窗口；
- Token 预算；
- 规则优先级。

### 面试表达

> 我没有用问题长度判断模型复杂度，而是复用 Context Builder 对完整 System、History、RAG 和用户请求的估算。当短问题依赖长上下文时，策略会因为上下文窗口硬约束选择远程模型。

------

## Bad Case 3：用户强制本地，却被静默升级远程

- **类型：假设构造**

### 触发条件

用户因为隐私要求：

```text
仅使用本地模型
```

但任务需要更长上下文或更强能力。

策略发现本地不足后自动调用远程模型。

### 故障表现

- 数据可能离开本地环境；
- 用户成本预期被破坏；
- 隐私约束被绕过；
- 日志显示本地，实际调用远程。

### 根因分析

把用户偏好当作普通软建议，而不是执行约束。

### 修复方案

```text
FORCE_LOCAL
+ 本地能力不足
→ ModelSelectionError
```

不得静默升级。

### 回归测试

- FORCE_LOCAL；
- 本地窗口不足；
- 远程窗口满足；
- 仍必须明确失败；
- Resolver 不得被调用；
- Decision 不得伪造本地成功。

### 对应知识点

- User Constraint（用户约束）；
- Privacy Boundary（隐私边界）；
- Fail Closed（安全失败）；
- Policy Enforcement（策略执行）。

### 面试表达

> 用户强制本地通常不只是性能偏好，也可能是隐私约束。因此本地能力不足时我选择明确失败，而不是静默升级远程模型，避免数据边界被策略层绕过。

------

## Bad Case 4：Planner 直接指定具体模型

- **类型：假设构造**

### 触发条件

PlanStep 直接保存：

```text
model = deepseek
```

后续用户要求本地、Budget 不足或模型配置变更。

### 故障表现

- Planner 与模型配置强耦合；
- 用户偏好无法覆盖；
- 测试环境难以替换模型；
- 模型改名需要修改计划数据；
- Budget 和健康状态无法参与决策。

### 根因分析

Planner 越权做了 Model Selection Policy 的工作。

### 修复方案

PlanStep 只保存：

```text
capability_requirements
```

Model Selection Policy 再结合：

- Context；
- User Preference；
- Model Profile；

选择模型。

### 回归测试

- `PlanStep` 不包含 provider、model_name、base_url；
- 同一个 Plan 在不同 Profile 配置下可以得到不同合法 Decision；
- Planner 不依赖模型客户端。

### 对应知识点

- Policy / Mechanism Separation（策略与机制分离）；
- Dependency Inversion（依赖倒置）；
- 可配置架构。

### 面试表达

> Planner 只表达任务能力需求，不直接写死 DeepSeek 或 Qwen。这样同一份 Plan 可以在不同环境下由策略层映射为本地或远程模型，也方便后续 Budget、Deadline 和熔断参与选择。

------

# 二十、测试方案

## Plan 数据模型

- 空 Plan ID 被拒绝；
- version 拒绝 bool、零、负数；
- task summary 为空被拒绝；
- 无 Step 被拒绝；
- Step ID 重复被拒绝；
- Step 自依赖被拒绝；
- 缺失依赖被拒绝；
- 重复依赖被拒绝；
- naive datetime 被拒绝；
- Plan 和 Step 不可变；
- PlanStep 不含 Runtime 状态。

## Capability Requirements

- bool 字段类型正确；
- estimated steps 拒绝 bool、零、负数；
- RiskLevel 合法；
- 简单任务能力映射；
- RAG 不单独强制远程；
- contains code 不单独强制远程。

## Model Profile

- Profile ID 唯一；
- context window 合法；
- max output 合法；
- Capability 字段合法；
- 不暴露 Secret；
- 不把具体模型名写进 Policy。

## Model Selection

- 简单单步任务选择本地；
- 长上下文选择远程；
- 本地不满足 Tool 能力时选择远程；
- 多 Agent / 长推理选择远程；
- 简单 RAG 可以选择本地；
- 简单代码问题可以选择本地；
- FORCE_LOCAL 成功；
- FORCE_LOCAL 能力不足明确失败；
- FORCE_REMOTE 成功；
- FORCE_REMOTE 不存在明确失败；
- 所有 Profile 都不满足时明确失败；
- 相同输入结果稳定；
- Reason Code 和 Reason Text 正确；
- 不执行 Fallback。

## 集成

- 知识专家获得 Context Requirements；
- Model Selection 只收到结构化特征；
- 用户请求正文不会进入 Selection Request；
- 本地选择调用本地现有模型入口；
- 远程选择调用远程现有模型入口；
- 每次只调用一个首选模型；
- 失败不会切换另一模型；
- 原有 messages 结构和流式输出保持不变；
- Runtime 测试继续通过。

------

# 二十一、Codex 实操提示词

下面的提示词已加入你要求的代码语言规范。

你正在继续改造 LocalAgent 项目的阶段二 Runtime。

## 一、代码语言规范

本次及后续代码修改必须遵守：

- 代码注释使用中文；
- Docstring 优先使用中文；
- 面向开发者的错误说明、日志说明和设计说明优先使用中文；
- 所有自然语言 Prompt 使用中文，以提高 DeepSeek 和 Qwen 的理解与可读性；
- Plan 的 `title`、`description`、`completion_criteria`、模型选择的 `reason_text` 等自然语言字段优先使用中文；
- 变量名、函数名、类名、枚举名、字段名、模块名、协议标识和常见技术搭配继续使用英文；
- 不要将标准技术标识符翻译为拼音；
- 稳定的 `error_code`、`reason_code`、Enum value 和配置 Key 继续使用英文；
- 中文说明与英文标识符混合时，保持代码简洁，不要为了中文化破坏 Python 常规命名习惯。

示例：

```python
class ModelSelectionReason(str, Enum):
    CONTEXT_WINDOW_REQUIRED = "context_window_required"


@dataclass(frozen=True, slots=True)
class ModelSelectionDecision:
    """描述一次模型选择结果。"""

    selected_profile: ModelProfileId
    reason_code: ModelSelectionReason
    reason_text: str
```

其中 `reason_text` 示例：

```text
当前任务需要长上下文，本地模型可用窗口不足，因此选择远程高级模型。
```

## 二、项目背景

LocalAgent 包含：

- PyQt6 前端；
- FastAPI 后端；
- 本地与远程模型；
- AgentRouter 和多 Agent 编排；
- Tool；
- RAG；
- SQLite Memory；
- Chroma；
- 自定义流式 HTTP；
- `[[ORCH]]` 编排标记。

已完成：

### 第 1～5 天

- Runtime 边界；
- RunContext；
- AgentState；
- AgentLoop；
- State Machine。

### 第 6 天：Context Engineering

已完成：

- ContextBuilder；
- ContextItem；
- Source Type；
- Trust Level；
- mandatory 保护；
- 确定性 Token 估算；
- 精确去重；
- 稳定排序；
- Item 级裁剪；
- 完整 messages Token 预算；
- ModelContextRequirements；
- 知识专家 RAG、当前请求和 Memory Summary 路径迁移。

知识专家当前完整 messages 为：

```text
system message
→ 仅通用系统指令和知识专家 Agent Prompt

history messages
→ 保持原始 role

Builder user message
→ Current User Request
→ Retrieved Documents
→ Relevant Memory
```

Memory Summary 不再进入 system message。

本次任务是：

“阶段二第 7 天：Planner 与 Model Selection Policy，包括结构化 Plan、PlanStep、任务能力需求，以及根据任务能力和完整上下文特征选择本地轻量模型或远程高级模型。”

## 三、固定工作流

严格执行：

第一步：阅读现有 Router、Planner、模型调用和配置代码
第二步：总结当前任务分解、模型选择和本地/远程调用方式
第三步：提出最小 Planner 与 Model Selection 方案
第四步：实施数据模型、Validator 和规则策略
第五步：最小接入一条真实模型调用路径
第六步：补充单元测试和集成测试
第七步：运行测试和检查
第八步：生成结果文档
第九步：补充 1～4 个重点 Bad Case
第十步：Commit、Push 并创建或更新 PR

不得跳过真实代码检查直接整体重写。

## 四、修改前必须检查

至少检查：

- `core/agent_router.py`
- 当前 Router 分类逻辑
- 当前任务拆分或 delegation 逻辑
- 当前已有 Planner Prompt 或计划解析逻辑
- `core/runtime/agent_loop.py`
- `core/runtime/state.py`
- `core/runtime/state_machine.py`
- `core/runtime/model_context.py` 或实际 ContextBuilder 文件
- 本地模型调用封装
- 远程 OpenAI-compatible 模型调用封装
- Model Client / Factory / Resolver
- Settings 中本地和远程模型配置
- Settings 中 context window / max output 配置
- 知识专家 `_build_messages()` 和实际模型调用位置
- 所有直接选择本地或远程模型的条件判断
- 所有硬编码 provider / model name 的位置
- 第 6 天结果文档

必须在结果文档中列出：

- 现有任务分解入口；
- 是否已有显式 Plan；
- 当前模型选择发生在哪里；
- 当前本地与远程模型如何实例化；
- 哪些路径本次接入；
- 哪些路径暂不接入。

## 五、本次目标

建立最小结构化 Planner 边界和确定性 Model Selection Policy。

目标调用关系：

```text
任务信息
→ Planner / Legacy Planner Adapter
→ Plan + TaskCapabilityRequirements
→ ContextBuilder
→ ModelContextRequirements
→ ModelSelectionPolicy
→ ModelSelectionDecision
→ Model Resolver
→ 现有本地或远程模型调用
```

本次：

- 不实现 Scheduler；
- 不让 Plan 驱动 AgentLoop；
- 不实现 Retry；
- 不实现 Fallback；
- 不实现 Circuit Breaker；
- 不实现 Budget；
- 不实现 Deadline 参与选择；
- 不实现模型健康检查；
- 不新增额外 LLM 规划调用，除非项目当前已经存在且本次只做兼容适配。

## 六、Planner 数据结构

建议新增：

```text
core/runtime/planning.py
```

或项目风格下的等价模块。

至少实现：

### 1. PlanSource

```text
DETERMINISTIC
LEGACY_ADAPTER
MODEL_GENERATED
```

### 2. RiskLevel

```text
LOW
MEDIUM
HIGH
```

### 3. TaskCapabilityRequirements

至少包含：

- `requires_planning`
- `requires_tools`
- `requires_rag`
- `requires_multi_agent`
- `requires_code_reasoning`
- `requires_structured_output`
- `requires_long_reasoning`
- `risk_level`
- `estimated_steps`

要求：

- 所有 bool 字段严格为 bool；
- `estimated_steps` 为正整数；
- 拒绝 bool 作为整数；
- 不保存用户正文；
- 不保存具体模型名。

### 4. PlanStep

至少包含：

- `step_id`
- `title`
- `description`
- `depends_on`
- `completion_criteria`
- `preferred_agent`
- `capability_requirements`

要求：

- frozen dataclass；
- Step ID 非空；
- title、description、completion criteria 使用清晰中文；
- 不保存 `status`、`started_at`、`ended_at`、`error_message`；
- 不保存 provider、model name、base URL；
- depends_on 使用不可变 tuple。

### 5. Plan

至少包含：

- `plan_id`
- `version`
- `task_summary`
- `steps`
- `created_at`
- `source`

要求：

- frozen dataclass；
- version 为正整数并拒绝 bool；
- task summary 使用简洁中文；
- created_at 为 timezone-aware UTC；
- 不保存 Runtime 状态。

### 6. PlanValidator

本次只校验：

- Plan ID；
- version；
- 至少一个 Step；
- Step ID 唯一；
- 依赖存在；
- 不允许自依赖；
- 不允许重复依赖；
- completion criteria 非空；
- estimated steps 合法。

不得本次实现：

- 拓扑排序；
- 环检测；
- Ready Step；
- Blocked 传播；
- Scheduler。

DAG 环检测属于第 9 天。

## 七、Planner 接入策略

先检查现有 AgentRouter 是否已有：

- 任务拆分；
- delegation；
- steps；
- Tool Plan；
- Agent assignment。

优先建立 Legacy Planner Adapter，将当前已有结构转换为 Plan。

如果某条简单路径没有显式 Plan，可创建确定性单步 Plan。

不得：

- 为简单问题强制构造多个虚假步骤；
- 额外调用一次远程模型只为生成 Plan；
- 改变现有 Router 选择结果；
- 让 Plan 直接控制 AgentLoop。

本次 Plan 主要用于：

- 结构化表达任务；
- 提供 TaskCapabilityRequirements；
- 为第 8 天 Scheduler 建立输入结构。

## 八、模型选择核心类型

建议新增：

```text
core/runtime/model_selection.py
```

至少实现：

### 1. ModelProfileId

```text
LOCAL_FAST
REMOTE_ADVANCED
```

### 2. ModelPreference

```text
AUTO
FORCE_LOCAL
FORCE_REMOTE
```

本次不修改 API，可在内部使用默认 AUTO。

### 3. ModelProfile

至少包含：

- `profile_id`
- `context_window`
- `max_output_tokens`
- `supports_tools`
- `supports_structured_output`
- `supports_code_reasoning`
- `supports_long_reasoning`
- `quality_tier`
- `latency_tier`

要求：

- 不能保存 API Key；
- Policy 中不能写死 DeepSeek / Qwen 名称；
- Profile 与真实模型名称的映射放在 Settings 或 Resolver；
- context window 必须来自现有配置或新增明确配置，不能偷偷根据模型名猜测；
- bool 不得作为整数配置。

### 4. ModelSelectionRequest

至少包含：

- `agent_id`
- `capability_requirements`
- `context_requirements`
- `preference`
- `available_profiles`

不得包含：

- 完整用户请求；
- Prompt；
- RAG 正文；
- Tool 原始参数；
- Memory 正文；
- Secret。

### 5. ModelSelectionReason

至少覆盖：

```text
USER_FORCED_LOCAL
USER_FORCED_REMOTE
LOCAL_SUFFICIENT
CONTEXT_WINDOW_REQUIRED
TOOL_CAPABILITY_REQUIRED
STRUCTURED_OUTPUT_REQUIRED
CODE_REASONING_REQUIRED
LONG_REASONING_REQUIRED
MULTI_STEP_PLAN
MULTI_AGENT_REQUIRED
HIGH_RISK_TASK
```

Enum value 使用稳定英文。

### 6. ModelSelectionDecision

至少包含：

- `selected_profile`
- `reason_code`
- `reason_text`
- `matched_rules`
- `fallback_allowed`

`reason_text` 使用中文。

本次 `fallback_allowed` 只作为后续策略提示，不执行实际 Fallback。

### 7. ModelSelectionError

异常只包含：

- 安全原因码；
- 请求的 Profile 类型；
- 缺失能力；
- 所需与可用 context window。

不得包含完整 Prompt、用户内容、RAG、路径或 Secret。

## 九、模型选择规则顺序

必须使用确定性规则并固定优先级。

### 1. Profile 校验

- Profile ID 唯一；
- 至少存在一个 Profile；
- context window、max output、tier 合法；
- FORCE_LOCAL 必须存在 LOCAL_FAST；
- FORCE_REMOTE 必须存在 REMOTE_ADVANCED。

### 2. 用户强制偏好

#### FORCE_LOCAL

本地满足全部硬约束：

```text
选择 LOCAL_FAST
```

本地不满足：

```text
抛出 ModelSelectionError
```

不得静默升级远程。

#### FORCE_REMOTE

存在远程 Profile：

```text
选择 REMOTE_ADVANCED
```

不存在则明确失败。

### 3. Context Window 硬约束

使用第 6 天完整调用的：

- `minimum_context_window`
- `estimated_input_tokens`
- `requires_long_context`

必须为近似 Token 预留安全余量。

可使用配置化：

```text
context_window_safety_ratio
```

或等价安全 margin。

要求：

- 安全系数明确；
- 结果文档说明默认值；
- 不得只用用户请求长度；
- 本地窗口不足、远程足够时选择远程；
- 所有 Profile 都不足时明确失败。

### 4. 硬能力匹配

根据 Profile 能力过滤：

- Tool；
- structured output；
- code reasoning；
- long reasoning。

不满足硬能力的 Profile 不得选择。

### 5. 复杂度规则

以下条件可以优先远程：

- requires_multi_agent；
- requires_long_reasoning；
- estimated_steps 达到明确阈值；
- risk_level = HIGH。

不得使用以下单一条件强制远程：

- requires_rag；
- contains_code；
- 用户输入字符数较长。

简单 RAG 和简单代码任务应允许本地模型。

### 6. 默认本地

AUTO 模式下，如果：

- 本地满足 context；
- 本地满足硬能力；
- 单步骤或低复杂度；
- 风险较低；

则选择 LOCAL_FAST。

## 十、模型 Resolver

如果当前已有本地和远程模型对象，建立最小 Resolver：

```text
ModelProfileId
→ 当前模型调用实现
```

Resolver 只负责解析并返回对应模型调用对象。

不得：

- 判断任务复杂度；
- 修改 ModelSelectionDecision；
- 执行 Fallback；
- 捕获失败后切换另一个 Profile；
- 创建无关网络客户端。

## 十一、最小真实路径接入

优先接入第 6 天知识专家主模型调用路径。

建议：

```text
知识专家任务
→ 创建或适配单步 Plan
→ 获取 TaskCapabilityRequirements
→ ContextBuilder.build()
→ 获取 ModelContextRequirements
→ ModelSelectionPolicy.select()
→ ModelResolver.resolve()
→ 调用所选模型
```

要求：

- AUTO 简单短上下文路径能够使用 LOCAL_FAST；
- 长上下文或硬能力不足时能够选择 REMOTE_ADVANCED；
- 每次只调用一个首选模型；
- 模型调用失败时不切换；
- 原有 messages 不变；
- 原有流式行为不变；
- 不修改 Router 选择结果；
- 不修改 `/api/chat`；
- 不修改 Memory Schema；
- 不修改 AgentState Schema；
- 不修改 `[[ORCH]]`。

如果知识专家当前必须固定使用某类模型，先说明真实限制，再选择风险最低的代表性模型调用路径，不得强行破坏业务语义。

## 十二、模型名称与配置

用户当前主要使用 DeepSeek 和 Qwen，但架构必须使用 Profile 抽象。

要求：

- 注释和自然语言 Prompt 中可以使用中文说明；
- 类名、变量、配置 Key 和 Profile ID 使用英文；
- Policy 不直接判断 `"deepseek"` 或 `"qwen"`；
- Settings / Resolver 可以配置实际 model name；
- 文档列出当前 LOCAL_FAST 和 REMOTE_ADVANCED 实际映射；
- 不输出 API Key、Token、Base URL 等敏感配置。

## 十三、状态和可观测边界

本次不得修改 AgentState Schema。

ModelSelectionDecision 暂时只存在于当前调用生命周期。

可以写入安全日志：

- selected profile；
- reason code；
- 中文 reason text；
- context window 需求；
- 不包含用户正文。

不得：

- 写入 final_output；
- 塞入 PlanStep status；
- 建立 Trace；
- 建立 Runtime Event；
- 持久化 Model Decision。

Trace 属于第 21 天。

## 十四、重点 Bad Case

结果文档必须包含：

```markdown
## 19. 重点 Bad Case
```

至少包含以下四个。

### Bad Case 1：Plan 和 AgentState 同时保存 Step 状态

- 类型：假设构造
- PlanStep 不得包含 status、时间或 error
- Runtime 状态只保存于 AgentState
- 增加反射或字段检查测试

### Bad Case 2：短问题依赖长上下文，却错误选择本地模型

- 类型：假设构造
- 不能根据 `len(user_query)` 选择模型
- 使用完整 `ModelContextRequirements`
- 本地窗口不足时选择远程
- Reason Code 为 CONTEXT_WINDOW_REQUIRED

### Bad Case 3：用户 FORCE_LOCAL 却静默升级远程

- 类型：假设构造
- FORCE_LOCAL 属于硬约束
- 本地能力不足时明确失败
- Resolver 不得调用远程
- 对应隐私和数据边界

### Bad Case 4：Planner 直接写死 DeepSeek 或 Qwen

- 类型：假设构造，除非真实检查发现
- Plan 只保存 capability requirements
- Model Selection Policy 根据 Profile 选择
- 同一 Plan 可在不同环境映射不同模型

每个 Bad Case 使用：

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

如果发现更高价值真实问题，可以增加，但必须明确真实性。

## 十五、测试要求

建议新增：

```text
tests/test_planning.py
tests/test_model_selection.py
```

至少覆盖：

### Plan

1. 合法单步 Plan
2. 合法多步 Plan
3. 空 Plan ID
4. 非法 version
5. bool version
6. 空 steps
7. 重复 Step ID
8. 缺失依赖
9. 自依赖
10. 重复依赖
11. naive datetime
12. frozen 不可修改
13. PlanStep 不含 Runtime 状态
14. PlanStep 不含 model/provider 字段

### Capability

1. estimated_steps 边界
2. RiskLevel
3. 简单任务映射
4. RAG 不单独强制远程
5. code content 不单独强制远程

### Model Profile

1. Profile ID 唯一
2. context window 边界
3. max output 边界
4. bool 数字配置拒绝
5. 能力字段匹配
6. 不保存 Secret

### Selection

1. AUTO 简单任务选择本地
2. 长上下文选择远程
3. 本地 Tool 能力不足选择远程
4. structured output 能力不足选择远程
5. 多步骤复杂任务选择远程
6. 多 Agent 任务选择远程
7. 高风险任务选择远程
8. 简单 RAG 允许本地
9. 简单代码问题允许本地
10. FORCE_LOCAL 成功
11. FORCE_LOCAL 能力不足明确失败
12. FORCE_REMOTE 成功
13. FORCE_REMOTE 不存在明确失败
14. 所有 Profile 都不满足明确失败
15. 相同输入结果稳定
16. reason code 正确
17. reason text 为中文且非空
18. 不执行 Fallback

### 集成

1. 知识专家产生 Plan 或 Legacy Plan
2. Context Requirements 进入 Selection Request
3. Selection Request 不含用户正文
4. LOCAL_FAST 调用本地现有模型入口
5. REMOTE_ADVANCED 调用远程现有模型入口
6. 每次只调用一个模型
7. 模型失败不会切换
8. messages 内容和 role 不变
9. `[[ORCH]]` 不变
10. 原有 Runtime 和 Context 测试全部通过

测试不得：

- 调用真实远程模型；
- 加载真实本地模型；
- 启动 Chroma、UI、FastAPI 或数据库；
- 访问外部网络。

使用 Fake Model、Fake Resolver 和 Fake TokenEstimator。

## 十六、测试和检查

至少执行：

```text
uv run python -m unittest \
  tests.test_runtime_context \
  tests.test_agent_state \
  tests.test_agent_loop \
  tests.test_state_machine \
  tests.test_model_context \
  tests.test_planning \
  tests.test_model_selection \
  -v

uv run pytest -q
uv pip check
uv run python -m compileall core tests
git diff --check
```

## 十七、GitHub 工作流

允许在当前任务分支：

- Commit；
- Push；
- 创建或更新 PR。

要求：

- 仅操作当前仓库和任务分支；
- 不擅自合并 PR；
- PR 说明 Planner、能力需求、Model Profile、规则优先级、真实接入路径、测试和 Bad Case；
- 不上传公司内部代码、配置、日志、数据、接口或敏感路径；
- 不在 Commit、PR 或文档中包含 API Key、Token、密码。

建议 Commit：

```text
Add rule-based planner model selection
```

## 十八、禁止事项

不得：

- 实现 Scheduler；
- 实现 DAG 环检测；
- 让 Plan 驱动 AgentLoop；
- 实现并行执行；
- 实现 Budget；
- 让 Deadline 参与模型选择；
- 实现 Retry；
- 实现 Fallback；
- 实现 Circuit Breaker；
- 实现模型健康检查；
- 修改 AgentState Schema；
- 实现 Trace；
- 实现 Runtime Event；
- 修改 API 请求体；
- 修改 Memory Schema；
- 修改流式协议；
- 修改 `[[ORCH]]`；
- 大规模重写 AgentRouter；
- 在 Planner 中写死 DeepSeek 或 Qwen；
- 使用用户输入长度作为唯一模型选择依据；
- 擅自合并 PR。

## 十九、结果文档

创建：

```text
docs/learning/stage2/day07_planner_model_selection_result.md
```

必须包含：

# 阶段二第 7 天改造结果

## 1. 本次任务目标

## 2. 修改前 Planner 和模型选择现状

## 3. 发现的问题

## 4. 最终设计方案

## 5. 新增文件

## 6. 修改文件

## 7. Plan 和 PlanStep

## 8. TaskCapabilityRequirements

## 9. PlanValidator

## 10. Model Profile

## 11. Model Selection Request / Decision

## 12. 规则优先级

## 13. 用户模型偏好

## 14. Model Resolver 和真实模型映射

说明当前 DeepSeek、Qwen 或现有本地/远程配置如何映射，但不得输出 Secret。

## 15. LocalAgent 真实接入路径

明确：

- 已接入路径；
- 未接入路径；
- 是否实际切换首选模型；
- 是否可能调用多个模型；
- 是否实现 Fallback。

## 16. 与现有功能兼容方式

## 17. 测试命令和结果

## 18. 设计权衡、未完成事项和面试描述

必须明确：

- Plan 尚未驱动 Scheduler；
- 未实现 DAG 环检测；
- 未实现 Budget；
- 未实现 Deadline 参与选择；
- 未实现 Fallback；
- 未实现模型健康检查；
- Decision 尚未持久化和 Trace；
- Token 统计仍为近似值；
- 其他 Agent 路径尚未迁移。

## 19. 重点 Bad Case

至少四个，按固定格式。

## 20. 需要带回 ChatGPT 审查的信息

必须包含：

- Planner 文件和入口；
- Plan 最终字段；
- PlanStep 最终字段；
- PlanValidator 规则；
- TaskCapabilityRequirements 字段；
- 能力需求来源；
- Model Profile 字段；
- LOCAL_FAST / REMOTE_ADVANCED 真实配置映射；
- ModelPreference；
- Selection Request 字段；
- Decision 字段；
- 规则执行顺序；
- context window 安全余量；
- FORCE_LOCAL / FORCE_REMOTE 语义；
- Model Resolver；
- 已接入路径；
- 未接入路径；
- 是否实际使用所选模型；
- 是否实现 Fallback；
- 是否修改 AgentState / API / Memory / Stream；
- 测试命令和结果；
- Bad Case；
- Commit / PR；
- 需要人工确认的问题；
- 后续建议，但不得实施第 8 天。

## 二十、聊天最终输出

完成后输出：

结果文档路径：

新增文件：

修改文件：

Planner 入口：

Plan 最终结构：

PlanStep 最终结构：

PlanValidator：

TaskCapabilityRequirements：

能力需求来源：

Model Profile：

LOCAL_FAST 映射：

REMOTE_ADVANCED 映射：

ModelSelectionPolicy 入口：

规则优先级：

context window 安全余量：

FORCE_LOCAL 语义：

FORCE_REMOTE 语义：

Model Resolver：

已接入路径：

未接入路径：

是否实际使用所选模型：

是否实现 Fallback：

测试命令：

测试是否通过：

Bad Case：

Commit：

PR：

需要人工确认的问题：

------

# 二十二、Codex 结果审查重点

结果回来后重点检查：

1. Planner 是否没有执行 Tool 或模型。
2. Plan 是否不可变。
3. PlanStep 是否没有运行状态。
4. Plan 是否没有具体 DeepSeek / Qwen 名称。
5. 依赖校验是否没有提前实现完整 DAG。
6. Capability Requirements 是否结构化。
7. 模型选择是否使用第 6 天完整上下文统计。
8. 是否使用 Token 安全余量。
9. 是否错误地用 `requires_rag` 强制远程。
10. 是否错误地用 `contains_code` 强制远程。
11. FORCE_LOCAL 是否绝不静默远程升级。
12. FORCE_REMOTE 不存在时是否明确失败。
13. Policy 是否确定性。
14. Reason Code 是否稳定。
15. Reason Text 是否使用中文。
16. Selection Request 是否不含用户正文。
17. Resolver 是否不做复杂度判断。
18. 是否只调用一个首选模型。
19. 模型失败后是否没有提前 Fallback。
20. 是否保持原 messages 和流式行为。
21. 是否修改 AgentState Schema。
22. Bad Case 是否区分真实与假设。
23. 代码注释和自然语言 Prompt 是否以中文为主。
24. 变量、类名、枚举和协议标识是否继续使用英文。

------

# 二十三、面试高频问题

## 1. Planner 为什么不能直接选择具体模型？

> Planner 负责表达任务步骤和能力需求，模型选择还要结合上下文窗口、用户偏好、可用 Profile、Budget 和模型健康状态。直接写死模型会让计划与环境配置强耦合。

## 2. 为什么不能通过用户问题长度选择模型？

> 用户问题可能很短，但依赖长 History、RAG 和 Memory。模型选择应使用完整 Model Input Context 的 Token 估算和任务能力需求。

## 3. FORCE_LOCAL 时本地模型能力不足怎么办？

> 明确返回模型选择失败，不能静默调用远程模型，因为 FORCE_LOCAL 可能代表隐私和数据边界，而不只是性能偏好。

## 4. Plan 和 AgentState 为什么要分开？

> Plan 描述应该做什么，AgentState 描述实际执行到哪里。执行状态只保存在 AgentState，避免 Planner 和 Runtime 出现双重事实源。

## 5. 为什么第 7 天不实现 Fallback？

> 主动模型选择发生在调用前，Fallback 是调用失败后的故障治理，还需要 Retry、Budget、熔断和流式输出边界，应在第 14 天统一实现。

------

# 二十四、当天验收清单

## 理论验收

-  区分 Planner、AgentLoop 和 Scheduler
-  理解不可变 Plan
-  理解 PlanStep 与 StepState 分离
-  理解 Task Capability Requirements
-  理解 Model Profile
-  理解用户偏好和硬能力约束
-  理解上下文窗口硬约束
-  理解规则型模型选择
-  理解按模型调用或 Step 选择
-  区分 Selection 与 Fallback

## 项目验收

-  Plan 和 PlanStep 已建立
-  PlanValidator 已建立
-  Capability Requirements 已建立
-  Model Profile 已建立
-  Model Selection Policy 已建立
-  FORCE_LOCAL / FORCE_REMOTE 已实现
-  上下文安全余量已实现
-  Reason Code 和中文 Reason Text 已实现
-  Model Resolver 已建立
-  至少一条真实路径接入
-  AUTO 简单任务可走本地
-  复杂或长上下文任务可走远程
-  每次只调用一个模型
-  未实现 Fallback
-  未实现 Scheduler
-  未修改 AgentState Schema
-  原有测试通过
-  Bad Case 完整
-  Commit 和 PR 完成
-  完成 ChatGPT 审查

## 阶段二进度

**第 7/25 天：理论与架构设计完成，等待 Codex 改造结果审查。**

下一天主题：**Scheduler（调度器）——根据 Plan 和 AgentState 计算 Ready Step、认领 Step、防止重复调度，并传播 BLOCKED 状态。**