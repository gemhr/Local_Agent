# Stage 2.5 Trace Contract v1

> 状态：WP5 冻结的 Trace Contract v1 语义合同，正式分类为 `PUBLIC_VERSIONED`。
> 实现 owner：`core/runtime/trace_contract.py`（六个稳定 operation 与属性常量）、
> `core/runtime/tracing.py`（span 原语与安全属性 allowlist）。Span 的实际创建
> 仍由各运行 owner 完成，`trace_contract.py` 不创建与 Event/Trace/Reducer/
> Coordinator 竞争的第二套 owner。
>
> 内部 Span 模型（`TraceContext` / `SpanHandle` / `SpanRecord`）分类为
> `INTERNAL`，不是公共 exporter payload。公共消费边界是 WP4-A 新增的
> consumer-neutral export contract（`core/runtime/trace_export_contract.py` +
> `core/runtime/trace_contract_fingerprint.py`，分类 `PUBLIC_VERSIONED`）：
> 只接受已完成且通过严格校验的 `SpanRecord`，投影为不可变 `TraceExportEnvelope`；
> 详见本文档 §10 与 `runtime_architecture_v1.md` / `runtime_owner_matrix.md`。

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

- `trace_id`（记录自带）、`run_id`（记录自带）
- `runtime_mode`、`plan_id`、`plan_version`、`plan_fingerprint`
- `planning_source`、`step_count`、`selected_entry_agent_id`、`shape`
- `final_status`、`stop_reason`
- `runtime_version`、`prompt_version`、`model_config_hash`、`toolset_hash`、
  `kb_version`：内部占位字段，当前恒为 `not_configured`。它们**不是**真实版本
  归因，**不**构成 Run Configuration Fingerprint，也**不**属于 WP4-A 公共导出
  （INTERNAL_ONLY，见 §10）。
- `session_id`：内部候选键，当前无 production writer，不进入公共导出。

`status`、`error_code`、`duration_ms` 是 `SpanRecord` 顶层字段，不是 attribute
map；`safe_error_code`/`duration_ms` 不以 attribute 形式重复导出。

禁止：

- `user_request`、Planner raw output、Prompt 全文、final 正文、
  specialist 正文、文件路径、exception 原文、Memory 正文。

## 3. Planning span 属性

允许：`planning_source`、`schema_version`、`planner_model_invoked`、
`compiled_shape`、`specialist_count`、`synthesis_required`。

`planner_attempt_count`、`planner_timeout_source` 为内部候选键，当前无
production writer，不进入 WP4-A 公共导出（INTERNAL_ONLY）。

禁止记录 raw model response 与 instruction。

## 4. Step span 属性

每个 Step 独立 `runtime.step`：

- `step_id`（记录自带）
- `preferred_agent`、`execution_kind`、`output_policy`、`dependency_count`
- `state`（typed 管道完成态/error code）、`result_char_count`（typed 管道）

`invocation_role`、`content_type` 为内部候选键，当前无 production writer，
不进入 WP4-A 公共导出（INTERNAL_ONLY）。

不记录 result 正文。

## 5. Model / Tool / Retrieval span 属性

低层 model invoke/attempt、tool invoke/attempt、retrieval execute/stages span
继续作为内部可观测 span 存在，分类为 `INTERNAL_RC` extension operation：它们
**不是** PUBLIC_VERSIONED 顶层 operation，不得升级为第七个稳定 operation。
WP4-A 公共 export 投影当前对扩展 operation 返回固定 `UNSUPPORTED_OPERATION`
（fail-closed），即扩展 span 当前不被导出。

当前 `SAFE_SPAN_ATTRIBUTES` 不含 model token/usage、model cost 或 retrieval
latency 键，因此这些字段**没有稳定实现**，不得描述为已导出/已支持。`kb_version`
仅由 Run root 以 `not_configured` 占位写入，不是真实 KB 版本归因。低层 span 只
记录各自 allowlist 允许的安全事实（如 `requested_top_k`、`returned_count` 与
安全统计）。

Tool 参数与返回值不得直接写入 Trace；Retrieval 禁止写入 query/chunk 正文。

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

