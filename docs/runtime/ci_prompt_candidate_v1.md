# CI Prompt 候选与受控合成评测 v1

候选工件 `ci-prompt-candidate.v1` 使用现有 catalog canonical JSON + SHA-256；含 base/content/manifest digest、来源引用、diff、rollback reference。它只是 SYNTHETIC 优化假设，不声明已定位 Prompt 根因。freeze 使用 exclusive create，不能覆盖原文件。

既有 `LOCAL_AGENT_QUALITY_EVALUATION_CONFIGURATION_PATH` 指向的受信部署配置可增加 `ci_prompt`，结构为 `asset`、`approved_manifest_digest`、`mode`（BASELINE/CANDIDATE）。审批摘要由运维配置，不来自 HTTP；路径也不来自普通请求。没有该段时旧配置/默认 Prompt 保持；不符摘要或非法字段启动失败，不回退旧 Prompt。

`server.py::lifespan` 在注册表冻结前调用 configured_bundle，产生仅替换 CI definition 的本地 bundle；不修改模块常量或全局 Instructions。运行后不热重载，变更需新工件/审批及应用重启。既有 Quality manifest 还须 pin 实际选中 AgentDefinition 与 runtime config。源资产正文不得通过 Go 的普通 Evidence 无条件传播。

`DurableExecutionRepository.start_model_attempt` 在原 fenced 事务中，对 CI 配置下实际 evaluation_messages 的 system instructions 作匹配与内容摘要检查；证据写入原 quality receipt capture 的可选 ci_prompt_binding。错误消息回滚该次 model start，测试确认没有遗留 capture。旧配置没有 CI pin 时不增加字段。

该记录的 scope 为 DURABLE_REQUEST_PREPARED：证明传入生产 model-start 的请求已构造并完成本次事务准入，不证明 HTTP 已发送、远端已处理或模型内部消费。原 model_request_digest、Prompt内容、messages、manifest 是不同摘要。

## 隔离部署与配置

BASELINE/CANDIDATE 分别使用固定进程和受信配置，不支持在线切换。配置任一 `ci_prompt` 选择时，`server.py` 标记 `ci_evaluation_only`，认证后拒绝所有 HUMAN `/api/` 请求，避免通用入口间接触达替换后的 CI Definition。未配置时维持普通部署行为。不要把评测进程部署到普通用户流量入口。

现有三项设置 `LOCAL_AGENT_QUALITY_EVALUATION_PROJECT_ID`、`LOCAL_AGENT_QUALITY_EVALUATION_SERVICE_PRINCIPAL_ID`、`LOCAL_AGENT_QUALITY_EVALUATION_CONFIGURATION_PATH` 必须齐备。配置文件仍包含原严格 `manifest` 与 `capture_profile`；增加上述 `ci_prompt` 及 `ci_synthetic`。后者严格包含 `tenant_id`、`frozen_ci`、`attempts`；`attempts` 按 BASELINE/CANDIDATE pin 经批准的 Attempt UUID。`frozen_ci` 字段见下节。不得使用测试配置替代正式执行配置。

专用 SERVICE 由既有身份系统管理员配置，同时具有 `localagent:evaluation:execute` 和 `localagent:ci-synthetic:evaluate`，Tenant 从数据库认证上下文取得。代码不会创建正式身份、授予 scope 或创建 EO Project。

## 独立 HTTP 合同

POST `/api/runtime/evaluation-execute/ci-synthetic/v1` 严格请求为：

```text
contract_version = ci-synthetic-evaluation-execute.v1
quality = 原 AgentQuality v1 请求（query 固定，run_id = attempt_id）
frozen_ci = {case_id, case_version, case_digest, template_id, template_version, input_digest}
candidate_manifest_digest = 已批准候选摘要
mode = BASELINE | CANDIDATE
```

仅允许固定 `report_pipeline_failure` v2 的公开合成 FAIL，输入摘要由 AC 对真实 `initial_state` 计算。调用者不能提供业务初始状态、现有 Incident/Investigation、Owner、Tenant、Prompt 或路径。Case 发布内容与其 Identity 分离：EO Case Input 仅为 `{template_id,template_version,input_digest}`，Case Identity 的 ref/version/content digest 另外冻结，避免摘要自引用。

先校验权限及固定配置，再通过现有容量与 admission gate。`core/incidents/evaluation.py::prepare` 在同一 PostgreSQL 事务中，固定先锁 SERVICE User，再复用 Quality receipt session helper，创建 Instance、Execution、FAIL Incident、Investigation 与 Binding。Prepare 不 claim Runtime、不发模型请求。提交 winner 才建立原 RUN ownership，进入原 Runtime V2；Session 沿用 Quality UUIDv5 算法，Root=Attempt。原 CI Tool `bound_execution`、报告 `validate_report/project_if_terminal` 保持原语义。

同 Attempt 同内容仅查询；不同内容返回冲突。每个受信 SERVICE/Project/Mode 只准入一次。pre-Root 崩溃返回 Pending/UNKNOWN，不能借重复请求重新执行；已有 Root 仅由原 Runtime Recovery 处理。网络未知不得自动重发模型请求。

GET `/api/runtime/runs/{run_id}/ci-synthetic-evidence/v1` 复用相同 SERVICE/Project/Owner 授权，只查询及幂等封存。未就绪返回 202，含 `QUERY_ONLY_NO_REEXECUTE`；终态返回 `{contract_version, quality, sidecar, sidecar_digest}`。原 Quality 对象不新增 CI 字段。Sidecar schema 为 `ci-synthetic-evidence.v1`，包含冻结绑定、原 Quality sealed response digest、Runtime status、模型 request/messages/system/prompt 摘要与引用。`report_digest` 为原 Investigation 的 runtime_result_digest，可为 null；不能据此宣称报告已完成。

Binding 表 `ci_synthetic_evaluation_bindings` 的主键及 FK 固定唯一 Attempt 与业务对象；数据库触发器禁止 DELETE、修改绑定及覆盖已封存 Sidecar，仅允许 sidecar NULL→首次封存。Migration `0041_ci_synthetic_evaluation` 仅新增该表/约束/触发器，不新增 Run 状态或执行 Claim。应用回滚应保留表及证据；downgrade 会删表，只适用于获准空库/测试清理，不能用于有历史证据的生产回滚。

Go `CISyntheticTarget` 独立校验 Sidecar，并复用原 Quality v1 Strict Decoder/验证器。缺证、超时、202、网络未知均不能作为成功。没有通用资产平台、新模型设施或热更新；当前测试 Provider 的 HTTP/PG 验证不代表真实模型执行成功。
