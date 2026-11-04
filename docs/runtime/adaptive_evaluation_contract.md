# CI 自适应执行评测合同 v1

## 入口与权限

`POST /api/runtime/evaluation-execute/adaptive/v1` 使用 `adaptive-execution-evaluate.v1` 请求，返回 `adaptive-execution-evidence.v1`。`GET /api/runtime/runs/{run_id}/adaptive-evidence/v1` 只读取原回执，不触发执行或恢复。尚无完整终态回执时返回 202，客户端不得推断远端成功或失败。

默认关闭。由 `LOCAL_AGENT_ADAPTIVE_EVALUATION_CONFIGURATION_PATH` 指向本机 UTF-8 JSON 配置，应用启动时读取并严格验证；非法配置使启动失败。配置不得包含密钥，其结构为：

```text
version = adaptive-evaluation-config.v1
project_id / principal_id / tenant_id
cases[case_id] = {case_version, case_digest, dataset_digest,
  input: {slice: STATUS|DIAGNOSIS, incident_id, execution_id, execution_digest}}
```

`execution_digest` 是 `{execution_id,state_revision,summary}` 的现有 catalog canonical JSON SHA-256。Case/Dataset digest 必须取自 AgentEvalOps 已发布目录，不能自行代算版本内容。请求还须携带 `input_digest`、EvaluationRun/Attempt/request UUID、`group`、`policy_digest`、`timeout_milliseconds`（1–300000）。所有对象拒绝未知字段，digest 为小写十六进制 64 字符。

该专用入口只接受配置指定的 HUMAN 用户、tenant、project 和冻结 Case；保留 JWT、原 CI 对象权限、Run Ownership、原工具治理及原限流边界。SERVICE 评测身份不能伪装 HUMAN。普通聊天/业务 options 不接受策略覆盖。旧 `/agent-loop/v1` 的 SERVICE scope 与严格 v1 合同保持原样。`ci_evaluation_only` 模式仍拒绝 HUMAN，不能使用本入口。

## 策略和事实 Owner

策略 `ci-adaptive-evaluation.v1` 的 digest 由 `{version,group,query_mode,diagnosis_stop}` 生成：

| group | STATUS | DIAGNOSIS |
|---|---|---|
| BASELINE | 原普通 Loop | 原调查 Loop，关闭动态停止 |
| CANDIDATE_A | 原 Fast Path | 原调查 Loop，关闭动态停止 |
| CANDIDATE_B | 原 Fast Path | 原调查 Loop，启用原 WP2 策略 |

Core Run ID 等于 Attempt ID；复用唯一准入 Receipt，重复请求只读回执。身份关联、冻结请求及策略存入原 Receipt；初始化事务核对实际 Agent、engine、route 或 frozen stop selection。调查仍通过原 `IncidentService.admit`，评测使用独立 Attempt 会话，避免上一次调查的聊天输出进入下一次 Seed。用户及项目级 Memory 若改变，原 Seed 摘要会改变，比较必须阻断。

`fixed-plan.v1` / `durable-agent-loop.v2` 是 engine policy，`FAST_PATH` / `AGENT_LOOP` 是策略模式。路由取自原 `resume_input.adaptive_route`；FINAL_ONLY 逐字段复制原 `RuntimeLoopFrameRow.adaptive_decision`，不生成 CONTINUE 或新执行授权。

## 回执

共同字段：`schema_version`、`identity`、`run_id`、`comparability`、`strategy`、`runtime`、`business`、`metrics`、`mode_evidence`。identity 保留 project、EvaluationRun、Attempt、request、Case ID/version/digest、Dataset digest、input digest 与 Core Run。

- Fast：`fast={step_count,tool_refs}`，`loop=null`；没有 Frame 或模型记录。交付依据原 client event feed 的合法 `run.completed`，不能仅凭 Run SUCCEEDED。
- Loop：`fast=null`，`loop={frames,tool_actions}`。Frame 包含原 config/seed digest、轮次、Observation 数量、可空原 Stop Decision；每轮导出编号、状态、decision 引用与类型、Observation cursor/digest/source digest。工具 action 关联实际 Invocation 和 started 事实。没有正文、参数或 Trace 文本授权。
- runtime 与 business 独立：Runtime 失败仍导出已发生调用；查询经过原输出 Schema；调查必须经过原业务 Validator/投影。非法业务输出不作为合法答案交付。
- comparability 冻结实际模型 profiles/adapter/routing 配置、已解析工具 schema/spec/adapter 摘要、授权摘要、超时、业务输入摘要。调查另带真实初始 Seed digest。恢复读取原摘要，不重新选择策略。

本协议不改变原 Journal、Trace、Snapshot、Loop 证据或业务状态机；复用原 Receipt 表，不新增评测数据库体系。

## 指标与缺失

模型次数只计原 ModelInvocation `started_at` 非空的记录；工具次数只计原 ToolInvocation `started_at` 非空的记录。另保留原模型状态，以及 PREPARED/STARTED/COMMITTED/UNKNOWN/NOT_COMMITTED 工具状态。COMMITTED replay 不新建启动，不重复计数。Loop 轮次与实际 Model Attempt 数可以不同，不将 FINAL 轮次当总调用次数。

Provider Usage 只读原 `usage_evidence`，全部已启动模型的 input/output/total 都可用时才汇总；另报可用次数。实际费用目前没有可信账单来源，始终 null，绝不使用 BudgetLedger estimated cost_units。Fast 零模型次数是已知零，缺失费用仍为 null。

Model/Tool duration 是各已启动调用已知耗时之和，存在重叠，不能相加当端到端耗时。Router、Admission、Delivery、首次有效响应及 TTFT 暂无可靠独立计时，保留 null；未建设新的追踪框架。

客户端端到端 RT、全量 Attempt 状态覆盖、分位数与质量判分由 AgentEvalOps 拥有，见该仓 `docs/contracts/adaptive_execution_evaluation_v1.md`。这不是浏览器感知 RT；受控模型与小样本不能证明真实 Provider 性能收益。
