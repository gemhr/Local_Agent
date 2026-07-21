# 阶段二第 4 天改造结果

## 1. 本次任务目标

为现有 `RunContext`、`AgentState` 和遗留 `AgentRouter.chat_stream()` 建立最小的同步 Agent Loop；不拆分 Router 内部的模型、Tool、RAG、Memory 或多 Agent 编排职责。

## 2. 修改前现状

`ChatService.stream_chat()` 直接创建、启动和结束 `legacy-agent-router` Step，并在自身处理成功、deadline、取消和普通异常。调用链为 `server.py::chat_endpoint()` → `ChatService.stream_chat()` → `AgentRouter.chat_stream()`。

## 3. 发现的问题

旧实现没有统一的“决策—执行—观察”边界，无法在不改造 AgentRouter 的情况下测试多轮、最大步骤、无动作和连续重复动作终止。循环计数器也不应提前进入 AgentState schema。

## 4. 最终设计方案

新增 `core/runtime/agent_loop.py`。`AgentLoop` 在单次 generator 生命周期内拥有循环控制，接受显式 `AgentLoopPolicy`、`RunContext`、`AgentState` 和最小 Driver。ChatService 只创建 Context/State/Legacy Driver 并 `yield from` Loop。未修改 AgentState schema（仍为版本 1）。

## 5. 新增文件

- `core/runtime/agent_loop.py`
- `tests/test_agent_loop.py`
- 本结果文档

## 6. 修改文件

- `core/runtime/__init__.py`：导出 Loop 的公共最小类型。
- `core/chat_service.py`：将生命周期维护迁移给 `AgentLoop`。
- `tests/test_agent_state.py`：断言迁移后的 Loop logger。

## 7. 核心类、接口和数据结构

- `AgentLoop`：运行入口是 `run_stream(...)`；它是 Run/State 生命周期和局部计数器的所有者。
- `AgentLoopPolicy`：默认 `max_steps=8`、`max_consecutive_no_action=1`、`max_consecutive_same_action=2`，所有字段必须是非 bool 的正整数。
- `AgentAction`：`step_id`、`name`、`action_type`、`dedup_key` 都不能为空。`dedup_key` 仅允许短的 opaque identifier 字符集（字母数字、`. _ : -`），拒绝路径和自由文本；Driver 必须只提供稳定类别标识，不可放用户输入、Prompt、文件路径、原始 Tool 参数、API Key 或公司信息。
- `ActionOutcome`：`CONTINUE`、`COMPLETED`、`FAILED`。
- `AgentObservation`：包含 `outcome`、`should_continue` 属性、可选 `final_output` 和安全 `error_code`/`error_message`；不保存异常对象、traceback 或原始异常文本。
- `AgentLoopDriver`：仅有 `decide(previous_observation)` 和 `execute(action, run_context)`。
- `LegacyAgentRouterDriver`：唯一兼容适配器，使用固定 action `legacy-agent-router`，将整个遗留 Router 包为一次 action。
- 循环局部计数器：`steps_taken`、`consecutive_no_action`、前一个 `dedup_key` 和连续相同动作计数，不进入 AgentState。

## 8. 关键执行流程

初始化时 Loop 验证 run_id、拒绝终态 State、标记 `RUNNING` 并检查 deadline/cancellation。每轮先决定 action，决策本身不执行 Router。`None` 连续次数达到阈值立即失败为 `NO_ACTION`，不会创建 Step。

非空 action 在执行前检查重复 key，并在未超限后增加 Step 计数、创建并启动 Step。相同 key 第一次计为 1；当连续次数**大于**默认 2 时，超限 action 在创建 Step 和执行前失败为 `REPEATED_ACTION`。不同 key 会将连续计数重置为 1。

接受的 action 在执行前消耗一个 Step。若已成功执行 `max_steps` 个且 observation 仍为 `CONTINUE`，下一轮在决定/执行新 action 前失败为 `MAX_STEPS_REACHED`；第 `max_steps + 1` action 不执行。

`CONTINUE` 将当前 Step 标记成功、保存 observation 并再次决策；`COMPLETED` 标记 Step/Run 成功、StopReason 为 `COMPLETED` 并保存 final output；`FAILED` 标记 Step/Run 失败并使用 observation 的安全错误摘要。deadline 使活跃 Step 失败并映射 `DEADLINE_EXCEEDED`；取消使活跃 Step 取消并映射 `USER_CANCELLED`；未知异常记录原异常日志，但 State 固定为 `UNHANDLED_ERROR` / `Agent execution failed`，随后原异常继续抛出。

