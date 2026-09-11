#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage6 进程级 Prometheus 与 OpenTelemetry 基础设施。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import logging
import time
from typing import Callable, Iterator, Mapping

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagators.textmap import Setter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from prometheus_client.exposition import start_http_server

from core.settings import Settings

logger = logging.getLogger(__name__)

HTTP_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
LONG_DURATION_BUCKETS = (0.1, 0.5, 1, 5, 15, 30, 60, 300, 900, 1800, 3600)
_KNOWN_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)
_STATUS_CLASSES = frozenset({"1xx", "2xx", "3xx", "4xx", "5xx"})
_OUTCOMES = {
    "cache": frozenset({"hit", "miss", "bypass", "error"}),
    "limiter": frozenset({"allowed", "rejected", "unavailable"}),
    "job_finalization": frozenset({"success", "failed", "cancelled", "duplicate", "conflict"}),
    "outbox_publish": frozenset({"success", "failure", "stale"}),
    "outbox_claim": frozenset({"claimed", "empty", "reclaimed"}),
    "kafka_producer": frozenset({"acked", "failed", "timeout"}),
    "kafka_consumer": frozenset({"processed", "duplicate", "cancelled_noop", "failed", "dlq"}),
    "offset_commit": frozenset({"success", "failure"}),
    "dlq_publish": frozenset({"acked", "failed"}),
    "worker_execution": frozenset({"success", "failed", "timeout", "cancelled_noop", "duplicate", "claim_conflict"}),
    "worker_claim": frozenset({"claimed", "reclaimed", "busy", "terminal"}),
}
_JOB_STATUSES = frozenset({"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"})
_DLQ_REASONS = frozenset({"invalid_schema", "intent_mismatch", "invalid_identity", "unsupported_event"})


@contextmanager
def fail_open_span(factory: Callable[[], object]) -> Iterator[object | None]:
    """隔离 tracing helper 的 start/enter/exit failure，不改变业务异常与结果。"""

    manager = None
    try:
        manager = factory()
        span = manager.__enter__()
    except Exception:
        logger.warning(
            "OpenTelemetry span start failed",
            extra={"component": "observability", "phase": "span", "status": "DEGRADED"},
        )
        yield None
        return

    try:
        yield span
    except BaseException as exc:
        try:
            manager.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            logger.warning(
                "OpenTelemetry span close failed",
                extra={"component": "observability", "phase": "span", "status": "DEGRADED"},
            )
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            logger.warning(
                "OpenTelemetry span close failed",
                extra={"component": "observability", "phase": "span", "status": "DEGRADED"},
            )


@dataclass(frozen=True, slots=True)
class MetricsPayload:
    body: bytes
    content_type: str = CONTENT_TYPE_LATEST


