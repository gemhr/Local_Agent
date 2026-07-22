# 阶段二第 1 天：Runtime 核心边界设计

**今日状态：进行中。**  
今天先完成通用架构设计和 LocalAgent 初步职责划分；待 Codex（代码智能体）输出项目现状分析后，再进行第 1 天最终验收。

---

## 1. 当天目标

今天必须解决三个问题：

1. **区分 Agent Framework（智能体框架）、Agent Harness（智能体执行支架）和 Agent Runtime（智能体运行时）。**
2. 明确 LocalAgent 中哪些职责应归入 Runtime，哪些不应归入。
3. 建立未来 25 天改造都要遵守的架构边界。

### 与前一阶段的关系

阶段一学习的是：

> Agent 应该采用什么模式完成任务。

阶段二学习的是：

> 一个 Agent 任务如何被创建、执行、约束、暂停、恢复、追踪和可靠终止。

也就是从“智能行为设计”转向“执行系统设计”。

### 今天暂不处理

今天不设计具体的：

- `RunContext`
- `AgentState`
- Agent Loop（智能体循环）
- State Machine（状态机）
- Checkpoint（检查点）
- Retry（重试）
- Scheduler（调度器）

这些将在后续对应学习日展开。今天只确定它们未来应该放在哪里。

---

# 2. 三个核心概念的边界

## 2.1 Agent Framework：开发 Agent 的工具箱

Agent Framework 关注的是：

> 开发者用什么抽象和组件来构建 Agent。

典型能力包括：

- Agent 定义
- Model（模型）适配
- Tool（工具）调用
- Prompt（提示词）管理
- Handoff（任务移交）
- Memory（记忆）
- Workflow（工作流）
- 结构化输出
- 中间件和扩展接口

例如，OpenAI Agents SDK 将 Agent 定义为配置了指令、工具、交接和结构化输出等行为的模型实体，并通过 Runner（运行器）驱动模型调用、工具调用和 Agent 移交循环。citeturn125252search0turn125252search1turn125252search5

### 核心特点

Framework 是一个**广义产品或开发工具集合**，不一定对应系统中的单独一层。

一个 Framework 可能同时提供：

- Harness 能力
- Runtime 能力
- Agent 抽象
- Tool 抽象
- Trace（追踪）能力

因此不能简单地说：

> OpenAI Agents SDK 是 Framework，所以它不是 Runtime。

更准确的说法是：

> OpenAI Agents SDK 整体是 Framework，其中的 Runner 和 Agent Loop 承担了部分 Runtime 职责。

---

## 2.2 Agent Harness：把 Agent 接入应用的执行支架

Agent Harness 没有完全统一的行业标准定义。工程上可以把它理解为：

> 围绕某个 Agent，组装其运行所需模型、指令、上下文、能力和策略的应用级支架。

它回答的是：

> 这个 Agent 在当前应用中以什么配置工作？它能看到什么？能使用什么？输出需要满足什么约束？

### Harness 的典型职责

1. **Agent 配置**
   - Agent 名称和职责
   - System Prompt（系统提示词）
   - 输出结构
   - 使用哪个模型

2. **能力装配**
   - 当前 Agent 可以调用哪些 Tool
   - 是否允许访问 RAG（检索增强生成）
   - 是否允许读取或写入 Memory
   - 是否允许委派给其他 Agent

3. **上下文装配策略**
   - 选择哪些历史消息
   - 注入哪些 Memory
   - 注入哪些检索结果
   - 如何插入 Tool Result（工具结果）
   - 如何组织模型输入

4. **执行前后钩子**
   - 模型调用前处理
   - 结果解析
   - 输出校验
   - Agent 层 Guardrail（护栏）
   - Agent 级日志或事件扩展

LangGraph 当前文档给出了一个很有参考价值的区分：LangChain 被称为 Agent Framework，Deep Agents 被称为构建在 LangGraph 之上的 Agent Harness，而 LangGraph 被称为提供持久化执行、流式输出和人工介入等能力的 Orchestration Runtime（编排运行时）。citeturn125252search2