`GeneratorExit` 显式重新抛出，不运行成功收尾、不伪造 `CLIENT_DISCONNECTED`；因此提前 close 后临时 State 可能保持 RUNNING。

## 9. 与现有功能的兼容方式

`LegacyAgentRouterDriver.execute()` 只调用一次 `AgentRouter.chat_stream()`，使用同一 RunContext，并逐 chunk 原样转发普通文本和 `[[ORCH]]` 内容。它只将不以 `[[ORCH]]` 开头的普通文本 chunk 聚合为 final output；当前 `AgentRouter.chat_stream()` 的 Generator return value 是 `None`，因此不能从其 return value 取得语义最终结果。API 请求体、Memory schema、文本流和 `[[ORCH]]` 协议均未修改。

## 10. 异常处理和边界情况

循环在决策和每次从执行 generator 读取 chunk 前调用 `RunContext.raise_if_inactive()`。deadline/cancellation 继续向上抛出，符合第 3 天边界。普通异常使用 `logger.exception` 记录，且不会将 `str(exc)` 写入 AgentState。

## 11. 测试内容

`tests/test_agent_loop.py` 覆盖 Policy 校验、单 action、chunk 顺序、CONTINUE previous observation、只读派生的 `should_continue`、多步上限、无动作、重复 key/重置、deadline、取消、未知异常及原异常传播、generator close、部分流后失败，以及 Legacy Router 单调用/同 Context/原样 `[[ORCH]]` 和 final_output 去协议污染。现有 Context 与 State 测试继续覆盖主服务兼容行为。

## 12. 实际执行的测试命令

```bash
python -m unittest tests.test_runtime_context tests.test_agent_state tests.test_agent_loop -v
python -m compileall core tests
```

## 13. 测试结果

上述 unittest 命令通过 43 个测试；`compileall` 通过。未启动本地/远程模型、Chroma、PyQt6、FastAPI 服务、真实数据库写入或外部网络。

## 14. 未完成事项

未实现 Planner、PlanStep、Scheduler/DAG、状态机/Runtime Event、Budget、Retry、模型轻重路由、Fallback、Checkpoint/Resume、状态仓库、真实 cancel API 或客户端断开传播。

## 15. 已知风险

- 当前只有 legacy AgentRouter 兼容 Action，真实主链通常只执行一轮。
- 最大步骤、无动作和重复动作主要通过 Fake Driver 验证。
- AgentState 尚未持久化。
- Generator close 尚不能形成可靠终态。
- 真实取消来源尚未接入。

## 16. 设计权衡

默认 8 步为未来多轮留出有限空间；无动作阈值为 1 以避免空转；相同动作允许连续成功两次，在第三次被执行前停止，避免过早拒绝短暂重复同时限制循环。Loop 不理解模型或 provider，路由选择仍由未来 Driver/Planner 边界承担。

## 17. 可用于面试的项目描述

我为 LocalAgent 的遗留流式聊天入口增加了一个可单测的最小 Agent Loop：它把 Router 作为兼容 action 包装，并统一管理 RunContext deadline/取消、AgentState Step 生命周期、最大步骤、无动作和重复动作终止，同时保持既有文本和编排状态流不变。该实现不是完整多步规划 Agent、状态机、Scheduler、Durable Execution、动态模型路由或生产级客户端断开处理。

## 18. 需要带回 ChatGPT 审查的信息

- 真实入口为 `core/runtime/agent_loop.py::AgentLoop.run_stream()`，所有者是单次 generator 中的 AgentLoop；ChatService 调用链现在是 `server.py::chat_endpoint()` → `ChatService.stream_chat()` → `LegacyAgentRouterDriver` + `AgentLoop.run_stream()` → `AgentRouter.chat_stream()`。
- Action、Observation、Policy、计数时机、无动作和重复动作阈值语义见第 7、8、16 节；dedup_key 是受限 opaque identifier，禁止敏感或自由文本。
- Legacy Driver 固定 action、Router 只调用一次，CONTINUE/COMPLETED 与所有失败映射见第 8 节；deadline/cancellation 保持第 3 天的异常传播。
- GeneratorExit 不形成可靠终态；未修改 AgentState schema，未实现任何状态机内容，也未做模型轻重路由。
- 新增/修改文件和测试命令见第 5、6、12 节。Commit 消息为 `Add bounded runtime agent loop`；PR 标题为 `Add bounded stage two runtime agent loop`（由当前 Codex 工作流登记）。需要人工确认未来真实取消来源、状态持久化边界和第 5 天状态机设计。后续建议仅讨论这些边界，不应直接实施第 5 天。

