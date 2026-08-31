# Taste

## Execution workflow
- Once authorized, prefers direct autonomous execution without further confirmation: pushes all changes to main (including user-created docs) with Chinese summaries ("直接push两仓的修改到main，不需要检查，我授权了", "你直接执行吧", "push所有修改到main，中文摘要"). Confidence: 0.9

## Multi-agent pipeline
- Runs a two-model pipeline and explicitly assigns roles: ZCode / DeepSeek as the execution/remediation agent, Codex as the architecture-decision and gate-review agent ("本轮任务你的角色是ZCode / DeepSeek"). Confidence: 0.75
- Prefers staged gate reviews that re-verify from current source, the real Git diff, and actually executed tests rather than trusting handoff claims ("不得直接把 believed closed 当成 PASS"; truth priority: current source / Git diff / executed tests > docs > handoffs). Confidence: 0.75