### Harness 不应该负责什么

Harness 不应成为整个任务的生命周期控制器，不应独立负责：

- Run 的全局状态
- 最大执行步数
- 全局超时和取消
- 步骤调度
- Checkpoint 和 Resume（恢复）
- 全局预算
- 幂等控制
- 故障恢复

否则 Harness 会逐渐变成一个隐藏的 Runtime。

---

## 2.3 Agent Runtime：控制任务如何运行

Agent Runtime 关注的是：

> 一个 Agent Run 从开始到结束，如何被可靠、可控、可恢复地执行。

它不主要决定 Agent“想做什么”，而是控制：

- 当前是否允许继续做
- 下一步何时执行
- 执行失败后怎么办
- 是否超时或超预算
- 是否应该取消
- 状态是否合法
- 进程退出后如何恢复
- 如何留下完整执行记录

### Runtime 的核心职责

阶段二最终将逐步覆盖：

```text
Run 生命周期
├── RunContext
├── AgentState
├── Agent Loop
├── State Machine
├── Planner 调用协调
├── Scheduler
├── Budget
├── Timeout
├── Cancellation
├── Retry
├── Fallback
├── Circuit Breaker
├── Checkpoint
├── Resume
├── Idempotency
├── Human Approval
├── Trace
└── Replay
```

Temporal 是更通用的 Durable Execution（持久化执行）平台，不是专门的 Agent Framework，但它很好地体现了 Runtime 思想：执行状态、重试、任务队列、信号和定时器由运行平台管理，业务逻辑不需要自行拼装全部故障恢复机制。citeturn125252search3turn125252search8turn125252search22

---

## 2.4 一句话区分

| 概念            | 核心问题                                      |
| --------------- | --------------------------------------------- |
| Agent Framework | 开发者用什么抽象构建 Agent？                  |
| Agent Harness   | 这个 Agent 在应用里携带哪些配置和能力运行？   |
| Agent Runtime   | 这个 Run 如何被执行、限制、暂停、恢复和终止？ |

可以用下面三个问题快速判断：

- **“Agent 能做什么、能看到什么？”**——通常属于 Harness。
- **“任务现在是什么状态、还能不能继续执行？”**——属于 Runtime。
- **“开发者用哪些组件搭建 Agent？”**——属于 Framework。

---

# 3. Agent、Harness 与 Runtime 的关系

建议 LocalAgent 最终形成以下控制链：

```text
PyQt6 / FastAPI
        │
        ▼
   RunService
        │
        ▼
   AgentRuntime
        │
        ├── RunContext
        ├── AgentState
        ├── Agent Loop
        ├── State Machine
        ├── Scheduler
        └── Runtime Policy
                │
                ▼
          Agent Harness
                │
                ├── Agent Definition
                ├── Prompt / Context Policy
                ├── Model Configuration
                ├── Capability Binding
                └── Output Validation
                        │
                        ▼
                  Agent Decision
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
       Model           Tool        RAG / Memory
```

这不是要求立即创建所有类和目录，而是先确定**依赖方向**。

---

# 4. 各层职责设计

## 4.1 API 层

负责：

- 接收 HTTP 请求
- 参数校验
- 身份和权限信息提取
- 创建或取消 Run 的应用请求
- SSE（服务器发送事件）或普通响应输出
- 将 Runtime Event（运行时事件）转换为前端事件

不负责：

- 自己执行 Agent Loop
- 自己保存 Run 状态
- 根据循环次数决定是否终止
- 在断开连接时直接修改 Agent 内部变量
- 直接编排多个 Agent

理想关系是：

```text
API → RunService
```

而不是：

```text
API → Router → Planner → Agent → Tool
```

---

## 4.2 RunService 层

RunService 是 Application Service（应用服务），负责把外部请求转成 Runtime 可以执行的用例。

