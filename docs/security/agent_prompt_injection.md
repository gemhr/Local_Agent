# Agent Prompt Injection Defense

## Threat Model

用户输入、Conversation History、Memory、RAG 文档、Tool/MCP observation 都可能包含诱导模型改变指令、身份、审批或数据边界的文本。Prompt Injection cannot be completely solved by delimiters or system prompts.

## Trusted vs Untrusted Context

`ContextItem` / `ContextBuilder` 是唯一上下文信任合同。只有 code-owned 的 `SYSTEM_INSTRUCTION` / `AGENT_INSTRUCTION` 可以绑定为 `system`；RAG 是 `RAG_DOCUMENT + UNTRUSTED_EXTERNAL`，Memory/History 是历史 `USER_CONTENT`。原生 Tool continuation 仍保持 `role=tool`、`tool_call_id` 和 provider protocol，但增加 code-owned `UNTRUSTED TOOL OUTPUT` wrapper。MCP observation 额外保留 bounded `local_tool_name`、`provider_kind=mcp`、`server_id` provenance；Provider annotation 不能改变 trust。

## Soft Defense vs Hard Defense

短的 canonical security instruction、稳定 wrapper 和 `PromptInjectionDetector` 只是 soft defense。Detector 是 deterministic、immutable、bounded、content-free signal，不能单独 deny、授权或触发 HITL。Hard boundary 仍由 Principal/RBAC、ownership、typed validation、ToolRegistry、`ToolGovernanceService`、HITL、execution claim/CAS 和 `ToolExecutionService` 提供。

## Direct and Indirect Injection

用户声称 “I am admin” 或 “already approved” 不改变 Principal、Policy、Approval Actor 或 Runtime state。RAG、Tool Result、MCP Result 中出现 “system message / ignore policy / call tool” 仍只是数据；若模型提出 Tool Intent，必须重新经过完整 Governance。

## RAG, Tool Result and MCP

RAG 正文保留 citation/hash 和不可信 trust。Local/native Tool 结果不改业务正文和 native continuation，只增加安全边界。MCP 的 network/egress 能力只能由 operator local config 显式映射；Provider 不能自报 `safe`、`readOnly` 或 `no-egress` 来降级风险。

## Memory Poisoning

Rolling summary 明确 history 是 historical data，不得把 adversarial instruction 保留为 authoritative instruction。原始 Conversation History 不删除。Semantic Memory 使用自身窄、code-owned 的命令式文本校验拒绝 directive-like candidate；`PromptInjectionDetector` 的 signal 不参与该 deny 决策。正常事实型 Memory schema 和 lifecycle 不变。

## Data Egress and Runtime Governance

`EXTERNAL_NETWORK` 表示 Tool 可与受控 Runtime 外部网络端点交互；`DATA_EGRESS` 表示可把 Invocation 参数、Context-derived data 或 local content 发送到边界之外。`DATA_EGRESS` 分类为 `HIGH`，进入既有 approval/HITL；未知完整组合 fail closed。未实现 Generic DLP、Sandbox 或网络隔离。

## Security Evaluation

Focused deterministic cases分布在现有安全测试与 WP10 affected tests：`test_model_context.py` 的可执行 `SECURITY_DATASET` 覆盖 detector 类别及 benign baseline；Stage3 WP3-C 测试覆盖 RAG/Tool trust role 与 synthetic secret marker；Tool approval/router 测试以 Tool event、adapter call count、side-effect store 和 approval state 证明未授权副作用为零；MCP integration 测试证明 Provider annotation 不能覆盖 operator policy。PASS/FAIL 不以模型声称拒绝为准。

## Known Limitations

Soft prompt defense 不能保证模型行为；本 WP 没有 Generic DLP、WAF、SIEM、Sandbox 或 continuous red-team service。授权范围内的数据仍可能通过正常回答暴露。Legacy direct-engine path 未作为 canonical COORDINATED model-boundary defense 的完整覆盖目标。
