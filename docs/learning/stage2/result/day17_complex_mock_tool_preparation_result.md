# 第 17 天复杂模拟 Tool 准备结果

## 1. 当前 Tool 调用链

当前项目采用轻量 Legacy Tool 链路：

```text
server.lifespan
  -> tools.registry.register_all_tools(router)
  -> AgentRouter.register_tool(name, Callable[[str], str], description)
  -> AgentRouter._plan_tool_call()
  -> LLM 输出 CALL: tool_name(argument_text)
  -> AgentRouter._parse_tool_call()
  -> self.tools[tool_name]["func"](tool_args)
  -> str(observation) 注入最终回答上下文
```

参数在现有链路中是单个字符串，结果也按字符串处理。工具名到函数的映射硬编码在 `tools/registry.py`。Tool Planner 通过提示词选择已注册工具，并仅接受 `CALL:` 或兼容的 `Action:` 格式。真实 Tool 边界外已有 Runtime Budget 计数与调用前后取消检查，但目前没有统一的 Tool Invocation/Context/Result/Error Contract，也没有 Tool Registry 抽象。Runtime 层已有通用 retry、timeout 和 cancellation 能力，本模拟 Tool 没有依赖或改写它们。

测试中的 Fake 通常定义在各测试文件内，以小型 Fake/Scripted 类或注入函数组织；本次延续该方式，使用 Fake Sleeper、Cancellation Probe 和进程内状态对象。

## 2. 新模拟场景

新增“批量变更请求处理流程”模拟器。它没有真实业务意义，不访问网络、数据库、模型、用户文件或外部服务。它用于稳定重现多阶段执行、批量 Item、部分成功、进程内资源冲突、三种副作用模式、幂等回放、提交前失败、提交后失败、取消及补偿。

强类型入口为 `ComplexWorkflowSimulationTool.execute(ComplexWorkflowRequest)`；Legacy 字符串入口为 `complex_workflow_simulator(argument_text)`。

## 3. 文件结构

```text
tools/complex_workflow_simulator.py
  - 输入、输出、阶段及状态枚举
  - ComplexWorkflowSimulationTool
  - InMemoryWorkflowStateStore
  - JsonFileWorkflowStateStore
  - WorkflowResourceLockManager
  - WorkflowSimulationError
  - Legacy JSON 字符串 Wrapper

tools/registry.py
  - 新增 Legacy Tool 硬编码注册

core/agent_router.py
  - 为新 Tool 名称及复杂流程意图补充最小 Planner 门控关键词

tests/test_complex_workflow_simulator.py
  - 复杂模拟 Tool 专项测试

docs/learning/stage2/result/day17_complex_mock_tool_preparation_result.md
  - 本结果文档
```

## 4. 输入模型

`ComplexWorkflowRequest` 包含：

- `operation_id`
- `resource_key`
- `idempotency_key`
- `execution_mode`
- `items`
- `failure_injection`
- `failure_stage`
- `failure_item_id`
- `processing_options`
- `metadata`

`WorkflowItem` 包含 `item_id`、`action`、`quantity`、`priority`、`attributes`。Item ID 必须唯一，结果始终恢复到输入顺序。

`WorkflowProcessingOptions` 包含 `max_parallel_items`、`processing_delay_ms`、`allow_partial_success`、`enable_compensation`。并发数限制为 1 至 16，延迟限制为 0 至 5000 ms，整数参数显式拒绝 `bool`、负数、浮点数和异常大值。测试通过注入 Sleeper 避免真实长等待。

操作 ID、资源 Key 和幂等 Key 必须是非空、最大 128 字符的稳定安全标识，不会被随机值覆盖。`IDEMPOTENT_COMMIT` 必须提供幂等 Key。

## 5. 输出模型

`ComplexWorkflowResult` 包含：

- `operation_id`
- `resource_key`
- `idempotency_key`
- `execution_mode`
- `status`
- `completed_stages`
- `item_results`
- `side_effect_committed`
- `compensation_attempted`
- `compensation_succeeded`
- `idempotency_replayed`
- `audit_digest`
- `safe_error_code`
- `safe_message`
- `duration_ms`

结果状态包括 `SUCCEEDED`、`PARTIALLY_SUCCEEDED`、`FAILED`、`CANCELLED`、`TIMED_OUT`、`IDEMPOTENCY_REPLAY`。

每个 `WorkflowItemResult` 只包含 `item_id`、`status`、`safe_code`、`attempted`、`side_effect_committed`，不包含原始异常或 Item attributes。

## 6. 执行阶段

成功路径显式记录：

```text
VALIDATE_REQUEST
LOAD_EXISTING_STATE
ACQUIRE_RESOURCE
CREATE_SNAPSHOT
PREPARE_ITEMS
PROCESS_ITEMS
VALIDATE_PROCESSED_ITEMS
COMMIT_SIDE_EFFECTS
CREATE_AUDIT_RECORD
FINALIZE
RELEASE_RESOURCE
```