它负责：

- 创建 Run 请求
- 选择入口 Agent 或 Harness
- 向 Runtime 提交执行
- 查询 Run
- 请求取消 Run
- 请求恢复 Run
- 将 Runtime 结果返回 API

它不负责实现具体 Agent Loop。

可以理解为：

> RunService 决定“发起哪个业务用例”，Runtime 决定“这个用例如何可靠执行”。

---

## 4.3 Runtime 层

负责所有跨 Agent、跨步骤的执行控制：

- 分配和维护 `run_id`
- 维护 Run 生命周期
- 持有运行状态
- 推进 Agent Loop
- 检查状态转移是否合法
- 调用 Planner 和 Scheduler
- 执行预算检查
- 传播超时和取消
- 应用重试与降级策略
- 保存 Checkpoint
- 恢复未完成任务
- 等待人工审批
- 生成结构化 Trace

Runtime 应依赖接口，而不是直接耦合具体实现，例如：

```text
ModelPort
ToolExecutorPort
RetrievalPort
MemoryPort
CheckpointStore
RuntimeEventSink
```

这里的名称只是架构角色，今天不要求在 LocalAgent 中创建这些文件或类。

---

## 4.4 Agent 层

Agent 负责决策逻辑：

- 对任务进行理解
- 选择行动
- 生成 Plan（计划）
- 判断调用哪个能力
- 解释观察结果
- 决定是否需要继续思考
- 生成最终回答

Agent 不应该随意修改全局 Run 状态。

例如 Agent 可以返回：

```text
CallTool
DelegateAgent
GenerateFinalAnswer
RequestReplan
```

但“是否允许执行”“执行后转成什么状态”应由 Runtime 决定。

---

## 4.5 Model 层

负责：

- 封装本地和远程模型
- 请求与响应转换
- 流式响应适配
- Provider（模型提供方）差异处理
- 底层网络异常转换
- Token 使用量解析

不负责：

- Run 最大步骤数
- 整体任务取消状态
- Agent 状态流转
- 业务级 Retry 决策
- 主备模型切换流程的全局控制

例如底层 Model Client 可以识别“HTTP 429”，但“是否重试、是否换备用模型、是否耗尽预算”应由 Runtime 策略决定。

---

## 4.6 Tool 层

Tool 负责具体能力：

- 输入校验
- 执行业务操作
- 返回结构化结果
- 抛出或转换明确错误
- 做必要的资源清理

Tool 不负责：

- 自己决定整个 Run 是否成功
- 自己进行无限重试
- 自己修改全局 AgentState
- 自己调度下一个步骤

Runtime 负责 Tool 的**执行治理**，Tool 负责 Tool 的**业务行为**。

---

## 4.7 RAG 层

RAG 负责：

- Query（查询）处理
- Embedding（向量嵌入）
- 检索
- 重排
- 文档片段和引用返回

Runtime 负责：

- 何时调用检索
- 检索是否超时
- 检索失败是否阻断任务
- 调用次数是否超过预算
- 记录 Retrieval Span（检索跨度）

不要把向量检索算法本身搬入 Runtime。

---

## 4.8 Memory 层

Memory 负责：

- 会话消息存储
- 滚动摘要
- 长期记忆检索
- FTS5 查询
- Memory 写入与读取

但需要区分：

### Conversation Memory（会话记忆）

记录用户和 Agent 交流过什么。

### Runtime State（运行时状态）

记录任务执行到了哪里。

### Checkpoint

记录 Runtime 为恢复执行所需的可信快照。

三者不能继续混为一体。

例如：

```text
“用户喜欢简洁回答”
```

属于 Memory。

```text
“步骤 3 已成功，步骤 4 正在等待审批”
```

属于 Runtime State 或 Checkpoint。

---

# 5. LocalAgent 当前职责的初步划分

下面仅根据你已经描述的 LocalAgent 功能进行初步判断，最终需要 Codex 结合代码验证。

