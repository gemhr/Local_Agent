# 阶段二第 25 天补充：Evidence Manifest Hardening

## 1. 本轮目标

只修正阶段二 Evidence Manifest 的证据准确性、可追溯性和派生边界，不新增 Runtime 功能，不修改业务语义。

## 2. 修改前问题

原清单混用了 Capability status 与验证状态，`primary_test_ids` 中包含文件级目标，S2-001 的 Settings 合同被高估为 API E2E，RC 场景定义与 20/20 执行结果共用一条 Claim，部分派生报告也容易被误读为事实 Authority。

## 3. Status / Evidence 语义

`status` 只表示能力实现状态；Claim 是否验证由 `evidence_level`、`primary_test_targets` 与 `execution_evidence` 表达。`CONTRACT_ONLY` 是能力状态，不代表其数量 Claim 未验证。

## 4. Test Target 字段

字段已改名为 `primary_test_targets`，支持具体 pytest Node ID 与真实文件级目标，并用 `<br>` 表达联合证据。真实性测试会调用 pytest collection 验证全部目标。

## 5. Execution Evidence

新增 `execution_evidence`，只允许目标存在、RC Gate 执行、全仓执行、静态审计和负向断言五类固定值。执行标记来自既有 RC Gate、最终验收和本轮真实测试输出，不从 Markdown 的 `passed` 字样反推。

## 6. S2-001 修正

S2-001 改为 `CONTRACT`。Settings 默认值是主证据；已有请求级 `server.chat_endpoint` 测试作为 API 默认入口补充，但不把 Settings 测试本身升级为 API E2E。

## 7. S2-013 复核

S2-013 保留 `RUNTIME_E2E`。源码审计确认目标不是只调用 Model Router：它经 `ChatService.run_coordinated_agent` 创建 RunScope，执行 Coordinator，并消费 EventChannel 至 terminal，再断言 Run 结束状态与 fallback 结果。

## 8. RC Scenario Definition / Execution 拆分

S2-036 只描述 20 个 REQUIRED 场景定义完整并绑定真实目标；S2-037 独立描述 Release Gate 派生的 20/20 执行结果。二者使用不同测试来源，后续编号顺延至 S2-044。

## 9. Authority / Derivation Owner

列名改为 `authority_or_derivation_owner`。Runtime 事实填写真实 Owner；Shutdown、Fault coverage、RC Gate 和资源不变量等汇总填写真实 builder/evaluator 与 derivation，不把 Report 本身提升为 Runtime Authority。

## 10. Node ID 精确化

Composition Root、RunContext、AgentState、PlanStep、Terminal、Tool evidence、Journal-first、Observability、Trace、Fault coverage 与生产 Fault 隔离等条目已精确到直接断言目标。Exactly-once 与 Automatic compensation 的全系统不存在性仍主要依赖架构静态审计，因此保留文件级目标，不虚构更强 Node ID。

## 11. Manifest Summary

Summary 由 `tests._stage2_evidence_manifest` 解析表格生成，并由测试逐字段比对。当前共有 44 项：SUPPORTED 33、PARTIALLY_SUPPORTED 3、CONTRACT_ONLY 1、NOT_IMPLEMENTED 7；其余证据等级、目标精度和执行覆盖统计以 Manifest 末尾生成块为准。

## 12. 文档同步

最终验收文档中的 Claim 数量与字段描述已从 43 同步为 44；其他引用文档没有固定 Claim 数量，无需修改。Runtime 能力结论、RC 状态和原有测试规模没有因本轮文档加固而改写。

## 13. 安全边界

Manifest 继续禁止绝对路径、测试机信息、业务正文、原始异常、Provider 配置、私有地址和故障注入规则标识；Derived Report 不保存 live Runtime 对象。

## 14. 测试结果

Manifest 专项：`3 passed`；最终文档测试：`11 passed`；Contract/RC：`21 passed`；全仓：`1089 passed`；附加 subtests：`42 passed`。`compileall`、lock check 与 diff check：`PASS`。

## 15. 未完成事项

本轮没有实现 Recovery execution、Replay、Step result rehydration、Exactly-once、Automatic compensation、生产 Fault 或随机 Chaos。Exactly-once 与自动补偿仍是架构级不存在性审计，而不是生产验证结论。

## 16. 需要带回 ChatGPT 审查的信息

请重点审查：S2-013 的完整 Runtime 入口判断是否仍准确；文件级静态目标是否需要未来真实契约变化驱动的直接回归；execution evidence 是否始终与实际 Gate 集合和全仓输出同步。
