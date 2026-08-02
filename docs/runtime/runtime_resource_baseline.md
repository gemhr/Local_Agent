# Stage2 Runtime RC1 Resource Baseline

该基线只使用离线 Fake Adapter，数据仅描述本机测试环境，不构成跨机器或生产 SLA。发布硬 Gate 是 owner 计数归零与事实保真，不是耗时阈值。

## Sequential baseline

- 规模：50 次轻量 Coordinated Run；
- 指标：总耗时、平均、中位数、P95；
- 每 Run：独立 sequence，从 1 单调递增；Journal records/run 数量稳定；Snapshot 关闭时 rows=0；
- 最终硬不变量：Registry handles=0、active reservations=0、active permits=0、active spans=0、active workers=0、pending watchers=0、request producers=0、channel owners=0。

本轮当前机器采样：总耗时 0.088393 s，平均 0.001768 s，中位数 0.001744 s，P95 0.002181 s，Journal records/run=5，Snapshot rows=0。测试本身不以耗时做断言。

## Concurrent baseline

- 规模：10 个共享 Application services、相互独立的 Coordinated Run；
- 每 Run 拥有不同 run_id 与独立 sequence owner；
- Application component 不缓存 Run/Controller；
- 完成后 Registry、Channel、Span 等 owner 计数归零；无未取回 Task 异常。
- 本轮采样：10/10 完成，10 个 unique run_id。

## Cancellation baseline

- 10 个独立 RunContext，取消其中 5 个；
- 首次 `REQUEST_CANCELLED` 保留，后续 shutdown reason 不覆盖；
- 其余 5 个 token 不受影响；验证 first-wins 与无跨请求取消。

## Memory baseline

- warm-up 5 次后，使用标准库 `tracemalloc` 连续采样两个 10-run batch；
- 只报告 batch 间 retained allocation trend，不设置阻断阈值；
- Journal/Snapshot 的预期持久记录增长不能直接认定为泄漏；owner 计数仍须归零。
- 本轮趋势采样：batch 1 `+185438 bytes`，batch 2 `+107334 bytes`；仅记录，不作 SLA/Gate 阈值。

## Worker truth

普通场景 active/detached worker 均须归零。Detached 场景允许非零，但必须保留在 worker snapshot 中、延迟关闭 model、且 `fully_closed=false`；禁止清空记录伪造成功。