## 5.1 应明确划入 Runtime 的职责

### 必须进入 Runtime

- 一次任务从开始到结束的生命周期
- Agent 执行循环
- 最大循环次数或最大步骤数
- 多 Agent 委派的执行控制
- 串行或并行步骤推进
- Run 和 Step 状态
- 终止原因
- 用户取消
- 全局超时
- 执行预算
- Retry、Fallback 和 Circuit Breaker 的协调
- Checkpoint 和 Resume
- 幂等和重复提交控制
- Human Approval（人工审批）暂停与恢复
- `[[ORCH]]` 对应的结构化运行事件
- Trace 和 Replay

### Runtime 负责协调，但不拥有业务实现

- Planner
- Model 调用
- Tool 调用
- RAG 调用
- Memory 读取与写入
- 多 Agent 调用

---

## 5.2 不应该直接划入 Runtime 的职责

- PyQt6 页面逻辑
- FastAPI 路由定义
- SSE 文本格式
- 专业 Agent 的 Prompt
- Router 的意图分类规则
- Planner 生成计划的提示词
- 具体 Tool 实现
- Excel、CSV 分析逻辑
- Chroma 检索逻辑
- Embedding 模型加载
- SQLite 会话历史表
- Wiki 同步
- AgentEvalOps 评估平台
- Tool Registry 和 Skill 系统

---

## 5.3 当前最可能存在的职责混杂

这些是需要 Codex 验证的**检查假设**，不是对代码现状的结论。

### 混杂一：Router 同时承担决策与执行

可能存在：

```text
Router 判断 Agent
→ Router 直接调用 Agent
→ Router 直接处理 Tool
→ Router 直接拼最终结果
```

应该拆分为：

```text
Router / Planner 给出决策
→ Runtime 接收决策
→ Runtime 执行动作
→ Runtime 更新状态
```

Router 可以决定“下一步是什么”，但不应该完全掌控 Run 生命周期。

---

### 混杂二：FastAPI 接口直接控制 Agent 循环

可能存在：

```text
API 函数
├── 加载 Memory
├── 调 Router
├── 调 Agent
├── 处理流式输出
├── 捕获所有异常
└── 保存结果
```

这会导致 API、应用服务和 Runtime 三层挤在一个调用链中。

目标应是：

```text
API
→ RunService
→ Runtime
→ Agent / Model / Tool
```

---

### 混杂三：Agent 内部直接修改会话和运行状态

如果 Agent 在执行中直接：

- 写 SQLite
- 修改会话状态
- 写入最终结果
- 决定 Run 成功或失败
- 捕获所有异常并返回普通文本

Runtime 就无法可靠区分：

- 业务失败
- Model 失败
- Tool 失败
- 用户取消
- 超时
- 预算耗尽

---

### 混杂四：`[[ORCH]]` 同时承担协议、日志和状态

现有 `[[ORCH]]` 事件可能同时用于：

- 前端展示
- 调试日志
- Agent 状态表达
- 多 Agent 编排消息
- 错误通知

后续应逐步分离成：

```text
Runtime Event
    ├── Event Type
    ├── run_id
    ├── step_id
    ├── timestamp
    ├── payload
    └── schema_version
```

然后由不同消费者分别处理：

- SSE Adapter
- 日志系统
- Trace Store
- 前端展示

但这属于第 21 天的正式改造，今天只记录边界。

---

# 6. 第 1 天目标架构方案

## 6.1 本次改造目标

今天不改生产代码，只完成：

1. LocalAgent 当前调用链盘点。
2. 模块职责矩阵。
3. Runtime 候选职责识别。
4. 职责混杂点识别。
5. 最小迁移顺序建议。
6. 输出第 1 天结果文档。

---

## 6.2 影响范围

Codex 需要重点检查，但不限于：