## 19. 重点 Bad Case

### Bad Case 1：[[ORCH]] 污染 final_output

- 类型：真实发现
- 触发条件：Legacy Router 在语义回答前、中或后 yield `[[ORCH]]` 编排状态 chunk，兼容 Driver 直接拼接全部传输 chunk。
- 故障表现：`AgentState.final_output` 会混入 `[[ORCH]]` JSON 或状态协议，导致最终语义回答与客户端传输控制内容未分离。
- 根因分析：初版 Legacy Driver 用单一 `output_chunks` 同时承载客户端流和 final_output 聚合，未区分协议 chunk。
- 修复方案：继续原样 yield 全部 chunk；仅将不以 `[[ORCH]]` 开头的 chunk 追加到 final_output。当前 Router 的 Generator return value 为 `None`，不能作为语义回答来源。
- 回归测试：Fake Router 先 yield `[[ORCH]]status`，再 yield 多个正文 chunk；断言流顺序完整，且 `AgentState.final_output == "plain answer"` 并不含 `[[ORCH]]`。
- 对应知识点：流式传输通道与领域语义结果分离、控制面与数据面分离、协议污染防护。
- 面试表达：我发现遗留流把编排状态和回答正文复用同一传输通道，因此在 Runtime 适配层保持协议原样透传，同时按稳定前缀排除控制事件，确保持久化/状态中的最终回答只保存语义正文。
- 当前状态：已解决。

### Bad Case 2：已经流式输出部分文本，但 Run 最终失败

- 类型：假设构造
- 触发条件：Driver 已 yield 部分文本，随后执行 generator 抛出未知异常或触发终止。
- 故障表现：已发送的文本 chunk 无法回滚；客户端看到部分文本不代表 Run 成功。
- 根因分析：HTTP 流是单向传输，而 Run 终态只能在后续执行结果或异常发生时确定。
- 修复方案：本次保持既有流式协议不变；Loop 捕获未知异常后将 active Step/Run 标为 `FAILED`，不执行成功收尾。第 21 天可通过 Runtime Event 区分 partial、final 和 failed。
- 回归测试：Fake Driver 先 yield `partial text`，然后抛出异常；断言流保留 partial，且 AgentState 最终不是 `SUCCEEDED`。
- 对应知识点：流式一致性边界、部分结果与终态分离、错误可观测性。
- 面试表达：流式响应的部分可见性不等同于事务成功；我保证状态机前的最小 Loop 能在后续失败时记录 FAILED，同时明确未来用事件模型表达 partial/final/failed。
- 当前状态：已通过 Fake Driver 验证当前状态映射；未改变协议，Runtime Event 留待第 21 天。

### Bad Case 3：Generator 关闭后副作用可能重复执行

- 类型：假设构造
- 触发条件：Action 内的 Tool 已产生副作用，随后客户端关闭 generator；用户或上层重新提交相同请求。
- 故障表现：当前 close 不形成可靠终态，重新提交可能重复执行已经产生副作用的操作。
- 根因分析：当前没有真实取消来源、可靠终态持久化、恢复机制或幂等键。
- 修复方案：本次只保留“提前 close 不错误标记成功”的回归测试，不提前实现幂等。第 12 天处理取消终态，第 16～18 天处理恢复，第 19 天使用幂等键防重。
- 回归测试：启动后读取一个 chunk 并 close，断言 Run 未被标记为 `SUCCEEDED`。
- 对应知识点：at-least-once 风险、外部副作用、取消语义、幂等设计与恢复执行。
- 面试表达：当前 Runtime 明确承认 generator close 不能证明未执行副作用；我先防止错误成功，后续按取消、恢复和幂等键的阶段计划处理重复执行风险。
- 当前状态：已保留不错误成功的回归测试；未实现取消终态、恢复或幂等。