失败路径可增加：

```text
COMPENSATE_COMMITTED_CHANGES
FINALIZE_FAILURE
RELEASE_RESOURCE
```

每条阶段记录只保存阶段、UTC 起止时间、安全状态、安全代码和已处理 Item 数量，不保存完整输入、异常正文或敏感元数据。

## 7. 副作用分类

- `DRY_RUN`：执行完整校验、Item 处理和审计摘要计算，但不修改 State Store，不产生 committed operation、持久审计或资源版本变化。
- `IDEMPOTENT_COMMIT`：向显式模拟 Store 提交本地记录，并保存可回放的幂等结果。
- `NON_IDEMPOTENT_SIMULATION`：每次调用均追加新的本地模拟提交记录；注册描述明确要求只能显式选择。

所有副作用仅存在于注入的内存 Store 或调用方显式传入目录中的单一 JSON 状态文件。

## 8. Idempotency 行为

请求会生成不保留 metadata 或 attributes 明文的确定性摘要。

- 相同 Key、相同请求：返回历史安全结果，状态改为 `IDEMPOTENCY_REPLAY`，不重复提交。
- 相同 Key、不同请求：返回 `TOOL_IDEMPOTENCY_CONFLICT`，不提交。
- 提交前 transient/timeout/validation 等失败：不占用幂等 Key，可安全修正或再次调用。
- 已成功提交、提交后失败或已尝试补偿：保存最终幂等结果，避免不明确状态下重复提交。
- `DRY_RUN` 和 `NON_IDEMPOTENT_SIMULATION` 不读取或写入幂等记录。

## 9. Resource Key 与冲突

`WorkflowResourceLockManager` 使用线程安全的进程内非阻塞互斥集合：

- 相同 `resource_key` 的重叠调用返回 `TOOL_RESOURCE_CONFLICT`。
- 不同 `resource_key` 可以并发。
- 成功、异常、取消和补偿结束后均释放资源。
- 不实现等待队列、跨进程锁、分布式锁或正式 Resource Scheduler。

## 10. 批量 Item 与并行

Item 通过上限为 16 的 `ThreadPoolExecutor` 受控并行。Future 完成顺序不会影响输出顺序。单个 Item 异常会转换为安全 Item 结果，不会丢失其他 Item 结果。

- `allow_partial_success=True`：成功 Item 可以提交，整体返回 `PARTIALLY_SUCCEEDED`。
- `allow_partial_success=False`：任一 Item 失败即在提交前返回 `TOOL_PARTIAL_FAILURE`。

模拟器没有改造项目为 async，也没有创建无界线程。

## 11. Failure Injection

支持以下确定性注入：

- `NONE`
- `TRANSIENT_BEFORE_SIDE_EFFECT`
- `TIMEOUT_BEFORE_SIDE_EFFECT`
- `VALIDATION_ERROR`
- `RESOURCE_CONFLICT`
- `PARTIAL_ITEM_FAILURE`
- `FAIL_AFTER_SIDE_EFFECT`
- `COMPENSATION_FAILURE`
- `UNKNOWN_FAILURE`

`failure_stage` 可把提交前 transient/timeout/unknown 注入延迟到指定检查阶段，`failure_item_id` 可精确选择失败 Item。Legacy JSON 同时接受字符串形式和包含 `type`、`failure_stage`、`failure_item_id` 的对象形式。没有随机概率。

## 12. 取消检查点

通过可注入的 `Callable[[], bool]` 检查：

- 获取资源前
- 调度每个 Item 前
- 提交副作用前
- 补偿前
- 最终返回前

副作用前取消返回 `CANCELLED` 且 `side_effect_committed=False`。提交后取消不会伪装为未执行；启用补偿时先完成安全补偿，禁用补偿时保留 `side_effect_committed=True` 供调用方人工确认。本次未接入正式 `CancellationToken`。

## 13. 提交后失败

`FAIL_AFTER_SIDE_EFFECT` 在本地 Store 完成提交后、返回结果前触发 `TOOL_SIDE_EFFECT_FAILURE`。

禁用补偿时，结果明确报告：

```text
status = FAILED
side_effect_committed = True
compensation_attempted = False
```

因此调用方不能仅根据“收到失败”推断操作没有发生，也不能对非幂等模式自动 Retry。

## 14. 补偿流程

启用补偿时，提交后失败进入 `COMPENSATE_COMMITTED_CHANGES`，恢复提交前资源版本并标记原提交已补偿。

- 补偿成功：`compensation_attempted=True`、`compensation_succeeded=True`、`side_effect_committed=False`。
- 补偿失败：返回 `TOOL_COMPENSATION_FAILURE`，保留 `side_effect_committed=True`，明确表示需要人工处理。

补偿记录只保存操作 ID、资源 Key、安全状态和安全代码。

## 15. 安全错误与输出边界