- FastAPI 启动入口和 API 路由
- 聊天或任务执行接口
- Router 和 Planner
- 多 Agent 编排代码
- Agent 基类和具体 Agent
- Model Client
- Tool 调用入口
- RAG 调用入口
- Memory 读写入口
- 流式输出和 SSE
- `[[ORCH]]` 事件
- 异常处理
- 任务状态或会话状态
- 测试目录

必须根据真实项目结构定位，不能预设文件名。

---

## 6.3 兼容要求

未来改造必须保持：

- PyQt6 前端现有可用行为
- FastAPI 接口基本兼容
- 本地模型和远程模型均可使用
- 现有 Agent 能力不删除
- RAG 和 Memory 功能不退化
- Tool 调用功能不删除
- `[[ORCH]]` 在替代方案完成前继续兼容
- 不把 AgentEvalOps 混入 LocalAgent
- 不提前引入阶段三功能

---

## 6.4 主要风险

### 风险一：Runtime 变成新的上帝对象

把所有代码都移到 `AgentRuntime` 中，不等于完成了架构拆分。

Runtime 应控制执行，而不实现：

- Prompt 业务逻辑
- 检索算法
- Tool 业务逻辑
- Model Provider 细节

### 风险二：过早抽象

在没有分析当前调用链之前，直接创建几十个接口和目录，容易形成空架构。

因此第 1 天只输出边界设计，不实施大规模重构。

### 风险三：破坏流式输出

当前 LocalAgent 可能把模型流、Agent 事件和前端状态紧密绑定。后续拆分 Runtime 时，必须保留流式体验。

### 风险四：混淆 Memory 与 Checkpoint

不能直接把现有 SQLite Memory 表当成 Runtime Checkpoint 使用。两者数据语义和一致性要求不同。

---

# 7. 今天应掌握的深度

## 必须掌握

- Framework、Harness、Runtime 的区别。
- Agent 负责决策，Runtime 负责控制执行。
- API 不应该直接承担 Agent Loop。
- Tool、RAG、Memory 是被 Runtime 调用的能力，不是 Runtime 本体。
- 会话记忆和运行状态必须分离。
- Runtime 边界首先是职责和依赖方向，而不是创建一个名为 `runtime.py` 的文件。

## 需要理解

- 一个开源框架可能同时跨越多个架构层。
- Harness 不是统一标准术语，需要结合上下文说明。
- RunService 与 Runtime 的区别。
- Runtime 应通过稳定接口调用外部能力。
- 结构化 Runtime Event 和前端 SSE 消息不是同一个概念。

## 加分项

- 能说明 Temporal、LangGraph 和 OpenAI Agents SDK 分别体现了哪些 Runtime 思想。
- 能解释为什么不能让 Agent 自己随意修改全局状态。
- 能识别“换目录但不换职责”的伪重构。

---

# 8. 第一份 Codex 分析提示词

这次只允许 Codex 分析项目并生成文档，不允许大规模修改生产代码。

:::writing{variant="document" id="81427"}
你正在协助分析一个名为 LocalAgent 的本地 AI Agent 项目。

项目背景：

- 项目由 PyQt6 前端和 FastAPI 后端组成。
- 支持本地与远程大模型。
- 当前已经具备 Router、Planner、多 Agent 编排、RAG、SQLite Memory、Chroma、Tool 系统、流式输出和 `[[ORCH]]` 编排事件。
- 当前学习目标是将项目逐步升级为具备生产级 Agent Runtime 的系统。
- 本次属于“阶段二第 1 天：Agent Framework、Agent Harness 与 Agent Runtime 的边界分析”。
- 本次不能实现 RunContext、AgentState、Agent Loop、State Machine、Scheduler、Checkpoint 等后续功能。
- 本次任务以代码现状分析和架构边界设计为主，不进行大规模代码改造。

请严格遵循以下工作流：

第一步：阅读项目结构和相关代码  
第二步：总结现状和问题  
第三步：给出最小改造方案  
第四步：实施修改  
第五步：补充或更新测试  
第六步：运行相关测试和检查  
第七步：输出结果信息文档  

