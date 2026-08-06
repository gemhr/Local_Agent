# Stage 2.5 Trace Contract v1

> 状态：WP5 冻结。供 AgentEvalOps 后续消费的 Stage 2.5 安全 Trace Schema。
> 实现 owner：`core/runtime/trace_contract.py`（常量与属性助手）、
> `core/runtime/tracing.py`（span 原语）。Span 的实际创建仍由各运行 owner
> 完成，`trace_contract.py` 不创建与 Event/Trace/Reducer/Coordinator
> 竞争的第二套 owner。

## 1. Span 命名

| Span | operation | 创建 owner | 父节点 |
|---|---|---|---|
| Run root | `runtime.run` | RunCoordinator.execute | 无（trace 根） |
| Planning | `runtime.planning` | RunCoordinator.execute | Run root |
| Step | `runtime.step` | ParallelExecutor worker | Run root |
| Synthesis | `runtime.synthesis` | MultiAgentDriver | Step span（synthesis step） |
| Output delivery | `runtime.output_delivery` | OutputGate.attempt_publish | 当前 Step span |
| Final Memory commit | `runtime.final_memory_commit` | RunFinalMemoryWriter | 当前 Step span |

Shape 3 并行 specialist span 必须满足：

- 每个 specialist 独立 `runtime.step` span，parent 为 Run root span，彼此是
  sibling，不嵌套；
- synthesis span 在依赖 specialist span 结束后开始；
- 不得伪造成嵌套 specialist。

## 2. 安全属性（Run root）

允许：

- `trace_id`（记录自带）、`run_id`（记录自带）、`session_id`（如可用）
- `runtime_mode`、`plan_id`、`plan_version`、`plan_fingerprint`
- `planning_source`、`step_count`、`selected_entry_agent_id`
- `runtime_version`、`prompt_version`、`model_config_hash`、`toolset_hash`、
  `kb_version`（当前未配置项显式写 `not_configured`，不虚构版本）
- `final_status`、`stop_reason`、`safe_error_code`、`duration_ms`

禁止：

- `user_request`、Planner raw output、Prompt 全文、final 正文、
  specialist 正文、文件路径、exception 原文、Memory 正文。

## 3. Planning span 属性

允许：`planning_source`、`schema_version`、`planner_model_invoked`、
`planner_attempt_count`（如可用）、`planner_timeout_source`（如可用）、
`compiled_shape`、`specialist_count`、`synthesis_required`、
`duration_ms`、`status`、`safe_error_code`。

禁止记录 raw model response 与 instruction。

## 4. Step span 属性

每个 Step 独立 `runtime.step`：

- `step_id`（记录自带）
- `preferred_agent`、`execution_kind`、`output_policy`、`invocation_role`
- `dependency_count`、`content_type`、`result_char_count`、`state`
- `duration_ms`、`safe_error_code`

不记录 result 正文。

## 5. Model / Tool / Retrieval span 属性

沿用现有 typed 合同，补齐：

- `owning_step_id`（记录自带 `step_id`）、`owning_agent_id`、
  `invocation_role`、`attempt_index`、`duration_ms`、安全 status/error、
  token/usage 等已有安全字段。

Tool 参数与返回值不得直接写入 Trace；Retrieval 只记录：

- `requested_top_k`、`returned_count`、`latency`、`kb_version`
- grounded/citation 安全统计（如已有）

禁止写入 query/chunk 正文。

## 6. Output delivery span 属性

`runtime.output_delivery`：

- `final_step_id`、`output_policy`、`delivery_status`
- `gate_terminal_state`、`publish_attempt_count`（只能 0 或 1）
- `partially_persisted`、`output_char_count`、`safe_error_code`、
  `duration_ms`

禁止记录正文和 digest 以外的敏感信息。

## 7. Final Memory span 属性

`runtime.final_memory_commit`：

- `persist_enabled`、`entry_agent_id`、`memory_scope`、`delivery_status`
- `user_write_status`、`assistant_write_status`、`transaction_used`
- `safe_error_code`、`duration_ms`

禁止记录消息内容。

## 8. 安全属性实现

`tracing.SAFE_SPAN_ATTRIBUTES` 是唯一 allowed set；`DENIED_SPAN_ATTRIBUTES`
包含 prompt、messages、user_input、model_output、tool_arguments、
tool_output、query、rag_chunk、memory、secret、api_key、provider_url、
canonical_path、exception_message、traceback、idempotency_key、
resource_key。`set_span_attributes` 对非法属性隔离，不改变 Runtime 行为。

## 9. 版本归因字段

缺失的版本字段允许：

- 从已有配置事实安全生成；
- 或显式写 `unknown/not_configured`；
- 不得虚构版本。
