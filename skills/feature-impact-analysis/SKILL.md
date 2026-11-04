---
name: feature-impact-analysis
description: 根据新特性或设计变更调查历史功能影响，分析共享资源、所有权、生命周期与延迟消费，读取支持证据和反证并提出黑盒测试。适用于事前影响分析，不用于声称已发生事故。
---
# 历史功能影响分析

## 适用边界
用于新设计或行为变更的历史兼容性调查。没有变更对象时先澄清；单次 CI 故障和普通语言解释不属于本包。

## 分析步骤
1. 从任务输入识别变更对象、旧行为、新行为及范围；区分已知事实和待确认假设。
2. 追踪共享资源的生产者、所有者、引用者、释放与复用时点，特别检查延迟消费者。区分共享引用和独立副本，不因名称相似认定依赖。
3. 对共享资源或生命周期变更，先用已授权的 `search_feature_history` 核对历史依赖与覆盖范围，即使任务已提供资料也要完成这项范围核对；再按需用 `read_feature_evidence` 精确核对关键影响机制的支持证据与反证。其他冻结任务文档可按 TASK_INPUT 引用，不要求为了合法引用而逐份重读；工具参数遵循实际 schema。
4. 设计资源模拟测试前，用 `read_skill_resource` 读取 [影响分析指南](references/impact-analysis-guide.md) 的“公开操作合同”，核对动作、精确字段及请求生命周期；分析共享内存或释放时点的不确定性时也参考该指南。必要的历史范围、关键证据和操作约束尚未核对完成时，不提交完整最终报告。
5. 同时寻找支持和反证：复制、独立资源、同步完成、所有权转移是否切断影响路径？资料缺失应提出澄清，不能把缺失当作没有风险。
6. 将影响候选与已证实缺陷分开。结论明确触发条件、机制、证据和未确定部分，不能把设计风险写成已发生事故。
7. 将每个有效候选转化为可执行黑盒测试，包含前置条件、操作顺序、观察点、预期和已有覆盖缺口；machine operations 只使用指南列出的四类合法动作，窗口/归档等业务含义写在自然语言观察字段，不自造操作。格式合法不等于顺序和预期已被执行证明。

## 输出合同
遵守现有 Feature Impact Report（feature-impact-report.v1）及宿主给出的 schema，不添加字段。最终直接返回 JSON 对象，不加 Markdown 代码围栏或解释前后缀。
引用真实 source_ref、version、chunk、content_digest。TASK_INPUT 表示本次冻结任务提供的文档，可用于支持或反证，无需先精确读取。
EVIDENCE_READ 仅用于本 Run 已成功调用 `read_feature_evidence`、且已观察到返回相同 source_ref 原文的记录。`search_feature_history` 只定位候选，命中不构成精确读取证明。同一文档同时存在于任务输入和成功读取结果时，按实际依据选择 basis。Skill 不是历史功能事实来源。
输出前逐条核对 supporting_evidence 与 counter_evidence：确认 source_ref；有本次成功精确读取证明才用 EVIDENCE_READ，否则若存在于冻结任务输入则用 TASK_INPUT；两种来源都没有则不得引用。不得将所有已看见的文档统一标为工具读取。
明确 supporting_evidence、counter_evidence、uncertainties 与 recommended_tests 的对应关系；每个 finding 必须包含 schema 要求的全部字段，包括 clarification_questions；没有澄清问题时用空数组，不省略字段。证据不足时填写具体澄清问题。
本包只提供方法，不自动授权或执行工具，不修改生产 execute_models 开关。宿主授权、用户要求和报告合同优先。