本次“实施修改”仅限于创建或更新分析结果文档。除非为了修复文档生成导致的明显小问题，否则不要修改生产代码、测试代码、配置文件或依赖文件。

## 一、本次任务目标

请基于项目真实代码完成以下分析：

1. 找到一次用户请求从 PyQt6 或 FastAPI 入口到最终回答的完整调用链。
2. 找到 Router、Planner、多 Agent、Model、Tool、RAG、Memory 和流式输出的真实入口。
3. 找到当前负责以下职责的代码位置：
   - 请求接收
   - Run 或任务创建
   - Agent 选择
   - 计划生成
   - Agent 执行循环
   - Tool 调用
   - RAG 调用
   - Memory 读取和写入
   - 多 Agent 委派
   - 并行执行
   - 流式输出
   - 异常处理
   - 取消或断开处理
   - 状态保存
   - `[[ORCH]]` 事件生成与消费
4. 识别哪些模块同时承担了 API、应用服务、Runtime、Agent 或基础设施等多种职责。
5. 判断当前项目是否已经存在以下概念的雏形：
   - RunService
   - Runtime 或 Runner
   - RunContext
   - AgentState
   - Agent Loop
   - 状态机
   - Runtime Event
6. 给出未来最小化建立 Runtime 边界的方案，但本次不得实施这些后续功能。

## 二、分析范围

请先查看完整项目目录，再根据真实结构定位相关代码，不要假设项目一定存在某些固定文件名。

重点检查：

- FastAPI 应用入口和路由
- 聊天、任务执行或流式接口
- PyQt6 与后端交互入口
- Router
- Planner
- 多 Agent 编排逻辑
- Agent 基类和具体 Agent
- Model Client 或模型适配层
- Tool 调用入口和执行器
- RAG 服务
- Memory 服务和 SQLite 访问
- Chroma 访问
- SSE、流式生成器或事件队列
- `[[ORCH]]` 事件
- 异常处理
- 日志
- 测试代码

对于每个结论，必须给出对应的：

- 文件路径
- 类名或函数名
- 当前职责
- 调用方
- 被调用方
- 判断依据

不要只根据文件名推断职责。

## 三、架构分类要求

请将当前模块和关键类划分到以下类别：

1. UI / Client
2. API / Transport
3. Application Service
4. Agent Runtime
5. Agent Harness
6. Agent / Planner / Router
7. Model Adapter
8. Tool
9. RAG / Retrieval
10. Memory
11. Persistence / Infrastructure
12. Observability
13. 无法明确分类或职责混杂

请特别区分：

- Agent 决策逻辑与 Runtime 执行控制
- API 流式协议与 Runtime Event
- Conversation Memory 与 Runtime State
- Tool 业务实现与 Tool 执行治理
- Model Client 网络调用与 Runtime 重试策略
- Router 的路由决策与 Router 直接执行任务

## 四、需要重点回答的问题

请在结果文档中明确回答：

1. 当前是否存在统一的 Run 概念？
2. 是否存在稳定的 `run_id`、`session_id` 或 `trace_id`？
3. 当前由谁控制 Agent 执行循环？
4. 当前由谁决定任务成功、失败或停止？
5. API 层是否直接调用具体 Agent、Model、Tool、RAG 或 Memory？
6. Router 或 Planner 是否同时承担执行器职责？
7. Agent 是否直接写入 Memory、数据库或全局状态？
8. Tool 是否自行实现了重试、超时或状态修改？
9. `[[ORCH]]` 是日志格式、前端协议、内部事件还是多种职责混合？
10. SSE 或客户端断开后，后台执行是否继续？
11. 当前是否存在取消机制？取消如何传播？
12. 当前异常是否被转换成普通文本，从而丢失错误类型？
13. 当前会话状态、任务状态和执行状态是否混在一起？
14. 如果只做最小改造，未来应该首先在哪一层增加 RunService 和 Runtime 边界？
15. 哪些现有代码应该保留原位，通过接口被 Runtime 调用，而不应该迁入 Runtime？

