# LocalAgent Backend Observability Operations

## 1. Signals and ownership

每个 API、Outbox Publisher、Evaluation Worker 进程各自创建一个
`ObservabilityService`。Prometheus metrics、OpenTelemetry spans 与日志关联字段都是
Observation，不是 Job、Outbox、Consumer Dedup、Kafka Offset、Runtime Event/Journal 或
Retrieval Authority。关闭 observability 或 exporter 故障不得改变业务结果。

API 默认通过 `GET /metrics` 暴露 Prometheus text exposition；该 infrastructure endpoint
不经过 end-user JWT 或 Redis limiter，访问边界由部署网络/ingress 控制。Publisher/Worker
可传 `--metrics-port <port>` 启动绑定 `127.0.0.1` 的专用 metrics server；`0` 表示不启动。
Scrape 只读取进程内 Registry 与 SQLAlchemy pool 的本地公开统计，不执行依赖网络查询。

## 2. Metrics

核心指标：

- HTTP：`localagent_http_server_requests_total`、
  `localagent_http_server_request_duration_seconds`、
  `localagent_http_server_in_flight_requests`；
- PostgreSQL：`localagent_postgresql_pool_connections{state}`；
- Redis：`localagent_rag_cache_requests_total{outcome}`、
  `localagent_rate_limit_decisions_total{outcome}`、
  `localagent_redis_operation_duration_seconds{component}`；
- Job/Outbox：`localagent_evaluation_jobs_created_total`、
  `localagent_evaluation_job_transitions_total`、
  `localagent_evaluation_job_finalization_total`、
  `localagent_outbox_claim_total`、`localagent_outbox_publish_total` 与 duration；
- Kafka/Worker/DLQ：`localagent_kafka_producer_publish_total`、producer duration、
  `localagent_kafka_consumer_messages_total`、`localagent_kafka_offset_commit_total`、
  `localagent_evaluation_worker_execution_total`、worker duration、
  `localagent_worker_claim_total`、`localagent_kafka_dlq_publish_total`、
  `localagent_kafka_dlq_total{reason}`。

所有 label 在代码中由固定 enum/allowlist 约束。禁止 request/trace/span/user/principal/job/event/
run/approval identity、raw path/query/prompt、Redis key、Kafka offset、exception text 进入 label。
HTTP route 只使用 Starlette route template；DLQ reason 只允许
`invalid_schema|intent_mismatch|invalid_identity|unsupported_event`。

未实现 PostgreSQL 定期 durable backlog sampler，因此当前没有 `jobs_queued`、
`outbox_pending` 或 oldest-age gauges；不能用进程本地加减计数伪装 durable truth。

## 3. OpenTelemetry

`LOCAL_AGENT_TRACING_ENABLED=1` 创建真实 SDK `TracerProvider`；默认
`ParentBased(TraceIdRatioBased(LOCAL_AGENT_OTEL_TRACE_SAMPLE_RATIO))`。配置
`LOCAL_AGENT_OTEL_EXPORTER_OTLP_ENDPOINT` 后使用 OTLP/HTTP bounded batch exporter；未配置
endpoint 时 span 不外发且业务正常。默认 service names：`localagent-api`、
`localagent-outbox-publisher`、`localagent-evaluation-worker`。

HTTP 只提取 W3C `traceparent/tracestate`，不传播 baggage。Job submission 将这两个受限字段
随 Outbox 事务持久化；Publisher 重启后从 PostgreSQL 恢复，在 Kafka produce span 中注入
headers；Worker 从 headers 建 consumer/process span，并建立 evaluation child span。缺失或
malformed context 启动新 trace，绝不使 Job 失败。

Span attributes 只包含 method、route template、status、operation、component 等低风险字段。
JWT、Authorization、API key、DSN/Redis/Kafka credential、prompt/query、retrieved text、完整
evaluation input/result 不进入 span。HTTP、Publisher、Worker 关键日志自动增加当前 OTel
`trace_id/span_id`；Request ID 仍是独立本地相关标识，不等同 trace ID。

Exporter 使用有界 queue；shutdown 做 best-effort bounded flush/shutdown。业务请求不调用
`force_flush()`，export failure 不映射 HTTP/Job/Worker failure。

## 4. Liveness and readiness

`GET /health` 只回答 API 进程是否仍处于可存活 lifecycle；Kafka、Redis、PostgreSQL 单独
故障不直接改变 liveness。

`GET /readyz` 每次执行 bounded read-only component checks，并与 Runtime admission 合并：

| Process | Required | Optional/degraded |
| --- | --- | --- |
| API | PostgreSQL、Redis limiter、Runtime accepting | Redis cache；Kafka 不检查 |
| Publisher | PostgreSQL、Kafka job topic | 无 |
| Worker | PostgreSQL、Kafka job topic | Redis cache 不检查 |

检查只执行 PostgreSQL `SELECT 1`、Redis `PING` 与 Kafka full-metadata topic lookup；Kafka
lookup 不传 missing topic 参数，不触发 auto-create。每项输出 `status`、bounded
`reason_code` 与 `latency_ms`，不返回 exception、host、DSN、credential 或 stack trace。

Publisher/Worker probe：

```powershell
uv run python -m scripts.healthcheck --component publisher
uv run python -m scripts.healthcheck --component worker
```

exit code `0=ready`，非零为 not ready。API 的 `--component api` 读取真实 `/readyz`，因此与
同一 Runtime admission 及 dependency readiness 事实一致；Publisher/Worker 直接执行各自
只读 dependency 检查。检查不创建 Job、不写 Outbox、不发 Kafka message、不修改 limiter state。

## 5. Failure diagnosis

- 系统慢：先看 HTTP latency/in-flight，再看 Redis operation latency、PG pool checked-out 与
  Worker duration；
- 消息积压：看 Outbox publish failure/stale、claim empty/reclaimed、Kafka producer ACK
  latency；durable backlog age 尚需直接数据库运维查询；
- Kafka 故障：API 仍可 ready；Publisher/Worker probe not ready，producer/offset/DLQ failure
  counter 增长；
- Cache Redis 故障：`rag_cache outcome=error`、`redis_cache=degraded`，RAG 回源继续；
- Limiter Redis 故障：`rate_limit outcome=unavailable`、`redis_limiter=unavailable`，API
  protected traffic fail-closed 503；
- Worker crash/timeout：Worker timeout/failure、reclaimed claim 与未提交 offset 后的重投可见；
- DLQ 突增：按四个 bounded reason 判断 schema、intent、identity 或 unsupported event 问题；
  DLQ publish failure 表示原 offset 不应推进。

## 6. SLI candidates

候选 SLI：HTTP success rate/latency、limiter unavailable rate、RAG cache hit/error rate、
Outbox publish failure rate、Evaluation success/failure rate、Worker duration、Kafka DLQ rate。
Outbox backlog age 是合理候选，但当前尚无 sampler metric。可据错误率持续上升或 oldest age
超过业务预算建立未来 alert；当前仓库没有部署 Grafana、Alertmanager、Collector 或真实 SLO
alert，不得声称这些已经上线。

## 7. Known limitations

没有 Grafana/Alertmanager/production Collector/hosted APM；没有 durable backlog sampler、
consumer lag dashboard、SQL statement auto-instrumentation、HA observability backend 或完整性能
压测。Publisher/Worker metrics HTTP server 需显式 `--metrics-port`，部署层负责端口与网络边界。
