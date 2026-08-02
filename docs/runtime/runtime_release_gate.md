# Stage2 Runtime RC1 Release Gate

本 Gate 适用于逻辑候选版本 `Stage2 Runtime RC1`。Gate 结论只能由真实测试与检查结果派生，Markdown 本身不是判定源。

## 判定规则

仅当 P0=0、P1=0、20/20 REQUIRED RC 场景通过、契约测试与全仓测试通过、资源不变量与安全扫描通过时为 `PASS`。P2 与 Known Limitation 必须披露，但不单独阻断发布。

## P0_BLOCKER

| ID | 阻断条件 |
|---|---|
| P0-01 | 默认入口不能启动 |
| P0-02 | 同一请求创建多个 Runtime |
| P0-03 | Coordinated/Legacy 发生跨 Runtime fallback |
| P0-04 | 业务副作用重复执行 |
| P0-05 | 一个 Run 产生多个 terminal |
| P0-06 | Event sequence 重用或非单调 |
| P0-07 | Journal digest 或 append-only 不变量失败 |
| P0-08 | AgentState 出现非法终态 |
| P0-09 | Registry/Permit/Reservation/Span 持续泄漏 |
| P0-10 | Active/detached worker 存在时关闭 Model |
| P0-11 | 生产请求可激活 Fault Injection |
| P0-12 | 敏感正文进入 Event/Journal/Wire/Report |
| P0-13 | 全仓测试失败 |

## P1_BLOCKER

| ID | 阻断条件 |
|---|---|
| P1-01 | Disabled Controller 改变正常行为 |
| P1-02 | Snapshot after-save 被自动重试或删除已提交记录 |
| P1-03 | Recovery 使用非持久测试 Evidence 作权威事实 |
| P1-04 | Client disconnect 后继续写输出 |
| P1-05 | Observability/Trace 故障改变业务结果 |
| P1-06 | ShutdownReport 伪报 fully closed |
| P1-07 | Legacy/Coordinated 文档与真实路径冲突 |
| P1-08 | 契约矩阵与代码不一致 |
| P1-09 | 任一 REQUIRED RC 场景未覆盖或失败 |

## P2_NON_BLOCKING

| ID | 当前发现 |
|---|---|
| P2-01 | 性能数据仅为当前机器离线 Fake 基线，不是生产 SLA |

## KNOWN_LIMITATION

| ID | 限制 |
|---|---|
| KL-01 | Snapshot 默认关闭，Recovery 仅 validation |
| KL-02 | 无 Replay/Resume/Step result rehydration |
| KL-03 | Legacy 不拥有完整 Journal/Snapshot/Recovery 能力 |
| KL-04 | 自定义 text/plain 流协议，不是标准 SSE/WebSocket |
| KL-05 | Registry 与 circuit state 为进程内对象 |
| KL-06 | 无随机 Chaos 与生产 Fault 激活 |
| KL-07 | 无全系统 exactly-once、自动补偿或分布式 durable execution |

## Derived Assessment

`ReleaseGateAssessment` 是测试辅助派生值，不是 Runtime Owner。它只保存固定 ID、计数和布尔检查结果，不保存路径、业务正文或原始异常。当前最终数值见第二轮结果文档；任何检查变化都必须重新计算，不能读取本文勾选项。