## 五、最小架构方案要求

只输出方案，不实施生产代码修改。

方案至少需要包含：

- 推荐的调用方向
- API、RunService、Runtime、Agent Harness、Agent、Model、Tool、RAG 和 Memory 的职责
- 现有模块未来应如何归类
- 哪些耦合需要优先解除
- 哪些耦合可以暂时保留
- 建议的分阶段迁移顺序
- 如何保持现有接口和流式输出兼容
- 第 2～5 天改造可能影响的代码区域

不要提前设计或实现阶段三内容，包括：

- Tool Registry
- Agent Skill
- MCP
- A2A
- Sandbox

不要把 AgentEvalOps 或独立评估平台功能加入 LocalAgent。

## 六、代码和改动限制

本次禁止：

- 未分析代码就整体重写。
- 大规模移动文件。
- 创建完整 Runtime 实现。
- 创建 RunContext、AgentState、Agent Loop 或 State Machine 的正式实现。
- 为展示复杂度引入大量接口或抽象基类。
- 修改无关模块。
- 删除现有功能。
- 修改现有 API 行为。
- 新增大型依赖。
- 修改公司内部配置。
- 输出公司内部数据、用户真实数据、内部接口地址或敏感日志。
- 将代码或信息上传到任何外部服务。
- 执行 Git push。
- 创建 Pull Request。
- 实施下一学习日内容。

允许的实际改动仅包括：

- 创建目录 `docs/learning/stage2/`，如果该目录尚不存在。
- 创建结果文档：
  `docs/learning/stage2/day01_runtime_boundary_result.md`

## 七、测试和检查要求

本次主要是只读分析，因此不要为了满足测试要求而修改生产代码。

请：

1. 记录项目当前已有的测试入口和静态检查方式。
2. 在环境允许且不会产生外部访问、数据变更或长时间运行的情况下，执行与主调用链相关的轻量测试或静态检查。
3. 如果测试依赖公司内部服务、真实模型、真实数据库或敏感数据，不要强行执行。
4. 不要修复与本次分析无关的已有测试失败。
5. 在文档中如实记录：
   - 实际执行的命令
   - 是否通过
   - 未执行的原因
   - 是否发现已有失败

## 八、结果文档要求

生成：

`docs/learning/stage2/day01_runtime_boundary_result.md`

文档必须使用以下结构：

# 阶段二第 1 天改造结果

## 1. 本次任务目标

## 2. 修改前现状

## 3. 发现的问题

## 4. 最终设计方案

## 5. 新增文件

## 6. 修改文件

## 7. 核心类、接口和数据结构

## 8. 关键执行流程

至少包含：

- 普通问答调用链
- RAG 调用链
- Tool 调用链
- 多 Agent 调用链
- 流式输出调用链

可以使用 Mermaid 绘制调用链，但必须同时提供文字说明。

## 9. 与现有功能的兼容方式

## 10. 异常处理和边界情况

## 11. 测试内容

## 12. 实际执行的测试命令

## 13. 测试结果

## 14. 未完成事项

## 15. 已知风险

## 16. 设计权衡

## 17. 可用于面试的项目描述

请给出一段基于真实项目现状的面试表达，重点说明：

- 为什么需要从功能集合升级为 Runtime
- 当前发现了什么职责混杂
- 为什么先建立边界而不是直接重写
- 后续准备如何演进

不要声称尚未实现的能力已经完成。

## 18. 需要带回 ChatGPT 审查的信息

必须包含：

- 本次最重要的设计决策
- 当前真实调用链
- 与原有理解相比的新发现
- 最严重的三个职责混杂点
- 推荐的 Runtime 接入位置
- 尚不确定的问题
- 测试失败或无法执行的原因
- 后续建议，但不得直接实施第 2 天内容

## 九、聊天最终输出格式

完成后，请在聊天中额外输出：