安全错误代码覆盖：

```text
TOOL_VALIDATION_ERROR
TOOL_RESOURCE_CONFLICT
TOOL_TRANSIENT_FAILURE
TOOL_TIMEOUT
TOOL_CANCELLED
TOOL_IDEMPOTENCY_CONFLICT
TOOL_PARTIAL_FAILURE
TOOL_SIDE_EFFECT_FAILURE
TOOL_COMPENSATION_FAILURE
TOOL_UNKNOWN_FAILURE
```

`WorkflowSimulationError` 只保存安全代码、安全消息、阶段、操作 ID、副作用状态和补偿状态。未知 Python 异常会被转换为固定 `TOOL_UNKNOWN_FAILURE` 或安全 Item 失败，不把 `repr(exception)`、traceback、Prompt、metadata、attributes 或 Secret 放入结果。

`audit_digest` 是对最小安全摘要的 SHA-256，不含完整输入。JSON Store 只持久化资源版本、模拟提交摘要、幂等安全结果、审计摘要和补偿摘要，使用同目录临时文件加 `os.replace` 原子替换。

## 16. Legacy 接入方式

Legacy Tool 名称：

```text
complex_workflow_simulator
```

`tools.registry.register_all_tools()` 继续使用现有硬编码映射，把 Wrapper 注册为 `Callable[[str], str]`。Wrapper 接受一个 JSON object 字符串，解析为强类型请求，并返回固定 JSON 字符串；无效 JSON 或参数只返回安全 validation 结果。

`AgentRouter._tool_intent_likely()` 仅增加了新 Tool 名称、英文复杂流程表达和中文“复杂流程/模拟工具”关键词，使请求可以进入现有 Planner；`CALL: complex_workflow_simulator(...)` 继续由原解析器处理。

复杂参数由当前小型 Tool Planner 稳定生成并不现实，因此同时保留强类型 Python 直接入口。专项测试会经过 Legacy Wrapper、现有注册函数、Planner 意图门控和 `CALL:` 解析器，但没有为此重写 Planner 或 Tool 系统。

## 17. 本次明确没有实现的第 17 天内容

本次没有实现或修改：

- `ToolInvocation`
- `ToolExecutionContext`
- `ToolExecutionResult`
- 统一 `ToolExecutionError`
- Tool Registry 抽象
- Skill、MCP、A2A
- `RunCoordinator`、`AgentState`、`RunStatus`、`StepStatus`
- Runtime Event 接入
- 正式 Budget 接入
- 正式 `CancellationToken`
- 正式 Tool Retry、Tool Timeout、Resource Scheduler
- Fallback 决策
- 真实网络、数据库、模型或外部服务调用

## 18. 测试命令和结果

执行命令：

```text
uv run python -m pytest tests/test_complex_workflow_simulator.py -q
uv run python -m pytest <现有 Tool/AgentRouter 相关回归> -q
uv run python -m pytest -q
uv run python -m compileall -q core tools tests
git diff --check
```

执行结果：

- 专项测试：`31 passed in 0.35s`
- 现有 Tool/AgentRouter/Runtime 相关回归：`79 passed, 7 subtests passed in 5.70s`
- 全仓回归：`359 passed, 42 subtests passed in 6.20s`
- `compileall`：通过，无输出
- `git diff --check`：通过，无错误

以上为本次工作区在 2026-07-24 的实际执行结果；未伪造或跳过失败。

## 19. 已知限制

- 资源锁与内存状态只在单进程内有效。
- JSON Store 由调用方负责提供隔离、可清理的临时目录；它不是生产数据库。
- JSON Store 不提供跨进程事务、锁恢复或损坏自动修复。
- Item 并行是同步调用内的受控线程池，不是正式 Tool 调度器。
- 模拟 timeout 是确定性故障注入，不是正式 Runtime Deadline。
- Cancellation Probe 是测试接缝，不是正式 Token Contract。
- 当前 Legacy Planner 的自由文本协议和较小输出上限不适合稳定构造大型 JSON；可靠验证应使用强类型入口或直接调用 Wrapper。

## 20. 需要带回 ChatGPT 的信息

- 当前 Legacy Tool 边界是 `Callable[[str], str]`，参数与结果均为文本。
- 模拟器已经提供未来 Contract 需要映射的 operation ID、resource key、idempotency key、execution mode、阶段记录、副作用状态、补偿状态和安全错误。
- 第 17 天改造时应复用这些业务语义，不应把本模拟器内部类型直接冒充统一 Runtime Contract。
- 自动 Retry 必须结合 execution mode、幂等 Key、`side_effect_committed` 与补偿状态判断；尤其不能自动 Retry `NON_IDEMPOTENT_SIMULATION` 或提交状态不明确的失败。
- Resource Scheduler、正式 Timeout/Cancellation/Budget/Event 应由 Runtime 统一实现，不能继续下沉到该模拟 Tool。