class ObservabilityService:
    """单进程唯一 Observability Owner；所有信号均不参与业务决策。"""

    def __init__(
        self,
        settings: Settings,
        *,
        process_role: str,
        span_exporter: SpanExporter | None = None,
    ) -> None:
        self.enabled = settings.observability_enabled
        self.metrics_enabled = self.enabled and settings.metrics_enabled
        self.tracing_enabled = self.enabled and settings.tracing_enabled
        self.metrics_path = settings.metrics_path
        self._shutdown_timeout_seconds = settings.observability_shutdown_timeout_seconds
        self._closed = False
        self._metrics_http_server = None
        service_name = settings.otel_service_name
        if service_name == "localagent-api":
            service_name = {
                "publisher": "localagent-outbox-publisher",
                "worker": "localagent-evaluation-worker",
            }.get(process_role, service_name)

        # 显式私有 Registry，避免全局 Registry 的重复时序与跨测试污染。
        self.registry = CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "localagent_http_server_requests_total",
            "HTTP server requests by route template and status class.",
            ("method", "route_template", "status_class"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "localagent_http_server_request_duration_seconds",
            "HTTP server request duration in seconds.",
            ("method", "route_template", "status_class"),
            buckets=HTTP_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.http_in_flight = Gauge(
            "localagent_http_server_in_flight_requests",
            "HTTP requests currently being processed.",
            ("method",),
            registry=self.registry,
        )
        self.rag_cache_requests = Counter(
            "localagent_rag_cache_requests_total", "RAG cache outcomes.", ("outcome",), registry=self.registry
        )
        self.rate_limit_decisions = Counter(
            "localagent_rate_limit_decisions_total", "Rate limiter decisions.", ("outcome",), registry=self.registry
        )
        self.redis_duration = Histogram(
            "localagent_redis_operation_duration_seconds", "Redis operation duration.", ("component",),
            buckets=HTTP_DURATION_BUCKETS, registry=self.registry,
        )
        self.jobs_created = Counter(
            "localagent_evaluation_jobs_created_total", "Durable evaluation jobs created.", registry=self.registry
        )
        self.job_transitions = Counter(
            "localagent_evaluation_job_transitions_total", "Durable job transitions.",
            ("from_status", "to_status"), registry=self.registry,
        )
        self.job_finalization = Counter(
            "localagent_evaluation_job_finalization_total", "Job finalization outcomes.",
            ("outcome",), registry=self.registry,
        )
        self.outbox_publish = Counter(
            "localagent_outbox_publish_total", "Outbox publication outcomes.", ("outcome",), registry=self.registry
        )
        self.outbox_claim = Counter(
            "localagent_outbox_claim_total", "Outbox claim outcomes.", ("outcome",), registry=self.registry
        )
        self.outbox_publish_duration = Histogram(
            "localagent_outbox_publish_duration_seconds", "Outbox publication duration.",
            buckets=HTTP_DURATION_BUCKETS, registry=self.registry,
        )
        self.kafka_producer_publish = Counter(
            "localagent_kafka_producer_publish_total", "Kafka producer broker outcomes.",
            ("outcome",), registry=self.registry,
        )
        self.kafka_producer_duration = Histogram(
            "localagent_kafka_producer_publish_duration_seconds", "Kafka producer ACK duration.",
            buckets=HTTP_DURATION_BUCKETS, registry=self.registry,
        )
        self.kafka_consumer_messages = Counter(
            "localagent_kafka_consumer_messages_total", "Kafka consumer processing outcomes.",
            ("outcome",), registry=self.registry,
        )
        self.kafka_offset_commit = Counter(
            "localagent_kafka_offset_commit_total", "Kafka offset commit outcomes.",
            ("outcome",), registry=self.registry,
        )
        self.kafka_dlq_publish = Counter(
            "localagent_kafka_dlq_publish_total", "Kafka DLQ broker outcomes.",
            ("outcome",), registry=self.registry,
        )
        self.kafka_dlq = Counter(
            "localagent_kafka_dlq_total", "Kafka DLQ bounded reason categories.",
            ("reason",), registry=self.registry,
        )
        self.worker_execution = Counter(
            "localagent_evaluation_worker_execution_total", "Evaluation worker outcomes.",
            ("outcome",), registry=self.registry,
        )
        self.worker_duration = Histogram(
            "localagent_evaluation_worker_duration_seconds", "Evaluation worker duration.",
            ("outcome",), buckets=LONG_DURATION_BUCKETS, registry=self.registry,
        )
        self.worker_claim = Counter(
            "localagent_worker_claim_total", "Worker claim outcomes.", ("outcome",), registry=self.registry
        )
        self.otel_exports = Counter(
            "localagent_otel_span_export_total",
            "OpenTelemetry span export outcomes.",
            ("outcome",),
            registry=self.registry,
        )
        self.postgresql_pool_connections = Gauge(
            "localagent_postgresql_pool_connections", "SQLAlchemy pool snapshot.",
            ("state",), registry=self.registry,
        )

        self._tracer_provider: TracerProvider | None = None
        self.tracer: Tracer = trace.get_tracer("localagent.disabled")
        exporter: SpanExporter | None = None
        if self.tracing_enabled:
            provider = TracerProvider(
                sampler=ParentBased(
                    TraceIdRatioBased(settings.otel_trace_sample_ratio)
                ),
                resource=Resource.create(
                    {
                        "service.name": service_name,
                        "service.version": settings.service_version,
                        "deployment.environment": settings.environment_profile.value.lower(),
                        "process.role": process_role,
                    }
                ),
            )
            if span_exporter is not None:
                exporter = _ObservedSpanExporter(span_exporter, self.otel_exports)
                provider.add_span_processor(SimpleSpanProcessor(exporter))
            else:
                exporter = self._build_otlp_exporter(settings)
            if span_exporter is None and exporter is not None:
                exporter = _ObservedSpanExporter(exporter, self.otel_exports)
                provider.add_span_processor(
                    BatchSpanProcessor(
                        exporter,
                        max_queue_size=512,
                        max_export_batch_size=128,
                        schedule_delay_millis=5000,
                    )
                )
            self._tracer_provider = provider
            self.tracer = provider.get_tracer(
                "localagent.backend", settings.service_version
            )

    @staticmethod
    def _build_otlp_exporter(settings: Settings) -> SpanExporter | None:
        endpoint = settings.otel_exporter_otlp_endpoint.strip()
        if not endpoint:
            return None
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        trace_endpoint = endpoint.rstrip("/")
        if not trace_endpoint.endswith("/v1/traces"):
            trace_endpoint += "/v1/traces"
        return OTLPSpanExporter(
            endpoint=trace_endpoint,
            timeout=float(settings.observability_shutdown_timeout_seconds),
        )

    @staticmethod
    def normalize_method(method: str) -> str:
        normalized = method.upper()
        return normalized if normalized in _KNOWN_HTTP_METHODS else "OTHER"

    @staticmethod
    def normalize_route_template(route_template: object) -> str:
        # route_template 只能来自 Starlette Route.path，永不接受 raw request path。
        if isinstance(route_template, str) and route_template.startswith("/"):
            return route_template[:256]
        return "unmatched"

    @staticmethod
    def normalize_status_class(status_code: int) -> str:
        candidate = f"{status_code // 100}xx"
        return candidate if candidate in _STATUS_CLASSES else "5xx"

    @staticmethod
    def _bounded(category: str, value: str) -> str:
        if value not in _OUTCOMES[category]:
            raise ValueError(f"unsupported {category} metric outcome")
        return value

    def observe_cache(self, outcome: str, duration_seconds: float | None = None) -> None:
        if not self.metrics_enabled:
            return
        self.rag_cache_requests.labels(outcome=self._bounded("cache", outcome)).inc()
        if duration_seconds is not None:
            self.redis_duration.labels(component="cache").observe(max(0.0, duration_seconds))

    def observe_limiter(self, outcome: str, duration_seconds: float | None = None) -> None:
        if not self.metrics_enabled:
            return
        self.rate_limit_decisions.labels(outcome=self._bounded("limiter", outcome)).inc()
        if duration_seconds is not None:
            self.redis_duration.labels(component="limiter").observe(max(0.0, duration_seconds))

    def observe_job_created(self) -> None:
        if self.metrics_enabled:
            self.jobs_created.inc()

    def observe_job_transition(self, from_status: str, to_status: str) -> None:
        if not self.metrics_enabled:
            return
        if from_status not in _JOB_STATUSES or to_status not in _JOB_STATUSES:
            raise ValueError("unsupported job status metric label")
        self.job_transitions.labels(from_status=from_status, to_status=to_status).inc()

    def observe_job_finalization(self, outcome: str) -> None:
        if self.metrics_enabled:
            self.job_finalization.labels(outcome=self._bounded("job_finalization", outcome)).inc()

    def observe_outbox_claim(self, outcome: str) -> None:
        if self.metrics_enabled:
            self.outbox_claim.labels(outcome=self._bounded("outbox_claim", outcome)).inc()

    def observe_outbox_publish(self, outcome: str, duration_seconds: float) -> None:
        if not self.metrics_enabled:
            return
        self.outbox_publish.labels(outcome=self._bounded("outbox_publish", outcome)).inc()
        self.outbox_publish_duration.observe(max(0.0, duration_seconds))

    def observe_kafka_producer(self, outcome: str, duration_seconds: float) -> None:
        if not self.metrics_enabled:
            return
        self.kafka_producer_publish.labels(outcome=self._bounded("kafka_producer", outcome)).inc()
        self.kafka_producer_duration.observe(max(0.0, duration_seconds))

    def observe_kafka_consumer(self, outcome: str) -> None:
        if self.metrics_enabled:
            self.kafka_consumer_messages.labels(outcome=self._bounded("kafka_consumer", outcome)).inc()

    def observe_offset_commit(self, outcome: str) -> None:
        if self.metrics_enabled:
            self.kafka_offset_commit.labels(outcome=self._bounded("offset_commit", outcome)).inc()

    def observe_dlq(self, *, outcome: str, reason: str) -> None:
        if not self.metrics_enabled:
            return
        if reason not in _DLQ_REASONS:
            raise ValueError("unsupported DLQ reason metric label")
        self.kafka_dlq_publish.labels(outcome=self._bounded("dlq_publish", outcome)).inc()
        if outcome == "acked":
            self.kafka_dlq.labels(reason=reason).inc()

    def observe_worker(self, outcome: str, duration_seconds: float) -> None:
        if not self.metrics_enabled:
            return
        value = self._bounded("worker_execution", outcome)
        self.worker_execution.labels(outcome=value).inc()
        self.worker_duration.labels(outcome=value).observe(max(0.0, duration_seconds))

    def observe_worker_claim(self, outcome: str) -> None:
        if self.metrics_enabled:
            self.worker_claim.labels(outcome=self._bounded("worker_claim", outcome)).inc()

    def update_database_pool(self, snapshot: Mapping[str, int]) -> None:
        if not self.metrics_enabled:
            return
        for state in ("checked_out", "checked_in", "overflow", "size"):
            value = snapshot.get(state)
            if isinstance(value, int):
                self.postgresql_pool_connections.labels(state=state).set(value)

    def begin_http_request(self, method: str) -> tuple[str, float]:
        normalized_method = self.normalize_method(method)
        if self.metrics_enabled:
            self.http_in_flight.labels(method=normalized_method).inc()
        return normalized_method, time.perf_counter()

    def finish_http_request(
        self,
        *,
        method: str,
        started_at: float,
        route_template: object,
        status_code: int,
    ) -> None:
        if not self.metrics_enabled:
            return
        route = self.normalize_route_template(route_template)
        status_class = self.normalize_status_class(status_code)
        labels = {
            "method": method,
            "route_template": route,
            "status_class": status_class,
        }
        self.http_requests.labels(**labels).inc()
        self.http_duration.labels(**labels).observe(
            max(0.0, time.perf_counter() - started_at)
        )
        self.http_in_flight.labels(method=method).dec()

    def start_server_span(self, headers: Mapping[str, str], method: str):
        if not self.tracing_enabled:
            return nullcontext(None)
        try:
            parent: Context = TraceContextTextMapPropagator().extract(
                headers, getter=_HeaderGetter()
            )
        except Exception:
            parent = Context()
        return self.tracer.start_as_current_span(
            "HTTP request",
            context=parent,
            kind=SpanKind.SERVER,
            attributes={"http.request.method": self.normalize_method(method)},
        )

    def capture_trace_context(self) -> dict[str, str]:
        if not self.tracing_enabled:
            return {}
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier, setter=_HeaderSetter())
        return {key: value for key, value in carrier.items() if key in {"traceparent", "tracestate"}}

    def start_messaging_span(
        self,
        name: str,
        *,
        carrier: Mapping[str, str] | None,
        kind: SpanKind,
        attributes: Mapping[str, str] | None = None,
    ):
        if not self.tracing_enabled:
            return nullcontext(None)
        parent: Context | None = None
        if carrier:
            try:
                parent = TraceContextTextMapPropagator().extract(carrier, getter=_HeaderGetter())
            except Exception:
                parent = Context()
        kwargs = {"kind": kind, "attributes": dict(attributes or {})}
        if parent is not None:
            kwargs["context"] = parent
        return self.tracer.start_as_current_span(name, **kwargs)

    @staticmethod
    def correlated_log_fields() -> dict[str, str | None]:
        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return {"trace_id": None, "span_id": None}
        return {
            "trace_id": f"{context.trace_id:032x}",
            "span_id": f"{context.span_id:016x}",
        }

    @staticmethod
    def finish_server_span(span, route_template: object, status_code: int) -> None:
        if span is None:
            return
        span.set_attribute(
            "http.route", ObservabilityService.normalize_route_template(route_template)
        )
        span.set_attribute("http.response.status_code", status_code)
        if status_code >= 500:
            span.set_status(Status(StatusCode.ERROR))

    def render_metrics(self) -> MetricsPayload:
        return MetricsPayload(generate_latest(self.registry))

    def start_metrics_http_server(self, port: int, *, address: str = "127.0.0.1") -> None:
        """为无 FastAPI 的 publisher/worker 暴露专用 Prometheus endpoint。"""
        if not self.metrics_enabled or port == 0:
            return
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("metrics port must be 1..65535 or 0 to disable")
        if self._metrics_http_server is not None:
            raise RuntimeError("metrics HTTP server already started")
        server, _thread = start_http_server(port, addr=address, registry=self.registry)
        self._metrics_http_server = server

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        metrics_server = self._metrics_http_server
        if metrics_server is not None:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(metrics_server.shutdown),
                    timeout=float(self._shutdown_timeout_seconds),
                )
                metrics_server.server_close()
            except Exception:
                logger.warning(
                    "Prometheus metrics server shutdown failed",
                    extra={"component": "observability", "phase": "shutdown", "status": "DEGRADED"},
                )
        provider = self._tracer_provider
        if provider is None:
            return
        timeout = float(self._shutdown_timeout_seconds)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(provider.force_flush, int(timeout * 1000)),
                timeout=timeout,
            )
        except Exception:
            logger.warning(
                "OpenTelemetry flush failed",
                extra={
                    "component": "observability",
                    "phase": "shutdown",
                    "status": "DEGRADED",
                },
            )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(provider.shutdown), timeout=timeout
            )
        except Exception:
            logger.warning(
                "OpenTelemetry shutdown failed",
                extra={
                    "component": "observability",
                    "phase": "shutdown",
                    "status": "DEGRADED",
                },
            )


