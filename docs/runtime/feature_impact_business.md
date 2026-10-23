# 历史影响与测试覆盖业务合同

本域实现 `feature-impact-report.v1`，开发公开样本 H1～H5 已曝光；不代表最终盲态或真实 Agent 效果。资源执行、Coverage Truth 和 Go Evaluation Truth 分别拥有独立权威。

## 运行与持久化

正常 `server.py::lifespan()` 注册业务服务、两项 Tool、单个 AgentDefinition 和现有 Loop v2 编排。`FeatureInvestigationService` 本次固定 `execute_models=False`，调查请求返回 `409 REAL_AGENT_EXECUTION_NOT_ENABLED`，不创建假 Run。

无模型业务进程使用同一领域服务与 HTTP router，并复用既有 `Database`、schema readiness、JWT `AuthService` 和 Redis 准入：

```powershell
uv run python -m alembic upgrade head
uv run uvicorn core.feature_impact.app:create_app --factory --host 127.0.0.1 --port 8000
```

凭据、数据库、Redis 配置沿用 `core/settings.py` 与 `.ai/setup.md`。启动不执行迁移，不调用 Provider，不装配另一套 Agent Runtime。Redis 准入不可用时返回 503；数据库未迁移时启动失败。

迁移 `0039_feature_impact` 新增任务、报告、计划、执行、Run 绑定五表。任务 snapshot 不可原地修改；报告、计划、执行和绑定不可 UPDATE/DELETE。仅允许任务首次设置 `exposed_at`。显式演练 fixture 可以清理隔离测试库。当前没有历史数据迁移或兼容变更。

## HTTP 与身份

共同前缀 `/api/feature-impact`。HUMAN 只能访问同 tenant、同 owner 的对象；SERVICE 不能浏览任务、报告、计划或执行。

| 接口 | 行为 |
| --- | --- |
| `GET/POST /tasks` | 列出本人任务 / 显式创建冻结开发任务，POST 带 UUID `request_id` |
| `GET /tasks/{id}` | 冻结公开资料及报告目录，无内部资源轨迹或评分参考 |
| `GET /tasks/{id}/search?query=...` | 当前 manifest 与 cutoff 内的公开摘要/依赖 |
| `GET /tasks/{id}/evidence?source_ref=...` | 精确版本/chunk/digest 原文，越权或未来材料拒绝 |
| `POST /tasks/{id}/reports` | `{request_id,raw_output}`；只接受 HUMAN 来源，格式/证据错误保存 REJECTED 原文与原因 |
| `POST /tasks/{id}/rule-reports` | 固定 `dependency-lifecycle.v1`；从公开资源链、所有权及用例推导 |
| `POST /tasks/{id}/investigations` | 本次 NOT_EXECUTED，不调用模型 |
| `GET /reports/{id}` | 原始输出、封存报告、来源、拒收原因、原计划及执行 identity |
| `POST /reports/{id}/reviews` | `{request_id,report_version,report_digest,decisions}`；每项 finding_id/决定/理由；拒绝过期报告；原报告不覆盖 |
| `GET /coverage-plans/{id}` | 不可变计划，包含获准测试、审核人与理由、plan_digest |
| `POST /coverage-plans/{id}/executions` | `{request_id,plan_digest,test_id}`；只执行计划内测试，审核阶段不执行模拟 |
| `GET /executions/{id}` | 独立验证视图；业务期望/实际上报/差异判据，无内部轨迹 |
| `POST /quality/evaluate` | SERVICE 精确 scope `localagent:feature-impact:evaluate`；仅消费 EO 冻结公开投影，不能反查人类任务库，不接收 GT |

同一命令 identity 重放返回原记录；不同意图冲突。首次执行后拒绝该任务的新事前报告；创建新开发任务也不恢复盲态。已封存计划仍可显式执行获准对照，输入/报告不会被结果覆写。

两项只读 Tool 为 `search_feature_history(query)`、`read_feature_evidence(source_ref)`；无 task_id、路径或任意数据库条件。ToolAdapter 经 ToolRegistry/ToolPolicy/执行链和同步持久化 bridge 使用真实 Run/Session/Owner 绑定。Agent 报告必须匹配同 Run 已交付 FINAL receipt，引用 TASK_INPUT 或已提交且已观察的精确原文；搜索摘要不充当原文阅读证明。

## 模拟与评分

资源操作为 SUBMIT/DELIVER/COMPLETE/CONSUME；报告校验仅检查操作格式。只有显式执行请求才运行生命周期。共享消费读取槽位当时 payload；释放不清空；复用写入增加 generation。旧策略在共享消费者未结束时记录待资源，新策略允许复用。独立副本和独立资源从自身数据读取，校验器独立比较输入与实际上报。非法顺序属于 422 输入错误。

开发 C1/C2/C4/C5/C6 为 PASS，C3 为上报内容与输入不一致的 FAIL。结论由状态和事件产生，不按特性编号判故障。

Go Target 使用 `feature-impact-request.v1` / `feature-impact-output.v1`。冻结 Case 输入为 `{task_id,snapshot,candidate_kind,candidate_version,report}`；RULE 的 report 为 null，HUMAN 使用本人已封存输出，AGENT 本轮不进入该薄 Target。独立参考 `feature-impact-truth.v1` 仅在 EO Case GroundTruth 保存，不向业务 HTTP 发送。评分按特性+机制+条件匹配，分别保存必测发现/可操作性、开发未提示发现、确认、澄清、无效/重复和证据真实性。原始计数在 ResultValue provenance，Score 为 null；语义支持待审为 INCONCLUSIVE，不能自动 PASS。

前端地址 `/feature-impact`，经现有 Next.js rewrite 转发到 `LOCAL_AGENT_API_URL`。凭据仅保存在当前页面内存。无真人效率实验、真实 Agent、最终 Holdout 或 Agent 增量价值结论。
