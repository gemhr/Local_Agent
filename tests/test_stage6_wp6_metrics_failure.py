"""WP6 Redis/DLQ metrics 与 observability failure isolation。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from redis.exceptions import RedisError

from core.evaluation_worker import KafkaEvaluationWorker, KafkaWorkerConfig, PermanentEvaluationError
from core.observability import HttpObservabilityMiddleware, ObservabilityService
from core.redis_service import (
    RagQueryCache,
    RedisTokenBucketRateLimiter,
    RedisUnavailableError,
    stable_digest,
)
from core.settings import Settings


def _settings(**changes):
    return replace(Settings.load(), **changes)


class _Redis:
    def __init__(self, *, value=None, decision=None, error: BaseException | None = None):
        self.value = value
        self.decision = decision
        self.error = error

    async def get(self, _key):
        if self.error:
            raise self.error
        return self.value

    async def delete(self, _key):
        return 1

    async def eval(self, *_args):
        if self.error:
            raise self.error
        return self.decision


@pytest.mark.asyncio
async def test_cache_and_limiter_metrics_follow_actual_outcomes(caplog) -> None:
    observability = ObservabilityService(_settings(), process_role="api")
    miss = RagQueryCache(_Redis(value=None), _settings(), observability=observability)
    assert await miss.get("digest-only-key") is None
    payload = {"schema_version": "safe"}
    envelope = json.dumps(
        {"schema_version": 2, "payload_digest": stable_digest(payload), "payload": payload}
    )
    hit = RagQueryCache(_Redis(value=envelope), _settings(), observability=observability)
    assert await hit.get("digest-only-key") == payload
    error = RagQueryCache(
        _Redis(error=RedisError("secret connection detail")),
        _settings(),
        observability=observability,
    )
    assert await error.get("digest-only-key") is None

    allowed = RedisTokenBucketRateLimiter(
        _Redis(decision=[1, 0]), _settings(), observability=observability
    )
    rejected = RedisTokenBucketRateLimiter(
        _Redis(decision=[0, 1000]), _settings(), observability=observability
    )
    unavailable = RedisTokenBucketRateLimiter(
        _Redis(error=RedisError("secret connection detail")),
        _settings(),
        observability=observability,
    )
    assert (await allowed.check("principal-secret")).allowed
    assert not (await rejected.check("principal-secret")).allowed
    with caplog.at_level(logging.WARNING), pytest.raises(RedisUnavailableError):
        await unavailable.check("principal-secret")

    rendered = observability.render_metrics().body.decode()
    for outcome in ("hit", "miss", "error"):
        assert f'localagent_rag_cache_requests_total{{outcome="{outcome}"}} 1.0' in rendered
    for outcome in ("allowed", "rejected", "unavailable"):
        assert f'localagent_rate_limit_decisions_total{{outcome="{outcome}"}} 1.0' in rendered
    assert "principal-secret" not in rendered
    assert "connection detail" not in rendered
    assert "Redis rate limiter unavailable" in caplog.text
    assert "secret connection detail" not in caplog.text


class _Producer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    def produce(self, topic, **kwargs):
        self.calls.append({"topic": topic, **kwargs})

    def flush(self, _timeout):
        if self.fail:
            return 1
        self.calls[-1]["callback"](None, None)
        return 0


class _Message:
    def __init__(self):
        self.event_id = uuid.uuid4()

    def value(self):
        return json.dumps({"schema_version": "invalid", "event_id": str(self.event_id)}).encode()

    def topic(self):
        return "jobs"

    def partition(self):
        return 0

    def offset(self):
        return 1


@pytest.mark.asyncio
async def test_dlq_metrics_use_bounded_reason_and_ack_truth() -> None:
    observability = ObservabilityService(_settings(), process_role="worker")
    worker = KafkaEvaluationWorker(
        SimpleNamespace(),
        SimpleNamespace(),
        KafkaWorkerConfig("broker", "client", "jobs", "dlq", "workers"),
        consumer=SimpleNamespace(),
        dlq_producer=_Producer(),
        observability=observability,
    )
    await worker._publish_dlq(_Message(), PermanentEvaluationError("unsupported event schema"))
    worker._dlq_producer = _Producer(fail=True)
    with pytest.raises(TimeoutError):
        await worker._publish_dlq(_Message(), PermanentEvaluationError("unsupported event schema"))
    rendered = observability.render_metrics().body.decode()
    assert 'localagent_kafka_dlq_total{reason="unsupported_event"} 1.0' in rendered
    assert 'localagent_kafka_dlq_publish_total{outcome="acked"} 1.0' in rendered
    assert 'localagent_kafka_dlq_publish_total{outcome="failed"} 1.0' in rendered
    assert "unsupported event schema" not in rendered


class _FailingExporter(SpanExporter):
    def export(self, _spans):
        return SpanExportResult.FAILURE

    def shutdown(self):
        return None


def test_trace_export_failure_does_not_change_http_business_result(caplog) -> None:
    observability = ObservabilityService(
        _settings(tracing_enabled=True, otel_trace_sample_ratio=1.0),
        process_role="api",
        span_exporter=_FailingExporter(),
    )
    app = FastAPI()
    app.state.observability_service = observability
    app.add_middleware(HttpObservabilityMiddleware)

    @app.get("/business")
    async def business():
        return {"status": "succeeded"}

    with caplog.at_level(logging.WARNING):
        response = TestClient(app).get("/business")
    assert response.status_code == 200
    assert response.json() == {"status": "succeeded"}
    rendered = observability.render_metrics().body.decode()
    assert 'localagent_otel_span_export_total{outcome="failure"} 1.0' in rendered
    assert "OpenTelemetry span export failed" in caplog.text
    asyncio.run(observability.close())
