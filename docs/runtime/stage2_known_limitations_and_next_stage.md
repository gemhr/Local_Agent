# Stage2 Known Limitations and Next-stage Handoff

全部条目状态均为 `not_started`；本文件是计划，不是完成证明。

| priority | item | why | dependency | risk | acceptance_criteria | not_started |
|---|---|---|---|---|---|---|
| P0_NEXT | 真实外部依赖受控集成环境 | code-level RC 未验证真实 Model/Embedding/Vector/Tool/DB | 隔离账号、数据与网络 | 测试泄密或副作用 | 安全 fixture、审批、可重复集成报告 | true |
| P0_NEXT | 统一配置验证与安全错误分类 | 仅局部 `RUNTIME_CONFIGURATION_ERROR` | Settings schema/启动边界设计 | 运维错误不可稳定分类 | Settings 全字段 taxonomy 与负向测试 | true |
| P0_NEXT | CI 安全摘要接入 | Gate 目前仅离线 helper | CI 权限与 artifact policy | 原始日志泄密 | 仅 allowlist 字段且阻断规则可审计 | true |
| P0_NEXT | 文档与代码持续一致性 Gate | 当前真实性测试需持续执行 | CI 与 ownership | 后续漂移 | 每次变更自动运行 manifest/config/code tests | true |
| P1_NEXT | 标准 SSE 协议 | 当前为 text/plain + control line | client migration plan | 兼容破坏 | 标准 framing、断连与 backpressure 测试 | true |
| P1_NEXT | 生产指标 Exporter | 当前仅进程内 recorder/snapshot | metric naming/security review | 高基数或敏感 label | exporter、scrape、安全 label 测试 | true |
| P1_NEXT | 受控 Soak Test | 无长时间稳定性证据 | 集成环境与资源预算 | 慢泄漏未发现 | 明确时长/workload/owner trend 报告 | true |
| P1_NEXT | 外部依赖 Failure Simulation | 当前 Offline Fake 为主 | 受控代理/模拟器 | 行为与真实 provider 偏差 | timeout/rate limit/partial response 矩阵 | true |
| P1_NEXT | Snapshot 默认策略评估 | 默认关闭是当前合同 | 数据保留/成本/隐私策略 | 存储成本与隐私 | 决策记录、迁移与 rollback 测试 | true |
| P1_NEXT | 人工 Reconciliation Record 合同 | 当前依赖外部工单 | 审批与审计 owner | 结论散落/不可追踪 | 独立 schema/store 决策，不改历史 authority | true |
| P1_NEXT | Step Result Rehydration 设计 | Recovery 无输出重建 | 安全结果 schema | 恢复错误或正文泄露 | 版本化安全 result contract 与 corruption tests | true |
| P2_LATER | 跨进程 Registry | 当前进程内 owner | durable coordination | split-brain | lease/identity/consistency 设计评审 | true |
| P2_LATER | Durable Execution | 当前无 durable scheduler | registry、result schema、recovery | 重复执行 | 明确执行/持久化模型和故障语义 | true |
| P2_LATER | Recovery Execution / Replay | 当前只读 validation | durable executor、rehydration | 副作用重复 | replay plan、审批、幂等与端到端测试 | true |
| P2_LATER | 分布式 Lease / Circuit State | 当前单进程状态 | shared store/clock | 竞争与误熔断 | 一致性、过期与分区测试 | true |
| P2_LATER | Production Chaos | 当前无生产激活 | 安全控制面与审批 | 真实流量损害 | allowlist、kill switch、审计与 blast radius | true |
| P2_LATER | Automatic Compensation | 当前仅 evidence | 业务级补偿合同 | 二次副作用 | 每 Tool 显式合同、审批和验证 | true |
| RESEARCH_ONLY | 全系统 Exactly-once | 分布式副作用不可由局部幂等保证 | 事务/外部系统协作 | 虚假保证 | 研究报告与可证明边界 | true |
| RESEARCH_ONLY | 任意 Tool 通用自动补偿 | 补偿高度业务相关 | domain contracts | 错误补偿扩大损失 | 证明适用域或明确不可行 | true |
| RESEARCH_ONLY | 不可中断 C Extension Worker 强制安全终止 | Python 无安全线程强杀语义 | 进程隔离/worker architecture | 进程损坏 | 研究进程隔离替代方案 | true |