结果文档路径：

本次修改文件列表：

是否修改生产代码：

测试是否通过：

识别出的 Runtime 推荐接入位置：

最严重的三个职责混杂点：

需要人工确认的问题：
:::

---

# 9. Codex 结果审查重点

你带回结果文档后，我会重点检查：

1. 是否真正沿调用链阅读代码，而不是只根据目录命名分类。
2. 是否找到实际控制 Agent Loop 的代码。
3. 是否区分 Router 决策与执行控制。
4. 是否区分 Memory、Runtime State 和 Checkpoint。
5. 是否错误地把 Tool、RAG、Model 实现归入 Runtime。
6. 是否提出了过度抽象或大规模搬迁方案。
7. 是否保留现有流式输出和接口行为。
8. 是否提前混入第 2 天以后或阶段三内容。
9. 面试描述是否只陈述已经完成的工作。
10. 文档是否包含公司敏感信息。

---

# 10. 面试高频问题

## 问题一：Agent Framework 和 Agent Runtime 有什么区别？

回答要点：

> Framework 提供开发 Agent 的抽象和组件；Runtime 负责一次 Agent Run 的生命周期、状态推进、超时、取消、预算、故障恢复和可观测性。一个 Framework 可以包含 Runtime，但两者关注的问题不同。

## 问题二：为什么不能让 FastAPI 接口直接执行整个 Agent 流程？

回答要点：

> API 是传输层，应该处理协议和请求响应。如果它同时负责 Agent 循环、状态、重试和工具调度，就会导致执行逻辑无法被其他入口复用，也难以支持取消、恢复、测试和持久化执行。

## 问题三：Agent 与 Runtime 谁决定下一步？

回答要点：

> Agent 可以提出下一步动作，例如调用工具或生成回答；Runtime 判断动作是否合法、预算是否足够、是否超时，并负责执行、更新状态和决定是否继续推进。

## 问题四：Memory 和 Runtime State 有什么区别？

回答要点：

> Memory 保存对未来对话有帮助的信息；Runtime State 保存当前任务执行到了哪里。前者服务于上下文和个性化，后者服务于控制、恢复和一致性。

## 问题五：如何避免 Runtime 成为上帝对象？

回答要点：

> Runtime 只负责执行控制，通过接口调用 Model、Tool、RAG 和 Memory。具体模型请求、检索算法和工具业务逻辑仍由各自模块负责。

---

# 11. 当天知识总结

今天需要形成的核心认识是：

> Agent 负责产生决策，Harness 负责组装 Agent 的能力和上下文，Runtime 负责把决策可靠地执行下去。

LocalAgent 的阶段二改造，不是简单增加一个 `runtime.py`，而是逐步做到：

```text
决策与执行分离
协议与执行分离
业务能力与执行治理分离
会话记忆与运行状态分离
事件事实与前端展示分离
```

---

# 12. 当天验收清单

## 理论验收

- [x] 能区分 Framework、Harness 和 Runtime
- [x] 能区分 Agent 决策与 Runtime 控制
- [x] 能区分 RunService 与 Runtime
- [x] 能区分 Memory 与 Runtime State
- [x] 能判断 Tool、RAG、Model 是否属于 Runtime

## 项目验收

- [ ] 已获取 LocalAgent 真实目录与调用链
- [ ] 已找到实际控制 Agent Loop 的代码
- [ ] 已找到 `[[ORCH]]` 事件生产和消费位置
- [ ] 已识别前三个职责混杂点
- [ ] 已确定 Runtime 最小接入位置
- [ ] 已生成 `day01_runtime_boundary_result.md`
- [ ] 已完成 ChatGPT 审查

## 阶段二进度

**第 1/25 天：理论与架构方案已完成，等待 Codex 项目分析结果审查。**

下一步主题为第 2 天：**RunContext（运行上下文）、生命周期、依赖注入、运行标识、Deadline（截止时间）与 Cancellation Token（取消令牌）**。