当前五个配置归因键（`runtime_version`/`prompt_version`/`model_config_hash`/
`toolset_hash`/`kb_version`）在 Run root 恒为 `not_configured` 占位：它们只是
内部记录事实，不构成 Run Configuration Fingerprint，也不属于 WP4-A 公共导出。

## 10. Export Contract 边界（WP4-A）

```text
内部已完成 SpanRecord
→ 严格 consumer-neutral 安全 export 投影（project_span）
→ contract identity/version + Trace Contract Fingerprint
→ 不可变 TraceExportEnvelope
→ WP4-B exporter transport（WP4-B 已完成）
→ WP4-C AgentEvalOps adapter（WP4-C 已完成；真实本地跨系统 E2E VERIFIED）
```

> 历史状态说明：上述 export 流水线在 Stage 2.5 冻结时仅到
> `TraceExportEnvelope`，WP4-B transport 与 WP4-C AgentEvalOps adapter 当时是
> 未来工作。后续 Stage 3 WP4-B（exporter transport）与 WP4-C（AgentEvalOps
> 最小 HTTP adapter 与 ingest API）均已实现并通过独立 Gate，真实本地跨系统
> E2E（首写 201 / 精确 replay 200 / 冲突 409）已 VERIFIED。本文档保持其
> Stage 2.5 历史合同上下文，不重写为 Stage 3 文档。

- export identity：`localagent.runtime.trace_export`
  （`TRACE_EXPORT_CONTRACT_IDENTITY`）
- export contract version：`1`（`TRACE_EXPORT_CONTRACT_VERSION`，与
  `RUNTIME_TRACE_CONTRACT_VERSION` 是两个独立事实）
- Trace Contract Fingerprint：算法 `sha256` + canonical encoding
  `canonical_json_v1`，lowercase 64-hex；唯一 canonicalize/digest owner 为
  `TraceContractFingerprinter`，权威规范语义描述符由 Consumer-neutral Trace
  Export Contract Semantic Owner（`core/runtime/trace_export_contract.py` 的
  `export_contract_semantic_descriptor()`）构建，指纹模块不维护第二份
  field/domain/policy literals
- 指纹覆盖：contract identity/version、`RUNTIME_TRACE_CONTRACT_VERSION`、
  六类 operation（category/step-bound）、common field presence/type/domain 规则、
  terminal `SpanStatus` 词汇表、OK/non-OK error-code 语义、六类 category 的
  type/presence/value-domain 规则、扩展拒绝策略、unknown attribute 行为
  （projection=OMIT / direct=INVALID）、metadata-first security 策略、
  compatibility 行为与 `CompatibilityReason` 词汇表（含 `ENVELOPE_INVALID`）；
  实例值（run/trace/span/step ID、时间、duration、status/error outcome、
  `delivery_status=DELIVERED` 等运行期值）不进指纹
- 只有已完成记录可导出：`completed_at`/`duration_ms` 存在、status 非
  `UNSET`、UTC 时间合法、duration 有限非负；不虚构终态
- 分类：Trace Contract v1 = `PUBLIC_VERSIONED`；consumer-neutral export
  contract = `PUBLIC_VERSIONED`；Trace Contract Fingerprint = `PUBLIC_VERSIONED`
- `SAFE_SPAN_ATTRIBUTES` ≠ 公共导出集合：公共导出使用六类 operation/category
  严格 schema；未知内部键默认省略；五个 `not_configured` 占位不导出
- 兼容判断：`TraceCompatibilityEvaluator`（已知 contract → ACCEPT；
  identity/version/fingerprint 缺失、畸形或不支持 → REJECT；
  已知 identity/version/fingerprint 但 envelope 语义非法 → REJECT
  （`ENVELOPE_INVALID`））
- 指纹版本：当前指纹取代 pre-Gate 历史值，**不**触发 export contract version
  bump（`TRACE_EXPORT_CONTRACT_VERSION` 仍为 1）——v1 从未通过 Final Gate、
  从未发布，无旧指纹兼容义务；代码仍是指纹值权威 Owner，文档不硬编码 digest
