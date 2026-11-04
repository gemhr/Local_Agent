---
name: ci-failure-investigation
description: 调查 CI 执行失败，依据日志、执行回执、环境和用例要求辨别首因与连锁异常。综合故障调查使用此 Skill；以环境兼容性核对为主时优先使用 ci-environment-check。
---
# CI 故障调查

## 适用边界
用于具体 CI 执行失败的证据调查。不用于普通编程解释、没有执行上下文的泛化猜测或自动修复。
若用户主要要求核对环境兼容性，优先选择 ci-environment-check；只有任务确有需要才结合两者。

## 调查步骤
1. 使用宿主提供且已授权的 `ci_get_execution_context` 获取本次执行身份、失败阶段与证据目录。工具参数以实际 schema 为准，不猜测执行 ID 或证据 ID。
2. 获取上下文后，先调用 `read_skill_resource(name="ci-failure-investigation", relative_path="references/failure-evidence-guide.md")` 阅读[取证指南](references/failure-evidence-guide.md)，再开始阶段传播分析。该指南提供三类证据判据，不是业务事实；只读一次即可。
3. 按失败阶段选择 `ci_read_execution_evidence`：先读最早失败阶段的日志或执行回执，再核对关联环境和用例要求；不要将最后一条错误直接认定为首因。
4. 将阶段输入、失败动作、输出缺失、下游报错按时间和依赖连接。区分“解析失败导致比较输入缺失”和独立比较失败；缺少连接证据则保留多个候选。
5. 对照 ENVIRONMENT_RECORD 与 CASE_REQUIREMENTS。用实际支持能力和要求判断，不能仅凭工具版本新旧推定兼容性。
6. 仅以实际读取且 AVAILABLE 的证据支持结论，原样引用 evidence_id 与 digest。Skill 方法不是业务证据；不可编造日志、工具读取或修复结果。

## 输出合同
遵守当前宿主的 DiagnosisReport 合同，不添加 Schema 或分类。failure_stage 使用 PARSER、COMPARISON、AUTOMATION 或 UNKNOWN。
当前 SUPPORTED 要求实际读取并引用 PARSER_LOG、ENVIRONMENT_RECORD、CASE_REQUIREMENTS，且环境能力值不能缺失；不满足时输出 INSUFFICIENT_EVIDENCE，并列 uncertainties 和可执行 recommended_checks。
只建议检查和责任角色，不执行修复。宿主授权、用户任务与报告合同优先于本方法；工具是否可调用由宿主决定，WP1 包本身不提供自动执行能力。
