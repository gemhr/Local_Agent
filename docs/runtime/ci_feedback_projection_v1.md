# CI 反馈安全投影 v1

`GET /api/ci/incidents/{incident_id}/feedback/{feedback_id}/projection` 复用现有 HUMAN principal、tenant/owner 权限。调用方只给业务引用，服务端重新读取 WP1 反馈、指定 Investigation、封存报告、Root、执行证据与修订状态。错误主体 404，绑定失效 409；没有 latest 推断。

Wire 为 `{projection, projection_digest}`；contract_version=`ci-feedback-projection.v1`。projection_digest 使用现有 `catalog_digest`（catalog canonical JSON + SHA-256），不能与原 Runtime digest 算法混用。白名单包含反馈/调查/Run 身份、可信 tenant/owner、报告版本及摘要、原 Runtime result 摘要、人工评价枚举/时间、来源维度、当前状态和有限观察。`evidence_bundle_digest` 是安全 observations 对象的 catalog 摘要，并非声称导出了完整 Runtime bundle。

不导出 reason/correction、Agent 假设正文、工具参数/正文、Memory、内部日志或 CoT；没有新增 Trace 输出。来源固定 SYNTHETIC，反馈来源标记 HUMAN；归因 verification_status 固定 INCONCLUSIVE，人工观点不会升级为 GT。

当前反馈状态 ACTIVE/SUPERSEDED/WITHDRAWN 按指定反馈是否有后继及其自身决策判断。查询旧版本仍显示旧版本及失效状态，不用新反馈覆盖原始版本。绑定失效拒绝投影。

归因只输出固定枚举：REJECT 证明 `OBSERVED_OUTCOME_DISAGREEMENT`；已证明实际读取及 Final 的强结论缺少要求的引用时输出 `STRONG_CONCLUSION_WITHOUT_REQUIRED_EVIDENCE`；缺执行证明输出 INCONCLUSIVE/缺口。复用 `ci_diagnosis.validate_report` 校验；不凭 Trace span 缺失认定未执行。普通 CI 调查尚无完整实际上下文 capture 合同，本版明确 UNAVAILABLE，不新增模型调用，也不猜 Prompt/Retrieval 错误。

Go 使用部署白名单拉取本接口，不能由 machine 请求提交原始投影正文、URL 或源端凭据。跨仓不会读取彼此数据库。部署前需由授权人员配置源端 HUMAN bearer，权限保持现有 owner 范围；本版没有扩大 AC SERVICE 权限。