class _HeaderGetter:
    def get(self, carrier: Mapping[str, str], key: str) -> list[str] | None:
        value = carrier.get(key)
        return [value] if value is not None else None

    def keys(self, carrier: Mapping[str, str]) -> list[str]:
        return list(carrier.keys())


class _ObservedSpanExporter(SpanExporter):
    """把 exporter failure 投影为安全、低基数证据，并保持 fail-open。"""

    def __init__(self, delegate: SpanExporter, counter: Counter) -> None:
        self._delegate = delegate
        self._counter = counter

    def export(self, spans) -> SpanExportResult:
        try:
            result = self._delegate.export(spans)
        except Exception:
            result = SpanExportResult.FAILURE
        outcome = "success" if result is SpanExportResult.SUCCESS else "failure"
        try:
            self._counter.labels(outcome=outcome).inc()
        except Exception:
            pass
        if outcome == "failure":
            logger.warning(
                "OpenTelemetry span export failed",
                extra={"component": "observability", "phase": "export", "status": "DEGRADED"},
            )
        return result

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)


class _HeaderSetter(Setter[dict[str, str]]):
    def set(self, carrier: dict[str, str], key: str, value: str) -> None:
        carrier[key] = value


class HttpObservabilityMiddleware:
    """纯 ASGI HTTP instrumentation；自身失败必须对业务 fail-open。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        app_state = getattr(scope.get("app"), "state", None)
        service = getattr(app_state, "observability_service", None)
        if not isinstance(service, ObservabilityService) or not service.enabled:
            await self.app(scope, receive, send)
            return

        method = service.normalize_method(scope.get("method", ""))
        try:
            method, started_at = service.begin_http_request(method)
        except Exception:
            await self.app(scope, receive, send)
            return
        status_code = 500

        async def observe_send(message) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        try:
            with fail_open_span(lambda: service.start_server_span(headers, method)) as span:
                try:
                    await self.app(scope, receive, observe_send)
                finally:
                    route = getattr(scope.get("route"), "path", None)
                    try:
                        service.finish_server_span(span, route, status_code)
                        logger.info(
                            "HTTP request completed",
                            extra={
                                "event": "http_request_completed",
                                "request_id": scope.get("state", {}).get("request_id"),
                                "trace_id": service.correlated_log_fields()["trace_id"],
                                "span_id": service.correlated_log_fields()["span_id"],
                                "method": method,
                                "route_template": service.normalize_route_template(route),
                                "status_class": service.normalize_status_class(status_code),
                            },
                        )
                    except Exception:
                        pass
        finally:
            route = getattr(scope.get("route"), "path", None)
            try:
                service.finish_http_request(
                    method=method,
                    started_at=started_at,
                    route_template=route,
                    status_code=status_code,
                )
            except Exception:
                # 指标永远不是业务 Authority；记录失败不得覆盖原响应/异常。
                pass


__all__ = [
    "HttpObservabilityMiddleware",
    "MetricsPayload",
    "ObservabilityService",
    "fail_open_span",
]
