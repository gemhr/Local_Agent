# Tool Governance

## Risk levels
ToolRiskLevel 包含 LOW、MEDIUM、HIGH 与 CRITICAL。默认 approval_required_threshold 为 HIGH。

## Denial rule
多个策略结果冲突时遵循 DENIAL_DOMINATES，任一明确拒绝都不能被较弱的允许结果覆盖。

## Registry
ToolRegistry 是工具 capability 与实现绑定的 owner。未注册工具不能通过临时名称绕过治理。
