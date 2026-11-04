---
name: ci-environment-check
description: 核对 CI 执行环境、版本能力、特性开关与用例前置条件。用户明确要求环境兼容性核对或证据指向环境问题时使用；综合故障调查优先使用 ci-failure-investigation。
---
# CI 环境兼容性检查

## 适用边界
用于环境配置与执行要求的证据化对照，不负责自动安装、升级或修改环境。综合 CI 首因调查优先使用 ci-failure-investigation，不要求每次故障都加载本包。

## 核对步骤
1. 通过已授权的 `ci_get_execution_context` 确认执行身份与证据目录；参数遵循实际工具 schema。
2. 定位 ENVIRONMENT_RECORD 和 CASE_REQUIREMENTS，使用 `ci_read_execution_evidence` 读取原文。目录摘要不能替代实际读取。
3. 需要逐项比较版本、能力、开关和缺失字段时，读取 [环境核对清单](references/environment-checklist.md)。
4. 将用例所需值、环境当前值及判定依据对应起来，分别检查格式版本、工具支持范围、特性开关和必要前置条件。未声明的范围不自行补全。
5. 区分明确不兼容、已证实满足、信息缺失；环境兼容不等于证明整个执行成功。
6. 必要时补读相关日志或回执，确认不兼容是否与失败阶段有关。不能因为存在环境差异就排除其他候选原因。

## 报告与证据
继续使用当前 DiagnosisReport，不创建环境专属 Schema。引用实际读取且 AVAILABLE 的 evidence_id/digest；只提出检查建议，不声称修复已执行。
当前 SUPPORTED 要求读取并引用 PARSER_LOG、ENVIRONMENT_RECORD、CASE_REQUIREMENTS，且环境能力值不能缺失；仅有环境对照而不满足该条件时，保留 INSUFFICIENT_EVIDENCE 与具体缺口。
分类与字段限制以宿主合同为准。Skill 不授予工具权限，也不会自行调用工具；用户要求、宿主安全和输出合同优先。
