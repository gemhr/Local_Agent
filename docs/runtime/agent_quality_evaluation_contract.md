# Agent Quality Evaluation 执行合同

状态：`SUPPORTED`（明确的公开合成任务与精确投影范围）。WP6 定向核心验收已通过；Sol A Final Gate 待执行，不代表生产 cutover。正式实施 Authority 为 AgentEvalOps 的 `170_sol_wp6_agent_quality_evaluation_architecture_decision.md` 及176局部裁决。

## 配置与准入

新入口默认关闭。必须同时设置 `LOCAL_AGENT_QUALITY_EVALUATION_PROJECT_ID`、`LOCAL_AGENT_QUALITY_EVALUATION_SERVICE_PRINCIPAL_ID`、`LOCAL_AGENT_QUALITY_EVALUATION_CONFIGURATION_PATH`。配置文件为 UTF-8 JSON，顶层仅 `manifest` 和 `capture_profile`。结构见 `core/agent_platform/quality_configuration.py`；不包含凭据、Prompt 或 Ground Truth。

Manifest pin 包括实际代码摘要、公开 Fixture 版本、Agent 定义摘要、部署 Runtime config 摘要及显式 `memory_effect_policy=SERVICE_PROJECT_READ_ONLY.v1`。POST 核对 Registry 定义和部署 pin；T0 在原 execution repository 事务中核对实际冻结 config。部署 config 摘要仅排除 Delegation 的逐 Run `root_source_digest`，完整 Frame pin、Root Memory Binding 与恢复校验保持原样。配置漂移拒绝执行，不自动更新历史 Run。

`POST /api/runtime/evaluation-execute/agent-quality/v1` 使用 `localagent-agent-quality-evaluation-execute.v1`；严格请求字段见 `QualityEvaluationRequest`。`run_id == attempt_id`，Session 从 Attempt 派生。必须是实际 SERVICE Principal、匹配的单 Project/Service 部署映射及 `localagent:evaluation:execute`。请求不能提供 Principal、Memory Scope、Policy、Provider Script 或 Ground Truth。

## Receipt、恢复和查询

Migration `0035_agent_quality_receipts` 新增 `runtime_agent_evaluation_receipts`。远端 Run、Attempt、Request 唯一，完整关联和 query digest 构成指纹。仅首次 INSERT 赢家调用已有 Application；冲突 409；未完成重复请求 202；已封存响应逐字复用。准入后失败或进程退出不再次授予 Execute。

`GET /api/runtime/runs/{run_id}/agent-quality-evidence/v1` 只查询/封存。它不创建 Run、不 claim Lease、不调用恢复、不升级 Go 已结束 UNKNOWN。封存依次锁 control/root/frame/iteration/Memory Scope，最后锁 Receipt；事务内没有 Provider I/O。数据库禁止修改 Receipt 身份和已封存响应。

## Evidence 边界

Bundle 为 `agent-quality-evidence.v1`，观察为 `agent-quality-observations.v1`。Wire 使用 `catalog-json-v1` 的整数子集：UTF-8、排序键、数组保序、紧凑 JSON、保留 null，禁止浮点数。Runtime 内部摘要继续使用原算法。

组件 256 KiB、Observations 512 KiB、Bundle 1 MiB、独立 Final Answer 64 KiB、HTTP 2 MiB；新入口读取旧 Memory Evidence 时，历史 Operation SQL 查询最多256项，超限组件 UNAVAILABLE/EVIDENCE_LIMIT。来源不足或超限不能声称 COMPLETE；无真实 Token usage 保留 null。普通 Evidence 不输出任务、完整参数、Tool/Child/Memory 正文、路径、凭据或 Provider 自由错误。参数明文投影仅限受管路径的 bool/int64 及固定公开枚举 LARGE/SMALL，其他字符串只可做完整参数摘要或标记不支持。Final Answer 仅通过既有受控通道，须匹配根 Final Receipt。

v1 引用适用的旧 Loop/Context/Memory 组件；v2 使用 Agent Execution 组件，并明确旧组件 `UNSUPPORTED_EXECUTION_POLICY`。新观察不修改旧 Evidence 合同。

## 当前阻断与错误

经 `176_sol_wp6_service_memory_write_decision.md` 局部裁决，新质量入口显式冻结 `SERVICE_PROJECT_READ_ONLY.v1`。`persist=True` 继续用于 PROJECT Scoped Memory 读取、绑定与 Guard；首次/恢复 Composition 均只关闭该模式的私人 Conversation Writer 和 Formation。私人 Scope 仍 HUMAN-only；缺少模式不根据当前身份推断跳过，不改变旧 Run。

终态封存以冻结策略、真实根 Final Receipt 和该 Run 的操作清单投影写入效果：Final 已交付且零写入时为 `NOT_ATTEMPTED/DISABLED_BY_POLICY`，无 Final 为 `NOT_REACHED`，未知来源保留 PARTIAL。它们均不等同 commit 成功。要求私人检索、Conversation 提交或 Formation 的 Case 返回 INCONCLUSIVE，不能将政策跳过算业务 PASS；公开 Memory Case 仅覆盖 PROJECT 授权读取和实际使用。

主要新拒绝码：`QUALITY_EVALUATION_DISABLED`（503）、`QUALITY_EVALUATION_BINDING_FORBIDDEN`（403）、`QUALITY_RUN_BINDING_INVALID` / `QUALITY_CONFIGURATION_PIN_INVALID`（422）、`QUALITY_REQUEST_CONFLICT`（409）、`QUALITY_RECEIPT_UNAVAILABLE`（404）。运行中保持 202/NOT_READY；未知结果不得自动重发。

两个 Required Evaluator、严格组件读取、真实请求/来源绑定、ChildResult 后续消费、可靠请求 BOUND_DELTA 和五 Case 发布/PG 比较 Gate 均已有定向证据，见180实施记录。私人 Conversation/Formation/CAS/删除质量不在该 SERVICE 模式支持范围，要求这些能力时仍不可判定；不外推通用语义、远程账单、容量或生产可靠性。
